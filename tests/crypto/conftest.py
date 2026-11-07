"""Fixtures for the credential encryption tests, built on the migrated database."""

from collections.abc import AsyncIterator

import pytest

from app.crypto.vault import Vault
from app.db.engine import Database, Session
from tests.crypto.helpers import (
    FIRST_KEY,
    SECOND_KEY,
    CredentialRepository,
    TracingDatabase,
    add_credential,
    start,
    vault_with,
)


@pytest.fixture
async def session(database: Database) -> AsyncIterator[Session]:
    """Return an open session on the migrated database."""
    async with database.session() as opened:
        yield opened


@pytest.fixture
def vault() -> Vault:
    """Return a vault holding the first key at version 1."""
    return vault_with(FIRST_KEY)


@pytest.fixture
def credentials(session: Session, vault: Vault) -> CredentialRepository:
    """Return a repository over every registered secret column."""
    return CredentialRepository(session, vault)


@pytest.fixture
def traced_database(database: Database) -> TracingDatabase:
    """Return the same database file, recording every statement run through it."""
    return TracingDatabase(database.path)


@pytest.fixture
async def database_at_version_one(database: Database) -> Database:
    """A database started under the first key and holding one credential sealed at version 1."""
    vault = await start(database, FIRST_KEY)
    await add_credential(database, vault)
    return database


@pytest.fixture
async def rotated_with_a_stranded_value(database_at_version_one: Database) -> Database:
    """A database rotated to the second key whose one credential is still at version 1."""
    await start(database_at_version_one, SECOND_KEY, FIRST_KEY, allow_rotation=True)
    return database_at_version_one
