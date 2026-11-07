"""Scheduled successes of a job that writes no events leave no run row; every other run does."""

import asyncio
from datetime import timedelta

import pytest

from app.db.repositories.job_run import JobRunRepository
from app.scheduler.loop import LAST_RUN_REWRITE, Scheduler, UnrecordedRun
from app.scheduler.model import Dispatch, RunResult
from tests.scheduler.support import (
    REFRESHER,
    Behaviour,
    Rig,
    ScriptedHandler,
    at,
    fails,
    hangs,
    stamp,
    succeeds,
)

FIRST_FIRE = 15
RUN_SECONDS = 5
SECOND_FIRE = 30
THIRD_FIRE = 45
AN_HOUR = int(LAST_RUN_REWRITE.total_seconds())
TINY_SHUTDOWN_WAIT = timedelta(milliseconds=20)


def a_refresher(rig: Rig, behaviour: Behaviour | None = None) -> tuple[Scheduler, ScriptedHandler]:
    handler = ScriptedHandler(REFRESHER, records_events=False)
    if behaviour is not None:
        handler.behaviour = behaviour
    return rig.scheduler(handler), handler


def taking(seconds: int, then: Behaviour, rig: Rig) -> Behaviour:
    async def behave(dispatch: Dispatch) -> RunResult:
        rig.clock.jump(seconds)
        return await then(dispatch)

    return behave


def stubborn(release: asyncio.Event, then: Behaviour) -> Behaviour:
    """Ignore a cancel, wait for the release, then finish with the given behaviour."""

    async def behave(dispatch: Dispatch) -> RunResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        return await then(dispatch)

    return behave


async def serve_at(rig: Rig, scheduler: Scheduler, job: int, seconds: int) -> None:
    rig.sql("UPDATE scheduled_job SET next_run_at = ? WHERE id = ?", (stamp(at(seconds)), job))
    rig.clock.set(at(seconds))
    await scheduler.tick()
    await scheduler.idle()


async def test_a_scheduled_success_leaves_no_run_row_and_records_the_start_on_the_job(
    rig: Rig,
) -> None:
    scheduler, handler = a_refresher(rig, taking(RUN_SECONDS, succeeds(), rig))
    job = await rig.refresher_job(next_run=at(FIRST_FIRE))

    await serve_at(rig, scheduler, job, FIRST_FIRE)

    row = await rig.job_row(job)
    assert rig.runs(job) == []
    assert (row.last_run_at, row.last_run_outcome) == (stamp(at(FIRST_FIRE)), "success")
    assert scheduler.unrecorded_run(job) == UnrecordedRun(stamp(at(FIRST_FIRE)), "success")
    assert len(handler.calls) == 1


async def test_a_failed_infrastructure_run_writes_its_row_with_the_original_start(
    rig: Rig,
) -> None:
    scheduler, _ = a_refresher(rig, taking(RUN_SECONDS, fails(), rig))
    job = await rig.refresher_job(next_run=at(FIRST_FIRE))

    await serve_at(rig, scheduler, job, FIRST_FIRE)

    [run] = rig.runs(job)
    async with rig.database.session() as db:
        history = await JobRunRepository(db, rig.clock).page(job, limit=10)
    assert (run["trigger"], run["outcome"], run["reason"]) == ("schedule", "failure", "host_down")
    assert (run["started_at"], run["finished_at"]) == (
        stamp(at(FIRST_FIRE)),
        stamp(at(FIRST_FIRE + RUN_SECONDS)),
    )
    assert [item.id for item in history.items] == [run["id"]]
    assert (await rig.job_row(job)).consecutive_failures == 1


async def test_a_manual_run_keeps_its_row_and_does_not_touch_the_last_run_fields(
    rig: Rig,
) -> None:
    scheduler, _ = a_refresher(rig)
    job = await rig.refresher_job(next_run=at(3600))
    run = await rig.start_run(job, "manual")

    scheduler.start_claimed(run)
    await scheduler.idle()

    row = await rig.job_row(job)
    assert rig.outcomes(job) == [("success", None)]
    assert (row.last_run_at, row.last_run_outcome) == (None, None)


