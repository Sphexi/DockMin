"""Companion file writes on a Compose stack and the queries behind the Files tab and its pages."""

from pathlib import Path

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.stack import StackRepository
from app.services.stack_deploy import ChangedSinceReview, StackRejected
from app.services.stack_document import FileUse
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD
from tests.auth_support import STANDARD_ID
from tests.automation_support import event_rows
from tests.stack_support import Rig, activity_dump, project_box, seed_stack_row
from tests.web.stack_file_support import PLAIN_BODY, PLAIN_NAME, SECRET_BODY, SECRET_NAME

SHOP = "shop"
STALE = "0" * 64
EDITED_BODY = "edited-visible-content\n"
SIMPLE = "services:\n  web:\n    image: nginx:1\n"
USING = f"services:\n  web:\n    image: nginx:1\n    env_file: [{PLAIN_NAME}]\n"
INCLUDING = "include:\n  - inc.yaml\n" + SIMPLE
INCLUDED = "inc.yaml"
INCLUDED_BODY = f"services:\n  db:\n    image: nginx:1\n    env_file: {PLAIN_NAME}\n"
REFUSING_CONFIG = f"{SIMPLE}x-fake:\n  config: fail\n"
HALF = 64 * 1024
EXTRA = 9


def tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


async def put(rig: Rig, name: str, content: str, *, secret: bool = False) -> None:
    repository = StackRepository(rig.db, rig.registry.vault, rig.clock)
    stored = await repository.get(ENV_A, SHOP)
    assert stored is not None
    await repository.files.put_file(stored.id, name, content=content, secret=secret)


async def inputs_digest(rig: Rig) -> str:
    row = await rig.fresh().queries.require_stack(ADMIN, ENV_A, SHOP, "write")
    return row.inputs_digest or ""


async def share_read(rig: Rig) -> None:
    async with rig.database.session() as db:
        await ResourceControlRepository(db, rig.clock).create(
            ENV_A,
            "stack",
            SHOP,
            is_public=True,
            is_administrators_only=False,
            access_level="read",
            owner_user_id=None,
        )


async def test_writing_a_file_returns_its_view_and_moves_only_the_inputs_digest(
    stack_rig: Rig,
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, SIMPLE)
    before = await stack_rig.fresh().queries.require_stack(ADMIN, ENV_A, SHOP, "write")

    plain = await stack_rig.fresh().actions.write_file(
        ADMIN, ENV_A, SHOP, PLAIN_NAME, content=PLAIN_BODY, secret=False
    )
    secret = await stack_rig.fresh().actions.write_file(
        ADMIN,
        ENV_A,
        SHOP,
        SECRET_NAME,
        content=SECRET_BODY,
        secret=True,
        expected_inputs_digest=await inputs_digest(stack_rig),
    )
    after = await stack_rig.fresh().queries.require_stack(ADMIN, ENV_A, SHOP, "write")

    assert (plain.name, plain.secret, plain.size, plain.content) == (
        PLAIN_NAME,
        False,
        len(PLAIN_BODY),
        PLAIN_BODY,
    )
    assert plain.digest is not None and plain.uses == ()
    assert (secret.size, secret.digest, secret.content) == (None, None, None)
    assert after.inputs_digest != before.inputs_digest
    assert after.compose_file_digest == before.compose_file_digest
    assert after.deployed_inputs_digest == before.deployed_inputs_digest
    rows = await event_rows(stack_rig.db, "stack_updated")
    assert [(r["result"], r["detail"]["file_written"], r["detail"]["file_kind"]) for r in rows] == [
        ("success", PLAIN_NAME, "plain"),
        ("success", SECRET_NAME, "secret"),
    ]
    assert all("file_secret" not in r["detail"] for r in rows)
    dump = await activity_dump(stack_rig)
    assert SECRET_BODY.strip() not in dump and PLAIN_BODY.strip() not in dump


