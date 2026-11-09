"""Named objects in tables link to their own page and carry the page they leave as the origin."""

from urllib.parse import quote

from httpx import AsyncClient

from app.db.repositories.resource_control import ResourceControlRepository
from app.services.access import ResourceKey
from tests.access_support import plant
from tests.auth_support import STANDARD_ID
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.stack_support import PROJECT_LABEL
from tests.swarm_support import NAMESPACE_LABEL
from tests.web.environment_support import World
from tests.web.html_tree import parse
from tests.web.test_container_pages import containers
from tests.web.test_origins import back_links
from tests.web.test_stack_log_routes import (
    SHOP,
    seed_shop,
    share_stack_with_standard_user,
    stack_url,
    swarm_url,
)

OK = 200
LOOSE = "loose"
LOOSE_CONTAINER = f"{0xD1:064x}"
SOLO_CONTAINER = f"{0xD2:064x}"
NAMESPACE = "edge"
MOUNTING_CONTAINER = f"{0xD4:064x}"
VOLUME = "appdata"


def links_named(html: str, text: str) -> list[str]:
    return [
        link.attrs["href"]
        for link in parse(html).walk()
        if link.tag == "a" and link.text().strip() == text
    ]


def from_list(world: World, name: str) -> str:
    return "from=" + quote(f"list:{world.remote_id}:{name}", safe="")


async def test_the_container_list_opens_a_managed_stack_and_filters_by_an_unmanaged_project(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await seed_shop(world, container_host)
    container_host.add(LOOSE_CONTAINER, "loose-web-1", labels={PROJECT_LABEL: LOOSE})

    listing = await admin_client.get(containers(world))
    stack_href = links_named(listing.text, SHOP)[0]
    reached = await admin_client.get(stack_href)

    assert set(links_named(listing.text, SHOP)) == {
        f"{stack_url(world)}?{from_list(world, 'containers')}"
    }
    assert links_named(listing.text, LOOSE) == [f"{containers(world)}?stack={LOOSE}"]
    assert back_links(reached.text) == [
        ("Back to stacks", f"/environments/{world.remote_id}/stacks"),
        ("Back to containers", containers(world)),
    ]


async def test_a_standard_user_is_offered_the_stack_page_only_once_they_may_read_the_stack(
    standard_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    await seed_shop(world, shared_host)
    shared_host.add(SOLO_CONTAINER, "logshop-solo-1", labels={PROJECT_LABEL: SHOP})
    async with world.app.state.database.session() as db:
        await plant(
            ResourceControlRepository(db, world.app.state.auth.clock),
            ResourceKey(world.remote_id, "container", SOLO_CONTAINER),
            owner=STANDARD_ID,
        )

    hidden = await standard_client.get(containers(world))
    await share_stack_with_standard_user(world)
    shared = await standard_client.get(containers(world))

    assert set(links_named(hidden.text, SHOP)) == {f"{containers(world)}?stack={SHOP}"}
    assert f"{stack_url(world)}?{from_list(world, 'containers')}" in links_named(shared.text, SHOP)


async def test_the_service_list_opens_the_swarm_stack_and_the_stack_offers_the_way_back(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    swarm_host.named_service("edge-web", labels={NAMESPACE_LABEL: NAMESPACE})
    services = f"/environments/{world.remote_id}/swarm/services"

    listing = await admin_client.get(services)
    reached = await admin_client.get(links_named(listing.text, NAMESPACE)[0])

    assert links_named(listing.text, NAMESPACE) == [
        f"{swarm_url(world, NAMESPACE)}?{from_list(world, 'services')}"
    ]
    assert ("Back to services", services) in back_links(reached.text)


async def test_a_global_list_origin_is_offered_only_to_a_viewer_who_may_open_that_list(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
) -> None:
    await seed_shop(world, shared_host)
    path = f"{stack_url(world)}?from=" + quote("list:-:activity", safe="")
    await share_stack_with_standard_user(world)

    administrator = await admin_client.get(path)
    standard = await standard_client.get(path)

    assert ("Back to the activity log", "/activity") in back_links(administrator.text)
    assert ("Back to the activity log", "/activity") not in back_links(standard.text)


async def test_a_mounted_volume_links_only_for_a_viewer_who_may_open_the_volume(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
) -> None:
    shared_host.add(
        MOUNTING_CONTAINER,
        "mounting",
        mounts=[{"Type": "volume", "Name": VOLUME, "Destination": "/data", "RW": True}],
    )
    async with world.app.state.database.session() as db:
        await plant(
            ResourceControlRepository(db, world.app.state.auth.clock),
            ResourceKey(world.remote_id, "container", MOUNTING_CONTAINER),
            owner=STANDARD_ID,
        )
    path = f"{containers(world)}/{MOUNTING_CONTAINER}?tab=mounts"

    administrator = await admin_client.get(path)
    standard = await standard_client.get(path)

    origin = "from=" + quote(f"container:{world.remote_id}:{MOUNTING_CONTAINER}", safe="")
    assert links_named(administrator.text, VOLUME) == [
        f"/environments/{world.remote_id}/volumes/{VOLUME}?{origin}"
    ]
    assert standard.status_code == OK
    assert links_named(standard.text, VOLUME) == []
    assert VOLUME in standard.text
