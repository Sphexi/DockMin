"""The JSON routes for jobs: bearer tokens, administrators only, read-only, paged and bounded."""

import base64
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from tests.auth_support import ADMIN_ID, ADMIN_SESSION_TOKEN, STANDARD_SESSION_TOKEN, Browsers
from tests.conftest import AttachApp
from tests.job_support import JOB_SUBSYSTEMS, REFRESHER, UPDATER, refresher_job_id
from tests.web.environment_support import bearer, issue_token

BASE = "/api/v1/jobs"
UNKNOWN = 99_999
BEYOND_INT64 = 2**63
RUN_MESSAGE = "unreachable"
HISTORY = 3


@pytest.fixture
def app(settings: Settings, attach_app: AttachApp) -> FastAPI:
    return attach_app(settings, JOB_SUBSYSTEMS)


@pytest.fixture
async def job_id(started_app: FastAPI) -> int:
    """The refresher's job, given a short history so the run routes have something to page."""
    job = await refresher_job_id(started_app)
    async with started_app.state.database.session() as db:
        runs = JobRunRepository(db, started_app.state.auth.clock)
        for _ in range(HISTORY):
            run = await runs.start(job, trigger="manual", scheduled_for=None)
            await runs.finish(
                run.id,
                outcome="failure",
                reason="host_down",
                message=RUN_MESSAGE,
                detail={"checked": 1, "flag": True},
            )
    return job


@pytest.fixture
async def rule_job_id(started_app: FastAPI) -> int:
    """An automation's job, which has a rule where the refresher's job has none."""
    clock = started_app.state.auth.clock
    async with started_app.state.database.session() as db, db.transaction():
        rule = await AutomationRuleRepository(db, clock).insert(
            name="Nightly",
            subsystem=UPDATER,
            environment_id=None,
            target_selector="",
            parameters="{}",
            is_enabled=False,
            created_by_user_id=ADMIN_ID,
        )
        job = await ScheduledJobRepository(db, clock).insert(
            subsystem=UPDATER,
            rule_id=rule.id,
            schedule_kind="interval",
            schedule_expression="600",
            time_zone="UTC",
            overrun_policy="skip",
            catch_up=False,
            failure_threshold=None,
            next_run_at=None,
        )
    return job.id


@pytest.fixture
async def admin_headers(browsers: Browsers) -> dict[str, str]:
    return bearer(await issue_token(browsers.open(session_token=ADMIN_SESSION_TOKEN)))


@pytest.fixture
async def standard_headers(browsers: Browsers) -> dict[str, str]:
    return bearer(await issue_token(browsers.open(session_token=STANDARD_SESSION_TOKEN)))


@pytest.fixture
def api(browsers: Browsers) -> AsyncClient:
    """A client with no cookie, so only the bearer header can authenticate it."""
    return browsers.open()


def paths(job: int, run: int = 1) -> list[str]:
    return [BASE, f"{BASE}/{job}", f"{BASE}/{job}/runs", f"{BASE}/-/runs/{run}"]


async def test_a_request_without_a_valid_token_is_unauthenticated_even_with_a_session_cookie(
    api: AsyncClient, browsers: Browsers, job_id: int
) -> None:
    admin_cookie = browsers.open(session_token=ADMIN_SESSION_TOKEN)

    anonymous = {p: (await api.get(p)).status_code for p in paths(job_id)}
    cookie_only = {p: (await admin_cookie.get(p)).status_code for p in paths(job_id)}
    bogus = {p: (await api.get(p, headers=bearer("bogus"))).status_code for p in paths(job_id)}

    assert set(anonymous.values()) == {401}
    assert set(cookie_only.values()) == {401}
    assert set(bogus.values()) == {401}


async def test_a_standard_users_token_is_refused_on_every_route(
    api: AsyncClient, standard_headers: dict[str, str], job_id: int
) -> None:
    refused = {p: await api.get(p, headers=standard_headers) for p in paths(job_id)}

    assert {p: r.status_code for p, r in refused.items()} == dict.fromkeys(refused, 403)
    assert {r.json()["error"]["status"] for r in refused.values()} == {403}


