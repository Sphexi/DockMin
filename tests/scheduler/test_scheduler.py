"""The scheduler loop: firing, slots, gates, manual runs, heartbeat and the infrastructure job."""

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.db.repositories.clocked import format_timestamp
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.scheduler.loop import (
    DETAIL_INVALID_MESSAGE,
    ERROR_PAUSE,
    MAX_CONCURRENT_RUNS,
    MAX_RUNS_PER_OWNER,
    MonotonicSleeper,
    Scheduler,
    SchedulerBusy,
)
from app.scheduler.model import RunFailed, RunResult
from app.scheduler.registry import HandlerRegistry
from tests.scheduler.support import (
    REFRESHER,
    UPDATER,
    WEBHOOK,
    BlockingSleeper,
    Clock,
    Rig,
    ScriptedHandler,
    at,
    blocks,
    detail_of,
    fails,
    raises,
    stamp,
)

FIRST_FIRE = 60
RESERVED_FOR_RULE_LESS = 1
A_LONG_INTERVAL = "3600"
AN_HOUR = 3600
SERVE_FAILURES_BEFORE_UNSCHEDULING = 3
JUST_PAST_THE_GRACE = 200
MANY_JOBS = 70


async def test_nothing_runs_before_the_fire_time(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(FIRST_FIRE))
    scheduler = rig.scheduler(ScriptedHandler())
    rig.clock.set(at(FIRST_FIRE - 1))

    started = await scheduler.tick()

    assert started == 0
    assert rig.runs(job) == []


async def test_a_due_job_runs_once_and_its_run_records_start_finish_and_outcome(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(FIRST_FIRE))
    handler = ScriptedHandler()

    async def takes_five_seconds(dispatch: Any) -> RunResult:
        rig.clock.jump(5)
        return RunResult("success", attempted=1)

    handler.behaviour = takes_five_seconds
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(FIRST_FIRE))

    started = await scheduler.tick()
    await scheduler.idle()

    [run] = rig.runs(job)
    assert started == 1
    assert (run["trigger"], run["outcome"]) == ("schedule", "success")
    assert run["scheduled_for"] == stamp(at(FIRST_FIRE))
    assert (run["started_at"], run["finished_at"]) == (
        stamp(at(FIRST_FIRE)),
        stamp(at(FIRST_FIRE + 5)),
    )
    assert (handler.calls[0].job.id, handler.calls[0].run.id) == (job, run["id"])
    assert rig.events() == []


async def test_the_next_fire_keeps_the_intervals_phase(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(FIRST_FIRE))
    scheduler = rig.scheduler(ScriptedHandler())
    rig.clock.set(at(FIRST_FIRE))

    await scheduler.tick()
    await scheduler.idle()

    assert (await rig.job_row(job)).next_run_at == stamp(at(2 * FIRST_FIRE))


async def test_a_fire_served_late_but_inside_the_grace_keeps_the_phase(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(FIRST_FIRE))
    scheduler = rig.scheduler(ScriptedHandler())
    rig.clock.set(at(FIRST_FIRE + 30))

    await scheduler.tick()
    await scheduler.idle()

    [run] = rig.runs(job)
    assert (run["trigger"], run["outcome"]) == ("schedule", "success")
    assert (await rig.job_row(job)).next_run_at == stamp(at(2 * FIRST_FIRE))


async def test_a_fire_missed_beyond_the_grace_without_catch_up_is_skipped_with_a_count(
    rig: Rig,
) -> None:
    job = await rig.rule_job("r", next_run=at(FIRST_FIRE))
    handler = ScriptedHandler()
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(FIRST_FIRE + 10 * 60 + 5))

    started = await scheduler.tick()

    [run] = rig.runs(job)
    assert started == 0 and handler.calls == []
    assert (run["outcome"], run["reason"]) == ("skipped", "missed")
    assert detail_of(run) == {"first_missed": stamp(at(FIRST_FIRE)), "missed_count": 11}


