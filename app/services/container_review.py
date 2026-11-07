"""Pure parts of container provisioning: the views, the review sections and the change summary."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.docker_engine.models import ContainerInspect, ImageInspect
from app.logging import DEFAULT_SCRUBBER
from app.services.container_derivation import Blocker, Notice
from app.services.container_restrictions import VOLUME_NAME
from app.services.container_spec import ContainerSpec, FormState, format_duration
from app.services.containers import short_id

CHANGE_CATEGORIES: Final[tuple[str, ...]] = (
    "image",
    "command",
    "entrypoint",
    "env",
    "labels",
    "ports",
    "mounts",
    "networks",
    "restart_policy",
    "resources",
    "healthcheck",
    "user",
    "working_dir",
    "log_config",
    "other",
)
UNREADABLE_CHANGES: Final = "unknown"
NO_CHANGES: Final = "none"
CONTROL_MISSING: Final = "missing"
OLD_REMOVED: Final = "removed"
OLD_KEPT: Final = "kept"
OLD_UNKNOWN: Final = "unknown"
CONTROL_MOVED: Final = "moved"
CONTROL_COPIED: Final = "copied"


@dataclass(frozen=True, slots=True)
class FormContext:
    """What the create form page renders: the environment, the values and what was not copied."""

    environment_id: int
    environment_name: str
    is_local: bool
    is_administrator: bool
    state: FormState
    notices: tuple[Notice, ...] = ()
    duplicate_of: str | None = None
    source_name: str | None = None
    suggested_name: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """One line of the review: a label and its values; hidden means the values are withheld."""

    label: str
    values: tuple[str, ...] = ()
    hidden: bool = False


@dataclass(frozen=True, slots=True)
class ReviewSection:
    """One titled group of review lines."""

    key: str
    title: str
    items: tuple[ReviewItem, ...]


@dataclass(frozen=True, slots=True)
class ReviewView:
    """The resolved configuration shown before creating.

    state carries every submitted value for the hidden inputs. restricted_used lists the
    administrator-only options an administrator's configuration uses.
    """

    environment_id: int
    environment_name: str
    is_local: bool
    image: str
    name: str | None
    will_pull: bool
    start: bool
    state: FormState
    sections: tuple[ReviewSection, ...]
    restricted_used: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CreateOutcome:
    """A container created; start_failed means Docker created but could not start it."""

    environment_id: int
    container_id: str
    name: str | None
    image: str
    pulled: bool
    started: bool
    start_failed: bool
    control_created: bool

    @property
    def short_id(self) -> str:
        """Return the 12-character form shown in the interface."""
        return short_id(self.container_id)


@dataclass(frozen=True, slots=True)
class RestrictedView:
    """An administrator-only option a container's configuration uses."""

    option: str
    label: str


@dataclass(frozen=True, slots=True)
class BlockerView:
    """A reason a container cannot be recreated."""

    code: Blocker
    message: str


@dataclass(frozen=True, slots=True)
class RecreateConfirmation:
    """Everything the recreate confirmation names before it runs.

    A page offers no submit button unless can_submit. pull_available is false when the container
    was created from a bare image id.
    """

    environment_id: int
    environment_name: str
    is_local: bool
    container_id: str
    short_id: str
    name: str
    image: str
    state: str
    was_running: bool
    named_volumes: tuple[str, ...]
    named_volumes_hidden: int
    anonymous_volume_count: int
    networks: tuple[str, ...]
    blockers: tuple[BlockerView, ...]
    restricted: tuple[RestrictedView, ...]
    pull_available: bool
    image_defaults_kept: bool

    @property
    def can_submit(self) -> bool:
        """Report whether the recreate may be submitted."""
        return not self.blockers and not self.restricted


