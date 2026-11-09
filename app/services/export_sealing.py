"""Seals named values under a passphrase for an export archive, and opens them again; pure."""

import base64
import binascii
import hmac
import os
import re
import unicodedata
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth.errors import ProductError, ValidationFailed

PASSPHRASE_MIN_CHARS: Final = 12
PASSPHRASE_MAX_CHARS: Final = 1024
MAX_RECORDS: Final = 5000
MAX_VALUE_BYTES: Final = 256 * 1024
MAX_NAME_CHARS: Final = 256
ARCHIVE_ID_BYTES: Final = 16
SALT_BYTES: Final = 16
NONCE_BYTES: Final = 12
KEY_BYTES: Final = 32
MAC_BYTES: Final = 32

MIN_MEMORY_KIB: Final = 8192
MAX_MEMORY_KIB: Final = 262_144
MAX_ITERATIONS: Final = 8
MAX_LANES: Final = 16

_AAD_PREFIX: Final = b"dockmin/export-seal/1"
_ENCRYPTION_INFO: Final = b"dockmin/export-seal/1 aes-256-gcm"
_AUTHENTICATION_INFO: Final = b"dockmin/export-seal/1 hmac-sha256"
_MAC_PREFIX: Final = b"dockmin/export-mac/1\0"
_ARCHIVE_ID: Final = re.compile(r"[0-9a-f]{32}")
_BASE64: Final = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_FAILURE: Final = "The sealed values could not be opened."


class SealError(ProductError):
    """The sealed values could not be opened; the message never says why or which value."""

    def __init__(self) -> None:
        super().__init__(_FAILURE)


@dataclass(frozen=True, slots=True)
class SealCost:
    """The Argon2id cost recorded with an archive."""

    memory_kib: int = 65_536
    iterations: int = 3
    lanes: int = 4


DEFAULT_COST: Final = SealCost()
MINIMUM_COST: Final = SealCost(MIN_MEMORY_KIB, 1, 1)


class KdfParameters(BaseModel):
    """The key derivation recorded with an archive; `salt` is base64."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: Literal["argon2id"]
    version: Literal[19]
    memory_kib: int = Field(ge=MIN_MEMORY_KIB, le=MAX_MEMORY_KIB)
    iterations: int = Field(ge=1, le=MAX_ITERATIONS)
    lanes: int = Field(ge=1, le=MAX_LANES)
    salt: str

    @field_validator("salt")
    @classmethod
    def _salt_is_sixteen_bytes(cls, value: str) -> str:
        if len(_decode(value)) != SALT_BYTES:
            raise ValueError("salt must be 16 bytes")
        return value


class SealedRecord(BaseModel):
    """One sealed value: its name, nonce and ciphertext with the tag, both base64."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS, pattern=r"[^\x00]+")
    nonce: str
    ciphertext: str = Field(repr=False)

    @field_validator("nonce")
    @classmethod
    def _nonce_is_twelve_bytes(cls, value: str) -> str:
        if len(_decode(value)) != NONCE_BYTES:
            raise ValueError("nonce must be 12 bytes")
        return value

    @field_validator("ciphertext")
    @classmethod
    def _ciphertext_is_base64(cls, value: str) -> str:
        _decode(value)
        return value


