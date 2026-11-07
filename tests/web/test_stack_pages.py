"""The stack pages over the web: what each role sees, the secret rule, and the failure surface."""

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient, Response

from app.compose_cli import cli as compose_cli
from app.compose_cli.parsers import parse_version_text
from app.compose_cli.process import run
from app.db.repositories.clocked import utc_now
from app.services.secret_mask import MASK
from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.fake_engine import Refuse
from tests.stack_support import (  # noqa: F401
    SECRET_SENTINEL,
    job_id_of,
    settle_stack_job,
    stopped_stack_jobs,
    variable_fields,
)
from tests.stream_support import read_stream
from tests.web.access_panel_support import digest_in
from tests.web.environment_support import World, rows
from tests.web.flash_support import location
from tests.web.helpers import form_values, inline_code_in, inputs_named
from tests.web.registry_support import visible_text

pytestmark = pytest.mark.usefixtures("stopped_stack_jobs")

OK, SEE_OTHER, BAD_REQUEST, UNAVAILABLE = 200, 303, 400, 503
REPLACEMENT_SENTINEL = "R3PLACEMENT-SENTINEL"
NAME = "blog"
BROKEN = "broken"
STDERR_SENTINEL = "SECRET_SENTINEL_VALUE"
UNMANAGED = "shop"
SECRET_NAME = "TOKEN"
PLAIN_NAME = "PLAIN"
PLAIN_VALUE = "plain-visible-value"
CHANGED_PLAIN_VALUE = "changed-visible-value"
HTMX = {"HX-Request": "true"}
AFTER_DEPLOY = timedelta(minutes=1)
NO_STORE = "no-store"
COMPOSE_FILE = """\
services:
  web:
    image: nginx:1
    environment:
      - TOKEN=${TOKEN}
      - PLAIN=${PLAIN}
  db:
    image: nginx:1
    volumes:
      - data:/data
volumes:
  data: {}
"""


def stacks_path(world: World, environment_id: int | None = None) -> str:
    return f"/environments/{environment_id or world.remote_id}/stacks"


def compose_file(*, record: Path | None = None, up: str = "ok") -> str:
    script = f"x-fake:\n  up: {up}\n"
    if record is not None:
        script += f"  record: {record}\n"
    return COMPOSE_FILE + script


def text_of(response: Response) -> str:
    return " ".join(visible_text(response.text).split())


def leaked(secret: str, *responses: Response) -> list[str]:
    return [str(r.url.path) for r in responses if secret in r.text]


def uncached(*responses: Response) -> list[str]:
    return [str(r.url.path) for r in responses if r.headers.get("cache-control") != NO_STORE]


def recorded_calls(record: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]


async def create_blog(client: AsyncClient, world: World, *, file: str) -> str:
    """Create the stack as the form does: by upload, with a secret and a plain variable."""
    form = csrf_form(
        client,
        name=NAME,
        **variable_fields((SECRET_NAME, SECRET_SENTINEL, True), (PLAIN_NAME, PLAIN_VALUE, False)),
    )
    response = await client.post(
        f"{stacks_path(world)}/-/new",
        data=form,
        files={"compose_upload": ("compose.yaml", file.encode())},
    )
    job_id = job_id_of(response)
    await settle_stack_job(world.app, job_id)
    return job_id


class Visited:
    """A client that remembers every response it was given."""

    def __init__(self, client: AsyncClient) -> None:
        self.client = client
        self.responses: list[Response] = []

    async def get(self, path: str, **kwargs: Any) -> Response:
        response = await self.client.get(path, **kwargs)
        self.responses.append(response)
        return response

    async def post(self, path: str, **kwargs: Any) -> Response:
        response = await self.client.post(path, **kwargs)
        self.responses.append(response)
        return response


def row_named(values: dict[str, str], name: str) -> str:
    """Return the number of the variable row that holds the variable `name`."""
    (found,) = [key for key, value in values.items() if value == name and key.endswith("-name")]
    return found.split("-")[1]


