"""The container pages: list, detail, confirmations, actions, batches and the counts fragment."""

import re
from html import unescape
from typing import Any

import pytest
from httpx import AsyncClient, Response

import app.docker_engine.resources as resources
from app.auth.principal import Principal
from app.services.activity import UserActor
from app.services.object_locks import LockHolder, ObjectKey
from tests.access_support import ADMIN, STANDARD, container_id
from tests.auth_support import csrf_form
from tests.container_support import (
    ABSENT,
    BIND_SOURCE,
    CACHE,
    DB,
    HIDDEN,
    JOB,
    SECRET_ARGUMENT,
    SECRET_ENV_VALUE,
    SECRET_LABEL_VALUE,
    URL_CREDENTIAL,
    WARM,
    WEB,
    WORKER,
    plant_control,
)
from tests.docker_engine.container_engine import ENGINE_TEXT, ContainerEngine
from tests.docker_engine.fake_engine import Refuse, Reply
from tests.web.access_panel_support import (
    CONFLICT,
    PanelRoutes,
    assert_grants_kept,
    assert_stale_saves_refused,
    digest_in,
    grant_by_name_and_keep,
    save_under_stale_digest,
)
from tests.web.environment_support import REMOTE_NAME, World, next_page_path
from tests.web.flash_support import flash_texts, land, landed_texts, location
from tests.web.helpers import hx_triggers

HTMX = {"HX-Request": "true"}
OK, BAD_REQUEST, FORBIDDEN, NOT_FOUND = 200, 400, 403, 404
BAD_GATEWAY, UNAVAILABLE = 502, 503
ROW = r'<tr id="container-{id}">(.*?)</tr>'
BADGE = re.compile(r'<span class="badge" data-tone="[a-z]+">([^<]+)</span>')
ROW_NAME = re.compile(r'<tr id="container-[0-9a-f]{64}">.*?<a href="[^"]+">([^<]+)</a>', re.S)
REQUEST_ID = re.compile(r"[0-9a-f]{32}")
ARIA_LABEL = re.compile(r'aria-label="([^"]+)"')
ALIAS = container_id(0xD1)
OVER_THE_CAP = {"Content-Length": "999"}
SECRETS = (SECRET_ENV_VALUE, SECRET_ARGUMENT, SECRET_LABEL_VALUE, URL_CREDENTIAL, BIND_SOURCE)
ADMIN_TABS = ["?tab=labels", "?tab=mounts"]


def containers(world: World, environment_id: int | None = None) -> str:
    return f"/environments/{environment_id or world.remote_id}/containers"


def row_of(html: str, container: str) -> str:
    found = re.search(ROW.format(id=container), html, re.S)
    assert found is not None, f"no row for {container}"
    return found.group(1)


def badges_of(html: str, container: str) -> list[str]:
    return BADGE.findall(row_of(html, container))


def names_in(html: str) -> list[str]:
    return [unescape(name) for name in ROW_NAME.findall(html)]


def stable(response: Response) -> str:
    """The body with the per-request reference removed, so two refusals can be compared."""
    return REQUEST_ID.sub("", response.text)


async def statuses(client: AsyncClient, paths: list[str], **options: Any) -> dict[str, int]:
    return {path: (await client.get(path, **options)).status_code for path in paths}


async def container_events(world: World) -> list[dict[str, Any]]:
    async with world.app.state.database.session() as db:
        rows = await db.fetch_limited(
            "SELECT action, result, actor_user_id, target_id, environment_id FROM activity_event "
            "WHERE action LIKE 'container_%' ORDER BY id",
            (),
            limit=100,
        )
    return [dict(row) for row in rows]


