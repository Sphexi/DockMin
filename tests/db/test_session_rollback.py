"""Tests for the savepoint and shielded rollback paths of app/db/engine.py."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.db.engine import Database, DatabaseError, Session

INSERT_SETTING = (
    "INSERT INTO setting (key, value_type, value, updated_at) VALUES (?, 'string', 'v', 't')"
)
LIST_KEYS = "SELECT key FROM setting ORDER BY key"
KEPT_KEY = "kept"
UNDONE_KEY = "undone"
NEXT_KEY = "next"
LIMIT = 10
FINISH_TIMEOUT_SECONDS = 1.0
WRITER_TIMEOUT_MS = 100


class RollbackGate:
    """Holds the first statement run after arm() until released, then reports it finished."""

    def __init__(self, real_run: Callable[..., Awaitable[Any]]) -> None:
        self._real_run = real_run
        self._armed = False
        self.held = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    def arm(self) -> None:
        """Hold the next statement the session runs."""
        self._armed = True

    async def run(self, work: Callable[..., Any]) -> Any:
        """Stand in for Session._run."""
        if not self._armed:
            return await self._real_run(work)
        self._armed = False
        self.held.set()
        await self.release.wait()
        result = await self._real_run(work)
        self.finished.set()
        return result


async def _keys(session: Session) -> list[str]:
    rows = await session.fetch_limited(LIST_KEYS, (), limit=LIMIT)
    return [row["key"] for row in rows]


async def test_savepoint_outside_a_transaction_is_refused(session: Session) -> None:
    with pytest.raises(DatabaseError, match="open transaction"):
        async with session.savepoint():
            pass


async def test_failed_savepoint_undoes_only_its_own_writes(session: Session) -> None:
    async with session.transaction():
        await session.execute(INSERT_SETTING, (KEPT_KEY,))
        with pytest.raises(RuntimeError):
            async with session.savepoint():
                await session.execute(INSERT_SETTING, (UNDONE_KEY,))
                raise RuntimeError

    assert await _keys(session) == [KEPT_KEY]


async def test_transaction_cancelled_twice_still_rolls_back_and_leaves_the_session_usable(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = RollbackGate(session._run)
    monkeypatch.setattr(session, "_run", gate.run)
    parked = asyncio.Event()

    async def write_then_wait() -> None:
        async with session.transaction():
            await session.execute(INSERT_SETTING, (UNDONE_KEY,))
            parked.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(write_then_wait())
    await parked.wait()
    gate.arm()
    task.cancel()
    await gate.held.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.release.set()
    await asyncio.wait_for(gate.finished.wait(), timeout=FINISH_TIMEOUT_SECONDS)

    assert not session.in_transaction
    async with session.transaction():
        await session.execute(INSERT_SETTING, (NEXT_KEY,))
    assert await _keys(session) == [NEXT_KEY]


async def test_a_transaction_cancelled_while_its_begin_is_queued_does_not_keep_the_write_lock(
    database: Database, session: Session
) -> None:
    async def hold_the_block_open() -> None:
        async with session.transaction():
            await asyncio.sleep(1)

    session._lock.acquire()
    task = asyncio.ensure_future(hold_the_block_open())
    await asyncio.sleep(0.05)
    task.cancel()
    session._lock.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)

    other = database.connect()
    other.execute(f"PRAGMA busy_timeout = {WRITER_TIMEOUT_MS}")
    try:
        other.execute(INSERT_SETTING, (KEPT_KEY,))
        other.commit()
    finally:
        other.close()
