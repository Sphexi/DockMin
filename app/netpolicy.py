"""Address policy for connections to operator-named hosts: internal addresses need a permit."""

import ipaddress
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from app.logging import get_logger

if TYPE_CHECKING:
    from app.config import Settings

type IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
type IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

PERMIT_VARIABLE: Final = "DOCKMIN_PERMITTED_INTERNAL_NETWORKS"
REFUSAL_TEXT: Final = (
    f"This address is on this server or a link-local network. Set {PERMIT_VARIABLE} to allow it."
)

_log = get_logger(__name__)

_INTERNAL_V4: Final = tuple(
    ipaddress.ip_network(text) for text in ("0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16")
)
_INTERNAL_V6: Final = tuple(
    ipaddress.ip_network(text) for text in ("::/128", "::1/128", "fe80::/10", "fd00:ec2::/64")
)
_V4_COMPATIBLE: Final = ipaddress.ip_network("::/96")
_V4_TRANSLATED: Final = ipaddress.ip_network("::ffff:0:0:0/96")
_NAT64: Final = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL_USE: Final = ipaddress.ip_network("64:ff9b:1::/48")
_SIX_TO_FOUR: Final = ipaddress.ip_network("2002::/16")
_UNSPECIFIED_V6: Final = ipaddress.IPv6Address("::")
_LOOPBACK_V6: Final = ipaddress.IPv6Address("::1")

_LOOPBACK_ADDRESSES: Final = (ipaddress.IPv4Address("127.0.0.1"), _LOOPBACK_V6)
_METADATA_ADDRESSES: Final = (
    ipaddress.IPv4Address("169.254.169.254"),
    ipaddress.IPv6Address("fd00:ec2::254"),
)
_INTERNAL_NAMES: Final[Mapping[str, tuple[IpAddress, ...]]] = {
    "localhost": _LOOPBACK_ADDRESSES,
    "metadata": _METADATA_ADDRESSES,
    "metadata.goog": _METADATA_ADDRESSES,
    "metadata.google.internal": _METADATA_ADDRESSES,
    "instance-data": _METADATA_ADDRESSES,
    "instance-data.ec2.internal": _METADATA_ADDRESSES,
}
_NUMERIC_HOST: Final = frozenset("0123456789abcdefABCDEFxX.")
MAX_LOGGED_REFUSALS: Final = 3
MAX_LOGGED_ADDRESS_CHARS: Final = 64


class AddressRefused(Exception):
    """A connection to an internal address that no permit covers; `address` is the literal."""

    def __init__(self, address: str) -> None:
        super().__init__(REFUSAL_TEXT)
        self.address = address


def _unscoped(address: IpAddress) -> IpAddress:
    if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
        return ipaddress.IPv6Address(int(address))
    return address


