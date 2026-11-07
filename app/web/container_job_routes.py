"""The page, event stream and cancel action of a container create or recreate job."""

from dataclasses import dataclass
from functools import partial
from typing import Any, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.services.assembly import Containers
from app.services.image_jobs import ImageJobRegistry, JobView
from app.services.object_locks import StreamSlots, TooManyStreams
from app.web.container_create_routes import Notice, creation_notices
from app.web.identifiers import PathId
from app.web.image_helpers import JobId
from app.web.image_stream_routes import JobResponse, job_frames
from app.web.navigation import no_store, redirect
from app.web.object_pages import self_href
from app.web.templating import render_fragment, render_page, render_partial

router = APIRouter(
    prefix="/environments/{environment_id}/containers/jobs",
    tags=["containers"],
    dependencies=[Depends(require_user)],
)

PAGE: Final = "containers/job.html"
PARTS: Final = "containers/_job.html"
CONTAINER_KINDS: Final = ("container_create", "container_recreate")
STAGE_ORDER: Final = {
    "container_create": ("pull", "create", "start"),
    "container_recreate": ("pull", "stop", "rename", "create", "start", "verify"),
}
STAGE_LABELS: Final = {
    "pull": "Pull the image",
    "stop": "Stop the container",
    "rename": "Set the original aside",
    "create": "Create the container",
    "start": "Start the container",
    "verify": "Check that the new container runs",
    "rollback": "Restore the original container",
}
PULL: Final = "pull"
PULLED: Final = "pulled"
ROLLBACK: Final = "rollback"


@dataclass(frozen=True, slots=True)
class StageLine:
    label: str
    state: str


def stage_lines(job: JobView) -> tuple[StageLine, ...]:
    """Return the steps of the job in order, each pending, running, done or stopped.

    A step that was never reached is listed while the job runs and left out once it has ended.
    """
    reached = [stage for stage in job.progress.stages if stage != PULLED]
    pulled = PULLED in job.progress.stages
    current = next((stage for stage in reversed(reached) if stage != ROLLBACK), None)
    lines: list[StageLine] = []
    for stage in (*STAGE_ORDER[job.kind], ROLLBACK):
        label = STAGE_LABELS[stage]
        if stage not in reached:
            skipped = stage == "stop" and "rename" in reached
            if job.running and not skipped and stage not in (ROLLBACK, "verify"):
                lines.append(StageLine(label, "pending"))
            continue
        if stage == ROLLBACK or stage != current or (stage == PULL and pulled):
            state = "done"
        elif job.running:
            state = "running"
        else:
            state = "done" if job.state == "succeeded" else "failed"
        if stage == current and job.state == "cancelled":
            label += " (cancelled)"
        lines.append(StageLine(label, state))
    return tuple(lines)


def _can_cancel(job: JobView) -> bool:
    stages = job.progress.stages
    return job.running and bool(stages) and stages[-1] == PULL


def _notices(job: JobView) -> tuple[Notice, ...]:
    if job.outcome is None or not job.outcome.ok:
        return ()
    return creation_notices(job.outcome.flags)


def _frame_arguments(request: Request, job: JobView) -> dict[str, Any]:
    return {
        "job": job,
        "stages": stage_lines(job),
        "notices": _notices(job),
        "can_cancel": _can_cancel(job),
        "cancel_href": str(
            request.app.url_path_for(
                "environment_container_job_cancel",
                environment_id=job.environment_id,
                job_id=job.job_id,
            )
        ),
    }


def _render(request: Request, job: JobView) -> str:
    response = render_fragment(
        request, PARTS, "container_job_progress", **_frame_arguments(request, job)
    )
    return bytes(response.body).decode("utf-8").strip()


def _visible_job(
    jobs: ImageJobRegistry, actor: Principal, environment_id: int, job_id: str
) -> JobView:
    job = jobs.view(job_id, actor)
    if job.environment_id != environment_id or job.kind not in CONTAINER_KINDS:
        raise ResourceNotFound("not found")
    return job


def _jobs_of(request: Request) -> ImageJobRegistry:
    jobs: ImageJobRegistry = request.app.state.engine.image_jobs
    return jobs


@router.get("/{job_id}", response_class=HTMLResponse, name="environment_container_job")
async def job_page(
    request: Request,
    containers: Containers,
    actor: CurrentUser,
    environment_id: PathId,
    job_id: JobId,
) -> Response:
    """Show a container create or recreate; script follows it live, the page itself needs none."""
    await containers.queries.require_visible(actor, environment_id)
    job = _visible_job(_jobs_of(request), actor, environment_id, job_id)
    path = partial(request.app.url_path_for, environment_id=environment_id)
    return no_store(
        render_page(
            request,
            PAGE,
            {
                **_frame_arguments(request, job),
                "list_href": str(path("environment_container_list")),
                "job_href": self_href(request),
                "stream_href": str(path("environment_container_job_stream", job_id=job_id)),
            },
        )
    )


@router.post(
    "/{job_id}/cancel", response_class=HTMLResponse, name="environment_container_job_cancel"
)
async def cancel_job(
    request: Request,
    containers: Containers,
    actor: CurrentUser,
    environment_id: PathId,
    job_id: JobId,
) -> Response:
    """Stop the pull of a running job; a job past its pull carries on."""
    await containers.queries.require_visible(actor, environment_id)
    jobs = _jobs_of(request)
    _visible_job(jobs, actor, environment_id, job_id)
    jobs.cancel(job_id, actor)
    return redirect(
        request,
        str(
            request.app.url_path_for(
                "environment_container_job", environment_id=environment_id, job_id=job_id
            )
        ),
    )


@router.get(
    "/{job_id}/events", response_class=StreamingResponse, name="environment_container_job_stream"
)
async def job_stream(
    request: Request,
    containers: Containers,
    actor: CurrentUser,
    environment_id: PathId,
    job_id: JobId,
) -> Response:
    """Stream a container job as server-sent events; only its starter or an administrator."""
    jobs = _jobs_of(request)
    slots: StreamSlots = request.app.state.engine.stream_slots
    await containers.queries.require_visible(actor, environment_id)
    _visible_job(jobs, actor, environment_id, job_id)
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
        return JobResponse(
            slot,
            job_frames(partial(_render, request), jobs, actor, environment_id, job_id, slot),
        )
    except BaseException:
        slot.close()
        raise
