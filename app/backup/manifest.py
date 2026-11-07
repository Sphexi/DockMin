"""Backup archive manifest: shape, canonical encoding, MAC, and the library's errors."""

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from app.crypto.envelope import MAX_KEY_VERSION
from app.crypto.keys import manifest_mac
from app.logging import get_logger

FORMAT: Final = "dockmin-backup"
FORMAT_VERSION: Final = 1
MANIFEST_NAME: Final = "manifest.json"
DATABASE_NAME: Final = "database.sqlite3"
MAX_ARCHIVE_BYTES: Final = 7 * 1024**3
MAX_MANIFEST_BYTES: Final = 1024 * 1024
MAX_STACK_MEMBERS: Final = 10_000
MAX_COMPOSE_BYTES: Final = 131_072
MAX_ADMINISTRATORS: Final = 100

ARCHIVE_MEMBER: Final[re.Pattern[str]] = re.compile(
    r"manifest\.json|database\.sqlite3|stacks/[0-9a-f]{32}/compose\.yaml"
)

MAX_SCHEMA_VERSION: Final = 100_000

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_PRODUCT_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,63}")
_INSTANCE_ID = re.compile(r"[0-9a-f]{32}")
_FINGERPRINT = re.compile(r"[0-9a-f]{16}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_USERNAME = re.compile(r"[A-Za-z0-9._@-]{1,128}")
_STACK_MEMBER = re.compile(r"stacks/[0-9a-f]{32}/compose\.yaml")
_FIELDS = frozenset(
    {
        "format",
        "format_version",
        "schema_version",
        "created_at",
        "product_version",
        "instance_id",
        "key_version",
        "key_fingerprint",
        "administrators",
        "members",
        "mac",
    }
)
_MEMBER_FIELDS = frozenset({"name", "size", "sha256"})

type RestoreReason = Literal[
    "archive_invalid",
    "format_unsupported",
    "insufficient_space",
    "key_missing",
    "key_invalid",
    "fingerprint_mismatch",
    "manifest_tampered",
    "digest_mismatch",
    "schema_newer",
    "migration_failed",
    "database_invalid",
    "schema_mismatch",
    "stack_files_inconsistent",
    "decrypt_failed",
    "reseal_failed",
    "operator_not_in_archive",
    "swap_failed",
    "restart_failed",
    "cancelled",
    "timed_out",
    "shutdown",
]
type ProductionReason = Literal[
    "insufficient_space", "secret_unreadable", "key_material_found", "production_failed"
]

logger = get_logger(__name__)


class ArchiveRefused(Exception):
    """An archive or restore step was refused; message is product text safe to show and store."""

    def __init__(self, reason: RestoreReason, message: str) -> None:
        super().__init__(message)
        self.reason: RestoreReason = reason
        self.message = message


class ProductionFailed(Exception):
    """An archive could not be produced; message is product text safe to show and store."""

    def __init__(self, reason: ProductionReason, message: str) -> None:
        super().__init__(message)
        self.reason: ProductionReason = reason
        self.message = message


@dataclass(frozen=True, slots=True)
class MemberEntry:
    """One archive member as the manifest inventories it."""

    name: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Manifest:
    """The decoded manifest of an archive."""

    format_version: int
    schema_version: int
    created_at: str
    product_version: str
    instance_id: str
    key_version: int
    key_fingerprint: str
    administrators: tuple[str, ...]
    members: tuple[MemberEntry, ...]
    mac: str


def canonical_json(document: dict[str, Any]) -> bytes:
    """Encode a document with sorted keys, no insignificant whitespace and ASCII only."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _document(fields: Manifest, mac: str | None) -> dict[str, Any]:
    document: dict[str, Any] = {
        "format": FORMAT,
        "format_version": fields.format_version,
        "schema_version": fields.schema_version,
        "created_at": fields.created_at,
        "product_version": fields.product_version,
        "instance_id": fields.instance_id,
        "key_version": fields.key_version,
        "key_fingerprint": fields.key_fingerprint,
        "administrators": list(fields.administrators),
        "members": [
            {"name": member.name, "size": member.size, "sha256": member.sha256}
            for member in fields.members
        ],
    }
    if mac is not None:
        document["mac"] = mac
    return document


def encode_manifest(fields: Manifest, *, mac: Callable[[bytes], str]) -> bytes:
    """Return the canonical manifest JSON, signing every field but fields.mac with mac."""
    payload = canonical_json(_document(fields, None))
    encoded = canonical_json(_document(fields, mac(payload)))
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("the manifest exceeds the size limit")
    return encoded


def _refuse(message: str) -> ArchiveRefused:
    return ArchiveRefused("archive_invalid", message)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _parse(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_MANIFEST_BYTES:
        raise _refuse("The manifest is larger than the limit.")
    try:
        parsed = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _refuse("The manifest is not valid JSON.") from None
    if not isinstance(parsed, dict):
        raise _refuse("The manifest is not a JSON object.")
    return parsed


def _integer(value: object, low: int, high: int, what: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _refuse(f"The manifest {what} is not valid.")
    return value


def _text(value: object, pattern: re.Pattern[str], what: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise _refuse(f"The manifest {what} is not valid.")
    return value


def _timestamp(value: object) -> str:
    text = _text(value, _TIMESTAMP, "creation time")
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise _refuse("The manifest creation time is not valid.") from None
    return text


def _member_entry(raw: object) -> MemberEntry:
    if not isinstance(raw, dict) or set(raw) != _MEMBER_FIELDS:
        raise _refuse("The manifest inventory is not valid.")
    name = _text(raw["name"], ARCHIVE_MEMBER, "member name")
    if name == MANIFEST_NAME:
        raise _refuse("The manifest inventory lists the manifest itself.")
    size = _integer(raw["size"], 0, MAX_ARCHIVE_BYTES, "member size")
    if name != DATABASE_NAME and not 1 <= size <= MAX_COMPOSE_BYTES:
        raise _refuse("The manifest lists a stack file of an impossible size.")
    return MemberEntry(name, size, _text(raw["sha256"], _SHA256, "member digest"))


def _members(raw: object) -> tuple[MemberEntry, ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_STACK_MEMBERS + 1:
        raise _refuse("The manifest inventory is not valid.")
    entries = tuple(_member_entry(item) for item in raw)
    names = [entry.name for entry in entries]
    stacks = names[1:]
    if (
        names[0] != DATABASE_NAME
        or any(_STACK_MEMBER.fullmatch(name) is None for name in stacks)
        or stacks != sorted(set(stacks))
    ):
        raise _refuse("The manifest inventory is not in archive order.")
    if sum(entry.size for entry in entries) > MAX_ARCHIVE_BYTES:
        raise _refuse("The manifest inventory exceeds the archive size limit.")
    return entries


def _administrators(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list) or len(raw) > MAX_ADMINISTRATORS:
        raise _refuse("The manifest administrator list is not valid.")
    names = tuple(_text(item, _USERNAME, "administrator name") for item in raw)
    if list(names) != sorted(names):
        raise _refuse("The manifest administrator list is not sorted.")
    return names


def decode_manifest(data: bytes) -> Manifest:
    """Return the manifest in data after checking its shape, not its signature."""
    document = _parse(data)
    if document.get("format") != FORMAT:
        raise _refuse("This file is not a backup archive.")
    version = document.get("format_version")
    if type(version) is not int or version != FORMAT_VERSION:
        raise ArchiveRefused(
            "format_unsupported",
            f"The archive format is not supported; this build reads version {FORMAT_VERSION}.",
        )
    if set(document) != _FIELDS:
        raise _refuse("The manifest does not have the expected fields.")
    manifest = Manifest(
        format_version=FORMAT_VERSION,
        schema_version=_integer(
            document["schema_version"], 1, MAX_SCHEMA_VERSION, "schema version"
        ),
        created_at=_timestamp(document["created_at"]),
        product_version=_text(document["product_version"], _PRODUCT_VERSION, "product version"),
        instance_id=_text(document["instance_id"], _INSTANCE_ID, "instance id"),
        key_version=_integer(document["key_version"], 1, MAX_KEY_VERSION, "key version"),
        key_fingerprint=_text(document["key_fingerprint"], _FINGERPRINT, "key fingerprint"),
        administrators=_administrators(document["administrators"]),
        members=_members(document["members"]),
        mac=_text(document["mac"], _SHA256, "signature"),
    )
    if canonical_json(document) != data:
        raise _refuse("The manifest is not in canonical form.")
    return manifest


def manifest_mac_matches(data: bytes, key: bytes) -> bool:
    """Report whether the manifest in data carries a valid MAC under key."""
    try:
        document = _parse(data)
        presented = document.pop("mac", None)
    except ArchiveRefused:
        return False
    if type(presented) is not str:
        return False
    expected = manifest_mac(key, canonical_json(document))
    return hmac.compare_digest(expected.encode("ascii"), presented.encode("utf-8", "replace"))


def sha256_hex(data: bytes) -> str:
    """Return the SHA-256 digest of data as lower-case hex."""
    return hashlib.sha256(data).hexdigest()
