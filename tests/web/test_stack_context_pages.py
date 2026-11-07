"""The Build contexts section of a Compose stack's Files tab: upload, replace and remove."""

import hashlib
import tarfile

import pytest
from httpx import AsyncClient, Response

from app.db.repositories import stack_build_context as limits
from app.web.limits import MAX_BODY_BYTES
from tests.archive_support import Member, build_tar
from tests.auth_support import csrf_form, csrf_token_of
from tests.container_support import plant_control
from tests.docker_engine.container_engine import ContainerEngine
from tests.stack_support import stopped_stack_jobs  # noqa: F401
from tests.web.environment_support import World
from tests.web.flash_support import flash_texts, land
from tests.web.helpers import hidden_inputs
from tests.web.stack_file_support import flat, seed_stack, stack_rows

pytestmark = pytest.mark.usefixtures("stopped_stack_jobs")

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN, NOT_FOUND = 200, 303, 400, 403, 404
CONFLICT, TOO_LARGE, UNPROCESSABLE = 409, 413, 422
SHOP = "shop"
CONTEXT = "app"
SIMPLE = "services:\n  web:\n    image: nginx:1\n"
BUILDING = "services:\n  web:\n    build: ./app\n"
BOUNDARY = "xx-context-boundary-xx"
SAVED = "Build context saved. Redeploy the stack to apply it."
REMOVED = "Build context removed."
SECTION = "Build contexts"
EMPTY = "The stack has no build contexts."
DOCKERFILE = b"FROM scratch\n"
BULK = b"x" * (MAX_BODY_BYTES + 1024)
LARGE = build_tar(Member("Dockerfile", DOCKERFILE), Member("bulk.bin", BULK))
SMALL = build_tar(Member("Dockerfile", DOCKERFILE), Member("run.sh", b"#!/bin/sh\n", mode=0o755))
LINKED = build_tar(
    Member("Dockerfile", DOCKERFILE), Member("l", type=tarfile.SYMTYPE, linkname="/")
)
CONTEXT_ROWS = "SELECT name, content_digest, member_count, extracted_size FROM stack_build_context"
UPDATED = (
    "SELECT result, json_extract(detail, '$.context_written') AS written, "
    "json_extract(detail, '$.context_removed') AS removed, "
    "json_extract(detail, '$.context_digest') AS digest, "
    "json_extract(detail, '$.context_size') AS size "
    "FROM activity_event WHERE action = 'stack_updated' AND target_id = ? ORDER BY id"
)


def base_of(world: World) -> str:
    return f"/environments/{world.remote_id}/stacks/{SHOP}"


def multipart(parts: list[tuple[str, str | None, bytes]]) -> tuple[bytes, dict[str, str]]:
    body = b""
    for name, filename, data in parts:
        disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        body += f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n\r\n".encode()
        body += data + b"\r\n"
    body += f"--{BOUNDARY}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}


async def upload(
    client: AsyncClient, world: World, name: str, archive: bytes, *, token: str | None = None
) -> Response:
    parts = [
        ("csrf_token", None, (token or csrf_token_of(client)).encode()),
        ("name", None, name.encode()),
        ("archive", "context.tar", archive),
    ]
    body, headers = multipart(parts)
    return await client.post(f"{base_of(world)}/contexts/-/upload", content=body, headers=headers)


async def files_tab(client: AsyncClient, world: World) -> Response:
    return await client.get(base_of(world), params={"tab": "files"})


