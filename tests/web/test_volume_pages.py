"""The volume pages: list, detail, create, remove, force removal and prune."""

import io
import json
import re

import pytest
from httpx import AsyncClient, Response

import app.docker_engine.resources as resources
from app.services.activity import SubsystemActor
from app.services.object_locks import LockHolder, ObjectBusy
from app.services.volume_views import MAX_FORCE_CONTAINERS
from app.services.volumes import VolumeService
from tests.auth_support import csrf_form
from tests.container_support import JOB, share_with_standard_user
from tests.docker_engine.container_engine import ENGINE_TEXT, ContainerEngine, anonymous_volume
from tests.docker_engine.fake_engine import Refuse, Reply
from tests.object_support import (
    HTMX,
    STACK_LABEL,
    add_container,
    hidden_values,
    visible_text,
    volume_mount,
)
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
from tests.web.helpers import parse_tags

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN, NOT_FOUND, CONFLICT = 200, 303, 400, 403, 404, 409
UNAVAILABLE, UNPROCESSABLE = 503, 422
SECRET = "hunter2"
MOUNT_SOURCE = "mount-0-source"
USER = "e2e-user"
USER_ID = "c" * 63 + "f"


def option_values(response: Response) -> list[str]:
    return [t.attrs["value"] for t in parse_tags(response.text) if t.name == "option"]


def volumes_path(world: World, suffix: str = "") -> str:
    return f"/environments/{world.remote_id}/volumes{suffix}"


def seeded(host: ContainerEngine) -> str:
    """Plain, stack, plugin, unused and daemon-anonymous volumes, and two users of `seeded`."""
    host.add_volume("data", labels={STACK_LABEL: "shop", "team": "core", "api_key": "zzz"})
    host.add_volume("seeded")
    host.add_volume("unused-one")
    host.add_volume(
        "nfs-vol",
        driver="nfs",
        options={"o": f"addr=1.2.3.4,password={SECRET}", "type": "nfs", "device": ":/x"},
    )
    anonymous = anonymous_volume(1)
    host.add_volume(anonymous, anonymous=True)
    host.boxes[JOB].mounts = [volume_mount("seeded")]
    host.add(USER_ID, USER, image="busybox", state="exited", mounts=[volume_mount("seeded", "/s")])
    return anonymous


