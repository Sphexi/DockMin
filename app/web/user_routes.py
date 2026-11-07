"""Administrator user management."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import (
    CurrentAdministrator,
    Services,
    get_runtime,
    remote_address,
    require_administrator,
)
from app.auth.errors import LastAdministratorError, UserHasActivity, ValidationFailed
from app.auth.passwords import MAX_PASSWORD_LENGTH
from app.auth.principal import Principal
from app.auth.services import AuthServices
from app.auth.users import ROLES
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.pagination import MAX_PAGE_SIZE, InvalidCursorError
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.navigation import no_store, redirect
from app.web.pagination import DEFAULT_LIMIT, Column, PageView
from app.web.results import forget_results
from app.web.templating import is_htmx_request, render_fragment, render_page, render_partial
from app.web.token_routes import TOKEN_COLUMNS, TOKEN_TABLE

# The guard is on the router, so a route added here later is administrator-only by default.
router = APIRouter(prefix="/users", dependencies=[Depends(require_administrator)])

USERS_PATH = "/users"
FIELD_MAX = MAX_PASSWORD_LENGTH + 1
USER_COLUMNS = (
    Column("username", "Username"),
    Column("role", "Role", width="short"),
    Column("is_enabled", "Status", width="badge"),
    Column("last_login_at", "Last sign-in", width="time"),
)

Field = Annotated[str, Form(max_length=FIELD_MAX)]


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


def _done(request: Request, user_id: int, message: str) -> Response:
    return redirect(request, f"{USERS_PATH}/{user_id}", flashes=[Flash("ok", message)])


async def _detail(
    request: Request,
    services: AuthServices,
    actor: Principal,
    user_id: PathId,
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    status_code: int = 200,
    error: str | None = None,
    fields: dict[str, str] | None = None,
    username: str | None = None,
    role: str | None = None,
    identity: str | None = None,
) -> Response:
    user = await services.user_admin.get(actor, user_id)
    try:
        tokens = await services.tokens.list_page(actor, user_id=user_id, limit=limit, cursor=cursor)
    except (InvalidCursorError, TypeError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    pager = PageView.of(tokens, base_path=f"{USERS_PATH}/{user_id}", limit=limit, cursor=cursor)
    now = format_timestamp(get_runtime(request).clock())
    if is_htmx_request(request):
        return no_store(
            render_fragment(
                request,
                TOKEN_TABLE,
                "tokens_table",
                rows=tokens.items,
                columns=TOKEN_COLUMNS,
                pager=pager,
                now=now,
                owner_id=user_id,
            )
        )
    return _page(
        request,
        "auth/user_detail.html",
        {
            "user": user,
            "token_rows": tokens.items,
            "token_columns": TOKEN_COLUMNS,
            "token_pager": pager,
            "now": now,
            "username": user.username if username is None else username,
            "role": user.role if role is None else role,
            "identity": (user.external_identity or "") if identity is None else identity,
            "platform_enabled": get_runtime(request).platform is not None,
            "roles": ROLES,
            "is_self": user.id == actor.user_id,
            "error": error,
            "fields": fields or {},
            "password_policy": services.policy,
        },
        status_code=status_code,
    )


def _new_form(
    request: Request,
    services: AuthServices,
    *,
    username: str = "",
    role: str = "standard",
    fields: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return _page(
        request,
        "auth/user_new.html",
        {
            "username": username,
            "role": role,
            "roles": ROLES,
            "fields": fields or {},
            "password_policy": services.policy,
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse, name="users")
async def list_users(
    request: Request,
    services: Services,
    actor: CurrentAdministrator,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Response:
    """List users, one cursor page at a time."""
    try:
        page = await services.user_admin.list_page(actor, limit=limit, cursor=cursor)
    except (InvalidCursorError, TypeError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    pager = PageView.of(
        page, base_path=USERS_PATH, limit=limit, cursor=cursor, sort="username", direction="asc"
    )
    if is_htmx_request(request):
        return no_store(
            render_partial(
                request,
                "users_table",
                rows=page.items,
                columns=USER_COLUMNS,
                pager=pager,
                current_user_id=actor.user_id,
            )
        )
    total = await services.user_admin.count(actor)
    return _page(
        request,
        "auth/users.html",
        {"rows": page.items, "columns": USER_COLUMNS, "pager": pager, "total": total},
    )


@router.get("/new", response_class=HTMLResponse, name="user_new")
async def new_user_form(request: Request, services: Services) -> Response:
    """Render the create-user form."""
    return _new_form(request, services)


@router.post("", name="user_create")
async def create_user(
    request: Request,
    services: Services,
    actor: CurrentAdministrator,
    username: Field = "",
    password: Field = "",
    role: Field = "standard",
) -> Response:
    """Create an account and open its page."""
    try:
        created = await services.user_admin.create(
            actor,
            username=username,
            password=password,
            role=role,
            remote_address=remote_address(request),
        )
    except ValidationFailed as invalid:
        return _new_form(
            request,
            services,
            username=username,
            role=role,
            fields=invalid.fields,
            status_code=422,
        )
    return redirect(request, f"{USERS_PATH}/{created.id}")


@router.get("/{user_id}", response_class=HTMLResponse, name="user_detail")
async def user_detail(
    request: Request,
    services: Services,
    actor: CurrentAdministrator,
    user_id: PathId,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Response:
    """Show one user with the controls that act on them."""
    return await _detail(request, services, actor, user_id, limit=limit, cursor=cursor)


@router.post("/{user_id}", name="user_update")
async def update_user(
    request: Request,
    services: Services,
    actor: CurrentAdministrator,
    user_id: PathId,
    username: Field = "",
    role: Field = "standard",
) -> Response:
    """Change a user's username and role."""
    try:
        await services.user_admin.update(
            actor, user_id, username=username, role=role, remote_address=remote_address(request)
        )
    except ValidationFailed as invalid:
        return await _detail(
            request,
            services,
            actor,
            user_id,
            status_code=422,
            fields=invalid.fields,
            username=username,
            role=role,
        )
    except LastAdministratorError as refused:
        return await _detail(
            request,
            services,
            actor,
            user_id,
            status_code=409,
            error=str(refused),
            username=username,
            role=role,
        )
    forget_results(request, user_id)
    return _done(request, user_id, "User updated.")


