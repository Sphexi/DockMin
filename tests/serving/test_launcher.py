"""Tests for the launcher: which listeners it describes and how they are marked."""

import asyncio
import signal
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from starlette.responses import PlainTextResponse
from starlette.types import Message, Receive, Scope, Send

from app.config import TLS_SELF_SIGNED_VAR, Settings, load_settings
from app.main import create_app
from app.selfcheck import StartupCheckFailed
from app.serving import launcher
from app.serving.redirect import HttpsRedirectApp
from app.tls import (
    TLS_LISTENER_SCOPE_KEY,
    TLS_SELF_SIGNED_SCOPE_KEY,
    load_server_tls,
    read_certificate,
)
from app.tls_self_signed import ensure_self_signed
from app.web.security_headers import HSTS_VALUE, SecurityHeadersMiddleware
from tests.tls_support import write_server_files

WS_FRAME_LIMIT = 65_536
GRACEFUL_SECONDS = 10
LOOPBACK = "127.0.0.1"
HTTP_PORT = 18080
HTTPS_PORT = 18443


def settings_with(env: dict[str, str], tmp_path: Path, **overrides: str) -> Settings:
    pair = write_server_files(tmp_path)
    tls = {"DOCKMIN_TLS_CERT_FILE": str(pair.cert), "DOCKMIN_TLS_KEY_FILE": str(pair.key)}
    return load_settings(
        env
        | {"DOCKMIN_PORT": str(HTTP_PORT), "DOCKMIN_HTTPS_PORT": str(HTTPS_PORT)}
        | tls
        | overrides
    )


async def recording_app(scope: Scope, receive: Receive, send: Send) -> None:
    recording_app.scopes.append(scope)  # type: ignore[attr-defined]


recording_app.scopes = []  # type: ignore[attr-defined]


def context_for(settings: Settings) -> ssl.SSLContext:
    assert settings.tls_cert_file is not None and settings.tls_key_file is not None
    return load_server_tls(settings.tls_cert_file, settings.tls_key_file)[0]


def test_without_a_certificate_one_plain_listener_serves_the_application(
    env: dict[str, str],
) -> None:
    settings = load_settings(env | {"DOCKMIN_PORT": str(HTTP_PORT)})

    [only] = launcher.server_configs(settings, recording_app)

    assert only.port == HTTP_PORT
    assert only.app is recording_app
    assert only.ssl_context_factory is None


def test_with_a_certificate_the_https_listener_comes_first_and_the_plain_one_redirects(
    env: dict[str, str], tmp_path: Path
) -> None:
    settings = settings_with(env, tmp_path)

    primary, plain = launcher.server_configs(settings, recording_app, context_for(settings))

    assert primary.port == HTTPS_PORT and primary.ssl_context_factory is not None
    assert plain.port == HTTP_PORT and plain.ssl_context_factory is None
    assert isinstance(plain.app, HttpsRedirectApp)
    assert (primary.lifespan, plain.lifespan) == ("auto", "off")
    assert plain.proxy_headers is False


def test_serve_mode_puts_the_application_on_both_listeners_with_the_lifespan_on_one(
    env: dict[str, str], tmp_path: Path
) -> None:
    settings = settings_with(env, tmp_path, DOCKMIN_HTTP_MODE="serve")

    primary, plain = launcher.server_configs(settings, recording_app, context_for(settings))

    assert plain.app is recording_app
    assert (primary.lifespan, plain.lifespan) == ("auto", "off")


def test_off_mode_describes_only_the_https_listener(env: dict[str, str], tmp_path: Path) -> None:
    settings = settings_with(env, tmp_path, DOCKMIN_HTTP_MODE="off")

    configs = launcher.server_configs(settings, recording_app, context_for(settings))

    assert [config.port for config in configs] == [HTTPS_PORT]


@pytest.mark.parametrize("tls", [False, True])
def test_every_listener_has_the_fixed_hardening_options(
    env: dict[str, str], tmp_path: Path, tls: bool
) -> None:
    settings = (
        settings_with(env, tmp_path, DOCKMIN_HTTP_MODE="serve") if tls else load_settings(env)
    )
    context = context_for(settings) if tls else None

    configs = launcher.server_configs(settings, recording_app, context)

    for config in configs:
        assert config.ws_max_size == WS_FRAME_LIMIT
        assert config.server_header is False
        assert config.access_log is False
        assert config.log_config is None
        assert config.timeout_graceful_shutdown == GRACEFUL_SECONDS
        assert config.log_level != "trace"


