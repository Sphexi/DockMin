"""Streamed container routes: the log stream and download, and the terminal WebSocket."""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, FastAPI, Path, Request, WebSocket
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from starlette.types import Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import PermissionDenied, ProductError, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.auth.sessions import session_cookie_name
from app.services.access import is_canonical_id
from app.services.assembly import app_container_services
from app.services.container_logs import (
    RETRY_MILLISECONDS,
    LogBatch,
    LogDownload,
    LogQuery,
    LogStream,
)
from app.services.container_stats import KeepAlive
from app.services.object_inspector import ContainerTooLarge, EnvironmentUnavailable
from app.services.object_locks import TooManyStreams
from app.services.terminals import (
    CLOSE_POLICY,
    HANDSHAKE_SECONDS,
    InvalidTicket,
    OpenFailure,
    PeerClosed,
    TerminalBridge,
    open_failure,
    open_guarded,
    read_ticket_message,
)
from app.web.identifiers import PathId
from app.web.navigation import no_store
from app.web.templating import render_partial

PREFIX = "/environments/{environment_id}/containers"
SEGMENT_MAX = 128
SSE_MEDIA_TYPE = "text/event-stream"
STREAM_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
DOWNLOAD_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

router = APIRouter(prefix=PREFIX, tags=["containers"], dependencies=[Depends(require_user)])
socket_router = APIRouter(prefix=PREFIX, tags=["containers"])


def _canonical_container_id(
    container_id: Annotated[str, Path(max_length=SEGMENT_MAX)],
) -> str:
    """Refuse anything but the full lower-case id before any Engine call, for every role."""
    if not is_canonical_id("container", container_id):
        raise ResourceNotFound("not found")
    return container_id


ContainerId = Annotated[str, Depends(_canonical_container_id)]


def _refusal(request: Request, title: str, message: str, status_code: int) -> Response:
    return no_store(
        render_partial(
            request, "error_state", title=title, message=message, status_code=status_code
        )
    )


def _invalid_query(error: ValidationFailed) -> Response:
    return PlainTextResponse(
        "; ".join(error.fields.values()), status_code=400, headers={"Cache-Control": "no-store"}
    )


def _event(batch: LogBatch) -> str:
    identifier = batch.event_id
    marker = "" if identifier is None else f"id: {identifier}\n"
    body = "".join(f"data: {line}\n" for line in batch.data.split("\n"))
    return f"event: {batch.stream}\n{marker}{body}\n"


async def _next_item(stream: LogStream) -> LogBatch | KeepAlive | None:
    try:
        return await anext(stream)
    except StopAsyncIteration:
        return None


async def _log_frames(stream: LogStream) -> AsyncGenerator[str, None]:
    """Render each batch as an event and a quiet interval as a comment line.

    Each wait runs as its own task so that a cancelled response never cancels the stream in the
    middle of releasing the Engine response; the release runs shielded, after that task ends.
    """
    pending: asyncio.Future[LogBatch | KeepAlive | None] | None = None
    try:
        while True:
            pending = asyncio.ensure_future(_next_item(stream))
            item = await asyncio.shield(pending)
            pending = None
            if item is None:
                break
            yield ": keep-alive\n\n" if isinstance(item, KeepAlive) else _event(item)
        match stream.end_reason:
            case "lifetime":
                yield f"retry: {RETRY_MILLISECONDS}\n\n"
            case "finished" | "unavailable" as reason:
                yield f"event: end\ndata: {reason}\n\n"
    finally:
        with anyio.CancelScope(shield=True):
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()


class _LogResponse(StreamingResponse):
    """A stream response that gives the log stream back on every way out.

    The generator's own cleanup never runs if the client leaves before the first chunk is
    requested, so the release is tied to the response instead.
    """

    def __init__(self, stream: LogStream, content: AsyncGenerator[str, None]) -> None:
        super().__init__(content, media_type=SSE_MEDIA_TYPE, headers=STREAM_HEADERS)
        self._stream = stream
        self._content = content

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._content.aclose()
                await self._stream.aclose()


async def _download_chunks(download: LogDownload) -> AsyncGenerator[bytes, None]:
    try:
        async for chunk in download:
            yield chunk
    finally:
        with anyio.CancelScope(shield=True):
            await download.aclose()


