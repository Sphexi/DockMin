"""Volume pages."""

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Form, Path, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, get_activity, get_runtime, require_user
from app.auth.errors import PermissionDenied, ResourceNotFound, SelfProtected, ValidationFailed
from app.auth.principal import Principal
from app.auth.services import AuthRuntime
from app.db.engine import Session, get_session
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.services.access import is_canonical_id
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    Access,
    AccessServices,
    Environments,
    Volumes,
    VolumeServices,
    get_access_services,
    get_engine_runtime,
    get_object_inspector,
)
from app.services.authorisation import ObjectInspector
from app.services.container_actions import (
    MAX_BATCH,
    ContainerGone,
    ContainerStateConflict,
    EngineRefused,
)
from app.services.containers import ContainerListTooLarge
from app.services.engine_setup import EngineRuntime
from app.services.environments import EnvironmentSummary
from app.services.host_objects import (
    DEFAULT_PAGE_SIZE,
    MAX_PRUNE,
    USAGE_FILTERS,
    VolumeListTooLarge,
)
from app.services.object_inspector import (
    VOLUME_OR_NETWORK_NAME,
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
)
from app.services.object_locks import ObjectBusy
from app.services.volume_backfill import (
    BackfillChanged,
    VolumeBackfillService,
    build_volume_backfill,
)
from app.services.volume_views import (
    MAX_FORCE_CONTAINERS,
    ForceRefused,
    PruneMode,
    VolumeBatchTarget,
    VolumeChangedSinceReview,
    VolumeFilters,
    VolumeGone,
    VolumeInUse,
    VolumeNameInUse,
    VolumePruneTarget,
    VolumeRefused,
    VolumeRow,
)
from app.services.volumes import DRIVER_FILTERS, KIND_FILTERS, PRUNE_MODES
from app.web.flash import Flash, FlashLevel
from app.web.identifiers import PathId
from app.web.object_pages import (
    ACCESS_REMOVED,
    ACCESS_SAVED,
    CURSOR_MAX,
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
from app.web.templating import is_htmx_request

TABS_REGION: Final = Region("volume-tabs", "outerHTML")

router = APIRouter(
    prefix="/environments/{environment_id}/volumes",
    tags=["volumes"],
    dependencies=[Depends(require_user)],
)

MAX_FORCE_FORM: Final = 64
NAME_MAX: Final = 255
COUNT_DIGITS: Final = 6
DIGEST_MAX: Final = 64
BATCH_PARAMS_MAX: Final = 100
CONFIRM_YES: Final = "yes"
FORCE_PAGE: Final = "1"
TABS: Final = ("overview", "containers", "access")
VOLUME_FIELDS: Final = frozenset({"name", "driver", "rows"})
NOTICES: Final[dict[str, tuple[FlashLevel, str]]] = {
    "created": ("ok", "Volume created."),
    "created_uncontrolled": (
        "warn",
        "Volume created, but its ownership could not be recorded. An administrator can assign "
        "access to it.",
    ),
    "preexisting": (
        "warn",
        "A volume with this name appeared just before the request. It was left as it is and no "
        "ownership was recorded for it.",
    ),
}
LIST_NOTICES: Final = {"removed": "Volume removed."}

LIST_TABLES: Final = "volumes/_tables.html"
DETAIL: Final = "volumes/_detail.html"


async def get_volume_backfill(
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> VolumeBackfillService:
    """Provide the owner backfill bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    return build_volume_backfill(
        db=db,
        clock=runtime.clock,
        authoriser=access.authoriser,
        inspector=inspector,
        locks=engine.locks,
        controls=access.controls,
        activity=activity,
    )


VolumeBackfill = Annotated[VolumeBackfillService, Depends(get_volume_backfill)]


def _canonical_volume_name(volume_name: Annotated[str, Path(max_length=NAME_MAX)]) -> str:
    """Refuse a malformed name as a missing one, before any Engine call and for every role."""
    if VOLUME_OR_NETWORK_NAME.fullmatch(volume_name) is None:
        raise ResourceNotFound("not found")
    return volume_name


VolumeName = Annotated[str, Depends(_canonical_volume_name)]


def _failure_of(error: Exception, volumes: VolumeServices, actor: Principal) -> Failure:
    """Map an error a volume action can raise to the notice the caller is shown."""
    if isinstance(error, ObjectBusy):
        busy = volumes.actions.describe_busy(error, actor)
        return Failure(409, "This volume is busy", busy_message(busy, "it"))
    if isinstance(error, SelfProtected):
        return Failure(409, "Protected", str(error))
    if isinstance(error, VolumeChangedSinceReview | VolumeNameInUse):
        return Failure(409, "The volume changed", "; ".join(error.fields.values()))
    if isinstance(error, ValidationFailed):
        return Failure(400, "Check the request", "; ".join(error.fields.values()))
    if isinstance(error, VolumeInUse):
        return Failure(409, "The volume is in use", str(error))
    if isinstance(error, ForceRefused):
        return Failure(409, "The removal cannot go ahead", str(error))
    if isinstance(error, ContainerStateConflict):
        return Failure(409, "A container cannot do that now", str(error))
    if isinstance(error, VolumeGone):
        return Failure(404, "The volume no longer exists", str(error))
    if isinstance(error, ContainerGone):
        return Failure(404, "A container no longer exists", str(error))
    if isinstance(error, VolumeRefused | EngineRefused):
        return Failure(
            502, "Docker refused the action", str(error), administrator_detail(error, actor)
        )
    return Failure(503, *unreachable_copy(error))


ACTION_ERRORS: Final = (
    SelfProtected,
    ObjectBusy,
    ValidationFailed,
    VolumeInUse,
    ForceRefused,
    ContainerStateConflict,
    VolumeGone,
    ContainerGone,
    VolumeRefused,
    EngineRefused,
    EnvironmentUnavailable,
    VolumeListTooLarge,
    ContainerListTooLarge,
    ObjectTooLarge,
)
UNAVAILABLE: Final = (
    EnvironmentUnavailable,
    VolumeListTooLarge,
    ContainerListTooLarge,
    ObjectTooLarge,
)


def _list_path(request: Request, environment_id: int) -> str:
    return route(request, "environment_volume_list", environment_id=environment_id)


def _detail_path(request: Request, environment_id: int, volume_name: str) -> str:
    return route(
        request, "environment_volume_detail", environment_id=environment_id, volume_name=volume_name
    )


COLUMNS: Final = (
    Column("name", "Name"),
    Column("driver", "Driver", optional=True, width="short"),
    Column("created", "Created", optional=True, width="time"),
    Column("stack", "Stack", optional=True, width="medium"),
    Column("used", "Used by", width="names"),
    Column("actions", "Actions", width="actions"),
)


@router.get("", response_class=HTMLResponse, name="environment_volume_list")
async def list_volumes(
    request: Request,
    volumes: Volumes,
    backfill: VolumeBackfill,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    q: Filter = None,
    driver: Filter = None,
    usage: Filter = None,
    kind: Filter = None,
    stack: Filter = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=CURSOR_MAX)] = None,
) -> Response:
    """List the volumes of the host the caller may see, read now."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    filters = VolumeFilters(
        q=blank_to_none(q),
        driver=blank_to_none(driver),
        usage=blank_to_none(usage),
        kind=blank_to_none(kind),
        stack=blank_to_none(stack),
    )
    rows: list[VolumeRow] = []
    total = visible = 0
    can_create = can_prune = False
    errors: tuple[str, ...] = ()
    failure: str | None = None
    status_code = 200
    next_cursor = None
    try:
        result = await volumes.queries.page(
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
        "max_batch": MAX_BATCH,
    }
    if is_htmx_request(request):
        return fragment_response(
            request, LIST_TABLES, "volumes_results", status_code, oob=True, **results
        )
    can_backfill = status_code == 200 and await backfill.has_uncontrolled(actor, environment_id)
    return page_response(
        request,
        "volumes/list.html",
        {
            **results,
            "environment": environment,
            "q": filters.q or "",
            "driver": filters.driver or "",
            "usage": filters.usage or "",
            "kind": filters.kind or "",
            "stack": filters.stack or "",
            "driver_options": DRIVER_FILTERS,
            "usage_options": USAGE_FILTERS,
            "kind_options": KIND_FILTERS,
            "can_create": can_create,
            "can_prune": can_prune,
            "can_backfill": can_backfill,
        },
        status_code,
    )


def _form_page(
    request: Request,
    environment: EnvironmentSummary,
    actor: Principal,
    values: Mapping[str, str],
    errors: Mapping[str, str],
    *,
    status_code: int = 200,
    banner: Failure | None = None,
    adding: str = "",
) -> Response:
    return page_response(
        request,
        "volumes/new.html",
        {
            "environment": environment,
            "values": values,
            "errors": errors,
            "banner": banner,
            "engine_detail": None if banner is None else banner.detail,
            "is_admin": actor.is_administrator,
            "option_count": row_count(values, "option", add=adding == "option"),
            "label_count": row_count(values, "label", add=adding == "label"),
            "row_cap": ROW_CAP,
            "post_href": route(request, "environment_volume_create", environment_id=environment.id),
            "list_href": _list_path(request, environment.id),
        },
        status_code,
    )


@router.get("/-/new", response_class=HTMLResponse, name="environment_volume_new")
async def new_volume(
    request: Request,
    volumes: Volumes,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show the create form; create access is needed."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    await volumes.queries.require_create(actor, environment_id)
    return _form_page(request, environment, actor, {}, {})


@router.post("/-/new", response_class=HTMLResponse, name="environment_volume_create")
async def create_volume(
    request: Request,
    volumes: Volumes,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Create the volume, or show the form again with more rows."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    await volumes.queries.require_create(actor, environment_id)
    values = await read_form(request, VOLUME_FIELDS)
    adding = values.get("rows", "")
    if adding in ROW_KINDS:
        return _form_page(request, environment, actor, values, {}, adding=adding)
    try:
        created = await volumes.actions.create(actor, environment_id, values)
    except VolumeNameInUse as taken:
        return _form_page(request, environment, actor, values, taken.fields, status_code=409)
    except ObjectBusy as busy:
        return _form_page(
            request,
            environment,
            actor,
            values,
            {},
            status_code=409,
            banner=Failure(
                409,
                "This volume is busy",
                busy_message(volumes.actions.describe_busy(busy, actor), "the volume"),
            ),
        )
    except ValidationFailed as invalid:
        return _form_page(request, environment, actor, values, invalid.fields, status_code=400)
    except VolumeRefused as refused:
        return _form_page(
            request,
            environment,
            actor,
            values,
            {},
            status_code=502,
            banner=Failure(
                502,
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
            banner=Failure(503, *unreachable_copy(unavailable)),
        )
    if created.preexisting:
        notice = "preexisting"
    else:
        notice = "created" if created.control_created else "created_uncontrolled"
    level, text = NOTICES[notice]
    return redirect(
        request,
        _detail_path(request, environment_id, created.name),
        [Flash(level, text)],
    )


def _prune_mode(text: str) -> PruneMode | None:
    return next((mode for mode in PRUNE_MODES if mode == text), None)


@router.get("/-/prune", response_class=HTMLResponse, name="environment_volume_prune")
async def prune_form(
    request: Request,
    volumes: Volumes,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    mode: Annotated[str, Query(max_length=16)] = "",
) -> Response:
    """Choose what to prune; once a mode is chosen, list the volumes that would be removed."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    if not actor.is_administrator:
        raise PermissionDenied("administrator role required")
    plan = None
    fields: dict[str, str] = {}
    status_code = 200
    chosen = _prune_mode(mode or "anonymous")
    if mode and chosen is None:
        fields, status_code = {"mode": "Choose anonymous or all."}, 400
    if chosen is not None:
        try:
            plan = await volumes.queries.prune_plan(actor, environment_id, chosen)
        except UNAVAILABLE as unavailable:
            return unavailable_page(
                request,
                "Prune volumes",
                unavailable,
                _list_path(request, environment_id),
                "Back to volumes",
                region=None,
            )
    return page_response(
        request,
        "volumes/prune_confirm.html",
        {
            "environment": environment,
            "mode": chosen or "",
            "plan": plan,
            "fields": fields,
            "list_href": _list_path(request, environment_id),
            "prune_href": route(request, "environment_volume_prune", environment_id=environment_id),
            "run_href": route(
                request, "environment_volume_prune_run", environment_id=environment_id
            ),
        },
        status_code,
    )


def _prune_targets(values: Sequence[str]) -> list[VolumePruneTarget]:
    targets: list[VolumePruneTarget] = []
    for value in values:
        name, separator, created_at = value.partition(":")
        if not separator or VOLUME_OR_NETWORK_NAME.fullmatch(name) is None:
            raise ValidationFailed({"volumes": "The selection was incomplete. Review it again."})
        targets.append(VolumePruneTarget(name, created_at))
    return targets


@router.post("/-/prune", response_class=HTMLResponse, name="environment_volume_prune_run")
async def run_prune(
    request: Request,
    volumes: Volumes,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    mode: Annotated[str, Form(max_length=16)] = "",
    target: Annotated[list[Short] | None, Form(max_length=MAX_PRUNE)] = None,
) -> Response:
    """Remove exactly the volumes the confirmation listed that are still candidates."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    prune_href = route(request, "environment_volume_prune", environment_id=environment_id)
    chosen = _prune_mode(mode)
    try:
        if chosen is None:
            raise ValidationFailed({"mode": "Choose anonymous or all."})
        result = await volumes.actions.run_prune(
            actor, environment_id, chosen, _prune_targets(target or [])
        )
    except ACTION_ERRORS as error:
        return problem_page(
            request,
            heading="Prune volumes",
            failure=_failure_of(error, volumes, actor),
            back_href=_list_path(request, environment_id),
            back_label="Back to volumes",
            review_href=f"{prune_href}?{query_of('mode', chosen)}" if chosen else prune_href,
            environment_name=environment.name,
            region=None,
        )
    return show_result(
        request,
        "volumes/prune_result.html",
        {
            "environment": environment,
            "result": result,
            "list_href": _list_path(request, environment_id),
        },
    )


def _batch_targets(tokens: Sequence[str]) -> list[VolumeBatchTarget]:
    targets: list[VolumeBatchTarget] = []
    for token in tokens:
        name, separator, rest = token.partition(":")
        created_at, _, count = rest.rpartition(":")
        in_use = (
            int(count)
            if count.isascii() and count.isdigit() and len(count) <= COUNT_DIGITS
            else None
        )
        if not separator or VOLUME_OR_NETWORK_NAME.fullmatch(name) is None or in_use is None:
            raise ValidationFailed({"volumes": "The selection was incomplete. Review it again."})
        targets.append(VolumeBatchTarget(name, created_at, in_use))
    return targets


def _require_selection(actor: Principal, selection: Sequence[str] | None) -> None:
    """Answer a standard user's empty selection as an unknown path."""
    if not selection and not actor.is_administrator:
        raise ResourceNotFound("not found")


@router.get("/-/remove", response_class=HTMLResponse, name="environment_volume_batch_remove")
async def batch_remove_confirm(
    request: Request,
    volumes: Volumes,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    volume: Annotated[list[Short] | None, Query(max_length=BATCH_PARAMS_MAX)] = None,
) -> Response:
    """Name every volume of a batch removal before anything is removed; write holders only.

    Volumes the container dockmin runs in has mounted are listed as kept and are not removed.
    """
    environment = (await environments.environments.get(actor, environment_id)).summary
    _require_selection(actor, volume)
    list_href = _list_path(request, environment_id)
    fields: dict[str, str] = {}
    confirmation = None
    try:
        confirmation = await volumes.queries.batch_confirmation(actor, environment_id, volume or [])
    except ValidationFailed as invalid:
        fields = invalid.fields
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Remove volumes", unavailable, list_href, "Back to volumes", region=None
        )
    return page_response(
        request,
        "volumes/batch_confirm.html",
        {
            "environment": environment,
            "environment_id": environment_id,
            "confirmation": confirmation,
            "fields": fields,
            "max_batch": MAX_BATCH,
            "list_href": list_href,
            "run_href": route(
                request, "environment_volume_batch_remove_run", environment_id=environment_id
            ),
        },
        400 if fields else 200,
    )


@router.post("/-/remove", response_class=HTMLResponse, name="environment_volume_batch_remove_run")
async def run_batch_remove(
    request: Request,
    volumes: Volumes,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    target: Annotated[list[Short] | None, Form(max_length=BATCH_PARAMS_MAX)] = None,
) -> Response:
    """Remove the confirmed volumes, each on its own and without force, and report each result."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    _require_selection(actor, target)
    list_href = _list_path(request, environment_id)
    try:
        result = await volumes.actions.run_batch_remove(
            actor, environment_id, _batch_targets(target or [])
        )
    except ACTION_ERRORS as error:
        return problem_page(
            request,
            heading="Remove volumes",
            failure=_failure_of(error, volumes, actor),
            back_href=list_href,
            back_label="Back to volumes",
            environment_name=environment.name,
            region=None,
        )
    return show_result(
        request,
        "volumes/batch_result.html",
        {"environment": environment, "result": result, "list_href": list_href},
    )


def _require_administrator(actor: Principal) -> None:
    """Answer a standard user as an unknown path."""
    if not actor.is_administrator:
        raise ResourceNotFound("not found")


def _backfill_path(request: Request, environment_id: int) -> str:
    return route(request, "environment_volume_backfill", environment_id=environment_id)


@router.get("/-/backfill", response_class=HTMLResponse, name="environment_volume_backfill")
async def backfill_review(
    request: Request,
    backfill: VolumeBackfill,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Name every volume that would be assigned and its owner; administrators only."""
    _require_administrator(actor)
    environment = (await environments.environments.get(actor, environment_id)).summary
    list_href = _list_path(request, environment_id)
    try:
        plan = await backfill.preview(actor, environment_id)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Assign volume owners", unavailable, list_href, "Back to volumes", region=None
        )
    return page_response(
        request,
        "volumes/backfill.html",
        {
            "environment": environment,
            "plan": plan,
            "list_href": list_href,
            "run_href": route(
                request, "environment_volume_backfill_run", environment_id=environment_id
            ),
        },
    )


@router.post("/-/backfill", response_class=HTMLResponse, name="environment_volume_backfill_run")
async def run_backfill(
    request: Request,
    backfill: VolumeBackfill,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    expected_plan: Annotated[str, Form(max_length=DIGEST_MAX)] = "",
) -> Response:
    """Assign exactly the volumes the review named, each checked again as it is written."""
    _require_administrator(actor)
    environment = (await environments.environments.get(actor, environment_id)).summary
    list_href = _list_path(request, environment_id)
    try:
        result = await backfill.apply(actor, environment_id, expected_plan)
    except BackfillChanged as changed:
        failure = Failure(409, "The plan changed", "; ".join(changed.fields.values()))
    except UNAVAILABLE as unavailable:
        failure = Failure(503, *unreachable_copy(unavailable))
    else:
        return show_result(
            request,
            "volumes/backfill_result.html",
            {"environment": environment, "result": result, "list_href": list_href},
        )
    return problem_page(
        request,
        heading="Assign volume owners",
        failure=failure,
        back_href=list_href,
        back_label="Back to volumes",
        review_href=_backfill_path(request, environment_id),
        environment_name=environment.name,
        region=None,
    )


async def _detail_response(
    request: Request,
    volumes: VolumeServices,
    actor: Principal,
    environment_id: int,
    volume_name: str,
    tab: str,
    *,
    status_code: int = 200,
    values: dict[str, Any] | None = None,
    fields: dict[str, str] | None = None,
) -> Response:
    if tab not in TABS:
        raise ResourceNotFound("not found")
    try:
        detail = await volumes.queries.detail(actor, environment_id, volume_name)
        panel = None
        if tab == "access":
            if not detail.can_manage_access:
                raise PermissionDenied("administrator role required")
            panel = bind_digest(
                await volumes.queries.access_panel(actor, environment_id, volume_name), values
            )
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Volume",
            unavailable,
            _list_path(request, environment_id),
            "Back to volumes",
            region=TABS_REGION,
        )
    tabs = [entry for entry in TABS if detail.can_manage_access or entry != "access"]
    arguments: dict[str, Any] = {
        "detail": detail,
        "tab": tab,
        "tabs": tabs,
        "panel": panel,
        "values": values,
        "fields": fields or {},
    }
    if is_htmx_request(request):
        return fragment_response(request, DETAIL, "volume_tabs", status_code, **arguments)
    return page_response(
        request,
        "volumes/detail.html",
        {
            **arguments,
            "list_href": _list_path(request, environment_id),
        },
        status_code,
    )


@router.get(
    "/{volume_name}",
    response_class=HTMLResponse,
    name="environment_volume_detail",
    dependencies=[RequestOrigin],
)
async def volume_detail(
    volume_name: VolumeName,
    request: Request,
    volumes: Volumes,
    actor: CurrentUser,
    environment_id: PathId,
    tab: Annotated[str, Query(max_length=32)] = "overview",
) -> Response:
    """Show one volume, one tab at a time; each tab has its own URL."""
    return await _detail_response(request, volumes, actor, environment_id, volume_name, tab)


@router.get("/{volume_name}/remove", response_class=HTMLResponse, name="environment_volume_remove")
async def remove_confirm(
    volume_name: VolumeName,
    request: Request,
    volumes: Volumes,
    actor: CurrentUser,
    environment_id: PathId,
    force: Annotated[str, Query(max_length=8)] = "",
) -> Response:
    """Name the volume and the containers using it; write holders only.

    A volume in use gets a first page that only leads to a second, which carries the force.
    """
    detail_href = _detail_path(request, environment_id, volume_name)
    here = route(
        request, "environment_volume_remove", environment_id=environment_id, volume_name=volume_name
    )
    run_href = route(
        request,
        "environment_volume_remove_run",
        environment_id=environment_id,
        volume_name=volume_name,
    )
    forcing = force == FORCE_PAGE
    try:
        if forcing:
            force_impact = await volumes.queries.force_impact(actor, environment_id, volume_name)
            if not (force_impact.containers or force_impact.unwritable or force_impact.too_many):
                return redirect(request, here)
            return page_response(
                request,
                "volumes/remove_force_confirm.html",
                {
                    "impact": force_impact,
                    "detail_href": detail_href,
                    "review_href": f"{here}?force={FORCE_PAGE}",
                    "run_href": run_href,
                    "max_containers": MAX_FORCE_CONTAINERS,
                },
            )
        impact = await volumes.queries.remove_impact(actor, environment_id, volume_name)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Remove volume", unavailable, detail_href, "Back", region=None
        )
    return page_response(
        request,
        "volumes/remove_confirm.html",
        {
            "impact": impact,
            "detail_href": detail_href,
            "force_href": f"{here}?force={FORCE_PAGE}",
            "run_href": run_href,
        },
    )


