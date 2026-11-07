"""Migration 0014: the rebuilt activity table, its widened actor check and the export cursor."""

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import MIGRATIONS_DIR, migrate
from tests.db.helpers import CREATED_STAMP

MIGRATION = "0014_activity_retention.sql"
CHECK_FAILED = "CHECK constraint failed"
FOREIGN_KEY_FAILED = "FOREIGN KEY constraint failed"
PRUNER = "activity-log-pruner"
EXPORTER = "activity-log-exporter"
HIGHEST_ID = 1000
ACTIVITY_INDEXES = {
    "idx_activity_event_action",
    "idx_activity_event_actor_subsystem",
    "idx_activity_event_actor_user_id",
    "idx_activity_event_category",
    "idx_activity_event_environment",
    "idx_activity_event_job_run_id",
    "idx_activity_event_occurred_at",
    "idx_activity_event_rule_id",
    "idx_activity_event_target",
}

INSERT_EVENT = (
    "INSERT INTO activity_event (id, category, actor_type, actor_user_id, actor_subsystem, "
    "rule_id, job_run_id, environment_id, target_type, target_id, action, result, detail, "
    "occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
INSERT_PRUNER_EVENT = (
    "INSERT INTO activity_event (category, actor_type, actor_subsystem, target_type, action, "
    "result, occurred_at) VALUES ('automation', 'subsystem', ?, 'activity_log', "
    "'activity_log_pruned', 'success', ?)"
)
INSERT_ANONYMOUS_EVENT = (
    "INSERT INTO activity_event (category, actor_type, target_type, action, result, occurred_at) "
    "VALUES ('authentication', 'anonymous', 'user', 'login_failed', 'failure', ?)"
)
INSERT_CURSOR = (
    "INSERT INTO activity_export_cursor (rule_id, last_event_id, object_key, exported_at) "
    "VALUES (?, ?, 'exports/a.csv.gz', ?)"
)
# id, category, actor type, user, subsystem, rule, run, environment, target type, target id,
# action, result, detail, time
PRE_MIGRATION_EVENTS = [
    (
        1,
        "authentication",
        "anonymous",
        None,
        None,
        None,
        None,
        None,
        "user",
        None,
        "login_failed",
        "failure",
        '{"username":"=x,\\"y\\"\\nz\\u202e"}',
        "2029-01-01T00:00:00Z",
    ),
    (
        2,
        "user_action",
        "user",
        1,
        None,
        None,
        None,
        1,
        "container",
        "c1",
        "container_started",
        "success",
        None,
        "2029-01-02T00:00:00Z",
    ),
    (
        5,
        "automation",
        "subsystem",
        None,
        "container-updater",
        1,
        7,
        1,
        "container",
        "c1",
        "container_recreated",
        "failure",
        '{"rule_id":1,"rule_name":"r1"}',
        "2029-01-03T00:00:00Z",
    ),
    (
        6,
        "user_action",
        "user",
        2,
        None,
        1,
        None,
        1,
        "automation_rule",
        "1",
        "automation_rule_updated",
        "success",
        '{"k":"\\u00e9"}',
        "2029-01-04T00:00:00Z",
    ),
    (
        900,
        "automation",
        "subsystem",
        None,
        "environment-snapshot-refresher",
        None,
        None,
        2,
        "environment",
        "2",
        "x",
        "success",
        "not json at all",
        "2029-01-05T00:00:00Z",
    ),
    (
        HIGHEST_ID,
        "user_action",
        "user",
        1,
        None,
        None,
        None,
        None,
        "environment",
        "2",
        "environment_updated",
        "success",
        "{}",
        "2029-01-06T00:00:00Z",
    ),
]

REFUSED_ACTORS = [
    pytest.param("scheduler", id="scheduler"),
    pytest.param("container-restarter", id="unknown-subsystem"),
]
REFUSED_CURSORS = [
    pytest.param(
        "UPDATE activity_export_cursor SET last_event_id = -1", id="negative-last-event-id"
    ),
    pytest.param("UPDATE activity_export_cursor SET object_key = ''", id="empty-key"),
    pytest.param(
        "UPDATE activity_export_cursor SET object_key = printf('%.1025c', 'a')", id="long-key"
    ),
    pytest.param(
        "INSERT INTO activity_export_cursor (rule_id, last_event_id) VALUES (99, 0)",
        id="missing-rule",
    ),
]


def rows_of(connection: sqlite3.Connection, statement: str) -> list[tuple[object, ...]]:
    return [tuple(row) for row in connection.execute(statement)]


def index_sql(connection: sqlite3.Connection) -> dict[str, str]:
    found = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'activity_event'"
    )
    return {row["name"]: " ".join(row["sql"].split()) for row in found}


