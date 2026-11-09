"""Activity log pages: the filtered log, one event, the downloads and the object panels."""

import re
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal, TypeGuard, cast, get_args
from urllib.parse import urlencode

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.auth.dependencies import CurrentAdministrator, require_administrator
from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.activity_event import ActivityFilter, ActorType, Category, Outcome
from app.db.repositories.pagination import MAX_PAGE_SIZE, MAX_ROW_ID, InvalidCursorError
from app.db.repositories.resource_control import canonical_stack_key
from app.db.repositories.stack import StackKind
from app.logging import get_logger
from app.services.activity import SUBSYSTEMS
from app.services.activity_csv import EXPORT_FORMATS, ExportFormat
from app.services.activity_log import (
    STACK_PANEL_SCAN_PAGES,
    STACK_PANEL_SCAN_ROWS,
    ActivityEventView,
    ActivityLogService,
    ExportBusy,
    ExportStream,
    PanelKind,
    RetentionStatus,
)
from app.services.activity_targets import (
    HEX_ID,
    IMAGE_ID,
    STACK_NAME,
    SWARM_ID,
    VOLUME_NAME,
    integer_id,
)
from app.services.assembly import (
    ActivityLog,
    Environments,
    TargetLookup,
    app_activity_log_service,
    app_stack_services,
)
from app.web.navigation import no_store
from app.web.object_pages import Failure, problem_page, route
from app.web.pagination import Column, PageView
from app.web.templating import (
    is_htmx_request,
    render_fragment,
    render_page,
    render_partial,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/activity", dependencies=[Depends(require_administrator)])

TABLES: Final = "activity/_table.html"
TABLE_TARGET: Final = "activity-table"
PAGE_SIZES: Final = (25, 50, 100, 200)
DEFAULT_SIZE: Final = 50
PANEL_EVENTS: Final = 10
MAX_ID_DIGITS: Final = len(str(MAX_ROW_ID))
FIELD_MAX: Final = 256
LIMIT_MAX: Final = 8
CURSOR_MAX: Final = 512
EXPORT_RETRY_SECONDS: Final = "5"
EARLIEST_TIME: Final = datetime(1970, 1, 1, tzinfo=UTC)
LATEST_TIME: Final = datetime(9999, 1, 1, tzinfo=UTC)
TIME_RANGE: Final = "Give a time between 1970 and 9998 (UTC)."
BAD_CURSOR: Final = "This page link is not valid. Start from the first page."

CATEGORIES: Final = get_args(Category)
ACTOR_TYPES: Final = get_args(ActorType)
RESULTS: Final = get_args(Outcome)
CATEGORY_LABELS: Final = {
    "authentication": "Authentication",
    "user_action": "User action",
    "automation": "Automation",
}
ACTOR_LABELS: Final = {"user": "User", "subsystem": "Subsystem", "anonymous": "Anonymous"}
RESULT_LABELS: Final = {"success": "Success", "failure": "Failure"}

FIELDS: Final = (
    "category",
    "actor_type",
    "username",
    "subsystem",
    "result",
    "action",
    "from",
    "to",
    "environment_id",
    "target_type",
    "target_id",
    "rule_id",
    "job_run_id",
)
MORE_FIELDS: Final = ("environment_id", "target_type", "target_id", "rule_id", "job_run_id")
NAME_PATTERN: Final = re.compile(r"[a-z_]{1,64}")
MOMENT_PATTERN: Final = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?")

LOG_COLUMNS: Final = (
    Column("time", "Time", width="time"),
    Column("actor", "Actor"),
    Column("action", "Action"),
    Column("target", "Target"),
    Column("environment", "Environment"),
    Column("result", "Result", width="badge"),
)


@dataclass(slots=True)
class Reading:
    """What a request asked for: the text as typed, the filter it means and what is wrong."""

    values: dict[str, str]
    size: int = DEFAULT_SIZE
    criteria: ActivityFilter | None = None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def params(self) -> list[tuple[str, str]]:
        """The filters that were filled in, as the links carry them."""
        return [(name, self.values[name]) for name in FIELDS if self.values[name]]


@dataclass(frozen=True, slots=True)
class ActivityPanel:
    """The newest events of one object and the link to all of them."""

    events: list[ActivityEventView]
    all_href: str
    title: str = "Recent activity"


@dataclass(frozen=True, slots=True)
class ActivityScope:
    """Which of an object's events one tab shows; both filters unset means all of them."""

    title: str
    actions: frozenset[str] | None = None
    actor_type: ActorType | None = None


type ActivityKind = PanelKind | Literal["service"]

RESOURCE_CONTROL_ACTIONS: Final = frozenset(
    {"resource_control_created", "resource_control_updated", "resource_control_deleted"}
)
ALL_ACTIVITY: Final = ActivityScope("Recent activity")
NETWORK_ACTIVITY: Final = ActivityScope(
    "Network changes", frozenset({"network_connected", "network_disconnected"})
)
ACCESS_ACTIVITY: Final = ActivityScope("Access changes", RESOURCE_CONTROL_ACTIONS)
AUTOMATION_ACTIVITY: Final = ActivityScope("Automated actions", actor_type="subsystem")


def _identifier(text: str) -> int | None:
    if text.isascii() and text.isdigit() and len(text) <= MAX_ID_DIGITS:
        value = int(text)
        if 1 <= value <= MAX_ROW_ID:
            return value
    return None


def _moment(text: str) -> datetime | None:
    if MOMENT_PATTERN.fullmatch(text) is None:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=UTC)
    except ValueError:
        return None


