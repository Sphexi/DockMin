"""Remembers when a user last acted on each object, so automation can stand back for a while."""

from datetime import datetime, timedelta
from typing import cast, get_args

from app.db.engine import Database, Session
from app.db.repositories.activity_event import ActivityEventRepository
from app.db.repositories.clocked import Clock, format_timestamp, parse_timestamp
from app.db.repositories.resource_control import ResourceType, canonical_stack_key
from app.services.activity import UserActor
from app.services.object_locks import LockHolder, LockType, ObjectKey

MAX_SUPPRESSION = timedelta(minutes=60)
MAX_SUPPRESSION_KEYS = 10_000

_LOCK_TYPES = frozenset((*get_args(ResourceType), "image", "image_reference", "node"))


class SuppressionRegistry:
    """Bounded map from object to the time of the last user action on it.

    When the bound has pushed an entry out, a lookup that misses falls back to the activity log
    for as long as the evicted entry could still be inside the window.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._last: dict[ObjectKey, datetime] = {}
        self._evicted_at: datetime | None = None

    def __len__(self) -> int:
        return len(self._last)

    def observe(self, key: ObjectKey, holder: LockHolder) -> None:
        """Record a lock release; only a user's counts."""
        if isinstance(holder.actor, UserActor):
            self.note(key)

    def note(self, key: ObjectKey, at: datetime | None = None) -> None:
        """Record a user action on the object, now unless a time is given; a newer time stays."""
        moment = self._clock() if at is None else at
        previous = self._last.pop(key, None)
        self._last[key] = moment if previous is None or moment > previous else previous
        self._trim(self._clock())

    def last_user_action(self, key: ObjectKey) -> datetime | None:
        """Return when a user last acted on the object, or None when that is outside the bound."""
        moment = self._last.get(key)
        if moment is None or self._clock() - moment > MAX_SUPPRESSION:
            return None
        return moment

    async def lookup(self, key: ObjectKey, db: Session) -> datetime | None:
        """Return when a user last acted on the object within the bound, consulting the log.

        The log is read only after an entry has been evicted inside the last MAX_SUPPRESSION.
        """
        found = self.last_user_action(key)
        if found is not None or not self._evictions_possible():
            return found
        since = format_timestamp(self._clock() - MAX_SUPPRESSION)
        logged = await ActivityEventRepository(db).latest_user_action(
            key.environment_id, key.object_type, key.object_id, since
        )
        return None if logged is None else parse_timestamp(logged)

    async def seed(self, database: Database) -> int:
        """Load user actions from the last hour of the activity log; return how many keys it set."""
        now = self._clock()
        since = format_timestamp(now - MAX_SUPPRESSION)
        async with database.session() as db:
            rows = await ActivityEventRepository(db).recent_user_targets(
                since, limit=MAX_SUPPRESSION_KEYS
            )
        if len(rows) >= MAX_SUPPRESSION_KEYS:
            self._evicted_at = now
        loaded = 0
        for environment_id, target_type, target_id, occurred_at in reversed(rows):
            key = _key_of(environment_id, target_type, target_id)
            if key is None:
                continue
            self.note(key, parse_timestamp(occurred_at))
            loaded += 1
        return loaded

    def _evictions_possible(self) -> bool:
        return self._evicted_at is not None and self._clock() - self._evicted_at <= MAX_SUPPRESSION

    def _trim(self, now: datetime) -> None:
        while self._last:
            oldest = next(iter(self._last))
            if now - self._last[oldest] <= MAX_SUPPRESSION:
                break
            del self._last[oldest]
        while len(self._last) > MAX_SUPPRESSION_KEYS:
            del self._last[next(iter(self._last))]
            self._evicted_at = now


def _key_of(
    environment_id: int | None, target_type: str, target_id: str | None
) -> ObjectKey | None:
    if environment_id is None or target_id is None or target_type not in _LOCK_TYPES:
        return None
    object_id = canonical_stack_key(target_id) if target_type == "stack" else target_id
    try:
        return ObjectKey(environment_id, cast(LockType, target_type), object_id)
    except ValueError:
        return None
