"""The template catalogue pages: who may reach them, the actions, and what each fetch leaves."""

import json
import re
import ssl
from collections.abc import Awaitable, Callable
from html import unescape
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from httpx import AsyncClient, Response

from app.auth.dependencies import get_activity, get_runtime
from app.auth.services import AuthRuntime
from app.db.engine import Session, get_session
from app.services import assembly
from app.services import template_fetch as fetch_module
from app.services.activity import ActivityRecorder
from app.services.template_catalogues import LINUXSERVER_PRESET
from app.services.template_fetch import FAILURE_TEXT, CatalogueFetcher
from tests.auth_support import csrf_form
from tests.job_support import rows
from tests.notification_fixtures import certificates, receivers, trusting  # noqa: F401
from tests.notification_support import HttpReceiver
from tests.registry_client.loopback import Certificates
from tests.template_support import container, portainer_document
from tests.web.flash_support import flash_texts, land, location
from tests.web.helpers import field_error, form_values

type StartReceiver = Callable[..., Awaitable[HttpReceiver]]


@pytest.fixture
def env(env: dict[str, str]) -> dict[str, str]:
    return env | {"DOCKMIN_PERMITTED_INTERNAL_NETWORKS": "127.0.0.0/8,::1/128"}


OK = 200
SEE_OTHER = 303
FORBIDDEN = 403
NOT_FOUND = 404
BAD_REQUEST = 400
UNPROCESSABLE = 422
HTMX = {"HX-Request": "true"}
DETAIL = "/template-catalogues/1"
REMOTE_TEXT = "remote-body-sentinel-text"

STANDARD_GETS = [
    "/template-catalogues",
    "/template-catalogues/new",
    "/template-catalogues/1",
    "/template-catalogues/1/edit",
    "/template-catalogues/1/delete",
]
POSTS = [
    "/template-catalogues",
    "/template-catalogues/1",
    "/template-catalogues/1/enable",
    "/template-catalogues/1/refresh",
    "/template-catalogues/1/disable",
    "/template-catalogues/1/delete",
]


def text_of(response: Response | str) -> str:
    html = response if isinstance(response, str) else response.text
    return unescape(re.sub(r"<[^>]+>", " ", html))


class Tripwire:
    """Counts every attempt to open an outbound transport for a catalogue."""

    def __init__(self) -> None:
        self.attempts = 0

    def __call__(self, **_: Any) -> Any:
        self.attempts += 1
        raise AssertionError("a catalogue was contacted")


@pytest.fixture
def tripwire(monkeypatch: pytest.MonkeyPatch) -> Tripwire:
    """Make any outbound catalogue connection fail the test and be counted."""
    wire = Tripwire()
    monkeypatch.setattr(fetch_module, "policy_transport", wire)
    return wire


@pytest.fixture
def fetching(started_app: FastAPI, trusting: ssl.SSLContext) -> None:  # noqa: F811
    """Give the catalogue service a fetcher that trusts the throwaway authority."""

    async def provide(
        db: Annotated[Session, Depends(get_session)],
        runtime: Annotated[AuthRuntime, Depends(get_runtime)],
        activity: Annotated[ActivityRecorder, Depends(get_activity)],
    ) -> Any:
        return assembly.build_template_catalogue_service(
            db, runtime, activity, fetcher=CatalogueFetcher(trust=trusting)
        )

    started_app.dependency_overrides[assembly.get_template_catalogue_service] = provide


async def add(client: AsyncClient, url: str, **changes: str) -> Response:
    data = {"name": "mine", "kind": "portainer", "url": url} | changes
    response = await client.post("/template-catalogues", data=csrf_form(client, **data))
    assert response.status_code == SEE_OTHER, text_of(response)[:300]
    return response


async def act(client: AsyncClient, action: str) -> str:
    response = await client.post(f"{DETAIL}/{action}", data=csrf_form(client))
    assert response.status_code == SEE_OTHER
    return text_of(await land(client, response))


async def serve(start: StartReceiver, authority: Certificates, **options: Any) -> HttpReceiver:
    return await start(identity=authority.server, **options)


def two_templates() -> bytes:
    return portainer_document(container("One", id=1), container("Two", id=2), {"type": 2})


async def test_a_standard_user_is_refused_every_page_and_action_and_nothing_is_contacted(
    standard_client: AsyncClient, tripwire: Tripwire
) -> None:
    gets = {path: (await standard_client.get(path)).status_code for path in STANDARD_GETS}
    posts = {
        path: (await standard_client.post(path, data=csrf_form(standard_client))).status_code
        for path in POSTS
    }

    assert set(gets.values()) == {FORBIDDEN} and set(posts.values()) == {FORBIDDEN}
    assert tripwire.attempts == 0


