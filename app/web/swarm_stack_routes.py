"""Swarm stack namespace pages."""

from typing import Annotated, Any, Final

from fastapi import Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.services.activity_log import ActivityLogService
from app.services.assembly import (
    ActivityLog,
    Environments,
    Jobs,
    JobServices,
    Stacks,
    StackServices,
    Swarm,
    SwarmServices,
)
from app.services.automation_rules import PanelRules
from app.services.containers import AccessPanel
from app.services.swarm_stacks import ManagedStackDetail
from app.web.activity_routes import (
    ACCESS_ACTIVITY,
    ALL_ACTIVITY,
    AUTOMATION_ACTIVITY,
    ActivityPanel,
    build_tab_activity_panel,
)
from app.web.flash import Flash, FlashLevel
from app.web.identifiers import PathId
from app.web.object_pages import (
    ACCESS_REMOVED,
    ACCESS_SAVED,
    AccessData,
    DigestField,
    Failure,
    Filter,
    Region,
    apply_access,
    bind_digest,
    blank_to_none,
    busy_message,
    fragment_response,
    page_response,
    query_of,
    redirect,
    remove_access,
    self_href,
    unavailable_page,
)
from app.web.origins import RequestOrigin
from app.web.pagination import Column, PageView
from app.web.sorting import DirectionParam, SortParam, view_sort
from app.web.stack_log_routes import LOGS_TAB, logs_status, logs_tab_context
from app.web.stack_pages import DISMISSED_PAGE, DISMISSED_PARAM
from app.web.stack_routes import compose_notice
from app.web.swarm_routes import (
    UNAVAILABLE,
    Namespace,
    swarm_path,
    swarm_router,
)
from app.web.templating import is_htmx_request

STACK_TABS: Final = ("overview", "logs", "access")
MANAGED_TABS: Final = ("overview", "file", "variables", "files", "logs", "automation", "access")
TAB_ACTIVITY: Final = {
    "overview": ALL_ACTIVITY,
    "automation": AUTOMATION_ACTIVITY,
    "access": ACCESS_ACTIVITY,
}
LIST_NOTICES: Final = {"forgotten": "Stack forgotten. Its objects are still on the cluster."}
DETAIL_NOTICES: Final[dict[str, tuple[FlashLevel, str]]] = {
    "saved": ("ok", "Changes saved. Redeploy the stack to apply them."),
    "unchanged": ("ok", "Nothing changed."),
    "adopted": (
        "ok",
        "Stack adopted. Nothing was changed on the cluster. Redeploy the stack to apply its file.",
    ),
}

TABS_REGION: Final = Region("stack-tabs", "outerHTML")
LIST_TABLES: Final = "swarm/stacks/_tables.html"

router = swarm_router()


STACK_LIMIT: Final = 500
STACK_COLUMNS: Final = (
    Column("name", "Namespace", sortable=True),
    Column("state", "State", sortable=True, width="badge"),
    Column("last", "Last operation", sortable=True, optional=True, width="time"),
    Column("services", "Services", sortable=True, numeric=True, optional=True, width="count"),
    Column("configs", "Configs", numeric=True, optional=True, width="count"),
    Column("secrets", "Secrets", numeric=True, optional=True, width="count"),
    Column("networks", "Networks", numeric=True, optional=True, width="count"),
    Column("actions", "Actions", width="actions"),
)


async def _can_create(swarm: SwarmServices, actor: Principal, environment_id: int) -> bool:
    try:
        await swarm.stacks.create_context(actor, environment_id)
    except PermissionDenied:
        return False
    return True


@router.get("/stacks", response_class=HTMLResponse, name="environment_swarm_stack_list")
async def list_stacks(
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    q: Filter = None,
    dismissed: Annotated[str, Query(max_length=8)] = "",
    sort: SortParam = None,
    direction: DirectionParam = None,
) -> Response:
    """List the managed stacks and the other namespaces the caller may see, merged by name."""
    return await list_response(
        request,
        swarm,
        stacks,
        environments,
        actor,
        environment_id,
        needle=blank_to_none(q),
        show_dismissed=dismissed == DISMISSED_PAGE,
        sort=sort,
        direction=direction,
    )


