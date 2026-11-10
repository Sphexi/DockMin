"""Pull jobs over the web: the job page, its event stream, cancellation and who may see a job."""

import asyncio
import re
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient, Response

import app.web.image_stream_routes as stream_routes
from tests.access_support import ADMIN, STANDARD
from tests.auth_support import ADMIN_ID, csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.image_support import binding_rows, rows_for
from tests.stream_support import leftover_tasks, read_stream
from tests.web.environment_support import World
from tests.web.image_support import images_path
from tests.web.registry_support import visible_text

OK, SEE_OTHER, FORBIDDEN, NOT_FOUND, TOO_MANY = 200, 303, 403, 404, 429
REFERENCE = "busybox:1"
JOB = re.compile(r"/jobs/([A-Za-z0-9_-]{22})$")
UNKNOWN_JOB = "A" * 22
LAYER = "a1b2c3d4e5f6"
QUICK = 0.01


def job_url(world: World, job_id: str, suffix: str = "", environment_id: int | None = None) -> str:
    return f"{images_path(world, environment_id)}/jobs/{job_id}{suffix}"


def started(response: Response) -> str:
    assert response.status_code == SEE_OTHER, response.text
    found = JOB.search(response.headers["location"])
    assert found is not None
    return found.group(1)


def text_of(response: Response) -> str:
    return " ".join(visible_text(response.text).split())


async def start_pull(client: AsyncClient, world: World, reference: str = REFERENCE) -> str:
    form = csrf_form(client, reference=reference)
    return started(await client.post(f"{images_path(world)}/pull", data=form))


async def settled(world: World, job_id: str) -> None:
    task = world.app.state.engine.image_jobs._jobs[job_id].task
    await asyncio.wait({task})


@pytest.fixture(autouse=True)
async def stopped_jobs(world: World, container_host: ContainerEngine) -> AsyncIterator[None]:
    """Cancel every job still held by the daemon before the daemon and the application close."""
    yield
    registry = world.app.state.engine.image_jobs
    tasks = [job.task for job in registry._jobs.values() if job.task and not job.task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks)


@pytest.fixture
async def running(admin_client: AsyncClient, world: World, container_host: ContainerEngine) -> str:
    """A pull the daemon is holding open, started by the administrator."""
    gate = container_host.hold("pull")
    job_id = await start_pull(admin_client, world)
    await asyncio.wait_for(gate.entered.wait(), timeout=5)
    return job_id


