"""Bind unbound images anonymously: the review job, the review, and the confirmed apply."""

import re
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import StringConstraints

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.services.anonymous_binding_assembly import AnonymousBinding, start_anonymous_binding_review
from app.services.anonymous_binding_plan import AnonymousBindingReview
from app.services.anonymous_binding_store import AnonymousBindingReviewStore
from app.services.anonymous_bindings import (
    AnonymousBindingChanged,
    AnonymousReviewBusy,
    AnonymousReviewExpired,
)
from app.services.assembly import Environments
from app.services.image_jobs import ImageJobRegistry, JobView, TooManyJobs
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.onboarding_store import TOKEN
from app.web.identifiers import PathId
from app.web.image_helpers import JobId
from app.web.object_pages import (
    Failure,
    page_response,
    problem_page,
    redirect,
    route,
    unavailable_page,
)
from app.web.results import show_result

router = APIRouter(
    prefix="/environments/{environment_id}/images/anonymous-binding",
    tags=["images"],
    dependencies=[Depends(require_user)],
)

HEADING: Final = "Bind unbound images anonymously"
DIGEST_MAX: Final = 64
ADDRESS_MAX: Final = 262
MAX_REGISTRIES: Final = 50
UNAVAILABLE: Final = (EnvironmentUnavailable, ObjectTooLarge)
EXPIRED: Final = Failure(
    409,
    "The review has expired",
    "A review is held for 30 minutes for the person who started it. Start a new review.",
)
APPLYING: Final = Failure(
    409,
    "Already being applied",
    "This review is already being applied. Its result is shown to the request that started it.",
)
_TOKEN: Final = re.compile(TOKEN)

Registry = Annotated[str, StringConstraints(max_length=ADDRESS_MAX)]


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
    return route(request, "anonymous_binding_review", environment_id=environment_id, token=token)


def _job_href(request: Request, environment_id: int, job_id: str) -> str:
    return route(request, "anonymous_binding_job", environment_id=environment_id, job_id=job_id)


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


@router.post("", response_class=HTMLResponse, name="anonymous_binding_start")
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
        job_id = start_anonymous_binding_review(request.app, actor, environment_id)
    except TooManyJobs as refused:
        return _failed(
            request,
            environment_id,
            Failure(429, "Too many transfers are running", str(refused)),
            environment_name=environment.name,
        )
    return redirect(request, _job_href(request, environment_id, job_id))


def _own_job(
    request: Request, actor: Principal, environment_id: int, job_id: str
) -> tuple[ImageJobRegistry, JobView]:
    jobs: ImageJobRegistry = request.app.state.engine.image_jobs
    job = jobs.view(job_id, actor)
    if (
        job.kind != "anonymous_binding_review"
        or job.environment_id != environment_id
        or job.owner_user_id != actor.user_id
    ):
        raise ResourceNotFound("not found")
    return jobs, job


@router.get("/jobs/{job_id}", response_class=HTMLResponse, name="anonymous_binding_job")
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
    return page_response(
        request,
        "images/anonymous_binding_job.html",
        {
            "environment": environment,
            "job": job,
            "review_href": None if token is None else _review_href(request, environment_id, token),
            "job_href": _job_href(request, environment_id, job_id),
            "cancel_href": route(
                request,
                "anonymous_binding_job_cancel",
                environment_id=environment_id,
                job_id=job_id,
            ),
            "images_href": _images_href(request, environment_id),
        },
    )


@router.post(
    "/jobs/{job_id}/cancel", response_class=HTMLResponse, name="anonymous_binding_job_cancel"
)
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
) -> AnonymousBindingReview | None:
    store: AnonymousBindingReviewStore = request.app.state.engine.anonymous_binding_reviews
    return store.get(token, actor.user_id, environment_id)


@router.get("/reviews/{token}", response_class=HTMLResponse, name="anonymous_binding_review")
async def review_page(
    request: Request,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    token: ReviewToken,
) -> Response:
    """Show the references each registry's anonymous HEAD matched, one confirmation per registry."""
    _administrators_only(actor)
    environment = (await environments.environments.get(actor, environment_id)).summary
    review = _held(request, actor, environment_id, token)
    if review is None:
        return _failed(request, environment_id, EXPIRED, environment_name=environment.name)
    return page_response(
        request,
        "images/anonymous_binding_review.html",
        {
            "environment": environment,
            "review": review,
            "plans": {group.address: review.digest([group.address]) for group in review.groups},
            "apply_href": route(
                request, "anonymous_binding_apply", environment_id=environment_id, token=token
            ),
            "images_href": _images_href(request, environment_id),
        },
    )


@router.post("/reviews/{token}/apply", response_class=HTMLResponse, name="anonymous_binding_apply")
async def apply(
    request: Request,
    bulk: AnonymousBinding,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    token: ReviewToken,
    registry: Annotated[list[Registry], Form(max_length=MAX_REGISTRIES)] = [],  # noqa: B006
    expected_plan: Annotated[str, Form(max_length=DIGEST_MAX)] = "",
) -> Response:
    """Bind the matched references of the confirmed registries, each as its own audited write."""
    _administrators_only(actor)
    environment = (await environments.environments.get(actor, environment_id)).summary
    back = _review_href(request, environment_id, token)
    try:
        result = await bulk.service.apply(actor, environment_id, token, registry, expected_plan)
    except AnonymousReviewExpired:
        return _failed(request, environment_id, EXPIRED, environment_name=environment.name)
    except AnonymousReviewBusy:
        return _failed(request, environment_id, APPLYING, environment_name=environment.name)
    except AnonymousBindingChanged as changed:
        return _failed(
            request,
            environment_id,
            Failure(409, "The host changed", "; ".join(changed.fields.values())),
            environment_name=environment.name,
        )
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
    return show_result(
        request,
        "images/anonymous_binding_result.html",
        {
            "environment": environment,
            "result": result,
            "images_href": _images_href(request, environment_id),
        },
    )
