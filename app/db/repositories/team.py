"""Repository for the team table."""

import json
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass

from app.db.repositories.base import like_prefix, row_as
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.errors import constraints_as_domain_errors
from app.db.repositories.pagination import Page

_SELECT = "SELECT id, name, created_at FROM team"


@dataclass(frozen=True, slots=True)
class Team:
    """A named group of users."""

    id: int
    name: str
    created_at: str


class TeamRepository(ClockedRepository[Team]):
    """Reads and writes teams; names are unique ignoring ASCII case."""

    def _map_row(self, row: sqlite3.Row) -> Team:
        return row_as(Team, dict(row))

    async def create(self, name: str) -> Team:
        """Insert a team; raise DuplicateError if the name exists in any case."""
        with constraints_as_domain_errors("team"):
            result = await self._execute(
                "INSERT INTO team (name, created_at) VALUES (?, ?)", (name, self._now())
            )
        return await self._created(result, self.get)

    async def get(self, team_id: int) -> Team | None:
        """Fetch a team by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (team_id,))

    async def get_by_name(self, name: str) -> Team | None:
        """Fetch a team by name, ignoring ASCII case."""
        return await self._one(f"{_SELECT} WHERE name = ? COLLATE NOCASE", (name,))

    async def team_names(self, team_ids: Collection[int]) -> dict[int, str]:
        """Map each id that names an existing team to its current name."""
        distinct = sorted(set(team_ids))
        if not distinct:
            return {}
        rows = await self._session.fetch_limited(
            "SELECT id, name FROM team WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(distinct),),
            limit=len(distinct),
        )
        return {int(row["id"]): str(row["name"]) for row in rows}

    async def list_page(
        self, *, limit: int, cursor: str | None = None, name_prefix: str | None = None
    ) -> Page[Team]:
        """Return one page of teams ordered by name, ignoring case; `name_prefix` narrows it."""
        if name_prefix is None:
            return await self._page(_SELECT, limit=limit, cursor=cursor, sort_column="name")
        return await self._page(
            _SELECT,
            limit=limit,
            cursor=cursor,
            sort_column="name",
            where="name LIKE ? ESCAPE '\\'",
            params=(like_prefix(name_prefix),),
        )

    async def list_page_for_user(
        self,
        user_id: int,
        *,
        limit: int,
        cursor: str | None = None,
        name_prefix: str | None = None,
    ) -> Page[Team]:
        """Return one page, ordered by name, of the teams a user currently belongs to."""
        where = "id IN (SELECT team_id FROM team_membership WHERE user_id = ?)"
        params: tuple[object, ...] = (user_id,)
        if name_prefix is not None:
            where += " AND name LIKE ? ESCAPE '\\'"
            params += (like_prefix(name_prefix),)
        return await self._page(
            "SELECT id, name, created_at FROM team",
            limit=limit,
            cursor=cursor,
            sort_column="name",
            where=where,
            params=params,
        )

    async def count(self) -> int:
        """Count every team."""
        row = await self._session.fetch_one("SELECT COUNT(*) AS total FROM team")
        return 0 if row is None else int(row["total"])

    async def count_for_user(self, user_id: int) -> int:
        """Count the teams a user currently belongs to."""
        row = await self._session.fetch_one(
            "SELECT COUNT(*) AS total FROM team "
            "WHERE id IN (SELECT team_id FROM team_membership WHERE user_id = ?)",
            (user_id,),
        )
        return 0 if row is None else int(row["total"])

    async def rename(self, team_id: int, name: str) -> bool:
        """Rename a team; return False if it does not exist, raise DuplicateError on a clash."""
        with constraints_as_domain_errors("team"):
            result = await self._execute("UPDATE team SET name = ? WHERE id = ?", (name, team_id))
        return result.rowcount > 0

    async def delete(self, team_id: int) -> bool:
        """Delete a team and, by cascade, its memberships; return False if it does not exist."""
        result = await self._execute("DELETE FROM team WHERE id = ?", (team_id,))
        return result.rowcount > 0
