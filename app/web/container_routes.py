"""Container pages, fragments and the stats stream."""

import asyncio
import hashlib
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Annotated, Any, Final
from urllib.parse import urlencode

import anyio
from fastapi import APIRouter, Depends, Form, Path, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import StringConstraints
from starlette.types import Receive, Scope, Send

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import (
    PermissionDenied,
    ProductError,
    ResourceNotFound,
    SelfProtected,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE, Page
from app.self_identity import SelfIdentity, source_of
from app.services.access import is_canonical_id
from app.services.activity_log import ActivityLogService
from app.services.assembly import (
    Access,
    ActivityLog,
    Containers,
    ContainerServices,
    Environments,
    Jobs,
    JobServices,
    Networks,
    NetworkServices,
    app_container_services,
)
from app.services.automation_rules import PanelRules
from app.services.container_actions import (
    MAX_BATCH,
    ActionConfirmation,
    ActionOutcome,
    BusyView,
    ContainerGone,
    ContainerStateConflict,
    EngineRefused,
    VolumeMount,
    conflict_message,
)
from app.services.container_logs import (
    DEFAULT_PAGE_TAIL,
    MAX_FILTER_CHARS,
    MAX_PAGE_TAIL,
    LogQuery,
    LogTail,
)
from app.services.container_stats import (
    RETRY_MILLISECONDS,
    ContainerNotRunning,
    KeepAlive,
    StatsSample,
    StatsStream,
)
from app.services.containers import (
    CONTAINER_ACTIONS,
    CONTAINER_STATES,
    DEFAULT_PAGE_SIZE,
    HEALTH_FILTERS,
    MAX_LABEL_FILTERS,
    SELF_REFUSED_ACTIONS,
    STOPPED_STATES,
    ContainerAction,
    ContainerDetail,
    ContainerFilters,
    ContainerListTooLarge,
    ContainerRow,
    marked_self,
    offered,
    parse_action,
)
from app.services.host_objects import NetworkListTooLarge
from app.services.networks import AttachOffer
from app.services.object_inspector import ContainerTooLarge, EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectBusy, TooManyStreams
from app.services.terminals import TerminalForm
from app.web.activity_routes import (
    ACCESS_ACTIVITY,
    ALL_ACTIVITY,
    AUTOMATION_ACTIVITY,
    NETWORK_ACTIVITY,
    build_tab_activity_panel,
)
from app.web.container_create_routes import Notice, creation_notices
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.navigation import no_store
from app.web.object_pages import (
    ACCESS_REMOVED,
    ACCESS_SAVED,
    AccessData,
    DigestField,
    Region,
    active_region,
    apply_access,
    bind_digest,
    redirect,
    remove_access,
)
from app.web.origins import RequestOrigin
from app.web.pagination import Column, PageView
from app.web.problems import error_copy, unreachable_copy
from app.web.results import show_result
from app.web.templating import is_htmx_request, render_fragment, render_page, render_partial

TABS_REGION: Final = Region("container-tabs", "outerHTML")

router = APIRouter(
    prefix="/environments/{environment_id}/containers",
    tags=["containers"],
    dependencies=[Depends(require_user)],
)

FIELD_MAX = 256
CURSOR_MAX = 512
SEGMENT_MAX = 128
LABEL_PARAMS_MAX = 20
BATCH_PARAMS_MAX = 100
CHECKED = "on"
FORCE_CONFIRMED = "yes"
CHECKED_FLAG = "1"
VIEWS = ("list", "detail")
TABS = (
    "overview",
    "labels",
    "mounts",
    "networks",
    "environment",
    "stats",
    "logs",
    "automation",
    "access",
)
TAB_ACTIVITY: Final = {
    "overview": ALL_ACTIVITY,
    "networks": NETWORK_ACTIVITY,
    "automation": AUTOMATION_ACTIVITY,
    "access": ACCESS_ACTIVITY,
}
ADMINISTRATOR_TABS = frozenset({"environment", "access"})
RETRYABLE = (409, 502, 503)
FILTER_STATES = tuple(dict.fromkeys(("stopped", *CONTAINER_STATES)))
CHANGED_HEADERS = {"HX-Trigger": "container-changed"}
REGION_HEADERS = {"HX-Retarget": "#action-region", "HX-Reswap": "innerHTML"}
STREAM_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
SSE_MEDIA_TYPE = "text/event-stream"
STATS_EVENT = "stats"
LOG_TAIL_CHOICES = (100, 200, 500, 1000, 5000, 10000)
LOG_BUDGET_CHOICES = (1000, 5000, 20000)
LOG_SINCE_PRESETS = ("all", "5m", "15m", "1h", "6h", "24h", "7d")
LOG_PARAMS = ("tail", "since", "until", "stdout", "stderr", "timestamps", "q")
RECENT_PARAM = "recent"
LOG_FALSE = frozenset({"0", "false", "off", "no"})
LOG_TAIL_DIGITS = 9

LIST_TABLES = "containers/_tables.html"
CONFIRM = "containers/_confirm.html"
DETAIL = "containers/_detail.html"
UPDATES = "containers/_updates.html"
STATS = "containers/_stats.html"

Field = Annotated[str, Form(max_length=FIELD_MAX)]
Short = Annotated[str, StringConstraints(max_length=FIELD_MAX)]
View = Annotated[str, Query(max_length=16)]


def _canonical_container_id(
    container_id: Annotated[str, Path(max_length=SEGMENT_MAX)],
) -> str:
    """Refuse anything but the full lower-case id before any Engine call, for every role."""
    if not is_canonical_id("container", container_id):
        raise ResourceNotFound("not found")
    return container_id


ContainerId = Annotated[str, Depends(_canonical_container_id)]


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


def _fragment(
    request: Request,
    template: str,
    macro: str,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    **arguments: Any,
) -> Response:
    return no_store(
        render_fragment(
            request, template, macro, status_code=status_code, headers=headers, **arguments
        )
    )


def _error_fragment(
    request: Request,
    title: str,
    message: str,
    status_code: int,
    region: Region | None,
    retry_href: str | None = None,
) -> Response:
    target = active_region(request, region)
    return no_store(
        render_partial(
            request,
            "error_state",
            title=title,
            message=message,
            request_id=str(getattr(request.state, "request_id", "")) or None,
            retry_href=retry_href,
            region_id=target.id if target else None,
            swap=target.swap if target else "outerHTML",
            status_code=status_code,
        )
    )


def _self_href(request: Request) -> str:
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


def _list_path(request: Request, environment_id: int) -> str:
    return str(
        request.app.url_path_for("environment_container_list", environment_id=environment_id)
    )


def _detail_path(request: Request, environment_id: int, container_id: str) -> str:
    return str(
        request.app.url_path_for(
            "environment_container_detail",
            environment_id=environment_id,
            container_id=container_id,
        )
    )


def _view(value: str) -> str:
    return value if value in VIEWS else "list"


def _blank(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _identity(request: Request) -> SelfIdentity:
    return source_of(request.app)()


def _instance_href(request: Request, actor: Principal) -> str | None:
    """Return the Instance page, which only administrators may open."""
    return str(request.app.url_path_for("server_instance")) if actor.is_administrator else None


def _marked_rows(request: Request, rows: list[ContainerRow]) -> list[ContainerRow]:
    identity = _identity(request)
    if not identity.containerised:
        return rows
    return [marked_self(row) if identity.protects_container(row.id) else row for row in rows]


def _marked_detail(request: Request, detail: ContainerDetail) -> ContainerDetail:
    return marked_self(detail) if _identity(request).protects_container(detail.id) else detail


def _problem(
    request: Request,
    environment_name: str,
    *,
    heading: str,
    title: str,
    message: str,
    back_href: str,
    back_label: str,
    status_code: int,
    region: Region | None,
) -> Response:
    """Render a failure that replaces the whole page, or only the target of an htmx request."""
    if is_htmx_request(request):
        return _error_fragment(
            request, title, message, status_code, region, retry_href=_self_href(request)
        )
    return _page(
        request,
        "containers/problem.html",
        {
            "environment_name": environment_name,
            "heading": heading,
            "title": title,
            "message": message,
            "back_href": back_href,
            "back_label": back_label,
            "retry_href": _self_href(request),
        },
        status_code,
    )


def _force_unconfirmed(request: Request, environment_id: int) -> Response:
    copy = error_copy(400)
    return _problem(
        request,
        "",
        heading="Containers",
        title=copy.title,
        message=copy.message,
        back_href=_list_path(request, environment_id),
        back_label="Back to containers",
        status_code=400,
        region=None,
    )


@dataclass(frozen=True, slots=True)
class _Failure:
    status: int
    title: str
    message: str
    busy: BusyView | None = None


def _failure_of(error: Exception, services: ContainerServices, actor: Principal) -> _Failure:
    """Map an error an action can raise to the notice the caller is shown."""
    if isinstance(error, SelfProtected):
        return _Failure(409, "This container is protected", str(error))
    if isinstance(error, ObjectBusy):
        busy = services.actions.describe_busy(error, actor)
        return _Failure(409, "This container is busy", str(error), busy)
    if isinstance(error, ContainerStateConflict):
        return _Failure(409, "The action could not be carried out", str(error))
    if isinstance(error, ValidationFailed):
        message = "; ".join(error.fields.values())
        if "name" in error.fields:
            return _Failure(409, "The container changed", message)
        return _Failure(400, "Check the request", message)
    if isinstance(error, ContainerGone):
        return _Failure(404, "The container no longer exists", str(error))
    if isinstance(error, EngineRefused):
        return _Failure(502, "Docker refused the action", str(error))
    if isinstance(error, EnvironmentUnavailable):
        return _Failure(503, *unreachable_copy(error))
    if isinstance(error, ContainerTooLarge):
        return _Failure(503, "The container cannot be read", str(error))
    if isinstance(error, PermissionDenied):
        return _Failure(
            403, "You cannot change this container", "You can read it but not change it."
        )
    return _Failure(
        404, "Container not found", "It does not exist, or you do not have access to it."
    )


ACTION_ERRORS = (
    SelfProtected,
    ObjectBusy,
    ContainerStateConflict,
    ValidationFailed,
    ContainerGone,
    EngineRefused,
    EnvironmentUnavailable,
    ContainerTooLarge,
    PermissionDenied,
    ResourceNotFound,
)


def _columns(writable: bool, may_create: bool) -> tuple[Column, ...]:
    base = (
        Column("name", "Container"),
        Column("state", "State", width="badge"),
        Column("image", "Image", optional=True),
        Column("stack", "Stack", optional=True, width="medium"),
        Column("created", "Created", optional=True, width="time"),
        Column("ports", "Ports", optional=True),
    )
    columns = (Column("select", "Select"), *base) if writable else base
    if writable or may_create:
        return (*columns, Column("actions", "Actions", width="actions-row"))
    return columns


@router.get("", response_class=HTMLResponse, name="environment_container_list")
async def list_containers(
    request: Request,
    containers: Containers,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    q: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
    state: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
    health: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
    image: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
    stack: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
    label: Annotated[list[str] | None, Query(max_length=LABEL_PARAMS_MAX)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=CURSOR_MAX)] = None,
) -> Response:
    """List the containers the caller may see, read from the host now, filtered and paged."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    labels = [text for text in (value.strip() for value in label or []) if text]
    filters = ContainerFilters(
        state=_blank(state),
        health=_blank(health),
        q=_blank(q),
        image=_blank(image),
        labels=tuple(labels),
        stack=_blank(stack),
    )
    page: Page[ContainerRow] = Page([], None)
    total = 0
    health_unavailable = False
    errors: tuple[str, ...] = ()
    failure: str | None = None
    status_code = 200
    try:
        result = await containers.queries.page(
            actor, environment_id, filters, limit=limit, cursor=cursor
        )
    except ValidationFailed as invalid:
        errors, status_code = tuple(invalid.fields.values()), 400
    except (EnvironmentUnavailable, ContainerListTooLarge) as unavailable:
        failure, status_code = str(unavailable), 503
    else:
        page, total = result.page, result.total
        page = Page(_marked_rows(request, page.items), page.next_cursor)
        health_unavailable = result.health_unavailable

    params = [
        (key, value)
        for key, value in (
            ("q", filters.q),
            ("state", filters.state),
            ("health", filters.health),
            ("image", filters.image),
            ("stack", filters.stack),
        )
        if value
    ]
    params.extend(("label", text) for text in labels)
    pager = PageView.of(
        page,
        base_path=_list_path(request, environment_id),
        limit=limit,
        cursor=None if errors or failure else cursor,
        params=params,
    )
    writable = any(row.can_write for row in page.items)
    may_create = await containers.queries.may_create(actor, environment_id)
    results: dict[str, Any] = {
        "rows": page.items,
        "columns": _columns(writable, may_create),
        "may_create": may_create,
        "pager": pager,
        "total": total,
        "writable": writable,
        "environment_id": environment_id,
        "actions": CONTAINER_ACTIONS,
        "health_unavailable": health_unavailable,
        "filtered": bool(params),
        "errors": errors,
        "failure": failure,
        "retry_href": _self_href(request),
    }
    instance_href = _instance_href(request, actor)
    if is_htmx_request(request):
        return _fragment(
            request,
            LIST_TABLES,
            "containers_results",
            status_code,
            oob=True,
            **results,
            instance_href=instance_href,
        )
    return _page(
        request,
        "containers/list.html",
        {
            **results,
            "environment": environment,
            "q": filters.q or "",
            "state": filters.state or "",
            "health": filters.health or "",
            "image": filters.image or "",
            "stack": filters.stack or "",
            "labels": labels,
            "states": FILTER_STATES,
            "health_options": HEALTH_FILTERS,
            "max_labels": MAX_LABEL_FILTERS,
            "is_admin": actor.is_administrator,
            "instance_href": instance_href,
        },
        status_code,
    )


@router.get(
    "/effective-access", response_class=HTMLResponse, name="environment_container_effective_access"
)
async def container_effective_access(
    request: Request,
    containers: Containers,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    username: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
) -> Response:
    """Explain one account's reach over every container on the host; administrators only."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    list_href = _list_path(request, environment_id)

    def prompt(error: str | None, status_code: int) -> Response:
        return _page(
            request,
            "containers/effective_access_prompt.html",
            {"environment": environment, "username": username or "", "error": error},
            status_code,
        )

    if username is None:
        if not actor.is_administrator:
            raise PermissionDenied("administrator role required")
        return prompt(None, 200)
    try:
        result = await containers.queries.effective_access(actor, environment_id, username)
    except ValidationFailed as invalid:
        return prompt("; ".join(invalid.fields.values()), 400)
    except (EnvironmentUnavailable, ContainerListTooLarge) as unavailable:
        return _problem(
            request,
            environment.name,
            heading="Effective access",
            title="Containers cannot be listed",
            message=str(unavailable),
            back_href=list_href,
            back_label="Back to containers",
            status_code=503,
            region=None,
        )
    return _page(
        request,
        "access/effective_access.html",
        {
            "access": result.access,
            "resource_type": "container",
            "names": result.names,
            "back_href": list_href,
            "back_label": "Back to containers",
        },
    )


@dataclass(frozen=True, slots=True)
class BatchVolumes:
    """The volumes one removal target mounts, and the token a confirmed removal must repeat."""

    mounts: tuple[VolumeMount, ...]
    hidden: int
    token: str


UNREAD_TOKEN = "unread"
BATCH_CHANGED = (
    "The volumes of a selected container changed since you reviewed them. "
    "Nothing was removed; review the selection again."
)


def _volumes_token(confirmation: ActionConfirmation) -> str:
    """Digest the volumes a removal with volumes could delete, so a changed set is detected."""
    removable = sorted(
        f"{mount.name}{'' if mount.anonymous else '?'}"
        for mount in confirmation.volumes
        if mount.anonymous is not False
    )
    text = "\n".join([*removable, str(confirmation.volumes_hidden)])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


async def _batch_volumes(
    containers: ContainerServices, actor: Principal, environment_id: int, ids: list[str]
) -> dict[str, BatchVolumes | None]:
    """Read each target's volumes; a target that cannot be read maps to None."""

    async def read(container_id: str) -> BatchVolumes | None:
        try:
            confirmation = await containers.actions.confirmation(
                actor, environment_id, container_id, "remove"
            )
        except (ResourceNotFound, PermissionDenied, ContainerTooLarge, EnvironmentUnavailable):
            return None
        return BatchVolumes(
            confirmation.volumes, confirmation.volumes_hidden, _volumes_token(confirmation)
        )

    found = await asyncio.gather(*(read(container_id) for container_id in ids))
    return dict(zip(ids, found, strict=True))


async def _volumes_changed(
    containers: ContainerServices,
    actor: Principal,
    environment_id: int,
    ids: list[str],
    expected: list[str],
) -> bool:
    """Report whether any target's volumes differ from what was reviewed."""
    unique = list(dict.fromkeys(ids))
    if not 0 < len(unique) <= MAX_BATCH:
        return False
    reviewed = dict(zip(ids, expected, strict=True))
    current = await _batch_volumes(containers, actor, environment_id, unique)
    return any(
        (UNREAD_TOKEN if volumes is None else volumes.token) != reviewed[container_id]
        for container_id, volumes in current.items()
    )


@router.get("/batch", response_class=HTMLResponse, name="environment_container_batch_confirm")
async def confirm_batch(
    request: Request,
    containers: Containers,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    action: Annotated[str, Query(max_length=16)] = "",
    container: Annotated[list[str] | None, Query(max_length=BATCH_PARAMS_MAX)] = None,
    force: Annotated[str, Query(max_length=8)] = "",
) -> Response:
    """Name every container of a batch before anything is done to it.

    A removal that kills running containers or deletes anonymous volumes is the separate `force`
    confirmation.
    """
    environment = (await environments.environments.get(actor, environment_id)).summary
    chosen = parse_action(action)
    if chosen is None:
        return _problem(
            request,
            environment.name,
            heading="Containers",
            title="Check the selection",
            message="Choose an action from the list.",
            back_href=_list_path(request, environment_id),
            back_label="Back to containers",
            status_code=400,
            region=None,
        )
    fields: dict[str, str] = {}
    confirmation = None
    try:
        confirmation = await containers.actions.batch_confirmation(
            actor, environment_id, chosen, container or []
        )
    except ValidationFailed as invalid:
        fields = invalid.fields
    except (EnvironmentUnavailable, ContainerTooLarge) as unavailable:
        return _problem(
            request,
            environment.name,
            heading=f"{chosen.capitalize()} containers",
            title=unreachable_copy(unavailable).title,
            message=unreachable_copy(unavailable).message,
            back_href=_list_path(request, environment_id),
            back_label="Back to containers",
            status_code=503,
            region=None,
        )
    volumes: dict[str, BatchVolumes | None] = {}
    if confirmation is not None and chosen == "remove":
        volumes = await _batch_volumes(
            containers,
            actor,
            environment_id,
            [target.container_id for target in confirmation.targets],
        )
    identity = _identity(request)
    protected_ids = (
        frozenset(
            target.container_id
            for target in confirmation.targets
            if identity.protects_container(target.container_id)
        )
        if confirmation is not None and chosen in SELF_REFUSED_ACTIONS
        else frozenset()
    )
    return _page(
        request,
        "containers/batch_force_confirm.html"
        if chosen == "remove" and force == CHECKED_FLAG
        else "containers/batch_confirm.html",
        {
            "confirmation": confirmation,
            "fields": fields,
            "action": chosen,
            "environment_id": environment_id,
            "volumes": volumes,
            "unread_token": UNREAD_TOKEN,
            "protected_ids": protected_ids,
            "instance_href": _instance_href(request, actor),
        },
        400 if fields else 200,
    )


@router.post("/batch", response_class=HTMLResponse, name="environment_container_batch_run")
async def run_batch(
    request: Request,
    containers: Containers,
    actor: CurrentUser,
    environment_id: PathId,
    action: Field = "",
    container: Annotated[list[Short] | None, Form(max_length=BATCH_PARAMS_MAX)] = None,
    expected_name: Annotated[list[Short] | None, Form(max_length=BATCH_PARAMS_MAX)] = None,
    expected_volumes: Annotated[list[Short] | None, Form(max_length=BATCH_PARAMS_MAX)] = None,
    display_name: Annotated[list[Short] | None, Form(max_length=BATCH_PARAMS_MAX)] = None,
    force: Annotated[str, Form(max_length=8)] = "",
    volumes: Annotated[str, Form(max_length=8)] = "",
    confirm_force: Annotated[str, Form(max_length=8)] = "",
) -> Response:
    """Apply one action to the confirmed containers, each on its own, and report each result."""
    chosen = parse_action(action)
    ids = container or []
    names = expected_name or []
    shown_names = display_name if display_name and len(display_name) == len(ids) else names
    reviewed_volumes = expected_volumes or []

    def refused(message: str) -> Response:
        return _problem(
            request,
            "",
            heading="Containers",
            title="Check the selection",
            message=message,
            back_href=_list_path(request, environment_id),
            back_label="Back to containers",
            status_code=400,
            region=None,
        )

    if chosen is None:
        return refused("Choose an action from the list.")
    if (force == CHECKED or volumes == CHECKED) and confirm_force != FORCE_CONFIRMED:
        return _force_unconfirmed(request, environment_id)
    if chosen == "remove" and len(ids) != len(names):
        return refused("The confirmation was incomplete. Review the selection again.")
    if chosen == "remove" and volumes == CHECKED:
        if len(ids) != len(reviewed_volumes):
            return refused("The confirmation was incomplete. Review the selection again.")
        if await _volumes_changed(containers, actor, environment_id, ids, reviewed_volumes):
            return refused(BATCH_CHANGED)
    try:
        result = await containers.actions.run_batch(
            actor,
            environment_id,
            chosen,
            ids,
            expected_names=dict(zip(ids, names, strict=False)) if chosen == "remove" else None,
            reviewed_names=dict(zip(ids, shown_names, strict=False)),
            force=force == CHECKED,
            volumes=volumes == CHECKED,
        )
    except ValidationFailed as invalid:
        return refused("; ".join(invalid.fields.values()))
    return show_result(
        request,
        "containers/batch_result.html",
        {"result": result, "environment_id": environment_id},
    )


_LOG_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z")


def _log_time(timestamp: str | None) -> str | None:
    """Shorten an RFC 3339 timestamp to the date, the time and milliseconds, in UTC."""
    if timestamp is None:
        return None
    match = _LOG_TIMESTAMP.fullmatch(timestamp)
    if match is None:
        return timestamp
    date, clock, fraction = match.groups()
    return f"{date} {clock}.{(fraction or '').ljust(3, '0')[:3]}"


def _log_checked(present: dict[str, str], name: str) -> bool:
    return present.get(name, "1").strip().lower() not in LOG_FALSE


def _log_lines(tail: LogTail, needle: str) -> list[dict[str, Any]]:
    """Return the tail's lines for display, keeping those that contain the casefolded needle."""
    folded = needle.casefold()
    return [
        {"time": _log_time(line.timestamp), "text": line.text, "stderr": line.stream == "stderr"}
        for line in tail.lines
        if not folded or folded in line.text.casefold()
    ]


@dataclass(frozen=True, slots=True)
class _LogFailure:
    status: int
    title: str
    message: str


def _log_failure(error: Exception) -> _LogFailure:
    if isinstance(error, TooManyStreams):
        return _LogFailure(429, "Too many live views are open", str(error))
    return _LogFailure(503, "Logs are not available", str(error))


async def _logs_context(
    request: Request,
    containers: ContainerServices,
    actor: Principal,
    environment_id: int,
    container_id: str,
    running: bool,
) -> dict[str, Any]:
    """Read the Logs tab's form values and, only on request, the bounded tail shown without script.

    Script streams the output itself, so a page load reads no tail unless the URL asks for one.
    """
    present = {
        name: request.query_params[name] for name in LOG_PARAMS if name in request.query_params
    }
    params = dict(present)
    clamped = False
    requested = params.get("tail", "").strip()
    if (
        requested.isascii()
        and requested.isdigit()
        and len(requested) <= LOG_TAIL_DIGITS
        and int(requested) > MAX_PAGE_TAIL
    ):
        params["tail"] = str(MAX_PAGE_TAIL)
        clamped = True
    errors: dict[str, str] = {}
    query: LogQuery | None = None
    try:
        query = LogQuery.from_params(params, default_tail=DEFAULT_PAGE_TAIL, max_tail=MAX_PAGE_TAIL)
    except ValidationFailed as invalid:
        errors = dict(invalid.fields)
    wants_recent = request.query_params.get(RECENT_PARAM) == CHECKED_FLAG
    lines: list[dict[str, Any]] = []
    total = 0
    cut = False
    failure: _LogFailure | None = None
    recent = query is not None and wants_recent and not is_htmx_request(request)
    if query is not None and recent:
        try:
            tail = await containers.logs.tail(actor, environment_id, container_id, query)
        except (EnvironmentUnavailable, ContainerTooLarge, TooManyStreams) as error:
            failure = _log_failure(error)
        else:
            total = len(tail.lines)
            cut = tail.cut
            lines = _log_lines(tail, query.filter)
    tail_value = (present.get("tail", "").strip() or str(DEFAULT_PAGE_TAIL))[:16]
    tail_options = [str(choice) for choice in LOG_TAIL_CHOICES]
    if tail_value not in tail_options:
        tail_options.insert(0, tail_value)
    values = {
        "tail": tail_value,
        "since": present.get("since", ""),
        "until": present.get("until", ""),
        "stdout": _log_checked(present, "stdout"),
        "stderr": _log_checked(present, "stderr"),
        "timestamps": _log_checked(present, "timestamps"),
        "q": present.get("q", ""),
    }
    routes = request.app
    download_params = [("tail", "all")]
    download_params.extend(
        (name, present[name]) for name in ("since", "until") if present.get(name)
    )
    download_params.extend(
        [
            ("stdout", "1" if values["stdout"] else "0"),
            ("stderr", "1" if values["stderr"] else "0"),
            ("timestamps", "1" if values["timestamps"] else "0"),
        ]
    )
    if present.get("q"):
        download_params.append(("q", present["q"]))
    download_path = str(
        routes.url_path_for(
            "environment_container_logs_download",
            environment_id=environment_id,
            container_id=container_id,
        )
    )
    recent_params = [("tab", "logs"), ("tail", tail_value)]
    recent_params.extend(
        (name, present[name]) for name in ("since", "until", "q") if present.get(name)
    )
    recent_params.extend(
        [
            ("stdout", "1" if values["stdout"] else "0"),
            ("stderr", "1" if values["stderr"] else "0"),
            ("timestamps", "1" if values["timestamps"] else "0"),
            (RECENT_PARAM, CHECKED_FLAG),
        ]
    )
    return {
        "selection": values,
        "errors": errors,
        "tail_options": tail_options,
        "budget_options": LOG_BUDGET_CHOICES,
        "since_presets": LOG_SINCE_PRESETS,
        "max_filter": MAX_FILTER_CHARS,
        "static": query is not None,
        "recent": recent,
        "lines": lines,
        "total": total,
        "cut": cut,
        "clamped": clamped,
        "failure": failure,
        "page_tail": DEFAULT_PAGE_TAIL if query is None else (query.tail or 0),
        "running": running,
        "stream_href": str(
            routes.url_path_for(
                "environment_container_logs_stream",
                environment_id=environment_id,
                container_id=container_id,
            )
        ),
        "download_base": download_path,
        "download_href": f"{download_path}?{urlencode(download_params)}",
        "refresh_href": _self_href(request),
        "recent_href": f"{request.url.path}?{urlencode(recent_params)}",
    }


async def _terminal_links(
    containers: ContainerServices,
    actor: Principal,
    environment_id: int,
    container_id: str,
    detail: ContainerDetail,
) -> TerminalForm | None:
    """Describe the terminal the caller may open, or None when no link should be offered."""
    if not (detail.can_write and detail.is_running):
        return None
    try:
        return await containers.terminals.form_context(actor, environment_id, container_id, "exec")
    except ProductError:
        return None


@dataclass(frozen=True, slots=True)
class AttachPanel:
    """What the Networks tab offers below its table: a connect form, or why there is none."""

    offer: AttachOffer | None
    failure: str | None = None


async def _attach_panel(
    networks: NetworkServices,
    actor: Principal,
    environment_id: int,
    container_id: str,
    detail: ContainerDetail,
) -> AttachPanel | None:
    """Read the networks the container may join; None when the caller may not attach it."""
    if not detail.can_attach:
        return None
    try:
        offer = await networks.queries.attach_offer(actor, environment_id, container_id)
    except (EnvironmentUnavailable, ObjectTooLarge, NetworkListTooLarge) as unavailable:
        return AttachPanel(None, str(unavailable))
    return AttachPanel(offer)


async def _tab_context(
    containers: ContainerServices,
    actor: Principal,
    environment_id: int,
    container_id: str,
    tab: str,
) -> dict[str, Any]:
    """Read what the chosen tab shows beyond the detail every tab shares."""
    context: dict[str, Any] = {
        "sample": None,
        "stats_failure": None,
        "panel": None,
        "logs": None,
    }
    if tab == "stats":
        try:
            context["sample"] = await containers.stats.sample(actor, environment_id, container_id)
        except ContainerNotRunning as stopped:
            context["stats_failure"] = str(stopped)
        except (EnvironmentUnavailable, ContainerTooLarge) as unavailable:
            context["stats_failure"] = str(unavailable)
    elif tab == "access":
        context["panel"] = await containers.queries.access_panel(
            actor, environment_id, container_id
        )
    return context


async def _last_output(
    request: Request,
    containers: ContainerServices,
    actor: Principal,
    detail: ContainerDetail,
) -> dict[str, Any] | None:
    """Read the newest log lines of a container that ran and stopped; failures become a note."""
    if not detail.ever_started or detail.state not in STOPPED_STATES:
        return None
    href = f"{_detail_path(request, detail.environment_id, detail.id)}?{urlencode({'tab': 'logs'})}"
    try:
        tail = await containers.logs.last_output(actor, detail.environment_id, detail.id)
    except (EnvironmentUnavailable, ContainerTooLarge, TooManyStreams) as error:
        return {"lines": [], "cut": False, "note": _log_failure(error).message, "href": href}
    return {"lines": _log_lines(tail, ""), "cut": tail.cut, "note": None, "href": href}


async def _detail_response(
    request: Request,
    containers: ContainerServices,
    actor: Principal,
    environment_id: int,
    container_id: str,
    tab: str,
    *,
    status_code: int = 200,
    values: dict[str, Any] | None = None,
    fields: dict[str, str] | None = None,
    notices: tuple[Notice, ...] = (),
    networks: NetworkServices | None = None,
    jobs: JobServices | None = None,
    activity: ActivityLogService | None = None,
) -> Response:
    if tab not in TABS:
        raise ResourceNotFound("not found")
    try:
        detail = _marked_detail(
            request, await containers.queries.detail(actor, environment_id, container_id)
        )
        if tab in ADMINISTRATOR_TABS and not actor.is_administrator:
            raise PermissionDenied("administrator role required")
        extra = await _tab_context(containers, actor, environment_id, container_id, tab)
        if tab == "access":
            extra["panel"] = bind_digest(extra["panel"], values)
        if tab == "networks" and networks is not None:
            extra["panel"] = await _attach_panel(
                networks, actor, environment_id, container_id, detail
            )
        if tab == "logs":
            extra["logs"] = await _logs_context(
                request, containers, actor, environment_id, container_id, detail.is_running
            )
            logs_failure = extra["logs"]["failure"]
            if extra["logs"]["errors"]:
                status_code = 400
            elif logs_failure is not None:
                status_code = logs_failure.status
    except (EnvironmentUnavailable, ContainerTooLarge) as unavailable:
        return _problem(
            request,
            "",
            heading="Container",
            title="This container cannot be shown",
            message=str(unavailable),
            back_href=_list_path(request, environment_id),
            back_label="Back to containers",
            status_code=503,
            region=TABS_REGION,
        )
    if tab == "environment" and detail.variables is None:
        raise PermissionDenied("administrator role required")
    visible = [name for name in TABS if actor.is_administrator or name not in ADMINISTRATOR_TABS]
    arguments: dict[str, Any] = {
        "detail": detail,
        "tab": tab,
        "values": values,
        "fields": fields or {},
        **extra,
    }
    additions: dict[str, Any] = {
        "tab_keys": visible,
        "automation": (
            await _automation_panel(jobs, actor, detail)
            if jobs is not None and (tab == "automation" or not is_htmx_request(request))
            else None
        ),
        "activity_log": await build_tab_activity_panel(
            request,
            activity,
            actor,
            "container",
            detail.environment_id,
            detail.id,
            TAB_ACTIVITY.get(tab),
        ),
        "last_output": (
            await _last_output(request, containers, actor, detail) if tab == "overview" else None
        ),
        "show_last_exit": detail.state in STOPPED_STATES,
    }
    if is_htmx_request(request):
        return _fragment(
            request,
            DETAIL,
            "container_tabs",
            status_code,
            **arguments,
            **additions,
        )
    may_create = await containers.queries.may_create(actor, environment_id)
    terminal = await _terminal_links(containers, actor, environment_id, container_id, detail)
    return _page(
        request,
        "containers/detail.html",
        {
            **arguments,
            **additions,
            "instance_href": _instance_href(request, actor),
            "notices": notices,
            "may_create": may_create,
            "terminal": terminal,
        },
        status_code,
    )


async def _automation_panel(
    jobs: JobServices, actor: Principal, detail: ContainerDetail
) -> PanelRules:
    return await jobs.rules.for_object(
        actor,
        detail.environment_id,
        name=detail.name,
        labels={label.key: label.value for label in detail.labels if not label.redacted},
        stack=detail.stack,
        kind="container",
    )


@router.get(
    "/{container_id}",
    response_class=HTMLResponse,
    name="environment_container_detail",
    dependencies=[RequestOrigin],
)
async def container_detail(
    container_id: ContainerId,
    request: Request,
    containers: Containers,
    networks: Networks,
    jobs: Jobs,
    activity: ActivityLog,
    actor: CurrentUser,
    environment_id: PathId,
    tab: Annotated[str, Query(max_length=32)] = "overview",
) -> Response:
    """Show one container, one tab at a time; each tab has its own URL."""
    return await _detail_response(
        request,
        containers,
        actor,
        environment_id,
        container_id,
        tab,
        notices=creation_notices(request.query_params),
        networks=networks,
        jobs=jobs,
        activity=activity,
    )


class _StatsResponse(StreamingResponse):
    """A stream response that gives the stats stream back on every way out.

    The generator's own cleanup never runs if the client leaves before the first chunk is
    requested, so the release is tied to the response instead.
    """

    def __init__(self, stream: StatsStream, content: AsyncGenerator[str, None]) -> None:
        super().__init__(content, media_type=SSE_MEDIA_TYPE, headers=STREAM_HEADERS)
        self._stream = stream
        self._content = content

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._content.aclose()
                await self._stream.aclose()


def _event(name: str, data: str) -> str:
    lines = data.replace("\r", "").split("\n")
    return f"event: {name}\n" + "".join(f"data: {line}\n" for line in lines) + "\n"


async def _next_item(stream: StatsStream) -> StatsSample | KeepAlive | None:
    try:
        return await anext(stream)
    except StopAsyncIteration:
        return None


async def _frames(request: Request, stream: StatsStream) -> AsyncGenerator[str, None]:
    """Render each reading as an event; a quiet interval is a comment line.

    Each wait runs as its own task so that a cancelled response never cancels the stream in the
    middle of releasing the Engine response; the release runs shielded, after that task ends.
    """
    pending: asyncio.Future[StatsSample | KeepAlive | None] | None = None
    try:
        while True:
            pending = asyncio.ensure_future(_next_item(stream))
            item = await asyncio.shield(pending)
            pending = None
            if item is None:
                break
            if isinstance(item, KeepAlive):
                yield ": keep-alive\n\n"
            else:
                yield _event(STATS_EVENT, _render_sample(request, item))
        if stream.end_reason == "lifetime":
            yield f"retry: {RETRY_MILLISECONDS}\n\n"
    finally:
        with anyio.CancelScope(shield=True):
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()


def _render_sample(request: Request, sample: StatsSample) -> str:
    response = render_fragment(request, STATS, "stats_panel", sample=sample)
    return bytes(response.body).decode("utf-8").strip()


async def _open_stream(
    request: Request, actor: Principal, environment_id: int, container_id: str
) -> StatsStream:
    """Authorise and open a stream on a session that closes before any byte is sent.

    The request-scoped session lives until the response ends, which for a stream would be minutes.
    """
    stream: StatsStream | None = None
    try:
        async with app_container_services(request.app) as services:
            stream = await services.stats.open(actor, environment_id, container_id)
    except BaseException:
        if stream is not None:
            await stream.aclose()
        raise
    return stream


@router.get(
    "/{container_id}/stats/stream",
    response_class=StreamingResponse,
    name="environment_container_stats_stream",
)
async def stats_stream(
    container_id: ContainerId, request: Request, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Stream a running container's resource use as server-sent events."""
    try:
        stream = await _open_stream(request, actor, environment_id, container_id)
    except TooManyStreams as refused:
        return _error_fragment(request, "Too many live views are open", str(refused), 429, None)
    except ContainerNotRunning as stopped:
        return _error_fragment(request, "The container is not running", str(stopped), 409, None)
    except (EnvironmentUnavailable, ContainerTooLarge) as unavailable:
        return _error_fragment(request, "Readings are not available", str(unavailable), 503, None)
    try:
        return _StatsResponse(stream, _frames(request, stream))
    except BaseException:
        await stream.aclose()
        raise


@router.get(
    "/{container_id}/actions/{action}",
    response_class=HTMLResponse,
    name="environment_container_action_confirm",
)
async def confirm_action(
    container_id: ContainerId,
    action: Annotated[str, Path(max_length=16)],
    request: Request,
    containers: Containers,
    actor: CurrentUser,
    environment_id: PathId,
    view: View = "list",
    force: Annotated[str, Query(max_length=8)] = "",
) -> Response:
    """Name the container and what the action does to it; nothing changes until it is posted.

    A removal that kills a running container or deletes its anonymous volumes is the separate
    `force` confirmation.
    """
    chosen = parse_action(action)
    if chosen is None:
        raise ResourceNotFound("not found")
    page_view = _view(view)
    forcing = chosen == "remove" and force == CHECKED_FLAG
    try:
        confirmation = await containers.actions.confirmation(
            actor, environment_id, container_id, chosen
        )
        if chosen in SELF_REFUSED_ACTIONS and _identity(request).protects_container(container_id):
            raise SelfProtected.container()
        if not offered(chosen, confirmation.state):
            raise ContainerStateConflict(conflict_message(chosen, confirmation.state))
    except (
        SelfProtected,
        ContainerStateConflict,
        EnvironmentUnavailable,
        ContainerTooLarge,
    ) as refused:
        failure = _failure_of(refused, containers, actor)
        if is_htmx_request(request):
            return _fragment(
                request,
                CONFIRM,
                "action_notice",
                failure.status,
                title=failure.title,
                message=failure.message,
                busy=None,
                retry_href=None,
            )
        return _action_result_page(
            request, environment_id, container_id, chosen, page_view, None, failure
        )
    if is_htmx_request(request):
        return _fragment(
            request,
            CONFIRM,
            "force_dialog" if forcing else "action_dialog",
            c=confirmation,
            view=page_view,
        )
    return _page(
        request,
        "containers/action_force_confirm.html" if forcing else "containers/action_confirm.html",
        {"confirmation": confirmation, "view": page_view},
    )


def _action_result_context(
    environment_id: int,
    container_id: str,
    action: ContainerAction,
    view: str,
    outcome: ActionOutcome | None,
    failure: _Failure | None,
) -> dict[str, Any]:
    return {
        "environment_id": environment_id,
        "container_id": container_id,
        "action": action,
        "view": view,
        "outcome": outcome,
        "busy": None if failure is None else failure.busy,
        "failure_title": "" if failure is None else failure.title,
        "failure_message": "" if failure is None else failure.message,
        "can_retry": failure is not None and failure.status in RETRYABLE,
    }


def _action_result_page(
    request: Request,
    environment_id: int,
    container_id: str,
    action: ContainerAction,
    view: str,
    outcome: ActionOutcome | None,
    failure: _Failure | None,
) -> Response:
    return _page(
        request,
        "containers/action_result.html",
        _action_result_context(environment_id, container_id, action, view, outcome, failure),
        200 if failure is None else failure.status,
    )


async def _updated_fragment(
    request: Request,
    containers: ContainerServices,
    actor: Principal,
    outcome: ActionOutcome,
    view: str,
) -> Response:
    """Answer an htmx action with the changed row or summary, or only the notice if it is gone."""
    common: dict[str, Any] = {"action": outcome.action, "result": outcome.result}
    if outcome.action == "remove":
        if view == "detail":
            return _fragment(
                request,
                UPDATES,
                "summary_removed",
                name=outcome.name,
                environment_id=outcome.environment_id,
                **common,
            )
        return _fragment(request, UPDATES, "row_removed", name=outcome.name, **common)
    try:
        if view == "detail":
            detail = _marked_detail(
                request,
                await containers.queries.detail(
                    actor, outcome.environment_id, outcome.container_id
                ),
            )
            return _fragment(
                request,
                UPDATES,
                "summary_updated",
                headers=CHANGED_HEADERS,
                detail=detail,
                instance_href=_instance_href(request, actor),
                **common,
            )
        (row,) = _marked_rows(
            request,
            [await containers.queries.row(actor, outcome.environment_id, outcome.container_id)],
        )
        may_create = await containers.queries.may_create(actor, outcome.environment_id)
        return _fragment(
            request,
            UPDATES,
            "row_updated",
            headers=CHANGED_HEADERS,
            row=row,
            may_create=may_create,
            **common,
        )
    except (ResourceNotFound, PermissionDenied, EnvironmentUnavailable, ContainerTooLarge):
        return _fragment(
            request,
            UPDATES,
            "unread",
            headers={"HX-Reswap": "none"},
            name=outcome.name,
            action=outcome.action,
        )


@router.post(
    "/{container_id}/actions/{action}",
    response_class=HTMLResponse,
    name="environment_container_action",
)
async def run_action(
    container_id: ContainerId,
    action: Annotated[str, Path(max_length=16)],
    request: Request,
    containers: Containers,
    actor: CurrentUser,
    environment_id: PathId,
    expected_name: Field = "",
    force: Annotated[str, Form(max_length=8)] = "",
    volumes: Annotated[str, Form(max_length=8)] = "",
    confirm_force: Annotated[str, Form(max_length=8)] = "",
    view: Annotated[str, Form(max_length=16)] = "list",
) -> Response:
    """Carry out one confirmed action; htmx gets the changed row, a browser a result page."""
    chosen = parse_action(action)
    if chosen is None:
        raise ResourceNotFound("not found")
    if (force == CHECKED or volumes == CHECKED) and confirm_force != FORCE_CONFIRMED:
        return _force_unconfirmed(request, environment_id)
    page_view = _view(view)
    try:
        outcome = await containers.actions.perform(
            actor,
            environment_id,
            container_id,
            chosen,
            expected_name=expected_name,
            force=force == CHECKED,
            volumes=volumes == CHECKED,
        )
    except ACTION_ERRORS as error:
        failure = _failure_of(error, containers, actor)
        if is_htmx_request(request):
            confirm_href = (
                str(
                    request.app.url_path_for(
                        "environment_container_action_confirm",
                        environment_id=environment_id,
                        container_id=container_id,
                        action=chosen,
                    )
                )
                + f"?view={page_view}"
            )
            return _fragment(
                request,
                CONFIRM,
                "action_notice",
                failure.status,
                REGION_HEADERS,
                title=failure.title,
                message=failure.message,
                busy=failure.busy,
                retry_href=confirm_href if failure.status in RETRYABLE else None,
            )
        return _action_result_page(
            request, environment_id, container_id, chosen, page_view, None, failure
        )
    if is_htmx_request(request):
        return await _updated_fragment(request, containers, actor, outcome, page_view)
    return show_result(
        request,
        "containers/action_result.html",
        _action_result_context(environment_id, container_id, chosen, page_view, outcome, None),
    )


@router.post(
    "/{container_id}/access", response_class=HTMLResponse, name="environment_container_access_save"
)
async def save_access(
    container_id: ContainerId,
    request: Request,
    containers: Containers,
    access: Access,
    actor: CurrentUser,
    environment_id: PathId,
    data: AccessData,
) -> Response:
    """Create or replace the resource control over one container; administrators only."""
    try:
        panel = await containers.queries.access_panel(actor, environment_id, container_id)
    except (EnvironmentUnavailable, ContainerTooLarge) as unavailable:
        return _access_unavailable(request, environment_id, unavailable)
    refusal = await apply_access(actor, access.controls, containers.queries, panel, data)
    if refusal is not None:
        return await _detail_response(
            request,
            containers,
            actor,
            environment_id,
            container_id,
            "access",
            status_code=refusal.status_code,
            values=refusal.values,
            fields=refusal.fields,
        )
    return redirect(
        request,
        _access_path(request, environment_id, container_id),
        [Flash("ok", ACCESS_SAVED)],
    )


@router.post(
    "/{container_id}/access/delete",
    response_class=HTMLResponse,
    name="environment_container_access_delete",
)
async def delete_access(
    container_id: ContainerId,
    request: Request,
    containers: Containers,
    access: Access,
    actor: CurrentUser,
    environment_id: PathId,
    expected_digest: DigestField = "",
) -> Response:
    """Remove the resource control over one container; administrators only."""
    try:
        panel = await containers.queries.access_panel(actor, environment_id, container_id)
    except (EnvironmentUnavailable, ContainerTooLarge) as unavailable:
        return _access_unavailable(request, environment_id, unavailable)
    refusal = await remove_access(actor, access.controls, panel, expected_digest)
    if refusal is not None:
        return await _detail_response(
            request,
            containers,
            actor,
            environment_id,
            container_id,
            "access",
            status_code=refusal.status_code,
            fields=refusal.fields,
        )
    return redirect(
        request,
        _access_path(request, environment_id, container_id),
        [Flash("ok", ACCESS_REMOVED)],
    )


def _access_path(request: Request, environment_id: int, container_id: str) -> str:
    return f"{_detail_path(request, environment_id, container_id)}?{urlencode({'tab': 'access'})}"


def _access_unavailable(request: Request, environment_id: int, unavailable: Exception) -> Response:
    return _problem(
        request,
        "",
        heading="Container",
        title="This container cannot be shown",
        message=str(unavailable),
        back_href=_list_path(request, environment_id),
        back_label="Back to containers",
        status_code=503,
        region=None,
    )
