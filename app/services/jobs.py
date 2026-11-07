"""Scheduled jobs for administrators: listing, detail, pause, run now and the subsystem switch."""

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final, Literal

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.automation_rule import AutomationRule, AutomationRuleRepository
from app.db.repositories.clocked import Clock, format_timestamp
from app.db.repositories.job_run import JobRun, JobRunRepository
from app.db.repositories.pagination import MAX_ROW_ID, Page
from app.db.repositories.scheduled_job import (
    JobKind,
    JobSummary,
    RuleGate,
    ScheduledJob,
    ScheduledJobRepository,
)
from app.db.repositories.setting import SettingRepository
from app.logging import get_logger
from app.scheduler.gates import (
    RuleState,
    effective_pause,
    job_gates,
    next_run_text,
    read_subsystem_enabled,
)
from app.scheduler.loop import Scheduler, SchedulerBusy
from app.scheduler.model import subsystem_setting_key
from app.scheduler.registry import HandlerRegistry
from app.scheduler.schedules import ScheduleInvalid, parse_schedule
from app.services.activity import (
    SUBSYSTEMS,
    UNSCHEDULED_SUBSYSTEMS,
    ActivityRecorder,
    audited,
    current_audit,
)
from app.services.guards import require_administrator

__all__ = [
    "NOT_AVAILABLE",
    "PAUSE_CHOICES",
    "PAUSE_DURATIONS",
    "PAUSE_OPTIONS",
    "JobNotRunnable",
    "JobService",
    "JobView",
    "RefusalReason",
    "RunGuard",
    "after_commit",
    "describe_schedule",
    "effective_pause",
    "job_digest",
    "job_gates",
    "job_state",
    "next_run_text",
    "read_subsystem_enabled",
]

logger = get_logger(__name__)
SUBSYSTEM_BATCH: Final = 100

PAUSE_DURATIONS: Final[Mapping[str, timedelta]] = MappingProxyType(
    {
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
    }
)
PAUSE_CHOICES: Final = "Choose 1h, 4h, 24h or 7d."
PAUSE_OPTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("1h", "1 hour"),
    ("4h", "4 hours"),
    ("24h", "24 hours"),
    ("7d", "7 days"),
)
NOT_AVAILABLE: Final = "This automation is not available."
RULE_OWNS_PAUSE: Final = "This job belongs to an automation. Pause the automation instead."
JOB_CHANGED_BEFORE_RUN: Final = "The job changed since you reviewed it; nothing was run."
JOB_KINDS: Final[Mapping[str, JobKind]] = MappingProxyType(
    {"system": "system", "automation": "automation"}
)

type RunGuard = Callable[[ScheduledJob, AutomationRule | None], str | None]

type JobState = Literal[
    "running",
    "scheduled",
    "on_call",
    "unscheduled",
    "paused",
    "rule_disabled",
    "auto_disabled",
    "subsystem_disabled",
    "unavailable",
]
type RefusalReason = Literal[
    "running",
    "auto_disabled",
    "rule_disabled",
    "subsystem_disabled",
    "unavailable",
    "dry_run_unsupported",
]

_REFUSALS: Final[Mapping[RefusalReason, str]] = MappingProxyType(
    {
        "running": "The job is already running.",
        "auto_disabled": "The job was disabled after repeated failures. Enable it first.",
        "rule_disabled": "The automation is disabled.",
        "subsystem_disabled": "The subsystem is disabled.",
        "unavailable": NOT_AVAILABLE,
        "dry_run_unsupported": "This job does not support a dry run.",
    }
)


class JobNotRunnable(SchedulerBusy):
    """Raised when a run is requested for a job that cannot run now; reason says why."""

    def __init__(self, reason: RefusalReason) -> None:
        super().__init__(_REFUSALS[reason])
        self.reason = reason


@dataclass(frozen=True, slots=True)
class JobView:
    """A job as shown: its summary, the schedule in words, and the state derived from every gate.

    paused_until is the pause still in force, whichever of the rule and the job holds it.
    supports_dry_run is whether the job's subsystem declares that a dry run changes nothing.
    The last_run fields describe the latest run, recorded or not; last_run_id is None for a run
    that left no job_run row.
    """

    summary: JobSummary
    schedule: str
    state: JobState
    running: bool
    rule_enabled: bool | None
    paused_until: str | None
    subsystem_enabled: bool
    available: bool
    supports_dry_run: bool
    last_run_id: int | None = None
    last_run_at: str | None = None
    last_run_outcome: str | None = None


