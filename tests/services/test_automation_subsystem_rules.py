"""Rules for the two built-in subsystems: what each accepts, and who may aim one at what."""

import json
from typing import Any

import pytest
from fastapi import FastAPI

from app.auth.errors import PermissionDenied, ValidationFailed
from app.scheduler.schedules import ScheduleInvalid
from app.subsystems.selectors import COMPOSE_ONLY, NOT_OFFERED, UNVERIFIED, TargetUnverified
from tests.access_support import ADMIN, STANDARD
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.fake_engine import Refuse
from tests.job_support import job_services, job_services_on_hosts
from tests.web.environment_support import World

HEALTH = "container-health-checker"
RESTARTER = "stack-restarter"
UPDATER = "container-updater"
LATE_ID = "c" * 64
CONTAINER = json.dumps({"kind": "container", "names": ["db"]})
ENVIRONMENT = json.dumps({"kind": "environment"})
LABELS = json.dumps({"kind": "labels", "labels": [["app", "web"]]})
RESTARTER_FLOOR = 3600


def arguments(world: World, subsystem: str = HEALTH, **changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "subsystem": subsystem,
        "name": "Rule",
        "environment_id": world.remote_id,
        "target_selector": CONTAINER,
        "parameters": {},
        "schedule_kind": "interval",
        "schedule_expression": "60" if subsystem == HEALTH else str(RESTARTER_FLOOR),
    }
    return base | changes


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"schedule_kind": "cron", "schedule_expression": "* * * * *"}, "schedule"),
        ({"overrun_policy": "queue"}, "overrun_policy"),
        ({"overrun_policy": "cancel_previous"}, "overrun_policy"),
        ({"parameters": {"max_restarts": 0}}, "parameters"),
        ({"parameters": {"max_restarts": 21}}, "parameters"),
        ({"parameters": {"window_seconds": 299}}, "parameters"),
        ({"parameters": {"window_seconds": 86401}}, "parameters"),
        ({"parameters": {"surprise": 1}}, "parameters"),
        ({"target_selector": "db"}, "target_selector"),
        ({"target_selector": json.dumps({"kind": "container", "name": "db"})}, "target_selector"),
        (
            {"target_selector": json.dumps({"kind": "container", "names": ["-bad"]})},
            "target_selector",
        ),
    ],
)
async def test_the_health_checker_refuses_what_it_cannot_run(
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    changes: dict[str, Any],
    field: str,
) -> None:
    async with job_services(started_app) as services:
        with pytest.raises(ValidationFailed) as refused:
            await services.rules.create(ADMIN, **arguments(world, **changes))

    assert field in refused.value.fields


