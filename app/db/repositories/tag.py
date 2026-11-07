"""Repository for the tag table."""

import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from app.db.repositories.base import Repository, row_as
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.environment_access import environment_visible_clause
from app.db.repositories.errors import constraints_as_domain_errors
from app.db.repositories.pagination import Page

MAX_TAGS_PER_ENVIRONMENT = 20
MAX_TAG_OPTIONS = 500

_SELECT = "SELECT id, name, created_at FROM tag"


@dataclass(frozen=True, slots=True)
class Tag:
    """A label attachable to environments."""

    id: int
    name: str
    created_at: str


class TagRepository(ClockedRepository[Tag]):
    """Reads and writes tags; names are unique ignoring ASCII case."""

    def _map_row(self, row: sqlite3.Row) -> Tag:
        return row_as(Tag, dict(row))

    async def create(self, name: str) -> Tag:
        """Insert a tag; raise DuplicateError if the name exists in any case."""
        with constraints_as_domain_errors("tag"):
            result = await self._execute(
                "INSERT INTO tag (name, created_at) VALUES (?, ?)", (name, self._now())
            )
        return await self._created(result, self.get)

    async def get(self, tag_id: int) -> Tag | None:
        """Fetch a tag by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (tag_id,))

    async def get_by_name(self, name: str) -> Tag | None:
        """Fetch a tag by name, ignoring ASCII case."""
        return await self._one(f"{_SELECT} WHERE name = ? COLLATE NOCASE", (name,))

    async def list_page(self, *, limit: int, cursor: str | None = None) -> Page[Tag]:
        """Return one page of tags ordered by name, ignoring case."""
        return await self._page(_SELECT, limit=limit, cursor=cursor, sort_column="name")

    async def rename(self, tag_id: int, name: str) -> bool:
        """Rename a tag; return False if it does not exist, raise DuplicateError on a clash."""
        with constraints_as_domain_errors("tag"):
            result = await self._execute("UPDATE tag SET name = ? WHERE id = ?", (name, tag_id))
        return result.rowcount > 0

    async def delete(self, tag_id: int) -> bool:
        """Delete a tag and, by cascade, its environment links; False if it does not exist."""
        result = await self._execute("DELETE FROM tag WHERE id = ?", (tag_id,))
        return result.rowcount > 0

    async def get_many(self, tag_ids: Collection[int]) -> list[Tag]:
        """Fetch the tags that exist among the ids, by name."""
        ids = sorted(set(tag_ids))
        if not ids:
            return []
        marks = ", ".join("?" for _ in ids)
        rows = await self._session.fetch_limited(
            f"{_SELECT} WHERE id IN ({marks}) ORDER BY name, id LIMIT ?",
            (*ids, len(ids)),
            limit=len(ids),
        )
        return [self._map_row(row) for row in rows]

    async def list_for_environment(self, environment_id: int, *, limit: int) -> list[Tag]:
        """List an environment's tags by name."""
        rows = await self._session.fetch_limited(
            "SELECT t.id AS id, t.name AS name, t.created_at AS created_at FROM environment_tag et "
            "JOIN tag t ON t.id = et.tag_id WHERE et.environment_id = ? ORDER BY t.name, t.id "
            "LIMIT ?",
            (environment_id, limit),
            limit=limit,
        )
        return [self._map_row(row) for row in rows]

    async def tags_for(self, environment_ids: Sequence[int]) -> dict[int, list[Tag]]:
        """Return each listed environment's tags by name; environments with none are absent."""
        ids = list(dict.fromkeys(environment_ids))
        if not ids:
            return {}
        marks = ", ".join("?" for _ in ids)
        bound = len(ids) * MAX_TAGS_PER_ENVIRONMENT
        rows = await self._session.fetch_limited(
            "SELECT et.environment_id AS environment_id, t.id AS id, t.name AS name, "
            "t.created_at AS created_at FROM environment_tag et JOIN tag t ON t.id = et.tag_id "
            f"WHERE et.environment_id IN ({marks}) ORDER BY et.environment_id, t.name, t.id "
            "LIMIT ?",
            (*ids, bound),
            limit=bound,
        )
        grouped: dict[int, list[Tag]] = {}
        for row in rows:
            tag = Tag(int(row["id"]), str(row["name"]), str(row["created_at"]))
            grouped.setdefault(int(row["environment_id"]), []).append(tag)
        return grouped

    async def replace_for_environment(
        self, environment_id: int, tag_ids: Collection[int]
    ) -> tuple[list[int], list[int]]:
        """Make the environment carry exactly these tags; return the ids added and removed.

        MissingReferenceError if a tag or the environment does not exist.
        """
        wanted = set(tag_ids)
        rows = await self._session.fetch_limited(
            "SELECT tag_id FROM environment_tag WHERE environment_id = ? LIMIT ?",
            (environment_id, MAX_TAGS_PER_ENVIRONMENT * 2),
            limit=MAX_TAGS_PER_ENVIRONMENT * 2,
        )
        held = {int(row["tag_id"]) for row in rows}
        removed = sorted(held - wanted)
        added = sorted(wanted - held)
        for tag_id in removed:
            await self._execute(
                "DELETE FROM environment_tag WHERE environment_id = ? AND tag_id = ?",
                (environment_id, tag_id),
            )
        with constraints_as_domain_errors("tag"):
            for tag_id in added:
                await self._execute(
                    "INSERT INTO environment_tag (environment_id, tag_id) VALUES (?, ?)",
                    (environment_id, tag_id),
                )
        return added, removed

    async def list_environments(self, tag_id: int, *, limit: int) -> list[tuple[int, str]]:
        """List (id, name) of the environments carrying a tag, by name; at most limit."""
        rows = await self._session.fetch_limited(
            "SELECT e.id AS id, e.name AS name FROM environment_tag et "
            "JOIN environment e ON e.id = et.environment_id WHERE et.tag_id = ? "
            "ORDER BY e.name, e.id LIMIT ?",
            (tag_id, limit),
            limit=limit,
        )
        return [(int(row["id"]), str(row["name"])) for row in rows]

    async def count_environments(self, tag_id: int) -> int:
        """Count the environments carrying a tag."""
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM environment_tag WHERE tag_id = ?", (tag_id,)
        )
        assert row is not None
        return int(row["total"])

    async def list_options(self, *, visible_to: int | None, limit: int) -> list[Tag]:
        """List tags by name; for a standard user, only those on an environment they may see."""
        if visible_to is None:
            rows = await self._session.fetch_limited(
                f"{_SELECT} ORDER BY name, id LIMIT ?", (limit,), limit=limit
            )
            return [self._map_row(row) for row in rows]
        fragment = environment_visible_clause(visible_to, "e.id", "e.environment_group_id")
        rows = await self._session.fetch_limited(
            "SELECT t.id AS id, t.name AS name, t.created_at AS created_at FROM tag t "
            "WHERE EXISTS (SELECT 1 FROM environment_tag et "
            "JOIN environment e ON e.id = et.environment_id "
            f"WHERE et.tag_id = t.id AND {fragment.clause}) ORDER BY t.name, t.id LIMIT ?",
            (*fragment.params, limit),
            limit=limit,
        )
        return [self._map_row(row) for row in rows]


@dataclass(frozen=True, slots=True)
class TagSummary:
    """A tag with the number of environments carrying it."""

    id: int
    name: str
    created_at: str
    environment_count: int


class TagSummaryRepository(Repository[TagSummary]):
    """Lists tags together with how many environments carry each."""

    def _map_row(self, row: sqlite3.Row) -> TagSummary:
        return row_as(TagSummary, dict(row))

    async def list_page(self, *, limit: int, cursor: str | None = None) -> Page[TagSummary]:
        """Return one page of tags ordered by name, ignoring case."""
        return await self._page(
            "SELECT t.id AS id, t.name AS name, t.created_at AS created_at, "
            "(SELECT count(*) FROM environment_tag et WHERE et.tag_id = t.id) "
            "AS environment_count FROM tag t",
            limit=limit,
            cursor=cursor,
            sort_column="t.name",
            id_column="t.id",
        )
