"""Managed Swarm stack pages: create, edit, redeploy, remove, forget, adopt and the job page."""

from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Annotated, Final

from fastapi import Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from app.auth.dependencies import CurrentUser
from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.services.assembly import (
    Environments,
    Stacks,
    StackServices,
    Swarm,
    SwarmServices,
    start_stack_job,
)
from app.services.object_locks import StreamSlot, StreamSlots, TooManyStreams
from app.services.stack_deploy import ChangedSinceReview, StackRejected
from app.services.stack_jobs import StackJobRegistry, TooManyStackJobs
from app.services.stack_views import ValidationReport
from app.services.swarm_stack_actions import RefusableOperation
from app.services.swarm_stack_plan import UNWRITABLE_MEMBERS
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.navigation import no_store
from app.web.object_pages import (
    Failure,
    fragment_response,
    page_response,
    problem_page,
    query_of,
    redirect,
    self_href,
    unavailable_page,
)
from app.web.registry_providers import CATALOGUE
from app.web.stack_file_pages import (
    FileState,
    read_submission,
    state_of_refusal,
    state_of_submission,
)
from app.web.stack_pages import (
    CREDENTIAL_REASONS,
    FILE_CAP,
    VARIABLE_CAP,
    FormState,
    JobResponse,
    OfferChoice,
    Problem,
    StackForm,
    failure_of,
    job_frames,
    located,
    offers_for,
    offers_for_report,
    padded,
    padded_files,
    posted_stack_id,
    problem_state,
    read_stack_form,
    rows_from_stored,
    state_from,
)
from app.web.stack_read_pages import TOKEN, HostReads, prefilled_state
from app.web.stack_routes import (
    FILE_NOTICES,
    FILES_TAB,
    STACK_ERRORS,
    ComposeNotice,
    FileName,
    FormKind,
    JobId,
    compose_notice,
    problem_of,
    refusal_failure,
)
from app.web.swarm_routes import UNAVAILABLE, Namespace, swarm_path, swarm_router
from app.web.swarm_stack_routes import DETAIL_NOTICES, LIST_NOTICES
from app.web.templating import is_htmx_request, render_partial

router = swarm_router()

SWARM_ERRORS: Final = (*STACK_ERRORS, *UNAVAILABLE)
VALIDATION: Final = "swarm/stacks/_validation.html"
JOB_PROGRESS: Final = "swarm/stacks/_job.html"
JOB_MACRO: Final = "swarm_job_progress"
CHOSEN: Final = "yes"
MEMBERS_REFUSED: Final = (
    "A stack is changed only by someone who can change all of its services, networks, configs "
    "and secrets. Ask an administrator."
)


def _list_path(request: Request, environment_id: int) -> str:
    return swarm_path(request, "stack_list", environment_id=environment_id)


def _detail_path(request: Request, environment_id: int, namespace: str) -> str:
    return swarm_path(request, "stack_detail", environment_id=environment_id, namespace=namespace)


def _stack_route(request: Request, name: str, environment_id: int, namespace: str) -> str:
    return swarm_path(request, name, environment_id=environment_id, namespace=namespace)


def _job_path(request: Request, environment_id: int, job_id: str) -> str:
    return swarm_path(request, "stack_job", environment_id=environment_id, job_id=job_id)


def _redeploy_path(request: Request, environment_id: int, namespace: str, *, orphans: bool) -> str:
    here = _stack_route(request, "stack_redeploy", environment_id, namespace)
    return f"{here}?{query_of('remove_orphans', CHOSEN)}" if orphans else here


def _compose(stacks: StackServices) -> ComposeNotice | None:
    return compose_notice(stacks.swarm_planner.compose_status())


def _echo(form: StackForm) -> list[tuple[int, str, str]]:
    """Return the credential choices to carry on, as (number, reference, choice)."""
    return [
        (number, reference, text)
        for number, (reference, text) in enumerate(form.posted_choices().items())
        if text.strip()
    ]


async def _refused(
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    namespace: str,
    operation: RefusableOperation,
    error: Exception,
) -> Problem:
    """Record a refusal given before the operation started, as the operation does; answer it."""
    try:
        await stacks.swarm_actions.record_refusal(
            actor, environment_id, namespace, operation=operation, error=error
        )
    except SWARM_ERRORS as recorded:
        return problem_of(recorded, stacks, actor)
    raise RuntimeError("a recorded refusal raises")


async def _busy_refusal(
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    namespace: str,
    operation: RefusableOperation,
) -> Failure | None:
    """Record and return the notice for a stack another action holds, or None when it is free."""
    busy = await stacks.swarm_planner.busy_error(actor, environment_id, namespace)
    if busy is None:
        return None
    return failure_of(await _refused(stacks, actor, environment_id, namespace, operation, busy))


