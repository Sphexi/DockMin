"""What a multi-object run shares: the per-object tally and the guarded act on one object."""

import dataclasses
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Final, Literal

from app.auth.errors import (
    SELF_PROTECTED_REASON,
    PermissionDenied,
    ProductError,
    ResourceNotFound,
    SelfProtected,
)
from app.db.repositories.activity_event import ActivityEventRepository, DetailValue
from app.db.repositories.job_run import MAX_RUN_DETAIL_TEXT, is_reason
from app.logging import get_logger
from app.scheduler.model import RunFailed, RunResult, RunSkipped, reason_of, reason_or_fallback
from app.services.acting import AutomationAuthority
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectBusy, ObjectKey, ObjectLockRegistry
from app.subsystems.base import RunContext, suppression_skip

logger = get_logger(__name__)

type ObjectStatus = Literal[
    "done",
    "failed",
    "held",
    "released",
    "exhausted",
    "suppressed",
    "busy",
    "not_permitted",
    "skipped",
    "deferred",
]

ALL_OBJECTS_FAILED: Final = "all_objects_failed"
SOME_OBJECTS_FAILED: Final = "some_objects_failed"
PERMISSION_DENIED: Final = "permission_denied"
SUPPRESSED: Final = "suppressed"
_COUNTED: Final = (
    "failed",
    "held",
    "released",
    "exhausted",
    "suppressed",
    "busy",
    "not_permitted",
    "deferred",
)
_FAILURES: Final = frozenset({"failed", "exhausted"})
LISTING_LIMIT: Final = 256
NAMES_LISTING_LIMIT: Final = MAX_RUN_DETAIL_TEXT
_SIGNIFICANCE: Final = (
    "failed",
    "exhausted",
    "not_permitted",
    "busy",
    "suppressed",
    "deferred",
    "held",
    "released",
    "skipped",
    "done",
)
UNCHANGED: Final = "unchanged"
NOT_FOUND: Final = "not_found"


