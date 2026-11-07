"""Tests for behaviour across separate connections to one database file."""

import sqlite3

import pytest

from app.db.engine import Database
from tests.db.helpers import CREATED_STAMP

NO_WAIT_MS = 0
INSERT_TEAM = "INSERT INTO team (name, created_at) VALUES (?, ?)"
COUNT_TEAMS = "SELECT COUNT(*) FROM team"
TEAM_NAME = "writers"


async def test_open_write_transaction_does_not_block_a_reader_but_does_block_a_second_writer(
    database: Database,
) -> None:
    # A zero busy timeout turns any lock wait into an immediate error, so success proves the
    # reader never waited at all.
    impatient = Database(database.path, busy_timeout_ms=NO_WAIT_MS)
    async with (
        database.session() as writer,
        impatient.session() as reader,
        impatient.session() as rival,
    ):
        async with writer.transaction():
            await writer.execute(INSERT_TEAM, (TEAM_NAME, CREATED_STAMP))

            during = await reader.fetch_one(COUNT_TEAMS)
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                async with rival.transaction():
                    pass

        after = await reader.fetch_one(COUNT_TEAMS)

    assert during is not None
    assert during[0] == 0
    assert after is not None
    assert after[0] == 1
