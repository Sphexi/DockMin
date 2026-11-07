"""Automation and activity on the Compose stack, Swarm service and managed Swarm stack tabs."""

from typing import Any

from httpx import AsyncClient

from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.swarm_engine import SwarmEngine, plain_spec
from tests.stack_support import stopped_stack_jobs  # noqa: F401
from tests.web.environment_support import World
from tests.web.flash_support import location
from tests.web.helpers import activity_panel_in, automation_panel_present
from tests.web.html_tree import parse
from tests.web.test_activity_pages import HEALTH, event_row, insert_events
from tests.web.test_automation_pages import RESTARTER, create
from tests.web.test_stack_pages import compose_file, create_blog, stacks_path
from tests.web.test_swarm_stack_pages import NAME as SHOP
from tests.web.test_swarm_stack_pages import Browser, create_shop, namespace_service
from tests.web.test_swarm_stack_pages import base as swarm_stacks_path

HTMX = {"HX-Request": "true"}
ADD_AUTOMATION = "Add automation"
BLOG = "blog"
STACK_TABS = ("overview", "file", "variables", "logs", "automation", "access")
SERVICE_TABS = ("overview", "configuration", "tasks", "automation", "access")
ALL_ACTIVITY = "Recent activity"
AUTOMATED = "Automated actions"
ACCESS_CHANGES = "Access changes"
SUBSYSTEM_ACTION = "stack_restart_budget_exhausted"
ACCESS_ACTION = "resource_control_updated"
USER_ACTION = "stack_restarted"


def tab_names(html: str) -> list[str]:
    nav = next(n for n in parse(html).walk() if n.tag == "ul" and "tabs__list" in n.classes())
    return [a.text() for a in nav.walk() if a.tag == "a"]


def three_events(
    world: World,
    kind: str,
    target: str,
) -> list[tuple[Any, ...]]:
    scope: dict[str, Any] = {"environment": world.remote_id, "kind": kind, "target": target}
    return [
        event_row(USER_ACTION, 1, **scope),
        event_row(ACCESS_ACTION, 2, **scope),
        event_row(
            SUBSYSTEM_ACTION,
            3,
            category="automation",
            actor="subsystem",
            user=None,
            subsystem=HEALTH,
            **scope,
        ),
    ]


async def panels_by_tab(
    client: AsyncClient, base: str, tabs: tuple[str, ...]
) -> dict[str, tuple[str, list[str]] | None]:
    return {tab: activity_panel_in((await client.get(f"{base}?tab={tab}")).text) for tab in tabs}


async def test_the_compose_stack_automation_tab_holds_the_rules_panel_and_no_other_tab_does(
    admin_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
    stopped_stack_jobs: None,  # noqa: F811
) -> None:
    await create_blog(admin_client, world, file=compose_file())
    base = f"{stacks_path(world)}/{BLOG}"

    pages = {tab: await admin_client.get(f"{base}?tab={tab}") for tab in STACK_TABS}
    fragment = await admin_client.get(f"{base}?tab=automation", headers=HTMX)
    other = await admin_client.get(f"{base}?tab=file", headers=HTMX)

    assert {t: automation_panel_present(p.text) for t, p in pages.items()} == {
        t: t == "automation" for t in STACK_TABS
    }
    assert {t: ADD_AUTOMATION in p.text for t, p in pages.items()} == {
        t: t == "automation" for t in STACK_TABS
    }
    assert tab_names(pages["overview"].text) == [
        "Overview",
        "Compose file",
        "Variables",
        "Files",
        "Logs",
        "Automation",
        "Access",
    ]
    assert automation_panel_present(fragment.text) and "<html" not in fragment.text
    assert not automation_panel_present(other.text)


async def test_the_compose_stack_tabs_each_show_only_their_own_events(
    admin_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
    stopped_stack_jobs: None,  # noqa: F811
) -> None:
    await create_blog(admin_client, world, file=compose_file())
    insert_events(world.app, three_events(world, "stack", BLOG))
    base = f"{stacks_path(world)}/{BLOG}"

    panels = await panels_by_tab(admin_client, base, STACK_TABS)

    assert panels["overview"] is not None
    assert panels["overview"][0] == ALL_ACTIVITY
    assert {USER_ACTION, ACCESS_ACTION, SUBSYSTEM_ACTION} <= set(panels["overview"][1])
    assert panels["automation"] == (AUTOMATED, [SUBSYSTEM_ACTION])
    assert panels["access"] == (ACCESS_CHANGES, [ACCESS_ACTION])
    assert (panels["file"], panels["variables"]) == (None, None)


