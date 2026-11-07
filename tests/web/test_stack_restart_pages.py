"""The Compose stack restart over the web: the confirmation, the job and who may run it."""

from pathlib import Path

import pytest
from httpx import AsyncClient, Response

from app.services.stack_views import containers_digest
from tests.auth_support import ADMIN_ID, csrf_form
from tests.container_support import STACK_LABEL, plant_control
from tests.docker_engine.container_engine import ContainerEngine
from tests.self_support import home_identity
from tests.stack_support import (  # noqa: F401
    SERVICE_LABEL,
    job_id_of,
    read_records,
    settle_stack_job,
    stopped_stack_jobs,
)
from tests.stream_support import read_stream
from tests.web.access_panel_support import digest_in
from tests.web.environment_support import World
from tests.web.helpers import form_values
from tests.web.stack_file_support import flat, stack_rows

pytestmark = pytest.mark.usefixtures("stopped_stack_jobs")

OK, SEE_OTHER, FORBIDDEN, CONFLICT = 200, 303, 403, 409
NAME = "blog"
MINE = "mine"
OUTAGE = "They are unavailable between the two steps."
STAYS_STOPPED = "Services that are stopped now stay stopped."
STALE = "The stack changed"
NOTHING_TO_RESTART = "No container of this stack is running, so there is nothing to restart."
UNWRITABLE = "You cannot change every container of this stack"
MEMBERS_REFUSED = "Compose acts on every container of a stack"
EXTRA = f"{0xE1:064x}"
HIDDEN = f"{0xE2:064x}"
STRAY = f"{0xE3:064x}"
THREE_SERVICES = (
    "services:\n  web:\n    image: nginx:1\n  db:\n    image: nginx:1\n  cache:\n"
    "    image: nginx:1\n"
)
RESTART_FAILURES = (
    "SELECT json_extract(detail, '$.reason') AS reason FROM activity_event "
    "WHERE action = 'stack_restarted' AND result = 'failure' ORDER BY id"
)


def stacks_path(world: World, environment_id: int | None = None) -> str:
    return f"/environments/{environment_id or world.remote_id}/stacks"


def scripted(text: str, record: Path) -> str:
    return f"{text}x-fake:\n  record: {record}\n"


async def create(
    client: AsyncClient, world: World, name: str, text: str, env: int | None = None
) -> None:
    form = csrf_form(client, name=name, compose_file=text)
    created = await client.post(f"{stacks_path(world, env)}/-/new", data=form)
    await settle_stack_job(world.app, job_id_of(created))


def project_ids(host: ContainerEngine, project: str) -> list[str]:
    return [b.id for b in host.boxes.values() if b.labels.get(STACK_LABEL) == project]


def stop_service(host: ContainerEngine, project: str, service: str) -> None:
    for box in host.boxes.values():
        if box.labels.get(STACK_LABEL) == project and box.labels.get(SERVICE_LABEL) == service:
            box.state = "exited"


def lifecycle_calls(record: Path) -> list[list[str]]:
    found = []
    for call in read_records(record):
        argv = call["argv"]
        start = argv.index("--env-file") + 2
        if argv[start] == "--profile":
            start += 2
        if argv[start] in {"stop", "start", "restart"}:
            found.append(argv[start:])
    return found


def restart_link(response: Response) -> bool:
    return f"/{NAME}/restart" in response.text


