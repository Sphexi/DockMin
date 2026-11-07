"""Bringing a job's next fire forward: every gate stays closed, and nothing queued is dropped."""

import asyncio

import pytest

from app.scheduler.registry import HandlerRegistry
from tests.scheduler.support import (
    RESTARTER,
    UPDATER,
    Rig,
    ScriptedHandler,
    at,
    blocks,
    stamp,
)

NOW = 600
HOUR = "3600"
LATER = 3600
FAR = 7200
DEADLINE = 20


async def test_a_scheduled_job_is_moved_to_now_and_the_next_tick_runs_it_and_keeps_its_phase(
    rig: Rig,
) -> None:
    job = await rig.rule_job("r", next_run=at(LATER), expression=HOUR)
    scheduler = rig.scheduler(ScriptedHandler())
    rig.clock.set(at(NOW))

    assert await scheduler.tick() == 0
    assert await scheduler.expedite(job) is True
    moved = (await rig.job_row(job)).next_run_at
    assert await scheduler.tick() == 1
    await scheduler.idle()

    [run] = rig.runs(job)
    assert moved == stamp(at(NOW))
    assert (run["trigger"], run["outcome"], run["scheduled_for"]) == (
        "schedule",
        "success",
        stamp(at(NOW)),
    )
    assert (await rig.job_row(job)).next_run_at == stamp(at(NOW + LATER))


async def test_expediting_wakes_the_loop_without_dropping_a_queued_fire(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(LATER), expression=HOUR)
    scheduler = rig.scheduler(ScriptedHandler())
    rig.clock.set(at(NOW))
    scheduler._queued[job + 100] = at(1)
    scheduler._wake.clear()

    assert await scheduler.expedite(job) is True

    assert scheduler._wake.is_set()
    assert scheduler._queued == {job + 100: at(1)}


async def test_no_gate_is_opened_by_bringing_a_job_forward(rig: Rig) -> None:
    gated = await rig.rule_job("gated", next_run=None)
    disabled = await rig.rule_job("disabled", next_run=at(LATER))
    due = await rig.rule_job("due", next_run=at(10))
    backing_off = await rig.rule_job("backoff", next_run=at(LATER))
    paused = await rig.rule_job("paused", next_run=at(FAR))
    rule_paused = await rig.rule_job("rule-paused", next_run=at(FAR))
    rule_off = await rig.rule("off", enabled=False)
    off_job = await rig.job(rule_id=rule_off, next_run=at(FAR))
    rig.sql("UPDATE scheduled_job SET is_enabled = 0 WHERE id = ?", (disabled,))
    rig.sql("UPDATE scheduled_job SET consecutive_failures = 2 WHERE id = ?", (backing_off,))
    rig.sql("UPDATE scheduled_job SET paused_until = ? WHERE id = ?", (stamp(at(FAR)), paused))
    rig.sql(
        "UPDATE automation_rule SET paused_until = ? "
        "WHERE id = (SELECT rule_id FROM scheduled_job WHERE id = ?)",
        (stamp(at(FAR)), rule_paused),
    )
    scheduler = rig.scheduler(ScriptedHandler())
    rig.clock.set(at(NOW))

    results = [
        await scheduler.expedite(job)
        for job in (gated, disabled, due, backing_off, paused, rule_paused, off_job, 99999)
    ]

    assert results == [False] * 8
    assert (await rig.job_row(gated)).next_run_at is None
    assert (await rig.job_row(due)).next_run_at == stamp(at(10))
    assert (await rig.job_row(backing_off)).next_run_at == stamp(at(LATER))
    assert (await rig.job_row(paused)).next_run_at == stamp(at(FAR))
    assert (await rig.job_row(rule_paused)).next_run_at == stamp(at(FAR))
    assert (await rig.job_row(off_job)).next_run_at == stamp(at(FAR))


async def test_a_job_that_is_running_is_left_alone(rig: Rig) -> None:
    job = await rig.rule_job("r", next_run=at(NOW), expression=HOUR)
    gate = asyncio.Event()
    handler = ScriptedHandler()
    handler.behaviour = blocks(gate)
    scheduler = rig.scheduler(handler)
    rig.clock.set(at(NOW))
    async with asyncio.timeout(DEADLINE):
        await scheduler.tick()
        await handler.entered(1)

        moved = await scheduler.expedite(job)
        gate.set()
        await scheduler.idle()

    assert moved is False
    assert (await rig.job_row(job)).next_run_at == stamp(at(NOW + LATER))


async def test_a_suspended_or_closed_scheduler_moves_nothing(rig: Rig) -> None:
    suspended = await rig.rule_job("s", next_run=at(5000), expression=HOUR)
    closed = await rig.rule_job("c", next_run=at(6000), expression=HOUR)
    scheduler = rig.scheduler(ScriptedHandler())
    rig.clock.set(at(NOW))

    async with scheduler.suspend():
        during = await scheduler.expedite(suspended)
    await scheduler.aclose()
    after = await scheduler.expedite(closed)

    assert (during, after) == (False, False)
    assert (await rig.job_row(suspended)).next_run_at == stamp(at(5000))
    assert (await rig.job_row(closed)).next_run_at == stamp(at(6000))


async def test_the_registry_replaces_a_handler_adds_a_new_one_and_refuses_an_unknown_name() -> None:
    registry = HandlerRegistry()
    first, second = ScriptedHandler(RESTARTER), ScriptedHandler(RESTARTER)
    registry.register(first)
    with pytest.raises(ValueError):
        registry.register(second)

    registry.replace(second)
    updater = ScriptedHandler(UPDATER)
    registry.replace(updater)

    assert registry.get(RESTARTER) is second
    assert registry.get(UPDATER) is updater
    assert registry.names() == {RESTARTER, UPDATER}
    with pytest.raises(ValueError):
        registry.replace(ScriptedHandler("not-a-subsystem"))
