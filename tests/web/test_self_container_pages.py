"""The container dockmin runs in, as the container pages show and refuse it."""

import re

import pytest
from httpx import AsyncClient, Response

from tests.auth_support import csrf_form
from tests.container_support import WEB, plant_control
from tests.docker_engine.container_engine import ContainerEngine
from tests.web.environment_support import World
from tests.web.flash_support import land
from tests.web.helpers import parse_tags
from tests.web.image_support import hidden_values, links_in, text_of
from tests.web.self_support import (
    SELF_ID,
    SELF_NAME,
    adopt,
    identity_of,
    refusals,
    seed_self,
)

HTMX = {"HX-Request": "true"}
OK, SEE_OTHER, CONFLICT = 200, 303, 409
BADGE = "This dockmin instance"
PROTECTED = "This is the container dockmin runs in."
REFUSED_ACTIONS = ("stop", "restart", "kill", "pause", "remove")
RECORDED_AS = {
    "stop": "container_stopped",
    "restart": "container_restarted",
    "kill": "container_killed",
    "pause": "container_paused",
    "remove": "container_removed",
}
REFUSED_BUTTONS = {
    f"{label} {SELF_NAME}" for label in ("Stop", "Restart", "Kill", "Pause", "Remove")
}
INSTANCE_LINK = "/server/instance"
SELF_ROW = re.compile(rf'<tr id="container-{SELF_ID}">(.*?)</tr>', re.S)


@pytest.fixture
async def running_self(world: World, container_host: ContainerEngine) -> ContainerEngine:
    """A daemon whose own dockmin container is running, known to the application."""
    seed_self(container_host)
    adopt(world.app, identity_of(world))
    return container_host


def base(world: World) -> str:
    return f"/environments/{world.remote_id}/containers"


def row_of(page: Response, container: str) -> str:
    found = re.search(rf'<tr id="container-{container}">(.*?)</tr>', page.text, re.S)
    assert found is not None, container
    return found.group(1)


def disabled_labels(html: str) -> set[str]:
    return {
        tag.attrs["aria-label"]
        for tag in parse_tags(html)
        if tag.name == "button" and "disabled" in tag.attrs and "aria-label" in tag.attrs
    }


def hrefs(html: str) -> list[str]:
    return [
        tag.attrs["href"] for tag in parse_tags(html) if tag.name == "a" and "href" in tag.attrs
    ]


def checkboxes(html: str) -> list[dict[str, str]]:
    return [
        tag.attrs
        for tag in parse_tags(html)
        if tag.name == "input" and tag.attrs.get("type") == "checkbox"
    ]


async def test_the_list_badges_the_row_and_renders_the_refused_actions_disabled(
    admin_client: AsyncClient, world: World, running_self: ContainerEngine
) -> None:
    page = await admin_client.get(base(world))

    row = row_of(page, SELF_ID)
    other = row_of(page, WEB)
    assert BADGE in text_of(page) and BADGE in row
    assert disabled_labels(row) == REFUSED_BUTTONS | {f"Recreate {SELF_NAME}"}
    assert not [href for href in hrefs(row) if "/actions/" in href or "/recreate" in href]
    assert disabled_labels(other) == set()
    assert INSTANCE_LINK in links_in(page)


async def test_the_list_disables_the_batch_checkbox_of_dockmin_with_an_accessible_name(
    admin_client: AsyncClient, world: World, running_self: ContainerEngine
) -> None:
    page = await admin_client.get(base(world))

    [own] = checkboxes(row_of(page, SELF_ID))
    [other] = checkboxes(row_of(page, WEB))
    assert "disabled" in own and "name" not in own and "value" not in own
    assert own["aria-label"] == f"{SELF_NAME} is this dockmin instance and cannot be selected"
    assert "disabled" not in other and other["name"] == "container" and other["value"] == WEB


async def test_the_detail_page_badges_the_container_and_disables_the_refused_actions(
    admin_client: AsyncClient, world: World, running_self: ContainerEngine
) -> None:
    page = await admin_client.get(f"{base(world)}/{SELF_ID}")

    assert page.status_code == OK
    assert BADGE in text_of(page)
    assert PROTECTED in text_of(page)
    assert disabled_labels(page.text) >= REFUSED_BUTTONS
    assert not [href for href in hrefs(page.text) if "/actions/" in href or "/recreate" in href]
    assert INSTANCE_LINK in links_in(page)


async def test_a_standard_user_sees_the_same_marks_without_a_link_to_the_instance_page(
    standard_client: AsyncClient, world: World, running_self: ContainerEngine
) -> None:
    await plant_control(
        world.app, world.remote_id, "container", SELF_ID, public=True, level="write"
    )

    page = await standard_client.get(base(world))

    row = row_of(page, SELF_ID)
    assert BADGE in row
    assert disabled_labels(row) >= REFUSED_BUTTONS
    assert INSTANCE_LINK not in links_in(page)


