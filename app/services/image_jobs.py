"""In-process pull, push, container and onboarding review jobs: the registry and their progress."""

import asyncio
import contextvars
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
from typing import Final, Literal

from app.auth.errors import (
    PermissionDenied,
    ProductError,
    ResourceNotFound,
    SelfProtected,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.db.repositories.clocked import Clock, utc_now
from app.docker_engine.models import ProgressDetail, PullProgress, PushProgress
from app.logging import get_logger
from app.services.image_bindings import REBIND_REFUSED, RebindRefused
from app.services.image_views import (
    ImageChangedSinceReview,
    ImageGone,
    ImageHasDependents,
    ImageInUse,
    ImagePullFailed,
    ImagePushFailed,
)
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectBusy
from app.services.pull_credentials import (
    BindingCredentialMissing,
    CredentialChoiceRequired,
    CredentialSetMismatch,
)
from app.services.registry_resolution import CredentialSetUnavailable

logger = get_logger(__name__)

JOB_ID_BYTES: Final = 16
JOB_ID_PATTERN: Final = r"[A-Za-z0-9_-]{22}"
MAX_JOBS_PER_USER: Final = 2
MAX_JOBS_PER_PROCESS: Final = 8
MAX_FINISHED_JOBS: Final = 64
FINISHED_RETENTION: Final = timedelta(minutes=15)
MAX_TRACKED_LAYERS: Final = 128
SHUTDOWN_WAIT_SECONDS: Final = 10.0
NO_LONGER_PERMITTED: Final = "You no longer have permission for this action."

_LAYER_ID = re.compile(r"[0-9a-f]{12}")

type JobKind = Literal[
    "pull",
    "push",
    "container_create",
    "container_recreate",
    "onboarding_review",
    "anonymous_binding_review",
]
type JobState = Literal["running", "succeeded", "failed", "cancelled"]
type LayerStage = Literal[
    "queued",
    "waiting",
    "downloading",
    "verifying",
    "downloaded",
    "extracting",
    "complete",
    "exists",
    "preparing",
    "pushing",
    "pushed",
    "mounted",
    "working",
]

_STAGES: Final[Mapping[str, LayerStage]] = {
    "Pulling fs layer": "queued",
    "Waiting": "waiting",
    "Downloading": "downloading",
    "Verifying Checksum": "verifying",
    "Download complete": "downloaded",
    "Extracting": "extracting",
    "Pull complete": "complete",
    "Already exists": "exists",
    "Preparing": "preparing",
    "Pushing": "pushing",
    "Pushed": "pushed",
    "Layer already exists": "exists",
}
_MOUNTED_PREFIX: Final = "Mounted from"
_CONTAINER_KINDS: Final = frozenset({"container_create", "container_recreate"})
_REVIEW_KINDS: Final = frozenset({"onboarding_review", "anonymous_binding_review"})
_FRESH_CONTEXT_KINDS: Final = _CONTAINER_KINDS | _REVIEW_KINDS
_PULL_STAGE: Final = "pull"
_DONE: Final = frozenset({"complete", "exists", "pushed", "mounted"})
_TRANSFERRING: Final = frozenset({"downloading", "pushing"})


class TooManyJobs(ProductError):
    """A user or the process has no free job slot; the message names the limit reached."""

    def __init__(self, scope: Literal["user", "process"], noun: str = "transfers") -> None:
        if scope == "user":
            super().__init__(
                f"You have {MAX_JOBS_PER_USER} {noun} running. Wait for one to finish."
            )
        else:
            super().__init__(
                f"This server is running as many {noun} as it allows. Try again shortly."
            )
        self.scope = scope


@dataclass(frozen=True, slots=True)
class LayerView:
    """One layer's stage and byte counts; the Engine's own status text is never kept."""

    layer: str
    stage: LayerStage
    current: int
    total: int


@dataclass(frozen=True, slots=True)
class ProgressView:
    """The progress of a job: tracked layers, how many exist in all, and the byte totals.

    `percent` is None until some layer reports a total.
    """

    layers: tuple[LayerView, ...]
    layers_total: int
    layers_done: int
    current_bytes: int
    total_bytes: int
    stages: tuple[str, ...] = ()

    @property
    def percent(self) -> int | None:
        """Return the share of known bytes transferred, or None when none are known."""
        if self.total_bytes <= 0:
            return None
        return min(100, self.current_bytes * 100 // self.total_bytes)


@dataclass(frozen=True, slots=True)
class JobResult:
    """What a finished job reports on success."""

    message: str
    image_id: str | None = None
    binding: str | None = None
    container_id: str | None = None
    flags: Mapping[str, str] = field(default_factory=dict)


class JobCancelled(Exception):
    """Raised by a job's body to end it as cancelled with a message of its own."""


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """How a job ended; `reason` is a stable identifier and `message` is product-written.

    `flags` are the query values the synchronous result of a container create or recreate carries.
    `engine_detail` is the Engine's scrubbed text, kept only for a job an administrator started.
    """

    ok: bool
    message: str
    reason: str | None = None
    image_id: str | None = None
    binding: str | None = None
    container_id: str | None = None
    flags: Mapping[str, str] = field(default_factory=dict)
    engine_detail: str | None = None


_CANCELLED: Final = JobOutcome(
    False,
    "The transfer was cancelled. What it left on the host is not known.",
    "outcome_unknown",
)
_REVIEW_CANCELLED: Final = JobOutcome(
    False, "The review was cancelled. Nothing was written.", "cancelled"
)


@dataclass(frozen=True, slots=True)
class JobView:
    """A job as a page or an event stream renders it; it holds no secret and no Engine text.

    `version` changes whenever the progress or the state does.
    """

    job_id: str
    kind: JobKind
    environment_id: int
    owner_user_id: int
    reference: str
    state: JobState
    started_at: datetime
    finished_at: datetime | None
    progress: ProgressView
    outcome: JobOutcome | None
    version: int

    @property
    def running(self) -> bool:
        """Report whether the job has not ended."""
        return self.state == "running"


@dataclass(slots=True)
class _Layer:
    stage: LayerStage = "working"
    current: int = 0
    total: int = 0


class JobProgress:
    """Collects layer progress and stages for display; it never decides whether a job succeeded.

    `cancellable` says whether a cancel request may stop the job now. A `pull_only` collector is
    cancellable only while the stage is the pull.
    """

    def __init__(self, *, pull_only: bool = False) -> None:
        self._layers: dict[str, _Layer] = {}
        self._stages: list[str] = []
        self._untracked = 0
        self._pull_only = pull_only
        self.version = 0
        self.cancellable = not pull_only
        self.cancel_requested = False

    def on_pull(self, event: PullProgress) -> None:
        """Take one pull progress message."""
        self._take(event.id, event.status, event.progress_detail)

    def on_stage(self, stage: str) -> None:
        """Take the step a container create or recreate has reached."""
        if self._stages and self._stages[-1] == stage:
            return
        self._stages.append(stage)
        if self._pull_only:
            self.cancellable = stage == _PULL_STAGE
        self.version += 1

    def on_push(self, event: PushProgress) -> None:
        """Take one push progress message."""
        self._take(event.id, event.status, event.progress_detail)

    def snapshot(self) -> ProgressView:
        """Return the progress as it stands."""
        layers = tuple(
            LayerView(layer_id, layer.stage, layer.current, layer.total)
            for layer_id, layer in self._layers.items()
        )
        return ProgressView(
            layers=layers,
            layers_total=len(layers) + self._untracked,
            layers_done=sum(1 for layer in layers if layer.stage in _DONE),
            current_bytes=sum(layer.current for layer in layers if layer.total > 0),
            total_bytes=sum(layer.total for layer in layers),
            stages=tuple(self._stages),
        )

    def _take(self, layer_id: str | None, status: str, detail: ProgressDetail) -> None:
        if layer_id is None or _LAYER_ID.fullmatch(layer_id) is None:
            return
        layer = self._layers.get(layer_id)
        if layer is None:
            if len(self._layers) >= MAX_TRACKED_LAYERS:
                self._untracked += 1
                return
            layer = self._layers[layer_id] = _Layer()
        stage = _stage_of(status)
        layer.stage = stage
        if stage in _TRANSFERRING or stage in _DONE:
            layer.total = detail.total or layer.total
            layer.current = min(detail.current, layer.total) if layer.total else detail.current
        if stage in _DONE and layer.total:
            layer.current = layer.total
        self.version += 1


def _stage_of(status: str) -> LayerStage:
    if status.startswith(_MOUNTED_PREFIX):
        return "mounted"
    return _STAGES.get(status, "working")


@dataclass(eq=False, slots=True)
class _Job:
    id: str
    kind: JobKind
    owner_user_id: int
    environment_id: int
    reference: str
    started_at: datetime
    progress: JobProgress
    is_administrator: bool = False
    state: JobState = "running"
    finished_at: datetime | None = None
    outcome: JobOutcome | None = None
    task: asyncio.Task[None] | None = None
    state_version: int = 0


def outcome_of(error: Exception, kind: JobKind | None = None) -> JobOutcome:
    """Turn the exception that ended a job into its outcome; every message is product-written."""
    match error:
        case ImagePullFailed() | ImagePushFailed():
            return JobOutcome(False, str(error), error.reason)
        case EnvironmentUnavailable():
            return JobOutcome(False, str(error), error.kind)
        case ObjectBusy():
            return JobOutcome(False, str(error), "object_busy")
        case CredentialSetUnavailable():
            return JobOutcome(False, str(error), "credential_unavailable")
        case BindingCredentialMissing():
            return JobOutcome(False, str(error), "credential_missing")
        case CredentialChoiceRequired():
            return JobOutcome(False, str(error), "credential_choice_required")
        case CredentialSetMismatch():
            return JobOutcome(False, str(error), "credential_mismatch")
        case RebindRefused():
            return JobOutcome(False, REBIND_REFUSED, "forbidden")
        case PermissionDenied():
            return JobOutcome(False, NO_LONGER_PERMITTED, "forbidden")
        case ResourceNotFound():
            return JobOutcome(False, "The image or environment was not found.", "not_found")
        case ImageGone():
            return JobOutcome(False, str(error), "image_gone")
        case ImageChangedSinceReview():
            return JobOutcome(False, str(error), "changed")
        case ValidationFailed():
            return JobOutcome(False, str(error), "invalid")
        case ImageInUse():
            return JobOutcome(False, str(error), "image_in_use")
        case ImageHasDependents():
            return JobOutcome(False, str(error), "image_has_dependents")
        case ObjectTooLarge():
            return JobOutcome(False, str(error), "too_large")
        case SelfProtected():
            return JobOutcome(False, str(error), error.reason)
        case ProductError():
            return JobOutcome(False, str(error), "failed")
        case _:
            logger.error("image_job_crashed", extra={"error": type(error).__name__})
            text = "The review failed." if kind in _REVIEW_KINDS else "The transfer failed."
            return JobOutcome(False, text, "failed")


def _cancelled_for(job: _Job) -> JobOutcome:
    return _REVIEW_CANCELLED if job.kind in _REVIEW_KINDS else _CANCELLED


class ImageJobRegistry:
    """Runs pulls, pushes, container creates and recreates in the background and keeps their state.

    Process-wide and in memory. A job is visible to the user who started it and to
    administrators, and to nobody else. At most two jobs run per user and eight per process, the
    last process slot being kept for administrators;
    finished jobs are kept for 15 minutes, at most 64 of them.
    """

    def __init__(
        self,
        clock: Clock = utc_now,
        *,
        per_user: int = MAX_JOBS_PER_USER,
        per_process: int = MAX_JOBS_PER_PROCESS,
        keep: int = MAX_FINISHED_JOBS,
        retention: timedelta = FINISHED_RETENTION,
    ) -> None:
        self._clock = clock
        self._per_user = per_user
        self._per_process = per_process
        self._keep = keep
        self._retention = retention
        self._jobs: dict[str, _Job] = {}
        self._closed = False

    def __len__(self) -> int:
        return len(self._jobs)

    def running_count(self) -> int:
        """Return how many jobs have not finished."""
        return sum(1 for job in self._jobs.values() if job.state == "running")

    def start(
        self,
        kind: JobKind,
        owner: Principal,
        environment_id: int,
        reference: str,
        run: Callable[[JobProgress], Awaitable[JobResult]],
    ) -> str:
        """Start a job and return its id; TooManyJobs when the user or the process is at its limit.

        `run` receives the progress collector, and its failure or cancellation is the job's
        outcome. It must be called inside a running event loop. A container or onboarding job runs
        in a context of its own, so nothing the caller has open, such as an audit scope, reaches it.
        """
        if self._closed:
            raise RuntimeError("the job registry is closed")
        self._evict()
        noun = "jobs" if kind in _REVIEW_KINDS else "transfers"
        running = [job for job in self._jobs.values() if job.state == "running"]
        reserved = 0 if owner.is_administrator else 1
        if len(running) >= max(self._per_process - reserved, 0):
            raise TooManyJobs("process", noun)
        if sum(1 for job in running if job.owner_user_id == owner.user_id) >= self._per_user:
            raise TooManyJobs("user", noun)
        job_id = secrets.token_urlsafe(JOB_ID_BYTES)
        job = _Job(
            job_id,
            kind,
            owner.user_id,
            environment_id,
            reference,
            self._clock(),
            JobProgress(pull_only=kind in _CONTAINER_KINDS),
            owner.is_administrator,
        )
        self._jobs[job_id] = job
        job.task = asyncio.get_running_loop().create_task(
            self._main(job, run),
            context=contextvars.Context() if kind in _FRESH_CONTEXT_KINDS else None,
        )
        job.task.add_done_callback(partial(self._ended, job))
        return job_id

    def view(self, job_id: str, principal: Principal) -> JobView:
        """Return the job as the caller may see it; ResourceNotFound for anyone else's."""
        self._evict()
        job = self._visible(job_id, principal)
        return JobView(
            job_id=job.id,
            kind=job.kind,
            environment_id=job.environment_id,
            owner_user_id=job.owner_user_id,
            reference=job.reference,
            state=job.state,
            started_at=job.started_at,
            finished_at=job.finished_at,
            progress=job.progress.snapshot(),
            outcome=job.outcome,
            version=job.progress.version + job.state_version,
        )

    def cancel(self, job_id: str, principal: Principal) -> None:
        """Cancel a running job started by the caller, or any job for an administrator.

        The Engine stream is closed, which makes the daemon stop the transfer. Cancelling a
        finished job, or a container job that has finished pulling, does nothing.
        ResourceNotFound for anyone else's job.
        """
        job = self._visible(job_id, principal)
        if job.state == "running" and job.task is not None and job.progress.cancellable:
            job.progress.cancel_requested = True
            job.task.cancel()

    async def aclose(self) -> None:
        """Cancel every running job and wait for each to record its interruption."""
        self._closed = True
        tasks = [job.task for job in self._jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=SHUTDOWN_WAIT_SECONDS)

    def _visible(self, job_id: str, principal: Principal) -> _Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise ResourceNotFound("not found")
        if not principal.is_administrator and job.owner_user_id != principal.user_id:
            raise ResourceNotFound("not found")
        return job

    async def _main(self, job: _Job, run: Callable[[JobProgress], Awaitable[JobResult]]) -> None:
        try:
            result = await run(job.progress)
        except asyncio.CancelledError:
            self._finish(job, "cancelled", _cancelled_for(job))
        except JobCancelled as cancelled:
            self._finish(job, "cancelled", JobOutcome(False, str(cancelled), "cancelled"))
        except Exception as error:
            self._finish(job, "failed", self._failure_of(job, error))
        else:
            outcome = JobOutcome(
                True,
                result.message,
                None,
                result.image_id,
                result.binding,
                result.container_id,
                result.flags,
            )
            self._finish(job, "succeeded", outcome)

    def _failure_of(self, job: _Job, error: Exception) -> JobOutcome:
        outcome = outcome_of(error, job.kind)
        if job.kind in _CONTAINER_KINDS and outcome.reason == "not_found":
            outcome = replace(outcome, message="The container or environment was not found.")
        detail = getattr(error, "engine_detail", None)
        if job.is_administrator and isinstance(detail, str):
            return replace(outcome, engine_detail=detail)
        return outcome

    def _ended(self, job: _Job, task: asyncio.Task[None]) -> None:
        """Settle a job whose task was cancelled before its body ever ran."""
        if job.state == "running":
            self._finish(job, "cancelled", _cancelled_for(job))

    def _finish(self, job: _Job, state: JobState, outcome: JobOutcome) -> None:
        job.outcome = outcome
        job.state = state
        job.finished_at = self._clock()
        job.state_version += 1

    def _evict(self) -> None:
        now = self._clock()
        finished = sorted(
            (job for job in self._jobs.values() if job.finished_at is not None),
            key=lambda job: job.finished_at or now,
        )
        alive: list[_Job] = []
        for job in finished:
            if job.finished_at is not None and now - job.finished_at > self._retention:
                del self._jobs[job.id]
            else:
                alive.append(job)
        for job in alive[: max(0, len(alive) - self._keep)]:
            del self._jobs[job.id]
