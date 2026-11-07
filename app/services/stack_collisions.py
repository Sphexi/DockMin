"""Container-name and published-port collisions between a stack file and what the host runs."""

import asyncio
import ipaddress
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, ResponseTooLarge
from app.docker_engine.models import ContainerSummary, SwarmService
from app.logging import Scrubber, get_logger
from app.services.access import COMPOSE_PROJECT_LABEL, SWARM_NAMESPACE_LABEL, canonical_stack_key
from app.services.authorisation import Authoriser
from app.services.containers import display_name, short_id
from app.services.host_objects import (
    ServiceListTooLarge,
    permitted_container_ids,
    permitted_service_ids,
    read_services,
)
from app.services.object_inspector import EngineObjectInspector, unavailable_from

__all__ = [
    "NOT_CHECKED",
    "REASON_NAME",
    "REASON_PORT",
    "REASON_RANGE",
    "CollisionChecker",
    "CollisionReport",
]

logger = get_logger(__name__)

MAX_PORTS_CHECKED: Final = 1024
MAX_STOPPED_INSPECTED: Final = 50
INSPECT_CONCURRENCY: Final = 8
MAX_BINDINGS_PER_CONTAINER: Final = 512
MAX_REPORTED: Final = 20
MAX_HITS: Final = 10_000
MAX_SPANS_SHOWN: Final = 3
MAX_PORT: Final = 65535
MAX_DIGITS: Final = 5

REASON_NAME: Final = "container_name_in_use"
REASON_PORT: Final = "port_in_use"
REASON_RANGE: Final = "port_range_too_large"
_REASON_ORDER: Final = (REASON_NAME, REASON_PORT, REASON_RANGE)
_LISTED_STATES: Final = frozenset({"running", "paused"})
_RESTARTING: Final = "restarting"

MANAGER_ONLY: Final = (
    "Host-mode ports were checked against the containers on the manager this environment "
    "connects to. Other nodes were not checked."
)
CONTAINERS_TOO_MANY: Final = (
    "The container list was too large to read here, so container names and published ports "
    "were not checked."
)
SERVICES_TOO_MANY: Final = (
    "The service list was too large to read here, so ports published by other services were "
    "not checked."
)
STOPPED_UNCHECKED: Final = "Some stopped containers were not checked for published ports."
BINDINGS_TRUNCATED: Final = (
    "Some containers publish more ports than can be checked here; the rest were not checked."
)
NOT_CHECKED: Final = "The stored file could not be checked for container names and published ports."
RANGE_TOO_LARGE: Final = (
    f"The file publishes more than {MAX_PORTS_CHECKED} host ports, which is more than can be "
    "checked. Publish fewer ports or a narrower range."
)

type Kind = Literal["container", "service"]
type PublishMode = Literal["ingress", "host"]
type Span = tuple[int, int]


@dataclass(frozen=True, slots=True)
class NameClaim:
    service: str
    name: str


@dataclass(frozen=True, slots=True)
class PortClaim:
    """A host port, or a range of them, that a service publishes; no address means every one."""

    service: str
    address: str | None
    first: int
    last: int
    protocol: str
    mode: PublishMode = "host"

    @property
    def size(self) -> int:
        return self.last - self.first + 1


@dataclass(frozen=True, slots=True)
class Holder:
    """`alive` is false for a container that is not running or restarting."""

    kind: Kind
    id: str
    name: str
    alive: bool = True


@dataclass(frozen=True, slots=True)
class HeldPort:
    holder: Holder
    address: str | None
    first: int
    last: int
    protocol: str


@dataclass(frozen=True, slots=True)
class PortConflict:
    """The ports of one claiming service that one holder also publishes, at one shared address."""

    service: str
    protocol: str
    address: str | None
    claim_address: str | None
    holder: Holder
    spans: tuple[Span, ...]


@dataclass(frozen=True, slots=True)
class ConflictSet:
    """Conflicts in claim order; `truncated` when matching stopped at the hit limit."""

    items: tuple[PortConflict, ...] = ()
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class CollisionReport:
    """Refusals keyed by Compose path, why, the warnings, and a summary free of holder names."""

    fields: Mapping[str, str] = field(default_factory=dict)
    reason: str = ""
    warnings: tuple[str, ...] = ()
    summary: str = ""


