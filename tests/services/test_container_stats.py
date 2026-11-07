"""Container stats: the one-shot sample and the bounded, throttled live stream."""

import asyncio
from typing import Any

import pytest

from app.auth.errors import ResourceNotFound
from app.docker_engine.errors import NotFound
from app.docker_engine.resources import STATS_FRAME_BYTES, ContainersApi
from app.services.container_stats import (
    KEEP_ALIVE,
    ContainerNotRunning,
    ContainerStatsService,
    StatsLimits,
    StatsSample,
    StatsStream,
)
from app.services.object_locks import MAX_STREAMS_PER_USER, TooManyStreams
from tests.access_support import ADMIN, ENV_A, STANDARD, control
from tests.access_support import container_key as key_of
from tests.container_support import CACHE, JOB, WEB, stock
from tests.docker_engine.container_engine import ContainerEngine, stats_line
from tests.docker_engine.fake_engine import Ending, Stream
from tests.services.conftest import ContainerRig

NEVER = 1000.0
FRAMES = 5
BASE_MEMORY = 52_428_800
INACTIVE_FILE = 1024
BLOCK_READ, BLOCK_WRITTEN, PROCESSES = 4096, 8192, 4
STATS_PATH = f"/containers/{WEB}/stats"


@pytest.fixture
def stocked(rig: ContainerRig, host: ContainerEngine) -> ContainerRig:
    stock(host)
    return rig


def service(rig: ContainerRig, **limits: float) -> ContainerStatsService:
    return ContainerStatsService(
        authoriser=rig.access.authoriser,
        inspector=rig.inspector,
        slots=rig.slots,
        limits=StatsLimits(**limits),
    )


def script(host: ContainerEngine, frames: int, ending: Ending = Ending.COMPLETE) -> Stream:
    stream = Stream([stats_line(n) for n in range(frames)], ending)
    host.route("GET", STATS_PATH, stream)
    return stream


async def collect(stream: StatsStream) -> list[Any]:
    try:
        return [item async for item in stream]
    finally:
        await stream.aclose()


async def test_a_sample_reduces_one_frame_to_the_figures_the_panel_shows(
    stocked: ContainerRig,
) -> None:
    sample = await stocked.services.stats.sample(ADMIN, ENV_A, WEB)

    assert sample.memory_used == BASE_MEMORY - INACTIVE_FILE
    assert (sample.block_read, sample.block_written, sample.processes) == (
        BLOCK_READ,
        BLOCK_WRITTEN,
        PROCESSES,
    )
    assert sample.cpu_percent is not None and sample.memory_percent is not None