async def test_a_job_with_catch_up_runs_late_once_instead_of_skipping(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(FIRST_FIRE), catch_up=True)
    scheduler = rig.scheduler(ScriptedHandler())
    rig.clock.set(at(FIRST_FIRE + 10 * 60))

    await scheduler.tick()
    await scheduler.idle()

    [run] = rig.runs(job)
    assert (run["trigger"], run["outcome"]) == ("catch_up", "success")
    assert run["scheduled_for"] == stamp(at(FIRST_FIRE))


async def test_one_owner_has_at_most_two_runs_at_once_and_the_rest_wait_for_the_next_tick(
    rig: Rig,
) -> None:
    owner = rig.user("shared")
    gate = asyncio.Event()
    handler = ScriptedHandler()
    handler.behaviour = blocks(gate)
    jobs = [
        await rig.job(rule_id=await rig.rule(f"r{number}", owner=owner), next_run=at(30))
        for number in range(MAX_RUNS_PER_OWNER + 2)
    ]
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(30))

    first = await scheduler.tick()
    with pytest.raises(SchedulerBusy):
        scheduler.require_idle(jobs[-1] + 1, rule_bearing=True, owner_id=owner)
    gate.set()
    await scheduler.idle()
    second = await scheduler.tick()
    await scheduler.idle()

    assert (first, second) == (MAX_RUNS_PER_OWNER, 2)
    assert all(rig.outcomes(job) == [("success", None)] for job in jobs)


async def fill_every_slot(rig: Rig, gate: asyncio.Event) -> tuple[Scheduler, int]:
    """Start the most rule jobs allowed and the rule-less job; return the scheduler and its job."""
    handler = ScriptedHandler()
    handler.behaviour = blocks(gate)
    refresher = ScriptedHandler(REFRESHER, records_events=False)
    refresher.behaviour = blocks(gate)
    rig.bulk_rule_jobs(MAX_CONCURRENT_RUNS + 4, next_run=at(30))
    infrastructure = await rig.refresher_job(next_run=at(40))
    scheduler = rig.scheduler(handler, refresher)
    rig.clock.set(at(45))
    return scheduler, infrastructure


async def test_rule_jobs_leave_one_slot_for_a_rule_less_job(rig: Rig) -> None:
    gate = asyncio.Event()
    scheduler, infrastructure = await fill_every_slot(rig, gate)

    started = await scheduler.tick()

    assert started == MAX_CONCURRENT_RUNS
    assert scheduler.running(infrastructure)
    gate.set()
    await scheduler.idle()


async def test_a_manual_run_is_refused_while_every_slot_is_taken(rig: Rig) -> None:
    gate = asyncio.Event()
    scheduler, _ = await fill_every_slot(rig, gate)
    await scheduler.tick()

    with pytest.raises(SchedulerBusy, match="No run slot"):
        scheduler.require_idle(10_000, rule_bearing=False)

    gate.set()
    await scheduler.idle()


async def test_a_fire_deferred_for_capacity_beyond_the_grace_is_recorded_as_capacity(
    rig: Rig,
) -> None:
    gate = asyncio.Event()
    handler = ScriptedHandler()
    handler.behaviour = blocks(gate)
    jobs = rig.bulk_rule_jobs(MAX_CONCURRENT_RUNS + 1, next_run=at(30), expression=A_LONG_INTERVAL)
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(30))
    await scheduler.tick()
    deferred = [job for job in jobs if not rig.runs(job)]
    rig.clock.set(at(30 + JUST_PAST_THE_GRACE))

    await scheduler.tick()
    gate.set()
    await scheduler.idle()

    reasons = [row["reason"] for job in deferred for row in rig.runs(job)]
    assert len(deferred) == 2
    assert reasons == ["capacity", "capacity"]


async def test_due_jobs_beyond_the_first_batch_are_still_considered(rig: Rig) -> None:
    gate = asyncio.Event()
    handler = ScriptedHandler()
    handler.behaviour = blocks(gate)
    refresher = ScriptedHandler(REFRESHER, records_events=False)
    refresher.behaviour = blocks(gate)
    rig.bulk_rule_jobs(MANY_JOBS, next_run=at(30))
    infrastructure = await rig.refresher_job(next_run=at(50))
    scheduler = rig.scheduler(handler, refresher)
    rig.clock.set(at(55))

    await scheduler.tick()

    assert scheduler.running(infrastructure)
    gate.set()
    await scheduler.idle()


