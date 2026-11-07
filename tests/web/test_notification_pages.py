"""The notification pages: administrator-only access, forms, secrets, tests and deletion."""

# ruff: noqa: F811

import asyncio
import json
import re
from collections.abc import Callable
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from httpx import AsyncClient, Response

from app.auth.dependencies import get_activity, get_runtime
from app.auth.services import AuthRuntime
from app.db.engine import Session, get_session
from app.db.repositories.notification_channel import NotificationChannelRepository
from app.services import assembly
from app.services.activity import ActivityRecorder, SubsystemActor
from app.services.notifications import NotificationService
from app.services.secret_mask import MASK
from tests.auth_support import csrf_form
from tests.job_support import rows
from tests.notification_fixtures import StartReceiver, receivers  # noqa: F401
from tests.web.flash_support import land, location
from tests.web.helpers import form_values, parse_tags

HMAC = "hmac-sentinel-aaaa"
HEADER = "header-sentinel-bbbb"
TOKEN = "token-sentinel-cccc"
HOOK = "https://hooks.example.test/T000/B000/hook-sentinel-dddd"
PASSWORD = "password-sentinel-eeee"
SECRETS = (HMAC, HEADER, TOKEN, HOOK, PASSWORD)
HOOK_MASK = HOOK[:4] + MASK + HOOK[-4:]
SETTING = "DOCKMIN_PERMITTED_INTERNAL_NETWORKS"
OK = 200
SEE_OTHER = 303
FORBIDDEN = 403
NOT_FOUND = 404
UNPROCESSABLE = 422
HTMX = {"HX-Request": "true"}
WAIT_SECONDS = 20.0
POLL_SECONDS = 0.05

GETS = [
    "/notifications",
    "/notifications/new",
    "/notifications/new?kind=webhook",
    "/notifications/1",
    "/notifications/1/edit",
    "/notifications/1/delete",
    "/notifications/1/subscriptions",
]
POSTS = [
    "/notifications",
    "/notifications/-/settings",
    "/notifications/1",
    "/notifications/1/test",
    "/notifications/1/delete",
    "/notifications/1/subscriptions",
]
FORMS: dict[str, dict[str, str]] = {
    "webhook": {
        "url": "https://hooks.example.test/in",
        "header_name": "X-Key",
        "header_value": HEADER,
        "hmac_key": HMAC,
    },
    "ntfy": {"server_url": "https://ntfy.example.test", "topic": "alerts", "token": TOKEN},
    "gotify": {"server_url": "https://gotify.example.test", "token": TOKEN},
    "discord": {"webhook_url": HOOK},
    "slack": {"webhook_url": HOOK},
    "teams": {"webhook_url": HOOK},
    "email": {
        "host": "mail.example.test",
        "security": "starttls",
        "port": "",
        "sender": "dockmin@example.test",
        "recipients": "a@example.test, b@example.test",
        "username": "mailer",
        "password": PASSWORD,
    },
}


