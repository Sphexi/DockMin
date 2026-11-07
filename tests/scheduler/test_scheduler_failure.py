"""Failures: reasons stored, the counter, backoff, auto-disable and the time limit."""

from datetime import timedelta
from typing import Any

import pytest

from app.auth.errors import PermissionDenied
from app.db.repositories.clocked import parse_timestamp
from app.db.repositories.errors import RepositoryError
from app.notifications.catalogue import match
from app.scheduler.model import RunResult, RunSkipped
from tests.scheduler.support import (
    REFRESHER,
    UPDATER,
    Rig,
    ScriptedHandler,
    at,
    detail_of,
    fails,
    hangs,
    raises,
    succeeds,
)

THRESHOLD = 3
TINY_LIMIT = timedelta(milliseconds=10)
SECRET_TEXT = "SECRET=abc"


async def serve_at_fire_time(rig: Rig, scheduler: Any, job: int) -> None:
    row = await rig.job_row(job)
    assert row.next_run_at is not None
    rig.clock.set(parse_timestamp(row.next_run_at))
    await scheduler.tick()
    await scheduler.idle()


async def next_delay_after_failing(rig: Rig, scheduler: Any, job: int) -> int:
    """Serve the job at its fire time and return the seconds to its next fire."""
    await serve_at_fire_time(rig, scheduler, job)
    after = await rig.job_row(job)
    assert after.next_run_at is not None
    return int((parse_timestamp(after.next_run_at) - rig.clock()).total_seconds())


async def test_repeated_failures_delay_the_next_fire_by_the_larger_of_schedule_and_backoff(
    rig: Rig,
) -> None:
    job = await rig.rule_job("r", next_run=at(60), threshold=5)
    handler = ScriptedHandler()
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)

    delays = [await next_delay_after_failing(rig, scheduler, job) for _ in range(4)]

    row = await rig.job_row(job)
    assert delays == [60, 60, 120, 240]
    assert (row.consecutive_failures, row.is_enabled) == (4, True)


async def test_a_product_written_failure_stores_its_reason_and_message(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(60))
    handler = ScriptedHandler()
    handler.behaviour = fails("host_down", "The host did not answer.")
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(60))

    await scheduler.tick()
    await scheduler.idle()

    [run] = rig.runs(job)
    assert (run["outcome"], run["reason"], run["message"]) == (
        "failure",
        "host_down",
        "The host did not answer.",
    )


@pytest.mark.parametrize(
    ("error", "reason", "stored_message"),
    [
        pytest.param(ValueError(SECRET_TEXT), "value_error", None, id="arbitrary-exception"),
        pytest.param(KeyError(SECRET_TEXT), "key_error", None, id="key-error"),
        pytest.param(
            PermissionDenied("insufficient access"),
            "permission_denied",
            "insufficient access",
            id="product-authorisation-error",
        ),
        pytest.param(
            RepositoryError("duplicate"), "repository_error", "duplicate", id="repository"
        ),
    ],
)
async def test_only_a_product_authored_error_stores_its_text(
    rig: Rig, error: Exception, reason: str, stored_message: str | None
) -> None:
    job = await rig.rule_job("r", next_run=at(60))
    handler = ScriptedHandler()
    handler.behaviour = raises(error)
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(60))

    await scheduler.tick()
    await scheduler.idle()

    [run] = rig.runs(job)
    assert (run["reason"], run["message"]) == (reason, stored_message)


async def test_a_skipped_run_leaves_the_failure_counter(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(60))
    rig.sql("UPDATE scheduled_job SET consecutive_failures = 2 WHERE id = ?", (job,))
    handler = ScriptedHandler()
    handler.behaviour = raises(RunSkipped("suppressed", "A user acted recently.", {"object": "x"}))
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(60))

    await scheduler.tick()
    await scheduler.idle()

    [run] = rig.runs(job)
    assert (run["outcome"], run["reason"], detail_of(run)) == (
        "skipped",
        "suppressed",
        {"object": "x"},
    )
    assert (await rig.job_row(job)).consecutive_failures == 2


