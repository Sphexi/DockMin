"""Notification channels: forms, sealed secrets, tests of a saved channel and their rows."""

# ruff: noqa: F811

import io
import json
import shutil
import ssl
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.crypto.registry import SECRET_FIELDS
from app.crypto.vault import Vault
from app.db.engine import Database
from app.db.repositories.notification_channel import NotificationChannelRepository
from app.netpolicy import REFUSAL_TEXT
from app.notifications.catalogue import CATALOGUE
from app.services import notifications as notification_module
from app.services.activity import ActivityRecorder, SubsystemActor
from app.services.notifications import (
    DESTINATION_MOVED,
    INSECURE_WARNING,
    INTERNAL_WARNING,
    SECRET_UNREADABLE,
    ChannelInput,
    NotificationService,
    SecretsUnreadable,
    SubscriptionInput,
    open_secrets,
)
from tests.crypto.helpers import FIRST_KEY, vault_with
from tests.job_support import ADMIN, STANDARD
from tests.notification_fixtures import (  # noqa: F401
    StartReceiver,
    StartSink,
    certificates,
    receivers,
    sinks,
    trusting,
)
from tests.notification_support import SMTP_PASSWORD, SMTP_USER, HttpReceiver, SmtpSink
from tests.scheduler.support import Clock

HMAC = "hmac-key-sentinel-1"
HEADER = "header-value-sentinel-2"
TOKEN = "access-token-sentinel-3"
HOOK = "https://hooks.example.test/T000/B000/hook-sentinel-4"
SETTING = "DOCKMIN_PERMITTED_INTERNAL_NETWORKS"
WEBHOOK_URL = "https://hooks.example.test/receive"
MOVED_URL = "https://other.example.test/receive"
STEP = 5
MISSING_ID = 999
ABSENT_ENVIRONMENT = 4242
NAME_TAKEN = "A channel with this name already exists."
CHANGED = "Changed since it was reviewed; nothing was saved."
SECRET_VALUES = (HMAC, HEADER, TOKEN, HOOK, SMTP_PASSWORD)


class Harness:
    """The service over a throwaway database, one session per call as a request would open."""

    def __init__(self, database: Database, vault: Vault) -> None:
        self.database = database
        self.vault = vault
        self.clock = Clock()

    async def call(
        self,
        name: str,
        *arguments: Any,
        transport: httpx.AsyncBaseTransport | None = None,
        ssl_context: ssl.SSLContext | None = None,
        **options: Any,
    ) -> Any:
        async with self.database.session() as session:
            service = NotificationService(
                session,
                self.vault,
                self.clock,
                ActivityRecorder(session, self.clock),
                transport=transport,
                ssl_context=ssl_context,
            )
            return await getattr(service, name)(*arguments, **options)

    async def outcome(self, name: str, *arguments: Any, **options: Any) -> Any:
        """Return a refusal's fields, or the strings denied, notfound or ok."""
        try:
            await self.call(name, *arguments, **options)
        except ValidationFailed as refused:
            return refused.fields
        except PermissionDenied:
            return "denied"
        except ResourceNotFound:
            return "notfound"
        return "ok"

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        connection = self.database.connect()
        try:
            found = connection.execute(sql, params).fetchall()
            connection.commit()
            return found
        finally:
            connection.close()

    def events(self, action: str) -> list[dict[str, Any]]:
        return [
            {**dict(row), "detail": json.loads(row["detail"]) if row["detail"] else {}}
            for row in self.rows(
                "SELECT actor_type, actor_user_id, target_type, target_id, result, detail "
                "FROM activity_event WHERE action = ? ORDER BY id",
                (action,),
            )
        ]

    def stamp(self, channel_id: int) -> str:
        return str(
            self.rows("SELECT updated_at FROM notification_channel WHERE id = ?", (channel_id,))[0][
                0
            ]
        )

    async def edit(self, channel_id: int, form: ChannelInput) -> Any:
        self.clock.jump(STEP)
        return await self.outcome(
            "update", ADMIN, channel_id, form, expected_updated_at=self.stamp(channel_id)
        )

    async def secrets_of(self, channel_id: int) -> dict[str, str]:
        async with self.database.session() as session:
            channel = await NotificationChannelRepository(session, self.clock).get(channel_id)
        assert channel is not None
        return open_secrets(self.vault, channel)


