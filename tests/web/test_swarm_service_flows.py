"""Service flows through the pages: create, review, update, scale, force, rollback, remove, logs."""

import io
import re
from html import unescape

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.services.swarm_service_queries import SwarmServiceQueries
from app.services.swarm_service_views import ServiceInvalid
from app.services.swarm_services import SwarmServiceActions
from tests.auth_support import csrf_form
from tests.container_support import plant_control
from tests.docker_engine.swarm_engine import SwarmEngine, plain_spec
from tests.object_support import HTMX, rows_of
from tests.swarm_support import (
    EMPTY_AUTH,
    NAMESPACE_LABEL,
    field_values,
    form_action,
    hidden_fields,
    without_token,
)
from tests.web.access_panel_support import digest_in
from tests.web.engine_detail_support import recorded_text
from tests.web.environment_support import World
from tests.web.flash_support import flash_texts, land, landed_texts, location
from tests.web.image_support import make_set

OK, SEE_OTHER, BAD, FORBIDDEN, NOT_FOUND, CONFLICT = 200, 303, 400, 403, 404, 409
REGISTRY_IMAGE = "registry.example.com/app:1"
ENGINE_TEXT = "engine detail"
CREATE = {
    "name": "web",
    "image": "busybox:latest",
    "mode": "replicated",
    "replicas": "2",
    "command": "sleep 100",
}


def base_of(world: World) -> str:
    return f"/environments/{world.remote_id}/swarm"


def service_id_of(location: str) -> str:
    return location.split("/services/")[1].split("?")[0]


async def create_through_the_form(client: AsyncClient, world: World, **fields: str) -> str:
    response = await client.post(
        f"{base_of(world)}/services/new", data=csrf_form(client, **{**CREATE, **fields})
    )
    assert response.status_code == SEE_OTHER
    return service_id_of(response.headers["location"])


async def reviewed_edit(
    client: AsyncClient, world: World, service_id: str, **changes: str
) -> tuple[AsyncClient, str, dict[str, str]]:
    base = f"{base_of(world)}/services/{service_id}"
    edit = await client.get(f"{base}/edit")
    values = {
        **field_values(edit),
        "mode": "replicated",
        "form_version": hidden_fields(edit)["form_version"],
        **changes,
    }
    review = await client.post(f"{base}/edit", data=csrf_form(client, **values))
    assert review.status_code == OK
    return client, form_action(review), hidden_fields(review)


