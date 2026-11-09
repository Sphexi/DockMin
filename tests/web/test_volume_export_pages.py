"""The volume export pages: the Export action, the confirmation and the archive download."""

import dataclasses
import json
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient

import app.services.volume_export as volume_export
from app.docker_engine.registry import EngineClientRegistry
from app.services.volume_export import ExportSlots
from tests.auth_support import csrf_form
from tests.container_support import stock
from tests.docker_engine.container_engine import VOLUME_CREATED
from tests.environment_support import activity_rows
from tests.object_support import add_container, hidden_values, visible_text, volume_mount
from tests.self_support import SELF_VOLUME, home_identity
from tests.volume_export_support import NOTE, NOTE_FILE, VolumeEngine, unpack, volume_tar
from tests.web.environment_support import World

OK, FORBIDDEN, NOT_FOUND, CONFLICT, TOO_MANY = 200, 403, 404, 409, 429
VOLUME = "archive-me"
IMAGE = "busybox:1"
RUNNING_NAME = "web-server"


@pytest.fixture
async def container_host(world: World) -> AsyncIterator[VolumeEngine]:
    fake = stock(VolumeEngine())
    fake.add_image(IMAGE)
    fake.add_volume(VOLUME)
    fake.serve_volume(VOLUME, volume_tar())
    engine = world.app.state.engine
    registry = EngineClientRegistry(lambda settings: fake.bundle())
    engine.registry = registry
    yield fake
    fake.release_gates()
    await registry.aclose()


def detail_path(world: World) -> str:
    return f"/environments/{world.remote_id}/volumes/{VOLUME}"


def export_path(world: World, name: str = VOLUME) -> str:
    return f"/environments/{world.remote_id}/volumes/{name}/export"


async def test_the_detail_page_offers_export_to_an_administrator_only_when_the_volume_is_free(
    admin_client: AsyncClient, world: World, container_host: VolumeEngine
) -> None:
    container_host.add_volume(SELF_VOLUME)
    world.app.state.self_identity = dataclasses.replace(
        home_identity(world.remote_id, volumes=(SELF_VOLUME,)),
        mount_sources={world.remote_id: frozenset()},
    )

    plain = await admin_client.get(detail_path(world))
    own = await admin_client.get(f"/environments/{world.remote_id}/volumes/{SELF_VOLUME}")

    assert f'href="{export_path(world)}"' in plain.text and ">Export<" in plain.text
    assert "Export</a>" not in own.text and "In use by dockmin" in own.text


async def test_the_confirmation_states_what_is_read_and_who_can_read_it(
    admin_client: AsyncClient, world: World, container_host: VolumeEngine
) -> None:
    page = await admin_client.get(export_path(world))

    text = " ".join(visible_text(page).split())
    assert page.status_code == OK and page.headers["cache-control"] == "no-store"
    assert "Anyone who holds the file can read everything in the volume" in text
    assert "dockmin-read-" in text and "never started" in text and "removed" in text
    assert "No running container mounts the volume" in text
    assert hidden_values(page, "expected_created_at") == [VOLUME_CREATED]
    assert hidden_values(page, "csrf_token") and 'method="post"' in page.text
    assert "hx-" not in page.text.split("<main", 1)[1]
    assert not container_host.helpers() and "create" not in container_host.verbs()


async def test_the_confirmation_names_the_running_containers_and_warns_of_an_inconsistent_archive(
    admin_client: AsyncClient, world: World, container_host: VolumeEngine
) -> None:
    add_container(
        container_host, 0x71, RUNNING_NAME, state="running", mounts=[volume_mount(VOLUME)]
    )

    page = await admin_client.get(export_path(world))

    text = " ".join(visible_text(page).split())
    assert RUNNING_NAME in text and "can be inconsistent" in text
    assert "No running container" not in text


@pytest.mark.parametrize("method", ["get", "post"])
async def test_a_standard_user_gets_a_not_found_page_and_nothing_is_recorded(
    standard_client: AsyncClient, world: World, container_host: VolumeEngine, method: str
) -> None:
    if method == "get":
        response = await standard_client.get(export_path(world))
    else:
        response = await standard_client.post(
            export_path(world),
            data=csrf_form(standard_client, expected_created_at=VOLUME_CREATED),
        )

    assert response.status_code == NOT_FOUND
    assert await activity_rows_of(world) == [] and "create" not in container_host.verbs()


