"""The job service: administrator-only reads and actions, run now, re-enable, subsystem switch."""

from dataclasses import dataclass, replace

import pytest
from fastapi import FastAPI

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.config import Settings
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.scheduled_job import ScheduledJob
from app.scheduler.loop import SchedulerBusy
from app.services.jobs import JobNotRunnable, job_state
from tests.conftest import AttachApp
from tests.job_support import (
    ADMIN,
    JOB_SUBSYSTEMS,
    REFRESHER,
    RESTARTER,
    STANDARD,
    events,
    execute,
    job_services,
    last_event_id,
    refresher_job_id,
    rows,
    rule_arguments,
    seed_settled_world,
    withhold_handler,
)
from tests.scheduler.support import START, at, stamp
from tests.web.environment_support import World

PAST_ANY_JOB = 99_999
PRUNER = "activity-log-pruner"
UNAVAILABLE = "backup-syncer"
THRESHOLD_FAILURES = 5
LONG_AGO = "2026-01-01T00:00:00Z"
FAR_FUTURE = "2099-01-01T00:00:00Z"
STORE_LAST_RUN = (
    "UPDATE scheduled_job SET last_run_at = ?, last_run_outcome = 'success' WHERE id = ?"
)


@pytest.fixture
def app(settings: Settings, attach_app: AttachApp) -> FastAPI:
    return attach_app(settings, JOB_SUBSYSTEMS)


@pytest.fixture
async def seeded(started_app: FastAPI) -> World:
    return await seed_settled_world(started_app)


async def a_rule_job(app: FastAPI, world: World, *, enabled: bool = True) -> tuple[int, int]:
    """Create a rule and return its id and its job's id."""
    async with job_services(app) as services:
        rule = await services.rules.create(ADMIN, **rule_arguments(world.remote_id))
        if enabled:
            await services.rules.enable(ADMIN, rule.id)
    found = await rows(app, "SELECT id FROM scheduled_job WHERE rule_id = ?", (rule.id,))
    return rule.id, int(found[0]["id"])


async def test_every_read_and_action_refuses_a_standard_user(
    started_app: FastAPI, seeded: World
) -> None:
    _, job = await a_rule_job(started_app, seeded)
    async with job_services(started_app) as services:
        calls = [
            services.jobs.page(STANDARD, limit=10),
            services.jobs.detail(STANDARD, job),
            services.jobs.runs(STANDARD, job, limit=10),
            services.jobs.run(STANDARD, 1),
            services.jobs.subsystems(STANDARD),
            services.jobs.subsystem_job_count(STANDARD, RESTARTER),
            services.jobs.pause(STANDARD, job, duration="1h"),
            services.jobs.resume(STANDARD, job),
            services.jobs.enable(STANDARD, job),
            services.jobs.run_now(STANDARD, job),
            services.jobs.set_subsystem_enabled(STANDARD, RESTARTER, enabled=False),
        ]

        for call in calls:
            with pytest.raises(PermissionDenied):
                await call


async def test_the_listing_shows_the_refresher_and_validates_its_filter_and_ids(
    started_app: FastAPI,
) -> None:
    async with job_services(started_app) as services:
        page = await services.jobs.page(ADMIN, limit=10)
        refresher = next(view for view in page.items if view.summary.job.subsystem == REFRESHER)
        with pytest.raises(ValidationFailed):
            await services.jobs.page(ADMIN, limit=10, subsystem="nope")
        with pytest.raises(ResourceNotFound):
            await services.jobs.detail(ADMIN, PAST_ANY_JOB)
        with pytest.raises(ResourceNotFound):
            await services.jobs.detail(ADMIN, 0)
        with pytest.raises(ResourceNotFound):
            await services.jobs.run(ADMIN, PAST_ANY_JOB)

    assert refresher.state in ("scheduled", "running")
    assert refresher.schedule.startswith("Every")


