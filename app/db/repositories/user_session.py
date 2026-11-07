"""Repository for the session table: server-side login sessions keyed by a hashed cookie."""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from app.db.repositories.base import row_as
from app.db.repositories.clocked import ClockedRepository, format_timestamp

type SignInMethod = Literal["password", "jwt"]

_SELECT = (
    "SELECT id, user_id, token_hash, issued_at, expires_at, last_seen_at, user_agent, "
    "remote_address, sign_in_method, nav_environment_id FROM session"
)

# last_seen_at is null until the first touch, and then issued_at is the last sign of life.
_LAST_ACTIVITY = "COALESCE(last_seen_at, issued_at)"
# One predicate for every query that must agree on what usable means.
_ACTIVE = f"revoked_at IS NULL AND expires_at > :now AND {_LAST_ACTIVITY} > :idle_cutoff"

USER_AGENT_MAX = 256
REVEAL_WINDOW = timedelta(seconds=300)
REVEAL_LIMIT = 20


@dataclass(frozen=True, slots=True)
class UserSession:
    """A login session; token_hash is kept out of repr so it cannot reach a log line."""

    id: int
    user_id: int
    token_hash: str = field(repr=False)
    issued_at: str
    expires_at: str
    last_seen_at: str | None
    user_agent: str | None
    remote_address: str | None
    sign_in_method: SignInMethod = "password"
    nav_environment_id: int | None = None


@dataclass(frozen=True, slots=True)
class RevealWindow:
    """An open window for revealing secrets: when it ends and how many reveals remain."""

    ends_at: str
    reveals_left: int


_REVEAL_OPEN = (
    "id = :session_id AND revoked_at IS NULL AND expires_at > :now AND reveal_until > :now "
    "AND reveal_until <= :ceiling AND reveal_count < :limit"
)


class UserSessionRepository(ClockedRepository[UserSession]):
    """Reads and writes sessions; revocation deletes the row."""

    def _map_row(self, row: sqlite3.Row) -> UserSession:
        return row_as(UserSession, dict(row))

    async def create(
        self,
        user_id: int,
        token_hash: str,
        *,
        lifetime: timedelta,
        user_agent: str | None,
        remote_address: str | None,
        sign_in_method: SignInMethod = "password",
    ) -> UserSession:
        """Insert a session that expires lifetime from now."""
        now = self._now()
        result = await self._execute(
            "INSERT INTO session (user_id, token_hash, issued_at, expires_at, last_seen_at, "
            "user_agent, remote_address, sign_in_method) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                token_hash,
                now,
                self._after_now(lifetime),
                now,
                None if user_agent is None else user_agent[:USER_AGENT_MAX],
                remote_address,
                sign_in_method,
            ),
        )
        return await self._created(result, self.get)

    async def get(self, session_id: int) -> UserSession | None:
        """Fetch a session by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (session_id,))

    async def get_active(self, token_hash: str, *, idle_timeout: timedelta) -> UserSession | None:
        """Fetch the session for a cookie hash unless it has expired or sat idle too long."""
        return await self._one(
            f"{_SELECT} WHERE token_hash = :token_hash AND {_ACTIVE}",
            {
                "now": self._now(),
                "idle_cutoff": self._before_now(idle_timeout),
                "token_hash": token_hash,
            },
        )

    async def touch(self, session_id: int, *, min_interval: timedelta) -> None:
        """Record activity, skipping the write when the last record is recent enough."""
        await self._execute(
            f"UPDATE session SET last_seen_at = ? WHERE id = ? AND {_LAST_ACTIVITY} < ?",
            (self._now(), session_id, self._before_now(min_interval)),
        )

    async def remember_environment(self, session_id: int, environment_id: int | None) -> None:
        """Store the environment the sidebar last showed, writing nothing when it is unchanged.

        Nothing is written for an environment that does not exist.
        """
        await self._execute(
            "UPDATE session SET nav_environment_id = ? "
            "WHERE id = ? AND nav_environment_id IS NOT ? "
            "AND (? IS NULL OR EXISTS (SELECT 1 FROM environment WHERE id = ?))",
            (environment_id, session_id, environment_id, environment_id, environment_id),
        )

    async def open_reveal_window(self, session_id: int, until: datetime) -> bool:
        """Open the session's reveal window until the given time, with no reveals used.

        Returns False when the session does not exist.
        """
        result = await self._execute(
            "UPDATE session SET reveal_until = ?, reveal_count = 0 WHERE id = ?",
            (format_timestamp(until), session_id),
        )
        return result.rowcount > 0

    async def take_reveal(self, session_id: int, now: datetime) -> RevealWindow | None:
        """Spend one reveal in one statement; return the window with the reveals left after it."""
        row = await self._session.fetch_written(
            f"UPDATE session SET reveal_count = reveal_count + 1 WHERE {_REVEAL_OPEN} "
            "RETURNING reveal_until, reveal_count",
            self._reveal_params(session_id, now),
        )
        if row is None:
            return None
        return RevealWindow(str(row["reveal_until"]), REVEAL_LIMIT - int(row["reveal_count"]))

    async def reveal_window(self, session_id: int, now: datetime) -> RevealWindow | None:
        """Return the session's window if take_reveal would currently succeed."""
        row = await self._session.fetch_one(
            f"SELECT reveal_until, reveal_count FROM session WHERE {_REVEAL_OPEN}",
            self._reveal_params(session_id, now),
        )
        if row is None:
            return None
        return RevealWindow(str(row["reveal_until"]), REVEAL_LIMIT - int(row["reveal_count"]))

    @staticmethod
    def _reveal_params(session_id: int, now: datetime) -> dict[str, object]:
        return {
            "session_id": session_id,
            "now": format_timestamp(now),
            "ceiling": format_timestamp(now + REVEAL_WINDOW),
            "limit": REVEAL_LIMIT,
        }

    async def delete(self, session_id: int) -> bool:
        """Delete one session; return False if it was already gone."""
        result = await self._execute("DELETE FROM session WHERE id = ?", (session_id,))
        return result.rowcount > 0

    async def delete_for_user(self, user_id: int, *, except_session_id: int | None = None) -> int:
        """Delete every session of a user, optionally sparing one; return how many went."""
        result = await self._execute(
            "DELETE FROM session WHERE user_id = ? AND id IS NOT ?", (user_id, except_session_id)
        )
        return result.rowcount

    async def count_for_user(self, user_id: int, *, idle_timeout: timedelta) -> int:
        """Count a user's usable sessions, leaving out rows that are dead but not yet reaped."""
        row = await self._session.fetch_one(
            f"SELECT COUNT(*) AS total FROM session WHERE user_id = :user_id AND {_ACTIVE}",
            {
                "now": self._now(),
                "idle_cutoff": self._before_now(idle_timeout),
                "user_id": user_id,
            },
        )
        return 0 if row is None else int(row["total"])

    async def delete_dead(self, *, idle_timeout: timedelta) -> int:
        """Delete revoked, expired and idle sessions; return how many went."""
        result = await self._execute(
            "DELETE FROM session WHERE revoked_at IS NOT NULL OR expires_at <= ? "
            f"OR {_LAST_ACTIVITY} <= ?",
            (self._now(), self._before_now(idle_timeout)),
        )
        return result.rowcount
