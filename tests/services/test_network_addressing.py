"""Tests for app/services/network_addressing.py."""

from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import Any

import pytest

from app.docker_engine.models import Network
from app.services.network_addressing import (
    OFFERED_DRIVERS,
    STANDARD_OPTIONS,
    AddressingInvalid,
    NetworkRequest,
    Pool,
    is_id_shaped,
    overlapping,
    parse_network_form,
    parse_static_address,
    pools_of,
    restricted_network_options,
)

MTU = "com.docker.network.driver.mtu"
ICC = "com.docker.network.bridge.enable_icc"
MASQUERADE = "com.docker.network.bridge.enable_ip_masquerade"
SUBNET_V4 = "10.20.0.0/24"
SUBNET_V6 = "fd00:20::/64"
SWARM = "swarm"
HEX_ID = "a" * 64
BASE36_ID = "z" * 25


def form(**fields: str) -> dict[str, str]:
    """A bridge network form with a name; keyword fields override or add to it."""
    return {"name": "net", "driver": "bridge"} | fields


def parsed(environment_type: str | None = None, **fields: str) -> NetworkRequest:
    return parse_network_form(form(**fields), environment_type=environment_type)


def refused_fields(environment_type: str | None = None, **fields: str) -> dict[str, str]:
    with pytest.raises(AddressingInvalid) as refused:
        parse_network_form(form(**fields), environment_type=environment_type)
    return refused.value.errors


def option(index: int, key: str, value: str) -> dict[str, str]:
    return {f"option-{index}-key": key, f"option-{index}-value": value}


def test_a_name_and_driver_alone_make_a_request_with_no_pools_options_or_labels() -> None:
    request = parsed()

    assert (request.name, request.driver) == ("net", "bridge")
    assert (request.pools, dict(request.options), dict(request.labels)) == ((), {}, {})
    assert not (request.internal or request.attachable or request.enable_ipv6)


def test_a_subnet_with_gateway_and_range_becomes_one_pool() -> None:
    request = parsed(
        subnet_v4=SUBNET_V4, gateway_v4="10.20.0.1", ip_range_v4="10.20.0.128/25", internal="on"
    )

    assert request.pools == (
        Pool(IPv4Network(SUBNET_V4), IPv4Address("10.20.0.1"), IPv4Network("10.20.0.128/25")),
    )
    assert request.internal


def test_a_gateway_outside_the_allocation_range_is_accepted() -> None:
    request = parsed(subnet_v4=SUBNET_V4, gateway_v4="10.20.0.1", ip_range_v4="10.20.0.128/25")

    assert request.pools[0].gateway == IPv4Address("10.20.0.1")


def test_a_dual_stack_request_keeps_one_pool_per_family() -> None:
    request = parsed(
        subnet_v4=SUBNET_V4, subnet_v6=SUBNET_V6, gateway_v6="fd00:20::1", enable_ipv6="on"
    )

    assert [pool.subnet for pool in request.pools] == [
        IPv4Network(SUBNET_V4),
        IPv6Network(SUBNET_V6),
    ]
    assert request.pools[1].gateway == IPv6Address("fd00:20::1")
    assert request.enable_ipv6


def test_the_ipv6_toggle_without_an_ipv6_pool_is_left_to_docker() -> None:
    request = parsed(enable_ipv6="on")

    assert request.enable_ipv6 and request.pools == ()


@pytest.mark.parametrize("flag", ["on", "true", "1", "yes", " ON "])
def test_a_checkbox_is_true_in_each_accepted_spelling(flag: str) -> None:
    assert parsed(internal=flag).internal


@pytest.mark.parametrize("flag", ["", "off", "false", "0", "maybe"])
def test_a_checkbox_is_false_otherwise(flag: str) -> None:
    assert not parsed(internal=flag).internal


