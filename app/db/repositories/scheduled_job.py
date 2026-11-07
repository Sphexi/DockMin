"""Repository for the scheduled_job table: the loop's due query, gates and failure counters."""

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from typing import Final, Literal

from app.db.repositories.base import Repository
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.errors import constraints_as_domain_errors
from app.db.repositories.job_run import JobRun, run_from_columns
from app.db.repositories.pagination import MAX_PAGE_SIZE, Page, validate_limit

type ScheduleKind = Literal["cron", "interval", "webhook"]
type OverrunPolicy = Literal["skip", "queue", "cancel_previous"]
type JobKind = Literal["system", "automation"]

_COLUMNS = (
    "id, subsystem, rule_id, environment_id, schedule_kind, schedule_expression, time_zone, "
    "next_run_at, overrun_policy, catch_up, is_enabled, paused_until, consecutive_failures, "
    "failure_threshold, created_at, updated_at, last_run_at, last_run_outcome"
)
_SELECT = f"SELECT {_COLUMNS} FROM scheduled_job"
_RULE_CHUNK: Final = 500

_RUN_COLUMNS: Final = (
    "scheduled_job_id", "trigger", "requested_by_user_id", "scheduled_for", "started_at",
    "finished_at", "heartbeat_at", "outcome", "reason", "message", "detail",
)  # fmt: skip
_SUMMARY_FROM: Final = (
    "SELECT j.id AS id, j.subsystem AS subsystem, j.rule_id AS rule_id, "
    "j.environment_id AS environment_id, j.schedule_kind AS schedule_kind, "
    "j.schedule_expression AS schedule_expression, "
    "j.time_zone AS time_zone, j.next_run_at AS next_run_at, "
    "j.overrun_policy AS overrun_policy, j.catch_up AS catch_up, j.is_enabled AS is_enabled, "
    "j.paused_until AS paused_until, j.consecutive_failures AS consecutive_failures, "
    "j.failure_threshold AS failure_threshold, j.created_at AS created_at, "
    "j.updated_at AS updated_at, j.last_run_at AS last_run_at, "
    "j.last_run_outcome AS last_run_outcome, r.name AS rule_name, r.is_enabled AS rule_is_enabled, "
    "r.paused_until AS rule_paused_until, r.created_by_user_id AS rule_owner_id, "
    "e.name AS environment_name, lr.id AS run_id, "
    + ", ".join(f"lr.{column} AS run_{column}" for column in _RUN_COLUMNS)
    + " FROM scheduled_job j "
    "LEFT JOIN automation_rule r ON r.id = j.rule_id "
    "LEFT JOIN environment e ON e.id = j.environment_id "
    "LEFT JOIN job_run lr ON lr.id = "
    "(SELECT max(id) FROM job_run WHERE scheduled_job_id = j.id)"
)


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """A scheduler entry, as stored; timestamps are the stored UTC text."""

    id: int
    subsystem: str
    rule_id: int | None
    environment_id: int | None
    schedule_kind: ScheduleKind
    schedule_expression: str
    time_zone: str
    next_run_at: str | None
    overrun_policy: OverrunPolicy
    catch_up: bool
    is_enabled: bool
    paused_until: str | None
    consecutive_failures: int
    failure_threshold: int | None
    created_at: str
    updated_at: str
    last_run_at: str | None = None
    last_run_outcome: str | None = None


@dataclass(frozen=True, slots=True)
class RuleGate:
    """The two rule fields that decide whether a job may run."""

    is_enabled: bool
    paused_until: str | None


@dataclass(frozen=True, slots=True)
class JobSummary:
    """A job as listed, with its rule, the job's environment and its latest run."""

    job: ScheduledJob
    rule_name: str | None
    rule_gate: RuleGate | None
    rule_owner_id: int | None
    environment_id: int | None
    environment_name: str | None
    last_run: JobRun | None


