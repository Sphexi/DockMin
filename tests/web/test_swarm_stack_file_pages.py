"""The Swarm stack file pages: rows on the create and adopt forms, the Files tab and its writes."""

from httpx import AsyncClient, Response

from tests.auth_support import STANDARD_USERNAME
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.stack_support import job_id_of, settle_stack_job
from tests.web.access_panel_support import digest_in
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
from tests.web.test_swarm_stack_pages import (
    CLI_FILE,
    NAME,
    SHOP_FILE,
    Browser,
    base,
    namespace_service,
    shop_fields,
)

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN, NOT_FOUND, CONFLICT = 200, 303, 400, 403, 404, 409
EDITED_BODY = "edited-visible-content\n"
INCLUDED = "inc.yaml"
INCLUDING = "Outer"
INCLUDED_BODY = "services:\n  db:\n    image: busybox:latest\n"
BUILD_BODY = INCLUDED_BODY + "    build: .\n"
INCLUDING_FILE = f"include:\n  - {INCLUDED}\n" + CLI_FILE
ADDED = "File added. Redeploy the stack to apply it."
SAVED = "File saved. Redeploy the stack to apply it."
REMOVED = "File removed. Redeploy the stack to apply the change."
STALE_DIGEST = "0" * 64
MALFORMED_NAMES = (".env", "-x", "a%20b", "x" * 129, "a%2Fb")


async def create_shop(browser: Browser, world: World, *files: tuple[str, str, bool]) -> None:
    response = await browser.post(
        f"{base(world)}/-/new",
        data=browser.form(name=NAME, **shop_fields(), **file_fields(*files)),
        files={"compose_upload": ("c.yaml", SHOP_FILE.encode())},
    )
    await settle_stack_job(world.app, job_id_of(response))


async def submit(browser: Browser, path: str, **fields: str) -> Response:
    page = await browser.get(path)
    assert page.status_code == OK, path
    return await browser.client.post(path, data=form_values(page.text) | fields)


