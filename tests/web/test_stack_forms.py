"""The stack forms: validation, refusals, confirmations bound to what was reviewed, and adoption."""

import re
from collections import Counter
from typing import Any

import pytest
from httpx import AsyncClient, Response

from app.services.activity import UserActor
from app.services.object_locks import LockHolder, ObjectKey
from app.services.stack_deploy import StackRejected, StackValidator
from app.services.stack_document import MAX_COMPOSE_BYTES
from app.web.limits import MAX_BODY_BYTES
from tests.access_support import container_id
from tests.auth_support import csrf_form
from tests.container_support import STACK_LABEL, plant_control
from tests.docker_engine.container_engine import Publish
from tests.stack_support import (  # noqa: F401
    PROJECT_LABEL,
    SECRET_SENTINEL,
    SERVICE_LABEL,
    job_id_of,
    settle_stack_job,
    stopped_stack_jobs,
    variable_fields,
)
from tests.web.environment_support import World, filled, rows
from tests.web.flash_support import flash_texts, land, location
from tests.web.helpers import form_values
from tests.web.registry_support import visible_text

pytestmark = pytest.mark.usefixtures("stopped_stack_jobs")

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN, CONFLICT = 200, 303, 400, 403, 409
TOO_LARGE = 413
HTMX = {"HX-Request": "true"}
NAME = "x1"
COMPOSE_FILE = """\
services:
  web:
    image: nginx:1
    environment:
      - TOKEN=${TOKEN}
"""
BUILD_FILE = "services:\n  a:\n    build: .\n    image: x\n"
SECRET_ROW = variable_fields(("TOKEN", SECRET_SENTINEL, True))
OVERSIZE_FILE = b"#" * (MAX_COMPOSE_BYTES + 1)
UPLOAD = "compose_upload"


async def failures(world: World) -> list[tuple[str, str, str]]:
    """Return the (action, target, reason) of every recorded stack failure, oldest first."""
    found = await rows(
        world,
        "SELECT action, target_id, json_extract(detail, '$.reason') AS reason "
        "FROM activity_event WHERE target_type = 'stack' AND result = 'failure' ORDER BY id",
    )
    return [(row["action"], row["target_id"], row["reason"]) for row in found]


def stacks_path(world: World, environment_id: int | None = None) -> str:
    return f"/environments/{environment_id or world.remote_id}/stacks"


def text_of(response: Response) -> str:
    return " ".join(visible_text(response.text).split())


def leaked(secret: str, *responses: Response) -> list[str]:
    return [str(r.url.path) for r in responses if secret in r.text]


async def post_new(
    client: AsyncClient, base: str, path: str, *, files: Any = None, **fields: str
) -> Response:
    return await client.post(f"{base}{path}", data=csrf_form(client, **fields), files=files)


REFUSALS: dict[str, tuple[dict[str, Any], dict[str, Any] | None, int, str]] = {
    "a name with a capital": (
        {"name": "Blog", "compose_file": COMPOSE_FILE},
        None,
        BAD_REQUEST,
        "1 to 63 lower-case letters, digits",
    ),
    "neither a paste nor a file": (
        {"name": NAME},
        None,
        BAD_REQUEST,
        "Paste a Compose file or choose one to upload.",
    ),
    "both a paste and a file": (
        {"name": NAME, "compose_file": COMPOSE_FILE},
        {UPLOAD: ("compose.yaml", COMPOSE_FILE.encode())},
        BAD_REQUEST,
        "Use either the text box or a file, not both.",
    ),
    "a file that is not YAML": (
        {"name": NAME, "compose_file": "services: [\n"},
        None,
        BAD_REQUEST,
        "The Compose file is not valid YAML",
    ),
    "a build section": (
        {"name": NAME, "compose_file": BUILD_FILE},
        None,
        BAD_REQUEST,
        "dockmin cannot build images for a stack.",
    ),
    "a pasted file over the cap": (
        {"name": NAME, "compose_file": "a" * (MAX_COMPOSE_BYTES + 1)},
        None,
        BAD_REQUEST,
        "The Compose file can be at most 128 KiB.",
    ),
    "an uploaded file over the cap": (
        {"name": NAME},
        {UPLOAD: ("compose.yaml", OVERSIZE_FILE)},
        BAD_REQUEST,
        "The Compose file can be at most 128 KiB.",
    ),
    "a body over the request cap": (
        {"name": NAME},
        {UPLOAD: ("compose.yaml", b"#" * (MAX_BODY_BYTES + 1))},
        TOO_LARGE,
        "",
    ),
}


