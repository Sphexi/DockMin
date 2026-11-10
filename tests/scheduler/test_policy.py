"""Pure scheduling policy: backoff, gates, the next fire, misfire, catch-up and overrun."""

import random
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.scheduler.policy import (
    BACKOFF_CAP,
    CANCEL_PREVIOUS_WAIT,
    MISFIRE_GRACE,
    MISSED_COUNT_CAP,
    Gates,
    backoff_delay,
    due_decision,
    failures_after,
    gates_open,
    missed,
    missed_count,
    next_run_at,
    off_grid,
    overrun_decision,
    reached_threshold,
    retry_at,
)
from app.scheduler.schedules import parse_schedule

NOW = datetime(2026, 10, 22, 10, 0, 0, tzinfo=UTC)
LOW_JITTER = 0.0
HIGH_JITTER = 0.999999999
HALF_JITTER_FACTOR = 0.5
SEEDED_DRAWS = 2000
OPEN = Gates(
    job_enabled=True,
    job_paused_until=None,
    rule_enabled=None,
    rule_paused_until=None,
    subsystem_enabled=True,
    subsystem_available=True,
)
DAILY_AT_THREE = parse_schedule("cron", "0 3 * * *")
EVERY_MINUTE = parse_schedule("cron", "* * * * *")
HOURLY = parse_schedule("cron", "0 * * * *")
FIVE_MINUTES = parse_schedule("interval", "300")


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def at_jitter(value: float) -> Callable[[], float]:
    return lambda: value


@pytest.mark.parametrize(
    ("failures", "raw_seconds"),
    [
        pytest.param(1, 60, id="first"),
        pytest.param(2, 120, id="second"),
        pytest.param(3, 240, id="third"),
        pytest.param(4, 480, id="fourth"),
        pytest.param(5, 960, id="fifth"),
        pytest.param(6, 1920, id="sixth"),
        pytest.param(7, 3600, id="capped-at-seven"),
        pytest.param(9, 3600, id="stays-capped"),
    ],
)
def test_backoff_doubles_to_the_cap_and_jitter_scales_it_between_half_and_whole(
    failures: int, raw_seconds: int
) -> None:
    shortest = backoff_delay(failures, rng=at_jitter(LOW_JITTER)).total_seconds()
    longest = backoff_delay(failures, rng=at_jitter(HIGH_JITTER)).total_seconds()

    assert shortest == pytest.approx(raw_seconds * HALF_JITTER_FACTOR)
    assert raw_seconds * 0.999 < longest <= raw_seconds


def test_no_failures_means_no_delay() -> None:
    assert backoff_delay(0, rng=at_jitter(LOW_JITTER)) == timedelta(0)


def test_an_enormous_failure_count_stays_at_the_cap() -> None:
    assert backoff_delay(10**6, rng=at_jitter(HIGH_JITTER)) <= BACKOFF_CAP


def test_seeded_jitter_stays_inside_its_range_for_a_failure_count() -> None:
    generator = random.Random(1234)
    third_failure_floor, third_failure_ceiling = 120, 240

    delays = [backoff_delay(3, rng=generator.random).total_seconds() for _ in range(SEEDED_DRAWS)]

    assert min(delays) >= third_failure_floor
    assert max(delays) < third_failure_ceiling


def test_a_random_source_outside_zero_to_one_is_clamped() -> None:
    below = backoff_delay(1, rng=at_jitter(-5.0)).total_seconds()
    above = backoff_delay(1, rng=at_jitter(9.0)).total_seconds()

    assert (below, above) == (30.0, 60.0)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param("failure", 3, id="failure-adds-one"),
        pytest.param("success", 0, id="success-resets"),
        pytest.param("partial", 2, id="partial-leaves-it"),
        pytest.param("skipped", 2, id="skipped-leaves-it"),
        pytest.param("interrupted", 2, id="interrupted-leaves-it"),
        pytest.param("dry_run", 2, id="unknown-leaves-it"),
    ],
)
def test_the_failure_counter_follows_the_outcome(outcome: str, expected: int) -> None:
    assert failures_after(2, outcome) == expected


@pytest.mark.parametrize(
    ("failures", "threshold", "reached"),
    [
        pytest.param(4, 5, False, id="below"),
        pytest.param(5, 5, True, id="at"),
        pytest.param(6, 5, True, id="above"),
        pytest.param(99, None, False, id="no-threshold"),
    ],
)
def test_the_auto_disable_threshold_is_reached_at_the_configured_count(
    failures: int, threshold: int | None, reached: bool
) -> None:
    assert reached_threshold(failures, threshold) is reached


