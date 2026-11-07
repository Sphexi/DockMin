"""In-process fake Docker Engine: an ASGI app, its transport and a socket front end."""

import asyncio
import contextlib
import json
import re
import ssl
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from http import HTTPStatus
from typing import Any, NoReturn
from urllib.parse import urlsplit

import httpx

from app.docker_engine.transport import TransportBundle

BODY_TOKEN = "engine-body-token-7c1e"
FAKE_BASE_URL = "http://fake-engine"

_VERSION_PREFIX = re.compile(r"^/v(\d+\.\d+)(/.*)$")
_SWITCHING_PROTOCOLS = 101

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]


class PeerDisconnected(Exception):
    """Raised by the ASGI app to drop a connection part-way through a response."""


@dataclass(frozen=True, slots=True)
class Reply:
    """A complete buffered response."""

    status: int = 200
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Disconnect:
    """Send part of a response, then drop the connection."""


@dataclass(frozen=True, slots=True)
class Stall:
    """Never answer, so the caller's read timeout trips."""


@dataclass(frozen=True, slots=True)
class Refuse:
    """Refuse the connection."""


class Ending(StrEnum):
    """How a Stream finishes once its chunks are delivered."""

    COMPLETE = "complete"
    CUT = "cut"
    HOLD = "hold"


class Stream:
    """A body delivered one chunk per pull; `delivered` counts the chunks handed over so far."""

    def __init__(
        self, chunks: Iterable[bytes], ending: Ending = Ending.COMPLETE, status: int = 200
    ) -> None:
        self.chunks = tuple(chunks)
        self.ending = ending
        self.status = status
        self.delivered = 0


type Behaviour = Reply | Disconnect | Stall | Refuse | Stream


def json_reply(payload: Any, status: int = 200) -> Reply:
    """Build a JSON response."""
    return Reply(status, json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"})


def error_reply(status: int) -> Reply:
    """Build an Engine-style error whose body carries BODY_TOKEN."""
    return json_reply({"message": f"engine detail {BODY_TOKEN}"}, status)


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """One request as the fake saw it, with the version prefix split off the path."""

    method: str
    path: str
    version: str | None
    query: Mapping[str, str]
    headers: Mapping[str, str]
    timeout: Mapping[str, float | None] | None
    body: bytes = b""


def split_version(path: str) -> tuple[str | None, str]:
    """Split a leading `/vMAJOR.MINOR` segment from a request path."""
    match = _VERSION_PREFIX.match(path)
    return (match.group(1), match.group(2)) if match else (None, path)


class FakeEngine:
    """A scriptable Engine; routes are keyed by method and version-less path."""

    def __init__(
        self,
        *,
        api_version: str | None = "1.51",
        min_api_version: str | None = "1.44",
        engine_version: str = "29.2.1",
        swarm: str | None = "inactive",
    ) -> None:
        self.requests: list[RecordedRequest] = []
        self.transports: list[FakeEngineTransport] = []
        self._routes: dict[tuple[str, str], deque[Behaviour]] = {}
        ping_headers = {
            name: value
            for name, value in (("Api-Version", api_version), ("Swarm", swarm))
            if value is not None
        }
        self.route("GET", "/_ping", Reply(body=b"OK", headers=ping_headers))
        document: dict[str, Any] = {"Version": engine_version, "ApiVersion": api_version}
        if min_api_version is not None:
            document["MinAPIVersion"] = min_api_version
        self.route("GET", "/version", json_reply(document))

    def route(self, method: str, path: str, *behaviours: Behaviour) -> None:
        """Script a route; each request takes the next behaviour and the last one repeats."""
        self._routes[(method, path)] = deque(behaviours)

    def calls(self, method: str | None = None, path: str | None = None) -> list[RecordedRequest]:
        """Return the recorded requests, optionally filtered."""
        return [
            r
            for r in self.requests
            if (method is None or r.method == method) and (path is None or r.path == path)
        ]

    def paths(self) -> list[str]:
        """Return `METHOD path` for every recorded request, in order."""
        return [f"{r.method} {r.path}" for r in self.requests]

    def peek(self, method: str, path: str) -> Behaviour:
        """Return the next behaviour for a route without consuming it."""
        queue = self._routes.get((method, path))
        return queue[0] if queue else error_reply(404)

    def take(self, method: str, path: str) -> Behaviour:
        """Return the next behaviour for a route and advance past it."""
        queue = self._routes.get((method, path))
        if not queue:
            return error_reply(404)
        return queue.popleft() if len(queue) > 1 else queue[0]

    def bundle(self) -> TransportBundle:
        """Return a fresh transport bundle over this engine."""
        transport = FakeEngineTransport(self)
        self.transports.append(transport)
        return TransportBundle(transport, FAKE_BASE_URL)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one ASGI request."""
        _, path = split_version(scope["path"])
        match self.take(scope["method"], path):
            case Reply(status=status, body=body, headers=headers):
                await send(
                    {"type": "http.response.start", "status": status, "headers": _pairs(headers)}
                )
                await send({"type": "http.response.body", "body": body})
            case Disconnect():
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"part", "more_body": True})
                raise PeerDisconnected
            case other:
                raise AssertionError(f"{other!r} is only served through the transport")


def _pairs(headers: Mapping[str, str]) -> list[tuple[bytes, bytes]]:
    return [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]


class FakeEngineTransport(httpx.AsyncBaseTransport):
    """Serves a FakeEngine, adding the behaviours ASGITransport cannot express."""

    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine
        self._asgi = httpx.ASGITransport(app=engine, raise_app_exceptions=True)
        self._closing = asyncio.Event()
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Record the request, then serve it from the matching behaviour."""
        engine = self._engine
        version, path = split_version(request.url.path)
        try:
            body = request.content
        except httpx.RequestNotRead:
            body = b""
        engine.requests.append(
            RecordedRequest(
                request.method,
                path,
                version,
                dict(request.url.params),
                dict(request.headers),
                request.extensions.get("timeout"),
                body,
            )
        )
        match engine.peek(request.method, path):
            case Refuse():
                engine.take(request.method, path)
                raise httpx.ConnectError("connection refused", request=request)
            case Stall():
                engine.take(request.method, path)
                self._stall(request)
            case Stream():
                stream = engine.take(request.method, path)
                assert isinstance(stream, Stream)
                return httpx.Response(
                    stream.status, stream=_ChunkStream(stream, self._closing), request=request
                )
            case _:
                return await self._through_asgi(request)

    @staticmethod
    def _stall(request: httpx.Request) -> NoReturn:
        timeout = request.extensions.get("timeout") or {}
        # A stall with no read limit would hang the test forever instead of failing it.
        assert timeout.get("read") is not None, "stalled request had no read timeout"
        raise httpx.ReadTimeout("read timed out", request=request)

    async def _through_asgi(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self._asgi.handle_async_request(request)
        except PeerDisconnected:
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body", request=request
            ) from None

    async def aclose(self) -> None:
        """Close the transport and end every held stream."""
        self.closed = True
        self._closing.set()


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, stream: Stream, closing: asyncio.Event) -> None:
        self._stream = stream
        self._closing = closing

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._stream.chunks:
            self._stream.delivered += 1
            yield chunk
        match self._stream.ending:
            case Ending.CUT:
                raise httpx.RemoteProtocolError("incomplete chunked read")
            case Ending.HOLD:
                await self._closing.wait()
                raise httpx.ReadError("connection closed")

    async def aclose(self) -> None:
        """Release nothing; the stream holds no resources."""


