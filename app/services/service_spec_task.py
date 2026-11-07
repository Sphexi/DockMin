"""Container-spec field groups of the Swarm service form."""

import copy
import ipaddress
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from app.services.container_restrictions import ALLOWED_LOG_DRIVERS, ALLOWED_LOG_OPTION_KEYS
from app.services.container_spec import (
    CAPABILITIES,
    DNS_LABEL,
    MAX_COMMAND_CHARS,
    USER_PATTERN,
    parse_argv,
    parse_duration,
    parse_int,
)

MAX_RETRIES: Final = 100
MAX_GROUPS: Final = 32
MAX_HOSTS: Final = 32
MAX_NAMESERVERS: Final = 8
MAX_SEARCH_DOMAINS: Final = 8
MAX_DNS_OPTIONS: Final = 8
MAX_CAPABILITIES: Final = 64
MAX_SYSCTLS: Final = 64
MAX_STOP_SIGNAL: Final = 64
MAX_LIMIT: Final = 2**63 - 1
MAX_VALUE_CHARS: Final = 1024

MANAGED_CONTAINER_KEYS: Final[tuple[str, ...]] = (
    "Healthcheck",
    "User",
    "Groups",
    "Dir",
    "Init",
    "StopSignal",
    "StopGracePeriod",
    "TTY",
    "OpenStdin",
    "Hostname",
    "Hosts",
    "DNSConfig",
    "ReadOnly",
    "CapabilityDrop",
    "CapabilityAdd",
    "Sysctls",
    "Ulimits",
)
MANAGED_TASK_KEYS: Final[tuple[str, ...]] = ("LogDriver",)
ULIMIT_NAMES: Final = frozenset(
    {
        "core",
        "cpu",
        "data",
        "fsize",
        "locks",
        "memlock",
        "msgqueue",
        "nice",
        "nofile",
        "nproc",
        "rss",
        "rtprio",
        "rttime",
        "sigpending",
        "stack",
    }
)
HEALTH_MODES: Final = ("", "none", "cmd", "shell")

_GROUP: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_SEARCH_DOMAIN: Final = re.compile(r"[A-Za-z0-9._-]{1,253}")
_DNS_OPTION: Final = re.compile(r"[A-Za-z0-9_:.-]{1,128}")
_SIGNAL_NAME: Final = re.compile(r"(?:SIG)?[A-Z][A-Z0-9+]{0,19}", re.IGNORECASE)
_SYSCTL_KEY: Final = re.compile(r"(?:net|kernel)\.[A-Za-z0-9_.-]{1,250}")
_LOG_DRIVER: Final = re.compile(r"[a-z0-9_-]{1,64}")
_LOG_KEY: Final = re.compile(r"[a-z0-9_.-]{1,64}")
_SPLIT: Final = re.compile(r"[\s,]+")
_TRUTHY: Final = frozenset({"on", "true", "1", "yes"})
_NUMERIC_HEALTH: Final = ("Interval", "Timeout", "Retries", "StartPeriod", "StartInterval")


@dataclass(frozen=True, slots=True)
class HealthInput:
    """Health check; mode `keep` leaves a stored test the form cannot show. Times in nanoseconds."""

    mode: str = ""
    command: tuple[str, ...] = ()
    interval: int | None = None
    timeout: int | None = None
    retries: int | None = None
    start_period: int | None = None
    start_interval: int | None = None


@dataclass(frozen=True, slots=True)
class ProcessInput:
    """How the container process runs; init None leaves the daemon default."""

    user: str = ""
    groups: tuple[str, ...] = ()
    working_dir: str = ""
    init: bool | None = None
    stop_signal: str = ""
    stop_grace_period: int | None = None
    tty: bool = False
    open_stdin: bool = False


