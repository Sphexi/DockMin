"""What an app template sets for one viewer, as container form values and as a Compose file."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import yaml

from app.services.app_template import AppTemplate, TemplateEnv, TemplatePort
from app.services.container_spec import ROW_CAPS, parse_container_name
from app.services.stack_document import STACK_NAME

ENV_ROWS: Final = ROW_CAPS["env"]
LABEL_ROWS: Final = ROW_CAPS["labels"]
PORT_ROWS: Final = ROW_CAPS["ports"]
MOUNT_ROWS: Final = ROW_CAPS["mounts"]
DEVICE_ROWS: Final = ROW_CAPS["devices"]

_NAME_RUN: Final = re.compile(r"[^A-Za-z0-9_.-]+")
_STACK_RUN: Final = re.compile(r"[^a-z0-9_-]+")
_SEGMENT_RUN: Final = re.compile(r"[^A-Za-z0-9_.-]+")
_DOCKER_ARCHITECTURES: Final = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
    "armhf": "arm",
    "arm": "arm",
}
_VOLUME_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,254}")
_DEFAULT_SEGMENT: Final = "data"
_FALLBACK_SERVICE: Final = "app"


@dataclass(frozen=True, slots=True)
class Mount:
    """One volume or host path to mount."""

    kind: Literal["volume", "bind"]
    source: str
    target: str
    read_only: bool


@dataclass(frozen=True, slots=True)
class Settings:
    """What a template sets for a viewer; administrator-only settings are empty for anyone else.

    For an administrator, `elevated` names the elevated settings that were filled in and `unset`
    the ones the template asks for that are left for the administrator to set.
    """

    image: str
    name: str | None
    restart_policy: str | None
    command: str | None
    labels: tuple[tuple[str, str], ...]
    shm_size: str | None
    hostname: str | None
    interactive: bool
    ports: tuple[TemplatePort, ...]
    mounts: tuple[Mount, ...]
    env: tuple[TemplateEnv, ...]
    capabilities: tuple[str, ...]
    devices: tuple[str, ...]
    sysctls: tuple[str, ...]
    security_options: tuple[str, ...]
    host_network: bool
    withheld: tuple[str, ...]
    elevated: tuple[str, ...] = ()
    unset: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContainerPrefill:
    """The container form's initial values and what could not be put on it."""

    values: dict[str, str]
    withheld: str | None
    not_carried: str | None
    hints: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StackPrefill:
    """The stack form's suggested name and generated Compose file."""

    name: str
    compose_text: str
    withheld: str | None
    hints: tuple[str, ...]


def container_name(template: AppTemplate) -> str | None:
    """Return the container name the template suggests, or None when none fits the grammar."""
    for candidate, fold in ((template.name, False), (template.title, True)):
        text = _NAME_RUN.sub("-", (candidate or "").strip()).strip("-_.")
        try:
            name = parse_container_name(text.lower() if fold else text)
        except ValueError:
            continue
        if name is not None and len(name) >= 2:
            return name
    return None


def stack_name(template: AppTemplate) -> str:
    """Return a stack name the template suggests, empty when none fits the grammar."""
    for candidate in (template.name, template.title, template.key):
        text = _STACK_RUN.sub("-", (candidate or "").strip().lower()).strip("-_")[:63]
        if STACK_NAME.fullmatch(text):
            return text
    return ""


def canonical_architecture(name: str) -> str:
    """Return Docker's platform name for an architecture as a host reports it."""
    folded = name.strip().lower()
    return _DOCKER_ARCHITECTURES.get(folded, folded)


def architecture_note(published: Sequence[str], host: str | None) -> str | None:
    """Return "not published for <architecture>" when the host's is not among the published ones."""
    if not published or not host or not host.strip():
        return None
    wanted = canonical_architecture(host)
    if wanted in published:
        return None
    return f"not published for {wanted}"


def settings_for(template: AppTemplate, *, is_administrator: bool) -> Settings:
    """Resolve the template into what its viewer may set; the rest is named in `withheld`."""
    base = container_name(template) or _volume_prefix(template.key)
    mounts, withheld = _mounts(template, base, is_administrator=is_administrator)
    host_network = template.host_network and is_administrator
    asked: list[str] = []
    elevated: list[str] = []
    unset: list[str] = []
    if not is_administrator:
        asked = _admin_requests(template)
    else:
        elevated = _elevated_requests(template)
        unset = _unset_requests(template)
    return Settings(
        image=template.image,
        name=container_name(template),
        restart_policy=template.restart_policy,
        command=template.command,
        labels=tuple((label.name, label.value) for label in template.labels),
        shm_size=template.shm_size,
        hostname=template.hostname,
        interactive=template.interactive,
        ports=() if host_network else tuple(p for p in template.ports if not p.optional),
        mounts=mounts,
        env=tuple(entry for entry in template.env if not entry.optional),
        capabilities=template.capabilities if is_administrator else (),
        devices=template.devices if is_administrator else (),
        sysctls=template.sysctls if is_administrator else (),
        security_options=template.security_options if is_administrator else (),
        host_network=host_network,
        withheld=(*asked, *withheld),
        elevated=tuple(elevated),
        unset=tuple(unset),
    )