def _span(value: object) -> Span | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        first = last = value
    elif isinstance(value, str):
        low, separator, high = value.strip().partition("-")
        for part in (low, high) if separator else (low,):
            if not (part.isascii() and part.isdigit()) or len(part) > MAX_DIGITS:
                return None
        first, last = int(low), int(high) if separator else int(low)
    else:
        return None
    if not 1 <= first <= last <= MAX_PORT:
        return None
    return first, last


def _address(value: object) -> str | None:
    """Return a canonical address, or None for no address or an unspecified one."""
    if not isinstance(value, str):
        return None
    text = value.strip().strip("[]")
    if not text:
        return None
    try:
        parsed: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(text)
    except ValueError:
        return text.casefold()
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return None if parsed.is_unspecified else str(parsed)


def _shared(first: str | None, second: str | None) -> tuple[bool, str | None]:
    """Return whether two addresses overlap and the address they overlap on; None is all."""
    if first is None or second is None:
        return True, second if first is None else first
    return first == second, first


def _protocol(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) and value.strip() else "tcp"


def compose_claims(
    services: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[NameClaim, ...], tuple[PortClaim, ...]]:
    """Read the explicit container names and published ports from a resolved Compose model."""
    names: list[NameClaim] = []
    ports: list[PortClaim] = []
    for service, body in services.items():
        if not isinstance(body, Mapping):
            continue
        name = body.get("container_name")
        if isinstance(name, str) and name:
            names.append(NameClaim(service, name))
        declared = body.get("ports")
        if not isinstance(declared, list):
            continue
        for port in declared:
            if not isinstance(port, Mapping):
                continue
            span = _span(port.get("published"))
            if span is None:
                continue
            ports.append(
                PortClaim(
                    service,
                    _address(port.get("host_ip")),
                    span[0],
                    span[1],
                    _protocol(port.get("protocol")),
                )
            )
    return tuple(names), tuple(ports)


def swarm_claims(services: Iterable[tuple[str, Mapping[str, Any]]]) -> tuple[PortClaim, ...]:
    """Read the published ports from translated Swarm service specs, keyed by Compose service."""
    claims: list[PortClaim] = []
    for key, spec in services:
        endpoint = spec.get("EndpointSpec")
        declared = endpoint.get("Ports") if isinstance(endpoint, Mapping) else None
        if not isinstance(declared, list):
            continue
        for port in declared:
            if not isinstance(port, Mapping):
                continue
            span = _span(port.get("PublishedPort"))
            if span is None:
                continue
            mode: PublishMode = "ingress" if port.get("PublishMode") == "ingress" else "host"
            claims.append(
                PortClaim(key, None, span[0], span[1], _protocol(port.get("Protocol")), mode)
            )
    return tuple(claims)


def over_cap(claims: Sequence[PortClaim]) -> PortClaim | None:
    """Return the claim whose ports take the total past the cap, or None when all fit."""
    total = 0
    for claim in claims:
        total += claim.size
        if total > MAX_PORTS_CHECKED:
            return claim
    return None


def _exact_names(container: ContainerSummary) -> set[str]:
    return {name[1:] if name.startswith("/") else name for name in container.names}


def find_name_conflicts(
    claims: Sequence[NameClaim], containers: Iterable[ContainerSummary]
) -> list[tuple[NameClaim, ContainerSummary]]:
    """Pair each claimed name with the container that has it, by exact name."""
    holders: dict[str, ContainerSummary] = {}
    for container in containers:
        for name in _exact_names(container):
            holders.setdefault(name, container)
    return [(claim, holders[claim.name]) for claim in claims if claim.name in holders]


@dataclass(frozen=True, slots=True)
class _Segment:
    first: int
    last: int
    address: str | None
    holder: Holder


class _Index:
    """Held ports merged per holder, protocol and address, searchable by interval overlap."""

    def __init__(self, held: Iterable[HeldPort]) -> None:
        grouped: dict[tuple[str, str, str | None], tuple[Holder, list[Span]]] = {}
        for entry in held:
            key = (entry.protocol, entry.holder.id, entry.address)
            grouped.setdefault(key, (entry.holder, []))[1].append((entry.first, entry.last))
        by_protocol: dict[str, list[_Segment]] = {}
        for (protocol, _, address), (holder, spans) in grouped.items():
            by_protocol.setdefault(protocol, []).extend(
                _Segment(first, last, address, holder) for first, last in _merged(spans)
            )
        self._segments = {
            protocol: sorted(segments, key=lambda s: s.first)
            for protocol, segments in by_protocol.items()
        }
        self._reach = {
            protocol: _reach_of(segments) for protocol, segments in self._segments.items()
        }

    def overlapping(self, protocol: str, first: int, last: int) -> Iterator[_Segment]:
        segments = self._segments.get(protocol)
        if segments is None:
            return
        reach = self._reach[protocol]
        pending = [(0, len(segments))]
        while pending:
            low, high = pending.pop()
            if low >= high:
                continue
            middle = (low + high) // 2
            if reach[middle] < first:
                continue
            pending.append((low, middle))
            segment = segments[middle]
            if segment.first <= last:
                if segment.last >= first:
                    yield segment
                pending.append((middle + 1, high))


