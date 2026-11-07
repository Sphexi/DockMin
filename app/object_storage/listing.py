"""Parses a ListObjectsV2 reply with bounded patterns."""

import re
from dataclasses import dataclass
from typing import Final

from app.object_storage.errors import StorageFailure
from app.object_storage.keys import validate_key

MAX_LIST_BODY: Final = 2 * 1024 * 1024

_OPEN: Final = b"<Contents>"
_CLOSE: Final = b"</Contents>"
_MAX_BLOCK: Final = 4096
_KEY: Final = re.compile(rb"<Key>([A-Za-z0-9._/-]{1,1024})</Key>")
_SIZE: Final = re.compile(rb"<Size>([0-9]{1,20})</Size>")
_MODIFIED: Final = re.compile(rb"<LastModified>([^<]{0,64})</LastModified>")
_TIMESTAMP: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", re.ASCII)
_ROOT: Final = re.compile(rb"<ListBucketResult[ >]")
_TRUNCATED: Final = re.compile(rb"<IsTruncated>(true|false)</IsTruncated>")
_TOKEN: Final = re.compile(rb"<NextContinuationToken>([^<]{0,2048})</NextContinuationToken>")
# Each predefined XML entity decodes outside this alphabet, so a token holding one is refused.
TOKEN_TEXT: Final = re.compile(r"[A-Za-z0-9+/=._-]{1,1024}", re.ASCII)


@dataclass(frozen=True, slots=True)
class ListedObject:
    """One object in a listing; the key is within the product's key alphabet."""

    key: str
    size: int
    last_modified: str | None


@dataclass(frozen=True, slots=True)
class ObjectListing:
    """One page of a listing; skipped counts entries that were not usable and were dropped."""

    objects: tuple[ListedObject, ...]
    truncated: bool
    next_token: str | None
    skipped: int


def parse_listing(body: bytes, *, prefix: str, sent_token: str | None) -> ObjectListing:
    """Return the page in body, or raise StorageFailure('storage_error') when it is not one."""
    objects: list[ListedObject] = []
    skipped = 0
    outside: list[bytes] = []
    position = 0
    while True:
        start = body.find(_OPEN, position)
        if start < 0:
            outside.append(body[position:])
            break
        outside.append(body[position:start])
        content = start + len(_OPEN)
        end = body.find(_CLOSE, content, content + _MAX_BLOCK)
        if end < 0:
            skipped += 1
            position = content
            continue
        block = body[content:end]
        if _OPEN in block:
            skipped += 1
            position = content
            continue
        entry = _entry(block, prefix)
        if entry is None:
            skipped += 1
        else:
            objects.append(entry)
        position = end + len(_CLOSE)
    rest = b"".join(outside)
    truncated = _truncated(rest)
    if truncated is None:
        raise StorageFailure("storage_error", status=200)
    token = _next_token(rest) if truncated else None
    if truncated and (token is None or token == sent_token):
        raise StorageFailure("storage_error", status=200)
    return ObjectListing(tuple(objects), truncated, token, skipped)


def _entry(block: bytes, prefix: str) -> ListedObject | None:
    keys: list[bytes] = _KEY.findall(block)
    sizes: list[bytes] = _SIZE.findall(block)
    modified: list[bytes] = _MODIFIED.findall(block)
    if len(keys) != 1 or len(sizes) != 1 or len(modified) > 1:
        return None
    key = keys[0].decode("ascii")
    stamp: str | None = None
    if modified:
        stamp = modified[0].decode("ascii", errors="replace")
        if _TIMESTAMP.fullmatch(stamp) is None:
            return None
    if not key.startswith(prefix) or not _usable_key(key):
        return None
    return ListedObject(key, int(sizes[0]), stamp)


def _usable_key(key: str) -> bool:
    try:
        validate_key(key)
    except StorageFailure:
        return False
    return True


def _truncated(rest: bytes) -> bool | None:
    flags: list[bytes] = _TRUNCATED.findall(rest)
    if _ROOT.search(rest) is None or len(flags) != 1:
        return None
    return bool(flags[0] == b"true")


def _next_token(rest: bytes) -> str | None:
    tokens: list[bytes] = _TOKEN.findall(rest)
    if len(tokens) != 1:
        return None
    text = tokens[0].decode("ascii", errors="replace")
    return text if TOKEN_TEXT.fullmatch(text) else None
