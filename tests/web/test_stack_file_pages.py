"""The Compose stack file pages: rows on the create and adopt forms and the Files tab."""

from pathlib import Path

import pytest
from httpx import AsyncClient, Response

from tests.auth_support import csrf_form
from tests.container_support import plant_control
from tests.docker_engine.container_engine import ContainerEngine
from tests.stack_support import (  # noqa: F401
    PROJECT_LABEL,
    job_id_of,
    read_records,
    settle_stack_job,
    stopped_stack_jobs,
)
from tests.web.environment_support import World
from tests.web.flash_support import flash_texts, land
from tests.web.helpers import form_values
from tests.web.stack_file_support import (
    NOT_USED,
    PLAIN_BODY,
    PLAIN_FILE,
    PLAIN_NAME,
    SECRET_BODY,
    SECRET_FILE,
    SECRET_NAME,
    UPDATED_ROWS,
    file_fields,
    flat,
    seed_stack,
    stack_rows,
)

pytestmark = pytest.mark.usefixtures("stopped_stack_jobs")

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN, NOT_FOUND, CONFLICT = 200, 303, 400, 403, 404, 409
HTMX = {"HX-Request": "true"}
NAME = "blog"
RESERVED_NAME = "Compose.yaml"
UNKNOWN_NAME = "missing.env"
SHOP = "shop"
UPLOAD_NAME = "up.env"
UPLOAD_BODY = b"UPLOADED=1\n"
EDITED_BODY = "edited-visible-content\n"
NEW_SECRET_BODY = "REPLACEMENT-SECRET-SENTINEL-9902\n"
INCLUDED = "inc.yaml"
INCLUDING_STACK = "outer"
INCLUDED_BODY = "services:\n  db:\n    image: nginx:1\n"
INCLUDED_FILE = (INCLUDED, INCLUDED_BODY, False)
BUILD_BODY = "services:\n  db:\n    image: nginx:1\n    build: .\n"
SIMPLE = "services:\n  web:\n    image: nginx:1\n"
INCLUDING = f"include:\n  - {INCLUDED}\n{SIMPLE}"
ENV_USING = f"services:\n  web:\n    image: nginx:1\n    env_file: {PLAIN_NAME}\n"
REFUSING_CONFIG = f"{SIMPLE}x-fake:\n  config: fail\n"
ADDED = "File added. Redeploy the stack to apply it."
SAVED = "File saved. Redeploy the stack to apply it."
REMOVED = "File removed. Redeploy the stack to apply the change."
CHANGED_CHIP = "Changed since the last deploy"
KEEP_PLACEHOLDER = "Set. Leave empty to keep it."
STALE_DIGEST = "0" * 64
MALFORMED_NAMES = (".env", "-x", "a%20b", "x" * 129, "a%2Fb", "%2E%2E")


def stacks_path(world: World, environment_id: int | None = None) -> str:
    return f"/environments/{environment_id or world.remote_id}/stacks"


def recorded_stack(record: Path) -> str:
    return f"{SIMPLE}x-fake:\n  record: {record}\n"


def commands(record: Path) -> list[str]:
    return [call["cmd"] for call in read_records(record)]


async def submit(client: AsyncClient, path: str, **fields: str) -> Response:
    """Open a file page and post its form back with the fields a person filled in."""
    page = await client.get(path)
    assert page.status_code == OK, path
    return await client.post(path, data=form_values(page.text) | fields)


async def create_blog(client: AsyncClient, world: World, record: Path) -> None:
    created = await client.post(
        f"{stacks_path(world)}/-/new",
        data=csrf_form(client, name=NAME, compose_file=recorded_stack(record)),
    )
    await settle_stack_job(world.app, job_id_of(created))


