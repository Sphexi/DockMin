"""Container create and recreate as jobs: redirect, job page and stream, cancel, visibility."""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient, Response

from app.auth.errors import PermissionDenied
from app.services import container_jobs
from tests.access_support import ADMIN, STANDARD
from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import ContainerEngine, created_id
from tests.provisioning_support import IMAGE, OLD, create_form, seed_recreatable
from tests.stream_support import read_stream
from tests.web.environment_support import World
from tests.web.registry_support import visible_text

SEE_OTHER, NOT_FOUND, TOO_MANY = 303, 404, 429
NEW = created_id(1)
PULLED_IMAGE = "ghcr.io/org/new:2"
OTHER_IMAGE = "ghcr.io/org/other:3"
LAYER = "a1b2c3d4e5f6"
LAYER_LINES: list[dict[str, Any]] = [
    {"status": "Pulling fs layer", "id": LAYER},
    {"status": "Downloading", "id": LAYER, "progressDetail": {"current": 5, "total": 10}},
    {"status": "Pull complete", "id": LAYER},
]
JOB = re.compile(r"/(?:containers|images)/jobs/([A-Za-z0-9_-]{22})$")
CONTAINER_JOB = re.compile(r"/containers/jobs/[A-Za-z0-9_-]{22}$")
WAIT_SECONDS = 5
RECHECK = 0.01
NAME = "recreate-me"
CREATED = "fresh"


def containers_path(world: World) -> str:
    return f"/environments/{world.remote_id}/containers"


def job_url(world: World, job_id: str, suffix: str = "") -> str:
    return f"{containers_path(world)}/jobs/{job_id}{suffix}"


def job_of(response: Response) -> str:
    assert response.status_code == SEE_OTHER, response.text
    found = JOB.search(response.headers["location"])
    assert found is not None, response.headers["location"]
    return found.group(1)


def text_of(response: Response) -> str:
    return " ".join(visible_text(response.text).split())


async def start_create(
    client: AsyncClient, world: World, image: str = PULLED_IMAGE, **fields: str
) -> Response:
    form = csrf_form(client, **create_form(image=image, **fields))
    return await client.post(containers_path(world), data=form)


async def start_recreate(client: AsyncClient, world: World, container: str = OLD) -> Response:
    form = csrf_form(client, expected_name=NAME, pull="on")
    return await client.post(f"{containers_path(world)}/{container}/recreate", data=form)


async def settled(world: World, job_id: str) -> None:
    task = world.app.state.engine.image_jobs._jobs[job_id].task
    await asyncio.wait_for(asyncio.wait({task}), WAIT_SECONDS)


async def event_rows(world: World, action: str) -> list[tuple[str, dict[str, Any]]]:
    async with world.app.state.database.session() as db:
        rows = await db.fetch_limited(
            "SELECT result, detail FROM activity_event WHERE action = ? ORDER BY id",
            (action,),
            limit=20,
        )
    return [(row["result"], json.loads(row["detail"])) for row in rows]


@pytest.fixture(autouse=True)
async def stopped_jobs(world: World, container_host: ContainerEngine) -> AsyncIterator[None]:
    """Release the daemon and end every job still running before the application closes."""
    yield
    container_host.release_gates()
    registry = world.app.state.engine.image_jobs
    tasks = [job.task for job in registry._jobs.values() if job.task and not job.task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks)


