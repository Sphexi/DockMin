"""Assign owners to volumes that predate volume ownership, one previewed pass at a time."""

import asyncio
import hashlib
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.activity_event import (
    ActivityEvent,
    ActivityEventRepository,
    ActivityFilter,
)
from app.db.repositories.clocked import Clock
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.user import UserRepository
from app.docker_engine.models import ContainerSummary, Volume
from app.logging import get_logger
from app.services.access import ResourceKey, canonical_stack_key, is_canonical_id, stack_of
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.containers import display_name, short_id
from app.services.guards import require_administrator
from app.services.host_objects import read_containers, read_volumes, volume_users
from app.services.object_inspector import (
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.resource_controls import ResourceControlService

logger = get_logger(__name__)

MAX_VOLUMES: Final = 500
CREATING_ACTION: Final = "container_created"
CONTROL_REMOVED_ACTION: Final = "resource_control_deleted"
CHANGED_MESSAGE: Final = (
    "The volumes or their owners changed since you reviewed them; nothing was changed."
)

type Outcome = Literal["assign", "no_candidate", "ambiguous", "unreadable"]
type Source = Literal["container_control", "activity"]
type ItemStatus = Literal[
    "assigned", "controlled", "gone", "changed", "busy", "unreachable", "failed"
]


class BackfillChanged(ValidationFailed):
    """The plan is no longer the one the caller reviewed; nothing was changed."""

    reason = "changed_since_review"

    def __init__(self) -> None:
        super().__init__({"plan": CHANGED_MESSAGE})


@dataclass(frozen=True, slots=True)
class Candidate:
    """A user a volume could be assigned to."""

    user_id: int
    username: str


@dataclass(frozen=True, slots=True)
class MountingContainer:
    """A container that mounts a volume, with the owner it points to, if any."""

    id: str
    short_id: str
    name: str
    owner: Candidate | None
    source: Source | None


@dataclass(frozen=True, slots=True)
class BackfillVolume:
    """One uncontrolled volume and what the preview concluded about it."""

    name: str
    outcome: Outcome
    containers: tuple[MountingContainer, ...]
    candidates: tuple[Candidate, ...]
    source: Source | None

    @property
    def owner(self) -> Candidate | None:
        """Return the owner to assign; set only for an `assign` outcome."""
        return self.candidates[0] if self.outcome == "assign" else None


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    """The preview of one environment: every listed volume, and the digest the apply binds to."""

    environment_id: int
    environment_name: str
    volumes: tuple[BackfillVolume, ...]
    uncontrolled_total: int
    digest: str

    @property
    def truncated(self) -> bool:
        """Report whether more volumes lack a control than are listed."""
        return self.uncontrolled_total > len(self.volumes)

    def of(self, outcome: Outcome) -> tuple[BackfillVolume, ...]:
        """Return the listed volumes with one outcome, in name order."""
        return tuple(volume for volume in self.volumes if volume.outcome == outcome)


@dataclass(frozen=True, slots=True)
class BackfillItem:
    """What happened to one volume of the plan."""

    name: str
    status: ItemStatus
    owner_name: str
    message: str


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """The outcome of an apply, one item per volume the reviewed plan assigned."""

    environment_id: int
    environment_name: str
    items: tuple[BackfillItem, ...]

    @property
    def assigned(self) -> int:
        """Return how many volumes were assigned."""
        return sum(1 for item in self.items if item.status == "assigned")


@dataclass(frozen=True, slots=True)
class _Owner:
    user_id: int
    source: Source


@dataclass(frozen=True, slots=True)
class _Preview:
    plan: BackfillPlan
    created_at: dict[str, str]


@dataclass(frozen=True, slots=True)
class _ControlIndex:
    volumes: frozenset[str]
    stacks: frozenset[str]

    def covers(self, volume: Volume) -> bool:
        if volume.name in self.volumes:
            return True
        stack = stack_of(volume.labels)
        return stack is not None and stack in self.stacks


_MESSAGES: Final[dict[ItemStatus, str]] = {
    "assigned": "Assigned to {owner}.",
    "controlled": "Skipped: the volume has an owner or access settings now.",
    "gone": "Skipped: the volume no longer exists.",
    "changed": "Skipped: the volume was replaced since you reviewed it.",
    "busy": "Skipped: another action on this volume is in progress.",
    "unreachable": "Skipped: the host could not be read.",
    "failed": "Not assigned: the owner could not be recorded.",
}


def plan_digest(volumes: Iterable[BackfillVolume]) -> str:
    """Return the SHA-256 over the sorted `volume user_id` lines of the volumes to assign."""
    lines = sorted(
        f"{volume.name} {volume.owner.user_id}\n" for volume in volumes if volume.owner is not None
    )
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def _volume_name(arguments: Arguments) -> str:
    return str(arguments["name"])


def _readable(volume: Volume) -> bool:
    return is_canonical_id("volume", volume.name) and not volume.labels_truncated


def _sort_key(volume: Volume) -> tuple[str, str]:
    return volume.name.casefold(), volume.name


def _decide(
    volume: Volume,
    using: Sequence[ContainerSummary],
    owners: dict[str, _Owner],
    usernames: dict[int, str],
) -> BackfillVolume:
    if not _readable(volume):
        return BackfillVolume(volume.name, "unreadable", (), (), None)
    mounting = []
    for container in sorted(using, key=lambda c: (display_name(c.names, c.id).casefold(), c.id)):
        owner = owners.get(container.id)
        username = None if owner is None else usernames.get(owner.user_id)
        mounting.append(
            MountingContainer(
                id=container.id,
                short_id=short_id(container.id),
                name=display_name(container.names, container.id),
                owner=None
                if owner is None or username is None
                else Candidate(owner.user_id, username),
                source=None if owner is None or username is None else owner.source,
            )
        )
    candidates = tuple(
        sorted(
            {m.owner for m in mounting if m.owner is not None},
            key=lambda c: (c.username.casefold(), c.user_id),
        )
    )
    outcome: Outcome = "no_candidate"
    if len(candidates) > 1:
        outcome = "ambiguous"
    elif candidates and all(m.owner is not None for m in mounting):
        outcome = "assign"
    source: Source | None = None
    if outcome == "assign":
        source = (
            "activity" if any(m.source == "activity" for m in mounting) else "container_control"
        )
    return BackfillVolume(volume.name, outcome, tuple(mounting), candidates, source)


class VolumeBackfillService:
    """Administrator-only owner backfill; writes only through the resource control service."""

    def __init__(
        self,
        *,
        db: Session,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        locks: ObjectLockRegistry,
        controls: ResourceControlService,
        control_rows: ResourceControlRepository,
        events: ActivityEventRepository,
        users: UserRepository,
        activity: ActivityRecorder,
    ) -> None:
        self._db = db
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._locks = locks
        self._controls = controls
        self._control_rows = control_rows
        self._events = events
        self._users = users
        self._activity = activity

    async def has_uncontrolled(self, actor: Principal, environment_id: int) -> bool:
        """Report whether any volume lacks a control of its own and an inherited one.

        False for a caller who is not an administrator and for a host that cannot be read.
        """
        if not actor.is_administrator:
            return False
        try:
            client = await self._hosts.client(environment_id)
            volumes = await read_volumes(client)
        except (EnvironmentUnavailable, ObjectTooLarge):
            return False
        index = await self._index(actor, environment_id, None)
        return any(not index.covers(volume) for volume in volumes)

    async def preview(self, actor: Principal, environment_id: int) -> BackfillPlan:
        """List the uncontrolled volumes, at most 500 by name, each with its outcome.

        PermissionDenied for a standard user; EnvironmentUnavailable, VolumeListTooLarge and
        ContainerListTooLarge when the host cannot be read in full.
        """
        return (await self._preview(actor, environment_id)).plan

    async def _preview(self, actor: Principal, environment_id: int) -> _Preview:
        require_administrator(actor)
        await self._authoriser.require_environment(actor, environment_id)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        volumes, containers = await asyncio.gather(read_volumes(client), read_containers(client))
        index = await self._index(actor, environment_id, None)
        uncontrolled = sorted((v for v in volumes if not index.covers(v)), key=_sort_key)
        listed = await self._assess(environment_id, uncontrolled[:MAX_VOLUMES], containers)
        plan = BackfillPlan(
            environment_id=environment_id,
            environment_name=environment.name,
            volumes=listed,
            uncontrolled_total=len(uncontrolled),
            digest=plan_digest(listed),
        )
        return _Preview(plan, {v.name: v.created_at for v in uncontrolled})

    async def candidates(
        self,
        environment_id: int,
        volumes: Sequence[Volume],
        containers: Sequence[ContainerSummary],
    ) -> dict[str, BackfillVolume]:
        """Return the preview's verdict for each volume by name; the caller has authorised.

        For a service that already holds the host's lists and has checked the caller is an
        administrator.
        """
        assessed = await self._assess(environment_id, volumes, containers)
        return {volume.name: volume for volume in assessed}

    async def apply(
        self, actor: Principal, environment_id: int, expected_plan: str
    ) -> BackfillResult:
        """Assign the volumes of the reviewed plan, each re-checked under its own lock.

        BackfillChanged, before anything is written, when the plan now differs from the one
        `expected_plan` was taken from. A volume that was replaced, gained a control or vanished
        since is skipped and reported; the others proceed.
        """
        preview = await self._preview(actor, environment_id)
        plan = preview.plan
        if plan.digest != expected_plan:
            raise BackfillChanged()
        items = [
            await self._apply_one(actor, environment_id, v, preview.created_at[v.name])
            for v in plan.of("assign")
        ]
        return BackfillResult(environment_id, plan.environment_name, tuple(items))

    async def _apply_one(
        self, actor: Principal, environment_id: int, reviewed: BackfillVolume, created_at: str
    ) -> BackfillItem:
        owner = reviewed.owner
        if owner is None:
            raise ValueError("only a volume with an owner to assign can be applied")

        def item(status: ItemStatus, message: str) -> BackfillItem:
            return BackfillItem(reviewed.name, status, owner.username, message)

        holder = LockHolder(UserActor.of(actor), actor.username, "backfill")
        key = ObjectKey(environment_id, "volume", reviewed.name)
        try:
            async with self._locks.hold(key, holder):
                status = await self._write_if_unchanged(actor, environment_id, reviewed, created_at)
        except ObjectBusy:
            return item("busy", _MESSAGES["busy"])
        except Exception as error:
            logger.warning(
                "volume_backfill_item_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            return item("failed", _MESSAGES["failed"])
        return item(status, _MESSAGES[status].format(owner=owner.username))

    async def _write_if_unchanged(
        self, actor: Principal, environment_id: int, reviewed: BackfillVolume, created_at: str
    ) -> ItemStatus:
        name = reviewed.name
        self._inspector.forget_volume(environment_id, name)
        try:
            volume = await self._inspector.volume(environment_id, name)
        except ResourceNotFound:
            return "gone"
        except (EnvironmentUnavailable, ObjectTooLarge):
            return "unreachable"
        if volume.created_at != created_at:
            return "changed"
        if (await self._index(actor, environment_id, [name])).covers(volume):
            return "controlled"
        claimed = await self.assign(actor, environment_id, name, reviewed)
        return "assigned" if claimed else "controlled"

    @audited(
        action="resource_control_created",
        target_type="volume",
        target_id=_volume_name,
        environment_id=environment_argument,
    )
    async def assign(
        self, actor: Principal, environment_id: int, name: str, volume: BackfillVolume
    ) -> bool:
        """Put an uncontrolled volume under a control owned by its planned owner.

        For `apply`, with the volume's lock held; no route may call this with a caller's input.
        False, with no row, when the volume already has a control.
        """
        require_administrator(actor)
        audit = current_audit()
        owner = volume.owner
        if owner is None:
            raise ValueError("only a volume with an owner to assign can be written")
        audit.detail.update(
            backfill=True,
            candidate_source=volume.source,
            containers=",".join(m.short_id for m in volume.containers),
            owner_user_id=owner.user_id,
            access_level="write",
        )
        async with self._db.transaction():
            claimed = await self._controls.claim(
                ResourceKey(environment_id, "volume", name), owner.user_id
            )
            if not claimed:
                audit.unchanged()
        return claimed

    async def _index(
        self, actor: Principal, environment_id: int, names: Collection[str] | None
    ) -> _ControlIndex:
        own = await self._control_rows.list_standing(actor.user_id, environment_id, "volume", names)
        stacks = await self._control_rows.list_standing(actor.user_id, environment_id, "stack")
        return _ControlIndex(
            frozenset(row.resource_id for row in own),
            frozenset(canonical_stack_key(row.resource_id) for row in stacks),
        )

    async def _assess(
        self,
        environment_id: int,
        volumes: Sequence[Volume],
        containers: Sequence[ContainerSummary],
    ) -> tuple[BackfillVolume, ...]:
        using = volume_users(containers)
        mounting = {c.id: c for v in volumes if _readable(v) for c in using.get(v.name, ())}
        owners: dict[str, _Owner] = {}
        for container_id in mounting:
            owner = await self._owner_of(environment_id, container_id)
            if owner is not None:
                owners[container_id] = owner
        usernames = await self._users.usernames({o.user_id for o in owners.values()})
        return tuple(_decide(v, using.get(v.name, ()), owners, usernames) for v in volumes)

    async def _owner_of(self, environment_id: int, container_id: str) -> _Owner | None:
        control = await self._control_rows.get_for(environment_id, "container", container_id)
        if control is not None:
            if control.owner_user_id is None:
                return None
            return _Owner(control.owner_user_id, "container_control")
        creator = await self._creator_of(environment_id, container_id)
        return None if creator is None else _Owner(creator, "activity")

    async def _creator_of(self, environment_id: int, container_id: str) -> int | None:
        created = await self._newest(environment_id, container_id, CREATING_ACTION, user=True)
        if created is None:
            return None
        removed = await self._newest(environment_id, container_id, CONTROL_REMOVED_ACTION)
        if removed is not None and _position(removed) > _position(created):
            return None
        return created.actor_user_id

    async def _newest(
        self, environment_id: int, container_id: str, action: str, *, user: bool = False
    ) -> ActivityEvent | None:
        found = await self._events.page(
            ActivityFilter(
                environment_id=environment_id,
                target_type="container",
                target_id=container_id,
                action=action,
                result="success",
                actor_type="user" if user else None,
            ),
            limit=1,
        )
        return found.items[0] if found.items else None


def _position(event: ActivityEvent) -> tuple[str, int]:
    return event.occurred_at, event.id


def build_volume_backfill(
    *,
    db: Session,
    clock: Clock,
    authoriser: Authoriser,
    inspector: EngineObjectInspector,
    locks: ObjectLockRegistry,
    controls: ResourceControlService,
    activity: ActivityRecorder,
) -> VolumeBackfillService:
    """Wire the backfill over one request's session and the services it already holds."""
    return VolumeBackfillService(
        db=db,
        authoriser=authoriser,
        inspector=inspector,
        locks=locks,
        controls=controls,
        control_rows=ResourceControlRepository(db, clock),
        events=ActivityEventRepository(db, clock),
        users=UserRepository(db, clock),
        activity=activity,
    )