async def test_a_new_swarm_stack_stores_its_file_rows_and_the_files_tab_withholds_a_secret(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    browser = Browser(admin_client)

    await create_shop(browser, world, PLAIN_FILE, SECRET_FILE)
    tab = await browser.get(f"{root}/{NAME}", params={"tab": "files"})
    stored = await stack_rows(
        world, "SELECT name, is_secret, content, content_ciphertext FROM stack_file ORDER BY name"
    )
    event = await stack_rows(
        world,
        "SELECT json_extract(detail, '$.files') AS files, "
        "json_extract(detail, '$.secret_files') AS secret_files FROM activity_event "
        "WHERE action = 'stack_created'",
    )

    text = flat(tab)
    assert f"{PLAIN_NAME} {NOT_USED} Plain {len(PLAIN_BODY)} B" in text
    assert f"{SECRET_NAME} {NOT_USED} Secret Set Not shown" in text
    assert PLAIN_BODY.strip() in text and SECRET_BODY.strip() not in tab.text
    assert [(r["name"], r["content"]) for r in stored] == [
        (PLAIN_NAME, PLAIN_BODY),
        (SECRET_NAME, None),
    ]
    assert SECRET_BODY.strip() not in str(stored)
    assert event == [{"files": 2, "secret_files": SECRET_NAME}]


async def test_a_namespace_is_adopted_with_its_file_rows(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    browser = Browser(admin_client)
    swarm_host.add_service(namespace_service("Cli", "Cli_web"))

    adopted = await browser.post(
        f"{root}/-/adopt/Cli",
        data=browser.form(compose_file=CLI_FILE, **file_fields(PLAIN_FILE, SECRET_FILE)),
    )
    tab = await browser.get(f"{root}/Cli", params={"tab": "files"})
    event = await stack_rows(
        world,
        "SELECT json_extract(detail, '$.files') AS files, "
        "json_extract(detail, '$.secret_files') AS secret_files FROM activity_event "
        "WHERE action = 'stack_adopted'",
    )

    assert adopted.status_code == SEE_OTHER
    assert f"{PLAIN_NAME} {NOT_USED} Plain" in flat(tab)
    assert (
        f"{SECRET_NAME} {NOT_USED} Secret Set" in flat(tab) and SECRET_BODY.strip() not in tab.text
    )
    assert event == [{"files": 2, "secret_files": SECRET_NAME}]


async def test_a_swarm_writer_adds_edits_and_removes_files_and_each_change_writes_one_row(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    browser = Browser(admin_client)
    await create_shop(browser, world)
    mutations = len(swarm_host.mutating())
    page = f"{root}/{NAME}/files"

    added = await submit(browser, f"{page}/-/new", name=PLAIN_NAME, content=PLAIN_BODY)
    after_add = await land(admin_client, added)
    added_secret = await submit(
        browser, f"{page}/-/new", name=SECRET_NAME, content=SECRET_BODY, secret="on"
    )
    before = await stack_rows(world, "SELECT value_token FROM stack_file WHERE is_secret = 1")
    edit = await browser.get(f"{page}/{PLAIN_NAME}/edit")
    saved = await submit(browser, f"{page}/{PLAIN_NAME}/edit", content=EDITED_BODY)
    after_save = await land(admin_client, saved)
    kept = await submit(browser, f"{page}/{SECRET_NAME}/edit", content="", secret="on")
    kept_token = await stack_rows(world, "SELECT value_token FROM stack_file WHERE is_secret = 1")
    confirm = await browser.get(f"{page}/{PLAIN_NAME}/remove")
    removed = await admin_client.post(f"{page}/{PLAIN_NAME}/remove", data=form_values(confirm.text))
    after_remove = await land(admin_client, removed)
    updates = await stack_rows(world, UPDATED_ROWS, (NAME,))

    assert flash_texts(after_add.text) == [ADDED]
    assert "Changed since the last deploy" in flat(after_add)
    assert added_secret.status_code == SEE_OTHER
    assert PLAIN_BODY.strip() in edit.text
    assert flash_texts(after_save.text) == [SAVED]
    assert kept.status_code == SEE_OTHER and kept_token == before
    assert f"Remove file {PLAIN_NAME} from {NAME}?" in flat(confirm)
    assert "No document names this file." in flat(confirm)
    assert flash_texts(after_remove.text) == [REMOVED]
    assert [(u["result"], u["written"], u["removed"], u["kind"]) for u in updates] == [
        ("success", PLAIN_NAME, None, "plain"),
        ("success", SECRET_NAME, None, "secret"),
        ("success", PLAIN_NAME, None, "plain"),
        ("success", SECRET_NAME, None, "secret"),
        ("success", None, PLAIN_NAME, "plain"),
    ]
    assert len(swarm_host.mutating()) == mutations


async def test_a_swarm_file_change_the_stack_would_not_survive_or_a_stale_one_is_refused(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    browser = Browser(admin_client)
    await seed_stack(
        world.app,
        world.remote_id,
        INCLUDING,
        INCLUDING_FILE,
        kind="swarm",
        files=[(INCLUDED, INCLUDED_BODY, False)],
    )
    page = f"{root}/{INCLUDING}/files"

    broken = await submit(browser, f"{page}/{INCLUDED}/edit", content=BUILD_BODY)
    review = await browser.get(f"{page}/{INCLUDED}/remove")
    stale = await submit(
        browser,
        f"{page}/-/new",
        name=PLAIN_NAME,
        content=PLAIN_BODY,
        expected_inputs_digest=STALE_DIGEST,
    )
    held = await browser.client.post(
        f"{page}/{INCLUDED}/remove",
        data=form_values((await browser.get(f"{page}/-/new")).text),
    )
    stored = await stack_rows(world, "SELECT name, content FROM stack_file")
    failures = await stack_rows(world, UPDATED_ROWS, (INCLUDING,))

    assert broken.status_code == BAD_REQUEST
    assert "dockmin cannot build images for a stack." in flat(broken)
    assert 'data-goto-line="4"' in broken.text
    assert "A document still names this file" in flat(review)
    assert stale.status_code == CONFLICT and "The stack changed" in flat(stale)
    assert held.status_code == CONFLICT and f"{INCLUDED} is still used at" in flat(held)
    assert stored == [{"name": INCLUDED, "content": INCLUDED_BODY}]
    assert [f["reason"] for f in failures] == [
        "refused_construct",
        "changed_since_review",
        "file_in_use",
    ]


async def test_a_swarm_reader_sees_plain_content_has_no_write_action_and_bad_names_are_not_found(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    swarm_stacks_world: World,
    swarm_host: SwarmEngine,
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    admin, reader = Browser(admin_client), Browser(standard_client)
    await create_shop(admin, world, PLAIN_FILE, SECRET_FILE)
    panel = await admin.get(f"{root}/{NAME}?tab=access")
    await admin.post(
        f"{root}/{NAME}/access",
        data=admin.form(
            visibility="restricted",
            access_level="read",
            add_users=STANDARD_USERNAME,
            expected_digest=digest_in(panel.text),
        ),
    )
    page = f"{root}/{NAME}/files"

    tab = await reader.get(f"{root}/{NAME}", params={"tab": "files"})
    forms = [await reader.get(f"{page}/{path}") for path in ("-/new", f"{PLAIN_NAME}/edit")]
    forms.append(await reader.get(f"{page}/{SECRET_NAME}/remove"))
    posts = [
        await reader.client.post(f"{page}/{path}", data=reader.form(name="x.env", content="x"))
        for path in ("-/new", f"{PLAIN_NAME}/edit", f"{SECRET_NAME}/remove")
    ]
    malformed = {
        (await client.get(f"{page}/{name}/{action}")).status_code
        for client in (admin_client, standard_client)
        for name in MALFORMED_NAMES
        for action in ("edit", "remove")
    }
    unknown = [await admin.get(f"{page}/missing.env/{action}") for action in ("edit", "remove")]

    text = flat(tab)
    assert f"{PLAIN_NAME} {NOT_USED} Plain" in text and PLAIN_BODY.strip() in text
    assert f"{SECRET_NAME} {NOT_USED} Secret Set Not shown" in text
    assert "Add file" not in text and "/files/" not in tab.text
    assert [r.status_code for r in forms + posts] == [FORBIDDEN] * 6
    assert malformed == {NOT_FOUND}
    assert [r.status_code for r in unknown] == [NOT_FOUND] * 2
