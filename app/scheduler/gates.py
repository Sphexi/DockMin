"""Gates on a job and the next fire they leave it."""

import random
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from app.db.repositories.clocked import format_timestamp, parse_timestamp
from app.db.repositories.scheduled_job import ScheduledJob
from app.db.repositories.setting import SettingRepository
from app.logging import get_logger
from app.scheduler.model import setting_means_enabled, subsystem_setting_key
from app.scheduler.policy import Gates, next_run_at
from app.scheduler.schedules import (
    IntervalSchedule,
    Schedule,
    ScheduleInvalid,
    WebhookSchedule,
    parse_schedule,
)

logger = get_logger(__name__)


class RuleState(Protocol):
    """The two rule fields that decide whether a job may run."""

    @property
    def is_enabled(self) -> bool:
        """Report whether the rule is switched on."""
        ...

    @property
    def paused_until(self) -> str | None:
        """Return the stored end of the rule's pause, if it has one."""
        ...


def moment_of(text: str | None) -> datetime | None:
    """Parse a stored time, passing None through."""
    return None if text is None else parse_timestamp(text)


def stored_of(moment: datetime | None) -> str | None:
    """Format a time for storage, passing None through."""
    return None if moment is None else format_timestamp(moment)


async def read_subsystem_enabled(settings: SettingRepository, name: str) -> bool:
    """Report whether a subsystem is switched on; no row means on."""
    row = await settings.get(subsystem_setting_key(name))
    return setting_means_enabled(None if row is None else row.value)


def job_gates(
    job: ScheduledJob, rule: RuleState | None, *, subsystem_enabled: bool, available: bool
) -> Gates:
    """Collect every gate that decides whether the job may run."""
    return Gates(
        job_enabled=job.is_enabled,
        job_paused_until=moment_of(job.paused_until),
        rule_enabled=None if rule is None else rule.is_enabled,
        rule_paused_until=None if rule is None else moment_of(rule.paused_until),
        subsystem_enabled=subsystem_enabled,
        subsystem_available=available,
    )


def effective_pause(job: ScheduledJob, rule: RuleState | None, now: datetime) -> str | None:
    """Return the later pause end still in the future, or None."""
    ends = [
        text
        for text in (job.paused_until, None if rule is None else rule.paused_until)
        if text is not None and parse_timestamp(text) > now
    ]
    return max(ends, default=None)


def _phase_anchor(job: ScheduledJob, schedule: Schedule) -> datetime | None:
    if not isinstance(schedule, IntervalSchedule | WebhookSchedule) or job.next_run_at is None:
        return None
    try:
        stored = parse_timestamp(job.next_run_at)
        if isinstance(schedule, WebhookSchedule):
            return stored
        return stored - timedelta(seconds=schedule.seconds)
    except ValueError:
        logger.error("job_next_run_unreadable", extra={"job_id": job.id})
        return None


def next_run_text(
    job: ScheduledJob,
    rule: RuleState | None,
    *,
    subsystem_enabled: bool,
    available: bool,
    now: datetime,
    rng: Callable[[], float] = random.random,
) -> str | None:
    """Return the stored form of the job's next fire under its gates, or None if it must not run.

    An interval keeps the phase of the fire already stored, except that a job without a rule fires
    on whole multiples of the interval in Unix time. Backoff applies only to a job that can be
    disabled by failures. A webhook job keeps the armed time it has while every gate is open.
    """
    try:
        schedule = parse_schedule(
            job.schedule_kind, job.schedule_expression, job.time_zone, min_interval=1
        )
    except ScheduleInvalid:
        logger.error("job_schedule_invalid", extra={"job_id": job.id})
        return None
    anchor = _phase_anchor(job, schedule)
    moment = next_run_at(
        schedule,
        job_gates(job, rule, subsystem_enabled=subsystem_enabled, available=available),
        previous=anchor,
        now=now,
        failures=job.consecutive_failures if job.failure_threshold is not None else 0,
        rng=rng,
        aligned=job.rule_id is None,
    )
    return stored_of(moment)
