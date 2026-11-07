"""The Jobs pages: who may open them, what they show, and every action as a script-free form."""

import re
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.services.activity import SUBSYSTEMS, UNSCHEDULED_SUBSYSTEMS
from app.web.job_routes import SYSTEM_TASK_NOTES
from tests.auth_support import ADMIN_ID, csrf_form
from tests.conftest import AttachApp
from tests.job_support import (
    JOB_SUBSYSTEMS,
    REFRESHER,
    RESTARTER,
    UPDATER,
    execute,
    refresher_job_id,
    rows,
    withhold_handler,
)
from tests.web.environment_support import World
from tests.web.helpers import refresh_metas_in, reviewed_run_data, run_region_in
from tests.web.html_tree import parse

RULE_NAME = "Nightly <b>health</b>"
RUN_MESSAGE = "<script>alert(1)</script> unreachable"
FAR_FUTURE = "2030-01-01T00:00:00Z"
THRESHOLD_FAILURES = 5
UNPROCESSABLE = 422
UNKNOWN_JOB = 99_999
HUGE_ID = 2**70
UNAVAILABLE = "backup-syncer"
PRUNER = "activity-log-pruner"
SENDER = "notification-sender"
QUIET_RUN = "2030-02-03T04:05:00Z"
QUIET_RUN_SHOWN = "2030-02-03 04:05 UTC"
NO_ROW_NOTE = "Successful scheduled runs of a job that writes no activity events are not kept."
INTRO_TERMS = ("administrator", "every scheduled job", "automations", "system tasks")


@pytest.fixture
def app(settings: Settings, attach_app: AttachApp) -> FastAPI:
    return attach_app(settings, JOB_SUBSYSTEMS)


@dataclass
class Seed:
    rule_job_id: int
    rule_id: int
    refresher_job_id: int
    run_ids: list[int]


async def add_rule_job(
    app: FastAPI,
    world: World,
    name: str,
    *,
    subsystem: str = RESTARTER,
    **job_state: str | int | None,
) -> tuple[int, int]:
    """Insert an enabled rule and its job, then apply the given job columns; return both ids."""
    clock = app.state.auth.clock
    async with app.state.database.session() as db, db.transaction():
        rule = await AutomationRuleRepository(db, clock).insert(
            name=name,
            subsystem=subsystem,
            environment_id=world.local_id if subsystem == RESTARTER else None,
            target_selector="",
            parameters="{}",
            is_enabled=True,
            created_by_user_id=ADMIN_ID,
        )
        job = await ScheduledJobRepository(db, clock).insert(
            subsystem=subsystem,
            rule_id=rule.id,
            schedule_kind="interval",
            schedule_expression="600",
            time_zone="UTC",
            overrun_policy="skip",
            catch_up=False,
            failure_threshold=THRESHOLD_FAILURES,
            next_run_at=FAR_FUTURE,
        )
    for column, value in job_state.items():
        await execute(app, f"UPDATE scheduled_job SET {column} = ? WHERE id = ?", (value, job.id))
    return rule.id, job.id


@pytest.fixture
async def seed(world: World) -> Seed:
    app = world.app
    rule_id, job_id = await add_rule_job(app, world, RULE_NAME)
    clock = app.state.auth.clock
    run_ids: list[int] = []
    async with app.state.database.session() as db:
        runs = JobRunRepository(db, clock)
        for index in range(3):
            run = await runs.start(job_id, trigger="schedule", scheduled_for=None)
            failed = index == 1
            await runs.finish(
                run.id,
                outcome="failure" if failed else "success",
                reason="host_down" if failed else None,
                message=RUN_MESSAGE if failed else None,
                detail={"checked": index, "flag": True, "note": "<i>x</i>"},
            )
            run_ids.append(run.id)
    return Seed(job_id, rule_id, await refresher_job_id(app), run_ids)


