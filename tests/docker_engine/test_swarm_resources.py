"""The Swarm Engine surface: paths, parameters, headers, bounds, ids, allow-lists and secrecy."""

import base64
import json
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from pydantic import SecretStr

import app.docker_engine.resources as resources
from app.docker_engine import EngineClient, EngineError, NotFound, ResponseTooLarge
from app.docker_engine.engine_http import EngineHttp
from app.docker_engine.errors import EnvironmentUnreachable
from app.docker_engine.models import SystemInfo
from tests.docker_engine.fake_engine import (
    FakeEngine,
    Refuse,
    Reply,
    Stall,
    Stream,
    json_reply,
)
from tests.docker_engine.helpers import log_frame
from tests.swarm_support import SAMPLES, SENTINEL, sample_bytes, sample_json

JSON = {"Content-Type": "application/json"}
MULTIPLEXED = {"Content-Type": "application/vnd.docker.multiplexed-stream"}
RAW = {"Content-Type": "application/vnd.docker.raw-stream"}
SERVICE_ID = "czyuoulk8tyhjx4z8q9c99d24"
NODE_ID = "t55prpsoltethywit8ykgjtgi"
CONFIG_ID = "4co44l45t1zgtt67q9mbk7w9k"
SECRET_ID = "sy2rb24rrhowyljgohbhv1c6p"
AUTH = "c2VjcmV0LXJlZ2lzdHJ5LWF1dGgtc2VudGluZWw="
SERVICE_SPEC = {"Name": "web", "TaskTemplate": {"ContainerSpec": {"Image": "x"}}}
MALFORMED_IDS = ["", "abc", "A" * 25, "a" * 24, "a" * 26, "0123456789abcdef" * 4, "a-b" + "c" * 22]
NAMESPACE = "com.docker.stack.namespace"


def served(engine: FakeEngine, method: str, path: str, sample: str, status: int = 200) -> None:
    engine.route(method, path, Reply(status, sample_bytes(sample), JSON))


def failure_text(error: BaseException) -> str:
    pieces: list[str] = []
    current: BaseException | None = error
    while current is not None:
        pieces.append(f"{current}|{current!r}|{current.args!r}")
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return "\n".join(pieces)