async def test_a_job_whose_subsystem_is_switched_off_is_cleared_without_running(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(30))
    rig.setting("subsystem.stack-restarter.enabled", "false")
    handler = ScriptedHandler()
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(30))

    started = await scheduler.tick()

    assert started == 0 and handler.calls == []
    assert rig.runs(job) == []
    assert (await rig.job_row(job)).next_run_at is None


async def test_a_job_with_no_handler_in_this_build_is_cleared_without_running(rig: Rig) -> None:
    job = await rig.rule_job("r", subsystem=UPDATER, next_run=at(30))
    scheduler = rig.scheduler(ScriptedHandler())
    rig.clock.set(at(30))

    started = await scheduler.tick()

    assert started == 0
    assert (await rig.job_row(job)).next_run_at is None


async def test_a_job_with_an_unreadable_schedule_is_cleared_instead_of_retried(rig: Rig) -> None:
    job = await rig.rule_job("r", expression="not-a-number", next_run=at(30))
    handler = ScriptedHandler()
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(30))

    await scheduler.tick()

    assert handler.calls == []
    assert (await rig.job_row(job)).next_run_at is None


async def test_a_loop_with_no_job_to_wait_for_sleeps_once_toward_the_next_hourly_upkeep(
    rig: Rig,
) -> None:
    await rig.job(rule_id=await rig.rule("off", enabled=False))
    scheduler = rig.scheduler(ScriptedHandler())

    await scheduler.start()
    await rig.sleeper.entered.wait()
    for _ in range(10):
        await asyncio.sleep(0)

    assert rig.sleeper.deadlines == [at(AN_HOUR)]


async def test_the_loop_wakes_for_the_hourly_upkeep_when_the_next_fire_is_later(rig: Rig) -> None:
    await rig.rule_job("later", next_run=at(2 * AN_HOUR))
    scheduler = rig.scheduler(ScriptedHandler())

    await scheduler.start()
    await rig.sleeper.entered.wait()

    assert rig.sleeper.deadlines[-1] == at(AN_HOUR)


async def test_the_loop_sleeps_toward_the_earliest_fire_and_a_wake_serves_a_due_job(
    rig: Rig,
) -> None:
    handler = ScriptedHandler()
    later = await rig.rule_job("later", next_run=at(120))
    sooner = await rig.rule_job("sooner", next_run=at(30))
    scheduler = rig.scheduler(handler)
    await scheduler.start()
    await rig.sleeper.entered.wait()
    waiting_for = rig.sleeper.deadlines[-1]

    rig.clock.set(at(30))
    scheduler.wake()
    await handler.entered(1)
    await scheduler.idle()

    assert waiting_for == at(30)
    assert rig.outcomes(sooner) == [("success", None)]
    assert rig.runs(later) == []


class ErrorPauseSleeper(BlockingSleeper):
    """Returns at once from the loop's pause after an error and waits for a wake-up otherwise."""

    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self._clock = clock
        self.serving = asyncio.Event()

    async def sleep_until(self, deadline: datetime | None, wake: asyncio.Event) -> None:
        self.deadlines.append(deadline)
        if deadline == self._clock() + ERROR_PAUSE:
            return
        self.serving.set()
        await wake.wait()


async def test_a_pass_of_the_loop_that_fails_is_logged_and_the_loop_goes_on_serving(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, logged: Any
) -> None:
    job = await rig.rule_job("r", next_run=at(30))
    handler = ScriptedHandler()
    sleeper = ErrorPauseSleeper(rig.clock)
    scheduler = rig.scheduler(handler, sleeper=sleeper)
    failures = iter([RuntimeError("upkeep broke")])
    original = scheduler.housekeeping.run_if_due

    async def breaks_once() -> object:
        for error in failures:
            raise error
        return await original()

    monkeypatch.setattr(scheduler.housekeeping, "run_if_due", breaks_once)
    await scheduler.start()
    await sleeper.serving.wait()

    rig.clock.set(at(30))
    scheduler.wake()
    await handler.entered(1)
    await scheduler.idle()

    assert [record["error"] for record in logged("scheduler_tick_failed")] == ["RuntimeError"]
    assert rig.outcomes(job) == [("success", None)]


