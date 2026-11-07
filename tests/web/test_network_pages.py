"""The network pages: list, detail, create, remove, prune and the container attachment panel."""

import io
import json

import pytest
from httpx import AsyncClient, Response

import app.docker_engine.resources as resources
from tests.auth_support import csrf_form
from tests.container_support import DB, JOB, WEB, share_with_standard_user
from tests.docker_engine.container_engine import (
    BRIDGE_ID,
    ENGINE_TEXT,
    HOST_NETWORK_ID,
    NONE_NETWORK_ID,
    ContainerEngine,
    swarm_network_id,
)
from tests.docker_engine.fake_engine import Refuse, Reply
from tests.object_support import HTMX, STACK_LABEL, hidden_values, visible_text
from tests.web.access_panel_support import (
    PanelRoutes,
    assert_grants_kept,
    assert_stale_saves_refused,
    digest_in,
    grant_by_name_and_keep,
    save_under_stale_digest,
)
from tests.web.engine_detail_support import recorded_text
from tests.web.environment_support import World
from tests.web.flash_support import flash_texts, land, landed_texts, location
from tests.web.helpers import hx_triggers, parse_tags

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN, NOT_FOUND, CONFLICT = 200, 303, 400, 403, 404, 409
BAD_GATEWAY, UNAVAILABLE, UNPROCESSABLE = 502, 503, 422
SECRET_OPTION = "s3cr3t-option-value"
NETWORK_NAME = "network-0-name"
SECRET_LABEL = "label-secret-value"
LAN_SUBNET = "10.50.0.0/24"
REFRESH_EVENTS = {"container-changed", "counts-changed"}


def option_values(response: Response) -> list[str]:
    return [t.attrs["value"] for t in parse_tags(response.text) if t.name == "option"]


def networks_path(world: World, suffix: str = "") -> str:
    return f"/environments/{world.remote_id}/networks{suffix}"


def container_path(world: World, container: str, suffix: str = "") -> str:
    return f"/environments/{world.remote_id}/containers/{container}{suffix}"


def seeded(host: ContainerEngine) -> dict[str, str]:
    """A stack network, a network with secret options, a spare one and a container on the stack."""
    ids = {
        "shop": host.add_network(
            "shop_default",
            0x51,
            Labels={STACK_LABEL: "shop"},
            IPAM={"Config": [{"Subnet": LAN_SUBNET, "Gateway": "10.50.0.1"}]},
        ),
        "app": host.add_network(
            "app-net",
            0x52,
            Options={"parent": "eth0", "secret_token": SECRET_OPTION},
            Labels={"team": "core", "api_key": SECRET_LABEL},
            IPAM={"Config": [{"Subnet": "10.51.0.0/24", "Gateway": "10.51.0.1"}]},
        ),
        "spare": host.add_network("spare", 0x53),
    }
    host.boxes[WEB].endpoints = {
        "bridge": {"NetworkID": BRIDGE_ID, "IPAddress": "172.17.0.2", "Aliases": ["web"]},
        "shop_default": {
            "NetworkID": ids["shop"],
            "IPAddress": "10.50.0.5",
            "MacAddress": "02:42:0a:32:00:05",
            "Aliases": ["web", "front"],
        },
    }
    return ids


