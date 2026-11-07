"""Repository for image_update_status: the only writer of the table."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from app.db.repositories.base import Repository

MAX_LOOKUP_REFERENCES: Final = 400


@dataclass(frozen=True, slots=True)
class StoredUpdateStatus:
    """The latest update check result of one image reference, as stored."""

    reference: str
    state: str
    reason: str | None
    local_digest: str | None
    remote_digest: str | None
    checked_at: str


class ImageUpdateStatusRepository(Repository[StoredUpdateStatus]):
    """Reads and writes the latest update check result of image references, per environment."""

    def _map_row(self, row: sqlite3.Row) -> StoredUpdateStatus:
        return StoredUpdateStatus(
            reference=str(row["image_reference"]),
            state=str(row["state"]),
            reason=row["reason"],
            local_digest=row["local_digest"],
            remote_digest=row["remote_digest"],
            checked_at=str(row["checked_at"]),
        )

    async def upsert(
        self,
        environment_id: int,
        statuses: Sequence[StoredUpdateStatus],
        *,
        prune_before: str,
    ) -> None:
        """Upsert the results and delete the environment's rows checked before prune_before.

        Opens its own transaction, so the caller must not be inside one. A result older than the
        row it would replace is dropped.
        """
        async with self._session.transaction():
            for status in statuses:
                await self._execute(
                    "INSERT INTO image_update_status (environment_id, image_reference, state, "
                    "reason, local_digest, remote_digest, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (environment_id, image_reference) DO UPDATE SET "
                    "state = excluded.state, reason = excluded.reason, "
                    "local_digest = excluded.local_digest, remote_digest = excluded.remote_digest, "
                    "checked_at = excluded.checked_at "
                    "WHERE excluded.checked_at >= image_update_status.checked_at",
                    (
                        environment_id,
                        status.reference,
                        status.state,
                        status.reason,
                        status.local_digest,
                        status.remote_digest,
                        status.checked_at,
                    ),
                )
            await self._execute(
                "DELETE FROM image_update_status WHERE environment_id = ? AND checked_at < ?",
                (environment_id, prune_before),
            )

    async def for_references(
        self, environment_id: int, keys: Sequence[str]
    ) -> dict[str, StoredUpdateStatus]:
        """Fetch the stored results that exist among the references, keyed by reference."""
        found: dict[str, StoredUpdateStatus] = {}
        distinct = list(dict.fromkeys(keys))
        for start in range(0, len(distinct), MAX_LOOKUP_REFERENCES):
            chunk = distinct[start : start + MAX_LOOKUP_REFERENCES]
            marks = ", ".join("?" for _ in chunk)
            rows = await self._session.fetch_limited(
                "SELECT image_reference, state, reason, local_digest, remote_digest, checked_at "
                "FROM image_update_status WHERE environment_id = ? "
                f"AND image_reference IN ({marks})",
                (environment_id, *chunk),
                limit=len(chunk),
            )
            for row in rows:
                status = self._map_row(row)
                found[status.reference] = status
        return found
