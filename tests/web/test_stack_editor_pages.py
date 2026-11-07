"""The stack file editor as served: line links, the plain textarea and the bytes posted."""

import html
import re
from typing import Any

import pytest
from httpx import AsyncClient, Response

from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.stack_support import (  # noqa: F401
    PROJECT_LABEL,
    job_id_of,
    settle_stack_job,
    stopped_stack_jobs,
)
from tests.web.environment_support import World
from tests.web.helpers import form_values, parse_tags
from tests.web.stack_file_support import PLAIN_FILE, file_fields, flat, seed_stack, stack_rows
from tests.web.test_swarm_stack_pages import CLI_FILE, namespace_service
from tests.web.test_swarm_stack_pages import base as swarm_base

JOBS = pytest.mark.usefixtures("stopped_stack_jobs")

OK, SEE_OTHER, BAD_REQUEST = 200, 303, 400
HTMX = {"HX-Request": "true"}
NAME = "blog"
SHOP = "shop"
COMPOSE_FIELD = "field-compose_file"
HEAD = "services:\n  web:\n    image: nginx:1\n"
SYNTAX_ERROR = HEAD + "    ports: [\n"
DUPLICATE_KEY = HEAD + "    command: a\n    command: b\n"
REFUSED_CONSTRUCT = HEAD + "    command: a\n    user: u\n    build: .\n"
INCLUDING = "include:\n  - inc.yaml\n" + HEAD
INCLUDED_SYNTAX_ERROR = "services: [\n"
INCLUDED_REFUSED = "services:\n  db:\n    image: nginx:1\n    build: .\n"
GOTO = re.compile(r'<a class="goto-line" href="#([\w-]+)" data-goto-line="(\d+)">')
TEXTAREA = re.compile(r"<textarea[^>]*\bname=\"compose_file\"[^>]*>\n?(.*?)</textarea>", re.DOTALL)
BIG_LINE_COUNT = 10_000
CASES = {
    "a YAML syntax error": (SYNTAX_ERROR, "5"),
    "a duplicate key": (DUPLICATE_KEY, "5"),
    "a refused construct": (REFUSED_CONSTRUCT, "6"),
}


def links(response: Response) -> list[tuple[str, str]]:
    """Return the (field id, line) of every go-to-line link in a response."""
    return GOTO.findall(response.text)


def stacks_path(world: World) -> str:
    return f"/environments/{world.remote_id}/stacks"


def editors(page: str) -> list[dict[str, str]]:
    """Return the attributes of each code editor textarea outside a script template."""
    live = re.sub(r"<template.*?</template>", "", page, flags=re.DOTALL)
    return [
        t.attrs for t in parse_tags(live) if t.name == "textarea" and "data-code-editor" in t.attrs
    ]


def labelled_ids(page: str) -> set[str]:
    return {t.attrs["for"] for t in parse_tags(page) if t.name == "label" and "for" in t.attrs}


async def checked(client: AsyncClient, path: str, text: str, **fields: str) -> dict[str, Response]:
    """Post one file to a check route as a page, then as a script would."""
    data = csrf_form(client, compose_file=text, **fields)
    return {
        "page": await client.post(path, data=data),
        "script": await client.post(path, data=data, headers=HTMX),
    }


