"""Login lockout: failure counters per username and per address, and the locks that order them."""

import asyncio
import math
import sqlite3
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial

from app.db.engine import retry_busy
from app.db.repositories.clocked import Clock, parse_timestamp
from app.db.repositories.login_throttle import LoginThrottleRepository, ThrottleKind
from app.logging import get_logger

logger = get_logger(__name__)

SUBJECT_MAX = 128

LockKey = tuple[ThrottleKind, str]


def username_subject(username: str) -> str:
    """Normalise an attempted username so case and padding do not dodge the counter."""
    return username.strip().casefold()[:SUBJECT_MAX]


@dataclass(slots=True)
class _Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class KeyedLocks:
    """In-process mutual exclusion per key; an entry lives only while it is held or awaited."""

    def __init__(self) -> None:
        self._entries: dict[LockKey, _Entry] = {}

    @asynccontextmanager
    async def hold(self, *keys: LockKey) -> AsyncIterator[None]:
        """Hold every key at once; a fixed acquisition order keeps two holders from deadlocking."""
        ordered = sorted(set(keys))
        for key in ordered:
            self._entries.setdefault(key, _Entry()).users += 1
        held: list[_Entry] = []
        try:
            for key in ordered:
                entry = self._entries[key]
                await entry.lock.acquire()
                held.append(entry)
            yield
        finally:
            for entry in reversed(held):
                entry.lock.release()
            for key in ordered:
                entry = self._entries[key]
                entry.users -= 1
                if entry.users == 0:
                    del self._entries[key]


class LoginThrottleService:
    """Locks a subject out for the window once it accumulates the allowed number of failures.

    An address lock refuses a login only when enforce_address is set; the address is counted either
    way. A shared address, as behind an untrusted proxy, would let one caller lock everyone out.
    """

    def __init__(
        self,
        repository: LoginThrottleRepository,
        clock: Clock,
        *,
        max_failures: int,
        window: timedelta,
        enforce_address: bool,
        locks: KeyedLocks,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._max_failures = max_failures
        self._window = window
        self._enforce_address = enforce_address
        self._locks = locks

    def guard(self, username: str | None, address: str | None) -> AbstractAsyncContextManager[None]:
        """Serialise every attempt that shares a username or an address with this one."""
        return self._locks.hold(*(self._subjects(username, address)))

    async def retry_after(self, username: str | None, address: str | None) -> int:
        """Return seconds until the stricter applicable lock lifts, or 0 when none holds."""
        now = self._clock()
        remaining = 0
        for kind, subject in self._subjects(username, address):
            if kind == "address" and not self._enforce_address:
                continue
            row = await self._repository.get(kind, subject)
            if row is not None and row.locked_until is not None:
                left = (parse_timestamp(row.locked_until) - now).total_seconds()
                remaining = max(remaining, math.ceil(left))
        return max(remaining, 0)

    async def record_failure(self, username: str | None, address: str) -> None:
        """Count a failure against both subjects, locking either that reaches the limit."""
        for kind, subject in self._subjects(username, address):
            await retry_busy(partial(self._increment, kind, subject))
        try:
            await self._repository.delete_stale(window=self._window)
        except sqlite3.OperationalError:
            # Pruning is housekeeping; a conflict must not turn a counted attempt into an error.
            logger.debug("login_throttle_prune_skipped")

    async def settle_success(self, username: str, address: str) -> None:
        """Forget the username's failures and take back the address's count of this attempt.

        The address count is decremented, not cleared, so signing in to a valid account between
        guesses does not reset it.
        """
        await self._repository.delete("username", username_subject(username))
        await self._repository.decrement("address", address, max_failures=self._max_failures)

    async def refund(self, username: str | None, address: str) -> None:
        """Take back one counted attempt that was refused before it could be judged."""
        for kind, subject in self._subjects(username, address):
            await self._repository.decrement(kind, subject, max_failures=self._max_failures)

    async def clear_username(self, username: str) -> int:
        """Lift a username's lock and forget its failures; return how many counters went."""
        return await self._repository.delete("username", username_subject(username))

    async def clear(self, subject: str) -> int:
        """Lift every lock held on a subject read as a username or as an address."""
        removed = await self._repository.delete("username", username_subject(subject))
        return removed + await self._repository.delete("address", subject.strip())

    def _subjects(self, username: str | None, address: str | None) -> list[LockKey]:
        subjects: list[LockKey] = []
        if address is not None:
            subjects.append(("address", address))
        if username is not None:
            subjects.append(("username", username_subject(username)))
        return subjects

    async def _increment(self, kind: ThrottleKind, subject: str) -> None:
        await self._repository.increment(
            kind, subject, max_failures=self._max_failures, window=self._window
        )
