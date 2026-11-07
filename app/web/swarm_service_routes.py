"""Swarm service pages: list, create, detail, edit, scale, roll back, remove and access."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final
from urllib.parse import urlencode

from fastapi import Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.services.activity_log import ActivityLogService
from app.services.assembly import (
    ActivityLog,
    Environments,
    Jobs,
    JobServices,
    Swarm,
    SwarmServices,
)
from app.services.automation_rules import PanelRules
from app.services.container_logs import (
    DEFAULT_PAGE_TAIL,
    MAX_FILTER_CHARS,
    MAX_PAGE_TAIL,
    LogQuery,
)
from app.services.container_restrictions import (
    ALLOWED_LOG_DRIVERS,
    ALLOWED_LOG_OPTION_KEYS,
    option_label,
)
from app.services.environments import EnvironmentSummary
from app.services.host_objects import (
    DEFAULT_PAGE_SIZE,
)
from app.services.image_bindings import CredentialOffer
from app.services.object_locks import ObjectBusy, TooManyStreams
from app.services.pull_credentials import (
    CHOICE_FIELD,
    BindingCredentialMissing,
    CredentialChoice,
    CredentialChoiceRequired,
)
from app.services.registry_resolution import CredentialSetUnavailable
from app.services.service_logs import SERVICE_DEFAULT_TAIL, SERVICE_MAX_TAIL
from app.services.service_spec import ROW_CAPS, ROW_PREFIX, SCALAR_FIELDS
from app.services.service_spec_task import ULIMIT_NAMES
from app.services.swarm_service_queries import MODE_FILTERS
from app.services.swarm_service_views import (
    RestrictedService,
    ServiceChangedSinceReview,
    ServiceDetail,
    ServiceFilters,
    ServiceInvalid,
    ServiceNameInUse,
    ServiceRefused,
)
from app.web.activity_routes import (
    ACCESS_ACTIVITY,
    ALL_ACTIVITY,
    AUTOMATION_ACTIVITY,
    build_tab_activity_panel,
)
from app.web.container_routes import (
    CHECKED_FLAG,
    LOG_BUDGET_CHOICES,
    LOG_FALSE,
    LOG_PARAMS,
    LOG_SINCE_PRESETS,
    LOG_TAIL_CHOICES,
    LOG_TAIL_DIGITS,
    RECENT_PARAM,
)
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.image_helpers import credential_choice_of, selected_choice
from app.web.object_pages import (
    ACCESS_REMOVED,
    ACCESS_SAVED,
    CURSOR_MAX,
    AccessData,
    DigestField,
    Field,
    Filter,
    Region,
    administrator_detail,
    apply_access,
    bind_digest,
    blank_to_none,
    fragment_response,
    page_response,
    problem_page,
    query_of,
    redirect,
    remove_access,
    self_href,
    unavailable_page,
)
from app.web.origins import RequestOrigin
from app.web.pagination import Column, PageView
from app.web.problems import unreachable_copy
from app.web.registry_providers import CATALOGUE
from app.web.swarm_routes import (
    ACTION_ERRORS,
    UNAVAILABLE,
    Banner,
    ServiceId,
    failure_of,
    notice_flash,
    removal_flash,
    service_detail_href,
    service_list_href,
    swarm_path,
    swarm_router,
    version_of,
    whole_number,
)
from app.web.templating import is_htmx_request, render_fragment

CHOICE_MAX: Final = 32
MAX_FORM_FIELDS: Final = 2500
MAX_REPLICA_DIGITS: Final = 6
CHECKED_VALUES: Final = frozenset({"on", "true", "1", "yes"})
SERVICE_TABS: Final = ("overview", "configuration", "tasks", "logs", "automation", "access")
TAB_ACTIVITY: Final = {
    "overview": ALL_ACTIVITY,
    "automation": AUTOMATION_ACTIVITY,
    "access": ACCESS_ACTIVITY,
}
CONTROL_FIELDS: Final = frozenset(
    {"rows", "step", CHOICE_FIELD, "expected_version", "form_version"}
)
SERVICE_FIELDS: Final = frozenset(SCALAR_FIELDS) | CONTROL_FIELDS
SERVICE_ROW: Final = re.compile(
    r"(env|label|network|port|mount|config|secret|ulimit|logopt)-([0-9]{1,4})-([a-z_]+)"
)
ULIMIT_CHOICES: Final = sorted(ULIMIT_NAMES)
STANDARD_LOG_DRIVERS: Final = sorted(ALLOWED_LOG_DRIVERS)
STANDARD_LOG_OPTIONS: Final = ["", *sorted(ALLOWED_LOG_OPTION_KEYS)]

TABS_REGION: Final = Region("service-tabs", "outerHTML")

router = swarm_router()


@dataclass(frozen=True, slots=True)
class FormView:
    """The values and errors a service form shows, and how many rows each group has."""

    values: Mapping[str, str]
    errors: Mapping[str, str]
    adding: str = ""

    def get(self, name: str) -> str:
        """Return a submitted value, or the empty string."""
        return self.values.get(name, "")

    def checked(self, name: str) -> bool:
        """Report whether a checkbox was ticked."""
        return self.get(name).strip().casefold() in CHECKED_VALUES

    def indices(self, group: str) -> range:
        """Return the row numbers a group shows: the filled ones, a blank, and more on request."""
        prefix = ROW_PREFIX[group]
        used = 0
        for key in self.values:
            found = SERVICE_ROW.fullmatch(key)
            if found is not None and found[1] == prefix:
                used = max(used, int(found[2]) + 1)
        shown = used + 1 + (2 if self.adding == group else 0)
        return range(max(min(shown, ROW_CAPS[group]), used))

    def hidden_fields(self) -> list[tuple[str, str]]:
        """Return every form field a review carries on to the confirmation."""
        return [(key, value) for key, value in self.values.items() if key not in CONTROL_FIELDS]


async def _read_service_form(request: Request) -> dict[str, str]:
    posted = await request.form()
    values: dict[str, str] = {}
    for key, value in posted.items():
        if not isinstance(value, str):
            continue
        if key in SERVICE_FIELDS or SERVICE_ROW.fullmatch(key):
            values[key] = value
        if len(values) >= MAX_FORM_FIELDS:
            break
    return values


def _service_fields(values: Mapping[str, str]) -> dict[str, str]:
    """Return the fields the service parser reads, without this page's own controls."""
    return {key: value for key, value in values.items() if key not in CONTROL_FIELDS}


