"""Holds a stack's files, read from a host, between showing them and adopting them."""

import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from app.db.repositories.stack import StackKind
from app.services.stack_read_result import HostReadResult

__all__ = ["TOKEN", "HostReadStash"]

TOKEN: Final = r"[A-Za-z0-9_-]{22}"
_LIFETIME_SECONDS: Final = 900.0
_CAPACITY: Final = 32
_PER_USER: Final = 8
_TOKEN_BYTES: Final = 16


@dataclass(frozen=True, slots=True)
class _Held:
    user_id: int
    environment_id: int
    kind: StackKind
    name: str
    result: HostReadResult
    expires: float


class HostReadStash:
    """In-memory results, each usable only by the user, environment and stack that read it.

    The oldest read of a user is dropped past eight, and the oldest of all past thirty-two.

    Secret file contents and secret variable values stay in this process and expire; nothing
    here is written to disk.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._held: OrderedDict[str, _Held] = OrderedDict()

    def put(
        self,
        user_id: int,
        environment_id: int,
        kind: StackKind,
        name: str,
        result: HostReadResult,
    ) -> str:
        """Hold a result and return the token that names it."""
        self._expire()
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        self._held[token] = _Held(
            user_id, environment_id, kind, name, result, self._clock() + _LIFETIME_SECONDS
        )
        mine = [t for t, held in self._held.items() if held.user_id == user_id]
        for old in mine[: max(0, len(mine) - _PER_USER)]:
            del self._held[old]
        while len(self._held) > _CAPACITY:
            self._held.popitem(last=False)
        return token

    def get(
        self, token: str, user_id: int, environment_id: int, kind: StackKind, name: str
    ) -> HostReadResult | None:
        """Return the held result when the token is live and was made for exactly this."""
        self._expire()
        held = self._held.get(token)
        if held is None or (held.user_id, held.environment_id, held.kind, held.name) != (
            user_id,
            environment_id,
            kind,
            name,
        ):
            return None
        return held.result

    def discard(self, token: str) -> None:
        """Forget a result."""
        self._held.pop(token, None)

    def _expire(self) -> None:
        now = self._clock()
        for token in [t for t, held in self._held.items() if held.expires <= now]:
            del self._held[token]