async def test_the_confirmation_names_the_running_services_and_a_run_restarts_only_them(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    base = f"{stacks_path(world)}/{NAME}"
    record = tmp_path / "calls.jsonl"
    await create(admin_client, world, NAME, scripted(THREE_SERVICES, record))
    stop_service(stack_host, NAME, "cache")
    running = await admin_client.get(base)

    confirm = await admin_client.get(f"{base}/restart")
    bound = form_values(confirm.text)["expected_containers"]
    started = await admin_client.post(f"{base}/restart", data=form_values(confirm.text))
    await settle_stack_job(world.app, job_id_of(started))
    job = await admin_client.get(started.headers["location"])
    streamed = await read_stream(world.app, admin_client, f"{started.headers['location']}/events")
    detail = await admin_client.get(base)
    events = await stack_rows(
        world,
        "SELECT result, actor_type, actor_user_id, json_extract(detail, '$.method') AS method, "
        "json_extract(detail, '$.services') AS services FROM activity_event "
        "WHERE action = 'stack_restarted'",
    )

    text = flat(confirm)
    assert restart_link(running)
    assert confirm.status_code == OK
    assert f"Restart stack {NAME} on remote-box?" in text
    assert "Services that are restarted 2 services db web" in text
    assert "Containers that are stopped and started 2 containers blog-web-1 blog-db-1" in text
    assert "blog-cache-1" not in text and "hidden from you" not in text
    assert OUTAGE in text and STAYS_STOPPED in text
    assert bound == containers_digest(project_ids(stack_host, NAME))
    assert started.status_code == SEE_OTHER and "/jobs/" in started.headers["location"]
    assert "Succeeded Restart blog" in flat(job) and "Restarted blog." in flat(job)
    assert streamed.body.startswith("event: done")
    assert lifecycle_calls(record) == [["stop", "db", "web"], ["start", "db", "web"]]
    states = {b.name: b.state for b in stack_host.boxes.values() if b.name.startswith(NAME)}
    assert states == {"blog-web-1": "running", "blog-db-1": "running", "blog-cache-1": "exited"}
    assert "Partly running" in flat(detail)
    assert events == [
        {
            "result": "success",
            "actor_type": "user",
            "actor_user_id": ADMIN_ID,
            "method": "stop_start",
            "services": "db,web",
        }
    ]


async def test_a_confirmation_that_no_longer_matches_the_containers_refuses_and_records_it(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    base = f"{stacks_path(world)}/{NAME}"
    record = tmp_path / "calls.jsonl"
    await create(admin_client, world, NAME, scripted(THREE_SERVICES, record))
    reviewed = form_values((await admin_client.get(f"{base}/restart")).text)
    stack_host.add(STRAY, "stray", labels={STACK_LABEL: NAME, SERVICE_LABEL: "ghost"})
    before = len(world.app.state.engine.stack_jobs)

    stale = await admin_client.post(f"{base}/restart", data=reviewed)
    unbound = await admin_client.post(f"{base}/restart", data=csrf_form(admin_client))

    assert [stale.status_code, unbound.status_code] == [CONFLICT, CONFLICT]
    assert STALE in flat(stale) and "Review again" in flat(stale)
    assert len(world.app.state.engine.stack_jobs) == before
    assert lifecycle_calls(record) == []
    assert await stack_rows(world, RESTART_FAILURES) == [{"reason": "changed_since_review"}] * 2


async def test_a_stack_with_nothing_running_offers_no_restart_and_refuses_a_posted_one(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    base = f"{stacks_path(world)}/{NAME}"
    record = tmp_path / "calls.jsonl"
    await create(admin_client, world, NAME, scripted(THREE_SERVICES, record))
    stopped = await admin_client.post(f"{base}/stop", data=csrf_form(admin_client))
    await settle_stack_job(world.app, job_id_of(stopped))
    bound = containers_digest(project_ids(stack_host, NAME))
    calls = lifecycle_calls(record)

    detail = await admin_client.get(base)
    confirm = await admin_client.get(f"{base}/restart")
    refused = await admin_client.post(
        f"{base}/restart", data=csrf_form(admin_client, expected_containers=bound)
    )

    assert not restart_link(detail)
    assert confirm.status_code == OK and NOTHING_TO_RESTART in flat(confirm)
    assert (
        "expected_containers" not in confirm.text and "Restart stack</button>" not in confirm.text
    )
    assert refused.status_code == CONFLICT and "Nothing is running" in flat(refused)
    assert lifecycle_calls(record) == calls
    assert await stack_rows(world, RESTART_FAILURES) == [{"reason": "not_running"}]


async def test_dockmins_own_stack_is_not_offered_a_restart_and_the_run_is_refused(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    base = f"{stacks_path(world)}/{NAME}"
    record = tmp_path / "calls.jsonl"
    await create(admin_client, world, NAME, scripted(THREE_SERVICES, record))
    reviewed = form_values((await admin_client.get(f"{base}/restart")).text)
    world.app.state.self_identity = home_identity(world.remote_id, compose_project=NAME)
    before = len(world.app.state.engine.stack_jobs)

    detail = await admin_client.get(base)
    confirm = await admin_client.get(f"{base}/restart")
    refused = await admin_client.post(f"{base}/restart", data=reviewed)

    assert detail.status_code == OK and not restart_link(detail)
    assert confirm.status_code == CONFLICT and "Protected" in flat(confirm)
    assert refused.status_code == CONFLICT and "Protected" in flat(refused)
    assert len(world.app.state.engine.stack_jobs) == before
    assert lifecycle_calls(record) == []


async def test_a_standard_user_without_write_on_every_container_is_refused_and_hidden_ones_counted(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
    tmp_path: Path,
) -> None:
    base = f"{stacks_path(world)}/{MINE}"
    record = tmp_path / "calls.jsonl"
    await create(standard_client, world, MINE, scripted(THREE_SERVICES, record))
    reviewed = form_values((await standard_client.get(f"{base}/restart")).text)
    allowed = await standard_client.get(f"{base}/restart")
    stack_host.add(EXTRA, "mine-extra-1", labels={STACK_LABEL: MINE, SERVICE_LABEL: "extra"})
    stack_host.add(HIDDEN, "mine-secret-1", labels={STACK_LABEL: MINE, SERVICE_LABEL: "secret"})
    await plant_control(world.app, world.remote_id, "container", EXTRA, public=True, level="read")
    await plant_control(world.app, world.remote_id, "container", HIDDEN, administrators_only=True)
    before = len(world.app.state.engine.stack_jobs)

    page = await standard_client.get(f"{base}/restart")
    admin_page = await admin_client.get(f"{base}/restart")
    refused = await standard_client.post(
        f"{base}/restart", data=form_values(admin_page.text) | csrf_form(standard_client)
    )
    unbound = await standard_client.post(f"{base}/restart", data=reviewed)

    assert "expected_containers" in allowed.text
    assert page.status_code == OK and UNWRITABLE in flat(page)
    assert "1 container is hidden from you and not listed." in flat(page)
    assert "expected_containers" not in page.text and "Restart stack</button>" not in page.text
    assert "mine-secret-1" not in flat(page)
    assert "1 container" not in flat(admin_page) and "expected_containers" in admin_page.text
    assert refused.status_code == FORBIDDEN and MEMBERS_REFUSED in flat(refused)
    assert unbound.status_code == CONFLICT
    assert len(world.app.state.engine.stack_jobs) == before
    assert lifecycle_calls(record) == []
    assert await stack_rows(world, RESTART_FAILURES) == [
        {"reason": "restricted"},
        {"reason": "changed_since_review"},
    ]


async def test_a_standard_user_on_a_local_socket_environment_cannot_restart(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
    tmp_path: Path,
) -> None:
    base = f"{stacks_path(world, world.local_id)}/{NAME}"
    record = tmp_path / "calls.jsonl"
    await create(admin_client, world, NAME, scripted(THREE_SERVICES, record), world.local_id)
    panel = await admin_client.get(f"{base}?tab=access")
    await admin_client.post(
        f"{base}/access",
        data=csrf_form(
            admin_client,
            visibility="public",
            access_level="write",
            expected_digest=digest_in(panel.text),
        ),
    )
    before = len(world.app.state.engine.stack_jobs)

    detail = await standard_client.get(base)
    confirm = await standard_client.get(f"{base}/restart")
    posted = await standard_client.post(f"{base}/restart", data=csrf_form(standard_client))

    assert detail.status_code == OK and not restart_link(detail)
    assert (confirm.status_code, posted.status_code) == (FORBIDDEN, FORBIDDEN)
    assert len(world.app.state.engine.stack_jobs) == before
    assert lifecycle_calls(record) == []