def _job_from(row: sqlite3.Row) -> ScheduledJob:
    return ScheduledJob(
        id=row["id"],
        subsystem=row["subsystem"],
        rule_id=row["rule_id"],
        environment_id=row["environment_id"],
        schedule_kind=row["schedule_kind"],
        schedule_expression=row["schedule_expression"],
        time_zone=row["time_zone"],
        next_run_at=row["next_run_at"],
        overrun_policy=row["overrun_policy"],
        catch_up=bool(row["catch_up"]),
        is_enabled=bool(row["is_enabled"]),
        paused_until=row["paused_until"],
        consecutive_failures=row["consecutive_failures"],
        failure_threshold=row["failure_threshold"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_run_at=row["last_run_at"],
        last_run_outcome=row["last_run_outcome"],
    )


class JobSummaryRepository(Repository[JobSummary]):
    """Lists jobs with the rule, environment and latest run joined in."""

    def _map_row(self, row: sqlite3.Row) -> JobSummary:
        last_run = None if row["run_id"] is None else run_from_columns(dict(row), "run_")
        gate = (
            None
            if row["rule_id"] is None or row["rule_is_enabled"] is None
            else RuleGate(bool(row["rule_is_enabled"]), row["rule_paused_until"])
        )
        return JobSummary(
            job=_job_from(row),
            rule_name=row["rule_name"],
            rule_gate=gate,
            rule_owner_id=row["rule_owner_id"],
            environment_id=row["environment_id"],
            environment_name=row["environment_name"],
            last_run=last_run,
        )

    async def get(self, job_id: int) -> JobSummary | None:
        """Fetch one job with its rule, environment and latest run."""
        return await self._one(f"{_SUMMARY_FROM} WHERE j.id = ?", (job_id,))

    async def page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        subsystem: str | None = None,
        kind: JobKind | None = None,
    ) -> Page[JobSummary]:
        """Return one page of jobs by ascending id, optionally for one subsystem and kind."""
        clauses = ["1 = 1"]
        params: tuple[str, ...] = ()
        if subsystem is not None:
            clauses.append("j.subsystem = ?")
            params = (subsystem,)
        if kind is not None:
            clauses.append("j.rule_id IS NULL" if kind == "system" else "j.rule_id IS NOT NULL")
        return await self._page(
            _SUMMARY_FROM,
            limit=limit,
            cursor=cursor,
            sort_column="j.id",
            id_column="j.id",
            where=" AND ".join(clauses),
            params=params,
        )


