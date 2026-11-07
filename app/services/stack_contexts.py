"""Build contexts of a Compose stack: add, replace and remove for administrators, and extraction."""

from functools import partial
from pathlib import Path

from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.db.engine import Session, retry_busy
from app.db.repositories.stack import StackRepository, StoredStack
from app.db.repositories.stack_build_context import (
    ContextWrite,
    StackContextLimitError,
    StoredContext,
)
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.build_context_archive import (
    ContextRefused,
    InspectedContext,
    extract_context_archive,
    inspect_context,
    run_limited,
)
from app.services.guards import require_administrator
from app.services.host_objects import hold_all, in_transaction
from app.services.object_inspector import EngineObjectInspector
from app.services.object_locks import LockHolder, ObjectLockRegistry
from app.services.stack_context_views import (
    ContextRemoveConfirmation,
    checked_context_name,
    context_in_use,
    context_references,
    context_view,
    context_views,
)
from app.services.stack_deploy import ChangedSinceReview, StackRejected
from app.services.stack_document import FILE_NAME, STACK_NAME
from app.services.stack_queries import StackQueryService, stack_key
from app.services.stack_views import ContextView

__all__ = ["StackContextService", "extract_stored_context"]

_ARCHIVE_FIELD = "archive"
_LIMIT_MESSAGE = "A stack holds at most 32 build contexts and 64 MiB of archives in total."


def _stack_name_of(arguments: Arguments) -> str | None:
    name = str(arguments["name"]).strip()
    return name if STACK_NAME.fullmatch(name) else None


def _refusal(refused: ContextRefused) -> StackRejected:
    message = str(refused)
    if refused.member:
        message = f"{message} Entry: {refused.member}"
    return StackRejected({_ARCHIVE_FIELD: message}, refused.reason)


async def extract_stored_context(
    repository: StackRepository, stack_id: int, name: str, destination: Path
) -> None:
    """Extract a stored context into an empty directory, re-validated and checked to its digest.

    ResourceNotFound when the stack has no such context; StackRejected when the stored archive no
    longer passes the checks.
    """
    archive = await repository.contexts.archive(stack_id, name)
    if archive is None:
        raise ResourceNotFound("not found")
    try:
        await run_limited(
            extract_context_archive, archive.content, destination, digest=archive.content_digest
        )
    except ContextRefused as refused:
        raise _refusal(refused) from None