@router.post("/{user_id}/identity", name="user_set_identity")
async def set_identity(
    request: Request,
    services: Services,
    actor: CurrentAdministrator,
    user_id: PathId,
    external_identity: Field = "",
) -> Response:
    """Link the user to an access platform identity, or unlink them when the field is blank."""
    try:
        await services.user_admin.set_external_identity(
            actor, user_id, identity=external_identity, remote_address=remote_address(request)
        )
    except ValidationFailed as invalid:
        return await _detail(
            request,
            services,
            actor,
            user_id,
            status_code=422,
            fields=invalid.fields,
            identity=external_identity,
        )
    return _done(request, user_id, "Access platform identity updated.")


@router.post("/{user_id}/password", name="user_reset_password")
async def reset_password(
    request: Request,
    services: Services,
    actor: CurrentAdministrator,
    user_id: PathId,
    new_password: Field = "",
) -> Response:
    """Set a user's password and end their sessions."""
    try:
        await services.user_admin.reset_password(
            actor, user_id, new_password=new_password, remote_address=remote_address(request)
        )
    except ValidationFailed as invalid:
        return await _detail(
            request, services, actor, user_id, status_code=422, fields=invalid.fields
        )
    forget_results(request, user_id)
    return _done(
        request,
        user_id,
        "Password reset. Their sessions were ended and their API tokens and webhook URLs revoked.",
    )


@router.post("/{user_id}/disable", name="user_disable")
async def disable_user(
    request: Request, services: Services, actor: CurrentAdministrator, user_id: PathId
) -> Response:
    """Disable an account and end its sessions."""
    try:
        await services.user_admin.set_enabled(
            actor, user_id, enabled=False, remote_address=remote_address(request)
        )
    except LastAdministratorError as refused:
        return await _detail(request, services, actor, user_id, status_code=409, error=str(refused))
    return _done(request, user_id, "User disabled.")


@router.post("/{user_id}/enable", name="user_enable")
async def enable_user(
    request: Request, services: Services, actor: CurrentAdministrator, user_id: PathId
) -> Response:
    """Re-enable an account."""
    await services.user_admin.set_enabled(
        actor, user_id, enabled=True, remote_address=remote_address(request)
    )
    return _done(request, user_id, "User enabled.")


@router.post("/{user_id}/sessions/revoke", name="user_revoke_sessions")
async def revoke_sessions(
    request: Request, services: Services, actor: CurrentAdministrator, user_id: PathId
) -> Response:
    """End every session a user has and revoke their API tokens and webhook URLs."""
    ended = await services.user_admin.revoke_sessions(
        actor, user_id, remote_address=remote_address(request)
    )
    return _done(
        request,
        user_id,
        f"Ended {ended} session(s) and revoked the user's API tokens and webhook URLs.",
    )


@router.post("/{user_id}/tokens/{token_id}/revoke", name="user_token_revoke")
async def revoke_user_token(
    request: Request,
    services: Services,
    actor: CurrentAdministrator,
    user_id: PathId,
    token_id: PathId,
) -> Response:
    """Revoke one API token of this user."""
    await services.user_admin.get(actor, user_id)
    await services.tokens.revoke(
        actor, token_id, remote_address=remote_address(request), owner_id=user_id
    )
    return _done(request, user_id, "Token revoked.")


@router.post("/{user_id}/lockout/clear", name="user_clear_lockout")
async def clear_lockout(
    request: Request, services: Services, actor: CurrentAdministrator, user_id: PathId
) -> Response:
    """Lift the sign-in lock that failed attempts placed on an account."""
    cleared = await services.user_admin.clear_lockout(
        actor, user_id, remote_address=remote_address(request)
    )
    message = "Sign-in lock lifted." if cleared else "This account was not locked."
    return _done(request, user_id, message)


@router.post("/{user_id}/delete", name="user_delete")
async def delete_user(
    request: Request,
    services: Services,
    actor: CurrentAdministrator,
    user_id: PathId,
    expected_name: Field = "",
) -> Response:
    """Delete an account that has never acted; refused for the last administrator."""
    try:
        await services.user_admin.delete(
            actor, user_id, expected_name=expected_name, remote_address=remote_address(request)
        )
    except ValidationFailed as invalid:
        return await _detail(
            request, services, actor, user_id, status_code=409, error=invalid.fields["name"]
        )
    except (LastAdministratorError, UserHasActivity) as refused:
        return await _detail(request, services, actor, user_id, status_code=409, error=str(refused))
    return redirect(request, USERS_PATH)
