"""Repository for the environment_group table."""

import sqlite3
from dataclasses import dataclass

from app.db.repositories.base import Repository, row_as
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.environment_access import environment_visible_clause
from app.db.repositories.errors import constraints_as_domain_errors
from app.db.repositories.pagination import MAX_PAGE_SIZE, Page

MAX_GROUP_OPTIONS = 500

_SELECT = "SELECT id, name, description, created_at FROM environment_group"


@dataclass(frozen=True, slots=True)
class EnvironmentGroup:
    """A named collection of environments."""

    id: int
    name: str
    description: str | None
    created_at: str


class EnvironmentGroupRepository(ClockedRepository[EnvironmentGroup]):
    """Reads and writes environment groups; names are unique ignoring ASCII case."""

    def _map_row(self, row: sqlite3.Row) -> EnvironmentGroup:
        return row_as(EnvironmentGroup, dict(row))

    async def create(self, name: str, description: str | None = None) -> EnvironmentGroup:
        """Insert a group; raise DuplicateError if the name exists in any case."""
        with constraints_as_domain_errors("environment group"):
            result = await self._execute(
                "INSERT INTO environment_group (name, description, created_at) VALUES (?, ?, ?)",
                (name, description, self._now()),
            )
        return await self._created(result, self.get)

    async def get(self, group_id: int) -> EnvironmentGroup | None:
        """Fetch a group by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (group_id,))

    async def get_by_name(self, name: str) -> EnvironmentGroup | None:
        """Fetch a group by name, ignoring ASCII case."""
        return await self._one(f"{_SELECT} WHERE name = ? COLLATE NOCASE", (name,))

    async def list_page(self, *, limit: int, cursor: str | None = None) -> Page[EnvironmentGroup]:
        """Return one page of groups ordered by name, ignoring case."""
        return await self._page(_SELECT, limit=limit, cursor=cursor, sort_column="name")

    async def update(self, group_id: int, *, name: str, description: str | None) -> bool:
        """Replace a group's name and description; False if it does not exist."""
        with constraints_as_domain_errors("environment group"):
            result = await self._execute(
                "UPDATE environment_group SET name = ?, description = ? WHERE id = ?",
                (name, description, group_id),
            )
        return result.rowcount > 0

    async def delete(self, group_id: int) -> bool:
        """Delete a group, leaving its environments ungrouped; False if it does not exist."""
        result = await self._execute("DELETE FROM environment_group WHERE id = ?", (group_id,))
        return result.rowcount > 0

    async def find_by_folded_name(
        self, name: str, *, excluding: int | None = None
    ) -> EnvironmentGroup | None:
        """Find a group whose name equals this one under full Unicode case folding.

        The column folds ASCII only, so a name differing in the case of a non-ASCII letter passes
        the unique index; this closes that gap. Walks the groups a page at a time.
        """
        folded = name.casefold()
        cursor: str | None = None
        while True:
            page = await self.list_page(limit=MAX_PAGE_SIZE, cursor=cursor)
            for group in page.items:
                if group.id != excluding and group.name.casefold() == folded:
                    return group
            if page.next_cursor is None:
                return None
            cursor = page.next_cursor

    async def list_options(self, *, visible_to: int | None, limit: int) -> list[EnvironmentGroup]:
        """List groups by name; for a standard user, only those holding an environment they see."""
        if visible_to is None:
            rows = await self._session.fetch_limited(
                f"{_SELECT} ORDER BY name, id LIMIT ?", (limit,), limit=limit
            )
            return [self._map_row(row) for row in rows]
        fragment = environment_visible_clause(visible_to, "e.id", "e.environment_group_id")
        rows = await self._session.fetch_limited(
            "SELECT g.id AS id, g.name AS name, g.description AS description, "
            "g.created_at AS created_at FROM environment_group g "
            "WHERE EXISTS (SELECT 1 FROM environment e "
            f"WHERE e.environment_group_id = g.id AND {fragment.clause}) "
            "ORDER BY g.name, g.id LIMIT ?",
            (*fragment.params, limit),
            limit=limit,
        )
        return [self._map_row(row) for row in rows]


@dataclass(frozen=True, slots=True)
class GroupSummary:
    """A group with the number of environments in it."""

    id: int
    name: str
    description: str | None
    created_at: str
    environment_count: int


class GroupSummaryRepository(Repository[GroupSummary]):
    """Lists groups together with how many environments each holds."""

    def _map_row(self, row: sqlite3.Row) -> GroupSummary:
        return row_as(GroupSummary, dict(row))

    async def list_page(self, *, limit: int, cursor: str | None = None) -> Page[GroupSummary]:
        """Return one page of groups ordered by name, ignoring case."""
        return await self._page(
            "SELECT g.id AS id, g.name AS name, g.description AS description, "
            "g.created_at AS created_at, (SELECT count(*) FROM environment e "
            "WHERE e.environment_group_id = g.id) AS environment_count FROM environment_group g",
            limit=limit,
            cursor=cursor,
            sort_column="g.name",
            id_column="g.id",
        )

    async def count(self) -> int:
        """Count every group."""
        row = await self._session.fetch_one("SELECT COUNT(*) AS total FROM environment_group")
        return 0 if row is None else int(row["total"])

    async def get(self, group_id: int) -> GroupSummary | None:
        """Fetch one group with its environment count."""
        return await self._one(
            "SELECT g.id AS id, g.name AS name, g.description AS description, "
            "g.created_at AS created_at, (SELECT count(*) FROM environment e "
            "WHERE e.environment_group_id = g.id) AS environment_count "
            "FROM environment_group g WHERE g.id = ?",
            (group_id,),
        )
