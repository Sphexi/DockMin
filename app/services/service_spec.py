"""Swarm service form parsing, and the ServiceSpec bodies built from it or edited by it."""

import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, cast

from app.auth.errors import ValidationFailed
from app.logging import DEFAULT_SCRUBBER
from app.services.access import RESERVED_LABELS, SubmittedLabels
from app.services.container_spec import (
    DNS_LABEL,
    MAX_COMMAND_CHARS,
    MAX_ENV_CHARS,
    MAX_LABEL_CHARS,
    RESOURCE_NAME,
    format_memory,
    parse_argv,
    parse_duration,
    parse_image,
    parse_int,
    parse_memory,
)
from app.services.service_restrictions import parse_constraint, pinned_to
from app.services.service_spec_task import (
    MANAGED_CONTAINER_KEYS,
    MANAGED_TASK_KEYS,
    TaskInput,
    parse_task,
    project_task,
    strip_managed_privileges,
    task_values,
    write_task,
)

__all__ = [
    "CATEGORIES",
    "CHECKBOX_FIELDS",
    "MAX_REPLICAS",
    "MAX_REPLICAS_STANDARD",
    "OBJECT_NAME",
    "ROW_CAPS",
    "ROW_FIELDS",
    "ROW_PREFIX",
    "SCALAR_FIELDS",
    "SERVICE_NAME",
    "ConfigRow",
    "EnvRow",
    "LabelRow",
    "MountRow",
    "NetworkRow",
    "PortRow",
    "ResolvedRefs",
    "ResourceInput",
    "RestartInput",
    "RolloutInput",
    "SecretRow",
    "ServiceInput",
    "build_spec",
    "changed_categories",
    "check_labels",
    "force_counter",
    "forced",
    "form_values",
    "format_exact_duration",
    "image_of",
    "merge_spec",
    "mode_of",
    "parse_replicas",
    "parse_service_form",
    "replicas_of",
    "scaled",
]

SERVICE_NAME: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?")
OBJECT_NAME: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?")
MAX_REPLICAS_STANDARD: Final = 100
MAX_REPLICAS: Final = 1000

MAX_CONSTRAINTS: Final = 32
MAX_CONSTRAINT_CHARS: Final = 256
MAX_PREFERENCES: Final = 8
MAX_ALIASES: Final = 16
MIN_NANO_CPUS: Final = 1_000_000
MIN_MEMORY_BYTES: Final = 4 * 1024 * 1024
MAX_CPUS: Final = 100_000
MAX_INT32: Final = 2**31 - 1
MAX_FORCE_UPDATE: Final = 2**63 - 1

CATEGORIES: Final[tuple[str, ...]] = (
    "image",
    "mode",
    "command",
    "environment",
    "labels",
    "placement",
    "resources",
    "restart_policy",
    "update_config",
    "rollback_config",
    "networks",
    "ports",
    "mounts",
    "configs",
    "secrets",
    "healthcheck",
    "process",
    "host",
    "security",
    "ulimits",
    "logging",
    "other",
)

ROW_PREFIX: Final[Mapping[str, str]] = {
    "env": "env",
    "labels": "label",
    "networks": "network",
    "ports": "port",
    "mounts": "mount",
    "configs": "config",
    "secrets": "secret",
    "ulimits": "ulimit",
    "log_options": "logopt",
}
ROW_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    "env": ("key", "value"),
    "labels": ("key", "value"),
    "networks": ("name", "aliases"),
    "ports": ("target", "published", "protocol", "mode"),
    "mounts": ("type", "source", "target", "read_only", "tmpfs_size"),
    "configs": ("name", "target", "uid", "gid", "mode"),
    "secrets": ("name", "target", "uid", "gid", "mode"),
    "ulimits": ("name", "soft", "hard"),
    "log_options": ("key", "value"),
}
ROW_CAPS: Final[Mapping[str, int]] = {
    "env": 100,
    "labels": 64,
    "networks": 32,
    "ports": 64,
    "mounts": 64,
    "configs": 64,
    "secrets": 64,
    "ulimits": 32,
    "log_options": 16,
}
ROW_DEFAULTED: Final[Mapping[str, tuple[str, ...]]] = {
    "ports": ("protocol", "mode"),
    "mounts": ("type",),
    "ulimits": ("name",),
}
ROW_CHECKBOXES: Final[Mapping[str, tuple[str, ...]]] = {"mounts": ("read_only",)}
GROUP_NOUNS: Final[Mapping[str, str]] = {
    "env": "environment variables",
    "labels": "labels",
    "networks": "networks",
    "ports": "published ports",
    "mounts": "mounts",
    "configs": "configs",
    "secrets": "secrets",
    "ulimits": "resource limits",
    "log_options": "log options",
}
SCALAR_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "image",
    "mode",
    "replicas",
    "command",
    "constraints",
    "preferences",
    "max_replicas_per_node",
    "run_on_manager",
    "cpu_limit",
    "memory_limit",
    "cpu_reservation",
    "memory_reservation",
    "pids_limit",
    "restart_condition",
    "restart_delay",
    "restart_max_attempts",
    "restart_window",
    "update_parallelism",
    "update_delay",
    "update_failure_action",
    "update_monitor",
    "update_max_failure_ratio",
    "update_order",
    "rollback_parallelism",
    "rollback_delay",
    "rollback_failure_action",
    "rollback_monitor",
    "rollback_max_failure_ratio",
    "rollback_order",
    "endpoint_mode",
    "health_test",
    "health_command",
    "health_interval",
    "health_timeout",
    "health_retries",
    "health_start_period",
    "health_start_interval",
    "user",
    "groups",
    "working_dir",
    "init",
    "stop_signal",
    "stop_grace_period",
    "tty",
    "open_stdin",
    "hostname",
    "hosts",
    "dns_nameservers",
    "dns_search",
    "dns_options",
    "read_only",
    "no_new_privileges",
    "cap_drop",
    "cap_add",
    "sysctls",
    "log_driver",
)
CHECKBOX_FIELDS: Final[frozenset[str]] = frozenset(
    {"run_on_manager", "tty", "open_stdin", "read_only", "no_new_privileges"}
)

_ROW_KEY: Final = re.compile(
    r"(env|label|network|port|mount|config|secret|ulimit|logopt)-([0-9]{1,4})-([a-z_]+)"
)
_PREFIX_GROUP: Final = {prefix: group for group, prefix in ROW_PREFIX.items()}
_TRUTHY: Final = frozenset({"on", "true", "1", "yes"})
_ID_SHAPED: Final = re.compile(r"[0-9a-z]{25}")
_NETWORK_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_PREFERENCE: Final = re.compile(r"spread=([A-Za-z0-9_.-]{1,256})")
_CPUS: Final = re.compile(r"[0-9]+(?:\.[0-9]{1,9})?")
_FILE_MODE: Final = re.compile(r"0?[0-7]{1,4}")
_FILE_ID: Final = re.compile(r"[0-9]{1,10}")
_IMAGE_ID: Final = re.compile(r"sha256:[0-9a-f]{64}")
_RESERVED_FOLDED: Final = frozenset(label.casefold() for label in RESERVED_LABELS)

_DURATION_UNITS: Final = (
    ("h", 3_600_000_000_000),
    ("m", 60_000_000_000),
    ("s", 1_000_000_000),
    ("ms", 1_000_000),
    ("us", 1_000),
    ("ns", 1),
)
_RESTART_CONDITIONS: Final = ("none", "on-failure", "any")
_UPDATE_ACTIONS: Final = ("continue", "pause", "rollback")
_ROLLBACK_ACTIONS: Final = ("continue", "pause")
_ORDERS: Final = ("stop-first", "start-first")
_PROTOCOLS: Final = ("tcp", "udp", "sctp")
_PUBLISH_MODES: Final = ("ingress", "host")
_ENDPOINT_MODES: Final = ("vip", "dnsrr")
_MOUNT_TYPES: Final = ("volume", "bind", "tmpfs")
_MODELLED_MOUNT_TYPES: Final = frozenset(_MOUNT_TYPES)


