"""Container export pages: the confirmation and the streamed archive download."""

import re
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from datetime import UTC
from typing import Annotated, Final

import anyio
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.auth.dependencies import require_administrator_or_not_found, require_user
from app.auth.errors import SelfProtected, ValidationFailed
from app.auth.principal import Principal
from app.services.container_export import (
    ContainerChangedSinceReview,
    ContainerExportBusy,
    ContainerExportRefused,
    ContainerExportStream,
)
from app.services.container_export_assembly import ContainerExports, open_container_export_service
from app.services.containers import ContainerListTooLarge
from app.services.host_objects import NetworkListTooLarge, VolumeListTooLarge, busy_view
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectBusy
from app.web.container_routes import ContainerId
from app.web.identifiers import PathId
from app.web.object_pages import (
    Failure,
    Field,
    Short,
    busy_message,
    page_response,
    problem_page,
    route,
)
from app.web.problems import unreachable_copy

router = APIRouter(
    prefix="/environments/{environment_id}/containers",
    tags=["containers"],
    dependencies=[Depends(require_user)],
)

RETRY_SECONDS: Final = "5"
HEADING: Final = "Export container"
SEAL: Final = "seal"
OMIT: Final = "omit"
MAX_CHOICES: Final = 128
PASSPHRASE_MAX: Final = 4096
Administrator = Annotated[Principal, Depends(require_administrator_or_not_found)]
Passphrase = Annotated[str, Form(max_length=PASSPHRASE_MAX)]
Choices = Annotated[list[Short] | None, Form(max_length=MAX_CHOICES)]

REFUSALS: Final = (
    SelfProtected,
    ObjectBusy,
    ValidationFailed,
    ContainerExportRefused,
    EnvironmentUnavailable,
    ObjectTooLarge,
    VolumeListTooLarge,
    ContainerListTooLarge,
    NetworkListTooLarge,
)


def _detail_path(request: Request, environment_id: int, container_id: str) -> str:
    return route(
        request,
        "environment_container_detail",
        environment_id=environment_id,
        container_id=container_id,
    )


def _failure_of(request: Request, error: Exception, actor: Principal) -> Failure:
    if isinstance(error, ObjectBusy):
        busy = busy_view(error, actor, request.app.state.auth.clock())
        return Failure(409, "A volume is busy", busy_message(busy, "it"))
    if isinstance(error, SelfProtected):
        return Failure(409, "Protected", str(error))
    if isinstance(error, ContainerChangedSinceReview):
        return Failure(409, "The container changed", "; ".join(error.fields.values()))
    if isinstance(error, ValidationFailed):
        return Failure(400, "Check the request", "; ".join(error.fields.values()))
    if isinstance(error, ContainerExportRefused):
        return Failure(409, "The container cannot be exported", str(error))
    return Failure(503, *unreachable_copy(error))


@router.get(
    "/{container_id}/export",
    response_class=HTMLResponse,
    name="environment_container_export_confirm",
)
async def export_confirm(
    container_id: ContainerId,
    request: Request,
    exports: ContainerExports,
    actor: Administrator,
    environment_id: PathId,
) -> Response:
    """Name what the archive holds and leaves out, and take the choices; administrators only."""
    detail_href = _detail_path(request, environment_id, container_id)
    try:
        confirmation = await exports.confirmation(actor, environment_id, container_id)
    except REFUSALS as error:
        return problem_page(
            request,
            heading=HEADING,
            failure=_failure_of(request, error, actor),
            back_href=detail_href,
            back_label="Back to the container",
            region=None,
        )
    return page_response(
        request,
        "containers/export_confirm.html",
        {
            "confirmation": confirmation,
            "detail_href": detail_href,
            "run_href": route(
                request,
                "environment_container_export",
                environment_id=environment_id,
                container_id=container_id,
            ),
        },
    )


async def _chunks(stream: ContainerExportStream) -> AsyncGenerator[bytes, None]:
    async for chunk in stream:
        yield chunk


class _ExportResponse(StreamingResponse):
    """An archive download that closes the export and its session on every way out.

    A client that leaves before the first chunk is requested never starts the generator, so
    the stream is closed here as well.
    """

    def __init__(self, stream: ContainerExportStream, stack: AsyncExitStack, filename: str) -> None:
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


def _filename(name: str, stamp: str, suffix: str) -> str:
    return f"container-{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}-{stamp}{suffix}"


@router.post(
    "/{container_id}/export",
    response_class=StreamingResponse,
    name="environment_container_export",
)
async def run_export(
    container_id: ContainerId,
    request: Request,
    actor: Administrator,
    environment_id: PathId,
    expected_digest: Field = "",
    secret_handling: Field = SEAL,
    passphrase: Passphrase = "",
    passphrase_confirm: Passphrase = "",
    secret: Choices = None,
    volume: Choices = None,
) -> Response:
    """Stream the reviewed container as a gzip archive; administrators only."""
    detail_href = _detail_path(request, environment_id, container_id)
    review_href = route(
        request,
        "environment_container_export_confirm",
        environment_id=environment_id,
        container_id=container_id,
    )
    stack = AsyncExitStack()
    try:
        if secret_handling not in (SEAL, OMIT):
            raise ValidationFailed({"secret_handling": "Choose how secret values are handled."})
        if secret_handling == SEAL and passphrase != passphrase_confirm:
            raise ValidationFailed({"passphrase": "The two passphrases do not match."})
        service = await stack.enter_async_context(open_container_export_service(request.app))
        stream = await service.export(
            actor,
            environment_id,
            container_id,
            expected_digest,
            passphrase if secret_handling == SEAL else None,
            secret or [],
            volume or [],
        )
    except ContainerExportBusy as busy:
        await stack.aclose()
        refused = problem_page(
            request,
            heading=HEADING,
            failure=Failure(429, "The export was not started", str(busy)),
            back_href=detail_href,
            back_label="Back to the container",
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
            back_label="Back to the container",
            review_href=review_href,
            region=None,
        )
    except BaseException:
        await stack.aclose()
        raise
    stamp = request.app.state.auth.clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return _ExportResponse(stream, stack, _filename(stream.name, stamp, stream.suffix))
