"""The Automation pages for restart holds: the held list, Release, and the held counts."""

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.db.repositories.automation_rule import AutomationRule
from app.db.repositories.job_run import JobRunRepository, RunTrigger
from app.db.repositories.restart_hold import RestartHoldRepository
from app.services.activity import ActivityRecorder, SubsystemActor
from app.services.automation_rules import HOLDS_SHOWN, NOT_HELD
from app.services.restart_holds import EXHAUSTED_ACTION, HEALTH_CHECKER, RELEASED_ACTION
from tests.auth_support import FakeClock, csrf_form
from tests.container_support import CACHE, DB, JOB, WEB
from tests.docker_engine.container_engine import ContainerEngine
from tests.job_support import (
    ADMIN,
    STANDARD,
    events,
    job_of_rule,
    job_services_on_hosts,
    last_event_id,
)
from tests.web.environment_support import World

OK = 200
SEE_OTHER = 303
FORBIDDEN = 403
NOT_FOUND = 404
UNPROCESSABLE = 422
STEP = timedelta(seconds=10)
RELEASED_NOTICE = "The container is released. Its restart budget starts again."
NONE_HELD = "No container is held."
REMOVED = "d" * 64
DB_SELECTOR = json.dumps({"kind": "container", "names": ["db"]})
HELD_SECTION = re.compile(r'id="holds-heading".*?</section>', re.DOTALL)
RELEASE_ACTIONS = re.compile(r'action="([^"]*/holds/[0-9a-f]{64}/release)"')
HELD_NAMES = re.compile(r'<th scope="row" class="cell-wrap">(.*?)</th>')
TIMES = re.compile(r'<time datetime="([^"]+)"')


@pytest.fixture
def ticking(started_app: FastAPI) -> Iterator[FakeClock]:
    """Run the application on a clock that moves only when told to."""
    runtime = started_app.state.auth
    original = runtime.clock
    runtime.clock = FakeClock(datetime.now(UTC).replace(microsecond=0))
    yield runtime.clock
    runtime.clock = original


def text_of(response: Response) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", response.text)).split())


def holds_section(response: Response) -> str:
    found = HELD_SECTION.search(response.text)
    assert found, response.text[:1500]
    return found.group(0)


async def make_rule(
    app: FastAPI, world: World, clock: FakeClock, owner: Any = ADMIN, name: str = "Heal db"
) -> AutomationRule:
    async with job_services_on_hosts(app) as services:
        rule: AutomationRule = await services.rules.create(
            owner,
            subsystem=HEALTH_CHECKER,
            name=name,
            environment_id=world.remote_id,
            target_selector=DB_SELECTOR,
            parameters={},
            schedule_kind="interval",
            schedule_expression="60",
        )
    clock.advance(STEP)
    return rule


async def hold(
    app: FastAPI,
    world: World,
    clock: FakeClock,
    rule: AutomationRule,
    container_id: str,
    name: str,
) -> None:
    async with app.state.database.session() as db:
        await ActivityRecorder(db, clock).record_failure(
            SubsystemActor(HEALTH_CHECKER, rule.id, None, rule.name),
            action=EXHAUSTED_ACTION,
            target_type="container",
            target_id=container_id,
            environment_id=world.remote_id,
            reason="restart_budget_exhausted",
            detail={"name": name},
        )
    clock.advance(STEP)


async def release(client: AsyncClient, rule_id: int, container_id: str) -> Response:
    return await client.post(
        f"/automation/rules/{rule_id}/holds/{container_id}/release", data=csrf_form(client)
    )


async def released_rows(app: FastAPI, since: int) -> list[dict[str, Any]]:
    return [row for row in await events(app, since=since) if row["action"] == RELEASED_ACTION]


async def record_run(
    app: FastAPI, rule: AutomationRule, *, trigger: RunTrigger, detail: dict[str, Any]
) -> int:
    job = await job_of_rule(app, rule.id)
    async with app.state.database.session() as db:
        runs = JobRunRepository(db, app.state.auth.clock)
        run = await runs.start(job["id"], trigger=trigger, scheduled_for=None)
        await runs.finish(run.id, outcome="success", detail=detail)
    return run.id