def withheld_notice(settings: Settings) -> str | None:
    """Return the notice naming the elevated settings a template carries, or None."""
    if settings.withheld:
        return (
            f"This template asks for {', '.join(settings.withheld)}, "
            "which only an administrator can set."
        )
    sentences: list[str] = []
    if settings.elevated:
        sentences.append(
            f"This template sets elevated options: {', '.join(settings.elevated)}. "
            "Review them before you create it."
        )
    if settings.unset:
        lead = "It also asks" if settings.elevated else "This template asks"
        sentences.append(
            f"{lead} for {', '.join(settings.unset)}, which are not filled in. "
            "Set them yourself if you want them."
        )
    return " ".join(sentences) or None


def container_prefill(template: AppTemplate, *, is_administrator: bool) -> ContainerPrefill:
    """Build the container form's initial values from a template."""
    settings = settings_for(template, is_administrator=is_administrator)
    values: dict[str, str] = {"image": settings.image}
    skipped: list[str] = []
    beyond: list[str] = []
    if settings.name:
        values["name"] = settings.name
    if settings.restart_policy:
        values["restart_policy"] = settings.restart_policy
    if settings.command:
        values["command"] = settings.command
    if settings.shm_size:
        values["shm_size"] = settings.shm_size
    env = _capped(settings.env, ENV_ROWS, "environment variables", beyond)
    _rows(values, "env", [(entry.name, entry.default) for entry in env])
    _rows(values, "label", _capped(settings.labels, LABEL_ROWS, "labels", beyond))
    for index, port in enumerate(_capped(settings.ports, PORT_ROWS, "ports", beyond)):
        values[f"port-{index}-container_port"] = str(port.container_port)
        values[f"port-{index}-protocol"] = port.protocol
        if port.host_port is not None:
            values[f"port-{index}-host_port"] = str(port.host_port)
    for index, mount in enumerate(_capped(settings.mounts, MOUNT_ROWS, "mounts", beyond)):
        values[f"mount-{index}-type"] = mount.kind
        values[f"mount-{index}-source"] = mount.source
        values[f"mount-{index}-target"] = mount.target
        if mount.read_only:
            values[f"mount-{index}-read_only"] = "on"
    for index, device in enumerate(_capped(settings.devices, DEVICE_ROWS, "devices", beyond)):
        host, _, inside = device.partition(":")
        values[f"device-{index}-host_path"] = host
        values[f"device-{index}-container_path"] = inside or host
    if settings.capabilities:
        values["cap_add"] = " ".join(settings.capabilities)
    if settings.host_network:
        values["network_mode"] = "host"
    if settings.hostname:
        skipped.append(f"the host name {settings.hostname}")
    if settings.interactive:
        skipped.append("interactive mode")
    if settings.sysctls:
        skipped.append(f"the kernel parameters {', '.join(settings.sysctls)}")
    if settings.security_options:
        skipped.append(f"the security options {', '.join(settings.security_options)}")
    notes = list(beyond)
    if skipped:
        notes.append(
            f"The create form has no field for {', '.join(skipped)}, so they were not carried over."
        )
    not_carried = " ".join(notes) or None
    return ContainerPrefill(
        values=values,
        withheld=withheld_notice(settings),
        not_carried=not_carried,
        hints=env_hints(settings.env),
    )


def stack_prefill(template: AppTemplate, *, is_administrator: bool) -> StackPrefill:
    """Build the stack form's name and a one-service Compose file from a template."""
    settings = settings_for(template, is_administrator=is_administrator)
    name = stack_name(template)
    return StackPrefill(
        name=name,
        compose_text=compose_text(settings, name or _FALLBACK_SERVICE),
        withheld=withheld_notice(settings),
        hints=env_hints(settings.env),
    )


def compose_text(settings: Settings, service_name: str) -> str:
    """Write the one-service Compose file for resolved settings with the safe YAML dumper."""
    service: dict[str, Any] = {"image": settings.image}
    if settings.restart_policy:
        service["restart"] = settings.restart_policy
    if settings.hostname:
        service["hostname"] = settings.hostname
    if settings.interactive:
        service["tty"] = True
        service["stdin_open"] = True
    if settings.command:
        service["command"] = _escape(settings.command)
    if settings.shm_size:
        service["shm_size"] = settings.shm_size
    if settings.host_network:
        service["network_mode"] = "host"
    if settings.ports:
        service["ports"] = [_port_text(port) for port in settings.ports]
    if settings.mounts:
        service["volumes"] = [_mount_text(mount) for mount in settings.mounts]
    if settings.env:
        service["environment"] = {entry.name: _escape(entry.default) for entry in settings.env}
    if settings.labels:
        service["labels"] = {name: _escape(value) for name, value in settings.labels}
    if settings.capabilities:
        service["cap_add"] = list(settings.capabilities)
    if settings.devices:
        service["devices"] = [_device_text(device) for device in settings.devices]
    if settings.sysctls:
        service["sysctls"] = [_escape(item) for item in settings.sysctls]
    if settings.security_options:
        service["security_opt"] = [_escape(item) for item in settings.security_options]
    document: dict[str, Any] = {"services": {_service_key(service_name): service}}
    named = [mount.source for mount in settings.mounts if mount.kind == "volume"]
    if named:
        document["volumes"] = {source: {} for source in named}
    return yaml.safe_dump(
        document, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000
    )