def _merged(spans: Iterable[Span]) -> list[Span]:
    merged: list[Span] = []
    for first, last in sorted(spans):
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def _reach_of(segments: Sequence[_Segment]) -> list[int]:
    """Return, per index, the highest `last` in the subtree of the implicit balanced tree."""
    reach = [0] * len(segments)

    def fill(low: int, high: int) -> int:
        if low >= high:
            return 0
        middle = (low + high) // 2
        reach[middle] = max(segments[middle].last, fill(low, middle), fill(middle + 1, high))
        return reach[middle]

    fill(0, len(segments))
    return reach


def find_port_conflicts(claims: Sequence[PortClaim], held: Iterable[HeldPort]) -> ConflictSet:
    """Intersect each claim with the held ports as intervals, one conflict per service and holder.

    Cost is bounded by the number of claims and holders, not by the width of any range. Matching
    stops after `MAX_HITS` overlaps.
    """
    index = _Index(held)
    groups: dict[tuple[str, str, str, str | None], tuple[int, PortClaim, Holder, list[Span]]] = {}
    hits = 0
    truncated = False
    for order, claim in enumerate(claims):
        for segment in index.overlapping(claim.protocol, claim.first, claim.last):
            overlap, address = _shared(claim.address, segment.address)
            if not overlap:
                continue
            hits += 1
            if hits > MAX_HITS:
                truncated = True
                break
            key = (claim.service, segment.holder.id, claim.protocol, address)
            span = (max(claim.first, segment.first), min(claim.last, segment.last))
            groups.setdefault(key, (order, claim, segment.holder, []))[3].append(span)
        if truncated:
            break
    ordered = sorted(
        groups.items(),
        key=lambda item: (item[1][0], item[1][2].name, item[1][2].id, item[0][3] or ""),
    )
    return ConflictSet(
        tuple(
            PortConflict(key[0], key[2], key[3], claim.address, holder, tuple(_merged(spans)))
            for key, (_, claim, holder, spans) in ordered
        ),
        truncated,
    )


def _held_from_listing(holder: Holder, container: ContainerSummary) -> list[HeldPort]:
    return [
        HeldPort(
            holder,
            _address(binding.ip),
            binding.public_port,
            binding.public_port,
            _protocol(binding.type),
        )
        for binding in container.ports
        if binding.public_port is not None and 1 <= binding.public_port <= MAX_PORT
    ]


def _held_from_bindings(
    holder: Holder, host_config: Mapping[str, Any]
) -> tuple[list[HeldPort], bool]:
    """Return the host bindings a container is configured with, and whether some were dropped."""
    configured = host_config.get("PortBindings")
    if not isinstance(configured, Mapping):
        return [], False
    found: list[HeldPort] = []
    for spec, targets in configured.items():
        if not isinstance(spec, str) or not isinstance(targets, list):
            continue
        protocol = _protocol(spec.partition("/")[2])
        for target in targets:
            if not isinstance(target, Mapping):
                continue
            span = _span(target.get("HostPort"))
            if span is None:
                continue
            if len(found) >= MAX_BINDINGS_PER_CONTAINER:
                return found, True
            found.append(
                HeldPort(holder, _address(target.get("HostIp")), span[0], span[1], protocol)
            )
    return found, False


def _held_from_service(
    holder: Holder, service: SwarmService, *, ingress_only: bool
) -> list[HeldPort]:
    return [
        HeldPort(holder, None, port.published_port, port.published_port, _protocol(port.protocol))
        for port in service.endpoint.ports
        if port.published_port is not None
        and 1 <= port.published_port <= MAX_PORT
        and (port.publish_mode == "ingress" or not ingress_only)
    ]


