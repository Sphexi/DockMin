"""Restart recovery, shutdown and suspend: every path that leaves a run unfinished."""

import asyncio
from typing import Any

import pytest

from app.scheduler.model import RunResult
from tests.scheduler.support import (
    REFRESHER,
    UPDATER,
    WEBHOOK,
    Rig,
    ScriptedHandler,
    at,
    blocks,
    detail_of,
    stamp,
)

MISSED_BY_TEN_MINUTES = -600
MISSED_FIRES_IN_TEN_MINUTES = 11
FIRST_FIRE_AFTER_RESTART = 60
LOOP_TURNS_FOR_A_CANCEL = 10


async def test_startup_interrupts_every_run_left_in_flight_and_records_one_event_for_each_rule_run(
    rig: Rig,
) -> None:
    rule = await rig.rule("flight")
    job = await rig.job(rule_id=rule, next_run=at(3600))
    run = await rig.start_run(job)
    scheduler = rig.scheduler(ScriptedHandler())

    await scheduler.start()

    [row] = rig.runs(job)
    [event] = rig.events("job_run_interrupted")
    assert (row["outcome"], row["reason"]) == ("interrupted", "process_stopped")
    assert "Last heartbeat" in row["message"]
    assert (event["actor_type"], event["actor_subsystem"], event["rule_id"]) == (
        "subsystem",
        "stack-restarter",
        rule,
    )
    assert event["job_run_id"] == run.id and event["result"] == "failure"
    assert detail_of(event) == {
        "reason": "process_stopped",
        "trigger": "schedule",
        "rule_id": rule,
        "rule_name": "flight",
    }


async def test_startup_interrupts_a_refresher_run_without_writing_an_event(rig: Rig) -> None:
    job = await rig.refresher_job(next_run=at(3600))
    await rig.start_run(job)
    scheduler = rig.scheduler(ScriptedHandler(REFRESHER, records_events=False))

    await scheduler.start()

    assert rig.outcomes(job) == [("interrupted", "process_stopped")]
    assert rig.events() == []


async def test_startup_records_an_event_for_an_interrupted_run_of_a_subsystem_with_no_handler(
    rig: Rig,
) -> None:
    job = await rig.rule_job("flight", subsystem=UPDATER, next_run=at(3600))
    await rig.start_run(job)
    scheduler = rig.scheduler()

    await scheduler.start()

    [event] = rig.events("job_run_interrupted")
    assert event["actor_subsystem"] == UPDATER


async def test_a_catch_up_job_runs_exactly_once_for_a_long_downtime(rig: Rig) -> None:
    job = await rig.rule_job("catch", next_run=at(MISSED_BY_TEN_MINUTES), catch_up=True)
    handler = ScriptedHandler()
    scheduler = rig.scheduler(handler)

    await scheduler.start()
    await scheduler.idle()
    await scheduler.tick()
    await scheduler.idle()

    [run] = rig.runs(job)
    assert (run["trigger"], run["outcome"]) == ("catch_up", "success")
    assert run["scheduled_for"] == stamp(at(MISSED_BY_TEN_MINUTES))
    assert len(handler.calls) == 1
    assert (await rig.job_row(job)).next_run_at == stamp(at(FIRST_FIRE_AFTER_RESTART))


async def test_a_job_without_catch_up_records_one_skipped_run_that_counts_the_missed_fires(
    rig: Rig,
) -> None:
    job = await rig.rule_job("miss", next_run=at(MISSED_BY_TEN_MINUTES))
    handler = ScriptedHandler()
    scheduler = rig.scheduler(handler)

    await scheduler.start()
    await scheduler.tick()

    [run] = rig.runs(job)
    assert (run["outcome"], run["reason"]) == ("skipped", "missed")
    assert run["message"] == "The application was not running at the scheduled time."
    assert detail_of(run) == {
        "first_missed": stamp(at(MISSED_BY_TEN_MINUTES)),
        "missed_count": MISSED_FIRES_IN_TEN_MINUTES,
    }
    assert handler.calls == []
    assert (await rig.job_row(job)).next_run_at == stamp(at(FIRST_FIRE_AFTER_RESTART))


async def test_a_job_that_is_not_yet_due_is_untouched_by_recovery(rig: Rig) -> None:
    job = await rig.rule_job("future", next_run=at(30))
    scheduler = rig.scheduler(ScriptedHandler())

    await scheduler.start()

    assert rig.runs(job) == []
    assert (await rig.job_row(job)).next_run_at == stamp(at(30))


