"""A statement-recording session and a reader for stored activity rows."""

import json
from typing import Any

from app.db.engine import Session, WriteResult

INSERT_COLUMNS = (
    "category",
    "actor_type",
    "actor_user_id",
    "actor_subsystem",
    "rule_id",
    "job_run_id",
    "request_id",
    "environment_id",
    "target_type",
    "target_id",
    "action",
    "result",
    "detail",
    "occurred_at",
)


class RecordingSession:
    """Stands in for a Session, keeping what a repository writes and queries."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.queries: list[tuple[str, list[Any]]] = []
        self.in_transaction = False

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> WriteResult:
        self.writes.append(dict(zip(INSERT_COLUMNS, params, strict=True)))
        return WriteResult(1, len(self.writes))

    async def fetch_limited(self, sql: str, params: list[Any], *, limit: int) -> list[Any]:
        self.queries.append((sql, list(params)))
        return []

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.queries.append((sql, list(params)))

    def stored_detail(self) -> dict[str, Any]:
        """Return the detail of the single row written, decoded."""
        [written] = self.writes
        return json.loads(written["detail"])  # type: ignore[no-any-return]


async def read_rows(db: Session) -> list[dict[str, Any]]:
    """Return every stored event, oldest first, with detail decoded."""
    rows = await db.fetch_limited(
        "SELECT id, category, actor_type, actor_user_id, actor_subsystem, rule_id, job_run_id, "
        "request_id, environment_id, target_type, target_id, action, result, detail "
        "FROM activity_event ORDER BY id",
        (),
        limit=1000,
    )
    return [
        {**dict(row), "detail": json.loads(row["detail"]) if row["detail"] else None}
        for row in rows
    ]
