"""Fixtures for the activity log: a migrated session, a controllable clock and a recorder."""

from collections.abc import AsyncIterator

import pytest

from app.db.engine import Database, Session
from app.services.activity import ActivityRecorder
from tests.activity.support import RecordingSession
from tests.auth_support import FakeClock
from tests.scheduler.support import Rig


@pytest.fixture
async def db(database: Database) -> AsyncIterator[Session]:
    """Open a session on a migrated database."""
    async with database.session() as opened:
        yield opened


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def recorder(db: Session, clock: FakeClock) -> ActivityRecorder:
    return ActivityRecorder(db, clock)


@pytest.fixture
def recording() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
def pure_recorder(recording: RecordingSession, clock: FakeClock) -> ActivityRecorder:
    """A recorder over a session that keeps statements instead of running them."""
    return ActivityRecorder(recording, clock)  # type: ignore[arg-type]


@pytest.fixture
async def rig(database: Database) -> AsyncIterator[Rig]:
    """The scheduler rig's builders over a migrated database; closed after the test."""
    built = Rig(database)
    yield built
    await built.close()