@pytest.mark.parametrize(
    "subnet", ["10.0.0.0/8", "10.0.0.0/30", "fd00::/48", "fd00::/120", "2001:db8::/48"]
)
def test_prefix_lengths_inside_the_bounds_are_accepted(subnet: str) -> None:
    v6 = ":" in subnet
    fields = {"subnet_v6": subnet, "enable_ipv6": "on"} if v6 else {"subnet_v4": subnet}

    assert parsed(**fields).pools[0].subnet == (IPv6Network if v6 else IPv4Network)(subnet)


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"subnet_v4": "10.0.0.0/7"}, "subnet_v4"),
        ({"subnet_v4": "10.0.0.0/31"}, "subnet_v4"),
        ({"subnet_v4": "10.0.0.0/32"}, "subnet_v4"),
        ({"subnet_v4": "10.0.0.0/33"}, "subnet_v4"),
        ({"subnet_v6": "fd00::/47", "enable_ipv6": "on"}, "subnet_v6"),
        ({"subnet_v6": "fd00::/121", "enable_ipv6": "on"}, "subnet_v6"),
        ({"subnet_v6": "fd00::/128", "enable_ipv6": "on"}, "subnet_v6"),
    ],
)
def test_prefix_lengths_outside_the_bounds_are_refused(fields: dict[str, str], field: str) -> None:
    assert field in refused_fields(**fields)


@pytest.mark.parametrize(
    "subnet",
    [
        "0.0.0.0/8",
        "0.0.0.0/7",
        "127.0.0.0/8",
        "127.1.0.0/16",
        "169.254.0.0/16",
        "169.254.5.0/24",
        "224.0.0.0/4",
        "239.1.0.0/16",
        "240.0.0.0/4",
        "250.0.0.0/8",
    ],
)
def test_an_ipv4_subnet_overlapping_a_refused_range_is_refused(subnet: str) -> None:
    assert "cannot be used here" in refused_fields(subnet_v4=subnet)["subnet_v4"]


@pytest.mark.parametrize(
    "subnet",
    [
        "::/48",
        "::1/128",
        "::ffff:0:0/96",
        "::ffff:10.0.0.0/104",
        "fe80::/64",
        "fe80::/10",
        "ff00::/8",
    ],
)
def test_an_ipv6_subnet_overlapping_a_refused_range_is_refused(subnet: str) -> None:
    errors = refused_fields(subnet_v6=subnet, enable_ipv6="on")

    assert "subnet_v6" in errors


@pytest.mark.parametrize("subnet", ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"])
def test_private_ipv4_ranges_are_not_refused(subnet: str) -> None:
    assert parsed(subnet_v4=subnet).pools[0].subnet == IPv4Network(subnet)


def test_a_subnet_with_host_bits_set_names_the_network_it_would_be() -> None:
    errors = refused_fields(subnet_v4="10.20.0.5/24")

    assert "10.20.0.0/24" in errors["subnet_v4"]


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"subnet_v6": "fd00:20::5/64", "enable_ipv6": "on"}, "subnet_v6"),
        ({"subnet_v4": "10.20.0.0"}, "subnet_v4"),
        ({"subnet_v4": "10.20.0.0/255.255.255.0"}, "subnet_v4"),
        ({"subnet_v4": "not a subnet"}, "subnet_v4"),
        ({"subnet_v4": SUBNET_V6}, "subnet_v4"),
        ({"subnet_v6": SUBNET_V4, "enable_ipv6": "on"}, "subnet_v6"),
        ({"subnet_v6": SUBNET_V6}, "subnet_v6"),
        ({"gateway_v4": "10.20.0.1"}, "gateway_v4"),
        ({"ip_range_v4": "10.20.0.0/25"}, "ip_range_v4"),
    ],
)
def test_a_malformed_or_misplaced_subnet_is_refused_on_its_own_field(
    fields: dict[str, str], field: str
) -> None:
    assert field in refused_fields(**fields)


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"gateway_v4": "10.20.0.0"}, "gateway_v4"),
        ({"gateway_v4": "10.20.0.255"}, "gateway_v4"),
        ({"gateway_v4": "10.21.0.1"}, "gateway_v4"),
        ({"gateway_v4": "fd00:20::1"}, "gateway_v4"),
        ({"gateway_v4": "gateway"}, "gateway_v4"),
        ({"ip_range_v4": "10.21.0.0/24"}, "ip_range_v4"),
        ({"ip_range_v4": "10.20.0.0/16"}, "ip_range_v4"),
        ({"ip_range_v4": "10.20.0.130/25"}, "ip_range_v4"),
        ({"ip_range_v4": SUBNET_V6}, "ip_range_v4"),
    ],
)
def test_a_gateway_or_range_that_does_not_fit_the_ipv4_subnet_is_refused(
    fields: dict[str, str], field: str
) -> None:
    assert field in refused_fields(subnet_v4=SUBNET_V4, **fields)