def _page_size(text: str) -> int | None:
    return int(text) if text.isascii() and text.isdigit() and int(text) in PAGE_SIZES else None


async def read_query(
    request: Request, activity: ActivityLogService, actor: Principal, limit_text: str = ""
) -> Reading:
    """Read the filter fields of the query string; what cannot be used is named in errors."""
    values = {name: request.query_params.get(name, "").strip() for name in FIELDS}
    reading = Reading(values)
    errors = reading.errors
    if limit_text.strip():
        size = _page_size(limit_text.strip())
        if size is None:
            errors["limit"] = "Page size: choose one of the listed sizes."
        else:
            reading.size = size

    def check(name: str, message: str, valid: bool) -> None:
        if values[name] and name not in errors and not valid:
            errors[name] = message

    for name, value in values.items():
        check(name, f"Use at most {FIELD_MAX} characters.", len(value) <= FIELD_MAX)
    check(
        "category",
        "Category: choose one of the listed categories.",
        values["category"] in CATEGORIES,
    )
    check(
        "actor_type", "Actor: choose one of the listed types.", values["actor_type"] in ACTOR_TYPES
    )
    check("result", "Result: choose one of the listed results.", values["result"] in RESULTS)
    check(
        "subsystem",
        "Subsystem: choose one of the listed subsystems.",
        values["subsystem"] in SUBSYSTEMS,
    )
    for name, label in (("action", "Action"), ("target_type", "Target type")):
        check(
            name,
            f"{label}: use lower-case letters and underscores, up to 64.",
            NAME_PATTERN.fullmatch(values[name]) is not None,
        )
    for name, label in (
        ("environment_id", "Environment"),
        ("rule_id", "Rule"),
        ("job_run_id", "Run"),
    ):
        check(
            name,
            f"{label}: enter a whole number from 1 to {MAX_ROW_ID}.",
            _identifier(values[name]) is not None,
        )
    for name, label in (("from", "From"), ("to", "To")):
        moment = _moment(values[name])
        check(name, f"{label}: enter a UTC time such as 2026-01-31T09:00.", moment is not None)
        check(
            name,
            TIME_RANGE,
            moment is None or EARLIEST_TIME <= moment < LATEST_TIME,
        )
    user_id: int | None = None
    if values["username"] and "username" not in errors:
        user_id = await activity.user_id_of(actor, values["username"])
        if user_id is None:
            errors["username"] = "Username: no user has this name."
    if not errors:
        reading.criteria = _criteria(reading, user_id)
    return reading


def _criteria(reading: Reading, user_id: int | None) -> ActivityFilter | None:
    values = reading.values

    def number(name: str) -> int | None:
        return _identifier(values[name]) if values[name] else None

    def moment(name: str) -> datetime | None:
        return _moment(values[name]) if values[name] else None

    try:
        return ActivityFilter(
            category=cast(Category | None, values["category"] or None),
            actor_type=cast(ActorType | None, values["actor_type"] or None),
            actor_user_id=user_id,
            actor_subsystem=values["subsystem"] or None,
            rule_id=number("rule_id"),
            environment_id=number("environment_id"),
            target_type=values["target_type"] or None,
            target_id=values["target_id"] or None,
            result=cast(Outcome | None, values["result"] or None),
            occurred_from=moment("from"),
            occurred_to=moment("to"),
            action=values["action"] or None,
            job_run_id=number("job_run_id"),
        )
    except ValueError:
        if values["target_id"] and not values["target_type"]:
            reading.errors["target_type"] = "Target type: a target id needs a target type."
        else:
            reading.errors["target_id"] = (
                "Target id: a target type needs a target id or an environment."
            )
        return None


