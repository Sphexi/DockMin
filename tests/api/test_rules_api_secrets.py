"""No secret field in a rule response: every subsystem's parameters and an example rule of each."""

import json
import re
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, SecretStr

from app.api.rules import RulePageModel
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.services.activity import SUBSYSTEMS
from app.subsystems.base import Subsystem
from tests.auth_support import ADMIN_ID, ADMIN_SESSION_TOKEN, Browsers
from tests.web.environment_support import World, bearer, issue_token

SECRET_NAME = re.compile(r"secret|passw|token|credential|private|api_?key|access_?key", re.I)
FAR_FUTURE = "2030-01-01T00:00:00Z"
EXAMPLE_ID = 1


class HoldsASecret(BaseModel):
    api_key: SecretStr


def secret_names(value: Any, path: str = "") -> list[str]:
    """Return the path of every mapping key, at any depth, whose name looks like a secret."""
    if isinstance(value, dict):
        return [
            found
            for key, item in value.items()
            for found in (
                ([f"{path}/{key}"] if SECRET_NAME.search(str(key)) else [])
                + secret_names(item, f"{path}/{key}")
            )
        ]
    if isinstance(value, list):
        return [found for item in value for found in secret_names(item, path)]
    return []


def secret_typed(model: type[BaseModel]) -> list[str]:
    """Return the fields declared as a secret type, by name."""
    return [
        name for name, field in model.model_fields.items() if "Secret" in repr(field.annotation)
    ]


def example_parameters(model: type[BaseModel]) -> dict[str, Any]:
    """Return a valid parameters document: the required fields set, the rest left at defaults."""
    required = {
        name: EXAMPLE_ID for name, field in model.model_fields.items() if field.is_required()
    }
    return model.model_validate(required).model_dump(mode="json")


def test_the_scanner_finds_a_secret_named_key_at_any_depth_and_a_secret_typed_field() -> None:
    document = {"parameters": {"api_key": "x"}, "items": [{"nested": {"Password": 1}}], "ok": 1}

    assert secret_names(document) == ["/parameters/api_key", "/items/nested/Password"]
    assert secret_typed(HoldsASecret) == ["api_key"]
    assert secret_names({"target": "web", "digest": "ab"}) == []


async def test_no_rule_response_nor_parameters_model_carries_a_secret_field(
    started_app: FastAPI, browsers: Browsers, world: World
) -> None:
    handlers = [
        started_app.state.scheduler.handlers.get(name)
        for name, ruled in SUBSYSTEMS.items()
        if ruled
    ]
    models = {
        h.name: h.parameters_model
        for h in handlers
        if isinstance(h, Subsystem) and h.parameters_model
    }
    clock = started_app.state.auth.clock
    async with started_app.state.database.session() as db:
        for name, model in models.items():
            rule = await AutomationRuleRepository(db, clock).insert(
                name=f"Example {name}",
                subsystem=name,
                environment_id=world.local_id,
                target_selector="",
                parameters=json.dumps(example_parameters(model)),
                is_enabled=False,
                created_by_user_id=ADMIN_ID,
            )
            await ScheduledJobRepository(db, clock).insert(
                subsystem=name,
                rule_id=rule.id,
                environment_id=world.local_id,
                schedule_kind="interval",
                schedule_expression="600",
                time_zone="UTC",
                overrun_policy="skip",
                catch_up=False,
                failure_threshold=5,
                next_run_at=FAR_FUTURE,
            )
    headers = bearer(await issue_token(browsers.open(session_token=ADMIN_SESSION_TOKEN)))

    listing = await browsers.open().get("/api/v1/rules?limit=500", headers=headers)

    served = listing.json()
    assert len(models) == len([ruled for ruled in SUBSYSTEMS.values() if ruled])
    assert {item["subsystem"] for item in served["items"]} == set(models)
    assert secret_names(served) == []
    assert [secret_typed(model) for model in models.values()] == [[]] * len(models)
    assert [secret_names(model.model_json_schema()) for model in models.values()] == [[]] * len(
        models
    )
    assert secret_names(RulePageModel.model_json_schema()) == []
