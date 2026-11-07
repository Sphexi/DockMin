"""Creating, editing, testing, granting and deleting environments through the forms."""

import re
from datetime import datetime, timedelta
from typing import Any

from httpx import AsyncClient, Response

from app.docker_engine.probe import Stage
from tests.auth_support import ADMIN_ID, STANDARD_ID, Browsers, csrf_form, read_events
from tests.docker_engine.tls_material import (
    Material,
    cert_pem,
    key_of,
    key_pem,
    make_ca,
    make_leaf,
)
from tests.environment_support import failed_result
from tests.web.environment_support import (
    LOCAL_NAME,
    LOCAL_SOCKET,
    REMOTE_ENDPOINT,
    REMOTE_NAME,
    World,
    filled,
    hold_unreachable,
    listed_names,
    local_form,
    remote_form,
    rows,
)
from tests.web.flash_support import flashes_in, land, landed_texts, location

OK = 200
SEE_OTHER = 303
WARN = "warn"
FOLDED_SUFFIX = " and 1 more."
EXPIRY_SOON = timedelta(days=10)
RESULT_PATH = "/results/"
REFRESH_NOTICE = "Refresh requested. The counts update shortly."
UNPROCESSABLE = 422
NOT_FOUND = 404
HTMX = {"HX-Request": "true"}
LOCATION = re.compile(r"/environments/(\d+)")


def created_id(response: Response) -> int:
    found = LOCATION.fullmatch(response.headers["location"])
    assert found is not None
    return int(found.group(1))


async def test_the_preview_shows_the_endpoint_to_be_saved_and_refuses_an_address_in_the_host_field(
    admin_client: AsyncClient, world: World
) -> None:
    remote = await admin_client.post(
        "/environments/preview",
        headers=HTMX,
        data=csrf_form(admin_client, kind="tcp_tls", host="Example.COM", port="2376"),
    )
    local = await admin_client.post(
        "/environments/preview",
        headers=HTMX,
        data=csrf_form(admin_client, kind="unix_socket", socket_path=LOCAL_SOCKET),
    )
    smuggled = await admin_client.post(
        "/environments/preview",
        headers=HTMX,
        data=csrf_form(admin_client, kind="tcp_tls", host="unix://host/var/run/docker.sock"),
    )

    assert remote.status_code == local.status_code == OK
    assert 'value="tcp://example.com:2376"' in remote.text
    assert f'value="unix://{LOCAL_SOCKET}"' in local.text
    assert "host this product runs on" in local.text
    assert smuggled.status_code == UNPROCESSABLE
    assert "local socket kind" in smuggled.text
    assert world.probes.calls == []


async def test_creating_a_local_and_a_remote_environment_tests_each_saves_it_and_records_who_did(
    admin_client: AsyncClient, world: World, browsers: Browsers
) -> None:
    local = await admin_client.post("/environments", data=local_form(admin_client))
    remote = await admin_client.post(
        "/environments", data=remote_form(admin_client, world.material)
    )

    created = [created_id(response) for response in (local, remote)]
    assert (local.status_code, remote.status_code) == (SEE_OTHER, SEE_OTHER)
    assert len(world.probes.calls) == 2
    assert world.poker.poked == created
    events = [e for e in await read_events_of(world) if e["action"] == "environment_created"]
    assert [(e["actor_user_id"], e["result"], e["target_id"]) for e in events] == [
        (ADMIN_ID, "success", str(identifier)) for identifier in created
    ]
    page = (await admin_client.get("/")).text
    assert {"new-box", "new-remote"} <= set(listed_names(page))


def warning_material(now: datetime) -> Material:
    """Return material that draws three warnings: an expiring CA, an expiring leaf, a stranger."""
    expiring = now + EXPIRY_SOON
    ca = make_ca(key_of("p256"), not_after=expiring)
    other_key = key_of("p256", 1)
    leaf = make_leaf(other_key, make_ca(other_key), key_of("p256", 2), not_after=expiring)
    return Material(ca=cert_pem(ca), cert=cert_pem(leaf), key=key_pem(key_of("p256", 2)))


