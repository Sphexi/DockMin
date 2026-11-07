"""The second back button: shown only for an origin the viewer may read, never built from input."""

import asyncio
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from httpx import AsyncClient

from app.services import origins
from app.services.stack_queries import StackQueryService
from tests.access_support import container_id
from tests.auth_support import STANDARD_USERNAME, csrf_form
from tests.container_support import JOB, WEB
from tests.docker_engine.container_engine import ContainerEngine
from tests.web.access_panel_support import SEE_OTHER, digest_in
from tests.web.environment_support import World, seed_stack_rows
from tests.web.html_tree import parse
from tests.web.test_container_pages import access_routes, containers
from tests.web.test_stack_log_routes import (
    SHOP,
    seed_shop,
    share_stack_with_standard_user,
    stack_url,
)

BACK_TO_CONTAINERS = "Back to containers"
BACK_TO_STACK = f"Back to stack {SHOP}"
LOGS_TAB = "logs"
BUDGET = 0.05
SECRET_MARKER = "marker-that-must-not-be-echoed"
TAB_KEYS = (
    "overview",
    "labels",
    "mounts",
    "networks",
    "environment",
    "stats",
    LOGS_TAB,
    "automation",
    "access",
)
MISSING_CONTAINER = container_id(0xEE)
HOSTILE = (
    "//evil.test/x",
    "\\\\evil.test\\x",
    "https://evil.test/x",
    "javascript:alert(1)",
    f"stack:{{remote}}:{SHOP}/../../x",
    f"stack:{{remote}}:{SHOP}%2f..%2fx",
    f"stack:{{remote}}:{SHOP}\\x",
    "stack:{remote}://evil.test",
    f"stack:{{remote}}:{SHOP}#frag",
    f"stack:{{remote}}:{SHOP}?x=1",
    f"stack:{{remote}}:{SHOP}\n",
)


def origin_ref(world: World, name: str = SHOP) -> str:
    return f"stack:{world.remote_id}:{name}"


def with_from(path: str, ref: str) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}from={quote(ref, safe='')}"


def back_links(html: str) -> list[tuple[str, str]]:
    """Return the text and target of every link that starts with `Back to`."""
    return [
        (link.text().strip(), link.attrs["href"])
        for link in parse(html).walk()
        if link.tag == "a" and link.text().strip().startswith("Back to")
    ]


def origin_of(href: str) -> str | None:
    values = parse_qs(urlsplit(href).query).get("from")
    return None if values is None else values[0]


def container_path(world: World, container: str = WEB, tab: str | None = None) -> str:
    suffix = "" if tab is None else f"?tab={tab}"
    return f"{containers(world)}/{container}{suffix}"