def test_without_trusted_proxies_forwarded_headers_are_not_believed(env: dict[str, str]) -> None:
    settings = load_settings(env | {"DOCKMIN_TRUSTED_PROXIES": ""})

    [config] = launcher.server_configs(settings, recording_app)

    assert config.proxy_headers is False
    assert config.forwarded_allow_ips == []


@pytest.mark.parametrize(
    ("proxies", "expected"),
    [
        pytest.param("10.0.0.0/24,192.0.2.7", ["10.0.0.0/24", "192.0.2.7/32"], id="listed"),
        pytest.param("*", "*", id="any"),
    ],
)
def test_the_trusted_proxies_are_handed_to_uvicorn_as_the_forwarded_addresses(
    env: dict[str, str], proxies: str, expected: Any
) -> None:
    settings = load_settings(env | {"DOCKMIN_TRUSTED_PROXIES": proxies})

    [config] = launcher.server_configs(settings, recording_app)

    assert config.proxy_headers is True
    assert config.forwarded_allow_ips == expected


def test_the_redirect_listener_never_believes_forwarded_headers(
    env: dict[str, str], tmp_path: Path
) -> None:
    settings = settings_with(env, tmp_path, DOCKMIN_TRUSTED_PROXIES="10.0.0.0/24")

    primary, plain = launcher.server_configs(settings, recording_app, context_for(settings))

    assert primary.proxy_headers is True
    assert plain.proxy_headers is False


async def test_only_the_https_listener_marks_a_connection_as_arriving_over_tls(
    env: dict[str, str], tmp_path: Path
) -> None:
    settings = settings_with(env, tmp_path, DOCKMIN_HTTP_MODE="serve")
    primary, plain = launcher.server_configs(settings, recording_app, context_for(settings))
    recording_app.scopes.clear()  # type: ignore[attr-defined]

    async def receive() -> dict[str, Any]:
        return {}

    async def send(message: dict[str, Any]) -> None:
        return None

    await cast(Callable[..., Awaitable[None]], primary.app)(
        {"type": "http", TLS_LISTENER_SCOPE_KEY: False}, receive, send
    )
    await cast(Callable[..., Awaitable[None]], plain.app)({"type": "http"}, receive, send)

    marked, unmarked = recording_app.scopes  # type: ignore[attr-defined]
    assert marked[TLS_LISTENER_SCOPE_KEY] is True
    assert TLS_SELF_SIGNED_SCOPE_KEY not in marked
    assert TLS_LISTENER_SCOPE_KEY not in unmarked


def self_signed_settings(env: dict[str, str], tmp_path: Path, **overrides: str) -> Settings:
    return load_settings(
        env
        | {
            TLS_SELF_SIGNED_VAR: "true",
            "DOCKMIN_DATA_DIR": str(tmp_path),
            "DOCKMIN_PORT": str(HTTP_PORT),
            "DOCKMIN_HTTPS_PORT": str(HTTPS_PORT),
        }
        | overrides
    )


def self_signed_context(tmp_path: Path) -> ssl.SSLContext:
    return ensure_self_signed(tmp_path / "server-tls", datetime.now(UTC)).tls[0]


async def ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("ok")(scope, receive, send)


