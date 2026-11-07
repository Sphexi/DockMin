"""The partials shared by the network and volume pages: user lists, kind badges and key rows."""

import re
from html import unescape
from types import SimpleNamespace
from typing import Any

import pytest

from tests.web.helpers import render_macro

LINK_BASE = "/containers"


def text_of(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def users(*names: str) -> list[Any]:
    return [SimpleNamespace(id=f"id{i}", name=name) for i, name in enumerate(names)]


@pytest.mark.parametrize(
    ("more", "hidden", "expected"),
    [
        (3, 0, "and 3 more"),
        (0, 1, "1 container you cannot see"),
        (0, 4, "4 containers you cannot see"),
        (3, 2, "and 3 more; 2 containers you cannot see"),
    ],
)
def test_the_cut_and_the_hidden_counts_are_worded_apart(
    more: int, hidden: int, expected: str
) -> None:
    html = render_macro(
        "object_users",
        "object_users",
        rows=users("alpha"),
        more=more,
        hidden=hidden,
        total=more + hidden + 1,
        link_base=LINK_BASE,
    )

    assert expected in text_of(html) and "alpha" in html


def test_a_list_with_nobody_visible_names_no_one_and_says_how_many_are_hidden() -> None:
    html = render_macro(
        "object_users", "object_users", rows=[], more=0, hidden=1, total=1, link_base=LINK_BASE
    )

    assert "1 container you cannot see" in html and "<ul" not in html


def test_an_unused_object_says_so() -> None:
    html = render_macro(
        "object_users", "object_users", rows=[], more=0, hidden=0, total=0, link_base=LINK_BASE
    )

    assert "Not used" in html and "<ul" not in html


def test_a_users_name_is_escaped_and_linked_to_its_own_id() -> None:
    html = render_macro(
        "object_users",
        "object_users",
        rows=users("<svg onload=alert(1)>"),
        more=0,
        hidden=0,
        total=1,
        link_base=LINK_BASE,
        suffix="?tab=networks",
    )

    assert "<svg" not in html and f'href="{LINK_BASE}/id0?tab=networks"' in html


@pytest.mark.parametrize("kind", ["predefined", "system", "<script>", ""])
def test_the_badge_kind_picks_a_tone_and_never_reaches_the_markup_as_text(kind: str) -> None:
    html = render_macro("network_badge", "network_badge", label="Predefined", kind=kind)

    assert "<script>" not in html and 'data-tone="' in html


def test_a_single_error_message_and_a_mapping_of_them_each_render_as_list_items() -> None:
    single = render_macro("error_summary", "error_summary", errors="one problem")
    several = render_macro("error_summary", "error_summary", errors={"a": "first", "b": "second"})

    assert "<li>one problem</li>" in single
    assert "<li>first</li>" in several and "<li>second</li>" in several


def test_key_value_rows_render_the_requested_count_the_errors_and_a_blank_template() -> None:
    html = render_macro(
        "key_value_rows",
        "key_value_rows",
        prefix="option",
        legend="Options",
        subject="Option",
        help=None,
        count=2,
        cap=5,
        values={"option-0-key": "k"},
        errors={"option-1-value": "oops"},
        submit_href="/x",
    )

    assert 'value="k"' in html and "oops" in html
    assert html.count('data-repeat-row="') == 3
    assert 'data-repeat-cap="5"' in html and 'formaction="/x#group-option"' in html
