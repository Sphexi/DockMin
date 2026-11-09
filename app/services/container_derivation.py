"""Recreate configuration derived from a container inspect document, and duplicate prefill."""

import copy
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal

from pydantic import ValidationError

from app.auth.errors import ValidationFailed
from app.logging import DEFAULT_SCRUBBER
from app.services.access import RESERVED_LABELS
from app.services.container_restrictions import (
    ALLOWED_LOG_DRIVERS,
    ALLOWED_LOG_OPTION_KEYS,
    VOLUME_NAME,
    find_restricted,
    is_blank,
    option_label,
    refused_options,
)
from app.services.container_spec import (
    DNS_LABEL,
    GROUPS,
    ROW_PREFIX,
    ContainerSpec,
    HealthcheckSpec,
    locate_row_field,
    parse_memory,
)

Blocker = Literal[
    "labels_truncated", "swarm_managed", "auto_remove", "image_id_reference", "hex_name"
]
NoticeKind = Literal[
    "labels_dropped",
    "restricted",
    "withheld",
    "not_copied",
    "dropped_invalid",
    "anonymous_volumes",
    "converted",
]

SWARM_SERVICE_LABEL: Final = "com.docker.swarm.service.id"
STACK_LABEL_PREFIXES: Final = ("com.docker.compose.", "com.docker.stack.", "com.docker.swarm.")
DEFAULT_SHM_BYTES: Final = 64 * 1024 * 1024
DEFAULT_RUNTIME: Final = "runc"

BLOCKER_MESSAGES: Final[dict[Blocker, str]] = {
    "labels_truncated": "This container has more label data than can be preserved.",
    "swarm_managed": "This container is managed by a Swarm service.",
    "auto_remove": "This container is removed when it stops, so there is nothing to roll back to.",
    "image_id_reference": "This container was created from an image id, which cannot be pulled.",
    "hex_name": "This container's name is 64 hexadecimal digits, which Docker reads as an id.",
}

_IMAGE_ID: Final = re.compile(r"(?:sha256:)?[0-9a-f]{12,64}")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_IMAGE_DEFAULT_KEYS: Final = (
    "Cmd",
    "Entrypoint",
    "WorkingDir",
    "User",
    "Healthcheck",
    "StopSignal",
    "Shell",
)
_CORE_MASKED: Final = frozenset(
    [
        "/proc/acpi",
        "/proc/kcore",
        "/proc/keys",
        "/proc/latency_stats",
        "/proc/timer_list",
        "/proc/sched_debug",
        "/proc/scsi",
        "/sys/firmware",
    ]
)
_DEFAULT_PATH_ROOTS: Final = ("/proc/", "/sys/")
_CORE_READONLY: Final = frozenset(
    ["/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"]
)
_NOTICE_PREFIX: Final[dict[NoticeKind, str]] = {
    "labels_dropped": "Not copied, a duplicate is not a member of a stack: ",
    "restricted": "Not copied, only administrators may set them: ",
    "withheld": "Withheld from your view and not copied: ",
    "not_copied": "Not copied, this form cannot set them: ",
    "dropped_invalid": "Not copied, the values are not valid in this form: ",
    "anonymous_volumes": "Anonymous volumes are created new and empty at: ",
    "converted": "Converted to the form this page uses: ",
}


@dataclass(frozen=True, slots=True)
class DaemonDefaults:
    """Host settings an inspect document reports for a container that never set them."""

    runtime: str | None = None
    logging_driver: str | None = None
    cgroup_version: str | None = None


@dataclass(frozen=True, slots=True)
class Derivation:
    """A recreate configuration and the facts the caller needs about it.

    body is the Engine create body, name the container's own name, own_volumes and
    own_network_ids what the old container already uses, and anonymous_volumes the names carried.
    """

    name: str
    body: dict[str, Any]
    image: str
    image_defaults_kept: bool
    own_volumes: frozenset[str]
    own_network_ids: frozenset[str]
    anonymous_volumes: tuple[str, ...]
    old_short_id: str


@dataclass(frozen=True, slots=True)
class Notice:
    """One statement the duplicate form shows about what was not carried over."""

    kind: NoticeKind
    items: tuple[str, ...]
    options: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """Return the sentence shown on the page."""
        return f"{_NOTICE_PREFIX[self.kind]}{', '.join(self.items)}."


@dataclass(frozen=True, slots=True)
class Duplicate:
    """A prefilled spec for the create form, the notices to show with it, and a suggested name."""

    spec: ContainerSpec
    notices: tuple[Notice, ...]
    suggested_name: str


def image_is_id(reference: str) -> bool:
    """Report whether an image reference is a bare image id."""
    return _IMAGE_ID.fullmatch(reference) is not None


