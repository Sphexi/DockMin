"""Environment pages: the dashboard, detail, create and edit, connection tests, access, deletion."""

import asyncio
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import (
    CurrentAdministrator,
    CurrentUser,
    require_administrator,
    require_user,
)
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import InvalidCursorError, Page
from app.services.activity_log import ActivityLogService
from app.services.assembly import ActivityLog, Environments, EnvironmentServices, Overview
from app.services.environment_connector import ConnectionTestFailed, ConnectionTestReport
from app.services.environment_overview import EnvironmentOverview
from app.services.environments import (
    MAX_BATCH,
    ConnectionInput,
    DashboardFilters,
    EnvironmentSpec,
    TlsInput,
    local_socket_endpoint,
    local_socket_state,
)
from app.services.navigation import load_navigation_environment
from app.web.activity_routes import build_activity_panel
from app.web.flash import Flash
from app.web.identifiers import BoundedId, PathId, QueryId
from app.web.nav_keys import ROUTE_PREFIXES
from app.web.navigation import no_store, redirect
from app.web.object_pages import HX_TARGET
from app.web.origins import RequestOrigin
from app.web.pagination import Column, PageView
from app.web.results import show_result
from app.web.sidebar import snapshot_pokes
from app.web.templating import is_htmx_request, render_fragment, render_page, render_partial

router = APIRouter(prefix="/environments", dependencies=[Depends(require_user)])

ENVIRONMENTS_PATH = "/environments"
DASHBOARD_PATH = "/"
UNGROUPED = "none"
ACCESS_OPEN_SUFFIX = "?open=access#access"
DEFAULT_SOCKET_PATH = "/var/run/docker.sock"
DEFAULT_TLS_PORT = "2376"
FIELD_MAX = 256
TLS_ITEM_MAX = 64 * 1024
ID_DIGITS = 18
GRANTEE_KINDS = ("user", "team")
TAG_PARAMS_MAX = 50
SHOWN_WARNINGS = 2
BATCH_PARAMS_MAX = MAX_BATCH + 1
COUNTS_WAIT_SECONDS = 10.0
NAVIGATION_KEY_MAX = 32
NAVIGATION_KEYS = frozenset(key for _, key in ROUTE_PREFIXES)

TABLES = "environments/_tables.html"
PARTS = "environments/_parts.html"
OVERVIEW = "environments/_overview.html"
OVERVIEW_REGION = "environment-overview"

ADMIN_COLUMNS = (
    Column("select", "Select"),
    Column("name", "Environment"),
    Column("status", "Status", width="badge"),
    Column("group", "Group", optional=True),
    Column("tags", "Tags", optional=True),
    Column("containers", "Containers", numeric=True, optional=True, width="count"),
    Column("images", "Images", numeric=True, optional=True, width="count"),
    Column("volumes", "Volumes", numeric=True, optional=True, width="count", wide_only=True),
    Column("networks", "Networks", numeric=True, optional=True, width="count", wide_only=True),
    Column("stacks", "Stacks", numeric=True, optional=True, width="count"),
    Column("contact", "Last contact", optional=True, width="time"),
)
STANDARD_COLUMNS = (
    Column("name", "Environment"),
    Column("status", "Status", width="badge"),
    Column("group", "Group", optional=True),
    Column("tags", "Tags", optional=True),
    Column("contact", "Last contact", optional=True, width="time"),
)

Field = Annotated[str, Form(max_length=FIELD_MAX)]


@dataclass(slots=True)
class SubmittedEnvironment:
    """The create and edit form as posted; the TLS items are never copied into a response."""

    name: str
    kind: str
    socket_path: str
    host: str
    port: str
    reviewed_endpoint: str | None
    save_anyway: bool
    group: str
    tag_ids: list[int]
    tls: TlsInput = field(repr=False)
    tls_errors: dict[str, str]


def _digits(value: str) -> bool:
    return value.isascii() and value.isdigit() and len(value) <= ID_DIGITS


