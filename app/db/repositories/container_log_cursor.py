"""Repository for container_log_cursor: how far each rule has copied each container's log."""

import json
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from app.db.repositories.base import row_as
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.errors import constraints_as_domain_errors

_COLUMNS = (
    "rule_id, rule_created_at, environment_id, container_id, last_timestamp_ns, "
    "lines_at_last, object_path, last_seen_at"
)


@dataclass(frozen=True, slots=True)
class LogCursor:
    """The newest log line a rule has copied from a container and the object that holds it.

    `lines_at_last` counts the copied lines stamped exactly `last_timestamp_ns`. A row with no
    `object_path` only records when the container was last examined; nothing is copied yet.
    """

    rule_id: int
    rule_created_at: str
    environment_id: int
    container_id: str
    last_timestamp_ns: int
    lines_at_last: int
    object_path: str | None
    last_seen_at: str


class ContainerLogCursorRepository(ClockedRepository[LogCursor]):
    """Reads and moves log cursors; a rule's cursors go with the rule.

    Rule ids are reused after a delete, so every read and write names the rule's creation time
    and a row stored under another one is replaced or dropped, never read.
    """

    def _map_row(self, row: sqlite3.Row) -> LogCursor:
        return row_as(LogCursor, dict(row))

    async def for_containers(
        self,
        rule_id: int,
        rule_created_at: str,
        environment_id: int,
        container_ids: Sequence[str],
    ) -> dict[str, LogCursor]:
        """Return the rule's cursor for each listed container that has one."""
        if not container_ids:
            return {}
        rows = await self._session.fetch_limited(
            f"SELECT {_COLUMNS} FROM container_log_cursor "
            "WHERE rule_id = ? AND rule_created_at = ? AND environment_id = ? "
            "AND container_id IN (SELECT value FROM json_each(?))",
            (rule_id, rule_created_at, environment_id, json.dumps(list(container_ids))),
            limit=len(container_ids),
        )
        return {found.container_id: found for found in map(self._map_row, rows)}

    async def store_position(
        self,
        rule_id: int,
        rule_created_at: str,
        environment_id: int,
        container_id: str,
        *,
        last_timestamp_ns: int,
        lines_at_last: int,
        object_path: str,
    ) -> None:
        """Move a container's cursor forward; a position behind the stored one is ignored.

        A row stored under another creation time of the rule is replaced outright. Raises
        MissingReferenceError when the rule or the environment does not exist.
        """
        with constraints_as_domain_errors("container log cursor"):
            await self._execute(
                f"INSERT INTO container_log_cursor ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (rule_id, environment_id, container_id) DO UPDATE SET "
                "rule_created_at = excluded.rule_created_at, "
                "last_timestamp_ns = excluded.last_timestamp_ns, "
                "lines_at_last = excluded.lines_at_last, "
                "object_path = excluded.object_path, last_seen_at = excluded.last_seen_at "
                "WHERE container_log_cursor.rule_created_at != excluded.rule_created_at "
                "OR excluded.last_timestamp_ns > container_log_cursor.last_timestamp_ns "
                "OR (excluded.last_timestamp_ns = container_log_cursor.last_timestamp_ns "
                "AND excluded.lines_at_last >= container_log_cursor.lines_at_last)",
                (
                    rule_id,
                    rule_created_at,
                    environment_id,
                    container_id,
                    last_timestamp_ns,
                    lines_at_last,
                    object_path,
                    self._now(),
                ),
            )

    async def refresh_seen(
        self,
        rule_id: int,
        rule_created_at: str,
        environment_id: int,
        container_ids: Sequence[str],
        *,
        stale_after: timedelta,
    ) -> int:
        """Stamp the listed containers as seen now unless that was recent; return rows changed."""
        if not container_ids:
            return 0
        result = await self._execute(
            "UPDATE container_log_cursor SET last_seen_at = ? "
            "WHERE rule_id = ? AND rule_created_at = ? AND environment_id = ? "
            "AND last_seen_at < ? AND container_id IN (SELECT value FROM json_each(?))",
            (
                self._now(),
                rule_id,
                rule_created_at,
                environment_id,
                self._before_now(stale_after),
                json.dumps(list(container_ids)),
            ),
        )
        return result.rowcount

    async def mark_examined(
        self,
        rule_id: int,
        rule_created_at: str,
        environment_id: int,
        starts: Mapping[str, int],
    ) -> None:
        """Record containers that have no row yet as examined now, each from its start time.

        A container that has a row is left as it is. The rows carry no object, so nothing counts
        as copied.
        """
        if not starts:
            return
        listing = json.dumps([{"id": found, "ns": start} for found, start in starts.items()])
        await self._execute(
            f"INSERT INTO container_log_cursor ({_COLUMNS}) "
            "SELECT ?, ?, ?, json_extract(value, '$.id'), json_extract(value, '$.ns'), 1, NULL, ? "
            "FROM json_each(?) WHERE true "
            "ON CONFLICT (rule_id, environment_id, container_id) DO NOTHING",
            (rule_id, rule_created_at, environment_id, self._now(), listing),
        )

    async def drop_stale(
        self,
        rule_id: int,
        rule_created_at: str,
        environment_id: int,
        present_ids: Collection[str] | None,
    ) -> int:
        """Delete the rule's cursors that belong to another creation of the rule or are gone.

        A cursor is gone when its container is not among `present_ids` and it was last seen
        before the oldest run still kept for the rule's job. With `present_ids` None, only the
        cursors of another creation are deleted. Returns the number of rows removed.
        """
        if present_ids is None:
            result = await self._execute(
                "DELETE FROM container_log_cursor WHERE rule_id = ? AND rule_created_at != ?",
                (rule_id, rule_created_at),
            )
            return result.rowcount
        result = await self._execute(
            "DELETE FROM container_log_cursor WHERE rule_id = ? AND (rule_created_at != ? "
            "OR (environment_id = ? "
            "AND container_id NOT IN (SELECT value FROM json_each(?)) "
            "AND last_seen_at < (SELECT min(r.started_at) FROM job_run r "
            "JOIN scheduled_job j ON j.id = r.scheduled_job_id WHERE j.rule_id = ?)))",
            (rule_id, rule_created_at, environment_id, json.dumps(sorted(present_ids)), rule_id),
        )
        return result.rowcount
