"""The container calls for lifecycle control, bounded reads and the stats stream."""

import json
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

import app.docker_engine.resources as resources
from app.docker_engine import EngineClient, EngineError
from app.docker_engine.engine_http import EngineHttp
from app.docker_engine.errors import Conflict, NotFound, ResponseTooLarge
from app.docker_engine.labels import LABEL_BUDGET_CHARS
from tests.access_support import container_id
from tests.docker_engine.container_engine import Box, ContainerEngine, stats_line
from tests.docker_engine.fake_engine import (
    Ending,
    FakeEngine,
    Reply,
    Stream,
    error_reply,
    json_reply,
)
from tests.docker_engine.helpers import drain
from tests.docker_engine.test_resources import CONTAINER_INSPECT

TARGET = container_id(1)
STACK_LABEL = "com.docker.compose.project"
SMALL_CAP = 100
OVER_THE_SMALL_CAP = {"Content-Length": "1" + "0" * 12}
DAEMON_WAIT_SECONDS = 600
STATS_PROCESSES = 4
HUGE_LABEL = "x" * (LABEL_BUDGET_CHARS + 1)
SUMMARY = Box(TARGET, "web").summary()


@pytest.mark.parametrize(
    ("verb", "status", "changed"),
    [("start", 204, True), ("start", 304, False), ("stop", 204, True), ("stop", 304, False)],
)
async def test_start_and_stop_report_whether_the_engine_changed_anything(
    engine: FakeEngine, engine_client: EngineClient, verb: str, status: int, changed: bool
) -> None:
    engine.route("POST", f"/containers/{TARGET}/{verb}", Reply(status))

    result = await getattr(engine_client.containers, verb)(TARGET)

    assert result is changed


@pytest.mark.parametrize("verb", ["pause", "unpause"])
async def test_pause_and_unpause_post_to_their_own_paths(
    engine: FakeEngine, engine_client: EngineClient, verb: str
) -> None:
    engine.route("POST", f"/containers/{TARGET}/{verb}", Reply(204))

    await getattr(engine_client.containers, verb)(TARGET)

    assert engine.paths()[-1] == f"POST /containers/{TARGET}/{verb}"


