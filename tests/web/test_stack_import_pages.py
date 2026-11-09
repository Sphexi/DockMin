"""The stack import pages: upload, review, passphrase, import and the deployment that follows."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import AsyncClient, Response

from app.docker_engine.registry import EngineClientRegistry
from app.services.stack_import_store import SPOOL_DIRECTORY
from app.services.stack_jobs import TooManyStackJobs
from tests.auth_support import csrf_form, csrf_token_of
from tests.environment_support import activity_rows
from tests.export_archive_support import PASSPHRASE, SECRET_VALUE
from tests.object_support import hidden_values, visible_text
from tests.stack_import_support import (
    BIND_STACK_FILE,
    DATA_VOLUME,
    NOTE,
    NOTE_FILE,
    SOURCE,
    WRONG_PASSPHRASE,
    StackImportEngine,
    build_archive,
)
from tests.stack_support import install_scripted_compose, job_id_of, settle_stack_job
from tests.web.environment_support import World
from tests.web.flash_support import flash_texts, land

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN = 200, 303, 400, 403
NOT_FOUND, CONFLICT, UNPROCESSABLE, TOO_MANY = 404, 409, 422, 429
BOUNDARY = "xx-stack-import-xx"
RENAMED = "copy"


@pytest.fixture
async def import_host(world: World) -> AsyncIterator[StackImportEngine]:
    fake = StackImportEngine()
    fake.add_image("nginx:1")
    engine = world.app.state.engine
    registry = EngineClientRegistry(lambda settings: fake.bundle())
    engine.registry = registry
    await install_scripted_compose(engine.compose, fake)
    yield fake
    for job in list(engine.stack_jobs._jobs.values()):
        if job.task is not None and not job.task.done():
            job.task.cancel()
    fake.release_gates()
    await registry.aclose()


def base(world: World) -> str:
    return f"/environments/{world.remote_id}/stacks"


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
    path: str | None = None,
) -> Response:
    data = archive if archive is not None else await build_archive()
    token_part = ("csrf_token", None, (token or csrf_token_of(client)).encode())
    file_part = ("archive", "export.tar.gz", data)
    parts = [file_part, token_part] if file_first else [token_part, file_part]
    body, headers = multipart(parts)
    return await client.post(path or f"{base(world)}/-/import", content=body, headers=headers)


def spooled(world: World) -> list[Path]:
    directory = world.app.state.settings.data_dir / SPOOL_DIRECTORY
    return sorted(directory.glob("*")) if directory.exists() else []


async def rows(world: World, action: str) -> list[dict[str, object]]:
    async with world.app.state.database.session() as db:
        return [r for r in await activity_rows(db) if r["action"] == action]


async def reviewed(client: AsyncClient, world: World, archive: bytes | None = None) -> str:
    response = await upload(client, world, archive)
    assert response.status_code == SEE_OTHER, response.text
    return str(response.headers["location"])


def import_fields(client: AsyncClient, review: Response, **fields: str) -> dict[str, str]:
    return csrf_form(
        client,
        expected_plan=hidden_values(review, "expected_plan")[0],
        name=SOURCE,
        action="import",
        **fields,
    )


async def unlocked(client: AsyncClient, location: str, passphrase: str = PASSPHRASE) -> Response:
    return await client.post(f"{location}/unlock", data=csrf_form(client, passphrase=passphrase))


async def test_the_list_offers_import_to_an_administrator_only(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    import_host: StackImportEngine,
) -> None:
    admin = await admin_client.get(base(world))
    standard = await standard_client.get(base(world))

    assert f'href="{base(world)}/-/import"' in admin.text and ">Import stack<" in admin.text
    assert "/-/import" not in standard.text


async def test_the_upload_form_posts_the_token_before_the_file_and_works_without_script(
    admin_client: AsyncClient, world: World, import_host: StackImportEngine
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
    import_host: StackImportEngine,
) -> None:
    location = await reviewed(admin_client, world)
    held = spooled(world)

    responses = [
        await standard_client.get(f"{base(world)}/-/import"),
        await upload(standard_client, world),
        await standard_client.get(location),
        await standard_client.post(location, data=csrf_form(standard_client, name=SOURCE)),
        await standard_client.post(f"{location}/unlock", data=csrf_form(standard_client)),
        await standard_client.post(f"{location}/cancel", data=csrf_form(standard_client)),
    ]

    assert [r.status_code for r in responses] == [NOT_FOUND] * 6
    assert await rows(world, "stack_imported") == [] and spooled(world) == held
    assert import_host.calls("POST", "/volumes/create") == []


async def test_a_wrong_token_is_refused_before_a_file_byte_is_stored(
    admin_client: AsyncClient, world: World, import_host: StackImportEngine
) -> None:
    wrong = await upload(admin_client, world, token="x" * 43)
    file_first = await upload(admin_client, world, file_first=True)

    assert wrong.status_code == FORBIDDEN and file_first.status_code == BAD_REQUEST
    assert spooled(world) == [] and import_host.requests == []


async def test_the_review_names_what_the_archive_creates_and_never_a_secret_value(
    admin_client: AsyncClient, world: World, import_host: StackImportEngine
) -> None:
    location = await reviewed(admin_client, world)
    review = await admin_client.get(location)
    text = visible_text(review)

    assert review.status_code == OK and review.headers["cache-control"] == "no-store"
    assert "shop-web-1" in text and "shop_default" in text and DATA_VOLUME in text
    assert "Format version 1" in text and "DB_PASSWORD" in text and "tls.key" in text
    assert "Sealed values" in text and "Open the sealed values" in text
    assert hidden_values(review, "expected_plan") and hidden_values(review, "csrf_token")
    assert "hx-" not in review.text.split("<main", 1)[1]
    assert SECRET_VALUE not in review.text and PASSPHRASE not in review.text
    assert import_host.calls("POST", "/volumes/create") == []


async def test_a_wrong_passphrase_is_counted_and_never_shown_again(
    admin_client: AsyncClient, world: World, import_host: StackImportEngine
) -> None:
    location = await reviewed(admin_client, world)

    failed = await unlocked(admin_client, location, WRONG_PASSPHRASE)

    assert failed.status_code == UNPROCESSABLE and "4 attempts left" in visible_text(failed)
    assert WRONG_PASSPHRASE not in failed.text
    assert (
        'type="password"' in failed.text
        and 'value=""' not in failed.text.split('name="passphrase"')[1].split(">")[0]
    )


async def test_an_archive_is_opened_reviewed_and_imported_then_its_deployment_runs(
    admin_client: AsyncClient, world: World, import_host: StackImportEngine
) -> None:
    location = await reviewed(admin_client, world)
    opened = await unlocked(admin_client, location)
    assert opened.status_code == SEE_OTHER and flash_texts(
        (await land(admin_client, opened)).text
    ) == ["The sealed values were opened."]
    review = await admin_client.get(location)

    response = await admin_client.post(location, data=import_fields(admin_client, review))
    job_id = job_id_of(response)
    await settle_stack_job(world.app, job_id)
    landed = await land(admin_client, response)
    again = await admin_client.get(f"{base(world)}/shop")

    assert flash_texts(landed.text)[0].startswith("Imported compose stack shop: 1 volume written")
    for page in (opened, review, response, landed, again):
        assert SECRET_VALUE not in page.text and PASSPHRASE not in page.text
    assert (
        DATA_VOLUME in import_host.volumes
        and import_host.files_of(DATA_VOLUME)[NOTE_FILE].data == NOTE
    )
    [imported] = await rows(world, "stack_imported")
    assert imported["result"] == "success" and imported["target_id"] == SOURCE
    deployed = await rows(world, "stack_deployed")
    assert deployed and deployed[-1]["result"] == "success"
    assert spooled(world) == [] and import_host.helpers() == []
    assert [box.labels["com.docker.compose.project"] for box in import_host.boxes.values()] == [
        SOURCE
    ]


async def test_a_rename_is_checked_without_importing_and_an_import_after_it_needs_a_fresh_plan(
    admin_client: AsyncClient, world: World, import_host: StackImportEngine
) -> None:
    location = await reviewed(admin_client, world)
    review = await admin_client.get(location)

    checked = await admin_client.post(
        location, data=import_fields(admin_client, review) | {"name": RENAMED, "action": "check"}
    )
    stale = await admin_client.post(
        location, data=import_fields(admin_client, review) | {"name": RENAMED}
    )

    assert checked.status_code == OK and f"{RENAMED}_data" in visible_text(checked)
    assert hidden_values(checked, "expected_plan") != hidden_values(review, "expected_plan")
    assert stale.status_code == CONFLICT and "changed since you reviewed" in visible_text(stale)
    assert import_host.calls("POST", "/volumes/create") == []


async def test_a_collision_is_listed_and_the_import_button_is_withheld(
    admin_client: AsyncClient, world: World, import_host: StackImportEngine
) -> None:
    import_host.add_volume(DATA_VOLUME)
    location = await reviewed(admin_client, world)

    review = await admin_client.get(location)
    text = visible_text(review)

    assert "Names already in use" in text and DATA_VOLUME in text
    assert 'value="import"' not in review.text and 'value="check"' in review.text


async def test_host_paths_are_offered_per_path_with_no_choice_made(
    admin_client: AsyncClient, world: World, import_host: StackImportEngine
) -> None:
    archive = await build_archive(text=BIND_STACK_FILE, volumes={})
    location = await reviewed(admin_client, world, archive)
    await unlocked(admin_client, location)

    review = await admin_client.get(location)
    text = visible_text(review)

    assert "/srv/site" in text and "/srv/logs" in text and "Keep the path" in text
    assert "checked" not in review.text.split("Host paths", 1)[1].split("</section>")[0]
    refused = await admin_client.post(location, data=import_fields(admin_client, review))
    assert (
        refused.status_code == CONFLICT
        and "Choose what to do with each host path" in visible_text(refused)
    )


@pytest.mark.parametrize(
    ("archive", "message"),
    [
        pytest.param(b"not an archive at all", "not a gzip archive", id="not-gzip"),
        pytest.param(b"\x1f\x8b not really", "not a readable export archive", id="bad-gzip"),
    ],
)
async def test_a_refused_archive_answers_the_upload_page_with_the_reason_and_stores_nothing(
    admin_client: AsyncClient,
    world: World,
    import_host: StackImportEngine,
    archive: bytes,
    message: str,
) -> None:
    response = await upload(admin_client, world, archive)

    assert response.status_code == UNPROCESSABLE and message in visible_text(response)
    assert spooled(world) == [] and import_host.requests == []
    assert await rows(world, "stack_imported") == []


async def test_a_discarded_upload_is_removed_and_its_review_is_gone(
    admin_client: AsyncClient, world: World, import_host: StackImportEngine
) -> None:
    location = await reviewed(admin_client, world)
    assert len(spooled(world)) == 1

    cancelled = await admin_client.post(f"{location}/cancel", data=csrf_form(admin_client))
    again = await admin_client.get(location)

    assert cancelled.status_code == SEE_OTHER and spooled(world) == []
    assert again.status_code == CONFLICT and "no longer held" in visible_text(again)


async def test_a_deployment_that_cannot_start_leaves_the_stack_and_says_so(
    admin_client: AsyncClient,
    world: World,
    import_host: StackImportEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.web.stack_import_routes as routes

    def refuse(*args: object, **kwargs: object) -> str:
        raise TooManyStackJobs("user")

    monkeypatch.setattr(routes, "start_stack_job", refuse)
    location = await reviewed(admin_client, world)
    await unlocked(admin_client, location)
    review = await admin_client.get(location)

    response = await admin_client.post(location, data=import_fields(admin_client, review))

    assert response.status_code == TOO_MANY
    text = visible_text(response)
    assert "The stack was not deployed" in text and SOURCE in text
    assert [r["result"] for r in await rows(world, "stack_imported")] == ["success"]
    await asyncio.sleep(0)


async def test_an_invalid_name_is_shown_on_the_field_and_nothing_is_imported(
    admin_client: AsyncClient, world: World, import_host: StackImportEngine
) -> None:
    location = await reviewed(admin_client, world)
    review = await admin_client.get(location)

    checked = await admin_client.post(
        location,
        data=import_fields(admin_client, review) | {"name": "Not A Name!", "action": "check"},
    )

    assert checked.status_code == OK and 'value="Not A Name!"' in checked.text
    assert "aria-invalid" in checked.text and import_host.calls("POST", "/volumes/create") == []