async def test_a_refused_new_stack_form_says_why_keeps_the_input_and_never_echoes_a_secret(
    admin_client: AsyncClient, world: World, stack_host: object
) -> None:
    base = stacks_path(world)
    started = len(world.app.state.engine.stack_jobs)
    checked = {
        label: await post_new(
            admin_client, base, "/-/validate", files=files, **(fields | SECRET_ROW)
        )
        for label, (fields, files, _, _) in REFUSALS.items()
    }
    created = {
        label: await post_new(admin_client, base, "/-/new", files=files, **(fields | SECRET_ROW))
        for label, (fields, files, _, _) in REFUSALS.items()
    }
    page = await post_new(
        admin_client, base, "/-/validate", **(REFUSALS["a build section"][0] | SECRET_ROW)
    )
    scripted = await admin_client.post(
        f"{base}/-/validate",
        data=csrf_form(admin_client, **(REFUSALS["a build section"][0] | SECRET_ROW)),
        headers=HTMX,
    )

    expected = {label: status for label, (_, _, status, _) in REFUSALS.items()}
    assert {label: r.status_code for label, r in checked.items()} == expected
    assert {label: r.status_code for label, r in created.items()} == expected
    assert [
        label
        for label, (_, _, _, message) in REFUSALS.items()
        if message not in text_of(checked[label]) or message not in text_of(created[label])
    ] == []
    assert leaked(SECRET_SENTINEL, *checked.values(), *created.values(), scripted) == []
    assert scripted.status_code == BAD_REQUEST and "<html" not in scripted.text
    assert "services.a.build" in text_of(scripted)
    assert page.status_code == BAD_REQUEST and "<html" in page.text
    assert len(world.app.state.engine.stack_jobs) == started


PLAIN_FILE = COMPOSE_FILE + "      - PLAIN=${PLAIN}\n"


async def test_a_new_stack_is_checked_with_and_without_script_then_created_by_paste(
    admin_client: AsyncClient, world: World, stack_host: object
) -> None:
    base = stacks_path(world)
    fields = {"name": NAME, "compose_file": PLAIN_FILE} | variable_fields(
        ("PLAIN", "plain-visible-value", False), ("TOKEN", SECRET_SENTINEL, True)
    )

    scripted = await admin_client.post(
        f"{base}/-/validate", data=csrf_form(admin_client, **fields), headers=HTMX
    )
    plain = await post_new(admin_client, base, "/-/validate", **fields)
    more = await post_new(admin_client, base, "/-/new", rows="variable", **fields)
    created = await post_new(admin_client, base, "/-/new", **fields)
    await settle_stack_job(world.app, job_id_of(created))
    taken = [
        await post_new(admin_client, base, "/-/validate", **fields),
        await post_new(admin_client, base, "/-/new", **fields),
    ]

    assert scripted.status_code == OK and "<html" not in scripted.text
    assert "The file is valid" in text_of(scripted) and "Images nginx:1" in text_of(scripted)
    assert "Mounts web nginx:1 — default 0" in text_of(scripted)
    assert plain.status_code == OK and "The file is valid" in text_of(plain)
    kept = form_values(plain.text)
    assert (kept["name"], kept["variable-0-value"]) == (NAME, "plain-visible-value")
    assert (kept["variable-1-secret"], kept["variable-1-value"]) == ("on", "")
    assert "      - PLAIN=${PLAIN}" in plain.text
    assert more.status_code == OK and "The file is valid" not in more.text
    assert form_values(more.text)["variable-3-name"] == ""
    assert "variable-5-name" in more.text
    assert [r.status_code for r in taken] == [CONFLICT, CONFLICT]
    assert all("already exists on this environment" in text_of(r) for r in taken)
    assert form_values(taken[1].text)["name"] == NAME
    assert leaked(SECRET_SENTINEL, scripted, plain, more, *taken) == []


BLOG, CACHE = "blog", "cache"
VOLUME_FILE = """\
services:
  web:
    image: nginx:1
    volumes:
      - data:/data
volumes:
  data: {}
"""
SIMPLE_FILE = "services:\n  web:\n    image: nginx:1\n"
WRONG_DIGEST = "0" * 64
STALE = "The stack changed"
STALE_REASON = "changed_since_review"
REVIEWED_FIELDS = (
    "expected_file_digest",
    "expected_containers",
    "expected_stack_id",
    "expected_networks",
    "expected_volumes",
)


async def create(client: AsyncClient, world: World, name: str, file: str) -> None:
    created = await post_new(client, stacks_path(world), "/-/new", name=name, compose_file=file)
    await settle_stack_job(world.app, job_id_of(created))


async def stale_posts(
    client: AsyncClient, path: str, reviewed: dict[str, str], *fields: str
) -> dict[str, Response]:
    """Post a confirmation once per field with that field wrong, and once with none of them."""
    wrong = {
        field: await client.post(path, data=reviewed | {field: WRONG_DIGEST}) for field in fields
    }
    wrong["none"] = await client.post(path, data=csrf_form(client))
    return wrong


STRAY = container_id(0x4F)


def add_stray(host: Any, project: str) -> None:
    """Put a container on the host that carries the project's label and no service of its file."""
    host.add(
        STRAY,
        "stray",
        image="nginx:1",
        labels={PROJECT_LABEL: project, SERVICE_LABEL: "ghost"},
    )


