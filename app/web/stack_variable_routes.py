"""Reveal of one secret stack variable behind the reveal window, and its masked row."""

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import (
    CurrentUser,
    get_activity,
    get_runtime,
    remote_address,
    require_user,
)
from app.auth.errors import NotFound, ResourceNotFound, ValidationFailed
from app.auth.passwords import MAX_PASSWORD_LENGTH, HashingBusy
from app.auth.principal import Principal
from app.auth.services import AuthRuntime
from app.db.engine import Session, get_session
from app.db.repositories.stack import StackKind, StackRepository, StoredStack
from app.db.repositories.user_session import UserSessionRepository
from app.services.activity import ActivityRecorder
from app.services.assembly import Stacks, get_engine_runtime
from app.services.engine_setup import EngineRuntime
from app.services.reveal_access import RevealAccess, get_reveal_access
from app.services.secret_mask import MASK
from app.services.stack_variable_reveal import (
    CHANGED_SINCE_REVIEW,
    REVEAL_WINDOW_CLOSED,
    SECRETS_UNREADABLE,
    RevealedVariable,
    RevealRefused,
    StackVariableReveal,
)
from app.services.stack_views import VariableView
from app.web.identifiers import PathId
from app.web.object_pages import (
    Failure,
    fragment_response,
    page_response,
    problem_page,
    query_of,
    redirect,
    route,
)
from app.web.stack_routes import StackName
from app.web.swarm_routes import Namespace, swarm_path, swarm_router
from app.web.templating import is_htmx_request

__all__ = ["router", "swarm"]

router = APIRouter(
    prefix="/environments/{environment_id}/stacks",
    tags=["stacks"],
    dependencies=[Depends(require_user)],
)
swarm = swarm_router()

PAGE: Final = "stacks/variable_reveal.html"
ROW: Final = "stack_variable_row"
FORM: Final = "stack_variable_reveal_form"
REVEALED: Final = "stack_variable_revealed"
NAME_MAX: Final = 128
PASSWORD_FIELD_MAX: Final = MAX_PASSWORD_LENGTH + 1
WRONG_PASSWORD: Final = "The password is incorrect."
WINDOW_CLOSED: Final = "The reveal window closed. Enter your password again."
GONE: Final = Failure(404, "Variable not found", "The variable no longer exists.")
BUSY: Final = Failure(503, "Server busy", "The server is busy. Try again.")
REFUSALS: Final = {
    CHANGED_SINCE_REVIEW: Failure(
        409,
        "The variable changed",
        "The variable was renamed or replaced after this page loaded. Reload the Variables tab.",
    ),
    SECRETS_UNREADABLE: Failure(
        409, "The value cannot be read", "The stored value cannot be read with this server's key."
    ),
}

ExpectedName = Annotated[str, Form(max_length=NAME_MAX)]
Password = Annotated[str, Form(max_length=PASSWORD_FIELD_MAX)]


