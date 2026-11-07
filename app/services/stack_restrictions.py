"""Standard-user restriction scan, product limits and masking over a resolved Compose model."""

import hashlib
import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol

from app.auth.errors import ValidationFailed
from app.db.repositories.stack import StackKind
from app.logging import REDACTED
from app.services.access import SubmittedLabels
from app.services.container_restrictions import (
    ALLOWED_CGROUPNS_MODES,
    ALLOWED_IPC_MODES,
    ALLOWED_LOG_DRIVERS,
    ALLOWED_LOG_OPTION_KEYS,
    ALLOWED_PID_MODES,
    ALLOWED_SECURITY_OPTS,
    OPTION_IDS,
    find_restricted,
    is_blank,
    is_empty,
    option_label,
)
from app.services.network_addressing import STANDARD_OPTIONS
from app.services.service_spec import MAX_REPLICAS as MAX_SWARM_REPLICAS
from app.services.service_spec import MAX_REPLICAS_STANDARD as MAX_SWARM_REPLICAS_STANDARD
from app.services.stack_document import (
    FILE_MESSAGE,
    REFUSED_SERVICE_KEYS,
    ServiceBuildCheck,
    path_part,
    source_variable_refusal,
)

__all__ = [
    "MAX_NETWORKS",
    "MAX_REPLICAS",
    "MAX_SERVICES",
    "MAX_SWARM_FILES",
    "MAX_VOLUMES",
    "PLAIN_SERVICE_KEYS",
    "RESTRICTED_SERVICE_KEYS",
    "STACK_OPTION_IDS",
    "SWARM_PLAIN_SERVICE_KEYS",
    "SWARM_SECURITY_OPTS",
    "SWARM_SERVICE_KEYS",
    "ExternalUse",
    "ResolvedModel",
    "StackFinding",
    "StackScan",
    "backstop",
    "engine_body",
    "limits",
    "masked",
    "refused_stack_options",
    "reserved_labels",
    "resolved_digest",
    "resolved_refusals",
    "scan",
    "stack_option_label",
    "swarm_refusals",
]

MAX_SERVICES: Final = 50
MAX_NETWORKS: Final = 50
MAX_VOLUMES: Final = 50
MAX_REPLICAS: Final = 20
MAX_SWARM_FILES: Final = 50

STACK_OPTION_IDS: Final[tuple[str, ...]] = (
    "network_addressing",
    "network_driver",
    "network_option",
    "volume_option",
    "reserved_label",
)
_STACK_PHRASES: Final[Mapping[str, str]] = {
    "network_addressing": "custom network addressing",
    "network_driver": "network drivers other than bridge",
    "network_option": "network driver options beyond the standard ones",
    "volume_option": "volume drivers and driver options",
    "reserved_label": "the stack membership labels",
}

PLAIN_SERVICE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "image",
        "command",
        "entrypoint",
        "environment",
        "working_dir",
        "user",
        "hostname",
        "domainname",
        "tty",
        "stdin_open",
        "init",
        "read_only",
        "stop_signal",
        "stop_grace_period",
        "healthcheck",
        "restart",
        "mac_address",
        "shm_size",
        "group_add",
        "dns",
        "dns_opt",
        "dns_search",
        "extra_hosts",
        "ulimits",
        "cap_drop",
        "ports",
        "expose",
        "tmpfs",
        "mem_limit",
        "mem_reservation",
        "memswap_limit",
        "mem_swappiness",
        "cpus",
        "cpu_shares",
        "cpu_period",
        "cpu_quota",
        "cpuset",
        "cpu_count",
        "cpu_percent",
        "pids_limit",
        "platform",
        "profiles",
        "depends_on",
        "scale",
        "pull_policy",
        "container_name",
        "attach",
        "labels",
        "env_file",
        "label_file",
    }
)

# Refused when the value is non-empty, with the container option id.
RESTRICTED_SERVICE_KEYS: Final[Mapping[str, str]] = {
    "privileged": "privileged",
    "cap_add": "cap_add",
    "devices": "devices",
    "device_cgroup_rules": "devices",
    "gpus": "devices",
    "volumes_from": "volumes_from",
    "links": "links",
    "external_links": "links",
    "cgroup_parent": "cgroup_parent",
    "userns_mode": "userns_mode",
    "uts": "uts_namespace",
    "sysctls": "sysctls",
    "storage_opt": "storage_opt",
    "oom_kill_disable": "oom_kill_disable",
    "cpu_rt_period": "realtime_cpu",
    "cpu_rt_runtime": "realtime_cpu",
    "annotations": "annotations",
}

_NO_SWARM_FORM: Final = "This key has no Swarm form."
_USE_RESOURCES: Final = "This key has no Swarm form; use deploy.resources."
_SWARM_NO_FORM_HINTS: Final[Mapping[str, str]] = {
    "depends_on": "Swarm starts services in no order, so this key has no Swarm form.",
    "container_name": "Swarm names the containers, so this key has no Swarm form.",
    "scale": "This key has no Swarm form; use deploy.replicas.",
    "tmpfs": "This key has no Swarm form; use a tmpfs entry under volumes.",
    "cpus": _USE_RESOURCES,
    "mem_limit": _USE_RESOURCES,
    "mem_reservation": _USE_RESOURCES,
    "memswap_limit": _USE_RESOURCES,
    "mem_swappiness": _USE_RESOURCES,
    "pids_limit": _USE_RESOURCES,
}
_SWARM_NO_FORM_KEYS: Final[frozenset[str]] = frozenset(
    {
        "container_name",
        "depends_on",
        "links",
        "external_links",
        "network_mode",
        "ipc",
        "pid",
        "uts",
        "userns_mode",
        "cgroup",
        "cgroup_parent",
        "privileged",
        "devices",
        "device_cgroup_rules",
        "gpus",
        "volumes_from",
        "mac_address",
        "domainname",
        "shm_size",
        "tmpfs",
        "storage_opt",
        "oom_kill_disable",
        "cpu_rt_period",
        "cpu_rt_runtime",
        "cpu_shares",
        "cpu_period",
        "cpu_quota",
        "cpuset",
        "cpu_count",
        "cpu_percent",
        "cpus",
        "mem_limit",
        "mem_reservation",
        "memswap_limit",
        "mem_swappiness",
        "pids_limit",
        "blkio_config",
        "expose",
        "profiles",
        "scale",
        "pull_policy",
        "attach",
        "platform",
        "runtime",
        "credential_spec",
        "annotations",
        "use_api_socket",
        "post_start",
        "pre_stop",
    }
)
SWARM_PLAIN_SERVICE_KEYS: Final[frozenset[str]] = PLAIN_SERVICE_KEYS - _SWARM_NO_FORM_KEYS
_SWARM_RESTRICTED_SERVICE_KEYS: Final[Mapping[str, str]] = {
    key: RESTRICTED_SERVICE_KEYS[key] for key in ("cap_add", "sysctls")
}
_SWARM_HANDLED_SERVICE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "oom_score_adj",
        "volumes",
        "networks",
        "security_opt",
        "logging",
        "isolation",
        "deploy",
        "configs",
        "secrets",
    }
)
SWARM_SERVICE_KEYS: Final[frozenset[str]] = (
    SWARM_PLAIN_SERVICE_KEYS | _SWARM_RESTRICTED_SERVICE_KEYS.keys() | _SWARM_HANDLED_SERVICE_KEYS
)
SWARM_SECURITY_OPTS: Final[Mapping[str, str]] = {
    "no-new-privileges": "no_new_privileges",
    "no-new-privileges:true": "no_new_privileges",
    "no-new-privileges=true": "no_new_privileges",
    "seccomp=unconfined": "seccomp_unconfined",
    "seccomp:unconfined": "seccomp_unconfined",
    "apparmor=unconfined": "apparmor_unconfined",
    "apparmor:unconfined": "apparmor_unconfined",
}