async def test_the_list_shows_every_network_with_its_badge_and_offers_remove_only_where_allowed(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = seeded(container_host)

    page = await admin_client.get(networks_path(world))

    assert page.status_code == OK and page.headers["cache-control"] == "no-store"
    text = visible_text(page)
    assert all(n in text for n in ("bridge", "host", "none", "shop_default", "app-net", "spare"))
    assert "Predefined" in text
    assert f"/networks/{BRIDGE_ID}/remove" not in page.text
    assert f"/networks/{ids['spare']}/remove" in page.text


async def test_each_filter_narrows_the_list(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seeded(container_host)
    base = networks_path(world)

    by_name = await admin_client.get(f"{base}?q=APP&driver=bridge&kind=user&usage=unused")
    predefined = await admin_client.get(f"{base}?kind=predefined")
    used = await admin_client.get(f"{base}?usage=used")
    stack = await admin_client.get(f"{base}?stack=SHOP")
    swarm = await admin_client.get(f"{base}?scope=swarm")

    assert visible_text(by_name).count("shop_default") == 0 and "app-net" in visible_text(by_name)
    assert "bridge" in visible_text(predefined) and "app-net" not in visible_text(predefined)
    assert "shop_default" in visible_text(used) and "spare" not in visible_text(used)
    assert "shop_default" in visible_text(stack) and "spare" not in visible_text(stack)
    assert "Nothing matches these filters" in visible_text(swarm) and "spare" not in visible_text(
        swarm
    )


async def test_the_list_answers_a_fragment_to_htmx_a_400_to_a_bad_filter_and_pages_on_a_limit(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seeded(container_host)

    fragment = await admin_client.get(networks_path(world) + "?kind=predefined", headers=HTMX)
    bad = await admin_client.get(networks_path(world) + "?driver=weird")
    paged = await admin_client.get(networks_path(world) + "?limit=2")

    assert fragment.status_code == OK and "<html" not in fragment.text
    assert bad.status_code == BAD_REQUEST and "Choose one of the listed drivers" in bad.text
    assert 'rel="next"' in paged.text


async def test_each_detail_tab_renders_and_secrets_never_reach_the_page(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = seeded(container_host)
    detail = networks_path(world, f"/{ids['app']}")

    tabs = [
        await admin_client.get(f"{detail}?tab={tab}")
        for tab in ("overview", "containers", "access")
    ]
    overview = tabs[0]
    shop = await admin_client.get(networks_path(world, f"/{ids['shop']}") + "?tab=containers")
    fragment = await admin_client.get(f"{detail}?tab=containers", headers=HTMX)

    assert [t.status_code for t in tabs] == [OK, OK, OK]
    assert SECRET_OPTION not in overview.text and "Redacted" in overview.text
    assert "10.51.0.0/24" in overview.text
    assert "10.50.0.5" in shop.text and "02:42:0a:32:00:05" in shop.text
    assert "<html" not in fragment.text


async def test_a_missing_or_malformed_network_is_a_404_and_so_is_an_unknown_tab(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = seeded(container_host)

    missing = [
        await admin_client.get(networks_path(world, suffix))
        for suffix in ("/zzz", f"/{'e' * 64}", f"/{'z' * 25}")
    ]
    unknown_tab = await admin_client.get(networks_path(world, f"/{ids['app']}") + "?tab=nope")

    assert {r.status_code for r in missing} == {NOT_FOUND} and unknown_tab.status_code == NOT_FOUND


async def test_the_create_form_adapts_to_the_driver_with_and_without_script(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    form = await admin_client.get(networks_path(world, "/new"))
    macvlan = await admin_client.get(networks_path(world, "/new?driver=macvlan"))
    carried = csrf_form(admin_client, name="made", driver="ipvlan", step="driver")
    continued = await admin_client.post(networks_path(world, "/new"), data=carried)
    swapped = await admin_client.post(networks_path(world, "/new"), data=carried, headers=HTMX)
    more_rows = await admin_client.post(
        networks_path(world, "/new"),
        data=csrf_form(admin_client, name="made", driver="bridge", rows="option"),
    )

    assert form.status_code == OK and form.headers["cache-control"] == "no-store"
    assert "A subnet is required" in macvlan.text
    assert continued.status_code == OK and "ipvlan_mode" in continued.text
    assert "<html" not in swapped.text and 'id="driver-fields"' in swapped.text
    assert "option-3-key" in more_rows.text


async def test_the_create_form_reports_each_refusal_where_it_belongs(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seeded(container_host)
    base = networks_path(world, "/new")

    blank = await admin_client.post(
        base, data=csrf_form(admin_client, name="", driver="bridge", subnet_v4="127.0.0.0/24")
    )
    overlap = await admin_client.post(
        base, data=csrf_form(admin_client, name="dupe", driver="bridge", subnet_v4="10.51.0.0/25")
    )
    taken = await admin_client.post(
        base, data=csrf_form(admin_client, name="app-net", driver="bridge")
    )

    assert blank.status_code == BAD_REQUEST and "Enter a name" in blank.text
    assert overlap.status_code == BAD_REQUEST
    assert "overlaps the existing network app-net" in overlap.text
    assert taken.status_code == CONFLICT and "already exists" in taken.text
    assert container_host.network_bodies("create") == []


async def test_docker_text_on_a_refused_network_is_for_administrators_only_and_recorded_nowhere(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    log_stream: io.StringIO,
) -> None:
    container_host.route(
        "POST",
        "/networks/create",
        Reply(BAD_REQUEST, json.dumps({"message": ENGINE_TEXT}).encode()),
    )
    path = networks_path(world, "/new")

    as_administrator = await admin_client.post(
        path, data=csrf_form(admin_client, name="refused", driver="bridge")
    )
    as_standard_user = await standard_client.post(
        path, data=csrf_form(standard_client, name="refused", driver="bridge")
    )

    assert as_administrator.status_code == as_standard_user.status_code == BAD_GATEWAY
    assert "Docker said" in as_administrator.text and ENGINE_TEXT in as_administrator.text
    assert "Docker said" not in as_standard_user.text
    assert ENGINE_TEXT not in as_standard_user.text
    assert ENGINE_TEXT not in await recorded_text(world.app, log_stream)


async def test_a_created_network_redirects_to_its_detail_and_says_so(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    form = csrf_form(
        admin_client,
        name="made",
        driver="bridge",
        subnet_v4="10.60.0.0/24",
        internal="on",
        **{
            "option-0-key": "com.docker.network.driver.mtu",
            "option-0-value": "1400",
            "label-0-key": "k",
            "label-0-value": "v",
        },
    )

    created = await admin_client.post(networks_path(world, "/new"), data=form)
    landed = await land(admin_client, created)

    assert "?" not in location(created)
    assert flash_texts(landed.text) == ["Network created."]
    (body,) = container_host.network_bodies("create")
    assert body["Internal"] is True and body["Options"] == {"com.docker.network.driver.mtu": "1400"}


async def test_the_remove_flow_names_the_network_and_binds_the_review_to_its_name_and_use(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = seeded(container_host)
    path = networks_path(world, f"/{ids['spare']}/remove")

    review = await admin_client.get(path)
    bad_name = await admin_client.post(
        path, data=csrf_form(admin_client, expected_name="wrong", expected_in_use="0")
    )
    incomplete = await admin_client.post(path, data=csrf_form(admin_client, expected_name="spare"))
    removed = await admin_client.post(
        path,
        data=csrf_form(
            admin_client,
            expected_name=hidden_values(review, "expected_name")[0],
            expected_in_use=hidden_values(review, "expected_in_use")[0],
        ),
    )

    landed = await land(admin_client, removed)

    assert review.status_code == OK and "Remove network" in review.text
    assert bad_name.status_code == CONFLICT and incomplete.status_code == BAD_REQUEST
    assert location(removed) == networks_path(world)
    assert flash_texts(landed.text) == ["Network removed."]
    assert ids["spare"] not in container_host.networks


async def test_an_in_use_network_names_its_containers_and_links_the_detach_tab(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = seeded(container_host)

    review = await admin_client.get(networks_path(world, f"/{ids['shop']}/remove"))
    refused = await admin_client.post(
        networks_path(world, f"/{ids['shop']}/remove"),
        data=csrf_form(admin_client, expected_name="shop_default", expected_in_use="1"),
    )

    assert "This network is in use" in review.text and "web" in review.text
    assert "tab=networks" in review.text and "Remove network</button>" not in review.text
    assert refused.status_code == CONFLICT and ids["shop"] in container_host.networks


async def test_a_predefined_network_has_no_remove_button_and_a_forged_post_is_refused(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    review = await admin_client.get(networks_path(world, f"/{BRIDGE_ID}/remove"))
    forged = await admin_client.post(
        networks_path(world, f"/{BRIDGE_ID}/remove"),
        data=csrf_form(admin_client, expected_name="bridge", expected_in_use="0"),
    )

    assert "cannot be removed" in review.text and "Remove network</button>" not in review.text
    assert forged.status_code == CONFLICT and BRIDGE_ID in container_host.networks


async def test_the_prune_page_lists_exactly_what_the_run_removes(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = seeded(container_host)
    path = networks_path(world, "/prune")

    review = await admin_client.get(path)
    targets = hidden_values(review, "target")
    ran = await admin_client.post(path, data=csrf_form(admin_client) | {"target": targets})
    ran_page = await land(admin_client, ran)
    empty = await admin_client.post(path, data=csrf_form(admin_client))

    text = visible_text(review)
    assert "spare" in text and "app-net" in text and "shop_default" not in text
    assert sorted(t.partition(":")[2] for t in targets) == ["app-net", "spare"]
    assert ran.status_code == SEE_OTHER and location(ran).startswith("/results/")
    assert "Removed" in ran_page.text
    assert not {ids["spare"], ids["app"]} & set(container_host.networks)
    assert ids["shop"] in container_host.networks
    assert empty.status_code == BAD_REQUEST


async def test_the_access_tab_saves_and_removes_a_control_and_the_environment_page_links_the_list(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = seeded(container_host)
    base = networks_path(world, f"/{ids['shop']}")

    saved = await admin_client.post(
        f"{base}/access", data=csrf_form(admin_client, visibility="public", access_level="read")
    )
    saved_page = await land(admin_client, saved)
    removed = await admin_client.post(
        f"{base}/access/delete",
        data=csrf_form(admin_client, expected_digest=digest_in(saved_page.text)),
    )
    removed_notices = await landed_texts(admin_client, removed)
    invalid = await admin_client.post(
        f"{base}/access", data=csrf_form(admin_client, visibility="bad")
    )
    environment = await admin_client.get(f"/environments/{world.remote_id}")

    assert location(saved) == f"{base}?tab=access"
    assert flash_texts(saved_page.text) == ["Access control saved."]
    assert removed_notices == ["Access control removed."]
    assert invalid.status_code == UNPROCESSABLE
    sidebar_link = f'<a class="nav__link" href="/environments/{world.remote_id}/networks">'
    assert sidebar_link in environment.text


def access_routes(world: World, network: str) -> PanelRoutes:
    base = networks_path(world, f"/{network}")
    return PanelRoutes(f"{base}?tab=access", f"{base}/access", f"{base}/access/delete")


async def test_the_network_access_panel_grants_by_typed_name_and_a_later_save_keeps_every_grant(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    routes = access_routes(world, seeded(container_host)["shop"])

    granted, resaved = await grant_by_name_and_keep(admin_client, routes)

    assert_grants_kept(granted, resaved)


async def test_a_network_save_under_a_stale_digest_is_a_conflict_that_shows_the_current_control(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    saves = await save_under_stale_digest(
        admin_client, access_routes(world, seeded(container_host)["shop"])
    )

    await assert_stale_saves_refused(world.app, saves)


async def test_a_standard_user_sees_only_permitted_networks_and_the_restricted_form(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await share_with_standard_user(world, container_host)
    ids = seeded(container_host)
    hidden = container_host.add_network("admin-only", 0x61)

    listing = await standard_client.get(networks_path(world))
    forbidden_detail = await standard_client.get(networks_path(world, f"/{hidden}"))
    overview = await standard_client.get(networks_path(world, f"/{ids['shop']}?tab=overview"))
    access = await standard_client.get(networks_path(world, f"/{ids['shop']}?tab=access"))
    prune = await standard_client.get(networks_path(world, "/prune"))
    form = await standard_client.get(networks_path(world, "/new"))

    text = visible_text(listing)
    assert "shop_default" in text and "admin-only" not in text and "Prune networks" not in text
    assert "Create network" in text
    assert forbidden_detail.status_code == NOT_FOUND
    assert "Not shown for your role" in overview.text and "?tab=access" not in overview.text
    assert access.status_code == FORBIDDEN and prune.status_code == FORBIDDEN
    assert "macvlan" not in form.text and 'name="subnet_v4"' not in form.text
    assert "Only administrators can set a subnet" in form.text
    assert "com.docker.network.bridge.enable_icc" in form.text
    assert "com.docker.network.bridge.name" not in form.text


async def test_a_standard_user_posting_a_restricted_network_is_told_what_is_for_administrators(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    restricted = [
        {"driver": "macvlan"},
        {"driver": "bridge", "subnet_v4": "10.9.0.0/24"},
        {
            "driver": "bridge",
            "option-0-key": "com.docker.network.bridge.name",
            "option-0-value": "x",
        },
    ]

    responses = [
        await standard_client.post(
            networks_path(world, "/new"), data=csrf_form(standard_client, name="nope", **fields)
        )
        for fields in restricted
    ]

    assert {r.status_code for r in responses} == {BAD_REQUEST}
    assert all("Only administrators" in r.text for r in responses)
    assert container_host.network_bodies("create") == []


async def test_a_standard_user_creates_and_then_removes_their_own_network(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    created = await standard_client.post(
        networks_path(world, "/new"), data=csrf_form(standard_client, name="mine", driver="bridge")
    )
    mine = created.headers["location"].split("/networks/")[1].split("?")[0]

    listing = await standard_client.get(networks_path(world))
    review = await standard_client.get(networks_path(world, f"/{mine}/remove"))

    assert created.status_code == SEE_OTHER and "mine" in visible_text(listing)
    assert review.status_code == OK


async def test_the_create_form_is_refused_to_a_standard_user_on_a_local_socket(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    form = await standard_client.get(f"/environments/{world.local_id}/networks/new")
    posted = await standard_client.post(
        f"/environments/{world.local_id}/networks/new",
        data=csrf_form(standard_client, name="nope", driver="bridge"),
    )

    assert form.status_code == FORBIDDEN and posted.status_code == FORBIDDEN
    assert container_host.network_bodies("create") == []


async def test_an_overlay_is_offered_on_a_swarm_manager_to_both_roles(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
) -> None:
    async with world.app.state.database.session() as db:
        await db.execute("UPDATE environment SET type = 'swarm' WHERE id = ?", (world.remote_id,))

    admin = await admin_client.get(networks_path(world, "/new?driver=overlay"))
    standard = await standard_client.get(networks_path(world, "/new?driver=overlay"))
    created = await admin_client.post(
        networks_path(world, "/new"),
        data=csrf_form(admin_client, name="ov", driver="overlay", attachable="on"),
    )

    assert 'value="overlay" selected' in admin.text and 'name="attachable"' in admin.text
    assert 'value="overlay" selected' in standard.text and 'value="macvlan"' not in standard.text
    assert created.status_code == SEE_OTHER


async def test_the_container_networks_tab_offers_attach_and_detach_and_reflects_a_change(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = seeded(container_host)
    other = container_host.add_network("other", 0x54)
    base = container_path(world, WEB)

    tab = await admin_client.get(f"{base}?tab=networks")
    fragment = await admin_client.get(f"{base}?tab=networks", headers=HTMX)
    overview = await admin_client.get(f"{base}?tab=overview")
    connect = await admin_client.get(f"{base}/networks/connect?network={other}")
    joined = await admin_client.post(
        f"{base}/networks/connect",
        data=csrf_form(admin_client, network=other, ipv4="", aliases="front back"),
        headers=HTMX,
    )
    after = await admin_client.get(f"{base}?tab=networks")

    assert "Attach to a network" in tab.text and "Detach" in tab.text
    assert f"{base}/networks/{BRIDGE_ID}/disconnect" in tab.text
    assert f"/networks/{ids['shop']}" in tab.text
    assert "<html" not in fragment.text and "attach-panel" in fragment.text
    assert "attach-panel" not in overview.text
    assert connect.status_code == OK and "bridge (bridge)" not in connect.text
    assert joined.status_code == OK and hx_triggers(joined) == REFRESH_EVENTS
    assert "other" in after.text and "front, back" in after.text


async def test_connect_errors_come_back_to_the_form_and_a_plain_post_redirects_to_the_tab(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = seeded(container_host)
    other = container_host.add_network("other", 0x54)
    base = container_path(world, WEB) + "/networks/connect"

    outside = await admin_client.post(
        base, data=csrf_form(admin_client, network=ids["shop"], ipv4="192.168.1.5")
    )
    outside_htmx = await admin_client.post(
        base, data=csrf_form(admin_client, network=ids["shop"], ipv4="192.168.1.5"), headers=HTMX
    )
    unknown = await admin_client.post(base, data=csrf_form(admin_client, network="nope"))
    plain = await admin_client.post(base, data=csrf_form(admin_client, network=other))
    again = await admin_client.post(base, data=csrf_form(admin_client, network=other))

    assert outside.status_code == BAD_REQUEST and "subnet" in outside.text.lower()
    assert outside_htmx.status_code == BAD_REQUEST
    assert 'id="attach-panel"' in outside_htmx.text and "<html" not in outside_htmx.text
    assert unknown.status_code == BAD_REQUEST
    assert plain.status_code == SEE_OTHER and plain.headers["location"].endswith("?tab=networks")
    assert again.status_code == BAD_REQUEST and "already connected" in again.text


async def test_the_detach_flow_confirms_names_the_attachment_and_triggers_a_refresh(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    ids = seeded(container_host)
    path = container_path(world, WEB) + f"/networks/{ids['shop']}/disconnect"

    review = await admin_client.get(path)
    dialog = await admin_client.get(path, headers=HTMX)
    done = await admin_client.post(path, data=csrf_form(admin_client), headers=HTMX)
    gone = await admin_client.post(path, data=csrf_form(admin_client), headers=HTMX)

    assert review.status_code == OK and "10.50.0.5" in review.text
    assert "<html" not in dialog.text and "<dialog" in dialog.text
    assert done.status_code == OK and hx_triggers(done) == REFRESH_EVENTS
    assert "Detached" in done.text
    assert gone.status_code == NOT_FOUND
    assert container_host.network_bodies("disconnect") == [{"Container": WEB, "Force": False}]


async def test_a_container_sharing_a_network_namespace_has_no_panel_and_the_form_says_why(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.boxes[WEB].host_config = {"NetworkMode": "host", "RestartPolicy": {"Name": "no"}}

    tab = await admin_client.get(container_path(world, WEB, "?tab=networks"))
    form = await admin_client.get(container_path(world, WEB, "/networks/connect"))

    assert tab.status_code == OK and "Attach to a network" not in tab.text
    assert "shares another network namespace" in form.text


async def test_an_administrator_cannot_attach_to_an_unjoinable_or_joined_network(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    path = container_path(world, WEB, "/networks/connect")

    responses = [
        await admin_client.post(path, data=csrf_form(admin_client, network=network))
        for network in (HOST_NETWORK_ID, NONE_NETWORK_ID, BRIDGE_ID)
    ]

    assert {r.status_code for r in responses} == {BAD_REQUEST}
    assert container_host.network_bodies("connect") == []


async def test_a_standard_user_attaches_through_the_panel_where_they_hold_write_and_nowhere_else(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await share_with_standard_user(world, container_host)
    container_host.boxes[DB].endpoints = {
        "bridge": {"NetworkID": BRIDGE_ID, "IPAddress": "172.17.0.2"}
    }
    hidden = container_host.add_network("admin-only", 0x61)
    created = await standard_client.post(
        networks_path(world, "/new"), data=csrf_form(standard_client, name="mine", driver="bridge")
    )
    mine = created.headers["location"].split("/networks/")[1].split("?")[0]
    base = container_path(world, DB)

    writable = await standard_client.get(f"{base}?tab=networks")
    read_only = await standard_client.get(container_path(world, JOB, "?tab=networks"))
    no_form = await standard_client.get(container_path(world, JOB, "/networks/connect"))
    to_hidden = await standard_client.post(
        f"{base}/networks/connect", data=csrf_form(standard_client, network=hidden)
    )
    to_mine = await standard_client.post(
        f"{base}/networks/connect",
        data=csrf_form(standard_client, network=mine, aliases="db-alias"),
    )

    assert "Attach to a network" in writable.text and "admin-only" not in writable.text
    assert "mine" in writable.text and f"/networks/{BRIDGE_ID}" in writable.text
    assert "Attach to a network" not in read_only.text and "Detach" not in read_only.text
    assert no_form.status_code == FORBIDDEN
    assert to_hidden.status_code in (BAD_REQUEST, NOT_FOUND)
    assert to_mine.status_code == SEE_OTHER


async def test_the_container_create_form_wires_network_names_to_suggestions_of_the_visible_networks(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
) -> None:
    await share_with_standard_user(world, container_host)
    container_host.add_network("admin-only", 0x61)
    created = await standard_client.post(
        networks_path(world, "/new"), data=csrf_form(standard_client, name="mine", driver="bridge")
    )
    assert created.status_code == SEE_OTHER
    path = f"/environments/{world.remote_id}/containers/new"

    standard = await standard_client.post(
        path, data=csrf_form(standard_client, rows="networks", image="")
    )
    admin = await admin_client.post(path, data=csrf_form(admin_client, rows="networks", image=""))
    suggest_href = f"/environments/{world.remote_id}/suggestions/networks?field={NETWORK_NAME}"
    wrapped = [t.attrs["hx-get"] for t in parse_tags(standard.text) if "hx-get" in t.attrs]
    name = next(t for t in parse_tags(standard.text) if t.attrs.get("name") == NETWORK_NAME)
    offered = {
        who: await client.get(f"{suggest_href}&{NETWORK_NAME}={typed}")
        for who, client, typed in (
            ("standard mine", standard_client, "mine"),
            ("standard admin", standard_client, "admin"),
            ("standard host", standard_client, "host"),
            ("administrator admin", admin_client, "admin"),
            ("administrator host", admin_client, "host"),
        )
    }

    assert suggest_href in wrapped
    assert name.attrs["list"] == f"suggest-{NETWORK_NAME}"
    assert f'<datalist id="suggest-{NETWORK_NAME}"></datalist>' in standard.text
    assert f'<datalist id="suggest-{NETWORK_NAME}"></datalist>' in admin.text
    assert {who: option_values(response) for who, response in offered.items()} == {
        "standard mine": ["mine"],
        "standard admin": [],
        "standard host": [],
        "administrator admin": ["admin-only"],
        "administrator host": ["host"],
    }


async def test_a_host_that_cannot_be_read_gives_an_error_state_not_a_traceback(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.route("GET", "/networks", Refuse())
    container_host.route("GET", f"/networks/{BRIDGE_ID}", Refuse())
    base = f"/environments/{world.remote_id}"

    page = await admin_client.get(f"{base}/networks")
    fragment = await admin_client.get(f"{base}/networks", headers=HTMX)
    detail = await admin_client.get(f"{base}/networks/{BRIDGE_ID}", headers=HTMX)
    prune = await admin_client.get(f"{base}/networks/prune")
    down = await admin_client.get(f"/environments/{world.down_id}/networks")

    assert page.status_code == UNAVAILABLE and "Traceback" not in page.text
    assert fragment.status_code == UNAVAILABLE and "state--error" in fragment.text
    assert "<html" not in fragment.text
    assert detail.status_code == UNAVAILABLE and "<html" not in detail.text
    assert prune.status_code == UNAVAILABLE and down.status_code == UNAVAILABLE


async def test_a_network_list_over_the_read_limit_is_refused_whole(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resources, "NETWORK_LIST_BYTES", 10)
    container_host.route("GET", "/networks", Reply(200, b"[]", {"Content-Length": "999"}))

    page = await admin_client.get(networks_path(world))

    assert page.status_code == UNAVAILABLE and "more network data" in page.text


async def test_the_swarm_id_of_an_overlay_reaches_its_detail_page(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    overlay = container_host.add_network(
        "ov", 0, "overlay", network_id=swarm_network_id(3), Scope="swarm", Attachable=True
    )

    page = await admin_client.get(networks_path(world, f"/{overlay}"))

    assert page.status_code == OK and "swarm" in visible_text(page)
