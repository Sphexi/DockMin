"""What a container import would create and ask for: the plan, its parts and its digest."""

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final, Literal

__all__ = [
    "HOST_PATH_CHOICES",
    "ContainerChoices",
    "ContainerImportPlan",
    "EnvironmentPlan",
    "HostPathPlan",
    "ImageStatus",
    "MountEntry",
    "NetworkPlan",
    "SkippedVolume",
    "VolumePlan",
    "mount_entries",
    "plan_digest",
    "suggest_name",
]

KEEP: Final = "keep"
VOLUME: Final = "volume"
HOST_PATH_CHOICES: Final = (KEEP, VOLUME)

_DIGEST_KEY: Final = os.urandom(32)
_MOUNT_FIELD: Final = re.compile(r"mount-([0-9]{1,4})-(type|source|target|read_only)")
_SUFFIX_LIMIT: Final = 100
_NAME_LIMIT: Final = 255

type SecretState = Literal["plain", "opened", "entered", "needed"]
type ImageStatus = Literal["same", "differs", "not_present", "not_recorded"]
type NetworkAction = Literal["create", "reuse"]


@dataclass(frozen=True, slots=True)
class ContainerChoices:
    """What the administrator decided on the review page.

    `volume_names` maps an archived volume's name to the name it is created under, `host_paths`
    a host path to `keep` or `volume`, and `path_volumes` a replaced host path to its new
    volume's name. `entered` maps a sealed name to a value typed in.
    """

    volume_names: Mapping[str, str] = field(default_factory=dict)
    host_paths: Mapping[str, str] = field(default_factory=dict)
    path_volumes: Mapping[str, str] = field(default_factory=dict)
    entered: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class MountEntry:
    """One mount row of the archived form."""

    index: int
    kind: str
    source: str
    target: str
    read_only: bool


@dataclass(frozen=True, slots=True)
class NetworkPlan:
    """A user-defined network the container joins: created from the archive, or already there.

    `differences` names what an existing network of that name has otherwise than the archive's;
    `problem` is set when the archive's options cannot be created here. The flags, `pools`
    (subnet, gateway, range), `options` and `labels` are the archive's own: what a created
    network gets.
    """

    name: str
    driver: str
    action: NetworkAction
    differences: tuple[str, ...] = ()
    problem: str | None = None
    flags: tuple[str, ...] = ()
    pools: tuple[tuple[str, str, str], ...] = ()
    options: tuple[tuple[str, str], ...] = ()
    labels: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class VolumePlan:
    """An archived volume, the name it is created under and what is written into it."""

    source: str
    name: str
    prefix: str
    entries: int
    size: int
    targets: tuple[str, ...]
    taken: bool
    suggestion: str | None
    problem: str | None = None


@dataclass(frozen=True, slots=True)
class SkippedVolume:
    """An archived volume the import does not write, and why."""

    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class HostPathPlan:
    """A host path the container binds, where, and what is to become of it."""

    source: str
    targets: tuple[str, ...]
    choice: str | None
    volume: str
    volume_taken: bool
    suggestion: str | None
    problem: str | None = None


@dataclass(frozen=True, slots=True)
class EnvironmentPlan:
    """An environment variable of the archive and whether its value is known now."""

    index: int
    name: str
    secret: bool
    state: SecretState
    sealed_name: str | None
    archive_sealed: bool


