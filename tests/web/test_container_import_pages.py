"""The container import pages: upload, review, passphrase, networks and volumes, then the form."""

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient, Response

from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.docker_engine.registry import EngineClientRegistry
from app.services.export_build import EnvironmentInput
from app.services.stack_import_store import SPOOL_DIRECTORY, UNSEAL_FAILURE_LIMIT
from tests.auth_support import ADMIN_ID, STANDARD_ID, csrf_form, csrf_token_of
from tests.container_import_support import (
    BASE_FORM,
    CONTAINER_NAME,
    FIXTURE,
    HOST_PATH,
    IMAGE,
    NETWORK,
    NOTE,
    NOTE_FILE,
    PASSPHRASE,
    PLAIN_NAME,
    PLAIN_VALUE,
    SECRET_NAME,
    SECRET_VALUE,
    SUBNET,
    VOLUME,
    WRONG_PASSPHRASE,
    ImportHost,
    container_archive,
    network_entry,
    with_manifest,
)
from tests.environment_support import activity_rows
from tests.export_archive_support import FIXTURE_PASSPHRASE
from tests.object_support import hidden_values, visible_text
from tests.self_support import SELF_VOLUME, home_identity
from tests.stack_import_support import build_archive
from tests.stack_support import logged_text
from tests.web.environment_support import World
from tests.web.flash_support import flash_texts, land
from tests.web.helpers import form_values, inputs_named, parse_tags, value_of
from tests.web.image_support import make_set
from tests.web.test_stack_import_pages import multipart

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN, NOT_FOUND = 200, 303, 400, 403, 404
CONFLICT, UNPROCESSABLE, TOO_MANY, BAD_GATEWAY = 409, 422, 429, 502
ACTION = "container_imported"


@pytest.fixture
async def import_host(world: World) -> AsyncIterator[ImportHost]:
    fake = ImportHost()
    engine = world.app.state.engine
    registry = EngineClientRegistry(lambda settings: fake.bundle())
    engine.registry = registry
    async with world.app.state.database.session() as db:
        await EnvironmentAccessRepository(db).add_user("environment", world.remote_id, STANDARD_ID)
    yield fake
    fake.release_gates()
    await registry.aclose()


def base(world: World) -> str:
    return f"/environments/{world.remote_id}/containers"


def new_form(world: World) -> str:
    return f"{base(world)}/new"


async def upload(
    client: AsyncClient,
    world: World,
    archive: bytes | None = None,
    *,
    token: str | None = None,
    file_first: bool = False,
    path: str | None = None,
) -> Response:
    data = archive if archive is not None else await container_archive()
    token_part = ("csrf_token", None, (token or csrf_token_of(client)).encode())
    file_part = ("archive", "export.tar.gz", data)
    body, headers = multipart([file_part, token_part] if file_first else [token_part, file_part])
    return await client.post(path or f"{base(world)}/-/import", content=body, headers=headers)


async def reviewed(client: AsyncClient, world: World, archive: bytes | None = None) -> str:
    response = await upload(client, world, archive)
    assert response.status_code == SEE_OTHER, response.text
    return str(response.headers["location"])


def spooled(world: World) -> list[Path]:
    directory = world.app.state.settings.data_dir / SPOOL_DIRECTORY
    return sorted(directory.glob("*")) if directory.exists() else []


async def rows(world: World, action: str = ACTION) -> list[dict[str, object]]:
    async with world.app.state.database.session() as db:
        return [r for r in await activity_rows(db) if r["action"] == action]


async def unlocked(client: AsyncClient, location: str, passphrase: str = PASSPHRASE) -> Response:
    return await client.post(f"{location}/unlock", data=csrf_form(client, passphrase=passphrase))


async def confirm(client: AsyncClient, location: str, review: Response, **fields: str) -> Response:
    data = csrf_form(
        client, expected_plan=hidden_values(review, "expected_plan")[0], action="import", **fields
    )
    return await client.post(location, data=data)


async def opened_review(
    client: AsyncClient, world: World, archive: bytes | None = None
) -> tuple[str, Response]:
    location = await reviewed(client, world, archive)
    await unlocked(client, location)
    return location, await client.get(location)