@pytest.fixture
async def harness(provisioned_template: Path, tmp_path: Path) -> AsyncIterator[Harness]:
    path = tmp_path / "dockmin.db"
    shutil.copyfile(provisioned_template, path)
    database = Database(path)
    yield Harness(database, vault_with(FIRST_KEY))
    await database.aclose()


def webhook(url: str = WEBHOOK_URL, **secrets: str) -> ChannelInput:
    return ChannelInput(
        name="chat",
        kind="webhook",
        values={"url": url, "header_name": "X-Token" if "header_value" in secrets else ""},
        secrets={name: SecretStr(value) for name, value in secrets.items()},
    )


def everything(
    event_types: set[str], scope: str = "all", environments: set[int] | None = None
) -> SubscriptionInput:
    return SubscriptionInput(frozenset(event_types), scope, frozenset(environments or ()))


def accepted(request: httpx.Request) -> httpx.Response:
    return httpx.Response(204, stream=httpx.ByteStream(b""))


def rejected(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, stream=httpx.ByteStream(b""))


def form_of(kind: str, receiver: HttpReceiver | SmtpSink) -> ChannelInput:
    match kind:
        case "webhook":
            assert isinstance(receiver, HttpReceiver)
            return webhook(receiver.url("/hook"), hmac_key=HMAC, header_value=HEADER)
        case "ntfy":
            assert isinstance(receiver, HttpReceiver)
            return ChannelInput(
                name="chat",
                kind="ntfy",
                values={"server_url": receiver.url(), "topic": "alerts"},
                secrets={"token": SecretStr(TOKEN)},
            )
        case "gotify":
            assert isinstance(receiver, HttpReceiver)
            return ChannelInput(
                name="chat",
                kind="gotify",
                values={"server_url": receiver.url()},
                secrets={"token": SecretStr(TOKEN)},
            )
        case "discord" | "slack" | "teams":
            assert isinstance(receiver, HttpReceiver)
            return ChannelInput(
                name="chat",
                kind=kind,
                values={},
                secrets={
                    "webhook_url": SecretStr(receiver.url(f"/{kind}/{HOOK.rsplit('/', 1)[1]}"))
                },
            )
        case _:
            assert isinstance(receiver, SmtpSink)
            return ChannelInput(
                name="chat",
                kind="email",
                values={
                    "host": "127.0.0.1",
                    "port": str(receiver.port),
                    "security": kind.removeprefix("email_"),
                    "sender": "dockmin@example.test",
                    "recipients": "ops@example.test, dev@example.test",
                    "username": SMTP_USER,
                },
                secrets={"password": SecretStr(SMTP_PASSWORD)},
            )


def check_webhook(received: Any) -> None:
    body = received.json()
    assert received.target == "/hook"
    assert received.headers["x-token"] == HEADER and "x-dockmin-signature" in received.headers
    assert (body["schema_version"], body["channel"]) == (1, "chat")
    assert body["events"][0]["label"] == "Test notification"
    assert body["events"][0]["actor"] == {"type": "user", "name": ADMIN.username, "rule": None}


def check_ntfy(received: Any) -> None:
    assert received.target == "/alerts"
    assert received.headers["authorization"] == f"Bearer {TOKEN}"
    assert received.headers["title"] == "dockmin: Test notification"


def check_gotify(received: Any) -> None:
    assert received.target == "/message" and received.headers["x-gotify-key"] == TOKEN
    assert received.json()["title"] == "dockmin: Test notification"


def check_discord(received: Any) -> None:
    assert received.target.startswith("/discord/")
    assert received.json()["embeds"][0]["description"].startswith("**Test notification**")


def check_slack(received: Any) -> None:
    assert received.target.startswith("/slack/")
    assert received.json()["text"].startswith("*dockmin: Test notification*")


def check_teams(received: Any) -> None:
    assert received.target.startswith("/teams/")
    assert received.json()["attachments"][0]["content"]["type"] == "AdaptiveCard"


