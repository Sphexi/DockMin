"""Config, secret and node flows through the pages, and the secret value sweep."""

import base64
import json
import logging

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tests.auth_support import csrf_form
from tests.docker_engine.swarm_engine import WORKER, SwarmEngine, plain_spec
from tests.object_support import HTMX, rows_of
from tests.swarm_support import SENTINEL, hidden_fields, without_token
from tests.web.access_panel_support import (
    PanelRoutes,
    assert_grants_kept,
    assert_stale_saves_refused,
    digest_in,
    grant_by_name_and_keep,
    save_under_stale_digest,
)
from tests.web.environment_support import World
from tests.web.flash_support import flash_texts, land, landed_texts, location

OK, SEE_OTHER, BAD, CONFLICT, UNPROCESSABLE, UNAVAILABLE = 200, 303, 400, 409, 422, 503
LEAKED = "LEAK-HOSTILE-VALUE"
MAX_FORM_BYTES = 129 * 1024
REENTER_NOTICE = "Enter the value again."


def base_of(world: World) -> str:
    return f"/environments/{world.remote_id}/swarm"


def id_of(location: str, collection: str) -> str:
    return location.split(f"/{collection}/")[1].split("?")[0]


async def test_a_config_is_created_by_paste_or_upload_and_its_content_is_shown_escaped(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    base = base_of(world)

    pasted = await admin_client.post(
        f"{base}/configs/new",
        data=csrf_form(
            admin_client,
            name="app-conf",
            content="a=1\nb=<2>",
            **{"label-0-key": "team", "label-0-value": "core"},
        ),
    )
    uploaded = await admin_client.post(
        f"{base}/configs/new",
        data=csrf_form(admin_client, name="up-conf"),
        files={"upload": ("x.txt", b"uploaded content")},
    )
    page = await admin_client.get(pasted.headers["location"])

    names = {c["Spec"]["Name"] for c in swarm_host.configs.values()}
    assert (pasted.status_code, uploaded.status_code) == (SEE_OTHER, SEE_OTHER)
    assert names == {"app-conf", "up-conf"}
    assert "a=1" in page.text and "&lt;2&gt;" in page.text and "core" in page.text


async def test_a_config_over_the_cap_is_refused_with_a_form_not_stored(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    response = await admin_client.post(
        f"{base_of(world)}/configs/new",
        data=csrf_form(admin_client, name="big"),
        files={"upload": ("x.txt", b"x" * MAX_FORM_BYTES)},
    )

    assert response.status_code in (BAD, 413) and swarm_host.configs == {}


async def test_removing_a_config_in_use_shows_the_service_and_offers_no_confirmation_form(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    config = swarm_host.add_config("app-conf")
    swarm_host.named_service("web", configs=[(config, "app-conf")])
    base = f"{base_of(world)}/configs/{config}"

    page = await admin_client.get(f"{base}/remove")
    post = await admin_client.post(
        f"{base}/remove",
        data=csrf_form(admin_client, expected_name="app-conf", expected_in_use="0"),
    )
    detail = await admin_client.get(base)

    assert page.status_code == OK and "is in use" in page.text
    assert 'name="expected_name"' not in page.text
    assert post.status_code == CONFLICT and "web" in detail.text


async def test_a_free_config_is_removed_through_its_confirmation(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    config = swarm_host.add_config("up-conf")
    page = await admin_client.get(f"{base_of(world)}/configs/{config}/remove")

    removed = await admin_client.post(
        page.url.path, data=csrf_form(admin_client, **without_token(hidden_fields(page)))
    )
    notices = await landed_texts(admin_client, removed)

    assert removed.status_code == SEE_OTHER and location(removed) == f"{base_of(world)}/configs"
    assert notices == ["Config removed."]
    assert swarm_host.configs == {}


async def test_an_unreachable_manager_answers_a_script_request_for_a_swarm_page_with_the_fragment(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    config = swarm_host.add_config("conf")
    swarm_host.outage = {"configs"}
    base = f"{base_of(world)}/configs"

    detail = await admin_client.get(f"{base}/{config}", headers=HTMX)
    removal = await admin_client.get(f"{base}/{config}/remove", headers=HTMX)
    listing = await admin_client.get(base, headers=HTMX)

    assert [r.status_code for r in (detail, removal, listing)] == [UNAVAILABLE] * 3
    assert not any("<html" in r.text or "Traceback" in r.text for r in (detail, removal, listing))


async def test_a_binary_config_is_never_rendered(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    config = swarm_host.add_config("blob", bytes(range(256)))

    page = await admin_client.get(f"{base_of(world)}/configs/{config}")

    assert (
        page.status_code == OK
        and "Binary content," in page.text
        and "It is not shown." in page.text
    )
    assert "\x01" not in page.text


async def test_a_configs_access_control_is_saved_and_removed_from_its_tab(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    config = swarm_host.add_config("conf")
    base = f"{base_of(world)}/configs/{config}"

    saved = await admin_client.post(
        f"{base}/access", data=csrf_form(admin_client, visibility="public", access_level="read")
    )
    saved_page = await land(admin_client, saved)
    removed = await admin_client.post(
        f"{base}/access/delete",
        data=csrf_form(admin_client, expected_digest=digest_in(saved_page.text)),
    )
    removed_notices = await landed_texts(admin_client, removed)

    assert location(saved) == f"{base}?tab=access"
    assert flash_texts(saved_page.text) == ["Access control saved."]
    assert removed_notices == ["Access control removed."]


def config_access_routes(world: World, config: str) -> PanelRoutes:
    base = f"{base_of(world)}/configs/{config}"
    return PanelRoutes(f"{base}?tab=access", f"{base}/access", f"{base}/access/delete")


async def test_a_configs_access_panel_grants_by_typed_name_and_a_later_save_keeps_every_grant(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    routes = config_access_routes(world, swarm_host.add_config("conf"))

    granted, resaved = await grant_by_name_and_keep(admin_client, routes)

    assert_grants_kept(granted, resaved)


async def test_a_config_save_under_a_stale_digest_is_a_conflict_that_shows_the_current_control(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    routes = config_access_routes(world, swarm_host.add_config("conf"))

    saves = await save_under_stale_digest(admin_client, routes)

    await assert_stale_saves_refused(world.app, saves)


async def test_a_refused_secret_create_re_renders_the_form_without_the_value_in_any_form(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    base = f"{base_of(world)}/secrets/new"
    data = csrf_form(admin_client, name="bad name!", value=SENTINEL)

    bad_name = await admin_client.post(base, data=data)
    both = await admin_client.post(
        base,
        data=csrf_form(admin_client, name="both", value=SENTINEL),
        files={"upload": ("s.txt", SENTINEL.encode())},
    )
    more_rows = await admin_client.post(
        base, data=csrf_form(admin_client, name="rowsy", value=SENTINEL, rows="label")
    )

    assert bad_name.status_code == BAD and both.status_code == BAD and more_rows.status_code == OK
    assert 'name="value"' in bad_name.text
    assert SENTINEL not in bad_name.text + both.text + more_rows.text
    assert swarm_host.secrets == {}


async def test_a_refused_secret_create_says_to_enter_the_value_again_and_a_config_does_not(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    secrets = f"{base_of(world)}/secrets/new"
    configs = f"{base_of(world)}/configs/new"

    fresh = await admin_client.get(secrets)
    refused = await admin_client.post(
        secrets, data=csrf_form(admin_client, name="bad name!", value=SENTINEL)
    )
    refused_config = await admin_client.post(
        configs, data=csrf_form(admin_client, name="bad name!", content="x")
    )

    assert REENTER_NOTICE not in fresh.text
    assert refused.status_code == BAD and REENTER_NOTICE in refused.text
    assert SENTINEL not in refused.text
    assert refused_config.status_code == BAD and REENTER_NOTICE not in refused_config.text


async def test_a_duplicate_secret_by_typing_or_upload_is_a_conflict_that_echoes_nothing(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    base = f"{base_of(world)}/secrets/new"
    created = await admin_client.post(
        base, data=csrf_form(admin_client, name="db-pass", value=SENTINEL)
    )

    typed = await admin_client.post(
        base, data=csrf_form(admin_client, name="db-pass", value=SENTINEL)
    )
    uploaded = await admin_client.post(
        base,
        data=csrf_form(admin_client, name="db-pass"),
        files={"upload": ("s.txt", SENTINEL.encode())},
    )

    assert created.status_code == SEE_OTHER
    assert (typed.status_code, uploaded.status_code) == (CONFLICT, CONFLICT)
    assert SENTINEL not in typed.text + uploaded.text


async def test_the_secret_value_is_on_no_page_fragment_row_or_log_after_it_was_created(
    admin_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
    started_app: FastAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    base = base_of(world)
    created = await admin_client.post(
        f"{base}/secrets/new", data=csrf_form(admin_client, name="db-pass", value=SENTINEL)
    )
    secret = id_of(created.headers["location"], "secrets")
    service = swarm_host.named_service("web", secrets=[(secret, "db-pass")])
    urls = [
        created.headers["location"],
        f"{base}/secrets",
        f"{base}/secrets/{secret}",
        f"{base}/secrets/{secret}?tab=access",
        f"{base}/secrets/new",
        f"{base}/services/{service}",
        f"{base}/services/{service}/edit",
        f"{base}/services/{service}?tab=configuration",
        f"{base}/stacks",
        f"{base}/",
    ]
    bodies = []
    for url in urls:
        bodies.append((await admin_client.get(url)).text)
        bodies.append((await admin_client.get(url, headers=HTMX)).text)

    async with started_app.state.database.session() as db:
        rows = json.dumps(await rows_of(db), default=str)
    sent = [
        base64.b64decode(b["Data"]) for name, b in swarm_host.create_bodies if name == "secrets"
    ]
    assert sent == [SENTINEL.encode()]
    assert SENTINEL not in "".join(bodies) + rows + caplog.text


async def test_a_daemon_that_returns_secret_data_has_it_rendered_nowhere(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    swarm_host.secret_data_visible = True
    secret = swarm_host.add_secret("leaky", data=LEAKED.encode())
    base = f"{base_of(world)}/secrets"

    page = await admin_client.get(f"{base}/{secret}")
    listing = await admin_client.get(base)

    assert page.status_code == OK and LEAKED not in page.text + listing.text


async def test_removing_a_secret_goes_through_its_confirmation_and_never_forces(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    secret = swarm_host.add_secret("leaky")
    base = f"{base_of(world)}/secrets/{secret}"

    removed = await admin_client.post(
        f"{base}/remove", data=csrf_form(admin_client, expected_name="leaky", expected_in_use="0")
    )

    assert removed.status_code == SEE_OTHER and swarm_host.secrets == {}


async def test_the_secret_form_does_not_claim_the_value_is_hidden_as_you_type(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    page = await admin_client.get(f"{base_of(world)}/secrets/new")

    assert "hidden as you type" not in page.text and "Some browsers show the value" in page.text


async def test_draining_a_node_asks_for_confirmation_applies_once_and_a_replay_is_stale(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id = swarm_host.add_service(plain_spec("web", replicas=2))
    base = f"{base_of(world)}/nodes/{WORKER}/availability"
    confirmation = await admin_client.get(f"{base}?availability=drain")
    fields = without_token(hidden_fields(confirmation))

    drained = await admin_client.post(base, data=csrf_form(admin_client, **fields))
    page = await land(admin_client, drained)
    replay = await admin_client.post(base, data=csrf_form(admin_client, **fields))
    restore = await admin_client.post(
        base, data=csrf_form(admin_client, **without_token(hidden_fields(page)))
    )

    assert drained.status_code == SEE_OTHER and location(drained) == base.removesuffix(
        "/availability"
    )
    assert flash_texts(page.text) == ["Availability changed."]
    assert "Set active" in page.text
    assert replay.status_code == CONFLICT and restore.status_code == SEE_OTHER
    assert swarm_host.nodes[WORKER]["Spec"]["Availability"] == "active"
    assert service_id in swarm_host.services


async def test_an_unknown_availability_is_a_400(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    page = await admin_client.get(
        f"{base_of(world)}/nodes/{WORKER}/availability?availability=sleep"
    )

    assert page.status_code == BAD


async def test_the_overview_links_configs_and_secrets_and_the_scale_ceiling_follows_the_role(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id = swarm_host.add_service(plain_spec("web"))
    swarm_host.add_config("cfg1")
    swarm_host.add_secret("sec1")
    base = base_of(world)

    overview = await admin_client.get(base)
    detail = await admin_client.get(f"{base}/services/{service_id}")

    assert f"{base}/configs" in overview.text and f"{base}/secrets" in overview.text
    assert 'max="1000"' in detail.text
