"""Container create, duplicate and recreate pages."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Annotated, Any, Final
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Path, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import PermissionDenied, ResourceNotFound, SelfProtected, ValidationFailed
from app.auth.principal import Principal
from app.self_identity import source_of
from app.services.assembly import Containers, ContainerServices
from app.services.container_actions import (
    BusyView,
    ContainerGone,
    ContainerStateConflict,
)
from app.services.container_jobs import start_container_create_job, start_container_recreate_job
from app.services.container_provisioning import (
    START_FAILED,
    CreateRefused,
    NameChanged,
    NameInUse,
    NotRecreatable,
    PullFailed,
    RecreateFailed,
    RestrictedOptions,
    SubmissionRejected,
)
from app.services.container_recreate import PARKED_INFIX
from app.services.container_review import (
    CHANGE_CATEGORIES,
    CONTROL_COPIED,
    CONTROL_MISSING,
    NO_CHANGES,
    OLD_KEPT,
    OLD_UNKNOWN,
    UNREADABLE_CHANGES,
    FormContext,
    RecreateOutcome,
    ReviewView,
    created_flags,
    recreated_flags,
)
from app.services.container_spec import (
    ADMIN_ONLY_FIELDS,
    ADMIN_ONLY_GROUPS,
    ADMIN_ONLY_MOUNT_TYPES,
    ADMIN_ONLY_NETWORK_MODES,
    GROUPS,
    ROW_CAPS,
    FormState,
)
from app.services.image_bindings import CredentialOffer, RebindRefused
from app.services.image_jobs import TooManyJobs
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectBusy
from app.services.pull_credentials import CHOICE_FIELD, BindingCredentialMissing
from app.services.registry_resolution import CredentialSetUnavailable
from app.web.identifiers import PathId
from app.web.image_helpers import credential_choice_of, selected_choice
from app.web.navigation import no_store, redirect
from app.web.object_pages import administrator_detail, busy_message
from app.web.problems import unreachable_copy
from app.web.registry_providers import CATALOGUE
from app.web.results import show_result
from app.web.template_forms import (
    RequestTemplate,
    bound_ref,
    container_panel,
    container_values,
)
from app.web.templating import is_htmx_request, render_fragment, render_page

router = APIRouter(
    prefix="/environments/{environment_id}/containers",
    tags=["containers"],
    dependencies=[Depends(require_user)],
)

SOURCE_MAX = 128
SEGMENT_MAX = 128
NAME_MAX = 256
FLAG_MAX = 8
CHOICE_MAX = 20
IMAGE_MAX = 512
CHECKED = "on"
INTENT_EDIT = "edit"
FORM_TEMPLATE = "containers/create/form.html"
REVIEW_TEMPLATE = "containers/create/review.html"
CHOICES_TEMPLATE = "containers/create/_choices.html"
CONFIRM_TEMPLATE = "containers/create/recreate_confirm.html"
RESULT_TEMPLATE = "containers/create/recreate_result.html"
PROBLEM_TEMPLATE = "containers/problem.html"
CREATED_FLAGS: Final = {
    "started": ("ok", "Container created and started."),
    "stopped": ("ok", "Container created. It was not started."),
    "start_failed": ("warn", START_FAILED),
}
RECREATED_FLAGS: Final = {
    "started": "Container recreated and started.",
    "stopped": "Container recreated. It was left stopped, as the original was.",
}
CHANGE_LABELS: Final = {
    "image": "Image",
    "command": "Command",
    "entrypoint": "Entrypoint",
    "env": "Environment variables",
    "labels": "Labels",
    "ports": "Ports",
    "mounts": "Mounts",
    "networks": "Networks",
    "restart_policy": "Restart policy",
    "resources": "Resources",
    "healthcheck": "Health check",
    "user": "User",
    "working_dir": "Working directory",
    "log_config": "Logging",
    "other": "Other settings",
    NO_CHANGES: "No configuration changed",
    UNREADABLE_CHANGES: "Not determined: the new container could not be read",
}
UNKNOWN_RESULT: Final = "The result of the recreate is not known"
ADMIN_ONLY = {
    "fields": ADMIN_ONLY_FIELDS,
    "groups": ADMIN_ONLY_GROUPS,
    "mount_types": ADMIN_ONLY_MOUNT_TYPES,
    "network_modes": ADMIN_ONLY_NETWORK_MODES,
}

Source = Annotated[str | None, Query(alias="from", max_length=SOURCE_MAX)]
ContainerPath = Annotated[str, Path(max_length=SEGMENT_MAX)]


@dataclass(frozen=True, slots=True)
class Notice:
    level: str
    message: str


@dataclass(frozen=True, slots=True)
class _Banner:
    title: str
    message: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class _Failure:
    status: int
    title: str
    message: str
    busy: BusyView | None = None
    outcome_unknown: bool = False
    detail: str | None = None


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


def _path(request: Request, name: str, **params: Any) -> str:
    return str(request.app.url_path_for(name, **params))


def _recreated_query(outcome: RecreateOutcome) -> str:
    return urlencode(recreated_flags(outcome), safe=",")


def creation_notices(params: Mapping[str, str]) -> tuple[Notice, ...]:
    """Turn the flags of a create or recreate redirect into notices; unknown values are dropped."""
    notices: list[Notice] = []
    control = params.get("control")
    created = CREATED_FLAGS.get(params.get("created", ""))
    if created is not None:
        notices.append(Notice(*created))
        if control == CONTROL_MISSING:
            notices.append(Notice("warn", "Created, but its access settings could not be saved."))
        return tuple(notices)
    headline = RECREATED_FLAGS.get(params.get("recreated", ""))
    if headline is None:
        return ()
    if params.get("pulled") == "1":
        headline += " The image was pulled again."
    notices.append(Notice("ok", headline))
    changed = [
        CHANGE_LABELS[name]
        for name in params.get("changed", "").split(",")
        if name in CHANGE_CATEGORIES or name in (NO_CHANGES, UNREADABLE_CHANGES)
    ]
    if changed:
        notices.append(Notice("info", "Changed: " + "; ".join(changed) + "."))
    old = params.get("old")
    if old == OLD_KEPT:
        kept = (
            f"The original container was not removed. It is still on the host under a name "
            f"containing {PARKED_INFIX}"
        )
        if control == CONTROL_COPIED:
            kept += ", with the same access settings as the new container"
        notices.append(Notice("warn", kept + ". Remove it when it is no longer needed."))
    elif old == OLD_UNKNOWN:
        notices.append(
            Notice(
                "warn",
                "Docker did not confirm whether the original container was removed. "
                "Check the container list.",
            )
        )
    if control == CONTROL_MISSING:
        notices.append(
            Notice(
                "warn",
                "Recreated, but its access settings could not be moved. "
                "An administrator can review them on its Access tab.",
            )
        )
    return tuple(notices)


def _unavailable_banner(error: EnvironmentUnavailable | ObjectTooLarge) -> _Banner:
    if isinstance(error, EnvironmentUnavailable) and error.kind == "outcome_unknown":
        return _Banner(
            "The result of the request is not known",
            f"{error} Check the container list before submitting again.",
        )
    return _Banner("The container was not created", str(error))


def _problem(
    request: Request,
    *,
    heading: str,
    title: str,
    message: str,
    back_href: str,
    back_label: str,
    status_code: int,
) -> Response:
    return _page(
        request,
        PROBLEM_TEMPLATE,
        {
            "environment_name": "",
            "heading": heading,
            "title": title,
            "message": message,
            "back_href": back_href,
            "back_label": back_label,
            "retry_href": request.url.path,
        },
        status_code,
    )


async def _read_form(request: Request) -> dict[str, Any]:
    return dict((await request.form()).items())


def _text(form: dict[str, Any], name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _source_of(form: dict[str, Any]) -> str | None:
    value = _text(form, "from")[:SOURCE_MAX].strip()
    return value or None


def _carried_choice(form: dict[str, Any]) -> str:
    """Return the submitted credential choice if it is one a form could have posted, else blank."""
    value = _text(form, CHOICE_FIELD)[:CHOICE_MAX].strip()
    try:
        return value if credential_choice_of(value) is not None else ""
    except ValidationFailed:
        return ""


async def _offer(
    containers: ContainerServices, actor: Principal, environment_id: int, image: str
) -> CredentialOffer | None:
    """Describe the credential choice for a recreate pull, or None when none can be asked."""
    try:
        return await containers.bindings.offer(actor, environment_id, image)
    except (ValidationFailed, PermissionDenied):
        return None


async def _picker(
    containers: ContainerServices, actor: Principal, environment_id: int, image: str
) -> CredentialOffer | None:
    """Describe the credential picker for a pull of the image, or None when none can be shown."""
    if not image.strip():
        return None
    try:
        return await containers.bindings.picker(actor, environment_id, image.strip())
    except (ValidationFailed, PermissionDenied, ResourceNotFound):
        return None


async def _review_page(
    request: Request,
    containers: ContainerServices,
    actor: Principal,
    environment_id: int,
    form: dict[str, Any],
    review: ReviewView,
    *,
    status_code: int = 200,
    error: str | None = None,
    fields: dict[str, str] | None = None,
) -> Response:
    offer = (
        await _picker(containers, actor, environment_id, review.image) if review.will_pull else None
    )
    return _page(
        request,
        REVIEW_TEMPLATE,
        {
            "review": review,
            "source_id": _source_of(form),
            "template_ref": bound_ref(request),
            "offer": offer,
            "selected": (
                "" if offer is None else selected_choice(offer, _text(form, CHOICE_FIELD))
            ),
            "choice_error": (fields or {}).get(CHOICE_FIELD),
            "error": error,
            "catalogue": CATALOGUE,
            "pull_href": _path(request, "environment_image_pull", environment_id=environment_id),
            "create_href": _path(
                request, "environment_container_create", environment_id=environment_id
            ),
            "edit_href": _path(
                request, "environment_container_new_submit", environment_id=environment_id
            ),
            "list_href": _path(
                request, "environment_container_list", environment_id=environment_id
            ),
        },
        status_code,
    )


async def _context(
    containers: ContainerServices,
    actor: Principal,
    environment_id: int,
    source_id: str | None,
    *,
    strict: bool,
) -> FormContext:
    """Describe the form; a source that cannot be read again costs only its notices."""
    creation = containers.creation
    if source_id is None:
        return await creation.form_context(actor, environment_id)
    try:
        return await creation.form_context(actor, environment_id, source_id=source_id)
    except (ResourceNotFound, PermissionDenied, EnvironmentUnavailable, ObjectTooLarge):
        if strict:
            raise
        blank = await creation.form_context(actor, environment_id)
        return replace(blank, duplicate_of=source_id)


def _form_page(
    request: Request,
    context: FormContext,
    state: FormState,
    *,
    status_code: int = 200,
    banner: _Banner | None = None,
    carried_choice: str = "",
    offer: CredentialOffer | None = None,
) -> Response:
    environment_id = context.environment_id
    return _page(
        request,
        FORM_TEMPLATE,
        {
            "context": context,
            "carried_choice": carried_choice,
            "template_panel": container_panel(request, is_administrator=context.is_administrator),
            "template_ref": bound_ref(request),
            "offer": offer,
            "catalogue": CATALOGUE,
            "selected": "" if offer is None else selected_choice(offer, carried_choice),
            "choices_href": _path(
                request, "environment_container_new_choices", environment_id=environment_id
            ),
            "state": state,
            "blank": FormState(),
            "banner": banner,
            "engine_detail": None if banner is None else banner.detail,
            "is_administrator": context.is_administrator,
            "admin_only": ADMIN_ONLY,
            "row_caps": ROW_CAPS,
            "environment_id": environment_id,
            "submit_href": _path(
                request, "environment_container_new_submit", environment_id=environment_id
            ),
            "list_href": _path(
                request, "environment_container_list", environment_id=environment_id
            ),
        },
        status_code,
    )


async def _rendered_form(
    request: Request,
    containers: ContainerServices,
    actor: Principal,
    environment_id: int,
    form: dict[str, Any],
    state: FormState,
    *,
    status_code: int = 200,
    banner: _Banner | None = None,
) -> Response:
    context = await _context(containers, actor, environment_id, _source_of(form), strict=False)
    return _form_page(
        request,
        context,
        state,
        status_code=status_code,
        banner=banner,
        carried_choice=_carried_choice(form),
        offer=await _picker(containers, actor, environment_id, state.get("image")),
    )


async def _review_again(
    request: Request,
    containers: ContainerServices,
    actor: Principal,
    environment_id: int,
    form: dict[str, Any],
    status_code: int,
    *,
    error: str | None = None,
    fields: dict[str, str] | None = None,
) -> Response:
    """Show the review again after a refusal that the credential choice can answer."""
    try:
        review = await containers.creation.review(actor, environment_id, form)
    except SubmissionRejected as rejected:
        state = rejected.state or FormState.from_form(form).with_errors(rejected.fields)
        return await _rendered_form(
            request, containers, actor, environment_id, form, state, status_code=400
        )
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return await _rendered_form(
            request,
            containers,
            actor,
            environment_id,
            form,
            FormState.from_form(form),
            status_code=503,
            banner=_Banner(*unreachable_copy(unavailable)),
        )
    return await _review_page(
        request,
        containers,
        actor,
        environment_id,
        form,
        review,
        status_code=status_code,
        error=error,
        fields=fields,
    )


@router.get(
    "/new",
    response_class=HTMLResponse,
    name="environment_container_new",
    dependencies=[RequestTemplate],
)
async def new_container(
    request: Request,
    containers: Containers,
    actor: CurrentUser,
    environment_id: PathId,
    source: Source = None,
) -> Response:
    """Show the create form, blank or prefilled from an existing container or a template."""
    if source is not None:
        request.state.template = None
    try:
        context = await _context(containers, actor, environment_id, source, strict=True)
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return _problem(
            request,
            heading="Create container",
            title=unreachable_copy(unavailable).title,
            message=unreachable_copy(unavailable).message,
            back_href=_path(request, "environment_container_list", environment_id=environment_id),
            back_label="Back to containers",
            status_code=503,
        )
    state = context.state
    values = container_values(request, is_administrator=context.is_administrator)
    if values is not None:
        state = FormState.from_form({**state.values, **values})
    return _form_page(
        request,
        context,
        state,
        offer=await _picker(containers, actor, environment_id, state.get("image")),
    )


@router.post("/new/choices", response_class=HTMLResponse, name="environment_container_new_choices")
async def image_choices(
    request: Request, containers: Containers, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Render the credential picker for the image typed in the create form; nothing is recorded."""
    await containers.creation.form_context(actor, environment_id)
    if not is_htmx_request(request):
        return no_store(
            RedirectResponse(
                _path(request, "environment_container_new", environment_id=environment_id),
                status_code=303,
            )
        )
    form = await _read_form(request)
    image = _text(form, "image")[:IMAGE_MAX].strip()
    offer: CredentialOffer | None = None
    image_error: str | None = None
    status_code = 200
    if image:
        try:
            offer = await containers.bindings.picker(actor, environment_id, image)
        except ValidationFailed as invalid:
            image_error, status_code = "; ".join(invalid.fields.values()), 400
    return render_fragment(
        request,
        CHOICES_TEMPLATE,
        "image_choices",
        status_code=status_code,
        offer=offer,
        selected="" if offer is None else selected_choice(offer, _text(form, CHOICE_FIELD)),
        image_error=image_error,
        pull_policy=_text(form, "pull"),
        catalogue=CATALOGUE,
    )