async def list_response(
    request: Request,
    swarm: SwarmServices,
    stacks: StackServices,
    environments: Environments,
    actor: Principal,
    environment_id: int,
    *,
    needle: str | None = None,
    show_dismissed: bool = False,
    refusal: Failure | None = None,
    sort: str | None = None,
    direction: str | None = None,
) -> Response:
    """Render the Swarm stacks list, with `refusal` above it when an action was turned down."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    page = None
    errors: tuple[str, ...] = ()
    refused: dict[str, str] = {}
    failure: str | None = None
    status_code = refusal.status if refusal else 200
    try:
        page = await swarm.stacks.page(
            actor,
            environment_id,
            q=needle,
            show_dismissed=show_dismissed,
            sort=sort,
            direction=direction,
        )
    except ValidationFailed as invalid:
        errors, status_code, refused = tuple(invalid.fields.values()), 400, invalid.fields
    except UNAVAILABLE as unavailable:
        failure, status_code = str(unavailable), 503
    rows = list(page.rows) if page is not None else []
    params = ((DISMISSED_PARAM, DISMISSED_PAGE),) if show_dismissed else ()
    view_key, view_direction = view_sort(sort, direction, refused)
    pager = PageView(
        base_path=swarm_path(request, "stack_list", environment_id=environment_id),
        limit=STACK_LIMIT,
        item_count=len(rows),
        cursor=None,
        next_cursor=None,
        sort=view_key,
        direction=view_direction,
        params=(*((("q", needle),) if needle else ()), *params),
    )
    results: dict[str, Any] = {
        "page": page,
        "rows": rows,
        "columns": STACK_COLUMNS,
        "pager": pager,
        "q": needle or "",
        "errors": errors,
        "failure": failure,
        "show_dismissed": show_dismissed,
        "dismissed_hidden": page.dismissed_hidden if page else 0,
        "retry_href": self_href(request),
        "can_create": await _can_create(swarm, actor, environment_id),
    }
    if is_htmx_request(request):
        return fragment_response(
            request,
            LIST_TABLES,
            "stacks_results",
            status_code,
            oob=True,
            environment_id=environment_id,
            **results,
        )
    return page_response(
        request,
        "swarm/stacks/list.html",
        {
            **results,
            "environment": environment,
            "refusal": refusal,
            "compose": compose_notice(stacks.swarm_planner.compose_status()),
        },
        status_code,
    )


async def _stack_response(
    request: Request,
    swarm: SwarmServices,
    actor: Principal,
    environment_id: int,
    namespace: str,
    tab: str,
    *,
    status_code: int = 200,
    values: dict[str, Any] | None = None,
    fields: dict[str, str] | None = None,
    jobs: JobServices | None = None,
    activity: ActivityLogService | None = None,
) -> Response:
    list_href = swarm_path(request, "stack_list", environment_id=environment_id)
    detail_href = swarm_path(
        request, "stack_detail", environment_id=environment_id, namespace=namespace
    )
    logs: dict[str, Any] | None = None
    try:
        managed = await swarm.stacks.managed_detail(
            actor, environment_id, namespace, open_masks=tab == "variables"
        )
        if managed is not None:
            if tab not in MANAGED_TABS:
                raise ResourceNotFound("not found")
            panel = None
            if tab == "access":
                if not managed.can_manage:
                    raise PermissionDenied("administrator role required")
                panel = await swarm.stacks.access_panel(actor, environment_id, namespace)
            elif tab == LOGS_TAB:
                logs = await logs_tab_context(
                    request,
                    swarm.stack_logs,
                    "swarm",
                    actor,
                    environment_id,
                    namespace,
                    detail_href,
                )
                status_code = logs_status(logs, status_code)
            return _managed_response(
                request,
                managed,
                tab,
                panel,
                list_href,
                logs,
                status_code=status_code,
                values=values,
                fields=fields,
                automation=(
                    await _automation_panel(jobs, actor, managed)
                    if jobs is not None and (tab == "automation" or not is_htmx_request(request))
                    else None
                ),
                activity_log=await build_tab_activity_panel(
                    request,
                    activity,
                    actor,
                    "stack",
                    environment_id,
                    managed.stack.name,
                    TAB_ACTIVITY.get(tab),
                ),
            )
        if tab not in STACK_TABS:
            raise ResourceNotFound("not found")
        detail = await swarm.stacks.detail(actor, environment_id, namespace)
        panel = None
        if tab == "access":
            if not detail.can_manage_access:
                raise PermissionDenied("administrator role required")
            panel = await swarm.stacks.access_panel(actor, environment_id, namespace)
        elif tab == LOGS_TAB:
            logs = await logs_tab_context(
                request, swarm.stack_logs, "swarm", actor, environment_id, namespace, detail_href
            )
            status_code = logs_status(logs, status_code)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Stack", unavailable, list_href, "Back to Swarm stacks", region=TABS_REGION
        )
    tabs = [name for name in STACK_TABS if detail.can_manage_access or name != "access"]
    arguments: dict[str, Any] = {
        "detail": detail,
        "tab": tab,
        "tabs": tabs,
        "panel": bind_digest(panel, values),
        "values": values,
        "fields": fields or {},
    }
    if logs is not None:
        arguments["logs"] = logs
    if is_htmx_request(request):
        return fragment_response(
            request, "swarm/stacks/_detail.html", "stack_tabs", status_code, **arguments
        )
    return page_response(
        request,
        "swarm/stacks/detail.html",
        {**arguments, "managed": False, "list_href": list_href},
        status_code,
    )


async def _automation_panel(
    jobs: JobServices, actor: Principal, detail: ManagedStackDetail
) -> PanelRules:
    return await jobs.rules.for_object(
        actor,
        detail.environment_id,
        name=detail.stack.name,
        labels={},
        stack=None,
        kind="swarm_stack",
        key=detail.stack.name,
    )


def _managed_response(
    request: Request,
    detail: ManagedStackDetail,
    tab: str,
    panel: AccessPanel | None,
    list_href: str,
    logs: dict[str, Any] | None,
    *,
    status_code: int,
    values: dict[str, Any] | None,
    fields: dict[str, str] | None,
    automation: PanelRules | None,
    activity_log: ActivityPanel | None,
) -> Response:
    visible = [name for name in MANAGED_TABS if detail.can_manage or name != "access"]
    arguments: dict[str, Any] = {
        "detail": detail,
        "tab": tab,
        "panel": bind_digest(panel, values),
        "values": values,
        "fields": fields or {},
    }
    if logs is not None:
        arguments["logs"] = logs
    additions: dict[str, Any] = {
        "tab_keys": visible,
        "automation": automation,
        "activity_log": activity_log,
    }
    if is_htmx_request(request):
        return fragment_response(
            request,
            "swarm/stacks/_managed.html",
            "managed_tabs",
            status_code,
            **arguments,
            **additions,
        )
    return page_response(
        request,
        "swarm/stacks/detail.html",
        {
            **arguments,
            **additions,
            "managed": True,
            "list_href": list_href,
            "busy_text": busy_message(detail.busy, "this stack") if detail.busy else None,
            "compose": compose_notice(detail.compose),
        },
        status_code,
    )


@router.get(
    "/stacks/{namespace}",
    response_class=HTMLResponse,
    name="environment_swarm_stack_detail",
    dependencies=[RequestOrigin],
)
async def stack_detail(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    jobs: Jobs,
    activity: ActivityLog,
    actor: CurrentUser,
    environment_id: PathId,
    tab: Annotated[str, Query(max_length=32)] = "overview",
) -> Response:
    """Show a managed stack or a read-only namespace; each tab has its own URL."""
    return await _stack_response(
        request,
        swarm,
        actor,
        environment_id,
        namespace,
        tab,
        jobs=jobs,
        activity=activity,
    )


@router.post(
    "/stacks/{namespace}/access",
    response_class=HTMLResponse,
    name="environment_swarm_stack_access_save",
)
async def save_stack_access(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
    data: AccessData,
) -> Response:
    """Create or replace the stack control, then refresh its member list; administrators only."""
    try:
        context = await swarm.stacks.access_context(actor, environment_id, namespace)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Stack",
            unavailable,
            swarm_path(request, "stack_list", environment_id=environment_id),
            "Back to Swarm stacks",
            region=None,
        )
    refusal = await apply_access(
        actor, swarm.access.controls, swarm.services, context.panel, data, context.members
    )
    if refusal is None:
        return redirect(
            request, _access_href(request, environment_id, namespace), [Flash("ok", ACCESS_SAVED)]
        )
    return await _stack_response(
        request,
        swarm,
        actor,
        environment_id,
        namespace,
        "access",
        status_code=refusal.status_code,
        values=refusal.values,
        fields=refusal.fields,
    )


@router.post(
    "/stacks/{namespace}/access/delete",
    response_class=HTMLResponse,
    name="environment_swarm_stack_access_delete",
)
async def delete_stack_access(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
    expected_digest: DigestField = "",
) -> Response:
    """Remove the stack control; administrators only."""
    try:
        panel = await swarm.stacks.access_panel(actor, environment_id, namespace)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Stack",
            unavailable,
            swarm_path(request, "stack_list", environment_id=environment_id),
            "Back to Swarm stacks",
            region=None,
        )
    refusal = await remove_access(actor, swarm.access.controls, panel, expected_digest)
    if refusal is None:
        return redirect(
            request,
            _access_href(request, environment_id, namespace),
            [Flash("ok", ACCESS_REMOVED)],
        )
    return await _stack_response(
        request,
        swarm,
        actor,
        environment_id,
        namespace,
        "access",
        status_code=refusal.status_code,
        fields=refusal.fields,
    )


def _access_href(request: Request, environment_id: int, namespace: str) -> str:
    base = swarm_path(request, "stack_detail", environment_id=environment_id, namespace=namespace)
    return f"{base}?{query_of('tab', 'access')}"
