"""Stand-ins and drivers for the log and terminal tests: a queue-backed hijack, client and peer."""

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import uvicorn
import websockets
from fastapi import FastAPI
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.auth.principal import Principal
from app.auth.sessions import hash_token, session_cookie_name
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.user import Role, UserRepository
from app.db.repositories.user_session import UserSessionRepository
from app.docker_engine.errors import NotFound
from app.docker_engine.models import ExecInspect
from app.services.assembly import app_container_services
from app.services.log_lines import parse_timestamp_ns
from app.services.object_locks import StreamSlot, StreamSlots
from app.services.terminals import IssuedTicket, PeerClosed, TerminalBridge, TerminalLimits
from tests.access_support import container_id
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_SESSION_TOKEN,
    ADMIN_USERNAME,
    STANDARD_ID,
    STANDARD_SESSION_TOKEN,
    STANDARD_USERNAME,
)
from tests.docker_engine.helpers import STDERR, STDOUT, log_frame
from tests.environment_support import activity_rows

CONTAINER = "a" * 64
PLAIN_CONTAINER = "b" * 64
EXEC_ID = "e" * 64
ENVIRONMENT_ID = 7
USER_ID = 1
SESSION_ID = "0123456789abcdef"
SENTINEL = "SENTINEL-4f2a9c"
WAIT_SECONDS = 5.0
POLL_SECONDS = 0.01
BURST_SECONDS = 30.0
RUN_SECONDS = 10.0
CLIENT_LOGGER = logging.getLogger("tests.websocket_client")
CLIENT_LOGGER.setLevel(logging.CRITICAL)
ACK_STEP = 128 * 1024

INTERACTIVE_CONFIG: dict[str, Any] = {
    "Image": "nginx:1",
    "Tty": True,
    "OpenStdin": True,
    "StdinOnce": False,
    "Labels": {},
    "Env": [],
    "Cmd": ["sh"],
    "Entrypoint": [],
}
PLAIN_CONFIG: dict[str, Any] = {**INTERACTIVE_CONFIG, "Tty": False, "OpenStdin": False}


def stamped(second: int, text: str) -> bytes:
    """One log line carrying the engine's timestamp for the given second of the test minute."""
    return f"2026-10-19T12:00:{second:02d}.000000001Z {text}\n".encode()


def nanos(second: int) -> int:
    """The Unix nanoseconds `stamped` writes for the second."""
    value = parse_timestamp_ns(f"2026-10-19T12:00:{second:02d}.000000001Z ")
    assert value is not None
    return value


def out(second: int, text: str) -> bytes:
    """A multiplexed stdout frame holding one stamped line."""
    return log_frame(STDOUT, stamped(second, text))


def err(second: int, text: str) -> bytes:
    """A multiplexed stderr frame holding one stamped line."""
    return log_frame(STDERR, stamped(second, text))


PRIVILEGED = container_id(0xE1)
BIND_MOUNT = container_id(0xE2)
HOST_PID = container_id(0xE3)
ADDED_CAPABILITY = container_id(0xE4)
DEVICE = container_id(0xE5)
PLAIN_MEMBER = container_id(0xE6)
LOCAL_WRITE = container_id(0xE7)
STOPPED = container_id(0xE8)
STDIN_ONCE = container_id(0xE9)
HOST_CONFIGS: dict[str, dict[str, Any]] = {
    PRIVILEGED: {"Privileged": True},
    BIND_MOUNT: {"Binds": ["/etc:/host:ro"]},
    HOST_PID: {"PidMode": "host"},
    ADDED_CAPABILITY: {"CapAdd": ["SYS_ADMIN"]},
    DEVICE: {
        "Devices": [
            {"PathOnHost": "/dev/sda", "PathInContainer": "/dev/sda", "CgroupPermissions": "rwm"}
        ]
    },
    PLAIN_MEMBER: {},
}


async def until(condition: Callable[[], Awaitable[bool] | bool]) -> None:
    """Wait for a condition that has no event to wait on, failing the test after a bound."""
    async with asyncio.timeout(WAIT_SECONDS):
        while True:
            outcome = condition()
            if (await outcome) if isinstance(outcome, Awaitable) else outcome:
                return
            await asyncio.sleep(POLL_SECONDS)