async def test_the_default_sleeper_returns_at_once_for_a_past_deadline_or_a_set_wake_up(
    rig: Rig,
) -> None:
    sleeper = MonotonicSleeper(rig.clock)
    woken = asyncio.Event()
    woken.set()

    await sleeper.sleep_until(at(-5), asyncio.Event())
    await sleeper.sleep_until(at(3600), woken)


async def test_the_default_sleeper_gives_up_after_its_cap_when_there_is_no_deadline(
    rig: Rig,
) -> None:
    sleeper = MonotonicSleeper(rig.clock, cap=timedelta(milliseconds=5))

    await sleeper.sleep_until(None, asyncio.Event())


async def test_a_job_whose_serving_keeps_failing_is_unscheduled_after_three_attempts(
    rig: Rig, logged: Any
) -> None:
    class FailingRegistry(HandlerRegistry):
        def get(self, name: str) -> Any:
            raise RuntimeError("registry unavailable")

    job = await rig.rule_job("r", next_run=at(30))
    scheduler = Scheduler(rig.database, FailingRegistry(), clock=rig.clock, sleeper=rig.sleeper)
    rig.clock.set(at(30))

    for _ in range(SERVE_FAILURES_BEFORE_UNSCHEDULING):
        await scheduler.tick()

    assert (await rig.job_row(job)).next_run_at is None
    assert [r["job_id"] for r in logged("scheduler_job_unscheduled")] == [job]


async def test_a_manual_run_does_not_move_the_next_fire(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(3600))
    scheduler = rig.scheduler(ScriptedHandler())
    run = await rig.start_run(job, "manual")

    scheduler.start_claimed(run)
    assert scheduler.running(job)
    await scheduler.idle()

    assert rig.outcomes(job) == [("success", None)]
    assert (await rig.job_row(job)).next_run_at == stamp(at(3600))


async def test_a_dry_run_reaches_the_handler_flagged_and_failing_leaves_the_counter(
    rig: Rig,
) -> None:
    job = await rig.rule_job("r", next_run=at(3600))
    handler = ScriptedHandler()
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)
    run = await rig.start_run(job, "dry_run")

    scheduler.start_claimed(run, dry_run=True)
    await scheduler.idle()

    assert handler.calls[0].dry_run is True
    assert rig.outcomes(job) == [("failure", "host_down")]
    assert (await rig.job_row(job)).consecutive_failures == 0


async def test_a_dry_run_of_a_handler_that_cannot_do_one_is_skipped_unsupported(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(3600))
    handler = ScriptedHandler(supports_dry_run=False)
    scheduler = rig.scheduler(handler)
    run = await rig.start_run(job, "dry_run")

    scheduler.start_claimed(run, dry_run=True)
    await scheduler.idle()

    assert rig.outcomes(job) == [("skipped", "dry_run_unsupported")]
    assert handler.calls == []


async def test_a_failed_manual_run_counts_as_a_failure_but_keeps_the_fire_time(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(3600))
    handler = ScriptedHandler()
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)
    run = await rig.start_run(job, "manual")

    scheduler.start_claimed(run)
    await scheduler.idle()

    row = await rig.job_row(job)
    assert (row.consecutive_failures, row.next_run_at) == (1, stamp(at(3600)))


