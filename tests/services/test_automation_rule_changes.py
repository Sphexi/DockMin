"""Changing rules: editing disables, enabling is bound to what was reviewed, delete and run now."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import FastAPI

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.config import Settings
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.services.activity import ActivityRecorder, SubsystemActor
from app.services.assembly import JobServices
from app.services.automation_rules import RULE_BUSY, RuleBusy
from app.services.jobs import JobNotRunnable
from tests.auth_support import STANDARD_ID
from tests.conftest import AttachApp
from tests.job_support import (
    ADMIN,
    JOB_SUBSYSTEMS,
    RESTARTER,
    STANDARD,
    events,
    execute,
    job_of_rule,
    job_services,
    last_event_id,
    rows,
    rule_arguments,
    seed_settled_world,
)
from tests.web.environment_support import World

OTHER_STANDARD = Principal(user_id=STANDARD_ID + 50, username="other", role="standard")
NEVER = "2999-01-01T00:00:00.000Z"
FAILURES_TO_DISABLE = 5
IDLE_SECONDS = 20


@pytest.fixture
def app(settings: Settings, attach_app: AttachApp) -> FastAPI:
    return attach_app(settings, JOB_SUBSYSTEMS)


@pytest.fixture
async def seeded(started_app: FastAPI) -> World:
    world = await seed_settled_world(started_app)
    async with started_app.state.database.session() as db:
        await EnvironmentAccessRepository(db).add_user("environment", world.remote_id, STANDARD_ID)
    return world


async def idle(app: FastAPI) -> None:
    async with asyncio.timeout(IDLE_SECONDS):
        await app.state.scheduler.scheduler.idle()


def edit_to(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Nightly",
        "target_selector": "web",
        "parameters": {},
        "schedule_kind": "interval",
        "schedule_expression": "60",
        "time_zone": "UTC",
        "overrun_policy": None,
    }
    return base | changes


@pytest.mark.parametrize(
    ("changes", "disables"),
    [
        ({"target_selector": "other"}, True),
        ({"parameters": {"note": "changed"}}, True),
        ({"schedule_expression": "120"}, True),
        ({"schedule_kind": "cron", "schedule_expression": "0 3 * * *"}, True),
        ({"name": "Renamed"}, False),
    ],
)
async def test_saving_a_change_to_what_an_enabled_rule_does_disables_it(
    started_app: FastAPI, seeded: World, changes: dict[str, Any], disables: bool
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **rule_arguments(seeded.remote_id))
        await services.rules.enable(ADMIN, rule.id)
        mark = await last_event_id(started_app)

        saved = await services.rules.update(ADMIN, rule.id, **edit_to(**changes))

    job = await job_of_rule(started_app, rule.id)
    [recorded] = await events(started_app, since=mark)
    assert saved.is_enabled is not disables
    assert (job["next_run_at"] is None) is disables
    assert json.loads(recorded["detail"]).get("rule_disabled", False) is disables


async def test_enabling_is_refused_when_the_rule_was_saved_after_it_was_reviewed(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **rule_arguments(seeded.remote_id))
        reviewed = (await services.rules.get(ADMIN, rule.id)).updated_at
        await execute(
            started_app, "UPDATE automation_rule SET updated_at = ? WHERE id = ?", (NEVER, rule.id)
        )
        mark = await last_event_id(started_app)

        with pytest.raises(ValidationFailed) as refused:
            await services.rules.enable(ADMIN, rule.id, expected_updated_at=reviewed)
        assert (await services.rules.get(ADMIN, rule.id)).is_enabled is False
        await services.rules.enable(ADMIN, rule.id, expected_updated_at=NEVER)

        assert (await services.rules.get(ADMIN, rule.id)).is_enabled is True
    [refusal, success] = await events(started_app, since=mark)
    assert refused.value.fields["rule"].startswith("The automation changed since you reviewed it")
    assert (refusal["action"], refusal["result"]) == ("automation_rule_enabled", "failure")
    assert json.loads(refusal["detail"])["reason"] == "changed_since_review"
    assert success["result"] == "success"


async def test_enabling_a_rule_whose_job_was_disabled_after_repeated_failures_re_enables_the_job(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **rule_arguments(seeded.remote_id))
        await services.rules.enable(ADMIN, rule.id)
        await execute(
            started_app,
            "UPDATE scheduled_job SET is_enabled = 0, consecutive_failures = ?, "
            "next_run_at = NULL WHERE rule_id = ?",
            (FAILURES_TO_DISABLE, rule.id),
        )
        mark = await last_event_id(started_app)

        await services.rules.enable(ADMIN, rule.id)
        await services.rules.enable(ADMIN, rule.id)

    job = await job_of_rule(started_app, rule.id)
    [recorded] = await events(started_app, since=mark)
    assert (job["is_enabled"], job["consecutive_failures"]) == (1, 0)
    assert job["next_run_at"] is not None
    assert json.loads(recorded["detail"])["job_reenabled"] is True


async def test_a_rule_is_deleted_with_its_job_and_runs_and_its_activity_stays_answerable(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(
            STANDARD, **rule_arguments(seeded.remote_id, name="mine")
        )
        job_id = (await job_of_rule(started_app, rule.id))["id"]
        await services.rules.run_now(STANDARD, rule.id, dry_run=True)
        await idle(started_app)
        run_id = (
            await rows(started_app, "SELECT id FROM job_run WHERE scheduled_job_id = ?", (job_id,))
        )[0]["id"]
        async with started_app.state.database.session() as db:
            await ActivityRecorder(db, started_app.state.auth.clock).record_event(
                SubsystemActor(RESTARTER, rule.id, run_id, rule_name="mine"),
                action="container_restarted",
                target_type="container",
                target_id="abc",
                environment_id=seeded.remote_id,
                result="success",
            )
        preview = await services.rules.delete_preview(STANDARD, rule.id)
        mark = await last_event_id(started_app)

        await services.rules.delete(STANDARD, rule.id, expected_name="mine")

    assert (preview.job_id, preview.runs, preview.rule.name) == (job_id, 1, "mine")
    assert await rows(started_app, "SELECT id FROM scheduled_job WHERE id = ?", (job_id,)) == []
    assert (
        await rows(started_app, "SELECT id FROM job_run WHERE scheduled_job_id = ?", (job_id,))
        == []
    )
    assert await rows(started_app, "SELECT id FROM automation_rule WHERE id = ?", (rule.id,)) == []
    [kept] = [e for e in await events(started_app) if e["action"] == "container_restarted"]
    assert (kept["rule_id"], kept["job_run_id"]) == (None, None)
    assert json.loads(kept["detail"]) == {"rule_id": rule.id, "rule_name": "mine"}
    [deleted] = await events(started_app, since=mark)
    assert (deleted["action"], deleted["result"], deleted["actor_user_id"]) == (
        "automation_rule_deleted",
        "success",
        STANDARD_ID,
    )
    assert (deleted["environment_id"], deleted["target_type"], deleted["target_id"]) == (
        seeded.remote_id,
        "automation_rule",
        str(rule.id),
    )
    assert json.loads(deleted["detail"]) == {
        "subsystem": RESTARTER,
        "name": "mine",
        "job_id": job_id,
        "runs_deleted": 1,
    }


async def test_a_delete_confirmed_under_another_name_deletes_nothing(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **rule_arguments(seeded.remote_id, name="old"))
        mark = await last_event_id(started_app)

        with pytest.raises(ValidationFailed) as refused:
            await services.rules.delete(ADMIN, rule.id, expected_name="renamed")

        assert (await services.rules.get(ADMIN, rule.id)).name == "old"
    assert "changed since you reviewed it" in refused.value.fields["name"]
    [recorded] = await events(started_app, since=mark)
    assert (recorded["result"], json.loads(recorded["detail"])["reason"]) == (
        "failure",
        "changed_since_review",
    )


async def test_a_rule_with_a_run_in_flight_cannot_be_deleted_until_the_run_ends(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **rule_arguments(seeded.remote_id))
        job_id = (await job_of_rule(started_app, rule.id))["id"]
        await execute(
            started_app,
            "INSERT INTO job_run (scheduled_job_id, trigger, started_at, heartbeat_at) "
            "VALUES (?, 'manual', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (job_id,),
        )
        mark = await last_event_id(started_app)

        with pytest.raises(RuleBusy) as busy:
            await services.rules.delete(ADMIN, rule.id, expected_name=rule.name)
        assert (await services.rules.get(ADMIN, rule.id)).id == rule.id
        await execute(
            started_app,
            "UPDATE job_run SET outcome = 'success', finished_at = '2026-01-01T00:01:00Z'",
        )
        await services.rules.delete(ADMIN, rule.id, expected_name=rule.name)

    assert str(busy.value) == RULE_BUSY
    recorded = await events(started_app, since=mark)
    assert [(e["action"], e["result"]) for e in recorded] == [
        ("automation_rule_deleted", "failure"),
        ("automation_rule_deleted", "success"),
    ]
    assert json.loads(recorded[0]["detail"])["reason"] == "rule_running"


Call = Callable[[JobServices, Principal, int], Awaitable[Any]]

CALLS: dict[str, Call] = {
    "get": lambda s, who, rid: s.rules.get(who, rid),
    "detail": lambda s, who, rid: s.rules.detail(who, rid),
    "runs": lambda s, who, rid: s.rules.runs(who, rid, limit=5),
    "run": lambda s, who, rid: s.rules.run(who, rid, 1),
    "update": lambda s, who, rid: s.rules.update(who, rid, **edit_to()),
    "enable": lambda s, who, rid: s.rules.enable(who, rid),
    "disable": lambda s, who, rid: s.rules.disable(who, rid),
    "pause": lambda s, who, rid: s.rules.pause(who, rid, duration="1h"),
    "resume": lambda s, who, rid: s.rules.resume(who, rid),
    "delete_preview": lambda s, who, rid: s.rules.delete_preview(who, rid),
    "delete": lambda s, who, rid: s.rules.delete(who, rid, expected_name="Nightly"),
    "run_now": lambda s, who, rid: s.rules.run_now(who, rid),
    "dry_run": lambda s, who, rid: s.rules.run_now(who, rid, dry_run=True),
}


@pytest.mark.parametrize("call", sorted(CALLS))
async def test_another_users_rule_is_not_found_exactly_as_a_rule_that_does_not_exist(
    started_app: FastAPI, seeded: World, call: str
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **rule_arguments(seeded.remote_id))
        mark = await last_event_id(started_app)

        with pytest.raises(ResourceNotFound) as other:
            await CALLS[call](services, STANDARD, rule.id)
        with pytest.raises(ResourceNotFound) as absent:
            await CALLS[call](services, STANDARD, rule.id + 1000)

    assert str(other.value) == str(absent.value)
    assert await events(started_app, since=mark) == []


async def test_an_owner_may_dry_run_a_disabled_rule_but_a_real_run_is_refused_until_it_is_enabled(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(STANDARD, **rule_arguments(seeded.remote_id))
        job_id = (await job_of_rule(started_app, rule.id))["id"]
        mark = await last_event_id(started_app)

        dry = await services.rules.run_now(STANDARD, rule.id, dry_run=True)
        await idle(started_app)
        with pytest.raises(JobNotRunnable) as refused:
            await services.rules.run_now(STANDARD, rule.id)
        await services.rules.enable(STANDARD, rule.id)
        real = await services.rules.run_now(STANDARD, rule.id)
        await idle(started_app)

    runs = await rows(
        started_app, "SELECT * FROM job_run WHERE scheduled_job_id = ? ORDER BY id", (job_id,)
    )
    assert [(r["id"], r["trigger"], r["outcome"], r["requested_by_user_id"]) for r in runs] == [
        (dry, "dry_run", "success", STANDARD_ID),
        (real, "manual", "success", STANDARD_ID),
    ]
    assert refused.value.reason == "rule_disabled"
    requested = [
        e for e in await events(started_app, since=mark) if e["action"] == "job_run_requested"
    ]
    assert [(e["actor_user_id"], e["result"]) for e in requested] == [
        (STANDARD_ID, "success"),
        (STANDARD_ID, "failure"),
        (STANDARD_ID, "success"),
    ]


async def test_an_administrator_may_run_another_users_rule_and_the_job_pages_stay_theirs_alone(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(STANDARD, **rule_arguments(seeded.remote_id))
        job_id = (await job_of_rule(started_app, rule.id))["id"]

        run_id = await services.rules.run_now(ADMIN, rule.id, dry_run=True)
        await idle(started_app)
        with pytest.raises(Exception, match="administrator role required"):
            await services.jobs.run_now(STANDARD, job_id)

    [run] = await rows(started_app, "SELECT * FROM job_run WHERE id = ?", (run_id,))
    assert (run["trigger"], run["requested_by_user_id"]) == ("dry_run", ADMIN.user_id)


async def test_a_dry_run_is_still_refused_when_the_subsystem_is_switched_off(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(STANDARD, **rule_arguments(seeded.remote_id))
        await services.jobs.set_subsystem_enabled(ADMIN, RESTARTER, enabled=False)

        with pytest.raises(JobNotRunnable) as refused:
            await services.rules.run_now(STANDARD, rule.id, dry_run=True)

    assert refused.value.reason == "subsystem_disabled"


async def test_running_a_rule_bound_to_a_review_is_refused_when_the_rule_changed_since(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **rule_arguments(seeded.remote_id))
        await services.rules.enable(ADMIN, rule.id)
        preview = await services.rules.run_preview(ADMIN, rule.id)
        reviewed = (await services.rules.get(ADMIN, rule.id)).updated_at
        runs_before = await rows(started_app, "SELECT id FROM job_run")
        await execute(
            started_app, "UPDATE automation_rule SET updated_at = ? WHERE id = ?", (NEVER, rule.id)
        )

        with pytest.raises(ValidationFailed) as stale_stamp:
            await services.rules.run_now(
                ADMIN, rule.id, expected_updated_at=reviewed, expected_digest=preview.digest
            )
        with pytest.raises(ValidationFailed) as stale_digest:
            await services.rules.run_now(
                ADMIN, rule.id, expected_updated_at=NEVER, expected_digest="0" * 64
            )
        started = await services.rules.run_now(
            ADMIN, rule.id, expected_updated_at=NEVER, expected_digest=preview.digest
        )

    assert await rows(started_app, "SELECT id FROM job_run") != runs_before
    assert started > 0
    assert stale_stamp.value.fields and stale_digest.value.fields
    assert all("changed since you reviewed" in text for text in stale_stamp.value.fields.values())


async def test_a_dry_run_of_a_rule_needs_no_review_binding(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **rule_arguments(seeded.remote_id))
        await services.rules.enable(ADMIN, rule.id)

        run_id = await services.rules.run_now(
            ADMIN, rule.id, dry_run=True, expected_updated_at=NEVER, expected_digest="0" * 64
        )

    assert run_id > 0