def _escape(text: str) -> str:
    return text.replace("$", "$$")


def _service_key(name: str) -> str:
    return name if name else _FALLBACK_SERVICE


def _port_text(port: TemplatePort) -> str:
    inside = f"{port.container_port}/{port.protocol}"
    return inside if port.host_port is None else f"{port.host_port}:{inside}"


def _mount_text(mount: Mount) -> str:
    text = f"{_escape(mount.source)}:{_escape(mount.target)}"
    return f"{text}:ro" if mount.read_only else text


def _device_text(device: str) -> str:
    return device if ":" in device else f"{device}:{device}"


def _capped[T](items: Sequence[T], cap: int, noun: str, notes: list[str]) -> Sequence[T]:
    if len(items) > cap:
        notes.append(f"{len(items) - cap} {noun} were not carried over: the form holds {cap}.")
    return items[:cap]


def _rows(values: dict[str, str], prefix: str, rows: Sequence[tuple[str, str]]) -> None:
    for index, (key, value) in enumerate(rows):
        values[f"{prefix}-{index}-key"] = key
        values[f"{prefix}-{index}-value"] = value


def env_hints(env: Sequence[TemplateEnv]) -> tuple[str, ...]:
    """List each environment variable that offers choices, with its choices."""
    return tuple(
        f"{entry.name}: {', '.join(option.value for option in entry.options)}"
        for entry in env
        if entry.options
    )


def _volume_prefix(key: str) -> str:
    text = _SEGMENT_RUN.sub("-", key).strip("-_.")
    return text if text else _FALLBACK_SERVICE


def _volume_name(prefix: str, target: str, taken: set[str]) -> str:
    segment = _SEGMENT_RUN.sub("-", target.rstrip("/").rpartition("/")[2]).strip("-_.")
    base = f"{prefix}-{segment or _DEFAULT_SEGMENT}"[:250]
    name, number = base, 1
    while name in taken:
        number += 1
        name = f"{base}-{number}"
    taken.add(name)
    return name if _VOLUME_NAME.fullmatch(name) else f"{_FALLBACK_SERVICE}-{_DEFAULT_SEGMENT}"


def _mounts(
    template: AppTemplate, prefix: str, *, is_administrator: bool
) -> tuple[tuple[Mount, ...], list[str]]:
    mounts: list[Mount] = []
    binds: list[str] = []
    taken: set[str] = set()
    for volume in template.volumes:
        if volume.optional:
            continue
        if volume.bind is not None:
            if not is_administrator:
                binds.append(volume.bind)
            continue
        source = _volume_name(prefix, volume.container_path, taken)
        mounts.append(Mount("volume", source, volume.container_path, volume.read_only))
    withheld = [f"host bind mounts ({', '.join(binds)})"] if binds else []
    return tuple(mounts), withheld


def _elevated_requests(template: AppTemplate) -> list[str]:
    elevated: list[str] = []
    if template.host_network:
        elevated.append("host networking")
    if template.capabilities:
        elevated.append(f"added capabilities ({', '.join(template.capabilities)})")
    if template.devices:
        elevated.append(f"device mappings ({', '.join(template.devices)})")
    if template.security_options:
        elevated.append(f"security options ({', '.join(template.security_options)})")
    return elevated


def _unset_requests(template: AppTemplate) -> list[str]:
    unset: list[str] = []
    if template.privileged:
        unset.append("privileged mode")
    binds = [volume.bind for volume in template.volumes if volume.bind and not volume.optional]
    if binds:
        unset.append(f"host bind mounts ({', '.join(binds)})")
    return unset


def _admin_requests(template: AppTemplate) -> list[str]:
    asked: list[str] = []
    if template.capabilities:
        asked.append(f"added capabilities ({', '.join(template.capabilities)})")
    if template.devices:
        asked.append(f"device mappings ({', '.join(template.devices)})")
    if template.sysctls:
        asked.append(f"kernel parameters ({', '.join(template.sysctls)})")
    if template.security_options:
        asked.append(f"security options ({', '.join(template.security_options)})")
    if template.host_network:
        asked.append("host networking")
    if template.privileged:
        asked.append("privileged mode")
    return asked
