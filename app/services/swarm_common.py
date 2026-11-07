"""Reach checks, object resolution and image pinning shared by Swarm services and stacks."""

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError
from app.docker_engine.models import Network
from app.logging import get_logger
from app.registry_client.references import ImageReference, ReferenceInvalid, split_reference
from app.services.access import ResourceKey, is_canonical_id
from app.services.acting import Acting, AutomationAuthority
from app.services.container_restrictions import Finding, reach_finding
from app.services.image_bindings import parse_pull_reference
from app.services.object_inspector import NETWORK_ID, EngineObjectInspector
from app.services.pull_credentials import (
    CredentialChoice,
    PullCredentialResolver,
    PullCredentials,
)
from app.services.service_restrictions import added_references, volume_placement_findings
from app.services.swarm import swarm_read

__all__ = [
    "CONFIG_UNAVAILABLE",
    "NETWORK_UNAVAILABLE",
    "SECRET_UNAVAILABLE",
    "VOLUME_UNAVAILABLE",
    "AccessLevel",
    "PinnedImage",
    "ServiceImage",
    "ServiceImageKind",
    "VolumeAccess",
    "entries_of",
    "manager_node_id",
    "mapping_of",
    "pin_image",
    "pinned_text",
    "resolve_file",
    "resolve_network",
    "split_service_image",
    "volume_reach",
]

logger = get_logger(__name__)

NETWORK_UNAVAILABLE: Final = "This network is not available."
CONFIG_UNAVAILABLE: Final = "This config is not available."
SECRET_UNAVAILABLE: Final = "This secret is not available."
VOLUME_UNAVAILABLE: Final = "This volume is not available."

_IMAGE_ID: Final = re.compile(r"(?:sha256:)?[0-9a-f]{12,64}")
_SHA256_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")

