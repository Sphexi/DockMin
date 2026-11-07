"""Template catalogue pages for administrators: list, add, edit, enable, refresh and delete."""

from dataclasses import dataclass
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentAdministrator, require_administrator
from app.auth.errors import ValidationFailed
from app.db.repositories.pagination import MAX_PAGE_SIZE, InvalidCursorError
from app.services.assembly import CatalogueAdmin
from app.services.template_catalogues import (
    KIND_LABELS,
    KINDS,
    LINUXSERVER_PRESET,
    MAX_CATALOGUES,
    CatalogueInput,
    CatalogueView,
    RefreshResult,
    preset_by_key,
)
from app.services.template_fetch import CatalogueFetchError
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.navigation import no_store
from app.web.object_pages import redirect, route
from app.web.pagination import DEFAULT_LIMIT, Column, PageView
from app.web.templating import is_htmx_request, render_fragment, render_page

router = APIRouter(prefix="/template-catalogues", dependencies=[Depends(require_administrator)])

TABLES: Final = "template_catalogues/_table.html"
FIELD_MAX: Final = 512
URL_FIELD_MAX: Final = 4096
STAMP_MAX: Final = 64
KIND_MAX: Final = 32

NOTICES: Final = {
    "created": "The catalogue is added and disabled. Nothing is contacted until you enable it.",
    "updated": "Changes saved.",
    "updated_source": (
        "Changes saved. The kind or address changed, so the fetched templates are discarded and "
        "the catalogue is disabled. Enable it to fetch from the new address."
    ),
    "disabled": "The catalogue is disabled. Its templates are no longer offered.",
    "deleted": "The catalogue is deleted.",
}
COLUMNS: Final = (
    Column("name", "Name"),
    Column("kind", "Kind", width="medium"),
    Column("host", "Host"),
    Column("state", "State", width="badge"),
    Column("templates", "Templates", numeric=True, width="count"),
    Column("fetched", "Last fetched", width="time"),
)
Text = Annotated[str, Form(max_length=FIELD_MAX)]


@dataclass(frozen=True, slots=True)
class FormState:
    """The values the catalogue form shows."""

    name: str = ""
    kind: str = LINUXSERVER_PRESET.kind
    url: str = ""


def _state_of(view: CatalogueView) -> FormState:
    return FormState(name=view.name, kind=view.kind, url=view.url)


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