@pytest.mark.parametrize(
    "closed",
    [
        pytest.param(replace(OPEN, job_enabled=False), id="job-disabled"),
        pytest.param(replace(OPEN, rule_enabled=False), id="rule-disabled"),
        pytest.param(replace(OPEN, subsystem_enabled=False), id="subsystem-disabled"),
        pytest.param(replace(OPEN, subsystem_available=False), id="subsystem-unavailable"),
    ],
)
def test_a_closed_gate_leaves_no_next_fire(closed: Gates) -> None:
    assert next_run_at(DAILY_AT_THREE, closed, previous=None, now=NOW) is None
    assert gates_open(closed, NOW) is False


@pytest.mark.parametrize(
    ("gates", "is_open"),
    [
        pytest.param(OPEN, True, id="all-open"),
        pytest.param(replace(OPEN, rule_enabled=True), True, id="rule-enabled"),
        pytest.param(
            replace(OPEN, job_paused_until=NOW + timedelta(seconds=1)), False, id="job-paused"
        ),
        pytest.param(
            replace(OPEN, rule_paused_until=NOW + timedelta(seconds=1)), False, id="rule-paused"
        ),
        pytest.param(replace(OPEN, rule_paused_until=NOW), True, id="pause-ending-now"),
        pytest.param(
            replace(OPEN, rule_paused_until=NOW - timedelta(hours=1)), True, id="pause-over"
        ),
    ],
)
def test_gates_open_reports_whether_a_run_may_start(gates: Gates, is_open: bool) -> None:
    assert gates_open(gates, NOW) is is_open


ON_CALL = parse_schedule("webhook", "")
ARMED = utc(2026, 10, 22, 10, 0, 30)


def test_a_webhook_schedule_returns_the_armed_time_while_every_gate_is_open() -> None:
    assert next_run_at(ON_CALL, OPEN, previous=ARMED, now=NOW) == ARMED


def test_a_webhook_schedule_with_nothing_armed_is_never_given_a_fire() -> None:
    assert next_run_at(ON_CALL, OPEN, previous=None, now=NOW) is None


@pytest.mark.parametrize(
    "closed",
    [
        pytest.param(replace(OPEN, job_enabled=False), id="job-disabled"),
        pytest.param(replace(OPEN, rule_enabled=False), id="rule-disabled"),
        pytest.param(replace(OPEN, subsystem_enabled=False), id="subsystem-disabled"),
        pytest.param(replace(OPEN, subsystem_available=False), id="subsystem-unavailable"),
        pytest.param(replace(OPEN, job_paused_until=NOW + timedelta(hours=1)), id="job-paused"),
        pytest.param(replace(OPEN, rule_paused_until=NOW + timedelta(hours=1)), id="rule-paused"),
    ],
)
def test_a_closed_gate_drops_the_armed_time_of_a_webhook_schedule(closed: Gates) -> None:
    assert next_run_at(ON_CALL, closed, previous=ARMED, now=NOW) is None


def test_failures_never_delay_the_armed_time_of_a_webhook_schedule() -> None:
    fire = next_run_at(
        ON_CALL, OPEN, previous=ARMED, now=NOW, failures=9, rng=at_jitter(HIGH_JITTER)
    )

    assert fire == ARMED


def test_a_pause_that_has_ended_leaves_the_armed_time_of_a_webhook_schedule() -> None:
    ended = replace(OPEN, job_paused_until=NOW)

    assert next_run_at(ON_CALL, ended, previous=ARMED, now=NOW) == ARMED


def test_open_gates_give_the_schedules_next_fire() -> None:
    assert next_run_at(DAILY_AT_THREE, OPEN, previous=None, now=NOW) == utc(2026, 10, 23, 3)


def test_a_new_interval_job_first_fires_one_interval_from_now() -> None:
    assert next_run_at(FIVE_MINUTES, OPEN, previous=None, now=NOW) == utc(2026, 10, 22, 10, 5)


def test_an_interval_keeps_the_phase_of_the_previous_fire() -> None:
    fire = next_run_at(
        FIVE_MINUTES, OPEN, previous=utc(2026, 10, 22, 9, 57, 0), now=utc(2026, 10, 22, 10, 0, 1)
    )

    assert fire == utc(2026, 10, 22, 10, 2)


