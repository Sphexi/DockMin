"""Challenge choice, token realm validation and token replies shared by registry requests."""

import json
import re
import socket
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, urlsplit

from app import netpolicy
from app.registry_client import wire
from app.registry_client.addresses import MAX_CHALLENGES, Challenge, parse_challenges, split_address
from app.registry_client.auth_config import MAX_ISSUED_TOKEN_CHARS
from app.registry_client.errors import AddressInvalid, ChallengeInvalid

MAX_SERVICE_CHARS: Final = 256
TOKEN_SERVICE_REFUSED: Final = f"The token service is not used. {netpolicy.REFUSAL_TEXT}"

_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9._~+/-]+=*$")


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A host and port with the address the connection is pinned to."""

    host: str
    port: int
    family: socket.AddressFamily
    sockaddr: tuple[Any, ...]

    @property
    def host_header(self) -> str:
        """The Host header value: the port is left off when it is 443."""
        bracketed = f"[{self.host}]" if ":" in self.host else self.host
        return bracketed if self.port == 443 else f"{bracketed}:{self.port}"


@dataclass(frozen=True, slots=True)
class Realm:
    """Where a token service is: its host, port and the request target to ask it."""

    host: str
    port: int
    target: str


def pull_scope(repository_path: str) -> str:
    """Return the token scope for pulling a repository; the path must already be validated."""
    return f"repository:{repository_path}:pull"


def challenges_of(reply: wire.WireResponse) -> tuple[Challenge, ...]:
    """Parse every WWW-Authenticate header of a reply, or raise ChallengeInvalid."""
    values = reply.header_values("www-authenticate")
    if len(values) > MAX_CHALLENGES:
        raise ChallengeInvalid("The authentication challenge is too large.")
    return tuple(challenge for value in values for challenge in parse_challenges(value))


def choose_challenge(reply: wire.WireResponse) -> Challenge | str:
    """Return the Bearer challenge, else the Basic one, else a product-written refusal."""
    try:
        challenges = challenges_of(reply)
    except ChallengeInvalid:
        return "The registry's authentication challenge is malformed or too large."
    for scheme in ("bearer", "basic"):
        for challenge in challenges:
            if challenge.scheme == scheme:
                return challenge
    if not challenges:
        return "The registry asked for credentials without naming an authentication method."
    return "The registry asks for an authentication method this product does not support."


def realm_of(challenge: Challenge, *, scope: str | None = None) -> Realm | str:
    """Validate a Bearer challenge's realm and build the token request target.

    Only an https realm is accepted, and not one that names an internal IP literal the address
    policy refuses. The target carries the challenge's `service` and, when given, `scope`; no
    other challenge parameter is echoed into it. Returns a product-written refusal otherwise.
    """
    text = challenge.param("realm")
    if not text:
        return "The registry's challenge does not name a token service."
    unusable = "The token service address in the registry's challenge is not usable."
    try:
        parts = urlsplit(text)
    except ValueError:
        return unusable
    scheme = parts.scheme.lower()
    if scheme == "http":
        return (
            "The registry's token service uses plain HTTP, so the credentials were not sent to it."
        )
    if scheme != "https" or parts.fragment or not text.isascii():
        return unusable
    try:
        host, port = split_address(parts.netloc)
    except AddressInvalid:
        return unusable
    if netpolicy.literal_host_refusal(host) is not None:
        return TOKEN_SERVICE_REFUSED
    path = parts.path or "/"
    service = challenge.param("service")
    query = parts.query
    if service is not None:
        if len(service) > MAX_SERVICE_CHARS:
            return unusable
        query = f"{query}&" if query else ""
        query += f"service={quote(service, safe='')}"
    if scope is not None:
        query = f"{query}&" if query else ""
        query += f"scope={quote(scope, safe='')}"
    target = path + (f"?{query}" if query else "")
    if not re.match(r"^/[\x21-\x7e]*$", target):
        return unusable
    return Realm(host, port, target)


def token_from(body: bytes) -> str | None:
    """Return the bearer token a token service replied with, or None if it is absent or unsafe."""
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    for name in ("token", "access_token"):
        value = document.get(name)
        if isinstance(value, str) and value:
            if len(value) <= MAX_ISSUED_TOKEN_CHARS and _TOKEN_CHARS.match(value):
                return value
            return None
    return None


__all__: Final = [
    "MAX_SERVICE_CHARS",
    "TOKEN_SERVICE_REFUSED",
    "Endpoint",
    "Realm",
    "challenges_of",
    "choose_challenge",
    "pull_scope",
    "realm_of",
    "token_from",
]