@dataclass(frozen=True, slots=True)
class RecreateOutcome:
    """A container recreated.

    kept_as names the old container when it was not removed, remove_outcome_unknown when the
    removal timed out so it may be gone. control_moved means the new container carries the old
    one's access control; old_control_kept that the old container still has it too. verified is
    the result of the watch an automated recreate runs, None when none ran.
    """

    environment_id: int
    old_id: str
    new_id: str
    name: str
    image: str
    pulled: bool
    image_changed: bool
    changed: tuple[str, ...]
    started: bool
    old_removed: bool
    remove_outcome_unknown: bool
    kept_as: str | None
    control_moved: bool
    old_control_kept: bool
    verified: bool | None = None


def created_flags(outcome: CreateOutcome) -> dict[str, str]:
    """Return the values a create reports to the page that shows its result."""
    flag = "start_failed" if outcome.start_failed else "started" if outcome.started else "stopped"
    flags = {"created": flag}
    if not outcome.control_created:
        flags["control"] = CONTROL_MISSING
    return flags


def recreated_flags(outcome: RecreateOutcome) -> dict[str, str]:
    """Return the values a recreate reports to the page that shows its result."""
    if outcome.old_removed:
        old = OLD_REMOVED
    elif outcome.remove_outcome_unknown:
        old = OLD_UNKNOWN
    else:
        old = OLD_KEPT
    if not outcome.control_moved:
        control = CONTROL_MISSING
    elif outcome.old_control_kept:
        control = CONTROL_COPIED
    else:
        control = CONTROL_MOVED
    return {
        "recreated": "started" if outcome.started else "stopped",
        "changed": ",".join(outcome.changed),
        "pulled": "1" if outcome.pulled else "0",
        "old": old,
        "control": control,
    }


def container_document(inspected: ContainerInspect) -> dict[str, Any]:
    networks = {
        name: {
            "NetworkID": endpoint.network_id,
            "Aliases": list(endpoint.aliases),
            "IPAMConfig": endpoint.ipam_config,
            "Links": list(endpoint.links),
            "DriverOpts": dict(endpoint.driver_opts),
            "GwPriority": endpoint.gw_priority,
        }
        for name, endpoint in inspected.network_settings.networks.items()
    }
    return {
        "Id": inspected.id,
        "Name": inspected.name,
        "Image": inspected.image,
        "LabelsTruncated": inspected.labels_truncated,
        "Config": inspected.config_document,
        "HostConfig": inspected.host_config,
        "Mounts": [
            {
                "Type": mount.type,
                "Name": mount.name,
                "Source": mount.source,
                "Destination": mount.destination,
                "RW": mount.read_write,
            }
            for mount in inspected.mounts
        ],
        "NetworkSettings": {"Networks": networks},
    }


def image_document(image: ImageInspect | None) -> dict[str, Any] | None:
    return None if image is None else {"Config": image.config}


def as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _normal(value: Any) -> Any:
    if not value:
        return None
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return sorted(value)
    return value


_CONFIG_CATEGORIES: Final[Mapping[str, str]] = {
    "Cmd": "command",
    "Entrypoint": "entrypoint",
    "Env": "env",
    "Labels": "labels",
    "ExposedPorts": "ports",
    "User": "user",
    "WorkingDir": "working_dir",
    "Healthcheck": "healthcheck",
}
_CONFIG_IGNORED: Final = frozenset({"Hostname", "Image", "MacAddress"})
_RESOURCE_KEYS: Final = (
    "Memory",
    "MemorySwap",
    "MemoryReservation",
    "MemorySwappiness",
    "KernelMemory",
    "NanoCpus",
    "CpuShares",
    "CpuPeriod",
    "CpuQuota",
    "CpuRealtimePeriod",
    "CpuRealtimeRuntime",
    "CpusetCpus",
    "CpusetMems",
    "PidsLimit",
    "ShmSize",
    "BlkioWeight",
    "Ulimits",
    "OomKillDisable",
    "OomScoreAdj",
)
_HOST_CATEGORIES: Final[Mapping[str, str]] = {
    "PortBindings": "ports",
    "PublishAllPorts": "ports",
    "RestartPolicy": "restart_policy",
    "LogConfig": "log_config",
    **dict.fromkeys(_RESOURCE_KEYS, "resources"),
}
_HOST_IGNORED: Final = frozenset(
    {
        "ContainerIDFile",
        "NetworkMode",
        "Binds",
        "Mounts",
        "Tmpfs",
        "VolumeDriver",
        "VolumesFrom",
    }
)


