"""The server-sent event stream of a pull or push job."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from functools import partial
from typing import Final

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.services.assembly import app_image_services
from app.services.image_jobs import ImageJobRegistry, JobView
from app.services.object_locks import StreamSlot, StreamSlots, TooManyStreams
from app.web.identifiers import PathId
from app.web.image_helpers import PREFIX, JobId
from app.web.navigation import no_store
from app.web.templating import render_fragment, render_partial

router = APIRouter(prefix=PREFIX, tags=["images"], dependencies=[Depends(require_user)])

SSE_MEDIA_TYPE: Final = "text/event-stream"
STREAM_HEADERS: Final = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
POLL_SECONDS: Final = 1.0
KEEP_ALIVE_SECONDS: Final = 15.0
LIFETIME_SECONDS: Final = 300.0
RETRY_MILLISECONDS: Final = 3000
PROGRESS_EVENT: Final = "progress"
DONE_EVENT: Final = "done"
PARTS: Final = "images/_parts.html"
TRANSFER_KINDS: Final = frozenset({"pull", "push"})


def sse_event(name: str, data: str) -> str:
    lines = data.replace("\r", "").split("\n")
    return f"event: {name}\n" + "".join(f"data: {line}\n" for line in lines) + "\n"


def _render(request: Request, job: object) -> str:
    response = render_fragment(request, PARTS, "job_progress", job=job)
    return bytes(response.body).decode("utf-8").strip()


async def job_frames(
    render: Callable[[JobView], str],
    jobs: ImageJobRegistry,
    actor: Principal,
    environment_id: int,
    job_id: str,
    slot: StreamSlot,
) -> AsyncGenerator[str, None]:
    """Send the job's state whenever it changes, then a last event when it has ended.

    The registry is in memory, so the loop holds no database session. A quiet interval is a
    comment line, and after the lifetime the stream ends with a retry hint so the browser asks
    again and is authorised again.
    """
    loop = asyncio.get_running_loop()
    started = quiet_since = loop.time()
    seen: int | None = None
    try:
        while True:
            try:
                job = jobs.view(job_id, actor)
            except ResourceNotFound:
                return
            if job.environment_id != environment_id:
                return
            if job.version != seen:
                seen = job.version
                quiet_since = loop.time()
                yield sse_event(PROGRESS_EVENT if job.running else DONE_EVENT, render(job))
                if not job.running:
                    return
            elif loop.time() - quiet_since >= KEEP_ALIVE_SECONDS:
                quiet_since = loop.time()
                yield ": keep-alive\n\n"
            if loop.time() - started >= LIFETIME_SECONDS:
                yield f"retry: {RETRY_MILLISECONDS}\n\n"
                return
            await asyncio.sleep(POLL_SECONDS)
    finally:
        slot.close()


class JobResponse(StreamingResponse):
    """A stream response that gives its slot back on every way out, even before the first chunk."""

    def __init__(self, slot: StreamSlot, content: AsyncGenerator[str, None]) -> None:
        super().__init__(content, media_type=SSE_MEDIA_TYPE, headers=STREAM_HEADERS)
        self._slot = slot
        self._content = content

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._content.aclose()
                self._slot.close()


@router.get(
    "/jobs/{job_id}/events", response_class=StreamingResponse, name="environment_image_job_stream"
)
async def job_stream(
    request: Request, actor: CurrentUser, environment_id: PathId, job_id: JobId
) -> Response:
    """Stream a pull or push as server-sent events; only its starter or an administrator."""
    jobs: ImageJobRegistry = request.app.state.engine.image_jobs
    slots: StreamSlots = request.app.state.engine.stream_slots
    async with app_image_services(request.app) as images:
        await images.queries.require_visible(actor, environment_id)
    first = jobs.view(job_id, actor)
    if first.environment_id != environment_id or first.kind not in TRANSFER_KINDS:
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
        return JobResponse(
            slot, job_frames(partial(_render, request), jobs, actor, environment_id, job_id, slot)
        )
    except BaseException:
        slot.close()
        raise
