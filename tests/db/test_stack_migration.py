"""Migration 0010: the empty-table guard, the rebuilt stack table and the variable table."""

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import MIGRATIONS_DIR, migrate
from tests.db.helpers import CREATED_STAMP, LATER_STAMP

MIGRATION = "0010_stacks.sql"
EARLIER = (
    "0001_initial.sql",
    "0002_json_column_checks.sql",
    "0003_authentication_state.sql",
    "0004_stack_key_case.sql",
    "0005_activity_event_indexes.sql",
    "0006_environments.sql",
    "0007_container_counts.sql",
    "0008_registry_credentials.sql",
    "0009_image_bindings.sql",
)
CHECK_FAILED = "CHECK constraint failed"
DIRECTORY = "a" * 32
OTHER_DIRECTORY = "b" * 32
DIGEST = "c" * 64
MAX_FILE_BYTES = 131072
MAX_VALUE_BYTES = 8192

INSERT_ENVIRONMENT = (
    "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
    "VALUES (?, 'unix_socket', ?, ?, ?)"
)
INSERT_STACK = (
    "INSERT INTO stack (environment_id, name, kind, origin, project_directory, compose_file, "
    "compose_file_digest, deployed_file_digest, resolved_digest, status, last_operation, "
    "last_outcome, created_by_user_id, created_at, updated_at) "
    "VALUES (:environment_id, :name, :kind, :origin, :project_directory, :compose_file, "
    ":compose_file_digest, :deployed_file_digest, :resolved_digest, :status, :last_operation, "
    ":last_outcome, :created_by_user_id, :created_at, :updated_at)"
)
INSERT_VARIABLE = (
    "INSERT INTO stack_variable (stack_id, name, is_secret, value, value_ciphertext, "
    "created_at, updated_at) "
    "VALUES (:stack_id, :name, :is_secret, :value, :value_ciphertext, :created_at, :updated_at)"
)

STACK = {
    "environment_id": 1,
    "name": "shop",
    "kind": "compose",
    "origin": "created",
    "project_directory": DIRECTORY,
    "compose_file": "services: {}\n",
    "compose_file_digest": DIGEST,
    "deployed_file_digest": None,
    "resolved_digest": None,
    "status": "unknown",
    "last_operation": None,
    "last_outcome": None,
    "created_by_user_id": None,
    "created_at": CREATED_STAMP,
    "updated_at": CREATED_STAMP,
}
PLAIN = {
    "stack_id": 1,
    "name": "TOKEN",
    "is_secret": 0,
    "value": "v",
    "value_ciphertext": None,
    "created_at": CREATED_STAMP,
    "updated_at": CREATED_STAMP,
}
SECRET = {**PLAIN, "is_secret": 1, "value": None, "value_ciphertext": "v1:nonce:body"}