class FakeHijack:
    """A hijacked connection fed by the test: reads come from a queue, writes are recorded."""

    multiplexed = False
    content_type = "application/vnd.docker.raw-stream"

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[bytes | OSError] = asyncio.Queue()
        self._held = b""
        self.written = bytearray()
        self.closed = False
        self.write_error: OSError | None = None

    def feed(self, data: bytes) -> None:
        """Deliver output to whoever reads."""
        self._inbox.put_nowait(data)

    def finish(self) -> None:
        """End the stream the way the Engine does when the process or container ends."""
        self._inbox.put_nowait(b"")

    def fail(self, error: OSError) -> None:
        """Make the next read raise."""
        self._inbox.put_nowait(error)

    async def read(self, size: int = 65536) -> bytes:
        if not self._held:
            item = await self._inbox.get()
            if isinstance(item, OSError):
                raise item
            self._held = item
            if not item:
                return b""
        chunk, self._held = self._held[:size], self._held[size:]
        return chunk

    async def write(self, data: bytes) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.written += data

    async def close(self) -> None:
        self.closed = True
        self._inbox.put_nowait(b"")


@dataclass(frozen=True, slots=True)
class Resized:
    """One resize call the bridge made."""

    kind: str
    rows: int
    cols: int


class FakeClient:
    """The parts of an Engine client the bridge calls after a session starts."""

    def __init__(self) -> None:
        self.closed = False
        self.resizes: list[Resized] = []
        self.exec_running = False
        self.exec_running_polls = 0
        self.exec_exit_code: int | None = None
        self.container_running = True
        self.container_gone = False
        self.exec_inspections = 0
        self.execs = SimpleNamespace(resize=self._resize_exec, inspect=self._inspect_exec)
        self.containers = SimpleNamespace(resize=self._resize_container, inspect=self._inspect)

    async def _resize_exec(self, exec_id: str, rows: int, cols: int) -> None:
        self.resizes.append(Resized("exec", rows, cols))

    async def _resize_container(self, container_id: str, rows: int, cols: int) -> None:
        self.resizes.append(Resized("container", rows, cols))

    async def _inspect_exec(self, exec_id: str) -> ExecInspect:
        self.exec_inspections += 1
        running = self.exec_running or self.exec_inspections <= self.exec_running_polls
        return ExecInspect.model_validate(
            {"Running": running, "ExitCode": None if running else self.exec_exit_code}
        )

    async def _inspect(self, container_id: str) -> Any:
        if self.container_gone:
            raise NotFound(ENVIRONMENT_ID, "gone", status_code=404)
        return SimpleNamespace(state=SimpleNamespace(running=self.container_running))


class ScriptedPeer:
    """The browser end of a session: messages are queued by the test, output is recorded."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[bytes | str | PeerClosed] = asyncio.Queue()
        self.output = bytearray()
        self.texts: list[dict[str, Any]] = []
        self.closed_with: tuple[int, str] | None = None
        self.gone = False
        self.gone_on_output = False
        self.hang_on_end = False
        self.acknowledge_every = 0
        self._unacknowledged = 0

    def say(self, message: bytes | str | dict[str, Any]) -> None:
        """Queue a browser message; a dict is sent as JSON."""
        self.inbox.put_nowait(json.dumps(message) if isinstance(message, dict) else message)

    def say_all(self, *messages: bytes | str | dict[str, Any]) -> None:
        """Queue several browser messages in order."""
        for message in messages:
            self.say(message)

    def hang_up(self, code: int | None = None) -> None:
        """Queue the browser going away."""
        self.inbox.put_nowait(PeerClosed(code))

    async def receive(self) -> bytes | str:
        item = await self.inbox.get()
        if isinstance(item, PeerClosed):
            raise item
        return item

    async def send_bytes(self, data: bytes) -> None:
        if self.gone or self.gone_on_output:
            raise PeerClosed
        self.output += data
        self._unacknowledged += len(data)
        if self.acknowledge_every and self._unacknowledged >= self.acknowledge_every:
            self.say({"t": "ack", "n": self._unacknowledged})
            self._unacknowledged = 0
        await asyncio.sleep(0)

    async def send_text(self, text: str) -> None:
        if self.gone:
            raise PeerClosed
        document = json.loads(text)
        if self.hang_on_end and document.get("t") == "end":
            await asyncio.Event().wait()
        self.texts.append(document)

    async def close(self, code: int, reason: str = "") -> None:
        self.closed_with = (code, reason)

    @property
    def last_text(self) -> dict[str, Any]:
        """The newest text message the bridge sent."""
        return self.texts[-1]


@dataclass(slots=True)
class Rig:
    """A bridge with everything around it recorded."""

    bridge: TerminalBridge
    hijack: FakeHijack
    client: FakeClient
    peer: ScriptedPeer
    slots: StreamSlots
    ends: list[tuple[str, int | None, bool]]
    durations: list[int]
    rechecks: list[bool]
    verdicts: list[Any]

    async def run(self) -> Any:
        """Run the bridge to its end against the scripted peer, failing the test if it hangs."""
        async with asyncio.timeout(RUN_SECONDS):
            return await self.bridge.run(self.peer)


def build_rig(
    *, limits: TerminalLimits | None = None, attach: bool = False, verdicts: list[Any] | None = None
) -> Rig:
    """Build a bridge over a fake hijack, client and peer, holding one terminal slot."""
    hijack, client, peer, slots = FakeHijack(), FakeClient(), ScriptedPeer(), StreamSlots()
    slot: StreamSlot = slots.acquire(USER_ID, kind="terminal")
    ends: list[tuple[str, int | None, bool]] = []
    durations: list[int] = []
    rechecks: list[bool] = []
    script = list(verdicts or ["permitted"])

    async def record_end(
        reason: str, duration: int, exit_code: int | None, process_running: bool
    ) -> None:
        ends.append((reason, exit_code, process_running))
        durations.append(duration)

    async def recheck(touch: bool) -> Any:
        rechecks.append(touch)
        verdict = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    bridge = TerminalBridge(
        mode="attach" if attach else "exec",
        environment_id=ENVIRONMENT_ID,
        container_id=CONTAINER,
        session_id=SESSION_ID,
        hijack=hijack,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        exec_id=None if attach else EXEC_ID,
        slot=slot,
        holds=AsyncExitStack(),
        recheck=recheck,
        record_end=record_end,
        limits=limits or TerminalLimits(),
        size=(24, 80),
    )
    return Rig(bridge, hijack, client, peer, slots, ends, durations, rechecks, script)


async def settle(turns: int = 50) -> None:
    """Let everything runnable run, so a test can show that something did not happen."""
    for _ in range(turns):
        await asyncio.sleep(0)


@dataclass
class Live:
    """The application served by uvicorn on a loopback port, for WebSocket clients."""

    port: int
    app: FastAPI
    server: uvicorn.Server

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def cookie(self, token: str = ADMIN_SESSION_TOKEN) -> str:
        return f"{session_cookie_name(self.app.state.settings)}={token}"

    def headers(
        self, token: str | None = ADMIN_SESSION_TOKEN, origin: str | None = None
    ) -> dict[str, str]:
        """Handshake headers of a browser on the application's own origin, by default."""
        headers = {"Origin": origin or self.origin}
        return headers if token is None else {**headers, "Cookie": self.cookie(token)}

    def socket_url(self, path: str) -> str:
        return f"ws://127.0.0.1:{self.port}{path}"


