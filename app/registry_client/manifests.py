"""The digest a registry holds for an image tag, read with a HEAD request."""

import asyncio
import hashlib
import re
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

from app import netpolicy
from app.logging import get_logger
from app.netcheck import (
    Address,
    BoundedResolver,
    Resolver,
    connect_first,
    literal_address,
    usable_addresses,
)
from app.netpolicy import ip_literal
from app.registry_client import wire
from app.registry_client.addresses import api_base_url, canonical_address
from app.registry_client.auth_config import basic_token
from app.registry_client.errors import RegistryClientError
from app.registry_client.probe import RegistryLogin, RegistryTarget
from app.registry_client.references import ImageReference
from app.registry_client.tls import SystemTrustUnavailable, registry_context
from app.registry_client.token_flow import (
    TOKEN_SERVICE_REFUSED,
    Endpoint,
    choose_challenge,
    pull_scope,
    realm_of,
    token_from,
)

MANIFEST_ACCEPT: Final = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
USER_AGENT: Final = "dockmin-image-update-check"
MAX_CACHED_CONTEXTS: Final = 16

_log = get_logger(__name__)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ManifestCheckKind(StrEnum):
    """Why a digest could not be read; the values are stable identifiers."""

    DNS = "dns"
    TCP = "tcp"
    TLS = "tls"
    CERTIFICATE = "certificate"
    AUTHENTICATION = "authentication"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    NO_DIGEST = "no_digest"
    PROTOCOL = "protocol"
    TIMEOUT = "timeout"
    ADDRESS_REFUSED = "address_refused"


_MESSAGES: Final = {
    ManifestCheckKind.DNS: "The registry's name could not be resolved from this server.",
    ManifestCheckKind.TCP: "This server could not connect to the registry.",
    ManifestCheckKind.TLS: "The TLS handshake with the registry failed.",
    ManifestCheckKind.CERTIFICATE: "The registry's certificate could not be verified.",
    ManifestCheckKind.AUTHENTICATION: (
        "The registry refused access, or asks for an authentication method this product "
        "does not support."
    ),
    ManifestCheckKind.NOT_FOUND: "The registry has no such tag.",
    ManifestCheckKind.RATE_LIMITED: "The registry is limiting how often it answers.",
    ManifestCheckKind.NO_DIGEST: "The registry did not report a digest for this tag.",
    ManifestCheckKind.PROTOCOL: "The registry's reply was not a valid registry response.",
    ManifestCheckKind.TIMEOUT: "The registry did not answer in time.",
    ManifestCheckKind.ADDRESS_REFUSED: (
        "The registry or its token service is on this server or a link-local network. "
        f"Set {netpolicy.PERMIT_VARIABLE} to allow it."
    ),
}


def check_message(kind: ManifestCheckKind) -> str:
    """Return the product-written text for a failed check."""
    return _MESSAGES[kind]


class ManifestCheckFailed(RegistryClientError):
    """A digest that could not be read; `message` is product-written and safe to show."""

    def __init__(self, kind: ManifestCheckKind) -> None:
        super().__init__(_MESSAGES[kind])
        self.kind = kind
        self.message = _MESSAGES[kind]


@dataclass(frozen=True, slots=True)
class ManifestTimeouts:
    """Seconds each phase may take, and the cap on one whole check."""

    dns: float = 5.0
    tcp: float = 5.0
    exchange: float = 10.0
    total: float = 30.0


class AttemptFailed(Exception):
    """A failure inside one attempt; the kind is what the caller reports."""

    def __init__(self, kind: ManifestCheckKind) -> None:
        super().__init__(kind.value)
        self.kind = kind


