"""What a stack import would create: the plan, its parts and the digest a confirmation posts."""

import hashlib
import hmac
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

import yaml

from app.services.pull_credentials import CredentialChoice
from app.services.stack_export_host import bind_source
from app.services.stack_import_archive import VolumeSummary

__all__ = [
    "HOST_PATH_CHOICES",
    "Collision",
    "HostPath",
    "ImageEntry",
    "ImportChoices",
    "ImportPlan",
    "PlanObject",
    "SecretEntry",
    "SkippedVolume",
    "VolumePlan",
    "candidate_sources",
    "host_path_uses",
    "plan_digest",
    "replace_host_paths",
    "volume_key_for_path",
]

KEEP: Final = "keep"
VOLUME: Final = "volume"
HOST_PATH_CHOICES: Final = (KEEP, VOLUME)
REPLACEMENT_KEY: Final = "hostpath-"

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


@dataclass(frozen=True, slots=True)
class PlanObject:
    """An object the import creates, or the deployment will."""

    kind: str
    name: str
    note: str = ""


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
class HostPath:
    """A host path the stack file binds, the services that use it and the choice made."""

    source: str
    uses: tuple[str, ...]
    choice: str | None = None
    volume: str | None = None


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
    digest: str = ""

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
        """Report whether the file was checked and no name collides."""
        return self.checked and not self.collisions

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


def host_path_uses(text: str, companions: frozenset[str]) -> dict[str, tuple[str, ...]]:
    """Return each host path the file binds with the services that bind it; companions excluded."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return {}
    found: dict[str, set[str]] = {}
    for service_name, service in services.items():
        entries = service.get("volumes") if isinstance(service, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            source = bind_source(entry)
            if source is None or _is_companion(source, companions):
                continue
            found.setdefault(source, set()).add(str(service_name))
    return {path: tuple(sorted(users)) for path, users in sorted(found.items())}


def _is_companion(source: str, companions: frozenset[str]) -> bool:
    name = source.removeprefix("./")
    return "/" not in name and name in companions


def volume_key_for_path(index: int) -> str:
    """Return the Compose key of the named volume that replaces the index'th host path."""
    return f"{REPLACEMENT_KEY}{index}"


def replace_host_paths(text: str, replacements: Mapping[str, str]) -> str:
    """Rewrite the file so each listed host path is a named volume with the given key.

    Short and long volume syntax are both rewritten and the volumes are declared at the top level.
    ValueError when the file cannot be read as YAML. The text is returned as it is when nothing
    is replaced.
    """
    if not replacements:
        return text
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError("the stack file cannot be rewritten") from None
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        raise ValueError("the stack file cannot be rewritten")
    for service in data["services"].values():
        entries = service.get("volumes") if isinstance(service, dict) else None
        if isinstance(entries, list):
            service["volumes"] = [_replaced(entry, replacements) for entry in entries]
    declared = data.get("volumes")
    if not isinstance(declared, dict):
        declared = {}
    for key in replacements.values():
        declared.setdefault(key, {})
    data["volumes"] = declared
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)


def _replaced(entry: object, replacements: Mapping[str, str]) -> object:
    source = bind_source(entry)
    key = replacements.get(source) if source is not None else None
    if key is None:
        return entry
    if isinstance(entry, str):
        return f"{key}:{entry.split(':', 1)[1]}"
    if isinstance(entry, dict):
        rewritten: dict[str, Any] = {name: value for name, value in entry.items() if name != "bind"}
        rewritten["type"] = "volume"
        rewritten["source"] = key
        return rewritten
    return entry


def plan_digest(plan: ImportPlan, archive_digest: str) -> str:
    """Return a keyed digest over what the review shows of the archive and of the host.

    The name is part of it, since it decides every name the import creates. What the
    administrator chooses beside it (the fate of each host path, the credential sets, secret
    values) is not, so deciding those does not change the digest; a volume that replaces a host
    path is left out for the same reason. The key never leaves the process, so a digest made
    before a restart no longer matches.
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
            (item.key, item.name, item.source, item.entries, item.size, item.origin)
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
