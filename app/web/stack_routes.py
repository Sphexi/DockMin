"""Stack pages: list, create, detail, edit, redeploy, remove, forget, adopt and the job page."""

import re
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.self_identity import source_of
from app.services.activity_log import ActivityLogService
from app.services.assembly import (
    Access,
    ActivityLog,
    Environments,
    Jobs,
    JobServices,
    Stacks,
    StackServices,
    StackWork,
    start_stack_job,
)
from app.services.compose_setup import ComposeStatus
from app.services.containers import ContainerListTooLarge
from app.services.host_objects import NetworkListTooLarge, VolumeListTooLarge
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectBusy
from app.services.stack_collisions import REASON_NAME, REASON_PORT
from app.services.stack_deploy import (
    ChangedSinceReview,
    ComposeNotReady,
    NotDeployed,
    PreexistingObjects,
    StackBusy,
    StackError,
    StackNameInUse,
    StackOperationFailed,
    StackRejected,
)
from app.services.stack_document import FILE_NAME, STACK_NAME
from app.services.stack_jobs import TooManyStackJobs
from app.services.stack_views import (
    RedeployConfirmation,
    StackPage,
    StackRow,
    ValidationReport,
    checked_name,
    unwritable_members,
)
from app.services.stacks import RefusableOperation
from app.services.template_prefill import stack_prefill
from app.web.activity_routes import (
    ACCESS_ACTIVITY,
    ALL_ACTIVITY,
    AUTOMATION_ACTIVITY,
    build_tab_activity_panel,
)
from app.web.flash import Flash, FlashLevel
from app.web.identifiers import PathId
from app.web.object_pages import (
    ACCESS_REMOVED,
    ACCESS_SAVED,
    CURSOR_MAX,
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
    problem_page,
    query_of,
    redirect,
    remove_access,
    route,
    self_href,
    unavailable_page,
)
from app.web.origins import RequestOrigin
from app.web.pagination import Column, PageView
from app.web.problems import unreachable_copy
from app.web.registry_providers import CATALOGUE
from app.web.stack_log_routes import LOGS_TAB, logs_status, logs_tab_context
from app.web.stack_pages import (
    CREDENTIAL_REASONS,
    DISMISSED_PAGE,
    DISMISSED_PARAM,
    FILE_CAP,
    FORM_FIELD,
    VARIABLE_CAP,
    FormState,
    OfferChoice,
    Problem,
    StackForm,
    failure_of,
    located,
    offers_for,
    offers_for_report,
    padded,
    padded_files,
    posted_stack_id,
    problem_state,
    pullable,
    read_stack_form,
    rejection_of,
    rows_from_stored,
    state_from,
)
from app.web.stack_read_pages import TOKEN, HostReads, prefilled_state
from app.web.template_forms import RequestTemplate, bound_entry, bound_ref, stack_panel
from app.web.templating import is_htmx_request

TABS_REGION: Final = Region("stack-tabs", "outerHTML")

router = APIRouter(
    prefix="/environments/{environment_id}/stacks",
    tags=["stacks"],
    dependencies=[Depends(require_user)],
)

SEGMENT_MAX: Final = 128
MAX_FLASHED: Final = 3
DEFAULT_PAGE_SIZE: Final = 25
CONFIRMED: Final = "yes"
VOLUMES_PAGE: Final = "1"
ORPHANS_PAGE: Final = "1"
TABS: Final = ("overview", "file", "variables", "files", "logs", "automation", "access")
TAB_ACTIVITY: Final = {
    "overview": ALL_ACTIVITY,
    "automation": AUTOMATION_ACTIVITY,
    "access": ACCESS_ACTIVITY,
}
CONFLICT_REASONS: Final = CREDENTIAL_REASONS | {
    "secrets_unreadable",
    "network_differs",
    "file_missing",
    "file_in_use",
    REASON_NAME,
    REASON_PORT,
}
OPERATION_CONFLICTS: Final = {
    "changed_since_review": "The stack changed",
    "preexisting_objects": "Objects with these names already exist",
}
OPERATION_UNAVAILABLE: Final = frozenset({"unreachable", "not_swarm_manager"})
MEMBERS_REFUSED: Final = (
    "Compose acts on every container of a stack, so a stack is changed only by someone who can "
    "change all of its containers. Ask an administrator."
)
LIST_NOTICES: Final = {"forgotten": "Stack forgotten. Its containers are still on the host."}
FILE_NOTICES: Final[dict[str, tuple[FlashLevel, str]]] = {
    "added": ("ok", "File added. Redeploy the stack to apply it."),
    "saved": ("ok", "File saved. Redeploy the stack to apply it."),
    "removed": ("ok", "File removed. Redeploy the stack to apply the change."),
}
DETAIL_NOTICES: Final[dict[str, tuple[FlashLevel, str]]] = {
    "saved": ("ok", "Changes saved. Redeploy the stack to apply them."),
    "unchanged": ("ok", "Nothing changed."),
    "adopted": ("ok", "Stack adopted. Nothing was changed on the host."),
}

LIST_TABLES: Final = "stacks/_tables.html"
DETAIL: Final = "stacks/_detail.html"
VALIDATION: Final = "stacks/_validation.html"

JOB_ID: Final = re.compile(r"[A-Za-z0-9_-]{22}")
FILES_TAB: Final = "files"

type FormKind = Literal["new", "edit", "adopt"]