def _form_context(
    request: Request,
    kind: FormKind,
    environment_id: int,
    state: FormState,
    *,
    namespace: str = "",
    expected_digest: str = "",
) -> dict[str, object]:
    """Return the links and labels of a form page; the caller adds the environment."""
    list_href = _list_path(request, environment_id)
    if kind == "new":
        links = {
            "post_href": swarm_path(request, "stack_create", environment_id=environment_id),
            "validate_href": swarm_path(
                request, "stack_validate_new", environment_id=environment_id
            ),
            "cancel_href": list_href,
        }
    elif kind == "edit":
        links = {
            "post_href": _stack_route(request, "stack_save", environment_id, namespace),
            "validate_href": _stack_route(request, "stack_validate", environment_id, namespace),
            "cancel_href": _detail_path(request, environment_id, namespace),
        }
    else:
        links = {
            "post_href": _stack_route(request, "stack_adopt_run", environment_id, namespace),
            "validate_href": _stack_route(
                request, "stack_validate_adopt", environment_id, namespace
            ),
            "cancel_href": _detail_path(request, environment_id, namespace),
        }
    return {
        "kind": kind,
        "form": state,
        "variable_cap": VARIABLE_CAP,
        "file_cap": FILE_CAP,
        "stack_name": namespace,
        "expected_digest": expected_digest,
        "catalogue": CATALOGUE,
        **links,
    }


async def _check(
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    form: StackForm,
    *,
    name: str,
    existing: bool = False,
    adopt: bool = False,
) -> tuple[ValidationReport | None, Problem | None]:
    """Validate the posted file and variables; return the report or the problem."""
    try:
        report = await stacks.swarm_planner.validate(
            actor,
            environment_id,
            name=name,
            compose_text=form.text(),
            variables=form.variable_inputs(),
            existing=existing,
            adopt=adopt,
            check_collisions=True,
            files=None if existing else form.file_inputs(),
        )
    except SWARM_ERRORS as error:
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
        "swarm_validation_result",
        problem.status if problem else 200,
        report=report,
        errors=problem.errors if problem else {},
        lines=problem.lines if problem else {},
        rejection=problem.rejection if problem else None,
        failure=problem.failure if problem else None,
        offers=offers,
        catalogue=CATALOGUE,
    )


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
        "swarm/stacks/new.html",
        {
            **_form_context(request, "new", environment_id, state),
            "environment": environment,
            "compose": _compose(stacks),
            "is_administrator": actor.is_administrator,
        },
        status_code,
    )


