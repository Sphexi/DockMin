"""Swarm stacks as namespaces: the objects that share a stack label, and the control over them."""

import asyncio
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.stack import (
    StackListRepository,
    StackRepository,
    StackStatus,
    StackSummary,
    StoredStack,
)
from app.db.repositories.stack_dismissal import StackDismissalRepository, StoredDismissal
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, ResponseTooLarge
from app.docker_engine.models import Network, SwarmConfig, SwarmSecret, SwarmService, Volume
from app.services.access import (
    COMPOSE_PROJECT_LABEL,
    SWARM_NAMESPACE_LABEL,
    ResourceKey,
    ResourceRef,
    ResourceType,
    canonical_stack_key,
)
from app.services.authorisation import Authoriser
from app.services.compose_setup import ComposeStatus
from app.services.container_actions import BusyView
from app.services.containers import AccessPanel, short_id
from app.services.guards import require_administrator
from app.services.host_objects import (
    NetworkListTooLarge,
    VolumeListTooLarge,
    busy_view,
    read_services,
    row_id,
    service_mode,
)
from app.services.image_bindings import CredentialOffer
from app.services.list_order import ListOrder, Ordering, number_key, rank_key, text_key
from app.services.object_inspector import (
    SWARM_OBJECT_ID,
    EngineObjectInspector,
    EnvironmentUnavailable,
    HostClients,
    ObjectTooLarge,
    unavailable_from,
)
from app.services.object_locks import ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.pull_credentials import CredentialChoice
from app.services.stack_common import (
    changed_since_deploy,
    declared_service_keys,
    read_labelled_names,
    read_labelled_networks,
)
from app.services.stack_dismissal_views import (
    DISMISSIBLE_NAME,
    DismissalView,
    HostMembers,
    LeftoverObject,
    LeftoverVolume,
    applies,
    dismissal_view,
    leftover_objects,
    leftover_volumes,
    members_digest,
    swarm_members,
)
from app.services.stack_document import FILE_NAME, SWARM_STACK_NAME
from app.services.swarm_configs import AccessPanelReader, read_configs, read_secrets
from app.services.swarm_gate import require_swarm

if TYPE_CHECKING:
    from app.services.stack_views import CreateContext, FileView, VariableView
    from app.services.swarm_stack_plan import ImageMove, PlanLine, SwarmStackPlanner

__all__ = [
    "MAX_NAMESPACES",
    "NAMESPACE_ROUTE",
    "NamespaceDetail",
    "NamespacePage",
    "NamespaceRow",
    "StackAccessContext",
    "StackMember",
    "StackMembers",
    "SwarmFileForm",
    "SwarmFileRemoveConfirmation",
    "SwarmFilesPanel",
    "SwarmStackQueries",
    "members_digest",
    "namespace_keys",
    "read_volumes_by_namespace",
]

MAX_NAMESPACES: Final = 500

_status_place = rank_key(("active", "partial", "inactive", "error", "unknown"))
MAX_MEMBERS_SHOWN: Final = 500
MAX_QUERY: Final = 128
NAMESPACE_ROUTE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
_NAMESPACE_FILTER: Final = {"label": [SWARM_NAMESPACE_LABEL]}


@dataclass(frozen=True, slots=True)
class NamespaceRow:
    """One Swarm stack namespace; the counts are the objects the caller may see.

    `name` is the spelling most objects carry and `spellings` lists every spelling found.
    `linkable` is false for a name the detail route cannot address. A managed row carries the
    stack's status and last operation; `compose_stack` names a Compose stack whose control the
    namespace shares, and `adoptable` is true only for an administrator's unmanaged namespace
    with a name the stack grammar accepts and no Compose stack folding to it.

    An unmanaged row also says whether the namespace holds only leftovers: no service, counting
    those the caller may not see. The leftover tuples list at most 20 of the members the caller
    may see, and the counts above are their totals. `manager_volumes` are the namespace's
    volumes visible on the manager, which a dismissal does not name. `objects_digest` covers
    every config, secret and network, and `dismissible` is false for a name a dismissal cannot
    be stored under.
    """

    environment_id: int
    name: str
    key: str
    spellings: tuple[str, ...]
    linkable: bool
    services: int
    configs: int
    secrets: int
    networks: int
    managed: bool = False
    origin: str | None = None
    status: StackStatus | None = None
    last_operation: str | None = None
    last_outcome: str | None = None
    last_operation_at: str | None = None
    changed_since_deploy: bool = False
    compose_stack: str | None = None
    adoptable: bool = False
    leftovers_only: bool = False
    leftover_configs: tuple[LeftoverObject, ...] = ()
    leftover_secrets: tuple[LeftoverObject, ...] = ()
    leftover_networks: tuple[LeftoverObject, ...] = ()
    manager_volumes: tuple[LeftoverVolume, ...] = ()
    manager_volumes_total: int = 0
    objects_digest: str = ""
    dismissible: bool = False
    dismissal: DismissalView | None = None


LIST_ORDER: Final = ListOrder[NamespaceRow](
    default="name",
    keys={
        "name": text_key(lambda r: r.name),
        "state": number_key(lambda r: _status_place(r.status)),
        "last": text_key(lambda r: r.last_operation_at),
        "services": number_key(lambda r: r.services),
    },
    name=lambda r: r.name,
    row_id=lambda r: row_id(r.key),
    entry_id=lambda r: r.key,
)


@dataclass(frozen=True, slots=True)
class NamespacePage:
    """The namespaces the caller may see, at most 500 by name.

    `dismissed_hidden` counts the unmanaged namespaces a dismissal covers, whether they are
    listed (`show_dismissed`) or left out. `volumes_unavailable` is true when the manager's
    volumes could not be read for the leftovers-only rows.
    """

    environment_id: int
    environment_name: str
    rows: tuple[NamespaceRow, ...]
    total: int
    is_local: bool
    dismissed_hidden: int = 0
    show_dismissed: bool = False
    volumes_unavailable: bool = False

    @property
    def truncated(self) -> bool:
        """Report whether more namespaces matched than are listed."""
        return self.total > len(self.rows)


