"""Holds uploaded stack archives, and the secrets opened from them, between review and import."""

import asyncio
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from app.services.activity import completed
from app.services.stack_import_archive import StackArchiveSummary
from app.services.volume_import_store import SpooledUpload, unlink_quietly

__all__ = [
    "CAPACITY",
    "LIFETIME_SECONDS",
    "SPOOL_DIRECTORY",
    "UNSEAL_FAILURE_LIMIT",
    "HeldStack",
    "StackImportStore",
]

SPOOL_DIRECTORY: Final = "stack-import"
LIFETIME_SECONDS: Final = 1800.0
CAPACITY: Final = 4
UNSEAL_FAILURE_LIMIT: Final = 5
_ID_BYTES: Final = 16


@dataclass(slots=True)
class HeldStack:
    """A validated stack upload, usable only by the session, user and environment that made it.

    `opened` holds the secret values once the passphrase has opened them, keyed by sealed name;
    it lives in memory only and is dropped with the hold.
    """

    id: str
    user_id: int
    session_id: int
    environment_id: int
    kind: str
    path: Path
    digest: str
    size: int
    summary: StackArchiveSummary
    expires: float
    importing: bool = False
    opened: dict[str, bytes] | None = field(default=None, repr=False)


class StackImportStore:
    """In-memory holds on spooled stack uploads: one per user, four in all, 30 minutes each.

    A hold is dropped, and its file removed, on a newer upload by the same user, on discard and
    on expiry; a hold whose import has begun does not expire. Failed attempts to open a hold's
    sealed values are counted per session for the life of the process.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._held: OrderedDict[str, HeldStack] = OrderedDict()
        self._timers: set[asyncio.TimerHandle] = set()
        self._sweeps: set[asyncio.Future[None]] = set()
        self._failures: dict[int, int] = {}

    @staticmethod
    def new_id() -> str:
        """Return a fresh upload id."""
        return secrets.token_hex(_ID_BYTES)

    async def hold(
        self,
        upload_id: str,
        *,
        user_id: int,
        session_id: int,
        environment_id: int,
        kind: str,
        upload: SpooledUpload,
        summary: StackArchiveSummary,
    ) -> HeldStack:
        """Hold a validated upload, dropping the user's earlier one and the oldest past four."""
        doomed = self._expire()
        held = HeldStack(
            upload_id,
            user_id,
            session_id,
            environment_id,
            kind,
            upload.path,
            upload.digest,
            upload.size,
            summary,
            self._clock() + LIFETIME_SECONDS,
        )
        for token, other in list(self._held.items()):
            if other.user_id == user_id and not other.importing:
                doomed.append(self._held.pop(token))
        self._held[upload_id] = held
        while len(self._held) > CAPACITY:
            oldest = next((t for t, h in self._held.items() if not h.importing), None)
            if oldest is None:
                break
            doomed.append(self._held.pop(oldest))
        await self._remove(doomed)
        self._arm()
        return held

    async def get(
        self, upload_id: str, *, user_id: int, session_id: int, environment_id: int
    ) -> HeldStack | None:
        """Return the hold when it is live and was made by exactly this session and environment."""
        await self._remove(self._expire())
        held = self._held.get(upload_id)
        if held is None or (held.user_id, held.session_id, held.environment_id) != (
            user_id,
            session_id,
            environment_id,
        ):
            return None
        return held

    async def discard(self, upload_id: str) -> None:
        """Drop a hold and remove its file."""
        held = self._held.pop(upload_id, None)
        if held is not None:
            held.opened = None
            await self._remove([held])

    def failures(self, session_id: int) -> int:
        """Return how many attempts to open sealed values have failed in this session."""
        return self._failures.get(session_id, 0)

    def note_failure(self, session_id: int) -> int:
        """Count a failed attempt and return how many the session has left."""
        count = self._failures.get(session_id, 0) + 1
        self._failures[session_id] = count
        return max(0, UNSEAL_FAILURE_LIMIT - count)

    async def sweep(self) -> None:
        """Remove what has expired."""
        await self._remove(self._expire())

    async def aclose(self) -> None:
        """Cancel the timers and remove every held file."""
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        held = list(self._held.values())
        self._held.clear()
        await self._remove(held)

    def _expire(self) -> list[HeldStack]:
        now = self._clock()
        gone = [t for t, h in self._held.items() if h.expires <= now and not h.importing]
        return [self._held.pop(token) for token in gone]

    async def _remove(self, held: list[HeldStack]) -> None:
        for item in held:
            item.opened = None
            await completed(asyncio.to_thread(unlink_quietly, item.path))

    def _arm(self) -> None:
        loop = asyncio.get_running_loop()
        timer: asyncio.TimerHandle | None = None

        def fire() -> None:
            if timer is not None:
                self._timers.discard(timer)
            future = asyncio.ensure_future(self.sweep())
            self._sweeps.add(future)
            future.add_done_callback(self._sweeps.discard)

        timer = loop.call_later(LIFETIME_SECONDS + 1, fire)
        self._timers.add(timer)