@pytest.mark.parametrize("outcome", ["success", "partial"])
async def test_a_success_or_partial_result_resets_the_counter(rig: Rig, outcome: str) -> None:
    job = await rig.rule_job("r", next_run=at(60))
    rig.sql("UPDATE scheduled_job SET consecutive_failures = 3 WHERE id = ?", (job,))
    handler = ScriptedHandler()

    async def reports(dispatch: Any) -> RunResult:
        return RunResult(outcome, attempted=1)  # type: ignore[arg-type]

    handler.behaviour = reports
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(60))

    await scheduler.tick()
    await scheduler.idle()

    assert (await rig.job_row(job)).consecutive_failures == 0


async def test_reaching_the_threshold_disables_the_job_and_records_who_and_why(rig: Rig) -> None:
    environment = rig.environment()
    owner = rig.user("owner")
    rule = await rig.rule("nightly", owner=owner, environment_id=environment)
    job = await rig.job(
        rule_id=rule, next_run=at(60), threshold=THRESHOLD, environment_id=environment
    )
    handler = ScriptedHandler()
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)

    for _ in range(THRESHOLD):
        await serve_at_fire_time(rig, scheduler, job)

    row = await rig.job_row(job)
    [event] = rig.events("scheduled_job_disabled")
    last_run = rig.runs(job)[-1]
    assert (row.is_enabled, row.next_run_at, row.consecutive_failures) == (False, None, THRESHOLD)
    assert (event["actor_type"], event["actor_subsystem"], event["result"]) == (
        "subsystem",
        "stack-restarter",
        "success",
    )
    assert (event["rule_id"], event["job_run_id"], event["environment_id"]) == (
        rule,
        last_run["id"],
        environment,
    )
    assert detail_of(event) == {
        "consecutive_failures": THRESHOLD,
        "threshold": THRESHOLD,
        "last_reason": "host_down",
        "rule_id": rule,
        "rule_name": "nightly",
    }


async def test_a_job_below_its_threshold_records_no_disable_event(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(60), threshold=THRESHOLD)
    handler = ScriptedHandler()
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)

    await serve_at_fire_time(rig, scheduler, job)

    assert rig.events("scheduled_job_disabled") == []


async def test_a_job_without_a_threshold_neither_backs_off_nor_disables_itself(rig: Rig) -> None:
    job = await rig.refresher_job(next_run=at(30))
    handler = ScriptedHandler(REFRESHER, records_events=False)
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)

    delay = await next_delay_after_failing(rig, scheduler, job)

    row = await rig.job_row(job)
    assert delay == 15
    assert (row.consecutive_failures, row.is_enabled) == (1, True)
    assert rig.events() == []


async def test_a_run_past_its_limit_fails_as_deadline_when_it_is_safe_to_cancel(rig: Rig) -> None:
    job = await rig.rule_job("r", subsystem=UPDATER, next_run=at(60))
    handler = ScriptedHandler(UPDATER, cancellation_safe=True, run_timeout=TINY_LIMIT)
    handler.behaviour = hangs()
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(60))

    await scheduler.tick()
    await scheduler.idle()

    assert rig.outcomes(job) == [("failure", "deadline")]


async def test_a_run_past_its_limit_whose_effects_are_unknown_is_recorded_as_outcome_unknown(
    rig: Rig,
) -> None:
    job = await rig.rule_job("r", subsystem=UPDATER, next_run=at(60))
    handler = ScriptedHandler(UPDATER, cancellation_safe=False, run_timeout=TINY_LIMIT)
    handler.behaviour = hangs()
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(60))

    await scheduler.tick()
    await scheduler.idle()

    [run] = rig.runs(job)
    assert (run["outcome"], run["reason"]) == ("failure", "outcome_unknown")
    assert "time_limit_seconds" in detail_of(run)


async def test_a_dry_run_that_fails_never_disables_the_job(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(3600), threshold=1)
    handler = ScriptedHandler()
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)
    run = await rig.start_run(job, "dry_run")

    scheduler.start_claimed(run, dry_run=True)
    await scheduler.idle()

    row = await rig.job_row(job)
    assert (row.is_enabled, row.consecutive_failures) == (True, 0)
    assert rig.events("scheduled_job_disabled") == []