async def test_a_rule_less_job_is_paused_and_resumed_with_the_administrator_as_actor(
    started_app: FastAPI,
) -> None:
    job = await refresher_job_id(started_app)
    mark = await last_event_id(started_app)
    async with job_services(started_app) as services:
        await services.jobs.pause(ADMIN, job, duration="4h")
        paused = await services.jobs.detail(ADMIN, job)
        await services.jobs.resume(ADMIN, job)
        resumed = await services.jobs.detail(ADMIN, job)

    recorded = await events(started_app, since=mark)
    assert (paused.state, resumed.state) == ("paused", "scheduled")
    assert [e["action"] for e in recorded] == ["scheduled_job_paused", "scheduled_job_resumed"]
    assert {(e["actor_type"], e["actor_user_id"], e["target_type"]) for e in recorded} == {
        ("user", ADMIN.user_id, "scheduled_job")
    }


async def test_a_pause_of_an_unknown_length_or_on_a_rules_job_is_refused_and_recorded_as_failed(
    started_app: FastAPI, seeded: World
) -> None:
    refresher = await refresher_job_id(started_app)
    _, rule_job = await a_rule_job(started_app, seeded)
    mark = await last_event_id(started_app)
    async with job_services(started_app) as services:
        with pytest.raises(ValidationFailed) as length:
            await services.jobs.pause(ADMIN, refresher, duration="2h")
        with pytest.raises(ValidationFailed) as owned:
            await services.jobs.pause(ADMIN, rule_job, duration="1h")

    assert "duration" in length.value.fields and "job" in owned.value.fields
    assert [e["result"] for e in await events(started_app, since=mark)] == ["failure", "failure"]


async def test_resuming_a_job_that_is_not_paused_writes_no_event(started_app: FastAPI) -> None:
    job = await refresher_job_id(started_app)
    mark = await last_event_id(started_app)
    async with job_services(started_app) as services:
        await services.jobs.resume(ADMIN, job)

    assert await events(started_app, since=mark) == []


async def test_running_a_job_now_records_the_request_and_the_manual_run_for_the_requester(
    started_app: FastAPI, seeded: World
) -> None:
    _, job = await a_rule_job(started_app, seeded)
    mark = await last_event_id(started_app)
    async with job_services(started_app) as services:
        run_id = await services.jobs.run_now(ADMIN, job)
        await started_app.state.scheduler.scheduler.idle()
        shown_view, shown_run = await services.jobs.run(ADMIN, run_id)
        listed = await services.jobs.runs(ADMIN, job, limit=10)

    [run] = await rows(
        started_app,
        "SELECT trigger, outcome, requested_by_user_id, scheduled_for FROM job_run WHERE id = ?",
        (run_id,),
    )
    [request] = await events(started_app, since=mark)
    assert run == {
        "trigger": "manual",
        "outcome": "success",
        "requested_by_user_id": ADMIN.user_id,
        "scheduled_for": None,
    }
    assert (request["action"], request["actor_user_id"], request["environment_id"]) == (
        "job_run_requested",
        ADMIN.user_id,
        seeded.remote_id,
    )
    assert shown_run.id == run_id and shown_view.summary.job.id == job
    assert [r.id for r in listed.items] == [run_id]


async def test_a_dry_run_is_recorded_as_one_and_reaches_a_subsystem_that_supports_it(
    started_app: FastAPI, seeded: World
) -> None:
    _, job = await a_rule_job(started_app, seeded)
    async with job_services(started_app) as services:
        run_id = await services.jobs.run_now(ADMIN, job, dry_run=True)
        await started_app.state.scheduler.scheduler.idle()

    [run] = await rows(started_app, "SELECT trigger, outcome FROM job_run WHERE id = ?", (run_id,))
    assert run == {"trigger": "dry_run", "outcome": "success"}


async def test_a_dry_run_of_a_job_whose_subsystem_cannot_do_one_is_refused_and_starts_nothing(
    started_app: FastAPI,
) -> None:
    job = await refresher_job_id(started_app)
    async with job_services(started_app) as services:
        with pytest.raises(JobNotRunnable) as refused:
            await services.jobs.run_now(ADMIN, job, dry_run=True)

    assert refused.value.reason == "dry_run_unsupported"
    assert isinstance(refused.value, SchedulerBusy)
    assert await rows(started_app, "SELECT id FROM job_run WHERE trigger = 'dry_run'") == []


