"""The stack file JSON routes: files on the detail and create, and PUT and DELETE of one file."""

import json
from typing import Any

import pytest
from httpx import AsyncClient

from tests.api.test_stacks_api import (
    BAD_REQUEST,
    CONFLICT,
    CREATED,
    FORBIDDEN,
    NOT_FOUND,
    OK,
    PAYLOAD_TOO_LARGE,
    UNAUTHENTICATED,
    UNPROCESSABLE,
    WEB_SERVICE,
    ZERO_DIGEST,
    base,
    stack,
)
from tests.auth_support import Browsers, csrf_headers
from tests.container_support import plant_control
from tests.docker_engine.container_engine import ContainerEngine
from tests.stack_support import install_scripted_compose
from tests.web.environment_support import World, bearer, issue_token
from tests.web.stack_file_support import (
    PLAIN_BODY,
    PLAIN_NAME,
    SECRET_BODY,
    SECRET_NAME,
    UPDATED_ROWS,
    seed_stack,
    stack_rows,
)

NO_CONTENT = 204
NAME = "alpha"
IMAGE = "nginx:1"
REFERENCING = f"services:\n  web:\n    image: {IMAGE}\n    env_file: {PLAIN_NAME}\n"
RESERVED_NAME = "compose.yaml"
EDITED_BODY = "edited-visible-content\n"
NEW_SECRET_BODY = "REPLACEMENT-SECRET-SENTINEL-6120\n"
MAX_FILE_BYTES = 65536
PLAIN = {"name": PLAIN_NAME, "content": PLAIN_BODY, "secret": False}
SECRET = {"name": SECRET_NAME, "content": SECRET_BODY, "secret": True}
FILE_KEYS = {"name", "secret", "size", "digest", "content", "used_as"}


@pytest.fixture
async def stack_host(world: World, container_host: ContainerEngine) -> ContainerEngine:
    container_host.add_image(IMAGE)
    await install_scripted_compose(world.app.state.engine.compose, container_host)
    return container_host


@pytest.fixture
async def admin_api(browsers: Browsers, admin_client: AsyncClient) -> AsyncClient:
    caller = browsers.open()
    caller.headers.update(bearer(await issue_token(admin_client)))
    return caller


@pytest.fixture
async def user_api(browsers: Browsers, standard_client: AsyncClient) -> AsyncClient:
    caller = browsers.open()
    caller.headers.update(bearer(await issue_token(standard_client)))
    return caller


def file_url(world: World, name: str, file: str) -> str:
    return f"{base(world)}/{name}/files/{file}"


def error_of(reply: Any) -> dict[str, Any]:
    found: dict[str, Any] = reply.json()["error"]
    return found


async def detail_of(client: AsyncClient, world: World, name: str = NAME) -> dict[str, Any]:
    found: dict[str, Any] = (await client.get(f"{base(world)}/{name}")).json()
    return found


def by_name(detail: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in detail["files"]}


