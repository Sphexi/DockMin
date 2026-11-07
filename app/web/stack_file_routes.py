"""Compose stack pages: the companion files (add, edit, remove), build contexts and the restart."""

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentAdministrator, CurrentUser, require_user
from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.db.repositories import stack_build_context as context_limits
from app.services.assembly import Stacks, StackServices, start_stack_job
from app.services.stack_deploy import ChangedSinceReview, StackRejected
from app.services.stack_jobs import TooManyStackJobs
from app.services.stack_views import NotRunning, unwritable_members
from app.web.flash import Flash, FlashLevel
from app.web.identifiers import PathId
from app.web.object_pages import (
    Failure,
    page_response,
    problem_page,
    query_of,
    redirect,
    route,
    unavailable_page,
)
from app.web.restore_upload import read_archive_upload
from app.web.stack_file_pages import (
    FileState,
    read_submission,
    state_of_refusal,
    state_of_submission,
)
from app.web.stack_pages import failure_of, read_stack_form
from app.web.stack_routes import (
    FILE_NOTICES,
    FILES_TAB,
    MEMBERS_REFUSED,
    STACK_ERRORS,
    UNAVAILABLE,
    FileName,
    StackName,
    _busy_refusal,
    _compose,
    _detail_path,
    _job_path,
    _refused,
    _stack_route,
    compose_notice,
    problem_of,
    refusal_failure,
)

router = APIRouter(
    prefix="/environments/{environment_id}/stacks",
    tags=["stacks"],
    dependencies=[Depends(require_user)],
)

CONTEXT_NOTICES: Final[dict[str, tuple[FlashLevel, str]]] = {
    "saved": ("ok", "Build context saved. Redeploy the stack to apply it."),
    "removed": ("ok", "Build context removed."),
}
CONTEXT_NAME_FIELD: Final = "name"
CONTEXT_STATUSES: Final = {"context_in_use": 409, "too_large": 413, "upload_too_large": 413}
UPLOAD_TOO_LARGE: Final = "The archive is larger than 16 MiB."


@router.get("/{stack_name}/restart", response_class=HTMLResponse, name="environment_stack_restart")
async def restart_confirm(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Name the services a restart stops and starts; write access is needed."""
    try:
        confirmation = await stacks.queries.restart_confirmation(actor, environment_id, stack_name)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Restart stack",
            unavailable,
            _detail_path(request, environment_id, stack_name),
            "Back to the stack",
            region=None,
        )
    return page_response(
        request,
        "stacks/restart_confirm.html",
        {
            "confirmation": confirmation,
            "compose": compose_notice(confirmation.compose),
            "detail_href": _detail_path(request, environment_id, stack_name),
            "run_href": _stack_route(
                request, "environment_stack_restart_run", environment_id, stack_name
            ),
        },
    )


@router.post(
    "/{stack_name}/restart", response_class=HTMLResponse, name="environment_stack_restart_run"
)
async def restart_run(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Restart the reviewed stack's running services as a job."""
    form = await read_stack_form(request)
    detail_href = _detail_path(request, environment_id, stack_name)
    review = _stack_route(request, "environment_stack_restart", environment_id, stack_name)

    def refuse(failure: Failure, environment_name: str = "") -> Response:
        return problem_page(
            request,
            heading="Restart stack",
            failure=failure,
            back_href=detail_href,
            back_label="Back to the stack",
            review_href=review,
            environment_name=environment_name,
            region=None,
        )

    try:
        confirmation = await stacks.queries.restart_confirmation(actor, environment_id, stack_name)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Restart stack", unavailable, detail_href, "Back to the stack", region=None
        )
    name = confirmation.environment_name
    if form.get("expected_containers") != confirmation.expected_containers:
        stale = await _refused(
            stacks, actor, environment_id, stack_name, "restart", ChangedSinceReview()
        )
        return refuse(failure_of(stale), name)
    if confirmation.unwritable_containers:
        await _refused(stacks, actor, environment_id, stack_name, "restart", unwritable_members())
        return refuse(Failure(403, "You cannot change every container", MEMBERS_REFUSED), name)
    if not confirmation.services and not confirmation.hidden_containers:
        await _refused(stacks, actor, environment_id, stack_name, "restart", NotRunning())
        return refuse(
            Failure(409, "Nothing is running", "No container of this stack is running."), name
        )
    busy = await _busy_refusal(stacks, actor, environment_id, stack_name, "restart")
    if busy is not None:
        return refuse(busy, name)
    expected = form.get("expected_containers")
    try:
        job_id = start_stack_job(
            request.app,
            actor,
            environment_id,
            "restart",
            stack_name,
            lambda services, progress: services.actions.restart(
                actor, environment_id, stack_name, expected_containers=expected, progress=progress
            ),
        )
    except TooManyStackJobs as busy_jobs:
        return refuse(Failure(429, "Too many operations are running", str(busy_jobs)), name)
    return redirect(request, _job_path(request, environment_id, job_id))


