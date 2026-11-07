"""Team pages: any member may look, and the service decides who may change what."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, require_administrator, require_user
from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE, InvalidCursorError
from app.services.assembly import Access, AccessServices
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.navigation import no_store, redirect
from app.web.pagination import DEFAULT_LIMIT, Column, PageView
from app.web.templating import is_htmx_request, render_fragment, render_page

router = APIRouter(prefix="/teams", dependencies=[Depends(require_user)])

TEAMS_PATH = "/teams"
FIELD_MAX = 256
TEAM_COLUMNS = (Column("name", "Team"), Column("created_at", "Created", width="time"))
TEAM_TABLES = "teams/_tables.html"

Field = Annotated[str, Form(max_length=FIELD_MAX)]


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


def _done(request: Request, team_id: int, message: str) -> Response:
    return redirect(request, f"{TEAMS_PATH}/{team_id}", flashes=[Flash("ok", message)])


def _roster_columns(can_manage_members: bool) -> tuple[Column, ...]:
    columns = (
        Column("username", "Member"),
        Column("role", "Role", width="short"),
        Column("joined", "Joined", width="time"),
    )
    if can_manage_members:
        return (*columns, Column("actions", "Actions", width="actions"))
    return columns


async def _detail(
    request: Request,
    access: AccessServices,
    actor: Principal,
    team_id: PathId,
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    status_code: int = 200,
    error: str | None = None,
    fields: dict[str, str] | None = None,
    name: str | None = None,
    member_username: str = "",
    member_is_leader: bool = False,
) -> Response:
    detail = await access.teams.get(actor, team_id)
    try:
        roster = await access.teams.list_members(actor, team_id, limit=limit, cursor=cursor)
    except (InvalidCursorError, TypeError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    pager = PageView.of(roster, base_path=f"{TEAMS_PATH}/{team_id}", limit=limit, cursor=cursor)
    columns = _roster_columns(detail.can_manage_members)
    if is_htmx_request(request):
        return no_store(
            render_fragment(
                request,
                TEAM_TABLES,
                "roster_table",
                rows=roster.items,
                columns=columns,
                pager=pager,
                team=detail.team,
                can_manage_members=detail.can_manage_members,
                can_manage_leaders=detail.can_manage_leaders,
            )
        )
    return _page(
        request,
        "teams/team_detail.html",
        {
            "detail": detail,
            "team": detail.team,
            "rows": roster.items,
            "columns": columns,
            "pager": pager,
            "name": detail.team.name if name is None else name,
            "member_username": member_username,
            "member_is_leader": member_is_leader,
            "error": error,
            "fields": fields or {},
        },
        status_code=status_code,
    )


def _new_form(
    request: Request,
    *,
    name: str = "",
    fields: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return _page(
        request, "teams/team_new.html", {"name": name, "fields": fields or {}}, status_code
    )


@router.get("", response_class=HTMLResponse, name="teams")
async def list_teams(
    request: Request,
    access: Access,
    actor: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Response:
    """List the teams the caller may see: all of them for an administrator, else their own."""
    try:
        page = await access.teams.list_page(actor, limit=limit, cursor=cursor)
    except (InvalidCursorError, TypeError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    pager = PageView.of(page, base_path=TEAMS_PATH, limit=limit, cursor=cursor)
    can_create = actor.is_administrator
    if is_htmx_request(request):
        return no_store(
            render_fragment(
                request,
                TEAM_TABLES,
                "teams_table",
                rows=page.items,
                columns=TEAM_COLUMNS,
                pager=pager,
                can_create=can_create,
            )
        )
    total = await access.teams.count(actor)
    return _page(
        request,
        "teams/teams.html",
        {
            "rows": page.items,
            "columns": TEAM_COLUMNS,
            "pager": pager,
            "can_create": can_create,
            "total": total,
        },
    )


@router.get(
    "/new",
    response_class=HTMLResponse,
    name="team_new",
    dependencies=[Depends(require_administrator)],
)
async def new_team_form(request: Request) -> Response:
    """Render the create-team form."""
    return _new_form(request)


@router.post("", name="team_create", dependencies=[Depends(require_administrator)])
async def create_team(
    request: Request, access: Access, actor: CurrentUser, name: Field = ""
) -> Response:
    """Create a team and open its page."""
    try:
        created = await access.teams.create(actor, name=name)
    except ValidationFailed as invalid:
        return _new_form(request, name=name, fields=invalid.fields, status_code=422)
    return redirect(request, f"{TEAMS_PATH}/{created.id}", flashes=[Flash("ok", "Team created.")])


@router.get("/{team_id}", response_class=HTMLResponse, name="team_detail")
async def team_detail(
    request: Request,
    access: Access,
    actor: CurrentUser,
    team_id: PathId,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Response:
    """Show a team with its roster and the controls the caller may use."""
    return await _detail(request, access, actor, team_id, limit=limit, cursor=cursor)


@router.post("/{team_id}", name="team_rename")
async def rename_team(
    request: Request, access: Access, actor: CurrentUser, team_id: PathId, name: Field = ""
) -> Response:
    """Rename a team."""
    try:
        await access.teams.rename(actor, team_id, name=name)
    except ValidationFailed as invalid:
        return await _detail(
            request, access, actor, team_id, status_code=422, fields=invalid.fields, name=name
        )
    return _done(request, team_id, "Team renamed.")


@router.post("/{team_id}/delete", name="team_delete")
async def delete_team(
    request: Request,
    access: Access,
    actor: CurrentUser,
    team_id: PathId,
    expected_name: Field = "",
) -> Response:
    """Delete a team, its memberships and the grants made to it, if its name is as confirmed."""
    try:
        await access.teams.delete(actor, team_id, expected_name=expected_name)
    except ValidationFailed as invalid:
        return await _detail(
            request, access, actor, team_id, status_code=409, error=invalid.fields["name"]
        )
    return redirect(request, TEAMS_PATH, flashes=[Flash("ok", "Team deleted.")])


@router.post("/{team_id}/members", name="team_member_add")
async def add_member(
    request: Request,
    access: Access,
    actor: CurrentUser,
    team_id: PathId,
    username: Field = "",
    is_leader: Field = "",
) -> Response:
    """Add an enabled user to a team, as a leader only when the form asks and the caller may."""
    leader = is_leader == "on"
    try:
        added = await access.teams.add_member(actor, team_id, username=username, is_leader=leader)
    except ValidationFailed as invalid:
        return await _detail(
            request,
            access,
            actor,
            team_id,
            status_code=422,
            fields=invalid.fields,
            member_username=username,
            member_is_leader=leader,
        )
    return _done(request, team_id, f"Added {added.username} to the team.")


@router.post("/{team_id}/members/{user_id}/remove", name="team_member_remove")
async def remove_member(
    request: Request, access: Access, actor: CurrentUser, team_id: PathId, user_id: PathId
) -> Response:
    """Remove a member from a team."""
    try:
        await access.teams.remove_member(actor, team_id, user_id=user_id)
    except ValidationFailed as invalid:
        return await _detail(
            request, access, actor, team_id, status_code=422, fields=invalid.fields
        )
    return _done(request, team_id, "Member removed.")


@router.post("/{team_id}/members/{user_id}/leader", name="team_member_leader")
async def set_leader(
    request: Request,
    access: Access,
    actor: CurrentUser,
    team_id: PathId,
    user_id: PathId,
    is_leader: Field = "",
) -> Response:
    """Appoint or dismiss a team leader; the form says which with is_leader=true or false."""
    leader = is_leader == "true"
    try:
        await access.teams.set_leader(actor, team_id, user_id=user_id, is_leader=leader)
    except ValidationFailed as invalid:
        return await _detail(
            request, access, actor, team_id, status_code=422, fields=invalid.fields
        )
    return _done(request, team_id, "Leader appointed." if leader else "Leader dismissed.")
