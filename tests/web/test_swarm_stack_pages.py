"""Swarm stack pages through the test client: forms, confirmations, jobs and the gates."""

import asyncio
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient, Response

from app.compose_cli.binary import ComposeBinary
from app.services.stack_collisions import MANAGER_ONLY
from app.services.stack_deploy import StackOperationFailed, StackRejected
from app.web.stack_routes import problem_of
from tests.access_support import container_id
from tests.auth_support import STANDARD_USERNAME, csrf_form
from tests.docker_engine.container_engine import Publish
from tests.docker_engine.swarm_engine import ServicePort, SwarmEngine
from tests.stack_support import SECRET_SENTINEL, job_id_of, settle_stack_job, variable_fields
from tests.stream_support import read_stream
from tests.swarm_support import NAMESPACE_LABEL
from tests.web.access_panel_support import digest_in
from tests.web.environment_support import World, rows
from tests.web.flash_support import flash_texts, location
from tests.web.helpers import inline_code_in
from tests.web.image_support import make_set
from tests.web.registry_support import visible_text

NAME = "Shop"
MINE = "Mine"
HTMX = {"HX-Request": "true"}
OK = 200
SEE_OTHER = 303
BAD_REQUEST = 400
FORBIDDEN = 403
NOT_FOUND = 404
CONFLICT = 409
SERVICE_UNAVAILABLE = 503
BAD_GATEWAY = 502
RENDERED_STATUSES = (200, 400, 409, 422, 429, 502, 503)
JOB_ID_PLACEHOLDER = "A" * 22
STALE_DIGEST = "0" * 64
STALE_MEMBERS = "f" * 64
REGISTRY_IMAGE = "registry.example.com/app:1"
SHOP_FILE = """\
services:
  web:
    image: busybox:latest
    deploy:
      replicas: 2
    networks: [front]
    configs: [cfg]
    secrets: [sec]
  api:
    image: busybox:latest
networks:
  front: {}
configs:
  cfg:
    environment: CFGVAR
secrets:
  sec:
    environment: SECVAR
"""
REFUSED_FILE = "services:\n  web:\n    image: busybox:latest\n    container_name: nope\n"
MINE_FILE = "services:\n  web:\n    image: busybox:latest\n  api:\n    image: busybox:latest\n"
CAP_FILE = MINE_FILE + "    cap_add: [SYS_ADMIN]\n"
CLI_FILE = "services:\n  web:\n    image: busybox:latest\n"
OLD_COMPOSE = (
    "import json, sys\n"
    "if 'version' in sys.argv:\n"
    "    print(json.dumps({'version': 'v2.20.0'}))\n"
    "else:\n"
    "    print('unsupported', file=sys.stderr)\n"
    "    sys.exit(1)\n"
)
FORGET_WORDING = (
    "The stack's access control is kept and still applies to its objects. "
    "Nothing on the cluster is touched."
)


def base(world: World) -> str:
    return f"/environments/{world.remote_id}/swarm/stacks"


def shop_fields() -> dict[str, str]:
    return variable_fields(("CFGVAR", "config v1", False), ("SECVAR", SECRET_SENTINEL, True))


def hidden(response: Response, name: str) -> str:
    found = re.search(rf'name="{name}" value="([^"]*)"', response.text)
    assert found is not None, f"{name} is not on the page"
    return found.group(1)


class Browser:
    """A client that holds every response to the rules each page keeps."""

    def __init__(self, client: AsyncClient) -> None:
        self.client = client

    def checked(self, response: Response) -> Response:
        assert SECRET_SENTINEL not in response.text, response.url
        assert inline_code_in(response.text) == [], response.url
        assert "Traceback" not in response.text, response.url
        html = response.headers.get("content-type", "").startswith("text/html")
        if html and response.status_code in RENDERED_STATUSES:
            assert response.headers.get("cache-control") == "no-store", response.url
        return response

    async def get(self, path: str, **kwargs: Any) -> Response:
        return self.checked(await self.client.get(path, **kwargs))

    async def post(self, path: str, **kwargs: Any) -> Response:
        return self.checked(await self.client.post(path, **kwargs))

    def form(self, **fields: str) -> dict[str, str]:
        return csrf_form(self.client, **fields)


