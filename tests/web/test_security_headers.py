"""Security headers: the policy strings, where each header is set, and every route and status."""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from starlette.responses import PlainTextResponse
from starlette.types import Message, Receive, Scope, Send

from app.selfcheck import StartupCheckFailed
from app.tls import TLS_LISTENER_SCOPE_KEY, TLS_SELF_SIGNED_SCOPE_KEY
from app.web.security_headers import (
    CONTENT_SECURITY_POLICIES,
    HSTS_VALUE,
    POLICY_STATE_KEY,
    STARTUP_FAILURE_ATTRIBUTE,
    DockminApp,
    SecurityHeadersMiddleware,
)
from tests.auth_support import ADMIN_SESSION_TOKEN, Browsers, csrf_form
from tests.conftest import DEVELOPMENT_CHECKS_VAR
from tests.container_support import DB
from tests.docker_engine.container_engine import ContainerEngine
from tests.web.environment_support import World
from tests.web.helpers import inline_code_in
from tests.web.route_support import fill, get_route_paths

DEFAULT_POLICY = CONTENT_SECURITY_POLICIES["default"]
TERMINAL_POLICY = CONTENT_SECURITY_POLICIES["terminal"]
MIN_GET_ROUTES = 100
REQUEST_SECONDS = 20
BASE_HTTPS = "https://testserver"
BASE_HTTP = "http://testserver"
SEE_OTHER = 303
LIMIT_BEYOND_BODY = str(10 * 1024 * 1024)


@pytest.fixture
def env(env: dict[str, str]) -> dict[str, str]:
    """The production setting: the interface kit is not served."""
    return env | {DEVELOPMENT_CHECKS_VAR: "false"}


@pytest.fixture
def app(private_app: FastAPI) -> FastAPI:
    """An application of its own: it is built without the kit, and a test adds a route to it."""
    return private_app


def assert_hardened(
    response: Response, *, policy: str = DEFAULT_POLICY, https: bool = True
) -> None:
    where = f"{response.request.method} {response.request.url.path} -> {response.status_code}"
    assert response.headers.get("content-security-policy") == policy, where
    assert response.headers.get("x-content-type-options") == "nosniff", where
    assert response.headers.get("referrer-policy") == "same-origin", where
    assert response.headers.get("x-frame-options") == "DENY", where
    assert response.headers.get("strict-transport-security") == (HSTS_VALUE if https else None), (
        where
    )


