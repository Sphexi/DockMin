"""Image pages: list, detail, pull, push, tag, untag, remove, prune and reap."""

import re
from collections.abc import Sequence
from typing import Annotated, Any, Final
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Path, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import StringConstraints

from app.auth.dependencies import CurrentUser, require_administrator_or_not_found, require_user
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.logging import get_logger
from app.services.assembly import (
    Environments,
    Images,
    ImageServices,
    start_pull_job,
    start_push_job,
)
from app.services.container_actions import MAX_BATCH, BusyView, EngineRefused
from app.services.image_bindings import (
    MAX_REAP,
    BindingChangedSinceReview,
    CredentialOffer,
    RebindRefused,
)
from app.services.image_jobs import TooManyJobs
from app.services.image_queries import (
    BINDING_FILTERS,
    DANGLING_FILTERS,
    DEFAULT_PAGE_SIZE,
    MAX_PRUNE,
    PRUNE_MODES,
    UPDATE_FILTERS,
    USAGE_FILTERS,
)
from app.services.image_updates import MAX_CHECK_REFERENCES, TooManyUpdateChecks, UpdateStatus
from app.services.image_views import (
    ImageBatchTarget,
    ImageChangedSinceReview,
    ImageFilters,
    ImageGone,
    ImageHasDependents,
    ImageInUse,
    ImageReferenceView,
    ImageRow,
    PruneMode,
    PruneTarget,
)
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectBusy
from app.services.pull_credentials import (
    CHOICE_FIELD,
    SeenBinding,
    SeenState,
)
from app.services.registry_resolution import CredentialSetUnavailable
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.image_helpers import (
    ID_DIGITS,
    PREFIX,
    SEGMENT_MAX,
    JobId,
    credential_choice_of,
    selected_choice,
)
from app.web.navigation import no_store
from app.web.navigation import redirect as redirect_to
from app.web.object_pages import Failure, Region, problem_page, unavailable_page
from app.web.origins import RequestOrigin
from app.web.pagination import Column, PageView
from app.web.problems import unreachable_copy
from app.web.registry_providers import CATALOGUE
from app.web.results import show_result
from app.web.sorting import DirectionParam, SortParam, view_sort
from app.web.templating import is_htmx_request, render_fragment, render_page

TABS_REGION: Final = Region("image-tabs", "outerHTML")

logger = get_logger(__name__)

router = APIRouter(prefix=PREFIX, tags=["images"], dependencies=[Depends(require_user)])

FIELD_MAX: Final = 512
CURSOR_MAX: Final = 512
FLAG_MAX: Final = 8
COUNT_DIGITS: Final = 6
CHECKED: Final = "yes"
TABS: Final = ("overview", "configuration", "history")
TARGET_SEPARATOR: Final = ":"
MAX_CHECK_FORM: Final = 64
BATCH_PARAMS_MAX: Final = 100
BATCH_PARTS: Final = 3
NOTICES: Final = {
    "removed": "Image removed.",
    "untagged": "Reference removed.",
    "tagged": "Reference added.",
}

LIST_TABLES: Final = "images/_tables.html"
PARTS: Final = "images/_parts.html"
DETAIL: Final = "images/_detail.html"

IMAGE_ID: Final = re.compile(r"[0-9a-f]{64}")
TRANSFER_KINDS: Final = frozenset({"pull", "push"})

ImageAdministrator = Annotated[Principal, Depends(require_administrator_or_not_found)]

SEEN_STATES: Final[dict[str, SeenState]] = {
    "unbound": "unbound",
    "bound": "bound",
    "credential_missing": "credential_missing",
}

Field = Annotated[str, Form(max_length=FIELD_MAX)]
Flag = Annotated[str, Form(max_length=FLAG_MAX)]
Short = Annotated[str, StringConstraints(max_length=FIELD_MAX)]


