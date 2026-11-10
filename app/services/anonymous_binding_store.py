"""Holds a finished anonymous binding review for the user and environment that started it."""

import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from app.services.anonymous_binding_plan import AnonymousBindingReview

LIFETIME_SECONDS: Final = 1800.0
CAPACITY: Final = 16
PER_USER: Final = 2
_TOKEN_BYTES: Final = 16


@dataclass(slots=True)
class _Held:
    user_id: int
    environment_id: int
    review: AnonymousBindingReview
    expires: float
    applying: bool = False


class AnonymousBindingReviewStore:
    """In-memory reviews, each usable only by the user and environment that started it.

    A review expires 30 minutes after it was kept. Each user keeps at most two and the process
    sixteen; the oldest is dropped first.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._held: OrderedDict[str, _Held] = OrderedDict()

    def put(self, user_id: int, environment_id: int, review: AnonymousBindingReview) -> str:
        """Hold a review and return the token that names it."""
        self._expire()
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        self._held[token] = _Held(user_id, environment_id, review, self._clock() + LIFETIME_SECONDS)
        mine = [t for t, held in self._held.items() if held.user_id == user_id]
        for old in mine[: max(0, len(mine) - PER_USER)]:
            del self._held[old]
        while len(self._held) > CAPACITY:
            self._held.popitem(last=False)
        return token

    def get(self, token: str, user_id: int, environment_id: int) -> AnonymousBindingReview | None:
        """Return the review when the token is live and was made for exactly this."""
        held = self._find(token, user_id, environment_id)
        return None if held is None else held.review

    def replace(self, token: str, review: AnonymousBindingReview) -> None:
        """Keep a smaller review under the same token, or forget it when no group is left."""
        held = self._held.get(token)
        if held is None:
            return
        if review.groups:
            held.review = review
        else:
            del self._held[token]

    def begin_apply(self, token: str, user_id: int, environment_id: int) -> bool:
        """Mark the review as being applied; False when it is gone or already being applied."""
        held = self._find(token, user_id, environment_id)
        if held is None or held.applying:
            return False
        held.applying = True
        return True

    def end_apply(self, token: str) -> None:
        """Mark the review as no longer being applied."""
        held = self._held.get(token)
        if held is not None:
            held.applying = False

    def _find(self, token: str, user_id: int, environment_id: int) -> _Held | None:
        self._expire()
        held = self._held.get(token)
        if held is None or (held.user_id, held.environment_id) != (user_id, environment_id):
            return None
        return held

    def _expire(self) -> None:
        now = self._clock()
        for token in [t for t, held in self._held.items() if held.expires <= now]:
            del self._held[token]
