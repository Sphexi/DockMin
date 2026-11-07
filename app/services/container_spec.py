"""The container create payload: spec model, form parsing and validation, and the Engine body."""

import ipaddress
import posixpath
import re
import shlex
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.auth.errors import ValidationFailed
from app.registry_client.references import ReferenceInvalid, split_reference
from app.services.access import RESERVED_LABELS, SubmittedLabels

Group = Literal[
    "env", "labels", "ports", "mounts", "networks", "devices", "extra_hosts", "log_options"
]
PullPolicy = Literal["missing", "always", "never"]
NetworkMode = Literal["bridge", "none", "host", "networks"]
HealthcheckMode = Literal["image", "none", "command"]

GROUPS: Final[tuple[Group, ...]] = (
    "env",
    "labels",
    "ports",
    "mounts",
    "networks",
    "devices",
    "extra_hosts",
    "log_options",
)
ROW_CAPS: Final[Mapping[str, int]] = {
    "env": 100,
    "labels": 64,
    "ports": 32,
    "mounts": 32,
    "networks": 8,
    "devices": 16,
    "extra_hosts": 32,
    "log_options": 16,
}
ROW_PREFIX: Final[Mapping[str, str]] = {
    "env": "env",
    "labels": "label",
    "ports": "port",
    "mounts": "mount",
    "networks": "network",
    "devices": "device",
    "extra_hosts": "host",
    "log_options": "logopt",
}
ROW_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    "env": ("key", "value"),
    "labels": ("key", "value"),
    "ports": ("host_ip", "host_port", "container_port", "protocol"),
    "mounts": ("type", "source", "target", "read_only", "tmpfs_size"),
    "networks": ("name", "aliases", "ipv4"),
    "devices": ("host_path", "container_path", "permissions"),
    "extra_hosts": ("name", "address"),
    "log_options": ("key", "value"),
}
ROW_CHECKBOXES: Final[Mapping[str, tuple[str, ...]]] = {"mounts": ("read_only",)}
# Selects that submit a value even in an otherwise empty row.
ROW_DEFAULTED: Final[Mapping[str, tuple[str, ...]]] = {
    "ports": ("protocol",),
    "mounts": ("type",),
    "devices": ("permissions",),
}
SPARE_ROWS: Final[Mapping[str, int]] = {
    "env": 3,
    "labels": 3,
    "ports": 3,
    "mounts": 3,
    "networks": 1,
    "devices": 1,
    "extra_hosts": 1,
    "log_options": 1,
}
GROUP_NOUNS: Final[Mapping[str, str]] = {
    "env": "environment variables",
    "labels": "labels",
    "ports": "port mappings",
    "mounts": "mounts",
    "networks": "networks",
    "devices": "devices",
    "extra_hosts": "extra hosts",
    "log_options": "log options",
}

SCALAR_FIELDS: Final[tuple[str, ...]] = (
    "image",
    "pull",
    "name",
    "command",
    "entrypoint",
    "working_dir",
    "user",
    "publish_all",
    "network_mode",
    "restart_policy",
    "restart_max_retries",
    "cpus",
    "cpu_shares",
    "memory",
    "memory_swap",
    "pids_limit",
    "shm_size",
    "cap_add",
    "cap_drop",
    "dns_servers",
    "dns_search",
    "dns_options",
    "healthcheck_mode",
    "healthcheck_test",
    "healthcheck_interval",
    "healthcheck_timeout",
    "healthcheck_start_period",
    "healthcheck_start_interval",
    "healthcheck_retries",
    "log_driver",
    "runtime",
    "init",
    "privileged",
    "pid_host",
    "ipc_host",
    "start",
)
CHECKBOX_FIELDS: Final[frozenset[str]] = frozenset(
    {"publish_all", "init", "privileged", "pid_host", "ipc_host", "start"}
)
ADMIN_ONLY_FIELDS: Final[frozenset[str]] = frozenset(
    {"privileged", "cap_add", "runtime", "pid_host", "ipc_host"}
)
ADMIN_ONLY_GROUPS: Final[frozenset[str]] = frozenset({"devices"})
ADMIN_ONLY_MOUNT_TYPES: Final[frozenset[str]] = frozenset({"bind"})
ADMIN_ONLY_NETWORK_MODES: Final[frozenset[str]] = frozenset({"host"})

MAX_ENV_CHARS: Final = 64 * 1024
MAX_LABEL_CHARS: Final = 64 * 1024
MAX_COMMAND_CHARS: Final = 4096
MAX_EXPANDED_PORTS: Final = 4096
MIN_MEMORY_BYTES: Final = 6 * 1024 * 1024
MAX_BYTES: Final = 2**63 - 1
MAX_INT32: Final = 2**31 - 1
MAX_CPUS: Final = 100_000
NS_PER_MS: Final = 1_000_000
MIN_DURATION_NS: Final = NS_PER_MS
MAX_DURATION_NS: Final = 24 * 3600 * 1_000_000_000
BLANK_MESSAGE: Final = "This value is not valid."

_TRUTHY: Final = frozenset({"on", "true", "1", "yes"})
_RESERVED_FOLDED: Final = frozenset(label.casefold() for label in RESERVED_LABELS)

RESOURCE_NAME: Final = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,254}")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
USER_PATTERN: Final = re.compile(r"[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?")
_MEMORY: Final = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?)(i?b)?", re.IGNORECASE)
_DURATION: Final = re.compile(r"(?:[0-9]+(?:ms|s|m|h))+")
_DURATION_PART: Final = re.compile(r"([0-9]+)(ms|s|m|h)")
_PORT_RANGE: Final = re.compile(r"([0-9]{1,5})(?:-([0-9]{1,5}))?")
DNS_LABEL: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_DNS_OPTION: Final = re.compile(r"[A-Za-z0-9_:.-]{1,128}")
_LOG_DRIVER: Final = re.compile(r"[a-z0-9_-]+")
_LOG_KEY: Final = re.compile(r"[a-z0-9_.-]+")
_RUNTIME: Final = re.compile(r"[A-Za-z0-9_.-]+")
_ROW_KEY: Final = re.compile(
    r"(env|label|port|mount|network|device|host|logopt)-([0-9]{1,4})-([a-z_0-9]+)"
)
_LIST_SPLIT: Final = re.compile(r"[\s,]+")

_UNSTRIPPED: Final = {
    "env": ("key", "value"),
    "labels": ("key", "value"),
    "log_options": ("value",),
}
_RESTART_POLICIES: Final = ("no", "always", "unless-stopped", "on-failure")
_UNITS: Final = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4}
_PREFIX_GROUP: Final = {prefix: group for group, prefix in ROW_PREFIX.items()}

CAPABILITIES: Final[frozenset[str]] = frozenset(
    [
        "AUDIT_CONTROL",
        "AUDIT_READ",
        "AUDIT_WRITE",
        "BLOCK_SUSPEND",
        "BPF",
        "CHECKPOINT_RESTORE",
        "CHOWN",
        "DAC_OVERRIDE",
        "DAC_READ_SEARCH",
        "FOWNER",
        "FSETID",
        "IPC_LOCK",
        "IPC_OWNER",
        "KILL",
        "LEASE",
        "LINUX_IMMUTABLE",
        "MAC_ADMIN",
        "MAC_OVERRIDE",
        "MKNOD",
        "NET_ADMIN",
        "NET_BIND_SERVICE",
        "NET_BROADCAST",
        "NET_RAW",
        "PERFMON",
        "SETFCAP",
        "SETGID",
        "SETPCAP",
        "SETUID",
        "SYSLOG",
        "SYS_ADMIN",
        "SYS_BOOT",
        "SYS_CHROOT",
        "SYS_MODULE",
        "SYS_NICE",
        "SYS_PACCT",
        "SYS_PTRACE",
        "SYS_RAWIO",
        "SYS_RESOURCE",
        "SYS_TIME",
        "SYS_TTY_CONFIG",
        "WAKE_ALARM",
    ]
)


