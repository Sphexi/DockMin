"""Tests for the typed call surface in app/docker_engine/resources.py and models.py."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.docker_engine import EngineClient, EngineError
from tests.docker_engine.fake_engine import FakeEngine, Reply, json_reply
from tests.docker_engine.helpers import CONTAINER_ID, CONTAINER_SUMMARY, ENV_ID

SECRET_ENV = "DB_PASSWORD=hunter2"
JOIN_TOKEN = "SWMTKN-1-join-token"
SERVICE_ID = "s" + "0" * 24
NODE_ID = "n" + "0" * 24
TASK_ID = "t" + "0" * 24
IMAGE_REF = "ghcr.io/org/app:1.2@sha256:abcdef"

CONTAINER_INSPECT = {
    "Id": "c0ffee",
    "Name": "/web",
    "Created": "2026-01-01T00:00:00Z",
    "Image": "sha256:aaa",
    "State": {"Status": "running", "Running": True, "Health": {"Status": "healthy"}},
    "Config": {"Image": "nginx", "Env": [SECRET_ENV], "Cmd": None, "Labels": None},
    "HostConfig": {"Binds": [f"/srv:/data?{SECRET_ENV}"]},
    "NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.2"}}},
    "Mounts": [{"Type": "bind", "Source": "/srv", "Destination": "/data", "RW": False}],
}

type Call = Callable[[EngineClient], Awaitable[Any]]


async def test_container_list_parses_rows_and_applies_defaults_for_null_fields(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    row = {**CONTAINER_SUMMARY, "Labels": None}
    engine.route("GET", "/containers/json", json_reply([row]))

    containers = await engine_client.containers.list(all=True, filters={"status": ["running"]})

    assert containers[0].id == "c0ffee"
    assert containers[0].labels == {}
    assert containers[0].ports[0].public_port == 8080
    assert engine.calls(path="/containers/json")[0].query == {
        "all": "true",
        "filters": json.dumps({"status": ["running"]}),
    }


async def test_a_null_list_from_the_engine_is_an_empty_list(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", "/containers/json", Reply(body=b"null"))

    containers = await engine_client.containers.list()

    assert containers == []


async def test_container_inspect_keeps_environment_variables_and_host_config_out_of_repr(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", f"/containers/{CONTAINER_ID}/json", json_reply(CONTAINER_INSPECT))

    container = await engine_client.containers.inspect(CONTAINER_ID)

    assert container.state.health is not None
    assert container.state.health.status == "healthy"
    assert container.network_settings.networks["bridge"].ip_address == "172.17.0.2"
    assert container.config.env == [SECRET_ENV]
    assert "hunter2" not in repr(container)


async def _start(client: EngineClient) -> None:
    await client.containers.start(CONTAINER_ID)


async def _stop_with_grace(client: EngineClient) -> None:
    await client.containers.stop(CONTAINER_ID, grace_seconds=20)


async def _restart(client: EngineClient) -> None:
    await client.containers.restart(CONTAINER_ID)


async def _kill_with_signal(client: EngineClient) -> None:
    await client.containers.kill(CONTAINER_ID, signal="SIGHUP")


async def _remove_forced(client: EngineClient) -> None:
    await client.containers.remove(CONTAINER_ID, force=True, volumes=True)


async def _remove_image(client: EngineClient) -> None:
    await client.images.remove("alpine", force=True, noprune=True)


async def _remove_network(client: EngineClient) -> None:
    await client.networks.remove("net1")


async def _remove_volume(client: EngineClient) -> None:
    await client.volumes.remove("data", force=True)


REPLIES = {"/images/alpine": json_reply([])}

MUTATION_CASES = [
    pytest.param("POST", f"/containers/{CONTAINER_ID}/start", {}, _start, id="start"),
    pytest.param(
        "POST", f"/containers/{CONTAINER_ID}/stop", {"t": "20"}, _stop_with_grace, id="stop"
    ),
    pytest.param("POST", f"/containers/{CONTAINER_ID}/restart", {}, _restart, id="restart"),
    pytest.param(
        "POST",
        f"/containers/{CONTAINER_ID}/kill",
        {"signal": "SIGHUP"},
        _kill_with_signal,
        id="kill",
    ),
    pytest.param(
        "DELETE",
        f"/containers/{CONTAINER_ID}",
        {"force": "true", "v": "true"},
        _remove_forced,
        id="remove-container",
    ),
    pytest.param(
        "DELETE",
        "/images/alpine",
        {"force": "true", "noprune": "true"},
        _remove_image,
        id="remove-image",
    ),
    pytest.param("DELETE", "/networks/net1", {}, _remove_network, id="remove-network"),
    pytest.param("DELETE", "/volumes/data", {"force": "true"}, _remove_volume, id="remove-volume"),
]


@pytest.mark.parametrize(("method", "path", "query", "call"), MUTATION_CASES)
async def test_mutating_calls_send_the_expected_method_path_and_parameters(
    engine: FakeEngine,
    engine_client: EngineClient,
    method: str,
    path: str,
    query: dict[str, str],
    call: Call,
) -> None:
    engine.route(method, path, REPLIES.get(path, Reply(204)))

    await call(engine_client)

    sent = engine.calls(method, path)
    assert len(sent) == 1
    assert sent[0].query == query


async def test_image_calls_parse_summaries_and_accept_a_digest_reference(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    summary = {"Id": "sha256:aaa", "RepoTags": None, "Created": 1, "Size": 5}
    engine.route("GET", "/images/json", json_reply([summary]))
    engine.route(
        "GET", f"/images/{IMAGE_REF}/json", json_reply({"Id": "sha256:aaa", "Os": "linux"})
    )

    images = await engine_client.images.list()
    inspected = await engine_client.images.inspect(IMAGE_REF)

    assert images[0].repo_tags == []
    assert images[0].containers == -1
    assert inspected.os == "linux"


async def test_network_and_volume_calls_parse_their_shapes(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    network = {"Id": "n1", "Name": "bridge", "Containers": {"c0ffee": {"Name": "web"}}}
    engine.route("GET", "/networks", json_reply([network]))
    engine.route("GET", "/volumes", json_reply({"Volumes": None, "Warnings": None}))
    engine.route("GET", "/volumes/data", json_reply({"Name": "data", "Driver": "local"}))

    networks = await engine_client.networks.list()
    volumes = await engine_client.volumes.list()
    volume = await engine_client.volumes.inspect("data")

    assert networks[0].containers["c0ffee"].name == "web"
    assert volumes == []
    assert volume.driver == "local"


async def test_system_info_reports_host_counts_and_swarm_membership(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    info = {
        "ID": "host1",
        "ServerVersion": "29.2.1",
        "NCPU": 4,
        "Containers": 3,
        "Swarm": {"NodeID": "node1", "LocalNodeState": "active", "ControlAvailable": True},
    }
    engine.route("GET", "/info", json_reply(info))

    system = await engine_client.system.info()

    assert (system.ncpu, system.containers) == (4, 3)
    assert system.swarm.control_available


async def test_swarm_calls_parse_nodes_services_and_tasks_and_never_model_join_tokens(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    cluster = {"ID": "cluster1", "JoinTokens": {"Worker": JOIN_TOKEN, "Manager": JOIN_TOKEN}}
    service = {
        "ID": SERVICE_ID,
        "Version": {"Index": 4},
        "Spec": {"Name": "web"},
        "ServiceStatus": {"RunningTasks": 2, "DesiredTasks": 3},
    }
    node = {"ID": NODE_ID, "Version": {"Index": 5}, "ManagerStatus": {"Leader": True}}
    task = {
        "ID": TASK_ID,
        "Version": {"Index": 6},
        "ServiceID": SERVICE_ID,
        "Slot": None,
        "Status": {"State": "running"},
    }
    engine.route("GET", "/swarm", json_reply(cluster))
    engine.route("GET", "/nodes", json_reply([node]))
    engine.route("GET", "/services", json_reply([service]))
    engine.route("GET", "/tasks", json_reply([task]))

    inspected = await engine_client.swarm.inspect()
    nodes = await engine_client.swarm_nodes.list()
    services = await engine_client.swarm_services.list()
    tasks = await engine_client.swarm_tasks.list(filters={"service": [SERVICE_ID]})

    assert JOIN_TOKEN not in inspected.model_dump_json()
    assert nodes[0].manager_status is not None
    assert services[0].service_status is not None
    assert services[0].service_status.running_tasks == 2
    assert tasks[0].status.state == "running"
    assert engine.calls(path="/services")[0].query == {"status": "true"}


@pytest.mark.parametrize(
    "identifier", ["", "../etc", "a/b", "a b", "-leading-hyphen", "a?x=1", "web\n"]
)
async def test_an_unsafe_identifier_is_rejected_before_any_request_is_sent(
    engine: FakeEngine, engine_client: EngineClient, identifier: str
) -> None:
    with pytest.raises(EngineError) as raised:
        await engine_client.containers.inspect(identifier)

    assert raised.value.environment_id == ENV_ID
    assert engine.requests == []


@pytest.mark.parametrize("reference", ["a/../b", "img name", "img?x=1", "../img"])
async def test_an_unsafe_image_reference_is_rejected_before_any_request_is_sent(
    engine: FakeEngine, engine_client: EngineClient, reference: str
) -> None:
    with pytest.raises(EngineError):
        await engine_client.images.inspect(reference)

    assert engine.requests == []