async def test_the_pages_that_delete_orphans_list_them_as_a_group_of_their_own(
    admin_client: AsyncClient, world: World, stack_host: Any
) -> None:
    base = stacks_path(world)
    await create(admin_client, world, BLOG, VOLUME_FILE)
    add_stray(stack_host, BLOG)

    plain_remove = await admin_client.get(f"{base}/{BLOG}/remove")
    force_remove = await admin_client.get(f"{base}/{BLOG}/remove", params={"orphans": "1"})
    plain_redeploy = await admin_client.get(f"{base}/{BLOG}/redeploy")
    force_redeploy = await admin_client.get(f"{base}/{BLOG}/redeploy", params={"orphans": "1"})

    assert re.search(
        r"Containers that are deleted.*blog-web-1.*Containers that are kept.*stray",
        text_of(plain_remove),
    )
    assert "leaves them as they are" in text_of(plain_remove)
    assert 'name="remove_orphans"' not in plain_remove.text
    assert re.search(
        r"Containers that are deleted.*blog-web-1.*Also removed.*stray", text_of(force_remove)
    )
    assert 'name="remove_orphans" value="yes"' in force_remove.text
    assert 'name="confirm_force" value="yes"' in force_remove.text
    assert 'name="remove_orphans"' not in plain_redeploy.text
    assert re.search(
        r"Containers that are deleted.*stray.*Containers that may be recreated.*blog-web-1",
        text_of(force_redeploy),
    )
    assert 'name="remove_orphans" value="yes"' in force_redeploy.text
    assert 'name="confirm_force" value="yes"' in force_redeploy.text


async def test_removing_or_redeploying_with_orphans_needs_the_force_marker_to_act(
    admin_client: AsyncClient, world: World, stack_host: Any
) -> None:
    base = stacks_path(world)
    await create(admin_client, world, BLOG, VOLUME_FILE)
    add_stray(stack_host, BLOG)
    removal = form_values((await admin_client.get(f"{base}/{BLOG}/remove")).text)
    redeploy = form_values((await admin_client.get(f"{base}/{BLOG}/redeploy")).text)
    jobs_before = len(world.app.state.engine.stack_jobs)

    unmarked_removal = await admin_client.post(
        f"{base}/{BLOG}/remove", data=removal | {"remove_orphans": "yes"}
    )
    unmarked_redeploy = await admin_client.post(
        f"{base}/{BLOG}/redeploy", data=redeploy | {"remove_orphans": "yes"}
    )
    refused_jobs = len(world.app.state.engine.stack_jobs)
    marked = await admin_client.post(
        f"{base}/{BLOG}/remove",
        data=removal | {"remove_orphans": "yes", "confirm_force": "yes"},
    )
    await settle_stack_job(world.app, job_id_of(marked))

    assert (unmarked_removal.status_code, unmarked_redeploy.status_code) == (BAD_REQUEST,) * 2
    assert refused_jobs == jobs_before
    assert STRAY not in stack_host.boxes
    assert marked.status_code == SEE_OTHER


