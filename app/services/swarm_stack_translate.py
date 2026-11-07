"""Translation of a resolved Compose model into Swarm networks, configs, secrets and services."""

import copy
import hashlib
import ipaddress
import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Literal

from app.auth.errors import ValidationFailed
from app.services.access import RESERVED_LABELS, SWARM_NAMESPACE_LABEL
from app.services.container_restrictions import is_blank
from app.services.container_spec import (
    CAPABILITIES,
    DNS_LABEL,
    MAX_COMMAND_CHARS,
    MAX_ENV_CHARS,
    MAX_LABEL_CHARS,
    USER_PATTERN,
    parse_image,
)
from app.services.service_restrictions import parse_constraint
from app.services.service_spec import (
    MAX_ALIASES,
    MAX_CONSTRAINT_CHARS,
    MAX_CONSTRAINTS,
    MAX_CPUS,
    MAX_INT32,
    MAX_PREFERENCES,
    MAX_REPLICAS,
    MIN_MEMORY_BYTES,
    MIN_NANO_CPUS,
    OBJECT_NAME,
    SERVICE_NAME,
)
from app.services.stack_document import SWARM_STACK_NAME
from app.services.stack_restrictions import (
    SWARM_SECURITY_OPTS,
    ResolvedModel,
    limits,
    swarm_refusals,
)
from app.services.swarm_configs import MAX_CONFIG_BYTES, MAX_SECRET_BYTES, SecretValue

__all__ = [
    "FileItem",
    "NetworkItem",
    "ServiceItem",
    "Translation",
    "TranslationInput",
    "TranslationRejected",
    "VolumeUse",
    "bind_references",
    "compose_path",
    "translate",
]

