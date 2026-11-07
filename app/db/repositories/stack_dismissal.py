"""Repository for stack_dismissal: unmanaged stack entries an administrator has dismissed."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.errors import constraints_as_domain_errors
from app.db.repositories.resource_control import canonical_stack_key
from app.db.repositories.stack import StackKind

MAX_DISMISSALS: Final = 5000

_SELECT: Final = (
    "SELECT d.project_key, d.objects, d.dismissed_by_user_id, d.dismissed_at, "
    "u.username AS dismissed_by FROM stack_dismissal AS d "
    "LEFT JOIN user AS u ON u.id = d.dismissed_by_user_id"
)


@dataclass(frozen=True, slots=True)
class StoredDismissal:
    """A dismissal as stored; `dismissed_by` is the user's current name, None once deleted."""

    project_key: str
    objects: tuple[str, ...]
    dismissed_by_user_id: int | None
    dismissed_by: str | None
    dismissed_at: str


class StackDismissalRepository(ClockedRepository[StoredDismissal]):
    """Reads and writes dismissals, keyed by environment, kind and folded stack name."""

    def _map_row(self, row: sqlite3.Row) -> StoredDismissal:
        user_id = row["dismissed_by_user_id"]
        username = row["dismissed_by"]
        return StoredDismissal(
            project_key=str(row["project_key"]),
            objects=tuple(str(item) for item in json.loads(str(row["objects"]))),
            dismissed_by_user_id=None if user_id is None else int(user_id),
            dismissed_by=None if username is None else str(username),
            dismissed_at=str(row["dismissed_at"]),
        )

    async def dismissals(self, environment_id: int, kind: StackKind) -> list[StoredDismissal]:
        """List the dismissals of one kind in an environment, by key, at most MAX_DISMISSALS."""
        rows = await self._session.fetch_limited(
            f"{_SELECT} WHERE d.environment_id = ? AND d.kind = ? ORDER BY d.project_key LIMIT ?",
            (environment_id, kind, MAX_DISMISSALS),
            limit=MAX_DISMISSALS,
        )
        return [self._map_row(row) for row in rows]

    async def dismissal(
        self, environment_id: int, kind: StackKind, key: str
    ) -> StoredDismissal | None:
        """Fetch the dismissal of one stack, or None."""
        return await self._one(
            f"{_SELECT} WHERE d.environment_id = ? AND d.kind = ? AND d.project_key = ?",
            (environment_id, kind, canonical_stack_key(key)),
        )

    async def put_dismissal(
        self,
        environment_id: int,
        kind: StackKind,
        key: str,
        objects: Sequence[str],
        user_id: int | None,
    ) -> None:
        """Store the dismissal of a stack, replacing one already there.

        The key is written folded and the objects sorted. Raises MissingReferenceError when the
        environment or the user does not exist.
        """
        with constraints_as_domain_errors("stack dismissal"):
            await self._execute(
                "INSERT INTO stack_dismissal (environment_id, kind, project_key, objects, "
                "dismissed_by_user_id, dismissed_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (environment_id, kind, project_key) DO UPDATE SET "
                "objects = excluded.objects, "
                "dismissed_by_user_id = excluded.dismissed_by_user_id, "
                "dismissed_at = excluded.dismissed_at",
                (
                    environment_id,
                    kind,
                    canonical_stack_key(key),
                    json.dumps(sorted(set(objects))),
                    user_id,
                    self._now(),
                ),
            )

    async def drop_dismissal(self, environment_id: int, kind: StackKind, key: str) -> bool:
        """Delete the dismissal of a stack; False when there was none."""
        result = await self._execute(
            "DELETE FROM stack_dismissal WHERE environment_id = ? AND kind = ? AND project_key = ?",
            (environment_id, kind, canonical_stack_key(key)),
        )
        return result.rowcount > 0