def recreate_blockers(inspect: Mapping[str, Any], *, pull: bool) -> tuple[Blocker, ...]:
    """Return the reasons this container cannot be recreated, empty when it can."""
    config = _mapping(inspect.get("Config"))
    host = _mapping(inspect.get("HostConfig"))
    labels = _mapping(config.get("Labels"))
    blockers: list[Blocker] = []
    if inspect.get("LabelsTruncated") is True:
        blockers.append("labels_truncated")
    if any(isinstance(k, str) and k.strip().casefold() == SWARM_SERVICE_LABEL for k in labels):
        blockers.append("swarm_managed")
    if host.get("AutoRemove") is True:
        blockers.append("auto_remove")
    image = config.get("Image")
    if pull and isinstance(image, str) and image_is_id(image):
        blockers.append("image_id_reference")
    if _HEX64.fullmatch(_name(inspect)):
        blockers.append("hex_name")
    return tuple(blockers)


def derive_recreate(
    inspect: Mapping[str, Any],
    *,
    image_inspect: Mapping[str, Any] | None = None,
    defaults: DaemonDefaults | None = None,
) -> Derivation:
    """Derive the Engine create body that reproduces a container.

    inspect is the decoded document of GET /containers/{id}/json; image_inspect is that of the
    container's image, or None when the image is gone. With defaults, settings equal to the
    daemon's own are left out of the body, so the new container takes the daemon's.
    """
    config, host, kept = _prepare(inspect, image_inspect, defaults)
    short = _short_id(inspect)
    declared = _declared_destinations(host)
    anonymous: list[str] = []
    own_volumes: set[str] = set()
    mounts = list(host["Mounts"]) if isinstance(host.get("Mounts"), list) else []
    unnamed = {
        entry["Target"]: index
        for index, entry in enumerate(mounts)
        if isinstance(entry, Mapping)
        and entry.get("Type") == "volume"
        and not entry.get("Source")
        and isinstance(entry.get("Target"), str)
    }
    for mount in _list(inspect.get("Mounts")):
        if not isinstance(mount, Mapping) or mount.get("Type") != "volume":
            continue
        name, destination = mount.get("Name"), mount.get("Destination")
        if not isinstance(name, str) or not name:
            continue
        own_volumes.add(name)
        if isinstance(destination, str) and destination in unnamed:
            index = unnamed.pop(destination)
            mounts[index] = {**mounts[index], "Source": name}
            anonymous.append(name)
        elif isinstance(destination, str) and destination and destination not in declared:
            entry: dict[str, Any] = {"Type": "volume", "Source": name, "Target": destination}
            if mount.get("RW") is False:
                entry["ReadOnly"] = True
            mounts.append(entry)
            anonymous.append(name)
            declared.add(destination)
    if mounts:
        host["Mounts"] = mounts
    endpoints = _endpoints(inspect, host, short)
    body = _compose(config, host, endpoints)
    for volume in _binds_volume_names(host):
        own_volumes.add(volume)
    return Derivation(
        name=_name(inspect),
        body=body,
        image=str(config.get("Image", "")),
        image_defaults_kept=kept,
        own_volumes=frozenset(own_volumes),
        own_network_ids=frozenset(endpoints),
        anonymous_volumes=tuple(anonymous),
        old_short_id=short,
    )


def spec_from_inspect(
    inspect: Mapping[str, Any],
    *,
    is_administrator: bool,
    image_inspect: Mapping[str, Any] | None = None,
    image_reference: str | None = None,
    defaults: DaemonDefaults | None = None,
) -> Duplicate:
    """Build the prefilled spec for duplicating a container.

    A standard user's prefill follows the detail page: no environment, command or entrypoint,
    sensitive label values blanked, and nothing only administrators may set. Stack, Compose and
    Swarm labels are dropped for everyone. image_reference stands in for a Config.Image that is a
    bare image id. defaults names the daemon's own settings, which are not treated as choices.
    """
    return _Duplicator(
        inspect, is_administrator, image_inspect, image_reference, defaults or DaemonDefaults()
    ).run()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _network_name(mode: str, networks: Mapping[str, Any]) -> str:
    """Return the attached network's name when `NetworkMode` names it by id."""
    if not mode or mode in networks:
        return mode
    for name, endpoint in networks.items():
        network_id = _mapping(endpoint).get("NetworkID")
        if isinstance(network_id, str) and network_id and network_id.startswith(mode):
            return str(name)
    return mode


def _str_list(value: Any) -> list[str]:
    return [item for item in _list(value) if isinstance(item, str)]


