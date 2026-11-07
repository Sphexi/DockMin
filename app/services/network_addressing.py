"""Network create form parsing, addressing and driver-option rules, and static address checks."""

import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
from types import MappingProxyType
from typing import Final, Literal

from app.auth.errors import ValidationFailed

__all__ = [
    "OFFERED_DRIVERS",
    "STANDARD_OPTIONS",
    "AddressingInvalid",
    "NetworkRequest",
    "Pool",
    "is_id_shaped",
    "overlapping",
    "parse_network_form",
    "parse_static_address",
    "pools_of",
    "restricted_network_options",
]

Driver = Literal["bridge", "overlay", "macvlan", "ipvlan"]
AnyNetwork = IPv4Network | IPv6Network
AnyAddress = IPv4Address | IPv6Address

OFFERED_DRIVERS: Final[Mapping[Literal["standalone", "swarm"], tuple[str, ...]]] = {
    "standalone": ("bridge", "macvlan", "ipvlan"),
    "swarm": ("bridge", "overlay", "macvlan", "ipvlan"),
}

MTU_OPTION: Final = "com.docker.network.driver.mtu"
ICC_OPTION: Final = "com.docker.network.bridge.enable_icc"
MASQUERADE_OPTION: Final = "com.docker.network.bridge.enable_ip_masquerade"
ENCRYPTED_OPTION: Final = "encrypted"

STANDARD_OPTIONS: Final[Mapping[str, frozenset[str]]] = {
    "bridge": frozenset({ICC_OPTION, MASQUERADE_OPTION, MTU_OPTION}),
    "overlay": frozenset({ENCRYPTED_OPTION, MTU_OPTION}),
}

MAX_OPTIONS: Final = 32
MAX_LABELS: Final = 64
MAX_LABEL_CHARS: Final = 64 * 1024
MAX_OPTION_VALUE: Final = 256
MAX_POOLS_READ: Final = 64
MIN_MTU: Final = 68
MIN_MTU_IPV6: Final = 1280
MAX_MTU: Final = 65535
IPV4_PREFIXES: Final = range(8, 31)
IPV6_PREFIXES: Final = range(48, 121)

PREDEFINED_NAMES: Final = frozenset({"bridge", "host", "none", "ingress", "docker_gwbridge"})
_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
_ID_SHAPED: Final = re.compile(r"[0-9a-f]{64}|[0-9a-z]{25}")
_OPTION_KEY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_INTERFACE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,14}")
_DIGITS: Final = re.compile(r"[0-9]{1,5}")
_ADDRESS: Final = re.compile(r"[0-9A-Fa-f:.]{2,45}")
_CIDR: Final = re.compile(r"[0-9A-Fa-f:.]{2,45}/[0-9]{1,3}")
_ROW: Final = re.compile(r"(option|label)-([0-9]{1,4})-(key|value)")
_TRUTHY: Final = frozenset({"on", "true", "1", "yes"})

_MACVLAN_MODES: Final = frozenset({"bridge", "vepa", "passthru", "private"})
_IPVLAN_MODES: Final = frozenset({"l2", "l3", "l3s"})
_IPVLAN_FLAGS: Final = frozenset({"bridge", "private", "vepa"})

