"""Migration 0033: the default mark, and the two anonymous entries a new database starts with."""

import sqlite3
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import migrate
from tests.db.backup_support import migrations_up_to
from tests.db.helpers import CREATED_STAMP
from tests.registry_support import DEFAULT_ENTRIES

PREVIOUS_VERSION = 32
ENTRIES = (
    "SELECT name, provider, url, authentication_enabled, seeded_default "
    "FROM registry_credential ORDER BY id"
)
DEFAULTS = [
    ("Docker Hub (anonymous)", "dockerhub", "docker.io", 0, 1),
    ("GitHub Container Registry (anonymous)", "ghcr", "ghcr.io", 0, 1),
]
INSERT_USER = (
    "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
    "VALUES ('admin', 'h', 'administrator', 1, ?)"
)
INSERT_REGISTRY = (
    "INSERT INTO registry_credential (name, provider, url, authentication_enabled, secret_kind, "
    "created_at, updated_at) VALUES ('mine', 'custom', 'r.example', 0, 'static', ?, ?)"
)


def previous(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "dockmin.db")
    migrate(connection, migrations_up_to(PREVIOUS_VERSION, tmp_path / "previous"))
    return connection


def test_a_new_database_holds_the_two_anonymous_entries_marked_as_defaults(
    tmp_path: Path,
) -> None:
    connection = connect(tmp_path / "dockmin.db")

    migrate(connection)

    assert [tuple(row) for row in connection.execute(ENTRIES)] == DEFAULTS
    assert connection.execute("SELECT count(*) FROM registry_environment_access").fetchone()[0] == 0


def test_the_entries_the_tests_seed_by_hand_match_the_ones_the_migration_inserts(
    tmp_path: Path,
) -> None:
    connection = connect(tmp_path / "dockmin.db")

    migrate(connection)

    stored = connection.execute(
        "SELECT name, provider, url, provider_extras FROM registry_credential ORDER BY id"
    )
    assert [tuple(row) for row in stored] == list(DEFAULT_ENTRIES)


def test_a_database_with_a_user_gets_the_column_and_no_entry(tmp_path: Path) -> None:
    connection = previous(tmp_path)
    connection.execute(INSERT_USER, (CREATED_STAMP,))

    migrate(connection)

    assert connection.execute(ENTRIES).fetchall() == []
    assert (
        connection.execute(
            "SELECT count(*) FROM pragma_table_info('registry_credential') "
            "WHERE name = 'seeded_default'"
        ).fetchone()[0]
        == 1
    )


def test_a_database_with_a_registry_set_and_no_user_keeps_only_its_own_set(
    tmp_path: Path,
) -> None:
    connection = previous(tmp_path)
    connection.execute(INSERT_REGISTRY, (CREATED_STAMP, CREATED_STAMP))

    migrate(connection)

    assert [(row["name"], row["seeded_default"]) for row in connection.execute(ENTRIES)] == [
        ("mine", 0)
    ]


def test_a_removed_default_is_absent_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "dockmin.db"
    connection = connect(path)
    migrate(connection)
    connection.execute("DELETE FROM registry_credential WHERE url = 'docker.io'")
    connection.close()

    restarted = connect(path)
    applied = migrate(restarted)

    assert applied == []
    assert [row["url"] for row in restarted.execute(ENTRIES)] == ["ghcr.io"]


def test_an_older_database_with_no_user_and_no_registry_set_gets_the_defaults(
    tmp_path: Path,
) -> None:
    connection = previous(tmp_path)

    migrate(connection)

    assert [tuple(row) for row in connection.execute(ENTRIES)] == DEFAULTS


@pytest.mark.parametrize("value", [2, -1])
def test_the_mark_accepts_only_zero_or_one(tmp_path: Path, value: int) -> None:
    connection = connect(tmp_path / "dockmin.db")
    migrate(connection)

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        connection.execute("UPDATE registry_credential SET seeded_default = ?", (value,))
