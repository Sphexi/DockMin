"""Stack export pages: the confirmation and the streamed archive download, Compose and Swarm."""

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from datetime import UTC
from typing import Annotated, Final

import anyio
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import StringConstraints
from starlette.types import Receive, Scope, Send

from app.auth.dependencies import require_administrator_or_not_found, require_user
from app.auth.errors import SelfProtected, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.stack import StackKind
from app.services.export_manifest import MAX_VOLUMES
from app.services.export_sealing import PASSPHRASE_MAX_CHARS, check_passphrase
from app.services.host_objects import busy_view
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectBusy
from app.services.stack_export import (
    StackChangedSinceReview,
    StackExportBusy,
    StackExportRefused,
    StackExportStream,
)
from app.services.stack_export_assembly import StackExports, open_stack_export_service
from app.web.identifiers import PathId
from app.web.object_pages import Failure, Field, busy_message, page_response, problem_page, route
from app.web.problems import unreachable_copy
from app.web.stack_routes import StackName
from app.web.swarm_routes import Namespace, swarm_path, swarm_router

__all__ = ["router", "swarm"]

router = APIRouter(
    prefix="/environments/{environment_id}/stacks",
    tags=["stacks"],
    dependencies=[Depends(require_user)],
)
swarm = swarm_router()

RETRY_SECONDS: Final = "5"
HEADING: Final = "Export stack"
TEMPLATE: Final = "stacks/export_confirm.html"
SEALED: Final = "sealed"
OMITTED: Final = "omitted"
VOLUME_NAME_MAX: Final = 255
PASSPHRASE_FIELD_MAX: Final = PASSPHRASE_MAX_CHARS * 2
Administrator = Annotated[Principal, Depends(require_administrator_or_not_found)]
Secret = Annotated[str, Form(max_length=PASSPHRASE_FIELD_MAX)]
ChosenVolumes = Annotated[
    list[Annotated[str, StringConstraints(max_length=VOLUME_NAME_MAX)]],
    Form(max_length=MAX_VOLUMES + 1),
]

REFUSALS: Final = (
    SelfProtected,
    ObjectBusy,
    ValidationFailed,
    StackExportRefused,
    EnvironmentUnavailable,
    ObjectTooLarge,
)


def _detail_href(request: Request, kind: StackKind, environment_id: int, name: str) -> str:
    if kind == "compose":
        return route(
            request, "environment_stack_detail", environment_id=environment_id, stack_name=name
        )
    return swarm_path(request, "stack_detail", environment_id=environment_id, namespace=name)


def _export_href(
    request: Request, kind: StackKind, environment_id: int, name: str, *, confirm: bool
) -> str:
    suffix = "_confirm" if confirm else ""
    if kind == "compose":
        return route(
            request,
            f"environment_stack_export{suffix}",
            environment_id=environment_id,
            stack_name=name,
        )
    return swarm_path(
        request, f"stack_export{suffix}", environment_id=environment_id, namespace=name
    )


def _failure_of(request: Request, error: Exception, actor: Principal) -> Failure:
    if isinstance(error, ObjectBusy):
        busy = busy_view(error, actor, request.app.state.auth.clock())
        return Failure(409, "This stack is busy", busy_message(busy, "it"))
    if isinstance(error, SelfProtected):
        return Failure(409, "Protected", str(error))
    if isinstance(error, StackChangedSinceReview):
        return Failure(
            409, "The stack changed", "The stack changed after you reviewed it. Review it again."
        )
    if isinstance(error, ValidationFailed):
        return Failure(400, "Check the request", "; ".join(error.fields.values()))
    if isinstance(error, StackExportRefused):
        return Failure(409, "The stack cannot be exported", str(error))
    return Failure(503, *unreachable_copy(error))


async def _confirm(
    request: Request,
    exports: StackExports,
    actor: Principal,
    kind: StackKind,
    environment_id: int,
    name: str,
) -> Response:
    detail_href = _detail_href(request, kind, environment_id, name)
    try:
        confirmation = await exports.confirmation(actor, environment_id, kind, name)
    except REFUSALS as error:
        return problem_page(
            request,
            heading=HEADING,
            failure=_failure_of(request, error, actor),
            back_href=detail_href,
            back_label="Back to the stack",
            region=None,
        )
    return page_response(
        request,
        TEMPLATE,
        {
            "confirmation": confirmation,
            "detail_href": detail_href,
            "run_href": _export_href(request, kind, environment_id, name, confirm=False),
            "kind_label": "Compose stack" if kind == "compose" else "Swarm stack",
            "passphrase_max": PASSPHRASE_MAX_CHARS,
        },
    )


