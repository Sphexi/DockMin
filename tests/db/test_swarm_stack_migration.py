"""Migration 0011: the folded name index and guard, the swarm name triggers and the value token."""

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import MIGRATIONS_DIR, migrate
from tests.db.helpers import CREATED_STAMP

MIGRATION = "0011_swarm_stacks.sql"
CHECK_FAILED = "CHECK constraint failed"
SWARM_NAME_FAILED = "CHECK constraint failed: stack swarm name"
UNIQUE_FAILED = "UNIQUE constraint failed"
GUARD_COLUMN = "no_stack_names_differ_only_by_case"
DIGEST = "c" * 64
CIPHERTEXT = "v1:nonce:body"
TOKEN = "a1b2c3d4e5f6"

INSERT_ENVIRONMENT = (
    "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
    "VALUES (?, 'unix_socket', ?, ?, ?)"
)
INSERT_STACK = (
    "INSERT INTO stack (environment_id, name, kind, origin, project_directory, compose_file, "
    "compose_file_digest, created_at, updated_at) "
    "VALUES (:environment_id, :name, :kind, 'created', :directory, 'services: {}', :digest, "
    ":stamp, :stamp)"
)
INSERT_VARIABLE = (
    "INSERT INTO stack_variable (stack_id, name, is_secret, value, value_ciphertext, "
    "value_token, created_at, updated_at) "
    "VALUES (:stack_id, :name, :is_secret, :value, :ciphertext, :token, :stamp, :stamp)"
)

ACCEPTED_SWARM_NAMES = [
    pytest.param("Shop", id="upper-case"),
    pytest.param("My_Stack-1", id="underscore-and-dash"),
    pytest.param("9lives", id="digit-first"),
    pytest.param("a" * 63, id="longest"),
    pytest.param("A", id="single-character"),
]
REFUSED_SWARM_NAMES = [
    pytest.param("", id="empty"),
    pytest.param("a" * 64, id="too-long"),
    pytest.param("-shop", id="leading-dash"),
    pytest.param("_shop", id="leading-underscore"),
    pytest.param("my.stack", id="dot"),
    pytest.param("my stack", id="space"),
    pytest.param("shop;", id="metacharacter"),
    pytest.param("café", id="non-ascii"),
]
VALID_TOKENS = [TOKEN, "0" * 12, None]
INVALID_TOKENS = ["a1b2c3d4e5f", TOKEN + "7", TOKEN.upper(), "a1b2c3d4e5f-", ""]


INSERT_UNTOKENED_VARIABLE = (
    "INSERT INTO stack_variable (stack_id, name, is_secret, value, value_ciphertext, "
    "created_at, updated_at) "
    "VALUES (:stack_id, :name, :is_secret, :value, :ciphertext, :stamp, :stamp)"
)


def directory_of(number: int) -> str:
    return f"{number:032x}"


