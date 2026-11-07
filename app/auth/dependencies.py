"""Request-time authentication: the setup gate, principal resolution, and the route guards."""

from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, WebSocketException
from starlette.requests import HTTPConnection

from app.auth.bearer import bearer_credentials, is_api_path, is_bearer_request
from app.auth.errors import RedirectRequired
from app.auth.login import PASSWORD_METHOD
from app.auth.platform_signin import JWT_METHOD, PlatformIdentity, PlatformSession
from app.auth.principal import Principal
from app.auth.services import (
    AuthRuntime,
    AuthServices,
    build_platform_login,
    build_services,
    build_session_service,
    build_token_service,
)
from app.auth.sessions import (
    SIGNED_OUT_COOKIE,
    clear_session_cookie,
    session_cookie_name,
    set_session_cookie,
)
from app.config import Settings, get_settings
from app.db.engine import Database, DatabaseClosed, Session, get_session
from app.db.repositories.user import UserRepository
from app.paths import FAVICON_PATH, HEALTH_PATH, READY_PATH
from app.services.activity import ActivityRecorder
from app.tls import served_over_tls
from app.web.csrf import attach_csrf_cookie, is_webhook_call, new_token
from app.web.queued_cookies import queue_cookie
from app.web.templating import is_htmx_request

LOGIN_PATH = "/login"
SETUP_PATH = "/setup"

# Reachable while no administrator exists: orchestrator probes, the setup form and the icon it uses.
SETUP_EXEMPT_PATHS = frozenset({HEALTH_PATH, READY_PATH, SETUP_PATH, FAVICON_PATH})
EXCHANGE_METHODS = frozenset({"GET", "HEAD"})
PASSWORD_SIGN_IN_DISABLED = "password_sign_in_disabled"


def get_runtime(connection: HTTPConnection) -> AuthRuntime:
    """Return the authentication state the application was constructed with."""
    runtime: AuthRuntime = connection.app.state.auth
    return runtime


async def get_activity(
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
) -> ActivityRecorder:
    """Provide the one activity recorder of this request's database session."""
    return ActivityRecorder(db, runtime.clock)


