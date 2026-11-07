"""A database file is created once, by the first connection of the process that owns it."""

import sqlite3
from pathlib import Path

import pytest

from app.config import Settings
from app.db.engine import Database, DatabaseClosed, connect
from app.db.migrations import migrate


def move_aside(live: Path, destination: Path) -> None:
    live.rename(destination)
    for suffix in ("-wal", "-shm"):
        sidecar = live.with_name(live.name + suffix)
        if sidecar.exists():
            sidecar.rename(destination.with_name(destination.name + suffix))


async def test_the_first_connection_from_settings_creates_the_file_and_serves_a_session(
    settings: Settings,
) -> None:
    database = Database.from_settings(settings)
    connection = database.connect()
    migrate(connection)
    connection.close()

    async with database.session() as session:
        row = await session.fetch_one("SELECT count(*) AS total FROM schema_migration")
    await database.aclose()

    assert settings.db_path.is_file()
    assert row is not None and row["total"] > 0


async def test_a_file_moved_away_is_not_recreated_by_a_later_session(settings: Settings) -> None:
    database = Database.from_settings(settings)
    database.connect().close()
    moved = settings.db_path.with_name("moved.db")
    move_aside(settings.db_path, moved)

    with pytest.raises(sqlite3.OperationalError):
        async with database.session():
            pass
    await database.aclose()

    assert not settings.db_path.exists()


@pytest.mark.parametrize("use", ["session", "connect"])
async def test_a_closed_database_refuses_every_use_and_never_recreates_its_file(
    settings: Settings, use: str
) -> None:
    database = Database.from_settings(settings)
    database.connect().close()
    move_aside(settings.db_path, settings.db_path.with_name("moved.db"))
    await database.aclose()

    with pytest.raises(DatabaseClosed):
        if use == "session":
            async with database.session():
                pass
        else:
            database.connect()

    assert database.closed
    assert not settings.db_path.exists()


def test_a_database_not_built_to_create_never_creates_a_missing_file(tmp_path: Path) -> None:
    absent = tmp_path / "absent.db"

    with pytest.raises(sqlite3.OperationalError):
        Database(absent).connect()
    with pytest.raises(sqlite3.OperationalError):
        connect(absent, create=False)

    assert not absent.exists()


def test_every_connection_distrusts_the_schema_and_secure_delete_is_on_when_asked(
    tmp_path: Path,
) -> None:
    plain = connect(tmp_path / "t.db")
    secure = connect(tmp_path / "t.db", secure_delete=True)
    try:
        trusted = plain.execute("PRAGMA trusted_schema").fetchone()[0]
        secure_deletes = secure.execute("PRAGMA secure_delete").fetchone()[0]
    finally:
        plain.close()
        secure.close()

    assert (trusted, secure_deletes) == (0, 1)