def _canonical_image_id(image_id: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    """Refuse anything but the full lower-case id before any Engine call, for every role."""
    if IMAGE_ID.fullmatch(image_id) is None:
        raise ResourceNotFound("not found")
    return image_id


ImageId = Annotated[str, Depends(_canonical_image_id)]


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


def _fragment(
    request: Request,
    template: str,
    macro: str,
    status_code: int = 200,
    **arguments: Any,
) -> Response:
    return no_store(render_fragment(request, template, macro, status_code=status_code, **arguments))


def redirect(request: Request, location: str, flashes: Sequence[Flash] = ()) -> Response:
    """Answer a completed post with a redirect that no cache keeps, carrying the notices."""
    return no_store(redirect_to(request, location, flashes))


def _query(name: str, value: str) -> str:
    return urlencode([(name, value)])


def _path(request: Request, name: str, **params: Any) -> str:
    return str(request.app.url_path_for(name, **params))


def _list_path(request: Request, environment_id: int) -> str:
    return _path(request, "environment_image_list", environment_id=environment_id)


def _detail_path(request: Request, environment_id: int, image_id: str) -> str:
    return _path(
        request, "environment_image_detail", environment_id=environment_id, image_id=image_id
    )


def _self_href(request: Request) -> str:
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


def _blank(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _digits(value: str, width: int = COUNT_DIGITS) -> int | None:
    text = value.strip()
    if text.isascii() and text.isdigit() and len(text) <= width:
        return int(text)
    return None


def _busy_message(busy: BusyView) -> str:
    if busy.held_by_you:
        who = "You started"
    elif busy.holder == "another user":
        who = "Another user started"
    else:
        who = f"{busy.holder} started"
    unit = "second" if busy.age_seconds == 1 else "seconds"
    return (
        f"{who} a {busy.action} on this image {busy.age_seconds} {unit} ago and it has not "
        "finished. Wait for it to complete, then try again."
    )


def _failure_of(error: Exception, services: ImageServices, actor: Principal) -> Failure:
    """Map an error an image action can raise to the notice the caller is shown."""
    if isinstance(error, ObjectBusy):
        return Failure(
            409, "This image is busy", _busy_message(services.actions.describe_busy(error, actor))
        )
    if isinstance(error, ImageChangedSinceReview):
        return Failure(409, "The image changed", "; ".join(error.fields.values()))
    if isinstance(error, ImageInUse):
        return Failure(409, "The image is in use", str(error))
    if isinstance(error, ImageHasDependents):
        return Failure(409, "Other images depend on this one", str(error))
    if isinstance(error, ValidationFailed):
        return Failure(400, "Check the request", "; ".join(error.fields.values()))
    if isinstance(error, ImageGone):
        return Failure(404, "The image no longer exists", str(error))
    if isinstance(error, EngineRefused):
        return Failure(502, "Docker refused the action", str(error))
    if isinstance(error, CredentialSetUnavailable):
        return Failure(409, "The credential set cannot be used", str(error))
    return Failure(503, *unreachable_copy(error))


ACTION_ERRORS = (
    ObjectBusy,
    ImageChangedSinceReview,
    ImageInUse,
    ImageHasDependents,
    ValidationFailed,
    ImageGone,
    EngineRefused,
    EnvironmentUnavailable,
    ObjectTooLarge,
)
RETRYABLE: Final = (409, 502, 503)


def _problem(
    request: Request,
    *,
    heading: str,
    failure: Failure,
    back_href: str,
    back_label: str,
    region: Region | None,
    review_href: str | None = None,
    environment_name: str = "",
) -> Response:
    return problem_page(
        request,
        heading=heading,
        failure=failure,
        back_href=back_href,
        back_label=back_label,
        region=region,
        review_href=review_href,
        environment_name=environment_name,
    )


def _unavailable(
    request: Request,
    heading: str,
    error: Exception,
    back_href: str,
    back_label: str,
    *,
    region: Region | None,
) -> Response:
    return unavailable_page(request, heading, error, back_href, back_label, region=region)


def _is_checkable(reference: ImageReferenceView) -> bool:
    return reference.kind == "tag"


def _checkable(rows: Sequence[ImageRow]) -> list[str]:
    """Collect the tag references of a page, at most as many as one check accepts."""
    found = [ref.key for row in rows for ref in row.references if _is_checkable(ref)]
    return found[:MAX_CHECK_REFERENCES]


def _columns(can_create: bool, can_remove: bool) -> tuple[Column, ...]:
    base = (
        Column("image", "Image", sortable=True),
        Column("size", "Size", sortable=True, numeric=True, width="count"),
        Column("created", "Created", sortable=True, optional=True, width="time"),
        Column("used", "Used by", width="names"),
    )
    return (
        (*base, Column("actions", "Actions", width="actions-wide"))
        if can_create or can_remove
        else base
    )


@router.get("", response_class=HTMLResponse, name="environment_image_list")
async def list_images(
    request: Request,
    images: Images,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    q: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
    dangling: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
    usage: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
    binding: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
    update: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=CURSOR_MAX)] = None,
    sort: SortParam = None,
    direction: DirectionParam = None,
) -> Response:
    """List the images of the host, read now, with the binding of each reference."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    filters = ImageFilters(
        q=_blank(q),
        dangling=_blank(dangling),
        usage=_blank(usage),
        binding=_blank(binding),
        update=_blank(update),
    )
    rows: list[ImageRow] = []
    total = 0
    can_create = False
    can_remove = False
    orphans = None
    errors: tuple[str, ...] = ()
    refused: dict[str, str] = {}
    failure: str | None = None
    status_code = 200
    next_cursor = None
    try:
        result = await images.queries.page(
            actor,
            environment_id,
            filters,
            limit=limit,
            cursor=cursor,
            sort=sort,
            direction=direction,
        )
    except ValidationFailed as invalid:
        errors, status_code, refused = tuple(invalid.fields.values()), 400, invalid.fields
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        failure, status_code = str(unavailable), 503
    else:
        rows = list(result.page.items)
        total, can_create, can_remove = result.total, result.can_create, result.can_remove
        orphans = result.orphans
        next_cursor = result.page.next_cursor
    params = [
        (key, value)
        for key, value in (
            ("q", filters.q),
            ("dangling", filters.dangling),
            ("usage", filters.usage),
            ("binding", filters.binding),
            ("update", filters.update),
        )
        if value
    ]
    view_key, view_direction = view_sort(sort, direction, refused)
    pager = PageView(
        base_path=_list_path(request, environment_id),
        limit=limit,
        item_count=len(rows),
        cursor=None if errors or failure else cursor,
        next_cursor=next_cursor,
        sort=view_key,
        direction=view_direction,
        params=tuple(params),
    )
    results: dict[str, Any] = {
        "rows": rows,
        "columns": _columns(can_create, can_remove),
        "pager": pager,
        "total": total,
        "filtered": bool(params),
        "environment_id": environment_id,
        "errors": errors,
        "failure": failure,
        "retry_href": _self_href(request),
        "checkable": _checkable(rows),
        "orphans": orphans,
        "can_create": can_create,
        "can_remove": can_remove,
        "max_batch": MAX_BATCH,
    }
    if is_htmx_request(request):
        return _fragment(request, LIST_TABLES, "images_results", status_code, oob=True, **results)
    return _page(
        request,
        "images/list.html",
        {
            **results,
            "environment": environment,
            "can_bind_unbound": actor.is_administrator,
            "q": filters.q or "",
            "dangling": filters.dangling or "",
            "usage": filters.usage or "",
            "binding": filters.binding or "",
            "update": filters.update or "",
            "dangling_options": DANGLING_FILTERS,
            "usage_options": USAGE_FILTERS,
            "binding_options": BINDING_FILTERS,
            "update_options": UPDATE_FILTERS,
        },
        status_code,
    )


async def _pull_page(
    request: Request,
    services: ImageServices,
    environments: Environments,
    actor: Principal,
    environment_id: int,
    reference: str,
    *,
    submitted: str = "",
    fields: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    environment = (await environments.environments.get(actor, environment_id)).summary
    problems = dict(fields or {})
    offer: CredentialOffer | None = None
    if reference.strip():
        try:
            offer = await services.bindings.picker(actor, environment_id, reference)
        except ValidationFailed as invalid:
            problems = {**invalid.fields, **problems}
    return _page(
        request,
        "images/pull.html",
        {
            "environment": environment,
            "reference": reference,
            "offer": offer,
            "selected": "" if offer is None else selected_choice(offer, submitted),
            "fields": problems,
            "error": error,
            "catalogue": CATALOGUE,
            "list_href": _list_path(request, environment_id),
            "pull_href": _path(request, "environment_image_pull", environment_id=environment_id),
            "post_href": _path(
                request, "environment_image_pull_start", environment_id=environment_id
            ),
            "choices_href": _path(
                request, "environment_image_pull_choices", environment_id=environment_id
            ),
        },
        status_code,
    )


@router.get("/disk-usage", response_class=HTMLResponse, name="environment_image_disk_usage")
async def image_disk_usage(
    request: Request, images: Images, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Return the host-wide image total, or an empty fragment when the host cannot give it."""
    total: int | None = None
    try:
        total = await images.queries.disk_usage(actor, environment_id)
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        logger.warning(
            "image_disk_usage_unavailable",
            extra={"environment_id": environment_id, "reason": type(unavailable).__name__},
        )
    return _fragment(request, LIST_TABLES, "image_disk_total", total=total)