_REFUSED_V4: Final = tuple(
    IPv4Network(text)
    for text in (
        "0.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_REFUSED_V6: Final = tuple(
    IPv6Network(text) for text in ("::/128", "::1/128", "::ffff:0:0/96", "fe80::/10", "ff00::/8")
)
_EXAMPLE: Final = {4: "10.20.0.0/24", 6: "fd00:20::/64"}
_PREFIX_RANGE_TEXT: Final = {4: "8 to 30", 6: "48 to 120"}


class AddressingInvalid(ValidationFailed):
    """Raised when a network form or address is unacceptable; errors maps a form field to text."""

    @property
    def errors(self) -> dict[str, str]:
        """Return the message for each refused form field."""
        return self.fields


@dataclass(frozen=True, slots=True)
class Pool:
    """One IPAM pool: a subnet, with the gateway and allocation range when set."""

    subnet: AnyNetwork
    gateway: AnyAddress | None
    ip_range: AnyNetwork | None


@dataclass(frozen=True, slots=True)
class NetworkRequest:
    """A validated network create request."""

    name: str
    driver: Driver
    internal: bool
    attachable: bool
    enable_ipv6: bool
    pools: tuple[Pool, ...]
    options: Mapping[str, str]
    labels: Mapping[str, str]


class _Rejected(Exception):
    """Carries the text for one refused field out of a helper."""


class _Errors:
    """Collects at most one message per form field, the first one raised."""

    def __init__(self) -> None:
        self.fields: dict[str, str] = {}

    def add(self, field: str, message: str) -> None:
        self.fields.setdefault(field, message)


def is_id_shaped(name: str) -> bool:
    """Report whether a name has the form of a 64-hex or 25-character base-36 network id."""
    return _ID_SHAPED.fullmatch(name) is not None


def parse_network_form(form: Mapping[str, str], *, environment_type: str | None) -> NetworkRequest:
    """Validate a network create form into a request, or raise AddressingInvalid."""
    errors = _Errors()
    name = form.get("name", "").strip()
    _check_name(name, errors)
    driver = _check_driver(form.get("driver", "").strip(), environment_type, errors)
    internal = _checked(form.get("internal", ""))
    attachable = _checked(form.get("attachable", ""))
    enable_ipv6 = _checked(form.get("enable_ipv6", ""))
    if attachable and driver is not None and driver != "overlay":
        errors.add("attachable", "Only overlay networks can be made attachable.")
        attachable = False

    pools = _build_pools(form, enable_ipv6, errors)
    if driver in ("macvlan", "ipvlan") and not pools:
        errors.add("subnet_v4", f"A {driver} network needs a subnet.")

    ipv6 = enable_ipv6 or any(pool.subnet.version == 6 for pool in pools)
    options = _parse_rows(form, "option", errors, ipv6=ipv6)
    labels = _parse_rows(form, "label", errors, ipv6=ipv6)

    if errors.fields or driver is None:
        raise AddressingInvalid(errors.fields)
    return NetworkRequest(
        name=name,
        driver=driver,
        internal=internal,
        attachable=attachable,
        enable_ipv6=enable_ipv6,
        pools=pools,
        options=MappingProxyType(options),
        labels=MappingProxyType(labels),
    )


def restricted_network_options(request: NetworkRequest) -> tuple[str, ...]:
    """Return the option keys outside the standard-user allow-list for the request's driver."""
    allowed = STANDARD_OPTIONS.get(request.driver, frozenset())
    return tuple(key for key in request.options if key not in allowed)


def overlapping(request: NetworkRequest, existing: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """Return the names of existing networks whose subnet overlaps one of the request's."""
    found: list[str] = []
    for name, subnet_text in existing:
        other = _loose_network(subnet_text)
        if other is None or name in found:
            continue
        if any(_overlaps(pool.subnet, other) for pool in request.pools):
            found.append(name)
    return tuple(found)


def parse_static_address(
    text: str,
    *,
    pools: Sequence[Pool],
    enable_ipv6: bool,
    in_use: Collection[str],
    field: str = "address",
    version: Literal[4, 6] | None = None,
) -> AnyAddress:
    """Validate a static container address against a network's pools, or raise AddressingInvalid."""
    try:
        return _static_address(text.strip(), pools, enable_ipv6, in_use, version)
    except _Rejected as refusal:
        raise AddressingInvalid({field: str(refusal)}) from None


def pools_of(network: object) -> tuple[Pool, ...]:
    """Read the IPAM pools from an Engine network, skipping entries that are not usable."""
    pools: list[Pool] = []
    for entry in _ipam_entries(network)[:MAX_POOLS_READ]:
        subnet = _loose_network(_text(_field(entry, "subnet", "Subnet")))
        if subnet is None:
            continue
        gateway = _loose_address(_text(_field(entry, "gateway", "Gateway")))
        if gateway is not None and not _contains(subnet, gateway):
            gateway = None
        ip_range = _loose_network(_text(_field(entry, "ip_range", "IPRange")))
        if ip_range is not None and not _subnet_of(ip_range, subnet):
            ip_range = None
        pools.append(Pool(subnet, gateway, ip_range))
    return tuple(pools)


def _checked(text: str) -> bool:
    return text.strip().casefold() in _TRUTHY


def _check_name(name: str, errors: _Errors) -> None:
    if not name:
        errors.add("name", "Enter a name.")
    elif _NAME.fullmatch(name) is None:
        errors.add(
            "name",
            "Use letters, digits, underscore, period and hyphen, starting with a letter or "
            "digit, up to 255 characters.",
        )
    elif is_id_shaped(name):
        errors.add("name", "This name has the form of a network id and cannot be used.")
    elif name.casefold() in PREDEFINED_NAMES:
        errors.add("name", "This name is reserved by Docker.")


def _check_driver(text: str, environment_type: str | None, errors: _Errors) -> Driver | None:
    drivers: dict[str, Driver] = {
        "bridge": "bridge",
        "overlay": "overlay",
        "macvlan": "macvlan",
        "ipvlan": "ipvlan",
    }
    driver = drivers.get(text)
    if driver is None:
        errors.add("driver", "Choose one of the offered drivers.")
        return None
    offered = OFFERED_DRIVERS["swarm" if environment_type == "swarm" else "standalone"]
    if driver not in offered:
        errors.add("driver", "This driver is not available on this environment.")
        return None
    return driver


def _build_pools(form: Mapping[str, str], enable_ipv6: bool, errors: _Errors) -> tuple[Pool, ...]:
    pools: list[Pool] = []
    families: tuple[Literal[4, 6], ...] = (4, 6)
    for family in families:
        suffix = f"_v{family}"
        subnet_text = form.get(f"subnet{suffix}", "").strip()
        gateway_text = form.get(f"gateway{suffix}", "").strip()
        range_text = form.get(f"ip_range{suffix}", "").strip()
        if not subnet_text:
            for field, text in (
                (f"gateway{suffix}", gateway_text),
                (f"ip_range{suffix}", range_text),
            ):
                if text:
                    errors.add(field, f"Enter the IPv{family} subnet first.")
            continue
        if family == 6 and not enable_ipv6:
            errors.add("subnet_v6", "Turn on IPv6 to use an IPv6 subnet.")
            continue
        pool = _build_pool(family, subnet_text, gateway_text, range_text, suffix, errors)
        if pool is not None:
            pools.append(pool)
    return tuple(pools)


def _build_pool(
    family: Literal[4, 6],
    subnet_text: str,
    gateway_text: str,
    range_text: str,
    suffix: str,
    errors: _Errors,
) -> Pool | None:
    noun = f"IPv{family} subnet"
    try:
        subnet = _strict_network(subnet_text, family, noun)
        _check_not_refused(subnet, noun)
        _check_prefix(subnet, family, noun)
    except _Rejected as refusal:
        errors.add(f"subnet{suffix}", str(refusal))
        return None

    gateway: AnyAddress | None = None
    if gateway_text:
        try:
            gateway = _checked_gateway(gateway_text, subnet, family)
        except _Rejected as refusal:
            errors.add(f"gateway{suffix}", str(refusal))
    ip_range: AnyNetwork | None = None
    if range_text:
        try:
            ip_range = _strict_network(range_text, family, f"IPv{family} IP range")
            if not _subnet_of(ip_range, subnet):
                raise _Rejected("The IP range must lie inside the subnet.")
        except _Rejected as refusal:
            errors.add(f"ip_range{suffix}", str(refusal))
            ip_range = None
    return Pool(subnet, gateway, ip_range)


def _strict_network(text: str, family: int, noun: str) -> AnyNetwork:
    if _CIDR.fullmatch(text) is None:
        raise _Rejected(f"Enter the {noun} in CIDR form, for example {_EXAMPLE[family]}.")
    try:
        network = ip_network(text)
    except ValueError:
        loose = _loose_network(text)
        if loose is None:
            raise _Rejected(
                f"Enter the {noun} in CIDR form, for example {_EXAMPLE[family]}."
            ) from None
        raise _Rejected(f"The {noun} has host bits set; the network is {loose}.") from None
    if network.version != family:
        raise _Rejected(f"The {noun} must be an IPv{family} network.")
    return network


def _check_prefix(subnet: AnyNetwork, family: int, noun: str) -> None:
    allowed = IPV4_PREFIXES if family == 4 else IPV6_PREFIXES
    if subnet.prefixlen not in allowed:
        raise _Rejected(f"The {noun} prefix length must be {_PREFIX_RANGE_TEXT[family]}.")


def _check_not_refused(subnet: AnyNetwork, noun: str) -> None:
    for refused in _REFUSED_V4 + _REFUSED_V6:
        if _overlaps(subnet, refused):
            raise _Rejected(f"The {noun} overlaps {refused}, which cannot be used here.")


def _checked_gateway(text: str, subnet: AnyNetwork, family: int) -> AnyAddress:
    noun = f"IPv{family} gateway"
    address = _loose_address(text)
    if address is None:
        raise _Rejected(f"Enter the {noun} as an address, for example {_example_address(family)}.")
    if address.version != family:
        raise _Rejected(f"The {noun} must be an IPv{family} address.")
    if not _contains(subnet, address):
        raise _Rejected("The gateway must lie inside the subnet.")
    if address == subnet.network_address:
        raise _Rejected("The gateway cannot be the network address of the subnet.")
    if isinstance(subnet, IPv4Network) and address == subnet.broadcast_address:
        raise _Rejected("The gateway cannot be the broadcast address of the subnet.")
    return address


def _example_address(family: int) -> str:
    return "10.20.0.1" if family == 4 else "fd00:20::1"


def _parse_rows(
    form: Mapping[str, str], kind: Literal["option", "label"], errors: _Errors, *, ipv6: bool
) -> dict[str, str]:
    rows: dict[int, dict[str, str]] = {}
    for name, value in form.items():
        match = _ROW.fullmatch(name)
        if match is not None and match[1] == kind:
            rows.setdefault(int(match[2]), {})[match[3]] = value

    result: dict[str, str] = {}
    keys_seen: dict[str, int] = {}
    chars = 0
    cap = MAX_OPTIONS if kind == "option" else MAX_LABELS
    noun = "option" if kind == "option" else "label"
    for index in sorted(rows):
        raw_key = rows[index].get("key", "")
        raw_value = rows[index].get("value", "")
        key = raw_key.strip() if kind == "option" else raw_key
        value = raw_value.strip() if kind == "option" else raw_value
        if not key.strip() and not value.strip():
            continue
        key_field = f"{kind}-{index}-key"
        value_field = f"{kind}-{index}-value"
        if len(result) >= cap:
            errors.add(f"{kind}s", f"At most {cap} {noun}s are allowed.")
            break
        problem = _row_problem(kind, key, value, ipv6=ipv6)
        if problem is not None:
            errors.add(key_field if problem[0] == "key" else value_field, problem[1])
            continue
        folded = key.casefold() if kind == "option" else key
        if folded in keys_seen:
            errors.add(
                key_field, f"This {noun} key is already used in row {keys_seen[folded] + 1}."
            )
            continue
        keys_seen[folded] = index
        chars += len(key) + len(value)
        result[key] = value
    if kind == "label" and chars > MAX_LABEL_CHARS:
        errors.add("labels", "Labels must total at most 64 KiB.")
    return result


def _row_problem(
    kind: str, key: str, value: str, *, ipv6: bool
) -> tuple[Literal["key", "value"], str] | None:
    if kind == "option":
        if not key:
            return ("key", "Enter an option key.")
        if _OPTION_KEY.fullmatch(key) is None:
            return (
                "key",
                "Use letters, digits, period, underscore and hyphen, starting with a letter or "
                "digit, up to 128 characters.",
            )
        if len(value) > MAX_OPTION_VALUE or _has_control(value):
            return ("value", "Use at most 256 characters without control characters.")
        message = _option_value_problem(key, value, ipv6=ipv6)
        return None if message is None else ("value", message)
    if not key.strip():
        return ("key", "Label key is required.")
    if len(key) > 255 or _has_control(key):
        return ("key", "Label key must be at most 255 characters without control characters.")
    if "\0" in value:
        return ("value", "Label value must not contain NUL.")
    return None


def _option_value_problem(key: str, value: str, *, ipv6: bool) -> str | None:
    match key.casefold():
        case "com.docker.network.driver.mtu":
            floor = MIN_MTU_IPV6 if ipv6 else MIN_MTU
            if _DIGITS.fullmatch(value) is None or not floor <= int(value) <= MAX_MTU:
                suffix = " when IPv6 is in use" if ipv6 else ""
                return f"The MTU must be a whole number from {floor} to {MAX_MTU}{suffix}."
        case "com.docker.network.bridge.enable_icc" | (
            "com.docker.network.bridge.enable_ip_masquerade"
        ):
            if value not in ("true", "false"):
                return "Use true or false."
        case "encrypted":
            if value not in ("", "true"):
                return "Leave empty or use true."
        case "macvlan_mode":
            return _one_of(value, _MACVLAN_MODES)
        case "ipvlan_mode":
            return _one_of(value, _IPVLAN_MODES)
        case "ipvlan_flag":
            return _one_of(value, _IPVLAN_FLAGS)
        case "parent":
            if _INTERFACE.fullmatch(value) is None:
                return "Enter a host interface name of at most 15 characters, such as eth0.10."
    return None


def _one_of(value: str, allowed: frozenset[str]) -> str | None:
    if value in allowed:
        return None
    return "Use one of: " + ", ".join(sorted(allowed)) + "."


def _has_control(text: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in text)


def _static_address(
    text: str,
    pools: Sequence[Pool],
    enable_ipv6: bool,
    in_use: Collection[str],
    version: Literal[4, 6] | None,
) -> AnyAddress:
    if not text:
        raise _Rejected("Enter an address.")
    address = _loose_address(text)
    if address is None:
        raise _Rejected("Enter a valid IP address.")
    if version is not None and address.version != version:
        raise _Rejected(f"Enter an IPv{version} address.")
    if address.version == 6 and not enable_ipv6:
        raise _Rejected("IPv6 is not enabled on this network.")
    candidates = [pool for pool in pools if pool.subnet.version == address.version]
    if not candidates:
        raise _Rejected(f"This network has no IPv{address.version} subnet configured.")
    pool = next((pool for pool in candidates if _contains(pool.subnet, address)), None)
    if pool is None:
        raise _Rejected("The address must lie inside the network's subnet.")
    if address == pool.subnet.network_address:
        raise _Rejected("The address cannot be the network address of the subnet.")
    if isinstance(pool.subnet, IPv4Network) and address == pool.subnet.broadcast_address:
        raise _Rejected("The address cannot be the broadcast address of the subnet.")
    if pool.gateway is not None and address == pool.gateway:
        raise _Rejected("The address is the network's gateway.")
    if any(_loose_address(held.partition("/")[0]) == address for held in in_use):
        raise _Rejected("This address is already in use on the network.")
    return address


def _loose_address(text: str) -> AnyAddress | None:
    text = text.strip()
    if _ADDRESS.fullmatch(text) is None:
        return None
    try:
        return ip_address(text)
    except ValueError:
        return None


def _loose_network(text: str) -> AnyNetwork | None:
    text = text.strip()
    if _CIDR.fullmatch(text) is None:
        return None
    try:
        return ip_network(text, strict=False)
    except ValueError:
        return None


def _overlaps(first: AnyNetwork, second: AnyNetwork) -> bool:
    if isinstance(first, IPv4Network) and isinstance(second, IPv4Network):
        return first.overlaps(second)
    if isinstance(first, IPv6Network) and isinstance(second, IPv6Network):
        return first.overlaps(second)
    return False


def _subnet_of(inner: AnyNetwork, outer: AnyNetwork) -> bool:
    if isinstance(inner, IPv4Network) and isinstance(outer, IPv4Network):
        return inner.subnet_of(outer)
    if isinstance(inner, IPv6Network) and isinstance(outer, IPv6Network):
        return inner.subnet_of(outer)
    return False


def _contains(network: AnyNetwork, address: AnyAddress) -> bool:
    if isinstance(network, IPv4Network) and isinstance(address, IPv4Address):
        return address in network
    if isinstance(network, IPv6Network) and isinstance(address, IPv6Address):
        return address in network
    return False


def _field(entry: object, attribute: str, key: str) -> object:
    if isinstance(entry, Mapping):
        return entry.get(key, entry.get(attribute))
    return getattr(entry, attribute, None)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _ipam_entries(network: object) -> list[object]:
    if isinstance(network, Mapping):
        ipam: object = network.get("IPAM", network.get("ipam"))
    else:
        ipam = getattr(network, "ipam", None)
    if isinstance(ipam, Mapping):
        ipam = ipam.get("Config", ipam.get("config"))
    elif ipam is not None and not isinstance(ipam, (list, tuple)):
        ipam = getattr(ipam, "config", None)
    return list(ipam) if isinstance(ipam, (list, tuple)) else []