async def response_headers(app: Any, **scope: Any) -> dict[str, str]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    await app({"type": "http", "method": "GET", "path": "/", "headers": [], **scope}, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    return {name.decode(): value.decode() for name, value in start["headers"]}


def test_with_a_self_signed_certificate_the_https_listener_leads_and_the_plain_one_redirects(
    env: dict[str, str], tmp_path: Path
) -> None:
    settings = self_signed_settings(env, tmp_path)

    primary, plain = launcher.server_configs(settings, recording_app, self_signed_context(tmp_path))

    assert primary.port == HTTPS_PORT and primary.ssl_context_factory is not None
    assert plain.port == HTTP_PORT and plain.ssl_context_factory is None
    assert isinstance(plain.app, HttpsRedirectApp)
    assert (primary.lifespan, plain.lifespan) == ("auto", "off")


async def test_only_the_self_signed_listener_carries_the_self_signed_mark(
    env: dict[str, str], tmp_path: Path
) -> None:
    settings = self_signed_settings(env, tmp_path, DOCKMIN_HTTP_MODE="serve")
    primary, plain = launcher.server_configs(settings, recording_app, self_signed_context(tmp_path))
    recording_app.scopes.clear()  # type: ignore[attr-defined]

    async def receive() -> dict[str, Any]:
        return {}

    async def send(message: dict[str, Any]) -> None:
        return None

    await cast(Callable[..., Awaitable[None]], primary.app)({"type": "http"}, receive, send)
    await cast(Callable[..., Awaitable[None]], plain.app)({"type": "http"}, receive, send)

    marked, unmarked = recording_app.scopes  # type: ignore[attr-defined]
    assert (marked[TLS_LISTENER_SCOPE_KEY], marked[TLS_SELF_SIGNED_SCOPE_KEY]) == (True, True)
    assert TLS_LISTENER_SCOPE_KEY not in unmarked and TLS_SELF_SIGNED_SCOPE_KEY not in unmarked


async def test_a_supplied_certificate_listener_is_marked_as_tls_but_not_as_self_signed(
    env: dict[str, str], tmp_path: Path
) -> None:
    settings = settings_with(env, tmp_path)
    [primary, _] = launcher.server_configs(settings, recording_app, context_for(settings))
    recording_app.scopes.clear()  # type: ignore[attr-defined]

    async def receive() -> dict[str, Any]:
        return {}

    async def send(message: dict[str, Any]) -> None:
        return None

    await cast(Callable[..., Awaitable[None]], primary.app)({"type": "http"}, receive, send)

    [scope] = recording_app.scopes  # type: ignore[attr-defined]
    assert scope[TLS_LISTENER_SCOPE_KEY] is True
    assert TLS_SELF_SIGNED_SCOPE_KEY not in scope


async def test_the_self_signed_listener_sends_no_hsts_header(
    env: dict[str, str], tmp_path: Path
) -> None:
    settings = self_signed_settings(env, tmp_path)
    [primary, _] = launcher.server_configs(
        settings, SecurityHeadersMiddleware(ok_app), self_signed_context(tmp_path)
    )

    headers = await response_headers(primary.app, scheme="https")

    assert "strict-transport-security" not in headers
    assert headers["x-frame-options"] == "DENY"


async def test_a_supplied_certificate_listener_sends_the_hsts_header(
    env: dict[str, str], tmp_path: Path
) -> None:
    settings = settings_with(env, tmp_path)
    [primary, _] = launcher.server_configs(
        settings, SecurityHeadersMiddleware(ok_app), context_for(settings)
    )

    headers = await response_headers(primary.app, scheme="https")

    assert headers["strict-transport-security"] == HSTS_VALUE


async def test_a_plain_listener_behind_a_trusted_proxy_that_reports_https_sends_the_hsts_header(
    env: dict[str, str], tmp_path: Path
) -> None:
    settings = self_signed_settings(
        env, tmp_path, DOCKMIN_HTTP_MODE="serve", DOCKMIN_TRUSTED_PROXIES="203.0.113.7"
    )
    [_, plain] = launcher.server_configs(
        settings, SecurityHeadersMiddleware(ok_app), self_signed_context(tmp_path)
    )

    headers = await response_headers(plain.app, scheme="https")

    assert headers["strict-transport-security"] == HSTS_VALUE


def test_a_self_signed_start_generates_the_pair_in_the_data_directory_and_then_reuses_it(
    env: dict[str, str], tmp_path: Path
) -> None:
    settings = self_signed_settings(env, tmp_path)

    first_tls, first = launcher._load_tls(settings)
    second_tls, second = launcher._load_tls(settings)

    assert first is not None and second is not None and first_tls is not None
    assert (first.created, first.reason) == (True, "missing")
    assert (second.created, second.reason) == (False, None)
    assert first.cert_file == tmp_path / "server-tls" / "cert.pem"
    assert second_tls is not None and second_tls[1] == first_tls[1]
    assert first_tls[1] == read_certificate(first.cert_file)


def test_supplied_files_are_served_and_no_certificate_directory_is_created(
    env: dict[str, str], tmp_path: Path
) -> None:
    settings = settings_with(env, tmp_path)

    tls, generated = launcher._load_tls(settings)

    assert generated is None and tls is not None
    assert tls[1] == read_certificate(settings.tls_cert_file)  # type: ignore[arg-type]
    assert not (settings.data_dir / "server-tls").exists()


def test_with_both_switched_off_no_certificate_is_loaded(env: dict[str, str]) -> None:
    assert launcher._load_tls(load_settings(env)) == (None, None)


def test_an_unusable_data_directory_is_reported_before_any_certificate_is_generated(
    env: dict[str, str], tmp_path: Path
) -> None:
    absent = tmp_path / "absent"
    settings = self_signed_settings(env, tmp_path, DOCKMIN_DATA_DIR=str(absent))

    with pytest.raises(StartupCheckFailed) as raised:
        launcher._load_tls(settings)

    assert raised.value.check == "data_directory"
    assert not absent.exists()


def test_a_certificate_directory_that_is_a_link_is_refused_with_the_data_directory_remedy(
    env: dict[str, str], tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "server-tls").symlink_to(elsewhere)
    settings = self_signed_settings(env, tmp_path)

    with pytest.raises(StartupCheckFailed) as raised:
        launcher._load_tls(settings)

    assert raised.value.check == "tls_material"
    assert raised.value.problem == (
        f"The certificate directory {tmp_path / 'server-tls'} is a symbolic link"
    )
    assert raised.value.remedy == (
        f"Make {tmp_path} writable by the user the process runs as (uid 10001 in the image), or "
        f"set {TLS_SELF_SIGNED_VAR}=false and supply a certificate or terminate TLS in front of "
        "dockmin"
    )
    assert list(elsewhere.iterdir()) == []


def test_the_generated_certificate_line_carries_the_fingerprint_expiry_and_whether_it_was_made(
    env: dict[str, str], tmp_path: Path, logged: Callable[[str], list[dict[str, Any]]]
) -> None:
    settings = self_signed_settings(env, tmp_path)
    tls, pair = launcher._load_tls(settings)
    assert tls is not None and pair is not None

    launcher._log_self_signed(pair, tls[1])

    [line] = logged("tls_certificate_self_signed")
    assert line["sha256_fingerprint"] == tls[1].sha256_fingerprint
    assert line["not_after"] == tls[1].not_after.isoformat()
    assert (line["generated"], line["reason"]) == (True, "missing")


@pytest.fixture
def bound_sockets() -> Iterator[tuple[socket.socket, socket.socket]]:
    """Return two sockets bound to loopback ports the system chose, held until the test ends."""
    with socket.socket() as https_socket, socket.socket() as http_socket:
        https_socket.bind((LOOPBACK, 0))
        http_socket.bind((LOOPBACK, 0))
        yield https_socket, http_socket


async def test_both_listeners_serve_one_application_with_headers_and_a_redirect(
    env: dict[str, str], tmp_path: Path, bound_sockets: tuple[socket.socket, socket.socket]
) -> None:
    https_socket, http_socket = bound_sockets
    https_port, http_port = https_socket.getsockname()[1], http_socket.getsockname()[1]
    settings = settings_with(
        env,
        tmp_path,
        DOCKMIN_HOST=LOOPBACK,
        DOCKMIN_PORT=str(http_port),
        DOCKMIN_HTTPS_PORT=str(https_port),
    )
    application = create_app(settings)
    tls_context, _ = load_server_tls(settings.tls_cert_file, settings.tls_key_file)  # type: ignore[arg-type]
    primary, plain = (
        launcher._Server(config)
        for config in launcher.server_configs(settings, application, tls_context)
    )
    primary_task = asyncio.create_task(primary.serve(sockets=[https_socket]))
    try:
        async with asyncio.timeout(30):
            while not primary.started:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
        plain_task = asyncio.create_task(plain.serve(sockets=[http_socket]))
        try:
            async with asyncio.timeout(30):
                while not plain.started:  # noqa: ASYNC110
                    await asyncio.sleep(0.05)
            stall_bounds = [
                listener.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT)
                for server in (primary, plain)
                for service in server.servers
                for listener in service.sockets
            ]
            async with httpx.AsyncClient(verify=False) as client:
                secure = await client.get(f"https://{LOOPBACK}:{https_port}/api/health")
                redirected = await client.get(
                    f"http://{LOOPBACK}:{http_port}/containers?x=1", follow_redirects=False
                )
        finally:
            plain.should_exit = True
            await plain_task
    finally:
        primary.should_exit = True
        await primary_task

    assert stall_bounds and set(stall_bounds) == {launcher.SOCKET_STALL_TIMEOUT_MS}
    assert secure.status_code == 200
    assert secure.headers["strict-transport-security"].startswith("max-age=")
    assert secure.headers["x-content-type-options"] == "nosniff"
    assert "server" not in secure.headers
    assert redirected.status_code == 308
    assert redirected.headers["location"] == f"https://{LOOPBACK}:{https_port}/containers?x=1"
    assert "strict-transport-security" not in redirected.headers


