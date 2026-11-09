"""The container export pages: the Export action, the confirmation and the archive download."""

import dataclasses
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient

import app.services.volume_export as volume_export
from app.docker_engine.registry import EngineClientRegistry
from app.services.volume_export import ExportSlots
from tests.auth_support import csrf_form
from tests.container_export_support import (
    BIND_SOURCE,
    IMAGE,
    PASSPHRASE,
    PLAIN_NAME,
    PLAIN_VALUE,
    SECRET_NAME,
    SECRET_VALUE,
    SOURCE,
    VOLUME,
    archive_text,
    manifest_of,
    stock_source,
)
from tests.environment_support import activity_rows
from tests.object_support import hidden_values, visible_text
from tests.self_support import SELF_ID, home_identity
from tests.volume_export_support import NOTE, NOTE_FILE, VolumeEngine, unpack
from tests.web.environment_support import World

OK, FORBIDDEN, NOT_FOUND, CONFLICT, BAD, TOO_MANY = 200, 403, 404, 409, 400, 429
WRONG_LENGTH = "short"


@pytest.fixture
async def container_host(world: World) -> AsyncIterator[VolumeEngine]:
    fake = VolumeEngine()
    stock_source(fake)
    engine = world.app.state.engine
    registry = EngineClientRegistry(lambda settings: fake.bundle())
    engine.registry = registry
    yield fake
    fake.release_gates()
    await registry.aclose()


def detail_path(world: World, container: str = SOURCE) -> str:
    return f"/environments/{world.remote_id}/containers/{container}"


def export_path(world: World, container: str = SOURCE) -> str:
    return f"{detail_path(world, container)}/export"


async def export_rows(world: World, action: str = "container_exported") -> list[dict[str, object]]:
    async with world.app.state.database.session() as db:
        return [r for r in await activity_rows(db) if str(r["action"]) == action]


async def digest_of(client: AsyncClient, world: World) -> str:
    page = await client.get(export_path(world))
    (digest,) = hidden_values(page, "expected_digest")
    return digest


async def test_the_detail_page_offers_export_to_an_administrator_but_not_for_dockmins_own_container(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: VolumeEngine,
) -> None:
    container_host.add(SELF_ID, "dockmin", image=IMAGE)
    world.app.state.self_identity = dataclasses.replace(
        home_identity(world.remote_id), mount_sources={world.remote_id: frozenset()}
    )

    plain = await admin_client.get(detail_path(world))
    own = await admin_client.get(detail_path(world, SELF_ID))
    standard = await standard_client.get(detail_path(world))

    assert f'href="{export_path(world)}"' in plain.text
    assert "/export" not in own.text and "/export" not in standard.text


async def test_the_confirmation_lists_what_is_included_and_left_out_and_shows_no_value(
    admin_client: AsyncClient, world: World, container_host: VolumeEngine
) -> None:
    page = await admin_client.get(export_path(world))

    text = visible_text(page)
    assert page.status_code == OK and page.headers["cache-control"] == "no-store"
    assert SECRET_NAME in text and PLAIN_NAME in text and BIND_SOURCE in text
    assert SECRET_VALUE not in page.text and PLAIN_VALUE not in page.text
    assert "written as plain text" in text and "Resource controls and team grants" in text
    assert "Anonymous volumes" not in text and "shop-net" in text and VOLUME in text
    assert page.text.count('autocomplete="new-password"') == 2
    assert page.text.count('type="password"') == 2
    assert 'name="secret" value="DB_PASSWORD"' in page.text and f'value="{VOLUME}"' in page.text
    assert hidden_values(page, "csrf_token") and 'method="post"' in page.text
    assert "hx-" not in page.text.split("<main", 1)[1]
    assert not container_host.helpers() and "create" not in container_host.verbs()


@pytest.mark.parametrize("method", ["get", "post"])
async def test_a_standard_user_gets_a_not_found_page_and_nothing_is_recorded(
    standard_client: AsyncClient, world: World, container_host: VolumeEngine, method: str
) -> None:
    if method == "get":
        response = await standard_client.get(export_path(world))
    else:
        response = await standard_client.post(
            export_path(world), data=csrf_form(standard_client, expected_digest="x")
        )

    assert response.status_code == NOT_FOUND
    assert await export_rows(world) == [] and "create" not in container_host.verbs()
    assert await export_rows(world, "container_export_started") == []