async def get_stack_variable_reveal(
    stacks: Stacks,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> StackVariableReveal:
    """Provide the reveal service bound to this request's session and recorder."""

    async def compose(actor: Principal, environment_id: int, name: str) -> StoredStack:
        return await stacks.queries.require_stack(actor, environment_id, name, "read")

    async def cluster(actor: Principal, environment_id: int, name: str) -> StoredStack:
        return await stacks.swarm_planner.require_stack(actor, environment_id, name, "read")

    return StackVariableReveal(
        db=db,
        stacks=StackRepository(db, engine.vault, runtime.clock),
        sessions=UserSessionRepository(db, runtime.clock),
        compose=compose,
        swarm=cluster,
        activity=activity,
        clock=runtime.clock,
    )


Reveals = Annotated[StackVariableReveal, Depends(get_stack_variable_reveal)]
Access = Annotated[RevealAccess, Depends(get_reveal_access)]


def _administrators_only(actor: Principal) -> None:
    if not actor.is_administrator:
        raise ResourceNotFound("not found")


def _row_id(variable_id: int) -> str:
    return f"variable-row-{variable_id}"


def _links(
    request: Request, kind: StackKind, environment_id: int, name: str, variable_id: int
) -> dict[str, str]:
    if kind == "compose":
        arguments: dict[str, Any] = {
            "environment_id": environment_id,
            "stack_name": name,
            "variable_id": variable_id,
        }
        detail = route(
            request, "environment_stack_detail", environment_id=environment_id, stack_name=name
        )
        return {
            "reveal_href": route(request, "environment_stack_variable_reveal", **arguments),
            "row_href": route(request, "environment_stack_variable_row", **arguments),
            "back_href": f"{detail}?{query_of('tab', 'variables')}",
        }
    arguments = {"environment_id": environment_id, "namespace": name, "variable_id": variable_id}
    detail = swarm_path(request, "stack_detail", environment_id=environment_id, namespace=name)
    return {
        "reveal_href": swarm_path(request, "stack_variable_reveal", **arguments),
        "row_href": swarm_path(request, "stack_variable_row", **arguments),
        "back_href": f"{detail}?{query_of('tab', 'variables')}",
    }


def _context(
    request: Request,
    kind: StackKind,
    environment_id: int,
    name: str,
    variable: VariableView,
    *,
    error: str | None = None,
    revealed: RevealedVariable | None = None,
) -> dict[str, Any]:
    return {
        "variable": variable,
        "environment_id": environment_id,
        "stack_kind": kind,
        "stack_name": name,
        "error": error,
        "revealed": None if revealed is None else revealed.value,
        "window_expires_at": None if revealed is None else revealed.window_expires_at,
        "reveals_left": None if revealed is None else revealed.reveals_left,
        "row_id": _row_id(variable.id),
        **_links(request, kind, environment_id, name, variable.id),
    }


def _answer(
    request: Request, macro: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    if is_htmx_request(request):
        return fragment_response(
            request, f"partials/{macro}.html", macro, status_code=status_code, **context
        )
    return page_response(request, PAGE, context, status_code)


async def _form(
    request: Request,
    service: StackVariableReveal,
    actor: Principal,
    kind: StackKind,
    environment_id: int,
    name: str,
    variable_id: int,
    *,
    error: str | None,
    status_code: int,
) -> Response:
    variable = await service.variable_view(actor, environment_id, kind, name, variable_id)
    context = _context(request, kind, environment_id, name, variable, error=error)
    return _answer(request, FORM, context, status_code)


async def _refusal(
    request: Request,
    service: StackVariableReveal,
    actor: Principal,
    kind: StackKind,
    environment_id: int,
    name: str,
    variable_id: int,
    expected_name: str,
    failure: Failure,
) -> Response:
    links = _links(request, kind, environment_id, name, variable_id)
    if not is_htmx_request(request):
        return problem_page(
            request,
            heading="Reveal variable",
            failure=failure,
            back_href=links["back_href"],
            back_label="Back to the Variables tab",
            region=None,
        )
    try:
        variable = await service.variable_view(actor, environment_id, kind, name, variable_id)
    except NotFound:
        variable = VariableView(variable_id, expected_name, True, None, MASK)
    context = _context(request, kind, environment_id, name, variable, error=failure.message)
    return _answer(request, ROW, context, failure.status)


async def _attempt(
    request: Request,
    access: RevealAccess,
    service: StackVariableReveal,
    actor: Principal,
    kind: StackKind,
    environment_id: int,
    name: str,
    variable_id: int,
    expected_name: str,
    password: str,
) -> Response:
    if password:
        try:
            await access.open(actor, password=password, remote_address=remote_address(request))
        except ValidationFailed as invalid:
            error = next(iter(invalid.fields.values()), WRONG_PASSWORD)
            return await _form(
                request,
                service,
                actor,
                kind,
                environment_id,
                name,
                variable_id,
                error=error,
                status_code=422,
            )
    elif not await service.window_open(actor):
        return await _form(
            request,
            service,
            actor,
            kind,
            environment_id,
            name,
            variable_id,
            error=None,
            status_code=200,
        )
    try:
        revealed = await service.reveal(
            actor, environment_id, kind, name, variable_id, expected_name
        )
    except RevealRefused as refused:
        if refused.reason == REVEAL_WINDOW_CLOSED:
            return await _form(
                request,
                service,
                actor,
                kind,
                environment_id,
                name,
                variable_id,
                error=WINDOW_CLOSED,
                status_code=422,
            )
        return await _refusal(
            request,
            service,
            actor,
            kind,
            environment_id,
            name,
            variable_id,
            expected_name,
            REFUSALS[refused.reason],
        )
    context = _context(request, kind, environment_id, name, revealed.variable, revealed=revealed)
    return _answer(request, REVEALED, context)


async def _reveal(
    request: Request,
    access: RevealAccess,
    service: StackVariableReveal,
    actor: Principal,
    kind: StackKind,
    environment_id: int,
    name: str,
    variable_id: int,
    expected_name: str,
    password: str,
) -> Response:
    _administrators_only(actor)
    try:
        return await _attempt(
            request,
            access,
            service,
            actor,
            kind,
            environment_id,
            name,
            variable_id,
            expected_name,
            password,
        )
    except (NotFound, HashingBusy) as error:
        if not is_htmx_request(request):
            raise
        failure = BUSY if isinstance(error, HashingBusy) else GONE
        return await _refusal(
            request,
            service,
            actor,
            kind,
            environment_id,
            name,
            variable_id,
            expected_name,
            failure,
        )


async def _row(
    request: Request,
    service: StackVariableReveal,
    actor: Principal,
    kind: StackKind,
    environment_id: int,
    name: str,
    variable_id: int,
) -> Response:
    _administrators_only(actor)
    variable = await service.variable_view(actor, environment_id, kind, name, variable_id)
    context = _context(request, kind, environment_id, name, variable)
    if not is_htmx_request(request):
        return redirect(request, context["back_href"])
    return _answer(request, ROW, context)


@router.post(
    "/{stack_name}/variables/{variable_id}/reveal",
    response_class=HTMLResponse,
    name="environment_stack_variable_reveal",
)
async def reveal_variable(
    request: Request,
    access: Access,
    service: Reveals,
    actor: CurrentUser,
    environment_id: PathId,
    stack_name: StackName,
    variable_id: PathId,
    expected_name: ExpectedName,
    password: Password = "",
) -> Response:
    """Reveal a secret variable of a Compose stack; ask for the password if no window is open."""
    return await _reveal(
        request,
        access,
        service,
        actor,
        "compose",
        environment_id,
        stack_name,
        variable_id,
        expected_name,
        password,
    )


@router.get(
    "/{stack_name}/variables/{variable_id}",
    response_class=HTMLResponse,
    name="environment_stack_variable_row",
)
async def variable_row(
    request: Request,
    service: Reveals,
    actor: CurrentUser,
    environment_id: PathId,
    stack_name: StackName,
    variable_id: PathId,
) -> Response:
    """Return a Compose stack's secret variable as its masked row."""
    return await _row(request, service, actor, "compose", environment_id, stack_name, variable_id)


@swarm.post(
    "/stacks/{namespace}/variables/{variable_id}/reveal",
    response_class=HTMLResponse,
    name="environment_swarm_stack_variable_reveal",
)
async def reveal_swarm_variable(
    request: Request,
    access: Access,
    service: Reveals,
    actor: CurrentUser,
    environment_id: PathId,
    namespace: Namespace,
    variable_id: PathId,
    expected_name: ExpectedName,
    password: Password = "",
) -> Response:
    """Reveal a secret variable of a Swarm stack; ask for the password if no window is open."""
    return await _reveal(
        request,
        access,
        service,
        actor,
        "swarm",
        environment_id,
        namespace,
        variable_id,
        expected_name,
        password,
    )


@swarm.get(
    "/stacks/{namespace}/variables/{variable_id}",
    response_class=HTMLResponse,
    name="environment_swarm_stack_variable_row",
)
async def swarm_variable_row(
    request: Request,
    service: Reveals,
    actor: CurrentUser,
    environment_id: PathId,
    namespace: Namespace,
    variable_id: PathId,
) -> Response:
    """Return a Swarm stack's secret variable as its masked row."""
    return await _row(request, service, actor, "swarm", environment_id, namespace, variable_id)
