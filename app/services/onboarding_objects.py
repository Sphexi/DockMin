"""The objects on a host that have no control, found for the onboarding review."""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from app.auth.principal import Principal
from app.db.repositories.resource_control import ResourceControlRepository
from app.docker_engine.models import SwarmService
from app.self_identity import SelfIdentity, SelfIdentitySource
from app.services.access import ResourceType, canonical_stack_key, stack_of
from app.services.containers import display_name
from app.services.host_objects import read_containers, read_networks, read_services, read_volumes
from app.services.networks import network_kind
from app.services.object_inspector import HostClients, ObjectTooLarge, SwarmUnavailable
from app.services.onboarding_plan import ObjectTarget, ProtectedObject
from app.services.swarm_configs import read_configs, read_secrets
from app.services.volume_backfill import BackfillVolume, VolumeBackfillService

MAX_OBJECTS: Final = 2000
TYPE_ORDER: Final[tuple[ResourceType, ...]] = (
    "container",
    "volume",
    "network",
    "service",
    "config",
    "secret",
)


@dataclass(frozen=True, slots=True)
class ObjectScan:
    """The uncontrolled objects of one environment, capped, and what was left out.

    `beyond` counts the uncontrolled objects past the cap. `complete` is false when a Swarm list
    could not be read, so the objects of those types are missing.
    """

    objects: tuple[ObjectTarget, ...]
    protected: tuple[ProtectedObject, ...]
    beyond: int
    complete: bool


@dataclass(frozen=True, slots=True)
class _Found:
    type: ResourceType
    id: str
    name: str
    labels: Mapping[str, str]


class UncontrolledObjectScanner:
    """Lists the objects with neither a control of their own nor one inherited from a stack.

    Reads only. dockmin's own container, Compose project, Swarm service and namespace, and the
    volumes it mounts, come back as protected and are never among the objects.
    """

    def __init__(
        self,
        *,
        hosts: HostClients,
        controls: ResourceControlRepository,
        identity: SelfIdentitySource,
        backfill: VolumeBackfillService,
        poke: Callable[[int], None] | None = None,
    ) -> None:
        self._hosts = hosts
        self._controls = controls
        self._identity = identity
        self._backfill = backfill
        self._poke = poke

    async def own_stack_keys(self, environment_id: int) -> frozenset[str]:
        """Return the canonical keys of dockmin's own Compose project and Swarm namespace here."""
        identity = self._identity()
        services: Sequence[SwarmService] = ()
        if environment_id in identity.swarm_service:
            client = await self._hosts.client(environment_id)
            try:
                services = await read_services(client, environment_id, self._poke)
            except (SwarmUnavailable, ObjectTooLarge):
                services = ()
        return _own_stacks(identity, environment_id, services)

    async def scan(
        self,
        actor: Principal,
        environment_id: int,
        *,
        offered_stacks: Collection[str],
        cap: int = MAX_OBJECTS,
    ) -> ObjectScan:
        """Find the uncontrolled objects; `offered_stacks` are the stacks the flow adopts.

        The members of those stacks, and of a stack that already has a control, are left out
        because the stack's control covers them. EnvironmentUnavailable and ObjectTooLarge when
        containers, volumes or networks cannot be read in full.
        """
        identity = self._identity()
        client = await self._hosts.client(environment_id)
        environment = await self._hosts.environment(environment_id)
        containers = await read_containers(client)
        volumes = await read_volumes(client)
        networks = await read_networks(client)
        found: list[_Found] = [
            *(_Found("container", c.id, display_name(c.names, c.id), c.labels) for c in containers),
            *(_Found("volume", v.name, v.name, v.labels) for v in volumes),
            *(
                _Found("network", n.id, n.name, n.labels)
                for n in networks
                if network_kind(n) == "user"
            ),
        ]
        services: Sequence[SwarmService] = ()
        complete = True
        if environment.type == "swarm":
            try:
                services = await read_services(client, environment_id, self._poke)
                configs = await read_configs(client, environment_id, self._poke)
                secrets = await read_secrets(client, environment_id, self._poke)
            except (SwarmUnavailable, ObjectTooLarge):
                services, complete = (), False
            else:
                found.extend(
                    _Found("service", s.id, _service_name(s), s.spec.labels) for s in services
                )
                found.extend(_Found("config", c.id, c.name, c.labels) for c in configs)
                found.extend(_Found("secret", s.id, s.name, s.labels) for s in secrets)
        own = _own_stacks(identity, environment_id, services)
        listed, protected = await self._classify(
            actor, environment_id, found, identity, own, offered_stacks
        )
        listed.sort(key=lambda item: (TYPE_ORDER.index(item.type), item.name.casefold(), item.id))
        kept = listed[: max(0, cap)]
        kept_volumes = {item.id for item in kept if item.type == "volume"}
        verdicts = await self._backfill.candidates(
            environment_id, [v for v in volumes if v.name in kept_volumes], containers
        )
        return ObjectScan(
            tuple(_target(item, verdicts) for item in kept),
            tuple(sorted(protected, key=lambda p: (TYPE_ORDER.index(p.type), p.name, p.id))),
            len(listed) - len(kept),
            complete,
        )

    async def _classify(
        self,
        actor: Principal,
        environment_id: int,
        found: Sequence[_Found],
        identity: SelfIdentity,
        own: frozenset[str],
        offered_stacks: Collection[str],
    ) -> tuple[list[_Found], list[ProtectedObject]]:
        own_controls = {
            kind: frozenset(
                row.resource_id
                for row in await self._controls.list_standing(actor.user_id, environment_id, kind)
            )
            for kind in TYPE_ORDER
        }
        stack_rows = await self._controls.list_standing(actor.user_id, environment_id, "stack")
        covering = {canonical_stack_key(row.resource_id) for row in stack_rows}
        covering.update(canonical_stack_key(name) for name in offered_stacks)
        listed: list[_Found] = []
        protected: list[ProtectedObject] = []
        for item in found:
            stack = stack_of(item.labels)
            if _is_own(identity, environment_id, item) or (stack is not None and stack in own):
                protected.append(ProtectedObject(item.type, item.name, item.id))
            elif item.id in own_controls[item.type]:
                continue
            elif stack is None or stack not in covering:
                listed.append(item)
        return listed, protected


def _own_stacks(
    identity: SelfIdentity, environment_id: int, services: Sequence[SwarmService]
) -> frozenset[str]:
    keys: set[str] = set()
    project = identity.compose_project.get(environment_id)
    if project is not None:
        keys.add(canonical_stack_key(project))
    for service in services:
        if identity.protects_service(environment_id, service.id):
            namespace = stack_of(service.spec.labels)
            if namespace is not None:
                keys.add(namespace)
    return frozenset(keys)


def _is_own(identity: SelfIdentity, environment_id: int, item: _Found) -> bool:
    match item.type:
        case "container":
            return identity.protects_container(item.id)
        case "volume":
            return identity.protects_volume(environment_id, item.id)
        case "service":
            return identity.protects_service(environment_id, item.id)
        case _:
            return False


def _service_name(service: SwarmService) -> str:
    return service.spec.name or service.id


def _target(item: _Found, verdicts: Mapping[str, BackfillVolume]) -> ObjectTarget:
    verdict = verdicts.get(item.id) if item.type == "volume" else None
    if verdict is not None and verdict.outcome == "assign":
        return ObjectTarget(item.type, item.id, item.name, verdict.owner, verdict.source)
    return ObjectTarget(item.type, item.id, item.name)