async def test_a_service_list_with_status_parses_counts_labels_ports_and_addresses(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    served(engine, "GET", "/services", "05-GET-services-list-status.bin")

    [first, *_] = await engine_client.swarm_services.list()

    assert len(first.id) == 25 and first.version.index > 0
    assert first.service_status is not None and first.service_status.desired_tasks == 2
    assert first.spec.labels[NAMESPACE] == "S17Manual"
    assert "Env" not in repr(first)
    assert engine.calls(path="/services")[0].query == {"status": "true"}


async def test_a_rolled_back_service_parses_and_its_document_keeps_every_field(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    served(engine, "GET", f"/services/{SERVICE_ID}", "10-GET-A-after-rollback.bin")

    service = await engine_client.swarm_services.inspect(SERVICE_ID)
    document = await engine_client.swarm_services.document(SERVICE_ID)

    assert service.update_status is not None
    assert service.update_status.state == "rollback_completed"
    assert document.id == service.id and document.version == service.version.index
    assert document.previous_spec is not None
    assert document.spec["TaskTemplate"]["ContainerSpec"]["Healthcheck"]["Test"] == [
        "CMD-SHELL",
        "true",
    ]
    assert "TaskTemplate" not in repr(document)


async def test_tasks_parse_with_their_slots_and_states(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    served(engine, "GET", "/tasks", "09-GET-tasks-for-A-with-history.bin")

    tasks = await engine_client.swarm_tasks.list()

    assert len(tasks) > 1
    assert all(len(task.id) == 25 for task in tasks)
    assert [(task.slot, task.status.state) for task in tasks] == [
        (1, "running"),
        (1, "pending"),
        (2, "running"),
    ]


async def test_nodes_parse_with_description_resources_and_manager_status(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    served(engine, "GET", "/nodes", "02-GET-nodes.bin")

    [node, *_] = await engine_client.swarm_nodes.list()

    assert node.description.hostname and node.description.engine_version == "27.5.1"
    assert node.description.nano_cpus == 8_000_000_000 and node.description.memory_bytes > 0


async def test_a_node_document_carries_only_the_spec_fields_an_update_may_send(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    node = sample_json("02-GET-nodes.bin")[0]
    engine.route("GET", f"/nodes/{node['ID']}", json_reply(node))

    document = await engine_client.swarm_nodes.document(node["ID"])

    assert document.id == node["ID"] and document.version == node["Version"]["Index"]
    assert set(document.spec) <= {"Name", "Labels", "Role", "Availability"}


async def test_a_config_lists_without_content_and_inspects_with_it_decoded(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    served(engine, "GET", "/configs", "03-GET-configs-list.bin")
    served(engine, "GET", f"/configs/{CONFIG_ID}", "03-GET-config-inspect.bin")

    [listed] = await engine_client.swarm_configs.list()
    document = await engine_client.swarm_configs.inspect(CONFIG_ID)

    assert (listed.name, listed.labels) == ("app-config", {"kind": "cfg"})
    assert "Data" not in listed.model_dump(by_alias=True) and "data" not in listed.model_dump()
    assert document.data == b"config-content-line\n" and document.config.id == CONFIG_ID
    assert "config-content-line" not in repr(document)


async def test_a_secret_model_has_no_field_that_could_hold_a_value(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    served(engine, "GET", "/secrets", "03-GET-secrets-list.bin")
    served(engine, "GET", f"/secrets/{SECRET_ID}", "03-GET-secret-inspect.bin")

    [listed] = await engine_client.swarm_secrets.list()
    inspected = await engine_client.swarm_secrets.inspect(SECRET_ID)

    assert listed.name == "app-secret" and inspected.id == SECRET_ID
    assert not any("data" in name.lower() for name in type(listed).model_fields)


async def test_the_cluster_reply_never_models_the_join_tokens(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    served(engine, "GET", "/swarm", "02-GET-swarm.bin")

    cluster = await engine_client.swarm.inspect()

    assert "SWMTKN" in sample_bytes("02-GET-swarm.bin").decode()
    assert "SWMTKN" not in cluster.model_dump_json() + repr(cluster)


@pytest.mark.parametrize(
    ("sample", "state", "control", "nodes"),
    [
        ("02-info-swarm-block.json", "active", True, 2),
        ("22-worker-info-swarm-block.json", "active", False, None),
        ("01-nonswarm-info-swarm-block.json", "inactive", False, None),
    ],
)
def test_the_swarm_block_of_info_parses_for_a_manager_a_worker_and_a_standalone_host(
    sample: str, state: str, control: bool, nodes: int | None
) -> None:
    block = json.loads((SAMPLES / sample).read_text())

    swarm = SystemInfo.model_validate(block).swarm

    assert (swarm.local_node_state, swarm.control_available, swarm.nodes) == (state, control, nodes)


def test_nonsense_in_the_swarm_block_of_info_is_ignored_not_raised() -> None:
    junk = SystemInfo.model_validate({"Swarm": {"Nodes": "x", "Managers": -3, "Cluster": 5}}).swarm

    assert junk.nodes is None and junk.managers is None and junk.cluster_id == ""


async def test_stack_namespaces_are_the_case_folded_namespace_labels_asked_by_key_alone(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    served(engine, "GET", "/services", "18-GET-services-label-key-filter.bin")

    found = await engine_client.swarm.stack_namespaces()

    assert found == frozenset({"s17stack", "s17manual"})
    assert json.loads(engine.calls("GET", "/services")[0].query["filters"]) == {
        "label": [NAMESPACE]
    }


async def test_a_distribution_lookup_returns_the_descriptor_digest(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route(
        "GET",
        "/distribution/busybox/json",
        json_reply({"Descriptor": {"digest": "sha256:" + "a" * 64}}),
    )

    descriptor = await engine_client.distribution.inspect("busybox", registry_auth=None)

    assert descriptor.digest == "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "digest", ["sha256:abc", "md5:" + "a" * 64, "sha256:" + "A" * 64, "sha256:" + "a" * 65]
)
async def test_a_malformed_digest_in_a_distribution_reply_is_an_engine_error(
    engine: FakeEngine, engine_client: EngineClient, digest: str
) -> None:
    engine.route(
        "GET", "/distribution/busybox/json", json_reply({"Descriptor": {"digest": digest}})
    )

    with pytest.raises(EngineError):
        await engine_client.distribution.inspect("busybox", registry_auth=None)


async def test_a_distribution_reply_without_a_descriptor_is_an_engine_error(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", "/distribution/busybox/json", json_reply({"Platforms": []}))

    with pytest.raises(EngineError):
        await engine_client.distribution.inspect("busybox", registry_auth=None)


@pytest.mark.parametrize(
    ("path", "call"),
    [
        ("/services", lambda c: c.swarm_services.list()),
        ("/nodes", lambda c: c.swarm_nodes.list()),
        ("/tasks", lambda c: c.swarm_tasks.list()),
        ("/configs", lambda c: c.swarm_configs.list()),
        ("/secrets", lambda c: c.swarm_secrets.list()),
    ],
)
async def test_a_list_entry_whose_id_is_not_canonical_is_dropped(
    engine: FakeEngine, engine_client: EngineClient, path: str, call: Callable[[EngineClient], Any]
) -> None:
    good = {"ID": "a" * 25, "Version": {"Index": 1}, "Spec": {"Name": "ok"}}
    evil = {"ID": "evil/../../users", "Version": {"Index": 1}, "Spec": {"Name": "bad"}}
    engine.route("GET", path, json_reply([good, evil]))

    found = await call(engine_client)

    assert [item.id for item in found] == ["a" * 25]


async def test_a_null_config_or_secret_list_is_empty(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", "/configs", Reply(200, b"null", JSON))
    engine.route("GET", "/secrets", Reply(200, b"null", JSON))

    assert await engine_client.swarm_configs.list() == []
    assert await engine_client.swarm_secrets.list() == []


async def test_service_list_filters_travel_as_a_json_parameter_beside_status(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", "/services", Reply(200, b"[]", JSON))

    await engine_client.swarm_services.list(filters={"label": ["a=b"]})

    query = engine.calls("GET", "/services")[-1].query
    assert query["status"] == "true" and json.loads(query["filters"]) == {"label": ["a=b"]}


@pytest.fixture
def two_tasks() -> list[dict[str, Any]]:
    tasks = sample_json("09-GET-tasks-for-A-with-history.bin")
    other = dict(tasks[0], ID="o" * 25, ServiceID="p" * 25, NodeID="q" * 25)
    return [*tasks[:1], other]


async def test_a_task_service_filter_keeps_only_tasks_of_exactly_that_service(
    engine: FakeEngine, engine_client: EngineClient, two_tasks: list[dict[str, Any]]
) -> None:
    engine.route("GET", "/tasks", json_reply(two_tasks))
    wanted = two_tasks[0]["ServiceID"]

    found = await engine_client.swarm_tasks.list(filters={"service": [wanted]})

    assert [t.service_id for t in found] == [wanted]
    assert json.loads(engine.calls("GET", "/tasks")[0].query["filters"]) == {"service": [wanted]}


async def test_a_task_node_filter_keeps_only_tasks_on_exactly_that_node(
    engine: FakeEngine, engine_client: EngineClient, two_tasks: list[dict[str, Any]]
) -> None:
    engine.route("GET", "/tasks", json_reply(two_tasks))

    found = await engine_client.swarm_tasks.list(filters={"node": ["q" * 25]})

    assert [t.node_id for t in found] == ["q" * 25]


@pytest.mark.parametrize(
    "bad",
    [
        {"service": []},
        {"node": []},
        {"service": ["web"]},
        {"service": [SERVICE_ID[:8]]},
        {"node": ["hostname"]},
        {"service": [SERVICE_ID, "x"]},
    ],
)
async def test_a_task_filter_that_is_not_a_set_of_canonical_ids_is_refused_unsent(
    engine: FakeEngine, engine_client: EngineClient, bad: dict[str, list[str]]
) -> None:
    with pytest.raises(EngineError):
        await engine_client.swarm_tasks.list(filters=bad)

    assert engine.requests == []


async def test_a_task_list_for_a_removed_service_is_not_found(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", "/tasks", Reply(404, b'{"message":"service gone"}', JSON))

    with pytest.raises(NotFound):
        await engine_client.swarm_tasks.list(filters={"service": [SERVICE_ID]})


@pytest.mark.parametrize("status", [400, 500, 503])
async def test_a_failed_write_is_a_plain_engine_error_with_its_status_never_an_outage(
    engine: FakeEngine, engine_client: EngineClient, status: int
) -> None:
    engine.route(
        "POST",
        f"/services/{SERVICE_ID}/update",
        Reply(status, b'{"message":"rpc error: update out of sequence"}', JSON),
    )

    with pytest.raises(EngineError) as raised:
        await engine_client.swarm_services.update(SERVICE_ID, SERVICE_SPEC, version=1)

    assert type(raised.value) is EngineError and raised.value.status_code == status
    assert not isinstance(raised.value, EnvironmentUnreachable)
    assert "out of sequence" not in str(raised.value)


async def test_a_swarm_read_on_a_non_manager_is_a_plain_503_engine_error(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", "/services", Reply(503, b'{"message":"not a swarm manager"}', JSON))

    with pytest.raises(EngineError) as raised:
        await engine_client.swarm_services.list()

    assert type(raised.value) is EngineError and raised.value.status_code == 503


async def test_engine_text_naming_a_service_never_reaches_the_exception_string(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route(
        "DELETE", f"/configs/{CONFIG_ID}", Reply(400, b'{"message":"in use by app-a"}', JSON)
    )

    with pytest.raises(EngineError) as raised:
        await engine_client.swarm_configs.remove(CONFIG_ID)

    assert raised.value.status_code == 400 and "app-a" not in str(raised.value)


CAPPED: list[tuple[str, str, str, Callable[[EngineClient], Awaitable[Any]]]] = [
    ("GET", "/swarm", "SWARM_INSPECT_BYTES", lambda c: c.swarm.inspect()),
    ("GET", "/services", "STACK_NAMESPACES_BYTES", lambda c: c.swarm.stack_namespaces()),
    ("GET", "/services", "SERVICE_LIST_BYTES", lambda c: c.swarm_services.list()),
    (
        "GET",
        f"/services/{SERVICE_ID}",
        "SERVICE_INSPECT_BYTES",
        lambda c: c.swarm_services.inspect(SERVICE_ID),
    ),
    (
        "GET",
        f"/services/{SERVICE_ID}",
        "SERVICE_INSPECT_BYTES",
        lambda c: c.swarm_services.document(SERVICE_ID),
    ),
    ("GET", "/tasks", "TASK_LIST_BYTES", lambda c: c.swarm_tasks.list()),
    ("GET", "/tasks/abc", "TASK_INSPECT_BYTES", lambda c: c.swarm_tasks.inspect("abc")),
    ("GET", "/nodes", "NODE_LIST_BYTES", lambda c: c.swarm_nodes.list()),
    ("GET", f"/nodes/{NODE_ID}", "NODE_INSPECT_BYTES", lambda c: c.swarm_nodes.inspect(NODE_ID)),
    ("GET", f"/nodes/{NODE_ID}", "NODE_INSPECT_BYTES", lambda c: c.swarm_nodes.document(NODE_ID)),
    ("GET", "/configs", "CONFIG_LIST_BYTES", lambda c: c.swarm_configs.list()),
    (
        "GET",
        f"/configs/{CONFIG_ID}",
        "CONFIG_INSPECT_BYTES",
        lambda c: c.swarm_configs.inspect(CONFIG_ID),
    ),
    ("GET", "/secrets", "SECRET_LIST_BYTES", lambda c: c.swarm_secrets.list()),
    (
        "GET",
        f"/secrets/{SECRET_ID}",
        "SECRET_INSPECT_BYTES",
        lambda c: c.swarm_secrets.inspect(SECRET_ID),
    ),
    (
        "GET",
        "/distribution/a/json",
        "DISTRIBUTION_BYTES",
        lambda c: c.distribution.inspect("a", registry_auth=None),
    ),
    (
        "POST",
        "/services/create",
        "SWARM_WRITE_REPLY_BYTES",
        lambda c: c.swarm_services.create(SERVICE_SPEC, registry_auth=None),
    ),
    (
        "POST",
        f"/services/{SERVICE_ID}/update",
        "SWARM_WRITE_REPLY_BYTES",
        lambda c: c.swarm_services.update(SERVICE_ID, SERVICE_SPEC, version=1),
    ),
    (
        "DELETE",
        f"/services/{SERVICE_ID}",
        "SWARM_WRITE_REPLY_BYTES",
        lambda c: c.swarm_services.remove(SERVICE_ID),
    ),
    (
        "POST",
        f"/nodes/{NODE_ID}/update",
        "SWARM_WRITE_REPLY_BYTES",
        lambda c: c.swarm_nodes.update(NODE_ID, {"Role": "worker"}, version=1),
    ),
    (
        "POST",
        "/configs/create",
        "SWARM_WRITE_REPLY_BYTES",
        lambda c: c.swarm_configs.create(name="a", data=b"x", labels=None),
    ),
    (
        "POST",
        "/secrets/create",
        "SWARM_WRITE_REPLY_BYTES",
        lambda c: c.swarm_secrets.create(name="a", data=b"x", labels=None),
    ),
    (
        "DELETE",
        f"/configs/{CONFIG_ID}",
        "SWARM_WRITE_REPLY_BYTES",
        lambda c: c.swarm_configs.remove(CONFIG_ID),
    ),
    (
        "DELETE",
        f"/secrets/{SECRET_ID}",
        "SWARM_WRITE_REPLY_BYTES",
        lambda c: c.swarm_secrets.remove(SECRET_ID),
    ),
]


@pytest.mark.parametrize(
    ("method", "path", "constant", "call"),
    [pytest.param(m, p, c, f, id=f"{m} {p} {c}") for m, p, c, f in CAPPED],
)
async def test_every_swarm_call_refuses_a_reply_over_its_cap(
    engine: FakeEngine,
    engine_client: EngineClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    constant: str,
    call: Callable[[EngineClient], Awaitable[Any]],
) -> None:
    monkeypatch.setattr(resources, constant, 64)
    engine.route(method, path, Reply(200, b'{"pad":"' + b"x" * 200 + b'"}', JSON))

    with pytest.raises(ResponseTooLarge):
        await call(engine_client)


@pytest.mark.parametrize(
    ("constant", "size"),
    [
        ("SWARM_INSPECT_BYTES", 1 << 20),
        ("SERVICE_LIST_BYTES", 16 << 20),
        ("SERVICE_INSPECT_BYTES", 4 << 20),
        ("TASK_LIST_BYTES", 16 << 20),
        ("TASK_INSPECT_BYTES", 1 << 20),
        ("NODE_LIST_BYTES", 4 << 20),
        ("NODE_INSPECT_BYTES", 1 << 20),
        ("CONFIG_LIST_BYTES", 32 << 20),
        ("CONFIG_INSPECT_BYTES", 4 << 20),
        ("SECRET_LIST_BYTES", 16 << 20),
        ("SECRET_INSPECT_BYTES", 1 << 20),
        ("STACK_NAMESPACES_BYTES", 4 << 20),
        ("DISTRIBUTION_BYTES", 1 << 20),
        ("SWARM_WRITE_REPLY_BYTES", 64 << 10),
    ],
)
def test_the_caps_are_the_documented_sizes(constant: str, size: int) -> None:
    assert getattr(resources, constant) == size


OFF_LOOP: list[tuple[str, str, str, str, Callable[[EngineClient], Awaitable[Any]]]] = [
    (
        "GET",
        "/services",
        "05-GET-services-list-status.bin",
        "_decode_models",
        lambda c: c.swarm_services.list(),
    ),
    (
        "GET",
        f"/services/{SERVICE_ID}",
        "10-GET-A-after-rollback.bin",
        "_decode_model",
        lambda c: c.swarm_services.inspect(SERVICE_ID),
    ),
    (
        "GET",
        f"/services/{SERVICE_ID}",
        "10-GET-A-after-rollback.bin",
        "_decode_service_document",
        lambda c: c.swarm_services.document(SERVICE_ID),
    ),
    (
        "GET",
        "/tasks",
        "09-GET-tasks-for-A-with-history.bin",
        "_decode_models",
        lambda c: c.swarm_tasks.list(),
    ),
    (
        "GET",
        f"/configs/{CONFIG_ID}",
        "03-GET-config-inspect.bin",
        "_decode_config",
        lambda c: c.swarm_configs.inspect(CONFIG_ID),
    ),
    (
        "POST",
        "/configs/create",
        "",
        "_object_body",
        lambda c: c.swarm_configs.create(name="c", data=b"d", labels=None),
    ),
    (
        "POST",
        f"/services/{SERVICE_ID}/update",
        "",
        "_encode_json",
        lambda c: c.swarm_services.update(SERVICE_ID, SERVICE_SPEC, version=3),
    ),
]


@pytest.mark.parametrize(
    ("method", "path", "sample", "decoder", "call"),
    [pytest.param(*row, id=row[3] + "-" + row[1]) for row in OFF_LOOP],
)
async def test_decoding_and_encoding_swarm_bodies_run_off_the_event_loop(
    engine: FakeEngine,
    engine_client: EngineClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    sample: str,
    decoder: str,
    call: Callable[[EngineClient], Awaitable[Any]],
) -> None:
    seen: list[int] = []
    owner = EngineHttp if decoder in ("_decode_model", "_decode_models") else resources
    real = getattr(owner, decoder)

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, decoder, spy)
    reply = Reply(200, sample_bytes(sample), JSON) if sample else json_reply({"ID": CONFIG_ID}, 201)
    engine.route(method, path, reply)

    await call(engine_client)

    assert seen and threading.get_ident() not in seen


CREATES: list[tuple[str, str, Callable[[EngineClient], Awaitable[Any]]]] = [
    (
        "service",
        "/services/create",
        lambda c: c.swarm_services.create(SERVICE_SPEC, registry_auth=None),
    ),
    (
        "config",
        "/configs/create",
        lambda c: c.swarm_configs.create(name="a", data=b"x", labels=None),
    ),
    (
        "secret",
        "/secrets/create",
        lambda c: c.swarm_secrets.create(name="a", data=b"x", labels=None),
    ),
]


@pytest.mark.parametrize("bad", MALFORMED_IDS)
@pytest.mark.parametrize(("path", "call"), [pytest.param(p, c, id=k) for k, p, c in CREATES])
async def test_a_create_reply_with_a_malformed_id_is_an_engine_error(
    engine: FakeEngine,
    engine_client: EngineClient,
    path: str,
    call: Callable[[EngineClient], Awaitable[Any]],
    bad: str,
) -> None:
    engine.route("POST", path, json_reply({"ID": bad}, 201))

    with pytest.raises(EngineError) as raised:
        await call(engine_client)

    assert "malformed id" in raised.value.reason


@pytest.mark.parametrize("body", [b"[]", b"not json", b"{}", b'{"Id": "' + b"a" * 25 + b'"}'])
@pytest.mark.parametrize(("path", "call"), [pytest.param(p, c, id=k) for k, p, c in CREATES])
async def test_a_create_reply_that_is_not_an_object_with_an_id_is_an_engine_error(
    engine: FakeEngine,
    engine_client: EngineClient,
    path: str,
    call: Callable[[EngineClient], Awaitable[Any]],
    body: bytes,
) -> None:
    engine.route("POST", path, Reply(201, body, JSON))

    with pytest.raises(EngineError):
        await call(engine_client)


async def test_a_create_reply_with_warnings_still_returns_the_id(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route(
        "POST", "/services/create", json_reply({"ID": SERVICE_ID, "Warnings": ["w1"]}, 201)
    )

    created = await engine_client.swarm_services.create(SERVICE_SPEC, registry_auth=None)

    assert created == SERVICE_ID


GOOD = {
    "Name": "web",
    "Labels": {},
    "TaskTemplate": {},
    "Mode": {},
    "UpdateConfig": {},
    "RollbackConfig": {},
    "EndpointSpec": {},
}

REFUSED_UNSENT: list[tuple[str, Callable[[EngineClient], Awaitable[Any]]]] = [
    (
        "legacy networks key",
        lambda c: c.swarm_services.create({**GOOD, "Networks": []}, registry_auth=None),
    ),
    (
        "previous spec key",
        lambda c: c.swarm_services.create({**GOOD, "PreviousSpec": {}}, registry_auth=None),
    ),
    (
        "lower-case key",
        lambda c: c.swarm_services.create({**GOOD, "name": "x"}, registry_auth=None),
    ),
    (
        "version key on update",
        lambda c: c.swarm_services.update(SERVICE_ID, {**GOOD, "Version": {}}, version=1),
    ),
    (
        "spec that is not a mapping",
        lambda c: c.swarm_services.update(SERVICE_ID, [], version=1),  # type: ignore[arg-type]
    ),
    (
        "node description key",
        lambda c: c.swarm_nodes.update(NODE_ID, {"Role": "worker", "Description": {}}, version=1),
    ),
    (
        "node spec key",
        lambda c: c.swarm_nodes.update(NODE_ID, {"Availability": "drain", "Spec": {}}, version=1),
    ),
    (
        "service name with a space",
        lambda c: c.swarm_services.create({"Name": "bad name"}, registry_auth=None),
    ),
    (
        "service name with a leading hyphen",
        lambda c: c.swarm_services.create({"Name": "-a"}, registry_auth=None),
    ),
    (
        "service name of 64 characters",
        lambda c: c.swarm_services.create({"Name": "a" * 64}, registry_auth=None),
    ),
    (
        "service without a name",
        lambda c: c.swarm_services.create({"TaskTemplate": {}}, registry_auth=None),
    ),
    (
        "config name with a space",
        lambda c: c.swarm_configs.create(name="a b", data=b"x", labels=None),
    ),
    (
        "config name of 65 characters",
        lambda c: c.swarm_configs.create(name="a" * 65, data=b"x", labels=None),
    ),
    ("empty config", lambda c: c.swarm_configs.create(name="a", data=b"", labels=None)),
    (
        "config of 1,024,000 bytes",
        lambda c: c.swarm_configs.create(name="a", data=b"x" * 1_024_000, labels=None),
    ),
    (
        "config content as text",
        lambda c: c.swarm_configs.create(name="a", data="text", labels=None),  # type: ignore[arg-type]
    ),
    (
        "secret of 512,000 bytes",
        lambda c: c.swarm_secrets.create(name="a", data=b"x" * 512_000, labels=None),
    ),
    (
        "secret label that is not text",
        lambda c: c.swarm_secrets.create(name="a", data=b"x", labels={"k": 1}),  # type: ignore[dict-item]
    ),
    (
        "secret name with a leading period",
        lambda c: c.swarm_secrets.create(name=".a", data=b"x", labels=None),
    ),
]


@pytest.mark.parametrize("call", [pytest.param(call, id=label) for label, call in REFUSED_UNSENT])
async def test_a_body_outside_the_allow_lists_names_or_sizes_is_refused_without_a_request(
    engine: FakeEngine, engine_client: EngineClient, call: Callable[[EngineClient], Awaitable[Any]]
) -> None:
    with pytest.raises(EngineError):
        await call(engine_client)

    assert engine.requests == []


async def test_the_largest_config_and_secret_the_engine_takes_are_sent(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", "/configs/create", json_reply({"ID": CONFIG_ID}, 201))
    engine.route("POST", "/secrets/create", json_reply({"ID": SECRET_ID}, 201))

    await engine_client.swarm_configs.create(name="a", data=b"x" * 1_023_999, labels=None)
    await engine_client.swarm_secrets.create(name="a", data=b"x" * 511_999, labels=None)

    assert len(engine.calls("POST")) == 2


@pytest.mark.parametrize(
    "bad", ["web", "A" * 25, "czyuoulk8", "../x", SERVICE_ID.upper(), SERVICE_ID + "a"]
)
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c, b: c.swarm_services.update(b, GOOD, version=1), id="service update"),
        pytest.param(lambda c, b: c.swarm_services.remove(b), id="service remove"),
        pytest.param(
            lambda c, b: c.swarm_nodes.update(b, {"Role": "worker"}, version=1), id="node update"
        ),
        pytest.param(lambda c, b: c.swarm_configs.remove(b), id="config remove"),
        pytest.param(lambda c, b: c.swarm_secrets.remove(b), id="secret remove"),
    ],
)
async def test_a_write_by_anything_but_a_canonical_id_is_refused_without_a_request(
    engine: FakeEngine,
    engine_client: EngineClient,
    call: Callable[[EngineClient, str], Awaitable[Any]],
    bad: str,
) -> None:
    with pytest.raises(EngineError):
        await call(engine_client, bad)

    assert engine.requests == []


@pytest.mark.parametrize("version", [-1, True, 2**63, "1", 1.5])
async def test_an_update_version_outside_zero_to_2_63_is_refused_without_a_request(
    engine: FakeEngine, engine_client: EngineClient, version: Any
) -> None:
    with pytest.raises(EngineError):
        await engine_client.swarm_services.update(SERVICE_ID, GOOD, version=version)

    assert engine.requests == []


UNSAFE_READS: list[Callable[[EngineClient], Any]] = [
    lambda c: c.swarm_services.inspect("../x"),
    lambda c: c.swarm_services.document("a/b"),
    lambda c: c.swarm_services.logs("a b", tty=False).__anext__(),
    lambda c: c.swarm_tasks.inspect("a?x=1"),
    lambda c: c.swarm_nodes.inspect(""),
    lambda c: c.swarm_configs.inspect("x\n"),
    lambda c: c.swarm_secrets.inspect("-a"),
    lambda c: c.distribution.inspect("a/../b", registry_auth=None),
]


@pytest.mark.parametrize("call", UNSAFE_READS, ids=range(len(UNSAFE_READS)))
async def test_a_read_by_an_unsafe_path_segment_is_refused_without_a_request(
    engine: FakeEngine, engine_client: EngineClient, call: Callable[[EngineClient], Any]
) -> None:
    with pytest.raises(EngineError):
        await call(engine_client)

    assert engine.requests == []


async def test_a_service_create_sends_the_registry_header_only_when_given(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", "/services/create", json_reply({"ID": SERVICE_ID}, 201))

    await engine_client.swarm_services.create(SERVICE_SPEC, registry_auth=None)
    await engine_client.swarm_services.create(SERVICE_SPEC, registry_auth=SecretStr("eyJ1IjoiYSJ9"))

    anonymous, authed = engine.calls("POST", "/services/create")
    assert "x-registry-auth" not in anonymous.headers
    assert authed.headers["x-registry-auth"] == "eyJ1IjoiYSJ9"
    assert json.loads(anonymous.body) == SERVICE_SPEC
    assert anonymous.headers["content-type"] == "application/json"


async def test_a_registry_header_that_is_not_base64_is_refused_unsent_and_never_quoted(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    bad = "zz yy!"

    with pytest.raises(EngineError) as raised:
        await engine_client.swarm_services.create(SERVICE_SPEC, registry_auth=SecretStr(bad))

    assert "not base64" in raised.value.reason and bad not in failure_text(raised.value)
    assert engine.requests == []


async def test_an_update_names_its_credential_source_or_sends_the_empty_document(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", f"/services/{SERVICE_ID}/update", Reply(200, b'{"Warnings":null}', JSON))
    client = engine_client.swarm_services

    await client.update(SERVICE_ID, SERVICE_SPEC, version=21)
    await client.update(SERVICE_ID, SERVICE_SPEC, version=22, registry_auth=SecretStr("e30="))
    await client.update(SERVICE_ID, SERVICE_SPEC, version=23, registry_auth_from="spec")
    await client.update(
        SERVICE_ID, SERVICE_SPEC, version=24, rollback=True, registry_auth_from="previous-spec"
    )

    bare, authed, from_spec, rollback = engine.calls("POST", f"/services/{SERVICE_ID}/update")
    assert bare.query == {"version": "21"} and bare.headers["x-registry-auth"] == "e30="
    assert authed.query == {"version": "22"} and authed.headers["x-registry-auth"] == "e30="
    assert from_spec.query == {"version": "23", "registryAuthFrom": "spec"}
    assert "x-registry-auth" not in from_spec.headers
    assert rollback.query == {
        "version": "24",
        "rollback": "previous",
        "registryAuthFrom": "previous-spec",
    }
    assert json.loads(rollback.body) == SERVICE_SPEC


@pytest.mark.parametrize(
    "extra",
    [
        {"registry_auth": SecretStr("e30="), "registry_auth_from": "spec"},
        {"registry_auth_from": "other"},
    ],
)
async def test_an_ambiguous_or_unknown_credential_source_is_refused_without_a_request(
    engine: FakeEngine, engine_client: EngineClient, extra: dict[str, Any]
) -> None:
    with pytest.raises(EngineError):
        await engine_client.swarm_services.update(SERVICE_ID, SERVICE_SPEC, version=1, **extra)

    assert engine.requests == []


async def test_removals_and_a_node_update_carry_the_expected_paths_and_bodies(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("DELETE", f"/services/{SERVICE_ID}", Reply(200, b"", {}))
    engine.route("POST", f"/nodes/{NODE_ID}/update", Reply(200, b"", {}))
    engine.route("DELETE", f"/configs/{CONFIG_ID}", Reply(204, b"", {}))
    engine.route("DELETE", f"/secrets/{SECRET_ID}", Reply(204, b"", {}))
    spec = {"Labels": {}, "Role": "worker", "Availability": "drain"}

    await engine_client.swarm_services.remove(SERVICE_ID)
    await engine_client.swarm_nodes.update(NODE_ID, spec, version=15)
    await engine_client.swarm_configs.remove(CONFIG_ID)
    await engine_client.swarm_secrets.remove(SECRET_ID)

    node_call = engine.calls("POST", f"/nodes/{NODE_ID}/update")[0]
    assert node_call.query == {"version": "15"} and json.loads(node_call.body) == spec
    assert engine.paths()[-4:] == [
        f"DELETE /services/{SERVICE_ID}",
        f"POST /nodes/{NODE_ID}/update",
        f"DELETE /configs/{CONFIG_ID}",
        f"DELETE /secrets/{SECRET_ID}",
    ]


async def test_a_config_body_holds_only_name_data_and_labels_when_there_are_labels(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", "/configs/create", json_reply({"ID": CONFIG_ID}, 201))

    await engine_client.swarm_configs.create(name="cfg", data=b"hello", labels={"a": "b"})
    await engine_client.swarm_configs.create(name="cfg2", data=b"hello", labels=None)

    with_labels, without = engine.calls("POST", "/configs/create")
    assert json.loads(with_labels.body) == {
        "Name": "cfg",
        "Labels": {"a": "b"},
        "Data": base64.b64encode(b"hello").decode(),
    }
    assert set(json.loads(without.body)) == {"Name", "Data"}


async def test_a_secret_body_never_carries_a_driver_or_templating(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", "/secrets/create", json_reply({"ID": SECRET_ID}, 201))

    await engine_client.swarm_secrets.create(name="sec.1", data=b"\x00\xff", labels={})

    assert set(json.loads(engine.calls("POST", "/secrets/create")[0].body)) == {"Name", "Data"}


async def test_a_distribution_lookup_sends_the_registry_header_only_when_given(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route(
        "GET",
        "/distribution/busybox/json",
        json_reply({"Descriptor": {"digest": "sha256:" + "a" * 64}}),
    )

    await engine_client.distribution.inspect("busybox", registry_auth=None)
    await engine_client.distribution.inspect("busybox", registry_auth=SecretStr("e30="))

    anonymous, authed = engine.calls("GET", "/distribution/busybox/json")
    assert (
        "x-registry-auth" not in anonymous.headers and authed.headers["x-registry-auth"] == "e30="
    )


SECRET_VALUE = SENTINEL.encode()
FAILING_CREATES = [
    pytest.param(Reply(500, b'{"message":"boom"}', JSON), id="500"),
    pytest.param(Reply(400, b'{"message":"bad"}', JSON), id="400"),
    pytest.param(Reply(409, b'{"message":"exists"}', JSON), id="409"),
    pytest.param(Reply(201, b"not json", JSON), id="not json"),
    pytest.param(Reply(201, b'{"ID":"short"}', JSON), id="short id"),
    pytest.param(Refuse(), id="refused"),
    pytest.param(Stall(), id="stalled"),
]


@pytest.mark.parametrize("behaviour", FAILING_CREATES)
async def test_a_failed_secret_create_leaks_the_value_into_no_exception_in_any_encoding(
    engine: FakeEngine, engine_client: EngineClient, behaviour: Any
) -> None:
    engine.route("POST", "/secrets/create", behaviour)

    with pytest.raises(EngineError) as raised:
        await engine_client.swarm_secrets.create(name="s", data=SECRET_VALUE, labels={"k": "v"})

    blob = failure_text(raised.value)
    assert SENTINEL not in blob and base64.b64encode(SECRET_VALUE).decode() not in blob


async def test_the_value_did_travel_in_the_request_so_the_checks_above_have_teeth(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", "/secrets/create", json_reply({"ID": SECRET_ID}, 201))

    await engine_client.swarm_secrets.create(name="s", data=SECRET_VALUE, labels=None)

    assert base64.b64encode(SECRET_VALUE) in engine.calls("POST", "/secrets/create")[0].body


@pytest.mark.parametrize(
    "behaviour",
    [Reply(500, b'{"message":"x"}', JSON), Refuse(), Stall(), Reply(201, b"junk", JSON)],
)
async def test_a_failed_service_create_leaks_the_registry_header_nowhere(
    engine: FakeEngine, engine_client: EngineClient, behaviour: Any
) -> None:
    engine.route("POST", "/services/create", behaviour)

    with pytest.raises(EngineError) as raised:
        await engine_client.swarm_services.create(SERVICE_SPEC, registry_auth=SecretStr(AUTH))

    assert AUTH not in failure_text(raised.value)
    assert AUTH not in repr(engine_client)
    assert any(r.headers.get("x-registry-auth") == AUTH for r in engine.requests)


async def test_a_hostile_daemon_returning_secret_data_on_every_read_has_it_dropped(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    encoded = base64.b64encode(SECRET_VALUE).decode()
    hostile = {
        "ID": SECRET_ID,
        "Version": {"Index": 5},
        "Spec": {"Name": "s", "Labels": {"a": "b"}, "Data": encoded, "Driver": {"Name": "vault"}},
    }
    engine.route("GET", "/secrets", json_reply([hostile, hostile]))
    engine.route("GET", f"/secrets/{SECRET_ID}", json_reply(hostile))
    engine.route("GET", "/configs", json_reply([hostile]))

    listed = await engine_client.swarm_secrets.list()
    inspected = await engine_client.swarm_secrets.inspect(SECRET_ID)
    configs = await engine_client.swarm_configs.list()

    for item in (*listed, inspected, *configs):
        shown = item.model_dump_json() + repr(item) + str(item.model_dump())
        assert encoded not in shown
    assert inspected.has_driver


@pytest.mark.parametrize("content", ["not base64!!", 7, {"a": 1}])
async def test_a_config_whose_content_is_not_base64_is_an_engine_error(
    engine: FakeEngine, engine_client: EngineClient, content: Any
) -> None:
    body = {"ID": CONFIG_ID, "Version": {"Index": 1}, "Spec": {"Name": "c", "Data": content}}
    engine.route("GET", f"/configs/{CONFIG_ID}", json_reply(body))

    with pytest.raises(EngineError) as raised:
        await engine_client.swarm_configs.inspect(CONFIG_ID)

    assert "not base64" in raised.value.reason


async def test_a_templated_config_without_content_reads_as_empty_and_templated(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    body = {
        "ID": CONFIG_ID,
        "Version": {"Index": 1},
        "Spec": {"Name": "c", "Templating": {"Name": "golang"}},
    }
    engine.route("GET", f"/configs/{CONFIG_ID}", json_reply(body))

    document = await engine_client.swarm_configs.inspect(CONFIG_ID)

    assert document.data == b"" and document.config.templated


async def test_listed_labels_are_cut_to_a_budget_keeping_the_namespace_and_the_document_is_not(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    many = {f"k{i:04d}": "v" * 1000 for i in range(200)}
    many[NAMESPACE] = "Late"
    service = {
        "ID": SERVICE_ID,
        "Version": {"Index": 1},
        "Spec": {
            "Name": "a",
            "Labels": dict(many),
            "TaskTemplate": {"ContainerSpec": {"Image": "x", "Labels": dict(many)}},
        },
        "PreviousSpec": {"Name": "a", "Labels": dict(many)},
    }
    engine.route("GET", "/services", json_reply([service]))
    engine.route("GET", f"/services/{SERVICE_ID}", json_reply(service))

    [listed] = await engine_client.swarm_services.list()
    document = await engine_client.swarm_services.document(SERVICE_ID)

    container_labels = listed.spec.task_template["ContainerSpec"]["Labels"]
    assert listed.spec.labels_truncated and NAMESPACE in listed.spec.labels
    assert sum(len(k) + len(v) for k, v in listed.spec.labels.items()) <= 64 * 1024 + 100
    assert len(container_labels) < 200 and NAMESPACE in container_labels
    assert listed.previous_spec is not None and listed.previous_spec.labels_truncated
    assert len(document.spec["Labels"]) == 201
    assert len(document.spec["TaskTemplate"]["ContainerSpec"]["Labels"]) == 201


@pytest.mark.parametrize("path", ["/configs", "/secrets"])
async def test_config_and_secret_labels_are_budgeted_too(
    engine: FakeEngine, engine_client: EngineClient, path: str
) -> None:
    many = {f"k{i:04d}": "v" * 1000 for i in range(200)}
    item = {"ID": CONFIG_ID, "Version": {"Index": 1}, "Spec": {"Name": "c", "Labels": many}}
    engine.route("GET", path, json_reply([item]))
    client = engine_client.swarm_configs if path == "/configs" else engine_client.swarm_secrets

    [listed] = await client.list()

    assert listed.labels_truncated


LOGS = f"/services/{SERVICE_ID}/logs"


async def collect(stream: Any) -> list[Any]:
    return [frame async for frame in stream]


async def test_a_log_request_always_asks_for_timestamps_and_details(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route(
        "GET", LOGS, Reply(200, sample_bytes("07-GET-logs-A-nontty-details.bin"), MULTIPLEXED)
    )

    frames = await collect(engine_client.swarm_services.logs(SERVICE_ID, tty=False, follow=False))

    assert {f.stream for f in frames} == {"stdout", "stderr"}
    assert engine.calls("GET", LOGS)[0].query == {
        "follow": "false",
        "stdout": "true",
        "stderr": "true",
        "timestamps": "true",
        "details": "true",
    }


async def test_the_response_content_type_chooses_the_framing_over_the_callers_hint(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    multiplexed = sample_bytes("07-GET-logs-A-nontty-details.bin")
    raw = sample_bytes("07-GET-logs-B-tty-details.bin")
    engine.route("GET", LOGS, Reply(200, multiplexed, MULTIPLEXED))
    hinted_raw = await collect(engine_client.swarm_services.logs(SERVICE_ID, tty=True))
    engine.route("GET", LOGS, Reply(200, raw, RAW))
    hinted_multiplexed = await collect(engine_client.swarm_services.logs(SERVICE_ID, tty=False))

    assert {f.stream for f in hinted_raw} == {"stdout", "stderr"}
    assert b"".join(f.data for f in hinted_multiplexed) == raw


@pytest.mark.parametrize(
    ("headers", "tty"),
    [
        ({"Content-Type": "APPLICATION/VND.DOCKER.RAW-STREAM; x=y"}, False),
        ({}, True),
    ],
)
async def test_a_content_type_parameter_is_tolerated_and_no_type_falls_back_on_the_hint(
    engine: FakeEngine, engine_client: EngineClient, headers: dict[str, str], tty: bool
) -> None:
    raw = sample_bytes("07-GET-logs-B-tty-details.bin")
    engine.route("GET", LOGS, Reply(200, raw, headers))

    frames = await collect(engine_client.swarm_services.logs(SERVICE_ID, tty=tty))

    assert b"".join(f.data for f in frames) == raw


async def test_a_raw_chunk_is_split_to_the_frame_limit(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", LOGS, Reply(200, b"x" * 25, RAW))

    pieces = await collect(
        engine_client.swarm_services.logs(SERVICE_ID, tty=False, max_frame_bytes=10)
    )

    assert [len(p.data) for p in pieces] == [10, 10, 5]


async def test_a_multiplexed_frame_over_the_limit_ends_the_stream_with_an_engine_error(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", LOGS, Reply(200, log_frame(1, b"y" * 50), MULTIPLEXED))

    with pytest.raises(EngineError) as raised:
        await collect(engine_client.swarm_services.logs(SERVICE_ID, tty=False, max_frame_bytes=10))

    assert type(raised.value) is EngineError and "size limit" in raised.value.reason


async def test_a_stream_cut_mid_frame_is_unreachable_and_yields_no_fragment(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", LOGS, Reply(200, log_frame(1, b"abcdef")[:-2], MULTIPLEXED))
    got: list[Any] = []

    with pytest.raises(EnvironmentUnreachable):
        async for frame in engine_client.swarm_services.logs(SERVICE_ID, tty=False):
            got.append(frame)

    assert got == []


async def test_text_that_is_not_frame_data_under_a_multiplexed_type_is_an_engine_error(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", LOGS, Reply(200, b"2026-10-20 plain text\n", MULTIPLEXED))

    with pytest.raises(EngineError):
        await collect(engine_client.swarm_services.logs(SERVICE_ID, tty=False))


@pytest.mark.parametrize(
    ("status", "error"), [(404, NotFound), (503, EngineError), (500, EngineError)]
)
async def test_a_log_request_that_the_engine_refuses_carries_its_status(
    engine: FakeEngine, engine_client: EngineClient, status: int, error: type[EngineError]
) -> None:
    engine.route("GET", LOGS, Reply(status, b'{"message":"x"}', JSON))

    with pytest.raises(error) as raised:
        await collect(engine_client.swarm_services.logs(SERVICE_ID, tty=False))

    assert raised.value.status_code == status


async def test_frames_are_delivered_as_they_arrive_and_an_early_close_stops_the_stream(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    stream = Stream([log_frame(1, b"one\n"), log_frame(1, b"two\n"), log_frame(1, b"three\n")])
    engine.route("GET", LOGS, stream)
    logs = engine_client.swarm_services.logs(SERVICE_ID, tty=True)

    first = await anext(logs)
    await logs.aclose()

    assert first.stream == "stdout"
    assert stream.delivered < 3


async def test_since_until_and_tail_travel_as_the_engine_reads_them(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", LOGS, Reply(200, b"", MULTIPLEXED))
    since = resources.format_engine_timestamp(1792494478_825000000 + 1)

    await collect(
        engine_client.swarm_services.logs(
            SERVICE_ID, tty=False, since=since, until=1792494999, tail=200, stderr=False
        )
    )

    query = engine.calls("GET", LOGS)[-1].query
    assert query["since"] == "1792494478.825000001" and query["until"] == "1792494999"
    assert query["tail"] == "200" and query["stderr"] == "false" and query["details"] == "true"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"since": "2026-10-20T00:00:00Z"},
        {"since": -1},
        {"until": "1.1234567891"},
        {"since": "abc"},
        {"tail": -1},
        {"tail": True},
        {"max_frame_bytes": 0},
    ],
)
async def test_a_log_bound_the_engine_would_misread_is_refused_without_a_request(
    engine: FakeEngine, engine_client: EngineClient, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(EngineError):
        await collect(engine_client.swarm_services.logs(SERVICE_ID, tty=False, **kwargs))

    assert engine.requests == []
