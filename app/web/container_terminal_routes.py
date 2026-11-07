"""Terminal pages for containers: the form, and the one-time ticket that opens the WebSocket."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import ResourceNotFound, SelfProtected, ValidationFailed
from app.auth.sessions import session_cookie_name
from app.services.assembly import Containers
from app.services.container_stats import ContainerNotRunning
from app.services.object_inspector import ContainerTooLarge, EnvironmentUnavailable
from app.services.object_locks import ObjectBusy, TooManyStreams
from app.services.terminals import (
    AttachNotAvailable,
    RestrictedContainer,
    TerminalForm,
    TerminalMode,
    open_failure,
)
from app.web.container_routes import ContainerId
from app.web.identifiers import PathId
from app.web.navigation import no_store
from app.web.problems import unreachable_copy
from app.web.security_headers import use_policy
from app.web.templating import render_page

router = APIRouter(
    prefix="/environments/{environment_id}/containers",
    tags=["containers"],
    dependencies=[Depends(require_user)],
)

MODES: tuple[TerminalMode, ...] = ("exec", "attach")
FORM_FIELD_MAX = 4096
SIZE_FIELD_MAX = 8
USER_NOT_OFFERED = "Running as another user is not available to your role."

Mode = Annotated[str, Query(max_length=16)]
FormText = Annotated[str, Form(max_length=FORM_FIELD_MAX)]
SizeText = Annotated[str, Form(max_length=SIZE_FIELD_MAX)]

REFUSALS = (
    SelfProtected,
    RestrictedContainer,
    ContainerNotRunning,
    ObjectBusy,
    AttachNotAvailable,
    TooManyStreams,
)


def _refusal_page(error: Exception) -> tuple[int, str]:
    """Return the status and heading for a refusal of the ticket request."""
    match error:
        case SelfProtected():
            return 409, "This container is protected"
        case RestrictedContainer():
            return 403, "A terminal cannot be opened here"
        case ContainerNotRunning():
            return 409, "The container is not running"
        case AttachNotAvailable():
            return 409, "This container cannot be attached to"
        case ObjectBusy():
            return 409, "This container is busy"
        case _:
            return 429, "Too many terminals are open"


def _mode(value: str) -> TerminalMode:
    for mode in MODES:
        if value == mode:
            return mode
    raise ResourceNotFound("not found")


def _terminal_path(request: Request, environment_id: int, container_id: str, mode: str) -> str:
    path = request.app.url_path_for(
        "environment_container_terminal", environment_id=environment_id, container_id=container_id
    )
    return f"{path}?mode={mode}"


def _detail_path(request: Request, environment_id: int, container_id: str) -> str:
    return str(
        request.app.url_path_for(
            "environment_container_detail",
            environment_id=environment_id,
            container_id=container_id,
        )
    )


def _problem(
    request: Request,
    environment_id: int,
    container_id: str,
    mode: str,
    *,
    title: str,
    message: str,
    status_code: int,
) -> Response:
    return no_store(
        render_page(
            request,
            "containers/problem.html",
            {
                "environment_name": "",
                "heading": "Terminal",
                "title": title,
                "message": message,
                "back_href": _detail_path(request, environment_id, container_id),
                "back_label": "Back to the container",
                "retry_href": _terminal_path(request, environment_id, container_id, mode),
            },
            status_code=status_code,
        )
    )


def _form_page(
    request: Request,
    form: TerminalForm,
    values: dict[str, str],
    errors: dict[str, str],
    status_code: int = 200,
) -> Response:
    return no_store(
        render_page(
            request,
            "containers/terminal_form.html",
            {
                "form": form,
                "values": {
                    "command": form.default_command,
                    "working_dir": "",
                    "user": "",
                    "rows": str(form.default_rows),
                    "cols": str(form.default_cols),
                    **values,
                },
                "errors": errors,
                "detail_href": _detail_path(request, form.environment_id, form.container_id),
                "exec_href": _terminal_path(
                    request, form.environment_id, form.container_id, "exec"
                ),
                "attach_href": _terminal_path(
                    request, form.environment_id, form.container_id, "attach"
                ),
            },
            status_code=status_code,
        )
    )


@router.get(
    "/{container_id}/terminal", response_class=HTMLResponse, name="environment_container_terminal"
)
async def terminal_form(
    container_id: ContainerId,
    request: Request,
    containers: Containers,
    actor: CurrentUser,
    environment_id: PathId,
    mode: Mode = "exec",
) -> Response:
    """Show the terminal form for a container; write access. Nothing is opened."""
    chosen = _mode(mode)
    try:
        form = await containers.terminals.form_context(actor, environment_id, container_id, chosen)
    except (EnvironmentUnavailable, ContainerTooLarge) as unavailable:
        return _problem(
            request,
            environment_id,
            container_id,
            chosen,
            title=unreachable_copy(unavailable).title,
            message=unreachable_copy(unavailable).message,
            status_code=503,
        )
    return _form_page(request, form, {}, {})


@router.post(
    "/{container_id}/terminal", response_class=HTMLResponse, name="environment_container_terminal"
)
async def terminal_prepare(
    container_id: ContainerId,
    request: Request,
    containers: Containers,
    actor: CurrentUser,
    environment_id: PathId,
    mode: Mode = "exec",
    command: FormText = "",
    working_dir: FormText = "",
    user: FormText = "",
    rows: SizeText = "",
    cols: SizeText = "",
) -> Response:
    """Validate the form and issue a ticket, then render the page that redeems it; write access.

    Opens no session; only a refusal for dockmin's own container is recorded here.
    """
    chosen = _mode(mode)
    submitted = {
        "command": command,
        "working_dir": working_dir,
        "user": user,
        "rows": rows,
        "cols": cols,
    }
    token = request.cookies.get(session_cookie_name(request.app.state.settings), "")
    try:
        issued = await containers.terminals.prepare(
            actor, token, environment_id, container_id, chosen, submitted
        )
    except ValidationFailed as invalid:
        try:
            form = await containers.terminals.form_context(
                actor, environment_id, container_id, chosen
            )
        except (EnvironmentUnavailable, ContainerTooLarge) as unavailable:
            return _problem(
                request,
                environment_id,
                container_id,
                chosen,
                title=unreachable_copy(unavailable).title,
                message=unreachable_copy(unavailable).message,
                status_code=503,
            )
        errors = dict(invalid.fields)
        if "user" in errors and not form.offers_user:
            errors["user"] = USER_NOT_OFFERED
        return _form_page(request, form, submitted, errors, status_code=400)
    except REFUSALS as refused:
        status_code, title = _refusal_page(refused)
        return _problem(
            request,
            environment_id,
            container_id,
            chosen,
            title=title,
            message=open_failure(refused).message or str(refused),
            status_code=status_code,
        )
    except (EnvironmentUnavailable, ContainerTooLarge) as unavailable:
        return _problem(
            request,
            environment_id,
            container_id,
            chosen,
            title=unreachable_copy(unavailable).title,
            message=unreachable_copy(unavailable).message,
            status_code=503,
        )
    use_policy(request, "terminal")
    return no_store(
        render_page(
            request,
            "containers/terminal.html",
            {
                "issued": issued,
                "environment_id": environment_id,
                "container_id": container_id,
                "detail_href": _detail_path(request, environment_id, container_id),
                "form_href": _terminal_path(request, environment_id, container_id, chosen),
            },
        )
    )