@dataclass(frozen=True, slots=True)
class HostInput:
    """Host name, hosts-file entries in `address host` form, and resolver settings."""

    hostname: str = ""
    hosts: tuple[str, ...] = ()
    nameservers: tuple[str, ...] = ()
    search: tuple[str, ...] = ()
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SecurityInput:
    """Security settings; cap_add and sysctls None mean the form did not offer them."""

    read_only: bool = False
    no_new_privileges: bool = False
    cap_drop: tuple[str, ...] = ()
    cap_add: tuple[str, ...] | None = None
    sysctls: tuple[tuple[str, str], ...] | None = None


@dataclass(frozen=True, slots=True)
class UlimitRow:
    """One resource limit; -1 is unlimited."""

    slot: int
    name: str
    soft: int
    hard: int


@dataclass(frozen=True, slots=True)
class LogInput:
    """The task log driver and its options."""

    driver: str = ""
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class TaskInput:
    """The field groups of the container spec; log None means the form did not offer it."""

    health: HealthInput = field(default_factory=HealthInput)
    process: ProcessInput = field(default_factory=ProcessInput)
    host: HostInput = field(default_factory=HostInput)
    security: SecurityInput = field(default_factory=SecurityInput)
    ulimits: tuple[UlimitRow, ...] = ()
    log: LogInput | None = None


class FormReader(Protocol):
    """What the field-group parsers need from the form parser."""

    creating: bool
    administrator: bool

    def raw(self, name: str) -> str:
        """Return a posted value, empty when absent."""
        ...

    def text(self, name: str) -> str:
        """Return a posted value without surrounding whitespace."""
        ...

    def has(self, name: str) -> bool:
        """Report whether the field was posted at all."""
        ...

    def fail(self, name: str, message: str) -> None:
        """Record the first error of a field."""
        ...

    def convert[T](self, name: str, parse: Callable[..., T], *args: Any) -> T | None:
        """Parse a non-empty field; a value posted as the form showed it keeps the stored one."""
        ...

    def unchanged(self, name: str, *, lines: bool = False) -> bool:
        """Report whether the field was posted exactly as the edit form showed it."""
        ...

    def rows(self, group: str) -> list[tuple[int, dict[str, str]]]:
        """Return the filled rows of a repeat group with their form index."""
        ...


def _whole(text: str, label: str, low: int, high: int) -> int:
    value = parse_int(text, label)
    if not low <= value <= high:
        raise ValueError(f"{label} must be between {low} and {high}.")
    return value


def _duration_or_zero(text: str, label: str) -> int:
    return 0 if text.strip() in ("0", "0s") else parse_duration(text, label)


def _checked(text: str) -> bool:
    return text.strip().casefold() in _TRUTHY


def _absolute(text: str, label: str) -> str:
    if (
        len(text) > 4096
        or "\0" in text
        or not text.startswith("/")
        or "//" in text
        or any(part in ("..", ".") for part in text.split("/")[1:-1])
        or text.rsplit("/", 1)[-1] in ("..", ".")
    ):
        raise ValueError(f"{label} must be an absolute, normalised path.")
    return text


def _signal(text: str) -> str:
    if text.isascii() and text.isdigit():
        if not 1 <= int(text) <= MAX_STOP_SIGNAL:
            raise ValueError(f"A signal number must be between 1 and {MAX_STOP_SIGNAL}.")
        return text
    if _SIGNAL_NAME.fullmatch(text) is None:
        raise ValueError("Stop signal must be a signal name such as SIGTERM, or a number.")
    return text


def _hostname(text: str) -> str:
    if len(text) > 253 or not all(DNS_LABEL.fullmatch(part) for part in text.split(".")):
        raise ValueError("Host name must be a DNS name of letters, digits, hyphens and dots.")
    return text


def _is_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _host_entry(text: str) -> str:
    host, separator, address = text.partition(":")
    if not separator or not host or len(host) > 253:
        raise ValueError("Each entry must be a host name and an address, such as db:10.0.0.5.")
    if not all(DNS_LABEL.fullmatch(part) for part in host.split(".")):
        raise ValueError("The host name of an entry must be a DNS name.")
    try:
        literal = ipaddress.ip_address(address.strip())
    except ValueError:
        raise ValueError("The address of an entry must be an IPv4 or IPv6 literal.") from None
    return f"{literal} {host}"