async def test_tls_warnings_on_a_create_and_on_a_save_fold_into_one_warn_notice_each(
    admin_client: AsyncClient, world: World
) -> None:
    material = warning_material(world.app.state.auth.clock())
    created = await admin_client.post("/environments", data=remote_form(admin_client, material))
    on_create = flashes_in((await land(admin_client, created)).text)
    saved = await admin_client.post(
        f"/environments/{created_id(created)}/edit", data=remote_form(admin_client, material)
    )
    on_save = flashes_in((await land(admin_client, saved)).text)

    assert [level for level, _ in on_create] == [WARN]
    assert [level for level, _ in on_save] == [WARN]
    assert on_create[0][1].startswith("The CA certificate expires on")
    assert on_create[0][1].endswith(FOLDED_SUFFIX)
    assert on_save[0][1] == on_create[0][1]


async def test_clean_tls_material_on_a_create_leaves_no_notice(
    admin_client: AsyncClient, world: World
) -> None:
    created = await admin_client.post(
        "/environments", data=remote_form(admin_client, world.material)
    )

    assert flashes_in((await land(admin_client, created)).text) == []


async def read_events_of(world: World) -> list[dict[str, Any]]:
    async with world.app.state.database.session() as db:
        return await read_events(db)


async def test_a_form_the_service_refuses_is_shown_again_with_the_reason_and_nothing_is_saved(
    admin_client: AsyncClient, world: World
) -> None:
    forms = {
        "unreviewed endpoint": local_form(admin_client, reviewed_endpoint="unix:///elsewhere.sock"),
        "no name": local_form(admin_client, name=""),
        "taken name": local_form(admin_client, name=LOCAL_NAME.upper()),
        "taken endpoint": local_form(
            admin_client, socket_path=LOCAL_SOCKET, reviewed_endpoint=f"unix://{LOCAL_SOCKET}"
        ),
        "plain tcp": remote_form(
            admin_client, world.material, ca_certificate="", client_certificate="", client_key=""
        ),
    }

    responses = {
        name: await admin_client.post("/environments", data=form) for name, form in forms.items()
    }

    assert {name: r.status_code for name, r in responses.items()} == dict.fromkeys(
        forms, UNPROCESSABLE
    )
    assert "Review the endpoint" in responses["unreviewed endpoint"].text
    assert "already uses this endpoint" in responses["taken endpoint"].text
    assert "plain TCP" in responses["plain tcp"].text
    assert len(await rows(world, "SELECT id FROM environment")) == 3
    assert world.probes.calls == []


async def test_a_failed_connection_test_names_its_stage_and_the_save_can_be_accepted_anyway(
    admin_client: AsyncClient, world: World
) -> None:
    world.probes.result = failed_result(Stage.TCP, "The connection was refused.")

    refused = await admin_client.post("/environments", data=local_form(admin_client))
    accepted = await admin_client.post(
        "/environments", data=local_form(admin_client, save_anyway="on")
    )

    saved = await rows(world, "SELECT status, failed_stage FROM environment WHERE name = 'new-box'")
    assert refused.status_code == UNPROCESSABLE
    assert "TCP connection" in refused.text and "refused" in refused.text
    assert accepted.status_code == SEE_OTHER
    assert saved == [{"status": "unreachable", "failed_stage": "tcp"}]


async def test_testing_a_saved_connection_stores_its_status_and_a_refresh_only_pokes_the_refresher(
    admin_client: AsyncClient, world: World
) -> None:
    world.probes.result = failed_result(Stage.SOCKET, "Nothing exists at this path.")

    fragment = await admin_client.post(
        f"/environments/{world.local_id}/test", headers=HTMX, data=csrf_form(admin_client)
    )
    refresh = await admin_client.post(
        f"/environments/{world.remote_id}/refresh", data=csrf_form(admin_client)
    )

    built_by_the_posts = list(world.engine_clients)
    await hold_unreachable(world, world.remote_id)
    notices = await landed_texts(admin_client, refresh)
    stored = await rows(
        world, "SELECT status, failed_stage FROM environment WHERE id = ?", (world.local_id,)
    )
    assert fragment.status_code == OK
    assert "<html" not in fragment.text
    assert "Nothing exists at this path." in fragment.text and "Failed" in fragment.text
    assert stored == [{"status": "unreachable", "failed_stage": "socket"}]
    assert location(refresh) == f"/environments/{world.remote_id}"
    assert notices == [REFRESH_NOTICE]
    assert world.poker.poked == [world.local_id, world.remote_id]
    assert len(world.probes.calls) == 1
    assert built_by_the_posts == []


