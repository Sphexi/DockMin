"""In-process stack operation jobs: the registry that runs them and the progress they report."""

import asyncio
import secrets
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Final, Literal

from app.auth.errors import ProductError, ResourceNotFound
from app.auth.principal import Principal
from app.compose_cli.buildx import BuildEvent, LogLine, VertexUpdate
from app.compose_cli.parsers import ProgressEvent
from app.db.repositories.clocked import Clock, utc_now
from app.db.repositories.stack import StackKind
from app.logging import get_logger
from app.services.image_jobs import JobProgress, ProgressView
from app.services.image_jobs import outcome_of as image_outcome_of
from app.services.stack_deploy import JobResult, StackOperationFailed, StackRejected
from app.services.stack_views import StackOutcome

__all__ = [
    "FINISHED_RETENTION",
    "MAX_JOBS_PER_PROCESS",
    "MAX_JOBS_PER_USER",
    "BuildStepView",
    "BuildView",
    "JobResult",
    "PullView",
    "ResourceView",
    "StackJobKind",
    "StackJobOutcome",
    "StackJobRegistry",
    "StackJobState",
    "StackJobView",
    "StackProgress",
    "StackProgressView",
    "TooManyStackJobs",
]

logger = get_logger(__name__)

JOB_ID_BYTES: Final = 16
MAX_JOBS_PER_USER: Final = 2
MAX_JOBS_PER_PROCESS: Final = 4
MAX_FINISHED_JOBS: Final = 64
FINISHED_RETENTION: Final = timedelta(minutes=15)
MAX_TRACKED_PULLS: Final = 64
MAX_TRACKED_BUILDS: Final = 16
MAX_TRACKED_STEPS: Final = 256
MAX_BUILD_LOG_LINES: Final = 200
MAX_BUILD_LOG_CHARS: Final = 512
MAX_TRACKED_RESOURCES: Final = 500
SHUTDOWN_WAIT_SECONDS: Final = 10.0
MAX_RESOURCE_TEXT: Final = 512

type StackJobKind = Literal["create", "redeploy", "start", "stop", "restart", "remove", "adopt"]
type StackJobState = Literal["running", "succeeded", "failed", "cancelled"]

_RESOURCE_KINDS: Final = frozenset(
    {"Container", "Network", "Volume", "Image", "Service", "Config", "Secret"}
)


class TooManyStackJobs(ProductError):
    """A user or the process has no free job slot; the message names the limit reached."""

    def __init__(self, scope: Literal["user", "process"]) -> None:
        if scope == "user":
            super().__init__(
                f"You have {MAX_JOBS_PER_USER} stack operations running. Wait for one to finish."
            )
        else:
            super().__init__(
                "This server is running as many stack operations as it allows. Try again shortly."
            )
        self.scope = scope


@dataclass(frozen=True, slots=True)
class ResourceView:
    """The latest event of one Compose resource; text is Compose's, scrubbed, and shown escaped."""

    resource: str
    kind: str
    name: str
    status: str
    text: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class PullView:
    """The layer progress of one image pull."""

    reference: str
    progress: ProgressView


@dataclass(frozen=True, slots=True)
class BuildStepView:
    """One step of an image build as BuildKit last reported it; text is scrubbed."""

    name: str
    status: str
    cached: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class BuildView:
    """The progress of one service's image build: its steps and the tail of its output."""

    service: str
    tag: str
    steps: tuple[BuildStepView, ...]
    log: tuple[str, ...]
    more_steps: int


@dataclass(frozen=True, slots=True)
class StackProgressView:
    """Everything a job page shows while it runs: pulls, image builds, then Compose resources."""

    pulls: tuple[PullView, ...]
    resources: tuple[ResourceView, ...]
    untracked_resources: int
    builds: tuple[BuildView, ...] = ()


class BuildProgress:
    """Collects the steps and output of one image build for display; it decides nothing."""

    def __init__(self, service: str, tag: str) -> None:
        self._service = service
        self._tag = tag
        self._steps: dict[str, VertexUpdate] = {}
        self._untracked = 0
        self._log: deque[str] = deque(maxlen=MAX_BUILD_LOG_LINES)
        self._changes = 0

    @property
    def version(self) -> int:
        """Return a counter that changes whenever anything shown here changes."""
        return self._changes

    def on_event(self, event: BuildEvent) -> None:
        """Take one step update or output line."""
        if isinstance(event, LogLine):
            self._log.append(event.text[:MAX_BUILD_LOG_CHARS])
        elif event.digest in self._steps or len(self._steps) < MAX_TRACKED_STEPS:
            self._steps[event.digest] = event
        else:
            self._untracked += 1
        self._changes += 1

    def snapshot(self) -> BuildView:
        """Return the build as it stands."""
        return BuildView(
            service=self._service,
            tag=self._tag,
            steps=tuple(
                BuildStepView(step.name, step.status, step.cached, step.error)
                for step in self._steps.values()
            ),
            log=tuple(self._log),
            more_steps=self._untracked,
        )