async def test_a_write_bound_to_a_stale_digest_is_refused_and_one_bound_to_none_is_not(
    stack_rig: Rig,
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, SIMPLE)

    with pytest.raises(ChangedSinceReview):
        await stack_rig.fresh().actions.write_file(
            ADMIN,
            ENV_A,
            SHOP,
            PLAIN_NAME,
            content=PLAIN_BODY,
            secret=False,
            expected_inputs_digest=STALE,
        )
    unbound = await stack_rig.fresh().actions.write_file(
        ADMIN, ENV_A, SHOP, PLAIN_NAME, content=PLAIN_BODY, secret=False
    )
    with pytest.raises(ChangedSinceReview):
        await stack_rig.fresh().actions.remove_file(
            ADMIN, ENV_A, SHOP, PLAIN_NAME, expected_inputs_digest=STALE
        )

    assert unbound.name == PLAIN_NAME
    rows = await event_rows(stack_rig.db, "stack_updated")
    assert [(r["result"], r["detail"].get("reason")) for r in rows] == [
        ("failure", "changed_since_review"),
        ("success", None),
        ("failure", "changed_since_review"),
    ]
    files = await stack_rig.fresh().queries.files_panel(ADMIN, ENV_A, SHOP)
    assert [f.name for f in files.files] == [PLAIN_NAME]


async def test_a_write_the_stack_cannot_take_leaves_nothing_stored_and_no_working_files(
    stack_rig: Rig,
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, REFUSING_CONFIG)
    before = tree(stack_rig.data_dir)

    with pytest.raises(StackRejected) as refused:
        await stack_rig.fresh().actions.write_file(
            ADMIN, ENV_A, SHOP, PLAIN_NAME, content=PLAIN_BODY, secret=False
        )
    with pytest.raises(StackRejected) as bad_name:
        await stack_rig.fresh().actions.write_file(
            ADMIN, ENV_A, SHOP, "compose.yml", content=PLAIN_BODY, secret=False
        )
    with pytest.raises(StackRejected) as bad_content:
        await stack_rig.fresh().actions.write_file(
            ADMIN, ENV_A, SHOP, PLAIN_NAME, content="a\x00b", secret=False
        )

    assert refused.value.reason == "invalid_compose"
    assert list(bad_name.value.fields) == ["name"] and list(bad_content.value.fields) == ["content"]
    assert (await stack_rig.fresh().queries.files_panel(ADMIN, ENV_A, SHOP)).files == ()
    assert tree(stack_rig.data_dir) == before


async def test_a_stack_holds_at_most_twenty_files_and_128_kib_and_a_write_over_either_is_refused(
    stack_rig: Rig,
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, SIMPLE)
    for number in range(20):
        await put(stack_rig, f"f{number}.env", "A=1\n")

    with pytest.raises(StackRejected) as count:
        await stack_rig.fresh().actions.write_file(
            ADMIN, ENV_A, SHOP, "one-more.env", content="A=1\n", secret=False
        )
    replaced = await stack_rig.fresh().actions.write_file(
        ADMIN, ENV_A, SHOP, "f0.env", content="B=2\n", secret=False
    )

    assert count.value.reason == "too_large"
    assert replaced.content == "B=2\n"


async def test_a_stack_whose_files_fill_128_kib_refuses_one_more_byte(stack_rig: Rig) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, SIMPLE)
    await put(stack_rig, "a.env", "x" * HALF)
    await put(stack_rig, "b.env", "x" * HALF)

    with pytest.raises(StackRejected) as total:
        await stack_rig.fresh().actions.write_file(
            ADMIN, ENV_A, SHOP, "c.env", content="x", secret=False
        )

    assert total.value.reason == "too_large" and list(total.value.fields) == ["files"]
    names = [
        f.name for f in (await stack_rig.fresh().queries.files_panel(ADMIN, ENV_A, SHOP)).files
    ]
    assert names == ["a.env", "b.env"]


