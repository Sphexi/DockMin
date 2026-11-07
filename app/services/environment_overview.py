"""The figures, stack states and container table of an environment's page, scoped to the caller."""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final, Literal

from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.backup_record import BackupRecordRepository
from app.db.repositories.clocked import Clock, utc_now
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.db.repositories.resource_control import canonical_stack_key
from app.db.repositories.stack import StackKind
from app.docker_engine.models import SwarmService
from app.services.access import SWARM_NAMESPACE_LABEL, ResourceRef
from app.services.authorisation import Authoriser
from app.services.containers import ContainerBrief, ContainerListing, ContainerService
from app.services.host_objects import read_services
from app.services.image_queries import ImageQueryService
from app.services.object_inspector import EnvironmentUnavailable, HostClients, ObjectTooLarge
from app.services.restart_holds import HEALTH_CHECKER, environment_holds
from app.services.stack_queries import StackQueryService
from app.services.stack_views import ManagedStackName

MAX_STACK_TILES: Final = 100
MAX_CONTAINER_ROWS: Final = 50
FAILED_RUN_WINDOW: Final = timedelta(hours=24)

type StackState = Literal["running", "partly running", "stopped"]
type HostState = Literal["live", "unreachable", "unavailable"]
type FigureKind = Literal["count", "time"]


@dataclass(frozen=True, slots=True)
class CardLink:
    """A page a card figure leads to: a route name, its path parameters and its query."""

    route: str
    path: Mapping[str, int] = field(default_factory=dict)
    query: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class CardFigure:
    """One number on a card.

    A "count" figure shows `value`, which is None when it cannot be shown now (rendered as a
    dash), optionally out of `of`. A "time" figure shows `at`, a stored timestamp, which is None
    when nothing has happened yet.
    """

    label: str
    link: CardLink
    value: int | None = None
    of: int | None = None
    at: str | None = None
    kind: FigureKind = "count"


@dataclass(frozen=True, slots=True)
class StatCard:
    """A card of the row under the facts: a label, an icon name and its figures, headline first."""

    key: str
    label: str
    icon: str
    figures: tuple[CardFigure, ...]


@dataclass(frozen=True, slots=True)
class StackTile:
    """A managed stack with its state; state is None when it could not be read."""

    name: str
    kind: StackKind
    state: StackState | None


@dataclass(frozen=True, slots=True)
class ContainerLine:
    """One row of the container table."""

    id: str
    name: str
    stack: str | None
    image: str
    state: str
    status: str
    health: str | None


@dataclass(frozen=True, slots=True)
class OverviewData:
    """What the environment page shows below its facts.

    host is live when the host was read for this page, unreachable when its stored status is
    anything but reachable (nothing was read), and unavailable when the read failed. Everything
    that needs the host is then None or empty; the figures kept in the database still hold.
    """

    host: HostState
    cards: tuple[StatCard, ...]
    stacks: tuple[StackTile, ...]
    stacks_total: int
    containers: tuple[ContainerLine, ...]
    containers_total: int | None
    containers_link: CardLink

    @property
    def stacks_more(self) -> int:
        """Return how many managed stacks are not in the grid."""
        return max(self.stacks_total - len(self.stacks), 0)