async def get_auth_services(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> AuthServices:
    """Provide the authentication services bound to this request's database session."""
    enforce_address = bool(settings.trusted_proxies) or served_over_tls(request)
    return build_services(db, settings, runtime, activity, enforce_address=enforce_address)


def remote_address(request: HTTPConnection) -> str:
    """Return the peer address, which is what uvicorn derives from trusted proxy headers.

    Behind a proxy it is the client's only when the proxy is listed in the trusted-proxy setting;
    otherwise it is the proxy's own address, shared by every client.
    """
    return request.client.host if request.client else "unknown"


def _database_of(connection: HTTPConnection) -> Database | None:
    database: Database | None = getattr(connection.app.state, "database", None)
    return database


async def gate_setup(connection: HTTPConnection) -> None:
    """Send every request to the setup form until an administrator exists.

    A WebSocket handshake is refused instead, since a redirect cannot answer an upgrade.
    """
    runtime = get_runtime(connection)
    if (
        runtime.setup.complete
        or connection.url.path in SETUP_EXEMPT_PATHS
        or is_webhook_call(connection)
    ):
        return
    database = _database_of(connection)
    if database is None:
        return
    try:
        async with database.session() as db:
            has_administrator = await UserRepository(db).count_administrators() > 0
    except DatabaseClosed:
        return
    if has_administrator:
        runtime.setup.complete = True
        runtime.setup.token = None
        return
    if not isinstance(connection, Request):
        raise WebSocketException(code=1008)
    if is_api_path(connection):
        raise HTTPException(status_code=503, detail="setup required")
    raise RedirectRequired(SETUP_PATH)


async def resolve_principal(connection: HTTPConnection) -> None:
    """Attach the signed-in principal, or None, to the connection state for every template to read.

    A bearer request is resolved from its token alone and never from a cookie, so the CSRF
    exemption it earns cannot be spent on a cookie-authenticated request. A WebSocket handshake
    is resolved from the session cookie only: a bearer token never opens one. While the database
    is closed or absent, as during a restore's swap, nobody is signed in.

    With access platform sign-in on, a page request is also resolved from the platform's header;
    the JSON surface never reads it.
    """
    connection.state.principal = None
    connection.state.authenticated_user_id = None
    settings: Settings = connection.app.state.settings
    if connection.url.path in SETUP_EXEMPT_PATHS or is_webhook_call(connection):
        return
    runtime = get_runtime(connection)
    platform = None if is_api_path(connection) else runtime.platform
    presented = bearer_credentials(connection) if isinstance(connection, Request) else None
    cookie = (
        None if presented is not None else connection.cookies.get(session_cookie_name(settings))
    )
    if presented is None and cookie is None and platform is None:
        return
    database = _database_of(connection)
    if database is None:
        return
    try:
        if presented is not None:
            async with database.session() as db:
                tokens = build_token_service(db, runtime, ActivityRecorder(db, runtime.clock))
                principal = await tokens.resolve(presented)
        else:
            principal = await _session_principal(
                connection, database, settings, runtime, platform, cookie
            )
    except DatabaseClosed:
        return
    connection.state.principal = principal
    if principal is not None:
        connection.state.authenticated_user_id = principal.user_id


async def _session_principal(
    connection: HTTPConnection,
    database: Database,
    settings: Settings,
    runtime: AuthRuntime,
    platform: PlatformIdentity | None,
    cookie: str | None,
) -> Principal | None:
    async with database.session() as db:
        principal = (
            None
            if cookie is None
            else await build_session_service(db, settings, runtime).resolve(cookie)
        )
    if principal is not None and _password_session_restricted(principal, settings):
        async with database.session() as db:
            login = build_platform_login(db, settings, runtime)
            if login is not None:
                await login.end_session(
                    principal,
                    PASSWORD_SIGN_IN_DISABLED,
                    remote_address=remote_address(connection),
                    method=PASSWORD_METHOD,
                )
        queue_cookie(connection, lambda response: clear_session_cookie(response, settings))
        principal = None
    if platform is None:
        return principal
    bound = principal is not None and principal.sign_in_method == JWT_METHOD
    may_exchange = (
        isinstance(connection, Request)
        and connection.method in EXCHANGE_METHODS
        and SIGNED_OUT_COOKIE not in connection.cookies
    )
    value = connection.headers.get(platform.header)
    if not bound and (principal is not None or not may_exchange or value is None):
        return principal
    assertion = await platform.assertion(value)
    address = remote_address(connection)
    async with database.session() as db:
        login = build_platform_login(db, settings, runtime)
        assert login is not None
        if principal is not None:
            ending = await login.binding_ending(principal, assertion)
            if ending is None:
                return principal
            await login.end_session(principal, ending, remote_address=address)
        if may_exchange and assertion is not None:
            opened = await login.exchange(
                assertion,
                remote_address=address,
                user_agent=connection.headers.get("user-agent"),
                may_create=accounts_may_be_created(connection),
            )
            if opened is not None:
                _queue_session_cookies(connection, opened, settings)
                return opened.principal
    if principal is not None:
        queue_cookie(connection, lambda response: clear_session_cookie(response, settings))
    return None


def accounts_may_be_created(connection: HTTPConnection) -> bool:
    """Report whether the instance is open, so a first sign-in may write a new account."""
    gate = getattr(connection.app.state, "restore_gate", None)
    return gate is None or bool(gate.phase == "open")


def _password_session_restricted(principal: Principal, settings: Settings) -> bool:
    return (
        settings.jwt_enabled
        and settings.jwt_password_sign_in == "administrators"
        and principal.sign_in_method == PASSWORD_METHOD
        and principal.role != "administrator"
    )


def _queue_session_cookies(
    connection: HTTPConnection, opened: PlatformSession, settings: Settings
) -> None:
    assert isinstance(connection, Request)
    queue_cookie(
        connection, lambda response: set_session_cookie(response, opened.session.token, settings)
    )
    # A fresh token at privilege change, so one planted before sign-in is worthless afterwards.
    connection.state.csrf_token = new_token()
    connection.state.csrf_issued = True
    queue_cookie(connection, lambda response: attach_csrf_cookie(connection, response))
    connection.state.csrf_issued = False


def safe_next(target: str | None) -> str:
    """Return target if it is a path on this site, otherwise the home page."""
    if (
        target
        and target.startswith("/")
        and not target.startswith("//")
        and "\\" not in target
        and not any(ord(char) < 32 for char in target)
    ):
        return target
    return "/"


def _unauthenticated(request: Request) -> Exception:
    if is_api_path(request):
        return HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    destination = f"{LOGIN_PATH}?next={quote(target, safe='')}"
    if is_htmx_request(request):
        # htmx does not follow a redirect for a fragment swap; this header makes it navigate.
        return HTTPException(status_code=401, headers={"HX-Redirect": destination})
    # A lost form submission cannot be replayed after login, so only a page view is remembered.
    return RedirectRequired(destination if request.method == "GET" else LOGIN_PATH)


async def require_user(request: Request) -> Principal:
    """Return the signed-in principal, or answer 401 or redirect to the login page."""
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise _unauthenticated(request)
    return principal


async def require_token_user(request: Request) -> Principal:
    """Return the principal of a bearer-token request; a session cookie alone answers 401."""
    if not is_bearer_request(request):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return await require_user(request)


async def require_administrator(
    principal: Annotated[Principal, Depends(require_user)],
) -> Principal:
    """Return the principal if it is an administrator, otherwise answer 403."""
    if not principal.is_administrator:
        raise HTTPException(status_code=403)
    return principal


async def require_administrator_or_not_found(
    request: Request,
    principal: Annotated[Principal, Depends(require_user)],
) -> Principal:
    """Return the principal if it is an administrator, otherwise answer 404 like an unknown path.

    The refusal clears the principal so the error page is the one an unknown path gets.
    """
    if not principal.is_administrator:
        request.state.principal = None
        raise HTTPException(status_code=404)
    return principal


CurrentUser = Annotated[Principal, Depends(require_user)]
CurrentAdministrator = Annotated[Principal, Depends(require_administrator)]
Services = Annotated[AuthServices, Depends(get_auth_services)]