class _Invalid(ValueError):
    """A validation failure whose text is shown to the user."""


def parse_image(text: str) -> str:
    """Return a normalised image reference, adding the latest tag when none is given."""
    try:
        return split_reference(text).typed()
    except ReferenceInvalid as error:
        raise _Invalid(error.message) from None


def parse_container_name(text: str) -> str | None:
    """Return a valid container name, None when blank."""
    value = text.strip()
    if not value:
        return None
    if len(value) > 128 or RESOURCE_NAME.fullmatch(value) is None:
        raise _Invalid(
            "Name must start with a letter or digit and use only letters, digits, "
            "underscores, periods and hyphens."
        )
    if _HEX64.fullmatch(value):
        raise _Invalid("Name must not be 64 hexadecimal digits, which Docker reads as an id.")
    return value


def parse_argv(text: str, label: str) -> tuple[str, ...] | None:
    """Split a command line the way a POSIX shell would, None when blank."""
    if not text.strip():
        return None
    if len(text) > MAX_COMMAND_CHARS or "\0" in text:
        raise _Invalid(f"{label} must be at most {MAX_COMMAND_CHARS} characters.")
    try:
        argv = tuple(shlex.split(text))
    except ValueError:
        raise _Invalid(f"{label} has an unclosed quote or a trailing backslash.") from None
    return argv or None


def parse_memory(text: str, label: str, *, allow_unlimited: bool = False) -> int:
    """Parse a size such as 512m or 1.5g into bytes; units are multiples of 1024."""
    value = text.strip()
    if allow_unlimited and value == "-1":
        return -1
    match = _MEMORY.fullmatch(value)
    if match is None or (not match[2] and (match[3] or "").lower() == "ib"):
        raise _Invalid(f"{label} must be a number of bytes or use a unit such as 512m or 2g.")
    amount = Decimal(match[1]) * 1024 ** _UNITS[match[2].lower()]
    if amount != amount.to_integral_value():
        raise _Invalid(f"{label} must be a whole number of bytes.")
    if amount > MAX_BYTES:
        raise _Invalid(f"{label} is too large.")
    return int(amount)


def parse_duration(text: str, label: str) -> int:
    """Parse a duration such as 30s or 1m30s into nanoseconds, between 1 ms and 24 h."""
    value = text.strip()
    if _DURATION.fullmatch(value) is None:
        raise _Invalid(f"{label} must be a duration such as 500ms, 30s or 1m30s.")
    scale = {
        "ms": NS_PER_MS,
        "s": 1000 * NS_PER_MS,
        "m": 60_000 * NS_PER_MS,
        "h": 3_600_000 * NS_PER_MS,
    }
    total = sum(int(n) * scale[unit] for n, unit in _DURATION_PART.findall(value))
    return _duration_in_range(total, label)


def _duration_in_range(nanoseconds: int, label: str) -> int:
    if not MIN_DURATION_NS <= nanoseconds <= MAX_DURATION_NS:
        raise _Invalid(f"{label} must be between 1ms and 24h.")
    return nanoseconds


def parse_int(text: str, label: str) -> int:
    """Parse a plain decimal integer."""
    value = text.strip()
    if re.fullmatch(r"-?[0-9]{1,18}", value) is None:
        raise _Invalid(f"{label} must be a whole number.")
    return int(value)


def parse_port_range(text: str, label: str) -> tuple[int, int]:
    """Parse a port or an inclusive range such as 8000-8010."""
    match = _PORT_RANGE.fullmatch(text.strip())
    if match is None:
        raise _Invalid(f"{label} must be a port from 1 to 65535 or a range such as 8000-8010.")
    low = int(match[1])
    high = int(match[2]) if match[2] else low
    if not 1 <= low <= high <= 65535:
        raise _Invalid(f"{label} must be a port from 1 to 65535 or a range such as 8000-8010.")
    return low, high


def format_port_range(low: int, high: int) -> str:
    """Return a port or range in the form the model stores."""
    return str(low) if low == high else f"{low}-{high}"


def format_memory(value: int) -> str:
    """Return bytes as the shortest exact form that parse_memory reads back."""
    if value <= 0:
        return str(value)
    for exponent, letter in ((4, "t"), (3, "g"), (2, "m"), (1, "k")):
        if value % 1024**exponent == 0:
            return f"{value // 1024**exponent}{letter}"
    return str(value)


def format_duration(nanoseconds: int) -> str:
    """Return nanoseconds as a duration that parse_duration reads back."""
    if nanoseconds <= 0:
        return "0s"
    remaining = nanoseconds
    parts: list[str] = []
    for unit, size in (("h", 3600 * 10**9), ("m", 60 * 10**9), ("s", 10**9), ("ms", NS_PER_MS)):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts) or f"{nanoseconds // NS_PER_MS}ms"


def _absolute_path(value: str, label: str) -> str:
    if (
        not value
        or len(value) > 4096
        or "\0" in value
        or not value.startswith("/")
        or value.startswith("//")
        or posixpath.normpath(value) != value
    ):
        raise _Invalid(f"{label} must be an absolute, normalised path.")
    return value


def _ip_literal(value: str, label: str) -> str:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        raise _Invalid(f"{label} must be an IP address.") from None


def _hostname(value: str, label: str) -> str:
    text = value.strip()
    if not text or len(text) > 253 or not all(DNS_LABEL.fullmatch(p) for p in text.split(".")):
        raise _Invalid(f"{label} must be a host name.")
    return text


def _split_list(text: str) -> tuple[str, ...]:
    return tuple(part for part in _LIST_SPLIT.split(text.strip()) if part)


def _capability(name: str, label: str) -> str:
    folded = name.strip().upper()
    folded = folded.removeprefix("CAP_")
    if folded != "ALL" and folded not in CAPABILITIES:
        raise _Invalid(f"{label} must be Linux capability names such as NET_ADMIN, or ALL.")
    return folded


def _cap_group(group: Group, count: int) -> None:
    if count > ROW_CAPS[group]:
        raise _Invalid(f"At most {ROW_CAPS[group]} {GROUP_NOUNS[group]} are allowed.")