def html_page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    """Render a full page that no cache may keep."""
    return no_store(render_page(request, template, context, status_code=status_code))


def html_fragment(
    request: Request, macro: str, *, status_code: int = 200, **arguments: Any
) -> Response:
    """Render one macro of the table template as a fragment that no cache may keep."""
    return no_store(render_fragment(request, TABLES, macro, status_code=status_code, **arguments))


def _href(request: Request, name: str, params: list[tuple[str, str]]) -> str:
    base = route(request, name)
    return f"{base}?{urlencode(params)}" if params else base


def object_log_params(
    kind: ActivityKind,
    environment_id: int,
    target_id: str,
    scope: ActivityScope = ALL_ACTIVITY,
) -> list[tuple[str, str]]:
    """Return the filter that selects the events of one object, narrowed to a scope."""
    params = [("environment_id", str(environment_id))]
    if kind != "environment":
        params += [("target_type", kind), ("target_id", target_id)]
    if scope.actor_type is not None:
        params.append(("actor_type", scope.actor_type))
    if scope.actions is not None and len(scope.actions) == 1:
        params.append(("action", next(iter(scope.actions))))
    return params


async def _stack_events(
    activity: ActivityLogService,
    actor: Principal,
    criteria: ActivityFilter,
    name: str,
    limit: int,
) -> list[ActivityEventView]:
    wanted = name.casefold()
    found: list[ActivityEventView] = []
    cursor: str | None = None
    for _ in range(STACK_PANEL_SCAN_PAGES):
        page = await activity.page(actor, criteria, limit=STACK_PANEL_SCAN_ROWS, cursor=cursor)
        for view in page.items:
            target = view.event.target_id
            if target is not None and target.casefold() == wanted:
                found.append(view)
                if len(found) == limit:
                    return found
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return found


async def _scoped_events(
    activity: ActivityLogService,
    actor: Principal,
    kind: ActivityKind,
    environment_id: int,
    target_id: str,
    scope: ActivityScope,
    limit: int,
) -> list[ActivityEventView]:
    found: list[ActivityEventView] = []
    for action in sorted(scope.actions) if scope.actions is not None else [None]:
        match kind:
            case "stack":
                criteria = ActivityFilter(
                    environment_id=environment_id,
                    target_type="stack",
                    actor_type=scope.actor_type,
                    action=action,
                )
                found += await _stack_events(activity, actor, criteria, target_id, limit)
            case "environment":
                criteria = ActivityFilter(
                    environment_id=environment_id, actor_type=scope.actor_type, action=action
                )
                found += (await activity.page(actor, criteria, limit=limit)).items
            case _:
                criteria = ActivityFilter(
                    environment_id=environment_id,
                    target_type=kind,
                    target_id=target_id,
                    actor_type=scope.actor_type,
                    action=action,
                )
                found += (await activity.page(actor, criteria, limit=limit)).items
    found.sort(key=lambda view: (view.event.occurred_at, view.event.id), reverse=True)
    return found[:limit]


async def build_activity_panel(
    request: Request,
    activity: ActivityLogService,
    actor: Principal,
    kind: ActivityKind,
    environment_id: int,
    target_id: str = "",
    *,
    actions: frozenset[str] | None = None,
    actor_type: ActorType | None = None,
    title: str = ALL_ACTIVITY.title,
) -> ActivityPanel | None:
    """Return the panel for an object page; None unless the viewer is an administrator."""
    if not actor.is_administrator:
        return None
    scope = ActivityScope(title, actions, actor_type)
    if actions is None and actor_type is None and kind != "service":
        events = await activity.for_object(
            actor, kind, environment_id, target_id, limit=PANEL_EVENTS
        )
    else:
        events = await _scoped_events(
            activity, actor, kind, environment_id, target_id, scope, PANEL_EVENTS
        )
    params = object_log_params(kind, environment_id, target_id, scope)
    return ActivityPanel(events, _href(request, "activity", params), title)


