"""Detection of options that reach host root, run over a final Engine create body."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.auth.errors import SelfProtected
from app.self_identity import SelfIdentity

OPTION_IDS: Final[tuple[str, ...]] = (
    "privileged",
    "cap_add",
    "bind_mount",
    "volume_driver",
    "host_backed_volume",
    "mount_type",
    "volumes_from",
    "network_host",
    "network_container",
    "pid_namespace",
    "ipc_namespace",
    "uts_namespace",
    "userns_mode",
    "cgroupns_mode",
    "cgroup",
    "cgroup_parent",
    "devices",
    "security_opt",
    "masked_paths",
    "sysctls",
    "runtime",
    "log_driver",
    "links",
    "oom_kill_disable",
    "oom_score_adj",
    "realtime_cpu",
    "blkio_devices",
    "storage_opt",
    "annotations",
    "isolation",
    "endpoint_driver_opts",
    "container_id_file",
    "swarm_volume_placement",
    "unknown_option",
)

# Per option id: phrase, Engine paths, form field templates. "[]" marks a list element.
_OPTIONS: Final[dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]]] = {
    "privileged": ("privileged mode", ("HostConfig.Privileged",), ("privileged",)),
    "cap_add": ("added capabilities", ("HostConfig.CapAdd",), ("cap_add",)),
    "bind_mount": (
        "host bind mounts",
        ("HostConfig.Binds", "HostConfig.Mounts[].Type"),
        ("mount-{i}-type",),
    ),
    "volume_driver": (
        "volume drivers and driver options",
        ("HostConfig.VolumeDriver", "HostConfig.Mounts[].VolumeOptions.DriverConfig"),
        ("mount-{i}-source",),
    ),
    "host_backed_volume": (
        "volumes backed by a host path or a plugin",
        ("HostConfig.Mounts[].Source", "HostConfig.Binds"),
        ("mount-{i}-source",),
    ),
    "mount_type": (
        "mount types other than volume and tmpfs",
        ("HostConfig.Mounts[].Type",),
        ("mount-{i}-type",),
    ),
    "volumes_from": ("volumes taken from other containers", ("HostConfig.VolumesFrom",), ()),
    "network_host": (
        "host networking",
        ("HostConfig.NetworkMode", "NetworkingConfig.EndpointsConfig"),
        ("network_mode", "network-{i}-name"),
    ),
    "network_container": (
        "sharing another container's network",
        ("HostConfig.NetworkMode",),
        (),
    ),
    "pid_namespace": ("a host or shared PID namespace", ("HostConfig.PidMode",), ("pid_host",)),
    "ipc_namespace": ("a host or shared IPC namespace", ("HostConfig.IpcMode",), ("ipc_host",)),
    "uts_namespace": ("a UTS namespace setting", ("HostConfig.UTSMode",), ()),
    "userns_mode": ("a user namespace setting", ("HostConfig.UsernsMode",), ()),
    "cgroupns_mode": ("the host cgroup namespace", ("HostConfig.CgroupnsMode",), ()),
    "cgroup": ("a cgroup setting", ("HostConfig.Cgroup",), ()),
    "cgroup_parent": ("a cgroup parent", ("HostConfig.CgroupParent",), ()),
    "devices": (
        "device mappings",
        ("HostConfig.Devices", "HostConfig.DeviceRequests", "HostConfig.DeviceCgroupRules"),
        ("device-{i}-host_path",),
    ),
    "security_opt": ("security options", ("HostConfig.SecurityOpt",), ()),
    "masked_paths": (
        "masked and read-only path overrides",
        ("HostConfig.MaskedPaths", "HostConfig.ReadonlyPaths"),
        (),
    ),
    "sysctls": ("kernel parameters", ("HostConfig.Sysctls",), ()),
    "runtime": ("a non-default runtime", ("HostConfig.Runtime",), ("runtime",)),
    "log_driver": (
        "log drivers and log options other than json-file and local",
        ("HostConfig.LogConfig",),
        ("log_driver", "logopt-{i}-key"),
    ),
    "links": (
        "container links",
        ("HostConfig.Links", "NetworkingConfig.EndpointsConfig[].Links"),
        (),
    ),
    "oom_kill_disable": ("disabling the OOM killer", ("HostConfig.OomKillDisable",), ()),
    "oom_score_adj": ("a negative OOM score adjustment", ("HostConfig.OomScoreAdj",), ()),
    "realtime_cpu": (
        "real-time CPU scheduling",
        ("HostConfig.CpuRealtimePeriod", "HostConfig.CpuRealtimeRuntime"),
        (),
    ),
    "blkio_devices": (
        "per-device block I/O limits",
        (
            "HostConfig.BlkioWeightDevice",
            "HostConfig.BlkioDeviceReadBps",
            "HostConfig.BlkioDeviceWriteBps",
            "HostConfig.BlkioDeviceReadIOps",
            "HostConfig.BlkioDeviceWriteIOps",
        ),
        (),
    ),
    "storage_opt": ("storage driver options", ("HostConfig.StorageOpt",), ()),
    "annotations": ("runtime annotations", ("HostConfig.Annotations",), ()),
    "isolation": ("an isolation mode", ("HostConfig.Isolation",), ()),
    "endpoint_driver_opts": (
        "network driver options",
        ("NetworkingConfig.EndpointsConfig[].DriverOpts",),
        (),
    ),
    "container_id_file": ("a container ID file", ("HostConfig.ContainerIDFile",), ()),
    "swarm_volume_placement": (
        "named volumes on a service that is not pinned to this manager",
        ("TaskTemplate.ContainerSpec.Mounts[].Source",),
        ("mount-{i}-source",),
    ),
    "unknown_option": ("options this product does not recognise", (), ()),
}

KNOWN_BODY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "Hostname",
        "Domainname",
        "User",
        "AttachStdin",
        "AttachStdout",
        "AttachStderr",
        "ExposedPorts",
        "Tty",
        "OpenStdin",
        "StdinOnce",
        "Env",
        "Cmd",
        "Healthcheck",
        "ArgsEscaped",
        "Image",
        "Volumes",
        "WorkingDir",
        "Entrypoint",
        "NetworkDisabled",
        "MacAddress",
        "OnBuild",
        "Labels",
        "StopSignal",
        "StopTimeout",
        "Shell",
        "HostConfig",
        "NetworkingConfig",
    }
)

KNOWN_HOST_CONFIG_KEYS: Final[frozenset[str]] = frozenset(
    {
        "Binds",
        "ContainerIDFile",
        "LogConfig",
        "NetworkMode",
        "PortBindings",
        "RestartPolicy",
        "AutoRemove",
        "VolumeDriver",
        "VolumesFrom",
        "Mounts",
        "ConsoleSize",
        "Annotations",
        "CapAdd",
        "CapDrop",
        "CgroupnsMode",
        "Dns",
        "DnsOptions",
        "DnsSearch",
        "ExtraHosts",
        "GroupAdd",
        "IpcMode",
        "Cgroup",
        "Links",
        "OomScoreAdj",
        "PidMode",
        "Privileged",
        "PublishAllPorts",
        "ReadonlyRootfs",
        "SecurityOpt",
        "StorageOpt",
        "Tmpfs",
        "UTSMode",
        "UsernsMode",
        "ShmSize",
        "Sysctls",
        "Runtime",
        "Isolation",
        "MaskedPaths",
        "ReadonlyPaths",
        "CpuShares",
        "Memory",
        "CgroupParent",
        "BlkioWeight",
        "BlkioWeightDevice",
        "BlkioDeviceReadBps",
        "BlkioDeviceWriteBps",
        "BlkioDeviceReadIOps",
        "BlkioDeviceWriteIOps",
        "CpuPeriod",
        "CpuQuota",
        "CpuRealtimePeriod",
        "CpuRealtimeRuntime",
        "CpusetCpus",
        "CpusetMems",
        "Devices",
        "DeviceCgroupRules",
        "DeviceRequests",
        "KernelMemory",
        "KernelMemoryTCP",
        "MemoryReservation",
        "MemorySwap",
        "MemorySwappiness",
        "NanoCpus",
        "OomKillDisable",
        "Init",
        "PidsLimit",
        "Ulimits",
        "CpuCount",
        "CpuPercent",
        "IOMaximumIOps",
        "IOMaximumBandwidth",
    }
)

KNOWN_ENDPOINT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "IPAMConfig",
        "Links",
        "MacAddress",
        "Aliases",
        "DriverOpts",
        "GwPriority",
        "NetworkID",
        "EndpointID",
        "Gateway",
        "IPAddress",
        "IPPrefixLen",
        "IPv6Gateway",
        "GlobalIPv6Address",
        "GlobalIPv6PrefixLen",
        "DNSNames",
    }
)

KNOWN_MOUNT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "Type",
        "Source",
        "Target",
        "ReadOnly",
        "Consistency",
        "BindOptions",
        "VolumeOptions",
        "ImageOptions",
        "TmpfsOptions",
        "ClusterOptions",
    }
)
KNOWN_VOLUME_OPTION_KEYS: Final[frozenset[str]] = frozenset(
    {"NoCopy", "Labels", "DriverConfig", "Subpath"}
)
KNOWN_TMPFS_OPTION_KEYS: Final[frozenset[str]] = frozenset({"SizeBytes", "Mode", "Options"})
KNOWN_LOG_CONFIG_KEYS: Final[frozenset[str]] = frozenset({"Type", "Config"})
KNOWN_NETWORKING_KEYS: Final[frozenset[str]] = frozenset({"EndpointsConfig"})

ALLOWED_LOG_DRIVERS: Final[frozenset[str]] = frozenset({"json-file", "local", "none"})
ALLOWED_LOG_OPTION_KEYS: Final[frozenset[str]] = frozenset(
    {"max-size", "max-file", "compress", "mode", "max-buffer-size"}
)
ALLOWED_SECURITY_OPTS: Final[frozenset[str]] = frozenset(
    {"no-new-privileges", "no-new-privileges:true"}
)
ALLOWED_IPC_MODES: Final[frozenset[str]] = frozenset({"", "private", "shareable", "none"})
ALLOWED_PID_MODES: Final[frozenset[str]] = frozenset({"", "private"})
ALLOWED_CGROUPNS_MODES: Final[frozenset[str]] = frozenset({"", "private"})

VOLUME_NAME: Final = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,254}")
_SAFE_KEY: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_JOIN_MODES: Final = ("PidMode", "IpcMode", "NetworkMode")
_CONTAINER_MODE: Final = "container:"
_PATH_OVERRIDES: Final = {key.casefold(): key for key in ("MaskedPaths", "ReadonlyPaths")}

# (option id, HostConfig keys that must be empty or zero), form field of the whole option.
_PLAIN_HOST_OPTIONS: Final[tuple[tuple[str, tuple[str, ...], str | None], ...]] = (
    ("privileged", ("Privileged",), "privileged"),
    ("cap_add", ("CapAdd",), "cap_add"),
    ("volume_driver", ("VolumeDriver",), None),
    ("volumes_from", ("VolumesFrom",), None),
    ("uts_namespace", ("UTSMode",), None),
    ("userns_mode", ("UsernsMode",), None),
    ("cgroup", ("Cgroup",), None),
    ("cgroup_parent", ("CgroupParent",), None),
    ("sysctls", ("Sysctls",), None),
    ("links", ("Links",), None),
    ("oom_kill_disable", ("OomKillDisable",), None),
    ("realtime_cpu", ("CpuRealtimePeriod", "CpuRealtimeRuntime"), None),
    (
        "blkio_devices",
        (
            "BlkioWeightDevice",
            "BlkioDeviceReadBps",
            "BlkioDeviceWriteBps",
            "BlkioDeviceReadIOps",
            "BlkioDeviceWriteIOps",
        ),
        None,
    ),
    ("storage_opt", ("StorageOpt",), None),
    ("annotations", ("Annotations",), None),
    ("container_id_file", ("ContainerIDFile",), None),
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One restricted use: the option id, the Engine path it was found at, and its form field."""

    option: str
    path: str
    field: str | None = None

    @property
    def key(self) -> str:
        """Return the form field when the form has one, otherwise the Engine path."""
        return self.field or self.path

    @property
    def label(self) -> str:
        """Return the option as a phrase, such as "host bind mounts"."""
        return _OPTIONS[self.option][0]

    @property
    def message(self) -> str:
        """Return the sentence shown beside the field."""
        if self.option == "unknown_option":
            return "This Docker option is not recognised, so only administrators may use it."
        return f"Only administrators may use {self.label}."