def _carried_choice(values: Mapping[str, str]) -> str:
    """Return the submitted credential choice if a form could have posted it, else blank."""
    text = values.get(CHOICE_FIELD, "")[:CHOICE_MAX].strip()
    try:
        return text if credential_choice_of(text) is not None else ""
    except ValidationFailed:
        return ""


def _choice_of(values: Mapping[str, str]) -> CredentialChoice | None:
    return credential_choice_of(_carried_choice(values))


def _rebind_refusal(invalid: ValidationFailed) -> str | None:
    """Return the refusal message when a standard user's choice would rebind an image."""
    if (
        isinstance(invalid, ServiceInvalid)
        and not isinstance(invalid, RestrictedService)
        and invalid.reason == "restricted"
    ):
        return invalid.fields.get("image")
    return None


async def _credential_offer(
    swarm: SwarmServices, actor: Principal, environment_id: int, image: str
) -> CredentialOffer | None:
    """Describe the credential choice for the image, or None when none can be asked."""
    if swarm.bindings is None or not image.strip():
        return None
    try:
        return await swarm.bindings.picker(actor, environment_id, image.strip())
    except (ValidationFailed, PermissionDenied):
        return None


SERVICE_COLUMNS: Final = (
    Column("name", "Name"),
    Column("mode", "Mode", optional=True, width="short"),
    Column("replicas", "Replicas", width="count"),
    Column("image", "Image", optional=True),
    Column("stack", "Stack", optional=True),
    Column("ports", "Ports", optional=True),
    Column("updated", "Updated", optional=True, width="time"),
    Column("actions", "Actions", width="actions"),
)
SERVICE_TABLES: Final = "swarm/services/_tables.html"
SERVICE_DETAIL: Final = "swarm/services/_detail.html"
CHOICES_TEMPLATE: Final = "swarm/services/_choices.html"