async def test_a_second_manual_run_while_one_is_running_is_skipped_as_an_overrun(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(3600))
    gate = asyncio.Event()
    handler = ScriptedHandler()
    handler.behaviour = blocks(gate)
    scheduler = rig.scheduler(handler)
    first = await rig.start_run(job, "manual")
    scheduler.start_claimed(first)
    await handler.entered(1)
    second = await rig.start_run(job, "manual")

    scheduler.start_claimed(second)
    await rig.settled(second.id)
    with pytest.raises(SchedulerBusy, match="already running"):
        scheduler.require_idle(job)
    gate.set()
    await scheduler.idle()

    by_id = {row["id"]: (row["outcome"], row["reason"]) for row in rig.runs(job)}
    assert by_id == {first.id: ("success", None), second.id: ("skipped", "overrun")}


async def test_a_run_cancelled_before_its_first_step_is_recorded_and_not_left_active(
    rig: Rig,
) -> None:
    job = await rig.rule_job("r", next_run=at(3600))
    handler = ScriptedHandler()
    scheduler = rig.scheduler(handler)
    run = await rig.start_run(job, "manual")

    scheduler.start_claimed(run)
    await scheduler.aclose()

    assert rig.outcomes(job) == [("interrupted", "shutdown")]
    assert not scheduler.running(job)
    assert handler.calls == []


async def test_a_run_that_reports_unstorable_detail_fails_and_leaves_no_run_in_flight(
    rig: Rig,
) -> None:
    job = await rig.rule_job("r", next_run=at(3600))
    handler = ScriptedHandler()

    async def reports_nested_detail(dispatch: Any) -> RunResult:
        return RunResult("success", detail={"nested": {"deeper": 1}})  # type: ignore[dict-item]

    handler.behaviour = reports_nested_detail
    scheduler = rig.scheduler(handler)
    run = await rig.start_run(job, "manual")

    scheduler.start_claimed(run)
    await scheduler.idle()

    [row] = rig.runs(job)
    assert (row["outcome"], row["reason"], row["message"]) == (
        "failure",
        "detail_invalid",
        DETAIL_INVALID_MESSAGE,
    )
    assert not scheduler.running(job)
    scheduler.require_idle(job)


async def test_the_heartbeat_stamps_every_run_in_flight(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(30))
    handler = ScriptedHandler()
    handler.behaviour = blocks(asyncio.Event())
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(30))
    await scheduler.tick()
    await handler.entered(1)
    rig.clock.jump(31)

    await scheduler.beat()

    [run] = rig.runs(job)
    assert (run["started_at"], run["heartbeat_at"]) == (stamp(at(30)), stamp(at(61)))


async def test_an_unexpected_error_in_a_handler_is_logged_by_class_and_ends_the_run_cleanly(
    rig: Rig, logged: Any
) -> None:
    job = await rig.rule_job("r", next_run=at(30))
    handler = ScriptedHandler()
    handler.behaviour = raises(KeyError("secret-value"))
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(30))

    await scheduler.tick()
    await scheduler.idle()

    [record] = logged("job_run_failed")
    assert record["error"] == "KeyError"
    assert "secret-value" not in str(record)
    assert rig.outcomes(job) == [("failure", "key_error")]


async def test_the_first_refresh_of_a_new_infrastructure_job_is_due_at_once(rig: Rig) -> None:
    scheduler = rig.scheduler(ScriptedHandler(REFRESHER, records_events=False))

    job = await scheduler.ensure_infrastructure_job(
        REFRESHER, kind="interval", expression="15", catch_up=True, overrun_policy="skip"
    )

    assert job.rule_id is None and job.failure_threshold is None
    assert job.next_run_at == format_timestamp(rig.clock())


async def test_a_new_infrastructure_job_without_catch_up_first_fires_one_interval_on(
    rig: Rig,
) -> None:
    scheduler = rig.scheduler(ScriptedHandler(REFRESHER, records_events=False))

    job = await scheduler.ensure_infrastructure_job(
        REFRESHER, kind="interval", expression="15", catch_up=False, overrun_policy="skip"
    )

    assert job.next_run_at == stamp(at(15))


async def test_a_new_infrastructure_job_without_a_handler_is_not_scheduled(rig: Rig) -> None:
    scheduler = rig.scheduler()

    job = await scheduler.ensure_infrastructure_job(
        REFRESHER, kind="interval", expression="15", catch_up=True, overrun_policy="skip"
    )

    assert job.next_run_at is None