async def test_an_administrator_without_a_token_and_an_anonymous_visitor_are_turned_away(
    admin_client: AsyncClient, anonymous_client: AsyncClient
) -> None:
    posts = {path: (await admin_client.post(path, data={})).status_code for path in POSTS}
    visitor = await anonymous_client.get("/template-catalogues")

    assert set(posts.values()) == {FORBIDDEN}
    assert visitor.status_code in (302, 303, 307) and "/login" in visitor.headers["location"]


async def test_no_page_contacts_a_catalogue_unless_one_is_enabled(
    tripwire: Tripwire, admin_client: AsyncClient, standard_client: AsyncClient
) -> None:
    await add(admin_client, "https://catalogue.example.test/templates.json")
    paths = [*STANDARD_GETS, "/", "/jobs", "/activity"]

    statuses = {path: (await admin_client.get(path)).status_code for path in paths}
    await standard_client.get("/")

    assert set(statuses.values()) == {OK}
    assert tripwire.attempts == 0


async def test_the_list_offers_the_preset_and_navigation_follows_the_role(
    admin_client: AsyncClient, standard_client: AsyncClient
) -> None:
    listing = await admin_client.get("/template-catalogues")
    form = await admin_client.get("/template-catalogues/new")
    preset = await admin_client.get(f"/template-catalogues/new?preset={LINUXSERVER_PRESET.key}")
    home = await standard_client.get("/")

    assert listing.status_code == OK and "No template catalogues" in listing.text
    assert 'href="/template-catalogues/new"' in listing.text
    assert listing.headers["cache-control"] == "no-store"
    assert form_values(form.text)["url"] == "" and form_values(form.text)["kind"] == "linuxserver"
    assert form_values(preset.text)["url"] == LINUXSERVER_PRESET.url
    assert form_values(preset.text)["name"] == LINUXSERVER_PRESET.name
    assert 'href="/template-catalogues"' in listing.text
    assert 'href="/template-catalogues"' not in home.text


async def test_a_form_that_breaks_a_rule_is_refused_with_the_message_beside_its_field(
    admin_client: AsyncClient,
) -> None:
    refusals = {
        "http": ({"url": "http://catalogue.example.test/x"}, "field-url", "https"),
        "userinfo": ({"url": "https://u:p@catalogue.example.test/x"}, "field-url", "user name"),
        "name": ({"name": ""}, "field-name", "Enter a name"),
    }

    results = {
        label: await admin_client.post(
            "/template-catalogues",
            data=csrf_form(
                admin_client,
                **(
                    {"name": "mine", "kind": "portainer", "url": "https://c.example.test/"}
                    | changes
                ),
            ),
        )
        for label, (changes, _, _) in refusals.items()
    }

    assert {label: r.status_code for label, r in results.items()} == dict.fromkeys(
        refusals, UNPROCESSABLE
    )
    for label, (_, field, text) in refusals.items():
        assert text in (field_error(results[label].text, field) or "")


async def test_adding_enabling_refreshing_disabling_and_deleting_follow_the_audited_flow(
    admin_client: AsyncClient,
    fetching: None,
    started_app: FastAPI,
    receivers: StartReceiver,  # noqa: F811
    certificates: Certificates,  # noqa: F811
) -> None:
    receiver = await serve(receivers, certificates, status=200, reply_body=two_templates())
    created = await add(admin_client, receiver.url("/templates.json"))

    detail = await land(admin_client, created)
    assert location(created) == DETAIL
    assert "Nothing is contacted until you enable it" in flash_texts(detail.text)[0]
    assert "Enable and fetch" in detail.text and receiver.requests == []

    enabled = await act(admin_client, "enable")
    assert "The catalogue is enabled. 2 templates are available." in enabled
    assert "1 entries were skipped (unsupported stack template: 1)" in enabled
    assert len(receiver.requests) == 1

    refreshed = await act(admin_client, "refresh")
    assert "has not changed since the last fetch" in refreshed
    assert len(receiver.requests) == 2

    listing = await admin_client.get("/template-catalogues")
    fragment = await admin_client.get("/template-catalogues", headers=HTMX)
    assert "Enabled" in listing.text and 'href="/template-catalogues/1"' in listing.text
    assert "<html" not in fragment.text and 'id="catalogues-table"' in fragment.text

    assert "Its templates are no longer offered" in await act(admin_client, "disable")
    assert "Enable and fetch" in (await admin_client.get(DETAIL)).text

    confirm = await admin_client.get(f"{DETAIL}/delete")
    assert "mine" in text_of(confirm) and "template catalogue" in text_of(confirm)
    deleted = await admin_client.post(
        f"{DETAIL}/delete", data=csrf_form(admin_client, expected_name="mine")
    )
    assert deleted.status_code == SEE_OTHER and location(deleted) == "/template-catalogues"
    assert (await admin_client.get(DETAIL)).status_code == NOT_FOUND

    events = await rows(
        started_app, "SELECT action, result FROM activity_event WHERE action LIKE 'template_cat%'"
    )
    assert [(e["action"], e["result"]) for e in events] == [
        ("template_catalogue_created", "success"),
        ("template_catalogue_refreshed", "success"),
        ("template_catalogue_refreshed", "success"),
        ("template_catalogue_updated", "success"),
        ("template_catalogue_deleted", "success"),
    ]


