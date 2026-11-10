"""Tests for app/netpolicy.py and app/net_transport.py: which addresses a connection may reach."""

import ipaddress
import socket
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app import netpolicy
from app.config import ConfigError, load_settings
from app.net_transport import policy_transport
from app.netpolicy import AddressPolicy, AddressRefused

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"
PRIVATE_V4 = "10.1.2.3"
TLS_PORT = 443
UNUSED_PORT = 1

INTERNAL = [
    pytest.param("0.0.0.0", id="v4-unspecified"),
    pytest.param("0.255.255.255", id="v4-this-network"),
    pytest.param("127.0.0.1", id="v4-loopback"),
    pytest.param("127.255.255.254", id="v4-loopback-top"),
    pytest.param("169.254.169.254", id="v4-metadata"),
    pytest.param("169.254.0.1", id="v4-link-local"),
    pytest.param("::", id="v6-unspecified"),
    pytest.param("::1", id="v6-loopback"),
    pytest.param("fe80::1", id="v6-link-local"),
    pytest.param("fd00:ec2::254", id="v6-metadata"),
    pytest.param("fd00:ec2::1:2", id="v6-metadata-network"),
    pytest.param("::ffff:127.0.0.1", id="mapped-loopback"),
    pytest.param("::ffff:169.254.169.254", id="mapped-metadata"),
    pytest.param("::127.0.0.1", id="compatible-loopback"),
    pytest.param("64:ff9b::7f00:1", id="nat64-loopback"),
    pytest.param("64:ff9b::a9fe:a9fe", id="nat64-metadata"),
    pytest.param("2002:7f00:1::1", id="six-to-four-loopback"),
    pytest.param("::ffff:0:7f00:1", id="siit-loopback"),
    pytest.param("64:ff9b:1::7f00:1", id="nat64-local-use-low"),
    pytest.param("fe80::1%eth0", id="scoped-link-local"),
]
NOT_INTERNAL = [
    pytest.param(PUBLIC_V4, id="v4-public"),
    pytest.param(PRIVATE_V4, id="v4-private"),
    pytest.param("172.16.0.1", id="v4-private-172"),
    pytest.param("192.168.0.1", id="v4-private-192"),
    pytest.param("100.64.0.1", id="v4-shared"),
    pytest.param(PUBLIC_V6, id="v6-public"),
    pytest.param("fd00::1", id="v6-unique-local"),
    pytest.param("::ffff:93.184.216.34", id="mapped-public"),
    pytest.param("64:ff9b::5db8:d822", id="nat64-public"),
    pytest.param("2002:5db8:d822::1", id="six-to-four-public"),
]


def address(text: str) -> netpolicy.IpAddress:
    return ipaddress.ip_address(text)


@pytest.mark.parametrize("text", INTERNAL)
def test_an_internal_address_is_refused_without_a_permit(text: str) -> None:
    policy = AddressPolicy()

    assert policy.is_internal(address(text))
    assert not policy.allows(address(text))
    with pytest.raises(AddressRefused) as refused:
        policy.check(address(text))
    assert "%" not in refused.value.address


@pytest.mark.parametrize(
    ("text", "public"),
    [
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("10.0.0.7", False),
        ("172.16.4.2", False),
        ("192.168.1.10", False),
        ("100.64.0.1", False),
        ("fd12:3456::1", False),
        ("::ffff:10.0.0.7", False),
        ("64:ff9b::a00:7", False),
        ("127.0.0.1", False),
    ],
)
def test_only_a_globally_routable_address_is_public(text: str, public: bool) -> None:
    assert netpolicy.is_public(address(text)) is public


@pytest.mark.parametrize("text", NOT_INTERNAL)
def test_every_other_address_is_allowed_without_a_permit(text: str) -> None:
    policy = AddressPolicy()

    assert not policy.is_internal(address(text))
    assert policy.allows(address(text))