_SWARM_DEPLOY_KEYS: Final = frozenset(
    {
        "mode",
        "replicas",
        "labels",
        "update_config",
        "rollback_config",
        "resources",
        "restart_policy",
        "placement",
        "endpoint_mode",
    }
)
_SWARM_ROLLOUT_KEYS: Final = frozenset(
    {"parallelism", "delay", "failure_action", "monitor", "max_failure_ratio", "order"}
)
_SWARM_PLACEMENT_KEYS: Final = frozenset({"constraints", "preferences", "max_replicas_per_node"})
_SWARM_LIMIT_KEYS: Final = frozenset({"cpus", "memory", "pids"})
_SWARM_RESERVATION_KEYS: Final = frozenset({"cpus", "memory", "generic_resources"})
_SWARM_HEALTHCHECK_KEYS: Final = frozenset(
    {"test", "interval", "timeout", "retries", "start_period", "start_interval", "disable"}
)
_SWARM_PORT_KEYS: Final = frozenset({"target", "published", "protocol", "mode", "name"})
_SWARM_MOUNT_KEYS: Final = frozenset(
    {"type", "source", "target", "read_only", "volume", "tmpfs", "bind"}
)
_SWARM_BIND_KEYS: Final = frozenset({"propagation", "create_host_path"})
_SWARM_ENDPOINT_KEYS: Final = frozenset({"aliases", "driver_opts"})
_SWARM_NETWORK_KEYS: Final = frozenset(
    {
        "name",
        "driver",
        "driver_opts",
        "internal",
        "attachable",
        "enable_ipv6",
        "labels",
        "ipam",
        "external",
    }
)
_SWARM_IPAM_KEYS: Final = frozenset({"driver", "config"})
_SWARM_IPAM_CONFIG_KEYS: Final = frozenset({"subnet", "gateway", "ip_range"})
_SWARM_CONFIG_KEYS: Final = frozenset({"name", "content", "environment", "labels", "external"})
_SWARM_SECRET_KEYS: Final = frozenset({"name", "environment", "labels", "external"})
_SWARM_UNSUPPORTED: Final = "This key is not supported for a Swarm stack."
_SWARM_NETWORK_OPTIONS: Final = STANDARD_OPTIONS["overlay"]
_BLKIO_DEVICE_KEYS: Final[Mapping[str, str]] = {
    "weight_device": "BlkioWeightDevice",
    "device_read_bps": "BlkioDeviceReadBps",
    "device_write_bps": "BlkioDeviceWriteBps",
    "device_read_iops": "BlkioDeviceReadIOps",
    "device_write_iops": "BlkioDeviceWriteIOps",
}
_ENDPOINT_KEYS: Final = frozenset(
    {"aliases", "ipv4_address", "ipv6_address", "mac_address", "priority", "gw_priority"}
)
_MOUNT_KEYS: Final = frozenset(
    {"type", "source", "target", "read_only", "consistency", "volume", "tmpfs"}
)
_SWARM_SCAN_MOUNT_KEYS: Final = _MOUNT_KEYS - {"consistency"}
_VOLUME_MOUNT_KEYS: Final = frozenset({"nocopy", "subpath"})
_TMPFS_MOUNT_KEYS: Final = frozenset({"size", "mode"})
_FILE_ENTRY_KEYS: Final = frozenset({"source", "target", "uid", "gid", "mode"})
_HOOK_KEYS: Final = frozenset({"command", "user", "working_dir", "environment", "privileged"})
_LOGGING_KEYS: Final = frozenset({"driver", "options"})
_RESTART_POLICY_KEYS: Final = frozenset({"condition", "delay", "max_attempts", "window"})
_NETWORK_KEYS: Final = frozenset(
    {"name", "internal", "attachable", "enable_ipv6", "labels", "external"}
)
_VOLUME_KEYS: Final = frozenset({"name", "labels", "external"})
_SWARM_VOLUME_KEYS: Final = _VOLUME_KEYS | {"driver", "driver_opts"}
_SOURCE_ENTRY_KEYS: Final = frozenset({"name", "content", "environment", "file", "labels"})
_RESOLVED_TOP_KEYS: Final = frozenset(
    {"name", "services", "networks", "volumes", "configs", "secrets"}
)
_SERVICE_BODY_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    "image": ("Image",),
    "command": ("Cmd",),
    "entrypoint": ("Entrypoint",),
    "environment": ("Env",),
    "working_dir": ("WorkingDir",),
    "user": ("User",),
    "hostname": ("Hostname",),
    "domainname": ("Domainname",),
    "tty": ("Tty",),
    "stdin_open": ("OpenStdin",),
    "stop_signal": ("StopSignal",),
    "stop_grace_period": ("StopTimeout",),
    "healthcheck": ("Healthcheck",),
    "mac_address": ("MacAddress",),
    "expose": ("ExposedPorts",),
    "labels": ("Labels",),
    "init": ("HostConfig", "Init"),
    "read_only": ("HostConfig", "ReadonlyRootfs"),
    "restart": ("HostConfig", "RestartPolicy"),
    "shm_size": ("HostConfig", "ShmSize"),
    "group_add": ("HostConfig", "GroupAdd"),
    "dns": ("HostConfig", "Dns"),
    "dns_opt": ("HostConfig", "DnsOptions"),
    "dns_search": ("HostConfig", "DnsSearch"),
    "extra_hosts": ("HostConfig", "ExtraHosts"),
    "ulimits": ("HostConfig", "Ulimits"),
    "cap_drop": ("HostConfig", "CapDrop"),
    "ports": ("HostConfig", "PortBindings"),
    "tmpfs": ("HostConfig", "Tmpfs"),
    "mem_limit": ("HostConfig", "Memory"),
    "mem_reservation": ("HostConfig", "MemoryReservation"),
    "memswap_limit": ("HostConfig", "MemorySwap"),
    "mem_swappiness": ("HostConfig", "MemorySwappiness"),
    "cpus": ("HostConfig", "NanoCpus"),
    "cpu_shares": ("HostConfig", "CpuShares"),
    "cpu_period": ("HostConfig", "CpuPeriod"),
    "cpu_quota": ("HostConfig", "CpuQuota"),
    "cpuset": ("HostConfig", "CpusetCpus"),
    "cpu_count": ("HostConfig", "CpuCount"),
    "cpu_percent": ("HostConfig", "CpuPercent"),
    "pids_limit": ("HostConfig", "PidsLimit"),
    "privileged": ("HostConfig", "Privileged"),
    "cap_add": ("HostConfig", "CapAdd"),
    "devices": ("HostConfig", "Devices"),
    "device_cgroup_rules": ("HostConfig", "DeviceCgroupRules"),
    "gpus": ("HostConfig", "DeviceRequests"),
    "volumes_from": ("HostConfig", "VolumesFrom"),
    "links": ("HostConfig", "Links"),
    "cgroup_parent": ("HostConfig", "CgroupParent"),
    "userns_mode": ("HostConfig", "UsernsMode"),
    "uts": ("HostConfig", "UTSMode"),
    "sysctls": ("HostConfig", "Sysctls"),
    "storage_opt": ("HostConfig", "StorageOpt"),
    "oom_kill_disable": ("HostConfig", "OomKillDisable"),
    "oom_score_adj": ("HostConfig", "OomScoreAdj"),
    "cpu_rt_period": ("HostConfig", "CpuRealtimePeriod"),
    "cpu_rt_runtime": ("HostConfig", "CpuRealtimeRuntime"),
    "annotations": ("HostConfig", "Annotations"),
    "ipc": ("HostConfig", "IpcMode"),
    "pid": ("HostConfig", "PidMode"),
    "cgroup": ("HostConfig", "CgroupnsMode"),
    "security_opt": ("HostConfig", "SecurityOpt"),
    "runtime": ("HostConfig", "Runtime"),
    "isolation": ("HostConfig", "Isolation"),
}
_NO_BODY_FIELD: Final = frozenset(
    {
        "platform",
        "profiles",
        "depends_on",
        "scale",
        "pull_policy",
        "container_name",
        "attach",
        "label_file",
    }
)

_HEX_ID: Final = re.compile(r"[0-9a-fA-F]{64}")


