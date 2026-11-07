"""The query behind the database readiness check."""

from app.db.engine import Session


async def schema_is_readable(session: Session) -> bool:
    """Return True if the database answers a query that reads its schema."""
    # sqlite_master is read from the file, so a missing, truncated or unmigrated database
    # fails here where SELECT 1 would succeed.
    return await session.fetch_one("SELECT 1 FROM sqlite_master LIMIT 1") is not None