@dataclass(frozen=True, slots=True)
class StackMember:
    """One object of a namespace; `detail` is the scheduling mode of a service, the driver of
    a network, and empty for a config or secret."""

    id: str
    short_id: str
    name: str
    detail: str


@dataclass(frozen=True, slots=True)
class StackMembers:
    """The members of one kind the caller may see; `more` counts those cut by the display limit."""

    items: tuple[StackMember, ...]
    more: int


@dataclass(frozen=True, slots=True)
class NamespaceDetail:
    """One namespace with the members of each kind the caller may see.

    `dismissal` is filled for a namespace no Swarm stack manages; `manager_volumes` are listed
    only when it holds no service.
    """

    environment_id: int
    environment_name: str
    name: str
    key: str
    spellings: tuple[str, ...]
    services: StackMembers
    configs: StackMembers
    secrets: StackMembers
    networks: StackMembers
    can_manage_access: bool
    can_adopt: bool = False
    compose_stack: str | None = None
    leftovers_only: bool = False
    manager_volumes: tuple[LeftoverVolume, ...] = ()
    manager_volumes_total: int = 0
    volumes_unavailable: bool = False
    dismissal: DismissalView | None = None


@dataclass(frozen=True, slots=True)
class StackAccessContext:
    """What saving a stack's access control needs, read before the control changes.

    `members` are the references to refresh the control's display cache with afterwards.
    """

    panel: AccessPanel
    members: tuple[ResourceRef, ...]


@dataclass(frozen=True, slots=True)
class ManagedStack:
    """The stored facts of a managed Swarm stack."""

    stack_id: int
    name: str
    origin: str
    status: StackStatus
    last_operation: str | None
    last_outcome: str | None
    last_operation_at: str | None
    last_deployed_at: str | None
    changed_since_deploy: bool


@dataclass(frozen=True, slots=True)
class ManagedService:
    """A service of a managed stack as the overview shows it.

    `deployed` is false for a service the file declares and the host does not have yet;
    `in_file` is false for a service in the namespace that the file no longer declares. The
    counts are None when the host reports no task counts.
    """

    id: str
    short_id: str
    name: str
    mode: str
    running: int | None
    desired: int | None
    image: str
    in_file: bool
    deployed: bool


@dataclass(frozen=True, slots=True)
class ManagedStackDetail:
    """A managed Swarm stack's detail page, narrowed to what the caller may see.

    `members_available` is false when the host could not be read; the services then list only
    what the file declares. `hidden_members` counts members the caller may not see.
    """

    environment_id: int
    environment_name: str
    stack: ManagedStack
    compose_file: str = field(repr=False)
    compose_file_digest: str
    deployed_file_digest: str | None
    resolved_digest: str | None
    variables: tuple["VariableView", ...]
    services: tuple[ManagedService, ...]
    not_in_file: tuple[ManagedService, ...]
    networks: StackMembers
    configs: StackMembers
    secrets: StackMembers
    hidden_members: int
    members_available: bool
    can_write: bool
    can_manage: bool
    busy: BusyView | None
    compose: ComposeStatus
    files: tuple["FileView", ...] = ()


@dataclass(frozen=True, slots=True)
class SwarmFilesPanel:
    """The Files tab of a managed stack; `inputs_digest` is the empty text when it has none."""

    environment_id: int
    environment_name: str
    stack: ManagedStack
    inputs_digest: str
    files: tuple["FileView", ...]
    can_write: bool
    compose: ComposeStatus


@dataclass(frozen=True, slots=True)
class SwarmFileForm:
    """What the add page (`file` None) or the edit page of one file starts from."""

    environment_id: int
    environment_name: str
    stack: ManagedStack
    inputs_digest: str
    file: "FileView | None"
    compose: ComposeStatus


@dataclass(frozen=True, slots=True)
class SwarmFileRemoveConfirmation:
    """What removing a file names. `in_use` is true while a document references the file."""

    environment_id: int
    environment_name: str
    stack: str
    file: "FileView"
    expected_inputs_digest: str
    in_use: bool
    compose: ComposeStatus


@dataclass(frozen=True, slots=True)
class SwarmStackEdit:
    """What the editor starts from: the stored file and variable rows."""

    stack: ManagedStack
    compose_file: str = field(repr=False)
    compose_file_digest: str
    variables: tuple["VariableView", ...]
    environment_name: str
    compose: ComposeStatus


@dataclass(frozen=True, slots=True)
class SwarmRedeployConfirmation:
    """What a redeploy names before it runs; the digests bind the POST to what was shown.

    The POST carries `expected_file_digest`, `expected_inputs_digest`, `expected_plan` and
    `remove_orphans`. Every tuple is in plan order. `services_kept` are services in the namespace
    that the file no longer declares and the redeploy leaves running. `offers` describe the
    credential choice each image reference needs.
    """

    environment_id: int
    environment_name: str
    stack: str
    stack_id: int
    expected_file_digest: str
    expected_inputs_digest: str
    expected_plan: str
    remove_orphans: bool
    networks_create: tuple["PlanLine", ...]
    networks_remove: tuple["PlanLine", ...]
    configs_create: tuple["PlanLine", ...]
    configs_remove: tuple["PlanLine", ...]
    secrets_create: tuple["PlanLine", ...]
    secrets_remove: tuple["PlanLine", ...]
    services_create: tuple["PlanLine", ...]
    services_update: tuple["PlanLine", ...]
    services_remove: tuple["PlanLine", ...]
    services_kept: tuple["PlanLine", ...]
    objects_kept: tuple["PlanLine", ...]
    image_moves: tuple["ImageMove", ...]
    unpinned: tuple[str, ...]
    adopted_first_redeploy: bool
    changed_since_deploy: bool
    offers: tuple[CredentialOffer, ...]
    compose: ComposeStatus
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SwarmRemoveConfirmation:
    """What a remove names before it runs; volumes are never removed.

    `services`, `networks`, `configs` and `secrets` are the members the caller may see, and
    `hidden_members` counts the rest. `unwritable_members` counts members the caller may not
    change, which a standard user's remove is refused for. The POST carries `stack_id` and
    `expected_members`, the digest over every member id including hidden ones. `volumes_kept`
    are the stack's volumes visible on this host; volumes on other nodes remain there too.
    """

    environment_id: int
    environment_name: str
    stack: str
    stack_id: int
    services: StackMembers
    networks: StackMembers
    configs: StackMembers
    secrets: StackMembers
    hidden_members: int
    unwritable_members: int
    expected_members: str
    volumes_kept: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SwarmForgetConfirmation:
    """What forgetting a stack leaves behind: `members` is the objects on the host, 0 if unread."""

    environment_id: int
    environment_name: str
    stack: str
    stack_id: int
    members: int


