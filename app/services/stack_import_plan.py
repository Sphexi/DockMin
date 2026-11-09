"""What a stack import would create: the plan, its parts and the digest a confirmation posts."""

import hashlib
import hmac
import json
import os
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

import yaml

from app.services.pull_credentials import CredentialChoice
from app.services.stack_import_archive import VolumeSummary

__all__ = [
    "HOST_PATH_CHOICES",
    "Collision",
    "HostPath",
    "HostPathUnresolved",
    "HostUse",
    "ImageEntry",
    "ImportChoices",
    "ImportPlan",
    "MountTarget",
    "PlanObject",
    "Rewrite",
    "SecretEntry",
    "SkippedVolume",
    "VolumePlan",
    "candidate_sources",
    "compose_host_uses",
    "mount_target",
    "plan_digest",
    "replace_host_paths",
    "swarm_host_uses",
    "volume_key_for_path",
]

KEEP: Final = "keep"
VOLUME: Final = "volume"
HOST_PATH_CHOICES: Final = (KEEP, VOLUME)
REPLACEMENT_KEY: Final = "hostpath-"
_MEMORY_TYPES: Final = frozenset({"", "tmpfs"})

_DIGEST_KEY: Final = os.urandom(32)

type SecretState = Literal["opened", "entered", "needed"]
type ImageStatus = Literal["same", "differs", "not_present", "not_recorded"]


@dataclass(frozen=True, slots=True)
class ImportChoices:
    """What the administrator decided on the review page."""

    name: str
    host_paths: Mapping[str, str] = field(default_factory=dict)
    credentials: Mapping[str, CredentialChoice] = field(default_factory=dict)
    entered: Mapping[str, str] = field(default_factory=dict, repr=False)
    confirmed_mounts: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PlanObject:
    """An object the import creates, or the deployment will."""

    kind: str
    name: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class MountTarget:
    """The host path or share a volume's options bind, and the token that confirms writing to it."""

    device: str
    kind: str
    token: str

    @property
    def label(self) -> str:
        """Return what the review names: the device, or the filesystem type when there is none."""
        return self.device or self.kind


@dataclass(frozen=True, slots=True)
class VolumePlan:
    """A volume of the stack: `source` is the archive volume that fills it, if one does."""

    key: str
    name: str
    source: str | None
    prefix: str | None
    entries: int
    size: int
    origin: Literal["archive", "host_path", "declared"]
    driver: str = "local"
    options: Mapping[str, str] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    mount: MountTarget | None = None
    mount_confirmed: bool = False
    protected: bool = False

    @property
    def filled(self) -> bool:
        """Report whether the import creates this volume and writes archived data into it."""
        return self.prefix is not None and self.entries > 0


@dataclass(frozen=True, slots=True)
class SkippedVolume:
    """An archived volume the import does not write, and why."""

    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class HostUse:
    """Where the resolved stack uses one host path: bind mounts and volumes with a device option."""

    binds: tuple[tuple[str, str], ...] = ()
    volumes: tuple[str, ...] = ()

    @property
    def users(self) -> tuple[str, ...]:
        """Return the services that bind the path and the volumes whose device it is."""
        return tuple(
            sorted({service for service, _ in self.binds})
            + [f"volume {key}" for key in sorted(self.volumes)]
        )


@dataclass(frozen=True, slots=True)
class HostPath:
    """A host path the resolved stack uses, where, and the choice made."""

    source: str
    uses: tuple[str, ...]
    choice: str | None = None
    volume: str | None = None
    use: HostUse = field(default_factory=HostUse)


@dataclass(frozen=True, slots=True)
class ImageEntry:
    """An image the stack names; `status` compares the archive's digest with the target's."""

    reference: str
    tag: str | None
    archive_digest: str | None
    local_digest: str | None
    status: ImageStatus
    used_by: tuple[str, ...]
    pulled: bool


@dataclass(frozen=True, slots=True)
class Collision:
    """A name the import would create that something already holds."""

    kind: str
    name: str
    text: str