@pytest.mark.parametrize(
    ("gates", "expected"),
    [
        pytest.param(
            replace(OPEN, rule_paused_until=utc(2026, 10, 25, 12)),
            utc(2026, 10, 26, 3),
            id="first-fire-after-the-pause",
        ),
        pytest.param(
            replace(OPEN, rule_paused_until=utc(2026, 10, 25, 3)),
            utc(2026, 10, 25, 3),
            id="a-fire-on-the-pause-end-is-kept",
        ),
        pytest.param(
            replace(
                OPEN,
                job_paused_until=utc(2026, 10, 24, 12),
                rule_paused_until=utc(2026, 10, 23, 12),
            ),
            utc(2026, 10, 25, 3),
            id="the-later-pause-wins",
        ),
        pytest.param(
            replace(OPEN, rule_paused_until=utc(2026, 10, 22, 9)),
            utc(2026, 10, 23, 3),
            id="an-expired-pause-is-ignored",
        ),
        pytest.param(
            replace(OPEN, rule_paused_until=NOW),
            utc(2026, 10, 23, 3),
            id="a-pause-ending-now-is-ignored",
        ),
    ],
)
def test_a_pause_moves_the_first_fire_to_at_or_after_its_end(
    gates: Gates, expected: datetime
) -> None:
    assert next_run_at(DAILY_AT_THREE, gates, previous=None, now=NOW) == expected


@pytest.mark.parametrize(
    ("pause_end", "previous", "expected"),
    [
        pytest.param(
            utc(2026, 10, 22, 11, 0, 0),
            utc(2026, 10, 22, 9, 57, 0),
            utc(2026, 10, 22, 11, 2),
            id="phase-kept-across-the-pause",
        ),
        pytest.param(
            utc(2026, 10, 22, 11, 2, 0),
            utc(2026, 10, 22, 9, 57, 0),
            utc(2026, 10, 22, 11, 2),
            id="phase-lands-on-the-pause-end",
        ),
        pytest.param(
            utc(2026, 10, 22, 11, 0, 0),
            None,
            utc(2026, 10, 22, 11, 5),
            id="no-phase-pause-end-plus-interval",
        ),
    ],
)
def test_a_paused_interval_resumes_on_its_phase(
    pause_end: datetime, previous: datetime | None, expected: datetime
) -> None:
    gates = replace(OPEN, rule_paused_until=pause_end)

    assert next_run_at(FIVE_MINUTES, gates, previous=previous, now=NOW) == expected


@pytest.mark.parametrize(
    ("schedule", "failures", "jitter", "expected"),
    [
        pytest.param(
            DAILY_AT_THREE,
            3,
            LOW_JITTER,
            utc(2026, 10, 23, 3),
            id="never-earlier-than-the-schedule",
        ),
        pytest.param(
            EVERY_MINUTE,
            5,
            LOW_JITTER,
            NOW + timedelta(seconds=480),
            id="delays-a-frequent-schedule",
        ),
        pytest.param(HOURLY, 1, LOW_JITTER, utc(2026, 10, 22, 11, 0), id="a-later-schedule-wins"),
        pytest.param(
            EVERY_MINUTE,
            7,
            LOW_JITTER,
            NOW + timedelta(seconds=1800),
            id="capped-delay-with-low-jitter",
        ),
        pytest.param(
            EVERY_MINUTE,
            2,
            0.31,
            NOW + timedelta(seconds=79),
            id="rounds-the-delay-up-to-a-whole-second",
        ),
    ],
)
def test_backoff_only_ever_delays_the_next_fire(
    schedule: object, failures: int, jitter: float, expected: datetime
) -> None:
    fire = next_run_at(
        schedule,  # type: ignore[arg-type]
        OPEN,
        previous=None,
        now=NOW,
        failures=failures,
        rng=at_jitter(jitter),
    )

    assert fire == expected


def test_an_interval_that_failed_retries_on_the_next_slot_of_its_original_phase() -> None:
    now = utc(2026, 10, 22, 10, 0, 1)

    fire = next_run_at(
        FIVE_MINUTES,
        OPEN,
        previous=utc(2026, 10, 22, 9, 57, 0),
        now=now,
        failures=4,
        rng=at_jitter(LOW_JITTER),
    )

    assert fire == utc(2026, 10, 22, 10, 7, 0)