def add_environment(connection: sqlite3.Connection, name: str) -> int:
    cursor = connection.execute(
        INSERT_ENVIRONMENT, (name, f"/run/{name}.sock", CREATED_STAMP, CREATED_STAMP)
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def add_stack(
    connection: sqlite3.Connection, environment_id: int, name: str, kind: str, number: int
) -> int:
    cursor = connection.execute(
        INSERT_STACK,
        {
            "environment_id": environment_id,
            "name": name,
            "kind": kind,
            "directory": directory_of(number),
            "digest": DIGEST,
            "stamp": CREATED_STAMP,
        },
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def add_variable(
    connection: sqlite3.Connection,
    stack_id: int,
    name: str,
    *,
    secret: bool = False,
    token: str | None = None,
) -> None:
    connection.execute(
        INSERT_VARIABLE,
        {
            "stack_id": stack_id,
            "name": name,
            "is_secret": int(secret),
            "value": None if secret else "v",
            "ciphertext": CIPHERTEXT if secret else None,
            "token": token,
            "stamp": CREATED_STAMP,
        },
    )


@pytest.fixture
def before(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    """A database migrated up to 0010, and the directory holding those migrations."""
    directory = tmp_path / "migrations"
    directory.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name < MIGRATION:
            shutil.copyfile(path, directory / path.name)
    connection = connect(tmp_path / "old.db")
    migrate(connection, directory)
    yield connection, directory
    connection.close()


@pytest.fixture
def seeded(raw_connection: sqlite3.Connection) -> sqlite3.Connection:
    """The fully migrated database holding two environments."""
    add_environment(raw_connection, "one")
    add_environment(raw_connection, "two")
    return raw_connection


def test_the_migration_keeps_every_stack_and_variable_of_a_populated_database(
    before: tuple[sqlite3.Connection, Path],
) -> None:
    connection, directory = before
    first = add_environment(connection, "one")
    second = add_environment(connection, "two")
    stacks = [
        add_stack(connection, first, "shop", "compose", 1),
        add_stack(connection, first, "Blog", "swarm", 2),
        add_stack(connection, second, "shop", "compose", 3),
    ]
    for stack_id in stacks:
        for name, secret in (("PLAIN", False), ("SECRET", True)):
            connection.execute(
                INSERT_UNTOKENED_VARIABLE,
                {
                    "stack_id": stack_id,
                    "name": name,
                    "is_secret": int(secret),
                    "value": None if secret else "v",
                    "ciphertext": CIPHERTEXT if secret else None,
                    "stamp": CREATED_STAMP,
                },
            )
    shape = "SELECT id, stack_id, name, is_secret, value, value_ciphertext FROM stack_variable"
    variables_before = [tuple(row) for row in connection.execute(shape + " ORDER BY id")]
    stacks_before = [tuple(row) for row in connection.execute("SELECT * FROM stack ORDER BY id")]
    shutil.copyfile(MIGRATIONS_DIR / MIGRATION, directory / MIGRATION)

    applied = migrate(connection, directory)

    assert [migration.filename for migration in applied] == [MIGRATION]
    assert [tuple(row) for row in connection.execute(shape + " ORDER BY id")] == variables_before
    assert [tuple(row) for row in connection.execute("SELECT * FROM stack ORDER BY id")] == (
        stacks_before
    )
    tokens = [row[0] for row in connection.execute("SELECT value_token FROM stack_variable")]
    assert tokens == [None] * len(variables_before)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    names = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('idx_stack_name_folded', "
            "'stack_swarm_name_insert', 'stack_swarm_name_update', 'migration_guard')"
        )
    }
    assert names == {"idx_stack_name_folded", "stack_swarm_name_insert", "stack_swarm_name_update"}


def test_the_guard_aborts_naming_its_column_and_leaves_the_rows_when_two_names_fold_equal(
    before: tuple[sqlite3.Connection, Path],
) -> None:
    connection, directory = before
    environment = add_environment(connection, "one")
    add_stack(connection, environment, "Dup", "swarm", 1)
    add_stack(connection, environment, "dup", "swarm", 2)
    shutil.copyfile(MIGRATIONS_DIR / MIGRATION, directory / MIGRATION)

    with pytest.raises(sqlite3.IntegrityError, match=GUARD_COLUMN):
        migrate(connection, directory)

    remaining = connection.execute("SELECT name FROM stack ORDER BY id").fetchall()
    ledger = connection.execute("SELECT max(version) FROM schema_migration").fetchone()[0]
    leftovers = connection.execute(
        "SELECT name FROM sqlite_master WHERE name IN ('idx_stack_name_folded', 'migration_guard')"
    ).fetchall()
    assert [row[0] for row in remaining] == ["Dup", "dup"]
    assert ledger == len(list(directory.glob("*.sql"))) - 1
    assert leftovers == []


@pytest.mark.parametrize("name", ACCEPTED_SWARM_NAMES)
def test_a_swarm_stack_keeps_a_name_of_the_swarm_grammar(
    seeded: sqlite3.Connection, name: str
) -> None:
    add_stack(seeded, 1, name, "swarm", 1)

    assert seeded.execute("SELECT name FROM stack").fetchone()[0] == name


@pytest.mark.parametrize("name", REFUSED_SWARM_NAMES)
def test_a_swarm_stack_name_outside_the_grammar_is_refused_on_insert(
    seeded: sqlite3.Connection, name: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=SWARM_NAME_FAILED):
        add_stack(seeded, 1, name, "swarm", 1)


@pytest.mark.parametrize("name", REFUSED_SWARM_NAMES)
def test_a_swarm_stack_cannot_be_renamed_outside_the_grammar(
    seeded: sqlite3.Connection, name: str
) -> None:
    stack_id = add_stack(seeded, 1, "Shop", "swarm", 1)

    with pytest.raises(sqlite3.IntegrityError, match=SWARM_NAME_FAILED):
        seeded.execute("UPDATE stack SET name = ? WHERE id = ?", (name, stack_id))

    assert seeded.execute("SELECT name FROM stack").fetchone()[0] == "Shop"


def test_a_swarm_stack_can_be_renamed_within_the_grammar_and_updated_otherwise(
    seeded: sqlite3.Connection,
) -> None:
    stack_id = add_stack(seeded, 1, "Shop", "swarm", 1)

    seeded.execute("UPDATE stack SET name = 'Renamed' WHERE id = ?", (stack_id,))
    seeded.execute("UPDATE stack SET status = 'active' WHERE id = ?", (stack_id,))

    row = seeded.execute("SELECT name, status FROM stack").fetchone()
    assert tuple(row) == ("Renamed", "active")


def test_changing_a_compose_row_to_swarm_applies_the_swarm_grammar_and_back_the_table_check(
    seeded: sqlite3.Connection,
) -> None:
    compose_id = add_stack(seeded, 1, "shop", "compose", 1)
    dotted_id = add_stack(seeded, 1, "other", "compose", 2)

    seeded.execute("UPDATE stack SET kind = 'swarm' WHERE id = ?", (compose_id,))
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        seeded.execute(
            "UPDATE stack SET name = 'my.stack', kind = 'swarm' WHERE id = ?", (dotted_id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        seeded.execute(
            "UPDATE stack SET name = 'Mixed', kind = 'compose' WHERE id = ?", (compose_id,)
        )


def test_a_compose_name_is_still_governed_by_the_table_check_alone(
    seeded: sqlite3.Connection,
) -> None:
    add_stack(seeded, 1, "ok-name", "compose", 1)

    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        add_stack(seeded, 1, "Upper", "compose", 2)


@pytest.mark.parametrize(
    ("first", "first_kind", "second", "second_kind"),
    [
        pytest.param("Shop", "swarm", "SHOP", "swarm", id="two-swarm-names"),
        pytest.param("shop", "compose", "Shop", "swarm", id="swarm-over-compose"),
        pytest.param("Shop", "swarm", "shop", "compose", id="compose-over-swarm"),
    ],
)
def test_two_stacks_of_one_environment_cannot_share_a_folded_name_across_kinds(
    seeded: sqlite3.Connection, first: str, first_kind: str, second: str, second_kind: str
) -> None:
    add_stack(seeded, 1, first, first_kind, 1)

    with pytest.raises(sqlite3.IntegrityError, match=UNIQUE_FAILED):
        add_stack(seeded, 1, second, second_kind, 2)


def test_a_swarm_name_may_repeat_in_another_environment(seeded: sqlite3.Connection) -> None:
    add_stack(seeded, 1, "Shop", "swarm", 1)

    add_stack(seeded, 2, "Shop", "swarm", 2)

    assert seeded.execute("SELECT count(*) FROM stack").fetchone()[0] == 2


def test_a_rename_cannot_fold_onto_another_stack(seeded: sqlite3.Connection) -> None:
    add_stack(seeded, 1, "Shop", "swarm", 1)
    other = add_stack(seeded, 1, "Blog", "swarm", 2)

    with pytest.raises(sqlite3.IntegrityError, match=UNIQUE_FAILED):
        seeded.execute("UPDATE stack SET name = 'SHOP' WHERE id = ?", (other,))


@pytest.mark.parametrize("token", VALID_TOKENS)
def test_a_value_token_of_twelve_lower_case_base_36_characters_or_none_is_stored(
    seeded: sqlite3.Connection, token: str | None
) -> None:
    stack_id = add_stack(seeded, 1, "shop", "compose", 1)

    add_variable(seeded, stack_id, "TOKEN", token=token)

    assert seeded.execute("SELECT value_token FROM stack_variable").fetchone()[0] == token


@pytest.mark.parametrize("token", INVALID_TOKENS)
def test_any_other_value_token_is_refused(seeded: sqlite3.Connection, token: str) -> None:
    stack_id = add_stack(seeded, 1, "shop", "compose", 1)

    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        add_variable(seeded, stack_id, "TOKEN", token=token)