def option_label(option: str) -> str:
    """Return the phrase naming an option id, such as "added capabilities"."""
    return _OPTIONS[option][0]


def option_engine_paths(option: str) -> tuple[str, ...]:
    """Return the Engine paths an option id covers."""
    return _OPTIONS[option][1]


def option_form_fields(option: str) -> tuple[str, ...]:
    """Return the form field templates an option id is reported against."""
    return _OPTIONS[option][2]


def reach_finding(option: str, path: str, field: str | None = None) -> Finding:
    """Build a finding for a restriction that only a lookup can establish."""
    if option not in _OPTIONS:
        raise ValueError(f"Unknown restricted option id {option!r}.")
    return Finding(option, path, field)


def refused_options(findings: Sequence[Finding]) -> tuple[str, ...]:
    """Return the distinct option ids of the findings, in vocabulary order."""
    present = {finding.option for finding in findings}
    return tuple(option for option in OPTION_IDS if option in present)


def is_empty(value: Any) -> bool:
    """Report whether a value is absent, false, zero, or an empty string or collection."""
    if value is None or value is False:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return value == 0
    if isinstance(value, str | bytes | list | tuple | set | frozenset | dict):
        return len(value) == 0
    return False


def is_blank(value: Any) -> bool:
    """Report whether a value is empty, or holds nothing but empty values."""
    if isinstance(value, Mapping):
        return all(is_blank(item) for item in value.values())
    if isinstance(value, list | tuple | set | frozenset):
        return all(is_blank(item) for item in value)
    return is_empty(value)