UVICORN_LOGGERS = ("uvicorn.error", "uvicorn.access", "uvicorn.asgi", "websockets")


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[Live]:
    """Serve the application with uvicorn on a loopback port, at its most verbose logging.

    The server stops, and the logger levels it set are put back, when the block ends.
    """
    levels = {name: logging.getLogger(name).level for name in UVICORN_LOGGERS}
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        lifespan="off",
        log_config=None,
        log_level="trace",
        access_log=False,
        ws_max_size=65536,
    )
    server = uvicorn.Server(config)
    running = asyncio.create_task(server.serve())
    try:
        await until(lambda: server.started)
        yield Live(server.servers[0].sockets[0].getsockname()[1], app, server)
    finally:
        server.should_exit = True
        await asyncio.wait_for(running, WAIT_SECONDS * 2)
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


ADMIN = Principal(ADMIN_ID, ADMIN_USERNAME, "administrator", 1)
STANDARD = Principal(STANDARD_ID, STANDARD_USERNAME, "standard", 2)
SESSION_LIFETIME = timedelta(hours=8)
LONG_AGO = format_timestamp(datetime(2000, 1, 1, tzinfo=UTC))


@dataclass(frozen=True, slots=True)
class Account:
    """A user with a live browser session, as the services see them."""

    principal: Principal
    token: str

    @property
    def token_hash(self) -> str:
        return hash_token(self.token)


async def add_account(app: FastAPI, username: str, role: Role = "administrator") -> Account:
    """Insert a user and a session for them, leaving the seeded accounts untouched."""
    clock = app.state.auth.clock
    token = secrets.token_urlsafe(32)
    async with app.state.database.session() as db:
        user = await UserRepository(db, clock).create(username, "unused-hash", role)
        session = await UserSessionRepository(db, clock).create(
            user.id,
            hash_token(token),
            lifetime=SESSION_LIFETIME,
            user_agent=None,
            remote_address=None,
        )
    return Account(Principal(user.id, username, role, session.id), token)


ADMIN_ACCOUNT = Account(ADMIN, ADMIN_SESSION_TOKEN)
STANDARD_ACCOUNT = Account(STANDARD, STANDARD_SESSION_TOKEN)


async def sql(app: FastAPI, statement: str, *params: Any) -> None:
    """Run one statement against the application's database."""
    async with app.state.database.session() as db:
        await db.execute(statement, params)


