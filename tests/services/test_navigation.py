"""The sidebar's environments, and the environment a session remembers between pages."""

import logging
import sqlite3
from datetime import timedelta

import pytest

from app.auth.principal import Principal
from app.auth.sessions import SessionService, hash_token
from app.db.engine import Session
from app.db.repositories.user import UserRepository
from app.db.repositories.user_session import UserSessionRepository
from app.services.navigation import (
    NAVIGATION_LIMIT,
    Navigation,
    load_navigation,
    remember_navigation,
)
from tests.access_support import ENV_A, ENV_B, STAMP
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_USERNAME,
    STANDARD_ID,
    STANDARD_USERNAME,
    FakeClock,
)
from tests.db.helpers import UNKNOWN_ID

LIFETIME = timedelta(hours=12)
IDLE = timedelta(hours=1)
WATCH_WRITES = (
    "CREATE TABLE nav_writes (value INTEGER)",
    "CREATE TRIGGER nav_watch AFTER UPDATE OF nav_environment_id ON session "
    "BEGIN INSERT INTO nav_writes (value) VALUES (NEW.nav_environment_id); END",
)
LOCKED = sqlite3.OperationalError("database is locked")
REJECTED = sqlite3.IntegrityError("FOREIGN KEY constraint failed")


async def open_session(db: Session, clock: FakeClock, user_id: int) -> int:
    row = await UserSessionRepository(db, clock).create(
        user_id,
        hash_token(f"token-{user_id}"),
        lifetime=LIFETIME,
        user_agent=None,
        remote_address=None,
    )
    return row.id


async def stored(db: Session, session_id: int) -> int | None:
    row = await db.fetch_one("SELECT nav_environment_id FROM session WHERE id = ?", (session_id,))
    assert row is not None
    value: int | None = row[0]
    return value


async def set_stored(db: Session, session_id: int, environment_id: int | None) -> None:
    await db.execute(
        "UPDATE session SET nav_environment_id = ? WHERE id = ?", (environment_id, session_id)
    )


async def watch_writes(db: Session) -> None:
    for statement in WATCH_WRITES:
        await db.execute(statement)


async def writes(db: Session) -> list[int | None]:
    rows = await db.fetch_limited("SELECT value FROM nav_writes ORDER BY rowid", (), limit=100)
    return [row[0] for row in rows]


def standard(session_id: int | None, remembered: int | None = None) -> Principal:
    return Principal(STANDARD_ID, STANDARD_USERNAME, "standard", session_id, None, remembered)


def administrator(session_id: int | None, remembered: int | None = None) -> Principal:
    return Principal(ADMIN_ID, ADMIN_USERNAME, "administrator", session_id, None, remembered)


async def test_remember_environment_writes_a_changed_id_once_and_clears_it_with_none(
    db: Session, clock: FakeClock
) -> None:
    session_id = await open_session(db, clock, STANDARD_ID)
    sessions = UserSessionRepository(db, clock)
    await watch_writes(db)

    await sessions.remember_environment(session_id, ENV_A)
    await sessions.remember_environment(session_id, ENV_A)
    after_repeat = await stored(db, session_id)
    await sessions.remember_environment(session_id, None)
    await sessions.remember_environment(session_id, None)

    assert after_repeat == ENV_A
    assert await stored(db, session_id) is None
    assert await writes(db) == [ENV_A, None]


async def test_remember_environment_ignores_an_environment_that_no_longer_exists(
    db: Session, clock: FakeClock
) -> None:
    session_id = await open_session(db, clock, STANDARD_ID)
    sessions = UserSessionRepository(db, clock)
    await sessions.remember_environment(session_id, ENV_A)
    await watch_writes(db)

    await sessions.remember_environment(session_id, UNKNOWN_ID)

    assert await stored(db, session_id) == ENV_A
    assert await writes(db) == []


async def test_a_resolved_principal_carries_the_environment_its_session_remembers(
    db: Session, clock: FakeClock
) -> None:
    service = SessionService(
        UserSessionRepository(db, clock),
        UserRepository(db, clock),
        lifetime=LIFETIME,
        idle_timeout=IDLE,
    )
    issued = await service.issue(STANDARD_ID, user_agent=None, remote_address=None)
    fresh = await service.resolve(issued.token)
    await UserSessionRepository(db, clock).remember_environment(issued.session_id, ENV_B)

    remembering = await service.resolve(issued.token)

    assert fresh is not None and fresh.remembered_environment_id is None
    assert remembering is not None and remembering.remembered_environment_id == ENV_B


async def test_the_current_environment_takes_precedence_over_the_remembered_one(
    db: Session,
) -> None:
    navigation = await load_navigation(db, standard(None), ENV_B, ENV_A)

    assert navigation.current is not None and navigation.current.id == ENV_B
    assert navigation.remembered is False


async def test_a_remembered_environment_stands_in_when_no_page_names_one(db: Session) -> None:
    navigation = await load_navigation(db, standard(None), None, ENV_A)

    assert navigation.current is not None and navigation.current.id == ENV_A
    assert navigation.remembered is True