def _group_id(value: str) -> int | None:
    """Return the group a form names, None for ungrouped; ValidationFailed when it is neither."""
    text = value.strip()
    if not text:
        return None
    if not _digits(text):
        raise ValidationFailed({"group": "Choose a group from the list."})
    return int(text)


async def grant_arguments(
    services: EnvironmentServices, actor: Principal, kind: str, name: str
) -> tuple[int | None, int | None]:
    """Resolve the user or team a grant form names to the `(user_id, team_id)` of a grant."""
    text = name.strip()
    if kind not in GRANTEE_KINDS:
        raise ValidationFailed({"grantee": "Choose whether the grant is for a user or a team."})
    if not text:
        raise ValidationFailed(
            {"grantee": "Enter a username." if kind == "user" else "Enter a team name."}
        )
    if kind == "user":
        grantee = await services.access.resolve_grantee(actor, username=text)
    else:
        grantee = await services.access.resolve_grantee(actor, team_name=text)
    return grantee.user_id, grantee.team_id


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


def _warning_flash(warnings: tuple[str, ...]) -> list[Flash]:
    """Fold the warnings into one notice, so the others a redirect carries are never displaced."""
    if not warnings:
        return []
    shown = warnings[:SHOWN_WARNINGS]
    more = len(warnings) - len(shown)
    suffix = f" and {more} more." if more else ""
    return [Flash("warn", " ".join(shown) + suffix)]


def _detail_redirect(
    request: Request,
    environment_id: int,
    *flashes: Flash,
    warnings: tuple[str, ...] = (),
    open_access: bool = False,
) -> Response:
    location = f"{ENVIRONMENTS_PATH}/{environment_id}"
    if open_access:
        location += ACCESS_OPEN_SUFFIX
    return redirect(
        request,
        location,
        flashes=[*flashes, *_warning_flash(warnings)],
    )


def _fragment(
    request: Request,
    template: str,
    macro: str,
    status_code: int = 200,
    **arguments: Any,
) -> Response:
    return no_store(render_fragment(request, template, macro, status_code=status_code, **arguments))


async def _first_run_panel(
    services: EnvironmentServices, actor: Principal, *, total: int | None, filtered: bool
) -> dict[str, str] | None:
    """Describe the local socket when an administrator has no environment yet, else none."""
    if not actor.is_administrator or total != 0:
        return None
    if filtered and await services.environments.dashboard_count(actor) != 0:
        return None
    return {
        "state": await asyncio.to_thread(local_socket_state, DEFAULT_SOCKET_PATH),
        "socket_path": DEFAULT_SOCKET_PATH,
        "endpoint": local_socket_endpoint(DEFAULT_SOCKET_PATH),
    }


