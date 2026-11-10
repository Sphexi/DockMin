"""Bind unbound images to credential sets: the review job, the review, and the confirmed applies."""

import re
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import StringConstraints

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.services.assembly import Environments
from app.services.bulk_binding_assembly import BulkBinding, start_bulk_binding_review
from app.services.bulk_binding_plan import CHANGED_MESSAGE, BulkBindingReview
from app.services.bulk_binding_store import BulkBindingReviewStore
from app.services.bulk_bindings import BulkReviewBusy, BulkReviewExpired
from app.services.image_jobs import ImageJobRegistry, JobView, TooManyJobs
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.onboarding_store import TOKEN
from app.web.identifiers import PathId
from app.web.image_helpers import JobId
from app.web.object_pages import (
    Failure,
    fragment_response,
    page_response,
    problem_page,
    redirect,
    route,
    unavailable_page,
)
from app.web.templating import is_htmx_request

router = APIRouter(
    prefix="/environments/{environment_id}/images/anonymous-binding",
    tags=["images"],
    dependencies=[Depends(require_user)],
)

HEADING: Final = "Bind unbound images"
JOB_TEMPLATE: Final = "images/bulk_binding_job.html"
PANEL_TEMPLATE: Final = "images/_bulk_binding_job.html"
REVIEW_TEMPLATE: Final = "images/bulk_binding_review.html"
DIGEST_MAX: Final = 64
ADDRESS_MAX: Final = 262
CHOICE_MAX: Final = 700
MAX_REGISTRIES: Final = 50
MAX_CHOICES: Final = 200
NO_SET: Final = "none"
BAD_CHOICE: Final = "A credential set choice is malformed."
DUPLICATE_CHOICE: Final = "An image was chosen twice."
UNAVAILABLE: Final = (EnvironmentUnavailable, ObjectTooLarge)
EXPIRED: Final = Failure(
    409,
    "The review has expired",
    "A review is held for 30 minutes for the person who started it. Start a new review.",
)
APPLYING: Final = Failure(
    409,
    "Already being applied",
    "This review is already being applied. Its result is shown on the review when it finishes.",
)
_TOKEN: Final = re.compile(TOKEN)

Registry = Annotated[str, StringConstraints(max_length=ADDRESS_MAX)]
PlanDigest = Annotated[str, StringConstraints(max_length=DIGEST_MAX)]
Choice = Annotated[str, StringConstraints(max_length=CHOICE_MAX)]


def _canonical_token(token: str) -> str:
    """Refuse a token of the wrong shape as a missing page."""
    if _TOKEN.fullmatch(token) is None:
        raise ResourceNotFound("not found")
    return token


ReviewToken = Annotated[str, Depends(_canonical_token)]


def _administrators_only(actor: Principal) -> None:
    if not actor.is_administrator:
        raise ResourceNotFound("not found")


def _images_href(request: Request, environment_id: int) -> str:
    return route(request, "environment_image_list", environment_id=environment_id)


def _review_href(request: Request, environment_id: int, token: str) -> str:
    return route(request, "bulk_binding_review", environment_id=environment_id, token=token)


def _job_href(request: Request, environment_id: int, job_id: str) -> str:
    return route(request, "bulk_binding_job", environment_id=environment_id, job_id=job_id)


def _failed(
    request: Request,
    environment_id: int,
    failure: Failure,
    *,
    environment_name: str,
    back_href: str | None = None,
    back_label: str = "Back to the images",
) -> Response:
    return problem_page(
        request,
        heading=HEADING,
        failure=failure,
        back_href=back_href or _images_href(request, environment_id),
        back_label=back_label,
        region=None,
        environment_name=environment_name,
    )


def _read_choices(values: list[str]) -> dict[str, int | None]:
    """Read `<reference> <set id>` pairs, `none` for an image left unbound."""
    found: dict[str, int | None] = {}
    for value in values:
        reference, separator, chosen = value.partition(" ")
        if not separator or not reference:
            raise ValidationFailed({"choices": BAD_CHOICE})
        if reference in found:
            raise ValidationFailed({"choices": DUPLICATE_CHOICE})
        if chosen == NO_SET:
            found[reference] = None
        elif chosen.isascii() and chosen.isdigit() and len(chosen) <= len(str(2**63)):
            found[reference] = int(chosen)
        else:
            raise ValidationFailed({"choices": BAD_CHOICE})
    return found


