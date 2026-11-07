"""Amazon ECR authorisation-token exchange over a SigV4-signed GetAuthorizationToken call."""

import asyncio
import json
import math
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Final, Protocol

import httpx
from pydantic import SecretStr

from app import netpolicy
from app.logging import get_logger
from app.net_transport import policy_transport
from app.registry_client import sigv4
from app.registry_client.auth_config import MAX_ISSUED_TOKEN_CHARS, split_basic_token
from app.registry_client.errors import TokenExchangeFailed, TokenExchangeKind
from app.registry_client.providers import REGION_PATTERN, EcrExtras, ProviderExtras
from app.registry_client.tls import SystemTrustUnavailable, no_trust_context, system_trust_context

type Clock = Callable[[], datetime]

_log = get_logger(__name__)

__all__ = [
    "DerivationRequest",
    "DerivedSecret",
    "DerivedSecretSource",
    "EcrTokenSource",
    "TokenExchangeFailed",
    "ecr_api_host",
    "new_ecr_http_client",
]

MAX_RESPONSE_BYTES: Final = 64 * 1024
MAX_LIFETIME: Final = timedelta(hours=12)
REQUEST_TIMEOUT: Final = 10.0
SKEW_LIMIT: Final = timedelta(minutes=5)

_TARGET = "AmazonEC2ContainerRegistry_V20150921.GetAuthorizationToken"
_SERVICE = "ecr"
_ACCESS_KEY_ID = re.compile(r"^[A-Z0-9]{16,128}$")

_TIME_ERRORS = frozenset({"RequestExpired", "RequestTimeTooSkewed"})
_SIGNATURE_ERRORS = frozenset(
    {"InvalidSignatureException", "SignatureDoesNotMatch", "IncompleteSignature"}
)
_KEY_ERRORS = frozenset(
    {
        "UnrecognizedClientException",
        "InvalidClientTokenId",
        "InvalidAccessKeyId",
        "MissingAuthenticationToken",
        "ExpiredTokenException",
    }
)
_DENIED_ERRORS = frozenset({"AccessDeniedException", "AccessDenied", "UnauthorizedOperation"})
_BUSY_ERRORS = frozenset(
    {"ThrottlingException", "TooManyRequestsException", "ServiceUnavailableException"}
)

_MESSAGES: Final = {
    TokenExchangeKind.UNREACHABLE: (
        "AWS could not be reached. Check this server's outbound access to the ECR endpoint "
        "for the region."
    ),
    TokenExchangeKind.ADDRESS_REFUSED: netpolicy.REFUSAL_TEXT,
    TokenExchangeKind.REFUSED_KEY: (
        "AWS rejected the access key. Check the access key ID and the secret access key."
    ),
    TokenExchangeKind.DENIED: (
        "The access key is valid but is not allowed to request an ECR authorisation token. "
        "Grant it ecr:GetAuthorizationToken."
    ),
    TokenExchangeKind.CLOCK_SKEW: (
        "AWS rejected the request because this server's clock differs from AWS's by more than "
        "five minutes. Correct the clock on this server."
    ),
    TokenExchangeKind.MALFORMED: "AWS answered, but not in the form a token exchange returns.",
    TokenExchangeKind.OVERSIZED: ("AWS answered with far more data than a token exchange returns."),
}


@dataclass(frozen=True, slots=True)
class DerivationRequest:
    """What a deriver needs; the user name and the secret never appear in a repr."""

    provider: str
    address: str
    username: str = field(repr=False)
    secret: SecretStr = field(repr=False)
    extras: ProviderExtras


@dataclass(frozen=True, slots=True)
class DerivedSecret:
    """A short-lived Basic token and when it stops working; the token never appears in a repr."""

    basic_token: SecretStr = field(repr=False)
    expires_at: datetime


class DerivedSecretSource(Protocol):
    """Turns a long-lived credential into a short-lived one."""

    async def derive(self, request: DerivationRequest) -> DerivedSecret:
        """Return a fresh secret, or raise TokenExchangeFailed."""
        ...


def ecr_api_host(region: str) -> str:
    """Return the ECR API host for a region, or raise ValueError if the region is malformed."""
    if len(region) > 32 or not REGION_PATTERN.match(region):
        raise ValueError("not an AWS region")
    suffix = ".cn" if region.startswith("cn-") else ""
    return f"api.ecr.{region}.amazonaws.com{suffix}"


def new_ecr_http_client() -> httpx.AsyncClient:
    """Build the HTTP client the token source needs: no environment, no redirects.

    Without operating system roots it trusts nothing, so every exchange fails; building it never
    raises.
    """
    try:
        context = system_trust_context()
    except SystemTrustUnavailable:
        _log.warning("registry_system_trust_missing")
        context = no_trust_context()
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=0)
    return httpx.AsyncClient(
        transport=policy_transport(verify=context, limits=limits),
        trust_env=False,
        follow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    )


