"""Sort keys, ordering and cursors shared by the host-object lists."""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from app.auth.errors import ValidationFailed
from app.db.repositories.pagination import (
    CursorPosition,
    InvalidCursorError,
    SortValue,
    decode_cursor,
    encode_cursor,
)

type Kind = Literal["text", "number"]
type Direction = Literal["asc", "desc"]
type Scalar = str | int | None

DIRECTIONS: Final[tuple[Direction, ...]] = ("asc", "desc")
SORT_MAX: Final = 32
INVALID_CURSOR: Final = "The page link is not valid."

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_INTEGER = re.compile(r"-?[0-9]{1,19}")


@dataclass(frozen=True, slots=True)
class SortKey[T]:
    """One sortable column: the kind of value it reads and how an entry yields it.

    A value of None is an empty value and sorts below every other value.
    """

    kind: Kind
    value: Callable[[T], Scalar]


def text_key[T](value: Callable[[T], str | None]) -> SortKey[T]:
    """Return a key over text, compared case-folded."""
    return SortKey("text", value)


def number_key[T](value: Callable[[T], int | None]) -> SortKey[T]:
    """Return a key over whole numbers."""
    return SortKey("number", value)


def moment_key(moment: datetime | None) -> int | None:
    """Return a timestamp as whole microseconds since the epoch, or None."""
    return None if moment is None else (moment - _EPOCH) // _MICROSECOND


def rank_key(order: Sequence[str]) -> Callable[[str | None], int | None]:
    """Return a function giving each listed value its place in `order`, and None to any other."""
    places = {value: place for place, value in enumerate(order, start=1)}
    return lambda value: None if value is None else places.get(value)


@dataclass(frozen=True, slots=True)
class ListOrder[T]:
    """A list's key table.

    `default` names the text key an unsorted request orders by, ascending. Ties break on the
    folded `name`, then `row_id`, then `entry_id`, all in the sort direction. `unsorted_empty`
    is the value an unsorted request gives an entry with no value under the default key; a
    request that names a sort gives it the lowest value.
    """

    default: str
    keys: Mapping[str, SortKey[T]]
    name: Callable[[T], str]
    row_id: Callable[[T], int]
    entry_id: Callable[[T], str]
    unsorted_empty: str | None = None

    def __post_init__(self) -> None:
        key = self.keys.get(self.default)
        if key is None or key.kind != "text":
            raise ValueError("the default key must be a text key of the table")


@dataclass(frozen=True, slots=True)
class Position:
    """Where a cursor points: the sort value and row id of the last row of the earlier page."""

    value: str | int | None
    row_id: int


@dataclass(frozen=True, slots=True)
class Window[T]:
    """One page of an ordered list, how many entries the list holds, and the next cursor."""

    items: list[T]
    total: int
    next_cursor: str | None


def _rank(kind: Kind, scalar: Scalar) -> tuple[int, int] | str:
    if kind == "text":
        return "" if scalar is None else str(scalar)
    return (0, 0) if scalar is None else (1, int(scalar))


class Ordering[T]:
    """A validated sort request over one list's key table."""

    def __init__(
        self, order: ListOrder[T], key: str, direction: Direction, *, named: bool = True
    ) -> None:
        self._order = order
        self._key = order.keys[key]
        self.key = key
        self.direction = direction
        self._tag = f"{key}.{direction}:" if named else None
        self._empty = order.unsorted_empty if not named and order.unsorted_empty else ""

    @classmethod
    def resolve(cls, order: ListOrder[T], sort: str | None, direction: str | None) -> "Ordering[T]":
        """Validate a sort key and direction; ValidationFailed keyed `sort` or `dir` otherwise.

        A direction without a sort key is checked and then ignored.
        """
        sort = sort or None
        direction = direction or None
        fields: dict[str, str] = {}
        if sort is not None and sort not in order.keys:
            fields["sort"] = "Sort by one of the listed columns."
        if direction is not None and direction not in DIRECTIONS:
            fields["dir"] = "Choose ascending or descending."
        if fields:
            raise ValidationFailed(fields)
        if sort is None:
            return cls(order, order.default, "asc", named=False)
        return cls(order, sort, "desc" if direction == "desc" else "asc")

    @property
    def descending(self) -> bool:
        """Report whether the list runs from the highest value down."""
        return self.direction == "desc"

    def position(self, cursor: str | None) -> Position | None:
        """Decode a cursor minted under this ordering; ValidationFailed keyed `cursor` if not."""
        if cursor is None:
            return None
        try:
            raw = decode_cursor(cursor)
        except InvalidCursorError:
            raise ValidationFailed({"cursor": INVALID_CURSOR}) from None
        return Position(self._value_of(raw.sort_value), raw.row_id)

    def encode(self, value: str | int | None, row_id: int) -> str:
        """Encode a position under this ordering."""
        if self._tag is None:
            return encode_cursor(CursorPosition("" if value is None else str(value), row_id))
        return encode_cursor(CursorPosition(f"{self._tag}{'' if value is None else value}", row_id))

    def ordered(self, entries: Iterable[T]) -> list[T]:
        """Return the entries in this ordering."""
        return sorted(entries, key=self._sort_key, reverse=self.descending)

    def window(self, entries: Iterable[T], position: Position | None, size: int) -> Window[T]:
        """Return the page of `size` entries that follows `position` in this ordering."""
        ordered = self.ordered(entries)
        remaining = ordered if position is None else self._after(ordered, position)
        items = remaining[:size]
        next_cursor = None
        if len(remaining) > size and items:
            last = items[-1]
            next_cursor = self.encode(self._scalar(last), self._order.row_id(last))
        return Window(items, len(ordered), next_cursor)

    def _scalar(self, entry: T) -> Scalar:
        value = self._key.value(entry)
        if self._key.kind == "text":
            return self._empty if not value else str(value).casefold()
        return value

    def _sort_key(self, entry: T) -> tuple[tuple[int, int] | str, str, int, str]:
        order = self._order
        return (
            _rank(self._key.kind, self._scalar(entry)),
            order.name(entry).casefold(),
            order.row_id(entry),
            order.entry_id(entry),
        )

    def _after(self, ordered: list[T], position: Position) -> list[T]:
        order = self._order
        for index, entry in enumerate(ordered):
            if order.row_id(entry) == position.row_id and self._scalar(entry) == position.value:
                return ordered[index + 1 :]
        kind = self._key.kind
        target = (_rank(kind, position.value), position.row_id)
        if self.descending:
            return [e for e in ordered if (_rank(kind, self._scalar(e)), order.row_id(e)) < target]
        return [e for e in ordered if (_rank(kind, self._scalar(e)), order.row_id(e)) > target]

    def _names_a_sort(self, text: str) -> bool:
        return any(text.startswith(f"{key}.{d}:") for key in self._order.keys for d in DIRECTIONS)

    def _value_of(self, raw: SortValue) -> str | int | None:
        if self._tag is None:
            if not isinstance(raw, str) or self._names_a_sort(raw):
                raise ValidationFailed({"cursor": INVALID_CURSOR})
            return raw
        if not isinstance(raw, str) or not raw.startswith(self._tag):
            raise ValidationFailed({"cursor": INVALID_CURSOR})
        text = raw.removeprefix(self._tag)
        if self._key.kind == "text":
            return text
        if text == "":
            return None
        if _INTEGER.fullmatch(text) is None:
            raise ValidationFailed({"cursor": INVALID_CURSOR})
        return int(text)