@dataclass(frozen=True, slots=True)
class SwarmAdoptContext:
    """The unmanaged namespace an administrator may adopt, with what is on the host."""

    environment_id: int
    environment_name: str
    name: str
    spellings: tuple[str, ...]
    services: int
    configs: int
    secrets: int
    networks: int
    compose: ComposeStatus


@dataclass(slots=True)
class _Inventory:
    services: list[SwarmService]
    configs: list[SwarmConfig]
    secrets: list[SwarmSecret]
    networks: list[Network]


@dataclass(slots=True)
class _Group:
    spellings: Counter[str] = field(default_factory=Counter)
    services: list[SwarmService] = field(default_factory=list)
    configs: list[SwarmConfig] = field(default_factory=list)
    secrets: list[SwarmSecret] = field(default_factory=list)
    networks: list[Network] = field(default_factory=list)

    @property
    def name(self) -> str:
        return min(self.spellings, key=lambda spelling: (-self.spellings[spelling], spelling))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.spellings))


def _namespace_of(labels: dict[str, str]) -> str | None:
    value = labels.get(SWARM_NAMESPACE_LABEL)
    return value or None


async def _read_inventory(
    client: EngineClient, environment_id: int, poke: Callable[[int], None] | None
) -> _Inventory:
    services, configs, secrets, networks = await asyncio.gather(
        read_services(client, environment_id, poke, filters=_NAMESPACE_FILTER),
        read_configs(client, environment_id, poke, filters=_NAMESPACE_FILTER),
        read_secrets(client, environment_id, poke, filters=_NAMESPACE_FILTER),
        read_labelled_networks(client, SWARM_NAMESPACE_LABEL),
    )
    return _Inventory(services, configs, secrets, networks)


async def namespace_keys(
    client: EngineClient,
    environment_id: int,
    poke: Callable[[int], None] | None,
    *,
    compose_projects: bool = True,
) -> frozenset[str]:
    """Return the case-folded names in use as a namespace on this manager.

    A namespace is carried by a service, config, secret or network, or by a container or volume
    visible on the manager, which can outlive every service. With `compose_projects`, a
    container, volume or network under a Compose project label counts too. Raises the
    list-too-large and unavailable errors rather than a partial answer, since a missing name here
    lets a name collide with one that exists. Folded client-side because the Engine's label
    filter is case-sensitive.
    """
    grouped = _grouped(await _read_inventory(client, environment_id, poke))
    labels = [SWARM_NAMESPACE_LABEL, *([COMPOSE_PROJECT_LABEL] if compose_projects else [])]
    labelled = await read_labelled_names(
        client, labels, network_labels=[COMPOSE_PROJECT_LABEL] if compose_projects else []
    )
    return frozenset(grouped) | labelled


@dataclass(frozen=True, slots=True)
class NamespaceMembers:
    """Every service, config, secret and network that carries one namespace, unfiltered."""

    services: tuple[SwarmService, ...] = ()
    configs: tuple[SwarmConfig, ...] = ()
    secrets: tuple[SwarmSecret, ...] = ()
    networks: tuple[Network, ...] = ()
    spellings: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        """Return how many objects the namespace holds."""
        return len(self.services) + len(self.configs) + len(self.secrets) + len(self.networks)

    @property
    def ids(self) -> tuple[str, ...]:
        """Return the id of every member."""
        return (
            *(s.id for s in self.services),
            *(c.id for c in self.configs),
            *(s.id for s in self.secrets),
            *(n.id for n in self.networks),
        )

    def refs(self, environment_id: int) -> tuple[ResourceRef, ...]:
        """Return every member as a reference for the authoriser."""
        return tuple(
            _refs(
                environment_id,
                _Group(
                    services=list(self.services),
                    configs=list(self.configs),
                    secrets=list(self.secrets),
                    networks=list(self.networks),
                ),
            )
        )


async def read_namespace(
    client: EngineClient,
    environment_id: int,
    poke: Callable[[int], None] | None,
    key: str,
) -> NamespaceMembers:
    """Read the members of the namespace whose case-folded name is `key`, from the host now.

    Raises the list-too-large and unavailable errors rather than a partial answer.
    """
    group = _grouped(await _read_inventory(client, environment_id, poke)).get(key)
    if group is None:
        return NamespaceMembers()
    return NamespaceMembers(
        services=tuple(group.services),
        configs=tuple(group.configs),
        secrets=tuple(group.secrets),
        networks=tuple(group.networks),
        spellings=group.names,
    )


async def read_volumes_by_namespace(client: EngineClient) -> dict[str, tuple[Volume, ...]]:
    """Read the volumes visible to this host that carry a namespace, by folded name, in one call.

    Swarm creates a stack's volumes on the node a task runs on, so only the node answering is
    visible. VolumeListTooLarge and the unavailable errors rather than a partial answer.
    """
    try:
        volumes = await client.volumes.list(filters=_NAMESPACE_FILTER)
    except ResponseTooLarge:
        raise VolumeListTooLarge() from None
    except EngineError as error:
        raise unavailable_from(error) from None
    grouped: dict[str, list[Volume]] = {}
    for volume in volumes:
        if (value := _namespace_of(volume.labels)) is not None:
            grouped.setdefault(canonical_stack_key(value), []).append(volume)
    return {
        key: tuple(sorted(found, key=lambda volume: volume.name)) for key, found in grouped.items()
    }


async def read_namespace_volumes(client: EngineClient, key: str) -> tuple[Volume, ...]:
    """Read the volumes visible to this host that carry the namespace `key`, by name.

    The errors are those of `read_volumes_by_namespace`.
    """
    return (await read_volumes_by_namespace(client)).get(key, ())


def _held_by(group: _Group) -> HostMembers:
    return swarm_members(group.services, group.configs, group.secrets, group.networks)