class ResolvedModel(Protocol):
    """The parts of a `config --format json` result this module reads."""

    @property
    def name(self) -> str:
        """Return the project name."""
        ...

    @property
    def services(self) -> Mapping[str, Mapping[str, Any]]:
        """Return the services by key."""
        ...

    @property
    def networks(self) -> Mapping[str, Mapping[str, Any]]:
        """Return the top-level networks by key."""
        ...

    @property
    def volumes(self) -> Mapping[str, Mapping[str, Any]]:
        """Return the top-level volumes by key."""
        ...

    @property
    def configs(self) -> Mapping[str, Mapping[str, Any]]:
        """Return the top-level configs by key."""
        ...

    @property
    def secrets(self) -> Mapping[str, Mapping[str, Any]]:
        """Return the top-level secrets by key."""
        ...

    @property
    def document(self) -> Mapping[str, Any]:
        """Return the whole decoded document."""
        ...


def stack_option_label(option: str) -> str:
    """Return a restricted option id as a phrase, such as "host bind mounts"."""
    phrase = _STACK_PHRASES.get(option)
    return phrase if phrase is not None else option_label(option)


@dataclass(frozen=True, slots=True)
class StackFinding:
    """One restricted use: the option id and the Compose path it was found at."""

    option: str
    path: str
    engine_path: str | None = None

    @property
    def label(self) -> str:
        """Return the option as a phrase, such as "host bind mounts"."""
        return stack_option_label(self.option)

    @property
    def message(self) -> str:
        """Return the sentence shown beside the path."""
        if self.option == "unknown_option":
            return "This option is not recognised, so only administrators may use it."
        if self.option == "reserved_label":
            return "The stack membership labels are set by the product."
        return f"Only administrators may use {self.label}."


@dataclass(frozen=True, slots=True)
class ExternalUse:
    """One service use of an external volume, network, config or secret, by its resolved name."""

    kind: Literal["volume", "network", "config", "secret"]
    name: str
    path: str
    read_only: bool


@dataclass(frozen=True, slots=True)
class StackScan:
    """The outcome of the standard-user scan and the objects the stack names."""

    findings: tuple[StackFinding, ...]
    external: tuple[ExternalUse, ...]
    project_volumes: tuple[str, ...]
    project_networks: tuple[str, ...]
    images: Mapping[str, tuple[str, str]]


def refused_stack_options(findings: Sequence[StackFinding]) -> tuple[str, ...]:
    """Return the distinct option ids of the findings, container ids first."""
    present = {finding.option for finding in findings}
    return tuple(option for option in (*OPTION_IDS, *STACK_OPTION_IDS) if option in present)


def _is_external(entry: Mapping[str, Any]) -> bool:
    external = entry.get("external")
    return external is True or (isinstance(external, Mapping) and bool(external))


def _resolved_name(key: str, entry: object) -> str:
    if isinstance(entry, Mapping):
        name = entry.get("name")
        if isinstance(name, str) and name:
            return name
    return key


def _project_names(project: ResolvedModel) -> tuple[tuple[str, ...], tuple[str, ...]]:
    volumes = tuple(
        _resolved_name(key, entry)
        for key, entry in project.volumes.items()
        if isinstance(entry, Mapping) and not _is_external(entry)
    )
    networks = [
        _resolved_name(key, entry)
        for key, entry in project.networks.items()
        if isinstance(entry, Mapping) and not _is_external(entry)
    ]
    default = f"{project.name}_default"
    uses_default = any(
        isinstance(service.get("networks"), Mapping | list) and "default" in service["networks"]
        for service in project.services.values()
        if isinstance(service, Mapping)
    )
    if uses_default and default not in networks and "default" not in project.networks:
        networks.append(default)
    return volumes, tuple(networks)


def limits(
    project: ResolvedModel, *, kind: StackKind = "compose", administrator: bool = True
) -> dict[str, str]:
    """Return the product limits the model breaks, keyed by Compose path; applies to everyone.

    administrator only changes the replica ceiling of a swarm stack.
    """
    if kind == "swarm":
        return _swarm_limits(project, administrator=administrator)
    found: dict[str, str] = {}
    if len(project.services) > MAX_SERVICES:
        found["services"] = f"A stack can have at most {MAX_SERVICES} services."
    volumes, networks = _project_names(project)
    if len(networks) > MAX_NETWORKS:
        found["networks"] = f"A stack can create at most {MAX_NETWORKS} networks."
    if len(volumes) > MAX_VOLUMES:
        found["volumes"] = f"A stack can create at most {MAX_VOLUMES} volumes."
    for key, service in project.services.items():
        if not isinstance(service, Mapping):
            continue
        base = f"services.{path_part(key)}"
        if _count(service.get("scale")) > MAX_REPLICAS:
            found[f"{base}.scale"] = f"A service can run at most {MAX_REPLICAS} replicas."
        deploy = service.get("deploy")
        if isinstance(deploy, Mapping) and _count(deploy.get("replicas")) > MAX_REPLICAS:
            found[f"{base}.deploy.replicas"] = f"A service can run at most {MAX_REPLICAS} replicas."
        container_name = service.get("container_name")
        if isinstance(container_name, str) and _HEX_ID.fullmatch(container_name):
            found[f"{base}.container_name"] = "A container name cannot look like a container id."
    return found


def _swarm_limits(project: ResolvedModel, *, administrator: bool) -> dict[str, str]:
    found: dict[str, str] = {}
    if len(project.services) > MAX_SERVICES:
        found["services"] = f"A stack can have at most {MAX_SERVICES} services."
    volumes, networks = _project_names(project)
    if len(networks) > MAX_NETWORKS:
        found["networks"] = f"A stack can create at most {MAX_NETWORKS} networks."
    if len(volumes) > MAX_VOLUMES:
        found["volumes"] = f"A stack can use at most {MAX_VOLUMES} volumes."
    for section, entries in (("configs", project.configs), ("secrets", project.secrets)):
        if len(entries) > MAX_SWARM_FILES:
            found[section] = f"A stack can have at most {MAX_SWARM_FILES} {section}."
    ceiling = MAX_SWARM_REPLICAS if administrator else MAX_SWARM_REPLICAS_STANDARD
    for key, service in project.services.items():
        deploy = service.get("deploy") if isinstance(service, Mapping) else None
        if isinstance(deploy, Mapping) and _count(deploy.get("replicas")) > ceiling:
            found[f"services.{path_part(key)}.deploy.replicas"] = (
                f"A service can run at most {ceiling} replicas."
            )
    return found


def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _is_reserved_label(key: object) -> bool:
    if not isinstance(key, str):
        return False
    try:
        SubmittedLabels({key: ""})
    except ValidationFailed:
        return True
    return False


def _label_keys(labels: object) -> list[str]:
    if isinstance(labels, Mapping):
        return [key for key in labels if isinstance(key, str)]
    if isinstance(labels, list | tuple):
        return [item.split("=", 1)[0] for item in labels if isinstance(item, str)]
    return []


def reserved_labels(
    project: ResolvedModel, *, kind: StackKind = "compose"
) -> tuple[StackFinding, ...]:
    """Return every use of a stack membership label on a service, network or volume.

    A swarm stack creates configs and secrets with labels, so those are checked as well.
    """
    found: list[StackFinding] = []

    def check(path: str, labels: object) -> None:
        if any(_is_reserved_label(key) for key in _label_keys(labels)):
            found.append(StackFinding("reserved_label", path))

    for key, service in project.services.items():
        if not isinstance(service, Mapping):
            continue
        base = f"services.{path_part(key)}"
        check(f"{base}.labels", service.get("labels"))
        deploy = service.get("deploy")
        if isinstance(deploy, Mapping):
            check(f"{base}.deploy.labels", deploy.get("labels"))
    sections = [("networks", project.networks), ("volumes", project.volumes)]
    if kind == "swarm":
        sections += [("configs", project.configs), ("secrets", project.secrets)]
    for section, entries in sections:
        for key, entry in entries.items():
            if isinstance(entry, Mapping):
                check(f"{section}.{path_part(key)}.labels", entry.get("labels"))
    return tuple(found)


