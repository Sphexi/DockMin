"""Replacing dockmin's own container: refusals, the update plan, the helper and its outcome."""

import asyncio
import contextvars
import json
import os
import re
import stat
import tempfile
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from starlette.applications import Starlette

from app.auth.errors import ProductError
from app.auth.principal import Principal
from app.backup.paths import remove_directory
from app.config import DB_FILENAME
from app.db.repositories.activity_event import (
    MAX_DETAIL_TEXT,
    ActivityEvent,
    ActivityEventRepository,
    ActivityFilter,
    DetailValue,
)
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.setting import SettingRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, NotFound
from app.docker_engine.models import ContainerInspect, ContainerSummary, ImageInspect
from app.logging import DEFAULT_SCRUBBER, get_logger
from app.registry_client.references import (
    ImageReference,
    ReferenceInvalid,
    digests_for,
    parse_reference,
)
from app.self_identity import source_of
from app.services.activity import ActivityRecorder, UserActor
from app.services.assembly import build_host_clients, open_image_services
from app.services.container_derivation import (
    BLOCKER_MESSAGES,
    DaemonDefaults,
    derive_recreate,
    image_is_id,
    recreate_blockers,
)
from app.services.container_review import container_document, image_document
from app.services.containers import display_name
from app.services.pull_credentials import CredentialChoice

logger = get_logger(__name__)

DEFAULT_STOP_TIMEOUT: Final = 45
MAX_DISPLAYED_NAME: Final = 128
UPDATE_DIRECTORY: Final = "update"
PLAN_FILE: Final = "plan.json"
RESULT_FILE: Final = "result.json"
RECORDED_FILE: Final = "recorded"
SCHEMA_VERSION: Final = 1
OUTCOME_SETTING: Final = "instance_update_outcome"
PREVIOUS_SUFFIX: Final = "-previous"
HELPER_SUFFIX: Final = "-updater"
HELPER_ROLE_LABEL: Final = "dockmin.role"
HELPER_ROLE: Final = "updater"
UPDATE_ID_LABEL: Final = "dockmin.update.id"
UPDATE_ID_ENVIRONMENT: Final = "DOCKMIN_UPDATE_ID"
HELPER_USER: Final = "10001:10001"
HELPER_SOCKET_PATH: Final = "/var/run/docker.sock"
HELPER_DATA_PATH: Final = "/data"
DATABASE_FILES: Final = (DB_FILENAME, f"{DB_FILENAME}-wal", f"{DB_FILENAME}-shm")
UPDATE_ID_BYTES: Final = 16
RESULT_POLL_SECONDS: Final = 2.0
HELPER_EXIT_WAIT_SECONDS: Final = 60
HELPER_CHECK_POLLS: Final = 5
MAX_STOP_TIMEOUT_SECONDS: Final = 3600
WATCH_MARGIN_SECONDS: Final = 600
WATCH_FALLBACK_SECONDS: Final = 1800
MAX_PROGRESS_ENTRIES: Final = 8
MAX_PLAN_BYTES: Final = 4 * 1024 * 1024
MAX_RESULT_BYTES: Final = 1024 * 1024
MAX_EXCERPT_LINES: Final = 200
MAX_EXCERPT_LINE_CHARS: Final = 1024
MAX_STORED_EXCERPT_BYTES: Final = 64 * 1024
COMPOSE_UPDATE_COMMANDS: Final = ("docker compose pull", "docker compose up -d")

UPDATE_ID_PATTERN: Final = re.compile(r"[0-9a-f]{32}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")
_API_VERSION = re.compile(r"\d{1,2}\.\d{1,3}")

type UpdateRefusal = Literal[
    "not_containerised",
    "identity_incomplete",
    "compose_project",
    "swarm_task",
    "not_restartable",
    "restore_in_progress",
    "rekey_in_progress",
    "update_in_progress",
    "container_missing",
    "container_unreadable",
    "no_repository",
    "no_digest",
    "no_socket_mount",
    "no_data_mount",
    "previous_exists",
    "not_recreatable",
    "record_failed",
    "stale_review",
    "digest_changed",
    "target_invalid",
    "credential_unavailable",
    "registry_failed",
]
type AbandonReason = Literal["pull_failed", "digest_changed", "plan_failed", "helper_failed"]
type UpdatePhase = Literal[
    "pulling_target",
    "pulling_helper",
    "preparing",
    "starting_helper",
    "handed_over",
    "abandoned",
    "finished",
]
type ResultOutcome = Literal["succeeded", "rolled_back", "rollback_failed", "not_started"]

