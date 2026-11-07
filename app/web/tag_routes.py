"""Tag pages: list and create on one page, and a confirmed delete."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentAdministrator, require_administrator
from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE, InvalidCursorError
from app.services.assembly import Environments, EnvironmentServices
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.navigation import no_store, redirect
from app.web.pagination import DEFAULT_LIMIT, Column, PageView
from app.web.templating import is_htmx_request, render_fragment, render_page

router = APIRouter(prefix="/tags", dependencies=[Depends(require_administrator)])

TAGS_PATH = "/tags"
FIELD_MAX = 256
TABLES = "tags/_tables.html"
TAG_COLUMNS = (
    Column("name", "Tag"),
    Column("environment_count", "Environments", numeric=True, width="short"),
    Column("created_at", "Created", width="time"),
    Column("actions", "Actions", width="actions"),
)

Field = Annotated[str, Form(max_length=FIELD_MAX)]


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


async def _list(
    request: Request,
    services: EnvironmentServices,
    actor: Principal,
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    status_code: int = 200,
    fields: dict[str, str] | None = None,
    name: str = "",
) -> Response:
    try:
        page = await services.tags.list_page(actor, limit=limit, cursor=cursor)
    except (InvalidCursorError, TypeError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    pager = PageView.of(page, base_path=TAGS_PATH, limit=limit, cursor=cursor)
    if is_htmx_request(request):
        return no_store(
            render_fragment(
                request,
                TABLES,
                "tags_table",
                rows=page.items,
                columns=TAG_COLUMNS,
                pager=pager,
            )
        )
    return _page(
        request,
        "tags/tags.html",
        {
            "rows": page.items,
            "columns": TAG_COLUMNS,
            "pager": pager,
            "name": name,
            "fields": fields or {},
        },
        status_code,
    )


@router.get("", response_class=HTMLResponse, name="tags")
async def list_tags(
    request: Request,
    services: Environments,
    actor: CurrentAdministrator,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Response:
    """List the tags with the number of environments carrying each, and the create form."""
    return await _list(request, services, actor, limit=limit, cursor=cursor)


@router.post("", name="tag_create")
async def create_tag(
    request: Request, services: Environments, actor: CurrentAdministrator, name: Field = ""
) -> Response:
    """Create a tag."""
    try:
        created = await services.tags.create(actor, name=name)
    except ValidationFailed as invalid:
        return await _list(
            request, services, actor, status_code=422, fields=invalid.fields, name=name
        )
    return redirect(request, TAGS_PATH, flashes=[Flash("ok", f"Created the tag {created.name}.")])


async def _confirm_delete_page(
    request: Request,
    services: EnvironmentServices,
    actor: Principal,
    tag_id: int,
    *,
    fields: dict[str, str] | None = None,
) -> Response:
    preview = await services.tags.delete_preview(actor, tag_id)
    return _page(
        request,
        "tags/delete_confirm.html",
        {"preview": preview, "fields": fields or {}},
        422 if fields else 200,
    )


@router.get("/{tag_id}/delete", response_class=HTMLResponse, name="tag_delete_confirm")
async def confirm_delete(
    request: Request, services: Environments, actor: CurrentAdministrator, tag_id: PathId
) -> Response:
    """List the environments that lose the tag; nothing is deleted by this page."""
    return await _confirm_delete_page(request, services, actor, tag_id)


@router.post("/{tag_id}/delete", name="tag_delete")
async def delete_tag(
    request: Request,
    services: Environments,
    actor: CurrentAdministrator,
    tag_id: PathId,
    expected_name: Field = "",
) -> Response:
    """Delete the tag and its links when its name is still the one confirmed."""
    try:
        await services.tags.delete(actor, tag_id, expected_name=expected_name)
    except ValidationFailed as invalid:
        return await _confirm_delete_page(request, services, actor, tag_id, fields=invalid.fields)
    return redirect(request, TAGS_PATH)
