"""Tests that timeouts are per-call arguments, never module state."""

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

import app.docker_engine as package
from app.docker_engine import EngineClient, Timeouts
from app.docker_engine.engine_http import EngineHttp
from tests.docker_engine.fake_engine import FakeEngine, Reply, Stream
from tests.docker_engine.helpers import CONTAINER_ID, drain

STANDARD = {"connect": 5.0, "read": 30.0, "write": 10.0, "pool": 5.0}
CUSTOM = Timeouts(connect=1.5, read=2.5, write=3.5, pool=4.5)
CUSTOM_EXTENSION = {"connect": 1.5, "read": 2.5, "write": 3.5, "pool": 4.5}
STANDARD_READ = 30.0
LONG_READ = 600.0
NO_READ_LIMIT = None
PROBE = "/probe"
HIJACK_DEFAULT_NAMES = {"DEFAULT_CONNECT_TIMEOUT", "DEFAULT_HANDSHAKE_TIMEOUT"}

type Call = Callable[[EngineClient], Awaitable[Any]]


def _modules() -> list[Any]:
    names = [m.name for m in pkgutil.iter_modules(package.__path__, f"{package.__name__}.")]
    return [importlib.import_module(name) for name in names]


def _timeout_globals() -> list[str]:
    return [
        f"{module.__name__}.{name}"
        for module in _modules()
        for name, value in vars(module).items()
        if isinstance(value, Timeouts | httpx.Timeout)
    ]


def _timeout_constants() -> set[str]:
    return {
        name
        for module in _modules()
        for name, value in vars(module).items()
        if "TIMEOUT" in name and isinstance(value, int | float)
    }


async def test_a_call_without_timeouts_uses_the_standard_profile(
    engine: FakeEngine, engine_http: EngineHttp
) -> None:
    engine.route("GET", PROBE, Reply(body=b"{}"))

    await engine_http.get_json(PROBE)

    assert engine.calls(path=PROBE)[0].timeout == STANDARD


async def test_timeouts_passed_to_a_call_are_the_ones_applied(
    engine: FakeEngine, engine_http: EngineHttp
) -> None:
    engine.route("GET", PROBE, Reply(body=b"{}"))

    await engine_http.get_json(PROBE, timeouts=CUSTOM)

    assert engine.calls(path=PROBE)[0].timeout == CUSTOM_EXTENSION


async def test_a_call_after_one_with_custom_timeouts_is_back_on_the_standard_profile(
    engine: FakeEngine, engine_http: EngineHttp
) -> None:
    engine.route("GET", PROBE, Reply(body=b"{}"))
    await engine_http.get_json(PROBE, timeouts=CUSTOM)

    await engine_http.get_json(PROBE)

    assert engine.calls(path=PROBE)[-1].timeout == STANDARD


async def _list(client: EngineClient) -> None:
    await client.containers.list()


async def _stop(client: EngineClient) -> None:
    await client.containers.stop(CONTAINER_ID)


async def _restart(client: EngineClient) -> None:
    await client.containers.restart(CONTAINER_ID)


async def _pull(client: EngineClient) -> None:
    await drain(client.images.pull("alpine"))


async def _stats(client: EngineClient) -> None:
    await drain(client.containers.stats(CONTAINER_ID))


async def _logs(client: EngineClient) -> None:
    await drain(client.containers.logs(CONTAINER_ID, tty=False))


PROFILE_CASES = [
    pytest.param("GET", "/containers/json", Reply(body=b"[]"), _list, STANDARD_READ, id="list"),
    pytest.param(
        "POST", f"/containers/{CONTAINER_ID}/stop", Reply(204), _stop, LONG_READ, id="stop"
    ),
    pytest.param(
        "POST", f"/containers/{CONTAINER_ID}/restart", Reply(204), _restart, LONG_READ, id="restart"
    ),
    pytest.param("POST", "/images/create", Stream([]), _pull, LONG_READ, id="pull"),
    pytest.param(
        "GET", f"/containers/{CONTAINER_ID}/stats", Stream([]), _stats, NO_READ_LIMIT, id="stats"
    ),
    pytest.param(
        "GET", f"/containers/{CONTAINER_ID}/logs", Stream([]), _logs, NO_READ_LIMIT, id="logs"
    ),
]


@pytest.mark.parametrize(("method", "path", "behaviour", "call", "read"), PROFILE_CASES)
async def test_each_operation_uses_the_profile_suited_to_how_long_it_may_stay_quiet(
    engine: FakeEngine,
    engine_client: EngineClient,
    method: str,
    path: str,
    behaviour: Reply | Stream,
    call: Call,
    read: float | None,
) -> None:
    engine.route(method, path, behaviour)

    await call(engine_client)

    sent = engine.calls(method, path)[0].timeout
    assert sent is not None
    assert sent["read"] == read


def test_no_module_holds_a_timeout_value_as_a_default() -> None:
    assert _timeout_globals() == []
    assert _timeout_constants() == HIJACK_DEFAULT_NAMES