async def test_the_post_downloads_an_archive_that_matches_the_confirmation(
    admin_client: AsyncClient, world: World, container_host: VolumeEngine
) -> None:
    digest = await digest_of(admin_client, world)

    response = await admin_client.post(
        export_path(world),
        data=csrf_form(
            admin_client,
            expected_digest=digest,
            secret_handling="seal",
            passphrase=PASSPHRASE,
            passphrase_confirm=PASSPHRASE,
            secret=[SECRET_NAME],
            volume=[VOLUME],
        ),
    )

    members = unpack(response.content)
    manifest = manifest_of(members)
    disposition = response.headers["content-disposition"]
    assert response.status_code == OK
    assert disposition.startswith('attachment; filename="container-shop-web-')
    assert disposition.endswith('.tar.gz"')
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "application/gzip"
    assert (manifest.format_version, manifest.secrets) == (1, "sealed")
    assert [v.manifest.name for v in manifest.volumes] == [VOLUME]
    assert members[f"{manifest.volumes[0].prefix}/data/{NOTE_FILE}"][1] == NOTE
    assert SECRET_VALUE not in archive_text(members) and PLAIN_VALUE in archive_text(members)
    for action in ("container_export_started", "container_exported"):
        rows = await export_rows(world, action)
        assert [(r["result"], r["detail"]["secrets"]) for r in rows] == [  # type: ignore[index]
            ("success", "sealed")
        ]
    assert container_host.helpers() == []


async def test_a_post_without_the_csrf_token_is_refused_before_anything_is_created(
    admin_client: AsyncClient, world: World, container_host: VolumeEngine
) -> None:
    response = await admin_client.post(export_path(world), data={"expected_digest": "x"})

    assert response.status_code == FORBIDDEN and "create" not in container_host.verbs()


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        ({"expected_digest": "stale"}, CONFLICT),
        ({"passphrase": PASSPHRASE, "passphrase_confirm": PASSPHRASE + "x"}, BAD),
        (
            {
                "passphrase": WRONG_LENGTH,
                "passphrase_confirm": WRONG_LENGTH,
                "secret": [SECRET_NAME],
            },
            BAD,
        ),
    ],
)
async def test_a_refused_export_answers_a_page_that_never_carries_the_passphrase(
    admin_client: AsyncClient,
    world: World,
    container_host: VolumeEngine,
    fields: dict[str, str | list[str]],
    status: int,
) -> None:
    form = {"expected_digest": await digest_of(admin_client, world), "secret_handling": "seal"}

    response = await admin_client.post(
        export_path(world), data=csrf_form(admin_client, **{**form, **fields})
    )

    assert response.status_code == status and "content-disposition" not in response.headers
    assert PASSPHRASE not in response.text and WRONG_LENGTH not in visible_text(response)
    assert "create" not in container_host.verbs()


async def test_a_busy_instance_answers_429_with_a_retry_header(
    admin_client: AsyncClient,
    world: World,
    container_host: VolumeEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = await digest_of(admin_client, world)
    full = ExportSlots(per_environment=1, per_instance=1)
    full.acquire(world.remote_id)
    monkeypatch.setattr(volume_export, "_SLOTS", full)

    response = await admin_client.post(
        export_path(world),
        data=csrf_form(admin_client, expected_digest=digest, secret_handling="omit"),
    )

    assert response.status_code == TOO_MANY and response.headers["retry-after"]
    assert "create" not in container_host.verbs()


async def test_dockmins_own_container_is_refused_on_the_confirmation(
    admin_client: AsyncClient, world: World, container_host: VolumeEngine
) -> None:
    container_host.add(SELF_ID, "dockmin", image=IMAGE)
    world.app.state.self_identity = dataclasses.replace(
        home_identity(world.remote_id), mount_sources={world.remote_id: frozenset()}
    )

    page = await admin_client.get(export_path(world, SELF_ID))

    assert page.status_code == CONFLICT and "cannot be exported" in visible_text(page)
    assert "create" not in container_host.verbs()
