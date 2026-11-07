"""Forward-only SQL migration runner backed by a version ledger."""

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.logging import get_logger

MIGRATIONS_DIR = Path(__file__).resolve().parent
LEDGER_TABLE = "schema_migration"

FOREIGN_KEYS_OFF_VERSIONS = frozenset({19, 24})

_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

# Unused and written empty; dropping it would make existing ledgers differ from a fresh one.
_CREATE_LEDGER = f"""
CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL,
    checksum    TEXT NOT NULL
)
"""

logger = get_logger(__name__)


class MigrationError(Exception):
    """Raised when the migration files and the ledger cannot be reconciled."""


class NewerSchemaError(MigrationError):
    """Raised when the database records a migration this build does not have."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One migration file, read."""

    version: int
    name: str
    filename: str
    sql: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Read every NNNN_name.sql file in directory, ordered by version."""
    found: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"migration file {path.name} does not match NNNN_name.sql")
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"migration version {version:04d} is used by {found[version].filename}"
                f" and {path.name}"
            )
        text = path.read_text(encoding="utf-8")
        found[version] = Migration(
            version=version,
            name=match.group(2),
            filename=path.name,
            sql=text.replace("\r\n", "\n").replace("\r", "\n"),
        )
    migrations = [found[version] for version in sorted(found)]
    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise MigrationError(f"migration {expected:04d} is missing before {migration.filename}")
    return migrations


def verify_ledger(connection: sqlite3.Connection, migrations: list[Migration]) -> list[Migration]:
    """Check the ledger against the files and return the migrations still to apply."""
    ledger = connection.execute(
        f"SELECT version, name FROM {LEDGER_TABLE} ORDER BY version"
    ).fetchall()
    by_version = {migration.version: migration for migration in migrations}
    supported = migrations[-1].version if migrations else 0
    newest = max((version for version, _ in ledger), default=0)
    if newest > supported:
        raise NewerSchemaError(
            f"This database was written by a newer dockmin (schema {newest}; this build "
            f"supports {supported}). Run that version or restore a backup."
        )
    for expected, (version, name) in enumerate(ledger, start=1):
        if version != expected:
            raise MigrationError(f"the ledger has no record of migration {expected:04d}")
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(
                f"migration {version:04d}_{name}.sql was applied but its file is missing"
            )
        if migration.name != name:
            raise MigrationError(
                f"migration {version:04d} was applied as {version:04d}_{name}.sql"
                f" but the file is now {migration.filename}"
            )
    return migrations[len(ledger) :]


def _split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        # complete_statement keeps CREATE TRIGGER ... BEGIN ... END; as one statement.
        if sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    if buffer.strip():
        statements.append(buffer)
    return statements


def _apply(connection: sqlite3.Connection, migration: Migration) -> bool:
    """Apply one migration atomically; return False if another process already did."""
    if migration.version not in FOREIGN_KEYS_OFF_VERSIONS:
        return _apply_in_transaction(connection, migration, check_foreign_keys=False)
    # SQLite ignores this pragma inside a transaction, so it is set before BEGIN.
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        return _apply_in_transaction(connection, migration, check_foreign_keys=True)
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _apply_in_transaction(
    connection: sqlite3.Connection, migration: Migration, *, check_foreign_keys: bool
) -> bool:
    connection.execute("BEGIN IMMEDIATE")
    try:
        recorded = connection.execute(
            f"SELECT 1 FROM {LEDGER_TABLE} WHERE version = ?", (migration.version,)
        ).fetchone()
        if recorded is not None:
            connection.execute("ROLLBACK")
            return False
        for statement in _split_statements(migration.sql):
            connection.execute(statement)
        if check_foreign_keys:
            violation = connection.execute("PRAGMA foreign_key_check").fetchone()
            if violation is not None:
                raise MigrationError(
                    f"migration {migration.filename} would leave a row in {violation[0]}"
                    " referring to a row that does not exist"
                )
        connection.execute(
            f"INSERT INTO {LEDGER_TABLE} (version, name, applied_at, checksum) VALUES (?, ?, ?, ?)",
            (
                migration.version,
                migration.name,
                datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "",
            ),
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return True


def migrate(connection: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Verify the ledger, apply every pending migration, and return those applied."""
    connection.execute(_CREATE_LEDGER)
    migrations = load_migrations(directory)
    applied: list[Migration] = []
    for migration in verify_ledger(connection, migrations):
        if _apply(connection, migration):
            logger.info("migration_applied", extra={"migration": migration.filename})
            applied.append(migration)
    # A concurrent starter may have applied some of them; confirm the final state is consistent.
    verify_ledger(connection, migrations)
    return applied
