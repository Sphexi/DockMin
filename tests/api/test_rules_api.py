"""The JSON routes for automation rules: bearer access, ownership, run guards, page parity."""

import json
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.config import Settings
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_SESSION_TOKEN,
    STANDARD_ID,
    STANDARD_SESSION_TOKEN,
    Browsers,
    csrf_form,
    csrf_headers,
)
from tests.conftest import AttachApp
from tests.job_support import (
    ADMIN,
    JOB_SUBSYSTEMS,
    RESTARTER,
    events,
    job_services,
    rows,
)
from tests.web.environment_support import World, bearer, issue_token

BASE = "/api/v1/rules"
UNKNOWN = 99_999
FAR_FUTURE = "2030-01-01T00:00:00Z"
STALE_DIGEST = "0" * 64
PAUSE = {"duration": "1h"}
HISTORY = 3
RULE_KEYS = {
    "webhook",
    "id",
    "name",
    "subsystem",
    "environment_id",
    "environment_name",
    "selector",
    "target",
    "parameters",
    "enabled",
    "paused_until",
    "state",
    "schedule",
    "next_run_at",
    "last_run",
    "supports_dry_run",
    "subsystem_enabled",
    "available",
    "owner",
    "created_at",
    "updated_at",
    "digest",
}
ID_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "{rule}", None),
    ("GET", "{rule}/runs", None),
    ("GET", "{rule}/runs/{run}", None),
    ("POST", "{rule}/pause", PAUSE),
    ("POST", "{rule}/resume", None),
    ("POST", "{rule}/disable", None),
    ("POST", "{rule}/run", {"dry_run": True}),
]


@pytest.fixture
def app(settings: Settings, attach_app: AttachApp) -> FastAPI:
    return attach_app(settings, JOB_SUBSYSTEMS)


async def add_rule(
    app: FastAPI,
    world: World,
    name: str,
    *,
    owner: int = ADMIN_ID,
    enabled: bool = True,
    selector: str = "web",
) -> tuple[int, int]:
    """Insert a rule and its job without starting anything; return the rule and job ids."""
    clock = app.state.auth.clock
    async with app.state.database.session() as db, db.transaction():
        rule = await AutomationRuleRepository(db, clock).insert(
            name=name,
            subsystem=RESTARTER,
            environment_id=world.local_id,
            target_selector=selector,
            parameters="{}",
            is_enabled=enabled,
            created_by_user_id=owner,
        )
        job = await ScheduledJobRepository(db, clock).insert(
            subsystem=RESTARTER,
            rule_id=rule.id,
            environment_id=world.local_id,
            schedule_kind="interval",
            schedule_expression="600",
            time_zone="UTC",
            overrun_policy="skip",
            catch_up=False,
            failure_threshold=5,
            next_run_at=FAR_FUTURE,
        )
    return rule.id, job.id


async def add_runs(app: FastAPI, job_id: int, count: int) -> list[int]:
    ids: list[int] = []
    async with app.state.database.session() as db:
        runs = JobRunRepository(db, app.state.auth.clock)
        for _ in range(count):
            run = await runs.start(job_id, trigger="manual", scheduled_for=None)
            await runs.finish(run.id, outcome="success", reason=None, message=None, detail={})
            ids.append(run.id)
    return ids


@pytest.fixture
async def admin_headers(browsers: Browsers) -> dict[str, str]:
    return bearer(await issue_token(browsers.open(session_token=ADMIN_SESSION_TOKEN)))


@pytest.fixture
async def standard_headers(browsers: Browsers) -> dict[str, str]:
    return bearer(await issue_token(browsers.open(session_token=STANDARD_SESSION_TOKEN)))


@pytest.fixture
def api(browsers: Browsers) -> AsyncClient:
    return browsers.open()


