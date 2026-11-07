"""Synchronous reads and writes on a database snapshot, through a connection the caller owns.

Every function blocks; call it from a worker thread, never on the event loop.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.db.migrations.runner import LEDGER_TABLE
from app.db.repositories.activity_event import activity_sequence
from app.db.repositories.pagination import MAX_ROW_ID

_SAVEPOINT = "backup_snapshot"
_INTERNAL_TABLES = {
    "sqlite_sequence": "CREATE TABLE sqlite_sequence(name,seq)",
    "sqlite_stat1": "CREATE TABLE sqlite_stat1(tbl,idx,stat)",
    "sqlite_stat4": "CREATE TABLE sqlite_stat4(tbl,idx,neq,nlt,ndlt,sample)",
}
_AUTOINDEX_PREFIX = "sqlite_autoindex_"

type SchemaObject = tuple[str, str, str, str | None]


type LiveState = Literal["absent", "readable", "unreadable"]


@dataclass(frozen=True, slots=True)
class InstanceCounts:
    """How many of each managed object a database holds."""

    users: int
    environments: int
    stacks: int
    registry_credentials: int
    object_storage_targets: int
    automation_rules: int


@contextmanager
def _savepoint(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute(f"SAVEPOINT {_SAVEPOINT}")
    try:
        yield
    except BaseException:
        connection.execute(f"ROLLBACK TO {_SAVEPOINT}")
        connection.execute(f"RELEASE {_SAVEPOINT}")
        raise
    connection.execute(f"RELEASE {_SAVEPOINT}")


def _bounded(limit: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= MAX_ROW_ID:
        raise ValueError("limit must be a positive integer")
    return limit


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def vacuum_into(connection: sqlite3.Connection, target: Path) -> None:
    """Write a consistent copy of the connection's database to target, which must not exist."""
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"snapshot target already exists: {target}")
    if connection.in_transaction:
        raise RuntimeError("VACUUM INTO cannot run inside a transaction")
    try:
        connection.execute("VACUUM INTO ?", (str(target),))
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def schema_version(connection: sqlite3.Connection) -> int:
    """Return the highest version in the migration ledger, 0 when there is none."""
    if not _table_exists(connection, LEDGER_TABLE):
        return 0
    row = connection.execute(f"SELECT max(version) FROM {LEDGER_TABLE}").fetchone()
    return 0 if row is None or row[0] is None else int(row[0])


def strip_ephemeral(connection: sqlite3.Connection) -> None:
    """Delete every browser session and login throttle row, overwriting their content on disk.

    Leaves secure_delete switched on for the connection.
    """
    connection.execute("PRAGMA secure_delete = ON")
    with _savepoint(connection):
        connection.execute("DELETE FROM session")
        connection.execute("DELETE FROM login_throttle")


def integrity_findings(connection: sqlite3.Connection, *, limit: int) -> list[str]:
    """Return up to limit integrity findings and up to limit foreign key findings."""
    bounded = _bounded(limit)
    findings = [
        str(row[0])
        for row in connection.execute(f"PRAGMA integrity_check({bounded})").fetchall()
        if str(row[0]) != "ok"
    ]
    cursor = connection.execute("PRAGMA foreign_key_check")
    for table, row_id, parent, _ in cursor.fetchmany(bounded):
        findings.append(f"{table} row {row_id} refers to a missing {parent} row")
    cursor.close()
    return findings


def _is_internal(item: SchemaObject) -> bool:
    kind, name, table, sql = item
    match kind:
        case "table":
            return table == name and _INTERNAL_TABLES.get(name) == sql
        case "index":
            return sql is None and name.startswith(f"{_AUTOINDEX_PREFIX}{table}_")
        case _:
            return False


def schema_objects(connection: sqlite3.Connection) -> list[SchemaObject]:
    """Return (type, name, table, SQL) of every schema row, sorted.

    Only SQLite's own sequence and statistics tables, exactly as SQLite creates them, and its
    constraint indexes are left out; any other object with a reserved sqlite_ name is returned.
    """
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    objects = [(str(row[0]), str(row[1]), str(row[2]), row[3]) for row in rows]
    return [item for item in objects if not _is_internal(item)]


def enabled_administrators(connection: sqlite3.Connection, *, limit: int) -> list[str]:
    """Return up to limit usernames of enabled administrators, in code point order."""
    rows = connection.execute(
        "SELECT username FROM user WHERE role = 'administrator' AND is_enabled = 1 "
        "ORDER BY username COLLATE BINARY LIMIT ?",
        (_bounded(limit),),
    ).fetchall()
    return [str(row[0]) for row in rows]


def administrator_id(connection: sqlite3.Connection, username: str) -> int | None:
    """Return the id of the enabled administrator with this username, ignoring ASCII case."""
    row = connection.execute(
        "SELECT id FROM user WHERE username = ? AND role = 'administrator' AND is_enabled = 1",
        (username,),
    ).fetchone()
    return None if row is None else int(row[0])


def stack_files(connection: sqlite3.Connection, *, limit: int) -> list[tuple[str, bytes]]:
    """Return up to limit (project directory, compose file as UTF-8), by directory."""
    rows = connection.execute(
        "SELECT project_directory, compose_file FROM stack ORDER BY project_directory LIMIT ?",
        (_bounded(limit),),
    ).fetchall()
    return [(str(row[0]), str(row[1]).encode("utf-8")) for row in rows]


def instance_counts(connection: sqlite3.Connection) -> InstanceCounts:
    """Count the users, environments, stacks, credential sets, targets and rules."""
    row = connection.execute(
        "SELECT (SELECT count(*) FROM user), (SELECT count(*) FROM environment), "
        "(SELECT count(*) FROM stack), (SELECT count(*) FROM registry_credential), "
        "(SELECT count(*) FROM object_storage_target), (SELECT count(*) FROM automation_rule)"
    ).fetchone()
    return InstanceCounts(
        users=int(row[0]),
        environments=int(row[1]),
        stacks=int(row[2]),
        registry_credentials=int(row[3]),
        object_storage_targets=int(row[4]),
        automation_rules=int(row[5]),
    )


@dataclass(frozen=True, slots=True)
class LiveDatabase:
    """What a read-only look at the live database found."""

    state: LiveState
    activity_floor: int
    counts: InstanceCounts | None


def read_live_database(database: Path) -> LiveDatabase:
    """Open database read-only and return its activity floor and counts, never writing to it.

    Blocks; call it from a worker thread. A file with a write-ahead log is opened without
    immutable, so the log is read.
    """
    if not database.is_file():
        return LiveDatabase("absent", 0, None)
    has_journal = database.with_name(f"{database.name}-wal").exists()
    uri = f"{database.absolute().as_uri()}?{'mode=ro' if has_journal else 'immutable=1'}"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            return LiveDatabase(
                "readable", activity_sequence(connection), instance_counts(connection)
            )
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        return LiveDatabase("unreadable", 0, None)
