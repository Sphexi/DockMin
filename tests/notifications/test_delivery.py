"""HTTP delivery: each kind's request, failures as product codes, and what never leaves."""

import hashlib
import hmac
import io
import logging
import socket
import ssl
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app import netcheck, netpolicy
from app.logging import PINNED_CLIENT_LOGGERS
from app.net_transport import policy_transport
from app.notifications import delivery
from app.notifications.delivery import (
    DeliveryError,
    DiscordTarget,
    GotifyTarget,
    HttpDeliverer,
    HttpTarget,
    NtfyTarget,
    SlackTarget,
    TeamsTarget,
    WebhookTarget,
    classify_exception,
    message_for,
    prepare,
    sign,
    validate_endpoint_url,
    validate_secret_header,
    validate_token,
    validate_topic,
)
from tests.notification_fixtures import StartReceiver
from tests.notification_support import EVENT_TIME, HttpReceiver, Received, sample_event
from tests.registry_client.loopback import Certificates, serve_not_tls

HMAC_KEY = "hmac-key-sentinel"
HEADER_VALUE = "header-value-sentinel"
TOKEN = "token-sentinel"
HOOK_SECRET = "hook-path-sentinel"
CHANNEL = "chat"
LINK = "https://dock.example.test/activity/7"
SETTING = "DOCKMIN_PERMITTED_INTERNAL_NETWORKS"
LARGE_REPLY = delivery.MAX_RESPONSE_BYTES + 1024
SHORT_TIMEOUT = 0.2
SLOW_REPLY = 2.0

type Check = Callable[[Received], None]
type Make = Callable[[HttpReceiver], HttpTarget]


def webhook(receiver: HttpReceiver) -> HttpTarget:
    return WebhookTarget(
        receiver.url("/hook"),
        hmac_key=SecretStr(HMAC_KEY),
        header_name="X-Token",
        header_value=SecretStr(HEADER_VALUE),
    )


def check_webhook(received: Received) -> None:
    expected = "sha256=" + hmac.new(HMAC_KEY.encode(), received.body, hashlib.sha256).hexdigest()
    assert received.target == "/hook"
    assert received.headers["content-type"] == "application/json"
    assert received.headers["x-token"] == HEADER_VALUE
    assert received.headers["x-dockmin-signature"] == expected
    assert received.json()["schema_version"] == 1
    assert received.json()["events"][0]["link"] == LINK


def ntfy(receiver: HttpReceiver) -> HttpTarget:
    return NtfyTarget(receiver.url("/base/"), "alerts", SecretStr(TOKEN))


def check_ntfy(received: Received) -> None:
    assert received.target == "/base/alerts"
    assert received.headers["content-type"] == "text/plain; charset=utf-8"
    assert received.headers["title"] == "dockmin: Update applied"
    assert received.headers["tags"] == "white_check_mark"
    assert received.headers["authorization"] == f"Bearer {TOKEN}"
    assert received.body.decode().startswith("Update applied (success)\n  Target: container web")


def gotify(receiver: HttpReceiver) -> HttpTarget:
    return GotifyTarget(receiver.url(), SecretStr(TOKEN))


def check_gotify(received: Received) -> None:
    assert received.target == "/message"
    assert received.headers["x-gotify-key"] == TOKEN
    assert received.json()["title"] == "dockmin: Update applied"
    assert received.json()["priority"] == 5


def discord(receiver: HttpReceiver) -> HttpTarget:
    return DiscordTarget(SecretStr(receiver.url("/api/webhooks/1/token")))


def check_discord(received: Received) -> None:
    assert received.target == "/api/webhooks/1/token"
    assert received.json()["allowed_mentions"] == {"parse": []}
    assert received.json()["embeds"][0]["timestamp"] == "2030-01-02T03:00:00Z"


def slack(receiver: HttpReceiver) -> HttpTarget:
    return SlackTarget(SecretStr(receiver.url("/services/T/B/token")))


def check_slack(received: Received) -> None:
    assert received.target == "/services/T/B/token"
    assert received.json()["text"].startswith("*dockmin: Update applied*")


def teams(receiver: HttpReceiver) -> HttpTarget:
    return TeamsTarget(SecretStr(receiver.url("/workflows/token")))