def _mount_set(document: Mapping[str, Any]) -> frozenset[tuple[Any, ...]]:
    mounts = document.get("Mounts")
    entries = mounts if isinstance(mounts, list) else []
    return frozenset(
        (
            mount.get("Type"),
            mount.get("Name") or mount.get("Source"),
            mount.get("Destination"),
            mount.get("RW"),
        )
        for mount in entries
        if isinstance(mount, Mapping)
    )


def _network_set(document: Mapping[str, Any]) -> frozenset[tuple[Any, ...]]:
    short = str(document.get("Id", ""))[:12]
    networks = as_mapping(as_mapping(document.get("NetworkSettings")).get("Networks"))
    result: set[tuple[Any, ...]] = set()
    for name, endpoint in networks.items():
        details = as_mapping(endpoint)
        aliases = tuple(
            sorted(a for a in details.get("Aliases") or [] if isinstance(a, str) and a != short)
        )
        ipam = repr(sorted(as_mapping(details.get("IPAMConfig")).items()))
        result.add((name, aliases, ipam))
    return frozenset(result)


def _hostname_default(document: Mapping[str, Any], config: Mapping[str, Any]) -> bool:
    short = str(document.get("Id", ""))[:12]
    return not config.get("Hostname") or config.get("Hostname") == short


def changed_categories(old: Mapping[str, Any], new: Mapping[str, Any]) -> tuple[str, ...]:
    """Return which categories of configuration differ between two container documents.

    Only the category names come back; a value never leaves this function.
    """
    found: set[str] = set()
    old_config, new_config = as_mapping(old.get("Config")), as_mapping(new.get("Config"))
    old_host, new_host = as_mapping(old.get("HostConfig")), as_mapping(new.get("HostConfig"))
    if old.get("Image") != new.get("Image"):
        found.add("image")
    for key, category in _CONFIG_CATEGORIES.items():
        if _normal(old_config.get(key)) != _normal(new_config.get(key)):
            found.add(category)
    for key, category in _HOST_CATEGORIES.items():
        if _normal(old_host.get(key)) != _normal(new_host.get(key)):
            found.add(category)
    if _network_set(old) != _network_set(new) or old_host.get("NetworkMode") != new_host.get(
        "NetworkMode"
    ):
        found.add("networks")
    if _mount_set(old) != _mount_set(new) or _normal(old_host.get("Tmpfs")) != _normal(
        new_host.get("Tmpfs")
    ):
        found.add("mounts")
    if not (_hostname_default(old, old_config) and _hostname_default(new, new_config)) and (
        old_config.get("Hostname") != new_config.get("Hostname")
    ):
        found.add("other")
    handled_config = set(_CONFIG_CATEGORIES) | _CONFIG_IGNORED
    handled_host = set(_HOST_CATEGORIES) | _HOST_IGNORED
    for config_key in (old_config.keys() | new_config.keys()) - handled_config:
        if _normal(old_config.get(config_key)) != _normal(new_config.get(config_key)):
            found.add("other")
    for host_key in (old_host.keys() | new_host.keys()) - handled_host:
        if _normal(old_host.get(host_key)) != _normal(new_host.get(host_key)):
            found.add("other")
    ordered = tuple(category for category in CHANGE_CATEGORIES if category in found)
    return ordered or (NO_CHANGES,)


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if amount < 1024 or unit == "GiB":
            text = f"{amount:.0f}" if amount == int(amount) else f"{amount:.1f}"
            return f"{text} {unit}"
        amount /= 1024
    return f"{value} B"


