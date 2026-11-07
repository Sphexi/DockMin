"""Connecting a container to a network and detaching it, through the services."""

import asyncio
from typing import Any

import pytest

import app.services.network_attachments as attachments_module
from app.auth.errors import PermissionDenied, ResourceNotFound
from app.services.access import ResourceKey
from app.services.activity import UserActor
from app.services.assembly import NetworkServices
from app.services.container_actions import ContainerGone
from app.services.network_attachments import AttachInvalid, AttachRefused, DetachRefused
from app.services.networks import NetworkGone
from app.services.object_inspector import EnvironmentUnavailable
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from tests.access_support import ADMIN, ENV_A, ENV_B, PEER, STANDARD, container_id, control
from tests.docker_engine.container_engine import (
    BRIDGE_ID,
    ENGINE_TEXT,
    HOST_NETWORK_ID,
    NONE_NETWORK_ID,
    ContainerEngine,
    swarm_network_id,
)
from tests.docker_engine.fake_engine import Stall
from tests.object_support import add_container, drain_background, endpoint, rows_of
from tests.services.conftest import ContainerRig

SUBNET = "10.50.0.0/24"
GATEWAY = "10.50.0.1"
FREE_ADDRESS = "10.50.0.7"
CONTAINER = container_id(0xA1)
OTHER_CONTAINER = container_id(0xA2)
SMALL_SETTLE = 0.05


@pytest.fixture
def lan(host: ContainerEngine) -> dict[str, str]:
    """The networks a container can be offered, and a container that is on none of them."""
    ids = {
        "lan": host.add_network(
            "lan", 0x51, IPAM={"Config": [{"Subnet": SUBNET, "Gateway": GATEWAY}]}
        ),
        "auto": host.add_network("auto", 0x52),
        "closed": host.add_network(
            "closed", 0, "overlay", network_id=swarm_network_id(1), Scope="swarm"
        ),
        "open": host.add_network(
            "open", 0, "overlay", network_id=swarm_network_id(2), Scope="swarm", Attachable=True
        ),
        "v6": host.add_network(
            "v6",
            0x53,
            EnableIPv6=True,
            IPAM={"Config": [{"Subnet": SUBNET}, {"Subnet": "fd00:50::/64"}]},
        ),
        "other": host.add_network("other", 0x54),
        "bridge": BRIDGE_ID,
        "host": HOST_NETWORK_ID,
        "none": NONE_NETWORK_ID,
    }
    add_container(host, 0xA1, "web", state="running", networks={"other": ids["other"]})
    return ids


async def writable_by_standard(rig: ContainerRig, *keys: ResourceKey, level: str = "write") -> None:
    for key in keys:
        await control(rig.access, key, public=True, level=level)


def container_key(container: str = CONTAINER) -> ResourceKey:
    return ResourceKey(ENV_A, "container", container)


def network_key(network_id: str) -> ResourceKey:
    return ResourceKey(ENV_A, "network", network_id)