def every_page(seed: Seed) -> list[str]:
    return [
        "/jobs",
        f"/jobs?subsystem={RESTARTER}",
        f"/jobs/{seed.rule_job_id}",
        f"/jobs/{seed.refresher_job_id}",
        f"/jobs/-/runs/{seed.run_ids[1]}",
        "/jobs/-/subsystems",
        f"/jobs/{seed.rule_job_id}/run",
        f"/jobs/{seed.rule_job_id}/enable",
        f"/jobs/-/subsystems/{RESTARTER}/disable",
        f"/jobs/-/subsystems/{RESTARTER}/enable",
    ]


async def test_every_job_page_renders_for_an_administrator_and_is_never_cached(
    admin_client: AsyncClient, seed: Seed
) -> None:
    responses = {path: await admin_client.get(path) for path in every_page(seed)}

    assert {path: r.status_code for path, r in responses.items()} == dict.fromkeys(responses, 200)
    assert {r.headers["cache-control"] for r in responses.values()} == {"no-store"}


async def test_the_jobs_page_opens_as_the_administrators_view_of_every_scheduled_job(
    admin_client: AsyncClient, seed: Seed
) -> None:
    page = await admin_client.get("/jobs")

    subtitle = next(
        node.text() for node in parse(page.text).walk() if "page-header__subtitle" in node.classes()
    )
    assert [term for term in INTRO_TERMS if term not in subtitle.lower()] == []


async def test_rule_names_run_messages_and_detail_are_shown_escaped(
    admin_client: AsyncClient, seed: Seed
) -> None:
    listing = (await admin_client.get("/jobs")).text
    run_page = (await admin_client.get(f"/jobs/-/runs/{seed.run_ids[1]}")).text
    history = (await admin_client.get(f"/jobs/{seed.rule_job_id}")).text

    assert "Nightly &lt;b&gt;health&lt;/b&gt;" in listing
    assert "host_down" in run_page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in run_page
    assert "&lt;i&gt;x&lt;/i&gt;" in run_page
    assert "<script>alert" not in listing + run_page + history
    assert "<b>health</b>" not in listing + history


async def test_the_navigation_offers_the_jobs_page_to_an_administrator_only(
    admin_client: AsyncClient, standard_client: AsyncClient
) -> None:
    admin_nav = (await admin_client.get("/")).text
    standard_nav = (await standard_client.get("/")).text

    assert 'href="/jobs"' in admin_nav
    assert 'href="/jobs"' not in standard_nav


async def test_a_standard_user_is_refused_every_page_and_an_anonymous_visitor_is_not_let_in(
    standard_client: AsyncClient, anonymous_client: AsyncClient, seed: Seed
) -> None:
    standard = {p: (await standard_client.get(p)).status_code for p in every_page(seed)}
    anonymous = {p: (await anonymous_client.get(p)).status_code for p in every_page(seed)}

    assert set(standard.values()) == {403}
    assert set(anonymous.values()) <= {303, 401}


async def test_a_refused_page_is_never_cached(standard_client: AsyncClient) -> None:
    response = await standard_client.get("/jobs")

    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"


async def test_a_missing_page_is_never_cached(admin_client: AsyncClient) -> None:
    response = await admin_client.get("/no-such-page")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