@dataclass(frozen=True, slots=True)
class SecretEntry:
    """A secret variable or file of the archive and whether its value is known now."""

    sealed_name: str
    kind: Literal["variable", "file"]
    name: str
    state: SecretState
    archive_sealed: bool


@dataclass(frozen=True, slots=True)
class ImportPlan:
    """Everything the import would create, with the digest the confirmation carries."""

    name: str
    kind: Literal["compose", "swarm"]
    source_name: str
    compose_text: str
    objects: tuple[PlanObject, ...]
    volumes: tuple[VolumePlan, ...]
    skipped_volumes: tuple[SkippedVolume, ...]
    host_paths: tuple[HostPath, ...]
    images: tuple[ImageEntry, ...]
    collisions: tuple[Collision, ...]
    secrets: tuple[SecretEntry, ...]
    variables: tuple[tuple[str, bool], ...]
    files: tuple[tuple[str, int, bool], ...]
    contexts: tuple[tuple[str, int], ...]
    warnings: tuple[str, ...]
    checked: bool
    archive_volumes: int = 0
    archive_bytes: int = 0
    digest: str = ""

    @property
    def unconfirmed_mounts(self) -> tuple[VolumePlan, ...]:
        """Return the volumes written onto a host path or share that nobody confirmed."""
        return tuple(
            item for item in self.volumes if item.filled and item.mount and not item.mount_confirmed
        )

    @property
    def protected_volumes(self) -> tuple[VolumePlan, ...]:
        """Return the volumes whose device is a path dockmin's own container mounts."""
        return tuple(item for item in self.volumes if item.filled and item.protected)

    @property
    def secrets_needed(self) -> tuple[SecretEntry, ...]:
        """Return the secrets whose value is neither opened nor entered."""
        return tuple(item for item in self.secrets if item.state == "needed")

    @property
    def paths_undecided(self) -> tuple[HostPath, ...]:
        """Return the host paths with no choice yet."""
        return tuple(item for item in self.host_paths if item.choice is None)

    @property
    def can_import(self) -> bool:
        """Report whether the file was checked, no name collides and no device is dockmin's own."""
        return self.checked and not self.collisions and not self.protected_volumes

    @property
    def data_bytes(self) -> int:
        """Return the file data the import writes."""
        return sum(item.size for item in self.volumes if item.filled)

    @property
    def has_data(self) -> bool:
        """Report whether the import creates any volume and writes data into it."""
        return any(item.filled for item in self.volumes)

    def with_digest(self, archive_digest: str) -> "ImportPlan":
        """Return the plan carrying the digest of what it shows."""
        return replace(self, digest=plan_digest(self, archive_digest))


def candidate_sources(target: str, project: str, source_project: str) -> tuple[str, ...]:
    """Return the archive volume names that can fill the volume called `target`.

    A name Compose derived from the project is looked for under the archive's project name, and
    any other name only as it is.
    """
    prefix = f"{project}_"
    if target.startswith(prefix):
        return (f"{source_project}_{target[len(prefix) :]}", target)
    return (target,)


def match_volumes(
    declared: Sequence[tuple[str, str]],
    archived: Sequence[VolumeSummary],
    project: str,
    source_project: str,
) -> tuple[dict[str, VolumeSummary], tuple[SkippedVolume, ...]]:
    """Pair each declared (key, name) with the archived volume that fills it; list the rest."""
    by_name = {item.manifest.name: item for item in archived}
    matched: dict[str, VolumeSummary] = {}
    taken: set[str] = set()
    for key, name in declared:
        for candidate in candidate_sources(name, project, source_project):
            found = by_name.get(candidate)
            if found is not None and found.manifest.name not in taken:
                matched[key] = found
                taken.add(found.manifest.name)
                break
    skipped = tuple(
        SkippedVolume(
            item.manifest.name,
            "The stack file declares no volume that this one fills.",
        )
        for item in archived
        if item.manifest.name not in taken
    )
    return matched, skipped