class SealedValues(BaseModel):
    """Everything an archive records to open its sealed values with the passphrase.

    `mac` is the base64 HMAC-SHA256 over the manifest that holds this section, minus `mac`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cipher: Literal["aes-256-gcm"]
    kdf: KdfParameters
    records: list[SealedRecord] = Field(max_length=MAX_RECORDS)
    mac: str | None = None

    @field_validator("mac")
    @classmethod
    def _mac_is_thirty_two_bytes(cls, value: str | None) -> str | None:
        if value is not None and len(_decode(value)) != MAC_BYTES:
            raise ValueError("mac must be 32 bytes")
        return value

    @field_validator("records")
    @classmethod
    def _names_are_unique(cls, records: list[SealedRecord]) -> list[SealedRecord]:
        names = [record.name for record in records]
        if len(set(names)) != len(names):
            raise ValueError("sealed record names must be unique")
        return records


def new_archive_id() -> str:
    """Return a random archive id: 32 lowercase hexadecimal characters."""
    return os.urandom(ARCHIVE_ID_BYTES).hex()


def is_valid_archive_id(archive_id: str) -> bool:
    """Report whether `archive_id` has the shape `new_archive_id` produces."""
    return _ARCHIVE_ID.fullmatch(archive_id) is not None


def check_passphrase(passphrase: str) -> None:
    """Raise ValidationFailed unless the passphrase is 12 to 1024 characters after NFKC."""
    length = len(unicodedata.normalize("NFKC", passphrase))
    if not PASSPHRASE_MIN_CHARS <= length <= PASSPHRASE_MAX_CHARS:
        raise ValidationFailed(
            {
                "passphrase": (
                    f"The passphrase must be {PASSPHRASE_MIN_CHARS} to "
                    f"{PASSPHRASE_MAX_CHARS} characters."
                )
            }
        )


def seal(
    values: Mapping[str, bytes],
    passphrase: str,
    archive_id: str,
    *,
    cost: SealCost = DEFAULT_COST,
    authenticate: Callable[[SealedValues], bytes] | None = None,
) -> SealedValues:
    """Seal each value under a key derived from the passphrase and a fresh random salt.

    With `authenticate`, the result carries a MAC over the bytes it returns for the section
    without its MAC; those bytes must cover the whole manifest. Blocking and memory-hungry: run
    it with `asyncio.to_thread`. ValidationFailed for a passphrase outside the length bounds,
    ValueError for a malformed archive id, name or cost.
    """
    check_passphrase(passphrase)
    if not is_valid_archive_id(archive_id):
        raise ValueError("archive_id must be 32 lowercase hexadecimal characters")
    if len(values) > MAX_RECORDS:
        raise ValueError("too many values to seal")
    for name, value in values.items():
        _check_name(name)
        if len(value) > MAX_VALUE_BYTES:
            raise ValueError("a value is too large to seal")
    salt = os.urandom(SALT_BYTES)
    parameters = KdfParameters(
        algorithm="argon2id",
        version=19,
        memory_kib=cost.memory_kib,
        iterations=cost.iterations,
        lanes=cost.lanes,
        salt=base64.b64encode(salt).decode("ascii"),
    )
    records: list[SealedRecord] = []
    keys = (
        _derive(unicodedata.normalize("NFKC", passphrase), parameters, salt)
        if values or authenticate is not None
        else None
    )
    if keys is not None:
        cipher = AESGCM(keys.encryption)
        for name in sorted(values):
            nonce = os.urandom(NONCE_BYTES)
            body = cipher.encrypt(nonce, values[name], _aad(archive_id, name))
            records.append(
                SealedRecord(
                    name=name,
                    nonce=base64.b64encode(nonce).decode("ascii"),
                    ciphertext=base64.b64encode(body).decode("ascii"),
                )
            )
    sealed = SealedValues(cipher="aes-256-gcm", kdf=parameters, records=records)
    if keys is None or authenticate is None:
        return sealed
    mac = _mac(keys.authentication, authenticate(sealed))
    return sealed.model_copy(update={"mac": base64.b64encode(mac).decode("ascii")})


def open_sealed(
    sealed: SealedValues,
    passphrase: str,
    archive_id: str,
    names: Collection[str] | None = None,
    *,
    authenticated: bytes | None = None,
) -> dict[str, bytes]:
    """Open every record, or only those in `names`, and return the values by name.

    With `authenticated`, the bytes the exporter authenticated (the manifest without the MAC) are
    checked against `sealed.mac` first; without it nothing but the records is checked. Blocking
    and memory-hungry: run it with `asyncio.to_thread`. SealError for a wrong passphrase, a
    record that was altered, renamed or taken from another archive, a manifest that does not
    match its MAC, or a name that is not in `sealed`; the error says nothing of which.
    """
    material = unicodedata.normalize("NFKC", passphrase)
    if len(material) > PASSPHRASE_MAX_CHARS or not is_valid_archive_id(archive_id):
        raise SealError
    by_name = {record.name: record for record in sealed.records}
    wanted = list(by_name) if names is None else list(names)
    if any(name not in by_name for name in wanted):
        raise SealError
    if not wanted and authenticated is None:
        return {}
    keys = _derive(material, sealed.kdf, _decode(sealed.kdf.salt))
    if authenticated is not None:
        _check_mac(sealed, keys, authenticated)
    cipher = AESGCM(keys.encryption)
    opened: dict[str, bytes] = {}
    for name in wanted:
        record = by_name[name]
        try:
            opened[name] = cipher.decrypt(
                _decode(record.nonce), _decode(record.ciphertext), _aad(archive_id, name)
            )
        except (InvalidTag, ValueError):
            raise SealError from None
    return opened


def verify_sealed(
    sealed: SealedValues, passphrase: str, archive_id: str, authenticated: bytes
) -> None:
    """Check `authenticated` against the MAC with the passphrase; SealError if it does not match.

    Blocking and memory-hungry: run it with `asyncio.to_thread`.
    """
    open_sealed(sealed, passphrase, archive_id, (), authenticated=authenticated)


@dataclass(frozen=True, slots=True)
class _Keys:
    encryption: bytes = field(repr=False)
    authentication: bytes = field(repr=False)


def _derive(material: str, parameters: KdfParameters, salt: bytes) -> _Keys:
    root = Argon2id(
        salt=salt,
        length=KEY_BYTES,
        iterations=parameters.iterations,
        lanes=parameters.lanes,
        memory_cost=parameters.memory_kib,
    ).derive(material.encode("utf-8"))
    return _Keys(_expand(root, _ENCRYPTION_INFO), _expand(root, _AUTHENTICATION_INFO))


def _expand(root: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=info).derive(root)


def _mac(key: bytes, authenticated: bytes) -> bytes:
    return hmac.digest(key, _MAC_PREFIX + authenticated, "sha256")


def _check_mac(sealed: SealedValues, keys: _Keys, authenticated: bytes) -> None:
    try:
        recorded = _decode(sealed.mac) if sealed.mac is not None else None
    except ValueError:
        raise SealError from None
    if recorded is None or not hmac.compare_digest(
        recorded, _mac(keys.authentication, authenticated)
    ):
        raise SealError


def _aad(archive_id: str, name: str) -> bytes:
    # NUL cannot occur in a validated archive id or name, so the fields cannot run together.
    return b"\0".join((_AAD_PREFIX, archive_id.encode("ascii"), name.encode("utf-8")))


def _check_name(name: str) -> None:
    if not 1 <= len(name) <= MAX_NAME_CHARS or "\0" in name:
        raise ValueError("a sealed name must be 1 to 256 characters with no NUL")


def _decode(text: str) -> bytes:
    if _BASE64.fullmatch(text) is None:
        raise ValueError("not base64")
    try:
        return base64.b64decode(text, validate=True)
    except binascii.Error:
        raise ValueError("not base64") from None