async def rows_of(app: FastAPI, action: str) -> list[dict[str, Any]]:
    """Return the recorded activity rows of one action, oldest first."""
    async with app.state.database.session() as db:
        return [row for row in await activity_rows(db) if row["action"] == action]


async def attempt(call: Awaitable[object]) -> str:
    """Await a call and name what it raised, or `issued` when it did not."""
    try:
        await call
    except Exception as error:
        return type(error).__name__
    return "issued"


@dataclass(frozen=True, slots=True)
class Closed:
    """What a WebSocket client saw up to the close: texts, output byte count and the close code."""

    texts: list[dict[str, Any]]
    received: int
    code: int | None

    @property
    def end(self) -> dict[str, Any]:
        """The last text message, which is the `end` report when the server closed the session."""
        return self.texts[-1]


async def issue(
    live: Live,
    environment_id: int,
    *,
    account: Account = ADMIN_ACCOUNT,
    container: str = CONTAINER,
    mode: str = "exec",
    form: dict[str, str] | None = None,
) -> IssuedTicket:
    """Prepare a terminal as the account would from the terminal page, and return the ticket."""
    async with app_container_services(live.app) as services:
        return await services.terminals.prepare(
            account.principal,
            account.token,
            environment_id,
            container,
            mode,  # type: ignore[arg-type]
            form or {},
        )


def connect(
    live: Live, path: str, *, token: str | None = ADMIN_SESSION_TOKEN, origin: str | None = None
) -> Any:
    """Open a WebSocket as a browser on the application's own origin would, by default."""
    return websockets.connect(
        live.socket_url(path),
        additional_headers=live.headers(token, origin),
        max_size=None,
        open_timeout=WAIT_SECONDS,
        logger=CLIENT_LOGGER,
    )


async def redeem(socket: Any, ticket: IssuedTicket) -> None:
    """Send the ticket and wait for the server to say the session is ready."""
    await socket.send(json.dumps({"t": "ticket", "v": ticket.ticket}))
    assert json.loads(await asyncio.wait_for(socket.recv(), WAIT_SECONDS)) == {"t": "ready"}


async def drain(socket: Any) -> Closed:
    """Read until the server closes the connection."""
    texts: list[dict[str, Any]] = []
    received = 0
    try:
        while True:
            message = await asyncio.wait_for(socket.recv(), WAIT_SECONDS)
            if isinstance(message, str):
                texts.append(json.loads(message))
            else:
                received += len(message)
    except ConnectionClosed as closed:
        return Closed(texts, received, closed.rcvd.code if closed.rcvd else None)


async def read_until(socket: Any, needle: bytes) -> bytes:
    """Read output until it contains the bytes."""
    seen = b""
    async with asyncio.timeout(WAIT_SECONDS):
        while needle not in seen:
            message = await socket.recv()
            if isinstance(message, bytes):
                seen += message
    return seen


async def receive_burst(socket: Any, total: int, *, step: int = ACK_STEP) -> int:
    """Read `total` bytes of output, acknowledging every `step` bytes parsed."""
    received = 0
    unacknowledged = 0
    async with asyncio.timeout(BURST_SECONDS):
        while received < total:
            message = await socket.recv()
            if isinstance(message, bytes):
                received += len(message)
                unacknowledged += len(message)
            if unacknowledged >= step:
                await socket.send(json.dumps({"t": "ack", "n": unacknowledged}))
                unacknowledged = 0
    if unacknowledged:
        await socket.send(json.dumps({"t": "ack", "n": unacknowledged}))
    return received


async def refused_status(live: Live, path: str, headers: dict[str, str]) -> int:
    """Attempt a handshake that must be refused, and return the HTTP status it was refused with."""
    try:
        async with websockets.connect(
            live.socket_url(path), additional_headers=headers, logger=CLIENT_LOGGER
        ):
            pass
    except InvalidStatus as refused:
        return refused.response.status_code
    return 101


async def first_message_close(
    live: Live, path: str, message: str | bytes | None, *, token: str = ADMIN_SESSION_TOKEN
) -> int | None:
    """Connect with a good handshake, send one message (or none) and return the close code."""
    async with connect(live, path, token=token) as socket:
        if message is not None:
            await socket.send(message)
        return (await drain(socket)).code


def socket_path(environment_id: int, container: str = CONTAINER) -> str:
    return f"/environments/{environment_id}/containers/{container}/terminal/ws"


def release_all(slots: list[StreamSlot]) -> None:
    """Give back every slot a test claimed."""
    for slot in slots:
        slot.close()


async def leave(socket: Any, how: str) -> None:
    """Leave the page the way a browser can: a close handshake or a dropped connection."""
    if how == "abort":
        socket.transport.abort()
    else:
        await socket.close()
