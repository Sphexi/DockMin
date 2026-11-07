"""Removing several images or volumes at once: selection, confirmation, run and result."""

import re

import pytest
from httpx import AsyncClient, Response

from app.services.container_actions import MAX_BATCH
from app.web.image_routes import TARGET_SEPARATOR
from tests.auth_support import csrf_form
from tests.container_support import JOB
from tests.docker_engine.container_engine import ContainerEngine
from tests.image_support import image_id
from tests.object_support import volume_mount
from tests.web.environment_support import World
from tests.web.flash_support import land
from tests.web.helpers import parse_tags
from tests.web.image_support import DANGLING, NGINX, SPARE, hidden_values, links_in, text_of
from tests.web.self_support import SELF_VOLUME, adopt, events, identity_of, refusals

OK, BAD_REQUEST, FORBIDDEN, NOT_FOUND = 200, 400, 403, 404
IDLE = "idle"
USED = "used"
GONE = "ghost"


def images(world: World, suffix: str = "") -> str:
    return f"/environments/{world.remote_id}/images{suffix}"


def volumes(world: World, suffix: str = "") -> str:
    return f"/environments/{world.remote_id}/volumes{suffix}"


def image_query(*numbers: int) -> str:
    return "&".join(f"image={image_id(number)}" for number in numbers)


def volume_query(*names: str) -> str:
    return "&".join(f"volume={name}" for name in names)


def checkboxes(response: Response, name: str) -> list[dict[str, str]]:
    return [
        tag.attrs
        for tag in parse_tags(response.text)
        if tag.name == "input" and tag.attrs.get("type") == "checkbox" and "form" in tag.attrs
        if tag.attrs.get("name") == name
    ]


REQUEST_ID = re.compile(r"[0-9a-f]{32}")


def stable(response: Response) -> str:
    return REQUEST_ID.sub("", response.text)


def posted(response: Response, name: str) -> list[str]:
    return hidden_values(response, name)


async def run(client: AsyncClient, path: str, review: Response, **fields: str) -> Response:
    reviewed = {name: posted(review, name) for name in ("target", "display_name")}
    return await client.post(path, data=csrf_form(client, **fields) | reviewed)


@pytest.fixture
async def volume_host(world: World, container_host: ContainerEngine) -> ContainerEngine:
    """Volumes that are free, mounted by a container, and held back for dockmin's container."""
    container_host.add_volume(IDLE)
    container_host.add_volume(USED)
    container_host.add_volume(SELF_VOLUME)
    container_host.boxes[JOB].mounts = [volume_mount(USED)]
    adopt(world.app, identity_of(world, volumes=[SELF_VOLUME]))
    return container_host