@router.get("/services", response_class=HTMLResponse, name="environment_swarm_service_list")
async def list_services(
    request: Request,
    swarm: Swarm,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    q: Filter = None,
    mode: Filter = None,
    stack: Filter = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=CURSOR_MAX)] = None,
) -> Response:
    """List the services the caller may see, read now."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    filters = ServiceFilters(blank_to_none(q), blank_to_none(mode), blank_to_none(stack))
    rows: list[Any] = []
    total = visible = 0
    can_create = False
    errors: tuple[str, ...] = ()
    failure: str | None = None
    status_code = 200
    next_cursor = None
    try:
        result = await swarm.services.page(
            actor, environment_id, filters, limit=limit, cursor=cursor
        )
    except ValidationFailed as invalid:
        errors, status_code = tuple(invalid.fields.values()), 400
    except UNAVAILABLE as unavailable:
        failure, status_code = str(unavailable), 503
    else:
        rows = list(result.page.items)
        total, visible, can_create = result.total, result.visible, result.can_create
        next_cursor = result.page.next_cursor
    params = [
        (key, value)
        for key, value in (("q", filters.q), ("mode", filters.mode), ("stack", filters.stack))
        if value
    ]
    pager = PageView(
        base_path=service_list_href(request, environment_id),
        limit=limit,
        item_count=len(rows),
        cursor=None if errors or failure else cursor,
        next_cursor=next_cursor,
        params=tuple(params),
    )
    results: dict[str, Any] = {
        "rows": rows,
        "columns": SERVICE_COLUMNS,
        "pager": pager,
        "total": total,
        "visible": visible,
        "filtered": bool(params),
        "errors": errors,
        "failure": failure,
        "retry_href": self_href(request),
    }
    if is_htmx_request(request):
        return fragment_response(
            request, SERVICE_TABLES, "services_results", status_code, oob=True, **results
        )
    return page_response(
        request,
        "swarm/services/list.html",
        {
            **results,
            "environment": environment,
            "q": filters.q or "",
            "mode": filters.mode or "",
            "stack": filters.stack or "",
            "mode_options": MODE_FILTERS,
            "can_create": can_create,
        },
        status_code,
    )


async def _form_response(
    request: Request,
    swarm: SwarmServices,
    environments: Environments,
    actor: Principal,
    environment_id: int,
    values: Mapping[str, str] | None,
    errors: Mapping[str, str],
    *,
    service_id: str | None = None,
    status_code: int = 200,
    banner: Banner | None = None,
    adding: str = "",
    choose_credentials: bool = False,
    choice_error: str | None = None,
) -> Response:
    """Render the create or edit form with what was submitted and what was wrong with it."""
    environment: EnvironmentSummary = (
        await environments.environments.get(actor, environment_id)
    ).summary
    editing = service_id is not None
    try:
        if service_id is None:
            await swarm.services.create_offer(actor, environment_id)
            offered = None
        else:
            offered = await swarm.services.edit_offer(actor, environment_id, service_id)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Service",
            unavailable,
            service_list_href(request, environment_id),
            "Back to services",
            region=None,
        )
    shown = dict(values) if values is not None else dict(offered.values if offered else {})
    if offered is None and values is None:
        shown = {"mode": "replicated", "replicas": "1"}
    if offered is not None:
        shown["mode"] = offered.mode if offered.mode in ("replicated", "global") else ""
    stored_image = "" if offered is None else offered.values.get("image", "").strip()
    offer = (
        await _credential_offer(swarm, actor, environment_id, shown.get("image", ""))
        if choose_credentials or shown.get("image", "").strip() != stored_image
        else None
    )
    selected = "" if offer is None else selected_choice(offer, shown.get(CHOICE_FIELD, ""))
    submit_href = (
        swarm_path(
            request,
            "service_review",
            environment_id=environment_id,
            service_id=service_id,
        )
        if service_id is not None
        else swarm_path(request, "service_create", environment_id=environment_id)
    )
    cancel_href = (
        service_detail_href(request, environment_id, service_id)
        if service_id is not None
        else service_list_href(request, environment_id)
    )
    return page_response(
        request,
        "swarm/services/edit.html" if editing else "swarm/services/new.html",
        {
            "environment": environment,
            "view": FormView(shown, errors, adding),
            "blank": FormView({}, {}),
            "mount_types": ["volume", "tmpfs", "bind"]
            if actor.is_administrator
            else ["volume", "tmpfs"],
            "offered": offered,
            "administrator": actor.is_administrator,
            "banner": banner,
            "engine_detail": None if banner is None else banner.detail,
            "offer": offer,
            "selected": selected,
            "choice_error": choice_error,
            "catalogue": CATALOGUE,
            "choices_href": swarm_path(
                request, "service_new_choices", environment_id=environment_id
            ),
            "submit_href": submit_href,
            "cancel_href": cancel_href,
            "row_caps": ROW_CAPS,
            "ulimit_names": ULIMIT_CHOICES,
            "log_drivers": STANDARD_LOG_DRIVERS,
            "log_option_keys": STANDARD_LOG_OPTIONS,
            "service_id": service_id,
        },
        status_code,
    )


@router.get("/services/new", response_class=HTMLResponse, name="environment_swarm_service_new")
async def new_service(
    request: Request,
    swarm: Swarm,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show the create form; create access is needed."""
    return await _form_response(request, swarm, environments, actor, environment_id, None, {})


