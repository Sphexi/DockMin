"""The visibility predicate and the enforcement helpers every service reaches objects through."""

from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Protocol

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.auth.principal import Principal
from app.db.repositories.environment_access import (
    EnvironmentAccessRepository,
    EnvironmentGate,
)
from app.db.repositories.resource_control import ControlStanding, ResourceControlRepository
from app.db.repositories.visibility_filter import (
    SqlFragment,
    require_qualified_columns,
    visible_clause,
)
from app.logging import get_logger
from app.services.access import (
    INHERITING_TYPES,
    AccessLevel,
    Controllable,
    Decision,
    Reason,
    ResourceKey,
    ResourceType,
    canonical_stack_key,
    evaluate,
    is_canonical_id,
    satisfies,
    stack_of,
)
from app.services.access_policy import AccessPolicyService

logger = get_logger(__name__)

_ADMINISTRATOR = Decision(level="write", reasons=(Reason.ADMINISTRATOR,))

type _VisibilityKey = tuple[int, int, ResourceType]


class ObjectInspector(Protocol):
    """Reads an object's labels from Docker, which is the only source the model trusts."""

    async def labels_of(
        self, environment_id: int, resource_type: ResourceType, resource_id: str
    ) -> Mapping[str, str]:
        """Return the labels of one object; raise ResourceNotFound if Docker has no such object."""
        ...


class UnavailableInspector:
    """Stands in until an inspector is wired; any attempt to inspect fails the request."""

    async def labels_of(
        self, environment_id: int, resource_type: ResourceType, resource_id: str
    ) -> Mapping[str, str]:
        """Refuse loudly, so a missing wiring is never read as an object without labels."""
        raise RuntimeError("no object inspector is configured")


@dataclass(frozen=True, slots=True)
class Visibility:
    """What one caller may reach among one type of object in one environment.

    Built per request. The membership and grants it holds are a read taken when it was built, so
    it must not be kept across requests. covered is the set of own-controls it was narrowed to,
    or None when it holds every control of the type; deciding an object outside it raises, since
    an object whose control was never loaded would read as uncontrolled.
    """

    user_id: int
    environment_id: int
    resource_type: ResourceType
    allow_all: bool
    default: AccessLevel | None
    own: Mapping[str, ControlStanding]
    stacks: Mapping[str, ControlStanding]
    covered: frozenset[str] | None = None
    environment_denied: bool = False
    ceiling: AccessLevel = "write"

    def decide(self, resource_id: str, labels: Mapping[str, str]) -> Decision:
        """Decide one object: its environment first, then its control, its stack's, the default."""
        if self.environment_denied:
            return Decision(None, (Reason.NO_ENVIRONMENT_ACCESS,))
        if self.allow_all:
            return _ADMINISTRATOR
        decision = self._decide_uncapped(resource_id, labels)
        if self.ceiling == "read" and decision.level == "write":
            return replace(
                decision, level="read", reasons=(*decision.reasons, Reason.LOCAL_SOCKET_READ_ONLY)
            )
        return decision

    def _decide_uncapped(self, resource_id: str, labels: Mapping[str, str]) -> Decision:
        if self.covered is not None and resource_id not in self.covered:
            raise LookupError("object was not among those this visibility was built for")
        own = self.own.get(self._own_key(resource_id))
        if own is not None:
            return evaluate(own, default=self.default)
        if self.resource_type in INHERITING_TYPES:
            stack = stack_of(labels)
            inherited = None if stack is None else self.stacks.get(stack)
            if inherited is not None:
                return evaluate(inherited, default=self.default, inherited_from=stack)
        return evaluate(None, default=self.default)

    def level(self, resource_id: str, labels: Mapping[str, str]) -> AccessLevel | None:
        """Return the access level held over an object, or None if it must not be seen."""
        return self.decide(resource_id, labels).level

    def permits(
        self, resource_id: str, labels: Mapping[str, str], required: AccessLevel = "read"
    ) -> bool:
        """Report whether an object may be reached at the required level."""
        return satisfies(self.level(resource_id, labels), required)

    def sql_filter(
        self, column: str, environment_column: str, required: AccessLevel = "read"
    ) -> SqlFragment:
        """Return a WHERE expression admitting a subset of what permits() admits.

        environment_column names where the queried row keeps its environment; there is no default,
        because a filter that ignored it would admit a same-named object on another host. Both
        names must be table-qualified: the expression interpolates them inside subqueries over
        resource_control, whose own columns would otherwise capture an unqualified name.

        Exact for stacks. For every other type the object's labels are invisible to SQL, so the
        expression admits only objects with a control of their own, and neither inherited access
        nor the uncontrolled default is applied; it never admits what permits() refuses.
        """
        require_qualified_columns(column, environment_column)
        if self.environment_denied or (self.ceiling == "read" and required == "write"):
            return SqlFragment("1 = 0", ())
        if self.allow_all:
            return SqlFragment("1 = 1", ())
        return visible_clause(
            user_id=self.user_id,
            environment_id=self.environment_id,
            resource_type=self.resource_type,
            column=column,
            environment_column=environment_column,
            required=required,
            honour_default=(
                self.resource_type not in INHERITING_TYPES and satisfies(self.default, required)
            ),
        )

    def _own_key(self, resource_id: str) -> str:
        return canonical_stack_key(resource_id) if self.resource_type == "stack" else resource_id


