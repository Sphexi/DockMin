"""Opaque keyset cursors and the page container."""

import base64
import binascii
import json
from dataclasses import dataclass

MAX_PAGE_SIZE = 500
MAX_ROW_ID = 2**63 - 1
MIN_SORT_INTEGER = -(2**63)

SortValue = str | int | float


class InvalidCursorError(ValueError):
    """Raised when a cursor token cannot be decoded."""


@dataclass(frozen=True, slots=True)
class CursorPosition:
    """The (sort value, id) of the last row of a page."""

    sort_value: SortValue
    row_id: int


@dataclass(frozen=True, slots=True)
class NamedCursorPosition:
    """A position that also carries the folded name of the last row."""

    sort_value: SortValue
    row_id: int
    name: str


@dataclass(frozen=True, slots=True)
class Page[T]:
    """A bounded slice of results; next_cursor is None on the last page."""

    items: list[T]
    next_cursor: str | None


def validate_limit(limit: int) -> int:
    """Return limit if it is within 1..MAX_PAGE_SIZE, otherwise raise ValueError."""
    if isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return limit


def encode_cursor(position: CursorPosition) -> str:
    """Encode a position as an opaque URL-safe token."""
    return _encode([position.sort_value, position.row_id])


def encode_named_cursor(position: NamedCursorPosition) -> str:
    """Encode a position that carries a name as an opaque URL-safe token."""
    return _encode([position.sort_value, position.row_id, position.name])


def decode_cursor(token: str) -> CursorPosition:
    """Decode a token produced by encode_cursor, raising InvalidCursorError if malformed."""
    parts = _decode(token)
    if len(parts) != 2:
        raise InvalidCursorError("invalid cursor")
    return _position(parts)


def decode_any_cursor(token: str) -> CursorPosition | NamedCursorPosition:
    """Decode a token from either encoder, raising InvalidCursorError if malformed."""
    parts = _decode(token)
    position = _position(parts[:2]) if len(parts) in (2, 3) else None
    if position is None:
        raise InvalidCursorError("invalid cursor")
    if len(parts) == 2:
        return position
    name = parts[2]
    if not isinstance(name, str) or not _encodable(name):
        raise InvalidCursorError("invalid cursor")
    return NamedCursorPosition(position.sort_value, position.row_id, name)


def _encode(payload: list[SortValue | str]) -> str:
    text = json.dumps(payload, separators=(",", ":"))
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _decode(token: str) -> list[object]:
    try:
        decoded = json.loads(base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8"))
    except (binascii.Error, UnicodeError, ValueError):
        raise InvalidCursorError("invalid cursor") from None
    if not isinstance(decoded, list):
        raise InvalidCursorError("invalid cursor")
    return decoded


def _position(parts: list[object]) -> CursorPosition:
    sort_value, row_id = parts
    if isinstance(sort_value, bool) or not isinstance(sort_value, str | int | float):
        raise InvalidCursorError("invalid cursor")
    if isinstance(sort_value, str) and not _encodable(sort_value):
        raise InvalidCursorError("invalid cursor")
    if isinstance(sort_value, int) and not MIN_SORT_INTEGER <= sort_value <= MAX_ROW_ID:
        raise InvalidCursorError("invalid cursor")
    if isinstance(row_id, bool) or not isinstance(row_id, int):
        raise InvalidCursorError("invalid cursor")
    if not MIN_SORT_INTEGER <= row_id <= MAX_ROW_ID:
        raise InvalidCursorError("invalid cursor")
    return CursorPosition(sort_value, row_id)


def _encodable(text: str) -> bool:
    # A JSON escape can decode to a lone surrogate, which no database driver can bind.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