async def test_a_catch_up_run_keeps_its_row(rig: Rig) -> None:
    scheduler, _ = a_refresher(rig)
    job = await rig.refresher_job(next_run=at(-600), catch_up=True)

    await scheduler.tick()
    await scheduler.idle()

    [run] = rig.runs(job)
    assert (run["trigger"], run["outcome"]) == ("catch_up", "success")


async def test_a_run_interrupted_by_shutdown_writes_exactly_one_interrupted_row(
    rig: Rig,
) -> None:
    scheduler, handler = a_refresher(rig, hangs())
    job = await rig.refresher_job(next_run=at(FIRST_FIRE))
    rig.clock.set(at(FIRST_FIRE))
    await scheduler.tick()
    await handler.entered(1)

    await scheduler.aclose()

    row = await rig.job_row(job)
    assert rig.outcomes(job) == [("interrupted", "shutdown")]
    assert rig.runs(job)[0]["started_at"] == stamp(at(FIRST_FIRE))
    assert (row.last_run_at, row.consecutive_failures) == (None, 0)
    assert rig.events() == []


@pytest.mark.parametrize(
    "late",
    [pytest.param(fails(), id="late-failure"), pytest.param(succeeds(), id="late-success")],
)
async def test_a_run_that_finishes_after_its_interruption_was_recorded_adds_no_second_row(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, late: Behaviour
) -> None:
    monkeypatch.setattr("app.scheduler.loop.SHUTDOWN_WAIT", TINY_SHUTDOWN_WAIT)
    release = asyncio.Event()
    scheduler, handler = a_refresher(rig, stubborn(release, late))
    job = await rig.refresher_job(next_run=at(FIRST_FIRE))
    rig.clock.set(at(FIRST_FIRE))
    await scheduler.tick()
    await handler.entered(1)
    await scheduler.aclose()

    release.set()
    await scheduler.idle()

    row = await rig.job_row(job)
    assert rig.outcomes(job) == [("interrupted", "shutdown")]
    assert (row.last_run_at, row.consecutive_failures) == (None, 0)


@pytest.mark.parametrize(
    ("later_fire", "rewritten"),
    [
        pytest.param(FIRST_FIRE + AN_HOUR - 1, False, id="just-under-an-hour"),
        pytest.param(FIRST_FIRE + AN_HOUR, True, id="an-hour"),
    ],
)
async def test_the_last_run_is_rewritten_only_once_the_stored_one_is_an_hour_old(
    rig: Rig, later_fire: int, rewritten: bool
) -> None:
    scheduler, _ = a_refresher(rig)
    job = await rig.refresher_job(next_run=at(FIRST_FIRE))
    await serve_at(rig, scheduler, job, FIRST_FIRE)

    await serve_at(rig, scheduler, job, later_fire)

    stored = (await rig.job_row(job)).last_run_at
    assert stored == stamp(at(later_fire if rewritten else FIRST_FIRE))
    assert scheduler.unrecorded_run(job) == UnrecordedRun(stamp(at(later_fire)), "success")


async def test_a_success_after_a_failure_becomes_the_last_run_whatever_the_stored_age(
    rig: Rig,
) -> None:
    scheduler, handler = a_refresher(rig)
    job = await rig.refresher_job(next_run=at(FIRST_FIRE))
    await serve_at(rig, scheduler, job, FIRST_FIRE)
    handler.behaviour = fails()
    await serve_at(rig, scheduler, job, SECOND_FIRE)
    handler.behaviour = succeeds()

    await serve_at(rig, scheduler, job, THIRD_FIRE)

    row = await rig.job_row(job)
    assert (row.last_run_at, row.last_run_outcome) == (stamp(at(THIRD_FIRE)), "success")
    assert row.consecutive_failures == 0
    assert rig.outcomes(job) == [("failure", "host_down")]
