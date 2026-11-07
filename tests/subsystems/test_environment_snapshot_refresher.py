"""The snapshot refresher as a scheduled job: its declared policies, its run, and its startup."""

import asyncio
import io
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from app.config import Settings
from app.db.engine import connect
from app.db.repositories.clocked import format_timestamp
from app.logging import configure_logging
from app.main import create_app
from app.services.environment_snapshots import EnvironmentSnapshotService
from app.subsystems.base import SubsystemDeps
from app.subsystems.environment_snapshot_refresher import EnvironmentSnapshotRefresher
from tests.conftest import serving_app
from tests.job_support import events, rows
from tests.scheduler.support import REFRESHER, Rig, at, dispatch_of

ATTEMPTED_ENVIRONMENTS = 3
SNAPSHOT_INTERVAL = 15
TEN_MINUTES_AGO = timedelta(minutes=-10)
CHANGED_INTERVAL = 30


class StubSnapshots:
    """Stands in for the snapshot service and records how the refresher called it."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._fail_next = fail_first

    async def refresh_all(self, *args: Any, **kwargs: Any) -> int:
        self.calls.append((args, kwargs))
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("database busy")
        return ATTEMPTED_ENVIRONMENTS


def refresher_over(deps: SubsystemDeps, snapshots: StubSnapshots) -> EnvironmentSnapshotRefresher:
    engine = SimpleNamespace(snapshots=snapshots, locks=deps.engine.locks)
    return EnvironmentSnapshotRefresher(replace(deps, engine=engine))  # type: ignore[arg-type]


def test_the_refresher_declares_the_policies_of_an_infrastructure_job() -> None:
    declared = {
        "name": EnvironmentSnapshotRefresher.name,
        "parameters": EnvironmentSnapshotRefresher.parameters_model,
        "event_policy": EnvironmentSnapshotRefresher.event_policy,
        "schedule_kinds": EnvironmentSnapshotRefresher.schedule_kinds,
        "min_interval_seconds": EnvironmentSnapshotRefresher.min_interval_seconds,
        "overrun_policy": EnvironmentSnapshotRefresher.overrun_policy,
        "allowed_overrun_policies": EnvironmentSnapshotRefresher.allowed_overrun_policies,
        "cancellation_safe": EnvironmentSnapshotRefresher.cancellation_safe,
        "catch_up": EnvironmentSnapshotRefresher.catch_up,
        "failure_threshold": EnvironmentSnapshotRefresher.failure_threshold,
        "supports_dry_run": EnvironmentSnapshotRefresher.supports_dry_run,
        "run_timeout": EnvironmentSnapshotRefresher.run_timeout,
    }

    assert declared == {
        "name": REFRESHER,
        "parameters": None,
        "event_policy": "none",
        "schedule_kinds": frozenset({"interval"}),
        "min_interval_seconds": SNAPSHOT_INTERVAL,
        "overrun_policy": "skip",
        "allowed_overrun_policies": frozenset({"skip"}),
        "cancellation_safe": False,
        "catch_up": True,
        "failure_threshold": None,
        "supports_dry_run": False,
        "run_timeout": timedelta(minutes=10),
    }


async def test_a_run_refreshes_every_environment_with_no_fixed_time_and_reports_how_many(
    rig: Rig, deps: SubsystemDeps
) -> None:
    snapshots = StubSnapshots()
    job = await rig.refresher_job(next_run=at(15))

    ran = await dispatch_of(rig, refresher_over(deps, snapshots), job)

    assert snapshots.calls == [((), {})]
    assert (ran.outcome, ran.attempted) == ("success", ATTEMPTED_ENVIRONMENTS)
    assert rig.events() == []


async def test_a_pass_that_fails_is_recorded_and_the_job_goes_on_at_its_interval(
    rig: Rig, deps: SubsystemDeps
) -> None:
    snapshots = StubSnapshots(fail_first=True)
    job = await rig.refresher_job(next_run=at(15))
    scheduler = rig.scheduler(refresher_over(deps, snapshots))
    rig.clock.set(at(15))
    await scheduler.tick()
    await scheduler.idle()
    after_failure = await rig.job_row(job)
    rig.clock.set(at(30))

    await scheduler.tick()
    await scheduler.idle()

    assert (after_failure.is_enabled, after_failure.next_run_at) == (True, format_timestamp(at(30)))
    after_success = await rig.job_row(job)
    assert [outcome for outcome, _ in rig.outcomes(job)] == ["failure"]
    assert (after_success.last_run_at, after_success.last_run_outcome) == (
        format_timestamp(at(30)),
        "success",
    )
    assert rig.events() == []


@pytest.fixture
def app(settings: Settings, log_stream: io.StringIO) -> FastAPI:
    application = create_app(settings)
    configure_logging(settings.log_level, stream=log_stream)
    return application


async def test_startup_registers_the_rule_less_job_from_the_configured_interval_and_runs_it_once(
    started_app: FastAPI, settings: Settings
) -> None:
    [job] = await rows(started_app, "SELECT * FROM scheduled_job WHERE subsystem = ?", (REFRESHER,))
    runs = await rows(started_app, "SELECT trigger, outcome FROM job_run")

    assert job["rule_id"] is None
    assert (job["schedule_kind"], job["schedule_expression"]) == (
        "interval",
        str(settings.snapshot_interval_seconds),
    )
    assert (job["catch_up"], job["overrun_policy"], job["failure_threshold"]) == (1, "skip", None)
    assert runs == []
    assert (job["last_run_at"] is not None, job["last_run_outcome"]) == (True, "success")
    assert await events(started_app) == []


async def stop_after_first_start(settings: Settings, template: Path) -> datetime:
    """Start and stop an application, then make its refresher's next fire ten minutes overdue."""
    first = create_app(settings)
    async with serving_app(first, settings, template):
        pass
    missed = first.state.auth.clock() + TEN_MINUTES_AGO
    connection = connect(settings.db_path)
    try:
        connection.execute("DELETE FROM job_run")
        connection.execute(
            "UPDATE scheduled_job SET next_run_at = ? WHERE subsystem = ?",
            (format_timestamp(missed), REFRESHER),
        )
    finally:
        connection.close()
    returned: datetime = missed
    return returned


