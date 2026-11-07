"""Hourly upkeep: run-history pruning and the retention hook."""

from datetime import datetime, timedelta
from typing import Any

import pytest

from app.config import ENV_PREFIX, load_settings
from app.db.repositories.job_run import KEEP_RUNS_PER_JOB
from app.scheduler.setup import build_scheduler_runtime
from app.services.activity_log import NoRetention, RetentionPolicy, RetentionResult
from app.services.activity_retention import ActivityRetention
from app.services.object_locks import ObjectLockRegistry
from tests.scheduler.support import SEEDED_AT, Rig, ScriptedHandler, at

RETENTION_VAR = f"{ENV_PREFIX}ACTIVITY_RETENTION_DAYS"
RETENTION_DAYS = 30
EXTRA_RUNS = 20
REMOVED_BY_RETENTION = 3
HALF_AN_HOUR = 1800
AN_HOUR = 3600
SLOW_PASS = 1200


class CountingRetention:
    def __init__(self, removed: int = REMOVED_BY_RETENTION) -> None:
        self.applied: list[datetime] = []
        self._removed = removed

    async def apply(self, now: datetime) -> RetentionResult:
        self.applied.append(now)
        return RetentionResult(removed=self._removed)


class SlowRetention:
    def __init__(self, rig: Rig) -> None:
        self._rig = rig

    async def apply(self, now: datetime) -> RetentionResult:
        self._rig.clock.jump(SLOW_PASS)
        return RetentionResult(removed=0)


class FailingRetention:
    async def apply(self, now: datetime) -> RetentionResult:
        raise RuntimeError("retention store unavailable")


def insert_finished_runs(rig: Rig, job: int, count: int) -> list[int]:
    connection = rig.database.connect()
    try:
        connection.execute("BEGIN")
        for _ in range(count):
            connection.execute(
                "INSERT INTO job_run (scheduled_job_id, trigger, started_at, finished_at, "
                "outcome, reason) VALUES (?, 'schedule', ?, ?, 'skipped', 'missed')",
                (job, SEEDED_AT, SEEDED_AT),
            )
        connection.execute("COMMIT")
        return [row["id"] for row in connection.execute("SELECT id FROM job_run ORDER BY id")]
    finally:
        connection.close()


async def test_a_pass_keeps_the_newest_runs_the_in_flight_run_and_a_run_an_event_names(
    rig: Rig,
) -> None:
    rule = await rig.rule("r")
    job = await rig.job(rule_id=rule, next_run=at(3600))
    ids = insert_finished_runs(rig, job, KEEP_RUNS_PER_JOB + EXTRA_RUNS)
    rig.sql(
        "INSERT INTO activity_event (category, actor_type, actor_subsystem, rule_id, job_run_id, "
        "target_type, action, result, occurred_at) VALUES ('automation', 'subsystem', "
        "'stack-restarter', ?, ?, 'x', 'a', 'success', ?)",
        (rule, ids[0], SEEDED_AT),
    )
    in_flight = await rig.start_run(job)
    scheduler = rig.scheduler(ScriptedHandler(), retention=CountingRetention())

    result = await scheduler.housekeeping.run()

    remaining = {row["id"] for row in rig.runs(job)}
    assert result.runs_pruned == EXTRA_RUNS - 1
    assert len(remaining) == KEEP_RUNS_PER_JOB + 2
    assert ids[0] in remaining and in_flight.id in remaining
    assert set(ids[-KEEP_RUNS_PER_JOB:]) <= remaining


async def test_a_pass_applies_the_retention_policy_with_the_clock(rig: Rig) -> None:
    retention = CountingRetention()
    scheduler = rig.scheduler(ScriptedHandler(), retention=retention)

    result = await scheduler.housekeeping.run()

    assert retention.applied == [rig.clock()]
    assert result.events_removed == REMOVED_BY_RETENTION


async def test_a_pass_is_not_due_again_for_an_hour(rig: Rig) -> None:
    scheduler = rig.scheduler(ScriptedHandler(), retention=CountingRetention())
    await scheduler.housekeeping.run()

    early = await scheduler.housekeeping.run_if_due()
    rig.clock.jump(timedelta(hours=1).total_seconds() + 1)
    later = await scheduler.housekeeping.run_if_due()

    assert early is None
    assert later is not None


