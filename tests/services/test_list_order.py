"""The shared ordering of the object lists: sort input, empty values, ties and cursors."""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.auth.errors import ValidationFailed
from app.db.repositories.pagination import CursorPosition, encode_cursor
from app.services.list_order import (
    ListOrder,
    Ordering,
    moment_key,
    number_key,
    rank_key,
    text_key,
)

CURSOR_MESSAGE = "The page link is not valid."
EMPTY_LAST = "~"
PAGE_SIZE = 3


@dataclass(frozen=True)
class Item:
    name: str
    kind: str | None
    size: int | None
    ident: int


ITEMS = [
    Item("beta", "Zed", 5, 1),
    Item("Alpha", None, None, 2),
    Item("gamma", "alpha", 5, 3),
    Item("delta", "", 1, 4),
    Item("epsilon", "Zed", None, 5),
    Item("zeta", "alpha", 9, 6),
    Item("eta", None, 5, 7),
    Item("theta", "Zed", 1, 8),
]

ORDER = ListOrder[Item](
    default="name",
    keys={
        "name": text_key(lambda i: i.name),
        "kind": text_key(lambda i: i.kind),
        "size": number_key(lambda i: i.size),
    },
    name=lambda i: i.name,
    row_id=lambda i: i.ident,
    entry_id=lambda i: str(i.ident),
)
EMPTY_LAST_ORDER = ListOrder[Item](
    default="kind",
    keys={"kind": text_key(lambda i: i.kind)},
    name=lambda i: i.name,
    row_id=lambda i: i.ident,
    entry_id=lambda i: str(i.ident),
    unsorted_empty=EMPTY_LAST,
)


def idents(items: list[Item]) -> list[int]:
    return [i.ident for i in items]


def walk(ordering: Ordering[Item], entries: list[Item], size: int) -> list[list[Item]]:
    pages: list[list[Item]] = []
    cursor = None
    while True:
        window = ordering.window(entries, ordering.position(cursor), size)
        pages.append(window.items)
        if window.next_cursor is None:
            return pages
        cursor = window.next_cursor


def ordering_of(sort: str | None, direction: str | None = None) -> Ordering[Item]:
    return Ordering.resolve(ORDER, sort, direction)


@pytest.mark.parametrize(
    ("sort", "direction", "fields"),
    [
        ("bogus", None, {"sort"}),
        (None, "sideways", {"dir"}),
        ("bogus", "up", {"sort", "dir"}),
        ("NAME", None, {"sort"}),
    ],
)
def test_resolve_refuses_an_unknown_sort_or_direction_naming_each_field(
    sort: str | None, direction: str | None, fields: set[str]
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        ordering_of(sort, direction)

    assert set(refused.value.fields) == fields


@pytest.mark.parametrize(
    ("sort", "direction", "key", "expected"),
    [
        (None, None, "name", "asc"),
        ("", "", "name", "asc"),
        (None, "desc", "name", "asc"),
        ("size", None, "size", "asc"),
        ("size", "desc", "size", "desc"),
    ],
)
def test_resolve_defaults_to_the_default_key_ascending_and_ignores_a_direction_without_a_key(
    sort: str | None, direction: str | None, key: str, expected: str
) -> None:
    ordering = ordering_of(sort, direction)

    assert (ordering.key, ordering.direction) == (key, expected)


@pytest.mark.parametrize(
    ("sort", "direction", "expected"),
    [
        ("kind", "asc", [2, 4, 7, 3, 6, 1, 5, 8]),
        ("kind", "desc", [8, 5, 1, 6, 3, 7, 4, 2]),
        ("size", "asc", [2, 5, 4, 8, 1, 7, 3, 6]),
        ("size", "desc", [6, 3, 7, 1, 8, 4, 5, 2]),
        ("name", "asc", [2, 1, 4, 5, 7, 3, 8, 6]),
        ("name", "desc", [6, 8, 3, 7, 5, 4, 1, 2]),
        (None, None, [2, 1, 4, 5, 7, 3, 8, 6]),
    ],
)
def test_ordered_puts_empty_values_lowest_folds_case_and_breaks_ties_in_the_sort_direction(
    sort: str, direction: str, expected: list[int]
) -> None:
    ordered = ordering_of(sort, direction).ordered(ITEMS)

    assert idents(ordered) == expected


def test_an_unsorted_request_may_place_empty_values_last_while_a_named_sort_places_them_first() -> (
    None
):
    unsorted = Ordering.resolve(EMPTY_LAST_ORDER, None, None).ordered(ITEMS)
    named = Ordering.resolve(EMPTY_LAST_ORDER, "kind", "asc").ordered(ITEMS)

    assert idents(unsorted)[-3:] == [2, 4, 7]
    assert idents(named)[:3] == [2, 4, 7]


@pytest.mark.parametrize(
    ("order", "values", "expected"),
    [
        (("running", "paused"), ["paused", "running", "dead", None], [2, 1, None, None]),
        ((), ["running"], [None]),
    ],
)
def test_rank_key_places_listed_values_by_position_and_any_other_value_or_none_as_empty(
    order: tuple[str, ...], values: list[str | None], expected: list[int | None]
) -> None:
    place = rank_key(order)

    assert [place(value) for value in values] == expected


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (None, None),
        (datetime(1970, 1, 1, tzinfo=UTC), 0),
        (datetime(1970, 1, 1, 0, 0, 1, 5, tzinfo=UTC), 1_000_005),
    ],
)
def test_moment_key_counts_microseconds_since_the_epoch(
    moment: datetime | None, expected: int | None
) -> None:
    assert moment_key(moment) == expected