UPDATE_REFUSALS: Final[Mapping[UpdateRefusal, str]] = {
    "not_containerised": (
        "dockmin could not identify a container of its own, so it cannot update itself."
    ),
    "identity_incomplete": (
        "Some environments could not be read, so dockmin cannot yet be sure which container it "
        "runs in. Try again shortly."
    ),
    "compose_project": (
        "dockmin runs in a Compose project, which owns its container. Update it with the "
        "Compose commands below, run in the project's directory."
    ),
    "swarm_task": (
        "dockmin runs as a Swarm service task, which cannot be replaced from here. Update the "
        "service instead."
    ),
    "not_restartable": (
        "dockmin could not confirm its container through an environment on the local Docker "
        "socket, so it cannot update itself."
    ),
    "restore_in_progress": "A restore is in progress. Updating is unavailable until it finishes.",
    "rekey_in_progress": (
        "Stored secrets are being re-encrypted. Updating is unavailable until it finishes."
    ),
    "update_in_progress": (
        "An update is in progress, or an earlier one left files in the data directory. "
        "Updating is unavailable until they are gone."
    ),
    "container_missing": "dockmin's container is no longer on the host that was used to find it.",
    "container_unreadable": "dockmin's container could not be read from the host.",
    "no_repository": (
        "The image dockmin runs from does not name a registry repository, so there is nothing "
        "to update from."
    ),
    "no_digest": (
        "The image dockmin runs from carries no registry digest, as with an image built on this "
        "host, so it cannot be compared with a registry."
    ),
    "no_socket_mount": (
        "dockmin's container does not mount the Docker socket its local environment uses, which "
        "the update helper needs."
    ),
    "no_data_mount": (
        "dockmin's data directory is not on a volume or a bind mount, so its database cannot be "
        "carried over."
    ),
    "previous_exists": "A container named for the previous version already exists.",
    "not_recreatable": "This container's configuration cannot be reproduced for a replacement.",
    "record_failed": "The update could not be recorded, so it was not started. Try again.",
    "stale_review": "dockmin changed since you reviewed it. Review the update again.",
    "digest_changed": "The registry's image changed since you reviewed it. Check again.",
    "target_invalid": "Choose one of the offered tags.",
    "credential_unavailable": "That registry credential set is not available here.",
    "registry_failed": "The registry could not be read.",
}
_ABANDON_TEXT: Final[Mapping[AbandonReason, str]] = {
    "pull_failed": "An image could not be pulled. dockmin was not changed.",
    "digest_changed": "The pulled image is not the one that was reviewed. dockmin was not changed.",
    "plan_failed": "The replacement could not be prepared. dockmin was not changed.",
    "helper_failed": "The update helper could not be started. dockmin was not changed.",
}
_FAILURE_TEXT: Final[Mapping[str, str]] = {
    "unhealthy": "The new container reported unhealthy.",
    "health_timeout": "The new container did not become healthy in time.",
    "exited": "The new container exited.",
    "create_failed": "The new container could not be created.",
    "start_failed": "The new container could not be started.",
    "copy_failed": "The database could not be copied.",
    "engine_error": "The Docker host returned an error.",
    "previous_exists": "A container for the previous version already existed.",
    "locked": "Another update already holds the update lock.",
    "helper_lost": "The update helper stopped before it reported a result.",
    "result_unreadable": "The update helper's result could not be read.",
    "image_changed": "The pulled image was no longer the planned one.",
    "plan_invalid": "The update plan could not be read.",
    "rollback_failed": "The new container failed and the previous one could not be restored.",
}
_ROLLED_BACK_SUFFIX: Final = " The previous container was started again."


class UpdateRefused(ProductError):
    """The instance cannot be updated now, or the request is stale; the message is product text."""

    def __init__(self, reason: UpdateRefusal, message: str | None = None) -> None:
        super().__init__(message or UPDATE_REFUSALS[reason])
        self.reason = reason


@dataclass(frozen=True, slots=True)
class HelperMount:
    """A volume or host path the helper mounts, as the own container mounts it."""

    kind: Literal["bind", "volume"]
    source: str


@dataclass(frozen=True, slots=True)
class OwnContainer:
    """The container dockmin runs in, read for an update.

    `document` and `image_document` feed the derivation of the replacement.
    """

    container_id: str
    name: str
    stop_timeout: int
    reference: ImageReference
    reference_text: str
    digest: str
    socket_source: str
    data_mount: HelperMount
    group_add: tuple[str, ...]
    document: dict[str, Any]
    image: ImageInspect | None

    @property
    def image_document(self) -> dict[str, Any] | None:
        """Return the old image's configuration for the derivation, or None when it is gone."""
        return image_document(self.image)


def stop_timeout_of(configured: int | None) -> int:
    """Return the seconds a stop waits: the container's own setting or the default."""
    return DEFAULT_STOP_TIMEOUT if configured is None or configured < 0 else configured


async def read_own_container(
    client: EngineClient, *, container_id: str, socket_path: str, data_dir: Path
) -> OwnContainer:
    """Read the container and its image and apply every refusal that needs the Engine.

    Raises UpdateRefused. The container is requested by id and refused unless the Engine
    answers with exactly that id.
    """
    try:
        inspected = await client.containers.inspect(container_id)
    except NotFound:
        raise UpdateRefused("container_missing") from None
    except EngineError:
        raise UpdateRefused("container_unreadable") from None
    if inspected.id != container_id:
        raise UpdateRefused("container_missing")
    name = display_name([inspected.name], inspected.id)[:MAX_DISPLAYED_NAME]
    reference_text = inspected.config.image
    reference = _reference_of(reference_text)
    try:
        image = await client.images.inspect(inspected.image)
    except NotFound:
        image = None
    except EngineError:
        raise UpdateRefused("container_unreadable") from None
    digest = reference.digest or next(
        iter(digests_for(image.repo_digests if image else [], reference.repository)), None
    )
    if digest is None:
        raise UpdateRefused("no_digest")
    socket_source = _socket_source(inspected, socket_path)
    data_mount = _data_mount(inspected, data_dir)
    previous = f"{name}{PREVIOUS_SUFFIX}"
    try:
        listed = await client.containers.list(all=True, filters={"name": [previous]})
    except EngineError:
        raise UpdateRefused("container_unreadable") from None
    if any(f"/{previous}" in summary.names for summary in listed):
        raise UpdateRefused(
            "previous_exists",
            f"A container named {previous} already exists. Remove it to allow an update.",
        )
    document = container_document(inspected)
    blockers = recreate_blockers(document, pull=True)
    if blockers:
        raise UpdateRefused(
            "not_recreatable", " ".join(BLOCKER_MESSAGES[blocker] for blocker in blockers)
        )
    groups = inspected.host_config.get("GroupAdd")
    return OwnContainer(
        container_id=inspected.id,
        name=name,
        stop_timeout=min(stop_timeout_of(inspected.config.stop_timeout), MAX_STOP_TIMEOUT_SECONDS),
        reference=reference,
        reference_text=reference_text,
        digest=digest,
        socket_source=socket_source,
        data_mount=data_mount,
        group_add=tuple(g for g in groups if isinstance(g, str))
        if isinstance(groups, list)
        else (),
        document=document,
        image=image,
    )


