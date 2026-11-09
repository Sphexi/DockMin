"""Networks on a host: lists and details, forms and reviews, and the audited network operations."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from typing import Any, Final, Literal

import anyio

from app.auth.errors import (
    PermissionDenied,
    ProductError,
    ResourceNotFound,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.db.engine import Database, Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.db.repositories.environment import Environment
from app.db.repositories.pagination import CursorPosition, Page, encode_cursor
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.stack import StackKind
from app.db.repositories.team import TeamRepository
from app.db.repositories.user import UserRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import (
    EngineError,
    EnvironmentUnreachable,
    NotFound,
    RequestOutcomeUnknown,
)
from app.docker_engine.models import (
    ContainerInspect,
    ContainerSummary,
    Network,
    SwarmService,
)
from app.logging import get_logger
from app.services.access import ResourceKey, ResourceRef, SubmittedLabels, is_canonical_id, stack_of
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.container_actions import BusyView, EngineRefused, engine_detail
from app.services.containers import (
    AccessPanel,
    LabelView,
    build_access_panel,
    display_name,
    joins_networks,
    parse_engine_time,
    short_id,
    stack_kind,
    stack_name,
)
from app.services.grantees import GranteeDirectory
from app.services.guards import require_administrator
from app.services.host_objects import (
    DEFAULT_PAGE_SIZE,
    MAX_PRUNE,
    USAGE_FILTERS,
    ObjectUser,
    OptionView,
    PruneStatus,
    SettleStep,
    audited_client,
    audited_list_read,
    busy_view,
    check_limit,
    create_failure,
    decode_position,
    describe_service_users,
    describe_users,
    failure_reason,
    hold_all,
    in_transaction,
    label_views,
    network_users,
    option_views,
    permitted_container_ids,
    permitted_service_ids,
    read_containers,
    read_networks,
    read_services,
    row_id,
    run_shielded,
    service_network_users,
    settled,
    write_settled_row,
)
from app.services.network_addressing import (
    AddressingInvalid,
    NetworkRequest,
    overlapping,
    parse_network_form,
    restricted_network_options,
)
from app.services.object_inspector import (
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.resource_controls import ControlView, ResourceControlService

logger = get_logger(__name__)

__all__ = [
    "DRIVER_FILTERS",
    "KIND_FILTERS",
    "MAX_NAME",
    "MAX_TEXT",
    "NAMESPACE_SHARED",
    "SCOPE_FILTERS",
    "SETTLE_SECONDS",
    "UNJOINABLE_DRIVERS",
    "AttachOffer",
    "EndpointView",
    "NetworkChangedSinceReview",
    "NetworkChoice",
    "NetworkCreated",
    "NetworkDetail",
    "NetworkFilters",
    "NetworkGone",
    "NetworkInUse",
    "NetworkKind",
    "NetworkNameInUse",
    "NetworkPage",
    "NetworkPredefined",
    "NetworkPruneCandidate",
    "NetworkPruneItem",
    "NetworkPrunePlan",
    "NetworkPruneResult",
    "NetworkPruneTarget",
    "NetworkQueryService",
    "NetworkRefused",
    "NetworkRemoveImpact",
    "NetworkRemoved",
    "NetworkRow",
    "NetworkService",
    "PoolView",
    "RestrictedNetwork",
    "clip_text",
    "container_joins_networks",
    "network_kind",
]

MAX_QUERY: Final = 128


MAX_NAME: Final = 255


MAX_TEXT: Final = 64


MAX_POOLS_SHOWN: Final = 16


MAX_ENDPOINTS_SHOWN: Final = 500


MAX_CHOICES: Final = 200


SETTLE_SECONDS: Final = 120.0


CHANGED_SINCE_REVIEW: Final = "This network changed since you reviewed it; nothing was changed."


NAMESPACE_SHARED: Final = (
    "This container shares another network namespace and cannot join networks."
)


DRIVER_FILTERS: Final = ("bridge", "overlay", "macvlan", "ipvlan", "other")


SCOPE_FILTERS: Final = ("local", "swarm")


KIND_FILTERS: Final = ("user", "predefined", "system")


_PREDEFINED: Final = frozenset({("bridge", "bridge"), ("host", "host"), ("none", "null")})


_GATEWAY_BRIDGE: Final = ("docker_gwbridge", "bridge")


UNJOINABLE_DRIVERS: Final = frozenset({"host", "null"})


_OFFERED_DRIVERS: Final = frozenset({"bridge", "overlay", "macvlan", "ipvlan"})


type NetworkKind = Literal["user", "predefined", "system"]


class NetworkGone(ProductError):
    """The network vanished between authorisation and the Engine call."""

    def __init__(self) -> None:
        super().__init__("The network no longer exists.")


class NetworkInUse(ProductError):
    """Containers or services use the network; the message and count are the product's own."""

    def __init__(self, containers: int) -> None:
        super().__init__("The network is used by one or more containers or services.")
        self.containers = containers


class NetworkPredefined(ProductError):
    """The network is one Docker provides, which cannot be removed."""

    def __init__(self) -> None:
        super().__init__("This network is provided by Docker and cannot be removed.")


class NetworkRefused(ProductError):
    """Docker rejected a network configuration; the Engine's text is not part of the message.

    engine_detail is that text, scrubbed and cut, for a page shown to an administrator.
    """

    def __init__(self, engine_detail: str | None = None) -> None:
        super().__init__("Docker refused this network configuration.")
        self.engine_detail = engine_detail


class NetworkChangedSinceReview(ValidationFailed):
    """The network is no longer the one the caller reviewed; nothing was changed."""

    def __init__(self) -> None:
        super().__init__({"network": CHANGED_SINCE_REVIEW})


class NetworkNameInUse(ValidationFailed):
    """A network with the requested name already exists."""

    def __init__(self) -> None:
        super().__init__({"name": "A network with this name already exists."})


class RestrictedNetwork(ValidationFailed):
    """A standard user's request uses a driver, address or option only administrators may set."""


@dataclass(frozen=True, slots=True)
class NetworkFilters:
    """The list filters as submitted; validated before any Engine call."""

    q: str | None = None
    driver: str | None = None
    scope: str | None = None
    usage: str | None = None
    kind: str | None = None
    stack: str | None = None


