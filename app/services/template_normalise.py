"""Normalisation of linuxserver.io and Portainer catalogue documents into one template list."""

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, NoReturn, cast

from pydantic import ValidationError

from app.netcheck import HIDDEN_CATEGORIES
from app.registry_client.references import ReferenceInvalid, split_reference
from app.services.app_template import (
    CATEGORIES_MAX,
    CATEGORY_MAX,
    COMMAND_MAX,
    CONTENT_FORMAT,
    DESCRIPTION_MAX,
    ENV_DEFAULT_MAX,
    ENV_NAME_PATTERN,
    ENV_OPTIONS_MAX,
    KEY_PATTERN,
    LABELS_MAX,
    MAX_CONTENT_BYTES,
    MAX_ENTRIES,
    NAME_PATTERN,
    NOTE_MAX,
    PATH_MAX,
    SETTINGS_LIST_MAX,
    TITLE_MAX,
    URL_MAX,
    AppTemplate,
    Protocol,
    TemplateEnv,
    TemplateEnvOption,
    TemplateLabel,
    TemplatePort,
    TemplateVolume,
)
from app.services.template_fetch import CatalogueFetchError, url_refusal

_PORT_TEXT: Final = re.compile(r"(?:([0-9]{1,5}):)?([0-9]{1,5})(?:/([A-Za-z]{3,4}))?")
_CAPABILITY: Final = re.compile(r"[A-Z][A-Z0-9_]{1,31}")
_SYSCTL: Final = re.compile(r"[A-Za-z0-9_./-]{1,128}=[A-Za-z0-9_.:/ -]{0,128}")
_SHM_SIZE: Final = re.compile(r"[0-9]+(?:\.[0-9]+)?\s?(?:[bkmg]|[kmg]i?b)?", re.IGNORECASE)
_DEVICE: Final = re.compile(r"/[A-Za-z0-9_./:-]{1,255}")
_HOSTNAME: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_ARCH_NAME: Final = re.compile(r"[a-z0-9_-]{1,16}")
_SHM_NAMES: Final = frozenset({"shm-size", "shm_size"})
_SYSCTL_NAMES: Final = frozenset({"sysctl", "sysctls"})
_RESTART_POLICIES: Final = frozenset({"no", "always", "unless-stopped", "on-failure"})
_TRUE_TEXT: Final = frozenset({"true", "1", "yes", "y", "on"})
_STACK_TYPES: Final = frozenset({2, 3})
_ARCHITECTURES: Final = {
    "x86_64": "amd64",
    "x86-64": "amd64",
    "amd64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "arm64v8": "arm64",
    "armhf": "arm",
    "arm32v7": "arm",
    "arm": "arm",
}
_PORTAINER_VERSIONS: Final = frozenset({"2", "3"})
_LINUXSERVER_REGISTRY: Final = "lscr.io/linuxserver"
_FRAME_BYTES: Final = len('{"format":1,"templates":[]}')

type Converter = Callable[[int, object], AppTemplate]