async def test_an_administrator_creates_a_stack_and_no_page_shows_its_secret(
    admin_client: AsyncClient, world: World, stack_host: object, tmp_path: Path
) -> None:
    base = stacks_path(world)
    record = tmp_path / "calls.jsonl"
    seen = Visited(admin_client)

    job_id = await create_blog(admin_client, world, file=compose_file(record=record))
    job = await seen.get(f"{base}/-/jobs/{job_id}")
    listing = await seen.get(base)
    fragment = await seen.get(base, headers=HTMX)
    overview = await seen.get(f"{base}/{NAME}")
    file_tab = await seen.get(f"{base}/{NAME}", params={"tab": "file"})
    variables = await seen.get(f"{base}/{NAME}", params={"tab": "variables"})
    access = await seen.get(f"{base}/{NAME}", params={"tab": "access"})
    tab_fragment = await seen.get(f"{base}/{NAME}", params={"tab": "variables"}, headers=HTMX)
    boosted = await seen.get(f"{base}/{NAME}", headers=HTMX | {"HX-Boosted": "true"})
    restored = await seen.get(base, headers=HTMX | {"HX-History-Restore-Request": "true"})
    editor = await seen.get(f"{base}/{NAME}/edit")
    streamed = await read_stream(world.app, admin_client, f"{base}/-/jobs/{job_id}/events")

    assert text_of(job).count("Succeeded") == 1
    assert "Container blog-web-1 Done Started" in text_of(job)
    assert "Service Containers Running db 1 1 web 1 1" in text_of(job)
    assert f'href="{base}/{NAME}"' in job.text
    assert 'id="stack-blog"' in listing.text and "Running" in text_of(listing)
    assert "Compose projects not managed here" in listing.text and UNMANAGED in listing.text
    assert "<html" not in fragment.text and 'id="results-summary"' in fragment.text
    assert "Created here" in text_of(overview)
    assert "blog-web-1 Running" in text_of(overview) and "blog-db-1 Running" in text_of(overview)
    assert "image: nginx:1" in file_tab.text and "${TOKEN}" in file_tab.text
    assert f"{PLAIN_NAME} {PLAIN_VALUE}" in text_of(variables)
    assert f"{SECRET_NAME} Secret {MASK} Reveal {SECRET_NAME}" in text_of(variables)
    assert "Access" in text_of(access) and "<html" not in tab_fragment.text
    assert "<html" in boosted.text and "<html" in restored.text
    assert streamed.body.startswith("event: done")
    assert leaked(SECRET_SENTINEL, *seen.responses) == []
    assert SECRET_SENTINEL not in streamed.body
    assert uncached(*seen.responses) == []
    assert [r for r in seen.responses if inline_code_in(r.text)] == []
    assert editor.status_code == OK


