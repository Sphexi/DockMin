"""Run now on the built-in automations: the run acts, and its audit rows keep their own actors."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI

from tests.automation_support import HEALTH, STACK_RESTARTER, cid
from tests.docker_engine.container_engine import ContainerEngine
from tests.job_support import ADMIN, events, job_services_on_hosts, rows
from tests.stack_support import project_box, seed_stack_row
from tests.web.environment_support import World

IDLE_SECONDS = 20
SICK = "sick"
SHOP = "bakery"
SHOP_FILE = "services:\n  web:\n    image: nginx:1\n"
HEALTH_INTERVAL = "60"
RESTARTER_INTERVAL = "3600"
REQUESTED = "job_run_requested"


def target(kind: str, *names: str) -> str:
    return json.dumps({"kind": kind, "names": list(names)})


async def run_now(
    app: FastAPI,
    world: World,
    subsystem: str,
    selector: str,
    interval: str,
) -> tuple[int, int]:
    """Create and enable a rule, run it now, wait for the run, and return the rule and run ids."""
    async with job_services_on_hosts(app) as services:
        rule = await services.rules.create(
            ADMIN,
            subsystem=subsystem,
            name="Run now",
            environment_id=world.remote_id,
            target_selector=selector,
            parameters={},
            schedule_kind="interval",
            schedule_expression=interval,
        )
        await services.rules.enable(ADMIN, rule.id)
        run_id = await services.rules.run_now(ADMIN, rule.id)
        async with asyncio.timeout(IDLE_SECONDS):
            await app.state.scheduler.scheduler.idle()
    return rule.id, run_id


async def action_rows(app: FastAPI, action: str) -> list[dict[str, Any]]:
    return [row for row in await events(app) if row["action"] == action]


async def outcome_of(app: FastAPI, run_id: int) -> tuple[str, str | None]:
    [run] = await rows(app, "SELECT outcome, reason FROM job_run WHERE id = ?", (run_id,))
    return run["outcome"], run["reason"]


async def seed_shop(app: FastAPI, world: World) -> None:
    engine = app.state.engine
    async with app.state.database.session() as db:
        stand_in = SimpleNamespace(
            db=db,
            registry=SimpleNamespace(vault=engine.vault),
            clock=app.state.auth.clock,
            compose=engine.compose,
        )
        await seed_stack_row(stand_in, world.remote_id, SHOP, SHOP_FILE)  # type: ignore[arg-type]


async def test_run_now_on_a_health_rule_restarts_the_unhealthy_container_as_the_subsystem(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    shared_host.add(cid(0x900), SICK, health="unhealthy")

    rule_id, run_id = await run_now(
        started_app, world, HEALTH, target("container", SICK), HEALTH_INTERVAL
    )

    assert await outcome_of(started_app, run_id) == ("success", None)
    [restarted] = await action_rows(started_app, "container_restarted")
    assert (restarted["actor_type"], restarted["actor_subsystem"], restarted["rule_id"]) == (
        "subsystem",
        HEALTH,
        rule_id,
    )
    assert restarted["job_run_id"] == run_id
    [requested] = await action_rows(started_app, REQUESTED)
    assert (requested["actor_type"], requested["actor_user_id"]) == ("user", ADMIN.user_id)


async def test_run_now_on_a_restarter_rule_restarts_the_container_as_the_subsystem(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    shared_host.add(cid(0x900), SICK)

    rule_id, run_id = await run_now(
        started_app, world, STACK_RESTARTER, target("container", SICK), RESTARTER_INTERVAL
    )

    assert await outcome_of(started_app, run_id) == ("success", None)
    [restarted] = await action_rows(started_app, "container_restarted")
    assert (restarted["actor_type"], restarted["actor_subsystem"], restarted["rule_id"]) == (
        "subsystem",
        STACK_RESTARTER,
        rule_id,
    )
    [requested] = await action_rows(started_app, REQUESTED)
    assert (requested["actor_type"], requested["actor_user_id"]) == ("user", ADMIN.user_id)


async def test_run_now_on_a_restarter_rule_restarts_the_stack_as_the_subsystem(
    started_app: FastAPI, world: World, stack_host: ContainerEngine
) -> None:
    await seed_shop(started_app, world)
    project_box(stack_host, 1, SHOP, "web", state="running")

    rule_id, run_id = await run_now(
        started_app, world, STACK_RESTARTER, target("stack", SHOP), RESTARTER_INTERVAL
    )

    assert await outcome_of(started_app, run_id) == ("success", None)
    [restarted] = await action_rows(started_app, "stack_restarted")
    assert (restarted["actor_type"], restarted["actor_subsystem"], restarted["rule_id"]) == (
        "subsystem",
        STACK_RESTARTER,
        rule_id,
    )
    [requested] = await action_rows(started_app, REQUESTED)
    assert (requested["actor_type"], requested["actor_user_id"]) == ("user", ADMIN.user_id)