@dataclass(frozen=True, slots=True)
class ContainerImportPlan:
    """Everything the import creates and asks for, with the digest a confirmation carries."""

    source_name: str
    image: str
    image_status: ImageStatus
    archive_digest: str | None
    local_digest: str | None
    name_taken: bool
    networks: tuple[NetworkPlan, ...]
    volumes: tuple[VolumePlan, ...]
    skipped_volumes: tuple[SkippedVolume, ...]
    host_paths: tuple[HostPathPlan, ...]
    environment: tuple[EnvironmentPlan, ...]
    not_exported: tuple[tuple[str, tuple[str, ...]], ...]
    helper_image: str | None
    digest: str = ""

    @property
    def networks_to_create(self) -> tuple[NetworkPlan, ...]:
        """Return the networks the import creates."""
        return tuple(item for item in self.networks if item.action == "create")

    @property
    def secrets_needed(self) -> tuple[EnvironmentPlan, ...]:
        """Return the secret variables whose value is neither opened nor entered."""
        return tuple(item for item in self.environment if item.state == "needed")

    @property
    def paths_undecided(self) -> tuple[HostPathPlan, ...]:
        """Return the host paths with no choice yet."""
        return tuple(item for item in self.host_paths if item.choice is None)

    @property
    def has_data(self) -> bool:
        """Report whether any volume is created and written."""
        return any(item.entries > 0 for item in self.volumes)

    @property
    def data_bytes(self) -> int:
        """Return the file data the import writes."""
        return sum(item.size for item in self.volumes)

    @property
    def problems(self) -> tuple[str, ...]:
        """Return what stops the import, each as a sentence."""
        found: list[str] = []
        found.extend(
            f"Network {item.name}: {item.problem}" for item in self.networks if item.problem
        )
        for volume in self.volumes:
            if volume.problem:
                found.append(f"Volume {volume.name}: {volume.problem}")
            elif volume.taken:
                found.append(f"Volume {volume.name}: a volume with this name already exists.")
        for path in self.host_paths:
            if path.problem:
                found.append(f"Host path {path.source}: {path.problem}")
            elif path.choice == VOLUME and path.volume_taken:
                found.append(f"Volume {path.volume}: a volume with this name already exists.")
        return tuple(found)

    @property
    def can_import(self) -> bool:
        """Report whether nothing in the plan stops the import."""
        return not self.problems and (self.helper_image is not None or not self.has_data)

    def with_digest(self, archive_digest: str) -> "ContainerImportPlan":
        """Return the plan carrying the digest of what it shows."""
        return replace(self, digest=plan_digest(self, archive_digest))


def mount_entries(form: Mapping[str, str]) -> tuple[MountEntry, ...]:
    """Return the mount rows of a form in row order."""
    rows: dict[int, dict[str, str]] = {}
    for key, value in form.items():
        match = _MOUNT_FIELD.fullmatch(key)
        if match is not None:
            rows.setdefault(int(match[1]), {})[match[2]] = value
    return tuple(
        MountEntry(
            index,
            row.get("type", ""),
            row.get("source", ""),
            row.get("target", ""),
            bool(row.get("read_only")),
        )
        for index, row in sorted(rows.items())
    )


def suggest_name(name: str, taken: Sequence[str] | set[str]) -> str:
    """Return `name` with the smallest numeric suffix, from 2, that is not in `taken`."""
    base = name[: _NAME_LIMIT - 4]
    for number in range(2, _SUFFIX_LIMIT):
        candidate = f"{base}-{number}"
        if candidate not in taken:
            return candidate
    return f"{base}-{len(taken) + 1}"


def plan_digest(plan: ContainerImportPlan, archive_digest: str) -> str:
    """Return a keyed digest of what the plan shows, to detect a change since the review.

    The key lives in this process only. Path decisions and secret values are not in it.
    """
    document = {
        "archive": archive_digest,
        "name": plan.source_name,
        "image": [plan.image, plan.image_status, plan.local_digest],
        "name_taken": plan.name_taken,
        "networks": [
            [
                item.name,
                item.driver,
                item.action,
                list(item.differences),
                item.problem,
                list(item.flags),
                [list(pool) for pool in item.pools],
                [list(pair) for pair in item.options],
                [list(pair) for pair in item.labels],
            ]
            for item in plan.networks
        ],
        "volumes": [
            [item.source, item.name, item.prefix, item.entries, item.taken, item.problem]
            for item in plan.volumes
        ],
        "skipped": [[item.name, item.reason] for item in plan.skipped_volumes],
        "paths": [[item.source, list(item.targets)] for item in plan.host_paths],
        "environment": [[item.index, item.name, item.secret] for item in plan.environment],
        "helper": plan.helper_image,
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(_DIGEST_KEY, encoded, hashlib.sha256).hexdigest()
