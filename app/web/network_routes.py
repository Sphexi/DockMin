"""Network pages and the container attachment routes."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Form, Path, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import PermissionDenied, ResourceNotFound, SelfProtected, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.services.access import is_canonical_id
from app.services.assembly import Access, Environments, Networks, NetworkServices
from app.services.container_actions import ContainerGone, ContainerStateConflict, EngineRefused
from app.services.containers import ContainerListTooLarge
from app.services.environments import EnvironmentSummary
from app.services.host_objects import (
    DEFAULT_PAGE_SIZE,
    MAX_PRUNE,
    USAGE_FILTERS,
    NetworkListTooLarge,
    ServiceListTooLarge,
)
from app.services.network_addressing import OFFERED_DRIVERS, STANDARD_OPTIONS
from app.services.network_attachments import AttachInvalid, AttachRefused, DetachRefused
from app.services.networks import (
    DRIVER_FILTERS,
    KIND_FILTERS,
    SCOPE_FILTERS,
    AttachOffer,
    NetworkChangedSinceReview,
    NetworkFilters,
    NetworkGone,
    NetworkInUse,
    NetworkNameInUse,
    NetworkPredefined,
    NetworkPruneTarget,
    NetworkRefused,
    NetworkRow,
)
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectBusy
from app.web.flash import Flash, FlashLevel
from app.web.identifiers import PathId
from app.web.object_pages import (
    ACCESS_REMOVED,
    ACCESS_SAVED,
    CURSOR_MAX,
    RETRYABLE,
    ROW_CAP,
    ROW_KINDS,
    AccessData,
    DigestField,
    Failure,
    Field,
    Filter,
    Region,
    Short,
    administrator_detail,
    apply_access,
    bind_digest,
    blank_to_none,
    busy_message,
    fragment_response,
    page_response,
    problem_page,
    query_of,
    read_form,
    redirect,
    remove_access,
    route,
    row_count,
    self_href,
    unavailable_page,
)
from app.web.origins import RequestOrigin
from app.web.pagination import Column, PageView
from app.web.problems import unreachable_copy
from app.web.results import show_result
from app.web.stack_links import stack_links
from app.web.templating import is_htmx_request

TABS_REGION: Final = Region("network-tabs", "outerHTML")

router = APIRouter(
    prefix="/environments/{environment_id}", tags=["networks"], dependencies=[Depends(require_user)]
)

SEGMENT_MAX: Final = 128
CHANGED_HEADERS: Final = {"HX-Trigger": "container-changed"}
TABS: Final = ("overview", "containers", "access")
ALIAS_SEPARATORS: Final = re.compile(r"[\s,]+")
NOTICES: Final[dict[str, tuple[FlashLevel, str]]] = {
    "created": ("ok", "Network created."),
    "created_uncontrolled": (
        "warn",
        "Network created, but its ownership could not be recorded. An administrator can assign "
        "access to it.",
    ),
}
LIST_NOTICES: Final = {"removed": "Network removed."}
NETWORK_FIELDS: Final = frozenset(
    {
        "name",
        "driver",
        "internal",
        "attachable",
        "enable_ipv6",
        "subnet_v4",
        "gateway_v4",
        "ip_range_v4",
        "subnet_v6",
        "gateway_v6",
        "ip_range_v6",
        "step",
        "rows",
    }
)
ADMIN_OPTION_KEYS: Final = {
    "bridge": (
        "com.docker.network.bridge.name",
        "com.docker.network.bridge.enable_icc",
        "com.docker.network.bridge.enable_ip_masquerade",
        "com.docker.network.bridge.host_binding_ipv4",
        "com.docker.network.driver.mtu",
    ),
    "overlay": ("encrypted", "com.docker.network.driver.mtu"),
    "macvlan": ("parent", "macvlan_mode", "com.docker.network.driver.mtu"),
    "ipvlan": ("parent", "ipvlan_mode", "ipvlan_flag", "com.docker.network.driver.mtu"),
}

LIST_TABLES: Final = "networks/_tables.html"
DETAIL: Final = "networks/_detail.html"
FORM: Final = "networks/_form.html"
ATTACH: Final = "networks/_attach.html"


def _canonical_network_id(network_id: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    """Refuse a malformed id as a missing one, before any Engine call and for every role."""
    if not is_canonical_id("network", network_id):
        raise ResourceNotFound("not found")
    return network_id


def _canonical_container_id(container_id: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    if not is_canonical_id("container", container_id):
        raise ResourceNotFound("not found")
    return container_id


NetworkId = Annotated[str, Depends(_canonical_network_id)]
ContainerId = Annotated[str, Depends(_canonical_container_id)]


def _failure_of(error: Exception, networks: NetworkServices, actor: Principal) -> Failure:
    """Map an error a network action can raise to the notice the caller is shown."""
    if isinstance(error, ObjectBusy):
        busy = networks.actions.describe_busy(error, actor)
        return Failure(409, "This object is busy", busy_message(busy, "it"))
    if isinstance(error, NetworkChangedSinceReview | NetworkNameInUse):
        return Failure(409, "The network changed", "; ".join(error.fields.values()))
    if isinstance(error, ValidationFailed):
        return Failure(400, "Check the request", "; ".join(error.fields.values()))
    if isinstance(error, NetworkInUse):
        return Failure(409, "The network is in use", str(error))
    if isinstance(error, NetworkPredefined):
        return Failure(409, "The network cannot be removed", str(error))
    if isinstance(error, SelfProtected):
        return Failure(409, "This container is protected", str(error))
    if isinstance(error, ContainerStateConflict):
        return Failure(409, "The container cannot do that now", str(error))
    if isinstance(error, NetworkGone):
        return Failure(404, "The network no longer exists", str(error))
    if isinstance(error, ContainerGone):
        return Failure(404, "The container no longer exists", str(error))
    if isinstance(error, NetworkRefused | AttachRefused | DetachRefused | EngineRefused):
        return Failure(
            502, "Docker refused the action", str(error), administrator_detail(error, actor)
        )
    return Failure(503, *unreachable_copy(error))


ACTION_ERRORS: Final = (
    SelfProtected,
    ObjectBusy,
    ValidationFailed,
    NetworkInUse,
    NetworkPredefined,
    ContainerStateConflict,
    NetworkGone,
    ContainerGone,
    NetworkRefused,
    AttachRefused,
    DetachRefused,
    EngineRefused,
    EnvironmentUnavailable,
    NetworkListTooLarge,
    ContainerListTooLarge,
    ServiceListTooLarge,
    ObjectTooLarge,
)
UNAVAILABLE: Final = (
    EnvironmentUnavailable,
    NetworkListTooLarge,
    ContainerListTooLarge,
    ServiceListTooLarge,
    ObjectTooLarge,
)


def _list_path(request: Request, environment_id: int) -> str:
    return route(request, "environment_network_list", environment_id=environment_id)


def _detail_path(request: Request, environment_id: int, network_id: str) -> str:
    return route(
        request, "environment_network_detail", environment_id=environment_id, network_id=network_id
    )


def _container_tab_path(request: Request, environment_id: int, container_id: str) -> str:
    base = route(
        request,
        "environment_container_detail",
        environment_id=environment_id,
        container_id=container_id,
    )
    return f"{base}?{query_of('tab', 'networks')}"


COLUMNS: Final = (
    Column("name", "Name"),
    Column("driver", "Driver", width="short"),
    Column("scope", "Scope", optional=True, width="short"),
    Column("subnets", "Subnets", optional=True, width="medium"),
    Column("flags", "Flags", optional=True, width="badge"),
    Column("stack", "Stack", optional=True, width="medium"),
    Column("used", "Used by"),
    Column("actions", "Actions", width="actions"),
)


@router.get("/networks", response_class=HTMLResponse, name="environment_network_list")
async def list_networks(
    request: Request,
    networks: Networks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    q: Filter = None,
    driver: Filter = None,
    scope: Filter = None,
    usage: Filter = None,
    kind: Filter = None,
    stack: Filter = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=CURSOR_MAX)] = None,
) -> Response:
    """List the networks of the host the caller may see, read now."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    filters = NetworkFilters(
        q=blank_to_none(q),
        driver=blank_to_none(driver),
        scope=blank_to_none(scope),
        usage=blank_to_none(usage),
        kind=blank_to_none(kind),
        stack=blank_to_none(stack),
    )
    rows: list[NetworkRow] = []
    total = visible = 0
    can_create = can_prune = False
    errors: tuple[str, ...] = ()
    failure: str | None = None
    status_code = 200
    next_cursor = None
    try:
        result = await networks.queries.page(
            actor, environment_id, filters, limit=limit, cursor=cursor
        )
    except ValidationFailed as invalid:
        errors, status_code = tuple(invalid.fields.values()), 400
    except UNAVAILABLE as unavailable:
        failure, status_code = str(unavailable), 503
    else:
        rows = list(result.page.items)
        total, visible = result.total, result.visible
        can_create, can_prune = result.can_create, result.can_prune
        next_cursor = result.page.next_cursor
    params = [
        (key, value)
        for key, value in (
            ("q", filters.q),
            ("driver", filters.driver),
            ("scope", filters.scope),
            ("usage", filters.usage),
            ("kind", filters.kind),
            ("stack", filters.stack),
        )
        if value
    ]
    pager = PageView(
        base_path=_list_path(request, environment_id),
        limit=limit,
        item_count=len(rows),
        cursor=None if errors or failure else cursor,
        next_cursor=next_cursor,
        params=tuple(params),
    )
    results: dict[str, Any] = {
        "rows": rows,
        "columns": COLUMNS,
        "pager": pager,
        "total": total,
        "visible": visible,
        "filtered": bool(params),
        "errors": errors,
        "failure": failure,
        "retry_href": self_href(request),
        "stack_links": await stack_links(
            request, actor, environment_id, ((row.stack_kind, row.stack) for row in rows)
        ),
    }
    if is_htmx_request(request):
        return fragment_response(
            request, LIST_TABLES, "networks_results", status_code, oob=True, **results
        )
    return page_response(
        request,
        "networks/list.html",
        {
            **results,
            "environment": environment,
            "q": filters.q or "",
            "driver": filters.driver or "",
            "scope": filters.scope or "",
            "usage": filters.usage or "",
            "kind": filters.kind or "",
            "stack": filters.stack or "",
            "driver_options": DRIVER_FILTERS,
            "scope_options": SCOPE_FILTERS,
            "usage_options": USAGE_FILTERS,
            "kind_options": KIND_FILTERS,
            "can_create": can_create,
            "can_prune": can_prune,
        },
        status_code,
    )


