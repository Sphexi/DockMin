"""Exporter rules: administrators only on every path, and the figure a deletion would release."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import FastAPI

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.config import Settings
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.services.assembly import JobServices
from tests.conftest import AttachApp
from tests.job_support import ADMIN, JOB_SUBSYSTEMS, STANDARD, job_services, rows

EXPORTER = "activity-log-exporter"
EVENTS = 5
CURSOR = 2
RULE_NAME = "export"
INSERT_TARGET = (
    "INSERT INTO object_storage_target (name, endpoint_url, region, bucket, access_key_id, "
    "secret_access_key_ciphertext, use_path_style, verify_tls, ca_bundle, backup_prefix, "
    "activity_log_prefix, retention_count, created_at, updated_at) VALUES ('b', "
    "'http://127.0.0.1:29000', 'us-east-1', 'b', 'k', 'x', 1, 1, NULL, 'backups', 'logs', NULL, "
    "'x', 'x')"
)
INSERT_EVENTS = (
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < ?) "
    "INSERT INTO activity_event (category, actor_type, target_type, action, result, occurred_at) "
    "SELECT 'authentication', 'anonymous', 'user', 'login_failed', 'failure', "
    "'2026-01-01T00:00:00Z' FROM n"
)
ARGUMENTS: dict[str, Any] = {
    "subsystem": EXPORTER,
    "environment_id": None,
    "target_selector": "",
    "parameters": {"object_storage_target_id": 1},
    "schedule_kind": "interval",
    "schedule_expression": "3600",
}

type Access = Callable[[JobServices, int], Awaitable[object]]


@pytest.fixture
def app(settings: Settings, attach_app: AttachApp) -> FastAPI:
    return attach_app(settings, JOB_SUBSYSTEMS)


async def get_rule(services: JobServices, rule_id: int) -> object:
    return await services.rules.get(STANDARD, rule_id)


async def detail_of(services: JobServices, rule_id: int) -> object:
    return await services.rules.detail(STANDARD, rule_id)


async def preview_delete(services: JobServices, rule_id: int) -> object:
    return await services.rules.delete_preview(STANDARD, rule_id)


async def delete(services: JobServices, rule_id: int) -> None:
    await services.rules.delete(STANDARD, rule_id, expected_name=RULE_NAME)


async def runs_of(services: JobServices, rule_id: int) -> object:
    return await services.rules.runs(STANDARD, rule_id, limit=5)


PATHS: list[Access] = [get_rule, detail_of, preview_delete, delete, runs_of]


async def refusals(services: JobServices, rule_id: int) -> list[type[BaseException]]:
    """Run every path as the demoted owner and name the error each one ends in."""
    outcomes: list[type[BaseException]] = []
    for access in PATHS:
        try:
            await access(services, rule_id)
        except ResourceNotFound as refused:
            outcomes.append(type(refused))
    return outcomes


async def exporter_rule(app: FastAPI, services: JobServices) -> int:
    async with app.state.database.session() as db:
        await db.execute(INSERT_TARGET)
        await db.execute(INSERT_EVENTS, (EVENTS,))
    rule = await services.rules.create(ADMIN, name=RULE_NAME, **ARGUMENTS)
    return rule.id


async def test_a_standard_user_may_not_create_an_exporter_rule(started_app: FastAPI) -> None:
    async with job_services(started_app) as services:
        with pytest.raises(PermissionDenied):
            await services.rules.create(STANDARD, name=RULE_NAME, **ARGUMENTS)


async def test_a_demoted_owner_reaches_an_exporter_rule_on_no_path_and_it_and_its_cursor_stay(
    started_app: FastAPI,
) -> None:
    async with job_services(started_app) as services:
        rule_id = await exporter_rule(started_app, services)
        await services.rules.runs(ADMIN, rule_id, limit=5)
        async with started_app.state.database.session() as db:
            await db.execute(
                "UPDATE automation_rule SET created_by_user_id = ? WHERE id = ?",
                (STANDARD.user_id, rule_id),
            )
            await db.execute(
                "INSERT INTO activity_export_cursor (rule_id, last_event_id) VALUES (?, ?)",
                (rule_id, CURSOR),
            )

        outcomes = await refusals(services, rule_id)
        visible_to_admin = await services.rules.get(ADMIN, rule_id)

    assert outcomes == [ResourceNotFound] * len(PATHS)
    assert visible_to_admin.id == rule_id
    assert await rows(started_app, "SELECT last_event_id FROM activity_export_cursor") == [
        {"last_event_id": CURSOR}
    ]


async def test_an_exporter_rules_delete_preview_counts_the_events_the_deletion_would_release(
    started_app: FastAPI,
) -> None:
    async with job_services(started_app) as services:
        rule_id = await exporter_rule(started_app, services)
        async with started_app.state.database.session() as db:
            await db.execute(
                "INSERT INTO activity_export_cursor (rule_id, last_event_id) VALUES (?, ?)",
                (rule_id, CURSOR),
            )

        preview = await services.rules.delete_preview(ADMIN, rule_id)

    [total] = await rows(started_app, "SELECT count(*) AS n FROM activity_event")
    assert preview.export_release is not None
    assert (preview.export_release.rows, preview.export_release.more_than) == (
        total["n"] - CURSOR,
        False,
    )


async def test_only_an_exporter_rules_delete_preview_carries_the_release_figure(
    started_app: FastAPI,
) -> None:
    async with job_services(started_app) as services:
        rule = await services.rules.create(
            ADMIN,
            name="update",
            subsystem="container-updater",
            environment_id=None,
            target_selector="",
            parameters={},
            schedule_kind="interval",
            schedule_expression="3600",
        )

        preview = await services.rules.delete_preview(ADMIN, rule.id)

    assert preview.export_release is None


LOG_EXPORTER = "container-log-exporter"
INSERT_REMOTE = (
    "INSERT INTO environment (name, type, connection_kind, url, created_at, updated_at) "
    "VALUES ('remote', 'standalone', 'tcp_tls', 'tcp://remote:2376', 'x', 'x')"
)
INSERT_MANAGER = INSERT_REMOTE.replace("'standalone'", "'swarm'").replace("remote", "manager")
SERVICE_SELECTOR = '{"kind":"service","names":["web"]}'
LOG_ARGUMENTS: dict[str, Any] = {
    "subsystem": LOG_EXPORTER,
    "target_selector": '{"kind":"environment"}',
    "parameters": {"object_storage_target_id": 1},
    "schedule_kind": "interval",
    "schedule_expression": "3600",
}


async def granted_remote_environment(app: FastAPI, insert: str = INSERT_REMOTE) -> int:
    """Insert a target and a remote environment the standard user may create objects in."""
    async with app.state.database.session() as db:
        await db.execute(INSERT_TARGET)
        environment_id = int((await db.execute(insert)).last_row_id)
        await EnvironmentAccessRepository(db).add_user(
            "environment", environment_id, STANDARD.user_id
        )
    return environment_id


async def test_a_standard_user_with_environment_access_may_not_create_a_log_exporter_rule(
    started_app: FastAPI,
) -> None:
    async with job_services(started_app) as services:
        environment_id = await granted_remote_environment(started_app)

        with pytest.raises(PermissionDenied):
            await services.rules.create(
                STANDARD, name=RULE_NAME, environment_id=environment_id, **LOG_ARGUMENTS
            )


async def test_an_administrator_may_create_a_container_log_exporter_rule(
    started_app: FastAPI,
) -> None:
    async with job_services(started_app) as services:
        environment_id = await granted_remote_environment(started_app)

        rule = await services.rules.create(
            ADMIN, name=RULE_NAME, environment_id=environment_id, **LOG_ARGUMENTS
        )

    assert (rule.subsystem, rule.environment_id) == (LOG_EXPORTER, environment_id)


async def test_a_standard_user_with_environment_access_may_not_create_a_service_log_rule(
    started_app: FastAPI,
) -> None:
    async with job_services(started_app) as services:
        environment_id = await granted_remote_environment(started_app, INSERT_MANAGER)

        with pytest.raises(PermissionDenied):
            await services.rules.create(
                STANDARD,
                name=RULE_NAME,
                environment_id=environment_id,
                **{**LOG_ARGUMENTS, "target_selector": SERVICE_SELECTOR},
            )


async def test_a_service_target_on_an_environment_that_is_not_a_swarm_manager_is_refused_at_save(
    started_app: FastAPI,
) -> None:
    async with job_services(started_app) as services:
        environment_id = await granted_remote_environment(started_app)

        with pytest.raises(ValidationFailed) as refused:
            await services.rules.create(
                ADMIN,
                name=RULE_NAME,
                environment_id=environment_id,
                **{**LOG_ARGUMENTS, "target_selector": SERVICE_SELECTOR},
            )

    assert "Swarm manager" in str(refused.value.fields["target_selector"])


async def test_an_administrator_may_create_a_service_log_rule_on_a_swarm_manager(
    started_app: FastAPI,
) -> None:
    async with job_services(started_app) as services:
        environment_id = await granted_remote_environment(started_app, INSERT_MANAGER)

        rule = await services.rules.create(
            ADMIN,
            name=RULE_NAME,
            environment_id=environment_id,
            **{**LOG_ARGUMENTS, "target_selector": SERVICE_SELECTOR},
        )

    assert (rule.subsystem, rule.environment_id) == (LOG_EXPORTER, environment_id)
