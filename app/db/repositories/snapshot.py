"""Consistent point-in-time copy of the database, safe to take while writes continue."""

import asyncio
import sqlite3
from pathlib import Path

from app.db.engine import Database


def _snapshot(database: Database, destination: Path) -> Path:
    if destination.exists():
        raise FileExistsError(f"snapshot destination already exists: {destination}")
    partial = destination.with_name(destination.name + ".partial")
    partial.unlink(missing_ok=True)
    connection = database.connect()
    try:
        # A read transaction in WAL mode sees one consistent state and never blocks writers.
        connection.execute("VACUUM INTO ?", (str(partial),))
    except BaseException:
        connection.close()
        partial.unlink(missing_ok=True)
        raise
    connection.close()
    _make_self_contained(partial)
    partial.replace(destination)
    return destination


def _make_self_contained(path: Path) -> None:
    # Without this the copy would need -wal and -shm sidecar files to be opened.
    copy = sqlite3.connect(path)
    try:
        copy.execute("PRAGMA journal_mode = DELETE")
    finally:
        copy.close()


async def snapshot_database(database: Database, destination: Path) -> Path:
    """Write a consistent copy of the database to destination, which must not exist."""
    return await asyncio.to_thread(_snapshot, database, destination)
