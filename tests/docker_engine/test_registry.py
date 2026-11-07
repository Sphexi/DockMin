"""Tests for the per-environment client registry in app/docker_engine/registry.py."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.docker_engine import EngineClientRegistry, VersionUnsupported
from app.docker_engine.transport import ConnectionSettings, TransportBundle, UnixSocketSettings
from tests.docker_engine.fake_engine import Ending, FakeEngine, Reply, Stream
from tests.docker_engine.helpers import (
    ENV_ID,
    HANG_GUARD_SECONDS,
    NEAR_ZERO_BACKOFF,
    OTHER_ENV_ID,
    event_payload,
    json_line,
)

SETTINGS = UnixSocketSettings()
OTHER_SETTINGS = UnixSocketSettings("/run/other-docker.sock")
FIRST_PRINT = "fingerprint-one"
SECOND_PRINT = "fingerprint-two"
ENDED = object()


@pytest.fixture
async def registry(engine: FakeEngine) -> AsyncIterator[EngineClientRegistry]:
    """Return a registry whose clients all talk to the default fake."""

    def factory(_settings: ConnectionSettings) -> TransportBundle:
        return engine.bundle()

    created = EngineClientRegistry(factory)
    yield created
    await created.aclose()


def test_a_new_registry_builds_no_client_and_sends_nothing(engine: FakeEngine) -> None:
    EngineClientRegistry(lambda _settings: engine.bundle())

    assert engine.transports == []
    assert engine.requests == []


async def test_get_returns_a_negotiated_client_for_the_environment(
    engine: FakeEngine, registry: EngineClientRegistry
) -> None:
    client = await registry.get(ENV_ID, SETTINGS, FIRST_PRINT)

    assert client.environment_id == ENV_ID
    assert client.daemon is not None
    assert client.daemon.negotiated_api_version == "1.51"
    assert engine.paths() == ["GET /_ping", "GET /version"]


async def test_get_reuses_the_client_while_the_fingerprint_and_settings_are_unchanged(
    engine: FakeEngine, registry: EngineClientRegistry
) -> None:
    first = await registry.get(ENV_ID, SETTINGS, FIRST_PRINT)

    second = await registry.get(ENV_ID, UnixSocketSettings(SETTINGS.path), FIRST_PRINT)

    assert second is first
    assert len(engine.transports) == 1


async def test_get_replaces_and_closes_the_client_when_the_fingerprint_changes(
    engine: FakeEngine, registry: EngineClientRegistry
) -> None:
    first = await registry.get(ENV_ID, SETTINGS, FIRST_PRINT)

    second = await registry.get(ENV_ID, SETTINGS, SECOND_PRINT)

    assert second is not first
    assert first.closed
    assert not second.closed
    assert [t.closed for t in engine.transports] == [True, False]


async def test_get_replaces_and_closes_the_client_when_only_the_settings_change(
    engine: FakeEngine, registry: EngineClientRegistry
) -> None:
    first = await registry.get(ENV_ID, SETTINGS, FIRST_PRINT)

    second = await registry.get(ENV_ID, OTHER_SETTINGS, FIRST_PRINT)

    assert second is not first
    assert first.closed
    assert not second.closed
    assert [t.closed for t in engine.transports] == [True, False]


async def test_get_replaces_a_client_that_was_closed_even_when_the_fingerprint_matches(
    engine: FakeEngine, registry: EngineClientRegistry
) -> None:
    first = await registry.get(ENV_ID, SETTINGS, FIRST_PRINT)
    await first.aclose()

    second = await registry.get(ENV_ID, SETTINGS, FIRST_PRINT)

    assert second is not first
    assert not second.closed


async def test_get_keeps_a_separate_client_for_each_environment(
    engine: FakeEngine, registry: EngineClientRegistry
) -> None:
    first = await registry.get(ENV_ID, SETTINGS, FIRST_PRINT)

    other = await registry.get(OTHER_ENV_ID, SETTINGS, FIRST_PRINT)

    assert other is not first
    assert other.environment_id == OTHER_ENV_ID
    assert not first.closed


async def test_concurrent_gets_for_one_environment_build_one_client(
    engine: FakeEngine, registry: EngineClientRegistry
) -> None:
    clients = await asyncio.gather(
        registry.get(ENV_ID, SETTINGS, FIRST_PRINT), registry.get(ENV_ID, SETTINGS, FIRST_PRINT)
    )

    assert clients[0] is clients[1]
    assert len(engine.transports) == 1


async def test_get_surfaces_an_unsupported_version_and_closes_the_client_it_built(
    registry: EngineClientRegistry, engine: FakeEngine
) -> None:
    engine.route("GET", "/_ping", Reply(headers={"Api-Version": "1.40"}))

    with pytest.raises(VersionUnsupported) as raised:
        await registry.get(ENV_ID, SETTINGS, FIRST_PRINT)

    assert raised.value.environment_id == ENV_ID
    assert [t.closed for t in engine.transports] == [True]


async def test_a_client_that_failed_to_negotiate_is_not_cached(
    registry: EngineClientRegistry, engine: FakeEngine
) -> None:
    engine.route("GET", "/_ping", Reply(headers={"Api-Version": "1.40"}))
    with pytest.raises(VersionUnsupported):
        await registry.get(ENV_ID, SETTINGS, FIRST_PRINT)
    engine.route("GET", "/_ping", Reply(headers={"Api-Version": "1.51"}))

    client = await registry.get(ENV_ID, SETTINGS, FIRST_PRINT)

    assert client.daemon is not None
    assert len(engine.transports) == 2


async def test_invalidate_closes_the_client_and_the_next_get_builds_a_new_one(
    engine: FakeEngine, registry: EngineClientRegistry
) -> None:
    first = await registry.get(ENV_ID, SETTINGS, FIRST_PRINT)

    await registry.invalidate(ENV_ID)

    assert first.closed
    assert await registry.get(ENV_ID, SETTINGS, FIRST_PRINT) is not first


async def test_aclose_closes_every_client(
    engine: FakeEngine, registry: EngineClientRegistry
) -> None:
    await registry.get(ENV_ID, SETTINGS, FIRST_PRINT)
    await registry.get(OTHER_ENV_ID, SETTINGS, FIRST_PRINT)

    await registry.aclose()

    assert [t.closed for t in engine.transports] == [True, True]


async def test_aclose_ends_a_running_events_watcher_and_leaves_no_open_client(
    engine: FakeEngine, registry: EngineClientRegistry
) -> None:
    engine.route("GET", "/events", Stream([json_line(event_payload(1))], Ending.HOLD))
    client = await registry.get(ENV_ID, SETTINGS, FIRST_PRINT)
    events = client.system.events(backoff=NEAR_ZERO_BACKOFF)
    await anext(events)
    waiting = asyncio.ensure_future(anext(events, ENDED))

    await registry.aclose()
    outcome = await asyncio.wait_for(waiting, HANG_GUARD_SECONDS)

    assert outcome is ENDED
    assert client.closed
    assert [t.closed for t in engine.transports] == [True]
    assert len(engine.calls("GET", "/events")) == 1


async def test_client_for_builds_settings_only_on_a_miss(
    engine: FakeEngine, registry: EngineClientRegistry
) -> None:
    built: list[ConnectionSettings] = []

    def factory() -> ConnectionSettings:
        built.append(SETTINGS)
        return SETTINGS

    first = await registry.client_for(ENV_ID, FIRST_PRINT, factory)
    second = await registry.client_for(ENV_ID, FIRST_PRINT, factory)

    assert second is first
    assert len(built) == 1


async def test_client_for_replaces_and_closes_the_client_when_the_fingerprint_changes(
    engine: FakeEngine, registry: EngineClientRegistry
) -> None:
    first = await registry.client_for(ENV_ID, FIRST_PRINT, lambda: SETTINGS)

    second = await registry.client_for(ENV_ID, SECOND_PRINT, lambda: OTHER_SETTINGS)

    assert second is not first
    assert first.closed
    assert not second.closed


async def test_client_for_keeps_the_existing_client_when_the_factory_raises(
    engine: FakeEngine, registry: EngineClientRegistry
) -> None:
    first = await registry.client_for(ENV_ID, FIRST_PRINT, lambda: SETTINGS)

    def unreadable() -> ConnectionSettings:
        raise ValueError("material unavailable")

    with pytest.raises(ValueError, match="material unavailable"):
        await registry.client_for(ENV_ID, SECOND_PRINT, unreadable)

    assert not first.closed
    assert await registry.client_for(ENV_ID, FIRST_PRINT, unreadable) is first


async def test_client_for_accepts_an_asynchronous_factory(
    engine: FakeEngine, registry: EngineClientRegistry
) -> None:
    async def factory() -> ConnectionSettings:
        return SETTINGS

    client = await registry.client_for(ENV_ID, FIRST_PRINT, factory)

    assert client.environment_id == ENV_ID
