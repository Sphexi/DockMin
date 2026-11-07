"""Network and volume routes: 404 parity, CSRF, the local-socket ceiling and hostile input."""

import pytest
from httpx import AsyncClient, Response

import app.docker_engine.resources as resources
from tests.auth_support import csrf_form
from tests.container_support import DB, JOB, WEB, plant_control, share_with_standard_user
from tests.docker_engine.container_engine import (
    BRIDGE_ID,
    HOST_NETWORK_ID,
    NONE_NETWORK_ID,
    ContainerEngine,
)
from tests.docker_engine.fake_engine import json_reply
from tests.object_support import STACK_LABEL, add_container, volume_mount
from tests.web.environment_support import World

NOT_FOUND, OK, CONFLICT, UNPROCESSABLE, FORBIDDEN, SEE_OTHER, BAD_REQUEST = (
    404,
    200,
    409,
    422,
    403,
    303,
    400,
)
Statuses = tuple[int, int, int]
LONG_NAME = "a" * 256
HOSTILE_MARKERS = (
    "<script>alert",
    "<img src=x",
    "<svg onload",
    "<b>shop</b>",
    "<i>s</i>",
    "<u>g</u>",
)
STACK = "shop"


async def seed(world: World, host: ContainerEngine) -> dict[str, str]:
    """Networks and volumes reaching the standard user in each way, and each way of missing."""
    await share_with_standard_user(world, host)
    remote, local = world.remote_id, world.local_id
    ids = {
        "shop": host.add_network("shop_default", 0xB2, Labels={STACK_LABEL: STACK}),
        "shop_upper": host.add_network("shop_upper", 0xB3, Labels={STACK_LABEL: STACK.upper()}),
        "wrongkey": host.add_network(
            "wrongkey", 0xB4, Labels={"Com.Docker.Compose.Project": STACK}
        ),
        "own_wins": host.add_network("own_wins", 0xB5, Labels={STACK_LABEL: STACK}),
        "hidden": host.add_network("hidden-net", 0xB6),
        "subnetted": host.add_network(
            "subnetted",
            0xB7,
            Labels={STACK_LABEL: STACK},
            IPAM={"Config": [{"Subnet": "10.60.0.0/24", "Gateway": "10.60.0.1"}]},
        ),
    }
    await plant_control(world.app, remote, "network", ids["own_wins"], administrators_only=True)
    await plant_control(world.app, remote, "network", ids["hidden"], administrators_only=True)
    for name, labels in (
        ("shop_data", {STACK_LABEL: STACK}),
        ("vol_wrongkey", {"COM.DOCKER.COMPOSE.PROJECT": STACK}),
        ("vol_own", {STACK_LABEL: STACK}),
        ("hidden-vol", {}),
    ):
        host.add_volume(name, labels=labels)
    await plant_control(world.app, remote, "volume", "vol_own", administrators_only=True)
    await plant_control(world.app, remote, "volume", "hidden-vol", administrators_only=True)
    await plant_control(world.app, local, "stack", STACK, public=True, level="write")
    return ids


async def network_statuses(client: AsyncClient, base: str, identifier: str) -> Statuses:
    form = csrf_form(client, expected_name="x", expected_in_use="0")
    return (
        (await client.get(f"{base}/{identifier}")).status_code,
        (await client.get(f"{base}/{identifier}/remove")).status_code,
        (await client.post(f"{base}/{identifier}/remove", data=form)).status_code,
    )


async def volume_statuses(client: AsyncClient, base: str, name: str) -> Statuses:
    form = csrf_form(client, expected_created_at="x", expected_in_use="0")
    return (
        (await client.get(f"{base}/{name}")).status_code,
        (await client.get(f"{base}/{name}/remove")).status_code,
        (await client.post(f"{base}/{name}/remove", data=form)).status_code,
    )


