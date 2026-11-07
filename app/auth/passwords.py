"""Argon2id password hashing as PHC strings, with verification and parameter upgrade checks."""

import asyncio
import base64
import binascii
import hmac
import re
import secrets
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from app.config import Settings
from app.logging import get_logger

logger = get_logger(__name__)

# Longer input is rejected unhashed: the work is linear in length and the value is untrusted.
MAX_PASSWORD_LENGTH = 1024

# Absolute ceilings on a stored hash's cost parameters, so a damaged row cannot request
# unbounded memory or time.
_MAX_STORED_MEMORY_KIB = 1_048_576
_MAX_STORED_ITERATIONS = 16
_MAX_STORED_LANES = 16

# Hashing requests beyond this many times the concurrency limit are refused, not queued.
_PENDING_PER_SLOT = 4

_PHC = re.compile(
    r"\$argon2id\$v=19\$m=(?P<m>\d{1,9}),t=(?P<t>\d{1,4}),p=(?P<p>\d{1,3})"
    r"\$(?P<salt>[A-Za-z0-9+/]{16,128})\$(?P<digest>[A-Za-z0-9+/]{16,128})"
)

_DUMMY_PASSWORD = "dockmin-timing-equaliser"


@dataclass(frozen=True, slots=True)
class HashParameters:
    """The Argon2id cost and size settings a hash is created with."""

    memory_kib: int
    iterations: int
    lanes: int
    salt_bytes: int
    hash_bytes: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "HashParameters":
        """Read the configured parameters."""
        return cls(
            settings.argon2_memory_kib,
            settings.argon2_iterations,
            settings.argon2_lanes,
            settings.argon2_salt_bytes,
            settings.argon2_hash_bytes,
        )


@dataclass(frozen=True, slots=True)
class _Stored:
    parameters: HashParameters
    salt: bytes
    digest: bytes


def _unpadded_b64decode(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)


def _parse(encoded: str) -> _Stored | None:
    match = _PHC.fullmatch(encoded)
    if match is None:
        return None
    memory, iterations, lanes = int(match["m"]), int(match["t"]), int(match["p"])
    if not (
        0 < memory <= _MAX_STORED_MEMORY_KIB
        and 0 < iterations <= _MAX_STORED_ITERATIONS
        and 0 < lanes <= _MAX_STORED_LANES
    ):
        return None
    try:
        salt, digest = _unpadded_b64decode(match["salt"]), _unpadded_b64decode(match["digest"])
    except (binascii.Error, ValueError):
        return None
    return _Stored(HashParameters(memory, iterations, lanes, len(salt), len(digest)), salt, digest)


def _material(password: str) -> bytes:
    # NFKC so the same typed password hashes identically whatever form the keyboard produced.
    return unicodedata.normalize("NFKC", password).encode("utf-8")


def _kdf(parameters: HashParameters, salt: bytes) -> Argon2id:
    return Argon2id(
        salt=salt,
        length=parameters.hash_bytes,
        iterations=parameters.iterations,
        lanes=parameters.lanes,
        memory_cost=parameters.memory_kib,
    )


def _hash_sync(password: str, parameters: HashParameters, salt: bytes) -> str:
    return _kdf(parameters, salt).derive_phc_encoded(_material(password))


def _verify_sync(encoded: str, password: str) -> bool:
    stored = _parse(encoded)
    if stored is None or len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        candidate = _kdf(stored.parameters, stored.salt).derive(_material(password))
    except ValueError:
        return False
    return hmac.compare_digest(candidate, stored.digest)


class HashingBusy(Exception):
    """Raised when too many hash computations are already queued to accept another."""


def _comparisons(stored: HashParameters, configured: HashParameters) -> tuple[bool, bool]:
    """Return whether any stored parameter is weaker, and whether any is stronger."""
    pairs = (
        (stored.memory_kib, configured.memory_kib),
        (stored.iterations, configured.iterations),
        (stored.lanes, configured.lanes),
        (stored.salt_bytes, configured.salt_bytes),
        (stored.hash_bytes, configured.hash_bytes),
    )
    return any(a < b for a, b in pairs), any(a > b for a, b in pairs)


class PasswordHasher:
    """Hashes and verifies passwords off the event loop, with bounded memory and bounded queue."""

    def __init__(self, parameters: HashParameters, *, max_concurrent: int = 4) -> None:
        self._parameters = parameters
        self._slots = asyncio.Semaphore(max_concurrent)
        self._max_pending = max_concurrent * _PENDING_PER_SLOT
        self._pending = 0
        self._dummy: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "PasswordHasher":
        """Build a hasher from the configured parameters."""
        return cls(HashParameters.from_settings(settings))

    async def prepare(self) -> None:
        """Build the hash used for unknown usernames, so no login pays for it first."""
        if self._dummy is None:
            self._dummy = await self.hash(_DUMMY_PASSWORD)

    async def hash(self, password: str) -> str:
        """Return the PHC string for a password under the configured parameters."""
        if len(password) > MAX_PASSWORD_LENGTH:
            raise ValueError("password is too long to hash")
        salt = secrets.token_bytes(self._parameters.salt_bytes)
        return await self._offload(_hash_sync, password, self._parameters, salt)

    async def verify(self, encoded: str, password: str) -> bool:
        """Report whether the password matches the stored hash, in constant time."""
        return await self._offload(_verify_sync, encoded, password)

    async def verify_against_nothing(self, password: str) -> None:
        """Spend the cost of a real verification when there is no stored hash to check."""
        await self.prepare()
        assert self._dummy is not None
        await self.verify(self._dummy, password)

    def needs_rehash(self, encoded: str) -> bool:
        """Report whether a stored hash is weaker than the configured parameters.

        A stronger hash is left alone, so lowering the configured cost never weakens an account.
        """
        stored = _parse(encoded)
        if stored is None:
            return True
        weaker, stronger = _comparisons(stored.parameters, self._parameters)
        if stronger:
            logger.warning(
                "stored_hash_stronger_than_configured",
                extra={
                    "stored_memory_kib": stored.parameters.memory_kib,
                    "stored_iterations": stored.parameters.iterations,
                    "stored_lanes": stored.parameters.lanes,
                },
            )
        return weaker

    async def _offload[T](self, function: Callable[..., T], *args: Any) -> T:
        if self._pending >= self._max_pending:
            raise HashingBusy
        self._pending += 1
        try:
            async with self._slots:
                return await asyncio.to_thread(function, *args)
        finally:
            self._pending -= 1