@pytest.mark.parametrize("direction", ["asc", "desc"])
@pytest.mark.parametrize("sort", [None, "name", "kind", "size"])
@pytest.mark.parametrize("size", [1, PAGE_SIZE, len(ITEMS) - 1, len(ITEMS)])
def test_walking_the_cursors_visits_every_entry_once_in_the_order_of_one_big_page(
    sort: str | None, direction: str, size: int
) -> None:
    ordering = ordering_of(sort, direction)
    whole = ordering.window(ITEMS, None, len(ITEMS))

    pages = walk(ordering, ITEMS, size)

    assert [i for page in pages for i in page] == whole.items
    assert whole.total == len(ITEMS) and whole.next_cursor is None


def test_a_page_reports_the_total_and_a_cursor_only_when_entries_remain() -> None:
    ordering = ordering_of("size", "desc")

    first = ordering.window(ITEMS, None, PAGE_SIZE)
    last = ordering.window(ITEMS, None, len(ITEMS))

    assert (first.total, len(first.items), first.next_cursor is not None) == (
        len(ITEMS),
        PAGE_SIZE,
        True,
    )
    assert last.next_cursor is None


def test_a_cursor_whose_entry_has_gone_resumes_past_its_sort_value() -> None:
    ordering = ordering_of("name", "asc")
    cursor = ordering.window(ITEMS, None, 2).next_cursor
    remaining = [i for i in ITEMS if i.ident != 1]

    resumed = ordering.window(remaining, ordering.position(cursor), PAGE_SIZE)

    assert idents(resumed.items) == [4, 5, 7]


TIED = [Item("bravo", "x", 1, 1), Item("alpha", "x", 1, 2), Item("charlie", "x", 1, 3)]


@pytest.mark.parametrize(
    ("direction", "size", "gone", "expected"),
    [("asc", 1, 2, [1, 3]), ("desc", 2, 1, [2])],
)
def test_a_cursor_whose_entry_has_gone_resumes_in_name_order_among_equal_sort_values(
    direction: str, size: int, gone: int, expected: list[int]
) -> None:
    ordering = ordering_of("kind", direction)
    cursor = ordering.window(TIED, None, size).next_cursor
    remaining = [i for i in TIED if i.ident != gone]

    resumed = ordering.window(remaining, ordering.position(cursor), PAGE_SIZE)

    assert idents(resumed.items) == expected


def test_an_unsorted_cursor_whose_value_reads_like_a_sort_tag_still_pages() -> None:
    lookalike = Item("size.desc:5000/x", None, None, 9)
    entries = [lookalike, *ITEMS]

    pages = walk(ordering_of(None), entries, 1)

    assert [i for page in pages for i in page] == ordering_of(None).ordered(entries)


def test_a_cursor_minted_before_sorting_existed_pages_the_unsorted_list() -> None:
    old = encode_cursor(CursorPosition("beta", 1))
    ordering = ordering_of(None)

    resumed = ordering.window(ITEMS, ordering.position(old), PAGE_SIZE)

    assert idents(resumed.items) == [4, 5, 7]


def test_an_old_cursor_that_carries_the_unsorted_empty_value_still_pages() -> None:
    old = encode_cursor(CursorPosition(EMPTY_LAST, 2))
    ordering = Ordering.resolve(EMPTY_LAST_ORDER, None, None)

    resumed = ordering.window(ITEMS, ordering.position(old), PAGE_SIZE)

    assert idents(resumed.items) == [4, 7]


@pytest.mark.parametrize(
    ("minted", "used"),
    [
        (("name", "asc"), ("kind", "asc")),
        (("kind", "asc"), ("name", "asc")),
        (("size", "asc"), ("size", "desc")),
        (("size", "desc"), ("size", "asc")),
        (("name", "asc"), (None, None)),
        ((None, None), ("name", "asc")),
        (("size", "asc"), ("name", "asc")),
    ],
)
def test_a_cursor_minted_under_another_sort_or_direction_is_refused(
    minted: tuple[str | None, str | None], used: tuple[str | None, str | None]
) -> None:
    cursor = ordering_of(*minted).window(ITEMS, None, 1).next_cursor

    with pytest.raises(ValidationFailed) as refused:
        ordering_of(*used).position(cursor)

    assert refused.value.fields == {"cursor": CURSOR_MESSAGE}


@pytest.mark.parametrize(
    ("sort", "token"),
    [
        (None, "not-a-cursor"),
        (None, encode_cursor(CursorPosition(7, 1))),
        (None, encode_cursor(CursorPosition(1.5, 1))),
        ("size", encode_cursor(CursorPosition(7, 1))),
        ("size", encode_cursor(CursorPosition("size.asc:seven", 1))),
        ("size", encode_cursor(CursorPosition("size.asc:99999999999999999999", 1))),
        ("name", encode_cursor(CursorPosition("beta", 1))),
    ],
)
def test_a_cursor_of_the_wrong_type_or_form_is_refused_never_raised_as_another_error(
    sort: str | None, token: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        ordering_of(sort).position(token)

    assert refused.value.fields == {"cursor": CURSOR_MESSAGE}


def test_a_default_key_that_is_not_text_is_refused_when_the_table_is_built() -> None:
    with pytest.raises(ValueError, match="default key"):
        ListOrder[Item](
            default="size",
            keys={"size": number_key(lambda i: i.size)},
            name=lambda i: i.name,
            row_id=lambda i: i.ident,
            entry_id=lambda i: str(i.ident),
        )