async def test_each_refusal_of_a_compose_file_links_to_its_line_on_every_form(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    base = stacks_path(world)
    await seed_stack(world.app, world.remote_id, SHOP, HEAD)
    stack_host.add(f"{0xD1:064x}", "cron-job-1", labels={PROJECT_LABEL: "cron"}, image="nginx:1")
    routes = {
        "new": f"{base}/-/validate",
        "edit": f"{base}/{SHOP}/validate",
        "adopt": f"{base}/-/adopt/cron/validate",
    }

    answers = {
        (label, form): await checked(admin_client, path, text, name="x1")
        for label, (text, _) in CASES.items()
        for form, path in routes.items()
    }

    expected = {(label, form): line for label, (_, line) in CASES.items() for form in routes}
    assert {key: r["page"].status_code for key, r in answers.items()} == dict.fromkeys(
        answers, BAD_REQUEST
    )
    assert {key: r["script"].status_code for key, r in answers.items()} == dict.fromkeys(
        answers, BAD_REQUEST
    )
    assert {key: links(r["page"]) for key, r in answers.items()} == {
        key: [(COMPOSE_FIELD, line)] for key, line in expected.items()
    }
    assert {key: links(r["script"]) for key, r in answers.items()} == {
        key: [(COMPOSE_FIELD, line)] for key, line in expected.items()
    }
    assert {
        key: f'id="{COMPOSE_FIELD}"' in r["page"].text for key, r in answers.items()
    } == dict.fromkeys(answers, True)
    assert {key: "<html" in r["script"].text for key, r in answers.items()} == dict.fromkeys(
        answers, False
    )


async def test_a_refusal_inside_a_file_row_links_to_that_rows_content_with_the_line_in_the_file(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    base = stacks_path(world)
    refused = csrf_form(
        admin_client,
        name=NAME,
        compose_file=INCLUDING,
        **file_fields(PLAIN_FILE, ("inc.yaml", INCLUDED_REFUSED, False)),
    )
    syntax = csrf_form(
        admin_client,
        name=NAME,
        compose_file=INCLUDING,
        **file_fields(("inc.yaml", INCLUDED_SYNTAX_ERROR, False)),
    )

    construct = await admin_client.post(f"{base}/-/validate", data=refused)
    broken = await admin_client.post(f"{base}/-/new", data=syntax)

    assert construct.status_code == broken.status_code == BAD_REQUEST
    assert links(construct) == [("field-file-1-content", "4")]
    assert links(broken) == [("field-file-0-content", "2")]
    assert (
        'id="field-file-1-content"' in construct.text and 'id="field-file-0-content"' in broken.text
    )
    assert "inc.yaml: `services.db.build`" in flat(construct)
    assert await stack_rows(world, "SELECT name FROM stack") == []


async def test_a_swarm_form_links_each_refusal_to_its_line(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world, root = swarm_stacks_world, swarm_base(swarm_stacks_world)
    swarm_host.add_service(namespace_service("Cli", "Cli_web"))
    await seed_stack(world.app, world.remote_id, SHOP.capitalize(), CLI_FILE, kind="swarm")
    routes = {
        "new": f"{root}/-/validate",
        "edit": f"{root}/{SHOP.capitalize()}/validate",
        "adopt": f"{root}/-/adopt/Cli/validate",
    }

    answers = {
        (label, form): await checked(admin_client, path, text, name="Fresh")
        for label, (text, _) in CASES.items()
        for form, path in routes.items()
    }

    expected = {(label, form): line for label, (_, line) in CASES.items() for form in routes}
    assert {key: r["page"].status_code for key, r in answers.items()} == dict.fromkeys(
        answers, BAD_REQUEST
    )
    assert {key: links(r["page"]) for key, r in answers.items()} == {
        key: [(COMPOSE_FIELD, line)] for key, line in expected.items()
    }
    assert {key: links(r["script"]) for key, r in answers.items()} == {
        key: [(COMPOSE_FIELD, line)] for key, line in expected.items()
    }


def facts(page: str) -> dict[str, list[tuple[Any, ...]]]:
    """Describe what a page without script still has: editors, their labels, the row buttons."""
    live = re.sub(r"<template.*?</template>", "", page, flags=re.DOTALL)
    tags = parse_tags(live)
    labels = {t.attrs["for"] for t in tags if t.name == "label" and "for" in t.attrs}
    return {
        "editors": [
            (t.attrs["id"], t.attrs["name"], str(t.attrs["id"] in labels))
            for t in tags
            if t.name == "textarea" and "data-code-editor" in t.attrs
        ],
        "script_only": [
            ("hidden" in t.attrs,)
            for t in tags
            if t.name == "button" and "data-repeat-add" in t.attrs
        ],
        "row_buttons": [
            (t.attrs["value"],)
            for t in tags
            if t.name == "button" and t.attrs.get("name") == "rows"
        ],
        "submits": [
            (t.attrs.get("type", ""),)
            for t in tags
            if t.name == "button" and t.attrs.get("type") == "submit" and "name" not in t.attrs
        ],
    }


def editor_ids(found: dict[str, list[tuple[Any, ...]]]) -> list[str]:
    return [editor[0] for editor in found["editors"]]


async def test_every_compose_stack_form_keeps_its_plain_textarea_and_works_without_script(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    base = stacks_path(world)
    await seed_stack(world.app, world.remote_id, SHOP, HEAD, files=[PLAIN_FILE])
    stack_host.add(f"{0xD1:064x}", "cron-job-1", labels={PROJECT_LABEL: "cron"}, image="nginx:1")

    pages = {
        "new": await admin_client.get(f"{base}/-/new"),
        "edit": await admin_client.get(f"{base}/{SHOP}/edit"),
        "adopt": await admin_client.get(f"{base}/-/adopt/cron"),
        "add file": await admin_client.get(f"{base}/{SHOP}/files/-/new"),
        "edit file": await admin_client.get(f"{base}/{SHOP}/files/app.conf/edit"),
    }
    found = {name: facts(page.text) for name, page in pages.items()}
    more = await admin_client.post(
        f"{base}/-/new", data=csrf_form(admin_client, name=NAME, compose_file=HEAD, rows="file")
    )

    assert {name: p.status_code for name, p in pages.items()} == dict.fromkeys(pages, OK)
    assert {name: editor_ids(f)[0] for name, f in found.items()} == {
        "new": COMPOSE_FIELD,
        "edit": COMPOSE_FIELD,
        "adopt": COMPOSE_FIELD,
        "add file": "field-content",
        "edit file": "field-content",
    }
    assert {name: f["editors"][0][1:] for name, f in found.items()} == {
        "new": ("compose_file", "True"),
        "edit": ("compose_file", "True"),
        "adopt": ("compose_file", "True"),
        "add file": ("content", "True"),
        "edit file": ("content", "True"),
    }
    assert all(editor[2] == "True" for f in found.values() for editor in f["editors"])
    assert {name: f["row_buttons"] for name, f in found.items()} == {
        "new": [("variable",), ("file",)],
        "edit": [("variable",)],
        "adopt": [("variable",), ("file",)],
        "add file": [],
        "edit file": [],
    }
    assert all(entry == (True,) for f in found.values() for entry in f["script_only"])
    assert all(f["submits"] for f in found.values())
    assert "file-0-name" not in pages["edit"].text
    assert more.status_code == OK and "The file is valid" not in more.text
    assert len(editor_ids(facts(more.text))) > len(editor_ids(found["new"]))
    assert len(world.app.state.engine.stack_jobs) == 0


async def test_every_swarm_stack_form_keeps_its_plain_textarea(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world, root = swarm_stacks_world, swarm_base(swarm_stacks_world)
    swarm_host.add_service(namespace_service("Cli", "Cli_web"))
    await seed_stack(
        world.app, world.remote_id, "Outer", CLI_FILE, kind="swarm", files=[PLAIN_FILE]
    )

    pages = {
        "new": await admin_client.get(f"{root}/-/new"),
        "edit": await admin_client.get(f"{root}/Outer/edit"),
        "adopt": await admin_client.get(f"{root}/-/adopt/Cli"),
        "add file": await admin_client.get(f"{root}/Outer/files/-/new"),
        "edit file": await admin_client.get(f"{root}/Outer/files/app.conf/edit"),
    }
    found = {name: facts(page.text) for name, page in pages.items()}
    more = await admin_client.post(
        f"{root}/-/new",
        data=csrf_form(admin_client, name="Fresh", compose_file=CLI_FILE, rows="file"),
    )

    assert {name: p.status_code for name, p in pages.items()} == dict.fromkeys(pages, OK)
    assert {name: f["editors"][0] for name, f in found.items()} == {
        "new": (COMPOSE_FIELD, "compose_file", "True"),
        "edit": (COMPOSE_FIELD, "compose_file", "True"),
        "adopt": (COMPOSE_FIELD, "compose_file", "True"),
        "add file": ("field-content", "content", "True"),
        "edit file": ("field-content", "content", "True"),
    }
    assert {name: f["row_buttons"] for name, f in found.items()} == {
        "new": [("variable",), ("file",)],
        "edit": [("variable",)],
        "adopt": [("variable",), ("file",)],
        "add file": [],
        "edit file": [],
    }
    assert all(entry == (True,) for f in found.values() for entry in f["script_only"])
    assert all(f["submits"] for f in found.values())
    assert more.status_code == OK
    assert len(editor_ids(facts(more.text))) > len(editor_ids(found["new"]))


@JOBS
async def test_a_file_of_ten_thousand_lines_is_stored_and_shown_byte_for_byte(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    base = stacks_path(world)
    comments = "".join(f"# {number:05d}\r\n" for number in range(BIG_LINE_COUNT - 3))
    sent = HEAD.replace("\n", "\r\n") + comments

    created = await admin_client.post(
        f"{base}/-/new", data=csrf_form(admin_client, name=NAME, compose_file=sent)
    )
    await settle_stack_job(world.app, job_id_of(created))
    stored = await stack_rows(world, "SELECT compose_file FROM stack")
    editor = await admin_client.get(f"{base}/{NAME}/edit")

    shown = TEXTAREA.search(editor.text)
    assert shown is not None
    assert sent.count("\n") == BIG_LINE_COUNT
    assert stored == [{"compose_file": sent}]
    assert html.unescape(shown.group(1)) == sent
    assert form_values(editor.text)["expected_file_digest"]
