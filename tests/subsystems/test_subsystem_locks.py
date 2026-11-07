"""Object locks and the suppression window as a subsystem run sees them."""

import asyncio
from datetime import datetime, timedelta

import pytest

from app.scheduler.model import RunResult
from app.services.activity import SubsystemActor, UserActor
from app.services.object_locks import LockHolder, ObjectKey, ObjectLockRegistry
from app.subsystems.base import RunContext, Subsystem, SubsystemDeps
from tests.scheduler.support import REFRESHER, Rig, at, dispatch_of, scripted_subsystem, stamp

CONTAINER = ObjectKey(1, "container", "a" * 64)
USER_HOLDER = LockHolder(UserActor(9), "alice", "stop")
SUBSYSTEM_HOLDER = LockHolder(SubsystemActor(REFRESHER, None), REFRESHER, "restart")
A_FEW_SECONDS = 2.0
FIVE_MINUTES = 300


async def holds_the_container(self: Subsystem, context: RunContext) -> RunResult:
    async with context.hold(CONTAINER, "restart"):
        return RunResult("success")


async def run_once(rig: Rig, deps: SubsystemDeps, body: object, **attributes: object) -> RunResult:
    job = await rig.refresher_job(next_run=at(60))
    subsystem = scripted_subsystem(body, name=REFRESHER, **attributes)(deps)  # type: ignore[arg-type]
    return await dispatch_of(rig, subsystem, job)


async def test_a_run_blocked_by_a_users_lock_is_skipped_naming_the_holder(
    rig: Rig, deps: SubsystemDeps, locks: ObjectLockRegistry
) -> None:
    async with locks.hold(CONTAINER, USER_HOLDER):
        ran = await run_once(rig, deps, holds_the_container, lock_wait=0.0)

    assert (ran.outcome, ran.reason) == ("skipped", "object_busy")
    assert dict(ran.detail) == {
        "holder_type": "user",
        "holder_label": "alice",
        "holder_action": "stop",
        "holder_age_seconds": 0,
    }


async def test_a_run_blocked_by_another_subsystems_lock_reports_a_subsystem_holder(
    rig: Rig, deps: SubsystemDeps, locks: ObjectLockRegistry
) -> None:
    async with locks.hold(CONTAINER, SUBSYSTEM_HOLDER):
        ran = await run_once(rig, deps, holds_the_container, lock_wait=0.0)

    assert dict(ran.detail)["holder_type"] == "subsystem"


async def test_a_free_object_is_held_by_the_subsystem_under_its_own_name(
    rig: Rig, deps: SubsystemDeps, locks: ObjectLockRegistry
) -> None:
    seen: list[LockHolder | None] = []

    async def inspects(self: Subsystem, context: RunContext) -> RunResult:
        async with context.hold(CONTAINER, "restart"):
            seen.append(locks.holder_of(CONTAINER))
        return RunResult("success")

    ran = await run_once(rig, deps, inspects)

    [holder] = seen
    assert ran.outcome == "success"
    assert holder is not None and isinstance(holder.actor, SubsystemActor)
    assert (holder.label, holder.action) == (REFRESHER, "restart")


async def test_a_run_waits_for_a_users_lock_to_clear_within_its_wait_when_suppression_is_off(
    rig: Rig, deps: SubsystemDeps, locks: ObjectLockRegistry
) -> None:
    release = asyncio.Event()
    waiting = asyncio.Event()

    async def user_action() -> None:
        async with locks.hold(CONTAINER, USER_HOLDER):
            waiting.set()
            await release.wait()

    async def waits_for_the_lock(self: Subsystem, context: RunContext) -> RunResult:
        release.set()
        return await holds_the_container(self, context)

    holder = asyncio.create_task(user_action())
    await waiting.wait()

    ran = await run_once(
        rig,
        deps,
        waits_for_the_lock,
        lock_wait=A_FEW_SECONDS,
        suppression_window=timedelta(0),
    )
    await holder

    assert ran.outcome == "success"


async def test_a_run_that_waited_behind_a_users_action_is_skipped_as_suppressed(
    rig: Rig, deps: SubsystemDeps, locks: ObjectLockRegistry
) -> None:
    release = asyncio.Event()
    waiting = asyncio.Event()
    acted: list[str] = []

    async def user_action() -> None:
        async with locks.hold(CONTAINER, USER_HOLDER):
            waiting.set()
            await release.wait()

    async def acts_once_it_holds_the_lock(self: Subsystem, context: RunContext) -> RunResult:
        release.set()
        async with context.hold(CONTAINER, "restart"):
            acted.append("restart")
        return RunResult("success")

    holder = asyncio.create_task(user_action())
    await waiting.wait()

    ran = await run_once(rig, deps, acts_once_it_holds_the_lock, lock_wait=A_FEW_SECONDS)
    await holder

    assert (ran.outcome, ran.reason) == ("skipped", "suppressed")
    assert dict(ran.detail) == {"acted_at": stamp(rig.clock())}
    assert acted == []
    assert locks.holder_of(CONTAINER) is None


async def test_holding_a_lock_inside_a_transaction_is_refused(
    rig: Rig, deps: SubsystemDeps
) -> None:
    async def in_transaction(self: Subsystem, context: RunContext) -> RunResult:
        async with context.db.transaction(), context.hold(CONTAINER, "restart"):
            pass
        return RunResult("success")

    with pytest.raises(RuntimeError, match="transaction"):
        await run_once(rig, deps, in_transaction)


async def user_acted_at(rig: Rig, deps: SubsystemDeps, acted: datetime | None) -> datetime | None:
    seen: list[datetime | None] = []

    async def asks(self: Subsystem, context: RunContext) -> RunResult:
        seen.append(await context.suppressed(CONTAINER))
        return RunResult("success")

    if acted is not None:
        deps.suppressions.note(CONTAINER, acted)
    await run_once(rig, deps, asks)
    return seen[0]


async def test_a_user_action_inside_the_default_window_suppresses(
    rig: Rig, deps: SubsystemDeps
) -> None:
    acted = rig.clock() - timedelta(seconds=30)

    assert await user_acted_at(rig, deps, acted) == acted


async def test_a_user_action_beyond_the_default_window_does_not_suppress(
    rig: Rig, deps: SubsystemDeps
) -> None:
    acted = rig.clock() - timedelta(seconds=FIVE_MINUTES + 1)

    assert await user_acted_at(rig, deps, acted) is None


async def test_an_object_nobody_touched_is_not_suppressed(rig: Rig, deps: SubsystemDeps) -> None:
    assert await user_acted_at(rig, deps, None) is None


async def test_a_rules_own_window_replaces_the_default(rig: Rig, deps: SubsystemDeps) -> None:
    seen: list[datetime | None] = []

    async def asks(self: Subsystem, context: RunContext) -> RunResult:
        seen.append(await context.suppressed(CONTAINER))
        return RunResult("success")

    rule = await rig.rule("short", parameters='{"suppression_seconds": 10}')
    job = await rig.job(rule_id=rule, next_run=at(60))
    deps.suppressions.note(CONTAINER, rig.clock() - timedelta(seconds=30))

    await dispatch_of(rig, scripted_subsystem(asks)(deps), job)

    assert seen == [None]


async def test_a_users_lock_release_suppresses_the_next_run_on_that_object(
    rig: Rig, deps: SubsystemDeps, locks: ObjectLockRegistry
) -> None:
    async with locks.hold(CONTAINER, USER_HOLDER):
        pass

    assert await user_acted_at(rig, deps, None) == rig.clock()
