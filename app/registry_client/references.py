"""Image reference grammar and the canonical form that keys an image binding."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from app.registry_client.addresses import DOCKER_HUB_ADDRESS, canonical_address
from app.registry_client.errors import AddressInvalid, RegistryClientError

MAX_REFERENCE_LENGTH: Final = 512
MAX_NAME_LENGTH: Final = 255
MAX_REPO_DIGEST_LENGTH: Final = 640
DEFAULT_TAG: Final = "latest"
LIBRARY_NAMESPACE: Final = "library"

_HUB_ALIASES: Final = frozenset({DOCKER_HUB_ADDRESS, "index.docker.io", "registry-1.docker.io"})

_DOMAIN_COMPONENT = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?"
_DOMAIN: Final = re.compile(
    rf"(?:{_DOMAIN_COMPONENT}(?:\.{_DOMAIN_COMPONENT})*|\[[0-9a-fA-F:]+\])(?::[0-9]+)?"
)
_PATH_COMPONENT: Final = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*")
_TAG: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_DIGEST: Final = re.compile(r"sha256:[a-f0-9]{64}")

_MESSAGES: Final = {
    "required": "Image is required.",
    "invalid": "Image is not a valid image reference.",
    "digest": "Image digest must be sha256: followed by 64 hexadecimal digits.",
    "tag": "Image tag may use letters, digits, underscores, periods and hyphens.",
    "length": "Image name must be between 1 and 255 characters.",
    "host": "Image registry host is not valid.",
    "path": "Image name must use lower-case letters, digits, periods and hyphens.",
}


class ReferenceInvalid(RegistryClientError, ValueError):
    """An image reference that is malformed; the message is product-written and quotes no input."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _refuse(kind: str) -> ReferenceInvalid:
    return ReferenceInvalid(_MESSAGES[kind])


@dataclass(frozen=True, slots=True)
class ReferenceParts:
    """A reference split by the grammar and otherwise as typed: no defaults, no aliases."""

    domain: str | None
    path: str
    tag: str | None
    digest: str | None

    def typed(self) -> str:
        """Return the reference as typed, with the latest tag added when it has no tag or digest."""
        name = f"{self.domain}/{self.path}" if self.domain is not None else self.path
        tag = self.tag
        if tag is None and self.digest is None:
            tag = DEFAULT_TAG
        resolved = f"{name}:{tag}" if tag is not None else name
        return f"{resolved}@{self.digest}" if self.digest is not None else resolved


@dataclass(frozen=True, slots=True)
class ImageReference:
    """A reference in canonical form.

    `domain` and `registry_address` are the registry's canonical address, `docker.io` for every
    Docker Hub name. A pinned reference has a digest, and its key and familiar form leave the tag
    out because the daemon pulls by digest and ignores it.
    """

    domain: str
    path: str
    tag: str | None
    digest: str | None
    key: str
    familiar: str
    repository: str
    registry_address: str
    pinned: bool


def split_reference(text: str) -> ReferenceParts:
    """Split and validate a reference without normalising it, or raise ReferenceInvalid.

    The registry host must be one a connection could be made to, so the grammar and the stored
    address rules agree.
    """
    reference = text.strip()
    if not reference:
        raise _refuse("required")
    if (
        len(reference) > MAX_REFERENCE_LENGTH
        or not reference.isascii()
        or any(ord(c) < 33 or ord(c) == 127 for c in reference)
    ):
        raise _refuse("invalid")
    name_tag, at, digest = reference.partition("@")
    if at and _DIGEST.fullmatch(digest) is None:
        raise _refuse("digest")
    name, tag = name_tag, ""
    tagged = ":" in name_tag.rpartition("/")[2]
    if tagged:
        name, _, tag = name_tag.rpartition(":")
    if tagged and _TAG.fullmatch(tag) is None:
        raise _refuse("tag")
    if not name or len(name) > MAX_NAME_LENGTH:
        raise _refuse("length")
    first, slash, rest = name.partition("/")
    has_domain = bool(slash) and ("." in first or ":" in first or first == "localhost")
    components = rest.split("/") if has_domain else name.split("/")
    if has_domain:
        if _DOMAIN.fullmatch(first) is None:
            raise _refuse("host")
        _canonical_domain(first)
    if not all(_PATH_COMPONENT.fullmatch(part) for part in components):
        raise _refuse("path")
    return ReferenceParts(
        domain=first if has_domain else None,
        path=rest if has_domain else name,
        tag=tag if tagged else None,
        digest=digest if at else None,
    )


def parse_reference(text: str) -> ImageReference:
    """Return the canonical form of an image reference, or raise ReferenceInvalid.

    `busybox` becomes `docker.io/library/busybox:latest`; `ghcr.io/org/app:1` is its own key.
    """
    parts = split_reference(text)
    domain = _canonical_domain(parts.domain)
    path = parts.path
    if domain == DOCKER_HUB_ADDRESS and "/" not in path:
        path = f"{LIBRARY_NAMESPACE}/{path}"
    tag = parts.tag
    if tag is None and parts.digest is None:
        tag = DEFAULT_TAG
    repository = f"{domain}/{path}"
    key = f"{repository}@{parts.digest}" if parts.digest is not None else f"{repository}:{tag}"
    familiar = _familiar_name(domain, path)
    familiar += f"@{parts.digest}" if parts.digest is not None else f":{tag}"
    return ImageReference(
        domain=domain,
        path=path,
        tag=tag,
        digest=parts.digest,
        key=key,
        familiar=familiar,
        repository=repository,
        registry_address=domain,
        pinned=parts.digest is not None,
    )


def digests_for(repo_digests: Sequence[str], repository: str) -> tuple[str, ...]:
    """Return the digests, in order and without repeats, an image holds for one repository.

    Entries are the Engine's `RepoDigests`; those that are malformed or name another repository
    are skipped.
    """
    found: list[str] = []
    for entry in repo_digests:
        if len(entry) > MAX_REPO_DIGEST_LENGTH:
            continue
        try:
            reference = parse_reference(entry)
        except ReferenceInvalid:
            continue
        if (
            reference.digest is not None
            and reference.repository == repository
            and reference.digest not in found
        ):
            found.append(reference.digest)
    return tuple(found)


def _canonical_domain(domain: str | None) -> str:
    if domain is None:
        return DOCKER_HUB_ADDRESS
    try:
        address = canonical_address(domain)
    except AddressInvalid:
        raise _refuse("host") from None
    return DOCKER_HUB_ADDRESS if address in _HUB_ALIASES else address


def _familiar_name(domain: str, path: str) -> str:
    if domain != DOCKER_HUB_ADDRESS:
        return f"{domain}/{path}"
    head, _, tail = path.partition("/")
    return tail if head == LIBRARY_NAMESPACE and "/" not in tail else path


__all__: Final = [
    "DEFAULT_TAG",
    "MAX_REFERENCE_LENGTH",
    "ImageReference",
    "ReferenceInvalid",
    "ReferenceParts",
    "digests_for",
    "parse_reference",
    "split_reference",
]
