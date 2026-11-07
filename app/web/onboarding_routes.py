"""Adopt everything on this host: the review job, the review, its confirmation and the apply."""

import re
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.services.assembly import Environments
from app.services.environments import EnvironmentSummary
from app.services.image_jobs import ImageJobRegistry, JobView, TooManyJobs
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.onboarding import OnboardingChanged
from app.services.onboarding_assembly import Onboarding, start_onboarding_review
from app.services.onboarding_plan import OnboardingReview
from app.services.onboarding_store import TOKEN, OnboardingReviewStore
from app.services.stack_read_result import mask_secret_values
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
from app.web.onboarding_pages import (
    image_rows,
    object_rows,
    owner_fields,
    read_selection,
    stack_rows,
)
from app.web.problems import unreachable_copy
from app.web.results import show_result
from app.web.stack_read_pages import SOURCES

router = APIRouter(
    prefix="/environments/{environment_id}/onboarding",
    tags=["environments"],
    dependencies=[Depends(require_user)],
)

DIGEST_MAX: Final = 64
HEADING: Final = "Adopt everything on this host"
UNAVAILABLE: Final = (EnvironmentUnavailable, ObjectTooLarge)
EXPIRED: Final = Failure(
    409,
    "The review has expired",
    "A review is held for 30 minutes for the person who started it and is dropped once applied. "
    "Start a new review.",
)
APPLYING: Final = Failure(
    409,
    "Already being applied",
    "This review is already being applied. Its result is shown to the request that started it.",
)
CONFIRM_AGAIN: Final = Failure(
    409,
    "Confirm the selection first",
    "The selection was not confirmed, or the review was replaced. Review and confirm again.",
)
_TOKEN: Final = re.compile(TOKEN)


def _canonical_token(token: str) -> str:
    """Refuse a token of the wrong shape as a missing page."""
    if _TOKEN.fullmatch(token) is None:
        raise ResourceNotFound("not found")
    return token


ReviewToken = Annotated[str, Depends(_canonical_token)]


def _administrators_only(actor: Principal) -> None:
    if not actor.is_administrator:
        raise ResourceNotFound("not found")


def _environment_href(request: Request, environment_id: int) -> str:
    return route(request, "environment_detail", environment_id=environment_id)


def _review_href(request: Request, environment_id: int, token: str) -> str:
    return route(
        request, "environment_onboarding_review", environment_id=environment_id, token=token
    )


def _held(request: Request, actor: Principal, environment_id: int, token: str) -> OnboardingReview:
    store: OnboardingReviewStore = request.app.state.engine.onboarding_reviews
    review = store.get(token, actor.user_id, environment_id)
    if review is None:
        raise ResourceNotFound("not found")
    return review


def _failed(
    request: Request,
    environment_id: int,
    failure: Failure,
    *,
    back_href: str | None = None,
    back_label: str = "Back to the environment",
    environment_name: str = "",
) -> Response:
    return problem_page(
        request,
        heading=HEADING,
        failure=failure,
        back_href=back_href or _environment_href(request, environment_id),
        back_label=back_label,
        region=None,
        environment_name=environment_name,
    )


@router.post("", response_class=HTMLResponse, name="environment_onboarding_start")
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
        job_id = start_onboarding_review(request.app, actor, environment_id)
    except TooManyJobs as refused:
        return _failed(
            request,
            environment_id,
            Failure(429, "Too many transfers are running", str(refused)),
            environment_name=environment.name,
        )
    return redirect(
        request,
        route(request, "environment_onboarding_job", environment_id=environment_id, job_id=job_id),
    )


def _own_job(
    request: Request, actor: Principal, environment_id: int, job_id: str
) -> tuple[ImageJobRegistry, JobView]:
    jobs: ImageJobRegistry = request.app.state.engine.image_jobs
    job = jobs.view(job_id, actor)
    if (
        job.kind != "onboarding_review"
        or job.environment_id != environment_id
        or job.owner_user_id != actor.user_id
    ):
        raise ResourceNotFound("not found")
    return jobs, job


@router.get("/jobs/{job_id}", response_class=HTMLResponse, name="environment_onboarding_job")
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
        "environments/onboarding_job.html",
        {
            "environment": environment,
            "job": job,
            "review_href": None if token is None else _review_href(request, environment_id, token),
            "job_href": route(
                request,
                "environment_onboarding_job",
                environment_id=environment_id,
                job_id=job_id,
            ),
            "cancel_href": route(
                request,
                "environment_onboarding_job_cancel",
                environment_id=environment_id,
                job_id=job_id,
            ),
            "environment_href": _environment_href(request, environment_id),
        },
    )