async def test_a_new_stack_stores_its_file_rows_and_no_page_or_row_shows_secret_content(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    base = stacks_path(world)
    rows = file_fields(PLAIN_FILE, SECRET_FILE) | {"file-2-name": UPLOAD_NAME}
    form = csrf_form(admin_client, name=NAME, compose_file=SIMPLE, **rows)

    created = await admin_client.post(
        f"{base}/-/new", data=form, files={"file-2-upload": ("up.env", UPLOAD_BODY)}
    )
    await settle_stack_job(world.app, job_id_of(created))
    tab = await admin_client.get(f"{base}/{NAME}", params={"tab": "files"})
    fragment = await admin_client.get(f"{base}/{NAME}", params={"tab": "files"}, headers=HTMX)
    stored = await stack_rows(
        world, "SELECT name, is_secret, content, content_ciphertext FROM stack_file ORDER BY name"
    )
    events = await stack_rows(
        world,
        "SELECT action, json_extract(detail, '$.files') AS files, "
        "json_extract(detail, '$.secret_files') AS secret_files, detail FROM activity_event "
        "WHERE target_type = 'stack' ORDER BY id",
    )

    text = flat(tab)
    assert created.status_code == SEE_OTHER
    assert f"{PLAIN_NAME} {NOT_USED} Plain {len(PLAIN_BODY)} B" in text
    assert f"{SECRET_NAME} {NOT_USED} Secret Set Not shown" in text
    assert f"{UPLOAD_NAME} {NOT_USED} Plain {len(UPLOAD_BODY)} B" in text
    assert PLAIN_BODY.strip() in text and UPLOAD_BODY.decode().strip() in text
    assert SECRET_BODY.strip() not in text + flat(fragment)
    assert "<html" not in fragment.text and PLAIN_BODY.strip() in fragment.text
    assert [(r["name"], r["is_secret"], r["content"]) for r in stored] == [
        (PLAIN_NAME, 0, PLAIN_BODY),
        (SECRET_NAME, 1, None),
        (UPLOAD_NAME, 0, UPLOAD_BODY.decode()),
    ]
    assert SECRET_BODY.strip() not in str(stored[1]["content_ciphertext"])
    assert stored[1]["content_ciphertext"] and stored[0]["content_ciphertext"] is None
    assert (events[0]["action"], events[0]["files"]) == ("stack_created", 3)
    assert events[0]["secret_files"] == SECRET_NAME
    assert SECRET_BODY.strip() not in str(events) and PLAIN_BODY.strip() not in str(events)


async def test_a_file_row_the_form_refuses_is_reported_on_its_own_field_and_a_secret_is_not_echoed(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    base = stacks_path(world)
    started = len(world.app.state.engine.stack_jobs)
    nameless = csrf_form(
        admin_client,
        name=NAME,
        compose_file=SIMPLE,
        **(file_fields(SECRET_FILE) | {"file-1-name": "", "file-1-content": "orphan-body"}),
    )
    reserved = csrf_form(
        admin_client, name=NAME, compose_file=SIMPLE, **file_fields((RESERVED_NAME, "x\n", False))
    )
    both = csrf_form(
        admin_client,
        name=NAME,
        compose_file=SIMPLE,
        **file_fields((UPLOAD_NAME, "pasted\n", False)),
    )
    twins = csrf_form(
        admin_client,
        name=NAME,
        compose_file=SIMPLE,
        **file_fields((PLAIN_NAME, "a\n", False), (PLAIN_NAME.upper(), "b\n", False)),
    )

    refused_nameless = await admin_client.post(f"{base}/-/new", data=nameless)
    refused_reserved = await admin_client.post(f"{base}/-/new", data=reserved)
    refused_both = await admin_client.post(
        f"{base}/-/new", data=both, files={"file-0-upload": ("x", b"uploaded\n")}
    )
    refused_twins = await admin_client.post(f"{base}/-/new", data=twins)
    stored = await stack_rows(world, "SELECT name FROM stack_file")

    refusals = [refused_nameless, refused_reserved, refused_both, refused_twins]
    assert [r.status_code for r in refusals] == [BAD_REQUEST] * 4
    assert "Enter a name for the file." in flat(refused_nameless)
    assert form_values(refused_nameless.text)["file-1-name"] == ""
    assert SECRET_BODY.strip() not in refused_nameless.text
    assert "This name is used by Compose for its own files." in flat(refused_reserved)
    assert "Use either the text box or a file, not both." in flat(refused_both)
    assert stored == [] and len(world.app.state.engine.stack_jobs) == started


async def test_an_unmanaged_project_is_adopted_with_its_file_rows(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    base = stacks_path(world)
    stack_host.add(f"{0xD1:064x}", "cron-job-1", labels={PROJECT_LABEL: "cron"}, image="nginx:1")
    form = csrf_form(admin_client, compose_file=SIMPLE, **file_fields(PLAIN_FILE, SECRET_FILE))

    adopted = await admin_client.post(f"{base}/-/adopt/cron", data=form)
    detail = await land(admin_client, adopted)
    tab = await admin_client.get(f"{base}/cron", params={"tab": "files"})
    event = await stack_rows(
        world,
        "SELECT json_extract(detail, '$.files') AS files, "
        "json_extract(detail, '$.secret_files') AS secret_files FROM activity_event "
        "WHERE action = 'stack_adopted'",
    )

    assert adopted.status_code == SEE_OTHER and detail.status_code == OK
    assert f"{PLAIN_NAME} {NOT_USED} Plain" in flat(tab)
    assert f"{SECRET_NAME} {NOT_USED} Secret Set" in flat(tab)
    assert SECRET_BODY.strip() not in tab.text
    assert event == [{"files": 2, "secret_files": SECRET_NAME}]


async def test_a_writer_adds_edits_and_removes_files_and_each_change_writes_one_row(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    base = f"{stacks_path(world)}/{NAME}"
    record = tmp_path / "calls.jsonl"
    await create_blog(admin_client, world, record)
    deployed = len(commands(record))
    plain_path, secret_path = f"{base}/files/{PLAIN_NAME}", f"{base}/files/{SECRET_NAME}"
    file_query = "SELECT name, value_token, content_ciphertext FROM stack_file ORDER BY name"

    add_page = await admin_client.get(f"{base}/files/-/new")
    added_plain = await submit(
        admin_client, f"{base}/files/-/new", name=PLAIN_NAME, content=PLAIN_BODY
    )
    after_add = await land(admin_client, added_plain)
    added_secret = await submit(
        admin_client, f"{base}/files/-/new", name=SECRET_NAME, content=SECRET_BODY, secret="on"
    )
    tab = await land(admin_client, added_secret)
    stored = await stack_rows(world, file_query)
    plain_edit = await admin_client.get(f"{plain_path}/edit")
    secret_edit = await admin_client.get(f"{secret_path}/edit")
    saved_plain = await submit(admin_client, f"{plain_path}/edit", content=EDITED_BODY)
    after_save = await land(admin_client, saved_plain)
    kept = await submit(admin_client, f"{secret_path}/edit", content="", secret="on")
    after_keep = await stack_rows(world, file_query)
    replaced = await submit(
        admin_client, f"{secret_path}/edit", content=NEW_SECRET_BODY, secret="on"
    )
    after_replace = await stack_rows(world, file_query)
    confirm = await admin_client.get(f"{plain_path}/remove")
    removed = await admin_client.post(f"{plain_path}/remove", data=form_values(confirm.text))
    final = await land(admin_client, removed)
    updates = await stack_rows(world, UPDATED_ROWS, (NAME,))

    assert add_page.status_code == OK and "Add file" in flat(add_page)
    assert flash_texts(after_add.text) == [ADDED] and CHANGED_CHIP in flat(after_add)
    assert f"{SECRET_NAME} {NOT_USED} Secret Set Not shown" in flat(tab)
    assert PLAIN_BODY.strip() in flat(tab) and SECRET_BODY.strip() not in tab.text
    assert PLAIN_BODY.strip() in plain_edit.text and SECRET_BODY.strip() not in secret_edit.text
    assert KEEP_PLACEHOLDER in secret_edit.text
    assert flash_texts(after_save.text) == [SAVED]
    assert kept.status_code == SEE_OTHER and after_keep[1] == stored[1]
    assert replaced.status_code == SEE_OTHER
    assert after_replace[1]["value_token"] != stored[1]["value_token"]
    assert after_replace[1]["content_ciphertext"] != stored[1]["content_ciphertext"]
    assert NEW_SECRET_BODY.strip() not in str(after_replace)
    assert f"Remove file {PLAIN_NAME} from {NAME}?" in flat(confirm)
    assert "No document names this file." in flat(confirm)
    assert flash_texts(final.text) == [REMOVED]
    assert [(u["result"], u["written"], u["removed"], u["kind"]) for u in updates] == [
        ("success", PLAIN_NAME, None, "plain"),
        ("success", SECRET_NAME, None, "secret"),
        ("success", PLAIN_NAME, None, "plain"),
        ("success", SECRET_NAME, None, "secret"),
        ("success", SECRET_NAME, None, "secret"),
        ("success", None, PLAIN_NAME, "plain"),
    ]
    assert set(commands(record)[deployed:]) == {"config"}


async def inputs_digest(world: World, name: str) -> str:
    [row] = await stack_rows(world, "SELECT inputs_digest FROM stack WHERE name = ?", (name,))
    return str(row["inputs_digest"])


async def failure_reasons(world: World, name: str) -> list[str | None]:
    found = await stack_rows(world, UPDATED_ROWS, (name,))
    return [row["reason"] for row in found if row["result"] == "failure"]


async def test_a_file_change_the_stack_would_not_survive_is_refused_and_nothing_is_stored(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    base = stacks_path(world)
    app, environment = world.app, world.remote_id
    await seed_stack(app, environment, SHOP, REFUSING_CONFIG)
    await seed_stack(app, environment, INCLUDING_STACK, INCLUDING, files=[INCLUDED_FILE])
    including = f"{base}/{INCLUDING_STACK}"

    config_refused = await submit(
        admin_client, f"{base}/{SHOP}/files/-/new", name=PLAIN_NAME, content=PLAIN_BODY
    )
    broken = await submit(admin_client, f"{including}/files/{INCLUDED}/edit", content=BUILD_BODY)
    review = await admin_client.get(f"{including}/files/{INCLUDED}/remove")
    unreviewed = await admin_client.post(
        f"{including}/files/{INCLUDED}/remove",
        data=csrf_form(
            admin_client, expected_inputs_digest=await inputs_digest(world, INCLUDING_STACK)
        ),
    )
    stale = await admin_client.post(
        f"{including}/files/-/new",
        data=form_values((await admin_client.get(f"{including}/files/-/new")).text)
        | {"name": PLAIN_NAME, "content": PLAIN_BODY, "expected_inputs_digest": STALE_DIGEST},
    )
    stored = await stack_rows(world, "SELECT name, content FROM stack_file ORDER BY name")

    assert config_refused.status_code == BAD_REQUEST
    assert "Docker Compose rejected this file" in flat(config_refused)
    assert broken.status_code == BAD_REQUEST
    assert "Name a build context added to this stack" in flat(broken)
    assert 'data-goto-line="4"' in broken.text and 'href="#field-content"' in broken.text
    assert review.status_code == OK
    assert "A document still names this file" in flat(review)
    assert "include[0]" in flat(review) and "Compose file" in flat(review)
    assert "Remove file</button>" not in review.text
    assert unreviewed.status_code == CONFLICT
    assert f"{INCLUDED} is still used at" in flat(unreviewed)
    assert stale.status_code == CONFLICT and "The stack changed" in flat(stale)
    assert stored == [{"name": INCLUDED, "content": INCLUDED_BODY}]
    assert await failure_reasons(world, SHOP) == ["invalid_compose"]
    assert await failure_reasons(world, INCLUDING_STACK) == [
        "refused_construct",
        "file_in_use",
        "changed_since_review",
    ]


async def test_a_reader_sees_plain_content_and_only_that_a_secret_is_set_and_has_no_write_action(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
) -> None:
    base = f"{stacks_path(world)}/{SHOP}"
    await seed_stack(world.app, world.remote_id, SHOP, ENV_USING, files=[PLAIN_FILE, SECRET_FILE])
    await plant_control(world.app, world.remote_id, "stack", SHOP, public=True, level="read")
    writes = (f"{base}/files/-/new", f"{base}/files/{PLAIN_NAME}/edit")
    removal = f"{base}/files/{SECRET_NAME}/remove"

    reader = await standard_client.get(base, params={"tab": "files"})
    writer = await admin_client.get(base, params={"tab": "files"})
    forms = [await standard_client.get(path) for path in (*writes, removal)]
    posts = [
        await standard_client.post(path, data=csrf_form(standard_client, name="x.env", content="x"))
        for path in (*writes, removal)
    ]
    updates = await stack_rows(world, UPDATED_ROWS, (SHOP,))

    shown = flat(reader)
    assert reader.status_code == OK
    assert f"{PLAIN_NAME} services.web.env_file environment file Plain" in shown
    assert PLAIN_BODY.strip() in shown
    assert f"{SECRET_NAME} {NOT_USED} Secret Set Not shown" in shown
    assert SECRET_BODY.strip() not in reader.text
    assert "Add file" not in shown and "/files/" not in reader.text
    assert 'aria-label="Edit app.conf"' not in reader.text and "Actions" not in shown
    assert "Add file" in flat(writer) and 'aria-label="Remove app.key"' in writer.text
    assert [r.status_code for r in forms] == [FORBIDDEN] * 3
    assert [r.status_code for r in posts] == [FORBIDDEN] * 3
    assert updates == []


async def statuses_for(client: AsyncClient, base: str, names: tuple[str, ...]) -> set[int]:
    """Return every status a role gets from the edit and remove pages, read and posted."""
    found = set()
    for name in names:
        for page in ("edit", "remove"):
            found.add((await client.get(f"{base}/files/{name}/{page}")).status_code)
            posted = await client.post(f"{base}/files/{name}/{page}", data=csrf_form(client))
            found.add(posted.status_code)
    return found


async def test_an_unknown_or_malformed_file_name_is_not_found_before_any_read(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
) -> None:
    base = f"{stacks_path(world)}/{SHOP}"
    await seed_stack(world.app, world.remote_id, SHOP, SIMPLE, files=[PLAIN_FILE])
    requests = len(stack_host.requests)

    malformed_admin = await statuses_for(admin_client, base, MALFORMED_NAMES)
    malformed_standard = await statuses_for(standard_client, base, MALFORMED_NAMES)
    requests_after_malformed = len(stack_host.requests)
    current = await inputs_digest(world, SHOP)
    unknown = [
        (await admin_client.get(f"{base}/files/{UNKNOWN_NAME}/{page}")).status_code
        for page in ("edit", "remove")
    ]
    unknown_posts = [
        (await admin_client.post(f"{base}/files/{UNKNOWN_NAME}/{page}", data=data)).status_code
        for page, data in (
            ("edit", csrf_form(admin_client, content="x\n", expected_inputs_digest=current)),
            ("remove", csrf_form(admin_client, expected_inputs_digest=current)),
        )
    ]
    stored = await stack_rows(world, "SELECT name FROM stack_file")

    assert malformed_admin == malformed_standard == {NOT_FOUND}
    assert requests_after_malformed == requests
    assert unknown + unknown_posts == [NOT_FOUND] * 4
    assert stored == [{"name": PLAIN_NAME}]