def _passphrase(choice: str, passphrase: str, confirmation: str) -> str | None:
    if choice not in {"", SEALED, OMITTED}:
        raise ValidationFailed({"secrets": "Choose to seal the secrets or to leave them out."})
    if choice != SEALED:
        return None
    if passphrase != confirmation:
        raise ValidationFailed({"passphrase_confirm": "The two passphrases do not match."})
    check_passphrase(passphrase)
    return passphrase


async def _chunks(stream: StackExportStream) -> AsyncGenerator[bytes, None]:
    async for chunk in stream:
        yield chunk


class _ExportResponse(StreamingResponse):
    """An archive download that closes the export and its session on every way out.

    A client that leaves before the first chunk is requested never starts the generator, so
    the stream is closed here as well.
    """

    def __init__(self, stream: StackExportStream, stack: AsyncExitStack, filename: str) -> None:
        self._stream = stream
        self._stack = stack
        self._content = _chunks(stream)
        super().__init__(
            self._content,
            media_type=stream.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._content.aclose()
                await self._stream.aclose()
                await self._stack.aclose()


async def _run(
    request: Request,
    actor: Principal,
    kind: StackKind,
    environment_id: int,
    name: str,
    *,
    expected_digest: str,
    choice: str,
    passphrase: str,
    confirmation: str,
    volumes: list[str],
) -> Response:
    detail_href = _detail_href(request, kind, environment_id, name)
    review_href = _export_href(request, kind, environment_id, name, confirm=True)
    stack = AsyncExitStack()
    try:
        sealed_under = _passphrase(choice, passphrase, confirmation)
        service = await stack.enter_async_context(open_stack_export_service(request.app))
        stream = await service.export(
            actor, environment_id, kind, name, expected_digest, sealed_under, volumes
        )
    except StackExportBusy as busy:
        await stack.aclose()
        refused = problem_page(
            request,
            heading=HEADING,
            failure=Failure(429, "The export was not started", str(busy)),
            back_href=detail_href,
            back_label="Back to the stack",
            region=None,
        )
        refused.headers["Retry-After"] = RETRY_SECONDS
        return refused
    except REFUSALS as error:
        await stack.aclose()
        return problem_page(
            request,
            heading=HEADING,
            failure=_failure_of(request, error, actor),
            back_href=detail_href,
            back_label="Back to the stack",
            review_href=review_href,
            region=None,
        )
    except BaseException:
        await stack.aclose()
        raise
    stamp = request.app.state.auth.clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return _ExportResponse(stream, stack, f"stack-{name}-{stamp}{stream.suffix}")


@router.get(
    "/{stack_name}/export", response_class=HTMLResponse, name="environment_stack_export_confirm"
)
async def export_confirm(
    stack_name: StackName,
    request: Request,
    exports: StackExports,
    actor: Administrator,
    environment_id: PathId,
) -> Response:
    """Name everything a Compose stack's archive holds and leaves out; administrators only."""
    return await _confirm(request, exports, actor, "compose", environment_id, stack_name)


@router.post(
    "/{stack_name}/export", response_class=StreamingResponse, name="environment_stack_export"
)
async def run_export(
    stack_name: StackName,
    request: Request,
    actor: Administrator,
    environment_id: PathId,
    expected_digest: Field = "",
    secrets: Field = "",
    passphrase: Secret = "",
    passphrase_confirm: Secret = "",
    volume: ChosenVolumes = [],  # noqa: B006
) -> Response:
    """Stream the reviewed Compose stack as a gzip archive; administrators only."""
    return await _run(
        request,
        actor,
        "compose",
        environment_id,
        stack_name,
        expected_digest=expected_digest,
        choice=secrets,
        passphrase=passphrase,
        confirmation=passphrase_confirm,
        volumes=volume,
    )


@swarm.get(
    "/stacks/{namespace}/export",
    response_class=HTMLResponse,
    name="environment_swarm_stack_export_confirm",
)
async def swarm_export_confirm(
    namespace: Namespace,
    request: Request,
    exports: StackExports,
    actor: Administrator,
    environment_id: PathId,
) -> Response:
    """Name everything a Swarm stack's archive holds and leaves out; administrators only."""
    return await _confirm(request, exports, actor, "swarm", environment_id, namespace)


@swarm.post(
    "/stacks/{namespace}/export",
    response_class=StreamingResponse,
    name="environment_swarm_stack_export",
)
async def run_swarm_export(
    namespace: Namespace,
    request: Request,
    actor: Administrator,
    environment_id: PathId,
    expected_digest: Field = "",
    secrets: Field = "",
    passphrase: Secret = "",
    passphrase_confirm: Secret = "",
    volume: ChosenVolumes = [],  # noqa: B006
) -> Response:
    """Stream the reviewed Swarm stack as a gzip archive; administrators only."""
    return await _run(
        request,
        actor,
        "swarm",
        environment_id,
        namespace,
        expected_digest=expected_digest,
        choice=secrets,
        passphrase=passphrase,
        confirmation=passphrase_confirm,
        volumes=volume,
    )