async def test_a_rename_with_a_group_and_tag_change_saves_without_reaching_the_host(
    admin_client: AsyncClient, world: World
) -> None:
    response = await admin_client.post(
        f"/environments/{world.remote_id}/edit",
        data=csrf_form(
            admin_client,
            name="renamed-remote",
            kind="tcp_tls",
            host="remote.example.test",
            port="2376",
            reviewed_endpoint=REMOTE_ENDPOINT,
            group=str(world.group_id),
            tag=str(world.tag_id),
        ),
    )

    saved = await rows(
        world,
        "SELECT name, environment_group_id AS group_id FROM environment WHERE id = ?",
        (world.remote_id,),
    )
    tagged = await rows(
        world, "SELECT tag_id FROM environment_tag WHERE environment_id = ?", (world.remote_id,)
    )
    assert response.status_code == SEE_OTHER
    assert saved == [{"name": "renamed-remote", "group_id": world.group_id}]
    assert tagged == [{"tag_id": world.tag_id}]
    assert world.probes.calls == []


async def test_granting_and_revoking_access_takes_effect_on_the_users_next_request(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World
) -> None:
    await hold_unreachable(world, world.remote_id)
    before = await standard_client.get(f"/environments/{world.remote_id}")
    await admin_client.post(
        f"/environments/{world.remote_id}/access",
        data=csrf_form(admin_client, grantee_kind="user", grantee_name="standard-user"),
    )
    granted = await standard_client.get(f"/environments/{world.remote_id}")
    await admin_client.post(
        f"/environments/{world.remote_id}/access/remove?user_id={STANDARD_ID}",
        data=csrf_form(admin_client),
    )
    after = await standard_client.get(f"/environments/{world.remote_id}")

    assert (before.status_code, granted.status_code, after.status_code) == (
        NOT_FOUND,
        OK,
        NOT_FOUND,
    )


async def test_a_grant_to_an_administrator_an_unknown_user_or_a_repeat_is_refused_with_a_reason(
    admin_client: AsyncClient, world: World
) -> None:
    await hold_unreachable(world, world.local_id)
    target = f"/environments/{world.local_id}/access"
    forms = {
        "administrator": {"grantee_kind": "user", "grantee_name": "admin-user"},
        "unknown": {"grantee_kind": "user", "grantee_name": "nobody"},
        "repeat": {"grantee_kind": "user", "grantee_name": "standard-user"},
        "no kind": {"grantee_kind": "robot", "grantee_name": "x"},
    }

    responses = {
        name: await admin_client.post(target, data=csrf_form(admin_client, **form))
        for name, form in forms.items()
    }

    assert {name: r.status_code for name, r in responses.items()} == dict.fromkeys(
        forms, UNPROCESSABLE
    )
    assert "need no grant" in responses["administrator"].text
    assert "already exists" in responses["repeat"].text


async def test_the_delete_confirmation_names_what_is_lost_and_deletes_nothing(
    admin_client: AsyncClient, world: World
) -> None:
    response = await admin_client.get(
        f"/environments/delete?id={world.local_id}&id={world.remote_id}&id=9999"
    )

    page = response.text
    assert response.status_code == OK
    assert LOCAL_NAME in page and REMOTE_NAME in page
    assert "Who loses access" in page and "Records deleted with it" in page
    assert "Standard users affected" in page
    assert "Snapshot" in page and "TLS material" in page
    assert "Activity history is kept" in page
    assert "Nothing is changed on the host" in page
    assert "does not exist" in page
    assert 'name="expected_name"' in page
    assert len(await rows(world, "SELECT id FROM environment")) == 3