async def test_the_editor_shows_a_stored_secret_as_set_and_keeps_it_when_left_empty(
    admin_client: AsyncClient, world: World, stack_host: object, tmp_path: Path
) -> None:
    base = stacks_path(world)
    record = tmp_path / "calls.jsonl"
    file = compose_file(record=record)
    seen = Visited(admin_client)
    await create_blog(admin_client, world, file=file)
    world.app.state.auth.clock = lambda: utc_now() + AFTER_DEPLOY

    editor = await seen.get(f"{base}/{NAME}/edit")
    fields = form_values(editor.text)
    secret_row, plain_row = row_named(fields, SECRET_NAME), row_named(fields, PLAIN_NAME)
    stored = await rows(
        world, "SELECT name, value, value_ciphertext FROM stack_variable ORDER BY name"
    )
    edited = fields | {
        "compose_file": file,
        f"variable-{plain_row}-value": CHANGED_PLAIN_VALUE,
        "next": "save",
    }
    replaced = edited | {f"variable-{secret_row}-value": REPLACEMENT_SENTINEL}
    refused = await seen.post(
        f"{base}/{NAME}/edit", data=replaced | {"compose_file": "services: [\n"}
    )
    checked = await seen.post(f"{base}/{NAME}/validate", data=replaced)
    scripted = await seen.post(f"{base}/{NAME}/validate", data=replaced, headers=HTMX)
    saved = await seen.post(f"{base}/{NAME}/edit", data=edited)
    after_save = await seen.get(location(saved))
    variables = await seen.get(f"{base}/{NAME}", params={"tab": "variables"})
    review = await seen.get(f"{base}/{NAME}/redeploy")
    redeploy = await seen.post(
        f"{base}/{NAME}/redeploy", data=form_values(review.text) | csrf_form(admin_client)
    )
    redeploy_id = job_id_of(redeploy)
    await settle_stack_job(world.app, redeploy_id)
    job = await seen.get(f"{base}/-/jobs/{redeploy_id}")
    final = await seen.get(f"{base}/{NAME}")
    cleared = await seen.get(f"{base}/{NAME}/edit")
    await seen.get(f"{base}/{NAME}/remove")
    await seen.get(f"{base}/{NAME}/remove", params={"volumes": "1"})
    await seen.get(f"{base}/{NAME}/forget")

    assert "Set. Leave empty to keep it." in editor.text
    assert fields[f"variable-{secret_row}-value"] == ""
    assert (fields[f"variable-{secret_row}-secret"], fields[f"variable-{secret_row}-stored"]) == (
        "on",
        "1",
    )
    assert fields[f"variable-{plain_row}-value"] == PLAIN_VALUE
    assert len(inputs_named(editor.text, f"variable-{secret_row}-remove")) == 1
    assert [(r["name"], r["value"]) for r in stored] == [
        (PLAIN_NAME, PLAIN_VALUE),
        (SECRET_NAME, None),
    ]
    assert SECRET_SENTINEL not in str(stored[1]["value_ciphertext"])
    assert stored[0]["value_ciphertext"] is None and stored[1]["value_ciphertext"]
    assert saved.status_code == SEE_OTHER and location(saved) == f"{base}/{NAME}"
    assert "Changes saved. Redeploy the stack to apply them." in text_of(after_save)
    assert "Changed since the last deploy" in text_of(after_save)
    assert f"{PLAIN_NAME} {CHANGED_PLAIN_VALUE}" in text_of(variables)
    assert f"{SECRET_NAME} Secret {MASK} Reveal {SECRET_NAME}" in text_of(variables)
    deployed = [call for call in recorded_calls(record) if call["cmd"] == "up"][-1]["env"]
    assert (deployed[SECRET_NAME], deployed[PLAIN_NAME]) == (SECRET_SENTINEL, CHANGED_PLAIN_VALUE)
    assert "Succeeded" in text_of(job)
    assert "Changed since the last deploy" not in text_of(final)
    assert "Set. Leave empty to keep it." in cleared.text
    assert refused.status_code == BAD_REQUEST and "not valid YAML" in text_of(refused)
    assert "Set. Leave empty to keep it." in refused.text
    assert form_values(refused.text)[f"variable-{secret_row}-value"] == ""
    assert checked.status_code == OK and "The file is valid" in text_of(checked)
    assert scripted.status_code == OK and "<html" not in scripted.text
    assert leaked(SECRET_SENTINEL, *seen.responses) == []
    assert leaked(REPLACEMENT_SENTINEL, *seen.responses) == []
    assert uncached(*seen.responses) == []


async def test_a_failing_deploy_shows_scrubbed_output_and_the_progress_made(
    admin_client: AsyncClient, world: World, stack_host: object
) -> None:
    base = stacks_path(world)
    seen = Visited(admin_client)
    form = csrf_form(
        admin_client, name=BROKEN, **variable_fields((SECRET_NAME, STDERR_SENTINEL, True))
    )
    created = await admin_client.post(
        f"{base}/-/new",
        data=form,
        files={"compose_upload": ("compose.yaml", compose_file(up="fail").encode())},
    )
    job_id = job_id_of(created)
    await settle_stack_job(world.app, job_id)

    job = await seen.get(f"{base}/-/jobs/{job_id}")
    streamed = await read_stream(world.app, admin_client, f"{base}/-/jobs/{job_id}/events")
    detail = await seen.get(f"{base}/{BROKEN}")
    listing = await seen.get(base)
    review = await seen.get(f"{base}/{BROKEN}/redeploy")
    editor = await seen.get(f"{base}/{BROKEN}/edit")

    assert job.status_code == OK
    assert "Failed Create and deploy broken" in text_of(job)
    assert "Docker Compose exited with status 3." in text_of(job)
    assert "Container broken-web-1 Working Starting" in text_of(job)
    assert '<details class="stderr">' in job.text and "<summary>Docker Compose output" in job.text
    assert "token=[REDACTED] in &lt;data&gt;/stacks/" in job.text
    assert "port is already allocated" in job.text
    assert streamed.body.startswith("event: done")
    assert "broken-web-1" in streamed.body and "[REDACTED]" in streamed.body
    assert "Error" in text_of(detail) and "Deploy failed" in text_of(detail)
    assert "Deploy failed" in text_of(listing)
    assert leaked(STDERR_SENTINEL, *seen.responses) == []
    assert STDERR_SENTINEL not in streamed.body and "hunter2secretvalue" not in streamed.body
    assert leaked("hunter2secretvalue", *seen.responses) == []
    assert str(world.app.state.settings.data_dir) not in job.text
    assert review.status_code == OK and editor.status_code == OK