async def test_recovery_interrupts_the_old_run_before_it_serves_the_missed_fire(rig: Rig) -> None:
    job = await rig.rule_job("both", next_run=at(MISSED_BY_TEN_MINUTES), catch_up=True)
    stale = await rig.start_run(job)
    scheduler = rig.scheduler(ScriptedHandler())

    await scheduler.start()
    await scheduler.idle()

    runs = rig.runs(job)
    assert runs[0]["id"] == stale.id
    assert [(row["trigger"], row["outcome"]) for row in runs] == [
        ("schedule", "interrupted"),
        ("catch_up", "success"),
    ]


async def test_recovery_clears_the_fire_of_a_job_with_no_handler_or_a_switched_off_subsystem(
    rig: Rig,
) -> None:
    no_handler = await rig.rule_job("none", subsystem="backup-syncer", next_run=at(10))
    switched_off = await rig.rule_job("off", subsystem=UPDATER, next_run=at(10))
    rig.setting("subsystem.container-updater.enabled", "false")
    handler = ScriptedHandler()
    updater = ScriptedHandler(UPDATER)
    scheduler = rig.scheduler(handler, updater)

    await scheduler.start()

    assert (await rig.job_row(no_handler)).next_run_at is None
    assert (await rig.job_row(switched_off)).next_run_at is None
    assert updater.calls == []


async def test_a_job_cleared_for_want_of_a_handler_is_scheduled_again_once_one_exists(
    rig: Rig,
) -> None:
    job = await rig.rule_job("r", next_run=at(600))
    without = rig.scheduler()
    await without.start()
    cleared = (await rig.job_row(job)).next_run_at
    await without.aclose()
    restored = rig.scheduler(ScriptedHandler())
    rig.clock.set(at(100))

    await restored.start()

    assert cleared is None
    assert (await rig.job_row(job)).next_run_at is not None


async def test_a_disabled_rules_job_stays_unscheduled_through_recovery(rig: Rig) -> None:
    rule = await rig.rule("off", enabled=False)
    job = await rig.job(rule_id=rule, next_run=None)
    scheduler = rig.scheduler(ScriptedHandler())

    await scheduler.start()

    assert (await rig.job_row(job)).next_run_at is None


async def test_a_scheduler_cannot_be_started_twice(rig: Rig) -> None:
    scheduler = rig.scheduler(ScriptedHandler())
    await scheduler.start()

    with pytest.raises(RuntimeError, match="already started"):
        await scheduler.start()


async def test_shutdown_interrupts_a_run_in_flight_and_records_its_event(rig: Rig) -> None:
    rule = await rig.rule("long")
    job = await rig.job(rule_id=rule, next_run=at(60))
    handler = ScriptedHandler()
    handler.behaviour = blocks(asyncio.Event())
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(60))
    await scheduler.tick()
    await handler.entered(1)

    await scheduler.aclose()

    [run] = rig.runs(job)
    [event] = rig.events("job_run_interrupted")
    assert (run["outcome"], run["reason"]) == ("interrupted", "shutdown")
    assert (event["actor_subsystem"], event["rule_id"], event["job_run_id"]) == (
        "stack-restarter",
        rule,
        run["id"],
    )
    assert detail_of(event)["reason"] == "shutdown"


async def test_a_closed_scheduler_claims_nothing(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(60))
    scheduler = rig.scheduler(ScriptedHandler())
    await scheduler.aclose()
    rig.clock.set(at(60))

    started = await scheduler.tick()

    assert started == 0
    assert rig.runs(job) == []


async def test_closing_a_started_scheduler_stops_its_loop(rig: Rig) -> None:
    scheduler = rig.scheduler(ScriptedHandler())
    await scheduler.start()
    await rig.sleeper.entered.wait()

    await scheduler.aclose()
    await scheduler.aclose()

    assert not any(task.get_name() == "scheduler-loop" for task in asyncio.all_tasks())


async def test_suspending_interrupts_runs_and_claims_nothing_until_it_ends(rig: Rig) -> None:
    job = await rig.rule_job("long", next_run=at(60))
    handler = ScriptedHandler()
    handler.behaviour = blocks(asyncio.Event())
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(60))
    await scheduler.tick()
    await handler.entered(1)

    async with scheduler.suspend():
        interrupted = rig.outcomes(job)
        rig.clock.set(at(120))
        claimed_while_suspended = await scheduler.tick()

    assert interrupted == [("interrupted", "suspended")]
    assert claimed_while_suspended == 0