HTTP_KINDS = [
    pytest.param("webhook", check_webhook, id="webhook"),
    pytest.param("ntfy", check_ntfy, id="ntfy"),
    pytest.param("gotify", check_gotify, id="gotify"),
    pytest.param("discord", check_discord, id="discord"),
    pytest.param("slack", check_slack, id="slack"),
    pytest.param("teams", check_teams, id="teams"),
]


@pytest.mark.parametrize(("kind", "check"), HTTP_KINDS)
async def test_testing_a_saved_channel_posts_the_native_format_to_its_receiver(
    harness: Harness,
    receivers: StartReceiver,
    kind: str,
    check: Callable[[Any], None],
) -> None:
    receiver = await receivers()
    channel_id = await harness.call("create", ADMIN, form_of(kind, receiver))

    await harness.call("test", ADMIN, channel_id)

    [received] = receiver.requests
    check(received)
    [row] = harness.events("notification_channel_tested")
    assert (row["result"], row["target_id"]) == ("success", str(channel_id))
    assert row["detail"]["kind"] == kind and row["detail"]["duration_ms"] >= 0


@pytest.mark.parametrize("security", ["starttls", "tls"])
async def test_testing_a_saved_email_channel_sends_a_message_over_the_chosen_security(
    harness: Harness,
    sinks: StartSink,
    trusting: ssl.SSLContext,
    security: str,
) -> None:
    sink = await sinks(security)
    channel_id = await harness.call("create", ADMIN, form_of(f"email_{security}", sink))

    await harness.call("test", ADMIN, channel_id, ssl_context=trusting)

    [mail] = sink.messages
    assert (mail.encrypted, mail.authenticated_as) == (True, SMTP_USER)
    assert mail.recipients == ("ops@example.test", "dev@example.test")
    assert mail.parsed()["Subject"] == "dockmin: Test notification"
    assert ADMIN.username in mail.parsed().get_payload()


async def test_a_test_the_receiver_refuses_is_reported_in_product_words_and_recorded(
    harness: Harness,
) -> None:
    channel_id = await harness.call("create", ADMIN, webhook(hmac_key=HMAC))

    with pytest.raises(ValidationFailed) as refused:
        await harness.call("test", ADMIN, channel_id, transport=httpx.MockTransport(rejected))

    [row] = harness.events("notification_channel_tested")
    assert refused.value.fields == {"test": "The receiver answered with HTTP status 503."}
    assert row["result"] == "failure" and row["detail"]["reason"] == "status_503"
    assert HMAC not in json.dumps(row)


async def test_creating_a_channel_seals_every_secret_and_records_only_their_names(
    harness: Harness,
) -> None:
    form = webhook(hmac_key=HMAC, header_value=HEADER)

    channel_id = await harness.call("create", ADMIN, form)

    [stored] = harness.rows("SELECT settings, secrets_ciphertext FROM notification_channel")
    [event] = harness.events("notification_channel_created")
    assert not any(secret in stored["settings"] for secret in (HMAC, HEADER))
    assert not any(secret in stored["secrets_ciphertext"] for secret in (HMAC, HEADER))
    assert await harness.secrets_of(channel_id) == {"hmac_key": HMAC, "header_value": HEADER}
    assert (event["actor_type"], event["actor_user_id"], event["result"]) == (
        "user",
        ADMIN.user_id,
        "success",
    )
    assert (event["target_type"], event["target_id"]) == ("notification_channel", str(channel_id))
    assert event["detail"] == {
        "name": "chat",
        "kind": "webhook",
        "is_enabled": True,
        "secrets": "header_value, hmac_key",
    }


async def test_no_secret_is_shown_back_logged_or_written_to_the_activity_log(
    harness: Harness, log_stream: io.StringIO
) -> None:
    form = ChannelInput(
        name="chat",
        kind="gotify",
        values={"server_url": "https://gotify.example.test"},
        secrets={"token": SecretStr(TOKEN)},
    )
    channel_id = await harness.call("create", ADMIN, form)
    chat = await harness.call(
        "create",
        ADMIN,
        ChannelInput("slack", "slack", secrets={"webhook_url": SecretStr(HOOK)}),
    )
    with pytest.raises(ValidationFailed):
        await harness.call("test", ADMIN, chat, transport=httpx.MockTransport(rejected))

    views = [await harness.call("get", ADMIN, channel_id), await harness.call("get", ADMIN, chat)]
    listing = await harness.call("list_channels", ADMIN)
    shown = f"{views!r} {listing!r} {form!r}"
    logged = log_stream.getvalue()
    stored = json.dumps([tuple(row) for row in harness.rows("SELECT * FROM activity_event")])
    assert (await harness.secrets_of(channel_id))["token"] == TOKEN
    assert (await harness.secrets_of(chat))["webhook_url"] == HOOK
    assert not any(secret in shown + logged + stored for secret in SECRET_VALUES)
    assert (
        views[0].secrets_set == frozenset({"token"})
        and views[1].summary == "Webhook address stored"
    )


