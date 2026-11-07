"""Swarm stacks: the audited create, save, deploy, redeploy, remove, forget and adopt operations."""

import asyncio
import contextvars
from collections.abc import Callable, Coroutine, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Final, Literal

import anyio

from app.auth.errors import SELF_PROTECTED_REASON, ProductError, SelfProtected
from app.auth.principal import Principal
from app.compose_cli.parsers import ProgressEvent
from app.compose_cli.process import uninterrupted
from app.crypto.vault import Vault
from app.db.engine import Database, Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.db.repositories.environment import Environment, EnvironmentRepository
from app.db.repositories.errors import DuplicateError
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.stack import (
    DeployedState,
    StackNameError,
    StackOperation,
    StackRepository,
    StackStatus,
    StoredStack,
)
from app.db.repositories.stack import StackOutcome as OutcomeKind
from app.db.repositories.stack_dismissal import StackDismissalRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import Conflict, EngineError
from app.docker_engine.errors import NotFound as EngineNotFound
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
from app.services.activity import (
    ActivityRecorder,
    Actor,
    Arguments,
    AuditState,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.compose_setup import ComposeRuntime
from app.services.container_actions import BusyView, retrieve_exception
from app.services.guards import require_administrator
from app.services.host_objects import (
    audited_client,
    busy_view,
    hold_all,
    in_transaction,
    read_services,
    settled,
)
from app.services.image_bindings import ImageBindingService
from app.services.object_inspector import (
    EngineObjectInspector,
    EnvironmentUnavailable,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.pull_credentials import CredentialChoice
from app.services.resource_controls import ResourceControlService
from app.services.service_restrictions import find_restricted_service
from app.services.service_spec import image_of
from app.services.stack_common import (
    RECORDING_ERRORS,
    changed_since_deploy,
    copy_stack_control,
    load_stack_control,
)
from app.services.stack_deploy import (
    ChangedSinceReview,
    PendingChanges,
    PreexistingObjects,
    StackError,
    StackNameInUse,
    StackOperationFailed,
    StackRejected,
)
from app.services.stack_document import (
    CompanionFile,
    ComposeDocumentRejected,
    VariableInput,
    swarm_stack_name,
)
from app.services.stack_file_views import checked_uses, secret_file_names
from app.services.stack_queries import StackQueryService
from app.services.stack_store import StackUpdated, store_update
from app.services.stack_views import (
    ProgressSink,
    StackCreated,
    ValidationReport,
    VariableSet,
)
from app.services.swarm import engine_failure
from app.services.swarm_common import PinnedImage
from app.services.swarm_gate import require_swarm
from app.services.swarm_stack_files import (
    SwarmStackFiles,
    named_reasons,
    stack_key,
    target_name,
)
from app.services.swarm_stack_outcomes import (
    SwarmStackOutcome,
    counts_of,
    force_update_counter,
    in_use,
    member_keys,
    rebinds_credential_set,
    resource_type,
)
from app.services.swarm_stack_plan import (
    PlanKind,
    PlanLine,
    PlannedService,
    SwarmDeployPlan,
    SwarmRemovePlan,
    SwarmStackPlanner,
    bound_spec,
    drifted_services,
    network_ids,
    spec_differs,
)
from app.services.swarm_stack_translate import FileItem
from app.services.swarm_stacks import read_namespace

logger = get_logger(__name__)

__all__ = [
    "RefusableOperation",
    "SwarmStackActions",
]

SETTLE_MARGIN_SECONDS: Final = 30.0


DEADLINE_SLACK_SECONDS: Final = 2.0


NETWORK_RETRY_SECONDS: Final = 2.0


MAX_PROGRESS_ENTRIES: Final = 12


TIMED_OUT: Final = "The operation did not finish in time. What it did on the host is not known."


INTERRUPTED: Final = "The operation was interrupted before it finished."


_LABELS: Final[Mapping[str, str]] = {
    "network": "Network",
    "config": "Config",
    "secret": "Secret",
    "service": "Service",
}


_REFUSAL_ACTIONS: Final[Mapping[str, str]] = {
    "update": "stack_updated",
    "deploy": "stack_deployed",
    "redeploy": "stack_redeployed",
    "remove": "stack_removed",
}


_AUTOMATION_MESSAGES: Final[Mapping[str, str]] = {
    "would_remove": "The redeploy would remove objects, which an automated redeploy never does.",
    "would_create": "The redeploy would create objects the host lacks, which an automated "
    "redeploy never does.",
    "drifted": "A service was changed on the host after the last deploy, and a redeploy would "
    "overwrite the change.",
    "unresolved": "An image's digest could not be looked up, and an automated redeploy never "
    "sends a tag without one.",
}


type Operation = Literal["deploy", "redeploy", "remove"]


type RefusableOperation = Literal["update", "deploy", "redeploy", "remove"]


class _Stop(Exception):
    """A step the sequence could not complete; `reason` is the activity failure reason."""

    def __init__(self, reason: str, message: str, failed: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.failed = failed


@dataclass(frozen=True, slots=True)
class _Repos:
    stacks: StackRepository
    environments: EnvironmentRepository
    controls: ResourceControlService
    control_repo: ResourceControlRepository
    bindings: ImageBindingService | None


@dataclass(slots=True)
class _Plan:
    operation: Operation
    action: str
    actor: Acting
    environment_id: int
    stack: StoredStack
    client: EngineClient
    timeout: float
    version: str
    sink: ProgressSink | None
    deploy: SwarmDeployPlan | None = field(default=None, repr=False)
    removal: SwarmRemovePlan | None = field(default=None, repr=False)


@dataclass(slots=True)
class _Run:
    abandoned: bool = False
    mutated: bool = False
    finished: bool = False
    observed: bool = False
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    sent: dict[str, PinnedImage] = field(default_factory=dict)
    events: dict[str, str] = field(default_factory=dict)
    failed: str | None = None
    members: tuple[ResourceRef, ...] = ()
    controls_copied: int = 0
    controls_released: int = 0

    def begin(self) -> None:
        """Note that a request is about to change the host."""
        self.mutated = True


def _refusal_action(arguments: Arguments) -> str:
    return _REFUSAL_ACTIONS[arguments["operation"]]


def _binding_actor(plan: _Plan) -> Actor | Principal:
    return plan.actor if isinstance(plan.actor, Principal) else actor_of(plan.actor)


def _logged_user(acting: Acting) -> int | None:
    return acting.user_id if isinstance(acting, Principal) else None


def _checked_name(name: str) -> str:
    try:
        return swarm_stack_name(name)
    except ComposeDocumentRejected as rejected:
        raise StackRejected(rejected.fields, rejected.reason) from None


def _listed(item: str) -> str:
    """Spell `kind:name` as `kind/name`, which the log scrubber does not take for a secret pair."""
    return item.replace(":", "/", 1)


def _joined(items: Sequence[str]) -> str:
    return ",".join(_listed(item) for item in items)


def _summary(events: Mapping[str, str]) -> str:
    return ", ".join(list(events.values())[-MAX_PROGRESS_ENTRIES:])


def _survivors(
    members: Sequence[PlanLine], run: _Run
) -> list[tuple[Literal["network", "config", "secret"], str]]:
    """Return the networks, configs and secrets a remove had to leave, with their ids."""
    ids = {f"{line.kind}:{line.name}": line.object_id for line in members}
    found: list[tuple[Literal["network", "config", "secret"], str]] = []
    for entry in run.kept:
        kind = entry.partition(":")[0]
        object_id = ids.get(entry)
        if object_id is None:
            continue
        match kind:
            case "network" | "config" | "secret":
                found.append((kind, object_id))
    return found


def _failure_of(error: BaseException | None) -> tuple[str, str | None]:
    if error is None:
        return "interrupted", INTERRUPTED
    if isinstance(error, _Stop):
        return error.reason, str(error)
    if isinstance(error, StackError):
        return error.reason, str(error)
    if isinstance(error, EnvironmentUnavailable):
        return error.kind, str(error)
    return type(error).__name__, None


class SwarmStackActions(SwarmStackFiles):
    """The audited Swarm stack operations; each is one audit scope with at most one commit."""

    def __init__(
        self,
        *,
        db: Session,
        database: Database,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        planner: SwarmStackPlanner,
        queries: StackQueryService,
        stacks: StackRepository,
        environments: EnvironmentRepository,
        controls: ResourceControlService,
        control_repo: ResourceControlRepository,
        locks: ObjectLockRegistry,
        runtime: ComposeRuntime,
        activity: ActivityRecorder,
        clock: Clock,
        vault: Vault,
        bindings: ImageBindingService | None,
        poke: Callable[[int], None],
        bindings_factory: Callable[[Session, ActivityRecorder], ImageBindingService] | None = None,
        self_identity: SelfIdentitySource | None = None,
    ) -> None:
        self._self_identity = self_identity or no_identity
        self._db = db
        self._database = database
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._planner = planner
        self._queries = queries
        self._stacks = stacks
        self._dismissals = StackDismissalRepository(db, clock)
        self._environments = environments
        self._controls = controls
        self._control_repo = control_repo
        self._locks = locks
        self._runtime = runtime
        self._activity = activity
        self._clock = clock
        self._vault = vault
        self._bindings = bindings
        self._bindings_factory = bindings_factory
        self._poke = poke
        self._background: set[asyncio.Future[None]] = set()

    def _refuse_self(self, environment_id: int, name: str, lines: Sequence[PlanLine]) -> None:
        """Raise SelfProtected, recording the reason, if a line acts on dockmin's own service."""
        identity = self._self_identity()
        if any(
            line.kind == "service"
            and line.action != "keep"
            and line.object_id is not None
            and identity.protects_service(environment_id, line.object_id)
            for line in lines
        ):
            current_audit().reason = SELF_PROTECTED_REASON
            raise SelfProtected.stack(name)

    def describe_busy(self, busy: ObjectBusy, viewer: Principal) -> BusyView:
        """Describe the lock holder to a viewer: names only for administrators."""
        return busy_view(busy, viewer, self._locks.now())

    @audited(
        action="stack_created",
        target_type="stack",
        target_id=target_name,
        environment_id=environment_argument,
    )
    async def create(
        self,
        actor: Principal,
        environment_id: int,
        *,
        name: str,
        compose_text: str,
        variables: Sequence[VariableInput] | None = None,
        choices: Mapping[str, CredentialChoice] | None = None,
        files: Sequence[CompanionFile] = (),
    ) -> StackCreated:
        """Validate and store a new Swarm stack under the creator's control; nothing is deployed.

        The credentials the first deployment will pull with are resolved too, so a choice still
        needed is raised before anything is stored. `files` is the stack's companion file set,
        validated with the file and stored with it.
        """
        audit = current_audit()
        with named_reasons():
            await require_swarm(self._authoriser, self._hosts, actor, environment_id)
            await self._authoriser.require_create(actor, environment_id)
            checked = _checked_name(name)
            audit.target_id = checked
            audit.detail["kind"] = "swarm"
            await self._queries.ready()
            environment = await self._hosts.environment(environment_id)
            values = await self._queries.variable_set(None, variables)
            await audited_client(self._hosts, environment_id)
            holder = LockHolder(UserActor.of(actor), actor.username, "create")
            directories = self._runtime.files
            companions = tuple(files)
            async with hold_all(self._locks, [stack_key(environment_id, checked)], holder):
                report = await self._planner.validate(
                    actor,
                    environment_id,
                    name=checked,
                    compose_text=compose_text,
                    variables=variables,
                    choices=choices,
                    resolve_images=True,
                    files=companions,
                )
                token = directories.new_token()
                project = directories.project(canonical_stack_key(checked), token)
                stored = False
                try:
                    async with directories.materialise(
                        project, compose_text, companions, kind="swarm"
                    ):
                        created = await retry_busy(
                            partial(
                                in_transaction,
                                self._db,
                                partial(
                                    self._store_new,
                                    actor,
                                    environment,
                                    checked,
                                    token,
                                    compose_text,
                                    values,
                                    report,
                                    companions,
                                ),
                            )
                        )
                        stored = True
                finally:
                    if not stored:
                        await asyncio.shield(directories.discard(token))
        return created

    async def _store_new(
        self,
        actor: Principal,
        environment: Environment,
        name: str,
        token: str,
        text: str,
        values: VariableSet,
        report: ValidationReport,
        companions: Sequence[CompanionFile],
    ) -> StackCreated:
        audit = current_audit()
        try:
            stack_id = await self._stacks.insert(
                environment_id=environment.id,
                name=name,
                kind="swarm",
                origin="created",
                project_directory=token,
                compose_file=text,
                compose_file_digest=report.compose_digest,
                created_by_user_id=actor.user_id,
            )
        except DuplicateError:
            raise StackNameInUse from None
        except StackNameError:
            raise StackRejected({"name": "Use a valid stack name."}, "invalid") from None
        await self._write_variables(stack_id, values)
        await self._store_files(stack_id, companions)
        key = ResourceKey(environment.id, "stack", name)
        replaced = await self._controls.release(key)
        created = await self._controls.claim(key, actor.user_id)
        await self._environments.record_compose_version(environment.id, self._version_text())
        audit.detail.update(
            stack_id=stack_id,
            origin="created",
            compose_digest=report.compose_digest,
            services=len(report.services),
            variables=len(values.writes or ()),
            secret_variables=values.secret_names,
            files=len(companions),
            secret_files=secret_file_names(companions),
            restricted=",".join(report.restricted),
            control_created=created,
            compose_version=self._version_text(),
        )
        if replaced:
            audit.detail["stale_control_replaced"] = True
        if await self._dismissals.drop_dismissal(environment.id, "swarm", name):
            audit.detail["dismissal_cleared"] = True
        return StackCreated(environment.id, name, stack_id, created, replaced)

    @audited(
        action="stack_updated",
        target_type="stack",
        target_id=target_name,
        environment_id=environment_argument,
    )
    async def update(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        *,
        compose_text: str | None = None,
        variables: Sequence[VariableInput] | None = None,
        expected_file_digest: str | None = None,
    ) -> StackUpdated:
        """Save a new file and variable set without deploying, after the full check.

        A None file or variable set keeps what is stored. A given `expected_file_digest` that is
        no longer the stored file's is ChangedSinceReview.
        """
        audit = current_audit()
        with named_reasons():
            row = await self._planner.require_stack(actor, environment_id, name, "write")
            environment = await self._hosts.environment(environment_id)
            audit.target_id = row.name
            audit.detail.update(kind="swarm", stack_id=row.id)
            await self._queries.ready()
            holder = LockHolder(UserActor.of(actor), actor.username, "update")
            async with hold_all(self._locks, [stack_key(environment_id, row.name)], holder):
                row = await self._reread(environment_id, row.name)
                if (
                    expected_file_digest is not None
                    and row.compose_file_digest != expected_file_digest
                ):
                    raise ChangedSinceReview
                text = row.compose_file if compose_text is None else compose_text
                values = await self._queries.variable_set(row, variables)
                report = await self._planner.validate(
                    actor,
                    environment_id,
                    name=row.name,
                    compose_text=text,
                    variables=variables,
                    existing=True,
                )
                updated = await retry_busy(
                    partial(
                        in_transaction,
                        self._db,
                        partial(
                            store_update,
                            self._stacks,
                            self._environments,
                            environment,
                            row,
                            text=text,
                            writes=values.writes,
                            compose_digest=report.compose_digest,
                            compose_version=self._version_text(),
                        ),
                    )
                )
        return updated

    @audited(
        action="stack_deployed",
        target_type="stack",
        target_id=target_name,
        environment_id=environment_argument,
    )
    async def deploy(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        *,
        sink: ProgressSink | None = None,
        choices: Mapping[str, CredentialChoice] | None = None,
    ) -> SwarmStackOutcome:
        """Run the stack's first deployment from its stored file and variables."""
        return await self._run_deploy(
            actor,
            environment_id,
            name,
            operation="deploy",
            action="stack_deployed",
            expected_file_digest=None,
            expected_inputs_digest=None,
            expected_plan=None,
            remove_orphans=False,
            sink=sink,
            choices=choices,
        )

    @audited(
        action="stack_redeployed",
        target_type="stack",
        target_id=target_name,
        environment_id=environment_argument,
    )
    async def redeploy(
        self,
        actor: Principal | AutomationAuthority,
        environment_id: int,
        name: str,
        *,
        expected_file_digest: str | None = None,
        expected_inputs_digest: str | None = None,
        expected_plan: str | None = None,
        remove_orphans: bool = False,
        sink: ProgressSink | None = None,
        choices: Mapping[str, CredentialChoice] | None = None,
        note: Mapping[str, DetailValue] | None = None,
    ) -> SwarmStackOutcome:
        """Redeploy exactly the plan the caller reviewed; a difference is ChangedSinceReview.

        A user passes all three `expected_*` values and no `note` (ValueError otherwise); an inputs
        digest that was null when shown is the empty text. An automation
        run passes neither, no `choices` and no `remove_orphans`, and is refused as StackRejected
        (`pending_changes`, `would_remove`, `would_create`, `drifted`, `unresolved`) before
        anything is sent unless the plan only updates services whose spec still matches the
        stack's file and whose images are all pinned. A service changed after the plan was made
        stops the sequence as `changed_since_review`.
        """
        if isinstance(actor, AutomationAuthority):
            if (
                expected_file_digest is not None
                or expected_inputs_digest is not None
                or expected_plan is not None
                or remove_orphans
                or choices
            ):
                raise ValueError(
                    "an automated redeploy takes no review, orphan or credential choice arguments"
                )
        elif None in (expected_file_digest, expected_inputs_digest, expected_plan):
            raise ValueError("a redeploy is bound to the file, inputs and plan its caller reviewed")
        elif note is not None:
            raise ValueError("only an automation run adds a note to a redeploy")
        return await self._run_deploy(
            actor,
            environment_id,
            name,
            operation="redeploy",
            action="stack_redeployed",
            expected_file_digest=expected_file_digest,
            expected_inputs_digest=expected_inputs_digest,
            expected_plan=expected_plan,
            remove_orphans=remove_orphans,
            sink=sink,
            choices=choices,
            note=note,
        )

    @audited(
        action="stack_removed",
        target_type="stack",
        target_id=target_name,
        environment_id=environment_argument,
    )
    async def remove(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        *,
        stack_id: int,
        expected_members: str,
        sink: ProgressSink | None = None,
    ) -> SwarmStackOutcome:
        """Remove every object carrying the stack's namespace, then forget the stack.

        Volumes are never touched. The stack id and the member digest bind the call to what a
        confirmation named; a difference is ChangedSinceReview and nothing runs.
        """
        audit = current_audit()
        with named_reasons():
            row = await self._planner.require_stack(actor, environment_id, name, "write")
            audit.target_id = row.name
            audit.detail.update(kind="swarm", stack_id=row.id)
            client = await audited_client(self._hosts, environment_id)
            holder = LockHolder(UserActor.of(actor), actor.username, "remove")
            async with AsyncExitStack() as held:
                await held.enter_async_context(
                    hold_all(self._locks, [stack_key(environment_id, row.name)], holder)
                )
                row = await self._reread(environment_id, row.name)
                if row.id != stack_id:
                    raise ChangedSinceReview
                first = await self._planner.remove_plan(actor, environment_id, row)
                self._refuse_self(environment_id, row.name, first.members)
                if first.digest != expected_members:
                    raise ChangedSinceReview
                await held.enter_async_context(
                    hold_all(self._locks, member_keys(environment_id, first.members), holder)
                )
                self._forget(environment_id, first.members)
                removal = await self._planner.remove_plan(actor, environment_id, row)
                if removal.digest != expected_members:
                    raise ChangedSinceReview
                plan = _Plan(
                    operation="remove",
                    action="stack_removed",
                    actor=actor,
                    environment_id=environment_id,
                    stack=row,
                    client=client,
                    timeout=self._runtime.timeout_seconds,
                    version=self._version_text(),
                    sink=sink,
                    removal=removal,
                )
                return await self._execute(plan, held)

    @audited(
        action="stack_forgotten",
        target_type="stack",
        target_id=target_name,
        environment_id=environment_argument,
    )
    async def forget(
        self, actor: Principal, environment_id: int, name: str, *, stack_id: int
    ) -> None:
        """Delete the stack's row and leave the cluster and the stack's control alone.

        Administrators only. The namespace's objects keep the access the control gives them.
        """
        audit = current_audit()
        with named_reasons():
            row = await self._planner.require_stack(actor, environment_id, name, "write")
            require_administrator(actor)
            audit.target_id = row.name
            audit.detail.update(kind="swarm", stack_id=row.id)
            holder = LockHolder(UserActor.of(actor), actor.username, "forget")
            async with hold_all(self._locks, [stack_key(environment_id, row.name)], holder):
                row = await self._reread(environment_id, row.name)
                if row.id != stack_id:
                    raise ChangedSinceReview
                await retry_busy(
                    partial(in_transaction, self._db, partial(self._store_forget, row))
                )
            await self._runtime.files.discard(row.project_directory)

    async def _store_forget(self, row: StoredStack) -> None:
        await self._stacks.delete(row.id)
        current_audit().detail["control_kept"] = True

    @audited(
        action=_refusal_action,
        target_type="stack",
        target_id=target_name,
        environment_id=environment_argument,
    )
    async def record_refusal(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        *,
        operation: RefusableOperation,
        error: Exception,
    ) -> None:
        """Record `error` as the failure of an operation refused before it started, and raise it.

        The caller needs write on the stack; without it the access error is raised and no row is
        written.
        """
        audit = current_audit()
        with named_reasons():
            row = await self._planner.require_stack(actor, environment_id, name, "write")
            audit.target_id = row.name
            audit.detail.update(kind="swarm", stack_id=row.id)
            if isinstance(error, ObjectBusy):
                audit.detail["holder_action"] = error.holder.action
            raise error

    def _forget(self, environment_id: int, lines: Sequence[PlanLine]) -> None:
        """Drop what this request remembers of the planned objects so a re-plan reads them anew."""
        for line in lines:
            if line.object_id is None:
                continue
            match line.kind:
                case "service":
                    self._inspector.forget_service(environment_id, line.object_id)
                case "config":
                    self._inspector.forget_config(environment_id, line.object_id)
                case "secret":
                    self._inspector.forget_secret(environment_id, line.object_id)
                case "network":
                    self._inspector.forget_network(environment_id, line.object_id)

    async def _hold(
        self,
        actor: Acting,
        keys: Sequence[ObjectKey],
        holder: LockHolder,
        operation: str,
        held: AsyncExitStack,
    ) -> None:
        if not isinstance(actor, AutomationAuthority):
            await held.enter_async_context(hold_all(self._locks, keys, holder))
            return
        for key in sorted(set(keys), key=lambda key: (key.object_type, key.object_id)):
            try:
                await held.enter_async_context(actor.hold(key, operation))
            except (ObjectBusy, RunSkipped):
                current_audit().refuse()
                raise

    def _refuse_unsafe(self, deploy: SwarmDeployPlan) -> None:
        """Raise StackRejected when the plan would remove, create or overwrite anything.

        Also when an image's digest is unresolved, which would send a bare tag.
        """
        counts: dict[str, int] = {}
        for action in ("remove", "create"):
            for kind in ("service", "network", "config", "secret"):
                found = len(deploy.with_action(action, kind))
                if found:
                    counts[f"{action}_{kind}s"] = found
        drifted = drifted_services(deploy)
        if drifted:
            counts["drifted_services"] = len(drifted)
        if deploy.unpinned:
            counts["unresolved_references"] = len(deploy.unpinned)
        if not counts:
            return
        current_audit().detail.update(counts)
        for prefix, reason in (("remove_", "would_remove"), ("create_", "would_create")):
            if any(key.startswith(prefix) for key in counts):
                raise StackRejected({"stack": _AUTOMATION_MESSAGES[reason]}, reason)
        if drifted:
            raise StackRejected({"stack": _AUTOMATION_MESSAGES["drifted"]}, "drifted")
        raise StackRejected({"stack": _AUTOMATION_MESSAGES["unresolved"]}, "unresolved")

    async def _run_deploy(
        self,
        actor: Acting,
        environment_id: int,
        name: str,
        *,
        operation: Literal["deploy", "redeploy"],
        action: str,
        expected_file_digest: str | None,
        expected_inputs_digest: str | None,
        expected_plan: str | None,
        remove_orphans: bool,
        sink: ProgressSink | None,
        choices: Mapping[str, CredentialChoice] | None,
        note: Mapping[str, DetailValue] | None = None,
    ) -> SwarmStackOutcome:
        audit = current_audit()
        if note:
            audit.detail.update(note)
        with named_reasons():
            row = await self._planner.require_stack(actor, environment_id, name, "write")
            audit.target_id = row.name
            audit.detail.update(kind="swarm", stack_id=row.id, remove_orphans=remove_orphans)
            await self._queries.ready()
            client = await audited_client(self._hosts, environment_id)
            holder = LockHolder(actor_of(actor), holder_label(actor), operation)
            async with AsyncExitStack() as held:
                await self._hold(
                    actor, [stack_key(environment_id, row.name)], holder, operation, held
                )
                row = await self._reread(environment_id, row.name)
                if isinstance(actor, AutomationAuthority) and changed_since_deploy(
                    row.compose_file_digest,
                    row.deployed_file_digest,
                    row.inputs_digest,
                    row.deployed_inputs_digest,
                ):
                    raise PendingChanges
                if (
                    expected_file_digest is not None
                    and row.compose_file_digest != expected_file_digest
                ):
                    raise ChangedSinceReview
                if (
                    expected_inputs_digest is not None
                    and (row.inputs_digest or "") != expected_inputs_digest
                ):
                    raise ChangedSinceReview
                await checked_uses(row.compose_file, await self._queries.stored_files(row), "swarm")
                first = await self._planner.deploy_plan(
                    actor,
                    environment_id,
                    row,
                    remove_orphans=remove_orphans,
                    resolve_images=False,
                )
                self._refuse_self(environment_id, row.name, first.lines)
                locked = member_keys(environment_id, first.lines)
                await self._hold(actor, locked, holder, operation, held)
                self._forget(environment_id, first.lines)
                deploy = await self._planner.deploy_plan(
                    actor, environment_id, row, remove_orphans=remove_orphans, choices=choices
                )
                if isinstance(actor, AutomationAuthority):
                    self._refuse_unsafe(deploy)
                if expected_plan is not None and deploy.digest != expected_plan:
                    raise ChangedSinceReview
                if operation == "deploy" and any(line.action == "remove" for line in deploy.lines):
                    raise ChangedSinceReview
                if not set(member_keys(environment_id, deploy.lines)) <= set(locked):
                    raise ChangedSinceReview
                plan = _Plan(
                    operation=operation,
                    action=action,
                    actor=actor,
                    environment_id=environment_id,
                    stack=row,
                    client=client,
                    timeout=self._runtime.timeout_seconds,
                    version=self._version_text(),
                    sink=sink,
                    deploy=deploy,
                )
                audit.detail.update(
                    compose_digest=deploy.compose_digest,
                    resolved_digest=deploy.resolved_digest,
                    compose_version=plan.version,
                    services=len(deploy.translation.services),
                    images=len(deploy.pinned),
                    unpinned=len(deploy.unpinned),
                    restricted=",".join(deploy.restricted),
                )
                return await self._execute(plan, held)

    async def _execute(self, plan: _Plan, held: AsyncExitStack) -> SwarmStackOutcome:
        """Run the Engine sequence to its end even if the request is cancelled.

        The locks pass to this call: they are released once the sequence has ended and the
        request has finished with its result, whichever is later.
        """
        audit = current_audit()
        run = _Run()
        locks = held.pop_all()
        task: asyncio.Future[None] = asyncio.ensure_future(self._sequence(plan, run))
        task.add_done_callback(retrieve_exception)
        try:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                run.abandoned = True
                await self._interrupted(plan, task, run)
                raise
            except _Stop as stop:
                self._note_run(plan, run, audit.detail)
                audit.reason = stop.reason
                await self._record_failure_state(
                    plan, run, "outcome_unknown" if stop.reason == "outcome_unknown" else "failure"
                )
                await self._bind_sent(plan, run)
                raise StackOperationFailed(
                    stop.reason,
                    str(stop),
                    progress=_summary(run.events),
                    result=self._outcome_of(plan, run, message=str(stop), failed=True),
                ) from None
            except Exception:
                self._note_run(plan, run, audit.detail)
                await self._record_failure_state(plan, run, "failure")
                await self._bind_sent(plan, run)
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
        self, recorder: ActivityRecorder, plan: _Plan
    ) -> AbstractAsyncContextManager[AuditState]:
        return recorder.audit(
            actor_of(plan.actor),
            action=plan.action,
            target_type="stack",
            target_id=plan.stack.name,
            environment_id=plan.environment_id,
        )

    def _repos(self, db: Session, recorder: ActivityRecorder) -> _Repos:
        control_repo = ResourceControlRepository(db, self._clock)
        return _Repos(
            stacks=StackRepository(db, self._vault, self._clock),
            environments=EnvironmentRepository(db, self._clock),
            controls=ResourceControlService(db=db, controls=control_repo, activity=recorder),
            control_repo=control_repo,
            bindings=(
                None if self._bindings_factory is None else self._bindings_factory(db, recorder)
            ),
        )

    async def _commit(self, plan: _Plan, run: _Run) -> SwarmStackOutcome:
        audit = current_audit()
        self._note_run(plan, run, audit.detail)
        try:
            await retry_busy(
                partial(
                    in_transaction,
                    self._db,
                    partial(
                        self._finalise,
                        self._repos_of_request(),
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
                "swarm_stack_finalise_retrying",
                extra={"environment_id": plan.environment_id, "error": type(error).__name__},
            )
            await self._finalise_fresh(plan, run, audit.detail)
        await self._after_success(plan)
        return self._outcome_of(plan, run)

    def _repos_of_request(self) -> _Repos:
        return _Repos(
            self._stacks,
            self._environments,
            self._controls,
            self._control_repo,
            self._bindings,
        )

    async def _finalise_fresh(self, plan: _Plan, run: _Run, detail: dict[str, DetailValue]) -> None:
        try:
            await self._apart(partial(self._finalise_apart, plan, run, detail))
        except RECORDING_ERRORS as error:
            logger.error(
                "swarm_stack_finalise_failed",
                extra={"environment_id": plan.environment_id, "error": type(error).__name__},
            )
            message = "The operation ran, but dockmin could not record the result."
            raise StackOperationFailed(
                "outcome_unknown",
                message,
                result=self._outcome_of(plan, run, message=message, failed=True),
            ) from None

    async def _finalise_apart(self, plan: _Plan, run: _Run, detail: dict[str, DetailValue]) -> None:
        async with self._database.session() as db:
            recorder = ActivityRecorder(db, self._clock)
            repos = self._repos(db, recorder)
            async with self._scope(recorder, plan) as state:
                state.unchanged()
                await retry_busy(
                    partial(in_transaction, db, partial(self._finalise, repos, plan, run, detail))
                )

    async def _finalise(
        self, repos: _Repos, plan: _Plan, run: _Run, detail: dict[str, DetailValue]
    ) -> None:
        match plan.operation:
            case "deploy" | "redeploy":
                await self._finalise_deploy(repos, plan, run, detail)
            case "remove":
                await self._finalise_remove(repos, plan, run, detail)

    async def _finalise_deploy(
        self, repos: _Repos, plan: _Plan, run: _Run, detail: dict[str, DetailValue]
    ) -> None:
        deploy = plan.deploy
        if deploy is None:
            raise RuntimeError("a deployment is finalised with its plan")
        await repos.stacks.record_operation(
            plan.stack.id,
            operation=plan.operation,
            outcome="success",
            status="active",
            deployed=DeployedState(deploy.compose_digest, deploy.resolved_digest),
        )
        await repos.environments.record_compose_version(plan.environment_id, plan.version)
        outcomes: list[str] = []
        if repos.bindings is not None:
            for pinned in run.sent.values():
                outcomes.append(
                    await repos.bindings.record_pull(
                        _binding_actor(plan),
                        plan.environment_id,
                        pinned.reference,
                        pinned.credentials,
                        pinned.digest,
                    )
                )
        if outcomes:
            detail["binding"] = ",".join(outcomes)
        elif run.sent:
            detail["binding"] = "not_recorded"
        run.controls_released = await self._release_removed(
            repos, plan, run, [line for line in deploy.lines if line.action == "remove"]
        )
        if run.controls_released:
            detail["controls_released"] = run.controls_released
        key = ResourceKey(plan.environment_id, "stack", plan.stack.name)
        if run.observed:
            await repos.controls.refresh_sub_resources(key, run.members)
        stack_key = canonical_stack_key(plan.stack.name)
        detail["sub_resources"] = sum(1 for m in run.members if stack_of(m.labels) == stack_key)

    async def _finalise_remove(
        self, repos: _Repos, plan: _Plan, run: _Run, detail: dict[str, DetailValue]
    ) -> None:
        removal = plan.removal
        if removal is None:
            raise RuntimeError("a removal is finalised with its plan")
        environment_id = plan.environment_id
        source = await load_stack_control(repos.control_repo, environment_id, plan.stack.name)
        copied = 0
        if source is not None:
            for volume in removal.volumes_kept:
                copied += await copy_stack_control(
                    repos.control_repo, environment_id, "volume", volume, source
                )
            for kind, object_id in _survivors(removal.members, run):
                copied += await copy_stack_control(
                    repos.control_repo, environment_id, kind, object_id, source
                )
        run.controls_copied = copied
        run.controls_released = await self._release_removed(repos, plan, run, removal.members)
        await repos.stacks.delete(plan.stack.id)
        released = await repos.controls.release(
            ResourceKey(environment_id, "stack", plan.stack.name)
        )
        detail.update(
            volumes_kept=len(removal.volumes_kept),
            controls_copied=copied,
            controls_released=run.controls_released,
            control_released=released,
        )

    async def _release_removed(
        self, repos: _Repos, plan: _Plan, run: _Run, lines: Sequence[PlanLine]
    ) -> int:
        """Drop the own control of every member the operation removed; the object is gone."""
        ids = {f"{line.kind}:{line.name}": line.object_id for line in lines}
        released = 0
        for entry in run.removed:
            kind = resource_type(entry.partition(":")[0])
            object_id = ids.get(entry)
            if kind is None or object_id is None or not is_canonical_id(kind, object_id):
                continue
            released += await repos.controls.release(
                ResourceKey(plan.environment_id, kind, object_id)
            )
        return released

    async def _after_success(self, plan: _Plan) -> None:
        if plan.operation == "remove":
            await asyncio.shield(self._runtime.files.discard(plan.stack.project_directory))

    def _note_run(self, plan: _Plan, run: _Run, detail: dict[str, DetailValue]) -> None:
        if plan.operation == "remove":
            detail["removed"] = _joined(run.removed)
            detail["removed_count"] = len(run.removed)
            detail.update(counts_of(run.removed))
        else:
            detail["created"] = _joined(run.created)
            detail["created_count"] = len(run.created)
            detail["updated"] = _joined(run.updated)
            detail["updated_count"] = len(run.updated)
            detail["removed"] = _joined(run.removed)
            detail["removed_count"] = len(run.removed)
            detail["unchanged_count"] = len(run.unchanged)
        if plan.operation == "remove":
            for kind in ("network", "config", "secret"):
                count = sum(1 for item in run.kept if item.startswith(f"{kind}:"))
                if count or kind == "network":
                    detail[f"{kind}s_kept"] = count
        elif run.kept:
            detail["kept"] = _joined(run.kept)
            detail["kept_count"] = len(run.kept)
        if run.failed is not None:
            detail["failed_object"] = _listed(run.failed)
        detail["progress"] = _summary(run.events)

    def _outcome_of(
        self,
        plan: _Plan,
        run: _Run,
        *,
        interrupted: bool = False,
        message: str | None = None,
        failed: bool = False,
    ) -> SwarmStackOutcome:
        name = plan.stack.name
        deploy = plan.deploy
        removal = plan.removal
        verbs = {"deploy": "Deployed", "redeploy": "Redeployed", "remove": "Removed"}
        status: StackStatus | None = None
        if failed:
            status = "error" if run.mutated and plan.operation != "remove" else None
        elif plan.operation != "remove":
            status = "active"
        return SwarmStackOutcome(
            operation=plan.operation,
            environment_id=plan.environment_id,
            name=name,
            message=message or f"{verbs[plan.operation]} {name}.",
            status=status,
            created=tuple(run.created),
            updated=tuple(run.updated),
            removed=tuple(run.removed),
            kept=tuple(run.kept),
            unchanged=tuple(run.unchanged),
            unpinned=() if deploy is None else deploy.unpinned,
            failed_object=run.failed,
            interrupted=interrupted,
            volumes_kept=0 if removal is None else len(removal.volumes_kept),
            controls_copied=run.controls_copied,
            controls_released=run.controls_released,
        )

    async def _sequence(self, plan: _Plan, run: _Run) -> None:
        """Run the operation under one deadline that covers every Engine call."""
        if run.abandoned:
            return
        deadline = asyncio.get_running_loop().time() + plan.timeout
        try:
            async with asyncio.timeout_at(deadline + DEADLINE_SLACK_SECONDS) as limit:
                match plan.operation:
                    case "deploy" | "redeploy":
                        await self._sequence_deploy(plan, run)
                    case "remove":
                        await self._sequence_remove(plan, run)
        except TimeoutError:
            if not limit.expired():
                raise
            raise _Stop("outcome_unknown", TIMED_OUT) from None

    def _emit(
        self,
        plan: _Plan,
        run: _Run,
        kind: PlanKind | Literal["image"],
        name: str,
        status: str,
        text: str,
        *,
        error: bool = False,
    ) -> None:
        resource = f"{'Image' if kind == 'image' else _LABELS[kind]} {name}"
        run.events[resource] = f"{resource} {text}"
        if plan.sink is not None:
            plan.sink.on_event(ProgressEvent(resource, None, status, text, error, None, None))

    def _stop(
        self,
        error: EngineError,
        plan: _Plan,
        run: _Run,
        verb: str,
        kind: PlanKind,
        name: str,
    ) -> _Stop:
        logger.warning(
            "swarm_stack_step_refused",
            extra={
                "environment_id": plan.environment_id,
                "step": f"{verb} {kind}",
                "error": type(error).__name__,
                "status": error.status_code,
            },
        )
        failed = f"{kind}:{name}"
        run.failed = failed
        self._emit(plan, run, kind, name, "Error", "Failed", error=True)
        classified = engine_failure(error, plan.environment_id, self._poke)
        if classified is not None:
            return _Stop(classified[0], str(classified[1]), failed)
        if verb == "create" and isinstance(error, Conflict):
            description = f"{_LABELS[kind]} {name}"
            return _Stop("preexisting_objects", str(PreexistingObjects([description])), failed)
        return _Stop("engine_rejected", f"Docker refused to {verb} {kind} {name}.", failed)

    def _refused(self, plan: _Plan, run: _Run, name: str) -> _Stop:
        failed = f"service:{name}"
        run.failed = failed
        self._emit(plan, run, "service", name, "Error", "Refused", error=True)
        return _Stop(
            "restricted",
            f"Service {name} would use options your role cannot deploy; nothing was sent for it.",
            failed,
        )

    def _changed(self, plan: _Plan, run: _Run, kind: PlanKind, name: str) -> _Stop:
        failed = f"{kind}:{name}"
        run.failed = failed
        self._emit(plan, run, kind, name, "Error", "Failed", error=True)
        return _Stop("changed_since_review", str(ChangedSinceReview()), failed)

    async def _sequence_deploy(self, plan: _Plan, run: _Run) -> None:
        deploy = plan.deploy
        if deploy is None:
            raise RuntimeError("a deployment is run with its plan")
        for reference, pinned in deploy.pinned.items():
            if pinned is not None:
                text = "Resolved" if pinned.pinned else "Unpinned"
                self._emit(plan, run, "image", reference, "Done", text)
        created: dict[tuple[str, str], tuple[str, str]] = {}
        absent = {(line.kind, line.name) for line in deploy.lines if line.action == "create"}
        for network in deploy.translation.networks:
            if not network.external and ("network", network.name) in absent:
                await self._create_network(plan, run, network.name, network.body)
        for kind in ("config", "secret"):
            for item in deploy.translation.files:
                if item.kind == kind and not item.external and (kind, item.name) in absent:
                    created[(kind, item.name)] = await self._create_file(plan, run, item)
        await self._restore_kept_networks(plan, run, deploy)
        for planned in deploy.services:
            await self._apply_service(plan, run, deploy, planned, created)
        if deploy.remove_orphans:
            for line in deploy.with_action("remove", "service"):
                await self._remove_service(plan, run, line)
        await self._remove_replaced(plan, run, deploy)
        for line in deploy.with_action("remove", "network"):
            await self._remove_network(plan, run, line)
        await self._observe(plan, run)
        run.finished = True

    async def _create_network(
        self, plan: _Plan, run: _Run, name: str, body: Mapping[str, Any]
    ) -> None:
        self._emit(plan, run, "network", name, "Working", "Creating")
        run.begin()
        try:
            await plan.client.networks.create(body)
        except EngineError as error:
            raise self._stop(error, plan, run, "create", "network", name) from None
        run.created.append(f"network:{name}")
        self._emit(plan, run, "network", name, "Done", "Created")

    async def _restore_kept_networks(self, plan: _Plan, run: _Run, deploy: SwarmDeployPlan) -> None:
        """Create each kept network the Engine no longer has; its list outlives a removal."""
        kept = {
            line.name: line.object_id
            for line in deploy.lines
            if line.action == "keep" and line.kind == "network" and line.object_id is not None
        }
        for network in deploy.translation.networks:
            network_id = kept.get(network.name)
            if network.external or network_id is None:
                continue
            try:
                held = await plan.client.networks.inspect(network_id)
            except EngineNotFound:
                await self._create_network(plan, run, network.name, network.body)
                continue
            except EngineError as error:
                raise self._stop(error, plan, run, "inspect", "network", network.name) from None
            if held.id != network_id:
                raise self._changed(plan, run, "network", network.name)

    async def _create_file(self, plan: _Plan, run: _Run, item: FileItem) -> tuple[str, str]:
        kind: PlanKind = item.kind
        self._emit(plan, run, kind, item.name, "Working", "Creating")
        data = item.data
        if data is None:
            raise self._changed(plan, run, kind, item.name)
        run.begin()
        try:
            if kind == "config":
                object_id = await plan.client.swarm_configs.create(
                    name=item.name, data=data.reveal(), labels=dict(item.labels)
                )
            else:
                object_id = await plan.client.swarm_secrets.create(
                    name=item.name, data=data.reveal(), labels=dict(item.labels)
                )
        except EngineError as error:
            raise self._stop(error, plan, run, "create", kind, item.name) from None
        run.created.append(f"{kind}:{item.name}")
        self._emit(plan, run, kind, item.name, "Done", "Created")
        return object_id, item.name

    async def _apply_service(
        self,
        plan: _Plan,
        run: _Run,
        deploy: SwarmDeployPlan,
        planned: PlannedService,
        created: Mapping[tuple[str, str], tuple[str, str]],
    ) -> None:
        name: str = planned.item.name
        pinned = deploy.pinned.get(planned.reference)
        client = plan.client
        if planned.action == "create":
            spec = self._bind(plan, run, deploy, planned, created, planned.force_update)
            self._emit(plan, run, "service", name, "Working", "Creating")
            run.begin()
            try:
                await client.swarm_services.create(
                    spec, registry_auth=None if pinned is None else pinned.credentials.registry_auth
                )
            except EngineError as error:
                raise self._stop(error, plan, run, "create", "service", name) from None
            run.created.append(f"service:{name}")
            self._note_sent(run, pinned)
            self._emit(plan, run, "service", name, "Done", "Created")
            return
        service_id = planned.service_id
        if service_id is None:
            raise self._changed(plan, run, "service", name)
        try:
            document = await client.swarm_services.document(service_id)
        except EngineNotFound:
            raise self._changed(plan, run, "service", name) from None
        except EngineError as error:
            raise self._stop(error, plan, run, "update", "service", name) from None
        if document.id != service_id:
            raise self._changed(plan, run, "service", name)
        if isinstance(plan.actor, AutomationAuthority) and document.version != planned.version:
            raise self._changed(plan, run, "service", name)
        spec = self._bind(plan, run, deploy, planned, created, force_update_counter(document.spec))
        rebinds = rebinds_credential_set(pinned)
        if not spec_differs(spec, document.spec, network_ids(deploy)) and not rebinds:
            run.unchanged.append(f"service:{name}")
            return
        image_changed = image_of(document.spec) != image_of(spec)
        sends_auth = image_changed or rebinds
        send_auth = pinned.credentials.registry_auth if sends_auth and pinned else None
        self._emit(plan, run, "service", name, "Working", "Updating")
        run.begin()
        try:
            await client.swarm_services.update(
                service_id,
                spec,
                version=document.version,
                registry_auth=send_auth,
                registry_auth_from=None if sends_auth else "spec",
            )
        except EngineError as error:
            raise await self._update_stop(
                error, plan, run, service_id, name, document.version
            ) from None
        self._inspector.forget_service(plan.environment_id, service_id)
        run.updated.append(f"service:{name}")
        if sends_auth:
            self._note_sent(run, pinned)
        self._emit(plan, run, "service", name, "Done", "Updated")

    def _bind(
        self,
        plan: _Plan,
        run: _Run,
        deploy: SwarmDeployPlan,
        planned: PlannedService,
        created: Mapping[tuple[str, str], tuple[str, str]],
        force_update: int,
    ) -> dict[str, Any]:
        try:
            spec = bound_spec(deploy, planned, created, force_update=force_update)
        except LookupError:
            raise self._changed(plan, run, "service", planned.item.name) from None
        if not is_administrator(plan.actor) and find_restricted_service(spec):
            raise self._refused(plan, run, planned.item.name)
        return spec

    def _note_sent(self, run: _Run, pinned: PinnedImage | None) -> None:
        if pinned is not None:
            run.sent[pinned.reference.key] = pinned

    async def _update_stop(
        self,
        error: EngineError,
        plan: _Plan,
        run: _Run,
        service_id: str,
        name: str,
        version: int,
    ) -> _Stop:
        if isinstance(error, EngineNotFound):
            return self._changed(plan, run, "service", name)
        stop = self._stop(error, plan, run, "update", "service", name)
        if stop.reason != "engine_rejected":
            return stop
        try:
            fresh = await plan.client.swarm_services.document(service_id)
        except EngineError:
            return stop
        if fresh.id != service_id or fresh.version != version:
            return self._changed(plan, run, "service", name)
        return stop

    async def _remove_service(self, plan: _Plan, run: _Run, line: PlanLine) -> None:
        service_id = line.object_id
        if service_id is None:
            return
        self._emit(plan, run, "service", line.name, "Working", "Removing")
        run.begin()
        try:
            await plan.client.swarm_services.remove(service_id)
        except EngineNotFound:
            pass
        except EngineError as error:
            raise self._stop(error, plan, run, "remove", "service", line.name) from None
        self._inspector.forget_service(plan.environment_id, service_id)
        run.removed.append(f"service:{line.name}")
        self._emit(plan, run, "service", line.name, "Done", "Removed")

    async def _remove_network(self, plan: _Plan, run: _Run, line: PlanLine) -> None:
        network_id = line.object_id
        if network_id is None:
            return
        self._emit(plan, run, "network", line.name, "Working", "Removing")
        for attempt in (0, 1):
            run.begin()
            try:
                await plan.client.networks.remove(network_id)
            except EngineNotFound:
                break
            except EngineError as error:
                if not in_use(error):
                    raise self._stop(error, plan, run, "remove", "network", line.name) from None
                if attempt == 0:
                    await asyncio.sleep(NETWORK_RETRY_SECONDS)
                    continue
                self._keep(plan, run, "network", line.name)
                return
            break
        self._inspector.forget_network(plan.environment_id, network_id)
        run.removed.append(f"network:{line.name}")
        self._emit(plan, run, "network", line.name, "Done", "Removed")

    async def _remove_file(self, plan: _Plan, run: _Run, line: PlanLine) -> None:
        object_id = line.object_id
        if object_id is None:
            return
        kind: PlanKind = line.kind
        self._emit(plan, run, kind, line.name, "Working", "Removing")
        run.begin()
        try:
            if kind == "config":
                await plan.client.swarm_configs.remove(object_id)
            else:
                await plan.client.swarm_secrets.remove(object_id)
        except EngineNotFound:
            pass
        except EngineError as error:
            if not in_use(error):
                raise self._stop(error, plan, run, "remove", kind, line.name) from None
            self._keep(plan, run, kind, line.name)
            return
        if kind == "config":
            self._inspector.forget_config(plan.environment_id, object_id)
        else:
            self._inspector.forget_secret(plan.environment_id, object_id)
        run.removed.append(f"{kind}:{line.name}")
        self._emit(plan, run, kind, line.name, "Done", "Removed")

    def _keep(self, plan: _Plan, run: _Run, kind: PlanKind, name: str) -> None:
        run.kept.append(f"{kind}:{name}")
        self._emit(plan, run, kind, name, "Done", "Kept")

    async def _remove_replaced(self, plan: _Plan, run: _Run, deploy: SwarmDeployPlan) -> None:
        """Remove the configs and secrets the file replaced, once no service references them."""
        declared = {(item.kind, item.name) for item in deploy.translation.files}
        candidates = [
            line
            for kind in ("config", "secret")
            for line in deploy.with_action("remove", kind)
            if (line.kind, line.name) not in declared
        ]
        if not candidates:
            return
        try:
            services = await read_services(plan.client, plan.environment_id, self._poke)
        except ProductError:
            for line in candidates:
                self._keep(plan, run, line.kind, line.name)
            return
        used: set[str] = set()
        for service in services:
            container = service.spec.task_template.get("ContainerSpec", {})
            if isinstance(container, Mapping):
                for group, key in (("Configs", "ConfigID"), ("Secrets", "SecretID")):
                    used.update(
                        reference[key]
                        for reference in container.get(group) or ()
                        if isinstance(reference, Mapping) and isinstance(reference.get(key), str)
                    )
        for line in candidates:
            if line.object_id in used:
                self._keep(plan, run, line.kind, line.name)
            else:
                await self._remove_file(plan, run, line)

    async def _sequence_remove(self, plan: _Plan, run: _Run) -> None:
        removal = plan.removal
        if removal is None:
            raise RuntimeError("a removal is run with its plan")
        members = removal.members
        for line in members:
            if line.kind == "service":
                await self._remove_service(plan, run, line)
        for line in members:
            if line.kind == "network":
                await self._remove_network(plan, run, line)
        for line in members:
            if line.kind == "config":
                await self._remove_file(plan, run, line)
        for line in members:
            if line.kind == "secret":
                await self._remove_file(plan, run, line)
        run.finished = True

    async def _observe(self, plan: _Plan, run: _Run) -> None:
        """Read the stack's members after the sequence for the control's display cache."""
        try:
            members = await read_namespace(
                plan.client,
                plan.environment_id,
                self._poke,
                canonical_stack_key(plan.stack.name),
            )
        except ProductError as error:
            logger.warning(
                "swarm_stack_observation_failed",
                extra={"environment_id": plan.environment_id, "error": type(error).__name__},
            )
            return
        run.members = members.refs(plan.environment_id)
        run.observed = True

    async def _record_failure_state(self, plan: _Plan, run: _Run, outcome: OutcomeKind) -> None:
        if not run.mutated:
            return
        operation: StackOperation = plan.operation
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
                                operation=operation,
                                outcome=outcome,
                                status=status,
                            ),
                        )
                    )
            except Exception as error:
                logger.error(
                    "swarm_stack_state_write_failed",
                    extra={"environment_id": plan.environment_id, "error": type(error).__name__},
                )

    async def _bind_sent(self, plan: _Plan, run: _Run) -> None:
        """Bind the images already sent to services before the operation stopped; best effort."""
        if not run.sent or self._bindings_factory is None:
            return
        try:
            await self._apart(partial(self._bind_apart, plan, run))
        except Exception as error:
            logger.error(
                "swarm_stack_binding_failed",
                extra={"environment_id": plan.environment_id, "error": type(error).__name__},
            )

    async def _bind_apart(self, plan: _Plan, run: _Run) -> None:
        async with self._database.session() as db:
            recorder = ActivityRecorder(db, self._clock)
            bindings = self._repos(db, recorder).bindings
            if bindings is None:
                return

            async def record() -> None:
                for pinned in run.sent.values():
                    await bindings.record_pull(
                        _binding_actor(plan),
                        plan.environment_id,
                        pinned.reference,
                        pinned.credentials,
                        pinned.digest,
                    )

            async with self._scope(recorder, plan) as state:
                state.unchanged()
                await retry_busy(partial(in_transaction, db, record))

    async def _interrupted(self, plan: _Plan, task: asyncio.Future[None], run: _Run) -> None:
        audit = current_audit()
        detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, plan.timeout + SETTLE_MARGIN_SECONDS)
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
        plan: _Plan,
        run: _Run,
        detail: Mapping[str, DetailValue],
        task: asyncio.Future[None],
    ) -> None:
        if task.cancelled():
            return
        late: dict[str, DetailValue] = {**detail, "settled_late": True}
        future = asyncio.ensure_future(self._write_settled(plan, run, late, task.exception()))
        self._background.add(future)
        future.add_done_callback(self._background.discard)

    async def _write_settled(
        self,
        plan: _Plan,
        run: _Run,
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
                message: str | None = TIMED_OUT
            else:
                reason, message = _failure_of(error)
            self._note_run(plan, run, detail)
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
                    "actor_user_id": _logged_user(plan.actor),
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
        await self._bind_sent(plan, run)

    async def _settle_success(self, plan: _Plan, run: _Run, detail: dict[str, DetailValue]) -> None:
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