async def test_the_fire_missed_while_suspended_is_recorded_as_missed_after_it_ends(
    rig: Rig,
) -> None:
    job = await rig.rule_job("r", next_run=at(60))
    handler = ScriptedHandler()
    scheduler = rig.scheduler(handler)
    async with scheduler.suspend():
        rig.clock.set(at(600))

    await scheduler.tick()

    assert rig.outcomes(job) == [("skipped", "missed")]
    assert handler.calls == []


async def test_a_scheduler_resumes_after_a_suspend_that_ended_in_an_error(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(60))
    scheduler = rig.scheduler(ScriptedHandler())
    with pytest.raises(RuntimeError, match="offline"):
        async with scheduler.suspend():
            raise RuntimeError("offline")
    rig.clock.set(at(60))

    started = await scheduler.tick()
    await scheduler.idle()

    assert started == 1
    assert rig.outcomes(job) == [("success", None)]


async def test_a_tick_under_way_when_suspend_begins_launches_nothing_more(rig: Rig) -> None:
    first = await rig.rule_job("first", next_run=at(30))
    second = await rig.rule_job("second", next_run=at(31))
    handler = ScriptedHandler()
    first_run_cancelled = asyncio.Event()

    async def hangs_until_cancelled(dispatch: Any) -> RunResult:
        try:
            await asyncio.Event().wait()
        finally:
            first_run_cancelled.set()
        raise AssertionError("unreachable")

    handler.behaviour = hangs_until_cancelled
    reached_second_job = asyncio.Event()
    release_tick = asyncio.Event()
    inside_suspend = asyncio.Event()
    leave_suspend = asyncio.Event()
    consulted: list[str] = []

    async def subsystem_enabled(name: str) -> bool:
        consulted.append(name)
        if len(consulted) == 2:
            reached_second_job.set()
            await release_tick.wait()
        return True

    scheduler = rig.scheduler(handler, subsystem_enabled=subsystem_enabled)
    rig.clock.set(at(40))

    async def suspended() -> None:
        async with scheduler.suspend():
            inside_suspend.set()
            await leave_suspend.wait()

    ticking = asyncio.create_task(scheduler.tick())
    await reached_second_job.wait()
    suspending = asyncio.create_task(suspended())
    for _ in range(LOOP_TURNS_FOR_A_CANCEL):
        await asyncio.sleep(0)
    cancelled_while_the_tick_was_still_serving = first_run_cancelled.is_set()
    release_tick.set()
    await ticking
    await inside_suspend.wait()

    assert cancelled_while_the_tick_was_still_serving is False
    assert len(handler.calls) == 1
    assert rig.outcomes(first) == [("interrupted", "suspended")]
    assert rig.outcomes(second) == [("skipped", "scheduler_stopped")]
    leave_suspend.set()
    await suspending


async def test_a_run_that_finishes_after_shutdown_began_is_not_followed_by_its_queued_fire(
    rig: Rig,
) -> None:
    job = await rig.rule_job("r", next_run=at(60), policy="queue")
    gate = asyncio.Event()
    handler = ScriptedHandler()
    handler.behaviour = blocks(gate)
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(60))
    await scheduler.tick()
    await handler.entered(1)
    rig.clock.set(at(120))
    await scheduler.tick()

    await scheduler.aclose()

    assert rig.outcomes(job) == [("interrupted", "shutdown")]
    assert len(handler.calls) == 1


async def test_a_handler_that_succeeds_just_before_shutdown_keeps_its_result(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(60))
    handler = ScriptedHandler()

    async def finishes_at_once(dispatch: object) -> RunResult:
        return RunResult("success")

    handler.behaviour = finishes_at_once
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(60))
    await scheduler.tick()
    await scheduler.idle()

    await scheduler.aclose()

    assert rig.outcomes(job) == [("success", None)]


async def test_the_startup_sweep_never_gives_an_on_call_job_a_fire(rig: Rig) -> None:
    job = await rig.webhook_job("hook")
    handler = ScriptedHandler(WEBHOOK)
    scheduler = rig.scheduler(handler)

    await scheduler.start()

    assert (await rig.job_row(job)).next_run_at is None
    assert rig.runs(job) == [] and handler.calls == []


async def test_a_call_armed_before_shutdown_runs_at_the_next_start_as_a_webhook_run(
    rig: Rig,
) -> None:
    job = await rig.webhook_job("hook", armed=at(60))
    rig.clock.set(at(60 + 3600))
    scheduler = rig.scheduler(ScriptedHandler(WEBHOOK))

    await scheduler.start()
    await scheduler.idle()

    [run] = rig.runs(job)
    assert (run["trigger"], run["outcome"]) == ("webhook", "success")
    assert run["scheduled_for"] == stamp(at(60))
    assert (await rig.job_row(job)).next_run_at is None
