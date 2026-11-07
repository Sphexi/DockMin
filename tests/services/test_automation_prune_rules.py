"""Saving a container-updater rule that removes the images its updates replaced."""

import json
from typing import Any

import pytest
from fastapi import FastAPI

from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.automation_rule import AutomationRule
from app.db.repositories.replaced_image import ReplacedImageRepository
from app.services.automation_rules import (
    PRUNE_NEEDS_ADMINISTRATOR,
    PRUNE_NEEDS_UPDATE,
    PRUNE_NOT_FOR_SERVICES,
)
from app.subsystems.container_updater import UpdaterParameters
from tests.access_support import ADMIN, STANDARD
from tests.docker_engine.container_engine import ContainerEngine
from tests.job_support import job_services, job_services_on_hosts
from tests.web.environment_support import World

UPDATER = "container-updater"
LABELS = json.dumps({"kind": "labels", "labels": [["app", "web"]]})
SERVICE = json.dumps({"kind": "service", "names": ["web"]})
UPDATE = {"action": "update"}
PRUNING = {"action": "update", "prune_replaced": True, "prune_after_hours": 6}
IMAGE = "a" * 64
STACK_NAME = "shop"
INTERVAL = "3600"


def arguments(world: World, **changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "subsystem": UPDATER,
        "name": "Keep current",
        "environment_id": world.remote_id,
        "target_selector": LABELS,
        "parameters": PRUNING,
        "schedule_kind": "interval",
        "schedule_expression": INTERVAL,
    }
    return base | changes


def edit_to(rule: AutomationRule, **changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": rule.name,
        "target_selector": rule.target_selector,
        "parameters": json.loads(rule.parameters),
        "schedule_kind": "interval",
        "schedule_expression": INTERVAL,
        "time_zone": "UTC",
        "overrun_policy": None,
    }
    return base | changes


async def record_waiting_image(app: FastAPI, world: World, rule: AutomationRule) -> None:
    async with app.state.database.session() as db:
        await ReplacedImageRepository(db, app.state.auth.clock).add_replaced(
            rule, world.remote_id, IMAGE, "stack", STACK_NAME
        )


async def waiting_count(app: FastAPI, rule: AutomationRule) -> int:
    async with app.state.database.session() as db:
        found = await ReplacedImageRepository(db, app.state.auth.clock).waiting(rule)
    return found.count


def test_pruning_is_off_by_default_and_a_stored_rule_without_the_key_reads_as_off() -> None:
    defaults = UpdaterParameters()
    stored = UpdaterParameters.model_validate_json('{"action": "update", "verify_seconds": 10}')

    assert (defaults.prune_replaced, stored.prune_replaced) == (False, False)
    assert stored.prune_after_hours == defaults.prune_after_hours


@pytest.mark.parametrize(
    ("owner", "changes", "message"),
    [
        pytest.param(
            ADMIN, {"parameters": {"prune_replaced": True}}, PRUNE_NEEDS_UPDATE, id="report"
        ),
        pytest.param(ADMIN, {"target_selector": SERVICE}, PRUNE_NOT_FOR_SERVICES, id="service"),
        pytest.param(STANDARD, {}, PRUNE_NEEDS_ADMINISTRATOR, id="standard-owner"),
    ],
)
async def test_saving_a_pruning_rule_is_refused_where_it_could_not_remove_anything(
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    owner: Principal,
    changes: dict[str, Any],
    message: str,
) -> None:
    async with job_services_on_hosts(started_app) as services:
        with pytest.raises(ValidationFailed) as refused:
            await services.rules.create(owner, **arguments(world, **changes))

    assert refused.value.fields == {"prune_replaced": message}


async def test_an_administrator_saves_a_pruning_rule_with_its_grace_period(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(ADMIN, **arguments(world))

    stored = UpdaterParameters.model_validate_json(rule.parameters)
    assert (stored.prune_replaced, stored.prune_after_hours) == (True, 6)


async def test_an_administrator_cannot_turn_pruning_on_for_a_rule_a_standard_user_owns(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(STANDARD, **arguments(world, parameters=UPDATE))

        with pytest.raises(ValidationFailed) as refused:
            await services.rules.update(ADMIN, rule.id, **edit_to(rule, parameters=PRUNING))

    assert refused.value.fields == {"prune_replaced": PRUNE_NEEDS_ADMINISTRATOR}


async def test_turning_pruning_off_forgets_the_waiting_images_and_leaving_it_on_keeps_them(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(ADMIN, **arguments(world))
        await record_waiting_image(started_app, world, rule)
        await services.rules.update(ADMIN, rule.id, **edit_to(rule, name="Renamed"))
        kept = await waiting_count(started_app, rule)

        await services.rules.update(ADMIN, rule.id, **edit_to(rule, parameters=UPDATE))

    assert kept == 1
    assert await waiting_count(started_app, rule) == 0


async def test_deleting_the_rule_removes_its_waiting_images(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(ADMIN, **arguments(world))
        await record_waiting_image(started_app, world, rule)

        await services.rules.delete(ADMIN, rule.id, expected_name=rule.name)

    async with started_app.state.database.session() as db:
        row = await db.fetch_one("SELECT COUNT(*) AS found FROM replaced_image")
    assert row is not None and row["found"] == 0
