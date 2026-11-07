"""Stack operation jobs over the web: the event stream, who may follow a job, and its limits."""

import asyncio
import re
from typing import Any

import pytest
from httpx import AsyncClient, Response

import app.web.stack_stream_routes as stream_routes
from app.services.object_locks import MAX_STREAMS_PER_USER, StreamSlot
from tests.auth_support import ADMIN_ID, csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.stack_support import (  # noqa: F401
    SECRET_SENTINEL,
    install_scripted_compose,
    job_id_of,
    settle_stack_job,
    stopped_stack_jobs,
    variable_fields,
)
from tests.stream_support import leftover_tasks, read_stream
from tests.web.environment_support import EngineTripwire, World
from tests.web.registry_support import visible_text

pytestmark = pytest.mark.usefixtures("stopped_stack_jobs")

OK, SEE_OTHER, NOT_FOUND, TOO_MANY = 200, 303, 404, 429
QUICK = 0.01
WAIT_SECONDS = 10.0
HANG_TIMEOUT = 3.0
STDERR_SENTINEL = "SECRET_SENTINEL_VALUE"
REFERENCE = re.compile(r"Reference [0-9a-f]{32}")
UNKNOWN_JOB = "A" * 22
COMPOSE_FILE = "services:\n  web:\n    image: nginx:1\n"


def script(up: str) -> str:
    return f"{COMPOSE_FILE}x-fake:\n  up: {up}\n"


def stacks_path(world: World, environment_id: int | None = None) -> str:
    return f"/environments/{environment_id or world.remote_id}/stacks"


def events_path(world: World, job_id: str, environment_id: int | None = None) -> str:
    return f"{stacks_path(world, environment_id)}/-/jobs/{job_id}/events"


def text_of(response: Response) -> str:
    return " ".join(visible_text(response.text).split())


def shape(response: Response) -> tuple[int, str]:
    return response.status_code, REFERENCE.sub("", text_of(response))


async def start(
    client: AsyncClient, world: World, name: str, *, up: str = "ok", **variables: Any
) -> str:
    """Start a create job and return its id without waiting for it."""
    created = await client.post(
        f"{stacks_path(world)}/-/new",
        data=csrf_form(client, name=name, compose_file=script(up), **variables),
    )
    return job_id_of(created)


def close_all(slots: list[StreamSlot]) -> None:
    for slot in slots:
        slot.close()


async def reported(client: AsyncClient, world: World, job_id: str, text: str) -> None:
    """Wait, within a bound, until the job page shows `text`."""
    page = f"{stacks_path(world)}/-/jobs/{job_id}"
    async with asyncio.timeout(WAIT_SECONDS):
        while text not in text_of(await client.get(page)):  # noqa: ASYNC110
            await asyncio.sleep(QUICK)


async def test_a_finished_job_streams_one_last_event_and_only_its_starter_or_an_administrator_may(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    anonymous_client: AsyncClient,
    world: World,
    stack_host: object,
) -> None:
    engine = world.app.state.engine
    secret = variable_fields(("TOKEN", STDERR_SENTINEL, True))
    done = await start(admin_client, world, "done")
    failed = await start(admin_client, world, "failed", up="fail", **secret)
    mine = await start(standard_client, world, "mine")
    await settle_stack_job(world.app, done)
    await settle_stack_job(world.app, failed)
    await settle_stack_job(world.app, mine)
    before = asyncio.all_tasks()

    succeeded_stream = await read_stream(world.app, admin_client, events_path(world, done))
    failed_stream = await read_stream(world.app, admin_client, events_path(world, failed))
    own_stream = await read_stream(world.app, standard_client, events_path(world, mine))
    watched = await read_stream(world.app, admin_client, events_path(world, mine))
    foreign = await standard_client.get(events_path(world, done))
    foreign_page = await standard_client.get(f"{stacks_path(world)}/-/jobs/{done}")
    unknown = await standard_client.get(events_path(world, UNKNOWN_JOB))
    elsewhere = await admin_client.get(events_path(world, done, world.local_id))
    signed_out = await anonymous_client.get(events_path(world, done))

    assert succeeded_stream.status == OK
    assert succeeded_stream.headers["content-type"].startswith("text/event-stream")
    assert succeeded_stream.headers["cache-control"] == "no-store"
    assert succeeded_stream.headers["x-accel-buffering"] == "no"
    assert succeeded_stream.body.startswith('event: done\ndata: <div class="job"')
    assert succeeded_stream.body.count("event: ") == 1
    assert "Succeeded" in succeeded_stream.body and "<html" not in succeeded_stream.body
    assert (
        failed_stream.body.startswith("event: done") and 'data-state="failed"' in failed_stream.body
    )
    assert "[REDACTED]" in failed_stream.body and "&lt;data&gt;/stacks/" in failed_stream.body
    assert (
        STDERR_SENTINEL not in failed_stream.body and "hunter2secretvalue" not in failed_stream.body
    )
    assert own_stream.status == OK and watched.status == OK
    assert "event: done" in own_stream.body and "event: done" in watched.body
    assert shape(foreign) == shape(unknown) and foreign.status_code == NOT_FOUND
    assert foreign_page.status_code == NOT_FOUND
    assert elsewhere.status_code == NOT_FOUND
    assert signed_out.status_code == SEE_OTHER and signed_out.headers["location"].startswith(
        "/login"
    )
    assert engine.stream_slots.open_count == 0
    assert await leftover_tasks(before) == set()


