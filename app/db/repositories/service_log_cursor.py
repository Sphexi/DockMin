"""Repository for service_log_cursor: how far each rule has copied each Swarm task's log."""

import json
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from app.db.repositories.base import row_as
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.errors import constraints_as_domain_errors

NO_TASK = ""
MAX_ROWS_PER_SERVICE = 20_001

_COLUMNS = (
    "rule_id, rule_created_at, environment_id, service_id, task_id, last_timestamp_ns, "
    "lines_at_last, object_path, finished, last_seen_at"
)
_ROW = "rule_id = ? AND rule_created_at = ? AND environment_id = ? AND service_id = ?"


@dataclass(frozen=True, slots=True)
class ServiceLogCursor:
    """The newest log line a rule has copied from a task and the object that holds it.

    `lines_at_last` counts the copied lines stamped exactly `last_timestamp_ns`. A row with no
    `object_path` only records that the task was seen; nothing is copied yet. `task_id` is
    NO_TASK for lines whose attributes named no task.
    """

    rule_id: int
    rule_created_at: str
    environment_id: int
    service_id: str
    task_id: str
    last_timestamp_ns: int
    lines_at_last: int
    object_path: str | None
    finished: bool
    last_seen_at: str


class ServiceLogCursorRepository(ClockedRepository[ServiceLogCursor]):
    """Reads and moves service log cursors; a rule's cursors go with the rule.

    Rule ids are reused after a delete, so every read and write names the rule's creation time
    and a row stored under another one is replaced or dropped, never read.
    """

    def _map_row(self, row: sqlite3.Row) -> ServiceLogCursor:
        data = dict(row)
        data["finished"] = bool(data["finished"])
        return row_as(ServiceLogCursor, data)

    async def for_service(
        self, rule_id: int, rule_created_at: str, environment_id: int, service_id: str
    ) -> dict[str, ServiceLogCursor]:
        """Return the rule's cursors for the service, by task id."""
        rows = await self._session.fetch_limited(
            f"SELECT {_COLUMNS} FROM service_log_cursor WHERE {_ROW}",
            (rule_id, rule_created_at, environment_id, service_id),
            limit=MAX_ROWS_PER_SERVICE,
        )
        return {found.task_id: found for found in map(self._map_row, rows)}

    async def last_seen(
        self,
        rule_id: int,
        rule_created_at: str,
        environment_id: int,
        service_ids: Sequence[str],
    ) -> dict[str, str]:
        """Return, for each listed service that has rows, the oldest time one of them was seen."""
        if not service_ids:
            return {}
        rows = await self._session.fetch_limited(
            "SELECT service_id, min(last_seen_at) AS seen FROM service_log_cursor "
            "WHERE rule_id = ? AND rule_created_at = ? AND environment_id = ? "
            "AND service_id IN (SELECT value FROM json_each(?)) GROUP BY service_id",
            (rule_id, rule_created_at, environment_id, json.dumps(list(service_ids))),
            limit=len(service_ids),
        )
        return {row["service_id"]: row["seen"] for row in rows}

    async def store_position(
        self,
        rule_id: int,
        rule_created_at: str,
        environment_id: int,
        service_id: str,
        task_id: str,
        *,
        last_timestamp_ns: int,
        lines_at_last: int,
        object_path: str,
    ) -> None:
        """Move a task's cursor forward; a position behind the stored one is ignored.

        A row stored under another creation time of the rule is replaced outright. Raises
        MissingReferenceError when the rule or the environment does not exist.
        """
        with constraints_as_domain_errors("service log cursor"):
            await self._execute(
                f"INSERT INTO service_log_cursor ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?) "
                "ON CONFLICT (rule_id, environment_id, service_id, task_id) DO UPDATE SET "
                "rule_created_at = excluded.rule_created_at, "
                "last_timestamp_ns = excluded.last_timestamp_ns, "
                "lines_at_last = excluded.lines_at_last, "
                "object_path = excluded.object_path, "
                "finished = CASE WHEN service_log_cursor.rule_created_at "
                "!= excluded.rule_created_at THEN 0 ELSE service_log_cursor.finished END, "
                "last_seen_at = excluded.last_seen_at "
                "WHERE service_log_cursor.rule_created_at != excluded.rule_created_at "
                "OR excluded.last_timestamp_ns > service_log_cursor.last_timestamp_ns "
                "OR (excluded.last_timestamp_ns = service_log_cursor.last_timestamp_ns "
                "AND excluded.lines_at_last >= service_log_cursor.lines_at_last)",
                (
                    rule_id,
                    rule_created_at,
                    environment_id,
                    service_id,
                    task_id,
                    last_timestamp_ns,
                    lines_at_last,
                    object_path,
                    self._now(),
                ),
            )

    async def mark_seen(
        self,
        rule_id: int,
        rule_created_at: str,
        environment_id: int,
        service_id: str,
        starts: Mapping[str, int],
    ) -> None:
        """Record tasks that have no row yet as seen now, each from its start time.

        A task that has a row is left as it is. The rows carry no object, so nothing counts as
        copied.
        """
        if not starts:
            return
        listing = json.dumps([{"id": found, "ns": start} for found, start in starts.items()])
        # SQLite needs the WHERE true to parse an upsert whose source is a SELECT.
        await self._execute(
            f"INSERT INTO service_log_cursor ({_COLUMNS}) "
            "SELECT ?, ?, ?, ?, json_extract(value, '$.id'), json_extract(value, '$.ns'), "
            "0, NULL, 0, ? FROM json_each(?) WHERE true "
            "ON CONFLICT (rule_id, environment_id, service_id, task_id) DO NOTHING",
            (rule_id, rule_created_at, environment_id, service_id, self._now(), listing),
        )

    async def mark_finished(
        self,
        rule_id: int,
        rule_created_at: str,
        environment_id: int,
        service_id: str,
        task_ids: Collection[str],
    ) -> None:
        """Mark the listed tasks as read to their end."""
        if not task_ids:
            return
        await self._execute(
            f"UPDATE service_log_cursor SET finished = 1 WHERE {_ROW} "
            "AND task_id IN (SELECT value FROM json_each(?))",
            (rule_id, rule_created_at, environment_id, service_id, json.dumps(sorted(task_ids))),
        )

    async def drop_unlisted_tasks(
        self,
        rule_id: int,
        rule_created_at: str,
        environment_id: int,
        service_id: str,
        listed_task_ids: Collection[str],
    ) -> int:
        """Delete the service's task cursors for tasks not among those listed; return the count.

        The row for lines that named no task is not a task and stays.
        """
        result = await self._execute(
            f"DELETE FROM service_log_cursor WHERE {_ROW} AND task_id != ? "
            "AND task_id NOT IN (SELECT value FROM json_each(?))",
            (
                rule_id,
                rule_created_at,
                environment_id,
                service_id,
                NO_TASK,
                json.dumps(sorted(listed_task_ids)),
            ),
        )
        return result.rowcount

    async def drop_unresolved_services(
        self,
        rule_id: int,
        rule_created_at: str,
        environment_id: int,
        keep_service_ids: Collection[str],
    ) -> int:
        """Delete the rule's cursors of services outside `keep_service_ids`; return the count."""
        result = await self._execute(
            "DELETE FROM service_log_cursor WHERE rule_id = ? AND environment_id = ? "
            "AND rule_created_at = ? AND service_id NOT IN (SELECT value FROM json_each(?))",
            (
                rule_id,
                environment_id,
                rule_created_at,
                json.dumps(sorted(keep_service_ids)),
            ),
        )
        return result.rowcount

    async def drop_other_creations(self, rule_id: int, rule_created_at: str) -> int:
        """Delete the rule's cursors that belong to another creation of the rule."""
        result = await self._execute(
            "DELETE FROM service_log_cursor WHERE rule_id = ? AND rule_created_at != ?",
            (rule_id, rule_created_at),
        )
        return result.rowcount

    async def refresh_seen(
        self,
        rule_id: int,
        rule_created_at: str,
        environment_id: int,
        service_id: str,
        *,
        stale_after: timedelta,
    ) -> int:
        """Stamp the service's rows as seen now unless that was recent; return rows changed."""
        result = await self._execute(
            f"UPDATE service_log_cursor SET last_seen_at = ? WHERE {_ROW} AND last_seen_at < ?",
            (
                self._now(),
                rule_id,
                rule_created_at,
                environment_id,
                service_id,
                self._before_now(stale_after),
            ),
        )
        return result.rowcount