@router.post(
    "/services/new/choices",
    response_class=HTMLResponse,
    name="environment_swarm_service_new_choices",
)
async def service_choices(
    request: Request, swarm: Swarm, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Render the credential picker for the image typed in the create form; nothing is recorded."""
    await swarm.services.require_creator(actor, environment_id)
    if not is_htmx_request(request):
        return redirect(request, swarm_path(request, "service_new", environment_id=environment_id))
    values = await _read_service_form(request)
    image = values.get("image", "").strip()
    offer: CredentialOffer | None = None
    image_error: str | None = None
    status_code = 200
    if image and swarm.bindings is not None:
        try:
            offer = await swarm.bindings.picker(actor, environment_id, image)
        except ValidationFailed as invalid:
            image_error, status_code = "; ".join(invalid.fields.values()), 400
    return render_fragment(
        request,
        CHOICES_TEMPLATE,
        "image_choices",
        status_code=status_code,
        offer=offer,
        selected="" if offer is None else selected_choice(offer),
        image_error=image_error,
        catalogue=CATALOGUE,
    )


@router.post("/services/new", response_class=HTMLResponse, name="environment_swarm_service_create")
async def create_service(
    request: Request,
    swarm: Swarm,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Create the service, or show the form again with what was wrong with it."""
    values = await _read_service_form(request)

    async def again(
        errors: Mapping[str, str],
        status_code: int,
        *,
        banner: Banner | None = None,
        credentials: bool = False,
        choice_error: str | None = None,
    ) -> Response:
        return await _form_response(
            request,
            swarm,
            environments,
            actor,
            environment_id,
            values,
            errors,
            status_code=status_code,
            banner=banner,
            adding=values.get("rows", ""),
            choose_credentials=credentials,
            choice_error=choice_error,
        )

    if values.get("rows") in ROW_PREFIX:
        return await again({}, 200)
    try:
        created = await swarm.service_actions.create(
            actor,
            environment_id,
            _service_fields(values),
            credential_choice=_choice_of(values),
        )
    except CredentialChoiceRequired as required:
        return await again(
            {}, 400, credentials=True, choice_error="; ".join(required.fields.values())
        )
    except BindingCredentialMissing as missing:
        return await again({}, 409, credentials=True, choice_error=str(missing))
    except CredentialSetUnavailable as unavailable_set:
        return await again({"image": str(unavailable_set)}, 409)
    except ServiceNameInUse as taken:
        return await again(taken.fields, 409)
    except ValidationFailed as invalid:
        refusal = _rebind_refusal(invalid)
        if refusal is not None:
            return await again({}, 403, credentials=True, choice_error=refusal)
        return await again(invalid.fields, 400)
    except ServiceRefused as refused:
        return await again(
            {},
            502,
            banner=Banner(
                "Docker refused this configuration",
                str(refused),
                administrator_detail(refused, actor),
            ),
        )
    except UNAVAILABLE as unavailable:
        return await again({}, 503, banner=Banner(*unreachable_copy(unavailable)))
    if created.warning is not None:
        notice = "created_unpinned"
    else:
        notice = "created" if created.control_created else "created_uncontrolled"
    return redirect(
        request,
        service_detail_href(request, environment_id, created.service_id),
        [notice_flash(notice)],
    )


def _log_checked(present: Mapping[str, str], name: str) -> bool:
    return present.get(name, "1").strip().lower() not in LOG_FALSE


def _log_time(timestamp: str | None) -> str | None:
    if timestamp is None:
        return None
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z", timestamp)
    if match is None:
        return timestamp
    date, clock, fraction = match.groups()
    return f"{date} {clock}.{(fraction or '').ljust(3, '0')[:3]}"


async def _logs_context(
    request: Request,
    swarm: SwarmServices,
    actor: Principal,
    environment_id: int,
    service_id: str,
    detail_href: str,
) -> dict[str, Any]:
    """Read the logs form and, only on request, the bounded tail shown without script."""
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
        query = LogQuery.from_params(
            params, default_tail=SERVICE_DEFAULT_TAIL, max_tail=SERVICE_MAX_TAIL
        )
    except ValidationFailed as invalid:
        errors = dict(invalid.fields)
    wants_recent = request.query_params.get(RECENT_PARAM) == CHECKED_FLAG
    recent = query is not None and wants_recent and not is_htmx_request(request)
    lines: list[dict[str, Any]] = []
    total = 0
    cut = False
    tasks = 0
    failure: dict[str, Any] | None = None
    if query is not None and recent:
        try:
            tail = await swarm.logs.tail(actor, environment_id, service_id, query)
        except TooManyStreams as refused:
            failure = {
                "status": 429,
                "title": "Too many live views are open",
                "message": str(refused),
            }
        except UNAVAILABLE as unavailable:
            failure = {
                "status": 503,
                "title": "Logs are not available",
                "message": str(unavailable),
            }
        else:
            folded = query.filter.casefold()
            total, cut, tasks = len(tail.lines), tail.cut, tail.tasks
            lines = [
                {
                    "time": _log_time(line.timestamp),
                    "task": line.origin,
                    "text": line.text,
                    "stderr": line.stream == "stderr",
                }
                for line in tail.lines
                if not folded or folded in line.text.casefold()
            ]
    tail_value = (present.get("tail", "").strip() or str(DEFAULT_PAGE_TAIL))[:16]
    tail_options = [str(choice) for choice in LOG_TAIL_CHOICES if choice <= SERVICE_MAX_TAIL]
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
    flags = [
        ("stdout", "1" if values["stdout"] else "0"),
        ("stderr", "1" if values["stderr"] else "0"),
        ("timestamps", "1" if values["timestamps"] else "0"),
    ]
    download_path = swarm_path(
        request, "service_logs_download", environment_id=environment_id, service_id=service_id
    )
    download_params = [("tail", "all")]
    download_params.extend(
        (name, present[name]) for name in ("since", "until") if present.get(name)
    )
    download_params.extend(flags)
    if present.get("q"):
        download_params.append(("q", present["q"]))
    recent_params = [("tab", "logs"), ("tail", tail_value)]
    recent_params.extend(
        (name, present[name]) for name in ("since", "until", "q") if present.get(name)
    )
    recent_params.extend(flags)
    recent_params.append((RECENT_PARAM, CHECKED_FLAG))
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
        "tasks": tasks,
        "clamped": clamped,
        "failure": failure,
        "page_tail": DEFAULT_PAGE_TAIL if query is None else (query.tail or 0),
        "stream_href": swarm_path(
            request, "service_logs_stream", environment_id=environment_id, service_id=service_id
        ),
        "download_base": download_path,
        "download_href": f"{download_path}?{urlencode(download_params)}",
        "refresh_href": self_href(request),
        "recent_href": f"{detail_href}?{urlencode(recent_params)}",
        "action_href": detail_href,
    }


