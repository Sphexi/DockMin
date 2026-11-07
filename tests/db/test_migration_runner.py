"""Tests for app/db/migrations/runner.py using throwaway migrations."""

import re
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.backup import restore_steps
from app.db.engine import connect
from app.db.migrations import (
    MIGRATIONS_DIR,
    MigrationError,
    NewerSchemaError,
    load_migrations,
    migrate,
    runner,
    verify_ledger,
)
from app.db.sql_text import normalise_sql

ACCOUNTS_FILE = "0001_accounts.sql"
ORDERS_FILE = "0002_orders.sql"
ITEMS_FILE = "0003_items.sql"
CREATE_ACCOUNTS = "CREATE TABLE accounts (id INTEGER PRIMARY KEY);\n"
CREATE_ORDERS = "CREATE TABLE orders (id INTEGER PRIMARY KEY);\n"
CREATE_ITEMS = "CREATE TABLE items (id INTEGER PRIMARY KEY);\n"
COMMENTED_ACCOUNTS = (
    "-- the accounts table\n/* kept for reporting */\n" + CREATE_ACCOUNTS + "-- end of file\n"
)
EARLIER_RELEASE_HEAD = 28
EARLIER_CHECKSUM = "0123abcd"
FAILING_SQL = (
    "CREATE TABLE doomed (id INTEGER PRIMARY KEY);\nINSERT INTO no_such_table VALUES (1);\n"
)
TRIGGER_SQL = (
    "CREATE TABLE source (id INTEGER PRIMARY KEY);\n"
    "CREATE TABLE audit (source_id INTEGER);\n"
    "CREATE TRIGGER source_audit AFTER INSERT ON source\n"
    "BEGIN\n"
    "    INSERT INTO audit (source_id) VALUES (NEW.id);\n"
    "END;\n"
)
CREATE_TWO_TABLES = CREATE_ACCOUNTS + CREATE_ORDERS
FOREIGN_KEYS_OFF_COMMENT = "-- foreign keys: off during this migration\n"
FIRST, SECOND = 1, 2
PARENT_WITH_CHILDREN = (
    "CREATE TABLE parent (id INTEGER PRIMARY KEY, label TEXT);\n"
    "CREATE TABLE child (\n"
    "    id INTEGER PRIMARY KEY,\n"
    "    parent_id INTEGER REFERENCES parent (id) ON DELETE CASCADE\n"
    ");\n"
    "INSERT INTO parent (id, label) VALUES (1, 'a'), (2, 'b');\n"
    "INSERT INTO child (id, parent_id) VALUES (1, 1), (2, 2);\n"
)
REBUILD_PARENT = (
    "CREATE TABLE parent_new (id INTEGER PRIMARY KEY, label TEXT, extra TEXT);\n"
    "INSERT INTO parent_new (id, label) SELECT id, label FROM parent;\n"
    "DROP TABLE parent;\n"
    "ALTER TABLE parent_new RENAME TO parent;\n"
)
ORPHAN_A_CHILD = "INSERT INTO child (id, parent_id) VALUES (3, 99);\n"
RECORD_FOREIGN_KEYS = (
    "CREATE TABLE seen AS SELECT foreign_keys AS enabled FROM pragma_foreign_keys;\n"
)
CHILDREN_SQL = "SELECT COUNT(*) FROM child"
DROP_TABLE = re.compile(r"DROP TABLE (?:IF EXISTS )?\"?(\w+)\"?", re.IGNORECASE)
GUARD_TABLE = "migration_guard"
ANNOTATE_SETTING_TABLE = (
    "UPDATE sqlite_master SET sql = replace(sql, 'CREATE TABLE setting (', "
    "'CREATE TABLE setting ( -- note' || char(10) || '      /* kept */   ') "
    "WHERE name = 'setting'"
)