def needs_default_runtime(body: Mapping[str, Any]) -> bool:
    """Report whether the body sets a runtime, which is judged against the daemon's default."""
    host = body.get("HostConfig")
    return isinstance(host, Mapping) and not is_empty(host.get("Runtime"))


def find_restricted(
    body: Mapping[str, Any],
    *,
    default_runtime: str | None = None,
    slots: Mapping[str, Sequence[int]] | None = None,
) -> list[Finding]:
    """Return every restricted use in an Engine create body, empty when there is none.

    slots maps a form row group to the form row index of each body element, so findings name the
    row the user typed; without it the position in the body stands in.
    """
    scan = _Scan(slots or {}, default_runtime)
    scan.body(body)
    return scan.findings


def require_no_self_reach(
    body: Mapping[str, Any], *, environment_id: int, identity: SelfIdentity
) -> None:
    """Raise SelfProtected when the body would join or mount what dockmin's container holds.

    Covers PidMode, IpcMode and NetworkMode of the form `container:<id or name>`, VolumesFrom,
    and a named volume that container has mounted, for every role.
    """
    home = next((h for h in identity.verified if h.environment_id == environment_id), None)
    container_id = identity.container_id
    host = body.get("HostConfig")
    if home is None or container_id is None or not isinstance(host, Mapping):
        return
    for key in _JOIN_MODES:
        mode = host.get(key)
        if isinstance(mode, str) and mode.startswith(_CONTAINER_MODE):
            reference = mode.removeprefix(_CONTAINER_MODE)
            if _names_container(reference, container_id, home.container_name):
                raise SelfProtected.joins()
    sources = host.get("VolumesFrom")
    for source in sources if isinstance(sources, list | tuple) else []:
        if isinstance(source, str) and _names_container(
            source.partition(":")[0], container_id, home.container_name
        ):
            raise SelfProtected.joins()
    for name in _named_volumes(host):
        if identity.protects_volume(environment_id, name):
            raise SelfProtected.mount(name)


