"""The signed-in user's API tokens: list, create and revoke."""

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, Services, get_runtime, remote_address, require_user
from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.auth.services import AuthServices
from app.auth.tokens import IssuedToken
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.pagination import MAX_PAGE_SIZE, InvalidCursorError
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.navigation import no_store, redirect
from app.web.pagination import DEFAULT_LIMIT, Column, PageView
from app.web.templating import is_htmx_request, render_fragment, render_page

router = APIRouter(prefix="/account/tokens", dependencies=[Depends(require_user)])

TOKENS_PATH = "/account/tokens"
FIELD_MAX = 256
EXPIRY_DAYS_MAX = 3650
TOKEN_COLUMNS = (
    Column("label", "Label"),
    Column("display_prefix", "Token"),
    Column("created_at", "Created", width="time"),
    Column("last_used_at", "Last used", width="time"),
    Column("expires_at", "Expires", width="time"),
    Column("status", "Status", width="badge"),
    Column("actions", "Actions", width="actions"),
)
TOKEN_TABLE = "tokens/_table.html"

Field = Annotated[str, Form(max_length=FIELD_MAX)]


def _expiry(now: datetime, days: str) -> datetime | None:
    """Turn the form's day count into an expiry; blank means the token does not expire."""
    stripped = days.strip()
    if not stripped:
        return None
    if (
        not (stripped.isascii() and stripped.isdecimal())
        or not 1 <= int(stripped) <= EXPIRY_DAYS_MAX
    ):
        raise ValidationFailed(
            {"expires_in_days": f"Use a whole number of days from 1 to {EXPIRY_DAYS_MAX}."}
        )
    return now + timedelta(days=int(stripped))


async def _tokens_page(
    request: Request,
    services: AuthServices,
    actor: Principal,
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    status_code: int = 200,
    issued: IssuedToken | None = None,
    fields: dict[str, str] | None = None,
    label: str = "",
    expires_in_days: str = "",
) -> Response:
    try:
        page = await services.tokens.list_page(actor, limit=limit, cursor=cursor)
    except (InvalidCursorError, TypeError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    pager = PageView.of(page, base_path=TOKENS_PATH, limit=limit, cursor=cursor)
    now = format_timestamp(get_runtime(request).clock())
    if is_htmx_request(request):
        return no_store(
            render_fragment(
                request,
                TOKEN_TABLE,
                "tokens_table",
                rows=page.items,
                columns=TOKEN_COLUMNS,
                pager=pager,
                now=now,
            )
        )
    context: dict[str, Any] = {
        "rows": page.items,
        "columns": TOKEN_COLUMNS,
        "pager": pager,
        "now": now,
        "issued": issued,
        "error": None,
        "fields": fields or {},
        "label": label,
        "expires_in_days": expires_in_days,
    }
    # no-store keeps the one page that holds a token value out of any cache and the back button.
    return no_store(render_page(request, "tokens/tokens.html", context, status_code=status_code))


@router.get("", response_class=HTMLResponse, name="tokens")
async def list_tokens(
    request: Request,
    services: Services,
    actor: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Response:
    """List the caller's own tokens, newest first."""
    return await _tokens_page(request, services, actor, limit=limit, cursor=cursor)


@router.post("", name="token_create")
async def create_token(
    request: Request,
    services: Services,
    actor: CurrentUser,
    label: Field = "",
    expires_in_days: Field = "",
) -> Response:
    """Issue a token and answer with the page that shows its value, once."""
    try:
        issued = await services.tokens.create(
            actor,
            label=label,
            expires_at=_expiry(get_runtime(request).clock(), expires_in_days),
            remote_address=remote_address(request),
        )
    except ValidationFailed as invalid:
        return await _tokens_page(
            request,
            services,
            actor,
            status_code=422,
            fields=invalid.fields,
            label=label,
            expires_in_days=expires_in_days,
        )
    return await _tokens_page(request, services, actor, issued=issued)


@router.post("/{token_id}/revoke", name="token_revoke")
async def revoke_token(
    request: Request, services: Services, actor: CurrentUser, token_id: PathId
) -> Response:
    """Revoke one of the caller's tokens."""
    await services.tokens.revoke(actor, token_id, remote_address=remote_address(request))
    return redirect(request, TOKENS_PATH, flashes=[Flash("ok", "Token revoked.")])