class EnvironmentOverview:
    """Build an environment's overview from one host read of each kind and the database."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        hosts: HostClients,
        containers: ContainerService,
        stacks: StackQueryService,
        images: ImageQueryService | None,
        db: Session,
        clock: Clock = utc_now,
        poke: Callable[[int], None] | None = None,
    ) -> None:
        self._authoriser = authoriser
        self._hosts = hosts
        self._containers = containers
        self._stacks = stacks
        self._images = images
        self._db = db
        self._clock = clock
        self._poke = poke

    async def overview(self, actor: Principal, environment_id: int) -> OverviewData:
        """Return the overview of an environment the caller may see.

        ResourceNotFound when the caller may not see it. A host that cannot be read is reported
        in `host`, never raised.
        """
        await self._authoriser.require_environment(actor, environment_id)
        environment = await self._hosts.environment(environment_id)
        names = await self._stacks.managed_names(actor, environment_id, limit=MAX_PAGE_SIZE)
        wants_services = environment.type == "swarm" and any(n.kind == "swarm" for n in names)

        host: HostState = "live" if environment.status == "reachable" else "unreachable"
        listing: ContainerListing | None = None
        services: list[SwarmService] | None = None
        updates: int | None = None
        if host == "live":
            listing, services, updates = await asyncio.gather(
                self._read_containers(actor, environment_id),
                self._read_services(actor, environment_id, wanted=wants_services),
                self._count_updates(actor, environment_id),
            )
            if listing is None:
                host = "unavailable"
                services = None

        tiles = _tiles_of(names, listing, services)
        in_environment = {"environment_id": environment_id}
        containers = () if listing is None else listing.containers
        running = None if listing is None else sum(c.state == "running" for c in containers)
        unhealthy = (
            None
            if listing is None or listing.health_unavailable
            else sum(c.health == "unhealthy" for c in containers)
        )
        held = (
            None
            if listing is None
            else await self._held(actor, environment_id, {c.id for c in containers})
        )
        stack_figures = _stack_figures(tiles, in_environment, capped=len(names) >= MAX_PAGE_SIZE)
        containers_link = CardLink("environment_container_list", in_environment)
        automation_here = (("environment_id", str(environment_id)),)
        cards = [
            StatCard(
                "stacks",
                "Stacks",
                "layers",
                stack_figures,
            ),
            StatCard(
                "containers",
                "Containers",
                "container",
                (
                    CardFigure(
                        "running",
                        CardLink(
                            "environment_container_list", in_environment, (("state", "running"),)
                        ),
                        value=running,
                        of=None if listing is None else len(containers),
                    ),
                ),
            ),
            StatCard(
                "health",
                "Unhealthy and held",
                "activity",
                (
                    CardFigure(
                        "unhealthy",
                        CardLink(
                            "environment_container_list", in_environment, (("health", "unhealthy"),)
                        ),
                        value=unhealthy,
                    ),
                    CardFigure(
                        "held",
                        CardLink(
                            "automation",
                            query=(*automation_here, ("subsystem", HEALTH_CHECKER)),
                        ),
                        value=held,
                    ),
                ),
            ),
            StatCard(
                "updates",
                "Updates available",
                "download",
                (
                    CardFigure(
                        "images",
                        CardLink(
                            "environment_image_list", in_environment, (("update", "available"),)
                        ),
                        value=updates,
                    ),
                ),
            ),
            await self._automation_card(actor, environment_id, automation_here),
        ]
        return OverviewData(
            host=host,
            cards=tuple(cards),
            stacks=tuple(tiles[:MAX_STACK_TILES]),
            stacks_total=len(tiles),
            containers=tuple(_line_of(c) for c in containers[:MAX_CONTAINER_ROWS]),
            containers_total=None if listing is None else len(containers),
            containers_link=containers_link,
        )

    async def _automation_card(
        self, actor: Principal, environment_id: int, here: tuple[tuple[str, str], ...]
    ) -> StatCard:
        failed = await JobRunRepository(self._db, self._clock).count_failed_since(
            FAILED_RUN_WINDOW,
            environment_id=environment_id,
            owner_id=None if actor.is_administrator else actor.user_id,
        )
        figures = [
            CardFigure("failed in 24 hours", CardLink("automation", query=here), value=failed)
        ]
        if actor.is_administrator:
            latest = await BackupRecordRepository(self._db, self._clock).latest_success_at()
            figures.append(CardFigure("last backup", CardLink("backups"), at=latest, kind="time"))
        return StatCard("automation", "Automation", "workflow", tuple(figures))

    async def _read_containers(
        self, actor: Principal, environment_id: int
    ) -> ContainerListing | None:
        try:
            return await self._containers.brief_listing(actor, environment_id)
        except (EnvironmentUnavailable, ObjectTooLarge):
            return None

    async def _count_updates(self, actor: Principal, environment_id: int) -> int | None:
        if self._images is None:
            return None
        try:
            return await self._images.count_update_available(actor, environment_id)
        except (EnvironmentUnavailable, ObjectTooLarge):
            return None

    async def _read_services(
        self, actor: Principal, environment_id: int, *, wanted: bool
    ) -> list[SwarmService] | None:
        if not wanted:
            return None
        try:
            client = await self._hosts.client(environment_id)
            found = await read_services(client, environment_id, self._poke)
            if actor.is_administrator:
                return found
            refs = [
                ResourceRef(environment_id, "service", service.id, service.spec.labels)
                for service in found
            ]
            allowed = {r.resource_id for r in await self._authoriser.filter_visible(actor, refs)}
            return [service for service in found if service.id in allowed]
        except (EnvironmentUnavailable, ObjectTooLarge):
            return None

    async def _held(self, actor: Principal, environment_id: int, visible: set[str]) -> int:
        holds = await environment_holds(
            self._db,
            self._clock,
            environment_id,
            owner_id=None if actor.is_administrator else actor.user_id,
        )
        return len({hold.container_id for hold in holds} & visible)


def _tiles_of(
    names: Sequence[ManagedStackName],
    listing: ContainerListing | None,
    services: Sequence[SwarmService] | None,
) -> list[StackTile]:
    by_project: dict[str, list[ContainerBrief]] = {}
    if listing is not None:
        for container in listing.containers:
            if container.compose_key is not None:
                by_project.setdefault(container.compose_key, []).append(container)
    by_namespace: dict[str, list[SwarmService]] = {}
    for service in services or ():
        namespace = service.spec.labels.get(SWARM_NAMESPACE_LABEL)
        if namespace:
            by_namespace.setdefault(canonical_stack_key(namespace), []).append(service)
    tiles: list[StackTile] = []
    for stack in names:
        key = canonical_stack_key(stack.name)
        state: StackState | None = None
        if listing is not None:
            if stack.kind == "compose":
                state = _compose_state(by_project.get(key, ()))
            elif services is not None:
                state = _swarm_state(by_namespace.get(key, ()))
        tiles.append(StackTile(stack.name, stack.kind, state))
    return sorted(tiles, key=lambda tile: tile.name.casefold())


def _stack_figures(
    tiles: Sequence[StackTile], in_environment: Mapping[str, int], *, capped: bool
) -> tuple[CardFigure, ...]:
    figures = [
        _stack_figure(
            "running",
            CardLink("environment_stack_list", in_environment),
            [tile for tile in tiles if tile.kind == "compose"],
            capped=capped,
        )
    ]
    swarm = [tile for tile in tiles if tile.kind == "swarm"]
    if swarm:
        figures.append(
            _stack_figure(
                "swarm running",
                CardLink("environment_swarm_stack_list", in_environment),
                swarm,
                capped=False,
            )
        )
    return tuple(figures)


def _stack_figure(
    label: str, link: CardLink, tiles: Sequence[StackTile], *, capped: bool
) -> CardFigure:
    known = [tile for tile in tiles if tile.state is not None]
    notes = []
    if len(known) < len(tiles):
        notes.append(f"{len(tiles) - len(known)} unreadable")
    if capped:
        notes.append(f"first {MAX_PAGE_SIZE} stacks only")
    return CardFigure(
        f"{label} ({', '.join(notes)})" if notes else label,
        link,
        value=sum(tile.state == "running" for tile in known) if known or not tiles else None,
        of=len(known) if known or not tiles else None,
    )


def _compose_state(containers: Sequence[ContainerBrief]) -> StackState:
    running = sum(c.state == "running" for c in containers)
    if containers and running == len(containers):
        return "running"
    return "partly running" if running else "stopped"


def _swarm_state(services: Sequence[SwarmService]) -> StackState | None:
    counts = [service.service_status for service in services]
    if any(count is None for count in counts):
        return None
    known = [count for count in counts if count is not None]
    if known and all(c.desired_tasks > 0 and c.running_tasks >= c.desired_tasks for c in known):
        return "running"
    return "partly running" if any(c.running_tasks > 0 for c in known) else "stopped"


def _line_of(container: ContainerBrief) -> ContainerLine:
    return ContainerLine(
        id=container.id,
        name=container.name,
        stack=container.stack,
        image=container.image,
        state=container.state,
        status=container.status,
        health=container.health,
    )
