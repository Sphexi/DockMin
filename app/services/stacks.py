"""The audited stack operations and the project-directory sweeps."""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from functools import partial
from typing import Final, Literal

from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.crypto.vault import Vault
from app.db.engine import Database, Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.db.repositories.environment import Environment, EnvironmentRepository
from app.db.repositories.environment_tls import EnvironmentTlsRepository
from app.db.repositories.errors import DuplicateError
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.stack import (
    StackListRepository,
    StackRepository,
    StackStatus,
    StackVariableError,
    StoredStack,
)
from app.db.repositories.stack_dismissal import StackDismissalRepository
from app.db.repositories.stack_file import StackFileError, StoredFile
from app.docker_engine.client import EngineClient
from app.logging import get_logger
from app.scheduler.model import RunSkipped
from app.self_identity import SelfIdentitySource
from app.services.access import ResourceKey, canonical_stack_key
from app.services.acting import Acting, AutomationAuthority
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.compose_setup import ComposeRuntime, StackFiles, build_target
from app.services.container_actions import BusyView
from app.services.guards import require_administrator
from app.services.host_objects import audited_client, hold_all, in_transaction
from app.services.image_bindings import ImageBindingService
from app.services.object_inspector import EngineObjectInspector, EnvironmentUnavailable
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.onboarding_ownership import Ownership
from app.services.pull_credentials import CredentialChoice
from app.services.resource_controls import ResourceControlService
from app.services.stack_common import swarm_namespaces_apply
from app.services.stack_deploy import (
    ChangedSinceReview,
    NotDeployed,
    StackNameInUse,
    StackRejected,
    StackValidator,
    Validated,
    in_project,
    refuse_builds_over_json,
)
from app.services.stack_document import FILE_NAME, STACK_NAME, CompanionFile, VariableInput
from app.services.stack_file_views import (
    candidate_with,
    candidate_without,
    checked_content,
    checked_file_name,
    file_in_use,
    file_kind,
    file_view,
    secret_file_names,
    stored_file_error,
    stored_placeholders,
    uses_of,
)
from app.services.stack_queries import (
    StackQueryService,
    labelled_networks,
    labelled_volumes,
    read_project_containers,
    stack_key,
    validation_request,
)
from app.services.stack_read_result import HostReadResult, adoption_inputs, compose_host_state
from app.services.stack_runs import (
    RESTART_METHOD,
    StackPlan,
    StackRun,
    StackRunner,
    member_refs,
    named_reasons,
)
from app.services.stack_store import StackUpdated, store_update
from app.services.stack_views import (
    FileView,
    NotRunning,
    ProgressSink,
    StackAdopted,
    StackCreated,
    StackOutcome,
    VariableSet,
    checked_name,
    containers_digest,
    networks_digest,
)

__all__ = [
    "RefusableOperation",
    "StackService",
    "known_project_directories",
    "remove_orphan_directories",
    "sweep_orphan_directories",
]

logger = get_logger(__name__)

DIRECTORY_PAGE: Final = 200

type RefusableOperation = Literal["redeploy", "start", "stop", "restart", "remove"]

_REFUSAL_ACTIONS: Final[Mapping[RefusableOperation, str]] = {
    "redeploy": "stack_redeployed",
    "start": "stack_started",
    "stop": "stack_stopped",
    "restart": "stack_restarted",
    "remove": "stack_removed",
}


def _refusal_action(arguments: Arguments) -> str:
    return _REFUSAL_ACTIONS[arguments["operation"]]


def _stack_name_of(arguments: Arguments) -> str | None:
    name = str(arguments["name"]).strip()
    return name if STACK_NAME.fullmatch(name) else None


async def known_project_directories(db: Session, clock: Clock) -> frozenset[str]:
    """Return the directory token of every stack of every environment."""
    environments = EnvironmentRepository(db, clock)
    listing = StackListRepository(db)
    tokens: set[str] = set()
    after = 0
    while ids := await environments.list_ids_page(limit=DIRECTORY_PAGE, after_id=after):
        after = ids[-1]
        for environment_id in ids:
            cursor: str | None = None
            while True:
                page = await listing.page(environment_id, limit=DIRECTORY_PAGE, cursor=cursor)
                tokens.update(item.project_directory for item in page.items)
                cursor = page.next_cursor
                if cursor is None:
                    break
    return frozenset(tokens)


async def remove_orphan_directories(
    db: Session, files: StackFiles, clock: Clock, *, min_age_seconds: float = 0.0
) -> int:
    """Delete project directories no stack holds; failures are logged and never raised.

    A directory younger than `min_age_seconds` is kept, because a stack being created writes its
    directory before its row exists.
    """
    try:
        known = await known_project_directories(db, clock)
        removed = await files.sweep(known, min_age_seconds=min_age_seconds)
    except Exception as error:
        logger.warning("stack_orphan_sweep_failed", extra={"error": type(error).__name__})
        return 0
    if removed:
        logger.info("stack_orphan_directories_removed", extra={"count": removed})
    return removed