async def test_a_file_a_document_names_cannot_be_removed_and_the_refusal_is_recorded(
    stack_rig: Rig,
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, USING)
    await put(stack_rig, PLAIN_NAME, PLAIN_BODY)

    with pytest.raises(StackRejected) as in_use:
        await stack_rig.fresh().actions.remove_file(ADMIN, ENV_A, SHOP, PLAIN_NAME)

    assert in_use.value.reason == "file_in_use"
    assert in_use.value.fields == {
        "file": f"{PLAIN_NAME} is still used at services.web.env_file[0]."
    }
    [row] = await event_rows(stack_rig.db, "stack_updated")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "file_in_use")
    files = await stack_rig.fresh().queries.files_panel(ADMIN, ENV_A, SHOP)
    assert [f.name for f in files.files] == [PLAIN_NAME]


async def test_an_unreferenced_file_is_removed_and_removing_it_again_is_not_found(
    stack_rig: Rig,
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, SIMPLE)
    await put(stack_rig, "spare.env", "A=1\n")
    before = await inputs_digest(stack_rig)

    await stack_rig.fresh().actions.remove_file(ADMIN, ENV_A, SHOP, "spare.env")
    with pytest.raises(ResourceNotFound):
        await stack_rig.fresh().actions.remove_file(ADMIN, ENV_A, SHOP, "spare.env")

    assert await inputs_digest(stack_rig) != before
    rows = await event_rows(stack_rig.db, "stack_updated")
    assert [(r["result"], r["detail"]["file_removed"]) for r in rows] == [("success", "spare.env")]
    files = await stack_rig.fresh().queries.files_panel(ADMIN, ENV_A, SHOP)
    assert files.files == ()


async def test_the_files_panel_shows_each_file_with_where_it_is_named_and_withholds_a_secret(
    stack_rig: Rig,
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, INCLUDING)
    await put(stack_rig, INCLUDED, INCLUDED_BODY)
    await put(stack_rig, PLAIN_NAME, PLAIN_BODY)
    await put(stack_rig, SECRET_NAME, SECRET_BODY, secret=True)
    await put(stack_rig, "spare.env", "A=1\n")
    await share_read(stack_rig)

    admin = await stack_rig.fresh().queries.files_panel(ADMIN, ENV_A, SHOP)
    reader = await stack_rig.fresh().queries.files_panel(STANDARD, ENV_A, SHOP)
    detail = await stack_rig.fresh().queries.detail(STANDARD, ENV_A, SHOP)

    by_name = {view.name: view for view in reader.files}
    assert [view.name for view in reader.files] == [PLAIN_NAME, SECRET_NAME, INCLUDED, "spare.env"]
    assert by_name[PLAIN_NAME].uses == (FileUse("services.db.env_file", "env", INCLUDED),)
    assert by_name[INCLUDED].uses == (FileUse("include[0]", "compose", None),)
    assert by_name["spare.env"].uses == ()
    assert by_name[PLAIN_NAME].content == PLAIN_BODY and by_name[PLAIN_NAME].digest is not None
    assert (by_name[SECRET_NAME].size, by_name[SECRET_NAME].digest) == (None, None)
    assert by_name[SECRET_NAME].content is None and by_name[SECRET_NAME].secret is True
    assert (admin.can_write, reader.can_write, detail.can_write) == (True, False, False)
    assert reader.inputs_digest == admin.inputs_digest != ""
    assert detail.files == reader.files
    assert SECRET_BODY.strip() not in repr(reader) + repr(detail)


async def test_the_add_edit_and_remove_pages_start_from_the_stored_file_for_a_writer_only(
    stack_rig: Rig,
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, USING)
    await put(stack_rig, PLAIN_NAME, PLAIN_BODY)
    await put(stack_rig, SECRET_NAME, SECRET_BODY, secret=True)
    await share_read(stack_rig)
    queries = stack_rig.fresh().queries

    add = await queries.file_form(ADMIN, ENV_A, SHOP, None)
    edit = await queries.file_form(ADMIN, ENV_A, SHOP, PLAIN_NAME)
    secret_edit = await queries.file_form(ADMIN, ENV_A, SHOP, SECRET_NAME)
    in_use = await queries.file_remove_confirmation(ADMIN, ENV_A, SHOP, PLAIN_NAME)
    free = await queries.file_remove_confirmation(ADMIN, ENV_A, SHOP, SECRET_NAME)

    assert add.file is None and add.inputs_digest == await inputs_digest(stack_rig)
    assert edit.file is not None and edit.file.content == PLAIN_BODY
    assert secret_edit.file is not None and secret_edit.file.content is None
    assert (in_use.in_use, free.in_use) == (True, False)
    assert in_use.file.uses == (FileUse("services.web.env_file[0]", "env", None),)
    assert in_use.expected_inputs_digest == add.inputs_digest == free.expected_inputs_digest
    assert (in_use.stack, in_use.environment_id) == (SHOP, ENV_A)
    for name in (None, PLAIN_NAME):
        with pytest.raises(PermissionDenied):
            await queries.file_form(STANDARD, ENV_A, SHOP, name)
    with pytest.raises(PermissionDenied):
        await queries.file_remove_confirmation(STANDARD, ENV_A, SHOP, PLAIN_NAME)