def _host_stored(line: str) -> str:
    try:
        return _host_entry(line)
    except ValueError:
        return line


def _host_shown(entry: str) -> str:
    parts = entry.split()
    return f"{parts[1]}:{parts[0]}" if len(parts) == 2 and _is_address(parts[0]) else entry


def _nameserver(text: str) -> str:
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        raise ValueError("Each name server must be an IPv4 or IPv6 address.") from None


def _search_domain(text: str) -> str:
    if _SEARCH_DOMAIN.fullmatch(text) is None:
        raise ValueError("Each search domain must be a domain name.")
    return text


def _dns_option(text: str) -> str:
    if _DNS_OPTION.fullmatch(text) is None:
        raise ValueError("Each resolver option must look like ndots:2.")
    return text


def _group(text: str) -> str:
    if _GROUP.fullmatch(text) is None:
        raise ValueError("Each group must be a group name or id.")
    return text


def canonical_capability(text: str) -> str:
    """Return a capability as `CAP_NAME` or `ALL`; ValueError when unknown."""
    name = text.strip().upper().removeprefix("CAP_")
    if name != "ALL" and name not in CAPABILITIES:
        raise ValueError("Capabilities must be Linux capability names such as NET_ADMIN, or ALL.")
    return name if name == "ALL" else f"CAP_{name}"


def _sysctl(line: str) -> tuple[str, str]:
    key, separator, value = line.partition("=")
    key, value = key.strip(), value.strip()
    if not separator or _SYSCTL_KEY.fullmatch(key) is None:
        raise ValueError(
            "Each kernel parameter must be name=value with a net.* or kernel.* name, "
            "such as net.core.somaxconn=1024."
        )
    if len(value) > MAX_VALUE_CHARS or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(
            f"A value must be at most {MAX_VALUE_CHARS} characters without control characters."
        )
    return key, value


def _sysctl_stored(line: str) -> tuple[str, str]:
    key, _, value = line.partition("=")
    return key.strip(), value.strip()


def _pieces(text: str, lines: bool) -> list[str]:
    if lines:
        return [line.strip() for line in text.splitlines() if line.strip()]
    return [piece for piece in _SPLIT.split(text.strip()) if piece]


def _items[T](
    form: FormReader,
    name: str,
    noun: str,
    cap: int,
    check: Callable[[str], T],
    held: Callable[[str], T],
    *,
    lines: bool = False,
) -> tuple[T, ...]:
    pieces = _pieces(form.raw(name), lines)
    if form.unchanged(name, lines=lines):
        return tuple(held(piece) for piece in pieces)
    if len(pieces) > cap:
        form.fail(name, f"At most {cap} {noun} are allowed.")
        return ()
    found: list[T] = []
    for piece in pieces:
        try:
            found.append(check(piece))
        except ValueError as error:
            form.fail(name, str(error))
    return tuple(dict.fromkeys(found))


def _plain_items(
    form: FormReader, name: str, noun: str, cap: int, check: Callable[[str], str]
) -> tuple[str, ...]:
    return _items(form, name, noun, cap, check, lambda piece: piece)


def parse_task(form: FormReader) -> TaskInput:
    """Read every field group of the container spec from the posted form."""
    return TaskInput(
        health=_parse_health(form),
        process=_parse_process(form),
        host=_parse_host(form),
        security=_parse_security(form),
        ulimits=_parse_ulimits(form),
        log=_parse_log(form),
    )


