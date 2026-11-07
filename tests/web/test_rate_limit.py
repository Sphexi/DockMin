"""The per-address limit on requests that resolve to no signed-in user."""

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette.requests import HTTPConnection
from starlette.types import Message

from app.config import Settings, load_settings
from app.paths import HEALTH_PATH, READY_PATH
from app.tls import TLS_LISTENER_SCOPE_KEY, served_over_tls
from app.web.rate_limit import (
    ANONYMOUS_BURST,
    ANONYMOUS_REFILL_PER_SECOND,
    MAX_TRACKED_ADDRESSES,
    AddressRateLimiter,
    bucket_key,
    unenforced_listener,
)
from tests.auth_support import ADMIN_SESSION_TOKEN, Browsers
from tests.serving.asgi import call, http_scope
from tests.tls_support import write_server_files

TOO_MANY = 429
FORBIDDEN = 403
OK = 200
ADDRESS = "198.51.100.7"
OTHER_ADDRESS = "198.51.100.8"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("address", "key"),
    [
        pytest.param("198.51.100.7", "198.51.100.7", id="v4"),
        pytest.param("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64", id="v6-by-its-64"),
        pytest.param("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64", id="v6-same-64"),
        pytest.param("2001:db8:1:3::1", "2001:db8:1:3::/64", id="v6-next-64"),
        pytest.param("::ffff:198.51.100.7", "198.51.100.7", id="v4-mapped"),
        pytest.param("unknown", "unknown", id="not-an-address"),
    ],
)
def test_an_address_is_counted_under_its_key(address: str, key: str) -> None:
    assert bucket_key(address) == key


def test_a_burst_is_admitted_and_the_next_request_is_told_how_long_to_wait() -> None:
    limiter = AddressRateLimiter(clock=Clock())

    admitted = [limiter.take(ADDRESS) for _ in range(ANONYMOUS_BURST)]
    wait = limiter.take(ADDRESS)

    assert admitted == [None] * ANONYMOUS_BURST
    assert wait == pytest.approx(1.0 / ANONYMOUS_REFILL_PER_SECOND)


def test_one_token_comes_back_each_second_and_no_more_than_the_burst_accumulates() -> None:
    clock = Clock()
    limiter = AddressRateLimiter(clock=clock)
    for _ in range(ANONYMOUS_BURST):
        limiter.take(ADDRESS)

    clock.now += 1.0
    after_one_second = [limiter.take(ADDRESS), limiter.take(ADDRESS)]
    clock.now += 10 * ANONYMOUS_BURST
    after_a_long_wait = [limiter.take(ADDRESS) for _ in range(ANONYMOUS_BURST + 1)]

    assert after_one_second[0] is None and after_one_second[1] is not None
    assert after_a_long_wait[:ANONYMOUS_BURST] == [None] * ANONYMOUS_BURST
    assert after_a_long_wait[-1] is not None


def test_a_wait_shrinks_as_time_passes() -> None:
    clock = Clock()
    limiter = AddressRateLimiter(clock=clock)
    for _ in range(ANONYMOUS_BURST):
        limiter.take(ADDRESS)
    first = limiter.take(ADDRESS)

    clock.now += 0.5
    second = limiter.take(ADDRESS)

    assert first is not None and second is not None
    assert second == pytest.approx(first - 0.5)


def test_addresses_are_counted_separately_but_one_v6_prefix_shares_a_bucket() -> None:
    limiter = AddressRateLimiter(clock=Clock())
    for _ in range(ANONYMOUS_BURST):
        limiter.take(ADDRESS)

    assert limiter.take(ADDRESS) is not None
    assert limiter.take(OTHER_ADDRESS) is None
    for number in range(ANONYMOUS_BURST):
        limiter.take(f"2001:db8:9:9::{number + 1:x}")
    assert limiter.take("2001:db8:9:9::ffff") is not None


def test_the_number_of_tracked_addresses_is_bounded_and_the_oldest_is_forgotten() -> None:
    limiter = AddressRateLimiter(clock=Clock())
    for _ in range(ANONYMOUS_BURST):
        limiter.take(ADDRESS)
    assert limiter.take(ADDRESS) is not None

    for number in range(MAX_TRACKED_ADDRESSES):
        limiter.take(f"10.{number // 65536}.{number // 256 % 256}.{number % 256}")

    assert limiter.take(ADDRESS) is None


def test_a_refusal_may_be_logged_once_a_minute() -> None:
    clock = Clock()
    limiter = AddressRateLimiter(clock=clock)

    first, immediately = limiter.log_due(), limiter.log_due()
    clock.now += 61
    later = limiter.log_due()

    assert (first, immediately, later) == (True, False, True)


def test_only_the_listener_mark_makes_a_request_count_as_arriving_over_tls() -> None:
    marked = HTTPConnection({"type": "http", TLS_LISTENER_SCOPE_KEY: True, "headers": []})
    forwarded = HTTPConnection(
        {"type": "http", "headers": [(b"x-forwarded-proto", b"https")], "scheme": "https"}
    )

    assert served_over_tls(marked) is True
    assert served_over_tls(forwarded) is False


@pytest.fixture
def settings_for(env: dict[str, str], tmp_path: Path) -> Any:
    def build(**overrides: str) -> Settings:
        extra: dict[str, str] = {}
        if "DOCKMIN_HTTP_MODE" in overrides or overrides.get("tls"):
            pair = write_server_files(tmp_path)
            extra = {"DOCKMIN_TLS_CERT_FILE": str(pair.cert), "DOCKMIN_TLS_KEY_FILE": str(pair.key)}
        overrides.pop("tls", None)
        return load_settings(env | extra | overrides)

    return build