def imported_id(response: Response) -> str:
    query = parse_qs(urlsplit(str(response.headers["location"])).query)
    return query["imported"][0]


async def finished(
    client: AsyncClient, world: World, archive: bytes | None = None, **fields: str
) -> tuple[str, Response]:
    location, review = await opened_review(client, world, archive)
    response = await confirm(client, location, review, **fields)
    assert response.status_code == SEE_OTHER, response.text
    return location, response


async def test_the_list_offers_import_to_an_administrator_only(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    import_host: ImportHost,
) -> None:
    admin = await admin_client.get(base(world))
    standard = await standard_client.get(base(world))

    assert f'href="{base(world)}/-/import"' in admin.text and ">Import container<" in admin.text
    assert "/-/import" not in standard.text


async def test_the_upload_form_posts_the_token_before_the_file_and_works_without_script(
    admin_client: AsyncClient, world: World, import_host: ImportHost
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
    import_host: ImportHost,
) -> None:
    location = await reviewed(admin_client, world)
    token = csrf_form(standard_client)

    answers = [
        await standard_client.get(f"{base(world)}/-/import"),
        await upload(standard_client, world),
        await standard_client.get(location),
        await standard_client.post(location, data=csrf_form(standard_client, action="import")),
        await standard_client.post(f"{location}/unlock", data={**token, "passphrase": PASSPHRASE}),
        await standard_client.post(f"{location}/cancel", data=token),
    ]

    assert {answer.status_code for answer in answers} == {NOT_FOUND}
    assert await rows(world) == [] and "create" not in import_host.verbs()
    assert len(spooled(world)) == 1