async def _service_response(
    request: Request,
    swarm: SwarmServices,
    actor: Principal,
    environment_id: int,
    service_id: str,
    tab: str,
    *,
    status_code: int = 200,
    values: dict[str, Any] | None = None,
    fields: dict[str, str] | None = None,
    jobs: JobServices | None = None,
    activity: ActivityLogService | None = None,
) -> Response:
    if tab not in SERVICE_TABS:
        raise ResourceNotFound("not found")
    list_href = service_list_href(request, environment_id)
    detail_href = service_detail_href(request, environment_id, service_id)
    try:
        detail = await swarm.services.detail(actor, environment_id, service_id)
        tasks = panel = logs = None
        if tab == "tasks":
            tasks = await swarm.services.tasks(actor, environment_id, service_id)
        elif tab == "access":
            if not detail.can_manage_access:
                raise PermissionDenied("administrator role required")
            panel = await swarm.services.access_panel(actor, environment_id, service_id)
        elif tab == "logs":
            logs = await _logs_context(
                request, swarm, actor, environment_id, service_id, detail_href
            )
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Service", unavailable, list_href, "Back to services", region=TABS_REGION
        )
    visible = [name for name in SERVICE_TABS if detail.can_manage_access or name != "access"]
    arguments: dict[str, Any] = {
        "detail": detail,
        "tab": tab,
        "tasks": tasks,
        "panel": bind_digest(panel, values),
        "logs": logs,
        "values": values,
        "fields": fields or {},
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
            "service",
            environment_id,
            detail.id,
            TAB_ACTIVITY.get(tab),
        ),
    }
    if is_htmx_request(request):
        return fragment_response(
            request,
            SERVICE_DETAIL,
            "service_tabs",
            status_code,
            **arguments,
            **additions,
        )
    return page_response(
        request,
        "swarm/services/detail.html",
        {**arguments, **additions, "list_href": list_href},
        status_code,
    )


async def _automation_panel(
    jobs: JobServices, actor: Principal, detail: ServiceDetail
) -> PanelRules:
    return await jobs.rules.for_object(
        actor,
        detail.environment_id,
        name=detail.name,
        labels={},
        stack=detail.stack,
        kind="service",
    )


@router.get(
    "/services/{service_id}",
    response_class=HTMLResponse,
    name="environment_swarm_service_detail",
    dependencies=[RequestOrigin],
)
async def service_detail(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    jobs: Jobs,
    activity: ActivityLog,
    actor: CurrentUser,
    environment_id: PathId,
    tab: Annotated[str, Query(max_length=32)] = "overview",
) -> Response:
    """Show one service, one tab at a time; each tab has its own URL."""
    return await _service_response(
        request, swarm, actor, environment_id, service_id, tab, jobs=jobs, activity=activity
    )