async def test_a_manual_run_and_a_dry_run_are_refused_at_the_subsystem_cap(
    started_app: FastAPI, seeded: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, job = await a_rule_job(started_app, seeded)
    monkeypatch.setattr(
        started_app.state.scheduler.handlers.get(RESTARTER), "max_concurrent_runs", 0
    )
    async with job_services(started_app) as services:
        with pytest.raises(SchedulerBusy, match="subsystem"):
            await services.jobs.run_now(ADMIN, job)
        with pytest.raises(SchedulerBusy, match="subsystem"):
            await services.jobs.run_now(ADMIN, job, dry_run=True)

    assert (
        await rows(started_app, "SELECT id FROM job_run WHERE scheduled_job_id = ?", (job,)) == []
    )


async def test_running_a_job_now_is_refused_with_the_reason_it_cannot_run(
    started_app: FastAPI, seeded: World
) -> None:
    rule, job = await a_rule_job(started_app, seeded, enabled=False)
    async with job_services(started_app) as services:
        with pytest.raises(JobNotRunnable) as disabled_rule:
            await services.jobs.run_now(ADMIN, job)
        await services.rules.enable(ADMIN, rule)
        await execute(started_app, "UPDATE scheduled_job SET is_enabled = 0 WHERE id = ?", (job,))
        with pytest.raises(JobNotRunnable) as auto_disabled:
            await services.jobs.run_now(ADMIN, job)
        await execute(started_app, "UPDATE scheduled_job SET is_enabled = 1 WHERE id = ?", (job,))
        await services.jobs.set_subsystem_enabled(ADMIN, RESTARTER, enabled=False)
        with pytest.raises(JobNotRunnable) as switched_off:
            await services.jobs.run_now(ADMIN, job)
        await services.jobs.set_subsystem_enabled(ADMIN, RESTARTER, enabled=True)
        await execute(
            started_app,
            "INSERT INTO job_run (scheduled_job_id, trigger, started_at, heartbeat_at) "
            "VALUES (?, 'schedule', '2030-01-01T00:00:00Z', '2030-01-01T00:00:00Z')",
            (job,),
        )
        with pytest.raises(JobNotRunnable) as running:
            await services.jobs.run_now(ADMIN, job)

    assert [e.value.reason for e in (disabled_rule, auto_disabled, switched_off, running)] == [
        "rule_disabled",
        "auto_disabled",
        "subsystem_disabled",
        "running",
    ]


async def test_a_job_disabled_after_failures_is_enabled_again_with_its_counter_reset(
    started_app: FastAPI, seeded: World
) -> None:
    _, job = await a_rule_job(started_app, seeded)
    await execute(
        started_app,
        "UPDATE scheduled_job SET is_enabled = 0, next_run_at = NULL, consecutive_failures = ? "
        "WHERE id = ?",
        (THRESHOLD_FAILURES, job),
    )
    mark = await last_event_id(started_app)
    async with job_services(started_app) as services:
        before = await services.jobs.detail(ADMIN, job)
        await services.jobs.enable(ADMIN, job)
        after = await services.jobs.detail(ADMIN, job)
        await services.jobs.enable(ADMIN, job)

    [request] = await events(started_app, since=mark)
    stored = (
        await rows(
            started_app,
            "SELECT consecutive_failures, next_run_at FROM scheduled_job WHERE id = ?",
            (job,),
        )
    )[0]
    assert (before.state, after.state) == ("auto_disabled", "scheduled")
    assert (stored["consecutive_failures"], stored["next_run_at"] is not None) == (0, True)
    assert (request["action"], request["actor_user_id"]) == ("scheduled_job_enabled", ADMIN.user_id)


async def test_switching_a_subsystem_off_and_on_reschedules_its_jobs_and_records_both(
    started_app: FastAPI, seeded: World
) -> None:
    _, job = await a_rule_job(started_app, seeded)
    withhold_handler(started_app, UNAVAILABLE)
    mark = await last_event_id(started_app)
    async with job_services(started_app) as services:
        await services.jobs.set_subsystem_enabled(ADMIN, RESTARTER, enabled=False)
        off_state = (await services.jobs.detail(ADMIN, job)).state
        off_next = (
            await rows(started_app, "SELECT next_run_at FROM scheduled_job WHERE id = ?", (job,))
        )[0]
        await services.jobs.set_subsystem_enabled(ADMIN, RESTARTER, enabled=False)
        await services.jobs.set_subsystem_enabled(ADMIN, RESTARTER, enabled=True)
        on_state = (await services.jobs.detail(ADMIN, job)).state
        on_next = (
            await rows(started_app, "SELECT next_run_at FROM scheduled_job WHERE id = ?", (job,))
        )[0]
        listing = {
            name: (on, available) for name, on, available in await services.jobs.subsystems(ADMIN)
        }
        count = await services.jobs.subsystem_job_count(ADMIN, RESTARTER)

    recorded = await events(started_app, since=mark)
    assert (off_state, off_next["next_run_at"]) == ("subsystem_disabled", None)
    assert (on_state, on_next["next_run_at"] is not None) == ("scheduled", True)
    assert [(e["action"], e["target_type"], e["target_id"]) for e in recorded] == [
        ("subsystem_disabled", "subsystem", RESTARTER),
        ("subsystem_enabled", "subsystem", RESTARTER),
    ]
    assert listing[RESTARTER] == (True, True) and listing[UNAVAILABLE] == (True, False)
    assert count == 1


async def test_an_unknown_subsystem_cannot_be_switched_or_counted(started_app: FastAPI) -> None:
    async with job_services(started_app) as services:
        with pytest.raises(ResourceNotFound):
            await services.jobs.set_subsystem_enabled(ADMIN, "nope", enabled=False)
        with pytest.raises(ResourceNotFound):
            await services.jobs.subsystem_job_count(ADMIN, "nope")


async def test_no_event_across_the_job_and_rule_services_names_the_scheduler(
    started_app: FastAPI, seeded: World
) -> None:
    mark = await last_event_id(started_app)
    _, job = await a_rule_job(started_app, seeded)
    async with job_services(started_app) as services:
        await services.jobs.run_now(ADMIN, job)
        await started_app.state.scheduler.scheduler.idle()
        await services.jobs.set_subsystem_enabled(ADMIN, RESTARTER, enabled=False)

    assert {e["actor_subsystem"] for e in await events(started_app, since=mark)} == {None}


@dataclass(frozen=True)
class FakeRule:
    is_enabled: bool = True
    paused_until: str | None = None


def a_job(**changes: object) -> ScheduledJob:
    job = ScheduledJob(
        id=1,
        subsystem=RESTARTER,
        rule_id=1,
        environment_id=None,
        schedule_kind="interval",
        schedule_expression="60",
        time_zone="UTC",
        next_run_at=stamp(at(60)),
        overrun_policy="skip",
        catch_up=False,
        is_enabled=True,
        paused_until=None,
        consecutive_failures=0,
        failure_threshold=5,
        created_at=stamp(START),
        updated_at=stamp(START),
    )
    return replace(job, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("job", "rule", "gates", "expected"),
    [
        pytest.param(a_job(), None, {}, "scheduled", id="scheduled"),
        pytest.param(a_job(), None, {"available": False}, "unavailable", id="no-handler"),
        pytest.param(
            a_job(), None, {"subsystem_enabled": False}, "subsystem_disabled", id="switched-off"
        ),
        pytest.param(a_job(is_enabled=False), None, {}, "auto_disabled", id="auto-disabled"),
        pytest.param(a_job(), FakeRule(is_enabled=False), {}, "rule_disabled", id="rule-disabled"),
        pytest.param(a_job(), None, {"running": True}, "running", id="running"),
        pytest.param(a_job(paused_until=stamp(at(7200))), None, {}, "paused", id="job-paused"),
        pytest.param(
            a_job(), FakeRule(paused_until=stamp(at(7200))), {}, "paused", id="rule-paused"
        ),
        pytest.param(
            a_job(next_run_at=None), None, {}, "unscheduled", id="enabled-with-nothing-scheduled"
        ),
        pytest.param(
            a_job(schedule_kind="webhook", schedule_expression="", next_run_at=None),
            None,
            {},
            "on_call",
            id="on-call-with-no-call-armed",
        ),
        pytest.param(
            a_job(schedule_kind="webhook", schedule_expression=""),
            None,
            {},
            "scheduled",
            id="on-call-with-a-call-armed",
        ),
        pytest.param(
            a_job(schedule_kind="webhook", schedule_expression="", next_run_at=None),
            FakeRule(is_enabled=False),
            {},
            "rule_disabled",
            id="on-call-under-a-disabled-rule",
        ),
        pytest.param(
            a_job(is_enabled=False),
            FakeRule(is_enabled=False),
            {"available": False},
            "unavailable",
            id="the-most-limiting-gate-wins",
        ),
    ],
)
def test_a_jobs_state_names_its_most_limiting_gate(
    job: ScheduledJob, rule: FakeRule | None, gates: dict[str, bool], expected: str
) -> None:
    options = {"running": False, "subsystem_enabled": True, "available": True} | gates

    state = job_state(job, rule, now=at(60), **options)

    assert state == expected


async def test_the_pruner_owns_no_job_so_it_is_not_listed_switched_or_counted(
    started_app: FastAPI,
) -> None:
    async with job_services(started_app) as services:
        listed = {name for name, _on, _available in await services.jobs.subsystems(ADMIN)}
        with pytest.raises(ResourceNotFound):
            await services.jobs.set_subsystem_enabled(ADMIN, PRUNER, enabled=False)
        with pytest.raises(ResourceNotFound):
            await services.jobs.subsystem_job_count(ADMIN, PRUNER)

    assert PRUNER not in listed
    assert "activity-log-exporter" in listed


async def record_a_failed_run(app: FastAPI, job: int) -> int:
    async with app.state.database.session() as db:
        runs = JobRunRepository(db, app.state.auth.clock)
        run = await runs.start(job, trigger="manual", scheduled_for=None)
        await runs.finish(run.id, outcome="failure", reason="host_down")
    return run.id


async def test_a_job_that_has_never_run_shows_no_last_run(
    started_app: FastAPI, seeded: World
) -> None:
    _, job = await a_rule_job(started_app, seeded)
    async with job_services(started_app) as services:
        view = await services.jobs.detail(ADMIN, job)

    assert (view.last_run_id, view.last_run_at, view.last_run_outcome) == (None, None, None)


async def test_a_last_run_stored_on_the_job_is_shown_with_no_run_to_link(
    started_app: FastAPI, seeded: World
) -> None:
    _, job = await a_rule_job(started_app, seeded)
    await execute(started_app, STORE_LAST_RUN, (LONG_AGO, job))
    async with job_services(started_app) as services:
        view = await services.jobs.detail(ADMIN, job)
        listed = next(
            item
            for item in (await services.jobs.page(ADMIN, limit=50)).items
            if item.summary.job.id == job
        )

    assert (view.last_run_id, view.last_run_at, view.last_run_outcome) == (
        None,
        LONG_AGO,
        "success",
    )
    assert (listed.last_run_id, listed.last_run_at) == (None, LONG_AGO)


async def test_a_recorded_run_newer_than_the_stored_last_run_is_the_last_run(
    started_app: FastAPI, seeded: World
) -> None:
    _, job = await a_rule_job(started_app, seeded)
    await execute(started_app, STORE_LAST_RUN, (LONG_AGO, job))
    run_id = await record_a_failed_run(started_app, job)
    async with job_services(started_app) as services:
        view = await services.jobs.detail(ADMIN, job)

    assert (view.last_run_id, view.last_run_outcome) == (run_id, "failure")


async def test_a_stored_last_run_newer_than_the_recorded_runs_is_the_last_run(
    started_app: FastAPI, seeded: World
) -> None:
    _, job = await a_rule_job(started_app, seeded)
    await execute(started_app, STORE_LAST_RUN, (FAR_FUTURE, job))
    await record_a_failed_run(started_app, job)
    async with job_services(started_app) as services:
        view = await services.jobs.detail(ADMIN, job)

    assert (view.last_run_id, view.last_run_at, view.last_run_outcome) == (
        None,
        FAR_FUTURE,
        "success",
    )


async def test_the_refreshers_scheduled_start_up_run_is_its_last_run_though_it_left_no_row(
    started_app: FastAPI,
) -> None:
    job = await refresher_job_id(started_app)
    async with job_services(started_app) as services:
        view = await services.jobs.detail(ADMIN, job)
        history = await services.jobs.runs(ADMIN, job, limit=10)

    assert history.items == []
    assert (view.last_run_id, view.last_run_outcome) == (None, "success")
    assert view.last_run_at is not None


async def on_call_job(app: FastAPI, world: World) -> tuple[int, int]:
    """Create an enabled rule whose job runs when called; return the rule and job ids."""
    rule, job = await a_rule_job(app, world)
    await execute(
        app,
        "UPDATE scheduled_job SET schedule_kind = 'webhook', schedule_expression = '', "
        "next_run_at = NULL WHERE id = ?",
        (job,),
    )
    return rule, job


async def test_an_on_call_job_reads_when_called_and_waits_for_calls(
    started_app: FastAPI, seeded: World
) -> None:
    _, job = await on_call_job(started_app, seeded)

    async with job_services(started_app) as services:
        view = await services.jobs.detail(ADMIN, job)

    assert (view.schedule, view.state) == ("When called", "on_call")


async def test_switching_a_subsystem_off_clears_an_armed_call_and_on_does_not_restore_it(
    started_app: FastAPI, seeded: World
) -> None:
    _, job = await on_call_job(started_app, seeded)
    await execute(
        started_app,
        "UPDATE scheduled_job SET next_run_at = ? WHERE id = ?",
        (stamp(at(60)), job),
    )
    query = "SELECT next_run_at FROM scheduled_job WHERE id = ?"
    async with job_services(started_app) as services:
        await services.jobs.set_subsystem_enabled(ADMIN, RESTARTER, enabled=False)
        off = (await rows(started_app, query, (job,)))[0]["next_run_at"]
        await services.jobs.set_subsystem_enabled(ADMIN, RESTARTER, enabled=True)
        on = (await rows(started_app, query, (job,)))[0]["next_run_at"]
        state = (await services.jobs.detail(ADMIN, job)).state

    assert (off, on, state) == (None, None, "on_call")


async def arm_directly(app: FastAPI, job: int) -> None:
    await execute(
        app, "UPDATE scheduled_job SET next_run_at = ? WHERE id = ?", (stamp(at(60)), job)
    )


async def test_pausing_and_disabling_the_rule_clear_an_armed_call_and_resuming_does_not_restore_it(
    started_app: FastAPI, seeded: World
) -> None:
    rule, job = await on_call_job(started_app, seeded)
    query = "SELECT next_run_at FROM scheduled_job WHERE id = ?"
    async with job_services(started_app) as services:
        await arm_directly(started_app, job)
        await services.rules.pause(ADMIN, rule, duration="1h")
        paused = (await rows(started_app, query, (job,)))[0]["next_run_at"]
        await services.rules.resume(ADMIN, rule)
        resumed = (await rows(started_app, query, (job,)))[0]["next_run_at"]
        await arm_directly(started_app, job)
        await services.rules.disable(ADMIN, rule)
        disabled = (await rows(started_app, query, (job,)))[0]["next_run_at"]

    assert (paused, resumed, disabled) == (None, None, None)
