"""Login, logout, first-run setup and the signed-in user's own account."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import (
    LOGIN_PATH,
    SETUP_PATH,
    CurrentUser,
    Services,
    accounts_may_be_created,
    get_runtime,
    remote_address,
    safe_next,
)
from app.auth.errors import (
    InvalidCredentials,
    LockedOut,
    SetupClosed,
    SetupTokenRejected,
    ValidationFailed,
)
from app.auth.login import PASSWORD_METHOD
from app.auth.passwords import MAX_PASSWORD_LENGTH
from app.auth.platform_signin import JWT_METHOD
from app.auth.principal import PasswordCredentials, Principal
from app.auth.providers import INTERNAL_PROVIDER, UnknownProviderError
from app.auth.services import AuthRuntime, AuthServices, build_platform_login
from app.auth.sessions import (
    clear_session_cookie,
    clear_signed_out_cookie,
    set_session_cookie,
    set_signed_out_cookie,
)
from app.config import Settings, get_settings
from app.db.engine import Database, DatabaseClosed
from app.web.csrf import attach_csrf_cookie, clear_csrf_cookie, new_token
from app.web.flash import Flash, clear_flashes
from app.web.navigation import no_store, redirect
from app.web.results import forget_results
from app.web.templating import render_page

router = APIRouter()

ACCOUNT_PATH = "/account"
SESSION_REPLACED = "session_replaced"
# Bounds the work a form field can request before validation; far above any real value.
FIELD_MAX = MAX_PASSWORD_LENGTH + 1

Field = Annotated[str, Form(max_length=FIELD_MAX)]


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


def _login_context(
    runtime: AuthRuntime,
    *,
    next_path: str,
    username: str = "",
    error: str | None = None,
    retry_after: int | None = None,
    continue_as: str | None = None,
) -> dict[str, Any]:
    return {
        "next": next_path,
        "username": username,
        "error": error,
        "retry_after": retry_after,
        "methods": runtime.registry.names(),
        "continue_as": continue_as,
    }


async def _continue_as(request: Request, services: AuthServices | None = None) -> str | None:
    runtime = get_runtime(request)
    platform = runtime.platform
    if platform is None:
        return None
    assertion = await platform.assertion(request.headers.get(platform.header))
    if assertion is None:
        return None
    if services is not None and services.platform is not None:
        return await services.platform.candidate(assertion)
    database: Database | None = getattr(request.app.state, "database", None)
    if database is None:
        return None
    try:
        async with database.session() as db:
            login = build_platform_login(db, request.app.state.settings, runtime)
            return None if login is None else await login.candidate(assertion)
    except DatabaseClosed:
        return None


@router.get(LOGIN_PATH, response_class=HTMLResponse, name="login")
async def login_page(request: Request, next: str | None = None) -> Response:
    """Render the sign-in form, or send an already signed-in caller on."""
    if getattr(request.state, "principal", None) is not None:
        return redirect(request, safe_next(next))
    return _page(
        request,
        "auth/login.html",
        _login_context(
            get_runtime(request),
            next_path=safe_next(next),
            continue_as=await _continue_as(request),
        ),
    )


@router.post(LOGIN_PATH, name="login_submit")
async def login_submit(
    request: Request,
    services: Services,
    settings: Annotated[Settings, Depends(get_settings)],
    username: Field = "",
    password: Field = "",
    next: Field = "",
    provider: Field = INTERNAL_PROVIDER,
) -> Response:
    """Sign in; on success set the session cookie and rotate the CSRF token."""
    runtime = get_runtime(request)
    next_path = safe_next(next)
    try:
        result = await services.login.login(
            provider,
            PasswordCredentials(username, password),
            remote_address=remote_address(request),
            user_agent=request.headers.get("user-agent"),
        )
    except LockedOut as locked:
        response = _page(
            request,
            "auth/login.html",
            _login_context(
                runtime,
                next_path=next_path,
                username=username,
                error=str(locked),
                retry_after=locked.retry_after,
                continue_as=await _continue_as(request, services),
            ),
            status_code=429,
        )
        response.headers["Retry-After"] = str(locked.retry_after)
        return response
    except InvalidCredentials as invalid:
        return _page(
            request,
            "auth/login.html",
            _login_context(
                runtime,
                next_path=next_path,
                username=username,
                error=str(invalid),
                continue_as=await _continue_as(request, services),
            ),
            status_code=401,
        )
    except UnknownProviderError:
        raise HTTPException(status_code=400) from None
    response = redirect(request, next_path)
    clear_flashes(request, response)
    set_session_cookie(response, result.session.token, settings)
    _rotate_csrf(request, response)
    return response


def _rotate_csrf(request: Request, response: Response) -> None:
    # A fresh token at privilege change, so one planted before login is worthless afterwards.
    request.state.csrf_token = new_token()
    request.state.csrf_issued = True
    attach_csrf_cookie(request, response)


@router.post("/login/continue", name="login_continue")
async def login_continue(
    request: Request,
    services: Services,
    settings: Annotated[Settings, Depends(get_settings)],
    next: Field = "",
) -> Response:
    """Sign in as the account the access platform's token names, after a sign-out."""
    runtime = get_runtime(request)
    platform = runtime.platform
    if platform is None or services.platform is None:
        raise HTTPException(status_code=404)
    assertion = await platform.assertion(request.headers.get(platform.header))
    opened = None
    if assertion is not None:
        current: Principal | None = getattr(request.state, "principal", None)
        if current is not None and current.session_id is not None:
            await services.platform.end_session(
                current,
                SESSION_REPLACED,
                remote_address=remote_address(request),
                method=current.sign_in_method or PASSWORD_METHOD,
            )
        opened = await services.platform.exchange(
            assertion,
            remote_address=remote_address(request),
            user_agent=request.headers.get("user-agent"),
            may_create=accounts_may_be_created(request),
        )
    if opened is None:
        return _page(
            request,
            "auth/login.html",
            _login_context(
                runtime,
                next_path=safe_next(next),
                error="The access platform did not identify an account that can sign in here.",
            ),
            status_code=401,
        )
    response = redirect(request, safe_next(next))
    clear_flashes(request, response)
    set_session_cookie(response, opened.session.token, settings)
    clear_signed_out_cookie(response, settings)
    _rotate_csrf(request, response)
    return response