async def dashboard_page(
    request: Request,
    services: EnvironmentServices,
    actor: Principal,
    *,
    q: str | None,
    group: str | None,
    status: str | None,
    tags: list[str],
    limit: int,
    cursor: str | None,
) -> Response:
    """Render the environment list for the caller, as a full page or as the table fragment."""
    text = (q or "").strip()
    group_value = (group or "").strip()
    status_value = (status or "").strip() or None
    fields: dict[str, str] = {}
    group_id: int | None = None
    if group_value and group_value != UNGROUPED:
        if _digits(group_value):
            group_id = int(group_value)
        else:
            fields["group"] = "Choose a group from the list."
    tag_ids: list[int] = []
    for raw in tags:
        if _digits(raw):
            tag_ids.append(int(raw))
        else:
            fields["tag"] = "Choose tags from the list."

    page: Page[Any] = Page([], None)
    total: int | None = None
    if not fields:
        filters = DashboardFilters(
            q=text or None,
            group_id=group_id,
            ungrouped=group_value == UNGROUPED,
            status=status_value,
            tag_ids=tuple(tag_ids),
        )
        try:
            page = await services.environments.dashboard(actor, filters, limit=limit, cursor=cursor)
            total = await services.environments.dashboard_count(actor, filters)
        except ValidationFailed as invalid:
            fields = invalid.fields
        except (InvalidCursorError, TypeError):
            raise HTTPException(status_code=400, detail="invalid cursor") from None

    params: list[tuple[str, str]] = []
    if text:
        params.append(("q", text))
    if group_value and "group" not in fields:
        params.append(("group", group_value))
    if status_value:
        params.append(("status", status_value))
    if "tag" not in fields:
        params.extend(("tag", str(tag_id)) for tag_id in tag_ids)
    pager = PageView.of(
        page,
        base_path=DASHBOARD_PATH,
        limit=limit,
        cursor=None if fields else cursor,
        params=params,
    )
    is_admin = actor.is_administrator
    columns = ADMIN_COLUMNS if is_admin else STANDARD_COLUMNS
    filtered = bool(params)
    status_code = 422 if fields else 200
    if is_htmx_request(request):
        return _fragment(
            request,
            TABLES,
            "environments_results",
            status_code,
            rows=page.items,
            columns=columns,
            pager=pager,
            is_admin=is_admin,
            filtered=filtered,
            errors=tuple(fields.values()),
            total=total,
            oob=True,
        )
    return _page(
        request,
        "environments/dashboard.html",
        {
            "rows": page.items,
            "columns": columns,
            "pager": pager,
            "is_admin": is_admin,
            "filtered": filtered,
            "errors": tuple(fields.values()),
            "total": total,
            "options": await services.environments.filter_options(actor),
            "q": text,
            "group": group_value,
            "status": status_value or "",
            "selected_tags": tuple(tag_ids),
            "ungrouped": UNGROUPED,
            "first_run": await _first_run_panel(services, actor, total=total, filtered=filtered),
        },
        status_code,
    )


async def _read_item(text: str, upload: UploadFile | None) -> tuple[str, str | None]:
    """Return one TLS item from its upload or its paste, or a message when it cannot be used."""
    if upload is not None and upload.filename:
        data = await upload.read(TLS_ITEM_MAX + 1)
        if len(data) > TLS_ITEM_MAX:
            return "", "That file is larger than 64 KiB."
        try:
            return data.decode("utf-8"), None
        except UnicodeDecodeError:
            return "", "That file is not text. Upload a PEM file."
    if len(text) > TLS_ITEM_MAX:
        return "", "That text is longer than 64 KiB."
    return text, None


async def read_environment_form(
    name: Field = "",
    kind: Field = "",
    socket_path: Field = "",
    host: Field = "",
    port: Field = "",
    reviewed_endpoint: Annotated[str | None, Form(max_length=FIELD_MAX)] = None,
    save_anyway: Field = "",
    group: Field = "",
    tag: Annotated[list[BoundedId] | None, Form(max_length=TAG_PARAMS_MAX)] = None,
    ca_certificate: Annotated[str, Form()] = "",
    client_certificate: Annotated[str, Form()] = "",
    client_key: Annotated[str, Form()] = "",
    ca_certificate_file: Annotated[UploadFile | None, File()] = None,
    client_certificate_file: Annotated[UploadFile | None, File()] = None,
    client_key_file: Annotated[UploadFile | None, File()] = None,
) -> SubmittedEnvironment:
    """Read the environment form, taking each TLS item from its file when one was chosen."""
    errors: dict[str, str] = {}
    items: dict[str, str] = {}
    sources = (
        ("ca_certificate", ca_certificate, ca_certificate_file),
        ("client_certificate", client_certificate, client_certificate_file),
        ("client_key", client_key, client_key_file),
    )
    for key, text, upload in sources:
        items[key], message = await _read_item(text, upload)
        if message is not None:
            errors[key] = message
    return SubmittedEnvironment(
        name=name,
        kind=kind,
        socket_path=socket_path,
        host=host,
        port=port,
        reviewed_endpoint=reviewed_endpoint,
        save_anyway=save_anyway == "on",
        group=group,
        tag_ids=list(tag or []),
        tls=TlsInput(**items),
        tls_errors=errors,
    )