class StackProgress:
    """Collects image pull and Compose resource progress for display.

    It decides nothing: a job's outcome comes from the operation, never from what was seen here.
    """

    def __init__(self) -> None:
        self._pulls: dict[str, JobProgress] = {}
        self._builds: dict[str, BuildProgress] = {}
        self._resources: dict[str, ResourceView] = {}
        self._untracked = 0
        self._changes = 0

    @property
    def version(self) -> int:
        """Return a counter that changes whenever anything shown here changes."""
        return (
            self._changes
            + sum(pull.version for pull in self._pulls.values())
            + sum(build.version for build in self._builds.values())
        )

    def pull(self, reference: str) -> JobProgress:
        """Return the collector for one image's pull, creating it on first use."""
        progress = self._pulls.get(reference)
        if progress is None:
            progress = JobProgress()
            if len(self._pulls) < MAX_TRACKED_PULLS:
                self._pulls[reference] = progress
        return progress

    def build(self, service: str, tag: str) -> Callable[[BuildEvent], None]:
        """Return the callback that takes the events of one service's build."""
        progress = self._builds.get(service)
        if progress is None:
            progress = BuildProgress(service, tag)
            if len(self._builds) < MAX_TRACKED_BUILDS:
                self._builds[service] = progress
        self._changes += 1
        return progress.on_event

    def on_event(self, event: ProgressEvent) -> None:
        """Take one Compose progress event; the latest event of a resource replaces the last."""
        resource = event.resource
        if resource is None:
            return
        view = _resource_view(resource, event)
        if resource not in self._resources:
            if len(self._resources) >= MAX_TRACKED_RESOURCES:
                self._untracked += 1
                self._changes += 1
                return
        elif self._resources[resource] == view:
            return
        self._resources[resource] = view
        self._changes += 1

    def snapshot(self) -> StackProgressView:
        """Return the progress as it stands."""
        return StackProgressView(
            pulls=tuple(
                PullView(reference, progress.snapshot())
                for reference, progress in self._pulls.items()
            ),
            resources=tuple(self._resources.values()),
            untracked_resources=self._untracked,
            builds=tuple(build.snapshot() for build in self._builds.values()),
        )


def _resource_view(resource: str, event: ProgressEvent) -> ResourceView:
    kind, _, name = resource.partition(" ")
    if not name or kind not in _RESOURCE_KINDS:
        kind, name = "Resource", resource
    return ResourceView(
        resource=resource,
        kind=kind,
        name=name,
        status=(event.status or "").casefold(),
        text=(event.text or "")[:MAX_RESOURCE_TEXT],
        is_error=event.is_error,
    )


@dataclass(frozen=True, slots=True)
class StackJobOutcome:
    """How a job ended.

    `reason` is a stable identifier and `message` is product-written. `stderr` is Compose's
    stderr after scrubbing, shown only in an escaped block; `progress` is a short summary of
    the last event of each resource.
    """

    ok: bool
    message: str
    reason: str | None = None
    exit_code: int | None = None
    stderr: str | None = None
    events: int = 0
    progress: str | None = None
    result: JobResult | None = None


_CANCELLED: Final = StackJobOutcome(
    False,
    "The operation was interrupted. What it left on the host is not known.",
    "outcome_unknown",
)


@dataclass(frozen=True, slots=True)
class StackJobView:
    """A job as a page or an event stream renders it; it holds no secret and no variable value.

    `version` changes whenever the progress or the state does.
    """

    job_id: str
    kind: StackJobKind
    environment_id: int
    owner_user_id: int
    stack: str
    state: StackJobState
    started_at: datetime
    finished_at: datetime | None
    progress: StackProgressView
    outcome: StackJobOutcome | None
    version: int
    stack_kind: StackKind = "compose"

    @property
    def running(self) -> bool:
        """Report whether the job has not ended."""
        return self.state == "running"


@dataclass(eq=False, slots=True)
class _Job:
    id: str
    kind: StackJobKind
    owner_user_id: int
    environment_id: int
    stack: str
    started_at: datetime
    progress: StackProgress = field(default_factory=StackProgress)
    state: StackJobState = "running"
    finished_at: datetime | None = None
    outcome: StackJobOutcome | None = None
    task: asyncio.Task[None] | None = None
    state_version: int = 0
    stack_kind: StackKind = "compose"


def outcome_of(error: Exception) -> StackJobOutcome:
    """Turn the exception that ended a job into its outcome; every message is product-written."""
    match error:
        case StackOperationFailed():
            return StackJobOutcome(
                False,
                str(error),
                error.reason,
                exit_code=error.exit_code,
                stderr=error.stderr or None,
                events=error.event_count,
                progress=error.progress or None,
                result=error.result,
            )
        case StackRejected():
            return StackJobOutcome(False, str(error), error.reason, stderr=error.stderr or None)
        case ProductError():
            base = image_outcome_of(error)
            return StackJobOutcome(False, base.message, base.reason)
        case _:
            logger.error("stack_job_crashed", extra={"error": type(error).__name__})
            return StackJobOutcome(False, "The operation failed.", "failed")


