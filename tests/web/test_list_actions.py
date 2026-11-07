"""List pages put their count and object actions in one bar above the list; the header navigates."""

import re

import pytest
from httpx import AsyncClient

from tests.web.environment_support import World
from tests.web.html_tree import Node, Page
from tests.web.page_world import ROLES, Key, world_pages  # noqa: F401

pytestmark = pytest.mark.xdist_group("page-world")

OK = 200
HTMX = {"HX-Request": "true"}
ENVIRONMENT_LISTS = (
    "image-list",
    "stack-list",
    "container-list",
    "volume-list",
    "network-list",
    "swarm-service-list",
    "swarm-config-list",
    "swarm-secret-list",
    "swarm-node-list",
    "swarm-stack-list",
)
GLOBAL_LISTS = (
    "dashboard",
    "team-list",
    "user-list",
    "group-list",
    "registry-list",
    "object-storage-list",
    "automation-overview",
)
COUNT_ONLY = "swarm-node-list"
FOLDED_FILTERS = ("stack-list", "swarm-stack-list")
LISTS = ENVIRONMENT_LISTS + GLOBAL_LISTS
COUNT = re.compile(r"\d+ [a-z]+\.")
NAVIGATION = "Back to"


def bar_of(page: Page) -> Node | None:
    bars = [node for node in page.tags("div") if "list-bar" in node.classes()]
    return bars[0] if bars else None


def controls_in(node: Node, class_name: str) -> list[str]:
    return [
        control.text()
        for box in node.walk()
        if class_name in box.classes()
        for control in box.walk()
        if control.tag in ("a", "button")
    ]


def header_controls(page: Page) -> list[str]:
    return [
        control.text()
        for box in page.tags("div")
        if "page-header__actions" in box.classes()
        for control in box.walk()
        if control.tag in ("a", "button")
    ]


def out_of_band_count(html: str) -> str | None:
    found = re.search(r'<p id="results-summary"[^>]*hx-swap-oob="innerHTML"[^>]*>([^<]*)</p>', html)
    return None if found is None else found.group(1).strip()


def answered(pages: dict[Key, Page], role: str) -> list[str]:
    return [label for label in LISTS if pages[(role, label)].status == OK]


def opens_with_count(page: Page) -> bool:
    bar = bar_of(page)
    return bar is not None and COUNT.match(bar.text()) is not None


def is_misplaced(page: Page) -> bool:
    bar = bar_of(page)
    assert bar is not None
    order = page.nodes.index(bar)
    header = next(n for n in page.nodes if "page-header" in n.classes())
    tables = [page.nodes.index(n) for n in page.nodes if n.tag == "table"]
    return page.nodes.index(header) > order or any(index < order for index in tables)


@pytest.mark.parametrize("role", ROLES)
def test_every_list_page_opens_its_bar_with_the_count(pages: dict[Key, Page], role: str) -> None:
    found = {label: opens_with_count(pages[(role, label)]) for label in answered(pages, role)}

    assert found == dict.fromkeys(answered(pages, role), True)


@pytest.mark.parametrize("role", ROLES)
def test_the_count_in_the_bar_is_the_results_line_the_filter_swaps_replace(
    pages: dict[Key, Page], role: str
) -> None:
    summaries = {
        label: [
            node.attrs.get("role")
            for node in pages[(role, label)].tags("p")
            if node.attrs.get("id") == "results-summary"
        ]
        for label in answered(pages, role)
    }

    assert summaries == {label: ["status"] for label in answered(pages, role)}


@pytest.mark.parametrize("label", [name for name in LISTS if name != COUNT_ONLY])
def test_an_administrators_object_actions_are_in_the_bar_and_not_in_the_header(
    pages: dict[Key, Page], label: str
) -> None:
    page = pages[("admin", label)]
    bar = bar_of(page)

    assert bar is not None
    assert controls_in(bar, "list-bar__actions") != []
    assert [text for text in header_controls(page) if not text.startswith(NAVIGATION)] == []


def test_the_node_list_shows_its_count_with_no_actions_and_keeps_its_way_back_in_the_header(
    pages: dict[Key, Page],
) -> None:
    page = pages[("admin", COUNT_ONLY)]
    bar = bar_of(page)

    assert bar is not None
    assert controls_in(bar, "list-bar__actions") == []
    assert header_controls(page) == ["Back to Swarm"]


@pytest.mark.parametrize("role", ROLES)
def test_no_list_page_header_holds_anything_but_navigation(
    pages: dict[Key, Page], role: str
) -> None:
    found = {
        label: [t for t in header_controls(pages[(role, label)]) if not t.startswith(NAVIGATION)]
        for label in answered(pages, role)
    }

    assert found == {label: [] for label in answered(pages, role)}


@pytest.mark.parametrize("role", ROLES)
def test_the_bar_sits_below_the_page_header_and_above_the_list(
    pages: dict[Key, Page], role: str
) -> None:
    misplaced = [label for label in answered(pages, role) if is_misplaced(pages[(role, label)])]

    assert misplaced == []


@pytest.mark.parametrize("label", FOLDED_FILTERS)
def test_a_list_with_a_single_filter_input_holds_it_in_the_bar(
    pages: dict[Key, Page], label: str
) -> None:
    page = pages[("admin", label)]
    bar = bar_of(page)

    assert bar is not None
    inputs = [n.attrs["name"] for n in bar.walk() if n.tag == "input" and "name" in n.attrs]
    assert inputs == ["q"]
    assert [n for n in page.tags("input") if n.attrs.get("name") == "q"] != []
    assert not any(
        n.attrs.get("name") == "q"
        for n in page.tags("input")
        if not any(a is bar for a in n.ancestors())
    )


def test_a_standard_user_is_offered_no_global_create_action_it_may_not_use(
    pages: dict[Key, Page],
) -> None:
    offered = {
        label: controls_in(bar, "list-bar__actions")
        for label in ("dashboard", "team-list")
        if (bar := bar_of(pages[("standard", label)])) is not None
    }

    assert offered == {"dashboard": [], "team-list": []}


async def test_a_filter_swap_of_the_environment_list_carries_the_count_out_of_band(
    admin_client: AsyncClient, world: World
) -> None:
    fragment = await admin_client.get("/?q=local", headers=HTMX)

    assert fragment.status_code == OK
    assert out_of_band_count(fragment.text) == "1 environment matches these filters."


async def test_a_filter_swap_of_the_automation_list_carries_the_count_out_of_band(
    admin_client: AsyncClient, world: World
) -> None:
    fragment = await admin_client.get("/automation?subsystem=container-updater", headers=HTMX)

    assert fragment.status_code == OK
    assert out_of_band_count(fragment.text) == "0 automations match these filters."
