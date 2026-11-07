"""Tests for app/db/repositories/snapshot.py."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.db.engine import Database
from app.db.repositories import snapshot_database
from tests.db.helpers import CREATED_STAMP

INSERT_TEAM = "INSERT INTO team (name, created_at) VALUES (?, ?)"
COMMITTED_TEAM = "committed"
UNCOMMITTED_TEAM = "uncommitted"
EXISTING_CONTENT = b"already here"


async def test_snapshot_taken_during_an_open_write_is_consistent_and_self_contained(
    database: Database, tmp_path: Path
) -> None:
    destination = tmp_path / "snapshot.db"
    async with database.session() as writer:
        await writer.execute(INSERT_TEAM, (COMMITTED_TEAM, CREATED_STAMP))
        async with writer.transaction():
            await writer.execute(INSERT_TEAM, (UNCOMMITTED_TEAM, CREATED_STAMP))

            await snapshot_database(database, destination)

    with closing(sqlite3.connect(destination)) as copy:
        integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
        teams = copy.execute("SELECT group_concat(name) FROM team").fetchone()[0]
        journal_mode = copy.execute("PRAGMA journal_mode").fetchone()[0]
    assert integrity == "ok"
    assert teams == COMMITTED_TEAM
    assert journal_mode == "delete"
    assert not destination.with_name("snapshot.db-wal").exists()
    assert not destination.with_name("snapshot.db.partial").exists()


async def test_snapshot_to_an_existing_destination_raises_and_leaves_it_untouched(
    database: Database, tmp_path: Path
) -> None:
    destination = tmp_path / "snapshot.db"
    destination.write_bytes(EXISTING_CONTENT)

    with pytest.raises(FileExistsError):
        await snapshot_database(database, destination)

    assert destination.read_bytes() == EXISTING_CONTENT
