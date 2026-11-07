"""Adopting an existing Swarm namespace as a managed stack, pasted or read from the cluster."""

import asyncio
from collections.abc import Callable, Sequence
from functools import partial

from app.auth.principal import Principal
from app.db.engine import Session, retry_busy
from app.db.repositories.clocked import Clock, utc_now
from app.db.repositories.environment import Environment, EnvironmentRepository
from app.db.repositories.errors import DuplicateError
from app.db.repositories.stack import StackNameError, StackRepository
from app.db.repositories.stack_dismissal import StackDismissalRepository
from app.services.access import ResourceKey, ResourceRef, canonical_stack_key
from app.services.activity import (
    ActivityRecorder,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.compose_setup import ComposeRuntime
from app.services.guards import require_administrator
from app.services.host_objects import audited_client, hold_all, in_transaction
from app.services.object_inspector import EngineObjectInspector
from app.services.object_locks import LockHolder, ObjectLockRegistry
from app.services.onboarding_ownership import Ownership
from app.services.resource_controls import ResourceControlService
from app.services.stack_deploy import ChangedSinceReview, StackNameInUse, StackRejected
from app.services.stack_document import (
    CompanionFile,
    ComposeDocumentRejected,
    VariableInput,
    swarm_stack_name,
)
from app.services.stack_file_views import secret_file_names
from app.services.stack_queries import StackQueryService
from app.services.stack_read_result import HostReadResult, adoption_inputs, swarm_host_state
from app.services.stack_views import StackAdopted, ValidationReport, VariableSet
from app.services.swarm_gate import require_swarm
from app.services.swarm_stack_files import SwarmStackFiles, named_reasons, stack_key, target_name
from app.services.swarm_stack_plan import SwarmStackPlanner
from app.services.swarm_stacks import read_namespace

__all__ = ["SwarmStackAdopter"]


def _checked_name(name: str) -> str:
    try:
        return swarm_stack_name(name)
    except ComposeDocumentRejected as rejected:
        raise StackRejected(rejected.fields, rejected.reason) from None


class SwarmStackAdopter(SwarmStackFiles):
    """The audited adoption of a Swarm namespace; one audit scope with one commit."""

    def __init__(
        self,
        *,
        db: Session,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        planner: SwarmStackPlanner,
        queries: StackQueryService,
        stacks: StackRepository,
        environments: EnvironmentRepository,
        controls: ResourceControlService,
        locks: ObjectLockRegistry,
        runtime: ComposeRuntime,
        activity: ActivityRecorder,
        poke: Callable[[int], None],
        clock: Clock = utc_now,
    ) -> None:
        self._db = db
        self._authoriser = authoriser
        self._hosts = inspector.hosts
        self._planner = planner
        self._queries = queries
        self._stacks = stacks
        self._dismissals = StackDismissalRepository(db, clock)
        self._environments = environments
        self._controls = controls
        self._locks = locks
        self._runtime = runtime
        self._activity = activity
        self._poke = poke

    @audited(
        action="stack_adopted",
        target_type="stack",
        target_id=target_name,
        environment_id=environment_argument,
    )
    async def adopt(
        self,
        actor: Principal,
        environment_id: int,
        namespace: str,
        *,
        compose_text: str = "",
        variables: Sequence[VariableInput] | None = None,
        files: Sequence[CompanionFile] = (),
        prepared: HostReadResult | None = None,
        as_is: bool = False,
        onboarding: Ownership | None = None,
    ) -> StackAdopted:
        """Record an existing namespace as a managed stack; the cluster is not changed.

        Administrators only. An existing stack control is kept. The first redeploy converges the
        namespace onto the file. `files` is the stack's companion file set, validated with the
        file and stored with it; a `prepared` result read from the cluster takes their place.
        With `as_is`, which needs `prepared`, the stack reads unchanged once it is stored;
        ChangedSinceReview, nothing stored, when a service of the namespace is no longer at the
        version `prepared` was read from. With `onboarding` the control goes to that owner and
        team instead of the caller, and none is claimed when it names neither.
        """
        if as_is and prepared is None:
            raise ValueError("adopting as is takes a prepared result")
        if prepared is not None:
            compose_text, variables, files = adoption_inputs(prepared)
        audit = current_audit()
        with named_reasons():
            await require_swarm(self._authoriser, self._hosts, actor, environment_id)
            require_administrator(actor)
            checked = _checked_name(namespace)
            audit.target_id = checked
            audit.detail["kind"] = "swarm"
            await self._queries.ready()
            environment = await self._hosts.environment(environment_id)
            values = await self._queries.variable_set(None, variables)
            client = await audited_client(self._hosts, environment_id)
            holder = LockHolder(UserActor.of(actor), actor.username, "adopt")
            directories = self._runtime.files
            companions = tuple(files)
            async with hold_all(self._locks, [stack_key(environment_id, checked)], holder):
                report = await self._planner.validate(
                    actor,
                    environment_id,
                    name=checked,
                    compose_text=compose_text,
                    variables=variables,
                    adopt=True,
                    files=companions,
                )
                members = await read_namespace(
                    client, environment_id, self._poke, canonical_stack_key(checked)
                )
                if prepared is not None and not prepared.matches_host(
                    swarm_host_state(members.services)
                ):
                    raise ChangedSinceReview
                token = directories.new_token()
                project = directories.project(canonical_stack_key(checked), token)
                stored = False
                try:
                    async with directories.materialise(
                        project, compose_text, companions, kind="swarm"
                    ):
                        adopted = await retry_busy(
                            partial(
                                in_transaction,
                                self._db,
                                partial(
                                    self._store_adopted,
                                    actor,
                                    environment,
                                    checked,
                                    token,
                                    compose_text,
                                    values,
                                    report,
                                    members.refs(environment_id),
                                    companions,
                                    prepared,
                                    as_is,
                                    onboarding,
                                ),
                            )
                        )
                        stored = True
                finally:
                    if not stored:
                        await asyncio.shield(directories.discard(token))
        return adopted

    async def _store_adopted(
        self,
        actor: Principal,
        environment: Environment,
        name: str,
        token: str,
        text: str,
        values: VariableSet,
        report: ValidationReport,
        members: Sequence[ResourceRef],
        companions: Sequence[CompanionFile],
        prepared: HostReadResult | None,
        as_is: bool,
        onboarding: Ownership | None,
    ) -> StackAdopted:
        audit = current_audit()
        try:
            stack_id = await self._stacks.insert(
                environment_id=environment.id,
                name=name,
                kind="swarm",
                origin="adopted",
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
        await self._stacks.record_operation(stack_id, operation="adopt", outcome="success")
        if as_is:
            await self._stacks.record_running_state(
                stack_id,
                file_digest=report.compose_digest,
                resolved_digest=report.resolved_digest,
            )
        key = ResourceKey(environment.id, "stack", name)
        if onboarding is None:
            created = await self._controls.claim(key, actor.user_id)
        else:
            created = onboarding.assigns and await self._controls.claim(
                key,
                onboarding.owner_user_id,
                team_ids=onboarding.team_ids,
                access_level=onboarding.level,
            )
        await self._controls.refresh_sub_resources(key, members)
        await self._environments.record_compose_version(environment.id, self._version_text())
        audit.detail.update(
            stack_id=stack_id,
            compose_digest=report.compose_digest,
            services=len(report.services),
            members=len(members),
            files=len(companions),
            secret_files=secret_file_names(companions),
            control_created=created,
            restricted=",".join(report.restricted),
            compose_version=self._version_text(),
        )
        if prepared is not None:
            audit.detail["file_source"] = prepared.file_source
        if as_is:
            audit.detail["as_is"] = True
        if onboarding is not None:
            audit.detail["onboarding"] = True
        if await self._dismissals.drop_dismissal(environment.id, "swarm", name):
            audit.detail["dismissal_cleared"] = True
        return StackAdopted(environment.id, name, stack_id, len(members), created)
