"""A Compose document written from the inspect data of a project's containers; pure."""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.services.access import COMPOSE_PROJECT_LABEL, canonical_stack_key
from app.services.stack_document import path_part
from app.services.stack_read_result import (
    NeedsFile,
    Octal,
    ReadOutcome,
    SecretVariables,
    cpu_amount,
    dump_document,
    duration_text,
    finish,
    is_secret_entry,
)

__all__ = ["KNOWN_LOSSES", "generate_compose"]

SERVICE_LABEL: Final = "com.docker.compose.service"
ONEOFF_LABEL: Final = "com.docker.compose.oneoff"
NUMBER_LABEL: Final = "com.docker.compose.container-number"
DEPENDS_LABEL: Final = "com.docker.compose.depends_on"
_COMPOSE_LABELS: Final = "com.docker.compose."

KNOWN_LOSSES: Final = (
    "Comments, anchors and the original layout are not recovered.",
    "A build section is not recovered; a built service is written with the image it ran.",
    "profiles and extends are not recovered.",
    "Values equal to Docker's defaults, and everything the image already sets, are left out.",
    "Settings outside the generated set are not read: logging, dns, extra_hosts, ulimits, "
    "sysctls, security_opt, init, tty, stdin_open, read_only, stop_signal, stop_grace_period "
    "and group_add.",
    "Only environment values are screened for secrets; one in a command or a label is written "
    "as found.",
)

_KEY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ENV_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
_ANONYMOUS: Final = re.compile(r"[0-9a-f]{64}")
_LOCAL_BRIDGES: Final = frozenset({"host", "none", "bridge", "default"})
_UNSPECIFIED: Final = frozenset({"", "0.0.0.0", "::"})
_PLAIN_PROPAGATION: Final = frozenset({"", "rprivate"})
_SIZE: Final = re.compile(r"([0-9]+)([kmg]?)b?", re.IGNORECASE)
_SIZE_UNITS: Final = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
_ALL_DEVICE_ACCESS: Final = "rwm"


class _Refused(Exception):
    def __init__(self, reason: str, path: str) -> None:
        super().__init__(reason)
        self.outcome = NeedsFile(reason, path)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _words(value: object) -> list[str]:
    return [str(word) for word in _items(value)]


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _escaped(text: str) -> str:
    return text.replace("$", "$$")


def generate_compose(
    project: str,
    containers: Sequence[Mapping[str, Any]],
    images: Mapping[str, Mapping[str, Any]],
) -> ReadOutcome:
    """Write one Compose document for the project's containers, or say that none can be written.

    `containers` are container inspect documents carrying the project label; `images` maps an
    image id to its inspect document and is how image defaults are left out. Blocking and
    CPU-bound; call through asyncio.to_thread.
    """
    try:
        return _Generator(project, containers, images).run()
    except _Refused as refused:
        return refused.outcome


class _Names:
    """Assigns Compose keys to the networks or volumes a project's services use."""

    def __init__(self, project: str, names: Sequence[str]) -> None:
        prefix = f"{project}_"
        self.keys: dict[str, str] = {}
        self.project_names: set[str] = set()
        for name in sorted(set(names)):
            rest = name[len(prefix) :] if name.startswith(prefix) else ""
            if _KEY.fullmatch(rest):
                self.keys[name] = rest
                self.project_names.add(name)
        for name in sorted(set(names) - self.project_names):
            key = name
            while key in self.keys.values():
                key = f"external-{key}"
            self.keys[name] = key

    def is_project(self, name: str) -> bool:
        return name in self.project_names