async def test_every_way_of_missing_a_network_is_the_same_404_for_a_standard_user(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = await seed(world, container_host)
    base = f"/environments/{world.remote_id}/networks"
    shop = ids["shop"]
    refused = {
        "missing": "e" * 64,
        "hidden": ids["hidden"],
        "base36-missing": "abcdefghijklmnopqrstuvwxy",
        "prefix-12": shop[:12],
        "prefix-25": shop[:25],
        "name": "shop_default",
        "overlong": shop + "0",
        "upper-case": shop.upper(),
        "wrong-key-case": ids["wrongkey"],
        "own-control-wins": ids["own_wins"],
    }

    found = {
        label: await network_statuses(standard_client, base, i) for label, i in refused.items()
    }

    assert found == dict.fromkeys(refused, (NOT_FOUND,) * 3)
    assert [m for m in container_host.mutations() if m.startswith("DELETE")] == []


async def test_a_standard_user_reaches_exactly_the_networks_the_stack_label_names_in_any_case(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = await seed(world, container_host)
    base = f"/environments/{world.remote_id}/networks"

    reachable = {
        label: await network_statuses(standard_client, base, ids[label])
        for label in ("shop", "shop_upper")
    }

    assert reachable == dict.fromkeys(("shop", "shop_upper"), (OK, OK, CONFLICT))


async def test_an_administrator_sees_every_network_and_the_same_404_for_ones_that_are_not_there(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = await seed(world, container_host)
    base = f"/environments/{world.remote_id}/networks"
    missing = {
        "e" * 64,
        "abcdefghijklmnopqrstuvwxy",
        ids["shop"][:12],
        ids["shop"][:25],
        ids["shop"].upper(),
        "shop_default",
        ids["shop"] + "0",
    }

    absent = {m: await network_statuses(admin_client, base, m) for m in missing}
    present = {
        i: await network_statuses(admin_client, base, i) for i in (ids["hidden"], ids["own_wins"])
    }

    assert set(absent.values()) == {(NOT_FOUND,) * 3}
    assert set(present.values()) == {(OK, OK, CONFLICT)}


async def test_every_way_of_missing_a_volume_is_the_same_404_for_a_standard_user(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await seed(world, container_host)
    base = f"/environments/{world.remote_id}/volumes"
    refused = {
        "missing": "missing-vol",
        "hidden": "hidden-vol",
        "len-255": "x" * 255,
        "dash": "-",
        "leading-dot": ".hidden",
        "other-case": "SHOP_DATA",
        "wrong-key-case": "vol_wrongkey",
        "own-control-wins": "vol_own",
        "anonymous-id-missing": "f" * 64,
    }

    found = {label: await volume_statuses(standard_client, base, n) for label, n in refused.items()}

    assert found == dict.fromkeys(refused, (NOT_FOUND,) * 3)
    assert container_host.volume_removals() == []


async def test_a_name_over_255_characters_is_refused_alike_for_every_role_whatever_exists(
    standard_client: AsyncClient,
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
) -> None:
    await seed(world, container_host)
    base = f"/environments/{world.remote_id}/volumes"

    std = await volume_statuses(standard_client, base, LONG_NAME)
    adm = await volume_statuses(admin_client, base, LONG_NAME)

    assert std == adm == (UNPROCESSABLE,) * 3


async def test_a_standard_user_reaches_the_stack_volume_and_an_administrator_the_hidden_ones(
    standard_client: AsyncClient,
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
) -> None:
    await seed(world, container_host)
    base = f"/environments/{world.remote_id}/volumes"

    std = await volume_statuses(standard_client, base, "shop_data")
    adm = [await volume_statuses(admin_client, base, n) for n in ("hidden-vol", "vol_own")]

    assert std == (OK, OK, CONFLICT) and adm == [(OK, OK, CONFLICT)] * 2


def routes(environment_id: int, ids: dict[str, str]) -> list[tuple[str, dict[str, str]]]:
    base = f"/environments/{environment_id}"
    shop = ids["shop"]
    stale = {"expected_created_at": "2024-05-06T07:08:09Z", "expected_in_use": "0"}
    return [
        (f"{base}/networks/new", {"name": "csrfnet", "driver": "bridge"}),
        (f"{base}/networks/prune", {"target": f"{shop}:shop_default"}),
        (
            f"{base}/networks/{shop}/remove",
            {"expected_name": "shop_default", "expected_in_use": "0"},
        ),
        (f"{base}/networks/{shop}/access", {"visibility": "public", "access_level": "write"}),
        (f"{base}/networks/{shop}/access/delete", {}),
        (f"{base}/containers/{DB}/networks/connect", {"network": shop}),
        (f"{base}/containers/{DB}/networks/{BRIDGE_ID}/disconnect", {}),
        (f"{base}/volumes/-/new", {"name": "csrfvol"}),
        (f"{base}/volumes/-/prune", {"mode": "all", "target": "shop_data:x"}),
        (f"{base}/volumes/shop_data/remove", stale),
        (
            f"{base}/volumes/shop_data/remove",
            {
                "force": "yes",
                "expected_created_at": stale["expected_created_at"],
                "expected_containers": "x",
            },
        ),
        (f"{base}/volumes/shop_data/access", {"visibility": "public", "access_level": "write"}),
        (f"{base}/volumes/shop_data/access/delete", {}),
    ]


def mutations(host: ContainerEngine) -> tuple[list[str], int, list[tuple[str, dict[str, str]]]]:
    return host.mutations(), len(host.network_bodies("create")), host.volume_removals()


async def test_every_post_route_refuses_a_request_without_the_token_or_from_another_origin(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = await seed(world, container_host)
    container_host.boxes[DB].endpoints = {
        "bridge": {"NetworkID": BRIDGE_ID, "IPAddress": "172.17.0.2"}
    }
    before = mutations(container_host)

    plain = [await admin_client.post(p, data=d) for p, d in routes(world.remote_id, ids)]
    foreign = [
        await admin_client.post(p, data=d, headers={"Origin": "https://evil.example"})
        for p, d in routes(world.remote_id, ids)
    ]

    assert {r.status_code for r in plain + foreign} == {FORBIDDEN}
    assert mutations(container_host) == before


async def test_a_standard_user_on_a_local_socket_changes_nothing_through_any_write_route(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = await seed(world, container_host)
    container_host.boxes[DB].endpoints = {
        "bridge": {"NetworkID": BRIDGE_ID, "IPAddress": "172.17.0.2"}
    }
    before = mutations(container_host)
    base = f"/environments/{world.local_id}"

    posted = [
        await standard_client.post(p, data=csrf_form(standard_client, **d))
        for p, d in routes(world.local_id, ids)
    ]
    forms = [
        await standard_client.get(f"{base}{suffix}")
        for suffix in (
            "/networks/new",
            "/volumes/-/new",
            f"/containers/{DB}/networks/connect",
            f"/networks/{ids['shop']}/remove",
            "/volumes/shop_data/remove?force=1",
        )
    ]

    assert {r.status_code for r in posted} <= {FORBIDDEN, NOT_FOUND}
    assert {r.status_code for r in forms} <= {FORBIDDEN, NOT_FOUND}
    assert mutations(container_host) == before


async def test_a_standard_user_cannot_attach_through_any_refused_network_or_address(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = await seed(world, container_host)
    ids["prefix25"] = ids["shop"][:25]
    container_host.boxes[DB].endpoints = {
        "bridge": {"NetworkID": BRIDGE_ID, "IPAddress": "172.17.0.2"}
    }
    cases = [
        {"network": "hidden"},
        {"network": "own_wins"},
        {"network": HOST_NETWORK_ID},
        {"network": NONE_NETWORK_ID},
        {"network": "shop_default"},
        {"network": "prefix25"},
        {"network": "subnetted", "ipv4": "10.61.0.5"},
        {"network": "subnetted", "ipv4": "10.60.0.1"},
        {"network": "subnetted", "ipv4": "10.60.0.0"},
        {"network": "subnetted", "ipv4": "10.60.0.255"},
        {"network": "subnetted", "ipv6": "fd00::5"},
        {"network": "shop", "ipv4": "172.30.0.9"},
        {"network": "shop", "aliases": "a..b"},
        {"network": BRIDGE_ID, "aliases": "x"},
    ]
    path = f"/environments/{world.remote_id}/containers/{DB}/networks/connect"

    responses = [
        await standard_client.post(
            path,
            data=csrf_form(
                standard_client, **{**case, "network": ids.get(case["network"], case["network"])}
            ),
        )
        for case in cases
    ]

    assert {r.status_code for r in responses} <= {BAD_REQUEST, NOT_FOUND, FORBIDDEN}
    assert container_host.network_bodies("connect") == []


async def test_a_standard_user_attaches_a_valid_static_address_and_cannot_change_other_containers(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = await seed(world, container_host)
    container_host.boxes[DB].endpoints = {
        "bridge": {"NetworkID": BRIDGE_ID, "IPAddress": "172.17.0.2"}
    }
    remote = world.remote_id

    valid = await standard_client.post(
        f"/environments/{remote}/containers/{DB}/networks/connect",
        data=csrf_form(standard_client, network=ids["subnetted"], ipv4="10.60.0.7"),
    )
    elsewhere = [
        await standard_client.post(
            f"/environments/{remote}/containers/{container}/networks/connect",
            data=csrf_form(standard_client, network=ids["shop"]),
        )
        for container in (JOB, WEB)
    ]

    assert valid.status_code == SEE_OTHER
    (body,) = container_host.network_bodies("connect")
    assert body["EndpointConfig"]["IPAMConfig"] == {"IPv4Address": "10.60.0.7"}
    assert {r.status_code for r in elsewhere} <= {FORBIDDEN, NOT_FOUND}


async def test_a_standard_user_cannot_detach_a_network_the_container_is_not_on(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = await seed(world, container_host)
    container_host.boxes[DB].endpoints = {
        "bridge": {"NetworkID": BRIDGE_ID, "IPAddress": "172.17.0.2"}
    }
    base = f"/environments/{world.remote_id}/containers/{DB}/networks"

    posted = [
        await standard_client.post(f"{base}/{n}/disconnect", data=csrf_form(standard_client))
        for n in (ids["shop"], ids["hidden"], "e" * 64)
    ]

    assert {r.status_code for r in posted} == {NOT_FOUND}
    assert container_host.network_bodies("disconnect") == []


def leaked(responses: list[Response]) -> list[str]:
    return [m for r in responses for m in HOSTILE_MARKERS if m in r.text]


async def test_hostile_names_labels_options_and_container_names_are_escaped_on_every_page(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    host = container_host
    evil = host.add_network(
        "<script>alert(1)</script>",
        0x81,
        Labels={"x": '"><img src=x onerror=alert(2)>', STACK_LABEL: "<b>shop</b>"},
        Options={"parent": "<svg onload=alert(5)>"},
        IPAM={"Config": [{"Subnet": "<i>s</i>", "Gateway": "<u>g</u>"}]},
    )
    box = add_container(host, 0xE7, "<svg onload=alert(3)>", mounts=[volume_mount("evilvol")])
    box.endpoints = {"<script>alert(1)</script>": {"NetworkID": evil, "IPAddress": "10.0.0.2"}}
    host.add_volume(
        "evilvol",
        options={"o": "<img src=x onerror=alert(4)>"},
        labels={"k<script>": "<script>alert(6)</script>"},
    )
    remote = world.remote_id
    cid = box.id
    pages = [
        "/networks",
        f"/networks/{evil}",
        f"/networks/{evil}?tab=containers",
        f"/networks/{evil}?tab=access",
        f"/networks/{evil}/remove",
        "/networks/prune",
        "/volumes",
        "/volumes/evilvol",
        "/volumes/evilvol?tab=containers",
        "/volumes/evilvol/remove",
        "/volumes/evilvol/remove?force=1",
        "/volumes/-/prune?mode=all",
        f"/containers/{cid}?tab=networks",
        f"/containers/{cid}/networks/connect",
        f"/containers/{cid}/networks/{evil}/disconnect",
        "/networks?q=%22%3E%3Cscript%3Ealert(8)%3C/script%3E&stack=%3Cb%3E",
        f"/containers/{cid}/networks/connect?network=%22%3E%3Cscript%3Ealert(9)%3C/script%3E",
    ]

    responses = [await admin_client.get(f"/environments/{remote}{page}") for page in pages]

    assert leaked(responses) == []
    assert "<script>alert(9)" not in responses[-1].text
    assert "<script>alert(8)" not in responses[-2].text


async def test_a_hostile_daemon_cannot_make_a_page_unbounded_or_list_an_object_with_a_bad_id(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    host = container_host
    wide = host.add_network("wide", 0x82)
    host.networks["ZZZ"] = {"Id": "ZZZ", "Name": "badid", "Driver": "bridge", "Scope": "local"}
    host.networks["a" * 65] = {
        "Id": "a" * 65,
        "Name": "longid",
        "Driver": "bridge",
        "Scope": "local",
    }
    document = {
        "Id": wide,
        "Name": "wide",
        "Driver": "bridge",
        "Scope": "local",
        "Labels": {"big": "A" * (1024 * 1024)},
        "Containers": {
            f"{n:064x}": {"Name": f"c{n}", "IPv4Address": f"10.1.{n // 250}.{n % 250}/16"}
            for n in range(5000)
        },
    }
    host.route("GET", f"/networks/{wide}", json_reply(document))
    base = f"/environments/{world.remote_id}/networks"

    listing = await admin_client.get(base)
    containers = await admin_client.get(f"{base}/{wide}?tab=containers")
    overview = await admin_client.get(f"{base}/{wide}?tab=overview")

    assert (
        listing.status_code == OK and "badid" not in listing.text and "longid" not in listing.text
    )
    assert containers.status_code == OK and overview.status_code == OK
    assert len(containers.text) + len(overview.text) < 1024 * 1024
    assert "A" * 4097 not in overview.text


async def test_lists_over_the_read_limit_do_not_stop_a_create_or_the_container_form(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = container_host
    monkeypatch.setattr(resources, "NETWORK_LIST_BYTES", 64 * 1024)
    monkeypatch.setattr(resources, "VOLUME_LIST_BYTES", 64 * 1024)
    for number in range(200):
        host.add_network(f"pad{number}", 0x1000 + number, Labels={"pad": "p" * 600})
        host.add_volume(f"padvol{number}", labels={"pad": "p" * 600})
    remote = f"/environments/{world.remote_id}"

    networks = await admin_client.get(f"{remote}/networks")
    volumes = await admin_client.get(f"{remote}/volumes")
    form = await admin_client.get(f"{remote}/containers/new")

    assert networks.status_code == 503 and "more network data" in networks.text
    assert volumes.status_code == 503 and "more volume data" in volumes.text
    assert form.status_code == OK