async def test_a_confirmed_delete_removes_each_environment_with_its_records_and_history_detached(
    admin_client: AsyncClient, world: World
) -> None:
    await admin_client.post(f"/environments/{world.remote_id}/test", data=csrf_form(admin_client))

    response = await admin_client.post(
        "/environments/delete",
        data=csrf_form(admin_client)
        | {
            "id": [str(world.remote_id), str(world.local_id)],
            "expected_name": [REMOTE_NAME, LOCAL_NAME],
        },
    )

    result = await land(admin_client, response)
    leftovers = await rows(world, "SELECT id FROM environment")
    tls = await rows(
        world,
        "SELECT id FROM environment_tls_material WHERE environment_id = ?",
        (world.remote_id,),
    )
    events = await rows(world, "SELECT environment_id FROM activity_event")
    assert (response.status_code, location(response).startswith(RESULT_PATH)) == (SEE_OTHER, True)
    assert result.text.count("Deleted") >= 2
    assert [row["id"] for row in leftovers] == [world.down_id]
    assert tls == []
    assert {row["environment_id"] for row in events} == {None}
    after = await admin_client.get(f"/environments/{world.remote_id}")
    assert after.status_code == NOT_FOUND


async def test_a_delete_that_is_stale_incomplete_or_over_fifty_deletes_nothing(
    admin_client: AsyncClient, world: World
) -> None:
    too_many = [str(number) for number in range(1000, 1051)]
    stale = await admin_client.post(
        "/environments/delete",
        data=csrf_form(admin_client)
        | {"id": [str(world.down_id)], "expected_name": ["a different name"]},
    )
    incomplete = await admin_client.post(
        "/environments/delete", data=csrf_form(admin_client) | {"id": [str(world.down_id)]}
    )
    over = await admin_client.post(
        "/environments/delete",
        data=csrf_form(admin_client) | {"id": too_many, "expected_name": too_many},
    )

    assert (stale.status_code, location(stale).startswith(RESULT_PATH)) == (SEE_OTHER, True)
    assert "Changed" in (await land(admin_client, stale)).text
    assert (incomplete.status_code, over.status_code) == (UNPROCESSABLE, UNPROCESSABLE)
    assert "incomplete" in incomplete.text and "at most 50" in over.text
    assert len(await rows(world, "SELECT id FROM environment")) == 3


MIN_POST_ROUTES = 16
PREFIXES = ("/environments", "/groups", "/tags")
FOREIGN_ORIGIN = "https://evil.example"
# Checks its own token inside the streamed body.
STREAMED_UPLOAD = "/contexts/-/upload"


def post_paths(world: World) -> list[str]:
    documented = world.app.openapi()["paths"]
    ids = {"environment_id": world.local_id, "group_id": world.group_id, "tag_id": world.tag_id}
    return [
        filled(path, **ids)
        for path, operations in documented.items()
        if "post" in operations and path.startswith(PREFIXES) and not path.endswith(STREAMED_UPLOAD)
    ]


async def test_every_post_route_refuses_a_missing_token_or_foreign_origin_and_changes_nothing(
    admin_client: AsyncClient, world: World
) -> None:
    paths = post_paths(world)
    before = await rows(
        world,
        "SELECT (SELECT count(*) FROM environment) AS environments, "
        "(SELECT count(*) FROM environment_group) AS groups, (SELECT count(*) FROM tag) AS tags, "
        "(SELECT count(*) FROM environment_user_access) AS grants, "
        "(SELECT count(*) FROM activity_event) AS events",
    )

    no_token = [(await admin_client.post(path, data={})).status_code for path in paths]
    foreign = [
        (
            await admin_client.post(
                path, data=csrf_form(admin_client), headers={"Origin": FOREIGN_ORIGIN}
            )
        ).status_code
        for path in paths
    ]

    assert len(paths) >= MIN_POST_ROUTES
    assert set(no_token) == {403}
    assert set(foreign) == {403}
    assert world.probes.calls == [] and world.poker.poked == []
    assert before == await rows(
        world,
        "SELECT (SELECT count(*) FROM environment) AS environments, "
        "(SELECT count(*) FROM environment_group) AS groups, (SELECT count(*) FROM tag) AS tags, "
        "(SELECT count(*) FROM environment_user_access) AS grants, "
        "(SELECT count(*) FROM activity_event) AS events",
    )
