"""Structure every rendered page keeps: headings, landmarks, names, labels, tables and times."""

from collections.abc import Callable

import pytest

from tests.web.html_tree import (
    TIMESTAMP_TEXT,
    Node,
    Page,
    accessible_name,
    heading_levels,
    is_aria_hidden,
    is_labelled_control,
    labelled_controls,
    text_nodes,
)
from tests.web.page_world import PAGES, ROLES, Key, world_pages  # noqa: F401

pytestmark = pytest.mark.xdist_group("page-world")

SECTIONING = frozenset({"main", "article", "section", "aside", "nav", "dialog", "form"})
NAMED_ROLES = frozenset({"button"})
ALWAYS_EMPTY = [
    pytest.param("admin", "tokens", id="admin-tokens"),
    pytest.param("admin", "object-storage-list", id="admin-object-storage"),
    pytest.param("admin", "backup-list", id="admin-backups"),
    pytest.param("standard", "team-list", id="standard-teams"),
]


def offenders(pages: dict[Key, Page], problem: Callable[[Page], list[str]]) -> list[str]:
    return [
        f"{role}/{label}: {text}" for (role, label), page in pages.items() for text in problem(page)
    ]


def _headline(page: Page) -> list[str]:
    count = len(list(page.tags("h1")))
    return [] if count == 1 else [f"{count} h1 elements"]


def _heading_order(page: Page) -> list[str]:
    levels = heading_levels(page.root)
    steps = zip([0, *levels], levels, strict=False)
    return [f"h{later} follows h{earlier}" for earlier, later in steps if later > earlier + 1]


def _landmarks(page: Page) -> list[str]:
    def top_level(tag: str) -> int:
        return sum(1 for n in page.tags(tag) if not any(a.tag in SECTIONING for a in n.ancestors()))

    counts = {
        "main": len(list(page.tags("main"))),
        "banner": top_level("header"),
        "footer": top_level("footer"),
    }
    return [f"{count} {name} landmarks" for name, count in counts.items() if count != 1]


def _navs(page: Page) -> list[str]:
    labels = [n.attrs.get("aria-label", "").strip() for n in page.tags("nav")]
    unlabelled = ["nav without a label" for label in labels if not label]
    repeated = [
        f"nav label {label!r} repeated"
        for label in set(labels)
        if label and labels.count(label) > 1
    ]
    return unlabelled + repeated


def _controls(page: Page) -> list[str]:
    return [
        f"{n.tag}[name={n.attrs.get('name')}] has no label"
        for n in labelled_controls(page.root)
        if not is_labelled_control(n, page.root, page.ids)
    ]


def _names(page: Page) -> list[str]:
    targets = [
        n
        for n in page.nodes
        if n.tag == "button"
        or (n.tag == "a" and "href" in n.attrs)
        or n.attrs.get("role") == "button"
    ]
    targets = [n for n in targets if not is_aria_hidden(n)]
    return [
        f"{n.tag} {n.attrs.get('href') or n.attrs.get('class', '')} has no accessible name"
        for n in targets
        if not accessible_name(n, page.ids)
    ]


def _header_cells(page: Page) -> list[str]:
    return [f"th {n.text()!r} has no scope" for n in page.tags("th") if not n.attrs.get("scope")]


def _captions(page: Page) -> list[str]:
    return [
        "table without a caption"
        for table in page.tags("table")
        if not any(
            isinstance(c, Node) and c.tag == "caption" and c.text(skip_hidden=False)
            for c in table.children
        )
    ]


def _badges(page: Page) -> list[str]:
    return [
        f"badge {n.attrs.get('data-tone')} has no text"
        for n in page.nodes
        if "badge" in n.classes() and not n.text()
    ]


def _empty_lists(page: Page) -> list[str]:
    containers = (n for n in page.nodes if "data-table" in n.classes())
    return [
        f"list {n.attrs.get('id')} shows neither rows nor an empty state"
        for n in containers
        if not any(d.tag == "table" or "state--empty" in d.classes() for d in n.walk())
    ]


def _empty_states_name_their_next_step(page: Page) -> list[str]:
    states = (n for n in page.nodes if "state--empty" in n.classes())
    return [
        "empty state without a title"
        for n in states
        if not any("state__title" in d.classes() and d.text() for d in n.walk())
    ]


def _document(page: Page) -> list[str]:
    icons = [n for n in page.tags("link") if n.attrs.get("rel") == "icon" and n.attrs.get("href")]
    html = next(page.tags("html"), None)
    titles = [n for n in page.tags("title") if n.text()]
    problems = [] if len(icons) == 1 else [f"{len(icons)} icon links"]
    problems += [] if html is not None and html.attrs.get("lang") else ["no lang on html"]
    return problems + ([] if titles else ["no title"])


def _timestamps(page: Page) -> list[str]:
    found = []
    for text, owner in text_nodes(page.root):
        if TIMESTAMP_TEXT.search(text) and not any(
            a.tag == "time" and a.attrs.get("datetime") for a in (owner, *owner.ancestors())
        ):
            found.append(f"{text.strip()!r} outside <time datetime>")
    return found


def _references(page: Page) -> list[str]:
    missing = []
    for node in page.nodes:
        for attribute in ("aria-labelledby", "aria-describedby", "aria-controls"):
            for target in node.attrs.get(attribute, "").split():
                if target not in page.ids:
                    missing.append(f"{node.tag}[{attribute}] names absent id {target}")
    return missing


def _duplicate_ids(page: Page) -> list[str]:
    seen: set[str] = set()
    repeated = []
    for node in page.nodes:
        identifier = node.attrs.get("id")
        if identifier and identifier in seen:
            repeated.append(f"id {identifier} used twice")
        seen.add(identifier or "")
    return repeated


