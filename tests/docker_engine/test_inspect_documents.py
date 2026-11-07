"""Tests for the raw inspect documents of containers and networks."""

from collections.abc import AsyncIterator

import pytest

from app.docker_engine import EngineClient
from app.docker_engine.errors import EngineError, NotFound
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.helpers import ENV_ID

CONTAINER_ID = "c" * 64
NETWORK_NUMBER = 0xAB
LABELS = {"com.docker.compose.project": "shop"}
HOST_CONFIG = {"NetworkMode": "bridge", "ExtraKeyTheModelDoesNotKnow": {"kept": True}}


@pytest.fixture
def daemon() -> ContainerEngine:
    engine = ContainerEngine()
    engine.add(CONTAINER_ID, "shop-web-1", labels=LABELS, host_config=HOST_CONFIG)
    return engine


@pytest.fixture
async def client(daemon: ContainerEngine) -> AsyncIterator[EngineClient]:
    async with EngineClient(ENV_ID, daemon.bundle()) as opened:
        yield opened


async def test_a_container_comes_back_as_the_document_the_engine_sent(
    client: EngineClient,
) -> None:
    document = await client.containers.inspect_document(CONTAINER_ID)

    assert document["Id"] == CONTAINER_ID
    assert document["Config"]["Labels"] == LABELS
    assert document["HostConfig"] == HOST_CONFIG


async def test_a_missing_container_is_not_found(client: EngineClient) -> None:
    with pytest.raises(NotFound):
        await client.containers.inspect_document("d" * 64)


async def test_an_identifier_that_is_not_plain_is_refused_before_a_request(
    client: EngineClient, daemon: ContainerEngine
) -> None:
    with pytest.raises(EngineError):
        await client.containers.inspect_document("../images/json")

    assert daemon.requests == []


async def test_a_network_comes_back_as_the_document_the_engine_sent(
    client: EngineClient, daemon: ContainerEngine
) -> None:
    network_id = daemon.add_network("shop_default", NETWORK_NUMBER, Labels=LABELS)

    document = await client.networks.inspect_document(network_id)

    assert (document["Id"], document["Name"], document["Labels"]) == (
        network_id,
        "shop_default",
        LABELS,
    )
