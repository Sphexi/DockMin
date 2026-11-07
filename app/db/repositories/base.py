"""Repository base class: row mapping, single-row reads, writes and keyset pagination."""

import re
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any

from app.db.engine import Params, Session, WriteResult
from app.db.repositories.pagination import (
    CursorPosition,
    Page,
    SortValue,
    decode_cursor,
    encode_cursor,
    validate_limit,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def like_prefix(text: str) -> str:
    """Return a LIKE pattern for values starting with text; pair it with ESCAPE '\\'."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def row_as[T](model: Callable[..., T], row: Mapping[str, Any]) -> T:
    """Build a model from a row by column name."""
    return model(**dict(row))


class Repository[T](ABC):
    """Base for repositories; the only place SQL is run, and it offers no unbounded list."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def in_transaction(self) -> bool:
        """Report whether the session is inside a transaction."""
        return self._session.in_transaction

    @abstractmethod
    def _map_row(self, row: sqlite3.Row) -> T:
        """Convert one result row into the repository's model."""

    async def _one(self, sql: str, params: Params = ()) -> T | None:
        row = await self._session.fetch_one(sql, params)
        return None if row is None else self._map_row(row)

    async def _execute(self, sql: str, params: Params = ()) -> WriteResult:
        return await self._session.execute(sql, params)

    async def _page(
        self,
        select: str,
        *,
        limit: int,
        cursor: str | None,
        sort_column: str,
        descending: bool = False,
        where: str = "1 = 1",
        params: Params = (),
        id_column: str = "id",
    ) -> Page[T]:
        """Return one keyset page ordered by (sort_column, id_column).

        select is a SELECT ... FROM clause without WHERE, ORDER BY or LIMIT. It must return the
        sort and id columns, and the sort column must be NOT NULL.
        """
        validate_limit(limit)
        sort_expr, id_expr = _identifier(sort_column), _identifier(id_column)
        direction, comparison = ("DESC", "<") if descending else ("ASC", ">")
        conditions = [f"({where})"]
        bound: list[Any] = list(params)
        if cursor is not None:
            position = decode_cursor(cursor)
            conditions.append(f"({sort_expr}, {id_expr}) {comparison} (?, ?)")
            bound.extend((position.sort_value, position.row_id))
        # One extra row reveals whether another page follows without a second query.
        bound.append(limit + 1)
        sql = (
            f"{select} WHERE {' AND '.join(conditions)} "
            f"ORDER BY {sort_expr} {direction}, {id_expr} {direction} LIMIT ?"
        )
        rows = await self._session.fetch_limited(sql, bound, limit=limit + 1)
        page_rows = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = page_rows[-1]
            sort_value: SortValue = last[sort_expr.rsplit(".", 1)[-1]]
            next_cursor = encode_cursor(
                CursorPosition(sort_value, int(last[id_expr.rsplit(".", 1)[-1]]))
            )
        return Page([self._map_row(row) for row in page_rows], next_cursor)


def _identifier(value: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("sort and id columns must be plain column names")
    return value