MINE = "mine"
THEIRS = "theirs"
ABSENT = "nothere"
REFERENCE = re.compile(r"Reference [0-9a-f]{32}")
FORBIDDEN, NOT_FOUND = 403, 404
STACK_GETS = ("", "/edit", "/redeploy", "/remove", "/forget")
STACK_POSTS = ("/start", "/stop", "/edit", "/validate", "/redeploy", "/remove", "/forget")
ADMIN_ONLY_PATHS = ("/-/adopt/shop", "/mine/forget", "/mine?tab=access")
ADMIN_ONLY_POSTS = (
    "/-/adopt/shop",
    "/-/adopt/shop/validate",
    "/mine/forget",
    "/mine/access",
    "/mine/access/delete",
)


def shape(response: Response) -> tuple[int, str]:
    """Return the status and the visible text of an answer, without its request reference."""
    return response.status_code, REFERENCE.sub("", text_of(response))


async def shapes_of_gets(client: AsyncClient, base: str, name: str) -> dict[str, tuple[int, str]]:
    return {path: shape(await client.get(f"{base}/{name}{path}")) for path in STACK_GETS}


async def shapes_of_posts(client: AsyncClient, base: str, name: str) -> dict[str, tuple[int, str]]:
    form = csrf_form(client, compose_file=COMPOSE_FILE)
    return {
        path: shape(await client.post(f"{base}/{name}{path}", data=form)) for path in STACK_POSTS
    }


async def statuses(client: AsyncClient, base: str, paths: tuple[str, ...]) -> dict[str, int]:
    return {path: (await client.get(f"{base}{path}")).status_code for path in paths}


async def post_statuses(client: AsyncClient, base: str, paths: tuple[str, ...]) -> dict[str, int]:
    form = csrf_form(client, name=MINE, compose_file=COMPOSE_FILE)
    return {path: (await client.post(f"{base}{path}", data=form)).status_code for path in paths}


async def create_stack(client: AsyncClient, world: World, name: str, environment_id: int) -> None:
    form = csrf_form(client, name=name, compose_file=COMPOSE_FILE)
    created = await client.post(f"{stacks_path(world, environment_id)}/-/new", data=form)
    await settle_stack_job(world.app, job_id_of(created))


async def test_a_standard_user_gets_the_same_answer_for_a_stack_they_cannot_see_as_for_none(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World, stack_host: object
) -> None:
    base = stacks_path(world)
    await create_stack(admin_client, world, THEIRS, world.remote_id)
    await create_stack(standard_client, world, MINE, world.remote_id)
    admin_job = job_id_of(
        await admin_client.post(f"{base}/{THEIRS}/stop", data=csrf_form(admin_client))
    )
    await settle_stack_job(world.app, admin_job)

    listing = await standard_client.get(base)
    own = await standard_client.get(f"{base}/{MINE}")
    hidden_gets = await shapes_of_gets(standard_client, base, THEIRS)
    absent_gets = await shapes_of_gets(standard_client, base, ABSENT)
    hidden_posts = await shapes_of_posts(standard_client, base, THEIRS)
    absent_posts = await shapes_of_posts(standard_client, base, ABSENT)
    foreign_job = await standard_client.get(f"{base}/-/jobs/{admin_job}")
    unknown_job = await standard_client.get(f"{base}/-/jobs/{'A' * 22}")
    forbidden_gets = await statuses(standard_client, base, ADMIN_ONLY_PATHS)
    forbidden_posts = await post_statuses(standard_client, base, ADMIN_ONLY_POSTS)

    assert MINE in text_of(listing) and THEIRS not in text_of(listing)
    assert "Compose projects not managed here" not in listing.text
    assert "Create stack" in text_of(listing)
    assert "Edit Redeploy" in text_of(own) and "Forget" not in text_of(own)
    assert "tab=access" not in own.text
    assert {status for status, _ in hidden_gets.values()} == {NOT_FOUND}
    assert hidden_gets == absent_gets
    assert {status for status, _ in hidden_posts.values()} == {NOT_FOUND}
    assert hidden_posts == absent_posts
    assert shape(foreign_job) == shape(unknown_job) and foreign_job.status_code == NOT_FOUND
    assert set(forbidden_gets.values()) == {FORBIDDEN}
    assert set(forbidden_posts.values()) == {FORBIDDEN}