def _files_path(request: Request, environment_id: int, stack_name: str) -> str:
    return f"{_detail_path(request, environment_id, stack_name)}?{query_of('tab', FILES_TAB)}"


def _file_page(
    request: Request,
    stacks: StackServices,
    environment_id: int,
    stack_name: str,
    template: str,
    context: dict[str, Any],
    status_code: int = 200,
) -> Response:
    return page_response(
        request,
        template,
        {
            "stack_name": stack_name,
            "swarm": False,
            "compose": _compose(stacks),
            "detail_href": _files_path(request, environment_id, stack_name),
            **context,
        },
        status_code,
    )


def _file_form_page(
    request: Request,
    stacks: StackServices,
    environment_id: int,
    stack_name: str,
    environment_name: str,
    digest: str,
    state: FileState,
    status_code: int = 200,
    *,
    file: str | None = None,
    stored_secret: bool = False,
) -> Response:
    if file is None:
        post_href = _stack_route(
            request, "environment_stack_file_create", environment_id, stack_name
        )
    else:
        post_href = route(
            request,
            "environment_stack_file_save",
            environment_id=environment_id,
            stack_name=stack_name,
            file=file,
        )
    return _file_page(
        request,
        stacks,
        environment_id,
        stack_name,
        "stacks/file_form.html",
        {
            "file_name": file,
            "form": state,
            "environment_name": environment_name,
            "expected_inputs_digest": digest,
            "post_href": post_href,
            "stored_secret": stored_secret,
        },
        status_code,
    )