def resolved_refusals(
    project: ResolvedModel,
    *,
    companions: Collection[str] = (),
    generated: Mapping[tuple[str, str], str] | None = None,
    builds: ServiceBuildCheck | None = None,
) -> dict[str, str]:
    """Return the constructs the resolved model must not hold for anyone, keyed by Compose path.

    `companions` holds the absolute paths of the stack's companion files in its project
    directory. Any other file path the model still carries is refused, and so is every
    `file` source: a source read from a file reaches Compose as an environment source.
    `generated` maps (section, key) to the generated variable that entry may name.
    `builds` admits a service's `build` section and returns its refusals by path; without it
    `build` is refused.
    """
    found: dict[str, str] = {}
    allowed = frozenset(companions)
    delivered = generated or {}
    for key in project.document:
        if not (key in _RESOLVED_TOP_KEYS or (isinstance(key, str) and key.startswith("x-"))):
            found[path_part(key)] = "This top-level key is not supported."
    for name, service in project.services.items():
        if not isinstance(service, Mapping):
            continue
        base = f"services.{path_part(name)}"
        for key, value in service.items():
            folded = key.strip().casefold() if isinstance(key, str) else ""
            message = REFUSED_SERVICE_KEYS.get(folded)
            if (
                folded == "pull_policy"
                and isinstance(value, str)
                and value.strip().casefold() == "build"
            ):
                message = REFUSED_SERVICE_KEYS["build"]
            if key == "build" and builds is not None:
                message = None
            if message is not None:
                found[f"{base}.{path_part(key)}"] = message
            if folded in ("env_file", "label_file"):
                _confine(f"{base}.{path_part(key)}", value, allowed, found)
            if folded == "extends" and isinstance(value, Mapping) and "file" in value:
                _confine(f"{base}.{path_part(key)}.file", value["file"], allowed, found)
        if builds is not None:
            found.update(builds(base, service))
    for section, entries in (("configs", project.configs), ("secrets", project.secrets)):
        for name, entry in entries.items():
            if not isinstance(entry, Mapping):
                continue
            for key in entry:
                if isinstance(key, str) and key.strip().casefold() == "file":
                    found[f"{section}.{path_part(name)}.{path_part(key)}"] = FILE_MESSAGE
            variable = entry.get("environment")
            if isinstance(variable, str) and delivered.get((section, str(name))) == variable:
                continue
            refusal = source_variable_refusal(variable) if isinstance(variable, str) else None
            if refusal is not None:
                found[f"{section}.{path_part(name)}.environment"] = refusal
    return found


def _confine(path: str, value: object, allowed: frozenset[str], found: dict[str, str]) -> None:
    """Refuse each file path in `value` that is not one of the allowed companion file paths."""
    if isinstance(value, str):
        if value not in allowed:
            found[path] = FILE_MESSAGE
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _confine(f"{path}[{index}]", item, allowed, found)
    elif isinstance(value, Mapping):
        _confine(f"{path}.path", value.get("path"), allowed, found)
    else:
        found[path] = FILE_MESSAGE


def swarm_refusals(
    project: ResolvedModel,
    *,
    companions: Collection[str] = (),
    generated: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, str]:
    """Return the constructs a swarm stack must not hold for anyone, keyed by Compose path.

    Includes resolved_refusals, with the same arguments. A key outside the supported set at
    any level is refused, not ignored.
    """
    found = resolved_refusals(project, companions=companions, generated=generated)
    _SwarmRefusals(project, found).run()
    return found