def _parse_health(form: FormReader) -> HealthInput:
    mode = form.text("health_test")
    allowed = HEALTH_MODES if form.creating else (*HEALTH_MODES, "keep")
    if mode not in allowed:
        form.fail("health_test", "Test must be inherit, disabled, CMD or CMD-SHELL.")
        mode = ""
    command: tuple[str, ...] = ()
    text = form.raw("health_command").strip()
    if mode == "cmd":
        argv = form.convert("health_command", parse_argv, "Command")
        command = argv or ()
        if not command and not text:
            form.fail("health_command", "A command is required.")
    elif mode == "shell":
        if not text:
            form.fail("health_command", "A command is required.")
        elif len(text) > MAX_COMMAND_CHARS or "\0" in text:
            form.fail("health_command", f"Command must be at most {MAX_COMMAND_CHARS} characters.")
        else:
            command = (text,)
    elif text and mode != "keep":
        form.fail("health_command", "Choose CMD or CMD-SHELL to give a command.")
    return HealthInput(
        mode=mode,
        command=command,
        interval=form.convert("health_interval", parse_duration, "Interval"),
        timeout=form.convert("health_timeout", parse_duration, "Timeout"),
        retries=form.convert("health_retries", _whole, "Retries", 0, MAX_RETRIES),
        start_period=form.convert("health_start_period", _duration_or_zero, "Start period"),
        start_interval=form.convert("health_start_interval", parse_duration, "Start interval"),
    )


def _parse_process(form: FormReader) -> ProcessInput:
    user = form.text("user")
    if (
        user
        and not form.unchanged("user")
        and (len(user) > 128 or USER_PATTERN.fullmatch(user) is None)
    ):
        form.fail("user", "User must be a name or id, optionally with a group, such as 1000:1000.")
    working_dir = form.text("working_dir")
    if working_dir and not form.unchanged("working_dir"):
        try:
            _absolute(working_dir, "Working directory")
        except ValueError as error:
            form.fail("working_dir", str(error))
    stop_signal = form.text("stop_signal")
    if stop_signal and not form.unchanged("stop_signal"):
        try:
            _signal(stop_signal)
        except ValueError as error:
            form.fail("stop_signal", str(error))
    init_text = form.text("init")
    init: bool | None = None
    if init_text in ("true", "false"):
        init = init_text == "true"
    elif init_text:
        form.fail("init", "Init must be the default, true or false.")
    return ProcessInput(
        user=user,
        groups=_plain_items(form, "groups", "groups", MAX_GROUPS, _group),
        working_dir=working_dir,
        init=init,
        stop_signal=stop_signal,
        stop_grace_period=form.convert("stop_grace_period", _duration_or_zero, "Stop grace period"),
        tty=_checked(form.raw("tty")),
        open_stdin=_checked(form.raw("open_stdin")),
    )


def _parse_host(form: FormReader) -> HostInput:
    hostname = form.text("hostname")
    if hostname and not form.unchanged("hostname"):
        try:
            _hostname(hostname)
        except ValueError as error:
            form.fail("hostname", str(error))
    return HostInput(
        hostname=hostname,
        hosts=_items(form, "hosts", "entries", MAX_HOSTS, _host_entry, _host_stored, lines=True),
        nameservers=_plain_items(
            form, "dns_nameservers", "name servers", MAX_NAMESERVERS, _nameserver
        ),
        search=_plain_items(
            form, "dns_search", "search domains", MAX_SEARCH_DOMAINS, _search_domain
        ),
        options=_plain_items(form, "dns_options", "options", MAX_DNS_OPTIONS, _dns_option),
    )


