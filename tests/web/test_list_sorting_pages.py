"""Sorting through the list pages: refused input is a 400, links keep the order, the total loads."""

import pytest
from httpx import AsyncClient

from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.pagination import CursorPosition, encode_cursor
from tests.auth_support import STANDARD_ID
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.fake_engine import Reply, error_reply, json_reply
from tests.docker_engine.swarm_engine import SwarmEngine, plain_spec
from tests.list_sort_support import CURSOR_MESSAGE
from tests.web.environment_support import World
from tests.web.image_support import assert_no_store, images_path, links_in, text_of
from tests.web.registry_support import HTMX

OK, BAD_REQUEST, NOT_FOUND = 200, 400, 404
SORT_MESSAGE = "Sort by one of the listed columns."
DIRECTION_MESSAGE = "Choose ascending or descending."
INTEGER_CURSOR = encode_cursor(CursorPosition(7, 1))
FOREIGN_CURSOR = encode_cursor(CursorPosition("size.desc:4", 1))
DF = "/system/df"
LAYERS_SIZE = 2_684_354_560
OVER_THE_CAP = 17 * 1024 * 1024
UNKNOWN_ENVIRONMENT = 9999

REFUSALS = [
    ("sort=bogus", SORT_MESSAGE),
    ("sort={key}&dir=sideways", DIRECTION_MESSAGE),
    (f"sort={{key}}&cursor={INTEGER_CURSOR}", CURSOR_MESSAGE),
    (f"cursor={INTEGER_CURSOR}", CURSOR_MESSAGE),
    (f"sort={{key}}&cursor={FOREIGN_CURSOR}", CURSOR_MESSAGE),
]
STANDALONE_LISTS = [
    ("containers", "name"),
    ("images", "image"),
    ("volumes", "name"),
    ("networks", "name"),
    ("stacks", "name"),
]
SWARM_LISTS = [("services", "name"), ("configs", "name"), ("secrets", "name")]


def standalone_path(world: World, kind: str) -> str:
    return f"/environments/{world.remote_id}/{kind}"


def swarm_path(world: World, kind: str) -> str:
    return f"/environments/{world.remote_id}/swarm/{kind}"


@pytest.mark.parametrize(("query", "message"), REFUSALS)
@pytest.mark.parametrize(("kind", "key"), STANDALONE_LISTS)
async def test_a_refused_sort_direction_or_cursor_is_a_400_with_the_message_never_a_500(
    admin_client: AsyncClient,
    world: World,
    daemon: ContainerEngine,
    kind: str,
    key: str,
    query: str,
    message: str,
) -> None:
    path = f"{standalone_path(world, kind)}?{query.format(key=key)}"

    page = await admin_client.get(path)
    fragment = await admin_client.get(path, headers=HTMX)

    assert (page.status_code, fragment.status_code) == (BAD_REQUEST, BAD_REQUEST)
    assert message in text_of(page) and message in text_of(fragment)


@pytest.mark.parametrize(("query", "message"), REFUSALS[:2] + REFUSALS[2:3])
@pytest.mark.parametrize(("kind", "key"), SWARM_LISTS)
async def test_a_refused_sort_direction_or_cursor_on_a_swarm_list_is_a_400_with_the_message(
    admin_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
    kind: str,
    key: str,
    query: str,
    message: str,
) -> None:
    path = f"{swarm_path(world, kind)}?{query.format(key=key)}"

    page = await admin_client.get(path)
    fragment = await admin_client.get(path, headers=HTMX)

    assert (page.status_code, fragment.status_code) == (BAD_REQUEST, BAD_REQUEST)
    assert message in text_of(page) and message in text_of(fragment)


@pytest.mark.parametrize(("query", "message"), REFUSALS[:2])
async def test_a_refused_sort_or_direction_on_the_swarm_stack_list_is_a_400_with_the_message(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine, query: str, message: str
) -> None:
    page = await admin_client.get(f"{swarm_path(world, 'stacks')}?{query.format(key='name')}")

    assert page.status_code == BAD_REQUEST
    assert message in text_of(page)