def test_the_world_renders_a_page_of_every_kind_for_both_roles(
    pages: dict[Key, Page],
) -> None:
    expected = {(role, label) for role in ROLES for label in PAGES}

    assert set(pages) == expected
    assert {page.status for page in pages.values()} <= {200, 403, 404}


@pytest.mark.parametrize(
    "tag",
    ["table", "th", "caption", "nav", "input", "select", "button", "time", "dialog", "h2"],
)
def test_the_rendered_pages_hold_each_element_the_checks_look_at(
    pages: dict[Key, Page], tag: str
) -> None:
    assert sum(len(list(page.tags(tag))) for page in pages.values()) > 0


@pytest.mark.parametrize(
    "check",
    [
        pytest.param(_headline, id="one-h1"),
        pytest.param(_heading_order, id="heading-order"),
        pytest.param(_landmarks, id="landmarks"),
        pytest.param(_navs, id="navigation-labels"),
        pytest.param(_controls, id="control-labels"),
        pytest.param(_names, id="button-and-link-names"),
        pytest.param(_header_cells, id="header-cell-scope"),
        pytest.param(_captions, id="table-captions"),
        pytest.param(_badges, id="badge-text"),
        pytest.param(_empty_lists, id="list-empty-states"),
        pytest.param(_empty_states_name_their_next_step, id="empty-state-titles"),
        pytest.param(_document, id="document-head"),
        pytest.param(_timestamps, id="timestamps-in-time-elements"),
        pytest.param(_references, id="aria-references"),
        pytest.param(_duplicate_ids, id="unique-ids"),
    ],
)
def test_every_rendered_page_keeps_the_structure(
    pages: dict[Key, Page], check: Callable[[Page], list[str]]
) -> None:
    assert offenders(pages, check) == []


@pytest.mark.parametrize(("role", "label"), ALWAYS_EMPTY)
def test_a_list_with_nothing_in_it_renders_an_empty_state(
    pages: dict[Key, Page], role: str, label: str
) -> None:
    page = pages[(role, label)]

    assert any("state--empty" in n.classes() for n in page.nodes)


HEAD = '<html lang="en"><head><title>t</title><link rel="icon" href="/i.svg"></head>'
GOOD_DOCUMENT = HEAD + (
    '<body><header><nav aria-label="Primary"><a href="/">Home</a></nav></header>'
    "<main><h1>Title</h1><h2>Part</h2>"
    '<label for="q">Search</label><input id="q" name="q">'
    '<div class="data-table" id="t"><table><caption>Rows</caption>'
    '<tr><th scope="col">Name</th></tr></table></div>'
    '<span class="badge">Running</span>'
    '<time datetime="2030-01-01T00:00:00+00:00">2030-01-01 00:00 UTC</time>'
    '<button type="button" aria-label="Close">x</button></main><footer>f</footer></body></html>'
)
BAD_PAGES = [
    pytest.param(_headline, GOOD_DOCUMENT.replace("<h1>Title</h1>", ""), id="no-h1"),
    pytest.param(_headline, GOOD_DOCUMENT.replace("<h2>", "<h1>"), id="two-h1"),
    pytest.param(
        _heading_order, GOOD_DOCUMENT.replace("<h2>Part</h2>", "<h3>Part</h3>"), id="skip"
    ),
    pytest.param(_landmarks, GOOD_DOCUMENT.replace("<main>", "<main></main><main>"), id="two-main"),
    pytest.param(_navs, GOOD_DOCUMENT.replace(' aria-label="Primary"', ""), id="nav-unlabelled"),
    pytest.param(
        _controls, GOOD_DOCUMENT.replace('<label for="q">Search</label>', ""), id="input-unlabelled"
    ),
    pytest.param(
        _names, GOOD_DOCUMENT.replace(' aria-label="Close">x<', "><"), id="button-without-name"
    ),
    pytest.param(_names, GOOD_DOCUMENT.replace(">Home<", "><"), id="link-without-name"),
    pytest.param(_header_cells, GOOD_DOCUMENT.replace(' scope="col"', ""), id="th-without-scope"),
    pytest.param(_captions, GOOD_DOCUMENT.replace("<caption>Rows</caption>", ""), id="no-caption"),
    pytest.param(_badges, GOOD_DOCUMENT.replace(">Running<", "><"), id="empty-badge"),
    pytest.param(
        _empty_lists,
        GOOD_DOCUMENT.replace(
            '<table><caption>Rows</caption><tr><th scope="col">Name</th></tr></table>', ""
        ),
        id="list-without-rows-or-state",
    ),
    pytest.param(
        _document, GOOD_DOCUMENT.replace('<link rel="icon" href="/i.svg">', ""), id="no-icon"
    ),
    pytest.param(
        _timestamps,
        GOOD_DOCUMENT.replace("<time", "<span").replace("</time>", "</span>"),
        id="bare-timestamp",
    ),
    pytest.param(
        _references, GOOD_DOCUMENT.replace("<main>", '<main aria-labelledby="gone">'), id="dangling"
    ),
    pytest.param(
        _duplicate_ids, GOOD_DOCUMENT.replace("<main>", '<main id="q">'), id="duplicate-id"
    ),
]


def _page(document: str) -> Page:
    return Page(200, document)


@pytest.mark.parametrize("check", list(dict.fromkeys(param.values[0] for param in BAD_PAGES)))
def test_the_checks_accept_well_formed_markup(check: Callable[[Page], list[str]]) -> None:
    assert check(_page(GOOD_DOCUMENT)) == []


@pytest.mark.parametrize(("check", "document"), BAD_PAGES)
def test_the_checks_report_markup_that_breaks_their_rule(
    check: Callable[[Page], list[str]], document: str
) -> None:
    assert check(_page(document)) != []
