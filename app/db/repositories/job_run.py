"""Repository for the job_run table: run history, in-flight bookkeeping and pruning."""

import json
import re
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, Literal

from app.db.repositories.activity_event import (
    ActivityEventRepository,
    DetailValue,
    check_flat_detail,
    encode_flat_detail,
    scrub_bounded_text,
)
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.errors import constraints_as_domain_errors
from app.db.repositories.pagination import MAX_PAGE_SIZE, Page

type RunOutcome = Literal["success", "partial", "failure", "skipped", "interrupted"]
type RunTrigger = Literal["schedule", "catch_up", "queued", "manual", "dry_run", "webhook"]

KEEP_RUNS_PER_JOB: Final = 500
MAX_RUN_DETAIL_KEYS: Final = 24
MAX_RUN_DETAIL_TEXT: Final = 3072
MAX_RUN_MESSAGE: Final = 512

_REASON: Final = re.compile(r"[a-z_]{1,64}")
_PRUNE_BATCH: Final = 200
_NEEDS_REASON: Final = frozenset({"failure", "skipped", "interrupted"})
_ID_CHUNK: Final = 500
_RECENT_CHUNK: Final = 100
_MAX_RECENT_PER_JOB: Final = 20

_COLUMNS = (
    "id, scheduled_job_id, trigger, requested_by_user_id, scheduled_for, started_at, "
    "finished_at, heartbeat_at, outcome, reason, message, detail"
)
_SELECT = f"SELECT {_COLUMNS} FROM job_run"


@dataclass(frozen=True, slots=True)
class JobRun:
    """One execution of a scheduled job, as stored."""

    id: int
    scheduled_job_id: int
    trigger: RunTrigger
    requested_by_user_id: int | None
    scheduled_for: str | None
    started_at: str
    finished_at: str | None
    heartbeat_at: str | None
    outcome: RunOutcome | None
    reason: str | None
    message: str | None
    detail: Mapping[str, DetailValue]


def run_from_columns(row: Mapping[str, Any], prefix: str = "") -> JobRun:
    """Build a JobRun from a row whose run columns carry the given name prefix."""
    raw = row[f"{prefix}detail"]
    return JobRun(
        id=row[f"{prefix}id"],
        scheduled_job_id=row[f"{prefix}scheduled_job_id"],
        trigger=row[f"{prefix}trigger"],
        requested_by_user_id=row[f"{prefix}requested_by_user_id"],
        scheduled_for=row[f"{prefix}scheduled_for"],
        started_at=row[f"{prefix}started_at"],
        finished_at=row[f"{prefix}finished_at"],
        heartbeat_at=row[f"{prefix}heartbeat_at"],
        outcome=row[f"{prefix}outcome"],
        reason=row[f"{prefix}reason"],
        message=row[f"{prefix}message"],
        detail={} if raw is None else json.loads(raw),
    )


