"""Repository for activity_export_cursor: how far each exporter rule has copied the log."""

import sqlite3
from dataclasses import dataclass

from app.db.repositories.base import row_as
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.errors import constraints_as_domain_errors


@dataclass(frozen=True, slots=True)
class ExportCursor:
    """The last event id an exporter rule has copied, and the object that holds it."""

    rule_id: int
    last_event_id: int
    object_key: str | None
    exported_at: str | None


class ActivityExportCursorRepository(ClockedRepository[ExportCursor]):
    """Reads and moves export cursors; a cursor goes when its rule is deleted."""

    def _map_row(self, row: sqlite3.Row) -> ExportCursor:
        return row_as(ExportCursor, dict(row))

    async def get(self, rule_id: int) -> ExportCursor | None:
        """Fetch a rule's cursor; None when the rule has exported nothing."""
        return await self._one(
            "SELECT rule_id, last_event_id, object_key, exported_at FROM activity_export_cursor "
            "WHERE rule_id = ?",
            (rule_id,),
        )

    async def move_export_cursor(
        self, rule_id: int, *, last_event_id: int, object_key: str
    ) -> None:
        """Move a rule's cursor forward to last_event_id; a lower id leaves it where it is.

        Raises MissingReferenceError when the rule does not exist.
        """
        with constraints_as_domain_errors("activity export cursor"):
            await self._execute(
                "INSERT INTO activity_export_cursor (rule_id, last_event_id, object_key, "
                "exported_at) VALUES (?, ?, ?, ?) ON CONFLICT (rule_id) DO UPDATE SET "
                "last_event_id = excluded.last_event_id, object_key = excluded.object_key, "
                "exported_at = excluded.exported_at "
                "WHERE excluded.last_event_id >= activity_export_cursor.last_event_id",
                (rule_id, last_event_id, object_key, self._now()),
            )
