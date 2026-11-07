"""Migration 0007: the paused and unhealthy container counts on the environment snapshot."""

import sqlite3

import pytest

from app.db.engine import Database
from tests.db.helpers import CREATED_STAMP

INSERT_ENVIRONMENT = (
    "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
    "VALUES ('host', 'unix_socket', '/var/run/docker.sock', ?, ?)"
)
INSERT_EMPTY_SNAPSHOT = (
    "INSERT INTO environment_snapshot (environment_id, captured_at) VALUES (?, ?)"
)
SELECT_COUNTS = (
    "SELECT container_paused_count AS paused, container_unhealthy_count AS unhealthy "
    "FROM environment_snapshot"
)
NEGATIVE = -1


async def test_the_counts_are_null_by_default_and_refuse_a_negative_value(
    database: Database,
) -> None:
    async with database.session() as db:
        environment = (
            await db.execute(INSERT_ENVIRONMENT, (CREATED_STAMP, CREATED_STAMP))
        ).last_row_id
        await db.execute(INSERT_EMPTY_SNAPSHOT, (environment, CREATED_STAMP))
        stored = await db.fetch_limited(SELECT_COUNTS, (), limit=1)

        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "UPDATE environment_snapshot SET container_paused_count = ?", (NEGATIVE,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "UPDATE environment_snapshot SET container_unhealthy_count = ?", (NEGATIVE,)
            )

    assert (stored[0]["paused"], stored[0]["unhealthy"]) == (None, None)