@pytest.mark.parametrize(
    ("permitted", "text", "allowed"),
    [
        pytest.param(["127.0.0.0/8"], "127.0.0.1", True, id="network-covers-loopback"),
        pytest.param(["127.0.0.1"], "127.0.0.2", False, id="single-address-covers-only-itself"),
        pytest.param(["127.0.0.1"], "127.0.0.1", True, id="single-address-itself"),
        pytest.param(["127.0.0.0/8"], "169.254.169.254", False, id="other-class-stays-refused"),
        pytest.param(["169.254.0.0/16"], "169.254.169.254", True, id="metadata-permitted"),
        pytest.param(["127.0.0.0/8"], "::ffff:127.0.0.1", True, id="mapped-follows-its-v4"),
        pytest.param(["::1/128"], "::ffff:127.0.0.1", False, id="mapped-needs-v4-permit"),
        pytest.param(["::1/128"], "::1", True, id="v6-loopback-permitted"),
        pytest.param(["::ffff:127.0.0.1/128"], "::ffff:127.0.0.1", True, id="mapped-literal"),
        pytest.param(["fe80::/10"], "fe80::1%eth0", True, id="scoped-matches-unscoped-network"),
    ],
)
def test_a_permit_covers_only_the_networks_it_names(
    permitted: list[str], text: str, allowed: bool
) -> None:
    policy = AddressPolicy(tuple(ipaddress.ip_network(item) for item in permitted))

    assert policy.allows(address(text)) is allowed


def test_the_policy_is_built_from_the_permitted_networks_setting(env: dict[str, str]) -> None:
    settings = load_settings(
        env | {"DOCKMIN_PERMITTED_INTERNAL_NETWORKS": "10.0.0.5, 127.0.0.0/8 ,fe80::/10"}
    )

    policy = AddressPolicy.from_settings(settings)

    assert policy.allows(address("127.0.0.9")) and policy.allows(address("fe80::5"))
    assert not policy.allows(address("169.254.169.254"))


@pytest.mark.parametrize("entry", ["not-an-address", "10.0.0.0/33", "localhost"])
def test_a_permit_entry_that_is_not_an_address_is_a_configuration_error(
    env: dict[str, str], entry: str
) -> None:
    with pytest.raises(ConfigError) as raised:
        load_settings(env | {"DOCKMIN_PERMITTED_INTERNAL_NETWORKS": entry})

    assert "DOCKMIN_PERMITTED_INTERNAL_NETWORKS" in raised.value.variables


@pytest.mark.parametrize(
    ("host", "refused"),
    [
        pytest.param("127.0.0.1", True, id="loopback"),
        pytest.param("[::1]", True, id="bracketed-loopback"),
        pytest.param("localhost", True, id="localhost"),
        pytest.param("LOCALHOST.", True, id="localhost-trailing-dot"),
        pytest.param("registry.localhost", True, id="localhost-suffix"),
        pytest.param("metadata.google.internal", True, id="gcp-metadata-name"),
        pytest.param("instance-data.ec2.internal", True, id="aws-metadata-name"),
        pytest.param("169.254.169.254", True, id="metadata-literal"),
        pytest.param("2130706433", True, id="decimal-loopback"),
        pytest.param("0x7f000001", True, id="hex-loopback"),
        pytest.param("127.1", True, id="short-loopback"),
        pytest.param("0177.0.0.1", True, id="octal-loopback"),
        pytest.param("fe80::1%eth0", True, id="scoped"),
        pytest.param(PUBLIC_V4, False, id="public"),
        pytest.param(PRIVATE_V4, False, id="private"),
        pytest.param("registry.example.test", False, id="ordinary-name"),
        pytest.param("localhost.example.test", False, id="name-merely-containing-localhost"),
    ],
)
def test_a_saved_host_is_refused_when_it_is_or_names_an_internal_address(
    host: str, refused: bool
) -> None:
    assert (netpolicy.literal_host_refusal(host) == netpolicy.REFUSAL_TEXT) is refused


def test_a_permitted_host_is_not_refused_at_save_time(
    address_policy: Callable[..., None],
) -> None:
    address_policy("127.0.0.0/8", "::1/128")

    assert netpolicy.literal_host_refusal("127.0.0.1") is None
    assert netpolicy.literal_host_refusal("localhost") is None
    assert netpolicy.literal_host_refusal("metadata") == netpolicy.REFUSAL_TEXT