async def test_the_health_checker_accepts_its_widest_target_and_the_floor_of_its_interval(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(
            ADMIN,
            **arguments(
                world,
                target_selector=ENVIRONMENT,
                parameters={"max_restarts": 20, "window_seconds": 86400},
            ),
        )
        with pytest.raises(ScheduleInvalid):
            await services.rules.create(
                ADMIN, **arguments(world, schedule_expression="59", name="x")
            )

    assert json.loads(rule.parameters) == {
        "suppression_seconds": None,
        "max_consecutive_failures": None,
        "max_restarts": 20,
        "window_seconds": 86400,
        "auto_release": False,
    }
    assert rule.is_enabled is False


async def test_the_restarter_takes_an_interval_of_an_hour_or_more_or_a_cron_expression(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    async with job_services(started_app) as services:
        with pytest.raises(ScheduleInvalid):
            await services.rules.create(
                ADMIN, **arguments(world, RESTARTER, schedule_expression=str(RESTARTER_FLOOR - 1))
            )
        hourly = await services.rules.create(ADMIN, **arguments(world, RESTARTER))
        nightly = await services.rules.create(
            ADMIN,
            **arguments(
                world,
                RESTARTER,
                name="Nightly",
                schedule_kind="cron",
                schedule_expression="0 3 * * *",
            ),
        )
        with pytest.raises(ValidationFailed) as queued:
            await services.rules.create(
                ADMIN, **arguments(world, RESTARTER, name="Queued", overrun_policy="queue")
            )

    assert (hourly.is_enabled, nightly.is_enabled) == (False, False)
    assert "overrun_policy" in queued.value.fields


@pytest.mark.parametrize("selector", [ENVIRONMENT, LABELS])
async def test_the_restarter_takes_a_container_or_a_stack_and_nothing_wider(
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    selector: str,
) -> None:
    async with job_services(started_app) as services:
        with pytest.raises(ValidationFailed) as refused:
            await services.rules.create(
                ADMIN, **arguments(world, RESTARTER, target_selector=selector)
            )

    assert refused.value.fields["target_selector"] == NOT_OFFERED


async def test_a_swarm_stack_is_refused_as_a_target_with_the_reason_given(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    async with started_app.state.database.session() as db:
        await db.execute(
            "INSERT INTO stack (environment_id, name, kind, origin, project_directory, "
            "compose_file, compose_file_digest, created_at, updated_at) "
            "VALUES (?, 'swarmy', 'swarm', 'created', ?, 'x', ?, 'x', 'x')",
            (world.remote_id, "a" * 32, "b" * 64),
        )
    selector = json.dumps({"kind": "stack", "names": ["swarmy"]})

    async with job_services(started_app) as services:
        with pytest.raises(ValidationFailed) as refused:
            await services.rules.create(
                ADMIN, **arguments(world, RESTARTER, target_selector=selector)
            )

    assert refused.value.fields["target_selector"] == COMPOSE_ONLY


async def test_a_standard_user_may_target_only_containers_they_may_change(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    def named(*names: str) -> str:
        return json.dumps({"kind": "container", "names": list(names)})

    async with job_services_on_hosts(started_app) as services:
        allowed = await services.rules.create(
            STANDARD, **arguments(world, target_selector=named("db"), name="a")
        )
        ahead = await services.rules.create(
            STANDARD, **arguments(world, target_selector=named("not-yet"), name="b")
        )
        several = await services.rules.create(
            STANDARD, **arguments(world, target_selector=named("db", "not-yet", "web"), name="c")
        )
        with pytest.raises(PermissionDenied):
            await services.rules.create(
                STANDARD, **arguments(world, target_selector=named("db", "job"), name="d")
            )

    assert {rule.created_by_user_id for rule in (allowed, ahead, several)} == {STANDARD.user_id}


async def test_a_container_the_user_cannot_see_is_saved_as_if_it_did_not_exist(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    hidden = json.dumps({"kind": "container", "names": ["web"]})
    absent = json.dumps({"kind": "container", "names": ["not-yet"]})

    async with job_services_on_hosts(started_app) as services:
        saved = await services.rules.create(
            STANDARD, **arguments(world, target_selector=hidden, name="a")
        )
        beside = await services.rules.create(
            STANDARD, **arguments(world, target_selector=absent, name="b")
        )

    assert saved.target_selector == '{"kind":"container","names":["web"]}'
    assert (saved.is_enabled, beside.is_enabled) == (False, False)


async def test_the_updater_refuses_a_compose_managed_container_at_save_and_at_enable(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    def named(name: str) -> str:
        return json.dumps({"kind": "container", "names": [name]})

    managed = (
        "db is managed by the Compose project shop. Choose the stack shop as the target instead."
    )

    async with job_services_on_hosts(started_app) as services:
        later = await services.rules.create(
            ADMIN, **arguments(world, UPDATER, target_selector=named("later"), name="later")
        )
        shared_host.add(LATE_ID, "later", labels={"com.docker.compose.project": "blog"})
        with pytest.raises(ValidationFailed) as by_admin:
            await services.rules.create(
                ADMIN, **arguments(world, UPDATER, target_selector=named("db"), name="a")
            )
        with pytest.raises(ValidationFailed) as by_standard:
            await services.rules.create(
                STANDARD, **arguments(world, UPDATER, target_selector=named("db"), name="b")
            )
        unseen = await services.rules.create(
            STANDARD, **arguments(world, UPDATER, target_selector=named("web"), name="c")
        )
        plain = await services.rules.create(
            ADMIN, **arguments(world, UPDATER, target_selector=named("cache"), name="d")
        )
        with pytest.raises(ValidationFailed) as at_enable:
            await services.rules.enable(ADMIN, later.id)

    assert by_admin.value.fields == {"target_selector": managed}
    assert by_standard.value.fields == {"target_selector": managed}
    assert at_enable.value.fields == {
        "target_selector": (
            "later is managed by the Compose project blog. "
            "Choose the stack blog as the target instead."
        )
    }
    assert (unseen.is_enabled, plain.is_enabled) == (False, False)


async def test_a_container_that_cannot_be_looked_up_leaves_the_target_unverified(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    shared_host.route("GET", "/containers/json", Refuse())

    async with job_services_on_hosts(started_app) as services:
        with pytest.raises(TargetUnverified) as unverified:
            await services.rules.create(ADMIN, **arguments(world))

    assert str(unverified.value) == UNVERIFIED
