"""In-process registries that bound concurrent work: per-object locks and stream slots."""

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from app.auth.errors import ProductError
from app.db.repositories.clocked import Clock, utc_now
from app.logging import get_logger
from app.registry_client.references import ReferenceInvalid, parse_reference
from app.services.access import ResourceType, is_canonical_id
from app.services.activity import Actor

logger = get_logger(__name__)

MAX_STREAMS_PER_USER = 4
MAX_STREAMS_PER_PROCESS = 64
MAX_STANDARD_STREAMS = 48
MAX_TERMINALS_PER_USER = 2
MAX_TERMINALS_PER_PROCESS = 16
MAX_STACK_STREAMS_PER_ENVIRONMENT = 3
MAX_STACK_STREAMS_PER_STANDARD_USER = 1
ADMINISTRATOR_STACK_RESERVE = 1

type StreamKind = Literal["view", "terminal"]
type LockObserver = Callable[[ObjectKey, LockHolder], None]
type LockType = ResourceType | Literal["image", "image_reference", "node"]

_IMAGE_ID = re.compile(r"[0-9a-f]{64}")
_NODE_ID = re.compile(r"[0-9a-z]{25}")


def _canonical_lock_id(object_type: LockType, object_id: str) -> bool:
    match object_type:
        case "image":
            return _IMAGE_ID.fullmatch(object_id) is not None
        case "node":
            return _NODE_ID.fullmatch(object_id) is not None
        case "image_reference":
            try:
                return parse_reference(object_id).key == object_id
            except ReferenceInvalid:
                return False
        case _:
            return is_canonical_id(object_type, object_id)


@dataclass(frozen=True, slots=True)
class ObjectKey:
    """One lockable object; the id must be the canonical one so a name cannot make a second lock.

    An image is locked by its 64-hex id, an image reference by its canonical key, and a node by
    its 25 base-36 id.
    """

    environment_id: int
    object_type: LockType
    object_id: str

    def __post_init__(self) -> None:
        """Refuse an id that is not in the canonical form of its type."""
        if not _canonical_lock_id(self.object_type, self.object_id):
            raise ValueError(f"{self.object_type} lock keys use the canonical id")


@dataclass(frozen=True, slots=True)
class LockHolder:
    """Who holds a lock and for what; the registry stamps `since` when the lock is taken."""

    actor: Actor
    label: str
    action: str
    since: datetime | None = None

    def age_seconds(self, now: datetime) -> float:
        """Return how long the lock has been held, never negative."""
        if self.since is None:
            return 0.0
        return max(0.0, (now - self.since).total_seconds())


class ObjectBusy(ProductError):
    """The object is locked by another action; the message is fixed and names nobody."""

    def __init__(self, holder: LockHolder) -> None:
        super().__init__("The object is busy with another action.")
        self.holder = holder


class TooManyStreams(ProductError):
    """A user or the process has no free stream slot; the message names the limit reached."""

    def __init__(
        self,
        scope: Literal["user", "process"],
        kind: StreamKind = "view",
        limit: int | None = None,
        message: str | None = None,
    ) -> None:
        super().__init__(message or _refusal_text(scope, kind, limit))
        self.scope = scope
        self.kind = kind


def _refusal_text(scope: Literal["user", "process"], kind: StreamKind, limit: int | None) -> str:
    match scope, kind:
        case "user", "view":
            count = MAX_STREAMS_PER_USER if limit is None else limit
            return (
                f"You have {count} live views open (stats, logs or downloads). "
                "Close one, then try again."
            )
        case "user", "terminal":
            count = MAX_TERMINALS_PER_USER if limit is None else limit
            return f"You have {count} terminals open. Close one, then try again."
        case "process", "view":
            return "This server has reached its limit of open live views. Try again shortly."
        case _:
            return "This server has reached its limit of open terminals. Try again shortly."


@dataclass(eq=False, slots=True)
class _Entry:
    holder: LockHolder
    released: asyncio.Event = field(default_factory=asyncio.Event)


