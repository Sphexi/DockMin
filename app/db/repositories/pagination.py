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
    payload = json.dumps([position.sort_value, position.row_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(token: str) -> CursorPosition:
    """Decode a token produced by encode_cursor, raising InvalidCursorError if malformed."""
    try:
        decoded = json.loads(base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8"))
    except (binascii.Error, UnicodeError, ValueError):
        raise InvalidCursorError("invalid cursor") from None
    if not (isinstance(decoded, list) and len(decoded) == 2):
        raise InvalidCursorError("invalid cursor")
    sort_value, row_id = decoded
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
