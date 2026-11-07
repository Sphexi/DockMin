"""Migration 0030: the stack_dismissal table, its cascade and its constraints."""

import json
import sqlite3
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import migrate
from tests.db.backup_support import migrations_up_to
from tests.db.helpers import CREATED_STAMP

PREVIOUS_VERSION = 29
MAX_OBJECTS = 500
CHECK_FAILED = "CHECK constraint failed"
INSERT_ENVIRONMENT = (
    "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
    "VALUES (?, 'unix_socket', ?, ?, ?)"
)
INSERT_USER = (
    "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
    "VALUES ('admin', 'h', 'administrator', 1, ?)"
)
INSERT_DISMISSAL = (
    "INSERT INTO stack_dismissal (environment_id, kind, project_key, objects, "
    "dismissed_by_user_id, dismissed_at) VALUES (?, ?, ?, ?, ?, ?)"
)
TABLE_EXISTS = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stack_dismissal'"
FIRST, SECOND = 1, 2
ONE_OBJECT = json.dumps(["network:a"])
BAD_OBJECTS = [
    pytest.param("not json", id="malformed-json"),
    pytest.param("", id="empty-text"),
    pytest.param("{}", id="object"),
    pytest.param('"network:a"', id="string"),
    pytest.param("[]", id="empty-array"),
    pytest.param(json.dumps(["x"] * (MAX_OBJECTS + 1)), id="over-the-limit"),
]
BAD_KEYS = [
    pytest.param("", id="empty"),
    pytest.param("k" * 64, id="too-long"),
]


@pytest.fixture
def previous(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "dockmin.db")
    migrate(connection, migrations_up_to(PREVIOUS_VERSION, tmp_path / "previous"))
    return connection


@pytest.fixture
def upgraded(previous: sqlite3.Connection) -> sqlite3.Connection:
    for name in ("one", "two"):
        previous.execute(
            INSERT_ENVIRONMENT, (name, f"/run/{name}.sock", CREATED_STAMP, CREATED_STAMP)
        )
    previous.execute(INSERT_USER, (CREATED_STAMP,))
    migrate(previous)
    return previous


def dismiss(
    connection: sqlite3.Connection,
    environment_id: int = FIRST,
    key: str = "shop",
    objects: str = ONE_OBJECT,
    kind: str = "compose",
    user_id: int | None = None,
) -> None:
    connection.execute(
        INSERT_DISMISSAL, (environment_id, kind, key, objects, user_id, CREATED_STAMP)
    )


def test_the_migration_adds_the_table_and_keeps_the_rows_already_there(
    previous: sqlite3.Connection,
) -> None:
    previous.execute(INSERT_ENVIRONMENT, ("one", "/run/one.sock", CREATED_STAMP, CREATED_STAMP))
    before = previous.execute(TABLE_EXISTS).fetchone()

    migrate(previous)

    assert before is None
    assert previous.execute(TABLE_EXISTS).fetchone() is not None
    assert previous.execute("SELECT name FROM environment").fetchall()[0][0] == "one"


def test_deleting_an_environment_removes_its_dismissals_and_deleting_a_user_keeps_them(
    upgraded: sqlite3.Connection,
) -> None:
    dismiss(upgraded, FIRST, "shop", user_id=FIRST)
    dismiss(upgraded, SECOND, "shop")

    upgraded.execute("DELETE FROM user")
    kept = upgraded.execute("SELECT dismissed_by_user_id FROM stack_dismissal").fetchall()
    upgraded.execute("DELETE FROM environment WHERE id = ?", (FIRST,))
    left = upgraded.execute("SELECT environment_id FROM stack_dismissal").fetchall()

    assert [row[0] for row in kept] == [None, None]
    assert [row[0] for row in left] == [SECOND]


@pytest.mark.parametrize("objects", BAD_OBJECTS)
def test_objects_must_be_a_json_array_of_one_to_five_hundred_entries(
    upgraded: sqlite3.Connection, objects: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        dismiss(upgraded, objects=objects)


@pytest.mark.parametrize("size", [1, MAX_OBJECTS])
def test_objects_accept_the_smallest_and_the_largest_array(
    upgraded: sqlite3.Connection, size: int
) -> None:
    dismiss(upgraded, objects=json.dumps(["x"] * size))

    stored = upgraded.execute("SELECT json_array_length(objects) FROM stack_dismissal").fetchone()
    assert stored[0] == size


@pytest.mark.parametrize("key", BAD_KEYS)
def test_a_project_key_must_be_between_one_and_sixty_three_characters(
    upgraded: sqlite3.Connection, key: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        dismiss(upgraded, key=key)


def test_a_kind_is_compose_or_swarm_and_a_stack_has_one_dismissal_per_kind(
    upgraded: sqlite3.Connection,
) -> None:
    dismiss(upgraded, kind="compose")
    dismiss(upgraded, kind="swarm")

    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        dismiss(upgraded, key="other", kind="kubernetes")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        dismiss(upgraded, kind="compose")