async def test_the_stream_gate_asks_the_host_nothing_and_the_per_user_limit_is_answered_politely(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    stack_host: object,
) -> None:
    engine = world.app.state.engine
    done = await start(admin_client, world, "done")
    await settle_stack_job(world.app, done)
    attempts: list[Any] = []
    engine.registry = EngineTripwire(attempts)

    page = await admin_client.get(f"{stacks_path(world)}/-/jobs/{done}")
    streamed = await read_stream(world.app, admin_client, events_path(world, done))
    hidden_environment = await standard_client.get(events_path(world, done, world.down_id))
    held = [engine.stream_slots.acquire(ADMIN_ID) for _ in range(MAX_STREAMS_PER_USER)]
    full = await admin_client.get(events_path(world, done))
    close_all(held)
    freed = await read_stream(world.app, admin_client, events_path(world, done))

    assert page.status_code == OK and streamed.status == OK and "event: done" in streamed.body
    assert attempts == []
    assert hidden_environment.status_code == NOT_FOUND
    assert full.status_code == TOO_MANY and "live views" in text_of(full)
    assert full.headers["cache-control"] == "no-store"
    assert freed.status == OK and "event: done" in freed.body
    assert engine.stream_slots.open_count == 0


async def test_a_running_job_page_names_its_stream_which_sends_progress_then_the_last_event(
    admin_client: AsyncClient,
    world: World,
    stack_host: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream_routes, "POLL_SECONDS", QUICK)
    job_id = await start(admin_client, world, "slow", up="slow")

    page = await admin_client.get(f"{stacks_path(world)}/-/jobs/{job_id}")
    streamed = await read_stream(
        world.app, admin_client, events_path(world, job_id), stop_when="event: done"
    )
    await settle_stack_job(world.app, job_id)
    ended = await admin_client.get(f"{stacks_path(world)}/-/jobs/{job_id}")

    assert page.status_code == OK and page.headers["cache-control"] == "no-store"
    assert f'data-job-stream="{events_path(world, job_id)}"' in page.text
    assert "Refresh status" in page.text and 'http-equiv="refresh"' in page.text
    assert 'data-state="running"' in page.text
    first, last = streamed.body.split("\n\n")[0], streamed.body.rstrip().split("\n\n")[-1]
    assert first.startswith("event: progress\ndata: ") and 'data-state="running"' in first
    assert last.startswith("event: done\ndata: ") and 'data-state="succeeded"' in last
    assert "data-job-stream" not in ended.text and 'http-equiv="refresh"' not in ended.text


async def test_an_idle_job_stream_sends_keep_alive_comments_and_closes_with_a_retry_hint(
    admin_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream_routes, "POLL_SECONDS", QUICK)
    monkeypatch.setattr(stream_routes, "KEEP_ALIVE_SECONDS", 3 * QUICK)
    await install_scripted_compose(
        world.app.state.engine.compose, stack_host, operation_timeout=HANG_TIMEOUT
    )
    job_id = await start(admin_client, world, "hung", up="hang")
    await reported(admin_client, world, job_id, "Container hung-web-1")
    before = asyncio.all_tasks()

    quiet = await read_stream(
        world.app, admin_client, events_path(world, job_id), stop_when=": keep-alive"
    )
    monkeypatch.setattr(stream_routes, "LIFETIME_SECONDS", 0.0)
    expired = await read_stream(world.app, admin_client, events_path(world, job_id))

    assert quiet.body.startswith("event: progress\ndata: ") and quiet.body.count("event: ") == 1
    assert quiet.body.rstrip().endswith(": keep-alive")
    assert expired.body.startswith("event: progress")
    assert expired.body.endswith(f"retry: {stream_routes.RETRY_MILLISECONDS}\n\n")
    assert world.app.state.engine.stream_slots.open_count == 0
    assert await leftover_tasks(before) == set()
    await settle_stack_job(world.app, job_id)