@router.get("/pull", response_class=HTMLResponse, name="environment_image_pull")
async def pull_form(
    request: Request,
    images: Images,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    reference: Annotated[str, Query(max_length=FIELD_MAX)] = "",
) -> Response:
    """Ask which image to pull and, once it is named, which credential set pulls it."""
    await images.queries.require_pull_rights(actor, environment_id)
    return await _pull_page(request, images, environments, actor, environment_id, reference)


@router.post("/pull/choices", response_class=HTMLResponse, name="environment_image_pull_choices")
async def pull_choices(
    request: Request,
    images: Images,
    actor: CurrentUser,
    environment_id: PathId,
    reference: Field = "",
) -> Response:
    """Render the credential choice for a reference; nothing is recorded."""
    await images.queries.require_pull_rights(actor, environment_id)
    if not is_htmx_request(request):
        target = _path(request, "environment_image_pull", environment_id=environment_id)
        return no_store(
            RedirectResponse(f"{target}?{_query('reference', reference)}", status_code=303)
        )
    offer: CredentialOffer | None = None
    reference_error: str | None = None
    status_code = 200
    if reference.strip():
        try:
            offer = await images.bindings.picker(actor, environment_id, reference)
        except ValidationFailed as invalid:
            reference_error, status_code = "; ".join(invalid.fields.values()), 400
    return _fragment(
        request,
        PARTS,
        "pull_choice",
        status_code,
        offer=offer,
        selected="" if offer is None else selected_choice(offer),
        error=None,
        catalogue=CATALOGUE,
        post_href=_path(request, "environment_image_pull_start", environment_id=environment_id),
        submit_label="Pull image",
        reference_error=reference_error,
    )


@router.post("/pull", response_class=HTMLResponse, name="environment_image_pull_start")
async def start_pull(
    request: Request,
    images: Images,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    reference: Field = "",
    credential_choice: Field = "",
) -> Response:
    """Start a pull as a background job and open its page."""

    async def again(status_code: int, **extra: Any) -> Response:
        return await _pull_page(
            request,
            images,
            environments,
            actor,
            environment_id,
            reference,
            submitted=credential_choice,
            status_code=status_code,
            **extra,
        )

    try:
        plan = await images.actions.prepare_pull(
            actor, environment_id, reference, credential_choice_of(credential_choice)
        )
        job_id = start_pull_job(request.app, actor, environment_id, plan)
    except ValidationFailed as invalid:
        return await again(400, fields=invalid.fields)
    except CredentialSetUnavailable as refused:
        return await again(409, error=str(refused))
    except TooManyJobs as busy:
        return await again(429, error=str(busy))
    except RebindRefused as denied:
        return await again(403, fields={CHOICE_FIELD: str(denied)})
    except EnvironmentUnavailable as unavailable:
        return _unavailable(
            request,
            "Pull image",
            unavailable,
            _list_path(request, environment_id),
            "Back to images",
            region=None,
        )
    return redirect(
        request,
        _path(request, "environment_image_job", environment_id=environment_id, job_id=job_id),
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse, name="environment_image_job")
async def job_page(
    request: Request, images: Images, actor: CurrentUser, environment_id: PathId, job_id: JobId
) -> Response:
    """Show the state of a pull or push; script follows it live, the page itself needs none."""
    await images.queries.require_visible(actor, environment_id)
    job = images.jobs.view(job_id, actor)
    if job.environment_id != environment_id or job.kind not in TRANSFER_KINDS:
        raise ResourceNotFound("not found")
    return _page(
        request,
        "images/job.html",
        {
            "job": job,
            "list_href": _list_path(request, environment_id),
            "job_href": _self_href(request),
            "stream_href": _path(
                request,
                "environment_image_job_stream",
                environment_id=environment_id,
                job_id=job_id,
            ),
            "cancel_href": _path(
                request,
                "environment_image_job_cancel",
                environment_id=environment_id,
                job_id=job_id,
            ),
            "pull_href": _path(request, "environment_image_pull", environment_id=environment_id),
        },
    )


