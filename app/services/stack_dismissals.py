"""The operations that dismiss an unmanaged stack entry and show it again."""

from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from typing import Final, Protocol

from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.stack import StackKind, StackRepository
from app.db.repositories.stack_dismissal import StackDismissalRepository
from app.services.access import canonical_stack_key
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.guards import require_administrator
from app.services.object_inspector import EnvironmentUnavailable, HostClients, ObjectTooLarge
from app.services.stack_deploy import ChangedSinceReview, StackError, StackRejected
from app.services.stack_dismissal_views import DISMISSIBLE_NAME, HostMembers, members_digest
from app.services.swarm_gate import require_swarm

__all__ = [
    "MAX_DISMISSED_OBJECTS",
    "MemberReader",
    "NotLeftoversOnly",
    "StackDismissalService",
    "TooManyObjects",
]

MAX_DISMISSED_OBJECTS: Final = 500


class MemberReader(Protocol):
    """Reads what one project or namespace holds on the host now."""

    def __call__(self, environment_id: int, key: str) -> Awaitable[HostMembers]:
        """Return the members of the stack whose folded name is `key`."""
        ...


class NotLeftoversOnly(StackError):
    """The entry still has a container or a service."""

    reason = "not_leftovers_only"

    def __init__(self) -> None:
        super().__init__("This entry still has containers or services, so it cannot be dismissed.")


class TooManyObjects(StackRejected):
    """The entry holds more objects than one dismissal can name."""

    def __init__(self) -> None:
        super().__init__(
            {"stack": f"This entry holds more than {MAX_DISMISSED_OBJECTS} objects."},
            "too_many_objects",
        )


def _target(arguments: Arguments) -> str | None:
    name = str(arguments["name"])
    return canonical_stack_key(name) if DISMISSIBLE_NAME.fullmatch(name) else None


@contextmanager
def _named_reasons() -> Iterator[None]:
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


class StackDismissalService:
    """Dismisses and restores unmanaged stack entries that hold only leftovers."""

    def __init__(
        self,
        *,
        db: Session,
        authoriser: Authoriser,
        hosts: HostClients,
        stacks: StackRepository,
        dismissals: StackDismissalRepository,
        activity: ActivityRecorder,
        compose: MemberReader,
        swarm: MemberReader,
    ) -> None:
        self._db = db
        self._authoriser = authoriser
        self._hosts = hosts
        self._stacks = stacks
        self._dismissals = dismissals
        self._activity = activity
        self._readers: dict[StackKind, MemberReader] = {"compose": compose, "swarm": swarm}

    @audited(
        action="stack_dismissed",
        target_type="stack",
        target_id=_target,
        environment_id=environment_argument,
        announces=False,
    )
    async def dismiss(
        self,
        actor: Principal,
        environment_id: int,
        kind: StackKind,
        name: str,
        expected_objects: str,
    ) -> None:
        """Hide an unmanaged entry that holds only leftovers; administrators only.

        `expected_objects` is the digest of the objects the administrator reviewed. The entry is
        read from the host again, and the dismissal names the objects found then.
        """
        audit = current_audit()
        with _named_reasons():
            require_administrator(actor)
            await self._authoriser.require_environment(actor, environment_id)
            if DISMISSIBLE_NAME.fullmatch(name) is None:
                raise StackRejected({"name": "This name cannot be dismissed."}, "invalid")
            key = canonical_stack_key(name)
            await self._hosts.environment(environment_id)
            if kind == "swarm":
                await require_swarm(self._authoriser, self._hosts, actor, environment_id)
            members = await self._readers[kind](environment_id, key)
            managed = await self._stacks.get_folded(environment_id, key)
            if (managed is not None and managed.kind == kind) or not members.present:
                raise ResourceNotFound("not found")
            if members.workloads > 0:
                raise NotLeftoversOnly
            if len(members.tokens) > MAX_DISMISSED_OBJECTS:
                raise TooManyObjects
            if members_digest(members.tokens) != expected_objects:
                raise ChangedSinceReview
            async with self._db.transaction():
                managed = await self._stacks.get_folded(environment_id, key)
                if managed is not None and managed.kind == kind:
                    raise ResourceNotFound("not found")
                await self._dismissals.put_dismissal(
                    environment_id, kind, key, members.tokens, actor.user_id
                )
                audit.detail.update(name=key, stack_kind=kind, objects=len(members.tokens))

    @audited(
        action="stack_dismissal_cleared",
        target_type="stack",
        target_id=_target,
        environment_id=environment_argument,
        announces=False,
    )
    async def clear(
        self, actor: Principal, environment_id: int, kind: StackKind, name: str
    ) -> None:
        """Show a dismissed entry again; administrators only. Nothing is recorded without one."""
        audit = current_audit()
        require_administrator(actor)
        await self._authoriser.require_environment(actor, environment_id)
        await self._hosts.environment(environment_id)
        async with self._db.transaction():
            dropped = DISMISSIBLE_NAME.fullmatch(name) is not None and (
                await self._dismissals.drop_dismissal(environment_id, kind, name)
            )
            if dropped:
                audit.detail.update(name=canonical_stack_key(name), stack_kind=kind)
            else:
                audit.unchanged()