@router.post("", response_class=HTMLResponse, name="bulk_binding_start")
async def start_review(
    request: Request, environments: Environments, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Start the review of a reachable environment as a background job and open its page."""
    _administrators_only(actor)
    environment = (await environments.environments.get(actor, environment_id)).summary
    if environment.status != "reachable":
        return _failed(
            request,
            environment_id,
            Failure(
                409,
                "The environment is not reachable",
                "Test the connection, then start the review again.",
            ),
            environment_name=environment.name,
        )
    try:
        job_id = start_bulk_binding_review(request.app, actor, environment_id)
    except TooManyJobs as refused:
        return _failed(
            request,
            environment_id,
            Failure(429, "Too many jobs are running", str(refused)),
            environment_name=environment.name,
        )
    return redirect(request, _job_href(request, environment_id, job_id))


def _own_job(
    request: Request, actor: Principal, environment_id: int, job_id: str
) -> tuple[ImageJobRegistry, JobView]:
    jobs: ImageJobRegistry = request.app.state.engine.image_jobs
    job = jobs.view(job_id, actor)
    if (
        job.kind != "bulk_binding_review"
        or job.environment_id != environment_id
        or job.owner_user_id != actor.user_id
    ):
        raise ResourceNotFound("not found")
    return jobs, job


@router.get("/jobs/{job_id}", response_class=HTMLResponse, name="bulk_binding_job")
async def job_page(
    request: Request,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    job_id: JobId,
) -> Response:
    """Show the progress of a review; only the user who started it may see it."""
    _administrators_only(actor)
    environment = (await environments.environments.get(actor, environment_id)).summary
    _, job = _own_job(request, actor, environment_id, job_id)
    token = None if job.outcome is None else job.outcome.flags.get("review")
    review_href = None if token is None else _review_href(request, environment_id, token)
    panel: dict[str, Any] = {
        "environment": environment,
        "job": job,
        "review_href": review_href,
        "job_href": _job_href(request, environment_id, job_id),
        "cancel_href": route(
            request,
            "bulk_binding_job_cancel",
            environment_id=environment_id,
            job_id=job_id,
        ),
    }
    if is_htmx_request(request):
        if review_href is not None:
            return redirect(request, review_href)
        return fragment_response(request, PANEL_TEMPLATE, "job_panel", **panel)
    return page_response(
        request, JOB_TEMPLATE, {**panel, "images_href": _images_href(request, environment_id)}
    )


@router.post("/jobs/{job_id}/cancel", response_class=HTMLResponse, name="bulk_binding_job_cancel")
async def cancel_job(
    request: Request,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    job_id: JobId,
) -> Response:
    """Stop a running review; the job page shows how it ended."""
    _administrators_only(actor)
    await environments.environments.get(actor, environment_id)
    jobs, _ = _own_job(request, actor, environment_id, job_id)
    jobs.cancel(job_id, actor)
    return redirect(request, _job_href(request, environment_id, job_id))


def _held(
    request: Request, actor: Principal, environment_id: int, token: str
) -> BulkBindingReview | None:
    store: BulkBindingReviewStore = request.app.state.engine.bulk_binding_reviews
    return store.get(token, actor.user_id, environment_id)


@router.get("/reviews/{token}", response_class=HTMLResponse, name="bulk_binding_review")
async def review_page(
    request: Request,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    token: ReviewToken,
) -> Response:
    """Show what each registry's credential sets can read, with the latest apply's result."""
    _administrators_only(actor)
    environment = (await environments.environments.get(actor, environment_id)).summary
    review = _held(request, actor, environment_id, token)
    if review is None:
        return _failed(request, environment_id, EXPIRED, environment_name=environment.name)
    store: BulkBindingReviewStore = request.app.state.engine.bulk_binding_reviews
    return page_response(
        request,
        REVIEW_TEMPLATE,
        {
            "environment": environment,
            "review": review,
            "summary": store.summary(token, actor.user_id, environment_id),
            "changed_message": CHANGED_MESSAGE,
            "apply_href": route(
                request, "bulk_binding_apply", environment_id=environment_id, token=token
            ),
            "images_href": _images_href(request, environment_id),
        },
    )


@router.post("/reviews/{token}/apply", response_class=HTMLResponse, name="bulk_binding_apply")
async def apply(
    request: Request,
    bulk: BulkBinding,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    token: ReviewToken,
    registry: Annotated[list[Registry], Form(max_length=MAX_REGISTRIES)] = [],  # noqa: B006
    expected_plan: Annotated[list[PlanDigest], Form(max_length=MAX_REGISTRIES)] = [],  # noqa: B006
    choice: Annotated[list[Choice], Form(max_length=MAX_CHOICES)] = [],  # noqa: B006
    only: Annotated[str, Form(max_length=ADDRESS_MAX)] = "",
) -> Response:
    """Bind what the confirmed registries offered, each image as its own audited write.

    `only` names the one registry a per-registry button applies; without it every posted
    registry is applied.
    """
    _administrators_only(actor)
    environment = (await environments.environments.get(actor, environment_id)).summary
    back = _review_href(request, environment_id, token)
    registries, plans = list(registry), list(expected_plan)
    if only and len(registries) == len(plans):
        pairs = [(a, p) for a, p in zip(registries, plans, strict=True) if a == only]
        registries, plans = [a for a, _ in pairs], [p for _, p in pairs]
    try:
        await bulk.service.apply(
            actor, environment_id, token, registries, plans, _read_choices(choice)
        )
    except BulkReviewExpired:
        return _failed(request, environment_id, EXPIRED, environment_name=environment.name)
    except BulkReviewBusy:
        return _failed(request, environment_id, APPLYING, environment_name=environment.name)
    except ValidationFailed as invalid:
        return _failed(
            request,
            environment_id,
            Failure(400, "Check the selection", "; ".join(invalid.fields.values())),
            environment_name=environment.name,
            back_href=back,
            back_label="Back to the review",
        )
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            HEADING,
            unavailable,
            _images_href(request, environment_id),
            "Back to the images",
            region=None,
        )
    except PermissionDenied:
        raise ResourceNotFound("not found") from None
    return redirect(request, back)
