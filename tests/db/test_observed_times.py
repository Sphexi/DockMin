"""The check times a refresh holds in memory instead of writing, and how readers prefer them."""

import pytest

from app.db.engine import Database
from app.db.repositories.environment_snapshot import (
    ObservedTimeCache,
    ObservedTimes,
    observed_times,
)

ENVIRONMENT_ID = 1
OTHER_ENVIRONMENT_ID = 2
STORED = "2026-01-01T12:00:00Z"
NEWER = "2026-01-01T12:05:00Z"
OLDER = "2026-01-01T11:55:00Z"
HELD = ObservedTimes(checked_at=NEWER, contacted_at=NEWER, captured_at=NEWER)


@pytest.mark.parametrize(
    ("stored", "held", "expected"),
    [
        pytest.param(STORED, NEWER, NEWER, id="held-is-newer"),
        pytest.param(STORED, OLDER, STORED, id="held-is-older"),
        pytest.param(STORED, STORED, STORED, id="equal"),
        pytest.param(None, NEWER, None, id="nothing-stored"),
    ],
)
def test_a_reader_gets_the_later_of_the_stored_time_and_the_held_one(
    stored: str | None, held: str, expected: str | None
) -> None:
    cache = ObservedTimeCache()
    cache.hold(ENVIRONMENT_ID, ObservedTimes(checked_at=held, contacted_at=held, captured_at=held))

    assert cache.checked_at(ENVIRONMENT_ID, stored) == expected
    assert cache.contacted_at(ENVIRONMENT_ID, stored) == expected
    assert cache.captured_at(ENVIRONMENT_ID, stored) == expected


def test_a_host_never_contacted_keeps_no_contact_time_whatever_was_checked() -> None:
    cache = ObservedTimeCache()
    cache.hold(
        ENVIRONMENT_ID, ObservedTimes(checked_at=NEWER, contacted_at=None, captured_at=NEWER)
    )

    assert cache.contacted_at(ENVIRONMENT_ID, STORED) == STORED


def test_holding_again_replaces_the_times_held() -> None:
    cache = ObservedTimeCache()
    cache.hold(ENVIRONMENT_ID, HELD)
    cache.hold(
        ENVIRONMENT_ID, ObservedTimes(checked_at=OLDER, contacted_at=OLDER, captured_at=OLDER)
    )

    assert cache.checked_at(ENVIRONMENT_ID, STORED) == STORED


def test_forgetting_an_environment_returns_its_readers_to_the_stored_times() -> None:
    cache = ObservedTimeCache()
    cache.hold(ENVIRONMENT_ID, HELD)
    cache.hold(OTHER_ENVIRONMENT_ID, HELD)

    cache.forget(ENVIRONMENT_ID)

    assert cache.checked_at(ENVIRONMENT_ID, STORED) == STORED
    assert cache.checked_at(OTHER_ENVIRONMENT_ID, STORED) == NEWER


def test_forgetting_an_environment_with_nothing_held_is_harmless() -> None:
    ObservedTimeCache().forget(ENVIRONMENT_ID)


async def test_every_session_of_one_database_reads_the_same_held_times(
    database: Database,
) -> None:
    async with database.session() as writer, database.session() as reader:
        observed_times(writer).hold(ENVIRONMENT_ID, HELD)

        seen = observed_times(reader).checked_at(ENVIRONMENT_ID, STORED)

    assert seen == NEWER