class JobRunRepository(ClockedRepository[JobRun]):
    """Reads and writes run history; text and detail are bounded and scrubbed on the way in."""

    def _map_row(self, row: sqlite3.Row) -> JobRun:
        return run_from_columns(dict(row))

    async def get(self, run_id: int) -> JobRun | None:
        """Fetch a run by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (run_id,))

    async def start(
        self,
        job_id: int,
        *,
        trigger: RunTrigger,
        scheduled_for: str | None,
        requested_by_user_id: int | None = None,
    ) -> JobRun:
        """Insert an in-flight run whose heartbeat starts now."""
        now = self._now()
        with constraints_as_domain_errors("job run"):
            result = await self._execute(
                "INSERT INTO job_run (scheduled_job_id, trigger, requested_by_user_id, "
                "scheduled_for, started_at, heartbeat_at) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, trigger, requested_by_user_id, scheduled_for, now, now),
            )
        return await self._created(result, self.get)

    async def finish(
        self,
        run_id: int,
        *,
        outcome: RunOutcome,
        reason: str | None = None,
        message: str | None = None,
        detail: Mapping[str, DetailValue] | None = None,
    ) -> bool:
        """Record the outcome of a run that is still in flight; False if it already finished."""
        _check_outcome(outcome, reason)
        result = await self._execute(
            "UPDATE job_run SET outcome = ?, reason = ?, message = ?, detail = ?, "
            "finished_at = ? WHERE id = ? AND outcome IS NULL",
            (outcome, reason, _bound_message(message), _encode_detail(detail), self._now(), run_id),
        )
        return result.rowcount > 0

    async def record_late(
        self,
        run: JobRun,
        *,
        outcome: RunOutcome | None = None,
        reason: str | None = None,
        message: str | None = None,
        detail: Mapping[str, DetailValue] | None = None,
    ) -> JobRun:
        """Insert the row of a run that started without one, keeping its start time.

        Without an outcome the row is in flight, with a heartbeat of now.
        """
        if outcome is not None:
            _check_outcome(outcome, reason)
        now = self._now()
        with constraints_as_domain_errors("job run"):
            result = await self._execute(
                "INSERT INTO job_run (scheduled_job_id, trigger, requested_by_user_id, "
                "scheduled_for, started_at, finished_at, heartbeat_at, outcome, reason, "
                "message, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.scheduled_job_id,
                    run.trigger,
                    run.requested_by_user_id,
                    run.scheduled_for,
                    run.started_at,
                    None if outcome is None else now,
                    now if outcome is None else None,
                    outcome,
                    reason,
                    _bound_message(message),
                    _encode_detail(detail),
                ),
            )
        return await self._created(result, self.get)

    async def record_skipped(
        self,
        job_id: int,
        *,
        trigger: RunTrigger,
        scheduled_for: str | None,
        reason: str,
        message: str,
        detail: Mapping[str, DetailValue] | None = None,
    ) -> JobRun:
        """Insert a run that never started and is already finished as skipped."""
        _check_outcome("skipped", reason)
        now = self._now()
        with constraints_as_domain_errors("job run"):
            result = await self._execute(
                "INSERT INTO job_run (scheduled_job_id, trigger, scheduled_for, started_at, "
                "finished_at, outcome, reason, message, detail) "
                "VALUES (?, ?, ?, ?, ?, 'skipped', ?, ?, ?)",
                (
                    job_id,
                    trigger,
                    scheduled_for,
                    now,
                    now,
                    reason,
                    _bound_message(message),
                    _encode_detail(detail),
                ),
            )
        return await self._created(result, self.get)

    async def heartbeat(self, run_ids: Collection[int]) -> None:
        """Stamp the heartbeat of every listed run that is still in flight."""
        now = self._now()
        ids = sorted(set(run_ids))
        for start in range(0, len(ids), _ID_CHUNK):
            chunk = ids[start : start + _ID_CHUNK]
            marks = ", ".join("?" * len(chunk))
            await self._execute(
                f"UPDATE job_run SET heartbeat_at = ? WHERE outcome IS NULL AND id IN ({marks})",
                (now, *chunk),
            )

    async def in_flight(self) -> list[JobRun]:
        """List every run still in flight, oldest id first."""
        runs: list[JobRun] = []
        after = 0
        while True:
            rows = await self._session.fetch_limited(
                f"{_SELECT} WHERE outcome IS NULL AND id > ? ORDER BY id LIMIT ?",
                (after, MAX_PAGE_SIZE),
                limit=MAX_PAGE_SIZE,
            )
            runs.extend(self._map_row(row) for row in rows)
            if len(rows) < MAX_PAGE_SIZE:
                return runs
            after = runs[-1].id

    async def latest(self, job_id: int) -> JobRun | None:
        """Fetch the job's most recently started run, finished or not."""
        return await self._one(
            f"{_SELECT} WHERE scheduled_job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
        )

    async def latest_dry_runs(self, job_ids: Collection[int]) -> dict[int, JobRun]:
        """Fetch the most recently started dry run of each listed job that has one."""
        ids = sorted(set(job_ids))
        latest: dict[int, JobRun] = {}
        for start in range(0, len(ids), _ID_CHUNK):
            chunk = ids[start : start + _ID_CHUNK]
            marks = ", ".join("?" * len(chunk))
            rows = await self._session.fetch_limited(
                f"{_SELECT} WHERE id IN (SELECT max(id) FROM job_run "
                f"WHERE trigger = 'dry_run' AND scheduled_job_id IN ({marks}) "
                "GROUP BY scheduled_job_id)",
                tuple(chunk),
                limit=len(chunk),
            )
            for row in rows:
                run = self._map_row(row)
                latest[run.scheduled_job_id] = run
        return latest

    async def count_for_job(self, job_id: int) -> int:
        """Count the runs recorded for a job."""
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM job_run WHERE scheduled_job_id = ?", (job_id,)
        )
        return 0 if row is None else int(row["total"])

    async def count_failed_since(
        self, within: timedelta, *, environment_id: int, owner_id: int | None = None
    ) -> int:
        """Count the runs of the environment's rule jobs that failed within the last `within`.

        Dry runs are not counted. With an owner id, only runs of rules that user created count.
        """
        owner_clause, owner_args = (
            ("", ())
            if owner_id is None
            else (
                "AND j.rule_id IN (SELECT id FROM automation_rule WHERE created_by_user_id = ?) ",
                (owner_id,),
            )
        )
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM job_run r "
            "JOIN scheduled_job j ON j.id = r.scheduled_job_id "
            "WHERE j.environment_id = ? AND j.rule_id IS NOT NULL AND r.outcome = 'failure' "
            "AND r.trigger <> 'dry_run' "
            f"AND r.finished_at >= ? {owner_clause}",
            (environment_id, self._before_now(within), *owner_args),
        )
        return 0 if row is None else int(row["total"])

    async def any_in_flight(self, job_ids: Collection[int]) -> bool:
        """Report whether any of the jobs has a run that has not finished."""
        ids = sorted(set(job_ids))
        for start in range(0, len(ids), _ID_CHUNK):
            chunk = ids[start : start + _ID_CHUNK]
            marks = ", ".join("?" * len(chunk))
            row = await self._session.fetch_one(
                "SELECT 1 AS found FROM job_run "
                f"WHERE outcome IS NULL AND scheduled_job_id IN ({marks}) LIMIT 1",
                tuple(chunk),
            )
            if row is not None:
                return True
        return False

    async def in_flight_for_environment(self, environment_id: int) -> bool:
        """Report whether a job scoped to the environment has a run that has not finished."""
        row = await self._session.fetch_one(
            "SELECT 1 AS found FROM job_run r "
            "WHERE r.outcome IS NULL AND r.scheduled_job_id IN "
            "(SELECT id FROM scheduled_job WHERE environment_id = ?) LIMIT 1",
            (environment_id,),
        )
        return row is not None

    async def recent_outcomes(
        self, job_ids: Collection[int], *, per_job: int = 5
    ) -> dict[int, list[JobRun]]:
        """Return each job's newest runs, newest first; every requested job has an entry.

        A run still in flight is included, with no outcome.
        """
        if not 1 <= per_job <= _MAX_RECENT_PER_JOB:
            raise ValueError(f"per_job must be between 1 and {_MAX_RECENT_PER_JOB}")
        ids = sorted(set(job_ids))
        recent: dict[int, list[JobRun]] = {job_id: [] for job_id in ids}
        for start in range(0, len(ids), _RECENT_CHUNK):
            chunk = ids[start : start + _RECENT_CHUNK]
            marks = ", ".join("?" * len(chunk))
            rows = await self._session.fetch_limited(
                f"SELECT {_COLUMNS} FROM (SELECT {_COLUMNS}, row_number() OVER "
                "(PARTITION BY scheduled_job_id ORDER BY id DESC) AS position "
                f"FROM job_run WHERE scheduled_job_id IN ({marks})) "
                "WHERE position <= ? ORDER BY scheduled_job_id, id DESC",
                (*chunk, per_job),
                limit=len(chunk) * per_job,
            )
            for row in rows:
                run = self._map_row(row)
                recent[run.scheduled_job_id].append(run)
        return recent

    async def page(self, job_id: int, *, limit: int, cursor: str | None = None) -> Page[JobRun]:
        """Return one page of a job's runs, newest first."""
        return await self._page(
            _SELECT,
            limit=limit,
            cursor=cursor,
            sort_column="id",
            descending=True,
            where="scheduled_job_id = ?",
            params=(job_id,),
        )

    async def prune(self, job_id: int, *, keep: int = KEEP_RUNS_PER_JOB) -> int:
        """Delete finished runs beyond the newest keep that no activity event references.

        In-flight runs are never deleted. Returns the number of rows removed.
        """
        events = ActivityEventRepository(self._session)
        removed = 0
        while ids := await events.unreferenced_run_ids_beyond(
            job_id, keep=keep, limit=_PRUNE_BATCH
        ):
            marks = ", ".join("?" * len(ids))
            result = await self._execute(
                f"DELETE FROM job_run WHERE outcome IS NOT NULL AND id IN ({marks})", tuple(ids)
            )
            removed += result.rowcount
            if len(ids) < _PRUNE_BATCH or result.rowcount == 0:
                break
        return removed


def is_reason(text: str) -> bool:
    """Report whether the text is a valid machine reason code."""
    return _REASON.fullmatch(text) is not None


def check_run_detail(detail: Mapping[str, DetailValue]) -> None:
    """Raise ValueError unless detail can be stored on a run."""
    check_flat_detail(detail, max_keys=MAX_RUN_DETAIL_KEYS)


def _check_outcome(outcome: str, reason: str | None) -> None:
    if reason is not None and not is_reason(reason):
        raise ValueError("a run reason is 1 to 64 lowercase letters and underscores")
    if outcome in _NEEDS_REASON and reason is None:
        raise ValueError(f"a {outcome} run records a reason")


def _bound_message(message: str | None) -> str | None:
    return None if message is None else scrub_bounded_text(message, MAX_RUN_MESSAGE)


def _encode_detail(detail: Mapping[str, DetailValue] | None) -> str | None:
    return encode_flat_detail(detail, max_keys=MAX_RUN_DETAIL_KEYS, max_text=MAX_RUN_DETAIL_TEXT)