@dataclass(frozen=True, slots=True)
class NetworkRow:
    """One network of the list.

    `containers` names the containers the viewer may see, at most ten; `containers_more` counts
    permitted ones cut by that limit and `containers_hidden` those the viewer may not see.
    `services` and its counts do the same for Swarm services. `container_count` counts every
    container on the host with an endpoint on the network and `service_count` every service that
    attaches to it; `in_use` is their sum. `services_known` is false when the host's services
    could not be read, and the service fields then count nothing.
    """

    environment_id: int
    id: str
    short_id: str
    name: str
    driver: str
    scope: str
    kind: NetworkKind
    subnets: tuple[str, ...]
    internal: bool
    attachable: bool
    ipv6: bool
    stack: str | None
    created_at: datetime | None
    in_use: int
    containers: tuple[ObjectUser, ...]
    containers_more: int
    containers_hidden: int
    can_remove: bool
    services: tuple[ObjectUser, ...] = ()
    services_more: int = 0
    services_hidden: int = 0
    container_count: int = 0
    service_count: int = 0
    services_known: bool = True
    stack_kind: StackKind | None = None


@dataclass(frozen=True, slots=True)
class NetworkPage:
    """A page of rows; `total` matches the filters and `visible` is the caller's count."""

    page: Page[NetworkRow]
    total: int
    visible: int
    limit: int
    environment_name: str
    is_local: bool
    can_create: bool
    can_prune: bool


@dataclass(frozen=True, slots=True)
class PoolView:
    """One address pool as the Engine reports it."""

    subnet: str
    gateway: str
    ip_range: str


@dataclass(frozen=True, slots=True)
class EndpointView:
    """A running container's endpoint on a network."""

    container_id: str
    short_id: str
    name: str
    ipv4_address: str
    ipv6_address: str
    mac_address: str


@dataclass(frozen=True, slots=True)
class NetworkDetail:
    """One network as the detail page shows it.

    `options` is None for a standard user: driver options name host interfaces. `endpoints` are
    the running containers the viewer may see; `stopped` the other containers attached to
    the network. Each list counts separately what the display limit cut (`*_more`) and what
    the viewer may not see (`*_hidden`). `services` names the Swarm services that attach to the
    network, with their scheduling mode in `state`; `container_count` and `service_count` count
    each kind and `in_use` is their sum. `services_known` is false when the host's services could
    not be read, and the service fields then count nothing.
    """

    environment_id: int
    environment_name: str
    is_local: bool
    id: str
    short_id: str
    name: str
    driver: str
    scope: str
    kind: NetworkKind
    created_at: datetime | None
    internal: bool
    attachable: bool
    ipv6: bool
    stack: str | None
    pools: tuple[PoolView, ...]
    options: tuple[OptionView, ...] | None
    labels: tuple[LabelView, ...]
    endpoints: tuple[EndpointView, ...]
    endpoints_more: int
    endpoints_hidden: int
    stopped: tuple[ObjectUser, ...]
    stopped_more: int
    stopped_hidden: int
    in_use: int
    can_remove: bool
    can_manage_access: bool
    services: tuple[ObjectUser, ...] = ()
    services_more: int = 0
    services_hidden: int = 0
    container_count: int = 0
    service_count: int = 0
    services_known: bool = True
    stack_kind: StackKind | None = None


@dataclass(frozen=True, slots=True)
class NetworkRemoveImpact:
    """What removing a network names: the network, its containers and the services that use it."""

    environment_id: int
    environment_name: str
    network_id: str
    short_id: str
    name: str
    driver: str
    scope: str
    kind: NetworkKind
    stack: str | None
    in_use: int
    containers: tuple[ObjectUser, ...]
    containers_more: int
    containers_hidden: int
    services: tuple[ObjectUser, ...] = ()
    services_more: int = 0
    services_hidden: int = 0
    container_count: int = 0
    service_count: int = 0

    @property
    def removable(self) -> bool:
        """Report whether the network is one the product may remove at all."""
        return self.kind == "user"


@dataclass(frozen=True, slots=True)
class NetworkPruneCandidate:
    """A network a prune would remove."""

    id: str
    short_id: str
    name: str
    driver: str
    stack: str | None
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class NetworkPrunePlan:
    """The networks a prune would remove, at most 100, and how many matched in all."""

    environment_id: int
    environment_name: str
    candidates: tuple[NetworkPruneCandidate, ...]
    total: int

    @property
    def truncated(self) -> bool:
        """Report whether more matched than are listed."""
        return self.total > len(self.candidates)


@dataclass(frozen=True, slots=True)
class NetworkPruneTarget:
    """One network the caller reviewed in a prune confirmation."""

    network_id: str
    name: str


@dataclass(frozen=True, slots=True)
class NetworkPruneItem:
    """The outcome for one listed network."""

    network_id: str
    short_id: str
    name: str
    status: PruneStatus


@dataclass(frozen=True, slots=True)
class NetworkPruneResult:
    """One item per listed network, in listed order."""

    items: tuple[NetworkPruneItem, ...]

    @property
    def removed(self) -> int:
        """Return how many networks were removed."""
        return sum(1 for item in self.items if item.status == "removed")


@dataclass(frozen=True, slots=True)
class NetworkCreated:
    """What a create did; `control_created` is false when the claim failed."""

    environment_id: int
    network_id: str
    name: str
    driver: str
    control_created: bool


@dataclass(frozen=True, slots=True)
class NetworkRemoved:
    """What a removal did; `control_released` is false when the control could not be dropped."""

    environment_id: int
    network_id: str
    name: str
    driver: str
    control_released: bool


@dataclass(frozen=True, slots=True)
class NetworkChoice:
    """A network the caller may attach to; `predefined` networks take no aliases or addresses."""

    id: str
    short_id: str
    name: str
    driver: str
    scope: str
    subnets: tuple[str, ...]
    ipv6: bool
    predefined: bool


@dataclass(frozen=True, slots=True)
class AttachOffer:
    """What the connect form offers for one container; `blocked` explains an empty offer."""

    environment_id: int
    environment_name: str
    container_id: str
    short_id: str
    container_name: str
    state: str
    networks: tuple[NetworkChoice, ...]
    blocked: str | None


@dataclass(frozen=True, slots=True)
class _Entry:
    network: Network
    ref: ResourceRef
    kind: NetworkKind
    sort_name: str
    row_id: int