@dataclass(frozen=True, slots=True)
class _Banner:
    title: str
    message: str
    detail: str | None = None


def _offered_drivers(environment_type: str | None, actor: Principal) -> tuple[str, ...]:
    offered = OFFERED_DRIVERS["swarm" if environment_type == "swarm" else "standalone"]
    if actor.is_administrator:
        return offered
    return tuple(driver for driver in offered if driver in STANDARD_OPTIONS)


def _option_keys(driver: str, actor: Principal) -> tuple[str, ...]:
    if actor.is_administrator:
        return ADMIN_OPTION_KEYS[driver]
    return tuple(sorted(STANDARD_OPTIONS[driver]))


def _form_arguments(
    environment_type: str | None,
    actor: Principal,
    values: Mapping[str, str],
    errors: Mapping[str, str],
    *,
    adding: str = "",
) -> dict[str, Any]:
    """Collect what the create form renders from the submitted values and the caller's role."""
    offered = _offered_drivers(environment_type, actor)
    chosen = values.get("driver", "")
    driver = chosen if chosen in offered else offered[0]
    return {
        "offered": offered,
        "driver": driver,
        "values": values,
        "errors": errors,
        "is_admin": actor.is_administrator,
        "option_keys": _option_keys(driver, actor),
        "option_count": row_count(values, "option", add=adding == "option"),
        "label_count": row_count(values, "label", add=adding == "label"),
        "row_cap": ROW_CAP,
    }