@pytest.fixture
def before(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    """A database migrated up to 0013, and the directory holding those migrations."""
    directory = tmp_path / "migrations"
    directory.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name < MIGRATION:
            shutil.copyfile(path, directory / path.name)
    connection = connect(tmp_path / "old.db")
    migrate(connection, directory)
    yield connection, directory
    connection.close()


def seed_pre_migration(connection: sqlite3.Connection) -> None:
    connection.executemany(
        "INSERT INTO user (id, username, password_hash, role, created_at) VALUES (?, ?, 'x', ?, ?)",
        [(1, "a", "administrator", CREATED_STAMP), (2, "b", "standard", CREATED_STAMP)],
    )
    connection.executemany(
        "INSERT INTO environment (id, name, type, connection_kind, socket_path, created_at, "
        "updated_at) VALUES (?, ?, ?, 'unix_socket', ?, ?, ?)",
        [
            (1, "e1", "standalone", "/s1", CREATED_STAMP, CREATED_STAMP),
            (2, "e2", "swarm", "/s2", CREATED_STAMP, CREATED_STAMP),
        ],
    )
    connection.execute(
        "INSERT INTO automation_rule (id, name, subsystem, environment_id, target_selector, "
        "created_at, updated_at) VALUES (1, 'r1', 'container-updater', 1, '{}', ?, ?)",
        (CREATED_STAMP, CREATED_STAMP),
    )
    connection.execute(
        "INSERT INTO scheduled_job (id, subsystem, rule_id, schedule_kind, schedule_expression, "
        "created_at, updated_at) VALUES (1, 'container-updater', 1, 'interval', '3600', ?, ?)",
        (CREATED_STAMP, CREATED_STAMP),
    )
    connection.execute(
        "INSERT INTO job_run (id, scheduled_job_id, trigger, started_at) "
        "VALUES (7, 1, 'schedule', ?)",
        (CREATED_STAMP,),
    )
    connection.executemany(INSERT_EVENT, PRE_MIGRATION_EVENTS)


def test_a_populated_0013_database_keeps_every_row_id_and_detail_byte_and_every_index(
    before: tuple[sqlite3.Connection, Path],
) -> None:
    connection, directory = before
    seed_pre_migration(connection)
    events = "SELECT * FROM activity_event ORDER BY id"
    detail_bytes = "SELECT hex(CAST(detail AS BLOB)) FROM activity_event ORDER BY id"
    rows_before = rows_of(connection, events)
    bytes_before = rows_of(connection, detail_bytes)
    columns_before = rows_of(connection, "PRAGMA table_xinfo(activity_event)")
    keys_before = sorted(rows_of(connection, "PRAGMA foreign_key_list(activity_event)"))
    indexes_before = index_sql(connection)
    shutil.copyfile(MIGRATIONS_DIR / MIGRATION, directory / MIGRATION)

    applied = migrate(connection, directory)

    assert [migration.filename for migration in applied] == [MIGRATION]
    assert rows_of(connection, events) == rows_before
    assert rows_of(connection, detail_bytes) == bytes_before
    assert rows_of(connection, "PRAGMA table_xinfo(activity_event)") == columns_before
    assert sorted(rows_of(connection, "PRAGMA foreign_key_list(activity_event)")) == keys_before
    indexes_after = index_sql(connection)
    assert {name: indexes_after[name] for name in indexes_before} == indexes_before
    assert set(indexes_after) == ACTIVITY_INDEXES
    sequence = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'activity_event'"
    ).fetchone()
    assert sequence["seq"] == HIGHEST_ID
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_pruner_is_a_permitted_actor_and_an_id_is_never_reissued_after_a_full_prune(
    raw_connection: sqlite3.Connection,
) -> None:
    raw_connection.execute(INSERT_PRUNER_EVENT, (PRUNER, CREATED_STAMP))
    raw_connection.execute(INSERT_ANONYMOUS_EVENT, (CREATED_STAMP,))
    newest = raw_connection.execute("SELECT max(id) FROM activity_event").fetchone()[0]

    raw_connection.execute("DELETE FROM activity_event")
    raw_connection.execute(INSERT_ANONYMOUS_EVENT, (CREATED_STAMP,))

    assert raw_connection.execute("SELECT min(id) FROM activity_event").fetchone()[0] == newest + 1


@pytest.mark.parametrize("subsystem", REFUSED_ACTORS)
def test_a_subsystem_outside_the_permitted_names_is_refused(
    raw_connection: sqlite3.Connection, subsystem: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        raw_connection.execute(INSERT_PRUNER_EVENT, (subsystem, CREATED_STAMP))


def test_a_deleted_run_rule_or_environment_clears_its_reference_and_a_user_is_restricted(
    raw_connection: sqlite3.Connection,
) -> None:
    seed_pre_migration(raw_connection)

    raw_connection.execute("DELETE FROM job_run WHERE id = 7")
    raw_connection.execute("DELETE FROM automation_rule WHERE id = 1")
    raw_connection.execute("DELETE FROM environment WHERE id = 1")

    cleared = rows_of(
        raw_connection,
        "SELECT job_run_id, rule_id, environment_id FROM activity_event WHERE id = 5",
    )
    assert cleared == [(None, None, None)]
    with pytest.raises(sqlite3.IntegrityError, match=FOREIGN_KEY_FAILED):
        raw_connection.execute("DELETE FROM user WHERE id = 2")


def test_the_export_cursor_follows_its_rule_and_refuses_an_impossible_row(
    raw_connection: sqlite3.Connection,
) -> None:
    raw_connection.execute(
        "INSERT INTO automation_rule (id, name, subsystem, target_selector, created_at, "
        "updated_at) VALUES (2, 'export', ?, '{}', ?, ?)",
        (EXPORTER, CREATED_STAMP, CREATED_STAMP),
    )
    raw_connection.execute(INSERT_CURSOR, (2, 5, CREATED_STAMP))

    raw_connection.execute("DELETE FROM automation_rule WHERE id = 2")

    remaining = raw_connection.execute("SELECT count(*) FROM activity_export_cursor").fetchone()
    assert remaining[0] == 0


@pytest.mark.parametrize("statement", REFUSED_CURSORS)
def test_an_export_cursor_with_an_impossible_value_is_refused(
    raw_connection: sqlite3.Connection, statement: str
) -> None:
    raw_connection.execute(
        "INSERT INTO automation_rule (id, name, subsystem, target_selector, created_at, "
        "updated_at) VALUES (2, 'export', ?, '{}', ?, ?)",
        (EXPORTER, CREATED_STAMP, CREATED_STAMP),
    )
    raw_connection.execute(INSERT_CURSOR, (2, 5, CREATED_STAMP))

    with pytest.raises(sqlite3.IntegrityError):
        raw_connection.execute(statement)
