"""Environment group pages: list, create, edit, membership, access and deletion."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentAdministrator, require_administrator
from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE, InvalidCursorError
from app.services.assembly import Environments, EnvironmentServices
from app.services.environments import DashboardFilters
from app.web.environment_routes import grant_arguments
from app.web.flash import Flash
from app.web.identifiers import BoundedId, PathId, QueryId
from app.web.navigation import no_store, redirect
from app.web.pagination import DEFAULT_LIMIT, Column, PageView
from app.web.templating import is_htmx_request, render_fragment, render_page

router = APIRouter(prefix="/groups", dependencies=[Depends(require_administrator)])

GROUPS_PATH = "/groups"
ACCESS_OPEN_SUFFIX = "?open=access#access"
FIELD_MAX = 256
ID_PARAMS_MAX = 50
CANDIDATE_LIMIT = 50
SEARCH_MAX = 64
TABLES = "groups/_tables.html"
GROUP_COLUMNS = (
    Column("name", "Group"),
    Column("description", "Description"),
    Column("environment_count", "Environments", numeric=True, width="short"),
    Column("created_at", "Created", width="time"),
)
MEMBER_COLUMNS = (
    Column("name", "Environment"),
    Column("status", "Status", width="badge"),
    Column("tags", "Tags"),
)

Field = Annotated[str, Form(max_length=FIELD_MAX)]


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


def _done(request: Request, group_id: int, *flashes: Flash, open_access: bool = False) -> Response:
    location = f"{GROUPS_PATH}/{group_id}"
    if open_access:
        location += ACCESS_OPEN_SUFFIX
    return redirect(request, location, flashes=flashes)


def _new_form(
    request: Request,
    *,
    name: str = "",
    description: str = "",
    fields: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return _page(
        request,
        "groups/group_form.html",
        {"name": name, "description": description, "fields": fields or {}},
        status_code,
    )


async def _candidates(
    services: EnvironmentServices, actor: Principal, group_id: int, search: str | None
) -> tuple[list[Any], str | None]:
    """Find the environments that could join the group, and an error when the search was refused."""
    try:
        found = await services.groups.candidate_environments(
            actor, group_id, q=search or None, limit=CANDIDATE_LIMIT
        )
    except ValidationFailed as invalid:
        return [], next(iter(invalid.fields.values()), None)
    return list(found), None


async def _detail(
    request: Request,
    services: EnvironmentServices,
    actor: Principal,
    group_id: int,
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    search: str | None = None,
    status_code: int = 200,
    error: str | None = None,
    fields: dict[str, str] | None = None,
    name: str | None = None,
    description: str | None = None,
    grantee_kind: str = "user",
    grantee_name: str = "",
) -> Response:
    group = await services.groups.get(actor, group_id)
    if is_htmx_request(request) and search is not None:
        candidates, search_error = await _candidates(services, actor, group_id, search.strip())
        return no_store(
            render_fragment(
                request,
                TABLES,
                "group_candidates",
                group=group,
                candidates=candidates,
                search=search.strip(),
                search_error=search_error,
                limit=CANDIDATE_LIMIT,
            )
        )
    try:
        members = await services.environments.dashboard(
            actor, DashboardFilters(group_id=group_id), limit=limit, cursor=cursor
        )
    except (InvalidCursorError, TypeError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    pager = PageView.of(members, base_path=f"{GROUPS_PATH}/{group_id}", limit=limit, cursor=cursor)
    if is_htmx_request(request):
        return no_store(
            render_fragment(
                request,
                TABLES,
                "group_members_table",
                rows=members.items,
                columns=MEMBER_COLUMNS,
                pager=pager,
            )
        )
    access_view = await services.access.for_group(actor, group_id)
    text = (search or "").strip()
    candidates, search_error = await _candidates(services, actor, group_id, text)
    return _page(
        request,
        "groups/group_detail.html",
        {
            "group": group,
            "rows": members.items,
            "columns": MEMBER_COLUMNS,
            "pager": pager,
            "access_view": access_view,
            "candidates": candidates,
            "search": text,
            "search_error": search_error,
            "candidate_limit": CANDIDATE_LIMIT,
            "name": group.name if name is None else name,
            "description": (group.description or "") if description is None else description,
            "error": error,
            "fields": fields or {},
            "grantee_kind": grantee_kind,
            "grantee_name": grantee_name,
        },
        status_code,
    )


@router.get("", response_class=HTMLResponse, name="groups")
async def list_groups(
    request: Request,
    services: Environments,
    actor: CurrentAdministrator,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Response:
    """List the groups with the number of environments in each."""
    try:
        page = await services.groups.list_page(actor, limit=limit, cursor=cursor)
    except (InvalidCursorError, TypeError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    pager = PageView.of(page, base_path=GROUPS_PATH, limit=limit, cursor=cursor)
    if is_htmx_request(request):
        return no_store(
            render_fragment(
                request,
                TABLES,
                "groups_table",
                rows=page.items,
                columns=GROUP_COLUMNS,
                pager=pager,
            )
        )
    total = await services.groups.count(actor)
    return _page(
        request,
        "groups/groups.html",
        {"rows": page.items, "columns": GROUP_COLUMNS, "pager": pager, "total": total},
    )


@router.get("/new", response_class=HTMLResponse, name="group_new")
async def new_group_form(request: Request) -> Response:
    """Render the create-group form."""
    return _new_form(request)


@router.post("/new", name="group_create")
async def create_group(
    request: Request,
    services: Environments,
    actor: CurrentAdministrator,
    name: Field = "",
    description: Field = "",
) -> Response:
    """Create a group and open its page."""
    try:
        created = await services.groups.create(actor, name=name, description=description)
    except ValidationFailed as invalid:
        return _new_form(
            request, name=name, description=description, fields=invalid.fields, status_code=422
        )
    return redirect(request, f"{GROUPS_PATH}/{created.id}", flashes=[Flash("ok", "Group created.")])


@router.get("/{group_id}", response_class=HTMLResponse, name="group_detail")
async def group_detail(
    request: Request,
    services: Environments,
    actor: CurrentAdministrator,
    group_id: PathId,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    cursor: str | None = None,
    search: Annotated[str | None, Query(alias="candidate_q", max_length=SEARCH_MAX)] = None,
) -> Response:
    """Show a group with its environments, its access and its controls."""
    return await _detail(
        request, services, actor, group_id, limit=limit, cursor=cursor, search=search
    )


@router.post("/{group_id}/edit", name="group_update")
async def update_group(
    request: Request,
    services: Environments,
    actor: CurrentAdministrator,
    group_id: PathId,
    name: Field = "",
    description: Field = "",
) -> Response:
    """Rename a group or change its description."""
    try:
        await services.groups.update(actor, group_id, name=name, description=description)
    except ValidationFailed as invalid:
        return await _detail(
            request,
            services,
            actor,
            group_id,
            status_code=422,
            fields=invalid.fields,
            name=name,
            description=description,
        )
    return _done(request, group_id, Flash("ok", "Group saved."))


@router.post("/{group_id}/environments", name="group_environments")
async def assign_environments(
    request: Request,
    services: Environments,
    actor: CurrentAdministrator,
    group_id: PathId,
    environment: Annotated[list[BoundedId] | None, Form(max_length=ID_PARAMS_MAX)] = None,
) -> Response:
    """Place the chosen environments in the group, each as its own operation."""
    try:
        result = await services.groups.assign_environments(actor, group_id, environment or [])
    except ValidationFailed as invalid:
        return await _detail(
            request,
            services,
            actor,
            group_id,
            status_code=422,
            fields=invalid.fields,
        )
    moved = sum(1 for item in result.items if item.outcome == "assigned")
    refused = len(result.items) - moved
    notices = [
        Flash(
            "ok", f"Moved {moved} {'environment' if moved == 1 else 'environments'} into the group."
        )
    ]
    if refused:
        notices.append(
            Flash(
                "warn",
                f"{refused} {'environment' if refused == 1 else 'environments'} could not be "
                "moved. Reload the page and try again.",
            )
        )
    return _done(request, group_id, *notices)


@router.post("/{group_id}/access", name="group_access_grant")
async def grant_access(
    request: Request,
    services: Environments,
    actor: CurrentAdministrator,
    group_id: PathId,
    grantee_kind: Field = "user",
    grantee_name: Field = "",
) -> Response:
    """Grant a user or a team access to every environment in the group."""
    try:
        user_id, team_id = await grant_arguments(services, actor, grantee_kind, grantee_name)
        await services.groups.grant(actor, group_id, user_id=user_id, team_id=team_id)
    except ValidationFailed as invalid:
        return await _detail(
            request,
            services,
            actor,
            group_id,
            status_code=422,
            fields=invalid.fields,
            grantee_kind=grantee_kind,
            grantee_name=grantee_name,
        )
    return _done(request, group_id, Flash("ok", "Access granted."), open_access=True)


@router.post("/{group_id}/access/remove", name="group_access_revoke")
async def revoke_access(
    request: Request,
    services: Environments,
    actor: CurrentAdministrator,
    group_id: PathId,
    user_id: QueryId = None,
    team_id: QueryId = None,
) -> Response:
    """Withdraw a user's or a team's grant on the group; the grantee is named in the query."""
    try:
        await services.groups.revoke(actor, group_id, user_id=user_id, team_id=team_id)
    except ValidationFailed as invalid:
        return await _detail(
            request,
            services,
            actor,
            group_id,
            status_code=422,
            fields=invalid.fields,
        )
    return _done(request, group_id, Flash("ok", "Access removed."), open_access=True)


async def _confirm_delete_page(
    request: Request,
    services: EnvironmentServices,
    actor: Principal,
    group_id: int,
    *,
    fields: dict[str, str] | None = None,
) -> Response:
    preview = await services.groups.delete_preview(actor, group_id)
    return _page(
        request,
        "groups/delete_confirm.html",
        {"preview": preview, "fields": fields or {}},
        422 if fields else 200,
    )


@router.get("/{group_id}/delete", response_class=HTMLResponse, name="group_delete_confirm")
async def confirm_delete(
    request: Request, services: Environments, actor: CurrentAdministrator, group_id: PathId
) -> Response:
    """List what deleting the group does; nothing is deleted by this page."""
    return await _confirm_delete_page(request, services, actor, group_id)


@router.post("/{group_id}/delete", name="group_delete")
async def delete_group(
    request: Request,
    services: Environments,
    actor: CurrentAdministrator,
    group_id: PathId,
    expected_name: Field = "",
) -> Response:
    """Delete the group when its name is still the one confirmed; its environments are ungrouped."""
    try:
        await services.groups.delete(actor, group_id, expected_name=expected_name)
    except ValidationFailed as invalid:
        return await _confirm_delete_page(request, services, actor, group_id, fields=invalid.fields)
    return redirect(request, GROUPS_PATH, flashes=[Flash("ok", "Group deleted.")])
