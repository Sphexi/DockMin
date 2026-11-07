"""An Engine endpoint the address policy refuses is never dialled and never retried as a fault."""

from collections.abc import Callable

import pytest

from app.docker_engine import EngineClient, EnvironmentUnreachable
from app.docker_engine.engine_http import AddressRefusedUnreachable
from app.docker_engine.transport import EndpointAddressRefused, TcpSettings, TlsMaterial
from app.netpolicy import REFUSAL_TEXT
from tests.docker_engine.fake_engine import FakeEngine, json_reply
from tests.docker_engine.helpers import (
    CONNECT_TIMEOUT_SECONDS,
    CONTAINER_SUMMARY,
    ENV_ID,
)


async def test_a_call_to_an_internal_endpoint_is_refused_as_its_own_kind_and_nothing_is_sent(
    engine: FakeEngine, tls_settings: TcpSettings
) -> None:
    async with EngineClient(ENV_ID, tls_settings.create_transport()) as client:
        with pytest.raises(EnvironmentUnreachable) as raised:
            await client.containers.list()

    assert isinstance(raised.value, AddressRefusedUnreachable)
    assert raised.value.kind == "address_refused"
    assert raised.value.environment_id == ENV_ID
    assert REFUSAL_TEXT in str(raised.value)
    assert engine.requests == []


async def test_a_host_name_that_resolves_to_an_internal_address_is_refused_the_same_way(
    engine: FakeEngine,
    tls_port: int,
    tls_material: TlsMaterial,
    fake_hosts: dict[str, str],
) -> None:
    fake_hosts["engine.example.test"] = "127.0.0.1"
    settings = TcpSettings("engine.example.test", tls_port, tls_material)

    async with EngineClient(ENV_ID, settings.create_transport()) as client:
        with pytest.raises(AddressRefusedUnreachable):
            await client.containers.list()

    assert engine.requests == []


async def test_a_raw_stream_to_an_internal_endpoint_is_refused_before_connecting(
    engine: FakeEngine, tls_settings: TcpSettings
) -> None:
    with pytest.raises(EndpointAddressRefused) as raised:
        await tls_settings.open_stream(CONNECT_TIMEOUT_SECONDS)

    assert raised.value.address == "127.0.0.1"
    assert engine.requests == []


async def test_the_refusal_is_not_a_transient_fault_the_client_would_retry(
    engine: FakeEngine, tls_settings: TcpSettings
) -> None:
    async with EngineClient(ENV_ID, tls_settings.create_transport()) as client:
        with pytest.raises(AddressRefusedUnreachable):
            await client.containers.list()
        with pytest.raises(AddressRefusedUnreachable):
            await client.containers.list()

    assert engine.requests == []


async def test_a_permit_for_the_address_lets_the_same_endpoint_serve_calls(
    engine: FakeEngine, tls_settings: TcpSettings, address_policy: Callable[..., None]
) -> None:
    address_policy("127.0.0.0/8")
    engine.route("GET", "/containers/json", json_reply([CONTAINER_SUMMARY]))

    async with EngineClient(ENV_ID, tls_settings.create_transport()) as client:
        containers = await client.containers.list()

    assert [c.id for c in containers] == ["c0ffee"]
