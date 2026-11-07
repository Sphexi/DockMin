"""Detection of service options that reach host root, and the references a service spec holds."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.services.container_restrictions import (
    ALLOWED_LOG_DRIVERS,
    ALLOWED_LOG_OPTION_KEYS,
    KNOWN_MOUNT_KEYS,
    KNOWN_TMPFS_OPTION_KEYS,
    KNOWN_VOLUME_OPTION_KEYS,
    VOLUME_NAME,
    Finding,
    is_blank,
    is_empty,
)

__all__ = [
    "KNOWN_CONFIG_REFERENCE_KEYS",
    "KNOWN_CONTAINER_SPEC_KEYS",
    "KNOWN_ENDPOINT_PORT_KEYS",
    "KNOWN_ENDPOINT_SPEC_KEYS",
    "KNOWN_FILE_KEYS",
    "KNOWN_HEALTHCHECK_KEYS",
    "KNOWN_LOG_DRIVER_KEYS",
    "KNOWN_MODE_KEYS",
    "KNOWN_NETWORK_KEYS",
    "KNOWN_PLACEMENT_KEYS",
    "KNOWN_PRIVILEGES_KEYS",
    "KNOWN_RESOURCES_KEYS",
    "KNOWN_RESTART_POLICY_KEYS",
    "KNOWN_ROLLOUT_KEYS",
    "KNOWN_SECRET_REFERENCE_KEYS",
    "KNOWN_SERVICE_SPEC_KEYS",
    "KNOWN_TASK_SPEC_KEYS",
    "ServiceReferences",
    "added_references",
    "constraints_of",
    "find_restricted_service",
    "parse_constraint",
    "pinned_to",
    "references",
    "volume_pin_exempt",
    "volume_placement_findings",
]

KNOWN_SERVICE_SPEC_KEYS: Final[frozenset[str]] = frozenset(
    {
        "Name",
        "Labels",
        "TaskTemplate",
        "Mode",
        "UpdateConfig",
        "RollbackConfig",
        "Networks",
        "EndpointSpec",
    }
)
KNOWN_TASK_SPEC_KEYS: Final[frozenset[str]] = frozenset(
    {
        "PluginSpec",
        "ContainerSpec",
        "NetworkAttachmentSpec",
        "Resources",
        "RestartPolicy",
        "Placement",
        "ForceUpdate",
        "Runtime",
        "Networks",
        "LogDriver",
    }
)
KNOWN_CONTAINER_SPEC_KEYS: Final[frozenset[str]] = frozenset(
    {
        "Image",
        "Labels",
        "Command",
        "Args",
        "Hostname",
        "Env",
        "Dir",
        "User",
        "Groups",
        "Privileges",
        "TTY",
        "OpenStdin",
        "ReadOnly",
        "Mounts",
        "StopSignal",
        "StopGracePeriod",
        "Healthcheck",
        "Hosts",
        "DNSConfig",
        "Secrets",
        "Configs",
        "OomScoreAdj",
        "Isolation",
        "Init",
        "Sysctls",
        "CapabilityAdd",
        "CapabilityDrop",
        "Ulimits",
    }
)
KNOWN_PRIVILEGES_KEYS: Final[frozenset[str]] = frozenset(
    {"CredentialSpec", "SELinuxContext", "Seccomp", "AppArmor", "NoNewPrivileges"}
)
KNOWN_CREDENTIAL_SPEC_KEYS: Final[frozenset[str]] = frozenset({"Config", "File", "Registry"})
KNOWN_SELINUX_KEYS: Final[frozenset[str]] = frozenset({"Disable", "User", "Role", "Type", "Level"})
KNOWN_SECCOMP_KEYS: Final[frozenset[str]] = frozenset({"Mode", "Profile"})
KNOWN_APPARMOR_KEYS: Final[frozenset[str]] = frozenset({"Mode"})
KNOWN_HEALTHCHECK_KEYS: Final[frozenset[str]] = frozenset(
    {"Test", "Interval", "Timeout", "Retries", "StartPeriod", "StartInterval"}
)
KNOWN_DNS_CONFIG_KEYS: Final[frozenset[str]] = frozenset({"Nameservers", "Search", "Options"})
KNOWN_ULIMIT_KEYS: Final[frozenset[str]] = frozenset({"Name", "Soft", "Hard"})
KNOWN_FILE_KEYS: Final[frozenset[str]] = frozenset({"Name", "UID", "GID", "Mode"})
KNOWN_SECRET_REFERENCE_KEYS: Final[frozenset[str]] = frozenset({"File", "SecretID", "SecretName"})
KNOWN_CONFIG_REFERENCE_KEYS: Final[frozenset[str]] = frozenset(
    {"File", "Runtime", "ConfigID", "ConfigName"}
)
KNOWN_MOUNT_BIND_KEYS: Final[frozenset[str]] = frozenset(
    {
        "Propagation",
        "NonRecursive",
        "CreateMountpoint",
        "ReadOnlyNonRecursive",
        "ReadOnlyForceRecursive",
    }
)
KNOWN_MOUNT_IMAGE_KEYS: Final[frozenset[str]] = frozenset({"Subpath"})
KNOWN_DRIVER_CONFIG_KEYS: Final[frozenset[str]] = frozenset({"Name", "Options"})
KNOWN_RESOURCES_KEYS: Final[frozenset[str]] = frozenset({"Limits", "Reservations"})
KNOWN_LIMIT_KEYS: Final[frozenset[str]] = frozenset({"NanoCPUs", "MemoryBytes", "Pids"})
KNOWN_RESERVATION_KEYS: Final[frozenset[str]] = frozenset(
    {"NanoCPUs", "MemoryBytes", "GenericResources"}
)
KNOWN_RESTART_POLICY_KEYS: Final[frozenset[str]] = frozenset(
    {"Condition", "Delay", "MaxAttempts", "Window"}
)
KNOWN_PLACEMENT_KEYS: Final[frozenset[str]] = frozenset(
    {"Constraints", "Preferences", "MaxReplicas", "Platforms"}
)
KNOWN_PREFERENCE_KEYS: Final[frozenset[str]] = frozenset({"Spread"})
KNOWN_SPREAD_KEYS: Final[frozenset[str]] = frozenset({"SpreadDescriptor"})
KNOWN_PLATFORM_KEYS: Final[frozenset[str]] = frozenset({"Architecture", "OS"})
KNOWN_NETWORK_KEYS: Final[frozenset[str]] = frozenset({"Target", "Aliases", "DriverOpts"})
KNOWN_LOG_DRIVER_KEYS: Final[frozenset[str]] = frozenset({"Name", "Options"})
KNOWN_MODE_KEYS: Final[frozenset[str]] = frozenset(
    {"Replicated", "Global", "ReplicatedJob", "GlobalJob"}
)
KNOWN_REPLICATED_KEYS: Final[frozenset[str]] = frozenset({"Replicas"})
KNOWN_REPLICATED_JOB_KEYS: Final[frozenset[str]] = frozenset({"MaxConcurrent", "TotalCompletions"})
KNOWN_ROLLOUT_KEYS: Final[frozenset[str]] = frozenset(
    {"Parallelism", "Delay", "FailureAction", "Monitor", "MaxFailureRatio", "Order"}
)
KNOWN_ENDPOINT_SPEC_KEYS: Final[frozenset[str]] = frozenset({"Mode", "Ports"})
KNOWN_ENDPOINT_PORT_KEYS: Final[frozenset[str]] = frozenset(
    {"Name", "Protocol", "TargetPort", "PublishedPort", "PublishMode"}
)

_SAFE_KEY: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_CONSTRAINT_KEY: Final = re.compile(r"[a-z_][a-z0-9\-_.]+", re.IGNORECASE)
_CONSTRAINT_VALUE: Final = re.compile(r"[a-z0-9:\-_\s.*()?+\[\]\\^$|/]+", re.IGNORECASE)
_SAFE_MODES: Final = frozenset({"", "default"})

_TASK = "TaskTemplate"
_CONTAINER = "TaskTemplate.ContainerSpec"


def find_restricted_service(
    spec: Mapping[str, Any], *, slots: Mapping[str, Sequence[int]] | None = None
) -> list[Finding]:
    """Return every restricted use in a final ServiceSpec, empty when there is none.

    slots maps "mounts" and "networks" to the form row index of each spec element, so findings
    name the row the user typed. A JSON null counts as absent wherever a value is judged.
    """
    walker = _Walker(slots or {})
    walker.service(spec)
    return walker.findings


class _Walker:
    def __init__(self, slots: Mapping[str, Sequence[int]]) -> None:
        self.findings: list[Finding] = []
        self._slots = slots

    def add(self, option: str, path: str, field: str | None = None) -> None:
        self.findings.append(Finding(option, path, field))

    def slot(self, group: str, ordinal: int) -> int:
        return _slot(self._slots, group, ordinal)

    def keys(self, path: str, node: Mapping[Any, Any], known: frozenset[str]) -> None:
        # The Engine decodes field names case-insensitively, so only the exact spelling is known.
        for key in node:
            if not isinstance(key, str) or key not in known:
                name = key if isinstance(key, str) and _SAFE_KEY.fullmatch(key) else "<key>"
                self.add("unknown_option", f"{path}.{name}" if path else name)

    def node(self, path: str, value: Any, known: frozenset[str]) -> Mapping[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            self.add("unknown_option", path)
            return None
        self.keys(path, value, known)
        return value

    def items(self, path: str, value: Any) -> list[Any]:
        if value is None:
            return []
        if not isinstance(value, list | tuple):
            self.add("unknown_option", path)
            return []
        return list(value)

    def service(self, spec: Mapping[str, Any]) -> None:
        self.keys("", spec, KNOWN_SERVICE_SPEC_KEYS)
        self._task(spec.get("TaskTemplate"))
        self._mode(spec.get("Mode"))
        for key in ("UpdateConfig", "RollbackConfig"):
            self.node(key, spec.get(key), KNOWN_ROLLOUT_KEYS)
        endpoint = self.node("EndpointSpec", spec.get("EndpointSpec"), KNOWN_ENDPOINT_SPEC_KEYS)
        if endpoint is not None:
            for ordinal, port in enumerate(self.items("EndpointSpec.Ports", endpoint.get("Ports"))):
                self.node(f"EndpointSpec.Ports[{ordinal}]", port, KNOWN_ENDPOINT_PORT_KEYS)
        if not is_empty(spec.get("Networks")):
            self.add("unknown_option", "Networks")

    def _mode(self, mode: Any) -> None:
        node = self.node("Mode", mode, KNOWN_MODE_KEYS)
        if node is None:
            return
        self.node("Mode.Replicated", node.get("Replicated"), KNOWN_REPLICATED_KEYS)
        self.node("Mode.ReplicatedJob", node.get("ReplicatedJob"), KNOWN_REPLICATED_JOB_KEYS)
        self.node("Mode.Global", node.get("Global"), frozenset())
        self.node("Mode.GlobalJob", node.get("GlobalJob"), frozenset())

    def _task(self, task_value: Any) -> None:
        task = self.node(_TASK, task_value, KNOWN_TASK_SPEC_KEYS)
        if task is None:
            return
        self._container(task.get("ContainerSpec"))
        self._resources(task.get("Resources"))
        self.node(f"{_TASK}.RestartPolicy", task.get("RestartPolicy"), KNOWN_RESTART_POLICY_KEYS)
        self._placement(task.get("Placement"))
        self._runtime(task)
        self._task_networks(task.get("Networks"))
        self._log_driver(task.get("LogDriver"))

    def _runtime(self, task: Mapping[str, Any]) -> None:
        runtime = task.get("Runtime")
        if runtime is not None and runtime not in ("", "container"):
            self.add("runtime", f"{_TASK}.Runtime")
        for key in ("PluginSpec", "NetworkAttachmentSpec"):
            if task.get(key) is not None:
                self.add("runtime", f"{_TASK}.{key}")

    def _placement(self, value: Any) -> None:
        placement = self.node(f"{_TASK}.Placement", value, KNOWN_PLACEMENT_KEYS)
        if placement is None:
            return
        base = f"{_TASK}.Placement"
        for ordinal, preference in enumerate(
            self.items(f"{base}.Preferences", placement.get("Preferences"))
        ):
            entry = self.node(f"{base}.Preferences[{ordinal}]", preference, KNOWN_PREFERENCE_KEYS)
            if entry is not None:
                self.node(
                    f"{base}.Preferences[{ordinal}].Spread", entry.get("Spread"), KNOWN_SPREAD_KEYS
                )
        for ordinal, platform in enumerate(
            self.items(f"{base}.Platforms", placement.get("Platforms"))
        ):
            self.node(f"{base}.Platforms[{ordinal}]", platform, KNOWN_PLATFORM_KEYS)

    def _resources(self, value: Any) -> None:
        resources = self.node(f"{_TASK}.Resources", value, KNOWN_RESOURCES_KEYS)
        if resources is None:
            return
        self.node(f"{_TASK}.Resources.Limits", resources.get("Limits"), KNOWN_LIMIT_KEYS)
        reservations = self.node(
            f"{_TASK}.Resources.Reservations", resources.get("Reservations"), KNOWN_RESERVATION_KEYS
        )
        if reservations is not None and not is_empty(reservations.get("GenericResources")):
            self.add("devices", f"{_TASK}.Resources.Reservations.GenericResources")

    def _task_networks(self, value: Any) -> None:
        base = f"{_TASK}.Networks"
        for ordinal, entry in enumerate(self.items(base, value)):
            path = f"{base}[{ordinal}]"
            network = self.node(path, entry, KNOWN_NETWORK_KEYS)
            if network is None:
                continue
            target = network.get("Target")
            if isinstance(target, str) and target.strip().casefold() == "host":
                self.add(
                    "network_host",
                    f"{path}.Target",
                    f"network-{self.slot('networks', ordinal)}-name",
                )
            if not is_empty(network.get("DriverOpts")):
                self.add("endpoint_driver_opts", f"{path}.DriverOpts")

    def _log_driver(self, value: Any) -> None:
        path = f"{_TASK}.LogDriver"
        log = self.node(path, value, KNOWN_LOG_DRIVER_KEYS)
        if log is None or is_blank(log):
            return
        driver = log.get("Name")
        if not is_empty(driver) and not (isinstance(driver, str) and driver in ALLOWED_LOG_DRIVERS):
            self.add("log_driver", f"{path}.Name")
        options = log.get("Options")
        if isinstance(options, Mapping):
            for key in options:
                if not (isinstance(key, str) and key in ALLOWED_LOG_OPTION_KEYS):
                    name = key if isinstance(key, str) and _SAFE_KEY.fullmatch(key) else "<key>"
                    self.add("log_driver", f"{path}.Options[{name}]")
        elif not is_empty(options):
            self.add("log_driver", f"{path}.Options")

    def _container(self, value: Any) -> None:
        container = self.node(_CONTAINER, value, KNOWN_CONTAINER_SPEC_KEYS)
        if container is None:
            return
        self._privileges(container.get("Privileges"))
        if not is_empty(container.get("CapabilityAdd")):
            self.add("cap_add", f"{_CONTAINER}.CapabilityAdd")
        self._oom_score(container.get("OomScoreAdj"))
        if not is_empty(container.get("Sysctls")):
            self.add("sysctls", f"{_CONTAINER}.Sysctls")
        isolation = container.get("Isolation")
        if not is_empty(isolation) and isolation != "default":
            self.add("isolation", f"{_CONTAINER}.Isolation")
        self.node(f"{_CONTAINER}.Healthcheck", container.get("Healthcheck"), KNOWN_HEALTHCHECK_KEYS)
        self.node(f"{_CONTAINER}.DNSConfig", container.get("DNSConfig"), KNOWN_DNS_CONFIG_KEYS)
        for ordinal, ulimit in enumerate(
            self.items(f"{_CONTAINER}.Ulimits", container.get("Ulimits"))
        ):
            self.node(f"{_CONTAINER}.Ulimits[{ordinal}]", ulimit, KNOWN_ULIMIT_KEYS)
        self._references(container.get("Secrets"), "Secrets", KNOWN_SECRET_REFERENCE_KEYS)
        self._references(container.get("Configs"), "Configs", KNOWN_CONFIG_REFERENCE_KEYS)
        self._mounts(container.get("Mounts"))

    def _oom_score(self, score: Any) -> None:
        if not is_empty(score) and not (
            isinstance(score, int) and not isinstance(score, bool) and score > 0
        ):
            self.add("oom_score_adj", f"{_CONTAINER}.OomScoreAdj")

    def _privileges(self, value: Any) -> None:
        base = f"{_CONTAINER}.Privileges"
        privileges = self.node(base, value, KNOWN_PRIVILEGES_KEYS)
        if privileges is None:
            return
        credential = self.node(
            f"{base}.CredentialSpec", privileges.get("CredentialSpec"), KNOWN_CREDENTIAL_SPEC_KEYS
        )
        if credential is not None and not is_blank(credential):
            self.add("unknown_option", f"{base}.CredentialSpec")
        selinux = self.node(
            f"{base}.SELinuxContext", privileges.get("SELinuxContext"), KNOWN_SELINUX_KEYS
        )
        if selinux is not None and not is_blank(selinux):
            self.add("security_opt", f"{base}.SELinuxContext")
        seccomp = self.node(f"{base}.Seccomp", privileges.get("Seccomp"), KNOWN_SECCOMP_KEYS)
        if seccomp is not None and (
            seccomp.get("Mode") not in (None, *_SAFE_MODES) or not is_empty(seccomp.get("Profile"))
        ):
            self.add("security_opt", f"{base}.Seccomp")
        apparmor = self.node(f"{base}.AppArmor", privileges.get("AppArmor"), KNOWN_APPARMOR_KEYS)
        if apparmor is not None and apparmor.get("Mode") not in (None, *_SAFE_MODES):
            self.add("security_opt", f"{base}.AppArmor")

    def _references(self, value: Any, group: str, known: frozenset[str]) -> None:
        base = f"{_CONTAINER}.{group}"
        for ordinal, entry in enumerate(self.items(base, value)):
            path = f"{base}[{ordinal}]"
            reference = self.node(path, entry, known)
            if reference is None:
                continue
            self.node(f"{path}.File", reference.get("File"), KNOWN_FILE_KEYS)
            if group == "Configs" and reference.get("Runtime") is not None:
                self.add("unknown_option", f"{path}.Runtime")

    def _mounts(self, value: Any) -> None:
        base = f"{_CONTAINER}.Mounts"
        for ordinal, entry in enumerate(self.items(base, value)):
            row = self.slot("mounts", ordinal)
            path = f"{base}[{ordinal}]"
            type_field, source_field = f"mount-{row}-type", f"mount-{row}-source"
            if not isinstance(entry, Mapping):
                self.add("mount_type", path, type_field)
                continue
            self.keys(path, entry, KNOWN_MOUNT_KEYS)
            mount_type = entry.get("Type")
            if isinstance(mount_type, str) and mount_type.strip().casefold() == "bind":
                self.add("bind_mount", f"{path}.Type", type_field)
            elif mount_type not in ("volume", "tmpfs"):
                self.add("mount_type", f"{path}.Type", type_field)
            elif mount_type == "volume":
                source = entry.get("Source")
                if not is_empty(source) and (
                    not isinstance(source, str) or VOLUME_NAME.fullmatch(source) is None
                ):
                    self.add("bind_mount", f"{path}.Source", source_field)
            self._mount_options(path, entry, row)

    def _mount_options(self, path: str, entry: Mapping[str, Any], row: int) -> None:
        bind = self.node(f"{path}.BindOptions", entry.get("BindOptions"), KNOWN_MOUNT_BIND_KEYS)
        if bind is not None and not is_blank(bind):
            self.add("bind_mount", f"{path}.BindOptions", f"mount-{row}-type")
        volume = self.node(
            f"{path}.VolumeOptions", entry.get("VolumeOptions"), KNOWN_VOLUME_OPTION_KEYS
        )
        if volume is not None:
            driver = self.node(
                f"{path}.VolumeOptions.DriverConfig",
                volume.get("DriverConfig"),
                KNOWN_DRIVER_CONFIG_KEYS,
            )
            if driver is not None and not is_blank(driver):
                self.add(
                    "volume_driver", f"{path}.VolumeOptions.DriverConfig", f"mount-{row}-source"
                )
        self.node(f"{path}.ImageOptions", entry.get("ImageOptions"), KNOWN_MOUNT_IMAGE_KEYS)
        self.node(f"{path}.TmpfsOptions", entry.get("TmpfsOptions"), KNOWN_TMPFS_OPTION_KEYS)
        cluster = entry.get("ClusterOptions")
        if cluster is not None and not isinstance(cluster, Mapping):
            self.add("unknown_option", f"{path}.ClusterOptions")
        elif isinstance(cluster, Mapping):
            self.keys(f"{path}.ClusterOptions", cluster, frozenset())


@dataclass(frozen=True, slots=True)
class ServiceReferences:
    """The objects a service spec points at, in spec order.

    networks hold (target, spec path); volumes (source, read only, target path) for named
    volumes only; configs and secrets (id, name, file name).
    """

    networks: tuple[tuple[str, str], ...] = ()
    volumes: tuple[tuple[str, bool, str], ...] = ()
    configs: tuple[tuple[str, str, str], ...] = ()
    secrets: tuple[tuple[str, str, str], ...] = ()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _listed(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _entries(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    return [entry for entry in value if isinstance(entry, Mapping)]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _container_of(spec: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(_mapping(spec.get("TaskTemplate")).get("ContainerSpec"))


def references(spec: Mapping[str, Any]) -> ServiceReferences:
    """Return the networks, named volumes, configs and secrets a spec references."""
    task = _mapping(spec.get("TaskTemplate"))
    container = _container_of(spec)
    networks = tuple(
        (_text(entry.get("Target")), f"TaskTemplate.Networks[{ordinal}]")
        for ordinal, entry in enumerate(_listed(task.get("Networks")))
        if isinstance(entry, Mapping) and _text(entry.get("Target"))
    )
    volumes = tuple(
        (_text(entry.get("Source")), entry.get("ReadOnly") is True, _text(entry.get("Target")))
        for entry in _entries(container.get("Mounts"))
        if entry.get("Type") == "volume" and _text(entry.get("Source"))
    )
    configs = tuple(
        (
            _text(entry.get("ConfigID")),
            _text(entry.get("ConfigName")),
            _text(_mapping(entry.get("File")).get("Name")),
        )
        for entry in _entries(container.get("Configs"))
    )
    secrets = tuple(
        (
            _text(entry.get("SecretID")),
            _text(entry.get("SecretName")),
            _text(_mapping(entry.get("File")).get("Name")),
        )
        for entry in _entries(container.get("Secrets"))
    )
    return ServiceReferences(networks, volumes, configs, secrets)


def _new(after: tuple[Any, ...], before: tuple[Any, ...], key: Any = None) -> tuple[Any, ...]:
    known = {key(item) if key else item for item in before}
    return tuple(item for item in after if (key(item) if key else item) not in known)


def added_references(
    before: Mapping[str, Any] | None, after: Mapping[str, Any]
) -> ServiceReferences:
    """Return the references in after that before does not already cover; all when before is None.

    A mount that was writable and becomes read-only is covered; the reverse is added.
    """
    current = references(after)
    if before is None:
        return current
    held = references(before)
    return ServiceReferences(
        networks=_new(current.networks, held.networks, key=lambda item: item[0]),
        volumes=tuple(
            volume
            for volume in current.volumes
            if not any(
                (source, target) == (volume[0], volume[2]) and (volume[1] or not read_only)
                for source, read_only, target in held.volumes
            )
        ),
        configs=_new(current.configs, held.configs),
        secrets=_new(current.secrets, held.secrets),
    )


def parse_constraint(text: str) -> tuple[str, str, str] | None:
    for operator in ("==", "!="):
        index = text.find(operator)
        if index >= 0:
            key, value = text[:index].strip(), text[index + len(operator) :].strip()
            if _CONSTRAINT_KEY.fullmatch(key) and _CONSTRAINT_VALUE.fullmatch(value):
                return key, operator, value
            return None
    return None


def constraints_of(spec: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the placement constraints of a spec as written."""
    placement = _mapping(_mapping(spec.get("TaskTemplate")).get("Placement"))
    raw = placement.get("Constraints")
    return tuple(item for item in raw if isinstance(item, str)) if isinstance(raw, list) else ()