async def test_the_image_list_offers_a_checkbox_per_row_and_a_remove_selected_control(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    page = await admin_client.get(images(world))

    boxes = checkboxes(page, "image")
    assert {box["value"] for box in boxes} >= {image_id(NGINX), image_id(SPARE), image_id(DANGLING)}
    assert {box["form"] for box in boxes} == {"image-batch-form"}
    assert "Remove selected..." in text_of(page)
    assert images(world, "/remove") in page.text


async def test_the_image_confirmation_names_every_target_and_lists_images_in_use_as_kept(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    page = await admin_client.get(f"{images(world, '/remove')}?{image_query(NGINX, SPARE)}")

    assert page.status_code == OK
    assert "spare:1" in text_of(page) and "nginx:1" in text_of(page)
    assert "Images in use are kept" in text_of(page)
    assert "kept: " in text_of(page)
    assert [value.split(":")[0] for value in posted(page, "target")] == [image_id(SPARE)]
    assert posted(page, "display_name") == ["spare:1"]
    assert posted(page, "force") == [] and posted(page, "confirm_force") == []


async def test_running_an_image_removal_removes_each_target_on_its_own_and_records_a_row_each(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    review = await admin_client.get(f"{images(world, '/remove')}?{image_query(SPARE, DANGLING)}")

    ran = await run(admin_client, images(world, "/remove"), review)
    result = await land(admin_client, ran)

    assert text_of(result).count("Removed") >= 2
    assert "2 of 2 images removed" in text_of(result)
    removals = [r for r in daemon.requests if r.method == "DELETE" and r.path.startswith("/images")]
    assert len(removals) == 2
    assert [r.query.get("force") for r in removals] == ["false", "false"]
    recorded = await events(world.app, "action LIKE 'image_%'")
    assert [row["result"] for row in recorded] == ["success", "success"]


async def test_a_forged_force_cannot_remove_an_image_that_a_container_uses(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    forged = f"{image_id(NGINX)}{TARGET_SEPARATOR}{image_id(NGINX)}{TARGET_SEPARATOR}0"

    ran = await admin_client.post(
        images(world, "/remove"),
        data=csrf_form(admin_client, force="1", confirm_force="yes")
        | {"target": [forged], "display_name": ["nginx:1"]},
    )
    result = await land(admin_client, ran)

    assert "0 of 1 image removed" in text_of(result)
    assert "true" not in [r.query.get("force") for r in daemon.calls("DELETE")]
    assert "nginx:1" in daemon.tags_of(f"sha256:{image_id(NGINX)}")


async def test_an_image_selection_over_the_cap_is_refused_before_anything_is_looked_up(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    allowed = await admin_client.get(
        f"{images(world, '/remove')}?{image_query(*range(100, 100 + MAX_BATCH))}"
    )
    refused = await admin_client.get(
        f"{images(world, '/remove')}?{image_query(*range(100, 101 + MAX_BATCH))}"
    )

    assert allowed.status_code == OK
    assert refused.status_code == BAD_REQUEST
    assert f"Select at most {MAX_BATCH} images." in text_of(refused)
    assert posted(refused, "target") == []


async def test_only_an_administrator_may_review_an_image_removal(
    standard_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    review = await standard_client.get(f"{images(world, '/remove')}?{image_query(SPARE)}")
    run_attempt = await standard_client.post(
        images(world, "/remove"),
        data=csrf_form(standard_client) | {"target": [f"{image_id(SPARE)}:{image_id(SPARE)}:0"]},
    )

    assert review.status_code == FORBIDDEN
    assert run_attempt.status_code == FORBIDDEN
    assert daemon.calls("DELETE") == []


async def test_the_volume_list_offers_checkboxes_and_locks_the_volume_dockmin_has_mounted(
    admin_client: AsyncClient, world: World, volume_host: ContainerEngine
) -> None:
    page = await admin_client.get(volumes(world))

    boxes = {box["value"]: box for box in checkboxes(page, "volume")}
    assert {IDLE, USED} <= set(boxes) and SELF_VOLUME not in boxes
    assert {box["form"] for box in boxes.values()} == {"volume-batch-form"}
    disabled = [
        tag.attrs["aria-label"]
        for tag in parse_tags(page.text)
        if tag.name == "input" and "disabled" in tag.attrs and "aria-label" in tag.attrs
    ]
    assert f"{SELF_VOLUME} is in use by dockmin and cannot be selected" in disabled
    assert text_of(page).count("In use by dockmin") == 1
    assert "Remove selected..." in text_of(page)
    assert f"/volumes/{SELF_VOLUME}/remove" not in links_in(page)


async def test_the_volume_confirmation_names_targets_keeps_used_ones_and_leaves_dockmins_out(
    admin_client: AsyncClient, world: World, volume_host: ContainerEngine
) -> None:
    page = await admin_client.get(
        volumes(world, f"/-/remove?{volume_query(IDLE, USED, SELF_VOLUME)}")
    )

    assert page.status_code == OK
    assert "Volumes in use are kept" in text_of(page) and USED in text_of(page)
    assert "Left out" in text_of(page) and SELF_VOLUME in text_of(page)
    assert SELF_VOLUME not in [value.split(":")[0] for value in posted(page, "target")]
    assert posted(page, "force") == []


async def test_the_volume_confirmation_does_not_post_a_volume_that_is_in_use(
    admin_client: AsyncClient, world: World, volume_host: ContainerEngine
) -> None:
    page = await admin_client.get(volumes(world, f"/-/remove?{volume_query(IDLE, USED)}"))

    assert [value.split(":")[0] for value in posted(page, "target")] == [IDLE]


async def test_running_a_volume_removal_removes_each_target_and_records_a_row_each(
    admin_client: AsyncClient, world: World, volume_host: ContainerEngine
) -> None:
    volume_host.add_volume("second")
    review = await admin_client.get(volumes(world, f"/-/remove?{volume_query(IDLE, 'second')}"))

    ran = await run(admin_client, volumes(world, "/-/remove"), review)
    result = await land(admin_client, ran)

    assert "2 of 2 volumes removed" in text_of(result)
    assert not {IDLE, "second"} & set(volume_host.volumes)
    assert {USED, SELF_VOLUME} <= set(volume_host.volumes)
    recorded = await events(world.app, "action LIKE 'volume_%'")
    assert [row["result"] for row in recorded] == ["success", "success"]


async def test_a_forged_target_for_dockmins_volume_is_refused_and_recorded(
    admin_client: AsyncClient, world: World, volume_host: ContainerEngine
) -> None:
    ran = await admin_client.post(
        volumes(world, "/-/remove"),
        data=csrf_form(admin_client) | {"target": [f"{SELF_VOLUME}::0"]},
    )
    result = await land(admin_client, ran)

    assert "Protected" in text_of(result)
    assert SELF_VOLUME in volume_host.volumes
    assert [row["target_id"] for row in await refusals(world.app)] == [SELF_VOLUME]


async def test_prune_lists_dockmins_volume_as_kept_and_does_not_remove_it(
    admin_client: AsyncClient, world: World, volume_host: ContainerEngine
) -> None:
    review = await admin_client.get(volumes(world, "/-/prune?mode=all"))

    ran = await admin_client.post(
        volumes(world, "/-/prune"),
        data=csrf_form(admin_client, mode="all") | {"target": posted(review, "target")},
    )
    await land(admin_client, ran)

    assert "not pruned" in text_of(review) and SELF_VOLUME in text_of(review)
    assert IDLE not in volume_host.volumes
    assert SELF_VOLUME in volume_host.volumes and USED in volume_host.volumes


async def test_the_volume_detail_shows_the_badge_and_offers_no_removal_for_dockmins_volume(
    admin_client: AsyncClient, world: World, volume_host: ContainerEngine
) -> None:
    protected = await admin_client.get(volumes(world, f"/{SELF_VOLUME}"))
    ordinary = await admin_client.get(volumes(world, f"/{IDLE}"))

    assert "In use by dockmin" in text_of(protected)
    assert volumes(world, f"/{SELF_VOLUME}/remove") not in links_in(protected)
    assert volumes(world, f"/{IDLE}/remove") in links_in(ordinary)


async def test_a_volume_selection_over_the_cap_is_refused(
    admin_client: AsyncClient, world: World, volume_host: ContainerEngine
) -> None:
    names = [f"v{number}" for number in range(MAX_BATCH + 1)]

    refused = await admin_client.get(volumes(world, f"/-/remove?{volume_query(*names)}"))

    assert refused.status_code == BAD_REQUEST
    assert f"Select at most {MAX_BATCH} volumes." in text_of(refused)
    assert posted(refused, "target") == []


async def test_an_empty_volume_selection_by_a_standard_user_is_the_same_not_found_as_any_other(
    standard_client: AsyncClient,
    admin_client: AsyncClient,
    world: World,
    volume_host: ContainerEngine,
) -> None:
    empty = await standard_client.get(volumes(world, "/-/remove"))
    empty_run = await standard_client.post(
        volumes(world, "/-/remove"), data=csrf_form(standard_client)
    )
    unknown = await standard_client.get(volumes(world, f"/{GONE}/remove"))
    admin_empty = await admin_client.get(volumes(world, "/-/remove"))

    assert (empty.status_code, empty_run.status_code, unknown.status_code) == (NOT_FOUND,) * 3
    assert stable(empty) == stable(unknown)
    assert admin_empty.status_code == BAD_REQUEST
