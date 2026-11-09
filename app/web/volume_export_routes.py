"""Volume export pages: the confirmation and the streamed archive download."""

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from datetime import UTC
from typing import Annotated, Final

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.auth.dependencies import require_administrator_or_not_found, require_user
from app.auth.errors import SelfProtected, ValidationFailed
from app.auth.principal import Principal
from app.services.containers import ContainerListTooLarge
from app.services.host_objects import VolumeListTooLarge, busy_view
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectBusy
from app.services.volume_export import (
    VolumeExportBusy,
    VolumeExportRefused,
    VolumeExportStream,
)
from app.services.volume_export_assembly import VolumeExports, open_volume_export_service
from app.services.volume_views import VolumeChangedSinceReview, VolumeGone
from app.web.identifiers import PathId
from app.web.object_pages import (
    Failure,
    Field,
    busy_message,
    page_response,
    problem_page,
    route,
)
from app.web.problems import unreachable_copy
from app.web.volume_routes import VolumeName

router = APIRouter(
    prefix="/environments/{environment_id}/volumes",
    tags=["volumes"],
    dependencies=[Depends(require_user)],
)

RETRY_SECONDS: Final = "5"
HEADING: Final = "Export volume"
Administrator = Annotated[Principal, Depends(require_administrator_or_not_found)]

REFUSALS: Final = (
    SelfProtected,
    ObjectBusy,
    ValidationFailed,
    VolumeGone,
    VolumeExportRefused,
    EnvironmentUnavailable,
    ObjectTooLarge,
    VolumeListTooLarge,
    ContainerListTooLarge,
)


def _detail_path(request: Request, environment_id: int, volume_name: str) -> str:
    return route(
        request, "environment_volume_detail", environment_id=environment_id, volume_name=volume_name
    )


def _failure_of(request: Request, error: Exception, actor: Principal) -> Failure:
    if isinstance(error, ObjectBusy):
        busy = busy_view(error, actor, request.app.state.auth.clock())
        return Failure(409, "This volume is busy", busy_message(busy, "it"))
    if isinstance(error, SelfProtected):
        return Failure(409, "Protected", str(error))
    if isinstance(error, VolumeChangedSinceReview):
        return Failure(409, "The volume changed", "; ".join(error.fields.values()))
    if isinstance(error, ValidationFailed):
        return Failure(400, "Check the request", "; ".join(error.fields.values()))
    if isinstance(error, VolumeGone):
        return Failure(404, "The volume no longer exists", str(error))
    if isinstance(error, VolumeExportRefused):
        return Failure(409, "The volume cannot be exported", str(error))
    return Failure(503, *unreachable_copy(error))


@router.get(
    "/{volume_name}/export",
    response_class=HTMLResponse,
    name="environment_volume_export_confirm",
)
async def export_confirm(
    volume_name: VolumeName,
    request: Request,
    exports: VolumeExports,
    actor: Administrator,
    environment_id: PathId,
) -> Response:
    """Name the volume, what is read from it and the helper container; administrators only."""
    detail_href = _detail_path(request, environment_id, volume_name)
    try:
        confirmation = await exports.confirmation(actor, environment_id, volume_name)
    except REFUSALS as error:
        return problem_page(
            request,
            heading=HEADING,
            failure=_failure_of(request, error, actor),
            back_href=detail_href,
            back_label="Back to the volume",
            region=None,
        )
    return page_response(
        request,
        "volumes/export_confirm.html",
        {
            "confirmation": confirmation,
            "detail_href": detail_href,
            "run_href": route(
                request,
                "environment_volume_export",
                environment_id=environment_id,
                volume_name=volume_name,
            ),
        },
    )


async def _chunks(stream: VolumeExportStream) -> AsyncGenerator[bytes, None]:
    async for chunk in stream:
        yield chunk


class _ExportResponse(StreamingResponse):
    """An archive download that closes the export and its session on every way out.

    A client that leaves before the first chunk is requested never starts the generator, so
    the stream is closed here as well.
    """

    def __init__(self, stream: VolumeExportStream, stack: AsyncExitStack, filename: str) -> None:
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


@router.post(
    "/{volume_name}/export",
    response_class=StreamingResponse,
    name="environment_volume_export",
)
async def run_export(
    volume_name: VolumeName,
    request: Request,
    actor: Administrator,
    environment_id: PathId,
    expected_created_at: Field = "",
    helper_image: Field = "",
) -> Response:
    """Stream the reviewed volume as a gzip archive; administrators only."""
    detail_href = _detail_path(request, environment_id, volume_name)
    stack = AsyncExitStack()
    try:
        service = await stack.enter_async_context(open_volume_export_service(request.app))
        stream = await service.start(
            actor,
            environment_id,
            volume_name,
            expected_created_at=expected_created_at,
            helper_image=helper_image,
        )
    except VolumeExportBusy as busy:
        await stack.aclose()
        refused = problem_page(
            request,
            heading=HEADING,
            failure=Failure(429, "The export was not started", str(busy)),
            back_href=detail_href,
            back_label="Back to the volume",
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
            back_label="Back to the volume",
            review_href=route(
                request,
                "environment_volume_export_confirm",
                environment_id=environment_id,
                volume_name=volume_name,
            ),
            region=None,
        )
    except BaseException:
        await stack.aclose()
        raise
    stamp = request.app.state.auth.clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return _ExportResponse(stream, stack, f"volume-{volume_name}-{stamp}{stream.suffix}")