def _form_page(
    request: Request,
    *,
    state: FormState,
    catalogue: CatalogueView | None = None,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return _page(
        request,
        "template_catalogues/form.html",
        {
            "state": state,
            "catalogue": catalogue,
            "editing": catalogue is not None,
            "errors": errors or {},
            "kinds": [(kind, KIND_LABELS[kind]) for kind in KINDS],
            "preset": LINUXSERVER_PRESET,
            "max_catalogues": MAX_CATALOGUES,
        },
        status_code,
    )


async def _detail_page(
    request: Request,
    services: CatalogueAdmin,
    actor: CurrentAdministrator,
    catalogue_id: int,
) -> Response:
    return _page(
        request,
        "template_catalogues/detail.html",
        {"catalogue": await services.get(actor, catalogue_id)},
    )


async def _delete_page(
    request: Request,
    services: CatalogueAdmin,
    actor: CurrentAdministrator,
    catalogue_id: int,
    *,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    preview = await services.delete_preview(actor, catalogue_id)
    return _page(
        request,
        "template_catalogues/delete_confirm.html",
        {"preview": preview, "errors": errors or {}},
        status_code,
    )


def _refreshed_notice(result: RefreshResult, *, enabled: bool) -> str:
    lead = "The catalogue is enabled. " if enabled else ""
    text = f"{lead}{result.entry_count} templates are available."
    if result.skipped:
        reasons = ", ".join(
            f"{reason.replace('_', ' ')}: {count}" for reason, count in result.skipped.items()
        )
        text += f" {result.skipped_total} entries were skipped ({reasons})."
    if not result.content_changed:
        text += " The catalogue has not changed since the last fetch."
    return text


@router.get("", response_class=HTMLResponse, name="template_catalogues")
async def list_catalogues(
    request: Request,
    services: CatalogueAdmin,
    actor: CurrentAdministrator,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
) -> Response:
    """List the catalogues by name, as a page or as the table fragment."""
    try:
        page = await services.page(actor, limit=limit, cursor=cursor)
    except (InvalidCursorError, TypeError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    pager = PageView.of(
        page, base_path=route(request, "template_catalogues"), limit=limit, cursor=cursor
    )
    if is_htmx_request(request):
        return no_store(
            render_fragment(
                request, TABLES, "catalogues_table", rows=page.items, columns=COLUMNS, pager=pager
            )
        )
    total = await services.count(actor)
    return _page(
        request,
        "template_catalogues/list.html",
        {"rows": page.items, "columns": COLUMNS, "pager": pager, "total": total},
    )


@router.get("/new", response_class=HTMLResponse, name="template_catalogue_new")
async def new_catalogue_form(
    request: Request,
    actor: CurrentAdministrator,
    preset: Annotated[str | None, Query(max_length=KIND_MAX)] = None,
) -> Response:
    """Render the add form, filled from a preset when one is named."""
    chosen = None if preset is None else preset_by_key(preset)
    state = FormState() if chosen is None else FormState(chosen.name, chosen.kind, chosen.url)
    return _form_page(request, state=state)


@router.post("", name="template_catalogue_create")
async def create_catalogue(
    request: Request,
    services: CatalogueAdmin,
    actor: CurrentAdministrator,
    name: Text = "",
    kind: Annotated[str, Form(max_length=KIND_MAX)] = "",
    url: Annotated[str, Form(max_length=URL_FIELD_MAX)] = "",
) -> Response:
    """Add a disabled catalogue, then open its page."""
    try:
        catalogue_id = await services.create(actor, CatalogueInput(name=name, kind=kind, url=url))
    except ValidationFailed as invalid:
        return _form_page(
            request, state=FormState(name, kind, url), errors=invalid.fields, status_code=422
        )
    return redirect(
        request,
        route(request, "template_catalogue_detail", catalogue_id=catalogue_id),
        [Flash("ok", NOTICES["created"])],
    )


@router.get("/{catalogue_id}", response_class=HTMLResponse, name="template_catalogue_detail")
async def catalogue_detail(
    request: Request, services: CatalogueAdmin, actor: CurrentAdministrator, catalogue_id: PathId
) -> Response:
    """Show a catalogue with its state and what each action does."""
    return await _detail_page(request, services, actor, catalogue_id)


@router.get("/{catalogue_id}/edit", response_class=HTMLResponse, name="template_catalogue_edit")
async def edit_catalogue_form(
    request: Request, services: CatalogueAdmin, actor: CurrentAdministrator, catalogue_id: PathId
) -> Response:
    """Render the edit form."""
    catalogue = await services.get(actor, catalogue_id)
    return _form_page(request, state=_state_of(catalogue), catalogue=catalogue)


@router.post("/{catalogue_id}", name="template_catalogue_update")
async def update_catalogue(
    request: Request,
    services: CatalogueAdmin,
    actor: CurrentAdministrator,
    catalogue_id: PathId,
    name: Text = "",
    kind: Annotated[str, Form(max_length=KIND_MAX)] = "",
    url: Annotated[str, Form(max_length=URL_FIELD_MAX)] = "",
    expected_updated_at: Annotated[str, Form(max_length=STAMP_MAX)] = "",
) -> Response:
    """Save the catalogue if it is unchanged since the form was loaded."""
    try:
        discarded = await services.update(
            actor,
            catalogue_id,
            CatalogueInput(name=name, kind=kind, url=url),
            expected_updated_at=expected_updated_at,
        )
    except ValidationFailed as invalid:
        catalogue = await services.get(actor, catalogue_id)
        state = _state_of(catalogue) if "form" in invalid.fields else FormState(name, kind, url)
        return _form_page(
            request, state=state, catalogue=catalogue, errors=invalid.fields, status_code=422
        )
    notice = NOTICES["updated_source" if discarded else "updated"]
    return redirect(
        request,
        route(request, "template_catalogue_detail", catalogue_id=catalogue_id),
        [Flash("warn" if discarded else "ok", notice)],
    )


@router.post("/{catalogue_id}/enable", name="template_catalogue_enable")
async def enable_catalogue(
    request: Request, services: CatalogueAdmin, actor: CurrentAdministrator, catalogue_id: PathId
) -> Response:
    """Fetch the catalogue once and enable it."""
    try:
        result = await services.enable(actor, catalogue_id)
    except (CatalogueFetchError, ValidationFailed) as refused:
        return _refused(request, catalogue_id, refused)
    return _done(request, catalogue_id, _refreshed_notice(result, enabled=True))


@router.post("/{catalogue_id}/refresh", name="template_catalogue_refresh")
async def refresh_catalogue(
    request: Request, services: CatalogueAdmin, actor: CurrentAdministrator, catalogue_id: PathId
) -> Response:
    """Fetch the enabled catalogue once more."""
    try:
        result = await services.refresh(actor, catalogue_id)
    except (CatalogueFetchError, ValidationFailed) as refused:
        return _refused(request, catalogue_id, refused)
    return _done(request, catalogue_id, _refreshed_notice(result, enabled=False))


@router.post("/{catalogue_id}/disable", name="template_catalogue_disable")
async def disable_catalogue(
    request: Request, services: CatalogueAdmin, actor: CurrentAdministrator, catalogue_id: PathId
) -> Response:
    """Stop offering the catalogue's templates."""
    try:
        await services.disable(actor, catalogue_id)
    except ValidationFailed as refused:
        return _refused(request, catalogue_id, refused)
    return _done(request, catalogue_id, NOTICES["disabled"])


@router.get(
    "/{catalogue_id}/delete", response_class=HTMLResponse, name="template_catalogue_delete_confirm"
)
async def confirm_delete(
    request: Request, services: CatalogueAdmin, actor: CurrentAdministrator, catalogue_id: PathId
) -> Response:
    """Name the catalogue a deletion removes; nothing is deleted by this page."""
    return await _delete_page(request, services, actor, catalogue_id)


@router.post("/{catalogue_id}/delete", name="template_catalogue_delete")
async def delete_catalogue(
    request: Request,
    services: CatalogueAdmin,
    actor: CurrentAdministrator,
    catalogue_id: PathId,
    expected_name: Text = "",
) -> Response:
    """Delete the catalogue while its name is still the reviewed one."""
    try:
        await services.delete(actor, catalogue_id, expected_name=expected_name)
    except ValidationFailed as invalid:
        return await _delete_page(
            request, services, actor, catalogue_id, errors=invalid.fields, status_code=422
        )
    return redirect(
        request, route(request, "template_catalogues"), [Flash("ok", NOTICES["deleted"])]
    )


def _done(request: Request, catalogue_id: int, notice: str) -> Response:
    return redirect(
        request,
        route(request, "template_catalogue_detail", catalogue_id=catalogue_id),
        [Flash("ok", notice)],
    )


def _refused(
    request: Request, catalogue_id: int, refused: CatalogueFetchError | ValidationFailed
) -> Response:
    return redirect(
        request,
        route(request, "template_catalogue_detail", catalogue_id=catalogue_id),
        [Flash("danger", str(refused))],
    )