def pinned_to(spec: Mapping[str, Any], node_id: str) -> bool:
    """Report whether the constraints confine every task to the node with this id.

    Constraints are ANDed, so one equality on the node id is enough.
    """
    for text in constraints_of(spec):
        parsed = parse_constraint(text)
        if (
            parsed is not None
            and parsed[1] == "=="
            and parsed[0].casefold() == "node.id"
            and parsed[2].casefold() == node_id.casefold()
        ):
            return True
    return False


def volume_pin_exempt(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> bool:
    """Report whether after holds no named mount or constraint change relative to before.

    A caller needs the pin only when references(after).volumes is non-empty.
    """
    if before is None:
        return False
    held = set(references(before).volumes)
    if any(volume not in held for volume in references(after).volumes):
        return False
    return constraints_of(before) == constraints_of(after)


def volume_placement_findings(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
    manager_node_id: str | None,
    *,
    slots: Mapping[str, Sequence[int]] | None = None,
) -> list[Finding]:
    """Return one finding per named mount when the service is not pinned to this manager."""
    volumes = references(after).volumes
    if not volumes or (manager_node_id is not None and pinned_to(after, manager_node_id)):
        return []
    if volume_pin_exempt(before, after):
        return []
    findings: list[Finding] = []
    for ordinal, entry in enumerate(_listed(_container_of(after).get("Mounts"))):
        if (
            not isinstance(entry, Mapping)
            or entry.get("Type") != "volume"
            or not _text(entry.get("Source"))
        ):
            continue
        row = _slot(slots, "mounts", ordinal)
        findings.append(
            Finding(
                "swarm_volume_placement",
                f"{_CONTAINER}.Mounts[{ordinal}].Source",
                f"mount-{row}-source",
            )
        )
    return findings


def _slot(slots: Mapping[str, Sequence[int]] | None, group: str, ordinal: int) -> int:
    rows = (slots or {}).get(group)
    return rows[ordinal] if rows is not None and ordinal < len(rows) else ordinal