@router.post(
    "/jobs/{job_id}/cancel", response_class=HTMLResponse, name="environment_image_job_cancel"
)
async def cancel_job(
    request: Request, images: Images, actor: CurrentUser, environment_id: PathId, job_id: JobId
) -> Response:
    """Stop a running pull or push; the job page shows how it ended."""
    await images.queries.require_visible(actor, environment_id)
    job = images.jobs.view(job_id, actor)
    if job.environment_id != environment_id or job.kind not in TRANSFER_KINDS:
        raise ResourceNotFound("not found")
    images.jobs.cancel(job_id, actor)
    return redirect(
        request,
        _path(request, "environment_image_job", environment_id=environment_id, job_id=job_id),
    )


async def _update_response(
    request: Request,
    images: Images,
    actor: Principal,
    environment_id: int,
    references: Sequence[str],
    back_href: str,
) -> Response:
    statuses: tuple[UpdateStatus, ...] = ()
    failure: str | None = None
    status_code = 200
    try:
        statuses = await images.updates.check(actor, environment_id, list(references))
    except ValidationFailed as invalid:
        failure, status_code = "; ".join(invalid.fields.values()), 400
    except TooManyUpdateChecks as busy:
        failure, status_code = str(busy), 429
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        failure, status_code = str(unavailable), 503
    if is_htmx_request(request):
        return _fragment(
            request, PARTS, "update_results", status_code, statuses=statuses, failure=failure
        )
    return _page(
        request,
        "images/updates.html",
        {"statuses": statuses, "failure": failure, "back_href": back_href},
        status_code,
    )


@router.post("/updates", response_class=HTMLResponse, name="environment_image_updates")
async def check_updates(
    request: Request,
    images: Images,
    actor: CurrentUser,
    environment_id: PathId,
    reference: Annotated[list[Short] | None, Form(max_length=MAX_CHECK_FORM)] = None,
    back: Annotated[str, Form(max_length=SEGMENT_MAX)] = "",
) -> Response:
    """Compare the given references with their registries; nothing is stored."""
    back_href = (
        _detail_path(request, environment_id, back)
        if IMAGE_ID.fullmatch(back)
        else _list_path(request, environment_id)
    )
    return await _update_response(
        request, images, actor, environment_id, reference or [], back_href
    )


@router.get("/prune", response_class=HTMLResponse, name="environment_image_prune")
async def prune_form(
    request: Request,
    images: Images,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    mode: Annotated[str, Query(max_length=16)] = "",
) -> Response:
    """Choose what to prune; once a mode is chosen, list the images that would be removed."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    if not actor.is_administrator:
        raise PermissionDenied("administrator role required")
    plan = None
    fields: dict[str, str] = {}
    status_code = 200
    chosen = _prune_mode(mode)
    if mode and chosen is None:
        fields, status_code = {"mode": "Choose dangling or unused."}, 400
    if chosen is not None:
        try:
            plan = await images.queries.prune_plan(actor, environment_id, chosen)
        except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
            return _unavailable(
                request,
                "Prune images",
                unavailable,
                _list_path(request, environment_id),
                "Back to images",
                region=None,
            )
    return _page(
        request,
        "images/prune_confirm.html",
        {
            "environment": environment,
            "mode": chosen or "",
            "modes": PRUNE_MODES,
            "plan": plan,
            "fields": fields,
            "max_prune": MAX_PRUNE,
            "list_href": _list_path(request, environment_id),
            "prune_href": _path(request, "environment_image_prune", environment_id=environment_id),
            "run_href": _path(
                request, "environment_image_prune_run", environment_id=environment_id
            ),
        },
        status_code,
    )


def _prune_mode(text: str) -> PruneMode | None:
    return next((mode for mode in PRUNE_MODES if mode == text), None)


def _prune_targets(values: Sequence[str]) -> list[PruneTarget]:
    targets: list[PruneTarget] = []
    for value in values:
        image_id, separator, digest = value.partition(TARGET_SEPARATOR)
        if (
            not separator
            or IMAGE_ID.fullmatch(image_id) is None
            or IMAGE_ID.fullmatch(digest) is None
        ):
            raise ValidationFailed({"images": "The selection was incomplete. Review it again."})
        targets.append(PruneTarget(image_id, digest))
    return targets


@router.post("/prune", response_class=HTMLResponse, name="environment_image_prune_run")
async def run_prune(
    request: Request,
    images: Images,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    mode: Annotated[str, Form(max_length=16)] = "",
    target: Annotated[list[Short] | None, Form(max_length=MAX_PRUNE)] = None,
) -> Response:
    """Remove exactly the images the confirmation listed that are still candidates."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    prune_href = _path(request, "environment_image_prune", environment_id=environment_id)
    chosen = _prune_mode(mode)
    try:
        if chosen is None:
            raise ValidationFailed({"mode": "Choose dangling or unused."})
        result = await images.actions.run_prune(
            actor, environment_id, chosen, _prune_targets(target or [])
        )
    except ACTION_ERRORS as error:
        return _problem(
            request,
            heading="Prune images",
            failure=_failure_of(error, images, actor),
            back_href=_list_path(request, environment_id),
            back_label="Back to images",
            review_href=f"{prune_href}?{_query('mode', chosen)}" if chosen else prune_href,
            environment_name=environment.name,
            region=None,
        )
    return show_result(
        request,
        "images/prune_result.html",
        {
            "environment": environment,
            "result": result,
            "list_href": _list_path(request, environment_id),
        },
    )


