"""Verification of a signed token from an access platform, down to the identity it asserts."""

import base64
import binascii
import hashlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import jwt

from app.auth.jwt_key_documents import PERMITTED_ALGORITHMS
from app.auth.jwt_keys import KeyRefused, KeyStore

MAX_HEADER_VALUE_CHARS: Final = 8 * 1024
MAX_JOSE_HEADER_CHARS: Final = 1024
MAX_IDENTITY_CHARS: Final = 320
SKEW_SECONDS: Final = 30
CACHE_ENTRIES: Final = 1000
CACHE_SECONDS: Final = 300.0
BEARER_PREFIX: Final = "bearer "

_SEGMENT = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
_REQUIRED_CLAIMS: Final = ["exp", "iat", "iss", "aud"]


class JwtRefused(Exception):
    """A token that is not accepted; `reason` is the internal code and never carries the token."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class JwtConfig:
    """What a token must satisfy and where its identity is found."""

    issuer: str
    audience: str
    algorithms: tuple[str, ...]
    identity_claim: str
    username_claim: str
    max_age: float


@dataclass(frozen=True, slots=True)
class Assertion:
    """An identity a verified token asserts; `digest` names the token without holding it."""

    identity: str
    username: str | None
    digest: str


class JwtVerifier:
    """Checks tokens against a key store, remembering verified ones until they would expire."""

    def __init__(
        self,
        config: JwtConfig,
        keys: KeyStore,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._keys = keys
        self._clock = clock
        self._verified: OrderedDict[str, tuple[Assertion, float]] = OrderedDict()

    async def verify(self, header_value: str) -> Assertion:
        """Return the assertion of a header value, or raise JwtRefused."""
        token = _token_of(header_value)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = self._clock()
        cached = self._verified.get(digest)
        if cached is not None and now < cached[1]:
            self._verified.move_to_end(digest)
            return cached[0]
        claims = await self._check(token)
        assertion = Assertion(
            identity=_identity_of(claims, self._config.identity_claim, "identity"),
            username=_optional_identity(claims, self._config.username_claim),
            digest=digest,
        )
        until = min(float(claims["exp"]), now + CACHE_SECONDS, claims["iat"] + self._config.max_age)
        self._remember(digest, assertion, until)
        return assertion

    async def _check(self, token: str) -> dict[str, Any]:
        header = _jose_header(token)
        algorithm = header["alg"]
        if algorithm not in self._config.algorithms or algorithm not in PERMITTED_ALGORITHMS:
            raise JwtRefused("alg_not_allowed")
        kid = header.get("kid")
        if kid is not None and not isinstance(kid, str):
            raise JwtRefused("malformed")
        try:
            key = await self._keys.select(kid)
        except KeyRefused as refused:
            raise JwtRefused(refused.reason) from None
        if not key.fits(algorithm):
            raise JwtRefused("key_type_mismatch")
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],
                audience=self._config.audience,
                issuer=self._config.issuer,
                leeway=SKEW_SECONDS,
                options={"require": _REQUIRED_CLAIMS, "verify_iat": False},
            )
        except jwt.InvalidSignatureError:
            raise JwtRefused("bad_signature") from None
        except jwt.ExpiredSignatureError:
            raise JwtRefused("expired") from None
        except jwt.ImmatureSignatureError:
            raise JwtRefused("not_yet_valid") from None
        except jwt.InvalidIssuerError:
            raise JwtRefused("bad_issuer") from None
        except jwt.InvalidAudienceError:
            raise JwtRefused("bad_audience") from None
        except jwt.MissingRequiredClaimError:
            raise JwtRefused("missing_claim") from None
        except jwt.InvalidKeyError:
            raise JwtRefused("key_type_mismatch") from None
        except jwt.InvalidAlgorithmError:
            raise JwtRefused("alg_not_allowed") from None
        except jwt.PyJWTError:
            raise JwtRefused("malformed") from None
        self._check_issued_at(claims)
        return claims

    def _check_issued_at(self, claims: dict[str, Any]) -> None:
        issued = claims["iat"]
        if isinstance(issued, bool) or not isinstance(issued, int | float):
            raise JwtRefused("malformed")
        now = self._clock()
        if issued > now + SKEW_SECONDS:
            raise JwtRefused("iat_future")
        if now - issued > self._config.max_age:
            raise JwtRefused("too_old")

    def _remember(self, digest: str, assertion: Assertion, until: float) -> None:
        self._verified[digest] = (assertion, until)
        self._verified.move_to_end(digest)
        while len(self._verified) > CACHE_ENTRIES:
            self._verified.popitem(last=False)


def _token_of(header_value: str) -> str:
    value = header_value.strip()
    if len(value) > MAX_HEADER_VALUE_CHARS:
        raise JwtRefused("too_long")
    if value[: len(BEARER_PREFIX)].lower() == BEARER_PREFIX:
        value = value[len(BEARER_PREFIX) :].strip()
    segments = value.split(".")
    if len(segments) != 3 or any(_SEGMENT.fullmatch(part) is None for part in segments):
        raise JwtRefused("malformed")
    return value


def _jose_header(token: str) -> dict[str, Any]:
    segment = token.split(".", 1)[0]
    if len(segment) > MAX_JOSE_HEADER_CHARS:
        raise JwtRefused("malformed")
    try:
        header = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (binascii.Error, ValueError, RecursionError):
        raise JwtRefused("malformed") from None
    if not isinstance(header, dict) or not isinstance(header.get("alg"), str):
        raise JwtRefused("malformed")
    return header


def _claim_at(claims: dict[str, Any], path: str) -> object:
    value: object = claims
    for name in path.split("."):
        if not isinstance(value, dict) or name not in value:
            return None
        value = value[name]
    return value


def _identity_of(claims: dict[str, Any], path: str, label: str) -> str:
    value = _claim_at(claims, path)
    if value is None:
        raise JwtRefused(f"{label}_missing")
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_IDENTITY_CHARS
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise JwtRefused(f"{label}_invalid")
    return value


def _optional_identity(claims: dict[str, Any], path: str) -> str | None:
    try:
        return _identity_of(claims, path, "username")
    except JwtRefused:
        return None