async def test_a_stale_confirmation_changes_nothing_and_a_current_one_acts_on_what_was_reviewed(
    admin_client: AsyncClient, world: World, stack_host: Any
) -> None:
    base = stacks_path(world)
    await create(admin_client, world, BLOG, VOLUME_FILE)
    await create(admin_client, world, CACHE, SIMPLE_FILE)
    redeploy_page = await admin_client.get(f"{base}/{BLOG}/redeploy")
    remove_page = await admin_client.get(f"{base}/{BLOG}/remove")
    volumes_page = await admin_client.get(f"{base}/{BLOG}/remove", params={"volumes": "1"})
    forget_page = await admin_client.get(f"{base}/{CACHE}/forget")
    redeploy = form_values(redeploy_page.text)
    removal = form_values(remove_page.text)
    with_volumes = form_values(volumes_page.text)
    forgetting = form_values(forget_page.text)
    containers_before = set(stack_host.boxes)
    jobs_before = len(world.app.state.engine.stack_jobs)

    stale_redeploys = await stale_posts(
        admin_client,
        f"{base}/{BLOG}/redeploy",
        redeploy,
        "expected_file_digest",
        "expected_containers",
    )
    stale_removals = await stale_posts(
        admin_client,
        f"{base}/{BLOG}/remove",
        removal,
        "expected_stack_id",
        "expected_containers",
        "expected_networks",
    )
    stale_volume_removals = await stale_posts(
        admin_client,
        f"{base}/{BLOG}/remove",
        with_volumes,
        "expected_stack_id",
        "expected_containers",
        "expected_networks",
    )
    stale_forgets = await stale_posts(
        admin_client, f"{base}/{CACHE}/forget", forgetting, "expected_stack_id"
    )
    survivors = [await admin_client.get(f"{base}/{name}") for name in (BLOG, CACHE)]
    jobs_after = len(world.app.state.engine.stack_jobs)
    recorded = await failures(world)
    stale_volumes = await admin_client.post(
        f"{base}/{BLOG}/remove", data=with_volumes | {"expected_volumes": WRONG_DIGEST}
    )
    await settle_stack_job(world.app, job_id_of(stale_volumes))
    stale_volumes_job = await admin_client.get(stale_volumes.headers["location"])
    volumes_after_stale = set(stack_host.volumes)

    stale = [
        *stale_redeploys.values(),
        *stale_removals.values(),
        *stale_volume_removals.values(),
        *stale_forgets.values(),
    ]
    assert {field for field in REVIEWED_FIELDS if f'name="{field}"' in redeploy_page.text} == {
        "expected_file_digest",
        "expected_containers",
    }
    assert {field for field in REVIEWED_FIELDS if f'name="{field}"' in remove_page.text} == {
        "expected_stack_id",
        "expected_containers",
        "expected_networks",
    }
    assert 'name="expected_volumes"' in volumes_page.text
    assert 'name="expected_stack_id"' in forget_page.text
    assert {r.status_code for r in stale} == {CONFLICT}
    assert [r for r in stale if STALE not in text_of(r)] == []
    assert [r for r in stale if "Review again" not in text_of(r)] == []
    assert [r for r in stale if r.headers.get("cache-control") != "no-store"] == []
    assert jobs_after == jobs_before
    assert Counter(recorded) == {
        ("stack_redeployed", BLOG, STALE_REASON): len(stale_redeploys),
        ("stack_removed", BLOG, STALE_REASON): len(stale_removals) + len(stale_volume_removals),
        ("stack_forgotten", CACHE, STALE_REASON): len(stale_forgets),
    }
    assert set(stack_host.boxes) == containers_before
    assert [r.status_code for r in survivors] == [OK, OK]

    assert "changed since you reviewed it; nothing was changed" in text_of(stale_volumes_job)
    assert volumes_after_stale == {"blog_data"} and set(stack_host.boxes) == containers_before

    removed = await admin_client.post(f"{base}/{BLOG}/remove", data=with_volumes)
    await settle_stack_job(world.app, job_id_of(removed))
    removal_job = await admin_client.get(removed.headers["location"])
    forgotten = await admin_client.post(f"{base}/{CACHE}/forget", data=forgetting)
    listing = await admin_client.get(forgotten.headers["location"])

    assert "Succeeded Remove blog" in text_of(removal_job)
    assert "Deleted 1 volume: blog_data" in text_of(removal_job)
    assert set(stack_host.volumes) == set()
    assert not [b for b in stack_host.boxes.values() if b.labels.get(PROJECT_LABEL) == BLOG]
    assert forgotten.status_code == SEE_OTHER
    assert "Stack forgotten. Its containers are still on the host." in text_of(listing)
    assert f"{CACHE} 1 of 1 running" in text_of(listing)
    assert [b for b in stack_host.boxes.values() if b.labels.get(PROJECT_LABEL) == CACHE]


MINE = "mine"
READ_ONLY_CONTAINER = f"{0xC1:064x}"
UNWRITABLE = "You cannot change every container of this stack"
REFUSAL = "You cannot change every container Compose acts on every container"
PRIVILEGED_FILE = "services:\n  web:\n    image: nginx:1\n    privileged: true\n"


async def test_a_standard_user_is_not_offered_an_action_on_containers_they_cannot_change(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    stack_host: Any,
) -> None:
    base = stacks_path(world)
    await create(standard_client, world, MINE, SIMPLE_FILE)
    stack_host.add(
        READ_ONLY_CONTAINER,
        "mine-extra-1",
        state="running",
        labels={STACK_LABEL: MINE, SERVICE_LABEL: "extra"},
        image="nginx:1",
    )
    await plant_control(
        world.app, world.remote_id, "container", READ_ONLY_CONTAINER, public=True, level="read"
    )
    jobs_before = len(world.app.state.engine.stack_jobs)

    pages = {
        path: await standard_client.get(f"{base}/{MINE}{path}")
        for path in ("/redeploy", "/remove", "/remove?volumes=1")
    }
    admin_pages = {
        path: await admin_client.get(f"{base}/{MINE}{path}") for path in ("/redeploy", "/remove")
    }
    refused = {
        path: await standard_client.post(f"{base}/{MINE}{path}", data=form_values(page.text))
        for path, page in admin_pages.items()
    }

    assert {path: r.status_code for path, r in pages.items()} == dict.fromkeys(pages, OK)
    assert [path for path, r in pages.items() if UNWRITABLE not in text_of(r)] == []
    assert "1 container of this stack is outside what you can change" in text_of(pages["/remove"])
    assert [
        path
        for path, r in pages.items()
        if {"expected_containers", "expected_stack_id", "expected_file_digest"}
        & form_values(r.text).keys()
    ] == []
    assert [path for path, r in pages.items() if 'class="btn btn--danger"' in r.text] == []
    assert 'name="expected_file_digest"' in admin_pages["/redeploy"].text
    assert 'name="expected_stack_id"' in admin_pages["/remove"].text
    assert {path: r.status_code for path, r in refused.items()} == dict.fromkeys(refused, 403)
    assert [path for path, r in refused.items() if REFUSAL not in text_of(r)] == []
    assert len(world.app.state.engine.stack_jobs) == jobs_before
    assert await failures(world) == [
        ("stack_redeployed", MINE, "restricted"),
        ("stack_removed", MINE, "restricted"),
    ]