class _Skip(Exception):
    """An entry that cannot be used; `reason` is a fixed code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class NormalisedCatalogue:
    """The usable templates of a document, what was skipped, and the stored JSON."""

    templates: tuple[AppTemplate, ...]
    skipped: dict[str, int]
    content: str
    digest: str

    @property
    def entry_count(self) -> int:
        """Return how many templates are kept."""
        return len(self.templates)


def parse_catalogue(kind: str, body: bytes) -> NormalisedCatalogue:
    """Parse and normalise a downloaded document, or raise CatalogueFetchError."""
    try:
        document = json.loads(body, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        raise CatalogueFetchError("invalid_document") from None
    return normalise(kind, document)


def normalise(kind: str, document: object) -> NormalisedCatalogue:
    """Normalise a parsed document of the given kind; raise CatalogueFetchError if it is not one."""
    match kind:
        case "linuxserver":
            entries, convert = _linuxserver_entries(document), _linuxserver_entry
        case "portainer":
            entries, convert = _portainer_entries(document), _portainer_entry
        case _:
            raise CatalogueFetchError("invalid_document")
    kept: list[AppTemplate] = []
    pieces: list[str] = []
    seen: set[str] = set()
    skipped: Counter[str] = Counter()
    size = _FRAME_BYTES
    for index, item in enumerate(entries):
        if len(kept) >= MAX_ENTRIES:
            skipped["too_many_entries"] += len(entries) - index
            break
        try:
            template = convert(index, item)
        except _Skip as skip:
            skipped[skip.reason] += 1
            continue
        except ValidationError:
            skipped["invalid_entry"] += 1
            continue
        if template.key in seen:
            skipped["duplicate_key"] += 1
            continue
        piece = json.dumps(template.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        if size + len(piece) + 1 > MAX_CONTENT_BYTES:
            skipped["size_limit"] += len(entries) - index
            break
        size += len(piece) + 1
        seen.add(template.key)
        kept.append(template)
        pieces.append(piece)
    content = f'{{"format":{CONTENT_FORMAT},"templates":[{",".join(pieces)}]}}'
    return NormalisedCatalogue(
        tuple(kept),
        dict(sorted(skipped.items())),
        content,
        hashlib.sha256(content.encode("ascii")).hexdigest(),
    )


def _refuse_constant(_: str) -> NoReturn:
    raise ValueError("not a JSON number")


def _skip(reason: str) -> NoReturn:
    raise _Skip(reason)


def _linuxserver_entries(document: object) -> list[object]:
    entries: object = None
    if isinstance(document, dict):
        data = document.get("data")
        repositories = data.get("repositories") if isinstance(data, dict) else None
        entries = repositories.get("linuxserver") if isinstance(repositories, dict) else None
    if not isinstance(entries, list):
        raise CatalogueFetchError("invalid_document")
    return entries


def _portainer_entries(document: object) -> list[object]:
    if not isinstance(document, dict) or str(document.get("version")) not in _PORTAINER_VERSIONS:
        raise CatalogueFetchError("invalid_document")
    entries = document.get("templates")
    if not isinstance(entries, list):
        raise CatalogueFetchError("invalid_document")
    return entries


def _linuxserver_entry(index: int, item: object) -> AppTemplate:
    if not isinstance(item, dict):
        _skip("not_an_object")
    if _flag(item.get("deprecated")):
        _skip("deprecated")
    name = item.get("name")
    if not isinstance(name, str) or KEY_PATTERN.fullmatch(name.strip()) is None:
        _skip("invalid_key")
    key = name.strip()
    raw_config = item.get("config")
    config: dict[str, Any] = raw_config if isinstance(raw_config, dict) else {}
    custom = _linuxserver_custom(config.get("custom"))
    return AppTemplate(
        key=key,
        title=key,
        description=_text(item.get("description"), DESCRIPTION_MAX, multiline=True),
        categories=_categories(item.get("category")),
        image=_image(f"{_LINUXSERVER_REGISTRY}/{key}:latest"),
        name=key,
        architectures=_architectures(item.get("architectures")),
        ports=tuple(
            _linuxserver_port(entry) for entry in _items(config.get("ports"), "invalid_port")
        ),
        volumes=tuple(
            _linuxserver_volume(entry) for entry in _items(config.get("volumes"), "invalid_volume")
        ),
        env=tuple(
            _linuxserver_env(entry) for entry in _items(config.get("env_vars"), "invalid_env")
        ),
        devices=_linuxserver_devices(config.get("devices")),
        capabilities=_linuxserver_capabilities(config.get("caps")),
        sysctls=custom.sysctls,
        security_options=_linuxserver_security(config.get("security_opt")),
        host_network=_linuxserver_host(config.get("networking")),
        hostname=_linuxserver_hostname(config.get("hostname")),
        shm_size=custom.shm_size,
        privileged=_flag(config.get("privileged")),
        restart_policy="unless-stopped",
        setup_url=_https_url(config.get("application_setup")),
    )


def _portainer_entry(index: int, item: object) -> AppTemplate:
    if not isinstance(item, dict):
        _skip("not_an_object")
    _check_portainer_type(item.get("type", 1))
    title = _text(item.get("title"), TITLE_MAX)
    if not title:
        _skip("missing_title")
    return AppTemplate(
        key=_portainer_key(item.get("id"), index, title),
        title=title,
        description=_text(item.get("description"), DESCRIPTION_MAX, multiline=True),
        categories=_categories(item.get("categories")),
        image=_image(_portainer_image(item.get("image"), item.get("registry"))),
        name=_container_name(item.get("name")),
        ports=tuple(
            _portainer_port(entry) for entry in _list_of(item.get("ports"), "invalid_port")
        ),
        volumes=tuple(
            _portainer_volume(entry) for entry in _items(item.get("volumes"), "invalid_volume")
        ),
        env=tuple(_portainer_env(entry) for entry in _items(item.get("env"), "invalid_env")),
        host_network=_text(item.get("network"), 64).lower() == "host",
        hostname=_hostname(item.get("hostname")),
        privileged=_flag(item.get("privileged")),
        interactive=_flag(item.get("interactive")),
        restart_policy=_restart_policy(item.get("restart_policy")),
        command=_command(item.get("command")),
        labels=_labels(item.get("labels")),
        note=_text(item.get("note"), NOTE_MAX, multiline=True),
        administrator_only=_flag(
            item.get("administrator_only", item.get("administrator-only")),
        ),
    )


def _check_portainer_type(value: object) -> None:
    number = int(value) if isinstance(value, str) and value.strip().isdigit() else value
    if isinstance(number, int) and not isinstance(number, bool):
        if number in _STACK_TYPES:
            _skip("unsupported_stack_template")
        if number == 1:
            return
    _skip("unsupported_type")


def _portainer_key(identifier: object, index: int, title: str) -> str:
    text = ""
    if isinstance(identifier, int) and not isinstance(identifier, bool) and identifier >= 0:
        text = str(identifier)
    elif isinstance(identifier, str):
        text = identifier.strip()
    if KEY_PATTERN.fullmatch(text):
        return text
    return f"i{index}-{hashlib.sha256(title.encode('utf-8', 'replace')).hexdigest()[:8]}"


def _portainer_image(image: object, registry: object) -> str:
    if not isinstance(image, str) or not image.strip():
        _skip("invalid_image")
    name = image.strip()
    if isinstance(registry, str) and registry.strip():
        prefix = re.sub(r"^https?://", "", registry.strip(), flags=re.IGNORECASE).rstrip("/")
        if prefix and not name.startswith(f"{prefix}/"):
            name = f"{prefix}/{name}"
    return name


def _image(text: str) -> str:
    try:
        return split_reference(text).typed()
    except ReferenceInvalid:
        _skip("invalid_image")


def _text(value: object, limit: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    kept: list[str] = []
    for character in value[: limit * 4]:
        if character == "\n" and multiline:
            kept.append(character)
        elif character in "\r\n\t":
            kept.append(" " if character != "\r" else "")
        elif unicodedata.category(character) not in HIDDEN_CATEGORIES | {"Cs"}:
            kept.append(character)
    return "".join(kept)[:limit].strip()


def _flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return isinstance(value, str) and value.strip().lower() in _TRUE_TEXT


def _list_of(value: object, reason: str) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        _skip(reason)
    return value


def _items(value: object, reason: str) -> list[dict[str, Any]]:
    entries = _list_of(value, reason)
    if not all(isinstance(entry, dict) for entry in entries):
        _skip(reason)
    return cast(list[dict[str, Any]], entries)


def _scalar_text(value: object, limit: int, reason: str) -> str:
    match value:
        case None:
            return ""
        case bool():
            return "true" if value else "false"
        case int() | float():
            text = str(value)
        case str():
            text = value
        case _:
            _skip(reason)
    if len(text) > limit or any(unicodedata.category(c) in HIDDEN_CATEGORIES for c in text):
        _skip(reason)
    return text


def _categories(value: object) -> tuple[str, ...]:
    parts: list[object]
    if isinstance(value, str):
        parts = list(value.split(","))
    elif isinstance(value, list):
        parts = list(value)
    else:
        return ()
    found: dict[str, str] = {}
    for part in parts:
        text = _text(part, CATEGORY_MAX)
        if text:
            found.setdefault(text.casefold(), text)
    return tuple(found.values())[:CATEGORIES_MAX]


def _architectures(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    found: dict[str, None] = {}
    for entry in value[:SETTINGS_LIST_MAX]:
        raw = entry.get("arch") if isinstance(entry, dict) else entry
        name = _text(raw, 16).lower()
        mapped = _ARCHITECTURES.get(name, name)
        if _ARCH_NAME.fullmatch(mapped):
            found[mapped] = None
    return tuple(found)


def _port_spec(
    container: object, host: object, description: object, *, optional: bool
) -> TemplatePort:
    matched = _PORT_TEXT.fullmatch(_scalar_text(container, 32, "invalid_port").strip())
    if matched is None:
        _skip("invalid_port")
    embedded_host, container_port, protocol = matched.groups()
    return TemplatePort(
        container_port=_port_number(container_port),
        protocol=_protocol(protocol),
        host_port=_suggested_port(embedded_host if embedded_host is not None else host),
        description=_text(description, DESCRIPTION_MAX),
        optional=optional,
    )


def _port_number(text: str) -> int:
    number = int(text)
    if not 1 <= number <= 65535:
        _skip("invalid_port")
    return number


def _protocol(text: str | None) -> Protocol:
    match (text or "tcp").lower():
        case "tcp":
            return "tcp"
        case "udp":
            return "udp"
        case "sctp":
            return "sctp"
        case _:
            _skip("invalid_port")


def _suggested_port(value: object) -> int | None:
    text = (
        str(value).strip() if isinstance(value, str | int) and not isinstance(value, bool) else ""
    )
    if not text.isascii() or not text.isdigit() or len(text) > 5:
        return None
    number = int(text)
    return number if 1 <= number <= 65535 else None


def _linuxserver_port(entry: dict[str, Any]) -> TemplatePort:
    return _port_spec(
        entry.get("internal"),
        entry.get("external"),
        entry.get("desc"),
        optional=_flag(entry.get("optional")),
    )


def _portainer_port(entry: object) -> TemplatePort:
    return _port_spec(entry, None, "", optional=False)


def _path(value: object, reason: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or len(value) > PATH_MAX
        or ":" in value
        or any(unicodedata.category(c) in HIDDEN_CATEGORIES for c in value)
    ):
        _skip(reason)
    return value


def _linuxserver_volume(entry: dict[str, Any]) -> TemplateVolume:
    return TemplateVolume(
        container_path=_path(entry.get("path"), "invalid_volume"),
        description=_text(entry.get("desc"), DESCRIPTION_MAX),
        optional=_flag(entry.get("optional")),
    )


def _portainer_volume(entry: dict[str, Any]) -> TemplateVolume:
    bind = entry.get("bind")
    return TemplateVolume(
        container_path=_path(entry.get("container"), "invalid_volume"),
        bind=None if bind in (None, "") else _path(bind, "invalid_volume"),
        read_only=_flag(entry.get("readonly")),
    )


def _env_name(value: object) -> str:
    if not isinstance(value, str) or ENV_NAME_PATTERN.fullmatch(value.strip()) is None:
        _skip("invalid_env")
    return value.strip()


def _linuxserver_env(entry: dict[str, Any]) -> TemplateEnv:
    return TemplateEnv(
        name=_env_name(entry.get("name")),
        default=_scalar_text(entry.get("value"), ENV_DEFAULT_MAX, "invalid_env"),
        description=_text(entry.get("desc"), DESCRIPTION_MAX),
        optional=_flag(entry.get("optional")),
    )


def _portainer_env(entry: dict[str, Any]) -> TemplateEnv:
    options, chosen = _env_options(entry.get("select"))
    default = _scalar_text(entry.get("default"), ENV_DEFAULT_MAX, "invalid_env")
    return TemplateEnv(
        name=_env_name(entry.get("name")),
        default=default or chosen,
        description=_text(entry.get("description") or entry.get("label"), DESCRIPTION_MAX),
        preset=_flag(entry.get("preset")),
        options=options,
    )


def _env_options(value: object) -> tuple[tuple[TemplateEnvOption, ...], str]:
    entries = _items(value, "invalid_env")
    if len(entries) > ENV_OPTIONS_MAX:
        _skip("invalid_env")
    options: list[TemplateEnvOption] = []
    chosen = ""
    for entry in entries:
        option_value = _scalar_text(entry.get("value"), ENV_DEFAULT_MAX, "invalid_env")
        options.append(
            TemplateEnvOption(
                text=_text(entry.get("text"), TITLE_MAX) or option_value[:TITLE_MAX],
                value=option_value,
            )
        )
        if _flag(entry.get("default")) and not chosen:
            chosen = option_value
    return tuple(options), chosen


def _labels(value: object) -> tuple[TemplateLabel, ...]:
    entries = _items(value, "invalid_label")
    if len(entries) > LABELS_MAX:
        _skip("invalid_label")
    labels: list[TemplateLabel] = []
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str) or not name or len(name) > PATH_MAX:
            _skip("invalid_label")
        labels.append(
            TemplateLabel(
                name=_scalar_text(name, PATH_MAX, "invalid_label"),
                value=_scalar_text(entry.get("value"), ENV_DEFAULT_MAX, "invalid_label"),
            )
        )
    return tuple(labels)


def _optional_entries(value: object, reason: str) -> list[object]:
    return [entry for entry in _list_of(value, reason) if not _entry_optional(entry)]


def _entry_optional(entry: object) -> bool:
    return isinstance(entry, dict) and _flag(entry.get("optional"))


def _linuxserver_devices(value: object) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for entry in _optional_entries(value, "invalid_device"):
        if not isinstance(entry, dict):
            _skip("invalid_device")
        container = entry.get("path")
        host = entry.get("host_path") or container
        for part in (host, container):
            if not isinstance(part, str) or _DEVICE.fullmatch(part) is None:
                _skip("invalid_device")
        found[str(host) if host == container else f"{host}:{container}"] = None
    return _bounded(found, "invalid_device")


def _linuxserver_capabilities(value: object) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for entry in _optional_entries(value, "invalid_capability"):
        name = entry.get("cap_add") if isinstance(entry, dict) else entry
        if not isinstance(name, str) or _CAPABILITY.fullmatch(name.strip().upper()) is None:
            _skip("invalid_capability")
        found[name.strip().upper()] = None
    return _bounded(found, "invalid_capability")


def _linuxserver_security(value: object) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for entry in _optional_entries(value, "invalid_security_option"):
        text = (
            (entry.get("run_var") or entry.get("compose_var")) if isinstance(entry, dict) else entry
        )
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 128
            or any(unicodedata.category(c) in HIDDEN_CATEGORIES for c in text)
        ):
            _skip("invalid_security_option")
        found[text.strip()] = None
    return _bounded(found, "invalid_security_option")


@dataclass(frozen=True, slots=True)
class _Custom:
    sysctls: tuple[str, ...] = ()
    shm_size: str | None = None


def _linuxserver_custom(value: object) -> _Custom:
    sysctls: dict[str, None] = {}
    shm_size: str | None = None
    for entry in _items(value, "invalid_sysctl"):
        if _flag(entry.get("optional")):
            continue
        name = entry.get("name")
        text = entry.get("value")
        if name in _SYSCTL_NAMES:
            for item in text if isinstance(text, list) else [text]:
                if not isinstance(item, str) or _SYSCTL.fullmatch(item.strip()) is None:
                    _skip("invalid_sysctl")
                sysctls[item.strip()] = None
        elif name in _SHM_NAMES:
            if not isinstance(text, str) or _SHM_SIZE.fullmatch(text.strip()) is None:
                _skip("invalid_shm_size")
            shm_size = text.strip()
    return _Custom(_bounded(sysctls, "invalid_sysctl"), shm_size)


def _bounded(found: dict[str, None], reason: str) -> tuple[str, ...]:
    if len(found) > SETTINGS_LIST_MAX:
        _skip(reason)
    return tuple(found)


def _linuxserver_host(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() == "host"


def _linuxserver_hostname(value: object) -> str | None:
    if isinstance(value, dict):
        return None if _flag(value.get("optional")) else _hostname(value.get("hostname"))
    return _hostname(value)


def _hostname(value: object) -> str | None:
    text = _text(value, 253)
    return text if _HOSTNAME.fullmatch(text) else None


def _container_name(value: object) -> str | None:
    text = _text(value, 255)
    return text if NAME_PATTERN.fullmatch(text) else None


def _restart_policy(value: object) -> str | None:
    text = _text(value, 32).lower()
    return text if text in _RESTART_POLICIES else None


def _command(value: object) -> str | None:
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or len(value) > COMMAND_MAX
        or any(unicodedata.category(c) in HIDDEN_CATEGORIES for c in value)
    ):
        _skip("invalid_command")
    return value


def _https_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if url_refusal(text.partition("#")[0]) is None and len(text) <= URL_MAX else None
