"""The sort through list markup: filter forms keep it, links keep it, the image total loads once."""

import re

import pytest
from httpx import AsyncClient

from app.db.repositories.pagination import CursorPosition, Page, encode_cursor
from app.web.pagination import Column, PageView
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.swarm_engine import SwarmEngine, plain_spec
from tests.swarm_support import NAMESPACE_LABEL
from tests.web.environment_support import World
from tests.web.helpers import render_macro
from tests.web.image_support import hidden_values, images_path, text_of
from tests.web.registry_support import HTMX

OK = 200
NO_MATCH = "q=no-such-object"
SORT_CONTROLS = re.compile(r'<div id="sort-fields" hidden( hx-swap-oob="true")?>(.*?)</div>', re.S)
CLEAR_FILTERS = re.compile(r'<a class="btn btn--primary" href="([^"]*)">Clear filters</a>')
STANDALONE_LISTS = [
    ("containers", "state"),
    ("images", "size"),
    ("volumes", "driver"),
    ("networks", "scope"),
    ("stacks", "name"),
]
SWARM_LISTS = [
    ("services", "mode"),
    ("configs", "stack"),
    ("secrets", "created"),
    ("stacks", "state"),
]
ALL_RESULTS_REGIONS = {
    "containers": "containers-results",
    "images": "images-results",
    "volumes": "volumes-results",
    "networks": "networks-results",
    "stacks": "stacks-results",
    "services": "services-results",
    "configs": "configs-results",
    "secrets": "secrets-results",
}
DISK_PLACEHOLDER = re.compile(
    r'<span id="image-disk-total" hx-get="([^"]*)" hx-trigger="load" hx-swap="outerHTML"></span>'
)


def standalone_path(world: World, kind: str) -> str:
    return f"/environments/{world.remote_id}/{kind}"


def swarm_path(world: World, kind: str) -> str:
    return f"/environments/{world.remote_id}/swarm/{kind}"


def sort_controls(html: str) -> tuple[bool, str]:
    found = SORT_CONTROLS.search(html)
    assert found is not None, "the sort controls are missing"
    return found.group(1) is not None, " ".join(found.group(2).split())


def empty_view(*, sort: str | None, cursor: str | None = None) -> PageView:
    return PageView.of(
        Page(items=[], next_cursor=None),
        base_path="/items",
        limit=25,
        cursor=cursor,
        sort=sort,
        direction="desc",
        params=(("q", "x"),),
    )


def empty_table(view: PageView) -> str:
    return render_macro(
        "data_table",
        "data_table",
        table_id="t",
        caption="Things",
        columns=[Column("name", "Name", sortable=True)],
        pager=view,
        filtered=True,
        caller=lambda: "",
    )


async def assert_sort_is_carried(
    client: AsyncClient, path: str, kind: str, key: str, *, region: str | None = None
) -> None:
    sorted_path = f"{path}?sort={key}&dir=desc"

    page = await client.get(sorted_path)
    fragment = await client.get(sorted_path, headers=HTMX)

    assert (page.status_code, fragment.status_code) == (OK, OK), kind
    assert (hidden_values(page, "sort"), hidden_values(page, "dir")) == ([key], ["desc"]), kind
    assert sort_controls(fragment.text) == (
        True,
        f'<input type="hidden" name="sort" value="{key}">'
        ' <input type="hidden" name="dir" value="desc">',
    ), kind
    if region is not None:
        assert f'hx-target="#{region}" hx-swap="innerHTML"' in page.text, kind


async def assert_no_sort_is_carried(client: AsyncClient, path: str, kind: str) -> None:
    page = await client.get(path)
    fragment = await client.get(path, headers=HTMX)

    assert hidden_values(page, "sort") == [] and hidden_values(page, "dir") == [], kind
    assert sort_controls(page.text) == (False, ""), kind
    assert sort_controls(fragment.text) == (True, ""), kind


@pytest.mark.parametrize(("kind", "key"), STANDALONE_LISTS)
async def test_a_standalone_filter_form_carries_the_active_sort_and_a_results_swap_replaces_it(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine, kind: str, key: str
) -> None:
    await assert_sort_is_carried(
        admin_client, standalone_path(world, kind), kind, key, region=f"{kind}-results"
    )


@pytest.mark.parametrize(("kind", "key"), SWARM_LISTS)
async def test_a_swarm_filter_form_carries_the_active_sort_and_a_results_swap_replaces_it(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine, kind: str, key: str
) -> None:
    await assert_sort_is_carried(
        admin_client, swarm_path(world, kind), kind, key, region=f"{kind}-results"
    )


@pytest.mark.parametrize("kind", [kind for kind, _ in STANDALONE_LISTS])
async def test_an_unsorted_standalone_list_has_no_sort_inputs_and_a_swap_empties_the_copy(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine, kind: str
) -> None:
    await assert_no_sort_is_carried(admin_client, standalone_path(world, kind), kind)