async def test_image_pager_links_and_headers_carry_the_sort_and_the_next_page_continues_it(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    path = f"{images_path(world)}?sort=size&dir=desc&limit=2"

    first = await admin_client.get(path)
    next_link = next(h for h in links_in(first) if "cursor=" in h)
    second = await admin_client.get(next_link)
    fragment = await admin_client.get(next_link, headers=HTMX)

    assert "sort=size&dir=desc" in next_link
    assert (first.status_code, second.status_code, fragment.status_code) == (OK, OK, OK)
    assert 'aria-sort="descending"' in first.text
    assert "nginx:1" in first.text and "nginx:1" not in second.text
    assert "spare:1" in second.text


async def test_container_and_volume_and_network_pager_links_carry_the_sort(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    for number in (1, 2, 3):
        container_host.add(f"{number:04x}".ljust(64, "0"), f"box-{number}")
        container_host.add_volume(f"vol-{number}")

    containers = await admin_client.get(
        f"{standalone_path(world, 'containers')}?sort=state&dir=desc&limit=2"
    )
    volumes = await admin_client.get(
        f"{standalone_path(world, 'volumes')}?sort=created&dir=asc&limit=2"
    )
    networks = await admin_client.get(
        f"{standalone_path(world, 'networks')}?sort=driver&dir=desc&limit=2"
    )

    assert "sort=state&dir=desc" in next(h for h in links_in(containers) if "cursor=" in h)
    assert "sort=created&dir=asc" in next(h for h in links_in(volumes) if "cursor=" in h)
    assert "sort=driver&dir=desc" in next(h for h in links_in(networks) if "cursor=" in h)


async def test_swarm_service_pager_links_carry_the_sort(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    for name in ("web", "api", "db"):
        swarm_host.add_service(plain_spec(name))

    page = await admin_client.get(f"{swarm_path(world, 'services')}?sort=name&dir=desc&limit=2")

    assert "sort=name&dir=desc" in next(h for h in links_in(page) if "cursor=" in h)
    assert text_of(page).index("web") < text_of(page).index("db")


async def test_the_image_total_fragment_shows_the_host_wide_figure_without_caching(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    daemon.route("GET", DF, json_reply({"LayersSize": LAYERS_SIZE}))

    fragment = await admin_client.get(f"{images_path(world)}/disk-usage")

    assert fragment.status_code == OK
    assert "2.5 GiB for all images on this host." in text_of(fragment)
    assert "<html" not in fragment.text
    assert_no_store(fragment)
    assert daemon.calls("GET", DF)[0].query == {"type": "image"}


@pytest.mark.parametrize(
    "behaviour",
    [
        error_reply(500),
        json_reply({}),
        Reply(200, b"{}", {"Content-Length": str(OVER_THE_CAP)}),
    ],
    ids=["engine error", "no figure", "over the cap"],
)
async def test_the_image_total_fragment_is_empty_when_the_host_cannot_give_the_figure(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine, behaviour: Reply
) -> None:
    daemon.route("GET", DF, behaviour)

    fragment = await admin_client.get(f"{images_path(world)}/disk-usage")

    assert fragment.status_code == OK
    assert fragment.text.strip() == ""
    assert_no_store(fragment)


async def test_the_image_total_fragment_makes_no_engine_call_for_an_environment_not_visible(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    daemon: ContainerEngine,
) -> None:
    daemon.route("GET", DF, json_reply({"LayersSize": LAYERS_SIZE}))
    async with world.app.state.database.session() as db:
        await EnvironmentAccessRepository(db).remove_user(
            "environment", world.remote_id, STANDARD_ID
        )

    unknown = await admin_client.get(f"/environments/{UNKNOWN_ENVIRONMENT}/images/disk-usage")
    refused = await standard_client.get(f"{images_path(world)}/disk-usage")

    assert (unknown.status_code, refused.status_code) == (NOT_FOUND, NOT_FOUND)
    assert daemon.calls("GET", DF) == []
