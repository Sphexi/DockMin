"""Repository for the api_token table: a searchable prefix and a hash, never the token."""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.db.repositories.base import row_as
from app.db.repositories.clocked import ClockedRepository, format_timestamp
from app.db.repositories.errors import constraints_as_domain_errors
from app.db.repositories.pagination import Page

_SELECT = (
    "SELECT id, user_id, label, prefix, token_hash, created_at, last_used_at, expires_at, "
    "revoked_at FROM api_token"
)


@dataclass(frozen=True, slots=True)
class ApiToken:
    """A token record; token_hash is kept out of repr so it cannot reach a log line."""

    id: int
    user_id: int
    label: str
    prefix: str
    token_hash: str = field(repr=False)
    created_at: str
    last_used_at: str | None
    expires_at: str | None
    revoked_at: str | None


class ApiTokenRepository(ClockedRepository[ApiToken]):
    """Reads and writes token records; revocation stamps the row and keeps it."""

    def _map_row(self, row: sqlite3.Row) -> ApiToken:
        return row_as(ApiToken, dict(row))

    async def create(
        self,
        user_id: int,
        label: str,
        prefix: str,
        token_hash: str,
        *,
        expires_at: datetime | None,
    ) -> ApiToken:
        """Insert a token; raise DuplicateError on a taken label or prefix."""
        with constraints_as_domain_errors("api token"):
            result = await self._execute(
                "INSERT INTO api_token (user_id, label, prefix, token_hash, created_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    label,
                    prefix,
                    token_hash,
                    self._now(),
                    None if expires_at is None else format_timestamp(expires_at),
                ),
            )
        return await self._created(result, self.get)

    async def get(self, token_id: int) -> ApiToken | None:
        """Fetch a token record by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (token_id,))

    async def get_by_prefix(self, prefix: str) -> ApiToken | None:
        """Fetch the record a presented token's prefix names."""
        return await self._one(f"{_SELECT} WHERE prefix = ?", (prefix,))

    async def label_exists(self, user_id: int, label: str) -> bool:
        """Report whether the user already has a token, live or revoked, with this label."""
        return (
            await self._session.fetch_one(
                "SELECT 1 AS found FROM api_token WHERE user_id = ? AND label = ?",
                (user_id, label),
            )
            is not None
        )

    async def list_for_user(
        self, user_id: int, *, limit: int, cursor: str | None = None
    ) -> Page[ApiToken]:
        """Return one page of a user's tokens, newest first."""
        return await self._page(
            _SELECT,
            limit=limit,
            cursor=cursor,
            sort_column="id",
            descending=True,
            where="user_id = ?",
            params=(user_id,),
        )

    async def revoke(self, token_id: int) -> bool:
        """Stamp a live token revoked; return False if it was already revoked or is gone."""
        result = await self._execute(
            "UPDATE api_token SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (self._now(), token_id),
        )
        return result.rowcount > 0

    async def touch(self, token_id: int, *, min_interval: timedelta) -> None:
        """Record use, skipping the write when the last record is recent enough."""
        await self._execute(
            "UPDATE api_token SET last_used_at = ? "
            "WHERE id = ? AND (last_used_at IS NULL OR last_used_at < ?)",
            (self._now(), token_id, self._before_now(min_interval)),
        )
