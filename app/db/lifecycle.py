"""Database startup: create the file if needed and bring the schema up to date."""

import asyncio
from pathlib import Path

from app.config import Settings
from app.db.engine import Database
from app.db.migrations import MIGRATIONS_DIR, migrate


def _prepare(database: Database, migrations_dir: Path) -> None:
    database.path.parent.mkdir(parents=True, exist_ok=True)
    connection = database.connect()
    try:
        migrate(connection, migrations_dir)
    finally:
        connection.close()


async def initialise_database(
    settings: Settings, migrations_dir: Path = MIGRATIONS_DIR
) -> Database:
    """Migrate the configured database, raising MigrationError if it diverges from the files."""
    database = Database.from_settings(settings)
    await asyncio.to_thread(_prepare, database, migrations_dir)
    return database