def _names_container(reference: str, container_id: str, container_name: str) -> bool:
    reference = reference.removeprefix("/")
    if not reference:
        return False
    return reference == container_name.removeprefix("/") or container_id.startswith(reference)


def _named_volumes(host: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    mounts = host.get("Mounts")
    for mount in mounts if isinstance(mounts, list | tuple) else []:
        if isinstance(mount, Mapping) and mount.get("Type") == "volume":
            source = mount.get("Source")
            if isinstance(source, str) and VOLUME_NAME.fullmatch(source):
                names.append(source)
    binds = host.get("Binds")
    for entry in binds if isinstance(binds, list | tuple) else []:
        source = entry.split(":")[0] if isinstance(entry, str) else ""
        if VOLUME_NAME.fullmatch(source):
            names.append(source)
    return names


class _Scan:
    def __init__(self, slots: Mapping[str, Sequence[int]], default_runtime: str | None) -> None:
        self.findings: list[Finding] = []
        self._slots = slots
        self._default_runtime = default_runtime

    def add(self, option: str, path: str, field: str | None = None) -> None:
        self.findings.append(Finding(option, path, field))

    def slot(self, group: str, ordinal: int) -> int:
        rows = self._slots.get(group)
        return rows[ordinal] if rows is not None and ordinal < len(rows) else ordinal

    def unknown(self, path: str, key: Any, known: frozenset[str]) -> None:
        # The Engine's JSON decoder matches field names case-insensitively, so only an exact
        # match counts as known, whatever the value.
        if not isinstance(key, str) or key not in known:
            name = key if isinstance(key, str) and _SAFE_KEY.fullmatch(key) else "<key>"
            self.add("unknown_option", f"{path}.{name}" if path else name)

    def body(self, body: Mapping[str, Any]) -> None:
        for key, value in body.items():
            if key == "HostConfig":
                self._host_config(value)
            elif key == "NetworkingConfig":
                self._networking_config(value)
            else:
                self.unknown("", key, KNOWN_BODY_KEYS)

    def _host_config(self, host: Any) -> None:
        if is_empty(host):
            return
        if not isinstance(host, Mapping):
            self.add("unknown_option", "HostConfig")
            return
        for key in host:
            self.unknown("HostConfig", key, KNOWN_HOST_CONFIG_KEYS)
        for option, keys, field in _PLAIN_HOST_OPTIONS:
            for key in keys:
                if not is_empty(host.get(key)):
                    self.add(option, f"HostConfig.{key}", field)
        self._binds(host.get("Binds"))
        self._mounts(host.get("Mounts"))
        self._network_mode(host.get("NetworkMode"))
        self._namespaces(host)
        self._devices(host)
        self._security(host)
        self._log_config(host.get("LogConfig"))
        self._runtime(host.get("Runtime"))
        self._numeric(host)

    def _binds(self, binds: Any) -> None:
        if is_empty(binds):
            return
        if not isinstance(binds, list | tuple):
            self.add("bind_mount", "HostConfig.Binds")
            return
        for ordinal, entry in enumerate(binds):
            source = entry.split(":", 1)[0] if isinstance(entry, str) and ":" in entry else None
            if source is None or VOLUME_NAME.fullmatch(source) is None:
                self.add("bind_mount", f"HostConfig.Binds[{ordinal}]")

    def _mounts(self, mounts: Any) -> None:
        if is_empty(mounts):
            return
        if not isinstance(mounts, list | tuple):
            self.add("unknown_option", "HostConfig.Mounts")
            return
        for ordinal, mount in enumerate(mounts):
            row = self.slot("mounts", ordinal)
            base = f"HostConfig.Mounts[{ordinal}]"
            type_field, source_field = f"mount-{row}-type", f"mount-{row}-source"
            if not isinstance(mount, Mapping):
                self.add("mount_type", base, type_field)
                continue
            for key in mount:
                self.unknown(base, key, KNOWN_MOUNT_KEYS)
            mount_type = mount.get("Type")
            if isinstance(mount_type, str) and mount_type.strip().casefold() == "bind":
                self.add("bind_mount", f"{base}.Type", type_field)
            elif mount_type not in ("volume", "tmpfs"):
                self.add("mount_type", f"{base}.Type", type_field)
            elif mount_type == "volume":
                source = mount.get("Source")
                if not is_empty(source) and (
                    not isinstance(source, str) or VOLUME_NAME.fullmatch(source) is None
                ):
                    self.add("bind_mount", f"{base}.Source", source_field)
            self._mount_options(base, mount, source_field)

    def _mount_options(self, base: str, mount: Mapping[str, Any], source_field: str) -> None:
        volume = mount.get("VolumeOptions")
        if isinstance(volume, Mapping):
            for key in volume:
                self.unknown(f"{base}.VolumeOptions", key, KNOWN_VOLUME_OPTION_KEYS)
            if not is_empty(volume.get("DriverConfig")):
                self.add("volume_driver", f"{base}.VolumeOptions.DriverConfig", source_field)
        elif not is_empty(volume):
            self.add("unknown_option", f"{base}.VolumeOptions")
        tmpfs = mount.get("TmpfsOptions")
        if isinstance(tmpfs, Mapping):
            for key in tmpfs:
                self.unknown(f"{base}.TmpfsOptions", key, KNOWN_TMPFS_OPTION_KEYS)
        elif not is_empty(tmpfs):
            self.add("unknown_option", f"{base}.TmpfsOptions")

    def _network_mode(self, mode: Any) -> None:
        if is_empty(mode):
            return
        if not isinstance(mode, str):
            self.add("unknown_option", "HostConfig.NetworkMode")
            return
        folded = mode.strip().casefold()
        if folded == "host":
            self.add("network_host", "HostConfig.NetworkMode", "network_mode")
        elif folded.startswith("container:"):
            self.add("network_container", "HostConfig.NetworkMode")

    def _namespaces(self, host: Mapping[str, Any]) -> None:
        for key, option, allowed, field in (
            ("PidMode", "pid_namespace", ALLOWED_PID_MODES, "pid_host"),
            ("IpcMode", "ipc_namespace", ALLOWED_IPC_MODES, "ipc_host"),
            ("CgroupnsMode", "cgroupns_mode", ALLOWED_CGROUPNS_MODES, None),
        ):
            value = host.get(key)
            if value is not None and not (isinstance(value, str) and value in allowed):
                self.add(option, f"HostConfig.{key}", field)

    def _devices(self, host: Mapping[str, Any]) -> None:
        devices = host.get("Devices")
        if not is_empty(devices):
            count = len(devices) if isinstance(devices, list | tuple) else 1
            for ordinal in range(count):
                row = self.slot("devices", ordinal)
                self.add("devices", f"HostConfig.Devices[{ordinal}]", f"device-{row}-host_path")
        for key in ("DeviceRequests", "DeviceCgroupRules"):
            if not is_empty(host.get(key)):
                self.add("devices", f"HostConfig.{key}")

    def _security(self, host: Mapping[str, Any]) -> None:
        options = host.get("SecurityOpt")
        if not is_empty(options):
            if not isinstance(options, list | tuple):
                self.add("security_opt", "HostConfig.SecurityOpt")
            else:
                for ordinal, entry in enumerate(options):
                    if not (isinstance(entry, str) and entry in ALLOWED_SECURITY_OPTS):
                        self.add("security_opt", f"HostConfig.SecurityOpt[{ordinal}]")
        for key, value in host.items():
            canonical = _PATH_OVERRIDES.get(key.casefold()) if isinstance(key, str) else None
            if canonical is not None and value is not None:
                self.add("masked_paths", f"HostConfig.{canonical}")

    def _log_config(self, log: Any) -> None:
        if is_blank(log):
            return
        if not isinstance(log, Mapping):
            self.add("log_driver", "HostConfig.LogConfig", "log_driver")
            return
        for key in log:
            self.unknown("HostConfig.LogConfig", key, KNOWN_LOG_CONFIG_KEYS)
        driver = log.get("Type")
        if not is_empty(driver) and not (isinstance(driver, str) and driver in ALLOWED_LOG_DRIVERS):
            self.add("log_driver", "HostConfig.LogConfig.Type", "log_driver")
        options = log.get("Config")
        if isinstance(options, Mapping):
            for ordinal, key in enumerate(options):
                if not (isinstance(key, str) and key in ALLOWED_LOG_OPTION_KEYS):
                    row = self.slot("log_options", ordinal)
                    name = key if isinstance(key, str) and _SAFE_KEY.fullmatch(key) else "<key>"
                    self.add(
                        "log_driver",
                        f"HostConfig.LogConfig.Config[{name}]",
                        f"logopt-{row}-key",
                    )
        elif not is_empty(options):
            self.add("log_driver", "HostConfig.LogConfig.Config", "log_driver")

    def _runtime(self, runtime: Any) -> None:
        if is_empty(runtime):
            return
        if not (isinstance(runtime, str) and runtime == self._default_runtime):
            self.add("runtime", "HostConfig.Runtime", "runtime")

    def _numeric(self, host: Mapping[str, Any]) -> None:
        score = host.get("OomScoreAdj")
        if not is_empty(score) and not (
            isinstance(score, int) and not isinstance(score, bool) and score > 0
        ):
            self.add("oom_score_adj", "HostConfig.OomScoreAdj")
        isolation = host.get("Isolation")
        if not is_empty(isolation) and isolation != "default":
            self.add("isolation", "HostConfig.Isolation")

    def _networking_config(self, config: Any) -> None:
        if is_empty(config):
            return
        if not isinstance(config, Mapping):
            self.add("unknown_option", "NetworkingConfig")
            return
        for key in config:
            self.unknown("NetworkingConfig", key, KNOWN_NETWORKING_KEYS)
        endpoints = config.get("EndpointsConfig")
        if is_empty(endpoints):
            return
        if not isinstance(endpoints, Mapping):
            self.add("unknown_option", "NetworkingConfig.EndpointsConfig")
            return
        for ordinal, (name, endpoint) in enumerate(endpoints.items()):
            self._endpoint(ordinal, name, endpoint)

    def _endpoint(self, ordinal: int, name: Any, endpoint: Any) -> None:
        row = self.slot("networks", ordinal)
        label = name if isinstance(name, str) and _SAFE_KEY.fullmatch(name) else "<network>"
        base = f"NetworkingConfig.EndpointsConfig[{label}]"
        folded = name.strip().casefold() if isinstance(name, str) else ""
        if folded == "host":
            self.add("network_host", base, f"network-{row}-name")
        elif folded.startswith("container:"):
            self.add("network_container", base)
        if is_blank(endpoint):
            return
        if not isinstance(endpoint, Mapping):
            self.add("unknown_option", base)
            return
        for key in endpoint:
            self.unknown(base, key, KNOWN_ENDPOINT_KEYS)
        if not is_empty(endpoint.get("DriverOpts")):
            self.add("endpoint_driver_opts", f"{base}.DriverOpts")
        if not is_empty(endpoint.get("Links")):
            self.add("links", f"{base}.Links")
