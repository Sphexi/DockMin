"""The `sort` and `dir` query parameters of the object lists and the sort their links carry."""

from collections.abc import Mapping
from typing import Annotated, Final

from fastapi import Query

from app.services.list_order import SORT_MAX
from app.web.pagination import Direction

DIRECTION_MAX: Final = 8

SortParam = Annotated[str | None, Query(max_length=SORT_MAX)]
DirectionParam = Annotated[str | None, Query(alias="dir", max_length=DIRECTION_MAX)]


def view_sort(
    sort: str | None, direction: str | None, refused: Mapping[str, str] | None = None
) -> tuple[str | None, Direction]:
    """Return the sort and direction a page's links carry; a refused value is dropped."""
    if not sort or (refused and ("sort" in refused or "dir" in refused)):
        return None, "asc"
    return sort, "desc" if direction == "desc" else "asc"