class ManifestClient:
    """Reads the digest of a tag from a registry the caller already trusts."""

    def __init__(
        self, *, resolver: Resolver | None = None, timeouts: ManifestTimeouts | None = None
    ) -> None:
        self._owned: BoundedResolver | None
        if resolver is None:
            self._owned = BoundedResolver()
            self._resolver: Resolver = self._owned
        else:
            self._owned = None
            self._resolver = resolver
        self._timeouts = timeouts or ManifestTimeouts()
        self._contexts: dict[tuple[bool, str | None], ssl.SSLContext] = {}
        self._closed = False

    async def head_digest(
        self, target: RegistryTarget, reference: ImageReference, login: RegistryLogin | None
    ) -> str:
        """Return the `sha256:` digest the registry reports for the reference's tag.

        Only HEAD is sent to the manifest endpoint, no redirect is followed, and every read is
        bounded. Connects to the target's address only, which must be the reference's
        registry. Raises ManifestCheckFailed with a product-written message when the digest
        cannot be read, ValueError if the arguments are inconsistent (a pinned reference, a
        target at another registry, or credentials without certificate verification).
        """
        if self._closed:
            raise RuntimeError("the manifest client is closed")
        _validate(target, reference, login)
        attempt = RegistryAttempt(
            self._timeouts, self._resolver, self._context_for, target, reference, login
        )
        digest: str | None = None
        failure: ManifestCheckKind | None = None
        try:
            async with asyncio.timeout(self._timeouts.total):
                digest = await attempt.run()
        except AttemptFailed as exc:
            failure = exc.kind
        except TimeoutError:
            failure = ManifestCheckKind.TIMEOUT
        except Exception as error:
            _log.error("manifest_check_crashed", extra={"error": type(error).__name__})
            failure = ManifestCheckKind.PROTOCOL
        if failure is not None:
            _log.debug("manifest_check_failed", extra={"kind": failure.value})
            raise ManifestCheckFailed(failure)
        assert digest is not None
        return digest

    def new_attempt(
        self, target: RegistryTarget, reference: ImageReference, login: RegistryLogin | None
    ) -> "RegistryAttempt":
        """Return an attempt on this client's resolver, timeouts and TLS context cache.

        Arguments are not validated. Raises RuntimeError once the client is closed.
        """
        if self._closed:
            raise RuntimeError("the manifest client is closed")
        return RegistryAttempt(
            self._timeouts, self._resolver, self._context_for, target, reference, login
        )

    async def aclose(self) -> None:
        """Stop the name-lookup threads; call once at shutdown."""
        self._closed = True
        self._contexts.clear()
        if self._owned is not None:
            self._owned.close()

    async def _context_for(self, target: RegistryTarget) -> ssl.SSLContext:
        bundle = target.ca_bundle if target.verify_tls else None
        key = (target.verify_tls, hashlib.sha256(bundle.encode()).hexdigest() if bundle else None)
        cached = self._contexts.get(key)
        if cached is not None:
            return cached
        try:
            context = await asyncio.to_thread(
                registry_context, verify_tls=target.verify_tls, ca_bundle=bundle
            )
        except (SystemTrustUnavailable, ssl.SSLError, ValueError):
            raise AttemptFailed(ManifestCheckKind.CERTIFICATE) from None
        if len(self._contexts) >= MAX_CACHED_CONTEXTS:
            del self._contexts[next(iter(self._contexts))]
        self._contexts[key] = context
        return context


def _validate(
    target: RegistryTarget, reference: ImageReference, login: RegistryLogin | None
) -> None:
    if reference.pinned or reference.tag is None:
        raise ValueError("a reference with a digest has no tag to look up")
    if canonical_address(target.address) != reference.registry_address:
        raise ValueError("the target is not the registry the reference names")
    if login is not None and not target.verify_tls:
        raise ValueError("credentials are never sent without certificate verification")


type ContextSource = Callable[[RegistryTarget], Awaitable[ssl.SSLContext]]


