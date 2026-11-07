"""Columns line up across rows: per-row lists share the list tracks and tables keep widths."""

import math
import re
from dataclasses import dataclass

import pytest

from app.db.repositories.pagination import Page as ResultPage
from app.web.pagination import COLUMN_WIDTHS, DEFAULT_LIMIT, Column, PageView
from tests.web.helpers import CSS_DIR, css_rules, render_macro
from tests.web.html_tree import Node, Page
from tests.web.page_world import Key, world_pages  # noqa: F401

pytestmark = pytest.mark.xdist_group("page-world")

Rules = list[tuple[str, dict[str, str]]]

COMPONENTS = css_rules((CSS_DIR / "components.css").read_text(encoding="utf-8"))
TOKENS = (CSS_DIR / "tokens.css").read_text(encoding="utf-8")
EXEMPT_LISTS = {
    "ref-list": "items carry a variable number of inline chips and notes",
    "automation-list": "each item is a bordered card holding a description list",
    "target-names__row": "a form row holding an input and a button",
}
UNSIZED_TRACK = re.compile(r"\b(?:auto|min-content|max-content|fit-content)\b")
SPANS_THE_LIST = "1 / -1"
SEVERAL_CELLS = 2
FLEXIBLE_COLUMN_REM = 7.5
SCROLL_FLOOR_STEP_REM = 4
ACTIVITY_CELLS = 4
CAPTION = "Widgets"
CELL = "<tr><td>row</td></tr>"
BANNED_TRACKS = [
    pytest.param("auto minmax(0, 1fr)", id="auto"),
    pytest.param("minmax(0, 1fr) fit-content(50%)", id="fit-content"),
    pytest.param("min-content 1fr", id="min-content"),
    pytest.param("1fr max-content", id="max-content"),
]


@dataclass(frozen=True, slots=True)
class RowList:
    """A list of rows as the stylesheet knows it: the list's class and its item selectors."""

    list_class: str
    selectors: tuple[str, ...]


def rules_for(rules: Rules, selector: str) -> list[dict[str, str]]:
    return [
        properties
        for selectors, properties in rules
        if selector in [part.strip() for part in selectors.split(",")]
    ]


def element_children(node: Node) -> list[Node]:
    return [child for child in node.children if isinstance(child, Node)]


def row_lists(page: Page, rules: Rules) -> set[RowList]:
    found: set[RowList] = set()
    for node in page.tags("ul", "ol"):
        items = [child for child in element_children(node) if child.tag == "li"]
        item_classes = sorted(
            {name for item in items for name in item.classes() if name.endswith("__item")}
        )
        several_cells = any(len(element_children(item)) >= SEVERAL_CELLS for item in items)
        for list_class in sorted(node.classes()):
            selectors = [f".{name}" for name in item_classes]
            if rules_for(rules, f".{list_class} > li"):
                selectors.append(f".{list_class} > li")
            if selectors and several_cells and list_class not in EXEMPT_LISTS:
                found.add(RowList(list_class, tuple(selectors)))
    return found


def row_problems(rules: Rules, row_list: RowList) -> list[str]:
    list_rules = rules_for(rules, f".{row_list.list_class}")
    item_rules = [p for selector in row_list.selectors for p in rules_for(rules, selector)]
    tracks = [p["grid-template-columns"] for p in item_rules if "grid-template-columns" in p]
    problems = [] if tracks else ["its items declare no tracks of their own"]
    problems += [
        f"track list {value!r} is sized per row" for value in tracks if sized_per_row(value)
    ]
    if "subgrid" in tracks:
        if not any(p.get("display") == "grid" for p in list_rules):
            problems.append("the list is not a grid")
        if not any(p.get("grid-column") == SPANS_THE_LIST for p in item_rules):
            problems.append("a subgrid item does not span the list")
    return problems


def sized_per_row(tracks: str) -> bool:
    return tracks != "subgrid" and UNSIZED_TRACK.search(tracks) is not None


def all_row_lists(pages: dict[Key, Page]) -> set[RowList]:
    return {found for page in pages.values() for found in row_lists(page, COMPONENTS)}


def test_the_world_holds_the_activity_list_the_guard_was_written_for(
    pages: dict[Key, Page],
) -> None:
    assert "activity-list" in {found.list_class for found in all_row_lists(pages)}


def test_every_per_row_list_of_every_page_shares_the_columns_of_the_list(
    pages: dict[Key, Page],
) -> None:
    problems = {
        found.list_class: row_problems(COMPONENTS, found)
        for found in all_row_lists(pages)
        if row_problems(COMPONENTS, found)
    }

    assert problems == {}


def test_every_exemption_names_a_class_the_stylesheet_declares() -> None:
    declared = {
        name for selectors, _ in COMPONENTS for name in re.findall(r"\.([A-Za-z0-9_-]+)", selectors)
    }

    assert set(EXEMPT_LISTS) <= declared