def _reference_of(text: str) -> ImageReference:
    if image_is_id(text):
        raise UpdateRefused("no_repository")
    try:
        return parse_reference(text)
    except ReferenceInvalid:
        raise UpdateRefused("no_repository") from None


def _socket_source(inspected: ContainerInspect, socket_path: str) -> str:
    for mount in inspected.mounts:
        if mount.type == "bind" and mount.destination == socket_path and mount.source:
            return mount.source
    raise UpdateRefused("no_socket_mount")


def _data_mount(inspected: ContainerInspect, data_dir: Path) -> HelperMount:
    for mount in inspected.mounts:
        if mount.destination != str(data_dir) or not mount.read_write:
            continue
        if mount.type == "volume" and mount.name:
            return HelperMount("volume", mount.name)
        if mount.type == "bind" and mount.source:
            return HelperMount("bind", mount.source)
    raise UpdateRefused("no_data_mount")


def repository_name(reference: ImageReference) -> str:
    """Return the reference's name as people write it, without its tag or digest."""
    if reference.digest is not None:
        return reference.familiar.partition("@")[0]
    return reference.familiar.removesuffix(f":{reference.tag}")


def new_update_id() -> str:
    """Return a random update id: 32 lower-case hexadecimal digits."""
    return os.urandom(UPDATE_ID_BYTES).hex()


@dataclass(frozen=True, slots=True)
class UpdateJob:
    """An accepted update, handed to the dispatcher after its request row is committed."""

    update_id: str
    principal: Principal
    request_event_id: int
    environment_id: int
    container_id: str
    container_name: str
    target_reference: str
    helper_reference: str
    target_digest: str
    helper_digest: str
    choice: CredentialChoice
    health_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class UpdateProgress:
    """Where one update is."""

    update_id: str
    phase: UpdatePhase
    reason: str | None = None
    message: str | None = None
    outcome: ResultOutcome | None = None