class HostPathUnresolved(ValueError):
    """A bind mount of the resolved stack names no usable source path."""

    def __init__(self, service: str, target: str) -> None:
        super().__init__(
            f"The bind mount of service {service} at {target} has no source path that can be "
            "resolved."
        )


def mount_target(
    options: Mapping[str, str], *, name: str, archive_digest: str
) -> MountTarget | None:
    """Return what needs confirming when the options bind a host path or mount a share."""
    device = options.get("device", "").strip()
    kind = options.get("type", "").strip()
    if not device and kind.casefold() in _MEMORY_TYPES:
        return None
    material = "\0".join((archive_digest, name, kind, device))
    return MountTarget(device, kind, hashlib.sha256(material.encode("utf-8")).hexdigest())


def _project_relative(source: str, work_dirs: Sequence[str]) -> str | None:
    """Return `./rest` for a source below a scratch project directory, else None."""
    for base in sorted(work_dirs, key=len, reverse=True):
        prefix = base.rstrip("/") + "/"
        if source.startswith(prefix):
            _, _, rest = source[len(prefix) :].partition("/")
            return f"./{rest}"
    return None


def _bind_path(
    raw: object, service: str, target: str, companions: Collection[str], work_dirs: Sequence[str]
) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        raise HostPathUnresolved(service, target)
    source = _project_relative(raw, work_dirs) or raw
    if source in (".", ".."):
        return source
    if source.startswith(("./", "../")):
        name = source.removeprefix("./")
        return None if "/" not in name and name in companions else source
    return source


def compose_host_uses(
    services: Mapping[str, Any],
    volumes: Mapping[str, Any],
    companions: Collection[str],
    work_dirs: Sequence[str] = (),
) -> dict[str, HostUse]:
    """Return each host path the resolved Compose model uses, with where; companion files excluded.

    Every bind mount counts, and every volume with a device option. HostPathUnresolved for a
    bind mount with no source.
    """
    binds: dict[str, list[tuple[str, str]]] = {}
    devices: dict[str, list[str]] = {}
    for service_name, service in services.items():
        entries = service.get("volumes") if isinstance(service, Mapping) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping) or entry.get("type") != "bind":
                continue
            target = str(entry.get("target", ""))
            path = _bind_path(entry.get("source"), str(service_name), target, companions, work_dirs)
            if path is not None:
                binds.setdefault(path, []).append((str(service_name), target))
    for key, declaration in volumes.items():
        options = declaration.get("driver_opts") if isinstance(declaration, Mapping) else None
        device = options.get("device") if isinstance(options, Mapping) else None
        if isinstance(declaration, Mapping) and declaration.get("external"):
            continue
        if isinstance(device, str) and device.strip():
            devices.setdefault(device.strip(), []).append(str(key))
    return _uses(binds, devices)


def swarm_host_uses(
    services: Sequence[tuple[str, Mapping[str, Any]]], namespace: str
) -> dict[str, HostUse]:
    """Return each host path the translated Swarm services use: bind sources and volume devices."""
    binds: dict[str, list[tuple[str, str]]] = {}
    devices: dict[str, list[str]] = {}
    for key, spec in services:
        task = spec.get("TaskTemplate")
        container = task.get("ContainerSpec") if isinstance(task, Mapping) else None
        mounts = container.get("Mounts") if isinstance(container, Mapping) else None
        for mount in mounts if isinstance(mounts, list) else ():
            if not isinstance(mount, Mapping):
                continue
            target = str(mount.get("Target", ""))
            if mount.get("Type") == "bind":
                source = mount.get("Source")
                if not isinstance(source, str) or not source.strip():
                    raise HostPathUnresolved(key, target)
                binds.setdefault(source, []).append((key, target))
                continue
            options = _driver_options(mount)
            device = options.get("device")
            if isinstance(device, str) and device.strip():
                name = str(mount.get("Source", ""))
                volume = name.removeprefix(f"{namespace}_")
                devices.setdefault(device.strip(), []).append(volume)
    return _uses(binds, devices)