async def build_tab_activity_panel(
    request: Request,
    activity: ActivityLogService | None,
    actor: Principal,
    kind: ActivityKind,
    environment_id: int,
    target_id: str,
    scope: ActivityScope | None,
) -> ActivityPanel | None:
    """Return the panel a tab shows, or None for a tab with no scope or no activity service."""
    if activity is None or scope is None:
        return None
    return await build_activity_panel(
        request,
        activity,
        actor,
        kind,
        environment_id,
        target_id,
        actions=scope.actions,
        actor_type=scope.actor_type,
        title=scope.title,
    )


async def _environment_options(
    environments: Environments, actor: Principal, chosen: str
) -> list[tuple[str, str]]:
    page = await environments.environments.dashboard(actor, limit=MAX_PAGE_SIZE)
    options = [("", "Any environment")] + [(str(row.id), row.name) for row in page.items]
    if chosen and chosen not in {value for value, _text in options}:
        options.append((chosen, f"Environment {chosen}"))
    return options


async def _filter_context(
    request: Request, environments: Environments, actor: Principal, reading: Reading
) -> dict[str, Any]:
    subsystems = ["", *sorted(SUBSYSTEMS)]
    return {
        "values": reading.values,
        "errors": reading.errors,
        "size_options": [(str(count), f"{count} per page") for count in PAGE_SIZES],
        "size_text": str(reading.size),
        "more_open": any(
            reading.values.get(name) or name in reading.errors for name in MORE_FIELDS
        ),
        "categories": [("", "Any category"), *CATEGORY_LABELS.items()],
        "actor_types": [("", "Any actor"), *ACTOR_LABELS.items()],
        "results": [("", "Any result"), *RESULT_LABELS.items()],
        "subsystems": [(name, name or "Any subsystem") for name in subsystems],
        "environment_options": await _environment_options(
            environments, actor, reading.values["environment_id"]
        ),
        "list_href": route(request, "activity"),
    }


async def _page_context(
    request: Request,
    environments: Environments,
    actor: Principal,
    reading: Reading,
    *,
    rows: list[ActivityEventView],
    pager: PageView | None,
    retention: RetentionStatus | None,
) -> dict[str, Any]:
    return {
        "rows": rows,
        "columns": LOG_COLUMNS,
        "pager": pager,
        "errors": reading.errors,
        "filters": await _filter_context(request, environments, actor, reading),
        "export_href": _href(request, "activity_export", reading.params),
        "retention": retention,
        "target_hrefs": {},
    }


def _subsystem_error(reading: Reading, invalid: ValidationFailed) -> None:
    for name, message in invalid.fields.items():
        reading.errors["subsystem" if name == "actor_subsystem" else name] = message


@router.get("", response_class=HTMLResponse, name="activity")
async def activity_log(
    request: Request,
    activity: ActivityLog,
    environments: Environments,
    actor: CurrentAdministrator,
    limit: Annotated[str, Query(max_length=LIMIT_MAX)] = "",
    cursor: Annotated[str, Query(max_length=CURSOR_MAX)] = "",
) -> Response:
    """Show the log newest first, filtered by the query string and paged by cursor."""
    reading = await read_query(request, activity, actor, limit)
    rows: list[ActivityEventView] = []
    pager: PageView | None = None
    if reading.criteria is not None:
        try:
            page = await activity.page(
                actor, reading.criteria, limit=reading.size, cursor=cursor or None
            )
            pager = PageView.of(
                page,
                base_path=route(request, "activity"),
                limit=reading.size,
                cursor=cursor or None,
                params=reading.params,
            )
            rows = list(page.items)
        except (InvalidCursorError, TypeError):
            reading.errors["cursor"] = BAD_CURSOR
        except ValidationFailed as invalid:
            _subsystem_error(reading, invalid)
        except ValueError:
            reading.errors["filters"] = "The filters do not fit together."
    status_code = 422 if reading.errors else 200
    target_hrefs = await _target_links(request, actor, rows)
    if is_htmx_request(request):
        if request.headers.get("HX-Target") == TABLE_TARGET:
            if reading.errors:
                return no_store(
                    render_partial(
                        request, "error_summary", errors=reading.errors, status_code=status_code
                    )
                )
            return html_fragment(
                request,
                "activity_table",
                rows=rows,
                columns=LOG_COLUMNS,
                pager=pager,
                target_hrefs=target_hrefs,
            )
        if reading.errors:
            return html_fragment(
                request,
                "activity_failed",
                status_code=status_code,
                errors=reading.errors,
                filters=await _filter_context(request, environments, actor, reading),
            )
        return html_fragment(
            request,
            "activity_results",
            rows=rows,
            columns=LOG_COLUMNS,
            pager=pager,
            export_href=_href(request, "activity_export", reading.params),
            errors=reading.errors,
            target_hrefs=target_hrefs,
        )
    context = await _page_context(
        request,
        environments,
        actor,
        reading,
        rows=rows,
        pager=pager,
        retention=await activity.retention_status(actor),
    )
    context["target_hrefs"] = target_hrefs
    return html_page(request, "activity/list.html", context, status_code)