LOCAL_STACK = "localstack"
LOCAL_MUTATIONS = ("/-/new", "/-/validate", "/localstack/start", "/localstack/stop")
LOCAL_FORMS = ("/-/new", "/localstack/edit", "/localstack/redeploy", "/localstack/remove")


async def test_a_standard_user_may_read_a_shared_stack_on_a_local_socket_but_never_change_it(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World, stack_host: object
) -> None:
    base = stacks_path(world, world.local_id)
    await create_stack(admin_client, world, LOCAL_STACK, world.local_id)
    panel = await admin_client.get(f"{base}/{LOCAL_STACK}?tab=access")
    shared = await admin_client.post(
        f"{base}/{LOCAL_STACK}/access",
        data=csrf_form(
            admin_client,
            visibility="public",
            access_level="write",
            expected_digest=digest_in(panel.text),
        ),
    )
    started = len(world.app.state.engine.stack_jobs)

    listing = await standard_client.get(base)
    detail = await standard_client.get(f"{base}/{LOCAL_STACK}")
    forms = await statuses(standard_client, base, LOCAL_FORMS)
    mutations = await post_statuses(standard_client, base, LOCAL_MUTATIONS)
    confirmations = await post_statuses(
        standard_client,
        base,
        ("/localstack/edit", "/localstack/redeploy", "/localstack/remove", "/localstack/forget"),
    )

    assert shared.status_code == SEE_OTHER
    assert LOCAL_STACK in text_of(listing) and "Create stack" not in text_of(listing)
    assert detail.status_code == OK
    assert "Redeploy" not in text_of(detail) and "/edit" not in detail.text
    assert set(forms.values()) == {FORBIDDEN}
    assert set(mutations.values()) == {FORBIDDEN}
    assert set(confirmations.values()) == {FORBIDDEN}
    assert len(world.app.state.engine.stack_jobs) == started


OLD_COMPOSE = "v2.20.0"
NOT_READY = "Docker Compose is not ready"
READ_PAGES = (
    "",
    "/-/new",
    f"/{NAME}",
    f"/{NAME}?tab=file",
    f"/{NAME}?tab=variables",
    f"/{NAME}?tab=access",
    f"/{NAME}/edit",
    f"/{NAME}/redeploy",
    f"/{NAME}/remove",
    f"/{NAME}/forget",
    f"/-/adopt/{UNMANAGED}",
)
BANNER_PAGES = tuple(path for path in READ_PAGES if not path.endswith("/forget"))
NO_COMPOSE_PAGES = tuple(path for path in READ_PAGES if path != f"/{NAME}/redeploy")