CRASH = "listener crashed"


class FakeListener:
    """Stands in for a bound listening socket and records when it is closed."""

    def __init__(self, owner: "FakeServer") -> None:
        self._owner = owner

    def close(self) -> None:
        self._owner.events.append(f"{self._owner.name}:closed")


class FakeServer:
    """Stands in for a uvicorn server and records when it is told to stop and when it ends."""

    events: list[str]
    ignore_stop: bool = False
    crash_server: str | None = None

    def __init__(self, config: Any) -> None:
        self.config = config
        self.name: str = config.name
        self.started = False
        self.force_exit = False
        self._should_exit = False

    @property
    def should_exit(self) -> bool:
        return self._should_exit

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self._should_exit = value
        self.events.append(f"{self.name}:told")

    async def serve(self) -> None:
        self.servers = [FakeListener(self)]
        self.started = True
        if self.name == self.crash_server:
            raise RuntimeError(CRASH)
        while not (self.force_exit or (self._should_exit and not self.ignore_stop)):  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        self.events.append(f"{self.name}:done")


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> list[FakeServer]:
    created: list[FakeServer] = []
    events: list[str] = []

    class Recording(FakeServer):
        def __init__(self, config: Any) -> None:
            super().__init__(config)
            created.append(self)

    Recording.events = events
    monkeypatch.setattr(launcher, "_Server", Recording)
    return created


