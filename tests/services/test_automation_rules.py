"""Automation rules: creation, authorisation, ownership, pause and the job each rule owns."""

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.config import Settings
from app.db.repositories.clocked import parse_timestamp
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.scheduler.schedules import ScheduleInvalid
from app.services.automation_rules import (
    INSTANCE_TARGET,
    MAX_RULES_PER_OWNER,
    TARGET_MISSING,
    describe_target,
    rule_digest,
)
from app.services.jobs import PAUSE_DURATIONS
from app.services.object_inspector import EnvironmentUnavailable, UnavailableKind
from app.subsystems.base import TargetPreview
from app.subsystems.selectors import MAX_SELECTOR_BYTES
from tests.auth_support import STANDARD_ID
from tests.conftest import AttachApp
from tests.job_support import (
    ADMIN,
    FORBIDDEN_SELECTOR,
    JOB_SUBSYSTEMS,
    REFRESHER,
    RESTARTER,
    STANDARD,
    UPDATER,
    ScriptedRestarter,
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

SENTINEL_NOTE = "SENTINEL-parameter-value"
LOCAL_SOCKET_REFUSAL = "insufficient access"
ONE_HOUR = "1h"


@pytest.fixture
def app(settings: Settings, attach_app: AttachApp) -> FastAPI:
    return attach_app(settings, JOB_SUBSYSTEMS)


@pytest.fixture
async def seeded(started_app: FastAPI) -> World:
    return await seed_settled_world(started_app)


async def test_a_rule_is_created_disabled_with_its_job_and_each_change_is_recorded_by_the_user(
    started_app: FastAPI, seeded: World
) -> None:
    mark = await last_event_id(started_app)
    async with job_services(started_app) as services:
        rule = await services.rules.create(
            ADMIN, **rule_arguments(seeded.remote_id, parameters={"note": SENTINEL_NOTE})
        )
        created = await job_of_rule(started_app, rule.id)
        await services.rules.enable(ADMIN, rule.id)
        enabled = await job_of_rule(started_app, rule.id)
        await services.rules.pause(ADMIN, rule.id, duration=ONE_HOUR)
        paused_rule = await services.rules.get(ADMIN, rule.id)
        paused = await job_of_rule(started_app, rule.id)
        await services.rules.resume(ADMIN, rule.id)
        await services.rules.update(
            ADMIN,
            rule.id,
            name="Nightly",
            target_selector="web",
            parameters={"note": SENTINEL_NOTE},
            schedule_kind="cron",
            schedule_expression="0 3 * * *",
            time_zone="Europe/London",
            overrun_policy="queue",
        )
        updated = await job_of_rule(started_app, rule.id)
        await services.rules.disable(ADMIN, rule.id)
        disabled = await job_of_rule(started_app, rule.id)

    recorded = await events(started_app, since=mark)
    assert rule.is_enabled is False
    assert (created["next_run_at"], created["is_enabled"], created["failure_threshold"]) == (
        None,
        1,
        5,
    )
    assert (created["catch_up"], created["environment_id"]) == (0, seeded.remote_id)
    assert enabled["next_run_at"] is not None
    assert (
        paused_rule.paused_until is not None and paused["next_run_at"] >= paused_rule.paused_until
    )
    assert (updated["schedule_kind"], updated["time_zone"], updated["overrun_policy"]) == (
        "cron",
        "Europe/London",
        "queue",
    )
    assert disabled["next_run_at"] is None
    assert [e["action"] for e in recorded] == [
        "automation_rule_created",
        "automation_rule_enabled",
        "automation_rule_paused",
        "automation_rule_resumed",
        "automation_rule_updated",
    ]
    assert {(e["actor_type"], e["actor_user_id"]) for e in recorded} == {("user", ADMIN.user_id)}
    assert {(e["environment_id"], e["target_type"], e["target_id"]) for e in recorded} == {
        (seeded.remote_id, "automation_rule", str(rule.id))
    }
    assert SENTINEL_NOTE not in json.dumps(recorded)
    updated_detail = json.loads(recorded[4]["detail"])
    assert updated_detail["parameters_changed"] is False
    assert updated_detail["schedule_changed"] is True
    assert updated_detail["rule_disabled"] is True


async def test_changing_nothing_or_switching_to_the_state_it_is_in_writes_no_event(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **rule_arguments(seeded.remote_id))
        mark = await last_event_id(started_app)

        await services.rules.disable(ADMIN, rule.id)
        await services.rules.resume(ADMIN, rule.id)
        await services.rules.update(
            ADMIN,
            rule.id,
            name="Nightly",
            target_selector="web",
            parameters={},
            schedule_kind="interval",
            schedule_expression="60",
            time_zone="UTC",
            overrun_policy=None,
        )

    assert await events(started_app, since=mark) == []


async def test_a_pause_lasts_the_chosen_time_and_any_other_length_is_refused(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **rule_arguments(seeded.remote_id))
        await services.rules.enable(ADMIN, rule.id)
        before = started_app.state.auth.clock()

        await services.rules.pause(ADMIN, rule.id, duration="4h")
        paused = await services.rules.get(ADMIN, rule.id)
        with pytest.raises(ValidationFailed) as refused:
            await services.rules.pause(ADMIN, rule.id, duration="2h")

    assert paused.paused_until is not None
    length = parse_timestamp(paused.paused_until) - before
    assert timedelta(hours=4) - timedelta(seconds=5) <= length <= timedelta(hours=4, seconds=5)
    assert "duration" in refused.value.fields
    assert set(PAUSE_DURATIONS) == {"1h", "4h", "24h", "7d"}


async def test_a_rule_that_is_not_valid_is_refused_with_the_field_at_fault(
    started_app: FastAPI, seeded: World
) -> None:
    arguments = rule_arguments(seeded.remote_id)
    async with job_services(started_app) as services:
        await services.rules.create(ADMIN, **arguments)

        async def refused_fields(**changes: Any) -> set[str]:
            with pytest.raises(ValidationFailed) as raised:
                await services.rules.create(ADMIN, **(arguments | {"name": "other"} | changes))
            return set(raised.value.fields)

        assert await refused_fields(name="NIGHTLY") == {"name"}
        assert await refused_fields(schedule_expression="5") == {"schedule"}
        assert await refused_fields(schedule_kind="cron", schedule_expression="nope") == {
            "schedule"
        }
        assert await refused_fields(schedule_expression="60", time_zone="Nope/Zone") == {
            "time_zone"
        }
        assert await refused_fields(parameters={"bogus": 1}) == {"parameters"}
        assert await refused_fields(subsystem=REFRESHER) == {"subsystem"}
        assert await refused_fields(subsystem="no-such-subsystem") == {"subsystem"}
        assert await refused_fields(environment_id=None) == {"environment"}
        assert await refused_fields(environment_id=9999) == {"environment"}
        assert await refused_fields(overrun_policy="bogus") == {"overrun_policy"}
        assert await refused_fields(name="  ") == {"name"}
        assert await refused_fields(name="x" * 65) == {"name"}
        assert await refused_fields(target_selector="x" * (MAX_SELECTOR_BYTES + 1)) == {
            "target_selector"
        }
        assert await refused_fields(parameters={"note": "x" * 20_000}) == {"parameters"}
        assert await refused_fields(schedule_kind="weekly") == {"schedule"}


async def test_a_schedule_the_subsystem_cannot_use_is_refused_as_a_schedule_error(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        with pytest.raises(ScheduleInvalid):
            await services.rules.create(
                ADMIN, **rule_arguments(seeded.remote_id, schedule_expression="5")
            )


async def test_cancel_previous_is_accepted_only_for_a_subsystem_that_is_safe_to_cancel(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        safe = await services.rules.create(
            ADMIN, **rule_arguments(seeded.remote_id, overrun_policy="cancel_previous")
        )
        with pytest.raises(ValidationFailed) as unsafe:
            await services.rules.create(
                ADMIN,
                **rule_arguments(
                    None,
                    subsystem=UPDATER,
                    name="Update",
                    target_selector="",
                    overrun_policy="cancel_previous",
                ),
            )

    assert (await job_of_rule(started_app, safe.id))["overrun_policy"] == "cancel_previous"
    assert "overrun_policy" in unsafe.value.fields


async def test_a_rule_may_set_how_many_failures_disable_it(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(
            ADMIN, **rule_arguments(seeded.remote_id, parameters={"max_consecutive_failures": 2})
        )

    assert (await job_of_rule(started_app, rule.id))["failure_threshold"] == 2


async def test_rule_names_are_unique_per_owner_and_an_owner_may_hold_only_fifty_rules(
    started_app: FastAPI, seeded: World
) -> None:
    async with started_app.state.database.session() as db:
        await EnvironmentAccessRepository(db).add_user("environment", seeded.remote_id, STANDARD_ID)
    arguments = rule_arguments(seeded.remote_id, name="Shared")
    async with job_services(started_app) as services:
        await services.rules.create(ADMIN, **arguments)
        await services.rules.create(STANDARD, **arguments)
        with pytest.raises(ValidationFailed) as repeated:
            await services.rules.create(STANDARD, **(arguments | {"name": "SHARED"}))
        for number in range(MAX_RULES_PER_OWNER - 1):
            await services.rules.create(ADMIN, **(arguments | {"name": f"n{number}"}))
        with pytest.raises(ValidationFailed) as capped:
            await services.rules.create(ADMIN, **(arguments | {"name": "one-too-many"}))
        await services.rules.create(STANDARD, **(arguments | {"name": "another-owner"}))

    assert "name" in repeated.value.fields
    assert "rules" in capped.value.fields
    environments = await rows(
        started_app, "SELECT environment_id FROM scheduled_job WHERE rule_id IS NOT NULL"
    )
    assert {row["environment_id"] for row in environments} == {seeded.remote_id}


async def test_a_standard_user_needs_a_grant_and_cannot_use_a_local_socket_host(
    started_app: FastAPI, seeded: World
) -> None:
    async with started_app.state.database.session() as db:
        await EnvironmentAccessRepository(db).add_user("environment", seeded.remote_id, STANDARD_ID)
    async with job_services(started_app) as services:
        with pytest.raises(ResourceNotFound):
            await services.rules.create(STANDARD, **rule_arguments(seeded.down_id))
        with pytest.raises(PermissionDenied):
            await services.rules.create(STANDARD, **rule_arguments(seeded.local_id))
        with pytest.raises(PermissionDenied):
            await services.rules.create(
                STANDARD, **rule_arguments(seeded.remote_id, target_selector=FORBIDDEN_SELECTOR)
            )
        mark = await last_event_id(started_app)
        rule = await services.rules.create(STANDARD, **rule_arguments(seeded.remote_id))

    [created] = [e for e in await events(started_app, since=mark) if e["result"] == "success"]
    assert (created["actor_user_id"], created["environment_id"]) == (STANDARD_ID, seeded.remote_id)
    assert rule.created_by_user_id == STANDARD_ID


async def test_a_standard_user_may_not_create_a_rule_that_is_not_about_one_host(
    started_app: FastAPI,
) -> None:
    async with job_services(started_app) as services:
        with pytest.raises(PermissionDenied):
            await services.rules.create(
                STANDARD, **rule_arguments(None, subsystem=UPDATER, name="Update")
            )


async def test_a_standard_user_sees_and_changes_only_their_own_rules(
    started_app: FastAPI, seeded: World
) -> None:
    async with started_app.state.database.session() as db:
        await EnvironmentAccessRepository(db).add_user("environment", seeded.remote_id, STANDARD_ID)
    async with job_services(started_app) as services:
        own = await services.rules.create(STANDARD, **rule_arguments(seeded.remote_id))
        admins = await services.rules.create(
            ADMIN, **rule_arguments(seeded.remote_id, name="admin-rule")
        )

        listed_for_standard = await services.rules.page(STANDARD, limit=50)
        listed_for_admin = await services.rules.page(ADMIN, limit=50)
        with pytest.raises(ResourceNotFound):
            await services.rules.get(STANDARD, admins.id)
        with pytest.raises(ResourceNotFound):
            await services.rules.enable(STANDARD, admins.id)
        with pytest.raises(ResourceNotFound):
            await services.rules.pause(STANDARD, admins.id, duration=ONE_HOUR)

    assert [r.id for r in listed_for_standard.items] == [own.id]
    assert {r.id for r in listed_for_admin.items} == {own.id, admins.id}


async def test_a_rule_listing_refuses_an_unknown_subsystem_filter(started_app: FastAPI) -> None:
    async with job_services(started_app) as services:
        with pytest.raises(ValidationFailed):
            await services.rules.page(ADMIN, limit=10, subsystem="nope")


async def test_a_users_lost_grant_stops_them_enabling_editing_or_resuming_but_not_disabling(
    started_app: FastAPI, seeded: World
) -> None:
    async with started_app.state.database.session() as db:
        await EnvironmentAccessRepository(db).add_user("environment", seeded.remote_id, STANDARD_ID)
    async with job_services(started_app) as services:
        rule = await services.rules.create(STANDARD, **rule_arguments(seeded.remote_id))
        await services.rules.enable(STANDARD, rule.id)
        await services.rules.pause(STANDARD, rule.id, duration=ONE_HOUR)
    async with started_app.state.database.session() as db:
        await EnvironmentAccessRepository(db).remove_user(
            "environment", seeded.remote_id, STANDARD_ID
        )
    async with job_services(started_app) as services:
        with pytest.raises(ResourceNotFound):
            await services.rules.resume(STANDARD, rule.id)
        with pytest.raises(ResourceNotFound):
            await services.rules.enable(STANDARD, rule.id)
        with pytest.raises(ResourceNotFound):
            await services.rules.update(
                STANDARD,
                rule.id,
                name="Nightly",
                target_selector="web",
                parameters={},
                schedule_kind="interval",
                schedule_expression="90",
                time_zone="UTC",
                overrun_policy=None,
            )
        await services.rules.disable(STANDARD, rule.id)

    assert (await job_of_rule(started_app, rule.id))["next_run_at"] is None


async def test_a_rule_whose_owner_lost_access_fails_its_next_run_and_is_disabled_by_repeats(
    started_app: FastAPI, seeded: World
) -> None:
    async with started_app.state.database.session() as db:
        await EnvironmentAccessRepository(db).add_user("environment", seeded.remote_id, STANDARD_ID)
    async with job_services(started_app) as services:
        rule = await services.rules.create(
            STANDARD, **rule_arguments(seeded.remote_id, parameters={"max_consecutive_failures": 1})
        )
        await services.rules.enable(STANDARD, rule.id)
    async with started_app.state.database.session() as db:
        await EnvironmentAccessRepository(db).remove_user(
            "environment", seeded.remote_id, STANDARD_ID
        )
    job = await job_of_rule(started_app, rule.id)
    await execute(
        started_app,
        "UPDATE scheduled_job SET next_run_at = ? WHERE id = ?",
        (
            (started_app.state.auth.clock() - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            job["id"],
        ),
    )

    scheduler = started_app.state.scheduler.scheduler
    await scheduler.tick()
    await scheduler.idle()

    [run] = await rows(
        started_app, "SELECT outcome, reason FROM job_run WHERE scheduled_job_id = ?", (job["id"],)
    )
    after = await job_of_rule(started_app, rule.id)
    disabled = await rows(
        started_app,
        "SELECT actor_subsystem, rule_id FROM activity_event "
        "WHERE action = 'scheduled_job_disabled'",
    )
    assert run == {"outcome": "failure", "reason": "owner_access_lost"}
    assert (after["is_enabled"], after["next_run_at"]) == (0, None)
    assert disabled == [{"actor_subsystem": RESTARTER, "rule_id": rule.id}]


DIGEST_LENGTH = 64
TARGET_NAMES = ("web",)
OTHERS = 2
UNREACHABLE: UnavailableKind = "unreachable"


async def test_the_digest_covers_the_selector_parameters_and_schedule_and_ignores_the_rest(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **rule_arguments(seeded.remote_id))
        job = (await services.rules.enable_preview(ADMIN, rule.id)).view.job

    base = rule_digest(rule, job)
    reordered_a = replace(rule, parameters='{"a":1,"b":2}', target_selector='{"x":1,"y":2}')
    reordered_b = replace(rule, parameters='{ "b": 2, "a": 1 }', target_selector='{"y":2, "x":1}')
    changed = {
        rule_digest(replace(rule, target_selector="other"), job),
        rule_digest(replace(rule, parameters='{"note":"x"}'), job),
        rule_digest(rule, replace(job, schedule_kind="cron")),
        rule_digest(rule, replace(job, schedule_expression="61")),
        rule_digest(rule, replace(job, time_zone="Europe/Paris")),
    }

    assert len(base) == DIGEST_LENGTH
    assert rule_digest(reordered_a, job) == rule_digest(reordered_b, job)
    assert len(changed | {base}) == len(changed) + 1
    renamed = replace(rule, name="Other", is_enabled=True, updated_at="2099-01-01T00:00:00Z")
    assert rule_digest(renamed, job) == base


async def test_enabling_is_refused_when_the_rule_changed_since_review_even_in_the_same_second(
    started_app: FastAPI, seeded: World
) -> None:
    mark = await last_event_id(started_app)
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **rule_arguments(seeded.remote_id))
        reviewed = await services.rules.enable_preview(ADMIN, rule.id)
        await services.rules.update(
            ADMIN,
            rule.id,
            name="Nightly",
            target_selector="web2",
            parameters={},
            schedule_kind="interval",
            schedule_expression="60",
            time_zone="UTC",
            overrun_policy=None,
        )

        with pytest.raises(ValidationFailed):
            await services.rules.enable(ADMIN, rule.id, expected_digest=reviewed.digest)
        refused = (await services.rules.get(ADMIN, rule.id)).is_enabled
        fresh = await services.rules.enable_preview(ADMIN, rule.id)
        await services.rules.enable(ADMIN, rule.id, expected_digest=fresh.digest)
        enabled = (await services.rules.get(ADMIN, rule.id)).is_enabled

    assert reviewed.digest == rule_digest(rule, reviewed.view.job)
    assert (refused, enabled) == (False, True)
    [failure] = [
        e
        for e in await events(started_app, since=mark)
        if e["action"] == "automation_rule_enabled" and e["result"] == "failure"
    ]
    assert json.loads(failure["detail"])["reason"] == "changed_since_review"


async def test_a_preview_asks_the_subsystem_as_the_viewer_and_keeps_another_users_rule_hidden(
    started_app: FastAPI, seeded: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with started_app.state.database.session() as db:
        await EnvironmentAccessRepository(db).add_user("environment", seeded.remote_id, STANDARD_ID)
    asked: list[tuple[int, int, str]] = []

    async def preview(
        self: ScriptedRestarter, principal: Any, environment_id: int, selector: str
    ) -> TargetPreview:
        asked.append((principal.user_id, environment_id, selector))
        return TargetPreview(TARGET_NAMES, OTHERS)

    async with job_services(started_app) as services:
        own = await services.rules.create(STANDARD, **rule_arguments(seeded.remote_id))
        admins = await services.rules.create(
            ADMIN, **rule_arguments(seeded.remote_id, name="Admin rule")
        )
        without_listing = await services.rules.run_preview(ADMIN, own.id)
        monkeypatch.setattr(ScriptedRestarter, "preview_targets", preview)
        with pytest.raises(ResourceNotFound):
            await services.rules.enable_preview(STANDARD, admins.id)
        with pytest.raises(ResourceNotFound):
            await services.rules.run_preview(STANDARD, admins.id)
        listed = await services.rules.run_preview(ADMIN, own.id)

    assert without_listing.target is None
    assert listed.target == TargetPreview(TARGET_NAMES, OTHERS)
    assert asked == [(ADMIN.user_id, seeded.remote_id, "web")]


async def test_a_preview_of_an_unreachable_host_carries_the_reason_instead_of_failing(
    started_app: FastAPI, seeded: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(self: ScriptedRestarter, *arguments: Any) -> TargetPreview:
        raise EnvironmentUnavailable(UNREACHABLE)

    monkeypatch.setattr(ScriptedRestarter, "preview_targets", down)
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **rule_arguments(seeded.remote_id))
        enabling = await services.rules.enable_preview(ADMIN, rule.id)
        running = await services.rules.run_preview(ADMIN, rule.id)

    for preview in (enabling, running):
        assert preview.target is not None
        assert (preview.target.names, preview.target.others) == ((), 0)
        assert preview.target.note


SYNCER = "backup-syncer"
EXPORTER = "activity-log-exporter"
MISSING_TARGET = 999
INSERT_TARGET = (
    "INSERT INTO object_storage_target (name, endpoint_url, region, bucket, access_key_id, "
    "secret_access_key_ciphertext, use_path_style, verify_tls, backup_prefix, "
    "activity_log_prefix, created_at, updated_at) VALUES ('target', 'https://s3.example', "
    "'us-east-1', 'b', 'AK', 'v1:x', 1, 1, 'backups', 'logs', 'x', 'x')"
)


def target_rule(subsystem: str, target_id: int, **changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "subsystem": subsystem,
        "name": "to-bucket",
        "environment_id": None,
        "target_selector": "",
        "parameters": {"object_storage_target_id": target_id},
        "schedule_kind": "interval",
        "schedule_expression": "3600",
    }
    return base | changes


@pytest.mark.parametrize("subsystem", [SYNCER, EXPORTER])
async def test_a_rule_aimed_at_a_target_that_does_not_exist_is_refused_on_the_target_field(
    started_app: FastAPI, subsystem: str
) -> None:
    async with job_services(started_app) as services:
        with pytest.raises(ValidationFailed) as refused:
            await services.rules.create(ADMIN, **target_rule(subsystem, MISSING_TARGET))

    assert refused.value.fields == {"object_storage_target_id": TARGET_MISSING}
    assert await rows(started_app, "SELECT 1 FROM automation_rule") == []


@pytest.mark.parametrize("subsystem", [SYNCER, EXPORTER])
async def test_a_rule_aimed_at_an_existing_target_is_created_without_an_environment(
    started_app: FastAPI, subsystem: str
) -> None:
    await execute(started_app, INSERT_TARGET)
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **target_rule(subsystem, 1))

    assert (rule.subsystem, rule.environment_id) == (subsystem, None)


async def test_repointing_a_rule_at_a_missing_target_is_refused_and_leaves_the_rule(
    started_app: FastAPI,
) -> None:
    await execute(started_app, INSERT_TARGET)
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **target_rule(EXPORTER, 1))
        with pytest.raises(ValidationFailed) as refused:
            await services.rules.update(
                ADMIN,
                rule.id,
                name="to-bucket",
                target_selector="",
                parameters={"object_storage_target_id": MISSING_TARGET},
                schedule_kind="interval",
                schedule_expression="3600",
                time_zone="UTC",
                overrun_policy=None,
            )
        unchanged = await services.rules.get(ADMIN, rule.id)

    assert refused.value.fields == {"object_storage_target_id": TARGET_MISSING}
    assert json.loads(unchanged.parameters)["object_storage_target_id"] == 1


@pytest.mark.parametrize(
    ("selector", "described"),
    [("", INSTANCE_TARGET), ("garbage{", "Unreadable target")],
    ids=["no-selector", "unreadable-selector"],
)
def test_a_rule_without_a_selector_is_described_as_this_instance(
    selector: str, described: str
) -> None:
    assert describe_target(selector) == described