class StackContextService:
    """The audited context operations; each is one audit scope with at most one commit."""

    def __init__(
        self,
        *,
        db: Session,
        inspector: EngineObjectInspector,
        queries: StackQueryService,
        stacks: StackRepository,
        locks: ObjectLockRegistry,
        activity: ActivityRecorder,
    ) -> None:
        self._db = db
        self._hosts = inspector.hosts
        self._queries = queries
        self._stacks = stacks
        self._locks = locks
        self._activity = activity

    async def views(
        self, actor: Principal, environment_id: int, name: str
    ) -> tuple[ContextView, ...]:
        """List a stack's contexts for an administrator."""
        require_administrator(actor)
        stack = await self._queries.require_stack(actor, environment_id, name, "write")
        return await context_views(self._stacks.contexts, stack)

    async def remove_confirmation(
        self, actor: Principal, environment_id: int, name: str, context: str
    ) -> ContextRemoveConfirmation:
        """Name the context a remove deletes and where it is still used; administrators only."""
        require_administrator(actor)
        stack = await self._queries.require_stack(actor, environment_id, name, "write")
        environment = await self._hosts.environment(environment_id)
        view = await self._view_of(stack, context)
        return ContextRemoveConfirmation(
            environment_id=environment_id,
            environment_name=environment.name,
            stack=stack.name,
            context=view,
            expected_inputs_digest=stack.inputs_digest or "",
            in_use=bool(view.uses),
        )

    @audited(
        action="stack_updated",
        target_type="stack",
        target_id=_stack_name_of,
        environment_id=environment_argument,
    )
    async def write(
        self, actor: Principal, environment_id: int, name: str, context: str, *, archive: bytes
    ) -> ContextView:
        """Add a context or replace the one of that name; nothing is deployed.

        StackRejected keyed `name` or `archive` for a name or archive the rules refuse, `too_large`
        past the per-stack limits; ObjectBusy while an operation holds the stack.
        """
        audit = current_audit()
        require_administrator(actor)
        row = await self._queries.require_stack(actor, environment_id, name, "write")
        audit.target_id = row.name
        audit.detail["stack_id"] = row.id
        checked = checked_context_name(context)
        try:
            inspected = await run_limited(inspect_context, archive)
        except ContextRefused as refused:
            raise _refusal(refused) from None
        audit.detail.update(
            context_written=checked,
            context_size=len(archive),
            context_digest=inspected.content_digest,
        )
        holder = LockHolder(UserActor.of(actor), actor.username, "update")
        async with hold_all(self._locks, [stack_key(environment_id, row.name)], holder):
            row = await self._reread(environment_id, row.name)
            stored = await retry_busy(
                partial(in_transaction, self._db, partial(self._store, row.id, checked, inspected))
            )
        uses = (await context_references(row.compose_file)).get(stored.name.casefold(), ())
        return context_view(stored, uses)

    @audited(
        action="stack_updated",
        target_type="stack",
        target_id=_stack_name_of,
        environment_id=environment_argument,
    )
    async def remove(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        context: str,
        *,
        expected_inputs_digest: str | None = None,
    ) -> None:
        """Delete a context; nothing is deployed.

        ResourceNotFound when the stack has no such context; StackRejected (`context_in_use`) while
        the Compose file builds from it; ChangedSinceReview when the stack's inputs digest is no
        longer `expected_inputs_digest`.
        """
        audit = current_audit()
        require_administrator(actor)
        row = await self._queries.require_stack(actor, environment_id, name, "write")
        audit.target_id = row.name
        audit.detail["stack_id"] = row.id
        if FILE_NAME.fullmatch(context) is None:
            raise ResourceNotFound("not found")
        holder = LockHolder(UserActor.of(actor), actor.username, "update")
        async with hold_all(self._locks, [stack_key(environment_id, row.name)], holder):
            row = await self._reread(environment_id, row.name)
            if expected_inputs_digest is not None and (row.inputs_digest or "") != (
                expected_inputs_digest
            ):
                raise ChangedSinceReview
            found = await self._stacks.contexts.get(row.id, context)
            if found is None:
                raise ResourceNotFound("not found")
            audit.detail.update(
                context_removed=found.name,
                context_size=found.size_bytes,
                context_digest=found.content_digest,
            )
            paths = (await context_references(row.compose_file)).get(found.name.casefold(), ())
            if paths:
                raise context_in_use(found.name, paths)
            await retry_busy(
                partial(in_transaction, self._db, partial(self._delete, row.id, found.name))
            )

    async def _reread(self, environment_id: int, name: str) -> StoredStack:
        row = await self._stacks.get(environment_id, name)
        if row is None:
            raise ResourceNotFound("not found")
        return row

    async def _view_of(self, stack: StoredStack, context: str) -> ContextView:
        if FILE_NAME.fullmatch(context) is None:
            raise ResourceNotFound("not found")
        found = next(
            (v for v in await context_views(self._stacks.contexts, stack) if v.name == context),
            None,
        )
        if found is None:
            raise ResourceNotFound("not found")
        return found

    async def _store(self, stack_id: int, name: str, inspected: InspectedContext) -> StoredContext:
        write = ContextWrite(
            content=inspected.content,
            content_digest=inspected.content_digest,
            compressed=inspected.compressed,
            member_count=inspected.member_count,
            extracted_size=inspected.extracted_size,
        )
        try:
            stored = await self._stacks.contexts.put(stack_id, name, write)
        except StackContextLimitError:
            raise StackRejected({_ARCHIVE_FIELD: _LIMIT_MESSAGE}, "too_large") from None
        current_audit().detail["context_written"] = stored.name
        return stored

    async def _delete(self, stack_id: int, name: str) -> None:
        if not await self._stacks.contexts.delete(stack_id, name):
            raise ResourceNotFound("not found")