async def test_asking_again_for_the_same_infrastructure_job_changes_nothing(rig: Rig) -> None:
    scheduler = rig.scheduler(ScriptedHandler(REFRESHER, records_events=False))
    options: dict[str, Any] = {
        "kind": "interval",
        "expression": "15",
        "catch_up": True,
        "overrun_policy": "skip",
    }
    first = await scheduler.ensure_infrastructure_job(REFRESHER, **options)

    again = await scheduler.ensure_infrastructure_job(REFRESHER, **options)

    assert again == first


async def test_a_changed_interval_rewrites_the_job_and_keeps_a_fire_that_is_already_due(
    rig: Rig,
) -> None:
    scheduler = rig.scheduler(ScriptedHandler(REFRESHER, records_events=False))
    first = await scheduler.ensure_infrastructure_job(
        REFRESHER, kind="interval", expression="15", catch_up=True, overrun_policy="skip"
    )

    changed = await scheduler.ensure_infrastructure_job(
        REFRESHER, kind="interval", expression="30", catch_up=True, overrun_policy="skip"
    )

    assert changed.id == first.id
    assert changed.schedule_expression == "30"
    assert changed.next_run_at == first.next_run_at


async def test_a_changed_interval_honours_a_pause_in_force(rig: Rig) -> None:
    scheduler = rig.scheduler(ScriptedHandler(REFRESHER, records_events=False))
    job = await scheduler.ensure_infrastructure_job(
        REFRESHER, kind="interval", expression="15", catch_up=True, overrun_policy="skip"
    )
    rig.sql(
        "UPDATE scheduled_job SET paused_until = ?, next_run_at = ? WHERE id = ?",
        (stamp(at(600)), stamp(at(100)), job.id),
    )

    changed = await scheduler.ensure_infrastructure_job(
        REFRESHER, kind="interval", expression="30", catch_up=True, overrun_policy="skip"
    )

    assert changed.next_run_at is not None and changed.next_run_at >= stamp(at(600))


async def test_a_changed_interval_keeps_a_pending_catch_up_fire(rig: Rig) -> None:
    scheduler = rig.scheduler(ScriptedHandler(REFRESHER, records_events=False))
    job = await scheduler.ensure_infrastructure_job(
        REFRESHER, kind="interval", expression="15", catch_up=True, overrun_policy="skip"
    )
    rig.sql("UPDATE scheduled_job SET next_run_at = ? WHERE id = ?", (stamp(at(-60)), job.id))

    changed = await scheduler.ensure_infrastructure_job(
        REFRESHER, kind="interval", expression="45", catch_up=True, overrun_policy="skip"
    )

    assert changed.next_run_at == stamp(at(-60))


@pytest.mark.parametrize("subsystem", ["stack-restarter", "activity-log-pruner"])
async def test_only_a_subsystem_that_owns_a_job_and_no_rules_may_have_an_infrastructure_job(
    rig: Rig, subsystem: str
) -> None:
    scheduler = rig.scheduler()

    with pytest.raises(ValueError, match="not a subsystem without rules"):
        await scheduler.ensure_infrastructure_job(
            subsystem,
            kind="interval",
            expression="60",
            catch_up=False,
            overrun_policy="skip",
        )


WEBHOOK_CAP = 3
SPACING = 60
ARMED_AT = 10


async def test_arming_sets_the_later_of_now_and_the_not_before_time_and_only_once(rig: Rig) -> None:
    job = await rig.webhook_job("hook")
    rig.clock.set(at(ARMED_AT))

    first = await rig.arm(job, at(ARMED_AT + SPACING))
    second = await rig.arm(job, at(ARMED_AT))

    assert (first, second) == (True, False)
    assert (await rig.job_row(job)).next_run_at == stamp(at(ARMED_AT + SPACING))


async def test_arming_sets_now_when_the_not_before_time_has_passed(rig: Rig) -> None:
    job = await rig.webhook_job("hook")
    rig.clock.set(at(ARMED_AT))

    await rig.arm(job, at(0))

    assert (await rig.job_row(job)).next_run_at == stamp(at(ARMED_AT))


