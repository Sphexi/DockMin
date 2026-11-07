"""Exec and attach terminals: one-time tickets, the audited open and end operations, the bridge."""

import asyncio
import hashlib
import hmac
import json
import posixpath
import re
import secrets
import shlex
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, suppress
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Final, Literal, NoReturn, Protocol

import anyio

from app.auth.errors import (
    SELF_PROTECTED_REASON,
    PermissionDenied,
    ProductError,
    ResourceNotFound,
    SelfProtected,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.auth.sessions import SessionService, hash_token
from app.db.engine import Database, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.db.repositories.environment import Environment
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import (
    Conflict,
    EngineError,
    EnvironmentUnreachable,
    NotFound,
    RequestOutcomeUnknown,
    ResponseTooLarge,
)
from app.docker_engine.models import ContainerInspect, ExecInspect, ImageInspect
from app.docker_engine.resources import (
    DETACH_KEYS,
    MAX_TERMINAL_COLUMNS,
    MAX_TERMINAL_ROWS,
    exec_create_body,
)
from app.docker_engine.transport import HijackedConnection
from app.logging import get_logger
from app.self_identity import SelfIdentitySource, no_identity
from app.services.access import ResourceKey, is_canonical_id
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    AuditState,
    UserActor,
    audited,
    completed,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.container_actions import ContainerGone, EngineRefused, retrieve_exception
from app.services.container_derivation import DaemonDefaults, derive_recreate
from app.services.container_restrictions import find_restricted, option_label, refused_options
from app.services.container_review import container_document, image_document
from app.services.container_spec import USER_PATTERN
from app.services.container_stats import ContainerNotRunning
from app.services.containers import display_name, short_id
from app.services.object_inspector import (
    ContainerTooLarge,
    EngineObjectInspector,
    EnvironmentUnavailable,
    unavailable_from,
)
from app.services.object_locks import (
    LockHolder,
    ObjectBusy,
    ObjectKey,
    ObjectLockRegistry,
    StreamSlot,
    StreamSlots,
    TooManyStreams,
)

logger = get_logger(__name__)

TERMINAL_SOCKET_PATH: Final = "/environments/{environment_id}/containers/{container_id}/terminal/ws"
DEFAULT_COMMAND: Final = "/bin/sh"
QUICK_COMMANDS: Final = ("/bin/sh", "/bin/bash", "/bin/ash")
DEFAULT_ROWS: Final = 24
DEFAULT_COLUMNS: Final = 80
MAX_COMMAND_CHARS: Final = 1024
MAX_ARGUMENTS: Final = 64
MAX_WORKING_DIR_CHARS: Final = 1024
MAX_USER_CHARS: Final = 256
MAX_PROGRAM_CHARS: Final = 256

TICKET_SECONDS: Final = 30.0
TICKETS_PER_USER: Final = 2
HANDSHAKE_SECONDS: Final = 5.0
IDLE_SECONDS: Final = 15 * 60.0
IDLE_WARNING_SECONDS: Final = 14 * 60.0
MAX_SESSION_SECONDS: Final = 4 * 3600.0
RECHECK_SECONDS: Final = 60.0
RECHECK_FAILURES: Final = 3
RESTRICTED_CACHE_SIZE: Final = 512
UNREADABLE_OPTIONS: Final = ("unknown_option",)
INPUT_MAX_BYTES: Final = 64 * 1024
CONTROL_MAX_BYTES: Final = 512
OUTPUT_CHUNK_BYTES: Final = 32 * 1024
WINDOW_BYTES: Final = 512 * 1024
RESIZE_INTERVAL_SECONDS: Final = 0.1
CONCLUDE_SECONDS: Final = 5.0
FAREWELL_SECONDS: Final = 5.0
PROCESS_SETTLE_SECONDS: Final = 1.0
PROCESS_POLL_SECONDS: Final = 0.05
WRITE_FAILURE_GRACE_SECONDS: Final = 5.0
SETTLE_SECONDS: Final = 15.0
MAX_ACK: Final = 2**31

CLOSE_NORMAL: Final = 1000
CLOSE_POLICY: Final = 1008
CLOSE_TOO_LARGE: Final = 1009
CLOSE_ERROR: Final = 1011
CLOSE_RESTART: Final = 1012
CLOSE_ACCESS_ENDED: Final = 4403
CLOSE_IDLE: Final = 4408
CLOSE_DURATION: Final = 4409
CLOSE_NO_SLOT: Final = 4429

_TICKET: Final = re.compile(r"[A-Za-z0-9_-]{43}")

type TerminalMode = Literal["exec", "attach"]
type EndReason = Literal[
    "client_closed",
    "process_exited",
    "detached",
    "container_stopped",
    "idle_timeout",
    "max_duration",
    "access_revoked",
    "access_unverified",
    "session_expired",
    "environment_closed",
    "engine_error",
    "protocol_error",
    "interrupted",
]
type RecheckVerdict = Literal["permitted", "access_revoked", "session_expired"]
type RefusalCode = Literal[
    "self_protected",
    "restricted_container",
    "not_running",
    "object_busy",
    "attach_unavailable",
    "attach_busy",
]

_ATTACH_UNAVAILABLE: Final = (
    "This container was not started with a terminal and open standard input, so there is "
    "nothing to attach to. Open a terminal with exec instead."
)
_ATTACH_STDIN_ONCE: Final = (
    "This container closes its standard input when a session detaches, which usually stops it. "
    "Open a terminal with exec instead."
)
_ATTACH_BUSY: Final = "Another session is already attached to this container."
_BUSY: Final = "Another action on this container is in progress. Try again shortly."
_INTERRUPTED_MESSAGES: Final[Mapping[str, str]] = {
    "interrupted": "The terminal was interrupted before it started.",
    "outcome_unknown": "The Docker host did not answer in time. The terminal may have started.",
}
_CLOSE_REASONS: Final[Mapping[int, str]] = {
    CLOSE_NORMAL: "session ended",
    CLOSE_POLICY: "protocol violation",
    CLOSE_TOO_LARGE: "message too large",
    CLOSE_ERROR: "server error",
    CLOSE_ACCESS_ENDED: "access ended",
    CLOSE_IDLE: "idle timeout",
    CLOSE_DURATION: "duration limit",
    CLOSE_NO_SLOT: "no free terminal",
}


class InvalidTicket(ProductError):
    """A terminal ticket is missing, malformed, expired, used, or bound to something else."""

    def __init__(self) -> None:
        super().__init__("The terminal ticket is not valid.")


class RestrictedContainer(PermissionDenied):
    """A standard user asked for a terminal in a container carrying options only administrators set.

    `labels` holds the phrases naming those options, such as "host bind mounts".
    """

    def __init__(self, labels: tuple[str, ...]) -> None:
        super().__init__(_restricted_message(labels))
        self.labels = labels


class AttachNotAvailable(ProductError):
    """The container is not one to attach to; the message says why and points at exec."""


class AttachBusy(ObjectBusy):
    """Another session is attached to the container; the holder is named only to administrators."""

    def __init__(self, holder: LockHolder) -> None:
        super().__init__(holder)
        self.args = (_ATTACH_BUSY,)


class PeerClosed(Exception):
    """The browser end of the terminal is gone; `code` is the close code it gave, if any."""

    def __init__(self, code: int | None = None) -> None:
        super().__init__(code)
        self.code = code


class TerminalPeer(Protocol):
    """The browser side of a terminal session, as the bridge uses it."""

    async def receive(self) -> bytes | str:
        """Return the next binary or text message; PeerClosed once the peer is gone."""
        ...

    async def send_bytes(self, data: bytes) -> None:
        """Send a binary message; PeerClosed if the peer is gone."""
        ...

    async def send_text(self, text: str) -> None:
        """Send a text message; PeerClosed if the peer is gone."""
        ...

    async def close(self, code: int, reason: str = "") -> None:
        """Close the connection with a WebSocket close code."""
        ...


class TerminalRuntime(Protocol):
    """The process-wide state terminals share, read when a terminal is opened."""

    @property
    def locks(self) -> ObjectLockRegistry:
        """Return the registry of actions holding containers."""
        ...

    @property
    def stream_slots(self) -> StreamSlots:
        """Return the open-stream counters."""
        ...

    @property
    def terminal_tickets(self) -> "TerminalTickets":
        """Return the one-time tickets."""
        ...

    @property
    def attach_locks(self) -> ObjectLockRegistry:
        """Return the registry of containers with a session attached."""
        ...

    @property
    def restricted_containers(self) -> "RestrictedCache":
        """Return the remembered restricted-option results."""
        ...


class TerminalScope(Protocol):
    """Opens a terminal service on a database session that closes with the block."""

    def __call__(self) -> AbstractAsyncContextManager["TerminalService"]:
        """Return the context manager."""
        ...


@dataclass(frozen=True, slots=True)
class TerminalLimits:
    """Timing and size bounds of a terminal session; the defaults are the product's."""

    handshake: float = HANDSHAKE_SECONDS
    idle: float = IDLE_SECONDS
    idle_warning: float = IDLE_WARNING_SECONDS
    max_duration: float = MAX_SESSION_SECONDS
    recheck: float = RECHECK_SECONDS
    recheck_failures: int = RECHECK_FAILURES
    window: int = WINDOW_BYTES
    resize_interval: float = RESIZE_INTERVAL_SECONDS


@dataclass(frozen=True, slots=True)
class TerminalSpec:
    """A validated terminal request; command is empty for an attach."""

    mode: TerminalMode
    command: tuple[str, ...]
    working_dir: str | None
    user: str | None
    rows: int
    cols: int


@dataclass(frozen=True, slots=True)
class IssuedTicket:
    """A ticket for one WebSocket session, and where to present it."""

    ticket: str = field(repr=False)
    websocket_path: str
    mode: TerminalMode
    name: str
    short_id: str
    rows: int
    cols: int


@dataclass(frozen=True, slots=True)
class TerminalRefusal:
    """Why a terminal cannot be opened now; `options` names the restricted options' phrases."""

    code: RefusalCode
    message: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TerminalForm:
    """What the terminal page shows before anything is opened."""

    environment_id: int
    environment_name: str
    is_local: bool
    container_id: str
    short_id: str
    name: str
    state: str
    mode: TerminalMode
    refusal: TerminalRefusal | None
    attach_available: bool
    attach_note: str | None
    offers_user: bool
    default_command: str = DEFAULT_COMMAND
    quick_commands: tuple[str, ...] = QUICK_COMMANDS
    default_rows: int = DEFAULT_ROWS
    default_cols: int = DEFAULT_COLUMNS


@dataclass(frozen=True, slots=True)
class TerminalEnd:
    """How a session ended."""

    reason: EndReason
    exit_code: int | None
    close_code: int
    process_running: bool = False


@dataclass(frozen=True, slots=True)
class OpenFailure:
    """How a failed open is reported: the `end` message's reason, the close code and a sentence.

    `message` is the product's explanation, set only where it helps the person and is safe to show.
    """

    reason: str
    close_code: int
    message: str | None = None


def _restricted_message(labels: tuple[str, ...]) -> str:
    return f"Only administrators may open a terminal in a container that uses {', '.join(labels)}."


def open_failure(error: Exception) -> OpenFailure:
    """Map an error raised while opening a session to the report the browser receives."""
    match error:
        case InvalidTicket():
            return OpenFailure("bad_ticket", CLOSE_POLICY)
        case TooManyStreams():
            return OpenFailure(f"slots_{error.scope}", CLOSE_NO_SLOT, str(error))
        case RestrictedContainer():
            return OpenFailure("restricted_container", CLOSE_ACCESS_ENDED, str(error))
        case SelfProtected():
            return OpenFailure(SELF_PROTECTED_REASON, CLOSE_NORMAL, str(error))
        case ValidationFailed():
            return OpenFailure("invalid", CLOSE_ACCESS_ENDED)
        case ResourceNotFound() | PermissionDenied():
            return OpenFailure("access_denied", CLOSE_ACCESS_ENDED)
        case ContainerNotRunning() | ContainerGone():
            return OpenFailure("not_running", CLOSE_NORMAL, str(error))
        case AttachBusy():
            return OpenFailure("attach_busy", CLOSE_NORMAL, str(error))
        case ObjectBusy():
            return OpenFailure("object_busy", CLOSE_NORMAL, _BUSY)
        case AttachNotAvailable():
            return OpenFailure("attach_unavailable", CLOSE_NORMAL, str(error))
        case EnvironmentUnavailable():
            reason = "outcome_unknown" if error.kind == "outcome_unknown" else "unreachable"
            return OpenFailure(reason, CLOSE_ERROR, str(error))
        case _:
            return OpenFailure("engine_error", CLOSE_ERROR)


class RestrictedCache:
    """Remembers which restricted options a container uses, by environment and container id.

    A container's configuration is fixed once it exists and ids are never reused, so an entry
    never goes stale; the least recently used entries are dropped at the size limit.
    """

    def __init__(self, size: int = RESTRICTED_CACHE_SIZE) -> None:
        self._size = size
        self._held: OrderedDict[tuple[int, str], tuple[str, ...]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._held)

    def recall(self, environment_id: int, container_id: str) -> tuple[str, ...] | None:
        """Return the remembered option ids, None when this container has not been seen."""
        key = (environment_id, container_id)
        found = self._held.get(key)
        if found is not None:
            self._held.move_to_end(key)
        return found

    def remember(self, environment_id: int, container_id: str, options: tuple[str, ...]) -> None:
        """Remember the option ids a container uses."""
        key = (environment_id, container_id)
        self._held[key] = options
        self._held.move_to_end(key)
        while len(self._held) > self._size:
            self._held.popitem(last=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Ticket:
    user_id: int
    session_hash: str = field(repr=False)
    environment_id: int
    container_id: str
    spec: TerminalSpec
    expires_at: float


class TerminalTickets:
    """One-time, short-lived permissions to open a terminal, held in memory by their hash.

    A ticket is bound to the user, the browser session, the container and the validated request,
    so presenting it anywhere else fails. It is consumed by the first presentation, whether or not
    that one succeeds.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        lifetime: float = TICKET_SECONDS,
        per_user: int = TICKETS_PER_USER,
    ) -> None:
        self._clock = clock
        self._lifetime = lifetime
        self._per_user = per_user
        self._held: dict[str, _Ticket] = {}

    def __len__(self) -> int:
        return len(self._held)

    def issue(
        self,
        user_id: int,
        session_hash: str,
        environment_id: int,
        container_id: str,
        spec: TerminalSpec,
    ) -> str:
        """Return a new ticket; a user's oldest outstanding ticket is dropped at the limit."""
        self._sweep()
        mine = [key for key, held in self._held.items() if held.user_id == user_id]
        for key in mine[: max(0, len(mine) - self._per_user + 1)]:
            del self._held[key]
        value = secrets.token_urlsafe(32)
        self._held[_digest(value)] = _Ticket(
            user_id,
            session_hash,
            environment_id,
            container_id,
            spec,
            self._clock() + self._lifetime,
        )
        return value

    def redeem(
        self,
        value: str,
        user_id: int,
        session_hash: str,
        environment_id: int,
        container_id: str,
    ) -> TerminalSpec:
        """Consume a ticket and return its request; InvalidTicket for anything wrong with it."""
        self._sweep()
        held = self._held.pop(_digest(value), None) if _TICKET.fullmatch(value) else None
        if (
            held is None
            or held.user_id != user_id
            or held.environment_id != environment_id
            or held.container_id != container_id
            or not hmac.compare_digest(held.session_hash, session_hash)
        ):
            raise InvalidTicket
        return held.spec

    def _sweep(self) -> None:
        now = self._clock()
        for key in [key for key, held in self._held.items() if held.expires_at <= now]:
            del self._held[key]


def _over_control_limit(text: str) -> bool:
    return len(text) > CONTROL_MAX_BYTES or len(text.encode("utf-8")) > CONTROL_MAX_BYTES


def read_ticket_message(text: str) -> str:
    """Return the ticket named by the first WebSocket message; InvalidTicket for anything else."""
    document: Any = None
    failed = _over_control_limit(text)
    if not failed:
        try:
            document = json.loads(text)
        except (ValueError, RecursionError):
            failed = True
    ticket = isinstance(document, dict) and document.get("t") == "ticket"
    value = document.get("v") if ticket else None
    if failed or not isinstance(value, str):
        raise InvalidTicket
    return value


def _size(form: Mapping[str, str]) -> tuple[int, int]:
    def read(name: str, default: int, ceiling: int) -> int:
        text = form.get(name, "").strip()
        if not (text.isascii() and text.isdigit() and len(text) <= len(str(ceiling))):
            return default
        return min(max(int(text), 1), ceiling)

    return read("rows", DEFAULT_ROWS, MAX_TERMINAL_ROWS), read(
        "cols", DEFAULT_COLUMNS, MAX_TERMINAL_COLUMNS
    )


def _command(text: str, errors: dict[str, str]) -> tuple[str, ...]:
    text = text.strip() or DEFAULT_COMMAND
    if len(text) > MAX_COMMAND_CHARS:
        errors["command"] = f"The command is at most {MAX_COMMAND_CHARS} characters."
        return ()
    if "\x00" in text:
        errors["command"] = "The command cannot contain null characters."
        return ()
    parts: list[str] = []
    unterminated = False
    try:
        parts = shlex.split(text)
    except ValueError:
        unterminated = True
    if unterminated:
        errors["command"] = "The command has an unclosed quotation mark or a trailing backslash."
    elif not parts or not parts[0]:
        errors["command"] = "Enter the program to run."
    elif len(parts) > MAX_ARGUMENTS:
        errors["command"] = f"The command has at most {MAX_ARGUMENTS} words."
    else:
        return tuple(parts)
    return ()


def _working_dir(text: str, errors: dict[str, str]) -> str | None:
    text = text.strip()
    if not text:
        return None
    if len(text) > MAX_WORKING_DIR_CHARS or "\x00" in text or not text.startswith("/"):
        errors["working_dir"] = (
            f"Enter an absolute path of at most {MAX_WORKING_DIR_CHARS} characters."
        )
        return None
    return "/" + posixpath.normpath(text).lstrip("/")


def _user(text: str, is_administrator: bool, errors: dict[str, str]) -> str | None:
    text = text.strip()
    if not text:
        return None
    if not is_administrator:
        errors["user"] = "Only administrators may choose the user."
    elif len(text) > MAX_USER_CHARS or USER_PATTERN.fullmatch(text) is None:
        errors["user"] = "Use a name or id, optionally followed by a colon and a group."
    else:
        return text
    return None


def build_spec(
    form: Mapping[str, str], *, mode: TerminalMode, is_administrator: bool
) -> TerminalSpec:
    """Validate a submitted terminal form; ValidationFailed, keyed by field, for each bad value.

    The form keys are `command`, `working_dir`, `user`, `rows` and `cols`. An attach takes only
    the size. A blank command is `/bin/sh`; the command is split with POSIX shell quoting rules
    and never run through a shell. Sizes that are missing or malformed fall back to 24 by 80 and
    are clamped to what the Engine accepts.
    """
    rows, cols = _size(form)
    if mode == "attach":
        return TerminalSpec("attach", (), None, None, rows, cols)
    errors: dict[str, str] = {}
    command = _command(form.get("command", ""), errors)
    working_dir = _working_dir(form.get("working_dir", ""), errors)
    user = _user(form.get("user", ""), is_administrator, errors)
    if errors:
        raise ValidationFailed(errors)
    return TerminalSpec("exec", command, working_dir, user, rows, cols)


def _target_id(arguments: Arguments) -> str:
    return str(arguments["container_id"])[:64]


def _started_action(arguments: Arguments) -> str:
    return "container_exec_started" if arguments["mode"] == "exec" else "container_attach_started"


def _ended_action(arguments: Arguments) -> str:
    return "container_exec_ended" if arguments["mode"] == "exec" else "container_attach_ended"


@dataclass(frozen=True, slots=True)
class _Evaluation:
    client: EngineClient
    environment: Environment
    inspected: ContainerInspect
    name: str
    refusals: Mapping[TerminalMode, TerminalRefusal | None]
    locked: LockHolder | None
    attached: LockHolder | None


@dataclass(slots=True)
class _Progress:
    create: asyncio.Future[str] | None = None
    start: asyncio.Future[HijackedConnection] | None = None


class _ControlError(Exception):
    """A control message was malformed; carries nothing of the message."""


class _ControlTooLarge(_ControlError):
    """A control message was over the size limit."""


@dataclass(frozen=True, slots=True)
class _Resize:
    rows: int
    cols: int


@dataclass(frozen=True, slots=True)
class _Ack:
    count: int


@dataclass(frozen=True, slots=True)
class _Activity:
    pass


def _parse_control(text: str) -> _Resize | _Ack | _Activity:
    """Parse one JSON control message; _ControlError, with the input dropped, for anything else.

    _ControlTooLarge when it is over the size limit in bytes.
    """
    if _over_control_limit(text):
        raise _ControlTooLarge
    document: Any = None
    failed = False
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        failed = True
    if not failed and isinstance(document, dict):
        kind = document.get("t")
        if kind == "activity":
            return _Activity()
        if kind == "resize":
            rows, cols = document.get("rows"), document.get("cols")
            if (
                type(rows) is int
                and type(cols) is int
                and 1 <= rows <= MAX_TERMINAL_ROWS
                and 1 <= cols <= MAX_TERMINAL_COLUMNS
            ):
                return _Resize(rows, cols)
        elif kind == "ack":
            count = document.get("n")
            if type(count) is int and 1 <= count <= MAX_ACK:
                return _Ack(count)
    raise _ControlError


def _json(**fields: Any) -> str:
    return json.dumps(fields, separators=(",", ":"))


def _peer_ending(closed: PeerClosed) -> TerminalEnd:
    if closed.code == CLOSE_TOO_LARGE:
        return TerminalEnd("protocol_error", None, CLOSE_TOO_LARGE)
    if closed.code == CLOSE_RESTART:
        return TerminalEnd("environment_closed", None, CLOSE_NORMAL)
    return TerminalEnd("client_closed", None, CLOSE_NORMAL)


class TerminalBridge:
    """Carries one terminal session between a browser peer and a hijacked Engine stream.

    Created only by the audited open operations, which have already claimed everything the
    session holds. Await `run` exactly once, or call `abandon` if it will never run: either owns
    the hijack, the slot and the holds, and releases them and records the end of the session on
    every path.

    The bridge never decodes, logs or stores what passes through it. Output reaches the peer in
    binary frames of at most 32 KiB, and the Engine is read only while fewer than `window` bytes
    are unacknowledged by the peer. Only the peer's `activity` messages count against the idle
    timeout, and only they extend the browser session at a re-check.
    """

    def __init__(
        self,
        *,
        mode: TerminalMode,
        environment_id: int,
        container_id: str,
        session_id: str,
        hijack: HijackedConnection,
        client: EngineClient,
        exec_id: str | None,
        slot: StreamSlot,
        holds: AsyncExitStack,
        recheck: Callable[[bool], Awaitable[RecheckVerdict]],
        record_end: Callable[[str, int, int | None, bool], Awaitable[None]],
        limits: TerminalLimits,
        size: tuple[int, int],
    ) -> None:
        self._mode = mode
        self._environment_id = environment_id
        self._container_id = container_id
        self._session_id = session_id
        self._hijack = hijack
        self._client = client
        self._exec_id = exec_id
        self._slot = slot
        self._holds = holds
        self._recheck = recheck
        self._record_end = record_end
        self._limits = limits
        self._size = size
        self._unacked = 0
        self._peak = 0
        self._window_open = asyncio.Event()
        self._resize_wanted = asyncio.Event()
        self._last_activity = 0.0
        self._activity_seen = False
        self._claimed = False
        self._opened_at = time.monotonic()

    @property
    def session_id(self) -> str:
        """Return the random id that pairs the started and ended activity rows."""
        return self._session_id

    @property
    def peak_unacknowledged(self) -> int:
        """Return the most output bytes that were ever sent and not yet acknowledged."""
        return self._peak

    async def run(self, peer: TerminalPeer) -> TerminalEnd:
        """Bridge the session until it ends, then release everything and record the end.

        RuntimeError when the bridge has already run or been abandoned.
        """
        if self._claimed:
            raise RuntimeError("a terminal bridge runs once")
        self._claimed = True
        loop = asyncio.get_running_loop()
        self._last_activity = loop.time()
        ending: TerminalEnd | None = None
        tasks: list[asyncio.Task[TerminalEnd]] = []
        try:
            try:
                await peer.send_text(_json(t="ready"))
            except PeerClosed as closed:
                ending = _peer_ending(closed)
            if ending is None:
                if self._exec_id is None:
                    self._resize_wanted.set()
                tasks = [
                    asyncio.create_task(work)
                    for work in (
                        self._pump_output(peer),
                        self._pump_input(peer),
                        self._pump_resize(),
                        self._watch_idle(peer),
                        self._watch_duration(),
                        self._watch_access(),
                    )
                ]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                ending = self._ending_of(next(task for task in tasks if task in done))
        finally:
            final = await completed(self._finish(peer, tasks, ending))
        return final

    async def abandon(self) -> None:
        """Release a bridge that will never run and record an `interrupted` end.

        Closes the hijack and gives back the slot and the holds. Does nothing once the bridge
        has run or been abandoned.
        """
        if self._claimed:
            return
        self._claimed = True
        await completed(self._close_out(TerminalEnd("interrupted", None, CLOSE_NORMAL), None))

    async def _finish(
        self, peer: TerminalPeer, tasks: list[asyncio.Task[TerminalEnd]], ending: TerminalEnd | None
    ) -> TerminalEnd:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        final = ending or TerminalEnd("environment_closed", None, CLOSE_NORMAL)
        return await self._close_out(final, peer)

    async def _close_out(self, ending: TerminalEnd, peer: TerminalPeer | None) -> TerminalEnd:
        with suppress(Exception):
            async with asyncio.timeout(CONCLUDE_SECONDS):
                await self._hijack.close()
        final = await self._with_process_state(ending)
        if peer is not None:
            with suppress(TimeoutError):
                async with asyncio.timeout(FAREWELL_SECONDS):
                    await self._farewell(peer, final)
        self._slot.close()
        with suppress(Exception):
            await self._holds.aclose()
        duration = int(time.monotonic() - self._opened_at)
        logger.info(
            "terminal_session_ended",
            extra={
                "session": self._session_id,
                "environment_id": self._environment_id,
                "container_id": self._container_id,
                "mode": self._mode,
                "end_reason": final.reason,
                "exit_code": final.exit_code,
                "process_running": final.process_running,
            },
        )
        await self._record_end(final.reason, duration, final.exit_code, final.process_running)
        return final

    @staticmethod
    def _ending_of(task: asyncio.Task[TerminalEnd]) -> TerminalEnd:
        if task.cancelled():
            return TerminalEnd("environment_closed", None, CLOSE_NORMAL)
        failure = task.exception()
        if failure is None:
            return task.result()
        logger.warning("terminal_task_failed", extra={"error": type(failure).__name__})
        return TerminalEnd("engine_error", None, CLOSE_ERROR)

    @staticmethod
    async def _farewell(peer: TerminalPeer, ending: TerminalEnd) -> None:
        if ending.reason == "client_closed":
            return
        report: dict[str, Any] = {"reason": ending.reason, "exit_code": ending.exit_code}
        if ending.process_running:
            report["process_running"] = True
        with suppress(Exception):
            await peer.send_text(_json(t="end", **report))
        with suppress(Exception):
            await peer.close(ending.close_code, _CLOSE_REASONS.get(ending.close_code, ""))

    async def _pump_output(self, peer: TerminalPeer) -> TerminalEnd:
        while True:
            while self._unacked + OUTPUT_CHUNK_BYTES > self._limits.window:
                self._window_open.clear()
                await self._window_open.wait()
            try:
                data = await self._hijack.read(OUTPUT_CHUNK_BYTES)
            except OSError:
                return self._hijack_failure()
            if not data:
                return await self._conclude()
            self._unacked += len(data)
            self._peak = max(self._peak, self._unacked)
            try:
                await peer.send_bytes(data)
            except PeerClosed as closed:
                return _peer_ending(closed)

    async def _pump_input(self, peer: TerminalPeer) -> TerminalEnd:
        loop = asyncio.get_running_loop()
        while True:
            try:
                message = await peer.receive()
            except PeerClosed as closed:
                return _peer_ending(closed)
            if isinstance(message, bytes):
                if len(message) > INPUT_MAX_BYTES:
                    return TerminalEnd("protocol_error", None, CLOSE_TOO_LARGE)
                try:
                    await self._hijack.write(message)
                except OSError:
                    await asyncio.sleep(WRITE_FAILURE_GRACE_SECONDS)
                    return self._hijack_failure()
                continue
            try:
                control = _parse_control(message)
            except _ControlTooLarge:
                return TerminalEnd("protocol_error", None, CLOSE_TOO_LARGE)
            except _ControlError:
                return TerminalEnd("protocol_error", None, CLOSE_POLICY)
            if isinstance(control, _Ack):
                self._unacked = max(0, self._unacked - control.count)
                self._window_open.set()
            elif isinstance(control, _Resize):
                self._size = (control.rows, control.cols)
                self._resize_wanted.set()
            else:
                self._last_activity = loop.time()
                self._activity_seen = True

    async def _pump_resize(self) -> TerminalEnd:
        while True:
            await self._resize_wanted.wait()
            self._resize_wanted.clear()
            rows, cols = self._size
            with suppress(EngineError):
                if self._exec_id is not None:
                    await self._client.execs.resize(self._exec_id, rows, cols)
                else:
                    await self._client.containers.resize(self._container_id, rows, cols)
            await asyncio.sleep(self._limits.resize_interval)

    async def _watch_idle(self, peer: TerminalPeer) -> TerminalEnd:
        loop = asyncio.get_running_loop()
        warned = False
        while True:
            idle = loop.time() - self._last_activity
            if idle >= self._limits.idle:
                return TerminalEnd("idle_timeout", None, CLOSE_IDLE)
            if idle >= self._limits.idle_warning:
                if not warned:
                    warned = True
                    try:
                        await peer.send_text(_json(t="notice", code="idle_warning"))
                    except PeerClosed as closed:
                        return _peer_ending(closed)
                wait = self._limits.idle - idle
            else:
                warned = False
                wait = self._limits.idle_warning - idle
            await asyncio.sleep(max(wait, 0.001))

    async def _watch_duration(self) -> TerminalEnd:
        await asyncio.sleep(self._limits.max_duration)
        return TerminalEnd("max_duration", None, CLOSE_DURATION)

    async def _watch_access(self) -> TerminalEnd:
        failures = 0
        while True:
            await asyncio.sleep(self._limits.recheck)
            touch, self._activity_seen = self._activity_seen, False
            try:
                verdict = await self._recheck(touch)
            except Exception as error:
                self._activity_seen = self._activity_seen or touch
                failures += 1
                logger.warning("terminal_recheck_failed", extra={"error": type(error).__name__})
                if failures >= self._limits.recheck_failures:
                    return TerminalEnd("access_unverified", None, CLOSE_ACCESS_ENDED)
                continue
            failures = 0
            if verdict == "session_expired":
                return TerminalEnd("session_expired", None, CLOSE_ACCESS_ENDED)
            if verdict == "access_revoked":
                return TerminalEnd("access_revoked", None, CLOSE_ACCESS_ENDED)

    def _hijack_failure(self) -> TerminalEnd:
        if self._client.closed:
            return TerminalEnd("environment_closed", None, CLOSE_NORMAL)
        return TerminalEnd("engine_error", None, CLOSE_ERROR)

    async def _conclude(self) -> TerminalEnd:
        """Decide why the Engine stream ended: the process left, the container did, or a detach."""
        ending: TerminalEnd | None = None
        try:
            async with asyncio.timeout(CONCLUDE_SECONDS):
                ending = await self._inspect_end()
        except (TimeoutError, EngineError):
            ending = None
        return ending or self._hijack_failure()

    async def _inspect_end(self) -> TerminalEnd | None:
        if self._client.closed:
            return TerminalEnd("environment_closed", None, CLOSE_NORMAL)
        running = True
        try:
            container = await self._client.containers.inspect(self._container_id)
            running = container.state.running
        except NotFound:
            running = False
        except ResponseTooLarge:
            running = True
        if not running:
            return TerminalEnd("container_stopped", None, CLOSE_NORMAL)
        if self._exec_id is None:
            return TerminalEnd("detached", None, CLOSE_NORMAL)
        info = await self._settled_exec(self._exec_id)
        if info.running:
            return TerminalEnd("detached", None, CLOSE_NORMAL, process_running=True)
        return TerminalEnd("process_exited", info.exit_code, CLOSE_NORMAL)

    async def _settled_exec(self, exec_id: str) -> ExecInspect:
        """Inspect the exec, polling briefly while it reports running.

        The Engine can still report an exec as running for a moment after its stream ended, so a
        running answer is believed only if it persists.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PROCESS_SETTLE_SECONDS
        while True:
            info = await self._client.execs.inspect(exec_id)
            if not info.running or loop.time() >= deadline:
                return info
            await asyncio.sleep(PROCESS_POLL_SECONDS)

    async def _with_process_state(self, ending: TerminalEnd) -> TerminalEnd:
        """Mark an ending whose exec process is still running; ending a session cannot stop it."""
        if (
            self._exec_id is None
            or ending.process_running
            or ending.reason == "process_exited"
            or self._client.closed
        ):
            return ending
        try:
            async with asyncio.timeout(CONCLUDE_SECONDS):
                info = await self._settled_exec(self._exec_id)
        except (TimeoutError, EngineError):
            return ending
        return replace(ending, process_running=True) if info.running else ending


class TerminalService:
    """Prepares, opens and records terminals; the session itself runs in the bridge."""

    def __init__(
        self,
        *,
        database: Database,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        runtime: TerminalRuntime,
        activity: ActivityRecorder,
        clock: Clock,
        sessions: SessionService | None,
        scope: TerminalScope,
        limits: TerminalLimits | None = None,
        self_identity: SelfIdentitySource | None = None,
    ) -> None:
        self._self_identity = self_identity or no_identity
        self._database = database
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._runtime = runtime
        self._activity = activity
        self._clock = clock
        self._sessions = sessions
        self._scope = scope
        self._limits = limits or TerminalLimits()

    async def authorise_socket(
        self, actor: Principal, environment_id: int, container_id: str
    ) -> None:
        """Check write access before a WebSocket upgrade is accepted.

        ResourceNotFound when the caller has no access, the id is malformed or the container does
        not exist, PermissionDenied for a read-only holder. Nothing is changed.
        """
        if not is_canonical_id("container", container_id):
            raise ResourceNotFound("not found")
        key = ResourceKey(environment_id, "container", container_id)
        await self._authoriser.require_access(actor, key, "write")
        await self._inspector.inspect(environment_id, container_id)

    async def form_context(
        self, actor: Principal, environment_id: int, container_id: str, mode: TerminalMode
    ) -> TerminalForm:
        """Describe the terminal page for a write holder, with any reason it cannot be opened.

        ResourceNotFound without access, PermissionDenied for a read-only holder, including a
        standard user on a local-socket environment. Nothing is changed or recorded.
        """
        evaluation = await self._evaluate(actor, environment_id, container_id)
        attach = evaluation.refusals["attach"]
        return TerminalForm(
            environment_id=environment_id,
            environment_name=evaluation.environment.name,
            is_local=evaluation.environment.connection_kind == "unix_socket",
            container_id=container_id,
            short_id=short_id(container_id),
            name=evaluation.name,
            state=evaluation.inspected.state.status,
            mode=mode,
            refusal=evaluation.refusals[mode],
            attach_available=attach is None,
            attach_note=(
                attach.message
                if attach is not None and attach.code in ("attach_unavailable", "attach_busy")
                else None
            ),
            offers_user=actor.is_administrator,
        )

    async def prepare(
        self,
        actor: Principal,
        session_token: str,
        environment_id: int,
        container_id: str,
        mode: TerminalMode,
        form: Mapping[str, str],
    ) -> IssuedTicket:
        """Validate a submitted terminal form and issue the ticket the WebSocket redeems.

        Opens nothing; a refusal for dockmin's own container records one failure row.
        ResourceNotFound, PermissionDenied (read-only holders, the local-socket ceiling),
        RestrictedContainer, ContainerNotRunning, ObjectBusy, AttachNotAvailable, AttachBusy,
        ValidationFailed keyed by field, and TooManyStreams when no terminal slot is free.
        `session_token` is the browser's session cookie value, kept only as a hash.
        """
        session_hash = _session_hash(actor, session_token)
        evaluation = await self._evaluate(actor, environment_id, container_id)
        refusal = evaluation.refusals[mode]
        if refusal is not None and refusal.code == SELF_PROTECTED_REASON:
            await self._refuse_self_protected(
                actor, environment_id, container_id, mode, evaluation.name
            )
        raise_refusal(evaluation, mode)
        spec = build_spec(form, mode=mode, is_administrator=actor.is_administrator)
        self._runtime.stream_slots.check(
            actor.user_id, is_administrator=actor.is_administrator, kind="terminal"
        )
        ticket = self._runtime.terminal_tickets.issue(
            actor.user_id, session_hash, environment_id, container_id, spec
        )
        return IssuedTicket(
            ticket=ticket,
            websocket_path=TERMINAL_SOCKET_PATH.format(
                environment_id=environment_id, container_id=container_id
            ),
            mode=mode,
            name=evaluation.name,
            short_id=short_id(container_id),
            rows=spec.rows,
            cols=spec.cols,
        )

    async def open(
        self,
        actor: Principal,
        session_token: str,
        environment_id: int,
        container_id: str,
        ticket: str,
    ) -> TerminalBridge:
        """Redeem a ticket and open the session it names; the caller awaits the bridge's `run`.

        InvalidTicket, then every error `open_exec` and `open_attach` raise. The service's
        database session is not needed once this returns.
        """
        session_hash = _session_hash(actor, session_token)
        spec = self._runtime.terminal_tickets.redeem(
            ticket, actor.user_id, session_hash, environment_id, container_id
        )
        if spec.mode == "attach":
            return await self.open_attach(actor, environment_id, container_id, spec, session_hash)
        return await self.open_exec(actor, environment_id, container_id, spec, session_hash)

    @audited(
        action=_started_action,
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def _refuse_self_protected(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        mode: TerminalMode,
        name: str,
    ) -> NoReturn:
        audit = current_audit()
        audit.detail["name"] = name
        audit.reason = SELF_PROTECTED_REASON
        raise SelfProtected.terminal()

    @audited(
        action="container_exec_started",
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def open_exec(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        spec: TerminalSpec,
        session_hash: str,
    ) -> TerminalBridge:
        """Create an exec instance and start it hijacked; the started row is written on return.

        Authorises at write, applies the restricted-container rule to standard users, refuses
        a container that is stopped or held by an action, claims a terminal slot, then calls the
        Engine. Cancelled part-way, it closes what opened and records an interrupted row.
        """
        audit = current_audit()
        if spec.mode != "exec":
            raise ValueError("open_exec takes an exec request")
        session_id = secrets.token_hex(8)
        audit.detail["session"] = session_id
        evaluation = await self._admit(actor, environment_id, container_id, "exec", audit)
        if spec.user is not None and not actor.is_administrator:
            audit.refuse()
            raise ValidationFailed({"user": "Only administrators may choose the user."})
        audit.detail["command"] = spec.command[0][:MAX_PROGRAM_CHARS]
        audit.detail["argument_count"] = len(spec.command) - 1
        audit.detail["working_dir_set"] = spec.working_dir is not None
        if spec.user is not None:
            audit.detail["user"] = spec.user
        slot = self._claim(actor, audit)
        holds = AsyncExitStack()
        progress = _Progress()
        hijack: HijackedConnection | None = None
        client = evaluation.client
        try:
            body = exec_create_body(
                spec.command,
                rows=spec.rows,
                cols=spec.cols,
                working_dir=spec.working_dir,
                user=spec.user,
            )
            progress.create = asyncio.ensure_future(
                client.containers.create_exec(container_id, body)
            )
            progress.create.add_done_callback(retrieve_exception)
            exec_id = await self._engine(progress.create)
            progress.start = asyncio.ensure_future(client.execs.start(exec_id))
            progress.start.add_done_callback(retrieve_exception)
            hijack = await self._engine(progress.start)
            return self._bridge(
                actor,
                environment_id,
                container_id,
                evaluation,
                spec,
                session_hash,
                session_id,
                hijack,
                exec_id,
                slot,
                holds,
            )
        except BaseException as error:
            await self._abandon(
                error,
                actor,
                environment_id,
                container_id,
                "container_exec_started",
                audit,
                progress,
                hijack,
                holds,
                slot,
            )
            raise

    @audited(
        action="container_attach_started",
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def open_attach(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        spec: TerminalSpec,
        session_hash: str,
    ) -> TerminalBridge:
        """Attach to the container's main process hijacked; the started row is written on return.

        As `open_exec`, and refused with AttachNotAvailable unless the container was started
        for interactive use, and with AttachBusy while another session is attached.
        """
        audit = current_audit()
        if spec.mode != "attach":
            raise ValueError("open_attach takes an attach request")
        session_id = secrets.token_hex(8)
        audit.detail["session"] = session_id
        evaluation = await self._admit(actor, environment_id, container_id, "attach", audit)
        slot = self._claim(actor, audit)
        holds = AsyncExitStack()
        progress = _Progress()
        hijack: HijackedConnection | None = None
        key = ObjectKey(environment_id, "container", container_id)
        try:
            try:
                await holds.enter_async_context(
                    self._runtime.attach_locks.hold(
                        key, LockHolder(UserActor.of(actor), actor.username, "attach")
                    )
                )
            except ObjectBusy as busy:
                audit.reason = "attach_busy"
                raise AttachBusy(busy.holder) from None
            progress.start = asyncio.ensure_future(
                evaluation.client.containers.attach(container_id, detach_keys=DETACH_KEYS)
            )
            progress.start.add_done_callback(retrieve_exception)
            hijack = await self._engine(progress.start)
            return self._bridge(
                actor,
                environment_id,
                container_id,
                evaluation,
                spec,
                session_hash,
                session_id,
                hijack,
                None,
                slot,
                holds,
            )
        except BaseException as error:
            await self._abandon(
                error,
                actor,
                environment_id,
                container_id,
                "container_attach_started",
                audit,
                progress,
                hijack,
                holds,
                slot,
            )
            raise

    @audited(
        action=_ended_action,
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def record_end(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        mode: TerminalMode,
        name: str,
        session_id: str,
        reason: str,
        duration_seconds: int,
        exit_code: int | None,
        process_running: bool = False,
    ) -> None:
        """Write the ended row of a session; makes no Engine call and opens no transaction.

        Call it on a service built over a fresh database session, after the bridge returned.
        `process_running` records that the exec process outlived the session.
        """
        detail = current_audit().detail
        detail.update(
            name=name, session=session_id, duration_seconds=duration_seconds, end_reason=reason
        )
        if exit_code is not None:
            detail["exit_code"] = exit_code
        if process_running:
            detail["process_running"] = True

    async def recheck_access(
        self,
        session_hash: str,
        session_id: int,
        environment_id: int,
        container_id: str,
        *,
        was_administrator: bool,
        touch: bool,
    ) -> RecheckVerdict:
        """Report whether an open terminal's browser session may continue.

        `session_expired` when the session ran out of idle time or lifetime, `access_revoked`
        when it was ended, its user disabled or demoted from administrator, or write access to the
        container lost. `touch` extends the session's idle expiry as a request would; without it
        the check records no activity. Errors from the lookups propagate: they mean "not known".
        """
        if self._sessions is None:
            raise RuntimeError("terminal sessions need the session service")
        standing = await self._sessions.standing(session_hash, session_id, touch=touch)
        if standing.state == "expired":
            return "session_expired"
        principal = standing.principal
        if principal is None or (was_administrator and not principal.is_administrator):
            return "access_revoked"
        try:
            await self._authoriser.require_access(
                principal, ResourceKey(environment_id, "container", container_id), "write"
            )
        except (ResourceNotFound, PermissionDenied):
            return "access_revoked"
        return "permitted"

    def _claim(self, actor: Principal, audit: AuditState) -> StreamSlot:
        try:
            return self._runtime.stream_slots.acquire(
                actor.user_id, is_administrator=actor.is_administrator, kind="terminal"
            )
        except TooManyStreams:
            audit.refuse()
            raise

    def _bridge(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        evaluation: _Evaluation,
        spec: TerminalSpec,
        session_hash: str,
        session_id: str,
        hijack: HijackedConnection,
        exec_id: str | None,
        slot: StreamSlot,
        holds: AsyncExitStack,
    ) -> TerminalBridge:
        return TerminalBridge(
            mode=spec.mode,
            environment_id=environment_id,
            container_id=container_id,
            session_id=session_id,
            hijack=hijack,
            client=evaluation.client,
            exec_id=exec_id,
            slot=slot,
            holds=holds,
            recheck=partial(
                self._recheck,
                session_hash,
                actor.session_id,
                environment_id,
                container_id,
                actor.is_administrator,
            ),
            record_end=partial(
                self._end_row,
                actor,
                environment_id,
                container_id,
                spec.mode,
                evaluation.name,
                session_id,
            ),
            limits=self._limits,
            size=(spec.rows, spec.cols),
        )

    async def _recheck(
        self,
        session_hash: str,
        session_id: int | None,
        environment_id: int,
        container_id: str,
        was_administrator: bool,
        touch: bool,
    ) -> RecheckVerdict:
        if session_id is None:
            raise RuntimeError("a terminal needs a browser session")
        async with self._scope() as fresh:
            return await fresh.recheck_access(
                session_hash,
                session_id,
                environment_id,
                container_id,
                was_administrator=was_administrator,
                touch=touch,
            )

    async def _end_row(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        mode: TerminalMode,
        name: str,
        session_id: str,
        reason: str,
        duration_seconds: int,
        exit_code: int | None,
        process_running: bool,
    ) -> None:
        try:
            async with self._scope() as fresh:
                await fresh.record_end(
                    actor,
                    environment_id,
                    container_id,
                    mode,
                    name,
                    session_id,
                    reason,
                    duration_seconds,
                    exit_code,
                    process_running,
                )
        except Exception as error:
            logger.error(
                "activity_event_write_failed",
                extra={
                    "action": _ended_action({"mode": mode}),
                    "session": session_id,
                    "error": type(error).__name__,
                },
            )

    async def _evaluate(
        self, actor: Principal, environment_id: int, container_id: str
    ) -> _Evaluation:
        if not is_canonical_id("container", container_id):
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "container", container_id), "write"
        )
        return await self._load(actor, environment_id, container_id)

    async def _load(self, actor: Principal, environment_id: int, container_id: str) -> _Evaluation:
        environment = await self._hosts.environment(environment_id)
        inspected = await self._inspector.inspect(environment_id, container_id)
        client = await self._hosts.client(environment_id)
        restricted = (
            ()
            if actor.is_administrator
            else await self._restricted(environment_id, client, inspected)
        )
        key = ObjectKey(environment_id, "container", container_id)
        locked = self._runtime.locks.holder_of(key)
        attached = self._runtime.attach_locks.holder_of(key)
        config = inspected.config
        common: TerminalRefusal | None = None
        if self._self_identity().protects_container(container_id):
            common = TerminalRefusal(SELF_PROTECTED_REASON, str(SelfProtected.terminal()))
        elif restricted:
            labels = tuple(option_label(option) for option in restricted)
            common = TerminalRefusal("restricted_container", _restricted_message(labels), labels)
        elif not inspected.state.running:
            common = TerminalRefusal("not_running", "The container is not running.")
        elif locked is not None:
            common = TerminalRefusal("object_busy", _BUSY)
        attach = common
        if attach is None:
            if not (config.tty and config.open_stdin):
                attach = TerminalRefusal("attach_unavailable", _ATTACH_UNAVAILABLE)
            elif config.stdin_once:
                attach = TerminalRefusal("attach_unavailable", _ATTACH_STDIN_ONCE)
            elif attached is not None:
                attach = TerminalRefusal("attach_busy", _ATTACH_BUSY)
        return _Evaluation(
            client=client,
            environment=environment,
            inspected=inspected,
            name=display_name([inspected.name], inspected.id),
            refusals={"exec": common, "attach": attach},
            locked=locked,
            attached=attached,
        )

    async def _admit(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        mode: TerminalMode,
        audit: AuditState,
    ) -> _Evaluation:
        if not is_canonical_id("container", container_id):
            raise ResourceNotFound("not found")
        try:
            await self._authoriser.require_access(
                actor, ResourceKey(environment_id, "container", container_id), "write"
            )
        except (EnvironmentUnavailable, ContainerTooLarge):
            audit.refuse()
            raise
        try:
            evaluation = await self._load(actor, environment_id, container_id)
        except EnvironmentUnavailable as unavailable:
            audit.reason = (
                "outcome_unknown" if unavailable.kind == "outcome_unknown" else "unreachable"
            )
            raise
        except ContainerTooLarge:
            audit.reason = "too_large"
            raise
        audit.detail["name"] = evaluation.name
        try:
            raise_refusal(evaluation, mode)
        except (RestrictedContainer, AttachNotAvailable):
            audit.refuse()
            raise
        except SelfProtected:
            audit.reason = SELF_PROTECTED_REASON
            raise
        except ContainerNotRunning:
            audit.reason = "not_running"
            raise
        except AttachBusy:
            audit.reason = "attach_busy"
            raise
        except ObjectBusy as busy:
            audit.reason = "object_busy"
            audit.detail["holder_action"] = busy.holder.action
            raise
        return evaluation

    async def _restricted(
        self, environment_id: int, client: EngineClient, inspected: ContainerInspect
    ) -> tuple[str, ...]:
        """Return the ids of the restricted options the container's configuration uses."""
        cache = self._runtime.restricted_containers
        remembered = cache.recall(environment_id, inspected.id)
        if remembered is None:
            remembered = await self._derive_restricted(client, inspected)
            if remembered != UNREADABLE_OPTIONS:
                cache.remember(environment_id, inspected.id, remembered)
        return remembered

    async def _derive_restricted(
        self, client: EngineClient, inspected: ContainerInspect
    ) -> tuple[str, ...]:
        image: ImageInspect | None = None
        try:
            info = await client.system.info()
        except EngineError as error:
            raise unavailable_from(error) from None
        try:
            image = await client.images.inspect(inspected.image)
        except (NotFound, ResponseTooLarge):
            image = None
        except EngineError as error:
            raise unavailable_from(error) from None
        defaults = DaemonDefaults(
            runtime=info.default_runtime or None,
            logging_driver=info.logging_driver or None,
            cgroup_version=info.cgroup_version or None,
        )
        document = await asyncio.to_thread(container_document, inspected)
        try:
            derivation = await asyncio.to_thread(
                partial(
                    derive_recreate,
                    document,
                    image_inspect=image_document(image),
                    defaults=defaults,
                )
            )
        except ValueError:
            return UNREADABLE_OPTIONS
        return refused_options(find_restricted(derivation.body, default_runtime=defaults.runtime))

    async def _engine[T](self, task: asyncio.Future[T]) -> T:
        audit = current_audit()
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except NotFound:
            audit.reason = "not_found"
            raise ContainerGone() from None
        except Conflict:
            audit.reason = "not_running"
            raise ContainerNotRunning() from None
        except RequestOutcomeUnknown:
            audit.reason = "outcome_unknown"
            raise EnvironmentUnavailable("outcome_unknown") from None
        except EnvironmentUnreachable:
            audit.reason = "unreachable"
            raise EnvironmentUnavailable("unreachable") from None
        except EngineError as error:
            audit.reason = "engine_error"
            logger.warning(
                "terminal_open_refused",
                extra={"error": type(error).__name__, "status": error.status_code},
            )
            raise EngineRefused() from None

    async def _abandon(
        self,
        error: BaseException,
        actor: Principal,
        environment_id: int,
        container_id: str,
        action: str,
        audit: AuditState,
        progress: _Progress,
        hijack: HijackedConnection | None,
        holds: AsyncExitStack,
        slot: StreamSlot,
    ) -> None:
        """Release what a failed or cancelled open took, then record a cancellation."""
        with anyio.CancelScope(shield=True):
            opened = hijack
            if isinstance(error, asyncio.CancelledError):
                opened = await self._settled_hijack(progress, hijack)
            if opened is not None:
                with suppress(Exception):
                    await opened.close()
            with suppress(Exception):
                await holds.aclose()
            slot.close()
            if isinstance(error, asyncio.CancelledError):
                await self._write_interrupted(
                    actor, environment_id, container_id, action, audit, progress, opened
                )

    async def _settled_hijack(
        self, progress: _Progress, hijack: HijackedConnection | None
    ) -> HijackedConnection | None:
        """Wait for an Engine call that was in flight at cancellation, and return its hijack."""
        if hijack is not None:
            return hijack
        pending = [task for task in (progress.create, progress.start) if task is not None]
        if pending:
            await asyncio.wait(pending, timeout=SETTLE_SECONDS)
        start = progress.start
        if start is None:
            return None
        if start.done() and not start.cancelled() and start.exception() is None:
            return start.result()
        if not start.done():
            start.add_done_callback(_close_late_hijack)
        return None

    @staticmethod
    def _interrupted_failure(action: str, progress: _Progress) -> str | None:
        """Return the failure reason an interrupted open leaves behind, None for nothing."""
        attach = action == "container_attach_started"
        create, start = progress.create, progress.start
        if start is not None:
            if not start.done():
                return "outcome_unknown"
            failed = start.cancelled() or start.exception() is not None
            return "interrupted" if failed and not attach else None
        if create is None:
            return None
        if not create.done():
            return "outcome_unknown"
        return None if create.cancelled() or create.exception() is not None else "interrupted"

    async def _write_interrupted(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        action: str,
        audit: AuditState,
        progress: _Progress,
        opened: HijackedConnection | None,
    ) -> None:
        failure = None if opened is not None else self._interrupted_failure(action, progress)
        if opened is None and failure is None:
            return
        detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
        subject = UserActor.of(actor)
        try:
            async with self._database.session() as db:
                recorder = ActivityRecorder(db, self._clock)
                if failure is not None:
                    await recorder.record_failure(
                        subject,
                        action=action,
                        target_type="container",
                        target_id=container_id,
                        environment_id=environment_id,
                        reason=failure,
                        detail=detail,
                        message=_INTERRUPTED_MESSAGES[failure],
                    )
                else:
                    await retry_busy(
                        partial(
                            recorder.record_event,
                            subject,
                            action=action,
                            target_type="container",
                            target_id=container_id,
                            environment_id=environment_id,
                            result="success",
                            detail=detail,
                        )
                    )
        except Exception as error:
            logger.error(
                "activity_event_write_failed",
                extra={
                    "action": action,
                    "session": str(audit.detail.get("session", "")),
                    "error": type(error).__name__,
                },
            )


_LATE_CLOSES: set[asyncio.Future[None]] = set()


def _close_late_hijack(future: "asyncio.Future[HijackedConnection]") -> None:
    if future.cancelled() or future.exception() is not None:
        return
    task = asyncio.ensure_future(future.result().close())
    _LATE_CLOSES.add(task)
    task.add_done_callback(_LATE_CLOSES.discard)
    task.add_done_callback(retrieve_exception)


async def open_guarded(opening: Coroutine[Any, Any, TerminalBridge]) -> TerminalBridge:
    """Await an open that finishes even if the caller is cancelled, so what it opens is never lost.

    The open runs as a task of its own. When the caller is cancelled, the open is awaited to the
    end and any bridge it produced is abandoned, which releases everything it holds and records
    the interrupted session. The cancellation is then re-raised.
    """
    task = asyncio.ensure_future(opening)
    try:
        await asyncio.wait({task})
    except asyncio.CancelledError:
        await completed(_abandon_when_open(task))
        raise
    return task.result()


async def _abandon_when_open(task: "asyncio.Future[TerminalBridge]") -> None:
    await asyncio.wait({task})
    if not task.cancelled() and task.exception() is None:
        await task.result().abandon()


def _session_hash(actor: Principal, session_token: str) -> str:
    if actor.session_id is None or not session_token or not session_token.isascii():
        raise PermissionDenied("insufficient access")
    return hash_token(session_token)


def raise_refusal(evaluation: _Evaluation, mode: TerminalMode) -> None:
    """Raise the error for the reason the evaluation refuses this mode, if it does."""
    refusal = evaluation.refusals[mode]
    if refusal is None:
        return
    match refusal.code:
        case "self_protected":
            raise SelfProtected.terminal()
        case "restricted_container":
            raise RestrictedContainer(refusal.options)
        case "not_running":
            raise ContainerNotRunning()
        case "object_busy":
            assert evaluation.locked is not None
            raise ObjectBusy(evaluation.locked)
        case "attach_busy":
            assert evaluation.attached is not None
            raise AttachBusy(evaluation.attached)
        case "attach_unavailable":
            raise AttachNotAvailable(refusal.message)
