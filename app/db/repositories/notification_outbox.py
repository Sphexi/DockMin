"""Repository for the notification outbox: the deliveries still to send, one row per channel."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.db.repositories.clocked import ClockedRepository, format_timestamp

MAX_ROWS_PER_CALL: Final = 100

_SELECT = (
    "SELECT id, channel_id, event_id, event_type, attempts, next_attempt_at, created_at "
    "FROM notification_outbox"
)

# One row per enabled channel that subscribes to a listed key and whose scope admits the
# environment. The key stored is the first listed one the channel subscribes to.
_ENQUEUE = (
    "INSERT INTO notification_outbox (channel_id, event_id, event_type, next_attempt_at, "
    "created_at) "
    "SELECT c.id, :event_id, "
    "(SELECT k.value FROM json_each(:keys) AS k "
    "JOIN notification_subscription AS s ON s.channel_id = c.id AND s.event_type = k.value "
    "ORDER BY k.key LIMIT 1), :now, :now "
    "FROM notification_channel AS c "
    "WHERE c.is_enabled = 1 "
    "AND EXISTS (SELECT 1 FROM json_each(:keys) AS k "
    "JOIN notification_subscription AS s ON s.channel_id = c.id AND s.event_type = k.value) "
    "AND (:environment_id IS NULL OR c.environment_scope = 'all' OR EXISTS ("
    "SELECT 1 FROM notification_channel_environment AS e "
    "WHERE e.channel_id = c.id AND e.environment_id = :environment_id)) "
    "ON CONFLICT DO NOTHING"
)


@dataclass(frozen=True, slots=True)
class OutboxRow:
    """One delivery to one channel; `attempts` counts the tries already made."""

    id: int
    channel_id: int
    event_id: int
    event_type: str
    attempts: int
    next_attempt_at: str
    created_at: str


class NotificationOutboxRepository(ClockedRepository[OutboxRow]):
    """Queues and reads deliveries; rows leave only by delivery, final failure or a lost match."""

    def _map_row(self, row: sqlite3.Row) -> OutboxRow:
        return OutboxRow(
            id=int(row["id"]),
            channel_id=int(row["channel_id"]),
            event_id=int(row["event_id"]),
            event_type=str(row["event_type"]),
            attempts=int(row["attempts"]),
            next_attempt_at=str(row["next_attempt_at"]),
            created_at=str(row["created_at"]),
        )

    async def enqueue(
        self, event_id: int, event_types: Sequence[str], environment_id: int | None
    ) -> int:
        """Queue the event for every channel that wants it; return how many rows were queued.

        `event_types` is the keys the event raised, in catalogue order. An event with no
        environment reaches every subscribed channel; one with an environment reaches the channels
        whose scope is all, or lists it. Runs in the caller's transaction when there is one.
        """
        if not event_types:
            return 0
        result = await self._execute(
            _ENQUEUE,
            {
                "event_id": event_id,
                "keys": json.dumps(list(event_types)),
                "now": self._now(),
                "environment_id": environment_id,
            },
        )
        return max(result.rowcount, 0)

    async def due_channel_ids(self, *, limit: int) -> list[int]:
        """List up to limit channels with a row due now, the one waiting longest first."""
        rows = await self._session.fetch_limited(
            "SELECT channel_id FROM notification_outbox WHERE next_attempt_at <= ? "
            "GROUP BY channel_id ORDER BY min(id) LIMIT ?",
            (self._now(), limit),
            limit=limit,
        )
        return [int(row["channel_id"]) for row in rows]

    async def due_rows(self, channel_id: int, *, limit: int) -> list[OutboxRow]:
        """List up to limit of a channel's rows due now, oldest first."""
        if not 1 <= limit <= MAX_ROWS_PER_CALL:
            raise ValueError(f"limit must be between 1 and {MAX_ROWS_PER_CALL}")
        rows = await self._session.fetch_limited(
            f"{_SELECT} WHERE channel_id = ? AND next_attempt_at <= ? ORDER BY id LIMIT ?",
            (channel_id, self._now(), limit),
            limit=limit,
        )
        return [self._map_row(row) for row in rows]

    async def delete(self, row_ids: Sequence[int]) -> int:
        """Remove rows that were delivered, failed finally or are unwanted; return how many went."""
        if not row_ids:
            return 0
        result = await self._execute(
            "DELETE FROM notification_outbox WHERE id IN (SELECT value FROM json_each(?))",
            (_ids(row_ids),),
        )
        return result.rowcount

    async def defer(self, row_ids: Sequence[int], next_attempt_at: datetime) -> int:
        """Count one failed try against rows and make them due again at next_attempt_at."""
        if not row_ids:
            return 0
        result = await self._execute(
            "UPDATE notification_outbox SET attempts = attempts + 1, next_attempt_at = ? "
            "WHERE id IN (SELECT value FROM json_each(?))",
            (format_timestamp(next_attempt_at), _ids(row_ids)),
        )
        return result.rowcount


def _ids(row_ids: Sequence[int]) -> str:
    if len(row_ids) > MAX_ROWS_PER_CALL:
        raise ValueError(f"at most {MAX_ROWS_PER_CALL} rows are changed at a time")
    return json.dumps(list(row_ids))