class _SwarmRefusals:
    def __init__(self, project: ResolvedModel, found: dict[str, str]) -> None:
        self.project = project
        self.found = found

    def add(self, path: str, message: str) -> None:
        self.found.setdefault(path, message)

    def keys_within(
        self,
        path: str,
        value: object,
        allowed: frozenset[str],
        refused: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(value, Mapping):
            return
        for key in value:
            if isinstance(key, str) and (key in allowed or key.startswith("x-")):
                continue
            message = refused.get(key) if refused is not None and isinstance(key, str) else None
            self.add(f"{path}.{path_part(key)}", message or _SWARM_UNSUPPORTED)

    def run(self) -> None:
        for name, service in self.project.services.items():
            if isinstance(service, Mapping):
                self.service(f"services.{path_part(name)}", service)
        for key, network in self.project.networks.items():
            self.network(f"networks.{path_part(key)}", key, network)
        for key, volume in self.project.volumes.items():
            self.volume(f"volumes.{path_part(key)}", key, volume)
        self.source_entries("configs", self.project.configs, _SWARM_CONFIG_KEYS, _CONFIG_REFUSED)
        self.source_entries("secrets", self.project.secrets, _SWARM_SECRET_KEYS, _SECRET_REFUSED)

    def service(self, base: str, service: Mapping[str, Any]) -> None:
        for key in service:
            if not isinstance(key, str):
                self.add(f"{base}.<key>", _SWARM_UNSUPPORTED)
            elif key in _SWARM_NO_FORM_KEYS:
                self.add(f"{base}.{key}", _SWARM_NO_FORM_HINTS.get(key, _NO_SWARM_FORM))
            elif key not in SWARM_SERVICE_KEYS and not key.startswith("x-"):
                self.add(f"{base}.{path_part(key)}", _SWARM_UNSUPPORTED)
        self.deploy(f"{base}.deploy", service.get("deploy"))
        self.keys_within(f"{base}.healthcheck", service.get("healthcheck"), _SWARM_HEALTHCHECK_KEYS)
        self.keys_within(f"{base}.logging", service.get("logging"), _LOGGING_KEYS)
        self.entries(f"{base}.ports", service.get("ports"), _SWARM_PORT_KEYS, _PORT_REFUSED)
        self.entries(f"{base}.configs", service.get("configs"), _FILE_ENTRY_KEYS)
        self.entries(f"{base}.secrets", service.get("secrets"), _FILE_ENTRY_KEYS)
        self.mounts(f"{base}.volumes", service.get("volumes"))
        self.service_networks(f"{base}.networks", service.get("networks"))
        self.security_opt(f"{base}.security_opt", service.get("security_opt"))

    def entries(
        self,
        path: str,
        value: object,
        allowed: frozenset[str],
        refused: Mapping[str, str] | None = None,
    ) -> None:
        if isinstance(value, list | tuple):
            for ordinal, entry in enumerate(value):
                self.keys_within(f"{path}[{ordinal}]", entry, allowed, refused)

    def deploy(self, path: str, deploy: object) -> None:
        if not isinstance(deploy, Mapping):
            return
        self.keys_within(path, deploy, _SWARM_DEPLOY_KEYS)
        mode = deploy.get("mode")
        if isinstance(mode, str) and mode in ("replicated-job", "global-job"):
            self.add(f"{path}.mode", "Job services have no Swarm stack form.")
        for key in ("update_config", "rollback_config"):
            self.keys_within(f"{path}.{key}", deploy.get(key), _SWARM_ROLLOUT_KEYS)
        self.keys_within(
            f"{path}.restart_policy", deploy.get("restart_policy"), _RESTART_POLICY_KEYS
        )
        self.keys_within(f"{path}.placement", deploy.get("placement"), _SWARM_PLACEMENT_KEYS)
        resources = deploy.get("resources")
        self.keys_within(f"{path}.resources", resources, frozenset({"limits", "reservations"}))
        if isinstance(resources, Mapping):
            self.keys_within(f"{path}.resources.limits", resources.get("limits"), _SWARM_LIMIT_KEYS)
            self.keys_within(
                f"{path}.resources.reservations",
                resources.get("reservations"),
                _SWARM_RESERVATION_KEYS,
                {"devices": "Device reservations have no Swarm form."},
            )

    def mounts(self, path: str, value: object) -> None:
        if not isinstance(value, list | tuple):
            return
        for ordinal, mount in enumerate(value):
            where = f"{path}[{ordinal}]"
            if not isinstance(mount, Mapping):
                continue
            self.keys_within(where, mount, _SWARM_MOUNT_KEYS, _MOUNT_REFUSED)
            mount_type = mount.get("type")
            if isinstance(mount_type, str) and mount_type not in ("volume", "bind", "tmpfs"):
                self.add(f"{where}.type", "A mount of this type has no Swarm form.")
            self.keys_within(
                f"{where}.volume", mount.get("volume"), _VOLUME_MOUNT_KEYS, _VOLUME_MOUNT_REFUSED
            )
            self.keys_within(f"{where}.tmpfs", mount.get("tmpfs"), _TMPFS_MOUNT_KEYS)
            self.keys_within(f"{where}.bind", mount.get("bind"), _SWARM_BIND_KEYS, _BIND_REFUSED)

    def service_networks(self, path: str, value: object) -> None:
        if not isinstance(value, Mapping):
            return
        for key, config in value.items():
            self.keys_within(
                f"{path}.{path_part(key)}", config, _SWARM_ENDPOINT_KEYS, _ENDPOINT_REFUSED
            )

    def security_opt(self, path: str, value: object) -> None:
        if not isinstance(value, list | tuple):
            return
        for ordinal, entry in enumerate(value):
            if not (isinstance(entry, str) and entry in SWARM_SECURITY_OPTS):
                self.add(f"{path}[{ordinal}]", "This security option has no Swarm form.")

    def network(self, path: str, key: object, entry: object) -> None:
        if not isinstance(entry, Mapping):
            return
        self.keys_within(path, entry, _SWARM_NETWORK_KEYS)
        if _is_external(entry):
            return
        driver = entry.get("driver")
        if isinstance(driver, str) and driver not in ("", "overlay"):
            self.add(f"{path}.driver", "Only the overlay driver is supported for a Swarm network.")
        self.explicit_name(path, key, entry)
        ipam = entry.get("ipam")
        self.keys_within(f"{path}.ipam", ipam, _SWARM_IPAM_KEYS, _IPAM_REFUSED)
        if isinstance(ipam, Mapping):
            self.entries(
                f"{path}.ipam.config", ipam.get("config"), _SWARM_IPAM_CONFIG_KEYS, _IPAM_REFUSED
            )

    def volume(self, path: str, key: object, entry: object) -> None:
        if not isinstance(entry, Mapping):
            return
        self.keys_within(path, entry, _SWARM_VOLUME_KEYS)
        if not _is_external(entry):
            self.explicit_name(path, key, entry)

    def source_entries(
        self,
        section: str,
        entries: Mapping[str, Mapping[str, Any]],
        allowed: frozenset[str],
        refused: Mapping[str, str],
    ) -> None:
        for key, entry in entries.items():
            path = f"{section}.{path_part(key)}"
            if not isinstance(entry, Mapping):
                continue
            self.keys_within(path, entry, allowed, refused)
            if not _is_external(entry):
                self.explicit_name(path, key, entry)

    def explicit_name(self, path: str, key: object, entry: Mapping[str, Any]) -> None:
        name = entry.get("name")
        if isinstance(name, str) and name and name != f"{self.project.name}_{key}":
            self.add(f"{path}.name", "The name is set by the product; remove `name`.")


_PORT_REFUSED: Final[Mapping[str, str]] = {
    "host_ip": "Binding a port to a host address has no Swarm form.",
    "app_protocol": _NO_SWARM_FORM,
}
_ENDPOINT_REFUSED: Final[Mapping[str, str]] = dict.fromkeys(
    (
        "ipv4_address",
        "ipv6_address",
        "mac_address",
        "priority",
        "gw_priority",
        "link_local_ips",
        "interface_name",
    ),
    _NO_SWARM_FORM,
)
_MOUNT_REFUSED: Final[Mapping[str, str]] = {"consistency": "Mount consistency has no Swarm form."}
_VOLUME_MOUNT_REFUSED: Final[Mapping[str, str]] = {
    "labels": "Volume labels are set by the product."
}
_BIND_REFUSED: Final[Mapping[str, str]] = {"selinux": "Relabelling a bind mount has no Swarm form."}
_IPAM_REFUSED: Final[Mapping[str, str]] = {
    "options": "Address pool options have no Swarm form.",
    "aux_addresses": "Auxiliary addresses have no Swarm form.",
}
_CONFIG_REFUSED: Final[Mapping[str, str]] = {"template_driver": "Templating has no Swarm form."}
_SECRET_REFUSED: Final[Mapping[str, str]] = {
    "content": "A secret value must come from a secret variable, not from the file.",
    "driver": "Secret drivers have no Swarm form.",
    "driver_opts": "Secret drivers have no Swarm form.",
    "template_driver": "Templating has no Swarm form.",
}


class _Scan:
    def __init__(
        self, project: ResolvedModel, default_runtime: str | None, kind: StackKind = "compose"
    ) -> None:
        self.project = project
        self.default_runtime = default_runtime
        self.swarm = kind == "swarm"
        self.findings: list[StackFinding] = []
        self.external: list[ExternalUse] = []

    def add(self, option: str, path: str) -> None:
        self.findings.append(StackFinding(option, path))

    def unknown(self, path: str, key: object) -> None:
        self.add("unknown_option", f"{path}.{path_part(key)}")

    def keys_within(self, path: str, value: object, allowed: frozenset[str]) -> None:
        if not isinstance(value, Mapping):
            self.add("unknown_option", path)
            return
        for key in value:
            if not (isinstance(key, str) and key in allowed):
                self.unknown(path, key)

    def run(self) -> None:
        for name, service in self.project.services.items():
            base = f"services.{path_part(name)}"
            if isinstance(service, Mapping):
                self.service(base, service)
            else:
                self.add("unknown_option", base)
        for key, network in self.project.networks.items():
            self.network(f"networks.{path_part(key)}", network)
        for key, volume in self.project.volumes.items():
            self.volume(f"volumes.{path_part(key)}", volume)
        for section, entries in (
            ("configs", self.project.configs),
            ("secrets", self.project.secrets),
        ):
            for key, entry in entries.items():
                self.source_entry(f"{section}.{path_part(key)}", entry, secret=section == "secrets")

    def service(self, base: str, service: Mapping[str, Any]) -> None:
        if self.swarm:
            self.swarm_service(base, service)
            return
        handlers: dict[str, Callable[[str, object], None]] = {
            "oom_score_adj": self.oom_score_adj,
            "volumes": self.mounts,
            "networks": self.networks,
            "network_mode": self.network_mode,
            "ipc": self.namespace(ALLOWED_IPC_MODES, "ipc_namespace"),
            "pid": self.namespace(ALLOWED_PID_MODES, "pid_namespace"),
            "cgroup": self.namespace(ALLOWED_CGROUPNS_MODES, "cgroupns_mode"),
            "security_opt": self.security_opt,
            "logging": self.logging,
            "runtime": self.runtime,
            "isolation": self.isolation,
            "deploy": self.deploy,
            "configs": self.file_entries,
            "secrets": self.file_entries,
            "post_start": self.hooks,
            "pre_stop": self.hooks,
            "blkio_config": self.blkio,
        }
        for key, value in service.items():
            path = f"{base}.{path_part(key)}"
            if not isinstance(key, str):
                self.add("unknown_option", path)
            elif key in PLAIN_SERVICE_KEYS or key.startswith("x-"):
                continue
            elif key in handlers:
                handlers[key](path, value)
            elif key in RESTRICTED_SERVICE_KEYS:
                if not is_empty(value):
                    self.add(RESTRICTED_SERVICE_KEYS[key], path)
            else:
                self.add("unknown_option", path)

    def swarm_service(self, base: str, service: Mapping[str, Any]) -> None:
        handlers: dict[str, Callable[[str, object], None]] = {
            "oom_score_adj": self.oom_score_adj,
            "volumes": self.mounts,
            "networks": self.networks,
            "security_opt": self.security_opt,
            "logging": self.logging,
            "isolation": self.isolation,
            "deploy": self.swarm_deploy,
            "configs": self.file_entries,
            "secrets": self.file_entries,
        }
        for key, value in service.items():
            path = f"{base}.{path_part(key)}"
            if not isinstance(key, str):
                self.add("unknown_option", path)
            elif key in SWARM_PLAIN_SERVICE_KEYS or key.startswith("x-"):
                continue
            elif key in handlers:
                handlers[key](path, value)
            elif key in _SWARM_RESTRICTED_SERVICE_KEYS:
                if not is_empty(value):
                    self.add(_SWARM_RESTRICTED_SERVICE_KEYS[key], path)
            else:
                self.add("unknown_option", path)

    def oom_score_adj(self, path: str, value: object) -> None:
        if not is_empty(value) and not (
            isinstance(value, int) and not isinstance(value, bool) and value > 0
        ):
            self.add("oom_score_adj", path)

    def runtime(self, path: str, value: object) -> None:
        if not is_empty(value) and not (isinstance(value, str) and value == self.default_runtime):
            self.add("runtime", path)

    def isolation(self, path: str, value: object) -> None:
        if not is_empty(value) and value != "default":
            self.add("isolation", path)

    def namespace(self, allowed: frozenset[str], option: str) -> Callable[[str, object], None]:
        def check(path: str, value: object) -> None:
            if value is not None and not (isinstance(value, str) and value in allowed):
                self.add(option, path)

        return check

    def security_opt(self, path: str, value: object) -> None:
        if is_empty(value):
            return
        if not isinstance(value, list | tuple):
            self.add("security_opt", path)
            return
        for ordinal, entry in enumerate(value):
            if not (isinstance(entry, str) and entry in ALLOWED_SECURITY_OPTS):
                self.add("security_opt", f"{path}[{ordinal}]")

    def logging(self, path: str, value: object) -> None:
        if is_blank(value):
            return
        if not isinstance(value, Mapping):
            self.add("log_driver", path)
            return
        for key in value:
            if key not in _LOGGING_KEYS:
                self.unknown(path, key)
        driver = value.get("driver")
        if not is_empty(driver) and not (isinstance(driver, str) and driver in ALLOWED_LOG_DRIVERS):
            self.add("log_driver", f"{path}.driver")
        options = value.get("options")
        if isinstance(options, Mapping):
            for key in options:
                if not (isinstance(key, str) and key in ALLOWED_LOG_OPTION_KEYS):
                    self.add("log_driver", f"{path}.options.{path_part(key)}")
        elif not is_empty(options):
            self.add("log_driver", f"{path}.options")

    def network_mode(self, path: str, value: object) -> None:
        if is_empty(value):
            return
        if not isinstance(value, str):
            self.add("unknown_option", path)
            return
        folded = value.strip().casefold()
        if folded == "host":
            self.add("network_host", path)
        elif folded.startswith(("container:", "service:")):
            self.add("network_container", path)
        elif value not in ("bridge", "none"):
            self.add("unknown_option", path)

    def blkio(self, path: str, value: object) -> None:
        if not isinstance(value, Mapping):
            self.add("unknown_option", path)
            return
        for key, item in value.items():
            if key == "weight":
                continue
            if key in _BLKIO_DEVICE_KEYS:
                if not is_empty(item):
                    self.add("blkio_devices", f"{path}.{path_part(key)}")
            else:
                self.unknown(path, key)

    def file_entries(self, path: str, value: object) -> None:
        if is_empty(value):
            return
        if not isinstance(value, list | tuple):
            self.add("unknown_option", path)
            return
        for ordinal, entry in enumerate(value):
            self.keys_within(f"{path}[{ordinal}]", entry, _FILE_ENTRY_KEYS)
            if self.swarm and isinstance(entry, Mapping):
                self.external_file(path, f"{path}[{ordinal}]", entry.get("source"))

    def external_file(self, path: str, where: str, source: object) -> None:
        section = "secrets" if path.endswith(".secrets") else "configs"
        entries = self.project.secrets if section == "secrets" else self.project.configs
        entry = entries.get(source) if isinstance(source, str) else None
        if isinstance(source, str) and isinstance(entry, Mapping) and _is_external(entry):
            self.external.append(
                ExternalUse(
                    "secret" if section == "secrets" else "config",
                    _resolved_name(source, entry),
                    where,
                    False,
                )
            )

    def hooks(self, path: str, value: object) -> None:
        if is_empty(value):
            return
        if not isinstance(value, list | tuple):
            self.add("unknown_option", path)
            return
        for ordinal, hook in enumerate(value):
            where = f"{path}[{ordinal}]"
            self.keys_within(where, hook, _HOOK_KEYS)
            if isinstance(hook, Mapping) and not is_empty(hook.get("privileged")):
                self.add("privileged", f"{where}.privileged")

    def deploy(self, path: str, value: object) -> None:
        if not isinstance(value, Mapping):
            self.add("unknown_option", path)
            return
        for key, item in value.items():
            where = f"{path}.{path_part(key)}"
            if key == "replicas":
                continue
            if key == "mode":
                if item != "replicated":
                    self.add("unknown_option", where)
            elif key == "placement":
                if not is_blank(item):
                    self.add("unknown_option", where)
            elif key == "restart_policy":
                self.keys_within(where, item, _RESTART_POLICY_KEYS)
            elif key == "resources":
                self.resources(where, item)
            else:
                self.add("unknown_option", where)

    def swarm_deploy(self, path: str, value: object) -> None:
        if not isinstance(value, Mapping):
            self.add("unknown_option", path)
            return
        for key, item in value.items():
            where = f"{path}.{path_part(key)}"
            if key == "mode":
                if item not in ("replicated", "global"):
                    self.add("unknown_option", where)
            elif key in ("replicas", "labels", "endpoint_mode"):
                continue
            elif key in ("update_config", "rollback_config"):
                self.keys_within(where, item, _SWARM_ROLLOUT_KEYS)
            elif key == "placement":
                self.keys_within(where, item, _SWARM_PLACEMENT_KEYS)
            elif key == "restart_policy":
                self.keys_within(where, item, _RESTART_POLICY_KEYS)
            elif key == "resources":
                self.swarm_resources(where, item)
            else:
                self.add("unknown_option", where)

    def swarm_resources(self, path: str, value: object) -> None:
        if not isinstance(value, Mapping):
            self.add("unknown_option", path)
            return
        for key, item in value.items():
            where = f"{path}.{path_part(key)}"
            if key == "limits":
                self.keys_within(where, item, _SWARM_LIMIT_KEYS)
            elif key == "reservations":
                if not isinstance(item, Mapping):
                    self.add("unknown_option", where)
                    continue
                for sub, content in item.items():
                    if sub in ("cpus", "memory"):
                        continue
                    if sub in ("generic_resources", "devices"):
                        if not is_empty(content):
                            self.add("devices", f"{where}.{sub}")
                    else:
                        self.unknown(where, sub)
            else:
                self.add("unknown_option", where)

    def resources(self, path: str, value: object) -> None:
        if not isinstance(value, Mapping):
            self.add("unknown_option", path)
            return
        for key, item in value.items():
            where = f"{path}.{path_part(key)}"
            if key == "limits":
                self.keys_within(where, item, frozenset({"cpus", "memory", "pids"}))
            elif key == "reservations":
                if not isinstance(item, Mapping):
                    self.add("unknown_option", where)
                    continue
                for sub, content in item.items():
                    if sub in ("cpus", "memory"):
                        continue
                    if sub == "devices":
                        if not is_empty(content):
                            self.add("devices", f"{where}.devices")
                    else:
                        self.unknown(where, sub)
            else:
                self.add("unknown_option", where)

    def mounts(self, path: str, value: object) -> None:
        if is_empty(value):
            return
        if not isinstance(value, list | tuple):
            self.add("unknown_option", path)
            return
        for ordinal, mount in enumerate(value):
            where = f"{path}[{ordinal}]"
            if not isinstance(mount, Mapping):
                self.add("unknown_option", where)
                continue
            mount_type = mount.get("type")
            if isinstance(mount_type, str) and mount_type.strip().casefold() == "bind":
                self.add("bind_mount", f"{where}.type")
                continue
            if mount_type not in ("volume", "tmpfs"):
                self.add("mount_type", f"{where}.type")
                continue
            mount_keys = _SWARM_SCAN_MOUNT_KEYS if self.swarm else _MOUNT_KEYS
            for key in mount:
                if not (isinstance(key, str) and key in mount_keys):
                    self.unknown(where, key)
            for sub, allowed in (("volume", _VOLUME_MOUNT_KEYS), ("tmpfs", _TMPFS_MOUNT_KEYS)):
                if not is_empty(mount.get(sub)):
                    self.keys_within(f"{where}.{sub}", mount[sub], allowed)
            if mount_type == "volume":
                self.volume_source(where, mount)

    def volume_source(self, where: str, mount: Mapping[str, Any]) -> None:
        source = mount.get("source")
        if is_empty(source):
            return
        entry = self.project.volumes.get(source) if isinstance(source, str) else None
        if not isinstance(source, str) or not isinstance(entry, Mapping):
            self.add("unknown_option", f"{where}.source")
            return
        if _is_external(entry):
            self.external.append(
                ExternalUse(
                    "volume",
                    _resolved_name(source, entry),
                    f"{where}.source",
                    mount.get("read_only") is True,
                )
            )

    def networks(self, path: str, value: object) -> None:
        if is_empty(value):
            return
        entries: dict[Any, Any]
        if isinstance(value, Mapping):
            entries = dict(value)
        elif isinstance(value, list | tuple):
            entries = {item: None for item in value if isinstance(item, str)}
            if len(entries) != len(value):
                self.add("unknown_option", path)
        else:
            self.add("unknown_option", path)
            return
        for key, config in entries.items():
            where = f"{path}.{path_part(key)}"
            top = self.project.networks.get(key) if isinstance(key, str) else None
            if not isinstance(key, str) or (top is None and key != "default"):
                self.add("unknown_option", where)
                continue
            resolved = (
                _resolved_name(key, top) if top is not None else f"{self.project.name}_default"
            )
            folded = resolved.strip().casefold()
            if folded == "host":
                self.add("network_host", where)
            elif folded.startswith("container:"):
                self.add("network_container", where)
            if config is not None:
                if not isinstance(config, Mapping):
                    self.add("unknown_option", where)
                    continue
                for sub, content in config.items():
                    if sub == "driver_opts":
                        if not is_empty(content):
                            self.add("endpoint_driver_opts", f"{where}.driver_opts")
                    elif not (
                        isinstance(sub, str)
                        and sub in (_SWARM_ENDPOINT_KEYS if self.swarm else _ENDPOINT_KEYS)
                    ):
                        self.unknown(where, sub)
            if isinstance(top, Mapping) and _is_external(top):
                self.external.append(ExternalUse("network", resolved, where, False))

    def network(self, path: str, entry: object) -> None:
        if not isinstance(entry, Mapping):
            self.add("unknown_option", path)
            return
        standard = _SWARM_NETWORK_OPTIONS if self.swarm else STANDARD_OPTIONS["bridge"]
        driver_name = "overlay" if self.swarm else "bridge"
        for key, value in entry.items():
            where = f"{path}.{path_part(key)}"
            if not isinstance(key, str):
                self.add("unknown_option", where)
            elif key in _NETWORK_KEYS or key.startswith("x-"):
                continue
            elif key == "driver":
                if not is_empty(value) and value != driver_name:
                    self.add("network_driver", where)
            elif key == "driver_opts":
                if isinstance(value, Mapping):
                    for option in value:
                        if not (isinstance(option, str) and option in standard):
                            self.add("network_option", f"{where}.{path_part(option)}")
                elif not is_empty(value):
                    self.add("network_option", where)
            elif key == "ipam":
                if not is_blank(value):
                    self.add("network_addressing", where)
            else:
                self.add("unknown_option", where)

    def volume(self, path: str, entry: object) -> None:
        if not isinstance(entry, Mapping):
            self.add("unknown_option", path)
            return
        for key, value in entry.items():
            where = f"{path}.{path_part(key)}"
            if not isinstance(key, str):
                self.add("unknown_option", where)
            elif key in _VOLUME_KEYS or key.startswith("x-"):
                continue
            elif key == "driver":
                if not is_empty(value) and value != "local":
                    self.add("volume_option", where)
            elif key == "driver_opts":
                if not is_blank(value):
                    self.add("volume_option", where)
            else:
                self.add("unknown_option", where)

    def source_entry(self, path: str, entry: object, *, secret: bool = False) -> None:
        if not isinstance(entry, Mapping):
            self.add("unknown_option", path)
            return
        allowed = _SOURCE_ENTRY_KEYS
        sources: tuple[str, ...] = ("content", "environment", "file")
        if self.swarm:
            allowed = _SWARM_SECRET_KEYS if secret else _SWARM_CONFIG_KEYS
            sources = (
                ("environment", "external") if secret else ("content", "environment", "external")
            )
        for key in entry:
            if not (isinstance(key, str) and (key in allowed or key.startswith("x-"))):
                self.unknown(path, key)
        if not any(source in entry for source in sources):
            self.add("unknown_option", path)


_POLICY_RANK: Final = {"always": 2, "never": 0}


def _images(project: ResolvedModel) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    ranks: dict[str, int] = {}
    for name, service in project.services.items():
        if not isinstance(service, Mapping):
            continue
        image = service.get("image")
        if not isinstance(image, str) or not image:
            continue
        raw = service.get("pull_policy")
        policy = raw.strip() if isinstance(raw, str) and raw.strip() else "missing"
        rank = _POLICY_RANK.get(policy.casefold(), 1)
        if image not in found or rank > ranks[image]:
            found[image] = (name, policy)
            ranks[image] = rank
    return found


def scan(
    project: ResolvedModel, *, default_runtime: str | None, kind: StackKind = "compose"
) -> StackScan:
    """Apply the standard-user allow-lists to a resolved model; administrators skip this.

    A swarm stack has its own key set, and its external networks, configs and secrets are
    returned in `external` for the reach checks.
    """
    scanner = _Scan(project, default_runtime, kind)
    scanner.run()
    volumes, networks = _project_names(project)
    return StackScan(
        findings=tuple(scanner.findings),
        external=tuple(scanner.external),
        project_volumes=volumes,
        project_networks=networks,
        images=MappingProxyType(_images(project)),
    )


def _put(body: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    target = body
    for part in path[:-1]:
        target = target.setdefault(part, {})
    target[path[-1]] = value


def _network_endpoints(project: ResolvedModel, networks: object) -> dict[str, Any]:
    entries: dict[Any, Any]
    if isinstance(networks, Mapping):
        entries = dict(networks)
    elif isinstance(networks, list | tuple):
        entries = {item: None for item in networks if isinstance(item, str)}
    else:
        return {}
    endpoints: dict[str, Any] = {}
    for key, config in entries.items():
        top = project.networks.get(key) if isinstance(key, str) else None
        if isinstance(key, str):
            name = _resolved_name(key, top) if top is not None else key
        else:
            name = "<network>"
        endpoint: dict[str, Any] = {}
        if isinstance(config, Mapping):
            for sub, content in config.items():
                match sub:
                    case "aliases":
                        endpoint["Aliases"] = content
                    case "ipv4_address":
                        endpoint.setdefault("IPAMConfig", {})["IPv4Address"] = content
                    case "ipv6_address":
                        endpoint.setdefault("IPAMConfig", {})["IPv6Address"] = content
                    case "mac_address":
                        endpoint["MacAddress"] = content
                    case "driver_opts":
                        endpoint["DriverOpts"] = content
                    case "gw_priority":
                        endpoint["GwPriority"] = content
                    case "priority":
                        continue
                    case _:
                        endpoint[f"compose.{sub}"] = content
        elif config is not None:
            endpoint["compose.entry"] = config
        endpoints[name] = endpoint
    return endpoints


def _mount_body(project: ResolvedModel, mount: object) -> Any:
    if not isinstance(mount, Mapping):
        return mount
    entry: dict[str, Any] = {}
    for key, value in mount.items():
        match key:
            case "type":
                entry["Type"] = value
            case "source":
                top = project.volumes.get(value) if isinstance(value, str) else None
                named = mount.get("type") == "volume" and isinstance(top, Mapping)
                entry["Source"] = _resolved_name(value, top) if named else value
            case "target":
                entry["Target"] = value
            case "read_only":
                entry["ReadOnly"] = value
            case "consistency":
                entry["Consistency"] = value
            case "volume" if isinstance(value, Mapping):
                options = {
                    {"nocopy": "NoCopy", "subpath": "Subpath"}.get(sub, f"compose.{sub}"): content
                    for sub, content in value.items()
                }
                entry["VolumeOptions"] = options
            case "tmpfs" if isinstance(value, Mapping):
                options = {
                    {"size": "SizeBytes", "mode": "Mode"}.get(sub, f"compose.{sub}"): content
                    for sub, content in value.items()
                }
                entry["TmpfsOptions"] = options
            case "bind":
                entry["BindOptions"] = value
            case "image":
                entry["ImageOptions"] = value
            case _:
                entry[f"compose.{key}"] = value
    return entry


def engine_body(service: Mapping[str, Any], project: ResolvedModel | None = None) -> dict[str, Any]:
    """Map a resolved service to the Engine create body that find_restricted inspects.

    Keys with no Engine field are carried under a "compose." prefix so the unknown-key rule
    sees them; benign fields carry their Compose values unchanged. project resolves network and
    volume keys to their names.
    """
    body: dict[str, Any] = {}
    lookup: ResolvedModel = project if project is not None else _EMPTY_PROJECT
    for key, value in service.items():
        if not isinstance(key, str):
            body["compose.<key>"] = value
        elif key in _NO_BODY_FIELD or key.startswith("x-"):
            continue
        elif key in ("post_start", "pre_stop"):
            _hooks_into(body, key, value)
        elif key in ("configs", "secrets"):
            _file_entries_into(body, key, value)
        elif key == "volumes":
            mounts = value if isinstance(value, list | tuple) else [value]
            _put(
                body,
                ("HostConfig", "Mounts"),
                [_mount_body(lookup, mount) for mount in mounts if not is_empty(mount)],
            )
        elif key == "networks":
            _put(body, ("NetworkingConfig", "EndpointsConfig"), _network_endpoints(lookup, value))
        elif key == "network_mode":
            _put(body, ("HostConfig", "NetworkMode"), _network_mode_body(value))
        elif key == "logging":
            _put(body, ("HostConfig", "LogConfig"), _logging_body(value))
        elif key == "deploy":
            _deploy_into(body, value)
        elif key == "blkio_config":
            _blkio_into(body, value)
        elif key in ("links", "external_links") and isinstance(value, list | tuple):
            existing = body.get("HostConfig", {}).get("Links") or []
            _put(body, ("HostConfig", "Links"), [*existing, *value])
        elif key in _SERVICE_BODY_FIELDS:
            _put(body, _SERVICE_BODY_FIELDS[key], value)
        else:
            body[f"compose.{key}"] = value
    return body


def _network_mode_body(value: object) -> object:
    if isinstance(value, str) and value.strip().casefold().startswith("service:"):
        return "container:" + value.strip().split(":", 1)[1]
    return value


def _logging_body(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    config: dict[str, Any] = {}
    for key, content in value.items():
        match key:
            case "driver":
                config["Type"] = content
            case "options":
                config["Config"] = content
            case _:
                config[f"compose.{key}"] = content
    return config


def _file_entries_into(body: dict[str, Any], kind: str, entries: object) -> None:
    if not isinstance(entries, list | tuple):
        if not is_empty(entries):
            body[f"compose.{kind}"] = entries
        return
    for entry in entries:
        if not isinstance(entry, Mapping):
            body[f"compose.{kind}"] = entry
            continue
        for key, content in entry.items():
            if key not in _FILE_ENTRY_KEYS:
                body[f"compose.{kind}.{key}"] = content


def _hooks_into(body: dict[str, Any], kind: str, hooks: object) -> None:
    if is_empty(hooks):
        return
    if not isinstance(hooks, list | tuple):
        body[f"compose.{kind}"] = hooks
        return
    for hook in hooks:
        if not isinstance(hook, Mapping):
            body[f"compose.{kind}"] = hook
            continue
        for key, content in hook.items():
            if key == "privileged":
                if not is_empty(content):
                    _put(body, ("HostConfig", "Privileged"), True)
            elif key not in ("command", "user", "working_dir", "environment"):
                body[f"compose.{kind}.{key}"] = content


def _blkio_into(body: dict[str, Any], value: object) -> None:
    if not isinstance(value, Mapping):
        body["compose.blkio_config"] = value
        return
    for key, content in value.items():
        if key == "weight":
            _put(body, ("HostConfig", "BlkioWeight"), content)
        elif key in _BLKIO_DEVICE_KEYS:
            _put(body, ("HostConfig", _BLKIO_DEVICE_KEYS[key]), content)
        else:
            body[f"compose.blkio_config.{key}"] = content


def _deploy_into(body: dict[str, Any], value: object) -> None:
    if not isinstance(value, Mapping):
        body["compose.deploy"] = value
        return
    for key, content in value.items():
        if key == "replicas" or (key == "mode" and content == "replicated"):
            continue
        if key == "placement" and is_blank(content):
            continue
        if key == "restart_policy":
            host = body.setdefault("HostConfig", {})
            host.setdefault("RestartPolicy", content)
        elif key == "resources" and isinstance(content, Mapping):
            _resources_into(body, content)
        else:
            body[f"compose.deploy.{key}"] = content


def _resources_into(body: dict[str, Any], resources: Mapping[str, Any]) -> None:
    for key, content in resources.items():
        if key == "limits" and isinstance(content, Mapping):
            for sub, amount in content.items():
                field = {"cpus": "NanoCpus", "memory": "Memory", "pids": "PidsLimit"}.get(sub)
                if field is None:
                    body[f"compose.deploy.resources.limits.{sub}"] = amount
                else:
                    _put(body, ("HostConfig", field), amount)
        elif key == "reservations" and isinstance(content, Mapping):
            for sub, amount in content.items():
                if sub == "memory":
                    _put(body, ("HostConfig", "MemoryReservation"), amount)
                elif sub == "devices":
                    _put(body, ("HostConfig", "DeviceRequests"), amount)
                elif sub != "cpus":
                    body[f"compose.deploy.resources.reservations.{sub}"] = amount
        else:
            body[f"compose.deploy.resources.{key}"] = content


@dataclass(frozen=True, slots=True)
class _NoProject:
    name: str = ""
    services: Mapping[str, Mapping[str, Any]] = MappingProxyType({})
    networks: Mapping[str, Mapping[str, Any]] = MappingProxyType({})
    volumes: Mapping[str, Mapping[str, Any]] = MappingProxyType({})
    configs: Mapping[str, Mapping[str, Any]] = MappingProxyType({})
    secrets: Mapping[str, Mapping[str, Any]] = MappingProxyType({})
    document: Mapping[str, Any] = MappingProxyType({})


_EMPTY_PROJECT: Final = _NoProject()


def backstop(
    project: ResolvedModel,
    *,
    default_runtime: str | None,
    covered: Sequence[StackFinding] = (),
) -> tuple[StackFinding, ...]:
    """Run find_restricted over each mapped service body; return findings the scan did not make.

    covered holds the scan's findings. A finding returned here marks a gap in the scan.
    """
    missed: list[StackFinding] = []
    for name, service in project.services.items():
        if not isinstance(service, Mapping):
            continue
        base = f"services.{path_part(name)}"
        body = engine_body(service, project)
        seen = {
            finding.option
            for finding in covered
            if finding.path == base or finding.path.startswith(f"{base}.")
        }
        for finding in find_restricted(body, default_runtime=default_runtime):
            if finding.option not in seen:
                seen.add(finding.option)
                missed.append(StackFinding(finding.option, base, finding.path))
    return tuple(missed)


def _secret_forms(secrets: Collection[str]) -> tuple[str, ...]:
    forms = {form for secret in secrets if secret for form in (secret, secret.replace("$", "$$"))}
    return tuple(sorted(forms, key=len, reverse=True))


def _mask(value: Any, forms: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        return {_mask_text(str(key), forms): _mask(item, forms) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_mask(item, forms) for item in value]
    if isinstance(value, str):
        return _mask_text(value, forms)
    if isinstance(value, int | float) and not isinstance(value, bool) and str(value) in forms:
        return REDACTED
    return value


def _mask_text(text: str, forms: tuple[str, ...]) -> str:
    for form in forms:
        if form in text:
            text = text.replace(form, REDACTED)
    return text


def masked(project: ResolvedModel, secrets: Collection[str]) -> Mapping[str, Any]:
    """Return the resolved document with every secret value, and its `$$`-escaped form, replaced."""
    masked_document: Mapping[str, Any] = _mask(project.document, _secret_forms(secrets))
    return masked_document


def resolved_digest(project: ResolvedModel, secrets: Collection[str]) -> str:
    """Return the SHA-256 of the canonical JSON of the masked resolved model."""
    canonical = json.dumps(
        masked(project, secrets), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