@router.post(
    "/jobs/{job_id}/cancel", response_class=HTMLResponse, name="environment_onboarding_job_cancel"
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
    return redirect(
        request,
        route(request, "environment_onboarding_job", environment_id=environment_id, job_id=job_id),
    )


@router.get("/reviews/{token}", response_class=HTMLResponse, name="environment_onboarding_review")
async def review_page(
    request: Request,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    token: ReviewToken,
) -> Response:
    """Show everything the review found, each target ticked or not by its default."""
    _administrators_only(actor)
    environment = (await environments.environments.get(actor, environment_id)).summary
    store: OnboardingReviewStore = request.app.state.engine.onboarding_reviews
    review = store.get(token, actor.user_id, environment_id)
    if review is None:
        return _failed(request, environment_id, EXPIRED, environment_name=environment.name)
    return page_response(
        request,
        "environments/onboarding_review.html",
        _review_context(request, environment, review, token),
    )


def _review_context(
    request: Request, environment: EnvironmentSummary, review: OnboardingReview, token: str
) -> dict[str, object]:
    arguments = {"environment_id": review.environment_id, "token": token}
    return {
        "environment": environment,
        "review": review,
        "stacks": stack_rows(review),
        "images": image_rows(review),
        "objects": object_rows(review),
        "confirm_href": route(request, "environment_onboarding_confirm", **arguments),
        "stack_href": {
            index: route(request, "environment_onboarding_stack", number=index + 1, **arguments)
            for index, target in enumerate(review.stacks)
            if target.offered
        },
        "environment_href": _environment_href(request, review.environment_id),
    }


@router.get(
    "/reviews/{token}/stacks/{number}",
    response_class=HTMLResponse,
    name="environment_onboarding_stack",
)
async def stack_page(
    request: Request,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    token: ReviewToken,
    number: PathId,
) -> Response:
    """Show the file that adopting one stack as is would store; no secret value is shown.

    Environment values the name or the value marks as secret are shown as `(hidden)`.
    """
    _administrators_only(actor)
    environment = (await environments.environments.get(actor, environment_id)).summary
    review = _held(request, actor, environment_id, token)
    if number > len(review.stacks) or not review.stacks[number - 1].offered:
        raise ResourceNotFound("not found")
    target = review.stacks[number - 1]
    result = target.result
    assert result is not None and target.report is not None
    return page_response(
        request,
        "environments/onboarding_stack.html",
        {
            "environment": environment,
            "target": target,
            "source": SOURCES[result.file_source],
            "services": target.report.services,
            "files": [(item.name, item.secret, len(item.content)) for item in result.files],
            "variables": [
                (item.name, item.secret, None if item.secret else item.value)
                for item in result.variables
            ],
            "losses": result.losses,
            "text": mask_secret_values(result.compose_text),
            "back_href": _review_href(request, environment_id, token),
        },
    )


@router.post(
    "/reviews/{token}/confirm", response_class=HTMLResponse, name="environment_onboarding_confirm"
)
async def confirm(
    request: Request,
    onboarding: Onboarding,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    token: ReviewToken,
) -> Response:
    """Name every ticked target and what it would get; nothing is written."""
    _administrators_only(actor)
    environment = (await environments.environments.get(actor, environment_id)).summary
    review = onboarding.reviews.get(token, actor.user_id, environment_id)
    if review is None:
        return _failed(request, environment_id, EXPIRED, environment_name=environment.name)
    back = _review_href(request, environment_id, token)
    try:
        form = await request.form()
        owner, team, level = owner_fields(form)
        ownership = await onboarding.onboarding.resolve_ownership(actor, owner, team, level)
        selection = read_selection(form, review, ownership)
        plan = await onboarding.onboarding.plan(actor, environment_id, review, selection)
    except ValidationFailed as invalid:
        return _failed(
            request,
            environment_id,
            Failure(400, "Check the selection", "; ".join(invalid.fields.values())),
            back_href=back,
            back_label="Back to the review",
            environment_name=environment.name,
        )
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, HEADING, unavailable, back, "Back to the review", region=None
        )
    onboarding.reviews.confirm(token, actor.user_id, environment_id, selection)
    return page_response(
        request,
        "environments/onboarding_confirm.html",
        {
            "environment": environment,
            "plan": plan,
            "review_href": back,
            "apply_href": route(
                request,
                "environment_onboarding_apply",
                environment_id=environment_id,
                token=token,
            ),
        },
    )


@router.post(
    "/reviews/{token}/apply", response_class=HTMLResponse, name="environment_onboarding_apply"
)
async def apply(
    request: Request,
    onboarding: Onboarding,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    token: ReviewToken,
    expected_plan: Annotated[str, Form(max_length=DIGEST_MAX)] = "",
) -> Response:
    """Write exactly the plan that was confirmed, each target as its own audited operation."""
    _administrators_only(actor)
    environment = (await environments.environments.get(actor, environment_id)).summary
    review = onboarding.reviews.get(token, actor.user_id, environment_id)
    if review is None:
        return _failed(request, environment_id, EXPIRED, environment_name=environment.name)
    selection = onboarding.reviews.confirmed(token, actor.user_id, environment_id)
    back = _review_href(request, environment_id, token)
    if selection is None:
        return _failed(
            request,
            environment_id,
            CONFIRM_AGAIN,
            back_href=back,
            back_label="Back to the review",
            environment_name=environment.name,
        )
    if not onboarding.reviews.begin_apply(token, actor.user_id, environment_id):
        return _failed(
            request,
            environment_id,
            APPLYING,
            environment_name=environment.name,
        )
    try:
        result = await onboarding.onboarding.apply(
            actor, environment_id, review, selection, expected_plan
        )
    except OnboardingChanged as changed:
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
            back_href=back,
            back_label="Back to the review",
            environment_name=environment.name,
        )
    except UNAVAILABLE as unavailable:
        failure = Failure(503, *unreachable_copy(unavailable))
        return _failed(request, environment_id, failure, environment_name=environment.name)
    except PermissionDenied:
        raise ResourceNotFound("not found") from None
    finally:
        onboarding.reviews.end_apply(token)
    onboarding.reviews.discard(token)
    return show_result(
        request,
        "environments/onboarding_result.html",
        {
            "environment": environment,
            "result": result,
            "environment_href": _environment_href(request, environment_id),
        },
    )