async def test_the_list_shows_kinds_filters_and_pages_and_refuses_a_bad_filter(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    anonymous = seeded(container_host)

    page = await admin_client.get(volumes_path(world))
    anonymous_only = await admin_client.get(volumes_path(world) + "?kind=anonymous")
    fragment = await admin_client.get(
        volumes_path(world) + "?driver=other&usage=unused", headers=HTMX
    )
    bad = await admin_client.get(volumes_path(world) + "?kind=bad")
    paged = await admin_client.get(volumes_path(world) + "?limit=2")

    text = visible_text(page)
    assert page.status_code == OK and page.headers["cache-control"] == "no-store"
    assert all(name in text for name in ("data", "seeded", "unused-one", "nfs-vol"))
    assert "Anonymous" in text and anonymous[:12] in text
    assert anonymous[:12] in anonymous_only.text and "unused-one" not in anonymous_only.text
    assert "nfs-vol" in fragment.text and "<html" not in fragment.text
    assert bad.status_code == BAD_REQUEST and 'rel="next"' in paged.text


async def test_volumes_named_new_and_prune_are_reached_by_name_beside_the_dash_routes(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.add_volume("new")
    container_host.add_volume("prune")

    named_new = await admin_client.get(volumes_path(world, "/new"))
    named_prune = await admin_client.get(volumes_path(world, "/prune"))
    form = await admin_client.get(volumes_path(world, "/-/new"))

    assert named_new.status_code == OK and 'name="driver"' not in named_new.text
    assert named_prune.status_code == OK and "Prune volumes" not in visible_text(named_prune)
    assert form.status_code == OK and 'name="driver"' in form.text
    assert form.headers["cache-control"] == "no-store"


async def test_each_detail_tab_renders_and_an_administrator_sees_the_path_but_never_the_password(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seeded(container_host)

    tabs = [
        await admin_client.get(volumes_path(world, f"/data?tab={tab}"))
        for tab in ("overview", "containers", "access")
    ]
    nfs = await admin_client.get(volumes_path(world, "/nfs-vol?tab=overview"))
    traversal = await admin_client.get(volumes_path(world, "/..%2Fx"))
    missing = await admin_client.get(volumes_path(world, "/missing-vol"))

    assert [t.status_code for t in tabs] == [OK, OK, OK]
    assert SECRET not in nfs.text and "/var/lib/docker/volumes/nfs-vol/_data" in nfs.text
    assert traversal.status_code == NOT_FOUND and missing.status_code == NOT_FOUND


async def test_the_create_form_reports_refusals_and_a_created_volume_redirects_and_says_so(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seeded(container_host)
    base = volumes_path(world, "/-/new")

    blank = await admin_client.post(base, data=csrf_form(admin_client, name="", driver=""))
    taken = await admin_client.post(base, data=csrf_form(admin_client, name="seeded"))
    rows = await admin_client.post(base, data=csrf_form(admin_client, rows="label", name="x1"))
    created = await admin_client.post(
        base,
        data=csrf_form(
            admin_client,
            name="made",
            **{
                "option-0-key": "type",
                "option-0-value": "tmpfs",
                "label-0-key": "k",
                "label-0-value": "v",
            },
        ),
    )
    landed = await land(admin_client, created)

    assert blank.status_code == BAD_REQUEST and taken.status_code == CONFLICT
    assert rows.status_code == OK and "label-3-key" in rows.text
    assert location(created) == volumes_path(world, "/made")
    assert flash_texts(landed.text) == ["Volume created."]


async def test_a_create_while_the_name_is_held_shows_the_busy_banner_with_a_409(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def busy(self: VolumeService, *args: object, **kwargs: object) -> None:
        raise ObjectBusy(
            LockHolder(SubsystemActor("container-updater", 1), "container-updater", "create")
        )

    monkeypatch.setattr(VolumeService, "create", busy)

    response = await admin_client.post(
        volumes_path(world, "/-/new"), data=csrf_form(admin_client, name="madeit")
    )

    assert response.status_code == CONFLICT
    assert "This volume is busy" in response.text and "Wait for it to complete" in response.text


async def test_the_plain_remove_flow_binds_the_review_to_the_creation_time_and_the_use(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seeded(container_host)
    path = volumes_path(world, "/unused-one/remove")

    review = await admin_client.get(path)
    changed = await admin_client.post(
        path, data=csrf_form(admin_client, expected_created_at="x", expected_in_use="0")
    )
    incomplete = await admin_client.post(
        path,
        data=csrf_form(
            admin_client, expected_created_at=hidden_values(review, "expected_created_at")[0]
        ),
    )
    removed = await admin_client.post(
        path,
        data=csrf_form(
            admin_client,
            expected_created_at=hidden_values(review, "expected_created_at")[0],
            expected_in_use=hidden_values(review, "expected_in_use")[0],
        ),
    )

    landed = await land(admin_client, removed)

    assert "Remove volume" in review.text
    assert changed.status_code == CONFLICT and "Review again" in changed.text

    assert incomplete.status_code == BAD_REQUEST
    assert location(removed) == volumes_path(world)
    assert flash_texts(landed.text) == ["Volume removed."]
    assert "unused-one" not in container_host.volumes
    assert all("true" not in q.values() for _, q in container_host.volume_removals())


async def test_a_volume_in_use_offers_only_the_force_page_which_names_every_container(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seeded(container_host)

    first = await admin_client.get(volumes_path(world, "/seeded/remove"))
    force = await admin_client.get(volumes_path(world, "/seeded/remove?force=1"))
    plain_post = await admin_client.post(
        volumes_path(world, "/seeded/remove"),
        data=csrf_form(
            admin_client,
            expected_created_at=hidden_values(force, "expected_created_at")[0],
            expected_in_use="0",
        ),
    )

    assert "This volume is in use" in first.text and "Continue to force removal" in first.text
    assert "Remove volume</button>" not in first.text
    assert force.status_code == OK and USER in force.text and "job" in force.text
    assert len(hidden_values(force, "container")) == 2
    assert f"at most {MAX_FORCE_CONTAINERS} containers" in force.text
    assert plain_post.status_code == CONFLICT and "seeded" in container_host.volumes


async def test_a_running_container_blocks_the_force_page_and_the_force_post(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seeded(container_host)
    path = volumes_path(world, "/seeded/remove")
    page = await admin_client.get(f"{path}?force=1")
    data = csrf_form(
        admin_client,
        force="yes",
        expected_created_at=hidden_values(page, "expected_created_at")[0],
        expected_containers=hidden_values(page, "expected_containers")[0],
    ) | {"container": hidden_values(page, "container")}
    container_host.boxes[JOB].state = "running"

    blocked_page = await admin_client.get(f"{path}?force=1")
    refused = await admin_client.post(path, data=data)

    assert "Stop" in blocked_page.text and "This removal cannot go ahead" in blocked_page.text
    assert 'name="force"' not in blocked_page.text
    assert refused.status_code == CONFLICT and "stopped first" in refused.text
    assert len(container_host.boxes) >= 2 and "seeded" in container_host.volumes


async def test_the_force_post_removes_the_containers_then_the_volume_and_reports_each_step(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seeded(container_host)
    path = volumes_path(world, "/seeded/remove")
    page = await admin_client.get(f"{path}?force=1")
    data = csrf_form(
        admin_client,
        force="yes",
        expected_created_at=hidden_values(page, "expected_created_at")[0],
        expected_containers=hidden_values(page, "expected_containers")[0],
    ) | {"container": hidden_values(page, "container")}

    stale = await admin_client.post(path, data=data | {"expected_containers": "0" * 64})
    incomplete = await admin_client.post(
        path,
        data=csrf_form(admin_client, force="yes", expected_created_at="x", expected_containers="y"),
    )
    done = await admin_client.post(path, data=data)
    result = await land(admin_client, done)

    assert stale.status_code == CONFLICT and incomplete.status_code == CONFLICT
    assert location(done).startswith("/results/")
    assert "removed" in result.text.lower() and USER in result.text
    assert "seeded" not in container_host.volumes and USER_ID not in container_host.boxes
    assert all("true" not in q.values() for _, q in container_host.volume_removals())


async def test_the_force_page_for_an_unused_volume_goes_back_to_the_plain_page(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.add_volume("free")

    response = await admin_client.get(volumes_path(world, "/free/remove?force=1"))

    assert response.status_code == SEE_OTHER
    assert response.headers["location"].endswith("/volumes/free/remove")


async def test_the_prune_pages_name_the_list_per_mode_and_the_run_removes_exactly_it(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    anonymous = seeded(container_host)
    base = volumes_path(world, "/-/prune")

    choose = await admin_client.get(base)
    review = await admin_client.get(f"{base}?mode=anonymous")
    ran = await admin_client.post(
        base,
        data=csrf_form(admin_client, mode="anonymous")
        | {"target": hidden_values(review, "target")},
    )
    ran_page = await land(admin_client, ran)
    all_review = await admin_client.get(f"{base}?mode=all")
    bad_mode = await admin_client.get(f"{base}?mode=weird")
    empty = await admin_client.post(base, data=csrf_form(admin_client, mode="all"))

    assert choose.status_code == OK
    assert anonymous[:12] in choose.text and "nfs-vol" not in choose.text
    assert re.search(r'value="anonymous"[^>]*\schecked', choose.text)
    assert anonymous[:12] in review.text and "nfs-vol" not in review.text
    assert location(ran).startswith("/results/") and "removed" in ran_page.text
    assert anonymous not in container_host.volumes and "nfs-vol" in container_host.volumes
    assert "nfs-vol" in all_review.text and "no container mounts right now" in all_review.text
    assert bad_mode.status_code == BAD_REQUEST and empty.status_code == BAD_REQUEST


async def test_the_access_tab_saves_and_removes_a_control_and_the_environment_page_links_the_list(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seeded(container_host)
    base = volumes_path(world, "/data")

    saved = await admin_client.post(
        f"{base}/access", data=csrf_form(admin_client, visibility="public", access_level="read")
    )
    saved_page = await land(admin_client, saved)
    removed = await admin_client.post(
        f"{base}/access/delete",
        data=csrf_form(admin_client, expected_digest=digest_in(saved_page.text)),
    )
    removed_notices = await landed_texts(admin_client, removed)
    environment = await admin_client.get(f"/environments/{world.remote_id}")

    assert location(saved) == f"{base}?tab=access"
    assert flash_texts(saved_page.text) == ["Access control saved."]
    assert removed_notices == ["Access control removed."]
    sidebar_link = f'<a class="nav__link" href="/environments/{world.remote_id}/volumes">'
    assert sidebar_link in environment.text


def access_routes(world: World) -> PanelRoutes:
    base = volumes_path(world, "/data")
    return PanelRoutes(f"{base}?tab=access", f"{base}/access", f"{base}/access/delete")


async def test_the_volume_access_panel_grants_by_typed_name_and_a_later_save_keeps_every_grant(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seeded(container_host)

    granted, resaved = await grant_by_name_and_keep(admin_client, access_routes(world))

    assert_grants_kept(granted, resaved)


async def test_a_volume_save_under_a_stale_digest_is_a_conflict_that_shows_the_current_control(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seeded(container_host)

    saves = await save_under_stale_digest(admin_client, access_routes(world))

    await assert_stale_saves_refused(world.app, saves)


async def test_docker_text_on_a_refused_volume_is_for_administrators_only_and_recorded_nowhere(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    log_stream: io.StringIO,
) -> None:
    container_host.route(
        "POST",
        "/volumes/create",
        Reply(BAD_REQUEST, json.dumps({"message": ENGINE_TEXT}).encode()),
    )
    path = volumes_path(world, "/-/new")

    as_administrator = await admin_client.post(path, data=csrf_form(admin_client, name="refused"))
    as_standard_user = await standard_client.post(
        path, data=csrf_form(standard_client, name="refused")
    )

    assert as_administrator.status_code == as_standard_user.status_code
    assert "Docker said" in as_administrator.text and ENGINE_TEXT in as_administrator.text
    assert "Docker said" not in as_standard_user.text
    assert ENGINE_TEXT not in as_standard_user.text
    assert ENGINE_TEXT not in await recorded_text(world.app, log_stream)


async def test_an_unreachable_host_answers_a_script_request_for_a_volume_page_with_the_fragment(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.add_volume("data")
    container_host.route("GET", "/volumes/data", Refuse())
    container_host.route("GET", "/volumes", Refuse())

    detail = await admin_client.get(volumes_path(world, "/data"), headers=HTMX)
    listing = await admin_client.get(volumes_path(world), headers=HTMX)

    assert [r.status_code for r in (detail, listing)] == [UNAVAILABLE] * 2
    assert not any("<html" in r.text or "Traceback" in r.text for r in (detail, listing))


async def test_a_standard_user_sees_only_permitted_volumes_without_paths_or_driver_fields(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await share_with_standard_user(world, container_host)
    container_host.add_volume("admin-vol", options={"o": "bind", "device": "/etc"})
    container_host.add_volume("shop_data", labels={STACK_LABEL: "shop"})

    listing = await standard_client.get(volumes_path(world))
    hidden = await standard_client.get(volumes_path(world, "/admin-vol"))
    detail = await standard_client.get(volumes_path(world, "/shop_data?tab=overview"))
    form = await standard_client.get(volumes_path(world, "/-/new"))
    prune = await standard_client.get(volumes_path(world, "/-/prune"))

    text = visible_text(listing)
    assert "shop_data" in text and "admin-vol" not in text and "Prune volumes" not in text
    assert hidden.status_code == NOT_FOUND
    assert "/var/lib/docker/volumes" not in detail.text and "Not shown for your role" in detail.text
    assert 'name="driver"' not in form.text and 'name="option-0-key"' not in form.text
    assert prune.status_code == FORBIDDEN


async def test_a_standard_user_creates_a_plain_volume_and_a_volume_with_options_is_refused(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    bound = await standard_client.post(
        volumes_path(world, "/-/new"),
        data=csrf_form(
            standard_client,
            name="bound",
            driver="local",
            **{"option-0-key": "o", "option-0-value": "bind"},
        ),
    )
    plain = await standard_client.post(
        volumes_path(world, "/-/new"), data=csrf_form(standard_client, name="mine-vol")
    )
    listing = await standard_client.get(volumes_path(world))

    assert bound.status_code == BAD_REQUEST and "bound" not in container_host.volumes
    assert plain.status_code == SEE_OTHER and "mine-vol" in visible_text(listing)


async def test_a_standard_user_on_a_local_socket_gets_no_create_form(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    form = await standard_client.get(f"/environments/{world.local_id}/volumes/-/new")
    posted = await standard_client.post(
        f"/environments/{world.local_id}/volumes/-/new",
        data=csrf_form(standard_client, name="nope"),
    )

    assert form.status_code == FORBIDDEN and posted.status_code == FORBIDDEN
    assert container_host.volume_bodies() == []


async def test_the_container_create_form_wires_mount_sources_to_suggestions_of_the_visible_volumes(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
) -> None:
    await share_with_standard_user(world, container_host)
    container_host.add_volume("admin-vol", options={"o": "bind", "device": "/etc"})
    created = await standard_client.post(
        volumes_path(world, "/-/new"), data=csrf_form(standard_client, name="mine-vol")
    )
    assert created.status_code == SEE_OTHER
    path = f"/environments/{world.remote_id}/containers/new"

    standard = await standard_client.post(
        path, data=csrf_form(standard_client, rows="mounts", image="")
    )
    admin = await admin_client.post(path, data=csrf_form(admin_client, rows="mounts", image=""))
    suggest_href = f"/environments/{world.remote_id}/suggestions/volumes?field={MOUNT_SOURCE}"
    wrapped = [t.attrs["hx-get"] for t in parse_tags(standard.text) if "hx-get" in t.attrs]
    source = next(t for t in parse_tags(standard.text) if t.attrs.get("name") == MOUNT_SOURCE)
    offered = {
        who: await client.get(f"{suggest_href}&{MOUNT_SOURCE}={typed}")
        for who, client, typed in (
            ("standard mine", standard_client, "mine"),
            ("standard admin", standard_client, "admin"),
            ("administrator admin", admin_client, "admin"),
        )
    }

    assert suggest_href in wrapped
    assert source.attrs["list"] == f"suggest-{MOUNT_SOURCE}"
    assert f'<datalist id="suggest-{MOUNT_SOURCE}"></datalist>' in standard.text
    assert f'<datalist id="suggest-{MOUNT_SOURCE}"></datalist>' in admin.text
    assert {who: option_values(response) for who, response in offered.items()} == {
        "standard mine": ["mine-vol"],
        "standard admin": [],
        "administrator admin": ["admin-vol"],
    }


async def test_a_host_that_cannot_be_read_gives_an_error_state_not_a_traceback(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.route("GET", "/volumes", Refuse())

    page = await admin_client.get(volumes_path(world))
    fragment = await admin_client.get(volumes_path(world), headers=HTMX)
    down = await admin_client.get(f"/environments/{world.down_id}/volumes", headers=HTMX)

    assert page.status_code == UNAVAILABLE and "Traceback" not in page.text
    assert fragment.status_code == UNAVAILABLE and "state--error" in fragment.text
    assert down.status_code == UNAVAILABLE and "state--error" in down.text


async def test_a_volume_list_over_the_read_limit_is_refused_whole(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resources, "VOLUME_LIST_BYTES", 10)
    container_host.route("GET", "/volumes", Reply(200, b"[]", {"Content-Length": "999"}))

    page = await admin_client.get(volumes_path(world))

    assert page.status_code == UNAVAILABLE and "more volume data" in page.text


async def test_a_container_users_name_and_state_are_listed_on_the_volume_detail(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seeded(container_host)
    add_container(container_host, 0xD1, "extra", mounts=[volume_mount("seeded", "/x")])

    page = await admin_client.get(volumes_path(world, "/seeded?tab=containers"))

    assert all(name in visible_text(page) for name in (USER, "extra", "job"))


async def test_the_list_offers_import_even_when_the_volumes_cannot_be_read(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.route("GET", "/volumes", Refuse())

    listing = await admin_client.get(volumes_path(world))

    assert listing.status_code == UNAVAILABLE
    assert f'href="{volumes_path(world, "/-/import")}"' in listing.text
    assert "Prune volumes" not in listing.text