def write_migration(directory: Path, filename: str, sql: str, newline: str = "\n") -> None:
    (directory / filename).write_text(sql, encoding="utf-8", newline=newline)


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def ledger_versions(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
    return [row[0] for row in rows]


def copy_migrations_through(directory: Path, last: int) -> None:
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if int(path.name[:4]) <= last:
            shutil.copy(path, directory / path.name)


def referenced_tables(connection: sqlite3.Connection) -> set[str]:
    tables = [
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    return {
        row[2]
        for table in tables
        for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        if row[2] != table
    }


def referenced_drops_by_version() -> dict[int, bool]:
    """Map each migration that drops a referenced table to whether it creates a guard table."""
    connection = sqlite3.connect(":memory:", isolation_level=None)
    found: dict[int, bool] = {}
    for migration in load_migrations():
        dropped = set(DROP_TABLE.findall(normalise_sql(migration.sql)))
        if dropped & referenced_tables(connection):
            found[migration.version] = GUARD_TABLE in migration.sql
        connection.executescript(migration.sql)
    connection.close()
    return found


def ledger_checksums(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT checksum FROM schema_migration").fetchall()
    return {row[0] for row in rows}


def dump(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    """Return the ledger and the schema text, which together describe the database state."""
    ledger = connection.execute("SELECT * FROM schema_migration ORDER BY version").fetchall()
    schema = connection.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY name"
    ).fetchall()
    return [tuple(row) for row in ledger + schema]


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    path = tmp_path / "migrations"
    path.mkdir()
    return path


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    opened = connect(tmp_path / "runner.db")
    yield opened
    opened.close()


def test_migrate_applies_every_file_to_a_fresh_database(
    migrations_dir: Path, connection: sqlite3.Connection
) -> None:
    write_migration(migrations_dir, ACCOUNTS_FILE, CREATE_ACCOUNTS)
    write_migration(migrations_dir, ORDERS_FILE, CREATE_ORDERS)

    applied = migrate(connection, migrations_dir)

    assert [migration.filename for migration in applied] == [ACCOUNTS_FILE, ORDERS_FILE]
    assert {"accounts", "orders"} <= table_names(connection)
    assert ledger_versions(connection) == [1, 2]
    assert ledger_checksums(connection) == {""}


def test_migrate_a_second_time_applies_nothing_and_changes_nothing(
    migrations_dir: Path, connection: sqlite3.Connection
) -> None:
    write_migration(migrations_dir, ACCOUNTS_FILE, CREATE_ACCOUNTS)
    migrate(connection, migrations_dir)
    before = dump(connection)

    applied = migrate(connection, migrations_dir)

    assert applied == []
    assert dump(connection) == before


def test_migrate_applies_only_files_added_since_the_last_run(
    migrations_dir: Path, connection: sqlite3.Connection
) -> None:
    write_migration(migrations_dir, ACCOUNTS_FILE, CREATE_ACCOUNTS)
    migrate(connection, migrations_dir)
    write_migration(migrations_dir, ORDERS_FILE, CREATE_ORDERS)

    applied = migrate(connection, migrations_dir)

    assert [migration.filename for migration in applied] == [ORDERS_FILE]


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_migrate_after_an_applied_files_comments_and_line_endings_change_applies_nothing(
    migrations_dir: Path, connection: sqlite3.Connection, newline: str
) -> None:
    write_migration(migrations_dir, ACCOUNTS_FILE, CREATE_ACCOUNTS)
    migrate(connection, migrations_dir)
    write_migration(migrations_dir, ACCOUNTS_FILE, COMMENTED_ACCOUNTS, newline)

    applied = migrate(connection, migrations_dir)

    assert applied == []
    assert ledger_versions(connection) == [1]


def test_migrate_upgrades_a_ledger_holding_earlier_release_checksums_to_the_current_head(
    migrations_dir: Path, connection: sqlite3.Connection
) -> None:
    copy_migrations_through(migrations_dir, EARLIER_RELEASE_HEAD)
    migrate(connection, migrations_dir)
    connection.execute("UPDATE schema_migration SET checksum = ?", (EARLIER_CHECKSUM,))
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(ANNOTATE_SETTING_TABLE)
    connection.execute("PRAGMA writable_schema = OFF")
    head = load_migrations()[-1].version

    applied = migrate(connection)

    assert [migration.version for migration in applied] == list(
        range(EARLIER_RELEASE_HEAD + 1, head + 1)
    )
    assert ledger_versions(connection) == list(range(1, head + 1))
    assert foreign_keys_enabled(connection) == 1
    assert restore_steps._schema(connection) == restore_steps._fresh_schema()


def test_every_listed_foreign_keys_off_version_is_a_migration_of_this_build() -> None:
    assert runner.FOREIGN_KEYS_OFF_VERSIONS.issubset(m.version for m in load_migrations())


def test_every_migration_dropping_a_referenced_table_runs_with_foreign_keys_off_or_is_guarded() -> (
    None
):
    unprotected = {
        version
        for version, guarded in referenced_drops_by_version().items()
        if not guarded and version not in runner.FOREIGN_KEYS_OFF_VERSIONS
    }

    assert unprotected == set()


def test_migrate_with_a_missing_version_fails_naming_the_gap(
    migrations_dir: Path, connection: sqlite3.Connection
) -> None:
    write_migration(migrations_dir, ACCOUNTS_FILE, CREATE_ACCOUNTS)
    write_migration(migrations_dir, "0003_invoices.sql", CREATE_ORDERS)

    with pytest.raises(
        MigrationError, match=r"0002 is missing before 0003_invoices\.sql"
    ) as caught:
        migrate(connection, migrations_dir)

    assert caught.type is MigrationError


def test_migrate_with_a_duplicated_version_fails_naming_both_files(
    migrations_dir: Path, connection: sqlite3.Connection
) -> None:
    write_migration(migrations_dir, ACCOUNTS_FILE, CREATE_ACCOUNTS)
    write_migration(migrations_dir, "0001_customers.sql", CREATE_ORDERS)

    with pytest.raises(MigrationError, match=r"0001 is used by 0001_accounts\.sql") as caught:
        migrate(connection, migrations_dir)

    assert "0001_customers.sql" in str(caught.value)
    assert caught.type is MigrationError


def test_migrate_when_the_highest_applied_file_has_vanished_reports_a_newer_schema(
    migrations_dir: Path, connection: sqlite3.Connection
) -> None:
    write_migration(migrations_dir, ACCOUNTS_FILE, CREATE_ACCOUNTS)
    write_migration(migrations_dir, ORDERS_FILE, CREATE_ORDERS)
    migrate(connection, migrations_dir)
    (migrations_dir / ORDERS_FILE).unlink()

    with pytest.raises(NewerSchemaError) as caught:
        migrate(connection, migrations_dir)

    assert str(caught.value) == (
        "This database was written by a newer dockmin (schema 2; this build supports 1). "
        "Run that version or restore a backup."
    )


def test_migrate_when_an_applied_file_below_the_highest_has_vanished_fails_naming_the_gap(
    migrations_dir: Path, connection: sqlite3.Connection
) -> None:
    write_migration(migrations_dir, ACCOUNTS_FILE, CREATE_ACCOUNTS)
    write_migration(migrations_dir, ORDERS_FILE, CREATE_ORDERS)
    write_migration(migrations_dir, ITEMS_FILE, CREATE_ITEMS)
    migrate(connection, migrations_dir)
    (migrations_dir / ORDERS_FILE).unlink()

    with pytest.raises(MigrationError, match=rf"0002 is missing before {ITEMS_FILE}") as caught:
        migrate(connection, migrations_dir)

    assert caught.type is MigrationError


def test_verifying_a_ledger_with_an_applied_migration_absent_from_the_files_names_it(
    migrations_dir: Path, connection: sqlite3.Connection
) -> None:
    write_migration(migrations_dir, ACCOUNTS_FILE, CREATE_ACCOUNTS)
    write_migration(migrations_dir, ORDERS_FILE, CREATE_ORDERS)
    write_migration(migrations_dir, ITEMS_FILE, CREATE_ITEMS)
    migrate(connection, migrations_dir)
    accounts, _, items = load_migrations(migrations_dir)

    with pytest.raises(
        MigrationError, match=r"0002_orders\.sql was applied but its file"
    ) as caught:
        verify_ledger(connection, [accounts, items])

    assert caught.type is MigrationError


def test_migrate_with_a_failing_statement_rolls_back_the_table_and_the_ledger_row(
    migrations_dir: Path, connection: sqlite3.Connection
) -> None:
    write_migration(migrations_dir, ACCOUNTS_FILE, CREATE_ACCOUNTS)
    write_migration(migrations_dir, ORDERS_FILE, FAILING_SQL)

    with pytest.raises(sqlite3.OperationalError):
        migrate(connection, migrations_dir)

    assert "doomed" not in table_names(connection)
    assert "accounts" in table_names(connection)
    assert ledger_versions(connection) == [1]
    assert not connection.in_transaction


def test_migrate_applies_a_trigger_body_as_one_statement(
    migrations_dir: Path, connection: sqlite3.Connection
) -> None:
    write_migration(migrations_dir, ACCOUNTS_FILE, TRIGGER_SQL)
    migrate(connection, migrations_dir)

    connection.execute("INSERT INTO source (id) VALUES (7)")

    assert connection.execute("SELECT source_id FROM audit").fetchone()[0] == 7


def foreign_keys_enabled(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA foreign_keys").fetchone()[0])


def seen_during_migration(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT enabled FROM seen").fetchone()[0])


def test_a_migration_whose_version_is_listed_runs_with_foreign_keys_off_and_restores_them(
    migrations_dir: Path, connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "FOREIGN_KEYS_OFF_VERSIONS", frozenset({FIRST}))
    write_migration(migrations_dir, ACCOUNTS_FILE, RECORD_FOREIGN_KEYS)

    migrate(connection, migrations_dir)

    assert seen_during_migration(connection) == 0
    assert foreign_keys_enabled(connection) == 1


def test_a_comment_in_a_migration_file_does_not_turn_foreign_keys_off(
    migrations_dir: Path, connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "FOREIGN_KEYS_OFF_VERSIONS", frozenset())
    write_migration(migrations_dir, ACCOUNTS_FILE, FOREIGN_KEYS_OFF_COMMENT + RECORD_FOREIGN_KEYS)

    migrate(connection, migrations_dir)

    assert seen_during_migration(connection) == 1


@pytest.mark.parametrize(
    ("listed", "children_left"),
    [pytest.param({SECOND}, 2, id="listed"), pytest.param(set(), 0, id="unlisted")],
)
def test_rebuilding_a_parent_keeps_its_children_only_when_the_version_is_listed(
    migrations_dir: Path,
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    listed: set[int],
    children_left: int,
) -> None:
    monkeypatch.setattr(runner, "FOREIGN_KEYS_OFF_VERSIONS", frozenset(listed))
    write_migration(migrations_dir, ACCOUNTS_FILE, PARENT_WITH_CHILDREN)
    write_migration(migrations_dir, ORDERS_FILE, REBUILD_PARENT)

    migrate(connection, migrations_dir)

    assert connection.execute(CHILDREN_SQL).fetchone()[0] == children_left
    assert connection.execute("SELECT COUNT(*) FROM parent").fetchone()[0] == 2


def test_a_listed_migration_that_leaves_a_dangling_reference_is_rolled_back_by_name(
    migrations_dir: Path, connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "FOREIGN_KEYS_OFF_VERSIONS", frozenset({SECOND}))
    write_migration(migrations_dir, ACCOUNTS_FILE, PARENT_WITH_CHILDREN)
    write_migration(migrations_dir, ORDERS_FILE, REBUILD_PARENT + ORPHAN_A_CHILD)

    with pytest.raises(MigrationError, match=r"0002_orders\.sql.*child"):
        migrate(connection, migrations_dir)

    columns = [row[1] for row in connection.execute("PRAGMA table_info(parent)")]
    assert columns == ["id", "label"]
    assert "parent_new" not in table_names(connection)
    assert connection.execute(CHILDREN_SQL).fetchone()[0] == 2
    assert ledger_versions(connection) == [1]
    assert not connection.in_transaction
    assert foreign_keys_enabled(connection) == 1


def test_a_listed_migration_with_a_failing_statement_rolls_back_and_restores_foreign_keys(
    migrations_dir: Path, connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "FOREIGN_KEYS_OFF_VERSIONS", frozenset({SECOND}))
    write_migration(migrations_dir, ACCOUNTS_FILE, CREATE_ACCOUNTS)
    write_migration(migrations_dir, ORDERS_FILE, FAILING_SQL)

    with pytest.raises(sqlite3.OperationalError):
        migrate(connection, migrations_dir)

    assert "doomed" not in table_names(connection)
    assert ledger_versions(connection) == [1]
    assert not connection.in_transaction
    assert foreign_keys_enabled(connection) == 1
