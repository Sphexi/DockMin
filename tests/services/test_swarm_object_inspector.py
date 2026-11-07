"""The object inspector over Swarm objects: ids checked, replies checked, memo per request."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

import app.docker_engine.resources as engine_resources
from app.auth.errors import ResourceNotFound
from app.services.object_inspector import EngineObjectInspector, ObjectTooLarge
from tests.access_support import ENV_A
from tests.docker_engine.swarm_engine import MANAGER, plain_spec
from tests.services.conftest import SwarmRig
from tests.swarm_support import MISSING_ID, NAMESPACE_LABEL

Read = Callable[[EngineObjectInspector, str], Awaitable[Any]]

READS: list[tuple[str, Read]] = [
    ("service", lambda i, x: i.service(ENV_A, x)),
    ("service document", lambda i, x: i.service_document(ENV_A, x)),
    ("config", lambda i, x: i.config(ENV_A, x)),
    ("secret", lambda i, x: i.secret(ENV_A, x)),
    ("node", lambda i, x: i.node(ENV_A, x)),
    ("task", lambda i, x: i.task(ENV_A, x)),
]
BAD_IDS = ["", "web", "A" * 25, "a" * 24, "a" * 26, "../x", "x y"]


@pytest.mark.parametrize("read", [pytest.param(r, id=n) for n, r in READS])
@pytest.mark.parametrize("bad", BAD_IDS)
async def test_an_id_that_is_not_25_lower_case_base_36_is_not_found_without_an_engine_call(
    swarm: SwarmRig, read: Read, bad: str
) -> None:
    swarm.host.requests.clear()

    with pytest.raises(ResourceNotFound):
        await read(swarm.inspector, bad)

    assert swarm.host.requests == []


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda i, x: i.service(ENV_A, x), id="service"),
        pytest.param(lambda i, x: i.service_document(ENV_A, x), id="service document"),
    ],
)
async def test_a_name_shaped_like_an_id_cannot_stand_in_for_another_services_id(
    swarm: SwarmRig, read: Read
) -> None:
    swarm.host.add_service(plain_spec(MISSING_ID))

    with pytest.raises(ResourceNotFound):
        await read(swarm.inspector, MISSING_ID)


async def test_a_config_or_secret_named_like_an_id_cannot_stand_in_for_one_with_that_id(
    swarm: SwarmRig,
) -> None:
    swarm.host.add_config(MISSING_ID)
    swarm.host.add_secret(MISSING_ID)

    with pytest.raises(ResourceNotFound):
        await swarm.inspector.config(ENV_A, MISSING_ID)
    with pytest.raises(ResourceNotFound):
        await swarm.inspector.secret(ENV_A, MISSING_ID)


async def test_a_second_read_in_one_request_is_remembered_and_forgetting_reads_again(
    swarm: SwarmRig,
) -> None:
    service_id = swarm.host.add_service(plain_spec("web"))
    path = f"/services/{service_id}"

    await swarm.inspector.service(ENV_A, service_id)
    await swarm.inspector.service(ENV_A, service_id)
    remembered = len(swarm.host.calls("GET", path))
    swarm.host.services[service_id]["Spec"]["Name"] = "renamed"
    swarm.inspector.forget_service(ENV_A, service_id)
    fresh = await swarm.inspector.service(ENV_A, service_id)

    assert remembered == 1 and fresh.spec.name == "renamed"
    assert len(swarm.host.calls("GET", path)) == 2


async def test_a_config_is_found_by_name_and_remembered_under_its_id(swarm: SwarmRig) -> None:
    config_id = swarm.host.add_config("conf", b"data")

    named = await swarm.inspector.config_named(ENV_A, "conf")
    by_id = await swarm.inspector.config(ENV_A, config_id)

    assert named.config.id == config_id and by_id is named
    assert swarm.host.calls("GET", f"/configs/{config_id}") == []


@pytest.mark.parametrize("name", ["", "x" * 65])
async def test_a_config_or_secret_name_outside_the_length_limit_is_not_found_unasked(
    swarm: SwarmRig, name: str
) -> None:
    swarm.host.requests.clear()

    with pytest.raises(ResourceNotFound):
        await swarm.inspector.config_named(ENV_A, name)
    with pytest.raises(ResourceNotFound):
        await swarm.inspector.secret_named(ENV_A, name)

    assert swarm.host.requests == []


async def test_a_secret_read_carries_no_value_even_when_the_daemon_sends_one(
    swarm: SwarmRig,
) -> None:
    swarm.host.secret_data_visible = True
    secret_id = swarm.host.add_secret("s", data=b"topsecret")

    secret = await swarm.inspector.secret(ENV_A, secret_id)

    assert "topsecret" not in repr(secret) + secret.model_dump_json()


async def test_labels_come_from_the_spec_the_config_or_the_secret(swarm: SwarmRig) -> None:
    service_id = swarm.host.add_service(plain_spec("web", Labels={NAMESPACE_LABEL: "Shop"}))
    config_id = swarm.host.add_config("c", labels={"k": "config"})
    secret_id = swarm.host.add_secret("s", labels={"k": "secret"})

    assert await swarm.inspector.labels_of(ENV_A, "service", service_id) == {
        NAMESPACE_LABEL: "Shop"
    }
    assert await swarm.inspector.labels_of(ENV_A, "config", config_id) == {"k": "config"}
    assert await swarm.inspector.labels_of(ENV_A, "secret", secret_id) == {"k": "secret"}


async def test_a_reply_over_the_read_limit_is_object_too_large(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    service_id = swarm.host.add_service(plain_spec("web"))
    monkeypatch.setattr(engine_resources, "SERVICE_INSPECT_BYTES", 16)

    with pytest.raises(ObjectTooLarge):
        await swarm.inspector.service(ENV_A, service_id)


async def test_a_node_is_read_by_its_canonical_id_only(swarm: SwarmRig) -> None:
    node = await swarm.inspector.node(ENV_A, MANAGER)

    assert node.id == MANAGER and node.description.hostname == "mgr"