class _Generator:
    def __init__(
        self,
        project: str,
        containers: Sequence[Mapping[str, Any]],
        images: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self.project = project
        self.images = images
        self.secrets = SecretVariables()
        self.losses = list(KNOWN_LOSSES)
        self.networks: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, Any]] = {}
        self.services = self._group(containers)
        self.network_names = _Names(project, self._network_names())
        self.volume_names = _Names(project, [name for name, _ in self._named_volumes()])
        self.volume_drivers = dict(self._named_volumes())

    def run(self) -> ReadOutcome:
        services: dict[str, dict[str, Any]] = {}
        for key in sorted(self.services):
            services[key] = self._service(key, self.services[key])
        self._dependencies(services)
        document: dict[str, Any] = {"services": services}
        if self.networks:
            document["networks"] = dict(sorted(self.networks.items()))
        if self.volumes:
            document["volumes"] = dict(sorted(self.volumes.items()))
        return finish(
            dump_document(document),
            kind="compose",
            source="generated",
            variables=self.secrets.variables,
            losses=self.losses,
        )

    def _group(self, containers: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for container in containers:
            labels = _mapping(_mapping(container.get("Config")).get("Labels"))
            owner = labels.get(COMPOSE_PROJECT_LABEL)
            if not isinstance(owner, str) or canonical_stack_key(owner) != canonical_stack_key(
                self.project
            ):
                raise _Refused("labels_disagree", COMPOSE_PROJECT_LABEL)
            self.project = owner
            if str(labels.get(ONEOFF_LABEL, "False")).casefold() == "true":
                continue
            key = labels.get(SERVICE_LABEL)
            if not isinstance(key, str) or _KEY.fullmatch(key) is None:
                raise _Refused("invalid_service_key", f"services.{path_part(key)}")
            grouped.setdefault(key, []).append(container)
        if not grouped:
            raise _Refused("no_containers", self.project)
        for members in grouped.values():
            members.sort(key=_order)
        return grouped

    def _network_names(self) -> list[str]:
        return [
            name
            for members in self.services.values()
            for name in _mapping(_mapping(members[0].get("NetworkSettings")).get("Networks"))
            if name not in _LOCAL_BRIDGES
        ]

    def _named_volumes(self) -> list[tuple[str, str]]:
        found: dict[str, str] = {}
        for members in self.services.values():
            for mount in _items(members[0].get("Mounts")):
                name = _mapping(mount).get("Name")
                if _mapping(mount).get("Type") == "volume" and _is_named(name):
                    found.setdefault(str(name), str(_mapping(mount).get("Driver") or "local"))
        return list(found.items())

    def _service(self, key: str, members: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        container = members[0]
        config = _mapping(container.get("Config"))
        host = _mapping(container.get("HostConfig"))
        image = self.images.get(str(container.get("Image")))
        if image is None:
            self.losses.append(
                f"services.{key}: the image was not on the host, so its defaults are written out."
            )
        defaults = _mapping(_mapping(image).get("Config"))
        reference = config.get("Image")
        if not isinstance(reference, str) or not reference:
            raise _Refused("image_missing", f"services.{key}.image")
        service: dict[str, Any] = {"image": reference}
        self._commands(service, config, defaults)
        for field, value, inherited in (
            ("user", config.get("User"), defaults.get("User")),
            ("working_dir", config.get("WorkingDir"), defaults.get("WorkingDir")),
        ):
            if value and value != inherited:
                service[field] = value
        hostname = config.get("Hostname")
        if hostname and hostname != str(container.get("Id", ""))[:12]:
            service["hostname"] = hostname
        self._environment(service, key, config, defaults)
        self._labels(service, config, defaults)
        self._ports(service, config, host, defaults)
        self._storage(service, key, container, host, defaults)
        self._attachments(service, key, container, host)
        self._depends(service, config)
        self._policies(service, config, host, defaults)
        if len(members) != 1:
            service["deploy"] = {"replicas": len(members)}
        return service

    def _commands(
        self, service: dict[str, Any], config: Mapping[str, Any], defaults: Mapping[str, Any]
    ) -> None:
        entrypoint, inherited_entrypoint = (
            _words(config.get("Entrypoint")),
            _words(defaults.get("Entrypoint")),
        )
        command, inherited_command = _words(config.get("Cmd")), _words(defaults.get("Cmd"))
        changed = entrypoint != inherited_entrypoint
        if changed:
            service["entrypoint"] = [_escaped(word) for word in entrypoint]
        if command and (command != inherited_command or (changed and entrypoint)):
            service["command"] = [_escaped(word) for word in command]

    def _environment(
        self,
        service: dict[str, Any],
        key: str,
        config: Mapping[str, Any],
        defaults: Mapping[str, Any],
    ) -> None:
        inherited = set(_words(defaults.get("Env")))
        written: dict[str, str] = {}
        for entry in _words(config.get("Env")):
            name, separator, value = entry.partition("=")
            if entry in inherited or not separator:
                continue
            where = f"services.{key}.environment.{path_part(name)}"
            if _ENV_NAME.fullmatch(name) is None:
                raise _Refused("invalid_environment_name", where)
            if value and is_secret_entry(name, value):
                reference = self.secrets.reference(key, name, value)
                if reference is None:
                    raise _Refused("secret_value_unusable", where)
                written[name] = reference
            else:
                written[name] = _escaped(value)
        if written:
            service["environment"] = written

    def _labels(
        self, service: dict[str, Any], config: Mapping[str, Any], defaults: Mapping[str, Any]
    ) -> None:
        inherited = _mapping(defaults.get("Labels"))
        labels = {
            _escaped(str(name)): _escaped(str(value))
            for name, value in sorted(_mapping(config.get("Labels")).items())
            if not str(name).startswith(_COMPOSE_LABELS) and inherited.get(name) != value
        }
        if labels:
            service["labels"] = labels

    def _ports(
        self,
        service: dict[str, Any],
        config: Mapping[str, Any],
        host: Mapping[str, Any],
        defaults: Mapping[str, Any],
    ) -> None:
        bindings = _mapping(host.get("PortBindings"))
        ports: list[Any] = []
        for spec, entries in sorted(bindings.items(), key=lambda item: _port_order(item[0])):
            port, _, protocol = str(spec).partition("/")
            seen: set[tuple[str, str]] = set()
            for entry in _items(entries):
                address = str(_mapping(entry).get("HostIp") or "")
                address = "" if address in _UNSPECIFIED else address
                published = str(_mapping(entry).get("HostPort") or "")
                if (address, published) not in seen:
                    seen.add((address, published))
                    ports.append(_port(port, protocol or "tcp", address, published))
        if ports:
            service["ports"] = ports
        inherited = _mapping(defaults.get("ExposedPorts"))
        exposed = [
            spec
            for spec in _mapping(config.get("ExposedPorts"))
            if spec not in inherited and spec not in bindings
        ]
        if exposed:
            service["expose"] = [_without_tcp(str(spec)) for spec in sorted(exposed)]

    def _storage(
        self,
        service: dict[str, Any],
        key: str,
        container: Mapping[str, Any],
        host: Mapping[str, Any],
        defaults: Mapping[str, Any],
    ) -> None:
        image_volumes = set(_mapping(defaults.get("Volumes")))
        create_time = {
            str(_mapping(mount).get("Target")): _mapping(mount)
            for mount in _items(host.get("Mounts"))
        }
        volumes: list[Any] = []
        for mount in sorted(
            (_mapping(item) for item in _items(container.get("Mounts"))),
            key=lambda item: str(item.get("Destination")),
        ):
            entry = self._mount(mount, create_time, image_volumes)
            if entry is not None:
                volumes.append(entry)
        tmpfs: list[str] = []
        for target, options in sorted(_mapping(host.get("Tmpfs")).items()):
            parsed = self._tmpfs_options(key, str(options))
            if parsed:
                volumes.append({"type": "tmpfs", "target": str(target), "tmpfs": parsed})
            else:
                tmpfs.append(str(target))
        for mount in create_time.values():
            if mount.get("Type") == "tmpfs":
                volumes.append(_tmpfs_mount(mount))
        if volumes:
            service["volumes"] = volumes
        if tmpfs:
            service["tmpfs"] = tmpfs

    def _mount(
        self,
        mount: Mapping[str, Any],
        create_time: Mapping[str, Mapping[str, Any]],
        image_volumes: set[str],
    ) -> Any:
        kind, target = mount.get("Type"), str(mount.get("Destination"))
        read_only = mount.get("RW") is False
        match kind:
            case "volume":
                name = mount.get("Name")
                options = _mapping(_mapping(create_time.get(target)).get("VolumeOptions"))
                if not _is_named(name):
                    if target in image_volumes:
                        return None
                    return (
                        {"type": "volume", "target": target, "read_only": True}
                        if read_only
                        else _escaped(target)
                    )
                source = self._volume_key(str(name))
                volume = {
                    field: options[field]
                    for field, option in (("nocopy", "NoCopy"), ("subpath", "Subpath"))
                    if options.get(option)
                }
                if volume:
                    entry: dict[str, Any] = {"type": "volume", "source": source, "target": target}
                    entry["volume"] = volume
                    return {**entry, **({"read_only": True} if read_only else {})}
                return _short(source, target, read_only, "")
            case "bind":
                source = str(mount.get("Source"))
                propagation = str(mount.get("Propagation") or "")
                if ":" in source or ":" in target or "," in source:
                    entry = {"type": "bind", "source": _escaped(source), "target": target}
                    if read_only:
                        entry["read_only"] = True
                    if propagation not in _PLAIN_PROPAGATION:
                        entry["bind"] = {"propagation": propagation}
                    return entry
                return _short(
                    _escaped(source),
                    _escaped(target),
                    read_only,
                    "" if propagation in _PLAIN_PROPAGATION else propagation,
                )
            case "tmpfs":
                return None
        self.losses.append(f"A {kind} mount at {target} is not a Compose volume and was left out.")
        return None

    def _volume_key(self, name: str) -> str:
        key = self.volume_names.keys[name]
        if key not in self.volumes:
            if self.volume_names.is_project(name):
                driver = self.volume_drivers.get(name, "local")
                self.volumes[key] = {} if driver == "local" else {"driver": driver}
            else:
                self.volumes[key] = {"external": True, **({"name": name} if key != name else {})}
        return key

    def _tmpfs_options(self, key: str, text: str) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for option in filter(None, text.split(",")):
            name, _, value = option.partition("=")
            if name == "size" and (match := _SIZE.fullmatch(value)):
                parsed["size"] = int(match[1]) * _SIZE_UNITS[match[2].lower()]
            elif name == "mode" and value.isdigit():
                parsed["mode"] = Octal(int(value, 8))
            else:
                self.losses.append(f"services.{key}: the tmpfs option {name} was left out.")
        return parsed

    def _attachments(
        self,
        service: dict[str, Any],
        key: str,
        container: Mapping[str, Any],
        host: Mapping[str, Any],
    ) -> None:
        mode = str(host.get("NetworkMode") or "")
        if mode in _LOCAL_BRIDGES:
            service["network_mode"] = "bridge" if mode == "default" else mode
            return
        if mode.startswith("container:"):
            self.losses.append(f"services.{key}: network_mode {mode} was left out.")
            return
        attached = _mapping(_mapping(container.get("NetworkSettings")).get("Networks"))
        ignored = {
            str(container.get("Name", "")).lstrip("/"),
            str(container.get("Id", ""))[:12],
            key,
        }
        entries: dict[str, Any] = {}
        for name, attachment in sorted(attached.items()):
            network = self._network_key(name)
            aliases = [
                alias
                for alias in dict.fromkeys(_words(_mapping(attachment).get("Aliases")))
                if alias not in ignored
            ]
            entries[network] = {"aliases": aliases} if aliases else {}
        if list(entries) == ["default"] and not entries["default"]:
            return
        if entries:
            service["networks"] = entries

    def _network_key(self, name: str) -> str:
        key = self.network_names.keys[name]
        if self.network_names.is_project(name):
            if key != "default" and key not in self.networks:
                self.networks[key] = {}
                self._note_network_definitions()
        elif key not in self.networks:
            self.networks[key] = {"external": True, **({"name": name} if key != name else {})}
        return key

    def _note_network_definitions(self) -> None:
        text = (
            "Network drivers, options and address pools are not read; "
            "project networks use defaults."
        )
        if text not in self.losses:
            self.losses.append(text)

    def _depends(self, service: dict[str, Any], config: Mapping[str, Any]) -> None:
        label = _mapping(config.get("Labels")).get(DEPENDS_LABEL)
        if not isinstance(label, str):
            return
        entries: dict[str, Any] = {}
        for item in filter(None, label.split(",")):
            name, _, rest = item.partition(":")
            condition, _, restart = rest.partition(":")
            entry: dict[str, Any] = {"condition": condition or "service_started"}
            if restart == "true":
                entry["restart"] = True
            entries[name] = entry
        if entries:
            service["depends_on"] = entries

    def _dependencies(self, services: dict[str, dict[str, Any]]) -> None:
        for key, service in services.items():
            wanted = service.get("depends_on")
            if wanted is None:
                continue
            kept = {name: entry for name, entry in wanted.items() if name in services}
            for name in wanted.keys() - kept.keys():
                self.losses.append(f"services.{key}: depends_on {name} has no container.")
            if not kept:
                del service["depends_on"]
            elif all(entry == {"condition": "service_started"} for entry in kept.values()):
                service["depends_on"] = sorted(kept)
            else:
                service["depends_on"] = kept

    def _policies(
        self,
        service: dict[str, Any],
        config: Mapping[str, Any],
        host: Mapping[str, Any],
        defaults: Mapping[str, Any],
    ) -> None:
        policy = _mapping(host.get("RestartPolicy"))
        match policy.get("Name"):
            case "always" | "unless-stopped" as name:
                service["restart"] = name
            case "on-failure":
                attempts = _count(policy.get("MaximumRetryCount"))
                service["restart"] = f"on-failure:{attempts}" if attempts else "on-failure"
        health = _mapping(config.get("Healthcheck"))
        if health and health != _mapping(defaults.get("Healthcheck")):
            service["healthcheck"] = _healthcheck(health)
        self._limits(service, host)
        for field, key in (("cap_add", "CapAdd"), ("cap_drop", "CapDrop")):
            if names := _words(host.get(key)):
                service[field] = names
        if devices := [_device(_mapping(item)) for item in _items(host.get("Devices"))]:
            service["devices"] = devices
        if host.get("Privileged") is True:
            service["privileged"] = True

    def _limits(self, service: dict[str, Any], host: Mapping[str, Any]) -> None:
        memory = _count(host.get("Memory"))
        swap = _count(host.get("MemorySwap"))
        for field, value in (
            ("mem_limit", memory),
            ("mem_reservation", _count(host.get("MemoryReservation"))),
            ("memswap_limit", swap if swap == -1 or (swap > 0 and swap != 2 * memory) else 0),
            ("cpu_shares", _count(host.get("CpuShares"))),
            ("pids_limit", _count(host.get("PidsLimit"))),
        ):
            if value > 0 or (field == "memswap_limit" and value == -1):
                service[field] = value
        if nano := _count(host.get("NanoCpus")):
            service["cpus"] = cpu_amount(nano)
        if cpuset := host.get("CpusetCpus"):
            service["cpuset"] = cpuset


def _order(container: Mapping[str, Any]) -> tuple[int, str]:
    labels = _mapping(_mapping(container.get("Config")).get("Labels"))
    number = str(labels.get(NUMBER_LABEL, ""))
    return (int(number) if number.isdigit() else 0, str(container.get("Name", "")))


def _is_named(name: object) -> bool:
    return isinstance(name, str) and bool(name) and _ANONYMOUS.fullmatch(name) is None


def _short(source: str, target: str, read_only: bool, propagation: str) -> str:
    modes = [*(["ro"] if read_only else []), *([propagation] if propagation else [])]
    return ":".join([source, target, *([",".join(modes)] if modes else [])])


def _tmpfs_mount(mount: Mapping[str, Any]) -> dict[str, Any]:
    options = _mapping(mount.get("TmpfsOptions"))
    entry: dict[str, Any] = {"type": "tmpfs", "target": str(mount.get("Target"))}
    parsed = {
        field: options[option]
        for field, option in (("size", "SizeBytes"), ("mode", "Mode"))
        if options.get(option)
    }
    if "mode" in parsed:
        parsed["mode"] = Octal(parsed["mode"])
    if parsed:
        entry["tmpfs"] = parsed
    return entry


def _port(port: str, protocol: str, address: str, published: str) -> Any:
    if address:
        entry: dict[str, Any] = {"target": int(port), "host_ip": address, "protocol": protocol}
        if published:
            entry["published"] = published
        return entry
    text = f"{published}:{port}" if published else port
    return text if protocol == "tcp" else f"{text}/{protocol}"


def _port_order(spec: str) -> tuple[int, str]:
    port, _, protocol = spec.partition("/")
    return (int(port) if port.isdigit() else 0, protocol)


def _without_tcp(spec: str) -> str:
    return spec.removesuffix("/tcp")


def _device(device: Mapping[str, Any]) -> str:
    parts = [str(device.get("PathOnHost")), str(device.get("PathInContainer"))]
    access = str(device.get("CgroupPermissions") or _ALL_DEVICE_ACCESS)
    if access != _ALL_DEVICE_ACCESS:
        parts.append(access)
    return ":".join(parts)


def _healthcheck(health: Mapping[str, Any]) -> dict[str, Any]:
    test = _words(health.get("Test"))
    if test == ["NONE"]:
        return {"disable": True}
    entry: dict[str, Any] = {}
    if test:
        entry["test"] = [_escaped(word) for word in test]
    for field, option in (
        ("interval", "Interval"),
        ("timeout", "Timeout"),
        ("start_period", "StartPeriod"),
        ("start_interval", "StartInterval"),
    ):
        if nanoseconds := _count(health.get(option)):
            entry[field] = duration_text(nanoseconds)
    if retries := _count(health.get("Retries")):
        entry["retries"] = retries
    return entry
