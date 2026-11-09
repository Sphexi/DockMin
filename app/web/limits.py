"""Request limits applied in front of routing: body size, requests in flight, and send idle time."""

import asyncio
import time
from collections.abc import Callable, Mapping
from http import HTTPStatus
from typing import Final, Protocol

from fastapi import HTTPException, Request
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.db.repositories.stack_build_context import MAX_CONTEXT_UPLOAD_BYTES
from app.logging import LOG_INTERVAL_SECONDS, get_logger
from app.paths import HEALTH_PATH
from app.services.volume_import_archive import MAX_UPLOAD_BYTES as MAX_VOLUME_UPLOAD_BYTES
from app.web.csrf import STREAMED_UPLOAD_ROUTES, is_context_upload, is_volume_import_upload

MAX_BODY_BYTES: Final = 256 * 1024
CONTEXT_UPLOAD_BODY_BYTES: Final = MAX_CONTEXT_UPLOAD_BYTES + 64 * 1024
BODY_MESSAGE_SECONDS: Final = 15.0
BODY_TOTAL_SECONDS: Final = 30.0
CONTEXT_UPLOAD_TOTAL_SECONDS: Final = 300.0
VOLUME_IMPORT_BODY_BYTES: Final = MAX_VOLUME_UPLOAD_BYTES + 64 * 1024
VOLUME_IMPORT_TOTAL_SECONDS: Final = 3600.0
SEND_IDLE_SECONDS: Final = 60.0
CLOSE_TRY_AGAIN_LATER: Final = 1013
CLOSE_CONNECTION: Final = {"Connection": "close"}

logger = get_logger(__name__)


class ErrorResponder(Protocol):
    """Builds the error response for a path: an HTML page, a fragment or a JSON error."""

    def __call__(
        self, request: Request, status_code: int, headers: Mapping[str, str] | None = None
    ) -> Response: ...


def plain_error(
    request: Request, status_code: int, headers: Mapping[str, str] | None = None
) -> Response:
    """Answer with the status phrase as plain text."""
    return PlainTextResponse(HTTPStatus(status_code).phrase, status_code, headers)


async def _refuse(
    respond: ErrorResponder,
    scope: Scope,
    receive: Receive,
    send: Send,
    status_code: int,
    headers: Mapping[str, str] | None = None,
) -> None:
    try:
        response = respond(Request(scope, receive), status_code, headers)
    except Exception as error:
        logger.error("limit_response_render_failed", extra={"error": type(error).__name__})
        response = plain_error(Request(scope, receive), status_code, headers)
    await response(scope, receive, send)