def test_an_ipv6_gateway_at_the_network_address_is_refused() -> None:
    errors = refused_fields(subnet_v6=SUBNET_V6, gateway_v6="fd00:20::", enable_ipv6="on")

    assert "gateway_v6" in errors


@pytest.mark.parametrize("environment_type", ["standalone", None])
def test_overlay_is_not_offered_outside_a_swarm_manager(environment_type: str | None) -> None:
    assert "driver" in refused_fields(environment_type, driver="overlay")


def test_overlay_on_a_swarm_manager_can_be_attachable() -> None:
    request = parsed(SWARM, driver="overlay", attachable="on")

    assert (request.driver, request.attachable) == ("overlay", True)


def test_only_overlay_networks_can_be_made_attachable() -> None:
    assert "attachable" in refused_fields(attachable="on")


@pytest.mark.parametrize("driver", ["host", "none", "null", "Bridge", "weave", ""])
def test_a_driver_outside_the_offered_list_is_refused(driver: str) -> None:
    assert "driver" in refused_fields(driver=driver)


@pytest.mark.parametrize("driver", ["macvlan", "ipvlan"])
def test_a_physical_segment_driver_requires_a_subnet(driver: str) -> None:
    assert "subnet_v4" in refused_fields(driver=driver)


def test_an_ipvlan_network_takes_a_vlan_sub_interface_parent_and_a_mode() -> None:
    request = parsed(
        driver="ipvlan",
        subnet_v4="192.168.50.0/24",
        **option(0, "parent", "eth0.10"),
        **{"option-1-key": "ipvlan_mode", "option-1-value": "l3"},
    )

    assert dict(request.options) == {"parent": "eth0.10", "ipvlan_mode": "l3"}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ipvlan_mode", "l4"),
        ("ipvlan_flag", "promiscuous"),
        ("parent", "a" * 16),
        ("parent", "-eth0"),
        ("parent", "eth/0"),
    ],
)
def test_a_physical_segment_option_with_a_bad_value_is_refused_on_the_value(
    key: str, value: str
) -> None:
    errors = refused_fields(driver="ipvlan", subnet_v4="192.168.50.0/24", **option(0, key, value))

    assert "option-0-value" in errors


def test_a_macvlan_mode_outside_the_list_is_refused() -> None:
    errors = refused_fields(
        driver="macvlan", subnet_v4="192.168.50.0/24", **option(0, "macvlan_mode", "trunk")
    )

    assert "option-0-value" in errors


@pytest.mark.parametrize(
    "name", ["a", "ok.name_1-x", "x" * 255, " net ", "Aabbbbbbbbbbbbbbbbbbbbbbb"]
)
def test_names_inside_the_grammar_are_accepted(name: str) -> None:
    assert parsed(name=name).name == name.strip()


@pytest.mark.parametrize(
    "name",
    [
        "",
        "-bad",
        "a b",
        "x" * 256,
        "bridge",
        "Bridge",
        "HOST",
        "None",
        "ingress",
        "docker_gwbridge",
        HEX_ID,
        "0" * 64,
        BASE36_ID,
        "abcdefghijklmnopqrstuvwxy",
    ],
)
def test_names_outside_the_grammar_or_shaped_like_an_id_or_reserved_are_refused(
    name: str,
) -> None:
    assert "name" in refused_fields(name=name)


@pytest.mark.parametrize("name", ["A" * 64, "a" * 63, "a" * 65, "a" * 26, "Z" * 25])
def test_names_close_to_an_id_shape_but_not_matching_it_are_accepted(name: str) -> None:
    assert parsed(name=name).name == name


@pytest.mark.parametrize(
    ("value", "ipv6"),
    [("1450", False), ("68", False), ("65535", False), ("1280", True)],
)
def test_an_mtu_inside_the_range_is_accepted(value: str, ipv6: bool) -> None:
    fields = {"subnet_v6": SUBNET_V6, "enable_ipv6": "on"} if ipv6 else {}

    assert dict(parsed(**fields, **option(0, MTU, value)).options) == {MTU: value}


