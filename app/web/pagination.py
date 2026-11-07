"""The view-side cursor contract: the only object the pagination and table partials accept."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args
from urllib.parse import urlencode

from app.db.repositories.pagination import Page, decode_cursor, validate_limit

Direction = Literal["asc", "desc"]
ColumnWidth = Literal[
    "select",
    "badge",
    "time",
    "count",
    "short",
    "medium",
    "names",
    "actions",
    "actions-wide",
    "actions-row",
]
COLUMN_WIDTHS: tuple[str, ...] = get_args(ColumnWidth)

CURSOR_PARAM = "cursor"
LIMIT_PARAM = "limit"
SORT_PARAM = "sort"
DIRECTION_PARAM = "dir"
DEFAULT_LIMIT = 25


@dataclass(frozen=True, slots=True)
class Column:
    """One table column; only columns the repository can order by may be sortable.

    An optional column is hidden below the narrow breakpoint, a wide-only column below the
    wide one. A column with a width keeps it in every render of the table; a column without one
    takes the remaining space.
    """

    key: str
    label: str
    sortable: bool = False
    numeric: bool = False
    optional: bool = False
    width: ColumnWidth | None = None
    wide_only: bool = False

    def __post_init__(self) -> None:
        if self.width is not None and self.width not in COLUMN_WIDTHS:
            raise ValueError(f"unknown column width {self.width!r}")


@dataclass(frozen=True, slots=True)
class PageView:
    """A page of results plus everything needed to link to its neighbours."""

    base_path: str
    limit: int
    item_count: int
    cursor: str | None
    next_cursor: str | None
    sort: str | None = None
    direction: Direction = "asc"
    # Filters carried on every link so paging never silently drops them.
    params: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        validate_limit(self.limit)
        # Only same-origin absolute paths: this value ends up in href and hx-get attributes.
        if not self.base_path.startswith("/") or self.base_path.startswith("//"):
            raise ValueError("base_path must be a same-origin absolute path")
        if self.item_count < 0:
            raise ValueError("item_count must not be negative")
        if self.direction not in ("asc", "desc"):
            raise ValueError("direction must be 'asc' or 'desc'")
        for token in (self.cursor, self.next_cursor):
            if token is not None:
                decode_cursor(token)

    @classmethod
    def of(
        cls,
        page: Page[Any],
        *,
        base_path: str,
        limit: int,
        cursor: str | None = None,
        sort: str | None = None,
        direction: Direction = "asc",
        params: Sequence[tuple[str, str]] = (),
    ) -> "PageView":
        """Build the view from a repository page and the query that produced it."""
        return cls(
            base_path=base_path,
            limit=limit,
            item_count=len(page.items),
            cursor=cursor,
            next_cursor=page.next_cursor,
            sort=sort,
            direction=direction,
            params=tuple(params),
        )

    @property
    def is_empty(self) -> bool:
        """Report whether the page has no rows."""
        return self.item_count == 0

    @property
    def has_next(self) -> bool:
        """Report whether another page follows."""
        return self.next_cursor is not None

    @property
    def is_first_page(self) -> bool:
        """Report whether this page was requested without a cursor."""
        return self.cursor is None

    def _href(self, *, cursor: str | None, sort: str | None, direction: Direction) -> str:
        query: list[tuple[str, str]] = list(self.params)
        if sort is not None:
            query += [(SORT_PARAM, sort), (DIRECTION_PARAM, direction)]
        query.append((LIMIT_PARAM, str(self.limit)))
        if cursor is not None:
            query.append((CURSOR_PARAM, cursor))
        return f"{self.base_path}?{urlencode(query)}"

    @property
    def first_href(self) -> str:
        """Link to the first page under the current ordering."""
        return self._href(cursor=None, sort=self.sort, direction=self.direction)

    @property
    def next_href(self) -> str | None:
        """Link to the following page, or None on the last page."""
        if self.next_cursor is None:
            return None
        return self._href(cursor=self.next_cursor, sort=self.sort, direction=self.direction)

    def sort_href(self, key: str) -> str:
        """Link that orders by key, toggling direction on the active column, from page one."""
        direction: Direction = "desc" if self.sort == key and self.direction == "asc" else "asc"
        # A cursor encodes a position in the old ordering and is meaningless in the new one.
        return self._href(cursor=None, sort=key, direction=direction)

    def aria_sort(self, key: str) -> str:
        """Return the aria-sort value for a column header."""
        if self.sort != key:
            return "none"
        return "ascending" if self.direction == "asc" else "descending"