def _grouped(inventory: _Inventory) -> dict[str, _Group]:
    groups: dict[str, _Group] = {}

    def group_of(labels: dict[str, str]) -> _Group | None:
        namespace = _namespace_of(labels)
        if namespace is None:
            return None
        group = groups.setdefault(canonical_stack_key(namespace), _Group())
        group.spellings[namespace] += 1
        return group

    for service in inventory.services:
        if (group := group_of(service.spec.labels)) is not None:
            group.services.append(service)
    for config in inventory.configs:
        if (group := group_of(config.labels)) is not None:
            group.configs.append(config)
    for secret in inventory.secrets:
        if (group := group_of(secret.labels)) is not None:
            group.secrets.append(secret)
    for network in inventory.networks:
        if (group := group_of(network.labels)) is not None:
            group.networks.append(network)
    return groups


class SwarmStackQueries:
    """Reads the Swarm stacks of a manager, managed and not; nothing here changes anything.

    A namespace exists while any service, config, secret or network carries its label. A standard
    user sees a namespace when its stack control, or the default for an uncontrolled one, admits
    the name, and the objects in it only through their own visibility. A managed stack is a row
    in the database that the namespace's name folds to; it is listed and addressable whether or
    not any object exists yet.
    """

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        hosts: HostClients,
        controls_read: AccessPanelReader,
        stacks: StackListRepository,
        rows: StackRepository,
        planner: "SwarmStackPlanner",
        locks: ObjectLockRegistry,
        dismissals: StackDismissalRepository,
        poke: Callable[[int], None] | None = None,
    ) -> None:
        self._dismissals = dismissals
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = hosts
        self._controls_read = controls_read
        self._listing = stacks
        self._rows = rows
        self._planner = planner
        self._locks = locks
        self._poke = poke

    async def page(
        self,
        actor: Principal,
        environment_id: int,
        *,
        q: str | None = None,
        show_dismissed: bool = False,
        sort: str | None = None,
        direction: str | None = None,
    ) -> NamespacePage:
        """List the managed stacks and namespaces the caller may see.

        ValidationFailed keyed `q`, `sort` or `dir`. A sort orders the one page of at most
        `MAX_NAMESPACES` entries, which is chosen by name.

        Managed and unmanaged entries merge by folded name. A dismissal hides its unmanaged
        namespace; with `show_dismissed` the list holds only the dismissed ones.
        ResourceNotFound unless the environment is a Swarm manager the caller may see;
        EnvironmentUnavailable and the list-too-large errors when the host cannot be read in full.
        """
        needle = (q or "").strip().casefold()
        if len(needle) > MAX_QUERY:
            raise ValidationFailed({"q": f"Search for at most {MAX_QUERY} characters."})
        ordering = Ordering.resolve(LIST_ORDER, sort, direction)
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        groups = await self._visible_groups(actor, environment_id)
        managed = await self._managed(actor, environment_id, needle)
        twins = await self._compose_twins(actor, environment_id)
        keys = {
            key
            for key, group in groups.items()
            if not needle or any(needle in name.casefold() for name in group.names)
        } | set(managed)
        stored = {
            held.project_key: held
            for held in await self._dismissals.dismissals(environment_id, "swarm")
        }
        covered = {
            key
            for key in keys - managed.keys()
            if (held := stored.get(key)) is not None and applies(held, _held_by(groups[key]))
        }
        shown = sorted(covered if show_dismissed else keys - covered)
        matched = [(key, groups.get(key) or _Group()) for key in shown][:MAX_NAMESPACES]
        volumes, volumes_unavailable = await self._manager_volumes(
            actor,
            environment_id,
            [key for key, group in matched if key not in managed and not group.services],
        )
        rows = await self._rows_of(actor, environment_id, matched, managed, twins, stored, volumes)
        if sort:
            rows = ordering.ordered(rows)
        return NamespacePage(
            environment_id=environment_id,
            environment_name=environment.name,
            rows=tuple(rows),
            total=len(shown),
            is_local=environment.connection_kind == "unix_socket",
            dismissed_hidden=len(covered),
            show_dismissed=show_dismissed,
            volumes_unavailable=volumes_unavailable,
        )

    async def origin_name(self, actor: Principal, environment_id: int, namespace: str) -> str:
        """Return the name of a Swarm namespace the caller may read; ResourceNotFound otherwise."""
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        return (await self._group(actor, environment_id, namespace, managed=True)).name

    async def detail(
        self, actor: Principal, environment_id: int, namespace: str
    ) -> NamespaceDetail:
        """Return one namespace; ResourceNotFound when it does not exist or is not visible."""
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        group = await self._group(actor, environment_id, namespace)
        visible = await self._members_of(actor, environment_id, group)
        key = canonical_stack_key(group.name)
        managed = await self._rows.get_folded(environment_id, group.name)
        twin = managed.name if managed is not None and managed.kind == "compose" else None
        unmanaged = managed is None or managed.kind != "swarm"
        held = _held_by(group)
        leftovers = unmanaged and not group.services
        volumes: Mapping[str, tuple[Volume, ...]] = {}
        volumes_unavailable = False
        if leftovers:
            volumes, volumes_unavailable = await self._manager_volumes(actor, environment_id, [key])
        manager_volumes = volumes.get(key, ())
        stored = (
            await self._dismissals.dismissal(environment_id, "swarm", key) if unmanaged else None
        )
        return NamespaceDetail(
            environment_id=environment_id,
            environment_name=environment.name,
            name=group.name,
            key=key,
            spellings=group.names,
            services=_members(
                [
                    StackMember(s.id, short_id(s.id), s.spec.name, _service_detail(s))
                    for s in visible.services
                ]
            ),
            configs=_members(
                [StackMember(c.id, short_id(c.id), c.name, "") for c in visible.configs]
            ),
            secrets=_members(
                [StackMember(s.id, short_id(s.id), s.name, "") for s in visible.secrets]
            ),
            networks=_members(
                [StackMember(n.id, short_id(n.id), n.name, n.driver) for n in visible.networks]
            ),
            can_manage_access=actor.is_administrator,
            can_adopt=actor.is_administrator
            and managed is None
            and SWARM_STACK_NAME.fullmatch(group.name) is not None,
            compose_stack=twin,
            leftovers_only=leftovers,
            manager_volumes=leftover_volumes(manager_volumes, MAX_MEMBERS_SHOWN),
            manager_volumes_total=len(manager_volumes),
            volumes_unavailable=volumes_unavailable,
            dismissal=dismissal_view(stored, held),
        )

    async def managed_detail(
        self, actor: Principal, environment_id: int, namespace: str, *, open_masks: bool = False
    ) -> ManagedStackDetail | None:
        """Return the detail of the managed stack this namespace names, or None if there is none.

        None means the page should show the read-only namespace. ResourceNotFound when a managed
        stack exists and the caller may not see it. Secret values are opened only when
        `open_masks` is set and the caller is an administrator, and then only to build masks.
        """
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        if NAMESPACE_ROUTE.fullmatch(namespace) is None:
            raise ResourceNotFound("not found")
        row = await self._rows.get_folded(environment_id, namespace)
        if row is None or row.kind != "swarm":
            return None
        stack = await self._planner.require_stack(actor, environment_id, namespace, "read")
        declared = await asyncio.to_thread(declared_service_keys, stack.compose_file)
        available = True
        members = _Group()
        try:
            members = (await self._read_groups(environment_id)).get(
                canonical_stack_key(stack.name), _Group()
            )
        except (EnvironmentUnavailable, NetworkListTooLarge, ObjectTooLarge):
            available = False
        visible = await self._members_of(actor, environment_id, members)
        hidden = (
            len(members.services)
            + len(members.configs)
            + len(members.secrets)
            + len(members.networks)
            - len(visible.services)
            - len(visible.configs)
            - len(visible.secrets)
            - len(visible.networks)
        )
        deployed = {service.spec.name: service for service in visible.services}
        hidden_names = {service.spec.name for service in members.services} - deployed.keys()
        in_file: list[ManagedService] = []
        for key in sorted(declared):
            name = f"{stack.name}_{key}"
            if name in hidden_names:
                continue
            found = deployed.pop(name, None)
            in_file.append(_managed_service(found, name, in_file=True))
        not_in_file = [
            _managed_service(service, name, in_file=False)
            for name, service in sorted(deployed.items())
        ]
        holder = self._locks.holder_of(_stack_lock(environment_id, stack.name))
        return ManagedStackDetail(
            environment_id=environment_id,
            environment_name=environment.name,
            stack=_managed_of(stack),
            compose_file=stack.compose_file,
            compose_file_digest=stack.compose_file_digest,
            deployed_file_digest=stack.deployed_file_digest,
            resolved_digest=stack.resolved_digest,
            variables=await self._variables(stack, actor, open_masks=open_masks),
            services=tuple(in_file),
            not_in_file=tuple(not_in_file),
            networks=_members(
                [StackMember(n.id, short_id(n.id), n.name, n.driver) for n in visible.networks]
            ),
            configs=_members(
                [StackMember(c.id, short_id(c.id), c.name, "") for c in visible.configs]
            ),
            secrets=_members(
                [StackMember(s.id, short_id(s.id), s.name, "") for s in visible.secrets]
            ),
            hidden_members=hidden,
            members_available=available,
            can_write=await self._may_write(actor, environment_id, stack.name),
            can_manage=actor.is_administrator,
            busy=None
            if holder is None
            else busy_view(ObjectBusy(holder), actor, self._locks.now()),
            compose=self._planner.compose_status(),
            files=await self._files(stack),
        )

    async def files_panel(
        self, actor: Principal, environment_id: int, namespace: str
    ) -> SwarmFilesPanel:
        """Return the Files tab: each file with its uses, plain content shown to any reader."""
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        stack = await self._planner.require_stack(actor, environment_id, namespace, "read")
        return SwarmFilesPanel(
            environment_id=environment_id,
            environment_name=environment.name,
            stack=_managed_of(stack),
            inputs_digest=stack.inputs_digest or "",
            files=await self._files(stack),
            can_write=await self._may_write(actor, environment_id, stack.name),
            compose=self._planner.compose_status(),
        )

    async def file_form(
        self, actor: Principal, environment_id: int, namespace: str, file: str | None
    ) -> SwarmFileForm:
        """Return what the add page (`file` None) or the edit page of one file starts from.

        Write holders only. ResourceNotFound for a name outside the grammar or no such file.
        """
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        stack = await self._planner.require_stack(actor, environment_id, namespace, "write")
        return SwarmFileForm(
            environment_id=environment_id,
            environment_name=environment.name,
            stack=_managed_of(stack),
            inputs_digest=stack.inputs_digest or "",
            file=None if file is None else await self._file(stack, file),
            compose=self._planner.compose_status(),
        )

    async def file_remove_confirmation(
        self, actor: Principal, environment_id: int, namespace: str, file: str
    ) -> SwarmFileRemoveConfirmation:
        """Name the file a remove deletes and where it is still used; write holders only."""
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        stack = await self._planner.require_stack(actor, environment_id, namespace, "write")
        view = await self._file(stack, file)
        return SwarmFileRemoveConfirmation(
            environment_id=environment_id,
            environment_name=environment.name,
            stack=stack.name,
            file=view,
            expected_inputs_digest=stack.inputs_digest or "",
            in_use=bool(view.uses),
            compose=self._planner.compose_status(),
        )

    async def create_context(self, actor: Principal, environment_id: int) -> "CreateContext":
        """Describe the create form; ResourceNotFound or PermissionDenied for a non-creator."""
        from app.services.stack_views import CreateContext

        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        await self._authoriser.require_create(actor, environment_id)
        status = self._planner.compose_status()
        return CreateContext(
            environment_id,
            environment.name,
            status.ready,
            status.message,
            status.required,
            status,
        )

    async def edit_context(
        self, actor: Principal, environment_id: int, namespace: str
    ) -> SwarmStackEdit:
        """Return what the editor starts from; write holders only."""
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        stack = await self._planner.require_stack(actor, environment_id, namespace, "write")
        return SwarmStackEdit(
            stack=_managed_of(stack),
            compose_file=stack.compose_file,
            compose_file_digest=stack.compose_file_digest,
            variables=await self._variables(stack, actor),
            environment_name=environment.name,
            compose=self._planner.compose_status(),
        )

    async def adopt_context(
        self, actor: Principal, environment_id: int, namespace: str
    ) -> SwarmAdoptContext:
        """Return the namespace to adopt; administrators only.

        ResourceNotFound when the host has no such namespace, a managed stack or a Compose stack
        already covers it, or its name is outside the stack grammar.
        """
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        require_administrator(actor)
        if NAMESPACE_ROUTE.fullmatch(namespace) is None:
            raise ResourceNotFound("not found")
        group = (await self._read_groups(environment_id)).get(canonical_stack_key(namespace))
        if (
            group is None
            or SWARM_STACK_NAME.fullmatch(group.name) is None
            or await self._rows.get_folded(environment_id, group.name) is not None
        ):
            raise ResourceNotFound("not found")
        return SwarmAdoptContext(
            environment_id=environment_id,
            environment_name=environment.name,
            name=group.name,
            spellings=group.names,
            services=len(group.services),
            configs=len(group.configs),
            secrets=len(group.secrets),
            networks=len(group.networks),
            compose=self._planner.compose_status(),
        )

    async def redeploy_confirmation(
        self,
        actor: Principal,
        environment_id: int,
        namespace: str,
        *,
        remove_orphans: bool = False,
        choices: Mapping[str, CredentialChoice] | None = None,
        check_collisions: bool = False,
    ) -> SwarmRedeployConfirmation:
        """Name every object a redeploy would create, replace or remove; write holders only.

        Resolves credentials and digests so moved images can be named, so it raises StackRejected
        (`credential_choice_required` and the like) when a choice is still needed, and the
        refusals a redeploy itself would meet. `offers` carries the credential choices. With
        `check_collisions` a published port that another service holds raises StackRejected, and
        the rest, including ports on the manager's containers, are `warnings`.
        """
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        stack = await self._planner.require_stack(actor, environment_id, namespace, "write")
        plan = await self._planner.deploy_plan(
            actor,
            environment_id,
            stack,
            remove_orphans=remove_orphans,
            choices=choices,
            resolve_images=True,
            check_collisions=check_collisions,
        )
        offers = await self._planner.credential_pickers(actor, environment_id, list(plan.pinned))
        return SwarmRedeployConfirmation(
            environment_id=environment_id,
            environment_name=environment.name,
            stack=stack.name,
            stack_id=stack.id,
            expected_file_digest=stack.compose_file_digest,
            expected_inputs_digest=stack.inputs_digest or "",
            expected_plan=plan.digest,
            remove_orphans=remove_orphans,
            networks_create=plan.with_action("create", "network"),
            networks_remove=plan.with_action("remove", "network"),
            configs_create=plan.with_action("create", "config"),
            configs_remove=plan.with_action("remove", "config"),
            secrets_create=plan.with_action("create", "secret"),
            secrets_remove=plan.with_action("remove", "secret"),
            services_create=plan.with_action("create", "service"),
            services_update=plan.with_action("update", "service"),
            services_remove=plan.with_action("remove", "service"),
            services_kept=plan.with_action("keep", "service"),
            objects_kept=tuple(
                line
                for line in plan.lines
                if line.action == "keep" and line.detail and line.kind != "service"
            ),
            image_moves=plan.image_moves,
            unpinned=plan.unpinned,
            adopted_first_redeploy=plan.adopted_first_redeploy,
            changed_since_deploy=changed_since_deploy(
                stack.compose_file_digest,
                stack.deployed_file_digest,
                stack.inputs_digest,
                stack.deployed_inputs_digest,
            ),
            offers=offers,
            compose=self._planner.compose_status(),
            warnings=plan.warnings,
        )

    async def remove_confirmation(
        self, actor: Principal, environment_id: int, namespace: str
    ) -> SwarmRemoveConfirmation:
        """Name every service, network, config and secret the remove takes away; write holders.

        A standard user sees the members they may see by name and the rest counted.
        """
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        stack = await self._planner.require_stack(actor, environment_id, namespace, "write")
        client = await self._hosts.client(environment_id)
        key = canonical_stack_key(stack.name)
        members = await read_namespace(client, environment_id, self._poke, key)
        group = _Group(
            services=list(members.services),
            configs=list(members.configs),
            secrets=list(members.secrets),
            networks=list(members.networks),
        )
        visible = await self._members_of(actor, environment_id, group)
        refs = members.refs(environment_id)
        unwritable = 0
        if not actor.is_administrator:
            writable = await self._authoriser.filter_visible(actor, refs, "write")
            unwritable = len(refs) - len(writable)
        volumes = await read_namespace_volumes(client, key)
        shown = (
            len(visible.services)
            + len(visible.networks)
            + len(visible.configs)
            + len(visible.secrets)
        )
        return SwarmRemoveConfirmation(
            environment_id=environment_id,
            environment_name=environment.name,
            stack=stack.name,
            stack_id=stack.id,
            services=_members(
                [
                    StackMember(s.id, short_id(s.id), s.spec.name, _service_detail(s))
                    for s in visible.services
                ]
            ),
            networks=_members(
                [StackMember(n.id, short_id(n.id), n.name, n.driver) for n in visible.networks]
            ),
            configs=_members(
                [StackMember(c.id, short_id(c.id), c.name, "") for c in visible.configs]
            ),
            secrets=_members(
                [StackMember(s.id, short_id(s.id), s.name, "") for s in visible.secrets]
            ),
            hidden_members=members.count - shown,
            unwritable_members=unwritable,
            expected_members=members_digest(members.ids),
            volumes_kept=tuple(volume.name for volume in volumes),
        )

    async def forget_confirmation(
        self, actor: Principal, environment_id: int, namespace: str
    ) -> SwarmForgetConfirmation:
        """Describe what forgetting leaves behind; administrators only."""
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        stack = await self._planner.require_stack(actor, environment_id, namespace, "write")
        require_administrator(actor)
        count = 0
        try:
            client = await self._hosts.client(environment_id)
            count = (
                await read_namespace(
                    client, environment_id, self._poke, canonical_stack_key(stack.name)
                )
            ).count
        except (EnvironmentUnavailable, NetworkListTooLarge, ObjectTooLarge):
            count = 0
        return SwarmForgetConfirmation(
            environment_id, environment.name, stack.name, stack.id, count
        )

    async def access_panel(
        self, actor: Principal, environment_id: int, namespace: str
    ) -> AccessPanel:
        """Return the content of the access tab for the stack control; administrators only.

        Works for a managed stack whose objects do not exist yet.
        """
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        group = await self._group(actor, environment_id, namespace, managed=True)
        if not actor.is_administrator:
            raise PermissionDenied("administrator role required")
        return await self._controls_read.panel(
            actor, ResourceKey(environment_id, "stack", group.name), group.name, ""
        )

    async def member_refs(
        self, actor: Principal, environment_id: int, namespace: str
    ) -> tuple[ResourceRef, ...]:
        """Return every member of the namespace as a reference; administrators only.

        For refreshing the stack control's display cache after it changes.
        """
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        group = await self._group(actor, environment_id, namespace, managed=True)
        if not actor.is_administrator:
            raise PermissionDenied("administrator role required")
        return tuple(_refs(environment_id, group))

    async def access_context(
        self, actor: Principal, environment_id: int, namespace: str
    ) -> StackAccessContext:
        """Read the access panel and the member references together; administrators only.

        A save reads this before it writes the control, so a failed read leaves the control as it
        was.
        """
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        group = await self._group(actor, environment_id, namespace, managed=True)
        if not actor.is_administrator:
            raise PermissionDenied("administrator role required")
        panel = await self._controls_read.panel(
            actor, ResourceKey(environment_id, "stack", group.name), group.name, ""
        )
        return StackAccessContext(panel, tuple(_refs(environment_id, group)))

    async def _group(
        self, actor: Principal, environment_id: int, namespace: str, *, managed: bool = False
    ) -> _Group:
        """Return the namespace's group after the read check.

        With `managed` a stack row stands in for a namespace that has no objects, named as the
        row spells it.
        """
        if NAMESPACE_ROUTE.fullmatch(namespace) is None:
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "stack", namespace), "read"
        )
        groups = await self._read_groups(environment_id)
        group = groups.get(canonical_stack_key(namespace))
        if group is not None:
            return group
        if managed:
            row = await self._rows.get_folded(environment_id, namespace)
            if row is not None and row.kind == "swarm":
                return _Group(spellings=Counter({row.name: 1}))
        raise ResourceNotFound("not found")

    async def _read_groups(self, environment_id: int) -> dict[str, _Group]:
        client = await self._hosts.client(environment_id)
        return _grouped(await _read_inventory(client, environment_id, self._poke))

    async def _visible_groups(self, actor: Principal, environment_id: int) -> dict[str, _Group]:
        groups = await self._read_groups(environment_id)
        if actor.is_administrator:
            return groups
        visibility = await self._authoriser.resolve_visibility(actor, environment_id, "stack")
        return {
            key: group
            for key, group in groups.items()
            if visibility.permits(group.name, {}, "read")
        }

    async def _managed(
        self, actor: Principal, environment_id: int, needle: str
    ) -> dict[str, StackSummary]:
        """Return the managed Swarm stacks the caller may see, by folded name."""
        visible = None
        if not actor.is_administrator:
            visibility = await self._authoriser.resolve_visibility(actor, environment_id, "stack")
            visible = visibility.sql_filter("s.name", "s.environment_id")
        page = await self._listing.page(
            environment_id,
            limit=MAX_NAMESPACES,
            name_contains=needle or None,
            visible=visible,
            kind="swarm",
        )
        return {canonical_stack_key(item.name): item for item in page.items}

    async def _manager_volumes(
        self, actor: Principal, environment_id: int, keys: Sequence[str]
    ) -> tuple[dict[str, tuple[Volume, ...]], bool]:
        """Return the manager's volumes of the namespaces, narrowed to what the caller may see.

        One list call however many namespaces; the flag is true when it could not be read.
        """
        if not keys:
            return {}, False
        try:
            found = await read_volumes_by_namespace(await self._hosts.client(environment_id))
        except (EnvironmentUnavailable, ObjectTooLarge):
            return {}, True
        wanted = {key: found[key] for key in keys if key in found}
        if actor.is_administrator:
            return wanted, False
        refs = [
            ResourceRef(environment_id, "volume", volume.name, volume.labels)
            for volumes in wanted.values()
            for volume in volumes
        ]
        permitted = {ref.key for ref in await self._authoriser.filter_visible(actor, refs)}
        return {
            key: tuple(
                volume
                for volume in volumes
                if ResourceKey(environment_id, "volume", volume.name) in permitted
            )
            for key, volumes in wanted.items()
        }, False

    async def _compose_twins(self, actor: Principal, environment_id: int) -> dict[str, str]:
        """Return the Compose stacks by folded name; only an administrator is shown them."""
        if not actor.is_administrator:
            return {}
        page = await self._listing.page(environment_id, limit=MAX_NAMESPACES, kind="compose")
        return {canonical_stack_key(item.name): item.name for item in page.items}

    async def _files(self, stack: StoredStack) -> tuple["FileView", ...]:
        from app.services.stack_file_views import file_views

        return await file_views(self._rows.files, stack)

    async def _file(self, stack: StoredStack, file: str) -> "FileView":
        if FILE_NAME.fullmatch(file) is None:
            raise ResourceNotFound("not found")
        found = next((view for view in await self._files(stack) if view.name == file), None)
        if found is None:
            raise ResourceNotFound("not found")
        return found

    async def _variables(
        self, stack: StoredStack, actor: Principal, *, open_masks: bool = False
    ) -> tuple["VariableView", ...]:
        from app.services.stack_views import variable_views

        return await variable_views(self._rows, stack.id, actor, open_masks=open_masks)

    async def _may_write(self, actor: Principal, environment_id: int, name: str) -> bool:
        if actor.is_administrator:
            return True
        try:
            await self._authoriser.require_access(
                actor, ResourceKey(environment_id, "stack", name), "write"
            )
        except (ResourceNotFound, PermissionDenied):
            return False
        return True

    async def _members_of(self, actor: Principal, environment_id: int, group: _Group) -> _Group:
        """Narrow a group to the members the caller may see."""
        if actor.is_administrator:
            return group
        return _narrowed(
            group, environment_id, await self._permitted(actor, environment_id, [group])
        )

    async def _permitted(
        self, actor: Principal, environment_id: int, groups: Sequence[_Group]
    ) -> set[ResourceKey]:
        """Return the keys of the members of every group the caller may see, in one check."""
        refs = [ref for group in groups for ref in _refs(environment_id, group)]
        return {ref.key for ref in await self._authoriser.filter_visible(actor, refs)}

    async def _rows_of(
        self,
        actor: Principal,
        environment_id: int,
        matched: Sequence[tuple[str, _Group]],
        managed: Mapping[str, StackSummary],
        twins: Mapping[str, str],
        stored: Mapping[str, StoredDismissal],
        volumes: Mapping[str, tuple[Volume, ...]],
    ) -> list[NamespaceRow]:
        if actor.is_administrator:
            narrowed = [group for _, group in matched]
        else:
            permitted = await self._permitted(actor, environment_id, [g for _, g in matched])
            narrowed = [_narrowed(group, environment_id, permitted) for _, group in matched]
        rows: list[NamespaceRow] = []
        for (key, group), visible in zip(matched, narrowed, strict=True):
            held = managed.get(key)
            name = held.name if held is not None else group.name
            twin = twins.get(key)
            members = _held_by(group)
            manager_volumes = volumes.get(key, ())
            rows.append(
                NamespaceRow(
                    environment_id=environment_id,
                    name=name,
                    key=key,
                    spellings=group.names or (name,),
                    linkable=NAMESPACE_ROUTE.fullmatch(name) is not None,
                    services=len(visible.services),
                    configs=len(visible.configs),
                    secrets=len(visible.secrets),
                    networks=len(visible.networks),
                    managed=held is not None,
                    origin=held.origin if held is not None else None,
                    status=held.status if held is not None else None,
                    last_operation=held.last_operation if held is not None else None,
                    last_outcome=held.last_outcome if held is not None else None,
                    last_operation_at=held.last_operation_at if held is not None else None,
                    changed_since_deploy=held is not None
                    and changed_since_deploy(
                        held.compose_file_digest,
                        held.deployed_file_digest,
                        held.inputs_digest,
                        held.deployed_inputs_digest,
                    ),
                    compose_stack=twin,
                    adoptable=actor.is_administrator
                    and held is None
                    and twin is None
                    and SWARM_STACK_NAME.fullmatch(name) is not None,
                    leftovers_only=held is None and not group.services,
                    leftover_configs=(
                        leftover_objects(visible.configs, SWARM_OBJECT_ID) if held is None else ()
                    ),
                    leftover_secrets=(
                        leftover_objects(visible.secrets, SWARM_OBJECT_ID) if held is None else ()
                    ),
                    leftover_networks=leftover_objects(visible.networks) if held is None else (),
                    manager_volumes=leftover_volumes(manager_volumes),
                    manager_volumes_total=len(manager_volumes),
                    objects_digest=members_digest(members.tokens) if held is None else "",
                    dismissible=held is None and DISMISSIBLE_NAME.fullmatch(name) is not None,
                    dismissal=dismissal_view(stored.get(key), members) if held is None else None,
                )
            )
        return rows