def fake_configs() -> list[Any]:
    return [
        SimpleNamespace(name="primary", loaded=True),
        SimpleNamespace(name="secondary", loaded=True),
    ]


async def deliver_after_start(
    servers: list[FakeServer], supervising: "asyncio.Future[int]", *numbers: signal.Signals
) -> None:
    def ready() -> bool:
        return supervising.done() or (len(servers) == 2 and all(s.started for s in servers))

    async with asyncio.timeout(10):
        while not ready():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    for number in numbers:
        if supervising.done():
            return
        signal.raise_signal(number)
        await asyncio.sleep(0.02)


async def supervise_delivering(servers: list[FakeServer], *numbers: signal.Signals) -> int:
    supervising = asyncio.ensure_future(launcher._supervise(fake_configs()))
    sender = asyncio.create_task(deliver_after_start(servers, supervising, *numbers))
    async with asyncio.timeout(10):
        try:
            return await supervising
        finally:
            await sender


async def test_a_stop_request_closes_the_primary_listeners_then_ends_the_secondary_then_the_primary(
    fakes: list[FakeServer],
) -> None:
    status = await supervise_delivering(fakes, signal.SIGTERM)

    assert status == launcher.EXIT_OK
    assert fakes[0].events == [
        "primary:closed",
        "secondary:told",
        "secondary:done",
        "primary:told",
        "primary:done",
    ]


async def test_a_second_interrupt_forces_both_listeners_while_the_secondary_ignores_a_stop(
    fakes: list[FakeServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeServer, "ignore_stop", True)

    status = await supervise_delivering(fakes, signal.SIGTERM, signal.SIGINT)

    assert status == launcher.EXIT_OK
    assert [server.force_exit for server in fakes] == [True, True]
    assert fakes[0].events.index("secondary:done") < fakes[0].events.index("primary:told")


async def test_a_secondary_that_fails_still_has_the_primary_stopped_and_the_error_propagates(
    fakes: list[FakeServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeServer, "crash_server", "secondary")

    with pytest.raises(RuntimeError, match=CRASH):
        async with asyncio.timeout(10):
            await launcher._supervise(fake_configs())

    assert fakes[0].events == ["primary:closed", "secondary:told", "primary:told", "primary:done"]


def test_stopping_closes_every_listener_a_bound_server_holds() -> None:
    events: list[str] = []
    server = SimpleNamespace(name="primary", events=events)
    server.servers = [FakeListener(server), FakeListener(server)]  # type: ignore[arg-type]

    launcher._stop_accepting(server)  # type: ignore[arg-type]

    assert events == ["primary:closed", "primary:closed"]


def test_stopping_a_server_that_has_not_bound_its_listeners_does_nothing() -> None:
    unbound = SimpleNamespace()

    launcher._stop_accepting(unbound)  # type: ignore[arg-type]

    assert vars(unbound) == {}