def _carries(labels: Mapping[str, str], label: str, key: str) -> bool:
    value = labels.get(label)
    return bool(value) and canonical_stack_key(value or "") == key


def _address_text(address: str | None) -> str:
    return "every address" if address is None else address


def _ports_text(spans: Sequence[Span]) -> str:
    shown = [str(first) if first == last else f"{first}-{last}" for first, last in spans]
    text = ", ".join(shown[:MAX_SPANS_SHOWN])
    return f"{text}, ..." if len(shown) > MAX_SPANS_SHOWN else text


def _label(spans: Sequence[Span]) -> str:
    return "Port" if len(spans) == 1 and spans[0][0] == spans[0][1] else "Ports"


def _who(holder: Holder, visible: frozenset[str]) -> str:
    if holder.id not in visible:
        return "another container" if holder.kind == "container" else "another service"
    return f"the {holder.kind} {holder.name}"


def _id_suffix(holder: Holder, visible: frozenset[str]) -> str:
    return f" ({short_id(holder.id)})" if holder.id in visible else ""


class _Findings:
    def __init__(self, scrubber: Scrubber) -> None:
        self._scrubber = scrubber
        self._refusals: dict[str, list[str]] = {}
        self._summaries: list[str] = []
        self._reasons: dict[str, None] = {}
        self._refused = 0
        self._refused_more = 0
        self._last_path = ""
        self._warnings: list[str] = []
        self._warned_more = 0
        self._once: dict[str, None] = {}
        self.notes: list[str] = []
        self.unbounded = False

    def reject_claim(self, path: str, reason: str, message: str, summary: str) -> None:
        self._reasons[reason] = None
        self._refused += 1
        if self._refused > MAX_REPORTED:
            self._refused_more += 1
            return
        self._refusals.setdefault(path, []).append(message)
        self._summaries.append(summary)
        self._last_path = path

    def warn(self, message: str) -> None:
        if len(self._warnings) >= MAX_REPORTED:
            self._warned_more += 1
        else:
            self._warnings.append(message)

    def warn_once(self, message: str) -> None:
        if message not in self._once:
            self._once[message] = None
            self.warn(message)

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def report(self) -> CollisionReport:
        scrub = self._scrubber.scrub_text
        fields = {path: " ".join(messages) for path, messages in self._refusals.items()}
        summaries = list(self._summaries)
        if self._refused_more or (self.unbounded and self._refused):
            tail = f"and {self._refused_more} more." if self._refused_more else "and more."
            fields[self._last_path] = f"{fields[self._last_path]} {tail}"
            summaries.append(tail)
        warnings = [*self.notes, *self._warnings]
        if self._warned_more or (self.unbounded and self._warnings):
            warnings.append(f"and {self._warned_more} more." if self._warned_more else "and more.")
        reason = min(self._reasons, key=_REASON_ORDER.index) if self._reasons else ""
        return CollisionReport(
            {path: scrub(text) for path, text in fields.items()},
            reason,
            tuple(scrub(text) for text in warnings),
            scrub(" ".join(summaries)),
        )