async def test_a_sample_of_a_container_removed_after_it_was_authorised_is_not_found(
    stocked: ContainerRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def removed(self: ContainersApi, container_id: str) -> None:
        raise NotFound(ENV_A, "GET stats returned HTTP 404", status_code=404)

    monkeypatch.setattr(ContainersApi, "stats_once", removed)

    with pytest.raises(ResourceNotFound):
        await stocked.services.stats.sample(ADMIN, ENV_A, WEB)


async def test_a_sample_of_a_container_that_is_not_running_says_so(
    stocked: ContainerRig,
) -> None:
    with pytest.raises(ContainerNotRunning):
        await stocked.services.stats.sample(ADMIN, ENV_A, JOB)


async def test_opening_a_stream_for_a_container_the_user_cannot_see_calls_the_host_for_no_stats(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    with pytest.raises(ResourceNotFound):
        await stocked.services.stats.open(STANDARD, ENV_A, CACHE)

    assert host.calls("GET", f"/containers/{CACHE}/stats") == []
    assert stocked.slots.open_count == 0


async def test_opening_a_stream_for_a_stopped_container_claims_no_slot(
    stocked: ContainerRig,
) -> None:
    with pytest.raises(ContainerNotRunning):
        await stocked.services.stats.open(ADMIN, ENV_A, JOB)

    assert stocked.slots.open_count == 0


async def test_an_open_stream_holds_a_slot_until_it_is_closed_and_closing_twice_is_harmless(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    script(host, 0, Ending.HOLD)
    stream = await stocked.services.stats.open(ADMIN, ENV_A, WEB)
    held = stocked.slots.open_count

    await stream.aclose()
    await stream.aclose()

    assert (held, stocked.slots.open_count) == (1, 0)
    assert stream.end_reason == "closed"


async def test_a_user_over_the_per_user_cap_is_refused_a_fifth_stream(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    script(host, 0, Ending.HOLD)
    open_streams = [
        await stocked.services.stats.open(ADMIN, ENV_A, WEB) for _ in range(MAX_STREAMS_PER_USER)
    ]

    with pytest.raises(TooManyStreams) as refused:
        await stocked.services.stats.open(ADMIN, ENV_A, WEB)
    for stream in open_streams:
        await stream.aclose()

    assert refused.value.scope == "user"
    assert stocked.slots.open_count == 0


async def test_the_stream_decodes_only_the_frame_it_sends_when_the_rest_arrive_within_the_interval(
    stocked: ContainerRig, host: ContainerEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(host, FRAMES)
    decoded: list[bytes] = []
    parse = ContainersApi.parse_stats

    def counting(self: ContainersApi, line: bytes) -> Any:
        decoded.append(line)
        return parse(self, line)

    monkeypatch.setattr(ContainersApi, "parse_stats", counting)
    stream = await service(stocked, interval=NEVER).open(ADMIN, ENV_A, WEB)

    items = await collect(stream)

    assert len(items) == 1 and isinstance(items[0], StatsSample)
    assert len(decoded) == 1
    assert stream.end_reason == "finished"


async def test_with_no_throttle_every_frame_is_sent_in_order(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    script(host, 3)
    stream = await service(stocked, interval=0.0).open(ADMIN, ENV_A, WEB)

    items = await collect(stream)

    assert [item.network_received for item in items] == [0, 1000, 2000]


async def test_a_frame_over_the_size_cap_ends_the_stream_as_unavailable_and_frees_the_slot(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.route(
        "GET",
        STATS_PATH,
        Stream([b"x" * (STATS_FRAME_BYTES + 1) + b"\n", stats_line()], Ending.HOLD),
    )
    stream = await stocked.services.stats.open(ADMIN, ENV_A, WEB)

    items = await collect(stream)

    assert items == []
    assert stream.end_reason == "unavailable"
    assert stocked.slots.open_count == 0


async def test_a_connection_the_host_drops_ends_the_stream_as_unavailable(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    script(host, 0, Ending.CUT)
    stream = await stocked.services.stats.open(ADMIN, ENV_A, WEB)

    await collect(stream)

    assert stream.end_reason == "unavailable"


async def test_a_stream_past_its_lifetime_ends_and_invites_a_reconnect(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    script(host, FRAMES, Ending.HOLD)
    stream = await service(stocked, lifetime=0.0).open(ADMIN, ENV_A, WEB)

    items = await collect(stream)

    assert items == [] and stream.end_reason == "lifetime"


async def test_a_quiet_stream_yields_a_keep_alive_marker(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    script(host, 0, Ending.HOLD)
    stream = await service(stocked, keepalive=0.01).open(ADMIN, ENV_A, WEB)

    first = await anext(stream)
    await stream.aclose()

    assert first is KEEP_ALIVE


async def test_a_reader_cancelled_while_waiting_leaves_no_task_and_no_slot_once_the_stream_closes(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    script(host, 0, Ending.HOLD)
    before = asyncio.all_tasks()
    stream = await service(stocked, keepalive=NEVER).open(ADMIN, ENV_A, WEB)
    reader = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)

    reader.cancel()
    await asyncio.gather(reader, return_exceptions=True)
    await stream.aclose()

    assert asyncio.all_tasks() - before == set()
    assert stocked.slots.open_count == 0


async def test_a_standard_user_with_read_may_stream_and_a_stream_is_authorised_once_at_open(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    await control(stocked.access, key_of(0xA1, ENV_A), public=True, level="read")
    script(host, 1)
    stream = await stocked.services.stats.open(STANDARD, ENV_A, WEB)

    items = await collect(stream)

    assert len(items) == 1
    assert len(host.calls("GET", f"/containers/{WEB}/json")) == 1