@pytest.fixture
def compose_runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Record the arguments of every Compose process the application starts."""
    started: list[tuple[str, ...]] = []
    real = run

    async def counting(binary: Any, invocation: Any, **kwargs: Any) -> Any:
        started.append(tuple(invocation.argv))
        return await real(binary, invocation, **kwargs)

    monkeypatch.setattr(compose_cli, "run", counting)
    return started


async def test_reading_any_stack_page_starts_no_compose_process(
    admin_client: AsyncClient,
    world: World,
    stack_host: object,
    compose_runs: list[tuple[str, ...]],
) -> None:
    base = stacks_path(world)
    job_id = await create_blog(admin_client, world, file=compose_file())
    compose_runs.clear()

    pages = await statuses(admin_client, base, NO_COMPOSE_PAGES)
    other = await statuses(admin_client, "", ("/", f"/environments/{world.remote_id}", base))
    job = await admin_client.get(f"{base}/-/jobs/{job_id}")
    streamed = await read_stream(world.app, admin_client, f"{base}/-/jobs/{job_id}/events")
    reads = len(compose_runs)
    volumes = await admin_client.get(f"{base}/{NAME}/remove", params={"volumes": "1"})
    redeploy = await admin_client.get(f"{base}/{NAME}/redeploy")
    reviewed = list(compose_runs)
    await admin_client.post(
        f"{base}/-/validate", data=csrf_form(admin_client, name="x1", compose_file=COMPOSE_FILE)
    )

    assert set(pages.values()) == {OK} and set(other.values()) == {OK}
    assert job.status_code == OK and streamed.status == OK
    assert reads == 0
    assert volumes.status_code == OK and redeploy.status_code == OK
    assert [run[-3:] for run in reviewed] == [("config", "--format", "json")] * 2
    assert len(compose_runs) > len(reviewed)


async def test_with_compose_below_the_floor_pages_explain_it_and_nothing_runs(
    admin_client: AsyncClient,
    world: World,
    stack_host: object,
    compose_runs: list[tuple[str, ...]],
) -> None:
    base = stacks_path(world)
    await create_blog(admin_client, world, file=compose_file())
    review = await admin_client.get(f"{base}/{NAME}/redeploy")
    removal = await admin_client.get(f"{base}/{NAME}/remove")
    world.app.state.engine.compose.version = parse_version_text(OLD_COMPOSE)
    compose_runs.clear()

    pages = {path: await admin_client.get(f"{base}{path}") for path in BANNER_PAGES}
    refused = {
        path: await admin_client.post(f"{base}{path}", data=data)
        for path, data in {
            "/-/new": csrf_form(admin_client, name="x1", compose_file=COMPOSE_FILE),
            "/-/validate": csrf_form(admin_client, name="x1", compose_file=COMPOSE_FILE),
            f"/{NAME}/edit": csrf_form(admin_client, compose_file=COMPOSE_FILE),
            f"/{NAME}/redeploy": form_values(review.text),
        }.items()
    }
    fragment = await admin_client.post(
        f"{base}/-/validate",
        data=csrf_form(admin_client, name="x1", compose_file=COMPOSE_FILE),
        headers=HTMX,
    )
    ended = await admin_client.post(f"{base}/{NAME}/remove", data=form_values(removal.text))
    await settle_stack_job(world.app, job_id_of(ended))
    job = await admin_client.get(ended.headers["location"])
    after = await admin_client.get(f"{base}/{NAME}")

    assert {path: r.status_code for path, r in pages.items()} == dict.fromkeys(BANNER_PAGES, OK)
    assert {path: NOT_READY in text_of(r) for path, r in pages.items()} == dict.fromkeys(
        BANNER_PAGES, True
    )
    assert "Installed v2.20.0 Required v2.32.0 or later" in text_of(pages[""])
    assert "disabled>Create and deploy" in pages["/-/new"].text
    assert "disabled>Redeploy stack" in pages[f"/{NAME}/redeploy"].text
    assert {path: r.status_code for path, r in refused.items()} == dict.fromkeys(refused, 503)
    assert "Installed v2.20.0 Required v2.32.0 or later" in text_of(refused["/-/new"])
    assert fragment.status_code == 503 and "v2.20.0" in text_of(fragment)
    assert "Failed Remove blog" in text_of(job) and OLD_COMPOSE in text_of(job)
    assert after.status_code == OK
    assert compose_runs == []


async def test_an_unreachable_host_answers_a_script_stack_confirmation_with_the_fragment(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    base = stacks_path(world)
    await create_blog(admin_client, world, file=compose_file())
    stack_host.route("GET", "/containers/json", Refuse())

    removal = await admin_client.get(f"{base}/{NAME}/remove", headers=HTMX)
    redeploy = await admin_client.get(f"{base}/{NAME}/redeploy", headers=HTMX)

    assert [r.status_code for r in (removal, redeploy)] == [UNAVAILABLE] * 2
    assert not any("<html" in r.text or "Traceback" in r.text for r in (removal, redeploy))