async def test_the_rule_page_lists_each_held_container_with_its_times_and_a_release_form(
    admin_client: AsyncClient,
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
) -> None:
    rule = await make_rule(started_app, world, ticking)
    await hold(started_app, world, ticking, rule, DB, "db")
    await hold(started_app, world, ticking, rule, WEB, "web")
    async with started_app.state.database.session() as db:
        await RestartHoldRepository(db, ticking).note_unhealthy(rule.id, world.remote_id, DB)

    page = await admin_client.get(f"/automation/rules/{rule.id}")

    section = holds_section(page)
    assert page.status_code == OK
    assert RELEASE_ACTIONS.findall(section) == [
        f"/automation/rules/{rule.id}/holds/{WEB}/release",
        f"/automation/rules/{rule.id}/holds/{DB}/release",
    ]
    web_since, web_seen, db_since, db_seen = TIMES.findall(section)
    assert web_since == web_seen
    assert db_seen > db_since
    text = text_of(page)
    assert "Held containers" in text
    assert "Container Held since Last seen unhealthy" in text
    assert HELD_NAMES.findall(section) == ["web", "db"]
    assert NONE_HELD not in text


async def test_a_rule_that_holds_nothing_says_so_and_offers_no_release(
    admin_client: AsyncClient,
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
) -> None:
    rule = await make_rule(started_app, world, ticking)

    page = await admin_client.get(f"/automation/rules/{rule.id}")

    assert NONE_HELD in holds_section(page)
    assert RELEASE_ACTIONS.findall(page.text) == []


async def test_the_list_stops_at_fifty_held_containers_and_counts_the_rest(
    admin_client: AsyncClient,
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
) -> None:
    extra = 3
    for number in range(HOLDS_SHOWN + extra):
        shared_host.add(f"{number:064x}", f"held-{number}")
    rule = await make_rule(started_app, world, ticking)
    for number in range(HOLDS_SHOWN + extra):
        await hold(started_app, world, ticking, rule, f"{number:064x}", f"held-{number}")

    page = await admin_client.get(f"/automation/rules/{rule.id}")

    section = holds_section(page)
    assert len(RELEASE_ACTIONS.findall(section)) == HOLDS_SHOWN
    assert f"and {extra} more not listed here." in text_of(page)


async def test_a_hold_on_a_container_removed_from_the_host_is_not_listed_on_the_rule_page(
    admin_client: AsyncClient,
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
) -> None:
    rule = await make_rule(started_app, world, ticking)
    await hold(started_app, world, ticking, rule, REMOVED, "removed")
    await hold(started_app, world, ticking, rule, DB, "db")

    page = await admin_client.get(f"/automation/rules/{rule.id}")

    section = holds_section(page)
    assert HELD_NAMES.findall(section) == ["db"]
    assert RELEASE_ACTIONS.findall(section) == [f"/automation/rules/{rule.id}/holds/{DB}/release"]


async def test_a_standard_owner_sees_only_the_held_containers_they_may_read(
    standard_client: AsyncClient,
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
) -> None:
    rule = await make_rule(started_app, world, ticking, STANDARD)
    for container_id, name in ((DB, "db"), (WEB, "web"), (CACHE, "cache")):
        await hold(started_app, world, ticking, rule, container_id, name)

    page = await standard_client.get(f"/automation/rules/{rule.id}")

    assert RELEASE_ACTIONS.findall(holds_section(page)) == [
        f"/automation/rules/{rule.id}/holds/{DB}/release"
    ]


async def test_release_records_a_user_action_for_the_rule_and_the_container_leaves_the_list(
    admin_client: AsyncClient,
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
) -> None:
    rule = await make_rule(started_app, world, ticking)
    await hold(started_app, world, ticking, rule, DB, "db")
    mark = await last_event_id(started_app)

    response = await release(admin_client, rule.id, DB)
    landed = await admin_client.get(response.headers["location"])

    assert response.status_code == SEE_OTHER
    assert response.headers["location"].endswith(f"/automation/rules/{rule.id}")
    assert RELEASED_NOTICE in text_of(landed)
    assert NONE_HELD in holds_section(landed)
    [row] = await released_rows(started_app, mark)
    assert (row["actor_type"], row["result"], row["target_id"]) == ("user", "success", DB)
    assert json.loads(row["detail"])["rule_id"] == rule.id


async def test_the_owner_of_a_rule_releases_a_container_they_may_change(
    standard_client: AsyncClient,
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
) -> None:
    rule = await make_rule(started_app, world, ticking, STANDARD)
    await hold(started_app, world, ticking, rule, DB, "db")

    response = await release(standard_client, rule.id, DB)

    assert response.status_code == SEE_OTHER