async def create_shop(browser: Browser, world: World, *, file: str = SHOP_FILE) -> str:
    response = await browser.post(
        f"{base(world)}/-/new",
        data=browser.form(name=NAME, **shop_fields()),
        files={"compose_upload": ("c.yaml", file.encode())},
    )
    job_id = job_id_of(response)
    await settle_stack_job(world.app, job_id)
    return job_id


def namespace_service(namespace: str, name: str) -> dict[str, Any]:
    return {
        "Name": name,
        "Labels": {NAMESPACE_LABEL: namespace},
        "Mode": {"Replicated": {"Replicas": 1}},
        "TaskTemplate": {"ContainerSpec": {"Image": "busybox:latest"}},
    }


async def test_the_administrator_creates_validates_redeploys_and_removes_a_stack(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    browser = Browser(admin_client)
    swarm_host.add_service(namespace_service("Cli", "Cli_app"))

    listing = await browser.get(root)
    assert listing.status_code == OK
    assert all(text in listing.text for text in ("Cli", "Not managed", "Adopt", "Create stack"))
    new = await browser.get(f"{root}/-/new")
    assert new.status_code == OK and 'name="csrf_token"' in new.text
    checked = await browser.post(
        f"{root}/-/validate",
        data=browser.form(name=NAME, compose_file=SHOP_FILE, **shop_fields()),
        headers=HTMX,
    )
    assert checked.status_code == OK and "The file is valid" in checked.text
    refused = await browser.post(
        f"{root}/-/validate",
        data=browser.form(name=NAME, compose_file=REFUSED_FILE, **shop_fields()),
        headers=HTMX,
    )
    assert refused.status_code == BAD_REQUEST and "container_name" in refused.text
    page_refused = await browser.post(
        f"{root}/-/validate",
        data=browser.form(name=NAME, compose_file=REFUSED_FILE, **shop_fields()),
    )
    assert page_refused.status_code == BAD_REQUEST and "<html" in page_refused.text
    refused_create = await browser.post(
        f"{root}/-/new", data=browser.form(name=NAME, compose_file=REFUSED_FILE, **shop_fields())
    )
    assert refused_create.status_code == BAD_REQUEST
    clash = await browser.post(
        f"{root}/-/new", data=browser.form(name="cli", compose_file=SHOP_FILE, **shop_fields())
    )
    assert clash.status_code == CONFLICT

    job_id = await create_shop(browser, world)
    job = await browser.get(f"{root}/-/jobs/{job_id}")
    assert job.status_code == OK and "Succeeded" in job.text and "Shop_web" in job.text
    streamed = await read_stream(world.app, admin_client, f"{root}/-/jobs/{job_id}/events")
    assert streamed.body.startswith("event: done") and SECRET_SENTINEL not in streamed.body

    listed = await browser.get(root)
    assert "Deployed" in listed.text and NAME in listed.text
    detail = await browser.get(f"{root}/{NAME}")
    assert detail.status_code == OK and "Shop_web" in detail.text
    assert (await browser.get(f"{root}/shop")).status_code == OK
    for tab in ("file", "variables", "access"):
        assert (await browser.get(f"{root}/{NAME}", params={"tab": tab})).status_code == OK
    fragment = await browser.get(f"{root}/{NAME}", params={"tab": "variables"}, headers=HTMX)
    assert "<html" not in fragment.text and "SECVAR" in fragment.text
    boosted = await browser.get(f"{root}/{NAME}", headers=HTMX | {"HX-Boosted": "true"})
    assert "<html" in boosted.text
    assert (await browser.get(f"{root}/{NAME}", params={"tab": "nope"})).status_code == NOT_FOUND
    editor = await browser.get(f"{root}/{NAME}/edit")
    assert editor.status_code == OK and "expected_file_digest" in editor.text

    review = await browser.get(f"{root}/{NAME}/redeploy")
    assert review.status_code == OK
    assert all(t in review.text for t in ("Redeploy stack", "expected_plan", "Update", "Shop_web"))
    plan, digest = hidden(review, "expected_plan"), hidden(review, "expected_file_digest")
    inputs = hidden(review, "expected_inputs_digest")
    stale = await browser.post(
        f"{root}/{NAME}/redeploy",
        data=browser.form(
            expected_file_digest=digest, expected_inputs_digest=inputs, expected_plan=STALE_DIGEST
        ),
    )
    assert stale.status_code == CONFLICT
    refusals = await rows(
        world,
        "SELECT result, detail FROM activity_event "
        "WHERE action = 'stack_redeployed' AND result = 'failure'",
    )
    assert len(refusals) == 1 and "changed_since_review" in refusals[0]["detail"]
    ran = await browser.post(
        f"{root}/{NAME}/redeploy",
        data=browser.form(
            expected_file_digest=digest, expected_inputs_digest=inputs, expected_plan=plan
        ),
    )
    redeploy_job = job_id_of(ran)
    await settle_stack_job(world.app, redeploy_job)
    assert "Succeeded" in (await browser.get(f"{root}/-/jobs/{redeploy_job}")).text

    removing = await browser.get(f"{root}/{NAME}/remove")
    assert removing.status_code == OK and "Volumes that are kept" in removing.text
    assert all(t in removing.text for t in ("Shop_web", "Shop_front", "Remove stack"))
    members, stack_id = hidden(removing, "expected_members"), hidden(removing, "expected_stack_id")
    stale_remove = await browser.post(
        f"{root}/{NAME}/remove",
        data=browser.form(expected_stack_id=stack_id, expected_members=STALE_MEMBERS),
    )
    assert stale_remove.status_code == CONFLICT
    gone = await browser.post(
        f"{root}/{NAME}/remove",
        data=browser.form(expected_stack_id=stack_id, expected_members=members),
    )
    remove_job = job_id_of(gone)
    await settle_stack_job(world.app, remove_job)
    removed = await browser.get(f"{root}/-/jobs/{remove_job}")
    assert "Succeeded" in removed.text and "Removed" in removed.text


async def test_the_forget_page_states_what_is_kept_and_forgetting_leaves_the_cluster_alone(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    browser = Browser(admin_client)
    await create_shop(browser, world)
    objects = len(swarm_host.services)

    page = await browser.get(f"{root}/{NAME}/forget")
    forgotten = await browser.post(
        f"{root}/{NAME}/forget",
        data=browser.form(expected_stack_id=hidden(page, "expected_stack_id")),
    )
    after = await browser.get(forgotten.headers["location"])

    assert page.status_code == OK and FORGET_WORDING in page.text
    assert "deletes its record of the stack and its stored file and variables" in page.text
    assert forgotten.status_code == SEE_OTHER and location(forgotten) == root
    assert flash_texts(after.text) == ["Stack forgotten. Its objects are still on the cluster."]
    assert len(swarm_host.services) == objects


async def test_the_administrator_adopts_a_namespace_and_the_first_redeploy_says_what_it_replaces(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    root = base(swarm_stacks_world)
    browser = Browser(admin_client)
    swarm_host.add_service(namespace_service("Cli", "Cli_web"))

    unmanaged = await browser.get(f"{root}/Cli")
    page = await browser.get(f"{root}/-/adopt/Cli")
    checked = await browser.post(
        f"{root}/-/adopt/Cli/validate", data=browser.form(compose_file=CLI_FILE), headers=HTMX
    )
    adopted = await browser.post(f"{root}/-/adopt/Cli", data=browser.form(compose_file=CLI_FILE))
    detail = await browser.get(adopted.headers["location"])
    review = await browser.get(f"{root}/Cli/redeploy")

    assert unmanaged.status_code == OK and "Adopt stack" in unmanaged.text
    assert page.status_code == OK and "Stack on the cluster" in page.text
    assert "The file is valid" in checked.text
    assert adopted.status_code == SEE_OTHER
    assert "Stack adopted" in detail.text and "adopted from objects created outside" in detail.text
    assert review.status_code == OK and "First redeploy of an adopted stack" in review.text
    assert swarm_host.mutating() == []


async def test_a_standalone_a_worker_and_a_never_contacted_environment_have_no_stack_routes(
    admin_client: AsyncClient, standard_client: AsyncClient, swarm_stacks_world: World
) -> None:
    world = swarm_stacks_world
    paths = (
        "",
        "/-/new",
        f"/{NAME}",
        f"/{NAME}/edit",
        f"/{NAME}/redeploy",
        f"/{NAME}/remove",
        f"/{NAME}/forget",
        "/-/adopt/Cli",
        f"/-/jobs/{JOB_ID_PLACEHOLDER}",
        f"/-/jobs/{JOB_ID_PLACEHOLDER}/events",
    )

    for client in (admin_client, standard_client):
        for environment_id in (world.local_id, world.down_id):
            for path in paths:
                response = await client.get(f"/environments/{environment_id}/swarm/stacks{path}")
                assert response.status_code == NOT_FOUND, (environment_id, path)


async def test_the_compose_list_points_a_manager_to_the_swarm_stacks_and_a_standalone_does_not(
    admin_client: AsyncClient, swarm_stacks_world: World
) -> None:
    world = swarm_stacks_world

    manager = await admin_client.get(f"/environments/{world.remote_id}/stacks")
    standalone = await admin_client.get(f"/environments/{world.local_id}/stacks")

    assert "Swarm stacks are listed under" in manager.text
    assert "Swarm stacks are listed under" not in standalone.text


async def test_every_post_without_a_csrf_token_is_refused(
    admin_client: AsyncClient, swarm_stacks_world: World
) -> None:
    root = base(swarm_stacks_world)
    posts = (
        "/-/new",
        "/-/validate",
        "/-/adopt/Cli",
        "/-/adopt/Cli/validate",
        f"/{NAME}/edit",
        f"/{NAME}/validate",
        f"/{NAME}/redeploy",
        f"/{NAME}/redeploy/review",
        f"/{NAME}/remove",
        f"/{NAME}/forget",
        f"/{NAME}/access",
        f"/{NAME}/access/delete",
    )

    for path in posts:
        response = await admin_client.post(f"{root}{path}", data={"compose_file": SHOP_FILE})
        assert response.status_code == FORBIDDEN, path


async def test_a_standard_user_is_refused_what_the_file_may_not_do_and_is_the_only_owner(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    swarm_stacks_world: World,
    swarm_host: SwarmEngine,
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    browser, admin = Browser(standard_client), Browser(admin_client)

    refused = await browser.post(
        f"{root}/-/new", data=browser.form(name=MINE, compose_file=CAP_FILE)
    )
    validated = await browser.post(
        f"{root}/-/validate", data=browser.form(name=MINE, compose_file=CAP_FILE), headers=HTMX
    )
    assert refused.status_code == FORBIDDEN and "services.api.cap_add" in refused.text
    assert validated.status_code == FORBIDDEN and "services.api.cap_add" in validated.text
    assert swarm_host.mutating() == []

    made = await browser.post(f"{root}/-/new", data=browser.form(name=MINE, compose_file=MINE_FILE))
    job_id = job_id_of(made)
    await settle_stack_job(world.app, job_id)
    assert "Succeeded" in (await browser.get(f"{root}/-/jobs/{job_id}")).text
    assert (await admin.get(f"{root}/-/jobs/{job_id}")).status_code == OK
    streamed = await read_stream(world.app, standard_client, f"{root}/-/jobs/{job_id}/events")
    assert streamed.body.startswith("event: done")
    for path in ("", "/edit", "/redeploy", "/remove"):
        assert (await browser.get(f"{root}/{MINE}{path}")).status_code == OK
    assert (await browser.get(f"{root}/{MINE}/forget")).status_code == FORBIDDEN
    assert (await browser.get(f"{root}/{MINE}", params={"tab": "access"})).status_code == FORBIDDEN

    editor = await browser.get(f"{root}/{MINE}/edit")
    digest = hidden(editor, "expected_file_digest")
    bad_save = await browser.post(
        f"{root}/{MINE}/edit",
        data=browser.form(compose_file=CAP_FILE, expected_file_digest=digest, next="save"),
    )
    stale_save = await browser.post(
        f"{root}/{MINE}/edit",
        data=browser.form(
            compose_file=MINE_FILE + "# x\n", expected_file_digest=STALE_DIGEST, next="save"
        ),
    )
    good_save = await browser.post(
        f"{root}/{MINE}/edit",
        data=browser.form(
            compose_file=MINE_FILE + "# x\n", expected_file_digest=digest, next="redeploy"
        ),
    )
    assert bad_save.status_code == FORBIDDEN and "services.api.cap_add" in bad_save.text
    assert stale_save.status_code == CONFLICT
    assert good_save.status_code == SEE_OTHER
    assert good_save.headers["location"].endswith(f"/{MINE}/redeploy")


async def test_a_member_the_user_cannot_change_blocks_their_redeploy_and_remove(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    swarm_stacks_world: World,
    swarm_host: SwarmEngine,
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    browser, admin = Browser(standard_client), Browser(admin_client)
    made = await browser.post(f"{root}/-/new", data=browser.form(name=MINE, compose_file=MINE_FILE))
    await settle_stack_job(world.app, job_id_of(made))
    api_id = next(i for i, s in swarm_host.services.items() if s["Spec"]["Name"] == f"{MINE}_api")
    service_path = f"/environments/{world.remote_id}/swarm/services/{api_id}"
    panel = await admin.get(f"{service_path}?tab=access")
    locked = await admin.post(
        f"{service_path}/access",
        data=admin.form(
            visibility="administrators_only",
            access_level="read",
            expected_digest=digest_in(panel.text),
        ),
    )
    assert locked.status_code == SEE_OTHER

    review = await browser.get(f"{root}/{MINE}/redeploy")
    remove_page = await browser.get(f"{root}/{MINE}/remove")
    admin_remove = await admin.get(f"{root}/{MINE}/remove")
    handbuilt = await browser.post(
        f"{root}/{MINE}/remove",
        data=browser.form(
            expected_stack_id=hidden(admin_remove, "expected_stack_id"),
            expected_members=hidden(admin_remove, "expected_members"),
        ),
    )
    stale_redeploy = await browser.post(
        f"{root}/{MINE}/redeploy",
        data=browser.form(expected_file_digest=STALE_DIGEST, expected_plan=STALE_DIGEST),
    )

    assert "cannot change every object" in review.text
    assert remove_page.status_code == OK and "cannot change every object" in remove_page.text
    assert 'name="expected_members"' not in remove_page.text
    assert handbuilt.status_code == FORBIDDEN
    assert stale_redeploy.status_code in (BAD_REQUEST, FORBIDDEN, CONFLICT)


async def test_a_read_grant_shows_the_stack_and_refuses_every_write_page(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    swarm_stacks_world: World,
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    admin, reader = Browser(admin_client), Browser(standard_client)
    await create_shop(admin, world)
    panel = await admin.get(f"{root}/{NAME}?tab=access")
    granted = await admin.post(
        f"{root}/{NAME}/access",
        data=admin.form(
            visibility="restricted",
            access_level="read",
            add_users=STANDARD_USERNAME,
            expected_digest=digest_in(panel.text),
        ),
    )
    assert granted.status_code == SEE_OTHER

    detail = await reader.get(f"{root}/{NAME}")

    assert detail.status_code == OK
    for path in ("/edit", "/redeploy", "/remove"):
        assert (await reader.get(f"{root}/{NAME}{path}")).status_code == FORBIDDEN


async def test_a_reference_with_two_credential_sets_asks_which_one_and_the_redeploy_asks_again(
    admin_client: AsyncClient, swarm_stacks_world: World
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    browser = Browser(admin_client)
    await make_set(admin_client, world, "alpha", default=True)
    await make_set(admin_client, world, "beta")
    text = f"services:\n  web:\n    image: {REGISTRY_IMAGE}\n"

    first = await browser.post(f"{root}/-/new", data=browser.form(name="Reg", compose_file=text))
    assert first.status_code == CONFLICT
    assert "Credential set for" in first.text and "credential-0-reference" in first.text
    choice = hidden_choice(first)
    chosen = {"credential-0-reference": REGISTRY_IMAGE, "credential-0-choice": choice}
    second = await browser.post(
        f"{root}/-/new", data=browser.form(name="Reg", compose_file=text, **chosen)
    )
    job_id = job_id_of(second)
    await settle_stack_job(world.app, job_id)
    assert "Succeeded" in (await browser.get(f"{root}/-/jobs/{job_id}")).text

    async with world.app.state.database.session() as db:
        await db.execute("DELETE FROM image_credential_binding")
    asked = await browser.get(f"{root}/Reg/redeploy")
    assert asked.status_code == OK and "credential-0-reference" in asked.text
    reviewed = await browser.post(f"{root}/Reg/redeploy/review", data=browser.form(**chosen))
    assert reviewed.status_code == OK and 'name="expected_plan"' in reviewed.text
    unchosen = await browser.post(f"{root}/Reg/redeploy/review", data=browser.form())
    assert unchosen.status_code == CONFLICT
    run = await browser.post(
        f"{root}/Reg/redeploy",
        data=browser.form(
            expected_file_digest=hidden(reviewed, "expected_file_digest"),
            expected_inputs_digest=hidden(reviewed, "expected_inputs_digest"),
            expected_plan=hidden(reviewed, "expected_plan"),
            **chosen,
        ),
    )
    await settle_stack_job(world.app, job_id_of(run))


def hidden_choice(response: Response) -> str:
    found = re.search(r'name="credential-0-choice" value="(\d+)"', response.text)
    assert found is not None
    return found.group(1)


async def test_a_running_job_shows_its_refresh_fallback_and_makes_the_stack_busy(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    browser = Browser(admin_client)
    swarm_host.gate = asyncio.Event()
    started = await browser.post(
        f"{root}/-/new", data=browser.form(name="Busy", compose_file=SHOP_FILE, **shop_fields())
    )
    job_id = job_id_of(started)
    await asyncio.wait_for(swarm_host.arrived.wait(), 10)

    running = await browser.get(f"{root}/-/jobs/{job_id}")
    live = await read_stream(
        world.app, admin_client, f"{root}/-/jobs/{job_id}/events", stop_after_events=1
    )
    review = await browser.get(f"{root}/Busy/redeploy")
    busy_redeploy = await browser.post(
        f"{root}/Busy/redeploy",
        data=browser.form(
            expected_file_digest=hidden(review, "expected_file_digest"),
            expected_inputs_digest=hidden(review, "expected_inputs_digest"),
            expected_plan=hidden(review, "expected_plan"),
        ),
    )
    remove = await browser.get(f"{root}/Busy/remove")
    busy_remove = await browser.post(
        f"{root}/Busy/remove",
        data=browser.form(
            expected_stack_id=hidden(remove, "expected_stack_id"),
            expected_members=hidden(remove, "expected_members"),
        ),
    )
    swarm_host.gate.set()
    await settle_stack_job(world.app, job_id)
    swarm_host.gate = None

    assert "data-job-stream" in running.text and "Refresh status" in running.text
    assert '<meta http-equiv="refresh"' in running.text and "<noscript>" in running.text
    assert live.body.startswith("event: progress")
    assert busy_redeploy.status_code == CONFLICT and "This stack is busy" in busy_redeploy.text
    assert busy_remove.status_code == CONFLICT and "This stack is busy" in busy_remove.text
    refusals = await rows(
        world,
        "SELECT action, detail FROM activity_event "
        "WHERE action IN ('stack_redeployed', 'stack_removed') AND result = 'failure'",
    )
    assert {r["action"] for r in refusals} == {"stack_redeployed", "stack_removed"}


async def test_the_form_variants_work_without_script_and_a_save_reports_what_changed(
    admin_client: AsyncClient, swarm_stacks_world: World
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    browser = Browser(admin_client)
    await create_shop(browser, world)

    more = await browser.post(
        f"{root}/-/new", data=browser.form(name="X", compose_file=SHOP_FILE, rows="variable")
    )
    page_check = await browser.post(
        f"{root}/{NAME}/validate",
        data=browser.form(compose_file=SHOP_FILE, **shop_fields()),
    )
    fragment_check = await browser.post(
        f"{root}/{NAME}/validate",
        data=browser.form(compose_file=SHOP_FILE, **shop_fields()),
        headers=HTMX,
    )
    more_edit = await browser.post(
        f"{root}/{NAME}/edit", data=browser.form(compose_file=SHOP_FILE, rows="variable")
    )
    saved = await browser.post(
        f"{root}/{NAME}/edit",
        data=browser.form(compose_file=SHOP_FILE, next="save", **shop_fields()),
    )
    note = await browser.get(saved.headers["location"])

    assert more.status_code == OK and "Add rows" in more.text
    assert "The file is valid" in page_check.text and "<html" in page_check.text
    assert "The file is valid" in fragment_check.text and "<html" not in fragment_check.text
    assert more_edit.status_code == OK
    assert saved.status_code == SEE_OTHER and "?" not in location(saved)
    assert "Changes saved" in note.text or "Nothing changed" in note.text


async def test_a_network_that_exists_with_other_settings_is_a_conflict_naming_the_network(
    admin_client: AsyncClient, swarm_stacks_world: World
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    browser = Browser(admin_client)
    await create_shop(browser, world)
    changed = SHOP_FILE.replace("  front: {}", "  front:\n    internal: true")

    saved = await browser.post(
        f"{root}/{NAME}/edit",
        data=browser.form(compose_file=changed, next="redeploy", **shop_fields()),
    )

    assert saved.status_code == CONFLICT and "networks.front" in saved.text


async def test_an_unready_compose_blocks_the_forms_and_leaves_remove_available(
    admin_client: AsyncClient, swarm_stacks_world: World, tmp_path: Path
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    browser = Browser(admin_client)
    await create_shop(browser, world)
    script = tmp_path / "old_compose.py"
    script.write_text(OLD_COMPOSE, encoding="utf-8")
    compose = world.app.state.engine.compose
    compose.use_binary(ComposeBinary((sys.executable, "-I", str(script))))
    await compose.detect()

    pages = {
        p: await browser.get(f"{root}{p}") for p in ("", "/-/new", f"/{NAME}", f"/{NAME}/edit")
    }
    refused = await browser.post(
        f"{root}/-/new", data=browser.form(name="Z", compose_file=SHOP_FILE, **shop_fields())
    )
    remove = await browser.get(f"{root}/{NAME}/remove")

    assert all("Docker Compose is not ready" in page.text for page in pages.values())
    assert re.search(
        r'<button type="submit" class="btn btn--primary" disabled>Create and deploy',
        pages["/-/new"].text,
    )
    assert refused.status_code == SERVICE_UNAVAILABLE
    assert (
        remove.status_code == OK and "Remove stack" in remove.text and "disabled" not in remove.text
    )


async def test_a_swarm_job_is_not_served_by_the_compose_routes(
    admin_client: AsyncClient, swarm_stacks_world: World
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    compose_root = f"/environments/{world.remote_id}/stacks"
    job_id = await create_shop(Browser(admin_client), world)

    swarm_page = await admin_client.get(f"{root}/-/jobs/{job_id}")
    compose_page = await admin_client.get(f"{compose_root}/-/jobs/{job_id}")
    compose_events = await admin_client.get(f"{compose_root}/-/jobs/{job_id}/events")

    assert swarm_page.status_code == OK
    assert compose_page.status_code == NOT_FOUND and compose_events.status_code == NOT_FOUND


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("changed_since_review", CONFLICT),
        ("preexisting_objects", CONFLICT),
        ("engine_rejected", BAD_GATEWAY),
        ("outcome_unknown", SERVICE_UNAVAILABLE),
        ("unreachable", SERVICE_UNAVAILABLE),
        ("not_swarm_manager", SERVICE_UNAVAILABLE),
    ],
)
def test_a_failed_stack_operation_maps_to_its_http_status(reason: str, status: int) -> None:
    problem = problem_of(StackOperationFailed(reason, "message"), None, None)  # type: ignore[arg-type]

    assert problem.status == status


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("network_differs", CONFLICT),
        ("restricted", FORBIDDEN),
        ("secret_in_config", BAD_REQUEST),
        ("container_name_in_use", CONFLICT),
        ("port_in_use", CONFLICT),
        ("port_range_too_large", BAD_REQUEST),
    ],
)
def test_a_rejected_stack_maps_to_its_http_status(reason: str, status: int) -> None:
    problem = problem_of(StackRejected({"networks.front": "x"}, reason), None, None)  # type: ignore[arg-type]

    assert problem.status == status


INGRESS_PORT, HOST_MODE_PORT = 18080, 19000
PORTS_FILE = (
    "services:\n  web:\n    image: busybox:latest\n    ports:\n"
    f"      - '{INGRESS_PORT}:80'\n"
    f"      - {{target: 81, published: {HOST_MODE_PORT}, mode: host}}\n"
)
HELD_BY_OTHER = (
    f"Port {INGRESS_PORT}/tcp on every address is already published by the service other."
)
MANAGER_WARNING = (
    f"Service web: port {HOST_MODE_PORT}/tcp on every address is published on the manager by "
    "the container batch. This conflicts if a task of the service is placed there."
)
WARNINGS_HEADING = "Check these before you deploy"


async def test_the_file_check_warns_and_a_redeploy_review_is_blocked_by_an_ingress_conflict(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world, root = swarm_stacks_world, base(swarm_stacks_world)
    browser = Browser(admin_client)
    swarm_host.add(container_id(1), "batch", publishes=[Publish(HOST_MODE_PORT)])
    check = {"name": NAME, "compose_file": PORTS_FILE, **shop_fields()}

    warned = await browser.post(f"{root}/-/validate", data=browser.form(**check), headers=HTMX)
    await create_shop(browser, world, file=PORTS_FILE)
    swarm_host.published_service("other", [ServicePort(INGRESS_PORT)])
    refused = await browser.post(
        f"{root}/-/validate",
        data=browser.form(**{**check, "name": MINE}),
        headers=HTMX,
    )
    blocked = await browser.get(f"{root}/{NAME}/redeploy")

    warned_text = " ".join(visible_text(warned.text).split())
    assert warned.status_code == OK and WARNINGS_HEADING in warned_text
    assert MANAGER_ONLY in warned_text and MANAGER_WARNING in warned_text
    for response in (refused, blocked):
        text = " ".join(visible_text(response.text).split())
        assert response.status_code == CONFLICT and "Port in use" in text
        assert text.count(HELD_BY_OTHER) == 1 and MANAGER_WARNING in text
    assert 'name="expected_plan"' not in blocked.text
