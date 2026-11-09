"""Limits on opening sealed archives: one key derivation at a time, and a budget per session."""

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Final

from app.auth.errors import ProductError

UNLOCK_ATTEMPT_LIMIT: Final = 5


class UnlockAttemptsExhausted(ProductError):
    """The session has no attempts left to open sealed values."""

    def __init__(self) -> None:
        super().__init__("This session has used its attempts to open sealed values.")


class UnlockReservation:
    """One attempt taken from a session's budget; `refund` returns it after a success."""

    __slots__ = ("_open", "_release", "remaining")

    def __init__(self, release: Callable[[], None], remaining: int) -> None:
        self._release = release
        self._open = True
        self.remaining = remaining

    def refund(self) -> None:
        """Return the attempt to the session; a second call changes nothing."""
        if self._open:
            self._open = False
            self._release()


class UnlockGate:
    """Serialises key derivations and counts attempts per session before each one starts."""

    def __init__(self, attempt_limit: int = UNLOCK_ATTEMPT_LIMIT) -> None:
        self._limit = attempt_limit
        self._used: dict[int, int] = {}
        self._bound: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None

    def slot(self) -> AbstractAsyncContextManager[None]:
        """Hold the single derivation slot: `async with gate.slot():` around the derivation."""
        loop = asyncio.get_running_loop()
        if self._bound is None or self._bound[0] is not loop:
            self._bound = (loop, asyncio.Semaphore(1))
        return self._bound[1]

    def remaining(self, session_id: int) -> int:
        """Return how many attempts the session has left."""
        return max(0, self._limit - self._used.get(session_id, 0))

    def reserve(self, session_id: int) -> UnlockReservation:
        """Take one attempt before deriving; UnlockAttemptsExhausted when none is left.

        The attempt stays taken unless `refund` is called, so a derivation that fails, or one
        that is cancelled, costs the session an attempt.
        """
        used = self._used.get(session_id, 0)
        if used >= self._limit:
            raise UnlockAttemptsExhausted
        self._used[session_id] = used + 1
        return UnlockReservation(lambda: self._release(session_id), self._limit - used - 1)

    def _release(self, session_id: int) -> None:
        used = self._used.get(session_id, 0) - 1
        if used > 0:
            self._used[session_id] = used
        else:
            self._used.pop(session_id, None)


_GATE: Final = UnlockGate()


def shared_unlock_gate() -> UnlockGate:
    """Return the instance's gate, shared by every importer."""
    return _GATE