def test_a_cron_schedule_that_failed_retries_at_the_backoff_time_itself() -> None:
    now = utc(2026, 10, 22, 10, 0, 1)

    fire = next_run_at(
        EVERY_MINUTE,
        OPEN,
        previous=None,
        now=now,
        failures=4,
        rng=at_jitter(LOW_JITTER),
    )

    assert fire == now + timedelta(seconds=240)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(utc(2026, 10, 22, 10, 0, 0), utc(2026, 10, 22, 10, 1, 30), id="on-a-point"),
        pytest.param(utc(2026, 10, 22, 10, 0, 59), utc(2026, 10, 22, 10, 1, 30), id="before-one"),
        pytest.param(utc(2026, 10, 22, 10, 1, 30), utc(2026, 10, 22, 10, 3, 0), id="on-the-next"),
    ],
)
def test_an_aligned_interval_fires_on_whole_multiples_of_its_length_in_unix_time(
    now: datetime, expected: datetime
) -> None:
    fire = next_run_at(
        parse_schedule("interval", "90", min_interval=1),
        OPEN,
        previous=utc(2026, 10, 22, 9, 59, 13),
        now=now,
        aligned=True,
    )

    assert fire == expected


def test_an_aligned_interval_that_failed_retries_on_the_grid() -> None:
    fire = next_run_at(
        parse_schedule("interval", "60", min_interval=1),
        OPEN,
        previous=None,
        now=utc(2026, 10, 22, 10, 0, 1),
        failures=4,
        rng=at_jitter(LOW_JITTER),
        aligned=True,
    )

    assert fire == utc(2026, 10, 22, 10, 5, 0)


@pytest.mark.parametrize(
    ("fire", "earliest", "expected"),
    [
        pytest.param(
            utc(2026, 10, 22, 10, 0), utc(2026, 10, 22, 9, 59), utc(2026, 10, 22, 10, 0), id="later"
        ),
        pytest.param(
            utc(2026, 10, 22, 10, 0),
            utc(2026, 10, 22, 10, 4, 1),
            utc(2026, 10, 22, 10, 5),
            id="next-slot",
        ),
        pytest.param(
            utc(2026, 10, 22, 10, 0),
            utc(2026, 10, 22, 10, 5),
            utc(2026, 10, 22, 10, 5),
            id="a-slot-exactly",
        ),
    ],
)
def test_an_interval_retries_on_the_first_slot_of_its_phase_at_or_after_the_earliest(
    fire: datetime, earliest: datetime, expected: datetime
) -> None:
    assert retry_at(FIVE_MINUTES, fire, earliest) == expected


def test_a_cron_schedule_retries_at_the_earliest_when_its_fire_is_sooner() -> None:
    earliest = utc(2026, 10, 22, 10, 4, 1)

    assert retry_at(HOURLY, utc(2026, 10, 22, 10, 0), earliest) == earliest


@pytest.mark.parametrize(
    ("schedule", "fire", "expected"),
    [
        pytest.param(
            parse_schedule("interval", "60", min_interval=1),
            utc(2026, 1, 1, 12, 1, 0),
            False,
            id="on-grid",
        ),
        pytest.param(
            parse_schedule("interval", "60", min_interval=1),
            utc(2026, 1, 1, 12, 1, 7),
            True,
            id="off-grid",
        ),
        pytest.param(
            parse_schedule("interval", "90", min_interval=1),
            utc(2026, 1, 1, 12, 1, 30),
            False,
            id="ninety",
        ),
        pytest.param(
            parse_schedule("interval", "90", min_interval=1),
            utc(2026, 1, 1, 12, 1, 0),
            True,
            id="off-ninety",
        ),
        pytest.param(HOURLY, utc(2026, 1, 1, 12, 0, 7), False, id="cron-is-never-off-grid"),
    ],
)
def test_a_fire_is_off_grid_only_for_an_interval_not_on_a_multiple_of_its_length(
    schedule: object, fire: datetime, expected: bool
) -> None:
    assert off_grid(schedule, fire) is expected  # type: ignore[arg-type]


def test_a_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="aware"):
        next_run_at(DAILY_AT_THREE, OPEN, previous=None, now=datetime(2026, 1, 1))