async def test_creating_a_service_through_the_form_redirects_to_its_detail_and_pins_the_image(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    form = csrf_form(
        admin_client,
        **CREATE,
        **{
            "env-0-key": "MODE",
            "env-0-value": "blue",
            "port-0-target": "80",
            "port-0-published": "8080",
            "port-0-protocol": "tcp",
            "port-0-mode": "ingress",
            "constraints": "node.role==worker\nnode.role!=manager",
        },
    )

    created = await admin_client.post(f"{base_of(world)}/services/new", data=form)
    detail = await land(admin_client, created)

    spec = swarm_host.services[service_id_of(location(created))]["Spec"]
    assert created.status_code == SEE_OTHER and "?" not in location(created)
    assert detail.status_code == OK and flash_texts(detail.text) == ["Created."]
    assert spec["TaskTemplate"]["ContainerSpec"]["Image"].endswith(swarm_host.digest)
    assert spec["TaskTemplate"]["Placement"]["Constraints"] == [
        "node.role==worker",
        "node.role!=manager",
    ]


async def test_docker_text_on_a_refused_service_is_for_administrators_only_and_recorded_nowhere(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
    log_stream: io.StringIO,
) -> None:
    swarm_host.create_status = BAD
    url = f"{base_of(world)}/services/new"

    as_administrator = await admin_client.post(url, data=csrf_form(admin_client, **CREATE))
    as_standard_user = await standard_client.post(url, data=csrf_form(standard_client, **CREATE))

    assert as_administrator.status_code == as_standard_user.status_code
    assert "Docker said" in as_administrator.text and ENGINE_TEXT in as_administrator.text
    assert "Docker said" not in as_standard_user.text
    assert ENGINE_TEXT not in as_standard_user.text
    assert ENGINE_TEXT not in await recorded_text(world.app, log_stream)


async def test_a_duplicate_name_a_bad_form_and_added_rows_re_render_the_form(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    url = f"{base_of(world)}/services/new"
    await create_through_the_form(admin_client, world)

    duplicate = await admin_client.post(url, data=csrf_form(admin_client, **CREATE))
    invalid = await admin_client.post(
        url, data=csrf_form(admin_client, name="x y", image="", replicas="x")
    )
    more_rows = await admin_client.post(
        url, data=csrf_form(admin_client, name="q", image="a", rows="env")
    )

    assert duplicate.status_code == CONFLICT and "already exists" in duplicate.text
    assert invalid.status_code == BAD and "Check the form" in invalid.text
    assert more_rows.status_code == OK and more_rows.text.count('name="env-') >= 6
    assert len(swarm_host.services) == 1


async def test_a_credential_choice_is_asked_for_then_used_and_the_new_image_asks_again(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    alpha = await make_set(admin_client, world, "alpha")
    beta = await make_set(admin_client, world, "beta")
    form = csrf_form(admin_client, **{**CREATE, "name": "reg", "image": REGISTRY_IMAGE})
    url = f"{base_of(world)}/services/new"

    asked = await admin_client.post(url, data=form)
    done = await admin_client.post(url, data={**form, "credential_choice": str(beta)})
    service_id = service_id_of(done.headers["location"])
    swarm_host.auth_headers.clear()
    _, confirm, hidden = await reviewed_edit(
        admin_client,
        world,
        service_id,
        image="registry.example.com/app:2",
        credential_choice=str(alpha),
    )
    confirmed = await admin_client.post(
        confirm, data=csrf_form(admin_client, **without_token(hidden), credential_choice=str(alpha))
    )

    assert asked.status_code == BAD and 'name="credential_choice"' in asked.text
    assert done.status_code == SEE_OTHER
    assert confirmed.status_code == SEE_OTHER
    assert [k for k, _ in swarm_host.auth_headers] == ["distribution", "update"]
    assert swarm_host.auth_headers[-1][1] not in (None, EMPTY_AUTH)


async def test_an_edit_goes_form_then_review_then_confirm_and_a_second_confirm_is_stale(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id = await create_through_the_form(admin_client, world)
    base = f"{base_of(world)}/services/{service_id}"
    edit = await admin_client.get(f"{base}/edit")
    values = {
        **field_values(edit),
        "mode": "replicated",
        "form_version": hidden_fields(edit)["form_version"],
        "command": "sleep 200",
        "constraints": "node.role==worker",
    }

    review = await admin_client.post(f"{base}/edit", data=csrf_form(admin_client, **values))
    hidden = hidden_fields(review)
    confirm = form_action(review)
    updated = await admin_client.post(confirm, data=hidden)
    updated_notices = await landed_texts(admin_client, updated)
    stale = await admin_client.post(confirm, data=hidden)

    text = unescape(review.text)
    assert review.status_code == OK and "Command" in text and "Placement" in text
    assert hidden["expected_version"].isdigit() and confirm.endswith("/edit/confirm")
    assert "sleep 200" not in text.split("<main")[1].split("</form>")[0].replace(
        'value="sleep 200"', ""
    )
    assert updated.status_code == SEE_OTHER and "?" not in location(updated)
    assert updated_notices == ["Service updated. Its tasks are replaced as the update rolls out."]
    assert stale.status_code == CONFLICT


async def test_the_review_and_the_edit_again_form_carry_the_version_the_form_was_built_from(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id = swarm_host.add_service(plain_spec("web"))
    base = f"{base_of(world)}/services/{service_id}"
    edit = await admin_client.get(f"{base}/edit")
    version = hidden_fields(edit)["form_version"]
    values = {**field_values(edit), "mode": "replicated", "form_version": version, "command": "a b"}

    review = await admin_client.post(f"{base}/edit", data=csrf_form(admin_client, **values))
    back = await admin_client.post(
        f"{base}/edit",
        data=csrf_form(admin_client, **without_token(hidden_fields(review)), step="edit"),
    )

    assert f'name="form_version" value="{version}"' in review.text
    assert back.status_code == OK and f'name="form_version" value="{version}"' in back.text


async def test_a_form_built_from_an_older_version_is_refused_at_the_review(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id = swarm_host.add_service(plain_spec("web"))
    base = f"{base_of(world)}/services/{service_id}"
    edit = await admin_client.get(f"{base}/edit")
    swarm_host.services[service_id]["Version"]["Index"] += 1
    values = {
        **field_values(edit),
        "mode": "replicated",
        "form_version": hidden_fields(edit)["form_version"],
        "command": "a b",
    }

    review = await admin_client.post(f"{base}/edit", data=csrf_form(admin_client, **values))

    assert review.status_code == CONFLICT


async def test_the_tasks_poll_fragment_does_not_read_the_service_detail(
    admin_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_id = swarm_host.add_service(plain_spec("web"))
    base = f"{base_of(world)}/services/{service_id}"

    async def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("detail() called by the tasks poll")

    monkeypatch.setattr(SwarmServiceQueries, "detail", boom)

    fragment = await admin_client.get(f"{base}/tasks", headers=HTMX)

    assert fragment.status_code == OK and 'id="service-tasks"' in fragment.text
    assert "/swarm/tasks/" in fragment.text


async def test_the_tasks_fragment_polls_only_while_a_task_is_moving_and_has_a_no_script_refresh(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id = swarm_host.add_service(plain_spec("web"))
    task = swarm_host.service_tasks(service_id)[0]
    base = f"{base_of(world)}/services/{service_id}"
    swarm_host.tasks[task]["DesiredState"] = "ready"

    moving = await admin_client.get(f"{base}/tasks", headers=HTMX)
    page = await admin_client.get(f"{base}?tab=tasks")
    swarm_host.tasks[task]["DesiredState"] = "running"
    settled = await admin_client.get(f"{base}/tasks", headers=HTMX)

    assert "<html" not in moving.text and 'hx-trigger="every 5s"' in moving.text
    assert '<noscript><meta http-equiv="refresh"' in page.text and "Refresh" in page.text
    assert "hx-trigger" not in settled.text


async def test_scaling_up_is_one_post_and_scaling_down_needs_a_confirmation_that_binds_the_counts(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine, started_app: FastAPI
) -> None:
    service_id = await create_through_the_form(admin_client, world)
    base = f"{base_of(world)}/services/{service_id}"
    bound = hidden_fields(await admin_client.get(base))

    up = await admin_client.post(
        f"{base}/scale", data=csrf_form(admin_client, replicas="3", **without_token(bound))
    )
    up_notices = await landed_texts(admin_client, up)
    bound = hidden_fields(await admin_client.get(base))
    down = await admin_client.post(
        f"{base}/scale", data=csrf_form(admin_client, replicas="1", **without_token(bound))
    )
    confirmation = await admin_client.get(down.headers["location"])
    fields = hidden_fields(confirmation)
    done = await admin_client.post(
        f"{base}/scale", data=csrf_form(admin_client, **without_token(fields))
    )

    assert up.status_code == SEE_OTHER and "?" not in location(up)
    assert up_notices == [
        "Replica count changed. Tasks start or stop as the manager schedules them."
    ]
    assert down.status_code == SEE_OTHER and "scale?replicas=1" in down.headers["location"]
    assert fields["confirmed"] == "yes" and done.status_code == SEE_OTHER
    assert swarm_host.services[service_id]["Spec"]["Mode"] == {"Replicated": {"Replicas": 1}}
    async with started_app.state.database.session() as db:
        rows = await rows_of(db, "service_scaled")
    assert [(r["detail"]["replicas_before"], r["detail"]["replicas_after"]) for r in rows] == [
        (2, 3),
        (3, 1),
    ]


async def test_a_force_update_names_the_tasks_and_a_second_post_on_the_same_version_is_stale(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id = swarm_host.add_service(plain_spec("web", replicas=2))
    base = f"{base_of(world)}/services/{service_id}"
    page = await admin_client.get(f"{base}/force-update")
    version = hidden_fields(page)["expected_version"]

    first = await admin_client.post(
        f"{base}/force-update", data=csrf_form(admin_client, expected_version=version)
    )
    stale = await admin_client.post(
        f"{base}/force-update", data=csrf_form(admin_client, expected_version=version)
    )

    assert first.status_code == SEE_OTHER and stale.status_code == CONFLICT
    assert swarm_host.services[service_id]["Spec"]["TaskTemplate"]["ForceUpdate"] == 1


async def test_a_rollback_confirmation_then_post_restores_the_previous_spec(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id = await create_through_the_form(admin_client, world)
    _, confirm, hidden = await reviewed_edit(admin_client, world, service_id, command="sleep 9")
    await admin_client.post(confirm, data=hidden)
    base = f"{base_of(world)}/services/{service_id}"

    page = await admin_client.get(f"{base}/rollback")
    done = await admin_client.post(
        f"{base}/rollback",
        data=csrf_form(admin_client, expected_version=hidden_fields(page)["expected_version"]),
    )

    assert page.status_code == OK and done.status_code == SEE_OTHER
    args = swarm_host.services[service_id]["Spec"]["TaskTemplate"]["ContainerSpec"]["Args"]
    assert args == ["sleep", "100"]


async def test_removing_names_the_service_binds_it_and_redirects_to_the_list_with_a_notice(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id = await create_through_the_form(admin_client, world)
    base = f"{base_of(world)}/services/{service_id}"
    page = await admin_client.get(f"{base}/remove")

    removed = await admin_client.post(
        f"{base}/remove", data=csrf_form(admin_client, **without_token(hidden_fields(page)))
    )
    listing = await land(admin_client, removed)

    assert removed.status_code == SEE_OTHER and location(removed) == f"{base_of(world)}/services"
    assert flash_texts(listing.text) == ["Service removed."]
    assert service_id not in swarm_host.services


async def test_a_standard_user_creates_edits_and_scales_their_own_service_within_the_restrictions(
    standard_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    base = base_of(world)
    bind = csrf_form(
        standard_client,
        **CREATE,
        **{"mount-0-type": "bind", "mount-0-source": "/etc", "mount-0-target": "/x"},
    )
    refused = await standard_client.post(f"{base}/services/new", data=bind)
    too_many = await standard_client.post(
        f"{base}/services/new",
        data=csrf_form(standard_client, **{**CREATE, "name": "big", "replicas": "500"}),
    )
    service_id = await create_through_the_form(
        standard_client, world, **{"env-0-key": "A", "env-0-value": "b"}
    )
    edit = await standard_client.get(f"{base}/services/{service_id}/edit")
    _, confirm, hidden = await reviewed_edit(standard_client, world, service_id, command="sleep 5")
    saved = await standard_client.post(confirm, data=hidden)
    bound = hidden_fields(await standard_client.get(f"{base}/services/{service_id}"))
    scaled = await standard_client.post(
        f"{base}/services/{service_id}/scale",
        data=csrf_form(standard_client, replicas="2", **without_token(bound)),
    )
    access = await standard_client.post(
        f"{base}/services/{service_id}/access",
        data=csrf_form(standard_client, visibility="public", access_level="read"),
    )

    env = swarm_host.services[service_id]["Spec"]["TaskTemplate"]["ContainerSpec"]["Env"]
    assert refused.status_code == BAD and too_many.status_code == BAD
    assert 'value="b"' not in edit.text and env == ["A=b"]
    assert saved.status_code == SEE_OTHER and scaled.status_code == SEE_OTHER
    assert access.status_code in (FORBIDDEN, NOT_FOUND)
    assert list(swarm_host.services) == [service_id]


async def test_an_image_change_with_no_credentials_sends_the_empty_auth_document_not_the_stored_one(
    standard_client: AsyncClient,
    admin_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
    started_app: FastAPI,
) -> None:
    service_id = swarm_host.add_service(plain_spec("web", image="registry.example.com/team/app:1"))
    await plant_control(
        started_app, world.remote_id, "service", service_id, public=True, level="write"
    )
    seen: list[tuple[str, list[str | None]]] = []

    for name, client, image in (
        ("standard", standard_client, "attacker.example.net/x:1"),
        ("administrator", admin_client, "other.example.net/y:2"),
    ):
        swarm_host.auth_headers.clear()
        swarm_host.registry_auth_from.clear()
        _, confirm, hidden = await reviewed_edit(client, world, service_id, image=image)
        done = await client.post(confirm, data=hidden)
        assert done.status_code == SEE_OTHER
        seen.append((name, [h for k, h in swarm_host.auth_headers if k == "update"]))

    assert seen == [("standard", [EMPTY_AUTH]), ("administrator", [EMPTY_AUTH])]
    assert swarm_host.registry_auth_from == [None]


async def test_a_standard_users_rollback_to_a_privileged_previous_spec_is_refused_and_sent_nowhere(
    standard_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
    started_app: FastAPI,
) -> None:
    service_id = swarm_host.add_service(plain_spec("web", image="busybox:latest"))
    previous = plain_spec("web", image="busybox:latest")
    previous["TaskTemplate"]["ContainerSpec"]["CapabilityAdd"] = ["SYS_ADMIN"]
    previous["TaskTemplate"]["ContainerSpec"]["Mounts"] = [
        {"Type": "bind", "Source": "/", "Target": "/host"}
    ]
    swarm_host.services[service_id]["PreviousSpec"] = previous
    await plant_control(
        started_app, world.remote_id, "service", service_id, public=True, level="write"
    )
    version = str(swarm_host.services[service_id]["Version"]["Index"])
    before = len(swarm_host.registry_auth_from)

    refused = await standard_client.post(
        f"{base_of(world)}/services/{service_id}/rollback",
        data=csrf_form(standard_client, expected_version=version),
    )

    assert refused.status_code in (BAD, FORBIDDEN, CONFLICT)
    assert len(swarm_host.registry_auth_from) == before


async def logged_service(host: SwarmEngine) -> tuple[str, list[str]]:
    service_id = host.add_service(plain_spec("web", replicas=2))
    tasks = host.service_tasks(service_id)
    host.add_log(tasks[0], "2026-01-01T00:00:01.000000000Z", "hello <b>one</b>")
    host.add_log(tasks[0], "2026-01-01T00:00:03.000000000Z", "oops", "err")
    host.add_log(tasks[1], "2026-01-01T00:00:02.000000000Z", "hello two")
    return service_id, tasks


async def test_the_log_stream_sends_labelled_events_for_both_streams_and_ends(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id, tasks = await logged_service(swarm_host)

    stream = await admin_client.get(
        f"{base_of(world)}/services/{service_id}/logs/stream?follow=0&tail=10"
    )

    assert stream.status_code == OK
    assert (
        "event: out" in stream.text and "event: err" in stream.text and "event: end" in stream.text
    )
    assert f"web.1.{tasks[0][:12]}" in stream.text


async def test_the_download_is_a_text_attachment_and_a_bad_query_is_a_400(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id, _ = await logged_service(swarm_host)
    base = f"{base_of(world)}/services/{service_id}/logs"

    everything = await admin_client.get(f"{base}/download?tail=all")
    recent = await admin_client.get(f"{base}/download?tail=50")
    bad = await admin_client.get(f"{base}/stream?tail=zzz")

    assert everything.status_code == OK and "hello two" in everything.text
    assert "attachment" in recent.headers["content-disposition"] and "hello two" in recent.text
    assert bad.status_code == BAD


async def test_a_hidden_services_logs_are_not_found_even_with_a_malformed_query(
    standard_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id, _ = await logged_service(swarm_host)
    base = f"{base_of(world)}/services/{service_id}/logs"

    stream = await standard_client.get(f"{base}/stream?tail=zzz")
    download = await standard_client.get(f"{base}/download?since=nonsense")

    assert (stream.status_code, download.status_code) == (NOT_FOUND, NOT_FOUND)


async def test_saving_a_stack_control_reads_the_members_first_and_covers_them(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
    started_app: FastAPI,
) -> None:
    shop = swarm_host.add_service(plain_spec("shop-web", Labels={NAMESPACE_LABEL: "Shop"}))
    alone = swarm_host.add_service(plain_spec("alone"))
    config = swarm_host.add_config("conf", labels={NAMESPACE_LABEL: "shop"})
    base = base_of(world)

    saved = await admin_client.post(
        f"{base}/stacks/Shop/access",
        data=csrf_form(admin_client, visibility="public", access_level="read"),
    )
    saved_page = await land(admin_client, saved)
    async with started_app.state.database.session() as db:
        row = await db.fetch_one(
            "SELECT sub_resource_ids FROM resource_control WHERE resource_type = 'stack'"
        )
    listing = await standard_client.get(f"{base}/services")
    stacks = await standard_client.get(f"{base}/stacks")
    detail = await standard_client.get(f"{base}/stacks/Shop")
    removed = await admin_client.post(
        f"{base}/stacks/Shop/access/delete",
        data=csrf_form(admin_client, expected_digest=digest_in(saved_page.text)),
    )
    removed_notices = await landed_texts(admin_client, removed)
    invalid = await admin_client.post(
        f"{base}/stacks/Shop/access", data=csrf_form(admin_client, visibility="bogus")
    )

    assert flash_texts(saved_page.text) == ["Access control saved."]
    assert row is not None
    assert (
        f"service:{shop}" in row["sub_resource_ids"]
        and f"config:{config}" in row["sub_resource_ids"]
    )
    assert f"service:{alone}" not in row["sub_resource_ids"]
    assert "shop-web" in listing.text and "alone" not in listing.text.split("<main")[1]
    assert "Shop" in stacks.text and "shop-web" in detail.text
    assert "Access" not in detail.text.split("tabs__list")[1].split("</ul>")[0]
    assert removed_notices == ["Access control removed."]
    assert invalid.status_code == 422


async def test_a_stack_access_save_whose_member_read_fails_writes_no_control(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    swarm_host.add_service(plain_spec("shop-web", Labels={NAMESPACE_LABEL: "Shop"}))
    swarm_host.outage = {"services"}

    response = await admin_client.post(
        f"{base_of(world)}/stacks/Shop/access",
        data=csrf_form(admin_client, visibility="administrators_only", access_level="read"),
    )
    swarm_host.outage = set()
    stacks = await admin_client.get(f"{base_of(world)}/stacks")

    assert response.status_code == 503
    assert "Access control saved" not in response.text and "administrators" not in stacks.text


async def test_the_network_pages_name_the_services_that_use_an_overlay_network(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    network = swarm_host.add_network(
        "ov", 1, "overlay", network_id="z" + "1".zfill(24), Scope="swarm"
    )
    free = swarm_host.add_network(
        "free", 2, "overlay", network_id="z" + "2".zfill(24), Scope="swarm"
    )
    swarm_host.named_service("uses-ov", networks=[network])
    base = f"/environments/{world.remote_id}/networks"

    listing = await admin_client.get(base)
    overview = await admin_client.get(f"{base}/{network}?tab=overview")
    removal = await admin_client.get(f"{base}/{network}/remove")
    unused = await admin_client.get(f"{base}/{free}/remove")

    row = listing.text.split(f'id="network-{network}"')[1].split("</tr>")[0]
    assert "uses-ov" in row and "Not used" not in row
    assert re.search(r"0 containers and 1 service", overview.text)
    assert "uses-ov" in removal.text and "1 service uses it" in removal.text
    assert 'name="expected_name"' not in removal.text
    assert (
        'name="expected_name"' in unused.text and "No container or service uses it" in unused.text
    )


async def test_the_scale_input_ceiling_follows_the_role(
    standard_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    service_id = await create_through_the_form(standard_client, world)

    detail = await standard_client.get(f"{base_of(world)}/services/{service_id}")

    assert 'max="100"' in detail.text and 'max="1000"' not in detail.text


async def test_a_refused_credential_rebind_is_a_forbidden_page_that_names_the_image_field(
    admin_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "Only an administrator can change the credential set."

    async def refuse(self: object, *args: object, **kwargs: object) -> None:
        raise ServiceInvalid({"image": message}, reason="restricted")

    monkeypatch.setattr(SwarmServiceActions, "create", refuse)

    response = await admin_client.post(
        f"{base_of(world)}/services/new", data=csrf_form(admin_client, **CREATE)
    )

    assert response.status_code == FORBIDDEN and message in response.text


async def test_the_stack_list_reads_the_engine_the_same_number_of_times_for_any_namespace_count(
    standard_client: AsyncClient,
    admin_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
    started_app: FastAPI,
) -> None:
    base = base_of(world)
    await admin_client.post(
        f"{base}/stacks/Shop/access",
        data=csrf_form(admin_client, visibility="public", access_level="read"),
    )
    swarm_host.add_service(plain_spec("shop-web", Labels={NAMESPACE_LABEL: "Shop"}))
    swarm_host.requests.clear()
    await standard_client.get(f"{base}/stacks")
    one = len(swarm_host.requests)
    for number in range(6):
        swarm_host.add_service(plain_spec(f"s{number}", Labels={NAMESPACE_LABEL: f"ns{number}"}))
    swarm_host.requests.clear()

    await standard_client.get(f"{base}/stacks")

    assert len(swarm_host.requests) == one
