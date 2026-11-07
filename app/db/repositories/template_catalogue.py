"""Repositories for template_catalogue: reads, and the only writer."""

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from app.db.repositories.base import Repository
from app.db.repositories.clocked import ClockedRepository, format_timestamp, parse_timestamp
from app.db.repositories.errors import constraints_as_domain_errors
from app.db.repositories.pagination import Page

_SELECT = (
    "SELECT id, name, kind, url, is_enabled, content_digest, entry_count, fetched_at, "
    "last_failure, created_at, updated_at FROM template_catalogue"
)


@dataclass(frozen=True, slots=True)
class StoredCatalogue:
    """A catalogue as stored, without its content."""

    id: int
    name: str
    kind: str
    url: str
    is_enabled: bool
    content_digest: str | None
    entry_count: int
    fetched_at: str | None
    last_failure: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CatalogueFields:
    """The columns an administrator edits, already validated."""

    name: str
    kind: str
    url: str


def _map_catalogue(row: sqlite3.Row) -> StoredCatalogue:
    return StoredCatalogue(
        id=int(row["id"]),
        name=str(row["name"]),
        kind=str(row["kind"]),
        url=str(row["url"]),
        is_enabled=bool(row["is_enabled"]),
        content_digest=row["content_digest"],
        entry_count=int(row["entry_count"]),
        fetched_at=row["fetched_at"],
        last_failure=row["last_failure"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class TemplateCatalogueRepository(Repository[StoredCatalogue]):
    """Reads template catalogues."""

    def _map_row(self, row: sqlite3.Row) -> StoredCatalogue:
        return _map_catalogue(row)

    async def get(self, catalogue_id: int) -> StoredCatalogue | None:
        """Fetch a catalogue by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (catalogue_id,))

    async def page_catalogues(
        self, *, limit: int, cursor: str | None = None
    ) -> Page[StoredCatalogue]:
        """Return one page of catalogues, by name."""
        return await self._page(_SELECT, limit=limit, cursor=cursor, sort_column="name")

    async def count_catalogues(self) -> int:
        """Count every catalogue."""
        row = await self._session.fetch_one("SELECT COUNT(*) AS total FROM template_catalogue")
        return 0 if row is None else int(row["total"])

    async def enabled_with_content(self, *, limit: int) -> list[StoredCatalogue]:
        """List up to limit enabled catalogues that hold templates, by name."""
        rows = await self._session.fetch_limited(
            f"{_SELECT} WHERE is_enabled = 1 AND content_digest IS NOT NULL "
            "ORDER BY name, id LIMIT ?",
            (limit,),
            limit=limit,
        )
        return [_map_catalogue(row) for row in rows]

    async def content_of(self, catalogue_id: int, *, digest: str) -> str | None:
        """Return the stored templates JSON of a catalogue, only while it still has this digest."""
        row = await self._session.fetch_one(
            "SELECT content FROM template_catalogue WHERE id = ? AND content_digest = ?",
            (catalogue_id, digest),
        )
        return None if row is None or row["content"] is None else str(row["content"])


class TemplateCatalogueStore(ClockedRepository[StoredCatalogue]):
    """Writes template catalogues."""

    def _map_row(self, row: sqlite3.Row) -> StoredCatalogue:
        return _map_catalogue(row)

    async def add_catalogue(self, fields: CatalogueFields) -> int:
        """Insert a disabled catalogue; return its id. DuplicateError on a taken name."""
        now = self._now()
        with constraints_as_domain_errors("template catalogue"):
            result = await self._execute(
                "INSERT INTO template_catalogue (name, kind, url, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (fields.name, fields.kind, fields.url, now, now),
            )
        if result.last_row_id is None:
            raise RuntimeError("an insert did not report a row id")
        return result.last_row_id

    async def change_catalogue(
        self,
        catalogue_id: int,
        fields: CatalogueFields,
        *,
        discard_content: bool,
        expected_updated_at: str,
    ) -> bool:
        """Change a catalogue if it is unchanged since expected_updated_at.

        With discard_content the fetched templates go and the catalogue is disabled. False when
        the catalogue is gone or was changed meanwhile. DuplicateError on a taken name.
        """
        with constraints_as_domain_errors("template catalogue"):
            result = await self._execute(
                "UPDATE template_catalogue SET name = ?, kind = ?, url = ?, updated_at = ?, "
                "is_enabled = CASE WHEN ? THEN 0 ELSE is_enabled END, "
                "content = CASE WHEN ? THEN NULL ELSE content END, "
                "content_digest = CASE WHEN ? THEN NULL ELSE content_digest END, "
                "entry_count = CASE WHEN ? THEN 0 ELSE entry_count END, "
                "fetched_at = CASE WHEN ? THEN NULL ELSE fetched_at END, "
                "last_failure = CASE WHEN ? THEN NULL ELSE last_failure END "
                "WHERE id = ? AND updated_at = ?",
                (
                    fields.name,
                    fields.kind,
                    fields.url,
                    self._next_stamp(expected_updated_at),
                    *[int(discard_content)] * 6,
                    catalogue_id,
                    expected_updated_at,
                ),
            )
        return result.rowcount > 0

    async def disable_catalogue(self, catalogue_id: int, *, expected_updated_at: str) -> bool:
        """Disable a catalogue if it is unchanged since expected_updated_at; its content stays."""
        result = await self._execute(
            "UPDATE template_catalogue SET is_enabled = 0, updated_at = ? "
            "WHERE id = ? AND updated_at = ? AND is_enabled = 1",
            (self._next_stamp(expected_updated_at), catalogue_id, expected_updated_at),
        )
        return result.rowcount > 0

    async def store_fetch(
        self,
        catalogue_id: int,
        *,
        enable: bool,
        entry_count: int,
        content: str | None,
        digest: str,
        expected_updated_at: str,
    ) -> bool:
        """Record a successful fetch; content None keeps the stored templates.

        With enable the catalogue is also switched on. False when the catalogue is gone or was
        changed since expected_updated_at.
        """
        result = await self._execute(
            "UPDATE template_catalogue SET fetched_at = ?, last_failure = NULL, entry_count = ?, "
            "content = COALESCE(?, content), content_digest = ?, "
            "is_enabled = CASE WHEN ? THEN 1 ELSE is_enabled END, "
            "updated_at = CASE WHEN ? THEN ? ELSE updated_at END "
            "WHERE id = ? AND updated_at = ?",
            (
                self._now(),
                entry_count,
                content,
                digest,
                int(enable),
                int(enable),
                self._next_stamp(expected_updated_at),
                catalogue_id,
                expected_updated_at,
            ),
        )
        return result.rowcount > 0

    async def store_fetch_failure(
        self, catalogue_id: int, *, code: str, expected_updated_at: str
    ) -> bool:
        """Record the product code of a failed fetch; the stored templates stay."""
        result = await self._execute(
            "UPDATE template_catalogue SET last_failure = ? WHERE id = ? AND updated_at = ?",
            (code, catalogue_id, expected_updated_at),
        )
        return result.rowcount > 0

    async def remove_catalogue(self, catalogue_id: int, *, expected_name: str) -> bool:
        """Delete a catalogue only if its name is exactly expected_name; False otherwise."""
        result = await self._execute(
            "DELETE FROM template_catalogue WHERE id = ? AND name = ? COLLATE BINARY",
            (catalogue_id, expected_name),
        )
        return result.rowcount > 0

    def _next_stamp(self, expected_updated_at: str) -> str:
        now = self._now()
        if now > expected_updated_at:
            return now
        return format_timestamp(parse_timestamp(expected_updated_at) + timedelta(seconds=1))