def _form_page(
    request: Request,
    environment: EnvironmentSummary,
    actor: Principal,
    values: Mapping[str, str],
    errors: Mapping[str, str],
    *,
    status_code: int = 200,
    banner: _Banner | None = None,
    adding: str = "",
) -> Response:
    environment_id = environment.id
    arguments = _form_arguments(environment.type, actor, values, errors, adding=adding)
    return page_response(
        request,
        "networks/new.html",
        {
            **arguments,
            "environment": environment,
            "banner": banner,
            "engine_detail": None if banner is None else banner.detail,
            "post_href": route(
                request, "environment_network_create", environment_id=environment_id
            ),
            "list_href": _list_path(request, environment_id),
        },
        status_code,
    )


@router.get("/networks/new", response_class=HTMLResponse, name="environment_network_new")
async def new_network(
    request: Request,
    networks: Networks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    driver: Annotated[str, Query(max_length=16)] = "",
) -> Response:
    """Show the create form; the driver in the query picks the fields it takes."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    await networks.queries.require_create(actor, environment_id)
    return _form_page(request, environment, actor, {"driver": driver} if driver else {}, {})


@router.post("/networks/new", response_class=HTMLResponse, name="environment_network_create")
async def create_network(
    request: Request,
    networks: Networks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Create the network, or show the form again with the fields of the chosen driver."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    await networks.queries.require_create(actor, environment_id)
    values = await read_form(request, NETWORK_FIELDS)
    adding = values.get("rows", "")
    if values.get("step") == "driver" or adding in ROW_KINDS:
        if is_htmx_request(request) and values.get("step") == "driver":
            arguments = _form_arguments(environment.type, actor, values, {})
            return fragment_response(
                request,
                FORM,
                "driver_fields",
                driver=arguments["driver"],
                values=values,
                errors={},
                is_admin=arguments["is_admin"],
                option_keys=arguments["option_keys"],
            )
        return _form_page(request, environment, actor, values, {}, adding=adding)
    try:
        created = await networks.actions.create(actor, environment_id, values)
    except NetworkNameInUse as taken:
        return _form_page(request, environment, actor, values, taken.fields, status_code=409)
    except ValidationFailed as invalid:
        return _form_page(request, environment, actor, values, invalid.fields, status_code=400)
    except NetworkRefused as refused:
        return _form_page(
            request,
            environment,
            actor,
            values,
            {},
            status_code=502,
            banner=_Banner(
                "Docker refused this configuration",
                str(refused),
                administrator_detail(refused, actor),
            ),
        )
    except UNAVAILABLE as unavailable:
        return _form_page(
            request,
            environment,
            actor,
            values,
            {},
            status_code=503,
            banner=_Banner(*unreachable_copy(unavailable)),
        )
    notice = "created" if created.control_created else "created_uncontrolled"
    level, text = NOTICES[notice]
    return redirect(
        request, _detail_path(request, environment_id, created.network_id), [Flash(level, text)]
    )


@router.get("/networks/prune", response_class=HTMLResponse, name="environment_network_prune")
async def prune_form(
    request: Request,
    networks: Networks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """List the networks a prune would remove; administrators only."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    try:
        plan = await networks.queries.prune_plan(actor, environment_id)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Prune networks",
            unavailable,
            _list_path(request, environment_id),
            "Back to networks",
            region=None,
        )
    return page_response(
        request,
        "networks/prune_confirm.html",
        {
            "environment": environment,
            "plan": plan,
            "list_href": _list_path(request, environment_id),
            "run_href": route(
                request, "environment_network_prune_run", environment_id=environment_id
            ),
        },
    )


