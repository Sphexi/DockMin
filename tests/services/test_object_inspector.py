"""The inspector's reads of containers, volumes and networks: matching, memoising and caps."""

from typing import Any

import pytest

import app.docker_engine.resources as resources
from app.auth.errors import ResourceNotFound
from app.docker_engine.models import Volume
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from tests.access_support import ENV_A, container_id
from tests.docker_engine.container_engine import BRIDGE_ID, ContainerEngine, swarm_network_id
from tests.docker_engine.fake_engine import Refuse
from tests.services.conftest import ContainerRig

CONTAINER = container_id(0xA1)
NETWORK_NUMBER = 0xE1
LOOK_ALIKE = "a" * 64
SWARM_LOOK_ALIKE = "a" * 25
SMALL_CAP = 10


async def test_a_volume_is_read_by_name_and_remembered_for_the_rest_of_the_request(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    host.add_volume("data", options={"o": "bind"})

    first = await rig.inspector.volume(ENV_A, "data")
    again = await rig.inspector.volume(ENV_A, "data")

    assert first is again and first.options == {"o": "bind"}
    assert len(host.calls("GET", "/volumes/data")) == 1


@pytest.mark.parametrize("name", ["absent", "-leading", "has space", "../x", "a" * 256, ""])
async def test_a_volume_that_is_absent_or_has_an_invalid_name_is_not_found(
    rig: ContainerRig, host: ContainerEngine, name: str
) -> None:
    with pytest.raises(ResourceNotFound):
        await rig.inspector.volume(ENV_A, name)


async def test_a_volume_the_host_returns_under_another_name_is_not_found(
    rig: ContainerRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await rig.inspector.hosts.client(ENV_A)

    async def answers_for_another_volume(name: str) -> Volume:
        return Volume.model_validate({"Name": "other"})

    monkeypatch.setattr(client.volumes, "inspect", answers_for_another_volume)

    with pytest.raises(ResourceNotFound):
        await rig.inspector.volume(ENV_A, "data")


async def test_a_network_is_read_by_its_full_id(rig: ContainerRig, host: ContainerEngine) -> None:
    network_id = host.add_network("appnet", NETWORK_NUMBER)

    network = await rig.inspector.network(ENV_A, network_id)

    assert (network.id, network.name) == (network_id, "appnet")


async def test_a_swarm_network_is_read_by_its_25_character_id(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    network_id = host.add_network("ov", 0, "overlay", network_id=swarm_network_id(4), Scope="swarm")

    network = await rig.inspector.network(ENV_A, network_id)

    assert (network.id, network.name, network.scope) == (network_id, "ov", "swarm")


async def test_a_swarm_id_shaped_name_does_not_resolve_to_the_network_with_that_name(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    host.add_network(SWARM_LOOK_ALIKE, NETWORK_NUMBER)

    with pytest.raises(ResourceNotFound):
        await rig.inspector.network(ENV_A, SWARM_LOOK_ALIKE)


@pytest.mark.parametrize("reference", ["z" * 24, "z" * 26, "Z" * 25, "z" * 24 + "-"])
async def test_a_reference_near_the_swarm_id_shape_is_not_found_without_a_request(
    rig: ContainerRig, host: ContainerEngine, reference: str
) -> None:
    with pytest.raises(ResourceNotFound):
        await rig.inspector.network(ENV_A, reference)

    assert host.calls("GET", f"/networks/{reference}") == []


@pytest.mark.parametrize("reference", ["bridge", "bbbb", BRIDGE_ID[:12], "B" * 64, ""])
async def test_a_network_asked_for_by_anything_but_a_full_id_is_not_found(
    rig: ContainerRig, host: ContainerEngine, reference: str
) -> None:
    with pytest.raises(ResourceNotFound):
        await rig.inspector.network(ENV_A, reference)

    assert host.calls("GET", f"/networks/{reference}") == []


async def test_a_name_that_looks_like_an_id_does_not_resolve_to_the_network_with_that_name(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    host.add_network(LOOK_ALIKE, NETWORK_NUMBER)

    with pytest.raises(ResourceNotFound):
        await rig.inspector.network(ENV_A, LOOK_ALIKE)


async def test_a_network_named_is_found_by_its_exact_name_and_remembered_under_its_id(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    network_id = host.add_network("appnet", NETWORK_NUMBER)

    named = await rig.inspector.network_named(ENV_A, "appnet")
    by_id = await rig.inspector.network(ENV_A, network_id)

    assert named is by_id
    assert len(host.calls("GET", "/networks/appnet")) == 1
    assert host.calls("GET", f"/networks/{network_id}") == []


@pytest.mark.parametrize("name", ["absent", "bbbb", BRIDGE_ID[:12], "-x", "a b", "a" * 256])
async def test_a_network_named_that_matches_only_an_id_prefix_or_nothing_is_not_found(
    rig: ContainerRig, host: ContainerEngine, name: str
) -> None:
    with pytest.raises(ResourceNotFound):
        await rig.inspector.network_named(ENV_A, name)


@pytest.mark.parametrize(
    ("limit", "read"),
    [
        pytest.param(
            "VOLUME_INSPECT_BYTES", lambda inspector: inspector.volume(ENV_A, "data"), id="volume"
        ),
        pytest.param(
            "NETWORK_INSPECT_BYTES",
            lambda inspector: inspector.network(ENV_A, BRIDGE_ID),
            id="network",
        ),
        pytest.param(
            "NETWORK_INSPECT_BYTES",
            lambda inspector: inspector.network_named(ENV_A, "bridge"),
            id="network-by-name",
        ),
    ],
)
async def test_an_object_over_the_read_limit_is_too_large_and_nothing_partial_is_used(
    rig: ContainerRig,
    host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
    read: Any,
) -> None:
    host.add_volume("data")
    monkeypatch.setattr(resources, limit, SMALL_CAP)

    with pytest.raises(ObjectTooLarge):
        await read(rig.inspector)


async def test_labels_are_served_for_a_container_a_volume_by_name_and_a_network_by_id(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    host.add(CONTAINER, "web", labels={"tier": "front"})
    host.add_volume("data")
    network_id = host.add_network("appnet", NETWORK_NUMBER)

    labels = [
        await rig.inspector.labels_of(ENV_A, "container", CONTAINER),
        await rig.inspector.labels_of(ENV_A, "volume", "data"),
        await rig.inspector.labels_of(ENV_A, "network", network_id),
    ]

    assert labels == [{"tier": "front"}, {}, {}]


async def test_labels_are_served_for_a_swarm_network_by_its_25_character_id(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    network_id = host.add_network(
        "ov", 0, "overlay", network_id=swarm_network_id(4), Labels={"tier": "back"}
    )

    assert await rig.inspector.labels_of(ENV_A, "network", network_id) == {"tier": "back"}


async def test_labels_of_an_object_type_with_no_inspector_is_a_programming_error(
    rig: ContainerRig,
) -> None:
    with pytest.raises(RuntimeError):
        await rig.inspector.labels_of(ENV_A, "stack", "shop")


async def test_an_unreachable_host_is_reported_as_unavailable_without_engine_text(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    host.route("GET", "/volumes/data", Refuse())
    host.add_volume("data")

    with pytest.raises(EnvironmentUnavailable) as unavailable:
        await rig.inspector.volume(ENV_A, "data")

    assert unavailable.value.kind == "unreachable"