async def sweep_orphan_directories(
    database: Database, files: StackFiles, clock: Clock, *, min_age_seconds: float = 0.0
) -> int:
    """Delete project directories no stack holds, on a database session of its own."""
    async with database.session() as db:
        return await remove_orphan_directories(db, files, clock, min_age_seconds=min_age_seconds)


class StackService:
    """The audited stack operations; each is one audit scope with at most one commit."""

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
        activity: ActivityRecorder,
        clock: Clock,
        vault: Vault,
        bindings: ImageBindingService | None,
        bindings_factory: Callable[[Session, ActivityRecorder], ImageBindingService] | None = None,
        self_identity: SelfIdentitySource | None = None,
    ) -> None:
        self._db = db
        self._authoriser = authoriser
        self._hosts = inspector.hosts
        self._queries = queries
        self._stacks = stacks
        self._dismissals = StackDismissalRepository(db, clock)
        self._environments = environments
        self._controls = controls
        self._validator = validator
        self._tls = tls
        self._locks = locks
        self._runtime = runtime
        self._activity = activity
        self._runner = StackRunner(
            db=db,
            database=database,
            authoriser=authoriser,
            inspector=inspector,
            queries=queries,
            stacks=stacks,
            environments=environments,
            controls=controls,
            control_repo=control_repo,
            validator=validator,
            tls=tls,
            locks=locks,
            runtime=runtime,
            clock=clock,
            vault=vault,
            bindings=bindings,
            bindings_factory=bindings_factory,
            self_identity=self_identity,
        )

    def describe_busy(self, busy: ObjectBusy, viewer: Principal) -> BusyView:
        """Describe the lock holder to a viewer: names only for administrators."""
        return self._queries.describe_busy(busy, viewer)

    @audited(
        action="stack_created",
        target_type="stack",
        target_id=_stack_name_of,
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
        resolve_pulls: bool = False,
        check_collisions: bool = False,
        files: Sequence[CompanionFile] = (),
        template: str | None = None,
        allow_builds: bool = True,
    ) -> StackCreated:
        """Validate and store a new stack and put it under the creator's control; no deploy.

        With `resolve_pulls` the credentials the first deployment will pull with are resolved
        too, so a choice still needed is raised before anything is stored. With
        `check_collisions` a container name or published port a running container holds refuses.
        `files` is the stack's companion file set, validated with the file and stored with it.
        `template`, a reference the caller has already resolved, is recorded as given.
        Without `allow_builds` a file with a service that builds is refused (`build_not_available`).

        StackRejected for anything the checks refuse; StackNameInUse when a stack or a host
        project has the name; ObjectBusy; ComposeNotReady; EnvironmentUnavailable.
        """
        audit = current_audit()
        if template is not None:
            audit.detail["template"] = template
        with named_reasons():
            await self._authoriser.require_create(actor, environment_id)
            if not allow_builds:
                await refuse_builds_over_json(compose_text)
            environment = await self._hosts.environment(environment_id)
            checked = checked_name(name)
            audit.target_id = checked
            cli = await self._queries.ready()
            values = await self._queries.variable_set(None, variables)
            client = await audited_client(self._hosts, environment_id)
            target = await build_target(environment, self._tls)
            holder = LockHolder(UserActor.of(actor), actor.username, "create")
            directories = self._runtime.files
            companions = tuple(files)
            async with hold_all(self._locks, [stack_key(environment_id, checked)], holder):
                await self._queries.require_free(environment_id, checked, client)
                token = directories.new_token()
                project = directories.project(checked, token)
                stored = False
                try:
                    async with directories.materialise(
                        project, compose_text, companions
                    ) as written:
                        validated = await self._validator.validate(
                            actor,
                            validation_request(
                                environment=environment,
                                client=client,
                                cli=cli,
                                target=target,
                                project=written.project,
                                text=compose_text,
                                values=values,
                                deploy=resolve_pulls,
                                choices=choices,
                                collisions=check_collisions,
                                files=companions,
                            ),
                        )
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
                                    validated,
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
        validated: Validated,
        companions: Sequence[CompanionFile],
    ) -> StackCreated:
        audit = current_audit()
        try:
            stack_id = await self._stacks.insert(
                environment_id=environment.id,
                name=name,
                kind="compose",
                origin="created",
                project_directory=token,
                compose_file=text,
                compose_file_digest=validated.compose_digest,
                created_by_user_id=actor.user_id,
            )
        except DuplicateError:
            raise StackNameInUse from None
        if values.writes:
            try:
                await self._stacks.replace_variables(stack_id, values.writes)
            except StackVariableError:
                raise StackRejected(
                    {"variables": "A variable could not be stored."}, "invalid"
                ) from None
        await self._store_files(stack_id, companions)
        key = ResourceKey(environment.id, "stack", name)
        replaced = await self._controls.release(key)
        created = await self._controls.claim(key, actor.user_id)
        await self._environments.record_compose_version(environment.id, self._runner.version_text())
        audit.detail.update(
            stack_id=stack_id,
            origin="created",
            compose_digest=validated.compose_digest,
            services=len(validated.services),
            variables=len(values.writes or ()),
            secret_variables=values.secret_names,
            files=len(companions),
            secret_files=secret_file_names(companions),
            restricted=",".join(validated.restricted),
            control_created=created,
            compose_version=self._runner.version_text(),
        )
        if replaced:
            audit.detail["stale_control_replaced"] = True
        if await self._dismissals.drop_dismissal(environment.id, "compose", name):
            audit.detail["dismissal_cleared"] = True
        return StackCreated(
            environment.id, name, stack_id, created, replaced, warnings=validated.warnings
        )

    @audited(
        action="stack_updated",
        target_type="stack",
        target_id=_stack_name_of,
        environment_id=environment_argument,
    )
    async def update(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        *,
        expected_file_digest: str,
        compose_text: str | None = None,
        variables: Sequence[VariableInput] | None = None,
    ) -> StackUpdated:
        """Save a new file and a new variable set without deploying.

        A None file or variable set keeps what is stored. The candidate is validated, `config`
        included, so a file that could not deploy cannot be saved. `expected_file_digest` is the
        stored file's digest when the edit began; ChangedSinceReview when it no longer is.
        """
        audit = current_audit()
        with named_reasons():
            row = await self._queries.require_stack(actor, environment_id, name, "write")
            environment = await self._hosts.environment(environment_id)
            audit.target_id = row.name
            audit.detail["stack_id"] = row.id
            cli = await self._queries.ready()
            client = await audited_client(self._hosts, environment_id)
            target = await build_target(environment, self._tls)
            holder = LockHolder(UserActor.of(actor), actor.username, "update")
            async with hold_all(self._locks, [stack_key(environment_id, row.name)], holder):
                row = await self._runner.reread(environment_id, row.name)
                if row.compose_file_digest != expected_file_digest:
                    raise ChangedSinceReview
                text = row.compose_file if compose_text is None else compose_text
                values = await self._queries.variable_set(row, variables)
                companions = await self._queries.stored_files(row)
                async with self._queries.scratch(
                    row.name,
                    text,
                    kind=row.kind,
                    files=companions,
                    builds=await self._queries.build_check(row, actor),
                ) as project:
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
                            files=companions,
                            builds=await self._queries.build_inputs(row),
                        ),
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
                            compose_digest=validated.compose_digest,
                            compose_version=self._runner.version_text(),
                        ),
                    )
                )
        return updated

    @audited(
        action="stack_updated",
        target_type="stack",
        target_id=_stack_name_of,
        environment_id=environment_argument,
    )
    async def write_file(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        file: str,
        *,
        content: str | None,
        secret: bool,
        expected_inputs_digest: str | None = None,
    ) -> FileView:
        """Add a companion file or replace one, checking the whole stack with it; no deploy.

        A secret given no `content` keeps the value stored; any other file needs content. The
        caller needs write on the stack and on every container of its project.
        `expected_inputs_digest` is the stack's inputs digest when the change began, the empty
        text for none; ChangedSinceReview when it no longer is. StackRejected keyed `name`,
        `content` or `files` for a file the rules refuse, or by Compose path when the stack
        would no longer pass the checks with it.
        """
        audit = current_audit()
        with named_reasons():
            row = await self._queries.require_stack(actor, environment_id, name, "write")
            audit.target_id = row.name
            audit.detail["stack_id"] = row.id
            checked = checked_file_name(file)
            text = checked_content(content)
            audit.detail.update(file_written=checked, file_kind=file_kind(secret))
            environment = await self._hosts.environment(environment_id)
            cli = await self._queries.ready()
            client = await audited_client(self._hosts, environment_id)
            target = await build_target(environment, self._tls)
            holder = LockHolder(UserActor.of(actor), actor.username, "update")
            async with hold_all(self._locks, [stack_key(environment_id, row.name)], holder):
                row = await self._runner.reread(environment_id, row.name)
                self._require_inputs(row, expected_inputs_digest)
                await self._require_writable_project(actor, row, client)
                candidate = candidate_with(
                    await self._queries.stored_files(row), checked, text, secret
                )
                values = await self._queries.variable_set(row, None)
                async with self._queries.scratch(
                    row.name,
                    row.compose_file,
                    kind=row.kind,
                    files=candidate,
                    builds=await self._queries.build_check(row, actor),
                ) as project:
                    await self._validator.validate(
                        actor,
                        validation_request(
                            environment=environment,
                            client=client,
                            cli=cli,
                            target=target,
                            project=project,
                            text=row.compose_file,
                            values=values,
                            files=candidate,
                            builds=await self._queries.build_inputs(row),
                        ),
                    )
                uses = await uses_of(row.compose_file, candidate, row.kind)
                stored = await retry_busy(
                    partial(
                        in_transaction,
                        self._db,
                        partial(self._store_file, environment, row, checked, text, secret),
                    )
                )
        return file_view(stored, uses.get(checked, ()))

    async def _store_file(
        self,
        environment: Environment,
        row: StoredStack,
        name: str,
        content: str | None,
        secret: bool,
    ) -> StoredFile:
        stored = await self._put_file(row.id, name, content, secret)
        await self._environments.record_compose_version(environment.id, self._runner.version_text())
        current_audit().detail["compose_version"] = self._runner.version_text()
        return stored

    @audited(
        action="stack_updated",
        target_type="stack",
        target_id=_stack_name_of,
        environment_id=environment_argument,
    )
    async def remove_file(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        file: str,
        *,
        expected_inputs_digest: str | None = None,
    ) -> None:
        """Delete a companion file, checking the whole stack without it; no deploy.

        ResourceNotFound when the stack has no such file; StackRejected (`file_in_use`) while a
        document names it. The authorisation and the digest binding are those of `write_file`.
        """
        audit = current_audit()
        with named_reasons():
            row = await self._queries.require_stack(actor, environment_id, name, "write")
            audit.target_id = row.name
            audit.detail["stack_id"] = row.id
            if FILE_NAME.fullmatch(file) is None:
                raise ResourceNotFound("not found")
            environment = await self._hosts.environment(environment_id)
            cli = await self._queries.ready()
            client = await audited_client(self._hosts, environment_id)
            target = await build_target(environment, self._tls)
            holder = LockHolder(UserActor.of(actor), actor.username, "update")
            async with hold_all(self._locks, [stack_key(environment_id, row.name)], holder):
                row = await self._runner.reread(environment_id, row.name)
                self._require_inputs(row, expected_inputs_digest)
                await self._require_writable_project(actor, row, client)
                items = await self._stacks.files.files(row.id)
                found = next((item for item in items if item.name == file), None)
                if found is None:
                    raise ResourceNotFound("not found")
                audit.detail.update(file_removed=file, file_kind=file_kind(found.is_secret))
                uses = await uses_of(row.compose_file, stored_placeholders(items), row.kind)
                if uses.get(file):
                    raise file_in_use(file, uses[file])
                candidate = candidate_without(await self._queries.stored_files(row), file)
                values = await self._queries.variable_set(row, None)
                async with self._queries.scratch(
                    row.name,
                    row.compose_file,
                    kind=row.kind,
                    files=candidate,
                    builds=await self._queries.build_check(row, actor),
                ) as project:
                    await self._validator.validate(
                        actor,
                        validation_request(
                            environment=environment,
                            client=client,
                            cli=cli,
                            target=target,
                            project=project,
                            text=row.compose_file,
                            values=values,
                            files=candidate,
                            builds=await self._queries.build_inputs(row),
                        ),
                    )
                await retry_busy(
                    partial(
                        in_transaction,
                        self._db,
                        partial(self._delete_file, environment, row, file),
                    )
                )

    async def _delete_file(self, environment: Environment, row: StoredStack, name: str) -> None:
        if not await self._stacks.files.delete_file(row.id, name):
            raise ResourceNotFound("not found")
        await self._environments.record_compose_version(environment.id, self._runner.version_text())
        current_audit().detail["compose_version"] = self._runner.version_text()

    def _require_inputs(self, row: StoredStack, expected: str | None) -> None:
        if expected is not None and (row.inputs_digest or "") != expected:
            raise ChangedSinceReview

    async def _require_writable_project(
        self, actor: Principal, row: StoredStack, client: EngineClient
    ) -> None:
        """Refuse a standard user unless every project container is theirs to change."""
        if actor.is_administrator:
            return
        key = canonical_stack_key(row.name)
        containers = [c for c in await read_project_containers(client) if in_project(c.labels, key)]
        await self._runner.require_writable_members(actor, row.environment_id, containers)

    async def _store_files(self, stack_id: int, files: Sequence[CompanionFile]) -> None:
        for item in files:
            await self._put_file(stack_id, item.name, item.content, item.secret)

    async def _put_file(
        self, stack_id: int, name: str, content: str | None, secret: bool
    ) -> StoredFile:
        try:
            return await self._stacks.files.put_file(stack_id, name, content=content, secret=secret)
        except StackFileError as error:
            raise stored_file_error(error) from None
        except DuplicateError:
            raise StackRejected({"files": "A file could not be stored."}, "invalid") from None

    @audited(
        action="stack_deployed",
        target_type="stack",
        target_id=_stack_name_of,
        environment_id=environment_argument,
    )
    async def deploy(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        *,
        choices: Mapping[str, CredentialChoice] | None = None,
        progress: ProgressSink | None = None,
        check_collisions: bool = False,
        rebuild: bool = False,
        allow_builds: bool = True,
    ) -> StackOutcome:
        """Run the stack's first deployment: validate again, pull, build, `up`, and record it."""
        return await self._runner.run_up(
            actor,
            environment_id,
            name,
            operation="deploy",
            action="stack_deployed",
            choices=choices or {},
            pull=False,
            remove_orphans=False,
            expected_digest=None,
            expected_inputs_digest=None,
            expected_containers=None,
            compose_text=None,
            variables=None,
            progress=progress,
            check_collisions=check_collisions,
            rebuild=rebuild,
            allow_builds=allow_builds,
        )

    @audited(
        action="stack_redeployed",
        target_type="stack",
        target_id=_stack_name_of,
        environment_id=environment_argument,
    )
    async def redeploy(
        self,
        actor: Acting,
        environment_id: int,
        name: str,
        *,
        expected_file_digest: str | None = None,
        expected_inputs_digest: str | None = None,
        expected_containers: str | None = None,
        pull: bool = False,
        remove_orphans: bool = False,
        choices: Mapping[str, CredentialChoice] | None = None,
        compose_text: str | None = None,
        variables: Sequence[VariableInput] | None = None,
        progress: ProgressSink | None = None,
        note: Mapping[str, DetailValue] | None = None,
        check_collisions: bool = False,
        rebuild: bool = False,
        expected_builds: str | None = None,
        allow_builds: bool = True,
    ) -> StackOutcome:
        """Redeploy a stack the caller reviewed.

        `expected_file_digest`, `expected_inputs_digest` and `expected_containers` bind the call to
        what a confirmation showed; a difference is ChangedSinceReview and nothing runs. An
        inputs digest that was null when shown is the empty text. A file or variable set given
        here is used for the run and stored only if the run succeeds.

        An automation run redeploys the stored file and variables as deployed and takes none of
        `pull`, `remove_orphans`, `compose_text`, `variables`, `choices`, `check_collisions` or
        the `expected_*` arguments (ValueError). It is refused unless every container of the
        stack is running. It never builds: a service that builds is reused from its image or the
        redeploy is refused (`built_image_missing`).

        `rebuild` builds every service that builds even when its image stands; `expected_builds`
        is the digest of the builds a confirmation named, and without `allow_builds` a stack with
        a service that builds is refused (`build_not_available`).
        """
        if isinstance(actor, AutomationAuthority) and (
            rebuild
            or expected_builds is not None
            or pull
            or remove_orphans
            or compose_text is not None
            or variables is not None
            or choices
            or check_collisions
            or expected_file_digest is not None
            or expected_inputs_digest is not None
            or expected_containers is not None
        ):
            raise ValueError(
                "an automated redeploy takes no pull, review, file, variables, orphans"
            )
        return await self._runner.run_up(
            actor,
            environment_id,
            name,
            operation="redeploy",
            action="stack_redeployed",
            choices=choices or {},
            pull=pull,
            remove_orphans=remove_orphans,
            expected_digest=expected_file_digest,
            expected_inputs_digest=expected_inputs_digest,
            expected_containers=expected_containers,
            compose_text=compose_text,
            variables=variables,
            progress=progress,
            note=note,
            check_collisions=check_collisions,
            rebuild=rebuild,
            expected_builds=expected_builds,
            allow_builds=allow_builds,
        )

    @audited(
        action="stack_started",
        target_type="stack",
        target_id=_stack_name_of,
        environment_id=environment_argument,
    )
    async def start(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        *,
        progress: ProgressSink | None = None,
    ) -> StackOutcome:
        """Start the stack's existing containers; NotDeployed when it has none."""
        return await self._runner.run_lifecycle(
            actor, environment_id, name, "start", "stack_started", progress
        )

    @audited(
        action="stack_stopped",
        target_type="stack",
        target_id=_stack_name_of,
        environment_id=environment_argument,
    )
    async def stop(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        *,
        progress: ProgressSink | None = None,
    ) -> StackOutcome:
        """Stop the stack's running containers; NotDeployed when it has none."""
        return await self._runner.run_lifecycle(
            actor, environment_id, name, "stop", "stack_stopped", progress
        )

    @audited(
        action="stack_restarted",
        target_type="stack",
        target_id=_stack_name_of,
        environment_id=environment_argument,
    )
    async def restart(
        self,
        actor: Acting,
        environment_id: int,
        name: str,
        *,
        also_start: Sequence[str] = (),
        expected_containers: str | None = None,
        progress: ProgressSink | None = None,
    ) -> StackOutcome:
        """Stop the running services, then start them, so Compose orders both by dependency.

        NotRunning when no container of the stack runs and, for an automation run, `also_start`
        names no service of it. A failed run leaves the stack partly stopped; the event names the
        phase that failed and, in `unstarted`, the services a later call passes as `also_start`.

        A user restarts what runs: `also_start` is a ValueError, and `expected_containers` binds
        the call to the containers a confirmation showed (ChangedSinceReview). An automation run
        takes neither it nor `progress` (ValueError).
        """
        if isinstance(actor, AutomationAuthority):
            if expected_containers is not None or progress is not None:
                raise ValueError("an automated restart takes no review and no progress sink")
            return await self._restart_automated(actor, environment_id, name, also_start)
        if also_start:
            raise ValueError("a restart by a user restarts the running services only")
        return await self._restart_by_user(
            actor, environment_id, name, expected_containers, progress
        )

    async def _restart_automated(
        self,
        actor: AutomationAuthority,
        environment_id: int,
        name: str,
        also_start: Sequence[str],
    ) -> StackOutcome:
        audit = current_audit()
        audit.detail["method"] = RESTART_METHOD
        with named_reasons():
            row = await self._runner.automation_stack(actor, environment_id, name)
            audit.target_id = row.name
            audit.detail["stack_id"] = row.id
            self._runner.refuse_self(environment_id, row.name)
            environment = await self._hosts.environment(environment_id)
            cli = await self._queries.ready()
            client = await audited_client(self._hosts, environment_id)
            target = await build_target(environment, self._tls)
            async with AsyncExitStack() as held:
                try:
                    await held.enter_async_context(
                        actor.hold(stack_key(environment_id, row.name), "restart")
                    )
                except (ObjectBusy, RunSkipped):
                    audit.refuse()
                    raise
                row = await self._runner.reread(environment_id, row.name)
                values = await self._queries.variable_set(row, None)
                key = canonical_stack_key(row.name)
                project_containers = [
                    c for c in await read_project_containers(client) if in_project(c.labels, key)
                ]
                if not project_containers:
                    raise NotDeployed
                self._runner.refuse_self(environment_id, row.name, project_containers)
                await self._runner.authorise_members(actor, environment_id, project_containers)
                if not also_start and not any(c.state == "running" for c in project_containers):
                    raise NotRunning
                try:
                    self._runner.require_idle(environment_id, project_containers)
                except ObjectBusy:
                    audit.refuse()
                    raise
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
                    operation="restart",
                    record="stop",
                    action="stack_restarted",
                    actor=actor,
                    environment_id=environment_id,
                    stack=row,
                    client=client,
                    cli=cli,
                    project=project,
                    target=target,
                    version=self._runner.version_text(),
                    values=values.values,
                    secrets=values.secrets,
                    text=row.compose_file,
                    containers=len(project_containers),
                    start_extra=tuple(also_start),
                )
                audit.detail["compose_version"] = plan.version
                return await self._runner.execute(plan, held)

    async def _restart_by_user(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        expected_containers: str | None,
        progress: ProgressSink | None,
    ) -> StackOutcome:
        audit = current_audit()
        audit.detail["method"] = RESTART_METHOD
        with named_reasons():
            row = await self._queries.require_stack(actor, environment_id, name, "write")
            audit.target_id = row.name
            audit.detail["stack_id"] = row.id
            self._runner.refuse_self(environment_id, row.name)
            environment = await self._hosts.environment(environment_id)
            cli = await self._queries.ready()
            client = await audited_client(self._hosts, environment_id)
            target = await build_target(environment, self._tls)
            holder = LockHolder(UserActor.of(actor), actor.username, "restart")
            async with AsyncExitStack() as held:
                await held.enter_async_context(
                    hold_all(self._locks, [stack_key(environment_id, row.name)], holder)
                )
                row = await self._runner.reread(environment_id, row.name)
                values = await self._queries.variable_set(row, None)
                key = canonical_stack_key(row.name)
                project_containers = [
                    c for c in await read_project_containers(client) if in_project(c.labels, key)
                ]
                if not project_containers:
                    raise NotDeployed
                self._runner.refuse_self(environment_id, row.name, project_containers)
                await self._runner.require_writable_members(
                    actor, environment_id, project_containers
                )
                if not any(c.state == "running" for c in project_containers):
                    raise NotRunning
                self._runner.require_idle(environment_id, project_containers)
                if (
                    expected_containers is not None
                    and containers_digest([c.id for c in project_containers]) != expected_containers
                ):
                    raise ChangedSinceReview
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
                    operation="restart",
                    record="stop",
                    action="stack_restarted",
                    actor=actor,
                    environment_id=environment_id,
                    stack=row,
                    client=client,
                    cli=cli,
                    project=project,
                    target=target,
                    version=self._runner.version_text(),
                    values=values.values,
                    secrets=values.secrets,
                    text=row.compose_file,
                    containers=len(project_containers),
                    sink=progress,
                )
                audit.detail["compose_version"] = plan.version
                return await self._runner.execute(plan, held)

    @audited(
        action="stack_removed",
        target_type="stack",
        target_id=_stack_name_of,
        environment_id=environment_argument,
    )
    async def remove(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        *,
        remove_volumes: bool = False,
        expected_volumes: str | None = None,
        remove_orphans: bool = False,
        expected_stack_id: int,
        expected_containers: str,
        expected_networks: str,
        progress: ProgressSink | None = None,
    ) -> StackOutcome:
        """Take the stack's containers and networks down, then forget the stack.

        `expected_stack_id`, `expected_containers` and `expected_networks` bind the call to
        what a confirmation named; a value that differs is ChangedSinceReview and nothing
        runs. With `remove_volumes` the volumes the confirmation listed are deleted through the
        Engine, each only while it still carries this project's label and no control of its
        own; `expected_volumes` must match the digest that confirmation showed. Without it, every
        surviving project volume that has no control of its own gets a copy of the stack's
        control.
        """
        audit = current_audit()
        with named_reasons():
            row = await self._queries.require_stack(actor, environment_id, name, "write")
            audit.target_id = row.name
            audit.detail.update(
                stack_id=row.id, volumes=remove_volumes, remove_orphans=remove_orphans
            )
            self._runner.refuse_self(environment_id, row.name)
            environment = await self._hosts.environment(environment_id)
            cli = await self._queries.ready()
            client = await audited_client(self._hosts, environment_id)
            target = await build_target(environment, self._tls)
            holder = LockHolder(UserActor.of(actor), actor.username, "remove")
            async with AsyncExitStack() as held:
                await held.enter_async_context(
                    hold_all(self._locks, [stack_key(environment_id, row.name)], holder)
                )
                row = await self._runner.reread(environment_id, row.name)
                if row.id != expected_stack_id:
                    raise ChangedSinceReview
                values = await self._queries.variable_set(row, None)
                key = canonical_stack_key(row.name)
                project_containers = [
                    c for c in await read_project_containers(client) if in_project(c.labels, key)
                ]
                self._runner.refuse_self(environment_id, row.name, project_containers)
                if containers_digest([c.id for c in project_containers]) != expected_containers:
                    raise ChangedSinceReview
                current = [
                    n.id for n in await labelled_networks(client) if in_project(n.labels, key)
                ]
                if networks_digest(current) != expected_networks:
                    raise ChangedSinceReview
                await self._runner.require_writable_members(
                    actor, environment_id, project_containers
                )
                self._runner.require_idle(environment_id, project_containers)
                volumes: tuple[str, ...] = ()
                if remove_volumes:
                    volumes = await self._lock_volumes(
                        actor, row, client, expected_volumes, held, holder
                    )
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
                    operation="remove",
                    record="remove",
                    action="stack_removed",
                    actor=actor,
                    environment_id=environment_id,
                    stack=row,
                    client=client,
                    cli=cli,
                    project=project,
                    target=target,
                    version=self._runner.version_text(),
                    values=values.values,
                    secrets=values.secrets,
                    text=row.compose_file,
                    remove_orphans=remove_orphans,
                    containers=len(project_containers),
                    volumes=volumes,
                    sink=progress,
                )
                return await self._runner.execute(plan, held)

    @audited(
        action="stack_forgotten",
        target_type="stack",
        target_id=_stack_name_of,
        environment_id=environment_argument,
    )
    async def forget(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        *,
        expected_stack_id: int | None = None,
    ) -> None:
        """Delete the stack's row and control and leave the host untouched; administrators only.

        A given `expected_stack_id` that is no longer the stack's is ChangedSinceReview.
        """
        audit = current_audit()
        with named_reasons():
            row = await self._queries.require_stack(actor, environment_id, name, "write")
            require_administrator(actor)
            audit.target_id = row.name
            holder = LockHolder(UserActor.of(actor), actor.username, "forget")
            async with hold_all(self._locks, [stack_key(environment_id, row.name)], holder):
                row = await self._runner.reread(environment_id, row.name)
                if expected_stack_id is not None and row.id != expected_stack_id:
                    raise ChangedSinceReview
                keeps = await self._namespace_survives(environment_id, row.name)
                await retry_busy(
                    partial(in_transaction, self._db, partial(self._store_forget, row, keeps))
                )
            await self._runtime.files.discard(row.project_directory)

    async def _namespace_survives(self, environment_id: int, name: str) -> bool:
        """Report whether a Swarm namespace may fold to the name; only a Swarm host is read."""
        environment = await self._hosts.environment(environment_id)
        if not swarm_namespaces_apply(environment):
            return False
        try:
            client = await self._hosts.client(environment_id)
        except EnvironmentUnavailable:
            return True
        return await self._queries.namespace_taken(environment_id, name, client)

    async def _store_forget(self, row: StoredStack, keeps_control: bool) -> None:
        audit = current_audit()
        await self._stacks.delete(row.id)
        released = False
        if not keeps_control:
            released = await self._controls.release(
                ResourceKey(row.environment_id, "stack", row.name)
            )
        audit.detail.update(stack_id=row.id, control_released=released)
        if keeps_control:
            audit.detail["control_kept"] = True

    @audited(
        action="stack_adopted",
        target_type="stack",
        target_id=_stack_name_of,
        environment_id=environment_argument,
    )
    async def adopt(
        self,
        actor: Principal,
        environment_id: int,
        *,
        name: str,
        compose_text: str = "",
        variables: Sequence[VariableInput] | None = None,
        deploy_requested: bool = False,
        choices: Mapping[str, CredentialChoice] | None = None,
        resolve_pulls: bool = False,
        files: Sequence[CompanionFile] = (),
        prepared: HostReadResult | None = None,
        as_is: bool = False,
        onboarding: Ownership | None = None,
    ) -> StackAdopted:
        """Take over a Compose project found on the host; administrators only.

        The file, variables and companion files the administrator gives, or a `prepared` result
        read from the host in their place, are validated and stored as the stack's; nothing is
        changed on the host. An existing control over the name is kept. With `resolve_pulls` the
        credentials the redeployment will pull with are resolved too. With `as_is`, which needs
        `prepared` and no deploy, the stack reads unchanged from the moment it is stored;
        ChangedSinceReview, nothing stored, when the project's containers are no longer the ones
        `prepared` was read from. With `onboarding` the control is claimed for that owner and
        team instead of the caller, and none is claimed when it names neither.
        """
        if as_is and (prepared is None or deploy_requested):
            raise ValueError("adopting as is takes a prepared result and no deploy")
        if prepared is not None:
            compose_text, variables, files = adoption_inputs(prepared)
        audit = current_audit()
        with named_reasons():
            await self._authoriser.require_environment(actor, environment_id)
            require_administrator(actor)
            environment = await self._hosts.environment(environment_id)
            checked = checked_name(name)
            audit.target_id = checked
            cli = await self._queries.ready()
            values = await self._queries.variable_set(None, variables)
            client = await audited_client(self._hosts, environment_id)
            target = await build_target(environment, self._tls)
            holder = LockHolder(UserActor.of(actor), actor.username, "adopt")
            directories = self._runtime.files
            companions = tuple(files)
            key = canonical_stack_key(checked)
            async with hold_all(self._locks, [stack_key(environment_id, checked)], holder):
                await self._queries.require_adoptable(environment_id, checked, client)
                run = StackRun()
                project_containers = [
                    c for c in await read_project_containers(client) if in_project(c.labels, key)
                ]
                if prepared is not None and not prepared.matches_host(
                    compose_host_state(project_containers)
                ):
                    raise ChangedSinceReview
                run.containers = tuple(project_containers)
                run.networks = tuple(
                    n for n in await labelled_networks(client) if in_project(n.labels, key)
                )
                run.volumes = tuple(
                    v for v in await labelled_volumes(client) if in_project(v.labels, key)
                )
                token = directories.new_token()
                project = directories.project(checked, token)
                stored = False
                try:
                    async with directories.materialise(
                        project, compose_text, companions
                    ) as written:
                        validated = await self._validator.validate(
                            actor,
                            validation_request(
                                environment=environment,
                                client=client,
                                cli=cli,
                                target=target,
                                project=written.project,
                                text=compose_text,
                                values=values,
                                deploy=resolve_pulls,
                                choices=choices,
                                files=companions,
                            ),
                        )
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
                                    validated,
                                    run,
                                    deploy_requested,
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
        validated: Validated,
        run: StackRun,
        deploy_requested: bool,
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
                kind="compose",
                origin="adopted",
                project_directory=token,
                compose_file=text,
                compose_file_digest=validated.compose_digest,
                created_by_user_id=actor.user_id,
            )
        except DuplicateError:
            raise StackNameInUse from None
        if values.writes:
            try:
                await self._stacks.replace_variables(stack_id, values.writes)
            except StackVariableError:
                raise StackRejected(
                    {"variables": "A variable could not be stored."}, "invalid"
                ) from None
        await self._store_files(stack_id, companions)
        states = [c.state for c in run.containers]
        running = sum(1 for state in states if state == "running")
        status: StackStatus = (
            "inactive"
            if not states or not running
            else ("active" if running == len(states) else "partial")
        )
        await self._stacks.record_operation(
            stack_id, operation="adopt", outcome="success", status=status
        )
        if as_is:
            await self._stacks.record_running_state(
                stack_id,
                file_digest=validated.compose_digest,
                resolved_digest=validated.resolved_digest,
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
        await self._controls.refresh_sub_resources(key, member_refs(environment.id, run))
        await self._environments.record_compose_version(environment.id, self._runner.version_text())
        audit.detail.update(
            stack_id=stack_id,
            compose_digest=validated.compose_digest,
            services=len(validated.services),
            containers=len(run.containers),
            files=len(companions),
            secret_files=secret_file_names(companions),
            control_created=created,
            deploy_requested=deploy_requested,
            restricted=",".join(validated.restricted),
            compose_version=self._runner.version_text(),
        )
        if prepared is not None:
            audit.detail["file_source"] = prepared.file_source
        if as_is:
            audit.detail["as_is"] = True
        if onboarding is not None:
            audit.detail["onboarding"] = True
        if await self._dismissals.drop_dismissal(environment.id, "compose", name):
            audit.detail["dismissal_cleared"] = True
        return StackAdopted(environment.id, name, stack_id, len(run.containers), created)

    @audited(
        action=_refusal_action,
        target_type="stack",
        target_id=_stack_name_of,
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

        The caller needs write on the stack; without it the access error is raised and no row
        is written.
        """
        audit = current_audit()
        with named_reasons():
            row = await self._queries.require_stack(actor, environment_id, name, "write")
            audit.target_id = row.name
            audit.detail["stack_id"] = row.id
            if isinstance(error, ObjectBusy):
                audit.detail["holder_action"] = error.holder.action
            raise error

    async def _lock_volumes(
        self,
        actor: Principal,
        row: StoredStack,
        client: EngineClient,
        expected: str | None,
        held: AsyncExitStack,
        holder: LockHolder,
    ) -> tuple[str, ...]:
        plan = await self._queries.plan_volumes(actor, row, client=client)
        if expected is None or plan.digest != expected:
            raise ChangedSinceReview
        if plan.cannot_change:
            raise StackRejected(
                {"volumes": f"You cannot change {plan.cannot_change} of these volumes."},
                "restricted",
            )
        await held.enter_async_context(
            hold_all(
                self._locks,
                [ObjectKey(row.environment_id, "volume", name) for name in plan.names],
                holder,
            )
        )
        return plan.names