def check_teams(received: Received) -> None:
    assert received.target == "/workflows/token"
    card = received.json()["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"


KINDS = [
    pytest.param(webhook, check_webhook, id="webhook"),
    pytest.param(ntfy, check_ntfy, id="ntfy"),
    pytest.param(gotify, check_gotify, id="gotify"),
    pytest.param(discord, check_discord, id="discord"),
    pytest.param(slack, check_slack, id="slack"),
    pytest.param(teams, check_teams, id="teams"),
]


async def send(target: HttpTarget, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
    async with HttpDeliverer(transport=transport, clock=lambda: EVENT_TIME) as http:
        await http.deliver(target, [sample_event(link=LINK)], channel_name=CHANNEL)


async def failure_of(target: HttpTarget, **options: Any) -> DeliveryError:
    with pytest.raises(DeliveryError) as raised:
        await send(target, **options)
    return raised.value


def accepted(request: httpx.Request) -> httpx.Response:
    return httpx.Response(204, stream=httpx.ByteStream(b""))


@pytest.fixture
def client_loggers_restored() -> Iterator[None]:
    levels = {name: logging.getLogger(name).level for name in PINNED_CLIENT_LOGGERS}
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize(("make", "check"), KINDS)
async def test_each_kind_posts_its_native_format_to_a_local_receiver(
    receivers: StartReceiver, make: Make, check: Check
) -> None:
    receiver = await receivers()

    await send(make(receiver))

    [received] = receiver.requests
    assert received.method == "POST"
    assert received.headers["user-agent"] == "dockmin"
    check(received)


async def test_a_webhook_without_a_key_or_header_sends_neither(receivers: StartReceiver) -> None:
    receiver = await receivers()

    await send(WebhookTarget(receiver.url("/hook")))

    [received] = receiver.requests
    assert "x-dockmin-signature" not in received.headers and "x-token" not in received.headers


async def test_an_ntfy_title_with_non_ascii_text_is_sent_as_an_encoded_word(
    receivers: StartReceiver,
) -> None:
    receiver = await receivers()
    non_ascii = sample_event(label="Mise à jour")

    async with HttpDeliverer() as http:
        await http.deliver(NtfyTarget(receiver.url(), "alerts"), [non_ascii], channel_name=CHANNEL)

    [received] = receiver.requests
    assert received.headers["title"].startswith("=?UTF-8?B?")
    assert received.headers["title"].isascii()


async def test_a_2xx_reply_with_a_body_over_the_read_cap_is_still_a_success(
    receivers: StartReceiver,
) -> None:
    receiver = await receivers(status=200, reply_body=b"x" * LARGE_REPLY)

    await send(webhook(receiver))

    assert len(receiver.requests) == 1


async def test_the_environments_proxy_settings_are_ignored(
    receivers: StartReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = await receivers()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")

    await send(webhook(receiver))

    assert len(receiver.requests) == 1


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500, 503])
async def test_a_reply_outside_2xx_is_a_status_failure_with_its_number(
    receivers: StartReceiver, status: int
) -> None:
    receiver = await receivers(status=status)

    failure = await failure_of(webhook(receiver))

    assert failure.code == f"status_{status}"
    assert failure.message == f"The receiver answered with HTTP status {status}."


async def test_a_redirect_is_not_followed(receivers: StartReceiver) -> None:
    elsewhere = await receivers()
    receiver = await receivers(status=302, headers={"Location": elsewhere.url("/other")})

    failure = await failure_of(webhook(receiver))

    assert failure.code == "status_302"
    assert elsewhere.requests == []


async def test_a_receiver_that_does_not_answer_in_time_is_a_timeout(
    receivers: StartReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(delivery, "REQUEST_TIMEOUT", SHORT_TIMEOUT)
    receiver = await receivers(delay=SLOW_REPLY)

    failure = await failure_of(webhook(receiver))

    assert failure.code == "timeout"


async def test_a_port_nothing_listens_on_is_a_tcp_failure(permit_loopback: None) -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]

    failure = await failure_of(WebhookTarget(f"http://127.0.0.1:{port}/hook"))

    assert failure.code == "tcp"


async def test_a_name_that_does_not_resolve_is_a_dns_failure() -> None:
    async def unresolvable(host: str, port: int) -> Sequence[netcheck.AddrInfo]:
        raise socket.gaierror(socket.EAI_NONAME, "unknown")

    transport = policy_transport(verify=True, resolver=unresolvable)

    failure = await failure_of(
        WebhookTarget("https://hooks.example.test/hook"), transport=transport
    )

    assert failure.code == "dns"


async def test_a_plain_listener_answering_a_tls_hello_is_a_tls_failure(
    permit_loopback: None,
) -> None:
    server = await serve_not_tls(b"HTTP/1.1 400 Bad Request\r\n\r\n")
    try:
        failure = await failure_of(WebhookTarget(f"https://127.0.0.1:{server.port}/hook"))
    finally:
        await server.close()

    assert failure.code == "tls"


@pytest.mark.parametrize("identity", ["rogue_server", "wrong_name_server", "expired_server"])
async def test_a_certificate_the_system_does_not_trust_is_a_certificate_failure(
    receivers: StartReceiver, certificates: Certificates, identity: str
) -> None:
    receiver = await receivers(identity=getattr(certificates, identity))

    failure = await failure_of(WebhookTarget(receiver.url("/hook")))

    assert failure.code == "certificate"
    assert receiver.requests == []


async def test_a_receiver_over_tls_with_a_trusted_certificate_is_delivered_to(
    receivers: StartReceiver,
    certificates: Certificates,
    trusting: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery, "system_trust_context", lambda: trusting)
    receiver = await receivers(identity=certificates.server)

    await send(webhook(receiver))

    assert len(receiver.requests) == 1


async def test_an_internal_receiver_is_refused_with_the_setting_named_and_never_contacted(
    receivers: StartReceiver, address_policy: Callable[..., None]
) -> None:
    receiver = await receivers()
    address_policy()

    failure = await failure_of(webhook(receiver))

    assert failure.code == "address_refused"
    assert SETTING in failure.message
    assert receiver.requests == []


async def test_an_internal_receiver_is_delivered_to_once_the_permit_covers_it(
    receivers: StartReceiver, address_policy: Callable[..., None]
) -> None:
    receiver = await receivers()
    address_policy("127.0.0.0/8")

    await send(webhook(receiver))

    assert len(receiver.requests) == 1


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(WebhookTarget("ftp://hooks.example.test/x"), id="scheme"),
        pytest.param(WebhookTarget("https://user:pw@hooks.example.test/x"), id="user-info"),
        pytest.param(
            WebhookTarget(
                "https://hooks.example.test/x", header_name="Host", header_value=SecretStr("v")
            ),
            id="reserved-header",
        ),
        pytest.param(
            WebhookTarget("https://hooks.example.test/x", header_name="X-Only"),
            id="unpaired-header",
        ),
        pytest.param(NtfyTarget("https://ntfy.example.test", "bad topic"), id="topic"),
        pytest.param(NtfyTarget("https://ntfy.example.test?x=1", "alerts"), id="server-query"),
        pytest.param(
            GotifyTarget("https://gotify.example.test", SecretStr("with space")), id="token"
        ),
        pytest.param(DiscordTarget(SecretStr("not a url")), id="chat-url"),
    ],
)
async def test_a_target_the_client_cannot_use_is_refused_before_any_request(
    target: HttpTarget,
) -> None:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return accepted(request)

    failure = await failure_of(target, transport=httpx.MockTransport(record))

    assert failure.code == "invalid_target"
    assert requests == []


