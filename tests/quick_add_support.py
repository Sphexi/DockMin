"""Rules, objects and reviews for the quick add service and page tests."""

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI

from app.auth.principal import Principal
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.db.repositories.user import UserRepository
from app.services.activity import ActivityRecorder, SubsystemActor
from app.services.assembly import JobServices
from app.services.automation_quick_add import ObjectKind, QuickAddObject
from app.services.restart_holds import EXHAUSTED_ACTION
from tests.auth_support import ADMIN_ID
from tests.job_support import execute
from tests.stack_support import seed_stack_row

HEALTH = "container-health-checker"
RESTARTER = "stack-restarter"
UPDATER = "container-updater"
EXPORTER = "container-log-exporter"
INTERVAL_SECONDS = "3600"
NEVER = "2999-01-01T00:00:00Z"
LONG_AGO = "2020-01-01T00:00:00Z"
COMPOSE_FILE = "services:\n  web:\n    image: nginx:1\n"
FAILURES_TO_DISABLE = 5
PASSWORD_HASH = "not-a-real-hash"
EXHAUSTED_REASON = "restart_budget_exhausted"


def selector_json(kind: str, *names: str) -> str:
    """Return a stored selector of a named kind, or the labels or environment kind with no names."""
    body: dict[str, Any] = {"kind": kind}
    if names:
        body["names"] = list(names)
    if kind == "labels":
        body["labels"] = [["app", "web"]]
    return json.dumps(body, separators=(",", ":"))


async def insert_rule(
    app: FastAPI,
    *,
    subsystem: str,
    selector: str,
    name: str,
    environment_id: int,
    owner: int = ADMIN_ID,
    enabled: bool = True,
    paused_until: str | None = None,
    job_disabled: bool = False,
    parameters: str = "{}",
) -> int:
    """Insert a rule and its job as a finished creation and enabling would, and return the rule id.

    job_disabled leaves the job disabled by repeated failures, as the scheduler would.
    """
    clock = app.state.auth.clock
    async with app.state.database.session() as db:
        rule = await AutomationRuleRepository(db, clock).insert(
            name=name,
            subsystem=subsystem,
            environment_id=environment_id,
            target_selector=selector,
            parameters=parameters,
            is_enabled=enabled,
            created_by_user_id=owner,
        )
        await ScheduledJobRepository(db, clock).insert(
            subsystem=subsystem,
            rule_id=rule.id,
            environment_id=environment_id,
            schedule_kind="interval",
            schedule_expression=INTERVAL_SECONDS,
            time_zone="UTC",
            overrun_policy="skip",
            catch_up=False,
            failure_threshold=FAILURES_TO_DISABLE,
            next_run_at=format_timestamp(clock() + timedelta(hours=24)),
        )
    if paused_until is not None:
        await execute(
            app, "UPDATE automation_rule SET paused_until = ? WHERE id = ?", (paused_until, rule.id)
        )
    if job_disabled:
        await execute(
            app,
            "UPDATE scheduled_job SET is_enabled = 0, consecutive_failures = ?, "
            "next_run_at = NULL WHERE rule_id = ?",
            (FAILURES_TO_DISABLE, rule.id),
        )
    return rule.id


async def age_rule(app: FastAPI, rule_id: int) -> None:
    """Stamp a rule as last saved long ago, so any later write moves its stamp on a coarse clock."""
    await execute(
        app, "UPDATE automation_rule SET updated_at = ? WHERE id = ?", (LONG_AGO, rule_id)
    )


async def seed_compose_stack(app: FastAPI, environment_id: int, name: str) -> None:
    """Insert a managed Compose stack row, as a finished creation would have left it."""
    engine = app.state.engine
    async with app.state.database.session() as db:
        stand_in = SimpleNamespace(
            db=db,
            registry=SimpleNamespace(vault=engine.vault),
            clock=app.state.auth.clock,
            compose=engine.compose,
        )
        await seed_stack_row(stand_in, environment_id, name, COMPOSE_FILE)  # type: ignore[arg-type]


def object_of(
    environment_id: int,
    kind: ObjectKind,
    key: str,
    name: str,
    labels: dict[str, str] | None = None,
) -> QuickAddObject:
    return QuickAddObject(environment_id, kind, key, name, labels or {})


async def review(
    services: JobServices, actor: Principal, rule_id: int, obj: QuickAddObject
) -> dict[str, str]:
    """Return the values a confirmation page binds an add to, read as the confirm page does."""
    preview = await services.rules.quick_add_preview(actor, rule_id, obj)
    return {
        "expected_updated_at": preview.view.rule.updated_at,
        "expected_digest": preview.digest,
        "expected_name": obj.name,
    }


async def seed_standard_user(app: FastAPI, username: str, *, enabled: bool = True) -> int:
    """Insert a standard user with no grants, optionally disabled, and return its id."""
    async with app.state.database.session() as db:
        users = UserRepository(db, app.state.auth.clock)
        user = await users.create(username, PASSWORD_HASH, "standard")
        if not enabled:
            await users.disable(user.id)
    return user.id


async def hold_containers(
    app: FastAPI, rule_id: int, environment_id: int, *container_ids: str
) -> None:
    """Record a restart-budget exhaustion by the rule for each container, as its run would."""
    clock = app.state.auth.clock
    async with app.state.database.session() as db:
        rule = await AutomationRuleRepository(db, clock).get(rule_id)
        assert rule is not None
        recorder = ActivityRecorder(db, clock)
        for container_id in container_ids:
            await recorder.record_failure(
                SubsystemActor(rule.subsystem, rule.id, None, rule.name),
                action=EXHAUSTED_ACTION,
                target_type="container",
                target_id=container_id,
                environment_id=environment_id,
                reason=EXHAUSTED_REASON,
                detail={"name": "web"},
            )
