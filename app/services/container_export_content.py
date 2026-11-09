"""What a container export carries, read out of the create form's state and the host's objects."""

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from app.docker_engine.models import ImageInspect, Network
from app.registry_client.references import ReferenceInvalid, digests_for, parse_reference
from app.services.container_spec import GROUP_NOUNS, GROUPS, ROW_PREFIX
from app.services.export_manifest import (
    ImageReference,
    IpamPoolEntry,
    NetworkEntry,
)
from app.services.volume_manifest import filter_labels, filter_options

PREDEFINED_NETWORKS: Final = frozenset({"bridge", "host", "none"})

_ENVIRONMENT_ROW: Final = re.compile(r"env-([0-9]{1,4})-(key|value)")
_MOUNT_ROW: Final = re.compile(r"mount-([0-9]{1,4})-(type|source|target|read_only)")
_NETWORK_ROW: Final = re.compile(r"network-([0-9]{1,4})-name")
_ROW: Final = re.compile(r"([a-z]+)-([0-9]{1,4})-[a-z_0-9]+")
_GROUP_OF_PREFIX: Final = {prefix: group for group, prefix in ROW_PREFIX.items()}
_PEPPER: Final = secrets.token_bytes(32)


@dataclass(frozen=True, slots=True)
class EnvironmentRow:
    """One environment variable of the form; the value is kept out of the repr."""

    name: str
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class MountRow:
    """One mount of the form: a `volume`, `bind` or `tmpfs` row."""

    kind: str
    source: str
    target: str
    read_only: bool


def split_form(values: Mapping[str, str]) -> tuple[dict[str, str], tuple[EnvironmentRow, ...]]:
    """Return the form's values without the environment rows, and those rows in order."""
    form = {key: value for key, value in values.items() if _ENVIRONMENT_ROW.fullmatch(key) is None}
    indexes = sorted(
        {int(match[1]) for key in values if (match := _ENVIRONMENT_ROW.fullmatch(key)) is not None}
    )
    rows = tuple(
        EnvironmentRow(values[f"env-{index}-key"], values.get(f"env-{index}-value", ""))
        for index in indexes
        if values.get(f"env-{index}-key")
    )
    return form, rows


def group_counts(values: Mapping[str, str]) -> tuple[tuple[str, int], ...]:
    """Count the rows of each repeatable group of the form, as (plural noun, rows)."""
    seen: dict[str, set[str]] = {group: set() for group in GROUPS}
    for key in values:
        match = _ROW.fullmatch(key)
        if match is not None and match[1] in _GROUP_OF_PREFIX:
            seen[_GROUP_OF_PREFIX[match[1]]].add(match[2])
    return tuple((GROUP_NOUNS[group], len(seen[group])) for group in GROUPS if seen[group])


def mount_rows(values: Mapping[str, str]) -> tuple[MountRow, ...]:
    """Return the form's mounts in row order."""
    indexes = sorted(
        {int(match[1]) for key in values if (match := _MOUNT_ROW.fullmatch(key)) is not None}
    )
    return tuple(
        MountRow(
            kind=values.get(f"mount-{index}-type", ""),
            source=values.get(f"mount-{index}-source", ""),
            target=values.get(f"mount-{index}-target", ""),
            read_only=bool(values.get(f"mount-{index}-read_only")),
        )
        for index in indexes
    )


def user_defined_network_names(values: Mapping[str, str]) -> tuple[str, ...]:
    """Return the networks the form attaches the container to, without Docker's built-in ones."""
    indexes = sorted(
        {int(match[1]) for key in values if (match := _NETWORK_ROW.fullmatch(key)) is not None}
    )
    names = (values[f"network-{index}-name"] for index in indexes)
    return tuple(dict.fromkeys(name for name in names if name and name not in PREDEFINED_NETWORKS))


def network_entry(network: Network) -> NetworkEntry:
    """Describe a network's create options, without credentials and access-granting labels."""
    options, removed = filter_options(network.options)
    return NetworkEntry(
        name=network.name,
        driver=network.driver,
        internal=network.internal,
        attachable=network.attachable,
        enable_ipv6=network.enable_ipv6,
        ipam_driver=network.ipam_driver,
        pools=[
            IpamPoolEntry(subnet=pool.subnet, gateway=pool.gateway, ip_range=pool.ip_range)
            for pool in network.ipam
        ],
        labels=filter_labels(network.labels),
        options=options,
        removed_option_keys=removed,
    )


def image_reference(reference: str, image: ImageInspect | None, used_by: str) -> ImageReference:
    """Name the image as written, its tag and the digest the host holds for that repository."""
    try:
        parsed = parse_reference(reference)
    except ReferenceInvalid:
        return ImageReference(reference=reference, used_by=[used_by])
    digest = parsed.digest
    if digest is None and image is not None:
        digest = next(iter(digests_for(image.repo_digests, parsed.repository)), None)
    return ImageReference(reference=reference, tag=parsed.tag, digest=digest, used_by=[used_by])


def review_digest(
    *,
    container_id: str,
    form: Mapping[str, str],
    environment: Sequence[EnvironmentRow],
    networks: Sequence[NetworkEntry],
    image: ImageReference,
    volumes: Sequence[tuple[str, str, str]],
) -> str:
    """Return a keyed digest of what an export would hold, to detect a change since the review.

    The key lives in this process only, so the digest reveals nothing about a variable's value.
    """
    document = {
        "id": container_id,
        "form": dict(form),
        "environment": [[row.name, row.value] for row in environment],
        "networks": [entry.model_dump(mode="json") for entry in networks],
        "image": image.model_dump(mode="json"),
        "volumes": [list(volume) for volume in volumes],
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(_PEPPER, encoded, hashlib.sha256).hexdigest()