async def test_a_stack_created_with_files_lists_them_and_never_returns_secret_content(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    created = await admin_api.post(
        base(world), json=stack(NAME, WEB_SERVICE, files=[PLAIN, SECRET])
    )
    detail = await detail_of(admin_api, world)
    listing = await admin_api.get(base(world))
    events = await stack_rows(
        world,
        "SELECT action, json_extract(detail, '$.files') AS files, "
        "json_extract(detail, '$.secret_files') AS secret_files, detail FROM activity_event "
        "WHERE target_type = 'stack' ORDER BY id",
    )

    files = by_name(detail)
    assert created.status_code == CREATED
    assert {entry["name"]: set(entry) for entry in detail["files"]} == {
        PLAIN_NAME: FILE_KEYS,
        SECRET_NAME: FILE_KEYS,
    }
    assert files[PLAIN_NAME]["content"] == PLAIN_BODY
    assert (files[PLAIN_NAME]["size"], files[PLAIN_NAME]["used_as"]) == (len(PLAIN_BODY), [])
    assert len(files[PLAIN_NAME]["digest"]) == 64
    assert files[SECRET_NAME] == {
        "name": SECRET_NAME,
        "secret": True,
        "size": None,
        "digest": None,
        "content": None,
        "used_as": [],
    }
    assert len(detail["inputs_digest"]) == 64
    assert detail["deployed_inputs_digest"] == detail["inputs_digest"]
    assert detail["changed_since_deploy"] is False
    assert "files" not in listing.json()["items"][0]
    assert SECRET_BODY.strip() not in created.text + json.dumps(detail) + listing.text
    assert (events[0]["action"], events[0]["files"]) == ("stack_created", 2)
    assert events[0]["secret_files"] == SECRET_NAME
    assert SECRET_BODY.strip() not in str(events) and PLAIN_BODY.strip() not in str(events)


async def test_a_create_whose_files_break_the_rules_is_refused_and_stores_nothing(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    url = base(world)
    twice = [PLAIN, {**PLAIN, "name": PLAIN_NAME.upper()}]
    many = [{"name": f"f{number}.env", "content": "A=1\n"} for number in range(21)]

    refusals = {
        "a reserved name": stack(NAME, WEB_SERVICE, files=[{**PLAIN, "name": RESERVED_NAME}]),
        "a name differing only by case": stack(NAME, WEB_SERVICE, files=twice),
        "empty content": stack(NAME, WEB_SERVICE, files=[{**PLAIN, "content": ""}]),
        "content over 64 KiB": stack(
            NAME, WEB_SERVICE, files=[{**PLAIN, "content": "x" * (MAX_FILE_BYTES + 1)}]
        ),
        "an unknown field": stack(NAME, WEB_SERVICE, files=[{**PLAIN, "mode": "0600"}]),
        "twenty-one files": stack(NAME, WEB_SERVICE, files=many),
    }
    replies = {label: await admin_api.post(url, json=body) for label, body in refusals.items()}
    listing = await admin_api.get(url)

    assert {label: r.status_code for label, r in replies.items()} == {
        "a reserved name": BAD_REQUEST,
        "a name differing only by case": BAD_REQUEST,
        "empty content": BAD_REQUEST,
        "content over 64 KiB": BAD_REQUEST,
        "an unknown field": UNPROCESSABLE,
        "twenty-one files": UNPROCESSABLE,
    }
    assert error_of(replies["content over 64 KiB"])["reason"] == "too_large"
    assert error_of(replies["a reserved name"])["fields"][0]["loc"] == ["body", "files"]
    assert listing.json()["items"] == []
    assert await stack_rows(world, "SELECT name FROM stack_file") == []


async def test_a_file_is_written_and_replaced_by_put_and_a_secret_given_no_content_is_kept(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    await admin_api.post(base(world), json=stack(NAME, WEB_SERVICE))
    before = await detail_of(admin_api, world)

    added = await admin_api.put(
        file_url(world, NAME, PLAIN_NAME),
        json={
            "content": PLAIN_BODY,
            "secret": False,
            "expected_inputs_digest": before["inputs_digest"],
        },
    )
    added_secret = await admin_api.put(
        file_url(world, NAME, SECRET_NAME), json={"content": SECRET_BODY, "secret": True}
    )
    after_add = await detail_of(admin_api, world)
    replaced = await admin_api.put(
        file_url(world, NAME, PLAIN_NAME), json={"content": EDITED_BODY, "secret": False}
    )
    after_replace = await detail_of(admin_api, world)
    kept = await admin_api.put(
        file_url(world, NAME, SECRET_NAME), json={"content": None, "secret": True}
    )
    after_keep = await detail_of(admin_api, world)
    rotated = await admin_api.put(
        file_url(world, NAME, SECRET_NAME), json={"content": NEW_SECRET_BODY, "secret": True}
    )
    after_rotate = await detail_of(admin_api, world)

    assert [added.status_code, added_secret.status_code] == [OK, OK]
    assert set(added.json()) == FILE_KEYS and added.json()["content"] == PLAIN_BODY
    assert added_secret.json()["content"] is None and added_secret.json()["size"] is None
    assert after_add["inputs_digest"] != before["inputs_digest"]
    assert after_add["changed_since_deploy"] is True
    assert after_add["deployed_inputs_digest"] == before["inputs_digest"]
    assert replaced.status_code == OK and replaced.json()["content"] == EDITED_BODY
    assert by_name(after_replace)[PLAIN_NAME]["content"] == EDITED_BODY
    assert after_replace["inputs_digest"] != after_add["inputs_digest"]
    assert kept.status_code == OK and after_keep["inputs_digest"] == after_replace["inputs_digest"]
    assert (
        rotated.status_code == OK and after_rotate["inputs_digest"] != after_keep["inputs_digest"]
    )
    assert SECRET_BODY.strip() not in json.dumps([added_secret.json(), kept.json(), after_keep])
    assert NEW_SECRET_BODY.strip() not in rotated.text + json.dumps(after_rotate)


async def test_a_put_the_rules_refuse_is_reported_at_its_own_location_and_changes_nothing(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    await admin_api.post(base(world), json=stack(NAME, WEB_SERVICE, files=[PLAIN, SECRET]))
    digest = (await detail_of(admin_api, world))["inputs_digest"]
    url = file_url(world, NAME, PLAIN_NAME)

    reserved = await admin_api.put(
        file_url(world, NAME, RESERVED_NAME), json={"content": "x\n", "secret": False}
    )
    no_content = await admin_api.put(url, json={"content": None, "secret": False})
    secret_to_plain = await admin_api.put(
        file_url(world, NAME, SECRET_NAME), json={"content": None, "secret": False}
    )
    stale = await admin_api.put(
        url,
        json={"content": EDITED_BODY, "secret": False, "expected_inputs_digest": ZERO_DIGEST},
    )
    too_big = await admin_api.put(
        url, json={"content": "x" * (MAX_FILE_BYTES + 1), "secret": False}
    )
    unknown_field = await admin_api.put(url, json={"content": "x\n", "secret": False, "mode": 1})
    bad_digest = await admin_api.put(
        url, json={"content": "x\n", "secret": False, "expected_inputs_digest": "nothex"}
    )
    after = await detail_of(admin_api, world)

    assert reserved.status_code == BAD_REQUEST
    assert error_of(reserved)["fields"][0]["loc"] == ["path", "file"]
    assert no_content.status_code == secret_to_plain.status_code == BAD_REQUEST
    assert error_of(no_content)["fields"][0]["loc"] == ["body", "content"]
    assert error_of(secret_to_plain)["fields"][0]["loc"] == ["body", "content"]
    assert stale.status_code == CONFLICT and error_of(stale)["reason"] == "changed_since_review"
    assert too_big.status_code == BAD_REQUEST and error_of(too_big)["reason"] == "too_large"
    assert unknown_field.status_code == bad_digest.status_code == UNPROCESSABLE
    assert after["inputs_digest"] == digest
    assert by_name(after)[PLAIN_NAME]["content"] == PLAIN_BODY
    assert by_name(after)[SECRET_NAME]["secret"] is True


async def test_a_body_over_the_cap_is_refused_on_a_file_put(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    await admin_api.post(base(world), json=stack(NAME, WEB_SERVICE))
    oversize = json.dumps({"content": "x" * (256 * 1024), "secret": False})

    refused = await admin_api.put(
        file_url(world, NAME, PLAIN_NAME),
        content=oversize,
        headers={"Content-Type": "application/json"},
    )

    assert refused.status_code == PAYLOAD_TOO_LARGE
    assert (await detail_of(admin_api, world))["files"] == []


async def test_a_file_is_deleted_unless_a_document_names_it_and_a_missing_one_is_not_found(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    await admin_api.post(base(world), json=stack(NAME, WEB_SERVICE, files=[PLAIN, SECRET]))
    await seed_stack(
        world.app, world.remote_id, "ref", REFERENCING, files=[(PLAIN_NAME, PLAIN_BODY, False)]
    )
    reference = await detail_of(admin_api, world, "ref")
    referenced_url = file_url(world, "ref", PLAIN_NAME)
    digest = (await detail_of(admin_api, world))["inputs_digest"]

    in_use = await admin_api.delete(referenced_url)
    stale = await admin_api.delete(
        file_url(world, NAME, PLAIN_NAME), params={"expected_inputs_digest": ZERO_DIGEST}
    )
    deleted = await admin_api.delete(
        file_url(world, NAME, PLAIN_NAME), params={"expected_inputs_digest": digest}
    )
    again = await admin_api.delete(file_url(world, NAME, PLAIN_NAME))
    secret = await admin_api.delete(file_url(world, NAME, SECRET_NAME))
    bad_digest = await admin_api.delete(
        file_url(world, NAME, SECRET_NAME), params={"expected_inputs_digest": "nothex"}
    )
    after = await detail_of(admin_api, world)

    assert by_name(reference)[PLAIN_NAME]["used_as"] == [
        {"path": "services.web.env_file", "role": "env", "document": None}
    ]
    assert in_use.status_code == CONFLICT and error_of(in_use)["reason"] == "file_in_use"
    assert error_of(in_use)["fields"][0]["loc"] == ["path", "file"]
    assert stale.status_code == CONFLICT and error_of(stale)["reason"] == "changed_since_review"
    assert (deleted.status_code, deleted.content) == (NO_CONTENT, b"")
    assert again.status_code == NOT_FOUND
    assert secret.status_code == NO_CONTENT
    assert bad_digest.status_code == UNPROCESSABLE
    assert after["files"] == []
    assert PLAIN_NAME in by_name(await detail_of(admin_api, world, "ref"))


async def test_every_put_and_delete_writes_one_stack_updated_row_with_no_content(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    await admin_api.post(base(world), json=stack(NAME, WEB_SERVICE))

    await admin_api.put(
        file_url(world, NAME, PLAIN_NAME), json={"content": PLAIN_BODY, "secret": False}
    )
    await admin_api.put(
        file_url(world, NAME, SECRET_NAME), json={"content": SECRET_BODY, "secret": True}
    )
    await admin_api.delete(file_url(world, NAME, PLAIN_NAME))
    await admin_api.delete(file_url(world, NAME, PLAIN_NAME))
    updates = await stack_rows(world, UPDATED_ROWS, (NAME,))
    everything = await stack_rows(world, "SELECT detail FROM activity_event")

    assert [(u["result"], u["written"], u["removed"], u["kind"]) for u in updates[:3]] == [
        ("success", PLAIN_NAME, None, "plain"),
        ("success", SECRET_NAME, None, "secret"),
        ("success", None, PLAIN_NAME, "plain"),
    ]
    assert len(updates) == 3
    assert SECRET_BODY.strip() not in str(everything) and PLAIN_BODY.strip() not in str(everything)


async def test_a_malformed_file_name_is_not_found_and_only_a_bearer_token_may_write(
    admin_api: AsyncClient,
    admin_client: AsyncClient,
    browsers: Browsers,
    world: World,
    stack_host: ContainerEngine,
) -> None:
    await admin_api.post(base(world), json=stack(NAME, WEB_SERVICE))
    requests = len(stack_host.requests)
    anonymous = browsers.open()
    body = {"content": "x\n", "secret": False}
    url = file_url(world, NAME, PLAIN_NAME)

    malformed = [
        (await admin_api.put(file_url(world, NAME, name), json=body)).status_code
        for name in (".env", "-x", "a%20b", "x" * 129, "a%2Fb")
    ]
    malformed += [
        (await admin_api.delete(file_url(world, NAME, name))).status_code
        for name in (".env", "-x", "a%20b", "x" * 129, "a%2Fb")
    ]
    after_malformed = len(stack_host.requests)
    anonymous_put = await anonymous.put(url, json=body)
    anonymous_delete = await anonymous.delete(url)
    session_put = await admin_client.put(url, json=body, headers=csrf_headers(admin_client))
    session_delete = await admin_client.delete(url, headers=csrf_headers(admin_client))

    assert malformed == [NOT_FOUND] * 10
    assert after_malformed == requests
    assert [anonymous_put.status_code, anonymous_delete.status_code] == [FORBIDDEN, FORBIDDEN]
    assert [session_put.status_code, session_delete.status_code] == [UNAUTHENTICATED] * 2
    assert await stack_rows(world, "SELECT name FROM stack_file") == []


async def test_a_reader_gets_the_files_but_cannot_write_and_a_stranger_finds_nothing(
    admin_api: AsyncClient,
    user_api: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
) -> None:
    env = world.remote_id
    await seed_stack(
        world.app,
        env,
        "shared",
        REFERENCING,
        files=[(PLAIN_NAME, PLAIN_BODY, False), (SECRET_NAME, SECRET_BODY, True)],
    )
    await seed_stack(
        world.app, env, "private", REFERENCING, files=[(PLAIN_NAME, PLAIN_BODY, False)]
    )
    await plant_control(world.app, env, "stack", "shared", public=True, level="read")
    body = {"content": EDITED_BODY, "secret": False}

    read = await detail_of(user_api, world, "shared")
    put = await user_api.put(file_url(world, "shared", PLAIN_NAME), json=body)
    delete = await user_api.delete(file_url(world, "shared", SECRET_NAME))
    stranger = [
        await user_api.get(f"{base(world)}/private"),
        await user_api.put(file_url(world, "private", PLAIN_NAME), json=body),
        await user_api.delete(file_url(world, "private", PLAIN_NAME)),
    ]
    rows = await stack_rows(world, "SELECT name, content FROM stack_file ORDER BY id")

    assert read["can_write"] is False
    assert by_name(read)[PLAIN_NAME]["content"] == PLAIN_BODY
    assert by_name(read)[SECRET_NAME]["content"] is None
    assert SECRET_BODY.strip() not in json.dumps(read)
    assert (put.status_code, delete.status_code) == (FORBIDDEN, FORBIDDEN)
    assert [r.status_code for r in stranger] == [NOT_FOUND] * 3
    assert PLAIN_BODY in [row["content"] for row in rows]
    assert EDITED_BODY not in str(rows)