def _prune_targets(values: Sequence[str]) -> list[NetworkPruneTarget]:
    targets: list[NetworkPruneTarget] = []
    for value in values:
        network_id, separator, name = value.partition(":")
        if not separator or not name or not is_canonical_id("network", network_id):
            raise ValidationFailed({"networks": "The selection was incomplete. Review it again."})
        targets.append(NetworkPruneTarget(network_id, name))
    return targets


@router.post("/networks/prune", response_class=HTMLResponse, name="environment_network_prune_run")
async def run_prune(
    request: Request,
    networks: Networks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    target: Annotated[list[Short] | None, Form(max_length=MAX_PRUNE)] = None,
) -> Response:
    """Remove exactly the networks the confirmation listed that are still candidates."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    try:
        result = await networks.actions.run_prune(
            actor, environment_id, _prune_targets(target or [])
        )
    except ACTION_ERRORS as error:
        return problem_page(
            request,
            heading="Prune networks",
            failure=_failure_of(error, networks, actor),
            back_href=_list_path(request, environment_id),
            back_label="Back to networks",
            review_href=route(request, "environment_network_prune", environment_id=environment_id),
            environment_name=environment.name,
            region=None,
        )
    return show_result(
        request,
        "networks/prune_result.html",
        {
            "environment": environment,
            "result": result,
            "list_href": _list_path(request, environment_id),
        },
    )


async def _detail_response(
    request: Request,
    networks: NetworkServices,
    actor: Principal,
    environment_id: int,
    network_id: str,
    tab: str,
    *,
    status_code: int = 200,
    values: dict[str, Any] | None = None,
    fields: dict[str, str] | None = None,
) -> Response:
    if tab not in TABS:
        raise ResourceNotFound("not found")
    try:
        detail = await networks.queries.detail(actor, environment_id, network_id)
        panel = None
        if tab == "access":
            if not detail.can_manage_access:
                raise PermissionDenied("administrator role required")
            panel = await networks.queries.access_panel(actor, environment_id, network_id)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Network",
            unavailable,
            _list_path(request, environment_id),
            "Back to networks",
            region=TABS_REGION,
        )
    tabs = [name for name in TABS if detail.can_manage_access or name != "access"]
    arguments: dict[str, Any] = {
        "detail": detail,
        "tab": tab,
        "tabs": tabs,
        "panel": bind_digest(panel, values),
        "values": values,
        "fields": fields or {},
        "stack_links": (
            await stack_links(request, actor, environment_id, [(detail.stack_kind, detail.stack)])
            if tab == "overview"
            else {}
        ),
    }
    if is_htmx_request(request):
        return fragment_response(request, DETAIL, "network_tabs", status_code, **arguments)
    return page_response(
        request,
        "networks/detail.html",
        {
            **arguments,
            "list_href": _list_path(request, environment_id),
        },
        status_code,
    )


@router.get(
    "/networks/{network_id}",
    response_class=HTMLResponse,
    name="environment_network_detail",
    dependencies=[RequestOrigin],
)
async def network_detail(
    network_id: NetworkId,
    request: Request,
    networks: Networks,
    actor: CurrentUser,
    environment_id: PathId,
    tab: Annotated[str, Query(max_length=32)] = "overview",
) -> Response:
    """Show one network, one tab at a time; each tab has its own URL."""
    return await _detail_response(request, networks, actor, environment_id, network_id, tab)


@router.get(
    "/networks/{network_id}/remove", response_class=HTMLResponse, name="environment_network_remove"
)
async def remove_confirm(
    network_id: NetworkId,
    request: Request,
    networks: Networks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Name the network and the containers using it; write holders only."""
    detail_href = _detail_path(request, environment_id, network_id)
    try:
        impact = await networks.queries.remove_impact(actor, environment_id, network_id)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Remove network", unavailable, detail_href, "Back", region=None
        )
    return page_response(
        request,
        "networks/remove_confirm.html",
        {
            "impact": impact,
            "detail_href": detail_href,
            "run_href": route(
                request,
                "environment_network_remove_run",
                environment_id=environment_id,
                network_id=network_id,
            ),
        },
    )


