"""Tests for error mapping at the client boundary in app/docker_engine/engine_http.py."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from pydantic import SecretStr

from app.docker_engine import (
    Conflict,
    EngineClient,
    EngineError,
    EnvironmentUnreachable,
    NotFound,
    PermissionDenied,
    ResponseTooLarge,
    Timeouts,
)
from app.docker_engine.engine_http import EngineHttp
from tests.docker_engine.fake_engine import (
    BODY_TOKEN,
    Behaviour,
    Disconnect,
    Ending,
    FakeEngine,
    Refuse,
    Reply,
    Stall,
    Stream,
    error_reply,
    json_reply,
)
from tests.docker_engine.helpers import (
    CONTAINER_ID,
    ENV_ID,
    drain,
    failure_texts,
    json_line,
)

SHORT_READ = Timeouts(connect=1.0, read=0.5, write=1.0, pool=1.0)
REGISTRY_SECRET = "registry-secret-5d2b"
INSPECT_PATH = f"/containers/{CONTAINER_ID}/json"
STATS_PATH = f"/containers/{CONTAINER_ID}/stats"
ENGINE_MESSAGE_CAP = 200
RAW_ENGINE_MESSAGE = "No such \x1b[31mcontainer\x00\n: abc\u2028"
CLEAN_ENGINE_MESSAGE = "No such [31mcontainer: abc"
ONE_MEBIBYTE_CHUNK = b"x" * (1024 * 1024)
CHUNKS_BEYOND_THE_RESPONSE_CAP = 65

type Call = Callable[[EngineClient], Awaitable[Any]]


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, PermissionDenied),
        (403, PermissionDenied),
        (404, NotFound),
        (409, Conflict),
        (502, EnvironmentUnreachable),
        (504, EnvironmentUnreachable),
        (400, EngineError),
        (500, EngineError),
        (503, EngineError),
    ],
)
async def test_http_status_maps_to_the_documented_error_and_carries_the_environment(
    engine: FakeEngine, engine_client: EngineClient, status: int, error_type: type[EngineError]
) -> None:
    engine.route("GET", "/containers/json", error_reply(status))

    with pytest.raises(error_type) as raised:
        await engine_client.containers.list()

    assert type(raised.value) is error_type
    assert raised.value.environment_id == ENV_ID
    assert raised.value.status_code == status
    assert str(raised.value).startswith(f"environment {ENV_ID}:")


@pytest.mark.parametrize(
    ("behaviour", "fragment"),
    [
        (Refuse(), "lost the connection"),
        (Disconnect(), "lost the connection"),
        (Stall(), "timed out"),
    ],
    ids=["refused", "dropped-mid-response", "stalled"],
)
async def test_transport_failure_is_environment_unreachable_with_the_environment_id(
    engine: FakeEngine, engine_client: EngineClient, behaviour: Behaviour, fragment: str
) -> None:
    engine.route("GET", "/containers/json", behaviour)

    with pytest.raises(EnvironmentUnreachable) as raised:
        await engine_client.containers.list()

    assert raised.value.environment_id == ENV_ID
    assert fragment in raised.value.reason


async def test_a_slow_response_trips_the_read_timeout_passed_as_an_argument(
    engine: FakeEngine, engine_http: EngineHttp
) -> None:
    engine.route("GET", INSPECT_PATH, Stall())

    with pytest.raises(EnvironmentUnreachable):
        await engine_http.get_json(INSPECT_PATH, timeouts=SHORT_READ)

    assert engine.calls(path=INSPECT_PATH)[0].timeout == {
        "connect": 1.0,
        "read": 0.5,
        "write": 1.0,
        "pool": 1.0,
    }


@pytest.mark.parametrize("call", ["start", "stop"])
async def test_not_modified_means_already_in_that_state_and_is_not_an_error(
    engine: FakeEngine, engine_client: EngineClient, call: str
) -> None:
    engine.route("POST", f"/containers/{CONTAINER_ID}/{call}", Reply(304))

    await getattr(engine_client.containers, call)(CONTAINER_ID)

    assert engine.calls("POST", f"/containers/{CONTAINER_ID}/{call}")


async def test_a_closed_client_refuses_further_calls_as_unreachable(
    engine_client: EngineClient,
) -> None:
    await engine_client.aclose()

    with pytest.raises(EnvironmentUnreachable) as raised:
        await engine_client.containers.list()

    assert raised.value.environment_id == ENV_ID
    assert "closed" in raised.value.reason


def _list_containers(client: EngineClient) -> Awaitable[Any]:
    return client.containers.list()


def _inspect_container(client: EngineClient) -> Awaitable[Any]:
    return client.containers.inspect(CONTAINER_ID)


def _remove_container(client: EngineClient) -> Awaitable[Any]:
    return client.containers.remove(CONTAINER_ID)


def _stream_stats(client: EngineClient) -> Awaitable[Any]:
    return drain(client.containers.stats(CONTAINER_ID))


def _pull_image(client: EngineClient) -> Awaitable[Any]:
    return drain(client.images.pull("alpine"))


BODY_LEAK_CASES = [
    pytest.param("GET", "/containers/json", error_reply(500), _list_containers, id="status-500"),
    pytest.param("GET", INSPECT_PATH, error_reply(404), _inspect_container, id="status-404"),
    pytest.param(
        "DELETE",
        f"/containers/{CONTAINER_ID}",
        error_reply(409),
        _remove_container,
        id="status-409",
    ),
    pytest.param(
        "GET",
        "/containers/json",
        Reply(200, f"<html>{BODY_TOKEN}</html>".encode()),
        _list_containers,
        id="success-body-not-json",
    ),
    pytest.param(
        "GET",
        "/containers/json",
        Reply(200, f'{{"message": "{BODY_TOKEN}"}}'.encode()),
        _list_containers,
        id="success-body-not-a-list",
    ),
    pytest.param(
        "GET",
        "/containers/json",
        Reply(200, f'[{{"Id": "{BODY_TOKEN}"}}]'.encode()),
        _list_containers,
        id="success-body-wrong-shape",
    ),
    pytest.param(
        "GET",
        "/version",
        Reply(200, BODY_TOKEN.encode()),
        _list_containers,
        id="negotiation-body-not-json",
    ),
    pytest.param(
        "GET",
        f"/containers/{CONTAINER_ID}/stats",
        Stream([BODY_TOKEN.encode()], status=500),
        _stream_stats,
        id="stream-error-status",
    ),
    pytest.param(
        "GET",
        f"/containers/{CONTAINER_ID}/stats",
        Stream([f"not json {BODY_TOKEN}\n".encode()]),
        _stream_stats,
        id="stream-corrupt-line",
    ),
    pytest.param(
        "POST",
        "/images/create",
        Stream([json_line({"error": BODY_TOKEN, "errorDetail": {"message": BODY_TOKEN}})]),
        _pull_image,
        id="pull-reports-failure",
    ),
]


@pytest.mark.parametrize(("method", "path", "behaviour", "call"), BODY_LEAK_CASES)
async def test_no_response_body_reaches_an_error_message(
    engine: FakeEngine,
    engine_client: EngineClient,
    method: str,
    path: str,
    behaviour: Behaviour,
    call: Call,
) -> None:
    engine.route(method, path, behaviour)

    with pytest.raises(EngineError) as raised:
        await call(engine_client)

    assert raised.value.environment_id == ENV_ID
    assert BODY_TOKEN not in "\n".join(failure_texts(raised.value))


async def test_an_error_status_on_a_stream_is_raised_without_reading_its_body(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    body = Stream([BODY_TOKEN.encode()], status=404)
    engine.route("GET", f"/containers/{CONTAINER_ID}/stats", body)

    with pytest.raises(NotFound):
        await drain(engine_client.containers.stats(CONTAINER_ID))

    assert body.delivered == 0


async def test_a_registry_credential_sent_with_a_pull_appears_in_no_error(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", "/images/create", Stream([json_line({"error": "denied"})]))

    with pytest.raises(EngineError) as raised:
        await drain(engine_client.images.pull("alpine", registry_auth=SecretStr(REGISTRY_SECRET)))

    assert engine.calls("POST", "/images/create")[0].headers["x-registry-auth"] == REGISTRY_SECRET
    assert REGISTRY_SECRET not in "\n".join(failure_texts(raised.value))


async def test_a_stream_ended_by_the_peer_mid_response_is_unreachable_not_a_partial_result(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", f"/containers/{CONTAINER_ID}/stats", Stream([], Ending.CUT))

    with pytest.raises(EnvironmentUnreachable) as raised:
        await drain(engine_client.containers.stats(CONTAINER_ID))

    assert raised.value.environment_id == ENV_ID


def _pull_with_credential(client: EngineClient) -> Awaitable[Any]:
    return drain(client.images.pull("alpine", registry_auth=SecretStr(REGISTRY_SECRET)))


CHAIN_CASES = [
    pytest.param("GET", "/containers/json", Refuse(), _list_containers, id="request-refused"),
    pytest.param("GET", "/containers/json", Disconnect(), _list_containers, id="request-dropped"),
    pytest.param("GET", STATS_PATH, Refuse(), _stream_stats, id="stream-refused"),
    pytest.param("GET", STATS_PATH, Stream([], Ending.CUT), _stream_stats, id="stream-cut"),
    pytest.param("POST", "/images/create", Refuse(), _pull_with_credential, id="pull-refused"),
]


@pytest.mark.parametrize(("method", "path", "behaviour", "call"), CHAIN_CASES)
async def test_a_mapped_transport_error_has_no_cause_or_context_and_no_credential(
    engine: FakeEngine,
    engine_client: EngineClient,
    method: str,
    path: str,
    behaviour: Behaviour,
    call: Call,
) -> None:
    engine.route(method, path, behaviour)

    with pytest.raises(EnvironmentUnreachable) as raised:
        await call(engine_client)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert REGISTRY_SECRET not in "\n".join(failure_texts(raised.value))


OVER_CAP_RESPONSES = [
    pytest.param(
        Reply(200, b"[]", {"Content-Length": "1" + "0" * 12}), id="declared-length-over-the-cap"
    ),
    pytest.param(
        Reply(200, b"[]", {"Content-Length": "9" * 5000}),
        id="declared-length-of-thousands-of-digits",
    ),
    pytest.param(
        Stream([ONE_MEBIBYTE_CHUNK] * CHUNKS_BEYOND_THE_RESPONSE_CAP),
        id="streamed-without-a-length",
    ),
]


@pytest.mark.parametrize("behaviour", OVER_CAP_RESPONSES)
async def test_a_response_body_over_the_cap_is_a_mapped_engine_error(
    engine: FakeEngine, engine_client: EngineClient, behaviour: Behaviour
) -> None:
    engine.route("GET", "/nodes", behaviour)

    with pytest.raises(EngineError) as raised:
        await engine_client.swarm_nodes.list()

    assert type(raised.value) is ResponseTooLarge
    assert raised.value.environment_id == ENV_ID
    assert "too large" in raised.value.reason


async def test_not_found_carries_the_engine_message_and_str_stays_free_of_it(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", INSPECT_PATH, error_reply(404))

    with pytest.raises(NotFound) as raised:
        await engine_client.containers.inspect(CONTAINER_ID)

    assert raised.value.engine_message == f"engine detail {BODY_TOKEN}"
    assert str(raised.value) == f"environment {ENV_ID}: GET {INSPECT_PATH} returned HTTP 404"


async def test_not_found_strips_control_characters_from_the_engine_message(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", INSPECT_PATH, json_reply({"message": RAW_ENGINE_MESSAGE}, 404))

    with pytest.raises(NotFound) as raised:
        await engine_client.containers.inspect(CONTAINER_ID)

    assert raised.value.engine_message == CLEAN_ENGINE_MESSAGE


async def test_not_found_truncates_a_long_engine_message_to_the_cap(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", INSPECT_PATH, json_reply({"message": "m" * (ENGINE_MESSAGE_CAP * 3)}, 404))

    with pytest.raises(NotFound) as raised:
        await engine_client.containers.inspect(CONTAINER_ID)

    assert raised.value.engine_message == "m" * ENGINE_MESSAGE_CAP


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<html>not json</html>", id="not-json"),
        pytest.param(b'["message"]', id="not-an-object"),
        pytest.param(b'{"message": 5}', id="message-not-a-string"),
        pytest.param(b'{"message": "\\n\\u2028"}', id="only-control-characters"),
        pytest.param(b"[" * 100_000, id="nested-past-the-recursion-limit"),
    ],
)
async def test_not_found_has_no_engine_message_when_the_body_offers_no_usable_text(
    engine: FakeEngine, engine_client: EngineClient, body: bytes
) -> None:
    engine.route("GET", INSPECT_PATH, Reply(404, body))

    with pytest.raises(NotFound) as raised:
        await engine_client.containers.inspect(CONTAINER_ID)

    assert raised.value.engine_message is None


@pytest.mark.parametrize(
    ("status", "error_type"), [(409, Conflict), (403, PermissionDenied), (500, EngineError)]
)
async def test_only_not_found_carries_an_engine_message(
    engine: FakeEngine, engine_client: EngineClient, status: int, error_type: type[EngineError]
) -> None:
    engine.route("GET", "/containers/json", error_reply(status))

    with pytest.raises(error_type) as raised:
        await engine_client.containers.list()

    assert raised.value.engine_message is None