async def test_a_secret_sealed_for_one_channel_cannot_be_opened_as_another(
    harness: Harness,
) -> None:
    first = await harness.call("create", ADMIN, webhook(hmac_key=HMAC))
    second = await harness.call(
        "create",
        ADMIN,
        ChannelInput(
            "other", "webhook", values={"url": WEBHOOK_URL}, secrets={"hmac_key": SecretStr("k")}
        ),
    )
    [(sealed,)] = harness.rows(
        "SELECT secrets_ciphertext FROM notification_channel WHERE id = ?", (first,)
    )
    harness.rows(
        "UPDATE notification_channel SET secrets_ciphertext = ? WHERE id = ?", (sealed, second)
    )

    with pytest.raises(SecretsUnreadable):
        await harness.secrets_of(second)


def test_the_channel_secrets_are_among_the_columns_rekeying_and_restore_reseal() -> None:
    assert ("notification_channel", "secrets_ciphertext") in {
        (field.table, field.column) for field in SECRET_FIELDS
    }


async def test_a_blank_secret_keeps_the_stored_one_and_a_cleared_one_is_removed(
    harness: Harness,
) -> None:
    channel_id = await harness.call("create", ADMIN, webhook(hmac_key=HMAC))

    kept = await harness.edit(channel_id, webhook())
    cleared = await harness.edit(
        channel_id,
        ChannelInput("chat", "webhook", values={"url": WEBHOOK_URL}, clear=frozenset({"hmac_key"})),
    )

    assert (kept, cleared) == ("ok", "ok")
    events = harness.events("notification_channel_updated")
    assert len(events) == 1 and events[0]["detail"]["changed"] == "hmac_key"
    assert await harness.secrets_of(channel_id) == {}


async def test_replacing_a_secret_records_which_one_was_replaced_and_not_its_value(
    harness: Harness,
) -> None:
    channel_id = await harness.call("create", ADMIN, webhook(hmac_key=HMAC))

    await harness.edit(channel_id, webhook(hmac_key="replacement-key"))

    [event] = harness.events("notification_channel_updated")
    assert event["detail"]["secrets_replaced"] == "hmac_key"
    assert "replacement-key" not in json.dumps(event)
    assert (await harness.secrets_of(channel_id))["hmac_key"] == "replacement-key"


async def test_an_edit_that_changes_nothing_writes_no_row(harness: Harness) -> None:
    channel_id = await harness.call("create", ADMIN, webhook(hmac_key=HMAC))

    outcome = await harness.edit(channel_id, webhook())

    assert outcome == "ok" and harness.events("notification_channel_updated") == []


async def test_changing_the_destination_clears_the_secrets_unless_they_are_entered_again(
    harness: Harness,
) -> None:
    channel_id = await harness.call("create", ADMIN, webhook(hmac_key=HMAC))

    same = await harness.edit(channel_id, webhook())
    kept_after_same = await harness.secrets_of(channel_id)
    moved = await harness.edit(channel_id, webhook(MOVED_URL))
    cleared = await harness.secrets_of(channel_id)
    again = await harness.edit(channel_id, webhook("https://third.example.test/x", hmac_key="k2"))

    assert (same, moved, again) == ("ok", "ok", "ok")
    assert kept_after_same == {"hmac_key": HMAC}
    assert cleared == {}
    assert await harness.secrets_of(channel_id) == {"hmac_key": "k2"}


