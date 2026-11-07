"""The scheduler's repositories: jobs, runs, rules, and the activity reads built for them."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.db.engine import Database, Session
from app.db.repositories.activity_event import ActivityEventRepository
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.errors import DuplicateError, MissingReferenceError
from app.db.repositories.job_run import (
    KEEP_RUNS_PER_JOB,
    MAX_RUN_DETAIL_KEYS,
    MAX_RUN_DETAIL_TEXT,
    MAX_RUN_MESSAGE,
    JobRunRepository,
)
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.logging import REDACTED
from tests.support import present

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
SECRET = "hunter2"
RESTARTER = "stack-restarter"
REFRESHER = "environment-snapshot-refresher"


def stamp(seconds: float) -> str:
    return (NOW + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def clock() -> datetime:
    return NOW


@dataclass
class Repos:
    session: Session
    rules: AutomationRuleRepository
    jobs: ScheduledJobRepository
    runs: JobRunRepository
    events: ActivityEventRepository
    owner: int
    other: int
    environment: int

    async def rule(
        self, name: str = "r", *, owner: int | None = None, subsystem: str = RESTARTER
    ) -> int:
        rule = await self.rules.insert(
            name=name,
            subsystem=subsystem,
            environment_id=None,
            target_selector="",
            parameters="{}",
            is_enabled=False,
            created_by_user_id=self.owner if owner is None else owner,
        )
        return rule.id

    async def job(
        self,
        *,
        rule_id: int | None = None,
        subsystem: str = RESTARTER,
        next_run_at: str | None = None,
        environment_id: int | None = None,
        threshold: int | None = 5,
    ) -> int:
        job = await self.jobs.insert(
            subsystem=subsystem,
            rule_id=rule_id,
            schedule_kind="interval",
            schedule_expression="60",
            time_zone="UTC",
            overrun_policy="skip",
            catch_up=False,
            failure_threshold=threshold,
            next_run_at=next_run_at,
            environment_id=environment_id,
        )
        return job.id

    async def rule_job(self, name: str = "r", **options: Any) -> int:
        return await self.job(rule_id=await self.rule(name), **options)

    async def finished_runs(self, job: int, count: int) -> list[int]:
        ids = []
        for _ in range(count):
            run = await self.runs.start(job, trigger="schedule", scheduled_for=None)
            await self.runs.finish(run.id, outcome="success")
            ids.append(run.id)
        return ids

    async def event_for_run(self, run_id: int) -> None:
        await self.session.execute(
            "INSERT INTO activity_event (category, actor_type, actor_subsystem, rule_id, "
            "job_run_id, target_type, action, result, occurred_at) VALUES ('automation', "
            "'subsystem', 'stack-restarter', 1, ?, 'x', 'a', 'success', ?)",
            (run_id, stamp(0)),
        )


@pytest.fixture
async def repos(session: Session) -> Repos:
    users: list[int] = []
    for name in ("owner", "other"):
        result = await session.execute(
            "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
            "VALUES (?, 'x', 'administrator', 1, ?)",
            (name, stamp(0)),
        )
        users.append(present(result.last_row_id))
    environment = await session.execute(
        "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
        "VALUES ('prod', 'unix_socket', '/run/docker.sock', ?, ?)",
        (stamp(0), stamp(0)),
    )
    return Repos(
        session=session,
        rules=AutomationRuleRepository(session, clock),
        jobs=ScheduledJobRepository(session, clock),
        runs=JobRunRepository(session, clock),
        events=ActivityEventRepository(session, clock),
        owner=users[0],
        other=users[1],
        environment=present(environment.last_row_id),
    )


async def test_a_new_job_is_enabled_with_no_failures_and_found_by_rule_or_subsystem(
    repos: Repos,
) -> None:
    rule_id = await repos.rule()
    in_rule = await repos.job(rule_id=rule_id, next_run_at=stamp(60))
    rule_less = await repos.job(subsystem=REFRESHER, threshold=None)

    job = await repos.jobs.get(in_rule)
    assert job is not None
    assert (job.is_enabled, job.consecutive_failures, job.time_zone) == (True, 0, "UTC")
    assert (job.next_run_at, job.failure_threshold, job.catch_up) == (stamp(60), 5, False)
    assert (await repos.jobs.get_for_rule(rule_id)) == job
    found = await repos.jobs.get_infrastructure(REFRESHER)
    assert found is not None and found.id == rule_less
    assert await repos.jobs.get_infrastructure(RESTARTER) is None
    assert await repos.jobs.get(9999) is None


async def test_a_rule_cannot_have_two_jobs_nor_a_subsystem_two_rule_less_jobs(
    repos: Repos,
) -> None:
    rule_id = await repos.rule()
    await repos.job(rule_id=rule_id)
    await repos.job(subsystem=REFRESHER)

    with pytest.raises(DuplicateError):
        await repos.job(rule_id=rule_id)
    with pytest.raises(DuplicateError):
        await repos.job(subsystem=REFRESHER)


async def test_a_job_for_a_rule_that_does_not_exist_is_refused(repos: Repos) -> None:
    with pytest.raises(MissingReferenceError):
        await repos.job(rule_id=9999)


async def test_the_earliest_next_run_ignores_disabled_and_unscheduled_jobs(repos: Repos) -> None:
    sooner = await repos.rule_job("sooner", next_run_at=stamp(30))
    await repos.rule_job("later", next_run_at=stamp(90))
    await repos.rule_job("never", next_run_at=None)
    disabled = await repos.rule_job("disabled", next_run_at=stamp(10))
    await repos.jobs.disable(disabled)

    earliest = await repos.jobs.earliest_next_run()
    after = await repos.jobs.earliest_next_run(after=stamp(30))

    assert sooner and earliest == stamp(30)
    assert after == stamp(90)


async def test_due_jobs_come_earliest_first_and_leave_out_the_future_unscheduled_and_disabled(
    repos: Repos,
) -> None:
    second = await repos.rule_job("second", next_run_at=stamp(20))
    first = await repos.rule_job("first", next_run_at=stamp(10))
    await repos.rule_job("future", next_run_at=stamp(500))
    await repos.rule_job("never", next_run_at=None)
    disabled = await repos.rule_job("disabled", next_run_at=stamp(5))
    await repos.jobs.disable(disabled)

    due = await repos.jobs.due(stamp(60), limit=10)

    assert [job.id for job in due] == [first, second]


async def test_due_jobs_resume_behind_a_position_even_among_equal_fire_times(
    repos: Repos,
) -> None:
    ids = [await repos.rule_job(f"r{n}", next_run_at=stamp(10)) for n in range(5)]
    last = await repos.rule_job("last", next_run_at=stamp(20))

    first_page = await repos.jobs.due(stamp(60), limit=2)
    position = (str(first_page[-1].next_run_at), first_page[-1].id)
    rest = await repos.jobs.due(stamp(60), limit=10, after=position)

    assert [job.id for job in first_page] == ids[:2]
    assert [job.id for job in rest] == [*ids[2:], last]


@pytest.mark.parametrize("limit", [0, MAX_PAGE_SIZE + 1])
async def test_due_refuses_a_limit_outside_the_page_bounds(repos: Repos, limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        await repos.jobs.due(stamp(0), limit=limit)


async def test_a_claim_advances_the_job_once_even_when_two_sessions_race(
    database: Database, repos: Repos
) -> None:
    job = await repos.rule_job(next_run_at=stamp(60))
    async with database.session() as rival:
        contenders = [repos.jobs, ScheduledJobRepository(rival, clock)]

        outcomes = await asyncio.gather(
            *(c.claim(job, due_at=stamp(60), next_run_at=stamp(120)) for c in contenders)
        )

    assert sorted(outcomes) == [False, True]
    assert (await repos.jobs.get(job)).next_run_at == stamp(120)  # type: ignore[union-attr]


async def test_a_claim_fails_for_a_different_due_time_or_a_disabled_job(repos: Repos) -> None:
    job = await repos.rule_job(next_run_at=stamp(60))

    wrong_time = await repos.jobs.claim(job, due_at=stamp(61), next_run_at=stamp(120))
    await repos.jobs.disable(job)
    disabled = await repos.jobs.claim(job, due_at=stamp(60), next_run_at=stamp(120))

    assert (wrong_time, disabled) == (False, False)


async def test_failures_are_counted_and_a_success_clears_them(repos: Repos) -> None:
    job = await repos.rule_job(next_run_at=stamp(60))

    await repos.jobs.record_failure(job, 3, stamp(200))
    failed = await repos.jobs.get(job)
    await repos.jobs.record_success(job, stamp(300))
    recovered = await repos.jobs.get(job)

    assert failed is not None and recovered is not None
    assert (failed.consecutive_failures, failed.next_run_at) == (3, stamp(200))
    assert (recovered.consecutive_failures, recovered.next_run_at) == (0, stamp(300))


async def test_a_negative_failure_count_is_refused(repos: Repos) -> None:
    job = await repos.rule_job()

    with pytest.raises(ValueError, match="negative"):
        await repos.jobs.record_failure(job, -1, None)


async def test_disabling_clears_the_next_fire_and_enabling_resets_the_counter(
    repos: Repos,
) -> None:
    job = await repos.rule_job(next_run_at=stamp(60))
    await repos.jobs.record_failure(job, 5, stamp(60))

    await repos.jobs.disable(job)
    disabled = await repos.jobs.get(job)
    await repos.jobs.enable(job, stamp(400))
    enabled = await repos.jobs.get(job)

    assert disabled is not None and enabled is not None
    assert (disabled.is_enabled, disabled.next_run_at) == (False, None)
    assert (enabled.is_enabled, enabled.consecutive_failures, enabled.next_run_at) == (
        True,
        0,
        stamp(400),
    )


async def test_a_pause_and_a_new_schedule_are_stored_with_the_fire_they_imply(
    repos: Repos,
) -> None:
    job = await repos.rule_job(next_run_at=stamp(60))

    await repos.jobs.set_paused(job, stamp(3600), stamp(3660))
    paused = await repos.jobs.get(job)
    await repos.jobs.update_schedule(
        job,
        schedule_kind="cron",
        schedule_expression="0 3 * * *",
        time_zone="Europe/London",
        overrun_policy="queue",
        catch_up=True,
        failure_threshold=2,
        next_run_at=stamp(7200),
    )
    updated = await repos.jobs.get(job)

    assert paused is not None and updated is not None
    assert (paused.paused_until, paused.next_run_at) == (stamp(3600), stamp(3660))
    assert (updated.schedule_kind, updated.time_zone, updated.overrun_policy) == (
        "cron",
        "Europe/London",
        "queue",
    )
    assert (updated.catch_up, updated.failure_threshold, updated.next_run_at) == (
        True,
        2,
        stamp(7200),
    )


async def test_a_subsystems_jobs_are_listed_and_counted(repos: Repos) -> None:
    ids = [await repos.rule_job(f"r{n}") for n in range(3)]
    await repos.job(subsystem=REFRESHER)

    assert await repos.jobs.ids_for_subsystem(RESTARTER) == ids
    assert await repos.jobs.count_for_subsystem(RESTARTER) == 3
    assert await repos.jobs.count_for_subsystem("backup-syncer") == 0


async def test_jobs_are_walked_by_id_in_pages_for_recovery(repos: Repos) -> None:
    ids = [await repos.rule_job(f"r{n}") for n in range(4)]

    page = await repos.jobs.page_after(ids[0], limit=2)
    rest = await repos.jobs.page_after(page[-1].id, limit=10)

    assert [job.id for job in page] == ids[1:3]
    assert [job.id for job in rest] == ids[3:]


async def test_the_owner_of_a_jobs_rule_is_found_and_absent_for_a_rule_less_job(
    repos: Repos,
) -> None:
    owned = await repos.rule_job()
    rule_less = await repos.job(subsystem=REFRESHER)
    orphaned = await repos.job(rule_id=await repos.rule("orphan"))
    await repos.session.execute("DELETE FROM user WHERE id = ?", (repos.owner,))

    assert await repos.jobs.rule_owner(rule_less) is None
    assert await repos.jobs.rule_owner(owned) is None
    assert await repos.jobs.rule_owner(orphaned) is None


async def test_the_owner_of_a_jobs_rule_is_the_user_who_created_it(repos: Repos) -> None:
    job = await repos.job(rule_id=await repos.rule(owner=repos.other))

    assert await repos.jobs.rule_owner(job) == repos.other


async def test_a_summary_joins_the_rule_the_environment_and_the_latest_run(repos: Repos) -> None:
    rule_id = await repos.rule("Nightly")
    job = await repos.job(rule_id=rule_id, environment_id=repos.environment)
    await repos.rules.set_enabled(rule_id, True)
    await repos.rules.set_paused(rule_id, stamp(900))
    run = await repos.runs.start(job, trigger="manual", scheduled_for=None)

    summary = await repos.jobs.summary(job)

    assert summary is not None
    assert (summary.rule_name, summary.rule_owner_id) == ("Nightly", repos.owner)
    assert summary.rule_gate is not None
    assert (summary.rule_gate.is_enabled, summary.rule_gate.paused_until) == (True, stamp(900))
    assert (summary.environment_id, summary.environment_name) == (repos.environment, "prod")
    assert summary.last_run is not None and summary.last_run.id == run.id
    assert await repos.jobs.summary(9999) is None


async def test_a_rule_less_summary_has_no_rule_and_no_run(repos: Repos) -> None:
    job = await repos.job(subsystem=REFRESHER)

    summary = await repos.jobs.summary(job)

    assert summary is not None
    assert (summary.rule_name, summary.rule_gate, summary.last_run) == (None, None, None)


async def test_job_summaries_page_by_id_and_filter_by_subsystem(repos: Repos) -> None:
    ids = [await repos.rule_job(f"r{n}") for n in range(3)]
    refresher = await repos.job(subsystem=REFRESHER)

    first = await repos.jobs.page(limit=2)
    second = await repos.jobs.page(limit=2, cursor=first.next_cursor)
    only = await repos.jobs.page(limit=10, subsystem=REFRESHER)

    assert [s.job.id for s in first.items] == ids[:2]
    assert [s.job.id for s in second.items] == [ids[2], refresher]
    assert second.next_cursor is None
    assert [s.job.id for s in only.items] == [refresher]


async def test_a_started_run_is_in_flight_with_its_heartbeat_and_requester(repos: Repos) -> None:
    job = await repos.rule_job()

    run = await repos.runs.start(
        job, trigger="manual", scheduled_for=None, requested_by_user_id=repos.other
    )

    assert (run.outcome, run.finished_at, run.heartbeat_at) == (None, None, stamp(0))
    assert (run.trigger, run.requested_by_user_id, run.started_at) == (
        "manual",
        repos.other,
        stamp(0),
    )
    assert run.detail == {}


async def test_a_run_finishes_once_and_a_second_finish_changes_nothing(repos: Repos) -> None:
    job = await repos.rule_job()
    run = await repos.runs.start(job, trigger="schedule", scheduled_for=stamp(0))

    first = await repos.runs.finish(run.id, outcome="success", message="done", detail={"n": 1})
    second = await repos.runs.finish(run.id, outcome="failure", reason="late")
    missing = await repos.runs.finish(9999, outcome="success")

    stored = await repos.runs.get(run.id)
    assert (first, second, missing) == (True, False, False)
    assert stored is not None
    assert (stored.outcome, stored.message, stored.detail) == ("success", "done", {"n": 1})
    assert stored.finished_at == stamp(0)


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        pytest.param("failure", None, id="failure-needs-a-reason"),
        pytest.param("skipped", None, id="skipped-needs-a-reason"),
        pytest.param("interrupted", None, id="interrupted-needs-a-reason"),
        pytest.param("failure", "Not A Code", id="reason-not-a-machine-code"),
        pytest.param("success", "UPPER", id="reason-checked-on-success-too"),
        pytest.param("failure", "x" * 65, id="reason-too-long"),
    ],
)
async def test_finishing_with_an_outcome_that_lacks_a_valid_reason_is_refused(
    repos: Repos, outcome: str, reason: str | None
) -> None:
    run = await repos.runs.start(await repos.rule_job(), trigger="schedule", scheduled_for=None)

    with pytest.raises(ValueError, match="reason"):
        await repos.runs.finish(run.id, outcome=outcome, reason=reason)  # type: ignore[arg-type]


async def test_message_and_detail_are_scrubbed_then_cut_to_their_bounds(repos: Repos) -> None:
    run = await repos.runs.start(await repos.rule_job(), trigger="schedule", scheduled_for=None)
    padded = "." * (MAX_RUN_MESSAGE * 2) + f" password={SECRET}"

    await repos.runs.finish(
        run.id,
        outcome="failure",
        reason="host_down",
        message=f"connect failed: password={SECRET}" + padded,
        detail={"note": "x" * (MAX_RUN_DETAIL_TEXT * 2), "password": SECRET, "flag": True},
    )

    stored = await repos.runs.get(run.id)
    assert stored is not None and stored.message is not None
    assert SECRET not in stored.message and len(stored.message) <= MAX_RUN_MESSAGE
    assert len(str(stored.detail["note"])) == MAX_RUN_DETAIL_TEXT
    assert (stored.detail["password"], stored.detail["flag"]) == (REDACTED, True)


@pytest.mark.parametrize(
    "detail",
    [
        pytest.param({"nested": {"a": 1}}, id="nested"),
        pytest.param({"listed": [1]}, id="list"),
        pytest.param({"fraction": 1.5}, id="float"),
        pytest.param({f"k{n}": n for n in range(MAX_RUN_DETAIL_KEYS + 1)}, id="too-many-keys"),
    ],
)
async def test_run_detail_that_is_not_flat_and_bounded_is_refused(
    repos: Repos, detail: dict[str, Any]
) -> None:
    run = await repos.runs.start(await repos.rule_job(), trigger="schedule", scheduled_for=None)

    with pytest.raises(ValueError):
        await repos.runs.finish(run.id, outcome="success", detail=detail)


async def test_a_skipped_run_is_recorded_already_finished(repos: Repos) -> None:
    job = await repos.rule_job()

    run = await repos.runs.record_skipped(
        job,
        trigger="schedule",
        scheduled_for=stamp(60),
        reason="overrun",
        message="The previous run was still running.",
        detail={"running_run_id": 3},
    )

    assert (run.outcome, run.reason, run.finished_at) == ("skipped", "overrun", stamp(0))
    assert run.detail == {"running_run_id": 3}


async def test_a_skipped_run_needs_a_machine_reason(repos: Repos) -> None:
    job = await repos.rule_job()

    with pytest.raises(ValueError, match="reason"):
        await repos.runs.record_skipped(
            job, trigger="schedule", scheduled_for=None, reason="Not A Code", message="x"
        )


async def test_a_heartbeat_stamps_runs_in_flight_and_leaves_finished_ones(repos: Repos) -> None:
    job = await repos.rule_job()
    running = await repos.runs.start(job, trigger="schedule", scheduled_for=None)
    [done] = await repos.finished_runs(job, 1)

    await repos.runs.heartbeat([running.id, done, running.id])

    assert (await repos.runs.get(running.id)).heartbeat_at == stamp(0)  # type: ignore[union-attr]


async def test_a_heartbeat_covers_more_runs_than_one_statement_takes(
    database: Database, repos: Repos
) -> None:
    job = await repos.rule_job()
    ids = []
    for _ in range(MAX_PAGE_SIZE + 20):
        run = await repos.runs.start(job, trigger="schedule", scheduled_for=None)
        ids.append(run.id)
    later = JobRunRepository(repos.session, lambda: NOW + timedelta(minutes=5))

    await later.heartbeat(ids)

    stamps = await repos.session.fetch_limited(
        "SELECT DISTINCT heartbeat_at FROM job_run", (), limit=10
    )
    assert [row["heartbeat_at"] for row in stamps] == [stamp(300)]


async def test_runs_in_flight_are_listed_oldest_first_and_finished_ones_are_not(
    repos: Repos,
) -> None:
    job = await repos.rule_job()
    first = await repos.runs.start(job, trigger="schedule", scheduled_for=None)
    await repos.finished_runs(job, 1)
    second = await repos.runs.start(job, trigger="manual", scheduled_for=None)

    in_flight = await repos.runs.in_flight()

    assert [run.id for run in in_flight] == [first.id, second.id]


async def test_the_latest_run_and_the_history_page_come_newest_first(repos: Repos) -> None:
    job = await repos.rule_job()
    ids = await repos.finished_runs(job, 3)

    latest = await repos.runs.latest(job)
    first = await repos.runs.page(job, limit=2)
    second = await repos.runs.page(job, limit=2, cursor=first.next_cursor)

    assert latest is not None and latest.id == ids[-1]
    assert [run.id for run in first.items] == [ids[2], ids[1]]
    assert [run.id for run in second.items] == [ids[0]]
    assert second.next_cursor is None
    assert await repos.runs.latest(9999) is None


async def test_pruning_keeps_the_newest_finished_runs(repos: Repos) -> None:
    job = await repos.rule_job()
    ids = await repos.finished_runs(job, 30)

    removed = await repos.runs.prune(job, keep=5)

    remaining = [run.id for run in (await repos.runs.page(job, limit=100)).items]
    assert removed == 25
    assert remaining == ids[-1:-6:-1]


async def test_pruning_never_removes_a_run_in_flight_or_one_an_event_names(repos: Repos) -> None:
    job = await repos.rule_job()
    ids = await repos.finished_runs(job, 10)
    in_flight = await repos.runs.start(job, trigger="schedule", scheduled_for=None)
    await repos.event_for_run(ids[0])

    removed = await repos.runs.prune(job, keep=3)

    remaining = {run.id for run in (await repos.runs.page(job, limit=100)).items}
    assert removed == 6
    assert remaining == {ids[0], *ids[-3:], in_flight.id}


async def test_pruning_works_through_more_runs_than_one_batch(repos: Repos) -> None:
    job = await repos.rule_job()
    await repos.finished_runs(job, KEEP_RUNS_PER_JOB // 2 + 250)

    removed = await repos.runs.prune(job, keep=10)

    assert removed == KEEP_RUNS_PER_JOB // 2 + 240
    assert len((await repos.runs.page(job, limit=100)).items) == 10


async def test_pruning_one_job_leaves_another_jobs_history(repos: Repos) -> None:
    pruned = await repos.rule_job("a")
    other = await repos.rule_job("b")
    await repos.finished_runs(pruned, 8)
    await repos.finished_runs(other, 8)

    await repos.runs.prune(pruned, keep=2)

    assert len((await repos.runs.page(other, limit=100)).items) == 8


async def test_pruning_with_a_keep_below_one_is_refused(repos: Repos) -> None:
    job = await repos.rule_job()
    await repos.finished_runs(job, 2)

    with pytest.raises(ValueError, match="keep"):
        await repos.runs.prune(job, keep=0)


async def test_a_rule_is_created_found_changed_switched_and_paused(repos: Repos) -> None:
    rule = await repos.rules.insert(
        name="Nightly",
        subsystem=RESTARTER,
        environment_id=repos.environment,
        target_selector="web",
        parameters='{"a":1}',
        is_enabled=False,
        created_by_user_id=repos.owner,
    )

    await repos.rules.update(rule.id, name="Weekly", target_selector="db", parameters="{}")
    await repos.rules.set_enabled(rule.id, True)
    await repos.rules.set_paused(rule.id, stamp(900))
    changed = await repos.rules.get(rule.id)

    assert not rule.is_enabled and rule.created_by_user_id == repos.owner
    assert changed is not None
    assert (changed.name, changed.target_selector, changed.parameters) == ("Weekly", "db", "{}")
    assert (changed.is_enabled, changed.paused_until) == (True, stamp(900))
    assert await repos.rules.get(9999) is None


async def test_a_repeated_rule_name_for_one_owner_is_refused_and_a_missing_owner_too(
    repos: Repos,
) -> None:
    await repos.rule("Nightly")

    with pytest.raises(DuplicateError):
        await repos.rule("NIGHTLY")
    with pytest.raises(MissingReferenceError):
        await repos.rule("Other", owner=9999)


@pytest.mark.parametrize(
    ("owner_is_other", "subsystem", "name", "excluding_own", "taken"),
    [
        pytest.param(False, RESTARTER, "NIGHTLY", False, True, id="same-owner-folded"),
        pytest.param(False, RESTARTER, "Straße", False, False, id="not-taken"),
        pytest.param(True, RESTARTER, "Nightly", False, False, id="another-owner"),
        pytest.param(False, "container-updater", "Nightly", False, False, id="another-subsystem"),
        pytest.param(False, RESTARTER, "nightly", True, False, id="excluding-itself"),
    ],
)
async def test_a_name_is_taken_per_owner_and_subsystem_under_full_case_folding(
    repos: Repos, owner_is_other: bool, subsystem: str, name: str, excluding_own: bool, taken: bool
) -> None:
    rule_id = await repos.rule("Nightly")
    owner = repos.other if owner_is_other else repos.owner

    result = await repos.rules.name_taken(
        subsystem, owner, name, excluding=rule_id if excluding_own else None
    )

    assert result is taken


async def test_a_name_that_folds_equal_only_under_unicode_rules_is_taken(repos: Repos) -> None:
    await repos.rule("Straße")

    assert await repos.rules.name_taken(RESTARTER, repos.owner, "STRASSE") is True


async def test_rules_page_by_id_filter_by_subsystem_and_owner_and_count_per_owner(
    repos: Repos,
) -> None:
    mine = [await repos.rule(f"r{n}") for n in range(3)]
    theirs = await repos.rule("t", owner=repos.other, subsystem="container-updater")

    first = await repos.rules.page(limit=2)
    second = await repos.rules.page(limit=2, cursor=first.next_cursor)
    by_subsystem = await repos.rules.page(limit=10, subsystem="container-updater")
    by_owner = await repos.rules.page(limit=10, owner_id=repos.owner)

    assert [r.id for r in first.items] == mine[:2]
    assert [r.id for r in second.items] == [mine[2], theirs]
    assert [r.id for r in by_subsystem.items] == [theirs]
    assert [r.id for r in by_owner.items] == mine
    assert await repos.rules.count_for_owner(repos.owner) == 3
    assert await repos.rules.count_for_owner(repos.other) == 1


async def user_action(
    repos: Repos,
    *,
    target_type: str,
    target_id: str | None,
    seconds: float,
    category: str = "user_action",
) -> None:
    await repos.session.execute(
        "INSERT INTO activity_event (category, actor_type, actor_user_id, environment_id, "
        "target_type, target_id, action, result, occurred_at) "
        "VALUES (?, 'user', ?, ?, ?, ?, 'a', 'success', ?)",
        (category, repos.owner, repos.environment, target_type, target_id, stamp(seconds)),
    )


async def test_events_are_counted_per_run(repos: Repos) -> None:
    job = await repos.rule_job()
    [busy, quiet] = await repos.finished_runs(job, 2)
    await repos.event_for_run(busy)
    await repos.event_for_run(busy)

    assert await repos.events.count_for_run(busy) == 2
    assert await repos.events.count_for_run(quiet) == 0


async def test_recent_user_targets_are_newest_first_since_a_time_and_cut_to_a_limit(
    repos: Repos,
) -> None:
    await user_action(repos, target_type="container", target_id="old", seconds=-4000)
    await user_action(repos, target_type="container", target_id="first", seconds=-30)
    await user_action(repos, target_type="volume", target_id="second", seconds=-10)
    await user_action(
        repos, target_type="setting", target_id="not-user", seconds=-5, category="automation"
    )

    everything = await repos.events.recent_user_targets(stamp(-3600), limit=10)
    newest = await repos.events.recent_user_targets(stamp(-3600), limit=1)

    assert [row[2] for row in everything] == ["second", "first"]
    assert everything[0] == (repos.environment, "volume", "second", stamp(-10))
    assert [row[2] for row in newest] == ["second"]


@pytest.mark.parametrize(
    ("target_type", "stored_id", "asked_id", "found"),
    [
        pytest.param("container", "c" * 64, "c" * 64, True, id="exact"),
        pytest.param("container", "c" * 64, "d" * 64, False, id="another-object"),
        pytest.param("stack", "WebApp", "webapp", True, id="stack-folded"),
        pytest.param("volume", "Data", "data", False, id="other-types-exact"),
    ],
)
async def test_the_latest_user_action_on_an_object_is_found_by_its_identity(
    repos: Repos, target_type: str, stored_id: str, asked_id: str, found: bool
) -> None:
    await user_action(repos, target_type=target_type, target_id=stored_id, seconds=-20)

    at = await repos.events.latest_user_action(
        repos.environment, target_type, asked_id, stamp(-3600)
    )

    assert at == (stamp(-20) if found else None)


async def test_the_latest_user_action_ignores_older_actions_and_other_environments(
    repos: Repos,
) -> None:
    await user_action(repos, target_type="container", target_id="c" * 64, seconds=-5000)

    in_window = await repos.events.latest_user_action(
        repos.environment, "container", "c" * 64, stamp(-3600)
    )
    elsewhere = await repos.events.latest_user_action(
        repos.environment + 1, "container", "c" * 64, stamp(-9000)
    )

    assert (in_window, elsewhere) == (None, None)
