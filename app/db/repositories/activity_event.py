"""Repository for the activity_event table: the only insert, the reads, and the only delete."""

import json
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from app.db.engine import DatabaseError
from app.db.repositories.clocked import ClockedRepository, format_timestamp
from app.db.repositories.pagination import (
    MAX_ROW_ID,
    CursorPosition,
    Page,
    decode_cursor,
    encode_cursor,
    validate_limit,
)
from app.logging import DEFAULT_SCRUBBER

Category = Literal["authentication", "user_action", "automation"]
ActorType = Literal["user", "subsystem", "anonymous"]
Outcome = Literal["success", "failure"]
DetailValue = str | int | bool | None

MAX_DETAIL_KEYS = 32
MAX_DETAIL_TEXT = 512
MAX_DETAIL_SCRUB_INPUT = 4096
MAX_EXPORT_COUNT: Final = 100_000
MAX_SEQUENTIAL_READ: Final = 2_000
MAX_BY_IDS: Final = 100

_SELECT = (
    "SELECT id, category, actor_type, actor_user_id, actor_subsystem, rule_id, job_run_id, "
    "request_id, environment_id, target_type, target_id, action, result, detail, occurred_at "
    "FROM activity_event"
)


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """One recorded action, as stored."""

    id: int
    category: Category
    actor_type: ActorType
    actor_user_id: int | None
    actor_subsystem: str | None
    rule_id: int | None
    job_run_id: int | None
    request_id: str | None
    environment_id: int | None
    target_type: str
    target_id: str | None
    action: str
    result: Outcome
    detail: dict[str, DetailValue] | None
    occurred_at: str


@dataclass(frozen=True, slots=True)
class ActivityFilter:
    """Criteria for reading the log; every field left as None is unconstrained.

    A target id names one object with its type, and environment_id then becomes an exact match,
    so None selects events with no environment. Otherwise an environment also matches the
    environment's own events, and a target type alone needs an environment.
    """

    category: Category | None = None
    actor_type: ActorType | None = None
    actor_user_id: int | None = None
    actor_subsystem: str | None = None
    rule_id: int | None = None
    environment_id: int | None = None
    target_type: str | None = None
    target_id: str | None = None
    result: Outcome | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    action: str | None = None
    job_run_id: int | None = None

    def __post_init__(self) -> None:
        if self.target_id is not None and self.target_type is None:
            raise ValueError("target_type and target_id are set together")
        if self.target_type is not None and self.target_id is None and self.environment_id is None:
            raise ValueError("target_type and target_id are set together without an environment")
        for moment in (self.occurred_from, self.occurred_to):
            if moment is not None and moment.tzinfo is None:
                raise ValueError("time bounds must be timezone-aware")


@dataclass(frozen=True, slots=True)
class HeldTarget:
    """A container one rule has an event of an action for, with what may have ended its hold.

    occurred_at and name come from the newest such event. acted_at is the newest successful user
    action on the container; released_at the newest event of the release action by the same rule.
    """

    target_id: str
    occurred_at: str
    name: str | None
    acted_at: str | None
    released_at: str | None


@dataclass(frozen=True, slots=True)
class RetentionAnchor:
    """The newest event of one rule's action on one target, when it is older than a cutoff."""

    id: int
    environment_id: int | None
    target_type: str
    target_id: str | None
    occurred_at: str


@dataclass(frozen=True, slots=True)
class PruneBatch:
    """What one prune_batch call removed and kept, and where the next one resumes.

    resume_after is None once no candidate remains past this batch; blocked_by_export reports
    that the oldest event above the export watermark is older than the cutoff.
    """

    removed: int
    first_id: int | None
    last_id: int | None
    oldest_at: str | None
    newest_at: str | None
    kept: int
    resume_after: tuple[str, int] | None
    watermark: int | None
    blocked_by_export: bool


