"""Constants and small helpers shared by the datastore tests."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.db.repositories import Clock, Page

CREATED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
CREATED_STAMP = "2026-01-02T03:04:05Z"
LATER_AT = datetime(2026, 1, 2, 4, 4, 5, tzinfo=UTC)
LATER_STAMP = "2026-01-02T04:04:05Z"

PASSWORD_HASH = "placeholder-password-hash"
UNKNOWN_ID = 9999

ACTIVITY_INSERT = (
    "INSERT INTO activity_event (category, actor_type, actor_user_id, actor_subsystem, "
    "target_type, action, result, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def at(moment: datetime) -> Clock:
    """Return a clock that always reports moment."""
    return lambda: moment


def activity_row(
    actor_type: str, actor_user_id: int | None, actor_subsystem: str | None
) -> tuple[Any, ...]:
    """Return the parameters for ACTIVITY_INSERT with only the actor columns varying."""
    return (
        "user_action",
        actor_type,
        actor_user_id,
        actor_subsystem,
        "environment",
        "restart",
        "success",
        CREATED_STAMP,
    )


@dataclass(slots=True)
class Walk:
    """The result of paging through a list to its end."""

    keys: list[Any] = field(default_factory=list)
    page_sizes: list[int] = field(default_factory=list)
    final_cursor: str | None = None


async def walk_pages(
    list_page: Callable[..., Awaitable[Page[Any]]], *, limit: int, key: Callable[[Any], Any]
) -> Walk:
    """Follow next_cursor until it is exhausted, recording each item's key and each page's size."""
    walk = Walk()
    cursor: str | None = None
    while True:
        page = await list_page(limit=limit, cursor=cursor)
        walk.keys.extend(key(item) for item in page.items)
        walk.page_sizes.append(len(page.items))
        cursor = page.next_cursor
        if cursor is None:
            return walk