@pytest.mark.parametrize("trigger", ["schedule", "manual"])
async def test_a_failed_rule_run_records_the_rule_the_run_and_the_reason(
    rig: Rig, trigger: str
) -> None:
    environment = rig.environment()
    rule = await rig.rule("nightly", owner=rig.user("owner"), environment_id=environment)
    job = await rig.job(
        rule_id=rule, next_run=at(3600), threshold=THRESHOLD, environment_id=environment
    )
    handler = ScriptedHandler()
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)
    run = await rig.start_run(job, trigger)

    scheduler.start_claimed(run)
    await scheduler.idle()

    [event] = rig.events("automation_run_failed")
    assert (event["actor_type"], event["actor_subsystem"], event["result"]) == (
        "subsystem",
        "stack-restarter",
        "failure",
    )
    assert (event["target_type"], event["target_id"]) == ("scheduled_job", str(job))
    assert (event["rule_id"], event["job_run_id"], event["environment_id"]) == (
        rule,
        run.id,
        environment,
    )
    assert detail_of(event) == {"rule_id": rule, "rule_name": "nightly", "reason": "host_down"}
    assert [
        raised.key for raised in match(event["action"], event["result"], event["actor_subsystem"])
    ] == ["automation_run_failed"]


async def test_each_failed_run_records_its_own_row(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(60), threshold=THRESHOLD)
    handler = ScriptedHandler()
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)

    await serve_at_fire_time(rig, scheduler, job)
    await serve_at_fire_time(rig, scheduler, job)

    events = rig.events("automation_run_failed")
    assert [event["job_run_id"] for event in events] == [run["id"] for run in rig.runs(job)]
    assert len(events) == 2


async def test_the_run_that_reaches_the_threshold_records_the_failure_before_the_disable(
    rig: Rig,
) -> None:
    job = await rig.rule_job("r", next_run=at(60), threshold=1)
    handler = ScriptedHandler()
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)

    await serve_at_fire_time(rig, scheduler, job)

    events = rig.events()
    assert [event["action"] for event in events] == [
        "automation_run_failed",
        "scheduled_job_disabled",
    ]
    assert events[0]["job_run_id"] == events[1]["job_run_id"]


async def test_a_failure_the_run_did_not_author_records_its_reason_but_not_its_text(
    rig: Rig,
) -> None:
    job = await rig.rule_job("r", next_run=at(60), threshold=THRESHOLD)
    handler = ScriptedHandler()
    handler.behaviour = raises(ValueError(SECRET_TEXT))
    scheduler = rig.scheduler(handler)

    await serve_at_fire_time(rig, scheduler, job)

    [event] = rig.events("automation_run_failed")
    assert detail_of(event)["reason"] == "value_error"
    assert SECRET_TEXT not in event["detail"]


@pytest.mark.parametrize(
    "behaviour",
    [
        pytest.param(succeeds(), id="success"),
        pytest.param(raises(RunSkipped("suppressed", "A user acted recently.")), id="skipped"),
    ],
)
async def test_a_run_that_did_not_fail_records_no_failure_row(rig: Rig, behaviour: Any) -> None:
    job = await rig.rule_job("r", next_run=at(60), threshold=THRESHOLD)
    handler = ScriptedHandler()
    handler.behaviour = behaviour
    scheduler = rig.scheduler(handler)

    await serve_at_fire_time(rig, scheduler, job)

    assert rig.events("automation_run_failed") == []


async def test_a_dry_run_that_fails_records_no_failure_row(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(3600), threshold=THRESHOLD)
    handler = ScriptedHandler()
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)
    run = await rig.start_run(job, "dry_run")

    scheduler.start_claimed(run, dry_run=True)
    await scheduler.idle()

    assert rig.events("automation_run_failed") == []


async def test_a_later_success_after_failures_restores_a_regular_schedule(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(60), threshold=5)
    handler = ScriptedHandler()
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)
    await next_delay_after_failing(rig, scheduler, job)
    handler.behaviour = succeeds()

    delay = await next_delay_after_failing(rig, scheduler, job)

    assert ((await rig.job_row(job)).consecutive_failures, delay) == (0, 60)