@pytest.mark.parametrize(
    ("value", "ipv6"),
    [
        ("67", False),
        ("65536", False),
        ("", False),
        ("-1450", False),
        ("14.5", False),
        ("١٤٥٠", False),
        ("1279", True),
    ],
)
def test_an_mtu_outside_the_range_or_not_a_number_is_refused(value: str, ipv6: bool) -> None:
    fields = {"subnet_v6": SUBNET_V6, "enable_ipv6": "on"} if ipv6 else {}

    assert "option-0-value" in refused_fields(**fields, **option(0, MTU, value))


def test_the_mtu_rule_applies_to_the_key_in_any_case() -> None:
    assert "option-0-value" in refused_fields(**option(0, MTU.upper(), "10"))


@pytest.mark.parametrize("key", [ICC, MASQUERADE])
@pytest.mark.parametrize("value", ["maybe", "True", "1", ""])
def test_a_boolean_bridge_option_takes_only_true_or_false(key: str, value: str) -> None:
    assert "option-0-value" in refused_fields(**option(0, key, value))


@pytest.mark.parametrize(
    ("key", "value", "field"),
    [
        ("", "x", "option-0-key"),
        (".leading", "x", "option-0-key"),
        ("has space", "x", "option-0-key"),
        ("k" * 129, "x", "option-0-key"),
        ("key", "v" * 257, "option-0-value"),
        ("key", "line\nbreak", "option-0-value"),
    ],
)
def test_an_option_row_outside_the_grammar_is_refused_on_the_offending_field(
    key: str, value: str, field: str
) -> None:
    assert field in refused_fields(**option(0, key, value))


def test_an_option_key_repeated_in_another_case_is_refused() -> None:
    errors = refused_fields(**option(0, "Key", "a"), **option(1, "key", "b"))

    assert "option-1-key" in errors


def test_blank_option_and_label_rows_are_skipped() -> None:
    request = parsed(**option(0, "", ""), **{"label-0-key": " ", "label-0-value": ""})

    assert (dict(request.options), dict(request.labels)) == ({}, {})


def test_thirty_two_options_are_accepted_and_a_thirty_third_is_refused() -> None:
    rows: dict[str, str] = {}
    for index in range(32):
        rows |= option(index, f"k{index}", "")

    assert len(parsed(**rows).options) == 32
    assert "options" in refused_fields(**rows, **option(32, "k32", ""))


def test_labels_keep_their_case_and_a_control_character_in_a_key_is_refused() -> None:
    request = parsed(**{"label-0-key": "Team", "label-0-value": "Core"})

    assert dict(request.labels) == {"Team": "Core"}
    assert "label-0-key" in refused_fields(**{"label-0-key": "bad\tkey", "label-0-value": "x"})


def test_labels_that_total_over_64_kib_are_refused() -> None:
    rows = {
        f"label-{i}-{part}": value
        for i in range(2)
        for part, value in (("key", f"k{i}"), ("value", "v" * (40 * 1024)))
    }

    assert "labels" in refused_fields(**rows)


def test_several_bad_fields_are_all_reported_in_one_refusal() -> None:
    errors = refused_fields(
        name="", subnet_v4="127.0.0.0/8", gateway_v6="fd00::1", **option(0, ICC, "maybe")
    )

    assert {"name", "subnet_v4", "gateway_v6", "option-0-value"} <= set(errors)


def test_the_drivers_offered_never_include_host_or_none() -> None:
    assert OFFERED_DRIVERS == {
        "standalone": ("bridge", "macvlan", "ipvlan"),
        "swarm": ("bridge", "overlay", "macvlan", "ipvlan"),
    }


def test_the_standard_user_option_allow_list_is_exactly_the_documented_keys() -> None:
    assert dict(STANDARD_OPTIONS) == {
        "bridge": frozenset({ICC, MASQUERADE, MTU}),
        "overlay": frozenset({"encrypted", MTU}),
    }


@pytest.mark.parametrize(
    ("driver", "key", "value"),
    [
        ("bridge", ICC, "true"),
        ("bridge", MASQUERADE, "false"),
        ("bridge", MTU, "1500"),
        ("overlay", "encrypted", ""),
        ("overlay", MTU, "1500"),
    ],
)
def test_an_allow_listed_option_is_not_restricted(driver: str, key: str, value: str) -> None:
    request = parsed(SWARM, driver=driver, **option(0, key, value))

    assert restricted_network_options(request) == ()