async def test_moving_a_destination_that_needs_its_secret_demands_it_again(
    harness: Harness,
) -> None:
    channel_id = await harness.call(
        "create",
        ADMIN,
        ChannelInput(
            "chat",
            "gotify",
            values={"server_url": "https://gotify.example.test"},
            secrets={"token": SecretStr(TOKEN)},
        ),
    )

    moved = await harness.edit(
        channel_id,
        ChannelInput("chat", "gotify", values={"server_url": "https://gotify2.example.test"}),
    )

    assert moved == {"token": DESTINATION_MOVED}
    assert await harness.secrets_of(channel_id) == {"token": TOKEN}


async def test_moving_the_mail_server_demands_the_password_again(
    harness: Harness,
    sinks: StartSink,
) -> None:
    sink = await sinks("starttls")
    channel_id = await harness.call("create", ADMIN, form_of("email_starttls", sink))
    moved = form_of("email_starttls", sink)
    moved = ChannelInput(
        moved.name, moved.kind, values={**moved.values, "host": "mail.example.test"}
    )

    outcome = await harness.edit(channel_id, moved)

    assert outcome == {"password": DESTINATION_MOVED}


async def test_stored_secrets_the_key_cannot_open_must_be_entered_again_and_block_a_test(
    harness: Harness,
) -> None:
    channel_id = await harness.call("create", ADMIN, webhook(hmac_key=HMAC, header_value=HEADER))
    harness.rows("UPDATE notification_channel SET secrets_ciphertext = 'v1:garbage'")

    saved_blind = await harness.edit(channel_id, webhook(header_value="new-value"))
    tested = await harness.outcome("test", ADMIN, channel_id)

    [event] = harness.events("notification_channel_tested")
    assert saved_blind == {"hmac_key": SECRET_UNREADABLE}
    assert (
        tested == {"test": SECRET_UNREADABLE} and event["detail"]["reason"] == "secrets_unreadable"
    )


async def test_a_stale_form_and_a_name_taken_in_another_case_are_refused(harness: Harness) -> None:
    channel_id = await harness.call("create", ADMIN, webhook())
    await harness.call(
        "create", ADMIN, ChannelInput("second", "webhook", values={"url": WEBHOOK_URL})
    )

    stale = await harness.outcome(
        "update", ADMIN, channel_id, webhook(), expected_updated_at="2000-01-01T00:00:00Z"
    )
    taken = await harness.edit(
        channel_id, ChannelInput("SECOND", "webhook", values={"url": WEBHOOK_URL})
    )
    duplicate = await harness.outcome(
        "create", ADMIN, ChannelInput("Chat", "webhook", values={"url": WEBHOOK_URL})
    )

    assert stale == {"form": CHANGED}
    assert taken == {"name": NAME_TAKEN} and duplicate == {"name": NAME_TAKEN}


async def test_the_channel_limit_is_enforced(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notification_module, "MAX_CHANNELS", 1)
    await harness.call("create", ADMIN, webhook())

    outcome = await harness.outcome(
        "create", ADMIN, ChannelInput("two", "webhook", values={"url": WEBHOOK_URL})
    )

    assert list(outcome) == ["name"]