async def test_a_running_job_names_its_stream_which_sends_state_then_comments_then_retry(
    admin_client: AsyncClient,
    world: World,
    running: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = await admin_client.get(job_url(world, running))
    monkeypatch.setattr(stream_routes, "POLL_SECONDS", QUICK)
    monkeypatch.setattr(stream_routes, "KEEP_ALIVE_SECONDS", 3 * QUICK)
    before = asyncio.all_tasks()
    quiet = await read_stream(
        world.app, admin_client, job_url(world, running, "/events"), stop_when=": keep-alive"
    )
    monkeypatch.setattr(stream_routes, "LIFETIME_SECONDS", 0.0)
    expired = await read_stream(world.app, admin_client, job_url(world, running, "/events"))

    html = page.text
    assert page.status_code == OK
    assert page.headers["cache-control"] == "no-store"
    assert "Running" in text_of(page)
    assert "Waiting for the host to report layers." in text_of(page)
    assert "Refresh status" in html
    assert f'href="{job_url(world, running)}"' in html
    assert "Cancel pull" in html
    assert f'data-job-stream="{job_url(world, running, "/events")}"' in html
    assert f'action="{job_url(world, running, "/cancel")}"' in html
    assert quiet.status == OK
    assert quiet.headers["content-type"].startswith("text/event-stream")
    assert quiet.headers["cache-control"] == "no-store"
    assert quiet.body.startswith("event: progress\ndata: ")
    assert quiet.body.count("event: progress") == 1
    assert "Running" in quiet.body and "<html" not in quiet.body
    assert expired.body.startswith("event: progress")
    assert expired.body.endswith(f"retry: {stream_routes.RETRY_MILLISECONDS}\n\n")
    assert world.app.state.engine.stream_slots.open_count == 0
    assert await leftover_tasks(before) == set()


async def test_a_finished_job_streams_one_last_event_and_its_page_shows_how_it_ended(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.pull_id = "sha256:" + "5" * 64
    container_host.pull_lines = [
        {"status": "Downloading", "id": LAYER, "progressDetail": {"current": 5, "total": 10}},
        {"status": "Pull complete", "id": LAYER},
    ]
    succeeded = await start_pull(admin_client, world)
    await settled(world, succeeded)
    container_host.pull_status = 404
    failed = await start_pull(admin_client, world, "busybox:2")
    await settled(world, failed)

    streamed = await read_stream(world.app, admin_client, job_url(world, succeeded, "/events"))
    success_page = await admin_client.get(job_url(world, succeeded))
    failure_page = await admin_client.get(job_url(world, failed))

    assert streamed.body.startswith("event: done\ndata: ")
    assert streamed.body.count("event: ") == 1
    assert "Succeeded" in streamed.body and "Pulled busybox:1." in streamed.body
    assert LAYER in streamed.body
    assert world.app.state.engine.stream_slots.open_count == 0
    assert "Succeeded" in text_of(success_page)
    assert "A binding was created for this reference." not in text_of(success_page)
    assert f"{images_path(world)}/{'5' * 64}" in success_page.text
    assert "Refresh status" not in success_page.text and "Cancel pull" not in success_page.text
    assert "data-job-stream" not in success_page.text
    assert "Failed" in text_of(failure_page)
    assert "The registry has no such image, or refused access to it." in text_of(failure_page)
    assert "registry detail" not in failure_page.text


async def test_a_job_belongs_to_its_starter_and_administrators_and_its_limits_and_tokens_hold(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    running: str,
) -> None:
    slots = world.app.state.engine.stream_slots
    elsewhere = job_url(world, running, environment_id=world.local_id)
    missing = [
        elsewhere,
        f"{elsewhere}/events",
        job_url(world, UNKNOWN_JOB),
        job_url(world, UNKNOWN_JOB, "/events"),
        job_url(world, "short"),
        job_url(world, running + "x"),
        job_url(world, "A" * 22 + "!"),
    ]

    other_user = [
        (await standard_client.get(job_url(world, running))).status_code,
        (await standard_client.get(job_url(world, running, "/events"))).status_code,
        (
            await standard_client.post(
                job_url(world, running, "/cancel"), data=csrf_form(standard_client)
            )
        ).status_code,
    ]
    absent = {path: (await admin_client.get(path)).status_code for path in missing}
    wrong_environment = await admin_client.post(
        job_url(world, running, "/cancel", world.local_id), data=csrf_form(admin_client)
    )
    unsigned = [
        (await admin_client.post(f"{images_path(world)}/pull", data={"reference": REFERENCE})),
        await admin_client.post(job_url(world, running, "/cancel")),
    ]
    held = [slots.acquire(ADMIN_ID) for _ in range(4)]
    full = await admin_client.get(job_url(world, running, "/events"))
    for slot in held:
        slot.close()
    second = await start_pull(admin_client, world, "busybox:2")
    third = await admin_client.post(
        f"{images_path(world)}/pull", data=csrf_form(admin_client, reference="busybox:3")
    )

    assert other_user == [NOT_FOUND] * 3
    assert set(absent.values()) == {NOT_FOUND}
    assert wrong_environment.status_code == NOT_FOUND
    assert [r.status_code for r in unsigned] == [FORBIDDEN, FORBIDDEN]
    assert full.status_code == TOO_MANY and "live views" in text_of(full)
    assert third.status_code == TOO_MANY and "2 transfers running" in text_of(third)
    assert len(world.app.state.engine.image_jobs) == 2
    assert world.app.state.engine.image_jobs.view(running, ADMIN).state == "running"
    assert world.app.state.engine.image_jobs.view(second, ADMIN).state == "running"


async def test_cancelling_a_job_ends_it_and_an_administrator_may_stop_a_standard_users(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    running: str,
    container_host: ContainerEngine,
) -> None:
    theirs = await start_pull(standard_client, world, "busybox:2")

    cancelled = await admin_client.post(
        job_url(world, running, "/cancel"), data=csrf_form(admin_client)
    )
    visible = await admin_client.get(job_url(world, theirs))
    stopped = await admin_client.post(
        job_url(world, theirs, "/cancel"), data=csrf_form(admin_client)
    )
    await settled(world, running)
    await settled(world, theirs)

    async with world.app.state.database.session() as db:
        bindings = await binding_rows(db)
        rows = await rows_for(db, "image_pulled")
    assert (cancelled.status_code, cancelled.headers["location"]) == (
        SEE_OTHER,
        job_url(world, running),
    )
    assert visible.status_code == OK and stopped.status_code == SEE_OTHER
    assert world.app.state.engine.image_jobs.view(theirs, STANDARD).state == "cancelled"
    assert bindings == []
    assert [row["detail"]["interrupted"] for row in rows] == [True, True]
    refreshed = await admin_client.get(job_url(world, running))
    assert "Cancelled" in text_of(refreshed)
    assert "What it left on the host is not known." in text_of(refreshed)
    assert "Check the image list before trying again." in text_of(refreshed)