async def test_an_error_fragment_for_an_htmx_request_is_never_cached(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.get("/no-such-page", headers={"HX-Request": "true"})

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


async def test_an_id_that_cannot_name_a_job_or_run_is_not_found(
    admin_client: AsyncClient,
) -> None:
    paths = [
        "/jobs/abc",
        f"/jobs/{UNKNOWN_JOB}",
        f"/jobs/{HUGE_ID}",
        "/jobs/-/runs/abc",
        f"/jobs/-/runs/{UNKNOWN_JOB}",
        "/jobs/-/subsystems/nope/enable",
        "/jobs/-/subsystems/Bad_Name/disable",
        f"/jobs/{UNKNOWN_JOB}/run",
        f"/jobs/{UNKNOWN_JOB}/enable",
    ]

    statuses = {path: (await admin_client.get(path)).status_code for path in paths}

    assert statuses == dict.fromkeys(paths, 404)


async def test_a_bad_cursor_is_a_bad_request_and_an_unknown_subsystem_filter_is_explained(
    admin_client: AsyncClient, seed: Seed
) -> None:
    cursor = await admin_client.get("/jobs?cursor=zzz")
    history_cursor = await admin_client.get(f"/jobs/{seed.rule_job_id}?cursor=zzz")
    filtered = await admin_client.get("/jobs?subsystem=nope")

    assert (cursor.status_code, history_cursor.status_code) == (400, 400)
    assert filtered.status_code == 422
    assert "Choose a known subsystem." in filtered.text


async def test_the_subsystem_filter_accepts_the_activity_log_pruner_before_it_has_a_job(
    admin_client: AsyncClient, seed: Seed
) -> None:
    response = await admin_client.get(f"/jobs?subsystem={PRUNER}")

    assert response.status_code == 200
    assert f'value="{PRUNER}" selected' in response.text
    assert "Nothing matches these filters" in response.text


async def test_every_post_is_refused_for_a_standard_user_and_for_a_request_without_the_csrf_token(
    admin_client: AsyncClient, standard_client: AsyncClient, seed: Seed
) -> None:
    job = seed.rule_job_id
    forms = {
        f"/jobs/{job}/pause": {"duration": "1h"},
        f"/jobs/{job}/resume": {},
        f"/jobs/{job}/run": {},
        f"/jobs/{job}/enable": {},
        f"/jobs/-/subsystems/{RESTARTER}/disable": {},
        f"/jobs/-/subsystems/{RESTARTER}/enable": {},
        f"/jobs/{UNKNOWN_JOB}/run": {},
    }

    standard = {
        path: (
            await standard_client.post(path, data=csrf_form(standard_client, **fields))
        ).status_code
        for path, fields in forms.items()
    }
    without_token = {
        path: (await admin_client.post(path, data=fields)).status_code
        for path, fields in forms.items()
    }

    assert set(standard.values()) == {403}
    assert set(without_token.values()) == {403}


async def test_pausing_a_rules_job_pauses_the_rule_and_resuming_clears_it(
    admin_client: AsyncClient, seed: Seed
) -> None:
    path = f"/jobs/{seed.rule_job_id}"

    paused = await admin_client.post(
        f"{path}/pause", data=csrf_form(admin_client, duration="4h"), follow_redirects=False
    )
    shown = (await admin_client.get(paused.headers["location"])).text
    resumed = await admin_client.post(
        f"{path}/resume", data=csrf_form(admin_client), follow_redirects=True
    )

    assert paused.status_code == 303
    assert "The job is paused." in shown and "Paused until" in shown and "Resume" in shown
    assert "The job is resumed." in resumed.text and "Pause job" in resumed.text


async def test_pausing_a_rule_less_job_and_an_unknown_length_are_handled_on_the_page(
    admin_client: AsyncClient, seed: Seed
) -> None:
    path = f"/jobs/{seed.refresher_job_id}"

    paused = await admin_client.post(
        f"{path}/pause", data=csrf_form(admin_client, duration="1h"), follow_redirects=True
    )
    refused = await admin_client.post(
        f"/jobs/{seed.rule_job_id}/pause", data=csrf_form(admin_client, duration="9y")
    )

    assert paused.status_code == 200 and "Paused until" in paused.text
    assert refused.status_code == 422
    assert "The job was not changed" in refused.text


async def test_run_now_opens_the_new_run_and_a_dry_run_is_offered_only_where_supported(
    admin_client: AsyncClient, seed: Seed, world: World
) -> None:
    path = f"/jobs/{seed.rule_job_id}"

    started = await admin_client.post(
        f"{path}/run",
        data=await reviewed_run_data(admin_client, f"{path}/run"),
        follow_redirects=False,
    )
    await world.app.state.scheduler.scheduler.idle()
    shown = await admin_client.get(started.headers["location"])
    dry = await admin_client.post(
        f"{path}/run", data=csrf_form(admin_client, dry_run="yes"), follow_redirects=True
    )
    await world.app.state.scheduler.scheduler.idle()
    supported_page = (await admin_client.get(path)).text
    unsupported_page = (await admin_client.get(f"/jobs/{seed.refresher_job_id}")).text

    assert started.status_code == 303
    assert started.headers["location"].startswith("/jobs/-/runs/")
    assert "The run has started." in shown.text and "Run now" in shown.text
    assert "The dry run has started." in dry.text
    assert 'name="dry_run"' in supported_page
    assert 'name="dry_run"' not in unsupported_page


async def test_a_run_that_cannot_start_is_a_conflict_that_says_why_and_the_page_says_so_too(
    admin_client: AsyncClient, seed: Seed, world: World
) -> None:
    path = f"/jobs/{seed.rule_job_id}"
    reviewed = await reviewed_run_data(admin_client, f"{path}/run")
    await admin_client.post(
        f"/jobs/-/subsystems/{RESTARTER}/disable",
        data=csrf_form(admin_client),
        follow_redirects=True,
    )

    refused = await admin_client.post(f"{path}/run", data=reviewed)
    confirm = await admin_client.get(f"{path}/run")
    detail = await admin_client.get(path)
    await admin_client.post(
        f"/jobs/-/subsystems/{RESTARTER}/enable",
        data=csrf_form(admin_client),
        follow_redirects=True,
    )
    from_htmx = await admin_client.post(
        f"{path}/run", data=reviewed, headers={"HX-Request": "true"}
    )
    await world.app.state.scheduler.scheduler.idle()

    assert refused.status_code == 409
    assert "The subsystem is disabled." in refused.text
    assert "The job cannot run now" in confirm.text
    assert "Cannot run now." in detail.text
    assert from_htmx.status_code == 204


async def test_a_job_disabled_after_failures_shows_it_and_is_enabled_from_a_confirmation_page(
    admin_client: AsyncClient, seed: Seed, world: World
) -> None:
    await execute(
        world.app,
        "UPDATE scheduled_job SET is_enabled = 0, consecutive_failures = ?, next_run_at = NULL "
        "WHERE id = ?",
        (THRESHOLD_FAILURES, seed.rule_job_id),
    )
    path = f"/jobs/{seed.rule_job_id}"

    page = await admin_client.get(path)
    confirm = await admin_client.get(f"{path}/enable")
    done = await admin_client.post(
        f"{path}/enable", data=csrf_form(admin_client), follow_redirects=True
    )

    assert "Disabled after 5 failures" in page.text and "Enable job" in page.text
    assert "reset to 0" in confirm.text
    assert "The job is enabled." in done.text and "Disabled after" not in done.text


async def test_each_state_a_job_can_be_in_has_its_own_label_on_the_list(
    admin_client: AsyncClient, seed: Seed, world: World
) -> None:
    app = world.app
    withhold_handler(app, UNAVAILABLE)
    await add_rule_job(app, world, "unscheduled", next_run_at=None)
    await add_rule_job(app, world, "paused", paused_until=FAR_FUTURE)
    rule, _ = await add_rule_job(app, world, "rule off")
    await execute(app, "UPDATE automation_rule SET is_enabled = 0 WHERE id = ?", (rule,))
    await add_rule_job(app, world, "auto off", is_enabled=0, consecutive_failures=1)
    await add_rule_job(app, world, "no handler", subsystem=UNAVAILABLE)
    await admin_client.post(f"/jobs/-/subsystems/{UPDATER}/disable", data=csrf_form(admin_client))
    await add_rule_job(app, world, "switched off", subsystem=UPDATER)

    page = (await admin_client.get("/jobs?limit=100")).text

    for label in (
        "Scheduled",
        "Nothing scheduled",
        "Paused until",
        "Automation disabled",
        "Disabled after 1 failure",
        "Not available",
    ):
        assert label in page


async def test_the_subsystem_page_lists_every_subsystem_and_marks_the_ones_missing_from_this_build(
    admin_client: AsyncClient, seed: Seed, world: World
) -> None:
    withhold_handler(world.app, UNAVAILABLE)
    page = (await admin_client.get("/jobs/-/subsystems")).text

    assert RESTARTER in page and REFRESHER in page
    assert "Not available in this build" in page


async def test_switching_a_subsystem_names_its_jobs_first_and_then_reports_the_change(
    admin_client: AsyncClient, seed: Seed
) -> None:
    confirm = await admin_client.get(f"/jobs/-/subsystems/{RESTARTER}/disable")
    done = await admin_client.post(
        f"/jobs/-/subsystems/{RESTARTER}/disable",
        data=csrf_form(admin_client),
        follow_redirects=True,
    )

    assert "Nightly &lt;b&gt;health&lt;/b&gt;" in confirm.text
    assert "The subsystem is disabled." in done.text


async def test_htmx_requests_get_fragments_and_boosted_or_restored_ones_get_the_full_page(
    admin_client: AsyncClient, seed: Seed
) -> None:
    fragment = await admin_client.get("/jobs", headers={"HX-Request": "true"})
    boosted = await admin_client.get("/jobs", headers={"HX-Request": "true", "HX-Boosted": "true"})
    restored = await admin_client.get(
        "/jobs", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"}
    )
    runs = await admin_client.get(
        f"/jobs/{seed.rule_job_id}?limit=1", headers={"HX-Request": "true"}
    )
    missing = await admin_client.get(f"/jobs/{UNKNOWN_JOB}", headers={"HX-Request": "true"})

    assert "<html" not in fragment.text and 'id="jobs-table"' in fragment.text
    assert "<html" in boosted.text and "<html" in restored.text
    assert "<html" not in runs.text and 'id="runs-table"' in runs.text
    assert missing.status_code == 404 and "<html" not in missing.text


async def test_the_run_history_pages_by_cursor_newest_first(
    admin_client: AsyncClient, seed: Seed
) -> None:
    first = await admin_client.get(
        f"/jobs/{seed.rule_job_id}?limit=1", headers={"HX-Request": "true"}
    )
    link = re.search(r'rel="next" hx-get="([^"]+)"', first.text)
    assert link is not None

    second = await admin_client.get(
        link.group(1).replace("&amp;", "&"), headers={"HX-Request": "true"}
    )

    assert f"/jobs/-/runs/{seed.run_ids[2]}" in first.text
    assert f"/jobs/-/runs/{seed.run_ids[1]}" in second.text
    assert f"/jobs/-/runs/{seed.run_ids[2]}" not in second.text


async def test_every_form_carries_the_csrf_token_and_no_page_needs_script(
    admin_client: AsyncClient, seed: Seed
) -> None:
    pages = [
        f"/jobs/{seed.rule_job_id}",
        f"/jobs/{seed.rule_job_id}/run",
        f"/jobs/{seed.rule_job_id}/enable",
        f"/jobs/-/subsystems/{RESTARTER}/disable",
        "/jobs/-/subsystems",
    ]
    texts = [(await admin_client.get(path)).text for path in pages]

    forms = [
        form
        for text in texts
        for form in re.findall(r'<form[^>]*method="post".*?</form>', text, re.S)
    ]

    assert forms and all('name="csrf_token"' in form for form in forms)
    assert not [t for t in texts if "onclick" in t or "style=" in t or "javascript:" in t]


POLL_TRIGGER = "every 2s"
NOSCRIPT_REFRESH = "5"


async def start_run(app: FastAPI, job_id: int) -> int:
    async with app.state.database.session() as db:
        run = await JobRunRepository(db, app.state.auth.clock).start(
            job_id, trigger="schedule", scheduled_for=None
        )
    return run.id


async def finish_run(app: FastAPI, run_id: int) -> None:
    async with app.state.database.session() as db:
        await JobRunRepository(db, app.state.auth.clock).finish(
            run_id, outcome="success", reason=None, message=None, detail={}
        )


async def test_an_unfinished_run_page_polls_its_region_and_asks_a_browser_without_script_to_reload(
    admin_client: AsyncClient, world: World, seed: Seed
) -> None:
    run = await start_run(world.app, seed.rule_job_id)
    path = f"/jobs/-/runs/{run}"

    page = await admin_client.get(path)
    fragment = await admin_client.get(path, headers={"HX-Request": "true"})

    region = run_region_in(page.text)
    assert region is not None
    assert (region.attrs["hx-get"], region.attrs["hx-trigger"], region.attrs["aria-busy"]) == (
        path,
        POLL_TRIGGER,
        "true",
    )
    assert [m.attrs["content"] for m in refresh_metas_in(page.text)] == [NOSCRIPT_REFRESH]
    assert "Still running" in page.text and "data-nojs-only" in page.text
    assert "<html" not in fragment.text
    fragment_region = run_region_in(fragment.text)
    assert fragment_region is not None
    assert fragment_region.attrs["hx-trigger"] == POLL_TRIGGER


async def test_a_finished_run_page_stops_polling_and_no_longer_asks_for_a_reload(
    admin_client: AsyncClient, world: World, seed: Seed
) -> None:
    run = await start_run(world.app, seed.rule_job_id)
    path = f"/jobs/-/runs/{run}"
    await finish_run(world.app, run)

    page = await admin_client.get(path)
    fragment = await admin_client.get(path, headers={"HX-Request": "true"})

    for html in (page.text, fragment.text):
        region = run_region_in(html)
        assert region is not None
        assert region.attrs["aria-busy"] == "false"
        assert not {"hx-get", "hx-trigger"} & set(region.attrs)
    assert refresh_metas_in(page.text) == []
    assert "data-nojs-only" not in page.text
    assert "Run finished." in fragment.text


def last_run_cell(page: str) -> str:
    found = re.search(r"<dt>Last run</dt>\s*<dd>(.*?)</dd>", page, re.S)
    assert found is not None
    return found.group(1)


async def test_the_detail_page_links_the_last_recorded_run(
    admin_client: AsyncClient, seed: Seed
) -> None:
    page = await admin_client.get(f"/jobs/{seed.rule_job_id}")

    cell = last_run_cell(page.text)
    assert f"/jobs/-/runs/{seed.run_ids[2]}" in cell
    assert "Success" in cell


async def test_a_last_run_kept_on_the_job_is_shown_without_a_link_beside_an_explained_empty_history(
    admin_client: AsyncClient, world: World
) -> None:
    _, job = await add_rule_job(
        world.app, world, "Quiet", last_run_at=QUIET_RUN, last_run_outcome="success"
    )

    page = await admin_client.get(f"/jobs/{job}")

    cell = last_run_cell(page.text)
    assert QUIET_RUN_SHOWN in cell and "Success" in cell
    assert "/jobs/-/runs/" not in cell
    assert "No runs recorded" in page.text and NO_ROW_NOTE in page.text


async def test_a_job_that_has_not_run_says_so_on_its_detail_page(
    admin_client: AsyncClient, world: World
) -> None:
    _, job = await add_rule_job(world.app, world, "Idle")

    page = await admin_client.get(f"/jobs/{job}")

    assert "Never run" in last_run_cell(page.text)


async def test_the_listing_shows_a_last_run_kept_on_the_job(
    admin_client: AsyncClient, world: World
) -> None:
    await add_rule_job(world.app, world, "Quiet", last_run_at=QUIET_RUN, last_run_outcome="success")

    page = await admin_client.get("/jobs")

    assert QUIET_RUN_SHOWN in page.text


def test_every_system_subsystem_with_a_job_has_a_detail_note() -> None:
    system = {name for name, scheduled in SUBSYSTEMS.items() if not scheduled}

    unnoted = system - UNSCHEDULED_SUBSYSTEMS - set(SYSTEM_TASK_NOTES)

    assert unnoted == set()


async def test_the_list_marks_each_jobs_kind_and_the_kind_filter_narrows_it(
    admin_client: AsyncClient, world: World, seed: Seed
) -> None:
    rule_link = f"/automation/rules/{seed.rule_id}"

    everything = await admin_client.get("/jobs")
    systems = await admin_client.get("/jobs?kind=system")
    automations = await admin_client.get("/jobs?kind=automation")
    unknown = await admin_client.get("/jobs?kind=other")

    assert "System task" in everything.text and "Automation" in everything.text
    assert rule_link in everything.text and rule_link not in systems.text
    assert f"/jobs/{seed.refresher_job_id}" in systems.text
    assert f"/jobs/{seed.refresher_job_id}" not in automations.text
    assert f"/jobs/{seed.rule_job_id}" in automations.text
    assert unknown.status_code == UNPROCESSABLE


async def test_a_system_task_detail_page_says_where_its_behaviour_is_set(
    admin_client: AsyncClient, seed: Seed
) -> None:
    system = await admin_client.get(f"/jobs/{seed.refresher_job_id}")
    automation = await admin_client.get(f"/jobs/{seed.rule_job_id}")

    assert "DOCKMIN_SNAPSHOT_INTERVAL_SECONDS" in system.text
    assert "Built in." not in automation.text


def kinds_by_job(page: str) -> dict[int, str]:
    """Map each listed job's id to the text of its Kind cell."""
    rows_of_jobs = [
        (re.search(r'href="/jobs/(\d+)"', row), re.findall(r"<td>(.*?)</td>", row, re.S))
        for row in re.findall(r"<tr>.*?</tr>", page, re.S)
    ]
    return {
        int(job.group(1)): " ".join(cells[0].split())
        for job, cells in rows_of_jobs
        if job and cells
    }


async def test_every_row_of_the_list_names_its_kind(admin_client: AsyncClient, seed: Seed) -> None:
    page = await admin_client.get("/jobs")

    kinds = kinds_by_job(page.text)
    assert kinds[seed.rule_job_id] == "Automation"
    assert kinds[seed.refresher_job_id] == "System task"
    assert set(kinds.values()) == {"System task", "Automation"}


async def test_an_unknown_kind_is_explained_and_the_filter_keeps_the_known_one_selected(
    admin_client: AsyncClient, seed: Seed
) -> None:
    unknown = await admin_client.get("/jobs?kind=other")
    chosen = await admin_client.get("/jobs?kind=system")

    assert "Choose a known kind." in unknown.text
    assert '<option value="system" selected>' in chosen.text
    assert set(kinds_by_job(chosen.text).values()) == {"System task"}


async def test_the_notification_sender_detail_links_the_page_that_sets_its_channels(
    admin_client: AsyncClient, world: World
) -> None:
    [sender] = await rows(world.app, "SELECT id FROM scheduled_job WHERE subsystem = ?", (SENDER,))

    page = await admin_client.get(f"/jobs/{sender['id']}")

    channels = world.app.url_path_for("notification_channels")
    assert "Built in." in page.text
    assert f'href="{channels}"' in page.text


def test_the_note_check_fails_when_a_system_task_has_no_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(SYSTEM_TASK_NOTES, SENDER)

    with pytest.raises(AssertionError):
        test_every_system_subsystem_with_a_job_has_a_detail_note()