Submitted = Annotated[SubmittedEnvironment, Depends(read_environment_form)]


def _spec(submitted: SubmittedEnvironment) -> EnvironmentSpec:
    return EnvironmentSpec(
        name=submitted.name,
        connection=ConnectionInput(
            kind=submitted.kind,
            socket_path=submitted.socket_path,
            host=submitted.host,
            port=submitted.port,
        ),
        tls=submitted.tls,
        reviewed_endpoint=submitted.reviewed_endpoint,
        save_anyway=submitted.save_anyway,
    )


async def _form_response(
    request: Request,
    services: EnvironmentServices,
    actor: Principal,
    *,
    environment_id: int | None,
    submitted: SubmittedEnvironment | None,
    fields: dict[str, str] | None = None,
    report: ConnectionTestReport | None = None,
    status_code: int = 200,
) -> Response:
    """Render the create or edit form; TLS inputs are never given a value."""
    detail = None
    if environment_id is not None:
        detail = await services.environments.get(actor, environment_id)
        if not detail.can_manage:
            raise PermissionDenied("administrator role required")
    if submitted is not None:
        values: dict[str, Any] = {
            "name": submitted.name,
            "kind": submitted.kind or "unix_socket",
            "socket_path": submitted.socket_path,
            "host": submitted.host,
            "port": submitted.port,
            "save_anyway": submitted.save_anyway,
            "group": submitted.group,
            "tag_ids": tuple(submitted.tag_ids),
        }
    elif detail is not None:
        summary = detail.summary
        connection = summary.connection
        values = {
            "name": summary.name,
            "kind": "unix_socket" if connection is None else connection.kind,
            "socket_path": DEFAULT_SOCKET_PATH
            if connection is None or connection.socket_path is None
            else connection.socket_path,
            "host": "" if connection is None or connection.host is None else connection.host,
            "port": DEFAULT_TLS_PORT
            if connection is None or connection.port is None
            else str(connection.port),
            "save_anyway": False,
            "group": "" if summary.group is None else str(summary.group.id),
            "tag_ids": tuple(tag.id for tag in summary.tags),
        }
    else:
        values = {
            "name": "",
            "kind": "unix_socket",
            "socket_path": DEFAULT_SOCKET_PATH,
            "host": "",
            "port": DEFAULT_TLS_PORT,
            "save_anyway": False,
            "group": "",
            "tag_ids": (),
        }

    preview = None
    preview_errors: tuple[str, ...] = ()
    try:
        preview = await services.environments.preview(
            actor,
            ConnectionInput(
                kind=values["kind"],
                socket_path=values["socket_path"],
                host=values["host"],
                port=values["port"],
            ),
            environment_id=environment_id,
        )
    except ValidationFailed as invalid:
        preview_errors = tuple(invalid.fields.values())

    return _page(
        request,
        "environments/form.html",
        {
            "detail": detail,
            "environment": None if detail is None else detail.summary,
            "values": values,
            "fields": fields or {},
            "report": report,
            "preview": preview,
            "preview_errors": preview_errors,
            "options": await services.environments.filter_options(actor),
            "tls_reset": submitted is not None and values["kind"] == "tcp_tls",
            "default_socket_path": DEFAULT_SOCKET_PATH,
        },
        status_code,
    )


async def _detail_page(
    request: Request,
    services: EnvironmentServices,
    overview: EnvironmentOverview,
    actor: Principal,
    environment_id: int,
    *,
    status_code: int = 200,
    fields: dict[str, str] | None = None,
    report: ConnectionTestReport | None = None,
    grantee_kind: str = "user",
    grantee_name: str = "",
    activity: ActivityLogService | None = None,
) -> Response:
    detail = await services.environments.get(actor, environment_id)
    access_view = None
    registry_addresses: tuple[str, ...] = ()
    if actor.is_administrator:
        access_view = await services.access.for_environment(actor, environment_id)
        registry_addresses = await services.environments.registry_addresses(actor, environment_id)
    return _page(
        request,
        "environments/detail.html",
        {
            "detail": detail,
            "registry_addresses": registry_addresses,
            "environment": detail.summary,
            "overview": await overview.overview(actor, environment_id),
            "access_view": access_view,
            "report": report,
            "fields": fields or {},
            "grantee_kind": grantee_kind,
            "grantee_name": grantee_name,
            "activity_log": (
                None
                if activity is None
                else await build_activity_panel(
                    request, activity, actor, "environment", environment_id
                )
            ),
        },
        status_code,
    )