class StackJobRegistry:
    """Runs stack operations in the background and keeps their state for the pages that watch.

    Process-wide and in memory. A job is visible to the user who started it and to
    administrators, and to nobody else. At most two jobs run per user and four per process;
    finished jobs are kept for 15 minutes, at most 64 of them. There is no cancel: the
    operation is shielded from its caller, and leaving the page does not stop it.
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
        self._reserved: dict[int, int] = {}
        self._closed = False

    def __len__(self) -> int:
        return len(self._jobs)

    def running_count(self) -> int:
        """Return how many jobs have not finished, counting operations about to start."""
        return sum(1 for job in self._jobs.values() if job.state == "running") + sum(
            self._reserved.values()
        )

    def start(
        self,
        kind: StackJobKind,
        owner: Principal,
        environment_id: int,
        stack: str,
        run: Callable[[StackProgress], Awaitable[JobResult]],
        *,
        stack_kind: StackKind = "compose",
    ) -> str:
        """Start a job and return its id; TooManyStackJobs when a limit is reached.

        `run` receives the progress collector; its failure or cancellation is the job's outcome.
        `stack_kind` tags the job with the kind of stack it works on.
        It must be called inside a running event loop.
        """
        if self._closed:
            raise RuntimeError("the job registry is closed")
        self._evict()
        self._require_room(owner)
        job_id = secrets.token_urlsafe(JOB_ID_BYTES)
        job = _Job(
            job_id, kind, owner.user_id, environment_id, stack, self._clock(), stack_kind=stack_kind
        )
        self._jobs[job_id] = job
        job.task = asyncio.get_running_loop().create_task(self._main(job, run))
        job.task.add_done_callback(partial(self._ended, job))
        return job_id

    @contextmanager
    def reserve(self, owner: Principal) -> Iterator[None]:
        """Count an operation run in the caller's own request against the same limits.

        TooManyStackJobs when a limit is reached; the slot is held until the block ends.
        """
        if self._closed:
            raise RuntimeError("the job registry is closed")
        self._evict()
        self._require_room(owner)
        self._reserved[owner.user_id] = self._reserved.get(owner.user_id, 0) + 1
        try:
            yield
        finally:
            self._reserved[owner.user_id] -= 1

    def view(self, job_id: str, principal: Principal) -> StackJobView:
        """Return the job as the caller may see it; ResourceNotFound for anyone else's."""
        self._evict()
        job = self._visible(job_id, principal)
        return StackJobView(
            job_id=job.id,
            kind=job.kind,
            environment_id=job.environment_id,
            owner_user_id=job.owner_user_id,
            stack=job.stack,
            state=job.state,
            started_at=job.started_at,
            finished_at=job.finished_at,
            progress=job.progress.snapshot(),
            outcome=job.outcome,
            version=job.progress.version + job.state_version,
            stack_kind=job.stack_kind,
        )

    async def aclose(self) -> None:
        """Cancel every running job and wait for each to record its interruption."""
        self._closed = True
        tasks = [job.task for job in self._jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=SHUTDOWN_WAIT_SECONDS)

    def _require_room(self, owner: Principal) -> None:
        running = [job.owner_user_id for job in self._jobs.values() if job.state == "running"]
        if len(running) + sum(self._reserved.values()) >= self._per_process:
            raise TooManyStackJobs("process")
        own = running.count(owner.user_id) + self._reserved.get(owner.user_id, 0)
        if own >= self._per_user:
            raise TooManyStackJobs("user")

    def _visible(self, job_id: str, principal: Principal) -> _Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise ResourceNotFound("not found")
        if not principal.is_administrator and job.owner_user_id != principal.user_id:
            raise ResourceNotFound("not found")
        return job

    async def _main(self, job: _Job, run: Callable[[StackProgress], Awaitable[JobResult]]) -> None:
        try:
            result = await run(job.progress)
        except asyncio.CancelledError:
            self._finish(job, "cancelled", _CANCELLED)
        except Exception as error:
            self._finish(job, "failed", outcome_of(error))
        else:
            exit_code, events = (
                (result.exit_code, result.events) if isinstance(result, StackOutcome) else (None, 0)
            )
            self._finish(
                job,
                "succeeded",
                StackJobOutcome(
                    True, result.message, exit_code=exit_code, events=events, result=result
                ),
            )

    def _ended(self, job: _Job, task: asyncio.Task[None]) -> None:
        """Settle a job whose task was cancelled before its body ever ran."""
        if job.state == "running":
            self._finish(job, "cancelled", _CANCELLED)

    def _finish(self, job: _Job, state: StackJobState, outcome: StackJobOutcome) -> None:
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
