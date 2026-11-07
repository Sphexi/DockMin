"""Tests for app/db/lifecycle.py."""

import sqlite3
from pathlib import Path

from app.config import Settings
from app.db import initialise_database
from app.db.migrations import load_migrations

ACCOUNTS_FILE = "0001_accounts.sql"
CREATE_ACCOUNTS = "CREATE TABLE accounts (id INTEGER PRIMARY KEY);\n"
COMMENTED_ACCOUNTS = "-- the accounts table\n" + CREATE_ACCOUNTS


def read_state(path: Path) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT 'ledger', version, name, applied_at, checksum FROM schema_migration "
            "UNION ALL SELECT type, name, sql, NULL, NULL FROM sqlite_master"
        ).fetchall()
    finally:
        connection.close()
    return sorted(rows, key=repr)


async def test_initialise_database_on_a_fresh_data_directory_applies_every_migration(
    settings: Settings,
) -> None:
    database = await initialise_database(settings)

    connection = sqlite3.connect(database.path)
    try:
        applied = connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    finally:
        connection.close()
    assert applied == len(load_migrations())
    assert {"user", "activity_event", "registry_credential"} <= tables


async def test_initialise_database_a_second_time_changes_nothing(settings: Settings) -> None:
    first = await initialise_database(settings)
    before = read_state(first.path)

    second = await initialise_database(settings)

    assert read_state(second.path) == before


async def test_initialise_database_after_an_applied_migration_gains_comments_changes_nothing(
    settings: Settings, tmp_path: Path
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / ACCOUNTS_FILE).write_text(CREATE_ACCOUNTS, encoding="utf-8")
    first = await initialise_database(settings, migrations_dir)
    before = read_state(first.path)
    (migrations_dir / ACCOUNTS_FILE).write_text(COMMENTED_ACCOUNTS, encoding="utf-8")

    second = await initialise_database(settings, migrations_dir)

    assert read_state(second.path) == before