@router.get(
    "/new",
    response_class=HTMLResponse,
    name="environment_new",
    dependencies=[Depends(require_administrator)],
)
async def new_environment_form(
    request: Request, services: Environments, actor: CurrentAdministrator
) -> Response:
    """Render the create form with the default local socket already previewed."""
    return await _form_response(request, services, actor, environment_id=None, submitted=None)


@router.post("/preview", name="environment_preview", dependencies=[Depends(require_administrator)])
async def preview_endpoint(
    request: Request,
    services: Environments,
    actor: CurrentAdministrator,
    submitted: Submitted,
    environment_id: Annotated[str, Form(max_length=ID_DIGITS + 1)] = "",
) -> Response:
    """Show the endpoint the connection fields parse to; the TLS items are not kept or echoed."""
    target = int(environment_id) if _digits(environment_id) else None
    if not is_htmx_request(request):
        return await _form_response(
            request, services, actor, environment_id=target, submitted=submitted
        )
    connection = ConnectionInput(
        kind=submitted.kind,
        socket_path=submitted.socket_path,
        host=submitted.host,
        port=submitted.port,
    )
    try:
        preview = await services.environments.preview(actor, connection, environment_id=target)
    except ValidationFailed as invalid:
        return _fragment(
            request,
            PARTS,
            "endpoint_preview",
            422,
            preview=None,
            errors=tuple(invalid.fields.values()),
        )
    return _fragment(request, PARTS, "endpoint_preview", preview=preview, errors=())


@router.post("", name="environment_create", dependencies=[Depends(require_administrator)])
async def create_environment(
    request: Request, services: Environments, actor: CurrentAdministrator, submitted: Submitted
) -> Response:
    """Create an environment after the connection test, then open its page."""

    async def failed(
        fields: dict[str, str] | None = None, report: ConnectionTestReport | None = None
    ) -> Response:
        return await _form_response(
            request,
            services,
            actor,
            environment_id=None,
            submitted=submitted,
            fields=fields,
            report=report,
            status_code=422,
        )

    if submitted.tls_errors:
        return await failed(submitted.tls_errors)
    try:
        created = await services.environments.create(actor, _spec(submitted))
    except ConnectionTestFailed as refused:
        return await failed(report=refused.report)
    except ValidationFailed as invalid:
        return await failed(invalid.fields)
    return _detail_redirect(request, created.summary.id, warnings=created.tls_warnings)


@router.get(
    "/delete",
    response_class=HTMLResponse,
    name="environment_delete_confirm",
    dependencies=[Depends(require_administrator)],
)
async def confirm_delete(
    request: Request,
    services: Environments,
    actor: CurrentAdministrator,
    ids: Annotated[list[BoundedId] | None, Query(alias="id", max_length=BATCH_PARAMS_MAX)] = None,
) -> Response:
    """List what deleting the chosen environments takes away; nothing is deleted by this page."""
    fields: dict[str, str] = {}
    preview = None
    try:
        preview = await services.environments.delete_preview(actor, ids or [])
    except ValidationFailed as invalid:
        fields = invalid.fields
    return _page(
        request,
        "environments/delete_confirm.html",
        {"preview": preview, "fields": fields},
        422 if fields else 200,
    )