class ActivityEventRepository(ClockedRepository[ActivityEvent]):
    """Appends, reads and prunes activity events; nothing updates a row."""

    def _map_row(self, row: sqlite3.Row) -> ActivityEvent:
        values = dict(row)
        raw = values["detail"]
        values["detail"] = None if raw is None else json.loads(raw)
        return ActivityEvent(**values)

    async def insert(
        self,
        *,
        category: Category,
        actor_type: ActorType,
        actor_user_id: int | None,
        actor_subsystem: str | None,
        rule_id: int | None,
        job_run_id: int | None,
        request_id: str | None,
        environment_id: int | None,
        target_type: str,
        target_id: str | None,
        action: str,
        result: Outcome,
        detail: Mapping[str, DetailValue] | None,
    ) -> int:
        """Append one event, scrubbing and bounding detail first; return its id."""
        written = await self._execute(
            "INSERT INTO activity_event (category, actor_type, actor_user_id, actor_subsystem, "
            "rule_id, job_run_id, request_id, environment_id, target_type, target_id, action, "
            "result, detail, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                category,
                actor_type,
                actor_user_id,
                actor_subsystem,
                rule_id,
                job_run_id,
                request_id,
                environment_id,
                target_type,
                target_id,
                action,
                result,
                _encode_detail(detail),
                self._now(),
            ),
        )
        if written.last_row_id is None:
            raise DatabaseError("an insert did not report a row id")
        return written.last_row_id

    async def page(
        self, criteria: ActivityFilter, *, limit: int, cursor: str | None = None
    ) -> Page[ActivityEvent]:
        """Return one page of matching events, newest first."""
        if criteria.environment_id is not None and criteria.target_id is None:
            return await self._environment_page(criteria, limit=limit, cursor=cursor)
        where, params = _where(criteria)
        return await self._page(
            _SELECT,
            limit=limit,
            cursor=cursor,
            sort_column="occurred_at",
            descending=True,
            where=where,
            params=params,
        )

    async def _environment_page(
        self, criteria: ActivityFilter, *, limit: int, cursor: str | None
    ) -> Page[ActivityEvent]:
        validate_limit(limit)
        keyset = ""
        keyset_params: list[Any] = []
        if cursor is not None:
            position = decode_cursor(cursor)
            keyset = " AND (occurred_at, id) < (?, ?)"
            keyset_params = [position.sort_value, position.row_id]
        run = "" if criteria.job_run_id is None else "+"
        residual, residual_params = _residuals(criteria, hinted=False)
        clauses = [f"{run}environment_id = ?", *residual]
        params: list[Any] = [criteria.environment_id, *residual_params]
        if criteria.target_type is not None:
            # On the target index the page order would need a sort; the plus keeps it a residual.
            clauses.append("+target_type = ?")
            params.append(criteria.target_type)
        sql = f"{_SELECT} WHERE {' AND '.join(clauses)}{keyset}"
        params.extend(keyset_params)
        if criteria.target_type in (None, "environment"):
            own, own_params = _residuals(criteria, hinted=True)
            own_clauses = [
                f"{run}environment_id IS NULL",
                f"{run}target_type = 'environment'",
                f"{run}target_id = ?",
                *own,
            ]
            sql += f" UNION ALL {_SELECT} WHERE {' AND '.join(own_clauses)}{keyset}"
            params.extend([str(criteria.environment_id), *own_params, *keyset_params])
        sql += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
        params.append(limit + 1)
        rows = await self._session.fetch_limited(sql, params, limit=limit + 1)
        page_rows = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = page_rows[-1]
            next_cursor = encode_cursor(CursorPosition(str(last["occurred_at"]), int(last["id"])))
        return Page([self._map_row(row) for row in page_rows], next_cursor)

    async def get(self, event_id: int) -> ActivityEvent | None:
        """Fetch one event by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (event_id,))

    async def by_ids(self, event_ids: Sequence[int]) -> list[ActivityEvent]:
        """Fetch the events with these ids, in id order; an id with no event is absent."""
        if not 1 <= len(event_ids) <= MAX_BY_IDS:
            raise ValueError(f"between 1 and {MAX_BY_IDS} ids are read at a time")
        marks = ", ".join("?" * len(event_ids))
        rows = await self._session.fetch_limited(
            f"{_SELECT} WHERE id IN ({marks}) ORDER BY id",
            tuple(event_ids),
            limit=len(event_ids),
        )
        return [self._map_row(row) for row in rows]

    async def after_id(self, after: int, *, limit: int) -> list[ActivityEvent]:
        """List up to limit events with an id above after, in id order."""
        if not 1 <= limit <= MAX_SEQUENTIAL_READ:
            raise ValueError(f"limit must be between 1 and {MAX_SEQUENTIAL_READ}")
        rows = await self._session.fetch_limited(
            f"{_SELECT} WHERE id > ? ORDER BY id LIMIT ?", (after, limit), limit=limit
        )
        return [self._map_row(row) for row in rows]

    async def count_after(self, after: int, *, cap: int) -> int:
        """Count the events with an id above after, stopping at cap."""
        if cap < 1:
            raise ValueError("cap must be at least 1")
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM (SELECT 1 FROM activity_event WHERE id > ? LIMIT ?)",
            (after, cap),
        )
        return 0 if row is None else int(row["total"])

    async def count_between(self, after_id: int, up_to_id: int, *, cap: int) -> int:
        """Count the events with an id above after_id and at most up_to_id, stopping at cap."""
        if cap < 1:
            raise ValueError("cap must be at least 1")
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM "
            "(SELECT 1 FROM activity_event WHERE id > ? AND id <= ? LIMIT ?)",
            (after_id, up_to_id, cap),
        )
        return 0 if row is None else int(row["total"])

    async def first_after(self, after: int) -> ActivityEvent | None:
        """Fetch the event with the lowest id above after."""
        return await self._one(f"{_SELECT} WHERE id > ? ORDER BY id LIMIT 1", (after,))

    async def export_watermark(self) -> int | None:
        """Return the lowest exported id over every exporter rule; None when there is no rule.

        A rule that has exported nothing counts as 0, whether or not it is enabled.
        """
        row = await self._session.fetch_one(
            "SELECT min(coalesce(c.last_event_id, 0)) AS watermark FROM automation_rule AS r "
            "LEFT JOIN activity_export_cursor AS c ON c.rule_id = r.id "
            "WHERE r.subsystem = 'activity-log-exporter'"
        )
        return None if row is None or row["watermark"] is None else int(row["watermark"])

    async def exporter_rule_count(self) -> int:
        """Count the exporter rules, in any state."""
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM automation_rule "
            "WHERE subsystem = 'activity-log-exporter'"
        )
        return 0 if row is None else int(row["total"])

    async def retention_anchors(self, cutoff: str, *, limit: int) -> list[RetentionAnchor]:
        """List up to limit anchors older than cutoff, in id order.

        An anchor is the newest subsystem event, by (occurred_at, id), of one rule's action on one
        target in one environment.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        rows = await self._session.fetch_limited(
            "SELECT id, environment_id, target_type, target_id, occurred_at FROM ("
            "SELECT id, environment_id, target_type, target_id, occurred_at, row_number() OVER ("
            "PARTITION BY rule_id, environment_id, target_type, target_id, action "
            "ORDER BY occurred_at DESC, id DESC) AS position "
            "FROM activity_event WHERE rule_id IS NOT NULL AND actor_type = 'subsystem'"
            ") WHERE position = 1 AND occurred_at < ? ORDER BY id LIMIT ?",
            (cutoff, limit),
            limit=limit,
        )
        return [
            RetentionAnchor(
                id=int(row["id"]),
                environment_id=row["environment_id"],
                target_type=str(row["target_type"]),
                target_id=row["target_id"],
                occurred_at=str(row["occurred_at"]),
            )
            for row in rows
        ]

    async def prune_batch(
        self,
        *,
        cutoff: str,
        anchors: Sequence[RetentionAnchor],
        resume_after: tuple[str, int] | None,
        scan: int,
        limit: int,
    ) -> PruneBatch:
        """Delete up to limit events older than cutoff among the next scan candidates.

        Candidates follow resume_after in (occurred_at, id) order. Events above the export
        watermark and the pruner's own events are never candidates; anchors and the user actions
        that may release them are kept. Raises RuntimeError outside a transaction: the watermark
        read and the delete must see one snapshot.
        """
        if not self.in_transaction:
            raise RuntimeError("prune_batch runs inside a transaction")
        if scan < 1 or limit < 1:
            raise ValueError("scan and limit must be at least 1")
        watermark = await self.export_watermark()
        keyset = ""
        keyset_params: tuple[str, int] | tuple[()] = ()
        if resume_after is not None:
            keyset, keyset_params = " AND (occurred_at, id) > (?, ?)", resume_after
        rows = await self._session.fetch_limited(
            "SELECT id, category, environment_id, target_type, target_id, occurred_at "
            "FROM activity_event WHERE occurred_at < ? AND (? IS NULL OR id <= ?) "
            f"AND actor_subsystem IS NOT 'activity-log-pruner'{keyset} "
            "ORDER BY occurred_at, id LIMIT ?",
            (cutoff, watermark, watermark, *keyset_params, scan),
            limit=scan,
        )
        holds = _RetentionHolds(anchors)
        doomed: list[sqlite3.Row] = []
        kept = 0
        resume: tuple[str, int] | None = None
        for row in rows:
            if holds.keeps(row):
                kept += 1
            else:
                doomed.append(row)
                if len(doomed) == limit:
                    resume = (str(row["occurred_at"]), int(row["id"]))
                    break
        else:
            if len(rows) == scan:
                resume = (str(rows[-1]["occurred_at"]), int(rows[-1]["id"]))
        ids = [int(row["id"]) for row in doomed]
        if ids:
            await self._delete_events(ids, cutoff, watermark)
        blocked = watermark is not None and await self._held_by_export(watermark, cutoff)
        return PruneBatch(
            removed=len(doomed),
            first_id=min(ids, default=None),
            last_id=max(ids, default=None),
            oldest_at=str(doomed[0]["occurred_at"]) if doomed else None,
            newest_at=str(doomed[-1]["occurred_at"]) if doomed else None,
            kept=kept,
            resume_after=resume,
            watermark=watermark,
            blocked_by_export=blocked,
        )

    async def _held_by_export(self, watermark: int, cutoff: str) -> bool:
        row = await self._session.fetch_one(
            "SELECT occurred_at FROM activity_event WHERE id > ? "
            "AND actor_subsystem IS NOT 'activity-log-pruner' ORDER BY id LIMIT 1",
            (watermark,),
        )
        return row is not None and str(row["occurred_at"]) < cutoff

    async def _delete_events(self, ids: list[int], cutoff: str, watermark: int | None) -> None:
        # The candidate conditions are checked again, so a fault in the selection cannot widen it.
        result = await self._execute(
            "DELETE FROM activity_event WHERE id IN (SELECT value FROM json_each(?)) "
            "AND occurred_at < ? AND (? IS NULL OR id <= ?) "
            "AND actor_subsystem IS NOT 'activity-log-pruner'",
            (json.dumps(ids), cutoff, watermark, watermark),
        )
        if result.rowcount != len(ids):
            raise DatabaseError("the pruned rows changed under the batch's transaction")

    async def count_for_run(self, job_run_id: int) -> int:
        """Count the events recorded under one job run."""
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM activity_event WHERE job_run_id = ?", (job_run_id,)
        )
        return 0 if row is None else int(row["total"])

    async def recent_user_targets(
        self, since: str, *, limit: int
    ) -> list[tuple[int | None, str, str | None, str]]:
        """List (environment, target type, target id, time) of user actions since a stored time.

        Newest first, so a cut at limit keeps the most recent.
        """
        rows = await self._session.fetch_limited(
            "SELECT environment_id, target_type, target_id, occurred_at FROM activity_event "
            "WHERE category = 'user_action' AND occurred_at >= ? "
            "ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (since, limit),
            limit=limit,
        )
        return [
            (row["environment_id"], row["target_type"], row["target_id"], row["occurred_at"])
            for row in rows
        ]

    async def latest_user_action(
        self, environment_id: int, target_type: str, target_id: str, since: str
    ) -> str | None:
        """Return the stored time of the newest user action on one object since a stored time.

        A stack is matched with case folded, as its controls are.
        """
        if target_type == "stack":
            row = await self._session.fetch_one(
                "SELECT max(occurred_at) AS at FROM activity_event "
                "WHERE category = 'user_action' AND occurred_at >= ? AND environment_id = ? "
                "AND target_type = 'stack' AND lower(target_id) = lower(?)",
                (since, environment_id, target_id),
            )
        else:
            row = await self._session.fetch_one(
                "SELECT max(occurred_at) AS at FROM activity_event "
                "WHERE environment_id = ? AND target_type = ? AND target_id = ? "
                "AND category = 'user_action' AND occurred_at >= ?",
                (environment_id, target_type, target_id, since),
            )
        return None if row is None or row["at"] is None else str(row["at"])

    async def count_subsystem_actions(
        self,
        environment_id: int,
        target_type: str,
        target_id: str,
        *,
        subsystem: str,
        rule_id: int,
        action: str,
        since: str,
    ) -> int:
        """Count one subsystem rule's events of an action on one object since a stored time."""
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM activity_event "
            "WHERE environment_id = ? AND target_type = ? AND target_id = ? AND occurred_at >= ? "
            "AND actor_subsystem = ? AND rule_id = ? AND action = ?",
            (environment_id, target_type, target_id, since, subsystem, rule_id, action),
        )
        return 0 if row is None else int(row["total"])

    async def latest_subsystem_action(
        self,
        environment_id: int,
        target_type: str,
        target_id: str,
        *,
        subsystem: str,
        rule_id: int,
        action: str,
    ) -> str | None:
        """Return the stored time of one subsystem rule's newest event of an action on an object."""
        row = await self._session.fetch_one(
            "SELECT max(occurred_at) AS at FROM activity_event "
            "WHERE environment_id = ? AND target_type = ? AND target_id = ? "
            "AND actor_subsystem = ? AND rule_id = ? AND action = ?",
            (environment_id, target_type, target_id, subsystem, rule_id, action),
        )
        return None if row is None or row["at"] is None else str(row["at"])

    async def latest_subsystem_event(
        self,
        environment_id: int,
        target_type: str,
        target_id: str,
        *,
        subsystem: str,
        rule_id: int | None,
        actions: Collection[str],
    ) -> ActivityEvent | None:
        """Return a subsystem's newest event of any of the actions on an object.

        A rule id limits the read to that rule's events; None reads every rule's. Newest by
        (occurred_at, id). The target id is matched exactly; a stack is found only under the name
        its events were recorded with.
        """
        if not actions:
            raise ValueError("actions must name at least one action")
        names = sorted(set(actions))
        marks = ", ".join("?" for _ in names)
        rule_clause, rule_args = ("", ()) if rule_id is None else ("AND rule_id = ? ", (rule_id,))
        row = await self._session.fetch_one(
            f"{_SELECT} WHERE environment_id = ? AND target_type = ? AND target_id = ? "
            f"AND actor_subsystem = ? {rule_clause}AND action IN ({marks}) "
            "ORDER BY occurred_at DESC, id DESC LIMIT 1",
            (environment_id, target_type, target_id, subsystem, *rule_args, *names),
        )
        return None if row is None else self._map_row(row)

    async def held_targets(
        self,
        environment_id: int,
        subsystem: str,
        rule_id: int,
        action: str,
        limit: int,
        *,
        release_action: str,
        target_ids: Sequence[str] | None = None,
    ) -> list[HeldTarget]:
        """List the containers a subsystem rule has events of an action for, newest first.

        `target_ids` narrows the read to those containers.
        """
        narrow = (
            "" if target_ids is None else "AND h.target_id IN (SELECT value FROM json_each(?)) "
        )
        ids = () if target_ids is None else (json.dumps(list(target_ids)),)
        # The unary plus keeps the planner on the action index, not the rule's.
        rows = await self._session.fetch_limited(
            "SELECT h.target_id AS target_id, max(h.occurred_at) AS at, "
            "json_extract(h.detail, '$.name') AS name, "
            "(SELECT max(u.occurred_at) FROM activity_event u "
            "WHERE u.environment_id = h.environment_id AND u.target_type = 'container' "
            "AND u.target_id = h.target_id AND u.category = 'user_action' "
            "AND u.result = 'success') AS acted_at, "
            "(SELECT max(r.occurred_at) FROM activity_event r "
            "WHERE r.environment_id = h.environment_id AND r.target_type = 'container' "
            "AND r.target_id = h.target_id AND r.actor_subsystem = ? AND +r.rule_id = ? "
            "AND r.action = ?) AS released_at "
            "FROM activity_event h WHERE h.action = ? AND h.environment_id = ? "
            "AND h.target_type = 'container' AND h.target_id IS NOT NULL "
            f"AND h.actor_subsystem = ? AND +h.rule_id = ? {narrow}"
            "GROUP BY h.target_id ORDER BY at DESC, h.target_id LIMIT ?",
            (
                subsystem,
                rule_id,
                release_action,
                action,
                environment_id,
                subsystem,
                rule_id,
                *ids,
                limit,
            ),
            limit=limit,
        )
        return [
            HeldTarget(
                target_id=str(row["target_id"]),
                occurred_at=str(row["at"]),
                name=None if row["name"] is None else str(row["name"]),
                acted_at=None if row["acted_at"] is None else str(row["acted_at"]),
                released_at=None if row["released_at"] is None else str(row["released_at"]),
            )
            for row in rows
        ]

    async def unreferenced_run_ids_beyond(self, job_id: int, *, keep: int, limit: int) -> list[int]:
        """List up to limit finished runs of a job older than its newest keep and named by no event.

        Runs in flight are never listed. Oldest first.
        """
        if keep < 1:
            raise ValueError("keep must be at least 1")
        rows = await self._session.fetch_limited(
            "SELECT id FROM job_run WHERE scheduled_job_id = ? AND outcome IS NOT NULL "
            "AND id < (SELECT id FROM job_run WHERE scheduled_job_id = ? "
            "AND outcome IS NOT NULL ORDER BY id DESC LIMIT 1 OFFSET ?) "
            "AND NOT EXISTS (SELECT 1 FROM activity_event WHERE job_run_id = job_run.id) "
            "ORDER BY id LIMIT ?",
            (job_id, job_id, keep - 1, limit),
            limit=limit,
        )
        return [int(row["id"]) for row in rows]