@pytest.mark.parametrize(
    "closing",
    [
        pytest.param("UPDATE scheduled_job SET is_enabled = 0 WHERE id = ?", id="job-disabled"),
        pytest.param(
            "UPDATE scheduled_job SET paused_until = '2099-01-01T00:00:00Z' WHERE id = ?",
            id="job-paused",
        ),
        pytest.param(
            "UPDATE automation_rule SET is_enabled = 0 "
            "WHERE id = (SELECT rule_id FROM scheduled_job WHERE id = ?)",
            id="rule-disabled",
        ),
        pytest.param(
            "UPDATE automation_rule SET paused_until = '2099-01-01T00:00:00Z' "
            "WHERE id = (SELECT rule_id FROM scheduled_job WHERE id = ?)",
            id="rule-paused",
        ),
    ],
)
async def test_arming_is_refused_while_a_gate_is_closed(rig: Rig, closing: str) -> None:
    job = await rig.webhook_job("hook")
    rig.sql(closing, (job,))

    armed = await rig.arm(job)

    assert armed is False
    assert (await rig.job_row(job)).next_run_at is None


async def test_arming_is_refused_for_a_job_that_is_not_on_call(rig: Rig) -> None:
    job = await rig.rule_job("timed")

    assert await rig.arm(job) is False


async def test_the_start_of_the_newest_run_is_read_back(rig: Rig) -> None:
    job = await rig.webhook_job("hook")
    rig.clock.set(at(ARMED_AT))
    never_run = await rig.job_row(job)
    await rig.start_run(job, "webhook")
    async with rig.database.session() as db:
        repository = ScheduledJobRepository(db, rig.clock)
        started = await repository.last_run_started(job)
        absent = await repository.last_run_started(job + 1)

    assert never_run.last_run_at is None
    assert (started, absent) == (stamp(at(ARMED_AT)), None)


async def test_an_armed_job_starts_one_run_with_the_webhook_trigger_and_the_armed_time(
    rig: Rig,
) -> None:
    job = await rig.webhook_job("hook")
    handler = ScriptedHandler(WEBHOOK)
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(ARMED_AT))
    await rig.arm(job)
    rig.clock.set(at(ARMED_AT + AN_HOUR))

    started = await scheduler.tick()
    await scheduler.idle()
    again = await scheduler.tick()

    [run] = rig.runs(job)
    assert (started, again) == (1, 0)
    assert (run["trigger"], run["outcome"]) == ("webhook", "success")
    assert run["scheduled_for"] == stamp(at(ARMED_AT))
    assert (await rig.job_row(job)).next_run_at is None


async def test_an_armed_time_still_ahead_waits_for_it(rig: Rig) -> None:
    job = await rig.webhook_job("hook")
    scheduler = rig.scheduler(ScriptedHandler(WEBHOOK))
    rig.clock.set(at(ARMED_AT))
    await rig.arm(job, at(ARMED_AT + SPACING))

    early = await scheduler.tick()
    rig.clock.set(at(ARMED_AT + SPACING))
    on_time = await scheduler.tick()
    await scheduler.idle()

    assert (early, on_time) == (0, 1)
    assert rig.runs(job)[0]["scheduled_for"] == stamp(at(ARMED_AT + SPACING))


async def test_a_call_armed_during_its_own_run_waits_and_starts_when_that_run_ends(
    rig: Rig,
) -> None:
    gate = asyncio.Event()
    handler = ScriptedHandler(WEBHOOK)
    handler.behaviour = blocks(gate)
    job = await rig.webhook_job("hook", armed=at(ARMED_AT))
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(ARMED_AT))
    await scheduler.start()
    await handler.entered(1)
    await rig.loop_passes(1)

    armed = await rig.arm(job)
    scheduler.poke()
    await rig.loop_passes(2)
    rows_while_running = len(rig.runs(job))
    stored_while_running = (await rig.job_row(job)).next_run_at
    gate.set()
    await handler.entered(2)
    await scheduler.idle()

    assert armed is True
    assert (rows_while_running, stored_while_running) == (1, stamp(at(ARMED_AT)))
    assert [run["trigger"] for run in rig.runs(job)] == ["webhook", "webhook"]
    assert rig.runs(job)[1]["scheduled_for"] == stamp(at(ARMED_AT))


