"""A fake Docker daemon on a real socket: the hijack upgrade for exec/attach and the log stream.

Everything else (ping, version, any route scripted on `engine`) is answered by the embedded
FakeEngine. A hijacked session behaves like a PTY: input is echoed, `burst N` emits N bytes,
`exit N` ends the process with that code and closes the stream.
"""

import asyncio
import contextlib
import json
import re
import ssl
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from app.docker_engine.client import EngineClient
from app.docker_engine.transport import ConnectionSettings, UnixSocketSettings
from tests.docker_engine.container_engine import Box
from tests.docker_engine.fake_engine import (
    FakeEngine,
    RecordedRequest,
    Reply,
    error_reply,
    json_reply,
    split_version,
)
from tests.docker_engine.helpers import ENV_ID, HANG_GUARD_SECONDS

RAW_STREAM = "application/vnd.docker.raw-stream"
MULTIPLEXED_STREAM = "application/vnd.docker.multiplexed-stream"
BURST_FILL = b"x"
BURST_CHUNK_BYTES = 16 * 1024
DAEMON_INFO = {
    "ID": "terminal-engine",
    "DefaultRuntime": "runc",
    "LoggingDriver": "json-file",
    "CgroupVersion": "2",
}


class Call(StrEnum):
    """The calls this daemon handles itself, each of which can be refused or dropped."""

    CREATE_EXEC = "create_exec"
    START_EXEC = "start_exec"
    ATTACH = "attach"
    RESIZE_EXEC = "resize_exec"
    RESIZE_CONTAINER = "resize_container"
    INSPECT_EXEC = "inspect_exec"
    LOGS = "logs"


_ROUTES = (
    ("POST", re.compile(r"/containers/([^/]+)/exec"), Call.CREATE_EXEC),
    ("POST", re.compile(r"/exec/([^/]+)/start"), Call.START_EXEC),
    ("POST", re.compile(r"/exec/([^/]+)/resize"), Call.RESIZE_EXEC),
    ("GET", re.compile(r"/exec/([^/]+)/json"), Call.INSPECT_EXEC),
    ("POST", re.compile(r"/containers/([^/]+)/attach"), Call.ATTACH),
    ("POST", re.compile(r"/containers/([^/]+)/resize"), Call.RESIZE_CONTAINER),
    ("GET", re.compile(r"/containers/([^/]+)/logs"), Call.LOGS),
)


@dataclass(slots=True)
class ExecInstance:
    """An exec instance the daemon created."""

    id: str
    container_id: str
    body: dict[str, Any]
    running: bool = False
    exit_code: int | None = None