async def activity_rows_of(world: World) -> list[dict[str, object]]:
    async with world.app.state.database.session() as db:
        return [r for r in await activity_rows(db) if str(r["action"]).startswith("volume_export")]


async def test_the_post_downloads_the_archive_with_the_headers_of_an_attachment(
    admin_client: AsyncClient, world: World, container_host: VolumeEngine
) -> None:
    response = await admin_client.post(
        export_path(world),
        data=csrf_form(admin_client, expected_created_at=VOLUME_CREATED),
    )

    members = unpack(response.content)
    disposition = response.headers["content-disposition"]
    assert response.status_code == OK
    assert disposition.startswith(f'attachment; filename="volume-{VOLUME}-')
    assert disposition.endswith('.tar.gz"')
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "application/gzip"
    assert list(members)[:2] == ["manifest.json", "data"]
    assert members[f"data/{NOTE_FILE}"][1] == NOTE
    assert json.loads(members["manifest.json"][1])["format_version"] == 1


async def test_a_completed_download_leaves_two_rows_and_no_helper(
    admin_client: AsyncClient, world: World, container_host: VolumeEngine
) -> None:
    await admin_client.post(
        export_path(world),
        data=csrf_form(admin_client, expected_created_at=VOLUME_CREATED),
    )

    rows = await activity_rows_of(world)

    assert [(r["action"], r["result"]) for r in rows] == [
        ("volume_export_started", "success"),
        ("volume_exported", "success"),
    ]
    assert container_host.helpers() == []


async def test_a_post_without_the_csrf_token_is_refused_before_anything_is_created(
    admin_client: AsyncClient, world: World, container_host: VolumeEngine
) -> None:
    response = await admin_client.post(
        export_path(world), data={"expected_created_at": VOLUME_CREATED}
    )

    assert response.status_code == FORBIDDEN and "create" not in container_host.verbs()


async def test_a_volume_that_changed_since_the_review_is_answered_with_a_page_to_review_again(
    admin_client: AsyncClient, world: World, container_host: VolumeEngine
) -> None:
    response = await admin_client.post(
        export_path(world),
        data=csrf_form(admin_client, expected_created_at="2000-01-01T00:00:00Z"),
    )

    assert response.status_code == CONFLICT
    assert "changed" in visible_text(response).lower()
    assert "content-disposition" not in response.headers and "create" not in container_host.verbs()


async def test_a_busy_instance_answers_429_with_a_retry_header(
    admin_client: AsyncClient,
    world: World,
    container_host: VolumeEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full = ExportSlots(per_environment=1, per_instance=1)
    full.acquire(world.local_id)
    monkeypatch.setattr(volume_export, "_SLOTS", full)

    response = await admin_client.post(
        export_path(world),
        data=csrf_form(admin_client, expected_created_at=VOLUME_CREATED),
    )

    assert response.status_code == TOO_MANY and response.headers["retry-after"]
    assert "Too many volume exports" in visible_text(response)
    assert "create" not in container_host.verbs()


async def test_a_plugin_volume_and_dockmins_own_volume_are_refused_on_the_confirmation(
    admin_client: AsyncClient, world: World, container_host: VolumeEngine
) -> None:
    container_host.add_volume("plugged", driver="nfs-plugin")
    container_host.add_volume(SELF_VOLUME)
    world.app.state.self_identity = dataclasses.replace(
        home_identity(world.remote_id, volumes=(SELF_VOLUME,)),
        mount_sources={world.remote_id: frozenset()},
    )

    plugin = await admin_client.get(export_path(world, "plugged"))
    own = await admin_client.get(export_path(world, SELF_VOLUME))

    assert (plugin.status_code, own.status_code) == (CONFLICT, CONFLICT)
    assert "local driver" in visible_text(plugin) and "cannot be exported" in visible_text(own)
    assert "create" not in container_host.verbs()