async def test_a_container_reached_from_a_stacks_services_table_offers_a_way_back_to_the_stack(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    boxes = await seed_shop(world, container_host)
    stack_page = await admin_client.get(stack_url(world))
    container_links = [
        link.attrs["href"]
        for link in parse(stack_page.text).walk()
        if link.tag == "a" and f"/containers/{boxes['web1'].id}" in link.attrs.get("href", "")
    ]
    reached = await admin_client.get(container_links[0])
    plain = await admin_client.get(container_path(world, boxes["web1"].id))

    assert {origin_of(href) for href in container_links} == {origin_ref(world)}
    assert [f"tab={LOGS_TAB}" in href for href in container_links] == [False, True]
    assert back_links(reached.text) == [
        (BACK_TO_CONTAINERS, containers(world)),
        (BACK_TO_STACK, stack_url(world)),
    ]
    assert back_links(plain.text) == [(BACK_TO_CONTAINERS, containers(world))]


async def test_an_origin_that_cannot_be_used_shows_no_second_button_and_is_never_echoed(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await seed_shop(world, container_host)
    unusable = {
        "removed stack": origin_ref(world, "gone"),
        "stack of another environment": f"stack:{world.local_id}:{SHOP}",
        "unknown environment": f"stack:99999:{SHOP}",
        "unreachable environment host": f"container:{world.down_id}:{WEB}",
        "removed container": f"container:{world.remote_id}:{MISSING_CONTAINER}",
        "malformed": f"stack:{world.remote_id}",
        "over-long": f"stack:{world.remote_id}:" + "a" * 400,
        "marker": f"stack:{world.remote_id}:{SECRET_MARKER}/x",
    }

    pages = {
        case: await admin_client.get(with_from(container_path(world), ref))
        for case, ref in unusable.items()
    }

    assert {case: back_links(page.text) for case, page in pages.items()} == {
        case: [(BACK_TO_CONTAINERS, containers(world))] for case in unusable
    }
    assert not any(
        ref in page.text or quote(ref, safe="") in page.text
        for ref, page in zip(unusable.values(), pages.values(), strict=True)
    )
    assert SECRET_MARKER not in "".join(page.text for page in pages.values())


async def test_a_standard_user_given_a_stack_they_cannot_read_gets_no_button_until_it_is_shared(
    standard_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    await seed_shop(world, shared_host)
    path = with_from(container_path(world, JOB), origin_ref(world))

    hidden = await standard_client.get(path)
    await share_stack_with_standard_user(world)
    shared = await standard_client.get(path)

    assert back_links(hidden.text) == [(BACK_TO_CONTAINERS, containers(world))]
    assert SHOP not in hidden.text
    assert back_links(shared.text) == [
        (BACK_TO_CONTAINERS, containers(world)),
        (BACK_TO_STACK, stack_url(world)),
    ]


async def test_an_origin_whose_read_outlasts_the_budget_shows_no_second_button(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with world.app.state.database.session() as db:
        await seed_stack_rows(db, world.remote_id, (SHOP, "compose"))

    async def slow(*_: object, **__: object) -> str:
        await asyncio.sleep(60)
        return SHOP

    monkeypatch.setattr(StackQueryService, "origin_name", slow)
    monkeypatch.setattr(origins, "HOST_BUDGET_SECONDS", BUDGET)

    page = await admin_client.get(with_from(container_path(world), origin_ref(world)))

    assert page.status_code == 200
    assert back_links(page.text) == [(BACK_TO_CONTAINERS, containers(world))]


async def test_from_survives_tab_changes_and_is_absent_after_a_post(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await seed_shop(world, container_host)
    ref = origin_ref(world)
    routes = access_routes(world)
    page = await admin_client.get(with_from(container_path(world), ref))
    fragment = await admin_client.get(
        with_from(container_path(world, tab=LOGS_TAB), ref),
        headers={"HX-Request": "true", "HX-Target": "container-tabs"},
    )
    access = await admin_client.get(with_from(routes.page, ref))
    posted = await admin_client.post(
        with_from(routes.save, ref),
        data=csrf_form(
            admin_client,
            expected_digest=digest_in(access.text),
            visibility="restricted",
            access_level="read",
            owner_name=STANDARD_USERNAME,
        ),
    )
    tab_hrefs = {
        name: [
            link.attrs["href"]
            for link in parse(html).walk()
            if link.tag == "a" and "tabs__link" in link.classes()
        ]
        for name, html in (("page", page.text), ("fragment", fragment.text))
    }
    forms = [
        form
        for html in (page.text, access.text)
        for form in parse(html).walk()
        if form.tag == "form" and form.attrs.get("method", "get").lower() == "post"
    ]

    assert {name: [origin_of(href) for href in hrefs] for name, hrefs in tab_hrefs.items()} == {
        name: [ref] * len(hrefs) for name, hrefs in tab_hrefs.items()
    }
    assert len(tab_hrefs["page"]) == len(TAB_KEYS)
    assert forms
    assert not any("from" in form.attrs.get("action", "") for form in forms)
    assert not any(field.attrs.get("name") == "from" for form in forms for field in form.walk())
    assert posted.status_code == SEE_OTHER
    assert "from=" not in posted.headers["location"]


async def test_a_from_value_cannot_build_an_off_site_or_arbitrary_path_link(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await seed_shop(world, container_host)
    values = [value.format(remote=world.remote_id) for value in HOSTILE]

    pages = [await admin_client.get(with_from(container_path(world), value)) for value in values]
    valid = await admin_client.get(with_from(container_path(world), origin_ref(world)))

    assert [back_links(page.text) for page in pages] == [
        [(BACK_TO_CONTAINERS, containers(world))]
    ] * len(values)
    assert "evil.test" not in "".join(page.text for page in pages)
    assert back_links(valid.text)[1][1] == stack_url(world)