@dataclass(frozen=True, slots=True)
class EnvRow:
    """One environment variable; value None keeps the value the service already holds."""

    slot: int
    key: str
    value: str | None


@dataclass(frozen=True, slots=True)
class LabelRow:
    """One service label; value None keeps the value the service already holds."""

    slot: int
    key: str
    value: str | None


@dataclass(frozen=True, slots=True)
class NetworkRow:
    """One network, by name or id, with its aliases."""

    slot: int
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PortRow:
    """One published port."""

    slot: int
    target: int
    published: int | None
    protocol: str = "tcp"
    mode: str = "ingress"


@dataclass(frozen=True, slots=True)
class MountRow:
    """One volume, bind or tmpfs mount."""

    slot: int
    type: str
    source: str
    target: str
    read_only: bool = False
    tmpfs_size: int | None = None


@dataclass(frozen=True, slots=True)
class ConfigRow:
    """One config file exposed in the container."""

    slot: int
    name: str
    target: str
    uid: str
    gid: str
    mode: int


@dataclass(frozen=True, slots=True)
class SecretRow:
    """One secret file exposed in the container."""

    slot: int
    name: str
    target: str
    uid: str
    gid: str
    mode: int


@dataclass(frozen=True, slots=True)
class ResourceInput:
    """CPU in nano-CPUs, memory in bytes, and the process limit; None leaves a value unset."""

    cpu_limit: int | None = None
    memory_limit: int | None = None
    cpu_reservation: int | None = None
    memory_reservation: int | None = None
    pids_limit: int | None = None


@dataclass(frozen=True, slots=True)
class RestartInput:
    """Restart policy; durations in nanoseconds."""

    condition: str | None = None
    delay: int | None = None
    max_attempts: int | None = None
    window: int | None = None


@dataclass(frozen=True, slots=True)
class RolloutInput:
    """Update or rollback configuration; durations in nanoseconds."""

    parallelism: int | None = None
    delay: int | None = None
    failure_action: str | None = None
    monitor: int | None = None
    max_failure_ratio: float | None = None
    order: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceInput:
    """Every field the service form manages; name is empty when updating."""

    name: str
    image: str
    mode: Literal["replicated", "global"] = "replicated"
    replicas: int | None = 1
    command: tuple[str, ...] = ()
    env: tuple[EnvRow, ...] = ()
    labels: tuple[LabelRow, ...] = ()
    constraints: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    max_replicas_per_node: int | None = None
    run_on_manager: bool = False
    resources: ResourceInput = field(default_factory=ResourceInput)
    restart: RestartInput = field(default_factory=RestartInput)
    update: RolloutInput = field(default_factory=RolloutInput)
    rollback: RolloutInput = field(default_factory=RolloutInput)
    networks: tuple[NetworkRow, ...] = ()
    ports: tuple[PortRow, ...] = ()
    endpoint_mode: str | None = None
    mounts: tuple[MountRow, ...] = ()
    configs: tuple[ConfigRow, ...] = ()
    secrets: tuple[SecretRow, ...] = ()
    task: TaskInput = field(default_factory=TaskInput)


@dataclass(frozen=True, slots=True)
class ResolvedRefs:
    """What the service layer resolved for a ServiceInput, keyed by form row index.

    image is the reference to run, digest-pinned or not. configs and secrets map to (id, name);
    manager_node_id is needed only when run_on_manager is set.
    """

    networks: Mapping[int, str]
    configs: Mapping[int, tuple[str, str]]
    secrets: Mapping[int, tuple[str, str]]
    image: str
    manager_node_id: str | None = None


def _invalid(message: str) -> ValueError:
    return ValueError(message)


def _is_checked(text: str) -> bool:
    return text.strip().casefold() in _TRUTHY


def _bounded_int(text: str, label: str, low: int, high: int) -> int:
    value = parse_int(text, label)
    if not low <= value <= high:
        raise _invalid(f"{label} must be between {low} and {high}.")
    return value


def _duration(text: str, label: str) -> int:
    if text.strip() in ("0", "0s"):
        return 0
    return parse_duration(text, label)


def _cpus(text: str, label: str) -> int:
    value = text.strip()
    if _CPUS.fullmatch(value) is None:
        raise _invalid(f"{label} must be a number such as 0.5 or 2.")
    nano = Decimal(value) * 1_000_000_000
    if not MIN_NANO_CPUS <= nano <= MAX_CPUS * 1_000_000_000:
        raise _invalid(f"{label} must be between 0.001 and {MAX_CPUS}.")
    return int(nano)


def _memory(text: str, label: str) -> int:
    value = parse_memory(text, label)
    if value < MIN_MEMORY_BYTES:
        raise _invalid(f"{label} must be at least 4 MiB.")
    return value


def _ratio(text: str, label: str) -> float:
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise _invalid(f"{label} must be a number from 0 to 1.") from None
    if not value.is_finite() or not 0 <= value <= 1:
        raise _invalid(f"{label} must be a number from 0 to 1.")
    return float(value)


def _choice(text: str, label: str, options: Sequence[str]) -> str:
    if text not in options:
        raise _invalid(f"{label} must be one of {', '.join(options)}.")
    return text


def _absolute_path(value: str, label: str) -> str:
    if (
        not value
        or len(value) > 4096
        or "\0" in value
        or not value.startswith("/")
        or "//" in value
        or any(part in ("..", ".") for part in value.split("/"))
    ):
        raise _invalid(f"{label} must be an absolute, normalised path.")
    return value


def parse_replicas(text: str, *, administrator: bool) -> int:
    """Parse a replica count, raising ValidationFailed keyed `replicas` when it is out of range."""
    ceiling = MAX_REPLICAS if administrator else MAX_REPLICAS_STANDARD
    try:
        return _bounded_int(text, "Replicas", 0, ceiling)
    except ValueError as error:
        raise ValidationFailed({"replicas": str(error)}) from None


def parse_service_form(
    form: Mapping[str, str],
    *,
    creating: bool,
    administrator: bool,
    current: Mapping[str, Any] | None = None,
) -> ServiceInput:
    """Validate a submitted service form, raising ValidationFailed with every field error found.

    Errors are keyed by form field name. On an update, an environment or label row whose value
    was withheld from the viewer and left empty keeps the value the service holds. With
    `current`, the stored spec of an update, a number or duration field posted exactly as the
    edit form showed it keeps the stored value without being checked against the form's ranges.
    """
    parser = _Parser(form, creating=creating, administrator=administrator, current=current)
    result = parser.parse()
    if parser.errors:
        raise ValidationFailed(dict(parser.errors))
    return result


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


