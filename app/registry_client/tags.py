"""The tags a registry lists for a repository, read with paged GET requests."""

import asyncio
import re
from typing import Annotated, Final
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from app.logging import get_logger
from app.registry_client import wire
from app.registry_client.addresses import canonical_address
from app.registry_client.manifests import (
    AttemptFailed,
    ManifestCheckFailed,
    ManifestCheckKind,
    ManifestClient,
    RegistryAttempt,
    refuse_status,
)
from app.registry_client.probe import RegistryLogin, RegistryTarget
from app.registry_client.references import ImageReference
from app.registry_client.token_flow import Endpoint

PAGE_SIZE: Final = 200
MAX_PAGES: Final = 10
MAX_PAGE_BYTES: Final = 256 * 1024
MAX_LINK_HEADER_CHARS: Final = 4096
MAX_LINK_QUERY_CHARS: Final = 512

_log = get_logger(__name__)

# One RFC 8288 link-value: `<target>` then `;name=value` parameters, ended by a comma or the end.
_TOKEN_CHARS: Final = r"[A-Za-z0-9!#$%&'*+.^_`|~-]"
_PARAMETERS: Final = rf"""(?:\s*;\s*{_TOKEN_CHARS}+(?:\s*=\s*(?:"[^"\\]*"|{_TOKEN_CHARS}*))?)*"""
_LINK_VALUE = re.compile(rf"\s*<([^<>\s]{{1,1024}})>({_PARAMETERS})\s*(?:,|$)")
_PARAMETER = re.compile(
    rf"""\s*;\s*({_TOKEN_CHARS}+)(?:\s*=\s*(?:"([^"\\]*)"|({_TOKEN_CHARS}*)))?"""
)
_QUERY = re.compile(rf"[\x21-\x7e]{{1,{MAX_LINK_QUERY_CHARS}}}")

type Tag = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")]


class _TagPage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    tags: list[Tag] | None


_MESSAGES: Final = {ManifestCheckKind.NOT_FOUND: "The registry has no such repository."}


class TagListFailed(ManifestCheckFailed):
    """A tag list that could not be read; `message` is product-written and safe to show."""

    def __init__(self, kind: ManifestCheckKind) -> None:
        super().__init__(kind)
        override = _MESSAGES.get(kind)
        if override is not None:
            self.message = override
            self.args = (override,)


class TagClient:
    """Lists a repository's tags over the manifest client's connection, TLS and token flow."""

    def __init__(self, manifests: ManifestClient) -> None:
        self._manifests = manifests

    async def list_tags(
        self, target: RegistryTarget, reference: ImageReference, login: RegistryLogin | None
    ) -> tuple[str, ...]:
        """Return every tag of the reference's repository in the order the registry gave them.

        Pages of at most `PAGE_SIZE` tags are followed through `Link` headers that stay on the
        registry's own host, up to `MAX_PAGES`; a list that does not end within that is a
        failure, as is any page that is oversized or malformed. The reference's tag or digest
        is not used. Raises TagListFailed with a product-written message, RuntimeError once the
        manifest client is closed, and ValueError if the target is at another registry than the
        reference or credentials would go without certificate verification.
        """
        if canonical_address(target.address) != reference.registry_address:
            raise ValueError("the target is not the registry the reference names")
        if login is not None and not target.verify_tls:
            raise ValueError("credentials are never sent without certificate verification")
        attempt = self._manifests.new_attempt(target, reference, login)
        tags: tuple[str, ...] = ()
        failure: ManifestCheckKind | None = None
        try:
            async with asyncio.timeout(attempt.timeouts.total):
                tags = await _collect(attempt, reference)
        except AttemptFailed as exc:
            failure = exc.kind
        except TimeoutError:
            failure = ManifestCheckKind.TIMEOUT
        except Exception as error:
            _log.error("tag_list_crashed", extra={"error": type(error).__name__})
            failure = ManifestCheckKind.PROTOCOL
        if failure is not None:
            _log.debug("tag_list_failed", extra={"kind": failure.value})
            raise TagListFailed(failure)
        return tags


async def _collect(attempt: RegistryAttempt, reference: ImageReference) -> tuple[str, ...]:
    endpoint = await attempt.connect()
    path = f"/v2/{reference.path}/tags/list"
    request = f"{path}?n={PAGE_SIZE}"
    headers: dict[str, str] = {}
    requested: set[str] = set()
    tags: dict[str, None] = {}
    for _ in range(MAX_PAGES):
        requested.add(request)
        reply = await attempt.exchange(endpoint, "GET", request, headers, max_body=MAX_PAGE_BYTES)
        if reply.status == 401 and "Authorization" not in headers:
            headers["Authorization"] = await attempt.authorise(endpoint, reply)
            reply = await attempt.exchange(
                endpoint, "GET", request, headers, max_body=MAX_PAGE_BYTES
            )
        refuse_status(reply.status)
        tags.update(dict.fromkeys(_tags_of(reply.body)))
        link = _next_link(reply)
        if link is None:
            return tuple(tags)
        request = _request_for(link, endpoint, path)
        if request in requested:
            raise AttemptFailed(ManifestCheckKind.PROTOCOL)
    raise AttemptFailed(ManifestCheckKind.PROTOCOL)


def _tags_of(body: bytes) -> list[str]:
    try:
        page = _TagPage.model_validate_json(body)
    except ValidationError:
        raise AttemptFailed(ManifestCheckKind.PROTOCOL) from None
    return page.tags or []


def _next_link(reply: wire.WireResponse) -> str | None:
    """Return the `rel="next"` target, None when there is none, or raise on an unreadable header."""
    found: list[str] = []
    for value in reply.header_values("link"):
        if len(value) > MAX_LINK_HEADER_CHARS:
            raise AttemptFailed(ManifestCheckKind.PROTOCOL)
        position = 0
        while position < len(value):
            entry = _LINK_VALUE.match(value, position)
            if entry is None:
                raise AttemptFailed(ManifestCheckKind.PROTOCOL)
            if "next" in _relations(entry.group(2)):
                found.append(entry.group(1))
            position = entry.end()
    if len(found) > 1:
        raise AttemptFailed(ManifestCheckKind.PROTOCOL)
    return found[0] if found else None


def _relations(parameters: str) -> list[str]:
    for match in _PARAMETER.finditer(parameters):
        if match.group(1).lower() == "rel":
            quoted, bare = match.group(2), match.group(3)
            return (quoted if quoted is not None else bare or "").lower().split()
    return []


def _request_for(link: str, endpoint: Endpoint, path: str) -> str:
    """Return the request target for a next-page link, or raise if it leaves the tag list."""
    try:
        parts = urlsplit(link)
        port = parts.port
        username = parts.username
    except ValueError:
        raise AttemptFailed(ManifestCheckKind.PROTOCOL) from None
    if (parts.scheme or parts.netloc) and (
        parts.scheme.lower() != "https"
        or username is not None
        or (parts.hostname or "").lower() != endpoint.host.lower()
        or (port or 443) != endpoint.port
    ):
        raise AttemptFailed(ManifestCheckKind.PROTOCOL)
    if parts.path != path or parts.fragment or _QUERY.fullmatch(parts.query) is None:
        raise AttemptFailed(ManifestCheckKind.PROTOCOL)
    return f"{path}?{parts.query}"


__all__: Final = [
    "MAX_PAGES",
    "MAX_PAGE_BYTES",
    "PAGE_SIZE",
    "TagClient",
    "TagListFailed",
]