def _expected_names(values: Sequence[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for value in values:
        container_id, separator, name = value.partition(":")
        if not separator or not is_canonical_id("container", container_id):
            raise ValidationFailed({"volume": "The confirmation was incomplete. Review it again."})
        names[container_id] = name
    return names


@router.post(
    "/{volume_name}/remove", response_class=HTMLResponse, name="environment_volume_remove_run"
)
async def run_remove(
    volume_name: VolumeName,
    request: Request,
    volumes: Volumes,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    expected_created_at: Field = "",
    expected_in_use: Annotated[str, Form(max_length=COUNT_DIGITS + 1)] = "",
    expected_containers: Field = "",
    force: Annotated[str, Form(max_length=8)] = "",
    container: Annotated[list[Short] | None, Form(max_length=MAX_FORCE_FORM)] = None,
) -> Response:
    """Remove the reviewed volume; with its containers only when the force page posted it."""
    detail_href = _detail_path(request, environment_id, volume_name)
    forcing = force == CONFIRM_YES
    review_href = route(
        request, "environment_volume_remove", environment_id=environment_id, volume_name=volume_name
    )
    if forcing:
        review_href = f"{review_href}?force={FORCE_PAGE}"
    try:
        if forcing:
            result = await volumes.actions.remove_with_containers(
                actor,
                environment_id,
                volume_name,
                expected_created_at=expected_created_at,
                expected_containers=expected_containers,
                expected_names=_expected_names(container or []),
            )
        else:
            in_use = (
                int(expected_in_use)
                if expected_in_use.isascii() and expected_in_use.isdigit()
                else None
            )
            if in_use is None:
                raise ValidationFailed(
                    {"volume": "The confirmation was incomplete. Review it again."}
                )
            await volumes.actions.remove(
                actor,
                environment_id,
                volume_name,
                expected_created_at=expected_created_at,
                expected_in_use=in_use,
            )
    except ACTION_ERRORS as error:
        return problem_page(
            request,
            heading="Remove volume",
            failure=_failure_of(error, volumes, actor),
            back_href=detail_href,
            back_label="Back to the volume",
            review_href=review_href,
            region=None,
        )
    if not forcing:
        return redirect(
            request, _list_path(request, environment_id), [Flash("ok", LIST_NOTICES["removed"])]
        )
    environment = (await environments.environments.get(actor, environment_id)).summary
    return show_result(
        request,
        "volumes/force_result.html",
        {
            "result": result,
            "environment_name": environment.name,
            "list_href": _list_path(request, environment_id),
        },
    )


@router.post(
    "/{volume_name}/access", response_class=HTMLResponse, name="environment_volume_access_save"
)
async def save_access(
    volume_name: VolumeName,
    request: Request,
    volumes: Volumes,
    access: Access,
    actor: CurrentUser,
    environment_id: PathId,
    data: AccessData,
) -> Response:
    """Create or replace the resource control over one volume; administrators only."""
    try:
        panel = await volumes.queries.access_panel(actor, environment_id, volume_name)
    except UNAVAILABLE as unavailable:
        return _access_unavailable(request, environment_id, unavailable)
    refusal = await apply_access(actor, access.controls, volumes.queries, panel, data)
    if refusal is not None:
        return await _detail_response(
            request,
            volumes,
            actor,
            environment_id,
            volume_name,
            "access",
            status_code=refusal.status_code,
            values=refusal.values,
            fields=refusal.fields,
        )
    return redirect(
        request,
        _access_path(request, environment_id, volume_name),
        [Flash("ok", ACCESS_SAVED)],
    )


@router.post(
    "/{volume_name}/access/delete",
    response_class=HTMLResponse,
    name="environment_volume_access_delete",
)
async def delete_access(
    volume_name: VolumeName,
    request: Request,
    volumes: Volumes,
    access: Access,
    actor: CurrentUser,
    environment_id: PathId,
    expected_digest: DigestField = "",
) -> Response:
    """Remove the resource control over one volume; administrators only."""
    try:
        panel = await volumes.queries.access_panel(actor, environment_id, volume_name)
    except UNAVAILABLE as unavailable:
        return _access_unavailable(request, environment_id, unavailable)
    refusal = await remove_access(actor, access.controls, panel, expected_digest)
    if refusal is not None:
        return await _detail_response(
            request,
            volumes,
            actor,
            environment_id,
            volume_name,
            "access",
            status_code=refusal.status_code,
            fields=refusal.fields,
        )
    return redirect(
        request,
        _access_path(request, environment_id, volume_name),
        [Flash("ok", ACCESS_REMOVED)],
    )


def _access_path(request: Request, environment_id: int, volume_name: str) -> str:
    return f"{_detail_path(request, environment_id, volume_name)}?{query_of('tab', 'access')}"


def _access_unavailable(request: Request, environment_id: int, unavailable: Exception) -> Response:
    return unavailable_page(
        request,
        "Volume",
        unavailable,
        _list_path(request, environment_id),
        "Back to volumes",
        region=None,
    )
