"""Tests for app/auth/sessions.py."""

from datetime import timedelta

import pytest

from app.auth.principal import Principal
from app.auth.sessions import SessionService, hash_token
from app.db.engine import Session
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.user import UserRepository
from app.db.repositories.user_session import UserSessionRepository
from tests.auth_support import ADMIN_ID, ADMIN_USERNAME, STANDARD_ID, FakeClock

LIFETIME = timedelta(hours=12)
IDLE = timedelta(hours=1)
SECOND = timedelta(seconds=1)
AGENT = "test-agent"
ADDRESS = "198.51.100.7"
WELL_FORMED_UNKNOWN = "u" * 43

UNUSABLE_COOKIES = [
    pytest.param(None, id="absent"),
    pytest.param("", id="empty"),
    pytest.param("short", id="too-short"),
    pytest.param("!" * 43, id="illegal-characters"),
    pytest.param(WELL_FORMED_UNKNOWN, id="unknown-value"),
]


def _service(
    db: Session, clock: FakeClock, *, lifetime: timedelta = LIFETIME, idle: timedelta = IDLE
) -> SessionService:
    return SessionService(
        UserSessionRepository(db, clock),
        UserRepository(db, clock),
        lifetime=lifetime,
        idle_timeout=idle,
    )


@pytest.fixture
def sessions(db: Session, clock: FakeClock) -> SessionService:
    return _service(db, clock)


async def _issue(sessions: SessionService, user_id: int = ADMIN_ID) -> str:
    return (await sessions.issue(user_id, user_agent=AGENT, remote_address=ADDRESS)).token


async def test_resolve_returns_the_principal_a_cookie_was_issued_for(
    sessions: SessionService,
) -> None:
    issued = await sessions.issue(ADMIN_ID, user_agent=AGENT, remote_address=ADDRESS)

    principal = await sessions.resolve(issued.token)

    assert principal == Principal(ADMIN_ID, ADMIN_USERNAME, "administrator", issued.session_id)


async def test_issue_stores_only_a_hash_of_the_cookie_value(
    db: Session, clock: FakeClock, sessions: SessionService
) -> None:
    issued = await sessions.issue(ADMIN_ID, user_agent=AGENT, remote_address=ADDRESS)

    row = await UserSessionRepository(db, clock).get(issued.session_id)

    assert row is not None
    assert row.token_hash == hash_token(issued.token)
    assert issued.token not in row.token_hash


@pytest.mark.parametrize("cookie", UNUSABLE_COOKIES)
async def test_resolve_returns_nothing_for_a_cookie_that_names_no_session(
    sessions: SessionService, cookie: str | None
) -> None:
    assert await sessions.resolve(cookie) is None


@pytest.mark.parametrize(
    ("elapsed", "usable"),
    [
        pytest.param(IDLE - SECOND, True, id="just-inside-the-idle-limit"),
        pytest.param(IDLE + SECOND, False, id="just-outside-the-idle-limit"),
    ],
)
async def test_resolve_enforces_the_idle_limit(
    sessions: SessionService, clock: FakeClock, elapsed: timedelta, usable: bool
) -> None:
    token = await _issue(sessions)
    clock.advance(elapsed)

    principal = await sessions.resolve(token)

    assert (principal is not None) is usable


async def test_activity_pushes_the_idle_limit_back(
    sessions: SessionService, clock: FakeClock
) -> None:
    token = await _issue(sessions)
    clock.advance(IDLE * 2 / 3)
    await sessions.resolve(token)
    clock.advance(IDLE * 2 / 3)

    assert await sessions.resolve(token) is not None


@pytest.mark.parametrize(
    ("elapsed", "usable"),
    [
        pytest.param(LIFETIME - SECOND, True, id="just-inside-the-lifetime"),
        pytest.param(LIFETIME + SECOND, False, id="just-outside-the-lifetime"),
    ],
)
async def test_resolve_enforces_the_absolute_lifetime_however_active_the_session_is(
    db: Session, clock: FakeClock, elapsed: timedelta, usable: bool
) -> None:
    sessions = _service(db, clock, idle=LIFETIME * 2)
    token = await _issue(sessions)
    clock.advance(elapsed)

    principal = await sessions.resolve(token)

    assert (principal is not None) is usable


async def test_resolve_stops_honouring_a_session_the_moment_its_user_is_disabled(
    db: Session, clock: FakeClock, sessions: SessionService
) -> None:
    token = await _issue(sessions, STANDARD_ID)

    await UserRepository(db, clock).disable(STANDARD_ID)

    assert await sessions.resolve(token) is None


async def test_end_makes_the_cookie_unusable_at_once(sessions: SessionService) -> None:
    issued = await sessions.issue(ADMIN_ID, user_agent=AGENT, remote_address=ADDRESS)

    await sessions.end(issued.session_id)

    assert await sessions.resolve(issued.token) is None
    assert await sessions.count_for_user(ADMIN_ID) == 0


async def test_end_all_for_user_spares_only_the_session_named_to_keep(
    sessions: SessionService,
) -> None:
    kept = await sessions.issue(ADMIN_ID, user_agent=AGENT, remote_address=ADDRESS)
    other_token = await _issue(sessions)
    elsewhere = await _issue(sessions, STANDARD_ID)

    ended = await sessions.end_all_for_user(ADMIN_ID, keep=kept.session_id)

    assert ended == 1
    assert await sessions.resolve(kept.token) is not None
    assert await sessions.resolve(other_token) is None
    assert await sessions.resolve(elsewhere) is not None


async def test_reap_removes_sessions_that_can_no_longer_be_used(
    sessions: SessionService, clock: FakeClock
) -> None:
    await _issue(sessions)
    clock.advance(IDLE + SECOND)
    live = await _issue(sessions, STANDARD_ID)

    await sessions.reap()

    assert await sessions.count_for_user(ADMIN_ID) == 0
    assert await sessions.resolve(live) is not None


async def _revoke(db: Session, clock: FakeClock, session_id: int) -> None:
    await db.execute(
        "UPDATE session SET revoked_at = ? WHERE id = ?", (format_timestamp(clock()), session_id)
    )


async def test_resolve_refuses_a_session_marked_revoked(
    db: Session, clock: FakeClock, sessions: SessionService
) -> None:
    issued = await sessions.issue(ADMIN_ID, user_agent=AGENT, remote_address=ADDRESS)
    await _revoke(db, clock, issued.session_id)

    assert await sessions.resolve(issued.token) is None


async def test_count_for_user_leaves_out_revoked_and_idle_rows_that_are_not_yet_reaped(
    db: Session, clock: FakeClock, sessions: SessionService
) -> None:
    await _issue(sessions)
    clock.advance(IDLE + SECOND)
    await _issue(sessions)
    revoked = await sessions.issue(ADMIN_ID, user_agent=AGENT, remote_address=ADDRESS)
    await _revoke(db, clock, revoked.session_id)

    assert await sessions.count_for_user(ADMIN_ID) == 1
