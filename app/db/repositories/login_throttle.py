"""Repository for the login_throttle table: failure counters per username and per address."""

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from app.db.repositories.base import row_as
from app.db.repositories.clocked import ClockedRepository

ThrottleKind = Literal["username", "address"]

_COLUMNS = "id, kind, subject, failure_count, window_started_at, locked_until"
_SELECT = f"SELECT {_COLUMNS} FROM login_throttle"

# A counter restarts when its lock has lapsed, or when it holds no lock and its window has
# passed. The same predicate appears three times because every SET expression of an upsert reads
# the row as it was before the statement.
_RESTARTS = (
    "((locked_until IS NOT NULL AND locked_until <= :now) "
    "OR (locked_until IS NULL AND window_started_at <= :window_start))"
)

_INCREMENT = (
    "INSERT INTO login_throttle (kind, subject, failure_count, window_started_at, locked_until) "
    "VALUES (:kind, :subject, 1, :now, CASE WHEN 1 >= :limit THEN :lock_until END) "
    "ON CONFLICT (kind, subject) DO UPDATE SET "
    f"failure_count = CASE WHEN {_RESTARTS} THEN 1 ELSE failure_count + 1 END, "
    f"window_started_at = CASE WHEN {_RESTARTS} THEN :now ELSE window_started_at END, "
    f"locked_until = CASE WHEN {_RESTARTS} THEN (CASE WHEN 1 >= :limit THEN :lock_until END) "
    "WHEN locked_until IS NOT NULL THEN locked_until "
    "WHEN failure_count + 1 >= :limit THEN :lock_until END "
    f"RETURNING {_COLUMNS}"
)


@dataclass(frozen=True, slots=True)
class LoginThrottle:
    """The failures counted against one username or address."""

    id: int
    kind: ThrottleKind
    subject: str
    failure_count: int
    window_started_at: str
    locked_until: str | None


class LoginThrottleRepository(ClockedRepository[LoginThrottle]):
    """Reads and writes failure counters; the policy that interprets them lives in the service."""

    def _map_row(self, row: sqlite3.Row) -> LoginThrottle:
        return row_as(LoginThrottle, dict(row))

    async def get(self, kind: ThrottleKind, subject: str) -> LoginThrottle | None:
        """Fetch the counter for one subject."""
        return await self._one(f"{_SELECT} WHERE kind = ? AND subject = ?", (kind, subject))

    async def increment(
        self, kind: ThrottleKind, subject: str, *, max_failures: int, window: timedelta
    ) -> LoginThrottle:
        """Count one failure in a single atomic statement, locking the subject at the limit."""
        row = await self._session.fetch_written(
            _INCREMENT,
            {
                "kind": kind,
                "subject": subject,
                "now": self._now(),
                "window_start": self._before_now(window),
                "limit": max_failures,
                "lock_until": self._after_now(window),
            },
        )
        if row is None:
            raise RuntimeError("an upsert with RETURNING produced no row")
        return self._map_row(row)

    async def decrement(self, kind: ThrottleKind, subject: str, *, max_failures: int) -> None:
        """Take back one counted failure, lifting the lock if the count drops below the limit."""
        await self._execute(
            "UPDATE login_throttle SET failure_count = MAX(failure_count - 1, 0), "
            "locked_until = CASE WHEN failure_count - 1 < ? THEN NULL ELSE locked_until END "
            "WHERE kind = ? AND subject = ?",
            (max_failures, kind, subject),
        )

    async def delete(self, kind: ThrottleKind, subject: str) -> int:
        """Forget one subject's failures; return how many counters went."""
        result = await self._execute(
            "DELETE FROM login_throttle WHERE kind = ? AND subject = ?", (kind, subject)
        )
        return result.rowcount

    async def delete_stale(self, *, window: timedelta) -> int:
        """Delete counters whose window has passed and that hold no active lock."""
        result = await self._execute(
            "DELETE FROM login_throttle WHERE window_started_at <= ? "
            "AND (locked_until IS NULL OR locked_until <= ?)",
            (self._before_now(window), self._now()),
        )
        return result.rowcount
