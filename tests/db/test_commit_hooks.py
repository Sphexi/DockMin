"""Hooks that run just before and just after a transaction commits."""

import pytest

from app.db.engine import Session

CREATE_PROBE = "CREATE TABLE probe (value TEXT NOT NULL)"
INSERT_PROBE = "INSERT INTO probe (value) VALUES ('written')"
COUNT_PROBE = "SELECT COUNT(*) FROM probe"


class HookFailed(Exception):
    pass


async def probe_count(session: Session) -> int:
    row = await session.fetch_one(COUNT_PROBE)
    assert row is not None
    return int(row[0])


@pytest.fixture
async def probed(session: Session) -> Session:
    await session.execute(CREATE_PROBE)
    return session


async def test_a_before_commit_hook_that_raises_rolls_the_writes_back_and_propagates(
    probed: Session,
) -> None:
    async def refuse() -> None:
        raise HookFailed

    probed.before_commit(refuse)

    with pytest.raises(HookFailed):
        async with probed.transaction():
            await probed.execute(INSERT_PROBE)

    assert await probe_count(probed) == 0
    assert not probed.in_transaction


async def test_a_before_commit_hook_runs_inside_the_transaction_it_precedes(
    probed: Session,
) -> None:
    async def write() -> None:
        await probed.execute(INSERT_PROBE)

    probed.before_commit(write)

    async with probed.transaction():
        pass

    assert await probe_count(probed) == 1


async def test_a_once_hook_runs_for_the_first_commit_only(probed: Session) -> None:
    calls: list[str] = []

    async def record() -> None:
        calls.append("ran")

    probed.before_commit(record, once=True)

    async with probed.transaction():
        pass
    async with probed.transaction():
        pass

    assert calls == ["ran"]


async def test_a_once_hook_is_still_registered_after_a_transaction_that_rolled_back(
    probed: Session,
) -> None:
    calls: list[str] = []

    async def record() -> None:
        calls.append("ran")

    probed.before_commit(record, once=True)
    with pytest.raises(HookFailed):
        async with probed.transaction():
            raise HookFailed

    async with probed.transaction():
        pass

    assert calls == ["ran"]


async def test_a_removed_hook_does_not_run(probed: Session) -> None:
    calls: list[str] = []

    async def record() -> None:
        calls.append("ran")

    probed.before_commit(record)()

    async with probed.transaction():
        pass

    assert calls == []


async def test_an_after_commit_callback_runs_after_a_commit_and_not_after_a_rollback(
    probed: Session,
) -> None:
    calls: list[str] = []
    probed.after_commit(lambda: calls.append("committed"))
    with pytest.raises(HookFailed):
        async with probed.transaction():
            raise HookFailed

    async with probed.transaction():
        pass

    assert calls == ["committed"]


async def test_a_standalone_transaction_leaves_the_hooks_and_callbacks_registered_before_it(
    probed: Session,
) -> None:
    calls: list[str] = []

    async def record() -> None:
        calls.append("hook")

    probed.before_commit(record, once=True)
    probed.after_commit(lambda: calls.append("callback"))

    async with probed.transaction(standalone=True):
        probed.after_commit(lambda: calls.append("inner"))
    assert calls == ["inner"]
    async with probed.transaction():
        pass

    assert calls == ["inner", "hook", "callback"]
