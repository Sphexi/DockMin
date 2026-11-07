"""The ownership and access panel on every object's Access tab: what is open, what is collapsed."""

from typing import NamedTuple

from httpx import AsyncClient, Response

from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.swarm_engine import SwarmEngine, plain_spec
from tests.stack_support import stopped_stack_jobs  # noqa: F401
from tests.web.access_panel_support import (
    UNKNOWN_USER,
    UNKNOWN_USER_TEXT,
    UNPROCESSABLE,
    PanelRoutes,
    digest_in,
    save,
    saved_page,
)
from tests.web.environment_support import World
from tests.web.helpers import disclosures_in
from tests.web.html_tree import Node, parse
from tests.web.test_container_pages import access_routes as container_routes
from tests.web.test_network_pages import access_routes as network_routes
from tests.web.test_network_pages import seeded as seeded_networks
from tests.web.test_stack_pages import compose_file, create_blog, stacks_path
from tests.web.test_swarm_object_flows import config_access_routes
from tests.web.test_swarm_stack_pages import NAME as SWARM_STACK
from tests.web.test_swarm_stack_pages import Browser, create_shop
from tests.web.test_swarm_stack_pages import base as swarm_stacks_path
from tests.web.test_volume_pages import access_routes as volume_routes
from tests.web.test_volume_pages import seeded as seeded_volumes

CHANGE = "access-change"
REMOVE = "access-remove"
EFFECTIVE = "effective-access"
COMPOSE_STACK = "blog"


class Flow(NamedTuple):
    """The Access tab with no control, with a control, and after a save the form rejects."""

    first: str
    created: str
    rejected: Response


def control_facts_are_expanded(html: str) -> bool:
    panel = next(n for n in parse(html).walk() if "access-panel" in n.classes())
    facts = [c for c in panel.children if isinstance(c, Node) and c.tag == "dl"]
    return len(facts) == 1 and "Control" in facts[0].text()


async def flow_over(client: AsyncClient, routes: PanelRoutes) -> Flow:
    first = await client.get(routes.page)
    created = await saved_page(
        client, routes, digest_in(first.text), visibility="public", access_level="read"
    )
    rejected = await save(
        client,
        routes,
        digest_in(created.text),
        visibility="restricted",
        access_level="read",
        add_users=UNKNOWN_USER,
    )
    return Flow(first.text, created.text, rejected)


def assert_panel_collapses_its_forms(flow: Flow) -> None:
    assert control_facts_are_expanded(flow.first)
    assert control_facts_are_expanded(flow.created)
    assert control_facts_are_expanded(flow.rejected.text)
    first = disclosures_in(flow.first)
    assert (first[CHANGE], first.get(REMOVE, False)) == (False, False)
    created = disclosures_in(flow.created)
    assert (created[CHANGE], created[REMOVE]) == (False, False)
    assert flow.rejected.status_code == UNPROCESSABLE
    assert disclosures_in(flow.rejected.text)[CHANGE] is True
    assert disclosures_in(flow.rejected.text)[REMOVE] is False
    assert UNKNOWN_USER_TEXT in flow.rejected.text


async def test_a_containers_access_tab_collapses_its_forms_and_opens_one_on_a_refusal(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    routes = container_routes(world)

    flow = await flow_over(admin_client, routes)

    assert_panel_collapses_its_forms(flow)
    assert disclosures_in(flow.first)[EFFECTIVE] is False
    assert disclosures_in(flow.rejected.text)[EFFECTIVE] is False


async def test_the_container_list_collapses_effective_access_and_opens_it_on_request(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    path = f"/environments/{world.remote_id}/containers"

    closed = await admin_client.get(path)
    requested = await admin_client.get(f"{path}?open={EFFECTIVE}")

    assert disclosures_in(closed.text)[EFFECTIVE] is False
    assert disclosures_in(requested.text)[EFFECTIVE] is True


async def test_a_volumes_access_tab_collapses_its_forms_and_opens_one_on_a_refusal(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seeded_volumes(container_host)

    flow = await flow_over(admin_client, volume_routes(world))

    assert_panel_collapses_its_forms(flow)


async def test_a_networks_access_tab_collapses_its_forms_and_opens_one_on_a_refusal(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    routes = network_routes(world, seeded_networks(container_host)["shop"])

    flow = await flow_over(admin_client, routes)

    assert_panel_collapses_its_forms(flow)


async def test_a_swarm_configs_access_tab_collapses_its_forms_and_opens_one_on_a_refusal(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    routes = config_access_routes(world, swarm_host.add_config("conf"))

    flow = await flow_over(admin_client, routes)

    assert_panel_collapses_its_forms(flow)


async def test_a_swarm_services_access_tab_collapses_its_forms_and_opens_one_on_a_refusal(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service = swarm_host.add_service(plain_spec("api"))
    base = f"/environments/{world.remote_id}/swarm/services/{service}"
    routes = PanelRoutes(f"{base}?tab=access", f"{base}/access", f"{base}/access/delete")

    flow = await flow_over(admin_client, routes)

    assert_panel_collapses_its_forms(flow)


async def test_a_compose_stacks_access_tab_collapses_its_forms_and_opens_one_on_a_refusal(
    admin_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
    stopped_stack_jobs: None,  # noqa: F811
) -> None:
    await create_blog(admin_client, world, file=compose_file())
    base = f"{stacks_path(world)}/{COMPOSE_STACK}"
    routes = PanelRoutes(f"{base}?tab=access", f"{base}/access", f"{base}/access/delete")

    flow = await flow_over(admin_client, routes)

    assert_panel_collapses_its_forms(flow)


async def test_a_managed_swarm_stacks_access_tab_collapses_its_forms_and_opens_one_on_a_refusal(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world = swarm_stacks_world
    await create_shop(Browser(admin_client), world)
    base = f"{swarm_stacks_path(world)}/{SWARM_STACK}"
    routes = PanelRoutes(f"{base}?tab=access", f"{base}/access", f"{base}/access/delete")

    flow = await flow_over(admin_client, routes)

    assert_panel_collapses_its_forms(flow)