def calls(rule: int, run: int) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Every route of the router once, with a body where one is required."""
    return [("GET", BASE, None)] + [
        (method, f"{BASE}/{template.format(rule=rule, run=run)}", body)
        for method, template, body in ID_ROUTES
    ]


def shown(response: Response) -> tuple[int, str]:
    return response.status_code, response.text


async def test_only_a_valid_bearer_token_reaches_a_rule_route_and_a_cookie_is_not_enough(
    api: AsyncClient, browsers: Browsers, world: World
) -> None:
    rule, _ = await add_rule(world.app, world, "Nightly")
    cookie = browsers.open(session_token=ADMIN_SESSION_TOKEN)
    forged = {"Authorization": "Bearer dkm_notatoken"}
    routes = calls(rule, 1)
    reads = [(m, p, b) for m, p, b in routes if m == "GET"]
    writes = [(m, p, b) for m, p, b in routes if m == "POST"]

    anonymous_reads = {p: (await api.get(p)).status_code for _, p, _ in reads}
    forged_all = {
        (m, p): (await api.request(m, p, json=b, headers=forged)).status_code for m, p, b in routes
    }
    cookie_reads = {p: (await cookie.get(p)).status_code for _, p, _ in reads}
    with_csrf = {
        p: (await cookie.post(p, json=b, headers=csrf_headers(cookie))).status_code
        for _, p, b in writes
    }
    without_csrf = {p: (await cookie.post(p, json=b)).status_code for _, p, b in writes}

    assert set(anonymous_reads.values()) == {401}
    assert set(forged_all.values()) == {401}
    assert set(cookie_reads.values()) == {401}
    assert set(with_csrf.values()) == {401}
    assert set(without_csrf.values()) == {403}


async def test_a_rule_that_is_not_the_callers_is_not_found_exactly_like_a_missing_or_deleted_one(
    api: AsyncClient, standard_headers: dict[str, str], world: World
) -> None:
    app = world.app
    others, other_job = await add_rule(app, world, "Admin rule")
    gone, _ = await add_rule(app, world, "Deleted rule")
    [run] = await add_runs(app, other_job, 1)
    async with job_services(app) as services:
        await services.rules.delete(ADMIN, gone, expected_name="Deleted rule")

    answers = {
        (method, template): [
            shown(
                await api.request(
                    method,
                    f"{BASE}/{template.format(rule=target, run=run)}",
                    json=body,
                    headers=standard_headers,
                )
            )
            for target in (others, gone, UNKNOWN)
        ]
        for method, template, body in ID_ROUTES
    }

    assert len(answers) == len(ID_ROUTES)
    assert {a[0][0] for a in answers.values()} == {404}
    assert all(len(set(a)) == 1 for a in answers.values())


async def test_the_listing_holds_a_standard_users_own_rules_and_every_rule_for_an_administrator(
    api: AsyncClient,
    admin_headers: dict[str, str],
    standard_headers: dict[str, str],
    world: World,
) -> None:
    app = world.app
    admin_rule, _ = await add_rule(app, world, "Admin rule")
    own_rule, _ = await add_rule(app, world, "Own rule", owner=STANDARD_ID)

    mine = (await api.get(BASE, headers=standard_headers)).json()
    everything = (await api.get(BASE, headers=admin_headers)).json()
    first = (await api.get(f"{BASE}?limit=1", headers=admin_headers)).json()
    second = (
        await api.get(
            BASE, params={"limit": 1, "cursor": first["next_cursor"]}, headers=admin_headers
        )
    ).json()

    assert [r["id"] for r in mine["items"]] == [own_rule]
    assert [r["id"] for r in everything["items"]] == [admin_rule, own_rule]
    assert (len(first["items"]), len(second["items"])) == (1, 1)
    assert first["items"][0]["id"] == admin_rule and second["items"][0]["id"] == own_rule
    assert set(everything["items"][0]) == RULE_KEYS


async def test_a_rule_names_its_owner_to_an_administrator_and_to_nobody_else(
    api: AsyncClient,
    admin_headers: dict[str, str],
    standard_headers: dict[str, str],
    world: World,
) -> None:
    rule, _ = await add_rule(world.app, world, "Own rule", owner=STANDARD_ID)

    seen_by_admin = (await api.get(f"{BASE}/{rule}", headers=admin_headers)).json()
    seen_by_owner = (await api.get(f"{BASE}/{rule}", headers=standard_headers)).json()

    assert seen_by_admin["owner"]["id"] == STANDARD_ID
    assert seen_by_owner["owner"] is None


async def test_a_rules_runs_come_newest_first_in_pages_and_a_run_of_another_job_is_not_found(
    api: AsyncClient, admin_headers: dict[str, str], world: World
) -> None:
    app = world.app
    rule, job = await add_rule(app, world, "Nightly")
    _, other_job = await add_rule(app, world, "Other")
    own_runs = await add_runs(app, job, HISTORY)
    [foreign_run] = await add_runs(app, other_job, 1)

    first = (await api.get(f"{BASE}/{rule}/runs?limit=2", headers=admin_headers)).json()
    second = (
        await api.get(
            f"{BASE}/{rule}/runs",
            params={"limit": 2, "cursor": first["next_cursor"]},
            headers=admin_headers,
        )
    ).json()
    one = await api.get(f"{BASE}/{rule}/runs/{own_runs[0]}", headers=admin_headers)
    foreign = await api.get(f"{BASE}/{rule}/runs/{foreign_run}", headers=admin_headers)

    assert [r["id"] for r in [*first["items"], *second["items"]]] == sorted(own_runs, reverse=True)
    assert second["next_cursor"] is None
    assert (one.status_code, one.json()["job_id"]) == (200, job)
    assert foreign.status_code == 404


async def test_a_real_run_needs_the_reviewed_digest_and_a_stale_one_is_a_conflict(
    api: AsyncClient, admin_headers: dict[str, str], world: World
) -> None:
    rule, _ = await add_rule(world.app, world, "Nightly")
    before = len(await rows(world.app, "SELECT id FROM job_run"))

    missing = await api.post(f"{BASE}/{rule}/run", json={}, headers=admin_headers)
    explicit = await api.post(f"{BASE}/{rule}/run", json={"dry_run": False}, headers=admin_headers)
    stale = await api.post(
        f"{BASE}/{rule}/run", json={"expected_digest": STALE_DIGEST}, headers=admin_headers
    )

    assert (missing.status_code, explicit.status_code) == (422, 422)
    assert stale.status_code == 409
    assert stale.json()["error"]["reason"] == "changed_since_review"
    assert len(await rows(world.app, "SELECT id FROM job_run")) == before


async def test_a_dry_run_needs_no_digest_and_a_real_run_with_the_current_one_starts(
    api: AsyncClient, admin_headers: dict[str, str], world: World
) -> None:
    app = world.app
    rule, job = await add_rule(app, world, "Nightly")
    current = (await api.get(f"{BASE}/{rule}", headers=admin_headers)).json()["digest"]

    dry = await api.post(f"{BASE}/{rule}/run", json={"dry_run": True}, headers=admin_headers)
    await app.state.scheduler.scheduler.idle()
    real = await api.post(
        f"{BASE}/{rule}/run", json={"expected_digest": current}, headers=admin_headers
    )
    await app.state.scheduler.scheduler.idle()

    started = await rows(app, "SELECT id, trigger FROM job_run WHERE scheduled_job_id = ?", (job,))
    assert dry.status_code == real.status_code == 202
    assert set(dry.json()) == {"run_id"}
    assert [r["id"] for r in started] == [dry.json()["run_id"], real.json()["run_id"]]
    assert [r["trigger"] for r in started] == ["dry_run", "manual"]


async def test_pause_resume_and_disable_write_the_same_activity_rows_as_the_pages(
    api: AsyncClient,
    admin_headers: dict[str, str],
    admin_client: AsyncClient,
    world: World,
) -> None:
    app = world.app
    by_api, _ = await add_rule(app, world, "By API")
    by_page, _ = await add_rule(app, world, "By page")
    since = len(await events(app))

    paused = await api.post(f"{BASE}/{by_api}/pause", json=PAUSE, headers=admin_headers)
    resumed = await api.post(f"{BASE}/{by_api}/resume", headers=admin_headers)
    disabled = await api.post(f"{BASE}/{by_api}/disable", headers=admin_headers)
    for action, data in (("pause", PAUSE), ("resume", {}), ("disable", {})):
        await admin_client.post(
            f"/automation/rules/{by_page}/{action}", data=csrf_form(admin_client, **data)
        )

    recorded = [r for r in (await events(app))[since:] if r["target_type"] == "automation_rule"]
    api_rows = [normal(r) for r in recorded if r["target_id"] == str(by_api)]
    page_rows = [normal(r) for r in recorded if r["target_id"] == str(by_page)]
    assert (paused.status_code, resumed.status_code, disabled.status_code) == (200, 200, 200)
    assert paused.json()["paused_until"] is not None and resumed.json()["paused_until"] is None
    assert disabled.json()["enabled"] is False
    assert [r["action"] for r in api_rows] == [
        "automation_rule_paused",
        "automation_rule_resumed",
        "automation_rule_disabled",
    ]
    assert api_rows == page_rows


def normal(event: dict[str, Any]) -> dict[str, Any]:
    """Drop what differs between two rules by design: the ids and the moment a pause ends."""
    detail = json.loads(event["detail"] or "{}")
    detail.pop("paused_until", None)
    return {
        key: event[key]
        for key in (
            "action",
            "result",
            "actor_type",
            "actor_user_id",
            "target_type",
            "environment_id",
        )
    } | {"detail": detail}


async def test_a_standard_owner_can_pause_and_disable_their_rule_and_not_anothers(
    api: AsyncClient, standard_headers: dict[str, str], world: World
) -> None:
    own_rule, _ = await add_rule(world.app, world, "Own rule", owner=STANDARD_ID)
    other_rule, _ = await add_rule(world.app, world, "Admin rule")

    own = await api.post(f"{BASE}/{own_rule}/pause", json=PAUSE, headers=standard_headers)
    other = await api.post(f"{BASE}/{other_rule}/pause", json=PAUSE, headers=standard_headers)

    unpaused = await rows(
        world.app, "SELECT paused_until FROM automation_rule WHERE id = ?", (other_rule,)
    )
    assert own.status_code == 200 and other.status_code == 404
    assert unpaused[0]["paused_until"] is None


async def test_an_unknown_body_field_and_a_pause_length_that_is_not_offered_are_unprocessable(
    api: AsyncClient, admin_headers: dict[str, str], world: World
) -> None:
    rule, _ = await add_rule(world.app, world, "Nightly")

    extra = await api.post(
        f"{BASE}/{rule}/run", json={"dry_run": True, "force": True}, headers=admin_headers
    )
    length = await api.post(f"{BASE}/{rule}/pause", json={"duration": "2h"}, headers=admin_headers)

    assert (extra.status_code, length.status_code) == (422, 422)