def _scalar(label: str, value: object) -> ReviewItem | None:
    if value is None or value == "":
        return None
    return ReviewItem(label, (str(value),))


def _port_text(host_ip: str, host_port: str, container_port: str, protocol: str) -> str:
    host = f"{host_ip}:{host_port}" if host_ip and host_port else host_ip or host_port
    return f"{host or 'assigned'} → {container_port}/{protocol}"


def _mount_text(row: Any) -> str:
    mode = " (read-only)" if row.read_only else ""
    match row.type:
        case "tmpfs":
            size = f", size {_human_bytes(row.tmpfs_size)}" if row.tmpfs_size else ""
            return f"tmpfs → {row.target}{size}{mode}"
        case "bind":
            return f"bind {row.source} → {row.target}{mode}"
        case _:
            return f"volume {row.source or '(anonymous)'} → {row.target}{mode}"


def review_sections(spec: ContainerSpec) -> tuple[ReviewSection, ...]:
    """Describe a validated spec section by section, with sensitive variable values withheld."""
    sections: list[ReviewSection] = []

    def add(key: str, title: str, *items: ReviewItem | None) -> None:
        kept = tuple(item for item in items if item is not None)
        if kept:
            sections.append(ReviewSection(key, title, kept))

    add(
        "command",
        "Command",
        ReviewItem("Command", spec.command) if spec.command else None,
        ReviewItem("Entrypoint", spec.entrypoint) if spec.entrypoint else None,
        _scalar("Working directory", spec.working_dir),
        _scalar("User", spec.user),
    )
    add(
        "environment",
        "Environment",
        *(
            ReviewItem(row.key, hidden=True)
            if DEFAULT_SCRUBBER.is_sensitive(row.key)
            else ReviewItem(row.key, (row.value,))
            for row in spec.env
        ),
    )
    add("labels", "Labels", *(ReviewItem(row.key, (row.value,)) for row in spec.labels))
    add(
        "ports",
        "Ports",
        ReviewItem(
            "Published",
            tuple(
                _port_text(row.host_ip, row.host_port, row.container_port, row.protocol)
                for row in spec.ports
            ),
        )
        if spec.ports
        else None,
        ReviewItem("Publish all exposed ports", ("yes",)) if spec.publish_all else None,
    )
    add(
        "mounts",
        "Volumes and mounts",
        ReviewItem("Mounts", tuple(_mount_text(row) for row in spec.mounts))
        if spec.mounts
        else None,
    )
    add(
        "network",
        "Network",
        _scalar("Mode", spec.network_mode),
        *(
            ReviewItem(
                row.name,
                tuple(
                    part
                    for part in (
                        f"aliases {', '.join(row.aliases)}" if row.aliases else "",
                        f"IPv4 {row.ipv4}" if row.ipv4 else "",
                    )
                    if part
                ),
            )
            for row in spec.networks
        ),
    )
    policy = spec.restart_policy
    if policy is not None and policy == "on-failure" and spec.restart_max_retries is not None:
        policy = f"{policy}, at most {spec.restart_max_retries} retries"
    add("restart", "Restart policy", _scalar("Policy", policy))
    add(
        "resources",
        "Resources",
        _scalar("CPUs", None if spec.cpus is None else format(spec.cpus.normalize(), "f")),
        _scalar("CPU shares", spec.cpu_shares),
        _scalar("Memory", None if spec.memory is None else _human_bytes(spec.memory)),
        _scalar("Swap", _swap_text(spec.memory_swap)),
        _scalar("PIDs", spec.pids_limit),
        _scalar("Shared memory", None if spec.shm_size is None else _human_bytes(spec.shm_size)),
    )
    add("healthcheck", "Health check", *_health_items(spec))
    add(
        "logging",
        "Logging",
        _scalar("Driver", spec.log.driver),
        *(ReviewItem(row.key, (row.value,)) for row in spec.log.options),
    )
    add(
        "dns",
        "DNS and hosts",
        ReviewItem("DNS servers", spec.dns.servers) if spec.dns.servers else None,
        ReviewItem("Search domains", spec.dns.search) if spec.dns.search else None,
        ReviewItem("DNS options", spec.dns.options) if spec.dns.options else None,
        ReviewItem("Extra hosts", tuple(f"{row.name}:{row.address}" for row in spec.extra_hosts))
        if spec.extra_hosts
        else None,
    )
    add(
        "security",
        "Runtime and security",
        ReviewItem("Init process", ("yes",)) if spec.init else None,
        ReviewItem("Privileged", ("yes",)) if spec.privileged else None,
        ReviewItem("Capabilities dropped", spec.cap_drop) if spec.cap_drop else None,
        ReviewItem("Capabilities added", spec.cap_add) if spec.cap_add else None,
        ReviewItem(
            "Devices",
            tuple(
                f"{row.host_path} → {row.container_path} ({row.permissions})"
                for row in spec.devices
            ),
        )
        if spec.devices
        else None,
        _scalar("Runtime", spec.runtime),
        ReviewItem("Host PID namespace", ("yes",)) if spec.pid_host else None,
        ReviewItem("Host IPC namespace", ("yes",)) if spec.ipc_host else None,
    )
    return tuple(sections)