@pytest.mark.parametrize(
    ("now", "due"),
    [
        pytest.param(at(HALF_AN_HOUR), at(AN_HOUR), id="mid-hour"),
        pytest.param(at(AN_HOUR - 1), at(AN_HOUR), id="last-second-of-the-hour"),
        pytest.param(at(AN_HOUR), at(2 * AN_HOUR), id="on-the-hour"),
    ],
)
async def test_the_next_pass_is_due_at_the_next_top_of_the_hour(
    rig: Rig, now: datetime, due: datetime
) -> None:
    rig.clock.set(now)
    scheduler = rig.scheduler(ScriptedHandler(), retention=CountingRetention())

    await scheduler.housekeeping.run()

    assert scheduler.housekeeping.next_due == due


async def test_a_pass_runs_when_the_hour_turns_and_not_a_second_before(rig: Rig) -> None:
    rig.clock.set(at(HALF_AN_HOUR))
    scheduler = rig.scheduler(ScriptedHandler(), retention=CountingRetention())
    await scheduler.housekeeping.run()
    rig.clock.set(at(AN_HOUR - 1))
    early = await scheduler.housekeeping.run_if_due()
    rig.clock.set(at(AN_HOUR))

    on_the_hour = await scheduler.housekeeping.run_if_due()

    assert early is None
    assert on_the_hour is not None


async def test_a_pass_that_takes_longer_than_usual_does_not_move_the_next_one(rig: Rig) -> None:
    rig.clock.set(at(HALF_AN_HOUR))
    scheduler = rig.scheduler(ScriptedHandler(), retention=SlowRetention(rig))

    await scheduler.housekeeping.run()

    assert rig.clock() == at(HALF_AN_HOUR + SLOW_PASS)
    assert scheduler.housekeeping.next_due == at(AN_HOUR)


async def test_a_failing_retention_policy_is_logged_and_does_not_stop_the_pass(
    rig: Rig, logged: Any
) -> None:
    scheduler = rig.scheduler(ScriptedHandler(), retention=FailingRetention())

    result = await scheduler.housekeeping.run()

    assert result.events_removed == 0
    assert [record["error"] for record in logged("retention_failed")] == ["RuntimeError"]


async def test_the_loop_runs_the_upkeep_when_it_starts(rig: Rig) -> None:
    retention = CountingRetention()
    scheduler = rig.scheduler(ScriptedHandler(), retention=retention)

    await scheduler.start()
    await rig.sleeper.entered.wait()

    assert len(retention.applied) == 1
    assert scheduler.housekeeping.next_due == rig.clock() + timedelta(hours=1)


async def test_the_loop_runs_the_upkeep_again_when_the_hour_turns(rig: Rig) -> None:
    retention = CountingRetention()
    scheduler = rig.scheduler(ScriptedHandler(), retention=retention)
    await scheduler.start()
    await rig.sleeper.entered.wait()
    rig.sleeper.entered.clear()
    rig.clock.set(at(AN_HOUR))

    scheduler.wake()
    await rig.sleeper.entered.wait()

    assert retention.applied == [at(0), at(AN_HOUR)]
    assert rig.sleeper.deadlines[-1] == at(2 * AN_HOUR)


@pytest.mark.parametrize(
    ("days", "policy"),
    [(None, NoRetention), (RETENTION_DAYS, ActivityRetention)],
    ids=["unset", "set"],
)
def test_the_scheduler_keeps_every_event_unless_retention_days_are_configured(
    rig: Rig, env: dict[str, str], days: int | None, policy: type[RetentionPolicy]
) -> None:
    settings = load_settings(env | ({} if days is None else {RETENTION_VAR: str(days)}))

    runtime = build_scheduler_runtime(
        database=rig.database, settings=settings, clock=rig.clock, locks=ObjectLockRegistry()
    )

    assert type(runtime.scheduler.housekeeping._retention) is policy
