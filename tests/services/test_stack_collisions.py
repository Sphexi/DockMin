"""Container-name and published-port collisions, without a daemon, a database or Compose."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import pytest

import app.services.stack_collisions as collisions
from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, ResponseTooLarge
from app.docker_engine.models import ContainerSummary
from app.logging import REDACTED, Scrubber
from app.services.authorisation import Authoriser
from app.services.containers import short_id
from app.services.object_inspector import EngineObjectInspector
from app.services.stack_collisions import (
    BINDINGS_TRUNCATED,
    CONTAINERS_TOO_MANY,
    MAX_PORTS_CHECKED,
    MAX_REPORTED,
    MAX_STOPPED_INSPECTED,
    RANGE_TOO_LARGE,
    REASON_NAME,
    REASON_PORT,
    REASON_RANGE,
    STOPPED_UNCHECKED,
    CollisionReport,
    HeldPort,
    Holder,
    PortClaim,
    compose_claims,
    find_name_conflicts,
    find_port_conflicts,
    over_cap,
    swarm_claims,
)
from tests.access_support import ADMIN, ENV_A, STANDARD, container_id

PROJECT = "shop"
WEB = "web"
WEB_PORTS = "services.web.ports"
WEB_NAME = "services.web.container_name"
PORT = 8080
HOLDER_NUMBER = 1
HOLDER_NAME = "legacy-db"
HOLDER_ID = container_id(HOLDER_NUMBER)
SPECIFIC = "10.0.0.9"
OTHER_SPECIFIC = "10.0.0.8"
SECRET = "hunter2-sentinel"
PROJECT_LABEL = "com.docker.compose.project"
BEYOND_CAP = 5
BINDING_BASE_PORT = 20_000
BINDING_CAP = collisions.MAX_BINDINGS_PER_CONTAINER
TCP, UDP = "tcp", "udp"
EVERY_ADDRESS = "every address"
GONE = ResourceNotFound("not found")


def holder(number: int = HOLDER_NUMBER, name: str = HOLDER_NAME) -> Holder:
    return Holder("container", container_id(number), name)


def held(
    first: int,
    last: int | None = None,
    *,
    address: str | None = None,
    protocol: str = TCP,
    by: Holder | None = None,
) -> HeldPort:
    return HeldPort(by or holder(), address, first, last or first, protocol)


def claim(
    first: int,
    last: int | None = None,
    *,
    service: str = WEB,
    address: str | None = None,
    protocol: str = TCP,
) -> PortClaim:
    return PortClaim(service, address, first, last or first, protocol)


def binding(port: int, ip: str = "", protocol: str = TCP) -> dict[str, Any]:
    return {"IP": ip, "PrivatePort": 80, "PublicPort": port, "Type": protocol}


def listed(
    number: int,
    name: str,
    *,
    state: str = "running",
    ports: Sequence[dict[str, Any]] = (),
    labels: Mapping[str, str] | None = None,
) -> ContainerSummary:
    return ContainerSummary.model_validate(
        {
            "Id": container_id(number),
            "Names": [f"/{name}"],
            "Image": "nginx:1",
            "Created": number,
            "State": state,
            "Labels": dict(labels or {}),
            "Ports": list(ports),
        }
    )


def configured(*ports: int, ip: str = "", protocol: str = TCP) -> dict[str, Any]:
    entries = [{"HostIp": ip, "HostPort": str(port)} for port in ports]
    return {"PortBindings": {f"80/{protocol}": entries}}


def publishing(
    port: int, *, host_ip: str | None = None, protocol: str | None = None
) -> dict[str, Any]:
    return {
        key: value
        for key, value in {"published": str(port), "host_ip": host_ip, "protocol": protocol}.items()
        if value is not None
    }


def web(*ports: dict[str, Any], container_name: str | None = None) -> dict[str, dict[str, Any]]:
    body: dict[str, Any] = {"ports": list(ports)}
    if container_name is not None:
        body["container_name"] = container_name
    return {WEB: body}


@dataclass(frozen=True, slots=True)
class Inspected:
    host_config: Mapping[str, Any]


class Host:
    """A stand-in for the Engine client, the object inspector and the authoriser at once."""

    def __init__(
        self,
        containers: Sequence[ContainerSummary] = (),
        *,
        host_configs: Mapping[str, Mapping[str, Any]] | None = None,
        visible: Collection[str] = (),
        inspect_errors: Mapping[str, Exception] | None = None,
        listing_error: Exception | None = None,
    ) -> None:
        self.containers = self
        self._rows = list(containers)
        self._host_configs = host_configs or {}
        self._visible = frozenset(visible)
        self._inspect_errors = inspect_errors or {}
        self._listing_error = listing_error
        self.list_reads = 0
        self.inspected: list[str] = []
        self.forgotten: list[str] = []

    async def list(self, *, all: bool = False) -> list[ContainerSummary]:
        self.list_reads += 1
        if self._listing_error is not None:
            raise self._listing_error
        return self._rows

    async def inspect(self, environment_id: int, container: str) -> Inspected:
        self.inspected.append(container)
        if container in self._inspect_errors:
            raise self._inspect_errors[container]
        return Inspected(self._host_configs.get(container, {}))

    def forget(self, environment_id: int, container: str) -> None:
        self.forgotten.append(container)

    async def filter_visible(
        self, principal: Principal, items: Sequence[Any], level: str = "read"
    ) -> Sequence[Any]:
        return [item for item in items if item.resource_id in self._visible]

    async def check(
        self,
        services: Mapping[str, Mapping[str, Any]],
        *,
        actor: Principal = ADMIN,
        project: str = PROJECT,
        scrubber: Scrubber | None = None,
    ) -> CollisionReport:
        checker = collisions.CollisionChecker(
            authoriser=cast(Authoriser, self), inspector=cast(EngineObjectInspector, self)
        )
        return await checker.compose(
            actor,
            ENV_A,
            cast(EngineClient, self),
            project=project,
            services=services,
            scrubber=scrubber or Scrubber(),
        )


def spans_of(found: collisions.ConflictSet) -> list[tuple[tuple[int, int], ...]]:
    return [conflict.spans for conflict in found.items]


class TestClaimsFromAResolvedModel:
    @pytest.mark.parametrize(
        ("host_ip", "address"),
        [
            pytest.param(None, None, id="absent"),
            pytest.param("", None, id="empty"),
            pytest.param("0.0.0.0", None, id="ipv4-unspecified"),
            pytest.param("::", None, id="ipv6-unspecified"),
            pytest.param("::ffff:0.0.0.0", None, id="mapped-unspecified"),
            pytest.param("[::1]", "::1", id="brackets-stripped"),
            pytest.param("::ffff:10.0.0.9", SPECIFIC, id="mapped-reads-as-ipv4"),
            pytest.param("127.0.0.1", "127.0.0.1", id="ipv4"),
            pytest.param("LocalHost", "localhost", id="name-folded"),
        ],
    )
    def test_a_host_address_is_canonical_and_an_unspecified_one_means_every_address(
        self, host_ip: str | None, address: str | None
    ) -> None:
        _, claims = compose_claims(web(publishing(PORT, host_ip=host_ip)))

        assert [c.address for c in claims] == [address]

    @pytest.mark.parametrize(
        ("published", "span"),
        [
            pytest.param("8080", (8080, 8080), id="single"),
            pytest.param(8080, (8080, 8080), id="integer"),
            pytest.param("9000-9010", (9000, 9010), id="range"),
            pytest.param(" 9000 ", (9000, 9000), id="padded"),
            pytest.param(None, None, id="ephemeral"),
            pytest.param("", None, id="empty"),
            pytest.param("0", None, id="zero"),
            pytest.param("65536", None, id="past-the-top"),
            pytest.param("9010-9000", None, id="reversed"),
            pytest.param("abc", None, id="text"),
            pytest.param("123456", None, id="too-many-digits"),
            pytest.param(True, None, id="boolean"),
        ],
    )
    def test_only_a_well_formed_published_port_or_range_becomes_a_claim(
        self, published: object, span: tuple[int, int] | None
    ) -> None:
        _, claims = compose_claims({WEB: {"ports": [{"published": published}]}})

        assert [(c.first, c.last) for c in claims] == ([span] if span else [])

    @pytest.mark.parametrize(
        ("protocol", "expected"),
        [(None, TCP), ("UDP", UDP), (" tcp ", TCP), ("", TCP)],
    )
    def test_a_protocol_is_lower_cased_and_defaults_to_tcp(
        self, protocol: str | None, expected: str
    ) -> None:
        _, claims = compose_claims(web(publishing(PORT, protocol=protocol)))

        assert [c.protocol for c in claims] == [expected]

    def test_an_explicit_container_name_is_a_claim_keyed_by_its_service(self) -> None:
        names, _ = compose_claims({WEB: {"container_name": HOLDER_NAME}, "worker": {}})

        assert [(n.service, n.name) for n in names] == [(WEB, HOLDER_NAME)]

    def test_a_swarm_port_keeps_its_publish_mode_and_an_unpublished_one_is_skipped(self) -> None:
        spec = {
            "EndpointSpec": {
                "Ports": [
                    {"PublishedPort": 80, "PublishMode": "ingress", "Protocol": "tcp"},
                    {"PublishedPort": 81, "PublishMode": "host", "Protocol": "udp"},
                    {"TargetPort": 82, "PublishMode": "ingress"},
                ]
            }
        }

        claims = swarm_claims([(WEB, spec)])

        assert [(c.first, c.protocol, c.mode, c.address) for c in claims] == [
            (80, TCP, "ingress", None),
            (81, UDP, "host", None),
        ]


class TestNameMatching:
    @pytest.mark.parametrize(
        ("held_names", "claimed", "matched"),
        [
            pytest.param(["/db"], "db", True, id="slash-stripped"),
            pytest.param(["/db"], "DB", False, id="case-sensitive"),
            pytest.param(["/db"], "d", False, id="whole-name"),
            pytest.param(["/other", "/shop/db"], "db", False, id="link-alias-is-not-a-name"),
            pytest.param(["/other", "/shop/db"], "other", True, id="any-of-several-names"),
        ],
    )
    def test_a_name_matches_a_container_by_exact_name(
        self, held_names: list[str], claimed: str, matched: bool
    ) -> None:
        row = ContainerSummary.model_validate(
            {
                "Id": HOLDER_ID,
                "Names": held_names,
                "Image": "nginx:1",
                "Created": 1,
                "State": "running",
            }
        )

        pairs = find_name_conflicts([collisions.NameClaim(WEB, claimed)], [row])

        assert [(c.name, r.id) for c, r in pairs] == ([(claimed, HOLDER_ID)] if matched else [])


class TestAddressAndProtocolOverlap:
    @pytest.mark.parametrize(
        ("claimed", "holding", "shared"),
        [
            pytest.param(
                None, SPECIFIC, SPECIFIC, id="every-address-claim-meets-a-specific-holder"
            ),
            pytest.param(SPECIFIC, None, SPECIFIC, id="specific-claim-meets-every-address"),
            pytest.param(None, None, None, id="both-every-address"),
            pytest.param(SPECIFIC, SPECIFIC, SPECIFIC, id="same-address"),
            pytest.param(SPECIFIC, OTHER_SPECIFIC, "none", id="different-addresses"),
            pytest.param(SPECIFIC, "::1", "none", id="ipv4-against-ipv6"),
            pytest.param("::1", "::2", "none", id="different-ipv6-addresses"),
            pytest.param("::1", "::1", "::1", id="same-ipv6-address"),
            pytest.param(None, "::1", "::1", id="every-address-claim-meets-ipv6"),
        ],
    )
    def test_addresses_overlap_when_either_is_unspecified_or_both_are_equal(
        self, claimed: str | None, holding: str | None, shared: str | None
    ) -> None:
        found = find_port_conflicts([claim(PORT, address=claimed)], [held(PORT, address=holding)])

        assert [c.address for c in found.items] == ([] if shared == "none" else [shared])

    @pytest.mark.parametrize(
        ("claimed", "holding", "conflicts"),
        [
            pytest.param(TCP, TCP, True, id="same-protocol"),
            pytest.param(TCP, UDP, False, id="tcp-against-udp"),
            pytest.param(UDP, TCP, False, id="udp-against-tcp"),
            pytest.param(UDP, UDP, True, id="same-udp"),
        ],
    )
    def test_a_port_conflicts_only_within_its_protocol(
        self, claimed: str, holding: str, conflicts: bool
    ) -> None:
        found = find_port_conflicts([claim(PORT, protocol=claimed)], [held(PORT, protocol=holding)])

        assert bool(found.items) is conflicts


class TestRangesAndGrouping:
    @pytest.mark.parametrize(
        ("claimed", "holding", "expected"),
        [
            pytest.param((8000, 8010), [(8005, 8005)], ((8005, 8005),), id="claim-contains-held"),
            pytest.param((8005, 8005), [(8000, 8010)], ((8005, 8005),), id="held-contains-claim"),
            pytest.param((8000, 8010), [(7990, 8002)], ((8000, 8002),), id="overlap-at-the-start"),
            pytest.param((8000, 8010), [(8008, 8020)], ((8008, 8010),), id="overlap-at-the-end"),
            pytest.param((8000, 8010), [(8011, 8020)], (), id="just-above"),
            pytest.param((8000, 8010), [(7000, 7999)], (), id="just-below"),
            pytest.param(
                (8000, 8010),
                [(8001, 8001), (8002, 8002), (8003, 8003)],
                ((8001, 8003),),
                id="adjacent-held-ports-merge",
            ),
            pytest.param(
                (8000, 8010),
                [(8001, 8001), (8005, 8006)],
                ((8001, 8001), (8005, 8006)),
                id="separate-held-ports-stay-apart",
            ),
            pytest.param(
                (8000, 8010),
                [(8001, 8005), (8003, 8008)],
                ((8001, 8008),),
                id="overlapping-held-ranges-merge",
            ),
        ],
    )
    def test_a_claimed_range_is_intersected_with_the_held_ranges_and_merged(
        self,
        claimed: tuple[int, int],
        holding: list[tuple[int, int]],
        expected: tuple[tuple[int, int], ...],
    ) -> None:
        found = find_port_conflicts(
            [claim(*claimed)], [held(first, last) for first, last in holding]
        )

        assert spans_of(found) == ([expected] if expected else [])

    def test_ports_of_one_service_held_by_one_holder_form_one_conflict_with_the_span(self) -> None:
        found = find_port_conflicts([claim(8080), claim(8081), claim(8090)], [held(8080, 8090)])

        assert spans_of(found) == [((8080, 8081), (8090, 8090))]

    def test_two_services_claiming_one_held_port_are_two_conflicts(self) -> None:
        found = find_port_conflicts(
            [claim(PORT, service="a"), claim(PORT, service="b")], [held(PORT)]
        )

        assert [c.service for c in found.items] == ["a", "b"]

    def test_one_service_claiming_a_port_two_holders_hold_is_two_conflicts(self) -> None:
        first, second = holder(1, "first"), holder(2, "second")

        found = find_port_conflicts([claim(PORT)], [held(PORT, by=first), held(PORT, by=second)])

        assert [c.holder.name for c in found.items] == ["first", "second"]

    def test_a_holder_on_two_addresses_gives_one_conflict_per_address(self) -> None:
        found = find_port_conflicts(
            [claim(PORT)],
            [held(PORT, address=SPECIFIC), held(PORT, address=OTHER_SPECIFIC)],
        )

        assert sorted(c.address or "" for c in found.items) == [OTHER_SPECIFIC, SPECIFIC]

    def test_matching_stops_at_the_hit_limit_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(collisions, "MAX_HITS", 2)
        ports = range(10, 60, 10)

        found = find_port_conflicts([claim(p) for p in ports], [held(p) for p in ports])

        assert found.truncated and len(found.items) <= 2

    @pytest.mark.parametrize(
        ("sizes", "excess"),
        [
            pytest.param([], None, id="no-claims"),
            pytest.param([MAX_PORTS_CHECKED], None, id="exactly-the-cap"),
            pytest.param([MAX_PORTS_CHECKED + 1], 0, id="one-past-the-cap"),
            pytest.param([MAX_PORTS_CHECKED - 1, 1, 1], 2, id="third-claim-crosses"),
            pytest.param([512, 512], None, id="two-halves"),
        ],
    )
    def test_the_range_cap_names_the_claim_that_takes_the_total_past_it(
        self, sizes: list[int], excess: int | None
    ) -> None:
        claims = [claim(1, size, service=f"s{position}") for position, size in enumerate(sizes)]

        crossing = over_cap(claims)

        assert crossing is (None if excess is None else claims[excess])


class TestRefusalsAndWarnings:
    async def test_a_name_held_outside_the_project_refuses_with_the_service_and_holder(
        self,
    ) -> None:
        host = Host([listed(HOLDER_NUMBER, HOLDER_NAME)])

        report = await host.check(web(container_name=HOLDER_NAME))

        assert report.reason == REASON_NAME
        assert report.fields == {
            WEB_NAME: f"The container name {HOLDER_NAME} is already used by "
            f"the container {HOLDER_NAME} ({short_id(HOLDER_ID)})."
        }

    @pytest.mark.parametrize(
        ("label", "project", "refused"),
        [
            pytest.param("shop", "shop", False, id="same-spelling"),
            pytest.param("Shop", "shop", False, id="label-in-another-case"),
            pytest.param("shop", "SHOP", False, id="project-in-another-case"),
            pytest.param("shop2", "shop", True, id="another-project"),
            pytest.param(None, "shop", True, id="no-project-label"),
        ],
    )
    async def test_a_container_of_the_same_project_is_left_out_whatever_the_case(
        self, label: str | None, project: str, refused: bool
    ) -> None:
        labels = {} if label is None else {PROJECT_LABEL: label}
        host = Host([listed(HOLDER_NUMBER, HOLDER_NAME, ports=[binding(PORT)], labels=labels)])

        report = await host.check(
            web(publishing(PORT), container_name=HOLDER_NAME), project=project
        )

        assert bool(report.fields) is refused

    async def test_a_running_port_holder_refuses_with_port_protocol_address_and_holder(
        self,
    ) -> None:
        host = Host([listed(HOLDER_NUMBER, HOLDER_NAME, ports=[binding(PORT, SPECIFIC)])])

        report = await host.check(web(publishing(PORT)))

        assert report.reason == REASON_PORT
        assert report.fields == {
            WEB_PORTS: f"Port {PORT}/tcp on {SPECIFIC} is already published by "
            f"the container {HOLDER_NAME}."
        }

    @pytest.mark.parametrize(
        ("state", "refused"),
        [
            pytest.param("running", True, id="running"),
            pytest.param("paused", True, id="paused"),
            pytest.param("restarting", True, id="restarting"),
            pytest.param("exited", False, id="exited"),
            pytest.param("created", False, id="created"),
            pytest.param("dead", False, id="dead"),
        ],
    )
    async def test_only_a_container_that_is_running_or_coming_back_blocks(
        self, state: str, refused: bool
    ) -> None:
        listing = [binding(PORT)] if state in ("running", "paused") else []
        host = Host(
            [listed(HOLDER_NUMBER, HOLDER_NAME, state=state, ports=listing)],
            host_configs={HOLDER_ID: configured(PORT)},
        )

        report = await host.check(web(publishing(PORT)))

        assert (report.reason, len(report.warnings)) == ((REASON_PORT, 0) if refused else ("", 1))

    async def test_a_stopped_holder_is_a_warning_that_names_it_and_does_not_block(self) -> None:
        host = Host(
            [listed(HOLDER_NUMBER, HOLDER_NAME, state="exited")],
            host_configs={HOLDER_ID: configured(PORT)},
        )

        report = await host.check(web(publishing(PORT)))

        assert report.fields == {} and report.reason == ""
        assert report.warnings == (
            f"Service {WEB}: port {PORT}/tcp on every address is published by "
            f"the container {HOLDER_NAME}, which is not running.",
        )

    @pytest.mark.parametrize(
        ("claimed_ip", "listed_ip", "refused"),
        [
            pytest.param(None, "0.0.0.0", True, id="every-address-claim-meets-ipv4-wildcard"),
            pytest.param(SPECIFIC, "0.0.0.0", True, id="specific-claim-meets-wildcard"),
            pytest.param("::", SPECIFIC, True, id="ipv6-wildcard-claim-meets-ipv4"),
            pytest.param("::ffff:10.0.0.9", SPECIFIC, True, id="mapped-claim-meets-ipv4"),
            pytest.param(SPECIFIC, "::ffff:10.0.0.9", True, id="ipv4-claim-meets-mapped"),
            pytest.param("127.0.0.1", "::1", False, id="loopback-v4-against-v6"),
            pytest.param(SPECIFIC, OTHER_SPECIFIC, False, id="different-ipv4"),
        ],
    )
    async def test_addresses_are_compared_across_spellings_and_families(
        self, claimed_ip: str | None, listed_ip: str, refused: bool
    ) -> None:
        host = Host([listed(HOLDER_NUMBER, HOLDER_NAME, ports=[binding(PORT, listed_ip)])])

        report = await host.check(web(publishing(PORT, host_ip=claimed_ip)))

        assert (report.reason == REASON_PORT) is refused

    async def test_a_name_and_a_port_both_held_report_the_name_reason_first(self) -> None:
        host = Host([listed(HOLDER_NUMBER, HOLDER_NAME, ports=[binding(PORT)])])

        report = await host.check(web(publishing(PORT), container_name=HOLDER_NAME))

        assert report.reason == REASON_NAME
        assert set(report.fields) == {WEB_NAME, WEB_PORTS}

    async def test_a_port_held_across_several_spans_names_the_first_three(self) -> None:
        ports = [8001, 8003, 8005, 8007]
        host = Host([listed(HOLDER_NUMBER, HOLDER_NAME, ports=[binding(p) for p in ports])])

        report = await host.check(web(*(publishing(p) for p in ports)))

        assert report.fields[WEB_PORTS].startswith(
            "Ports 8001, 8003, 8005, .../tcp on every address"
        )

    async def test_a_range_over_the_cap_refuses_with_its_own_message_and_reads_nothing(
        self,
    ) -> None:
        host = Host([listed(HOLDER_NUMBER, HOLDER_NAME)])
        wide = f"9000-{9000 + MAX_PORTS_CHECKED}"

        report = await host.check({WEB: {"ports": [{"published": wide}]}})

        assert report.reason == REASON_RANGE
        assert report.fields == {WEB_PORTS: RANGE_TOO_LARGE}
        assert report.summary == f"Service {WEB} publishes more ports than can be checked."
        assert host.list_reads == 0

    async def test_a_held_name_is_still_checked_when_the_ports_are_too_many(self) -> None:
        host = Host([listed(HOLDER_NUMBER, HOLDER_NAME)])
        wide = {"published": f"9000-{9000 + MAX_PORTS_CHECKED}"}

        report = await host.check({WEB: {"ports": [wide], "container_name": HOLDER_NAME}})

        assert set(report.fields) == {WEB_NAME, WEB_PORTS}
        assert report.reason == REASON_NAME


class TestCaps:
    async def test_refusals_past_twenty_are_counted_in_an_and_more_line(self) -> None:
        total = MAX_REPORTED + BEYOND_CAP
        ports = [PORT + number for number in range(total)]
        rows = [
            listed(number, f"holder-{number}", ports=[binding(p)])
            for number, p in enumerate(ports, start=1)
        ]

        report = await Host(rows).check(web(*(publishing(p) for p in ports)))

        message = report.fields[WEB_PORTS]
        assert message.count("is already published by") == MAX_REPORTED
        assert message.endswith(f"and {BEYOND_CAP} more.")
        assert report.summary.count(f"Service {WEB} publishes") == MAX_REPORTED
        assert report.summary.endswith(f"and {BEYOND_CAP} more.")

    async def test_warnings_past_twenty_are_counted_in_an_and_more_entry(self) -> None:
        total = MAX_REPORTED + BEYOND_CAP
        ports = [PORT + number for number in range(total)]
        rows = [
            listed(number, f"holder-{number}", state="exited")
            for number, _ in enumerate(ports, start=1)
        ]
        configs = {container_id(n): configured(p) for n, p in enumerate(ports, start=1)}

        report = await Host(rows, host_configs=configs).check(web(*(publishing(p) for p in ports)))

        assert len(report.warnings) == MAX_REPORTED + 1
        assert report.warnings[-1] == f"and {BEYOND_CAP} more."

    async def test_stopped_containers_past_fifty_are_not_read_and_the_warning_has_no_count(
        self,
    ) -> None:
        total = MAX_STOPPED_INSPECTED + BEYOND_CAP
        rows = [
            listed(number, f"stopped-{number}", state="exited") for number in range(1, total + 1)
        ]
        host = Host(rows)

        report = await host.check(web(publishing(PORT)))

        assert len(host.inspected) == MAX_STOPPED_INSPECTED
        assert report.warnings == (STOPPED_UNCHECKED,)
        assert not any(character.isdigit() for character in STOPPED_UNCHECKED)

    async def test_every_inspected_container_is_forgotten_afterwards(self) -> None:
        host = Host([listed(HOLDER_NUMBER, HOLDER_NAME, state="exited")])

        await host.check(web(publishing(PORT)))

        assert host.forgotten == host.inspected == [HOLDER_ID]

    @pytest.mark.parametrize(
        ("claimed_index", "reported"),
        [
            pytest.param(0, True, id="within-the-cap"),
            pytest.param(BINDING_CAP, False, id="past-the-cap"),
        ],
    )
    async def test_bindings_past_the_per_container_cap_are_dropped_with_a_warning(
        self, claimed_index: int, reported: bool
    ) -> None:
        ports = [BINDING_BASE_PORT + index for index in range(BINDING_CAP + 1)]
        host = Host(
            [listed(HOLDER_NUMBER, HOLDER_NAME, state="exited")],
            host_configs={HOLDER_ID: configured(*ports)},
        )

        report = await host.check(web(publishing(ports[claimed_index])))

        assert BINDINGS_TRUNCATED in report.warnings
        assert (len(report.warnings) == 2) is reported


class TestUnreadableHosts:
    async def test_a_listing_over_the_cap_is_a_warning_and_nothing_is_refused(self) -> None:
        host = Host(listing_error=ResponseTooLarge(ENV_A, "too large"))

        report = await host.check(web(publishing(PORT), container_name=HOLDER_NAME))

        assert report.fields == {} and report.reason == ""
        assert report.warnings == (CONTAINERS_TOO_MANY,)

    @pytest.mark.parametrize(
        ("error", "warnings"),
        [
            pytest.param(EngineError(ENV_A, "boom"), (STOPPED_UNCHECKED,), id="engine-error"),
            pytest.param(RuntimeError("boom"), (STOPPED_UNCHECKED,), id="unexpected-error"),
            pytest.param(GONE, (), id="removed-since-the-listing"),
        ],
    )
    async def test_an_inspect_failure_is_a_warning_not_an_exception(
        self, error: Exception, warnings: tuple[str, ...]
    ) -> None:
        host = Host(
            [listed(HOLDER_NUMBER, HOLDER_NAME, state="exited")],
            inspect_errors={HOLDER_ID: error},
        )

        report = await host.check(web(publishing(PORT)))

        assert report.fields == {} and report.warnings == warnings

    async def test_a_file_with_nothing_to_check_reads_nothing(self) -> None:
        host = Host([listed(HOLDER_NUMBER, HOLDER_NAME)])

        report = await host.check({WEB: {"image": "nginx:1"}})

        assert report == CollisionReport() and host.list_reads == 0


class TestWhatACallerWhoCannotSeeTheHolderLearns:
    async def test_a_hidden_name_holder_is_another_container_without_its_id(self) -> None:
        host = Host([listed(HOLDER_NUMBER, HOLDER_NAME)])

        report = await host.check(web(container_name=HOLDER_NAME), actor=STANDARD)

        assert report.fields == {
            WEB_NAME: f"The container name {HOLDER_NAME} is already used by another container."
        }
        assert short_id(HOLDER_ID) not in report.fields[WEB_NAME]

    @pytest.mark.parametrize(
        ("visible", "address_shown"),
        [
            pytest.param({HOLDER_ID}, SPECIFIC, id="visible-holder-gives-its-address"),
            pytest.param(set(), EVERY_ADDRESS, id="hidden-holder-gives-the-claims-address"),
        ],
    )
    async def test_a_hidden_port_holder_is_named_generically_and_its_address_stays_unsaid(
        self, visible: set[str], address_shown: str
    ) -> None:
        host = Host(
            [listed(HOLDER_NUMBER, HOLDER_NAME, ports=[binding(PORT, SPECIFIC)])],
            visible=visible,
        )

        report = await host.check(web(publishing(PORT)), actor=STANDARD)

        message = report.fields[WEB_PORTS]
        assert f"on {address_shown} is already published by" in message
        assert (HOLDER_NAME in message) is bool(visible)
        assert (SPECIFIC in message) is bool(visible)
        assert ("another container" in message) is not bool(visible)

    async def test_hidden_stopped_holders_fold_into_one_generic_warning(self) -> None:
        rows = [listed(number, f"hidden-{number}", state="exited") for number in (1, 2, 3)]
        configs = {container_id(number): configured(PORT) for number in (1, 2, 3)}
        host = Host(rows, host_configs=configs)

        report = await host.check(web(publishing(PORT)), actor=STANDARD)

        assert report.warnings == (
            f"Service {WEB}: a container you cannot see has a stopped port binding that "
            "overlaps the ports this service publishes.",
        )

    async def test_the_summary_carries_the_service_protocol_and_port_and_nothing_of_the_holder(
        self,
    ) -> None:
        host = Host([listed(HOLDER_NUMBER, HOLDER_NAME, ports=[binding(PORT, SPECIFIC)])])

        report = await host.check(web(publishing(PORT), container_name=HOLDER_NAME))

        assert report.summary == (
            f"Service {WEB} sets a container name that is already in use. "
            f"Service {WEB} publishes tcp port {PORT} that is already published."
        )
        assert not {HOLDER_NAME, SPECIFIC, short_id(HOLDER_ID)} & set(report.summary.split())


class TestSecretsInMessages:
    async def test_a_secret_value_in_a_holder_name_is_redacted_everywhere_it_is_reported(
        self,
    ) -> None:
        name = f"web-{SECRET}"
        rows = [
            listed(1, name, ports=[binding(PORT)]),
            listed(2, f"old-{SECRET}", state="exited"),
        ]
        host = Host(rows, host_configs={container_id(2): configured(PORT + 1)})
        scrubber = Scrubber()
        scrubber.register_secret(SECRET)

        report = await host.check(
            web(publishing(PORT), publishing(PORT + 1), container_name=name), scrubber=scrubber
        )

        everything = " ".join([*report.fields.values(), *report.warnings, report.summary])
        assert SECRET not in everything
        assert REDACTED in " ".join(report.fields.values())
        assert REDACTED in " ".join(report.warnings)
