"""Migration 0034: anonymous binding rows are removed; set and credential_missing rows stay."""

import sqlite3
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import migrate
from tests.db.backup_support import migrations_up_to
from tests.db.helpers import CREATED_STAMP

PREVIOUS_VERSION = 33
SELECT_REFERENCES = "SELECT image_reference FROM image_credential_binding ORDER BY id"
INSERT_ENVIRONMENT = (
    "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
    "VALUES ('host', 'unix_socket', '/var/run/docker.sock', ?, ?)"
)
INSERT_CREDENTIAL = (
    "INSERT INTO registry_credential (name, provider, url, authentication_enabled, secret_kind, "
    "created_at, updated_at) VALUES ('mine', 'custom', 'r.example', 0, 'static', ?, ?)"
)
INSERT_BOUND = (
    "INSERT INTO image_credential_binding (environment_id, image_reference, "
    "registry_credential_id, state, bound_at, updated_at) VALUES (1, ?, ?, 'bound', ?, ?)"
)
INSERT_MISSING = (
    "INSERT INTO image_credential_binding (environment_id, image_reference, "
    "missing_credential_id, missing_credential_name, missing_reason, state, bound_at, "
    "updated_at, credential_missing_at) "
    "VALUES (1, 'r.example/missing:1', 1, 'mine', 'deleted', 'credential_missing', ?, ?, ?)"
)


@pytest.fixture
def previous(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "dockmin.db")
    migrate(connection, migrations_up_to(PREVIOUS_VERSION, tmp_path / "previous"))
    connection.execute(INSERT_ENVIRONMENT, (CREATED_STAMP, CREATED_STAMP))
    connection.execute(INSERT_CREDENTIAL, (CREATED_STAMP, CREATED_STAMP))
    connection.execute(INSERT_BOUND, ("r.example/anonymous:1", None, CREATED_STAMP, CREATED_STAMP))
    connection.execute(INSERT_BOUND, ("r.example/set:1", 1, CREATED_STAMP, CREATED_STAMP))
    connection.execute(INSERT_MISSING, (CREATED_STAMP, CREATED_STAMP, CREATED_STAMP))
    return connection


def test_the_migration_deletes_anonymous_rows_and_keeps_set_and_missing_rows(
    previous: sqlite3.Connection,
) -> None:
    migrate(previous)

    kept = [row["image_reference"] for row in previous.execute(SELECT_REFERENCES)]
    assert kept == ["r.example/set:1", "r.example/missing:1"]
