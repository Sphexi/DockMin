"""The scheduler: one loop that claims due jobs, runs them as tasks, and records what happened."""

import asyncio
import contextvars
import itertools
import math
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
from typing import Literal, Protocol

from app.auth.errors import ProductError
from app.db.engine import Database, Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.clocked import Clock, format_timestamp, parse_timestamp
from app.db.repositories.errors import RepositoryError
from app.db.repositories.job_run import (
    JobRun,
    JobRunRepository,
    RunOutcome,
    RunTrigger,
    is_reason,
)
from app.db.repositories.scheduled_job import (
    OverrunPolicy,
    ScheduledJob,
    ScheduledJobRepository,
    ScheduleKind,
)
from app.db.repositories.setting import SettingRepository
from app.logging import get_logger
from app.notifications.catalogue import (
    AUTOMATION_RUN_FAILED_ACTION,
    AUTOMATION_RUN_PARTIAL_ACTION,
)
from app.scheduler.events import rule_detail
from app.scheduler.gates import job_gates, moment_of, next_run_text, stored_of
from app.scheduler.housekeeping import Housekeeping
from app.scheduler.model import (
    DetailInvalid,
    Dispatch,
    JobHandler,
    RunFailed,
    RunResult,
    RunSkipped,
    reason_of,
    reason_or_fallback,
    setting_means_enabled,
    subsystem_setting_key,
)
from app.scheduler.policy import (
    CANCEL_PREVIOUS_WAIT,
    Gates,
    backoff_delay,
    due_decision,
    failures_after,
    gates_open,
    missed_count,
    next_run_at,
    off_grid,
    overrun_decision,
    reached_threshold,
    retry_at,
)
from app.scheduler.recovery import record_interruption, recover
from app.scheduler.registry import HandlerRegistry
from app.scheduler.schedules import (
    IntervalSchedule,
    Schedule,
    ScheduleInvalid,
    floor_seconds,
    next_grid_point,
    parse_schedule,
)
from app.services.activity import (
    SUBSYSTEMS,
    UNSCHEDULED_SUBSYSTEMS,
    ActivityRecorder,
    SubsystemActor,
    completed,
)
from app.services.activity_log import RetentionPolicy

logger = get_logger(__name__)

MAX_CONCURRENT_RUNS = 8
RESERVED_RULE_LESS_SLOTS = 1
MAX_RUNS_PER_OWNER = 2
SERVE_FAILURES_BEFORE_UNSCHEDULE = 3
HEARTBEAT = timedelta(seconds=30)
MAX_SLEEP = timedelta(seconds=60)
SHUTDOWN_WAIT = timedelta(seconds=10)
LOOP_EXIT_WAIT = timedelta(seconds=1)
ERROR_PAUSE = timedelta(seconds=5)
DUE_BATCH = 64
LAST_RUN_REWRITE = timedelta(hours=1)

DISABLED_ACTION = "scheduled_job_disabled"
MISSED_MESSAGE = "The application was not running at the scheduled time."
OVERRUN_MESSAGE = "The previous run was still running."
COLLAPSED_MESSAGE = "A run was already queued behind the running one."
DROPPED_MESSAGE = "The queued run was dropped because the job can no longer run."
NOT_STOPPED_MESSAGE = "The previous run did not stop in time."
STOPPING_MESSAGE = "The scheduler was not accepting runs."
DEADLINE_MESSAGE = "The run took longer than its time limit."
UNKNOWN_OUTCOME_MESSAGE = (
    "The run took longer than its time limit and was cancelled; what it changed is not known."
)
CAPACITY_MESSAGE = "No run slot was free when the run was due."
SETTINGS_DROPPED_MESSAGE = "The queued run was dropped because the job's settings changed."
DETAIL_INVALID_MESSAGE = "The run reported detail that cannot be stored."
UNSETTLED_MESSAGE = "The run's result could not be recorded."
DRY_RUN_UNSUPPORTED_MESSAGE = "This job does not support a dry run."
UNAVAILABLE_MESSAGE = "No handler is registered for this job's subsystem."

type Served = Literal["started", "recorded", "deferred"]


class Sleeper(Protocol):
    """Waits for a deadline or a wake-up, whichever comes first."""

    async def sleep_until(self, deadline: datetime | None, wake: asyncio.Event) -> None:
        """Return at the deadline, when the event is set, or after the longest allowed sleep."""
        ...


class MonotonicSleeper:
    """Sleeps on the event loop's clock for the wall-clock distance to the deadline."""

    def __init__(self, clock: Clock, cap: timedelta = MAX_SLEEP) -> None:
        self._clock = clock
        self._cap = cap

    async def sleep_until(self, deadline: datetime | None, wake: asyncio.Event) -> None:
        """Return at the deadline, when the event is set, or after the cap."""
        seconds = self._cap.total_seconds()
        if deadline is not None:
            seconds = min(seconds, (deadline - self._clock()).total_seconds())
        if seconds <= 0 or wake.is_set():
            return
        try:
            async with asyncio.timeout(seconds):
                await wake.wait()
        except TimeoutError:
            return


class SchedulerBusy(ProductError):
    """The scheduler cannot take the run now; the message is fixed text."""


@dataclass(frozen=True, slots=True)
class UnrecordedRun:
    """The start time and outcome of the latest run of a job that left no job_run row."""

    started_at: str
    outcome: str