async def test_a_failure_names_the_receiver_nowhere_in_its_text(receivers: StartReceiver) -> None:
    receiver = await receivers(status=500, reply_body=HOOK_SECRET.encode())
    target = DiscordTarget(SecretStr(receiver.url(f"/{HOOK_SECRET}")))

    failure = await failure_of(target)

    shown = f"{failure} {failure!r} {failure.message} {failure.args}"
    assert HOOK_SECRET not in shown and str(receiver.port) not in shown


async def test_a_prepared_request_shows_neither_its_address_nor_its_headers() -> None:
    target = WebhookTarget(
        f"https://hooks.example.test/{HOOK_SECRET}",
        hmac_key=SecretStr(HMAC_KEY),
        header_name="X-Token",
        header_value=SecretStr(HEADER_VALUE),
    )

    request = prepare(target, [sample_event()], channel_name=CHANNEL, sent_at=EVENT_TIME)

    shown = repr(request)
    assert HOOK_SECRET not in shown and HMAC_KEY not in shown and HEADER_VALUE not in shown


async def test_deliveries_log_no_part_of_a_credential_address(
    log_stream: io.StringIO, receivers: StartReceiver
) -> None:
    secret_url = f"https://hooks.example.test/{HOOK_SECRET}"
    unused = socket.socket()
    unused.bind(("127.0.0.1", 0))
    refused_port = unused.getsockname()[1]
    unused.close()

    await send(DiscordTarget(SecretStr(secret_url)), transport=httpx.MockTransport(accepted))
    await failure_of(DiscordTarget(SecretStr(f"http://127.0.0.1:{refused_port}/{HOOK_SECRET}")))

    written = log_stream.getvalue()
    assert "notification_request_failed" in written
    assert HOOK_SECRET not in written


