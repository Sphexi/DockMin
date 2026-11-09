"""Migration 0032: the index that reads one request id newest first."""

import sqlite3
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import migrate
from tests.db.backup_support import migrations_up_to
from tests.db.helpers import CREATED_STAMP

PREVIOUS_VERSION = 31
INDEX = "idx_activity_event_request_id"
INSERT_USER = (
    "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
    "VALUES ('admin', 'h', 'administrator', 1, ?)"
)
INSERT_EVENT = (
    "INSERT INTO activity_event (category, actor_type, actor_user_id, request_id, target_type, "
    "target_id, action, result, occurred_at) "
    "VALUES ('user_action', 'user', 1, ?, 'container', 'c', 'container_started', 'success', ?)"
)
ALL_ROWS = "SELECT * FROM activity_event ORDER BY id"
INDEX_SQL = "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?"
REQUEST_PLAN = (
    "EXPLAIN QUERY PLAN SELECT id FROM activity_event WHERE request_id = ? "
    "ORDER BY occurred_at DESC, id DESC"
)


@pytest.fixture
def previous(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "dockmin.db")
    migrate(connection, migrations_up_to(PREVIOUS_VERSION, tmp_path / "previous"))
    connection.execute(INSERT_USER, (CREATED_STAMP,))
    connection.executemany(INSERT_EVENT, [("req-1", CREATED_STAMP), (None, CREATED_STAMP)])
    return connection


def test_the_migration_adds_a_partial_index_and_changes_no_row(
    previous: sqlite3.Connection,
) -> None:
    before = [tuple(row) for row in previous.execute(ALL_ROWS)]
    absent = previous.execute(INDEX_SQL, (INDEX,)).fetchone()

    migrate(previous)

    after = [tuple(row) for row in previous.execute(ALL_ROWS)]
    [(sql,)] = previous.execute(INDEX_SQL, (INDEX,)).fetchall()
    assert absent is None
    assert after == before
    assert "request_id IS NOT NULL" in sql


def test_a_read_by_request_id_uses_the_index_after_the_migration(
    previous: sqlite3.Connection,
) -> None:
    migrate(previous)

    plan = [row["detail"] for row in previous.execute(REQUEST_PLAN, ("req-1",))]

    assert any(INDEX in step for step in plan)