class ObjectTally:
    """Collects one status per object and turns them into a run's result.

    A run is `success` when no object failed, `partial` when some succeeded and some failed, and
    `failure` otherwise. Detail holds counts, and names only in a dry run's plan and, once
    `track_targets` is called, the names the rule's owner typed as its targets.
    """

    def __init__(self, *, done_name: str = "done", plan_name: str = "would_do") -> None:
        self._done_name = done_name
        self._plan_name = plan_name
        self._counts: Counter[str] = Counter()
        self._skips: Counter[str] = Counter()
        self._failure_reasons: list[str] = []
        self._planned: list[str] = []
        self._attempted = 0
        self._tracking = False
        self._current: str | None = None
        self._targets: list[str] = []
        self._not_found: list[str] = []
        self._outcomes: dict[str, tuple[str, str | None]] = {}

    def track_targets(self) -> None:
        """Report, in the result, which of the rule's named targets resolved and how each ended."""
        self._tracking = True

    def begin(self, name: str | None) -> None:
        """Attribute the statuses recorded from now on to the named target."""
        self._current = name

    def resolved(self, name: str) -> None:
        """Note that the named target resolved to an object."""
        self._targets.append(name)

    def missing(self, name: str) -> None:
        """Note that the named target resolved to nothing the rule's owner can see."""
        self._not_found.append(name)
        self._outcomes[name] = (NOT_FOUND, None)

    @property
    def targets_found(self) -> int:
        """Return how many named targets resolved so far."""
        return len(self._targets)

    @property
    def targets_missing(self) -> int:
        """Return how many named targets resolved to nothing so far."""
        return len(self._not_found)

    def note(self, status: str, reason: str | None = None) -> None:
        """Attribute a status to the current target, keeping the most significant one."""
        name = self._current
        if name is None:
            return
        held = self._outcomes.get(name)
        if held is not None and held[0] == NOT_FOUND:
            return
        if held is None or _SIGNIFICANCE.index(status) < _SIGNIFICANCE.index(held[0]):
            self._outcomes[name] = (status, reason)

    def record(
        self,
        status: ObjectStatus,
        *,
        attempted: bool,
        name: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Count one object. `attempted` is true when the object's action wrote an event."""
        if attempted:
            self._attempted += 1
        self.note(status, reason)
        if status == "skipped":
            self._skips[reason_or_fallback(reason) if reason else "skipped"] += 1
            return
        self._counts[status] += 1
        if status in _FAILURES:
            self._failure_reasons.append(reason_or_fallback(reason))
        if status == "done" and name is not None:
            self._planned.append(name)

    def result(self, *, dry_run: bool, extra: Mapping[str, DetailValue] | None = None) -> RunResult:
        """Build the run's result; a dry run reports its plan and always succeeds."""
        detail: dict[str, DetailValue] = {}
        done = self._counts["done"]
        if dry_run:
            detail[f"{self._plan_name}_count"] = done
            detail[self._plan_name] = listing(sorted(self._planned))
        else:
            detail[self._done_name] = done
        detail.update({key: self._counts[key] for key in _COUNTED if self._counts[key]})
        detail.update(self._skips)
        if self._tracking:
            detail.update(self._target_detail())
        detail.update(extra or {})
        failed = self._counts["failed"] + self._counts["exhausted"]
        if dry_run or failed == 0:
            return RunResult("success", attempted=self._attempted, detail=detail)
        if done:
            return RunResult(
                "partial",
                attempted=self._attempted,
                reason=self._failure_reasons[0] if failed == 1 else SOME_OBJECTS_FAILED,
                message="Some objects could not be acted on.",
                detail=detail,
            )
        reason = self._failure_reasons[0] if failed == 1 else ALL_OBJECTS_FAILED
        return RunResult(
            "failure",
            attempted=self._attempted,
            reason=reason,
            message="The object could not be acted on."
            if failed == 1
            else "No object could be acted on.",
            detail=detail,
        )

    def _target_detail(self) -> dict[str, DetailValue]:
        detail: dict[str, DetailValue] = {"targets": listing(self._targets, NAMES_LISTING_LIMIT)}
        outcomes = []
        for name in self._targets:
            status, reason = self._outcomes.get(name, (UNCHANGED, None))
            outcomes.append(f"{name}: {status}" + (f" ({reason})" if reason else ""))
        detail["target_outcomes"] = listing(outcomes, NAMES_LISTING_LIMIT)
        if self._not_found:
            detail["not_found"] = len(self._not_found)
            detail["not_found_names"] = listing(self._not_found, NAMES_LISTING_LIMIT)
        return detail


async def check_authorised(
    context: RunContext,
    key: ObjectKey,
    *,
    explicit: bool,
    tally: ObjectTally | None = None,
    name: str | None = None,
) -> bool:
    """Report whether the rule's owner may write to the object, recording a refusal in the tally.

    `explicit` marks a target the rule names, whose denial is a failure; a dynamic match the
    owner may not touch is `not_permitted` instead. Without a tally an unreachable environment
    or an object too large to read propagates; with one it fails that object alone.
    """
    return await _refusal_of(context, key, explicit, tally, name) is None


async def _refusal_of(
    context: RunContext,
    key: ObjectKey,
    explicit: bool,
    tally: ObjectTally | None,
    name: str | None,
) -> ObjectStatus | None:
    """Return the status an unauthorised object is settled as, or None when the owner may act."""
    try:
        await context.authorise_object(key)
    except (PermissionDenied, ResourceNotFound):
        record_unwritable(tally, explicit=explicit, name=name)
        return "failed" if explicit else "not_permitted"
    except (EnvironmentUnavailable, ObjectTooLarge) as error:
        if tally is None:
            raise
        _settle(tally, "failed", False, name, failure_reason(error))
        return "failed"
    return None


def record_unwritable(tally: ObjectTally | None, *, explicit: bool, name: str | None) -> None:
    """Record an object the owner may not change: a failure if the rule names it, else a count."""
    if explicit:
        _settle(tally, "failed", False, name, PERMISSION_DENIED)
    else:
        _settle(tally, "not_permitted", False, name, None)


def forecast_busy(locks: ObjectLockRegistry, keys: Sequence[ObjectKey], *, single: bool) -> bool:
    """Report whether any object is locked now, for a dry run that takes no lock.

    A single-object run raises ObjectBusy instead, so it is recorded the way a real run would be.
    """
    for key in keys:
        holder = locks.holder_of(key)
        if holder is not None:
            if single:
                raise ObjectBusy(holder)
            return True
    return False


async def permitted(context: RunContext, *keys: ObjectKey) -> bool:
    """Report whether the rule's owner may write to every object, stopping at the first refusal."""
    for key in keys:
        if not await check_authorised(context, key, explicit=True):
            return False
    return True


def rechecking(
    context: RunContext, authority: AutomationAuthority, members: Sequence[ObjectKey]
) -> AutomationAuthority:
    """Return the authority with `hold` re-checking suppression of each member once locked."""
    hold = authority.hold

    @asynccontextmanager
    async def hold_then_recheck(key: ObjectKey, action: str) -> AsyncIterator[None]:
        async with hold(key, action):
            for member in members:
                await check_suppressed(context, member, single=True)
            yield

    return dataclasses.replace(authority, hold=hold_then_recheck)


async def check_suppressed(
    context: RunContext,
    key: ObjectKey,
    *,
    single: bool,
    tally: ObjectTally | None = None,
    name: str | None = None,
) -> bool:
    """Report whether a user acted on the object inside the window, recording it in the tally.

    With `single` set the suppression raises RunSkipped instead, so the run is recorded as
    skipped with the time of the user's action.
    """
    acted_at = await context.suppressed(key)
    if acted_at is None:
        return False
    if single:
        raise suppression_skip(acted_at)
    _settle(tally, "suppressed", False, name, None)
    return True


async def act_on(
    context: RunContext,
    key: ObjectKey,
    *,
    explicit: bool,
    work: Callable[[AutomationAuthority], Awaitable[None]],
    tally: ObjectTally | None = None,
    name: str | None = None,
    single: bool = False,
    on_success: ObjectStatus = "done",
    success_reason: str | None = None,
) -> ObjectStatus:
    """Act on one object the way a run must: owner check, suppression, then the work.

    With `single` set, a busy object or a suppression raised while waiting for the lock escapes,
    so the run is recorded as skipped with its detail; without it they are statuses. The status
    is also recorded in `tally` when given. `on_success` is the status of work that returned,
    with `success_reason` as its reason when that status is a failure.
    """
    refused = await _refusal_of(context, key, explicit, tally, name)
    if refused is not None:
        return refused
    if await check_suppressed(context, key, single=single, tally=tally, name=name):
        return "suppressed"
    events = ActivityEventRepository(context.db)
    run_id = context.dispatch.run.id
    before = await events.count_for_run(run_id)
    status: ObjectStatus
    reason: str | None = None
    try:
        await work(context.authority())
    except ObjectBusy:
        if single:
            raise
        return _settle(tally, "busy", False, name, None)
    except RunSkipped as skipped:
        if single or skipped.reason != SUPPRESSED:
            raise
        return _settle(tally, "suppressed", False, name, None)
    except RunFailed:
        raise
    except SelfProtected:
        status, reason = "skipped", SELF_PROTECTED_REASON
    except ProductError as error:
        status, reason = "failed", failure_reason(error)
    except Exception as error:
        logger.error(
            "subsystem_object_failed",
            extra={"subsystem": context.actor.name, "error": type(error).__name__},
        )
        status, reason = "failed", reason_of(error)
    else:
        status, reason = on_success, success_reason
    attempted = await events.count_for_run(run_id) > before
    return _settle(tally, status, attempted, name, reason)


def failure_reason(error: ProductError) -> str:
    """Return the reason a product error states, else one derived from its class name."""
    stated = getattr(error, "reason", None)
    return stated if isinstance(stated, str) and is_reason(stated) else reason_of(error)


def _settle(
    tally: ObjectTally | None,
    status: ObjectStatus,
    attempted: bool,
    name: str | None,
    reason: str | None,
) -> ObjectStatus:
    if tally is not None:
        tally.record(status, attempted=attempted, name=name, reason=reason)
    return status


def listing(names: Sequence[str], limit: int = LISTING_LIMIT, separator: str = ", ") -> str:
    """Join names for a run's detail, cut to `limit` characters with a count of the rest."""
    text = separator.join(names)
    if len(text) <= limit:
        return text
    kept: list[str] = []
    for index, name in enumerate(names):
        suffix = f" and {len(names) - index} more"
        if len(separator.join([*kept, name])) + len(suffix) > limit:
            break
        kept.append(name)
    return f"{separator.join(kept)} and {len(names) - len(kept)} more".strip()