class _Row(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # The form row this value came from, so errors and findings can name the row the user typed.
    slot: int | None = Field(default=None, exclude=True, repr=False)


class EnvVar(_Row):
    """One environment variable."""

    key: str
    value: str = ""

    @field_validator("key")
    @classmethod
    def _key(cls, key: str) -> str:
        if not key:
            raise _Invalid("Variable name is required.")
        if len(key) > 255 or "=" in key or "\0" in key:
            raise _Invalid("Variable name must be at most 255 characters without = or NUL.")
        return key

    @field_validator("value")
    @classmethod
    def _value(cls, value: str) -> str:
        if "\0" in value:
            raise _Invalid("Variable value must not contain NUL.")
        return value


class LabelRow(_Row):
    """One label."""

    key: str
    value: str = ""

    @field_validator("key")
    @classmethod
    def _key(cls, key: str) -> str:
        if not key.strip():
            raise _Invalid("Label key is required.")
        if key.strip().casefold() in _RESERVED_FOLDED:
            raise _Invalid("This label is set by the product and cannot be supplied.")
        if len(key) > 255 or any(unicodedata.category(c) == "Cc" for c in key):
            raise _Invalid("Label key must be at most 255 characters without control characters.")
        return key


class PortMapping(_Row):
    """One published port or equal-length range."""

    host_ip: str = ""
    container_port: str
    host_port: str = ""
    protocol: str = "tcp"

    @field_validator("host_ip")
    @classmethod
    def _host_ip(cls, value: str) -> str:
        return _ip_literal(value, "Host IP") if value.strip() else ""

    @field_validator("container_port")
    @classmethod
    def _container_port(cls, value: str) -> str:
        return format_port_range(*parse_port_range(value, "Container port"))

    @field_validator("host_port")
    @classmethod
    def _host_port(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            return ""
        low, high = parse_port_range(value, "Host port")
        container = info.data.get("container_port")
        if isinstance(container, str):
            c_low, c_high = parse_port_range(container, "Container port")
            if c_high - c_low != high - low:
                raise _Invalid("Host port and container port ranges must cover the same count.")
        return format_port_range(low, high)

    @field_validator("protocol")
    @classmethod
    def _protocol(cls, value: str) -> str:
        if value not in ("tcp", "udp", "sctp"):
            raise _Invalid("Protocol must be tcp, udp or sctp.")
        return value

    def container_ports(self) -> range:
        """Return the container ports this mapping covers."""
        low, high = parse_port_range(self.container_port, "Container port")
        return range(low, high + 1)

    def host_ports(self) -> range | None:
        """Return the host ports this mapping covers, None when Docker assigns them."""
        if not self.host_port:
            return None
        low, high = parse_port_range(self.host_port, "Host port")
        return range(low, high + 1)


class MountSpec(_Row):
    """One mount: a named or anonymous volume, a host bind, or a tmpfs."""

    type: str = "volume"
    source: str = Field(default="", validate_default=True)
    target: str
    read_only: bool = False
    tmpfs_size: int | None = None

    @field_validator("type")
    @classmethod
    def _type(cls, value: str) -> str:
        if value not in ("volume", "bind", "tmpfs"):
            raise _Invalid("Mount type must be volume, bind or tmpfs.")
        return value

    @field_validator("source")
    @classmethod
    def _source(cls, value: str, info: ValidationInfo) -> str:
        kind = info.data.get("type")
        if kind == "tmpfs":
            if value:
                raise _Invalid("A tmpfs mount takes no source.")
        elif kind == "bind":
            _absolute_path(value, "Bind source")
        elif kind == "volume" and value and RESOURCE_NAME.fullmatch(value) is None:
            raise _Invalid("Volume source must be a volume name, or empty for an anonymous volume.")
        return value

    @field_validator("target")
    @classmethod
    def _target(cls, value: str) -> str:
        _absolute_path(value, "Mount target")
        if value == "/":
            raise _Invalid("Mount target must not be /.")
        return value

    @field_validator("tmpfs_size")
    @classmethod
    def _tmpfs_size(cls, value: int | None, info: ValidationInfo) -> int | None:
        if value is None:
            return None
        if info.data.get("type") != "tmpfs":
            raise _Invalid("Tmpfs size applies only to a tmpfs mount.")
        if value < 1:
            raise _Invalid("Tmpfs size must be greater than zero.")
        return value


class NetworkAttachment(_Row):
    """One network the container joins."""

    name: str
    aliases: tuple[str, ...] = ()
    ipv4: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if len(value) > 128 or RESOURCE_NAME.fullmatch(value) is None:
            raise _Invalid(
                "Network name must use letters, digits, underscores, periods and hyphens."
            )
        return value

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 16:
            raise _Invalid("At most 16 aliases are allowed per network.")
        for alias in value:
            if DNS_LABEL.fullmatch(alias) is None:
                raise _Invalid("Aliases must be DNS labels of letters, digits and hyphens.")
        return value

    @field_validator("ipv4")
    @classmethod
    def _ipv4(cls, value: str) -> str:
        if not value.strip():
            return ""
        try:
            return str(ipaddress.IPv4Address(value.strip()))
        except ValueError:
            raise _Invalid("IPv4 address must be an IPv4 address.") from None


class DeviceMapping(_Row):
    """One host device passed through to the container."""

    host_path: str
    container_path: str = Field(default="", validate_default=True)
    permissions: str = "rwm"

    @field_validator("host_path")
    @classmethod
    def _host_path(cls, value: str) -> str:
        return _absolute_path(value, "Host device path")

    @field_validator("container_path")
    @classmethod
    def _container_path(cls, value: str, info: ValidationInfo) -> str:
        if not value:
            return str(info.data.get("host_path", ""))
        return _absolute_path(value, "Container device path")

    @field_validator("permissions")
    @classmethod
    def _permissions(cls, value: str) -> str:
        if not value or not set(value) <= set("rwm") or len(set(value)) != len(value):
            raise _Invalid("Device permissions must be a combination of r, w and m.")
        return "".join(c for c in "rwm" if c in value)


class ExtraHost(_Row):
    """One extra hosts-file entry."""

    name: str
    address: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _hostname(value, "Host name")

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        text = value.strip()
        return text if text == "host-gateway" else _ip_literal(text, "Host address")


class LogOption(_Row):
    """One log driver option."""

    key: str
    value: str = ""

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if len(value) > 128 or _LOG_KEY.fullmatch(value) is None:
            raise _Invalid(
                "Log option key may use lower-case letters, digits, periods and hyphens."
            )
        return value

    @field_validator("value")
    @classmethod
    def _value(cls, value: str) -> str:
        if len(value) > 1024 or "\0" in value:
            raise _Invalid("Log option value must be at most 1024 characters without NUL.")
        return value


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HealthcheckSpec(_Section):
    """The health check override; image keeps the image's, none disables it."""

    mode: HealthcheckMode = "image"
    test: str = ""
    interval: int | None = None
    timeout: int | None = None
    start_period: int | None = None
    start_interval: int | None = None
    retries: int | None = None

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, value: Any) -> Any:
        if value not in ("image", "none", "command"):
            raise _Invalid("Health check mode must be image, none or command.")
        return value

    @field_validator("test")
    @classmethod
    def _test(cls, value: str) -> str:
        if len(value) > MAX_COMMAND_CHARS or "\0" in value:
            raise _Invalid(f"Health check command must be at most {MAX_COMMAND_CHARS} characters.")
        return value

    @field_validator("interval", "timeout", "start_period", "start_interval")
    @classmethod
    def _duration(cls, value: int | None, info: ValidationInfo) -> int | None:
        if value is None:
            return None
        label = (info.field_name or "duration").replace("_", " ").capitalize()
        return _duration_in_range(value, f"Health check {label.lower()}")

    @field_validator("retries")
    @classmethod
    def _retries(cls, value: int | None) -> int | None:
        if value is not None and not 0 <= value <= 100:
            raise _Invalid("Health check retries must be from 0 to 100.")
        return value


class DnsSpec(_Section):
    """Resolver settings."""

    servers: tuple[str, ...] = ()
    search: tuple[str, ...] = ()
    options: tuple[str, ...] = ()

    @field_validator("servers")
    @classmethod
    def _servers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_ip_literal(item, "DNS servers") for item in value)

    @field_validator("search")
    @classmethod
    def _search(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_hostname(item, "DNS search domains") for item in value)

    @field_validator("options")
    @classmethod
    def _options(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not all(_DNS_OPTION.fullmatch(item) for item in value):
            raise _Invalid("DNS options may use letters, digits, colons, periods and hyphens.")
        return value


class LogSpec(_Section):
    """Log driver and its options."""

    driver: str | None = None
    options: tuple[LogOption, ...] = ()

    @field_validator("driver")
    @classmethod
    def _driver(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if _LOG_DRIVER.fullmatch(value.strip()) is None:
            raise _Invalid(
                "Log driver may use lower-case letters, digits, underscores and hyphens."
            )
        return value.strip()

    @field_validator("options")
    @classmethod
    def _options(cls, value: tuple[LogOption, ...]) -> tuple[LogOption, ...]:
        _cap_group("log_options", len(value))
        return value


class ContainerSpec(BaseModel):
    """Everything the create form can set, in the shape the Engine create body is built from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    image: str
    name: str | None = None
    command: tuple[str, ...] | None = None
    entrypoint: tuple[str, ...] | None = None
    working_dir: str | None = None
    user: str | None = None
    env: tuple[EnvVar, ...] = ()
    labels: tuple[LabelRow, ...] = ()
    ports: tuple[PortMapping, ...] = ()
    publish_all: bool = False
    mounts: tuple[MountSpec, ...] = ()
    network_mode: NetworkMode = "bridge"
    networks: tuple[NetworkAttachment, ...] = ()
    restart_policy: str | None = None
    restart_max_retries: int | None = None
    cpus: Decimal | None = None
    cpu_shares: int | None = None
    memory: int | None = None
    memory_swap: int | None = None
    pids_limit: int | None = None
    shm_size: int | None = None
    cap_add: tuple[str, ...] = ()
    cap_drop: tuple[str, ...] = ()
    devices: tuple[DeviceMapping, ...] = ()
    dns: DnsSpec = Field(default_factory=DnsSpec)
    extra_hosts: tuple[ExtraHost, ...] = ()
    healthcheck: HealthcheckSpec = Field(default_factory=HealthcheckSpec)
    log: LogSpec = Field(default_factory=LogSpec)
    runtime: str | None = None
    init: bool = False
    privileged: bool = False
    pid_host: bool = False
    ipc_host: bool = False
    pull: PullPolicy = "missing"
    start: bool = True

    @field_validator("image")
    @classmethod
    def _image(cls, value: str) -> str:
        return parse_image(value)

    @field_validator("pull", mode="before")
    @classmethod
    def _pull(cls, value: Any) -> Any:
        if value not in ("missing", "always", "never"):
            raise _Invalid("Pull must be missing, always or never.")
        return value

    @field_validator("network_mode", mode="before")
    @classmethod
    def _network_mode(cls, value: Any) -> Any:
        if value not in ("bridge", "none", "host", "networks"):
            raise _Invalid("Network mode must be bridge, none, host or networks.")
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else parse_container_name(value)

    @field_validator("command", "entrypoint")
    @classmethod
    def _argv(cls, value: tuple[str, ...] | None, info: ValidationInfo) -> tuple[str, ...] | None:
        if not value:
            return None
        label = "Command" if info.field_name == "command" else "Entrypoint"
        if any("\0" in part for part in value) or len(shlex.join(value)) > MAX_COMMAND_CHARS:
            raise _Invalid(f"{label} must be at most {MAX_COMMAND_CHARS} characters without NUL.")
        return value

    @field_validator("working_dir")
    @classmethod
    def _working_dir(cls, value: str | None) -> str | None:
        return None if not value else _absolute_path(value, "Working directory")

    @field_validator("user")
    @classmethod
    def _user(cls, value: str | None) -> str | None:
        if not value:
            return None
        if len(value) > 256 or USER_PATTERN.fullmatch(value) is None:
            raise _Invalid("User must be a name or id, optionally followed by :group.")
        return value

    @field_validator("env")
    @classmethod
    def _env(cls, value: tuple[EnvVar, ...]) -> tuple[EnvVar, ...]:
        _cap_group("env", len(value))
        return value

    @field_validator("labels")
    @classmethod
    def _labels(cls, value: tuple[LabelRow, ...]) -> tuple[LabelRow, ...]:
        _cap_group("labels", len(value))
        return value

    @field_validator("ports")
    @classmethod
    def _ports(cls, value: tuple[PortMapping, ...]) -> tuple[PortMapping, ...]:
        _cap_group("ports", len(value))
        return value

    @field_validator("mounts")
    @classmethod
    def _mounts(cls, value: tuple[MountSpec, ...]) -> tuple[MountSpec, ...]:
        _cap_group("mounts", len(value))
        return value

    @field_validator("networks")
    @classmethod
    def _networks(cls, value: tuple[NetworkAttachment, ...]) -> tuple[NetworkAttachment, ...]:
        _cap_group("networks", len(value))
        return value

    @field_validator("devices")
    @classmethod
    def _devices(cls, value: tuple[DeviceMapping, ...]) -> tuple[DeviceMapping, ...]:
        _cap_group("devices", len(value))
        return value

    @field_validator("extra_hosts")
    @classmethod
    def _extra_hosts(cls, value: tuple[ExtraHost, ...]) -> tuple[ExtraHost, ...]:
        _cap_group("extra_hosts", len(value))
        return value

    @field_validator("restart_policy")
    @classmethod
    def _restart_policy(cls, value: str | None) -> str | None:
        if not value:
            return None
        if value not in _RESTART_POLICIES:
            raise _Invalid("Restart policy must be no, always, unless-stopped or on-failure.")
        return value

    @field_validator("restart_max_retries")
    @classmethod
    def _restart_max_retries(cls, value: int | None) -> int | None:
        if value is not None and not 0 <= value <= 1000:
            raise _Invalid("Maximum restart retries must be from 0 to 1000.")
        return value

    @field_validator("cpus")
    @classmethod
    def _cpus(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        if not value.is_finite() or value <= 0 or value > MAX_CPUS:
            raise _Invalid("CPUs must be greater than 0 and at most 100000.")
        if value != value.quantize(Decimal("0.01")):
            raise _Invalid("CPUs must have at most 2 decimal places.")
        return value

    @field_validator("cpu_shares")
    @classmethod
    def _cpu_shares(cls, value: int | None) -> int | None:
        if value is not None and not 2 <= value <= MAX_INT32:
            raise _Invalid("CPU shares must be at least 2.")
        return value

    @field_validator("memory")
    @classmethod
    def _memory(cls, value: int | None) -> int | None:
        if value is not None and not MIN_MEMORY_BYTES <= value <= MAX_BYTES:
            raise _Invalid("Memory must be at least 6 MiB.")
        return value

    @field_validator("memory_swap")
    @classmethod
    def _memory_swap(cls, value: int | None) -> int | None:
        if value is not None and value != -1 and not 1 <= value <= MAX_BYTES:
            raise _Invalid("Swap must be -1 or at least the memory limit.")
        return value

    @field_validator("pids_limit")
    @classmethod
    def _pids_limit(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= MAX_INT32:
            raise _Invalid("PIDs limit must be a positive whole number.")
        return value

    @field_validator("shm_size")
    @classmethod
    def _shm_size(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= MAX_BYTES:
            raise _Invalid("Shared memory size must be greater than zero.")
        return value

    @field_validator("cap_add", "cap_drop")
    @classmethod
    def _caps(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        label = "Added capabilities" if info.field_name == "cap_add" else "Dropped capabilities"
        return tuple(dict.fromkeys(_capability(name, label) for name in value))

    @field_validator("runtime")
    @classmethod
    def _runtime(cls, value: str | None) -> str | None:
        if not value:
            return None
        if _RUNTIME.fullmatch(value) is None:
            raise _Invalid("Runtime may use letters, digits, underscores, periods and hyphens.")
        return value

    @model_validator(mode="after")
    def _cross_checks(self) -> Self:
        errors = cross_errors(self)
        if errors:
            raise ValidationFailed(errors)
        return self


_Add = Callable[[str, str], None]


def _slot(rows: Sequence[_Row], ordinal: int) -> int:
    slot = rows[ordinal].slot
    return ordinal if slot is None else slot


def cross_errors(spec: ContainerSpec) -> dict[str, str]:
    """Return the conflicts among otherwise valid fields, keyed by form field name."""
    errors: dict[str, str] = {}

    def add(name: str, message: str) -> None:
        errors.setdefault(name, message)

    _env_errors(spec, add)
    _label_errors(spec, add)
    _port_errors(spec, add)
    _mount_errors(spec, add)
    _network_errors(spec, add)
    _resource_errors(spec, add)
    if spec.restart_max_retries is not None and spec.restart_policy != "on-failure":
        add("restart_max_retries", "Maximum retries apply only to the on-failure restart policy.")
    _healthcheck_errors(spec, add)
    _log_errors(spec, add)
    return errors


def _env_errors(spec: ContainerSpec, add: _Add) -> None:
    seen: set[str] = set()
    for ordinal, row in enumerate(spec.env):
        if row.key in seen:
            add(f"env-{_slot(spec.env, ordinal)}-key", "Variable names must be unique.")
        seen.add(row.key)
    if sum(len(row.key) + len(row.value) for row in spec.env) > MAX_ENV_CHARS:
        add("env", "Environment variables must total at most 64 KiB.")


def _label_errors(spec: ContainerSpec, add: _Add) -> None:
    seen: set[str] = set()
    for ordinal, row in enumerate(spec.labels):
        if row.key in seen:
            add(f"label-{_slot(spec.labels, ordinal)}-key", "Label keys must be unique.")
        seen.add(row.key)
    if sum(len(row.key) + len(row.value) for row in spec.labels) > MAX_LABEL_CHARS:
        add("labels", "Labels must total at most 64 KiB.")


def _port_errors(spec: ContainerSpec, add: _Add) -> None:
    published: set[tuple[str, int, str]] = set()
    bindings: set[tuple[str, int | None, int, str]] = set()
    expanded = 0
    for ordinal, row in enumerate(spec.ports):
        slot = _slot(spec.ports, ordinal)
        ip = "0.0.0.0" if row.host_ip in ("", "0.0.0.0") else row.host_ip
        host_ports = row.host_ports()
        for index, container_port in enumerate(row.container_ports()):
            expanded += 1
            host_port = None if host_ports is None else host_ports[index]
            if host_port is not None:
                key = (ip, host_port, row.protocol)
                if key in published:
                    add(
                        f"port-{slot}-host_port", "This host IP, port and protocol is mapped twice."
                    )
                published.add(key)
            full = (ip, host_port, container_port, row.protocol)
            if full in bindings:
                add(
                    f"port-{slot}-container_port",
                    "This container port is mapped twice to the same host binding.",
                )
            bindings.add(full)
    if expanded > MAX_EXPANDED_PORTS:
        add("ports", f"Port mappings may cover at most {MAX_EXPANDED_PORTS} ports in total.")
    if spec.network_mode in ("host", "none"):
        if spec.ports:
            add("ports", "Ports cannot be published in host or none network mode.")
        if spec.publish_all:
            add("publish_all", "Ports cannot be published in host or none network mode.")


def _mount_errors(spec: ContainerSpec, add: _Add) -> None:
    seen: set[str] = set()
    for ordinal, row in enumerate(spec.mounts):
        if row.target in seen:
            add(f"mount-{_slot(spec.mounts, ordinal)}-target", "Mount targets must be unique.")
        seen.add(row.target)


def _network_errors(spec: ContainerSpec, add: _Add) -> None:
    if spec.network_mode == "networks":
        if not spec.networks:
            add("networks", "The networks mode needs at least one network.")
    elif spec.networks:
        add("networks", "Networks can be listed only in the networks mode.")
    seen: set[str] = set()
    for ordinal, row in enumerate(spec.networks):
        if row.name in seen:
            add(f"network-{_slot(spec.networks, ordinal)}-name", "Network names must be unique.")
        seen.add(row.name)


def _resource_errors(spec: ContainerSpec, add: _Add) -> None:
    if spec.memory_swap is not None:
        if spec.memory is None:
            add("memory_swap", "Swap needs a memory limit.")
        elif spec.memory_swap != -1 and spec.memory_swap < spec.memory:
            add("memory_swap", "Swap must be -1 or at least the memory limit.")
    overlap = set(spec.cap_add) & set(spec.cap_drop)
    if overlap:
        add("cap_add", "A capability cannot be both added and dropped.")
        add("cap_drop", "A capability cannot be both added and dropped.")


def _healthcheck_errors(spec: ContainerSpec, add: _Add) -> None:
    check = spec.healthcheck
    if check.mode == "command":
        if not check.test.strip():
            add("healthcheck_test", "A health check command is required in command mode.")
        return
    for name in ("test", "interval", "timeout", "start_period", "start_interval", "retries"):
        if getattr(check, name) not in (None, ""):
            add(f"healthcheck_{name}", "This setting applies only in command mode.")


def _log_errors(spec: ContainerSpec, add: _Add) -> None:
    seen: set[str] = set()
    for ordinal, row in enumerate(spec.log.options):
        if row.key in seen:
            add(f"logopt-{_slot(spec.log.options, ordinal)}-key", "Log option keys must be unique.")
        seen.add(row.key)
    if spec.log.options and not spec.log.driver:
        add("log_driver", "A log driver is required when log options are set.")


def row_slots(spec: ContainerSpec) -> dict[str, tuple[int, ...]]:
    """Return the form row index of each element of the groups find_restricted reports by row."""

    def slots(rows: Sequence[_Row]) -> tuple[int, ...]:
        return tuple(_slot(rows, i) for i in range(len(rows)))

    return {
        "mounts": slots(spec.mounts),
        "networks": slots(spec.networks),
        "devices": slots(spec.devices),
        "log_options": slots(spec.log.options),
    }


def to_engine_body(
    spec: ContainerSpec, *, network_ids: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Build the Engine create body; the name goes in the query string, never in the body.

    network_ids maps a network name to the id its endpoint is keyed by; an unmapped name is used
    as it stands. Bind mounts are expressed as Mounts entries and Binds is never emitted.
    """
    body: dict[str, Any] = {"Image": spec.image}
    host: dict[str, Any] = {}
    body["HostConfig"] = host
    if spec.command:
        body["Cmd"] = list(spec.command)
    if spec.entrypoint:
        body["Entrypoint"] = list(spec.entrypoint)
    if spec.working_dir:
        body["WorkingDir"] = spec.working_dir
    if spec.user:
        body["User"] = spec.user
    if spec.env:
        body["Env"] = [f"{row.key}={row.value}" for row in spec.env]
    if spec.labels:
        labels = SubmittedLabels(values={row.key: row.value for row in spec.labels})
        body["Labels"] = dict(labels.values)
    _add_ports(spec, body, host)
    if spec.mounts:
        host["Mounts"] = [_mount_body(row) for row in spec.mounts]
    _add_network(spec, body, host, network_ids or {})
    if spec.restart_policy:
        policy: dict[str, Any] = {"Name": spec.restart_policy}
        if spec.restart_policy == "on-failure" and spec.restart_max_retries is not None:
            policy["MaximumRetryCount"] = spec.restart_max_retries
        host["RestartPolicy"] = policy
    _add_resources(spec, host)
    if spec.cap_add:
        host["CapAdd"] = list(spec.cap_add)
    if spec.cap_drop:
        host["CapDrop"] = list(spec.cap_drop)
    if spec.devices:
        host["Devices"] = [
            {
                "PathOnHost": row.host_path,
                "PathInContainer": row.container_path,
                "CgroupPermissions": row.permissions,
            }
            for row in spec.devices
        ]
    _add_dns(spec, host)
    _add_healthcheck(spec, body)
    if spec.log.driver:
        host["LogConfig"] = {
            "Type": spec.log.driver,
            "Config": {row.key: row.value for row in spec.log.options},
        }
    if spec.runtime:
        host["Runtime"] = spec.runtime
    if spec.init:
        host["Init"] = True
    if spec.privileged:
        host["Privileged"] = True
    if spec.pid_host:
        host["PidMode"] = "host"
    if spec.ipc_host:
        host["IpcMode"] = "host"
    return body


def _mount_body(row: MountSpec) -> dict[str, Any]:
    mount: dict[str, Any] = {"Type": row.type, "Target": row.target}
    if row.source:
        mount["Source"] = row.source
    if row.read_only:
        mount["ReadOnly"] = True
    if row.type == "tmpfs" and row.tmpfs_size is not None:
        mount["TmpfsOptions"] = {"SizeBytes": row.tmpfs_size}
    return mount


def _add_ports(spec: ContainerSpec, body: dict[str, Any], host: dict[str, Any]) -> None:
    exposed: dict[str, dict[str, Any]] = {}
    bindings: dict[str, list[dict[str, str]]] = {}
    for row in spec.ports:
        host_ports = row.host_ports()
        for index, container_port in enumerate(row.container_ports()):
            key = f"{container_port}/{row.protocol}"
            exposed[key] = {}
            host_port = "" if host_ports is None else str(host_ports[index])
            bindings.setdefault(key, []).append({"HostIp": row.host_ip, "HostPort": host_port})
    if exposed:
        body["ExposedPorts"] = exposed
        host["PortBindings"] = bindings
    if spec.publish_all:
        host["PublishAllPorts"] = True


def _add_network(
    spec: ContainerSpec,
    body: dict[str, Any],
    host: dict[str, Any],
    network_ids: Mapping[str, str],
) -> None:
    if spec.network_mode != "networks":
        host["NetworkMode"] = spec.network_mode
        return
    first = spec.networks[0].name
    host["NetworkMode"] = network_ids.get(first, first)
    endpoints: dict[str, dict[str, Any]] = {}
    for row in spec.networks:
        endpoint: dict[str, Any] = {}
        if row.aliases:
            endpoint["Aliases"] = list(row.aliases)
        if row.ipv4:
            endpoint["IPAMConfig"] = {"IPv4Address": row.ipv4}
        endpoints[network_ids.get(row.name, row.name)] = endpoint
    body["NetworkingConfig"] = {"EndpointsConfig": endpoints}


def _add_resources(spec: ContainerSpec, host: dict[str, Any]) -> None:
    if spec.cpus is not None:
        host["NanoCpus"] = int(spec.cpus * 1_000_000_000)
    if spec.cpu_shares is not None:
        host["CpuShares"] = spec.cpu_shares
    if spec.memory is not None:
        host["Memory"] = spec.memory
    if spec.memory_swap is not None:
        host["MemorySwap"] = spec.memory_swap
    if spec.pids_limit is not None:
        host["PidsLimit"] = spec.pids_limit
    if spec.shm_size is not None:
        host["ShmSize"] = spec.shm_size


def _add_dns(spec: ContainerSpec, host: dict[str, Any]) -> None:
    if spec.dns.servers:
        host["Dns"] = list(spec.dns.servers)
    if spec.dns.search:
        host["DnsSearch"] = list(spec.dns.search)
    if spec.dns.options:
        host["DnsOptions"] = list(spec.dns.options)
    if spec.extra_hosts:
        host["ExtraHosts"] = [f"{row.name}:{row.address}" for row in spec.extra_hosts]


def _add_healthcheck(spec: ContainerSpec, body: dict[str, Any]) -> None:
    check = spec.healthcheck
    if check.mode == "none":
        body["Healthcheck"] = {"Test": ["NONE"]}
    elif check.mode == "command":
        health: dict[str, Any] = {"Test": ["CMD-SHELL", check.test.strip()]}
        for name, key in (
            ("interval", "Interval"),
            ("timeout", "Timeout"),
            ("start_period", "StartPeriod"),
            ("start_interval", "StartInterval"),
            ("retries", "Retries"),
        ):
            value = getattr(check, name)
            if value is not None:
                health[key] = value
        body["Healthcheck"] = health


@dataclass(frozen=True, slots=True)
class FormState:
    """Every submitted value of the create form, with the errors found and the rows to render.

    values is keyed by form field name; a ticked checkbox is "on", an unticked one "". errors is
    keyed by form field name, or by group name for a group-level error. rows is, per group, the
    index after the last row holding a value or an error; slots is how many row slots to render.
    """

    values: Mapping[str, str] = field(default_factory=dict)
    errors: Mapping[str, str] = field(default_factory=dict)
    rows: Mapping[str, int] = field(default_factory=dict)
    slots: Mapping[str, int] = field(default_factory=dict)

    def get(self, name: str, default: str = "") -> str:
        """Return the submitted value of a field."""
        return self.values.get(name, default)

    def checked(self, name: str) -> bool:
        """Report whether a checkbox field was ticked."""
        return bool(self.values.get(name))

    def row_indices(self, group: str) -> range:
        """Return the row indices to render for a group."""
        return range(self.slots.get(group, SPARE_ROWS[group]))

    def add_rows(self, group: str) -> "FormState":
        """Return the state with more blank rows in one group, up to its cap."""
        slots = dict(self.slots)
        current = slots.get(group, SPARE_ROWS[group])
        slots[group] = min(ROW_CAPS[group], current + SPARE_ROWS[group])
        return FormState(self.values, self.errors, self.rows, slots)

    def with_errors(self, errors: Mapping[str, str]) -> "FormState":
        """Return the state carrying these errors, with the rows they sit in kept visible."""
        values = self.values
        rows = dict(self.rows)
        slots = dict(self.slots)
        for name in errors:
            located = locate_row_field(name)
            if located is not None:
                group, index = located
                rows[group] = max(rows.get(group, 0), index + 1)
                slots[group] = max(slots.get(group, 0), rows[group])
        return FormState(values, dict(errors), rows, slots)

    def without_errors(self) -> "FormState":
        """Return the state with its errors cleared."""
        return FormState(self.values, {}, self.rows, self.slots)

    def hidden_fields(self) -> list[tuple[str, str]]:
        """Return the non-empty values as name and value pairs, for carrying across a review."""
        return [(name, value) for name, value in self.values.items() if value]

    @classmethod
    def from_form(cls, form: Mapping[str, Any]) -> "FormState":
        """Collect the known fields of a submitted form without validating them."""
        values: dict[str, str] = {}
        seen: dict[str, set[int]] = {group: set() for group in GROUPS}
        filled: dict[str, set[int]] = {group: set() for group in GROUPS}
        for name, raw in form.items():
            text = raw if isinstance(raw, str) else ""
            if name in SCALAR_FIELDS:
                values[name] = (
                    ("on" if _is_checked(text) else "") if name in CHECKBOX_FIELDS else text
                )
                continue
            match = _ROW_KEY.fullmatch(name)
            if match is None:
                continue
            group = _PREFIX_GROUP[match[1]]
            column = match[3]
            if column not in ROW_FIELDS[group]:
                continue
            index = int(match[2])
            checkbox = column in ROW_CHECKBOXES.get(group, ())
            values[name] = ("on" if _is_checked(text) else "") if checkbox else text
            seen[group].add(index)
        for group in GROUPS:
            for index in seen[group]:
                if not _row_blank(values, group, index):
                    filled[group].add(index)
        rows: dict[str, int] = {
            group: (max(filled[group]) + 1 if filled[group] else 0) for group in GROUPS
        }
        slots: dict[str, int] = {
            group: min(
                ROW_CAPS[group],
                max(rows[group] + SPARE_ROWS[group], max(seen[group], default=-1) + 1),
            )
            for group in GROUPS
        }
        return cls(values, {}, rows, slots)

    @classmethod
    def from_spec(cls, spec: ContainerSpec) -> "FormState":
        """Build the state a form renders to show an existing spec."""
        values: dict[str, str] = {}
        _put(values, "image", spec.image)
        _put(values, "name", spec.name or "")
        _put(values, "pull", spec.pull)
        _put(values, "command", shlex.join(spec.command) if spec.command else "")
        _put(values, "entrypoint", shlex.join(spec.entrypoint) if spec.entrypoint else "")
        _put(values, "working_dir", spec.working_dir or "")
        _put(values, "user", spec.user or "")
        _put(values, "publish_all", "on" if spec.publish_all else "")
        _put(values, "network_mode", spec.network_mode)
        _put(values, "restart_policy", spec.restart_policy or "")
        _put(values, "restart_max_retries", _text(spec.restart_max_retries))
        _put(values, "cpus", "" if spec.cpus is None else format(spec.cpus.normalize(), "f"))
        _put(values, "cpu_shares", _text(spec.cpu_shares))
        _put(values, "memory", _memory_text(spec.memory))
        _put(values, "memory_swap", _memory_text(spec.memory_swap))
        _put(values, "pids_limit", _text(spec.pids_limit))
        _put(values, "shm_size", _memory_text(spec.shm_size))
        _put(values, "cap_add", " ".join(spec.cap_add))
        _put(values, "cap_drop", " ".join(spec.cap_drop))
        _put(values, "dns_servers", " ".join(spec.dns.servers))
        _put(values, "dns_search", " ".join(spec.dns.search))
        _put(values, "dns_options", " ".join(spec.dns.options))
        check = spec.healthcheck
        _put(values, "healthcheck_mode", check.mode)
        _put(values, "healthcheck_test", check.test)
        _put(values, "healthcheck_interval", _duration_text(check.interval))
        _put(values, "healthcheck_timeout", _duration_text(check.timeout))
        _put(values, "healthcheck_start_period", _duration_text(check.start_period))
        _put(values, "healthcheck_start_interval", _duration_text(check.start_interval))
        _put(values, "healthcheck_retries", _text(check.retries))
        _put(values, "log_driver", spec.log.driver or "")
        _put(values, "runtime", spec.runtime or "")
        for name in ("init", "privileged", "pid_host", "ipc_host", "start"):
            _put(values, name, "on" if getattr(spec, name) else "")
        for group, rows in (
            ("env", spec.env),
            ("labels", spec.labels),
            ("ports", spec.ports),
            ("mounts", spec.mounts),
            ("networks", spec.networks),
            ("devices", spec.devices),
            ("extra_hosts", spec.extra_hosts),
            ("log_options", spec.log.options),
        ):
            for index, row in enumerate(rows):
                for column in ROW_FIELDS[group]:
                    _put(values, f"{ROW_PREFIX[group]}-{index}-{column}", _row_text(row, column))
        counts: dict[str, int] = {
            "env": len(spec.env),
            "labels": len(spec.labels),
            "ports": len(spec.ports),
            "mounts": len(spec.mounts),
            "networks": len(spec.networks),
            "devices": len(spec.devices),
            "extra_hosts": len(spec.extra_hosts),
            "log_options": len(spec.log.options),
        }
        slots: dict[str, int] = {g: min(ROW_CAPS[g], counts[g] + SPARE_ROWS[g]) for g in GROUPS}
        return cls(values, {}, counts, slots)


def _put(values: dict[str, str], name: str, value: str) -> None:
    if value:
        values[name] = value


def _text(value: int | None) -> str:
    return "" if value is None else str(value)


def _memory_text(value: int | None) -> str:
    return "" if value is None else format_memory(value)


def _duration_text(value: int | None) -> str:
    return "" if value is None else format_duration(value)


def _row_text(row: Any, column: str) -> str:
    value = getattr(row, column)
    if column == "aliases":
        return " ".join(value)
    if column == "tmpfs_size":
        return _memory_text(value)
    if isinstance(value, bool):
        return "on" if value else ""
    return str(value)


def _is_checked(text: str) -> bool:
    return text.strip().casefold() in _TRUTHY


def _row_blank(values: Mapping[str, str], group: str, index: int) -> bool:
    prefix = ROW_PREFIX[group]
    skipped = ROW_DEFAULTED.get(group, ())
    for column in ROW_FIELDS[group]:
        if column in skipped:
            continue
        text = values.get(f"{prefix}-{index}-{column}", "")
        if text.strip():
            return False
    return True


def _row_indices(values: Mapping[str, str], prefix: str) -> set[int]:
    found: set[int] = set()
    for name in values:
        match = _ROW_KEY.fullmatch(name)
        if match is not None and match[1] == prefix:
            found.add(int(match[2]))
    return found


def locate_row_field(name: str) -> tuple[str, int] | None:
    """Return the group and row index a repeatable-row field name belongs to, else None."""
    match = _ROW_KEY.fullmatch(name)
    if match is None:
        return None
    return _PREFIX_GROUP[match[1]], int(match[2])


class SpecInvalid(ValidationFailed):
    """Raised by spec_from_form; state holds every submitted value and the errors found."""

    def __init__(self, state: FormState) -> None:
        super().__init__(dict(state.errors))
        self.state = state


@dataclass(frozen=True, slots=True)
class ParsedForm:
    """A parsed form: the spec when it was valid, and the state to render either way."""

    spec: ContainerSpec | None
    state: FormState


def spec_from_form(form: Mapping[str, Any]) -> ContainerSpec:
    """Return the spec a submitted form describes, or raise SpecInvalid carrying the form state."""
    parsed = parse_form(form)
    if parsed.spec is None:
        raise SpecInvalid(parsed.state)
    return parsed.spec


def parse_form(form: Mapping[str, Any]) -> ParsedForm:
    """Validate a submitted form without raising; the state always holds every value."""
    state = FormState.from_form(form)
    errors: dict[str, str] = {}
    kwargs, row_slots_by_group = _kwargs(state, errors)
    spec: ContainerSpec | None = None
    try:
        spec = ContainerSpec.model_validate(kwargs)
    except ValidationError as exc:
        for item in exc.errors():
            errors.setdefault(_form_name(item["loc"], row_slots_by_group), _message(item))
    except ValidationFailed as exc:
        for name, message in exc.fields.items():
            errors.setdefault(name, message)
    if errors:
        return ParsedForm(None, state.with_errors(errors))
    return ParsedForm(spec, state)


def _message(item: Any) -> str:
    error = (item.get("ctx") or {}).get("error")
    return str(error) if isinstance(error, _Invalid) else BLANK_MESSAGE


def _form_name(loc: Sequence[int | str], slots: Mapping[str, Sequence[int]]) -> str:
    head = str(loc[0])
    if head in ROW_PREFIX:
        if len(loc) >= 3 and isinstance(loc[1], int):
            rows = slots.get(head, ())
            index = rows[loc[1]] if loc[1] < len(rows) else loc[1]
            return f"{ROW_PREFIX[head]}-{index}-{loc[2]}"
        return head
    if head in ("healthcheck", "dns") and len(loc) >= 2:
        return f"{head}_{loc[1]}"
    if head == "log":
        if len(loc) >= 4 and loc[1] == "options" and isinstance(loc[2], int):
            rows = slots.get("log_options", ())
            index = rows[loc[2]] if loc[2] < len(rows) else loc[2]
            return f"logopt-{index}-{loc[3]}"
        return "log_driver" if len(loc) >= 2 and loc[1] == "driver" else "log_options"
    return head


def _kwargs(
    state: FormState, errors: dict[str, str]
) -> tuple[dict[str, Any], dict[str, tuple[int, ...]]]:
    values = state.values
    kwargs: dict[str, Any] = {}

    def text(name: str) -> str:
        return values.get(name, "").strip()

    def convert(name: str, parse: Any, *args: Any, **options: Any) -> Any:
        raw = text(name)
        if not raw:
            return None
        try:
            return parse(raw, *args, **options)
        except _Invalid as exc:
            errors.setdefault(name, str(exc))
            return None

    kwargs["image"] = text("image")
    kwargs["pull"] = text("pull") or "missing"
    kwargs["name"] = text("name") or None
    kwargs["command"] = convert("command", parse_argv, "Command")
    kwargs["entrypoint"] = convert("entrypoint", parse_argv, "Entrypoint")
    kwargs["working_dir"] = text("working_dir") or None
    kwargs["user"] = text("user") or None
    kwargs["publish_all"] = state.checked("publish_all")
    kwargs["network_mode"] = text("network_mode") or "bridge"
    kwargs["restart_policy"] = text("restart_policy") or None
    kwargs["restart_max_retries"] = convert("restart_max_retries", parse_int, "Maximum retries")
    cpus = text("cpus")
    if cpus and re.fullmatch(r"[0-9]+(\.[0-9]+)?", cpus):
        kwargs["cpus"] = Decimal(cpus)
    elif cpus:
        errors.setdefault("cpus", "CPUs must be a number such as 0.5 or 2.")
    kwargs["cpu_shares"] = convert("cpu_shares", parse_int, "CPU shares")
    kwargs["memory"] = convert("memory", parse_memory, "Memory")
    kwargs["memory_swap"] = convert("memory_swap", parse_memory, "Swap", allow_unlimited=True)
    kwargs["pids_limit"] = convert("pids_limit", parse_int, "PIDs limit")
    kwargs["shm_size"] = convert("shm_size", parse_memory, "Shared memory size")
    kwargs["cap_add"] = _split_list(values.get("cap_add", ""))
    kwargs["cap_drop"] = _split_list(values.get("cap_drop", ""))
    kwargs["dns"] = {
        "servers": _split_list(values.get("dns_servers", "")),
        "search": _split_list(values.get("dns_search", "")),
        "options": _split_list(values.get("dns_options", "")),
    }
    kwargs["healthcheck"] = {
        "mode": text("healthcheck_mode") or "image",
        "test": text("healthcheck_test"),
        "interval": convert("healthcheck_interval", parse_duration, "Health check interval"),
        "timeout": convert("healthcheck_timeout", parse_duration, "Health check timeout"),
        "start_period": convert(
            "healthcheck_start_period", parse_duration, "Health check start period"
        ),
        "start_interval": convert(
            "healthcheck_start_interval", parse_duration, "Health check start interval"
        ),
        "retries": convert("healthcheck_retries", parse_int, "Health check retries"),
    }
    row_slots_by_group: dict[str, tuple[int, ...]] = {}
    rows = {group: _rows(state, group, errors) for group in GROUPS}
    for group, (items, slots) in rows.items():
        row_slots_by_group[group] = slots
        if group != "log_options":
            kwargs[group] = items
    kwargs["log"] = {"driver": text("log_driver") or None, "options": rows["log_options"][0]}
    kwargs["runtime"] = text("runtime") or None
    for flag in ("init", "privileged", "pid_host", "ipc_host", "start"):
        kwargs[flag] = state.checked(flag)
    return kwargs, row_slots_by_group


def _rows(
    state: FormState, group: str, errors: dict[str, str]
) -> tuple[list[dict[str, Any]], tuple[int, ...]]:
    prefix = ROW_PREFIX[group]
    cap = ROW_CAPS[group]
    indices = sorted(
        index
        for index in _row_indices(state.values, prefix)
        if not _row_blank(state.values, group, index)
    )
    if len(indices) > cap or (indices and indices[-1] >= cap):
        errors.setdefault(group, f"At most {cap} {GROUP_NOUNS[group]} are allowed.")
        indices = [index for index in indices if index < cap][:cap]
    items: list[dict[str, Any]] = []
    for index in indices:
        raw = {c: state.values.get(f"{prefix}-{index}-{c}", "") for c in ROW_FIELDS[group]}
        items.append(_row_kwargs(group, index, raw, errors))
    return items, tuple(indices)


def _row_kwargs(
    group: str, index: int, raw: Mapping[str, str], errors: dict[str, str]
) -> dict[str, Any]:
    prefix = ROW_PREFIX[group]
    row: dict[str, Any] = {"slot": index}
    for column, value in raw.items():
        row[column] = value if column in _UNSTRIPPED.get(group, ()) else value.strip()
    if group == "mounts":
        row["read_only"] = _is_checked(raw["read_only"])
        size = row.pop("tmpfs_size")
        row["tmpfs_size"] = None
        if size:
            try:
                row["tmpfs_size"] = parse_memory(size, "Tmpfs size")
            except _Invalid as exc:
                errors.setdefault(f"{prefix}-{index}-tmpfs_size", str(exc))
        row["type"] = row["type"] or "volume"
    elif group == "networks":
        row["aliases"] = _split_list(raw["aliases"])
    elif group == "ports":
        row["protocol"] = row["protocol"] or "tcp"
    elif group == "devices":
        row["permissions"] = row["permissions"] or "rwm"
    return row
