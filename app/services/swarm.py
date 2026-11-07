"""Swarm on a manager environment: the overview, nodes, tasks, and node availability changes."""

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Final, Literal

import anyio

from app.auth.errors import (
    SELF_PROTECTED_REASON,
    ProductError,
    ResourceNotFound,
    SelfProtected,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.db.engine import Database
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.db.repositories.environment import Environment
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import (
    EngineError,
    EnvironmentUnreachable,
    NotFound,
    RequestOutcomeUnknown,
    ResponseTooLarge,
)
from app.docker_engine.models import SwarmNode, SwarmService, SwarmTask
from app.logging import DEFAULT_SCRUBBER, get_logger
from app.self_identity import SelfIdentitySource, no_identity
from app.services.access import ResourceKey, ResourceRef, is_canonical_id
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.container_actions import engine_detail
from app.services.containers import LabelView, parse_engine_time
from app.services.guards import require_administrator
from app.services.host_objects import (
    audited_client,
    failure_reason,
    hold_all,
    label_views,
    permitted_container_ids,
    read_containers,
    run_shielded,
    settled,
    write_settled_row,
)
from app.services.log_lines import sanitise
from app.services.object_inspector import (
    SWARM_OBJECT_ID,
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
    unavailable_from,
)
from app.services.object_locks import LockHolder, ObjectKey, ObjectLockRegistry
from app.services.swarm_configs import read_configs, read_secrets
from app.services.swarm_gate import (
    SWARM_UNAVAILABLE_STATUS,
    require_swarm,
    swarm_unavailable_from,
)

logger = get_logger(__name__)

__all__ = [
    "AvailabilityImpact",
    "NodeChanged",
    "NodeChangedSinceReview",
    "NodeDetail",
    "NodeGone",
    "NodeList",
    "NodeRefused",
    "NodeRow",
    "NodeService",
    "NodeTask",
    "SwarmListTooLarge",
    "SwarmOverview",
    "SwarmQueryService",
    "TaskDetail",
    "TaskGroup",
    "TaskTable",
    "TaskView",
    "audited_swarm_read",
    "build_task_table",
    "engine_failure",
    "label_safe_hostname",
    "link_candidates",
    "linkable_tasks",
    "node_hostnames",
    "swarm_read",
    "task_message",
    "task_view",
]

MAX_NAME: Final = 255
MAX_TASK_ROWS: Final = 500
MAX_NODE_TASKS: Final = 500
MAX_MESSAGE_CHARS: Final = 512
MAX_SERVICES_NAMED: Final = 50
SETTLE_SECONDS: Final = 120.0
AVAILABILITIES: Final = ("active", "pause", "drain")
CHANGED_SINCE_REVIEW: Final = "This node changed since you reviewed it; nothing was changed."
TERMINAL_STATES: Final = frozenset(
    {"complete", "shutdown", "failed", "rejected", "remove", "orphaned"}
)

_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_HOSTNAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_NO_SLOT = 2**62


class SwarmListTooLarge(ObjectTooLarge):
    """A Swarm list is larger than the read limit; nothing partial is used."""

    def __init__(self) -> None:
        super().__init__("This host reports more Swarm data than can be listed here.")


class NodeGone(ProductError):
    """The node vanished between authorisation and the Engine call."""

    def __init__(self) -> None:
        super().__init__("The node no longer exists.")


class NodeRefused(ProductError):
    """Docker rejected a node change; the Engine's text is not part of the message.

    engine_detail is that text, scrubbed and cut, for a page shown to an administrator.
    """

    def __init__(self, engine_detail: str | None = None) -> None:
        super().__init__("Docker refused the change to this node.")
        self.engine_detail = engine_detail


class NodeChangedSinceReview(ValidationFailed):
    """The node is no longer the one the caller reviewed; nothing was changed."""

    def __init__(self) -> None:
        super().__init__({"node": CHANGED_SINCE_REVIEW})


@dataclass(frozen=True, slots=True)
class TaskView:
    """One task as shown; the messages are the Engine's own text after cleaning and cutting."""

    id: str
    short_id: str
    service_id: str
    slot: int | None
    node_id: str | None
    node_hostname: str | None
    desired_state: str
    state: str
    message: str
    error: str
    container_id: str | None
    exit_code: int | None
    container_link: bool
    created_at: datetime | None
    updated_at: datetime | None
    version: int
    settling: bool


@dataclass(frozen=True, slots=True)
class TaskGroup:
    """Tasks of one slot (replicated services) or one node (global ones), newest first."""

    label: str
    tasks: tuple[TaskView, ...]


@dataclass(frozen=True, slots=True)
class TaskTable:
    """The tasks of a service; polling is true while any of them is still moving.

    `environment_id` and `service_id` name the service the table belongs to, so a panel that
    shows it needs nothing else.
    """

    groups: tuple[TaskGroup, ...]
    shown: int
    total: int
    truncated: bool
    polling: bool
    environment_id: int = 0
    service_id: str = ""


@dataclass(frozen=True, slots=True)
class TaskDetail:
    """One task with the service it belongs to."""

    environment_id: int
    environment_name: str
    service_id: str
    service_name: str
    task: TaskView
    can_view_node: bool


@dataclass(frozen=True, slots=True)
class SwarmOverview:
    """The cluster at a glance; the cluster, node and manager fields are for administrators.

    `configs` and `secrets` count the objects the caller may see, None when the host's list could
    not be read.
    """

    environment_id: int
    environment_name: str
    is_local: bool
    administrator: bool
    cluster_id: str | None
    created_at: datetime | None
    manager_node_id: str | None
    nodes: int | None
    managers: int | None
    nodes_ready: int | None
    nodes_active: int | None
    nodes_paused: int | None
    nodes_drained: int | None
    services: int
    tasks_running: int
    tasks_desired: int
    can_create: bool
    configs: int | None = None
    secrets: int | None = None


@dataclass(frozen=True, slots=True)
class NodeRow:
    """One node in the list."""

    id: str
    short_id: str
    hostname: str
    role: str
    availability: str
    state: str
    reachability: str | None
    leader: bool
    engine_version: str
    address: str
    is_this_manager: bool


@dataclass(frozen=True, slots=True)
class NodeList:
    """Every node of the cluster."""

    environment_id: int
    environment_name: str
    rows: tuple[NodeRow, ...]
    manager_node_id: str | None


@dataclass(frozen=True, slots=True)
class NodeTask:
    """A task placed on a node, named by its service."""

    id: str
    short_id: str
    service_id: str
    service_name: str
    slot: int | None
    state: str
    desired_state: str


@dataclass(frozen=True, slots=True)
class NodeDetail:
    """One node with its platform, resources, labels and the tasks placed on it."""

    environment_id: int
    environment_name: str
    row: NodeRow
    version: int
    architecture: str
    os: str
    cpus: str
    memory_bytes: int
    labels: tuple[LabelView, ...]
    engine_labels: tuple[LabelView, ...]
    tasks: tuple[NodeTask, ...]
    tasks_more: int
    can_change: bool


@dataclass(frozen=True, slots=True)
class ServiceTasks:
    """Running tasks of one service on a node."""

    service_id: str
    service_name: str
    tasks: int


@dataclass(frozen=True, slots=True)
class AvailabilityImpact:
    """What a change of availability does, for the confirmation page."""

    environment_id: int
    environment_name: str
    node_id: str
    short_id: str
    hostname: str
    role: str
    current: str
    requested: str
    version: int
    services: tuple[ServiceTasks, ...]
    services_more: int
    tasks_running: int
    other_active_nodes: int
    stays_pending: bool


@dataclass(frozen=True, slots=True)
class NodeChanged:
    """The outcome of an availability change."""

    environment_id: int
    node_id: str
    hostname: str
    availability_before: str
    availability_after: str
    tasks_running: int
    unchanged: bool


def task_message(text: str) -> str:
    """Clean an Engine task message for display: control characters, secrets, and length."""
    cleaned = DEFAULT_SCRUBBER.scrub_text(sanitise(text))
    if len(cleaned) <= MAX_MESSAGE_CHARS:
        return cleaned
    return f"{cleaned[:MAX_MESSAGE_CHARS]}…"


def _hostname_text(hostname: str) -> str:
    return sanitise(hostname)[:MAX_NAME]


def label_safe_hostname(hostname: str) -> str:
    """Return a hostname reduced to characters that survive a space-separated log line."""
    return _HOSTNAME_UNSAFE.sub("_", hostname)[:MAX_NAME] or "-"


async def swarm_read[T](call: Awaitable[T], environment_id: int, poke: Callable[[int], None]) -> T:
    """Await a Swarm read, mapping what can go wrong to product errors.

    ResourceNotFound when the Engine has no such object, SwarmListTooLarge past a size limit,
    and EnvironmentUnavailable (SwarmUnavailable for a 503) for every other Engine failure.
    """
    try:
        return await call
    except NotFound:
        raise ResourceNotFound("not found") from None
    except ResponseTooLarge:
        raise SwarmListTooLarge() from None
    except EngineError as error:
        raise swarm_unavailable_from(error, environment_id, poke) from None


async def audited_swarm_read[T](
    call: Awaitable[T], environment_id: int, poke: Callable[[int], None]
) -> T:
    """Await a Swarm read inside an audit scope, naming the reason on the scope if it fails."""
    try:
        return await swarm_read(call, environment_id, poke)
    except EnvironmentUnavailable as error:
        current_audit().reason = error.kind
        raise
    except ObjectTooLarge:
        current_audit().reason = "too_large"
        raise


def engine_failure(
    error: EngineError, environment_id: int, poke: Callable[[int], None]
) -> tuple[str, ProductError] | None:
    """Classify the Engine failures every Swarm write shares; None leaves the rest to the caller.

    The first value is the failure reason for the activity row.
    """
    if isinstance(error, RequestOutcomeUnknown):
        return "outcome_unknown", EnvironmentUnavailable("outcome_unknown")
    if isinstance(error, EnvironmentUnreachable):
        return "unreachable", EnvironmentUnavailable("unreachable")
    if error.status_code == SWARM_UNAVAILABLE_STATUS:
        return "not_swarm_manager", swarm_unavailable_from(error, environment_id, poke)
    return None


def node_hostnames(nodes: Sequence[SwarmNode]) -> dict[str, str]:
    """Map node ids to their hostnames, cleaned for display."""
    return {node.id: _hostname_text(node.description.hostname) or node.id[:12] for node in nodes}


def _state_of(task: SwarmTask) -> str:
    return task.status.state


def _exit_code(task: SwarmTask) -> int | None:
    status = task.status.container_status
    if status is None or task.status.state not in TERMINAL_STATES:
        return None
    return status.exit_code


def _container_of(task: SwarmTask) -> str | None:
    status = task.status.container_status
    if status is None or _CONTAINER_ID.fullmatch(status.container_id) is None:
        return None
    return status.container_id


def link_candidates(tasks: Sequence[SwarmTask], manager_node_id: str | None) -> dict[str, str]:
    """Map container ids to task ids for the tasks that ran on the connected manager."""
    if manager_node_id is None:
        return {}
    found: dict[str, str] = {}
    for task in tasks:
        container = _container_of(task)
        if container is not None and task.node_id == manager_node_id:
            found[container] = task.id
    return found


async def linkable_tasks(
    authoriser: Authoriser,
    actor: Principal,
    environment_id: int,
    client: EngineClient,
    candidates: Mapping[str, str],
) -> frozenset[str]:
    """Return the ids of the tasks whose container exists and the caller may read."""
    if not candidates:
        return frozenset()
    try:
        containers = await read_containers(client)
    except ProductError:
        return frozenset()
    known = [c for c in containers if c.id in candidates]
    permitted = await permitted_container_ids(authoriser, actor, environment_id, known)
    return frozenset(candidates[cid] for cid in permitted)


def task_view(
    task: SwarmTask,
    hostnames: Mapping[str, str],
    linkable: frozenset[str],
) -> TaskView:
    """Build the view of one task."""
    state = _state_of(task)
    return TaskView(
        id=task.id,
        short_id=task.id[:12],
        service_id=task.service_id,
        slot=task.slot,
        node_id=task.node_id or None,
        node_hostname=hostnames.get(task.node_id) if task.node_id else None,
        desired_state=task.desired_state,
        state=state,
        message=task_message(task.status.message),
        error=task_message(task.status.err),
        container_id=_container_of(task),
        exit_code=_exit_code(task),
        container_link=task.id in linkable,
        created_at=parse_engine_time(task.created_at),
        updated_at=parse_engine_time(task.updated_at),
        version=task.version.index,
        settling=state not in TERMINAL_STATES and state != task.desired_state,
    )


def build_task_table(
    tasks: Sequence[SwarmTask],
    *,
    mode: str | None,
    hostnames: Mapping[str, str],
    linkable: frozenset[str],
    environment_id: int = 0,
    service_id: str = "",
) -> TaskTable:
    """Group tasks by slot (replicated) or node (global), newest first, at most 500 shown."""
    kept = sorted(tasks, key=lambda task: task.version.index, reverse=True)[:MAX_TASK_ROWS]
    groups: dict[tuple[int, str], tuple[str, list[TaskView]]] = {}
    for task in kept:
        view = task_view(task, hostnames, linkable)
        if mode == "global":
            name = view.node_hostname or "Unassigned"
            key, label = (0, name.casefold()), name
        elif task.slot is not None:
            key, label = (task.slot, ""), f"Slot {task.slot}"
        else:
            key, label = (_NO_SLOT, ""), "No slot"
        groups.setdefault(key, (label, []))[1].append(view)
    built = tuple(
        TaskGroup(label, tuple(views))
        for _, (label, views) in sorted(groups.items(), key=lambda item: item[0])
    )
    return TaskTable(
        groups=built,
        shown=len(kept),
        total=len(tasks),
        truncated=len(tasks) > len(kept),
        polling=any(view.settling for group in built for view in group.tasks),
        environment_id=environment_id,
        service_id=service_id,
    )


def _service_ref(environment_id: int, service: SwarmService) -> ResourceRef:
    return ResourceRef(environment_id, "service", service.id, service.spec.labels)


def _node_row(node: SwarmNode, manager_node_id: str | None) -> NodeRow:
    manager = node.manager_status
    return NodeRow(
        id=node.id,
        short_id=node.id[:12],
        hostname=_hostname_text(node.description.hostname) or node.id[:12],
        role=node.spec.role,
        availability=node.spec.availability,
        state=node.status.state,
        reachability=None if manager is None else manager.reachability,
        leader=manager is not None and manager.leader,
        engine_version=node.description.engine_version[:64],
        address=(manager.addr if manager is not None and manager.addr else node.status.addr)[:255],
        is_this_manager=node.id == manager_node_id,
    )


def _node_argument(arguments: Arguments) -> str:
    return str(arguments["node_id"])[:25]


class SwarmQueryService:
    """Reads the overview, nodes and tasks of a Swarm manager through the authoriser."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        poke: Callable[[int], None],
    ) -> None:
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._poke = poke

    async def overview(self, actor: Principal, environment_id: int) -> SwarmOverview:
        """Describe the cluster; a standard user sees only their visible services' counts.

        ResourceNotFound unless the environment is a Swarm manager the caller can see;
        SwarmUnavailable and EnvironmentUnavailable when the host cannot be read.
        """
        environment = await self._gate(actor, environment_id)
        client = await self._hosts.client(environment_id)
        services = await swarm_read(client.swarm_services.list(), environment_id, self._poke)
        if not actor.is_administrator:
            refs = [_service_ref(environment_id, s) for s in services]
            allowed = {r.resource_id for r in await self._authoriser.filter_visible(actor, refs)}
            services = [s for s in services if s.id in allowed]
        running = sum(s.service_status.running_tasks for s in services if s.service_status)
        desired = sum(s.service_status.desired_tasks for s in services if s.service_status)
        configs, secrets = await asyncio.gather(
            self._visible_count(actor, environment_id, "config", client),
            self._visible_count(actor, environment_id, "secret", client),
        )
        if not actor.is_administrator:
            return SwarmOverview(
                environment_id=environment_id,
                environment_name=environment.name,
                is_local=environment.connection_kind == "unix_socket",
                administrator=False,
                cluster_id=None,
                created_at=None,
                manager_node_id=None,
                nodes=None,
                managers=None,
                nodes_ready=None,
                nodes_active=None,
                nodes_paused=None,
                nodes_drained=None,
                services=len(services),
                tasks_running=running,
                tasks_desired=desired,
                can_create=await self._authoriser.may_create(actor, environment_id),
                configs=configs,
                secrets=secrets,
            )
        cluster = await swarm_read(client.swarm.inspect(), environment_id, self._poke)
        info = await swarm_read(client.system.info(), environment_id, self._poke)
        nodes = await swarm_read(client.swarm_nodes.list(), environment_id, self._poke)
        return SwarmOverview(
            environment_id=environment_id,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            administrator=True,
            cluster_id=cluster.id,
            created_at=parse_engine_time(cluster.created_at),
            manager_node_id=info.swarm.node_id or None,
            nodes=len(nodes),
            managers=sum(1 for n in nodes if n.spec.role == "manager"),
            nodes_ready=sum(1 for n in nodes if n.status.state == "ready"),
            nodes_active=sum(1 for n in nodes if n.spec.availability == "active"),
            nodes_paused=sum(1 for n in nodes if n.spec.availability == "pause"),
            nodes_drained=sum(1 for n in nodes if n.spec.availability == "drain"),
            services=len(services),
            tasks_running=running,
            tasks_desired=desired,
            can_create=True,
            configs=configs,
            secrets=secrets,
        )

    async def nodes(self, actor: Principal, environment_id: int) -> NodeList:
        """List the nodes; administrators only (PermissionDenied for anyone else)."""
        environment = await self._gate(actor, environment_id)
        require_administrator(actor)
        client = await self._hosts.client(environment_id)
        nodes = await swarm_read(client.swarm_nodes.list(), environment_id, self._poke)
        info = await swarm_read(client.system.info(), environment_id, self._poke)
        manager = info.swarm.node_id or None
        rows = sorted(
            (_node_row(n, manager) for n in nodes),
            key=lambda row: (row.hostname.casefold(), row.id),
        )
        return NodeList(environment_id, environment.name, tuple(rows), manager)

    async def node(self, actor: Principal, environment_id: int, node_id: str) -> NodeDetail:
        """Return one node and its tasks; administrators only.

        ResourceNotFound for a malformed or unknown id, whichever role asks, before any
        administrator check would reveal it.
        """
        environment = await self._gate(actor, environment_id)
        self._canonical_node(node_id)
        require_administrator(actor)
        client = await self._hosts.client(environment_id)
        node = await swarm_read(
            self._inspector.node(environment_id, node_id), environment_id, self._poke
        )
        info = await swarm_read(client.system.info(), environment_id, self._poke)
        tasks = await swarm_read(
            client.swarm_tasks.list(filters={"node": [node_id]}), environment_id, self._poke
        )
        services = await swarm_read(client.swarm_services.list(), environment_id, self._poke)
        names = {s.id: s.spec.name for s in services}
        ordered = sorted(tasks, key=lambda t: t.version.index, reverse=True)
        shown = ordered[:MAX_NODE_TASKS]
        return NodeDetail(
            environment_id=environment_id,
            environment_name=environment.name,
            row=_node_row(node, info.swarm.node_id or None),
            version=node.version.index,
            architecture=node.description.platform.architecture,
            os=node.description.platform.os,
            cpus=format(node.description.nano_cpus / 1_000_000_000, "g"),
            memory_bytes=node.description.memory_bytes,
            labels=label_views(node.spec.labels, True),
            engine_labels=label_views(node.description.engine_labels, True),
            tasks=tuple(
                NodeTask(
                    t.id,
                    t.id[:12],
                    t.service_id,
                    names.get(t.service_id, t.service_id[:12])[:MAX_NAME],
                    t.slot,
                    t.status.state,
                    t.desired_state,
                )
                for t in shown
            ),
            tasks_more=len(ordered) - len(shown),
            can_change=True,
        )

    async def availability_impact(
        self, actor: Principal, environment_id: int, node_id: str, availability: str
    ) -> AvailabilityImpact:
        """Name the running tasks a change of availability moves; administrators only."""
        environment = await self._gate(actor, environment_id)
        self._canonical_node(node_id)
        require_administrator(actor)
        if availability not in AVAILABILITIES:
            raise ValidationFailed({"availability": "Choose active, pause or drain."})
        client = await self._hosts.client(environment_id)
        node = await swarm_read(
            self._inspector.node(environment_id, node_id), environment_id, self._poke
        )
        tasks = await swarm_read(
            client.swarm_tasks.list(filters={"node": [node_id]}), environment_id, self._poke
        )
        services = await swarm_read(client.swarm_services.list(), environment_id, self._poke)
        nodes = await swarm_read(client.swarm_nodes.list(), environment_id, self._poke)
        names = {s.id: s.spec.name for s in services}
        counts: dict[str, int] = {}
        for task in tasks:
            if task.status.state == "running" and task.desired_state == "running":
                counts[task.service_id] = counts.get(task.service_id, 0) + 1
        ordered = sorted(
            counts.items(), key=lambda item: (names.get(item[0], "").casefold(), item[0])
        )
        others = sum(
            1
            for n in nodes
            if n.id != node_id and n.spec.availability == "active" and n.status.state == "ready"
        )
        return AvailabilityImpact(
            environment_id=environment_id,
            environment_name=environment.name,
            node_id=node.id,
            short_id=node.id[:12],
            hostname=_hostname_text(node.description.hostname) or node.id[:12],
            role=node.spec.role,
            current=node.spec.availability,
            requested=availability,
            version=node.version.index,
            services=tuple(
                ServiceTasks(sid, names.get(sid, sid[:12])[:MAX_NAME], count)
                for sid, count in ordered[:MAX_SERVICES_NAMED]
            ),
            services_more=max(len(ordered) - MAX_SERVICES_NAMED, 0),
            tasks_running=sum(counts.values()),
            other_active_nodes=others,
            stays_pending=availability != "active" and others == 0 and bool(counts),
        )

    async def task(self, actor: Principal, environment_id: int, task_id: str) -> TaskDetail:
        """Return one task; visible exactly when its service is (read access)."""
        environment = await self._gate(actor, environment_id)
        task = await swarm_read(
            self._inspector.task(environment_id, task_id), environment_id, self._poke
        )
        if not is_canonical_id("service", task.service_id):
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "service", task.service_id), "read"
        )
        service = await swarm_read(
            self._inspector.service(environment_id, task.service_id), environment_id, self._poke
        )
        client = await self._hosts.client(environment_id)
        nodes = await swarm_read(client.swarm_nodes.list(), environment_id, self._poke)
        info = await swarm_read(client.system.info(), environment_id, self._poke)
        hostnames = node_hostnames([n for n in nodes if n.id == task.node_id])
        linkable = await linkable_tasks(
            self._authoriser,
            actor,
            environment_id,
            client,
            link_candidates([task], info.swarm.node_id or None),
        )
        return TaskDetail(
            environment_id=environment_id,
            environment_name=environment.name,
            service_id=service.id,
            service_name=service.spec.name[:MAX_NAME],
            task=task_view(task, hostnames, linkable),
            can_view_node=actor.is_administrator,
        )

    async def _gate(self, actor: Principal, environment_id: int) -> Environment:
        return await require_swarm(self._authoriser, self._hosts, actor, environment_id)

    async def _visible_count(
        self,
        actor: Principal,
        environment_id: int,
        kind: Literal["config", "secret"],
        client: EngineClient,
    ) -> int | None:
        try:
            if kind == "config":
                configs = await read_configs(client, environment_id, self._poke)
                refs = [ResourceRef(environment_id, kind, c.id, c.labels) for c in configs]
            else:
                secrets = await read_secrets(client, environment_id, self._poke)
                refs = [ResourceRef(environment_id, kind, s.id, s.labels) for s in secrets]
        except (EnvironmentUnavailable, ObjectTooLarge):
            return None
        if actor.is_administrator:
            return len(refs)
        return len(await self._authoriser.filter_visible(actor, refs))

    @staticmethod
    def _canonical_node(node_id: str) -> None:
        if SWARM_OBJECT_ID.fullmatch(node_id) is None:
            raise ResourceNotFound("not found")


class NodeService:
    """Changes a node's availability, one audited operation."""

    def __init__(
        self,
        *,
        database: Database,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        locks: ObjectLockRegistry,
        activity: ActivityRecorder,
        clock: Clock,
        poke: Callable[[int], None],
        self_identity: SelfIdentitySource | None = None,
    ) -> None:
        self._database = database
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._locks = locks
        self._activity = activity
        self._clock = clock
        self._poke = poke
        self._self_identity = self_identity or no_identity

    @audited(
        action="node_availability_changed",
        target_type="node",
        target_id=_node_argument,
        environment_id=environment_argument,
    )
    async def set_availability(
        self,
        actor: Principal,
        environment_id: int,
        node_id: str,
        *,
        availability: str,
        expected_version: int,
        expected_availability: str,
    ) -> NodeChanged:
        """Set a node's availability to active, pause or drain; administrators only.

        `expected_version` and `expected_availability` are what the confirmation showed; a node
        that no longer matches refuses the change. Docker reschedules the tasks of a drained
        node; nothing is removed here. ValidationFailed keyed `availability` for another value.
        SelfProtected when asked to drain the node that runs the container dockmin runs in.
        """
        audit = current_audit()
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        if SWARM_OBJECT_ID.fullmatch(node_id) is None:
            raise ResourceNotFound("not found")
        require_administrator(actor)
        if availability not in AVAILABILITIES:
            audit.reason = "invalid"
            raise ValidationFailed({"availability": "Choose active, pause or drain."})
        if availability == "drain" and self._self_identity().protects_node(environment_id, node_id):
            audit.reason = SELF_PROTECTED_REASON
            raise SelfProtected.node()
        client = await audited_client(self._hosts, environment_id)
        holder = LockHolder(UserActor.of(actor), actor.username, "availability")
        async with hold_all(self._locks, [ObjectKey(environment_id, "node", node_id)], holder):
            self._inspector.forget_node(environment_id, node_id)
            try:
                node = await self._inspector.node(environment_id, node_id)
                document = await client.swarm_nodes.document(node_id)
            except ResourceNotFound:
                audit.reason = "node_gone"
                raise NodeGone() from None
            except EnvironmentUnavailable as error:
                audit.reason = error.kind
                raise
            except ObjectTooLarge:
                audit.reason = "too_large"
                raise
            except NotFound:
                audit.reason = "node_gone"
                raise NodeGone() from None
            except EngineError as error:
                raise self._failure(error, environment_id) from None
            hostname = _hostname_text(node.description.hostname) or node.id[:12]
            before = str(document.spec.get("Availability") or "active")
            audit.detail.update(
                hostname=hostname,
                role=node.spec.role,
                availability_before=before,
                availability_after=availability,
            )
            if document.id != node_id or document.version != expected_version:
                audit.reason = "changed_since_review"
                raise NodeChangedSinceReview()
            if before != expected_availability:
                audit.reason = "changed_since_review"
                raise NodeChangedSinceReview()
            running = await self._running_tasks(client, environment_id, node_id)
            audit.detail["tasks_running"] = running
            if availability == before:
                audit.unchanged()
                return NodeChanged(
                    environment_id, node_id, hostname, before, availability, running, True
                )
            body: dict[str, Any] = dict(document.spec)
            body["Availability"] = availability
            try:
                await run_shielded(
                    partial(client.swarm_nodes.update, node_id, body, version=document.version),
                    partial(self._interrupted, actor, environment_id, node_id),
                )
            except EngineError as error:
                raise await self._update_failure(
                    error, client, environment_id, node_id, document.version
                ) from None
            self._inspector.forget_node(environment_id, node_id)
        return NodeChanged(environment_id, node_id, hostname, before, availability, running, False)

    async def _running_tasks(self, client: EngineClient, environment_id: int, node_id: str) -> int:
        tasks = await audited_swarm_read(
            client.swarm_tasks.list(filters={"node": [node_id]}), environment_id, self._poke
        )
        return sum(1 for t in tasks if t.status.state == "running" and t.desired_state == "running")

    def _failure(self, error: EngineError, environment_id: int) -> ProductError:
        audit = current_audit()
        classified = engine_failure(error, environment_id, self._poke)
        if classified is not None:
            audit.reason = classified[0]
            return classified[1]
        audit.reason = "engine_rejected"
        return unavailable_from(error)

    async def _update_failure(
        self,
        error: EngineError,
        client: EngineClient,
        environment_id: int,
        node_id: str,
        version: int,
    ) -> ProductError:
        audit = current_audit()
        logger.warning(
            "node_update_refused",
            extra={"error": type(error).__name__, "status": error.status_code},
        )
        if isinstance(error, NotFound):
            audit.reason = "node_gone"
            return NodeGone()
        classified = engine_failure(error, environment_id, self._poke)
        if classified is not None:
            audit.reason = classified[0]
            return classified[1]
        try:
            fresh = await client.swarm_nodes.document(node_id)
        except EngineError:
            audit.reason = "engine_rejected"
            return NodeRefused(engine_detail(error))
        if fresh.version != version:
            audit.reason = "changed_since_review"
            return NodeChangedSinceReview()
        audit.reason = "engine_rejected"
        return NodeRefused(engine_detail(error))

    async def _interrupted(
        self,
        actor: Principal,
        environment_id: int,
        node_id: str,
        task: asyncio.Future[None],
    ) -> None:
        audit = current_audit()
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, SETTLE_SECONDS)
            detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
            failure = None
            if not (finished and error is None):
                failure = failure_reason(error) if error is not None else "outcome_unknown"
            await write_settled_row(
                self._database,
                self._clock,
                actor,
                action="node_availability_changed",
                target_type="node",
                target_id=node_id,
                environment_id=environment_id,
                detail=detail,
                failure=failure,
            )