def _driver_options(mount: Mapping[str, Any]) -> Mapping[str, Any]:
    volume = mount.get("VolumeOptions")
    config = volume.get("DriverConfig") if isinstance(volume, Mapping) else None
    options = config.get("Options") if isinstance(config, Mapping) else None
    return options if isinstance(options, Mapping) else {}


def _uses(
    binds: Mapping[str, Sequence[tuple[str, str]]], devices: Mapping[str, Sequence[str]]
) -> dict[str, HostUse]:
    paths = sorted(set(binds) | set(devices))
    return {
        path: HostUse(
            tuple(sorted(set(binds.get(path, ())))), tuple(sorted(set(devices.get(path, ()))))
        )
        for path in paths
    }


def volume_key_for_path(index: int) -> str:
    """Return the Compose key of the named volume that replaces the index'th host path."""
    return f"{REPLACEMENT_KEY}{index}"


@dataclass(frozen=True, slots=True)
class Rewrite:
    """The edits that replace host paths: bind mounts to retarget, volumes to make plain or add.

    `binds` maps (service, target) to the key of the named volume that takes the mount over,
    `plain` lists the volumes whose driver options are dropped, and `declare` the new volumes.
    """

    binds: Mapping[tuple[str, str], str] = field(default_factory=dict)
    plain: Collection[str] = ()
    declare: Collection[str] = ()

    def __bool__(self) -> bool:
        return bool(self.binds or self.plain or self.declare)


def _flow(value: object) -> str:
    dumped = yaml.safe_dump(
        value, default_flow_style=True, width=2**20, sort_keys=False, allow_unicode=True
    )
    return dumped.removesuffix("\n...\n").strip()


def _construct(node: yaml.Node) -> Any:
    loader = yaml.SafeLoader("")
    try:
        return loader.construct_object(node, deep=True)
    finally:
        loader.dispose()


def _entry(mapping: yaml.MappingNode, key: str) -> yaml.Node | None:
    for key_node, value_node in mapping.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            found: yaml.Node = value_node
            return found
    return None


def _content_end(node: yaml.Node) -> int:
    """Return where the node's content ends; a block collection's mark runs on past it."""
    if isinstance(node, yaml.MappingNode) and not node.flow_style and node.value:
        return _content_end(node.value[-1][1])
    if isinstance(node, yaml.SequenceNode) and not node.flow_style and node.value:
        return _content_end(node.value[-1])
    end: int = node.end_mark.index
    return end


def _span(node: yaml.Node) -> tuple[int, int]:
    return node.start_mark.index, _content_end(node)


def _bind_edit(
    item: yaml.Node, service: str, binds: Mapping[tuple[str, str], str], done: set[tuple[str, str]]
) -> str | None:
    if isinstance(item, yaml.ScalarNode):
        parts = str(item.value).split(":")
        key = binds.get((service, parts[1])) if len(parts) > 1 else None
        if key is None:
            return None
        done.add((service, parts[1]))
        return _flow(":".join([key, *parts[1:]]))
    if isinstance(item, yaml.MappingNode):
        entry = _construct(item)
        target = str(entry.get("target", "")) if isinstance(entry, dict) else ""
        key = binds.get((service, target))
        if key is None or entry.get("type") != "bind":
            return None
        done.add((service, target))
        rewritten = {name: value for name, value in entry.items() if name != "bind"}
        rewritten["type"] = "volume"
        rewritten["source"] = key
        return _flow(rewritten)
    return None


def _without_driver(declaration: object) -> object:
    if not isinstance(declaration, dict):
        return declaration
    return {
        key: value
        for key, value in declaration.items()
        if key != "driver_opts" and not (key == "driver" and value in ("", "local"))
    }