@router.get(
    "/services/{service_id}/tasks",
    response_class=HTMLResponse,
    name="environment_swarm_service_tasks",
)
async def service_tasks(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Return the tasks panel on its own for the poll, or the tab as a page."""
    if not is_htmx_request(request):
        return await _service_response(request, swarm, actor, environment_id, service_id, "tasks")
    try:
        tasks = await swarm.services.tasks(actor, environment_id, service_id)
    except UNAVAILABLE as unavailable:
        return fragment_response(
            request,
            SERVICE_DETAIL,
            "tasks_unavailable",
            503,
            message=str(unavailable),
            href=self_href(request),
        )
    return fragment_response(request, SERVICE_DETAIL, "tasks_panel", tasks=tasks)


@router.get(
    "/services/{service_id}/edit",
    response_class=HTMLResponse,
    name="environment_swarm_service_edit",
)
async def edit_service(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show the edit form, filled from the stored configuration; write access is needed."""
    return await _form_response(
        request, swarm, environments, actor, environment_id, None, {}, service_id=service_id
    )


@router.post(
    "/services/{service_id}/edit",
    response_class=HTMLResponse,
    name="environment_swarm_service_review",
)
async def review_service(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Check the edit as the update would and name what it changes; nothing is sent to Docker."""
    values = await _read_service_form(request)

    async def again(
        errors: Mapping[str, str],
        status_code: int,
        *,
        banner: Banner | None = None,
        credentials: bool = False,
        choice_error: str | None = None,
    ) -> Response:
        return await _form_response(
            request,
            swarm,
            environments,
            actor,
            environment_id,
            values,
            errors,
            service_id=service_id,
            status_code=status_code,
            banner=banner,
            adding=values.get("rows", ""),
            choose_credentials=credentials,
            choice_error=choice_error,
        )

    if values.get("rows") in ROW_PREFIX or values.get("step") == "edit":
        return await again({}, 200)
    try:
        review = await swarm.services.review(
            actor,
            environment_id,
            service_id,
            _service_fields(values),
            form_version=version_of(values.get("form_version", "")),
            credential_choice=_choice_of(values),
        )
    except CredentialChoiceRequired as required:
        return await again(
            {}, 400, credentials=True, choice_error="; ".join(required.fields.values())
        )
    except BindingCredentialMissing as missing:
        return await again({}, 409, credentials=True, choice_error=str(missing))
    except CredentialSetUnavailable as unavailable_set:
        return await again({"image": str(unavailable_set)}, 409)
    except ServiceChangedSinceReview as stale:
        values.pop("form_version", None)
        return await again(stale.fields, 409)
    except ValidationFailed as invalid:
        refusal = _rebind_refusal(invalid)
        if refusal is not None:
            return await again({}, 403, credentials=True, choice_error=refusal)
        return await again(invalid.fields, 400)
    except UNAVAILABLE as unavailable:
        return await again({}, 503, banner=Banner(*unreachable_copy(unavailable)))
    environment = (await environments.environments.get(actor, environment_id)).summary
    offer = (
        await _credential_offer(swarm, actor, environment_id, values.get("image", ""))
        if review.image_changed
        else None
    )
    return page_response(
        request,
        "swarm/services/review.html",
        {
            "environment": environment,
            "review": review,
            "view": FormView(values, {}),
            "restricted": [option_label(option) for option in review.restricted],
            "offer": offer,
            "selected": ""
            if offer is None
            else selected_choice(offer, values.get(CHOICE_FIELD, "")),
            "choice_error": None,
            "catalogue": CATALOGUE,
            "confirm_href": swarm_path(
                request, "service_update", environment_id=environment_id, service_id=service_id
            ),
            "edit_href": swarm_path(
                request, "service_review", environment_id=environment_id, service_id=service_id
            ),
            "detail_href": service_detail_href(request, environment_id, service_id),
        },
    )


@router.post(
    "/services/{service_id}/edit/confirm",
    response_class=HTMLResponse,
    name="environment_swarm_service_update",
)
async def update_service(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Apply the reviewed edit to exactly the version that was reviewed."""
    values = await _read_service_form(request)
    detail_href = service_detail_href(request, environment_id, service_id)

    async def again(
        errors: Mapping[str, str],
        status_code: int,
        *,
        credentials: bool = False,
        choice_error: str | None = None,
    ) -> Response:
        return await _form_response(
            request,
            swarm,
            environments,
            actor,
            environment_id,
            values,
            errors,
            service_id=service_id,
            status_code=status_code,
            choose_credentials=credentials,
            choice_error=choice_error,
        )

    try:
        updated = await swarm.service_actions.update(
            actor,
            environment_id,
            service_id,
            _service_fields(values),
            expected_version=version_of(values.get("expected_version", "")),
            credential_choice=_choice_of(values),
        )
    except CredentialChoiceRequired as required:
        return await again(
            {}, 400, credentials=True, choice_error="; ".join(required.fields.values())
        )
    except BindingCredentialMissing as missing:
        return await again({}, 409, credentials=True, choice_error=str(missing))
    except CredentialSetUnavailable as unavailable_set:
        return await again({"image": str(unavailable_set)}, 409)
    except (ServiceChangedSinceReview, ObjectBusy) as error:
        return problem_page(
            request,
            heading="Edit service",
            failure=failure_of(error, swarm, actor),
            back_href=detail_href,
            back_label="Back to the service",
            review_href=swarm_path(
                request, "service_edit", environment_id=environment_id, service_id=service_id
            ),
            region=None,
        )
    except ValidationFailed as invalid:
        refusal = _rebind_refusal(invalid)
        if refusal is not None:
            return await again({}, 403, credentials=True, choice_error=refusal)
        return await again(invalid.fields, 400)
    except ACTION_ERRORS as error:
        return problem_page(
            request,
            heading="Edit service",
            failure=failure_of(error, swarm, actor),
            back_href=detail_href,
            back_label="Back to the service",
            review_href=swarm_path(
                request, "service_edit", environment_id=environment_id, service_id=service_id
            ),
            region=None,
        )
    if updated.unchanged:
        notice = "unchanged"
    else:
        notice = "updated_unpinned" if updated.warning else "updated"
    return redirect(request, detail_href, [notice_flash(notice)])


def _service_problem(
    request: Request,
    swarm: SwarmServices,
    actor: Principal,
    error: Exception,
    environment_id: int,
    service_id: str,
    heading: str,
    review: str,
) -> Response:
    detail_href = service_detail_href(request, environment_id, service_id)
    return problem_page(
        request,
        heading=heading,
        failure=failure_of(error, swarm, actor),
        back_href=detail_href,
        back_label="Back to the service",
        review_href=swarm_path(
            request, review, environment_id=environment_id, service_id=service_id
        ),
        region=None,
    )


@router.get(
    "/services/{service_id}/scale",
    response_class=HTMLResponse,
    name="environment_swarm_service_scale",
)
async def scale_confirm(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
    replicas: Annotated[str, Query(max_length=16)] = "",
) -> Response:
    """Name the tasks a change of replica count stops; write holders only."""
    count = whole_number(replicas, MAX_REPLICA_DIGITS)
    try:
        if count is None:
            raise ValidationFailed({"replicas": "Enter a whole number of replicas."})
        impact = await swarm.services.scale_impact(actor, environment_id, service_id, count)
    except ACTION_ERRORS as error:
        return _service_problem(
            request,
            swarm,
            actor,
            error,
            environment_id,
            service_id,
            "Scale service",
            "service_detail",
        )
    return page_response(
        request,
        "swarm/services/scale_confirm.html",
        {
            "impact": impact,
            "detail_href": service_detail_href(request, environment_id, service_id),
            "run_href": swarm_path(
                request, "service_scale_run", environment_id=environment_id, service_id=service_id
            ),
        },
    )


@router.post(
    "/services/{service_id}/scale",
    response_class=HTMLResponse,
    name="environment_swarm_service_scale_run",
)
async def run_scale(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
    replicas: Field = "",
    expected_version: Field = "",
    expected_replicas: Field = "",
    confirmed: Field = "",
) -> Response:
    """Set the replica count; a count below the reviewed one goes through its confirmation first."""
    count = whole_number(replicas, MAX_REPLICA_DIGITS)
    held = whole_number(expected_replicas, MAX_REPLICA_DIGITS)
    try:
        version = version_of(expected_version)
        if count is None or held is None:
            raise ValidationFailed({"replicas": "Enter a whole number of replicas."})
        if count < held and confirmed != "yes":
            return redirect(
                request,
                swarm_path(
                    request, "service_scale", environment_id=environment_id, service_id=service_id
                )
                + f"?{query_of('replicas', str(count))}",
            )
        await swarm.service_actions.scale(
            actor,
            environment_id,
            service_id,
            replicas=count,
            expected_version=version,
            expected_replicas=held,
        )
    except ACTION_ERRORS as error:
        return _service_problem(
            request,
            swarm,
            actor,
            error,
            environment_id,
            service_id,
            "Scale service",
            "service_detail",
        )
    return redirect(
        request,
        service_detail_href(request, environment_id, service_id),
        [notice_flash("scaled")],
    )


@router.get(
    "/services/{service_id}/force-update",
    response_class=HTMLResponse,
    name="environment_swarm_service_force_update",
)
async def force_update_confirm(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Name the running tasks a forced update replaces; write holders only."""
    try:
        impact = await swarm.services.force_impact(actor, environment_id, service_id)
    except ACTION_ERRORS as error:
        return _service_problem(
            request,
            swarm,
            actor,
            error,
            environment_id,
            service_id,
            "Force update",
            "service_detail",
        )
    return page_response(
        request,
        "swarm/services/force_update_confirm.html",
        {
            "impact": impact,
            "detail_href": service_detail_href(request, environment_id, service_id),
            "run_href": swarm_path(
                request,
                "service_force_update_run",
                environment_id=environment_id,
                service_id=service_id,
            ),
        },
    )


@router.post(
    "/services/{service_id}/force-update",
    response_class=HTMLResponse,
    name="environment_swarm_service_force_update_run",
)
async def run_force_update(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
    expected_version: Field = "",
) -> Response:
    """Replace the running tasks of the version that was reviewed."""
    try:
        await swarm.service_actions.force_update(
            actor, environment_id, service_id, expected_version=version_of(expected_version)
        )
    except ACTION_ERRORS as error:
        return _service_problem(
            request,
            swarm,
            actor,
            error,
            environment_id,
            service_id,
            "Force update",
            "service_force_update",
        )
    return redirect(
        request,
        service_detail_href(request, environment_id, service_id),
        [notice_flash("force_updated")],
    )


@router.get(
    "/services/{service_id}/rollback",
    response_class=HTMLResponse,
    name="environment_swarm_service_rollback",
)
async def rollback_confirm(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Name what returning to the previous configuration changes; write holders only."""
    try:
        impact = await swarm.services.rollback_impact(actor, environment_id, service_id)
    except ACTION_ERRORS as error:
        return _service_problem(
            request, swarm, actor, error, environment_id, service_id, "Roll back", "service_detail"
        )
    return page_response(
        request,
        "swarm/services/rollback_confirm.html",
        {
            "impact": impact,
            "detail_href": service_detail_href(request, environment_id, service_id),
            "run_href": swarm_path(
                request,
                "service_rollback_run",
                environment_id=environment_id,
                service_id=service_id,
            ),
        },
    )


@router.post(
    "/services/{service_id}/rollback",
    response_class=HTMLResponse,
    name="environment_swarm_service_rollback_run",
)
async def run_rollback(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
    expected_version: Field = "",
) -> Response:
    """Return the service to the previous configuration that was reviewed."""
    try:
        await swarm.service_actions.rollback(
            actor, environment_id, service_id, expected_version=version_of(expected_version)
        )
    except ACTION_ERRORS as error:
        return _service_problem(
            request,
            swarm,
            actor,
            error,
            environment_id,
            service_id,
            "Roll back",
            "service_rollback",
        )
    return redirect(
        request,
        service_detail_href(request, environment_id, service_id),
        [notice_flash("rolled_back")],
    )


@router.get(
    "/services/{service_id}/remove",
    response_class=HTMLResponse,
    name="environment_swarm_service_remove",
)
async def remove_service_confirm(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Name the service, its tasks and its ports; write holders only."""
    try:
        impact = await swarm.services.remove_impact(actor, environment_id, service_id)
    except ACTION_ERRORS as error:
        return _service_problem(
            request,
            swarm,
            actor,
            error,
            environment_id,
            service_id,
            "Remove service",
            "service_detail",
        )
    return page_response(
        request,
        "swarm/services/remove_confirm.html",
        {
            "impact": impact,
            "detail_href": service_detail_href(request, environment_id, service_id),
            "run_href": swarm_path(
                request, "service_remove_run", environment_id=environment_id, service_id=service_id
            ),
        },
    )


@router.post(
    "/services/{service_id}/remove",
    response_class=HTMLResponse,
    name="environment_swarm_service_remove_run",
)
async def run_remove_service(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
    expected_name: Field = "",
    expected_version: Field = "",
) -> Response:
    """Remove the reviewed service; its name and version must be the ones that were shown."""
    try:
        if not expected_name:
            raise ValidationFailed({"service": "The confirmation was incomplete. Review it again."})
        await swarm.service_actions.remove(
            actor,
            environment_id,
            service_id,
            expected_name=expected_name,
            expected_version=version_of(expected_version),
        )
    except ACTION_ERRORS as error:
        return _service_problem(
            request,
            swarm,
            actor,
            error,
            environment_id,
            service_id,
            "Remove service",
            "service_remove",
        )
    return redirect(
        request,
        service_list_href(request, environment_id),
        [removal_flash("service_removed")],
    )


@router.post(
    "/services/{service_id}/access",
    response_class=HTMLResponse,
    name="environment_swarm_service_access_save",
)
async def save_service_access(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
    data: AccessData,
) -> Response:
    """Create or replace the resource control over one service; administrators only."""
    try:
        panel = await swarm.services.access_panel(actor, environment_id, service_id)
    except UNAVAILABLE as unavailable:
        return _access_unavailable(request, environment_id, unavailable)
    refusal = await apply_access(actor, swarm.access.controls, swarm.services, panel, data)
    if refusal is None:
        return redirect(
            request, _access_href(request, environment_id, service_id), [Flash("ok", ACCESS_SAVED)]
        )
    return await _service_response(
        request,
        swarm,
        actor,
        environment_id,
        service_id,
        "access",
        status_code=refusal.status_code,
        values=refusal.values,
        fields=refusal.fields,
    )


@router.post(
    "/services/{service_id}/access/delete",
    response_class=HTMLResponse,
    name="environment_swarm_service_access_delete",
)
async def delete_service_access(
    service_id: ServiceId,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
    expected_digest: DigestField = "",
) -> Response:
    """Remove the resource control over one service; administrators only."""
    try:
        panel = await swarm.services.access_panel(actor, environment_id, service_id)
    except UNAVAILABLE as unavailable:
        return _access_unavailable(request, environment_id, unavailable)
    refusal = await remove_access(actor, swarm.access.controls, panel, expected_digest)
    if refusal is None:
        return redirect(
            request,
            _access_href(request, environment_id, service_id),
            [Flash("ok", ACCESS_REMOVED)],
        )
    return await _service_response(
        request,
        swarm,
        actor,
        environment_id,
        service_id,
        "access",
        status_code=refusal.status_code,
        fields=refusal.fields,
    )


def _access_href(request: Request, environment_id: int, service_id: str) -> str:
    base = service_detail_href(request, environment_id, service_id)
    return f"{base}?{query_of('tab', 'access')}"


def _access_unavailable(request: Request, environment_id: int, unavailable: Exception) -> Response:
    return unavailable_page(
        request,
        "Service",
        unavailable,
        service_list_href(request, environment_id),
        "Back to services",
        region=None,
    )