@pytest.mark.parametrize("name", ["missing.env", ".env", "-x", "a b", "a/b", "x" * 129])
async def test_a_name_that_is_not_a_stored_file_is_not_found_on_every_file_query(
    stack_rig: Rig, name: str
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, SIMPLE)
    await put(stack_rig, PLAIN_NAME, PLAIN_BODY)
    queries = stack_rig.fresh().queries

    with pytest.raises(ResourceNotFound):
        await queries.file_form(ADMIN, ENV_A, SHOP, name)
    with pytest.raises(ResourceNotFound):
        await queries.file_remove_confirmation(ADMIN, ENV_A, SHOP, name)
    with pytest.raises(ResourceNotFound):
        await stack_rig.fresh().actions.remove_file(ADMIN, ENV_A, SHOP, name)


async def test_a_stack_nobody_shared_has_no_files_panel_for_a_standard_user(
    stack_rig: Rig,
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, SIMPLE)
    await put(stack_rig, PLAIN_NAME, PLAIN_BODY)

    with pytest.raises(ResourceNotFound):
        await stack_rig.fresh().queries.files_panel(STANDARD, ENV_A, SHOP)
    with pytest.raises(ResourceNotFound):
        await stack_rig.fresh().actions.write_file(
            STANDARD, ENV_A, SHOP, "x.env", content="A=1\n", secret=False
        )


async def test_a_standard_user_writes_files_only_when_every_container_is_theirs_and_off_the_socket(
    stack_rig: Rig,
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, SIMPLE, owner_user_id=STANDARD_ID)
    await seed_stack_row(stack_rig, ENV_B, SHOP, SIMPLE, owner_user_id=STANDARD_ID)
    own = await stack_rig.fresh().actions.write_file(
        STANDARD, ENV_A, SHOP, PLAIN_NAME, content=PLAIN_BODY, secret=False
    )
    project_box(stack_rig.host, EXTRA, SHOP, "extra")
    extra = next(iter(stack_rig.host.boxes.values())).id
    async with stack_rig.database.session() as db:
        await ResourceControlRepository(db, stack_rig.clock).create(
            ENV_A,
            "container",
            extra,
            is_public=True,
            is_administrators_only=False,
            access_level="read",
            owner_user_id=None,
        )

    with pytest.raises(StackRejected) as restricted:
        await stack_rig.fresh().actions.write_file(
            STANDARD, ENV_A, SHOP, "more.env", content="A=1\n", secret=False
        )
    with pytest.raises(StackRejected):
        await stack_rig.fresh().actions.remove_file(STANDARD, ENV_A, SHOP, PLAIN_NAME)
    with pytest.raises(PermissionDenied):
        await stack_rig.fresh().actions.write_file(
            STANDARD, ENV_B, SHOP, PLAIN_NAME, content=PLAIN_BODY, secret=False
        )

    assert own.name == PLAIN_NAME and restricted.value.reason == "restricted"
    assert [
        f.name for f in (await stack_rig.fresh().queries.files_panel(ADMIN, ENV_A, SHOP)).files
    ] == [PLAIN_NAME]
    rows = await event_rows(stack_rig.db, "stack_updated")
    assert [(r["result"], r["detail"].get("reason")) for r in rows] == [
        ("success", None),
        ("failure", "restricted"),
        ("failure", "restricted"),
    ]