async def test_a_restricted_option_is_named_in_words_and_a_standard_user_is_refused_it(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World, stack_host: object
) -> None:
    base = stacks_path(world)
    fields = {"name": NAME, "compose_file": PRIVILEGED_FILE}

    allowed = await post_new(admin_client, base, "/-/validate", **fields)
    refused = await post_new(standard_client, base, "/-/validate", **fields)
    scripted = await standard_client.post(
        f"{base}/-/validate", data=csrf_form(standard_client, **fields), headers=HTMX
    )
    created = await post_new(standard_client, base, "/-/new", **fields)

    assert allowed.status_code == OK
    assert "restricted for standard users" in text_of(allowed)
    assert "<li>privileged mode</li>" in allowed.text and '<li class="mono">' not in allowed.text
    assert [r.status_code for r in (refused, scripted, created)] == [FORBIDDEN] * 3
    assert "The file uses options your role cannot deploy" in text_of(scripted)
    assert "services.web.privileged Only administrators may use privileged mode." in text_of(
        scripted
    )
    assert "<html" not in scripted.text
    assert len(world.app.state.engine.stack_jobs) == 0
    assert await failures(world) == [("stack_created", NAME, "restricted")]


SHOP, CRON, FRESH = "shop", "cron", "fresh"
CRON_CONTAINER = f"{0xD1:064x}"
REFERENCE = "nginx:1"
CHOICE_REQUIRED = "credential_choice_required"
BUSY_REASON = "object_busy"
HOLDER_ID = 4242
HOLDER_NAME = "holder"