class _DownloadResponse(StreamingResponse):
    """A download response that gives the view slot back on every way out."""

    def __init__(self, download: LogDownload, content: AsyncGenerator[bytes, None]) -> None:
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
    "/{container_id}/logs/stream",
    response_class=StreamingResponse,
    name="environment_container_logs_stream",
)
async def logs_stream(
    container_id: ContainerId, request: Request, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Stream a container's logs as server-sent events; read access.

    Events `out` and `err` carry up to 200 lines each, `end` says `finished` or `unavailable`.
    The `Last-Event-ID` header, or an `after` query value of nanoseconds, resumes after that line.
    """
    try:
        query = LogQuery.from_params(request.query_params)
    except ValidationFailed as invalid:
        return _invalid_query(invalid)
    last_event_id = request.headers.get("last-event-id")
    after = request.query_params.get("after")
    try:
        async with app_container_services(request.app) as services:
            stream = await services.logs.open(
                actor, environment_id, container_id, query, last_event_id, after
            )
    except TooManyStreams as refused:
        return _refusal(request, "Too many live views are open", str(refused), 429)
    except (EnvironmentUnavailable, ContainerTooLarge) as unavailable:
        return _refusal(request, "Logs are not available", str(unavailable), 503)
    try:
        return _LogResponse(stream, _log_frames(stream))
    except BaseException:
        await stream.aclose()
        raise


@router.get(
    "/{container_id}/logs/download",
    response_class=StreamingResponse,
    name="environment_container_logs_download",
)
async def logs_download(
    container_id: ContainerId, request: Request, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Download a capped rendering of a container's logs; read access.

    The `format` query value is `text` (the default) or `ndjson`.
    """
    try:
        query = LogQuery.from_params(request.query_params, allow_all=True)
    except ValidationFailed as invalid:
        return _invalid_query(invalid)
    try:
        async with app_container_services(request.app) as services:
            download = await services.logs.download(actor, environment_id, container_id, query)
    except TooManyStreams as refused:
        return _refusal(request, "Too many live views are open", str(refused), 429)
    except (EnvironmentUnavailable, ContainerTooLarge) as unavailable:
        return _refusal(request, "Logs are not available", str(unavailable), 503)
    try:
        return _DownloadResponse(download, _download_chunks(download))
    except BaseException:
        await download.aclose()
        raise


class _SocketPeer:
    """Adapts a Starlette WebSocket to what the terminal bridge needs."""

    def __init__(self, socket: WebSocket) -> None:
        self._socket = socket

    async def receive(self) -> bytes | str:
        try:
            message = await self._socket.receive()
        except WebSocketDisconnect as disconnected:
            raise PeerClosed(disconnected.code) from None
        except RuntimeError:
            raise PeerClosed from None
        if message["type"] == "websocket.disconnect":
            raise PeerClosed(message.get("code"))
        data = message.get("bytes")
        if data is not None:
            return bytes(data)
        return str(message.get("text") or "")

    async def send_bytes(self, data: bytes) -> None:
        try:
            await self._socket.send_bytes(data)
        except WebSocketDisconnect as disconnected:
            raise PeerClosed(disconnected.code) from None
        except (RuntimeError, OSError):
            raise PeerClosed from None

    async def send_text(self, text: str) -> None:
        try:
            await self._socket.send_text(text)
        except WebSocketDisconnect as disconnected:
            raise PeerClosed(disconnected.code) from None
        except (RuntimeError, OSError):
            raise PeerClosed from None

    async def close(self, code: int, reason: str = "") -> None:
        await self._socket.close(code=code, reason=reason)


async def _read_ticket(websocket: WebSocket) -> str | None:
    """Read the ticket from the first message; None, with the socket closed, if it is not one."""
    ticket: str | None = None
    try:
        async with asyncio.timeout(HANDSHAKE_SECONDS):
            message: Message = await websocket.receive()
    except TimeoutError:
        message = {}
    text = message.get("text")
    if isinstance(text, str):
        try:
            ticket = read_ticket_message(text)
        except InvalidTicket:
            ticket = None
    if ticket is None:
        if message.get("type") != "websocket.disconnect":
            await websocket.close(code=CLOSE_POLICY, reason="ticket required")
        return None
    return ticket


async def _refuse_open(websocket: WebSocket, failure: OpenFailure) -> None:
    if failure.reason != "bad_ticket":
        report: dict[str, Any] = {"t": "end", "reason": failure.reason, "exit_code": None}
        if failure.message is not None:
            report["message"] = failure.message
        with suppress(WebSocketDisconnect, RuntimeError, OSError):
            await websocket.send_text(json.dumps(report, separators=(",", ":")))
    with suppress(WebSocketDisconnect, RuntimeError, OSError):
        await websocket.close(code=failure.close_code)


async def _open_bridge(
    app: FastAPI,
    principal: Principal,
    token: str,
    environment_id: int,
    container_id: str,
    ticket: str,
) -> TerminalBridge:
    async with app_container_services(app) as services:
        return await services.terminals.open(principal, token, environment_id, container_id, ticket)


@socket_router.websocket("/{container_id}/terminal/ws", name="environment_container_terminal_ws")
async def terminal_socket(
    websocket: WebSocket,
    environment_id: PathId,
    container_id: Annotated[str, Path(max_length=SEGMENT_MAX)],
) -> None:
    """Bridge a browser to an exec or attach session; write access, session cookie, ticket.

    Every refusal before the upgrade is a plain close, so the handshake fails with 403 whether
    there is no session, the container cannot be seen or is read-only, or the host is down. The
    ticket arrives as the first message. The Origin rule and the setup gate run before this body,
    as application-wide dependencies.
    """
    app = websocket.app
    principal: Principal | None = getattr(websocket.state, "principal", None)
    token = websocket.cookies.get(session_cookie_name(app.state.settings))
    if principal is None or principal.session_id is None or token is None:
        await websocket.close(code=CLOSE_POLICY)
        return
    try:
        async with app_container_services(app) as services:
            await services.terminals.authorise_socket(principal, environment_id, container_id)
    except (ResourceNotFound, PermissionDenied, EnvironmentUnavailable, ContainerTooLarge):
        await websocket.close(code=CLOSE_POLICY)
        return
    await websocket.accept()
    ticket = await _read_ticket(websocket)
    if ticket is None:
        return
    try:
        bridge = await open_guarded(
            _open_bridge(app, principal, token, environment_id, container_id, ticket)
        )
    except ProductError as refused:
        await _refuse_open(websocket, open_failure(refused))
        return
    await bridge.run(_SocketPeer(websocket))
