"""The network and volume calls: bounded lists, create, connect, disconnect and their bodies."""

import json
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

import app.docker_engine.resources as resources
from app.docker_engine import EngineClient, EngineError
from app.docker_engine.engine_http import EngineHttp
from app.docker_engine.errors import Conflict, NotFound, RequestOutcomeUnknown, ResponseTooLarge
from app.docker_engine.labels import LABEL_BUDGET_CHARS
from app.docker_engine.models import ContainerSummary
from tests.access_support import container_id
from tests.docker_engine.container_engine import (
    Box,
    ContainerEngine,
    created_network_id,
    swarm_network_id,
)
from tests.docker_engine.fake_engine import FakeEngine, Reply, Stall, json_reply

COMPOSE_LABEL = "com.docker.compose.project"
SWARM_LABEL = "com.docker.stack.namespace"
HEX_ID = "a" * 64
SMALL_CAP = 100
OVER_THE_SMALL_CAP = {"Content-Length": "1" + "0" * 12}
HUGE_LABEL = "x" * (LABEL_BUDGET_CHARS + 1)
SECRET = "option-secret-7f3a"
NETWORK_DOCUMENT = {"Id": HEX_ID, "Name": "front", "Driver": "bridge", "Scope": "local"}
VOLUME_DOCUMENT = {
    "Name": "data",
    "Driver": "local",
    "Labels": {},
    "CreatedAt": "2024-05-06T07:08:09Z",
}
CONTAINER = container_id(1)

type Listing = Callable[[EngineClient], Awaitable[Any]]


def created_reply(identifier: str) -> Reply:
    return json_reply({"Id": identifier, "Warning": ""}, 201)


async def list_networks(client: EngineClient) -> Any:
    return await client.networks.list()


async def list_volumes(client: EngineClient) -> Any:
    return await client.volumes.list()


LISTS = [
    pytest.param("NETWORK_LIST_BYTES", "/networks", list_networks, id="networks"),
    pytest.param("VOLUME_LIST_BYTES", "/volumes", list_volumes, id="volumes"),
]


@pytest.mark.parametrize(("constant", "path", "call"), LISTS)
async def test_a_list_over_its_cap_raises_the_specific_error_and_returns_nothing(
    engine: FakeEngine,
    engine_client: EngineClient,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    path: str,
    call: Listing,
) -> None:
    monkeypatch.setattr(resources, constant, SMALL_CAP)
    engine.route("GET", path, Reply(200, b"[]", OVER_THE_SMALL_CAP))

    with pytest.raises(ResponseTooLarge) as raised:
        await call(engine_client)

    assert "too large" in raised.value.reason


