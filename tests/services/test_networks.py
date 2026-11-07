"""The network query and action services over a fake daemon and the seeded database."""

import asyncio
from typing import Any

import pytest

import app.services.networks as networks_module
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.services.access import ResourceKey
from app.services.activity import UserActor
from app.services.assembly import NetworkServices
from app.services.container_actions import EngineRefused
from app.services.network_addressing import AddressingInvalid
from app.services.networks import (
    NetworkChangedSinceReview,
    NetworkFilters,
    NetworkGone,
    NetworkInUse,
    NetworkNameInUse,
    NetworkPredefined,
    NetworkPruneTarget,
    NetworkRefused,
    RestrictedNetwork,
)
from app.services.object_inspector import EnvironmentUnavailable
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.resource_controls import ResourceControlService
from tests.access_support import (
    ADMIN,
    ENV_A,
    ENV_B,
    PEER,
    STANDARD,
    control,
    stack_key,
)
from tests.docker_engine.container_engine import (
    ENGINE_TEXT,
    HOST_NETWORK_ID,
    ContainerEngine,
    created_network_id,
    swarm_network_id,
)
from tests.docker_engine.fake_engine import Stall
from tests.object_support import (
    STACK_LABEL,
    SWARM_LABEL,
    add_container,
    drain_background,
    rows_of,
)
from tests.services.conftest import ContainerRig

SUBNET = "10.77.0.0/24"
GATEWAY = "10.77.0.1"
MTU_KEY = "com.docker.network.driver.mtu"
BRIDGE_NAME_KEY = "com.docker.network.bridge.name"
BRIDGE_NAME_VALUE = "br-private-1"
IN_USE_ERRORS = (NetworkInUse, NetworkChangedSinceReview)
MAX_PRUNE = 100
NAME_LIMIT = 20
SMALL_SETTLE = 0.05


def names(page: Any) -> list[str]:
    return [row.name for row in page.page.items]


def requests_of(host: ContainerEngine, method: str) -> list[str]:
    return [p for p in host.paths() if p.startswith(f"{method} /networks")]


def id_of(host: ContainerEngine, name: str) -> str:
    return next(i for i, n in host.networks.items() if n["Name"] == name)


@pytest.fixture
def catalogue(host: ContainerEngine) -> dict[str, str]:
    """A stack bridge, a macvlan, a Swarm overlay, a system network and a plugin-driven one."""
    return {
        "alpha": host.add_network("alpha", 0x11, Labels={STACK_LABEL: "Shop"}),
        "beta": host.add_network("beta", 0x12, "macvlan"),
        "ov": host.add_network(
            "ov", 0, "overlay", network_id=swarm_network_id(5), Scope="swarm", Attachable=True
        ),
        "gw": host.add_network("docker_gwbridge", 0x13),
        "weird": host.add_network("weird", 0x14, "weave"),
        "ingress": host.add_network(
            "ingress", 0, "overlay", network_id=swarm_network_id(6), Scope="swarm", Ingress=True
        ),
    }


EVERYTHING = {
    "bridge",
    "host",
    "none",
    "alpha",
    "beta",
    "ov",
    "docker_gwbridge",
    "weird",
    "ingress",
}


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (NetworkFilters(), EVERYTHING),
        (NetworkFilters(q="  "), EVERYTHING),
        (NetworkFilters(q="ALP"), {"alpha"}),
        (NetworkFilters(driver="macvlan"), {"beta"}),
        (NetworkFilters(driver="overlay"), {"ov", "ingress"}),
        (NetworkFilters(driver="other"), {"weird", "host", "none"}),
        (NetworkFilters(scope="swarm"), {"ov", "ingress"}),
        (NetworkFilters(usage="used"), {"beta"}),
        (NetworkFilters(kind="predefined"), {"bridge", "host", "none"}),
        (NetworkFilters(kind="system"), {"docker_gwbridge", "ingress"}),
        (NetworkFilters(stack="sHoP"), {"alpha"}),
        (NetworkFilters(stack="shop", driver="bridge", usage="unused"), {"alpha"}),
    ],
)
async def test_filters_narrow_the_decoded_list_by_name_driver_scope_use_kind_and_stack(
    nets: NetworkServices,
    host: ContainerEngine,
    catalogue: dict[str, str],
    filters: NetworkFilters,
    expected: set[str],
) -> None:
    add_container(host, 1, "user", networks={"beta": catalogue["beta"]})

    page = await nets.queries.page(ADMIN, ENV_A, filters)

    assert set(names(page)) == expected
    assert page.total == len(expected)


async def test_the_list_orders_by_casefolded_name_and_pages_with_a_cursor_over_both_id_shapes(
    nets: NetworkServices, host: ContainerEngine, catalogue: dict[str, str]
) -> None:
    first = await nets.queries.page(ADMIN, ENV_A, limit=4)
    second = await nets.queries.page(ADMIN, ENV_A, limit=4, cursor=first.page.next_cursor)
    third = await nets.queries.page(ADMIN, ENV_A, limit=4, cursor=second.page.next_cursor)

    walked = names(first) + names(second) + names(third)
    assert walked == sorted(walked, key=str.casefold)
    assert len(walked) == len(set(walked)) == 9
    assert third.page.next_cursor is None
    assert first.total == 9