@dataclass(eq=False, slots=True)
class _Active:
    job_id: int
    run: JobRun
    run_id: int
    rule_bearing: bool
    owner_id: int | None
    subsystem: str | None = None
    task: asyncio.Task[None] | None = None
    cancel_reason: str | None = None
    recording: asyncio.Lock = field(default_factory=asyncio.Lock)
    recorded: bool = False


@dataclass(frozen=True, slots=True)
class _Outcome:
    outcome: RunOutcome
    reason: str | None = None
    message: str | None = None
    detail: dict[str, DetailValue] | None = None


@dataclass(frozen=True, slots=True)
class _Skip:
    reason: str
    message: str
    trigger: RunTrigger
    detail: dict[str, DetailValue]


@dataclass(frozen=True, slots=True)
class _Claim:
    claimed: bool
    run: JobRun | None = None


def _running_detail(running: _Active | None) -> dict[str, DetailValue]:
    if running is None or running.run_id < 0:
        return {}
    return {"running_run_id": running.run_id}


def _stale(job: ScheduledJob, run: JobRun) -> bool:
    if job.last_run_at is None or job.last_run_outcome != "success":
        return True
    return parse_timestamp(run.started_at) - parse_timestamp(job.last_run_at) >= LAST_RUN_REWRITE


def _result_outcome(result: RunResult) -> _Outcome:
    detail = dict(result.detail) or None
    match result.outcome:
        case "failure" | "skipped":
            return _Outcome(
                result.outcome, reason_or_fallback(result.reason), result.message, detail
            )
        case _:
            reason = (
                result.reason if result.reason is not None and is_reason(result.reason) else None
            )
            return _Outcome(result.outcome, reason, result.message, detail)