@router.post(
    "/networks/{network_id}/remove",
    response_class=HTMLResponse,
    name="environment_network_remove_run",
)
async def run_remove(
    network_id: NetworkId,
    request: Request,
    networks: Networks,
    actor: CurrentUser,
    environment_id: PathId,
    expected_name: Field = "",
    expected_in_use: Annotated[str, Form(max_length=8)] = "",
) -> Response:
    """Remove the reviewed network; the name and the use must be the ones that were shown."""
    detail_href = _detail_path(request, environment_id, network_id)
    in_use = (
        int(expected_in_use) if expected_in_use.isascii() and expected_in_use.isdigit() else None
    )
    try:
        if in_use is None or not expected_name:
            raise ValidationFailed({"network": "The confirmation was incomplete. Review it again."})
        await networks.actions.remove(
            actor,
            environment_id,
            network_id,
            expected_name=expected_name,
            expected_in_use=in_use,
        )
    except ACTION_ERRORS as error:
        return problem_page(
            request,
            heading="Remove network",
            failure=_failure_of(error, networks, actor),
            back_href=detail_href,
            back_label="Back to the network",
            review_href=route(
                request,
                "environment_network_remove",
                environment_id=environment_id,
                network_id=network_id,
            ),
            region=None,
        )
    return redirect(
        request, _list_path(request, environment_id), [Flash("ok", LIST_NOTICES["removed"])]
    )


