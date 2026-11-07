"""Tests for log, stats and pull streams through the client."""

import pytest
from pydantic import SecretStr

from app.docker_engine import EngineClient, EngineError, EnvironmentUnreachable
from app.docker_engine.frames import LogFrame
from tests.docker_engine.fake_engine import Ending, FakeEngine, Stream
from tests.docker_engine.helpers import (
    CONTAINER_ID,
    ENV_ID,
    STDERR,
    STDOUT,
    drain,
    drain_until_error,
    json_line,
    log_frame,
    stats_payload,
)

LOGS_PATH = f"/containers/{CONTAINER_ID}/logs"
STATS_PATH = f"/containers/{CONTAINER_ID}/stats"
ENDINGS = [
    pytest.param(Ending.COMPLETE, id="clean-end-of-stream"),
    pytest.param(Ending.CUT, id="connection-cut"),
]
FIRST = log_frame(STDOUT, b"first")
SECOND = log_frame(STDERR, b"second")


async def test_logs_deliver_each_frame_as_its_chunk_arrives_not_after_the_stream_ends(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    body = Stream([FIRST, SECOND])
    engine.route("GET", LOGS_PATH, body)
    logs = engine_client.containers.logs(CONTAINER_ID, tty=False)

    first = await anext(logs)

    assert first == LogFrame("stdout", b"first")
    assert body.delivered == 1
    await logs.aclose()


async def test_logs_reassemble_a_frame_split_across_chunks(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", LOGS_PATH, Stream([FIRST[:3], FIRST[3:11], FIRST[11:], SECOND]))

    frames = await drain(engine_client.containers.logs(CONTAINER_ID, tty=False))

    assert frames == [LogFrame("stdout", b"first"), LogFrame("stderr", b"second")]


@pytest.mark.parametrize("ending", ENDINGS)
async def test_logs_cut_mid_frame_raise_a_mapped_error_and_never_yield_the_fragment(
    engine: FakeEngine, engine_client: EngineClient, ending: Ending
) -> None:
    engine.route("GET", LOGS_PATH, Stream([FIRST, SECOND[:-2]], ending))

    frames, error = await drain_until_error(engine_client.containers.logs(CONTAINER_ID, tty=False))

    assert frames == [LogFrame("stdout", b"first")]
    assert isinstance(error, EnvironmentUnreachable)
    assert error.environment_id == ENV_ID


async def test_logs_requested_without_a_tty_but_served_raw_are_an_engine_error(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", LOGS_PATH, Stream([b"plain text with no frame headers\n"]))

    _, error = await drain_until_error(engine_client.containers.logs(CONTAINER_ID, tty=False))

    assert type(error) is EngineError
    assert error.environment_id == ENV_ID


async def test_logs_of_a_tty_container_pass_raw_chunks_through_as_stdout(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", LOGS_PATH, Stream([b"raw ", b"output"]))

    frames = await drain(engine_client.containers.logs(CONTAINER_ID, tty=True))

    assert frames == [LogFrame("stdout", b"raw "), LogFrame("stdout", b"output")]


async def test_logs_send_the_requested_filters_as_query_parameters(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", LOGS_PATH, Stream([]))

    await drain(
        engine_client.containers.logs(
            CONTAINER_ID, tty=False, follow=False, stderr=False, since=100, tail=50, timestamps=True
        )
    )

    assert engine.calls(path=LOGS_PATH)[0].query == {
        "follow": "false",
        "stdout": "true",
        "stderr": "false",
        "timestamps": "true",
        "since": "100",
        "tail": "50",
    }


async def test_stats_deliver_each_object_as_its_chunk_arrives(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    body = Stream([json_line(stats_payload(10)), json_line(stats_payload(20))])
    engine.route("GET", STATS_PATH, body)
    stats = engine_client.containers.stats(CONTAINER_ID)

    first = await anext(stats)

    assert first.memory_stats.usage == 10
    assert body.delivered == 1
    await stats.aclose()


@pytest.mark.parametrize("ending", ENDINGS)
async def test_stats_cut_mid_object_raise_a_mapped_error_and_never_yield_the_fragment(
    engine: FakeEngine, engine_client: EngineClient, ending: Ending
) -> None:
    whole = json_line(stats_payload(10))
    engine.route("GET", STATS_PATH, Stream([whole, json_line(stats_payload(20))[:-8]], ending))

    stats, error = await drain_until_error(engine_client.containers.stats(CONTAINER_ID))

    assert [s.memory_stats.usage for s in stats] == [10]
    assert isinstance(error, EnvironmentUnreachable)
    assert error.environment_id == ENV_ID


async def test_pull_yields_progress_and_sends_the_registry_credential_as_a_header(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    lines = [json_line({"status": "Pulling", "id": "a1"}), json_line({"status": "Done"})]
    engine.route("POST", "/images/create", Stream(lines))

    progress = await drain(
        engine_client.images.pull("alpine", tag="3.20", registry_auth=SecretStr("c2VjcmV0"))
    )

    sent = engine.calls("POST", "/images/create")[0]
    assert [p.status for p in progress] == ["Pulling", "Done"]
    assert sent.query == {"fromImage": "alpine", "tag": "3.20"}
    assert sent.headers["x-registry-auth"] == "c2VjcmV0"
