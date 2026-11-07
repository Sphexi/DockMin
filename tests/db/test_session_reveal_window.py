"""Tests for the reveal window of app/db/repositories/user_session.py."""

from datetime import timedelta

import pytest

from app.db.engine import Session
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.user import UserRepository
from app.db.repositories.user_session import (
    REVEAL_LIMIT,
    REVEAL_WINDOW,
    RevealWindow,
    UserSessionRepository,
)
from tests.db.helpers import CREATED_AT, PASSWORD_HASH, UNKNOWN_ID, at

LIFETIME = timedelta(hours=12)
SECOND = timedelta(seconds=1)
USED_BEFORE_REOPEN = 5


@pytest.fixture
def sessions(session: Session) -> UserSessionRepository:
    return UserSessionRepository(session, at(CREATED_AT))


async def _open_session(
    session: Session, sessions: UserSessionRepository, name: str = "admin"
) -> int:
    user = await UserRepository(session, at(CREATED_AT)).create(
        name, PASSWORD_HASH, "administrator"
    )
    created = await sessions.create(
        user.id, f"hash-{name}", lifetime=LIFETIME, user_agent=None, remote_address=None
    )
    return created.id


async def _spend(sessions: UserSessionRepository, session_id: int, times: int) -> list[bool]:
    return [await sessions.take_reveal(session_id, CREATED_AT) is not None for _ in range(times)]


async def test_a_session_without_a_window_cannot_spend_a_reveal(
    session: Session, sessions: UserSessionRepository
) -> None:
    session_id = await _open_session(session, sessions)

    assert await sessions.take_reveal(session_id, CREATED_AT) is None
    assert await sessions.reveal_window(session_id, CREATED_AT) is None


async def test_opening_a_window_reports_whether_the_session_exists(
    session: Session, sessions: UserSessionRepository
) -> None:
    session_id = await _open_session(session, sessions)

    assert await sessions.open_reveal_window(session_id, CREATED_AT + REVEAL_WINDOW) is True
    assert await sessions.open_reveal_window(UNKNOWN_ID, CREATED_AT + REVEAL_WINDOW) is False


async def test_a_reveal_reports_the_window_end_and_the_reveals_left_and_reopening_resets_them(
    session: Session, sessions: UserSessionRepository
) -> None:
    session_id = await _open_session(session, sessions)
    ends = CREATED_AT + REVEAL_WINDOW
    await sessions.open_reveal_window(session_id, ends)

    first = await sessions.take_reveal(session_id, CREATED_AT)
    await _spend(sessions, session_id, USED_BEFORE_REOPEN - 1)
    peeked = await sessions.reveal_window(session_id, CREATED_AT)
    await sessions.open_reveal_window(session_id, ends)
    reopened = await sessions.reveal_window(session_id, CREATED_AT)

    assert first == RevealWindow(format_timestamp(ends), REVEAL_LIMIT - 1)
    assert peeked == RevealWindow(format_timestamp(ends), REVEAL_LIMIT - USED_BEFORE_REOPEN)
    assert reopened == RevealWindow(format_timestamp(ends), REVEAL_LIMIT)


async def test_the_window_closes_at_the_twentieth_reveal_and_the_twenty_first_call_is_refused(
    session: Session, sessions: UserSessionRepository
) -> None:
    session_id = await _open_session(session, sessions)
    await sessions.open_reveal_window(session_id, CREATED_AT + REVEAL_WINDOW)

    results = await _spend(sessions, session_id, REVEAL_LIMIT + 1)
    stored = await session.fetch_one("SELECT reveal_count FROM session WHERE id = ?", (session_id,))

    assert results == [True] * REVEAL_LIMIT + [False]
    assert stored is not None and stored["reveal_count"] == REVEAL_LIMIT
    assert await sessions.reveal_window(session_id, CREATED_AT) is None


async def test_the_window_is_open_until_its_end_and_closed_from_that_moment(
    session: Session, sessions: UserSessionRepository
) -> None:
    session_id = await _open_session(session, sessions)
    ends = CREATED_AT + REVEAL_WINDOW
    await sessions.open_reveal_window(session_id, ends)

    just_before = await sessions.take_reveal(session_id, ends - SECOND)
    at_the_end = await sessions.take_reveal(session_id, ends)
    after = await sessions.reveal_window(session_id, ends + SECOND)

    assert just_before is not None
    assert (at_the_end, after) == (None, None)


async def test_an_end_more_than_the_window_length_ahead_of_the_clock_reads_as_closed(
    session: Session, sessions: UserSessionRepository
) -> None:
    session_id = await _open_session(session, sessions)
    await sessions.open_reveal_window(session_id, CREATED_AT + REVEAL_WINDOW + SECOND)

    assert await sessions.reveal_window(session_id, CREATED_AT) is None
    assert await sessions.take_reveal(session_id, CREATED_AT) is None
    assert await sessions.reveal_window(session_id, CREATED_AT + SECOND) is not None


async def test_an_expired_or_revoked_session_reads_as_closed_whatever_its_window_says(
    session: Session, sessions: UserSessionRepository
) -> None:
    expiring = await _open_session(session, sessions, "expiring")
    revoked = await _open_session(session, sessions, "revoked")
    for session_id in (expiring, revoked):
        await sessions.open_reveal_window(session_id, CREATED_AT + REVEAL_WINDOW)
    await session.execute(
        "UPDATE session SET expires_at = ? WHERE id = ?", (format_timestamp(CREATED_AT), expiring)
    )
    await session.execute(
        "UPDATE session SET revoked_at = ? WHERE id = ?", (format_timestamp(CREATED_AT), revoked)
    )

    assert await sessions.reveal_window(expiring, CREATED_AT) is None
    assert await sessions.take_reveal(expiring, CREATED_AT) is None
    assert await sessions.reveal_window(revoked, CREATED_AT) is None
    assert await sessions.take_reveal(revoked, CREATED_AT) is None


async def test_a_reveal_is_spent_from_one_session_only_and_deleting_the_session_ends_its_window(
    session: Session, sessions: UserSessionRepository
) -> None:
    spent = await _open_session(session, sessions, "spent")
    untouched = await _open_session(session, sessions, "untouched")
    for session_id in (spent, untouched):
        await sessions.open_reveal_window(session_id, CREATED_AT + REVEAL_WINDOW)

    await sessions.take_reveal(spent, CREATED_AT)
    other = await sessions.reveal_window(untouched, CREATED_AT)
    await sessions.delete(spent)

    assert other is not None and other.reveals_left == REVEAL_LIMIT
    assert await sessions.take_reveal(spent, CREATED_AT) is None
    assert await sessions.open_reveal_window(spent, CREATED_AT + REVEAL_WINDOW) is False