@dataclass(slots=True)
class _Criteria:
    q: str | None
    driver: str | None
    scope: str | None
    usage: str | None
    kind: str | None
    stack: str | None


@dataclass(slots=True)
class _CreateRun:
    network_id: str | None = None


def network_kind(network: Network) -> NetworkKind:
    """Classify a network: one Docker provides, one a Swarm node provides, or the user's own."""
    identity = (network.name, network.driver)
    if identity in _PREDEFINED:
        return "predefined"
    if network.ingress or identity == _GATEWAY_BRIDGE:
        return "system"
    return "user"


def _network_argument(arguments: Arguments) -> str:
    return str(arguments["network_id"])[:64]


def _clean(value: str | None) -> str | None:
    stripped = None if value is None else value.strip()
    return stripped or None


def _compile(filters: NetworkFilters) -> _Criteria:
    fields: dict[str, str] = {}
    text = _clean(filters.q)
    if text is not None and len(text) > MAX_QUERY:
        fields["q"] = f"Search for at most {MAX_QUERY} characters."
    choices: tuple[tuple[str, tuple[str, ...], str, str | None], ...] = (
        ("driver", DRIVER_FILTERS, "Choose one of the listed drivers.", filters.driver),
        ("scope", SCOPE_FILTERS, "Choose local or swarm.", filters.scope),
        ("usage", USAGE_FILTERS, "Choose used or unused.", filters.usage),
        ("kind", KIND_FILTERS, "Choose user, predefined or system.", filters.kind),
    )
    chosen: dict[str, str | None] = {}
    for name, allowed, message, value in choices:
        cleaned = _clean(value)
        if cleaned is not None and cleaned not in allowed:
            fields[name] = message
        chosen[name] = cleaned
    stack = _clean(filters.stack)
    if stack is not None and len(stack) > MAX_QUERY:
        fields["stack"] = f"Use at most {MAX_QUERY} characters."
    if fields:
        raise ValidationFailed(fields)
    return _Criteria(
        None if text is None else text.casefold(),
        chosen["driver"],
        chosen["scope"],
        chosen["usage"],
        chosen["kind"],
        None if stack is None else stack.casefold(),
    )


def _entry_of(network: Network, environment_id: int) -> _Entry | None:
    if not is_canonical_id("network", network.id):
        logger.warning("network_list_entry_skipped", extra={"reason": "malformed_id"})
        return None
    return _Entry(
        network=network,
        ref=ResourceRef(environment_id, "network", network.id, network.labels),
        kind=network_kind(network),
        sort_name=network.name[:MAX_NAME].casefold(),
        row_id=row_id(network.id),
    )


def _order(entry: _Entry) -> tuple[str, int, str]:
    return entry.sort_name, entry.row_id, entry.network.id


def _matches(entry: _Entry, criteria: _Criteria, used: int) -> bool:
    network = entry.network
    if criteria.driver is not None:
        offered = network.driver if network.driver in _OFFERED_DRIVERS else "other"
        if offered != criteria.driver:
            return False
    if criteria.scope is not None and network.scope != criteria.scope:
        return False
    if criteria.usage == "used" and used == 0:
        return False
    if criteria.usage == "unused" and used > 0:
        return False
    if criteria.kind is not None and entry.kind != criteria.kind:
        return False
    if criteria.stack is not None and stack_of(network.labels) != criteria.stack:
        return False
    return criteria.q is None or criteria.q in network.name.casefold()


def clip_text(value: str) -> str:
    return value[:MAX_TEXT]


def _subnets(network: Network) -> tuple[str, ...]:
    return tuple(clip_text(pool.subnet) for pool in network.ipam[:MAX_POOLS_SHOWN] if pool.subnet)


def container_joins_networks(container: ContainerInspect) -> bool:
    mode = container.host_config.get("NetworkMode")
    return joins_networks(mode if isinstance(mode, str) else "")


def _prune_candidates(
    networks: Sequence[Network],
    containers: Sequence[ContainerSummary],
    services: Sequence[SwarmService] = (),
) -> list[NetworkPruneCandidate]:
    users = network_users(containers, networks)
    serviced = service_network_users(services, networks)
    found: list[NetworkPruneCandidate] = []
    for network in networks:
        if (
            not is_canonical_id("network", network.id)
            or network_kind(network) != "user"
            or network.scope != "local"
            or users.get(network.id)
            or serviced.get(network.id)
        ):
            continue
        found.append(
            NetworkPruneCandidate(
                id=network.id,
                short_id=short_id(network.id),
                name=network.name[:MAX_NAME],
                stack=stack_name(network.labels),
                driver=network.driver,
                created_at=parse_engine_time(network.created),
            )
        )
    found.sort(key=lambda c: (c.name.casefold(), c.id))
    return found


def _engine_body(request: NetworkRequest) -> dict[str, Any]:
    config: list[dict[str, str]] = []
    for pool in request.pools:
        entry = {"Subnet": str(pool.subnet)}
        if pool.gateway is not None:
            entry["Gateway"] = str(pool.gateway)
        if pool.ip_range is not None:
            entry["IPRange"] = str(pool.ip_range)
        config.append(entry)
    ipam: dict[str, Any] = {"Driver": "default"}
    if config:
        ipam["Config"] = config
    body: dict[str, Any] = {
        "Name": request.name,
        "Driver": request.driver,
        "Internal": request.internal,
        "EnableIPv6": request.enable_ipv6,
        "IPAM": ipam,
    }
    if request.driver == "overlay":
        body["Attachable"] = request.attachable
    if request.options:
        body["Options"] = dict(request.options)
    if request.labels:
        body["Labels"] = dict(request.labels)
    return body


def _option_field(form: Mapping[str, str], key: str) -> str:
    for name, value in form.items():
        if name.startswith("option-") and name.endswith("-key") and value.strip() == key:
            return name
    return "options"