@router.post(
    "/networks/{network_id}/access",
    response_class=HTMLResponse,
    name="environment_network_access_save",
)
async def save_access(
    network_id: NetworkId,
    request: Request,
    networks: Networks,
    access: Access,
    actor: CurrentUser,
    environment_id: PathId,
    data: AccessData,
) -> Response:
    """Create or replace the resource control over one network; administrators only."""
    try:
        panel = await networks.queries.access_panel(actor, environment_id, network_id)
    except UNAVAILABLE as unavailable:
        return _access_unavailable(request, environment_id, unavailable)
    refusal = await apply_access(actor, access.controls, networks.queries, panel, data)
    if refusal is not None:
        return await _detail_response(
            request,
            networks,
            actor,
            environment_id,
            network_id,
            "access",
            status_code=refusal.status_code,
            values=refusal.values,
            fields=refusal.fields,
        )
    return redirect(
        request,
        _access_path(request, environment_id, network_id),
        [Flash("ok", ACCESS_SAVED)],
    )


@router.post(
    "/networks/{network_id}/access/delete",
    response_class=HTMLResponse,
    name="environment_network_access_delete",
)
async def delete_access(
    network_id: NetworkId,
    request: Request,
    networks: Networks,
    access: Access,
    actor: CurrentUser,
    environment_id: PathId,
    expected_digest: DigestField = "",
) -> Response:
    """Remove the resource control over one network; administrators only."""
    try:
        panel = await networks.queries.access_panel(actor, environment_id, network_id)
    except UNAVAILABLE as unavailable:
        return _access_unavailable(request, environment_id, unavailable)
    refusal = await remove_access(actor, access.controls, panel, expected_digest)
    if refusal is not None:
        return await _detail_response(
            request,
            networks,
            actor,
            environment_id,
            network_id,
            "access",
            status_code=refusal.status_code,
            fields=refusal.fields,
        )
    return redirect(
        request,
        _access_path(request, environment_id, network_id),
        [Flash("ok", ACCESS_REMOVED)],
    )


def _access_path(request: Request, environment_id: int, network_id: str) -> str:
    return f"{_detail_path(request, environment_id, network_id)}?{query_of('tab', 'access')}"


def _access_unavailable(request: Request, environment_id: int, unavailable: Exception) -> Response:
    return unavailable_page(
        request,
        "Network",
        unavailable,
        _list_path(request, environment_id),
        "Back to networks",
        region=None,
    )


def _aliases_of(text: str) -> tuple[str, ...]:
    return tuple(alias for alias in ALIAS_SEPARATORS.split(text.strip()) if alias)


async def _connect_response(
    request: Request,
    networks: NetworkServices,
    actor: Principal,
    environment_id: int,
    container_id: str,
    values: dict[str, str],
    errors: dict[str, str],
    status_code: int,
) -> Response:
    """Show the connect form again with the submitted values and what was wrong with them."""
    back_href = _container_tab_path(request, environment_id, container_id)
    try:
        offer: AttachOffer = await networks.queries.attach_offer(
            actor, environment_id, container_id
        )
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Attach to a network", unavailable, back_href, "Back", region=None
        )
    if is_htmx_request(request):
        return fragment_response(
            request, ATTACH, "attach_panel", status_code, offer=offer, values=values, errors=errors
        )
    return page_response(
        request,
        "networks/connect.html",
        {"offer": offer, "values": values, "errors": errors},
        status_code,
    )


@router.get(
    "/containers/{container_id}/networks/connect",
    response_class=HTMLResponse,
    name="environment_container_network_connect",
)
async def connect_form(
    container_id: ContainerId,
    request: Request,
    networks: Networks,
    actor: CurrentUser,
    environment_id: PathId,
    network: Annotated[str, Query(max_length=SEGMENT_MAX)] = "",
) -> Response:
    """Ask which network to attach the container to; write holders only."""
    values = {"network": network} if network else {}
    return await _connect_response(
        request, networks, actor, environment_id, container_id, values, {}, 200
    )


