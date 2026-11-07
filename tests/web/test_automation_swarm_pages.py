"""The Automation pages for container-updater on Swarm: targets, confirmations and the panels."""

import re
from typing import Any

import pytest
from httpx import AsyncClient

from app.subsystems.base import TargetPreview
from app.subsystems.container_updater import ContainerUpdater
from tests.auth_support import csrf_form
from tests.docker_engine.swarm_engine import SwarmEngine, plain_spec
from tests.job_support import execute
from tests.web.environment_support import World
from tests.web.helpers import inline_code_in, inputs_named, parse_tags, value_of
from tests.web.test_automation_pages import location
from tests.web.test_automation_updater_pages import (
    OK,
    SEE_OTHER,
    UNPROCESSABLE,
    UPDATE_MODE,
    Maker,
    add_run,
    enable,
    form,
    named,
    visible,
)
from tests.web.test_swarm_stack_pages import NAME, Browser, base, create_shop

UPDATER = "container-updater"
HEALTH = "container-health-checker"
RESTARTER = "stack-restarter"
SERVICE_NAME = "web"
AUTOMATION_PANEL = 'class="automation-panel"'
SERVICE_SENTENCES = (
    "The service is updated in place by the rolling update its own update settings define. "
    "Swarm replaces its tasks across the nodes.",
    "An update that pauses or leaves fewer tasks running is rolled back to the service's "
    "previous configuration.",
    "A service bound to an Amazon ECR credential set has its stored registry credential renewed "
    "every 6 hours without replacing tasks.",
    "Nothing is removed. Images stay on the nodes.",
)
STACK_SENTENCES = (
    "The stack is redeployed from its deployed file; only services whose image changed are "
    "updated. A redeploy that would create or remove anything, or overwrite an edit made on a "
    "service page, is refused. A redeploy is not rolled back.",
    "Nothing is removed. Images stay on the nodes.",
)
COMPOSE_LINE = "Matching containers are recreated"
SWARM_HINT = "If the name is a Swarm stack instead"
DRY_RUN_SERVICE = (
    "A dry run lists which services would be updated and which registry credentials would be "
    "renewed, and changes no service, credential or automation."
)
DRY_RUN_STACK = "A dry run reports whether the stack would be redeployed, and changes no stack"


@pytest.fixture
def make(admin_client: AsyncClient, swarm_stacks_world: World) -> Maker:
    return Maker(admin_client, swarm_stacks_world)


def options_of(html: str, select: str) -> list[tuple[str, str]]:
    block = re.search(rf'<select[^>]*name="{select}".*?</select>', html, flags=re.S)
    assert block is not None, select
    return re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', block.group(0))


def names_of(html: str) -> set[str]:
    return {tag.attrs["name"] for tag in parse_tags(html) if "name" in tag.attrs}


def datalist(html: str) -> list[str]:
    return re.findall(r'<option value="([^"]*)"></option>', html)


def service_path(world: World, service_id: str) -> str:
    return f"/environments/{world.remote_id}/swarm/services/{service_id}"


async def new_form(client: AsyncClient, world: World, subsystem: str, query: str = "") -> str:
    page = await client.get(
        f"/automation/rules/new?subsystem={subsystem}&environment_id={world.remote_id}{query}"
    )
    assert page.status_code == OK, subsystem
    assert inline_code_in(page.text) == []
    return page.text


async def test_the_updater_offers_the_service_and_stack_kinds_and_the_others_stay_with_compose(
    admin_client: AsyncClient, swarm_stacks_world: World
) -> None:
    world = swarm_stacks_world
    await create_shop(Browser(admin_client), world)

    updater = await new_form(admin_client, world, UPDATER)
    health = await new_form(admin_client, world, HEALTH)
    restarter = await new_form(admin_client, world, RESTARTER)

    assert options_of(updater, "target_kind") == [
        ("container", "Container"),
        ("stack", "Stack"),
        ("labels", "Containers with labels"),
        ("service", "Swarm service"),
    ]
    for page in (updater, health, restarter):
        assert len(inputs_named(page, "target_name")) == 1
        assert not any(
            f"target_{kind}" in names_of(page) for kind in ("container", "stack", "service")
        )
    assert ("stack", "Compose stack") in options_of(health, "target_kind")
    assert ("stack", "Compose stack") in options_of(restarter, "target_kind")
    assert NAME not in datalist(updater) and NAME not in datalist(health)