class ScheduledJobRepository(ClockedRepository[ScheduledJob]):
    """Reads and writes scheduled jobs; next_run_at is null whenever a job must not run."""

    def _map_row(self, row: sqlite3.Row) -> ScheduledJob:
        return _job_from(row)

    async def get(self, job_id: int) -> ScheduledJob | None:
        """Fetch a job by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (job_id,))

    async def get_for_rule(self, rule_id: int) -> ScheduledJob | None:
        """Fetch the one job a rule owns."""
        return await self._one(f"{_SELECT} WHERE rule_id = ?", (rule_id,))

    async def get_for_rules(self, rule_ids: Collection[int]) -> dict[int, ScheduledJob]:
        """Fetch the job of each listed rule that has one, keyed by rule id."""
        ids = sorted(set(rule_ids))
        jobs: dict[int, ScheduledJob] = {}
        for start in range(0, len(ids), _RULE_CHUNK):
            chunk = ids[start : start + _RULE_CHUNK]
            marks = ", ".join("?" * len(chunk))
            rows = await self._session.fetch_limited(
                f"{_SELECT} WHERE rule_id IN ({marks})", tuple(chunk), limit=len(chunk)
            )
            for row in rows:
                job = self._map_row(row)
                if job.rule_id is not None:
                    jobs[job.rule_id] = job
        return jobs

    async def get_infrastructure(self, subsystem: str) -> ScheduledJob | None:
        """Fetch a subsystem's rule-less job."""
        return await self._one(f"{_SELECT} WHERE subsystem = ? AND rule_id IS NULL", (subsystem,))

    async def insert(
        self,
        *,
        subsystem: str,
        rule_id: int | None,
        schedule_kind: ScheduleKind,
        schedule_expression: str,
        time_zone: str,
        overrun_policy: OverrunPolicy,
        catch_up: bool,
        failure_threshold: int | None,
        next_run_at: str | None,
        environment_id: int | None = None,
    ) -> ScheduledJob:
        """Insert an enabled job; DuplicateError if the rule or rule-less subsystem has one."""
        now = self._now()
        with constraints_as_domain_errors("scheduled job"):
            result = await self._execute(
                "INSERT INTO scheduled_job (subsystem, rule_id, environment_id, schedule_kind, "
                "schedule_expression, time_zone, next_run_at, overrun_policy, catch_up, "
                "is_enabled, failure_threshold, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    subsystem,
                    rule_id,
                    environment_id,
                    schedule_kind,
                    schedule_expression,
                    time_zone,
                    next_run_at,
                    overrun_policy,
                    int(catch_up),
                    failure_threshold,
                    now,
                    now,
                ),
            )
        return await self._created(result, self.get)

    async def update_schedule(
        self,
        job_id: int,
        *,
        schedule_kind: ScheduleKind,
        schedule_expression: str,
        time_zone: str,
        overrun_policy: OverrunPolicy,
        catch_up: bool,
        failure_threshold: int | None,
        next_run_at: str | None,
    ) -> None:
        """Replace a job's schedule and policies."""
        await self._execute(
            "UPDATE scheduled_job SET schedule_kind = ?, schedule_expression = ?, "
            "time_zone = ?, overrun_policy = ?, catch_up = ?, failure_threshold = ?, "
            "next_run_at = ?, updated_at = ? WHERE id = ?",
            (
                schedule_kind,
                schedule_expression,
                time_zone,
                overrun_policy,
                int(catch_up),
                failure_threshold,
                next_run_at,
                self._now(),
                job_id,
            ),
        )

    async def set_next_run(self, job_id: int, next_run_at: str | None) -> None:
        """Set when the job next fires; null stops it firing."""
        await self._execute(
            "UPDATE scheduled_job SET next_run_at = ? WHERE id = ?", (next_run_at, job_id)
        )

    async def claim(self, job_id: int, *, due_at: str, next_run_at: str | None) -> bool:
        """Advance an enabled job from due_at, and report whether this call did it.

        Two callers claiming the same fire cannot both succeed: the second finds next_run_at
        already moved.
        """
        result = await self._execute(
            "UPDATE scheduled_job SET next_run_at = ? WHERE id = ? AND is_enabled = 1 "
            "AND next_run_at = ?",
            (next_run_at, job_id, due_at),
        )
        return result.rowcount > 0

    async def bring_forward(self, job_id: int, now: str) -> bool:
        """Move a job's next fire to now, and report whether it moved.

        The job must be enabled, have a later fire, be outside every pause, belong to an enabled
        rule if it has one, and not be backing off after failures; otherwise nothing changes.
        """
        result = await self._execute(
            "UPDATE scheduled_job SET next_run_at = ? WHERE id = ? AND is_enabled = 1 "
            "AND next_run_at IS NOT NULL AND next_run_at > ? AND consecutive_failures = 0 "
            "AND (paused_until IS NULL OR paused_until <= ?) "
            "AND (rule_id IS NULL OR EXISTS (SELECT 1 FROM automation_rule r "
            "WHERE r.id = scheduled_job.rule_id AND r.is_enabled = 1 "
            "AND (r.paused_until IS NULL OR r.paused_until <= ?)))",
            (now, job_id, now, now, now),
        )
        return result.rowcount > 0

    async def arm(self, job_id: int, not_before: str) -> bool:
        """Arm an on-call job for max(now, not_before), and report whether this call did it.

        Only an enabled webhook job with no armed call, outside every pause and under an enabled
        rule, is armed. The caller wakes the scheduler after the transaction commits.
        """
        now = self._now()
        result = await self._execute(
            "UPDATE scheduled_job SET next_run_at = max(?, ?) WHERE id = ? "
            "AND schedule_kind = 'webhook' AND next_run_at IS NULL AND is_enabled = 1 "
            "AND (paused_until IS NULL OR paused_until <= ?) "
            "AND EXISTS (SELECT 1 FROM automation_rule r WHERE r.id = scheduled_job.rule_id "
            "AND r.is_enabled = 1 AND (r.paused_until IS NULL OR r.paused_until <= ?))",
            (now, not_before, job_id, now, now),
        )
        return result.rowcount > 0

    async def last_run_started(self, job_id: int) -> str | None:
        """Return when the job's newest run started, or None if it has never run."""
        row = await self._session.fetch_one(
            "SELECT started_at FROM job_run WHERE scheduled_job_id = ? ORDER BY id DESC LIMIT 1",
            (job_id,),
        )
        return None if row is None else str(row["started_at"])

    async def newest_run_open(self, job_id: int) -> bool:
        """Return whether the job's newest run has started and not finished."""
        row = await self._session.fetch_one(
            "SELECT finished_at FROM job_run WHERE scheduled_job_id = ? ORDER BY id DESC LIMIT 1",
            (job_id,),
        )
        return row is not None and row["finished_at"] is None

    async def open_job_ids(self, subsystem: str, environment_id: int) -> list[int]:
        """List the enabled jobs of a subsystem in one environment that have a next fire."""
        ids: list[int] = []
        after = 0
        while True:
            rows = await self._session.fetch_limited(
                "SELECT id FROM scheduled_job WHERE subsystem = ? AND environment_id = ? "
                "AND is_enabled = 1 AND next_run_at IS NOT NULL AND id > ? ORDER BY id LIMIT ?",
                (subsystem, environment_id, after, MAX_PAGE_SIZE),
                limit=MAX_PAGE_SIZE,
            )
            ids.extend(int(row["id"]) for row in rows)
            if len(rows) < MAX_PAGE_SIZE:
                return ids
            after = ids[-1]

    async def environments_with_open_jobs(self, subsystem: str, *, limit: int) -> list[int]:
        """List up to limit environments, by id, that hold an open job of the subsystem."""
        validate_limit(limit)
        rows = await self._session.fetch_limited(
            "SELECT DISTINCT environment_id FROM scheduled_job WHERE subsystem = ? "
            "AND environment_id IS NOT NULL AND is_enabled = 1 AND next_run_at IS NOT NULL "
            "ORDER BY environment_id LIMIT ?",
            (subsystem, limit),
            limit=limit,
        )
        return [int(row["environment_id"]) for row in rows]

    async def earliest_next_run(self, *, after: str | None = None) -> str | None:
        """Return the soonest next_run_at among enabled jobs, or None if none will fire.

        With `after`, only fires strictly later than that stored time count.
        """
        if after is None:
            row = await self._session.fetch_one(
                "SELECT next_run_at FROM scheduled_job "
                "WHERE is_enabled = 1 AND next_run_at IS NOT NULL ORDER BY next_run_at LIMIT 1"
            )
        else:
            row = await self._session.fetch_one(
                "SELECT next_run_at FROM scheduled_job "
                "WHERE is_enabled = 1 AND next_run_at > ? ORDER BY next_run_at LIMIT 1",
                (after,),
            )
        return None if row is None else str(row["next_run_at"])

    async def due(
        self, now: str, *, limit: int, after: tuple[str, int] | None = None
    ) -> list[ScheduledJob]:
        """List enabled jobs whose next_run_at is at or before now, earliest first.

        With `after`, resumes behind that (next_run_at, id) position in the same order.
        """
        validate_limit(limit)
        if after is None:
            rows = await self._session.fetch_limited(
                f"{_SELECT} WHERE is_enabled = 1 AND next_run_at IS NOT NULL "
                "AND next_run_at <= ? ORDER BY next_run_at, id LIMIT ?",
                (now, limit),
                limit=limit,
            )
        else:
            rows = await self._session.fetch_limited(
                f"{_SELECT} WHERE is_enabled = 1 AND next_run_at IS NOT NULL "
                "AND next_run_at <= ? AND (next_run_at > ? OR (next_run_at = ? AND id > ?)) "
                "ORDER BY next_run_at, id LIMIT ?",
                (now, after[0], after[0], after[1], limit),
                limit=limit,
            )
        return [self._map_row(row) for row in rows]

    async def page_after(self, after_id: int, *, limit: int) -> list[ScheduledJob]:
        """List up to limit jobs, enabled or not, with an id above after_id, by id."""
        validate_limit(limit)
        rows = await self._session.fetch_limited(
            f"{_SELECT} WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit), limit=limit
        )
        return [self._map_row(row) for row in rows]

    async def ids_for_subsystem(self, subsystem: str) -> list[int]:
        """List the id of every job a subsystem owns."""
        ids: list[int] = []
        after = 0
        while True:
            rows = await self._session.fetch_limited(
                "SELECT id FROM scheduled_job WHERE subsystem = ? AND id > ? ORDER BY id LIMIT ?",
                (subsystem, after, MAX_PAGE_SIZE),
                limit=MAX_PAGE_SIZE,
            )
            ids.extend(int(row["id"]) for row in rows)
            if len(rows) < MAX_PAGE_SIZE:
                return ids
            after = ids[-1]

    async def count_for_subsystem(self, subsystem: str) -> int:
        """Count the jobs a subsystem owns."""
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM scheduled_job WHERE subsystem = ?", (subsystem,)
        )
        return 0 if row is None else int(row["total"])

    async def rule_owner(self, job_id: int) -> int | None:
        """Return the user who owns the job's rule; None for a rule-less job or a deleted owner."""
        row = await self._session.fetch_one(
            "SELECT r.created_by_user_id AS owner FROM scheduled_job j "
            "JOIN automation_rule r ON r.id = j.rule_id WHERE j.id = ?",
            (job_id,),
        )
        return None if row is None or row["owner"] is None else int(row["owner"])

    async def record_success(self, job_id: int, next_run_at: str | None) -> None:
        """Clear the failure counter and set the next fire."""
        await self._execute(
            "UPDATE scheduled_job SET consecutive_failures = 0, next_run_at = ? WHERE id = ?",
            (next_run_at, job_id),
        )

    async def record_failure(self, job_id: int, failures: int, next_run_at: str | None) -> None:
        """Store the failure count reached and the next fire, which backoff may have delayed."""
        if failures < 0:
            raise ValueError("failures cannot be negative")
        await self._execute(
            "UPDATE scheduled_job SET consecutive_failures = ?, next_run_at = ? WHERE id = ?",
            (failures, next_run_at, job_id),
        )

    async def set_last_run(self, job_id: int, started_at: str, outcome: str) -> None:
        """Store the start time and outcome of the latest run that left no job_run row."""
        await self._execute(
            "UPDATE scheduled_job SET last_run_at = ?, last_run_outcome = ? WHERE id = ?",
            (started_at, outcome, job_id),
        )

    async def disable(self, job_id: int) -> None:
        """Disable a job and clear its next fire."""
        await self._execute(
            "UPDATE scheduled_job SET is_enabled = 0, next_run_at = NULL, updated_at = ? "
            "WHERE id = ?",
            (self._now(), job_id),
        )

    async def enable(self, job_id: int, next_run_at: str | None) -> None:
        """Enable a job, clear its failure counter and set its next fire."""
        await self._execute(
            "UPDATE scheduled_job SET is_enabled = 1, consecutive_failures = 0, "
            "next_run_at = ?, updated_at = ? WHERE id = ?",
            (next_run_at, self._now(), job_id),
        )

    async def set_paused(
        self, job_id: int, paused_until: str | None, next_run_at: str | None
    ) -> None:
        """Set or clear the pause and the next fire it implies."""
        await self._execute(
            "UPDATE scheduled_job SET paused_until = ?, next_run_at = ?, updated_at = ? "
            "WHERE id = ?",
            (paused_until, next_run_at, self._now(), job_id),
        )

    async def page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        subsystem: str | None = None,
        kind: JobKind | None = None,
    ) -> Page[JobSummary]:
        """Return one page of job summaries by ascending id."""
        return await JobSummaryRepository(self._session).page(
            limit=limit, cursor=cursor, subsystem=subsystem, kind=kind
        )

    async def summary(self, job_id: int) -> JobSummary | None:
        """Fetch one job with its rule, environment and latest run."""
        return await JobSummaryRepository(self._session).get(job_id)
