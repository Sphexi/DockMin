"""Holds the create form state an import produced until the form is opened and submitted."""

import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

__all__ = ["CAPACITY", "LIFETIME_SECONDS", "PreparedContainer", "PreparedContainerStore"]

LIFETIME_SECONDS: Final = 1800.0
CAPACITY: Final = 8
_ID_BYTES: Final = 16


@dataclass(frozen=True, slots=True)
class PreparedContainer:
    """Create form values made from an archive, usable only by the session that imported it.

    `values` holds every form field including environment values, some of them secret; it is
    held in memory only and never rendered outside the form's own fields.
    """

    id: str
    user_id: int
    session_id: int
    environment_id: int
    source_name: str
    exported: str
    values: dict[str, str] = field(repr=False)
    not_exported: tuple[tuple[str, tuple[str, ...]], ...]
    networks_created: tuple[str, ...]
    volumes_created: tuple[str, ...]
    environment_variables: int
    secret_values: int
    expires: float


class PreparedContainerStore:
    """In-memory holds on prepared form state: one per user and environment, eight in all."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._held: OrderedDict[str, PreparedContainer] = OrderedDict()

    @staticmethod
    def new_id() -> str:
        """Return a fresh id."""
        return secrets.token_hex(_ID_BYTES)

    def hold(self, prepared: PreparedContainer) -> None:
        """Hold the state, dropping the same user's earlier state for the environment."""
        self._expire()
        for token, other in list(self._held.items()):
            if (other.user_id, other.environment_id) == (
                prepared.user_id,
                prepared.environment_id,
            ):
                del self._held[token]
        self._held[prepared.id] = prepared
        while len(self._held) > CAPACITY:
            self._held.popitem(last=False)

    def get(
        self, prepared_id: str, *, user_id: int, session_id: int, environment_id: int
    ) -> PreparedContainer | None:
        """Return the state when it is live and was made by exactly this session and environment."""
        self._expire()
        found = self._held.get(prepared_id)
        if found is None or (found.user_id, found.session_id, found.environment_id) != (
            user_id,
            session_id,
            environment_id,
        ):
            return None
        return found

    def discard(
        self, prepared_id: str, *, user_id: int, session_id: int, environment_id: int
    ) -> None:
        """Drop the state when this session holds it."""
        if self.get(
            prepared_id, user_id=user_id, session_id=session_id, environment_id=environment_id
        ):
            del self._held[prepared_id]

    def clear(self) -> None:
        """Drop everything held."""
        self._held.clear()

    def _expire(self) -> None:
        now = self._clock()
        for token in [t for t, p in self._held.items() if p.expires <= now]:
            del self._held[token]

    def expiry(self) -> float:
        """Return the time at which state held now expires."""
        return self._clock() + LIFETIME_SECONDS
