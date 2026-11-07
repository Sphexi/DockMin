"""Tests for app/db/repositories/pagination.py."""

import base64

import pytest

from app.db.repositories import MAX_PAGE_SIZE, CursorPosition, InvalidCursorError
from app.db.repositories.pagination import decode_cursor, encode_cursor, validate_limit


def token(payload: str) -> str:
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


MALFORMED_CURSORS = [
    pytest.param("not base64 !!", id="not-base64"),
    pytest.param(token("not json"), id="not-json"),
    pytest.param(token('{"sort": 1}'), id="not-a-list"),
    pytest.param(token("[1]"), id="wrong-length"),
    pytest.param(token("[true, 1]"), id="boolean-sort-value"),
    pytest.param(token("[null, 1]"), id="null-sort-value"),
    pytest.param(token('["a", "1"]'), id="string-row-id"),
    pytest.param(token('["a", true]'), id="boolean-row-id"),
    pytest.param(token('["\\ud800", 1]'), id="lone-surrogate-sort-value"),
]


@pytest.mark.parametrize("limit", [1, 50, MAX_PAGE_SIZE])
def test_validate_limit_returns_a_limit_within_range(limit: int) -> None:
    assert validate_limit(limit) == limit


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_SIZE + 1, True])
def test_validate_limit_rejects_a_limit_outside_range(limit: int) -> None:
    with pytest.raises(ValueError, match="limit must be between"):
        validate_limit(limit)


@pytest.mark.parametrize(
    "position",
    [CursorPosition("alpha", 3), CursorPosition(12, 4), CursorPosition(1.5, 5)],
    ids=["text", "integer", "float"],
)
def test_decode_cursor_returns_the_position_encode_cursor_was_given(
    position: CursorPosition,
) -> None:
    assert decode_cursor(encode_cursor(position)) == position


@pytest.mark.parametrize("cursor", MALFORMED_CURSORS)
def test_decode_cursor_rejects_a_malformed_token(cursor: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor)