class EcrTokenSource:
    """Exchanges an IAM access key for an ECR registry token."""

    def __init__(self, client: httpx.AsyncClient, clock: Clock) -> None:
        if client.trust_env or client.follow_redirects:
            raise ValueError("the HTTP client must ignore the environment and not follow redirects")
        self._client = client
        self._clock = clock

    async def derive(self, request: DerivationRequest) -> DerivedSecret:
        """Request an authorisation token for the set's region."""
        extras = request.extras
        if not isinstance(extras, EcrExtras):
            raise TypeError("an ECR exchange needs ECR extras")
        host = ecr_api_host(extras.region)
        if not _ACCESS_KEY_ID.match(request.username):
            raise _failure(TokenExchangeKind.REFUSED_KEY)

        now = self._clock().astimezone(UTC)
        headers = sigv4.sign_request(
            method="POST",
            host=host,
            path="/",
            headers={
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": _TARGET,
            },
            body=b"{}",
            access_key_id=request.username,
            secret_access_key=request.secret,
            region=extras.region,
            service=_SERVICE,
            moment=now,
        )
        headers["Accept-Encoding"] = "identity"
        reply = await self._post(f"https://{host}/", headers)
        if isinstance(reply, TokenExchangeKind):
            raise _failure(reply)
        if reply.status == 200:
            return _read_token(reply.body, now)
        raise _classify_error(reply, now)

    async def _post(self, url: str, headers: dict[str, str]) -> "_Reply | TokenExchangeKind":
        outcome: _Reply | TokenExchangeKind
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                outcome = await self._read(url, headers)
        except netpolicy.AddressRefused:
            raise _failure(TokenExchangeKind.ADDRESS_REFUSED) from None
        except (httpx.HTTPError, TimeoutError, OSError):
            outcome = TokenExchangeKind.UNREACHABLE
        return outcome

    async def _read(self, url: str, headers: dict[str, str]) -> "_Reply | TokenExchangeKind":
        async with self._client.stream("POST", url, headers=headers, content=b"{}") as response:
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                return TokenExchangeKind.MALFORMED
            declared = response.headers.get("content-length", "")
            if declared.isdecimal() and int(declared) > MAX_RESPONSE_BYTES:
                return TokenExchangeKind.OVERSIZED
            chunks: list[bytes] = []
            received = 0
            async for chunk in _raw_chunks(response):
                received += len(chunk)
                if received > MAX_RESPONSE_BYTES:
                    return TokenExchangeKind.OVERSIZED
                chunks.append(chunk)
            return _Reply(
                response.status_code,
                b"".join(chunks),
                response.headers.get("date"),
                response.headers.get("x-amzn-errortype"),
            )


async def _raw_chunks(response: httpx.Response) -> AsyncIterator[bytes]:
    if response.is_stream_consumed:
        yield response.content
        return
    async for chunk in response.aiter_raw():
        yield chunk


@dataclass(frozen=True, slots=True)
class _Reply:
    status: int
    body: bytes
    date: str | None
    error_header: str | None


def _failure(kind: TokenExchangeKind) -> TokenExchangeFailed:
    return TokenExchangeFailed(kind, _MESSAGES[kind])


def _read_token(body: bytes, now: datetime) -> DerivedSecret:
    token, expires = _parse_success(body, now)
    if token is None or expires is None:
        raise _failure(TokenExchangeKind.MALFORMED)
    return DerivedSecret(SecretStr(token), min(expires, now + MAX_LIFETIME))


def _parse_success(body: bytes, now: datetime) -> tuple[str | None, datetime | None]:
    try:
        document = json.loads(body)
        entry = document["authorizationData"][0]
        token = entry["authorizationToken"]
        stamp = entry["expiresAt"]
    except (ValueError, KeyError, IndexError, TypeError, RecursionError):
        return None, None
    if not isinstance(token, str) or not token or len(token) > MAX_ISSUED_TOKEN_CHARS:
        return None, None
    if isinstance(stamp, bool) or not isinstance(stamp, int | float) or not math.isfinite(stamp):
        return None, None
    try:
        split_basic_token(SecretStr(token))
        expires = datetime.fromtimestamp(stamp, UTC)
    except (ValueError, OverflowError, OSError):
        return None, None
    if expires <= now:
        return None, None
    return token, expires


def _classify_error(reply: _Reply, now: datetime) -> TokenExchangeFailed:
    error_type = _error_type(reply)
    skew = _clock_difference(reply.date, now)
    kind: TokenExchangeKind
    if error_type in _TIME_ERRORS or (
        error_type in _SIGNATURE_ERRORS and skew is not None and skew > SKEW_LIMIT
    ):
        kind = TokenExchangeKind.CLOCK_SKEW
    elif error_type in _DENIED_ERRORS:
        kind = TokenExchangeKind.DENIED
    elif error_type in _BUSY_ERRORS:
        kind = TokenExchangeKind.UNREACHABLE
    elif error_type in _KEY_ERRORS or error_type in _SIGNATURE_ERRORS or reply.status in (401, 403):
        kind = TokenExchangeKind.REFUSED_KEY
    elif reply.status >= 500 or reply.status == 429:
        kind = TokenExchangeKind.UNREACHABLE
    else:
        kind = TokenExchangeKind.MALFORMED
    return _failure(kind)


def _error_type(reply: _Reply) -> str:
    raw = reply.error_header
    if raw is None:
        try:
            document = json.loads(reply.body)
            candidate = document.get("__type") if isinstance(document, dict) else None
        except (ValueError, RecursionError):
            candidate = None
        raw = candidate if isinstance(candidate, str) else ""
    return raw.rsplit("#", 1)[-1].split(":", 1)[0]


def _clock_difference(date_header: str | None, now: datetime) -> timedelta | None:
    if not date_header:
        return None
    try:
        server = parsedate_to_datetime(date_header)
    except (TypeError, ValueError, IndexError):
        return None
    if server.tzinfo is None:
        return None
    return abs(server - now)
