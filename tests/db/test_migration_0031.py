"""Migration 0031: the environment a session's sidebar last scoped to."""

import sqlite3
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import migrate
from tests.db.backup_support import migrations_up_to
from tests.db.helpers import CREATED_STAMP

PREVIOUS_VERSION = 30
UNKNOWN_ENVIRONMENT_ID = 999
INSERT_USER = (
    "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
    "VALUES ('existing', 'h', 'standard', 1, ?)"
)
INSERT_SESSION = (
    "INSERT INTO session (user_id, token_hash, issued_at, expires_at) VALUES (1, 't', ?, ?)"
)
INSERT_ENVIRONMENT = (
    "INSERT INTO environment (name, type, connection_kind, socket_path, created_at, updated_at) "
    "VALUES ('box', 'standalone', 'unix_socket', '/run/box.sock', ?, ?)"
)
REMEMBER = "UPDATE session SET nav_environment_id = ? WHERE id = 1"
REMEMBERED = "SELECT nav_environment_id FROM session WHERE id = 1"


@pytest.fixture
def upgraded(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "dockmin.db")
    migrate(connection, migrations_up_to(PREVIOUS_VERSION, tmp_path / "previous"))
    connection.execute(INSERT_USER, (CREATED_STAMP,))
    connection.execute(INSERT_SESSION, (CREATED_STAMP, CREATED_STAMP))
    connection.execute(INSERT_ENVIRONMENT, (CREATED_STAMP, CREATED_STAMP))
    migrate(connection)
    return connection


def test_a_session_that_existed_before_the_migration_remembers_no_environment(
    upgraded: sqlite3.Connection,
) -> None:
    assert upgraded.execute(REMEMBERED).fetchone()[0] is None


def test_deleting_the_remembered_environment_clears_the_session_reference(
    upgraded: sqlite3.Connection,
) -> None:
    environment_id = upgraded.execute("SELECT id FROM environment").fetchone()[0]
    upgraded.execute(REMEMBER, (environment_id,))

    upgraded.execute("DELETE FROM environment WHERE id = ?", (environment_id,))

    assert upgraded.execute("SELECT count(*) FROM session").fetchone()[0] == 1
    assert upgraded.execute(REMEMBERED).fetchone()[0] is None


def test_a_session_cannot_remember_an_environment_that_does_not_exist(
    upgraded: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        upgraded.execute(REMEMBER, (UNKNOWN_ENVIRONMENT_ID,))
