"""Tests for app/db/engine.py."""

import sqlite3
from pathlib import Path

import pytest

from app.db.engine import Database, DatabaseError, Session, connect

BUSY_TIMEOUT_MS = 1234
SETTING_KEY = "engine.test"
INSERT_SETTING = (
    "INSERT INTO setting (key, value_type, value, updated_at) VALUES (?, 'string', 'v', 't')"
)
COUNT_SETTINGS = "SELECT COUNT(*) FROM setting"
JOURNAL_SIZE_LIMIT_BYTES = 4 * 1024 * 1024


def test_connect_enables_wal_foreign_keys_and_the_requested_busy_timeout(tmp_path: Path) -> None:
    connection = connect(tmp_path / "pragmas.db", busy_timeout_ms=BUSY_TIMEOUT_MS)

    try:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        connection.close()
    assert (journal_mode, foreign_keys, busy_timeout) == ("wal", 1, BUSY_TIMEOUT_MS)


async def test_transaction_rolls_back_every_write_when_the_block_raises(session: Session) -> None:
    with pytest.raises(RuntimeError):
        async with session.transaction():
            await session.execute(INSERT_SETTING, (SETTING_KEY,))
            raise RuntimeError

    remaining = await session.fetch_one(COUNT_SETTINGS)
    assert remaining is not None
    assert remaining[0] == 0


async def test_transaction_commits_its_writes_when_the_block_completes(
    database: Database, session: Session
) -> None:
    async with session.transaction():
        await session.execute(INSERT_SETTING, (SETTING_KEY,))

    other = sqlite3.connect(database.path)
    try:
        committed = other.execute(COUNT_SETTINGS).fetchone()[0]
    finally:
        other.close()
    assert committed == 1


async def test_transaction_opened_inside_another_raises_and_leaves_the_outer_usable(
    session: Session,
) -> None:
    async with session.transaction():
        with pytest.raises(DatabaseError, match="do not nest"):
            async with session.transaction():
                pass
        await session.execute(INSERT_SETTING, (SETTING_KEY,))

    stored = await session.fetch_one(COUNT_SETTINGS)
    assert stored is not None
    assert stored[0] == 1


async def test_fetch_limited_returns_at_most_the_requested_rows(session: Session) -> None:
    await session.execute(INSERT_SETTING, ("a",))
    await session.execute(INSERT_SETTING, ("b",))
    await session.execute(INSERT_SETTING, ("c",))

    rows = await session.fetch_limited("SELECT key FROM setting ORDER BY key", (), limit=2)

    assert len(rows) == 2


@pytest.mark.parametrize("limit", [0, -1])
async def test_fetch_limited_rejects_a_limit_below_one(session: Session, limit: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        await session.fetch_limited("SELECT key FROM setting", (), limit=limit)


def test_connect_caps_the_size_the_write_ahead_log_may_keep(tmp_path: Path) -> None:
    connection = connect(tmp_path / "limit.db")

    try:
        limit = connection.execute("PRAGMA journal_size_limit").fetchone()[0]
    finally:
        connection.close()

    assert limit == JOURNAL_SIZE_LIMIT_BYTES


def wal_files_exist(path: Path) -> bool:
    return all(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm"))


def any_wal_file_exists(path: Path) -> bool:
    return any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm"))


@pytest.mark.parametrize(
    ("keep_wal", "held_between_sessions"),
    [pytest.param(True, True, id="held"), pytest.param(False, False, id="not-held")],
)
async def test_the_wal_files_persist_between_sessions_only_while_a_connection_is_held(
    tmp_path: Path, keep_wal: bool, held_between_sessions: bool
) -> None:
    path = tmp_path / "held.db"
    database = Database(path, create=True, keep_wal=keep_wal)
    async with database.session() as session:
        await session.execute("CREATE TABLE note (body TEXT)")

    between_sessions = wal_files_exist(path)
    await database.aclose()

    assert between_sessions is held_between_sessions
    assert not any_wal_file_exists(path)


async def test_the_held_connection_is_opened_by_the_first_session_and_not_before(
    tmp_path: Path,
) -> None:
    path = tmp_path / "late.db"
    database = Database(path, create=True, keep_wal=True)

    before_any_session = any_wal_file_exists(path)
    async with database.session() as session:
        await session.execute("CREATE TABLE note (body TEXT)")
    after_first_session = wal_files_exist(path)
    await database.aclose()

    assert (before_any_session, after_first_session) == (False, True)


async def test_every_session_of_a_database_shares_one_slot_and_another_database_has_its_own(
    tmp_path: Path,
) -> None:
    first = Database(tmp_path / "first.db", create=True)
    second = Database(tmp_path / "second.db", create=True)

    async with first.session() as one, first.session() as two, second.session() as other:
        shared = (one.shared, two.shared, other.shared)
    await first.aclose()
    await second.aclose()

    assert shared[0] is shared[1] is first.shared
    assert shared[2] is second.shared and shared[2] is not first.shared
