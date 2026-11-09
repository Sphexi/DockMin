"""Repository for backup_record: one row per archive produced, kept to the newest few hundred."""

import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final, Literal, cast

from app.db.engine import DatabaseError
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.pagination import Page

type BackupOrigin = Literal["download", "sync"]
type BackupOutcome = Literal["success", "failure"]

KEPT_BACKUP_RECORDS: Final = 500
DESTINATION_CHUNK: Final = 100

_SELECT = (
    "SELECT id, origin, object_storage_target_id, created_at, size_bytes, key_version, "
    "key_fingerprint, destination_key, archive_sha256, schema_version, outcome, error "
    "FROM backup_record"
)


@dataclass(frozen=True, slots=True)
class BackupRecord:
    """One produced archive, or one failed attempt to produce or store it."""

    id: int
    origin: BackupOrigin
    object_storage_target_id: int | None
    created_at: str
    size_bytes: int | None
    key_version: int
    key_fingerprint: str
    destination_key: str | None
    archive_sha256: str | None
    schema_version: int | None
    outcome: BackupOutcome
    error: str | None


class BackupRecordRepository(ClockedRepository[BackupRecord]):
    """Writes and pages backup records."""

    def _map_row(self, row: sqlite3.Row) -> BackupRecord:
        return BackupRecord(
            id=int(row["id"]),
            origin=cast(BackupOrigin, row["origin"]),
            object_storage_target_id=row["object_storage_target_id"],
            created_at=str(row["created_at"]),
            size_bytes=row["size_bytes"],
            key_version=int(row["key_version"]),
            key_fingerprint=str(row["key_fingerprint"]),
            destination_key=row["destination_key"],
            archive_sha256=row["archive_sha256"],
            schema_version=row["schema_version"],
            outcome=cast(BackupOutcome, row["outcome"]),
            error=row["error"],
        )

    async def add_backup_record(
        self,
        *,
        origin: BackupOrigin,
        target_id: int | None,
        size_bytes: int | None,
        key_version: int,
        key_fingerprint: str,
        destination_key: str | None,
        archive_sha256: str | None,
        schema_version: int | None,
        outcome: BackupOutcome,
        error: str | None,
    ) -> int:
        """Insert a record and delete all but the newest KEPT_BACKUP_RECORDS; return its id."""
        if origin == "download" and target_id is not None:
            raise ValueError("a downloaded archive has no object storage target")
        async with self._atomic():
            result = await self._execute(
                "INSERT INTO backup_record (origin, object_storage_target_id, created_at, "
                "size_bytes, key_version, key_fingerprint, destination_key, archive_sha256, "
                "schema_version, outcome, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    origin,
                    target_id,
                    self._now(),
                    size_bytes,
                    key_version,
                    key_fingerprint,
                    destination_key,
                    archive_sha256,
                    schema_version,
                    outcome,
                    error,
                ),
            )
            if result.last_row_id is None:
                raise DatabaseError("an insert did not report a row id")
            await self._execute(
                "DELETE FROM backup_record WHERE id IN (SELECT id FROM backup_record "
                "ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?)",
                (KEPT_BACKUP_RECORDS,),
            )
        return result.last_row_id

    async def page_backup_records(
        self, *, limit: int, cursor: str | None = None
    ) -> Page[BackupRecord]:
        """Return one page of records, newest first."""
        return await self._page(
            _SELECT, limit=limit, cursor=cursor, sort_column="created_at", descending=True
        )

    async def get(self, record_id: int) -> BackupRecord | None:
        """Fetch a record by id; None once it has left the kept set."""
        return await self._one(f"{_SELECT} WHERE id = ?", (record_id,))

    async def by_destinations(self, target_id: int, keys: Sequence[str]) -> dict[str, BackupRecord]:
        """Return the newest successful record of each key stored on the target, by key."""
        found: dict[str, BackupRecord] = {}
        for start in range(0, len(keys), DESTINATION_CHUNK):
            chunk = keys[start : start + DESTINATION_CHUNK]
            marks = ", ".join("?" for _ in chunk)
            rows = await self._session.fetch_limited(
                f"{_SELECT} WHERE object_storage_target_id = ? AND outcome = 'success' "
                f"AND destination_key IN ({marks}) ORDER BY created_at DESC, id DESC",
                (target_id, *chunk),
                limit=KEPT_BACKUP_RECORDS,
            )
            for row in rows:
                record = self._map_row(row)
                if record.destination_key is not None:
                    found.setdefault(record.destination_key, record)
        return found

    async def latest_success_at(self) -> str | None:
        """Return the stored time of the newest record with a successful outcome, if any."""
        row = await self._session.fetch_one(
            "SELECT created_at FROM backup_record WHERE outcome = 'success' "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        )
        return None if row is None else str(row["created_at"])

    @asynccontextmanager
    async def _atomic(self) -> AsyncIterator[None]:
        if self._session.in_transaction:
            async with self._session.savepoint():
                yield
        else:
            async with self._session.transaction():
                yield