async def test_a_failed_enable_shows_the_product_text_never_the_remote_body(
    admin_client: AsyncClient,
    fetching: None,
    started_app: FastAPI,
    receivers: StartReceiver,  # noqa: F811
    certificates: Certificates,  # noqa: F811
) -> None:
    receiver = await serve(receivers, certificates, status=500, reply_body=REMOTE_TEXT.encode())
    await add(admin_client, receiver.url("/templates.json"))

    after = await act(admin_client, "enable")
    detail = await admin_client.get(DETAIL)

    assert FAILURE_TEXT["status"] in after
    assert "The last fetch failed" in detail.text and FAILURE_TEXT["status"] in text_of(detail)
    assert "Enable and fetch" in detail.text
    events = await rows(
        started_app, "SELECT result, detail FROM activity_event WHERE action LIKE '%refreshed'"
    )
    assert [e["result"] for e in events] == ["failure"]
    assert REMOTE_TEXT not in json.dumps(events) and REMOTE_TEXT not in after + detail.text


async def test_the_edit_form_carries_the_stamp_and_a_new_address_warns_and_disables(
    admin_client: AsyncClient,
    fetching: None,
    receivers: StartReceiver,  # noqa: F811
    certificates: Certificates,  # noqa: F811
) -> None:
    receiver = await serve(receivers, certificates, status=200, reply_body=two_templates())
    await add(admin_client, receiver.url("/templates.json"))
    await act(admin_client, "enable")

    edit = await admin_client.get(f"{DETAIL}/edit")
    values = form_values(edit.text)
    moved = await admin_client.post(
        DETAIL,
        data=csrf_form(
            admin_client,
            name="mine",
            kind="portainer",
            url="https://other.example.test/t.json",
            expected_updated_at=values["expected_updated_at"],
        ),
    )

    after = await land(admin_client, moved)
    assert values["name"] == "mine" and values["url"] == receiver.url("/templates.json")
    assert "discards the fetched templates" in edit.text
    assert moved.status_code == SEE_OTHER and "discarded and the catalogue is disabled" in "".join(
        flash_texts(after.text)
    )
    assert (
        "Enable and fetch" in after.text and "Enabled" not in text_of(after).split("State")[1][:40]
    )


async def test_a_stale_edit_form_is_refused_and_shows_the_stored_values(
    admin_client: AsyncClient,
) -> None:
    await add(admin_client, "https://catalogue.example.test/templates.json")

    stale = await admin_client.post(
        DETAIL,
        data=csrf_form(
            admin_client,
            name="renamed",
            kind="portainer",
            url="https://catalogue.example.test/templates.json",
            expected_updated_at="2001-01-01T00:00:00Z",
        ),
    )

    assert stale.status_code == UNPROCESSABLE
    assert "Changed since it was reviewed" in text_of(stale)
    assert form_values(stale.text)["name"] == "mine"


async def test_deleting_with_a_changed_name_is_refused_and_the_page_names_the_catalogue(
    admin_client: AsyncClient,
) -> None:
    await add(admin_client, "https://catalogue.example.test/templates.json")

    refused = await admin_client.post(
        f"{DETAIL}/delete", data=csrf_form(admin_client, expected_name="other")
    )

    assert refused.status_code == UNPROCESSABLE and "was not deleted" in text_of(refused)
    assert (await admin_client.get(DETAIL)).status_code == OK