_ESCAPED_DOLLAR: Final = "$$"
_MAX_NS: Final = 24 * 3600 * 1_000_000_000
_MIN_NS: Final = 1_000_000
_MAX_INT64: Final = 2**63 - 1
_MIN_API_FOR_NEW_FIELDS: Final = (1, 47)
_FILE_MODE_MAX: Final = 0o7777
_TOKEN: Final = re.compile(r"[0-9a-z]{12}")
_DIGEST_LENGTH: Final = 12
_IMAGE_ID: Final = re.compile(r"sha256:[0-9a-f]{64}")
_VOLUME_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
_NETWORK_REFERENCE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
_ALIAS: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?")
_ENV_NAME: Final = re.compile(r"[^=\x00]+")
_STOP_SIGNAL: Final = re.compile(r"(?:SIG)?[A-Za-z0-9+_-]{1,20}", re.IGNORECASE)
_GROUP: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_DNS_OPTION: Final = re.compile(r"[A-Za-z0-9_:.-]{1,128}")
_SEARCH_DOMAIN: Final = re.compile(r"[A-Za-z0-9._-]{1,253}")
_LOG_DRIVER: Final = re.compile(r"[a-z0-9_-]{1,64}")
_LOG_KEY: Final = re.compile(r"[a-z0-9_.-]{1,64}")
_SYSCTL: Final = re.compile(r"[A-Za-z0-9_.-]{1,255}")
_PREFERENCE: Final = re.compile(r"[A-Za-z0-9_.-]{1,256}")
_PORT_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
_OPTION_KEY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_FILE_ID: Final = re.compile(r"[0-9]{1,10}")
_FILE_MODE_TEXT: Final = re.compile(r"(?:0o|0)?[0-7]{1,4}")
_RESOURCE_KIND: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_IPAM_DRIVER: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_RESTART_SHORT: Final = re.compile(r"(no|always|unless-stopped|on-failure)(?::([0-9]{1,10}))?")
_GO_DURATION: Final = re.compile(r"(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:ns|us|µs|ms|s|m|h))+")
_GO_PART: Final = re.compile(r"([0-9]+(?:\.[0-9]*)?|\.[0-9]+)(ns|us|µs|ms|s|m|h)")
_GO_UNITS: Final[Mapping[str, int]] = {
    "ns": 1,
    "us": 1_000,
    "µs": 1_000,
    "ms": 1_000_000,
    "s": 1_000_000_000,
    "m": 60_000_000_000,
    "h": 3_600_000_000_000,
}
_RESERVED_FOLDED: Final = frozenset(label.casefold() for label in RESERVED_LABELS)
_ULIMITS: Final = frozenset(
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
_PREDEFINED_NETWORKS: Final = frozenset({"host", "bridge", "none", "default"})
_PROPAGATION: Final = frozenset({"private", "rprivate", "shared", "rshared", "slave", "rslave"})
_CONDITIONS: Final = ("none", "on-failure", "any")
_UPDATE_ACTIONS: Final = ("continue", "pause", "rollback")
_ROLLBACK_ACTIONS: Final = ("continue", "pause")
_ORDERS: Final = ("stop-first", "start-first")
_PROTOCOLS: Final = ("tcp", "udp", "sctp")
_PUBLISH_MODES: Final = ("ingress", "host")
_ENDPOINT_MODES: Final = ("vip", "dnsrr")
_ISOLATION: Final = ("default", "process", "hyperv")

_CAP_PORTS: Final = 256
_CAP_MOUNTS: Final = 64
_CAP_FILES: Final = 64
_CAP_NETWORKS: Final = 32
_CAP_ENV: Final = 256
_CAP_LABELS: Final = 128
_CAP_HOSTS: Final = 64
_CAP_ULIMITS: Final = 32
_CAP_SYSCTLS: Final = 64
_CAP_OPTIONS: Final = 32

_TOO_LONG: Final = "The stack name and this key are too long together."


class TranslationRejected(ValidationFailed):
    """Raised when the model cannot be translated; fields are keyed by Compose path.

    reason is the activity failure reason: refused_construct, restricted or invalid.
    """

    def __init__(self, fields: dict[str, str], reason: str) -> None:
        super().__init__(fields)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Source:
    """The content of a companion file that a config or a secret is read from.

    token is the file's value token; a secret is named by it.
    """

    section: Literal["configs", "secrets"]
    key: str
    data: bytes = field(repr=False)
    secret: bool
    token: str = field(repr=False)
    file: str


@dataclass(frozen=True, slots=True)
class TranslationInput:
    """The resolved model with the stack's variables; values are never shown in a repr."""

    namespace: str
    project: ResolvedModel = field(repr=False)
    variables: Mapping[str, str] = field(repr=False)
    secret_variables: frozenset[str]
    variable_tokens: Mapping[str, str] = field(repr=False)
    project_directory: Path
    api_version: tuple[int, int]
    protected_paths: tuple[str, ...] = ()
    sources: Mapping[str, Source] = field(default_factory=dict, repr=False)
    companions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NetworkItem:
    """A network the stack uses: a body to create, or an external one to resolve by name."""

    key: str
    name: str
    external: bool
    body: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class FileItem:
    """A config or secret the stack uses; data is None for an external one."""

    kind: Literal["config", "secret"]
    key: str
    name: str
    external: bool
    labels: Mapping[str, str]
    data: SecretValue | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class ServiceItem:
    """A ServiceSpec with the image unpinned and files and networks referenced by name."""

    key: str
    name: str
    image: str
    spec: Mapping[str, Any] = field(repr=False)
    paths: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class VolumeUse:
    """One named volume mount, for the manager pin and the reach checks."""

    service: str
    source: str
    read_only: bool
    external: bool
    path: str


@dataclass(frozen=True, slots=True)
class Translation:
    """Everything a stack deploy sends, in a fixed order."""

    networks: tuple[NetworkItem, ...]
    files: tuple[FileItem, ...]
    services: tuple[ServiceItem, ...]
    volumes: tuple[VolumeUse, ...]
    images: Mapping[str, tuple[str, ...]]


class _Bad(Exception):
    def __init__(self, path: str, message: str, reason: str = "invalid") -> None:
        super().__init__(message)
        self.path = path
        self.message = message
        self.reason = reason


def compose_path(service: ServiceItem, engine_path: str) -> str:
    """Return the Compose path a ServiceSpec path was written from; the service path if none."""
    best = ""
    for prefix in service.paths:
        if len(prefix) > len(best) and (
            engine_path == prefix or engine_path.startswith((f"{prefix}.", f"{prefix}["))
        ):
            best = prefix
    return service.paths[best] if best else f"services.{service.key}"


def bind_references(
    spec: Mapping[str, Any],
    *,
    image: str,
    files: Mapping[tuple[str, str], tuple[str, str]],
    networks: Mapping[str, str],
) -> dict[str, Any]:
    """Return a copy of the spec with the pinned image and the ids of files and external networks.

    Raises LookupError when a referenced config or secret is missing from files.
    """
    bound: dict[str, Any] = copy.deepcopy(dict(spec))
    task = bound["TaskTemplate"]
    container = task["ContainerSpec"]
    container["Image"] = image
    for group, kind, name_key, id_key in (
        ("Configs", "config", "ConfigName", "ConfigID"),
        ("Secrets", "secret", "SecretName", "SecretID"),
    ):
        for reference in container.get(group, ()):
            resolved = files.get((kind, reference[name_key]))
            if resolved is None:
                raise LookupError(f"{kind} {reference[name_key]} is not resolved")
            reference[id_key], reference[name_key] = resolved
    for attachment in task.get("Networks", ()):
        network_id = networks.get(attachment["Target"])
        if network_id is not None:
            attachment["Target"] = network_id
    return bound


def translate(source: TranslationInput) -> Translation:
    """Translate the model, or raise TranslationRejected with every refusal found.

    Pure and CPU-bound. Nothing user-shaped is copied into a spec: every Engine key is written
    from a typed value.
    """
    refused = swarm_refusals(
        source.project,
        companions=source.companions,
        generated={(item.section, item.key): name for name, item in source.sources.items()},
    )
    if refused:
        raise TranslationRejected(refused, "refused_construct")
    return _Translator(source).run()


def _parse_go_duration(text: str) -> int | None:
    value = text.strip()
    if value == "0":
        return 0
    if len(value) > 64 or _GO_DURATION.fullmatch(value) is None:
        return None
    total = sum(
        (Decimal(number) * _GO_UNITS[unit] for number, unit in _GO_PART.findall(value)),
        Decimal(0),
    )
    return int(total)


def _plain(text: str) -> str:
    return text.replace(_ESCAPED_DOLLAR, "$")


def _text(value: object, path: str, *, empty: bool = False, limit: int = 4096) -> str:
    if not isinstance(value, str):
        raise _Bad(path, "Must be text.")
    text = _plain(value)
    if "\0" in text:
        raise _Bad(path, "Must not contain a NUL character.")
    if not text and not empty:
        raise _Bad(path, "Must not be empty.")
    if len(text) > limit:
        raise _Bad(path, f"Must be at most {limit} characters.")
    return text


def _integer(value: object, path: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _Bad(path, f"Must be a whole number from {low} to {high}.")
    return value


def _flag(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise _Bad(path, "Must be true or false.")
    return value


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _Bad(path, "Must be a mapping.")
    return value


def _sequence(value: object, path: str, cap: int) -> list[Any]:
    if not isinstance(value, list | tuple):
        raise _Bad(path, "Must be a list.")
    if len(value) > cap:
        raise _Bad(path, f"At most {cap} entries are allowed.")
    return list(value)


def _duration(value: object, path: str) -> int:
    parsed = _parse_go_duration(value) if isinstance(value, str) else None
    if parsed is None or not (parsed == 0 or _MIN_NS <= parsed <= _MAX_NS):
        raise _Bad(path, "Must be 0 or a duration between 1ms and 24h, such as 30s or 1m30s.")
    return parsed


def _byte_count(value: object, path: str, minimum: int) -> int:
    if isinstance(value, bool):
        raise _Bad(path, "Must be a number of bytes.")
    if isinstance(value, int):
        count = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        count = int(value)
    else:
        raise _Bad(path, "Must be a number of bytes.")
    if not minimum <= count <= _MAX_INT64:
        floor = f"{minimum // (1024 * 1024)} MiB" if minimum >= 1024 * 1024 else f"{minimum} byte"
        raise _Bad(path, f"Must be a number of bytes, at least {floor}.")
    return count


def _nano_cpus(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise _Bad(path, "Must be a number of CPUs such as 0.5 or 2.")
    try:
        amount = Decimal(repr(value) if isinstance(value, float) else str(value).strip())
    except InvalidOperation:
        raise _Bad(path, "Must be a number of CPUs such as 0.5 or 2.") from None
    if not amount.is_finite():
        raise _Bad(path, "Must be a number of CPUs such as 0.5 or 2.")
    nano = amount * 1_000_000_000
    if nano != nano.to_integral_value():
        raise _Bad(path, "Must be a number of CPUs with at most 9 decimal places.")
    if not MIN_NANO_CPUS <= nano <= MAX_CPUS * 1_000_000_000:
        raise _Bad(path, f"Must be between 0.001 and {MAX_CPUS} CPUs.")
    return int(nano)


def _file_mode(value: object, path: str) -> int:
    if isinstance(value, bool):
        raise _Bad(path, "Must be a file mode such as 0440.")
    if isinstance(value, int):
        if 0 <= value <= _FILE_MODE_MAX:
            return value
    elif isinstance(value, str) and _FILE_MODE_TEXT.fullmatch(value.strip()):
        return int(value.strip().removeprefix("0o"), 8)
    raise _Bad(path, "Must be a file mode such as 0440.")


def _absolute(value: object, path: str, *, root_ok: bool = False) -> str:
    text = _text(value, path)
    if not text.startswith("/") or text.startswith("//") or posixpath.normpath(text) != text:
        raise _Bad(path, "Must be an absolute, normalised path.")
    if text == "/" and not root_ok:
        raise _Bad(path, "Must not be /.")
    return text


def _string_map(
    value: object, path: str, *, cap: int, key: re.Pattern[str], limit: int = 1024
) -> dict[str, str]:
    entries = _mapping(value, path)
    if len(entries) > cap:
        raise _Bad(path, f"At most {cap} entries are allowed.")
    result: dict[str, str] = {}
    for name, content in entries.items():
        if not isinstance(name, str) or key.fullmatch(name) is None:
            raise _Bad(path, "Has a key that is not valid.")
        result[name] = _text(content, f"{path}.{name}", empty=True, limit=limit)
    return result


def _is_external(entry: Mapping[str, Any]) -> bool:
    external = entry.get("external")
    return external is True or (isinstance(external, Mapping) and bool(external))


@dataclass(slots=True)
class _Draft:
    key: str
    base: str
    model: Mapping[str, Any]
    container: dict[str, Any] = field(default_factory=dict)
    task: dict[str, Any] = field(default_factory=dict)
    spec: dict[str, Any] = field(default_factory=dict)
    paths: dict[str, str] = field(default_factory=dict)
    volumes: list[VolumeUse] = field(default_factory=list)
    mount_labels: list[dict[str, str]] = field(default_factory=list)
    image: str = ""
    failed: bool = False

    def note(self, engine_path: str, compose: str) -> None:
        self.paths[engine_path] = compose


_CONTAINER = "TaskTemplate.ContainerSpec"
_TASK = "TaskTemplate"


class _Translator:
    def __init__(self, source: TranslationInput) -> None:
        self.source = source
        self.namespace = source.namespace
        self.project = source.project
        self.errors: dict[str, str] = {}
        self.restricted = False
        self._networks: dict[str, NetworkItem | _Bad] = {}
        self._files: dict[tuple[str, str], FileItem | _Bad] = {}

    def run(self) -> Translation:
        if SWARM_STACK_NAME.fullmatch(self.namespace) is None:
            raise TranslationRejected({"name": "The stack name is not valid."}, "invalid")
        problems = limits(self.project, kind="swarm", administrator=True)
        if problems:
            raise TranslationRejected(problems, "invalid")
        services: list[ServiceItem] = []
        volumes: list[VolumeUse] = []
        images: dict[str, list[str]] = {}
        for key in sorted(self.project.services):
            model = self.project.services[key]
            base = f"services.{_safe(key)}"
            if not isinstance(model, Mapping):
                self.errors.setdefault(base, "Must be a mapping.")
                continue
            draft = self._service(key, base, model)
            if draft is None:
                continue
            item = ServiceItem(key, draft.spec["Name"], draft.image, draft.spec, draft.paths)
            services.append(item)
            volumes.extend(draft.volumes)
            images.setdefault(draft.image, []).append(key)
        networks = tuple(sorted(self._built_networks(), key=lambda item: item.key))
        files = tuple(sorted(self._built_files(), key=lambda item: (item.kind, item.key)))
        self._distinct_names(networks, files)
        if self.errors:
            raise TranslationRejected(self.errors, "restricted" if self.restricted else "invalid")
        return Translation(
            networks=networks,
            files=files,
            services=tuple(services),
            volumes=tuple(volumes),
            images={image: tuple(keys) for image, keys in images.items()},
        )

    def _built_networks(self) -> list[NetworkItem]:
        return [item for item in self._networks.values() if isinstance(item, NetworkItem)]

    def _built_files(self) -> list[FileItem]:
        return [item for item in self._files.values() if isinstance(item, FileItem)]

    def _distinct_names(self, networks: Sequence[NetworkItem], files: Sequence[FileItem]) -> None:
        seen: dict[str, str] = {}
        for network in networks:
            other = seen.setdefault(network.name, network.key)
            if other != network.key:
                self.errors.setdefault(
                    f"networks.{_safe(network.key)}",
                    "This network and another one resolve to the same Docker network.",
                )
        names: dict[tuple[str, str], str] = {}
        for item in files:
            other = names.setdefault((item.kind, item.name), item.key)
            if other != item.key:
                self.errors.setdefault(
                    f"{item.kind}s.{_safe(item.key)}",
                    f"This {item.kind} and another one resolve to the same Docker {item.kind}.",
                )

    def fail(self, bad: _Bad) -> None:
        self.errors.setdefault(bad.path, bad.message)
        if bad.reason == "restricted":
            self.restricted = True

    def step(self, call: Callable[[_Draft], None], draft: _Draft) -> None:
        try:
            call(draft)
        except _Bad as bad:
            draft.failed = True
            self.fail(bad)

    def _service(self, key: str, base: str, model: Mapping[str, Any]) -> _Draft | None:
        name = f"{self.namespace}_{key}"
        if SERVICE_NAME.fullmatch(name) is None:
            message = (
                _TOO_LONG
                if len(name) > 63
                else "A Swarm service name cannot contain '.' or end with '-' or '_'."
            )
            self.errors.setdefault(base, message)
            return None
        draft = _Draft(key, base, model)
        draft.spec["Name"] = name
        draft.note("Name", base)
        for call in (
            self._image,
            self._command,
            self._environment,
            self._labels,
            self._identity,
            self._security,
            self._lists,
            self._healthcheck,
            self._logging,
            self._mode,
            self._placement,
            self._resources,
            self._restart,
            self._rollouts,
            self._ports,
            self._networks_step,
            self._mounts,
            self._references,
        ):
            self.step(call, draft)
        self.step(self._label_budget, draft)
        if draft.failed:
            return None
        task = draft.task
        task["ContainerSpec"] = draft.container
        draft.spec["TaskTemplate"] = task
        return draft

    def _get(self, draft: _Draft, key: str) -> Any:
        return draft.model.get(key)

    def _path(self, draft: _Draft, *parts: str) -> str:
        return ".".join((draft.base, *parts))

    def _image(self, draft: _Draft) -> None:
        path = self._path(draft, "image")
        raw = self._get(draft, "image")
        if not isinstance(raw, str) or not raw:
            raise _Bad(path, "An image is required.")
        image = _text(raw, path, limit=512)
        if _IMAGE_ID.fullmatch(image) is None:
            try:
                image = parse_image(image)
            except ValueError as error:
                raise _Bad(path, str(error)) from None
        draft.image = image
        draft.container["Image"] = image
        draft.note(f"{_CONTAINER}.Image", path)

    def _command(self, draft: _Draft) -> None:
        entrypoint = self._get(draft, "entrypoint")
        command = self._get(draft, "command")
        budget = 0
        if entrypoint is not None:
            path = self._path(draft, "entrypoint")
            items = _sequence(entrypoint, path, 256)
            words = [_text(item, path, empty=True) for item in items]
            budget += sum(len(word) for word in words)
            draft.container["Command"] = words or [""]
            draft.note(f"{_CONTAINER}.Command", path)
        if command is not None:
            path = self._path(draft, "command")
            items = _sequence(command, path, 256)
            if not items:
                raise _Bad(path, "A Swarm service cannot clear the image command.")
            words = [_text(item, path, empty=True) for item in items]
            budget += sum(len(word) for word in words)
            draft.container["Args"] = words
            draft.note(f"{_CONTAINER}.Args", path)
        if budget > MAX_COMMAND_CHARS:
            raise _Bad(
                self._path(draft, "command"),
                f"The command and entrypoint total at most {MAX_COMMAND_CHARS} characters.",
            )

    def _environment(self, draft: _Draft) -> None:
        environment = self._get(draft, "environment")
        if environment is None:
            return
        path = self._path(draft, "environment")
        entries = _mapping(environment, path)
        if len(entries) > _CAP_ENV:
            raise _Bad(path, f"At most {_CAP_ENV} variables are allowed.")
        lines: list[str] = []
        for name, value in entries.items():
            where = f"{path}.{_safe(name)}"
            if not isinstance(name, str) or _ENV_NAME.fullmatch(name) is None:
                raise _Bad(where, "Has a variable name that is not valid.")
            if value is None:
                raise _Bad(where, "Set this variable; a variable without a value is not supported.")
            lines.append(f"{name}={_text(value, where, empty=True, limit=MAX_ENV_CHARS)}")
        if sum(len(line) for line in lines) > MAX_ENV_CHARS:
            raise _Bad(path, "Environment variables must total at most 64 KiB.")
        if lines:
            draft.container["Env"] = sorted(lines)
            draft.note(f"{_CONTAINER}.Env", path)

    def _label_map(self, value: object, path: str) -> dict[str, str]:
        if value is None:
            return {}
        entries = _mapping(value, path)
        if len(entries) > _CAP_LABELS:
            raise _Bad(path, f"At most {_CAP_LABELS} labels are allowed.")
        labels: dict[str, str] = {}
        for key, content in entries.items():
            text = _text(key, path, limit=256)
            if text.strip().casefold() in _RESERVED_FOLDED:
                raise _Bad(
                    path, "The stack membership labels are set by the product.", "restricted"
                )
            labels[text] = _text(content, path, empty=True, limit=MAX_LABEL_CHARS)
        return labels

    def _stamped(self, labels: Mapping[str, str]) -> dict[str, str]:
        return {**labels, SWARM_NAMESPACE_LABEL: self.namespace}

    def _labels(self, draft: _Draft) -> None:
        path = self._path(draft, "labels")
        labels = self._label_map(self._get(draft, "labels"), path)
        draft.container["Labels"] = self._stamped(labels)
        draft.note(f"{_CONTAINER}.Labels", path)
        deploy = self._get(draft, "deploy")
        service_labels: dict[str, str] = {}
        if isinstance(deploy, Mapping) and deploy.get("labels") is not None:
            where = self._path(draft, "deploy", "labels")
            service_labels = self._label_map(deploy["labels"], where)
            draft.note("Labels", where)
        draft.spec["Labels"] = self._stamped(service_labels)

    def _label_budget(self, draft: _Draft) -> None:
        total = sum(
            len(key) + len(value)
            for labels in (
                draft.spec.get("Labels", {}),
                draft.container.get("Labels", {}),
                *draft.mount_labels,
            )
            for key, value in labels.items()
        )
        if total > MAX_LABEL_CHARS:
            raise _Bad(self._path(draft, "labels"), "Labels must total at most 64 KiB.")

    def _identity(self, draft: _Draft) -> None:
        container = draft.container
        for key, field_name, check in (
            ("user", "User", _user),
            ("working_dir", "Dir", _working_dir),
            ("hostname", "Hostname", _hostname),
            ("stop_signal", "StopSignal", _signal),
        ):
            value = self._get(draft, key)
            if value is not None:
                path = self._path(draft, key)
                container[field_name] = check(value, path)
                draft.note(f"{_CONTAINER}.{field_name}", path)
        grace = self._get(draft, "stop_grace_period")
        if grace is not None:
            path = self._path(draft, "stop_grace_period")
            container["StopGracePeriod"] = _duration(grace, path)
            draft.note(f"{_CONTAINER}.StopGracePeriod", path)
        for key, field_name in (
            ("tty", "TTY"),
            ("stdin_open", "OpenStdin"),
            ("read_only", "ReadOnly"),
        ):
            value = self._get(draft, key)
            if value is not None:
                path = self._path(draft, key)
                if _flag(value, path):
                    container[field_name] = True
                    draft.note(f"{_CONTAINER}.{field_name}", path)
        init = self._get(draft, "init")
        if init is not None:
            path = self._path(draft, "init")
            container["Init"] = _flag(init, path)
            draft.note(f"{_CONTAINER}.Init", path)
        groups = self._get(draft, "group_add")
        if groups is not None:
            path = self._path(draft, "group_add")
            names = [_text(item, path, limit=64) for item in _sequence(groups, path, 16)]
            if any(_GROUP.fullmatch(name) is None for name in names):
                raise _Bad(path, "Must be group names or ids.")
            if names:
                container["Groups"] = names
                draft.note(f"{_CONTAINER}.Groups", path)

    def _security(self, draft: _Draft) -> None:
        container = draft.container
        for key, field_name in (("cap_add", "CapabilityAdd"), ("cap_drop", "CapabilityDrop")):
            value = self._get(draft, key)
            if value is not None:
                path = self._path(draft, key)
                names = sorted({_capability(item, path) for item in _sequence(value, path, 64)})
                if names:
                    container[field_name] = names
                    draft.note(f"{_CONTAINER}.{field_name}", path)
        sysctls = self._get(draft, "sysctls")
        if sysctls is not None:
            path = self._path(draft, "sysctls")
            entries = _string_map(sysctls, path, cap=_CAP_SYSCTLS, key=_SYSCTL)
            if entries:
                container["Sysctls"] = entries
                draft.note(f"{_CONTAINER}.Sysctls", path)
        isolation = self._get(draft, "isolation")
        if isolation is not None:
            path = self._path(draft, "isolation")
            value = _text(isolation, path)
            if value not in _ISOLATION:
                raise _Bad(path, f"Must be one of {', '.join(_ISOLATION)}.")
            container["Isolation"] = value
            draft.note(f"{_CONTAINER}.Isolation", path)
        score = self._get(draft, "oom_score_adj")
        if score is not None:
            path = self._path(draft, "oom_score_adj")
            if self.source.api_version < _MIN_API_FOR_NEW_FIELDS:
                raise _Bad(path, "This needs Docker API 1.47 or later on the environment.")
            container["OomScoreAdj"] = _integer(score, path, -1000, 1000)
            draft.note(f"{_CONTAINER}.OomScoreAdj", path)
        options = self._get(draft, "security_opt")
        if options is not None:
            path = self._path(draft, "security_opt")
            privileges: dict[str, Any] = {}
            for ordinal, option in enumerate(_sequence(options, path, 16)):
                effect = SWARM_SECURITY_OPTS.get(option) if isinstance(option, str) else None
                if effect is None:
                    raise _Bad(f"{path}[{ordinal}]", "This security option has no Swarm form.")
                if effect == "no_new_privileges":
                    privileges["NoNewPrivileges"] = True
                elif effect == "seccomp_unconfined":
                    privileges["Seccomp"] = {"Mode": "unconfined"}
                else:
                    privileges["AppArmor"] = {"Mode": "disabled"}
            if privileges:
                container["Privileges"] = privileges
                draft.note(f"{_CONTAINER}.Privileges", path)

    def _lists(self, draft: _Draft) -> None:
        container = draft.container
        ulimits = self._get(draft, "ulimits")
        if ulimits is not None:
            path = self._path(draft, "ulimits")
            entries = _mapping(ulimits, path)
            if len(entries) > _CAP_ULIMITS:
                raise _Bad(path, f"At most {_CAP_ULIMITS} limits are allowed.")
            built = [_ulimit(name, value, path) for name, value in entries.items()]
            if built:
                container["Ulimits"] = sorted(built, key=lambda item: item["Name"])
                draft.note(f"{_CONTAINER}.Ulimits", path)
        dns: dict[str, Any] = {}
        for key, field_name, convert in (
            ("dns", "Nameservers", _address),
            ("dns_search", "Search", _search_domain),
            ("dns_opt", "Options", _dns_option),
        ):
            value = self._get(draft, key)
            if value is not None:
                path = self._path(draft, key)
                items = [convert(item, path) for item in _sequence(value, path, 8)]
                if items:
                    dns[field_name] = items
                    draft.note(f"{_CONTAINER}.DNSConfig.{field_name}", path)
        if dns:
            container["DNSConfig"] = dns
            draft.note(f"{_CONTAINER}.DNSConfig", self._path(draft, "dns"))
        hosts = self._get(draft, "extra_hosts")
        if hosts is not None:
            path = self._path(draft, "extra_hosts")
            lines = [_extra_host(item, path) for item in _sequence(hosts, path, _CAP_HOSTS)]
            if lines:
                container["Hosts"] = lines
                draft.note(f"{_CONTAINER}.Hosts", path)

    def _healthcheck(self, draft: _Draft) -> None:
        check = self._get(draft, "healthcheck")
        if check is None:
            return
        path = self._path(draft, "healthcheck")
        entries = _mapping(check, path)
        disable = entries.get("disable")
        if disable is not None and _flag(disable, f"{path}.disable"):
            if entries.get("test") is not None:
                raise _Bad(path, "test and disable can't be set at the same time.")
            draft.container["Healthcheck"] = {"Test": ["NONE"]}
            draft.note(f"{_CONTAINER}.Healthcheck", path)
            return
        built: dict[str, Any] = {}
        test = entries.get("test")
        if test is not None:
            built["Test"] = _health_test(test, f"{path}.test")
        for key, field_name in (
            ("interval", "Interval"),
            ("timeout", "Timeout"),
            ("start_period", "StartPeriod"),
            ("start_interval", "StartInterval"),
        ):
            if entries.get(key) is not None:
                built[field_name] = _duration(entries[key], f"{path}.{key}")
        if entries.get("retries") is not None:
            built["Retries"] = _integer(entries["retries"], f"{path}.retries", 0, MAX_INT32)
        if built:
            draft.container["Healthcheck"] = built
            draft.note(f"{_CONTAINER}.Healthcheck", path)

    def _logging(self, draft: _Draft) -> None:
        logging = self._get(draft, "logging")
        if logging is None:
            return
        path = self._path(draft, "logging")
        entries = _mapping(logging, path)
        driver = entries.get("driver")
        options = entries.get("options")
        if driver is None and options is None:
            return
        if driver is None:
            raise _Bad(path, "Set a driver for the options.")
        name = _text(driver, f"{path}.driver")
        if _LOG_DRIVER.fullmatch(name) is None:
            raise _Bad(f"{path}.driver", "Must be a log driver name.")
        built: dict[str, Any] = {"Name": name}
        if options is not None:
            chosen = _string_map(
                options, f"{path}.options", cap=_CAP_OPTIONS, key=_LOG_KEY, limit=256
            )
            if chosen:
                built["Options"] = chosen
        draft.task["LogDriver"] = built
        draft.note(f"{_TASK}.LogDriver", path)

    def _deploy(self, draft: _Draft) -> Mapping[str, Any]:
        deploy = self._get(draft, "deploy")
        return _mapping(deploy, self._path(draft, "deploy")) if deploy is not None else {}

    def _mode(self, draft: _Draft) -> None:
        deploy = self._deploy(draft)
        path = self._path(draft, "deploy")
        mode = deploy.get("mode", "replicated")
        replicas = deploy.get("replicas")
        if mode == "global":
            if replicas is not None:
                raise _Bad(f"{path}.replicas", "Replicas apply only to replicated mode.")
            draft.spec["Mode"] = {"Global": {}}
            draft.note("Mode", f"{path}.mode")
        elif mode == "replicated":
            count = (
                1 if replicas is None else _integer(replicas, f"{path}.replicas", 0, MAX_REPLICAS)
            )
            draft.spec["Mode"] = {"Replicated": {"Replicas": count}}
            draft.note("Mode", f"{path}.mode")
            draft.note("Mode.Replicated", f"{path}.replicas")
        else:
            raise _Bad(f"{path}.mode", "Must be replicated or global.")
        endpoint = deploy.get("endpoint_mode")
        if endpoint is not None:
            chosen = _text(endpoint, f"{path}.endpoint_mode").lower()
            if chosen not in _ENDPOINT_MODES:
                raise _Bad(f"{path}.endpoint_mode", f"Must be one of {', '.join(_ENDPOINT_MODES)}.")
            draft.spec.setdefault("EndpointSpec", {})["Mode"] = chosen
            draft.note("EndpointSpec", f"{path}.endpoint_mode")

    def _placement(self, draft: _Draft) -> None:
        placement = self._deploy(draft).get("placement")
        if placement is None:
            return
        path = self._path(draft, "deploy", "placement")
        entries = _mapping(placement, path)
        built: dict[str, Any] = {}
        constraints = entries.get("constraints")
        if constraints is not None:
            where = f"{path}.constraints"
            lines = [
                _text(item, where, limit=MAX_CONSTRAINT_CHARS).strip()
                for item in _sequence(constraints, where, MAX_CONSTRAINTS)
            ]
            if any(parse_constraint(line) is None for line in lines):
                raise _Bad(
                    where,
                    "Each constraint must be an attribute, == or !=, and a value, "
                    "such as node.role==worker.",
                )
            if lines:
                built["Constraints"] = lines
                draft.note(f"{_TASK}.Placement.Constraints", where)
        preferences = entries.get("preferences")
        if preferences is not None:
            where = f"{path}.preferences"
            spreads: list[dict[str, Any]] = []
            for ordinal, preference in enumerate(_sequence(preferences, where, MAX_PREFERENCES)):
                spread = _text(
                    _mapping(preference, f"{where}[{ordinal}]").get("spread"),
                    f"{where}[{ordinal}].spread",
                    limit=256,
                )
                if _PREFERENCE.fullmatch(spread) is None:
                    raise _Bad(
                        f"{where}[{ordinal}].spread",
                        "Must be a node label such as node.labels.zone.",
                    )
                spreads.append({"Spread": {"SpreadDescriptor": spread}})
            if spreads:
                built["Preferences"] = spreads
                draft.note(f"{_TASK}.Placement.Preferences", where)
        per_node = entries.get("max_replicas_per_node")
        if per_node is not None:
            where = f"{path}.max_replicas_per_node"
            count = _integer(per_node, where, 0, MAX_INT32)
            if count:
                built["MaxReplicas"] = count
                draft.note(f"{_TASK}.Placement.MaxReplicas", where)
        if built:
            draft.task["Placement"] = built
            draft.note(f"{_TASK}.Placement", path)

    def _resources(self, draft: _Draft) -> None:
        resources = self._deploy(draft).get("resources")
        if resources is None:
            return
        path = self._path(draft, "deploy", "resources")
        entries = _mapping(resources, path)
        built: dict[str, Any] = {}
        limit_values = self._resource_group(entries.get("limits"), f"{path}.limits", pids=True)
        reservation_values = self._resource_group(
            entries.get("reservations"), f"{path}.reservations", pids=False
        )
        for field_name, name in (("NanoCPUs", "cpus"), ("MemoryBytes", "memory")):
            low, high = limit_values.get(field_name), reservation_values.get(field_name)
            if low is not None and high is not None and high > low:
                raise _Bad(f"{path}.reservations.{name}", "A reservation cannot exceed the limit.")
        if limit_values:
            built["Limits"] = limit_values
            draft.note(f"{_TASK}.Resources.Limits", f"{path}.limits")
        reservations = entries.get("reservations")
        generic = self._generic_resources(
            reservations.get("generic_resources") if isinstance(reservations, Mapping) else None,
            f"{path}.reservations.generic_resources",
        )
        if generic:
            reservation_values["GenericResources"] = generic
            draft.note(
                f"{_TASK}.Resources.Reservations.GenericResources",
                f"{path}.reservations.generic_resources",
            )
        if reservation_values:
            built["Reservations"] = reservation_values
            draft.note(f"{_TASK}.Resources.Reservations", f"{path}.reservations")
        if built:
            draft.task["Resources"] = built
            draft.note(f"{_TASK}.Resources", path)

    def _resource_group(self, group: object, path: str, *, pids: bool) -> dict[str, Any]:
        if group is None:
            return {}
        entries = _mapping(group, path)
        built: dict[str, Any] = {}
        if entries.get("cpus") is not None:
            built["NanoCPUs"] = _nano_cpus(entries["cpus"], f"{path}.cpus")
        if entries.get("memory") is not None:
            built["MemoryBytes"] = _byte_count(
                entries["memory"], f"{path}.memory", MIN_MEMORY_BYTES
            )
        if pids and entries.get("pids") is not None:
            built["Pids"] = _integer(entries["pids"], f"{path}.pids", 1, MAX_INT32)
        return built

    def _generic_resources(self, value: object, path: str) -> list[dict[str, Any]]:
        if value is None:
            return []
        built: list[dict[str, Any]] = []
        for ordinal, entry in enumerate(_sequence(value, path, 16)):
            where = f"{path}[{ordinal}]"
            discrete = _mapping(entry, where).get("discrete_resource_spec")
            if discrete is None or len(entry) != 1:
                raise _Bad(where, "Only a discrete_resource_spec is supported.")
            spec = _mapping(discrete, f"{where}.discrete_resource_spec")
            kind = _text(spec.get("kind"), f"{where}.discrete_resource_spec.kind", limit=64)
            if _RESOURCE_KIND.fullmatch(kind) is None:
                raise _Bad(f"{where}.discrete_resource_spec.kind", "Must be a resource kind.")
            amount = _integer(
                spec.get("value"), f"{where}.discrete_resource_spec.value", 1, MAX_INT32
            )
            built.append({"DiscreteResourceSpec": {"Kind": kind, "Value": amount}})
        return built

    def _restart(self, draft: _Draft) -> None:
        deploy = self._deploy(draft)
        restart = self._get(draft, "restart")
        policy = deploy.get("restart_policy")
        path = self._path(draft, "restart")
        if restart is not None and policy is not None:
            raise _Bad(path, "Use deploy.restart_policy alone.")
        built: dict[str, Any] = {}
        if restart is not None:
            matched = _RESTART_SHORT.fullmatch(_text(restart, path))
            if matched is None or (matched[2] is not None and matched[1] != "on-failure"):
                raise _Bad(path, "Must be no, always, unless-stopped, on-failure or on-failure:N.")
            built["Condition"] = {
                "no": "none",
                "always": "any",
                "unless-stopped": "any",
                "on-failure": "on-failure",
            }[matched[1]]
            attempts = int(matched[2]) if matched[2] else 0
            if attempts:
                built["MaxAttempts"] = _integer(attempts, path, 0, MAX_INT32)
            draft.note(f"{_TASK}.RestartPolicy", path)
        elif policy is not None:
            where = self._path(draft, "deploy", "restart_policy")
            entries = _mapping(policy, where)
            condition = entries.get("condition")
            if condition is not None:
                chosen = _text(condition, f"{where}.condition")
                if chosen not in _CONDITIONS:
                    raise _Bad(f"{where}.condition", f"Must be one of {', '.join(_CONDITIONS)}.")
                built["Condition"] = chosen
            for key, field_name in (("delay", "Delay"), ("window", "Window")):
                if entries.get(key) is not None:
                    built[field_name] = _duration(entries[key], f"{where}.{key}")
            if entries.get("max_attempts") is not None:
                built["MaxAttempts"] = _integer(
                    entries["max_attempts"], f"{where}.max_attempts", 0, MAX_INT32
                )
            draft.note(f"{_TASK}.RestartPolicy", where)
        if built:
            draft.task["RestartPolicy"] = built

    def _rollouts(self, draft: _Draft) -> None:
        deploy = self._deploy(draft)
        for key, field_name, actions in (
            ("update_config", "UpdateConfig", _UPDATE_ACTIONS),
            ("rollback_config", "RollbackConfig", _ROLLBACK_ACTIONS),
        ):
            value = deploy.get(key)
            if value is None:
                continue
            path = self._path(draft, "deploy", key)
            entries = _mapping(value, path)
            built: dict[str, Any] = {"Parallelism": 1}
            if entries.get("parallelism") is not None:
                built["Parallelism"] = _integer(
                    entries["parallelism"], f"{path}.parallelism", 0, MAX_INT32
                )
            for name, target in (("delay", "Delay"), ("monitor", "Monitor")):
                if entries.get(name) is not None:
                    built[target] = _duration(entries[name], f"{path}.{name}")
            if entries.get("failure_action") is not None:
                action = _text(entries["failure_action"], f"{path}.failure_action")
                if action not in actions:
                    raise _Bad(f"{path}.failure_action", f"Must be one of {', '.join(actions)}.")
                built["FailureAction"] = action
            if entries.get("order") is not None:
                order = _text(entries["order"], f"{path}.order")
                if order not in _ORDERS:
                    raise _Bad(f"{path}.order", f"Must be one of {', '.join(_ORDERS)}.")
                built["Order"] = order
            if entries.get("max_failure_ratio") is not None:
                ratio = entries["max_failure_ratio"]
                if (
                    isinstance(ratio, bool)
                    or not isinstance(ratio, int | float)
                    or not 0 <= ratio <= 1
                ):
                    raise _Bad(f"{path}.max_failure_ratio", "Must be a number from 0 to 1.")
                built["MaxFailureRatio"] = float(ratio)
            draft.spec[field_name] = built
            draft.note(field_name, path)

    def _ports(self, draft: _Draft) -> None:
        ports = self._get(draft, "ports")
        if ports is None:
            return
        path = self._path(draft, "ports")
        endpoint_mode = draft.spec.get("EndpointSpec", {}).get("Mode")
        entries: list[tuple[tuple[int, int, str, str, str], dict[str, Any], str]] = []
        seen: set[tuple[int, str]] = set()
        for ordinal, port in enumerate(_sequence(ports, path, _CAP_PORTS)):
            where = f"{path}[{ordinal}]"
            fields = _mapping(port, where)
            target = _integer(fields.get("target"), f"{where}.target", 1, 65535)
            published = _published(fields.get("published"), f"{where}.published")
            protocol = fields.get("protocol", "tcp")
            if protocol not in _PROTOCOLS:
                raise _Bad(f"{where}.protocol", "Must be tcp, udp or sctp.")
            mode = fields.get("mode", "ingress")
            if mode not in _PUBLISH_MODES:
                raise _Bad(f"{where}.mode", "Must be ingress or host.")
            if mode == "ingress" and endpoint_mode == "dnsrr":
                raise _Bad(
                    f"{where}.mode", "A port published in ingress mode needs the vip endpoint mode."
                )
            if published is not None:
                if (published, protocol) in seen:
                    raise _Bad(
                        f"{where}.published", "This published port and protocol is used twice."
                    )
                seen.add((published, protocol))
            entry: dict[str, Any] = {
                "Protocol": protocol,
                "TargetPort": target,
                "PublishMode": mode,
            }
            name = fields.get("name")
            if name is not None:
                label = _text(name, f"{where}.name", limit=63)
                if _PORT_NAME.fullmatch(label) is None:
                    raise _Bad(
                        f"{where}.name",
                        "Must be a short name of letters, digits, '-', '_' and '.'.",
                    )
                entry["Name"] = label
            if published is not None:
                entry["PublishedPort"] = published
            order = (published or 0, target, protocol, mode, entry.get("Name", ""))
            entries.append((order, entry, where))
        if entries:
            entries.sort(key=lambda item: item[0])
            built = draft.spec.setdefault("EndpointSpec", {})
            built["Ports"] = [entry for _, entry, _ in entries]
            for position, (_, _, where) in enumerate(entries):
                draft.note(f"EndpointSpec.Ports[{position}]", where)

    def _networks_step(self, draft: _Draft) -> None:
        declared = self._get(draft, "networks")
        path = self._path(draft, "networks")
        attachments: dict[str, Any]
        if isinstance(declared, Mapping):
            attachments = dict(declared)
        elif isinstance(declared, list | tuple):
            attachments = {name: None for name in declared if isinstance(name, str)}
        else:
            attachments = {}
        if not attachments:
            attachments = {"default": None}
        if len(attachments) > _CAP_NETWORKS:
            raise _Bad(path, f"At most {_CAP_NETWORKS} networks are allowed.")
        built: list[tuple[str, dict[str, Any], str]] = []
        targets: set[str] = set()
        for key, config in attachments.items():
            where = f"{path}.{_safe(key)}"
            if not isinstance(key, str):
                raise _Bad(path, "Has a network key that is not text.")
            item = self._network_item(key)
            if item.name in targets:
                raise _Bad(
                    where, "Two of this service's networks resolve to the same Docker network."
                )
            targets.add(item.name)
            attachment: dict[str, Any] = {"Target": item.name}
            aliases: list[str] = []
            if config is not None:
                fields = _mapping(config, where)
                if fields.get("aliases") is not None:
                    aliases = [
                        _alias(alias, f"{where}.aliases")
                        for alias in _sequence(fields["aliases"], f"{where}.aliases", MAX_ALIASES)
                    ]
                if fields.get("driver_opts") is not None:
                    chosen = _string_map(
                        fields["driver_opts"],
                        f"{where}.driver_opts",
                        cap=_CAP_OPTIONS,
                        key=_OPTION_KEY,
                        limit=256,
                    )
                    if chosen:
                        attachment["DriverOpts"] = chosen
            if item.name not in _PREDEFINED_NETWORKS:
                aliases.append(draft.key)
            unique = list(dict.fromkeys(aliases))
            if len(unique) > MAX_ALIASES:
                raise _Bad(f"{where}.aliases", f"At most {MAX_ALIASES} aliases are allowed.")
            if unique:
                attachment["Aliases"] = unique
            built.append((item.name, attachment, where))
        built.sort(key=lambda entry: entry[0])
        draft.task["Networks"] = [entry for _, entry, _ in built]
        for position, (_, _, where) in enumerate(built):
            draft.note(f"{_TASK}.Networks[{position}]", where)

    def _network_item(self, key: str) -> NetworkItem:
        cached = self._networks.get(key)
        if cached is None:
            try:
                cached = self._build_network(key)
            except _Bad as bad:
                cached = bad
            self._networks[key] = cached
        if isinstance(cached, _Bad):
            self.fail(cached)
            raise cached
        return cached

    def _build_network(self, key: str) -> NetworkItem:
        path = f"networks.{_safe(key)}"
        top = self.project.networks.get(key)
        if top is None:
            if key != "default":
                raise _Bad(path, "This network is not declared.")
            top = {}
        if not isinstance(top, Mapping):
            raise _Bad(path, "Must be a mapping.")
        if _is_external(top):
            name = _text(top.get("name") or key, f"{path}.name", limit=255)
            if _NETWORK_REFERENCE.fullmatch(name) is None:
                raise _Bad(f"{path}.name", "Must be a Docker network name.")
            return NetworkItem(key, name, True, {})
        name = f"{self.namespace}_{key}"
        if SERVICE_NAME.fullmatch(name) is None:
            raise _Bad(
                path,
                _TOO_LONG
                if len(name) > 63
                else "A Swarm network name cannot contain '.' or end with '-' or '_'.",
            )
        body: dict[str, Any] = {
            "Name": name,
            "Driver": "overlay",
            "Attachable": _optional_flag(top.get("attachable"), f"{path}.attachable"),
            "Internal": _optional_flag(top.get("internal"), f"{path}.internal"),
            "EnableIPv6": _optional_flag(top.get("enable_ipv6"), f"{path}.enable_ipv6"),
            "Labels": self._stamped(self._label_map(top.get("labels"), f"{path}.labels")),
        }
        if top.get("driver_opts"):
            body["Options"] = _string_map(
                top["driver_opts"],
                f"{path}.driver_opts",
                cap=_CAP_OPTIONS,
                key=_OPTION_KEY,
                limit=256,
            )
        ipam = _ipam(top.get("ipam"), f"{path}.ipam")
        if ipam:
            body["IPAM"] = ipam
        return NetworkItem(key, name, False, body)

    def _mounts(self, draft: _Draft) -> None:
        mounts = self._get(draft, "volumes")
        if mounts is None:
            return
        path = self._path(draft, "volumes")
        entries: list[tuple[str, dict[str, Any], str]] = []
        targets: set[str] = set()
        for ordinal, mount in enumerate(_sequence(mounts, path, _CAP_MOUNTS)):
            where = f"{path}[{ordinal}]"
            entry = self._mount(draft, _mapping(mount, where), where)
            if entry["Target"] in targets:
                raise _Bad(f"{where}.target", "Mount targets must be unique.")
            targets.add(entry["Target"])
            entries.append((entry["Target"], entry, where))
        entries.sort(key=lambda item: item[0])
        if entries:
            draft.container["Mounts"] = [entry for _, entry, _ in entries]
            for position, (_, _, where) in enumerate(entries):
                draft.note(f"{_CONTAINER}.Mounts[{position}]", where)
            uses = {use.path: use for use in draft.volumes}
            draft.volumes[:] = [uses[where] for _, _, where in entries if where in uses]

    def _mount(self, draft: _Draft, mount: Mapping[str, Any], where: str) -> dict[str, Any]:
        kind = mount.get("type", "volume")
        target = _absolute(mount.get("target"), f"{where}.target")
        entry: dict[str, Any] = {"Type": kind, "Target": target}
        read_only = mount.get("read_only")
        if read_only is not None and _flag(read_only, f"{where}.read_only"):
            entry["ReadOnly"] = True
        source = mount.get("source")
        if kind == "tmpfs":
            if source:
                raise _Bad(f"{where}.source", "A tmpfs mount takes no source.")
            options = self._tmpfs_options(mount.get("tmpfs"), f"{where}.tmpfs")
            if options:
                entry["TmpfsOptions"] = options
        elif kind == "bind":
            self._bind(draft, entry, mount, source, where)
        elif kind == "volume":
            self._volume_mount(draft, entry, mount, source, where)
        else:
            raise _Bad(f"{where}.type", "Must be volume, bind or tmpfs.")
        return entry

    def _tmpfs_options(self, value: object, path: str) -> dict[str, Any]:
        if value is None:
            return {}
        fields = _mapping(value, path)
        options: dict[str, Any] = {}
        if fields.get("size") is not None:
            options["SizeBytes"] = _byte_count(fields["size"], f"{path}.size", 1)
        if fields.get("mode") is not None:
            options["Mode"] = _file_mode(fields["mode"], f"{path}.mode")
        return options

    def _bind(
        self,
        draft: _Draft,
        entry: dict[str, Any],
        mount: Mapping[str, Any],
        source: object,
        where: str,
    ) -> None:
        path = _absolute(source, f"{where}.source", root_ok=True)
        for protected in (self.source.project_directory.as_posix(), *self.source.protected_paths):
            if path == protected or path.startswith(protected.rstrip("/") + "/"):
                raise _Bad(
                    f"{where}.source",
                    "A path inside the stack's own directory or this server's data directory "
                    "names a path on this server, not on a Swarm node.",
                )
        entry["Source"] = path
        bind = mount.get("bind")
        if bind is not None:
            fields = _mapping(bind, f"{where}.bind")
            propagation = fields.get("propagation")
            if propagation is not None:
                if propagation not in _PROPAGATION:
                    raise _Bad(f"{where}.bind.propagation", "Is not a mount propagation mode.")
                entry["BindOptions"] = {"Propagation": propagation}

    def _volume_mount(
        self,
        draft: _Draft,
        entry: dict[str, Any],
        mount: Mapping[str, Any],
        source: object,
        where: str,
    ) -> None:
        options: dict[str, Any] = {}
        volume = mount.get("volume")
        if volume is not None:
            fields = _mapping(volume, f"{where}.volume")
            if fields.get("nocopy") is not None and _flag(
                fields["nocopy"], f"{where}.volume.nocopy"
            ):
                options["NoCopy"] = True
            if fields.get("subpath") is not None:
                if self.source.api_version < _MIN_API_FOR_NEW_FIELDS:
                    raise _Bad(
                        f"{where}.volume.subpath",
                        "This needs Docker API 1.47 or later on the environment.",
                    )
                subpath = _text(fields["subpath"], f"{where}.volume.subpath")
                if subpath.startswith("/") or ".." in subpath.split("/"):
                    raise _Bad(f"{where}.volume.subpath", "Must be a relative path without '..'.")
                options["Subpath"] = subpath
        if source is not None and source != "":
            if not isinstance(source, str):
                raise _Bad(f"{where}.source", "Must be a volume name.")
            top = self.project.volumes.get(source)
            if not isinstance(top, Mapping):
                raise _Bad(f"{where}.source", "This volume is not declared.")
            external = _is_external(top)
            if external:
                name = _text(top.get("name") or source, f"{where}.source", limit=255)
            else:
                name = f"{self.namespace}_{source}"
                self._created_volume(top, options, f"volumes.{_safe(source)}")
                draft.mount_labels.append(dict(options.get("Labels", {})))
            if _VOLUME_NAME.fullmatch(name) is None or len(name) < 2:
                raise _Bad(f"{where}.source", "Is not a valid volume name.")
            entry["Source"] = name
            draft.volumes.append(
                VolumeUse(draft.key, name, entry.get("ReadOnly") is True, external, where)
            )
        if options:
            entry["VolumeOptions"] = options

    def _created_volume(self, top: Mapping[str, Any], options: dict[str, Any], path: str) -> None:
        options["Labels"] = self._stamped(self._label_map(top.get("labels"), f"{path}.labels"))
        driver = top.get("driver")
        driver_opts = top.get("driver_opts")
        if (driver not in (None, "", "local")) or not is_blank(driver_opts):
            name = _text(driver or "local", f"{path}.driver", limit=64)
            if _LOG_DRIVER.fullmatch(name) is None:
                raise _Bad(f"{path}.driver", "Must be a volume driver name.")
            config: dict[str, Any] = {"Name": name}
            if not is_blank(driver_opts):
                config["Options"] = _string_map(
                    driver_opts,
                    f"{path}.driver_opts",
                    cap=_CAP_OPTIONS,
                    key=_OPTION_KEY,
                    limit=1024,
                )
            options["DriverConfig"] = config

    def _references(self, draft: _Draft) -> None:
        groups: tuple[tuple[str, Literal["config", "secret"], str], ...] = (
            ("configs", "config", "ConfigName"),
            ("secrets", "secret", "SecretName"),
        )
        for group, kind, name_key in groups:
            value = self._get(draft, group)
            if value is None:
                continue
            path = self._path(draft, group)
            entries: list[tuple[str, dict[str, Any], str]] = []
            targets: set[str] = set()
            for ordinal, reference in enumerate(_sequence(value, path, _CAP_FILES)):
                where = f"{path}[{ordinal}]"
                fields = _mapping(reference, where)
                source = fields.get("source")
                if not isinstance(source, str):
                    raise _Bad(f"{where}.source", f"Must name a {kind}.")
                item = self._file_item(kind, source)
                target = self._file_target(kind, source, fields.get("target"), f"{where}.target")
                if target in targets:
                    raise _Bad(f"{where}.target", "File targets must be unique.")
                targets.add(target)
                entry: dict[str, Any] = {
                    "File": {
                        "Name": target,
                        "UID": self._owner(fields.get("uid"), f"{where}.uid"),
                        "GID": self._owner(fields.get("gid"), f"{where}.gid"),
                        "Mode": 0o444
                        if fields.get("mode") is None
                        else _file_mode(fields["mode"], f"{where}.mode"),
                    },
                    name_key: item.name,
                }
                entries.append((item.name, entry, where))
            entries.sort(key=lambda entry: (entry[0], entry[1]["File"]["Name"]))
            if entries:
                field_name = "Configs" if kind == "config" else "Secrets"
                draft.container[field_name] = [entry for _, entry, _ in entries]
                for position, (_, _, where) in enumerate(entries):
                    draft.note(f"{_CONTAINER}.{field_name}[{position}]", where)

    def _file_target(self, kind: str, key: str, value: object, path: str) -> str:
        target = key if value is None else _text(value, path)
        if len(target) > 4096 or ".." in target.split("/"):
            raise _Bad(path, "Must be a file path without .. segments.")
        return target

    def _owner(self, value: object, path: str) -> str:
        if value is None:
            return "0"
        text = _text(value, path, limit=10)
        if _FILE_ID.fullmatch(text) is None:
            raise _Bad(path, "Must be a numeric id.")
        return text

    def _file_item(self, kind: Literal["config", "secret"], key: str) -> FileItem:
        slot = (kind, key)
        cached = self._files.get(slot)
        if cached is None:
            try:
                cached = self._build_file(kind, key)
            except _Bad as bad:
                cached = bad
            self._files[slot] = cached
        if isinstance(cached, _Bad):
            self.fail(cached)
            raise cached
        return cached

    def _build_file(self, kind: Literal["config", "secret"], key: str) -> FileItem:
        section = f"{kind}s"
        path = f"{section}.{_safe(key)}"
        entries = self.project.configs if kind == "config" else self.project.secrets
        entry = entries.get(key)
        if not isinstance(entry, Mapping):
            raise _Bad(path, f"This {kind} is not declared.")
        if _is_external(entry):
            name = _text(entry.get("name") or key, f"{path}.name", limit=64)
            if OBJECT_NAME.fullmatch(name) is None:
                raise _Bad(f"{path}.name", f"Must be a Docker {kind} name.")
            return FileItem(kind, key, name, True, {}, None)
        content = entry.get("content")
        variable = entry.get("environment")
        file_source = self._file_source(variable, kind, key)
        if kind == "secret":
            data, suffix = self._secret_data(variable, path, file_source)
        else:
            data = self._config_data(content, variable, path, file_source)
            suffix = hashlib.sha256(data).hexdigest()[:_DIGEST_LENGTH]
        name = f"{self.namespace}_{key}_{suffix}"
        if OBJECT_NAME.fullmatch(name) is None:
            raise _Bad(
                path,
                "The stack name and this key are too long together."
                if len(name) > 64
                else f"A Swarm {kind} name can use letters, digits, '-', '_' and '.' only.",
            )
        labels = self._stamped(self._label_map(entry.get("labels"), f"{path}.labels"))
        return FileItem(kind, key, name, False, labels, SecretValue(data))

    def _file_source(self, variable: object, kind: str, key: str) -> Source | None:
        found = self.source.sources.get(variable) if isinstance(variable, str) else None
        if found is not None and (found.section, found.key) != (f"{kind}s", key):
            return None
        return found

    def _config_data(
        self, content: object, variable: object, path: str, file_source: Source | None
    ) -> bytes:
        if (content is None) == (variable is None):
            raise _Bad(path, "Use exactly one of content and environment.")
        if file_source is not None:
            if file_source.secret:
                raise _Bad(f"{path}.file", "A config must be read from a plain file.")
            data = file_source.data
        elif content is not None:
            data = _text(content, f"{path}.content", limit=MAX_CONFIG_BYTES).encode("utf-8")
        else:
            where = f"{path}.environment"
            name = _text(variable, where, limit=128)
            if name in self.source.secret_variables:
                raise _Bad(where, "A config cannot be read from a secret variable.")
            value = self.source.variables.get(name)
            if value is None:
                raise _Bad(where, "This must name a variable of the stack.")
            data = value.encode("utf-8")
        if not 1 <= len(data) <= MAX_CONFIG_BYTES:
            raise _Bad(path, f"Content must be from 1 byte to {MAX_CONFIG_BYTES // 1024} KiB.")
        return data

    def _secret_data(
        self, variable: object, path: str, file_source: Source | None
    ) -> tuple[bytes, str]:
        where = f"{path}.environment"
        if file_source is not None:
            if not file_source.secret:
                raise _Bad(f"{path}.file", "A secret must be read from a secret file.")
            if _TOKEN.fullmatch(file_source.token) is None:
                raise _Bad(
                    f"{path}.file", "This secret file is not stored yet, so it cannot be versioned."
                )
            if not 1 <= len(file_source.data) <= MAX_SECRET_BYTES:
                raise _Bad(
                    f"{path}.file",
                    f"A secret must be from 1 byte to {MAX_SECRET_BYTES // 1024} KiB.",
                )
            return file_source.data, file_source.token
        if variable is None:
            raise _Bad(where, "A secret must name a secret variable.")
        name = _text(variable, where, limit=128)
        if name not in self.source.secret_variables:
            raise _Bad(where, "This must name a secret variable of the stack.")
        value = self.source.variables.get(name)
        token = self.source.variable_tokens.get(name)
        if value is None:
            raise _Bad(where, "This secret variable holds no value.")
        if token is None or _TOKEN.fullmatch(token) is None:
            raise _Bad(where, "This secret variable is not stored yet, so it cannot be versioned.")
        data = value.encode("utf-8")
        if not 1 <= len(data) <= MAX_SECRET_BYTES:
            raise _Bad(where, f"A secret must be from 1 byte to {MAX_SECRET_BYTES // 1024} KiB.")
        return data, token


def _safe(key: object) -> str:
    return key if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key) else "<key>"


def _optional_flag(value: object, path: str) -> bool:
    return False if value is None else _flag(value, path)


def _user(value: object, path: str) -> str:
    text = _text(value, path, limit=128)
    if USER_PATTERN.fullmatch(text) is None:
        raise _Bad(path, "Must be a user name or id, optionally with a group.")
    return text


def _working_dir(value: object, path: str) -> str:
    return _absolute(value, path, root_ok=True)


def _hostname(value: object, path: str) -> str:
    text = _text(value, path, limit=63)
    if DNS_LABEL.fullmatch(text) is None:
        raise _Bad(path, "Must be a host name.")
    return text


def _signal(value: object, path: str) -> str:
    text = _text(value, path, limit=24)
    if _STOP_SIGNAL.fullmatch(text) is None:
        raise _Bad(path, "Must be a signal name or number.")
    return text


def _capability(value: object, path: str) -> str:
    name = _text(value, path, limit=64).strip().upper().removeprefix("CAP_")
    if name != "ALL" and name not in CAPABILITIES:
        raise _Bad(path, "Must be Linux capability names such as NET_ADMIN, or ALL.")
    return name


def _address(value: object, path: str) -> str:
    try:
        return str(ipaddress.ip_address(_text(value, path, limit=64).strip()))
    except ValueError:
        raise _Bad(path, "Must be an IP address.") from None


def _search_domain(value: object, path: str) -> str:
    text = _text(value, path, limit=253)
    if _SEARCH_DOMAIN.fullmatch(text) is None:
        raise _Bad(path, "Must be a domain name.")
    return text


def _dns_option(value: object, path: str) -> str:
    text = _text(value, path, limit=128)
    if _DNS_OPTION.fullmatch(text) is None:
        raise _Bad(path, "Must be a resolver option such as ndots:2.")
    return text


def _extra_host(value: object, path: str) -> str:
    text = _text(value, path, limit=512)
    host, separator, address = text.partition("=")
    parts = host.split(".")
    if (
        not separator
        or not host
        or len(host) > 253
        or not all(DNS_LABEL.fullmatch(part) for part in parts)
    ):
        raise _Bad(path, "Must be a host name and an IP address.")
    try:
        literal = str(ipaddress.ip_address(address.strip()))
    except ValueError:
        raise _Bad(path, "The address must be an IPv4 or IPv6 literal.") from None
    return f"{literal} {host}"


def _ulimit(name: object, value: object, path: str) -> dict[str, Any]:
    where = f"{path}.{_safe(name)}"
    if not isinstance(name, str) or name not in _ULIMITS:
        raise _Bad(where, "Is not a known resource limit.")
    if isinstance(value, Mapping):
        soft = _integer(value.get("soft"), f"{where}.soft", -1, _MAX_INT64)
        hard = _integer(value.get("hard"), f"{where}.hard", -1, _MAX_INT64)
    else:
        soft = hard = _integer(value, where, -1, _MAX_INT64)
    return {"Name": name, "Soft": soft, "Hard": hard}


def _health_test(value: object, path: str) -> list[str]:
    words = [_text(item, path, empty=True) for item in _sequence(value, path, 64)]
    if not words:
        raise _Bad(path, "Must not be empty.")
    if sum(len(word) for word in words) > MAX_COMMAND_CHARS:
        raise _Bad(path, f"Must total at most {MAX_COMMAND_CHARS} characters.")
    form = words[0]
    if form == "NONE":
        if len(words) != 1:
            raise _Bad(path, "NONE takes no arguments.")
    elif form not in ("CMD", "CMD-SHELL") or len(words) < 2:
        raise _Bad(path, "Must start with CMD, CMD-SHELL or NONE and give a command.")
    return words


def _alias(value: object, path: str) -> str:
    text = _text(value, path, limit=64)
    if _ALIAS.fullmatch(text) is None:
        raise _Bad(path, "Must be a network alias.")
    return text


def _published(value: object, path: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _Bad(path, "Must be a single port from 1 to 65535.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        number = int(value)
    else:
        raise _Bad(path, "Must be a single port from 1 to 65535.")
    if not 1 <= number <= 65535:
        raise _Bad(path, "Must be a single port from 1 to 65535.")
    return number


def _ipam(value: object, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    fields = _mapping(value, path)
    built: dict[str, Any] = {}
    driver = fields.get("driver")
    if driver is not None:
        name = _text(driver, f"{path}.driver", limit=64)
        if _IPAM_DRIVER.fullmatch(name) is None:
            raise _Bad(f"{path}.driver", "Must be an address driver name.")
        built["Driver"] = name
    pools: list[dict[str, str]] = []
    for ordinal, pool in enumerate(_sequence(fields.get("config") or [], f"{path}.config", 16)):
        where = f"{path}.config[{ordinal}]"
        entries = _mapping(pool, where)
        entry: dict[str, str] = {}
        try:
            subnet = ipaddress.ip_network(_text(entries.get("subnet"), f"{where}.subnet", limit=64))
        except ValueError:
            raise _Bad(f"{where}.subnet", "Must be a subnet such as 10.20.0.0/24.") from None
        entry["Subnet"] = str(subnet)
        if entries.get("gateway") is not None:
            gateway = _address(entries["gateway"], f"{where}.gateway")
            if ipaddress.ip_address(gateway) not in subnet:
                raise _Bad(f"{where}.gateway", "Must be inside the subnet.")
            entry["Gateway"] = gateway
        if entries.get("ip_range") is not None:
            try:
                pool_range = ipaddress.ip_network(
                    _text(entries["ip_range"], f"{where}.ip_range", limit=64)
                )
            except ValueError:
                raise _Bad(f"{where}.ip_range", "Must be a range such as 10.20.0.0/25.") from None
            if pool_range.version != subnet.version or not pool_range.subnet_of(subnet):  # type: ignore[arg-type]
                raise _Bad(f"{where}.ip_range", "Must be inside the subnet.")
            entry["IPRange"] = str(pool_range)
        pools.append(entry)
    if pools:
        built["Config"] = pools
    return built