@router.post("/delete", name="environment_delete", dependencies=[Depends(require_administrator)])
async def delete_environments(
    request: Request,
    services: Environments,
    actor: CurrentAdministrator,
    ids: Annotated[list[BoundedId] | None, Form(alias="id", max_length=BATCH_PARAMS_MAX)] = None,
    names: Annotated[
        list[str] | None, Form(alias="expected_name", max_length=BATCH_PARAMS_MAX)
    ] = None,
) -> Response:
    """Delete the confirmed environments, each on its own, and report one result per environment."""
    chosen = ids or []
    if len(chosen) != len(names or []):
        return _page(
            request,
            "environments/delete_confirm.html",
            {
                "preview": None,
                "fields": {
                    "selection": "The confirmation was incomplete. Review the selection again."
                },
            },
            422,
        )
    try:
        result = await services.environments.delete_many(
            actor, chosen, expected_names=dict(zip(chosen, names or [], strict=True))
        )
    except ValidationFailed as invalid:
        return _page(
            request,
            "environments/delete_confirm.html",
            {"preview": None, "fields": invalid.fields},
            422,
        )
    return show_result(request, "environments/delete_result.html", {"result": result})


@router.get(
    "/{environment_id}",
    response_class=HTMLResponse,
    name="environment_detail",
    dependencies=[RequestOrigin],
)
async def environment_detail(
    request: Request,
    services: Environments,
    overview: Overview,
    activity: ActivityLog,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show an environment the caller may see, with the controls their role allows."""
    if is_htmx_request(request) and request.headers.get(HX_TARGET) == OVERVIEW_REGION:
        detail = await services.environments.get(actor, environment_id)
        return _fragment(
            request,
            OVERVIEW,
            "overview_body",
            overview=await overview.overview(actor, environment_id),
            environment=detail.summary,
        )
    return await _detail_page(request, services, overview, actor, environment_id, activity=activity)


@router.get(
    "/{environment_id}/navigation-counts",
    response_class=HTMLResponse,
    name="environment_navigation_counts",
)
async def environment_navigation_counts(
    request: Request,
    actor: CurrentUser,
    environment_id: PathId,
    active: Annotated[str, Query(max_length=NAVIGATION_KEY_MAX)] = "",
) -> Response:
    """Render the sidebar's list for the environment, once any refresh pending for it has ended.

    Counts are shown to administrators only; everyone else gets an empty 204. It never starts a
    refresh, and waits at most COUNTS_WAIT_SECONDS for one that is pending.
    """
    if not actor.is_administrator:
        return no_store(Response(status_code=204))
    pokes = snapshot_pokes(request)
    if pokes is not None:
        await pokes.wait_settled(environment_id, COUNTS_WAIT_SECONDS)
    async with request.app.state.database.session() as db:
        environment = await load_navigation_environment(db, actor, environment_id)
    if environment is None:
        raise ResourceNotFound("not found")
    return no_store(
        render_partial(
            request,
            "navigation_counts",
            environment=environment,
            active=active if active in NAVIGATION_KEYS else "",
        )
    )


@router.get("/{environment_id}/edit", response_class=HTMLResponse, name="environment_edit")
async def edit_environment_form(
    request: Request, services: Environments, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Render the edit form; the TLS items are empty, and empty keeps what is stored."""
    return await _form_response(
        request, services, actor, environment_id=environment_id, submitted=None
    )


@router.post("/{environment_id}/edit", name="environment_update")
async def update_environment(
    request: Request,
    services: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    submitted: Submitted,
) -> Response:
    """Save the connection, group and tags; the connection is tested again only if it changed."""

    async def failed(
        fields: dict[str, str] | None = None,
        report: ConnectionTestReport | None = None,
    ) -> Response:
        return await _form_response(
            request,
            services,
            actor,
            environment_id=environment_id,
            submitted=submitted,
            fields=fields,
            report=report,
            status_code=422,
        )

    if submitted.tls_errors:
        return await failed(submitted.tls_errors)
    try:
        group_id = _group_id(submitted.group)
        await services.environments.check_assignment(
            actor, group_id=group_id, tag_ids=submitted.tag_ids, environment_id=environment_id
        )
        saved = await services.environments.update(actor, environment_id, _spec(submitted))
    except ConnectionTestFailed as refused:
        return await failed(report=refused.report)
    except ValidationFailed as invalid:
        return await failed(invalid.fields)

    try:
        current_group = None if saved.summary.group is None else saved.summary.group.id
        if group_id != current_group:
            await services.environments.assign_group(actor, environment_id, group_id)
        wanted_tags = set(submitted.tag_ids)
        if wanted_tags != {tag.id for tag in saved.summary.tags}:
            await services.environments.set_tags(actor, environment_id, wanted_tags)
    except ValidationFailed as invalid:
        return _detail_redirect(
            request,
            environment_id,
            Flash(
                "warn",
                "The connection settings were saved. The group and tags were not changed: "
                + " ".join(invalid.fields.values()),
            ),
            warnings=saved.tls_warnings,
        )
    except ResourceNotFound:
        return _detail_redirect(
            request,
            environment_id,
            Flash(
                "ok",
                "The connection settings were saved. The group and tags were not changed "
                "because something they refer to was removed.",
            ),
            warnings=saved.tls_warnings,
        )
    return _detail_redirect(request, environment_id, warnings=saved.tls_warnings)


@router.post("/{environment_id}/test", name="environment_test")
async def test_connection(
    request: Request,
    services: Environments,
    overview: Overview,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Test the saved connection now; htmx receives the staged report, a browser the page."""
    report = await services.environments.test_connection(actor, environment_id)
    if is_htmx_request(request):
        detail = await services.environments.get(actor, environment_id)
        return _fragment(
            request,
            PARTS,
            "test_connection_response",
            report=report,
            environment=detail.summary,
        )
    return await _detail_page(request, services, overview, actor, environment_id, report=report)


@router.post("/{environment_id}/refresh", name="environment_refresh")
async def refresh_environment(
    request: Request,
    services: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Ask for the environment's counts to be refreshed soon."""
    await services.environments.refresh(actor, environment_id)
    return _detail_redirect(
        request, environment_id, Flash("ok", "Refresh requested. The counts update shortly.")
    )


@router.post("/{environment_id}/access", name="environment_access_grant")
async def grant_access(
    request: Request,
    services: Environments,
    overview: Overview,
    actor: CurrentUser,
    environment_id: PathId,
    grantee_kind: Field = "user",
    grantee_name: Field = "",
) -> Response:
    """Grant a user or a team direct access to the environment."""
    target = await services.environments.get(actor, environment_id)
    if not target.can_manage:
        raise PermissionDenied("administrator role required")
    try:
        user_id, team_id = await grant_arguments(services, actor, grantee_kind, grantee_name)
        await services.environments.grant(actor, environment_id, user_id=user_id, team_id=team_id)
    except ValidationFailed as invalid:
        return await _detail_page(
            request,
            services,
            overview,
            actor,
            environment_id,
            status_code=422,
            fields=invalid.fields,
            grantee_kind=grantee_kind,
            grantee_name=grantee_name,
        )
    return _detail_redirect(
        request, environment_id, Flash("ok", "Access granted."), open_access=True
    )


@router.post("/{environment_id}/access/remove", name="environment_access_revoke")
async def revoke_access(
    request: Request,
    services: Environments,
    overview: Overview,
    actor: CurrentUser,
    environment_id: PathId,
    user_id: QueryId = None,
    team_id: QueryId = None,
) -> Response:
    """Withdraw a user's or a team's direct grant; the grantee is named in the query."""
    try:
        await services.environments.revoke(actor, environment_id, user_id=user_id, team_id=team_id)
    except ValidationFailed as invalid:
        return await _detail_page(
            request,
            services,
            overview,
            actor,
            environment_id,
            status_code=422,
            fields=invalid.fields,
        )
    return _detail_redirect(
        request, environment_id, Flash("ok", "Access removed."), open_access=True
    )