def _standard_refusals(request: NetworkRequest, form: Mapping[str, str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    if request.driver not in ("bridge", "overlay"):
        fields["driver"] = "Only administrators can create this kind of network."
    for pool in request.pools:
        version = pool.subnet.version
        message = "Only administrators can choose addresses; leave this empty to use Docker's."
        fields[f"subnet_v{version}"] = message
        if pool.gateway is not None:
            fields[f"gateway_v{version}"] = message
        if pool.ip_range is not None:
            fields[f"ip_range_v{version}"] = message
    for key in restricted_network_options(request):
        fields[_option_field(form, key)] = f"Only administrators can set the option {key}."
    return fields


def _restricted_tokens(request: NetworkRequest) -> str:
    tokens: list[str] = []
    if request.driver not in ("bridge", "overlay"):
        tokens.append("driver")
    if request.pools:
        tokens.append("addressing")
    tokens.extend(restricted_network_options(request))
    return ",".join(tokens)


async def _swarm_services(
    client: EngineClient, environment: Environment, poke: Callable[[int], None] | None = None
) -> list[SwarmService]:
    """Read the services of a Swarm manager; any other environment has none."""
    if environment.type != "swarm":
        return []
    return await read_services(client, environment.id, poke)


async def _services_if_readable(
    client: EngineClient, environment: Environment, poke: Callable[[int], None] | None
) -> list[SwarmService] | None:
    """Read the services, or None when the host cannot answer; a display may go on without."""
    try:
        return await _swarm_services(client, environment, poke)
    except (EnvironmentUnavailable, ObjectTooLarge):
        return None


class NetworkQueryService:
    """Reads networks through the authoriser; nothing here changes a network."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        controls: ResourceControlService,
        users: UserRepository,
        teams: TeamRepository,
        poke: Callable[[int], None] | None = None,
    ) -> None:
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._controls = controls
        self.grantees = GranteeDirectory(users, teams)
        self._poke = poke

    async def page(
        self,
        actor: Principal,
        environment_id: int,
        filters: NetworkFilters | None = None,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> NetworkPage:
        """Return one page of the networks the caller may see, read live from the host.

        ValidationFailed for a bad filter, limit or cursor; EnvironmentUnavailable,
        NetworkListTooLarge and ContainerListTooLarge when the host cannot be read in full. A host
        whose services cannot be read still lists its networks, with `services_known` false.
        """
        criteria = _compile(filters or NetworkFilters())
        size = check_limit(limit)
        position = decode_position(cursor)
        await self._authoriser.require_environment(actor, environment_id)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        networks, containers, services = await asyncio.gather(
            read_networks(client),
            read_containers(client),
            _services_if_readable(client, environment, self._poke),
        )
        known_services = services is not None
        entries = [e for e in (_entry_of(n, environment_id) for n in networks) if e is not None]
        visible = await self._visible(actor, entries)
        listed = [e.network for e in visible]
        users = network_users(containers, listed)
        serviced = service_network_users(services or [], listed)

        def used_by(network_id: str) -> int:
            return len(users.get(network_id, ())) + len(serviced.get(network_id, ()))

        matched = [e for e in visible if _matches(e, criteria, used_by(e.network.id))]
        ordered = sorted(matched, key=_order)
        remaining = (
            ordered
            if position is None
            else [e for e in ordered if _order(e)[:2] > (position.sort_value, position.row_id)]
        )
        window = remaining[:size]
        permitted = await permitted_container_ids(
            self._authoriser, actor, environment_id, containers
        )
        permitted_services = await permitted_service_ids(
            self._authoriser, actor, environment_id, services or []
        )
        writable = await self._writable(actor, window)
        rows = [
            self._row_of(
                environment_id,
                entry,
                users.get(entry.network.id, []),
                serviced.get(entry.network.id, []),
                permitted,
                permitted_services,
                writable,
                known_services,
            )
            for entry in window
        ]
        next_cursor = None
        if len(remaining) > size and window:
            last = _order(window[-1])
            next_cursor = encode_cursor(CursorPosition(last[0], last[1]))
        return NetworkPage(
            page=Page(rows, next_cursor),
            total=len(ordered),
            visible=len(visible),
            limit=size,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            can_create=await self._authoriser.may_create(actor, environment_id),
            can_prune=actor.is_administrator,
        )

    async def require_create(self, actor: Principal, environment_id: int) -> None:
        """Refuse unless the caller may create networks here: ResourceNotFound, PermissionDenied."""
        await self._authoriser.require_create(actor, environment_id)

    async def origin_name(self, actor: Principal, environment_id: int, network_id: str) -> str:
        """Return the name of a network the caller may read; ResourceNotFound otherwise."""
        return (await self._readable(actor, environment_id, network_id)).name

    async def detail(self, actor: Principal, environment_id: int, network_id: str) -> NetworkDetail:
        """Return one network for the detail page; ResourceNotFound unless the caller may see it.

        A host whose services cannot be read still shows the network, with `services_known` false.
        """
        network = await self._readable(actor, environment_id, network_id)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        containers, services = await asyncio.gather(
            read_containers(client), _services_if_readable(client, environment, self._poke)
        )
        by_id = {c.id: c for c in containers}
        using = network_users(containers, [network]).get(network.id, [])
        serving = service_network_users(services or [], [network]).get(network.id, [])
        ids = {c.id for c in using} | set(network.containers)
        known = [by_id[i] for i in ids if i in by_id]
        permitted = await permitted_container_ids(self._authoriser, actor, environment_id, known)
        permitted_services = await permitted_service_ids(
            self._authoriser, actor, environment_id, serving
        )
        named_services, services_more, services_hidden = describe_service_users(
            serving, permitted_services, MAX_ENDPOINTS_SHOWN
        )
        allowed = sorted(
            (
                EndpointView(
                    container_id=container_id,
                    short_id=short_id(container_id),
                    name=display_name(by_id[container_id].names, container_id),
                    ipv4_address=clip_text(endpoint.ipv4_address),
                    ipv6_address=clip_text(endpoint.ipv6_address),
                    mac_address=clip_text(endpoint.mac_address),
                )
                for container_id, endpoint in network.containers.items()
                if container_id in permitted
            ),
            key=lambda e: (e.name.casefold(), e.container_id),
        )
        endpoints = tuple(allowed[:MAX_ENDPOINTS_SHOWN])
        stopped, stopped_more, stopped_hidden = describe_users(
            [c for c in using if c.id not in network.containers], permitted, MAX_ENDPOINTS_SHOWN
        )
        ref = ResourceRef(environment_id, "network", network.id, network.labels)
        kind = network_kind(network)
        writable = bool(await self._authoriser.filter_visible(actor, [ref], "write"))
        return NetworkDetail(
            environment_id=environment_id,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            id=network.id,
            short_id=short_id(network.id),
            name=network.name[:MAX_NAME],
            driver=network.driver,
            scope=network.scope,
            kind=kind,
            created_at=parse_engine_time(network.created),
            internal=network.internal,
            attachable=network.attachable,
            ipv6=network.enable_ipv6,
            stack=stack_name(network.labels),
            stack_kind=stack_kind(network.labels),
            pools=tuple(
                PoolView(clip_text(p.subnet), clip_text(p.gateway), clip_text(p.ip_range))
                for p in network.ipam[:MAX_POOLS_SHOWN]
            ),
            options=option_views(network.options) if actor.is_administrator else None,
            labels=label_views(network.labels, actor.is_administrator),
            endpoints=endpoints,
            endpoints_more=len(allowed) - len(endpoints),
            endpoints_hidden=len(network.containers) - len(allowed),
            stopped=stopped,
            stopped_more=stopped_more,
            stopped_hidden=stopped_hidden,
            in_use=len(ids) + len(serving),
            can_remove=writable and kind == "user",
            can_manage_access=actor.is_administrator,
            services=named_services,
            services_more=services_more,
            services_hidden=services_hidden,
            container_count=len(ids),
            service_count=len(serving),
            services_known=services is not None,
        )

    async def access_panel(
        self, actor: Principal, environment_id: int, network_id: str
    ) -> AccessPanel:
        """Return the access tab's content; administrators only."""
        network = await self._readable(actor, environment_id, network_id)
        if not actor.is_administrator:
            raise PermissionDenied("administrator role required")
        key = ResourceKey(environment_id, "network", network.id)
        control: ControlView | None = await self._controls.get(actor, key)
        return await build_access_panel(
            self, key, network.name[:MAX_NAME], short_id(network.id), control
        )

    async def remove_impact(
        self, actor: Principal, environment_id: int, network_id: str
    ) -> NetworkRemoveImpact:
        """Name what removing the network takes away; write holders only."""
        if not is_canonical_id("network", network_id):
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "network", network_id), "write"
        )
        network = await self._inspector.network(environment_id, network_id)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        containers, services = await asyncio.gather(
            read_containers(client), _swarm_services(client, environment, self._poke)
        )
        by_id = {c.id: c for c in containers}
        using = network_users(containers, [network]).get(network.id, [])
        serving = service_network_users(services, [network]).get(network.id, [])
        ids = {c.id for c in using} | set(network.containers)
        known = [by_id[i] for i in ids if i in by_id]
        permitted = await permitted_container_ids(self._authoriser, actor, environment_id, known)
        permitted_services = await permitted_service_ids(
            self._authoriser, actor, environment_id, serving
        )
        named, more, hidden = describe_users(known, permitted)
        named_services, services_more, services_hidden = describe_service_users(
            serving, permitted_services
        )
        return NetworkRemoveImpact(
            environment_id=environment_id,
            environment_name=environment.name,
            network_id=network.id,
            short_id=short_id(network.id),
            name=network.name[:MAX_NAME],
            driver=network.driver,
            scope=network.scope,
            kind=network_kind(network),
            stack=stack_name(network.labels),
            in_use=len(ids) + len(serving),
            containers=named,
            containers_more=more,
            containers_hidden=hidden + len(ids) - len(known),
            services=named_services,
            services_more=services_more,
            services_hidden=services_hidden,
            container_count=len(ids),
            service_count=len(serving),
        )

    async def prune_plan(self, actor: Principal, environment_id: int) -> NetworkPrunePlan:
        """List the networks a prune would remove, at most 100; administrators only."""
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        networks, containers, services = await asyncio.gather(
            read_networks(client),
            read_containers(client),
            _swarm_services(client, environment, self._poke),
        )
        found = _prune_candidates(networks, containers, services)
        return NetworkPrunePlan(
            environment_id, environment.name, tuple(found[:MAX_PRUNE]), len(found)
        )

    async def names(
        self, actor: Principal, environment_id: int, prefix: str, *, limit: int
    ) -> list[str]:
        """Return the first `limit` names, by name, of visible networks starting with `prefix`.

        Case is ignored. One host read; EnvironmentUnavailable and NetworkListTooLarge when the
        host cannot be read.
        """
        await self._authoriser.require_environment(actor, environment_id)
        client = await self._hosts.client(environment_id)
        wanted = prefix.casefold()
        entries = [
            e
            for e in (_entry_of(n, environment_id) for n in await read_networks(client))
            if e is not None and e.network.name.casefold().startswith(wanted)
        ]
        visible = sorted(await self._visible(actor, entries), key=_order)
        return [e.network.name for e in visible[:limit]]

    async def attach_offer(
        self, actor: Principal, environment_id: int, container_id: str
    ) -> AttachOffer:
        """Describe what the connect form offers for a container; write holders only."""
        if not is_canonical_id("container", container_id):
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "container", container_id), "write"
        )
        container = await self._inspector.inspect(environment_id, container_id)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        networks = await read_networks(client)
        joined = {
            *(endpoint.network_id for endpoint in container.network_settings.networks.values()),
            *container.network_settings.networks,
        }
        blocked = None if container_joins_networks(container) else NAMESPACE_SHARED
        offered = (
            ()
            if blocked
            else await self._offered(
                actor,
                environment_id,
                [
                    n
                    for n in networks
                    if n.id not in joined
                    and n.name not in joined
                    and n.driver not in UNJOINABLE_DRIVERS
                    and not (n.driver == "overlay" and not n.attachable)
                ],
            )
        )
        return AttachOffer(
            environment_id=environment_id,
            environment_name=environment.name,
            container_id=container.id,
            short_id=short_id(container.id),
            container_name=display_name([container.name], container.id),
            state=container.state.status,
            networks=offered,
            blocked=blocked,
        )

    async def _readable(self, actor: Principal, environment_id: int, network_id: str) -> Network:
        if not is_canonical_id("network", network_id):
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "network", network_id), "read"
        )
        return await self._inspector.network(environment_id, network_id)

    async def _visible(self, actor: Principal, entries: Sequence[_Entry]) -> list[_Entry]:
        permitted = {
            ref.resource_id
            for ref in await self._authoriser.filter_visible(actor, [e.ref for e in entries])
        }
        return [e for e in entries if e.network.id in permitted]

    async def _writable(self, actor: Principal, entries: Sequence[_Entry]) -> frozenset[str]:
        held = await self._authoriser.filter_visible(actor, [e.ref for e in entries], "write")
        return frozenset(ref.resource_id for ref in held)

    async def _offered(
        self,
        actor: Principal,
        environment_id: int,
        networks: Sequence[Network],
    ) -> tuple[NetworkChoice, ...]:
        entries = [e for e in (_entry_of(n, environment_id) for n in networks) if e is not None]
        if not actor.is_administrator:
            writable = await self._writable(actor, entries)
            entries = [
                e for e in entries if e.network.id in writable and e.network.driver != "host"
            ]
        entries.sort(key=_order)
        return tuple(
            NetworkChoice(
                id=e.network.id,
                short_id=short_id(e.network.id),
                name=e.network.name[:MAX_NAME],
                driver=e.network.driver,
                scope=e.network.scope,
                subnets=_subnets(e.network),
                ipv6=e.network.enable_ipv6,
                predefined=e.kind == "predefined",
            )
            for e in entries[:MAX_CHOICES]
        )

    def _row_of(
        self,
        environment_id: int,
        entry: _Entry,
        using: Sequence[ContainerSummary],
        serving: Sequence[SwarmService],
        permitted: frozenset[str],
        permitted_services: frozenset[str],
        writable: frozenset[str],
        services_known: bool,
    ) -> NetworkRow:
        network = entry.network
        named, more, hidden = describe_users(using, permitted)
        named_services, services_more, services_hidden = describe_service_users(
            serving, permitted_services
        )
        return NetworkRow(
            environment_id=environment_id,
            id=network.id,
            short_id=short_id(network.id),
            name=network.name[:MAX_NAME],
            driver=network.driver,
            scope=network.scope,
            kind=entry.kind,
            subnets=_subnets(network),
            internal=network.internal,
            attachable=network.attachable,
            ipv6=network.enable_ipv6,
            stack=stack_name(network.labels),
            stack_kind=stack_kind(network.labels),
            created_at=parse_engine_time(network.created),
            in_use=len(using) + len(serving),
            containers=named,
            containers_more=more,
            containers_hidden=hidden,
            can_remove=network.id in writable and entry.kind == "user",
            services=named_services,
            services_more=services_more,
            services_hidden=services_hidden,
            container_count=len(using),
            service_count=len(serving),
            services_known=services_known,
        )