@pytest.mark.parametrize(
    ("overrides", "listener"),
    [
        pytest.param({}, "http", id="plain-listener-serving-without-proxies"),
        pytest.param({"DOCKMIN_TRUSTED_PROXIES": "10.0.0.0/8"}, None, id="behind-a-proxy"),
        pytest.param({"tls": "1"}, None, id="tls-with-a-redirecting-plain-listener"),
        pytest.param(
            {"tls": "1", "DOCKMIN_HTTP_MODE": "serve"},
            "http",
            id="tls-and-a-serving-plain-listener",
        ),
        pytest.param({"tls": "1", "DOCKMIN_HTTP_MODE": "off"}, None, id="tls-only"),
        pytest.param(
            {"tls": "1", "DOCKMIN_HTTP_MODE": "serve", "DOCKMIN_TRUSTED_PROXIES": "*"},
            None,
            id="serving-plain-listener-behind-proxies",
        ),
    ],
)
def test_a_listener_that_sees_no_per_client_address_is_named(
    settings_for: Any, overrides: dict[str, str], listener: str | None
) -> None:
    assert unenforced_listener(settings_for(**overrides)) == listener


def tls_scope(path: str = "/login", **extra: Any) -> dict[str, Any]:
    options: dict[str, Any] = {TLS_LISTENER_SCOPE_KEY: True, **extra}
    return http_scope(path=path, host="testserver", **options)


@pytest.fixture
def frozen_limiter(started_app: FastAPI) -> AddressRateLimiter:
    limiter = AddressRateLimiter(clock=Clock())
    started_app.state.rate_limiter = limiter
    return limiter


@pytest.mark.usefixtures("frozen_limiter")
async def test_anonymous_requests_beyond_the_burst_are_refused_with_retry_after(
    started_app: FastAPI,
) -> None:
    answers = [await call(started_app, tls_scope()) for _ in range(ANONYMOUS_BURST + 2)]

    assert {a.status for a in answers[:ANONYMOUS_BURST]} == {OK}
    refused = answers[-1]
    assert refused.status == TOO_MANY
    assert int(refused.headers["retry-after"]) >= 1
    assert refused.headers["x-content-type-options"] == "nosniff"


@pytest.mark.usefixtures("frozen_limiter")
async def test_requests_failing_the_form_token_check_are_counted_and_then_refused(
    started_app: FastAPI,
) -> None:
    def forged() -> dict[str, Any]:
        return tls_scope(
            extra_headers=((b"content-type", b"application/x-www-form-urlencoded"),),
            method="POST",
        )

    answers = [
        await call(started_app, forged(), body=b"username=a&password=b")
        for _ in range(ANONYMOUS_BURST + 1)
    ]

    assert {a.status for a in answers[:ANONYMOUS_BURST]} == {FORBIDDEN}
    assert answers[-1].status == TOO_MANY


async def test_signed_in_requests_are_not_counted(
    started_app: FastAPI, browsers: Browsers, frozen_limiter: AddressRateLimiter
) -> None:
    admin = browsers.open(session_token=ADMIN_SESSION_TOKEN)
    for _ in range(ANONYMOUS_BURST):
        frozen_limiter.take("127.0.0.1")

    responses = [await admin.get("/users") for _ in range(5)]

    assert {r.status_code for r in responses} == {OK}


@pytest.mark.parametrize("path", [HEALTH_PATH, READY_PATH])
async def test_the_probe_endpoints_are_exempt_however_many_times_they_are_called(
    started_app: FastAPI, path: str
) -> None:
    answers = [await call(started_app, tls_scope(path=path)) for _ in range(ANONYMOUS_BURST + 5)]

    assert {a.status for a in answers} == {OK}


@pytest.mark.usefixtures("frozen_limiter")
async def test_nothing_is_refused_without_per_client_addresses_but_the_excess_is_logged(
    started_app: FastAPI, anonymous_client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    responses = [await anonymous_client.get("/login") for _ in range(ANONYMOUS_BURST + 3)]

    assert {r.status_code for r in responses} == {OK}
    excess = [r for r in caplog.records if r.message == "rate_limit_exceeded"]
    assert len(excess) == 1 and excess[0].enforced is False  # type: ignore[attr-defined]


@pytest.mark.usefixtures("frozen_limiter")
async def test_behind_trusted_proxies_the_limit_applies_without_the_listener_mark(
    started_app: FastAPI, anonymous_client: AsyncClient
) -> None:
    started_app.state.settings = started_app.state.settings.model_copy(
        update={"trusted_proxies": ["127.0.0.0/8", "203.0.113.0/24"]}
    )

    responses = [await anonymous_client.get("/login") for _ in range(ANONYMOUS_BURST + 1)]

    assert responses[-1].status_code == TOO_MANY


async def test_a_websocket_handshake_from_an_exhausted_address_is_closed_before_it_is_accepted(
    started_app: FastAPI, frozen_limiter: AddressRateLimiter
) -> None:
    for _ in range(ANONYMOUS_BURST):
        frozen_limiter.take("203.0.113.9")
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "websocket",
        "path": "/environments/1/containers/c/terminal/ws",
        "raw_path": b"/environments/1/containers/c/terminal/ws",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "scheme": "wss",
        "client": ("203.0.113.9", 50000),
        "server": ("testserver", 443),
        "subprotocols": [],
        TLS_LISTENER_SCOPE_KEY: True,
    }
    await started_app(scope, receive, send)

    assert not any(m["type"] == "websocket.accept" for m in sent)
    assert sent[-1]["type"] == "websocket.close"
    assert sent[-1]["code"] == 1008