def choosing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a deploying check ask for a credential choice until one is posted."""
    real = StackValidator.validate

    async def validate(self: Any, actor: Any, request: Any, **kwargs: Any) -> Any:
        if request.deploy and not request.choices:
            raise StackRejected({REFERENCE: "Choose how to pull this image."}, CHOICE_REQUIRED)
        return await real(self, actor, request, **kwargs)

    monkeypatch.setattr(StackValidator, "validate", validate)


async def test_a_new_stack_asks_for_a_credential_choice_then_is_created_and_deployed_with_it(
    admin_client: AsyncClient, world: World, stack_host: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = stacks_path(world)
    choosing_credentials(monkeypatch)
    fields = {"name": FRESH, "compose_file": SIMPLE_FILE}

    asked = await post_new(admin_client, base, "/-/new", **fields)
    stored_after_asking = await admin_client.get(f"{base}/{FRESH}")
    chosen = await post_new(
        admin_client,
        base,
        "/-/new",
        **fields,
        **{"credential-0-reference": REFERENCE, "credential-0-choice": "anonymous"},
    )
    await settle_stack_job(world.app, job_id_of(chosen))
    deployed = await admin_client.get(f"{base}/{FRESH}")

    assert asked.status_code == CONFLICT
    assert 'name="credential-0-choice"' in asked.text and form_values(asked.text)["name"] == FRESH
    assert stored_after_asking.status_code == 404
    assert await failures(world) == [("stack_created", FRESH, CHOICE_REQUIRED)]
    assert chosen.status_code == SEE_OTHER and "/jobs/" in chosen.headers["location"]
    assert f"{FRESH}-web-1 Running" in text_of(deployed)


async def test_a_new_stack_form_refused_by_intake_records_nothing_and_a_taken_name_records_one(
    admin_client: AsyncClient, world: World, stack_host: object
) -> None:
    base = stacks_path(world)
    await create(admin_client, world, NAME, SIMPLE_FILE)

    capital = await post_new(admin_client, base, "/-/new", name="Blog", compose_file=SIMPLE_FILE)
    empty = await post_new(admin_client, base, "/-/new", name=NAME)
    taken = await post_new(admin_client, base, "/-/new", name=NAME, compose_file=SIMPLE_FILE)

    assert [r.status_code for r in (capital, empty, taken)] == [BAD_REQUEST, BAD_REQUEST, CONFLICT]
    assert await failures(world) == [("stack_created", NAME, "name_in_use")]


async def test_a_busy_stack_refuses_each_operation_and_records_the_failure_the_action_would(
    admin_client: AsyncClient, world: World, stack_host: Any
) -> None:
    base = stacks_path(world)
    await create(admin_client, world, BLOG, SIMPLE_FILE)
    redeploy = form_values((await admin_client.get(f"{base}/{BLOG}/redeploy")).text)
    removal = form_values((await admin_client.get(f"{base}/{BLOG}/remove")).text)
    jobs_before = len(world.app.state.engine.stack_jobs)
    holder = LockHolder(UserActor(HOLDER_ID), HOLDER_NAME, "redeploy")
    key = ObjectKey(world.remote_id, "stack", BLOG)

    async with world.app.state.engine.locks.hold(key, holder):
        refused = {
            "start": await admin_client.post(f"{base}/{BLOG}/start", data=csrf_form(admin_client)),
            "stop": await admin_client.post(f"{base}/{BLOG}/stop", data=csrf_form(admin_client)),
            "redeploy": await admin_client.post(f"{base}/{BLOG}/redeploy", data=redeploy),
            "remove": await admin_client.post(f"{base}/{BLOG}/remove", data=removal),
        }

    assert {name: r.status_code for name, r in refused.items()} == dict.fromkeys(refused, CONFLICT)
    assert [name for name, r in refused.items() if "This stack is busy" not in text_of(r)] == []
    assert len(world.app.state.engine.stack_jobs) == jobs_before
    assert await failures(world) == [
        ("stack_started", BLOG, BUSY_REASON),
        ("stack_stopped", BLOG, BUSY_REASON),
        ("stack_redeployed", BLOG, BUSY_REASON),
        ("stack_removed", BLOG, BUSY_REASON),
    ]


async def test_an_unmanaged_project_is_adopted_with_its_containers_named_and_credentials_chosen(
    admin_client: AsyncClient,
    world: World,
    stack_host: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = stacks_path(world)
    stack_host.add(
        CRON_CONTAINER, "cron-job-1", state="exited", labels={PROJECT_LABEL: CRON}, image="nginx:1"
    )
    adopt = f"{base}/-/adopt/{SHOP}"
    listing = await admin_client.get(base)
    page = await admin_client.get(adopt)
    fragment = await admin_client.post(
        f"{adopt}/validate", data=csrf_form(admin_client, compose_file=SIMPLE_FILE), headers=HTMX
    )
    checked = await admin_client.post(
        f"{adopt}/validate", data=csrf_form(admin_client, compose_file=SIMPLE_FILE)
    )
    broken = await admin_client.post(
        f"{adopt}/validate",
        data=csrf_form(admin_client, compose_file="services: [\n"),
        headers=HTMX,
    )
    unmanaged = await admin_client.get(f"{base}/-/adopt/nothere")
    as_new = await post_new(admin_client, base, "/-/validate", name=SHOP, compose_file=SIMPLE_FILE)
    choosing_credentials(monkeypatch)
    deploying = csrf_form(admin_client, compose_file=SIMPLE_FILE, deploy="yes")
    fragment_choice = await admin_client.post(f"{adopt}/validate", data=deploying, headers=HTMX)
    page_choice = await admin_client.post(f"{adopt}/validate", data=deploying)
    run_choice = await admin_client.post(adopt, data=deploying)
    chosen = await admin_client.post(
        adopt,
        data=deploying | {"credential-0-reference": REFERENCE, "credential-0-choice": "anonymous"},
    )
    await settle_stack_job(world.app, job_id_of(chosen))
    plain = await admin_client.post(
        f"{base}/-/adopt/{CRON}", data=csrf_form(admin_client, compose_file=SIMPLE_FILE)
    )
    detail = await land(admin_client, plain)
    after = await admin_client.get(base)

    assert 'href="' + adopt + '"' in listing.text
    assert "2 of 2 running" in text_of(page) and "1 of 2 running" not in text_of(page)
    assert "<li>web</li>" in page.text and "<li>db</li>" in page.text
    assert "Containers of this project that deploying may recreate" in text_of(page)
    assert f'formaction="{adopt}/validate"' in page.text and "Check the file" in page.text
    assert fragment.status_code == OK and "The file is valid" in text_of(fragment)
    assert checked.status_code == OK and "The file is valid" in text_of(checked)
    assert broken.status_code == BAD_REQUEST and "not valid YAML" in text_of(broken)
    assert unmanaged.status_code == 404
    assert as_new.status_code == CONFLICT and "already exists" in text_of(as_new)
    assert fragment_choice.status_code == CONFLICT
    assert 'name="credential-0-reference"' in fragment_choice.text
    assert 'name="credential-0-choice"' in fragment_choice.text
    assert page_choice.status_code == CONFLICT and 'name="credential-0-choice"' in page_choice.text
    assert run_choice.status_code == CONFLICT
    assert text_of(run_choice).count("Credentials for image pulls") == 1
    assert await failures(world) == [("stack_adopted", SHOP, CHOICE_REQUIRED)]
    assert chosen.status_code == SEE_OTHER and "/jobs/" in chosen.headers["location"]
    assert location(plain) == f"{base}/{CRON}"
    assert "Stack adopted. Nothing was changed on the host." in text_of(detail)
    assert "Adopted on remote-box" in text_of(detail)
    assert CRON_CONTAINER in stack_host.boxes
    assert f"{SHOP} Running" in text_of(after)
    assert "Every Compose project on the host is managed here." in text_of(after)


ROUTE_PREFIX = "/environments/{environment_id}/stacks"
WRONG_TOKEN = "W" * 43
FIXED_NAMES = ("new", "adopt")
MALFORMED_NAMES = ("Bad", "has.dot", "-dash", "x" * 64, "a%2Fb", "%2E%2E")
STREAMED_UPLOAD = "/contexts/-/upload"
POST_ROUTES, GET_ROUTES = 23, 18
DISMISSAL_ROUTES = {
    f"{ROUTE_PREFIX}/-/unmanaged/{{project}}/dismiss",
    f"{ROUTE_PREFIX}/-/unmanaged/{{project}}/restore",
}
MALFORMED_JOBS = ("short", "A" * 21, "A" * 23, "A" * 21 + "!")


def stack_paths(app: Any, method: str) -> list[str]:
    """Return every documented stack route that answers `method`, in the order of the schema."""
    return [
        path
        for path, operations in app.openapi()["paths"].items()
        if path.startswith(ROUTE_PREFIX) and method in operations
    ]


async def test_every_stack_post_needs_the_csrf_token_and_every_stack_route_needs_a_session(
    admin_client: AsyncClient, anonymous_client: AsyncClient, world: World, stack_host: object
) -> None:
    posts = [
        filled(p, environment_id=world.remote_id)
        for p in stack_paths(world.app, "post")
        if not p.endswith(STREAMED_UPLOAD)
    ]
    gets = [filled(p, environment_id=world.remote_id) for p in stack_paths(world.app, "get")]
    started = len(world.app.state.engine.stack_jobs)

    without = {path: (await admin_client.post(path, data={})).status_code for path in posts}
    wrong = {
        path: (await admin_client.post(path, data={"csrf_token": WRONG_TOKEN})).status_code
        for path in posts
    }
    signed_out_posts = {
        path: (await anonymous_client.post(path, data=csrf_form(anonymous_client)))
        for path in posts
    }
    signed_out_gets = {path: await anonymous_client.get(path) for path in gets}

    assert len(posts) == POST_ROUTES and len(gets) == GET_ROUTES
    assert set(stack_paths(world.app, "post")) >= DISMISSAL_ROUTES
    assert set(without.values()) == {403} and set(wrong.values()) == {403}
    assert {r.status_code for r in signed_out_posts.values()} == {SEE_OTHER}
    assert {r.status_code for r in signed_out_gets.values()} == {SEE_OTHER}
    assert {r.headers["location"].split("?")[0] for r in signed_out_gets.values()} == {"/login"}
    assert len(world.app.state.engine.stack_jobs) == started


async def test_stacks_named_like_the_fixed_routes_are_reachable_and_malformed_names_are_not(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World, stack_host: object
) -> None:
    base = stacks_path(world)
    await create(admin_client, world, "new", SIMPLE_FILE)
    await create(admin_client, world, "adopt", SIMPLE_FILE)

    details = {name: await admin_client.get(f"{base}/{name}") for name in FIXED_NAMES}
    editors = {name: await admin_client.get(f"{base}/{name}/edit") for name in FIXED_NAMES}
    form = await admin_client.get(f"{base}/-/new")
    listing = await admin_client.get(base)
    malformed = {
        role: {
            name: (
                (await client.get(f"{base}/{name}")).status_code,
                (await client.post(f"{base}/{name}/start", data=csrf_form(client))).status_code,
            )
            for name in MALFORMED_NAMES
        }
        for role, client in (("administrator", admin_client), ("standard", standard_client))
    }
    jobs = {
        job: (
            (await admin_client.get(f"{base}/-/jobs/{job}")).status_code,
            (await admin_client.get(f"{base}/-/jobs/{job}/events")).status_code,
        )
        for job in MALFORMED_JOBS
    }

    assert [r.status_code for r in details.values()] == [OK, OK]
    assert text_of(details["new"]).count("Stack on remote-box") == 1
    assert 'href="' + f"{base}/new" + '"' in listing.text
    assert [r.status_code for r in editors.values()] == [OK, OK]
    assert form.status_code == OK and "Create and deploy" in text_of(form)
    assert {pair for roles in malformed.values() for pair in roles.values()} == {(404, 404)}
    assert set(jobs.values()) == {(404, 404)}


RUNNING_PORT, STOPPED_PORT = 18080, 19090
WIDE_RANGE = "9000-10100"
HOLDER_NAME = "legacy-web"
OWN_NAME = "blog-web"
STOPPED_WARNING = (
    f"Service web: port {STOPPED_PORT}/tcp on every address is published by the container old, "
    "which is not running."
)
RUNNING_MESSAGE = (
    f"Port {RUNNING_PORT}/tcp on every address is already published by the container api."
)
WARNINGS_HEADING = "Check these before you deploy"
RESOLVE_HEADING = "Resolve these before redeploying"


def publishing(*ports: int | str, container_name: str | None = None) -> str:
    lines = ["services:", "  web:", "    image: nginx:1"]
    if container_name is not None:
        lines.append(f"    container_name: {container_name}")
    lines.append("    ports:")
    lines.extend(f"      - '{port}:80'" for port in ports)
    return "\n".join(lines) + "\n"


def hold_ports(host: Any) -> None:
    host.add(container_id(0x61), "api", publishes=[Publish(RUNNING_PORT)])
    host.add(container_id(0x62), "old", state="exited", publishes=[Publish(STOPPED_PORT)])
    host.add(container_id(0x63), HOLDER_NAME)


async def test_checking_a_file_shows_the_warnings_and_a_refused_check_shows_its_pairs_and_warnings(
    admin_client: AsyncClient, world: World, stack_host: Any
) -> None:
    base = stacks_path(world)
    hold_ports(stack_host)

    async def check(file: str, *, scripted: bool = True) -> Response:
        return await admin_client.post(
            f"{base}/-/validate",
            data=csrf_form(admin_client, name=NAME, compose_file=file),
            headers=HTMX if scripted else {},
        )

    warned = await check(publishing(STOPPED_PORT))
    port_refused = await check(publishing(RUNNING_PORT, STOPPED_PORT))
    name_refused = await check(publishing(7000, container_name=HOLDER_NAME))
    page_refused = await check(publishing(RUNNING_PORT), scripted=False)
    too_wide = await check(publishing(WIDE_RANGE))

    assert warned.status_code == OK and "<html" not in warned.text
    assert "The file is valid. 1 service. 1 warning." in text_of(warned)
    assert WARNINGS_HEADING in text_of(warned) and STOPPED_WARNING in text_of(warned)
    assert port_refused.status_code == CONFLICT and "Port in use" in text_of(port_refused)
    assert RUNNING_MESSAGE in text_of(port_refused) and STOPPED_WARNING in text_of(port_refused)
    assert WARNINGS_HEADING in text_of(port_refused)
    assert name_refused.status_code == CONFLICT
    assert "Container name in use" in text_of(name_refused)
    assert f"The container name {HOLDER_NAME} is already used by the container" in text_of(
        name_refused
    )
    assert page_refused.status_code == CONFLICT and "<html" in page_refused.text
    assert text_of(page_refused).count(RUNNING_MESSAGE) == 1
    assert too_wide.status_code == BAD_REQUEST and "Port range too large" in text_of(too_wide)


async def test_creating_flashes_the_warnings_and_a_running_holder_stops_the_create(
    admin_client: AsyncClient, world: World, stack_host: Any
) -> None:
    base = stacks_path(world)
    hold_ports(stack_host)
    started = len(world.app.state.engine.stack_jobs)

    created = await post_new(
        admin_client, base, "/-/new", name=BLOG, compose_file=publishing(STOPPED_PORT)
    )
    await settle_stack_job(world.app, job_id_of(created))
    flashed = flash_texts((await land(admin_client, created)).text)
    refused = await post_new(
        admin_client, base, "/-/new", name=CACHE, compose_file=publishing(RUNNING_PORT)
    )

    assert STOPPED_WARNING in flashed
    assert refused.status_code == CONFLICT
    assert text_of(refused).count(RUNNING_MESSAGE) == 1
    assert [r["name"] for r in await rows(world, "SELECT name FROM stack")] == [BLOG]
    assert len(world.app.state.engine.stack_jobs) == started + 1


async def test_the_redeploy_page_blocks_on_conflicts_and_a_refused_post_shows_them_once(
    admin_client: AsyncClient, world: World, stack_host: Any
) -> None:
    base = stacks_path(world)
    await create(
        admin_client,
        world,
        BLOG,
        publishing(RUNNING_PORT, STOPPED_PORT, container_name=OWN_NAME),
    )
    hold_ports(stack_host)
    stack_host.add(container_id(0x64), OWN_NAME)

    review = await admin_client.get(f"{base}/{BLOG}/redeploy")
    refused = await admin_client.post(f"{base}/{BLOG}/redeploy", data=form_values(review.text))
    for number in (0x61, 0x64):
        del stack_host.boxes[container_id(number)]
    clear = await admin_client.get(f"{base}/{BLOG}/redeploy")
    ran = await admin_client.post(f"{base}/{BLOG}/redeploy", data=form_values(clear.text))
    await settle_stack_job(world.app, job_id_of(ran))

    reviewed = text_of(review)
    assert review.status_code == OK and RESOLVE_HEADING in reviewed
    assert f"The container name {OWN_NAME} is already used by the container" in reviewed
    assert RUNNING_MESSAGE in reviewed and STOPPED_WARNING in reviewed
    assert "disabled>Redeploy stack" in review.text
    assert refused.status_code == CONFLICT and RESOLVE_HEADING not in text_of(refused)
    assert text_of(refused).count("is already published by the container api") == 1
    assert RESOLVE_HEADING not in text_of(clear) and "disabled>Redeploy stack" not in clear.text
    assert STOPPED_WARNING in text_of(clear)
    assert ran.status_code == SEE_OTHER