def _narrowed(group: _Group, environment_id: int, permitted: set[ResourceKey]) -> _Group:
    def allowed(kind: ResourceType, object_id: str) -> bool:
        return ResourceKey(environment_id, kind, object_id) in permitted

    return _Group(
        spellings=group.spellings,
        services=[s for s in group.services if allowed("service", s.id)],
        configs=[c for c in group.configs if allowed("config", c.id)],
        secrets=[s for s in group.secrets if allowed("secret", s.id)],
        networks=[n for n in group.networks if allowed("network", n.id)],
    )


def _refs(environment_id: int, group: _Group) -> list[ResourceRef]:
    return [
        *(ResourceRef(environment_id, "service", s.id, s.spec.labels) for s in group.services),
        *(ResourceRef(environment_id, "config", c.id, c.labels) for c in group.configs),
        *(ResourceRef(environment_id, "secret", s.id, s.labels) for s in group.secrets),
        *(ResourceRef(environment_id, "network", n.id, n.labels) for n in group.networks),
    ]


def _members(items: list[StackMember]) -> StackMembers:
    ordered = sorted(items, key=lambda member: (member.name.casefold(), member.id))
    shown = ordered[:MAX_MEMBERS_SHOWN]
    return StackMembers(tuple(shown), len(ordered) - len(shown))


