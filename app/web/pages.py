"""HTML page routes."""

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth.dependencies import CurrentUser, require_user
from app.db.repositories.pagination import (
    MAX_PAGE_SIZE,
    CursorPosition,
    InvalidCursorError,
    Page,
    decode_cursor,
    encode_cursor,
)
from app.services.assembly import Environments
from app.web.environment_routes import dashboard_page
from app.web.pagination import DEFAULT_LIMIT, Column, Direction, PageView
from app.web.templating import is_htmx_request, render_page, render_partial

router = APIRouter(dependencies=[Depends(require_user)])
# Sample-data routes: signed-in users only, mounted only when development checks are on.
kit_router = APIRouter(dependencies=[Depends(require_user)])

SortKey = Literal["name", "size"]
KIT_PATH = "/ui-kit"
KIT_COLUMNS = (
    Column("name", "Name", sortable=True),
    Column("state", "State", width="badge"),
    Column("size", "Size", sortable=True, numeric=True, width="count"),
)
PROGRESS_STEPS = 4
DASHBOARD_PARAM_MAX = 256
DASHBOARD_TAGS_MAX = 50


@dataclass(frozen=True, slots=True)
class KitRow:
    """A fixed sample row; the worked example needs no datastore."""

    row_id: int
    name: str
    state: Literal["running", "stopped", "unhealthy"]
    size: int


KIT_ROWS = tuple(
    KitRow(
        row_id=n,
        name=f"service-{n:02d}",
        state=("running", "stopped", "unhealthy")[n % 3],
        size=(n * 7_919_003) % 900_000_000,
    )
    for n in range(1, 15)
)


def _kit_page(limit: int, cursor: str | None, sort: SortKey, direction: Direction) -> Page[KitRow]:
    """Page the sample rows by keyset, the way a repository does."""

    def key(row: KitRow) -> tuple[str | int, int]:
        return (row.name if sort == "name" else row.size, row.row_id)

    ordered = sorted(KIT_ROWS, key=key, reverse=direction == "desc")
    if cursor is not None:
        position = decode_cursor(cursor)
        after = (position.sort_value, position.row_id)
        ordered = [
            row
            for row in ordered
            if (key(row) < after if direction == "desc" else key(row) > after)
        ]
    window = ordered[: limit + 1]
    items = window[:limit]
    next_cursor = None
    if len(window) > limit:
        last = items[-1]
        next_cursor = encode_cursor(CursorPosition(key(last)[0], last.row_id))
    return Page(items=items, next_cursor=next_cursor)


@router.get("/", response_class=HTMLResponse, name="index")
async def index(
    request: Request,
    services: Environments,
    actor: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    cursor: str | None = None,
    q: Annotated[str | None, Query(max_length=DASHBOARD_PARAM_MAX)] = None,
    group: Annotated[str | None, Query(max_length=DASHBOARD_PARAM_MAX)] = None,
    status: Annotated[str | None, Query(max_length=DASHBOARD_PARAM_MAX)] = None,
    tag: Annotated[list[str] | None, Query(max_length=DASHBOARD_TAGS_MAX)] = None,
) -> Response:
    """Render the dashboard: the environments the caller may see, filtered and paged."""
    return await dashboard_page(
        request,
        services,
        actor,
        q=q,
        group=group,
        status=status,
        tags=tag or [],
        limit=limit,
        cursor=cursor,
    )


@kit_router.get(KIT_PATH, response_class=HTMLResponse, name="ui_kit")
async def ui_kit(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    cursor: str | None = None,
    sort: SortKey = "name",
    direction: Annotated[Direction, Query(alias="dir")] = "asc",
) -> Response:
    """Render the shared components with sample data; the fragment form serves htmx sorting."""
    try:
        page = _kit_page(limit, cursor, sort, direction)
    except (InvalidCursorError, TypeError):
        # A cursor minted under a different ordering compares a string with a number.
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    pager = PageView.of(
        page, base_path=KIT_PATH, limit=limit, cursor=cursor, sort=sort, direction=direction
    )
    if is_htmx_request(request):
        return render_partial(
            request, "ui_kit_table", rows=page.items, columns=KIT_COLUMNS, pager=pager
        )
    return render_page(
        request, "ui_kit.html", {"rows": page.items, "columns": KIT_COLUMNS, "pager": pager}
    )


@kit_router.post(KIT_PATH, name="ui_kit_submit")
async def ui_kit_submit() -> RedirectResponse:
    """Accept the sample forms and return to the kit, so the no-script flow completes."""
    return RedirectResponse(KIT_PATH, status_code=303)


@kit_router.get(f"{KIT_PATH}/progress", response_class=HTMLResponse, name="ui_kit_progress")
async def ui_kit_progress(
    request: Request, step: Annotated[int, Query(ge=1, le=PROGRESS_STEPS)] = 1
) -> Response:
    """Report sample long-operation progress; each poll asks for the next step."""
    done = step >= PROGRESS_STEPS
    arguments: dict[str, Any] = {
        "panel_id": "kit-progress",
        "title": "Pulling sample image",
        "state": "succeeded" if done else "running",
        "href": f"{KIT_PATH}/progress?step={min(step + 1, PROGRESS_STEPS)}",
        "percent": step * 100 // PROGRESS_STEPS,
        "detail": "Pull complete." if done else f"Step {step} of {PROGRESS_STEPS}.",
    }
    if is_htmx_request(request):
        return render_partial(request, "progress_panel", **arguments)
    return render_page(request, "ui_kit_progress.html", {"panel": arguments})
