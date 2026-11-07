"""Datastore fixtures built on the migrated database in tests/conftest.py."""

import sqlite3
from collections.abc import AsyncIterator, Iterator

import pytest

from app.db.engine import Database, Session
from app.db.repositories import (
    EnvironmentGroupRepository,
    SettingRepository,
    TagRepository,
    TeamMembershipRepository,
    TeamRepository,
    UserRepository,
)
from tests.db.helpers import CREATED_AT, at


@pytest.fixture
def raw_connection(database: Database) -> Iterator[sqlite3.Connection]:
    """Return a synchronous connection to the migrated database."""
    connection = database.connect()
    yield connection
    connection.close()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[Session]:
    """Return an open session on the migrated database."""
    async with database.session() as opened:
        yield opened


@pytest.fixture
def user_repo(session: Session) -> UserRepository:
    return UserRepository(session, clock=at(CREATED_AT))


@pytest.fixture
def team_repo(session: Session) -> TeamRepository:
    return TeamRepository(session, clock=at(CREATED_AT))


@pytest.fixture
def tag_repo(session: Session) -> TagRepository:
    return TagRepository(session, clock=at(CREATED_AT))


@pytest.fixture
def group_repo(session: Session) -> EnvironmentGroupRepository:
    return EnvironmentGroupRepository(session, clock=at(CREATED_AT))


@pytest.fixture
def setting_repo(session: Session) -> SettingRepository:
    return SettingRepository(session, clock=at(CREATED_AT))


@pytest.fixture
def membership_repo(session: Session) -> TeamMembershipRepository:
    return TeamMembershipRepository(session, clock=at(CREATED_AT))