def _swap_text(value: int | None) -> str | None:
    if value is None:
        return None
    return "unlimited" if value == -1 else _human_bytes(value)


def _health_items(spec: ContainerSpec) -> tuple[ReviewItem | None, ...]:
    check = spec.healthcheck
    if check.mode == "image":
        return ()
    if check.mode == "none":
        return (ReviewItem("Health check", ("disabled",)),)
    durations = (
        ("Interval", check.interval),
        ("Timeout", check.timeout),
        ("Start period", check.start_period),
        ("Start interval", check.start_interval),
    )
    return (
        _scalar("Command", check.test.strip()),
        *(_scalar(label, format_duration(value)) for label, value in durations if value),
        _scalar("Retries", check.retries),
    )


def volume_uses(
    host: Mapping[str, Any], slots: Mapping[str, Sequence[int]] | None
) -> list[tuple[str, bool, str, str | None]]:
    uses: list[tuple[str, bool, str, str | None]] = []
    mounts = host.get("Mounts")
    for ordinal, mount in enumerate(mounts if isinstance(mounts, list | tuple) else []):
        if not isinstance(mount, Mapping) or mount.get("Type") != "volume":
            continue
        source = mount.get("Source")
        if not isinstance(source, str) or VOLUME_NAME.fullmatch(source) is None:
            continue
        rows = None if slots is None else slots.get("mounts")
        row = rows[ordinal] if rows is not None and ordinal < len(rows) else ordinal
        field_name = None if slots is None else f"mount-{row}-source"
        uses.append(
            (
                source,
                bool(mount.get("ReadOnly")),
                f"HostConfig.Mounts[{ordinal}].Source",
                field_name,
            )
        )
    binds = host.get("Binds")
    for ordinal, entry in enumerate(binds if isinstance(binds, list | tuple) else []):
        parts = entry.split(":") if isinstance(entry, str) else []
        if len(parts) >= 2 and VOLUME_NAME.fullmatch(parts[0]):
            options = parts[2].split(",") if len(parts) > 2 else []
            uses.append((parts[0], "ro" in options, f"HostConfig.Binds[{ordinal}]", None))
    return uses


def endpoint_keys(body: Mapping[str, Any]) -> list[str]:
    endpoints = as_mapping(as_mapping(body.get("NetworkingConfig")).get("EndpointsConfig"))
    return [key for key in endpoints if isinstance(key, str)]