def _batch_targets(tokens: Sequence[str], labels: Sequence[str]) -> list[ImageBatchTarget]:
    named = len(labels) == len(tokens)
    targets: list[ImageBatchTarget] = []
    for index, token in enumerate(tokens):
        parts = token.split(TARGET_SEPARATOR)
        in_use = _digits(parts[2]) if len(parts) == BATCH_PARTS else None
        if (
            len(parts) != BATCH_PARTS
            or IMAGE_ID.fullmatch(parts[0]) is None
            or IMAGE_ID.fullmatch(parts[1]) is None
            or in_use is None
        ):
            raise ValidationFailed({"images": "The selection was incomplete. Review it again."})
        targets.append(ImageBatchTarget(parts[0], parts[1], in_use, labels[index] if named else ""))
    return targets


@router.get("/remove", response_class=HTMLResponse, name="environment_image_batch_remove")
async def batch_remove_confirm(
    request: Request,
    images: Images,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    image: Annotated[list[Short] | None, Query(max_length=BATCH_PARAMS_MAX)] = None,
) -> Response:
    """Name every image of a batch removal before anything is removed; administrators only."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    list_href = _list_path(request, environment_id)
    fields: dict[str, str] = {}
    confirmation = None
    try:
        confirmation = await images.queries.batch_remove_confirmation(
            actor, environment_id, image or []
        )
    except ValidationFailed as invalid:
        fields = invalid.fields
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return _unavailable(
            request, "Remove images", unavailable, list_href, "Back to images", region=None
        )
    return _page(
        request,
        "images/batch_confirm.html",
        {
            "environment": environment,
            "environment_id": environment_id,
            "confirmation": confirmation,
            "fields": fields,
            "max_batch": MAX_BATCH,
            "list_href": list_href,
            "run_href": _path(
                request, "environment_image_batch_remove_run", environment_id=environment_id
            ),
        },
        400 if fields else 200,
    )


@router.post("/remove", response_class=HTMLResponse, name="environment_image_batch_remove_run")
async def run_batch_remove(
    request: Request,
    images: Images,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    target: Annotated[list[Short] | None, Form(max_length=BATCH_PARAMS_MAX)] = None,
    display_name: Annotated[list[Short] | None, Form(max_length=BATCH_PARAMS_MAX)] = None,
) -> Response:
    """Remove the confirmed images, each on its own and without force, and report each result."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    list_href = _list_path(request, environment_id)
    try:
        result = await images.actions.run_batch_remove(
            actor, environment_id, _batch_targets(target or [], display_name or [])
        )
    except ACTION_ERRORS as error:
        return _problem(
            request,
            heading="Remove images",
            failure=_failure_of(error, images, actor),
            back_href=list_href,
            back_label="Back to images",
            environment_name=environment.name,
            region=None,
        )
    return show_result(
        request,
        "images/batch_result.html",
        {"environment": environment, "result": result, "list_href": list_href},
    )