@pytest.mark.parametrize(
    ("arm_during_run", "expected"),
    [
        pytest.param(False, None, id="nothing-armed"),
        pytest.param(True, stamp(at(ARMED_AT)), id="armed-during-the-run"),
    ],
)
async def test_a_failed_webhook_run_never_backs_off_or_moves_an_armed_call(
    rig: Rig, arm_during_run: bool, expected: str | None
) -> None:
    job = await rig.webhook_job("hook", armed=at(ARMED_AT))
    handler = ScriptedHandler(WEBHOOK)

    async def fails_after_arming(dispatch: Any) -> RunResult:
        if arm_during_run:
            await rig.arm(job)
        raise RunFailed("host_down", "The host did not answer.")

    handler.behaviour = fails_after_arming
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(ARMED_AT))

    await scheduler.tick()
    await scheduler.idle()

    stored = await rig.job_row(job)
    assert (stored.consecutive_failures, stored.next_run_at) == (1, expected)


async def test_no_more_runs_than_the_subsystem_cap_are_in_flight_and_a_fourth_waits_then_runs(
    rig: Rig,
) -> None:
    jobs = [await rig.webhook_job(f"hook-{number}", armed=at(ARMED_AT)) for number in range(4)]
    gates = {job: asyncio.Event() for job in jobs}
    handler = ScriptedHandler(WEBHOOK, max_concurrent_runs=WEBHOOK_CAP)

    async def held(dispatch: Any) -> RunResult:
        await gates[dispatch.job.id].wait()
        return RunResult("success", attempted=1)

    handler.behaviour = held
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(ARMED_AT))
    await scheduler.start()
    await handler.entered(WEBHOOK_CAP)
    await rig.loop_passes(1)

    calls_at_the_cap = len(handler.calls)
    rows_for_the_fourth = rig.runs(jobs[3])
    gates[jobs[0]].set()
    await handler.entered(WEBHOOK_CAP + 1)
    for gate in gates.values():
        gate.set()
    await scheduler.idle()

    assert calls_at_the_cap == WEBHOOK_CAP
    assert rows_for_the_fourth == []
    assert [rig.outcomes(job) for job in jobs] == [[("success", None)]] * 4
    assert rig.runs(jobs[3])[0]["trigger"] == "webhook"


async def test_a_manual_run_is_refused_while_its_subsystem_is_at_the_cap(rig: Rig) -> None:
    jobs = [
        await rig.webhook_job(f"hook-{number}", armed=at(ARMED_AT))
        for number in range(WEBHOOK_CAP + 1)
    ]
    gate = asyncio.Event()
    handler = ScriptedHandler(WEBHOOK, max_concurrent_runs=WEBHOOK_CAP)
    handler.behaviour = blocks(gate)
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(ARMED_AT))
    await scheduler.start()
    await handler.entered(WEBHOOK_CAP)
    await rig.loop_passes(1)

    with pytest.raises(SchedulerBusy, match="subsystem"):
        scheduler.require_idle(jobs[-1], rule_bearing=True, subsystem=WEBHOOK)
    scheduler.require_idle(jobs[-1], rule_bearing=True)

    gate.set()
    await scheduler.idle()


async def test_a_subsystem_without_a_cap_is_limited_only_by_the_shared_slots(rig: Rig) -> None:
    jobs = [await rig.webhook_job(f"hook-{number}", armed=at(ARMED_AT)) for number in range(4)]
    handler = ScriptedHandler(WEBHOOK)
    handler.behaviour = blocks(asyncio.Event())
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(ARMED_AT))

    started = await scheduler.tick()
    await handler.entered(len(jobs))

    assert started == len(jobs)