class Authoriser:
    """Resolves visibility and enforces it; the only route from a caller to a Docker object.

    One instance serves one request: it remembers the visibility it has built, so a control or
    membership changed during the request is not seen by later checks on it.
    """

    def __init__(
        self,
        *,
        controls: ResourceControlRepository,
        environments: EnvironmentAccessRepository,
        policy: AccessPolicyService,
        inspector: ObjectInspector,
    ) -> None:
        self._controls = controls
        self._environments = environments
        self._policy = policy
        self._inspector = inspector
        self._built: dict[_VisibilityKey, Visibility] = {}
        self._environment_gate: dict[tuple[int, int], EnvironmentGate | None] = {}

    def forget_decisions(self) -> None:
        """Drop what this request remembered, so the next check reads the controls again."""
        self._built.clear()
        self._environment_gate.clear()

    async def environment_visible(self, principal: Principal, environment_id: int) -> bool:
        """Report whether the caller may see the environment at all.

        Administrators see every environment. A standard user needs a grant on it or on its
        group; with none, nothing scoped to it is reachable whatever the per-object controls say.
        The answer and the environment's locality are remembered for the request, like the
        visibility built from them.
        """
        if principal.is_administrator:
            return True
        return await self._gate(principal, environment_id) is not None

    async def _gate(self, principal: Principal, environment_id: int) -> EnvironmentGate | None:
        key = (principal.user_id, environment_id)
        if key not in self._environment_gate:
            self._environment_gate[key] = await self._environments.gate(
                principal.user_id, environment_id
            )
        return self._environment_gate[key]

    async def require_environment(self, principal: Principal, environment_id: int) -> None:
        """Raise ResourceNotFound unless the caller may see the environment.

        A missing environment and a denied one are the same answer.
        """
        if not await self.environment_visible(principal, environment_id):
            logger.info(
                "access_refused",
                extra={
                    "user_id": principal.user_id,
                    "environment_id": environment_id,
                    "outcome": "not_found",
                    "branch": "no_environment_access",
                },
            )
            raise ResourceNotFound("not found")

    async def require_create(self, principal: Principal, environment_id: int) -> None:
        """Raise unless the caller may create objects in the environment.

        Administrators may anywhere. A standard user needs a grant on the environment, and its
        local socket is read-only for them. ResourceNotFound without the grant, so a missing
        environment and a denied one answer alike; PermissionDenied on the local socket, since
        a caller who can see the environment already knows it exists.
        """
        branch = await self._create_refusal(principal, environment_id)
        if branch is None:
            return
        outcome = "not_found" if branch == "no_environment_access" else "forbidden"
        logger.info(
            "access_refused",
            extra={
                "user_id": principal.user_id,
                "environment_id": environment_id,
                "required": "create",
                "outcome": outcome,
                "branch": branch,
            },
        )
        if outcome == "not_found":
            raise ResourceNotFound("not found")
        raise PermissionDenied("insufficient access")

    async def may_create(self, principal: Principal, environment_id: int) -> bool:
        """Report whether require_create would pass, for deciding what to offer."""
        return await self._create_refusal(principal, environment_id) is None

    async def _create_refusal(self, principal: Principal, environment_id: int) -> str | None:
        if principal.is_administrator:
            return None
        gate = await self._gate(principal, environment_id)
        if gate is None:
            return "no_environment_access"
        return "local_socket_read_only" if gate.is_local else None

    async def resolve_visibility(
        self,
        principal: Principal,
        environment_id: int,
        resource_type: ResourceType,
        resource_ids: Collection[str] | None = None,
    ) -> Visibility:
        """Build the predicate for one caller, one environment and one object type.

        resource_ids narrows the read of the type's own controls to the objects the caller will
        ask about; stack controls are always read in full because inheritance needs them.
        """
        key: _VisibilityKey = (principal.user_id, environment_id, resource_type)
        wanted = (
            None if resource_ids is None or resource_type == "stack" else frozenset(resource_ids)
        )
        held = self._built.get(key)
        if held is not None and _covers(held, wanted):
            return held
        if held is not None and held.covered is not None and wanted is not None:
            wanted = held.covered | wanted
        built = await self._build(principal, environment_id, resource_type, wanted)
        self._built[key] = built
        return built

    async def require_access(
        self, principal: Principal, key: ResourceKey, level: AccessLevel = "read"
    ) -> None:
        """Raise unless the caller holds the level over the object.

        ResourceNotFound when the caller holds no access to it, so the response never depends on
        whether the object exists. PermissionDenied when the caller holds access to this object
        but below the level required, since they have already been shown it. The labels come from
        the injected inspector; the caller has no way to supply them.
        """
        if principal.is_administrator:
            return
        if not await self.environment_visible(principal, key.environment_id):
            self._log_refusal(principal, key, level, "not_found", None, "no_environment_access")
            raise ResourceNotFound("not found")
        if not is_canonical_id(key.resource_type, key.resource_id):
            self._log_refusal(principal, key, level, "not_found", None, "malformed_id")
            raise ResourceNotFound("not found")
        labels = await self._labels(key)
        visibility = await self.resolve_visibility(
            principal, key.environment_id, key.resource_type, (key.resource_id,)
        )
        decision = visibility.decide(key.resource_id, labels)
        held = decision.level
        if satisfies(held, level):
            return
        if held is None:
            self._log_refusal(principal, key, level, "not_found", held, "no_access")
            raise ResourceNotFound("not found")
        branch = (
            "local_socket_read_only"
            if Reason.LOCAL_SOCKET_READ_ONLY in decision.reasons
            else "below_required"
        )
        self._log_refusal(principal, key, level, "forbidden", held, branch)
        raise PermissionDenied("insufficient access")

    async def filter_visible[T: Controllable](
        self, principal: Principal, items: Sequence[T], level: AccessLevel = "read"
    ) -> list[T]:
        """Return the items the caller holds the level over, in their original order.

        Silent by design: a list omits what the caller may not see and never refuses. The items
        are view models built from a Docker listing, so their labels are Docker's.
        """
        if principal.is_administrator:
            return list(items)
        ids: dict[tuple[int, ResourceType], set[str]] = {}
        for item in items:
            ids.setdefault((item.environment_id, item.resource_type), set()).add(item.resource_id)
        predicates: dict[tuple[int, ResourceType], Visibility] = {}
        for group, wanted in ids.items():
            predicates[group] = await self.resolve_visibility(principal, *group, wanted)
        return [
            item
            for item in items
            if predicates[(item.environment_id, item.resource_type)].permits(
                item.resource_id, item.labels, level
            )
        ]

    @asynccontextmanager
    async def mutation(
        self, principal: Principal, key: ResourceKey, level: AccessLevel = "write"
    ) -> AsyncIterator[None]:
        """Check access on entry, so a refused caller never reaches the body that calls Docker."""
        await self.require_access(principal, key, level)
        yield

    async def _build(
        self,
        principal: Principal,
        environment_id: int,
        resource_type: ResourceType,
        wanted: frozenset[str] | None,
    ) -> Visibility:
        if principal.is_administrator:
            return Visibility(
                principal.user_id, environment_id, resource_type, True, "write", {}, {}
            )
        gate = await self._gate(principal, environment_id)
        if gate is None:
            return Visibility(
                principal.user_id,
                environment_id,
                resource_type,
                False,
                None,
                {},
                {},
                environment_denied=True,
            )
        default = await self._policy.uncontrolled_default()
        own = await self._standing(principal, environment_id, resource_type, wanted)
        stacks = (
            await self._standing(principal, environment_id, "stack")
            if resource_type in INHERITING_TYPES
            else {}
        )
        return Visibility(
            principal.user_id,
            environment_id,
            resource_type,
            False,
            default,
            own,
            stacks,
            wanted,
            ceiling="read" if gate.is_local else "write",
        )

    async def _labels(self, key: ResourceKey) -> Mapping[str, str]:
        if key.resource_type not in INHERITING_TYPES:
            return {}
        return await self._inspector.labels_of(
            key.environment_id, key.resource_type, key.resource_id
        )

    async def _standing(
        self,
        principal: Principal,
        environment_id: int,
        resource_type: ResourceType,
        resource_ids: Collection[str] | None = None,
    ) -> dict[str, ControlStanding]:
        rows = await self._controls.list_standing(
            principal.user_id, environment_id, resource_type, resource_ids
        )
        if resource_type == "stack":
            return {canonical_stack_key(row.resource_id): row for row in rows}
        return {row.resource_id: row for row in rows}

    def _log_refusal(
        self,
        principal: Principal,
        key: ResourceKey,
        required: AccessLevel,
        outcome: str,
        held: AccessLevel | None,
        branch: str,
    ) -> None:
        logger.info(
            "access_refused",
            extra={
                "user_id": principal.user_id,
                "environment_id": key.environment_id,
                "resource_type": key.resource_type,
                "resource_id": key.resource_id,
                "required": required,
                "held": held,
                "outcome": outcome,
                "branch": branch,
            },
        )


def _covers(held: Visibility, wanted: frozenset[str] | None) -> bool:
    if held.covered is None:
        return True
    return wanted is not None and wanted <= held.covered
