"""The next stored fire a job's gates leave it, and the pause that is in force."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.db.repositories.scheduled_job import ScheduledJob
from app.scheduler.gates import effective_pause, job_gates, moment_of, next_run_text, stored_of
from tests.scheduler.support import START, at, stamp

NOW = at(60)
AN_HOUR = 3600


@dataclass(frozen=True)
class FakeRule:
    is_enabled: bool = True
    paused_until: str | None = None


def a_job(**changes: object) -> ScheduledJob:
    job = ScheduledJob(
        id=1,
        subsystem="stack-restarter",
        rule_id=1,
        environment_id=None,
        schedule_kind="interval",
        schedule_expression=str(AN_HOUR),
        time_zone="UTC",
        next_run_at=stamp(at(1800)),
        overrun_policy="skip",
        catch_up=False,
        is_enabled=True,
        paused_until=None,
        consecutive_failures=0,
        failure_threshold=5,
        created_at=stamp(START),
        updated_at=stamp(START),
    )
    return replace(job, **changes)  # type: ignore[arg-type]


def text_for(job: ScheduledJob, rule: FakeRule | None = None, **gates: object) -> str | None:
    options = {"subsystem_enabled": True, "available": True, "now": NOW} | gates
    return next_run_text(job, rule, **options)  # type: ignore[arg-type]


def test_a_gate_write_keeps_the_phase_of_the_fire_already_stored() -> None:
    assert text_for(a_job()) == stamp(at(1800))


def test_a_pause_resumes_on_the_same_phase() -> None:
    paused = a_job(paused_until=stamp(at(7000)))

    assert text_for(paused) == stamp(at(1800 + 2 * AN_HOUR))


def test_lifting_the_pause_brings_the_nearest_slot_back() -> None:
    resumed = a_job(paused_until=None, next_run_at=stamp(at(1800 + 2 * AN_HOUR)))

    assert text_for(resumed) == stamp(at(1800))


def test_a_rules_pause_counts_as_well_as_the_jobs() -> None:
    rule = FakeRule(paused_until=stamp(at(7000)))

    assert text_for(a_job(), rule) == stamp(at(1800 + 2 * AN_HOUR))


@pytest.mark.parametrize(
    "gates",
    [
        pytest.param({"subsystem_enabled": False}, id="subsystem-switched-off"),
        pytest.param({"available": False}, id="subsystem-not-in-this-build"),
    ],
)
def test_a_closed_subsystem_gate_leaves_no_fire(gates: dict[str, Any]) -> None:
    assert text_for(a_job(), **gates) is None


def test_a_disabled_job_leaves_no_fire() -> None:
    assert text_for(a_job(is_enabled=False)) is None


def test_a_disabled_rule_leaves_no_fire() -> None:
    assert text_for(a_job(), FakeRule(is_enabled=False)) is None


def test_an_unreadable_schedule_leaves_no_fire() -> None:
    assert text_for(a_job(schedule_expression="not-a-number")) is None


def test_failures_delay_the_fire_of_a_job_that_can_be_disabled() -> None:
    job = a_job(
        schedule_expression="60",
        next_run_at=stamp(at(61)),
        consecutive_failures=4,
        failure_threshold=5,
    )

    delayed = text_for(job, now=at(61))

    assert delayed is not None and delayed > stamp(at(61 + 60))


def test_failures_never_delay_a_job_that_cannot_be_disabled() -> None:
    job = a_job(
        schedule_expression="60",
        next_run_at=stamp(at(61)),
        consecutive_failures=6,
        failure_threshold=None,
    )

    assert text_for(job, now=at(61)) == stamp(at(121))


def test_a_backed_off_interval_job_stays_on_the_phase_of_the_fire_already_stored() -> None:
    stored_phase = at(300)
    job = a_job(
        schedule_expression="60",
        next_run_at=stamp(stored_phase),
        consecutive_failures=4,
        failure_threshold=5,
    )

    retry = text_for(job, now=at(70), rng=lambda: 0.0)

    assert retry == stamp(at(360))


def test_a_job_without_a_rule_fires_on_whole_multiples_of_its_interval_in_unix_time() -> None:
    job = a_job(rule_id=None, schedule_expression="90", next_run_at=stamp(at(13)))

    assert text_for(job, now=at(20)) == stamp(at(90))


def test_a_job_with_a_rule_keeps_its_own_phase_off_the_grid() -> None:
    job = a_job(schedule_expression="90", next_run_at=stamp(at(113)))

    assert text_for(job, now=at(30)) == stamp(at(113))


def test_the_effective_pause_is_the_later_of_the_job_and_the_rule() -> None:
    job = a_job(paused_until=stamp(at(7000)))
    rule = FakeRule(paused_until=stamp(at(9000)))

    assert effective_pause(job, rule, NOW) == stamp(at(9000))


def test_a_pause_that_has_ended_is_not_in_force() -> None:
    job = a_job(paused_until=stamp(at(10)))

    assert effective_pause(job, None, NOW) is None


def test_job_gates_collects_the_rule_and_the_jobs_own_fields() -> None:
    job = a_job(paused_until=stamp(at(7000)))
    rule = FakeRule(is_enabled=False, paused_until=stamp(at(9000)))

    gates = job_gates(job, rule, subsystem_enabled=False, available=True)

    assert (gates.job_enabled, gates.job_paused_until) == (True, at(7000))
    assert (gates.rule_enabled, gates.rule_paused_until) == (False, at(9000))
    assert (gates.subsystem_enabled, gates.subsystem_available) == (False, True)


def test_a_job_without_a_rule_has_no_rule_gates() -> None:
    gates = job_gates(a_job(rule_id=None), None, subsystem_enabled=True, available=True)

    assert (gates.rule_enabled, gates.rule_paused_until) == (None, None)


@pytest.mark.parametrize("moment", [None, datetime(2026, 1, 1, 12, 0, 5).astimezone()])
def test_stored_and_parsed_moments_round_trip_and_pass_none_through(
    moment: datetime | None,
) -> None:
    assert moment_of(stored_of(moment)) == (
        None if moment is None else moment.replace(microsecond=0)
    )


def test_a_stored_time_is_second_resolution_utc_text() -> None:
    assert stored_of(NOW + timedelta(milliseconds=900)) == "2026-01-01T12:01:00Z"


ARMED_AT = stamp(at(300))


def a_webhook_job(**changes: object) -> ScheduledJob:
    options = {"schedule_kind": "webhook", "schedule_expression": "", "next_run_at": ARMED_AT}
    return a_job(**(options | changes))


def test_a_gate_write_keeps_the_armed_call_of_a_webhook_job_while_every_gate_is_open() -> None:
    assert text_for(a_webhook_job()) == ARMED_AT


def test_a_gate_write_never_gives_a_webhook_job_a_fire() -> None:
    assert text_for(a_webhook_job(next_run_at=None)) is None
    assert text_for(a_webhook_job(next_run_at=None, consecutive_failures=3)) is None


@pytest.mark.parametrize(
    ("job", "rule", "gates"),
    [
        pytest.param(a_webhook_job(is_enabled=False), None, {}, id="disabled"),
        pytest.param(a_webhook_job(paused_until=stamp(at(7000))), None, {}, id="job-paused"),
        pytest.param(a_webhook_job(), FakeRule(is_enabled=False), {}, id="rule-disabled"),
        pytest.param(a_webhook_job(), FakeRule(paused_until=stamp(at(7000))), {}, id="rule-paused"),
        pytest.param(a_webhook_job(), None, {"subsystem_enabled": False}, id="switched-off"),
        pytest.param(a_webhook_job(), None, {"available": False}, id="unavailable"),
    ],
)
def test_a_closed_gate_clears_the_armed_call_and_lifting_it_does_not_restore_it(
    job: ScheduledJob, rule: FakeRule | None, gates: dict[str, bool]
) -> None:
    cleared = text_for(job, rule, **gates)

    resumed = text_for(replace(job, next_run_at=cleared, paused_until=None, is_enabled=True))
    assert (cleared, resumed) == (None, None)
