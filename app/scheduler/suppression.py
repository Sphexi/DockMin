"""Remembers when each object was last acted on, so automation can stand back for a while."""

from datetime import datetime, timedelta
from typing import cast, get_args

from app.db.engine import Database, Session
from app.db.repositories.activity_event import ActivityEventRepository
from app.db.repositories.clocked import Clock, format_timestamp, parse_timestamp
from app.db.repositories.resource_control import ResourceType, canonical_stack_key
from app.services.activity import SubsystemActor, UserActor
from app.services.object_locks import LockHolder, LockType, ObjectKey
from app.services.restart_holds import HEALTH_CHECKER

MAX_SUPPRESSION = timedelta(minutes=60)
MAX_SUPPRESSION_KEYS = 10_000

_LOCK_TYPES = frozenset((*get_args(ResourceType), "image", "image_reference", "node"))
LIFECYCLE_TYPES = frozenset({"container", "stack"})
LIFECYCLE_LOCK_ACTIONS = frozenset({"start", "stop", "restart", "recreate", "redeploy", "update"})
LIFECYCLE_ACTIONS = frozenset(
    {
        "container_started",
        "container_stopped",
        "container_restarted",
        "container_recreated",
        "container_update_rolled_back",
        "stack_started",
        "stack_stopped",
        "stack_restarted",
        "stack_redeployed",
        "stack_updated",
    }
)


class _Recent:
    """A bounded map from object to the time of the last action noted on it."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._last: dict[ObjectKey, datetime] = {}
        self.evicted_at: datetime | None = None

    def __len__(self) -> int:
        return len(self._last)

    def note(self, key: ObjectKey, at: datetime | None = None) -> None:
        moment = self._clock() if at is None else at
        previous = self._last.pop(key, None)
        self._last[key] = moment if previous is None or moment > previous else previous
        self._trim(self._clock())

    def get(self, key: ObjectKey) -> datetime | None:
        moment = self._last.get(key)
        if moment is None or self._clock() - moment > MAX_SUPPRESSION:
            return None
        return moment

    def evictions_possible(self) -> bool:
        return self.evicted_at is not None and self._clock() - self.evicted_at <= MAX_SUPPRESSION

    def _trim(self, now: datetime) -> None:
        while self._last:
            oldest = next(iter(self._last))
            if now - self._last[oldest] <= MAX_SUPPRESSION:
                break
            del self._last[oldest]
        while len(self._last) > MAX_SUPPRESSION_KEYS:
            del self._last[next(iter(self._last))]
            self.evicted_at = now


class SuppressionRegistry:
    """Bounded maps of when an object was last acted on by a user, and last restarted, started,
    stopped, redeployed or recreated by anyone but the container health checker.

    When the bound has pushed an entry out, a lookup that misses falls back to the activity log
    for as long as the evicted entry could still be inside the window.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._users = _Recent(clock)
        self._lifecycle = _Recent(clock)

    def __len__(self) -> int:
        return len(self._users)

    def observe(self, key: ObjectKey, holder: LockHolder) -> None:
        """Record a lock release; a user's counts, and so does a lifecycle action by anyone else."""
        actor = holder.actor
        if isinstance(actor, UserActor):
            self.note(key)
        if (
            key.object_type in LIFECYCLE_TYPES
            and holder.action in LIFECYCLE_LOCK_ACTIONS
            and not (isinstance(actor, SubsystemActor) and actor.name == HEALTH_CHECKER)
        ):
            self._lifecycle.note(key)

    def note(self, key: ObjectKey, at: datetime | None = None) -> None:
        """Record a user action on the object, now unless a time is given; a newer time stays."""
        self._users.note(key, at)

    def last_user_action(self, key: ObjectKey) -> datetime | None:
        """Return when a user last acted on the object, or None when that is outside the bound."""
        return self._users.get(key)

    async def lookup(self, key: ObjectKey, db: Session) -> datetime | None:
        """Return when a user last acted on the object within the bound, consulting the log.

        The log is read only after an entry has been evicted inside the last MAX_SUPPRESSION.
        """
        found = self._users.get(key)
        if found is not None or not self._users.evictions_possible():
            return found
        since = format_timestamp(self._clock() - MAX_SUPPRESSION)
        logged = await ActivityEventRepository(db).latest_user_action(
            key.environment_id, key.object_type, key.object_id, since
        )
        return None if logged is None else parse_timestamp(logged)

    async def lookup_lifecycle(self, key: ObjectKey, db: Session) -> datetime | None:
        """Return when a user acted on the object or a lifecycle action by anyone else ran on it."""
        user = await self.lookup(key, db)
        found = self._lifecycle.get(key)
        if found is None and self._lifecycle.evictions_possible():
            since = format_timestamp(self._clock() - MAX_SUPPRESSION)
            logged = await ActivityEventRepository(db).latest_lifecycle_action(
                key.environment_id,
                key.object_type,
                key.object_id,
                since,
                actions=LIFECYCLE_ACTIONS,
                excluding=HEALTH_CHECKER,
            )
            found = None if logged is None else parse_timestamp(logged)
        return max((moment for moment in (user, found) if moment is not None), default=None)

    async def seed(self, database: Database) -> int:
        """Load the last hour of the activity log; return how many user keys it set."""
        now = self._clock()
        since = format_timestamp(now - MAX_SUPPRESSION)
        async with database.session() as db:
            events = ActivityEventRepository(db)
            rows = await events.recent_user_targets(since, limit=MAX_SUPPRESSION_KEYS)
            lifecycle = await events.recent_lifecycle_targets(
                since,
                limit=MAX_SUPPRESSION_KEYS,
                actions=LIFECYCLE_ACTIONS,
                excluding=HEALTH_CHECKER,
            )
        if len(rows) >= MAX_SUPPRESSION_KEYS:
            self._users.evicted_at = now
        if len(lifecycle) >= MAX_SUPPRESSION_KEYS:
            self._lifecycle.evicted_at = now
        for environment_id, target_type, target_id, occurred_at in reversed(lifecycle):
            key = _key_of(environment_id, target_type, target_id)
            if key is not None:
                self._lifecycle.note(key, parse_timestamp(occurred_at))
        loaded = 0
        for environment_id, target_type, target_id, occurred_at in reversed(rows):
            key = _key_of(environment_id, target_type, target_id)
            if key is None:
                continue
            self.note(key, parse_timestamp(occurred_at))
            loaded += 1
        return loaded


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