async def test_the_list_renders_each_state_with_its_badges_ports_stack_and_offered_actions(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.add(
        container_id(0xE1), "markup", image="<img src=x onerror=alert(1)>", labels={"k": "<b>"}
    )
    page = await admin_client.get(containers(world))
    container_host.health_listing_status = 500
    without_health = await admin_client.get(containers(world))

    assert page.status_code == OK
    assert "&lt;img src=x" in page.text and "<img src=x" not in page.text
    assert without_health.status_code == OK
    assert "Health could not be read from this host" in without_health.text
    assert badges_of(without_health.text, DB) == ["Running"]
    assert {id_: badges_of(page.text, id_) for id_ in (WEB, DB, CACHE, WORKER, JOB, WARM)} == {
        WEB: ["Running"],
        DB: ["Running", "Unhealthy"],
        CACHE: ["Paused"],
        WORKER: ["Restarting"],
        JOB: ["Exited"],
        WARM: ["Running", "Health starting"],
    }
    web = row_of(page.text, WEB)
    assert "8080-&gt;80/tcp" in web and "?stack=shop" in web
    assert 'aria-label="Stop web"' in web and 'aria-label="Start web"' not in web
    assert 'aria-label="Unpause cache"' in row_of(page.text, CACHE)
    assert 'aria-label="Start job"' in row_of(page.text, JOB)
    assert "7 containers." in page.text
    assert "engine-status-text" not in page.text


async def test_filters_paging_and_the_fragment_answer_from_the_server(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    first = await admin_client.get(f"{containers(world)}?limit=2")
    second = await admin_client.get(next_page_path(first.text) or "")
    third = await admin_client.get(next_page_path(second.text) or "")
    paused = await admin_client.get(f"{containers(world)}?state=paused", headers=HTMX)
    stopped = await admin_client.get(f"{containers(world)}?state=stopped&q=JO")
    invalid = await admin_client.get(f"{containers(world)}?state=bogus")

    assert [names_in(p.text) for p in (first, second, third)] == [
        ["cache", "db"],
        ["job", "warm"],
        ["web", "worker"],
    ]
    assert next_page_path(third.text) is None
    assert "<html" not in paused.text and names_in(paused.text) == ["cache"]
    assert "1 container matches these filters." in paused.text
    assert names_in(stopped.text) == ["job"]
    assert invalid.status_code == BAD_REQUEST and "Choose one of the listed states." in invalid.text
    assert len(container_host.calls("GET", "/containers/json")) == 5 * 3


async def test_an_unreachable_or_oversized_host_renders_an_error_state_without_internals(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_host.route("GET", "/containers/json", Refuse())
    down = await admin_client.get(containers(world))
    down_fragment = await admin_client.get(containers(world), headers=HTMX)
    monkeypatch.setattr(resources, "CONTAINER_LIST_BYTES", 10)
    container_host.route("GET", "/containers/json", Reply(200, b"[]", OVER_THE_CAP))
    large = await admin_client.get(containers(world))
    monkeypatch.setattr(resources, "CONTAINER_INSPECT_BYTES", 10)
    container_host.route("GET", f"/containers/{WEB}/json", Reply(200, b"{}", OVER_THE_CAP))
    heavy = await admin_client.get(f"{containers(world)}/{WEB}")

    assert heavy.status_code == UNAVAILABLE
    assert "too large to show" in heavy.text and "Traceback" not in heavy.text
    assert (down.status_code, down_fragment.status_code, large.status_code) == (
        UNAVAILABLE,
        UNAVAILABLE,
        UNAVAILABLE,
    )
    assert "Containers cannot be listed" in down.text
    assert "The Docker host could not be reached." in down.text
    assert "<html" not in down_fragment.text
    assert "limit 32 MiB" in large.text
    assert not any(
        leak in page.text
        for page in (down, down_fragment, large)
        for leak in ("Traceback", "ConnectError", "httpx", ENGINE_TEXT)
    )


def stop_path(world: World, reference: str) -> str:
    return f"{containers(world)}/{reference}/actions/stop"


def hidden_paths(world: World, references: list[str], suffixes: list[str]) -> list[str]:
    return [f"{containers(world)}/{ref}{suffix}" for ref in references for suffix in suffixes]


async def test_a_standard_user_lists_only_permitted_containers_and_all_others_are_alike_not_found(
    standard_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    shared_host.add(container_id(0xC1), ALIAS)
    listing = await standard_client.get(containers(world))
    unseen = hidden_paths(
        world,
        [WEB, CACHE, HIDDEN, ABSENT, "web", WEB[:12], WEB.upper(), ALIAS],
        ["", "?tab=labels", "?tab=environment", "/stats/stream", "/actions/stop"],
    )

    refused = await statuses(standard_client, unseen)
    hidden_page = await standard_client.get(f"{containers(world)}/{WEB}")
    absent_page = await standard_client.get(f"{containers(world)}/{ABSENT}")

    assert names_in(listing.text) == ["api", "db", "job"]
    assert 'aria-label="Stop db"' in row_of(listing.text, DB)
    assert ARIA_LABEL.findall(row_of(listing.text, JOB)) == ["Duplicate job"]
    assert set(refused.values()) == {NOT_FOUND}
    assert stable(hidden_page) == stable(absent_page)
    assert shared_host.mutations() == []


async def test_a_standard_user_changing_what_they_cannot_is_refused_with_no_effect_and_no_row(
    standard_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    shared_host.add(container_id(0xC1), ALIAS)
    form = csrf_form(standard_client)
    unseen = [WEB, CACHE, ABSENT, "web", ALIAS]

    unseen_codes = [
        (
            await standard_client.post(f"{containers(world)}/{ref}/actions/stop", data=form)
        ).status_code
        for ref in unseen
    ]
    read_only = await standard_client.post(stop_path(world, JOB), data=form)
    batch = await standard_client.post(
        f"{containers(world)}/batch",
        data=csrf_form(standard_client, action="stop", container=[JOB, WEB, CACHE]),
    )

    assert unseen_codes == [NOT_FOUND] * len(unseen)
    assert read_only.status_code == FORBIDDEN
    assert BADGE.findall((await land(standard_client, batch)).text) == [
        "Read only",
        "Not found",
        "Not found",
    ]
    assert shared_host.mutations() == []
    assert await container_events(world) == []


async def test_a_standard_user_never_sees_secrets_and_an_administrator_sees_them(
    standard_client: AsyncClient,
    admin_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
) -> None:
    box = shared_host.boxes[DB]
    box.labels.update(
        {
            "db_password": SECRET_LABEL_VALUE,
            "dsn": f"postgres://user:{URL_CREDENTIAL}@db/app",
            "service.api.token": SECRET_LABEL_VALUE,
        }
    )
    box.env, box.cmd = [f"DB_PASSWORD={SECRET_ENV_VALUE}"], ["serve", f"--token={SECRET_ARGUMENT}"]
    box.mounts = [{"Type": "bind", "Source": BIND_SOURCE, "Destination": "/srv", "RW": False}]
    detail = f"{containers(world)}/{DB}"
    tabs = ["", "?tab=labels", "?tab=mounts", "?tab=networks"]

    seen_by_user = [(await standard_client.get(detail + tab)).text for tab in tabs]
    seen_by_user.append((await standard_client.get(containers(world))).text)
    seen_by_admin = [
        (await admin_client.get(detail + tab)).text for tab in ["?tab=labels", "?tab=mounts"]
    ]
    admin_environment = await admin_client.get(f"{detail}?tab=environment")
    user_environment = await standard_client.get(f"{detail}?tab=environment")
    user_access = await standard_client.get(f"{detail}?tab=access")
    hidden_environment = await standard_client.get(f"{containers(world)}/{WEB}?tab=environment")

    everything = "".join(seen_by_user)
    for secret in (
        SECRET_ENV_VALUE,
        SECRET_ARGUMENT,
        SECRET_LABEL_VALUE,
        URL_CREDENTIAL,
        BIND_SOURCE,
    ):
        assert secret not in everything
    assert "Not shown for your role" in everything
    assert SECRET_LABEL_VALUE in seen_by_admin[0] and BIND_SOURCE in seen_by_admin[1]
    assert (
        "DB_PASSWORD" in admin_environment.text and SECRET_ENV_VALUE not in admin_environment.text
    )
    assert (user_environment.status_code, user_access.status_code) == (FORBIDDEN, FORBIDDEN)
    assert hidden_environment.status_code == NOT_FOUND


async def test_on_the_local_socket_a_standard_user_sees_containers_but_every_write_is_forbidden(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await plant_control(world.app, world.local_id, "container", DB, public=True, level="write")
    base = containers(world, world.local_id)

    listing = await standard_client.get(base)
    detail = await standard_client.get(f"{base}/{DB}")
    confirmation = await standard_client.get(f"{base}/{DB}/actions/stop")
    action = await standard_client.post(
        f"{base}/{DB}/actions/stop", data=csrf_form(standard_client)
    )
    batch = await standard_client.post(
        f"{base}/batch", data=csrf_form(standard_client, action="stop", container=[DB])
    )

    assert names_in(listing.text) == ["db"] and "/actions/" not in listing.text
    assert detail.status_code == OK and "Stop db" not in detail.text
    assert (confirmation.status_code, action.status_code) == (FORBIDDEN, FORBIDDEN)
    assert BADGE.findall((await land(standard_client, batch)).text) == ["Read only"]
    assert container_host.mutations() == []


async def test_a_confirmation_names_the_target_and_changes_nothing(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.volumes.update({"data": False, "scratch": True})
    container_host.boxes[WEB].mounts = [
        {"Type": "volume", "Name": "data", "Destination": "/data", "RW": True},
        {"Type": "volume", "Name": "scratch", "Destination": "/tmp", "RW": True},
    ]
    base = f"{containers(world)}"

    stop = await admin_client.get(f"{base}/{WEB}/actions/stop?view=detail", headers=HTMX)
    remove_running = await admin_client.get(f"{base}/{WEB}/actions/remove", headers=HTMX)
    remove_stopped = await admin_client.get(f"{base}/{JOB}/actions/remove", headers=HTMX)
    force_running = await admin_client.get(f"{base}/{WEB}/actions/remove?force=1", headers=HTMX)
    force_stopped = await admin_client.get(f"{base}/{JOB}/actions/remove?force=1", headers=HTMX)
    force_page = await admin_client.get(f"{base}/{WEB}/actions/remove?force=1")
    as_page = await admin_client.get(f"{base}/{WEB}/actions/kill")
    not_offered = await admin_client.get(f"{base}/{JOB}/actions/stop", headers=HTMX)
    unknown = await admin_client.get(f"{base}/{WEB}/actions/format")

    assert "<html" not in stop.text and 'name="view" value="detail"' in stop.text
    assert all(text in stop.text for text in ("web", WEB[:12], "nginx:1", REMOTE_NAME))
    assert 'name="expected_name"' not in stop.text
    assert 'name="expected_name" value="web"' in remove_running.text
    assert "named, always kept" in remove_running.text and "anonymous" in remove_running.text
    assert 'name="volumes"' not in remove_running.text and 'name="force"' not in remove_running.text
    assert "checked" not in remove_running.text
    assert "actions/remove?view=list&amp;force=1" in remove_running.text
    assert 'name="force"' not in remove_stopped.text
    assert all(text in force_running.text for text in ("web", WEB[:12], "nginx:1", REMOTE_NAME))
    assert 'name="confirm_force" value="yes"' in force_running.text
    assert 'name="volumes"' in force_running.text and 'name="force"' in force_running.text
    assert 'name="force"' not in force_stopped.text and 'name="volumes"' in force_stopped.text
    assert "<html" in force_page.text and 'name="confirm_force" value="yes"' in force_page.text
    assert "web" in force_page.text and WEB[:12] in force_page.text
    assert "<html" in as_page.text and "<form" in as_page.text and "Cancel" in as_page.text
    assert (not_offered.status_code, unknown.status_code) == (CONFLICT, NOT_FOUND)
    assert container_host.mutations() == []


async def test_an_action_answers_a_browser_with_a_result_page_and_a_script_with_the_changed_row(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    base = containers(world)

    stopped = await admin_client.post(
        f"{base}/{WEB}/actions/stop", data=csrf_form(admin_client), headers=HTMX
    )
    started = await admin_client.post(f"{base}/{JOB}/actions/start", data=csrf_form(admin_client))
    unchanged = await admin_client.post(
        f"{base}/{WARM}/actions/start", data=csrf_form(admin_client), headers=HTMX
    )
    removed = await admin_client.post(
        f"{base}/{CACHE}/actions/kill", data=csrf_form(admin_client), headers=HTMX
    )
    started_page = await land(admin_client, started)

    assert stopped.status_code == OK
    assert badges_of(stopped.text, WEB) == ["Exited"] and "Stopped web." in stopped.text
    assert hx_triggers(stopped) == {"container-changed", "counts-changed"}
    assert "Started job" in started_page.text and "<html" in started_page.text
    assert "warm was already in that state" in unchanged.text
    assert badges_of(removed.text, CACHE) == ["Exited"] and "Killed cache." in removed.text
    assert [(e["action"], e["result"], e["target_id"]) for e in await container_events(world)] == [
        ("container_stopped", "success", WEB),
        ("container_started", "success", JOB),
        ("container_started", "success", WARM),
        ("container_killed", "success", CACHE),
    ]


async def test_removing_names_the_container_and_a_changed_name_removes_nothing(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    base = containers(world)

    mismatch = await admin_client.post(
        f"{base}/{JOB}/actions/remove",
        data=csrf_form(admin_client, expected_name="renamed"),
        headers=HTMX,
    )
    missing_name = await admin_client.post(
        f"{base}/{JOB}/actions/remove", data=csrf_form(admin_client), headers=HTMX
    )
    removed = await admin_client.post(
        f"{base}/{JOB}/actions/remove",
        data=csrf_form(admin_client, expected_name="job", view="detail"),
        headers=HTMX,
    )

    assert (mismatch.status_code, missing_name.status_code) == (CONFLICT, CONFLICT)
    assert "changed since you reviewed it" in mismatch.text
    assert removed.status_code == OK and "Removed job." in removed.text
    assert JOB not in container_host.boxes
    delete = container_host.calls("DELETE", f"/containers/{JOB}")
    assert [(c.query["force"], c.query["v"]) for c in delete] == [("false", "false")]


async def test_each_failure_is_shown_with_its_own_status_and_text_and_never_the_engines(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    base = containers(world)
    form = csrf_form(admin_client)
    container_host.fail("stop", 500)
    container_host.fail("restart", 404)
    container_host.route("POST", f"/containers/{CACHE}/kill", Refuse())

    refused = await admin_client.post(f"{base}/{WEB}/actions/stop", data=form, headers=HTMX)
    gone = await admin_client.post(f"{base}/{WEB}/actions/restart", data=form, headers=HTMX)
    unreachable = await admin_client.post(f"{base}/{CACHE}/actions/kill", data=form, headers=HTMX)
    conflict = await admin_client.post(f"{base}/{JOB}/actions/pause", data=form, headers=HTMX)
    running = await admin_client.post(
        f"{base}/{WEB}/actions/remove", data=csrf_form(admin_client, expected_name="web")
    )

    assert [r.status_code for r in (refused, gone, unreachable, conflict, running)] == [
        BAD_GATEWAY,
        NOT_FOUND,
        UNAVAILABLE,
        CONFLICT,
        CONFLICT,
    ]
    assert "Docker refused the action." in refused.text
    assert "The container no longer exists." in gone.text
    assert "The Docker host could not be reached." in unreachable.text
    assert "The container is not running." in conflict.text
    assert "Stop it first, or choose to kill it first." in running.text
    assert "Review again" in running.text
    assert not any(ENGINE_TEXT in r.text for r in (refused, gone, unreachable, conflict, running))


async def stop_while_held(
    world: World, client: AsyncClient, holder: Principal, key: ObjectKey
) -> Response:
    locks = world.app.state.engine.locks
    async with locks.hold(key, LockHolder(UserActor(holder.user_id), holder.username, "stop")):
        return await client.post(
            f"{containers(world)}/{key.object_id}/actions/stop",
            data=csrf_form(client),
            headers=HTMX,
        )


async def test_a_busy_container_is_a_conflict_that_names_the_holder_by_the_viewers_standing(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
) -> None:
    key = ObjectKey(world.remote_id, "container", DB)

    admin_held_by_admin = await stop_while_held(world, admin_client, ADMIN, key)
    user_held_by_admin = await stop_while_held(world, standard_client, ADMIN, key)
    admin_held_by_user = await stop_while_held(world, admin_client, STANDARD, key)
    user_held_by_user = await stop_while_held(world, standard_client, STANDARD, key)

    assert {
        r.status_code
        for r in (admin_held_by_admin, user_held_by_admin, admin_held_by_user, user_held_by_user)
    } == {CONFLICT}
    assert "You started a stop" in admin_held_by_admin.text
    assert "Another user started a stop" in user_held_by_admin.text
    assert "admin-user" not in user_held_by_admin.text
    assert "standard-user started a stop" in admin_held_by_user.text
    assert "You started a stop" in user_held_by_user.text
    assert shared_host.mutations() == []


async def test_a_batch_names_every_target_runs_each_on_its_own_and_reports_each_result(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    base = containers(world)
    selection = [JOB, WEB, ABSENT]

    review = await admin_client.get(f"{base}/batch?action=start&container={JOB}&container={ABSENT}")
    ran = await admin_client.post(
        f"{base}/batch", data=csrf_form(admin_client, action="start", container=selection)
    )
    removal = await admin_client.get(
        f"{base}/batch?action=remove&container={CACHE}&container={WORKER}"
    )
    force_removal = await admin_client.get(
        f"{base}/batch?action=remove&container={CACHE}&container={WORKER}&force=1"
    )
    removed = await admin_client.post(
        f"{base}/batch",
        data=csrf_form(
            admin_client,
            action="remove",
            container=[CACHE, WORKER],
            expected_name=["cache", "other"],
            force="on",
            confirm_force="yes",
        ),
    )
    ran_page = await land(admin_client, ran)
    removed_page = await land(admin_client, removed)

    assert "job" in review.text and "1 of the selected container is left out" in review.text
    assert container_host.mutations()[0] == f"POST /containers/{JOB}/start"
    assert BADGE.findall(ran_page.text) == ["Done", "No change", "Not found"]
    assert 'name="expected_name" value="cache"' in removal.text
    assert 'name="force"' not in removal.text and 'name="confirm_force"' not in removal.text
    assert "Force remove these containers instead" in removal.text
    assert 'name="confirm_force" value="yes"' in force_removal.text
    assert 'name="force"' in force_removal.text and 'name="volumes"' in force_removal.text
    assert all(text in force_removal.text for text in ("cache", "worker", CACHE[:12], WORKER[:12]))
    assert BADGE.findall(removed_page.text) == ["Done", "Conflict"]
    assert (CACHE in container_host.boxes, WORKER in container_host.boxes) == (False, True)
    audited = [(e["target_id"], e["result"]) for e in await container_events(world)]
    assert audited == [
        (JOB, "success"),
        (WEB, "success"),
        (ABSENT, "failure"),
        (CACHE, "success"),
        (WORKER, "failure"),
    ]


async def test_a_batch_that_is_empty_unnamed_or_over_the_cap_is_refused_before_anything_runs(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    base = containers(world)
    too_many = [container_id(0x100 + n) for n in range(26)]

    empty = await admin_client.post(f"{base}/batch", data=csrf_form(admin_client, action="stop"))
    unnamed = await admin_client.post(
        f"{base}/batch", data=csrf_form(admin_client, action="bogus", container=[JOB])
    )
    over = await admin_client.post(
        f"{base}/batch", data=csrf_form(admin_client, action="stop", container=too_many)
    )
    unmatched = await admin_client.post(
        f"{base}/batch",
        data=csrf_form(admin_client, action="remove", container=[JOB], expected_name=[]),
    )

    assert [r.status_code for r in (empty, unnamed, over, unmatched)] == [BAD_REQUEST] * 4
    assert "Select at most 25 containers." in over.text
    assert container_host.requests == []


async def test_the_detail_page_serves_an_administrator_every_tab_and_edits_the_control(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    base = f"{containers(world)}/{WEB}"

    overview = await admin_client.get(base)
    stats = await admin_client.get(f"{base}?tab=stats")
    stopped_stats = await admin_client.get(f"{containers(world)}/{JOB}?tab=stats")
    access = await admin_client.get(f"{base}?tab=access")
    saved = await admin_client.post(
        f"{base}/access",
        data=csrf_form(admin_client, visibility="public", access_level="read"),
    )
    saved_page = await land(admin_client, saved)
    removed = await admin_client.post(
        f"{base}/access/delete",
        data=csrf_form(admin_client, expected_digest=digest_in(saved_page.text)),
    )
    removed_notices = await landed_texts(admin_client, removed)
    unknown_tab = await admin_client.get(f"{base}?tab=nonsense")

    assert "Privileged" in overview.text and "Yes" in overview.text
    assert "8080-&gt;80/tcp" in overview.text
    assert "data-stats-stream" in stats.text and "Processes" in stats.text
    assert "not running" in stopped_stats.text and "data-stats-stream" not in stopped_stats.text
    assert access.status_code == OK and "csrf_token" in access.text
    assert location(saved).endswith(f"{WEB}?tab=access")
    assert flash_texts(saved_page.text) == ["Access control saved."]
    assert removed_notices == ["Access control removed."]
    assert unknown_tab.status_code == NOT_FOUND


async def test_the_effective_access_view_is_for_administrators_and_explains_each_container(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
) -> None:
    base = f"{containers(world)}/effective-access"

    prompt = await admin_client.get(base)
    explained = await admin_client.get(f"{base}?username=standard-user")
    unknown = await admin_client.get(f"{base}?username=nobody")
    refused = await standard_client.get(f"{base}?username=standard-user")
    await plant_control(world.app, world.local_id, "container", DB, public=True, level="write")
    local = await admin_client.get(
        f"{containers(world, world.local_id)}/effective-access?username=standard-user"
    )

    assert prompt.status_code == OK and 'name="username"' in prompt.text
    assert explained.status_code == OK and "api" in explained.text and "wrong-key" in explained.text
    assert unknown.status_code == BAD_REQUEST and "No account has that username." in unknown.text
    assert refused.status_code == FORBIDDEN
    assert "where a standard user has read access at most" in local.text


async def test_every_container_post_without_a_csrf_token_is_refused_before_it_acts(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    base = containers(world)
    posts = [
        f"{base}/{JOB}/actions/start",
        f"{base}/batch",
        f"{base}/{WEB}/access",
        f"{base}/{WEB}/access/delete",
    ]

    refused = [
        (await admin_client.post(path, data={"action": "start"})).status_code for path in posts
    ]

    assert refused == [FORBIDDEN] * len(posts)
    assert container_host.mutations() == []
    assert await container_events(world) == []


SCRATCH = container_id(0x300)
ANONYMOUS_VOLUME = "0" * 63 + "a"
LATER_VOLUME = "0" * 63 + "b"
STALE_TOKEN = "0" * 32
VOLUME_TOKEN = re.compile(r'name="expected_volumes" value="([^"]+)"')


def scratch_with(host: ContainerEngine, *volumes: str) -> None:
    for volume in volumes:
        host.add_volume(volume, anonymous=True)
    host.add(
        SCRATCH,
        "scratch",
        image="busybox",
        state="exited",
        mounts=[
            {"Type": "volume", "Name": volume, "Destination": f"/v{n}", "RW": True}
            for n, volume in enumerate(volumes)
        ],
    )


async def test_a_batch_removal_that_also_deletes_volumes_is_bound_to_the_volumes_reviewed(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    base = containers(world)
    scratch_with(container_host, ANONYMOUS_VOLUME)
    review = await admin_client.get(f"{base}/batch?action=remove&container={SCRATCH}")
    [token] = VOLUME_TOKEN.findall(review.text)
    form = {
        "action": "remove",
        "container": [SCRATCH],
        "expected_name": ["scratch"],
        "volumes": "on",
        "confirm_force": "yes",
    }

    unbound = await admin_client.post(f"{base}/batch", data=csrf_form(admin_client, **form))
    stale = await admin_client.post(
        f"{base}/batch", data=csrf_form(admin_client, **form, expected_volumes=[STALE_TOKEN])
    )
    container_host.add_volume(LATER_VOLUME, anonymous=True)
    container_host.boxes[SCRATCH].mounts.append(
        {"Type": "volume", "Name": LATER_VOLUME, "Destination": "/later", "RW": True}
    )
    changed = await admin_client.post(
        f"{base}/batch", data=csrf_form(admin_client, **form, expected_volumes=[token])
    )
    untouched = (SCRATCH in container_host.boxes, set(container_host.volumes))
    fresh = VOLUME_TOKEN.findall(
        (await admin_client.get(f"{base}/batch?action=remove&container={SCRATCH}")).text
    )
    removed = await admin_client.post(
        f"{base}/batch", data=csrf_form(admin_client, **form, expected_volumes=fresh)
    )
    removed_page = await land(admin_client, removed)

    assert "anonymous" in review.text and ANONYMOUS_VOLUME in review.text
    assert [r.status_code for r in (unbound, stale, changed)] == [BAD_REQUEST] * 3
    assert "confirmation was incomplete" in unbound.text
    assert "volumes of a selected container changed" in stale.text + changed.text
    assert untouched == (True, {ANONYMOUS_VOLUME, LATER_VOLUME})
    assert fresh != [token]
    assert BADGE.findall(removed_page.text) == ["Done"]
    assert SCRATCH not in container_host.boxes
    deletes = [r for r in container_host.requests if r.method == "DELETE" and SCRATCH in r.path]
    assert [r.query["v"] for r in deletes] == ["true"]


async def test_a_batch_removal_that_keeps_volumes_needs_no_volume_binding(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    base = containers(world)
    scratch_with(container_host, ANONYMOUS_VOLUME)

    removed = await admin_client.post(
        f"{base}/batch",
        data=csrf_form(
            admin_client, action="remove", container=[SCRATCH], expected_name=["scratch"]
        ),
    )
    removed_page = await land(admin_client, removed)

    assert BADGE.findall(removed_page.text) == ["Done"]
    assert SCRATCH not in container_host.boxes
    deletes = [r for r in container_host.requests if r.method == "DELETE" and SCRATCH in r.path]
    assert [r.query["v"] for r in deletes] == ["false"]


async def test_changing_access_while_the_host_cannot_be_read_is_a_503_page_not_a_server_error(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    base = f"{containers(world)}/{WEB}"
    container_host.route("GET", "/containers/json", Refuse())
    container_host.route("GET", f"/containers/{WEB}/json", Refuse())

    saved = await admin_client.post(
        f"{base}/access", data=csrf_form(admin_client, visibility="public", access_level="read")
    )
    removed = await admin_client.post(f"{base}/access/delete", data=csrf_form(admin_client))
    fragment = await admin_client.post(
        f"{base}/access/delete", data=csrf_form(admin_client), headers=HTMX
    )

    assert [r.status_code for r in (saved, removed, fragment)] == [UNAVAILABLE] * 3
    assert "<html" not in fragment.text
    assert not any("Traceback" in r.text for r in (saved, removed, fragment))


async def test_a_failed_action_asked_for_by_a_fragment_request_answers_the_notice_alone(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    page = await admin_client.get(f"{containers(world)}/{ABSENT}/actions/stop")
    fragment = await admin_client.get(f"{containers(world)}/{ABSENT}/actions/stop", headers=HTMX)

    assert page.status_code == fragment.status_code == NOT_FOUND
    assert "<html" in page.text and "<html" not in fragment.text


async def test_a_removal_that_forces_or_deletes_volumes_needs_the_marker_in_every_posted_form(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    base = containers(world)
    path = f"{base}/{JOB}/actions/remove"
    batch_form = {"action": "remove", "container": [JOB], "expected_name": ["job"]}

    plain_force = await admin_client.post(
        path, data=csrf_form(admin_client, expected_name="job", force="on")
    )
    plain_volumes = await admin_client.post(
        path, data=csrf_form(admin_client, expected_name="job", volumes="on")
    )
    script_force = await admin_client.post(
        path, data=csrf_form(admin_client, expected_name="job", force="on"), headers=HTMX
    )
    batch_force = await admin_client.post(
        f"{base}/batch", data=csrf_form(admin_client, **batch_form, force="on")
    )
    batch_volumes = await admin_client.post(
        f"{base}/batch", data=csrf_form(admin_client, **batch_form, volumes="on")
    )

    assert [
        r.status_code
        for r in (plain_force, plain_volumes, script_force, batch_force, batch_volumes)
    ] == [BAD_REQUEST] * 5
    assert "<html" in plain_force.text and "<html" not in script_force.text
    assert JOB in container_host.boxes
    assert container_host.mutations() == []
    assert await container_events(world) == []


NAMED_ITEM = re.compile(r'<span class="cell-wrap mono">([^<]+)</span>')


async def test_a_batch_names_each_container_it_stopped_restarted_or_killed_from_the_reviewed_names(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    base = containers(world)

    stopped = await admin_client.post(
        f"{base}/batch",
        data=csrf_form(
            admin_client,
            action="stop",
            container=[WEB, ABSENT],
            display_name=["web", "gone"],
        ),
    )
    restarted = await admin_client.post(
        f"{base}/batch",
        data=csrf_form(admin_client, action="restart", container=[DB], display_name=["db"]),
    )
    killed = await admin_client.post(
        f"{base}/batch",
        data=csrf_form(admin_client, action="kill", container=[CACHE], display_name=["cache"]),
    )
    pages = [await land(admin_client, r) for r in (stopped, restarted, killed)]

    assert [NAMED_ITEM.findall(page.text) for page in pages] == [
        ["web", "gone"],
        ["db"],
        ["cache"],
    ]


async def test_an_unreachable_host_answers_a_script_request_for_a_container_page_with_the_fragment(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.route("GET", f"/containers/{WEB}/json", Refuse())
    container_host.route("GET", "/containers/json", Refuse())
    base = f"{containers(world)}/{WEB}"

    detail = await admin_client.get(base, headers=HTMX)
    tab = await admin_client.get(f"{base}?tab=labels", headers=HTMX)
    effective = await admin_client.get(
        f"{containers(world)}/effective-access?username=standard-user", headers=HTMX
    )

    assert [r.status_code for r in (detail, tab, effective)] == [UNAVAILABLE] * 3
    assert not any("<html" in r.text or "Traceback" in r.text for r in (detail, tab, effective))
    assert "The Docker host could not be reached." in detail.text


def access_routes(world: World, container: str = WEB) -> PanelRoutes:
    base = f"{containers(world)}/{container}"
    return PanelRoutes(f"{base}?tab=access", f"{base}/access", f"{base}/access/delete")


async def test_the_access_panel_grants_by_typed_name_and_a_later_save_keeps_every_grant(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    routes = access_routes(world)

    granted, resaved = await grant_by_name_and_keep(admin_client, routes)

    assert_grants_kept(granted, resaved)
    assert flash_texts(resaved.text) == ["Access control saved."]


async def test_a_save_under_a_stale_digest_is_a_conflict_that_shows_the_current_control(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    saves = await save_under_stale_digest(admin_client, access_routes(world))

    await assert_stale_saves_refused(world.app, saves)