@dataclass(slots=True)
class Gate:
    """A call held inside the daemon: `entered` is set on arrival, the call resumes at `release`."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True, slots=True)
class Resize:
    """One terminal resize the daemon received."""

    kind: Literal["exec", "container"]
    target: str
    rows: int
    cols: int


@dataclass(slots=True, eq=False)
class Session:
    """One hijacked connection; `received` holds every byte the client sent."""

    kind: Literal["exec", "attach"]
    target: str
    request: RecordedRequest
    writer: asyncio.StreamWriter
    received: bytearray = field(default_factory=bytearray)
    written: int = 0
    exit_code: int | None = None
    ended: asyncio.Event = field(default_factory=asyncio.Event)

    async def send(self, data: bytes) -> None:
        """Write output to the client, waiting for the transport to accept it."""
        self.writer.write(data)
        self.written += len(data)
        await self.writer.drain()


@dataclass(slots=True, eq=False)
class LogStream:
    """One log request; `push` and `end` feed it live, and the events say who closed it."""

    request: RecordedRequest
    queue: asyncio.Queue[bytes | BaseException | None] = field(default_factory=asyncio.Queue)
    client_closed: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def push(self, chunk: bytes) -> None:
        """Deliver more bytes to the client."""
        self.queue.put_nowait(chunk)

    def end(self) -> None:
        """Finish the stream the way a stopped container does."""
        self.queue.put_nowait(None)

    def fail(self) -> None:
        """Drop the connection part-way through the response, after what was pushed."""
        self.queue.put_nowait(ConnectionAbortedError())


def _head(status: int, headers: dict[str, str]) -> bytes:
    lines = [f"HTTP/1.1 {status} {HTTPStatus(status).phrase}"]
    lines += [f"{name}: {value}" for name, value in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _chunk(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n"


async def _read_request(reader: asyncio.StreamReader) -> RecordedRequest:
    head = await reader.readuntil(b"\r\n\r\n")
    request_line, *header_lines = head.decode("latin-1").split("\r\n")
    method, target, _ = request_line.split(" ")
    headers = {
        name.strip().lower(): value.strip()
        for name, _, value in (line.partition(":") for line in header_lines if line)
    }
    length = int(headers.get("content-length", "0"))
    body = await reader.readexactly(length) if length else b""
    split = urlsplit(target)
    version, path = split_version(split.path)
    return RecordedRequest(method, path, version, dict(parse_qsl(split.query)), headers, None, body)


def _route(request: RecordedRequest) -> tuple[Call, str] | None:
    for method, pattern, call in _ROUTES:
        match = pattern.fullmatch(request.path)
        if request.method == method and match is not None:
            return call, match.group(1)
    return None


class TerminalEngine:
    """The daemon's state and scripting surface; build one with `serve_terminal_engine`."""

    def __init__(self) -> None:
        self.engine = FakeEngine()
        self.execs: dict[str, ExecInstance] = {}
        self.sessions: list[Session] = []
        self.resizes: list[Resize] = []
        self.log_streams: list[LogStream] = []
        self.hijack_content_type = RAW_STREAM
        self.malformed_exec_id: str | None = None
        self.inspect_padding_bytes = 0
        self.unix_path: str | None = None
        self.port: int | None = None
        self._log_script: tuple[tuple[bytes, ...], bool, str, bool] = (
            (),
            False,
            MULTIPLEXED_STREAM,
            False,
        )
        self._refusals: dict[Call, int | None] = {}
        self._gates: dict[Call, Gate] = {}
        self._live: dict[int, asyncio.StreamWriter] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._clients: list[EngineClient] = []
        self._server: asyncio.Server | None = None
        self._idle = asyncio.Event()
        self._idle.set()
        self.engine.route("GET", "/info", json_reply(DAEMON_INFO))

    @property
    def requests(self) -> list[RecordedRequest]:
        """Every request seen, in order."""
        return self.engine.requests

    @property
    def open_connections(self) -> int:
        """Return how many client connections the daemon still holds open."""
        return len(self._live)

    def calls(self, method: str | None = None, path: str | None = None) -> list[RecordedRequest]:
        """Return the recorded requests, optionally filtered by method and version-less path."""
        return self.engine.calls(method, path)

    def refuse(self, call: Call, status: int = 409) -> None:
        """Answer the call with an Engine-style error whose body carries BODY_TOKEN."""
        self._refusals[call] = status

    def clear_faults(self) -> None:
        """Forget every refusal and dropped call."""
        self._refusals.clear()

    def hold(self, call: Call) -> Gate:
        """Make every request for the call wait inside the daemon until the gate is released."""
        gate = Gate()
        self._gates[call] = gate
        return gate

    def drop(self, call: Call) -> None:
        """Close the connection on the call without answering."""
        self._refusals[call] = None

    def script_logs(
        self,
        *chunks: bytes,
        hold: bool = False,
        content_type: str = MULTIPLEXED_STREAM,
        fail: bool = False,
    ) -> None:
        """Set what each log request receives.

        With `hold` the stream stays open for `push`; with `fail` the connection is dropped
        after the chunks instead of ending cleanly.
        """
        self._log_script = (chunks, hold, content_type, fail)

    def script_container(self, container_id: str, name: str, **fields: Any) -> Box:
        """Make container inspect answer for the id with a box built from the fields."""
        box = Box(container_id, name, **fields)
        self.engine.route("GET", f"/containers/{container_id}/json", json_reply(box.inspect()))
        return box

    def hang_up(self) -> None:
        """Close every hijacked connection from the daemon side, leaving exec state unchanged."""
        for session in self.sessions:
            session.writer.close()

    def settings(self) -> ConnectionSettings:
        """Return settings for the Unix socket this daemon listens on."""
        assert self.unix_path is not None
        return UnixSocketSettings(self.unix_path)

    def client(
        self, settings: ConnectionSettings | None = None, environment_id: int = ENV_ID
    ) -> EngineClient:
        """Build a client that is closed when the daemon shuts down."""
        chosen = settings or self.settings()
        client = EngineClient(environment_id, chosen.create_transport(), chosen)
        self._clients.append(client)
        return client

    async def stop_listening(self) -> None:
        """Stop accepting connections, so the next attempt to connect is refused."""
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def wait_for_no_connections(self) -> None:
        """Wait until every connection has closed, failing the test on a hang."""
        async with asyncio.timeout(HANG_GUARD_SECONDS):
            await self._idle.wait()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one connection; passed to the socket server."""
        key = id(writer)
        self._live[key] = writer
        self._idle.clear()
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            request = await _read_request(reader)
            self.engine.requests.append(request)
            await self._dispatch(request, reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            self._live.pop(key, None)
            if not self._live:
                self._idle.set()
            if task is not None:
                self._tasks.discard(task)
            writer.close()
            with contextlib.suppress(OSError, ssl.SSLError):
                await writer.wait_closed()

    async def shutdown(self, server: asyncio.Server) -> None:
        """Close the clients built here, then every connection, then the listener."""
        for gate in self._gates.values():
            gate.release.set()
        for client in self._clients:
            await client.aclose()
        server.close()
        for writer in list(self._live.values()):
            writer.close()
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=HANG_GUARD_SECONDS)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=HANG_GUARD_SECONDS)
        await server.wait_closed()

    async def _dispatch(
        self, request: RecordedRequest, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        route = _route(request)
        if route is None:
            await self._plain(writer, self.engine.take(request.method, request.path))
            return
        call, target = route
        gate = self._gates.get(call)
        if gate is not None:
            gate.entered.set()
            await gate.release.wait()
        if call in self._refusals:
            status = self._refusals[call]
            if status is not None:
                await self._plain(writer, error_reply(status))
            return
        match call:
            case Call.CREATE_EXEC:
                await self._create_exec(request, target, writer)
            case Call.START_EXEC:
                await self._start_exec(request, target, reader, writer)
            case Call.ATTACH:
                await self._upgrade("attach", target, None, request, reader, writer)
            case Call.RESIZE_EXEC | Call.RESIZE_CONTAINER:
                await self._resize(call, request, target, writer)
            case Call.INSPECT_EXEC:
                await self._inspect_exec(target, writer)
            case Call.LOGS:
                await self._logs(request, reader, writer)

    @staticmethod
    async def _plain(writer: asyncio.StreamWriter, behaviour: object) -> None:
        assert isinstance(behaviour, Reply), "only buffered replies are served on this path"
        headers = {
            **behaviour.headers,
            "Content-Length": str(len(behaviour.body)),
            "Connection": "close",
        }
        writer.write(_head(behaviour.status, headers) + behaviour.body)
        await writer.drain()

    async def _create_exec(
        self, request: RecordedRequest, container_id: str, writer: asyncio.StreamWriter
    ) -> None:
        exec_id = f"{0xE0000 + len(self.execs):064x}"
        self.execs[exec_id] = ExecInstance(exec_id, container_id, json.loads(request.body or b"{}"))
        reply_id = self.malformed_exec_id or exec_id
        await self._plain(writer, json_reply({"Id": reply_id}, 201))

    async def _start_exec(
        self,
        request: RecordedRequest,
        exec_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        instance = self.execs.get(exec_id)
        if instance is None:
            await self._plain(writer, error_reply(404))
            return
        await self._upgrade("exec", exec_id, instance, request, reader, writer)

    async def _inspect_exec(self, exec_id: str, writer: asyncio.StreamWriter) -> None:
        instance = self.execs.get(exec_id)
        if instance is None:
            await self._plain(writer, error_reply(404))
            return
        document = {
            "ID": exec_id,
            "Running": instance.running,
            "ExitCode": instance.exit_code,
            "Padding": "x" * self.inspect_padding_bytes,
        }
        await self._plain(writer, json_reply(document))

    async def _resize(
        self, call: Call, request: RecordedRequest, target: str, writer: asyncio.StreamWriter
    ) -> None:
        kind: Literal["exec", "container"] = "exec" if call is Call.RESIZE_EXEC else "container"
        if kind == "exec" and target not in self.execs:
            await self._plain(writer, error_reply(404))
            return
        size = Resize(kind, target, int(request.query["h"]), int(request.query["w"]))
        self.resizes.append(size)
        await self._plain(writer, Reply(200))

    async def _upgrade(
        self,
        kind: Literal["exec", "attach"],
        target: str,
        instance: ExecInstance | None,
        request: RecordedRequest,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        headers = {
            "Content-Type": self.hijack_content_type,
            "Connection": "Upgrade",
            "Upgrade": "tcp",
        }
        writer.write(_head(101, headers))
        await writer.drain()
        session = Session(kind, target, request, writer)
        self.sessions.append(session)
        if instance is not None:
            instance.running = True
        try:
            await self._pty(session, instance, reader)
        finally:
            session.ended.set()

    async def _pty(
        self, session: Session, instance: ExecInstance | None, reader: asyncio.StreamReader
    ) -> None:
        line = bytearray()
        while data := await reader.read(65536):
            session.received += data
            await session.send(data)
            for byte in data:
                if byte not in (10, 13):
                    line.append(byte)
                    continue
                verb, _, argument = line.decode("utf-8", "replace").partition(" ")
                line.clear()
                if verb == "burst" and argument.isdigit():
                    await self._burst(session, int(argument))
                elif verb == "exit" and argument.isdigit():
                    session.exit_code = int(argument)
                    if instance is not None:
                        instance.running = False
                        instance.exit_code = session.exit_code
                    return

    @staticmethod
    async def _burst(session: Session, count: int) -> None:
        remaining = count
        while remaining:
            size = min(BURST_CHUNK_BYTES, remaining)
            await session.send(BURST_FILL * size)
            remaining -= size

    async def _logs(
        self, request: RecordedRequest, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        chunks, hold, content_type, fail = self._log_script
        stream = LogStream(request)
        for chunk in chunks:
            stream.push(chunk)
        if fail:
            stream.fail()
        elif not hold:
            stream.end()
        self.log_streams.append(stream)
        headers = {
            "Content-Type": content_type,
            "Transfer-Encoding": "chunked",
            "Connection": "close",
        }
        writer.write(_head(200, headers))
        await writer.drain()
        client_gone = asyncio.create_task(reader.read())
        try:
            while True:
                pending = asyncio.ensure_future(stream.queue.get())
                finished, _ = await asyncio.wait(
                    {pending, client_gone}, return_when=asyncio.FIRST_COMPLETED
                )
                if client_gone in finished:
                    pending.cancel()
                    stream.client_closed.set()
                    return
                item = pending.result()
                if isinstance(item, BaseException):
                    writer.transport.abort()
                    return
                if item is None:
                    writer.write(b"0\r\n\r\n")
                    await writer.drain()
                    return
                if item:
                    writer.write(_chunk(item))
                    await writer.drain()
        finally:
            client_gone.cancel()
            stream.done.set()


@contextlib.asynccontextmanager
async def serve_terminal_engine() -> AsyncIterator[TerminalEngine]:
    """Serve a TerminalEngine on a Unix socket; the yielded object builds settings and clients."""
    # A short prefix keeps the socket path inside the platform's sun_path limit.
    with tempfile.TemporaryDirectory(prefix="dmk") as directory:
        daemon = TerminalEngine()
        daemon.unix_path = str(Path(directory) / "t.sock")
        server = await asyncio.start_unix_server(daemon.handle, daemon.unix_path)
        daemon._server = server
        try:
            yield daemon
        finally:
            await daemon.shutdown(server)


@contextlib.asynccontextmanager
async def serve_terminal_engine_tls(ssl_context: ssl.SSLContext) -> AsyncIterator[TerminalEngine]:
    """Serve a TerminalEngine over TLS on a loopback port, exposed as `port`."""
    daemon = TerminalEngine()
    server = await asyncio.start_server(daemon.handle, "127.0.0.1", 0, ssl=ssl_context)
    daemon.port = server.sockets[0].getsockname()[1]
    daemon._server = server
    try:
        yield daemon
    finally:
        await daemon.shutdown(server)
