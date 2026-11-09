"""What a stack export reads from the host and from the stack file: images, host paths, volumes."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import yaml

from app.db.repositories.stack import StackKind
from app.docker_engine.client import EngineClient
from app.docker_engine.models import ContainerSummary, ImageSummary, SwarmService, Volume
from app.services.access import SWARM_NAMESPACE_LABEL, canonical_stack_key
from app.services.export_manifest import ImageReference
from app.services.host_objects import volume_users
from app.services.stack_common import declared_service_images, read_labelled_containers
from app.services.stack_deploy import in_project
from app.services.stack_queries import labelled_volumes, read_project_containers
from app.services.swarm_stacks import read_namespace, read_namespace_volumes

MAX_IMAGES: Final = 256
MAX_HOST_PATHS: Final = 200

_HUB_PREFIXES: Final = ("docker.io/", "index.docker.io/", "registry-1.docker.io/")
_LIBRARY: Final = "library/"
_HOST_PATH_STARTS: Final = ("/", "./", "../", "~")
_BARE_HOST_PATHS: Final = frozenset({".", "..", "~"})


@dataclass(slots=True)
class ProjectHost:
    """The host's objects that carry a stack's label, and the volumes its containers mount."""

    containers: list[ContainerSummary] = field(default_factory=list)
    services: list[SwarmService] = field(default_factory=list)
    labelled_volumes: dict[str, Volume] = field(default_factory=dict)
    mounted_volumes: frozenset[str] = frozenset()


async def read_project_host(
    client: EngineClient, kind: StackKind, environment_id: int, name: str
) -> ProjectHost:
    """Read the containers, services and labelled volumes of the stack from the host now.

    The list-too-large and unavailable errors are raised rather than a partial answer.
    """
    key = canonical_stack_key(name)
    host = ProjectHost()
    if kind == "compose":
        host.containers = [
            c for c in await read_project_containers(client) if in_project(c.labels, key)
        ]
        volumes = [v for v in await labelled_volumes(client) if in_project(v.labels, key)]
    else:
        members = await read_namespace(client, environment_id, None, key)
        host.services = list(members.services)
        host.containers = [
            c
            for c in await read_labelled_containers(client, SWARM_NAMESPACE_LABEL)
            if _in_namespace(c.labels, key)
        ]
        volumes = list(await read_namespace_volumes(client, key))
    host.labelled_volumes = {volume.name: volume for volume in volumes}
    host.mounted_volumes = frozenset(volume_users(host.containers))
    return host


def _in_namespace(labels: Mapping[str, str], key: str) -> bool:
    value = labels.get(SWARM_NAMESPACE_LABEL)
    return bool(value) and canonical_stack_key(value or "") == key


def declared_host_paths(text: str) -> tuple[str, ...]:
    """Return the host paths the file's services bind, as written, sorted and without repeats."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return ()
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return ()
    found: set[str] = set()
    for service in services.values():
        entries = service.get("volumes") if isinstance(service, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            source = bind_source(entry)
            if source is not None:
                found.add(source)
    return tuple(sorted(found))


def bind_source(entry: object) -> str | None:
    """Return the host path a service volume entry binds, as written, or None for any other."""
    source: object = None
    if isinstance(entry, str) and ":" in entry:
        source = entry.split(":", 1)[0]
    elif isinstance(entry, dict) and entry.get("type") == "bind":
        source = entry.get("source")
    if not isinstance(source, str) or not source:
        return None
    if source in _BARE_HOST_PATHS or source.startswith(_HOST_PATH_STARTS):
        return source
    return None


def image_references(text: str, local: Sequence[ImageSummary]) -> list[ImageReference]:
    """Return each image the file declares with its tag and the digest present on the host.

    A reference carrying `$` is interpolated at deploy time, so no tag or digest is read for it.
    """
    used_by: dict[str, list[str]] = {}
    for service, reference in declared_service_images(text).items():
        if reference:
            used_by.setdefault(reference, []).append(service)
    entries = [
        ImageReference(
            reference=reference,
            tag=_tag_of(reference),
            digest=_digest_of(reference, local),
            used_by=sorted(services),
        )
        for reference, services in sorted(used_by.items())
        if len(reference) <= 512
    ]
    return entries[:MAX_IMAGES]


def _split(reference: str) -> tuple[str, str | None, str | None]:
    if "$" in reference:
        return reference, None, None
    name, _, digest = reference.partition("@")
    leaf = name.rsplit("/", 1)[-1]
    if ":" in leaf:
        repository, _, tag = name.rpartition(":")
        return repository, tag, digest or None
    return name, None, digest or None


def _tag_of(reference: str) -> str | None:
    return _split(reference)[1]


def _familiar(repository: str) -> str:
    lowered = repository.lower()
    for prefix in _HUB_PREFIXES:
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix) :]
            break
    return lowered.removeprefix(_LIBRARY)


def _digest_of(reference: str, local: Iterable[ImageSummary]) -> str | None:
    repository, tag, pinned = _split(reference)
    if "$" in reference:
        return None
    if pinned:
        return pinned if pinned.startswith("sha256:") else None
    wanted_name = _familiar(repository)
    wanted = f"{wanted_name}:{tag or 'latest'}"
    for image in local:
        if wanted not in {_tagged(item) for item in image.repo_tags}:
            continue
        for digest_reference in image.repo_digests:
            held_repository, _, digest = digest_reference.partition("@")
            if _familiar(held_repository) == wanted_name and digest.startswith("sha256:"):
                return digest
    return None


def _tagged(reference: str) -> str:
    repository, tag, _ = _split(reference)
    return f"{_familiar(repository)}:{tag or 'latest'}"


__all__ = [
    "MAX_HOST_PATHS",
    "MAX_IMAGES",
    "ProjectHost",
    "bind_source",
    "declared_host_paths",
    "image_references",
    "read_project_host",
]
