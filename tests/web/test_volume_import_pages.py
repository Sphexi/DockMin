"""The volume import pages: upload, review, import and result."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import AsyncClient, Response

from app.docker_engine.registry import EngineClientRegistry
from app.services.volume_import_store import SPOOL_DIRECTORY
from tests.auth_support import csrf_form, csrf_token_of
from tests.container_support import stock
from tests.environment_support import activity_rows
from tests.object_support import hidden_values, visible_text
from tests.volume_import_support import (
    FIXTURE,
    ImportEngine,
    export_archive,
    file,
    manifest_document,
    plain_tar,
)
from tests.web.environment_support import World

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN = 200, 303, 400, 403
NOT_FOUND, CONFLICT, UNPROCESSABLE = 404, 409, 422
BOUNDARY = "xx-volume-import-xx"
IMAGE = "busybox:1"
TARGET = "restored"
STACK_LABEL = "com.docker.compose.project"


@pytest.fixture
async def import_host(world: World) -> AsyncIterator[ImportEngine]:
    fake = stock(ImportEngine())
    fake.add_image(IMAGE)
    engine = world.app.state.engine
    registry = EngineClientRegistry(lambda settings: fake.bundle())
    engine.registry = registry
    yield fake
    fake.release_gates()
    await registry.aclose()


def base(world: World) -> str:
    return f"/environments/{world.remote_id}/volumes"


def multipart(parts: list[tuple[str, str | None, bytes]]) -> tuple[bytes, dict[str, str]]:
    body = b""
    for name, filename, data in parts:
        disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        body += f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n\r\n".encode()
        body += data + b"\r\n"
    body += f"--{BOUNDARY}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}


async def upload(
    client: AsyncClient,
    world: World,
    archive: bytes | None = None,
    *,
    token: str | None = None,
    file_first: bool = False,
) -> Response:
    token_part = ("csrf_token", None, (token or csrf_token_of(client)).encode())
    file_part = ("archive", "export.tar.gz", FIXTURE.read_bytes() if archive is None else archive)
    parts = [file_part, token_part] if file_first else [token_part, file_part]
    body, headers = multipart(parts)
    return await client.post(f"{base(world)}/-/import", content=body, headers=headers)


def spooled(world: World) -> list[Path]:
    directory = world.app.state.settings.data_dir / SPOOL_DIRECTORY
    return sorted(directory.glob("*")) if directory.exists() else []


async def rows(world: World) -> list[dict[str, object]]:
    async with world.app.state.database.session() as db:
        return [r for r in await activity_rows(db) if r["action"] == "volume_imported"]


async def reviewed(client: AsyncClient, world: World, archive: bytes | None = None) -> str:
    response = await upload(client, world, archive)
    assert response.status_code == SEE_OTHER
    return str(response.headers["location"])


def run_fields(client: AsyncClient, review: Response, **fields: str) -> dict[str, str]:
    return csrf_form(
        client,
        expected_digest=hidden_values(review, "expected_digest")[0],
        name=TARGET,
        driver="",
        **fields,
    )


async def test_the_list_offers_import_to_an_administrator_only(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    import_host: ImportEngine,
) -> None:
    admin = await admin_client.get(base(world))
    standard = await standard_client.get(base(world))

    assert f'href="{base(world)}/-/import"' in admin.text and ">Import volume<" in admin.text
    assert "/-/import" not in standard.text


async def test_the_upload_form_posts_the_token_before_the_file_and_works_without_script(
    admin_client: AsyncClient, world: World, import_host: ImportEngine
) -> None:
    page = await admin_client.get(f"{base(world)}/-/import")

    form = page.text
    assert page.status_code == OK and page.headers["cache-control"] == "no-store"
    assert form.index('name="csrf_token"') < form.index('name="archive"')
    assert 'enctype="multipart/form-data"' in form and 'method="post"' in form
    assert "hx-" not in form.split("<main", 1)[1]


async def test_a_standard_user_gets_a_not_found_page_on_every_route_and_nothing_is_recorded(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    import_host: ImportEngine,
) -> None:
    location = await reviewed(admin_client, world)
    review_path = location
    held = spooled(world)

    responses = [
        await standard_client.get(f"{base(world)}/-/import"),
        await upload(standard_client, world),
        await standard_client.get(review_path),
        await standard_client.post(review_path, data=csrf_form(standard_client, name=TARGET)),
        await standard_client.post(f"{review_path}/cancel", data=csrf_form(standard_client)),
    ]

    assert [r.status_code for r in responses] == [NOT_FOUND] * 5
    assert await rows(world) == [] and spooled(world) == held
    assert import_host.calls("POST", "/volumes/create") == [] and import_host.written == {}


async def test_a_wrong_token_is_refused_before_a_file_byte_is_stored(
    admin_client: AsyncClient, world: World, import_host: ImportEngine
) -> None:
    wrong = await upload(admin_client, world, token="x" * 43)
    file_first = await upload(admin_client, world, file_first=True)

    assert wrong.status_code == FORBIDDEN and file_first.status_code == BAD_REQUEST
    assert spooled(world) == [] and import_host.requests == []


async def test_an_upload_is_reviewed_and_imported_and_the_result_names_the_volume(
    admin_client: AsyncClient, world: World, import_host: ImportEngine
) -> None:
    location = await reviewed(
        admin_client,
        world,
        export_archive(
            file("a.txt"),
            manifest=manifest_document(
                name="shop-data",
                labels={"team": "core", STACK_LABEL: "shop"},
                options={"type": "none", "o": "bind", "device": "/srv/shop"},
                removed_option_keys=["o.password"],
            ),
        ),
    )
    review = await admin_client.get(location)
    text = visible_text(review)

    assert review.status_code == OK and review.headers["cache-control"] == "no-store"
    assert "shop-data" in text and "Format version 1" in text and "1 files" in text
    assert "team" in text and STACK_LABEL in text and "Not applied" in text
    assert "o.password" in text and "dockmin-read-" in text and "never started" in text
    assert hidden_values(review, "expected_digest") and hidden_values(review, "csrf_token")
    assert 'value="/srv/shop"' in review.text and "hx-" not in review.text.split("<main", 1)[1]
    assert import_host.calls("POST", "/volumes/create") == []

    options = {
        "option-0-key": "type",
        "option-0-value": "none",
        "option-1-key": "o",
        "option-1-value": "bind",
        "option-2-key": "device",
        "option-2-value": "/srv/shop",
    }
    result = await admin_client.post(location, data=run_fields(admin_client, review, **options))
    listed = await admin_client.get(base(world))
    (row,) = await rows(world)

    assert result.status_code == OK and "was created and holds the archived data" in visible_text(
        result
    )
    assert TARGET in import_host.volumes and set(import_host.files_of(TARGET)) == {"a.txt"}
    assert f'href="{base(world)}/{TARGET}"' in result.text and TARGET in listed.text
    (body,) = import_host.volume_bodies()
    assert body["Labels"] == {"team": "core"} and body["DriverOpts"] == {
        "type": "none",
        "o": "bind",
        "device": "/srv/shop",
    }
    assert (row["result"], row["target_id"]) == ("success", TARGET)
    assert spooled(world) == [] and import_host.helpers() == []


async def test_an_existing_name_answers_the_review_again_with_the_name_to_change(
    admin_client: AsyncClient, world: World, import_host: ImportEngine
) -> None:
    import_host.add_volume("taken")
    location = await reviewed(admin_client, world)
    review = await admin_client.get(location)

    refused = await admin_client.post(
        location, data=run_fields(admin_client, review) | {"name": "taken"}
    )
    retried = await admin_client.post(location, data=run_fields(admin_client, review))

    assert refused.status_code == CONFLICT and "already exists" in visible_text(refused)
    assert 'value="taken"' in refused.text and hidden_values(refused, "expected_digest")
    assert import_host.calls("POST", "/volumes/create") != [] and "taken" not in import_host.written
    assert retried.status_code == OK and TARGET in import_host.volumes


async def test_more_option_rows_can_be_added_without_importing(
    admin_client: AsyncClient, world: World, import_host: ImportEngine
) -> None:
    location = await reviewed(admin_client, world)
    review = await admin_client.get(location)

    more = await admin_client.post(location, data=run_fields(admin_client, review, rows="option"))

    assert (
        more.status_code == OK and "option-3-key" in more.text and "option-4-key" not in more.text
    )
    assert import_host.calls("POST", "/volumes/create") == []


@pytest.mark.parametrize(
    ("archive", "message"),
    [
        pytest.param(plain_tar(file("a")), "volume export", id="plain-tar"),
        pytest.param(
            export_archive(manifest=manifest_document(format_version=3), skipped=None),
            "version is 3",
            id="newer-version",
        ),
        pytest.param(b"not an archive at all", "not a gzip archive", id="not-gzip"),
    ],
)
async def test_a_refused_archive_answers_the_upload_page_with_the_reason_and_stores_nothing(
    admin_client: AsyncClient, world: World, import_host: ImportEngine, archive: bytes, message: str
) -> None:
    response = await upload(admin_client, world, archive)

    assert response.status_code == UNPROCESSABLE and message in visible_text(response)
    assert spooled(world) == [] and import_host.requests == [] and await rows(world) == []


async def test_a_discarded_upload_is_removed_and_its_review_is_gone(
    admin_client: AsyncClient, world: World, import_host: ImportEngine
) -> None:
    location = await reviewed(admin_client, world)
    assert len(spooled(world)) == 1

    cancelled = await admin_client.post(f"{location}/cancel", data=csrf_form(admin_client))
    again = await admin_client.get(location)

    assert cancelled.status_code == SEE_OTHER and spooled(world) == []
    assert again.status_code == CONFLICT and "no longer held" in visible_text(again)


async def test_a_failed_write_answers_with_the_removal_of_the_volume(
    admin_client: AsyncClient, world: World, import_host: ImportEngine
) -> None:
    import_host.fail("put_archive", 500)
    location = await reviewed(admin_client, world)
    review = await admin_client.get(location)

    response = await admin_client.post(location, data=run_fields(admin_client, review))

    assert response.status_code == 502 and "was removed" in visible_text(response)
    assert TARGET not in import_host.volumes and spooled(world) == []
    (row,) = await rows(world)
    assert row["result"] == "failure"