def test_no_per_row_item_rule_of_the_stylesheet_sizes_its_tracks_per_row() -> None:
    item_rules = [
        properties
        for selectors, properties in COMPONENTS
        if "__item" in selectors or "> li" in selectors
        if "grid-template-columns" in properties
    ]

    sized = [
        p["grid-template-columns"] for p in item_rules if sized_per_row(p["grid-template-columns"])
    ]
    assert sized == []


SUBGRID_LIST: Rules = [
    (".rows", {"display": "grid", "grid-template-columns": "8rem minmax(0, 1fr)"}),
    (".rows__item", {"grid-column": "1 / -1", "grid-template-columns": "subgrid"}),
]
ROWS = RowList("rows", (".rows__item",))


def test_a_list_whose_items_are_subgrids_spanning_a_grid_list_has_no_problem() -> None:
    assert row_problems(SUBGRID_LIST, ROWS) == []


def test_items_with_fixed_tracks_have_no_problem() -> None:
    rules: Rules = [(".rows__item", {"grid-template-columns": "8rem minmax(0, 1fr)"})]

    assert row_problems(rules, ROWS) == []


@pytest.mark.parametrize("tracks", BANNED_TRACKS)
def test_items_whose_tracks_are_sized_per_row_are_found(tracks: str) -> None:
    rules: Rules = [(".rows__item", {"grid-template-columns": tracks})]

    assert row_problems(rules, ROWS) == [f"track list {tracks!r} is sized per row"]


def test_flex_items_declare_no_tracks_and_are_found() -> None:
    rules: Rules = [(".rows__item", {"display": "flex"})]

    assert row_problems(rules, ROWS) == ["its items declare no tracks of their own"]


def test_a_subgrid_item_that_does_not_span_its_list_is_found() -> None:
    rules: Rules = [
        (".rows", {"display": "grid"}),
        (".rows__item", {"grid-template-columns": "subgrid"}),
    ]

    assert row_problems(rules, ROWS) == ["a subgrid item does not span the list"]


def test_a_subgrid_item_in_a_list_that_is_not_a_grid_is_found() -> None:
    rules: Rules = [
        (".rows", {"display": "block"}),
        (".rows__item", {"grid-column": "1 / -1", "grid-template-columns": "subgrid"}),
    ]

    assert row_problems(rules, ROWS) == ["the list is not a grid"]


def test_the_collector_finds_a_list_by_its_item_class_and_by_a_child_rule() -> None:
    page = Page(
        200,
        '<ul class="rows"><li class="rows__item"><a>a</a><b>b</b></li></ul>'
        '<ul class="bare"><li><a>a</a><b>b</b></li></ul>',
    )
    rules: Rules = [*SUBGRID_LIST, (".bare > li", {"display": "grid"})]

    assert row_lists(page, rules) == {
        RowList("rows", (".rows__item",)),
        RowList("bare", (".bare > li",)),
    }


def test_the_collector_leaves_out_single_cell_items_and_the_exempt_lists() -> None:
    page = Page(
        200,
        '<ul class="tiles"><li class="tiles__item"><a>a</a></li></ul>'
        '<ul class="ref-list"><li class="ref-list__item"><a>a</a><b>b</b></li></ul>',
    )

    assert row_lists(page, SUBGRID_LIST) == set()


def activity_lists(pages: dict[Key, Page]) -> list[Node]:
    return [
        node
        for page in pages.values()
        for node in page.tags("ul")
        if "activity-list" in node.classes()
    ]


def track_count(tracks: str) -> int:
    return len(re.findall(r"[\w.-]+(?:\([^)]*\))?", tracks))


def test_the_activity_list_has_a_track_for_each_of_the_four_cells_of_an_item() -> None:
    base = rules_for(COMPONENTS, ".activity-list")[0]

    assert track_count(base["grid-template-columns"]) == ACTIVITY_CELLS


def test_each_item_of_an_activity_panel_is_a_row_of_time_action_actor_and_result(
    pages: dict[Key, Page],
) -> None:
    lists = activity_lists(pages)
    items = [item for node in lists for item in element_children(node)]

    assert lists != []
    assert {item.tag for item in items} == {"li"}
    assert {"activity-list__item" in item.classes() for item in items} == {True}
    assert {len(element_children(item)) for item in items} == {ACTIVITY_CELLS}
    assert [[cell.tag for cell in element_children(item)] for item in items[:1]] == [
        ["a", "span", "div", "span"]
    ]


