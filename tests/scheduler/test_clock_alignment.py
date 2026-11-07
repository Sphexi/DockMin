"""Jobs without a rule fire on the clock; rule jobs keep their own phase, even after a retry."""

import asyncio
from datetime import datetime
from typing import Any

import pytest

from app.db.repositories.clocked import parse_timestamp
from app.scheduler.loop import Scheduler
from tests.scheduler.support import (
    REFRESHER,
    Rig,
    ScriptedHandler,
    at,
    blocks,
    fails,
    stamp,
    succeeds,
)

MINUTE = 60
NINETY_SECONDS = 90
RETRIED_AFTER_FAILURES = 3
SERVED_TEN_SECONDS_LATE = 70
BACKOFF_FROM_THE_LATE_SERVE = 240
RETRY_ON_THE_ORIGINAL_PHASE = 360
SLOT_AFTER_THE_RETRY = 420


def refresher_options(expression: str, *, catch_up: bool = False) -> dict[str, Any]:
    return {
        "kind": "interval",
        "expression": expression,
        "catch_up": catch_up,
        "overrun_policy": "skip",
    }


def a_refresher(rig: Rig) -> tuple[Scheduler, ScriptedHandler]:
    handler = ScriptedHandler(REFRESHER, records_events=False)
    return rig.scheduler(handler), handler


async def serve_when_due(rig: Rig, scheduler: Scheduler, job: int) -> None:
    due = (await rig.job_row(job)).next_run_at
    assert due is not None
    rig.clock.set(parse_timestamp(due))
    await scheduler.tick()
    await scheduler.idle()


async def stored_next_fire(rig: Rig, job: int) -> str | None:
    return (await rig.job_row(job)).next_run_at


async def test_refreshes_every_sixty_seconds_start_at_the_top_of_each_minute(rig: Rig) -> None:
    scheduler, handler = a_refresher(rig)
    rig.clock.set(at(23))
    job = await scheduler.ensure_infrastructure_job(REFRESHER, **refresher_options("60"))

    await serve_when_due(rig, scheduler, job.id)
    await serve_when_due(rig, scheduler, job.id)
    await serve_when_due(rig, scheduler, job.id)

    assert [call.now for call in handler.calls] == [at(MINUTE), at(2 * MINUTE), at(3 * MINUTE)]


async def test_a_refresh_served_late_still_schedules_the_next_at_the_top_of_a_minute(
    rig: Rig,
) -> None:
    scheduler, _ = a_refresher(rig)
    job = await scheduler.ensure_infrastructure_job(
        REFRESHER,
        **refresher_options("60", catch_up=False),
    )
    rig.clock.set(at(MINUTE + 40))

    await scheduler.tick()
    await scheduler.idle()

    assert await stored_next_fire(rig, job.id) == stamp(at(2 * MINUTE))


async def test_an_interval_that_does_not_divide_a_minute_fires_on_multiples_in_unix_time(
    rig: Rig,
) -> None:
    scheduler, handler = a_refresher(rig)
    rig.clock.set(at(20))
    job = await scheduler.ensure_infrastructure_job(
        REFRESHER,
        **refresher_options("90"),
    )

    await serve_when_due(rig, scheduler, job.id)
    await serve_when_due(rig, scheduler, job.id)
    await serve_when_due(rig, scheduler, job.id)

    assert [call.now for call in handler.calls] == [
        at(NINETY_SECONDS),
        at(2 * NINETY_SECONDS),
        at(3 * NINETY_SECONDS),
    ]
    assert await stored_next_fire(rig, job.id) == stamp(at(4 * NINETY_SECONDS))


async def test_a_fire_that_arrives_while_a_run_is_going_is_skipped_and_the_next_stays_on_the_grid(
    rig: Rig,
) -> None:
    scheduler, handler = a_refresher(rig)
    handler.behaviour = blocks(asyncio.Event())
    job = await scheduler.ensure_infrastructure_job(
        REFRESHER,
        **refresher_options("60"),
    )
    rig.clock.set(at(MINUTE))
    await scheduler.tick()
    await handler.entered(1)
    rig.clock.set(at(2 * MINUTE + 30))

    await scheduler.tick()

    assert [outcome for outcome, _ in rig.outcomes(job.id)] == ["skipped"]
    assert await stored_next_fire(rig, job.id) == stamp(at(3 * MINUTE))