def _declaration_edits(
    root: yaml.MappingNode, text: str, rewrite: Rewrite, edits: dict[tuple[int, int], str]
) -> None:
    declared = _entry(root, "volumes")
    new = list(rewrite.declare)
    if declared is None:
        if new:
            lead = "" if not text or text.endswith("\n") else "\n"
            body = "".join(f"  {key}: {{}}\n" for key in new)
            edits[(len(text), len(text))] = f"{lead}volumes:\n{body}"
        return
    if isinstance(declared, yaml.ScalarNode) and declared.tag.endswith("null"):
        edits[_span(declared)] = " " + _flow({key: {} for key in new})
        return
    if not isinstance(declared, yaml.MappingNode):
        raise ValueError("the stack file cannot be rewritten")
    present: set[str] = set()
    for key_node, value_node in declared.value:
        name = str(key_node.value) if isinstance(key_node, yaml.ScalarNode) else ""
        present.add(name)
        if name in rewrite.plain:
            edits[_span(value_node)] = _flow(_without_driver(_construct(value_node)))
    new = [key for key in new if key not in present]
    if not new:
        return
    if declared.flow_style or not declared.value:
        merged = {**(_construct(declared) or {}), **{key: {} for key in new}}
        edits[_span(declared)] = _flow(merged)
        return
    column = declared.value[0][0].start_mark.column
    position = declared.end_mark.index
    lead = "" if position == 0 or text[position - 1] == "\n" else "\n"
    edits[(position, position)] = lead + "".join(f"{' ' * column}{key}: {{}}\n" for key in new)


def replace_host_paths(text: str, rewrite: Rewrite) -> str:
    """Return the file with each bind mount in `rewrite` retargeted and its volumes declared.

    Only the nodes that change are re-emitted; every other character of the file is kept as
    written. ValueError when the file cannot be read as a mapping or a bind mount is not
    written in the file's own services.
    """
    if not rewrite:
        return text
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        raise ValueError("the stack file cannot be rewritten") from None
    if not isinstance(root, yaml.MappingNode):
        raise ValueError("the stack file cannot be rewritten")
    services = _entry(root, "services")
    if not isinstance(services, yaml.MappingNode):
        raise ValueError("the stack file cannot be rewritten")
    edits: dict[tuple[int, int], str] = {}
    done: set[tuple[str, str]] = set()
    for key_node, service_node in services.value:
        entries = (
            _entry(service_node, "volumes") if isinstance(service_node, yaml.MappingNode) else None
        )
        if not isinstance(entries, yaml.SequenceNode) or not isinstance(key_node, yaml.ScalarNode):
            continue
        for item in entries.value:
            replacement = _bind_edit(item, str(key_node.value), rewrite.binds, done)
            if replacement is not None:
                edits[_span(item)] = replacement
    if done != set(rewrite.binds):
        raise ValueError("a bind mount is not written in the stack file's own services")
    _declaration_edits(root, text, rewrite, edits)
    for (start, end), replacement in sorted(edits.items(), reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def plan_digest(plan: ImportPlan, archive_digest: str) -> str:
    """Return a keyed digest over what the review shows of the archive and of the host.

    Choices made beside it (the fate of each host path, credential sets, secret values,
    confirmed mounts) are left out, and so is a volume that replaces a host path. The key
    never leaves the process, so a digest made before a restart no longer matches.
    """
    replaced = {f"{plan.name}_{item.volume}" for item in plan.host_paths if item.volume}
    document = {
        "archive": archive_digest,
        "name": plan.name,
        "kind": plan.kind,
        "checked": plan.checked,
        "objects": [
            (item.kind, item.name)
            for item in plan.objects
            if not (item.kind == "volume" and item.name in replaced)
        ],
        "volumes": [
            (
                item.key,
                item.name,
                item.source,
                item.entries,
                item.size,
                item.origin,
                sorted(item.options.items()),
                None if item.mount is None else (item.mount.device, item.mount.kind),
                item.protected,
            )
            for item in plan.volumes
            if item.name not in replaced
        ],
        "skipped": [item.name for item in plan.skipped_volumes],
        "paths": [item.source for item in plan.host_paths],
        "images": [(item.reference, item.status, item.local_digest) for item in plan.images],
        "collisions": [
            (item.kind, item.name) for item in plan.collisions if item.name not in replaced
        ],
        "variables": list(plan.variables),
        "files": list(plan.files),
        "contexts": list(plan.contexts),
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(_DIGEST_KEY, encoded, hashlib.sha256).hexdigest()