def table_page(columns: list[Column]) -> Page:
    pager = PageView.of(
        ResultPage(items=["row"], next_cursor=None), base_path="/widgets", limit=DEFAULT_LIMIT
    )
    html = render_macro(
        "data_table",
        "data_table",
        table_id="t",
        caption=CAPTION,
        columns=columns,
        pager=pager,
        caller=lambda: CELL,
    )
    return Page(200, html)


def test_a_table_with_widths_gets_a_colgroup_with_a_class_per_width_and_the_fixed_layout() -> None:
    page = table_page(
        [
            Column("select", "Select"),
            Column("name", "Name", sortable=True),
            Column("state", "State", width="badge"),
            Column("actions", "Actions", width="actions-wide"),
        ]
    )

    table = next(page.tags("table"))
    columns = [node.attrs.get("class") for node in page.tags("col")]
    assert "table--fixed" in table.classes()
    assert columns == ["col--select", None, "col--badge", "col--actions-wide"]
    assert next(page.tags("colgroup")) is not None


def test_a_table_without_widths_has_no_colgroup_and_keeps_the_automatic_layout() -> None:
    page = table_page([Column("name", "Name", sortable=True), Column("size", "Size")])

    table = next(page.tags("table"))
    assert "table--fixed" not in table.classes()
    assert "data-min" not in table.attrs
    assert list(page.tags("colgroup")) == []


def test_a_column_with_a_width_the_stylesheet_does_not_know_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown column width"):
        Column("name", "Name", width="wide")  # type: ignore[arg-type]


def token_rem(width: str) -> float:
    found = re.search(rf"--col-{width}:\s*([0-9.]+)rem;", TOKENS)
    assert found is not None
    return float(found.group(1))


@pytest.mark.parametrize("width", COLUMN_WIDTHS)
def test_every_column_width_has_a_token_and_a_column_class_that_applies_it(width: str) -> None:
    [rule] = rules_for(COMPONENTS, f".col--{width}")

    assert token_rem(width) > 0
    assert rule == {"width": f"var(--col-{width})"}


def test_no_rule_lets_a_table_header_wrap() -> None:
    header_rules = [
        properties
        for selectors, properties in COMPONENTS
        if "white-space" in properties
        and any(part.strip().endswith("thead th") for part in selectors.split(","))
    ]

    assert header_rules
    assert all(rule["white-space"] == "nowrap" for rule in header_rules)


def test_the_scroll_floor_of_a_fixed_table_sums_its_width_tokens_and_a_flexible_column() -> None:
    columns = [Column(f"c{width}", width, width=width) for width in COLUMN_WIDTHS]  # type: ignore[arg-type]
    page = table_page([*columns, Column("name", "Name")])

    expected = sum(token_rem(width) for width in COLUMN_WIDTHS) + FLEXIBLE_COLUMN_REM
    floor = math.ceil(expected / SCROLL_FLOOR_STEP_REM) * SCROLL_FLOOR_STEP_REM
    assert next(page.tags("table")).attrs["data-min"] == str(floor)


def test_every_scroll_floor_a_page_of_the_world_emits_above_the_first_step_has_a_rule(
    pages: dict[Key, Page],
) -> None:
    emitted = {
        table.attrs["data-min"]
        for page in pages.values()
        for table in page.tags("table")
        if "table--fixed" in table.classes() and "data-min" in table.attrs
    }
    declared = {
        found
        for selectors, _ in COMPONENTS
        for found in re.findall(r'\.table--fixed\[data-min="(\d+)"\]', selectors)
    }

    assert emitted != set()
    assert {floor for floor in emitted if int(floor) >= int(min(declared, key=int))} <= declared


def test_the_fixed_layout_is_set_on_fixed_tables_only() -> None:
    layouts = [
        selectors
        for selectors, properties in COMPONENTS
        if properties.get("table-layout") == "fixed"
    ]

    assert layouts == [".table--fixed"]


def test_every_fixed_table_of_the_world_has_a_colgroup_with_a_column_per_heading(
    pages: dict[Key, Page],
) -> None:
    mismatched = [
        str(key)
        for key, page in pages.items()
        for table in page.tags("table")
        if "table--fixed" in table.classes() and not columns_match(table)
    ]

    assert mismatched == []


def columns_match(table: Node) -> bool:
    groups = [n for n in table.walk() if n.tag == "colgroup"]
    headings = [n for n in table.walk() if n.tag == "th" and n.attrs.get("scope") == "col"]
    return len(groups) == 1 and len([n for n in groups[0].walk() if n.tag == "col"]) == len(
        headings
    )


def test_a_stack_grid_is_one_column_at_a_phone_width_and_three_across_at_a_desktop_width() -> None:
    columns = [p["grid-template-columns"] for p in rules_for(COMPONENTS, ".stack-grid")]

    assert columns == [
        "minmax(0, 1fr)",
        "repeat(2, minmax(0, 1fr))",
        "repeat(3, minmax(0, 1fr))",
    ]
