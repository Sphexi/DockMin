"""Streamed Swarm routes: the service log stream and its capped download."""

from collections.abc import AsyncGenerator
from typing import Final

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import ValidationFailed
from app.services.assembly import app_swarm_services
from app.services.container_logs import LogDownload, LogQuery
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import TooManyStreams
from app.services.service_logs import SERVICE_DEFAULT_TAIL, SERVICE_MAX_TAIL, MergedDownload
from app.services.swarm import SwarmListTooLarge
from app.web.container_stream_routes import (
    DOWNLOAD_HEADERS,
    _invalid_query,
    _log_frames,
    _LogResponse,
    _refusal,
)
from app.web.identifiers import PathId
from app.web.swarm_routes import ServiceId

router = APIRouter(
    prefix="/environments/{environment_id}/swarm",
    tags=["swarm"],
    dependencies=[Depends(require_user)],
)

type Download = LogDownload | MergedDownload

UNAVAILABLE: Final = (EnvironmentUnavailable, ObjectTooLarge, SwarmListTooLarge)


async def _download_chunks(download: Download) -> AsyncGenerator[bytes, None]:
    try:
        async for chunk in download:
            yield chunk
    finally:
        with anyio.CancelScope(shield=True):
            await download.aclose()


class _DownloadResponse(StreamingResponse):
    """A download response that gives its view slot back on every way out."""

    def __init__(self, download: Download, content: AsyncGenerator[bytes, None]) -> None:
        super().__init__(
            content,
            media_type=download.media_type,
            headers={
                **DOWNLOAD_HEADERS,
                "Content-Disposition": f'attachment; filename="{download.filename}"',
            },
        )
        self._download = download
        self._content = content

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._content.aclose()
                await self._download.aclose()


@router.get(
    "/services/{service_id}/logs/stream",
    response_class=StreamingResponse,
    name="environment_swarm_service_logs_stream",
)
async def logs_stream(
    service_id: ServiceId, request: Request, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Stream a service's logs across its tasks as server-sent events; read access.

    Each line is `<timestamp> <task label> <text>`. The environment gate and the service's
    read access are checked by the service layer before the query is read and before any log
    request is made, on a session that is closed before the first line is sent.
    """
    last_event_id = request.headers.get("last-event-id")
    after = request.query_params.get("after")
    try:
        async with app_swarm_services(request.app) as swarm:
            await swarm.logs.authorise(actor, environment_id, service_id)
            try:
                query = LogQuery.from_params(
                    request.query_params,
                    default_tail=SERVICE_DEFAULT_TAIL,
                    max_tail=SERVICE_MAX_TAIL,
                )
            except ValidationFailed as invalid:
                return _invalid_query(invalid)
            stream = await swarm.logs.open(
                actor, environment_id, service_id, query, last_event_id, after
            )
    except TooManyStreams as refused:
        return _refusal(request, "Too many live views are open", str(refused), 429)
    except UNAVAILABLE as unavailable:
        return _refusal(request, "Logs are not available", str(unavailable), 503)
    try:
        return _LogResponse(stream, _log_frames(stream))
    except BaseException:
        await stream.aclose()
        raise


@router.get(
    "/services/{service_id}/logs/download",
    response_class=StreamingResponse,
    name="environment_swarm_service_logs_download",
)
async def logs_download(
    service_id: ServiceId, request: Request, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Download a capped rendering of a service's logs; read access.

    The `format` query value is `text` (the default) or `ndjson`.
    """
    try:
        async with app_swarm_services(request.app) as swarm:
            await swarm.logs.authorise(actor, environment_id, service_id)
            try:
                query = LogQuery.from_params(
                    request.query_params,
                    default_tail=SERVICE_DEFAULT_TAIL,
                    max_tail=SERVICE_MAX_TAIL,
                    allow_all=True,
                )
            except ValidationFailed as invalid:
                return _invalid_query(invalid)
            download = await swarm.logs.download(actor, environment_id, service_id, query)
    except TooManyStreams as refused:
        return _refusal(request, "Too many live views are open", str(refused), 429)
    except UNAVAILABLE as unavailable:
        return _refusal(request, "Logs are not available", str(unavailable), 503)
    try:
        return _DownloadResponse(download, _download_chunks(download))
    except BaseException:
        await download.aclose()
        raise