class RegistryAttempt:
    """One bounded conversation with a registry: connect, authenticate, exchange."""

    def __init__(
        self,
        timeouts: ManifestTimeouts,
        resolver: Resolver,
        context_source: ContextSource,
        target: RegistryTarget,
        reference: ImageReference,
        login: RegistryLogin | None,
    ) -> None:
        self._timeouts = timeouts
        self._resolver = resolver
        self._context_source = context_source
        self._target = target
        self._reference = reference
        self._login = login
        self._deadline = asyncio.get_running_loop().time() + timeouts.total
        self._context: ssl.SSLContext | None = None

    @property
    def timeouts(self) -> ManifestTimeouts:
        return self._timeouts

    async def connect(self) -> Endpoint:
        """Resolve and pin the registry's API address; the first call to make."""
        base = urlsplit(api_base_url(self._target.provider, self._target.address))
        host = base.hostname or ""
        port = base.port or 443
        self._context = await self._context_source(self._target)
        return await self._endpoint(host, port)

    async def run(self) -> str:
        endpoint = await self.connect()
        path = f"/v2/{self._reference.path}/manifests/{self._reference.tag}"
        headers = {"Accept": MANIFEST_ACCEPT}
        reply = await self.exchange(endpoint, "HEAD", path, headers)
        if reply.status == 401:
            authorization = await self.authorise(endpoint, reply)
            reply = await self.exchange(
                endpoint, "HEAD", path, {**headers, "Authorization": authorization}
            )
        return _digest_of(reply)

    async def authorise(self, endpoint: Endpoint, challenged: wire.WireResponse) -> str:
        """Return the Authorization value that answers a 401 reply, fetching a token if needed."""
        challenge = choose_challenge(challenged)
        if isinstance(challenge, str):
            raise AttemptFailed(ManifestCheckKind.AUTHENTICATION)
        if challenge.scheme == "basic":
            return self._basic()
        realm = realm_of(challenge, scope=pull_scope(self._reference.path))
        if isinstance(realm, str):
            raise AttemptFailed(
                ManifestCheckKind.ADDRESS_REFUSED
                if realm == TOKEN_SERVICE_REFUSED
                else ManifestCheckKind.AUTHENTICATION
            )
        if (realm.host, realm.port) == (endpoint.host, endpoint.port):
            service = endpoint
        else:
            service = await self._endpoint(realm.host, realm.port)
        headers = {"Authorization": self._basic()} if self._login is not None else {}
        reply = await self.exchange(
            service, "GET", realm.target, headers, max_body=wire.MAX_BODY_BYTES
        )
        if reply.status == 404:
            raise AttemptFailed(ManifestCheckKind.PROTOCOL)
        refuse_status(reply.status)
        token = token_from(reply.body)
        if token is None:
            raise AttemptFailed(ManifestCheckKind.PROTOCOL)
        return f"Bearer {token}"

    def _basic(self) -> str:
        if self._login is None:
            raise AttemptFailed(ManifestCheckKind.AUTHENTICATION)
        try:
            return (
                "Basic " + basic_token(self._login.username, self._login.secret).get_secret_value()
            )
        except ValueError:
            raise AttemptFailed(ManifestCheckKind.AUTHENTICATION) from None

    async def _endpoint(self, host: str, port: int) -> Endpoint:
        literal = ip_literal(host)
        if literal is not None:
            addresses = [literal_address(literal, port)]
        else:
            addresses = await self._resolve(host, port)
        result = await connect_first(
            addresses, attempt_limit=self._timeouts.tcp, budget=self._budget
        )
        if result.sock is None:
            if result.kinds == ("address_refused",):
                raise AttemptFailed(ManifestCheckKind.ADDRESS_REFUSED)
            raise AttemptFailed(ManifestCheckKind.TCP)
        family = result.sock.family
        result.sock.close()
        return Endpoint(host, port, family, result.sockaddr)

    async def _resolve(self, host: str, port: int) -> list[Address]:
        found: list[Address] = []
        try:
            async with asyncio.timeout(self._timeouts.dns):
                found = usable_addresses(await self._resolver(host, port))
        except Exception:
            found = []
        if not found:
            raise AttemptFailed(ManifestCheckKind.DNS)
        return found

    def _budget(self, limit: float) -> tuple[float, bool]:
        remaining = self._deadline - asyncio.get_running_loop().time()
        return max(0.0, min(limit, remaining)), remaining < limit

    async def exchange(
        self,
        endpoint: Endpoint,
        method: str,
        target: str,
        headers: dict[str, str],
        *,
        max_body: int | None = None,
    ) -> wire.WireResponse:
        """Send one request on a fresh connection; GET reads a body of at most max_body bytes."""
        assert self._context is not None
        failure: ManifestCheckKind | None = None
        writer: asyncio.StreamWriter | None = None
        reply: wire.WireResponse | None = None
        headers = {"User-Agent": USER_AGENT, **headers}
        try:
            async with asyncio.timeout(self._timeouts.exchange):
                reader, writer = await wire.open_connection(
                    endpoint.family,
                    endpoint.sockaddr,
                    server_name=endpoint.host,
                    context=self._context,
                )
                if method == "HEAD":
                    reply = await wire.head(
                        reader,
                        writer,
                        target=target,
                        host_header=endpoint.host_header,
                        headers=headers,
                    )
                else:
                    reply = await wire.get(
                        reader,
                        writer,
                        target=target,
                        host_header=endpoint.host_header,
                        headers=headers,
                        max_body=max_body,
                    )
        except TimeoutError:
            failure = ManifestCheckKind.TIMEOUT
        except netpolicy.AddressRefused:
            failure = ManifestCheckKind.ADDRESS_REFUSED
        except wire.WireError:
            failure = ManifestCheckKind.PROTOCOL
        except ssl.SSLCertVerificationError:
            failure = ManifestCheckKind.CERTIFICATE
        except ssl.SSLError:
            failure = ManifestCheckKind.TLS
        except (OSError, EOFError):
            failure = ManifestCheckKind.TCP
        finally:
            if writer is not None:
                writer.transport.abort()
        if failure is not None:
            raise AttemptFailed(failure)
        assert reply is not None
        return reply


def refuse_status(status: int) -> None:
    """Return for 200; otherwise raise the AttemptFailed that status maps to."""
    if status == 200:
        return
    if status in (401, 403):
        raise AttemptFailed(ManifestCheckKind.AUTHENTICATION)
    if status == 404:
        raise AttemptFailed(ManifestCheckKind.NOT_FOUND)
    if status == 429:
        raise AttemptFailed(ManifestCheckKind.RATE_LIMITED)
    raise AttemptFailed(ManifestCheckKind.PROTOCOL)


def _digest_of(reply: wire.WireResponse) -> str:
    refuse_status(reply.status)
    values = set(reply.header_values("docker-content-digest"))
    if not values:
        raise AttemptFailed(ManifestCheckKind.NO_DIGEST)
    (value,) = values if len(values) == 1 else (None,)
    if value is None or _DIGEST.match(value) is None:
        raise AttemptFailed(ManifestCheckKind.PROTOCOL)
    return value


__all__: Final = [
    "MANIFEST_ACCEPT",
    "AttemptFailed",
    "ManifestCheckFailed",
    "ManifestCheckKind",
    "ManifestClient",
    "ManifestTimeouts",
    "RegistryAttempt",
    "refuse_status",
]