@router.post(
    "/new",
    response_class=HTMLResponse,
    name="environment_container_new_submit",
    dependencies=[RequestTemplate],
)
async def submit_form(
    request: Request, containers: Containers, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Add blank rows, return to editing, or validate and show the review."""
    form = await _read_form(request)
    group = _text(form, "rows")
    if group in GROUPS:
        state = FormState.from_form(form).add_rows(group)
        return await _rendered_form(request, containers, actor, environment_id, form, state)
    if _text(form, "intent") == INTENT_EDIT:
        state = FormState.from_form(form)
        return await _rendered_form(request, containers, actor, environment_id, form, state)
    try:
        review = await containers.creation.review(actor, environment_id, form)
    except SubmissionRejected as rejected:
        state = rejected.state or FormState.from_form(form).with_errors(rejected.fields)
        return await _rendered_form(
            request, containers, actor, environment_id, form, state, status_code=400
        )
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return await _rendered_form(
            request,
            containers,
            actor,
            environment_id,
            form,
            FormState.from_form(form),
            status_code=503,
            banner=_Banner(*unreachable_copy(unavailable)),
        )
    return await _review_page(request, containers, actor, environment_id, form, review)


@router.post(
    "",
    response_class=HTMLResponse,
    name="environment_container_create",
    dependencies=[RequestTemplate],
)
async def create_container(
    request: Request, containers: Containers, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Create the container; a refusal returns to the form with every value kept."""
    form = await _read_form(request)
    try:
        choice = credential_choice_of(_text(form, CHOICE_FIELD))
        if await containers.creation.prepare_create(
            actor, environment_id, form, credential_choice=choice
        ):
            job_id = start_container_create_job(
                request.app,
                actor,
                environment_id,
                {name: value for name, value in form.items() if isinstance(value, str)},
                duplicate_of=_source_of(form),
                credential_choice=choice,
                template=bound_ref(request),
            )
            return no_store(
                redirect(
                    request,
                    _path(
                        request,
                        "environment_container_job",
                        environment_id=environment_id,
                        job_id=job_id,
                    ),
                )
            )
        outcome = await containers.creation.create(
            actor,
            environment_id,
            form,
            duplicate_of=_source_of(form),
            credential_choice=choice,
            template=bound_ref(request),
        )
    except SubmissionRejected as rejected:
        state = rejected.state or FormState.from_form(form).with_errors(rejected.fields)
        return await _rendered_form(
            request, containers, actor, environment_id, form, state, status_code=400
        )
    except (BindingCredentialMissing, CredentialSetUnavailable) as refused:
        return await _review_again(
            request, containers, actor, environment_id, form, 409, error=str(refused)
        )
    except TooManyJobs as busy:
        return await _review_again(
            request, containers, actor, environment_id, form, 429, error=str(busy)
        )
    except RebindRefused as denied:
        return await _review_again(
            request,
            containers,
            actor,
            environment_id,
            form,
            403,
            fields={CHOICE_FIELD: str(denied)},
        )
    except ObjectBusy as busy:
        return await _review_again(
            request,
            containers,
            actor,
            environment_id,
            form,
            409,
            error=busy_message(
                containers.actions.describe_busy(busy, actor),
                "the image or volume this container uses",
            ),
        )
    except ValidationFailed as invalid:
        if CHOICE_FIELD in invalid.fields:
            return await _review_again(
                request, containers, actor, environment_id, form, 400, fields=invalid.fields
            )
        return await _rendered_form(
            request,
            containers,
            actor,
            environment_id,
            form,
            FormState.from_form(form),
            status_code=400,
            banner=_Banner(
                "Check the container's configuration", " ".join(invalid.fields.values())
            ),
        )
    except CreateRefused as refused:
        state = FormState.from_form(form)
        if refused.field_name is None:
            return await _rendered_form(
                request,
                containers,
                actor,
                environment_id,
                form,
                state,
                status_code=400,
                banner=_Banner(
                    "Docker refused this configuration",
                    str(refused),
                    administrator_detail(refused, actor),
                ),
            )
        status = 409 if isinstance(refused, NameInUse) else 502
        return await _rendered_form(
            request,
            containers,
            actor,
            environment_id,
            form,
            state.with_errors({refused.field_name: str(refused)}),
            status_code=status,
        )
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return await _rendered_form(
            request,
            containers,
            actor,
            environment_id,
            form,
            FormState.from_form(form),
            status_code=503,
            banner=_unavailable_banner(unavailable),
        )
    location = _path(
        request,
        "environment_container_detail",
        environment_id=environment_id,
        container_id=outcome.container_id,
    )
    return no_store(
        RedirectResponse(f"{location}?{urlencode(created_flags(outcome))}", status_code=303)
    )


async def _confirm_page(
    request: Request,
    containers: ContainerServices,
    actor: Principal,
    environment_id: int,
    container_id: str,
    *,
    submitted: str = "",
    status_code: int = 200,
    error: str | None = None,
    fields: dict[str, str] | None = None,
) -> Response:
    try:
        confirmation = await containers.recreation.recreate_confirmation(
            actor, environment_id, container_id
        )
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return _problem(
            request,
            heading="Recreate container",
            title="This container cannot be shown",
            message=str(unavailable),
            back_href=_path(request, "environment_container_list", environment_id=environment_id),
            back_label="Back to containers",
            status_code=503,
        )
    if source_of(request.app)().protects_container(container_id):
        return _problem(
            request,
            heading="Recreate container",
            title="This container is protected",
            message=str(SelfProtected.container()),
            back_href=_path(
                request,
                "environment_container_detail",
                environment_id=environment_id,
                container_id=container_id,
            ),
            back_label="Back to container",
            status_code=409,
        )
    offer = (
        await _offer(containers, actor, environment_id, confirmation.image)
        if confirmation.pull_available
        else None
    )
    return _page(
        request,
        CONFIRM_TEMPLATE,
        {
            "confirmation": confirmation,
            "offer": offer,
            "selected": "" if offer is None else selected_choice(offer, submitted),
            "choice_error": (fields or {}).get(CHOICE_FIELD),
            "error": error,
            "catalogue": CATALOGUE,
            "recreate_href": _path(
                request,
                "environment_container_recreate",
                environment_id=environment_id,
                container_id=container_id,
            ),
            "detail_href": _path(
                request,
                "environment_container_detail",
                environment_id=environment_id,
                container_id=container_id,
            ),
        },
        status_code,
    )


@router.get(
    "/{container_id}/recreate",
    response_class=HTMLResponse,
    name="environment_container_recreate_confirm",
)
async def confirm_recreate(
    container_id: ContainerPath,
    request: Request,
    containers: Containers,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Name the container and what a recreate does to it; nothing changes until it is posted."""
    return await _confirm_page(request, containers, actor, environment_id, container_id)


def _failure_of(error: Exception, containers: ContainerServices, actor: Principal) -> _Failure:
    if isinstance(error, SelfProtected):
        return _Failure(409, "This container is protected", str(error))
    if isinstance(error, ObjectBusy):
        busy = containers.actions.describe_busy(error, actor)
        return _Failure(409, "This container is busy", str(error), busy)
    if isinstance(error, NameChanged):
        return _Failure(409, "The container changed", " ".join(error.fields.values()))
    if isinstance(error, NotRecreatable):
        return _Failure(409, "This container cannot be recreated", str(error))
    if isinstance(error, RestrictedOptions):
        return _Failure(
            400,
            "Only administrators can recreate this container",
            " ".join(error.fields.values()),
        )
    if isinstance(error, ValidationFailed):
        return _Failure(400, "Check the container's configuration", " ".join(error.fields.values()))
    if isinstance(error, ContainerGone):
        return _Failure(404, "The container no longer exists", str(error))
    if isinstance(error, ContainerStateConflict):
        return _Failure(409, "The container cannot be recreated now", str(error))
    if isinstance(error, PullFailed):
        return _Failure(
            502, "The image could not be pulled", f"{error} The original container was not changed."
        )
    if isinstance(error, RecreateFailed):
        if error.outcome_unknown:
            return _Failure(503, UNKNOWN_RESULT, str(error), outcome_unknown=True)
        return _Failure(
            502,
            "The container was not recreated",
            str(error),
            detail=administrator_detail(error, actor),
        )
    if isinstance(error, CreateRefused):
        return _Failure(
            502,
            "The container was not recreated",
            str(error),
            detail=administrator_detail(error, actor),
        )
    if isinstance(error, EnvironmentUnavailable):
        if error.kind == "outcome_unknown":
            return _Failure(503, UNKNOWN_RESULT, str(error), outcome_unknown=True)
        return _Failure(503, *unreachable_copy(error))
    return _Failure(503, "The container cannot be read", str(error))


RECREATE_ERRORS = (
    SelfProtected,
    ObjectBusy,
    ValidationFailed,
    NotRecreatable,
    ContainerGone,
    ContainerStateConflict,
    CreateRefused,
    RecreateFailed,
    EnvironmentUnavailable,
    ObjectTooLarge,
)


@router.post(
    "/{container_id}/recreate", response_class=HTMLResponse, name="environment_container_recreate"
)
async def recreate_container(
    container_id: ContainerPath,
    request: Request,
    containers: Containers,
    actor: CurrentUser,
    environment_id: PathId,
    expected_name: Annotated[str, Form(max_length=NAME_MAX)] = "",
    pull: Annotated[str, Form(max_length=FLAG_MAX)] = "",
    credential_choice: Annotated[str, Form(max_length=CHOICE_MAX)] = "",
) -> Response:
    """Replace the container with one built from its own configuration, then show the new one."""

    async def again(status_code: int, **extra: Any) -> Response:
        return await _confirm_page(
            request,
            containers,
            actor,
            environment_id,
            container_id,
            submitted=credential_choice,
            status_code=status_code,
            **extra,
        )

    try:
        choice = credential_choice_of(credential_choice)
        if pull == CHECKED:
            await containers.recreation.prepare_recreate(
                actor,
                environment_id,
                container_id,
                expected_name=expected_name,
                credential_choice=choice,
            )
            job_id = start_container_recreate_job(
                request.app,
                actor,
                environment_id,
                container_id,
                expected_name=expected_name,
                credential_choice=choice,
            )
            return no_store(
                redirect(
                    request,
                    _path(
                        request,
                        "environment_container_job",
                        environment_id=environment_id,
                        job_id=job_id,
                    ),
                )
            )
        outcome = await containers.recreation.recreate(
            actor,
            environment_id,
            container_id,
            expected_name=expected_name,
            pull=False,
            credential_choice=choice,
        )
    except (BindingCredentialMissing, CredentialSetUnavailable) as refused:
        return await again(409, error=str(refused))
    except TooManyJobs as busy:
        return await again(429, error=str(busy))
    except RebindRefused as denied:
        return await again(403, fields={CHOICE_FIELD: str(denied)})
    except RECREATE_ERRORS as error:
        if isinstance(error, ValidationFailed) and CHOICE_FIELD in error.fields:
            return await again(400, fields=error.fields)
        failure = _failure_of(error, containers, actor)
        result = {
            "failure": failure,
            "engine_detail": failure.detail,
            "list_href": _path(
                request, "environment_container_list", environment_id=environment_id
            ),
            "confirm_href": _path(
                request,
                "environment_container_recreate_confirm",
                environment_id=environment_id,
                container_id=container_id,
            ),
        }
        if isinstance(error, RecreateFailed):
            return show_result(request, RESULT_TEMPLATE, result)
        return _page(request, RESULT_TEMPLATE, result, failure.status)
    location = _path(
        request,
        "environment_container_detail",
        environment_id=environment_id,
        container_id=outcome.new_id,
    )
    return no_store(RedirectResponse(f"{location}?{_recreated_query(outcome)}", status_code=303))