class Receiver(httpx.AsyncBaseTransport):
    """A stand-in for the network that records what the service sends."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 204

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        return httpx.Response(self.status, stream=httpx.ByteStream(b""))


@pytest.fixture
def receiver(started_app: FastAPI) -> Receiver:
    """Make the channel service talk to a recording stand-in instead of the network."""
    fake = Receiver()

    async def provide(
        request: Request,
        db: Annotated[Session, Depends(get_session)],
        runtime: Annotated[AuthRuntime, Depends(get_runtime)],
        activity: Annotated[ActivityRecorder, Depends(get_activity)],
    ) -> NotificationService:
        return NotificationService(
            db, request.app.state.vault, runtime.clock, activity, transport=fake
        )

    started_app.dependency_overrides[assembly.get_notification_service] = provide
    return fake


async def post(client: AsyncClient, path: str, **fields: str) -> Response:
    return await client.post(path, data=csrf_form(client, **fields))


async def create(
    client: AsyncClient, kind: str, name: str | None = None, **fields: str
) -> Response:
    form = FORMS[kind] | fields
    return await post(
        client, "/notifications", kind=kind, name=name or f"{kind} channel", is_enabled="on", **form
    )


def normalised(response: Response) -> str:
    return re.sub(r"[A-Za-z0-9_-]{20,}", "X", response.text)


async def test_a_standard_user_gets_not_found_for_every_notification_route(
    standard_client: AsyncClient,
) -> None:
    gets = {path: await standard_client.get(path) for path in GETS}
    posts = {path: await post(standard_client, path) for path in POSTS}

    answers = [*gets.values(), *posts.values()]
    assert {r.status_code for r in answers} == {NOT_FOUND}
    assert not any("Add channel" in r.text or "Link base URL" in r.text for r in answers)


async def test_a_standard_user_gets_the_same_page_as_for_a_path_that_does_not_exist(
    standard_client: AsyncClient,
) -> None:
    unknown = await standard_client.get("/no-such-page")
    gets = {path: await standard_client.get(path) for path in GETS}
    posts = {path: await post(standard_client, path) for path in POSTS}

    differing = [
        path for path, r in {**gets, **posts}.items() if normalised(r) != normalised(unknown)
    ]
    assert differing == []


async def test_an_administrator_without_a_token_and_an_anonymous_visitor_are_turned_away(
    admin_client: AsyncClient, anonymous_client: AsyncClient
) -> None:
    posts = {path: (await admin_client.post(path, data={})).status_code for path in POSTS}
    visitor = await anonymous_client.get("/notifications")

    assert set(posts.values()) == {FORBIDDEN}
    assert visitor.status_code in (302, 303, 307) and "/login" in visitor.headers["location"]


async def test_the_sidebar_offers_notifications_to_administrators_only(
    admin_client: AsyncClient, standard_client: AsyncClient
) -> None:
    admin_home = await admin_client.get("/")
    standard_home = await standard_client.get("/")

    assert 'href="/notifications"' in admin_home.text
    assert 'href="/notifications"' not in standard_home.text


async def test_an_empty_list_offers_to_add_a_channel_and_the_chooser_lists_every_kind(
    admin_client: AsyncClient,
) -> None:
    listing = await admin_client.get("/notifications")
    chooser = await admin_client.get("/notifications/new")

    assert listing.status_code == OK and "No channels yet" in listing.text
    assert listing.headers["cache-control"] == "no-store"
    for label in ("Webhook", "ntfy", "Gotify", "Discord", "Slack", "Microsoft Teams", "Email"):
        assert label in chooser.text


async def test_every_kinds_form_shows_its_fields_and_keeps_secret_fields_empty(
    admin_client: AsyncClient,
) -> None:
    pages = {
        kind: (await admin_client.get(f"/notifications/new?kind={kind}")).text for kind in FORMS
    }

    for kind, html in pages.items():
        fields = {
            t.attrs.get("name"): t
            for t in parse_tags(html)
            if t.name in ("input", "select", "textarea")
        }
        assert set(FORMS[kind]) <= set(fields), kind
        secret_names = {"header_value", "hmac_key", "token", "webhook_url", "password"} & set(
            FORMS[kind]
        )
        assert all("value" not in fields[name].attrs for name in secret_names), kind
        assert all(fields[name].attrs["type"] == "password" for name in secret_names), kind


async def test_every_kind_can_be_created_and_none_of_its_secrets_is_shown_back_or_stored_plain(
    admin_client: AsyncClient, receiver: Receiver, started_app: FastAPI
) -> None:
    created = {kind: await create(admin_client, kind) for kind in FORMS}

    assert {r.status_code for r in created.values()} == {SEE_OTHER}
    assert [r.headers["location"] for r in created.values()] == [
        f"/notifications/{number}" for number in range(1, len(FORMS) + 1)
    ]
    shown = ""
    for number in range(1, len(FORMS) + 1):
        shown += (await admin_client.get(f"/notifications/{number}")).text
        shown += (await admin_client.get(f"/notifications/{number}/edit")).text
    shown += (await admin_client.get("/notifications")).text
    stored = await rows(
        started_app, "SELECT settings, secrets_ciphertext FROM notification_channel"
    )
    events = await rows(started_app, "SELECT detail FROM activity_event")
    everything = shown + json.dumps([list(r.values()) for r in stored + events])
    assert not any(secret in everything for secret in SECRETS)
    assert "Set. Shown masked only." in shown
    assert "Set. It is never shown." not in shown


async def test_the_channel_page_shows_each_set_secret_as_its_mask_and_never_its_value(
    admin_client: AsyncClient, receiver: Receiver
) -> None:
    await create(admin_client, "webhook")
    await create(admin_client, "slack")

    webhook = await admin_client.get("/notifications/1")
    slack = await admin_client.get("/notifications/2")
    listing = await admin_client.get("/notifications")

    assert webhook.text.count(f'<code class="mono secret-mask">{MASK}</code>') == 2
    assert slack.text.count(f'<code class="mono secret-mask">{HOOK_MASK}</code>') == 1
    assert webhook.text.count("Set. Shown masked only.") == 2
    assert slack.text.count("Set. Shown masked only.") == 1
    assert "Cannot be opened" not in webhook.text + slack.text
    assert not any(secret in page.text for secret in SECRETS for page in (webhook, slack, listing))
    assert webhook.headers["cache-control"] == "no-store"


async def test_a_channel_whose_secrets_the_key_cannot_open_says_so_and_shows_no_mask(
    admin_client: AsyncClient, receiver: Receiver, started_app: FastAPI
) -> None:
    await create(admin_client, "webhook")
    async with started_app.state.database.session() as db:
        await db.execute("UPDATE notification_channel SET secrets_ciphertext = 'corrupt'")

    detail = await admin_client.get("/notifications/1")

    assert detail.status_code == OK
    assert "cannot be opened with the current key" in detail.text
    assert "secret-mask" not in detail.text
    assert detail.text.count("Set. It is never shown.") == 2


async def test_the_test_button_reports_the_receivers_answer_and_records_each_test(
    admin_client: AsyncClient, receiver: Receiver, started_app: FastAPI
) -> None:
    await create(admin_client, "webhook")

    accepted = await post(admin_client, "/notifications/1/test")
    receiver.status = 502
    refused = await post(admin_client, "/notifications/1/test")
    fragment = await admin_client.post(
        "/notifications/1/test", data=csrf_form(admin_client), headers=HTMX
    )

    results = await rows(
        started_app,
        "SELECT result, detail FROM activity_event WHERE action = 'notification_channel_tested' "
        "ORDER BY id",
    )
    assert accepted.status_code == OK and "The test message was accepted" in accepted.text
    assert "x-dockmin-signature" in receiver.requests[0].headers
    assert refused.status_code == OK and "HTTP status 502" in refused.text
    assert "notification-test" in fragment.text and "<html" not in fragment.text
    assert [r["result"] for r in results] == ["success", "failure", "failure"]
    assert not any(secret in json.dumps(results) for secret in SECRETS)


async def test_an_internal_address_is_refused_beside_its_field_naming_the_setting(
    admin_client: AsyncClient, receiver: Receiver
) -> None:
    refused = await create(admin_client, "webhook", url="http://127.0.0.1:9/in")

    assert refused.status_code == UNPROCESSABLE and SETTING in refused.text


async def test_editing_keeps_a_blank_secret_clears_a_ticked_one_and_refuses_a_stale_form(
    admin_client: AsyncClient, receiver: Receiver
) -> None:
    await create(admin_client, "webhook")
    form = dict(FORMS["webhook"], header_value="", hmac_key="", name="renamed", is_enabled="on")

    stamp = form_values((await admin_client.get("/notifications/1/edit")).text)[
        "expected_updated_at"
    ]
    kept = await post(admin_client, "/notifications/1", expected_updated_at=stamp, **form)
    await post(admin_client, "/notifications/1/test")
    stamp = form_values((await admin_client.get("/notifications/1/edit")).text)[
        "expected_updated_at"
    ]
    cleared = await post(
        admin_client, "/notifications/1", expected_updated_at=stamp, clear_hmac_key="on", **form
    )
    await post(admin_client, "/notifications/1/test")
    stale = await post(
        admin_client, "/notifications/1", expected_updated_at="2000-01-01T00:00:00Z", **form
    )

    assert (kept.status_code, cleared.status_code, stale.status_code) == (
        SEE_OTHER,
        SEE_OTHER,
        UNPROCESSABLE,
    )
    assert "x-dockmin-signature" in receiver.requests[0].headers
    assert "x-dockmin-signature" not in receiver.requests[1].headers
    assert receiver.requests[1].headers["x-key"] == HEADER


async def test_moving_a_channels_address_drops_its_stored_secrets(
    admin_client: AsyncClient, receiver: Receiver
) -> None:
    await create(admin_client, "webhook")
    form = dict(
        FORMS["webhook"], header_value="", hmac_key="", name="webhook channel", is_enabled="on"
    )
    form["url"] = "https://elsewhere.example.test/in"
    form["header_name"] = ""

    stamp = form_values((await admin_client.get("/notifications/1/edit")).text)[
        "expected_updated_at"
    ]
    moved = await post(admin_client, "/notifications/1", expected_updated_at=stamp, **form)
    await post(admin_client, "/notifications/1/test")

    assert moved.status_code == SEE_OTHER
    assert "x-dockmin-signature" not in receiver.requests[0].headers
    assert "Not set." in (await admin_client.get("/notifications/1")).text


async def test_a_name_taken_in_another_case_is_refused(
    admin_client: AsyncClient, receiver: Receiver
) -> None:
    await create(admin_client, "webhook", name="hook")

    duplicate = await create(admin_client, "webhook", name="HOOK")

    assert duplicate.status_code == UNPROCESSABLE and "already exists" in duplicate.text


async def test_subscriptions_are_saved_from_the_catalogue_and_refused_for_an_unknown_type(
    admin_client: AsyncClient, receiver: Receiver, started_app: FastAPI
) -> None:
    await create(admin_client, "slack")

    page = await admin_client.get("/notifications/1/subscriptions")
    unknown = await admin_client.post(
        "/notifications/1/subscriptions",
        data=csrf_form(admin_client, event_type="bogus", environment_scope="all"),
    )
    saved = await admin_client.post(
        "/notifications/1/subscriptions",
        data=csrf_form(
            admin_client,
            event_type=["update_applied", "automation_run_failed"],
            environment_scope="all",
        ),
    )

    stored = await rows(started_app, "SELECT event_type FROM notification_subscription")
    assert page.status_code == OK and "Environment unreachable" in page.text
    assert unknown.status_code == UNPROCESSABLE and saved.status_code == SEE_OTHER
    assert sorted(r["event_type"] for r in stored) == ["automation_run_failed", "update_applied"]


async def test_the_link_base_url_is_validated_and_stored_without_a_trailing_slash(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    invalid = await post(admin_client, "/notifications/-/settings", link_base_url="ftp://x")
    saved = await post(
        admin_client, "/notifications/-/settings", link_base_url="https://dock.example.test/"
    )

    stored = await rows(
        started_app, "SELECT value FROM setting WHERE key = 'notification_link_base_url'"
    )
    listing = await land(admin_client, saved)
    assert invalid.status_code == UNPROCESSABLE
    assert saved.status_code == SEE_OTHER and location(saved) == "/notifications"
    assert stored == [{"value": "https://dock.example.test"}]
    assert 'value="https://dock.example.test"' in listing.text


async def test_deleting_names_the_channel_and_needs_the_name_the_page_showed(
    admin_client: AsyncClient, receiver: Receiver
) -> None:
    await create(admin_client, "slack", name="team chat")

    confirm = await admin_client.get("/notifications/1/delete")
    wrong = await post(admin_client, "/notifications/1/delete", expected_name="other")
    deleted = await post(admin_client, "/notifications/1/delete", expected_name="team chat")

    assert confirm.status_code == OK and "team chat" in confirm.text
    assert wrong.status_code == UNPROCESSABLE and deleted.status_code == SEE_OTHER
    assert (await admin_client.get("/notifications/1")).status_code == NOT_FOUND


async def test_each_action_leaves_its_row_in_the_activity_log_in_order(
    admin_client: AsyncClient, receiver: Receiver, started_app: FastAPI
) -> None:
    await create(admin_client, "slack", name="team chat")
    await post(admin_client, "/notifications/1/test")
    await admin_client.post(
        "/notifications/1/subscriptions",
        data=csrf_form(admin_client, event_type="update_applied", environment_scope="all"),
    )
    await post(admin_client, "/notifications/-/settings", link_base_url="https://dock.example.test")
    stamp = form_values((await admin_client.get("/notifications/1/edit")).text)[
        "expected_updated_at"
    ]
    await post(
        admin_client,
        "/notifications/1",
        expected_updated_at=stamp,
        name="chat",
        is_enabled="on",
        webhook_url="",
    )
    await post(admin_client, "/notifications/1/delete", expected_name="chat")

    logged = await rows(
        started_app,
        "SELECT action, actor_type, target_type, target_id, result FROM activity_event "
        "WHERE action LIKE 'notification_%' ORDER BY id",
    )
    assert [(r["action"], r["result"]) for r in logged] == [
        ("notification_channel_created", "success"),
        ("notification_channel_tested", "success"),
        ("notification_subscriptions_changed", "success"),
        ("notification_settings_updated", "success"),
        ("notification_channel_updated", "success"),
        ("notification_channel_deleted", "success"),
    ]
    assert {r["actor_type"] for r in logged} == {"user"}
    assert [r["target_type"] for r in logged][3] == "notification_settings"
    assert {r["target_id"] for r in logged if r["target_type"] == "notification_channel"} == {"1"}


async def test_the_last_success_and_the_last_error_are_shown_on_the_channel_and_its_list(
    admin_client: AsyncClient, receiver: Receiver, started_app: FastAPI
) -> None:
    await create(admin_client, "slack")
    before = await admin_client.get("/notifications/1")
    async with started_app.state.database.session() as db:
        channels = NotificationChannelRepository(db, started_app.state.auth.clock)
        await channels.record_success(1)
        await channels.record_failure(1, "The receiver answered with HTTP status 503.")

    detail = await admin_client.get("/notifications/1")
    listing = await admin_client.get("/notifications")

    assert "Never" in before.text and "HTTP status 503" not in before.text
    assert "HTTP status 503" in detail.text and "HTTP status 503" in listing.text
    assert "Never" not in detail.text.split("Last success")[1].split("Last error")[0]


async def wait_for(
    started_app: FastAPI, condition: Callable[[list[dict[str, object]]], bool]
) -> None:
    async with asyncio.timeout(WAIT_SECONDS):
        while not condition(  # noqa: ASYNC110
            await rows(started_app, "SELECT attempts FROM notification_outbox")
        ):
            await asyncio.sleep(POLL_SECONDS)


async def test_an_event_in_the_running_application_is_delivered_without_waiting_for_the_grid(
    admin_client: AsyncClient,
    started_app: FastAPI,
    receivers: StartReceiver,
    address_policy: Callable[..., None],
) -> None:
    receiver = await receivers()
    address_policy("127.0.0.0/8")
    await post(
        admin_client,
        "/notifications",
        kind="webhook",
        name="local",
        is_enabled="on",
        url=receiver.url("/hook"),
    )
    await admin_client.post(
        "/notifications/1/subscriptions",
        data=csrf_form(admin_client, event_type="environment_unreachable", environment_scope="all"),
    )

    async with started_app.state.database.session() as db:
        await ActivityRecorder(db).record_event(
            SubsystemActor("environment-snapshot-refresher", None),
            action="environment_unreachable",
            target_type="environment",
            target_id="1",
            result="success",
            detail={"name": "prod"},
        )
    await wait_for(started_app, lambda outbox: outbox == [])

    [received] = receiver.requests
    channel = await rows(
        started_app, "SELECT last_success_at, last_error FROM notification_channel"
    )
    assert received.json()["events"][0]["type"] == "environment_unreachable"
    assert channel[0]["last_success_at"] is not None and channel[0]["last_error"] is None