async def test_an_administrator_attaches_with_aliases_and_a_static_address_and_the_detail_shows_it(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    outcome = await nets.attachments.connect(
        ADMIN, ENV_A, CONTAINER, lan["lan"], aliases=["front", "web1"], ipv4=FREE_ADDRESS
    )

    assert host.network_bodies("connect") == [
        {
            "Container": CONTAINER,
            "EndpointConfig": {
                "Aliases": ["front", "web1"],
                "IPAMConfig": {"IPv4Address": FREE_ADDRESS},
            },
        }
    ]
    assert (outcome.network_name, outcome.ipv4_address, outcome.aliases) == (
        "lan",
        FREE_ADDRESS,
        ("front", "web1"),
    )
    detail = await rig.services.queries.detail(ADMIN, ENV_A, CONTAINER)
    attachment = next(n for n in detail.networks if n.name == "lan")
    assert (attachment.network_id, attachment.ip_address) == (lan["lan"], FREE_ADDRESS)
    assert attachment.aliases == ("front", "web1")
    (row,) = await rows_of(rig.db, "network_connected")
    assert (row["result"], row["target_type"], row["target_id"]) == (
        "success",
        "container",
        CONTAINER,
    )
    assert row["detail"] == {
        "name": "web",
        "network_id": lan["lan"],
        "network_name": "lan",
        "aliases": 2,
        "ipv4_address": FREE_ADDRESS,
        "ipv6_address": None,
    }


async def test_a_connect_without_aliases_or_addresses_sends_only_the_container(
    nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    await nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["auto"])

    assert host.network_bodies("connect") == [{"Container": CONTAINER}]


async def test_an_overlay_marked_attachable_by_swarm_id_can_be_joined_by_a_stopped_container(
    nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    add_container(host, 0xA3, "idle", state="exited")

    outcome = await nets.attachments.connect(ADMIN, ENV_A, container_id(0xA3), lan["open"])

    assert outcome.network_id == lan["open"]


async def test_detach_removes_the_endpoint_and_never_asks_the_engine_to_force(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    await nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["lan"])
    impact = await nets.attachments.disconnect_impact(ADMIN, ENV_A, CONTAINER, lan["lan"])

    outcome = await nets.attachments.disconnect(ADMIN, ENV_A, CONTAINER, lan["lan"])

    assert (impact.network_name, impact.container_name, impact.state) == ("lan", "web", "running")
    assert host.network_bodies("disconnect") == [{"Container": CONTAINER, "Force": False}]
    assert (outcome.network_name, outcome.container_name) == ("lan", "web")
    detail = await rig.services.queries.detail(ADMIN, ENV_A, CONTAINER)
    assert "lan" not in [n.name for n in detail.networks]
    (row,) = await rows_of(rig.db, "network_disconnected")
    assert row["detail"] == {"name": "web", "network_id": lan["lan"], "network_name": "lan"}


async def test_a_detached_network_is_removable_although_the_container_mode_still_names_it(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    await nets.attachments.disconnect(ADMIN, ENV_A, CONTAINER, lan["other"])
    rig.inspector.forget_network(ENV_A, lan["other"])

    impact = await nets.queries.remove_impact(ADMIN, ENV_A, lan["other"])
    await nets.actions.remove(ADMIN, ENV_A, lan["other"], expected_name="other", expected_in_use=0)

    assert host.boxes[CONTAINER].host_config == {"NetworkMode": "other"}
    assert impact.in_use == 0
    assert lan["other"] not in host.networks


async def test_a_network_the_container_is_not_on_cannot_be_detached(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    with pytest.raises(ResourceNotFound):
        await nets.attachments.disconnect(ADMIN, ENV_A, CONTAINER, lan["lan"])
    with pytest.raises(ResourceNotFound):
        await nets.attachments.disconnect_impact(ADMIN, ENV_A, CONTAINER, lan["lan"])

    assert host.network_bodies("disconnect") == []
    assert await rows_of(rig.db, "network_disconnected") == []


@pytest.mark.parametrize(
    ("network", "arguments", "field"),
    [
        ("host", {}, "network"),
        ("none", {}, "network"),
        ("closed", {}, "network"),
        ("other", {}, "network"),
        ("bridge", {"aliases": ["x"]}, "aliases"),
        ("lan", {"aliases": ["a..b"]}, "aliases"),
        ("lan", {"aliases": ["-lead"]}, "aliases"),
        ("lan", {"aliases": [f"a{n}" for n in range(17)]}, "aliases"),
        ("lan", {"ipv4": "10.99.0.5"}, "ipv4"),
        ("lan", {"ipv4": GATEWAY}, "ipv4"),
        ("lan", {"ipv4": "10.50.0.0"}, "ipv4"),
        ("lan", {"ipv4": "10.50.0.255"}, "ipv4"),
        ("lan", {"ipv4": "not-an-address"}, "ipv4"),
        ("lan", {"ipv4": "fd00:50::5"}, "ipv4"),
        ("lan", {"ipv6": "fd00:50::5"}, "ipv6"),
        ("auto", {"ipv4": "172.30.0.9"}, "ipv4"),
        ("v6", {"ipv6": "fd00:50::1:5", "ipv4": FREE_ADDRESS + "x"}, "ipv4"),
        ("bridge", {"ipv4": "172.17.0.9"}, "ipv4"),
    ],
)
async def test_a_connect_the_product_refuses_names_the_cause_and_reaches_no_engine(
    rig: ContainerRig,
    nets: NetworkServices,
    host: ContainerEngine,
    lan: dict[str, str],
    network: str,
    arguments: dict[str, Any],
    field: str,
) -> None:
    with pytest.raises(AttachInvalid) as refused:
        await nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan[network], **arguments)

    assert field in refused.value.fields
    assert host.network_bodies("connect") == []
    (row,) = await rows_of(rig.db, "network_connected")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "invalid")


async def test_an_address_another_running_endpoint_already_holds_is_refused(
    nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    add_container(host, 0xA2, "holder", state="running", networks={"lan": lan["lan"]})
    host.boxes[OTHER_CONTAINER].endpoints = {"lan": endpoint(lan["lan"], FREE_ADDRESS)}

    with pytest.raises(AttachInvalid) as refused:
        await nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["lan"], ipv4=FREE_ADDRESS)

    assert "ipv4" in refused.value.fields


async def test_a_dual_stack_address_pair_is_accepted_on_an_ipv6_network(
    nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    outcome = await nets.attachments.connect(
        ADMIN, ENV_A, CONTAINER, lan["v6"], ipv4=FREE_ADDRESS, ipv6="fd00:50::5"
    )

    assert (outcome.ipv4_address, outcome.ipv6_address) == (FREE_ADDRESS, "fd00:50::5")
    (body,) = host.network_bodies("connect")
    assert body["EndpointConfig"]["IPAMConfig"] == {
        "IPv4Address": FREE_ADDRESS,
        "IPv6Address": "fd00:50::5",
    }


@pytest.mark.parametrize("mode", ["host", "none", "container:abc"])
async def test_a_container_sharing_another_network_namespace_cannot_join_networks(
    nets: NetworkServices, host: ContainerEngine, lan: dict[str, str], mode: str
) -> None:
    box = add_container(host, 0xA4, "shared", state="running")
    box.host_config = {"NetworkMode": mode}

    with pytest.raises(AttachInvalid) as refused:
        await nets.attachments.connect(ADMIN, ENV_A, container_id(0xA4), lan["lan"])
    offer = await nets.queries.attach_offer(ADMIN, ENV_A, container_id(0xA4))

    assert "container" in refused.value.fields
    assert offer.networks == () and offer.blocked is not None
    assert host.network_bodies("connect") == []


async def test_a_second_connect_to_the_same_network_is_refused_as_already_connected(
    nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    await nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["lan"])

    with pytest.raises(AttachInvalid) as refused:
        await nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["lan"])

    assert "network" in refused.value.fields
    assert len(host.network_bodies("connect")) == 1


async def test_the_offer_leaves_out_joined_unjoinable_and_closed_networks(
    nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    offer = await nets.queries.attach_offer(ADMIN, ENV_A, CONTAINER)

    assert {c.name for c in offer.networks} == {"lan", "auto", "open", "v6", "bridge"}
    assert offer.blocked is None and offer.state == "running"
    assert {c.name for c in offer.networks if c.predefined} == {"bridge"}


@pytest.mark.parametrize(
    ("container", "network"),
    [("e" * 64, "lan"), (CONTAINER, "e" * 64), ("web", "lan"), (CONTAINER, "lan-name")],
)
async def test_a_missing_or_malformed_id_is_refused_as_not_found_or_gone_without_an_engine_write(
    nets: NetworkServices,
    host: ContainerEngine,
    lan: dict[str, str],
    container: str,
    network: str,
) -> None:
    resolved = lan.get(network, network)

    with pytest.raises((ResourceNotFound, NetworkGone, ContainerGone)):
        await nets.attachments.connect(ADMIN, ENV_A, container, resolved)
    with pytest.raises((ResourceNotFound, NetworkGone, ContainerGone)):
        await nets.attachments.disconnect(ADMIN, ENV_A, container, resolved)

    assert host.network_bodies("connect") == []


async def test_a_standard_user_needs_write_on_the_container_and_on_the_network(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    await writable_by_standard(rig, container_key())
    await writable_by_standard(rig, network_key(lan["open"]), level="read")

    with pytest.raises(ResourceNotFound):
        await nets.attachments.connect(STANDARD, ENV_A, CONTAINER, lan["lan"])
    with pytest.raises(PermissionDenied):
        await nets.attachments.connect(STANDARD, ENV_A, CONTAINER, lan["open"])
    with pytest.raises(ResourceNotFound):
        await nets.attachments.connect(STANDARD, ENV_A, CONTAINER, HOST_NETWORK_ID)

    assert host.network_bodies("connect") == []
    assert await rows_of(rig.db, "network_connected") == []


async def test_a_standard_user_with_only_read_on_the_container_is_denied_the_attach(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    await writable_by_standard(rig, network_key(lan["lan"]))
    await writable_by_standard(rig, container_key(), level="read")

    with pytest.raises(PermissionDenied):
        await nets.attachments.connect(STANDARD, ENV_A, CONTAINER, lan["lan"])
    with pytest.raises(PermissionDenied):
        await nets.attachments.disconnect(STANDARD, ENV_A, CONTAINER, lan["other"])

    assert host.network_bodies("connect") == [] and host.network_bodies("disconnect") == []


async def test_a_standard_user_cannot_attach_a_container_they_cannot_see(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    await writable_by_standard(rig, network_key(lan["lan"]))

    with pytest.raises(ResourceNotFound):
        await nets.attachments.connect(STANDARD, ENV_A, CONTAINER, lan["lan"])

    assert host.network_bodies("connect") == []


async def test_a_standard_user_attaches_and_detaches_objects_they_hold_write_on(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    created = await nets.actions.create(STANDARD, ENV_A, {"name": "mine", "driver": "bridge"})
    await writable_by_standard(rig, container_key())

    offer = await nets.queries.attach_offer(STANDARD, ENV_A, CONTAINER)
    await nets.attachments.connect(STANDARD, ENV_A, CONTAINER, created.network_id, aliases=["x"])
    await nets.attachments.disconnect(STANDARD, ENV_A, CONTAINER, created.network_id)

    assert [c.name for c in offer.networks] == ["mine"]
    assert len(host.network_bodies("connect")) == len(host.network_bodies("disconnect")) == 1


async def test_a_standard_user_can_detach_a_network_they_have_no_access_to(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    await nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["lan"])
    await writable_by_standard(rig, container_key())

    outcome = await nets.attachments.disconnect(STANDARD, ENV_A, CONTAINER, lan["lan"])

    assert outcome.network_name == "lan"


async def test_a_standard_user_on_a_local_socket_cannot_attach_or_detach(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    for key in (
        ResourceKey(ENV_B, "container", CONTAINER),
        ResourceKey(ENV_B, "network", lan["lan"]),
    ):
        await control(rig.access, key, public=True, level="write")

    with pytest.raises(PermissionDenied):
        await nets.attachments.connect(STANDARD, ENV_B, CONTAINER, lan["lan"])
    with pytest.raises(PermissionDenied):
        await nets.attachments.disconnect(STANDARD, ENV_B, CONTAINER, lan["other"])

    assert host.network_bodies("connect") == [] and host.network_bodies("disconnect") == []


@pytest.mark.parametrize("held", ["container", "network"])
async def test_a_busy_container_or_network_refuses_an_attach_without_waiting(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str], held: str
) -> None:
    key = (
        ObjectKey(ENV_A, "container", CONTAINER)
        if held == "container"
        else ObjectKey(ENV_A, "network", lan["lan"])
    )
    holder = LockHolder(UserActor(PEER.user_id), "peer", "recreate")

    async with rig.engine.locks.hold(key, holder):
        with pytest.raises(ObjectBusy):
            await nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["lan"])

    assert host.network_bodies("connect") == []
    (row,) = await rows_of(rig.db, "network_connected")
    assert row["detail"]["reason"] == "object_busy"


async def test_the_locks_are_free_again_after_an_attach(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    await nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["lan"])

    assert rig.engine.locks.holder_of(ObjectKey(ENV_A, "container", CONTAINER)) is None
    assert rig.engine.locks.holder_of(ObjectKey(ENV_A, "network", lan["lan"])) is None


async def test_an_engine_refusal_of_a_connect_is_a_product_error_with_no_engine_text(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    host.fail("network_connect", 403)

    with pytest.raises(AttachRefused) as refused:
        await nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["lan"])

    assert ENGINE_TEXT not in str(refused.value)
    (row,) = await rows_of(rig.db, "network_connected")
    assert row["detail"]["reason"] == "engine_rejected" and ENGINE_TEXT not in str(row)


async def test_an_engine_refusal_of_a_disconnect_is_a_product_error(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    await nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["lan"])
    host.fail("network_disconnect", 500)

    with pytest.raises(DetachRefused):
        await nets.attachments.disconnect(ADMIN, ENV_A, CONTAINER, lan["lan"])

    (row,) = await rows_of(rig.db, "network_disconnected")
    assert row["detail"]["reason"] == "engine_rejected"


async def test_a_not_found_from_the_engine_names_the_object_that_vanished(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    host.fail("network_connect", 404)
    with pytest.raises(NetworkGone):
        await nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["lan"])
    host.clear_faults()

    host.on("network_connect", host.boxes.clear)
    with pytest.raises(ContainerGone):
        await nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["auto"])

    reasons = [r["detail"]["reason"] for r in await rows_of(rig.db, "network_connected")]
    assert reasons == ["network_gone", "container_gone"]


async def test_a_connect_that_times_out_is_an_unknown_outcome(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    host.route("POST", f"/networks/{lan['lan']}/connect", Stall())

    with pytest.raises(EnvironmentUnavailable):
        await nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["lan"])

    (row,) = await rows_of(rig.db, "network_connected")
    assert row["detail"]["reason"] == "outcome_unknown"


async def test_a_connect_cancelled_while_the_engine_works_is_recorded_as_done(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    gate = host.hold("network_connect")
    task = asyncio.ensure_future(nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["lan"]))
    await gate.entered.wait()
    task.cancel()
    gate.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    (row,) = await rows_of(rig.db, "network_connected")
    assert (row["result"], row["detail"]["interrupted"]) == ("success", True)


async def test_a_connect_that_outlives_the_wait_is_recorded_as_unknown(
    rig: ContainerRig,
    nets: NetworkServices,
    host: ContainerEngine,
    lan: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attachments_module, "SETTLE_SECONDS", SMALL_SETTLE)
    gate = host.hold("network_connect")
    task = asyncio.ensure_future(nets.attachments.connect(ADMIN, ENV_A, CONTAINER, lan["lan"]))
    await gate.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.release.set()
    await drain_background()

    (row,) = await rows_of(rig.db, "network_connected")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "outcome_unknown")


async def test_a_never_started_container_can_be_detached_and_its_detail_links_the_network(
    rig: ContainerRig, nets: NetworkServices, host: ContainerEngine, lan: dict[str, str]
) -> None:
    box = add_container(host, 0xA5, "fresh", state="created")
    box.endpoints = {"lan": {"NetworkID": "", "IPAddress": ""}}
    fresh = container_id(0xA5)

    detail = await rig.services.queries.detail(ADMIN, ENV_A, fresh)
    outcome = await nets.attachments.disconnect(ADMIN, ENV_A, fresh, lan["lan"])

    attachment = next(n for n in detail.networks if n.name == "lan")
    assert attachment.network_id == lan["lan"]
    assert outcome.network_name == "lan"
    assert (await nets.queries.remove_impact(ADMIN, ENV_A, lan["lan"])).in_use == 0


@pytest.mark.parametrize("junk", ["../evil", "x" * 70, "ABC", ""])
async def test_a_detail_never_carries_an_endpoint_id_that_is_not_a_canonical_network_id(
    rig: ContainerRig, host: ContainerEngine, junk: str
) -> None:
    box = add_container(host, 0xA6, "odd", state="running")
    box.endpoints = {"weird": {"NetworkID": junk, "IPAddress": "10.0.0.2"}}

    detail = await rig.services.queries.detail(ADMIN, ENV_A, container_id(0xA6))

    assert [n.network_id for n in detail.networks if n.name == "weird"] == [""]


@pytest.mark.parametrize(
    ("actor", "granted", "expected"),
    [
        pytest.param(ADMIN, False, True, id="administrator"),
        pytest.param(STANDARD, False, False, id="no-write"),
        pytest.param(STANDARD, True, True, id="write"),
    ],
)
async def test_the_container_detail_offers_the_panel_to_those_who_may_change_it(
    rig: ContainerRig,
    host: ContainerEngine,
    lan: dict[str, str],
    actor: Any,
    granted: bool,
    expected: bool,
) -> None:
    await control(rig.access, container_key(), public=True, level="write" if granted else "read")

    detail = await rig.services.queries.detail(actor, ENV_A, CONTAINER)

    assert detail.can_attach is expected


async def test_the_panel_is_withheld_from_a_container_sharing_a_network_namespace(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    box = add_container(host, 0xA7, "hosted", state="running")
    box.host_config = {"NetworkMode": "host"}

    detail = await rig.services.queries.detail(ADMIN, ENV_A, container_id(0xA7))

    assert detail.can_attach is False