@pytest.mark.parametrize(
    ("host", "internal"),
    [
        pytest.param("127.0.0.1", True, id="loopback"),
        pytest.param("localhost", True, id="localhost"),
        pytest.param("anything.localhost", True, id="localhost-suffix"),
        pytest.param("metadata", True, id="metadata"),
        pytest.param("[::ffff:127.0.0.1]", True, id="bracketed-mapped"),
        pytest.param("0x7f.1", True, id="numeric-notation"),
        pytest.param(PUBLIC_V4, False, id="public"),
        pytest.param(PRIVATE_V4, False, id="private"),
        pytest.param("s3.example.test", False, id="name"),
    ],
)
def test_is_internal_host_ignores_the_permit(
    host: str, internal: bool, address_policy: Callable[..., None]
) -> None:
    address_policy("127.0.0.0/8", "169.254.0.0/16", "::1/128")

    assert netpolicy.is_internal_host(host) is internal


@pytest.mark.parametrize(
    ("sockaddr", "parsed"),
    [
        pytest.param(("127.0.0.1", 80), "127.0.0.1", id="v4"),
        pytest.param(("::1", 80, 0, 0), "::1", id="v6"),
        pytest.param(("fe80::1%eth0", 80, 0, 2), "fe80::1", id="scope-suffix-dropped"),
        pytest.param(("not-an-ip", 80), None, id="not-an-address"),
        pytest.param((), None, id="empty"),
    ],
)
def test_a_socket_address_yields_its_ip_or_nothing(
    sockaddr: tuple[Any, ...], parsed: str | None
) -> None:
    result = netpolicy.sockaddr_address(sockaddr)

    assert (None if result is None else str(result)) == parsed


@pytest.mark.parametrize("sockaddr", [("not-an-ip", 80), ()])
def test_a_socket_address_that_does_not_parse_may_not_be_dialled(sockaddr: tuple[Any, ...]) -> None:
    with pytest.raises(AddressRefused):
        netpolicy.check_sockaddr(sockaddr)


def test_a_refusal_is_logged_with_the_address_cut_to_a_fixed_length(
    logged: Callable[[str], list[dict[str, Any]]],
) -> None:
    netpolicy.log_refusal("9" * 500)

    [record] = logged("outbound_address_refused")
    assert len(record["address"]) == netpolicy.MAX_LOGGED_ADDRESS_CHARS


def test_configure_replaces_the_policy_and_the_fixture_puts_the_default_back(
    address_policy: Callable[..., None],
) -> None:
    address_policy("10.0.0.0/8")

    assert netpolicy.current().permitted == (ipaddress.ip_network("10.0.0.0/8"),)


def test_the_default_policy_permits_nothing_after_a_test_that_changed_it() -> None:
    assert netpolicy.current().permitted == ()


def resolving_to(*addresses: str) -> Callable[[str, int], Any]:
    async def resolve(host: str, port: int) -> list[Any]:
        return [
            (
                socket.AF_INET6 if ":" in text else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (text, port),
            )
            for text in addresses
        ]

    return resolve


async def fetch(transport: httpx.AsyncBaseTransport, url: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=transport) as client:
        return await client.get(url)


async def test_a_name_that_resolves_to_an_internal_address_is_refused_at_connect() -> None:
    transport = policy_transport(verify=False, resolver=resolving_to("127.0.0.1"))

    with pytest.raises(AddressRefused) as refused:
        await fetch(transport, f"https://registry.example.test:{TLS_PORT}/v2/")

    assert refused.value.address == "127.0.0.1"
    assert not isinstance(refused.value, httpx.HTTPError)


async def test_an_internal_literal_url_is_refused_without_a_lookup() -> None:
    async def forbidden(host: str, port: int) -> list[Any]:
        raise AssertionError("a literal address must not be resolved")

    transport = policy_transport(verify=False, resolver=forbidden)

    with pytest.raises(AddressRefused):
        await fetch(transport, f"http://169.254.169.254:{TLS_PORT}/latest/")


@pytest.mark.usefixtures("permit_loopback")
async def test_a_permitted_internal_address_passes_the_policy_and_fails_as_a_connect_error() -> (
    None
):
    transport = policy_transport(verify=False, resolver=resolving_to("127.0.0.1"))

    with pytest.raises(httpx.ConnectError):
        await fetch(transport, f"http://registry.example.test:{UNUSED_PORT}/")


async def test_a_name_with_no_usable_address_is_a_connect_error_not_a_refusal() -> None:
    transport = policy_transport(verify=False, resolver=resolving_to())

    with pytest.raises(httpx.ConnectError):
        await fetch(transport, f"http://nothing.example.test:{UNUSED_PORT}/")