class BodySizeLimitMiddleware:
    """Refuse a request whose body passes the cap or arrives too slowly.

    A declared length over the cap is answered 413 without reading anything. Otherwise the count
    is taken from the receive channel, which covers chunked bodies that declare no length, and
    the read stops at the first message that passes the cap. A body that stalls between messages
    or takes too long overall is answered 408 and the connection is closed. Once a response has
    started the same conditions end the request as a disconnect. It must sit below every reader of
    the body, so the framework's form parsing is bounded too.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int = MAX_BODY_BYTES,
        message_seconds: float = BODY_MESSAGE_SECONDS,
        total_seconds: float = BODY_TOTAL_SECONDS,
        respond: ErrorResponder = plain_error,
        upload_max_bytes: int = CONTEXT_UPLOAD_BODY_BYTES,
        upload_total_seconds: float = CONTEXT_UPLOAD_TOTAL_SECONDS,
        volume_import_max_bytes: int = VOLUME_IMPORT_BODY_BYTES,
        volume_import_total_seconds: float = VOLUME_IMPORT_TOTAL_SECONDS,
    ) -> None:
        self.app = app
        self._max_bytes = max_bytes
        self._message_seconds = message_seconds
        self._total_seconds = total_seconds
        self._respond = respond
        self._upload_max_bytes = upload_max_bytes
        self._upload_total_seconds = upload_total_seconds
        self._volume_import_max_bytes = volume_import_max_bytes
        self._volume_import_total_seconds = volume_import_total_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on with a bounded receive channel, or refuse it."""
        if scope["type"] != "http" or (scope["method"], scope["path"]) in STREAMED_UPLOAD_ROUTES:
            await self.app(scope, receive, send)
            return
        max_bytes, total_seconds = self._max_bytes, self._total_seconds
        if is_context_upload(scope["method"], scope["path"]):
            max_bytes, total_seconds = self._upload_max_bytes, self._upload_total_seconds
        elif is_volume_import_upload(scope["method"], scope["path"]):
            max_bytes = self._volume_import_max_bytes
            total_seconds = self._volume_import_total_seconds
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            await _refuse(self._respond, scope, receive, send, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        received = 0
        complete = False
        responding = False
        deadline: float | None = None

        def refuse(status_code: HTTPStatus) -> Message:
            if responding:
                return {"type": "http.disconnect"}
            headers = CLOSE_CONNECTION if status_code == HTTPStatus.REQUEST_TIMEOUT else None
            raise HTTPException(status_code=status_code, headers=headers)

        async def limited() -> Message:
            nonlocal received, complete, deadline
            if complete:
                return await receive()
            now = asyncio.get_running_loop().time()
            if deadline is None:
                deadline = now + total_seconds
            try:
                async with asyncio.timeout(min(self._message_seconds, max(0.0, deadline - now))):
                    message = await receive()
            except TimeoutError:
                return refuse(HTTPStatus.REQUEST_TIMEOUT)
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                complete = not message.get("more_body", False)
                if received > max_bytes:
                    return refuse(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            else:
                complete = True
            return message

        async def tracked(message: Message) -> None:
            nonlocal responding
            if message["type"] == "http.response.start":
                responding = True
            await send(message)

        await self.app(scope, limited, tracked)


class ConcurrencyLimitMiddleware:
    """Refuse HTTP requests and WebSocket handshakes beyond a fixed number in flight.

    A request counts until its response, or its socket, is finished, so streams hold a place for
    as long as they are open. The liveness endpoint is neither counted nor refused.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limit: int,
        respond: ErrorResponder = plain_error,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self._limit = limit
        self._respond = respond
        self._clock = clock
        self._active = 0
        self._logged_at: float | None = None

    @property
    def active(self) -> int:
        """Return how many counted requests are in flight."""
        return self._active

    def _note_refusal(self) -> None:
        now = self._clock()
        if self._logged_at is not None and now - self._logged_at < LOG_INTERVAL_SECONDS:
            return
        self._logged_at = now
        logger.warning("concurrency_limit_reached", extra={"limit": self._limit})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Count the request, or refuse it when the limit is reached."""
        kind = scope["type"]
        if kind not in ("http", "websocket") or scope["path"] == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        if self._active >= self._limit:
            self._note_refusal()
            if kind == "websocket":
                await receive()
                await send({"type": "websocket.close", "code": CLOSE_TRY_AGAIN_LATER})
                return
            await _refuse(
                self._respond,
                scope,
                receive,
                send,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"Retry-After": "1"},
            )
            return
        self._active += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self._active -= 1


class SendIdleTimeoutMiddleware:
    """Stop serving a response whose client has not accepted a body chunk within the bound.

    The inner application is cancelled so the response's cleanup runs, and the middleware returns
    without completing the response, which makes the server close the connection.
    """

    def __init__(self, app: ASGIApp, *, seconds: float = SEND_IDLE_SECONDS) -> None:
        self.app = app
        self._seconds = seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the application with a guarded send channel."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        stalled = asyncio.Event()
        sent = 0

        async def guarded(message: Message) -> None:
            nonlocal sent
            if message["type"] != "http.response.body":
                await send(message)
                return
            try:
                async with asyncio.timeout(self._seconds):
                    await send(message)
            except TimeoutError:
                stalled.set()
                await asyncio.Event().wait()
            sent += len(message.get("body", b""))

        inner: asyncio.Task[None] = asyncio.ensure_future(self.app(scope, receive, guarded))
        watcher = asyncio.create_task(stalled.wait())
        try:
            await asyncio.wait({inner, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if not stalled.is_set():
                inner.result()
                return
        finally:
            watcher.cancel()
            inner.cancel()
            for outcome in await asyncio.gather(inner, watcher, return_exceptions=True):
                if isinstance(outcome, Exception):
                    logger.warning(
                        "response_cleanup_failed", extra={"error": type(outcome).__name__}
                    )
        route = scope.get("route")
        logger.warning(
            "response_send_stalled",
            extra={"path": getattr(route, "path", "unmatched"), "bytes_sent": sent},
        )