def check_flat_detail(detail: Mapping[str, DetailValue], *, max_keys: int) -> None:
    """Raise ValueError unless detail is flat, string-keyed, bounded and made of simple values."""
    if len(detail) > max_keys:
        raise ValueError(f"detail holds at most {max_keys} keys")
    for key, value in detail.items():
        if not isinstance(key, str) or not (value is None or isinstance(value, str | int | bool)):
            raise ValueError(
                "detail holds only string keys and flat string, integer, boolean or null values"
            )


def scrub_bounded_text(text: str, limit: int) -> str:
    """Scrub a bounded window, then cut to the limit, so a straddling secret is never half-kept."""
    return DEFAULT_SCRUBBER.scrub_text(text[:MAX_DETAIL_SCRUB_INPUT])[:limit]


def encode_flat_detail(
    detail: Mapping[str, DetailValue] | None, *, max_keys: int, max_text: int
) -> str | None:
    """Return the canonical JSON of detail after scrubbing it and cutting each string."""
    if not detail:
        return None
    check_flat_detail(detail, max_keys=max_keys)
    window = {
        _encodable(key): _encodable(value[:MAX_DETAIL_SCRUB_INPUT])
        if isinstance(value, str)
        else value
        for key, value in detail.items()
    }
    scrubbed: dict[str, DetailValue] = DEFAULT_SCRUBBER.scrub(window)
    bounded = _truncate(scrubbed, max_text)
    return json.dumps(bounded, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _encode_detail(detail: Mapping[str, DetailValue] | None) -> str | None:
    return encode_flat_detail(detail, max_keys=MAX_DETAIL_KEYS, max_text=MAX_DETAIL_TEXT)


def _encodable(text: str) -> str:
    # json.dumps escapes a lone surrogate, so it would be stored and fail only when read back.
    return text.encode("utf-8", "replace").decode("utf-8")


def _truncate(values: Mapping[str, DetailValue], limit: int) -> dict[str, DetailValue]:
    return {k: v[:limit] if isinstance(v, str) else v for k, v in values.items()}


def _residuals(criteria: ActivityFilter, *, hinted: bool) -> tuple[list[str], list[Any]]:
    """Return the conditions of every filter but environment and target.

    Each takes a unary plus so it cannot choose the index: every equality when hinted, all but
    the run when a job run is named.
    """
    by_run = criteria.job_run_id is not None
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("category", criteria.category),
        ("actor_type", criteria.actor_type),
        ("actor_user_id", criteria.actor_user_id),
        ("actor_subsystem", criteria.actor_subsystem),
        ("rule_id", criteria.rule_id),
        ("job_run_id", criteria.job_run_id),
        ("action", criteria.action),
        ("result", criteria.result),
    ):
        if value is not None:
            prefix = "+" if (hinted or by_run) and column != "job_run_id" else ""
            clauses.append(f"{prefix}{column} = ?")
            params.append(value)
    prefix = "+" if by_run else ""
    if criteria.occurred_from is not None:
        clauses.append(f"{prefix}occurred_at >= ?")
        params.append(format_timestamp(criteria.occurred_from))
    if criteria.occurred_to is not None:
        clauses.append(f"{prefix}occurred_at < ?")
        params.append(format_timestamp(criteria.occurred_to))
    return clauses, params