class CollisionChecker:
    """Compares a stack file's names and published ports with the host; it writes nothing.

    Every Engine read happens here, so a caller must not hold a database transaction open.
    """

    def __init__(self, *, authoriser: Authoriser, inspector: EngineObjectInspector) -> None:
        self._authoriser = authoriser
        self._inspector = inspector

    async def compose(
        self,
        actor: Principal,
        environment_id: int,
        client: EngineClient,
        *,
        project: str,
        services: Mapping[str, Mapping[str, Any]],
        scrubber: Scrubber,
    ) -> CollisionReport:
        """Check a resolved Compose model; the project's own containers are left out."""
        names, claims = compose_claims(services)
        findings = _Findings(scrubber)
        excess = over_cap(claims)
        if excess is not None:
            self._too_wide(findings, excess)
            claims = ()
        if not names and not claims:
            return findings.report()
        listing = await self._containers(client)
        if listing is None:
            findings.note(CONTAINERS_TOO_MANY)
            return findings.report()
        key = canonical_stack_key(project)
        others = [c for c in listing if not _carries(c.labels, COMPOSE_PROJECT_LABEL, key)]
        named = find_name_conflicts(names, others)
        conflicts = await self._port_conflicts(environment_id, claims, others, findings)
        findings.unbounded = conflicts.truncated
        visible = await self._visible(
            actor,
            environment_id,
            others,
            {container.id for _, container in named} | {c.holder.id for c in conflicts.items},
        )
        for claim, container in named:
            holder = _container_holder(container)
            findings.reject_claim(
                f"services.{claim.service}.container_name",
                REASON_NAME,
                f"The container name {claim.name} is already used by "
                f"{_who(holder, visible)}{_id_suffix(holder, visible)}.",
                f"Service {claim.service} sets a container name that is already in use.",
            )
        self._record(findings, conflicts, visible, manager=False)
        return findings.report()

    async def swarm(
        self,
        actor: Principal,
        environment_id: int,
        client: EngineClient,
        *,
        namespace: str,
        services: Iterable[tuple[str, Mapping[str, Any]]],
        scrubber: Scrubber,
        poke: Callable[[int], None] | None = None,
    ) -> CollisionReport:
        """Check translated Swarm services.

        Ingress ports are compared with the endpoint ports of every other service. Host-mode
        ports are compared with other services' ingress ports, which every node holds, and with
        the containers on the manager, which only warn: the task may be placed elsewhere.
        """
        claims = swarm_claims(services)
        findings = _Findings(scrubber)
        excess = over_cap(claims)
        if excess is not None:
            self._too_wide(findings, excess)
            return findings.report()
        if not claims:
            return findings.report()
        key = canonical_stack_key(namespace)
        ingress = tuple(claim for claim in claims if claim.mode == "ingress")
        host = tuple(claim for claim in claims if claim.mode == "host")
        others = await self._other_services(environment_id, client, key, poke, findings)
        every = [
            entry
            for service in others
            for entry in _held_from_service(_service_holder(service), service, ingress_only=False)
        ]
        ingress_held = [
            entry
            for service in others
            for entry in _held_from_service(_service_holder(service), service, ingress_only=True)
        ]
        found_ingress = await self._find(ingress, every)
        found_host = await self._find(host, ingress_held)
        against_services = ConflictSet(
            (*found_ingress.items, *found_host.items),
            found_ingress.truncated or found_host.truncated,
        )
        against_containers = ConflictSet()
        container_ids: set[str] = set()
        others_c: list[ContainerSummary] = []
        if host:
            findings.note(MANAGER_ONLY)
            listing = await self._containers(client)
            if listing is None:
                findings.note(CONTAINERS_TOO_MANY)
            else:
                others_c = [
                    c for c in listing if not _carries(c.labels, SWARM_NAMESPACE_LABEL, key)
                ]
                against_containers = await self._port_conflicts(
                    environment_id, host, others_c, findings
                )
                container_ids = {c.holder.id for c in against_containers.items}
        matched = {c.holder.id for c in against_services.items}
        service_visible = (
            await permitted_service_ids(
                self._authoriser,
                actor,
                environment_id,
                [s for s in others if s.id in matched],
            )
            if matched
            else frozenset()
        )
        container_visible = await self._visible(actor, environment_id, others_c, container_ids)
        visible = service_visible | container_visible
        self._record(findings, against_services, visible, manager=False)
        self._record(findings, against_containers, visible, manager=True)
        findings.unbounded = against_services.truncated or against_containers.truncated
        return findings.report()

    def _too_wide(self, findings: _Findings, claim: PortClaim) -> None:
        findings.reject_claim(
            f"services.{claim.service}.ports",
            REASON_RANGE,
            RANGE_TOO_LARGE,
            f"Service {claim.service} publishes more ports than can be checked.",
        )

    async def _find(self, claims: Sequence[PortClaim], held: Sequence[HeldPort]) -> ConflictSet:
        if not claims or not held:
            return ConflictSet()
        return await asyncio.to_thread(find_port_conflicts, claims, held)

    async def _containers(self, client: EngineClient) -> list[ContainerSummary] | None:
        try:
            return await client.containers.list(all=True)
        except ResponseTooLarge:
            return None
        except EngineError as error:
            raise unavailable_from(error) from None

    async def _other_services(
        self,
        environment_id: int,
        client: EngineClient,
        key: str,
        poke: Callable[[int], None] | None,
        findings: _Findings,
    ) -> list[SwarmService]:
        try:
            listed = await read_services(client, environment_id, poke)
        except ServiceListTooLarge:
            findings.note(SERVICES_TOO_MANY)
            return []
        return [s for s in listed if not _carries(s.spec.labels, SWARM_NAMESPACE_LABEL, key)]

    async def _port_conflicts(
        self,
        environment_id: int,
        claims: Sequence[PortClaim],
        others: Sequence[ContainerSummary],
        findings: _Findings,
    ) -> ConflictSet:
        """Match the claims against the containers' published ports, listed or configured."""
        if not claims:
            return ConflictSet()
        held = [
            entry
            for c in others
            if c.state in _LISTED_STATES
            for entry in _held_from_listing(_container_holder(c), c)
        ]
        quiet = [c for c in others if c.state not in _LISTED_STATES]
        configured, unchecked, cut = await self._bindings(environment_id, quiet)
        held.extend(configured)
        if unchecked:
            findings.note(STOPPED_UNCHECKED)
        if cut:
            findings.note(BINDINGS_TRUNCATED)
        return await self._find(claims, held)

    async def _bindings(
        self, environment_id: int, quiet: Sequence[ContainerSummary]
    ) -> tuple[list[HeldPort], bool, bool]:
        """Read the configured bindings of at most 50 containers.

        Also returns whether any container was left unread, and whether any container's
        bindings were cut.
        """
        chosen = sorted(quiet, key=lambda c: (c.state != _RESTARTING, -c.created, c.id))[
            :MAX_STOPPED_INSPECTED
        ]
        gate = asyncio.Semaphore(INSPECT_CONCURRENCY)

        async def read(container: ContainerSummary) -> tuple[list[HeldPort], bool] | None:
            holder = _container_holder(container)
            async with gate:
                try:
                    inspected = await self._inspector.inspect(environment_id, container.id)
                except ResourceNotFound:
                    return [], False
                except Exception as error:
                    logger.warning(
                        "collision_inspect_failed", extra={"error": type(error).__name__}
                    )
                    return None
                finally:
                    self._inspector.forget(environment_id, container.id)
            return _held_from_bindings(holder, inspected.host_config)

        held: list[HeldPort] = []
        unchecked = len(chosen) < len(quiet)
        cut = False
        for result in await asyncio.gather(*(read(container) for container in chosen)):
            if result is None:
                unchecked = True
                continue
            held.extend(result[0])
            cut |= result[1]
        return held, unchecked, cut

    async def _visible(
        self,
        actor: Principal,
        environment_id: int,
        containers: Sequence[ContainerSummary],
        wanted: set[str],
    ) -> frozenset[str]:
        if not wanted:
            return frozenset()
        return await permitted_container_ids(
            self._authoriser,
            actor,
            environment_id,
            [c for c in containers if c.id in wanted],
        )

    def _record(
        self, findings: _Findings, conflicts: ConflictSet, visible: frozenset[str], *, manager: bool
    ) -> None:
        """Turn conflicts into refusals, or into warnings for a holder that does not block."""
        for conflict in conflicts.items:
            holder = conflict.holder
            seen = holder.id in visible
            shared = conflict.address if seen else conflict.claim_address
            where = f"{_ports_text(conflict.spans)}/{conflict.protocol} on {_address_text(shared)}"
            label = _label(conflict.spans)
            service = conflict.service
            if manager:
                if seen:
                    findings.warn(
                        f"Service {service}: {label.lower()} {where} is published on the "
                        f"manager by {_who(holder, visible)}. This conflicts if a task of the "
                        "service is placed there."
                    )
                else:
                    findings.warn_once(
                        f"Service {service}: a container you cannot see on the manager "
                        "publishes a port that overlaps the host-mode ports this service "
                        "publishes."
                    )
            elif holder.kind == "container" and not holder.alive:
                if seen:
                    findings.warn(
                        f"Service {service}: {label.lower()} {where} is published by "
                        f"{_who(holder, visible)}, which is not running."
                    )
                else:
                    findings.warn_once(
                        f"Service {service}: a container you cannot see has a stopped port "
                        "binding that overlaps the ports this service publishes."
                    )
            else:
                findings.reject_claim(
                    f"services.{service}.ports",
                    REASON_PORT,
                    f"{label} {where} is already published by {_who(holder, visible)}.",
                    f"Service {service} publishes {conflict.protocol} "
                    f"{label.lower()} {_ports_text(conflict.spans)} that is already published.",
                )


def _container_holder(container: ContainerSummary) -> Holder:
    alive = container.state in _LISTED_STATES or container.state == _RESTARTING
    return Holder("container", container.id, display_name(container.names, container.id), alive)


def _service_holder(service: SwarmService) -> Holder:
    return Holder("service", service.id, service.spec.name)