def _parse_security(form: FormReader) -> SecurityInput:
    cap_drop = _items(
        form, "cap_drop", "capabilities", MAX_CAPABILITIES, canonical_capability, lambda p: p
    )
    cap_add: tuple[str, ...] | None = None
    if form.has("cap_add"):
        cap_add = _items(
            form, "cap_add", "capabilities", MAX_CAPABILITIES, canonical_capability, lambda p: p
        )
    sysctls: tuple[tuple[str, str], ...] | None = None
    if form.has("sysctls"):
        sysctls = _items(
            form, "sysctls", "kernel parameters", MAX_SYSCTLS, _sysctl, _sysctl_stored, lines=True
        )
        keys = [key for key, _ in sysctls]
        if len(set(keys)) != len(keys):
            form.fail("sysctls", "A kernel parameter can be set once.")
    if cap_add is not None and {_bare(c) for c in cap_add} & {_bare(c) for c in cap_drop}:
        form.fail("cap_add", "A capability cannot be both added and dropped.")
        form.fail("cap_drop", "A capability cannot be both added and dropped.")
    return SecurityInput(
        read_only=_checked(form.raw("read_only")),
        no_new_privileges=_checked(form.raw("no_new_privileges")),
        cap_drop=cap_drop,
        cap_add=cap_add,
        sysctls=sysctls,
    )


def _parse_ulimits(form: FormReader) -> tuple[UlimitRow, ...]:
    rows: list[UlimitRow] = []
    seen: set[str] = set()
    for index, values in form.rows("ulimits"):
        prefix = f"ulimit-{index}"
        name = values["name"].strip()
        if name not in ULIMIT_NAMES and not form.unchanged(f"{prefix}-name"):
            form.fail(f"{prefix}-name", f"Name must be one of {', '.join(sorted(ULIMIT_NAMES))}.")
            continue
        if name in seen:
            form.fail(f"{prefix}-name", "Each limit can be set once.")
        seen.add(name)
        soft_text, hard_text = values["soft"].strip(), values["hard"].strip()
        try:
            if not soft_text:
                raise ValueError("A soft limit is required; -1 means unlimited.")
            soft = _whole(soft_text, "Soft limit", -1, MAX_LIMIT)
        except ValueError as error:
            form.fail(f"{prefix}-soft", str(error))
            continue
        try:
            hard = _whole(hard_text, "Hard limit", -1, MAX_LIMIT) if hard_text else soft
        except ValueError as error:
            form.fail(f"{prefix}-hard", str(error))
            continue
        if hard != -1 and (soft == -1 or soft > hard):
            form.fail(f"{prefix}-soft", "The soft limit cannot exceed the hard limit.")
            continue
        rows.append(UlimitRow(index, name, soft, hard))
    return tuple(rows)