def _canonical_stack_name(stack_name: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    """Refuse a malformed name as a missing one, before any service call and for every role."""
    if STACK_NAME.fullmatch(stack_name) is None:
        raise ResourceNotFound("not found")
    return stack_name


def _canonical_project(project: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    if STACK_NAME.fullmatch(project) is None:
        raise ResourceNotFound("not found")
    return project


def _canonical_job_id(job_id: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    if JOB_ID.fullmatch(job_id) is None:
        raise ResourceNotFound("not found")
    return job_id


def _canonical_file(file: Annotated[str, Path()]) -> str:
    """Refuse a file name outside the grammar as a missing one, before any read."""
    if FILE_NAME.fullmatch(file) is None:
        raise ResourceNotFound("not found")
    return file


StackName = Annotated[str, Depends(_canonical_stack_name)]
FileName = Annotated[str, Depends(_canonical_file)]
ProjectName = Annotated[str, Depends(_canonical_project)]
JobId = Annotated[str, Depends(_canonical_job_id)]

STACK_ERRORS: Final = (
    StackRejected,
    StackError,
    ObjectBusy,
    EnvironmentUnavailable,
    ContainerListTooLarge,
    NetworkListTooLarge,
    VolumeListTooLarge,
    ObjectTooLarge,
    TooManyStackJobs,
    ValidationFailed,
)
UNAVAILABLE: Final = (
    EnvironmentUnavailable,
    ContainerListTooLarge,
    NetworkListTooLarge,
    VolumeListTooLarge,
    ObjectTooLarge,
)


@dataclass(frozen=True, slots=True)
class ComposeNotice:
    """Why Compose cannot run here, for the banner."""

    message: str
    installed: str | None
    required: str | None


def _operation_problem(error: StackOperationFailed) -> Problem:
    reason = error.reason
    if reason in OPERATION_CONFLICTS:
        return Problem(409, failure=Failure(409, OPERATION_CONFLICTS[reason], str(error)))
    if reason == "outcome_unknown":
        return Problem(503, failure=Failure(503, "The result is not known", str(error)))
    if reason in OPERATION_UNAVAILABLE:
        return Problem(503, failure=Failure(503, *unreachable_copy(error)))
    if reason == "restricted":
        return Problem(403, failure=Failure(403, "Your role cannot deploy this", str(error)))
    return Problem(502, failure=Failure(502, "Docker or Compose failed", str(error)))


def _rejection_status(reason: str) -> int:
    if reason in CONFLICT_REASONS:
        return 409
    return 403 if reason == "restricted" else 400


def problem_of(error: Exception, stacks: StackServices, actor: Principal) -> Problem:
    """Map an error a stack action can raise to the answer the caller is shown."""
    if isinstance(error, StackRejected):
        status = _rejection_status(error.reason)
        inline = {key: msg for key, msg in error.fields.items() if FORM_FIELD.fullmatch(key)}
        needed = error.fields if error.reason in CREDENTIAL_REASONS else {}
        return Problem(
            status, inline, rejection_of(error), reference_errors=needed, lines=error.lines
        )
    if isinstance(error, StackNameInUse):
        return Problem(409, {"name": str(error)})
    if isinstance(error, ObjectBusy):
        busy = stacks.queries.describe_busy(error, actor)
        return Problem(
            409, failure=Failure(409, "This stack is busy", busy_message(busy, "the stack"))
        )
    if isinstance(error, PreexistingObjects):
        return Problem(
            409, failure=Failure(409, "Objects with these names already exist", str(error))
        )
    if isinstance(error, ChangedSinceReview):
        return Problem(409, failure=Failure(409, "The stack changed", str(error)))
    if isinstance(error, NotDeployed):
        return Problem(409, failure=Failure(409, "The stack is not deployed", str(error)))
    if isinstance(error, ComposeNotReady):
        return Problem(503, failure=Failure(503, "Docker Compose is not ready", str(error)))
    if isinstance(error, StackBusy):
        return Problem(503, failure=Failure(503, "The server is busy", str(error)))
    if isinstance(error, StackOperationFailed):
        return _operation_problem(error)
    if isinstance(error, TooManyStackJobs):
        return Problem(429, failure=Failure(429, "Too many operations are running", str(error)))
    if isinstance(error, ValidationFailed):
        return Problem(400, failure=Failure(400, "Check the request", str(error)))
    return Problem(503, failure=Failure(503, *unreachable_copy(error)))


def compose_notice(status: ComposeStatus) -> ComposeNotice | None:
    """Return why Compose cannot run on this server, or None when it can."""
    if status.ready:
        return None
    message = status.message or "Docker Compose is not available on this server."
    return ComposeNotice(message, status.installed, status.required)


def _compose(stacks: StackServices) -> ComposeNotice | None:
    return compose_notice(stacks.queries.compose_status())


async def _refused(
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    stack_name: str,
    operation: RefusableOperation,
    error: Exception,
) -> Problem:
    """Record a refusal given before the operation started, as the operation does; answer it."""
    try:
        await stacks.actions.record_refusal(
            actor, environment_id, stack_name, operation=operation, error=error
        )
    except STACK_ERRORS as recorded:
        return problem_of(recorded, stacks, actor)
    raise RuntimeError("a recorded refusal raises")


async def _busy_refusal(
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    stack_name: str,
    operation: RefusableOperation,
) -> Failure | None:
    """Record and return the notice for a stack another action holds, or None when it is free."""
    busy = await stacks.queries.busy_error(actor, environment_id, stack_name)
    if busy is None:
        return None
    return failure_of(await _refused(stacks, actor, environment_id, stack_name, operation, busy))


def _list_path(request: Request, environment_id: int) -> str:
    return route(request, "environment_stack_list", environment_id=environment_id)


def _detail_path(request: Request, environment_id: int, stack_name: str) -> str:
    return route(
        request, "environment_stack_detail", environment_id=environment_id, stack_name=stack_name
    )


def _stack_route(request: Request, name: str, environment_id: int, stack_name: str) -> str:
    return route(request, name, environment_id=environment_id, stack_name=stack_name)


def _job_path(request: Request, environment_id: int, job_id: str) -> str:
    return route(request, "environment_stack_job", environment_id=environment_id, job_id=job_id)


COLUMNS: Final = (
    Column("name", "Stack"),
    Column("status", "Status", width="badge"),
    Column("last", "Last operation", optional=True, width="time"),
    Column("origin", "Origin", optional=True, width="short"),
    Column("actions", "Actions", width="actions"),
)


async def list_response(
    request: Request,
    stacks: StackServices,
    environments: Environments,
    actor: Principal,
    environment_id: int,
    *,
    needle: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
    show_dismissed: bool = False,
    refusal: Failure | None = None,
) -> Response:
    """Render the stacks list, with `refusal` shown above it when an action was turned down."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    rows: tuple[StackRow, ...] = ()
    page: StackPage | None = None
    errors: tuple[str, ...] = ()
    failure: str | None = None
    status_code = refusal.status if refusal else 200
    try:
        page = await stacks.queries.page(
            actor,
            environment_id,
            limit=limit,
            cursor=cursor,
            q=needle,
            show_dismissed=show_dismissed,
        )
    except ValidationFailed as invalid:
        errors, status_code = tuple(invalid.fields.values()), 400
    except UNAVAILABLE as unavailable:
        failure, status_code = str(unavailable), 503
    else:
        rows = page.items
    params = [("q", needle)] if needle else []
    if show_dismissed:
        params.append((DISMISSED_PARAM, DISMISSED_PAGE))
    pager = PageView(
        base_path=_list_path(request, environment_id),
        limit=limit,
        item_count=len(rows),
        cursor=None if errors or failure else cursor,
        next_cursor=page.next_cursor if page else None,
        params=tuple(params),
    )
    results: dict[str, Any] = {
        "rows": rows,
        "columns": COLUMNS,
        "pager": pager,
        "filtered": bool(needle),
        "errors": errors,
        "failure": failure,
        "retry_href": self_href(request),
        "environment_id": environment_id,
        "visible_count": page.visible_count if page else None,
        "unmanaged": page.unmanaged if page else (),
        "unmanaged_unavailable": page.unmanaged_unavailable if page else False,
        "is_administrator": actor.is_administrator,
    }
    if is_htmx_request(request):
        return fragment_response(
            request,
            LIST_TABLES,
            "stacks_results",
            status_code,
            oob=True,
            dismissed_hidden=page.dismissed_hidden if page else 0,
            show_dismissed=show_dismissed,
            **results,
        )
    return page_response(
        request,
        "stacks/list.html",
        {
            **results,
            "refusal": refusal,
            "dismissed_hidden": page.dismissed_hidden if page else 0,
            "show_dismissed": show_dismissed,
            "environment": environment,
            "q": needle or "",
            "can_create": page.can_create if page else False,
            "compose": compose_notice(page.compose) if page else _compose(stacks),
        },
        status_code,
    )


@router.get("", response_class=HTMLResponse, name="environment_stack_list")
async def list_stacks(
    request: Request,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    q: Filter = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=CURSOR_MAX)] = None,
    dismissed: Annotated[str, Query(max_length=8)] = "",
) -> Response:
    """List the stacks the caller may see, and for administrators the unmanaged projects."""
    return await list_response(
        request,
        stacks,
        environments,
        actor,
        environment_id,
        needle=blank_to_none(q),
        limit=limit,
        cursor=cursor,
        show_dismissed=dismissed == DISMISSED_PAGE,
    )


def _form_context(
    request: Request,
    kind: FormKind,
    environment_id: int,
    state: FormState,
    *,
    stack_name: str = "",
    project: str = "",
) -> dict[str, Any]:
    """Return the links and labels of a form page; the caller adds the environment."""
    list_href = _list_path(request, environment_id)
    if kind == "new":
        links = {
            "post_href": route(request, "environment_stack_create", environment_id=environment_id),
            "validate_href": route(
                request, "environment_stack_validate_new", environment_id=environment_id
            ),
            "cancel_href": list_href,
        }
    elif kind == "edit":
        links = {
            "post_href": _stack_route(
                request, "environment_stack_save", environment_id, stack_name
            ),
            "validate_href": _stack_route(
                request, "environment_stack_validate", environment_id, stack_name
            ),
            "cancel_href": _detail_path(request, environment_id, stack_name),
        }
    else:
        links = {
            "post_href": route(
                request,
                "environment_stack_adopt_run",
                environment_id=environment_id,
                project=project,
            ),
            "validate_href": route(
                request,
                "environment_stack_validate_adopt",
                environment_id=environment_id,
                project=project,
            ),
            "cancel_href": list_href,
        }
    return {
        "kind": kind,
        "form": state,
        "variable_cap": VARIABLE_CAP,
        "file_cap": FILE_CAP,
        "stack_name": stack_name or project,
        "catalogue": CATALOGUE,
        **links,
    }


async def _new_page(
    request: Request,
    stacks: StackServices,
    environments: Environments,
    actor: Principal,
    environment_id: int,
    state: FormState,
    status_code: int = 200,
) -> Response:
    environment = (await environments.environments.get(actor, environment_id)).summary
    return page_response(
        request,
        "stacks/new.html",
        {
            **_form_context(request, "new", environment_id, state),
            "environment": environment,
            "compose": _compose(stacks),
            "is_administrator": actor.is_administrator,
            "template_panel": stack_panel(request, is_administrator=actor.is_administrator),
            "template_ref": bound_ref(request),
        },
        status_code,
    )


@router.get(
    "/-/new",
    response_class=HTMLResponse,
    name="environment_stack_new",
    dependencies=[RequestTemplate],
)
async def new_stack(
    request: Request,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show the create form, blank or filled from a template; create access is needed."""
    await stacks.queries.create_context(actor, environment_id)
    state = FormState(rows=padded([]), file_rows=padded_files([]))
    entry = bound_entry(request)
    if entry is not None:
        prefill = stack_prefill(entry.template, is_administrator=actor.is_administrator)
        state = FormState(
            name=prefill.name,
            compose_text=prefill.compose_text,
            rows=padded([]),
            file_rows=padded_files([]),
        )
    return await _new_page(request, stacks, environments, actor, environment_id, state)


async def _check(
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    form: StackForm,
    *,
    name: str,
    existing: bool = False,
    adopt: bool = False,
    deploy: bool = False,
) -> tuple[ValidationReport | None, Problem | None]:
    """Validate the posted file, variables and companion files; return the report or the problem."""
    try:
        report = await stacks.queries.validate(
            actor,
            environment_id,
            name=name,
            compose_text=form.text(),
            variables=form.variable_inputs(),
            existing=existing,
            adopt=adopt,
            deploy=deploy,
            choices=form.choices() if deploy else None,
            check_collisions=True,
            files=None if existing else form.file_inputs(),
        )
    except STACK_ERRORS as error:
        return None, await located(problem_of(error, stacks, actor), form.display_text())
    return report, None


def _validation_fragment(
    request: Request,
    report: ValidationReport | None,
    problem: Problem | None,
    offers: tuple[OfferChoice, ...] = (),
) -> Response:
    return fragment_response(
        request,
        VALIDATION,
        "validation_result",
        problem.status if problem else 200,
        report=report,
        errors=problem.errors if problem else {},
        lines=problem.lines if problem else {},
        rejection=problem.rejection if problem else None,
        failure=problem.failure if problem else None,
        offers=offers,
        catalogue=CATALOGUE,
    )


@router.post(
    "/-/validate",
    response_class=HTMLResponse,
    name="environment_stack_validate_new",
    dependencies=[RequestTemplate],
)
async def validate_new(
    request: Request,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Check a new stack's file and variables; nothing is stored or run."""
    await stacks.queries.create_context(actor, environment_id)
    form = await read_stack_form(request)
    report, problem = await _check(stacks, actor, environment_id, form, name=form.get("name"))
    offers = await offers_for_report(stacks, actor, environment_id, form, report)
    if is_htmx_request(request):
        return _validation_fragment(request, report, problem, offers)
    return await _new_page(
        request,
        stacks,
        environments,
        actor,
        environment_id,
        state_from(form, problem=problem, report=report, offers=offers),
        problem.status if problem else 200,
    )


def _deploy_job(
    request: Request,
    actor: Principal,
    environment_id: int,
    stack_name: str,
    kind: Literal["create", "adopt"],
    work: StackWork,
    *,
    heading: str,
    flashes: Sequence[Flash] = (),
) -> Response:
    """Start the deployment of a stack that now exists, and send the caller to its job page."""
    try:
        job_id = start_stack_job(request.app, actor, environment_id, kind, stack_name, work)
    except TooManyStackJobs as busy:
        return problem_page(
            request,
            heading=heading,
            failure=Failure(
                429,
                "Too many operations are running",
                f"{busy} The stack exists but was not deployed; redeploy it from its page.",
            ),
            back_href=_detail_path(request, environment_id, stack_name),
            back_label="Open the stack",
            region=None,
        )
    return redirect(request, _job_path(request, environment_id, job_id), flashes)


@router.post(
    "/-/new",
    response_class=HTMLResponse,
    name="environment_stack_create",
    dependencies=[RequestTemplate],
)
async def create_stack(
    request: Request,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Check the stack again, create it and start its first deployment as a job."""
    await stacks.queries.create_context(actor, environment_id)
    form = await read_stack_form(request)
    if form.adding_rows():
        return await _new_page(
            request, stacks, environments, actor, environment_id, state_from(form)
        )
    try:
        name = checked_name(form.get("name"))
        text = form.text()
        variables = form.variable_inputs()
        files = form.file_inputs()
        choices = form.choices()
        with stacks.jobs.reserve(actor):
            created = await stacks.actions.create(
                actor,
                environment_id,
                name=name,
                compose_text=text,
                variables=variables,
                choices=choices,
                resolve_pulls=True,
                check_collisions=True,
                files=files,
                template=bound_ref(request),
            )
    except STACK_ERRORS as error:
        problem = problem_of(error, stacks, actor)
    else:
        return _deploy_job(
            request,
            actor,
            environment_id,
            name,
            "create",
            lambda services, progress: services.actions.deploy(
                actor,
                environment_id,
                name,
                choices=choices,
                progress=progress,
                check_collisions=True,
            ),
            heading="Create stack",
            flashes=[Flash("warn", warning) for warning in created.warnings[:MAX_FLASHED]],
        )
    state = await problem_state(stacks, actor, environment_id, form, problem)
    return await _new_page(
        request, stacks, environments, actor, environment_id, state, problem.status
    )


async def _detail_response(
    request: Request,
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    stack_name: str,
    tab: str,
    *,
    status_code: int = 200,
    values: dict[str, Any] | None = None,
    fields: dict[str, str] | None = None,
    jobs: JobServices | None = None,
    activity: ActivityLogService | None = None,
) -> Response:
    if tab not in TABS:
        raise ResourceNotFound("not found")
    try:
        detail = await stacks.queries.detail(
            actor, environment_id, stack_name, open_masks=tab == "variables"
        )
        panel = None
        logs: dict[str, Any] | None = None
        if tab == "access":
            if not detail.can_manage:
                raise PermissionDenied("administrator role required")
            panel = await stacks.queries.access_panel(actor, environment_id, stack_name)
        elif tab == LOGS_TAB:
            logs = await logs_tab_context(
                request,
                stacks.logs,
                "compose",
                actor,
                environment_id,
                detail.row.name,
                _detail_path(request, environment_id, detail.row.name),
            )
            status_code = logs_status(logs, status_code)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Stack",
            unavailable,
            _list_path(request, environment_id),
            "Back to stacks",
            region=TABS_REGION,
        )
    visible = [entry for entry in TABS if detail.can_manage or entry != "access"]
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
        "automation": (
            await jobs.rules.for_object(
                actor, environment_id, name=detail.row.name, labels={}, stack=None, kind="stack"
            )
            if jobs is not None and (tab == "automation" or not is_htmx_request(request))
            else None
        ),
        "activity_log": await build_tab_activity_panel(
            request,
            activity,
            actor,
            "stack",
            environment_id,
            detail.row.name,
            TAB_ACTIVITY.get(tab),
        ),
    }
    if is_htmx_request(request):
        return fragment_response(
            request,
            DETAIL,
            "stack_tabs",
            status_code,
            **arguments,
            **additions,
        )
    return page_response(
        request,
        "stacks/detail.html",
        {
            **arguments,
            **additions,
            "list_href": _list_path(request, environment_id),
            "busy_text": busy_message(detail.busy, "this stack") if detail.busy else None,
            "compose": compose_notice(detail.compose),
            "own_stack": source_of(request.app)().protects_stack(environment_id, detail.row.name),
        },
        status_code,
    )


@router.get(
    "/{stack_name}",
    response_class=HTMLResponse,
    name="environment_stack_detail",
    dependencies=[RequestOrigin],
)
async def stack_detail(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    jobs: Jobs,
    activity: ActivityLog,
    actor: CurrentUser,
    environment_id: PathId,
    tab: Annotated[str, Query(max_length=32)] = "overview",
) -> Response:
    """Show one stack, one tab at a time; each tab has its own URL."""
    return await _detail_response(
        request,
        stacks,
        actor,
        environment_id,
        stack_name,
        tab,
        jobs=jobs,
        activity=activity,
    )


async def _edit_page(
    request: Request,
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    stack_name: str,
    environment_name: str,
    state: FormState,
    status_code: int = 200,
    *,
    file_digest: str = "",
) -> Response:
    return page_response(
        request,
        "stacks/edit.html",
        {
            **_form_context(request, "edit", environment_id, state, stack_name=stack_name),
            "environment_name": environment_name,
            "expected_file_digest": file_digest,
            "compose": _compose(stacks),
        },
        status_code,
    )


@router.get("/{stack_name}/edit", response_class=HTMLResponse, name="environment_stack_edit")
async def edit_form(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show the file and variables of a stack for editing; write access is needed."""
    edit = await stacks.queries.edit_context(actor, environment_id, stack_name)
    state = FormState(
        name=edit.row.name,
        compose_text=edit.compose_file,
        rows=padded(rows_from_stored(edit.variables)),
    )
    return await _edit_page(
        request,
        stacks,
        actor,
        environment_id,
        stack_name,
        edit.environment_name,
        state,
        file_digest=edit.compose_file_digest,
    )


@router.post(
    "/{stack_name}/validate", response_class=HTMLResponse, name="environment_stack_validate"
)
async def validate_existing(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Check a changed file and variable set of an existing stack; nothing is stored or run."""
    edit = await stacks.queries.edit_context(actor, environment_id, stack_name)
    form = await read_stack_form(request)
    report, problem = await _check(
        stacks, actor, environment_id, form, name=stack_name, existing=True
    )
    offers = await offers_for_report(stacks, actor, environment_id, form, report)
    if is_htmx_request(request):
        return _validation_fragment(request, report, problem, offers)
    state = state_from(
        form,
        problem=problem,
        report=report,
        offers=offers,
        name=stack_name,
        stored_rows=rows_from_stored(edit.variables),
    )
    return await _edit_page(
        request,
        stacks,
        actor,
        environment_id,
        stack_name,
        edit.environment_name,
        state,
        problem.status if problem else 200,
        file_digest=form.get("expected_file_digest"),
    )


@router.post("/{stack_name}/edit", response_class=HTMLResponse, name="environment_stack_save")
async def save_stack(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Save the file and variables without deploying; optionally go on to the redeploy review."""
    edit = await stacks.queries.edit_context(actor, environment_id, stack_name)
    form = await read_stack_form(request)
    stored = rows_from_stored(edit.variables)
    posted_digest = form.get("expected_file_digest")
    if form.adding_rows():
        return await _edit_page(
            request,
            stacks,
            actor,
            environment_id,
            stack_name,
            edit.environment_name,
            state_from(form, name=stack_name, stored_rows=stored),
            file_digest=posted_digest,
        )
    try:
        updated = await stacks.actions.update(
            actor,
            environment_id,
            stack_name,
            expected_file_digest=posted_digest,
            compose_text=form.text(),
            variables=form.variable_inputs(),
        )
    except STACK_ERRORS as error:
        problem = await located(problem_of(error, stacks, actor), form.display_text())
        return await _edit_page(
            request,
            stacks,
            actor,
            environment_id,
            stack_name,
            edit.environment_name,
            state_from(form, problem=problem, name=stack_name, stored_rows=stored),
            problem.status,
            file_digest=posted_digest,
        )
    if form.get("next") == "redeploy":
        return redirect(
            request, _stack_route(request, "environment_stack_redeploy", environment_id, stack_name)
        )
    level, text = DETAIL_NOTICES["saved" if updated.changed else "unchanged"]
    return redirect(
        request, _detail_path(request, environment_id, stack_name), [Flash(level, text)]
    )


def _redeploy_page(
    request: Request,
    environment_id: int,
    stack_name: str,
    confirmation: RedeployConfirmation,
    *,
    form: StackForm | None = None,
    problem: Problem | None = None,
    offers: tuple[OfferChoice, ...] = (),
    status_code: int = 200,
    with_orphans: bool = False,
) -> Response:
    here = _stack_route(request, "environment_stack_redeploy", environment_id, stack_name)
    return page_response(
        request,
        "stacks/redeploy_orphans_confirm.html" if with_orphans else "stacks/redeploy_confirm.html",
        {
            "confirmation": confirmation,
            "problem": problem,
            "offers": offers,
            "pull": form.flag("pull") if form else False,
            "rebuild": form.flag("rebuild") if form else False,
            "remove_orphans": with_orphans,
            "with_orphans": with_orphans,
            "orphans_href": f"{here}?{query_of('orphans', ORPHANS_PAGE)}",
            "plain_href": here,
            "catalogue": CATALOGUE,
            "compose": compose_notice(confirmation.compose),
            "detail_href": _detail_path(request, environment_id, stack_name),
            "edit_href": _stack_route(
                request, "environment_stack_edit", environment_id, stack_name
            ),
            "run_href": _stack_route(
                request, "environment_stack_redeploy_run", environment_id, stack_name
            ),
        },
        status_code,
    )


async def _redeploy_offers(
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    stack_name: str,
    confirmation: RedeployConfirmation,
) -> tuple[OfferChoice, ...]:
    """Build the credential pickers for the images the stored file names outright and the bases."""
    declared = await stacks.queries.declared_images(actor, environment_id, stack_name)
    bases = [base for build in confirmation.builds if not build.reuse for base in build.bases]
    offers, _ = await offers_for(
        stacks,
        actor,
        environment_id,
        [reference for reference in (*declared, *bases) if pullable(reference)],
        {},
        {},
        include_posted=False,
    )
    return offers


@router.get(
    "/{stack_name}/redeploy", response_class=HTMLResponse, name="environment_stack_redeploy"
)
async def redeploy_confirm(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
    orphans: Annotated[str, Query(max_length=8)] = "",
) -> Response:
    """Name the containers a redeploy may recreate.

    The orphans page is the separate confirmation that also removes the containers no service in
    the file defines.
    """
    try:
        confirmation = await stacks.queries.redeploy_confirmation(
            actor, environment_id, stack_name, check_collisions=True
        )
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Redeploy stack",
            unavailable,
            _detail_path(request, environment_id, stack_name),
            "Back to the stack",
            region=None,
        )
    return _redeploy_page(
        request,
        environment_id,
        stack_name,
        confirmation,
        offers=await _redeploy_offers(stacks, actor, environment_id, stack_name, confirmation),
        with_orphans=orphans == ORPHANS_PAGE,
    )


@router.post(
    "/{stack_name}/redeploy", response_class=HTMLResponse, name="environment_stack_redeploy_run"
)
async def redeploy_run(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Redeploy the reviewed stack as a job."""
    form = await read_stack_form(request)
    detail_href = _detail_path(request, environment_id, stack_name)
    pull, orphans = form.flag("pull"), form.flag("remove_orphans")
    rebuild = form.flag("rebuild")
    if orphans and form.get("confirm_force") != CONFIRMED:
        return problem_page(
            request,
            heading="Redeploy stack",
            failure=Failure(400, "", ""),
            back_href=detail_href,
            back_label="Back to the stack",
            region=None,
        )
    review = _stack_route(request, "environment_stack_redeploy", environment_id, stack_name)
    if orphans:
        review = f"{review}?{query_of('orphans', ORPHANS_PAGE)}"
    try:
        confirmation = await stacks.queries.redeploy_confirmation(actor, environment_id, stack_name)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Redeploy stack", unavailable, detail_href, "Back to the stack", region=None
        )
    expected_file = form.get("expected_file_digest")
    expected_inputs = form.get("expected_inputs_digest")
    expected_containers = form.get("expected_containers")
    expected_builds = form.get("expected_builds")
    if (
        expected_file != confirmation.expected_file_digest
        or expected_inputs != confirmation.expected_inputs_digest
        or expected_containers != confirmation.expected_containers
        or expected_builds != confirmation.expected_builds
    ):
        stale = await _refused(
            stacks, actor, environment_id, stack_name, "redeploy", ChangedSinceReview()
        )
        return problem_page(
            request,
            heading="Redeploy stack",
            failure=failure_of(stale),
            back_href=detail_href,
            back_label="Back to the stack",
            review_href=review,
            environment_name=confirmation.environment_name,
            region=None,
        )
    if confirmation.unwritable_containers:
        await _refused(stacks, actor, environment_id, stack_name, "redeploy", unwritable_members())
        return problem_page(
            request,
            heading="Redeploy stack",
            failure=Failure(403, "You cannot change every container", MEMBERS_REFUSED),
            back_href=detail_href,
            back_label="Back to the stack",
            environment_name=confirmation.environment_name,
            region=None,
        )
    busy = await _busy_refusal(stacks, actor, environment_id, stack_name, "redeploy")
    if busy is not None:
        return problem_page(
            request,
            heading="Redeploy stack",
            failure=busy,
            back_href=detail_href,
            back_label="Back to the stack",
            review_href=review,
            environment_name=confirmation.environment_name,
            region=None,
        )
    edit = await stacks.queries.edit_context(actor, environment_id, stack_name)
    try:
        choices = form.choices()
    except ValidationFailed as invalid:
        problem = problem_of(invalid, stacks, actor)
    else:
        try:
            await stacks.queries.validate(
                actor,
                environment_id,
                name=stack_name,
                compose_text=edit.compose_file,
                variables=None,
                existing=True,
                deploy=True,
                pull=pull,
                choices=choices,
                check_collisions=True,
                rebuild=rebuild,
            )
        except STACK_ERRORS as error:
            problem = await _refused(stacks, actor, environment_id, stack_name, "redeploy", error)
        else:
            try:
                job_id = start_stack_job(
                    request.app,
                    actor,
                    environment_id,
                    "redeploy",
                    stack_name,
                    lambda services, progress: services.actions.redeploy(
                        actor,
                        environment_id,
                        stack_name,
                        expected_file_digest=expected_file,
                        expected_inputs_digest=expected_inputs,
                        expected_containers=expected_containers,
                        pull=pull,
                        remove_orphans=orphans,
                        choices=choices,
                        progress=progress,
                        check_collisions=True,
                        rebuild=rebuild,
                        expected_builds=expected_builds,
                    ),
                )
            except TooManyStackJobs as busy_jobs:
                problem = problem_of(busy_jobs, stacks, actor)
            else:
                return redirect(request, _job_path(request, environment_id, job_id))
    offers, _ = await offers_for(
        stacks,
        actor,
        environment_id,
        problem.references,
        form.posted_choices(),
        problem.reference_errors,
    )
    return _redeploy_page(
        request,
        environment_id,
        stack_name,
        confirmation,
        form=form,
        problem=problem,
        offers=offers,
        status_code=problem.status,
        with_orphans=orphans,
    )


async def _start_lifecycle(
    request: Request,
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    stack_name: str,
    kind: Literal["start", "stop"],
) -> Response:
    await stacks.queries.require_stack(actor, environment_id, stack_name, "write")
    detail_href = _detail_path(request, environment_id, stack_name)
    busy = await _busy_refusal(stacks, actor, environment_id, stack_name, kind)
    if busy is not None:
        return problem_page(
            request,
            heading=f"{kind.capitalize()} stack",
            failure=busy,
            back_href=detail_href,
            back_label="Back to the stack",
            review_href=detail_href,
            region=None,
        )
    try:
        if kind == "start":
            job_id = start_stack_job(
                request.app,
                actor,
                environment_id,
                kind,
                stack_name,
                lambda services, progress: services.actions.start(
                    actor, environment_id, stack_name, progress=progress
                ),
            )
        else:
            job_id = start_stack_job(
                request.app,
                actor,
                environment_id,
                kind,
                stack_name,
                lambda services, progress: services.actions.stop(
                    actor, environment_id, stack_name, progress=progress
                ),
            )
    except TooManyStackJobs as busy:
        return problem_page(
            request,
            heading=f"{kind.capitalize()} stack",
            failure=Failure(429, "Too many operations are running", str(busy)),
            back_href=_detail_path(request, environment_id, stack_name),
            back_label="Back to the stack",
            region=None,
        )
    return redirect(request, _job_path(request, environment_id, job_id))


@router.post("/{stack_name}/start", response_class=HTMLResponse, name="environment_stack_start")
async def start_stack(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Start the stack's containers as a job."""
    return await _start_lifecycle(request, stacks, actor, environment_id, stack_name, "start")


@router.post("/{stack_name}/stop", response_class=HTMLResponse, name="environment_stack_stop")
async def stop_stack(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Stop the stack's containers as a job."""
    return await _start_lifecycle(request, stacks, actor, environment_id, stack_name, "stop")


@router.get("/{stack_name}/remove", response_class=HTMLResponse, name="environment_stack_remove")
async def remove_confirm(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
    volumes: Annotated[str, Query(max_length=8)] = "",
    orphans: Annotated[str, Query(max_length=8)] = "",
) -> Response:
    """Name what a remove takes away.

    The volumes page lists each volume and what happens to it. The orphans page is the separate
    confirmation that also removes the containers no service in the file defines.
    """
    with_volumes = volumes == VOLUMES_PAGE
    with_orphans = orphans == ORPHANS_PAGE
    detail_href = _detail_path(request, environment_id, stack_name)
    try:
        confirmation = await stacks.queries.remove_confirmation(
            actor, environment_id, stack_name, volumes=with_volumes
        )
    except STACK_ERRORS as error:
        return problem_page(
            request,
            heading="Remove stack",
            failure=failure_of(problem_of(error, stacks, actor)),
            back_href=detail_href,
            back_label="Back to the stack",
            region=None,
        )
    here = _stack_route(request, "environment_stack_remove", environment_id, stack_name)
    if with_orphans:
        template = "stacks/remove_orphans_confirm.html"
    elif with_volumes:
        template = "stacks/remove_volumes_confirm.html"
    else:
        template = "stacks/remove_confirm.html"
    return page_response(
        request,
        template,
        {
            "confirmation": confirmation,
            "compose": compose_notice(confirmation.compose),
            "detail_href": detail_href,
            "with_volumes": with_volumes,
            "with_orphans": with_orphans,
            "volumes_href": f"{here}?{query_of('volumes', VOLUMES_PAGE)}",
            "orphans_href": f"{here}?{_remove_query(with_volumes, True)}",
            "plain_href": here,
            "run_href": _stack_route(
                request, "environment_stack_remove_run", environment_id, stack_name
            ),
        },
    )


def _remove_query(with_volumes: bool, with_orphans: bool) -> str:
    pairs = []
    if with_volumes:
        pairs.append(("volumes", VOLUMES_PAGE))
    if with_orphans:
        pairs.append(("orphans", ORPHANS_PAGE))
    return urlencode(pairs)


@router.post(
    "/{stack_name}/remove", response_class=HTMLResponse, name="environment_stack_remove_run"
)
async def remove_run(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Remove the reviewed stack as a job."""
    form = await read_stack_form(request)
    with_volumes = form.get("remove_volumes") == CONFIRMED
    detail_href = _detail_path(request, environment_id, stack_name)
    orphans = form.flag("remove_orphans")
    here = _stack_route(request, "environment_stack_remove", environment_id, stack_name)
    review = f"{here}?{_remove_query(with_volumes, orphans)}" if with_volumes or orphans else here

    def refuse(failure: Failure, environment_name: str = "") -> Response:
        return problem_page(
            request,
            heading="Remove stack",
            failure=failure,
            back_href=detail_href,
            back_label="Back to the stack",
            review_href=review,
            environment_name=environment_name,
            region=None,
        )

    if orphans and form.get("confirm_force") != CONFIRMED:
        return refuse(Failure(400, "", ""))
    try:
        confirmation = await stacks.queries.remove_confirmation(
            actor, environment_id, stack_name, volumes=False
        )
    except STACK_ERRORS as error:
        return refuse(failure_of(problem_of(error, stacks, actor)))
    name = confirmation.environment_name
    if (
        form.get("expected_stack_id") != str(confirmation.stack_id)
        or form.get("expected_containers") != confirmation.expected_containers
        or form.get("expected_networks") != confirmation.expected_networks
    ):
        stale = await _refused(
            stacks, actor, environment_id, stack_name, "remove", ChangedSinceReview()
        )
        return refuse(failure_of(stale), name)
    if confirmation.unwritable_containers:
        await _refused(stacks, actor, environment_id, stack_name, "remove", unwritable_members())
        return refuse(Failure(403, "You cannot change every container", MEMBERS_REFUSED), name)
    busy = await _busy_refusal(stacks, actor, environment_id, stack_name, "remove")
    if busy is not None:
        return refuse(busy, name)
    expected = form.get("expected_volumes") if with_volumes else None
    try:
        job_id = start_stack_job(
            request.app,
            actor,
            environment_id,
            "remove",
            stack_name,
            lambda services, progress: services.actions.remove(
                actor,
                environment_id,
                stack_name,
                remove_volumes=with_volumes,
                expected_volumes=expected,
                remove_orphans=orphans,
                expected_stack_id=confirmation.stack_id,
                expected_containers=form.get("expected_containers"),
                expected_networks=form.get("expected_networks"),
                progress=progress,
            ),
        )
    except TooManyStackJobs as busy_jobs:
        return refuse(Failure(429, "Too many operations are running", str(busy_jobs)), name)
    return redirect(request, _job_path(request, environment_id, job_id))


@router.get("/{stack_name}/forget", response_class=HTMLResponse, name="environment_stack_forget")
async def forget_confirm(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Say what forgetting leaves on the host; administrators only."""
    confirmation = await stacks.queries.forget_confirmation(actor, environment_id, stack_name)
    return page_response(
        request,
        "stacks/forget_confirm.html",
        {
            "confirmation": confirmation,
            "detail_href": _detail_path(request, environment_id, stack_name),
            "run_href": _stack_route(
                request, "environment_stack_forget_run", environment_id, stack_name
            ),
        },
    )


@router.post(
    "/{stack_name}/forget", response_class=HTMLResponse, name="environment_stack_forget_run"
)
async def forget_run(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Forget the stack without touching the host; administrators only."""
    form = await read_stack_form(request)
    try:
        await stacks.actions.forget(
            actor,
            environment_id,
            stack_name,
            expected_stack_id=posted_stack_id(form.get("expected_stack_id")),
        )
    except STACK_ERRORS as error:
        return problem_page(
            request,
            heading="Forget stack",
            failure=failure_of(problem_of(error, stacks, actor)),
            back_href=_detail_path(request, environment_id, stack_name),
            back_label="Back to the stack",
            review_href=_stack_route(
                request, "environment_stack_forget", environment_id, stack_name
            ),
            region=None,
        )
    return redirect(
        request, _list_path(request, environment_id), [Flash("ok", LIST_NOTICES["forgotten"])]
    )


def _adopt_unavailable(request: Request, error: Exception, environment_id: int) -> Response:
    return unavailable_page(
        request,
        "Adopt stack",
        error,
        _list_path(request, environment_id),
        "Back to stacks",
        region=None,
    )


async def _adopt_page(
    request: Request,
    stacks: StackServices,
    environments: Environments,
    actor: Principal,
    environment_id: int,
    project: str,
    state: FormState,
    status_code: int = 200,
) -> Response:
    environment = (await environments.environments.get(actor, environment_id)).summary
    try:
        found = await stacks.queries.adopt_context(actor, environment_id, project)
    except UNAVAILABLE as unavailable:
        return _adopt_unavailable(request, unavailable, environment_id)
    return page_response(
        request,
        "stacks/adopt.html",
        {
            **_form_context(request, "adopt", environment_id, state, project=project),
            "environment": environment,
            "project": found,
            "compose": _compose(stacks),
            "read_href": route(
                request,
                "environment_stack_read_host",
                environment_id=environment_id,
                project=project,
            ),
        },
        status_code,
    )


@router.get("/-/adopt/{project}", response_class=HTMLResponse, name="environment_stack_adopt")
async def adopt_form(
    project: ProjectName,
    request: Request,
    stacks: Stacks,
    environments: Environments,
    stash: HostReads,
    actor: CurrentUser,
    environment_id: PathId,
    read: Annotated[str | None, Query(pattern=TOKEN)] = None,
) -> Response:
    """Ask for the file of an unmanaged project; administrators only.

    `read` names a result read from the host, which fills the form.
    """
    held = (
        None if read is None else stash.get(read, actor.user_id, environment_id, "compose", project)
    )
    state = (
        FormState(name=project, rows=padded([]), file_rows=padded_files([]))
        if held is None
        else prefilled_state(project, held)
    )
    return await _adopt_page(request, stacks, environments, actor, environment_id, project, state)


@router.post(
    "/-/adopt/{project}/validate",
    response_class=HTMLResponse,
    name="environment_stack_validate_adopt",
)
async def validate_adopt(
    project: ProjectName,
    request: Request,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Check the file for adopting an unmanaged project; nothing is stored or run."""
    try:
        await stacks.queries.adopt_context(actor, environment_id, project)
    except UNAVAILABLE as unavailable:
        return _adopt_unavailable(request, unavailable, environment_id)
    form = await read_stack_form(request)
    report, problem = await _check(
        stacks,
        actor,
        environment_id,
        form,
        name=project,
        adopt=True,
        deploy=form.flag("deploy"),
    )
    state = (
        await problem_state(stacks, actor, environment_id, form, problem, name=project)
        if problem
        else state_from(
            form,
            report=report,
            offers=await offers_for_report(stacks, actor, environment_id, form, report),
            name=project,
        )
    )
    if is_htmx_request(request):
        return _validation_fragment(request, report, problem, state.offers)
    return await _adopt_page(
        request,
        stacks,
        environments,
        actor,
        environment_id,
        project,
        state,
        problem.status if problem else 200,
    )


@router.post("/-/adopt/{project}", response_class=HTMLResponse, name="environment_stack_adopt_run")
async def adopt_run(
    project: ProjectName,
    request: Request,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Adopt the project, and redeploy it as a job when asked."""
    try:
        await stacks.queries.adopt_context(actor, environment_id, project)
    except UNAVAILABLE as unavailable:
        return _adopt_unavailable(request, unavailable, environment_id)
    form = await read_stack_form(request)
    if form.adding_rows():
        return await _adopt_page(
            request,
            stacks,
            environments,
            actor,
            environment_id,
            project,
            state_from(form, name=project),
        )
    deploy = form.flag("deploy")
    try:
        text = form.text()
        variables = form.variable_inputs()
        files = form.file_inputs()
        choices = form.choices()
        with stacks.jobs.reserve(actor) if deploy else nullcontext():
            await stacks.actions.adopt(
                actor,
                environment_id,
                name=project,
                compose_text=text,
                variables=variables,
                deploy_requested=deploy,
                choices=choices,
                resolve_pulls=deploy,
                files=files,
            )
    except STACK_ERRORS as error:
        problem = problem_of(error, stacks, actor)
    else:
        if deploy:
            return _deploy_job(
                request,
                actor,
                environment_id,
                project,
                "adopt",
                lambda services, progress: services.actions.redeploy(
                    actor, environment_id, project, choices=choices, progress=progress
                ),
                heading="Adopt stack",
            )
        return redirect(
            request,
            _detail_path(request, environment_id, project),
            [Flash(*DETAIL_NOTICES["adopted"])],
        )
    return await _adopt_page(
        request,
        stacks,
        environments,
        actor,
        environment_id,
        project,
        await problem_state(stacks, actor, environment_id, form, problem, name=project),
        problem.status,
    )


@router.post(
    "/{stack_name}/access", response_class=HTMLResponse, name="environment_stack_access_save"
)
async def save_access(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    access: Access,
    actor: CurrentUser,
    environment_id: PathId,
    data: AccessData,
) -> Response:
    """Create or replace the resource control over one stack; administrators only."""
    try:
        panel = await stacks.queries.access_panel(actor, environment_id, stack_name)
    except UNAVAILABLE as unavailable:
        return _access_unavailable(request, environment_id, unavailable)
    refusal = await apply_access(actor, access.controls, stacks.queries, panel, data)
    if refusal is not None:
        return await _detail_response(
            request,
            stacks,
            actor,
            environment_id,
            stack_name,
            "access",
            status_code=refusal.status_code,
            values=refusal.values,
            fields=refusal.fields,
        )
    return redirect(
        request,
        _access_path(request, environment_id, stack_name),
        [Flash("ok", ACCESS_SAVED)],
    )


@router.post(
    "/{stack_name}/access/delete",
    response_class=HTMLResponse,
    name="environment_stack_access_delete",
)
async def delete_access(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    access: Access,
    actor: CurrentUser,
    environment_id: PathId,
    expected_digest: DigestField = "",
) -> Response:
    """Remove the resource control over one stack; administrators only."""
    try:
        panel = await stacks.queries.access_panel(actor, environment_id, stack_name)
    except UNAVAILABLE as unavailable:
        return _access_unavailable(request, environment_id, unavailable)
    refusal = await remove_access(actor, access.controls, panel, expected_digest)
    if refusal is not None:
        return await _detail_response(
            request,
            stacks,
            actor,
            environment_id,
            stack_name,
            "access",
            status_code=refusal.status_code,
            fields=refusal.fields,
        )
    return redirect(
        request,
        _access_path(request, environment_id, stack_name),
        [Flash("ok", ACCESS_REMOVED)],
    )


def _access_path(request: Request, environment_id: int, stack_name: str) -> str:
    return f"{_detail_path(request, environment_id, stack_name)}?{query_of('tab', 'access')}"


def _access_unavailable(request: Request, environment_id: int, unavailable: Exception) -> Response:
    return unavailable_page(
        request,
        "Stack",
        unavailable,
        _list_path(request, environment_id),
        "Back to stacks",
        region=None,
    )


def refusal_failure(problem: Problem, error: Exception) -> Failure:
    """Return the notice for a refusal that has no form to sit beside."""
    if problem.failure is not None:
        return problem.failure
    title = problem.rejection.title if problem.rejection else "Check the request"
    return Failure(problem.status, title, str(error))


@router.get("/-/jobs/{job_id}", response_class=HTMLResponse, name="environment_stack_job")
async def job_page(
    request: Request,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    job_id: JobId,
) -> Response:
    """Show the state of a stack operation; script follows it live, the page itself needs none."""
    await environments.environments.get(actor, environment_id)
    job = stacks.jobs.view(job_id, actor)
    if job.environment_id != environment_id or job.stack_kind != "compose":
        raise ResourceNotFound("not found")
    return page_response(
        request,
        "stacks/job.html",
        {
            "job": job,
            "list_href": _list_path(request, environment_id),
            "job_href": self_href(request),
            "stream_href": route(
                request,
                "environment_stack_job_stream",
                environment_id=environment_id,
                job_id=job_id,
            ),
        },
    )