async def test_the_confirmation_of_every_refused_action_and_of_a_recreate_answers_409(
    admin_client: AsyncClient, world: World, running_self: ContainerEngine
) -> None:
    pages = {
        action: await admin_client.get(f"{base(world)}/{SELF_ID}/actions/{action}")
        for action in REFUSED_ACTIONS
    }
    recreate = await admin_client.get(f"{base(world)}/{SELF_ID}/recreate")
    fragment = await admin_client.get(f"{base(world)}/{SELF_ID}/actions/stop", headers=HTMX)

    assert {action: page.status_code for action, page in pages.items()} == dict.fromkeys(
        REFUSED_ACTIONS, CONFLICT
    )
    assert all(PROTECTED in text_of(page) for page in pages.values())
    assert recreate.status_code == CONFLICT and PROTECTED in text_of(recreate)
    assert fragment.status_code == CONFLICT and PROTECTED in text_of(fragment)
    assert running_self.mutations() == []


async def test_a_posted_refused_action_answers_409_and_records_a_self_protected_row(
    admin_client: AsyncClient, world: World, running_self: ContainerEngine
) -> None:
    results = {
        action: await admin_client.post(
            f"{base(world)}/{SELF_ID}/actions/{action}",
            data=csrf_form(admin_client, expected_name=SELF_NAME),
        )
        for action in REFUSED_ACTIONS
    }

    recorded = await refusals(world.app)
    assert {action: result.status_code for action, result in results.items()} == dict.fromkeys(
        REFUSED_ACTIONS, CONFLICT
    )
    assert all(PROTECTED in text_of(result) for result in results.values())
    assert [(row["action"], row["result"]) for row in recorded] == [
        (RECORDED_AS[action], "failure") for action in REFUSED_ACTIONS
    ]
    assert {row["target_id"] for row in recorded} == {SELF_ID}
    assert running_self.mutations() == []


async def test_a_recreate_of_dockmin_is_refused_with_a_row_and_changes_nothing(
    admin_client: AsyncClient, world: World, running_self: ContainerEngine
) -> None:
    refused = await admin_client.post(
        f"{base(world)}/{SELF_ID}/recreate",
        data=csrf_form(admin_client, expected_name=SELF_NAME),
    )

    assert refused.status_code == CONFLICT and PROTECTED in text_of(refused)
    assert [row["action"] for row in await refusals(world.app)] == ["container_recreated"]
    assert running_self.mutations() == []


async def test_a_stopped_dockmin_container_can_still_be_started(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seed_self(container_host, state="exited")
    adopt(world.app, identity_of(world))

    confirm = await admin_client.get(f"{base(world)}/{SELF_ID}/actions/start")
    started = await admin_client.post(
        f"{base(world)}/{SELF_ID}/actions/start", data=csrf_form(admin_client)
    )

    assert confirm.status_code == OK
    assert started.status_code == SEE_OTHER
    assert container_host.calls("POST", f"/containers/{SELF_ID}/start") != []
    assert await refusals(world.app) == []


async def test_a_batch_with_dockmin_reports_it_protected_and_acts_on_the_others(
    admin_client: AsyncClient, world: World, running_self: ContainerEngine
) -> None:
    review = await admin_client.get(
        f"{base(world)}/batch?action=stop&container={SELF_ID}&container={WEB}"
    )
    run = await admin_client.post(
        f"{base(world)}/batch",
        data=csrf_form(admin_client, action="stop", container=[SELF_ID, WEB]),
    )
    result = await land(admin_client, run)

    assert review.status_code == OK
    assert hidden_values(review, "container") == [WEB]
    assert "the one dockmin runs in" in text_of(review)
    assert "Protected" in text_of(result) and "Done" in text_of(result)
    assert running_self.calls("POST", f"/containers/{WEB}/stop") != []
    assert running_self.calls("POST", f"/containers/{SELF_ID}/stop") == []
    assert [row["target_id"] for row in await refusals(world.app)] == [SELF_ID]


async def test_a_terminal_request_in_dockmin_is_refused_with_a_row_and_opens_nothing(
    admin_client: AsyncClient, world: World, running_self: ContainerEngine
) -> None:
    refused = await admin_client.post(
        f"{base(world)}/{SELF_ID}/terminal", data=csrf_form(admin_client, command="/bin/sh")
    )

    assert refused.status_code == CONFLICT
    assert "A terminal cannot be opened in it" in text_of(refused)
    [row] = await refusals(world.app)
    assert row["target_id"] == SELF_ID and row["result"] == "failure"
    assert [r for r in running_self.requests if "/exec" in r.path or "/attach" in r.path] == []