async def test_the_listing_describes_each_job_with_its_schedule_state_and_latest_run(
    api: AsyncClient, admin_headers: dict[str, str], job_id: int
) -> None:
    response = await api.get(BASE, headers=admin_headers)

    [job] = [j for j in response.json()["items"] if j["id"] == job_id]
    assert response.status_code == 200
    assert job["subsystem"] == REFRESHER
    assert job["rule"] is None and job["environment_id"] is None
    assert job["schedule"]["kind"] == "interval"
    assert job["schedule"]["description"].startswith("Every")
    assert (job["catch_up"], job["overrun_policy"], job["failure_threshold"]) == (
        True,
        "skip",
        None,
    )
    assert (job["supports_dry_run"], job["available"], job["subsystem_enabled"]) == (
        False,
        True,
        True,
    )
    assert job["last_run"]["reason"] == "host_down" and job["last_run"]["message"] == RUN_MESSAGE
    assert job["last_run"]["detail"] == {"checked": 1, "flag": True}


async def test_the_listing_filters_by_subsystem_and_refuses_an_unknown_one(
    api: AsyncClient, admin_headers: dict[str, str], job_id: int
) -> None:
    matching = await api.get(f"{BASE}?subsystem={REFRESHER}", headers=admin_headers)
    other = await api.get(f"{BASE}?subsystem=backup-syncer", headers=admin_headers)
    unknown = await api.get(f"{BASE}?subsystem=nope", headers=admin_headers)

    assert [j["id"] for j in matching.json()["items"]] == [job_id]
    assert other.json()["items"] == []
    assert unknown.status_code == 422


async def test_one_job_is_returned_by_id(
    api: AsyncClient, admin_headers: dict[str, str], job_id: int
) -> None:
    response = await api.get(f"{BASE}/{job_id}", headers=admin_headers)

    assert response.status_code == 200 and response.json()["id"] == job_id


async def test_a_jobs_runs_come_newest_first_in_pages(
    api: AsyncClient, admin_headers: dict[str, str], job_id: int
) -> None:
    first = (await api.get(f"{BASE}/{job_id}/runs?limit=2", headers=admin_headers)).json()
    second = (
        await api.get(
            f"{BASE}/{job_id}/runs",
            params={"limit": 2, "cursor": first["next_cursor"]},
            headers=admin_headers,
        )
    ).json()

    ids = [run["id"] for run in [*first["items"], *second["items"]]]
    assert len(first["items"]) == 2 and len(second["items"]) >= 1
    assert ids == sorted(ids, reverse=True)
    assert first["items"][0]["job_id"] == job_id and first["items"][0]["trigger"] == "manual"


async def test_one_run_is_returned_with_the_job_it_belongs_to(
    api: AsyncClient, admin_headers: dict[str, str], job_id: int
) -> None:
    runs = (await api.get(f"{BASE}/{job_id}/runs", headers=admin_headers)).json()["items"]

    response = await api.get(f"{BASE}/-/runs/{runs[0]['id']}", headers=admin_headers)

    body: dict[str, Any] = response.json()
    assert response.status_code == 200
    assert body["id"] == runs[0]["id"] and body["job"]["id"] == job_id


async def test_the_routes_cannot_change_anything(
    api: AsyncClient, admin_headers: dict[str, str], job_id: int
) -> None:
    attempts = [(method, p) for method in ("post", "put", "patch", "delete") for p in paths(job_id)]

    statuses = {
        (method, p): (await api.request(method, p, headers=admin_headers)).status_code
        for method, p in attempts
    }

    assert set(statuses.values()) == {405}


async def test_an_id_that_is_out_of_range_or_not_a_number_is_unprocessable(
    api: AsyncClient, admin_headers: dict[str, str]
) -> None:
    bad = [
        f"{BASE}/0",
        f"{BASE}/abc",
        f"{BASE}/{BEYOND_INT64}",
        f"{BASE}/-1",
        f"{BASE}/0/runs",
        f"{BASE}/{BEYOND_INT64}/runs",
        f"{BASE}/-/runs/0",
        f"{BASE}/-/runs/x",
        f"{BASE}/-/runs/{BEYOND_INT64}",
    ]

    responses = {p: await api.get(p, headers=admin_headers) for p in bad}

    assert {p: r.status_code for p, r in responses.items()} == dict.fromkeys(bad, 422)
    assert {r.json()["error"]["status"] for r in responses.values()} == {422}


