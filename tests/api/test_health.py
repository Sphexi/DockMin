"""Tests for app/api/health.py."""

import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.health import database_check
from app.db.engine import Database

INTERNAL_DETAIL = "db-connection-refused-at-10.0.0.7"
NOT_A_DATABASE = b"this is not a database file" * 8


async def _broken_check() -> bool:
    raise RuntimeError(INTERNAL_DETAIL)


async def test_health_returns_200_with_status_only(client: AsyncClient) -> None:
    response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.fixture
async def started_client(client: AsyncClient, app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Return the client with the lifespan running, so startup registers the database check."""
    async with app.router.lifespan_context(app):
        yield client


async def test_ready_reports_every_check_up(started_client: AsyncClient) -> None:
    response = await started_client.get("/api/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"data_dir": "up", "database": "up"},
    }


async def test_ready_returns_503_and_marks_database_down_without_its_error_when_unreadable(
    started_client: AsyncClient, app: FastAPI
) -> None:
    app.state.database.path.write_bytes(NOT_A_DATABASE)

    response = await started_client.get("/api/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"data_dir": "up", "database": "down"},
    }
    assert "not a database" not in response.text


async def test_database_check_is_true_for_a_migrated_database(database: Database) -> None:
    assert await database_check(database)() is True


async def test_database_check_is_false_for_a_database_with_no_schema(tmp_path: Path) -> None:
    empty = Database(tmp_path / "empty.db", create=True)

    assert await database_check(empty)() is False


@pytest.fixture
def data_dir_removed(data_dir: Path) -> None:
    data_dir.rmdir()


@pytest.mark.usefixtures("data_dir_removed")
async def test_ready_returns_503_and_marks_data_dir_down_when_it_is_missing(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"data_dir": "down"}}


async def test_ready_reports_a_raising_check_as_down_without_its_error(
    client: AsyncClient, app: FastAPI
) -> None:
    app.state.readiness_checks["broken"] = _broken_check

    response = await client.get("/api/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {"data_dir": "up", "broken": "down"}
    assert INTERNAL_DETAIL not in response.text


@pytest.mark.parametrize("path", ["/api/health", "/api/ready"])
async def test_probe_payload_discloses_no_version_hostname_or_path(
    client: AsyncClient, app: FastAPI, data_dir: Path, path: str
) -> None:
    response = await client.get(path)

    body = response.text.lower()
    assert "version" not in body
    assert app.version not in body
    assert socket.gethostname().lower() not in body
    assert str(data_dir).lower() not in body
