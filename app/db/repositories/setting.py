"""Repository for the setting table."""

import sqlite3
from dataclasses import dataclass
from typing import Final, Literal

from app.db.repositories.base import row_as
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.pagination import Page

SettingValueType = Literal["string", "integer", "boolean", "json"]

RESTORE_AUTOMATION_PAUSED_KEY: Final = "restore.automation_paused"

_SELECT = "SELECT id, key, value_type, value, updated_at FROM setting"


@dataclass(frozen=True, slots=True)
class Setting:
    """A stored configuration value; value is the raw text for its value_type."""

    id: int
    key: str
    value_type: SettingValueType
    value: str
    updated_at: str


class SettingRepository(ClockedRepository[Setting]):
    """Reads and writes settings, keyed by their unique key."""

    def _map_row(self, row: sqlite3.Row) -> Setting:
        return row_as(Setting, dict(row))

    async def get(self, key: str) -> Setting | None:
        """Fetch a setting by key; the match is case-sensitive."""
        return await self._one(f"{_SELECT} WHERE key = ?", (key,))

    async def put(self, key: str, value_type: SettingValueType, value: str) -> Setting:
        """Create the setting or replace its type and value."""
        await self._execute(
            "INSERT INTO setting (key, value_type, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value_type = excluded.value_type, "
            "value = excluded.value, updated_at = excluded.updated_at",
            (key, value_type, value, self._now()),
        )
        return self._required(await self.get(key))

    async def list_page(self, *, limit: int, cursor: str | None = None) -> Page[Setting]:
        """Return one page of settings ordered by key."""
        return await self._page(_SELECT, limit=limit, cursor=cursor, sort_column="key")

    async def delete(self, key: str) -> bool:
        """Delete a setting; return False if no such key exists."""
        result = await self._execute("DELETE FROM setting WHERE key = ?", (key,))
        return result.rowcount > 0