@pytest.mark.parametrize("remembered_id", [None, UNKNOWN_ID], ids=["none", "deleted"])
async def test_nothing_stands_in_when_no_environment_is_remembered_or_it_is_gone(
    db: Session, remembered_id: int | None
) -> None:
    navigation = await load_navigation(db, administrator(None), None, remembered_id)

    assert navigation.current is None
    assert navigation.remembered is False
    assert len(navigation.environments) == 2


@pytest.mark.usefixtures("no_grants")
async def test_a_standard_user_without_access_gets_no_block_for_a_remembered_environment(
    db: Session,
) -> None:
    navigation = await load_navigation(db, standard(None), None, ENV_A)

    assert navigation == Navigation(environments=(), current=None, remembered=False)


async def test_a_remembered_environment_beyond_the_listing_limit_is_still_found(
    db: Session,
) -> None:
    await db.execute(
        "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < ?) "
        "INSERT INTO environment (name, type, connection_kind, socket_path, created_at, "
        "updated_at) SELECT printf('z-%03d', n), 'standalone', 'unix_socket', "
        "printf('/run/z-%03d.sock', n), ?, ? FROM seq",
        (NAVIGATION_LIMIT, STAMP, STAMP),
    )
    last = await db.fetch_one("SELECT id FROM environment WHERE name = 'z-100'")
    assert last is not None

    navigation = await load_navigation(db, administrator(None), None, last[0])

    assert len(navigation.environments) == NAVIGATION_LIMIT
    assert navigation.current is not None and navigation.current.name == "z-100"
    assert navigation.remembered is True


@pytest.mark.parametrize(
    ("before", "current_id", "expected", "expected_writes"),
    [
        pytest.param(None, ENV_B, ENV_B, [ENV_B], id="first-view-stores-it"),
        pytest.param(ENV_A, ENV_B, ENV_B, [ENV_B], id="another-environment-replaces-it"),
        pytest.param(ENV_B, ENV_B, ENV_B, [], id="the-same-environment-writes-nothing"),
        pytest.param(ENV_A, None, ENV_A, [], id="a-page-without-one-keeps-it"),
    ],
)
async def test_remember_navigation_stores_the_environment_a_page_showed_only_when_it_changed(
    db: Session,
    clock: FakeClock,
    before: int | None,
    current_id: int | None,
    expected: int | None,
    expected_writes: list[int],
) -> None:
    session_id = await open_session(db, clock, STANDARD_ID)
    await set_stored(db, session_id, before)
    await watch_writes(db)
    actor = standard(session_id, before)
    navigation = await load_navigation(db, actor, current_id, before)

    await remember_navigation(db, actor, current_id, before, navigation)

    assert await stored(db, session_id) == expected
    assert await writes(db) == expected_writes


@pytest.mark.usefixtures("no_grants")
async def test_remember_navigation_clears_a_remembered_environment_the_user_can_no_longer_see(
    db: Session, clock: FakeClock
) -> None:
    session_id = await open_session(db, clock, STANDARD_ID)
    await set_stored(db, session_id, ENV_A)
    actor = standard(session_id, ENV_A)
    navigation = await load_navigation(db, actor, None, ENV_A)

    await remember_navigation(db, actor, None, ENV_A, navigation)

    assert await stored(db, session_id) is None


@pytest.mark.usefixtures("no_grants")
async def test_remember_navigation_keeps_the_value_when_the_page_shown_is_not_visible(
    db: Session, clock: FakeClock
) -> None:
    session_id = await open_session(db, clock, STANDARD_ID)
    await set_stored(db, session_id, ENV_A)
    await watch_writes(db)
    actor = standard(session_id, ENV_A)
    navigation = await load_navigation(db, actor, ENV_B, ENV_A)

    await remember_navigation(db, actor, ENV_B, ENV_A, navigation)

    assert await stored(db, session_id) == ENV_A
    assert await writes(db) == []


async def test_remember_navigation_writes_nothing_for_a_principal_without_a_session(
    db: Session, clock: FakeClock
) -> None:
    await watch_writes(db)
    actor = standard(None)
    navigation = await load_navigation(db, actor, ENV_A, None)

    await remember_navigation(db, actor, ENV_A, None, navigation)

    assert await writes(db) == []


@pytest.mark.parametrize("failure", [LOCKED, REJECTED], ids=["locked", "rejected"])
async def test_remember_navigation_drops_a_database_failure_and_the_session_stays_usable(
    db: Session,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: sqlite3.Error,
) -> None:
    session_id = await open_session(db, clock, STANDARD_ID)
    actor = standard(session_id)
    navigation = await load_navigation(db, actor, ENV_A, None)
    original = UserSessionRepository.remember_environment

    async def refuse(*_: object) -> None:
        raise failure

    monkeypatch.setattr(UserSessionRepository, "remember_environment", refuse)
    with caplog.at_level(logging.DEBUG, logger="app.services.navigation"):
        await remember_navigation(db, actor, ENV_A, None, navigation)
    monkeypatch.setattr(UserSessionRepository, "remember_environment", original)
    await remember_navigation(db, actor, ENV_A, None, navigation)

    assert [record.getMessage() for record in caplog.records] == ["navigation_not_remembered"]
    assert caplog.records[0].levelno == logging.DEBUG
    assert await stored(db, session_id) == ENV_A
