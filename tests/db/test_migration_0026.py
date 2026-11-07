"""Migration 0026: the access platform identity on a user and the sign-in method of a session."""

import sqlite3
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import migrate
from tests.db.backup_support import migrations_up_to
from tests.db.helpers import CREATED_STAMP

PREVIOUS_VERSION = 25
INSERT_USER = (
    "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
    "VALUES (?, 'h', 'standard', 1, ?)"
)
INSERT_SESSION = (
    "INSERT INTO session (user_id, token_hash, issued_at, expires_at) VALUES (1, 't', ?, ?)"
)
MAX_IDENTITY = 320
IDENTITY = "Pat@Example.test"


@pytest.fixture
def upgraded(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "dockmin.db")
    migrate(connection, migrations_up_to(PREVIOUS_VERSION, tmp_path / "previous"))
    connection.execute(INSERT_USER, ("existing", CREATED_STAMP))
    connection.execute(INSERT_SESSION, (CREATED_STAMP, CREATED_STAMP))
    connection.commit()
    migrate(connection)
    return connection


def test_existing_rows_are_unlinked_and_their_sessions_are_password_sessions(
    upgraded: sqlite3.Connection,
) -> None:
    identity = upgraded.execute("SELECT external_identity FROM user").fetchone()[0]
    method = upgraded.execute("SELECT sign_in_method FROM session").fetchone()[0]

    assert (identity, method) == (None, "password")


def test_an_identity_is_unique_ignoring_ascii_case_and_many_users_may_have_none(
    upgraded: sqlite3.Connection,
) -> None:
    upgraded.execute(INSERT_USER, ("second", CREATED_STAMP))
    upgraded.execute(
        "UPDATE user SET external_identity = ? WHERE username = 'existing'", (IDENTITY,)
    )

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        upgraded.execute(
            "UPDATE user SET external_identity = ? WHERE username = 'second'", (IDENTITY.lower(),)
        )


@pytest.mark.parametrize("identity", ["", "x" * (MAX_IDENTITY + 1)])
def test_an_identity_must_be_between_one_and_the_maximum_length(
    upgraded: sqlite3.Connection, identity: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        upgraded.execute("UPDATE user SET external_identity = ?", (identity,))


def test_a_session_method_is_password_or_jwt(upgraded: sqlite3.Connection) -> None:
    upgraded.execute("UPDATE session SET sign_in_method = 'jwt'")

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        upgraded.execute("UPDATE session SET sign_in_method = 'oidc'")
