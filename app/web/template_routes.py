"""Template browsing for signed-in users: the list and a template's page."""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import ResourceNotFound
from app.db.repositories.pagination import (
    CursorPosition,
    InvalidCursorError,
    Page,
    decode_cursor,
    encode_cursor,
)
from app.services.app_template import template_ref
from app.services.assembly import TemplateReads, TemplateTargets
from app.services.template_prefill import architecture_note
from app.services.template_reader import DEFAULT_LIMIT, MAX_LIMIT, MAX_SEARCH_CHARS
from app.web.identifiers import MAX_ID, PathId
from app.web.navigation import no_store
from app.web.object_pages import CURSOR_MAX, route
from app.web.pagination import Column, PageView
from app.web.templating import is_htmx_request, render_fragment, render_page

router = APIRouter(prefix="/templates", tags=["templates"], dependencies=[Depends(require_user)])

TABLE: Final = "app_templates/_table.html"
CATEGORY_MAX: Final = 64
KEY_MAX: Final = 64
ID_DIGITS: Final = len(str(MAX_ID))
COLUMNS: Final = (
    Column("title", "Application"),
    Column("catalogue", "Catalogue", width="medium"),
    Column("categories", "Categories", optional=True),
    Column("image", "Image", optional=True, wide_only=True),
)


def _offset_of(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        position = decode_cursor(cursor)
    except InvalidCursorError:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    if (
        isinstance(position.sort_value, bool)
        or not isinstance(position.sort_value, int)
        or position.sort_value < 0
        or position.row_id != 0
    ):
        raise HTTPException(status_code=400, detail="invalid cursor")
    return position.sort_value


def _catalogue_of(text: str) -> int | None:
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.isascii() and stripped.isdigit() and 1 <= int(stripped) <= MAX_ID:
        return int(stripped)
    raise HTTPException(status_code=422, detail="invalid catalogue")


def _cursor_at(offset: int) -> str:
    return encode_cursor(CursorPosition(offset, 0))


@router.get("", response_class=HTMLResponse, name="templates")
async def list_templates(
    request: Request,
    reads: TemplateReads,
    actor: CurrentUser,
    q: Annotated[str, Query(max_length=MAX_SEARCH_CHARS)] = "",
    category: Annotated[str, Query(max_length=CATEGORY_MAX)] = "",
    catalogue: Annotated[str, Query(max_length=ID_DIGITS)] = "",
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=CURSOR_MAX)] = None,
) -> Response:
    """List the templates of the enabled catalogues with search and filters, or the table alone."""
    offset = _offset_of(cursor)
    catalogue_id = _catalogue_of(catalogue)
    term = q.strip()
    page = await reads.list_templates(
        actor,
        search=term,
        category=category.strip() or None,
        catalogue_id=catalogue_id,
        offset=offset,
        limit=limit,
    )
    carried = [
        (name, value)
        for name, value in (
            ("q", term),
            ("category", category.strip()),
            ("catalogue", "" if catalogue_id is None else str(catalogue_id)),
        )
        if value
    ]
    pager = PageView.of(
        Page(page.items, _cursor_at(offset + limit) if page.has_more else None),
        base_path=route(request, "templates"),
        limit=limit,
        cursor=cursor,
        params=carried,
    )
    filtered = bool(carried)
    if is_htmx_request(request):
        return no_store(
            render_fragment(
                request,
                TABLE,
                "templates_table",
                rows=page.items,
                columns=COLUMNS,
                pager=pager,
                filtered=filtered,
            )
        )
    return no_store(
        render_page(
            request,
            "app_templates/list.html",
            {
                "rows": page.items,
                "columns": COLUMNS,
                "pager": pager,
                "filtered": filtered,
                "total": page.total,
                "q": term,
                "category": category.strip(),
                "catalogue": catalogue_id,
                "categories": await reads.categories(actor),
                "catalogues": await reads.catalogues(actor),
                "can_add": actor.is_administrator,
            },
        )
    )


@router.get("/{catalogue_id}/{key}", response_class=HTMLResponse, name="template_detail")
async def template_detail(
    request: Request,
    reads: TemplateReads,
    targets: TemplateTargets,
    actor: CurrentUser,
    catalogue_id: PathId,
    key: Annotated[str, Path(max_length=KEY_MAX)],
) -> Response:
    """Show what a template sets and the environments where it can be created."""
    entry = await reads.get_template(actor, template_ref(catalogue_id, key))
    if entry is None:
        raise ResourceNotFound("not found")
    creatable = await targets.creatable(actor)
    # The environment snapshot does not record a host's architecture; pass it once it does.
    rows = [
        (target, architecture_note(entry.template.architectures, None))
        for target in creatable.items
    ]
    return no_store(
        render_page(
            request,
            "app_templates/detail.html",
            {
                "entry": entry,
                "template": entry.template,
                "targets": rows,
                "truncated": creatable.truncated,
                "is_administrator": actor.is_administrator,
            },
        )
    )
