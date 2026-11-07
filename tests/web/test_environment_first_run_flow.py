"""Adding the Docker socket from the dashboard panel: the create path it posts to, and its end."""

from collections.abc import Iterator
from dataclasses import dataclass
from html import unescape
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

import app.web.environment_routes as environment_routes
from app.docker_engine.probe import Stage
from app.docker_engine.transport import UnixSocketSettings
from tests.auth_support import ADMIN_ID, csrf_form
from tests.environment_support import (
    RecordingPoker,
    ScriptedProbes,
    failed_result,
    seed_environment,
)
from tests.job_support import rows
from tests.socket_support import socket_in_state
from tests.web.flash_support import location
from tests.web.helpers import hidden_inputs

SEE_OTHER = 303
UNPROCESSABLE = 422
PANEL = 'id="first-run"'
SOCKET_REFUSED = "Nothing answered on the socket."
SAVE_ANYWAY = "Save even though the connection failed"
OTHER_ENVIRONMENT = "already-there"


@dataclass(slots=True)
class Scripts:
    probes: ScriptedProbes
    poker: RecordingPoker


@pytest.fixture
def scripts(started_app: FastAPI) -> Iterator[Scripts]:
    engine = started_app.state.engine
    real = (engine.probes, engine.refresher)
    scripted = Scripts(ScriptedProbes(), RecordingPoker())
    engine.probes, engine.refresher = scripted.probes, scripted.poker
    yield scripted
    engine.probes, engine.refresher = real


@pytest.fixture
def socket_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    with socket_in_state(tmp_path, "present") as path:
        monkeypatch.setattr(environment_routes, "DEFAULT_SOCKET_PATH", path)
        yield path


async def submit_the_panel(client: AsyncClient) -> Response:
    """Post the panel's form as a browser would: its hidden fields plus the name it shows."""
    page = await client.get("/")
    fields = hidden_inputs(page.text)
    assert PANEL in page.text and 'action="/environments"' in page.text
    return await client.post("/environments", data=csrf_form(client, **fields, name="local"))


async def test_test_and_add_creates_the_environment_through_the_create_path_and_lands_on_it(
    started_app: FastAPI,
    admin_client: AsyncClient,
    scripts: Scripts,
    socket_path: str,
) -> None:
    created = await submit_the_panel(admin_client)

    stored = await rows(
        started_app, "SELECT id, name, connection_kind, socket_path FROM environment"
    )
    events = await rows(
        started_app,
        "SELECT actor_user_id, result, target_id FROM activity_event "
        "WHERE action = 'environment_created'",
    )
    [environment] = stored
    assert created.status_code == SEE_OTHER
    assert location(created) == f"/environments/{environment['id']}"
    assert (environment["name"], environment["connection_kind"], environment["socket_path"]) == (
        "local",
        "unix_socket",
        socket_path,
    )
    assert [(e["actor_user_id"], e["result"], e["target_id"]) for e in events] == [
        (ADMIN_ID, "success", str(environment["id"]))
    ]
    [call] = scripts.probes.calls
    assert call.settings == UnixSocketSettings(socket_path)
    assert scripts.poker.poked == [environment["id"]]


async def test_a_failing_connection_test_shows_the_existing_form_with_its_report_and_saves_nothing(
    started_app: FastAPI,
    admin_client: AsyncClient,
    scripts: Scripts,
    socket_path: str,
) -> None:
    scripts.probes.result = failed_result(Stage.SOCKET, SOCKET_REFUSED)

    refused = await submit_the_panel(admin_client)

    text = unescape(refused.text)
    assert refused.status_code == UNPROCESSABLE
    assert SOCKET_REFUSED in text and SAVE_ANYWAY in text
    assert await rows(started_app, "SELECT id FROM environment") == []


async def test_the_panel_is_gone_once_any_environment_exists_even_under_a_filter_that_matches_none(
    started_app: FastAPI, admin_client: AsyncClient, socket_path: str
) -> None:
    async with started_app.state.database.session() as db:
        await seed_environment(db, started_app.state.auth.clock, OTHER_ENVIRONMENT)

    unfiltered = await admin_client.get("/")
    filtered = await admin_client.get("/?q=matches-nothing")

    assert OTHER_ENVIRONMENT in unfiltered.text
    assert PANEL not in unfiltered.text and PANEL not in filtered.text
    assert "Test and add" not in unfiltered.text + filtered.text