async def test_an_id_that_names_nothing_is_not_found(
    api: AsyncClient, admin_headers: dict[str, str]
) -> None:
    missing = [f"{BASE}/{UNKNOWN}", f"{BASE}/{UNKNOWN}/runs", f"{BASE}/-/runs/{UNKNOWN}"]

    responses = {p: await api.get(p, headers=admin_headers) for p in missing}

    assert {p: r.status_code for p, r in responses.items()} == dict.fromkeys(missing, 404)


BAD_CURSORS = [
    "!!!",
    "é",
    base64.urlsafe_b64encode(b"[1]").decode(),
    base64.urlsafe_b64encode(b'["x","y"]').decode(),
    base64.urlsafe_b64encode(b"[true,1]").decode(),
    "x" * 600,
]


async def test_a_cursor_that_is_not_one_the_routes_issued_is_refused(
    api: AsyncClient, admin_headers: dict[str, str], job_id: int
) -> None:
    attempts = [(p, cursor) for p in (BASE, f"{BASE}/{job_id}/runs") for cursor in BAD_CURSORS]

    statuses = [
        (await api.get(p, params={"cursor": cursor}, headers=admin_headers)).status_code
        for p, cursor in attempts
    ]

    assert set(statuses) <= {400, 422}


async def test_a_page_size_outside_the_bounds_is_unprocessable(
    api: AsyncClient, admin_headers: dict[str, str], job_id: int
) -> None:
    attempts = [
        (p, limit) for p in (BASE, f"{BASE}/{job_id}/runs") for limit in ("0", "501", "x", "-1")
    ]

    statuses = [
        (await api.get(p, params={"limit": limit}, headers=admin_headers)).status_code
        for p, limit in attempts
    ]

    assert set(statuses) == {422}


async def test_a_job_reports_the_start_and_outcome_of_its_latest_recorded_run(
    api: AsyncClient, admin_headers: dict[str, str], job_id: int
) -> None:
    body = (await api.get(f"{BASE}/{job_id}", headers=admin_headers)).json()

    assert body["last_run_outcome"] == "failure"
    assert body["last_run_at"] == body["last_run"]["started_at"]


async def test_a_job_whose_latest_run_left_no_row_still_reports_its_start_and_outcome(
    api: AsyncClient, admin_headers: dict[str, str], started_app: FastAPI
) -> None:
    job = await refresher_job_id(started_app)

    body = (await api.get(f"{BASE}/{job}", headers=admin_headers)).json()

    assert body["last_run"] is None
    assert body["last_run_outcome"] == "success"
    assert body["last_run_at"] is not None


async def test_a_job_reports_whether_it_is_a_system_task_or_an_automation(
    api: AsyncClient, admin_headers: dict[str, str], job_id: int, rule_job_id: int
) -> None:
    listing = (await api.get(BASE, headers=admin_headers)).json()["items"]
    single = (await api.get(f"{BASE}/{rule_job_id}", headers=admin_headers)).json()

    kinds = {j["id"]: j["kind"] for j in listing}
    assert (kinds[job_id], kinds[rule_job_id]) == ("system", "automation")
    assert single["kind"] == "automation"


async def test_the_listing_filters_by_kind(
    api: AsyncClient, admin_headers: dict[str, str], job_id: int, rule_job_id: int
) -> None:
    systems = (await api.get(f"{BASE}?kind=system", headers=admin_headers)).json()["items"]
    automations = (await api.get(f"{BASE}?kind=automation", headers=admin_headers)).json()["items"]

    assert {j["kind"] for j in systems} == {"system"} and job_id in {j["id"] for j in systems}
    assert [j["id"] for j in automations] == [rule_job_id]


async def test_a_kind_that_is_not_offered_is_unprocessable(
    api: AsyncClient, admin_headers: dict[str, str]
) -> None:
    statuses = [
        (await api.get(BASE, params={"kind": kind}, headers=admin_headers)).status_code
        for kind in ("other", "System", "x" * 65)
    ]

    assert statuses == [422, 422, 422]