@pytest.mark.parametrize(
    "key",
    [
        "com.docker.network.bridge.name",
        "com.docker.network.bridge.gateway_mode_ipv4",
        "com.docker.network.bridge.gateway_mode_ipv6",
        "com.docker.network.bridge.host_binding_ipv4",
        "com.docker.network.bridge.inhibit_ipv4",
        "com.docker.network.bridge.trusted_host_interfaces",
        "com.docker.network.driver.overlay.vxlanid_list",
        "encrypted",
        ICC.upper(),
        "Com.Docker.Network.Bridge.Enable_Icc",
        f"{ICC}.",
        f"{ICC}_",
        ICC.replace("_", "-"),
        "parent",
        "anything",
    ],
)
def test_any_other_key_or_spelling_is_restricted_on_a_bridge(key: str) -> None:
    request = parsed(**option(0, key, "true"))

    assert restricted_network_options(request) == (key,)


@pytest.mark.parametrize(
    "key", ["ENCRYPTED", "Encrypted", ICC, "com.docker.network.driver.overlay.vxlanid_list"]
)
def test_any_other_key_or_spelling_is_restricted_on_an_overlay(key: str) -> None:
    request = parsed(SWARM, driver="overlay", **option(0, key, "true"))

    assert restricted_network_options(request) == (key,)


def test_a_padded_option_key_is_judged_by_its_trimmed_spelling() -> None:
    request = parsed(**option(0, " com.docker.network.bridge.name ", "x"))

    assert restricted_network_options(request) == ("com.docker.network.bridge.name",)


def test_every_option_of_a_physical_segment_driver_is_restricted() -> None:
    request = parsed(driver="macvlan", subnet_v4="192.168.50.0/24", **option(0, "parent", "eth0"))

    assert restricted_network_options(request) == ("parent",)


def request_with_pools(**fields: str) -> NetworkRequest:
    return parsed(subnet_v4=SUBNET_V4, subnet_v6=SUBNET_V6, enable_ipv6="on", **fields)


def test_overlap_names_each_network_whose_subnet_meets_one_of_the_pools_once() -> None:
    existing = [
        ("wider", "10.20.0.0/16"),
        ("beside", "10.21.0.0/24"),
        ("inside", "10.20.0.128/25"),
        ("v6-wider", "fd00:20::/48"),
        ("v6-beside", "fd01::/64"),
        ("junk", "junk"),
        ("empty", ""),
        ("host-bits", "10.20.0.1/24"),
        ("wider", "10.20.0.0/24"),
        ("v6-same", SUBNET_V6),
    ]

    assert overlapping(request_with_pools(), existing) == (
        "wider",
        "inside",
        "v6-wider",
        "host-bits",
        "v6-same",
    )


def test_a_request_without_pools_overlaps_nothing() -> None:
    assert overlapping(parsed(), [("any", "10.20.0.0/16")]) == ()


STATIC_POOLS = (
    Pool(IPv4Network(SUBNET_V4), IPv4Address("10.20.0.1"), IPv4Network("10.20.0.128/25")),
    Pool(IPv6Network(SUBNET_V6), IPv6Address("fd00:20::1"), None),
)


def static(text: str, **overrides: Any) -> IPv4Address | IPv6Address:
    arguments: dict[str, Any] = {"pools": STATIC_POOLS, "enable_ipv6": True, "in_use": ()}
    return parse_static_address(text, **(arguments | overrides))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10.20.0.5", IPv4Address("10.20.0.5")),
        (" 10.20.0.9 ", IPv4Address("10.20.0.9")),
        ("10.20.0.2", IPv4Address("10.20.0.2")),
        ("fd00:20::5", IPv6Address("fd00:20::5")),
    ],
)
def test_a_static_address_inside_a_subnet_is_parsed_even_outside_the_allocation_range(
    text: str, expected: IPv4Address | IPv6Address
) -> None:
    assert static(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not-an-ip",
        "10.20.0.5%eth0",
        "010.20.0.5",
        "10.20.0.5/24",
        "10.21.0.5",
        "10.20.0.0",
        "10.20.0.255",
        "10.20.0.1",
        "fd00:20::1",
        "fd00:20::",
    ],
)
def test_a_static_address_that_is_malformed_outside_the_subnet_or_reserved_is_refused(
    text: str,
) -> None:
    with pytest.raises(AddressingInvalid):
        static(text)