@dataclass(frozen=True, slots=True)
class UpdatePlan:
    """The plan file the helper reads; the helper validates it again."""

    update_id: str
    created_at: str
    requested_by_user_id: int
    request_event_id: int
    engine_api_version: str
    old_id: str
    old_name: str
    stop_timeout: int
    old_image: str
    new_image: str
    new_image_id: str
    create_body: dict[str, Any]
    health_timeout_seconds: int

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form of the plan."""
        return {
            "schema_version": SCHEMA_VERSION,
            "update_id": self.update_id,
            "created_at": self.created_at,
            "requested_by_user_id": self.requested_by_user_id,
            "request_event_id": self.request_event_id,
            "engine_api_version": self.engine_api_version,
            "old": {
                "id": self.old_id,
                "name": self.old_name,
                "stop_timeout": self.stop_timeout,
                "image": self.old_image,
            },
            "new": {
                "name": self.old_name,
                "image": self.new_image,
                "image_id": self.new_image_id,
                "create_body": self.create_body,
            },
            "health_timeout_seconds": self.health_timeout_seconds,
            "database_files": list(DATABASE_FILES),
        }


class _PlanOld(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: Annotated[str, StringConstraints(pattern=_CONTAINER_ID.pattern)]
    name: str


class _PlanDocument(BaseModel):
    """The parts of a plan the instance reads back when it records the outcome."""

    model_config = ConfigDict(extra="ignore", strict=True)

    update_id: Annotated[str, StringConstraints(pattern=UPDATE_ID_PATTERN.pattern)]
    requested_by_user_id: Annotated[int, Field(ge=1)]
    request_event_id: Annotated[int, Field(ge=1)]
    old: _PlanOld
    health_timeout_seconds: Annotated[int, Field(ge=1)]


_OUTCOME_REASONS: Final[Mapping[str, frozenset[str]]] = {
    "succeeded": frozenset(),
    "rolled_back": frozenset(
        {
            "unhealthy",
            "health_timeout",
            "exited",
            "create_failed",
            "start_failed",
            "copy_failed",
            "image_changed",
            "engine_error",
        }
    ),
    "rollback_failed": frozenset(
        {
            "unhealthy",
            "health_timeout",
            "exited",
            "create_failed",
            "start_failed",
            "copy_failed",
            "image_changed",
            "engine_error",
        }
    ),
    "not_started": frozenset(
        {"previous_exists", "image_changed", "plan_invalid", "locked", "engine_error"}
    ),
}


class UpdateResult(BaseModel):
    """The result file the helper writes."""

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    schema_version: Literal[1]
    update_id: Annotated[str, StringConstraints(pattern=UPDATE_ID_PATTERN.pattern)]
    outcome: ResultOutcome
    reason: str | None
    finished_at: Annotated[str, StringConstraints(pattern=_TIMESTAMP.pattern)]
    new_container_id: Annotated[str, StringConstraints(pattern=_CONTAINER_ID.pattern)] | None
    log_excerpt: Annotated[
        list[Annotated[str, StringConstraints(max_length=MAX_EXCERPT_LINE_CHARS)]],
        Field(max_length=MAX_EXCERPT_LINES),
    ]

    @model_validator(mode="after")
    def _reason_fits_outcome(self) -> "UpdateResult":
        allowed = _OUTCOME_REASONS[self.outcome]
        if self.outcome == "succeeded":
            if self.reason is not None:
                raise ValueError("a succeeded update has no reason")
        elif self.reason not in allowed:
            raise ValueError("the reason does not fit the outcome")
        return self


class _Abandon(Exception):
    def __init__(self, reason: AbandonReason) -> None:
        super().__init__(reason)
        self.reason = reason


def update_root(data_dir: Path) -> Path:
    """Return the directory that holds one subdirectory per update."""
    return data_dir / UPDATE_DIRECTORY


def reserve_update(data_dir: Path, update_id: str) -> None:
    """Create the update's directory (0700), which marks an update as in progress; blocks.

    Raises FileExistsError when the update already has one.
    """
    root = update_root(data_dir)
    if root.is_symlink():
        raise OSError("the update directory is a symbolic link")
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    directory = root / update_id
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)


def _update_ids(data_dir: Path) -> list[str]:
    root = update_root(data_dir)
    if root.is_symlink() or not root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in root.iterdir()
        if UPDATE_ID_PATTERN.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink()
    )


def _write_private(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    _sync_directory(path.parent)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_plan(data_dir: Path, plan: UpdatePlan) -> None:
    """Write the plan into the reserved update directory atomically (0600); blocks."""
    directory = update_root(data_dir) / plan.update_id
    if not directory.is_dir() or directory.is_symlink():
        raise OSError("the update directory was not reserved")
    payload = json.dumps(plan.to_dict(), separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_PLAN_BYTES:
        raise OSError("the update plan is too large")
    _write_private(directory / PLAN_FILE, payload)


def _read_bounded(path: Path, limit: int) -> bytes | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"{path.name} is not a regular file")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise OSError(f"{path.name} is too large")
    return raw


@dataclass(frozen=True, slots=True)
class _Marker:
    event_id: int | None
    keep: bool


@dataclass(frozen=True, slots=True)
class _Found:
    plan: _PlanDocument | None
    result: UpdateResult | None
    result_unreadable: bool
    recorded: _Marker | None


def _load(directory: Path) -> _Found:
    plan: _PlanDocument | None = None
    result: UpdateResult | None = None
    unreadable = False
    try:
        raw = _read_bounded(directory / PLAN_FILE, MAX_PLAN_BYTES)
        plan = None if raw is None else _PlanDocument.model_validate_json(raw)
    except (OSError, ValidationError):
        plan = None
    try:
        raw = _read_bounded(directory / RESULT_FILE, MAX_RESULT_BYTES)
        result = None if raw is None else UpdateResult.model_validate_json(raw)
    except (OSError, ValidationError):
        unreadable = True
    recorded: _Marker | None = None
    try:
        raw = _read_bounded(directory / RECORDED_FILE, 1024)
        if raw is not None:
            stored = json.loads(raw)
            event = stored.get("event_id")
            recorded = _Marker(
                event if isinstance(event, int) and not isinstance(event, bool) else None,
                stored.get("keep") is True,
            )
    except (OSError, ValueError, AttributeError):
        recorded = _Marker(None, False)
    return _Found(plan, result, unreadable, recorded)


def _write_marker(directory: Path, event_id: int | None, *, keep: bool) -> None:
    payload = json.dumps({"event_id": event_id, "keep": keep}).encode("utf-8")
    _write_private(directory / RECORDED_FILE, payload)


def _scrubbed(lines: Sequence[str]) -> list[str]:
    return [DEFAULT_SCRUBBER.scrub_text(line)[:MAX_EXCERPT_LINE_CHARS] for line in lines]


def newest_lines_within(lines: Sequence[str], limit: int) -> str:
    """Return the newest lines, oldest first, whose scrubbed text fits the limit.

    The text is returned unscrubbed, for a writer that scrubs it; scrubbing twice mangles a
    redaction marker.
    """
    kept: list[str] = []
    for line in reversed(lines):
        candidate = " | ".join([line, *kept])
        if len(DEFAULT_SCRUBBER.scrub_text(candidate)) > limit:
            break
        kept = [line, *kept]
    return " | ".join(kept)


def _stored_excerpt(lines: Sequence[str]) -> list[str]:
    kept: list[str] = []
    used = 0
    for line in reversed(lines[-MAX_EXCERPT_LINES:]):
        used += len(line.encode("utf-8")) + 1
        if used > MAX_STORED_EXCERPT_BYTES:
            break
        kept.append(line)
    return list(reversed(kept))


@dataclass(frozen=True, slots=True)
class _Outcome:
    """How an update ended, from the helper's result or from the helper's absence."""

    label: ResultOutcome
    reason: str | None
    cause: str | None
    message: str
    finished_at: str
    new_container_id: str | None
    excerpt: tuple[str, ...]

    @property
    def action(self) -> str:
        match self.label:
            case "succeeded":
                return "instance_updated"
            case "rolled_back" | "rollback_failed":
                return "instance_update_rolled_back"
            case "not_started":
                return "instance_update_abandoned"

    @property
    def failed(self) -> bool:
        return self.label != "succeeded"

    @property
    def keeps_files(self) -> bool:
        # The database copy is the only recovery point when the helper's result is unknown.
        return self.label == "rollback_failed" or self.reason == "result_unreadable"


def _outcome_of(result: UpdateResult) -> _Outcome:
    reason = result.reason
    cause: str | None = None
    match result.outcome:
        case "succeeded":
            message = "dockmin was updated."
        case "rolled_back":
            message = _FAILURE_TEXT.get(reason or "", "The update failed.") + _ROLLED_BACK_SUFFIX
        case "rollback_failed":
            message = _FAILURE_TEXT["rollback_failed"]
            cause, reason = reason, "rollback_failed"
        case "not_started":
            message = _FAILURE_TEXT.get(reason or "", "The update did not start.")
    return _Outcome(
        result.outcome,
        reason,
        cause,
        message,
        result.finished_at,
        result.new_container_id,
        tuple(result.log_excerpt),
    )


def _lost_outcome(reason: str, finished_at: str) -> _Outcome:
    return _Outcome("not_started", reason, None, _FAILURE_TEXT[reason], finished_at, None, ())


@dataclass(frozen=True, slots=True)
class _Requester:
    """Who asked for an update and which rows and container the request named."""

    update_id: str
    user_id: int
    request_event_id: int
    container_id: str
    container_name: str


HelperState = Literal["running", "stopped", "absent", "unknown"]


class UpdateDispatcher:
    """Runs accepted updates after their response and records the outcome of finished ones.

    A dispatched update pulls its images, writes the plan, starts the helper and then waits for
    the helper's result. A task started here runs in a fresh context, so it never inherits an
    audit scope from the request that spawned it. At most one update is in flight: a request
    claims it under `single_flight()` and the claim ends with the update.
    """

    def __init__(
        self,
        app: Starlette,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_seconds: float = RESULT_POLL_SECONDS,
    ) -> None:
        self._app = app
        self._sleep = sleep
        self._poll = poll_seconds
        self._tasks: set[asyncio.Task[None]] = set()
        self._progress: dict[str, UpdateProgress] = {}
        self._completing: set[str] = set()
        self._claimed: set[str] = set()
        self._lock = asyncio.Lock()
        self._stopping = False

    @property
    def pending(self) -> int:
        """Return how many updates and watches are still running."""
        return len(self._tasks)

    def single_flight(self) -> asyncio.Lock:
        """Return the lock a request holds from its last refusal check to its claim."""
        return self._lock

    def busy(self) -> bool:
        """Report whether this process holds a claim on an update that has not ended."""
        return bool(self._claimed)

    def claim(self, update_id: str) -> None:
        """Hold the update as in flight until `release` or the end of its task."""
        self._claimed.add(update_id)

    def release(self, update_id: str) -> None:
        """End a claim."""
        self._claimed.discard(update_id)

    def discard(self, update_id: str) -> None:
        """End a claim that was never dispatched and remove the directory it reserved."""
        if update_id in self._claimed:
            self._claimed.discard(update_id)
            remove_directory(update_root(self._app.state.settings.data_dir) / update_id)

    def mark_stopping(self) -> None:
        """Record that the process is shutting down, so nothing more is recorded."""
        self._stopping = True

    async def aclose(self) -> None:
        """Cancel the tasks still running and wait for them."""
        self._stopping = True
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def progress(self, update_id: str) -> UpdateProgress | None:
        """Return where the update is, or None when this process knows nothing of it."""
        return self._progress.get(update_id)

    def dispatch(self, job: UpdateJob) -> None:
        """Start the update in the background; must be called from the running event loop."""
        self.claim(job.update_id)
        self._advance(job.update_id, "pulling_target")
        self._spawn(self._run(job))

    def watch(self, update_id: str, seconds: float) -> None:
        """Wait up to `seconds` for the update's result, then record it."""
        self._spawn(self._wait_for_result(update_id, seconds))

    async def recover(self) -> None:
        """Record every finished update, resolve the lost ones, watch the rest, sweep helpers.

        Never raises: a failure is logged, and what was left is found again at the next start.
        """
        try:
            data_dir: Path = self._app.state.settings.data_dir
            for update_id in await asyncio.to_thread(_update_ids, data_dir):
                found = await asyncio.to_thread(_load, update_root(data_dir) / update_id)
                if found.recorded or found.result is not None or found.result_unreadable:
                    await self.complete(update_id)
                    continue
                state = await self._helper_state(update_id)
                if state in ("stopped", "absent"):
                    await self.complete(update_id, lost="helper_lost")
                else:
                    seconds = WATCH_FALLBACK_SECONDS
                    if found.plan is not None:
                        seconds = found.plan.health_timeout_seconds + WATCH_MARGIN_SECONDS
                    self.watch(update_id, seconds)
            known = set(await asyncio.to_thread(_update_ids, data_dir))
            await self._sweep_stale_helpers(known)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error("update_recovery_failed", extra={"error": type(error).__name__})

    async def complete(self, update_id: str, *, lost: str | None = None) -> None:
        """Record the outcome of a finished update once, then remove its helper and its files.

        `lost` names why there is no result to read: the helper is gone, or its result is
        unreadable. The plan is deleted as soon as the outcome is recorded.
        """
        if update_id in self._completing or self._stopping:
            return
        self._completing.add(update_id)
        try:
            await self._complete(update_id, lost)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "update_completion_failed",
                extra={"update_id": update_id, "error": type(error).__name__},
            )
        finally:
            self._completing.discard(update_id)

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(work, context=contextvars.Context())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _advance(
        self,
        update_id: str,
        phase: UpdatePhase,
        *,
        reason: str | None = None,
        message: str | None = None,
        outcome: ResultOutcome | None = None,
    ) -> None:
        self._progress.pop(update_id, None)
        self._progress[update_id] = UpdateProgress(update_id, phase, reason, message, outcome)
        while len(self._progress) > MAX_PROGRESS_ENTRIES:
            self._progress.pop(next(iter(self._progress)))

    async def _run(self, job: UpdateJob) -> None:
        try:
            try:
                await self._pull(job, job.target_reference, job.target_digest)
                self._advance(job.update_id, "pulling_helper")
                await self._pull(job, job.helper_reference, job.helper_digest)
                self._advance(job.update_id, "preparing")
                client, own, helper_image_id = await self._prepare(job)
                self._advance(job.update_id, "starting_helper")
                await self._start_helper(job, client, own, helper_image_id)
            except _Abandon as stop:
                await self._abandon(job, stop.reason)
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(
                    "update_dispatch_failed",
                    extra={"update_id": job.update_id, "error": type(error).__name__},
                )
                await self._abandon(job, "plan_failed")
                return
            self._advance(job.update_id, "handed_over")
            await self._wait_for_result(
                job.update_id,
                own.stop_timeout + job.health_timeout_seconds + WATCH_MARGIN_SECONDS,
            )
        finally:
            self.release(job.update_id)

    async def _pull(self, job: UpdateJob, reference: str, digest: str) -> None:
        state = self._app.state
        try:
            async with open_image_services(
                state.database, state.auth, state.engine, state.registries
            ) as services:
                plan = await services.actions.prepare_pull(
                    job.principal, job.environment_id, reference, job.choice
                )
                outcome = await services.actions.pull(job.principal, job.environment_id, plan)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "update_pull_failed",
                extra={"update_id": job.update_id, "error": type(error).__name__},
            )
            raise _Abandon("pull_failed") from None
        if outcome.digest != digest:
            raise _Abandon("digest_changed")

    async def _home(self, environment_id: int) -> tuple[EngineClient, str]:
        state = self._app.state
        async with state.database.session() as db:
            hosts = build_host_clients(db, state.auth, state.engine)
            environment = await hosts.environment(environment_id)
            client = await hosts.client(environment_id)
        if environment.socket_path is None:
            raise UpdateRefused("not_restartable")
        return client, environment.socket_path

    async def _pulled(self, client: EngineClient, reference: str, digest: str) -> str:
        """Return the id of the image the reference names, which must carry the reviewed digest."""
        image = await client.images.inspect(reference)
        parsed = parse_reference(reference)
        if digest not in digests_for(image.repo_digests, parsed.repository):
            raise _Abandon("digest_changed")
        if _IMAGE_ID.fullmatch(image.id) is None:
            raise _Abandon("plan_failed")
        return image.id

    async def _prepare(self, job: UpdateJob) -> tuple[EngineClient, OwnContainer, str]:
        state = self._app.state
        data_dir: Path = state.settings.data_dir
        try:
            client, socket_path = await self._home(job.environment_id)
            own = await read_own_container(
                client, container_id=job.container_id, socket_path=socket_path, data_dir=data_dir
            )
            info = await client.system.info()
            defaults = DaemonDefaults(
                runtime=info.default_runtime or None,
                logging_driver=info.logging_driver or None,
                cgroup_version=info.cgroup_version or None,
            )
            derivation = await asyncio.to_thread(
                partial(
                    derive_recreate,
                    own.document,
                    image_inspect=own.image_document,
                    defaults=defaults,
                )
            )
            if derivation.image_defaults_kept:
                raise _Abandon("plan_failed")
            target_image_id = await self._pulled(client, job.target_reference, job.target_digest)
            helper_image_id = await self._pulled(client, job.helper_reference, job.helper_digest)
            daemon = client.daemon or await client.negotiate()
            if _API_VERSION.fullmatch(daemon.negotiated_api_version) is None:
                raise _Abandon("plan_failed")
            body = dict(derivation.body)
            body["Image"] = job.target_reference
            plan = UpdatePlan(
                update_id=job.update_id,
                created_at=format_timestamp(state.auth.clock()),
                requested_by_user_id=job.principal.user_id,
                request_event_id=job.request_event_id,
                engine_api_version=daemon.negotiated_api_version,
                old_id=own.container_id,
                old_name=own.name,
                stop_timeout=own.stop_timeout,
                old_image=own.reference_text,
                new_image=job.target_reference,
                new_image_id=target_image_id,
                create_body=body,
                health_timeout_seconds=job.health_timeout_seconds,
            )
            await asyncio.to_thread(write_plan, data_dir, plan)
        except _Abandon:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "update_plan_failed",
                extra={"update_id": job.update_id, "error": type(error).__name__},
            )
            raise _Abandon("plan_failed") from None
        return client, own, helper_image_id

    async def _start_helper(
        self, job: UpdateJob, client: EngineClient, own: OwnContainer, image_id: str
    ) -> None:
        body = helper_body(job, own, image_id)
        created: str | None = None
        try:
            created = await client.containers.create(
                body, name=f"{own.name}{HELPER_SUFFIX}-{job.update_id[:8]}"
            )
            await client.containers.start(created)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "update_helper_start_failed",
                extra={"update_id": job.update_id, "error": type(error).__name__},
            )
            if created is not None and await self._helper_running(client, created):
                return
            if created is not None:
                await self._discard_helper(client, created)
            raise _Abandon("helper_failed") from None

    async def _helper_running(self, client: EngineClient, container_id: str) -> bool:
        try:
            inspected = await client.containers.inspect(container_id)
        except EngineError:
            return True
        return inspected.id == container_id and inspected.state.running

    async def _discard_helper(self, client: EngineClient, container_id: str) -> None:
        try:
            await client.containers.remove(container_id, force=False)
        except EngineError as error:
            logger.warning("update_helper_remove_failed", extra={"error": type(error).__name__})

    async def _abandon(self, job: UpdateJob, reason: AbandonReason) -> None:
        state = self._app.state
        message = _ABANDON_TEXT[reason]
        self._advance(job.update_id, "abandoned", reason=reason, message=message)
        directory = update_root(state.settings.data_dir) / job.update_id
        if self._stopping:
            return
        try:
            async with state.database.session() as db:
                await ActivityRecorder(db, state.auth.clock).record_failure(
                    UserActor(job.principal.user_id),
                    action="instance_update_abandoned",
                    target_type="instance",
                    target_id=job.container_id,
                    reason=reason,
                    message=message,
                    detail={
                        "update_id": job.update_id,
                        "request_event_id": job.request_event_id,
                        "container_name": job.container_name,
                    },
                )
        except Exception as error:
            logger.error("update_abandon_unrecorded", extra={"error": type(error).__name__})
        await asyncio.to_thread(remove_directory, directory)

    async def _wait_for_result(self, update_id: str, seconds: float) -> None:
        directory = update_root(self._app.state.settings.data_dir) / update_id
        attempts = max(1, int(seconds / self._poll))
        for turn in range(attempts):
            if self._stopping or not await asyncio.to_thread(directory.is_dir):
                return
            if await asyncio.to_thread((directory / RESULT_FILE).exists):
                await self.complete(update_id)
                return
            if turn and turn % HELPER_CHECK_POLLS == 0 and await self._helper_lost(update_id):
                await self.complete(update_id, lost="helper_lost")
                return
            await self._sleep(self._poll)
        logger.warning("update_result_not_seen", extra={"update_id": update_id})

    async def _helper_lost(self, update_id: str) -> bool:
        """Report whether the helper is gone or stopped, and left no result."""
        if await self._helper_state(update_id) not in ("stopped", "absent"):
            return False
        directory = update_root(self._app.state.settings.data_dir) / update_id
        return not await asyncio.to_thread((directory / RESULT_FILE).exists)

    async def _requester(self, update_id: str, plan: _PlanDocument | None) -> _Requester | None:
        if plan is not None:
            return _Requester(
                update_id,
                plan.requested_by_user_id,
                plan.request_event_id,
                plan.old.id,
                plan.old.name,
            )
        state = self._app.state
        async with state.database.session() as db:
            row = await find_request_by_update(
                ActivityEventRepository(db, state.auth.clock), update_id
            )
        if row is None or row.actor_user_id is None or row.target_id is None:
            return None
        name = (row.detail or {}).get("container_name")
        return _Requester(
            update_id,
            row.actor_user_id,
            row.id,
            row.target_id,
            name if isinstance(name, str) else "",
        )

    async def _complete(self, update_id: str, lost: str | None) -> None:
        state = self._app.state
        directory = update_root(state.settings.data_dir) / update_id
        found = await asyncio.to_thread(_load, directory)
        marker = found.recorded
        keep = False
        if marker is not None:
            keep = marker.keep
        else:
            outcome: _Outcome
            if found.result is not None:
                outcome = _outcome_of(found.result)
            else:
                reason = lost or ("result_unreadable" if found.result_unreadable else None)
                if reason is None:
                    return
                outcome = _lost_outcome(reason, format_timestamp(state.auth.clock()))
            who = await self._requester(update_id, found.plan)
            if who is None:
                logger.error("update_request_unknown", extra={"update_id": update_id})
            else:
                event_id = await self._record(who, outcome)
                await asyncio.to_thread(
                    _write_marker, directory, event_id, keep=outcome.keeps_files
                )
            keep = outcome.keeps_files
            self._advance(
                update_id,
                "finished",
                reason=outcome.reason,
                message=outcome.message,
                outcome=outcome.label,
            )
        await asyncio.to_thread((directory / PLAN_FILE).unlink, missing_ok=True)
        if keep:
            return
        if await self._remove_helper(update_id):
            await asyncio.to_thread(remove_directory, directory)

    async def _record(self, who: _Requester, outcome: _Outcome) -> int | None:
        state = self._app.state
        action = outcome.action
        target_id = (
            outcome.new_container_id if outcome.label == "succeeded" else None
        ) or who.container_id
        leftover = outcome.label == "succeeded" and await self._previous_left(who.container_name)
        excerpt = list(outcome.excerpt)
        detail: dict[str, DetailValue] = {
            "update_id": who.update_id,
            "request_event_id": who.request_event_id,
            "container_name": who.container_name,
        }
        if outcome.failed:
            detail["reason"] = outcome.reason
            detail["message"] = outcome.message
            if outcome.cause is not None:
                detail["cause"] = outcome.cause
        if excerpt:
            detail["log_excerpt"] = newest_lines_within(excerpt, MAX_DETAIL_TEXT)
        if leftover:
            detail["previous_container_left"] = True
        document: dict[str, Any] = {
            "update_id": detail["update_id"],
            "outcome": outcome.label,
            "reason": outcome.reason,
            "message": outcome.message,
            "finished_at": outcome.finished_at,
            "request_event_id": who.request_event_id,
            "event_id": None,
            "previous_container_left": leftover,
            "excerpt": _stored_excerpt(_scrubbed(excerpt)),
        }
        async with state.database.session() as db:
            clock = state.auth.clock
            events = ActivityEventRepository(db, clock)
            existing = await _find_outcome(events, who, action, target_id)
            if existing is not None:
                return existing.id
            async with db.transaction():
                await ActivityRecorder(db, clock).record_event(
                    UserActor(who.user_id),
                    action=action,
                    target_type="instance",
                    target_id=target_id,
                    result="failure" if outcome.failed else "success",
                    detail=detail,
                )
                await SettingRepository(db, clock).put(
                    OUTCOME_SETTING, "json", json.dumps(document, separators=(",", ":"))
                )
            row = await _find_outcome(events, who, action, target_id)
            if row is None:
                return None
            document["event_id"] = row.id
            try:
                await SettingRepository(db, clock).put(
                    OUTCOME_SETTING, "json", json.dumps(document, separators=(",", ":"))
                )
            except Exception as error:
                logger.warning("update_outcome_link_failed", extra={"error": type(error).__name__})
            return row.id

    async def _home_client(self) -> EngineClient | None:
        state = self._app.state
        environment_id = source_of(self._app)().restartable_via
        if environment_id is None:
            return None
        try:
            async with state.database.session() as db:
                return await build_host_clients(db, state.auth, state.engine).client(environment_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("update_home_unavailable", extra={"error": type(error).__name__})
            return None

    def _own_name(self) -> str | None:
        identity = source_of(self._app)()
        return next(
            (
                home.container_name
                for home in identity.verified
                if home.environment_id == identity.restartable_via
            ),
            None,
        )

    async def _previous_left(self, name: str) -> bool:
        client = await self._home_client()
        if client is None:
            return False
        previous = f"{name}{PREVIOUS_SUFFIX}"
        try:
            listed = await client.containers.list(all=True, filters={"name": [previous]})
        except EngineError:
            return False
        return any(f"/{previous}" in summary.names for summary in listed)

    async def _is_helper(
        self, client: EngineClient, summary: ContainerSummary, update_id: str | None
    ) -> bool:
        """Report whether the container is an update helper of this instance.

        It carries the helper labels, is named for this container and its update, and runs an
        image of a repository that ends in `-updater`. `update_id` None accepts any update.
        """
        own = self._own_name()
        found = summary.labels.get(UPDATE_ID_LABEL, "")
        if (
            own is None
            or summary.labels.get(HELPER_ROLE_LABEL) != HELPER_ROLE
            or UPDATE_ID_PATTERN.fullmatch(found) is None
            or (update_id is not None and found != update_id)
            or f"/{own}{HELPER_SUFFIX}-{found[:8]}" not in summary.names
        ):
            return False
        try:
            image = await client.images.inspect(summary.image_id)
        except EngineError:
            return False
        for text in (*image.repo_tags, *image.repo_digests):
            try:
                if parse_reference(text).path.endswith(HELPER_SUFFIX):
                    return True
            except ReferenceInvalid:
                continue
        return False

    async def _helper_state(self, update_id: str) -> HelperState:
        client = await self._home_client()
        if client is None:
            return "unknown"
        try:
            listed = await client.containers.list(
                all=True,
                filters={
                    "label": [
                        f"{HELPER_ROLE_LABEL}={HELPER_ROLE}",
                        f"{UPDATE_ID_LABEL}={update_id}",
                    ]
                },
            )
            found = [s for s in listed if await self._is_helper(client, s, update_id)]
        except EngineError:
            return "unknown"
        if not found:
            return "absent"
        return "running" if any(s.state == "running" for s in found) else "stopped"

    async def _remove_helper(self, update_id: str) -> bool:
        """Remove the update's helper; False when that could not be done and should be retried."""
        client = await self._home_client()
        if client is None:
            logger.warning("update_helper_not_removed", extra={"update_id": update_id})
            return False
        try:
            listed = await client.containers.list(
                all=True,
                filters={
                    "label": [
                        f"{HELPER_ROLE_LABEL}={HELPER_ROLE}",
                        f"{UPDATE_ID_LABEL}={update_id}",
                    ]
                },
            )
            for summary in listed:
                if await self._is_helper(client, summary, update_id):
                    await self._remove_when_stopped(client, summary.id)
        except asyncio.CancelledError:
            raise
        except EngineError as error:
            logger.warning(
                "update_helper_remove_failed",
                extra={"update_id": update_id, "error": type(error).__name__},
            )
            return False
        return True

    async def _remove_when_stopped(self, client: EngineClient, container_id: str) -> None:
        running = True
        for _ in range(HELPER_EXIT_WAIT_SECONDS):
            try:
                inspected = await client.containers.inspect(container_id)
            except NotFound:
                return
            if inspected.id != container_id:
                return
            running = inspected.state.running
            if not running:
                break
            await self._sleep(1.0)
        await client.containers.remove(container_id, force=running)

    async def _sweep_stale_helpers(self, known: set[str]) -> None:
        client = await self._home_client()
        if client is None:
            return
        try:
            listed = await client.containers.list(
                all=True, filters={"label": [f"{HELPER_ROLE_LABEL}={HELPER_ROLE}"]}
            )
            for summary in listed:
                if (
                    summary.labels.get(UPDATE_ID_LABEL, "") in known
                    or summary.state == "running"
                    or not await self._is_helper(client, summary, None)
                ):
                    continue
                await client.containers.remove(summary.id, force=False)
        except asyncio.CancelledError:
            raise
        except EngineError as error:
            logger.warning("update_helper_sweep_failed", extra={"error": type(error).__name__})


async def _find_outcome(
    events: ActivityEventRepository, who: _Requester, action: str, target_id: str
) -> ActivityEvent | None:
    page = await events.page(
        ActivityFilter(
            actor_user_id=who.user_id,
            action=action,
            target_type="instance",
            target_id=target_id,
        ),
        limit=20,
    )
    return next(
        (
            row
            for row in page.items
            if row.detail is not None and row.detail.get("request_event_id") == who.request_event_id
        ),
        None,
    )


async def find_request_by_update(
    events: ActivityEventRepository, update_id: str
) -> ActivityEvent | None:
    """Return the `instance_update_requested` row of the update, whoever asked, or None."""
    page = await events.page(ActivityFilter(action="instance_update_requested"), limit=50)
    return next(
        (
            row
            for row in page.items
            if row.detail is not None and row.detail.get("update_id") == update_id
        ),
        None,
    )


async def find_request_event(
    events: ActivityEventRepository, user_id: int, container_id: str, update_id: str
) -> ActivityEvent | None:
    """Return the `instance_update_requested` row written for the update, or None."""
    page = await events.page(
        ActivityFilter(
            actor_user_id=user_id,
            action="instance_update_requested",
            target_type="instance",
            target_id=container_id,
        ),
        limit=20,
    )
    return next(
        (
            row
            for row in page.items
            if row.detail is not None and row.detail.get("update_id") == update_id
        ),
        None,
    )


def helper_body(job: UpdateJob, own: OwnContainer, image_id: str) -> dict[str, Any]:
    """Return the create body of the update helper, which runs from the pulled image's id."""
    data = own.data_mount
    return {
        "Image": image_id,
        "User": HELPER_USER,
        "Env": [f"{UPDATE_ID_ENVIRONMENT}={job.update_id}"],
        "Labels": {HELPER_ROLE_LABEL: HELPER_ROLE, UPDATE_ID_LABEL: job.update_id},
        "HostConfig": {
            "Mounts": [
                {"Type": "bind", "Source": own.socket_source, "Target": HELPER_SOCKET_PATH},
                {"Type": data.kind, "Source": data.source, "Target": HELPER_DATA_PATH},
            ],
            "GroupAdd": list(own.group_add),
            "NetworkMode": "none",
            "SecurityOpt": ["no-new-privileges:true"],
            "CapDrop": ["ALL"],
        },
    }


def update_dispatcher_of(app: Starlette) -> UpdateDispatcher:
    """Return the application's update dispatcher, creating it on first use."""
    dispatcher = getattr(app.state, "update_dispatcher", None)
    if not isinstance(dispatcher, UpdateDispatcher):
        dispatcher = UpdateDispatcher(app)
        app.state.update_dispatcher = dispatcher
    return dispatcher


async def finish_updates(app: Starlette) -> None:
    """Record the updates that finished while dockmin was not running, and watch the rest."""
    await update_dispatcher_of(app).recover()
