"""The dashboard panel that offers the Docker socket while no environment exists."""

from html import unescape
from pathlib import Path

import pytest
from httpx import AsyncClient

import app.web.environment_routes as environment_routes
from tests.auth_support import ADMIN_SESSION_TOKEN, STANDARD_SESSION_TOKEN, Browsers
from tests.socket_support import socket_in_state
from tests.web.helpers import hidden_inputs

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.xdist_group("test_environment_first_run"),
    pytest.mark.usefixtures("module_app_guard"),
]

PANEL = 'id="first-run"'
CREATE_ACTION = 'action="/environments"'
ADD_ANOTHER = 'href="/environments/new"'
STANDARD_EMPTY = "No environment has been shared with you yet."
DOCUMENT = 'documentation page <span class="mono">Environments</span>'


@pytest.fixture
def admin_client(module_browsers: Browsers) -> AsyncClient:
    return module_browsers.open(session_token=ADMIN_SESSION_TOKEN)


@pytest.fixture
def standard_client(module_browsers: Browsers) -> AsyncClient:
    return module_browsers.open(session_token=STANDARD_SESSION_TOKEN)


async def dashboard(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    state: str,
    query: str = "",
) -> tuple[str, str]:
    """Return the dashboard text with the socket in the given state, and the socket's path."""
    with socket_in_state(directory, state) as path:
        monkeypatch.setattr(environment_routes, "DEFAULT_SOCKET_PATH", path)
        response = await client.get(f"/{query}")
    assert response.status_code == 200
    return unescape(response.text), path


async def test_a_present_socket_is_offered_through_the_create_form_with_the_endpoint_reviewed(
    admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text, path = await dashboard(admin_client, monkeypatch, tmp_path, "present")

    fields = hidden_inputs(text)
    assert PANEL in text and CREATE_ACTION in text
    assert "Test and add" in text and "Add your first environment" in text
    assert fields["kind"] == "unix_socket"
    assert fields["socket_path"] == path
    assert fields["reviewed_endpoint"] == f"unix://{path}"
    assert 'name="name"' in text and 'value="local"' in text
    assert f"unix://{path}" in text
    assert ADD_ANOTHER in text


async def test_a_socket_the_process_cannot_open_says_which_group_to_add_and_offers_no_form(
    admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text, path = await dashboard(admin_client, monkeypatch, tmp_path, "no_access")

    assert "dockmin cannot open it" in text
    assert f'--group-add "$(stat -c %g {path})"' in text
    assert DOCUMENT in text and ADD_ANOTHER in text
    assert "Test and add" not in text and CREATE_ACTION not in text


@pytest.mark.parametrize(
    ("state", "headline"),
    [
        ("absent", "The Docker socket is not available to dockmin."),
        ("not_socket", "is not a socket."),
    ],
)
async def test_a_missing_or_wrong_kind_of_file_says_which_mount_to_add_and_offers_no_form(
    admin_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
    headline: str,
) -> None:
    text, path = await dashboard(admin_client, monkeypatch, tmp_path, state)

    assert headline in text
    assert f"-v {path}:{path}" in text
    assert DOCUMENT in text and ADD_ANOTHER in text
    assert "Test and add" not in text and CREATE_ACTION not in text


async def test_an_administrator_with_a_filter_on_an_empty_instance_still_sees_the_panel(
    admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text, _ = await dashboard(admin_client, monkeypatch, tmp_path, "present", "?q=anything")

    assert PANEL in text


async def test_a_standard_user_sees_the_existing_empty_state_and_never_the_panel(
    standard_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text, _ = await dashboard(standard_client, monkeypatch, tmp_path, "present")

    assert STANDARD_EMPTY in text
    assert PANEL not in text and "Test and add" not in text