_STORED_SCALARS: Final[Mapping[str, tuple[str, ...]]] = {
    "replicas": ("Mode", "Replicated", "Replicas"),
    "max_replicas_per_node": ("TaskTemplate", "Placement", "MaxReplicas"),
    "cpu_limit": ("TaskTemplate", "Resources", "Limits", "NanoCPUs"),
    "memory_limit": ("TaskTemplate", "Resources", "Limits", "MemoryBytes"),
    "pids_limit": ("TaskTemplate", "Resources", "Limits", "Pids"),
    "cpu_reservation": ("TaskTemplate", "Resources", "Reservations", "NanoCPUs"),
    "memory_reservation": ("TaskTemplate", "Resources", "Reservations", "MemoryBytes"),
    "restart_delay": ("TaskTemplate", "RestartPolicy", "Delay"),
    "restart_max_attempts": ("TaskTemplate", "RestartPolicy", "MaxAttempts"),
    "restart_window": ("TaskTemplate", "RestartPolicy", "Window"),
    "stop_grace_period": ("TaskTemplate", "ContainerSpec", "StopGracePeriod"),
    **{
        f"health_{name}": ("TaskTemplate", "ContainerSpec", "Healthcheck", node)
        for name, node in (
            ("interval", "Interval"),
            ("timeout", "Timeout"),
            ("retries", "Retries"),
            ("start_period", "StartPeriod"),
            ("start_interval", "StartInterval"),
        )
    },
    **{
        f"{prefix}_{field_name}": (key, node)
        for prefix, key in (("update", "UpdateConfig"), ("rollback", "RollbackConfig"))
        for field_name, node in (
            ("parallelism", "Parallelism"),
            ("delay", "Delay"),
            ("monitor", "Monitor"),
            ("max_failure_ratio", "MaxFailureRatio"),
        )
    },
}


def _stored_scalars(
    spec: Mapping[str, Any], shown: Mapping[str, str]
) -> dict[str, tuple[str, Any]]:
    """Map each number field of the form to the text the edit form shows and the stored value."""
    found: dict[str, tuple[str, Any]] = {}
    for name, path in _STORED_SCALARS.items():
        node: Any = spec
        for step in path:
            node = _map(node).get(step)
        if name in shown and isinstance(node, int | float) and not isinstance(node, bool):
            found[name] = (shown[name], node)
    return found