def _parse_log(form: FormReader) -> LogInput | None:
    if not form.has("log_driver"):
        return None
    driver = form.text("log_driver")
    if driver and _LOG_DRIVER.fullmatch(driver) is None:
        form.fail(
            "log_driver", "Log driver may use lower-case letters, digits, underscores and hyphens."
        )
    options: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, values in form.rows("log_options"):
        prefix = f"logopt-{index}"
        key, value = values["key"].strip(), values["value"]
        if _LOG_KEY.fullmatch(key) is None:
            form.fail(f"{prefix}-key", "Option name may use lower-case letters, digits, . _ and -.")
            continue
        if len(value) > MAX_VALUE_CHARS or any(ord(c) < 32 or ord(c) == 127 for c in value):
            form.fail(
                f"{prefix}-value",
                f"A value must be at most {MAX_VALUE_CHARS} characters without control characters.",
            )
            continue
        if key in seen:
            form.fail(f"{prefix}-key", "Option names must be unique.")
        seen.add(key)
        options.append((key, value))
    if options and not driver:
        form.fail("log_driver", "A log driver is required when log options are set.")
    return LogInput(driver, tuple(options))


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _child(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def _put(node: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        node.pop(key, None)
    else:
        node[key] = value


def _tidy(parent: dict[str, Any], key: str) -> None:
    if key in parent and parent[key] == {}:
        del parent[key]


def _compact(value: Any) -> Any:
    if isinstance(value, Mapping):
        kept = {key: _compact(item) for key, item in value.items()}
        return {key: item for key, item in kept.items() if item is not None} or None
    if isinstance(value, list | tuple):
        return [item for item in (_compact(item) for item in value) if item is not None] or None
    if value is None or value is False or value == "":
        return None
    return value


def _exact(value: Any) -> Any:
    return value


def _bare(name: Any) -> str:
    return str(name).strip().upper().removeprefix("CAP_")


def _health_norm(value: Any) -> Any:
    node = _map(value)
    return _compact(
        {key: None if key in _NUMERIC_HEALTH and item == 0 else item for key, item in node.items()}
    )


def _caps_norm(value: Any) -> Any:
    return sorted({_bare(name) for name in _list(value)}) or None


def _sysctls_norm(value: Any) -> Any:
    node = _map(value)
    return {str(key): str(item).strip() for key, item in node.items()} or None


def _ulimits_norm(value: Any) -> Any:
    entries = [
        (entry.get("Name"), entry.get("Soft"), entry.get("Hard"))
        for entry in _list(value)
        if isinstance(entry, Mapping)
    ]
    return sorted(entries, key=str) or None


def _log_norm(value: Any) -> Any:
    return _compact(value)


def _settle(
    node: dict[str, Any],
    key: str,
    old: Mapping[str, Any],
    norm: Callable[[Any], Any] = _compact,
) -> None:
    """Put the stored node back, spelled as stored, when it means the same as the new one."""
    if norm(node.get(key)) != norm(old.get(key)):
        return
    if key in old:
        node[key] = copy.deepcopy(old[key])
    else:
        node.pop(key, None)


def write_task(
    task: dict[str, Any],
    container: dict[str, Any],
    old_task: Mapping[str, Any],
    old_container: Mapping[str, Any],
    fields: TaskInput,
) -> None:
    """Write the field groups onto the task template, leaving every key they do not manage."""
    _write_health(container, old_container, fields.health)
    _write_process(container, old_container, fields.process)
    _write_host(container, old_container, fields.host)
    _write_security(container, old_container, fields.security)
    _write_ulimits(container, old_container, fields.ulimits)
    _write_log(task, old_task, fields.log)


def _write_health(container: dict[str, Any], old: Mapping[str, Any], health: HealthInput) -> None:
    node = _child(container, "Healthcheck")
    match health.mode:
        case "none":
            node["Test"] = ["NONE"]
        case "cmd":
            node["Test"] = ["CMD", *health.command]
        case "shell":
            node["Test"] = ["CMD-SHELL", *health.command]
        case "keep":
            pass
        case _:
            node.pop("Test", None)
    _put(node, "Interval", health.interval)
    _put(node, "Timeout", health.timeout)
    _put(node, "Retries", health.retries)
    _put(node, "StartPeriod", health.start_period)
    _put(node, "StartInterval", health.start_interval)
    _tidy(container, "Healthcheck")
    _settle(container, "Healthcheck", old, _health_norm)


def _write_process(
    container: dict[str, Any], old: Mapping[str, Any], process: ProcessInput
) -> None:
    _put(container, "User", process.user or None)
    _put(container, "Groups", list(process.groups) or None)
    _put(container, "Dir", process.working_dir or None)
    _put(container, "Init", process.init)
    _put(container, "StopSignal", process.stop_signal or None)
    _put(container, "StopGracePeriod", process.stop_grace_period)
    _put(container, "TTY", True if process.tty else None)
    _put(container, "OpenStdin", True if process.open_stdin else None)
    for key in ("User", "Groups", "Dir", "StopSignal", "StopGracePeriod", "TTY", "OpenStdin"):
        _settle(container, key, old)
    _settle(container, "Init", old, _exact)


def _write_host(container: dict[str, Any], old: Mapping[str, Any], host: HostInput) -> None:
    _put(container, "Hostname", host.hostname or None)
    _put(container, "Hosts", list(host.hosts) or None)
    dns = _child(container, "DNSConfig")
    _put(dns, "Nameservers", list(host.nameservers) or None)
    _put(dns, "Search", list(host.search) or None)
    _put(dns, "Options", list(host.options) or None)
    _tidy(container, "DNSConfig")
    for key in ("Hostname", "Hosts", "DNSConfig"):
        _settle(container, key, old)


def _write_security(
    container: dict[str, Any], old: Mapping[str, Any], security: SecurityInput
) -> None:
    _put(container, "ReadOnly", True if security.read_only else None)
    privileges = _child(container, "Privileges")
    _put(privileges, "NoNewPrivileges", True if security.no_new_privileges else None)
    _tidy(container, "Privileges")
    _put(container, "CapabilityDrop", list(security.cap_drop) or None)
    if security.cap_add is not None:
        _put(container, "CapabilityAdd", list(security.cap_add) or None)
    if security.sysctls is not None:
        _put(container, "Sysctls", dict(security.sysctls) or None)
    for key, norm in (
        ("ReadOnly", _compact),
        ("Privileges", _compact),
        ("CapabilityDrop", _caps_norm),
        ("CapabilityAdd", _caps_norm),
        ("Sysctls", _sysctls_norm),
    ):
        _settle(container, key, old, norm)


def _write_ulimits(
    container: dict[str, Any], old: Mapping[str, Any], ulimits: tuple[UlimitRow, ...]
) -> None:
    _put(
        container,
        "Ulimits",
        [{"Name": row.name, "Soft": row.soft, "Hard": row.hard} for row in ulimits] or None,
    )
    _settle(container, "Ulimits", old, _ulimits_norm)


def _write_log(task: dict[str, Any], old: Mapping[str, Any], log: LogInput | None) -> None:
    if log is None:
        return
    node: dict[str, Any] = {}
    if log.driver:
        node["Name"] = log.driver
    if log.options:
        node["Options"] = dict(log.options)
    _put(task, "LogDriver", node or None)
    _settle(task, "LogDriver", old, _log_norm)


def project_task(task: Mapping[str, Any], container: Mapping[str, Any]) -> dict[str, Any]:
    """Return the comparable view of each field group, keyed by review category."""
    init = container.get("Init")
    privileges = _map(container.get("Privileges"))
    dns = _map(container.get("DNSConfig"))
    return {
        "healthcheck": _health_norm(container.get("Healthcheck")),
        "process": {
            "user": container.get("User"),
            "groups": container.get("Groups"),
            "dir": container.get("Dir"),
            "init": None if init is None else str(bool(init)).lower(),
            "stop_signal": container.get("StopSignal"),
            "stop_grace_period": container.get("StopGracePeriod"),
            "tty": container.get("TTY"),
            "open_stdin": container.get("OpenStdin"),
        },
        "host": {
            "hostname": container.get("Hostname"),
            "hosts": container.get("Hosts"),
            "nameservers": dns.get("Nameservers"),
            "search": dns.get("Search"),
            "options": dns.get("Options"),
        },
        "security": {
            "read_only": container.get("ReadOnly"),
            "no_new_privileges": privileges.get("NoNewPrivileges"),
            "cap_drop": _caps_norm(container.get("CapabilityDrop")),
            "cap_add": _caps_norm(container.get("CapabilityAdd")),
            "sysctls": _sysctls_norm(container.get("Sysctls")),
        },
        "ulimits": _ulimits_norm(container.get("Ulimits")),
        "logging": _log_norm(task.get("LogDriver")),
    }


def strip_managed_privileges(container: dict[str, Any]) -> None:
    """Remove the privilege setting the form manages, leaving the keys it does not."""
    privileges = container.get("Privileges")
    if isinstance(privileges, dict):
        privileges.pop("NoNewPrivileges", None)


def log_is_allowed(log: Any) -> bool:
    """Report whether a stored log driver is within what a standard user may set."""
    node = _map(log)
    name = node.get("Name")
    options = node.get("Options")
    if name not in (None, "") and name not in ALLOWED_LOG_DRIVERS:
        return False
    if options in (None, {}):
        return True
    return isinstance(options, Mapping) and all(key in ALLOWED_LOG_OPTION_KEYS for key in options)


def _health_test(test: Any) -> tuple[str, str]:
    if test is None or test == []:
        return "", ""
    if not isinstance(test, list) or not all(isinstance(word, str) for word in test):
        return "keep", ""
    head, rest = test[0], test[1:]
    if test == ["NONE"]:
        return "none", ""
    if head == "CMD" and rest:
        return "cmd", shlex.join(rest)
    if head == "CMD-SHELL" and len(rest) == 1 and not any(c in rest[0] for c in "\r\n\0"):
        return "shell", rest[0]
    return "keep", ""


def task_values(
    put: Callable[[str, Any], None],
    task: Mapping[str, Any],
    container: Mapping[str, Any],
    *,
    administrator: bool,
    duration_text: Callable[[Any], str | None],
) -> None:
    """Add the edit-form values of the field groups.

    A standard user is never shown the fields the restriction set refuses; a stored value is
    reported under a `locked_` name instead.
    """
    health = _map(container.get("Healthcheck"))
    mode, command = _health_test(health.get("Test"))
    put("health_test", mode)
    put("health_command", command)
    put("health_interval", duration_text(health.get("Interval")))
    put("health_timeout", duration_text(health.get("Timeout")))
    put("health_retries", _number(health.get("Retries")))
    put("health_start_period", duration_text(health.get("StartPeriod")))
    put("health_start_interval", duration_text(health.get("StartInterval")))

    put("user", container.get("User"))
    put("groups", " ".join(str(g) for g in _list(container.get("Groups"))))
    put("working_dir", container.get("Dir"))
    init = container.get("Init")
    put("init", None if not isinstance(init, bool) else str(init).lower())
    put("stop_signal", container.get("StopSignal"))
    put("stop_grace_period", duration_text(container.get("StopGracePeriod")))
    put("tty", "on" if container.get("TTY") is True else None)
    put("open_stdin", "on" if container.get("OpenStdin") is True else None)

    put("hostname", container.get("Hostname"))
    hosts = [_host_shown(str(entry)) for entry in _list(container.get("Hosts"))]
    put("hosts", "\n".join(hosts))
    dns = _map(container.get("DNSConfig"))
    put("dns_nameservers", " ".join(str(v) for v in _list(dns.get("Nameservers"))))
    put("dns_search", " ".join(str(v) for v in _list(dns.get("Search"))))
    put("dns_options", " ".join(str(v) for v in _list(dns.get("Options"))))

    put("read_only", "on" if container.get("ReadOnly") is True else None)
    privileges = _map(container.get("Privileges"))
    put("no_new_privileges", "on" if privileges.get("NoNewPrivileges") is True else None)
    put("cap_drop", " ".join(str(c) for c in _list(container.get("CapabilityDrop"))))
    added = " ".join(str(c) for c in _list(container.get("CapabilityAdd")))
    sysctls = "\n".join(f"{key}={value}" for key, value in _map(container.get("Sysctls")).items())
    log = _map(task.get("LogDriver"))
    log_name = log.get("Name")
    log_options = _map(log.get("Options"))
    options_text = "\n".join(f"{key}={value}" for key, value in log_options.items())
    limits = [entry for entry in _list(container.get("Ulimits")) if isinstance(entry, Mapping)]
    for ordinal, entry in enumerate(limits):
        put(f"ulimit-{ordinal}-name", entry.get("Name"))
        put(f"ulimit-{ordinal}-soft", entry.get("Soft"))
        put(f"ulimit-{ordinal}-hard", entry.get("Hard"))
    if administrator:
        put("cap_add", added)
        put("sysctls", sysctls)
    else:
        put("locked_cap_add", added)
        put("locked_sysctls", sysctls)
    if administrator or log_is_allowed(log):
        put("log_driver", log_name)
        for ordinal, (key, value) in enumerate(log_options.items()):
            put(f"logopt-{ordinal}-key", key)
            put(f"logopt-{ordinal}-value", value)
    else:
        put("locked_log", "1")
        put("locked_log_driver", log_name)
        put("locked_log_options", options_text)