def _embedded_v4(address: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    mapped = address.ipv4_mapped
    if mapped is not None:
        return [mapped]
    packed = address.packed
    if (
        address in _NAT64
        or address in _V4_TRANSLATED
        or (address in _V4_COMPATIBLE and address not in (_UNSPECIFIED_V6, _LOOPBACK_V6))
    ):
        return [ipaddress.IPv4Address(packed[12:])]
    if address in _SIX_TO_FOUR:
        return [ipaddress.IPv4Address(packed[2:6])]
    if address in _NAT64_LOCAL_USE:
        # The local-use block may be carved into prefixes of any length, so every placement counts.
        return [
            ipaddress.IPv4Address(packed[6:8] + packed[9:11]),
            ipaddress.IPv4Address(packed[7:8] + packed[9:12]),
            ipaddress.IPv4Address(packed[9:13]),
            ipaddress.IPv4Address(packed[12:]),
        ]
    return []


def _in_internal_class(address: IpAddress) -> bool:
    networks = _INTERNAL_V4 if address.version == 4 else _INTERNAL_V6
    return any(address in network for network in networks)


def _internal_forms(address: IpAddress) -> list[IpAddress]:
    forms: list[IpAddress] = [address] if _in_internal_class(address) else []
    if isinstance(address, ipaddress.IPv6Address):
        forms.extend(form for form in _embedded_v4(address) if _in_internal_class(form))
    return forms


@dataclass(frozen=True, slots=True)
class AddressPolicy:
    """Which internal addresses connections may reach; every other address is allowed."""

    permitted: tuple[IpNetwork, ...] = ()

    def is_internal(self, address: IpAddress) -> bool:
        """Report whether the address, or the IPv4 address it embeds, is internal."""
        return bool(_internal_forms(_unscoped(address)))

    def allows(self, address: IpAddress) -> bool:
        """Report whether a connection to the address may proceed."""
        plain = _unscoped(address)
        forms = _internal_forms(plain)
        return not forms or self._covers(plain) or all(self._covers(form) for form in forms)

    def check(self, address: IpAddress) -> None:
        """Raise AddressRefused unless a connection to the address may proceed."""
        if not self.allows(address):
            raise AddressRefused(str(_unscoped(address)))

    @classmethod
    def from_settings(cls, settings: "Settings") -> "AddressPolicy":
        """Build the policy from the permitted networks setting."""
        return cls(
            tuple(
                ipaddress.ip_network(text, strict=False)
                for text in settings.permitted_internal_networks
            )
        )

    def _covers(self, address: IpAddress) -> bool:
        return any(address in network for network in self.permitted)


_policy = AddressPolicy()


def configure(policy: AddressPolicy) -> None:
    """Set the process-wide policy."""
    global _policy
    _policy = policy


def current() -> AddressPolicy:
    """Return the process-wide policy."""
    return _policy


def sockaddr_address(sockaddr: tuple[Any, ...]) -> IpAddress | None:
    """Return the IP address of a socket address, or None when it does not parse."""
    try:
        return ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
    except (ValueError, IndexError):
        return None


def ip_literal(host: str) -> IpAddress | None:
    """Return the address when host is an IP literal, else None."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def log_refusal(address: str) -> None:
    """Record a refused connection; the address is capped because it may come from a peer."""
    _log.warning("outbound_address_refused", extra={"address": address[:MAX_LOGGED_ADDRESS_CHARS]})


def refusal(address: IpAddress) -> AddressRefused:
    """Build the refusal for an address and record it."""
    refused = AddressRefused(str(_unscoped(address)))
    log_refusal(refused.address)
    return refused


def check_sockaddr(sockaddr: tuple[Any, ...]) -> None:
    """Raise AddressRefused unless the socket address may be dialled; an unreadable one may not."""
    address = sockaddr_address(sockaddr)
    try:
        if address is None:
            raise AddressRefused(str(sockaddr[0]) if sockaddr else "")
        current().check(address)
    except AddressRefused as refusal:
        log_refusal(refusal.address)
        raise


def literal_host_refusal(host: str) -> str | None:
    """Return the refusal text when host is an internal IP literal or name that no permit covers."""
    name = _host_name(host)
    policy = current()
    literal = _numeric_host(name)
    if literal is not None:
        return None if policy.allows(literal) else REFUSAL_TEXT
    representatives = _internal_name_addresses(name)
    if representatives is not None and not any(policy.allows(a) for a in representatives):
        return REFUSAL_TEXT
    return None


def is_internal_host(host: str) -> bool:
    """Report whether host is an internal IP literal or a name that stands for one, permit aside."""
    name = _host_name(host)
    literal = _numeric_host(name)
    if literal is not None:
        return current().is_internal(literal)
    return _internal_name_addresses(name) is not None


def _host_name(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


def _internal_name_addresses(name: str) -> tuple[IpAddress, ...] | None:
    addresses = _INTERNAL_NAMES.get(name)
    if addresses is None and name.endswith(".localhost"):
        return _LOOPBACK_ADDRESSES
    return addresses


def _numeric_host(name: str) -> IpAddress | None:
    literal = ip_literal(name.split("%", 1)[0])
    if literal is not None or not name or not _NUMERIC_HOST.issuperset(name):
        return literal
    try:
        return ipaddress.IPv4Address(socket.inet_aton(name))
    except OSError:
        return None
