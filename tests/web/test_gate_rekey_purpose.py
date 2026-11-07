"""The offline gate while a re-encryption holds it: who is admitted and what the rest read."""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.services.restore_gate import OFFLINE_MESSAGE, REKEY_MESSAGE, RestoreGate, admits
from tests.auth_support import ADMIN_PASSWORD, ADMIN_USERNAME, Browsers, csrf_form

NOW = datetime(2026, 1, 1, tzinfo=UTC)
OWNER_ID = "r" * 32
SERVICE_UNAVAILABLE = 503
TOO_MANY = 429
TOKEN = "C" * 43
REKEY_HEADING = "Offline while stored secrets are re-encrypted"
HTMX = {"HX-Request": "true"}

REKEY_ADMISSION = [
    ("GET", "/x", True),
    ("HEAD", "/x", True),
    ("OPTIONS", "/x", True),
    ("GET", "/restore", True),
    ("GET", "/restore/review", True),
    ("GET", "/server/encryption/runs/abc", True),
    ("POST", "/login", True),
    ("POST", "/logout", True),
    ("POST", "/x", False),
    ("POST", "/login/x", False),
    ("POST", "/restore", False),
    ("POST", "/restore/begin", False),
    ("POST", "/restore/archive", False),
    ("POST", "/restore/confirm", False),
    ("POST", "/restore/cancel", False),
    ("POST", "/server/encryption/rekey", False),
    ("PUT", "/api/x", False),
    ("PATCH", "/x", False),
    ("DELETE", "/x", False),
]
RESTORE_ADMISSION = [
    ("GET", "/x", True),
    ("POST", "/login", True),
    ("POST", "/logout", True),
    ("POST", "/restore", True),
    ("POST", "/restore/begin", True),
    ("POST", "/restore/archive", True),
    ("POST", "/restorex", False),
    ("POST", "/x", False),
    ("PUT", "/api/x", False),
    ("DELETE", "/x", False),
]


@pytest.mark.parametrize(("method", "path", "admitted"), REKEY_ADMISSION)
def test_a_gate_engaged_for_a_rekey_admits_reads_and_sign_in_and_refuses_every_other_change(
    method: str, path: str, admitted: bool
) -> None:
    assert admits("engaged", method, path, purpose="rekey") is admitted


@pytest.mark.parametrize(("method", "path", "admitted"), RESTORE_ADMISSION)
def test_a_gate_engaged_for_a_restore_still_admits_the_restore_paths(
    method: str, path: str, admitted: bool
) -> None:
    assert admits("engaged", method, path, purpose="restore") is admitted


def test_a_gate_engaged_for_a_rekey_admits_no_websocket() -> None:
    assert admits("engaged", "GET", "/ws", websocket=True, purpose="rekey") is False


def test_an_open_gate_admits_every_request_whatever_purpose_it_last_held() -> None:
    assert admits("open", "POST", "/restore/begin", purpose="rekey") is True


@pytest.mark.parametrize(
    ("purpose", "message"), [("rekey", REKEY_MESSAGE), ("restore", OFFLINE_MESSAGE)]
)
def test_the_banner_message_follows_the_purpose_that_engaged_the_gate(
    purpose: str, message: str
) -> None:
    gate = RestoreGate()
    gate.engage(OWNER_ID, NOW, purpose=purpose)  # type: ignore[arg-type]

    assert (gate.purpose, gate.banner_message) == (purpose, message)


def test_reopening_the_gate_clears_its_purpose_and_its_message() -> None:
    gate = RestoreGate()
    gate.engage(OWNER_ID, NOW, purpose="rekey")

    gate.reopen()

    assert (gate.purpose, gate.banner_message, gate.phase) == (None, None, "open")


@pytest.fixture
def rekeying(started_app: FastAPI) -> FastAPI:
    started_app.state.restore_gate.engage(OWNER_ID, NOW, purpose="rekey")
    return started_app


async def test_every_refused_change_carries_the_rekey_message_in_the_shape_the_caller_asked_for(
    rekeying: FastAPI, admin_client: AsyncClient
) -> None:
    api = await admin_client.post("/api/anything")
    htmx = await admin_client.post("/groups", headers=HTMX | {"X-CSRF-Token": TOKEN})
    page = await admin_client.post("/groups", data={"csrf_token": TOKEN})

    assert api.status_code == htmx.status_code == page.status_code == SERVICE_UNAVAILABLE
    assert api.json()["error"]["message"] == REKEY_MESSAGE
    assert "<html" not in htmx.text and REKEY_MESSAGE in htmx.text and REKEY_HEADING in htmx.text
    assert REKEY_MESSAGE in page.text and REKEY_HEADING in page.text
    assert 'href="/restore"' not in page.text


async def test_a_restore_cannot_begin_and_a_backup_cannot_be_downloaded_during_a_rekey(
    rekeying: FastAPI, admin_client: AsyncClient
) -> None:
    begin = await admin_client.post("/restore/begin", data=csrf_form(admin_client))
    download = await admin_client.post("/backups/download", data=csrf_form(admin_client))

    assert begin.status_code == download.status_code == SERVICE_UNAVAILABLE
    assert REKEY_MESSAGE in begin.text and REKEY_MESSAGE in download.text


async def test_during_a_rekey_pages_are_read_and_the_sign_in_form_still_works(
    rekeying: FastAPI, browsers: Browsers
) -> None:
    client = browsers.open()

    health = await client.get("/api/health")
    login = await client.get("/login")
    attempt = await client.post(
        "/login", data=csrf_form(client, username=ADMIN_USERNAME, password=ADMIN_PASSWORD)
    )

    assert (health.status_code, login.status_code, attempt.status_code) == (200, 200, 303)


@pytest.mark.parametrize(
    ("purpose", "message", "links_to_the_restore"),
    [("rekey", REKEY_MESSAGE, False), ("restore", OFFLINE_MESSAGE, True)],
)
async def test_every_page_carries_the_banner_of_the_purpose_and_links_the_restore_only_for_one(
    started_app: FastAPI,
    admin_client: AsyncClient,
    purpose: str,
    message: str,
    links_to_the_restore: bool,
) -> None:
    started_app.state.restore_gate.engage(OWNER_ID, NOW, purpose=purpose)

    page = await admin_client.get("/")

    assert message in page.text
    assert ('href="/restore"' in page.text) is links_to_the_restore


async def test_a_production_held_for_a_rekey_refuses_a_download_with_the_rekey_text(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    assert started_app.state.backup_production.try_hold(for_rekey=True)

    response = await admin_client.post("/backups/download", data=csrf_form(admin_client))

    assert response.status_code == TOO_MANY
    assert "Stored secrets are being re-encrypted" in response.text