async def test_the_address_would_be_logged_if_the_http_client_logged_at_info(
    log_stream: io.StringIO, client_loggers_restored: None
) -> None:
    for name in PINNED_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)

    await send(
        DiscordTarget(SecretStr(f"https://hooks.example.test/{HOOK_SECRET}")),
        transport=httpx.MockTransport(accepted),
    )

    assert HOOK_SECRET in log_stream.getvalue()


@pytest.mark.parametrize(
    "url",
    ["http://hooks.example.test/x", "https://hooks.example.test:8443/x?a=b", "http://[::1]:9/x"],
)
def test_an_http_or_https_address_with_a_host_is_accepted(url: str) -> None:
    assert validate_endpoint_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://hooks.example.test/x",
        "https://",
        "https://user@hooks.example.test/x",
        "https://hooks.example.test/x#frag",
        "https://hooks.example.test/a b",
        "https://hooks.example.test/é",
        "https://hooks.example.test/" + "a" * delivery.MAX_URL_CHARS,
    ],
)
def test_anything_but_a_plain_http_address_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="URL"):
        validate_endpoint_url(url)


def test_a_server_address_may_not_carry_a_query() -> None:
    with pytest.raises(ValueError, match="URL"):
        validate_endpoint_url("https://ntfy.example.test/?a=b", allow_query=False)


@pytest.mark.parametrize("name", ["Host", "content-length", "Content-Type", "X-Dockmin-Signature"])
def test_a_secret_header_may_not_replace_one_the_client_sets(name: str) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        validate_secret_header(name, "value")


@pytest.mark.parametrize(
    ("name", "value"), [("bad name", "v"), ("X-Ok", " v"), ("X-Ok", "v\r\nX: y")]
)
def test_a_secret_header_with_a_bad_name_or_value_is_refused(name: str, value: str) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        validate_secret_header(name, value)


@pytest.mark.parametrize("token", ["", "with space", "té", "x" * (delivery.MAX_TOKEN_CHARS + 1)])
def test_a_token_that_is_not_visible_ascii_within_the_limit_is_refused(token: str) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        validate_token(token)


@pytest.mark.parametrize("topic", ["", "a b", "a/b", "té", "x" * 65])
def test_a_topic_outside_letters_digits_underscore_and_hyphen_is_refused(topic: str) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        validate_topic(topic)


def test_a_signature_is_sha256_hex_of_the_exact_body() -> None:
    body = b'{"a":1}'

    assert sign(SecretStr("k"), body) == (
        "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    )


def test_an_empty_signing_key_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        sign(SecretStr(""), b"{}")


def _chained(outer: BaseException, cause: BaseException) -> BaseException:
    outer.__cause__ = cause
    return outer


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            _chained(httpx.ConnectError("x"), netpolicy.AddressRefused("127.0.0.1")),
            "address_refused",
        ),
        (ssl.SSLCertVerificationError("bad certificate"), "certificate"),
        (ssl.SSLError("handshake"), "tls"),
        (socket.gaierror(socket.EAI_NONAME, "unknown"), "dns"),
        (httpx.ConnectError("name lookup failed"), "dns"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (TimeoutError(), "timeout"),
        (ConnectionRefusedError(), "tcp"),
        (httpx.ConnectError("refused"), "tcp"),
        (ValueError("unexpected"), "protocol"),
    ],
)
def test_a_transport_failure_is_reduced_to_a_product_code_by_its_cause_chain(
    error: BaseException, code: str
) -> None:
    assert classify_exception(error) == code


def test_the_text_for_a_code_is_fixed_and_an_unknown_code_has_none() -> None:
    assert message_for("address_refused") == netpolicy.REFUSAL_TEXT
    assert message_for("status_418").endswith("HTTP status 418.")
    with pytest.raises(ValueError, match="unknown"):
        message_for("status_999")