type ServiceImageKind = Literal["tagged_digest", "digest_only", "unpinned", "image_id"]
type AccessLevel = Literal["read", "write"]
type VolumeAccess = Callable[[ResourceKey, AccessLevel], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PinnedImage:
    """The image a spec will run and the credentials and digest it was settled with."""

    image: str
    credentials: PullCredentials
    reference: ImageReference
    digest: str | None
    pinned: bool


@dataclass(frozen=True, slots=True)
class ServiceImage:
    """A service spec's image text split into the reference as written and its digest.

    `reference` is the text before `@`, never re-formatted; None for an image id and for text
    that is not a valid reference, which is `unpinned`. `digest` is set for the two pinned kinds.
    """

    text: str
    kind: ServiceImageKind
    reference: str | None
    digest: str | None


def split_service_image(text: str) -> ServiceImage:
    """Classify a service spec's image text; never raises."""
    candidate = text.strip()
    if _IMAGE_ID.fullmatch(candidate):
        return ServiceImage(text, "image_id", None, None)
    try:
        parts = split_reference(candidate)
    except ReferenceInvalid:
        return ServiceImage(text, "unpinned", None, None)
    reference = candidate.partition("@")[0]
    if parts.digest is None:
        return ServiceImage(text, "unpinned", reference, None)
    kind: ServiceImageKind = "tagged_digest" if parts.tag is not None else "digest_only"
    return ServiceImage(text, kind, reference, parts.digest)


def pinned_text(image: ServiceImage, digest: str) -> str:
    """Return the image text with the digest put after the reference as written."""
    if image.reference is None:
        raise ValueError("an image id or an invalid reference cannot be pinned")
    if _SHA256_DIGEST.fullmatch(digest) is None:
        raise ValueError("digest must be sha256: followed by 64 lower-case hexadecimal digits")
    return f"{image.reference}@{digest}"


def mapping_of(value: Any) -> Mapping[str, Any]:
    """Return the value if it is a mapping, else an empty one."""
    return value if isinstance(value, Mapping) else {}


def entries_of(value: Any) -> list[Mapping[str, Any]]:
    """Return the mapping items of a list, or an empty list for any other value."""
    return (
        [entry for entry in value if isinstance(entry, Mapping)] if isinstance(value, list) else []
    )


async def manager_node_id(
    client: EngineClient, environment_id: int, poke: Callable[[int], None]
) -> str | None:
    """Return the id of the node answering, or None when the Engine reports none."""
    info = await swarm_read(client.system.info(), environment_id, poke)
    return info.swarm.node_id or None


async def resolve_network(
    inspector: EngineObjectInspector, environment_id: int, key: str
) -> Network:
    """Resolve a network by id or exact name; ResourceNotFound when none has that id or name."""
    if NETWORK_ID.fullmatch(key):
        try:
            return await inspector.network(environment_id, key)
        except ResourceNotFound:
            pass
    return await inspector.network_named(environment_id, key)


async def resolve_file(
    inspector: EngineObjectInspector,
    environment_id: int,
    kind: Literal["config", "secret"],
    key: str,
) -> tuple[str, str]:
    """Resolve a config or secret by id or exact name to (id, name).

    ResourceNotFound when none has that id or name; EnvironmentUnavailable and
    ObjectTooLarge when the host cannot answer.
    """
    if is_canonical_id(kind, key):
        try:
            if kind == "config":
                document = await inspector.config(environment_id, key)
                return document.config.id, document.config.name
            secret = await inspector.secret(environment_id, key)
            return secret.id, secret.name
        except ResourceNotFound:
            pass
    if kind == "config":
        named = (await inspector.config_named(environment_id, key)).config
        return named.id, named.name
    found = await inspector.secret_named(environment_id, key)
    return found.id, found.name


async def volume_reach(
    access: VolumeAccess,
    inspector: EngineObjectInspector,
    environment_id: int,
    before: Mapping[str, Any] | None,
    spec: Mapping[str, Any],
    manager_node_id: str | None,
    slots: Mapping[str, Sequence[int]],
) -> tuple[list[Finding], dict[str, str]]:
    """Apply the named-volume rule: the manager pin, then the container reach rule.

    `access` raises ResourceNotFound or PermissionDenied when the caller lacks the level.
    """
    findings = volume_placement_findings(before, spec, manager_node_id, slots=slots)
    errors: dict[str, str] = {}
    added = set(added_references(before, spec).volumes)
    mounts = entries_of(
        mapping_of(mapping_of(spec.get("TaskTemplate")).get("ContainerSpec")).get("Mounts")
    )
    mount_slots = slots.get("mounts", ())
    for ordinal, mount in enumerate(mounts):
        source = mount.get("Source")
        if mount.get("Type") != "volume" or not isinstance(source, str) or not source:
            continue
        row = mount_slots[ordinal] if ordinal < len(mount_slots) else ordinal
        field_name = f"mount-{row}-source"
        path = f"TaskTemplate.ContainerSpec.Mounts[{ordinal}].Source"
        read_only = mount.get("ReadOnly") is True
        try:
            volume = await inspector.volume(environment_id, source)
        except ResourceNotFound:
            continue
        if (source, read_only, str(mount.get("Target") or "")) in added:
            try:
                await access(
                    ResourceKey(environment_id, "volume", source),
                    "read" if read_only else "write",
                )
            except (ResourceNotFound, PermissionDenied):
                errors[field_name] = VOLUME_UNAVAILABLE
                continue
        if volume.driver != "local" or volume.options:
            findings.append(reach_finding("host_backed_volume", path, field_name))
    return findings, errors


async def pin_image(
    resolver: PullCredentialResolver,
    client: EngineClient,
    actor: Acting,
    environment_id: int,
    image: str,
    choice: CredentialChoice | None,
    *,
    authorise_set: Callable[[int], Awaitable[None]] | None = None,
) -> PinnedImage | None:
    """Choose the credentials for an image and pin it to a digest; outside any transaction.

    None for an image id, which is never pulled. A failed digest lookup leaves the image
    unpinned rather than failing. RebindRefused and the credential errors of the resolver
    propagate.

    An automation run resolves with no principal and no choice, so only the binding can name
    the set; the set is then checked by `authorise_set`, which defaults to the run's own check.
    """
    if image.startswith("sha256:") and ":" not in image[7:]:
        return None
    reference = parse_pull_reference(image)
    check: Callable[[int], Awaitable[None]] | None
    if isinstance(actor, AutomationAuthority):
        if choice is not None:
            raise ValueError("an automation run cannot choose a credential set")
        credentials = await resolver.resolve(environment_id, image, principal=None, choice=None)
        check = authorise_set or actor.authorise_credential_set
    else:
        credentials = await resolver.resolve(environment_id, image, principal=actor, choice=choice)
        check = authorise_set
    if check is not None and credentials.credential_set_id is not None:
        await check(credentials.credential_set_id)
    if reference.digest is not None:
        return PinnedImage(image, credentials, reference, reference.digest, True)
    try:
        typed = split_reference(image).typed()
        descriptor = await client.distribution.inspect(
            typed, registry_auth=credentials.registry_auth
        )
    except (EngineError, ReferenceInvalid) as error:
        logger.warning(
            "service_image_unpinned",
            extra={"environment_id": environment_id, "error": type(error).__name__},
        )
        return PinnedImage(image, credentials, reference, None, False)
    return PinnedImage(
        f"{typed}@{descriptor.digest}", credentials, reference, descriptor.digest, True
    )
