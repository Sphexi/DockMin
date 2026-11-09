"""Tests for ContainersApi.archive_directory: a directory streamed through the archive endpoint."""

import asyncio

import pytest

from app.docker_engine import EngineClient
from app.docker_engine.archive import (
    STALL_SECONDS,
    ArchiveMalformed,
    FileMissing,
    NotADirectory,
)
from app.docker_engine.errors import EngineError, EnvironmentUnreachable, RequestOutcomeUnknown
from tests.archive_support import GO_MODE_DIR, GO_MODE_SYMLINK, HEADER, stat_header
from tests.docker_engine.fake_engine import Ending, FakeEngine, Reply, Stall, Stream, error_reply

CONTAINER = "ab" * 32
ARCHIVE_PATH = f"/containers/{CONTAINER}/archive"
DIRECTORY = "/dockmin-read"
CHUNK_BYTES = 64 * 1024
CHUNK_COUNT = 48
DIRECTORY_HEADERS = stat_header("dockmin-read", 4096, GO_MODE_DIR | 0o755)


def script(
    engine: FakeEngine,
    chunks: list[bytes],
    ending: Ending = Ending.COMPLETE,
    headers: dict[str, str] | None = None,
) -> Stream:
    stream = Stream(chunks, ending, headers=DIRECTORY_HEADERS if headers is None else headers)
    engine.route("GET", ARCHIVE_PATH, stream)
    return stream


def chunks_of(count: int = CHUNK_COUNT) -> list[bytes]:
    return [bytes([number % 251]) * CHUNK_BYTES for number in range(count)]


async def test_a_directory_is_yielded_chunk_by_chunk_and_counted(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    sent = chunks_of()
    script(engine, sent)

    stream = await engine_client.containers.archive_directory(CONTAINER, DIRECTORY)
    received = [chunk async for chunk in stream]

    assert received == sent
    assert stream.received == CHUNK_BYTES * CHUNK_COUNT and stream.name == "dockmin-read"
    assert engine.calls("GET", ARCHIVE_PATH)[0].query == {"path": DIRECTORY}


async def test_the_body_is_pulled_one_chunk_at_a_time_and_never_held_whole(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    scripted = script(engine, chunks_of())

    stream = await engine_client.containers.archive_directory(CONTAINER, DIRECTORY)
    after_open = scripted.delivered
    await stream.__anext__()
    after_first = scripted.delivered
    await stream.__anext__()

    assert (after_open, after_first, scripted.delivered) == (1, 1, 2)
    await stream.aclose()


async def test_a_read_that_is_cancelled_or_closed_releases_the_response(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    held = script(engine, chunks_of(2), Ending.HOLD)
    stream = await engine_client.containers.archive_directory(CONTAINER, DIRECTORY)
    await stream.__anext__()
    await stream.__anext__()

    reading = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    await stream.aclose()

    assert held.closed == 1


async def test_closing_part_way_releases_the_response(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    held = script(engine, chunks_of(), Ending.COMPLETE)
    stream = await engine_client.containers.archive_directory(CONTAINER, DIRECTORY)
    await stream.__anext__()

    await stream.aclose()
    await stream.aclose()

    assert held.closed == 1


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param(stat_header("f", 3), NotADirectory, id="regular-file"),
        pytest.param(stat_header("l", 0, GO_MODE_SYMLINK | 0o777), NotADirectory, id="symlink"),
        pytest.param(
            stat_header("d", 4096, GO_MODE_DIR | 0o755, link_target="/x"),
            NotADirectory,
            id="link-target",
        ),
        pytest.param({}, ArchiveMalformed, id="no-stat-header"),
        pytest.param({HEADER: "not base64"}, ArchiveMalformed, id="malformed-stat-header"),
    ],
)
async def test_a_path_that_is_not_a_directory_is_refused_before_the_body_is_read(
    engine: FakeEngine,
    engine_client: EngineClient,
    headers: dict[str, str],
    expected: type[EngineError],
) -> None:
    refused = script(engine, chunks_of(2), headers=headers)

    with pytest.raises(expected):
        await engine_client.containers.archive_directory(CONTAINER, DIRECTORY)

    assert refused.delivered == 0 and refused.closed == 1


@pytest.mark.parametrize("status", [400, 404])
async def test_a_path_the_engine_does_not_have_is_reported_missing(
    engine: FakeEngine, engine_client: EngineClient, status: int
) -> None:
    engine.route("GET", ARCHIVE_PATH, error_reply(status))

    with pytest.raises(FileMissing):
        await engine_client.containers.archive_directory(CONTAINER, DIRECTORY)


async def test_a_response_with_no_body_is_refused(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", ARCHIVE_PATH, Reply(200, b"", DIRECTORY_HEADERS))

    with pytest.raises(ArchiveMalformed):
        await engine_client.containers.archive_directory(CONTAINER, DIRECTORY)


async def test_the_read_has_a_stall_limit_and_no_other_time_limit(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    script(engine, chunks_of(1))
    stream = await engine_client.containers.archive_directory(CONTAINER, DIRECTORY)
    await stream.aclose()
    engine.route("GET", ARCHIVE_PATH, Stall())

    with pytest.raises(RequestOutcomeUnknown):
        await engine_client.containers.archive_directory(CONTAINER, DIRECTORY)

    timeouts = [call.timeout for call in engine.calls("GET", ARCHIVE_PATH)]
    assert all(t is not None and t["read"] == STALL_SECONDS for t in timeouts)


async def test_a_connection_cut_part_way_fails_the_read_instead_of_ending_it(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    script(engine, chunks_of(2), Ending.CUT)
    stream = await engine_client.containers.archive_directory(CONTAINER, DIRECTORY)

    with pytest.raises(EnvironmentUnreachable):
        async for _chunk in stream:
            pass


@pytest.mark.parametrize(
    "container_id", ["short", "AB" * 32], ids=["too-short", "not-lower-case-hex"]
)
async def test_a_container_that_is_not_a_full_id_is_refused_unsent(
    engine: FakeEngine, engine_client: EngineClient, container_id: str
) -> None:
    with pytest.raises(EngineError):
        await engine_client.containers.archive_directory(container_id, DIRECTORY)

    assert engine.calls("GET") == []