INVALID_FORMS: dict[str, tuple[ChannelInput, set[str]]] = {
    "unknown-kind": (ChannelInput("x", "pager"), {"kind"}),
    "blank-name": (ChannelInput("  ", "webhook", values={"url": WEBHOOK_URL}), {"name"}),
    "long-name": (ChannelInput("n" * 101, "webhook", values={"url": WEBHOOK_URL}), {"name"}),
    "control-name": (ChannelInput("a\x00b", "webhook", values={"url": WEBHOOK_URL}), {"name"}),
    "webhook-scheme": (
        ChannelInput("x", "webhook", values={"url": "ftp://x.example.test"}),
        {"url"},
    ),
    "webhook-user-info": (
        ChannelInput("x", "webhook", values={"url": "https://u:p@x.example.test/"}),
        {"url"},
    ),
    "reserved-header": (
        ChannelInput(
            "x",
            "webhook",
            values={"url": WEBHOOK_URL, "header_name": "Host"},
            secrets={"header_value": SecretStr("v")},
        ),
        {"header_name", "header_value"},
    ),
    "header-without-value": (
        ChannelInput("x", "webhook", values={"url": WEBHOOK_URL, "header_name": "X-Token"}),
        {"header_value"},
    ),
    "value-without-header": (
        ChannelInput(
            "x", "webhook", values={"url": WEBHOOK_URL}, secrets={"header_value": SecretStr("v")}
        ),
        {"header_name"},
    ),
    "signing-key-control": (
        ChannelInput(
            "x", "webhook", values={"url": WEBHOOK_URL}, secrets={"hmac_key": SecretStr("a\x07b")}
        ),
        {"hmac_key"},
    ),
    "ntfy-topic": (
        ChannelInput("x", "ntfy", values={"server_url": WEBHOOK_URL, "topic": "a b"}),
        {"topic"},
    ),
    "ntfy-query": (
        ChannelInput("x", "ntfy", values={"server_url": WEBHOOK_URL + "?a=b", "topic": "t"}),
        {"server_url"},
    ),
    "gotify-no-token": (
        ChannelInput("x", "gotify", values={"server_url": WEBHOOK_URL}),
        {"token"},
    ),
    "gotify-spaced-token": (
        ChannelInput(
            "x",
            "gotify",
            values={"server_url": WEBHOOK_URL},
            secrets={"token": SecretStr("with space")},
        ),
        {"token"},
    ),
    "chat-no-url": (ChannelInput("x", "discord"), {"webhook_url"}),
    "chat-not-a-url": (
        ChannelInput("x", "slack", secrets={"webhook_url": SecretStr("hooks")}),
        {"webhook_url"},
    ),
    "email-nothing": (ChannelInput("x", "email"), {"host", "sender", "recipients"}),
    "email-port": (
        ChannelInput(
            "x",
            "email",
            values={
                "host": "mail.example.test",
                "port": "70000",
                "sender": "a@example.test",
                "recipients": "b@example.test",
            },
        ),
        {"port"},
    ),
    "email-security": (
        ChannelInput(
            "x",
            "email",
            values={
                "host": "mail.example.test",
                "security": "plain",
                "sender": "a@example.test",
                "recipients": "b@example.test",
            },
        ),
        {"security"},
    ),
    "email-recipient": (
        ChannelInput(
            "x",
            "email",
            values={
                "host": "mail.example.test",
                "sender": "a@example.test",
                "recipients": "not-a-mailbox",
            },
        ),
        {"recipients"},
    ),
    "email-too-many-recipients": (
        ChannelInput(
            "x",
            "email",
            values={
                "host": "mail.example.test",
                "sender": "a@example.test",
                "recipients": ",".join(f"r{n}@example.test" for n in range(11)),
            },
        ),
        {"recipients"},
    ),
    "email-password-without-user": (
        ChannelInput(
            "x",
            "email",
            values={
                "host": "mail.example.test",
                "sender": "a@example.test",
                "recipients": "b@example.test",
            },
            secrets={"password": SecretStr("p")},
        ),
        {"username"},
    ),
    "email-user-without-password": (
        ChannelInput(
            "x",
            "email",
            values={
                "host": "mail.example.test",
                "sender": "a@example.test",
                "recipients": "b@example.test",
                "username": "u",
            },
        ),
        {"password"},
    ),
}


async def test_a_form_that_breaks_a_rule_is_refused_on_exactly_the_fields_at_fault(
    harness: Harness,
) -> None:
    refused = {
        label: set(await harness.outcome("create", ADMIN, form))
        for label, (form, _) in INVALID_FORMS.items()
    }

    assert refused == {label: fields for label, (_, fields) in INVALID_FORMS.items()}
    assert harness.rows("SELECT 1 FROM notification_channel") == []


async def test_an_internal_receiver_is_refused_naming_the_setting_unless_the_permit_covers_it(
    harness: Harness, address_policy: Callable[..., None]
) -> None:
    form = webhook("http://127.0.0.1:9/hook")

    before = await harness.outcome("create", ADMIN, form)
    address_policy("127.0.0.0/8")
    after = await harness.outcome("create", ADMIN, form)

    assert before == {"url": REFUSAL_TEXT} and SETTING in REFUSAL_TEXT
    assert after == "ok"


async def test_a_permitted_internal_or_plain_http_address_carries_a_warning_on_its_page(
    harness: Harness, address_policy: Callable[..., None]
) -> None:
    address_policy("127.0.0.0/8")
    channel_id = await harness.call("create", ADMIN, webhook("http://127.0.0.1:9/hook"))

    view = await harness.call("get", ADMIN, channel_id)

    assert view.warnings == (INSECURE_WARNING, INTERNAL_WARNING)