@pytest.mark.parametrize(
    ("path", "document", "call"),
    [
        pytest.param("/networks", [NETWORK_DOCUMENT], list_networks, id="networks"),
        pytest.param("/volumes", {"Volumes": [VOLUME_DOCUMENT]}, list_volumes, id="volumes"),
    ],
)
async def test_a_list_is_decoded_in_a_worker_thread(
    engine: FakeEngine,
    engine_client: EngineClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    document: Any,
    call: Listing,
) -> None:
    engine.route("GET", path, json_reply(document))
    threads: list[int] = []
    decode = EngineHttp._decode_models

    def recording(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        return decode(*args, **kwargs)

    monkeypatch.setattr(EngineHttp, "_decode_models", recording)

    await call(engine_client)

    assert threads != [] and threading.get_ident() not in threads


async def test_oversized_network_labels_are_dropped_but_both_stack_labels_are_kept(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    labels = {"app": "web", "bulk": HUGE_LABEL, COMPOSE_LABEL: "shop", SWARM_LABEL: "swarm-shop"}
    engine.route("GET", "/networks", json_reply([{**NETWORK_DOCUMENT, "Labels": labels}]))

    (found,) = await engine_client.networks.list()

    assert found.labels == {"app": "web", COMPOSE_LABEL: "shop", SWARM_LABEL: "swarm-shop"}
    assert found.labels_truncated is True


async def test_oversized_volume_labels_are_dropped_but_the_stack_label_is_kept() -> None:
    engine = FakeEngine()
    labels = {"app": "web", "bulk": HUGE_LABEL, COMPOSE_LABEL: "shop"}
    engine.route(
        "GET", "/volumes", json_reply({"Volumes": [{**VOLUME_DOCUMENT, "Labels": labels}]})
    )
    client = EngineClient(7, engine.bundle())

    (found,) = await client.volumes.list()
    await client.aclose()

    assert found.labels == {"app": "web", COMPOSE_LABEL: "shop"}
    assert found.labels_truncated is True


async def test_a_volume_list_with_a_null_volumes_member_is_empty(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", "/volumes", json_reply({"Volumes": None, "Warnings": None}))

    assert await engine_client.volumes.list() == []


async def test_a_network_list_reads_pools_ipv6_options_and_endpoints_and_drops_junk_pools(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    document = {
        **NETWORK_DOCUMENT,
        "EnableIPv6": True,
        "IPAM": {"Config": [{"Subnet": "10.0.0.0/24", "Gateway": "10.0.0.1"}, "junk", None]},
        "Options": {"parent": "eth0"},
        "Containers": {CONTAINER: {"Name": "web", "IPv4Address": "10.0.0.2/24", "MacAddress": "m"}},
    }
    engine.route("GET", "/networks", json_reply([document]))

    (found,) = await engine_client.networks.list()

    assert found.enable_ipv6
    assert [(p.subnet, p.gateway, p.ip_range) for p in found.ipam] == [
        ("10.0.0.0/24", "10.0.0.1", "")
    ]
    assert found.options == {"parent": "eth0"}
    assert found.containers[CONTAINER].mac_address == "m"


async def test_driver_options_mountpoints_and_volume_options_never_reach_a_repr(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", "/networks", json_reply([{**NETWORK_DOCUMENT, "Options": {"k": SECRET}}]))
    engine.route(
        "GET",
        "/volumes",
        json_reply(
            {"Volumes": [{**VOLUME_DOCUMENT, "Options": {"o": SECRET}, "Mountpoint": SECRET}]}
        ),
    )

    (network,) = await engine_client.networks.list()
    (volume,) = await engine_client.volumes.list()

    assert SECRET not in repr(network) + repr(volume)


CREATE_BODY = {
    "Name": "front",
    "Driver": "bridge",
    "Internal": False,
    "EnableIPv6": False,
    "IPAM": {"Driver": "default", "Config": [{"Subnet": "10.0.0.0/24"}]},
    "Options": {"com.docker.network.driver.mtu": "1400"},
    "Labels": {"team": "core"},
}


@pytest.mark.parametrize("identifier", [HEX_ID, swarm_network_id(7), "0" * 64, "a" * 25])
async def test_network_create_returns_a_64_hex_or_25_base_36_id(
    engine: FakeEngine, engine_client: EngineClient, identifier: str
) -> None:
    engine.route("POST", "/networks/create", created_reply(identifier))

    assert await engine_client.networks.create(CREATE_BODY) == identifier


async def test_network_create_sends_the_body_as_given_in_json() -> None:
    engine = ContainerEngine()
    client = EngineClient(7, engine.bundle())

    created = await client.networks.create(CREATE_BODY)
    await client.aclose()

    assert created == created_network_id(1)
    assert engine.network_bodies("create") == [CREATE_BODY]


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        "abc",
        "A" * 64,
        "g" * 64,
        "a" * 63,
        "a" * 65,
        "z" * 24,
        "z" * 26,
        "Z" * 25,
        "../" + "a" * 61,
    ],
)
async def test_network_create_refuses_a_reply_whose_id_has_neither_shape(
    engine: FakeEngine, engine_client: EngineClient, identifier: str
) -> None:
    engine.route("POST", "/networks/create", created_reply(identifier))

    with pytest.raises(EngineError):
        await engine_client.networks.create(CREATE_BODY)


async def test_network_create_refuses_a_reply_that_is_not_json(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", "/networks/create", Reply(201, b"not json"))

    with pytest.raises(EngineError):
        await engine_client.networks.create(CREATE_BODY)


@pytest.mark.parametrize(
    "extra",
    [
        {"ConfigFrom": {"Network": "x"}},
        {"ConfigOnly": True},
        {"Ingress": True},
        {"CheckDuplicate": True},
    ],
)
async def test_network_create_refuses_a_body_key_outside_the_known_set_unsent(
    engine: FakeEngine, engine_client: EngineClient, extra: dict[str, Any]
) -> None:
    with pytest.raises(EngineError):
        await engine_client.networks.create({**CREATE_BODY, **extra})

    assert engine.requests == []


@pytest.mark.parametrize("name", ["", "a b", "a/b", "..", "-x", "a" * 256])
async def test_network_create_refuses_an_unsafe_name_unsent(
    engine: FakeEngine, engine_client: EngineClient, name: str
) -> None:
    with pytest.raises(EngineError):
        await engine_client.networks.create({**CREATE_BODY, "Name": name})

    assert engine.requests == []


async def test_network_create_reads_at_most_64_kib_of_reply(
    engine: FakeEngine, engine_client: EngineClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources, "NETWORK_WRITE_REPLY_BYTES", SMALL_CAP)
    engine.route("POST", "/networks/create", Reply(201, b"{}", OVER_THE_SMALL_CAP))

    with pytest.raises(ResponseTooLarge):
        await engine_client.networks.create(CREATE_BODY)


async def test_network_create_encodes_the_body_in_a_worker_thread(
    engine: FakeEngine, engine_client: EngineClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine.route("POST", "/networks/create", created_reply(HEX_ID))
    threads: list[int] = []
    encode = resources._encode_json

    def recording(body: Any) -> bytes:
        threads.append(threading.get_ident())
        return encode(body)

    monkeypatch.setattr(resources, "_encode_json", recording)

    await engine_client.networks.create(CREATE_BODY)

    assert threads != [] and threading.get_ident() not in threads


@pytest.mark.parametrize(
    ("status", "error"), [(409, Conflict), (404, NotFound), (400, EngineError), (500, EngineError)]
)
async def test_network_create_maps_the_engines_refusals_to_typed_errors(
    engine: FakeEngine, engine_client: EngineClient, status: int, error: type[EngineError]
) -> None:
    engine.route("POST", "/networks/create", json_reply({"message": "no"}, status))

    with pytest.raises(error):
        await engine_client.networks.create(CREATE_BODY)


async def test_a_network_create_that_times_out_is_an_unknown_outcome(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", "/networks/create", Stall())

    with pytest.raises(RequestOutcomeUnknown):
        await engine_client.networks.create(CREATE_BODY)


CONNECT_BODY = {
    "Container": CONTAINER,
    "EndpointConfig": {"Aliases": ["web"], "IPAMConfig": {"IPv4Address": "10.0.0.5"}},
}


async def test_connect_posts_the_body_to_the_networks_own_path(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", f"/networks/{HEX_ID}/connect", Reply(200))

    await engine_client.networks.connect(HEX_ID, CONNECT_BODY)

    (sent,) = engine.calls("POST", f"/networks/{HEX_ID}/connect")
    assert json.loads(sent.body) == CONNECT_BODY


async def test_connect_without_an_endpoint_configuration_sends_only_the_container(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", f"/networks/{swarm_network_id(3)}/connect", Reply(200))

    await engine_client.networks.connect(swarm_network_id(3), {"Container": CONTAINER})

    (sent,) = engine.calls("POST")
    assert json.loads(sent.body) == {"Container": CONTAINER}


@pytest.mark.parametrize(
    "body",
    [
        {**CONNECT_BODY, "Force": True},
        {"Container": CONTAINER, "EndpointConfig": {"Links": ["x"]}},
        {"Container": CONTAINER, "EndpointConfig": {"DriverOpts": {"a": "b"}}},
        {"Container": CONTAINER, "EndpointConfig": {"MacAddress": "02:42:ac:11:00:02"}},
        {"Container": CONTAINER, "EndpointConfig": {"IPAMConfig": {"LinkLocalIPs": ["1.2.3.4"]}}},
        {"EndpointConfig": {}},
        {"Container": "../x"},
        {"Container": 5},
    ],
)
async def test_connect_refuses_any_body_outside_its_fixed_shape_unsent(
    engine: FakeEngine, engine_client: EngineClient, body: dict[str, Any]
) -> None:
    with pytest.raises(EngineError):
        await engine_client.networks.connect(HEX_ID, body)

    assert engine.requests == []


@pytest.mark.parametrize("network_id", ["", "..", "a/b", "a b"])
async def test_connect_and_disconnect_refuse_an_unsafe_network_id_unsent(
    engine: FakeEngine, engine_client: EngineClient, network_id: str
) -> None:
    with pytest.raises(EngineError):
        await engine_client.networks.connect(network_id, CONNECT_BODY)
    with pytest.raises(EngineError):
        await engine_client.networks.disconnect(network_id, container_id=CONTAINER)

    assert engine.requests == []


async def test_disconnect_never_asks_the_engine_to_force(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", f"/networks/{HEX_ID}/disconnect", Reply(200))

    await engine_client.networks.disconnect(HEX_ID, container_id=CONTAINER)

    (sent,) = engine.calls("POST", f"/networks/{HEX_ID}/disconnect")
    assert json.loads(sent.body) == {"Container": CONTAINER, "Force": False}


async def test_disconnect_refuses_an_unsafe_container_id_unsent(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    with pytest.raises(EngineError):
        await engine_client.networks.disconnect(HEX_ID, container_id="../x")

    assert engine.requests == []


@pytest.mark.parametrize(
    ("status", "error"), [(404, NotFound), (403, EngineError), (500, EngineError)]
)
async def test_connect_maps_the_engines_refusals_to_typed_errors(
    engine: FakeEngine, engine_client: EngineClient, status: int, error: type[EngineError]
) -> None:
    engine.route("POST", f"/networks/{HEX_ID}/connect", json_reply({"message": "no"}, status))

    with pytest.raises(error):
        await engine_client.networks.connect(HEX_ID, CONNECT_BODY)


async def test_volume_create_sends_name_driver_options_and_labels_and_reads_the_volume(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    reply = {**VOLUME_DOCUMENT, "Name": "db", "Driver": "nfs", "Options": {"o": "addr=1"}}
    engine.route("POST", "/volumes/create", json_reply(reply, 201))

    created = await engine_client.volumes.create(
        name="db", driver="nfs", driver_opts={"o": "addr=1"}, labels={"team": "core"}
    )

    (sent,) = engine.calls("POST", "/volumes/create")
    assert json.loads(sent.body) == {
        "Name": "db",
        "Driver": "nfs",
        "DriverOpts": {"o": "addr=1"},
        "Labels": {"team": "core"},
    }
    assert (created.name, created.driver, created.options) == ("db", "nfs", {"o": "addr=1"})


async def test_volume_create_omits_empty_options_and_labels(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", "/volumes/create", json_reply(VOLUME_DOCUMENT, 201))

    await engine_client.volumes.create(name="data")

    (sent,) = engine.calls("POST", "/volumes/create")
    assert json.loads(sent.body) == {"Name": "data", "Driver": "local"}


async def test_volume_create_for_an_existing_name_returns_the_existing_volume() -> None:
    engine = ContainerEngine()
    engine.add_volume("data", labels={"owner": "someone"})
    client = EngineClient(7, engine.bundle())

    returned = await client.volumes.create(name="data", labels={"owner": "me"})
    await client.aclose()

    assert returned.labels == {"owner": "someone"}


@pytest.mark.parametrize("name", ["", "a b", "a/b", "..", "-x", "a" * 256])
async def test_volume_create_refuses_an_unsafe_name_unsent(
    engine: FakeEngine, engine_client: EngineClient, name: str
) -> None:
    with pytest.raises(EngineError):
        await engine_client.volumes.create(name=name)

    assert engine.requests == []


@pytest.mark.parametrize("driver", ["", "a b", "-x", "a\nb", "d" * 129])
async def test_volume_create_refuses_a_malformed_driver_name_unsent(
    engine: FakeEngine, engine_client: EngineClient, driver: str
) -> None:
    with pytest.raises(EngineError):
        await engine_client.volumes.create(name="data", driver=driver)

    assert engine.requests == []


async def test_volume_create_reads_at_most_1_mib_of_reply(
    engine: FakeEngine, engine_client: EngineClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources, "VOLUME_CREATE_REPLY_BYTES", SMALL_CAP)
    engine.route("POST", "/volumes/create", Reply(201, b"{}", OVER_THE_SMALL_CAP))

    with pytest.raises(ResponseTooLarge):
        await engine_client.volumes.create(name="data")


async def test_volume_create_refuses_a_reply_that_is_not_a_volume(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", "/volumes/create", Reply(201, b"not json"))

    with pytest.raises(EngineError):
        await engine_client.volumes.create(name="data")


async def test_a_volume_create_that_times_out_is_an_unknown_outcome(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", "/volumes/create", Stall())

    with pytest.raises(RequestOutcomeUnknown):
        await engine_client.volumes.create(name="data")


def test_the_container_summary_carries_mounts_endpoints_and_the_network_mode() -> None:
    box = Box(
        CONTAINER,
        "web",
        mounts=[
            {"Type": "volume", "Name": "data", "Destination": "/data"},
            {"Type": "bind", "Source": "/srv", "Destination": "/srv"},
        ],
        endpoints={"front": {"NetworkID": HEX_ID}},
    )

    summary = ContainerSummary.model_validate(box.summary())

    assert [(m.type, m.name, m.destination) for m in summary.mounts] == [
        ("volume", "data", "/data"),
        ("bind", None, "/srv"),
    ]
    assert summary.network_ids == [HEX_ID]
    assert [(e.name, e.network_id) for e in summary.endpoints] == [("front", HEX_ID)]
    assert summary.network_mode == "bridge"


def test_the_summary_of_a_created_container_leaves_the_endpoint_ids_empty() -> None:
    box = Box(CONTAINER, "web", state="created", endpoints={"front": {"NetworkID": HEX_ID}})

    summary = ContainerSummary.model_validate(box.summary())

    assert summary.network_ids == []
    assert [(e.name, e.network_id) for e in summary.endpoints] == [("front", "")]


def test_the_summary_keeps_no_host_path_of_a_mount() -> None:
    box = Box(
        CONTAINER,
        "web",
        mounts=[{"Type": "bind", "Source": "/srv/host-layout", "Destination": "/srv"}],
    )

    summary = ContainerSummary.model_validate(box.summary())

    assert "/srv/host-layout" not in repr(summary)
