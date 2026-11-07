"""Every row the scheduler paths write names the owning subsystem, its rule and its run."""

import asyncio
import json
import sqlite3
from typing import Any

import pytest

from app.scheduler.model import RunFailed, RunResult
from app.scheduler.registry import HandlerRegistry
from app.services.activity import SUBSYSTEMS, SubsystemActor, UnknownSubsystem
from tests.scheduler.support import Rig, ScriptedHandler, at


def test_the_scheduler_is_not_a_registered_subsystem() -> None:
    assert "scheduler" not in SUBSYSTEMS


def test_an_actor_cannot_be_built_for_the_scheduler() -> None:
    with pytest.raises(UnknownSubsystem):
        SubsystemActor("scheduler", None)


def test_the_scheduler_cannot_register_a_handler_under_its_own_name() -> None:
    with pytest.raises(ValueError, match="not a registered subsystem"):
        HandlerRegistry().register(ScriptedHandler("scheduler"))


async def test_the_database_refuses_an_event_attributed_to_the_scheduler(rig: Rig) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        rig.sql(
            "INSERT INTO activity_event (category, actor_type, actor_subsystem, target_type, "
            "action, result, occurred_at) VALUES ('automation', 'subsystem', 'scheduler', "
            "'scheduled_job', 'a', 'success', '2026-01-01T00:00:00Z')"
        )


async def test_every_event_written_across_the_scheduler_paths_names_subsystem_rule_and_run(
    rig: Rig,
) -> None:
    environment = rig.environment()
    failing_rule = await rig.rule("failing", environment_id=environment)
    failing_job = await rig.job(
        rule_id=failing_rule, next_run=at(60), threshold=1, environment_id=environment
    )
    running_rule = await rig.rule("running", environment_id=environment)
    running_job = await rig.job(rule_id=running_rule, next_run=at(200), environment_id=environment)
    stale_rule = await rig.rule("stale", environment_id=environment)
    stale_job = await rig.job(rule_id=stale_rule, next_run=at(7200), environment_id=environment)
    await rig.start_run(stale_job)
    handler = ScriptedHandler()

    async def fails_one_and_hangs_the_other(dispatch: Any) -> RunResult:
        if dispatch.job.id == failing_job:
            raise RunFailed("host_down", "The host did not answer.")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    handler.behaviour = fails_one_and_hangs_the_other
    scheduler = rig.scheduler(handler)
    await scheduler.start()
    rig.clock.set(at(60))
    await scheduler.tick()
    await scheduler.idle()
    rig.clock.set(at(200))
    await scheduler.tick()
    await handler.entered(2)

    await scheduler.aclose()

    events = rig.events()
    rules_by_job = {failing_job: failing_rule, running_job: running_rule, stale_job: stale_rule}
    runs = {row["id"]: row for row in rig.sql("SELECT * FROM job_run")}
    assert sorted(event["action"] for event in events) == [
        "automation_run_failed",
        "job_run_interrupted",
        "job_run_interrupted",
        "scheduled_job_disabled",
    ]
    assert [
        (
            event["actor_type"],
            event["actor_subsystem"],
            event["actor_user_id"],
            event["environment_id"],
            event["rule_id"] == rules_by_job[runs[event["job_run_id"]]["scheduled_job_id"]],
        )
        for event in events
    ] == [("subsystem", "stack-restarter", None, environment, True)] * 4
    names = {failing_rule: "failing", running_rule: "running", stale_rule: "stale"}
    assert [json.loads(event["detail"])["rule_name"] for event in events] == [
        names[event["rule_id"]] for event in events
    ]