@pytest.mark.parametrize(
    ("text", "overrides"),
    [
        ("10.20.0.5", {"in_use": ["10.20.0.5/24"]}),
        ("fd00:20::5", {"in_use": ["fd00:20::5/64"]}),
        ("fd00:20::5", {"enable_ipv6": False}),
        ("10.20.0.5", {"pools": ()}),
        ("fd00:20::5", {"pools": STATIC_POOLS[:1]}),
        ("10.20.0.5", {"pools": STATIC_POOLS[1:]}),
        ("10.20.0.5", {"version": 6}),
        ("fd00:20::5", {"version": 4}),
    ],
)
def test_a_static_address_the_network_cannot_take_is_refused(
    text: str, overrides: dict[str, Any]
) -> None:
    with pytest.raises(AddressingInvalid):
        static(text, **overrides)


def test_junk_among_the_held_addresses_does_not_block_a_free_one() -> None:
    assert static("10.20.0.5", in_use=["10.20.0.6/24", "junk"]) == IPv4Address("10.20.0.5")


def test_the_refusal_of_a_static_address_is_keyed_by_the_field_given() -> None:
    with pytest.raises(AddressingInvalid) as refused:
        static("nope", field="ipv4")

    assert set(refused.value.errors) == {"ipv4"}


def test_pools_come_from_an_engine_network_model() -> None:
    network = Network.model_validate(
        {
            "Id": HEX_ID,
            "Name": "n",
            "IPAM": {
                "Config": [
                    {"Subnet": SUBNET_V4, "Gateway": "10.20.0.1", "IPRange": "10.20.0.128/25"},
                    {"Subnet": SUBNET_V6},
                ]
            },
        }
    )

    assert pools_of(network) == (
        Pool(IPv4Network(SUBNET_V4), IPv4Address("10.20.0.1"), IPv4Network("10.20.0.128/25")),
        Pool(IPv6Network(SUBNET_V6), None, None),
    )


@pytest.mark.parametrize(
    "document",
    [
        None,
        5,
        "x",
        {},
        {"IPAM": None},
        {"IPAM": {"Config": None}},
        {"IPAM": {"Config": [None, 5, "x", {"Subnet": 5}, {"Subnet": "junk"}]}},
    ],
)
def test_pools_of_anything_that_is_not_a_network_with_usable_pools_is_empty(
    document: object,
) -> None:
    assert pools_of(document) == ()


def test_pools_of_drops_a_gateway_or_range_outside_its_subnet_and_keeps_the_subnet() -> None:
    document = {
        "IPAM": {
            "Config": [
                {"Subnet": "10.1.0.0/24", "Gateway": "10.2.0.1", "IPRange": "10.9.0.0/24"},
                {"Subnet": "10.3.0.5/24", "Gateway": "10.3.0.1/24"},
                {"Subnet": "fd00::/64", "Gateway": "10.0.0.1"},
            ]
        }
    }

    assert [(p.subnet, p.gateway, p.ip_range) for p in pools_of(document)] == [
        (IPv4Network("10.1.0.0/24"), None, None),
        (IPv4Network("10.3.0.0/24"), None, None),
        (IPv6Network("fd00::/64"), None, None),
    ]


def test_pools_of_reads_at_most_sixty_four_entries() -> None:
    config = [{"Subnet": f"10.{i}.0.0/24"} for i in range(100)]

    assert len(pools_of({"IPAM": {"Config": config}})) == 64


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (HEX_ID, True),
        ("0" * 64, True),
        (BASE36_ID, True),
        ("a" * 25, True),
        ("A" * 64, False),
        ("a" * 63, False),
        ("a" * 65, False),
        ("g" * 64, False),
        ("Z" * 25, False),
        ("z" * 24, False),
        ("z" * 26, False),
        ("a-" * 12 + "a", False),
        ("net", False),
        ("", False),
    ],
)
def test_is_id_shaped_matches_a_64_hex_or_25_base_36_name_only(name: str, expected: bool) -> None:
    assert is_id_shaped(name) is expected