@router.post(
    "/containers/{container_id}/networks/connect",
    response_class=HTMLResponse,
    name="environment_container_network_connect_run",
)
async def run_connect(
    container_id: ContainerId,
    request: Request,
    networks: Networks,
    actor: CurrentUser,
    environment_id: PathId,
    network: Field = "",
    aliases: Field = "",
    ipv4: Field = "",
    ipv6: Field = "",
) -> Response:
    """Attach the container to the chosen network with the aliases and addresses given."""
    values = {"network": network, "aliases": aliases, "ipv4": ipv4, "ipv6": ipv6}
    network_id = network.strip()
    try:
        if not is_canonical_id("network", network_id):
            raise AttachInvalid({"network": "Choose one of the listed networks."})
        outcome = await networks.attachments.connect(
            actor,
            environment_id,
            container_id,
            network_id,
            aliases=_aliases_of(aliases),
            ipv4=ipv4,
            ipv6=ipv6,
        )
    except ValidationFailed as invalid:
        return await _connect_response(
            request, networks, actor, environment_id, container_id, values, invalid.fields, 400
        )
    except ACTION_ERRORS as error:
        failure = _failure_of(error, networks, actor)
        return await _connect_response(
            request,
            networks,
            actor,
            environment_id,
            container_id,
            values,
            {"form": failure.message},
            failure.status,
        )
    if is_htmx_request(request):
        return fragment_response(
            request,
            ATTACH,
            "attach_done",
            200,
            CHANGED_HEADERS,
            message=f"Attached {outcome.container_name} to {outcome.network_name}.",
        )
    return redirect(
        request,
        _container_tab_path(request, environment_id, container_id),
        [Flash("ok", f"Attached {outcome.container_name} to {outcome.network_name}.")],
    )


@router.get(
    "/containers/{container_id}/networks/{network_id}/disconnect",
    response_class=HTMLResponse,
    name="environment_container_network_disconnect",
)
async def disconnect_confirm(
    container_id: ContainerId,
    network_id: NetworkId,
    request: Request,
    networks: Networks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Name the attachment about to end; write holders over the container only."""
    back_href = _container_tab_path(request, environment_id, container_id)
    try:
        impact = await networks.attachments.disconnect_impact(
            actor, environment_id, container_id, network_id
        )
    except UNAVAILABLE as unavailable:
        if is_htmx_request(request):
            return fragment_response(
                request,
                ATTACH,
                "detach_notice",
                503,
                title=unreachable_copy(unavailable).title,
                message=unreachable_copy(unavailable).message,
            )
        return unavailable_page(
            request, "Detach from a network", unavailable, back_href, "Back", region=None
        )
    if is_htmx_request(request):
        return fragment_response(request, ATTACH, "detach_dialog", impact=impact)
    return page_response(
        request, "networks/disconnect_confirm.html", {"impact": impact, "back_href": back_href}
    )


@router.post(
    "/containers/{container_id}/networks/{network_id}/disconnect",
    response_class=HTMLResponse,
    name="environment_container_network_disconnect_run",
)
async def run_disconnect(
    container_id: ContainerId,
    network_id: NetworkId,
    request: Request,
    networks: Networks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Detach the container from the network; Docker is never asked to force it."""
    back_href = _container_tab_path(request, environment_id, container_id)
    try:
        outcome = await networks.attachments.disconnect(
            actor, environment_id, container_id, network_id
        )
    except ACTION_ERRORS as error:
        failure = _failure_of(error, networks, actor)
        review_href = route(
            request,
            "environment_container_network_disconnect",
            environment_id=environment_id,
            container_id=container_id,
            network_id=network_id,
        )
        if is_htmx_request(request):
            return fragment_response(
                request,
                ATTACH,
                "detach_notice",
                failure.status,
                title=failure.title,
                message=failure.message,
                retry_href=review_href if failure.status in RETRYABLE else None,
            )
        return problem_page(
            request,
            heading="Detach from a network",
            failure=failure,
            back_href=back_href,
            back_label="Back to the container",
            review_href=review_href,
            region=None,
        )
    if is_htmx_request(request):
        return fragment_response(
            request,
            ATTACH,
            "detach_done",
            200,
            CHANGED_HEADERS,
            message=f"Detached {outcome.container_name} from {outcome.network_name}.",
        )
    return redirect(
        request,
        back_href,
        [Flash("ok", f"Detached {outcome.container_name} from {outcome.network_name}.")],
    )