async def test_an_administrator_adds_replaces_and_removes_a_context_from_the_files_tab(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    await seed_stack(world.app, world.remote_id, SHOP, SIMPLE)
    empty = await files_tab(admin_client, world)
    form = empty.text
    large_digest = hashlib.sha256(LARGE).hexdigest()

    added = await upload(admin_client, world, CONTEXT, LARGE)
    after_add = await land(admin_client, added)
    stored = await stack_rows(world, CONTEXT_ROWS)
    replaced = await upload(admin_client, world, CONTEXT.upper(), SMALL)
    after_replace = await land(admin_client, replaced)
    confirm = await admin_client.get(f"{base_of(world)}/contexts/{CONTEXT}/remove")
    removed = await admin_client.post(
        f"{base_of(world)}/contexts/{CONTEXT}/remove",
        data=csrf_form(
            admin_client,
            expected_inputs_digest=hidden_inputs(confirm.text)["expected_inputs_digest"],
        ),
    )
    after_remove = await land(admin_client, removed)
    events = await stack_rows(world, UPDATED, (SHOP,))

    assert SECTION in flat(empty) and EMPTY in flat(empty)
    assert (
        form.index('name="csrf_token"') < form.index('name="name"') < form.index('name="archive"')
    )
    assert 'enctype="multipart/form-data"' in form
    assert len(LARGE) > MAX_BODY_BYTES and added.status_code == SEE_OTHER
    assert SAVED in flash_texts(after_add.text)
    assert [(r["name"], r["content_digest"]) for r in stored] == [(CONTEXT, large_digest)]
    assert large_digest[:12] in after_add.text and CONTEXT in flat(after_add)
    assert SAVED in flash_texts(after_replace.text)
    assert hashlib.sha256(SMALL).hexdigest()[:12] in after_replace.text
    assert large_digest[:12] not in after_replace.text
    assert confirm.status_code == OK and "Remove build context" in flat(confirm)
    assert REMOVED in flash_texts(after_remove.text) and EMPTY in flat(after_remove)
    assert await stack_rows(world, CONTEXT_ROWS) == []
    assert [(e["result"], e["written"], e["removed"]) for e in events] == [
        ("success", CONTEXT, None),
        ("success", CONTEXT, None),
        ("success", None, CONTEXT),
    ]
    assert events[0]["digest"] == large_digest and events[0]["size"] == len(LARGE)


async def test_an_upload_the_rules_refuse_is_answered_on_a_problem_page_and_stores_nothing(
    admin_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_stack(world.app, world.remote_id, SHOP, SIMPLE)
    url = f"{base_of(world)}/contexts/-/upload"
    token = csrf_token_of(admin_client)
    reordered, headers = multipart(
        [("name", None, b"x"), ("csrf_token", None, token.encode()), ("archive", "c.tar", SMALL)]
    )
    no_file, no_file_headers = multipart(
        [("csrf_token", None, token.encode()), ("name", None, b"x")]
    )
    extra, extra_headers = multipart(
        [
            ("csrf_token", None, token.encode()),
            ("note", None, b"x"),
            ("name", None, b"x"),
            ("archive", "c.tar", SMALL),
        ]
    )

    symlink = await upload(admin_client, world, CONTEXT, LINKED)
    bad_name = await upload(admin_client, world, "bad name", SMALL)
    garbage = await upload(admin_client, world, CONTEXT, b"not an archive" * 50)
    wrong_token = await upload(admin_client, world, CONTEXT, SMALL, token="W" * 43)
    out_of_order = await admin_client.post(url, content=reordered, headers=headers)
    without_file = await admin_client.post(url, content=no_file, headers=no_file_headers)
    unexpected = await admin_client.post(url, content=extra, headers=extra_headers)
    monkeypatch.setattr(limits, "MAX_CONTEXT_UPLOAD_BYTES", len(SMALL) - 1)
    oversized = await upload(admin_client, world, CONTEXT, SMALL)

    assert [r.status_code for r in (symlink, bad_name, garbage)] == [UNPROCESSABLE] * 3
    assert "not a regular file" in flat(symlink) and "Use 1 to 128" in flat(bad_name)
    assert wrong_token.status_code == FORBIDDEN
    assert [r.status_code for r in (out_of_order, without_file, unexpected)] == [BAD_REQUEST] * 3
    assert oversized.status_code == TOO_LARGE and "larger than 16 MiB" in flat(oversized)
    assert await stack_rows(world, CONTEXT_ROWS) == []


async def test_a_context_the_compose_file_builds_from_cannot_be_removed(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    await seed_stack(world.app, world.remote_id, SHOP, BUILDING)
    await upload(admin_client, world, CONTEXT, SMALL)
    path = f"{base_of(world)}/contexts/{CONTEXT}/remove"

    digest = (await stack_rows(world, "SELECT inputs_digest FROM stack"))[0]["inputs_digest"]

    tab = await files_tab(admin_client, world)
    confirm = await admin_client.get(path)
    refused = await admin_client.post(
        path, data=csrf_form(admin_client, expected_inputs_digest=digest)
    )

    assert "services.web.build" in flat(tab)
    assert "still builds from this context" in flat(confirm)
    assert "Remove build context</button>" not in confirm.text
    assert refused.status_code == CONFLICT and "services.web.build" in flat(refused)
    assert [r["name"] for r in await stack_rows(world, CONTEXT_ROWS)] == [CONTEXT]


async def test_a_standard_user_has_no_section_and_is_refused_every_context_route(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
) -> None:
    await seed_stack(world.app, world.remote_id, SHOP, SIMPLE)
    await plant_control(world.app, world.remote_id, "stack", SHOP, public=True, level="write")
    await upload(admin_client, world, CONTEXT, SMALL)
    base = base_of(world)

    reader = await files_tab(standard_client, world)
    writer = await files_tab(admin_client, world)
    confirm = await standard_client.get(f"{base}/contexts/{CONTEXT}/remove")
    removal = await standard_client.post(
        f"{base}/contexts/{CONTEXT}/remove", data=csrf_form(standard_client)
    )
    added = await upload(standard_client, world, "other", SMALL)
    malformed = await standard_client.get(f"{base}/contexts/x%20y/remove")
    malformed_admin = await admin_client.get(f"{base}/contexts/x%20y/remove")

    assert reader.status_code == OK and SECTION not in flat(reader) and CONTEXT not in flat(reader)
    assert SECTION in flat(writer) and CONTEXT in flat(writer)
    assert [r.status_code for r in (confirm, removal, added)] == [FORBIDDEN] * 3
    assert (malformed.status_code, malformed_admin.status_code) == (NOT_FOUND, NOT_FOUND)
    assert [r["name"] for r in await stack_rows(world, CONTEXT_ROWS)] == [CONTEXT]


async def test_an_anonymous_visitor_is_turned_away_before_the_upload_is_read(
    anonymous_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    await seed_stack(world.app, world.remote_id, SHOP, SIMPLE)
    body, headers = multipart([("csrf_token", None, b"x")])

    response = await anonymous_client.post(
        f"{base_of(world)}/contexts/-/upload", content=body, headers=headers
    )

    assert response.status_code in (SEE_OTHER, 401)
    assert await stack_rows(world, CONTEXT_ROWS) == []
