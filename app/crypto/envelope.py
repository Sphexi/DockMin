"""AES-256-GCM encryption of single values, bound to the row that stores them."""

import base64
import binascii
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
INSTANCE_ID_BYTES = 16
MAX_KEY_VERSION = 999_999_999

# Both names are interpolated into SQL and into the authenticated data.
PLAIN_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")

_AAD_PREFIX = b"dockmin/field/2"
_TOKEN = re.compile(
    r"v(?P<version>[1-9][0-9]{0,8}):(?P<nonce>[A-Za-z0-9+/=]+):(?P<body>[A-Za-z0-9+/=]+)"
)
_INSTANCE_ID = re.compile(r"[0-9a-f]{32}")


class DecryptionError(Exception):
    """A stored value could not be authenticated or decoded; the cause is deliberately not said."""


class UnknownKeyVersionError(DecryptionError):
    """A stored value was written at a key version this process holds no key for."""

    def __init__(self, version: int) -> None:
        super().__init__(f"no key is available for key version {version}")
        self.version = version


@dataclass(frozen=True, slots=True)
class SecretLocation:
    """Where a ciphertext is stored; it is authenticated, so it cannot be moved."""

    table: str
    column: str
    row_id: int

    def __post_init__(self) -> None:
        if (
            PLAIN_IDENTIFIER.fullmatch(self.table) is None
            or PLAIN_IDENTIFIER.fullmatch(self.column) is None
        ):
            raise ValueError("table and column must be plain lowercase identifiers")
        if self.row_id < 1:
            raise ValueError("row_id must be a stored row's id")

    def aad(self, instance_id: str) -> bytes:
        """Return the additional authenticated data for this location in one database."""
        # NUL cannot occur in a validated identifier or instance id, so the fields cannot run
        # together.
        return b"\0".join(
            (
                _AAD_PREFIX,
                _checked_instance_id(instance_id).encode(),
                self.table.encode(),
                self.column.encode(),
                str(self.row_id).encode(),
            )
        )


def new_instance_id() -> str:
    """Return a random identifier for a database, to be stored beside its canary."""
    return os.urandom(INSTANCE_ID_BYTES).hex()


def is_valid_instance_id(instance_id: str) -> bool:
    """Report whether instance_id has the shape new_instance_id produces."""
    return _INSTANCE_ID.fullmatch(instance_id) is not None


def _checked_instance_id(instance_id: str) -> str:
    if not is_valid_instance_id(instance_id):
        raise ValueError("instance_id must be 32 lowercase hexadecimal characters")
    return instance_id


def _check_key(key: bytes) -> None:
    if len(key) != KEY_BYTES:
        raise ValueError(f"key must be exactly {KEY_BYTES} bytes")


def encrypt(
    plaintext: str, key: bytes, version: int, location: SecretLocation, instance_id: str
) -> str:
    """Return v<version>:<b64 nonce>:<b64 ciphertext+tag> for plaintext at location."""
    _check_key(key)
    if not 1 <= version <= MAX_KEY_VERSION:
        raise ValueError("key version out of range")
    # Random 96-bit nonces are safe only below about 2**32 encryptions under one key.
    nonce = os.urandom(NONCE_BYTES)
    body = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), location.aad(instance_id))
    encoded_nonce = base64.b64encode(nonce).decode("ascii")
    encoded_body = base64.b64encode(body).decode("ascii")
    return f"v{version}:{encoded_nonce}:{encoded_body}"


def key_version_of(token: str) -> int:
    """Return the key version a stored value was written at."""
    match = _TOKEN.fullmatch(token)
    if match is None:
        raise DecryptionError("stored value could not be decrypted")
    return int(match["version"])


def decrypt(
    token: str, keys: Mapping[int, bytes], location: SecretLocation, instance_id: str
) -> str:
    """Decrypt token with the key for its version, raising DecryptionError on any failure."""
    version = key_version_of(token)
    key = keys.get(version)
    if key is None:
        raise UnknownKeyVersionError(version)
    _check_key(key)
    aad = location.aad(instance_id)
    match = _TOKEN.fullmatch(token)
    assert match is not None
    try:
        nonce = base64.b64decode(match["nonce"], validate=True)
        body = base64.b64decode(match["body"], validate=True)
        if len(nonce) != NONCE_BYTES:
            raise ValueError
        return AESGCM(key).decrypt(nonce, body, aad).decode("utf-8")
    except (InvalidTag, binascii.Error, ValueError):
        # One message for a wrong key, wrong location, tampering and malformed input alike, and
        # no chaining, so nothing about the ciphertext reaches a traceback.
        raise DecryptionError("stored value could not be decrypted") from None