async def test_a_create_that_pulls_answers_303_to_a_job_page_with_the_layers_then_the_container(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.pull_lines = LAYER_LINES
    gate = container_host.hold("create")

    posted = await start_create(admin_client, world, name=CREATED)
    job_id = job_of(posted)
    await asyncio.wait_for(gate.entered.wait(), WAIT_SECONDS)
    during = await admin_client.get(job_url(world, job_id))
    streamed = await read_stream(
        world.app, admin_client, job_url(world, job_id, "/events"), stop_after_events=1
    )
    gate.release.set()
    await settled(world, job_id)
    after = await admin_client.get(job_url(world, job_id))
    last = await read_stream(world.app, admin_client, job_url(world, job_id, "/events"))

    assert posted.headers["cache-control"] == "no-store"
    assert "Running" in text_of(during) and LAYER in during.text
    assert "Pull the image" in text_of(during) and "Create the container" in text_of(during)
    assert f'data-job-stream="{job_url(world, job_id, "/events")}"' in during.text
    assert f'action="{job_url(world, job_id, "/cancel")}"' not in during.text
    assert streamed.body.startswith("event: progress\ndata: ") and LAYER in streamed.body
    assert "Succeeded" in text_of(after) and "Container created and started." in text_of(after)
    assert f"{containers_path(world)}/{NEW}" in after.text
    assert "data-job-stream" not in after.text
    assert last.body.startswith("event: done\ndata: ") and last.body.count("event: ") == 1
    assert container_host.verbs() == ["pull", "create", "start"]
    assert container_host.boxes[NEW].name == CREATED


async def test_a_pulling_create_writes_one_row_with_the_detail_a_synchronous_create_writes(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.add_image(IMAGE)
    synchronous = await start_create(admin_client, world, IMAGE)
    jobs_after_synchronous = len(world.app.state.engine.image_jobs)
    pulling = await start_create(admin_client, world, PULLED_IMAGE)
    await settled(world, job_of(pulling))

    [(first_result, first), (second_result, second)] = await event_rows(world, "container_created")

    assert (
        synchronous.status_code == SEE_OTHER
        and CONTAINER_JOB.search(synchronous.headers["location"]) is None
    )
    assert jobs_after_synchronous == 0
    assert (first_result, second_result) == ("success", "success")
    assert (first["pulled"], second["pulled"]) == (False, True)
    assert second["image"] == PULLED_IMAGE and second["started"] is True
    assert set(second) - set(first) == {"anonymous", "credential_source", "binding"}


async def test_a_finished_create_job_repeats_the_warning_a_synchronous_result_shows(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.fail("start", 500)

    job_id = job_of(await start_create(admin_client, world))
    await settled(world, job_id)

    page = await admin_client.get(job_url(world, job_id))
    assert "Succeeded" in text_of(page)
    assert "Warning: Created, but Docker could not start it." in text_of(page)
    assert f"{containers_path(world)}/{NEW}" in page.text


async def test_a_create_that_does_not_pull_stays_synchronous(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.add_image(IMAGE)

    posted = await start_create(admin_client, world, IMAGE)

    assert posted.status_code == SEE_OTHER
    assert "/jobs/" not in posted.headers["location"]
    assert posted.headers["location"].startswith(f"{containers_path(world)}/{NEW}?created=started")
    assert len(world.app.state.engine.image_jobs) == 0
    assert container_host.verbs() == ["create", "start"]


async def test_a_refused_pulling_create_shows_the_form_in_place_and_starts_no_job(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    posted = await start_create(admin_client, world, PULLED_IMAGE, pull="never")
    invalid = await admin_client.post(
        containers_path(world), data=csrf_form(admin_client, **create_form(image=""))
    )

    assert posted.status_code == 400 and invalid.status_code == 400
    assert len(world.app.state.engine.image_jobs) == 0
    assert container_host.mutations() == []


async def test_a_pull_that_fails_ends_the_job_failed_with_no_registry_text_and_one_failure_row(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.pull_status = 500

    job_id = job_of(await start_create(admin_client, world))
    await settled(world, job_id)

    page = await admin_client.get(job_url(world, job_id))
    [(result, detail)] = await event_rows(world, "container_created")
    assert "Failed" in text_of(page) and "The image could not be pulled." in text_of(page)
    assert "registry detail" not in page.text
    assert (result, detail["reason"]) == ("failure", "pull_failed")
    assert NEW not in container_host.boxes


async def test_cancelling_during_the_pull_leaves_no_container_and_one_cancelled_failure_row(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    gate = container_host.hold("pull")
    job_id = job_of(await start_create(admin_client, world))
    await asyncio.wait_for(gate.entered.wait(), WAIT_SECONDS)
    running = await admin_client.get(job_url(world, job_id))

    cancelled = await admin_client.post(
        job_url(world, job_id, "/cancel"), data=csrf_form(admin_client)
    )
    await settled(world, job_id)

    page = await admin_client.get(job_url(world, job_id))
    [(result, detail)] = await event_rows(world, "container_created")
    assert f'action="{job_url(world, job_id, "/cancel")}"' in running.text
    assert cancelled.status_code == SEE_OTHER and cancelled.headers["location"] == job_url(
        world, job_id
    )
    assert "Cancelled" in text_of(page) and "No container was created." in text_of(page)
    assert (result, detail["reason"]) == ("failure", "cancelled")
    assert container_host.verbs() == ["pull"]
    assert NEW not in container_host.boxes


async def test_cancelling_after_the_pull_changes_nothing_and_the_container_is_created(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    gate = container_host.hold("create")
    job_id = job_of(await start_create(admin_client, world))
    await asyncio.wait_for(gate.entered.wait(), WAIT_SECONDS)

    cancelled = await admin_client.post(
        job_url(world, job_id, "/cancel"), data=csrf_form(admin_client)
    )
    still_running = world.app.state.engine.image_jobs.view(job_id, ADMIN).state
    gate.release.set()
    await settled(world, job_id)

    page = await admin_client.get(job_url(world, job_id))
    rows = await event_rows(world, "container_created")
    assert cancelled.status_code == SEE_OTHER
    assert still_running == "running"
    assert "Succeeded" in text_of(page)
    assert [(result, detail.get("reason")) for result, detail in rows] == [("success", None)]
    assert container_host.verbs() == ["pull", "create", "start"]


async def test_a_starter_disabled_during_the_pull_ends_the_job_before_the_container_is_created(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revoked = asyncio.Event()

    class Recorded(container_jobs._StarterGuard):
        @property
        def revoked(self) -> bool:
            return revoked.is_set()

        @revoked.setter
        def revoked(self, value: bool) -> None:
            if value:
                revoked.set()

    monkeypatch.setattr(container_jobs, "_StarterGuard", Recorded)
    monkeypatch.setattr(container_jobs, "STARTER_RECHECK_SECONDS", RECHECK)
    gate = container_host.hold("pull")
    job_id = job_of(await start_create(admin_client, world))
    await asyncio.wait_for(gate.entered.wait(), WAIT_SECONDS)

    async with world.app.state.database.session() as db:
        await db.execute("UPDATE user SET is_enabled = 0 WHERE id = ?", (ADMIN.user_id,))
    await asyncio.wait_for(revoked.wait(), WAIT_SECONDS)
    gate.release.set()
    await settled(world, job_id)

    view = world.app.state.engine.image_jobs.view(job_id, ADMIN)
    assert (view.state, view.outcome.reason) == ("failed", "forbidden")
    assert view.outcome.message == "You no longer have permission for this action."
    assert NEW not in container_host.boxes


@pytest.mark.parametrize(
    "change",
    [
        pytest.param("is_enabled = 0", id="disabled"),
        pytest.param("role = 'standard'", id="demoted"),
    ],
)
async def test_a_job_whose_starter_is_disabled_or_demoted_is_refused_at_its_start(
    world: World, change: str
) -> None:
    async with world.app.state.database.session() as db:
        await db.execute(f"UPDATE user SET {change} WHERE id = ?", (ADMIN.user_id,))

    with pytest.raises(PermissionDenied):
        await container_jobs._current_principal(world.app, ADMIN)


async def test_a_job_starts_as_the_account_stands_now(world: World) -> None:
    async with world.app.state.database.session() as db:
        await db.execute("UPDATE user SET role = 'administrator' WHERE id = ?", (STANDARD.user_id,))

    current = await container_jobs._current_principal(world.app, STANDARD)

    assert current.is_administrator


async def test_a_recreate_that_pulls_runs_as_a_job_through_each_step_to_the_new_container(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seed_recreatable(container_host, name=NAME)
    container_host.pull_lines = LAYER_LINES

    job_id = job_of(await start_recreate(admin_client, world))
    await settled(world, job_id)

    page = await admin_client.get(job_url(world, job_id))
    [(result, detail)] = await event_rows(world, "container_recreated")
    assert container_host.verbs() == ["pull", "stop", "rename", "create", "start", "remove"]
    assert "Succeeded" in text_of(page) and "Container recreated and started." in text_of(page)
    for step in ("Pull the image", "Stop the container", "Create the container"):
        assert step in text_of(page)
    assert f"{containers_path(world)}/{NEW}" in page.text
    assert (result, detail["pulled"], detail["new_container_id"]) == ("success", True, NEW)


async def test_a_recreate_job_whose_create_is_refused_restores_the_original_and_says_so(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seed_recreatable(container_host, name=NAME)
    container_host.fail("create", 400)

    job_id = job_of(await start_recreate(admin_client, world))
    await settled(world, job_id)

    page = await admin_client.get(job_url(world, job_id))
    [(result, detail)] = await event_rows(world, "container_recreated")
    assert "Failed" in text_of(page) and "The original container was restored." in text_of(page)
    assert "Restore the original container" in text_of(page)
    assert (container_host.boxes[OLD].name, container_host.boxes[OLD].state) == (NAME, "running")
    assert (result, detail["reason"], detail["rolled_back"]) == (
        "failure",
        "engine_rejected",
        True,
    )


async def test_cancelling_a_recreate_during_the_pull_leaves_the_container_as_it_was(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seed_recreatable(container_host, name=NAME)
    gate = container_host.hold("pull")
    job_id = job_of(await start_recreate(admin_client, world))
    await asyncio.wait_for(gate.entered.wait(), WAIT_SECONDS)

    await admin_client.post(job_url(world, job_id, "/cancel"), data=csrf_form(admin_client))
    await settled(world, job_id)

    [(result, detail)] = await event_rows(world, "container_recreated")
    assert (result, detail["reason"]) == ("failure", "cancelled")
    assert container_host.verbs() == ["pull"]
    assert (container_host.boxes[OLD].name, container_host.boxes[OLD].state) == (NAME, "running")


async def test_a_standard_user_cannot_see_stream_or_cancel_another_users_job(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
) -> None:
    gate = container_host.hold("pull")
    job_id = job_of(await start_create(admin_client, world))
    await asyncio.wait_for(gate.entered.wait(), WAIT_SECONDS)

    page = await standard_client.get(job_url(world, job_id))
    stream = await standard_client.get(job_url(world, job_id, "/events"))
    cancel = await standard_client.post(
        job_url(world, job_id, "/cancel"), data=csrf_form(standard_client)
    )
    by_owner = await admin_client.get(job_url(world, job_id))

    assert (page.status_code, stream.status_code, cancel.status_code) == (NOT_FOUND,) * 3
    assert by_owner.status_code == 200
    assert world.app.state.engine.image_jobs.running_count() == 1


async def test_a_job_page_answers_404_for_another_environment_a_pull_job_and_a_bad_id(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    gate = container_host.hold("pull")
    created = job_of(await start_create(admin_client, world))
    await asyncio.wait_for(gate.entered.wait(), WAIT_SECONDS)
    pulled = job_of(
        await admin_client.post(
            f"/environments/{world.remote_id}/images/pull",
            data=csrf_form(admin_client, reference="busybox:1"),
        )
    )

    elsewhere = await admin_client.get(f"/environments/{world.local_id}/containers/jobs/{created}")
    as_pull = await admin_client.get(job_url(world, pulled))
    as_container_on_images = await admin_client.get(
        f"/environments/{world.remote_id}/images/jobs/{created}"
    )
    malformed = await admin_client.get(job_url(world, "short"))

    assert (elsewhere.status_code, as_pull.status_code) == (NOT_FOUND, NOT_FOUND)
    assert (as_container_on_images.status_code, malformed.status_code) == (NOT_FOUND, NOT_FOUND)


async def test_the_user_limit_is_shared_by_pull_and_create_jobs_and_a_refused_create_shows_why(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    gate = container_host.hold("pull")
    first = job_of(await start_create(admin_client, world))
    await asyncio.wait_for(gate.entered.wait(), WAIT_SECONDS)
    second = job_of(
        await admin_client.post(
            f"/environments/{world.remote_id}/images/pull",
            data=csrf_form(admin_client, reference="busybox:1"),
        )
    )

    refused = await start_create(admin_client, world, OTHER_IMAGE)

    assert first != second
    assert refused.status_code == TOO_MANY
    assert "transfers running" in text_of(refused)
    assert world.app.state.engine.image_jobs.running_count() == 2


async def test_a_running_job_page_works_without_script(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    gate = container_host.hold("pull")
    job_id = job_of(await start_create(admin_client, world))
    await asyncio.wait_for(gate.entered.wait(), WAIT_SECONDS)

    page = await admin_client.get(job_url(world, job_id))

    assert '<meta http-equiv="refresh" content="5">' in page.text
    assert f'href="{job_url(world, job_id)}"' in page.text and "Refresh status" in page.text
    assert "Cancel pull" in page.text
    assert page.headers["cache-control"] == "no-store"
