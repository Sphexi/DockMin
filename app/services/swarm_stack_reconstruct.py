"""A Compose document written from the specs of a Swarm namespace's objects; pure."""

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.services.access import SWARM_NAMESPACE_LABEL, canonical_stack_key
from app.services.stack_document import ComposeDocumentRejected, decode_file, path_part
from app.services.stack_read_result import (
    NeedsFile,
    Octal,
    ReadFile,
    ReadOutcome,
    SecretVariables,
    cpu_amount,
    dump_document,
    duration_text,
    finish,
    is_secret_entry,
)
from app.services.swarm_common import split_service_image

__all__ = ["KNOWN_LOSSES", "reconstruct_stack"]

KNOWN_LOSSES: Final = (
    "Comments, anchors and the original layout are not recovered.",
    "Image references are written without their digest pin; the next deploy resolves the tag.",
    "Fields the Engine fills in itself are left out: default isolation, the container runtime, "
    "placement platforms and the force-update counter.",
    "Secret values cannot be read; secrets are written as references to the existing objects.",
    "Network address pools are written as the cluster holds them.",
)

_VXLAN_OPTION: Final = "com.docker.network.driver.overlay.vxlanid_list"
_KEY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ENV_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
_ID: Final = re.compile(r"[0-9a-z]{25}|[0-9a-f]{64}")
_FILE_UNSAFE: Final = re.compile(r"[^A-Za-z0-9._-]")
_MAX_FILE_NAME: Final = 128
_DIGEST_LENGTH: Final = 12
_TOKEN_SUFFIX: Final = re.compile(r"_[0-9a-z]{12}$")
_MIN_NS: Final = 1_000_000
_MAX_NS: Final = 24 * 3600 * 1_000_000_000
_DEFAULT_FILE_MODE: Final = 0o444
_PLAIN_ISOLATION: Final = frozenset({"", "default"})
_DEFAULT_MODES: Final = frozenset({"", "default"})
_MOUNT_ORDER: Final = {
    "type": 0,
    "source": 1,
    "target": 2,
    "read_only": 3,
    "volume": 4,
    "bind": 4,
    "tmpfs": 4,
}


class _Stop(Exception):
    def __init__(self, reason: str, path: str) -> None:
        super().__init__(reason)
        self.outcome = NeedsFile(reason, path)


def _no_row(path: str) -> _Stop:
    return _Stop("no_translator_row", path)


def _empty(value: object) -> bool:
    if isinstance(value, Mapping):
        return all(_empty(item) for item in value.values())
    if isinstance(value, list | tuple):
        return all(_empty(item) for item in value)
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value == 0
    return value is None or value == ""


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _words(value: object) -> list[str]:
    return [str(item) for item in _items(value)]


def _escaped(text: str) -> str:
    return text.replace("$", "$$")


def _spec_of(item: Mapping[str, Any]) -> Mapping[str, Any]:
    spec = item.get("Spec")
    return spec if isinstance(spec, Mapping) else item


class _Fields:
    """A mapping read one key at a time; a key left unread with a value has no translator row."""

    def __init__(self, value: object, path: str) -> None:
        self.path = path
        self._data: dict[str, Any] = dict(_mapping(value))

    def take(self, key: str) -> Any:
        return self._data.pop(key, None)

    def has(self, key: str) -> bool:
        return key in self._data

    def present(self, key: str) -> bool:
        return self._data.get(key) is not None

    def sub(self, key: str) -> "_Fields":
        return _Fields(self.take(key), f"{self.path}.{key}")

    def done(self) -> None:
        for key, value in self._data.items():
            if not _empty(value):
                raise _no_row(f"{self.path}.{key}")

    def done_strict(self) -> None:
        for key in self._data:
            raise _no_row(f"{self.path}.{key}")


def _duration(value: object, path: str) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool) or value == 0:
        return None
    if not _MIN_NS <= value <= _MAX_NS:
        raise _Stop("duration_out_of_range", path)
    return duration_text(value)


def _written_image(image: str) -> str:
    parsed = split_service_image(image)
    if parsed.kind == "tagged_digest" and parsed.reference is not None:
        return parsed.reference
    return image