@router.get("/stacks/-/new", response_class=HTMLResponse, name="environment_swarm_stack_new")
async def new_stack(
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show the create form; create access is needed."""
    await swarm.stacks.create_context(actor, environment_id)
    return await _new_page(
        request,
        stacks,
        environments,
        actor,
        environment_id,
        FormState(rows=padded([]), file_rows=padded_files([])),
    )


@router.post(
    "/stacks/-/validate",
    response_class=HTMLResponse,
    name="environment_swarm_stack_validate_new",
)
async def validate_new(
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Check a new stack's file and variables; nothing is stored or run."""
    await swarm.stacks.create_context(actor, environment_id)
    form = await read_stack_form(request)
    report, problem = await _check(stacks, actor, environment_id, form, name=form.get("name"))
    offers = await offers_for_report(stacks, actor, environment_id, form, report, every_image=True)
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


@router.post("/stacks/-/new", response_class=HTMLResponse, name="environment_swarm_stack_create")
async def create_stack(
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Check the stack again, create it and start its first deployment as a job."""
    await swarm.stacks.create_context(actor, environment_id)
    form = await read_stack_form(request)
    if form.adding_rows():
        return await _new_page(
            request, stacks, environments, actor, environment_id, state_from(form)
        )
    try:
        text = form.text()
        variables = form.variable_inputs()
        files = form.file_inputs()
        choices = form.choices()
        with stacks.jobs.reserve(actor):
            created = await stacks.swarm_actions.create(
                actor,
                environment_id,
                name=form.get("name"),
                compose_text=text,
                variables=variables,
                choices=choices,
                files=files,
            )
    except SWARM_ERRORS as error:
        problem = problem_of(error, stacks, actor)
    else:
        name = created.name
        try:
            job_id = start_stack_job(
                request.app,
                actor,
                environment_id,
                "create",
                name,
                lambda services, progress: services.swarm_actions.deploy(
                    actor, environment_id, name, sink=progress, choices=choices
                ),
                stack_kind="swarm",
            )
        except TooManyStackJobs as busy:
            return problem_page(
                request,
                heading="Create stack",
                failure=Failure(
                    429,
                    "Too many operations are running",
                    f"{busy} The stack exists but was not deployed; redeploy it from its page.",
                ),
                back_href=_detail_path(request, environment_id, name),
                back_label="Open the stack",
                region=None,
            )
        return redirect(request, _job_path(request, environment_id, job_id))
    state = await problem_state(stacks, actor, environment_id, form, problem)
    return await _new_page(
        request, stacks, environments, actor, environment_id, state, problem.status
    )


@router.get(
    "/stacks/-/jobs/{job_id}", response_class=HTMLResponse, name="environment_swarm_stack_job"
)
async def job_page(
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
    job_id: JobId,
) -> Response:
    """Show the state of a stack operation; script follows it live, the page itself needs none."""
    job = stacks.jobs.view(job_id, actor)
    if job.environment_id != environment_id or job.stack_kind != "swarm":
        raise ResourceNotFound("not found")
    return page_response(
        request,
        "swarm/stacks/job.html",
        {
            "job": job,
            "list_href": _list_path(request, environment_id),
            "job_href": self_href(request),
            "stream_href": swarm_path(
                request, "stack_job_stream", environment_id=environment_id, job_id=job_id
            ),
        },
    )


def _frames(
    request: Request,
    jobs: StackJobRegistry,
    actor: Principal,
    environment_id: int,
    job_id: str,
    slot: StreamSlot,
) -> AsyncGenerator[str, None]:
    return job_frames(
        request,
        jobs,
        actor,
        environment_id,
        job_id,
        slot,
        stack_kind="swarm",
        template=JOB_PROGRESS,
        macro=JOB_MACRO,
    )


@router.get(
    "/stacks/-/jobs/{job_id}/events",
    response_class=StreamingResponse,
    name="environment_swarm_stack_job_stream",
)
async def job_stream(
    request: Request, actor: CurrentUser, environment_id: PathId, job_id: JobId
) -> Response:
    """Stream a stack operation as server-sent events; only its starter or an administrator."""
    jobs: StackJobRegistry = request.app.state.engine.stack_jobs
    slots: StreamSlots = request.app.state.engine.stream_slots
    first = jobs.view(job_id, actor)
    if first.environment_id != environment_id or first.stack_kind != "swarm":
        raise ResourceNotFound("not found")
    try:
        slot = slots.acquire(actor.user_id, is_administrator=actor.is_administrator)
    except TooManyStreams as refused:
        return no_store(
            render_partial(
                request,
                "error_state",
                title="Too many live views are open",
                message=str(refused),
                status_code=429,
            )
        )
    try:
        return JobResponse(slot, _frames(request, jobs, actor, environment_id, job_id, slot))
    except BaseException:
        slot.close()
        raise


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
    swarm: Swarm,
    stacks: StackServices,
    environments: Environments,
    actor: Principal,
    environment_id: int,
    namespace: str,
    state: FormState,
    status_code: int = 200,
) -> Response:
    environment = (await environments.environments.get(actor, environment_id)).summary
    try:
        found = await swarm.stacks.adopt_context(actor, environment_id, namespace)
    except UNAVAILABLE as unavailable:
        return _adopt_unavailable(request, unavailable, environment_id)
    return page_response(
        request,
        "swarm/stacks/adopt.html",
        {
            **_form_context(request, "adopt", environment_id, state, namespace=found.name),
            "environment": environment,
            "namespace": found,
            "compose": _compose(stacks),
            "read_href": _stack_route(request, "stack_read_cluster", environment_id, found.name),
        },
        status_code,
    )


@router.get(
    "/stacks/-/adopt/{namespace}", response_class=HTMLResponse, name="environment_swarm_stack_adopt"
)
async def adopt_form(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    environments: Environments,
    stash: HostReads,
    actor: CurrentUser,
    environment_id: PathId,
    read: Annotated[str | None, Query(pattern=TOKEN)] = None,
) -> Response:
    """Ask for the file of a namespace that is not managed here; administrators only.

    `read` names a result reconstructed from the cluster, which fills the form.
    """
    held = (
        None if read is None else stash.get(read, actor.user_id, environment_id, "swarm", namespace)
    )
    state = (
        FormState(name=namespace, rows=padded([]), file_rows=padded_files([]))
        if held is None
        else prefilled_state(namespace, held)
    )
    return await _adopt_page(
        request, swarm, stacks, environments, actor, environment_id, namespace, state
    )


@router.post(
    "/stacks/-/adopt/{namespace}/validate",
    response_class=HTMLResponse,
    name="environment_swarm_stack_validate_adopt",
)
async def validate_adopt(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Check the file for adopting a namespace; nothing is stored or run."""
    try:
        found = await swarm.stacks.adopt_context(actor, environment_id, namespace)
    except UNAVAILABLE as unavailable:
        return _adopt_unavailable(request, unavailable, environment_id)
    form = await read_stack_form(request)
    report, problem = await _check(stacks, actor, environment_id, form, name=found.name, adopt=True)
    offers = await offers_for_report(stacks, actor, environment_id, form, report, every_image=True)
    if is_htmx_request(request):
        return _validation_fragment(request, report, problem, offers)
    return await _adopt_page(
        request,
        swarm,
        stacks,
        environments,
        actor,
        environment_id,
        namespace,
        state_from(form, problem=problem, report=report, offers=offers, name=found.name),
        problem.status if problem else 200,
    )


@router.post(
    "/stacks/-/adopt/{namespace}",
    response_class=HTMLResponse,
    name="environment_swarm_stack_adopt_run",
)
async def adopt_run(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Record the namespace as a managed stack; the cluster is not changed."""
    try:
        found = await swarm.stacks.adopt_context(actor, environment_id, namespace)
    except UNAVAILABLE as unavailable:
        return _adopt_unavailable(request, unavailable, environment_id)
    form = await read_stack_form(request)
    if form.adding_rows():
        return await _adopt_page(
            request,
            swarm,
            stacks,
            environments,
            actor,
            environment_id,
            namespace,
            state_from(form, name=found.name),
        )
    try:
        await stacks.swarm_adopter.adopt(
            actor,
            environment_id,
            found.name,
            compose_text=form.text(),
            variables=form.variable_inputs(),
            files=form.file_inputs(),
        )
    except SWARM_ERRORS as error:
        problem = await located(problem_of(error, stacks, actor), form.display_text())
    else:
        return redirect(
            request,
            _detail_path(request, environment_id, found.name),
            [Flash(*DETAIL_NOTICES["adopted"])],
        )
    return await _adopt_page(
        request,
        swarm,
        stacks,
        environments,
        actor,
        environment_id,
        namespace,
        state_from(form, problem=problem, name=found.name),
        problem.status,
    )


async def _edit_page(
    request: Request,
    stacks: StackServices,
    environment_id: int,
    namespace: str,
    environment_name: str,
    state: FormState,
    expected_digest: str,
    status_code: int = 200,
) -> Response:
    return page_response(
        request,
        "swarm/stacks/edit.html",
        {
            **_form_context(
                request,
                "edit",
                environment_id,
                state,
                namespace=namespace,
                expected_digest=expected_digest,
            ),
            "environment_name": environment_name,
            "compose": _compose(stacks),
        },
        status_code,
    )


@router.get(
    "/stacks/{namespace}/edit", response_class=HTMLResponse, name="environment_swarm_stack_edit"
)
async def edit_form(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show the file and variables of a stack for editing; write access is needed."""
    edit = await swarm.stacks.edit_context(actor, environment_id, namespace)
    state = FormState(
        name=edit.stack.name,
        compose_text=edit.compose_file,
        rows=padded(rows_from_stored(edit.variables)),
    )
    return await _edit_page(
        request,
        stacks,
        environment_id,
        edit.stack.name,
        edit.environment_name,
        state,
        edit.compose_file_digest,
    )


@router.post(
    "/stacks/{namespace}/validate",
    response_class=HTMLResponse,
    name="environment_swarm_stack_validate",
)
async def validate_existing(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Check a changed file and variable set of an existing stack; nothing is stored or run."""
    edit = await swarm.stacks.edit_context(actor, environment_id, namespace)
    form = await read_stack_form(request)
    report, problem = await _check(
        stacks, actor, environment_id, form, name=edit.stack.name, existing=True
    )
    offers = await offers_for_report(stacks, actor, environment_id, form, report, every_image=True)
    if is_htmx_request(request):
        return _validation_fragment(request, report, problem, offers)
    state = state_from(
        form,
        problem=problem,
        report=report,
        offers=offers,
        name=edit.stack.name,
        stored_rows=rows_from_stored(edit.variables),
    )
    return await _edit_page(
        request,
        stacks,
        environment_id,
        edit.stack.name,
        edit.environment_name,
        state,
        form.get("expected_file_digest") or edit.compose_file_digest,
        problem.status if problem else 200,
    )


@router.post(
    "/stacks/{namespace}/edit", response_class=HTMLResponse, name="environment_swarm_stack_save"
)
async def save_stack(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Save the file and variables without deploying; optionally go on to the redeploy review."""
    edit = await swarm.stacks.edit_context(actor, environment_id, namespace)
    form = await read_stack_form(request)
    name = edit.stack.name
    stored = rows_from_stored(edit.variables)
    posted_digest = form.get("expected_file_digest") or edit.compose_file_digest
    if form.adding_rows():
        return await _edit_page(
            request,
            stacks,
            environment_id,
            name,
            edit.environment_name,
            state_from(form, name=name, stored_rows=stored),
            posted_digest,
        )
    try:
        updated = await stacks.swarm_actions.update(
            actor,
            environment_id,
            name,
            compose_text=form.text(),
            variables=form.variable_inputs(),
            expected_file_digest=form.get("expected_file_digest") or None,
        )
    except SWARM_ERRORS as error:
        problem = await located(problem_of(error, stacks, actor), form.display_text())
        return await _edit_page(
            request,
            stacks,
            environment_id,
            name,
            edit.environment_name,
            state_from(form, problem=problem, name=name, stored_rows=stored),
            posted_digest,
            problem.status,
        )
    if form.get("next") == "redeploy":
        return redirect(request, _redeploy_path(request, environment_id, name, orphans=False))
    return redirect(
        request,
        _detail_path(request, environment_id, name),
        [Flash(*DETAIL_NOTICES["saved" if updated.changed else "unchanged"])],
    )


async def _blocked_page(
    request: Request,
    stacks: StackServices,
    environments: Environments,
    actor: Principal,
    environment_id: int,
    namespace: str,
    form: StackForm,
    problem: Problem,
    *,
    status_code: int,
) -> Response:
    """Show why the redeploy cannot be reviewed yet, with the credential choices it needs."""
    rejection = problem.rejection
    if rejection is not None and rejection.reason == "restricted" and not rejection.findings:
        problem = Problem(
            problem.status,
            failure=Failure(problem.status, "You cannot change every object", MEMBERS_REFUSED),
        )
    offers, offers_error = await offers_for(
        stacks,
        actor,
        environment_id,
        problem.references,
        form.posted_choices(),
        problem.reference_errors,
    )
    environment = (await environments.environments.get(actor, environment_id)).summary
    failure = problem.failure
    if offers_error:
        failure = Failure(400, "Check the request", offers_error)
    return page_response(
        request,
        "swarm/stacks/redeploy_blocked.html",
        {
            "environment_name": environment.name,
            "stack": namespace,
            "problem": problem,
            "failure": failure,
            "offers": offers,
            "catalogue": CATALOGUE,
            "remove_orphans": form.flag("remove_orphans"),
            "compose": _compose(stacks),
            "detail_href": _detail_path(request, environment_id, namespace),
            "edit_href": _stack_route(request, "stack_edit", environment_id, namespace),
            "review_href": _stack_route(
                request, "stack_redeploy_review", environment_id, namespace
            ),
        },
        status_code,
    )


async def _redeploy_review(
    request: Request,
    swarm: Swarm,
    stacks: StackServices,
    environments: Environments,
    actor: Principal,
    environment_id: int,
    namespace: str,
    form: StackForm,
    *,
    posted: bool,
) -> Response:
    """Render the redeploy confirmation, or the page that says what stands in its way."""
    orphans = form.flag("remove_orphans")
    detail_href = _detail_path(request, environment_id, namespace)
    try:
        choices = form.choices()
        confirmation = await swarm.stacks.redeploy_confirmation(
            actor,
            environment_id,
            namespace,
            remove_orphans=orphans,
            choices=choices,
            check_collisions=True,
        )
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Redeploy stack", unavailable, detail_href, "Back to the stack", region=None
        )
    except SWARM_ERRORS as error:
        problem = problem_of(error, stacks, actor)
        credential = isinstance(error, StackRejected) and error.reason in CREDENTIAL_REASONS
        return await _blocked_page(
            request,
            stacks,
            environments,
            actor,
            environment_id,
            namespace,
            form,
            problem,
            status_code=problem.status if posted or not credential else 200,
        )
    return page_response(
        request,
        "swarm/stacks/redeploy_confirm.html",
        {
            "confirmation": confirmation,
            "echo": _echo(form),
            "compose": compose_notice(confirmation.compose),
            "detail_href": detail_href,
            "edit_href": _stack_route(request, "stack_edit", environment_id, namespace),
            "run_href": _stack_route(request, "stack_redeploy_run", environment_id, namespace),
            "with_orphans_href": _redeploy_path(
                request, environment_id, confirmation.stack, orphans=True
            ),
            "without_orphans_href": _redeploy_path(
                request, environment_id, confirmation.stack, orphans=False
            ),
        },
    )


@router.get(
    "/stacks/{namespace}/redeploy",
    response_class=HTMLResponse,
    name="environment_swarm_stack_redeploy",
)
async def redeploy_confirm(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    remove_orphans: str = "",
) -> Response:
    """Name every object a redeploy would create, replace or remove, grouped by action."""
    form = StackForm(scalars={"remove_orphans": remove_orphans})
    return await _redeploy_review(
        request, swarm, stacks, environments, actor, environment_id, namespace, form, posted=False
    )


@router.post(
    "/stacks/{namespace}/redeploy/review",
    response_class=HTMLResponse,
    name="environment_swarm_stack_redeploy_review",
)
async def redeploy_review(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Review the redeploy again with the credential choices just made."""
    form = await read_stack_form(request)
    return await _redeploy_review(
        request, swarm, stacks, environments, actor, environment_id, namespace, form, posted=True
    )


@router.post(
    "/stacks/{namespace}/redeploy",
    response_class=HTMLResponse,
    name="environment_swarm_stack_redeploy_run",
)
async def redeploy_run(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Redeploy exactly the reviewed plan as a job."""
    form = await read_stack_form(request)
    expected_plan = form.get("expected_plan")
    orphans = form.flag("remove_orphans")
    detail_href = _detail_path(request, environment_id, namespace)
    review = _redeploy_path(request, environment_id, namespace, orphans=orphans)

    def refuse(failure: Failure, environment_name: str = "") -> Response:
        return problem_page(
            request,
            heading="Redeploy stack",
            failure=failure,
            back_href=detail_href,
            back_label="Back to the stack",
            review_href=review,
            environment_name=environment_name,
            region=None,
        )

    try:
        choices = form.choices()
        confirmation = await swarm.stacks.redeploy_confirmation(
            actor,
            environment_id,
            namespace,
            remove_orphans=orphans,
            choices=choices,
            check_collisions=True,
        )
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Redeploy stack", unavailable, detail_href, "Back to the stack", region=None
        )
    except SWARM_ERRORS as error:
        problem = problem_of(error, stacks, actor)
        if isinstance(error, StackRejected) and error.reason in CREDENTIAL_REASONS:
            return await _blocked_page(
                request,
                stacks,
                environments,
                actor,
                environment_id,
                namespace,
                form,
                problem,
                status_code=problem.status,
            )
        if isinstance(error, ValidationFailed) and not isinstance(error, StackRejected):
            return refuse(failure_of(problem))
        recorded = await _refused(stacks, actor, environment_id, namespace, "redeploy", error)
        return await _blocked_page(
            request,
            stacks,
            environments,
            actor,
            environment_id,
            namespace,
            form,
            recorded,
            status_code=recorded.status,
        )
    name = confirmation.stack
    if (
        form.get("expected_file_digest") != confirmation.expected_file_digest
        or form.get("expected_inputs_digest") != confirmation.expected_inputs_digest
        or expected_plan != confirmation.expected_plan
    ):
        stale = await _refused(
            stacks, actor, environment_id, name, "redeploy", ChangedSinceReview()
        )
        return refuse(failure_of(stale), confirmation.environment_name)
    busy = await _busy_refusal(stacks, actor, environment_id, name, "redeploy")
    if busy is not None:
        return refuse(busy, confirmation.environment_name)
    try:
        job_id = start_stack_job(
            request.app,
            actor,
            environment_id,
            "redeploy",
            name,
            lambda services, progress: services.swarm_actions.redeploy(
                actor,
                environment_id,
                name,
                expected_file_digest=confirmation.expected_file_digest,
                expected_inputs_digest=confirmation.expected_inputs_digest,
                expected_plan=confirmation.expected_plan,
                remove_orphans=orphans,
                sink=progress,
                choices=choices,
            ),
            stack_kind="swarm",
        )
    except TooManyStackJobs as busy_jobs:
        return refuse(
            Failure(429, "Too many operations are running", str(busy_jobs)),
            confirmation.environment_name,
        )
    return redirect(request, _job_path(request, environment_id, job_id))


@router.get(
    "/stacks/{namespace}/remove", response_class=HTMLResponse, name="environment_swarm_stack_remove"
)
async def remove_confirm(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Name every service, network, config and secret a remove takes away; volumes stay."""
    detail_href = _detail_path(request, environment_id, namespace)
    try:
        confirmation = await swarm.stacks.remove_confirmation(actor, environment_id, namespace)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Remove stack", unavailable, detail_href, "Back to the stack", region=None
        )
    except SWARM_ERRORS as error:
        return problem_page(
            request,
            heading="Remove stack",
            failure=failure_of(problem_of(error, stacks, actor)),
            back_href=detail_href,
            back_label="Back to the stack",
            region=None,
        )
    return page_response(
        request,
        "swarm/stacks/remove_confirm.html",
        {
            "confirmation": confirmation,
            "detail_href": detail_href,
            "run_href": _stack_route(request, "stack_remove_run", environment_id, namespace),
        },
    )


@router.post(
    "/stacks/{namespace}/remove",
    response_class=HTMLResponse,
    name="environment_swarm_stack_remove_run",
)
async def remove_run(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Remove exactly the reviewed members as a job."""
    form = await read_stack_form(request)
    expected_members = form.get("expected_members")
    detail_href = _detail_path(request, environment_id, namespace)
    review = _stack_route(request, "stack_remove", environment_id, namespace)

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

    try:
        confirmation = await swarm.stacks.remove_confirmation(actor, environment_id, namespace)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Remove stack", unavailable, detail_href, "Back to the stack", region=None
        )
    except SWARM_ERRORS as error:
        return refuse(failure_of(problem_of(error, stacks, actor)))
    name = confirmation.stack
    environment_name = confirmation.environment_name
    if (
        form.get("expected_stack_id") != str(confirmation.stack_id)
        or expected_members != confirmation.expected_members
    ):
        stale = await _refused(stacks, actor, environment_id, name, "remove", ChangedSinceReview())
        return refuse(failure_of(stale), environment_name)
    if confirmation.unwritable_members:
        blocked = StackRejected({"stack": UNWRITABLE_MEMBERS}, "restricted")
        await _refused(stacks, actor, environment_id, name, "remove", blocked)
        return refuse(
            Failure(403, "You cannot change every object", MEMBERS_REFUSED), environment_name
        )
    busy = await _busy_refusal(stacks, actor, environment_id, name, "remove")
    if busy is not None:
        return refuse(busy, environment_name)
    try:
        job_id = start_stack_job(
            request.app,
            actor,
            environment_id,
            "remove",
            name,
            lambda services, progress: services.swarm_actions.remove(
                actor,
                environment_id,
                name,
                stack_id=confirmation.stack_id,
                expected_members=confirmation.expected_members,
                sink=progress,
            ),
            stack_kind="swarm",
        )
    except TooManyStackJobs as busy_jobs:
        return refuse(
            Failure(429, "Too many operations are running", str(busy_jobs)), environment_name
        )
    return redirect(request, _job_path(request, environment_id, job_id))


@router.get(
    "/stacks/{namespace}/forget", response_class=HTMLResponse, name="environment_swarm_stack_forget"
)
async def forget_confirm(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Say what forgetting leaves on the cluster; administrators only."""
    confirmation = await swarm.stacks.forget_confirmation(actor, environment_id, namespace)
    return page_response(
        request,
        "swarm/stacks/forget_confirm.html",
        {
            "confirmation": confirmation,
            "detail_href": _detail_path(request, environment_id, confirmation.stack),
            "run_href": _stack_route(request, "stack_forget_run", environment_id, namespace),
        },
    )


@router.post(
    "/stacks/{namespace}/forget",
    response_class=HTMLResponse,
    name="environment_swarm_stack_forget_run",
)
async def forget_run(
    namespace: Namespace,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Forget the stack without touching the cluster; administrators only."""
    form = await read_stack_form(request)
    try:
        await stacks.swarm_actions.forget(
            actor,
            environment_id,
            namespace,
            stack_id=posted_stack_id(form.get("expected_stack_id")),
        )
    except SWARM_ERRORS as error:
        return problem_page(
            request,
            heading="Forget stack",
            failure=failure_of(problem_of(error, stacks, actor)),
            back_href=_detail_path(request, environment_id, namespace),
            back_label="Back to the stack",
            review_href=_stack_route(request, "stack_forget", environment_id, namespace),
            region=None,
        )
    return redirect(
        request, _list_path(request, environment_id), [Flash("ok", LIST_NOTICES["forgotten"])]
    )


def _files_path(request: Request, environment_id: int, namespace: str) -> str:
    return f"{_detail_path(request, environment_id, namespace)}?{query_of('tab', FILES_TAB)}"


def _file_unavailable(
    request: Request, error: Exception, environment_id: int, namespace: str
) -> Response:
    return unavailable_page(
        request,
        "Stack file",
        error,
        _files_path(request, environment_id, namespace),
        "Back to the files",
        region=None,
    )


def _file_page(
    request: Request,
    stacks: StackServices,
    environment_id: int,
    namespace: str,
    template: str,
    context: dict[str, object],
    status_code: int = 200,
) -> Response:
    return page_response(
        request,
        template,
        {
            "stack_name": namespace,
            "swarm": True,
            "compose": _compose(stacks),
            "detail_href": _files_path(request, environment_id, namespace),
            **context,
        },
        status_code,
    )


def _file_form_page(
    request: Request,
    stacks: StackServices,
    environment_id: int,
    namespace: str,
    environment_name: str,
    digest: str,
    state: FileState,
    status_code: int = 200,
    *,
    file: str | None = None,
    stored_secret: bool = False,
) -> Response:
    if file is None:
        post_href = _stack_route(request, "stack_file_create", environment_id, namespace)
    else:
        post_href = swarm_path(
            request,
            "stack_file_save",
            environment_id=environment_id,
            namespace=namespace,
            file=file,
        )
    return _file_page(
        request,
        stacks,
        environment_id,
        namespace,
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
    "/stacks/{namespace}/files/-/new",
    response_class=HTMLResponse,
    name="environment_swarm_stack_file_new",
)
async def new_file(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show the form that adds one companion file; write access is needed."""
    try:
        form = await swarm.stacks.file_form(actor, environment_id, namespace, None)
    except UNAVAILABLE as unavailable:
        return _file_unavailable(request, unavailable, environment_id, namespace)
    return _file_form_page(
        request,
        stacks,
        environment_id,
        namespace,
        form.environment_name,
        form.inputs_digest,
        FileState("", "", False),
    )


async def _exists(
    swarm: SwarmServices, actor: Principal, environment_id: int, namespace: str, file: str
) -> bool:
    try:
        await swarm.stacks.file_form(actor, environment_id, namespace, file)
    except ResourceNotFound:
        return False
    return True


@router.post(
    "/stacks/{namespace}/files/-/new",
    response_class=HTMLResponse,
    name="environment_swarm_stack_file_create",
)
async def create_file(
    namespace: Namespace,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Add one companion file, checking the whole stack with it; nothing is deployed."""
    try:
        form = await swarm.stacks.file_form(actor, environment_id, namespace, None)
    except UNAVAILABLE as unavailable:
        return _file_unavailable(request, unavailable, environment_id, namespace)
    submission = await read_submission(request, fixed_name=None, keeps=False)
    errors = dict(submission.errors)
    if "name" not in errors and await _exists(
        swarm, actor, environment_id, namespace, submission.name
    ):
        errors["name"] = "A file with this name is already added. Edit it instead."
    if errors:
        return _file_form_page(
            request,
            stacks,
            environment_id,
            namespace,
            form.environment_name,
            submission.expected_inputs_digest,
            state_of_submission(replace(submission, errors=errors)),
            400,
        )
    try:
        await stacks.swarm_actions.write_file(
            actor,
            environment_id,
            namespace,
            submission.name,
            content=submission.content,
            secret=submission.secret,
            expected_inputs_digest=submission.expected_inputs_digest,
        )
    except SWARM_ERRORS as error:
        problem = problem_of(error, stacks, actor)
        return _file_form_page(
            request,
            stacks,
            environment_id,
            namespace,
            form.environment_name,
            submission.expected_inputs_digest,
            state_of_refusal(submission, problem, error, submission.name),
            problem.status,
        )
    return redirect(
        request, _files_path(request, environment_id, namespace), [Flash(*FILE_NOTICES["added"])]
    )


@router.get(
    "/stacks/{namespace}/files/{file}/edit",
    response_class=HTMLResponse,
    name="environment_swarm_stack_file_edit",
)
async def edit_file(
    namespace: Namespace,
    file: FileName,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show one companion file for editing; write access is needed."""
    try:
        form = await swarm.stacks.file_form(actor, environment_id, namespace, file)
    except UNAVAILABLE as unavailable:
        return _file_unavailable(request, unavailable, environment_id, namespace)
    stored = form.file
    if stored is None:
        raise ResourceNotFound("not found")
    return _file_form_page(
        request,
        stacks,
        environment_id,
        namespace,
        form.environment_name,
        form.inputs_digest,
        FileState(stored.name, stored.content, stored.secret),
        file=file,
        stored_secret=stored.secret,
    )


@router.post(
    "/stacks/{namespace}/files/{file}/edit",
    response_class=HTMLResponse,
    name="environment_swarm_stack_file_save",
)
async def save_file(
    namespace: Namespace,
    file: FileName,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Replace one companion file, checking the whole stack with it; nothing is deployed."""
    try:
        form = await swarm.stacks.file_form(actor, environment_id, namespace, file)
    except UNAVAILABLE as unavailable:
        return _file_unavailable(request, unavailable, environment_id, namespace)
    stored = form.file
    if stored is None:
        raise ResourceNotFound("not found")
    submission = await read_submission(request, fixed_name=file, keeps=stored.secret)
    if submission.errors:
        return _file_form_page(
            request,
            stacks,
            environment_id,
            namespace,
            form.environment_name,
            submission.expected_inputs_digest,
            state_of_submission(submission),
            400,
            file=file,
            stored_secret=stored.secret,
        )
    try:
        await stacks.swarm_actions.write_file(
            actor,
            environment_id,
            namespace,
            file,
            content=submission.content,
            secret=submission.secret,
            expected_inputs_digest=submission.expected_inputs_digest,
        )
    except SWARM_ERRORS as error:
        problem = problem_of(error, stacks, actor)
        return _file_form_page(
            request,
            stacks,
            environment_id,
            namespace,
            form.environment_name,
            submission.expected_inputs_digest,
            state_of_refusal(submission, problem, error, file),
            problem.status,
            file=file,
            stored_secret=stored.secret,
        )
    return redirect(
        request, _files_path(request, environment_id, namespace), [Flash(*FILE_NOTICES["saved"])]
    )


@router.get(
    "/stacks/{namespace}/files/{file}/remove",
    response_class=HTMLResponse,
    name="environment_swarm_stack_file_remove",
)
async def remove_file_confirm(
    namespace: Namespace,
    file: FileName,
    request: Request,
    swarm: Swarm,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Name the file a remove deletes and where it is still used; write access is needed."""
    try:
        confirmation = await swarm.stacks.file_remove_confirmation(
            actor, environment_id, namespace, file
        )
    except UNAVAILABLE as unavailable:
        return _file_unavailable(request, unavailable, environment_id, namespace)
    return _file_page(
        request,
        stacks,
        environment_id,
        namespace,
        "stacks/file_remove_confirm.html",
        {
            "confirmation": confirmation,
            "run_href": swarm_path(
                request,
                "stack_file_remove_run",
                environment_id=environment_id,
                namespace=namespace,
                file=file,
            ),
            "edit_href": swarm_path(
                request,
                "stack_file_edit",
                environment_id=environment_id,
                namespace=namespace,
                file=file,
            ),
            "stack_edit_href": _stack_route(request, "stack_edit", environment_id, namespace),
        },
    )


@router.post(
    "/stacks/{namespace}/files/{file}/remove",
    response_class=HTMLResponse,
    name="environment_swarm_stack_file_remove_run",
)
async def remove_file_run(
    namespace: Namespace,
    file: FileName,
    request: Request,
    stacks: Stacks,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Delete one companion file, checking the whole stack without it; nothing is deployed."""
    form = await read_stack_form(request)
    review = swarm_path(
        request,
        "stack_file_remove",
        environment_id=environment_id,
        namespace=namespace,
        file=file,
    )
    try:
        await stacks.swarm_actions.remove_file(
            actor,
            environment_id,
            namespace,
            file,
            expected_inputs_digest=form.get("expected_inputs_digest") or "",
        )
    except SWARM_ERRORS as error:
        problem = problem_of(error, stacks, actor)
        return problem_page(
            request,
            heading="Remove file",
            failure=refusal_failure(problem, error),
            back_href=_files_path(request, environment_id, namespace),
            back_label="Back to the files",
            review_href=review,
            region=None,
        )
    return redirect(
        request, _files_path(request, environment_id, namespace), [Flash(*FILE_NOTICES["removed"])]
    )
