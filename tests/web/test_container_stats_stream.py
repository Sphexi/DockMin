"""The stats event stream: its events, its refusals and the clean close when the page is left."""

import asyncio

import pytest
from httpx import AsyncClient

import app.services.container_stats as container_stats
from app.services.container_stats import RETRY_MILLISECONDS, StatsLimits
from tests.auth_support import ADMIN_ID
from tests.container_support import ABSENT, CACHE, DB, JOB, WEB
from tests.docker_engine.container_engine import ContainerEngine, stats_line
from tests.docker_engine.fake_engine import Ending, Stream
from tests.stream_support import leftover_tasks, read_stream
from tests.web.environment_support import World

CONFLICT, NOT_FOUND, TOO_MANY = 409, 404, 429
FRAMES = 3


def stats_path(world: World, container: str) -> str:
    return f"/environments/{world.remote_id}/containers/{container}/stats/stream"


@pytest.fixture
def stream_of_frames(container_host: ContainerEngine) -> Stream:
    stream = Stream([stats_line(n) for n in range(FRAMES)], Ending.HOLD)
    container_host.route("GET", f"/containers/{WEB}/stats", stream)
    return stream


async def test_the_stream_sends_a_rendered_event_and_closes_cleanly_when_the_client_leaves(
    admin_client: AsyncClient, world: World, stream_of_frames: Stream
) -> None:
    before = asyncio.all_tasks()
    slots = world.app.state.engine.stream_slots

    seen = await read_stream(world.app, admin_client, stats_path(world, WEB), stop_after_events=1)

    assert seen.status == 200
    assert seen.headers["content-type"].startswith("text/event-stream")
    assert seen.headers["cache-control"] == "no-store"
    assert seen.body.startswith("event: stats\n")
    assert "Processes" in seen.body and "<html" not in seen.body
    assert seen.body.count("event: stats") == 1
    assert slots.open_count == 0
    assert await leftover_tasks(before) == set()


async def test_the_server_ends_a_stream_at_its_lifetime_and_tells_the_browser_when_to_reconnect(
    admin_client: AsyncClient,
    world: World,
    stream_of_frames: Stream,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(container_stats, "StatsLimits", lambda: StatsLimits(lifetime=0.0))

    seen = await read_stream(
        world.app, admin_client, stats_path(world, WEB), stop_after_events=None
    )

    assert seen.body == f"retry: {RETRY_MILLISECONDS}\n\n"
    assert world.app.state.engine.stream_slots.open_count == 0


async def test_a_stream_is_refused_for_a_stopped_hidden_or_missing_container_or_a_full_user(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
) -> None:
    slots = world.app.state.engine.stream_slots
    stopped = await standard_client.get(stats_path(world, JOB))
    hidden = await standard_client.get(stats_path(world, CACHE))
    missing = await standard_client.get(stats_path(world, ABSENT))
    held = [slots.acquire(ADMIN_ID) for _ in range(4)]
    full = await admin_client.get(stats_path(world, WEB))
    for slot in held:
        slot.close()

    assert stopped.status_code == CONFLICT
    assert (hidden.status_code, missing.status_code) == (NOT_FOUND, NOT_FOUND)
    assert full.status_code == TOO_MANY
    assert slots.open_count == 0
    assert shared_host.calls("GET", f"/containers/{DB}/stats") == []
