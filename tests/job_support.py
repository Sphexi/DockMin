"""Builders for the job and rule service, page and JSON tests: an app with scripted subsystems."""

import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.auth.errors import PermissionDenied
from app.auth.principal import Principal
from app.config import Settings
from app.logging import configure_logging
from app.main import create_app
from app.scheduler.model import RunResult
from app.services import assembly
from app.services.activity import ActivityRecorder
from app.services.assembly import JobServices, build_host_clients
from app.services.authorisation import UnavailableInspector
from app.services.object_inspector import EngineObjectInspector
from app.subsystems.base import RuleParameters, RunContext, Subsystem, SubsystemFactory
from tests.web.environment_support import World, seed_world

ADMIN = Principal(user_id=1, username="admin-user", role="administrator")
STANDARD = Principal(user_id=2, username="standard-user", role="standard")
REFRESHER = "environment-snapshot-refresher"
RESTARTER = "stack-restarter"
UPDATER = "container-updater"
FORBIDDEN_SELECTOR = "forbidden"
SETTLE_SECONDS = 20
EVENT_COLUMNS = (
    "id, action, actor_type, actor_user_id, actor_subsystem, rule_id, job_run_id, "
    "environment_id, target_type, target_id, result, detail"
)


class NoteParameters(RuleParameters):
    """Parameters of the scripted subsystems; the note is not secret and not logged."""

    note: str = ""


class ScriptedRestarter(Subsystem):
    """A rule-bearing subsystem that does nothing, safe to cancel and able to dry run."""

    name = RESTARTER
    parameters_model = NoteParameters
    requires_environment = True
    min_interval_seconds = 15
    allowed_overrun_policies = frozenset({"skip", "queue", "cancel_previous"})
    cancellation_safe = True
    supports_dry_run = True

    def parse_selector(self, text: str) -> str:
        return text.strip()

    async def authorise_target(
        self,
        principal: Principal,
        environment_id: int | None,
        selector: str,
        authoriser: Any,
    ) -> None:
        if selector == FORBIDDEN_SELECTOR:
            raise PermissionDenied("insufficient access")

    async def run(self, context: RunContext) -> RunResult:
        return RunResult("success", attempted=0)


class ScriptedUpdater(Subsystem):
    """A subsystem with no host, offering cancel_previous although it is not safe to cancel."""

    name = UPDATER
    parameters_model = NoteParameters
    allowed_overrun_policies = frozenset({"skip", "queue", "cancel_previous"})

    async def authorise_target(
        self,
        principal: Principal,
        environment_id: int | None,
        selector: str,
        authoriser: Any,
    ) -> None:
        return None

    async def run(self, context: RunContext) -> RunResult:
        return RunResult("success", attempted=0)


JOB_SUBSYSTEMS: tuple[SubsystemFactory, ...] = (
    lambda deps: ScriptedRestarter(deps),
    lambda deps: ScriptedUpdater(deps),
)


def build_job_app(settings: Settings, log_stream: io.StringIO) -> FastAPI:
    """Build an application of its own with a scripted restarter and updater."""
    application = create_app(settings, extra_subsystems=JOB_SUBSYSTEMS)
    configure_logging(settings.log_level, stream=log_stream)
    return application


def withhold_handler(app: FastAPI, name: str) -> None:
    """Remove a subsystem's handler, as a build without that subsystem would lack it."""
    handlers = app.state.scheduler.handlers
    del handlers._handlers[name]


@asynccontextmanager
async def job_services(app: FastAPI) -> AsyncIterator[JobServices]:
    """Open the rule and job services on one session, as a request would."""
    runtime = app.state.auth
    async with app.state.database.session() as db:
        activity = ActivityRecorder(db, runtime.clock)
        access = assembly.build_access_services(db, runtime, UnavailableInspector(), activity)
        yield assembly.build_job_services(db, runtime, access, app.state.scheduler, activity)


@asynccontextmanager
async def job_services_on_hosts(app: FastAPI) -> AsyncIterator[JobServices]:
    """Open the rule and job services as a request would, with objects read from the hosts."""
    runtime = app.state.auth
    async with app.state.database.session() as db:
        activity = ActivityRecorder(db, runtime.clock)
        inspector = EngineObjectInspector(build_host_clients(db, runtime, app.state.engine))
        access = assembly.build_access_services(db, runtime, inspector, activity)
        yield assembly.build_job_services(
            db, runtime, access, app.state.scheduler, activity, inspector
        )


async def seed_settled_world(app: FastAPI) -> World:
    """Seed the environments and wait out the refresh each audited action on them starts.

    The seeded hosts cannot be reached, so the first refresh records one status transition.
    Settling it here keeps that event out of the events a test goes on to count.
    """
    world = await seed_world(app)
    refresher = app.state.engine.refresher
    environment_ids = (world.local_id, world.remote_id, world.down_id)
    for environment_id in environment_ids:
        refresher.poke(environment_id)
    for environment_id in environment_ids:
        assert await refresher.wait_settled(environment_id, SETTLE_SECONDS)
    return world


async def rows(app: FastAPI, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    async with app.state.database.session() as db:
        found = await db.fetch_limited(sql, params, limit=1000)
    return [dict(row) for row in found]


async def events(app: FastAPI, *, since: int = 0) -> list[dict[str, Any]]:
    return await rows(
        app,
        f"SELECT {EVENT_COLUMNS} FROM activity_event WHERE id > ? ORDER BY id",
        (since,),
    )


async def last_event_id(app: FastAPI) -> int:
    found = await rows(app, "SELECT COALESCE(MAX(id), 0) AS last FROM activity_event")
    return int(found[0]["last"])


async def execute(app: FastAPI, sql: str, params: tuple[Any, ...] = ()) -> None:
    async with app.state.database.session() as db:
        await db.execute(sql, params)


async def job_of_rule(app: FastAPI, rule_id: int) -> dict[str, Any]:
    return (await rows(app, "SELECT * FROM scheduled_job WHERE rule_id = ?", (rule_id,)))[0]


async def refresher_job_id(app: FastAPI) -> int:
    found = await rows(app, "SELECT id FROM scheduled_job WHERE subsystem = ?", (REFRESHER,))
    return int(found[0]["id"])


def rule_arguments(environment_id: int | None, **changes: Any) -> dict[str, Any]:
    """The arguments of a valid rule creation for the scripted restarter."""
    base: dict[str, Any] = {
        "subsystem": RESTARTER,
        "name": "Nightly",
        "environment_id": environment_id,
        "target_selector": "web",
        "parameters": {},
        "schedule_kind": "interval",
        "schedule_expression": "60",
    }
    return base | changes