async def test_a_stored_fire_off_the_grid_moves_to_the_next_grid_point(rig: Rig) -> None:
    scheduler, _ = a_refresher(rig)
    options = refresher_options("60")
    job = await scheduler.ensure_infrastructure_job(REFRESHER, **options)
    rig.sql(
        "UPDATE scheduled_job SET next_run_at = ? WHERE id = ?", (stamp(at(MINUTE + 40)), job.id)
    )

    realigned = await scheduler.ensure_infrastructure_job(REFRESHER, **options)

    assert realigned.next_run_at == stamp(at(MINUTE))


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(at(-45), id="due-and-off-the-grid"),
        pytest.param(at(2 * MINUTE), id="future-and-on-the-grid"),
    ],
)
async def test_a_stored_fire_that_is_due_or_on_the_grid_is_left_alone(
    rig: Rig, stored: datetime
) -> None:
    scheduler, _ = a_refresher(rig)
    options = refresher_options("60")
    job = await scheduler.ensure_infrastructure_job(REFRESHER, **options)
    rig.sql("UPDATE scheduled_job SET next_run_at = ? WHERE id = ?", (stamp(stored), job.id))

    again = await scheduler.ensure_infrastructure_job(REFRESHER, **options)

    assert again.next_run_at == stamp(stored)


async def test_realigning_a_second_time_changes_nothing(rig: Rig) -> None:
    scheduler, _ = a_refresher(rig)
    options = refresher_options("60")
    job = await scheduler.ensure_infrastructure_job(REFRESHER, **options)
    rig.sql(
        "UPDATE scheduled_job SET next_run_at = ? WHERE id = ?", (stamp(at(MINUTE + 40)), job.id)
    )
    first = await scheduler.ensure_infrastructure_job(REFRESHER, **options)

    second = await scheduler.ensure_infrastructure_job(REFRESHER, **options)

    assert second == first


async def test_a_cron_infrastructure_job_is_never_realigned(rig: Rig) -> None:
    scheduler, _ = a_refresher(rig)
    options: dict[str, Any] = {
        "kind": "cron",
        "expression": "* * * * *",
        "catch_up": False,
        "overrun_policy": "skip",
    }
    job = await scheduler.ensure_infrastructure_job(REFRESHER, **options)
    rig.sql(
        "UPDATE scheduled_job SET next_run_at = ? WHERE id = ?", (stamp(at(MINUTE + 40)), job.id)
    )

    again = await scheduler.ensure_infrastructure_job(REFRESHER, **options)

    assert again.next_run_at == stamp(at(MINUTE + 40))


async def a_rule_job_failing_off_phase(rig: Rig, scheduler: Scheduler, expression: str) -> int:
    job = await rig.rule_job("r", kind="interval", expression=expression, next_run=at(MINUTE))
    rig.sql(
        "UPDATE scheduled_job SET consecutive_failures = ? WHERE id = ?",
        (RETRIED_AFTER_FAILURES, job),
    )
    rig.clock.set(at(SERVED_TEN_SECONDS_LATE))
    await scheduler.tick()
    await scheduler.idle()
    return job


async def test_an_interval_rule_retries_on_the_next_slot_of_its_original_phase(rig: Rig) -> None:
    handler = ScriptedHandler()
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)

    job = await a_rule_job_failing_off_phase(rig, scheduler, "60")

    assert await stored_next_fire(rig, job) == stamp(at(RETRY_ON_THE_ORIGINAL_PHASE))


async def test_an_interval_rule_that_recovers_after_a_retry_stays_on_its_original_phase(
    rig: Rig,
) -> None:
    handler = ScriptedHandler()
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)
    job = await a_rule_job_failing_off_phase(rig, scheduler, "60")
    handler.behaviour = succeeds()

    await serve_when_due(rig, scheduler, job)

    assert await stored_next_fire(rig, job) == stamp(at(SLOT_AFTER_THE_RETRY))


async def test_a_cron_rule_retries_at_the_backoff_time_itself(rig: Rig) -> None:
    handler = ScriptedHandler()
    handler.behaviour = fails()
    scheduler = rig.scheduler(handler)
    job = await rig.rule_job("r", kind="cron", expression="* * * * *", next_run=at(MINUTE))
    rig.sql(
        "UPDATE scheduled_job SET consecutive_failures = ? WHERE id = ?",
        (RETRIED_AFTER_FAILURES, job),
    )
    rig.clock.set(at(SERVED_TEN_SECONDS_LATE))

    await scheduler.tick()
    await scheduler.idle()

    assert await stored_next_fire(rig, job) == stamp(
        at(SERVED_TEN_SECONDS_LATE + BACKOFF_FROM_THE_LATE_SERVE)
    )
