"""Creating a webhook rule: the targets it accepts, and the one place its token ever appears."""

import json
from typing import Any

import pytest
from fastapi import FastAPI

from app.auth.errors import ValidationFailed
from app.auth.tokens import hash_token
from app.services.automation_rules import WEBHOOK_NEEDS_TOKEN
from app.services.webhooks import WEBHOOK_SUBSYSTEM
from app.subsystems.selectors import MAX_TARGET_NAMES
from tests.container_support import DB
from tests.docker_engine.container_engine import ContainerEngine
from tests.job_support import ADMIN, events, job_services, job_services_on_hosts, rows
from tests.web.environment_support import World

LABELS = json.dumps({"kind": "labels", "labels": [["app", "web"]]})
ENVIRONMENT = json.dumps({"kind": "environment"})
CONTAINER = json.dumps({"kind": "container", "names": [DB]})
TOO_MANY_NAMES = json.dumps(
    {"kind": "container", "names": [f"box-{number}" for number in range(MAX_TARGET_NAMES + 1)]}
)
SCHEDULE_KIND = "webhook"


def arguments(world: World, **changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "subsystem": WEBHOOK_SUBSYSTEM,
        "name": "Deploy web",
        "environment_id": world.remote_id,
        "target_selector": CONTAINER,
        "parameters": {},
        "schedule_kind": SCHEDULE_KIND,
        "schedule_expression": "",
    }
    return base | changes


@pytest.mark.parametrize(
    "selector",
    [
        pytest.param(LABELS, id="labels"),
        pytest.param(ENVIRONMENT, id="whole-environment"),
        pytest.param(TOO_MANY_NAMES, id="more-names-than-a-selector-holds"),
    ],
)
async def test_a_webhook_rule_refuses_a_target_it_cannot_name_exactly(
    started_app: FastAPI, world: World, selector: str
) -> None:
    async with job_services(started_app) as services:
        with pytest.raises(ValidationFailed) as refused:
            await services.rules.create_issuing(ADMIN, **arguments(world, target_selector=selector))

    assert "target_selector" in refused.value.fields


async def test_creating_a_webhook_rule_without_a_way_to_show_its_token_is_refused(
    started_app: FastAPI, world: World
) -> None:
    async with job_services(started_app) as services:
        with pytest.raises(ValidationFailed) as refused:
            await services.rules.create(ADMIN, **arguments(world))

    assert refused.value.fields == {"subsystem": WEBHOOK_NEEDS_TOKEN}
    assert await rows(started_app, "SELECT id FROM automation_rule") == []


async def test_the_token_is_returned_once_and_stored_only_as_a_hash(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    async with job_services_on_hosts(started_app) as services:
        created = await services.rules.create_issuing(ADMIN, **arguments(world))

    assert created.webhook is not None
    token = created.webhook.value
    [stored] = await rows(started_app, "SELECT * FROM webhook")
    [job] = await rows(
        started_app, "SELECT * FROM scheduled_job WHERE rule_id = ?", (created.rule.id,)
    )
    connection = started_app.state.database.connect()
    try:
        dump = "\n".join(connection.iterdump())
    finally:
        connection.close()
    [creation] = [e for e in await events(started_app) if e["action"] == "automation_rule_created"]
    assert (stored["rule_id"], stored["token_prefix"]) == (created.rule.id, created.webhook.prefix)
    assert stored["token_hash"] == hash_token(token)
    assert (job["schedule_kind"], job["schedule_expression"], job["next_run_at"]) == (
        SCHEDULE_KIND,
        "",
        None,
    )
    assert created.rule.is_enabled is False
    assert token not in dump
    assert creation["detail"] is not None and token not in creation["detail"]
    assert created.webhook.prefix in creation["detail"]
