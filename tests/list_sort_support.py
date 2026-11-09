"""Helpers for the sorted-list tests: walking a list page by page and reading a refusal."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest

from app.auth.errors import ValidationFailed

WHOLE_LIST = 500
SORT_FIELD = "sort"
DIRECTION_FIELD = "dir"
CURSOR_FIELD = "cursor"
CURSOR_MESSAGE = "The page link is not valid."


@dataclass(frozen=True)
class Fetched:
    names: list[str]
    total: int
    next_cursor: str | None


type Fetch = Callable[[str | None, str | None, int, str | None], Awaitable[Fetched]]


async def paged(fetch: Fetch, sort: str | None, direction: str | None, size: int) -> list[str]:
    """Follow the next-page cursors from page one and return every name read, in order."""
    names: list[str] = []
    cursor = None
    while True:
        page = await fetch(sort, direction, size, cursor)
        names.extend(page.names)
        if page.next_cursor is None:
            return names
        cursor = page.next_cursor


async def whole(fetch: Fetch, sort: str | None, direction: str | None) -> Fetched:
    """Read the whole list in one page."""
    return await fetch(sort, direction, WHOLE_LIST, None)


async def refused_fields(call: Awaitable[object]) -> dict[str, str]:
    """Await a read that must be refused and return the fields it names."""
    with pytest.raises(ValidationFailed) as refused:
        await call
    return refused.value.fields
