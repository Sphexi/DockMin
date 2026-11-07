"""Tests for the plain listener's redirect application."""

from collections.abc import MutableMapping
from typing import Any

import pytest

from app.serving.redirect import HttpsRedirectApp
from app.web.security_headers import CONTENT_SECURITY_POLICIES
from tests.serving.asgi import call, http_scope

PUBLIC_PORT = 8443
DEFAULT_HTTPS = 443
PERMANENT_REDIRECT = 308
BAD_REQUEST = 400


def app_for(port: int = PUBLIC_PORT, origins: tuple[str, ...] = ()) -> HttpsRedirectApp:
    return HttpsRedirectApp(public_port=port, trusted_origins=origins)


@pytest.mark.parametrize(
    ("kwargs", "port", "location"),
    [
        pytest.param({}, PUBLIC_PORT, "https://dockmin.example.test:8443/", id="root"),
        pytest.param(
            {"path": "/containers", "query": b"page=2&q=a%20b"},
            PUBLIC_PORT,
            "https://dockmin.example.test:8443/containers?page=2&q=a%20b",
            id="path-and-query",
        ),
        pytest.param({}, DEFAULT_HTTPS, "https://dockmin.example.test/", id="default-port-omitted"),
        pytest.param(
            {"host": "Dockmin.Example.TEST:8000"},
            PUBLIC_PORT,
            "https://dockmin.example.test:8443/",
            id="host-lower-cased-and-its-port-replaced",
        ),
        pytest.param({"host": "[::1]:8000"}, PUBLIC_PORT, "https://[::1]:8443/", id="ipv6"),
        pytest.param({"host": "192.0.2.5"}, DEFAULT_HTTPS, "https://192.0.2.5/", id="ipv4"),
        pytest.param(
            {"path": "/a%2Fb", "raw_path": b"/a%2Fb"},
            PUBLIC_PORT,
            "https://dockmin.example.test:8443/a%2Fb",
            id="raw-path-kept",
        ),
        pytest.param(
            {"method": "HEAD"}, PUBLIC_PORT, "https://dockmin.example.test:8443/", id="head"
        ),
    ],
)
async def test_a_get_or_head_is_redirected_to_the_same_target_on_https(
    kwargs: dict[str, Any], port: int, location: str
) -> None:
    answer = await call(app_for(port), http_scope(**kwargs))

    assert answer.status == PERMANENT_REDIRECT
    assert answer.headers["location"] == location
    assert answer.headers["cache-control"] == "no-store"
    assert answer.body == b""


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
async def test_a_request_that_changes_state_is_refused_not_redirected(method: str) -> None:
    answer = await call(app_for(), http_scope(method=method), body=b"password=secret")

    assert answer.status == BAD_REQUEST
    assert "location" not in answer.headers
    assert b"secret" not in answer.body


@pytest.mark.parametrize(
    "host",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param("exa mple.test", id="space"),
        pytest.param("evil.test/path", id="slash"),
        pytest.param("evil.test@good.test", id="userinfo"),
        pytest.param("evil.test:99999", id="port-out-of-range"),
        pytest.param("evil.test:80a", id="port-not-numeric"),
        pytest.param("[::1", id="unterminated-bracket"),
        pytest.param("[zz]:80", id="not-an-ipv6-address"),
        pytest.param("-bad.test", id="label-starts-with-hyphen"),
        pytest.param("a" * 64 + ".test", id="label-too-long"),
        pytest.param("a." * 130 + "test", id="name-too-long"),
        pytest.param("evil.test\r\nX-Injected: 1", id="header-injection"),
    ],
)
async def test_a_host_header_that_is_not_a_plain_host_is_refused(host: str | None) -> None:
    scope = http_scope(host=None)
    if host is not None:
        scope["headers"] = [(b"host", host.encode("latin-1"))]

    answer = await call(app_for(), scope)

    assert answer.status == BAD_REQUEST
    assert "location" not in answer.headers


async def test_two_host_headers_are_refused() -> None:
    scope = http_scope(host=None, extra_headers=((b"host", b"a.test"), (b"host", b"b.test")))

    answer = await call(app_for(), scope)

    assert answer.status == BAD_REQUEST


@pytest.mark.parametrize(
    ("target", "query"),
    [
        pytest.param(b"/with space", b"", id="space-in-path"),
        pytest.param(b"relative", b"", id="not-absolute"),
        pytest.param(b"/ok", b"a=\r\nX: 1", id="control-in-query"),
    ],
)
async def test_a_target_that_could_rewrite_the_location_is_refused(
    target: bytes, query: bytes
) -> None:
    answer = await call(app_for(), http_scope(raw_path=target, path="/x", query=query))

    assert answer.status == BAD_REQUEST


@pytest.mark.parametrize(
    ("origins", "host", "status"),
    [
        pytest.param(
            ("https://dockmin.example.test",),
            "dockmin.example.test",
            PERMANENT_REDIRECT,
            id="listed",
        ),
        pytest.param(
            ("https://dockmin.example.test:8443",),
            "DOCKMIN.example.test:80",
            PERMANENT_REDIRECT,
            id="listed-by-host-only",
        ),
        pytest.param(
            ("https://dockmin.example.test",), "other.example.test", BAD_REQUEST, id="not-listed"
        ),
        pytest.param(
            ("http://dockmin.example.test",),
            "other.example.test",
            PERMANENT_REDIRECT,
            id="http-origins-restrict-nothing",
        ),
        pytest.param((), "anything.example.test", PERMANENT_REDIRECT, id="none-configured"),
    ],
)
async def test_hosts_are_limited_to_the_trusted_origins_when_any_are_configured(
    origins: tuple[str, ...], host: str, status: int
) -> None:
    answer = await call(app_for(origins=origins), http_scope(host=host))

    assert answer.status == status


async def test_every_response_carries_the_security_headers_but_never_hsts() -> None:
    redirected = await call(app_for(), http_scope())
    refused = await call(app_for(), http_scope(method="POST"))

    for answer in (redirected, refused):
        assert answer.headers["content-security-policy"] == CONTENT_SECURITY_POLICIES["default"]
        assert answer.headers["x-content-type-options"] == "nosniff"
        assert answer.headers["x-frame-options"] == "DENY"
        assert answer.headers["referrer-policy"] == "same-origin"
        assert "strict-transport-security" not in answer.headers


async def test_a_websocket_handshake_is_closed_with_a_policy_violation() -> None:
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await app_for()({"type": "websocket", "path": "/ws", "headers": []}, receive, send)

    assert sent == [{"type": "websocket.close", "code": 1008}]