async def test_a_wrong_token_or_a_file_before_the_token_is_refused_and_nothing_is_kept(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    wrong = await upload(admin_client, world, token="W" * 43)
    file_first = await upload(admin_client, world, file_first=True)

    assert wrong.status_code == FORBIDDEN
    assert file_first.status_code in (BAD_REQUEST, FORBIDDEN)
    assert spooled(world) == [] and await rows(world) == []


async def test_the_review_lists_networks_volumes_and_variables_and_shows_no_value(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    location = await reviewed(admin_client, world)

    page = await admin_client.get(location)

    text = visible_text(page)
    assert page.status_code == OK and page.headers["cache-control"] == "no-store"
    assert NETWORK in text and "Created from the archive's options" in text
    assert VOLUME in text and CONTAINER_NAME in text
    assert PLAIN_NAME in text and SECRET_NAME in text
    assert PLAIN_VALUE not in page.text and SECRET_VALUE not in page.text
    assert "Sealed values" in text and 'type="password"' in page.text
    assert hidden_values(page, "expected_plan") and "hx-" not in page.text.split("<main", 1)[1]
    assert import_host.mutations() == [] and await rows(world) == []


async def test_an_archive_imports_its_network_and_volume_then_the_form_creates_the_container(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    _, response = await finished(admin_client, world)

    form = await land(admin_client, response)
    values = form_values(form.text)
    created = await admin_client.post(f"{base(world)}", data=csrf_form(admin_client, **values))
    box = next(b for b in import_host.boxes.values() if b.name == CONTAINER_NAME)

    assert response.headers["location"].startswith(new_form(world) + "?imported=")
    assert flash_texts(form.text)[0].startswith("Created 1 network and 1 volume.")
    assert values["name"] == CONTAINER_NAME and values["image"] == IMAGE
    assert values["env-1-key"] == SECRET_NAME and values["env-1-value"] == SECRET_VALUE
    assert values["mount-0-source"] == VOLUME and values["network-0-name"] == NETWORK
    assert "Imported from the archive of" in form.text and NETWORK in form.text
    assert created.status_code == SEE_OTHER
    assert f"{SECRET_NAME}={SECRET_VALUE}" in (box.config or {})["Env"]
    assert import_host.files_of(VOLUME)[NOTE_FILE].data == NOTE
    assert [b["Name"] for b in import_host.network_bodies("create")] == [NETWORK]
    assert import_host.network_bodies("create")[0]["IPAM"]["Config"] == [{"Subnet": SUBNET}]
    assert import_host.helpers() == [] and spooled(world) == []
    (row,) = await rows(world)
    assert (row["result"], row["target_type"], row["target_id"]) == (
        "success",
        "container",
        CONTAINER_NAME,
    )
    assert row["actor_user_id"] == ADMIN_ID
    assert {
        key: row["detail"][key]  # type: ignore[index]
        for key in ("networks_created", "volumes_created", "environment", "secret_values")
    } == {"networks_created": 1, "volumes_created": 1, "environment": 2, "secret_values": 1}


async def test_environment_values_and_the_passphrase_reach_no_url_log_line_or_row(
    admin_client: AsyncClient,
    world: World,
    import_host: ImportHost,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    location, review = await opened_review(admin_client, world)
    unlock_failure = await unlocked(admin_client, location, WRONG_PASSPHRASE)
    response = await confirm(admin_client, location, review)
    form = await admin_client.get(response.headers["location"])
    await admin_client.post(
        f"{base(world)}", data=csrf_form(admin_client, **form_values(form.text))
    )

    urls = [location, str(response.headers["location"])]
    async with world.app.state.database.session() as db:
        stored = str(await activity_rows(db))
    pages = [review.text, unlock_failure.text, form.text]
    assert unlock_failure.status_code == UNPROCESSABLE
    assert not any(SECRET_VALUE in url or PASSPHRASE in url for url in urls)
    assert SECRET_VALUE not in logged_text(caplog) and PASSPHRASE not in logged_text(caplog)
    assert SECRET_VALUE not in stored and PASSPHRASE not in stored
    assert WRONG_PASSPHRASE not in stored and WRONG_PASSPHRASE not in logged_text(caplog)
    assert SECRET_VALUE not in pages[0] and SECRET_VALUE not in pages[1]
    assert PASSPHRASE not in "".join(pages) and WRONG_PASSPHRASE not in "".join(pages)


async def test_the_passphrase_has_five_attempts_then_the_session_is_limited(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    location = await reviewed(admin_client, world)

    wrong = [await unlocked(admin_client, location, WRONG_PASSPHRASE) for _ in range(5)]
    limited = await unlocked(admin_client, location, PASSPHRASE)

    assert [r.status_code for r in wrong] == [UNPROCESSABLE] * UNSEAL_FAILURE_LIMIT
    assert "4 attempts left" in visible_text(wrong[0]) and "0 attempts left" in visible_text(
        wrong[4]
    )
    assert limited.status_code == TOO_MANY


async def test_omitted_values_are_entered_again_and_an_import_without_them_is_refused(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    archive = await container_archive(passphrase=None)
    location = await reviewed(admin_client, world, archive)
    review = await admin_client.get(location)

    refused = await confirm(admin_client, location, review)
    done = await confirm(
        admin_client,
        location,
        review,
        **{"secret-0-name": "environment:1", "secret-0-value": "typed-by-hand-77"},
    )

    form = await admin_client.get(done.headers["location"])
    assert "Sealed values" not in visible_text(review)
    assert "The archive carries only the name" in review.text
    assert refused.status_code == CONFLICT
    assert [r["result"] for r in await rows(world)] == ["failure", "success"]
    assert form_values(form.text)["env-1-value"] == "typed-by-hand-77"


async def test_an_existing_volume_name_is_refused_with_another_offered_and_a_rename_is_followed(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    import_host.add_volume(VOLUME)
    location, review = await opened_review(admin_client, world)

    refused = await confirm(
        admin_client, location, review, **{"volume-0-source": VOLUME, "volume-0-name": VOLUME}
    )
    again = await admin_client.get(location)
    renamed = await admin_client.post(
        location,
        data=csrf_form(
            admin_client,
            action="check",
            **{"volume-0-source": VOLUME, "volume-0-name": f"{VOLUME}-2"},
        ),
    )
    done = await confirm(
        admin_client,
        location,
        renamed,
        **{"volume-0-source": VOLUME, "volume-0-name": f"{VOLUME}-2"},
    )

    form = await admin_client.get(done.headers["location"])
    assert refused.status_code == CONFLICT
    assert "already exists" in visible_text(again) and f"{VOLUME}-2" in visible_text(again)
    assert import_host.files_of(VOLUME) == {}
    assert import_host.files_of(f"{VOLUME}-2")[NOTE_FILE].data == NOTE
    assert form_values(form.text)["mount-0-source"] == f"{VOLUME}-2"


async def test_an_existing_network_is_reused_and_what_differs_is_listed(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    import_host.add_network(NETWORK, 0x77, "bridge", Internal=True)
    location = await reviewed(admin_client, world)
    await unlocked(admin_client, location)

    review = await admin_client.get(location)
    done = await confirm(admin_client, location, review)

    assert "Exists and is used as it is" in visible_text(review) and "internal" in visible_text(
        review
    )
    assert done.status_code == SEE_OTHER
    assert import_host.network_bodies("create") == []


async def test_a_network_the_archive_describes_badly_blocks_the_import_and_creates_nothing(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    bad = network_entry(labels={"com.docker.compose.project": "shop"})
    archive = await container_archive(networks=(bad,))
    location, review = await opened_review(admin_client, world, archive)

    refused = await confirm(admin_client, location, review)

    assert "Cannot be created" in visible_text(review)
    assert refused.status_code == CONFLICT
    assert import_host.mutations() == []
    assert [r["result"] for r in await rows(world)] == ["failure"]


async def test_host_paths_need_a_choice_and_keep_or_replace_changes_the_form(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    form = {"mount-1-type": "bind", "mount-1-source": HOST_PATH, "mount-1-target": "/host"}
    archive = await container_archive(form=form, volumes={})
    location, review = await opened_review(admin_client, world, archive)
    row = "path-0-source"

    undecided = await confirm(admin_client, location, review)
    kept = await confirm(
        admin_client, location, review, **{row: HOST_PATH, "path-0-choice": "keep"}
    )
    kept_form = form_values((await admin_client.get(kept.headers["location"])).text)
    again, review2 = await opened_review(admin_client, world, archive)
    replaced = await confirm(
        admin_client,
        again,
        review2,
        **{row: HOST_PATH, "path-0-choice": "volume", "path-0-name": "site-data"},
    )
    replaced_form = form_values((await admin_client.get(replaced.headers["location"])).text)

    assert HOST_PATH in visible_text(review) and undecided.status_code == CONFLICT
    assert kept_form["mount-1-type"] == "bind" and kept_form["mount-1-source"] == HOST_PATH
    assert (
        replaced_form["mount-1-type"] == "volume" and replaced_form["mount-1-source"] == "site-data"
    )
    assert HOST_PATH not in str(replaced_form.values())
    assert [r["detail"]["host_paths_kept"] for r in await rows(world)][-2:] == [1, 0]  # type: ignore[index]
    assert not any(b["Name"] == "site-data" for b in import_host.volume_bodies())


async def test_a_stack_archive_and_a_newer_format_and_a_damaged_file_are_refused_at_upload(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    stack = await upload(admin_client, world, await build_archive())
    newer = await upload(
        admin_client, world, with_manifest(await container_archive(), format_version=2)
    )
    damaged = await upload(admin_client, world, (await container_archive())[:60])

    assert stack.status_code == UNPROCESSABLE and "holds a stack" in stack.text
    assert newer.status_code == UNPROCESSABLE and "format version is 2" in visible_text(newer)
    assert damaged.status_code == UNPROCESSABLE
    assert spooled(world) == [] and await rows(world) == []


async def test_a_change_on_the_host_between_review_and_import_is_refused_without_creating_anything(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    location, review = await opened_review(admin_client, world)
    import_host.add_network(NETWORK, 0x77, "bridge")

    refused = await confirm(admin_client, location, review)

    assert refused.status_code == CONFLICT and "changed since you reviewed" in visible_text(refused)
    assert import_host.volume_bodies() == []
    (row,) = await rows(world)
    assert row["result"] == "failure" and row["detail"]["networks_created"] == 0  # type: ignore[index]


async def test_a_failed_write_removes_what_the_import_created_in_reverse_order(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    import_host.fail_after_members = 0
    location, review = await opened_review(admin_client, world)

    failed = await confirm(admin_client, location, review)

    assert failed.status_code == BAD_GATEWAY
    assert "Every object this import created was removed" in visible_text(failed)
    assert VOLUME not in import_host.volumes and not import_host.helpers()
    assert not [n for n in import_host.networks.values() if n["Name"] == NETWORK]
    (row,) = await rows(world)
    assert row["result"] == "failure"
    assert row["detail"]["objects_removed"] == f"volume {VOLUME},network {NETWORK}"  # type: ignore[index]
    assert spooled(world) == []


async def test_the_held_form_belongs_to_the_session_and_is_released_when_the_container_is_created(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    import_host: ImportHost,
) -> None:
    _, response = await finished(admin_client, world)
    path = response.headers["location"]

    first = await admin_client.get(path)
    other = await standard_client.get(path)
    junk = await admin_client.get(f"{new_form(world)}?imported=not-an-id")
    created = await admin_client.post(
        f"{base(world)}", data=csrf_form(admin_client, **form_values(first.text))
    )
    after = await admin_client.get(path)

    assert inputs_named(first.text, "imported_ref") and value_of(
        first.text, "imported_ref"
    ) == imported_id(response)
    assert SECRET_VALUE in first.text
    assert SECRET_VALUE not in other.text and "no longer held" in visible_text(other)
    assert "no longer held" in visible_text(junk) and created.status_code == SEE_OTHER
    assert "no longer held" in visible_text(after) and SECRET_VALUE not in after.text


async def test_a_discarded_upload_is_removed_and_a_second_review_is_gone(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    location = await reviewed(admin_client, world)

    discarded = await admin_client.post(f"{location}/cancel", data=csrf_form(admin_client))
    gone = await admin_client.get(location)

    assert discarded.status_code == SEE_OTHER and spooled(world) == []
    assert gone.status_code == CONFLICT and "no longer held" in visible_text(gone)
    assert await rows(world) == []


async def test_a_plain_environment_variable_needs_no_passphrase_and_none_is_asked_for(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    archive = await container_archive(
        environment=(EnvironmentInput(PLAIN_NAME, False, PLAIN_VALUE),)
    )
    location = await reviewed(admin_client, world, archive)

    review = await admin_client.get(location)
    done = await confirm(admin_client, location, review)

    form = form_values((await admin_client.get(done.headers["location"])).text)
    assert "Sealed values" not in visible_text(review)
    assert form["env-0-key"] == PLAIN_NAME and form["env-0-value"] == PLAIN_VALUE
    assert BASE_FORM["image"] == form["image"]


RESTRICTED_FORM = {
    "privileged": "on",
    "cap_add": "SYS_ADMIN",
    "pid_host": "on",
    "mount-1-type": "bind",
    "mount-1-source": HOST_PATH,
    "mount-1-target": "/host",
}


async def imported_form(client: AsyncClient, world: World, archive: bytes) -> dict[str, str]:
    location, review = await opened_review(client, world, archive)
    done = await confirm(
        client, location, review, **{"path-0-source": HOST_PATH, "path-0-choice": "keep"}
    )
    assert done.status_code == SEE_OTHER, done.text
    return form_values((await land(client, done)).text)


async def test_restricted_options_in_an_archive_are_refused_to_a_standard_user_who_posts_them(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    import_host: ImportHost,
) -> None:
    archive = await container_archive(form=RESTRICTED_FORM, volumes={})
    values = await imported_form(admin_client, world, archive)

    standard = await standard_client.post(
        new_form(world), data=csrf_form(standard_client, **values, intent="review")
    )
    admin = await admin_client.post(
        new_form(world), data=csrf_form(admin_client, **values, intent="review")
    )

    assert standard.status_code == BAD_REQUEST
    for name in ("privileged", "cap_add", "pid_host", "mount-1-type"):
        assert f'id="field-{name}"' in standard.text or name in standard.text
    assert admin.status_code == OK and "Administrator options in use" in admin.text
    assert "create" not in import_host.verbs()


async def test_a_form_that_mounts_dockmins_own_volume_is_refused_to_an_administrator(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    import_host.add_volume(SELF_VOLUME)
    world.app.state.self_identity = home_identity(world.remote_id, volumes=(SELF_VOLUME,))
    archive = await container_archive(
        form={"mount-0-source": SELF_VOLUME, "network_mode": "bridge", "network-0-name": ""},
        networks=(),
        volumes={},
    )
    values = await imported_form(admin_client, world, archive)

    created = await admin_client.post(base(world), data=csrf_form(admin_client, **values))

    assert created.status_code == CONFLICT
    assert "create" not in import_host.verbs()


async def test_the_form_an_archive_fills_offers_the_credential_picker_for_its_image(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    reference = "registry.example.com/app:1"
    await make_set(admin_client, world, "alpha", default=True)
    archive = await container_archive(
        form={"image": reference, "pull": "missing"}, networks=(), volumes={}
    )
    location, review = await opened_review(admin_client, world, archive)
    done = await confirm(admin_client, location, review)

    form = await land(admin_client, done)

    assert "anonymous" in [t.attrs.get("value") for t in parse_tags(form.text)]
    assert form_values(form.text)["image"] == reference


async def test_a_format_version_1_container_fixture_imports_with_its_own_passphrase(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    archive = FIXTURE.read_bytes()
    location = await reviewed(admin_client, world, archive)

    wrong = await unlocked(admin_client, location, PASSPHRASE)
    right = await unlocked(admin_client, location, FIXTURE_PASSPHRASE)
    review = await admin_client.get(location)
    done = await confirm(admin_client, location, review)

    values = form_values((await land(admin_client, done)).text)
    assert wrong.status_code == UNPROCESSABLE and right.status_code == SEE_OTHER
    assert values["env-1-key"] == "TOKEN" and values["env-1-value"] == "fixture-token"
    assert values["mount-0-source"] == "shop-data" and values["name"] == "web"
    assert import_host.files_of("shop-data")["a.txt"].data == b"hello\n"


@pytest.mark.parametrize("driver", ["overlay", "macvlan"])
async def test_a_network_the_environment_cannot_create_blocks_the_import(
    admin_client: AsyncClient, world: World, import_host: ImportHost, driver: str
) -> None:
    archive = await container_archive(
        networks=(network_entry(driver=driver, pools=[]),), volumes={}
    )
    location, review = await opened_review(admin_client, world, archive)

    refused = await confirm(admin_client, location, review)

    assert "Cannot be created" in visible_text(review) and refused.status_code == CONFLICT
    assert import_host.mutations() == []


async def test_a_container_archive_sent_to_the_stack_import_is_sent_to_the_containers_list(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    path = f"/environments/{world.remote_id}/stacks/-/import"

    refused = await upload(admin_client, world, path=path)

    assert refused.status_code == UNPROCESSABLE
    assert "Import it from the Containers list" in visible_text(refused)
    assert spooled(world) == []


async def test_a_container_name_already_on_the_host_is_left_to_the_forms_name_field(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    import_host.add("c" * 64, CONTAINER_NAME, image=IMAGE)
    location, review = await opened_review(admin_client, world)
    done = await confirm(admin_client, location, review)
    form = await land(admin_client, done)

    created = await admin_client.post(
        base(world), data=csrf_form(admin_client, **form_values(form.text))
    )

    assert "form needs another name" in visible_text(review)
    assert done.status_code == SEE_OTHER and created.status_code == CONFLICT
    assert "A container with this name already exists." in visible_text(created)


async def test_the_start_option_of_the_archive_decides_whether_the_container_is_started(
    admin_client: AsyncClient, world: World, import_host: ImportHost
) -> None:
    archive = await container_archive(form={"start": ""})
    _, done = await finished(admin_client, world, archive)
    form = await land(admin_client, done)

    created = await admin_client.post(
        base(world), data=csrf_form(admin_client, **form_values(form.text))
    )

    assert "start" not in form_values(form.text)
    assert created.status_code == SEE_OTHER and created.headers["location"].endswith(
        "created=stopped"
    )
    assert "start" not in import_host.verbs()