@pytest.mark.parametrize(
    ("lateness", "late"),
    [
        pytest.param(timedelta(0), False, id="on-time"),
        pytest.param(MISFIRE_GRACE, False, id="exactly-the-grace"),
        pytest.param(MISFIRE_GRACE + timedelta(seconds=1), True, id="one-second-past-the-grace"),
    ],
)
def test_a_fire_is_missed_only_beyond_the_misfire_grace(lateness: timedelta, late: bool) -> None:
    assert missed(NOW, NOW + lateness) is late


@pytest.mark.parametrize(
    ("lateness", "catch_up", "decision"),
    [
        pytest.param(timedelta(seconds=30), True, "run", id="on-time-with-catch-up"),
        pytest.param(timedelta(seconds=30), False, "run", id="on-time-without"),
        pytest.param(timedelta(hours=5), True, "catch_up", id="late-with-catch-up"),
        pytest.param(timedelta(hours=5), False, "skip_missed", id="late-without"),
        pytest.param(MISFIRE_GRACE, False, "run", id="the-grace-boundary"),
    ],
)
def test_a_due_fire_is_run_caught_up_or_skipped(
    lateness: timedelta, catch_up: bool, decision: str
) -> None:
    assert due_decision(NOW, NOW + lateness, catch_up=catch_up) == decision


@pytest.mark.parametrize(
    ("schedule", "first", "now", "expected"),
    [
        pytest.param(
            parse_schedule("cron", "*/5 * * * *"),
            utc(2026, 1, 1, 0, 0),
            utc(2026, 1, 1, 1, 0),
            13,
            id="cron-hour-counts-both-ends",
        ),
        pytest.param(
            parse_schedule("cron", "*/5 * * * *"),
            utc(2026, 1, 1, 0, 0),
            utc(2026, 1, 1, 0, 0),
            1,
            id="cron-first-is-now",
        ),
        pytest.param(
            parse_schedule("cron", "*/5 * * * *"),
            utc(2026, 1, 1, 1, 0),
            utc(2026, 1, 1, 0, 0),
            0,
            id="cron-first-after-now",
        ),
        pytest.param(
            FIVE_MINUTES, utc(2026, 1, 1, 0, 0), utc(2026, 1, 1, 1, 0), 13, id="interval-hour"
        ),
        pytest.param(
            FIVE_MINUTES,
            utc(2026, 1, 1),
            utc(2027, 1, 1),
            MISSED_COUNT_CAP,
            id="interval-year-capped",
        ),
    ],
)
def test_missed_count_counts_fires_from_the_first_missed_up_to_now(
    schedule: object, first: datetime, now: datetime, expected: int
) -> None:
    assert missed_count(schedule, first, now) == expected  # type: ignore[arg-type]


def test_missed_count_honours_a_smaller_cap() -> None:
    assert missed_count(FIVE_MINUTES, utc(2026, 1, 1), utc(2027, 1, 1), cap=7) == 7


def test_missed_count_for_a_cron_stops_at_the_cap() -> None:
    assert missed_count(EVERY_MINUTE, utc(2026, 1, 1), utc(2026, 1, 1, 12), cap=50) == 50


@pytest.mark.parametrize("policy", ["skip", "queue", "cancel_previous"])
def test_a_fire_with_nothing_running_always_starts(policy: str) -> None:
    assert overrun_decision(policy, running=False, queued=False, cancellation_safe=False) == "start"


@pytest.mark.parametrize(
    ("policy", "queued", "safe", "decision"),
    [
        pytest.param("skip", False, True, "skip", id="skip"),
        pytest.param("queue", False, False, "queue", id="queue-first"),
        pytest.param("queue", True, False, "collapse", id="queue-second-collapses"),
        pytest.param("cancel_previous", False, True, "cancel_previous", id="cancel-when-safe"),
        pytest.param(
            "cancel_previous", False, False, "skip", id="cancel-degrades-to-skip-when-unsafe"
        ),
    ],
)
def test_a_fire_while_a_run_is_in_flight_follows_the_overrun_policy(
    policy: str, queued: bool, safe: bool, decision: str
) -> None:
    assert overrun_decision(policy, running=True, queued=queued, cancellation_safe=safe) == decision


def test_an_unknown_overrun_policy_is_refused() -> None:
    with pytest.raises(ValueError, match="Unknown overrun policy"):
        overrun_decision("bogus", running=True, queued=False, cancellation_safe=False)


def test_cancelling_the_previous_run_waits_thirty_seconds() -> None:
    assert timedelta(seconds=30) == CANCEL_PREVIOUS_WAIT
