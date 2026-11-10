"""Pure scheduling policy: gates, next run, backoff, misfire, catch-up and overrun decisions."""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.scheduler.schedules import (
    IntervalSchedule,
    Schedule,
    WebhookSchedule,
    floor_seconds,
    next_grid_point,
)

BACKOFF_BASE = timedelta(seconds=60)
BACKOFF_CAP = timedelta(hours=1)
MISFIRE_GRACE = timedelta(seconds=60)
CANCEL_PREVIOUS_WAIT = timedelta(seconds=30)
MISSED_COUNT_CAP = 10_000

_JITTER_FLOOR = 0.5
_MAX_BACKOFF_EXPONENT = 20
_ONE_SECOND = timedelta(seconds=1)

type DueDecision = Literal["run", "catch_up", "skip_missed"]
type OverrunAction = Literal["start", "skip", "queue", "collapse", "cancel_previous"]


@dataclass(frozen=True, slots=True)
class Gates:
    """Every condition other than the schedule that decides whether a job may run."""

    job_enabled: bool
    job_paused_until: datetime | None
    rule_enabled: bool | None
    rule_paused_until: datetime | None
    subsystem_enabled: bool
    subsystem_available: bool


def gates_open(gates: Gates, now: datetime) -> bool:
    """Report whether every gate lets a run start at the given time."""
    if not (gates.job_enabled and gates.subsystem_enabled and gates.subsystem_available):
        return False
    if gates.rule_enabled is False:
        return False
    return not any(
        end is not None and end > now for end in (gates.job_paused_until, gates.rule_paused_until)
    )


def backoff_delay(failures: int, *, rng: Callable[[], float]) -> timedelta:
    """Return the capped exponential delay for a failure count, scaled by jitter in [0.5, 1.0)."""
    if failures < 1:
        return timedelta(0)
    exponent = min(failures - 1, _MAX_BACKOFF_EXPONENT)
    raw = min(BACKOFF_CAP, BACKOFF_BASE * (1 << exponent))
    jitter = _JITTER_FLOOR + (1 - _JITTER_FLOOR) * min(max(rng(), 0.0), 1.0)
    return raw * jitter


def _scheduled_fire(
    schedule: Schedule,
    *,
    threshold: datetime,
    previous: datetime | None,
    first_start: datetime,
    aligned: bool,
) -> datetime:
    """Return the earliest fire strictly after threshold, keeping an interval's phase."""
    if isinstance(schedule, IntervalSchedule):
        if aligned:
            return next_grid_point(threshold, schedule.seconds)
        if previous is None:
            return schedule.next_after(first_start)
        return schedule.next_after_anchored(previous, threshold)
    return schedule.next_after(threshold)


def retry_at(schedule: Schedule, fire: datetime, earliest: datetime) -> datetime:
    """Return the first fire at or after earliest; an interval keeps the phase of fire."""
    if fire >= earliest:
        return fire
    if isinstance(schedule, IntervalSchedule):
        return schedule.at_or_after_anchored(fire, earliest)
    return earliest


def off_grid(schedule: Schedule, fire: datetime) -> bool:
    """Report whether an interval fire is not a whole multiple of the interval in Unix time."""
    return isinstance(schedule, IntervalSchedule) and (
        int(floor_seconds(fire).timestamp()) % schedule.seconds != 0
    )


def next_run_at(
    schedule: Schedule,
    gates: Gates,
    *,
    previous: datetime | None,
    now: datetime,
    failures: int = 0,
    rng: Callable[[], float] = random.random,
    aligned: bool = False,
) -> datetime | None:
    """Return the next fire as aware UTC, or None when any gate forbids running.

    An aligned interval ignores previous and fires on whole multiples of the interval in Unix time.
    A webhook schedule never computes a fire: it returns previous, the armed time, while every
    gate is open and None otherwise.
    """
    if not (gates.job_enabled and gates.subsystem_enabled and gates.subsystem_available):
        return None
    if gates.rule_enabled is False:
        return None
    now = floor_seconds(now)
    previous = None if previous is None else floor_seconds(previous)
    pauses = [
        floor_seconds(end)
        for end in (gates.job_paused_until, gates.rule_paused_until)
        if end is not None and end > now
    ]
    if isinstance(schedule, WebhookSchedule):
        return None if pauses else previous
    if pauses:
        resume = max(pauses)
        fire = _scheduled_fire(
            schedule,
            threshold=resume - _ONE_SECOND,
            previous=previous,
            first_start=resume,
            aligned=aligned,
        )
    else:
        fire = _scheduled_fire(
            schedule, threshold=now, previous=previous, first_start=now, aligned=aligned
        )
    if failures > 0:
        delay = backoff_delay(failures, rng=rng)
        earliest_retry = now + timedelta(seconds=math.ceil(delay.total_seconds()))
        fire = retry_at(schedule, fire, earliest_retry)
    return fire


def missed(due: datetime, now: datetime) -> bool:
    """Report whether a due fire is later than the misfire grace."""
    return now - due > MISFIRE_GRACE


def missed_count(
    schedule: Schedule, first: datetime, now: datetime, *, cap: int = MISSED_COUNT_CAP
) -> int:
    """Count fires from first (inclusive) up to now, stopping at cap."""
    first, now = floor_seconds(first), floor_seconds(now)
    if first > now:
        return 0
    if isinstance(schedule, IntervalSchedule):
        return min(cap, int((now - first).total_seconds()) // schedule.seconds + 1)
    count, fire = 0, first
    while fire <= now and count < cap:
        count += 1
        fire = schedule.next_after(fire)
    return count


def due_decision(due: datetime, now: datetime, *, catch_up: bool) -> DueDecision:
    """Decide how a due fire is served: on time, as one catch-up run, or as one skipped run."""
    if not missed(due, now):
        return "run"
    return "catch_up" if catch_up else "skip_missed"


def failures_after(current: int, outcome: str) -> int:
    """Return the consecutive-failure counter after a run outcome."""
    match outcome:
        case "failure":
            return current + 1
        case "success":
            return 0
        case _:
            return current


def reached_threshold(failures: int, threshold: int | None) -> bool:
    """Report whether the counter has reached the auto-disable threshold."""
    return threshold is not None and failures >= threshold


def overrun_decision(
    policy: str, *, running: bool, queued: bool, cancellation_safe: bool
) -> OverrunAction:
    """Decide what a fire does when it arrives; cancel_previous degrades to skip when unsafe."""
    if not running:
        return "start"
    match policy:
        case "skip":
            return "skip"
        case "queue":
            return "collapse" if queued else "queue"
        case "cancel_previous":
            return "cancel_previous" if cancellation_safe else "skip"
        case _:
            raise ValueError(f"Unknown overrun policy: {policy}")