@router.get("/bindings/reap", response_class=HTMLResponse, name="environment_image_reap")
async def reap_form(
    request: Request,
    images: Images,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """List the bindings whose image is gone, which a reap would delete."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    if not actor.is_administrator:
        raise PermissionDenied("administrator role required")
    try:
        reapable = await images.bindings.reapable(actor, environment_id)
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return _unavailable(
            request,
            "Image bindings",
            unavailable,
            _list_path(request, environment_id),
            "Back to images",
            region=None,
        )
    return _page(
        request,
        "images/reap_confirm.html",
        {
            "environment": environment,
            "reapable": reapable,
            "max_reap": MAX_REAP,
            "list_href": _list_path(request, environment_id),
            "run_href": _path(request, "environment_image_reap_run", environment_id=environment_id),
        },
    )


def _reap_expectations(values: Sequence[str]) -> dict[int, str]:
    expected: dict[int, str] = {}
    for value in values:
        number, separator, reference = value.partition(TARGET_SEPARATOR)
        if not separator or not reference or _digits(number, ID_DIGITS) is None:
            raise ValidationFailed({"bindings": "The selection was incomplete. Review it again."})
        expected[int(number)] = reference
    return expected


@router.post("/bindings/reap", response_class=HTMLResponse, name="environment_image_reap_run")
async def run_reap(
    request: Request,
    images: Images,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    binding: Annotated[list[Short] | None, Form(max_length=MAX_REAP)] = None,
) -> Response:
    """Delete the listed bindings that are still without an image."""
    environment = (await environments.environments.get(actor, environment_id)).summary
    try:
        count = await images.bindings.reap(
            actor, environment_id, expected=_reap_expectations(binding or [])
        )
    except ACTION_ERRORS as error:
        return _problem(
            request,
            heading="Image bindings",
            failure=_failure_of(error, images, actor),
            back_href=_list_path(request, environment_id),
            back_label="Back to images",
            review_href=_path(request, "environment_image_reap", environment_id=environment_id),
            environment_name=environment.name,
            region=None,
        )
    noun = "binding" if count == 1 else "bindings"
    return redirect(
        request, _list_path(request, environment_id), [Flash("ok", f"Removed {count} {noun}.")]
    )


async def _rebind_page(
    request: Request,
    images: ImageServices,
    environments: Environments,
    actor: Principal,
    environment_id: int,
    reference: str,
    *,
    submitted: str = "",
    fields: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    environment = (await environments.environments.get(actor, environment_id)).summary
    try:
        review = await images.bindings.rebind_review(actor, environment_id, reference)
    except ValidationFailed as invalid:
        return _problem(
            request,
            heading="Change credential set",
            failure=Failure(
                400, "This reference cannot be rebound", "; ".join(invalid.fields.values())
            ),
            back_href=_list_path(request, environment_id),
            back_label="Back to images",
            environment_name=environment.name,
            region=None,
        )
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return _unavailable(
            request,
            "Change credential set",
            unavailable,
            _list_path(request, environment_id),
            "Back to images",
            region=None,
        )
    offer = review.offer
    return _page(
        request,
        "images/rebind.html",
        {
            "environment": environment,
            "offer": offer,
            "seen": review.seen,
            "selected": selected_choice(offer, submitted),
            "fields": fields or {},
            "error": error,
            "catalogue": CATALOGUE,
            "list_href": _list_path(request, environment_id),
            "pull_href": _path(request, "environment_image_pull", environment_id=environment_id)
            + "?"
            + _query("reference", offer.reference.familiar),
            "post_href": _path(
                request, "environment_image_rebind_run", environment_id=environment_id
            ),
        },
        status_code,
    )


def _seen_of(state: str, binding_id: str, set_id: str) -> SeenBinding:
    """Read the reviewed binding the rebind form posts back."""
    if state not in SEEN_STATES:
        raise ValidationFailed({"reference": "The review was incomplete. Review it again."})
    numbers: list[int | None] = []
    for text in (binding_id, set_id):
        if not text.strip():
            numbers.append(None)
        elif (number := _digits(text, ID_DIGITS)) is None:
            raise ValidationFailed({"reference": "The review was incomplete. Review it again."})
        else:
            numbers.append(number)
    return SeenBinding(state=SEEN_STATES[state], binding_id=numbers[0], set_id=numbers[1])


@router.get("/bindings/rebind", response_class=HTMLResponse, name="environment_image_rebind")
async def rebind_form(
    request: Request,
    images: Images,
    environments: Environments,
    actor: ImageAdministrator,
    environment_id: PathId,
    reference: Annotated[str, Query(max_length=FIELD_MAX)] = "",
) -> Response:
    """Choose another credential set for a bound reference, or remove its binding."""
    return await _rebind_page(request, images, environments, actor, environment_id, reference)


@router.post("/bindings/rebind", response_class=HTMLResponse, name="environment_image_rebind_run")
async def run_rebind(
    request: Request,
    images: Images,
    environments: Environments,
    actor: ImageAdministrator,
    environment_id: PathId,
    reference: Field = "",
    credential_choice: Field = "",
    expected_binding_id: Field = "",
    expected_state: Field = "",
    expected_set: Field = "",
) -> Response:
    """Verify the chosen set and point the binding at it, or remove the binding."""

    async def again(status_code: int, **extra: Any) -> Response:
        return await _rebind_page(
            request,
            images,
            environments,
            actor,
            environment_id,
            reference,
            submitted=credential_choice,
            status_code=status_code,
            **extra,
        )

    try:
        choice = credential_choice_of(credential_choice)
        if choice is None:
            raise ValidationFailed({CHOICE_FIELD: "Choose one of the listed credential sets."})
        seen = _seen_of(expected_state, expected_binding_id, expected_set)
        await images.bindings.rebind(actor, environment_id, reference, choice, seen)
    except ValidationFailed as invalid:
        if CHOICE_FIELD in invalid.fields:
            return await again(400, fields=invalid.fields)
        environment = (await environments.environments.get(actor, environment_id)).summary
        return _problem(
            request,
            heading="Change credential set",
            failure=Failure(400, "Check the request", "; ".join(invalid.fields.values())),
            back_href=_list_path(request, environment_id),
            back_label="Back to images",
            environment_name=environment.name,
            region=None,
        )
    except CredentialSetUnavailable as refused:
        return await again(409, error=str(refused))
    except BindingChangedSinceReview as changed:
        environment = (await environments.environments.get(actor, environment_id)).summary
        return _problem(
            request,
            heading="Change credential set",
            failure=Failure(409, "The binding changed", str(changed)),
            back_href=_list_path(request, environment_id),
            back_label="Back to images",
            review_href=_path(request, "environment_image_rebind", environment_id=environment_id)
            + "?"
            + _query("reference", reference),
            environment_name=environment.name,
            region=None,
        )
    except EnvironmentUnavailable as unavailable:
        return _unavailable(
            request,
            "Change credential set",
            unavailable,
            _list_path(request, environment_id),
            "Back to images",
            region=None,
        )
    return redirect(
        request,
        _list_path(request, environment_id),
        [
            Flash(
                "ok",
                "The binding was removed."
                if choice.credential_set_id is None
                else "The binding now uses the chosen credential set.",
            )
        ],
    )


async def _detail_response(
    request: Request,
    images: ImageServices,
    actor: Principal,
    environment_id: int,
    image_id: str,
    tab: str,
) -> Response:
    if tab not in TABS:
        raise ResourceNotFound("not found")
    try:
        detail = await images.queries.detail(actor, environment_id, image_id)
        history = (
            await images.queries.history(actor, environment_id, image_id)
            if tab == "history"
            else ()
        )
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return _unavailable(
            request,
            "Image",
            unavailable,
            _list_path(request, environment_id),
            "Back to images",
            region=TABS_REGION,
        )
    arguments: dict[str, Any] = {
        "detail": detail,
        "tab": tab,
        "tabs": TABS,
        "history": history,
        "checkable": [r.key for r in detail.references if _is_checkable(r)][:MAX_CHECK_REFERENCES],
    }
    if is_htmx_request(request):
        return _fragment(request, DETAIL, "image_tabs", **arguments)
    return _page(
        request,
        "images/detail.html",
        {
            **arguments,
            "list_href": _list_path(request, environment_id),
        },
    )


@router.get(
    "/{image_id}",
    response_class=HTMLResponse,
    name="environment_image_detail",
    dependencies=[RequestOrigin],
)
async def image_detail(
    image_id: ImageId,
    request: Request,
    images: Images,
    actor: CurrentUser,
    environment_id: PathId,
    tab: Annotated[str, Query(max_length=32)] = "overview",
) -> Response:
    """Show one image, one tab at a time; each tab has its own URL."""
    return await _detail_response(request, images, actor, environment_id, image_id, tab)


async def _tag_page(
    request: Request,
    images: ImageServices,
    actor: Principal,
    environment_id: int,
    image_id: str,
    reference: str,
    *,
    fields: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    impact = None
    problems = dict(fields or {})
    if not actor.is_administrator:
        raise PermissionDenied("administrator role required")
    detail = await images.queries.detail(actor, environment_id, image_id)
    if reference.strip() and not problems:
        try:
            impact = await images.queries.tag_impact(actor, environment_id, image_id, reference)
        except ValidationFailed as invalid:
            problems, status_code = invalid.fields, 400
    return _page(
        request,
        "images/tag.html",
        {
            "detail": detail,
            "reference": reference,
            "impact": impact,
            "fields": problems,
            "tag_href": _path(
                request, "environment_image_tag", environment_id=environment_id, image_id=image_id
            ),
            "detail_href": _detail_path(request, environment_id, image_id),
        },
        status_code,
    )


@router.get("/{image_id}/tag", response_class=HTMLResponse, name="environment_image_tag")
async def tag_form(
    image_id: ImageId,
    request: Request,
    images: Images,
    actor: CurrentUser,
    environment_id: PathId,
    reference: Annotated[str, Query(max_length=FIELD_MAX)] = "",
) -> Response:
    """Ask for the new reference; once it is named, say which image holds it now."""
    try:
        return await _tag_page(request, images, actor, environment_id, image_id, reference)
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return _unavailable(
            request,
            "Tag image",
            unavailable,
            _detail_path(request, environment_id, image_id),
            "Back",
            region=None,
        )


@router.post("/{image_id}/tag", response_class=HTMLResponse, name="environment_image_tag_run")
async def run_tag(
    image_id: ImageId,
    request: Request,
    images: Images,
    actor: CurrentUser,
    environment_id: PathId,
    reference: Field = "",
    expected_replaced_id: Field = "",
) -> Response:
    """Give the image the reviewed reference."""
    detail_href = _detail_path(request, environment_id, image_id)
    try:
        await images.actions.tag(
            actor, environment_id, image_id, reference, expected_replaced_id=expected_replaced_id
        )
    except ACTION_ERRORS as error:
        failure = _failure_of(error, images, actor)
        review = _path(
            request, "environment_image_tag", environment_id=environment_id, image_id=image_id
        )
        return _problem(
            request,
            heading="Tag image",
            failure=failure,
            back_href=detail_href,
            back_label="Back to the image",
            review_href=f"{review}?{_query('reference', reference)}",
            region=None,
        )
    return redirect(request, detail_href, [Flash("ok", NOTICES["tagged"])])


async def _push_page(
    request: Request,
    images: ImageServices,
    actor: Principal,
    environment_id: int,
    image_id: str,
    reference: str,
    *,
    submitted: str = "",
    fields: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    detail = await images.queries.detail(actor, environment_id, image_id)
    if not detail.can_create:
        raise PermissionDenied("create access required")
    tags = [r.text for r in detail.references if r.kind == "tag"]
    chosen = reference if reference in tags else (tags[0] if len(tags) == 1 else "")
    problems = dict(fields or {})
    offer: CredentialOffer | None = None
    if chosen:
        try:
            offer = await images.bindings.offer(actor, environment_id, chosen)
        except ValidationFailed as invalid:
            problems = {**invalid.fields, **problems}
    return _page(
        request,
        "images/push.html",
        {
            "detail": detail,
            "tags": tags,
            "reference": chosen,
            "offer": offer,
            "selected": "" if offer is None else selected_choice(offer, submitted),
            "fields": problems,
            "error": error,
            "catalogue": CATALOGUE,
            "push_href": _path(
                request, "environment_image_push", environment_id=environment_id, image_id=image_id
            ),
            "post_href": _path(
                request,
                "environment_image_push_start",
                environment_id=environment_id,
                image_id=image_id,
            ),
            "detail_href": _detail_path(request, environment_id, image_id),
        },
        status_code,
    )


@router.get("/{image_id}/push", response_class=HTMLResponse, name="environment_image_push")
async def push_form(
    image_id: ImageId,
    request: Request,
    images: Images,
    actor: CurrentUser,
    environment_id: PathId,
    reference: Annotated[str, Query(max_length=FIELD_MAX)] = "",
) -> Response:
    """Choose which tag to push and which credential set pushes it."""
    try:
        return await _push_page(request, images, actor, environment_id, image_id, reference)
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return _unavailable(
            request,
            "Push image",
            unavailable,
            _detail_path(request, environment_id, image_id),
            "Back",
            region=None,
        )


@router.post("/{image_id}/push", response_class=HTMLResponse, name="environment_image_push_start")
async def start_push(
    image_id: ImageId,
    request: Request,
    images: Images,
    actor: CurrentUser,
    environment_id: PathId,
    reference: Field = "",
    credential_choice: Field = "",
) -> Response:
    """Start a push as a background job and open its page."""

    async def again(status_code: int, **extra: Any) -> Response:
        return await _push_page(
            request,
            images,
            actor,
            environment_id,
            image_id,
            reference,
            submitted=credential_choice,
            status_code=status_code,
            **extra,
        )

    try:
        plan = await images.actions.prepare_push(
            actor, environment_id, image_id, reference, credential_choice_of(credential_choice)
        )
        job_id = start_push_job(request.app, actor, environment_id, plan)
    except ValidationFailed as invalid:
        return await again(400, fields=invalid.fields)
    except CredentialSetUnavailable as refused:
        return await again(409, error=str(refused))
    except TooManyJobs as busy:
        return await again(429, error=str(busy))
    except RebindRefused as denied:
        return await again(403, fields={CHOICE_FIELD: str(denied)})
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return _unavailable(
            request,
            "Push image",
            unavailable,
            _detail_path(request, environment_id, image_id),
            "Back",
            region=None,
        )
    return redirect(
        request,
        _path(request, "environment_image_job", environment_id=environment_id, job_id=job_id),
    )


@router.get("/{image_id}/untag", response_class=HTMLResponse, name="environment_image_untag")
async def untag_confirm(
    image_id: ImageId,
    request: Request,
    images: Images,
    actor: CurrentUser,
    environment_id: PathId,
    reference: Annotated[str, Query(max_length=FIELD_MAX)] = "",
    force: Annotated[str, Query(max_length=FLAG_MAX)] = "",
) -> Response:
    """Name the reference and say whether removing it removes the image; administrators only."""
    detail_href = _detail_path(request, environment_id, image_id)
    try:
        impact = await images.queries.untag_impact(actor, environment_id, image_id, reference)
    except ValidationFailed as invalid:
        return _problem(
            request,
            heading="Remove reference",
            failure=Failure(400, "Check the reference", "; ".join(invalid.fields.values())),
            back_href=detail_href,
            back_label="Back to the image",
            region=None,
        )
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return _unavailable(
            request, "Remove reference", unavailable, detail_href, "Back", region=None
        )
    forcing = force == "1"
    here = _path(
        request, "environment_image_untag", environment_id=environment_id, image_id=image_id
    )
    if forcing and not (impact.is_last and impact.in_use):
        return redirect(request, f"{here}?{_query('reference', reference)}")
    return _page(
        request,
        "images/untag_confirm.html",
        {
            "impact": impact,
            "forcing": forcing,
            "reference": reference,
            "detail_href": detail_href,
            "force_href": f"{here}?{_query('reference', reference)}&force=1",
            "run_href": _path(
                request,
                "environment_image_untag_run",
                environment_id=environment_id,
                image_id=image_id,
            ),
        },
    )


@router.post("/{image_id}/untag", response_class=HTMLResponse, name="environment_image_untag_run")
async def run_untag(
    image_id: ImageId,
    request: Request,
    images: Images,
    actor: CurrentUser,
    environment_id: PathId,
    reference: Field = "",
    expected_last: Flag = "",
    expected_in_use: Annotated[str, Form(max_length=COUNT_DIGITS + 1)] = "",
    force: Flag = "",
) -> Response:
    """Remove the reviewed reference; the image goes with it if it was the last."""
    detail_href = _detail_path(request, environment_id, image_id)
    in_use = _digits(expected_in_use)
    try:
        if in_use is None:
            raise ValidationFailed({"image": "The confirmation was incomplete. Review it again."})
        outcome = await images.actions.untag(
            actor,
            environment_id,
            image_id,
            reference,
            expected_last=expected_last == CHECKED,
            expected_in_use=in_use,
            force=force == CHECKED,
        )
    except ACTION_ERRORS as error:
        review = _path(
            request, "environment_image_untag", environment_id=environment_id, image_id=image_id
        )
        return _problem(
            request,
            heading="Remove reference",
            failure=_failure_of(error, images, actor),
            back_href=detail_href,
            back_label="Back to the image",
            review_href=f"{review}?{_query('reference', reference)}",
            region=None,
        )
    if outcome.deleted:
        return redirect(
            request, _list_path(request, environment_id), [Flash("ok", NOTICES["removed"])]
        )
    return redirect(request, detail_href, [Flash("ok", NOTICES["untagged"])])


@router.get("/{image_id}/remove", response_class=HTMLResponse, name="environment_image_remove")
async def remove_confirm(
    image_id: ImageId,
    request: Request,
    images: Images,
    actor: CurrentUser,
    environment_id: PathId,
    force: Annotated[str, Query(max_length=FLAG_MAX)] = "",
) -> Response:
    """Name the image, its references and the containers using it; administrators only.

    An image in use gets a first page that only leads to a second, which carries the force.
    """
    detail_href = _detail_path(request, environment_id, image_id)
    try:
        impact = await images.queries.remove_impact(actor, environment_id, image_id)
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return _unavailable(request, "Remove image", unavailable, detail_href, "Back", region=None)
    here = _path(
        request, "environment_image_remove", environment_id=environment_id, image_id=image_id
    )
    forcing = force == "1"
    if forcing and not impact.in_use:
        return redirect(request, here)
    return _page(
        request,
        "images/remove_force_confirm.html" if forcing else "images/remove_confirm.html",
        {
            "impact": impact,
            "detail_href": detail_href,
            "force_href": f"{here}?force=1",
            "run_href": _path(
                request,
                "environment_image_remove_run",
                environment_id=environment_id,
                image_id=image_id,
            ),
        },
    )


@router.post("/{image_id}/remove", response_class=HTMLResponse, name="environment_image_remove_run")
async def run_remove(
    image_id: ImageId,
    request: Request,
    images: Images,
    actor: CurrentUser,
    environment_id: PathId,
    expected_references: Annotated[str, Form(max_length=SEGMENT_MAX)] = "",
    expected_in_use: Annotated[str, Form(max_length=COUNT_DIGITS + 1)] = "",
    force: Flag = "",
) -> Response:
    """Remove the reviewed image; forced only when the force confirmation posted it."""
    detail_href = _detail_path(request, environment_id, image_id)
    in_use = _digits(expected_in_use)
    try:
        if in_use is None:
            raise ValidationFailed({"image": "The confirmation was incomplete. Review it again."})
        await images.actions.remove(
            actor,
            environment_id,
            image_id,
            expected_references=expected_references,
            expected_in_use=in_use,
            force=force == CHECKED,
        )
    except ACTION_ERRORS as error:
        return _problem(
            request,
            heading="Remove image",
            failure=_failure_of(error, images, actor),
            back_href=detail_href,
            back_label="Back to the image",
            review_href=_path(
                request,
                "environment_image_remove",
                environment_id=environment_id,
                image_id=image_id,
            ),
            region=None,
        )
    return redirect(request, _list_path(request, environment_id), [Flash("ok", NOTICES["removed"])])