@router.post("/logout", name="logout")
async def logout(
    request: Request,
    services: Services,
    principal: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """End the session, deleting its row so the cookie stops working at once."""
    await services.login.logout(principal, remote_address=remote_address(request))
    forget_results(request, principal.user_id)
    response = redirect(request, LOGIN_PATH)
    clear_flashes(request, response)
    clear_session_cookie(response, settings)
    clear_csrf_cookie(response, settings)
    if principal.sign_in_method == JWT_METHOD:
        set_signed_out_cookie(response, settings)
    return response


def _setup_context(
    services: AuthServices,
    *,
    username: str = "",
    error: str | None = None,
    fields: dict[str, str] | None = None,
    retry_after: int | None = None,
) -> dict[str, Any]:
    return {
        "username": username,
        "error": error,
        "retry_after": retry_after,
        "fields": fields or {},
        "password_policy": services.policy,
    }


@router.get(SETUP_PATH, response_class=HTMLResponse, name="setup")
async def setup_page(request: Request, services: Services) -> Response:
    """Render the first-run form; once an administrator exists the route does not exist."""
    if not await services.bootstrap.is_open():
        raise HTTPException(status_code=404)
    return _page(request, "auth/setup.html", _setup_context(services))


@router.post(SETUP_PATH, name="setup_submit")
async def setup_submit(
    request: Request,
    services: Services,
    setup_token: Field = "",
    username: Field = "",
    password: Field = "",
    password_confirm: Field = "",
) -> Response:
    """Create the first administrator, then close the setup window for good."""
    try:
        await services.bootstrap.complete(
            setup_token=setup_token,
            username=username,
            password=password,
            password_confirm=password_confirm,
            remote_address=remote_address(request),
        )
    except SetupClosed:
        raise HTTPException(status_code=404) from None
    except LockedOut as locked:
        response = _page(
            request,
            "auth/setup.html",
            _setup_context(
                services, username=username, error=str(locked), retry_after=locked.retry_after
            ),
            status_code=429,
        )
        response.headers["Retry-After"] = str(locked.retry_after)
        return response
    except SetupTokenRejected:
        return _page(
            request,
            "auth/setup.html",
            _setup_context(
                services,
                username=username,
                fields={"setup_token": "The setup token is not correct."},
            ),
            status_code=403,
        )
    except ValidationFailed as invalid:
        return _page(
            request,
            "auth/setup.html",
            _setup_context(services, username=username, fields=invalid.fields),
            status_code=422,
        )
    return redirect(request, LOGIN_PATH)


async def _account_response(
    request: Request,
    services: AuthServices,
    principal: Principal,
    *,
    status_code: int = 200,
    fields: dict[str, str] | None = None,
    username: str | None = None,
) -> Response:
    user = await services.user_admin.profile(principal)
    return _page(
        request,
        "auth/account.html",
        {
            "user": user,
            "username": user.username if username is None else username,
            "fields": fields or {},
            "password_policy": services.policy,
        },
        status_code=status_code,
    )


@router.get(ACCOUNT_PATH, response_class=HTMLResponse, name="account")
async def account_page(request: Request, services: Services, principal: CurrentUser) -> Response:
    """Render the signed-in user's profile and password forms."""
    return await _account_response(request, services, principal)


@router.post(ACCOUNT_PATH, name="account_update")
async def account_update(
    request: Request, services: Services, principal: CurrentUser, username: Field = ""
) -> Response:
    """Change the signed-in user's own username."""
    try:
        await services.user_admin.update_profile(
            principal, username=username, remote_address=remote_address(request)
        )
    except ValidationFailed as invalid:
        return await _account_response(
            request, services, principal, status_code=422, fields=invalid.fields, username=username
        )
    return redirect(request, ACCOUNT_PATH, flashes=[Flash("ok", "Profile updated.")])


@router.post(f"{ACCOUNT_PATH}/password", name="account_password")
async def account_password(
    request: Request,
    services: Services,
    principal: CurrentUser,
    current_password: Field = "",
    new_password: Field = "",
    new_password_confirm: Field = "",
) -> Response:
    """Change the signed-in user's password; their other sessions end."""
    try:
        await services.user_admin.change_password(
            principal,
            current_password=current_password,
            new_password=new_password,
            new_password_confirm=new_password_confirm,
            remote_address=remote_address(request),
        )
    except ValidationFailed as invalid:
        return await _account_response(
            request, services, principal, status_code=422, fields=invalid.fields
        )
    forget_results(request, principal.user_id)
    return redirect(
        request,
        ACCOUNT_PATH,
        flashes=[
            Flash(
                "ok",
                "Password changed. Your other sessions have been signed out"
                " and your API tokens and webhook URLs revoked.",
            )
        ],
    )