async def test_a_test_to_an_address_the_policy_no_longer_permits_names_the_setting(
    harness: Harness,
    receivers: StartReceiver,
    address_policy: Callable[..., None],
) -> None:
    receiver = await receivers()
    channel_id = await harness.call("create", ADMIN, webhook(receiver.url("/hook")))
    address_policy()

    outcome = await harness.outcome("test", ADMIN, channel_id)

    assert outcome == {"test": REFUSAL_TEXT}
    assert receiver.requests == []


async def test_deleting_a_channel_needs_its_name_and_removes_subscriptions_and_queued_rows(
    harness: Harness,
) -> None:
    channel_id = await harness.call("create", ADMIN, webhook())
    await harness.call(
        "set_subscriptions", ADMIN, channel_id, everything({"environment_unreachable"})
    )
    async with harness.database.session() as session:
        await ActivityRecorder(session, harness.clock).record_event(
            SubsystemActor("environment-snapshot-refresher", None),
            action="environment_unreachable",
            target_type="environment",
            target_id="1",
            result="success",
        )
    assert len(harness.rows("SELECT 1 FROM notification_outbox")) == 1

    wrong = await harness.outcome("delete", ADMIN, channel_id, expected_name="other")
    await harness.call("delete", ADMIN, channel_id, expected_name="chat")

    refused, deleted = harness.events("notification_channel_deleted")
    assert wrong == {"name": "Changed since it was reviewed; nothing was deleted."}
    assert refused["result"] == "failure"
    assert deleted["detail"] == {"name": "chat", "kind": "webhook"}
    assert deleted["target_id"] == str(channel_id)
    for table in ("notification_channel", "notification_subscription", "notification_outbox"):
        assert harness.rows(f"SELECT 1 FROM {table}") == []


async def test_subscriptions_are_replaced_whole_and_the_change_is_recorded(
    harness: Harness,
) -> None:
    channel_id = await harness.call("create", ADMIN, webhook())
    await harness.call(
        "set_subscriptions", ADMIN, channel_id, everything({"update_applied", "dockmin_updated"})
    )

    await harness.call(
        "set_subscriptions",
        ADMIN,
        channel_id,
        everything({"update_applied", "automation_run_failed"}),
    )

    view = await harness.call("subscription_view", ADMIN, channel_id)
    first, second = harness.events("notification_subscriptions_changed")
    assert {choice.key for choice in view.event_types if choice.selected} == {
        "update_applied",
        "automation_run_failed",
    }
    assert first["detail"]["added"] == "dockmin_updated, update_applied"
    assert (second["detail"]["added"], second["detail"]["removed"]) == (
        "automation_run_failed",
        "dockmin_updated",
    )
    assert second["target_id"] == str(channel_id)


async def test_the_subscription_page_lists_every_catalogue_row_and_environment(
    harness: Harness,
) -> None:
    channel_id = await harness.call("create", ADMIN, webhook())
    harness.rows(
        "INSERT INTO environment (name, connection_kind, url, created_at, updated_at) "
        "VALUES ('prod', 'tcp_tls', 'tcp://prod:2376', 'x', 'x')"
    )

    view = await harness.call("subscription_view", ADMIN, channel_id)

    assert [choice.key for choice in view.event_types] == [row.key for row in CATALOGUE]
    assert [environment.name for environment in view.environments] == ["prod"]
    assert view.environment_scope == "all" and not view.more_environments


async def test_a_listed_scope_stores_its_environments_and_an_unchanged_save_writes_no_row(
    harness: Harness,
) -> None:
    channel_id = await harness.call("create", ADMIN, webhook())
    harness.rows(
        "INSERT INTO environment (name, connection_kind, url, created_at, updated_at) "
        "VALUES ('prod', 'tcp_tls', 'tcp://prod:2376', 'x', 'x')"
    )
    form = everything({"update_applied"}, "listed", {1})

    await harness.call("set_subscriptions", ADMIN, channel_id, form)
    await harness.call("set_subscriptions", ADMIN, channel_id, form)

    [event] = harness.events("notification_subscriptions_changed")
    assert event["detail"]["scope"] == "listed" and event["detail"]["environments"] == 1
    assert harness.rows("SELECT environment_id FROM notification_channel_environment")[0][0] == 1


