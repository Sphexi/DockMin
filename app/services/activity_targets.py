"""Whether the object an activity event acted on still exists."""

import asyncio
import re
from typing import Final, Literal

from app.auth.errors import ProductError, ResourceNotFound
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.activity_event import ActivityEvent
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.environment import Environment
from app.db.repositories.object_storage_target import ObjectStorageTargetRepository
from app.db.repositories.pagination import MAX_ROW_ID
from app.db.repositories.registry_credential import RegistryListRepository
from app.db.repositories.stack import StackListRepository
from app.db.repositories.team import TeamRepository
from app.db.repositories.user import UserRepository
from app.docker_engine.errors import EngineError
from app.docker_engine.errors import NotFound as EngineNotFound
from app.logging import get_logger
from app.services.guards import require_administrator
from app.services.object_inspector import EngineObjectInspector

logger = get_logger(__name__)

type Presence = Literal["present", "deleted", "unknown"]

HOST_BUDGET_SECONDS: Final = 3.0
HEX_ID: Final = re.compile(r"[0-9a-f]{64}")
IMAGE_ID: Final = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
SWARM_ID: Final = re.compile(r"[0-9a-z]{25}")
VOLUME_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
STACK_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
REUSED_ID_TARGETS: Final = frozenset({"automation_rule", "user", "team", "object_storage_target"})
INTEGER_ID_TARGETS: Final = REUSED_ID_TARGETS | {"registry_credential"}


def integer_id(text: str) -> int | None:
    """Return the stored row id a target id spells, or None."""
    if text.isascii() and text.isdigit() and 1 <= int(text) <= MAX_ROW_ID:
        return int(text)
    return None


class ActivityTargets:
    """Looks up whether an event's target still exists; administrators only."""

    def __init__(self, db: Session, inspector: EngineObjectInspector | None) -> None:
        self._rules = AutomationRuleRepository(db)
        self._users = UserRepository(db)
        self._teams = TeamRepository(db)
        self._storage = ObjectStorageTargetRepository(db)
        self._registries = RegistryListRepository(db)
        self._stacks = StackListRepository(db)
        self._inspector = inspector

    async def presence(self, actor: Principal, event: ActivityEvent) -> Presence:
        """Return whether the event's target exists; unknown unless that is certain.

        A host is read only when its environment is stored as reachable, with one time budget
        for all reads of the call. A budget spent or any failure to read is unknown, never deleted.
        """
        require_administrator(actor)
        kind, target = event.target_type, event.target_id
        if target is None:
            return "unknown"
        if kind in INTEGER_ID_TARGETS:
            row_id = integer_id(target)
            return "unknown" if row_id is None else await self._row(kind, row_id, event)
        environment_id = event.environment_id
        if environment_id is None or not _well_formed(kind, target):
            return "unknown"
        if kind == "stack" and await self._stacks.exists_folded(environment_id, target):
            return "present"
        return await self._host(environment_id, kind, target)

    async def _row(self, kind: str, row_id: int, event: ActivityEvent) -> Presence:
        created_at: str | None
        match kind:
            case "automation_rule":
                rule = await self._rules.get(row_id)
                created_at = None if rule is None else rule.created_at
            case "user":
                user = await self._users.get(row_id)
                created_at = None if user is None else user.created_at
            case "team":
                team = await self._teams.get(row_id)
                created_at = None if team is None else team.created_at
            case "object_storage_target":
                stored = await self._storage.get(row_id)
                created_at = None if stored is None else stored.created_at
            case _:
                return "present" if await self._registries.exists(row_id) else "deleted"
        if created_at is None or created_at > event.occurred_at:
            return "deleted"
        return "present"

    async def _host(self, environment_id: int, kind: str, target: str) -> Presence:
        inspector = self._inspector
        if inspector is None:
            return "unknown"
        try:
            environment = await inspector.hosts.environment(environment_id)
        except ResourceNotFound:
            return "unknown"
        if environment.status != "reachable":
            return "unknown"
        try:
            async with asyncio.timeout(HOST_BUDGET_SECONDS):
                return await self._read(inspector, environment, kind, target)
        except (TimeoutError, ProductError, EngineError, OSError) as error:
            logger.debug(
                "activity_target_unknown",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            return "unknown"

    async def _read(
        self, inspector: EngineObjectInspector, environment: Environment, kind: str, target: str
    ) -> Presence:
        env = environment.id
        try:
            match kind:
                case "container":
                    await inspector.inspect(env, target)
                case "network":
                    await inspector.network(env, target)
                case "volume":
                    await inspector.volume(env, target)
                case "image":
                    await self._image(inspector, env, target)
                case "service" | "node" if environment.type != "swarm":
                    return "unknown"
                case "service":
                    await inspector.service(env, target)
                case "node":
                    await inspector.node(env, target)
                case "stack":
                    return await self._stack(inspector, environment, target)
                case _:
                    return "unknown"
        except ResourceNotFound:
            return "deleted"
        return "present"

    async def _image(
        self, inspector: EngineObjectInspector, environment_id: int, target: str
    ) -> None:
        digest = target.removeprefix("sha256:")
        client = await inspector.hosts.client(environment_id)
        try:
            image = await client.images.inspect(f"sha256:{digest}")
        except EngineNotFound:
            raise ResourceNotFound("not found") from None
        if image.id.removeprefix("sha256:") != digest:
            raise ResourceNotFound("not found")

    async def _stack(
        self, inspector: EngineObjectInspector, environment: Environment, name: str
    ) -> Presence:
        key = name.casefold()
        client = await inspector.hosts.client(environment.id)
        if key in await client.containers.compose_projects():
            return "present"
        if environment.type == "swarm" and key in await client.swarm.stack_namespaces():
            return "present"
        return "deleted"


def _well_formed(kind: str, target: str) -> bool:
    match kind:
        case "container" | "network":
            return HEX_ID.fullmatch(target) is not None
        case "service" | "node":
            return SWARM_ID.fullmatch(target) is not None
        case "image":
            return IMAGE_ID.fullmatch(target) is not None
        case "volume":
            return VOLUME_NAME.fullmatch(target) is not None
        case "stack":
            return STACK_NAME.fullmatch(target) is not None
        case _:
            return False