async def _chunks(stream: ExportStream) -> AsyncGenerator[bytes, None]:
    async for chunk in stream:
        yield chunk


class _ExportResponse(StreamingResponse):
    """An export download that gives the export slot and the session back on every way out.

    A client that leaves before the first chunk is requested never starts the generator, so
    the stream's slot is released here instead.
    """

    def __init__(self, stream: ExportStream, stack: AsyncExitStack, filename: str) -> None:
        self._stream = stream
        self._stack = stack
        self._content = _chunks(stream)
        super().__init__(
            self._content,
            media_type=stream.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._content.aclose()
                await self._stream.aclose()
                await self._stack.aclose()


async def _open_export(
    request: Request,
    actor: Principal,
    criteria: ActivityFilter,
    export_format: ExportFormat,
    stack: AsyncExitStack,
) -> ExportStream:
    """Open the export on its own session, held open by the stack until the response ends."""
    service = await stack.enter_async_context(app_activity_log_service(request.app))
    return service.export(actor, criteria, export_format)


@router.get("/-/export", response_class=StreamingResponse, name="activity_export")
async def export_events(
    request: Request,
    activity: ActivityLog,
    environments: Environments,
    actor: CurrentAdministrator,
) -> Response:
    """Download the events matching the filters, streamed newest first.

    The `format` query value is `csv` (the default) or `ndjson`.
    """
    reading = await read_query(request, activity, actor)
    criteria = reading.criteria
    if criteria is None:
        return await _refused_export(request, environments, actor, reading)
    export_format = request.query_params.get("format", "csv")
    if not _is_export_format(export_format):
        reading.errors["format"] = "Format: choose csv or ndjson."
        return await _refused_export(request, environments, actor, reading, 400)
    stack = AsyncExitStack()
    try:
        stream = await _open_export(request, actor, criteria, export_format, stack)
    except ExportBusy as busy:
        await stack.aclose()
        refused = problem_page(
            request,
            heading=f"Download {export_format.upper()}",
            failure=Failure(429, "The download was not started", str(busy)),
            back_href=_href(request, "activity", reading.params),
            back_label="Back to the log",
            region=None,
        )
        refused.headers["Retry-After"] = EXPORT_RETRY_SECONDS
        return refused
    except ValidationFailed as invalid:
        await stack.aclose()
        _subsystem_error(reading, invalid)
        return await _refused_export(request, environments, actor, reading)
    except BaseException:
        await stack.aclose()
        raise
    logger.info(
        "activity_exported",
        extra={
            "user_id": actor.user_id,
            "filter_names": [name for name, _value in reading.params],
            "export_format": export_format,
        },
    )
    stamp = request.app.state.auth.clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return _ExportResponse(stream, stack, f"activity-{stamp}{stream.suffix}")


def _is_export_format(value: str) -> TypeGuard[ExportFormat]:
    return value in EXPORT_FORMATS


async def _refused_export(
    request: Request,
    environments: Environments,
    actor: Principal,
    reading: Reading,
    status_code: int = 422,
) -> Response:
    """Answer a download whose filters or format are wrong with the log page and what to correct."""
    context = await _page_context(
        request, environments, actor, reading, rows=[], pager=None, retention=None
    )
    return html_page(request, "activity/list.html", context, status_code)


def event_id_of(event_id: str) -> int:
    """Read the event id of the path; anything that is not a stored id is a missing event."""
    value = _identifier(event_id)
    if value is None:
        raise HTTPException(status_code=404)
    return value


EventId = Annotated[int, Depends(event_id_of)]


INTEGER_TARGETS: Final = {
    "automation_rule": ("automation_rule_detail", "rule_id"),
    "user": ("user_detail", "user_id"),
    "team": ("team_detail", "team_id"),
    "registry_credential": ("registry_detail", "credential_id"),
    "object_storage_target": ("object_storage_target_detail", "target_id"),
}
HEX_TARGETS: Final = {
    "container": ("environment_container_detail", "container_id"),
    "network": ("environment_network_detail", "network_id"),
}
SWARM_TARGETS: Final = {
    "service": ("environment_swarm_service_detail", "service_id"),
    "node": ("environment_swarm_node_detail", "node_id"),
}


async def _stack_link(request: Request, actor: Principal, environment_id: int, name: str) -> str:
    """Link the Swarm or Compose page of a stack by the kind the managed stack records."""
    kind: StackKind = "compose"
    async with app_stack_services(request.app) as stacks:
        try:
            managed = await stacks.queries.managed_names(
                actor, environment_id, q=name, limit=MAX_PAGE_SIZE
            )
        except ResourceNotFound:
            managed = ()
    key = canonical_stack_key(name)
    for item in managed:
        if canonical_stack_key(item.name) == key:
            kind, name = item.kind, item.name
            break
    return _stack_href(request, environment_id, name, kind)


async def _target_links(
    request: Request, actor: Principal, rows: list[ActivityEventView]
) -> dict[int, str]:
    """Link each row's target, a stack only when it is a managed stack, without existence checks."""
    links: dict[int, str] = {}
    stacks: dict[int, dict[int, str]] = {}
    for view in rows:
        event = view.event
        if event.target_type == "stack":
            name = event.target_id
            if event.environment_id is not None and name and STACK_NAME.fullmatch(name):
                stacks.setdefault(event.environment_id, {})[event.id] = name
            continue
        href = await _target_link(request, actor, view)
        if href is not None:
            links[event.id] = href
    if not stacks:
        return links
    async with app_stack_services(request.app) as services:
        for environment_id, named in stacks.items():
            managed = await services.queries.managed_stacks(actor, environment_id, named.values())
            for event_id, name in named.items():
                if name in managed:
                    stored, kind = managed[name]
                    links[event_id] = _stack_href(request, environment_id, stored, kind)
    return links


def _stack_href(request: Request, environment_id: int, name: str, kind: StackKind) -> str:
    if kind == "swarm":
        return route(
            request, "environment_swarm_stack_detail", environment_id=environment_id, namespace=name
        )
    return route(
        request, "environment_stack_detail", environment_id=environment_id, stack_name=name
    )


async def _target_link(request: Request, actor: Principal, view: ActivityEventView) -> str | None:
    """Return the page of the event's target, or None when its id is not one that page takes."""
    event = view.event
    kind, target = event.target_type, event.target_id
    if target is None:
        return None
    if kind in INTEGER_TARGETS:
        row_id = integer_id(target)
        if row_id is None:
            return None
        name, parameter = INTEGER_TARGETS[kind]
        return route(request, name, **{parameter: row_id})
    environment_id = event.environment_id
    if environment_id is None:
        return None
    if kind in HEX_TARGETS and HEX_ID.fullmatch(target):
        name, parameter = HEX_TARGETS[kind]
        return route(request, name, environment_id=environment_id, **{parameter: target})
    if kind in SWARM_TARGETS and SWARM_ID.fullmatch(target):
        name, parameter = SWARM_TARGETS[kind]
        return route(request, name, environment_id=environment_id, **{parameter: target})
    if kind == "image" and (found := IMAGE_ID.fullmatch(target)):
        return route(
            request, "environment_image_detail", environment_id=environment_id, image_id=found[1]
        )
    if kind == "volume" and VOLUME_NAME.fullmatch(target):
        return route(
            request, "environment_volume_detail", environment_id=environment_id, volume_name=target
        )
    if kind == "stack" and STACK_NAME.fullmatch(target):
        return await _stack_link(request, actor, environment_id, target)
    return None


@router.get("/{event_id}", response_class=HTMLResponse, name="activity_event_detail")
async def event_detail(
    request: Request,
    activity: ActivityLog,
    targets: TargetLookup,
    actor: CurrentAdministrator,
    event_id: EventId,
) -> Response:
    """Show every column of one event, with links to the automation, run, target and environment."""
    view = await activity.get(actor, event_id)
    detail = sorted((view.event.detail or {}).items())
    target_deleted = await targets.presence(actor, view.event) == "deleted"
    return html_page(
        request,
        "activity/detail.html",
        {
            "view": view,
            "detail": detail,
            "list_href": route(request, "activity"),
            "target_deleted": target_deleted,
            "target_href": None if target_deleted else await _target_link(request, actor, view),
        },
    )