def _short_id(inspect: Mapping[str, Any]) -> str:
    return str(inspect.get("Id", ""))[:12]


def _name(inspect: Mapping[str, Any]) -> str:
    return str(inspect.get("Name", "")).removeprefix("/")


def _prepare(
    inspect: Mapping[str, Any],
    image_inspect: Mapping[str, Any] | None,
    defaults: DaemonDefaults | None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    config = copy.deepcopy(dict(_mapping(inspect.get("Config"))))
    host = copy.deepcopy(dict(_mapping(inspect.get("HostConfig"))))
    if not config.get("Image"):
        config["Image"] = inspect.get("Image", "")
    short = _short_id(inspect)
    if short and config.get("Hostname") == short:
        del config["Hostname"]
    config.pop("MacAddress", None)
    host.pop("ContainerIDFile", None)
    mode = host.get("NetworkMode")
    if isinstance(mode, str) and mode.startswith("container:"):
        # The Engine refuses a hostname or domain name on a container that shares another's network.
        config.pop("Hostname", None)
        config.pop("Domainname", None)
    # Inspect reports the daemon's default path lists, which differ by version; sending them back
    # would read as an override.
    if _holds_all(host.get("MaskedPaths"), _CORE_MASKED):
        host.pop("MaskedPaths")
    if _holds_all(host.get("ReadonlyPaths"), _CORE_READONLY):
        host.pop("ReadonlyPaths")
    if defaults is not None:
        _drop_daemon_defaults(host, defaults)
    image_config = _mapping(image_inspect).get("Config")
    if not isinstance(image_config, Mapping):
        return config, host, True
    _subtract_image_defaults(config, image_config)
    return config, host, False


def _holds_all(value: Any, required: frozenset[str]) -> bool:
    """Whether value is a daemon default list: the core entries and nothing outside /proc or /sys.

    An entry elsewhere was set for this container, and dropping it would unmask that path.
    """
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        return False
    return required <= set(value) and all(p.startswith(_DEFAULT_PATH_ROOTS) for p in value)


def _drop_daemon_defaults(host: dict[str, Any], defaults: DaemonDefaults) -> None:
    log = host.get("LogConfig")
    if (
        isinstance(log, dict)
        and defaults.logging_driver
        and log.get("Type") == defaults.logging_driver
        and is_blank(log.get("Config"))
    ):
        del host["LogConfig"]
    if defaults.cgroup_version == "1" and host.get("CgroupnsMode") == "host":
        del host["CgroupnsMode"]


def _subtract_image_defaults(config: dict[str, Any], image: Mapping[str, Any]) -> None:
    image_env = set(_str_list(image.get("Env")))
    if isinstance(config.get("Env"), list):
        config["Env"] = [e for e in config["Env"] if e not in image_env]
        if not config["Env"]:
            del config["Env"]
    image_labels = _mapping(image.get("Labels"))
    if isinstance(config.get("Labels"), dict):
        config["Labels"] = {
            k: v
            for k, v in config["Labels"].items()
            if not (k in image_labels and image_labels[k] == v)
        }
        if not config["Labels"]:
            del config["Labels"]
    for key in ("ExposedPorts", "Volumes"):
        image_keys = _mapping(image.get(key))
        if isinstance(config.get(key), dict):
            config[key] = {k: v for k, v in config[key].items() if k not in image_keys}
            if not config[key]:
                del config[key]
    for key in _IMAGE_DEFAULT_KEYS:
        if key in config and config[key] == image.get(key):
            del config[key]


def _declared_destinations(host: Mapping[str, Any]) -> set[str]:
    declared: set[str] = set()
    for entry in _str_list(host.get("Binds")):
        parts = entry.split(":")
        declared.add(parts[1] if len(parts) >= 2 else parts[0])
    for mount in _list(host.get("Mounts")):
        if isinstance(mount, Mapping) and isinstance(mount.get("Target"), str):
            declared.add(mount["Target"])
    return declared


def _binds_volume_names(host: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for entry in _str_list(host.get("Binds")):
        source = entry.split(":", 1)[0]
        if ":" in entry and VOLUME_NAME.fullmatch(source):
            names.add(source)
    return names


def _endpoints(
    inspect: Mapping[str, Any], host: Mapping[str, Any], short: str
) -> dict[str, dict[str, Any]]:
    mode = host.get("NetworkMode")
    if isinstance(mode, str) and (mode in ("host", "none") or mode.startswith("container:")):
        return {}
    networks = _mapping(_mapping(inspect.get("NetworkSettings")).get("Networks"))
    container_id = str(inspect.get("Id", ""))
    endpoints: dict[str, dict[str, Any]] = {}
    for name, endpoint in networks.items():
        if not isinstance(endpoint, Mapping) or name in ("host", "none"):
            continue
        network_id = endpoint.get("NetworkID")
        key = network_id if isinstance(network_id, str) and network_id else name
        entry: dict[str, Any] = {}
        if not is_blank(endpoint.get("IPAMConfig")):
            entry["IPAMConfig"] = copy.deepcopy(endpoint["IPAMConfig"])
        if not is_blank(endpoint.get("Links")):
            entry["Links"] = copy.deepcopy(endpoint["Links"])
        aliases = [a for a in _str_list(endpoint.get("Aliases")) if a not in (short, container_id)]
        if aliases:
            entry["Aliases"] = aliases
        if not is_blank(endpoint.get("DriverOpts")):
            entry["DriverOpts"] = copy.deepcopy(endpoint["DriverOpts"])
        priority = endpoint.get("GwPriority")
        if isinstance(priority, int) and not isinstance(priority, bool) and priority != 0:
            entry["GwPriority"] = priority
        endpoints[key] = entry
    return endpoints


def _compose(
    config: Mapping[str, Any], host: Mapping[str, Any], endpoints: Mapping[str, Any]
) -> dict[str, Any]:
    body: dict[str, Any] = dict(config)
    body["HostConfig"] = dict(host)
    if endpoints:
        body["NetworkingConfig"] = {"EndpointsConfig": {k: dict(v) for k, v in endpoints.items()}}
    return body


class _Duplicator:
    def __init__(
        self,
        inspect: Mapping[str, Any],
        is_administrator: bool,
        image_inspect: Mapping[str, Any] | None,
        image_reference: str | None,
        defaults: DaemonDefaults,
    ) -> None:
        self.inspect = inspect
        self.admin = is_administrator
        self.default_runtime = defaults.runtime or DEFAULT_RUNTIME
        self.image_inspect = image_inspect
        self.image_reference = image_reference
        self.config, self.host, _ = _prepare(inspect, image_inspect, defaults)
        self.short = _short_id(inspect)
        self.kwargs: dict[str, Any] = {}
        self.not_copied: list[str] = []
        self.withheld: list[str] = []
        self.converted: list[str] = []
        self.dropped_labels: list[str] = []
        self.anonymous: list[str] = []
        self.withheld_check: dict[str, Any] | None = None

    def run(self) -> Duplicate:
        endpoints = _endpoints(self.inspect, self.host, self.short)
        restricted = (
            []
            if self.admin
            else find_restricted(
                _compose(self.config, self.host, endpoints), default_runtime=self.default_runtime
            )
        )
        self._image()
        self._process()
        self._labels()
        self._ports()
        self._mounts()
        self._network(endpoints)
        self._resources()
        self._security()
        self._dns_and_hosts()
        self._healthcheck()
        self._log()
        self._leftovers(restricted)
        spec, dropped = _build(self.kwargs)
        if self.withheld_check is not None:
            # A command-mode check without its command fails the cross checks on purpose: the
            # form keeps the mode and timings and asks for the command again.
            spec = spec.model_copy(
                update={"healthcheck": self._withheld_check(self.withheld_check)}
            )
        notices: list[Notice] = []
        if self.dropped_labels:
            notices.append(Notice("labels_dropped", tuple(sorted(set(self.dropped_labels)))))
        if restricted:
            options = refused_options(restricted)
            notices.append(Notice("restricted", tuple(option_label(o) for o in options), options))
        if self.withheld:
            notices.append(Notice("withheld", tuple(dict.fromkeys(self.withheld))))
        if self.not_copied:
            notices.append(Notice("not_copied", tuple(sorted(set(self.not_copied)))))
        if dropped:
            notices.append(Notice("dropped_invalid", dropped))
        if self.anonymous:
            notices.append(Notice("anonymous_volumes", tuple(self.anonymous)))
        if self.converted:
            notices.append(Notice("converted", tuple(self.converted)))
        name = _name(self.inspect)
        return Duplicate(spec, tuple(notices), _suggested_name(name))

    def _take(self, source: dict[str, Any], key: str) -> Any:
        return source.pop(key, None)

    def _image(self) -> None:
        image = str(self.config.pop("Image", "") or "")
        if image_is_id(image) and self.image_reference:
            image = self.image_reference
        self.kwargs["image"] = image
        self.kwargs["pull"] = "missing"
        self.kwargs["start"] = True

    def _process(self) -> None:
        command = _str_list(self._take(self.config, "Cmd"))
        entrypoint = _str_list(self._take(self.config, "Entrypoint"))
        env = _str_list(self._take(self.config, "Env"))
        if self.admin:
            self.kwargs["command"] = tuple(command) or None
            self.kwargs["entrypoint"] = tuple(entrypoint) or None
            self.kwargs["env"] = [_env_row(entry) for entry in env]
        else:
            for present, label in (
                (command, "command"),
                (entrypoint, "entrypoint"),
                (env, "environment variables"),
            ):
                if present:
                    self.withheld.append(label)
        self.kwargs["working_dir"] = self._take(self.config, "WorkingDir") or None
        self.kwargs["user"] = self._take(self.config, "User") or None

    def _labels(self) -> None:
        labels = self._take(self.config, "Labels")
        rows: list[dict[str, Any]] = []
        for key, value in (labels if isinstance(labels, dict) else {}).items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            folded = key.strip().casefold()
            if folded in {label.casefold() for label in RESERVED_LABELS} or folded.startswith(
                STACK_LABEL_PREFIXES
            ):
                self.dropped_labels.append(key)
                continue
            if not self.admin and (
                DEFAULT_SCRUBBER.is_sensitive_label(key)
                or DEFAULT_SCRUBBER.scrub_text(value) != value
            ):
                value = ""
                self.withheld.append("values of labels with sensitive names")
            rows.append({"key": key, "value": value})
        self.kwargs["labels"] = rows

    def _ports(self) -> None:
        bindings = self._take(self.host, "PortBindings")
        rows: list[dict[str, Any]] = []
        bound: set[str] = set()
        if isinstance(bindings, dict):
            for key in sorted(bindings, key=_port_order):
                port, _, protocol = str(key).partition("/")
                entries = bindings[key]
                bound.add(str(key))
                for entry in entries if isinstance(entries, list) else []:
                    if isinstance(entry, Mapping):
                        rows.append(
                            {
                                "container_port": port,
                                "protocol": protocol or "tcp",
                                "host_ip": str(entry.get("HostIp") or ""),
                                "host_port": str(entry.get("HostPort") or ""),
                            }
                        )
        exposed = self._take(self.config, "ExposedPorts")
        if isinstance(exposed, dict) and set(exposed) - bound:
            self.not_copied.append("Config.ExposedPorts")
        self.kwargs["ports"] = rows
        self.kwargs["publish_all"] = bool(self._take(self.host, "PublishAllPorts"))

    def _mounts(self) -> None:
        rows: list[dict[str, Any]] = []
        declared: set[str] = set()
        for ordinal, mount in enumerate(_list(self._take(self.host, "Mounts"))):
            row = self._mount_row(ordinal, mount)
            if row is not None:
                rows.append(row)
                declared.add(row["target"])
        for entry in _str_list(self._take(self.host, "Binds")):
            row = self._bind_row(entry)
            if row is not None:
                rows.append(row)
                declared.add(row["target"])
        tmpfs = self._take(self.host, "Tmpfs")
        for target, options in (tmpfs if isinstance(tmpfs, dict) else {}).items():
            rows.append(self._tmpfs_row(target, options))
            declared.add(target)
        image_volumes = _mapping(
            _mapping(_mapping(self.image_inspect).get("Config")).get("Volumes")
        )
        self.config.pop("Volumes", None)
        for mount in _list(self.inspect.get("Mounts")):
            if not isinstance(mount, Mapping) or mount.get("Type") != "volume":
                continue
            destination = mount.get("Destination")
            if not isinstance(destination, str) or destination in declared:
                continue
            if destination not in image_volumes:
                rows.append({"type": "volume", "source": "", "target": destination})
            declared.add(destination)
            self.anonymous.append(destination)
        self.kwargs["mounts"] = rows

    def _mount_row(self, ordinal: int, mount: Any) -> dict[str, Any] | None:
        if not isinstance(mount, Mapping):
            return None
        kind = mount.get("Type")
        row: dict[str, Any] = {
            "type": kind,
            "source": str(mount.get("Source") or ""),
            "target": str(mount.get("Target") or ""),
            "read_only": bool(mount.get("ReadOnly")),
        }
        if kind == "bind":
            return row if self.admin else None
        if kind == "tmpfs":
            options = _mapping(mount.get("TmpfsOptions"))
            size = options.get("SizeBytes")
            row["tmpfs_size"] = size if isinstance(size, int) and size > 0 else None
            row["source"] = ""
            return row
        if kind == "volume":
            if not is_blank(mount.get("VolumeOptions")):
                self.not_copied.append("HostConfig.Mounts[].VolumeOptions")
            if _HEX64.fullmatch(row["source"]):
                row["source"] = ""
                self.anonymous.append(row["target"])
            return row
        self.not_copied.append(f"HostConfig.Mounts[{ordinal}] ({kind})")
        return None

    def _bind_row(self, entry: str) -> dict[str, Any] | None:
        parts = entry.split(":")
        if len(parts) < 2:
            return None
        source, target = parts[0], parts[1]
        options = parts[2].split(",") if len(parts) > 2 else []
        is_volume = VOLUME_NAME.fullmatch(source) is not None
        if not is_volume and not self.admin:
            return None
        if is_volume and _HEX64.fullmatch(source):
            source = ""
            self.anonymous.append(target)
        return {
            "type": "volume" if is_volume else "bind",
            "source": source,
            "target": target,
            "read_only": "ro" in options,
        }

    def _tmpfs_row(self, target: str, options: Any) -> dict[str, Any]:
        row: dict[str, Any] = {"type": "tmpfs", "source": "", "target": target}
        others = False
        for option in str(options or "").split(","):
            if option.startswith("size="):
                try:
                    row["tmpfs_size"] = parse_memory(option[5:], "Tmpfs size")
                except ValueError:
                    others = True
            elif option:
                others = True
        if others:
            self.not_copied.append("HostConfig.Tmpfs")
        return row

    def _network(self, endpoints: Mapping[str, Mapping[str, Any]]) -> None:
        mode = self._take(self.host, "NetworkMode")
        mode = mode if isinstance(mode, str) else ""
        networks = _mapping(_mapping(self.inspect.get("NetworkSettings")).get("Networks"))
        names = [n for n in networks if isinstance(n, str) and n not in ("host", "none")]
        mode = _network_name(mode, networks)
        self.kwargs["network_mode"] = "bridge"
        self.kwargs["networks"] = []
        if mode == "none":
            self.kwargs["network_mode"] = "none"
        elif mode == "host":
            if self.admin:
                self.kwargs["network_mode"] = "host"
        elif mode.startswith("container:"):
            if self.admin:
                self.not_copied.append("HostConfig.NetworkMode")
        elif mode not in ("", "default", "bridge") or set(names) - {"bridge"}:
            primary = mode if mode not in ("", "default", "bridge") else "bridge"
            ordered = [primary, *(n for n in names if n != primary)]
            rows = []
            for name in ordered:
                endpoint = _mapping(networks.get(name))
                aliases = [a for a in _str_list(endpoint.get("Aliases")) if a != self.short]
                valid = [a for a in aliases if DNS_LABEL.fullmatch(a)]
                if len(valid) != len(aliases):
                    self.not_copied.append("NetworkSettings.Networks[].Aliases")
                ipam = _mapping(endpoint.get("IPAMConfig"))
                rows.append(
                    {
                        "name": name,
                        "aliases": tuple(valid),
                        "ipv4": str(ipam.get("IPv4Address") or ""),
                    }
                )
            self.kwargs["network_mode"] = "networks"
            self.kwargs["networks"] = rows
        for key in ("DriverOpts", "Links"):
            if any(endpoint.get(key) for endpoint in endpoints.values()):
                self.not_copied.append(f"NetworkSettings.Networks[].{key}")

    def _resources(self) -> None:
        policy = _mapping(self._take(self.host, "RestartPolicy"))
        name = policy.get("Name")
        if isinstance(name, str) and name not in ("", "no"):
            self.kwargs["restart_policy"] = name
            retries = policy.get("MaximumRetryCount")
            if name == "on-failure" and isinstance(retries, int) and retries > 0:
                self.kwargs["restart_max_retries"] = retries
        nano = self._take(self.host, "NanoCpus")
        if isinstance(nano, int) and nano > 0:
            if nano % 10_000_000 == 0:
                self.kwargs["cpus"] = Decimal(nano) / Decimal(1_000_000_000)
            else:
                self.not_copied.append("HostConfig.NanoCpus")
        for key, field_name in (
            ("CpuShares", "cpu_shares"),
            ("Memory", "memory"),
            ("PidsLimit", "pids_limit"),
        ):
            value = self._take(self.host, key)
            if isinstance(value, int) and value > 0:
                self.kwargs[field_name] = value
        swap = self._take(self.host, "MemorySwap")
        if isinstance(swap, int) and (swap > 0 or swap == -1):
            self.kwargs["memory_swap"] = swap
        shm = self._take(self.host, "ShmSize")
        if isinstance(shm, int) and shm > 0 and shm != DEFAULT_SHM_BYTES:
            self.kwargs["shm_size"] = shm
        init = self._take(self.host, "Init")
        self.kwargs["init"] = init is True

    def _security(self) -> None:
        privileged = self._take(self.host, "Privileged")
        cap_add = _str_list(self._take(self.host, "CapAdd"))
        cap_drop = _str_list(self._take(self.host, "CapDrop"))
        devices = self._take(self.host, "Devices")
        runtime = self._take(self.host, "Runtime")
        pid = self._take(self.host, "PidMode")
        ipc = self._take(self.host, "IpcMode")
        self.kwargs["cap_drop"] = tuple(cap_drop)
        if isinstance(ipc, str) and ipc not in ("", "private", "shareable", "host"):
            self.not_copied.append("HostConfig.IpcMode")
        if isinstance(pid, str) and pid not in ("", "private", "host"):
            self.not_copied.append("HostConfig.PidMode")
        if not self.admin:
            return
        self.kwargs["privileged"] = privileged is True
        self.kwargs["cap_add"] = tuple(cap_add)
        self.kwargs["pid_host"] = pid == "host"
        self.kwargs["ipc_host"] = ipc == "host"
        if isinstance(runtime, str) and runtime and runtime != self.default_runtime:
            self.kwargs["runtime"] = runtime
        rows = []
        for device in _list(devices):
            if isinstance(device, Mapping):
                rows.append(
                    {
                        "host_path": str(device.get("PathOnHost") or ""),
                        "container_path": str(device.get("PathInContainer") or ""),
                        "permissions": str(device.get("CgroupPermissions") or "rwm"),
                    }
                )
        self.kwargs["devices"] = rows

    def _dns_and_hosts(self) -> None:
        self.kwargs["dns"] = {
            "servers": tuple(_str_list(self._take(self.host, "Dns"))),
            "search": tuple(_str_list(self._take(self.host, "DnsSearch"))),
            "options": tuple(_str_list(self._take(self.host, "DnsOptions"))),
        }
        rows = []
        for entry in _str_list(self._take(self.host, "ExtraHosts")):
            name, _, address = entry.partition(":")
            rows.append({"name": name, "address": address})
        self.kwargs["extra_hosts"] = rows

    def _healthcheck(self) -> None:
        check = self._take(self.config, "Healthcheck")
        if not isinstance(check, Mapping) or is_blank(check):
            return
        test = _str_list(check.get("Test"))
        section: dict[str, Any] = {"mode": "image"}
        if test[:1] == ["NONE"]:
            section["mode"] = "none"
        elif test[:1] in (["CMD-SHELL"], ["CMD"]) and len(test) > 1:
            section["mode"] = "command"
            if not self.admin:
                self.withheld.append("health check command")
            elif test[0] == "CMD-SHELL":
                section["test"] = test[1]
            else:
                section["test"] = shlex.join(test[1:])
                self.converted.append("health check command (exec form to shell form)")
        if section["mode"] == "command":
            for key, name in (
                ("Interval", "interval"),
                ("Timeout", "timeout"),
                ("StartPeriod", "start_period"),
                ("StartInterval", "start_interval"),
                ("Retries", "retries"),
            ):
                value = check.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    section[name] = value
        if section["mode"] == "command" and not self.admin:
            self.withheld_check = section
        else:
            self.kwargs["healthcheck"] = section

    def _withheld_check(self, section: dict[str, Any]) -> HealthcheckSpec:
        try:
            return HealthcheckSpec.model_validate(section)
        except (ValidationError, ValidationFailed):
            return HealthcheckSpec(mode="command")

    def _log(self) -> None:
        log = _mapping(self._take(self.host, "LogConfig"))
        driver = log.get("Type")
        options = _mapping(log.get("Config"))
        if driver == "json-file" and not options:
            return
        pairs = [(k, v) for k, v in options.items() if isinstance(k, str) and isinstance(v, str)]
        if not self.admin:
            if driver not in ALLOWED_LOG_DRIVERS:
                return
            pairs = [(k, v) for k, v in pairs if k in ALLOWED_LOG_OPTION_KEYS]
        if isinstance(driver, str) and driver:
            self.kwargs["log"] = {
                "driver": driver,
                "options": [{"key": k, "value": v} for k, v in pairs],
            }

    def _leftovers(self, restricted: Sequence[Any]) -> None:
        covered = {_engine_key(f.path) for f in restricted}
        for key, value in self.config.items():
            if key == "Image" or is_blank(value):
                continue
            if not self.admin and key in covered:
                continue
            self.not_copied.append(f"Config.{key}")
        if self.host.get("CgroupnsMode") in ("", "private"):
            del self.host["CgroupnsMode"]
        for key, value in self.host.items():
            if is_blank(value):
                continue
            if not self.admin and key in covered:
                continue
            self.not_copied.append(f"HostConfig.{key}")


def _engine_key(path: str) -> str:
    parts = path.split(".")
    key = (
        parts[1] if parts[0] in ("HostConfig", "NetworkingConfig") and len(parts) > 1 else parts[0]
    )
    return key.split("[")[0]


def _env_row(entry: str) -> dict[str, Any]:
    key, _, value = entry.partition("=")
    return {"key": key, "value": value}


def _port_order(key: Any) -> tuple[int, str]:
    port, _, protocol = str(key).partition("/")
    return (int(port) if port.isdigit() else 0, protocol)


def _suggested_name(name: str) -> str:
    return f"{name}-copy"[:128] if name else ""


def _build(kwargs: dict[str, Any]) -> tuple[ContainerSpec, tuple[str, ...]]:
    dropped: list[str] = []
    for _ in range(40):
        _slot_rows(kwargs)
        try:
            return ContainerSpec.model_validate(kwargs), tuple(dict.fromkeys(dropped))
        except ValidationError as exc:
            locs = [tuple(item["loc"]) for item in exc.errors()]
            dropped.extend(_drop_by_loc(kwargs, locs))
        except ValidationFailed as exc:
            dropped.extend(_drop_by_field(kwargs, list(exc.fields)))
    raise ValueError("The container's configuration could not be reduced to a valid form.")


def _slot_rows(kwargs: dict[str, Any]) -> None:
    for group in GROUPS:
        rows = _group_rows(kwargs, group)
        for index, row in enumerate(rows):
            row["slot"] = index


def _group_rows(kwargs: dict[str, Any], group: str) -> list[dict[str, Any]]:
    if group == "log_options":
        log = kwargs.get("log")
        return log["options"] if isinstance(log, dict) and "options" in log else []
    rows = kwargs.get(group)
    return rows if isinstance(rows, list) else []


def _remove_rows(kwargs: dict[str, Any], group: str, positions: set[int]) -> None:
    rows = _group_rows(kwargs, group)
    for position in sorted(positions, reverse=True):
        if position < len(rows):
            del rows[position]


def _clear_group(kwargs: dict[str, Any], group: str) -> None:
    if group == "networks" and not kwargs.get("networks"):
        kwargs["network_mode"] = "bridge"
        return
    if group == "log_options":
        if isinstance(kwargs.get("log"), dict):
            kwargs["log"]["options"] = []
    else:
        kwargs[group] = []


def _drop_by_loc(kwargs: dict[str, Any], locs: Sequence[tuple[Any, ...]]) -> list[str]:
    names: list[str] = []
    row_drops: dict[str, set[int]] = {}
    for loc in locs:
        head = str(loc[0])
        if head in ROW_PREFIX and head != "log_options":
            if len(loc) >= 2 and isinstance(loc[1], int):
                row_drops.setdefault(head, set()).add(loc[1])
            else:
                _clear_group(kwargs, head)
            names.append(head)
        elif head == "log":
            if len(loc) >= 3 and loc[1] == "options" and isinstance(loc[2], int):
                row_drops.setdefault("log_options", set()).add(loc[2])
                names.append("log_options")
            elif len(loc) >= 2 and loc[1] == "options":
                _clear_group(kwargs, "log_options")
                names.append("log_options")
            elif isinstance(kwargs.get("log"), dict):
                kwargs["log"].pop("driver", None)
                names.append("log_driver")
        elif head in ("dns", "healthcheck") and isinstance(kwargs.get(head), dict):
            if len(loc) >= 2:
                kwargs[head].pop(str(loc[1]), None)
                names.append(f"{head}_{loc[1]}")
            else:
                kwargs.pop(head, None)
                names.append(head)
        elif head == "image":
            raise ValueError("The container's image reference is not valid in this form.")
        else:
            kwargs.pop(head, None)
            names.append(head)
    for group, positions in row_drops.items():
        _remove_rows(kwargs, group, positions)
    return names


def _drop_by_field(kwargs: dict[str, Any], fields: Sequence[str]) -> list[str]:
    names: list[str] = []
    row_drops: dict[str, set[int]] = {}
    for name in fields:
        located = locate_row_field(name)
        if located is not None:
            group, index = located
            row_drops.setdefault(group, set()).add(index)
            names.append(group)
        elif name in ROW_PREFIX:
            _clear_group(kwargs, name)
            names.append(name)
        elif name.startswith("healthcheck_"):
            kwargs.pop("healthcheck", None)
            names.append("healthcheck")
        elif name == "log_driver":
            _clear_group(kwargs, "log_options")
            names.append("log_options")
        elif name in ("cap_add", "cap_drop"):
            kwargs.pop("cap_add", None)
            names.append("cap_add")
        else:
            kwargs.pop(name, None)
            names.append(name)
    for group, positions in row_drops.items():
        _remove_rows(kwargs, group, positions)
    return names