def _service_detail(service: SwarmService) -> str:
    mode = service_mode(service)
    status = service.service_status
    if status is None or not mode:
        return mode
    return f"{mode}, {status.running_tasks} of {status.desired_tasks} running"


def _stack_lock(environment_id: int, name: str) -> ObjectKey:
    return ObjectKey(environment_id, "stack", canonical_stack_key(name))


def _managed_of(item: StackSummary | StoredStack) -> ManagedStack:
    return ManagedStack(
        stack_id=item.id,
        name=item.name,
        origin=item.origin,
        status=item.status,
        last_operation=item.last_operation,
        last_outcome=item.last_outcome,
        last_operation_at=item.last_operation_at,
        last_deployed_at=item.last_deployed_at,
        changed_since_deploy=changed_since_deploy(
            item.compose_file_digest,
            item.deployed_file_digest,
            item.inputs_digest,
            item.deployed_inputs_digest,
        ),
    )


def _managed_service(service: SwarmService | None, name: str, *, in_file: bool) -> ManagedService:
    if service is None:
        return ManagedService("", "", name, "", None, None, "", in_file, False)
    status = service.service_status
    container = service.spec.task_template.get("ContainerSpec", {})
    image = container.get("Image") if isinstance(container, dict) else None
    return ManagedService(
        id=service.id,
        short_id=short_id(service.id),
        name=service.spec.name,
        mode=service_mode(service),
        running=None if status is None else status.running_tasks,
        desired=None if status is None else status.desired_tasks,
        image=image if isinstance(image, str) else "",
        in_file=in_file,
        deployed=True,
    )