async def test_the_form_opens_on_a_service_from_the_service_page(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    page = await new_form(
        admin_client,
        swarm_stacks_world,
        UPDATER,
        f"&target_kind=service&target={SERVICE_NAME}",
    )

    assert f'value="{SERVICE_NAME}"' in page
    assert re.search(r'<option value="service" selected', page)


async def test_the_form_reads_nothing_from_the_host_and_points_the_name_field_at_the_suggestions(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    await create_shop(Browser(admin_client), swarm_stacks_world)
    before = len(swarm_host.requests)

    page = await new_form(admin_client, swarm_stacks_world, UPDATER)

    assert [r for r in swarm_host.requests[before:] if r.path.startswith("/services")] == []
    assert f"/environments/{swarm_stacks_world.remote_id}/suggestions/rule-targets?" in page
    assert datalist(page) == []


async def test_a_service_rule_is_confirmed_in_swarm_words_and_shown_on_the_service_page(
    admin_client: AsyncClient,
    swarm_stacks_world: World,
    swarm_host: SwarmEngine,
    make: Maker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = swarm_stacks_world
    service_id = swarm_host.add_service(plain_spec(SERVICE_NAME, replicas=2))

    async def preview(*arguments: object) -> TargetPreview:
        return TargetPreview((SERVICE_NAME,), 0, "2 replicas")

    monkeypatch.setattr(ContainerUpdater, "preview_targets", preview)
    rule_id = await make(named(UPDATE_MODE, SERVICE_NAME), target_kind="service", target_labels="")

    detail = await admin_client.get(f"/automation/rules/{rule_id}")
    review = await admin_client.get(f"/automation/rules/{rule_id}/enable")
    await enable(admin_client, rule_id)
    run = await admin_client.get(f"/automation/rules/{rule_id}/run")
    page = await admin_client.get(f"{service_path(world, service_id)}?tab=automation")
    fragment = await admin_client.get(
        f"{service_path(world, service_id)}?tab=configuration", headers={"HX-Request": "true"}
    )
    boosted = await admin_client.get(
        f"{service_path(world, service_id)}?tab=automation",
        headers={"HX-Request": "true", "HX-Boosted": "true"},
    )
    paused = await admin_client.post(
        f"/automation/rules/{rule_id}/pause",
        data=csrf_form(admin_client, duration="1h", return_to=f"service:{service_id}"),
    )

    assert "Swarm service web" in visible(detail.text)
    for confirmation in (review, run):
        text = visible(confirmation.text)
        assert confirmation.status_code == OK and "web 2 replicas" in text
        assert all(sentence in text for sentence in SERVICE_SENTENCES)
        assert COMPOSE_LINE not in text and inline_code_in(confirmation.text) == []
    assert DRY_RUN_SERVICE in visible(run.text)
    text = visible(page.text)
    assert "Automation" in text and "Swarm service web" in text
    assert f'value="service:{service_id}"' in page.text
    assert "target_kind=service" in page.text
    assert AUTOMATION_PANEL not in fragment.text and "<html" not in fragment.text
    assert "<html" in boosted.text and AUTOMATION_PANEL in boosted.text
    assert paused.status_code == SEE_OTHER
    assert location(paused).endswith(f"/swarm/services/{service_id}?tab=automation")


async def test_a_swarm_stack_rule_is_confirmed_in_swarm_words_and_shown_on_the_stack_page(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine, make: Maker
) -> None:
    world = swarm_stacks_world
    await create_shop(Browser(admin_client), world)
    rule_id = await make(named(UPDATE_MODE, NAME), target_kind="stack", target_labels="")

    review = await admin_client.get(f"/automation/rules/{rule_id}/enable")
    await enable(admin_client, rule_id)
    run = await admin_client.get(f"/automation/rules/{rule_id}/run")
    page = await admin_client.get(f"{base(world)}/{NAME}?tab=automation")
    fragment = await admin_client.get(
        f"{base(world)}/{NAME}?tab=variables", headers={"HX-Request": "true"}
    )
    paused = await admin_client.post(
        f"/automation/rules/{rule_id}/pause",
        data=csrf_form(admin_client, duration="1h", return_to=f"swarm_stack:{NAME}"),
    )
    ignored = await admin_client.post(
        f"/automation/rules/{rule_id}/resume",
        data=csrf_form(admin_client, return_to="swarm_stack:../x"),
    )

    for confirmation in (review, run):
        text = visible(confirmation.text)
        assert confirmation.status_code == OK and f"Stack {NAME}" in text
        assert all(sentence in text for sentence in STACK_SENTENCES)
        assert COMPOSE_LINE not in text and SWARM_HINT not in text
    assert DRY_RUN_STACK in visible(run.text)
    text = visible(page.text)
    assert "Automation" in text and f"Stack {NAME}" in text
    assert f'value="swarm_stack:{NAME}"' in page.text and "target_kind=stack" in page.text
    assert AUTOMATION_PANEL not in fragment.text
    assert paused.status_code == SEE_OTHER
    assert location(paused).endswith(f"/swarm/stacks/{NAME}?tab=automation")
    assert "/automation/rules/" in location(ignored)


async def test_a_stack_name_no_stack_has_shows_the_compose_lines_and_a_hint_about_swarm(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine, make: Maker
) -> None:
    rule_id = await make(named(UPDATE_MODE, "ghost"), target_kind="stack", target_labels="")
    await enable(admin_client, rule_id)

    text = visible((await admin_client.get(f"/automation/rules/{rule_id}/run")).text)

    assert "recreated" in text and SWARM_HINT in text


async def test_the_overview_counts_a_swarm_runs_updates_rollbacks_and_renewals(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine, make: Maker
) -> None:
    world = swarm_stacks_world
    updating = await make(named(UPDATE_MODE, SERVICE_NAME), target_kind="service", target_labels="")
    reporting = await make(
        named([], "api"), target_kind="service", target_labels="", name="Reporter"
    )
    await add_run(
        world,
        updating,
        {"mode": "update", "updated": 2, "rolled_back": 1, "refreshed": 3, "unknown": 1},
    )
    await add_run(world, reporting, {"mode": "report", "update_available": 2, "refreshed": 1})

    text = visible((await admin_client.get("/automation")).text)

    assert "2 updated" in text and "1 rolled back" in text and "3 credentials renewed" in text
    assert "2 updates available" in text and "1 unknown" in text
    assert "1 credential renewed" not in text


async def test_the_edit_form_keeps_the_service_and_a_bad_service_is_refused_with_its_value(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine, make: Maker
) -> None:
    world = swarm_stacks_world
    rule_id = await make(named(UPDATE_MODE, SERVICE_NAME), target_kind="service", target_labels="")

    edit = await admin_client.get(f"/automation/rules/{rule_id}/edit")
    bad = await admin_client.post(
        "/automation/rules",
        data=form(
            admin_client,
            world,
            target_kind="service",
            target_name="bad name!",
            target_labels="",
            name="Bad",
        ),
    )

    assert value_of(edit.text, "target_name") == SERVICE_NAME
    assert re.search(r'<option value="service" selected', edit.text)
    assert bad.status_code == UNPROCESSABLE and 'value="bad name!"' in bad.text


async def test_a_service_rule_in_an_environment_that_is_not_a_manager_is_refused(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world = swarm_stacks_world
    data: dict[str, Any] = form(
        admin_client,
        world,
        environment_id=str(world.local_id),
        target_kind="service",
        target_name=SERVICE_NAME,
        target_labels="",
        name="Local",
    )

    refused = await admin_client.post("/automation/rules", data=data)

    assert refused.status_code == UNPROCESSABLE
    assert "This environment is not a Swarm manager." in visible(refused.text)


EXPORTER = "container-log-exporter"
SERVICE_LABELS = ("service_labels", "Services with labels")
NAMESPACE_NOTE = "com.docker.stack.namespace=&lt;name&gt;"
EXPORTER_TARGET = (
    "INSERT INTO object_storage_target (name, endpoint_url, region, bucket, access_key_id, "
    "secret_access_key_ciphertext, use_path_style, verify_tls, backup_prefix, "
    "activity_log_prefix, created_at, updated_at) VALUES ('b', 'https://s3.example', "
    "'us-east-1', 'b', 'AK', 'v1:x', 1, 1, 'backups', 'logs', 'x', 'x')"
)
LABEL_LINES = "tier=web\napp"
SORTED_LINES = "app\ntier=web"


def exporter_form(client: AsyncClient, world: World, **changes: str) -> dict[str, Any]:
    return form(
        client,
        world,
        subsystem=EXPORTER,
        name="Ship service logs",
        target_kind="service_labels",
        target_labels=LABEL_LINES,
        param_object_storage_target_id="1",
        **changes,
    )


async def test_the_log_exporter_offers_services_with_labels_only_on_a_manager(
    admin_client: AsyncClient, swarm_stacks_world: World
) -> None:
    world = swarm_stacks_world

    manager = await new_form(admin_client, world, EXPORTER)
    standalone = await admin_client.get(
        f"/automation/rules/new?subsystem={EXPORTER}&environment_id={world.local_id}"
    )

    assert SERVICE_LABELS in options_of(manager, "target_kind")
    assert NAMESPACE_NOTE in manager
    assert SERVICE_LABELS not in options_of(standalone.text, "target_kind")
    assert NAMESPACE_NOTE not in standalone.text


async def test_a_services_with_labels_rule_is_saved_shown_again_and_refused_off_a_manager(
    admin_client: AsyncClient, swarm_stacks_world: World
) -> None:
    world = swarm_stacks_world
    await execute(world.app, EXPORTER_TARGET)

    created = await admin_client.post("/automation/rules", data=exporter_form(admin_client, world))
    edit = await admin_client.get(f"{location(created).split('?')[0]}/edit")
    refused = await admin_client.post(
        "/automation/rules",
        data=exporter_form(admin_client, world, environment_id=str(world.local_id)),
    )

    assert re.search(r'<option value="service_labels" selected', edit.text)
    assert f"{SORTED_LINES}</textarea>" in edit.text
    assert refused.status_code == UNPROCESSABLE
    assert "This environment is not a Swarm manager." in visible(refused.text)