async def test_a_container_the_owner_may_only_read_is_not_released_and_nothing_is_recorded(
    standard_client: AsyncClient,
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
) -> None:
    rule = await make_rule(started_app, world, ticking, STANDARD)
    await hold(started_app, world, ticking, rule, JOB, "job")
    mark = await last_event_id(started_app)

    response = await release(standard_client, rule.id, JOB)
    page = await standard_client.get(f"/automation/rules/{rule.id}")

    assert response.status_code == FORBIDDEN
    assert await events(started_app, since=mark) == []
    assert RELEASE_ACTIONS.findall(holds_section(page)) == [
        f"/automation/rules/{rule.id}/holds/{JOB}/release"
    ]


async def test_a_user_who_does_not_own_the_rule_cannot_release_its_holds(
    standard_client: AsyncClient,
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
) -> None:
    rule = await make_rule(started_app, world, ticking, ADMIN)
    await hold(started_app, world, ticking, rule, DB, "db")
    mark = await last_event_id(started_app)

    response = await release(standard_client, rule.id, DB)

    assert response.status_code == NOT_FOUND
    assert await events(started_app, since=mark) == []


async def test_releasing_a_container_the_rule_does_not_hold_is_refused_and_leaves_other_holds(
    admin_client: AsyncClient,
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
) -> None:
    holder = await make_rule(started_app, world, ticking, name="Holder")
    bystander = await make_rule(started_app, world, ticking, name="Bystander")
    await hold(started_app, world, ticking, holder, DB, "db")
    mark = await last_event_id(started_app)

    response = await release(admin_client, bystander.id, DB)
    holder_page = await admin_client.get(f"/automation/rules/{holder.id}")

    assert response.status_code == UNPROCESSABLE
    assert NOT_HELD in text_of(response)
    [row] = await released_rows(started_app, mark)
    assert row["result"] == "failure"
    assert RELEASE_ACTIONS.findall(holds_section(holder_page)) == [
        f"/automation/rules/{holder.id}/holds/{DB}/release"
    ]


async def test_release_needs_the_forms_token(
    admin_client: AsyncClient,
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
) -> None:
    rule = await make_rule(started_app, world, ticking)
    await hold(started_app, world, ticking, rule, DB, "db")
    mark = await last_event_id(started_app)

    response = await admin_client.post(f"/automation/rules/{rule.id}/holds/{DB}/release")

    assert response.status_code == FORBIDDEN
    assert await events(started_app, since=mark) == []


async def test_the_last_real_runs_held_count_shows_on_the_list_the_rule_and_the_job(
    admin_client: AsyncClient,
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
) -> None:
    rule = await make_rule(started_app, world, ticking)
    run_id = await record_run(
        started_app, rule, trigger="schedule", detail={"held": 2, "released": 1}
    )
    ticking.advance(STEP)
    await record_run(started_app, rule, trigger="dry_run", detail={"held": 9})
    job = await job_of_rule(started_app, rule.id)

    listing = await admin_client.get("/automation")
    detail = await admin_client.get(f"/automation/rules/{rule.id}")
    job_page = await admin_client.get(f"/jobs/{job['id']}")
    run_page = await admin_client.get(f"/automation/rules/{rule.id}/runs/{run_id}")

    assert "Held 2" in text_of(listing)
    assert "Held 9" not in text_of(listing)
    assert "Held 2 Released 1" in text_of(detail)
    assert "Held 2 Released 1" in text_of(job_page)
    assert re.search(r'<dt class="mono">held</dt>\s*<dd>2</dd>', run_page.text)
    assert re.search(r'<dt class="mono">released</dt>\s*<dd>1</dd>', run_page.text)


async def test_a_run_that_held_nothing_shows_no_held_badge(
    admin_client: AsyncClient,
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
) -> None:
    rule = await make_rule(started_app, world, ticking)
    await record_run(started_app, rule, trigger="schedule", detail={"restarted": 1})

    listing = await admin_client.get("/automation")
    detail = await admin_client.get(f"/automation/rules/{rule.id}")

    assert "Held" not in text_of(listing).replace("Held containers", "")
    assert "Held" not in text_of(detail).replace("Held containers", "")