class NetworkService:
    """Create and remove networks, each one audited operation, and the prune orchestrator."""

    def __init__(
        self,
        *,
        db: Session,
        database: Database,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        locks: ObjectLockRegistry,
        controls: ResourceControlService,
        activity: ActivityRecorder,
        clock: Clock,
        poke: Callable[[int], None] | None = None,
    ) -> None:
        self._poke = poke
        self._db = db
        self._database = database
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._locks = locks
        self._controls = controls
        self._activity = activity
        self._clock = clock
        self._background: set[asyncio.Future[None]] = set()

    @audited(action="network_created", target_type="network", environment_id=environment_argument)
    async def create(
        self, actor: Principal, environment_id: int, form: Mapping[str, str]
    ) -> NetworkCreated:
        """Create a network from a submitted form; the caller owns it afterwards.

        A standard user may create only bridge (and overlay on a Swarm manager) networks with
        automatic addressing and the allowed options. ValidationFailed keyed by form field for an
        invalid or restricted request, an address range that overlaps another network, or a name
        in use; NetworkRefused when Docker rejects it; EnvironmentUnavailable.
        """
        audit = current_audit()
        await self._authoriser.require_create(actor, environment_id)
        environment = await self._hosts.environment(environment_id)
        request = self._validated(actor, form, environment.type)
        audit.detail.update(
            name=request.name,
            driver=request.driver,
            scope="swarm" if request.driver == "overlay" else "local",
            internal=request.internal,
            attachable=request.attachable,
            ipv6=request.enable_ipv6,
            subnets=",".join(str(p.subnet) for p in request.pools),
            gateways=",".join(str(p.gateway) for p in request.pools if p.gateway is not None),
            ip_ranges=",".join(str(p.ip_range) for p in request.pools if p.ip_range is not None),
            option_keys=",".join(request.options),
            labels=len(request.labels),
        )
        restricted = _restricted_tokens(request)
        if restricted and actor.is_administrator:
            audit.detail["restricted"] = restricted
        client = await audited_client(self._hosts, environment_id)
        existing = await audited_list_read(read_networks(client))
        self._refuse_conflicts(request, existing)
        run = _CreateRun()
        try:
            network_id = await run_shielded(
                partial(self._create_on_host, client, _engine_body(request), run),
                partial(self._interrupted_create, actor, environment_id, run),
            )
        except EngineError as error:
            raise create_failure(
                error,
                event="network_create_refused",
                name_in_use=NetworkNameInUse,
                refused=NetworkRefused,
            ) from None
        audit.target_id = network_id
        control_created = await self._claim(actor, environment_id, network_id)
        return NetworkCreated(
            environment_id, network_id, request.name, request.driver, control_created
        )

    @audited(
        action="network_removed",
        target_type="network",
        target_id=_network_argument,
        environment_id=environment_argument,
    )
    async def remove(
        self,
        actor: Principal,
        environment_id: int,
        network_id: str,
        *,
        expected_name: str,
        expected_in_use: int,
        pruned: bool = False,
    ) -> NetworkRemoved:
        """Remove a network the caller reviewed, then release its control.

        `expected_name` and `expected_in_use` are what the confirmation showed; either differing
        refuses the removal. Containers in any state with an endpoint on the network refuse it:
        there is no force. NetworkPredefined for a network Docker provides. The control is
        released in the transaction that commits the success row, and only after the Engine
        removed the network; if the release fails the removal still counts and the row records
        control_released as false.
        """
        audit = current_audit()
        if not is_canonical_id("network", network_id):
            raise ResourceNotFound("not found")
        key = ResourceKey(environment_id, "network", network_id)
        try:
            await self._authoriser.require_access(actor, key, "write")
        except (EnvironmentUnavailable, ObjectTooLarge):
            audit.refuse()
            raise
        environment = await self._hosts.environment(environment_id)
        network, client = await self._load(environment_id, network_id)
        audit.detail.update(name=network.name[:MAX_NAME], driver=network.driver)
        if pruned:
            audit.detail["pruned"] = True
        if network_kind(network) != "user":
            audit.reason = "network_predefined"
            raise NetworkPredefined()
        holder = LockHolder(UserActor.of(actor), actor.username, "remove")
        async with hold_all(
            self._locks, [ObjectKey(environment_id, "network", network_id)], holder
        ):
            self._inspector.forget_network(environment_id, network_id)
            network, client = await self._load(environment_id, network_id)
            if expected_name != network.name:
                audit.reason = "changed_since_review"
                raise NetworkChangedSinceReview()
            in_use = await self._count_in_use(client, environment, network)
            if in_use != expected_in_use:
                audit.reason = "changed_since_review"
                raise NetworkChangedSinceReview()
            if in_use:
                audit.reason = "network_in_use"
                raise NetworkInUse(in_use)
            try:
                await run_shielded(
                    partial(client.networks.remove, network_id),
                    partial(self._interrupted_remove, actor, environment_id, network),
                )
            except EngineError as error:
                raise await self._remove_failure(error, client, environment, network) from None
            released = await self._release(actor, environment_id, network_id)
        return NetworkRemoved(
            environment_id, network_id, network.name[:MAX_NAME], network.driver, released
        )

    async def run_prune(
        self, actor: Principal, environment_id: int, targets: Sequence[NetworkPruneTarget]
    ) -> NetworkPruneResult:
        """Remove exactly the reviewed networks that are still candidates, one at a time.

        Each listed network is its own audited removal with its own lock and row; nothing is
        rolled back or stopped early. The Engine's own prune endpoint is never called.
        Administrators only, at most 100 networks.
        """
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        listed = list({t.network_id: t for t in targets}.values())
        if not listed or len(listed) > MAX_PRUNE:
            raise ValidationFailed({"networks": f"Select between 1 and {MAX_PRUNE} networks."})
        if any(not is_canonical_id("network", t.network_id) for t in listed):
            raise ValidationFailed({"networks": "A network id in the selection is not valid."})
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        networks, containers, services = await asyncio.gather(
            read_networks(client),
            read_containers(client),
            _swarm_services(client, environment, self._poke),
        )
        current = {c.id: c for c in _prune_candidates(networks, containers, services)}
        items: list[NetworkPruneItem] = []
        for target in listed:
            candidate = current.get(target.network_id)
            if candidate is None or candidate.name != target.name:
                items.append(
                    NetworkPruneItem(
                        target.network_id,
                        short_id(target.network_id),
                        target.name[:MAX_NAME],
                        "skipped_changed",
                    )
                )
                continue
            items.append(await self._prune_one(actor, environment_id, candidate))
        return NetworkPruneResult(tuple(items))

    def describe_busy(self, busy: ObjectBusy, viewer: Principal) -> BusyView:
        """Describe the lock holder to a viewer: names only for administrators."""
        return busy_view(busy, viewer, self._locks.now())

    async def _prune_one(
        self, actor: Principal, environment_id: int, candidate: NetworkPruneCandidate
    ) -> NetworkPruneItem:
        status: PruneStatus
        try:
            await self.remove(
                actor,
                environment_id,
                candidate.id,
                expected_name=candidate.name,
                expected_in_use=0,
                pruned=True,
            )
        except NetworkInUse:
            status = "skipped_in_use"
        except (NetworkChangedSinceReview, NetworkPredefined):
            status = "skipped_changed"
        except ObjectBusy:
            status = "busy"
        except (NetworkGone, ResourceNotFound):
            status = "gone"
        except Exception as error:
            logger.warning(
                "network_prune_item_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            status = "failed"
        else:
            status = "removed"
        return NetworkPruneItem(candidate.id, candidate.short_id, candidate.name, status)

    def _validated(
        self, actor: Principal, form: Mapping[str, str], environment_type: str | None
    ) -> NetworkRequest:
        audit = current_audit()
        try:
            request = parse_network_form(form, environment_type=environment_type)
            SubmittedLabels(request.labels)
        except ValidationFailed as invalid:
            audit.reason = "invalid"
            audit.detail["fields"] = ",".join(sorted(invalid.fields))
            raise
        if not actor.is_administrator:
            refused = _standard_refusals(request, form)
            if refused:
                audit.reason = "restricted"
                audit.detail["fields"] = ",".join(sorted(refused))
                raise RestrictedNetwork(refused)
        return request

    def _refuse_conflicts(self, request: NetworkRequest, existing: Sequence[Network]) -> None:
        audit = current_audit()
        if any(network.name == request.name for network in existing):
            audit.reason = "name_in_use"
            raise NetworkNameInUse()
        others = [
            (network.name, pool.subnet)
            for network in existing
            for pool in network.ipam[:MAX_POOLS_SHOWN]
            if pool.subnet
        ]
        fields: dict[str, str] = {}
        for pool in request.pools:
            names = overlapping(replace(request, pools=(pool,)), others)
            if names:
                fields[f"subnet_v{pool.subnet.version}"] = (
                    f"This subnet overlaps the existing network {names[0]}."
                )
        if fields:
            audit.reason = "invalid"
            audit.detail["fields"] = ",".join(sorted(fields))
            raise AddressingInvalid(fields)

    async def _create_on_host(
        self, client: EngineClient, body: Mapping[str, Any], run: _CreateRun
    ) -> str:
        network_id = await client.networks.create(body)
        run.network_id = network_id
        return network_id

    async def _claim(self, actor: Principal, environment_id: int, network_id: str) -> bool:
        audit = current_audit()
        key = ResourceKey(environment_id, "network", network_id)

        def note(created: bool) -> None:
            audit.detail["control_created"] = created

        try:
            return await retry_busy(
                partial(
                    in_transaction,
                    self._db,
                    partial(self._controls.claim, key, actor.user_id),
                    None,
                    note,
                )
            )
        except asyncio.CancelledError:
            await self._write_settled(
                actor,
                "network_created",
                environment_id,
                network_id,
                {**audit.detail, "interrupted": True},
                claim=True,
            )
            raise
        except Exception as error:
            self._log_control_failure("network_control_claim_failed", key, error)
            audit.detail["control_created"] = False
            return False

    async def _release(self, actor: Principal, environment_id: int, network_id: str) -> bool:
        audit = current_audit()
        key = ResourceKey(environment_id, "network", network_id)

        def note(_: bool) -> None:
            audit.detail["control_released"] = True

        try:
            await retry_busy(
                partial(in_transaction, self._db, partial(self._controls.release, key), None, note)
            )
        except asyncio.CancelledError:
            await self._write_settled(
                actor,
                "network_removed",
                environment_id,
                network_id,
                {**audit.detail, "interrupted": True},
                claim=False,
            )
            raise
        except Exception as error:
            self._log_control_failure("network_control_release_failed", key, error)
            audit.detail["control_released"] = False
            return False
        return True

    async def _load(self, environment_id: int, network_id: str) -> tuple[Network, EngineClient]:
        audit = current_audit()
        try:
            network = await self._inspector.network(environment_id, network_id)
            client = await self._hosts.client(environment_id)
        except EnvironmentUnavailable as error:
            audit.reason = error.kind
            raise
        except ObjectTooLarge:
            audit.reason = "too_large"
            raise
        except ResourceNotFound:
            audit.reason = "network_gone"
            raise NetworkGone() from None
        return network, client

    async def _count_in_use(
        self, client: EngineClient, environment: Environment, network: Network
    ) -> int:
        containers, services = await asyncio.gather(
            audited_list_read(read_containers(client)),
            audited_list_read(_swarm_services(client, environment, self._poke)),
        )
        users = network_users(containers, [network]).get(network.id, [])
        serving = service_network_users(services, [network]).get(network.id, [])
        return len({c.id for c in users} | set(network.containers)) + len(serving)

    async def _remove_failure(
        self, error: EngineError, client: EngineClient, environment: Environment, network: Network
    ) -> ProductError:
        audit = current_audit()
        logger.warning(
            "network_remove_refused",
            extra={"error": type(error).__name__, "status": error.status_code},
        )
        if isinstance(error, NotFound):
            audit.reason = "network_gone"
            return NetworkGone()
        if isinstance(error, RequestOutcomeUnknown):
            audit.reason = "outcome_unknown"
            return EnvironmentUnavailable("outcome_unknown")
        if isinstance(error, EnvironmentUnreachable):
            audit.reason = "unreachable"
            return EnvironmentUnavailable("unreachable")
        in_use = 0
        try:
            in_use = await self._count_in_use(client, environment, network)
        except ProductError:
            in_use = 0
        if in_use or network.scope == "swarm":
            audit.reason = "network_in_use"
            return NetworkInUse(in_use)
        audit.reason = "engine_rejected"
        return EngineRefused(engine_detail(error))

    async def _interrupted_create(
        self,
        actor: Principal,
        environment_id: int,
        run: _CreateRun,
        task: asyncio.Future[str],
    ) -> None:
        audit = current_audit()
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, SETTLE_SECONDS)
            detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
            if run.network_id is not None:
                await self._write_settled(
                    actor, "network_created", environment_id, run.network_id, detail, claim=True
                )
            elif not finished:
                task.add_done_callback(
                    partial(self._settle_late_create, actor, environment_id, run, dict(detail))
                )
                await self._write_settled(
                    actor,
                    "network_created",
                    environment_id,
                    None,
                    detail,
                    failure="outcome_unknown",
                )
            else:
                await self._write_settled(
                    actor,
                    "network_created",
                    environment_id,
                    None,
                    detail,
                    failure=failure_reason(error) if error is not None else "interrupted",
                )

    def _settle_late_create(
        self,
        actor: Principal,
        environment_id: int,
        run: _CreateRun,
        detail: dict[str, DetailValue],
        task: asyncio.Future[str],
    ) -> None:
        if task.cancelled() or task.exception() is not None or run.network_id is None:
            return
        future = asyncio.ensure_future(
            self._write_settled(
                actor,
                "network_created",
                environment_id,
                run.network_id,
                {**detail, "settled_late": True},
                claim=True,
            )
        )
        self._background.add(future)
        future.add_done_callback(self._background.discard)

    async def _interrupted_remove(
        self,
        actor: Principal,
        environment_id: int,
        network: Network,
        task: asyncio.Future[None],
    ) -> None:
        audit = current_audit()
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, SETTLE_SECONDS)
            detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
            if finished and error is None:
                await self._write_settled(
                    actor, "network_removed", environment_id, network.id, detail, claim=False
                )
                return
            await self._write_settled(
                actor,
                "network_removed",
                environment_id,
                network.id,
                detail,
                failure=failure_reason(error) if error is not None else "outcome_unknown",
            )

    async def _write_settled(
        self,
        actor: Principal,
        action: str,
        environment_id: int,
        target_id: str | None,
        detail: dict[str, DetailValue],
        *,
        claim: bool | None = None,
        failure: str | None = None,
    ) -> None:
        steps = (
            ()
            if claim is None or target_id is None
            else (self._control_step(actor, environment_id, target_id, detail, claim),)
        )
        await write_settled_row(
            self._database,
            self._clock,
            actor,
            action=action,
            target_type="network",
            target_id=target_id,
            environment_id=environment_id,
            detail=detail,
            failure=failure,
            steps=steps,
        )

    def _control_step(
        self,
        actor: Principal,
        environment_id: int,
        network_id: str,
        detail: dict[str, DetailValue],
        claim: bool,
    ) -> SettleStep:
        key = ResourceKey(environment_id, "network", network_id)
        field = "control_created" if claim else "control_released"

        def work(db: Session, recorder: ActivityRecorder) -> Callable[[], Awaitable[bool]]:
            controls = ResourceControlService(
                db=db, controls=ResourceControlRepository(db, self._clock), activity=recorder
            )
            if claim:
                return partial(controls.claim, key, actor.user_id)
            return partial(controls.release, key)

        def note(result: bool) -> None:
            detail[field] = result if claim else True

        def failed(error: Exception) -> None:
            self._log_control_failure("network_control_settle_failed", key, error)
            detail[field] = False

        return SettleStep(work, note, failed)

    def _log_control_failure(self, event: str, key: ResourceKey, error: Exception) -> None:
        logger.error(
            event,
            extra={
                "environment_id": key.environment_id,
                "network_id": key.resource_id,
                "error": type(error).__name__,
            },
        )
