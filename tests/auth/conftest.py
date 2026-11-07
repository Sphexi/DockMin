"""Service-level fixtures: accounts in a migrated database, a controllable clock, real services."""

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.auth.passwords import PasswordHasher
from app.auth.principal import Principal
from app.auth.services import AuthRuntime, AuthServices, build_services
from app.config import Settings
from app.db.engine import Database, Session
from app.services.activity import ActivityRecorder
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_USERNAME,
    STANDARD_ID,
    STANDARD_USERNAME,
    FakeClock,
    provision,
)
from tests.notification_fixtures import certificates, receivers, trusting  # noqa: F401
from tests.support import CHEAP_ARGON2, LOCKOUT_ENV


@pytest.fixture
def env(env: dict[str, str]) -> dict[str, str]:
    return env | LOCKOUT_ENV


@pytest.fixture(scope="session")
def users_template(migrated_template: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build once a migrated database holding an administrator and a standard user, no sessions."""
    path = tmp_path_factory.mktemp("users") / "dockmin.db"
    provision(migrated_template, path, with_sessions=False)
    return path


@pytest.fixture
async def db(users_template: Path, tmp_path: Path) -> AsyncIterator[Session]:
    """Open a session on a private copy of the users database."""
    path = tmp_path / "dockmin.db"
    shutil.copyfile(users_template, path)
    async with Database(path).session() as opened:
        yield opened


@pytest.fixture
async def empty_db(database: Database) -> AsyncIterator[Session]:
    """Open a session on a migrated database with no users."""
    async with database.session() as opened:
        yield opened


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def hasher() -> PasswordHasher:
    return PasswordHasher(CHEAP_ARGON2)


@pytest.fixture
def runtime(hasher: PasswordHasher, clock: FakeClock) -> AuthRuntime:
    return AuthRuntime(hasher=hasher, clock=clock)


@pytest.fixture
def services(db: Session, settings: Settings, runtime: AuthRuntime) -> AuthServices:
    """Wire the services over the users database."""
    return build_services(db, settings, runtime, ActivityRecorder(db, runtime.clock))


@pytest.fixture
def empty_services(empty_db: Session, settings: Settings, runtime: AuthRuntime) -> AuthServices:
    """Wire the services over the database with no users."""
    return build_services(empty_db, settings, runtime, ActivityRecorder(empty_db, runtime.clock))


@pytest.fixture
def administrator() -> Principal:
    return Principal(ADMIN_ID, ADMIN_USERNAME, "administrator")


@pytest.fixture
def standard_user() -> Principal:
    return Principal(STANDARD_ID, STANDARD_USERNAME, "standard")