@pytest.mark.parametrize("name", ["default", "terminal"])
def test_each_policy_denies_by_default_and_allows_only_its_own_origin(name: str) -> None:
    directives = dict(  # type: ignore[var-annotated]
        item.strip().split(" ", 1)
        for item in CONTENT_SECURITY_POLICIES[name].split(";")  # type: ignore[index,arg-type]
    )

    assert directives["default-src"] == "'none'"
    assert directives["script-src"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["base-uri"] == "'none'"
    assert directives["object-src"] == "'none'"
    assert directives["form-action"] == "'self'"
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICIES[name]  # type: ignore[index]


def test_only_the_terminal_policy_allows_inline_style_elements() -> None:
    assert "'unsafe-inline'" not in DEFAULT_POLICY
    assert f"{DEFAULT_POLICY}; style-src-elem 'self' 'unsafe-inline'" == TERMINAL_POLICY


async def ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("ok", headers={"X-Frame-Options": "SAMEORIGIN"})(scope, receive, send)


async def run(app: Any, scope: dict[str, Any]) -> Message:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent[0]


def plain_scope(**extra: Any) -> dict[str, Any]:
    return {"type": "http", "method": "GET", "path": "/", "headers": [], "scheme": "http", **extra}


@pytest.mark.parametrize(
    ("scheme", "hsts"),
    [("http", False), ("https", True), ("wss", False)],
)
async def test_strict_transport_security_is_sent_only_when_the_request_arrived_over_https(
    scheme: str, hsts: bool
) -> None:
    start = await run(SecurityHeadersMiddleware(ok_app), plain_scope(scheme=scheme))

    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    assert ("strict-transport-security" in headers) is hsts


@pytest.mark.parametrize(
    ("scope_extra", "hsts"),
    [
        pytest.param({TLS_SELF_SIGNED_SCOPE_KEY: True}, False, id="self-signed-listener"),
        pytest.param(
            {TLS_LISTENER_SCOPE_KEY: True, TLS_SELF_SIGNED_SCOPE_KEY: True},
            False,
            id="self-signed-listener-marked-as-tls",
        ),
        pytest.param({TLS_LISTENER_SCOPE_KEY: True}, True, id="supplied-certificate-listener"),
        pytest.param({TLS_SELF_SIGNED_SCOPE_KEY: False}, True, id="mark-false"),
        pytest.param({TLS_SELF_SIGNED_SCOPE_KEY: "yes"}, True, id="mark-not-exactly-true"),
        pytest.param({}, True, id="behind-a-proxy-that-reports-https"),
    ],
)
async def test_strict_transport_security_is_withheld_only_from_the_self_signed_listener(
    scope_extra: dict[str, Any], hsts: bool
) -> None:
    start = await run(SecurityHeadersMiddleware(ok_app), plain_scope(scheme="https", **scope_extra))

    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    assert ("strict-transport-security" in headers) is hsts
    assert headers["x-content-type-options"] == "nosniff"


async def test_a_header_the_route_set_is_replaced_not_duplicated() -> None:
    start = await run(SecurityHeadersMiddleware(ok_app), plain_scope())

    frame = [v.decode() for k, v in start["headers"] if k.lower() == b"x-frame-options"]
    assert frame == ["DENY"]


async def test_the_policy_a_request_selected_is_the_one_sent() -> None:
    start = await run(
        SecurityHeadersMiddleware(ok_app),
        plain_scope(state={POLICY_STATE_KEY: "terminal"}),
    )

    csp = next(v.decode() for k, v in start["headers"] if k == b"content-security-policy")
    assert csp == TERMINAL_POLICY


async def test_a_websocket_scope_passes_through_untouched() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    await SecurityHeadersMiddleware(inner)({"type": "websocket"}, None, None)  # type: ignore[arg-type]

    assert seen == ["websocket"]


def raising_app() -> DockminApp:
    application = DockminApp(docs_url=None, redoc_url=None, openapi_url=None)

    @application.get("/boom")
    async def boom() -> None:
        raise RuntimeError("internal detail")

    @application.get("/fine")
    async def fine() -> dict[str, str]:
        return {"ok": "yes"}

    return application


@pytest.mark.parametrize(
    ("path", "status"),
    [("/fine", 200), ("/missing", 404), ("/boom", 500)],
)
async def test_the_application_class_stamps_the_headers_outside_the_error_layer(
    path: str, status: int
) -> None:
    transport = ASGITransport(app=raising_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url=BASE_HTTPS) as client:
        response = await client.get(path)

    assert response.status_code == status
    assert_hardened(response, https=True)


async def drive_lifespan(application: DockminApp) -> list[Message]:
    sent: list[Message] = []
    inbox = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def receive() -> Message:
        return inbox.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    with contextlib.suppress(RuntimeError):
        await application({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    return sent


def failing_lifespan(error: Exception, *, record: bool) -> DockminApp:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(application: Any) -> AsyncIterator[None]:
        if record and isinstance(error, StartupCheckFailed):
            setattr(application.state, STARTUP_FAILURE_ATTRIBUTE, error)
        raise error
        yield

    return DockminApp(lifespan=lifespan)


async def test_a_failed_startup_check_is_reported_to_the_server_without_a_message() -> None:
    failure = StartupCheckFailed("data_directory", "problem", "remedy")

    sent = await drive_lifespan(failing_lifespan(failure, record=True))

    failed = next(m for m in sent if m["type"] == "lifespan.startup.failed")
    assert failed["message"] == ""


async def test_any_other_startup_failure_keeps_its_message_for_the_server_to_print() -> None:
    sent = await drive_lifespan(failing_lifespan(RuntimeError("database exploded"), record=False))

    failed = next(m for m in sent if m["type"] == "lifespan.startup.failed")
    assert "database exploded" in failed["message"]


async def test_a_startup_failure_recorded_once_is_not_hidden_on_the_next_start() -> None:
    application = failing_lifespan(StartupCheckFailed("database", "problem", "remedy"), record=True)
    await drive_lifespan(application)

    assert not hasattr(application.state, STARTUP_FAILURE_ATTRIBUTE)


async def error_statuses(app: FastAPI, browsers: Browsers) -> dict[str, Response]:
    app.add_api_route("/test-only/boom", _explode)
    admin = browsers.open(session_token=ADMIN_SESSION_TOKEN)
    anonymous = browsers.open()
    oversize = {"Content-Length": LIMIT_BEYOND_BODY}
    return {
        "redirect": await anonymous.get("/users"),
        "not found": await admin.get("/missing"),
        "method not allowed": await admin.post("/api/health"),
        "unprocessable": await admin.get("/environments/not-a-number"),
        "too large": await anonymous.post(
            "/login",
            content=b"x",
            headers={"Content-Type": "application/x-www-form-urlencoded"} | oversize,
        ),
        "unauthorised": await anonymous.post(
            "/login", data=csrf_form(anonymous, username="nobody", password="wrong")
        ),
        "server error": await admin.get("/test-only/boom"),
        "docs": await admin.get("/docs"),
    }


async def _explode() -> None:
    raise RuntimeError("internal detail")


async def test_every_get_route_and_every_error_status_carries_every_header(
    world: World, browsers: Browsers
) -> None:
    app = world.app
    admin = browsers.open(session_token=ADMIN_SESSION_TOKEN)
    paths = get_route_paths(app)
    openapi_paths = {p for p, operations in app.openapi()["paths"].items() if "get" in operations}
    environments = [world.local_id, world.remote_id, world.down_id]
    targets = [
        fill(path, environment_id=environment_id, group_id=world.group_id, tag_id=world.tag_id)
        for path in paths
        for environment_id in (environments if "{environment_id}" in path else [1])
    ]
    inline: dict[str, list[str]] = {}
    statuses: set[int] = set()

    for target in targets:
        response = await admin.get(target)
        assert_hardened(response)
        statuses.add(response.status_code)
        if response.headers.get("content-type", "").startswith("text/html"):
            found = inline_code_in(response.text)
            if found:
                inline[target] = found
    static = await admin.get("/static/css/components.css")
    errors = await error_statuses(app, browsers)
    for response in [static, *errors.values()]:
        assert_hardened(response)

    assert len(paths) >= MIN_GET_ROUTES
    assert openapi_paths <= set(paths)
    assert {200, 404} <= statuses
    assert inline == {}
    assert static.status_code == 200
    assert {name: r.status_code for name, r in errors.items()} == {
        "redirect": SEE_OTHER,
        "not found": 404,
        "method not allowed": 405,
        "unprocessable": 422,
        "too large": 413,
        "unauthorised": 401,
        "server error": 500,
        "docs": 404,
    }


async def test_strict_transport_security_follows_the_request_scheme_on_a_page_a_file_and_an_error(
    browsers: Browsers,
) -> None:
    secure = browsers.open(session_token=ADMIN_SESSION_TOKEN, base_url=BASE_HTTPS)
    plain = browsers.open(session_token=ADMIN_SESSION_TOKEN, base_url=BASE_HTTP)
    targets = ("/users", "/static/css/components.css", "/missing")

    secure_responses = [await secure.get(target) for target in targets]
    plain_responses = [await plain.get(target) for target in targets]

    for response in secure_responses:
        assert_hardened(response, https=True)
    for response in plain_responses:
        assert_hardened(response, https=False)


async def test_the_documentation_routes_and_the_schema_endpoint_are_not_served(
    browsers: Browsers,
) -> None:
    admin = browsers.open(session_token=ADMIN_SESSION_TOKEN)

    responses = [
        await admin.get(path)
        for path in ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json")
    ]

    assert [r.status_code for r in responses] == [404] * 4
    assert browsers._app.openapi_url is None
    assert "paths" in browsers._app.openapi()


async def test_the_interface_kit_is_not_served_without_development_checks(
    browsers: Browsers,
) -> None:
    admin = browsers.open(session_token=ADMIN_SESSION_TOKEN)

    responses = [await admin.get(path) for path in ("/ui-kit", "/ui-kit/progress")]

    assert [r.status_code for r in responses] == [404, 404]
    assert not any(path.startswith("/ui-kit") for path in get_route_paths(browsers._app))


async def test_only_the_terminal_page_carries_the_terminal_policy(
    world: World, container_host: ContainerEngine, browsers: Browsers
) -> None:
    admin = browsers.open(session_token=ADMIN_SESSION_TOKEN)
    base = f"/environments/{world.remote_id}/containers/{DB}"

    terminal = await admin.post(
        f"{base}/terminal", data=csrf_form(admin, command="/bin/sh", rows="30", cols="100")
    )
    detail = await admin.get(base)

    assert terminal.status_code == 200 and detail.status_code == 200
    assert_hardened(terminal, policy=TERMINAL_POLICY)
    assert_hardened(detail)