def _where(criteria: ActivityFilter) -> tuple[str, list[Any]]:
    """Return the condition of a filter naming one object, or no environment at all."""
    clauses, params = _residuals(criteria, hinted=False)
    if criteria.target_id is not None:
        clauses = ["environment_id IS ?", "target_type = ?", "target_id = ?", *clauses]
        params = [criteria.environment_id, criteria.target_type, criteria.target_id, *params]
    return (" AND ".join(clauses) if clauses else "1 = 1"), params


class _RetentionHolds:
    """Anchors, and the user actions at or after one on its target or, for a stack, a service."""

    def __init__(self, anchors: Sequence[RetentionAnchor]) -> None:
        self._ids = frozenset(anchor.id for anchor in anchors)
        self._targets: dict[tuple[int | None, str, str | None], str] = {}
        self._stacks: dict[int | None, str] = {}
        for anchor in anchors:
            key = (
                anchor.environment_id,
                anchor.target_type,
                _folded(anchor.target_type, anchor.target_id),
            )
            _keep_earliest(self._targets, key, anchor.occurred_at)
            if anchor.target_type == "stack":
                _keep_earliest(self._stacks, anchor.environment_id, anchor.occurred_at)

    def keeps(self, row: sqlite3.Row) -> bool:
        if row["id"] in self._ids:
            return True
        if row["category"] != "user_action":
            return False
        occurred_at = str(row["occurred_at"])
        target_type = str(row["target_type"])
        key = (row["environment_id"], target_type, _folded(target_type, row["target_id"]))
        since = self._targets.get(key)
        if since is not None and occurred_at >= since:
            return True
        if target_type == "service":
            since = self._stacks.get(row["environment_id"])
            return since is not None and occurred_at >= since
        return False