async def _respond(engine: FakeEngine, method: str, target: str) -> bytes:
    split = urlsplit(target)
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    version, path = split_version(split.path)
    engine.requests.append(RecordedRequest(method, path, version, {}, {}, None))
    scope = {
        "type": "http",
        "method": method,
        "path": split.path,
        "query_string": b"",
        "headers": [],
    }
    await engine(scope, receive, send)
    status = messages[0]["status"]
    body = b"".join(m.get("body", b"") for m in messages[1:])
    headers = dict(messages[0]["headers"])
    lines = [f"HTTP/1.1 {status} {HTTPStatus(status).phrase}", "Connection: close"]
    lines += [f"{k.decode()}: {v.decode()}" for k, v in headers.items()]
    if status != _SWITCHING_PROTOCOLS:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


async def _handle(
    engine: FakeEngine, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        method, target, _ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
        writer.write(await _respond(engine, method, target))
        await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError, PeerDisconnected):
        pass
    finally:
        writer.close()
        with contextlib.suppress(OSError, ssl.SSLError):
            await writer.wait_closed()


@contextlib.asynccontextmanager
async def serve_tcp(engine: FakeEngine, ssl_context: ssl.SSLContext) -> AsyncIterator[int]:
    """Serve the engine over TLS on a loopback port and yield the port."""
    server = await asyncio.start_server(partial(_handle, engine), "127.0.0.1", 0, ssl=ssl_context)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@contextlib.asynccontextmanager
async def serve_unix(engine: FakeEngine, path: str) -> AsyncIterator[str]:
    """Serve the engine on a Unix socket and yield its path."""
    server = await asyncio.start_unix_server(partial(_handle, engine), path)
    try:
        yield path
    finally:
        server.close()
        await server.wait_closed()