@router.get(
    "/{stack_name}/files/-/new", response_class=HTMLResponse, name="environment_stack_file_new"
)
async def new_file(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show the form that adds one companion file; write access is needed."""
    form = await stacks.queries.file_form(actor, environment_id, stack_name, None)
    return _file_form_page(
        request,
        stacks,
        environment_id,
        stack_name,
        form.environment_name,
        form.inputs_digest,
        FileState("", "", False),
    )


async def _exists(
    stacks: StackServices, actor: Principal, environment_id: int, stack_name: str, file: str
) -> bool:
    try:
        await stacks.queries.file_form(actor, environment_id, stack_name, file)
    except ResourceNotFound:
        return False
    return True


@router.post(
    "/{stack_name}/files/-/new", response_class=HTMLResponse, name="environment_stack_file_create"
)
async def create_file(
    stack_name: StackName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Add one companion file, checking the whole stack with it; nothing is deployed."""
    form = await stacks.queries.file_form(actor, environment_id, stack_name, None)
    submission = await read_submission(request, fixed_name=None, keeps=False)
    errors = dict(submission.errors)
    if "name" not in errors and await _exists(
        stacks, actor, environment_id, stack_name, submission.name
    ):
        errors["name"] = "A file with this name is already added. Edit it instead."
    if errors:
        return _file_form_page(
            request,
            stacks,
            environment_id,
            stack_name,
            form.environment_name,
            submission.expected_inputs_digest,
            state_of_submission(replace(submission, errors=errors)),
            400,
        )
    try:
        await stacks.actions.write_file(
            actor,
            environment_id,
            stack_name,
            submission.name,
            content=submission.content,
            secret=submission.secret,
            expected_inputs_digest=submission.expected_inputs_digest,
        )
    except STACK_ERRORS as error:
        problem = problem_of(error, stacks, actor)
        return _file_form_page(
            request,
            stacks,
            environment_id,
            stack_name,
            form.environment_name,
            submission.expected_inputs_digest,
            state_of_refusal(submission, problem, error, submission.name),
            problem.status,
        )
    return redirect(
        request, _files_path(request, environment_id, stack_name), [Flash(*FILE_NOTICES["added"])]
    )


@router.get(
    "/{stack_name}/files/{file}/edit",
    response_class=HTMLResponse,
    name="environment_stack_file_edit",
)
async def edit_file(
    stack_name: StackName,
    file: FileName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show one companion file for editing; write access is needed."""
    form = await stacks.queries.file_form(actor, environment_id, stack_name, file)
    stored = form.file
    if stored is None:
        raise ResourceNotFound("not found")
    return _file_form_page(
        request,
        stacks,
        environment_id,
        stack_name,
        form.environment_name,
        form.inputs_digest,
        FileState(stored.name, stored.content, stored.secret),
        file=file,
        stored_secret=stored.secret,
    )


@router.post(
    "/{stack_name}/files/{file}/edit",
    response_class=HTMLResponse,
    name="environment_stack_file_save",
)
async def save_file(
    stack_name: StackName,
    file: FileName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Replace one companion file, checking the whole stack with it; nothing is deployed."""
    form = await stacks.queries.file_form(actor, environment_id, stack_name, file)
    stored = form.file
    if stored is None:
        raise ResourceNotFound("not found")
    submission = await read_submission(request, fixed_name=file, keeps=stored.secret)
    if submission.errors:
        return _file_form_page(
            request,
            stacks,
            environment_id,
            stack_name,
            form.environment_name,
            submission.expected_inputs_digest,
            state_of_submission(submission),
            400,
            file=file,
            stored_secret=stored.secret,
        )
    try:
        await stacks.actions.write_file(
            actor,
            environment_id,
            stack_name,
            file,
            content=submission.content,
            secret=submission.secret,
            expected_inputs_digest=submission.expected_inputs_digest,
        )
    except STACK_ERRORS as error:
        problem = problem_of(error, stacks, actor)
        return _file_form_page(
            request,
            stacks,
            environment_id,
            stack_name,
            form.environment_name,
            submission.expected_inputs_digest,
            state_of_refusal(submission, problem, error, file),
            problem.status,
            file=file,
            stored_secret=stored.secret,
        )
    return redirect(
        request, _files_path(request, environment_id, stack_name), [Flash(*FILE_NOTICES["saved"])]
    )


@router.get(
    "/{stack_name}/files/{file}/remove",
    response_class=HTMLResponse,
    name="environment_stack_file_remove",
)
async def remove_file_confirm(
    stack_name: StackName,
    file: FileName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Name the file a remove deletes and where it is still used; write access is needed."""
    confirmation = await stacks.queries.file_remove_confirmation(
        actor, environment_id, stack_name, file
    )
    return _file_page(
        request,
        stacks,
        environment_id,
        stack_name,
        "stacks/file_remove_confirm.html",
        {
            "confirmation": confirmation,
            "run_href": route(
                request,
                "environment_stack_file_remove_run",
                environment_id=environment_id,
                stack_name=stack_name,
                file=file,
            ),
            "edit_href": route(
                request,
                "environment_stack_file_edit",
                environment_id=environment_id,
                stack_name=stack_name,
                file=file,
            ),
            "stack_edit_href": _stack_route(
                request, "environment_stack_edit", environment_id, stack_name
            ),
        },
    )


@router.post(
    "/{stack_name}/files/{file}/remove",
    response_class=HTMLResponse,
    name="environment_stack_file_remove_run",
)
async def remove_file_run(
    stack_name: StackName,
    file: FileName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Delete one companion file, checking the whole stack without it; nothing is deployed."""
    form = await read_stack_form(request)
    review = route(
        request,
        "environment_stack_file_remove",
        environment_id=environment_id,
        stack_name=stack_name,
        file=file,
    )
    try:
        await stacks.actions.remove_file(
            actor,
            environment_id,
            stack_name,
            file,
            expected_inputs_digest=form.get("expected_inputs_digest") or "",
        )
    except STACK_ERRORS as error:
        problem = problem_of(error, stacks, actor)
        return problem_page(
            request,
            heading="Remove file",
            failure=refusal_failure(problem, error),
            back_href=_files_path(request, environment_id, stack_name),
            back_label="Back to the files",
            review_href=review,
            region=None,
        )
    return redirect(
        request,
        _files_path(request, environment_id, stack_name),
        [Flash(*FILE_NOTICES["removed"])],
    )


def _context_failure(error: Exception, stacks: StackServices, actor: Principal) -> Failure:
    if isinstance(error, StackRejected):
        status = CONTEXT_STATUSES.get(error.reason, 422)
        title = "Build context refused" if status == 422 else "Build context not changed"
        return Failure(status, title, str(error))
    return refusal_failure(problem_of(error, stacks, actor), error)


async def _upload_bytes(parts: AsyncIterator[bytes]) -> bytes:
    received: list[bytes] = []
    size = 0
    async for chunk in parts:
        size += len(chunk)
        if size > context_limits.MAX_CONTEXT_UPLOAD_BYTES:
            raise StackRejected({"archive": UPLOAD_TOO_LARGE}, "upload_too_large")
        received.append(chunk)
    return b"".join(received)


@router.post(
    "/{stack_name}/contexts/-/upload",
    response_class=HTMLResponse,
    name="environment_stack_context_upload",
)
async def upload_context(
    stack_name: StackName,
    request: Request,
    actor: CurrentAdministrator,
    stacks: Stacks,
    environment_id: PathId,
) -> Response:
    """Add or replace a build context from a streamed archive; the first part is the CSRF token."""
    fields: dict[str, str] = {}

    async def receive(parts: AsyncIterator[bytes]) -> None:
        archive = await _upload_bytes(parts)
        await stacks.contexts.write(
            actor,
            environment_id,
            stack_name,
            fields.get(CONTEXT_NAME_FIELD, ""),
            archive=archive,
        )

    try:
        await read_archive_upload(
            request, receive, fields=fields, field_names=frozenset({CONTEXT_NAME_FIELD})
        )
    except STACK_ERRORS as error:
        return problem_page(
            request,
            heading="Upload build context",
            failure=_context_failure(error, stacks, actor),
            back_href=_files_path(request, environment_id, stack_name),
            back_label="Back to the files",
            region=None,
        )
    return redirect(
        request,
        _files_path(request, environment_id, stack_name),
        [Flash(*CONTEXT_NOTICES["saved"])],
    )


@router.get(
    "/{stack_name}/contexts/{file}/remove",
    response_class=HTMLResponse,
    name="environment_stack_context_remove",
)
async def remove_context_confirm(
    stack_name: StackName,
    file: FileName,
    request: Request,
    actor: CurrentAdministrator,
    stacks: Stacks,
    environment_id: PathId,
) -> Response:
    """Name the build context a remove deletes and where it is still used."""
    confirmation = await stacks.contexts.remove_confirmation(
        actor, environment_id, stack_name, file
    )
    return _file_page(
        request,
        stacks,
        environment_id,
        stack_name,
        "stacks/context_remove_confirm.html",
        {
            "confirmation": confirmation,
            "run_href": route(
                request,
                "environment_stack_context_remove_run",
                environment_id=environment_id,
                stack_name=stack_name,
                file=file,
            ),
            "stack_edit_href": _stack_route(
                request, "environment_stack_edit", environment_id, stack_name
            ),
        },
    )


@router.post(
    "/{stack_name}/contexts/{file}/remove",
    response_class=HTMLResponse,
    name="environment_stack_context_remove_run",
)
async def remove_context_run(
    stack_name: StackName,
    file: FileName,
    request: Request,
    actor: CurrentAdministrator,
    stacks: Stacks,
    environment_id: PathId,
) -> Response:
    """Delete one build context unless the Compose file still builds from it."""
    form = await read_stack_form(request)
    try:
        await stacks.contexts.remove(
            actor,
            environment_id,
            stack_name,
            file,
            expected_inputs_digest=form.get("expected_inputs_digest") or "",
        )
    except STACK_ERRORS as error:
        return problem_page(
            request,
            heading="Remove build context",
            failure=_context_failure(error, stacks, actor),
            back_href=_files_path(request, environment_id, stack_name),
            back_label="Back to the files",
            review_href=route(
                request,
                "environment_stack_context_remove",
                environment_id=environment_id,
                stack_name=stack_name,
                file=file,
            ),
            region=None,
        )
    return redirect(
        request,
        _files_path(request, environment_id, stack_name),
        [Flash(*CONTEXT_NOTICES["removed"])],
    )
