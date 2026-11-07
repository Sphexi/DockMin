"""Tests for version negotiation in app/docker_engine/engine_http.py and versioning.py."""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from app.docker_engine import EngineClient, EngineError, VersionUnsupported
from app.docker_engine.models import DaemonInfo, SwarmState
from app.docker_engine.versioning import parse_api_version
from tests.docker_engine.fake_engine import FakeEngine, json_reply
from tests.docker_engine.helpers import ENV_ID

type ClientFactory = Callable[[FakeEngine], EngineClient]

CLIENT_MAX = "1.51"
ARABIC_INDIC_FIVE = chr(0x665)


@pytest.mark.parametrize(
    ("daemon_api", "pinned"),
    [
        ("1.51", "1.51"),
        ("1.47", "1.47"),
        ("1.44", "1.44"),
        ("1.99", CLIENT_MAX),
        ("1.100", CLIENT_MAX),
    ],
)
async def test_negotiation_pins_the_lower_of_the_daemon_and_client_maximum_into_every_path(
    make_client: ClientFactory, daemon_api: str, pinned: str
) -> None:
    engine = FakeEngine(api_version=daemon_api)
    engine.route("GET", "/containers/json", json_reply([]))
    client = make_client(engine)

    await client.containers.list()

    assert client.daemon is not None
    assert client.daemon.negotiated_api_version == pinned
    assert engine.calls(path="/_ping")[0].version is None
    assert engine.calls(path="/version")[0].version == pinned
    assert engine.calls(path="/containers/json")[0].version == pinned


async def test_negotiation_records_the_daemon_version_api_range_and_swarm_state(
    make_client: ClientFactory,
) -> None:
    engine = FakeEngine(
        api_version="1.49", min_api_version="1.44", engine_version="28.0.1", swarm="active/manager"
    )
    client = make_client(engine)

    info = await client.negotiate()

    assert info == DaemonInfo(
        engine_version="28.0.1",
        daemon_api_version="1.49",
        min_api_version="1.44",
        negotiated_api_version="1.49",
        swarm_state=SwarmState.MANAGER,
    )


@pytest.mark.parametrize("daemon_api", ["1.43", "1.24", "1.9"])
async def test_a_daemon_below_the_floor_is_unsupported_before_any_real_call_is_sent(
    make_client: ClientFactory, daemon_api: str
) -> None:
    engine = FakeEngine(api_version=daemon_api)
    client = make_client(engine)

    with pytest.raises(VersionUnsupported) as raised:
        await client.containers.list()

    assert raised.value.environment_id == ENV_ID
    assert raised.value.daemon_api_version == daemon_api
    assert engine.paths() == ["GET /_ping"]
    assert client.daemon is None


async def test_a_daemon_whose_minimum_exceeds_the_client_maximum_is_unsupported_at_negotiation(
    make_client: ClientFactory,
) -> None:
    engine = FakeEngine(api_version="1.99", min_api_version="1.60")
    client = make_client(engine)

    with pytest.raises(VersionUnsupported) as raised:
        await client.containers.list()

    assert raised.value.environment_id == ENV_ID
    assert raised.value.daemon_api_version == "1.99"
    assert engine.paths() == ["GET /_ping", "GET /version"]


async def test_a_daemon_whose_minimum_equals_the_client_maximum_is_accepted(
    make_client: ClientFactory,
) -> None:
    client = make_client(FakeEngine(api_version="1.99", min_api_version=CLIENT_MAX))

    info = await client.negotiate()

    assert info.negotiated_api_version == CLIENT_MAX


@pytest.mark.parametrize(
    "engine_kwargs",
    [
        {"api_version": None},
        {"api_version": "latest"},
        {"min_api_version": "one.forty"},
    ],
    ids=["missing-header", "malformed-header", "malformed-minimum"],
)
async def test_an_unreadable_version_is_a_plain_engine_error_not_a_version_error(
    make_client: ClientFactory, engine_kwargs: dict[str, Any]
) -> None:
    client = make_client(FakeEngine(**engine_kwargs))

    with pytest.raises(EngineError) as raised:
        await client.negotiate()

    assert type(raised.value) is EngineError
    assert raised.value.environment_id == ENV_ID


async def test_concurrent_first_calls_negotiate_once(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", "/containers/json", json_reply([]))
    engine.route("GET", "/images/json", json_reply([]))

    await asyncio.gather(engine_client.containers.list(), engine_client.images.list())

    assert len(engine.calls(path="/_ping")) == 1


@pytest.mark.parametrize(
    ("header", "state"),
    [
        ("inactive", SwarmState.INACTIVE),
        ("active/manager", SwarmState.MANAGER),
        ("active/worker", SwarmState.WORKER),
        ("pending", SwarmState.PENDING),
        ("locked", SwarmState.LOCKED),
        ("error", SwarmState.ERROR),
        (None, SwarmState.UNKNOWN),
        ("something-new", SwarmState.UNKNOWN),
    ],
)
def test_swarm_header_maps_to_a_state(header: str | None, state: SwarmState) -> None:
    assert SwarmState.from_header(header) is state


@pytest.mark.parametrize(
    ("text", "parsed"),
    [("1.51", (1, 51)), (" 1.44 ", (1, 44)), ("1.9", (1, 9)), ("1.100", (1, 100))],
)
def test_api_version_parses_numerically(text: str, parsed: tuple[int, int]) -> None:
    assert parse_api_version(text) == parsed


@pytest.mark.parametrize(
    "text", ["", "1", "1.", ".5", "1.5.2", "a.b", "-1.2", "1." + ARABIC_INDIC_FIVE]
)
def test_api_version_rejects_anything_but_two_ascii_numbers(text: str) -> None:
    with pytest.raises(ValueError, match="malformed"):
        parse_api_version(text)