@pytest.mark.parametrize(
    "form",
    [
        pytest.param(everything({"not_an_event"}), id="unknown-event-type"),
        pytest.param(everything({"update_applied"}, "some"), id="unknown-scope"),
        pytest.param(
            everything({"update_applied"}, "listed", {ABSENT_ENVIRONMENT}), id="unknown-environment"
        ),
    ],
)
async def test_subscriptions_naming_something_unknown_are_refused_and_change_nothing(
    harness: Harness, form: SubscriptionInput
) -> None:
    channel_id = await harness.call("create", ADMIN, webhook())

    outcome = await harness.outcome("set_subscriptions", ADMIN, channel_id, form)

    assert isinstance(outcome, dict)
    assert harness.rows("SELECT 1 FROM notification_subscription") == []


async def test_the_link_base_url_is_normalised_recorded_and_cleared(harness: Harness) -> None:
    await harness.call("set_link_base_url", ADMIN, " https://dock.example.test/ ")
    unchanged = await harness.outcome("set_link_base_url", ADMIN, "https://dock.example.test")
    await harness.call("set_link_base_url", ADMIN, "")

    settings = await harness.call("settings", ADMIN)
    first, second = harness.events("notification_settings_updated")
    assert unchanged == "ok" and settings.link_base_url == ""
    assert harness.rows("SELECT 1 FROM setting WHERE key = 'notification_link_base_url'") == []
    assert (first["target_type"], second["result"]) == ("notification_settings", "success")


async def test_a_link_base_url_change_records_whether_one_is_set(harness: Harness) -> None:
    await harness.call("set_link_base_url", ADMIN, "https://dock.example.test")
    await harness.call("set_link_base_url", ADMIN, "")

    first, second = harness.events("notification_settings_updated")
    assert (first["detail"], second["detail"]) == (
        {"link_base_url_set": True},
        {"link_base_url_set": False},
    )


@pytest.mark.parametrize(
    "value",
    [
        "ftp://dock.example.test",
        "dock.example.test",
        "https://dock.example.test/?a=b",
        "https://x/#f",
        "https://dock.example.test/" + "a" * 600,
    ],
)
async def test_a_link_base_url_that_is_not_a_plain_http_address_is_refused(
    harness: Harness, value: str
) -> None:
    outcome = await harness.outcome("set_link_base_url", ADMIN, value)

    assert list(outcome) == ["link_base_url"]


async def test_a_standard_user_is_refused_every_operation_and_nothing_is_written(
    harness: Harness,
) -> None:
    channel_id = await harness.call("create", ADMIN, webhook())
    written = len(harness.rows("SELECT 1 FROM activity_event"))
    calls = {
        "list_channels": (),
        "get": (channel_id,),
        "settings": (),
        "subscription_view": (channel_id,),
        "create": (webhook(),),
        "delete": (channel_id,),
        "test": (channel_id,),
        "set_subscriptions": (channel_id, everything(set())),
        "set_link_base_url": ("",),
    }

    refused = {
        name: await harness.outcome(name, STANDARD, *arguments, **_options(name))
        for name, arguments in calls.items()
    }
    refused["update"] = await harness.outcome(
        "update", STANDARD, channel_id, webhook(), expected_updated_at=harness.stamp(channel_id)
    )

    assert set(refused.values()) == {"denied"}
    assert len(harness.rows("SELECT 1 FROM activity_event")) == written


def _options(name: str) -> dict[str, str]:
    return {"expected_name": "chat"} if name == "delete" else {}


async def test_an_unknown_channel_is_not_found_for_an_administrator(harness: Harness) -> None:
    outcomes = [
        await harness.outcome("get", ADMIN, MISSING_ID),
        await harness.outcome("test", ADMIN, MISSING_ID),
        await harness.outcome("subscription_view", ADMIN, MISSING_ID),
        await harness.outcome("delete", ADMIN, MISSING_ID, expected_name="x"),
    ]

    assert outcomes == ["notfound"] * 4
