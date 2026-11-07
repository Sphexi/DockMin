"""Container lifecycle actions: seven audited mutations, their confirmations, and batches."""

import asyncio
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal

from app.auth.errors import (
    SELF_PROTECTED_REASON,
    PermissionDenied,
    ProductError,
    ResourceNotFound,
    SelfProtected,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.db.engine import Database, Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.db.repositories.resource_control import ResourceControlRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import (
    Conflict,
    EngineError,
    EnvironmentUnreachable,
    NotFound,
    RequestOutcomeUnknown,
)
from app.docker_engine.timeouts import Timeouts
from app.logging import DEFAULT_SCRUBBER, get_logger
from app.scheduler.model import RunSkipped
from app.self_identity import SelfIdentitySource, no_identity
from app.services.access import ResourceKey, is_canonical_id, stack_of
from app.services.acting import Acting, AutomationAuthority, actor_of, holder_label
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    SubsystemActor,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.containers import (
    CONTAINER_STATES,
    SELF_REFUSED_ACTIONS,
    ContainerAction,
    display_name,
    short_id,
)
from app.services.object_inspector import (
    ContainerTooLarge,
    EngineObjectInspector,
    EnvironmentUnavailable,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.resource_controls import ResourceControlService

logger = get_logger(__name__)

MAX_BATCH = 25
MAX_VOLUMES_LISTED = 20
MAX_DISPLAYED_ID = 64
MAX_DISPLAYED_NAME = 128
ENGINE_DETAIL_READ = 4096
ENGINE_DETAIL_CHARS = 300
SETTLE_MARGIN_SECONDS = 50.0
LOCK_BYPASSED_BY_KILL = frozenset({"stop", "restart"})
ANONYMOUS_VOLUME_LABEL = "com.docker.volume.anonymous"
FORCEABLE_STATES = frozenset({"running", "paused", "restarting"})
CHANGED_SINCE_REVIEW = "This container changed since you reviewed it; nothing was removed."

_EVENTS: Mapping[ContainerAction, str] = {
    "start": "container_started",
    "stop": "container_stopped",
    "restart": "container_restarted",
    "kill": "container_killed",
    "pause": "container_paused",
    "unpause": "container_unpaused",
    "remove": "container_removed",
}
_REMOVE_RUNNING = "Stop it first, or choose to kill it first."
_CONFLICTS: Mapping[tuple[ContainerAction, str], str] = {
    ("start", "paused"): "The container is paused. Unpause it first.",
    ("start", "removing"): "The container is being removed.",
    ("start", "dead"): "The container is dead and cannot be started.",
    ("stop", "removing"): "The container is being removed.",
    ("restart", "removing"): "The container is being removed.",
    ("kill", "created"): "The container is not running.",
    ("kill", "exited"): "The container is not running.",
    ("kill", "dead"): "The container is not running.",
    ("pause", "paused"): "The container is already paused.",
    ("pause", "created"): "The container is not running.",
    ("pause", "exited"): "The container is not running.",
    ("pause", "dead"): "The container is not running.",
    ("pause", "restarting"): "The container is restarting. Try again once it is running.",
    ("unpause", "created"): "The container is not paused.",
    ("unpause", "running"): "The container is not paused.",
    ("unpause", "restarting"): "The container is not paused.",
    ("unpause", "exited"): "The container is not paused.",
    ("unpause", "dead"): "The container is not paused.",
    ("remove", "running"): f"The container is running. {_REMOVE_RUNNING}",
    ("remove", "paused"): f"The container is paused. {_REMOVE_RUNNING}",
    ("remove", "restarting"): f"The container is restarting. {_REMOVE_RUNNING}",
    ("remove", "removing"): "The container is already being removed.",
}
_CONFLICT_DEFAULT: Mapping[ContainerAction, str] = {
    "start": "The container cannot be started in its current state.",
    "stop": "The container cannot be stopped in its current state.",
    "restart": "The container cannot be restarted in its current state.",
    "kill": "The container cannot be killed in its current state.",
    "pause": "The container cannot be paused in its current state.",
    "unpause": "The container cannot be unpaused in its current state.",
    "remove": "The container cannot be removed in its current state.",
}

type ActionResult = Literal["done", "unchanged"]
type BatchStatus = Literal[
    "done",
    "unchanged",
    "not_found",
    "forbidden",
    "busy",
    "conflict",
    "unreachable",
    "protected",
    "failed",
]


def engine_detail(error: EngineError) -> str | None:
    """Return the Engine's message for an administrator: scrubbed, one line, at most 300 characters.

    Held in memory on the exception that carries it. It is never part of `str()`, a log record
    or an activity row, because it can name host paths.
    """
    raw = (error.engine_message or "")[:ENGINE_DETAIL_READ]
    kept = "".join(
        " " if char.isspace() else char
        for char in raw
        if char.isspace() or not unicodedata.category(char).startswith("C")
    )
    text = " ".join(DEFAULT_SCRUBBER.scrub_text(kept).split())
    return text[:ENGINE_DETAIL_CHARS] or None


class ContainerGone(ProductError):
    """The container vanished between authorisation and the Engine call."""

    def __init__(self) -> None:
        super().__init__("The container no longer exists.")


class ContainerStateConflict(ProductError):
    """The Engine refused the action for the container's state; the message is the product's."""


class EngineRefused(ProductError):
    """The Engine rejected or failed the action; the Engine's text is not part of the message.

    engine_detail is that text, scrubbed and cut, for a page shown to an administrator.
    """

    def __init__(self, engine_detail: str | None = None) -> None:
        super().__init__("Docker refused the action.")
        self.engine_detail = engine_detail


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """What one action did; unchanged means the container was already in the requested state."""

    action: ContainerAction
    environment_id: int
    container_id: str
    name: str
    result: ActionResult


@dataclass(frozen=True, slots=True)
class BusyView:
    """Who holds a contended container and for how long, as the viewer may be told."""

    action: str
    age_seconds: int
    holder: str
    held_by_you: bool


@dataclass(frozen=True, slots=True)
class BatchItem:
    """The outcome for one container of a batch; message is always product-written."""

    container_id: str
    short_id: str
    name: str | None
    status: BatchStatus
    message: str
    holder: BusyView | None = None


@dataclass(frozen=True, slots=True)
class BatchResult:
    """One item per distinct submitted id, in submitted order."""

    action: ContainerAction
    items: tuple[BatchItem, ...]

    @property
    def succeeded(self) -> int:
        """Return how many items were done or already in the requested state."""
        return sum(1 for item in self.items if item.status in ("done", "unchanged"))

    @property
    def failed(self) -> int:
        """Return how many items were not carried out."""
        return len(self.items) - self.succeeded


@dataclass(frozen=True, slots=True)
class VolumeMount:
    """A volume mount named in a removal confirmation; anonymous is None when unreadable."""

    name: str
    destination: str
    anonymous: bool | None


@dataclass(frozen=True, slots=True)
class ActionConfirmation:
    """Everything a confirmation names before an action runs."""

    action: ContainerAction
    environment_id: int
    environment_name: str
    is_local: bool
    container_id: str
    short_id: str
    name: str
    image: str
    state: str
    can_force: bool
    volumes: tuple[VolumeMount, ...]
    volumes_hidden: int


@dataclass(frozen=True, slots=True)
class BatchTarget:
    """One container a batch confirmation names."""

    container_id: str
    short_id: str
    name: str
    state: str


@dataclass(frozen=True, slots=True)
class BatchConfirmation:
    """The targets of a batch the caller may act on; unavailable counts the rest."""

    action: ContainerAction
    environment_id: int
    environment_name: str
    is_local: bool
    targets: tuple[BatchTarget, ...]
    unavailable: int
    can_force: bool


@dataclass(frozen=True, slots=True)
class _Target:
    actor: Acting
    action: ContainerAction
    environment_id: int
    container_id: str
    name: str
    client: EngineClient
    key: ObjectKey
    holder: LockHolder
    stop_wait: int | None
    stack: ObjectKey | None = None


def _stack_key_of(environment_id: int, labels: Mapping[str, str]) -> ObjectKey | None:
    stack = stack_of(labels)
    return None if stack is None else ObjectKey(environment_id, "stack", stack)


def conflict_message(action: ContainerAction, state: str | None) -> str:
    """Return the product's explanation of an Engine conflict, chosen by action and state."""
    known = state if state in CONTAINER_STATES else None
    if known is not None and (action, known) in _CONFLICTS:
        return _CONFLICTS[(action, known)]
    return _CONFLICT_DEFAULT[action]


def _target_id(arguments: Arguments) -> str:
    return str(arguments["container_id"])[:MAX_DISPLAYED_ID]


def retrieve_exception(future: asyncio.Future[Any]) -> None:
    """Mark a future's exception as retrieved so an abandoned task does not log it."""
    if not future.cancelled():
        future.exception()


def _changed(changed: bool) -> Mapping[str, DetailValue]:
    return {"changed": changed}


def _distinct(container_ids: Sequence[str]) -> list[str]:
    unique = list(dict.fromkeys(container_ids))
    if not unique:
        raise ValidationFailed({"containers": "Select at least one container."})
    if len(unique) > MAX_BATCH:
        raise ValidationFailed({"containers": f"Select at most {MAX_BATCH} containers."})
    return unique


def _settle_seconds(target: _Target) -> float:
    read = Timeouts.long_for(target.stop_wait or 0).read
    return (read or 0.0) + SETTLE_MARGIN_SECONDS


def _failure_message(failure: str, action: ContainerAction) -> str:
    match failure:
        case "not_found":
            return str(ContainerGone())
        case "conflict":
            return conflict_message(action, None)
        case "unreachable":
            return str(EnvironmentUnavailable("unreachable"))
        case "outcome_unknown":
            return str(EnvironmentUnavailable("outcome_unknown"))
        case _:
            return str(EngineRefused())


async def _settle_removal(
    db: Session,
    controls: ResourceControlService,
    key: ResourceKey,
    row: Callable[[], Awaitable[None]] | None = None,
) -> None:
    async with db.transaction():
        await controls.release(key)
        if row is not None:
            await row()


class ContainerActionService:
    """The seven lifecycle actions, each one audited operation, plus their confirmations."""

    def __init__(
        self,
        *,
        db: Session,
        database: Database,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        locks: ObjectLockRegistry,
        controls: ResourceControlService,
        activity: ActivityRecorder,
        clock: Clock,
        self_identity: SelfIdentitySource | None = None,
    ) -> None:
        self._db = db
        self._database = database
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._locks = locks
        self._controls = controls
        self._activity = activity
        self._clock = clock
        self._self_identity = self_identity or no_identity

    @audited(
        action="container_started",
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def start(
        self, actor: Principal, environment_id: int, container_id: str, *, batch: bool = False
    ) -> ActionOutcome:
        """Start a container; the result is unchanged if it was already running."""
        target = await self._prepare(actor, environment_id, container_id, "start", batch=batch)
        async with self._locked(target):
            changed = await self._engine(
                target, lambda: target.client.containers.start(container_id), detail_of=_changed
            )
        current_audit().detail["changed"] = changed
        return self._finish(target, "done" if changed else "unchanged")

    @audited(
        action="container_stopped",
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def stop(
        self, actor: Principal, environment_id: int, container_id: str, *, batch: bool = False
    ) -> ActionOutcome:
        """Stop a container with its own grace period; unchanged if it was already stopped."""
        target = await self._prepare(actor, environment_id, container_id, "stop", batch=batch)
        async with self._locked(target):
            changed = await self._engine(
                target,
                lambda: target.client.containers.stop(
                    container_id, daemon_wait_seconds=target.stop_wait
                ),
                detail_of=_changed,
            )
        current_audit().detail["changed"] = changed
        return self._finish(target, "done" if changed else "unchanged")

    @audited(
        action="container_restarted",
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def restart(
        self,
        actor: Acting,
        environment_id: int,
        container_id: str,
        *,
        batch: bool = False,
        note: Mapping[str, DetailValue] | None = None,
    ) -> ActionOutcome:
        """Restart a container; `note` adds detail to the event."""
        if note is not None:
            current_audit().detail.update(note)
        target = await self._prepare(actor, environment_id, container_id, "restart", batch=batch)
        async with self._locked(target):
            await self._engine(
                target,
                lambda: target.client.containers.restart(
                    container_id, daemon_wait_seconds=target.stop_wait
                ),
            )
        return self._finish(target, "done")

    @audited(
        action="container_killed",
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def kill(
        self, actor: Principal, environment_id: int, container_id: str, *, batch: bool = False
    ) -> ActionOutcome:
        """Send SIGKILL to a container."""
        target = await self._prepare(actor, environment_id, container_id, "kill", batch=batch)
        async with self._locked(target):
            await self._engine(
                target, lambda: target.client.containers.kill(container_id, signal="SIGKILL")
            )
        return self._finish(target, "done")

    @audited(
        action="container_paused",
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def pause(
        self, actor: Principal, environment_id: int, container_id: str, *, batch: bool = False
    ) -> ActionOutcome:
        """Freeze every process in a running container."""
        target = await self._prepare(actor, environment_id, container_id, "pause", batch=batch)
        async with self._locked(target):
            await self._engine(target, lambda: target.client.containers.pause(container_id))
        return self._finish(target, "done")

    @audited(
        action="container_unpaused",
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def unpause(
        self, actor: Principal, environment_id: int, container_id: str, *, batch: bool = False
    ) -> ActionOutcome:
        """Resume a paused container."""
        target = await self._prepare(actor, environment_id, container_id, "unpause", batch=batch)
        async with self._locked(target):
            await self._engine(target, lambda: target.client.containers.unpause(container_id))
        return self._finish(target, "done")

    @audited(
        action="container_removed",
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def remove(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        *,
        expected_name: str,
        force: bool = False,
        volumes: bool = False,
        batch: bool = False,
    ) -> ActionOutcome:
        """Remove a container that still has the name the caller reviewed, then its control.

        Named volumes are kept whatever `volumes` says. The control is released in the
        transaction that commits the success row, and only after the Engine removed the
        container. If the release fails the removal still counts: the row records
        control_released as false.
        """
        target = await self._prepare(actor, environment_id, container_id, "remove", batch=batch)
        audit = current_audit()
        audit.detail.update(force=force, volumes=volumes)
        if expected_name != target.name:
            audit.reason = "name_changed"
            raise ValidationFailed({"name": CHANGED_SINCE_REVIEW})
        async with self._locked(target):
            await self._engine(
                target,
                lambda: target.client.containers.remove(container_id, force=force, volumes=volumes),
                release=True,
            )
            try:
                await retry_busy(
                    partial(
                        _settle_removal,
                        self._db,
                        self._controls,
                        ResourceKey(environment_id, "container", container_id),
                    )
                )
            except asyncio.CancelledError:
                interrupted: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
                await self._write_interrupted(target, interrupted, "", True)
                raise
            except Exception as error:
                logger.error(
                    "container_control_release_failed",
                    extra={
                        "environment_id": environment_id,
                        "container_id": container_id,
                        "error": type(error).__name__,
                    },
                )
                audit.detail["control_released"] = False
        return self._finish(target, "done")

    async def run_batch(
        self,
        actor: Principal,
        environment_id: int,
        action: ContainerAction,
        container_ids: Sequence[str],
        *,
        expected_names: Mapping[str, str] | None = None,
        reviewed_names: Mapping[str, str] | None = None,
        force: bool = False,
        volumes: bool = False,
    ) -> BatchResult:
        """Apply one action to up to 25 containers in order, each its own audited operation.

        Not atomic and never stopped early; a refusal on one item is that item's result.
        ValidationFailed only when the selection itself is empty or over the limit.
        `reviewed_names` are the names the confirmation showed. They label an item the action
        could not name, and are never used to find a container.
        """
        unique = _distinct(container_ids)
        names = expected_names or {}
        shown = reviewed_names or {}
        items = [
            await self._batch_item(
                actor,
                environment_id,
                action,
                container_id,
                names,
                shown,
                force=force,
                volumes=volumes,
            )
            for container_id in unique
        ]
        return BatchResult(action, tuple(items))

    def describe_busy(self, busy: ObjectBusy, viewer: Principal) -> BusyView:
        """Describe the lock holder to a viewer: names only for administrators and subsystems."""
        holder = busy.holder
        age = int(holder.age_seconds(self._locks.now()))
        match holder.actor:
            case UserActor(user_id=user_id):
                mine = user_id == viewer.user_id
                if viewer.is_administrator:
                    return BusyView(holder.action, age, holder.label, mine)
                return BusyView(holder.action, age, "you" if mine else "another user", mine)
            case SubsystemActor():
                return BusyView(holder.action, age, holder.label, False)

    async def confirmation(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        action: ContainerAction,
    ) -> ActionConfirmation:
        """Describe the target of an action before it runs; write holders only."""
        if not is_canonical_id("container", container_id):
            raise ResourceNotFound("not found")
        key = ResourceKey(environment_id, "container", container_id)
        await self._authoriser.require_access(actor, key, "write")
        environment = await self._hosts.environment(environment_id)
        inspected = await self._inspector.inspect(environment_id, container_id)
        volume_mounts: tuple[VolumeMount, ...] = ()
        hidden = 0
        if action == "remove":
            client = await self._hosts.client(environment_id)
            named = [m for m in inspected.mounts if m.type == "volume" and m.name]
            shown = named[:MAX_VOLUMES_LISTED]
            hidden = len(named) - len(shown)
            marks = await asyncio.gather(*(_is_anonymous(client, m.name or "") for m in shown))
            volume_mounts = tuple(
                VolumeMount(m.name or "", m.destination, mark)
                for m, mark in zip(shown, marks, strict=True)
            )
        return ActionConfirmation(
            action=action,
            environment_id=environment_id,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            container_id=inspected.id,
            short_id=short_id(inspected.id),
            name=display_name([inspected.name], inspected.id),
            image=inspected.config.image or inspected.image,
            state=inspected.state.status,
            can_force=inspected.state.status in FORCEABLE_STATES,
            volumes=volume_mounts,
            volumes_hidden=hidden,
        )

    async def batch_confirmation(
        self,
        actor: Principal,
        environment_id: int,
        action: ContainerAction,
        container_ids: Sequence[str],
    ) -> BatchConfirmation:
        """Name every container of a batch the caller may act on; the rest are only counted."""
        unique = _distinct(container_ids)
        environment = await self._hosts.environment(environment_id)
        targets: list[BatchTarget] = []
        unavailable = 0
        for container_id in unique:
            try:
                if not is_canonical_id("container", container_id):
                    raise ResourceNotFound("not found")
                key = ResourceKey(environment_id, "container", container_id)
                await self._authoriser.require_access(actor, key, "write")
                inspected = await self._inspector.inspect(environment_id, container_id)
            except (ResourceNotFound, PermissionDenied, ContainerTooLarge):
                unavailable += 1
                continue
            targets.append(
                BatchTarget(
                    inspected.id,
                    short_id(inspected.id),
                    display_name([inspected.name], inspected.id),
                    inspected.state.status,
                )
            )
        return BatchConfirmation(
            action=action,
            environment_id=environment_id,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            targets=tuple(targets),
            unavailable=unavailable,
            can_force=any(t.state in FORCEABLE_STATES for t in targets),
        )

    async def _batch_item(
        self,
        actor: Principal,
        environment_id: int,
        action: ContainerAction,
        container_id: str,
        names: Mapping[str, str],
        reviewed: Mapping[str, str],
        *,
        force: bool,
        volumes: bool,
    ) -> BatchItem:
        shown = container_id[:MAX_DISPLAYED_ID]
        name = (names.get(container_id) or reviewed.get(container_id, ""))[:MAX_DISPLAYED_NAME]

        def item(status: BatchStatus, message: str, holder: BusyView | None = None) -> BatchItem:
            return BatchItem(shown, short_id(shown), name or None, status, message, holder)

        try:
            outcome = await self._invoke(
                actor,
                environment_id,
                container_id,
                action,
                expected_name=names.get(container_id, ""),
                force=force,
                volumes=volumes,
                batch=True,
            )
        except (ResourceNotFound, ContainerGone):
            return item("not_found", "Not found, or already removed.")
        except PermissionDenied:
            return item("forbidden", "You can read this container but not change it.")
        except SelfProtected as error:
            return item("protected", str(error))
        except ObjectBusy as busy:
            return item(
                "busy",
                "Another action on this container is in progress.",
                self.describe_busy(busy, actor),
            )
        except (ContainerStateConflict, ValidationFailed) as error:
            return item("conflict", str(error))
        except EnvironmentUnavailable as error:
            return item("unreachable", str(error))
        except (EngineRefused, ContainerTooLarge) as error:
            return item("failed", str(error))
        except Exception as error:
            logger.error(
                "container_batch_item_failed",
                extra={
                    "environment_id": environment_id,
                    "container_id": shown,
                    "action": action,
                    "error": type(error).__name__,
                },
            )
            return item("failed", "The action could not be completed.")
        name = outcome.name[:MAX_DISPLAYED_NAME] or name
        if outcome.result == "unchanged":
            return item("unchanged", "No change was needed.")
        return item("done", "Done.")

    async def perform(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        action: ContainerAction,
        *,
        expected_name: str | None = None,
        force: bool = False,
        volumes: bool = False,
    ) -> ActionOutcome:
        """Run one action as its own audited operation; remove needs the name the caller reviewed.

        ValidationFailed, with no event recorded, when a remove carries no expected_name.
        """
        if action == "remove" and expected_name is None:
            raise ValidationFailed({"name": CHANGED_SINCE_REVIEW})
        return await self._invoke(
            actor,
            environment_id,
            container_id,
            action,
            expected_name=expected_name or "",
            force=force,
            volumes=volumes,
            batch=False,
        )

    async def _invoke(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        action: ContainerAction,
        *,
        expected_name: str,
        force: bool,
        volumes: bool,
        batch: bool,
    ) -> ActionOutcome:
        match action:
            case "start":
                return await self.start(actor, environment_id, container_id, batch=batch)
            case "stop":
                return await self.stop(actor, environment_id, container_id, batch=batch)
            case "restart":
                return await self.restart(actor, environment_id, container_id, batch=batch)
            case "kill":
                return await self.kill(actor, environment_id, container_id, batch=batch)
            case "pause":
                return await self.pause(actor, environment_id, container_id, batch=batch)
            case "unpause":
                return await self.unpause(actor, environment_id, container_id, batch=batch)
            case "remove":
                return await self.remove(
                    actor,
                    environment_id,
                    container_id,
                    expected_name=expected_name,
                    force=force,
                    volumes=volumes,
                    batch=batch,
                )

    async def _prepare(
        self,
        actor: Acting,
        environment_id: int,
        container_id: str,
        action: ContainerAction,
        *,
        batch: bool,
    ) -> _Target:
        audit = current_audit()
        if batch:
            audit.detail["batch"] = True
        if not is_canonical_id("container", container_id):
            raise ResourceNotFound("not found")
        try:
            if isinstance(actor, AutomationAuthority):
                await actor.authorise(ObjectKey(environment_id, "container", container_id))
            else:
                key = ResourceKey(environment_id, "container", container_id)
                await self._authoriser.require_access(actor, key, "write")
        except (EnvironmentUnavailable, ContainerTooLarge):
            audit.refuse()
            raise
        await self._hosts.environment(environment_id)
        try:
            inspected = await self._inspector.inspect(environment_id, container_id)
            client = await self._hosts.client(environment_id)
        except EnvironmentUnavailable:
            audit.reason = "unreachable"
            raise
        except ContainerTooLarge:
            audit.reason = "too_large"
            raise
        except ResourceNotFound:
            audit.reason = "not_found"
            raise ContainerGone() from None
        name = display_name([inspected.name], inspected.id)
        audit.detail["name"] = name
        if action in SELF_REFUSED_ACTIONS and self._self_identity().protects_container(
            container_id
        ):
            audit.reason = SELF_PROTECTED_REASON
            raise SelfProtected.container()
        return _Target(
            actor=actor,
            action=action,
            environment_id=environment_id,
            container_id=container_id,
            name=name,
            client=client,
            key=ObjectKey(environment_id, "container", container_id),
            holder=LockHolder(actor_of(actor), holder_label(actor), action),
            stop_wait=inspected.config.stop_timeout,
            stack=_stack_key_of(environment_id, inspected.config.labels),
        )

    @asynccontextmanager
    async def _locked(self, target: _Target) -> AsyncIterator[None]:
        actor = target.actor
        if isinstance(actor, AutomationAuthority):
            async with self._locked_for_automation(target, actor):
                yield
            return
        if target.stack is not None:
            running = self._locks.holder_of(target.stack)
            if running is not None:
                audit = current_audit()
                audit.reason = "object_busy"
                audit.detail["holder_action"] = running.action
                raise ObjectBusy(running)
        if target.action == "kill" and actor.is_administrator:
            # A kill has to cut short a stop or restart that is waiting out its grace period.
            current = self._locks.holder_of(target.key)
            if current is not None and current.action in LOCK_BYPASSED_BY_KILL:
                try:
                    yield
                finally:
                    self._locks.note(target.key, target.holder)
                return
        try:
            async with self._locks.hold(target.key, target.holder):
                yield
        except ObjectBusy as busy:
            audit = current_audit()
            audit.reason = "object_busy"
            audit.detail["holder_action"] = busy.holder.action
            raise

    @asynccontextmanager
    async def _locked_for_automation(
        self, target: _Target, authority: AutomationAuthority
    ) -> AsyncIterator[None]:
        """Take the container's lock for a run; a refusal to take it leaves no event.

        The run's job record carries the skip, so contention is not an audited failure.
        """
        audit = current_audit()
        if target.stack is not None:
            running = await self._locks.wait_free(target.stack, authority.lock_wait)
            if running is not None:
                audit.refuse()
                raise ObjectBusy(running)
        async with AsyncExitStack() as held:
            try:
                await held.enter_async_context(authority.hold(target.key, target.action))
            except (ObjectBusy, RunSkipped):
                audit.refuse()
                raise
            # Waiting for the container's lock can outlast the start of a stack operation.
            running = None if target.stack is None else self._locks.holder_of(target.stack)
            if running is not None:
                audit.refuse()
                raise ObjectBusy(running)
            yield

    async def _engine[T](
        self,
        target: _Target,
        call: Callable[[], Awaitable[T]],
        *,
        detail_of: Callable[[T], Mapping[str, DetailValue]] | None = None,
        release: bool = False,
    ) -> T:
        task: asyncio.Future[T] = asyncio.ensure_future(call())
        task.add_done_callback(retrieve_exception)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._record_interrupted(target, task, detail_of, release)
            raise
        except NotFound:
            current_audit().reason = "not_found"
            raise ContainerGone() from None
        except Conflict:
            raise await self._conflict(target) from None
        except RequestOutcomeUnknown:
            current_audit().reason = "outcome_unknown"
            raise EnvironmentUnavailable("outcome_unknown") from None
        except EnvironmentUnreachable:
            current_audit().reason = "unreachable"
            raise EnvironmentUnavailable("unreachable") from None
        except EngineError as error:
            current_audit().reason = "engine_error"
            logger.warning(
                "container_action_refused",
                extra={
                    "environment_id": target.environment_id,
                    "container_id": target.container_id,
                    "action": target.action,
                    "error": type(error).__name__,
                    "status": error.status_code,
                },
            )
            raise EngineRefused(engine_detail(error)) from None

    async def _conflict(self, target: _Target) -> ContainerStateConflict:
        current_audit().reason = "conflict"
        state: str | None = None
        self._inspector.forget(target.environment_id, target.container_id)
        try:
            inspected = await self._inspector.inspect(target.environment_id, target.container_id)
            state = inspected.state.status
        except ProductError:
            state = None
        return ContainerStateConflict(conflict_message(target.action, state))

    def _finish(self, target: _Target, result: ActionResult) -> ActionOutcome:
        self._inspector.forget(target.environment_id, target.container_id)
        return ActionOutcome(
            target.action, target.environment_id, target.container_id, target.name, result
        )

    async def _record_interrupted(
        self,
        target: _Target,
        task: asyncio.Future[Any],
        detail_of: Callable[[Any], Mapping[str, DetailValue]] | None,
        release: bool,
    ) -> None:
        done, _ = await asyncio.wait({task}, timeout=_settle_seconds(target))
        detail: dict[str, DetailValue] = {**current_audit().detail, "interrupted": True}
        failure = "outcome_unknown"
        if task in done and not task.cancelled():
            error = task.exception()
            if error is None:
                failure = ""
                if detail_of is not None:
                    detail.update(detail_of(task.result()))
            elif isinstance(error, NotFound):
                failure = "not_found"
            elif isinstance(error, Conflict):
                failure = "conflict"
            elif isinstance(error, RequestOutcomeUnknown):
                failure = "outcome_unknown"
            elif isinstance(error, EnvironmentUnreachable):
                failure = "unreachable"
            else:
                failure = "engine_error"
        self._inspector.forget(target.environment_id, target.container_id)
        await self._write_interrupted(target, detail, failure, release and not failure)

    async def _write_interrupted(
        self, target: _Target, detail: dict[str, DetailValue], failure: str, release: bool
    ) -> None:
        actor = actor_of(target.actor)
        action = _EVENTS[target.action]
        key = ResourceKey(target.environment_id, "container", target.container_id)
        try:
            async with self._database.session() as db:
                recorder = ActivityRecorder(db, self._clock)
                write = partial(
                    recorder.record_event,
                    actor,
                    action=action,
                    target_type="container",
                    target_id=target.container_id,
                    environment_id=target.environment_id,
                    result="success",
                    detail=detail,
                )
                if failure:
                    await recorder.record_failure(
                        actor,
                        action=action,
                        target_type="container",
                        target_id=target.container_id,
                        environment_id=target.environment_id,
                        reason=failure,
                        detail=detail,
                        message=_failure_message(failure, target.action),
                    )
                elif release:
                    controls = ResourceControlService(
                        db=db,
                        controls=ResourceControlRepository(db, self._clock),
                        activity=recorder,
                    )
                    await retry_busy(partial(_settle_removal, db, controls, key, write))
                else:
                    await retry_busy(write)
        except Exception as error:
            logger.error(
                "activity_event_write_failed",
                extra={
                    "action": action,
                    "result": "failure" if failure else "success",
                    "actor_user_id": actor.user_id if isinstance(actor, UserActor) else None,
                    "actor_subsystem": actor.name if isinstance(actor, SubsystemActor) else None,
                    "target_type": "container",
                    "target_id": target.container_id,
                    "environment_id": target.environment_id,
                    "reason": failure or None,
                    "error": type(error).__name__,
                },
            )


async def _is_anonymous(client: EngineClient, volume_name: str) -> bool | None:
    try:
        volume = await client.volumes.inspect(volume_name)
    except EngineError:
        return None
    return ANONYMOUS_VOLUME_LABEL in volume.labels