@pytest.mark.parametrize("kind", [kind for kind, _ in SWARM_LISTS])
async def test_an_unsorted_swarm_list_has_no_sort_inputs_and_a_swap_empties_the_copy(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine, kind: str
) -> None:
    await assert_no_sort_is_carried(admin_client, swarm_path(world, kind), kind)


async def test_a_refused_sort_drops_the_sort_inputs_instead_of_carrying_it_on(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    fragment = await admin_client.get(
        f"{standalone_path(world, 'volumes')}?sort=bogus&dir=desc", headers=HTMX
    )

    assert fragment.status_code == 400
    assert sort_controls(fragment.text) == (True, "")


@pytest.mark.parametrize(("kind", "key"), STANDALONE_LISTS)
async def test_clear_filters_on_a_standalone_list_keeps_the_sort_and_drops_the_filter(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine, kind: str, key: str
) -> None:
    page = await admin_client.get(
        f"{standalone_path(world, kind)}?{NO_MATCH}&sort={key}&dir=desc", headers=HTMX
    )

    link = CLEAR_FILTERS.search(page.text)
    assert link is not None, kind
    assert link.group(1).endswith(f"/{kind}?sort={key}&amp;dir=desc"), kind
    assert "q=" not in link.group(1)


@pytest.mark.parametrize(("kind", "key"), SWARM_LISTS)
async def test_clear_filters_on_a_swarm_list_keeps_the_sort_and_drops_the_filter(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine, kind: str, key: str
) -> None:
    page = await admin_client.get(
        f"{swarm_path(world, kind)}?{NO_MATCH}&sort={key}&dir=desc", headers=HTMX
    )

    link = CLEAR_FILTERS.search(page.text)
    assert link is not None, kind
    assert link.group(1).endswith(f"/{kind}?sort={key}&amp;dir=desc"), kind
    assert "q=" not in link.group(1)


def test_clear_filters_without_a_sort_is_the_plain_list_path() -> None:
    link = CLEAR_FILTERS.search(empty_table(empty_view(sort=None)))

    assert link is not None and link.group(1) == "/items"


def test_back_to_the_first_page_keeps_the_sort_and_the_filter() -> None:
    html = empty_table(empty_view(sort="name", cursor=encode_cursor(CursorPosition("a", 1))))

    assert 'href="/items?q=x&amp;sort=name&amp;dir=desc&amp;limit=25"' in html
    assert "Back to the first page" in html


async def test_the_swarm_stack_list_answers_a_results_fragment_with_the_summary_out_of_band(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    for namespace in ("beta", "alpha"):
        swarm_host.add_service(
            {**plain_spec(f"{namespace}_web"), "Labels": {NAMESPACE_LABEL: namespace}}
        )
    path = f"{swarm_path(world, 'stacks')}?sort=name&dir=desc"

    page = await admin_client.get(path)
    fragment = await admin_client.get(path, headers=HTMX)

    assert "<html" in page.text and '<div id="stacks-results">' in page.text
    assert "<html" not in fragment.text and 'id="stacks-table"' in fragment.text
    assert re.search(
        r'<p id="results-summary"[^>]*hx-swap-oob="innerHTML"[^>]*>\s*2 stacks\.', fragment.text
    )
    assert 'hx-target="#stacks-results" hx-swap="innerHTML"' in fragment.text
    assert fragment.headers["cache-control"] == "no-store"
    assert text_of(fragment).index("beta") < text_of(fragment).index("alpha")
    assert 'aria-sort="descending"' in fragment.text


async def test_the_swarm_stack_list_fragment_reports_a_refused_sort_as_a_400_region(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    fragment = await admin_client.get(f"{swarm_path(world, 'stacks')}?sort=bogus", headers=HTMX)

    assert fragment.status_code == 400
    assert "<html" not in fragment.text and "Sort by one of the listed columns." in fragment.text


async def test_the_images_page_holds_an_empty_placeholder_that_loads_the_total_on_load(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    page = await admin_client.get(images_path(world))

    found = DISK_PLACEHOLDER.search(page.text)
    assert found is not None
    assert found.group(1) == f"{images_path(world)}/disk-usage"
    assert "for all images on this host" not in text_of(page)
    summary = re.search(r'<p id="results-summary"[^>]*>(.*?)</p>', page.text, re.S)
    assert summary is not None and re.sub(r"<[^>]+>", "", summary.group(1)).strip() == "4 images."


@pytest.mark.parametrize("query", ["", "?sort=size&dir=desc", "?q=nginx", "?q=nginx&sort=size"])
async def test_an_images_results_swap_keeps_the_total_by_id_and_never_asks_for_it_again(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine, query: str
) -> None:
    fragment = await admin_client.get(f"{images_path(world)}{query}", headers=HTMX)

    assert re.search(
        r'<p id="results-summary"[^>]*hx-swap-oob="innerHTML"[^>]*>[^<]*'
        r'<span id="image-disk-total" hx-preserve></span>\s*</p>',
        fragment.text,
    )
    assert "disk-usage" not in fragment.text and 'hx-trigger="load"' not in fragment.text