async def test_pausing_and_resuming_a_stack_rule_return_to_the_stacks_automation_tab(
    admin_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
    stopped_stack_jobs: None,  # noqa: F811
) -> None:
    await create_blog(admin_client, world, file=compose_file())
    rule = await create(
        admin_client,
        world,
        subsystem=RESTARTER,
        target_kind="stack",
        target_name=BLOG,
        schedule_expression="3600",
        param_max_restarts="",
        param_window_seconds="",
    )
    back_to = f"stack:{BLOG}"

    paused = await admin_client.post(
        f"/automation/rules/{rule}/pause",
        data=csrf_form(admin_client, duration="1h", return_to=back_to),
    )
    resumed = await admin_client.post(
        f"/automation/rules/{rule}/resume", data=csrf_form(admin_client, return_to=back_to)
    )

    expected = f"{stacks_path(world)}/{BLOG}?tab=automation"
    assert location(paused) == location(resumed) == expected


async def test_the_swarm_service_automation_tab_holds_the_rules_panel_and_no_other_tab_does(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service = swarm_host.add_service(plain_spec("api"))
    base = f"/environments/{world.remote_id}/swarm/services/{service}"

    pages = {tab: await admin_client.get(f"{base}?tab={tab}") for tab in SERVICE_TABS}
    fragment = await admin_client.get(f"{base}?tab=automation", headers=HTMX)
    other = await admin_client.get(f"{base}?tab=tasks", headers=HTMX)

    assert {t: automation_panel_present(p.text) for t, p in pages.items()} == {
        t: t == "automation" for t in SERVICE_TABS
    }
    assert {t: ADD_AUTOMATION in p.text for t, p in pages.items()} == {
        t: t == "automation" for t in SERVICE_TABS
    }
    assert automation_panel_present(fragment.text) and "<html" not in fragment.text
    assert not automation_panel_present(other.text)


async def test_the_swarm_service_tabs_each_show_only_their_own_events(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service = swarm_host.add_service(plain_spec("api"))
    insert_events(world.app, three_events(world, "service", service))
    base = f"/environments/{world.remote_id}/swarm/services/{service}"

    panels = await panels_by_tab(admin_client, base, SERVICE_TABS)
    fragment = await admin_client.get(f"{base}?tab=access", headers=HTMX)

    assert panels["overview"] is not None
    assert {USER_ACTION, ACCESS_ACTION, SUBSYSTEM_ACTION} <= set(panels["overview"][1])
    assert panels["automation"] == (AUTOMATED, [SUBSYSTEM_ACTION])
    assert panels["access"] == (ACCESS_CHANGES, [ACCESS_ACTION])
    assert (panels["configuration"], panels["tasks"]) == (None, None)
    assert activity_panel_in(fragment.text) == (ACCESS_CHANGES, [ACCESS_ACTION])


async def test_the_managed_swarm_stack_automation_tab_holds_the_rules_panel_and_no_other_tab_does(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world = swarm_stacks_world
    await create_shop(Browser(admin_client), world)
    base = f"{swarm_stacks_path(world)}/{SHOP}"

    pages = {tab: await admin_client.get(f"{base}?tab={tab}") for tab in STACK_TABS}
    other = await admin_client.get(f"{base}?tab=file", headers=HTMX)

    assert {t: automation_panel_present(p.text) for t, p in pages.items()} == {
        t: t == "automation" for t in STACK_TABS
    }
    assert {t: ADD_AUTOMATION in p.text for t, p in pages.items()} == {
        t: t == "automation" for t in STACK_TABS
    }
    assert {"Logs", "Automation"} <= set(tab_names(pages["overview"].text))
    assert not automation_panel_present(other.text)


async def test_the_managed_swarm_stack_tabs_each_show_only_their_own_events(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world = swarm_stacks_world
    await create_shop(Browser(admin_client), world)
    insert_events(world.app, three_events(world, "stack", SHOP))
    base = f"{swarm_stacks_path(world)}/{SHOP}"

    panels = await panels_by_tab(admin_client, base, STACK_TABS)

    assert panels["overview"] is not None
    assert {USER_ACTION, ACCESS_ACTION, SUBSYSTEM_ACTION} <= set(panels["overview"][1])
    assert panels["automation"] == (AUTOMATED, [SUBSYSTEM_ACTION])
    assert panels["access"] == (ACCESS_CHANGES, [ACCESS_ACTION])
    assert (panels["file"], panels["variables"]) == (None, None)


async def test_an_unmanaged_swarm_stack_has_no_automation_tab(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world = swarm_stacks_world
    swarm_host.add_service(namespace_service(SHOP, f"{SHOP}_web"))

    page = await admin_client.get(f"{swarm_stacks_path(world)}/{SHOP}")

    assert page.status_code == 200
    assert "Automation" not in tab_names(page.text)
    assert not automation_panel_present(page.text)
