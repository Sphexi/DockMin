"""A new instance: the default entries, their grant to the first environment, and no contact."""

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any, NoReturn

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.db.engine import connect
from app.db.migrations import migrate
from app.registry_client.manifests import ManifestClient
from app.registry_client.probe import RegistryProbeCoordinator
from app.registry_client.tags import TagClient
from tests.auth_support import ADMIN_PASSWORD, ADMIN_USERNAME, Browsers, csrf_form
from tests.conftest import serving_app
from tests.environment_support import RecordingPoker, ScriptedProbes
from tests.job_support import rows
from tests.web.environment_support import local_form

SEE_OTHER = 303
OK = 200
SETUP = "/setup"
DEFAULT_ADDRESSES = ["docker.io", "ghcr.io"]


@pytest.fixture(autouse=True)
def registry_contacts(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Fail the test, and record, on any manifest, tag-list or connection-test request."""
    contacts: list[str] = []

    def refuse(name: str) -> Any:
        async def tripwire(*args: Any, **kwargs: Any) -> NoReturn:
            contacts.append(name)
            raise AssertionError(f"a registry was contacted by {name}")

        return tripwire

    monkeypatch.setattr(ManifestClient, "head_digest", refuse("head_digest"))
    monkeypatch.setattr(TagClient, "list_tags", refuse("list_tags"))
    monkeypatch.setattr(RegistryProbeCoordinator, "probe", refuse("probe"))
    yield contacts


@pytest.fixture
async def new_instance(
    app: FastAPI, settings: Any, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[FastAPI]:
    """Start the application over a database created by the migrations alone."""
    path = tmp_path_factory.mktemp("new-instance") / "dockmin.db"
    connection = connect(path)
    try:
        migrate(connection)
    finally:
        connection.close()
    async with serving_app(app, settings, path):
        yield app


@pytest.fixture
async def new_browsers(new_instance: FastAPI) -> AsyncIterator[Browsers]:
    opened = Browsers(new_instance)
    yield opened
    await opened.close()


async def sign_in(browsers: Browsers, application: FastAPI) -> AsyncClient:
    token = application.state.auth.setup.token
    assert token is not None
    setup = browsers.open()
    created = await setup.post(
        SETUP,
        data=csrf_form(
            setup,
            setup_token=str(token),
            username=ADMIN_USERNAME,
            password=ADMIN_PASSWORD,
            password_confirm=ADMIN_PASSWORD,
        ),
    )
    assert created.status_code == SEE_OTHER
    return await browsers.sign_in()


async def test_a_new_instance_holds_the_defaults_and_the_first_environment_is_granted_them(
    new_instance: FastAPI,
    new_browsers: Browsers,
    registry_contacts: list[str],
) -> None:
    entries = await rows(
        new_instance,
        "SELECT id, url, authentication_enabled, seeded_default FROM registry_credential "
        "ORDER BY id",
    )
    assert [(e["url"], e["authentication_enabled"], e["seeded_default"]) for e in entries] == [
        (address, 0, 1) for address in DEFAULT_ADDRESSES
    ]
    engine = new_instance.state.engine
    engine.probes, engine.refresher = ScriptedProbes(), RecordingPoker()
    client = await sign_in(new_browsers, new_instance)

    created = await client.post("/environments", data=local_form(client, name="local"))

    assert created.status_code == SEE_OTHER
    granted = await rows(
        new_instance,
        "SELECT registry_credential_id FROM registry_environment_access ORDER BY 1",
    )
    assert [g["registry_credential_id"] for g in granted] == [e["id"] for e in entries]
    [event] = await rows(
        new_instance,
        "SELECT detail FROM activity_event WHERE action = 'environment_created'",
    )
    assert json.loads(event["detail"])["registry_grants"] == ",".join(str(e["id"]) for e in entries)
    pages = [
        "/",
        "/registries",
        f"/registries/{entries[0]['id']}",
        created.headers["location"],
    ]
    statuses = [(await client.get(path)).status_code for path in pages]
    assert statuses == [OK] * len(pages)
    assert registry_contacts == []