ACCEPTED_STACKS = [
    pytest.param({}, id="minimal"),
    pytest.param({"kind": "swarm", "name": "My_Stack-Name"}, id="swarm-name-differs"),
    pytest.param({"origin": "adopted"}, id="adopted"),
    pytest.param({"name": "a" * 63}, id="longest-compose-name"),
    pytest.param({"name": "0a-b_c"}, id="compose-name-with-digit-first"),
    pytest.param({"compose_file": "x" * MAX_FILE_BYTES}, id="file-at-the-cap"),
    pytest.param({"compose_file": "é" * (MAX_FILE_BYTES // 2)}, id="multibyte-file-at-the-cap"),
    pytest.param({"deployed_file_digest": DIGEST, "resolved_digest": "0" * 64}, id="digests"),
    pytest.param({"status": "partial"}, id="partial-status"),
    pytest.param({"last_operation": "remove", "last_outcome": "failure"}, id="remove-operation"),
    pytest.param({"last_operation": "adopt", "last_outcome": "interrupted"}, id="interrupted"),
    pytest.param({"last_operation": "stop", "last_outcome": "outcome_unknown"}, id="unknown"),
]
REFUSED_STACKS = [
    pytest.param({"kind": "kubernetes"}, id="unknown-kind"),
    pytest.param({"origin": "imported"}, id="unknown-origin"),
    pytest.param({"project_directory": "a" * 31}, id="short-directory"),
    pytest.param({"project_directory": "a" * 33}, id="long-directory"),
    pytest.param({"project_directory": "A" * 32}, id="upper-case-directory"),
    pytest.param({"project_directory": "g" * 32}, id="non-hex-directory"),
    pytest.param({"project_directory": "." * 32}, id="path-like-directory"),
    pytest.param({"compose_file": ""}, id="empty-file"),
    pytest.param({"compose_file": "x" * (MAX_FILE_BYTES + 1)}, id="file-over-the-cap"),
    pytest.param(
        {"compose_file": "é" * (MAX_FILE_BYTES // 2 + 1)}, id="multibyte-file-over-the-cap"
    ),
    pytest.param({"compose_file_digest": "c" * 63}, id="short-file-digest"),
    pytest.param({"compose_file_digest": "C" * 64}, id="upper-case-file-digest"),
    pytest.param({"deployed_file_digest": "c" * 63}, id="short-deployed-digest"),
    pytest.param({"resolved_digest": "G" * 64}, id="non-hex-resolved-digest"),
    pytest.param({"status": "paused"}, id="unknown-status"),
    pytest.param({"last_operation": "restart", "last_outcome": "success"}, id="unknown-operation"),
    pytest.param({"last_operation": "deploy", "last_outcome": "partial"}, id="unknown-outcome"),
    pytest.param(
        {"last_operation": "deploy", "last_outcome": None}, id="operation-without-outcome"
    ),
    pytest.param(
        {"last_operation": None, "last_outcome": "success"}, id="outcome-without-operation"
    ),
    pytest.param({"name": ""}, id="empty-compose-name"),
    pytest.param({"name": "a" * 64}, id="long-compose-name"),
    pytest.param({"name": "Shop"}, id="upper-case-compose-name"),
    pytest.param({"name": "-shop"}, id="compose-name-leading-dash"),
    pytest.param({"name": "_shop"}, id="compose-name-leading-underscore"),
    pytest.param({"name": "a b"}, id="compose-name-with-space"),
    pytest.param({"name": "a.b"}, id="compose-name-with-dot"),
    pytest.param({"name": "a;b"}, id="compose-name-with-metacharacter"),
    pytest.param({"kind": "swarm", "name": "My.Stack"}, id="swarm-name-with-dot"),
]
REFUSED_VARIABLES = [
    pytest.param({"is_secret": 2}, id="flag-out-of-range"),
    pytest.param({"name": ""}, id="empty-name"),
    pytest.param({"name": "N" * 129}, id="long-name"),
    pytest.param({"name": "1ABC"}, id="digit-first-name"),
    pytest.param({"name": "A-B"}, id="dash-in-name"),
    pytest.param({"name": "A B"}, id="space-in-name"),
    pytest.param({"name": "A.B"}, id="dot-in-name"),
    pytest.param({"value": "x" * (MAX_VALUE_BYTES + 1)}, id="value-over-the-cap"),
    pytest.param({"value": "é" * (MAX_VALUE_BYTES // 2 + 1)}, id="multibyte-over-the-cap"),
    pytest.param({"value": None}, id="plain-without-a-value"),
    pytest.param({"value_ciphertext": "v1:nonce:body"}, id="plain-with-ciphertext"),
    pytest.param({**SECRET, "value": "clear"}, id="secret-with-a-clear-value"),
    pytest.param({**SECRET, "value_ciphertext": None}, id="secret-without-ciphertext"),
]


@pytest.fixture
def before_stacks(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    directory = tmp_path / "migrations"
    directory.mkdir()
    for name in EARLIER:
        shutil.copyfile(MIGRATIONS_DIR / name, directory / name)
    connection = connect(tmp_path / "old.db")
    migrate(connection, directory)
    yield connection, directory
    connection.close()


@pytest.fixture
def seeded(raw_connection: sqlite3.Connection) -> sqlite3.Connection:
    """The migrated database holding two environments."""
    for name in ("one", "two"):
        raw_connection.execute(
            INSERT_ENVIRONMENT, (name, f"/run/{name}.sock", CREATED_STAMP, CREATED_STAMP)
        )
    return raw_connection


def count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def test_the_migration_aborts_and_keeps_the_row_when_a_stack_exists(
    before_stacks: tuple[sqlite3.Connection, Path],
) -> None:
    connection, directory = before_stacks
    connection.execute(INSERT_ENVIRONMENT, ("box", "/run/box.sock", CREATED_STAMP, CREATED_STAMP))
    connection.execute(
        "INSERT INTO stack (environment_id, name, kind, project_directory, created_at, "
        "updated_at) VALUES (1, 'old', 'compose', 'old-dir', ?, ?)",
        (CREATED_STAMP, CREATED_STAMP),
    )
    shutil.copyfile(MIGRATIONS_DIR / MIGRATION, directory / MIGRATION)

    with pytest.raises(sqlite3.IntegrityError, match="stack_table_is_empty"):
        migrate(connection, directory)

    assert connection.execute("SELECT name FROM stack").fetchall()[0][0] == "old"
    assert connection.execute("SELECT max(version) FROM schema_migration").fetchone()[0] == len(
        EARLIER
    )
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('migration_guard', 'stack_new', "
            "'stack_variable')"
        ).fetchall()
        == []
    )


def test_the_migration_applies_to_an_empty_table_and_leaves_the_schema_consistent(
    before_stacks: tuple[sqlite3.Connection, Path],
) -> None:
    connection, directory = before_stacks
    shutil.copyfile(MIGRATIONS_DIR / MIGRATION, directory / MIGRATION)

    applied = migrate(connection, directory)

    stack_references = {
        row["table"]: row["on_delete"]
        for row in connection.execute("PRAGMA foreign_key_list(stack)")
    }
    variable_references = {
        row["table"]: row["on_delete"]
        for row in connection.execute("PRAGMA foreign_key_list(stack_variable)")
    }
    leftovers = connection.execute(
        "SELECT name FROM sqlite_master WHERE name IN ('migration_guard', 'stack_new')"
    ).fetchall()
    indexes = {row["name"] for row in connection.execute("PRAGMA index_list(stack)")}
    assert [migration.filename for migration in applied] == [MIGRATION]
    assert stack_references == {"environment": "CASCADE", "user": "SET NULL"}
    assert variable_references == {"stack": "CASCADE"}
    assert leftovers == []
    assert "idx_stack_created_by_user_id" in indexes
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("overrides", ACCEPTED_STACKS)
def test_every_accepted_stack_shape_is_stored(
    seeded: sqlite3.Connection, overrides: dict[str, object]
) -> None:
    seeded.execute(INSERT_STACK, {**STACK, **overrides})

    assert count(seeded, "stack") == 1


@pytest.mark.parametrize("overrides", REFUSED_STACKS)
def test_every_check_on_the_stack_table_refuses_its_case(
    seeded: sqlite3.Connection, overrides: dict[str, object]
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        seeded.execute(INSERT_STACK, {**STACK, **overrides})


def test_a_name_is_unique_per_environment_and_a_directory_is_unique_everywhere(
    seeded: sqlite3.Connection,
) -> None:
    seeded.execute(INSERT_STACK, STACK)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        seeded.execute(INSERT_STACK, {**STACK, "project_directory": OTHER_DIRECTORY})
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        seeded.execute(INSERT_STACK, {**STACK, "environment_id": 2, "name": "other"})
    seeded.execute(
        INSERT_STACK, {**STACK, "environment_id": 2, "project_directory": OTHER_DIRECTORY}
    )
    assert count(seeded, "stack") == 2


def test_a_deleted_stack_id_is_never_issued_again(seeded: sqlite3.Connection) -> None:
    seeded.execute(INSERT_STACK, STACK)
    seeded.execute(INSERT_VARIABLE, PLAIN)
    seeded.execute("DELETE FROM stack WHERE id = 1")

    seeded.execute(INSERT_STACK, STACK)
    seeded.execute(INSERT_VARIABLE, {**PLAIN, "stack_id": 2})

    assert seeded.execute("SELECT id FROM stack").fetchone()[0] == 2
    assert seeded.execute("SELECT id FROM stack_variable").fetchone()[0] == 2


def test_a_status_omitted_on_insert_is_unknown(seeded: sqlite3.Connection) -> None:
    seeded.execute(
        "INSERT INTO stack (environment_id, name, kind, origin, project_directory, compose_file, "
        "compose_file_digest, created_at, updated_at) "
        "VALUES (1, 'shop', 'compose', 'created', ?, 'x', ?, ?, ?)",
        (DIRECTORY, DIGEST, CREATED_STAMP, CREATED_STAMP),
    )

    row = seeded.execute("SELECT status, last_operation, last_outcome FROM stack").fetchone()

    assert tuple(row) == ("unknown", None, None)


@pytest.mark.parametrize("variable", [PLAIN, SECRET], ids=["plain", "secret"])
def test_a_plain_and_a_secret_variable_are_stored_in_their_own_column(
    seeded: sqlite3.Connection, variable: dict[str, object]
) -> None:
    seeded.execute(INSERT_STACK, STACK)

    seeded.execute(INSERT_VARIABLE, variable)

    assert count(seeded, "stack_variable") == 1


def test_a_value_of_exactly_the_cap_is_stored(seeded: sqlite3.Connection) -> None:
    seeded.execute(INSERT_STACK, STACK)

    seeded.execute(INSERT_VARIABLE, {**PLAIN, "value": "x" * MAX_VALUE_BYTES})

    assert count(seeded, "stack_variable") == 1


@pytest.mark.parametrize("overrides", REFUSED_VARIABLES)
def test_every_check_on_the_variable_table_refuses_its_case(
    seeded: sqlite3.Connection, overrides: dict[str, object]
) -> None:
    seeded.execute(INSERT_STACK, STACK)

    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        seeded.execute(INSERT_VARIABLE, {**PLAIN, **overrides})


def test_an_update_cannot_leave_a_secret_in_the_clear(seeded: sqlite3.Connection) -> None:
    seeded.execute(INSERT_STACK, STACK)
    seeded.execute(INSERT_VARIABLE, SECRET)

    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        seeded.execute("UPDATE stack_variable SET value = 'clear'")
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        seeded.execute("UPDATE stack_variable SET is_secret = 0")


def test_a_variable_name_is_unique_per_stack(seeded: sqlite3.Connection) -> None:
    seeded.execute(INSERT_STACK, STACK)
    seeded.execute(INSERT_STACK, {**STACK, "name": "other", "project_directory": OTHER_DIRECTORY})
    seeded.execute(INSERT_VARIABLE, PLAIN)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        seeded.execute(INSERT_VARIABLE, {**SECRET, "name": "TOKEN"})
    seeded.execute(INSERT_VARIABLE, {**PLAIN, "stack_id": 2})
    assert count(seeded, "stack_variable") == 2


def test_deleting_a_stack_removes_its_variables_and_deleting_an_environment_its_stacks(
    seeded: sqlite3.Connection,
) -> None:
    seeded.execute(INSERT_STACK, STACK)
    seeded.execute(INSERT_STACK, {**STACK, "name": "other", "project_directory": OTHER_DIRECTORY})
    seeded.execute(INSERT_VARIABLE, PLAIN)
    seeded.execute(INSERT_VARIABLE, {**SECRET, "stack_id": 2})

    seeded.execute("DELETE FROM stack WHERE id = 1")
    after_stack = count(seeded, "stack_variable")
    seeded.execute("DELETE FROM environment WHERE id = 1")

    assert after_stack == 1
    assert count(seeded, "stack") == 0
    assert count(seeded, "stack_variable") == 0


def test_deleting_the_creating_user_keeps_the_stack(seeded: sqlite3.Connection) -> None:
    seeded.execute(
        "INSERT INTO user (username, password_hash, role, created_at) "
        "VALUES ('maker', 'hash', 'standard', ?)",
        (LATER_STAMP,),
    )
    seeded.execute(INSERT_STACK, {**STACK, "created_by_user_id": 1})

    seeded.execute("DELETE FROM user WHERE id = 1")

    assert seeded.execute("SELECT created_by_user_id FROM stack").fetchone()[0] is None


def test_the_creating_user_column_is_indexed(
    seeded: sqlite3.Connection,
) -> None:
    plan = " ".join(
        row["detail"]
        for row in seeded.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM stack WHERE created_by_user_id = ?", (1,)
        )
    )

    assert "idx_stack_created_by_user_id" in plan
