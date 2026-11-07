"""Tests for the operator command that lifts a sign-in lock from inside the container."""

import asyncio
import shutil
from pathlib import Path

import pytest

from app.__main__ import OPERATOR_SOURCE, main
from app.config import Settings
from app.db.engine import Database
from app.db.repositories.login_throttle import LoginThrottleRepository, ThrottleKind
from app.services.activity import CLEAR_LOCKOUT_ACTION
from tests.auth_support import CLIENT_ADDRESS, lock_out, read_events

USERNAME = "operator"
USAGE_ERROR = 2
NOTHING_LOCKED = "no lock was held on that subject"


@pytest.fixture
def operator_env(
    env: dict[str, str],
    settings: Settings,
    migrated_template: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Database:
    """Point the process environment at a migrated database and return a handle on it."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    shutil.copyfile(migrated_template, settings.db_path)
    return Database(settings.db_path)


async def _holds_lock(database: Database, kind: ThrottleKind, subject: str) -> bool:
    async with database.session() as db:
        row = await LoginThrottleRepository(db).get(kind, subject)
    return row is not None


async def _events(database: Database) -> list[dict[str, object]]:
    async with database.session() as db:
        return list(await read_events(db))


def test_clearing_a_username_lifts_its_lock_and_records_who_did_it(
    operator_env: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(lock_out(operator_env, "username", USERNAME))

    status = main(["clear-lockout", USERNAME])

    [event] = asyncio.run(_events(operator_env))
    assert status == 0
    assert asyncio.run(_holds_lock(operator_env, "username", USERNAME)) is False
    assert "lifted 1 lock(s)" in capsys.readouterr().out
    assert (event["action"], event["actor_type"], event["actor_user_id"]) == (
        CLEAR_LOCKOUT_ACTION,
        "anonymous",
        None,
    )
    assert event["detail"] == {"source": OPERATOR_SOURCE, "subject": USERNAME}


def test_clearing_an_address_lifts_its_lock(operator_env: Database) -> None:
    asyncio.run(lock_out(operator_env, "address", CLIENT_ADDRESS))

    status = main(["clear-lockout", CLIENT_ADDRESS])

    assert status == 0
    assert asyncio.run(_holds_lock(operator_env, "address", CLIENT_ADDRESS)) is False


def test_clearing_a_subject_that_holds_no_lock_says_so_and_still_leaves_a_record(
    operator_env: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main(["clear-lockout", USERNAME])

    assert status == 0
    assert NOTHING_LOCKED in capsys.readouterr().out
    assert len(asyncio.run(_events(operator_env))) == 1


def test_the_command_refuses_when_there_is_no_database(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    status = main(["clear-lockout", USERNAME])

    assert status == USAGE_ERROR
    assert "no database" in capsys.readouterr().err
