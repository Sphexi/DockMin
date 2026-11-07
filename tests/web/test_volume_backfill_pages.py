"""The volume owner backfill pages: the review, the confirmed run, and who may reach them."""

from fastapi import FastAPI
from httpx import AsyncClient

from app.db.repositories.resource_control import ResourceControl, ResourceControlRepository
from tests.auth_support import ADMIN_ID, STANDARD_ID, STANDARD_USERNAME, csrf_form
from tests.container_support import share_with_standard_user
from tests.docker_engine.container_engine import ContainerEngine
from tests.object_support import add_container, hidden_values, visible_text, volume_mount
from tests.web.environment_support import World
from tests.web.flash_support import land

OK, SEE_OTHER, NOT_FOUND, CONFLICT = 200, 303, 404, 409
VOLUME = "solo"
OWNER_CONTAINER = 0x71


def backfill_path(world: World) -> str:
    return f"/environments/{world.remote_id}/volumes/-/backfill"


async def control_over(app: FastAPI, environment_id: int, name: str) -> ResourceControl | None:
    async with app.state.database.session() as db:
        return await ResourceControlRepository(db, app.state.auth.clock).get_for(
            environment_id, "volume", name
        )


async def own_container(app: FastAPI, environment_id: int, container: str, owner: int) -> None:
    async with app.state.database.session() as db:
        await ResourceControlRepository(db, app.state.auth.clock).create(
            environment_id,
            "container",
            container,
            is_public=False,
            is_administrators_only=False,
            access_level="write",
            owner_user_id=owner,
        )


async def stock_owned_volume(app: FastAPI, world: World, host: ContainerEngine) -> None:
    host.add_volume(VOLUME)
    box = add_container(host, OWNER_CONTAINER, "owned-web", mounts=[volume_mount(VOLUME)])
    await own_container(app, world.remote_id, box.id, STANDARD_ID)


async def test_a_standard_user_reaches_neither_the_review_nor_the_run(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await share_with_standard_user(world, container_host)
    await stock_owned_volume(world.app, world, container_host)

    review = await standard_client.get(backfill_path(world))
    run = await standard_client.post(
        backfill_path(world), data=csrf_form(standard_client, expected_plan="x")
    )

    assert (review.status_code, run.status_code) == (NOT_FOUND, NOT_FOUND)
    assert await control_over(world.app, world.remote_id, VOLUME) is None


async def test_the_review_names_each_volume_and_owner_and_the_run_assigns_exactly_those(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await stock_owned_volume(world.app, world, container_host)
    container_host.add_volume("contested")
    container_host.add_volume("unmounted")
    container_host.add_volume("_odd name")
    for number, owner in ((0x73, STANDARD_ID), (0x74, ADMIN_ID)):
        box = add_container(
            container_host, number, f"c{number}", mounts=[volume_mount("contested")]
        )
        await own_container(world.app, world.remote_id, box.id, owner)
    list_page = await admin_client.get(f"/environments/{world.remote_id}/volumes")

    review = await admin_client.get(backfill_path(world))
    ran = await admin_client.post(
        backfill_path(world),
        data=csrf_form(admin_client, expected_plan=hidden_values(review, "expected_plan")[0]),
    )
    result = await land(admin_client, ran)

    assert "Assign owners" in visible_text(list_page)
    assert review.status_code == OK
    text = visible_text(review)
    assert f"{VOLUME} owner {STANDARD_USERNAME}" in text
    assert "contested candidates admin-user, standard-user" in text
    assert "unmounted no container mounts it" in text
    assert "1 volume left alone: unreadable" in text and "_odd name" in text
    assert "1 of 1 volume assigned" in visible_text(result)
    control = await control_over(world.app, world.remote_id, VOLUME)
    assert control is not None and control.owner_user_id == STANDARD_ID


async def test_a_run_bound_to_a_plan_that_changed_is_refused_and_writes_nothing(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await stock_owned_volume(world.app, world, container_host)
    review = await admin_client.get(backfill_path(world))
    add_container(container_host, 0x72, "unowned", mounts=[volume_mount(VOLUME)])

    ran = await admin_client.post(
        backfill_path(world),
        data=csrf_form(admin_client, expected_plan=hidden_values(review, "expected_plan")[0]),
    )

    assert review.status_code == OK and ran.status_code == CONFLICT
    assert await control_over(world.app, world.remote_id, VOLUME) is None