async def test_a_restart_after_downtime_catches_up_with_exactly_one_refresh(
    settings: Settings,
    provisioned_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    log_stream: io.StringIO,
) -> None:
    missed = await stop_after_first_start(settings, provisioned_template)
    calls: list[object] = []

    async def counting_refresh_all(self: EnvironmentSnapshotService, now: object = None) -> int:
        calls.append(now)
        return 0

    monkeypatch.setattr(EnvironmentSnapshotService, "refresh_all", counting_refresh_all)
    second = create_app(settings)
    configure_logging(settings.log_level, stream=log_stream)

    async with second.router.lifespan_context(second):
        await second.state.scheduler.scheduler.idle()
        runs = await rows(second, "SELECT trigger, scheduled_for, outcome FROM job_run ORDER BY id")

    assert runs == [
        {"trigger": "catch_up", "scheduled_for": format_timestamp(missed), "outcome": "success"}
    ]
    assert calls == [None]


async def test_a_changed_interval_setting_rewrites_the_job_at_the_next_start(
    settings: Settings, provisioned_template: Path, log_stream: io.StringIO
) -> None:
    await stop_after_first_start(settings, provisioned_template)
    changed = settings.model_copy(update={"snapshot_interval_seconds": CHANGED_INTERVAL})
    second = create_app(changed)
    configure_logging(settings.log_level, stream=log_stream)

    async with second.router.lifespan_context(second):
        await second.state.scheduler.scheduler.idle()
        [job] = await rows(second, "SELECT * FROM scheduled_job WHERE subsystem = ?", (REFRESHER,))

    assert job["schedule_expression"] == str(CHANGED_INTERVAL)


async def test_a_poke_still_refreshes_one_environment_through_the_service(
    started_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    refreshed: list[int] = []
    called = asyncio.Event()

    async def record(self: EnvironmentSnapshotService, environment_id: int, **_: Any) -> None:
        refreshed.append(environment_id)
        called.set()

    monkeypatch.setattr(EnvironmentSnapshotService, "refresh_one", record)

    started_app.state.engine.refresher.poke(7)
    await called.wait()

    assert refreshed == [7]