def _folded(target_type: str, target_id: str | None) -> str | None:
    # casefold equates at least every pair SQL lower() does, so it can only keep more rows.
    return target_id.casefold() if target_type == "stack" and target_id is not None else target_id


def _keep_earliest[K](earliest: dict[K, str], key: K, occurred_at: str) -> None:
    current = earliest.get(key)
    if current is None or occurred_at < current:
        earliest[key] = occurred_at


def activity_sequence(connection: sqlite3.Connection) -> int:
    """Return the highest activity event id ever issued, 0 when none has been.

    Blocks; call it from a worker thread with a connection the caller owns.
    """
    issued = 0
    has_sequence = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
    ).fetchone()
    if has_sequence is not None:
        row = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'activity_event'"
        ).fetchone()
        if row is not None and row[0] is not None:
            issued = int(row[0])
    row = connection.execute("SELECT max(id) FROM activity_event").fetchone()
    if row is not None and row[0] is not None:
        issued = max(issued, int(row[0]))
    return issued


def raise_activity_sequence(connection: sqlite3.Connection, floor: int) -> None:
    """Make every activity event id issued from now on greater than floor; never lower it.

    Blocks; call it from a worker thread with a connection the caller owns.
    """
    if isinstance(floor, bool) or not 0 <= floor < MAX_ROW_ID:
        raise ValueError("floor must be a non-negative row id")
    # A savepoint works both inside a caller's transaction and as a transaction of its own.
    connection.execute("SAVEPOINT activity_sequence")
    try:
        updated = connection.execute(
            "UPDATE sqlite_sequence SET seq = max(seq, ?) WHERE name = 'activity_event'",
            (floor,),
        )
        if updated.rowcount == 0:
            connection.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('activity_event', ?)", (floor,)
            )
    except BaseException:
        connection.execute("ROLLBACK TO activity_sequence")
        connection.execute("RELEASE activity_sequence")
        raise
    connection.execute("RELEASE activity_sequence")