class _Parser:
    def __init__(
        self,
        form: Mapping[str, str],
        *,
        creating: bool,
        administrator: bool,
        current: Mapping[str, Any] | None = None,
    ) -> None:
        self.form = form
        self.creating = creating
        self.administrator = administrator
        self.errors: dict[str, str] = {}
        self._shown = (
            {} if creating or current is None else form_values(current, administrator=administrator)
        )
        self._stored = {} if current is None else _stored_scalars(current, self._shown)

    def raw(self, name: str) -> str:
        value = self.form.get(name, "")
        return value if isinstance(value, str) else ""

    def text(self, name: str) -> str:
        return self.raw(name).strip()

    def fail(self, name: str, message: str) -> None:
        self.errors.setdefault(name, message)

    def has(self, name: str) -> bool:
        return name in self.form

    def rows(self, group: str) -> list[tuple[int, dict[str, str]]]:
        return self._rows(group)

    def unchanged(self, name: str, *, lines: bool = False) -> bool:
        shown = self._shown.get(name)
        if shown is None:
            return False
        if lines:
            return _lines(self.raw(name)) == _lines(shown)
        return self.raw(name).split() == shown.split()

    def convert[T](self, name: str, parse: Callable[..., T], *args: Any) -> T | None:
        raw = self.text(name)
        if not raw:
            return None
        shown, stored = self._stored.get(name, (None, None))
        if shown == raw:
            return cast(T, stored)
        try:
            return parse(raw, *args)
        except ValueError as error:
            self.fail(name, str(error))
            return None

    def parse(self) -> ServiceInput:
        mode, replicas = self._mode()
        return ServiceInput(
            name=self._name(),
            image=self._image(),
            mode=mode,
            replicas=replicas,
            command=self._command(),
            env=self._env(),
            labels=self._labels(),
            constraints=self._constraints(),
            preferences=self._preferences(),
            max_replicas_per_node=self.convert(
                "max_replicas_per_node",
                _bounded_int,
                "Maximum replicas per node",
                0,
                MAX_INT32,
            ),
            run_on_manager=_is_checked(self.raw("run_on_manager")),
            resources=self._resources(),
            restart=self._restart(),
            update=self._rollout("update", _UPDATE_ACTIONS),
            rollback=self._rollout("rollback", _ROLLBACK_ACTIONS),
            networks=self._networks(),
            ports=self._ports(),
            endpoint_mode=self._endpoint_mode(),
            mounts=self._mounts(),
            configs=tuple(ConfigRow(*row) for row in self._files("configs")),
            secrets=tuple(SecretRow(*row) for row in self._files("secrets")),
            task=parse_task(self),
        )

    def _name(self) -> str:
        if not self.creating:
            return ""
        name = self.text("name")
        if not name:
            self.fail("name", "Name is required.")
        elif SERVICE_NAME.fullmatch(name) is None:
            self.fail(
                "name",
                "Name must be 1 to 63 letters, digits, hyphens and underscores, "
                "starting and ending with a letter or digit.",
            )
        elif _ID_SHAPED.fullmatch(name):
            self.fail("name", "Name must not be 25 lower-case letters and digits, which is an id.")
        return name

    def _image(self) -> str:
        image = self.text("image")
        if not image:
            self.fail("image", "Image is required.")
            return ""
        if _IMAGE_ID.fullmatch(image):
            return image
        try:
            return parse_image(image)
        except ValueError as error:
            self.fail("image", str(error))
            return image

    def _mode(self) -> tuple[Literal["replicated", "global"], int | None]:
        mode = self.text("mode") or "replicated"
        if mode not in ("replicated", "global"):
            self.fail("mode", "Mode must be replicated or global.")
            return "replicated", 1
        replicas_text = self.text("replicas")
        if mode == "global":
            if replicas_text:
                self.fail("replicas", "Replicas apply only to replicated mode.")
            return "global", None
        if not replicas_text:
            return "replicated", 1
        ceiling = MAX_REPLICAS if self.administrator else MAX_REPLICAS_STANDARD
        replicas = self.convert("replicas", _bounded_int, "Replicas", 0, ceiling)
        return "replicated", 1 if replicas is None else replicas

    def _command(self) -> tuple[str, ...]:
        if len(self.raw("command")) > MAX_COMMAND_CHARS:
            self.fail("command", f"Command must be at most {MAX_COMMAND_CHARS} characters.")
            return ()
        return self.convert("command", parse_argv, "Command") or ()

    def _constraints(self) -> tuple[str, ...]:
        lines = [line.strip() for line in self.raw("constraints").splitlines()]
        lines = [line for line in lines if line]
        if len(lines) > MAX_CONSTRAINTS:
            self.fail("constraints", f"At most {MAX_CONSTRAINTS} constraints are allowed.")
            return ()
        for line in lines:
            if len(line) > MAX_CONSTRAINT_CHARS:
                self.fail(
                    "constraints",
                    f"A constraint must be at most {MAX_CONSTRAINT_CHARS} characters.",
                )
            elif parse_constraint(line) is None:
                self.fail(
                    "constraints",
                    "Each constraint must be an attribute, == or !=, and a value, "
                    "such as node.role==worker.",
                )
        return tuple(lines)

    def _preferences(self) -> tuple[str, ...]:
        lines = [line.strip() for line in self.raw("preferences").splitlines()]
        lines = [line for line in lines if line]
        if len(lines) > MAX_PREFERENCES:
            self.fail("preferences", f"At most {MAX_PREFERENCES} preferences are allowed.")
            return ()
        for line in lines:
            if _PREFERENCE.fullmatch(line) is None:
                self.fail(
                    "preferences",
                    "Each preference must be spread=<descriptor>, such as spread=node.labels.zone.",
                )
        return tuple(lines)

    def _resources(self) -> ResourceInput:
        resources = ResourceInput(
            cpu_limit=self.convert("cpu_limit", _cpus, "CPU limit"),
            memory_limit=self.convert("memory_limit", _memory, "Memory limit"),
            cpu_reservation=self.convert("cpu_reservation", _cpus, "CPU reservation"),
            memory_reservation=self.convert("memory_reservation", _memory, "Memory reservation"),
            pids_limit=self.convert("pids_limit", _bounded_int, "PIDs limit", 1, MAX_INT32),
        )
        for limit, reservation, name in (
            (resources.cpu_limit, resources.cpu_reservation, "cpu_reservation"),
            (resources.memory_limit, resources.memory_reservation, "memory_reservation"),
        ):
            if limit is not None and reservation is not None and reservation > limit:
                self.fail(name, "A reservation cannot exceed the limit.")
        return resources

    def _restart(self) -> RestartInput:
        condition = self.text("restart_condition") or None
        if condition is not None and condition not in _RESTART_CONDITIONS:
            self.fail(
                "restart_condition",
                f"Condition must be one of {', '.join(_RESTART_CONDITIONS)}.",
            )
            condition = None
        return RestartInput(
            condition=condition,
            delay=self.convert("restart_delay", _duration, "Restart delay"),
            max_attempts=self.convert(
                "restart_max_attempts", _bounded_int, "Maximum attempts", 0, MAX_INT32
            ),
            window=self.convert("restart_window", _duration, "Restart window"),
        )

    def _rollout(self, prefix: str, actions: Sequence[str]) -> RolloutInput:
        noun = "Update" if prefix == "update" else "Rollback"
        action = self.text(f"{prefix}_failure_action") or None
        if action is not None and action not in actions:
            self.fail(
                f"{prefix}_failure_action", f"Failure action must be one of {', '.join(actions)}."
            )
            action = None
        order = self.text(f"{prefix}_order") or None
        if order is not None and order not in _ORDERS:
            self.fail(f"{prefix}_order", f"Order must be one of {', '.join(_ORDERS)}.")
            order = None
        return RolloutInput(
            parallelism=self.convert(
                f"{prefix}_parallelism", _bounded_int, f"{noun} parallelism", 0, MAX_INT32
            ),
            delay=self.convert(f"{prefix}_delay", _duration, f"{noun} delay"),
            failure_action=action,
            monitor=self.convert(f"{prefix}_monitor", _duration, f"{noun} monitor"),
            max_failure_ratio=self.convert(
                f"{prefix}_max_failure_ratio", _ratio, "Maximum failure ratio"
            ),
            order=order,
        )

    def _endpoint_mode(self) -> str | None:
        mode = self.text("endpoint_mode") or None
        if mode is not None and mode not in _ENDPOINT_MODES:
            self.fail(
                "endpoint_mode", f"Endpoint mode must be one of {', '.join(_ENDPOINT_MODES)}."
            )
            return None
        return mode

    def _rows(self, group: str) -> list[tuple[int, dict[str, str]]]:
        prefix, columns = ROW_PREFIX[group], ROW_FIELDS[group]
        found: dict[int, dict[str, str]] = {}
        for name, raw in self.form.items():
            match = _ROW_KEY.fullmatch(name)
            if (
                match is None
                or match[1] != prefix
                or match[3] not in columns
                or not isinstance(raw, str)
            ):
                continue
            found.setdefault(int(match[2]), {})[match[3]] = raw
        skipped = ROW_DEFAULTED.get(group, ())
        rows: list[tuple[int, dict[str, str]]] = []
        for index in sorted(found):
            values = {column: found[index].get(column, "") for column in columns}
            if all(not values[c].strip() for c in columns if c not in skipped):
                continue
            rows.append((index, values))
        cap = ROW_CAPS[group]
        if len(rows) > cap:
            self.fail(group, f"At most {cap} {GROUP_NOUNS[group]} are allowed.")
            rows = rows[:cap]
        return rows

    def _withheld(self, key: str, *, label: bool = False) -> bool:
        if self.creating:
            return False
        if not self.administrator:
            return True
        return not label and DEFAULT_SCRUBBER.is_sensitive(key)

    def _env(self) -> tuple[EnvRow, ...]:
        rows: list[EnvRow] = []
        seen: set[str] = set()
        total = 0
        for index, values in self._rows("env"):
            field_name = f"env-{index}-key"
            key, value = values["key"], values["value"]
            if not key:
                self.fail(field_name, "Variable name is required.")
                continue
            if len(key) > 255 or "=" in key or "\0" in key:
                self.fail(
                    field_name, "Variable name must be at most 255 characters without = or NUL."
                )
                continue
            if "\0" in value:
                self.fail(f"env-{index}-value", "Variable value must not contain NUL.")
                continue
            if key in seen:
                self.fail(field_name, "Variable names must be unique.")
            seen.add(key)
            total += len(key) + len(value)
            keep = value == "" and self._withheld(key)
            rows.append(EnvRow(index, key, None if keep else value))
        if total > MAX_ENV_CHARS:
            self.fail("env", "Environment variables must total at most 64 KiB.")
        return tuple(rows)

    def _labels(self) -> tuple[LabelRow, ...]:
        rows: list[LabelRow] = []
        seen: set[str] = set()
        total = 0
        for index, values in self._rows("labels"):
            field_name = f"label-{index}-key"
            key, value = values["key"], values["value"]
            if not key.strip():
                self.fail(field_name, "Label key is required.")
                continue
            if key.strip().casefold() in _RESERVED_FOLDED:
                self.fail(field_name, "This label is set by the product and cannot be supplied.")
                continue
            if len(key) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in key):
                self.fail(
                    field_name,
                    "Label key must be at most 255 characters without control characters.",
                )
                continue
            if key in seen:
                self.fail(field_name, "Label keys must be unique.")
            seen.add(key)
            total += len(key) + len(value)
            keep = value == "" and self._withheld(key, label=True)
            rows.append(LabelRow(index, key, None if keep else value))
        if total > MAX_LABEL_CHARS:
            self.fail("labels", "Labels must total at most 64 KiB.")
        return tuple(rows)

    def _networks(self) -> tuple[NetworkRow, ...]:
        rows: list[NetworkRow] = []
        seen: set[str] = set()
        for index, values in self._rows("networks"):
            name = values["name"].strip()
            if _NETWORK_NAME.fullmatch(name) is None:
                self.fail(
                    f"network-{index}-name",
                    "Network name must use letters, digits, underscores, periods and hyphens.",
                )
                continue
            if name in seen:
                self.fail(f"network-{index}-name", "Network names must be unique.")
            seen.add(name)
            aliases = tuple(part for part in re.split(r"[\s,]+", values["aliases"].strip()) if part)
            if len(aliases) > MAX_ALIASES:
                self.fail(f"network-{index}-aliases", f"At most {MAX_ALIASES} aliases are allowed.")
            elif any(DNS_LABEL.fullmatch(alias) is None for alias in aliases):
                self.fail(
                    f"network-{index}-aliases",
                    "Aliases must be DNS labels of letters, digits and hyphens.",
                )
            rows.append(NetworkRow(index, name, aliases))
        return tuple(rows)

    def _ports(self) -> tuple[PortRow, ...]:
        rows: list[PortRow] = []
        published_keys: set[tuple[int, str]] = set()
        endpoint = self.text("endpoint_mode")
        for index, values in self._rows("ports"):
            prefix = f"port-{index}"
            try:
                target = _bounded_int(values["target"], "Target port", 1, 65535)
            except ValueError as error:
                self.fail(f"{prefix}-target", str(error))
                continue
            published: int | None = None
            if values["published"].strip():
                try:
                    published = _bounded_int(values["published"], "Published port", 1, 65535)
                except ValueError as error:
                    self.fail(f"{prefix}-published", str(error))
                    continue
            protocol = values["protocol"].strip() or "tcp"
            mode = values["mode"].strip() or "ingress"
            if protocol not in _PROTOCOLS:
                self.fail(f"{prefix}-protocol", "Protocol must be tcp, udp or sctp.")
                continue
            if mode not in _PUBLISH_MODES:
                self.fail(f"{prefix}-mode", "Publish mode must be ingress or host.")
                continue
            if mode == "ingress" and endpoint == "dnsrr":
                self.fail(
                    f"{prefix}-mode",
                    "A port published in ingress mode needs the vip endpoint mode.",
                )
            if published is not None:
                if (published, protocol) in published_keys:
                    self.fail(
                        f"{prefix}-published", "This published port and protocol is used twice."
                    )
                published_keys.add((published, protocol))
            rows.append(PortRow(index, target, published, protocol, mode))
        return tuple(rows)

    def _mounts(self) -> tuple[MountRow, ...]:
        rows: list[MountRow] = []
        targets: set[str] = set()
        for index, values in self._rows("mounts"):
            prefix = f"mount-{index}"
            kind = values["type"].strip() or "volume"
            source, target = values["source"].strip(), values["target"].strip()
            try:
                if kind not in _MOUNT_TYPES:
                    raise _invalid("Mount type must be volume, bind or tmpfs.")
                if kind == "tmpfs" and source:
                    raise _invalid("A tmpfs mount takes no source.")
                if kind == "bind":
                    _absolute_path(source, "Bind source")
                if kind == "volume" and source and RESOURCE_NAME.fullmatch(source) is None:
                    raise _invalid(
                        "Volume source must be a volume name, or empty for an anonymous volume."
                    )
            except ValueError as error:
                self.fail(
                    f"{prefix}-type" if kind not in _MOUNT_TYPES else f"{prefix}-source", str(error)
                )
                continue
            try:
                _absolute_path(target, "Mount target")
                if target == "/":
                    raise _invalid("Mount target must not be /.")
            except ValueError as error:
                self.fail(f"{prefix}-target", str(error))
                continue
            if target in targets:
                self.fail(f"{prefix}-target", "Mount targets must be unique.")
            targets.add(target)
            size: int | None = None
            if values["tmpfs_size"].strip():
                try:
                    if kind != "tmpfs":
                        raise _invalid("Tmpfs size applies only to a tmpfs mount.")
                    size = parse_memory(values["tmpfs_size"], "Tmpfs size")
                    if size < 1:
                        raise _invalid("Tmpfs size must be greater than zero.")
                except ValueError as error:
                    self.fail(f"{prefix}-tmpfs_size", str(error))
                    continue
            rows.append(
                MountRow(index, kind, source, target, _is_checked(values["read_only"]), size)
            )
        return tuple(rows)

    def _files(self, group: str) -> list[tuple[int, str, str, str, str, int]]:
        prefix_name = ROW_PREFIX[group]
        configs = group == "configs"
        rows: list[tuple[int, str, str, str, str, int]] = []
        targets: set[str] = set()
        for index, values in self._rows(group):
            prefix = f"{prefix_name}-{index}"
            name = values["name"].strip()
            if OBJECT_NAME.fullmatch(name) is None:
                self.fail(
                    f"{prefix}-name",
                    "Name must be 1 to 64 letters, digits, hyphens, underscores and periods, "
                    "starting and ending with a letter or digit.",
                )
                continue
            target = values["target"].strip() or (f"/{name}" if configs else name)
            if (
                len(target) > 4096
                or "\0" in target
                or any(part == ".." for part in target.split("/"))
            ):
                self.fail(f"{prefix}-target", "Target must be a file path without .. segments.")
                continue
            uid = values["uid"].strip() or "0"
            gid = values["gid"].strip() or "0"
            if _FILE_ID.fullmatch(uid) is None:
                self.fail(f"{prefix}-uid", "User id must be a number.")
                continue
            if _FILE_ID.fullmatch(gid) is None:
                self.fail(f"{prefix}-gid", "Group id must be a number.")
                continue
            mode_text = values["mode"].strip() or "0444"
            if _FILE_MODE.fullmatch(mode_text) is None:
                self.fail(f"{prefix}-mode", "Mode must be octal digits, such as 0444.")
                continue
            if target in targets:
                self.fail(f"{prefix}-target", "File targets must be unique.")
            targets.add(target)
            rows.append((index, name, target, uid, gid, int(mode_text, 8)))
        return rows


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _child(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def _entries(value: Any) -> list[Mapping[str, Any]]:
    return (
        [entry for entry in value if isinstance(entry, Mapping)] if isinstance(value, list) else []
    )


def _put(node: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        node.pop(key, None)
    else:
        node[key] = value


def _tidy(parent: dict[str, Any], key: str) -> None:
    if key in parent and parent[key] == {}:
        del parent[key]


def _is_reserved(key: str) -> bool:
    return key.strip().casefold() in _RESERVED_FOLDED


def build_spec(service: ServiceInput, refs: ResolvedRefs) -> dict[str, Any]:
    """Return the ServiceSpec for a new service, with exactly the keys the form manages."""
    spec: dict[str, Any] = {"Name": service.name}
    return _apply(spec, service, refs, current=None)


def merge_spec(
    current: Mapping[str, Any], service: ServiceInput, refs: ResolvedRefs
) -> dict[str, Any]:
    """Return a copy of the current spec with only the fields the form manages replaced."""
    return _apply(_plain(dict(current)), service, refs, current=current)


def _apply(
    spec: dict[str, Any],
    service: ServiceInput,
    refs: ResolvedRefs,
    *,
    current: Mapping[str, Any] | None,
) -> dict[str, Any]:
    old = _map(current)
    old_task = _map(old.get("TaskTemplate"))
    old_container = _map(old_task.get("ContainerSpec"))
    task = _child(spec, "TaskTemplate")
    container = _child(task, "ContainerSpec")

    container["Image"] = refs.image
    _put(container, "Args", list(service.command) or None)
    _put(container, "Env", _env_entries(service, old_container) or None)
    container_mounts = _mount_entries(service, old_container)
    _put(container, "Mounts", container_mounts or None)
    _put(container, "Configs", _file_entries(service, refs, old_container, "Config") or None)
    _put(container, "Secrets", _file_entries(service, refs, old_container, "Secret") or None)

    labels = _labels(service, _map(old.get("Labels")))
    _put(spec, "Labels", labels or None)

    _write_mode(spec, service, old.get("Mode"))
    _write_resources(task, service.resources)
    _write_restart(task, service.restart)
    _write_placement(task, service, refs)
    _put(task, "Networks", _network_entries(service, refs, old_task) or None)
    _write_rollout(spec, "UpdateConfig", service.update)
    _write_rollout(spec, "RollbackConfig", service.rollback)
    _write_endpoint(spec, service, old)
    write_task(task, container, old_task, old_container, service.task)

    check_labels(spec, current)
    return spec


def _unresolved(name: str) -> ValidationFailed:
    return ValidationFailed({name: "This reference could not be resolved."})


def _env_entries(service: ServiceInput, old: Mapping[str, Any]) -> list[str]:
    existing: dict[str, str] = {}
    raw = old.get("Env")
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, str):
            existing.setdefault(entry.partition("=")[0], entry)
    entries: list[str] = []
    for row in service.env:
        held = existing.get(row.key)
        if row.value is None:
            entries.append(held if held is not None else f"{row.key}=")
        elif row.value == "" and held == row.key:
            entries.append(row.key)
        else:
            entries.append(f"{row.key}={row.value}")
    return entries


def _labels(service: ServiceInput, old: Mapping[str, Any]) -> dict[str, str]:
    labels = {key: value for key, value in old.items() if _is_reserved(key)}
    for row in service.labels:
        if row.value is None:
            value = old.get(row.key, "")
            labels[row.key] = value if isinstance(value, str) else ""
        else:
            labels[row.key] = row.value
    return labels


def _mount_entries(service: ServiceInput, old: Mapping[str, Any]) -> list[dict[str, Any]]:
    held = _entries(old.get("Mounts"))
    matched: dict[int, dict[str, Any]] = {}
    added: list[dict[str, Any]] = []
    for row in service.mounts:
        position = next(
            (
                index
                for index, mount in enumerate(held)
                if mount.get("Target") == row.target and mount.get("Type") == row.type
            ),
            None,
        )
        entry: dict[str, Any] = _plain(dict(held[position])) if position is not None else {}
        entry["Type"] = row.type
        entry["Target"] = row.target
        _put(entry, "Source", row.source or None)
        _put(entry, "ReadOnly", True if row.read_only else None)
        if row.type == "tmpfs":
            options = _child(entry, "TmpfsOptions")
            _put(options, "SizeBytes", row.tmpfs_size)
            _tidy(entry, "TmpfsOptions")
        if position is None:
            added.append(entry)
        else:
            matched[position] = entry
    entries: list[dict[str, Any]] = []
    for index, mount in enumerate(held):
        if mount.get("Type") not in _MODELLED_MOUNT_TYPES:
            entries.append(_plain(dict(mount)))
        elif index in matched:
            entries.append(matched[index])
    return entries + added


def _file_entries(
    service: ServiceInput, refs: ResolvedRefs, old: Mapping[str, Any], kind: str
) -> list[dict[str, Any]]:
    configs = kind == "Config"
    rows = service.configs if configs else service.secrets
    resolved = refs.configs if configs else refs.secrets
    modelled: list[dict[str, Any]] = []
    for row in rows:
        if row.slot not in resolved:
            raise _unresolved(f"{kind.lower()}-{row.slot}-name")
        object_id, object_name = resolved[row.slot]
        modelled.append(
            {
                "File": {"Name": row.target, "UID": row.uid, "GID": row.gid, "Mode": row.mode},
                f"{kind}ID": object_id,
                f"{kind}Name": object_name,
            }
        )
    if not configs:
        return modelled
    pending = iter(modelled)
    entries: list[dict[str, Any]] = []
    for entry in _entries(old.get("Configs")):
        if "File" not in entry or entry.get("Runtime") is not None:
            entries.append(_plain(dict(entry)))
        elif (replacement := next(pending, None)) is not None:
            entries.append(replacement)
    entries.extend(pending)
    return entries


def _network_entries(
    service: ServiceInput, refs: ResolvedRefs, old: Mapping[str, Any]
) -> list[dict[str, Any]]:
    held = _entries(old.get("Networks"))
    entries: list[dict[str, Any]] = []
    for row in service.networks:
        if row.slot not in refs.networks:
            raise _unresolved(f"network-{row.slot}-name")
        target = refs.networks[row.slot]
        carried = next((n for n in held if n.get("Target") == target), None)
        entry: dict[str, Any] = _plain(dict(carried)) if carried is not None else {}
        entry["Target"] = target
        _put(entry, "Aliases", list(row.aliases) or None)
        entries.append(entry)
    return entries


def _write_mode(spec: dict[str, Any], service: ServiceInput, old: Any) -> None:
    if isinstance(old, Mapping) and ("ReplicatedJob" in old or "GlobalJob" in old):
        return
    if service.mode == "global":
        spec["Mode"] = {"Global": {}}
    else:
        spec["Mode"] = {
            "Replicated": {"Replicas": 1 if service.replicas is None else service.replicas}
        }


def _write_resources(task: dict[str, Any], resources: ResourceInput) -> None:
    node = _child(task, "Resources")
    limits = _child(node, "Limits")
    _put(limits, "NanoCPUs", resources.cpu_limit)
    _put(limits, "MemoryBytes", resources.memory_limit)
    _put(limits, "Pids", resources.pids_limit)
    reservations = _child(node, "Reservations")
    _put(reservations, "NanoCPUs", resources.cpu_reservation)
    _put(reservations, "MemoryBytes", resources.memory_reservation)
    _tidy(node, "Limits")
    _tidy(node, "Reservations")
    _tidy(task, "Resources")


def _write_restart(task: dict[str, Any], restart: RestartInput) -> None:
    node = _child(task, "RestartPolicy")
    _put(node, "Condition", restart.condition)
    _put(node, "Delay", restart.delay)
    _put(node, "MaxAttempts", restart.max_attempts)
    _put(node, "Window", restart.window)
    _tidy(task, "RestartPolicy")


def _write_placement(task: dict[str, Any], service: ServiceInput, refs: ResolvedRefs) -> None:
    node = _child(task, "Placement")
    constraints = list(service.constraints)
    if service.run_on_manager:
        if refs.manager_node_id is None:
            raise ValidationFailed(
                {"run_on_manager": "The manager node of this environment could not be determined."}
            )
        probe = {"TaskTemplate": {"Placement": {"Constraints": constraints}}}
        if not pinned_to(probe, refs.manager_node_id):
            constraints.append(f"node.id=={refs.manager_node_id}")
    _put(node, "Constraints", constraints or None)
    _put(
        node,
        "Preferences",
        [{"Spread": {"SpreadDescriptor": line.partition("=")[2]}} for line in service.preferences]
        or None,
    )
    _put(node, "MaxReplicas", service.max_replicas_per_node or None)
    _tidy(task, "Placement")


def _write_rollout(spec: dict[str, Any], key: str, rollout: RolloutInput) -> None:
    node = _child(spec, key)
    _put(node, "Parallelism", rollout.parallelism)
    _put(node, "Delay", rollout.delay)
    _put(node, "FailureAction", rollout.failure_action)
    _put(node, "Monitor", rollout.monitor)
    _put(node, "MaxFailureRatio", rollout.max_failure_ratio)
    _put(node, "Order", rollout.order)
    _tidy(spec, key)


def _write_endpoint(spec: dict[str, Any], service: ServiceInput, old: Mapping[str, Any]) -> None:
    node = _child(spec, "EndpointSpec")
    held = _entries(_map(old.get("EndpointSpec")).get("Ports"))
    ports: list[dict[str, Any]] = []
    for row in service.ports:
        carried = next(
            (
                p
                for p in held
                if p.get("TargetPort") == row.target
                and (p.get("Protocol") or "tcp") == row.protocol
            ),
            None,
        )
        entry: dict[str, Any] = _plain(dict(carried)) if carried is not None else {}
        entry["TargetPort"] = row.target
        entry["Protocol"] = row.protocol
        entry["PublishMode"] = row.mode
        _put(entry, "PublishedPort", row.published)
        ports.append(entry)
    _put(node, "Mode", service.endpoint_mode)
    _put(node, "Ports", ports or None)
    _tidy(spec, "EndpointSpec")


def _label_sets(spec: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    container = _map(_map(spec.get("TaskTemplate")).get("ContainerSpec"))
    mounts = [
        _map(_map(mount).get("VolumeOptions")).get("Labels")
        for mount in _list(container.get("Mounts"))
    ]
    return {
        "service": [_map(spec.get("Labels"))],
        "container": [_map(container.get("Labels"))],
        "mount": [_map(labels) for labels in mounts],
    }


def check_labels(spec: Mapping[str, Any], baseline: Mapping[str, Any] | None = None) -> None:
    """Refuse reserved labels not already in the baseline, and labels over the 64 KiB total.

    Covers the service labels, the container labels and every mount's volume labels. Raises
    ValidationFailed keyed `labels`.
    """
    held = {
        kind: {(key, value) for labels in groups for key, value in labels.items()}
        for kind, groups in _label_sets(baseline or {}).items()
    }
    added: dict[str, str] = {}
    total = 0
    for kind, groups in _label_sets(spec).items():
        for labels in groups:
            for key, value in labels.items():
                total += (len(key) if isinstance(key, str) else 0) + (
                    len(value) if isinstance(value, str) else 0
                )
                if isinstance(key, str) and (key, value) not in held.get(kind, set()):
                    added[key] = value if isinstance(value, str) else ""
    SubmittedLabels(added)
    if total > MAX_LABEL_CHARS:
        raise ValidationFailed({"labels": "Labels must total at most 64 KiB."})


def mode_of(spec: Mapping[str, Any]) -> Literal["replicated", "global", "job"] | None:
    """Return the scheduling mode of a spec, or None when it names none."""
    mode = _map(spec.get("Mode"))
    if "ReplicatedJob" in mode or "GlobalJob" in mode:
        return "job"
    if "Replicated" in mode:
        return "replicated"
    if "Global" in mode:
        return "global"
    return None


def replicas_of(spec: Mapping[str, Any]) -> int | None:
    """Return the replica count of a replicated service, None for any other mode."""
    mode = _map(spec.get("Mode"))
    if "Replicated" not in mode or "ReplicatedJob" in mode or "GlobalJob" in mode:
        return None
    replicas = _map(mode.get("Replicated")).get("Replicas")
    if isinstance(replicas, int) and not isinstance(replicas, bool) and replicas >= 0:
        return replicas
    return 1


def scaled(current: Mapping[str, Any], replicas: int) -> dict[str, Any]:
    """Return a copy of the spec with the replica count changed; ValueError unless replicated."""
    if replicas_of(current) is None:
        raise ValueError("The service is not in replicated mode.")
    if not 0 <= replicas <= MAX_REPLICAS:
        raise ValueError(f"Replicas must be between 0 and {MAX_REPLICAS}.")
    spec: dict[str, Any] = _plain(dict(current))
    _child(_child(spec, "Mode"), "Replicated")["Replicas"] = replicas
    return spec


def force_counter(spec: Mapping[str, Any]) -> int:
    """Return the spec's ForceUpdate counter, 0 when absent."""
    value = _map(spec.get("TaskTemplate")).get("ForceUpdate")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def forced(current: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of the spec with the ForceUpdate counter incremented."""
    spec: dict[str, Any] = _plain(dict(current))
    task = _child(spec, "TaskTemplate")
    task["ForceUpdate"] = min(force_counter(current) + 1, MAX_FORCE_UPDATE)
    return spec


def image_of(spec: Mapping[str, Any]) -> str | None:
    """Return the image reference of a spec, None when it names none."""
    image = _map(_map(spec.get("TaskTemplate")).get("ContainerSpec")).get("Image")
    return image if isinstance(image, str) and image else None


def _strip(value: Any) -> Any:
    if isinstance(value, Mapping):
        stripped = {key: _strip(item) for key, item in value.items()}
        return {key: item for key, item in stripped.items() if item is not None} or None
    if isinstance(value, list | tuple):
        return [item for item in (_strip(item) for item in value) if item is not None] or None
    if value is None or value is False or value == "":
        return None
    return value


def _project(spec: Mapping[str, Any]) -> dict[str, Any]:
    task = _map(spec.get("TaskTemplate"))
    container = _map(task.get("ContainerSpec"))
    placement = _map(task.get("Placement"))
    endpoint = _map(spec.get("EndpointSpec"))
    mode = _map(spec.get("Mode"))
    mode_view: Any
    if "ReplicatedJob" in mode or "GlobalJob" in mode:
        mode_view = ("job", _strip(mode))
    elif "Replicated" in mode:
        mode_view = ("replicated", replicas_of(spec))
    elif "Global" in mode:
        mode_view = ("global",)
    else:
        mode_view = None
    preferences = [
        _map(_map(entry).get("Spread")).get("SpreadDescriptor")
        for entry in _list(placement.get("Preferences"))
    ]
    ports = [
        {
            **{key: value for key, value in port.items() if key != "Name"},
            "Protocol": port.get("Protocol") or "tcp",
            "PublishMode": port.get("PublishMode") or "ingress",
        }
        for port in _entries(endpoint.get("Ports"))
    ]
    env = container.get("Env")
    return {
        **project_task(task, container),
        "image": container.get("Image"),
        "mode": mode_view,
        "command": [container.get("Command"), container.get("Args")],
        "environment": sorted(
            item for item in (env if isinstance(env, list) else []) if isinstance(item, str)
        ),
        "labels": spec.get("Labels"),
        "placement": [
            placement.get("Constraints"),
            preferences,
            placement.get("MaxReplicas"),
        ],
        "resources": task.get("Resources"),
        "restart_policy": task.get("RestartPolicy"),
        "update_config": spec.get("UpdateConfig"),
        "rollback_config": spec.get("RollbackConfig"),
        "networks": task.get("Networks") or spec.get("Networks"),
        "ports": [endpoint.get("Mode") or "vip", ports],
        "mounts": sorted(
            (_strip(mount) for mount in _entries(container.get("Mounts"))),
            key=lambda mount: str((mount or {}).get("Target", "")),
        ),
        "configs": _sorted_refs(container.get("Configs")),
        "secrets": _sorted_refs(container.get("Secrets")),
        "other": _remainder(spec),
    }


_MANAGED_TOP: Final = ("Name", "Labels", "Mode", "UpdateConfig", "RollbackConfig", "EndpointSpec")
_MANAGED_TASK: Final = ("Resources", "RestartPolicy", "Networks", *MANAGED_TASK_KEYS)
_MANAGED_PLACEMENT: Final = ("Constraints", "Preferences", "MaxReplicas")
_MANAGED_CONTAINER: Final = (
    "Image",
    "Command",
    "Args",
    "Env",
    "Mounts",
    "Configs",
    "Secrets",
    *MANAGED_CONTAINER_KEYS,
)


def _remainder(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Return the spec without the fields the form manages and without Engine-added defaults."""
    rest: dict[str, Any] = _plain(dict(spec))
    for key in (*_MANAGED_TOP, "Networks"):
        rest.pop(key, None)
    task = rest.get("TaskTemplate")
    if not isinstance(task, dict):
        return rest
    for key in _MANAGED_TASK:
        task.pop(key, None)
    if task.get("Runtime") == "container":
        del task["Runtime"]
    if task.get("ForceUpdate") == 0:
        del task["ForceUpdate"]
    placement = task.get("Placement")
    if isinstance(placement, dict):
        for key in _MANAGED_PLACEMENT:
            placement.pop(key, None)
    container = task.get("ContainerSpec")
    if isinstance(container, dict):
        for key in _MANAGED_CONTAINER:
            container.pop(key, None)
        strip_managed_privileges(container)
        if container.get("Isolation") == "default":
            del container["Isolation"]
    return rest


def _sorted_refs(value: Any) -> list[Any]:
    return sorted(
        (_strip(entry) for entry in _entries(value)),
        key=lambda entry: str(sorted((entry or {}).items(), key=str)),
    )


def changed_categories(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the form categories whose managed fields differ, in the fixed vocabulary order.

    Keys the form does not manage are compared together as `other`, ignoring the defaults the
    Engine adds; an absent value equals an empty one.
    """
    old, new = _project(before), _project(after)
    return tuple(name for name in CATEGORIES if _strip(old[name]) != _strip(new[name]))


def form_values(spec: Mapping[str, Any], *, administrator: bool) -> dict[str, str]:
    """Return the edit form's field values for a spec, with withheld values left empty.

    A standard user is shown environment and label names only; an administrator is shown every
    value except sensitive-named environment variables. Reserved labels are omitted. Mounts of a
    type the form does not model are omitted and survive an update unchanged.
    """
    values: dict[str, str] = {}

    def put(name: str, value: Any) -> None:
        if value is None or value is False or value == "":
            return
        values[name] = str(value)

    task = _map(spec.get("TaskTemplate"))
    container = _map(task.get("ContainerSpec"))
    put("name", spec.get("Name"))
    put("image", container.get("Image"))
    mode = mode_of(spec)
    if mode in ("replicated", "global"):
        put("mode", mode)
    if mode == "replicated":
        put("replicas", replicas_of(spec))
    args = container.get("Args")
    if isinstance(args, list) and all(isinstance(item, str) for item in args):
        put("command", shlex.join(args))
    _env_values(put, container, administrator)
    _label_values(put, _map(spec.get("Labels")), administrator)
    placement = _map(task.get("Placement"))
    constraints = [c for c in _list(placement.get("Constraints")) if isinstance(c, str)]
    put("constraints", "\n".join(constraints))
    preferences = [
        f"spread={_map(_map(p).get('Spread')).get('SpreadDescriptor')}"
        for p in _list(placement.get("Preferences"))
        if isinstance(_map(_map(p).get("Spread")).get("SpreadDescriptor"), str)
    ]
    put("preferences", "\n".join(preferences))
    put("max_replicas_per_node", _number(placement.get("MaxReplicas")))
    _resource_values(put, _map(task.get("Resources")))
    restart = _map(task.get("RestartPolicy"))
    put("restart_condition", restart.get("Condition"))
    put("restart_delay", _duration_text(restart.get("Delay")))
    put("restart_max_attempts", _number(restart.get("MaxAttempts")))
    put("restart_window", _duration_text(restart.get("Window")))
    for prefix, key in (("update", "UpdateConfig"), ("rollback", "RollbackConfig")):
        node = _map(spec.get(key))
        put(f"{prefix}_parallelism", _number(node.get("Parallelism")))
        put(f"{prefix}_delay", _duration_text(node.get("Delay")))
        put(f"{prefix}_failure_action", node.get("FailureAction"))
        put(f"{prefix}_monitor", _duration_text(node.get("Monitor")))
        ratio = node.get("MaxFailureRatio")
        if isinstance(ratio, int | float) and not isinstance(ratio, bool):
            put(f"{prefix}_max_failure_ratio", format(Decimal(str(ratio)).normalize(), "f"))
        put(f"{prefix}_order", node.get("Order"))
    endpoint = _map(spec.get("EndpointSpec"))
    put("endpoint_mode", endpoint.get("Mode"))
    _row_values(put, task, container, endpoint)
    task_values(put, task, container, administrator=administrator, duration_text=_duration_text)
    return values


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def format_exact_duration(nanoseconds: int) -> str:
    """Return nanoseconds as hours down to nanoseconds, losing nothing; `0s` for zero or less."""
    if nanoseconds <= 0:
        return "0s"
    remaining = nanoseconds
    parts: list[str] = []
    for unit, size in _DURATION_UNITS:
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts)


def _duration_text(value: Any) -> str | None:
    number = _number(value)
    return None if number is None or number < 0 else format_exact_duration(number)


def _resource_values(put: Callable[[str, Any], None], resources: Mapping[str, Any]) -> None:
    limits, reservations = _map(resources.get("Limits")), _map(resources.get("Reservations"))
    for name, source in (
        ("cpu_limit", limits.get("NanoCPUs")),
        ("cpu_reservation", reservations.get("NanoCPUs")),
    ):
        nano = _number(source)
        if nano:
            put(name, format(Decimal(nano).scaleb(-9).normalize(), "f"))
    for name, source in (
        ("memory_limit", limits.get("MemoryBytes")),
        ("memory_reservation", reservations.get("MemoryBytes")),
    ):
        size = _number(source)
        if size:
            put(name, format_memory(size))
    put("pids_limit", _number(limits.get("Pids")) or None)


def _env_values(put: Callable[[str, Any], None], container: Mapping[str, Any], admin: bool) -> None:
    for ordinal, entry in enumerate(_list(container.get("Env"))):
        if not isinstance(entry, str):
            continue
        key, _, value = entry.partition("=")
        put(f"env-{ordinal}-key", key)
        if admin and not DEFAULT_SCRUBBER.is_sensitive(key):
            put(f"env-{ordinal}-value", value)


def _label_values(put: Callable[[str, Any], None], labels: Mapping[str, Any], admin: bool) -> None:
    ordinal = 0
    for key, value in labels.items():
        if not isinstance(key, str) or not isinstance(value, str) or _is_reserved(key):
            continue
        put(f"label-{ordinal}-key", key)
        shown = admin or (
            not DEFAULT_SCRUBBER.is_sensitive_label(key)
            and DEFAULT_SCRUBBER.scrub_text(value) == value
        )
        if shown:
            put(f"label-{ordinal}-value", value)
        ordinal += 1


def _row_values(
    put: Callable[[str, Any], None],
    task: Mapping[str, Any],
    container: Mapping[str, Any],
    endpoint: Mapping[str, Any],
) -> None:
    for ordinal, network in enumerate(_list(task.get("Networks"))):
        entry = _map(network)
        put(f"network-{ordinal}-name", entry.get("Target"))
        aliases = [a for a in _list(entry.get("Aliases")) if isinstance(a, str)]
        put(f"network-{ordinal}-aliases", " ".join(aliases))
    for ordinal, port in enumerate(_list(endpoint.get("Ports"))):
        entry = _map(port)
        put(f"port-{ordinal}-target", _number(entry.get("TargetPort")))
        put(f"port-{ordinal}-published", _number(entry.get("PublishedPort")) or None)
        put(f"port-{ordinal}-protocol", entry.get("Protocol") or "tcp")
        put(f"port-{ordinal}-mode", entry.get("PublishMode") or "ingress")
    for ordinal, mount in enumerate(_list(container.get("Mounts"))):
        entry = _map(mount)
        if entry.get("Type") not in _MODELLED_MOUNT_TYPES:
            continue
        put(f"mount-{ordinal}-type", entry.get("Type"))
        put(f"mount-{ordinal}-source", entry.get("Source"))
        put(f"mount-{ordinal}-target", entry.get("Target"))
        put(f"mount-{ordinal}-read_only", "on" if entry.get("ReadOnly") is True else None)
        size = _number(_map(entry.get("TmpfsOptions")).get("SizeBytes"))
        put(f"mount-{ordinal}-tmpfs_size", format_memory(size) if size else None)
    for group, key, prefix in (
        ("Configs", "Config", "config"),
        ("Secrets", "Secret", "secret"),
    ):
        for ordinal, reference in enumerate(_list(container.get(group))):
            entry = _map(reference)
            file = _map(entry.get("File"))
            if not file:
                continue
            put(f"{prefix}-{ordinal}-name", entry.get(f"{key}Name"))
            put(f"{prefix}-{ordinal}-target", file.get("Name"))
            put(f"{prefix}-{ordinal}-uid", file.get("UID"))
            put(f"{prefix}-{ordinal}-gid", file.get("GID"))
            mode = _number(file.get("Mode"))
            put(f"{prefix}-{ordinal}-mode", format(mode, "04o") if mode is not None else None)
