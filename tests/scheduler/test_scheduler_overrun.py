"""Overrun policies: skip, queue (with its collapse and drops) and cancel_previous."""

import asyncio

import pytest

from app.scheduler.loop import Scheduler
from tests.scheduler.support import Rig, ScriptedHandler, at, blocks, detail_of, stamp

FIRST_FIRE = 60
SECOND_FIRE = 120
THIRD_FIRE = 180


async def start_first_run(
    rig: Rig, handler: ScriptedHandler, *, policy: str, gate: asyncio.Event
) -> tuple[int, int, Scheduler]:
    """Start a run that stays in flight; return the job, the run and the scheduler."""
    handler.behaviour = blocks(gate)
    job = await rig.rule_job("r", next_run=at(FIRST_FIRE), policy=policy)
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(FIRST_FIRE))
    await scheduler.tick()
    await handler.entered(1)
    return job, rig.runs(job)[0]["id"], scheduler


async def test_the_skip_policy_records_a_skipped_overrun_naming_the_running_run(rig: Rig) -> None:
    gate = asyncio.Event()
    handler = ScriptedHandler()
    job, first, scheduler = await start_first_run(rig, handler, policy="skip", gate=gate)
    rig.clock.set(at(SECOND_FIRE))

    await scheduler.tick()

    skipped = rig.runs(job)[1]
    assert (skipped["outcome"], skipped["reason"]) == ("skipped", "overrun")
    assert detail_of(skipped) == {"running_run_id": first}
    gate.set()
    await scheduler.idle()
    assert rig.runs(job)[0]["outcome"] == "success"


async def test_the_queue_policy_holds_one_fire_without_a_row_and_collapses_the_next(
    rig: Rig,
) -> None:
    gate = asyncio.Event()
    handler = ScriptedHandler()
    job, _, scheduler = await start_first_run(rig, handler, policy="queue", gate=gate)
    rig.clock.set(at(SECOND_FIRE))
    await scheduler.tick()
    rows_while_queued = len(rig.runs(job))
    rig.clock.set(at(THIRD_FIRE))

    await scheduler.tick()

    collapsed = rig.runs(job)[1]
    assert rows_while_queued == 1
    assert (collapsed["outcome"], collapsed["reason"]) == ("skipped", "overrun_collapsed")
    gate.set()
    await scheduler.idle()


async def test_the_queued_fire_runs_as_soon_as_the_running_run_ends(rig: Rig) -> None:
    gate = asyncio.Event()
    handler = ScriptedHandler()
    job, _, scheduler = await start_first_run(rig, handler, policy="queue", gate=gate)
    rig.clock.set(at(SECOND_FIRE))
    await scheduler.tick()

    gate.set()
    await scheduler.idle()

    queued = rig.runs(job)[1]
    assert [row["trigger"] for row in rig.runs(job)] == ["schedule", "queued"]
    assert (queued["scheduled_for"], queued["outcome"]) == (stamp(at(SECOND_FIRE)), "success")


@pytest.mark.parametrize(
    "gate_change",
    [
        pytest.param("UPDATE automation_rule SET is_enabled = 0", id="rule-disabled"),
        pytest.param(
            "UPDATE automation_rule SET paused_until = '2030-01-01T00:00:00Z'", id="rule-paused"
        ),
        pytest.param(
            "INSERT INTO setting (key, value_type, value, updated_at) VALUES "
            "('subsystem.stack-restarter.enabled', 'boolean', 'false', '2026-01-01T00:00:00Z')",
            id="subsystem-disabled",
        ),
    ],
)
async def test_a_queued_fire_is_dropped_not_run_when_the_job_can_no_longer_run(
    rig: Rig, gate_change: str
) -> None:
    gate = asyncio.Event()
    handler = ScriptedHandler()
    job, _, scheduler = await start_first_run(rig, handler, policy="queue", gate=gate)
    rig.clock.set(at(SECOND_FIRE))
    await scheduler.tick()
    rig.sql(gate_change)

    gate.set()
    await scheduler.idle()

    assert rig.outcomes(job) == [("success", None), ("skipped", "queue_dropped")]
    assert len(handler.calls) == 1


async def test_a_gate_write_drops_the_queued_fire_and_the_next_pass_records_it(rig: Rig) -> None:
    gate = asyncio.Event()
    handler = ScriptedHandler()
    job, _, scheduler = await start_first_run(rig, handler, policy="queue", gate=gate)
    rig.clock.set(at(SECOND_FIRE))
    await scheduler.tick()

    scheduler.wake()
    gate.set()
    await scheduler.idle()
    rig.clock.set(at(SECOND_FIRE + 5))
    await scheduler.tick()

    assert rig.outcomes(job) == [("success", None), ("skipped", "queue_dropped")]
    assert [row["trigger"] for row in rig.runs(job)] == ["schedule", "queued"]


async def test_cancel_previous_interrupts_the_running_run_with_an_event_and_starts_the_new_one(
    rig: Rig,
) -> None:
    gate = asyncio.Event()
    handler = ScriptedHandler(cancellation_safe=True)
    job, first, scheduler = await start_first_run(rig, handler, policy="cancel_previous", gate=gate)
    rig.clock.set(at(SECOND_FIRE))

    await scheduler.tick()
    await handler.entered(2)

    previous, current = rig.runs(job)
    [event] = rig.events("job_run_interrupted")
    assert (previous["outcome"], previous["reason"]) == ("interrupted", "superseded")
    assert (current["outcome"], current["trigger"]) == (None, "schedule")
    assert (event["actor_subsystem"], event["job_run_id"], event["result"]) == (
        "stack-restarter",
        first,
        "failure",
    )
    assert event["rule_id"] == (await rig.job_row(job)).rule_id
    gate.set()
    await scheduler.idle()
    assert rig.runs(job)[1]["outcome"] == "success"


async def test_cancel_previous_degrades_to_a_skipped_overrun_for_a_handler_not_safe_to_cancel(
    rig: Rig,
) -> None:
    gate = asyncio.Event()
    handler = ScriptedHandler(cancellation_safe=False)
    job, first, scheduler = await start_first_run(rig, handler, policy="cancel_previous", gate=gate)
    rig.clock.set(at(SECOND_FIRE))

    await scheduler.tick()

    skipped = rig.runs(job)[1]
    assert (skipped["outcome"], skipped["reason"]) == ("skipped", "overrun")
    assert detail_of(skipped) == {"running_run_id": first}
    gate.set()
    await scheduler.idle()
    assert rig.runs(job)[0]["outcome"] == "success"