@pytest.mark.parametrize(
    ("filters", "field"),
    [
        (NetworkFilters(driver="weird-driver"), "driver"),
        (NetworkFilters(scope="global"), "scope"),
        (NetworkFilters(usage="sometimes"), "usage"),
        (NetworkFilters(kind="mine"), "kind"),
        (NetworkFilters(q="x" * 129), "q"),
        (NetworkFilters(stack="x" * 129), "stack"),
    ],
)
async def test_an_invalid_filter_is_refused_on_its_own_field_before_the_host_is_read(
    nets: NetworkServices, host: ContainerEngine, filters: NetworkFilters, field: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await nets.queries.page(ADMIN, ENV_A, filters)

    assert field in refused.value.fields
    assert host.requests == []


@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": 501}, {"cursor": "garbage"}])
async def test_an_invalid_page_size_or_cursor_is_refused(
    nets: NetworkServices, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ValidationFailed):
        await nets.queries.page(ADMIN, ENV_A, **arguments)


async def test_predefined_networks_are_listed_with_their_kind_and_cannot_be_removed(
    nets: NetworkServices, host: ContainerEngine, catalogue: dict[str, str]
) -> None:
    page = await nets.queries.page(ADMIN, ENV_A)

    by_name = {row.name: row for row in page.page.items}
    assert {n for n in ("bridge", "host", "none") if by_name[n].kind == "predefined"} == {
        "bridge",
        "host",
        "none",
    }
    assert not any(by_name[n].can_remove for n in ("bridge", "host", "none", "ingress"))
    assert by_name["docker_gwbridge"].kind == "system"
    assert by_name["alpha"].can_remove and by_name["ov"].scope == "swarm"


async def test_an_administrator_sees_the_name_of_every_container_using_a_network(
    nets: NetworkServices, host: ContainerEngine, catalogue: dict[str, str]
) -> None:
    add_container(host, 1, "mine", networks={"alpha": catalogue["alpha"]})
    add_container(host, 2, "theirs", networks={"alpha": catalogue["alpha"]})
    page = await nets.queries.page(ADMIN, ENV_A, NetworkFilters(q="alpha"))

    (row,) = page.page.items
    assert row.in_use == 2 and [u.name for u in row.containers] == ["mine", "theirs"]
    assert (row.containers_more, row.containers_hidden) == (0, 0)


async def test_a_standard_user_sees_the_networks_a_grant_or_a_stack_reaches_and_no_others(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, catalogue: dict[str, str]
) -> None:
    await control(rig.access, ResourceKey(ENV_A, "network", catalogue["beta"]), public=True)
    await control(rig.access, stack_key("shop"), public=True)
    add_container(host, 1, "hidden-c", networks={"beta": catalogue["beta"]})

    page = await nets.queries.page(STANDARD, ENV_A)

    assert set(names(page)) == {"alpha", "beta"}
    assert page.visible == 2 and page.total == 2
    beta = next(row for row in page.page.items if row.name == "beta")
    assert (beta.in_use, beta.containers, beta.containers_hidden) == (1, (), 1)
    assert "hidden-c" not in repr(page)


@pytest.mark.parametrize(
    ("labels", "visible"),
    [
        ({STACK_LABEL: "shop"}, True),
        ({STACK_LABEL: "ShOp"}, True),
        ({SWARM_LABEL: "SHOP"}, True),
        ({"Com.Docker.Compose.Project": "shop"}, False),
        ({STACK_LABEL: "other"}, False),
        ({}, False),
    ],
)
async def test_a_network_is_visible_through_the_stack_label_in_any_case_of_its_value_only(
    rig: ContainerRig,
    nets: NetworkServices,
    host: ContainerEngine,
    labels: dict[str, str],
    visible: bool,
) -> None:
    network_id = host.add_network("probe", 0x21, Labels=labels)
    await control(rig.access, stack_key("shop"), public=True)

    page = await nets.queries.page(STANDARD, ENV_A)

    assert (network_id in [row.id for row in page.page.items]) is visible


async def test_a_networks_own_control_wins_over_the_stack_it_belongs_to(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    inherited = host.add_network("inherited", 0x22, Labels={STACK_LABEL: "shop"})
    overridden = host.add_network("overridden", 0x23, Labels={STACK_LABEL: "shop"})
    await control(rig.access, stack_key("shop"), public=True)
    await control(rig.access, ResourceKey(ENV_A, "network", overridden), administrators_only=True)

    page = await nets.queries.page(STANDARD, ENV_A)

    assert [row.id for row in page.page.items] == [inherited]
    with pytest.raises(ResourceNotFound):
        await nets.queries.detail(STANDARD, ENV_A, overridden)


@pytest.mark.parametrize("actor", [ADMIN, STANDARD])
@pytest.mark.parametrize(
    "network_id",
    [
        "e" * 64,
        "e" * 63,
        "e" * 65,
        "E" * 64,
        "abcdefghijklmnopqrstuvwxy",
        "backend",
        "../x",
        "",
        " " + "e" * 63,
    ],
)
async def test_a_missing_or_malformed_network_id_is_not_found_for_every_role(
    nets: NetworkServices, host: ContainerEngine, actor: Any, network_id: str
) -> None:
    with pytest.raises(ResourceNotFound):
        await nets.queries.detail(actor, ENV_A, network_id)
    with pytest.raises(ResourceNotFound):
        await nets.queries.remove_impact(actor, ENV_A, network_id)


async def test_a_hidden_network_and_a_missing_one_are_the_same_not_found_for_a_standard_user(
    nets: NetworkServices, host: ContainerEngine, catalogue: dict[str, str]
) -> None:
    with pytest.raises(ResourceNotFound) as hidden:
        await nets.queries.detail(STANDARD, ENV_A, catalogue["beta"])
    with pytest.raises(ResourceNotFound) as missing:
        await nets.queries.detail(STANDARD, ENV_A, "e" * 64)

    assert str(hidden.value) == str(missing.value)


async def test_an_id_prefix_or_a_name_does_not_reach_a_network(
    nets: NetworkServices, host: ContainerEngine, catalogue: dict[str, str]
) -> None:
    with pytest.raises(ResourceNotFound):
        await nets.queries.detail(ADMIN, ENV_A, catalogue["alpha"][:25])
    with pytest.raises(ResourceNotFound):
        await nets.queries.detail(ADMIN, ENV_A, "alpha")


async def test_the_detail_separates_running_from_stopped_users_and_withholds_options_from_standard(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    network_id = host.add_network(
        "app",
        0x31,
        Options={"parent": "eth0", "secret_token": "s3cr3t"},
        Labels={"team": "core", "api_key": "k-1"},
        IPAM={"Config": [{"Subnet": SUBNET, "Gateway": GATEWAY}]},
    )
    add_container(host, 1, "up", state="running", networks={"app": network_id})
    add_container(host, 2, "down", state="exited", networks={"app": network_id})
    await control(rig.access, ResourceKey(ENV_A, "network", network_id), public=True)

    admin = await nets.queries.detail(ADMIN, ENV_A, network_id)
    standard = await nets.queries.detail(STANDARD, ENV_A, network_id)

    assert [e.name for e in admin.endpoints] == ["up"] and [s.name for s in admin.stopped] == [
        "down"
    ]
    assert admin.in_use == 2 and [(p.subnet, p.gateway) for p in admin.pools] == [(SUBNET, GATEWAY)]
    assert {(o.key, o.redacted) for o in admin.options or ()} == {
        ("parent", False),
        ("secret_token", True),
    }
    assert standard.options is None and not standard.can_manage_access
    assert [(v.key, v.redacted) for v in standard.labels] == [("api_key", True), ("team", False)]
    assert (
        standard.endpoints,
        standard.stopped,
        standard.endpoints_hidden,
        standard.stopped_hidden,
    ) == ((), (), 1, 1)
    assert "s3cr3t" not in repr(admin) + repr(standard)


async def test_a_users_view_cuts_the_names_at_ten_and_counts_the_cut_apart_from_the_hidden(
    nets: NetworkServices, host: ContainerEngine
) -> None:
    network_id = host.add_network("crowded", 0x32)
    for number in range(1, 15):
        add_container(host, number, f"c{number:02d}", networks={"crowded": network_id})

    row = (await nets.queries.page(ADMIN, ENV_A, NetworkFilters(q="crowded"))).page.items[0]

    assert (len(row.containers), row.containers_more, row.containers_hidden, row.in_use) == (
        10,
        4,
        0,
        14,
    )


async def test_a_never_started_container_counts_as_a_user_of_the_network_it_is_configured_with(
    nets: NetworkServices, host: ContainerEngine
) -> None:
    network_id = host.add_network("usednet", 0x91)
    add_container(host, 0x91, "never-started", state="created", networks={"usednet": network_id})

    impact = await nets.queries.remove_impact(ADMIN, ENV_A, network_id)
    detail = await nets.queries.detail(ADMIN, ENV_A, network_id)
    row = (await nets.queries.page(ADMIN, ENV_A, NetworkFilters(q="usednet"))).page.items[0]
    plan = await nets.queries.prune_plan(ADMIN, ENV_A)

    assert impact.in_use == 1 and [u.name for u in impact.containers] == ["never-started"]
    assert detail.in_use == 1 and [u.name for u in detail.stopped] == ["never-started"]
    assert row.in_use == 1
    assert network_id not in [c.id for c in plan.candidates]


async def test_a_never_started_container_blocks_removal_and_prune(
    nets: NetworkServices, host: ContainerEngine
) -> None:
    network_id = host.add_network("usednet", 0x92)
    add_container(host, 0x92, "never-started", state="created", networks={"usednet": network_id})

    with pytest.raises(IN_USE_ERRORS):
        await nets.actions.remove(
            ADMIN, ENV_A, network_id, expected_name="usednet", expected_in_use=0
        )
    result = await nets.actions.run_prune(ADMIN, ENV_A, [NetworkPruneTarget(network_id, "usednet")])

    assert [i.status for i in result.items] == ["skipped_changed"]
    assert network_id in host.networks


async def test_a_container_that_only_names_the_network_as_its_mode_does_not_use_it(
    nets: NetworkServices, host: ContainerEngine
) -> None:
    network_id = host.add_network("modenet", 0x93)
    box = add_container(host, 0x93, "mode-only", state="running")
    box.endpoints = {}
    box.host_config = {"NetworkMode": "modenet"}

    impact = await nets.queries.remove_impact(ADMIN, ENV_A, network_id)
    plan = await nets.queries.prune_plan(ADMIN, ENV_A)
    await nets.actions.remove(ADMIN, ENV_A, network_id, expected_name="modenet", expected_in_use=0)

    assert impact.in_use == 0
    assert network_id in [c.id for c in plan.candidates]
    assert network_id not in host.networks


@pytest.mark.parametrize(
    ("actor", "expected"),
    [
        pytest.param(
            ADMIN, ["bridge", "host", "mine", "none", "readable", "theirs"], id="administrator"
        ),
        pytest.param(STANDARD, ["host", "mine", "readable"], id="standard"),
    ],
)
async def test_names_list_the_networks_the_caller_may_see_sorted_by_name(
    rig: ContainerRig,
    nets: NetworkServices,
    host: ContainerEngine,
    actor: Any,
    expected: list[str],
) -> None:
    mine = host.add_network("mine", 0x41)
    host.add_network("theirs", 0x42)
    readable = host.add_network("readable", 0x43)
    await control(rig.access, ResourceKey(ENV_A, "network", mine), public=True, level="write")
    await control(rig.access, ResourceKey(ENV_A, "network", readable), public=True, level="read")
    await control(
        rig.access, ResourceKey(ENV_A, "network", HOST_NETWORK_ID), public=True, level="write"
    )

    found = await nets.queries.names(actor, ENV_A, "", limit=NAME_LIMIT)

    assert found == expected


async def test_names_keep_only_the_prefix_ignoring_case_and_stop_at_the_limit(
    nets: NetworkServices, host: ContainerEngine
) -> None:
    host.add_network("Web-b", 0x41)
    host.add_network("web-a", 0x42)
    host.add_network("db", 0x43)
    for number in range(30):
        host.add_network(f"n{number:02d}", 0x1000 + number)

    prefixed = await nets.queries.names(ADMIN, ENV_A, "WEB", limit=NAME_LIMIT)
    limited = await nets.queries.names(ADMIN, ENV_A, "n", limit=NAME_LIMIT)

    assert prefixed == ["web-a", "Web-b"]
    assert limited == [f"n{number:02d}" for number in range(NAME_LIMIT)]


async def test_names_read_the_host_network_list_once(
    nets: NetworkServices, host: ContainerEngine
) -> None:
    host.add_network("web", 0x41)

    await nets.queries.names(ADMIN, ENV_A, "w", limit=NAME_LIMIT)

    assert requests_of(host, "GET") == ["GET /networks"]


async def test_a_standard_user_on_a_local_socket_reads_but_cannot_create_or_remove(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    network_id = host.add_network("local-net", 0x61)
    await control(rig.access, ResourceKey(ENV_B, "network", network_id), public=True, level="write")

    page = await nets.queries.page(STANDARD, ENV_B)

    assert "local-net" in names(page) and not page.can_create
    assert not any(row.can_remove for row in page.page.items)
    assert await nets.queries.names(STANDARD, ENV_B, "local", limit=NAME_LIMIT) == ["local-net"]
    with pytest.raises(PermissionDenied):
        await nets.queries.require_create(STANDARD, ENV_B)
    with pytest.raises(PermissionDenied):
        await nets.actions.create(STANDARD, ENV_B, {"name": "nope", "driver": "bridge"})
    with pytest.raises(PermissionDenied):
        await nets.actions.remove(
            STANDARD, ENV_B, network_id, expected_name="local-net", expected_in_use=0
        )
    assert await rows_of(rig.db, "network_created") == []
    assert await rows_of(rig.db, "network_removed") == []
    assert requests_of(host, "POST") == [] and requests_of(host, "DELETE") == []


async def test_create_requires_an_environment_the_caller_may_reach(
    nets: NetworkServices,
) -> None:
    with pytest.raises(ResourceNotFound):
        await nets.queries.require_create(STANDARD, 9999)


async def test_an_administrator_creates_a_network_with_addressing_options_and_labels(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    form = {
        "name": "app-net",
        "driver": "bridge",
        "internal": "on",
        "subnet_v4": SUBNET,
        "gateway_v4": GATEWAY,
        "ip_range_v4": "10.77.0.128/25",
        "option-0-key": BRIDGE_NAME_KEY,
        "option-0-value": BRIDGE_NAME_VALUE,
        "option-1-key": MTU_KEY,
        "option-1-value": "1400",
        "label-0-key": "team",
        "label-0-value": "core",
    }

    created = await nets.actions.create(ADMIN, ENV_A, form)

    assert created.network_id == created_network_id(1) and created.control_created
    assert host.network_bodies("create") == [
        {
            "Name": "app-net",
            "Driver": "bridge",
            "Internal": True,
            "EnableIPv6": False,
            "IPAM": {
                "Driver": "default",
                "Config": [{"Subnet": SUBNET, "Gateway": GATEWAY, "IPRange": "10.77.0.128/25"}],
            },
            "Options": {BRIDGE_NAME_KEY: BRIDGE_NAME_VALUE, MTU_KEY: "1400"},
            "Labels": {"team": "core"},
        }
    ]
    (row,) = await rows_of(rig.db, "network_created")
    assert (row["result"], row["target_type"], row["target_id"]) == (
        "success",
        "network",
        created.network_id,
    )
    detail = dict(row["detail"])
    restricted = detail.pop("restricted")
    assert detail == {
        "name": "app-net",
        "driver": "bridge",
        "scope": "local",
        "internal": True,
        "attachable": False,
        "ipv6": False,
        "subnets": SUBNET,
        "gateways": GATEWAY,
        "ip_ranges": "10.77.0.128/25",
        "option_keys": f"{BRIDGE_NAME_KEY},{MTU_KEY}",
        "labels": 1,
        "control_created": True,
    }
    assert restricted == f"addressing,{BRIDGE_NAME_KEY}"
    assert BRIDGE_NAME_VALUE not in str(row)
    owned = await rig.access.controls.get(ADMIN, ResourceKey(ENV_A, "network", created.network_id))
    assert owned is not None and owned.owner_user_id == ADMIN.user_id


async def test_a_standard_user_creates_a_plain_bridge_network_and_owns_it(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    form = {"name": "mine", "driver": "bridge", "option-0-key": MTU_KEY, "option-0-value": "1400"}

    created = await nets.actions.create(STANDARD, ENV_A, form)

    owned = await rig.access.controls.get(ADMIN, ResourceKey(ENV_A, "network", created.network_id))
    assert owned is not None and owned.owner_user_id == STANDARD.user_id
    (row,) = await rows_of(rig.db, "network_created")
    assert "restricted" not in row["detail"]
    page = await nets.queries.page(STANDARD, ENV_A)
    assert names(page) == ["mine"] and page.page.items[0].can_remove


@pytest.mark.parametrize(
    ("form", "field"),
    [
        (
            {
                "driver": "macvlan",
                "subnet_v4": "10.78.0.0/24",
                "option-0-key": "parent",
                "option-0-value": "eth0",
            },
            "driver",
        ),
        ({"driver": "ipvlan", "subnet_v4": "10.78.0.0/24"}, "driver"),
        ({"driver": "bridge", "subnet_v4": "10.78.0.0/24"}, "subnet_v4"),
        (
            {"driver": "bridge", "subnet_v4": "10.78.0.0/24", "gateway_v4": "10.78.0.1"},
            "gateway_v4",
        ),
        (
            {"driver": "bridge", "subnet_v4": "10.78.0.0/24", "ip_range_v4": "10.78.0.0/25"},
            "ip_range_v4",
        ),
        ({"driver": "bridge", "enable_ipv6": "on", "subnet_v6": "fd00:9::/64"}, "subnet_v6"),
        (
            {"driver": "bridge", "option-0-key": BRIDGE_NAME_KEY, "option-0-value": "x"},
            "option-0-key",
        ),
        (
            {
                "driver": "bridge",
                "option-0-key": "com.docker.network.bridge.gateway_mode_ipv4",
                "option-0-value": "routed",
            },
            "option-0-key",
        ),
        (
            {
                "driver": "bridge",
                "option-0-key": "com.docker.network.bridge.host_binding_ipv4",
                "option-0-value": "0.0.0.0",
            },
            "option-0-key",
        ),
        (
            {
                "driver": "bridge",
                "option-0-key": "com.docker.network.bridge.trusted_host_interfaces",
                "option-0-value": "eth0",
            },
            "option-0-key",
        ),
        (
            {
                "driver": "bridge",
                "option-0-key": "com.docker.network.bridge.inhibit_ipv4",
                "option-0-value": "true",
            },
            "option-0-key",
        ),
        (
            {
                "driver": "bridge",
                "option-0-key": "COM.DOCKER.NETWORK.BRIDGE.ENABLE_ICC",
                "option-0-value": "true",
            },
            "option-0-key",
        ),
        (
            {"driver": "bridge", "option-0-key": "encrypted", "option-0-value": "true"},
            "option-0-key",
        ),
        (
            {
                "driver": "bridge",
                "option-0-key": MTU_KEY,
                "option-0-value": "1500",
                "option-1-key": BRIDGE_NAME_KEY,
                "option-1-value": "x",
            },
            "option-1-key",
        ),
    ],
)
async def test_a_standard_user_is_refused_what_only_administrators_may_set(
    rig: ContainerRig,
    nets: NetworkServices,
    host: ContainerEngine,
    form: dict[str, str],
    field: str,
) -> None:
    with pytest.raises(RestrictedNetwork) as refused:
        await nets.actions.create(STANDARD, ENV_A, {"name": "nope"} | form)

    assert field in refused.value.fields
    assert host.network_bodies("create") == []
    (row,) = await rows_of(rig.db, "network_created")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "restricted")
    assert field in row["detail"]["fields"]


@pytest.mark.parametrize("actor", [ADMIN, STANDARD])
@pytest.mark.parametrize("driver", ["host", "none", "null", "weave"])
async def test_the_host_and_none_drivers_are_never_creatable_by_anyone(
    nets: NetworkServices, host: ContainerEngine, actor: Any, driver: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await nets.actions.create(actor, ENV_A, {"name": "nope", "driver": driver})

    assert "driver" in refused.value.fields
    assert host.network_bodies("create") == []


@pytest.mark.parametrize("actor", [ADMIN, STANDARD])
@pytest.mark.parametrize(
    "key",
    [
        STACK_LABEL,
        SWARM_LABEL,
        STACK_LABEL.upper(),
        f" {STACK_LABEL} ",
        f"{SWARM_LABEL}\t",
    ],
)
async def test_a_network_cannot_be_created_carrying_an_inheritance_label_in_any_spelling(
    nets: NetworkServices, host: ContainerEngine, actor: Any, key: str
) -> None:
    form = {"name": "lab", "driver": "bridge", "label-0-key": key, "label-0-value": "shop"}

    with pytest.raises(ValidationFailed) as refused:
        await nets.actions.create(actor, ENV_A, form)

    assert set(refused.value.fields) & {"labels", "label-0-key"}
    assert host.network_bodies("create") == []


@pytest.mark.parametrize(
    "form",
    [
        {"name": "", "driver": "bridge"},
        {"name": "bridge", "driver": "bridge"},
        {"name": "a" * 64, "driver": "bridge"},
        {"name": "ok", "driver": "bridge", "subnet_v4": "127.0.0.0/24"},
        {"name": "ok", "driver": "macvlan"},
    ],
)
async def test_an_invalid_form_is_refused_with_a_failure_row_and_no_engine_call(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, form: dict[str, str]
) -> None:
    with pytest.raises(ValidationFailed):
        await nets.actions.create(ADMIN, ENV_A, form)

    assert host.network_bodies("create") == []
    (row,) = await rows_of(rig.db, "network_created")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "invalid")


async def test_a_name_already_on_the_host_is_refused_before_and_after_the_race(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    host.add_network("taken", 0x51)

    with pytest.raises(NetworkNameInUse):
        await nets.actions.create(ADMIN, ENV_A, {"name": "taken", "driver": "bridge"})
    host.fail("network_create", 409)
    with pytest.raises(NetworkNameInUse):
        await nets.actions.create(ADMIN, ENV_A, {"name": "late", "driver": "bridge"})

    reasons = [r["detail"]["reason"] for r in await rows_of(rig.db, "network_created")]
    assert reasons == ["name_in_use", "name_in_use"]


async def test_a_subnet_overlapping_another_network_is_refused_naming_that_network(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    host.add_network("existing", 0x52, IPAM={"Config": [{"Subnet": "10.77.0.0/16"}]})

    with pytest.raises(AddressingInvalid) as refused:
        await nets.actions.create(
            ADMIN, ENV_A, {"name": "clash", "driver": "bridge", "subnet_v4": SUBNET}
        )

    assert "existing" in refused.value.errors["subnet_v4"]
    assert host.network_bodies("create") == []


async def test_an_overlay_is_created_on_a_swarm_manager_and_is_scoped_to_the_swarm(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    await rig.db.execute("UPDATE environment SET type = 'swarm' WHERE id = ?", (ENV_A,))
    form = {"name": "ov", "driver": "overlay", "attachable": "on"}

    created = await nets.actions.create(STANDARD, ENV_A, form)

    (body,) = host.network_bodies("create")
    assert body["Driver"] == "overlay" and body["Attachable"] is True
    (row,) = await rows_of(rig.db, "network_created")
    assert row["detail"]["scope"] == "swarm" and created.driver == "overlay"


async def test_an_overlay_is_not_created_on_a_standalone_host(
    nets: NetworkServices, host: ContainerEngine
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await nets.actions.create(ADMIN, ENV_A, {"name": "ov", "driver": "overlay"})

    assert "driver" in refused.value.fields


@pytest.mark.parametrize(
    ("status", "error", "reason"),
    [(500, NetworkRefused, "engine_rejected"), (400, NetworkRefused, "engine_rejected")],
)
async def test_an_engine_refusal_of_a_create_is_a_product_error_with_no_engine_text(
    rig: ContainerRig,
    nets: NetworkServices,
    host: ContainerEngine,
    status: int,
    error: type[Exception],
    reason: str,
) -> None:
    host.fail("network_create", status)

    with pytest.raises(error) as refused:
        await nets.actions.create(ADMIN, ENV_A, {"name": "nope", "driver": "bridge"})

    assert ENGINE_TEXT not in str(refused.value)
    (row,) = await rows_of(rig.db, "network_created")
    assert row["detail"]["reason"] == reason and ENGINE_TEXT not in str(row)


async def test_a_create_that_times_out_is_an_unknown_outcome(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    host.route("POST", "/networks/create", Stall())

    with pytest.raises(EnvironmentUnavailable):
        await nets.actions.create(ADMIN, ENV_A, {"name": "slow", "driver": "bridge"})

    (row,) = await rows_of(rig.db, "network_created")
    assert row["detail"]["reason"] == "outcome_unknown"


async def test_a_failed_claim_leaves_the_network_in_place_uncontrolled_and_says_so_in_the_row(
    rig: ContainerRig,
    nets: NetworkServices,
    host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing(self: ResourceControlService, key: ResourceKey, owner: int | None) -> bool:
        raise RuntimeError("claim failed")

    monkeypatch.setattr(ResourceControlService, "claim", failing)

    created = await nets.actions.create(STANDARD, ENV_A, {"name": "mine", "driver": "bridge"})

    assert not created.control_created and created.network_id in host.networks
    (row,) = await rows_of(rig.db, "network_created")
    assert (row["result"], row["detail"]["control_created"]) == ("success", False)


async def test_a_create_cancelled_while_the_engine_works_still_claims_and_records_the_network(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    gate = host.hold("network_create")
    task = asyncio.ensure_future(
        nets.actions.create(STANDARD, ENV_A, {"name": "slow", "driver": "bridge"})
    )
    await gate.entered.wait()
    task.cancel()
    gate.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    (row,) = await rows_of(rig.db, "network_created")
    assert (row["result"], row["detail"]["interrupted"]) == ("success", True)
    owned = await rig.access.controls.get(ADMIN, ResourceKey(ENV_A, "network", row["target_id"]))
    assert owned is not None and owned.owner_user_id == STANDARD.user_id


async def test_a_create_that_outlives_the_wait_is_recorded_unknown_and_settled_late(
    rig: ContainerRig,
    nets: NetworkServices,
    host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(networks_module, "SETTLE_SECONDS", SMALL_SETTLE)
    gate = host.hold("network_create")
    task = asyncio.ensure_future(
        nets.actions.create(STANDARD, ENV_A, {"name": "slow", "driver": "bridge"})
    )
    await gate.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.release.set()
    await drain_background()

    first, second = await rows_of(rig.db, "network_created")
    assert (first["result"], first["detail"]["reason"]) == ("failure", "outcome_unknown")
    assert (second["result"], second["detail"]["settled_late"]) == ("success", True)
    owned = await rig.access.controls.get(ADMIN, ResourceKey(ENV_A, "network", second["target_id"]))
    assert owned is not None


async def test_a_network_is_removed_after_review_and_its_control_is_released(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    created = await nets.actions.create(STANDARD, ENV_A, {"name": "mine", "driver": "bridge"})
    impact = await nets.queries.remove_impact(STANDARD, ENV_A, created.network_id)

    removed = await nets.actions.remove(
        STANDARD,
        ENV_A,
        created.network_id,
        expected_name=impact.name,
        expected_in_use=impact.in_use,
    )

    assert removed.control_released and created.network_id not in host.networks
    assert (
        await rig.access.controls.get(ADMIN, ResourceKey(ENV_A, "network", created.network_id))
        is None
    )
    (row,) = await rows_of(rig.db, "network_removed")
    assert row["detail"] == {
        "name": "mine",
        "driver": "bridge",
        "control_released": True,
    }


@pytest.mark.parametrize(
    ("name", "reason", "error"),
    [
        ("bridge", "network_predefined", NetworkPredefined),
        ("host", "network_predefined", NetworkPredefined),
        ("none", "network_predefined", NetworkPredefined),
    ],
)
async def test_a_predefined_network_is_refused_with_a_row_and_no_engine_call(
    rig: ContainerRig,
    nets: NetworkServices,
    host: ContainerEngine,
    name: str,
    reason: str,
    error: type[Exception],
) -> None:
    network_id = id_of(host, name)

    with pytest.raises(error):
        await nets.actions.remove(ADMIN, ENV_A, network_id, expected_name=name, expected_in_use=0)

    assert requests_of(host, "DELETE") == []
    (row,) = await rows_of(rig.db, "network_removed")
    assert row["detail"]["reason"] == reason


async def test_a_network_that_changed_since_the_review_is_not_removed(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    network_id = host.add_network("alpha", 0x71)

    with pytest.raises(NetworkChangedSinceReview):
        await nets.actions.remove(ADMIN, ENV_A, network_id, expected_name="beta", expected_in_use=0)
    add_container(host, 1, "late", networks={"alpha": network_id})
    with pytest.raises(NetworkChangedSinceReview):
        await nets.actions.remove(
            ADMIN, ENV_A, network_id, expected_name="alpha", expected_in_use=0
        )

    assert requests_of(host, "DELETE") == []
    reasons = [r["detail"]["reason"] for r in await rows_of(rig.db, "network_removed")]
    assert reasons == ["changed_since_review", "changed_since_review"]


async def test_a_network_used_by_a_stopped_container_is_refused_even_when_the_review_agreed(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    network_id = host.add_network("alpha", 0x72)
    add_container(host, 1, "stopped", state="exited", networks={"alpha": network_id})

    with pytest.raises(NetworkInUse) as refused:
        await nets.actions.remove(
            ADMIN, ENV_A, network_id, expected_name="alpha", expected_in_use=1
        )

    assert refused.value.containers == 1 and requests_of(host, "DELETE") == []
    (row,) = await rows_of(rig.db, "network_removed")
    assert row["detail"]["reason"] == "network_in_use"


async def test_a_remove_takes_the_lock_before_it_reads_the_containers(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    network_id = host.add_network("alpha", 0x73)
    holder = LockHolder(UserActor(PEER.user_id), "peer", "connect")
    async with rig.engine.locks.hold(ObjectKey(ENV_A, "network", network_id), holder):
        with pytest.raises(ObjectBusy):
            await nets.actions.remove(
                ADMIN, ENV_A, network_id, expected_name="alpha", expected_in_use=0
            )

    assert host.calls("GET", "/containers/json") == []
    (row,) = await rows_of(rig.db, "network_removed")
    assert row["detail"]["reason"] == "object_busy"


async def test_a_remove_of_a_network_that_vanished_is_gone_not_a_server_error(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    with pytest.raises(NetworkGone):
        await nets.actions.remove(ADMIN, ENV_A, "e" * 64, expected_name="x", expected_in_use=0)

    (row,) = await rows_of(rig.db, "network_removed")
    assert row["detail"]["reason"] == "network_gone"


@pytest.mark.parametrize(
    ("status", "error", "reason"),
    [(404, NetworkGone, "network_gone"), (403, EngineRefused, "engine_rejected")],
)
async def test_an_engine_refusal_of_a_remove_is_mapped_to_a_product_error_with_no_engine_text(
    rig: ContainerRig,
    nets: NetworkServices,
    host: ContainerEngine,
    status: int,
    error: type[Exception],
    reason: str,
) -> None:
    network_id = host.add_network("alpha", 0x74)
    host.fail("network_remove", status)

    with pytest.raises(error) as refused:
        await nets.actions.remove(
            ADMIN, ENV_A, network_id, expected_name="alpha", expected_in_use=0
        )

    assert ENGINE_TEXT not in str(refused.value)
    (row,) = await rows_of(rig.db, "network_removed")
    assert row["detail"]["reason"] == reason


async def test_a_swarm_network_refused_by_docker_is_reported_as_in_use(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    network_id = host.add_network(
        "ov", 0, "overlay", network_id=swarm_network_id(9), Scope="swarm", Attachable=True
    )
    host.fail("network_remove", 403)

    with pytest.raises(NetworkInUse):
        await nets.actions.remove(ADMIN, ENV_A, network_id, expected_name="ov", expected_in_use=0)

    (row,) = await rows_of(rig.db, "network_removed")
    assert row["detail"]["reason"] == "network_in_use"


async def test_a_failed_release_still_counts_the_removal_and_the_row_says_so(
    rig: ContainerRig,
    nets: NetworkServices,
    host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await nets.actions.create(ADMIN, ENV_A, {"name": "alpha", "driver": "bridge"})

    async def failing(self: ResourceControlService, key: ResourceKey) -> bool:
        raise RuntimeError("release failed")

    monkeypatch.setattr(ResourceControlService, "release", failing)

    removed = await nets.actions.remove(
        ADMIN, ENV_A, created.network_id, expected_name="alpha", expected_in_use=0
    )

    assert not removed.control_released and created.network_id not in host.networks
    (row,) = await rows_of(rig.db, "network_removed")
    assert (row["result"], row["detail"]["control_released"]) == ("success", False)


async def test_a_standard_user_removes_only_a_network_they_hold_write_on(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    admins = host.add_network("admin-only", 0x75)
    readable = host.add_network("readable", 0x76)
    await control(rig.access, ResourceKey(ENV_A, "network", readable), public=True, level="read")

    with pytest.raises(ResourceNotFound):
        await nets.actions.remove(
            STANDARD, ENV_A, admins, expected_name="admin-only", expected_in_use=0
        )
    with pytest.raises(PermissionDenied):
        await nets.actions.remove(
            STANDARD, ENV_A, readable, expected_name="readable", expected_in_use=0
        )

    assert requests_of(host, "DELETE") == []
    assert await rows_of(rig.db, "network_removed") == []


async def test_a_remove_cancelled_while_the_engine_works_is_recorded_as_removed(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    network_id = host.add_network("gone", 0x77)
    gate = host.hold("network_remove")
    task = asyncio.ensure_future(
        nets.actions.remove(ADMIN, ENV_A, network_id, expected_name="gone", expected_in_use=0)
    )
    await gate.entered.wait()
    task.cancel()
    gate.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    (row,) = await rows_of(rig.db, "network_removed")
    assert (row["result"], row["detail"]["interrupted"]) == ("success", True)
    assert network_id not in host.networks


async def test_the_prune_plan_lists_only_unused_local_user_networks_by_name(
    nets: NetworkServices, host: ContainerEngine, catalogue: dict[str, str]
) -> None:
    add_container(host, 1, "user", networks={"beta": catalogue["beta"]})

    plan = await nets.queries.prune_plan(ADMIN, ENV_A)

    assert [c.name for c in plan.candidates] == ["alpha", "weird"]
    assert (plan.total, plan.truncated) == (2, False)


async def test_the_prune_plan_is_cut_at_one_hundred_and_says_how_many_matched(
    nets: NetworkServices, host: ContainerEngine
) -> None:
    for number in range(MAX_PRUNE + 5):
        host.add_network(f"spare{number:03d}", 0x2000 + number)

    plan = await nets.queries.prune_plan(ADMIN, ENV_A)

    assert (len(plan.candidates), plan.total, plan.truncated) == (MAX_PRUNE, MAX_PRUNE + 5, True)


async def test_a_prune_removes_exactly_the_reviewed_networks_that_are_still_candidates(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    first = host.add_network("first", 0x82)
    kept = host.add_network("kept", 0x81)
    second = host.add_network("second", 0x83)
    third = host.add_network("third", 0x84)
    fourth = host.add_network("fourth", 0x86)
    unlisted = host.add_network("zz-unlisted", 0x85)
    plan = await nets.queries.prune_plan(ADMIN, ENV_A)
    reviewed = [NetworkPruneTarget(c.id, c.name) for c in plan.candidates if c.id != unlisted]
    add_container(host, 1, "late", networks={"second": second})
    await nets.actions.remove(ADMIN, ENV_A, kept, expected_name="kept", expected_in_use=0)
    reviewed = [
        t if t.network_id != fourth else NetworkPruneTarget(fourth, "renamed") for t in reviewed
    ]

    result = await nets.actions.run_prune(ADMIN, ENV_A, reviewed)

    assert {i.name: i.status for i in result.items} == {
        "first": "removed",
        "kept": "skipped_changed",
        "second": "skipped_changed",
        "third": "removed",
        "renamed": "skipped_changed",
    }
    assert first not in host.networks and third not in host.networks
    assert {second, fourth, unlisted} <= set(host.networks)
    assert host.calls("POST", "/networks/prune") == []
    pruned = [r for r in await rows_of(rig.db, "network_removed") if r["detail"].get("pruned")]
    assert {r["detail"]["name"] for r in pruned} == {"first", "third"}


async def test_a_prune_item_that_cannot_be_removed_does_not_stop_the_rest(
    nets: NetworkServices, host: ContainerEngine
) -> None:
    first = host.add_network("first", 0x191)
    second = host.add_network("second", 0x192)
    host.fail("network_remove", 500, times=1)
    plan = await nets.queries.prune_plan(ADMIN, ENV_A)

    result = await nets.actions.run_prune(
        ADMIN, ENV_A, [NetworkPruneTarget(c.id, c.name) for c in plan.candidates]
    )

    assert [i.status for i in result.items] == ["failed", "removed"]
    assert first in host.networks and second not in host.networks


async def test_a_busy_network_is_skipped_by_prune_not_waited_for(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    busy = host.add_network("busy", 0x95)
    free = host.add_network("free", 0x96)
    plan = await nets.queries.prune_plan(ADMIN, ENV_A)
    holder = LockHolder(UserActor(PEER.user_id), "peer", "connect")

    async with rig.engine.locks.hold(ObjectKey(ENV_A, "network", busy), holder):
        result = await nets.actions.run_prune(
            ADMIN, ENV_A, [NetworkPruneTarget(c.id, c.name) for c in plan.candidates]
        )

    assert {i.name: i.status for i in result.items} == {"busy": "busy", "free": "removed"}
    assert busy in host.networks and free not in host.networks


@pytest.mark.parametrize("targets", [[], [NetworkPruneTarget("not-an-id", "x")]])
async def test_a_prune_with_no_valid_selection_is_refused(
    nets: NetworkServices, targets: list[NetworkPruneTarget]
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await nets.actions.run_prune(ADMIN, ENV_A, targets)

    assert "networks" in refused.value.fields


async def test_a_prune_of_more_than_one_hundred_networks_is_refused(
    nets: NetworkServices,
) -> None:
    targets = [NetworkPruneTarget(f"{n:064x}", "x") for n in range(MAX_PRUNE + 1)]

    with pytest.raises(ValidationFailed):
        await nets.actions.run_prune(ADMIN, ENV_A, targets)


async def test_prune_is_for_administrators_only(
    nets: NetworkServices, host: ContainerEngine
) -> None:
    with pytest.raises(PermissionDenied):
        await nets.queries.prune_plan(STANDARD, ENV_A)
    with pytest.raises(PermissionDenied):
        await nets.actions.run_prune(STANDARD, ENV_A, [NetworkPruneTarget("a" * 64, "x")])

    assert requests_of(host, "DELETE") == []


async def test_the_access_tab_is_for_administrators_only(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine
) -> None:
    network_id = host.add_network("shared", 0x97)
    await control(rig.access, ResourceKey(ENV_A, "network", network_id), public=True)

    panel = await nets.queries.access_panel(ADMIN, ENV_A, network_id)

    assert panel.name == "shared" and panel.control is not None
    with pytest.raises(PermissionDenied):
        await nets.queries.access_panel(STANDARD, ENV_A, network_id)


async def test_a_network_whose_id_has_neither_shape_is_left_out_of_the_list(
    nets: NetworkServices, host: ContainerEngine
) -> None:
    host.networks["ZZZ"] = {"Id": "ZZZ", "Name": "badid", "Driver": "bridge", "Scope": "local"}
    host.networks["a" * 65] = {
        "Id": "a" * 65,
        "Name": "longid",
        "Driver": "bridge",
        "Scope": "local",
    }

    page = await nets.queries.page(ADMIN, ENV_A)

    assert names(page) == ["bridge", "host", "none"]