class ObjectLockRegistry:
    """Serialises actions on one object across the process.

    Single instance only: the registry lives in this process's memory.
    """

    def __init__(self, clock: Clock = utc_now, *, observer: LockObserver | None = None) -> None:
        self._clock = clock
        self._observer = observer
        self._held: dict[ObjectKey, _Entry] = {}

    def __len__(self) -> int:
        return len(self._held)

    def holder_of(self, key: ObjectKey) -> LockHolder | None:
        """Return the current holder, or None when the object is free."""
        entry = self._held.get(key)
        return None if entry is None else entry.holder

    def now(self) -> datetime:
        """Return the registry's clock, for ageing a holder against the same time source."""
        return self._clock()

    def set_observer(self, observer: LockObserver | None) -> None:
        """Replace the callback told of every release and every note."""
        self._observer = observer

    def note(self, key: ObjectKey, holder: LockHolder) -> None:
        """Tell the observer an action finished on the object without holding its lock."""
        self._notify(key, holder)

    def hold(
        self, key: ObjectKey, holder: LockHolder, *, wait: float = 0.0
    ) -> AbstractAsyncContextManager[None]:
        """Hold the object for the body; raise ObjectBusy if it is not free within `wait` seconds.

        A zero wait fails at once without yielding to the event loop. The lock is released on
        every exit, cancellation included.
        """
        return self._held_for_body(key, holder, wait)

    @asynccontextmanager
    async def _held_for_body(
        self, key: ObjectKey, holder: LockHolder, wait: float
    ) -> AsyncIterator[None]:
        entry = await self._acquire(key, holder, wait)
        try:
            yield
        finally:
            self._drop(key, entry)

    async def wait_free(self, key: ObjectKey, timeout: float) -> LockHolder | None:  # noqa: ASYNC109
        """Wait up to `timeout` seconds for the object to be free, claiming nothing.

        Returns the holder still in place when the time ran out, or None once the object is free.
        """
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        blocker = await self._until_free(key, deadline)
        return None if blocker is None else blocker.holder

    async def _acquire(self, key: ObjectKey, holder: LockHolder, wait: float) -> _Entry:
        deadline = asyncio.get_running_loop().time() + max(0.0, wait)
        blocker = await self._until_free(key, deadline)
        if blocker is not None:
            raise ObjectBusy(blocker.holder)
        entry = _Entry(replace(holder, since=self._clock()))
        self._held[key] = entry
        return entry

    async def _until_free(self, key: ObjectKey, deadline: float) -> _Entry | None:
        while (current := self._held.get(key)) is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return current
            try:
                async with asyncio.timeout(remaining):
                    await current.released.wait()
            except TimeoutError:
                continue
        return None

    def _drop(self, key: ObjectKey, entry: _Entry) -> None:
        if self._held.get(key) is entry:
            del self._held[key]
        entry.released.set()
        self._notify(key, entry.holder)

    def _notify(self, key: ObjectKey, holder: LockHolder) -> None:
        if self._observer is None:
            return
        try:
            self._observer(key, holder)
        except Exception as error:
            logger.error("lock_observer_failed", extra={"error": type(error).__name__})


class StreamSlot:
    """One claimed stream slot; close it on every path, and closing twice is harmless.

    A slot dropped without being closed gives itself back when collected.
    """

    def __init__(
        self,
        slots: "StreamSlots",
        user_id: int,
        *,
        shared: bool,
        kind: StreamKind,
        stack_environment: int | None = None,
    ) -> None:
        self._slots: StreamSlots | None = slots
        self._user_id = user_id
        self._shared = shared
        self._kind = kind
        self._stack_environment = stack_environment

    def close(self) -> None:
        """Give the slot back."""
        slots, self._slots = self._slots, None
        if slots is not None:
            slots._give_back(
                self._user_id,
                shared=self._shared,
                kind=self._kind,
                stack_environment=self._stack_environment,
            )

    def __del__(self) -> None:
        self.close()


