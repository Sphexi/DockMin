"""Fields laid out in a row start with their label and help, so the controls align."""

import pytest

from tests.support import present
from tests.web.html_tree import Node, Page
from tests.web.page_world import PAGES, Key, world_pages  # noqa: F401

pytestmark = pytest.mark.xdist_group("page-world")

MULTI_COLUMN = frozenset({"create-section__body", "filters", "form-pair", "address-grid"})


def first_element(node: Node) -> Node | None:
    return next((child for child in node.children if isinstance(child, Node)), None)


def fields_in_rows(page: Page) -> list[Node]:
    return [
        node
        for node in page.nodes
        if "field" in node.classes()
        and any(MULTI_COLUMN & ancestor.classes() for ancestor in node.ancestors())
    ]


def test_every_field_in_a_row_container_starts_with_its_head(pages: dict[Key, Page]) -> None:
    offenders = []
    for (role, label), page in pages.items():
        for field in fields_in_rows(page):
            first = first_element(field)
            if first is None or "field__head" not in first.classes():
                name = next((n.attrs.get("name") for n in field.walk() if n.attrs.get("name")), "?")
                offenders.append(
                    f"{role}/{label}: field {name!r} starts with {first and first.tag}"
                )

    assert offenders == []


def test_the_pages_checked_include_every_row_container(pages: dict[Key, Page]) -> None:
    seen = {
        name
        for page in pages.values()
        for field in fields_in_rows(page)
        for ancestor in field.ancestors()
        for name in MULTI_COLUMN & ancestor.classes()
    }

    assert seen == MULTI_COLUMN


def test_a_field_that_starts_with_its_label_instead_of_a_head_is_found() -> None:
    page = Page(
        200,
        '<div class="form-pair">'
        '<div class="field"><label for="a">A</label><input id="a"></div>'
        '<div class="field"><div class="field__head"><label for="b">B</label></div>'
        '<input id="b"></div></div>',
    )

    starts = [present(first_element(field)).attrs.get("class") for field in fields_in_rows(page)]

    assert starts == [None, "field__head"]