class Scheduler:
    """Fires scheduled jobs on time and records every run.

    Single instance per data directory. A job's next_run_at is null whenever any gate outside
    the schedule forbids it, and whoever changes a gate rewrites it.
    """

    def __init__(
        self,
        database: Database,
        handlers: HandlerRegistry,
        *,
        clock: Clock,
        sleeper: Sleeper | None = None,
        rng: Callable[[], float] = random.random,
        retention: RetentionPolicy | None = None,
        development_checks: bool = False,
        subsystem_enabled: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._database = database
        self._handlers = handlers
        self._clock = clock
        self._sleeper: Sleeper = MonotonicSleeper(clock) if sleeper is None else sleeper
        self._rng = rng
        self._development_checks = development_checks
        self._subsystem_enabled = subsystem_enabled
        self._housekeeping = Housekeeping(database, clock=clock, retention=retention)
        self._wake = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._active: dict[int, _Active] = {}
        self._unrecorded: dict[int, UnrecordedRun] = {}
        self._unrecorded_ids = itertools.count(-1, -1)
        self._queued: dict[int, datetime] = {}
        self._dropped: dict[int, datetime] = {}
        self._capacity_deferred: set[int] = set()
        self._serve_failures: dict[int, int] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._loop_task: asyncio.Task[None] | None = None
        self._closing = False
        self._suspended = 0
        self._deferred = False
        self._errored = False
        self._last_beat = clock()

    @property
    def housekeeping(self) -> Housekeeping:
        """Return the hourly upkeep, so a caller can run a pass on demand."""
        return self._housekeeping

    def wake(self) -> None:
        """Tell the scheduler a gate or schedule changed: drop queued fires and look at the jobs.

        Call after the change commits. A dropped fire is recorded as skipped on the next tick.
        """
        for job_id, fire in self._queued.items():
            self._dropped[job_id] = fire
        self._queued.clear()
        self._wake.set()

    def poke(self) -> None:
        """Tell the scheduler a job was armed: look at the jobs again, keeping queued fires.

        Call after the arming transaction commits.
        """
        self._wake.set()

    async def expedite(self, job_id: int) -> bool:
        """Bring a job's next fire forward to now; report whether it moved.

        Every gate is checked in the statement that moves the fire: a disabled, paused, gated or
        backing-off job stays as it is. A job that is running, or a scheduler that is closing or
        suspended, is left alone. Fires already queued behind a running job are kept.
        """
        if self._closing or self._suspended or self.running(job_id):
            return False

        async def attempt() -> bool:
            async with self._database.session() as db:
                jobs = ScheduledJobRepository(db, self._clock)
                async with db.transaction():
                    return await jobs.bring_forward(job_id, format_timestamp(self._clock()))

        moved = await retry_busy(attempt)
        if moved:
            self._wake.set()
        return moved

    def unrecorded_run(self, job_id: int) -> UnrecordedRun | None:
        """Return the latest run of the job that left no row, as held since the process started."""
        return self._unrecorded.get(job_id)

    def running(self, job_id: int) -> bool:
        """Report whether a run of the job is in flight in this process."""
        return self._active_for(job_id) is not None

    def require_idle(
        self,
        job_id: int,
        *,
        rule_bearing: bool = False,
        owner_id: int | None = None,
        subsystem: str | None = None,
    ) -> None:
        """Raise SchedulerBusy unless a run of the job could start now.

        A manual run takes a slot like a scheduled one, so the slot limits apply to it.
        """
        if self._closing or self._suspended:
            raise SchedulerBusy("The scheduler is not accepting runs right now.")
        if self.running(job_id):
            raise SchedulerBusy("This job is already running.")
        match self._room(rule_bearing=rule_bearing, owner_id=owner_id, subsystem=subsystem):
            case "capacity":
                raise SchedulerBusy("No run slot is free right now. Try again shortly.")
            case "owner_limit":
                raise SchedulerBusy(
                    "The automation's owner already has the most runs allowed at once."
                )
            case "subsystem_limit":
                raise SchedulerBusy("This subsystem already has the most runs allowed at once.")
            case _:
                return

    async def ensure_infrastructure_job(
        self,
        subsystem: str,
        *,
        kind: ScheduleKind,
        expression: str,
        catch_up: bool,
        overrun_policy: OverrunPolicy,
        failure_threshold: int | None = None,
    ) -> ScheduledJob:
        """Create or bring up to date the one job of a subsystem that has no rules.

        An interval fires on whole multiples of its length in Unix time. A change keeps a fire that
        is already due and honours a pause in force; a stored future fire off that grid moves to
        the next grid point.
        """
        if SUBSYSTEMS.get(subsystem) is not False or subsystem in UNSCHEDULED_SUBSYSTEMS:
            raise ValueError(f"{subsystem} is not a subsystem without rules")
        schedule = parse_schedule(kind, expression, "UTC", min_interval=1)
        now = self._clock()
        available = self._handlers.get(subsystem) is not None
        first_fire = (
            next_grid_point(now, schedule.seconds)
            if isinstance(schedule, IntervalSchedule)
            else schedule.next_after(now)
        )
        async with self._database.session() as db:
            jobs = ScheduledJobRepository(db, self._clock)
            async with db.transaction():
                enabled = await self._is_enabled(db, subsystem)
                existing = await jobs.get_infrastructure(subsystem)
                if existing is None:
                    first = now if catch_up else first_fire
                    return await jobs.insert(
                        subsystem=subsystem,
                        rule_id=None,
                        schedule_kind=kind,
                        schedule_expression=expression,
                        time_zone="UTC",
                        overrun_policy=overrun_policy,
                        catch_up=catch_up,
                        failure_threshold=failure_threshold,
                        next_run_at=stored_of(first) if enabled and available else None,
                    )
                unchanged = (
                    existing.schedule_kind == kind
                    and existing.schedule_expression == expression
                    and existing.overrun_policy == overrun_policy
                    and existing.catch_up == catch_up
                    and existing.failure_threshold == failure_threshold
                )
                needs_fire = existing.is_enabled and enabled and existing.next_run_at is None
                pending = moment_of(existing.next_run_at)
                misaligned = pending is not None and pending > now and off_grid(schedule, pending)
                if unchanged and not needs_fire and not misaligned:
                    return existing
                changed = replace(
                    existing,
                    schedule_kind=kind,
                    schedule_expression=expression,
                    overrun_policy=overrun_policy,
                    catch_up=catch_up,
                    failure_threshold=failure_threshold,
                    next_run_at=None,
                )
                await jobs.update_schedule(
                    existing.id,
                    schedule_kind=kind,
                    schedule_expression=expression,
                    time_zone="UTC",
                    overrun_policy=overrun_policy,
                    catch_up=catch_up,
                    failure_threshold=failure_threshold,
                    next_run_at=self._kept_or_recomputed(
                        existing, changed, enabled=enabled, available=available, now=now
                    ),
                )
                return await self._reloaded(jobs, existing.id)

    async def start(self) -> None:
        """Sweep what the last process left behind, serve missed fires, then start the loop."""
        if self._loop_task is not None:
            raise RuntimeError("the scheduler is already started")
        report = await recover(
            self._database, self._handlers, clock=self._clock, subsystem_enabled=self._enabled
        )
        logger.info(
            "scheduler_recovered",
            extra={
                "interrupted": report.interrupted,
                "gated": report.gated,
                "revived": report.revived,
            },
        )
        self._last_beat = self._clock()
        await self.tick()
        self._loop_task = asyncio.create_task(self._loop(), name="scheduler-loop")

    async def aclose(self) -> None:
        """Stop the loop and interrupt every run in flight, waiting at most ten seconds for them."""
        if self._closing:
            return
        self._closing = True
        self._wake.set()
        try:
            if self._loop_task is not None:
                done, _ = await asyncio.wait(
                    {self._loop_task}, timeout=LOOP_EXIT_WAIT.total_seconds()
                )
                if not done:
                    self._loop_task.cancel()
                    await asyncio.gather(self._loop_task, return_exceptions=True)
        finally:
            async with self._tick_lock:
                await self._interrupt_all("shutdown")

    @asynccontextmanager
    async def suspend(self) -> AsyncIterator[None]:
        """Stop claiming and interrupt runs in flight until the block exits, failure included.

        A tick already under way finishes the job it is on, launches nothing more, and is waited
        for before the runs are interrupted.
        """
        self._suspended += 1
        try:
            async with self._tick_lock:
                await self._interrupt_all("suspended")
            yield
        finally:
            self._suspended -= 1
            self._wake.set()

    def start_claimed(
        self,
        run: JobRun,
        *,
        dry_run: bool = False,
        rule_bearing: bool = False,
        owner_id: int | None = None,
        subsystem: str | None = None,
    ) -> None:
        """Run a run row that was inserted and committed outside the loop."""
        self._launch(
            run,
            None,
            dry_run=dry_run,
            external=True,
            rule_bearing=rule_bearing,
            owner_id=owner_id,
            subsystem=subsystem,
        )

    async def tick(self) -> int:
        """Claim and start every due job that has a free slot; return how many runs started."""
        if self._closing or self._suspended:
            return 0
        async with self._tick_lock:
            if self._closing or self._suspended:
                return 0
            return await self._tick_locked()

    async def beat(self) -> None:
        """Stamp the heartbeat of every run in flight."""
        self._last_beat = self._clock()
        run_ids = [active.run_id for active in self._active.values() if active.run_id > 0]
        if not run_ids:
            return
        async with self._database.session() as db:
            await JobRunRepository(db, self._clock).heartbeat(run_ids)

    async def idle(self) -> None:
        """Wait until no run is in flight, including runs those runs start."""
        while self._tasks:
            await asyncio.wait(list(self._tasks))

    def _active_for(self, job_id: int) -> _Active | None:
        for active in reversed(self._active.values()):
            if active.job_id == job_id:
                return active
        return None

    def _room(
        self, *, rule_bearing: bool, owner_id: int | None, subsystem: str | None = None
    ) -> Literal["capacity", "owner_limit", "subsystem_limit"] | None:
        limit = MAX_CONCURRENT_RUNS - (RESERVED_RULE_LESS_SLOTS if rule_bearing else 0)
        if len(self._active) >= limit:
            return "capacity"
        if owner_id is not None:
            held = sum(1 for active in self._active.values() if active.owner_id == owner_id)
            if held >= MAX_RUNS_PER_OWNER:
                return "owner_limit"
        handler = None if subsystem is None else self._handlers.get(subsystem)
        cap = None if handler is None else handler.max_concurrent_runs
        if cap is not None:
            running = sum(1 for active in self._active.values() if active.subsystem == subsystem)
            if running >= cap:
                return "subsystem_limit"
        return None

    def _kept_or_recomputed(
        self,
        existing: ScheduledJob,
        changed: ScheduledJob,
        *,
        enabled: bool,
        available: bool,
        now: datetime,
    ) -> str | None:
        if not (existing.is_enabled and enabled and available):
            return None
        pending = moment_of(existing.next_run_at)
        paused = moment_of(existing.paused_until)
        if pending is not None and pending <= now and (paused is None or paused <= now):
            return existing.next_run_at
        return next_run_text(
            changed, None, subsystem_enabled=enabled, available=available, now=now, rng=self._rng
        )

    async def _reloaded(self, jobs: ScheduledJobRepository, job_id: int) -> ScheduledJob:
        job = await jobs.get(job_id)
        if job is None:
            raise RuntimeError("a job written by this call could not be read back")
        return job

    async def _enabled(self, name: str) -> bool:
        async with self._database.session() as db:
            return await self._is_enabled(db, name)

    async def _is_enabled(self, db: Session, name: str) -> bool:
        if self._subsystem_enabled is not None:
            return await self._subsystem_enabled(name)
        setting = await SettingRepository(db, self._clock).get(subsystem_setting_key(name))
        return setting_means_enabled(None if setting is None else setting.value)

    async def _loop(self) -> None:
        while not self._closing:
            self._wake.clear()
            try:
                await self.tick()
                await self._maintain()
                deadline = await self._deadline()
            except Exception as error:
                logger.error("scheduler_tick_failed", extra={"error": type(error).__name__})
                await self._pause()
                continue
            if self._errored:
                await self._pause()
                continue
            await self._sleeper.sleep_until(deadline, self._wake)

    async def _pause(self) -> None:
        await self._sleeper.sleep_until(self._clock() + ERROR_PAUSE, asyncio.Event())

    async def _maintain(self) -> None:
        if self._active and self._clock() - self._last_beat >= HEARTBEAT:
            await self.beat()
        await self._housekeeping.run_if_due()

    async def _deadline(self) -> datetime | None:
        if self._suspended:
            return None
        now = self._clock()
        async with self._database.session() as db:
            earliest = await ScheduledJobRepository(db, self._clock).earliest_next_run(
                after=format_timestamp(now) if self._deferred else None
            )
        deadline = None if earliest is None else parse_timestamp(earliest)
        if self._active:
            beat = self._last_beat + HEARTBEAT
            deadline = beat if deadline is None else min(deadline, beat)
        upkeep = self._housekeeping.next_due
        if upkeep is not None:
            deadline = upkeep if deadline is None else min(deadline, upkeep)
        return deadline

    async def _tick_locked(self) -> int:
        started = 0
        self._deferred = False
        self._errored = False
        async with self._database.session() as db:
            await self._record_dropped(db)
            jobs = ScheduledJobRepository(db, self._clock)
            after: tuple[str, int] | None = None
            while True:
                now = self._clock()
                due = await jobs.due(format_timestamp(now), limit=DUE_BATCH, after=after)
                if not due:
                    return started
                after = (str(due[-1].next_run_at), due[-1].id)
                for job in due:
                    if self._closing or self._suspended:
                        return started
                    try:
                        served = await self._serve(db, job, now)
                    except Exception as error:
                        self._errored = True
                        await self._serve_failed(db, job, error)
                        continue
                    self._serve_failures.pop(job.id, None)
                    if served == "started":
                        started += 1
                    elif served == "deferred":
                        self._deferred = True

    async def _serve_failed(self, db: Session, job: ScheduledJob, error: Exception) -> None:
        failures = self._serve_failures.get(job.id, 0) + 1
        self._serve_failures[job.id] = failures
        logger.error(
            "scheduler_job_failed", extra={"job_id": job.id, "error": type(error).__name__}
        )
        if failures < SERVE_FAILURES_BEFORE_UNSCHEDULE:
            return
        self._serve_failures.pop(job.id, None)
        try:
            await ScheduledJobRepository(db, self._clock).set_next_run(job.id, None)
        except Exception as unscheduled:
            logger.error(
                "scheduler_job_unschedule_failed",
                extra={"job_id": job.id, "error": type(unscheduled).__name__},
            )
            return
        logger.error("scheduler_job_unscheduled", extra={"job_id": job.id})

    async def _record_dropped(self, db: Session) -> None:
        dropped, self._dropped = self._dropped, {}
        runs = JobRunRepository(db, self._clock)
        for job_id, fire in dropped.items():
            try:
                async with db.transaction():
                    await runs.record_skipped(
                        job_id,
                        trigger="queued",
                        scheduled_for=stored_of(fire),
                        reason="queue_dropped",
                        message=SETTINGS_DROPPED_MESSAGE,
                    )
            except Exception as error:
                logger.error(
                    "scheduler_dropped_fire_unrecorded",
                    extra={"job_id": job_id, "error": type(error).__name__},
                )

    async def _serve(self, db: Session, job: ScheduledJob, now: datetime) -> Served:
        if job.next_run_at is None:
            return "recorded"
        due = parse_timestamp(job.next_run_at)
        handler = self._handlers.get(job.subsystem)
        enabled = await self._is_enabled(db, job.subsystem)
        schedule = self._schedule_of(job)
        if handler is None or not enabled or schedule is None:
            await self._commit(db, job, None)
            return "recorded"
        if job.schedule_kind == "webhook":
            return await self._serve_called(db, job)
        following = self._following(job, schedule, previous=due, now=now)
        decision = due_decision(due, now, catch_up=job.catch_up)
        if decision == "skip_missed":
            count = await asyncio.to_thread(missed_count, schedule, due, now)
            waited = job.id in self._capacity_deferred
            skip = _Skip(
                "capacity" if waited else "missed",
                CAPACITY_MESSAGE if waited else MISSED_MESSAGE,
                "schedule",
                {"first_missed": job.next_run_at, "missed_count": count},
            )
            await self._commit(db, job, following, skip=skip)
            self._capacity_deferred.discard(job.id)
            return "recorded"
        trigger: RunTrigger = "schedule" if decision == "run" else "catch_up"
        running = self._active_for(job.id)
        action = overrun_decision(
            job.overrun_policy,
            running=running is not None,
            queued=job.id in self._queued,
            cancellation_safe=handler.cancellation_safe,
        )
        detail = _running_detail(running)
        match action:
            case "start":
                owner_id = await self._owner_of(db, job)
                room = self._room(
                    rule_bearing=job.rule_id is not None,
                    owner_id=owner_id,
                    subsystem=job.subsystem,
                )
                if room is not None:
                    self._capacity_deferred.add(job.id)
                    return "deferred"
                claim = await self._commit(db, job, following, start=trigger)
                self._capacity_deferred.discard(job.id)
                if claim.run is None:
                    return "recorded"
                if self._closing or self._suspended:
                    await self._abandon(db, claim.run)
                    return "recorded"
                self._launch(claim.run, job, owner_id=owner_id)
                return "started"
            case "cancel_previous":
                claim = await self._commit(db, job, following, start=trigger)
                if claim.run is None:
                    return "recorded"
                self._launch(
                    claim.run, job, supersede=running, owner_id=await self._owner_of(db, job)
                )
                return "started"
            case "queue":
                claim = await self._commit(db, job, following)
                if claim.claimed:
                    self._queued[job.id] = due
                return "recorded"
            case "collapse":
                skip = _Skip("overrun_collapsed", COLLAPSED_MESSAGE, trigger, detail)
            case _:
                skip = _Skip("overrun", OVERRUN_MESSAGE, trigger, detail)
        await self._commit(db, job, following, skip=skip)
        return "recorded"

    async def _serve_called(self, db: Session, job: ScheduledJob) -> Served:
        if self._active_for(job.id) is not None:
            self._capacity_deferred.add(job.id)
            return "deferred"
        owner_id = await self._owner_of(db, job)
        room = self._room(
            rule_bearing=job.rule_id is not None, owner_id=owner_id, subsystem=job.subsystem
        )
        if room is not None:
            self._capacity_deferred.add(job.id)
            return "deferred"
        claim = await self._commit(db, job, None, start="webhook")
        self._capacity_deferred.discard(job.id)
        if claim.run is None:
            return "recorded"
        if self._closing or self._suspended:
            await self._abandon(db, claim.run)
            return "recorded"
        self._launch(claim.run, job, owner_id=owner_id)
        return "started"

    async def _owner_of(self, db: Session, job: ScheduledJob) -> int | None:
        if job.rule_id is None:
            return None
        return await ScheduledJobRepository(db, self._clock).rule_owner(job.id)

    def _schedule_of(self, job: ScheduledJob) -> Schedule | None:
        try:
            return parse_schedule(
                job.schedule_kind, job.schedule_expression, job.time_zone, min_interval=1
            )
        except ScheduleInvalid:
            logger.error("scheduler_schedule_invalid", extra={"job_id": job.id})
            return None

    def _following(
        self, job: ScheduledJob, schedule: Schedule, *, previous: datetime, now: datetime
    ) -> datetime | None:
        gates = Gates(
            job_enabled=True,
            job_paused_until=moment_of(job.paused_until),
            rule_enabled=None if job.rule_id is None else True,
            rule_paused_until=None,
            subsystem_enabled=True,
            subsystem_available=True,
        )
        try:
            return next_run_at(
                schedule,
                gates,
                previous=previous,
                now=now,
                rng=self._rng,
                aligned=job.rule_id is None,
            )
        except ScheduleInvalid:
            logger.error("scheduler_schedule_exhausted", extra={"job_id": job.id})
            return None

    async def _commit(
        self,
        db: Session,
        job: ScheduledJob,
        following: datetime | None,
        *,
        start: RunTrigger | None = None,
        skip: _Skip | None = None,
    ) -> _Claim:
        jobs = ScheduledJobRepository(db, self._clock)
        runs = JobRunRepository(db, self._clock)
        run: JobRun | None = None
        async with db.transaction():
            claimed = await jobs.claim(
                job.id, due_at=str(job.next_run_at), next_run_at=stored_of(following)
            )
            if claimed and start is not None and self._leaves_no_row(job, start):
                run = self._unrecorded_run(job, start)
            elif claimed and start is not None:
                run = await runs.start(job.id, trigger=start, scheduled_for=job.next_run_at)
            elif claimed and skip is not None:
                run = await runs.record_skipped(
                    job.id,
                    trigger=skip.trigger,
                    scheduled_for=job.next_run_at,
                    reason=skip.reason,
                    message=skip.message,
                    detail=skip.detail,
                )
        return _Claim(claimed, run)

    def _leaves_no_row(self, job: ScheduledJob, trigger: RunTrigger) -> bool:
        handler = self._handlers.get(job.subsystem)
        return (
            trigger == "schedule"
            and job.rule_id is None
            and handler is not None
            and not handler.records_events
        )

    def _unrecorded_run(self, job: ScheduledJob, trigger: RunTrigger) -> JobRun:
        return JobRun(
            id=next(self._unrecorded_ids),
            scheduled_job_id=job.id,
            trigger=trigger,
            requested_by_user_id=None,
            scheduled_for=job.next_run_at,
            started_at=format_timestamp(self._clock()),
            finished_at=None,
            heartbeat_at=None,
            outcome=None,
            reason=None,
            message=None,
            detail={},
        )

    def _launch(
        self,
        run: JobRun,
        job: ScheduledJob | None,
        *,
        dry_run: bool = False,
        supersede: _Active | None = None,
        external: bool = False,
        rule_bearing: bool = False,
        owner_id: int | None = None,
        subsystem: str | None = None,
    ) -> None:
        active = _Active(
            job_id=run.scheduled_job_id,
            run=run,
            run_id=run.id,
            rule_bearing=rule_bearing or (job is not None and job.rule_id is not None),
            owner_id=owner_id,
            subsystem=subsystem if job is None else job.subsystem,
        )
        # A fresh context keeps the caller's open audit scope out of the run, which opens its own.
        task = asyncio.create_task(
            self._execute(
                active, run, job, dry_run=dry_run, supersede=supersede, external=external
            ),
            name=f"scheduler-run-{run.id}",
            context=contextvars.Context(),
        )
        active.task = task
        self._active[run.id] = active
        self._tasks.add(task)
        task.add_done_callback(partial(self._task_done, active))

    def _task_done(self, active: _Active, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            self._active.pop(active.run_id, None)

    async def _execute(
        self,
        active: _Active,
        run: JobRun,
        job: ScheduledJob | None,
        *,
        dry_run: bool,
        supersede: _Active | None,
        external: bool,
    ) -> None:
        try:
            if job is None:
                job = await self._load(run.scheduled_job_id)
            if job is None:
                return
            outcome = await self._outcome(active, run, job, dry_run, supersede, external)
            await completed(self._settle(run, outcome))
        except asyncio.CancelledError:
            await self._record_cancelled(active)
        except Exception as error:
            logger.error(
                "scheduler_run_crashed",
                extra={
                    "run_id": run.id,
                    "job_id": run.scheduled_job_id,
                    "error": type(error).__name__,
                },
            )
            await self._settle_unsettled(run)
        finally:
            await self._finish_up(active)

    async def _settle_unsettled(self, run: JobRun) -> None:
        try:
            await completed(
                self._settle(run, _Outcome("failure", "run_unsettled", UNSETTLED_MESSAGE))
            )
        except Exception as error:
            logger.error(
                "scheduler_run_unsettled",
                extra={"run_id": run.id, "error": type(error).__name__},
            )

    async def _load(self, job_id: int) -> ScheduledJob | None:
        async with self._database.session() as db:
            return await ScheduledJobRepository(db, self._clock).get(job_id)

    async def _outcome(
        self,
        active: _Active,
        run: JobRun,
        job: ScheduledJob,
        dry_run: bool,
        supersede: _Active | None,
        external: bool,
    ) -> _Outcome:
        running = self._other_run(active, job.id) if external else None
        if self._closing or self._suspended:
            return _Outcome("skipped", "scheduler_stopped", STOPPING_MESSAGE)
        if running is not None:
            return _Outcome("skipped", "overrun", OVERRUN_MESSAGE, _running_detail(running))
        if supersede is not None and not await self._supersede(supersede):
            return _Outcome("skipped", "overrun", NOT_STOPPED_MESSAGE, _running_detail(supersede))
        handler = self._handlers.get(job.subsystem)
        if handler is None:
            return _Outcome("failure", "subsystem_unavailable", UNAVAILABLE_MESSAGE)
        if dry_run and not handler.supports_dry_run:
            return _Outcome("skipped", "dry_run_unsupported", DRY_RUN_UNSUPPORTED_MESSAGE)
        return await self._call(handler, Dispatch(job, run, self._clock(), dry_run))

    def _other_run(self, active: _Active, job_id: int) -> _Active | None:
        for other in self._active.values():
            if other.job_id == job_id and other is not active:
                return other
        return None

    async def _supersede(self, previous: _Active) -> bool:
        previous.cancel_reason = "superseded"
        task = previous.task
        if task is None or task.done():
            return True
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=CANCEL_PREVIOUS_WAIT.total_seconds())
        return bool(done)

    async def _call(self, handler: JobHandler, dispatch: Dispatch) -> _Outcome:
        try:
            limit = asyncio.timeout(handler.run_timeout.total_seconds())
            try:
                async with limit:
                    result = await handler.dispatch(dispatch)
            except TimeoutError:
                if limit.expired():
                    return self._deadline_outcome(handler)
                raise
        except RunSkipped as skipped:
            return _Outcome("skipped", skipped.reason, skipped.message, dict(skipped.detail))
        except RunFailed as failed:
            return _Outcome("failure", failed.reason, failed.message)
        except DetailInvalid:
            return _Outcome("failure", "detail_invalid", DETAIL_INVALID_MESSAGE)
        except Exception as error:
            logger.error(
                "job_run_failed",
                extra={
                    "run_id": dispatch.run.id,
                    "job_id": dispatch.job.id,
                    "error": type(error).__name__,
                },
                exc_info=error if self._development_checks else None,
            )
            message = str(error) if isinstance(error, ProductError | RepositoryError) else None
            return _Outcome("failure", reason_of(error), message)
        return _result_outcome(result)

    def _deadline_outcome(self, handler: JobHandler) -> _Outcome:
        if handler.cancellation_safe:
            return _Outcome("failure", "deadline", DEADLINE_MESSAGE)
        return _Outcome(
            "failure",
            "outcome_unknown",
            UNKNOWN_OUTCOME_MESSAGE,
            {"time_limit_seconds": int(handler.run_timeout.total_seconds())},
        )

    async def _settle(self, run: JobRun, outcome: _Outcome) -> None:
        if run.id < 0:
            await self._settle_unrecorded(run, outcome)
            return

        async def attempt() -> None:
            async with self._database.session() as db:
                runs = JobRunRepository(db, self._clock)
                jobs = ScheduledJobRepository(db, self._clock)
                async with db.transaction():
                    finished = await runs.finish(
                        run.id,
                        outcome=outcome.outcome,
                        reason=outcome.reason,
                        message=outcome.message,
                        detail=outcome.detail,
                    )
                    if not finished or run.trigger == "dry_run":
                        return
                    job = await jobs.get(run.scheduled_job_id)
                    if job is not None:
                        await self._count(db, jobs, job, run, outcome)

        await retry_busy(attempt)

    async def _settle_unrecorded(self, run: JobRun, outcome: _Outcome) -> None:
        active = self._active.get(run.id)
        async with nullcontext() if active is None else active.recording:
            if active is not None and active.recorded:
                return
            await self._write_unrecorded(run, outcome)
            if active is not None:
                active.recorded = True

    async def _write_unrecorded(self, run: JobRun, outcome: _Outcome) -> None:
        if outcome.outcome == "success":
            await retry_busy(partial(self._settle_quietly, run))
            self._unrecorded[run.scheduled_job_id] = UnrecordedRun(run.started_at, "success")
            return

        async def attempt() -> None:
            async with self._database.session() as db:
                runs = JobRunRepository(db, self._clock)
                jobs = ScheduledJobRepository(db, self._clock)
                async with db.transaction():
                    recorded = await runs.record_late(
                        run,
                        outcome=outcome.outcome,
                        reason=outcome.reason,
                        message=outcome.message,
                        detail=outcome.detail,
                    )
                    job = await jobs.get(run.scheduled_job_id)
                    if job is not None:
                        await self._count(db, jobs, job, recorded, outcome)

        await retry_busy(attempt)

    async def _settle_quietly(self, run: JobRun) -> None:
        async with self._database.session() as db:
            jobs = ScheduledJobRepository(db, self._clock)
            job = await jobs.get(run.scheduled_job_id)
            if job is None or not (job.consecutive_failures or _stale(job, run)):
                return
            async with db.transaction():
                job = await jobs.get(run.scheduled_job_id)
                if job is None:
                    return
                failed = job.consecutive_failures > 0
                if failed:
                    await jobs.record_success(job.id, job.next_run_at)
                if failed or _stale(job, run):
                    await jobs.set_last_run(job.id, run.started_at, "success")

    async def _count(
        self,
        db: Session,
        jobs: ScheduledJobRepository,
        job: ScheduledJob,
        run: JobRun,
        outcome: _Outcome,
    ) -> None:
        failures = failures_after(job.consecutive_failures, outcome.outcome)
        recorder = ActivityRecorder(db, self._clock)
        if (
            outcome.outcome in ("failure", "partial")
            and job.rule_id is not None
            and run.trigger != "dry_run"
        ):
            await self._record_run_failed(db, recorder, job, run, outcome)
        match outcome.outcome:
            case "success":
                if job.consecutive_failures:
                    await jobs.record_success(job.id, job.next_run_at)
            case "failure" if reached_threshold(failures, job.failure_threshold):
                await jobs.record_failure(job.id, failures, None)
                await jobs.disable(job.id)
                rule = await rule_detail(db, self._clock, job)
                name = rule.get("rule_name")
                await recorder.record_event(
                    SubsystemActor(
                        job.subsystem,
                        job.rule_id,
                        run.id,
                        name if isinstance(name, str) else None,
                    ),
                    action=DISABLED_ACTION,
                    target_type="scheduled_job",
                    target_id=str(job.id),
                    environment_id=job.environment_id,
                    result="success",
                    detail={
                        "consecutive_failures": failures,
                        "threshold": job.failure_threshold,
                        "last_reason": outcome.reason,
                        **rule,
                    },
                )
                logger.warning(
                    "scheduled_job_disabled",
                    extra={"job_id": job.id, "subsystem": job.subsystem, "failures": failures},
                )
            case "failure":
                following = job.next_run_at
                if (
                    run.trigger != "manual"
                    and job.failure_threshold is not None
                    and job.schedule_kind != "webhook"
                ):
                    following = stored_of(self._after_failure(job, failures))
                await jobs.record_failure(job.id, failures, following)

    async def _record_run_failed(
        self,
        db: Session,
        recorder: ActivityRecorder,
        job: ScheduledJob,
        run: JobRun,
        outcome: _Outcome,
    ) -> None:
        rule = await rule_detail(db, self._clock, job)
        name = rule.get("rule_name")
        detail: dict[str, DetailValue] = {**rule}
        if outcome.reason is not None:
            detail["reason"] = outcome.reason
        await recorder.record_event(
            SubsystemActor(
                job.subsystem, job.rule_id, run.id, name if isinstance(name, str) else None
            ),
            action=(
                AUTOMATION_RUN_PARTIAL_ACTION
                if outcome.outcome == "partial"
                else AUTOMATION_RUN_FAILED_ACTION
            ),
            target_type="scheduled_job",
            target_id=str(job.id),
            environment_id=job.environment_id,
            result="failure",
            detail=detail,
        )

    def _after_failure(self, job: ScheduledJob, failures: int) -> datetime | None:
        scheduled = moment_of(job.next_run_at)
        if scheduled is None:
            return None
        delay = backoff_delay(failures, rng=self._rng)
        earliest = floor_seconds(self._clock()) + timedelta(
            seconds=math.ceil(delay.total_seconds())
        )
        schedule = self._schedule_of(job)
        if schedule is None:
            return max(scheduled, earliest)
        return retry_at(schedule, scheduled, earliest)

    async def _record_cancelled(self, active: _Active) -> None:
        reason = active.cancel_reason or "shutdown"
        try:
            async with active.recording:
                if active.recorded:
                    return
                await completed(
                    record_interruption(
                        self._database,
                        self._handlers,
                        self._clock,
                        active.run_id,
                        reason=reason,
                        unrecorded=active.run if active.run_id < 0 else None,
                    )
                )
                active.recorded = True
        except Exception as error:
            logger.error(
                "scheduler_interruption_unrecorded",
                extra={"run_id": active.run_id, "error": type(error).__name__},
            )

    async def _finish_up(self, active: _Active) -> None:
        try:
            if active.cancel_reason is None and not (self._closing or self._suspended):
                await self._start_queued(active.job_id)
        except Exception as error:
            logger.error(
                "scheduler_queued_run_failed",
                extra={"job_id": active.job_id, "error": type(error).__name__},
            )
        finally:
            self._active.pop(active.run_id, None)
            self._wake.set()

    async def _start_queued(self, job_id: int) -> None:
        fire = self._queued.pop(job_id, None)
        if fire is None:
            return
        async with self._database.session() as db:
            jobs = ScheduledJobRepository(db, self._clock)
            runs = JobRunRepository(db, self._clock)
            job = await jobs.get(job_id)
            if job is None:
                return
            runnable = job.next_run_at is not None and await self._runnable_now(db, job)
            async with db.transaction():
                if runnable:
                    run = await runs.start(job_id, trigger="queued", scheduled_for=stored_of(fire))
                else:
                    await runs.record_skipped(
                        job_id,
                        trigger="queued",
                        scheduled_for=stored_of(fire),
                        reason="queue_dropped",
                        message=DROPPED_MESSAGE,
                    )
            if not runnable:
                return
            if self._closing or self._suspended:
                await self._abandon(db, run)
                return
            owner_id = await self._owner_of(db, job)
        self._launch(run, job, owner_id=owner_id)

    async def _runnable_now(self, db: Session, job: ScheduledJob) -> bool:
        available = self._handlers.get(job.subsystem) is not None
        rule = (
            None
            if job.rule_id is None
            else await AutomationRuleRepository(db, self._clock).get(job.rule_id)
        )
        if job.rule_id is not None and rule is None:
            return False
        gates = job_gates(
            job,
            rule,
            subsystem_enabled=await self._is_enabled(db, job.subsystem),
            available=available,
        )
        return gates_open(gates, self._clock())

    async def _abandon(self, db: Session, run: JobRun) -> None:
        runs = JobRunRepository(db, self._clock)
        async with db.transaction():
            if run.id < 0:
                await runs.record_late(
                    run, outcome="skipped", reason="scheduler_stopped", message=STOPPING_MESSAGE
                )
                return
            await runs.finish(
                run.id, outcome="skipped", reason="scheduler_stopped", message=STOPPING_MESSAGE
            )

    async def _interrupt_all(self, reason: str) -> None:
        active = list(self._active.values())
        self._queued.clear()
        tasks = []
        for item in active:
            item.cancel_reason = reason
            if item.task is not None and not item.task.done():
                item.task.cancel()
                tasks.append(item.task)
        if tasks:
            await asyncio.wait(tasks, timeout=SHUTDOWN_WAIT.total_seconds())
        for item in active:
            if item.task is None or not item.task.done() or item.task.cancelled():
                await self._record_cancelled(item)