async def test_a_stop_widens_the_read_limit_past_the_grace_period_the_daemon_will_wait(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", f"/containers/{TARGET}/stop", Reply(204))

    await engine_client.containers.stop(TARGET, daemon_wait_seconds=DAEMON_WAIT_SECONDS)

    sent = engine.calls("POST", f"/containers/{TARGET}/stop")[0]
    assert sent.timeout is not None
    assert sent.timeout["read"] is not None and sent.timeout["read"] > DAEMON_WAIT_SECONDS


async def test_a_container_list_over_its_cap_raises_the_specific_error_and_returns_nothing(
    engine: FakeEngine, engine_client: EngineClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources, "CONTAINER_LIST_BYTES", SMALL_CAP)
    engine.route("GET", "/containers/json", Reply(200, b"[]", OVER_THE_SMALL_CAP))

    with pytest.raises(ResponseTooLarge) as raised:
        await engine_client.containers.list(all=True)

    assert "too large" in raised.value.reason


async def test_a_container_list_is_decoded_in_a_worker_thread(
    engine: FakeEngine, engine_client: EngineClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine.route("GET", "/containers/json", json_reply([SUMMARY]))
    threads: list[int] = []
    decode = EngineHttp._decode_models

    def recording(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        return decode(*args, **kwargs)

    monkeypatch.setattr(EngineHttp, "_decode_models", recording)

    await engine_client.containers.list(all=True)

    assert threads != [] and threading.get_ident() not in threads


async def listed_labels(client: EngineClient) -> tuple[dict[str, str], bool]:
    row = (await client.containers.list(all=True))[0]
    return row.labels, row.labels_truncated


async def inspected_labels(client: EngineClient) -> tuple[dict[str, str], bool]:
    found = await client.containers.inspect(TARGET)
    return found.config.labels, found.labels_truncated


@pytest.mark.parametrize(
    ("route", "document", "read"),
    [
        pytest.param(
            "/containers/json",
            lambda labels: [{**SUMMARY, "Labels": labels}],
            listed_labels,
            id="list",
        ),
        pytest.param(
            f"/containers/{TARGET}/json",
            lambda labels: {**CONTAINER_INSPECT, "Id": TARGET, "Config": {"Labels": labels}},
            inspected_labels,
            id="inspect",
        ),
    ],
)
async def test_oversized_labels_are_dropped_but_the_stack_label_is_kept_and_the_row_is_marked(
    engine: FakeEngine,
    engine_client: EngineClient,
    route: str,
    document: Callable[[dict[str, str]], Any],
    read: Callable[[EngineClient], Awaitable[tuple[dict[str, str], bool]]],
) -> None:
    labels = {"app": "web", "bulk": HUGE_LABEL, STACK_LABEL: "shop"}
    engine.route("GET", route, json_reply(document(labels)))

    kept, truncated = await read(engine_client)

    assert kept == {"app": "web", STACK_LABEL: "shop"}
    assert truncated is True


async def test_labels_within_the_budget_are_left_alone(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", "/containers/json", json_reply([{**SUMMARY, "Labels": {"app": "web"}}]))

    kept, truncated = await listed_labels(engine_client)

    assert (kept, truncated) == ({"app": "web"}, False)


async def test_listing_ids_returns_the_set_of_ids_and_nothing_else(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", "/containers/json", json_reply([SUMMARY, {**SUMMARY, "Id": "other"}]))

    ids = await engine_client.containers.list_ids(all=True, filters={"health": ["unhealthy"]})

    assert ids == frozenset({TARGET, "other"})


@pytest.mark.parametrize(
    ("constant", "path", "call"),
    [
        pytest.param(
            "CONTAINER_IDS_BYTES",
            "/containers/json",
            lambda client: client.containers.list_ids(all=True),
            id="ids",
        ),
        pytest.param(
            "CONTAINER_INSPECT_BYTES",
            f"/containers/{TARGET}/json",
            lambda client: client.containers.inspect(TARGET),
            id="inspect",
        ),
        pytest.param(
            "STATS_SAMPLE_BYTES",
            f"/containers/{TARGET}/stats",
            lambda client: client.containers.stats_once(TARGET),
            id="one-shot-stats",
        ),
    ],
)
async def test_each_bounded_read_raises_response_too_large_past_its_own_cap(
    engine: FakeEngine,
    engine_client: EngineClient,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    path: str,
    call: Callable[[EngineClient], Awaitable[Any]],
) -> None:
    monkeypatch.setattr(resources, constant, SMALL_CAP)
    engine.route("GET", path, Reply(200, b"{}", OVER_THE_SMALL_CAP))

    with pytest.raises(ResponseTooLarge):
        await call(engine_client)


async def test_stats_lines_yields_each_raw_line_without_validating_it(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    first, second = stats_line(1), b"not json\n"
    engine.route("GET", f"/containers/{TARGET}/stats", Stream([first, second]))

    lines = await drain(engine_client.containers.stats_lines(TARGET))

    assert lines == [first.strip(), second.strip()]


async def test_a_stats_line_over_the_frame_cap_ends_the_stream_with_an_engine_error(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    oversized = b"x" * (resources.STATS_FRAME_BYTES + 1) + b"\n"
    engine.route("GET", f"/containers/{TARGET}/stats", Stream([oversized], Ending.HOLD))

    with pytest.raises(EngineError):
        await drain(engine_client.containers.stats_lines(TARGET))


async def test_a_frame_that_is_not_json_is_an_engine_error(engine_client: EngineClient) -> None:
    with pytest.raises(EngineError):
        engine_client.containers.parse_stats(b"{not json")


async def test_a_one_shot_sample_asks_for_one_frame_without_streaming(
    make_client: Callable[[FakeEngine], EngineClient],
) -> None:
    served = ContainerEngine()
    served.add(TARGET, "web")

    sample = await make_client(served).containers.stats_once(TARGET)

    assert sample.pids_stats.current == STATS_PROCESSES
    assert served.calls("GET", f"/containers/{TARGET}/stats")[0].query == {
        "stream": "false",
        "one-shot": "true",
    }


CREATED_ID = container_id(0xC0)
CREATE_BODY = {"Image": "nginx:1", "HostConfig": {"NetworkMode": "bridge"}}
CREATE_REPLY_CAP = 64


def created(identifier: str = CREATED_ID) -> Reply:
    return json_reply({"Id": identifier, "Warnings": ["a warning"]}, 201)


async def test_create_posts_the_body_as_json_with_the_name_in_the_query_and_returns_the_id(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", "/containers/create", created())

    identifier = await engine_client.containers.create(CREATE_BODY, name="web")

    sent = engine.calls("POST", "/containers/create")[0]
    assert identifier == CREATED_ID
    assert json.loads(sent.body) == CREATE_BODY
    assert sent.headers["content-type"] == "application/json"
    assert sent.query == {"name": "web"}


async def test_create_without_a_name_sends_no_name_parameter(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", "/containers/create", created())

    await engine_client.containers.create(CREATE_BODY)

    assert engine.calls("POST", "/containers/create")[0].query == {}


async def test_create_encodes_the_body_in_a_worker_thread(
    engine: FakeEngine, engine_client: EngineClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine.route("POST", "/containers/create", created())
    threads: list[int] = []
    encode = resources._encode_json

    def recording(body: Any) -> bytes:
        threads.append(threading.get_ident())
        return encode(body)

    monkeypatch.setattr(resources, "_encode_json", recording)

    await engine_client.containers.create(CREATE_BODY)

    assert threads != [] and threading.get_ident() not in threads


@pytest.mark.parametrize(
    "identifier", ["", "abc", "A" * 64, "g" * 64, container_id(1) + "0", "../" + "a" * 61]
)
async def test_create_refuses_a_reply_whose_id_is_not_64_lower_case_hex(
    engine: FakeEngine, engine_client: EngineClient, identifier: str
) -> None:
    engine.route("POST", "/containers/create", created(identifier))

    with pytest.raises(EngineError):
        await engine_client.containers.create(CREATE_BODY)


async def test_create_refuses_a_reply_that_is_not_json(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", "/containers/create", Reply(201, b"not json"))

    with pytest.raises(EngineError):
        await engine_client.containers.create(CREATE_BODY)


async def test_create_reads_at_most_64_kib_of_reply(
    engine: FakeEngine, engine_client: EngineClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources, "CONTAINER_CREATE_REPLY_BYTES", CREATE_REPLY_CAP)
    engine.route("POST", "/containers/create", Reply(201, b"{}", OVER_THE_SMALL_CAP))

    with pytest.raises(ResponseTooLarge):
        await engine_client.containers.create(CREATE_BODY)


@pytest.mark.parametrize(
    ("status", "error"),
    [(404, NotFound), (409, Conflict), (400, EngineError), (500, EngineError)],
)
async def test_create_maps_the_engines_refusals_to_typed_errors(
    engine: FakeEngine, engine_client: EngineClient, status: int, error: type[EngineError]
) -> None:
    engine.route("POST", "/containers/create", error_reply(status))

    with pytest.raises(error):
        await engine_client.containers.create(CREATE_BODY, name="web")


@pytest.mark.parametrize("name", ["a b", "a/b", "..", "a?x=1", "", "-x"])
async def test_create_and_rename_reject_an_unsafe_name_before_any_request_is_sent(
    engine: FakeEngine, engine_client: EngineClient, name: str
) -> None:
    with pytest.raises(EngineError):
        await engine_client.containers.create(CREATE_BODY, name=name)
    with pytest.raises(EngineError):
        await engine_client.containers.rename(TARGET, name)

    assert engine.requests == []


async def test_rename_posts_the_new_name_to_the_containers_own_path(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", f"/containers/{TARGET}/rename", Reply(204))

    await engine_client.containers.rename(TARGET, "web-recreating-abc")

    sent = engine.calls("POST", f"/containers/{TARGET}/rename")
    assert [s.query for s in sent] == [{"name": "web-recreating-abc"}]


@pytest.mark.parametrize(
    ("limit", "route", "read"),
    [
        pytest.param(
            "IMAGE_INSPECT_BYTES",
            "/images/nginx:1/json",
            lambda client: client.images.inspect("nginx:1"),
            id="image",
        ),
        pytest.param(
            "NETWORK_INSPECT_BYTES",
            "/networks/front",
            lambda client: client.networks.inspect("front"),
            id="network",
        ),
        pytest.param(
            "VOLUME_INSPECT_BYTES",
            "/volumes/data",
            lambda client: client.volumes.inspect("data"),
            id="volume",
        ),
    ],
)
async def test_an_inspect_of_an_image_network_or_volume_is_capped(
    engine: FakeEngine,
    engine_client: EngineClient,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
    route: str,
    read: Callable[[EngineClient], Awaitable[Any]],
) -> None:
    monkeypatch.setattr(resources, limit, SMALL_CAP)
    engine.route("GET", route, Reply(200, b"{}", OVER_THE_SMALL_CAP))

    with pytest.raises(ResponseTooLarge):
        await read(engine_client)


@pytest.mark.parametrize(
    ("route", "document", "read"),
    [
        pytest.param(
            "/images/nginx:1/json",
            {"Id": "sha256:aaa"},
            lambda client: client.images.inspect("nginx:1"),
            id="image",
        ),
        pytest.param(
            "/networks/front",
            {"Id": "n1", "Name": "front"},
            lambda client: client.networks.inspect("front"),
            id="network",
        ),
    ],
)
async def test_an_image_or_network_inspect_is_decoded_in_a_worker_thread(
    engine: FakeEngine,
    engine_client: EngineClient,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    document: dict[str, Any],
    read: Callable[[EngineClient], Awaitable[Any]],
) -> None:
    engine.route("GET", route, json_reply(document))
    threads: list[int] = []
    decode = EngineHttp._decode_model

    def recording(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        return decode(*args, **kwargs)

    monkeypatch.setattr(EngineHttp, "_decode_model", recording)

    await read(engine_client)

    assert threads != [] and threading.get_ident() not in threads