@contextmanager
def after_commit(db: Session, callback: Callable[[], None]) -> Iterator[None]:
    """Call callback after each commit made while the block is open."""
    remove = db.after_commit(callback)
    try:
        yield
    finally:
        remove()


def job_state(
    job: ScheduledJob,
    rule: RuleState | None,
    *,
    running: bool,
    subsystem_enabled: bool,
    available: bool,
    now: datetime,
) -> JobState:
    """Name the state a job is in, most limiting gate first."""
    if not available:
        return "unavailable"
    if not subsystem_enabled:
        return "subsystem_disabled"
    if not job.is_enabled:
        return "auto_disabled"
    if rule is not None and not rule.is_enabled:
        return "rule_disabled"
    if running:
        return "running"
    if effective_pause(job, rule, now) is not None:
        return "paused"
    if job.next_run_at is None:
        return "on_call" if job.schedule_kind == "webhook" else "unscheduled"
    return "scheduled"


class JobService:
    """Reads jobs and runs, and applies the administrator actions on rule-less jobs."""

    def __init__(
        self,
        *,
        db: Session,
        jobs: ScheduledJobRepository,
        runs: JobRunRepository,
        rules: AutomationRuleRepository,
        settings_repo: SettingRepository,
        subsystems: HandlerRegistry,
        scheduler: Scheduler,
        activity: ActivityRecorder,
        clock: Clock,
    ) -> None:
        self._db = db
        self._jobs = jobs
        self._runs = runs
        self._rules = rules
        self._settings = settings_repo
        self._subsystems = subsystems
        self._scheduler = scheduler
        self._activity = activity
        self._clock = clock

    async def page(
        self,
        actor: Principal,
        *,
        limit: int,
        cursor: str | None = None,
        subsystem: str | None = None,
        kind: str | None = None,
    ) -> Page[JobView]:
        """Return one page of jobs by ascending id; administrators only."""
        require_administrator(actor)
        if subsystem is not None and subsystem not in SUBSYSTEMS:
            raise ValidationFailed({"subsystem": "Choose a known subsystem."})
        if kind is not None and kind not in JOB_KINDS:
            raise ValidationFailed({"kind": "Choose system or automation."})
        page = await self._jobs.page(
            limit=limit,
            cursor=cursor,
            subsystem=subsystem,
            kind=None if kind is None else JOB_KINDS[kind],
        )
        now = self._clock()
        switches: dict[str, bool] = {}
        views = [await self._view(summary, now, switches) for summary in page.items]
        return Page(views, page.next_cursor)

    async def subsystem_job_count(self, actor: Principal, name: str) -> int:
        """Return how many jobs a subsystem owns; administrators only."""
        require_administrator(actor)
        if name not in SUBSYSTEMS or name in UNSCHEDULED_SUBSYSTEMS:
            raise ResourceNotFound("not found")
        return await self._jobs.count_for_subsystem(name)

    async def detail(self, actor: Principal, job_id: int) -> JobView:
        """Return one job; administrators only, and a missing job is not found."""
        require_administrator(actor)
        return await self._view(await self._summary(job_id), self._clock(), {})

    async def runs(
        self, actor: Principal, job_id: int, *, limit: int, cursor: str | None = None
    ) -> Page[JobRun]:
        """Return a job's runs, newest first; administrators only."""
        require_administrator(actor)
        await self._summary(job_id)
        return await self._runs.page(job_id, limit=limit, cursor=cursor)

    async def run(self, actor: Principal, run_id: int) -> tuple[JobView, JobRun]:
        """Return one run with the job it belongs to; administrators only."""
        require_administrator(actor)
        run = await self._runs.get(run_id) if 1 <= run_id <= MAX_ROW_ID else None
        if run is None:
            raise ResourceNotFound("not found")
        view = await self._view(await self._summary(run.scheduled_job_id), self._clock(), {})
        return view, run

    async def subsystems(self, actor: Principal) -> list[tuple[str, bool, bool]]:
        """Return each subsystem's name, whether it is switched on and whether it is available."""
        require_administrator(actor)
        return [
            (
                name,
                await read_subsystem_enabled(self._settings, name),
                self._subsystems.get(name) is not None,
            )
            for name in sorted(SUBSYSTEMS.keys() - UNSCHEDULED_SUBSYSTEMS)
        ]

    @audited(
        action="scheduled_job_paused",
        target_type="scheduled_job",
        target_id=lambda a: str(a["job_id"]),
    )
    async def pause(self, actor: Principal, job_id: int, *, duration: str) -> None:
        """Pause a rule-less job for a fixed time; administrators only."""
        require_administrator(actor)
        length = PAUSE_DURATIONS.get(duration)
        if length is None:
            raise ValidationFailed({"duration": PAUSE_CHOICES})
        with after_commit(self._db, self._scheduler.wake):
            async with self._db.transaction():
                job = await self._rule_less(job_id)
                until = format_timestamp(self._clock() + length)
                paused = replace(job, paused_until=until)
                await self._jobs.set_paused(job_id, until, await self._next_run(paused, None))
                current_audit().detail.update(duration=duration, paused_until=until)

    @audited(
        action="scheduled_job_resumed",
        target_type="scheduled_job",
        target_id=lambda a: str(a["job_id"]),
    )
    async def resume(self, actor: Principal, job_id: int) -> None:
        """Clear the pause on a rule-less job; administrators only."""
        require_administrator(actor)
        with after_commit(self._db, self._scheduler.wake):
            async with self._db.transaction():
                job = await self._rule_less(job_id)
                if job.paused_until is None:
                    current_audit().unchanged()
                    return
                resumed = replace(job, paused_until=None)
                await self._jobs.set_paused(job_id, None, await self._next_run(resumed, None))

    async def enable(self, actor: Principal, job_id: int) -> None:
        """Re-enable a job disabled after repeated failures; administrators only."""
        require_administrator(actor)
        summary = await self._summary(job_id)
        await self._enable(actor, job_id, environment_id=summary.environment_id)

    async def run_confirmation(self, actor: Principal, job_id: int) -> tuple[JobView, str]:
        """Return the job and the digest a run of it is bound to; administrators only."""
        require_administrator(actor)
        summary = await self._summary(job_id)
        rule = None if summary.job.rule_id is None else await self._rules.get(summary.job.rule_id)
        return await self._view(summary, self._clock(), {}), job_digest(summary.job, rule)

    async def run_now(
        self,
        actor: Principal,
        job_id: int,
        *,
        dry_run: bool = False,
        expected_digest: str | None = None,
    ) -> int:
        """Start a run outside the schedule and return its id; administrators only.

        A real run given `expected_digest` starts only if the job and its rule still hash to it.
        """
        require_administrator(actor)
        summary = await self._summary(job_id)
        return await self._start_run(
            actor,
            job_id,
            dry_run=dry_run,
            environment_id=summary.environment_id,
            guard=None if dry_run or expected_digest is None else _unchanged(expected_digest),
        )

    async def run_rule_job(
        self,
        actor: Principal,
        rule: AutomationRule,
        *,
        dry_run: bool = False,
        guard: RunGuard | None = None,
    ) -> int:
        """Start a run of the rule's job and return its id; the rule's owner or an administrator.

        `guard` sees the job and rule as read inside the starting transaction and returns the
        refusal text when they are not what the caller reviewed.
        """
        if not (actor.is_administrator or rule.created_by_user_id == actor.user_id):
            raise ResourceNotFound("not found")
        job = await self._jobs.get_for_rule(rule.id)
        if job is None:
            raise ResourceNotFound("not found")
        return await self._start_run(
            actor, job.id, dry_run=dry_run, environment_id=rule.environment_id, guard=guard
        )

    async def set_subsystem_enabled(self, actor: Principal, name: str, *, enabled: bool) -> None:
        """Switch a subsystem on or off for every job it owns; administrators only.

        The switch is one audited write. Each job's next fire follows in batches, and the
        scheduler also reads the switch before it serves a job, so a job not yet reached is
        still held back or released correctly.
        """
        require_administrator(actor)
        if name not in SUBSYSTEMS or name in UNSCHEDULED_SUBSYSTEMS:
            raise ResourceNotFound("not found")
        with after_commit(self._db, self._scheduler.wake):
            changed = await self._switch_subsystem(actor, name, enabled=enabled)
            if changed:
                await self._reschedule_subsystem(name, enabled=enabled)

    @audited(
        action=lambda a: "subsystem_enabled" if a["enabled"] else "subsystem_disabled",
        target_type="subsystem",
        target_id=lambda a: a["name"],
    )
    async def _switch_subsystem(self, actor: Principal, name: str, *, enabled: bool) -> bool:
        async with self._db.transaction():
            if await read_subsystem_enabled(self._settings, name) == enabled:
                current_audit().unchanged()
                return False
            await self._settings.put(
                subsystem_setting_key(name), "boolean", "true" if enabled else "false"
            )
            current_audit().detail["jobs"] = await self._jobs.count_for_subsystem(name)
        return True

    async def _reschedule_subsystem(self, name: str, *, enabled: bool) -> None:
        available = self._subsystems.get(name) is not None
        job_ids = await self._jobs.ids_for_subsystem(name)
        for start in range(0, len(job_ids), SUBSYSTEM_BATCH):
            async with self._db.transaction():
                now = self._clock()
                for job_id in job_ids[start : start + SUBSYSTEM_BATCH]:
                    summary = await self._jobs.summary(job_id)
                    if summary is None:
                        continue
                    await self._jobs.set_next_run(
                        job_id,
                        next_run_text(
                            summary.job,
                            summary.rule_gate,
                            subsystem_enabled=enabled,
                            available=available,
                            now=now,
                        ),
                    )

    @audited(
        action="scheduled_job_enabled",
        target_type="scheduled_job",
        target_id=lambda a: str(a["job_id"]),
        environment_id=lambda a: a["environment_id"],
    )
    async def _enable(self, actor: Principal, job_id: int, *, environment_id: int | None) -> None:
        require_administrator(actor)
        with after_commit(self._db, self._scheduler.wake):
            async with self._db.transaction():
                job = await self._required_job(job_id)
                if job.is_enabled:
                    current_audit().unchanged()
                    return
                rule = None if job.rule_id is None else await self._rules.get(job.rule_id)
                enabled = replace(job, is_enabled=True, consecutive_failures=0)
                await self._jobs.enable(job_id, await self._next_run(enabled, rule))

    @audited(
        action="job_run_requested",
        target_type="scheduled_job",
        target_id=lambda a: str(a["job_id"]),
        environment_id=lambda a: a["environment_id"],
    )
    async def _start_run(
        self,
        actor: Principal,
        job_id: int,
        *,
        dry_run: bool,
        environment_id: int | None,
        guard: RunGuard | None = None,
    ) -> int:
        """Start a run outside the schedule and return its id; checks nothing about the actor."""
        claimed: list[tuple[JobRun, bool, int | None, str]] = []

        def begin() -> None:
            for run, rule_bearing, owner_id, subsystem in claimed:
                self._scheduler.start_claimed(
                    run,
                    dry_run=dry_run,
                    rule_bearing=rule_bearing,
                    owner_id=owner_id,
                    subsystem=subsystem,
                )

        with after_commit(self._db, begin):
            async with self._db.transaction():
                job = await self._required_job(job_id)
                rule = None if job.rule_id is None else await self._rules.get(job.rule_id)
                if guard is not None and (changed := guard(job, rule)) is not None:
                    current_audit().reason = "changed_since_review"
                    raise ValidationFailed({"job": changed})
                refusal = await self._refusal(job, rule, dry_run=dry_run)
                if refusal is not None:
                    raise JobNotRunnable(refusal)
                owner_id = None if rule is None else rule.created_by_user_id
                self._scheduler.require_idle(
                    job_id,
                    rule_bearing=rule is not None,
                    owner_id=owner_id,
                    subsystem=job.subsystem,
                )
                run = await self._runs.start(
                    job_id,
                    trigger="dry_run" if dry_run else "manual",
                    scheduled_for=None,
                    requested_by_user_id=actor.user_id,
                )
                claimed.append((run, rule is not None, owner_id, job.subsystem))
                current_audit().detail.update(job_run_id=run.id, dry_run=dry_run)
        return claimed[0][0].id

    async def _refusal(
        self, job: ScheduledJob, rule: AutomationRule | None, *, dry_run: bool = False
    ) -> RefusalReason | None:
        handler = self._subsystems.get(job.subsystem)
        if handler is None:
            return "unavailable"
        if dry_run and not handler.supports_dry_run:
            return "dry_run_unsupported"
        if not await read_subsystem_enabled(self._settings, job.subsystem):
            return "subsystem_disabled"
        if not dry_run:
            if not job.is_enabled:
                return "auto_disabled"
            if rule is not None and not rule.is_enabled:
                return "rule_disabled"
        latest = await self._runs.latest(job.id)
        if self._scheduler.running(job.id) or (latest is not None and latest.outcome is None):
            return "running"
        return None

    async def _next_run(self, job: ScheduledJob, rule: AutomationRule | None) -> str | None:
        return next_run_text(
            job,
            rule,
            subsystem_enabled=await read_subsystem_enabled(self._settings, job.subsystem),
            available=self._subsystems.get(job.subsystem) is not None,
            now=self._clock(),
        )

    async def _required_job(self, job_id: int) -> ScheduledJob:
        job = await self._jobs.get(job_id) if 1 <= job_id <= MAX_ROW_ID else None
        if job is None:
            raise ResourceNotFound("not found")
        return job

    async def _rule_less(self, job_id: int) -> ScheduledJob:
        job = await self._required_job(job_id)
        if job.rule_id is not None:
            raise ValidationFailed({"job": RULE_OWNS_PAUSE})
        return job

    async def _summary(self, job_id: int) -> JobSummary:
        summary = await self._jobs.summary(job_id) if 1 <= job_id <= MAX_ROW_ID else None
        if summary is None:
            raise ResourceNotFound("not found")
        return summary

    async def _view(self, summary: JobSummary, now: datetime, switches: dict[str, bool]) -> JobView:
        job = summary.job
        if job.subsystem not in switches:
            switches[job.subsystem] = await read_subsystem_enabled(self._settings, job.subsystem)
        gate: RuleGate | None = summary.rule_gate
        handler = self._subsystems.get(job.subsystem)
        running = self._scheduler.running(job.id)
        last_id, last_at, last_outcome = self._latest_run(summary)
        return JobView(
            summary=summary,
            schedule=describe_schedule(job),
            state=job_state(
                job,
                gate,
                running=running,
                subsystem_enabled=switches[job.subsystem],
                available=handler is not None,
                now=now,
            ),
            running=running,
            rule_enabled=None if gate is None else gate.is_enabled,
            paused_until=effective_pause(job, gate, now),
            subsystem_enabled=switches[job.subsystem],
            available=handler is not None,
            supports_dry_run=handler is not None and handler.supports_dry_run,
            last_run_id=last_id,
            last_run_at=last_at,
            last_run_outcome=last_outcome,
        )

    def _latest_run(self, summary: JobSummary) -> tuple[int | None, str | None, str | None]:
        recorded = summary.last_run
        job = summary.job
        held = self._scheduler.unrecorded_run(job.id)
        if held is not None:
            unrecorded = (held.started_at, held.outcome)
        elif job.last_run_at is not None and job.last_run_outcome is not None:
            unrecorded = (job.last_run_at, job.last_run_outcome)
        else:
            unrecorded = None
        if unrecorded is not None and (recorded is None or unrecorded[0] > recorded.started_at):
            return None, unrecorded[0], unrecorded[1]
        if recorded is None:
            return None, None, None
        return recorded.id, recorded.started_at, recorded.outcome


def job_digest(job: ScheduledJob, rule: AutomationRule | None) -> str:
    """Return the SHA-256 hex digest of what a run of the job acts on and under which rule."""
    canonical = json.dumps(
        {
            "job": [
                job.id,
                job.created_at,
                job.subsystem,
                job.rule_id,
                job.environment_id,
                job.schedule_kind,
                job.schedule_expression,
                job.time_zone,
            ],
            "rule": None
            if rule is None
            else [
                rule.id,
                rule.created_at,
                rule.updated_at,
                rule.target_selector,
                rule.parameters,
                rule.environment_id,
            ],
        },
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _unchanged(expected: str) -> RunGuard:
    def guard(job: ScheduledJob, rule: AutomationRule | None) -> str | None:
        return None if job_digest(job, rule) == expected else JOB_CHANGED_BEFORE_RUN

    return guard


def describe_schedule(job: ScheduledJob) -> str:
    """Describe the job's schedule in words; an unreadable expression is returned as stored."""
    try:
        return parse_schedule(
            job.schedule_kind, job.schedule_expression, job.time_zone, min_interval=1
        ).describe()
    except ScheduleInvalid:
        return job.schedule_expression
