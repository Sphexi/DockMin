"""The server-sent event stream of a Compose stack operation."""

from collections.abc import AsyncGenerator
from typing import Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.services.assembly import app_stack_services
from app.services.object_locks import StreamSlot, StreamSlots, TooManyStreams
from app.services.stack_jobs import StackJobRegistry
from app.web.identifiers import PathId
from app.web.navigation import no_store
from app.web.stack_pages import RETRY_MILLISECONDS, JobResponse, job_frames
from app.web.stack_routes import JobId
from app.web.templating import render_partial

router = APIRouter(
    prefix="/environments/{environment_id}/stacks",
    tags=["stacks"],
    dependencies=[Depends(require_user)],
)

POLL_SECONDS: Final = 1.0
KEEP_ALIVE_SECONDS: Final = 15.0
LIFETIME_SECONDS: Final = 300.0
PARTS: Final = "stacks/_parts.html"
PROGRESS_MACRO: Final = "stack_job_progress"

__all__ = ["RETRY_MILLISECONDS", "router"]


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
        stack_kind="compose",
        template=PARTS,
        macro=PROGRESS_MACRO,
        poll=POLL_SECONDS,
        keep_alive=KEEP_ALIVE_SECONDS,
        lifetime=LIFETIME_SECONDS,
        retry=RETRY_MILLISECONDS,
    )


@router.get(
    "/-/jobs/{job_id}/events",
    response_class=StreamingResponse,
    name="environment_stack_job_stream",
)
async def job_stream(
    request: Request, actor: CurrentUser, environment_id: PathId, job_id: JobId
) -> Response:
    """Stream a stack operation as server-sent events; only its starter or an administrator."""
    jobs: StackJobRegistry = request.app.state.engine.stack_jobs
    slots: StreamSlots = request.app.state.engine.stream_slots
    async with app_stack_services(request.app) as stacks:
        await stacks.queries.require_environment(actor, environment_id)
    first = jobs.view(job_id, actor)
    if first.environment_id != environment_id or first.stack_kind != "compose":
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
