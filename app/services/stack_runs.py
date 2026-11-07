"""The shielded Compose sequences that carry out the audited stack operations."""

import asyncio
import contextvars
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    contextmanager,
)
from contextlib import aclosing as _aclosing
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Final, Literal

import anyio

from app.auth.errors import (
    SELF_PROTECTED_REASON,
    PermissionDenied,
    ProductError,
    ResourceNotFound,
    SelfProtected,
)
from app.auth.principal import Principal
from app.compose_cli.buildx import BUILD_TIMEOUT_SECONDS
from app.compose_cli.cli import ComposeCli, ComposeProject, ComposeRun, is_service_name
from app.compose_cli.errors import (
    ComposeCommandFailed,
    ComposeError,
    ComposeOutputInvalid,
    ComposeOutputTooLarge,
    ComposeTimedOut,
)
from app.compose_cli.parsers import ProgressEvent, PsEntry
from app.compose_cli.process import Completed, uninterrupted
from app.compose_cli.target import ComposeTarget
from app.crypto.vault import Vault
from app.db.engine import Database, Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.db.repositories.environment import EnvironmentRepository
from app.db.repositories.environment_tls import EnvironmentTlsRepository
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.stack import (
    DeployedState,
    StackOperation,
    StackRepository,
    StackStatus,
    StoredStack,
    VariableWrite,
)
from app.db.repositories.stack import StackOutcome as OutcomeKind
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import Conflict, EngineError, EnvironmentUnreachable
from app.docker_engine.errors import NotFound as EngineNotFound
from app.docker_engine.models import ContainerSummary, Network, Volume
from app.logging import get_logger
from app.scheduler.model import RunSkipped
from app.self_identity import SelfIdentitySource, no_identity
from app.services.access import (
    ResourceKey,
    ResourceRef,
    canonical_stack_key,
    is_canonical_id,
    stack_of,
)
from app.services.acting import (
    Acting,
    AutomationAuthority,
    actor_of,
    holder_label,
    is_administrator,
)
from app.services.activity import ActivityRecorder, Actor, AuditState, UserActor, current_audit
from app.services.authorisation import Authoriser
from app.services.compose_setup import ComposeRuntime, build_target
from app.services.container_actions import retrieve_exception
from app.services.containers import ContainerListTooLarge
from app.services.host_objects import (
    audited_client,
    hold_all,
    in_transaction,
    permitted_container_ids,
    read_networks,
    read_volumes,
    settled,
)
from app.services.image_bindings import ImageBindingService, observe_image
from app.services.object_inspector import (
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
    unavailable_from,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.pull_credentials import CredentialChoice
from app.services.resource_controls import ResourceControlService
from app.services.stack_build_runs import (
    BuiltImage,
    build_seconds,
    built_detail,
    check_bases,
    run_builds,
)
from app.services.stack_common import (
    RECORDING_ERRORS,
    changed_since_deploy,
    copy_stack_control,
    load_stack_control,
)
from app.services.stack_deploy import (
    ChangedSinceReview,
    NotDeployed,
    PendingChanges,
    PullPlan,
    StackError,
    StackOperationFailed,
    StackRejected,
    StackValidator,
    Validated,
    compose_to_stack_error,
    file_digest,
    in_project,
    make_scrubber,
    refuse_builds_over_json,
    require_build_rights,
)
from app.services.stack_document import STACK_NAME, VariableInput
from app.services.stack_queries import (
    SERVICE_LABEL,
    StackQueryService,
    read_project_containers,
    stack_key,
    validation_request,
)
from app.services.stack_views import (
    BuildSink,
    BuiltView,
    NotRunning,
    PartlyStopped,
    ProgressSink,
    ServiceState,
    StackOutcome,
    containers_digest,
    unwritable_members,
)

__all__ = [
    "RESTART_METHOD",
    "StackPlan",
    "StackRepos",
    "StackRun",
    "StackRunner",
    "member_refs",
    "named_reasons",
]

logger = get_logger(__name__)

MAX_PROGRESS_ENTRIES: Final = 12

SETTLE_MARGIN_SECONDS: Final = 30.0

DEADLINE_SLACK_SECONDS: Final = 2.0

RESTART_METHOD: Final = "stop_start"

type OperationName = Literal["deploy", "redeploy", "start", "stop", "restart", "remove"]

# These run a stored file without the pre-check; Compose's stderr can echo substituted values.
_STORED_FILE_OPERATIONS: Final[frozenset[OperationName]] = frozenset(
    {"start", "stop", "restart", "remove"}
)


def _status_of(entries: Sequence[PsEntry]) -> StackStatus:
    if not entries:
        return "inactive"
    running = sum(1 for entry in entries if entry.state == "running")
    if running == len(entries):
        return "active"
    return "partial" if running else "inactive"


def _service_states(entries: Sequence[PsEntry]) -> tuple[ServiceState, ...]:
    totals: dict[str, list[int]] = {}
    for entry in entries:
        counts = totals.setdefault(entry.service, [0, 0])
        counts[0] += 1
        counts[1] += entry.state == "running"
    return tuple(ServiceState(service, c[0], c[1]) for service, c in sorted(totals.items()))


def _progress_summary(events: Sequence[ProgressEvent]) -> str:
    latest: dict[str, str] = {}
    for event in events:
        if event.resource is not None:
            latest[event.resource] = f"{event.resource} {event.status or ''}".strip()
    return ", ".join(list(latest.values())[-MAX_PROGRESS_ENTRIES:])


def _compose_failure(error: ComposeCommandFailed, *, detail: bool = True) -> StackOperationFailed:
    if isinstance(error, ComposeTimedOut):
        reason, message = (
            "outcome_unknown",
            "Docker Compose did not finish in time. What it did on the host is not known.",
        )
    else:
        reason, message = "compose_failed", f"Docker Compose exited with status {error.exit_code}."
    return StackOperationFailed(
        reason,
        message,
        exit_code=error.exit_code,
        stderr=error.stderr if detail else "",
        event_count=len(error.events),
        progress=_progress_summary(error.events),
    )


@contextmanager
def named_reasons() -> Iterator[None]:
    """Put the failure reason of a stack error on the open audit scope."""
    try:
        yield
    except (StackError, StackRejected) as error:
        current_audit().reason = error.reason
        raise
    except EnvironmentUnavailable as error:
        current_audit().reason = error.kind
        raise
    except ObjectBusy:
        current_audit().reason = "object_busy"
        raise
    except ObjectTooLarge:
        current_audit().reason = "too_large"
        raise


@dataclass(frozen=True, slots=True)
class StackRepos:
    stacks: StackRepository
    environments: EnvironmentRepository
    controls: ResourceControlService
    control_repo: ResourceControlRepository
    bindings: ImageBindingService | None


@dataclass(slots=True)
class StackPlan:
    operation: OperationName
    record: StackOperation | None
    action: str
    actor: Acting
    environment_id: int
    stack: StoredStack
    client: EngineClient
    cli: ComposeCli
    project: ComposeProject
    target: ComposeTarget
    version: str
    values: Mapping[str, str] = field(repr=False)
    secrets: frozenset[str] = field(repr=False)
    text: str = field(repr=False)
    validated: Validated | None = None
    new_text: str | None = field(default=None, repr=False)
    new_variables: tuple[VariableWrite, ...] | None = field(default=None, repr=False)
    remove_orphans: bool = False
    orphans: tuple[str, ...] = ()
    containers: int = 0
    volumes: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    start_extra: tuple[str, ...] = ()
    sink: ProgressSink | None = None
    build_seconds: float = 0.0


@dataclass(slots=True)
class StackRun:
    abandoned: bool = False
    mutated: bool = False
    finished: bool = False
    observed: bool = False
    ps_failed: bool = False
    pulled: list[tuple[PullPlan, str | None]] = field(default_factory=list)
    built: list[BuiltImage] = field(default_factory=list)
    completed: Completed | None = None
    stopped: Completed | None = None
    phase: Literal["stop", "start"] | None = None
    events: tuple[ProgressEvent, ...] = ()
    services: tuple[PsEntry, ...] = ()
    containers: tuple[ContainerSummary, ...] = ()
    networks: tuple[Network, ...] = ()
    volumes: tuple[Volume, ...] = ()
    removed: list[str] = field(default_factory=list)
    kept_in_use: list[str] = field(default_factory=list)
    kept_changed: list[str] = field(default_factory=list)
    preexisting: list[str] = field(default_factory=list)
    keeps_control: bool = False

    def begin(self) -> None:
        """Note that a command is about to change the host."""
        self.mutated = True


def _binding_actor(plan: StackPlan) -> Actor | Principal:
    return plan.actor if isinstance(plan.actor, Principal) else actor_of(plan.actor)


def _event_count(run: StackRun) -> int:
    return sum(len(c.events) for c in (run.stopped, run.completed) if c is not None)


def member_refs(environment_id: int, run: StackRun) -> list[ResourceRef]:
    return [
        *(ResourceRef(environment_id, "container", c.id, c.labels) for c in run.containers),
        *(ResourceRef(environment_id, "network", n.id, n.labels) for n in run.networks),
        *(ResourceRef(environment_id, "volume", v.name, v.labels) for v in run.volumes),
    ]


def _failure_of(error: BaseException | None) -> tuple[str, str | None]:
    if error is None:
        return "interrupted", "The operation was interrupted before it finished."
    if isinstance(error, ComposeCommandFailed):
        failure = _compose_failure(error)
        return failure.reason, str(failure)
    if isinstance(error, StackError):
        return error.reason, str(error)
    if isinstance(error, EnvironmentUnavailable):
        return error.kind, str(error)
    return type(error).__name__, None


class StackRunner:
    """Runs the shielded Compose sequences behind the audited stack operations."""

    def __init__(
        self,
        *,
        db: Session,
        database: Database,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        queries: StackQueryService,
        stacks: StackRepository,
        environments: EnvironmentRepository,
        controls: ResourceControlService,
        control_repo: ResourceControlRepository,
        validator: StackValidator,
        tls: EnvironmentTlsRepository,
        locks: ObjectLockRegistry,
        runtime: ComposeRuntime,
        clock: Clock,
        vault: Vault,
        bindings: ImageBindingService | None,
        bindings_factory: Callable[[Session, ActivityRecorder], ImageBindingService] | None,
        self_identity: SelfIdentitySource | None,
    ) -> None:
        self._self_identity = self_identity or no_identity
        self._db = db
        self._database = database
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._queries = queries
        self._stacks = stacks
        self._environments = environments
        self._controls = controls
        self._control_repo = control_repo
        self._validator = validator
        self._tls = tls
        self._locks = locks
        self._runtime = runtime
        self._clock = clock
        self._vault = vault
        self._bindings = bindings
        self._bindings_factory = bindings_factory
        self._background: set[asyncio.Future[None]] = set()

    def version_text(self) -> str:
        return self._runtime.version_text or ""

    def refuse_self(
        self, environment_id: int, name: str, containers: Sequence[ContainerSummary] = ()
    ) -> None:
        """Raise SelfProtected, recording the reason, for dockmin's own stack."""
        identity = self._self_identity()
        if identity.protects_stack(environment_id, name) or any(
            identity.protects_container(container.id) for container in containers
        ):
            current_audit().reason = SELF_PROTECTED_REASON
            raise SelfProtected.stack(name)

    async def automation_stack(
        self, authority: AutomationAuthority, environment_id: int, name: str
    ) -> StoredStack:
        await self._hosts.environment(environment_id)
        if STACK_NAME.fullmatch(name) is None:
            raise ResourceNotFound("not found")
        row = await self._stacks.get(environment_id, name)
        if row is None or row.kind != "compose":
            raise ResourceNotFound("not found")
        await authority.authorise(stack_key(environment_id, row.name))
        return row

    async def authorise_members(
        self,
        authority: AutomationAuthority,
        environment_id: int,
        containers: Sequence[ContainerSummary],
    ) -> None:
        """Refuse unless the rule's owner may change every container of the project."""
        for container in containers:
            if not is_canonical_id("container", container.id):
                raise unwritable_members()
            try:
                await authority.authorise(ObjectKey(environment_id, "container", container.id))
            except (PermissionDenied, ResourceNotFound):
                raise unwritable_members() from None

    async def reread(self, environment_id: int, name: str) -> StoredStack:
        row = await self._stacks.get(environment_id, name)
        if row is None:
            raise ResourceNotFound("not found")
        return row

    async def require_writable_members(
        self, actor: Principal, environment_id: int, containers: Sequence[ContainerSummary]
    ) -> None:
        """Refuse a standard user unless every container of the project is theirs to change.

        Compose acts on all of a project's containers with their own, stricter, controls ignored.
        """
        if actor.is_administrator or not containers:
            return
        writable = await permitted_container_ids(
            self._authoriser, actor, environment_id, containers, "write"
        )
        if any(container.id not in writable for container in containers):
            raise unwritable_members()

    def require_idle(self, environment_id: int, containers: Sequence[ContainerSummary]) -> None:
        for container in containers:
            if not is_canonical_id("container", container.id):
                continue
            holder = self._locks.holder_of(ObjectKey(environment_id, "container", container.id))
            if holder is not None:
                audit = current_audit()
                audit.reason = "object_busy"
                audit.detail["holder_action"] = holder.action
                raise ObjectBusy(holder)

    async def _hold_volumes(
        self,
        held: AsyncExitStack,
        environment_id: int,
        holder: LockHolder,
        names: Sequence[str],
    ) -> None:
        await held.enter_async_context(
            hold_all(self._locks, [ObjectKey(environment_id, "volume", n) for n in names], holder)
        )

    async def _hold_stack(
        self,
        actor: Acting,
        key: ObjectKey,
        holder: LockHolder,
        operation: str,
        held: AsyncExitStack,
    ) -> None:
        if not isinstance(actor, AutomationAuthority):
            await held.enter_async_context(hold_all(self._locks, [key], holder))
            return
        try:
            await held.enter_async_context(actor.hold(key, operation))
        except (ObjectBusy, RunSkipped):
            current_audit().refuse()
            raise

    async def _require_redeployable(
        self, actor: Acting, environment_id: int, containers: Sequence[ContainerSummary]
    ) -> None:
        if not isinstance(actor, AutomationAuthority):
            await self.require_writable_members(actor, environment_id, containers)
            self.require_idle(environment_id, containers)
            return
        await self.authorise_members(actor, environment_id, containers)
        if not containers:
            raise NotDeployed
        running = sum(1 for c in containers if c.state == "running")
        if running == 0:
            raise NotRunning
        if running < len(containers):
            raise PartlyStopped
        try:
            self.require_idle(environment_id, containers)
        except ObjectBusy:
            current_audit().refuse()
            raise

    async def run_up(
        self,
        actor: Acting,
        environment_id: int,
        name: str,
        *,
        operation: Literal["deploy", "redeploy"],
        action: str,
        choices: Mapping[str, CredentialChoice],
        pull: bool,
        remove_orphans: bool,
        expected_digest: str | None,
        expected_inputs_digest: str | None,
        expected_containers: str | None,
        compose_text: str | None,
        variables: Sequence[VariableInput] | None,
        progress: ProgressSink | None,
        note: Mapping[str, DetailValue] | None = None,
        check_collisions: bool = False,
        rebuild: bool = False,
        expected_builds: str | None = None,
        allow_builds: bool = True,
    ) -> StackOutcome:
        audit = current_audit()
        if note:
            audit.detail.update(note)
        with named_reasons():
            if isinstance(actor, AutomationAuthority):
                row = await self.automation_stack(actor, environment_id, name)
            else:
                row = await self._queries.require_stack(actor, environment_id, name, "write")
            audit.target_id = row.name
            audit.detail["stack_id"] = row.id
            self.refuse_self(environment_id, row.name)
            if remove_orphans and expected_containers is None:
                raise StackRejected(
                    {"expected_containers": "Review the containers that would be removed first."},
                    "invalid",
                )
            environment = await self._hosts.environment(environment_id)
            cli = await self._queries.ready()
            client = await audited_client(self._hosts, environment_id)
            target = await build_target(environment, self._tls)
            holder = LockHolder(actor_of(actor), holder_label(actor), operation)
            async with AsyncExitStack() as held:
                await self._hold_stack(
                    actor, stack_key(environment_id, row.name), holder, operation, held
                )
                row = await self.reread(environment_id, row.name)
                if isinstance(actor, AutomationAuthority) and changed_since_deploy(
                    row.compose_file_digest,
                    row.deployed_file_digest,
                    row.inputs_digest,
                    row.deployed_inputs_digest,
                ):
                    raise PendingChanges
                if expected_digest is not None and row.compose_file_digest != expected_digest:
                    raise ChangedSinceReview
                if (
                    expected_inputs_digest is not None
                    and (row.inputs_digest or "") != expected_inputs_digest
                ):
                    raise ChangedSinceReview
                text = row.compose_file if compose_text is None else compose_text
                if not allow_builds:
                    await refuse_builds_over_json(text)
                await require_build_rights(actor, text)
                values = await self._queries.variable_set(row, variables)
                key = canonical_stack_key(row.name)
                project_containers = [
                    c for c in await read_project_containers(client) if in_project(c.labels, key)
                ]
                self.refuse_self(environment_id, row.name, project_containers)
                await self._require_redeployable(actor, environment_id, project_containers)
                if (
                    expected_containers is not None
                    and containers_digest([c.id for c in project_containers]) != expected_containers
                ):
                    raise ChangedSinceReview
                companions = await self._queries.stored_files(row)
                project = (
                    await held.enter_async_context(
                        self._runtime.files.materialise(
                            self._runtime.files.project(row.name, row.project_directory),
                            text,
                            companions,
                            builds=await self._queries.build_check(row, actor),
                        )
                    )
                ).project
                try:
                    validated = await self._validator.validate(
                        actor,
                        validation_request(
                            environment=environment,
                            client=client,
                            cli=cli,
                            target=target,
                            project=project,
                            text=text,
                            values=values,
                            deploy=True,
                            pull=pull,
                            choices=choices,
                            collisions=check_collisions,
                            files=companions,
                            builds=await self._queries.build_inputs(row, rebuild=rebuild),
                        ),
                        hold=partial(self._hold_volumes, held, environment_id, holder),
                    )
                except ObjectBusy:
                    if isinstance(actor, AutomationAuthority):
                        audit.refuse()
                    raise
                if expected_builds is not None and validated.builds_digest != expected_builds:
                    raise ChangedSinceReview
                orphans = tuple(
                    c.id
                    for c in project_containers
                    if c.labels.get(SERVICE_LABEL) not in validated.project.services
                )
                plan = StackPlan(
                    operation=operation,
                    record=operation,
                    action=action,
                    actor=actor,
                    environment_id=environment_id,
                    stack=row,
                    client=client,
                    cli=cli,
                    project=project,
                    target=target,
                    version=self.version_text(),
                    values=values.values,
                    secrets=values.secrets,
                    text=text,
                    validated=validated,
                    new_text=compose_text,
                    new_variables=values.writes,
                    remove_orphans=remove_orphans,
                    orphans=orphans,
                    containers=len(project_containers),
                    sink=progress,
                    build_seconds=build_seconds(validated.builds, BUILD_TIMEOUT_SECONDS),
                )
                audit.detail.update(
                    compose_digest=validated.compose_digest,
                    resolved_digest=validated.resolved_digest,
                    compose_version=plan.version,
                    services=len(validated.services),
                    images=len(validated.images),
                    pull_requested=pull,
                    remove_orphans=remove_orphans,
                    restricted=",".join(validated.restricted),
                )
                return await self.execute(plan, held)

    async def run_lifecycle(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        operation: Literal["start", "stop"],
        action: str,
        progress: ProgressSink | None,
    ) -> StackOutcome:
        audit = current_audit()
        with named_reasons():
            row = await self._queries.require_stack(actor, environment_id, name, "write")
            audit.target_id = row.name
            audit.detail["stack_id"] = row.id
            if operation == "stop":
                self.refuse_self(environment_id, row.name)
            environment = await self._hosts.environment(environment_id)
            cli = await self._queries.ready()
            client = await audited_client(self._hosts, environment_id)
            target = await build_target(environment, self._tls)
            holder = LockHolder(UserActor.of(actor), actor.username, operation)
            async with AsyncExitStack() as held:
                await held.enter_async_context(
                    hold_all(self._locks, [stack_key(environment_id, row.name)], holder)
                )
                row = await self.reread(environment_id, row.name)
                values = await self._queries.variable_set(row, None)
                key = canonical_stack_key(row.name)
                project_containers = [
                    c for c in await read_project_containers(client) if in_project(c.labels, key)
                ]
                if not project_containers:
                    raise NotDeployed
                if operation == "stop":
                    self.refuse_self(environment_id, row.name, project_containers)
                await self.require_writable_members(actor, environment_id, project_containers)
                self.require_idle(environment_id, project_containers)
                project = (
                    await held.enter_async_context(
                        self._runtime.files.materialise(
                            self._runtime.files.project(row.name, row.project_directory),
                            row.compose_file,
                            await self._queries.stored_files(row),
                            builds=await self._queries.build_check(row),
                        )
                    )
                ).project
                plan = StackPlan(
                    operation=operation,
                    record=operation,
                    action=action,
                    actor=actor,
                    environment_id=environment_id,
                    stack=row,
                    client=client,
                    cli=cli,
                    project=project,
                    target=target,
                    version=self.version_text(),
                    values=values.values,
                    secrets=values.secrets,
                    text=row.compose_file,
                    containers=len(project_containers),
                    sink=progress,
                )
                audit.detail["compose_version"] = plan.version
                return await self.execute(plan, held)

    async def execute(self, plan: StackPlan, held: AsyncExitStack) -> StackOutcome:
        """Run the Docker-changing sequence to its end even if the request is cancelled.

        The locks pass to this call: they are released once the sequence has ended and the
        request has finished with its result, whichever is later.
        """
        audit = current_audit()
        run = StackRun()
        locks = held.pop_all()
        plan.secrets = plan.secrets | plan.project.sensitive
        task: asyncio.Future[None] = asyncio.ensure_future(self._sequence(plan, run))
        task.add_done_callback(retrieve_exception)
        try:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                run.abandoned = True
                await self._interrupted(plan, task, run)
                raise
            except ComposeCommandFailed as error:
                failure = _compose_failure(
                    error,
                    detail=plan.operation not in _STORED_FILE_OPERATIONS
                    or is_administrator(plan.actor),
                )
                audit.reason = failure.reason
                audit.detail.update(
                    exit_code=error.exit_code, events=len(error.events), progress=failure.progress
                )
                self._note_restart(plan, run, audit.detail, failed=True)
                self._note_built(run, audit.detail)
                await self._record_failure_state(
                    plan,
                    run,
                    "outcome_unknown" if isinstance(error, ComposeTimedOut) else "failure",
                )
                await self._bind_completed_pulls(plan, run)
                raise failure from None
            except ComposeError as error:
                self._note_restart(plan, run, audit.detail, failed=True)
                self._note_built(run, audit.detail)
                await self._record_failure_state(plan, run, "failure")
                await self._bind_completed_pulls(plan, run)
                raise compose_to_stack_error(error) from None
            except Exception:
                self._note_restart(plan, run, audit.detail, failed=True)
                self._note_built(run, audit.detail)
                await self._record_failure_state(plan, run, "failure")
                await self._bind_completed_pulls(plan, run)
                raise
            return await self._commit(plan, run)
        finally:
            await self._release(task, locks)

    async def _release(self, task: asyncio.Future[None], locks: AsyncExitStack) -> None:
        if task.done():
            await uninterrupted(locks.aclose())
        else:
            task.add_done_callback(partial(self._release_late, locks))

    def _release_late(self, locks: AsyncExitStack, _task: asyncio.Future[None]) -> None:
        future = asyncio.ensure_future(locks.aclose())
        self._background.add(future)
        future.add_done_callback(self._background.discard)

    async def _apart[T](self, work: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """Run work with no audit scope open, so it can open one on a session of its own."""
        task = asyncio.get_running_loop().create_task(work(), context=contextvars.Context())
        task.add_done_callback(retrieve_exception)
        return await asyncio.shield(task)

    def _scope(
        self, recorder: ActivityRecorder, plan: StackPlan
    ) -> AbstractAsyncContextManager[AuditState]:
        return recorder.audit(
            actor_of(plan.actor),
            action=plan.action,
            target_type="stack",
            target_id=plan.stack.name,
            environment_id=plan.environment_id,
        )

    async def _commit(self, plan: StackPlan, run: StackRun) -> StackOutcome:
        audit = current_audit()
        self._note_run(plan, run, audit.detail)
        try:
            await retry_busy(
                partial(
                    in_transaction,
                    self._db,
                    partial(
                        self._finalise,
                        StackRepos(
                            self._stacks,
                            self._environments,
                            self._controls,
                            self._control_repo,
                            self._bindings,
                        ),
                        plan,
                        run,
                        audit.detail,
                    ),
                )
            )
        except asyncio.CancelledError:
            if audit.committed:
                await self._after_success(plan)
            else:
                await self._write_settled(plan, run, {**audit.detail, "interrupted": True}, None)
            raise
        except RECORDING_ERRORS as error:
            logger.error(
                "stack_finalise_retrying",
                extra={"environment_id": plan.environment_id, "error": type(error).__name__},
            )
            await self._finalise_fresh(plan, run, audit.detail)
        await self._after_success(plan)
        return self._outcome_of(plan, run)

    async def _finalise_fresh(
        self, plan: StackPlan, run: StackRun, detail: dict[str, DetailValue]
    ) -> None:
        try:
            await self._apart(partial(self._finalise_apart, plan, run, detail))
        except RECORDING_ERRORS as error:
            logger.error(
                "stack_finalise_failed",
                extra={"environment_id": plan.environment_id, "error": type(error).__name__},
            )
            raise StackOperationFailed(
                "outcome_unknown",
                "The operation ran, but dockmin could not record the result.",
            ) from None

    async def _finalise_apart(
        self, plan: StackPlan, run: StackRun, detail: dict[str, DetailValue]
    ) -> None:
        async with self._database.session() as db:
            recorder = ActivityRecorder(db, self._clock)
            repos = self._repos(db, recorder)
            async with self._scope(recorder, plan) as state:
                state.unchanged()
                await retry_busy(
                    partial(in_transaction, db, partial(self._finalise, repos, plan, run, detail))
                )

    async def _bind_completed_pulls(self, plan: StackPlan, run: StackRun) -> None:
        """Bind the images an operation pulled before it failed; best effort."""
        if not run.pulled or self._bindings_factory is None:
            return
        try:
            await self._apart(partial(self._bind_apart, plan, run))
        except Exception as error:
            logger.error(
                "stack_binding_failed",
                extra={"environment_id": plan.environment_id, "error": type(error).__name__},
            )

    async def _bind_apart(self, plan: StackPlan, run: StackRun) -> None:
        async with self._database.session() as db:
            recorder = ActivityRecorder(db, self._clock)
            bindings = self._repos(db, recorder).bindings
            if bindings is None:
                return

            async def record() -> None:
                for item, observed in run.pulled:
                    await bindings.record_pull(
                        _binding_actor(plan),
                        plan.environment_id,
                        item.parsed,
                        item.credentials,
                        observed,
                    )

            async with self._scope(recorder, plan) as state:
                state.unchanged()
                await retry_busy(partial(in_transaction, db, record))

    def _repos(self, db: Session, recorder: ActivityRecorder) -> StackRepos:
        control_repo = ResourceControlRepository(db, self._clock)
        return StackRepos(
            stacks=StackRepository(db, self._vault, self._clock),
            environments=EnvironmentRepository(db, self._clock),
            controls=ResourceControlService(db=db, controls=control_repo, activity=recorder),
            control_repo=control_repo,
            bindings=(
                None if self._bindings_factory is None else self._bindings_factory(db, recorder)
            ),
        )

    def _note_restart(
        self, plan: StackPlan, run: StackRun, detail: dict[str, DetailValue], *, failed: bool
    ) -> None:
        if plan.operation != "restart":
            return
        detail["method"] = RESTART_METHOD
        if plan.services:
            detail["services"] = ",".join(plan.services)
        if run.stopped is not None:
            detail["stop_exit_code"] = run.stopped.exit_code
        if failed and run.phase is not None:
            detail["phase"] = run.phase
            detail["unstarted"] = ",".join(plan.services)

    def _note_built(self, run: StackRun, detail: dict[str, DetailValue]) -> None:
        if run.built:
            detail["built"] = built_detail(run.built)

    def _note_run(self, plan: StackPlan, run: StackRun, detail: dict[str, DetailValue]) -> None:
        self._note_restart(plan, run, detail, failed=False)
        self._note_built(run, detail)
        if run.completed is not None:
            detail["events"] = _event_count(run)
            detail["exit_code"] = run.completed.exit_code
            detail["progress"] = _progress_summary(run.completed.events)
        detail["pulled"] = len(run.pulled)
        detail["services_running"] = sum(1 for entry in run.services if entry.state == "running")

    async def _finalise(
        self,
        repos: StackRepos,
        plan: StackPlan,
        run: StackRun,
        detail: dict[str, DetailValue],
    ) -> None:
        match plan.operation:
            case "deploy" | "redeploy":
                await self._finalise_up(repos, plan, run, detail)
            case "start" | "stop" | "restart":
                await self._finalise_lifecycle(repos, plan, run, detail)
            case "remove":
                await self._finalise_remove(repos, plan, run, detail)

    async def _finalise_up(
        self,
        repos: StackRepos,
        plan: StackPlan,
        run: StackRun,
        detail: dict[str, DetailValue],
    ) -> None:
        validated = plan.validated
        if validated is None or plan.record is None:
            raise RuntimeError("a deployment is finalised with its validation")
        stack_id = plan.stack.id
        digest = file_digest(plan.text)
        if plan.new_text is not None:
            await repos.stacks.update_file(
                stack_id, compose_file=plan.new_text, compose_file_digest=digest
            )
        if plan.new_variables is not None:
            await repos.stacks.replace_variables(stack_id, plan.new_variables)
        await repos.stacks.record_operation(
            stack_id,
            operation=plan.record,
            outcome="success",
            status=_status_of(run.services),
            deployed=DeployedState(digest, validated.resolved_digest),
        )
        await repos.environments.record_compose_version(plan.environment_id, plan.version)
        outcomes: list[str] = []
        if repos.bindings is not None:
            for item, observed in run.pulled:
                outcomes.append(
                    await repos.bindings.record_pull(
                        _binding_actor(plan),
                        plan.environment_id,
                        item.parsed,
                        item.credentials,
                        observed,
                    )
                )
        if outcomes:
            detail["binding"] = ",".join(outcomes)
        elif run.pulled:
            detail["binding"] = "not_recorded"
        key = ResourceKey(plan.environment_id, "stack", plan.stack.name)
        members = member_refs(plan.environment_id, run)
        if run.observed:
            await repos.controls.refresh_sub_resources(key, members)
        stack_key = canonical_stack_key(plan.stack.name)
        detail["sub_resources"] = sum(1 for m in members if stack_of(m.labels) == stack_key)
        detail["orphans_removed"] = len(plan.orphans) if plan.remove_orphans else 0
        released = 0
        project_labelled = {v.name for v in run.volumes if in_project(v.labels, stack_key)}
        for volume_name in validated.stale_volume_controls:
            if volume_name in project_labelled and is_canonical_id("volume", volume_name):
                released += await repos.controls.release(
                    ResourceKey(plan.environment_id, "volume", volume_name)
                )
        if released:
            detail["stale_controls_released"] = released
        after = await self._preexisting_after(repos, plan, run, validated)
        if after:
            detail["preexisting_after_deploy"] = len(after)
            run.preexisting.extend(after)

    async def _preexisting_after(
        self, repos: StackRepos, plan: StackPlan, run: StackRun, validated: Validated
    ) -> list[str]:
        if not run.observed:
            return []
        key = canonical_stack_key(plan.stack.name)
        found: list[str] = []
        for volume in run.volumes:
            if volume.name not in validated.volumes:
                continue
            own = await repos.control_repo.get_for(plan.environment_id, "volume", volume.name)
            if not in_project(volume.labels, key) or own is not None:
                found.append(f"Volume {volume.name}")
        for network in run.networks:
            if network.name not in validated.networks:
                continue
            own = await repos.control_repo.get_for(plan.environment_id, "network", network.id)
            if not in_project(network.labels, key) or own is not None:
                found.append(f"Network {network.name}")
        return found

    async def _finalise_lifecycle(
        self, repos: StackRepos, plan: StackPlan, run: StackRun, detail: dict[str, DetailValue]
    ) -> None:
        if plan.record is None:
            raise RuntimeError("a lifecycle operation is recorded")
        await repos.stacks.record_operation(
            plan.stack.id,
            operation=plan.record,
            outcome="success",
            status=_status_of(run.services),
        )
        await repos.environments.record_compose_version(plan.environment_id, plan.version)
        key = ResourceKey(plan.environment_id, "stack", plan.stack.name)
        members = member_refs(plan.environment_id, run)
        if run.observed:
            await repos.controls.refresh_sub_resources(key, members)
        stack_key = canonical_stack_key(plan.stack.name)
        detail["sub_resources"] = sum(1 for m in members if stack_of(m.labels) == stack_key)

    async def _finalise_remove(
        self, repos: StackRepos, plan: StackPlan, run: StackRun, detail: dict[str, DetailValue]
    ) -> None:
        stack_control = await load_stack_control(
            repos.control_repo, plan.environment_id, plan.stack.name
        )
        kept = [v for v in run.volumes if v.name not in run.removed]
        copied = 0
        if stack_control is not None:
            for volume in kept:
                copied += await copy_stack_control(
                    repos.control_repo, plan.environment_id, "volume", volume.name, stack_control
                )
        await repos.stacks.delete(plan.stack.id)
        released = False
        if not run.keeps_control:
            released = await repos.controls.release(
                ResourceKey(plan.environment_id, "stack", plan.stack.name)
            )
        await repos.environments.record_compose_version(plan.environment_id, plan.version)
        detail.update(
            volumes_removed=len(run.removed),
            volumes_kept=len(kept),
            controls_copied=copied,
            containers=plan.containers,
            control_released=released,
        )
        if run.keeps_control:
            detail["control_kept"] = True
        if run.kept_in_use:
            detail["volumes_in_use"] = len(run.kept_in_use)
        if run.kept_changed:
            detail["volumes_changed_since_review"] = len(run.kept_changed)

    async def _after_success(self, plan: StackPlan) -> None:
        if plan.operation == "remove":
            await asyncio.shield(self._runtime.files.discard(plan.stack.project_directory))

    def _outcome_of(
        self, plan: StackPlan, run: StackRun, *, interrupted: bool = False
    ) -> StackOutcome:
        name = plan.stack.name
        verbs = {
            "deploy": "Deployed",
            "redeploy": "Redeployed",
            "start": "Started",
            "stop": "Stopped",
            "restart": "Restarted",
            "remove": "Removed",
        }
        message = f"{verbs[plan.operation]} {name}."
        return StackOutcome(
            operation=plan.operation,
            environment_id=plan.environment_id,
            name=name,
            message=message,
            status=None if plan.operation == "remove" else _status_of(run.services),
            events=_event_count(run),
            exit_code=run.completed.exit_code if run.completed else None,
            pulled=tuple(item.reference for item, _ in run.pulled),
            services=_service_states(run.services),
            services_running=sum(1 for entry in run.services if entry.state == "running"),
            volumes_removed=tuple(run.removed),
            volumes_kept=(
                sum(1 for v in run.volumes if v.name not in run.removed)
                if plan.operation == "remove"
                else 0
            ),
            orphans_removed=len(plan.orphans) if plan.remove_orphans else 0,
            preexisting_after_deploy=tuple(run.preexisting),
            interrupted=interrupted,
            warnings=() if plan.validated is None else plan.validated.warnings,
            built=tuple(BuiltView(item.service, item.tag, item.reused) for item in run.built),
        )

    async def _sequence(self, plan: StackPlan, run: StackRun) -> None:
        """Run the operation under one deadline that covers pulls and every Compose command."""
        if run.abandoned:
            return
        deadline = (
            asyncio.get_running_loop().time() + plan.cli.operation_timeout + plan.build_seconds
        )
        try:
            async with asyncio.timeout_at(deadline + DEADLINE_SLACK_SECONDS) as limit:
                match plan.operation:
                    case "deploy" | "redeploy":
                        await self._sequence_up(plan, run, deadline)
                    case "start" | "stop":
                        await self._sequence_lifecycle(plan, run, deadline)
                    case "restart":
                        await self._sequence_restart(plan, run, deadline)
                    case "remove":
                        await self._sequence_down(plan, run, deadline)
        except TimeoutError:
            if not limit.expired():
                raise
            raise ComposeTimedOut(("sequence",), None, "") from None

    async def _sequence_up(self, plan: StackPlan, run: StackRun, deadline: float) -> None:
        validated = plan.validated
        if validated is None:
            raise RuntimeError("a deployment is run with its validation")
        sink = plan.sink
        for item in validated.pulls:
            run.begin()
            await self._pull(plan.client, item, sink)
            run.pulled.append((item, (await observe_image(plan.client, item.parsed))[1]))
        scrubber = make_scrubber(plan.secrets)
        project = plan.project
        if validated.builds:
            bases = await check_bases(plan.client, validated.builds)
            await run_builds(
                validated.builds,
                run.built,
                bases=bases,
                project=plan.project.name,
                client=plan.client,
                cli=plan.cli,
                buildx=self._runtime.buildx,
                target=plan.target,
                repository=self._stack_repository,
                stack_id=plan.stack.id,
                scrub=scrubber.scrub_text,
                deadline=deadline,
                on_start=run.begin,
                progress=sink.build if isinstance(sink, BuildSink) else None,
            )
            project = replace(
                plan.project, built=tuple((item.service, item.tag) for item in run.built)
            )
        async with plan.cli.invocation(
            project, plan.target, plan.values, scrubber, deadline=deadline
        ) as compose:
            run.completed = await compose.compose_up(
                remove_orphans=plan.remove_orphans,
                on_event=None if sink is None else sink.on_event,
                on_start=run.begin,
            )
            run.services = await self._ps(compose, run)
        await self._observe(plan, run)
        self._fallback_services(run)
        run.finished = True

    @asynccontextmanager
    async def _stack_repository(self) -> AsyncIterator[StackRepository]:
        async with self._database.session() as db:
            yield StackRepository(db, self._vault, self._clock)

    async def _ps(self, compose: ComposeRun, run: StackRun) -> tuple[PsEntry, ...]:
        try:
            return await compose.ps()
        except (ComposeCommandFailed, ComposeOutputInvalid, ComposeOutputTooLarge) as error:
            logger.warning("stack_ps_failed", extra={"error": type(error).__name__})
            run.ps_failed = True
            return ()

    def _fallback_services(self, run: StackRun) -> None:
        """Take the services from the Engine's container list when `ps` could not be read."""
        if run.ps_failed and not run.services:
            run.services = tuple(
                PsEntry(
                    c.names[0].lstrip("/") if c.names else c.id,
                    c.labels.get(SERVICE_LABEL, ""),
                    c.state,
                    None,
                    None,
                )
                for c in run.containers
            )

    async def _sequence_lifecycle(self, plan: StackPlan, run: StackRun, deadline: float) -> None:
        sink = plan.sink
        on_event = None if sink is None else sink.on_event
        scrubber = make_scrubber(plan.secrets)
        async with plan.cli.invocation(
            plan.project, plan.target, plan.values, scrubber, deadline=deadline
        ) as compose:
            if plan.operation == "start":
                run.completed = await compose.compose_start(on_event=on_event, on_start=run.begin)
            else:
                run.completed = await compose.compose_stop(on_event=on_event, on_start=run.begin)
            run.services = await self._ps(compose, run)
        await self._observe(plan, run)
        self._fallback_services(run)
        run.finished = True

    async def _sequence_restart(self, plan: StackPlan, run: StackRun, deadline: float) -> None:
        scrubber = make_scrubber(plan.secrets)
        async with plan.cli.invocation(
            plan.project, plan.target, plan.values, scrubber, deadline=deadline
        ) as compose:
            before = await compose.ps()
            present = {e.service for e in before}
            running = {e.service for e in before if e.state == "running"}
            plan.services = tuple(sorted(running | (set(plan.start_extra) & present)))
            if not plan.services:
                raise NotRunning
            if not all(is_service_name(name) for name in plan.services):
                raise StackOperationFailed(
                    "service_name_invalid", "A running service has a name Compose cannot be given."
                )
            on_event = None if plan.sink is None else plan.sink.on_event
            run.phase = "stop"
            run.stopped = await compose.compose_stop(
                services=plan.services, on_event=on_event, on_start=run.begin
            )
            run.phase = "start"
            plan.record = "start"
            run.completed = await compose.compose_start(
                services=plan.services, on_event=on_event, on_start=run.begin
            )
            run.services = await self._ps(compose, run)
        await self._observe(plan, run)
        self._fallback_services(run)
        run.finished = True

    async def _sequence_down(self, plan: StackPlan, run: StackRun, deadline: float) -> None:
        sink = plan.sink
        scrubber = make_scrubber(plan.secrets)
        async with plan.cli.invocation(
            plan.project, plan.target, plan.values, scrubber, deadline=deadline
        ) as compose:
            run.completed = await compose.compose_down(
                remove_orphans=plan.remove_orphans,
                on_event=None if sink is None else sink.on_event,
                on_start=run.begin,
            )
        for volume_name in plan.volumes:
            await self._remove_volume(plan, volume_name, run)
        await self._observe(plan, run)
        run.keeps_control = await self._queries.namespace_taken(
            plan.environment_id, plan.stack.name, plan.client
        )
        run.finished = True

    async def _still_removable(self, plan: StackPlan, name: str) -> bool:
        """Report whether the volume still carries this project's label and has no control."""
        if self._self_identity().protects_volume(plan.environment_id, name):
            return False
        self._inspector.forget_volume(plan.environment_id, name)
        try:
            volume = await self._inspector.volume(plan.environment_id, name)
        except ObjectTooLarge:
            return False
        if not in_project(volume.labels, canonical_stack_key(plan.stack.name)):
            return False
        async with self._database.session() as db:
            controls = ResourceControlRepository(db, self._clock)
            return await controls.get_for(plan.environment_id, "volume", volume.name) is None

    async def _remove_volume(self, plan: StackPlan, name: str, run: StackRun) -> None:
        client = plan.client
        try:
            if not await self._still_removable(plan, name):
                run.kept_changed.append(name)
                return
            await client.volumes.remove(name, force=False)
        except (ResourceNotFound, EngineNotFound):
            run.removed.append(name)
        except Conflict:
            run.kept_in_use.append(name)
        except EngineError as error:
            logger.warning("stack_volume_remove_failed", extra={"error": type(error).__name__})
            if isinstance(error, EnvironmentUnreachable):
                raise unavailable_from(error) from None
            run.kept_in_use.append(name)
        else:
            run.removed.append(name)

    async def _pull(self, client: EngineClient, item: PullPlan, sink: ProgressSink | None) -> None:
        progress = None if sink is None else sink.pull(item.reference)
        try:
            async with _aclosing(
                client.images.pull(item.reference, registry_auth=item.credentials.registry_auth)
            ) as stream:
                async for event in stream:
                    if progress is not None:
                        progress.on_pull(event)
        except EngineError as error:
            logger.warning(
                "stack_image_pull_failed",
                extra={"error": type(error).__name__, "status": error.status_code},
            )
            if isinstance(error, EnvironmentUnreachable):
                raise unavailable_from(error) from None
            raise StackOperationFailed(
                "pull_failed", "Pulling an image failed.", progress=f"Pull {item.reference}"
            ) from None

    async def _observe(self, plan: StackPlan, run: StackRun) -> None:
        """Read the project's containers, networks and volumes after the command; best effort."""
        key = canonical_stack_key(plan.stack.name)
        validated = plan.validated
        try:
            containers = await read_project_containers(plan.client)
            networks = await read_networks(plan.client)
            volumes = await read_volumes(plan.client)
        except (EnvironmentUnavailable, ContainerListTooLarge, ProductError) as error:
            logger.warning(
                "stack_observation_failed",
                extra={"environment_id": plan.environment_id, "error": type(error).__name__},
            )
            return
        project_networks = set() if validated is None else set(validated.networks)
        project_volumes = set() if validated is None else set(validated.volumes)
        run.containers = tuple(c for c in containers if in_project(c.labels, key))
        run.networks = tuple(
            n for n in networks if in_project(n.labels, key) or n.name in project_networks
        )
        run.volumes = tuple(
            v for v in volumes if in_project(v.labels, key) or v.name in project_volumes
        )
        run.observed = True

    async def _record_failure_state(
        self, plan: StackPlan, run: StackRun, outcome: OutcomeKind
    ) -> None:
        if not run.mutated or plan.record is None:
            return
        status: StackStatus = "partial" if outcome == "interrupted" else "error"
        with anyio.CancelScope(shield=True):
            try:
                async with self._database.session() as db:
                    repos = self._repos(db, ActivityRecorder(db, self._clock))
                    await retry_busy(
                        partial(
                            in_transaction,
                            db,
                            partial(
                                repos.stacks.record_operation,
                                plan.stack.id,
                                operation=plan.record,
                                outcome=outcome,
                                status=status,
                            ),
                        )
                    )
            except Exception as error:
                logger.error(
                    "stack_state_write_failed",
                    extra={"environment_id": plan.environment_id, "error": type(error).__name__},
                )

    async def _interrupted(
        self, plan: StackPlan, task: asyncio.Future[None], run: StackRun
    ) -> None:
        audit = current_audit()
        detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
        self._note_restart(plan, run, detail, failed=True)
        self._note_built(run, detail)
        with anyio.CancelScope(shield=True):
            finished, error = await settled(
                task, plan.cli.operation_timeout + plan.build_seconds + SETTLE_MARGIN_SECONDS
            )
            if finished and (error is None or isinstance(error, asyncio.CancelledError)):
                if run.mutated:
                    await self._write_settled(plan, run, detail, None)
                return
            if finished:
                await self._write_settled(plan, run, detail, error)
                return
            await self._write_settled(plan, run, detail, None, unknown=True)
            task.add_done_callback(partial(self._settle_late, plan, run, detail))

    def _settle_late(
        self,
        plan: StackPlan,
        run: StackRun,
        detail: Mapping[str, DetailValue],
        task: asyncio.Future[None],
    ) -> None:
        if task.cancelled():
            return
        late: dict[str, DetailValue] = {**detail, "settled_late": True}
        self._note_restart(plan, run, late, failed=True)
        self._note_built(run, late)
        future = asyncio.ensure_future(self._write_settled(plan, run, late, task.exception()))
        self._background.add(future)
        future.add_done_callback(self._background.discard)

    async def _write_settled(
        self,
        plan: StackPlan,
        run: StackRun,
        detail: dict[str, DetailValue],
        error: BaseException | None,
        *,
        unknown: bool = False,
    ) -> None:
        """Record how an operation that its request abandoned ended, on a session of its own."""
        try:
            if error is None and run.finished and not unknown:
                await self._apart(partial(self._settle_success, plan, run, detail))
                await self._after_success(plan)
                return
            if unknown:
                reason: str = "outcome_unknown"
                message: str | None = (
                    "The operation did not finish in time. What it did is not known."
                )
            else:
                reason, message = _failure_of(error)
            async with self._database.session() as db:
                await ActivityRecorder(db, self._clock).record_failure(
                    actor_of(plan.actor),
                    action=plan.action,
                    target_type="stack",
                    target_id=plan.stack.name,
                    environment_id=plan.environment_id,
                    reason=reason,
                    detail=detail,
                    message=message,
                )
        except Exception as failure:
            logger.error(
                "activity_event_write_failed",
                extra={
                    "action": plan.action,
                    "actor_user_id": plan.actor.user_id
                    if isinstance(plan.actor, Principal)
                    else None,
                    "target_type": "stack",
                    "environment_id": plan.environment_id,
                    "error": type(failure).__name__,
                },
            )
            return
        if run.mutated:
            await self._record_failure_state(
                plan,
                run,
                "outcome_unknown"
                if reason == "outcome_unknown"
                else ("interrupted" if error is None else "failure"),
            )
        await self._bind_completed_pulls(plan, run)

    async def _settle_success(
        self, plan: StackPlan, run: StackRun, detail: dict[str, DetailValue]
    ) -> None:
        async with self._database.session() as db:
            recorder = ActivityRecorder(db, self._clock)
            repos = self._repos(db, recorder)
            async with self._scope(recorder, plan) as state:
                state.detail.update(detail)
                self._note_run(plan, run, state.detail)
                await retry_busy(
                    partial(
                        in_transaction, db, partial(self._finalise, repos, plan, run, state.detail)
                    )
                )