class StreamSlots:
    """Counts open long-lived streams per user and per process.

    Live views (stats, logs, downloads) and terminals are counted per user separately, and both
    count towards the process total. Streams opened by standard users share a smaller pool than
    the process total, so administrators always have the difference. Stack log views also count
    per environment, whoever opens them.
    """

    def __init__(
        self,
        *,
        per_user: int = MAX_STREAMS_PER_USER,
        total: int = MAX_STREAMS_PER_PROCESS,
        standard_total: int = MAX_STANDARD_STREAMS,
        terminals_per_user: int = MAX_TERMINALS_PER_USER,
        terminals_total: int = MAX_TERMINALS_PER_PROCESS,
        stack_per_environment: int = MAX_STACK_STREAMS_PER_ENVIRONMENT,
        stack_per_standard_user: int = MAX_STACK_STREAMS_PER_STANDARD_USER,
        stack_administrator_reserve: int = ADMINISTRATOR_STACK_RESERVE,
    ) -> None:
        self._per_user: dict[StreamKind, int] = {"view": per_user, "terminal": terminals_per_user}
        self._total = total
        self._standard_total = standard_total
        self._terminals_total = terminals_total
        self._stack_per_environment = stack_per_environment
        self._stack_per_standard_user = stack_per_standard_user
        self._stack_standard_total = max(stack_per_environment - stack_administrator_reserve, 0)
        self._stack_open: dict[int, int] = {}
        self._stack_standard: dict[int, int] = {}
        self._stack_by_user: dict[tuple[int, int], int] = {}
        self._by_user: dict[StreamKind, dict[int, int]] = {"view": {}, "terminal": {}}
        self._open = 0
        self._standard_open = 0
        self._terminals_open = 0

    @property
    def open_count(self) -> int:
        """Return how many slots are claimed across the process."""
        return self._open

    def check(
        self, user_id: int, *, is_administrator: bool = False, kind: StreamKind = "view"
    ) -> None:
        """Raise TooManyStreams if a claim would be refused now; claims nothing."""
        refusal = self._refusal(user_id, is_administrator, kind)
        if refusal is not None:
            raise refusal

    def acquire(
        self, user_id: int, *, is_administrator: bool = False, kind: StreamKind = "view"
    ) -> StreamSlot:
        """Acquire a slot, or raise TooManyStreams naming which limit was reached."""
        self.check(user_id, is_administrator=is_administrator, kind=kind)
        return self._claim(user_id, is_administrator, kind, None)

    def stack_open_count(self, environment_id: int) -> int:
        """Return how many stack log views are open against the environment."""
        return self._stack_open.get(environment_id, 0)

    def acquire_stack(
        self, user_id: int, environment_id: int, *, is_administrator: bool = False
    ) -> StreamSlot:
        """Acquire a view slot that also counts against the environment's stack log views.

        TooManyStreams for the user or process limit as `acquire` does. Past that, an
        environment holds at most MAX_STACK_STREAMS_PER_ENVIRONMENT stack log views. A standard
        user holds at most one in an environment, and standard users together leave the
        administrator reserve free; administrators are bound by the environment total only.
        """
        self.check(user_id, is_administrator=is_administrator)
        if self.stack_open_count(environment_id) >= self._stack_per_environment:
            raise TooManyStreams("process")
        if not is_administrator:
            if self._stack_standard.get(environment_id, 0) >= self._stack_standard_total:
                raise TooManyStreams("process")
            if (
                self._stack_by_user.get((environment_id, user_id), 0)
                >= self._stack_per_standard_user
            ):
                raise TooManyStreams(
                    "user",
                    message="You already have a stack log view open in this environment. "
                    "Close it, then try again.",
                )
        return self._claim(user_id, is_administrator, "view", environment_id)

    def _claim(
        self, user_id: int, is_administrator: bool, kind: StreamKind, stack_environment: int | None
    ) -> StreamSlot:
        self._open += 1
        self._standard_open += not is_administrator
        self._terminals_open += kind == "terminal"
        held = self._by_user[kind]
        held[user_id] = held.get(user_id, 0) + 1
        if stack_environment is not None:
            self._stack_open[stack_environment] = self.stack_open_count(stack_environment) + 1
            if not is_administrator:
                self._stack_standard[stack_environment] = (
                    self._stack_standard.get(stack_environment, 0) + 1
                )
                owner = (stack_environment, user_id)
                self._stack_by_user[owner] = self._stack_by_user.get(owner, 0) + 1
        return StreamSlot(
            self,
            user_id,
            shared=not is_administrator,
            kind=kind,
            stack_environment=stack_environment,
        )

    def _refusal(
        self, user_id: int, is_administrator: bool, kind: StreamKind
    ) -> TooManyStreams | None:
        if (
            self._open >= self._total
            or (not is_administrator and self._standard_open >= self._standard_total)
            or (kind == "terminal" and self._terminals_open >= self._terminals_total)
        ):
            return TooManyStreams("process", kind)
        if self._by_user[kind].get(user_id, 0) >= self._per_user[kind]:
            return TooManyStreams("user", kind, self._per_user[kind])
        return None

    def _give_back(
        self, user_id: int, *, shared: bool, kind: StreamKind, stack_environment: int | None
    ) -> None:
        self._open -= 1
        self._standard_open -= shared
        self._terminals_open -= kind == "terminal"
        held = self._by_user[kind]
        remaining = held[user_id] - 1
        if remaining:
            held[user_id] = remaining
        else:
            del held[user_id]
        if stack_environment is not None:
            _decrement(self._stack_open, stack_environment)
            if shared:
                _decrement(self._stack_standard, stack_environment)
                _decrement(self._stack_by_user, (stack_environment, user_id))


def _decrement[K](counts: dict[K, int], key: K) -> None:
    left = counts[key] - 1
    if left:
        counts[key] = left
    else:
        del counts[key]
