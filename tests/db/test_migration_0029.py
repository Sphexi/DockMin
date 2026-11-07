"""Migration 0029: the reveal window on a session and the trigger that closes it."""

import sqlite3
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import migrate
from tests.db.backup_support import migrations_up_to
from tests.db.helpers import CREATED_STAMP

PREVIOUS_VERSION = 28
TRIGGER = "user_closes_reveal_windows"
WINDOW_END = "2099-01-01T00:05:00Z"
USED = 7
MAX_REVEALS = 20
INSERT_USER = (
    "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
    "VALUES (?, 'hash', 'administrator', 1, ?)"
)
INSERT_SESSION = (
    "INSERT INTO session (user_id, token_hash, issued_at, expires_at) VALUES (?, ?, ?, ?)"
)
OPEN_WINDOW = "UPDATE session SET reveal_until = ?, reveal_count = ?"
WINDOWS = "SELECT user_id, reveal_until, reveal_count FROM session ORDER BY user_id, id"

CLOSED = (None, 0)
OPEN = (WINDOW_END, USED)


@pytest.fixture
def upgraded(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "dockmin.db")
    migrate(connection, migrations_up_to(PREVIOUS_VERSION, tmp_path / "previous"))
    for number in range(1, 6):
        connection.execute(INSERT_USER, (f"user-{number}", CREATED_STAMP))
    connection.execute(INSERT_SESSION, (1, "first", CREATED_STAMP, CREATED_STAMP))
    connection.commit()
    migrate(connection)
    return connection


def _windows(connection: sqlite3.Connection) -> list[tuple[int, str | None, int]]:
    return [tuple(row) for row in connection.execute(WINDOWS)]


def _open_every_window(connection: sqlite3.Connection) -> None:
    for number in range(2, 6):
        connection.execute(INSERT_SESSION, (number, f"token-{number}", CREATED_STAMP, WINDOW_END))
    connection.execute(INSERT_SESSION, (1, "second", CREATED_STAMP, WINDOW_END))
    connection.execute(OPEN_WINDOW, (WINDOW_END, USED))


def test_an_existing_session_has_no_window_and_no_reveals_used(
    upgraded: sqlite3.Connection,
) -> None:
    columns = {row[1]: row for row in upgraded.execute("PRAGMA table_info(session)")}
    trigger = upgraded.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = ?", (TRIGGER,)
    ).fetchone()

    assert _windows(upgraded) == [(1, None, 0)]
    assert {"reveal_until", "reveal_count"} <= set(columns)
    assert columns["reveal_count"][3] == 1
    assert trigger is not None


def test_a_window_end_must_be_a_timestamp_and_the_count_must_stay_within_the_cap(
    upgraded: sqlite3.Connection,
) -> None:
    upgraded.execute(OPEN_WINDOW, (WINDOW_END, MAX_REVEALS))

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        upgraded.execute("UPDATE session SET reveal_until = 'tomorrow'")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        upgraded.execute("UPDATE session SET reveal_count = ?", (MAX_REVEALS + 1,))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        upgraded.execute("UPDATE session SET reveal_count = -1")
    assert _windows(upgraded) == [(1, WINDOW_END, MAX_REVEALS)]


def test_changing_a_password_hash_a_role_or_the_enabled_flag_closes_every_window_of_that_user(
    upgraded: sqlite3.Connection,
) -> None:
    _open_every_window(upgraded)

    upgraded.execute("UPDATE user SET password_hash = 'replaced' WHERE id = 1")
    upgraded.execute("UPDATE user SET role = 'standard' WHERE id = 2")
    upgraded.execute("UPDATE user SET is_enabled = 0 WHERE id = 3")

    assert _windows(upgraded) == [
        (1, *CLOSED),
        (1, *CLOSED),
        (2, *CLOSED),
        (3, *CLOSED),
        (4, *OPEN),
        (5, *OPEN),
    ]


def test_a_username_change_or_a_write_that_changes_nothing_leaves_every_window_open(
    upgraded: sqlite3.Connection,
) -> None:
    _open_every_window(upgraded)

    upgraded.execute("UPDATE user SET username = 'renamed' WHERE id = 4")
    upgraded.execute("UPDATE user SET role = role, password_hash = password_hash WHERE id = 5")
    upgraded.execute("UPDATE user SET last_login_at = ? WHERE id = 1", (CREATED_STAMP,))

    assert {(until, count) for _, until, count in _windows(upgraded)} == {OPEN}