def _stripped(name: str, spelling: str) -> str:
    prefix = f"{spelling}_"
    return name[len(prefix) :] if name.startswith(prefix) and len(name) > len(prefix) else name


def _labels_without_namespace(labels: object) -> dict[str, str]:
    return {
        _escaped(str(name)): _escaped(str(value))
        for name, value in sorted(_mapping(labels).items())
        if str(name) != SWARM_NAMESPACE_LABEL
    }


def reconstruct_stack(
    namespace: str,
    services: Sequence[Mapping[str, Any]],
    networks: Sequence[Mapping[str, Any]],
    configs: Sequence[Mapping[str, Any]],
    secrets: Sequence[Mapping[str, Any]],
) -> ReadOutcome:
    """Write the namespace's services as one Compose document, or name the field that blocks it.

    `services`, `configs` and `secrets` are the Engine's objects, each holding its `Spec` (a bare
    spec is accepted); `networks` are network inspect documents and must include every network
    the services attach to. Objects not carrying the namespace label are external references.
    Blocking and CPU-bound; call through asyncio.to_thread.
    """
    try:
        return _Reconstruction(namespace, services, networks, configs, secrets).run()
    except _Stop as stop:
        return stop.outcome


class _Reconstruction:
    def __init__(
        self,
        namespace: str,
        services: Sequence[Mapping[str, Any]],
        networks: Sequence[Mapping[str, Any]],
        configs: Sequence[Mapping[str, Any]],
        secrets: Sequence[Mapping[str, Any]],
    ) -> None:
        self.folded = canonical_stack_key(namespace)
        self.members = sorted(
            (spec for spec in map(_spec_of, services) if self._owner(spec.get("Labels"))),
            key=lambda spec: str(spec.get("Name")),
        )
        if not self.members:
            raise _Stop("no_services", namespace)
        self.variables = SecretVariables()
        self.losses = list(KNOWN_LOSSES)
        self.files: list[ReadFile] = []
        self.top: dict[str, dict[str, dict[str, Any]]] = {
            "networks": {},
            "volumes": {},
            "configs": {},
            "secrets": {},
        }
        self._network_docs: dict[str, Mapping[str, Any]] = {}
        for network in networks:
            for ref in (network.get("Id"), network.get("Name")):
                if isinstance(ref, str):
                    self._network_docs[ref] = network
        self._keys: dict[str, dict[str, str]] = {kind: {} for kind in self.top}
        self._reserve_networks(networks)
        self._reserve_volumes()
        self._config_docs: dict[str, tuple[Mapping[str, Any], bytes]] = {}
        self._reserve_configs(configs)
        self._reserve_secrets(secrets)

    def _owner(self, labels: object) -> str | None:
        label = _mapping(labels).get(SWARM_NAMESPACE_LABEL)
        if isinstance(label, str) and canonical_stack_key(label) == self.folded:
            return label
        return None

    def _reserve(self, kind: str, name: str, key: str) -> None:
        taken = self._keys[kind]
        base, number = key, 1
        while key in taken.values() and taken.get(name) != key:
            number += 1
            key = f"{base}-{number}"
        taken[name] = key

    def _free_key(self, kind: str, name: str) -> str:
        taken = self._keys[kind]
        if name in taken:
            return taken[name]
        key = name
        while key in taken.values():
            key = f"external-{key}"
        taken[name] = key
        return key

    def _reserve_networks(self, networks: Sequence[Mapping[str, Any]]) -> None:
        for network in networks:
            spelling = self._owner(network.get("Labels"))
            name = str(network.get("Name"))
            if spelling is not None and name.startswith(f"{spelling}_"):
                self._reserve("networks", name, _stripped(name, spelling))

    def _reserve_volumes(self) -> None:
        for spec in self.members:
            container = _mapping(_mapping(spec.get("TaskTemplate")).get("ContainerSpec"))
            for mount in _items(container.get("Mounts")):
                owned = self._owned_volume(_mapping(mount))
                if owned is not None:
                    self._reserve("volumes", owned[0], owned[1])

    def _owned_volume(self, mount: Mapping[str, Any]) -> tuple[str, str] | None:
        if mount.get("Type", "volume") != "volume":
            return None
        source = mount.get("Source")
        options = _mapping(mount.get("VolumeOptions"))
        spelling = self._owner(options.get("Labels"))
        if isinstance(source, str) and spelling is not None and source.startswith(f"{spelling}_"):
            return source, _stripped(source, spelling)
        return None

    def _reserve_configs(self, configs: Sequence[Mapping[str, Any]]) -> None:
        for item in configs:
            spec = _spec_of(item)
            spelling = self._owner(spec.get("Labels"))
            name = str(spec.get("Name"))
            if spelling is None:
                continue
            data = _config_data(spec.get("Data"))
            if data is None:
                raise _Stop("config_unreadable", f"configs.{path_part(name)}")
            key = _stripped(name, spelling)
            suffix = f"_{hashlib.sha256(data).hexdigest()[:_DIGEST_LENGTH]}"
            self._reserve("configs", name, key.removesuffix(suffix) or key)
            self._config_docs[name] = (spec, data)
            if isinstance(item.get("ID"), str):
                self._config_docs[str(item["ID"])] = (spec, data)

    def _reserve_secrets(self, secrets: Sequence[Mapping[str, Any]]) -> None:
        for item in secrets:
            spec = _spec_of(item)
            spelling = self._owner(spec.get("Labels"))
            if spelling is not None:
                name = str(spec.get("Name"))
                key = _TOKEN_SUFFIX.sub("", _stripped(name, spelling)) or name
                self._reserve("secrets", name, key)

    def run(self) -> ReadOutcome:
        services = dict(map(self._service, self.members))
        document: dict[str, Any] = {"services": services}
        for section in ("networks", "volumes", "configs", "secrets"):
            if self.top[section]:
                document[section] = dict(sorted(self.top[section].items()))
        return finish(
            dump_document(document),
            kind="swarm",
            source="reconstructed",
            files=self.files,
            variables=self.variables.variables,
            losses=self.losses,
        )

    def _service(self, spec: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        fields = _Fields(spec, "services")
        name = str(fields.take("Name"))
        spelling = self._owner(fields.take("Labels")) or ""
        key = _stripped(name, spelling)
        if key == name or _KEY.fullmatch(key) is None:
            raise _Stop("name_without_namespace", f"services.{path_part(name)}")
        fields.path = f"services.{key}"
        service: dict[str, Any] = {}
        deploy: dict[str, Any] = {}
        labels = _labels_without_namespace(_mapping(spec.get("Labels")))
        self._mode(deploy, fields)
        task = fields.sub("TaskTemplate")
        container = task.sub("ContainerSpec")
        self._container(service, key, container)
        self._task(service, deploy, key, task)
        self._rollouts(deploy, fields)
        self._endpoint(service, deploy, fields)
        if labels:
            deploy["labels"] = labels
        if deploy:
            service["deploy"] = deploy
        fields.done()
        return key, service

    def _mode(self, deploy: dict[str, Any], fields: _Fields) -> None:
        mode = fields.sub("Mode")
        if mode.has("Global"):
            mode.take("Global")
            deploy["mode"] = "global"
        elif mode.has("Replicated"):
            replicas = _Fields(mode.take("Replicated"), f"{mode.path}.Replicated")
            count = replicas.take("Replicas")
            replicas.done()
            deploy["replicas"] = 1 if count is None else int(count)
        else:
            deploy["replicas"] = 1
        mode.done_strict()

    def _rollouts(self, deploy: dict[str, Any], fields: _Fields) -> None:
        for field, key, actions in (
            ("UpdateConfig", "update_config", ("continue", "pause", "rollback")),
            ("RollbackConfig", "rollback_config", ("continue", "pause")),
        ):
            if not fields.present(field):
                continue
            config = fields.sub(field)
            entry: dict[str, Any] = {"parallelism": int(config.take("Parallelism") or 0)}
            for option, name in (("Delay", "delay"), ("Monitor", "monitor")):
                if text := _duration(config.take(option), f"{config.path}.{option}"):
                    entry[name] = text
            action = config.take("FailureAction")
            if action:
                if action not in actions:
                    raise _no_row(f"{config.path}.FailureAction")
                entry["failure_action"] = action
            if order := config.take("Order"):
                entry["order"] = order
            if ratio := config.take("MaxFailureRatio"):
                entry["max_failure_ratio"] = float(ratio)
            config.done()
            deploy[key] = entry

    def _endpoint(self, service: dict[str, Any], deploy: dict[str, Any], fields: _Fields) -> None:
        endpoint = fields.sub("EndpointSpec")
        if mode := endpoint.take("Mode"):
            deploy["endpoint_mode"] = mode
        ports: list[dict[str, Any]] = []
        for ordinal, raw in enumerate(_items(endpoint.take("Ports"))):
            port = _Fields(raw, f"{endpoint.path}.Ports[{ordinal}]")
            entry: dict[str, Any] = {"target": int(port.take("TargetPort") or 0)}
            if published := port.take("PublishedPort"):
                entry["published"] = int(published)
            entry["protocol"] = port.take("Protocol") or "tcp"
            entry["mode"] = port.take("PublishMode") or "ingress"
            if name := port.take("Name"):
                entry["name"] = str(name)
            port.done()
            ports.append(entry)
        endpoint.done()
        if ports:
            service["ports"] = ports

    def _task(
        self, service: dict[str, Any], deploy: dict[str, Any], key: str, task: _Fields
    ) -> None:
        self._resources(deploy, task)
        self._restart(deploy, task)
        self._placement(deploy, task)
        self._attachments(service, key, task)
        driver = task.sub("LogDriver")
        if name := driver.take("Name"):
            logging: dict[str, Any] = {"driver": str(name)}
            if options := _mapping(driver.take("Options")):
                logging["options"] = {str(k): _escaped(str(v)) for k, v in options.items()}
            service["logging"] = logging
        driver.done()
        task.take("ForceUpdate")
        if task.take("Runtime") not in (None, "", "container"):
            raise _no_row(f"{task.path}.Runtime")
        task.done()

    def _resources(self, deploy: dict[str, Any], task: _Fields) -> None:
        resources = task.sub("Resources")
        built: dict[str, Any] = {}
        for field, name, pids in (
            ("Limits", "limits", True),
            ("Reservations", "reservations", False),
        ):
            group = resources.sub(field)
            entry: dict[str, Any] = {}
            if nano := group.take("NanoCPUs"):
                entry["cpus"] = cpu_amount(int(nano))
            if memory := group.take("MemoryBytes"):
                entry["memory"] = int(memory)
            if pids and (count := group.take("Pids")):
                entry["pids"] = int(count)
            generic = self._generic(group)
            if generic:
                entry["generic_resources"] = generic
            group.done()
            if entry:
                built[name] = entry
        resources.done()
        if built:
            deploy["resources"] = built

    def _generic(self, group: _Fields) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for ordinal, raw in enumerate(_items(group.take("GenericResources"))):
            where = f"{group.path}.GenericResources[{ordinal}]"
            entry = _Fields(raw, where)
            discrete = _Fields(entry.take("DiscreteResourceSpec"), f"{where}.DiscreteResourceSpec")
            kind, value = discrete.take("Kind"), discrete.take("Value")
            discrete.done()
            entry.done_strict()
            if not kind or not value:
                raise _no_row(where)
            found.append({"discrete_resource_spec": {"kind": str(kind), "value": int(value)}})
        return found

    def _restart(self, deploy: dict[str, Any], task: _Fields) -> None:
        policy = task.sub("RestartPolicy")
        entry: dict[str, Any] = {}
        if condition := policy.take("Condition"):
            entry["condition"] = str(condition)
        for option, name in (("Delay", "delay"), ("Window", "window")):
            if text := _duration(policy.take(option), f"{policy.path}.{option}"):
                entry[name] = text
        if attempts := policy.take("MaxAttempts"):
            entry["max_attempts"] = int(attempts)
        policy.done()
        if entry:
            deploy["restart_policy"] = entry

    def _placement(self, deploy: dict[str, Any], task: _Fields) -> None:
        placement = task.sub("Placement")
        entry: dict[str, Any] = {}
        if constraints := _words(placement.take("Constraints")):
            entry["constraints"] = constraints
        spreads: list[dict[str, str]] = []
        for ordinal, raw in enumerate(_items(placement.take("Preferences"))):
            where = f"{placement.path}.Preferences[{ordinal}]"
            preference = _Fields(raw, where)
            spread = _Fields(preference.take("Spread"), f"{where}.Spread")
            descriptor = spread.take("SpreadDescriptor")
            spread.done()
            preference.done_strict()
            if not isinstance(descriptor, str) or not descriptor:
                raise _no_row(where)
            spreads.append({"spread": descriptor})
        if spreads:
            entry["preferences"] = spreads
        if per_node := placement.take("MaxReplicas"):
            entry["max_replicas_per_node"] = int(per_node)
        placement.take("Platforms")
        placement.done()
        if entry:
            deploy["placement"] = entry

    def _attachments(self, service: dict[str, Any], key: str, task: _Fields) -> None:
        entries: dict[str, Any] = {}
        for ordinal, raw in enumerate(_items(task.take("Networks"))):
            where = f"{task.path}.Networks[{ordinal}]"
            attachment = _Fields(raw, where)
            target = attachment.take("Target")
            if not isinstance(target, str) or not target:
                raise _no_row(f"{where}.Target")
            network = self._network_key(target, f"{where}.Target")
            entry: dict[str, Any] = {}
            if aliases := [a for a in _words(attachment.take("Aliases")) if a != key]:
                entry["aliases"] = aliases
            if options := _mapping(attachment.take("DriverOpts")):
                entry["driver_opts"] = {str(k): _escaped(str(v)) for k, v in options.items()}
            attachment.done()
            entries[network] = entry
        if entries and not (list(entries) == ["default"] and not entries["default"]):
            service["networks"] = entries

    def _network_key(self, target: str, path: str) -> str:
        document = self._network_docs.get(target)
        if document is None:
            if _ID.fullmatch(target):
                raise _Stop("network_unresolved", path)
            name, spelling = target, None
        else:
            name = str(document.get("Name"))
            spelling = self._owner(document.get("Labels"))
        if document is not None and spelling is not None and name.startswith(f"{spelling}_"):
            key = self._keys["networks"][name]
            if key not in self.top["networks"]:
                definition = self._network_definition(document, key, spelling)
                if definition or key != "default":
                    self.top["networks"][key] = definition
            return key
        key = self._free_key("networks", name)
        self.top["networks"].setdefault(
            key, {"external": True, **({"name": name} if key != name else {})}
        )
        return key

    def _network_definition(
        self, document: Mapping[str, Any], key: str, spelling: str
    ) -> dict[str, Any]:
        path = f"networks.{key}"
        if str(document.get("Driver") or "overlay") != "overlay":
            raise _no_row(f"{path}.Driver")
        if document.get("Ingress") or not _empty(document.get("ConfigFrom")):
            raise _no_row(f"{path}.Ingress")
        definition: dict[str, Any] = {}
        for field, name in (
            ("Attachable", "attachable"),
            ("Internal", "internal"),
            ("EnableIPv6", "enable_ipv6"),
        ):
            if document.get(field):
                definition[name] = True
        if labels := _labels_without_namespace(document.get("Labels")):
            definition["labels"] = labels
        options = {
            str(k): _escaped(str(v))
            for k, v in _mapping(document.get("Options")).items()
            if k != _VXLAN_OPTION
        }
        if options:
            definition["driver_opts"] = options
        if ipam := self._ipam(_mapping(document.get("IPAM")), path):
            definition["ipam"] = ipam
        return definition

    def _ipam(self, document: Mapping[str, Any], path: str) -> dict[str, Any]:
        ipam: dict[str, Any] = {}
        driver = document.get("Driver")
        if driver and driver != "default":
            ipam["driver"] = str(driver)
        if not _empty(document.get("Options")):
            raise _no_row(f"{path}.IPAM.Options")
        pools: list[dict[str, str]] = []
        for raw in _items(document.get("Config")):
            pool = _mapping(raw)
            entry = {
                name: str(pool[field])
                for field, name in (
                    ("Subnet", "subnet"),
                    ("Gateway", "gateway"),
                    ("IPRange", "ip_range"),
                )
                if pool.get(field)
            }
            if entry:
                pools.append(entry)
        if pools:
            ipam["config"] = pools
        return ipam

    def _container(self, service: dict[str, Any], key: str, container: _Fields) -> None:
        image = container.take("Image")
        if not isinstance(image, str) or not image:
            raise _Stop("image_missing", f"{container.path}.Image")
        service["image"] = _written_image(image)
        entrypoint = _words(container.take("Command"))
        if entrypoint == [""]:
            service["entrypoint"] = []
        elif entrypoint:
            service["entrypoint"] = [_escaped(word) for word in entrypoint]
        if arguments := _words(container.take("Args")):
            service["command"] = [_escaped(word) for word in arguments]
        for field, name in (
            ("User", "user"),
            ("Dir", "working_dir"),
            ("Hostname", "hostname"),
            ("StopSignal", "stop_signal"),
        ):
            if value := container.take(field):
                service[name] = str(value)
        if grace := _duration(
            container.take("StopGracePeriod"), f"{container.path}.StopGracePeriod"
        ):
            service["stop_grace_period"] = grace
        self._environment(service, key, container)
        if labels := _labels_without_namespace(container.take("Labels")):
            service["labels"] = labels
        for field, name in (("TTY", "tty"), ("OpenStdin", "stdin_open"), ("ReadOnly", "read_only")):
            if container.take(field):
                service[name] = True
        init = container.take("Init")
        if init is not None:
            service["init"] = bool(init)
        self._security(service, container)
        self._lists(service, container)
        self._health(service, container)
        self._mounts(service, container)
        self._references(service, container)
        container.done()

    def _environment(self, service: dict[str, Any], key: str, container: _Fields) -> None:
        written: dict[str, str] = {}
        for entry in _words(container.take("Env")):
            name, separator, value = entry.partition("=")
            where = f"{container.path}.Env"
            if not separator or name in written or _ENV_NAME.fullmatch(name) is None:
                raise _no_row(where)
            if value and is_secret_entry(name, value):
                reference = self.variables.reference(key, name, value)
                if reference is None:
                    raise _Stop("secret_value_unusable", f"{where}.{path_part(name)}")
                written[name] = reference
            else:
                written[name] = _escaped(value)
        if written:
            service["environment"] = written

    def _security(self, service: dict[str, Any], container: _Fields) -> None:
        for field, name in (("CapabilityAdd", "cap_add"), ("CapabilityDrop", "cap_drop")):
            if names := _words(container.take(field)):
                service[name] = names
        if sysctls := _mapping(container.take("Sysctls")):
            service["sysctls"] = {str(k): _escaped(str(v)) for k, v in sysctls.items()}
        isolation = container.take("Isolation")
        if isolation and isolation not in _PLAIN_ISOLATION:
            service["isolation"] = str(isolation)
        if score := container.take("OomScoreAdj"):
            service["oom_score_adj"] = int(score)
        privileges = container.sub("Privileges")
        options: list[str] = []
        if privileges.take("NoNewPrivileges") is True:
            options.append("no-new-privileges:true")
        for field, unsafe, option in (
            ("Seccomp", "unconfined", "seccomp=unconfined"),
            ("AppArmor", "disabled", "apparmor=unconfined"),
        ):
            profile = privileges.sub(field)
            mode = profile.take("Mode")
            profile.done()
            if mode == unsafe:
                options.append(option)
            elif mode not in _DEFAULT_MODES and mode is not None:
                raise _no_row(f"{profile.path}.Mode")
        privileges.done()
        if options:
            service["security_opt"] = options

    def _lists(self, service: dict[str, Any], container: _Fields) -> None:
        ulimits: dict[str, Any] = {}
        for ordinal, raw in enumerate(_items(container.take("Ulimits"))):
            limit = _Fields(raw, f"{container.path}.Ulimits[{ordinal}]")
            name = limit.take("Name")
            ulimits[str(name)] = {
                "soft": int(limit.take("Soft") or 0),
                "hard": int(limit.take("Hard") or 0),
            }
            limit.done()
        if ulimits:
            service["ulimits"] = ulimits
        dns = container.sub("DNSConfig")
        for field, name in (
            ("Nameservers", "dns"),
            ("Search", "dns_search"),
            ("Options", "dns_opt"),
        ):
            if values := _words(dns.take(field)):
                service[name] = values
        dns.done()
        hosts: list[str] = []
        for entry in _words(container.take("Hosts")):
            parts = entry.split()
            if len(parts) != 2:
                raise _no_row(f"{container.path}.Hosts")
            hosts.append(f"{parts[1]}={parts[0]}")
        if hosts:
            service["extra_hosts"] = hosts

    def _health(self, service: dict[str, Any], container: _Fields) -> None:
        health = container.sub("Healthcheck")
        test = _words(health.take("Test"))
        entry: dict[str, Any] = {}
        if test == ["NONE"]:
            entry["disable"] = True
        elif test:
            entry["test"] = [_escaped(word) for word in test]
        for field, name in (
            ("Interval", "interval"),
            ("Timeout", "timeout"),
            ("StartPeriod", "start_period"),
            ("StartInterval", "start_interval"),
        ):
            if text := _duration(health.take(field), f"{health.path}.{field}"):
                entry[name] = text
        if retries := health.take("Retries"):
            entry["retries"] = int(retries)
        health.done()
        if entry:
            service["healthcheck"] = entry

    def _mounts(self, service: dict[str, Any], container: _Fields) -> None:
        mounts: list[dict[str, Any]] = []
        for ordinal, raw in enumerate(_items(container.take("Mounts"))):
            mounts.append(self._mount(raw, f"{container.path}.Mounts[{ordinal}]"))
        if mounts:
            service["volumes"] = mounts

    def _mount(self, raw: object, path: str) -> dict[str, Any]:
        mount = _Fields(raw, path)
        kind = str(mount.take("Type") or "volume")
        source = str(mount.take("Source") or "")
        target = mount.take("Target")
        if not isinstance(target, str) or not target:
            raise _no_row(f"{path}.Target")
        read_only = bool(mount.take("ReadOnly"))
        entry: dict[str, Any] = {"type": kind}
        options = {
            "volume": mount.take("VolumeOptions"),
            "bind": mount.take("BindOptions"),
            "tmpfs": mount.take("TmpfsOptions"),
        }
        match kind:
            case "volume":
                owned = self._owned_volume(
                    {"Type": kind, "Source": source, "VolumeOptions": options["volume"]}
                )
                volume = _Fields(options["volume"], f"{path}.VolumeOptions")
                section: dict[str, Any] = {}
                if volume.take("NoCopy"):
                    section["nocopy"] = True
                if subpath := volume.take("Subpath"):
                    section["subpath"] = str(subpath)
                labels, driver = volume.take("Labels"), volume.sub("DriverConfig")
                volume.done()
                if source:
                    entry["source"] = self._volume(source, owned, labels, driver)
                else:
                    driver.done()
                if section:
                    entry["volume"] = section
            case "bind":
                entry["source"] = _escaped(source)
                bind = _Fields(options["bind"], f"{path}.BindOptions")
                propagation = bind.take("Propagation")
                bind.done()
                if propagation:
                    entry["bind"] = {"propagation": str(propagation)}
            case "tmpfs":
                tmpfs = _Fields(options["tmpfs"], f"{path}.TmpfsOptions")
                section = {}
                if size := tmpfs.take("SizeBytes"):
                    section["size"] = int(size)
                if mode := tmpfs.take("Mode"):
                    section["mode"] = Octal(int(mode))
                tmpfs.done()
                if section:
                    entry["tmpfs"] = section
            case _:
                raise _no_row(f"{path}.Type")
        entry["target"] = _escaped(target)
        if read_only:
            entry["read_only"] = True
        mount.done()
        return dict(sorted(entry.items(), key=lambda item: _MOUNT_ORDER.get(item[0], 9)))

    def _volume(
        self,
        source: str,
        owned: tuple[str, str] | None,
        labels: object,
        driver: _Fields,
    ) -> str:
        if owned is None:
            driver.done()
            key = self._free_key("volumes", source)
            self.top["volumes"].setdefault(
                key, {"external": True, **({"name": source} if key != source else {})}
            )
            return key
        key = self._keys["volumes"][source]
        if key not in self.top["volumes"]:
            definition: dict[str, Any] = {}
            if kept := _labels_without_namespace(labels):
                definition["labels"] = kept
            name, options = driver.take("Name"), _mapping(driver.take("Options"))
            driver.done()
            if name and name != "local":
                definition["driver"] = str(name)
            if options:
                definition["driver_opts"] = {str(k): _escaped(str(v)) for k, v in options.items()}
            self.top["volumes"][key] = definition
        return key

    def _references(self, service: dict[str, Any], container: _Fields) -> None:
        for field, section in (("Configs", "configs"), ("Secrets", "secrets")):
            entries: list[dict[str, Any]] = []
            for ordinal, raw in enumerate(_items(container.take(field))):
                entries.append(
                    self._reference(section, raw, f"{container.path}.{field}[{ordinal}]")
                )
            if entries:
                service[section] = entries

    def _reference(self, section: str, raw: object, path: str) -> dict[str, Any]:
        reference = _Fields(raw, path)
        singular = "Config" if section == "configs" else "Secret"
        name, identifier = reference.take(f"{singular}Name"), reference.take(f"{singular}ID")
        file = reference.sub("File")
        target = file.take("Name")
        uid, gid, mode = file.take("UID"), file.take("GID"), file.take("Mode")
        file.done()
        reference.done()
        if not isinstance(name, str) or not name or not isinstance(target, str) or not target:
            raise _no_row(path)
        key = self._config_key(name, identifier) if section == "configs" else self._secret_key(name)
        entry: dict[str, Any] = {"source": key, "target": _escaped(target)}
        if uid not in (None, "", "0", 0):
            entry["uid"] = str(uid)
        if gid not in (None, "", "0", 0):
            entry["gid"] = str(gid)
        if isinstance(mode, int) and mode != _DEFAULT_FILE_MODE:
            entry["mode"] = Octal(mode)
        return entry

    def _config_key(self, name: str, identifier: object) -> str:
        found = self._config_docs.get(name) or self._config_docs.get(str(identifier))
        if found is None:
            key = self._free_key("configs", name)
            self.top["configs"].setdefault(
                key, {"external": True, **({"name": name} if key != name else {})}
            )
            return key
        spec, data = found
        key = self._keys["configs"][str(spec.get("Name"))]
        if key not in self.top["configs"]:
            if not _empty(spec.get("Templating")):
                raise _no_row(f"configs.{key}.Templating")
            try:
                decode_file(data)
            except ComposeDocumentRejected as rejected:
                raise _Stop(rejected.reason, f"configs.{key}") from None
            file_name = self._file_name(key)
            self.files.append(ReadFile(file_name, data, False))
            self.top["configs"][key] = {"file": f"./{file_name}"}
        return key

    def _file_name(self, key: str) -> str:
        base = _FILE_UNSAFE.sub("_", f"config-{key}")[:_MAX_FILE_NAME]
        taken = {item.name.casefold() for item in self.files}
        name, number = base, 1
        while name.casefold() in taken:
            number += 1
            name = f"{base[: _MAX_FILE_NAME - 4]}-{number}"
        return name

    def _secret_key(self, name: str) -> str:
        key = self._keys["secrets"].get(name) or self._free_key("secrets", name)
        self.top["secrets"].setdefault(key, {"external": True, "name": name})
        return key


def _config_data(value: object) -> bytes | None:
    if isinstance(value, bytes) and value:
        return value
    if isinstance(value, str) and value:
        try:
            return base64.b64decode(value, validate=True)
        except binascii.Error:
            return None
    return None
