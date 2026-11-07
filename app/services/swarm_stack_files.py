"""The Swarm stack file operations: add, replace and remove a companion file without deploying."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import partial

from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.db.engine import Session, retry_busy
from app.db.repositories.environment import Environment, EnvironmentRepository
from app.db.repositories.errors import DuplicateError
from app.db.repositories.stack import StackRepository, StackVariableError, StoredStack
from app.db.repositories.stack_file import StackFileError, StoredFile
from app.services.access import canonical_stack_key
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.compose_setup import ComposeRuntime
from app.services.host_objects import hold_all, in_transaction
from app.services.object_inspector import (
    EnvironmentUnavailable,
    HostClients,
    ObjectTooLarge,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.stack_deploy import ChangedSinceReview, StackError, StackRejected
from app.services.stack_document import FILE_NAME, CompanionFile
from app.services.stack_file_views import (
    candidate_with,
    candidate_without,
    checked_content,
    checked_file_name,
    file_in_use,
    file_kind,
    file_view,
    stored_file_error,
    stored_placeholders,
    uses_of,
)
from app.services.stack_queries import StackQueryService
from app.services.stack_views import FileView, VariableSet
from app.services.swarm_stack_plan import SwarmStackPlanner
from app.services.swarm_stacks import NAMESPACE_ROUTE

__all__ = ["SwarmStackFiles", "named_reasons", "stack_key", "target_name"]


def target_name(arguments: Arguments) -> str | None:
    text = str(arguments.get("name") or arguments.get("namespace") or "").strip()
    return text if NAMESPACE_ROUTE.fullmatch(text) else None


def stack_key(environment_id: int, name: str) -> ObjectKey:
    return ObjectKey(environment_id, "stack", canonical_stack_key(name))


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
    except ObjectTooLarge:
        current_audit().reason = "too_large"
        raise
    except ObjectBusy as error:
        audit = current_audit()
        audit.reason = "object_busy"
        audit.detail["holder_action"] = error.holder.action
        raise


class SwarmStackFiles:
    """The audited file operations of a Swarm stack, and the row re-read they share."""

    _activity: ActivityRecorder
    _db: Session
    _hosts: HostClients
    _planner: SwarmStackPlanner
    _queries: StackQueryService
    _stacks: StackRepository
    _environments: EnvironmentRepository
    _locks: ObjectLockRegistry
    _runtime: ComposeRuntime

    def _version_text(self) -> str:
        return self._runtime.version_text or ""

    async def _reread(self, environment_id: int, name: str) -> StoredStack:
        row = await self._stacks.get(environment_id, name)
        if row is None or row.kind != "swarm":
            raise ResourceNotFound("not found")
        return row

    @audited(
        action="stack_updated",
        target_type="stack",
        target_id=target_name,
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
        caller needs write on the stack and, as a standard user, on every member.
        `expected_inputs_digest` is the stack's inputs digest when the change began, the empty
        text for none; ChangedSinceReview when it no longer is.
        """
        audit = current_audit()
        with named_reasons():
            row = await self._planner.require_stack(actor, environment_id, name, "write")
            audit.target_id = row.name
            audit.detail.update(kind="swarm", stack_id=row.id)
            checked = checked_file_name(file)
            text = checked_content(content)
            audit.detail.update(file_written=checked, file_kind=file_kind(secret))
            environment = await self._hosts.environment(environment_id)
            await self._queries.ready()
            holder = LockHolder(UserActor.of(actor), actor.username, "update")
            async with hold_all(self._locks, [stack_key(environment_id, row.name)], holder):
                row = await self._reread(environment_id, row.name)
                self._require_inputs(row, expected_inputs_digest)
                candidate = candidate_with(
                    await self._queries.stored_files(row), checked, text, secret
                )
                await self._planner.validate(
                    actor,
                    environment_id,
                    name=row.name,
                    compose_text=row.compose_file,
                    variables=None,
                    existing=True,
                    files=candidate,
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
        await self._environments.record_compose_version(environment.id, self._version_text())
        current_audit().detail["compose_version"] = self._version_text()
        return stored

    @audited(
        action="stack_updated",
        target_type="stack",
        target_id=target_name,
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
            row = await self._planner.require_stack(actor, environment_id, name, "write")
            audit.target_id = row.name
            audit.detail.update(kind="swarm", stack_id=row.id)
            if FILE_NAME.fullmatch(file) is None:
                raise ResourceNotFound("not found")
            environment = await self._hosts.environment(environment_id)
            await self._queries.ready()
            holder = LockHolder(UserActor.of(actor), actor.username, "update")
            async with hold_all(self._locks, [stack_key(environment_id, row.name)], holder):
                row = await self._reread(environment_id, row.name)
                self._require_inputs(row, expected_inputs_digest)
                items = await self._stacks.files.files(row.id)
                found = next((item for item in items if item.name == file), None)
                if found is None:
                    raise ResourceNotFound("not found")
                audit.detail.update(file_removed=file, file_kind=file_kind(found.is_secret))
                uses = await uses_of(row.compose_file, stored_placeholders(items), row.kind)
                if uses.get(file):
                    raise file_in_use(file, uses[file])
                await self._planner.validate(
                    actor,
                    environment_id,
                    name=row.name,
                    compose_text=row.compose_file,
                    variables=None,
                    existing=True,
                    files=candidate_without(await self._queries.stored_files(row), file),
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
        await self._environments.record_compose_version(environment.id, self._version_text())
        current_audit().detail["compose_version"] = self._version_text()

    @staticmethod
    def _require_inputs(row: StoredStack, expected: str | None) -> None:
        if expected is not None and (row.inputs_digest or "") != expected:
            raise ChangedSinceReview

    async def _write_variables(self, stack_id: int, values: VariableSet) -> None:
        if not values.writes:
            return
        try:
            await self._stacks.replace_variables(stack_id, values.writes)
        except StackVariableError:
            raise StackRejected(
                {"variables": "A variable could not be stored."}, "invalid"
            ) from None

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
