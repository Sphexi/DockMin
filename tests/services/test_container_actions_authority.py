"""A container restart made on behalf of an automation rule instead of a user."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from app.auth.errors import PermissionDenied
from app.db.repositories.activity_event import DetailValue
from app.docker_engine.models import ContainerSummary
from app.scheduler.model import RunSkipped
from app.scheduler.suppression import SuppressionRegistry
from app.services.access import ResourceKey
from app.services.activity import UserActor
from app.services.container_actions import EngineRefused
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.subsystems.base import MAX_FALLBACK_CHECKS, OwnerGate
from tests.access_support import (
    ADMIN,
    ENV_A,
    STANDARD,
    container_id,
    control,
    stack_key,
)
from tests.automation_support import HEALTH, RULE_NAME, authority_context, event_rows
from tests.container_support import STACK, STACK_LABEL, WEB, stock
from tests.docker_engine.container_engine import ContainerEngine
from tests.services.conftest import ContainerRig

WEB_KEY = ObjectKey(ENV_A, "container", WEB)
STACK_KEY = ObjectKey(ENV_A, "stack", STACK)
SHORT_WAIT = 0.05
LONG_WAIT = 2.0
NOTE: dict[str, DetailValue] = {
    "trigger": "unhealthy",
    "failing_streak": 3,
    "restarts_in_window": 1,
}


def container_key_of(identifier: str) -> ResourceKey:
    return ResourceKey(ENV_A, "container", identifier)


def user_holder(action: str = "stop") -> LockHolder:
    return LockHolder(UserActor.of(ADMIN), ADMIN.username, action)


@pytest.fixture
def stocked(rig: ContainerRig, host: ContainerEngine) -> ContainerRig:
    stock(host)
    return rig


async def test_a_restart_for_a_rule_checks_the_owner_takes_the_lock_and_records_the_rule(
    stocked: ContainerRig,
) -> None:
    held: list[tuple[ObjectKey, LockHolder]] = []
    stocked.locks.set_observer(lambda key, holder: held.append((key, holder)))
    context = await authority_context(
        stocked.db, stocked.runtime.clock, stocked.locks, environment_id=ENV_A
    )

    outcome = await stocked.services.actions.restart(context.authority(), ENV_A, WEB, note=NOTE)

    assert outcome.result == "done"
    [(key, holder)] = held
    assert key == WEB_KEY
    assert (holder.label, holder.action) == (HEALTH, "restart")
    [row] = await event_rows(stocked.db, "container_restarted")
    assert (row["result"], row["actor_type"], row["actor_subsystem"]) == (
        "success",
        "subsystem",
        HEALTH,
    )
    assert (row["rule_id"], row["job_run_id"]) == (
        context.actor.rule_id,
        context.actor.job_run_id,
    )
    assert row["actor_user_id"] is None
    assert row["detail"] == {
        "name": "web",
        "rule_id": context.actor.rule_id,
        "rule_name": RULE_NAME,
        **NOTE,
    }


async def test_an_owner_who_may_not_change_the_container_acts_on_nothing_and_leaves_no_row(
    stocked: ContainerRig,
) -> None:
    context = await authority_context(
        stocked.db,
        stocked.runtime.clock,
        stocked.locks,
        environment_id=ENV_A,
        allow=lambda _key: False,
    )

    with pytest.raises(PermissionDenied):
        await stocked.services.actions.restart(context.authority(), ENV_A, WEB)

    assert await event_rows(stocked.db, "container_restarted") == []
    assert stocked.host.mutations() == []


async def test_a_refusal_by_the_host_leaves_a_failure_row_carrying_the_rule_name(
    stocked: ContainerRig,
) -> None:
    stocked.host.fail("restart", 500)
    context = await authority_context(
        stocked.db, stocked.runtime.clock, stocked.locks, environment_id=ENV_A
    )

    with pytest.raises(EngineRefused):
        await stocked.services.actions.restart(context.authority(), ENV_A, WEB)

    [row] = await event_rows(stocked.db, "container_restarted")
    assert (row["result"], row["actor_subsystem"], row["rule_id"]) == (
        "failure",
        HEALTH,
        context.actor.rule_id,
    )
    assert row["detail"]["rule_name"] == RULE_NAME
    assert row["detail"]["reason"] == "engine_error"


async def test_a_busy_or_recently_acted_on_container_writes_no_row_and_frees_its_lock(
    stocked: ContainerRig,
) -> None:
    clock = stocked.runtime.clock
    suppressions = SuppressionRegistry(clock)
    context = await authority_context(
        stocked.db,
        clock,
        stocked.locks,
        environment_id=ENV_A,
        lock_wait=SHORT_WAIT,
        suppressions=suppressions,
    )

    async with stocked.locks.hold(WEB_KEY, user_holder()):
        with pytest.raises(ObjectBusy):
            await stocked.services.actions.restart(context.authority(), ENV_A, WEB)
    suppressions.note(WEB_KEY)
    with pytest.raises(RunSkipped) as skipped:
        await stocked.services.actions.restart(context.authority(), ENV_A, WEB)

    assert skipped.value.reason == "suppressed"
    assert await event_rows(stocked.db, "container_restarted") == []
    assert stocked.locks.holder_of(WEB_KEY) is None
    assert stocked.host.mutations() == []


async def test_a_container_whose_stack_is_busy_waits_for_the_stack_and_then_restarts(
    stocked: ContainerRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    waited: list[tuple[ObjectKey, float]] = []
    real_wait = stocked.locks.wait_free

    @asynccontextmanager
    async def user_operation() -> AsyncIterator[None]:
        async with stocked.locks.hold(STACK_KEY, user_holder("redeploy")):
            yield

    operation = user_operation()
    await operation.__aenter__()

    async def operation_ends_while_the_run_waits(
        key: ObjectKey,
        timeout: float,  # noqa: ASYNC109
    ) -> LockHolder | None:
        waited.append((key, timeout))
        await operation.__aexit__(None, None, None)
        return await real_wait(key, timeout)

    monkeypatch.setattr(stocked.locks, "wait_free", operation_ends_while_the_run_waits)
    context = await authority_context(
        stocked.db,
        stocked.runtime.clock,
        stocked.locks,
        environment_id=ENV_A,
        lock_wait=LONG_WAIT,
    )

    outcome = await stocked.services.actions.restart(context.authority(), ENV_A, WEB)

    assert outcome.result == "done"
    assert waited == [(STACK_KEY, LONG_WAIT)]
    assert [r["result"] for r in await event_rows(stocked.db, "container_restarted")] == ["success"]


async def test_a_container_whose_stack_stays_busy_past_the_wait_is_refused_without_a_row(
    stocked: ContainerRig,
) -> None:
    context = await authority_context(
        stocked.db,
        stocked.runtime.clock,
        stocked.locks,
        environment_id=ENV_A,
        lock_wait=SHORT_WAIT,
    )

    async with stocked.locks.hold(STACK_KEY, user_holder("redeploy")):
        with pytest.raises(ObjectBusy) as busy:
            await stocked.services.actions.restart(context.authority(), ENV_A, WEB)

    assert busy.value.holder.action == "redeploy"
    assert await event_rows(stocked.db, "container_restarted") == []
    assert stocked.host.mutations() == []


async def test_a_stack_operation_that_starts_after_the_container_lock_is_taken_stops_the_restart(
    stocked: ContainerRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_hold = stocked.locks.hold

    @asynccontextmanager
    async def stack_operation_begins(
        key: ObjectKey, holder: LockHolder, *, wait: float = 0.0
    ) -> AsyncIterator[None]:
        async with real_hold(key, holder, wait=wait):
            if key.object_type != "container":
                yield
                return
            async with real_hold(STACK_KEY, user_holder("redeploy")):
                yield

    monkeypatch.setattr(stocked.locks, "hold", stack_operation_begins)
    context = await authority_context(
        stocked.db, stocked.runtime.clock, stocked.locks, environment_id=ENV_A
    )

    with pytest.raises(ObjectBusy) as busy:
        await stocked.services.actions.restart(context.authority(), ENV_A, WEB)

    assert busy.value.holder.action == "redeploy"
    assert await event_rows(stocked.db, "container_restarted") == []
    assert stocked.host.mutations() == []


async def test_cancelling_a_restart_for_a_rule_records_an_interrupted_row_with_the_rule(
    stocked: ContainerRig,
) -> None:
    gate = stocked.host.hold("restart")
    context = await authority_context(
        stocked.db, stocked.runtime.clock, stocked.locks, environment_id=ENV_A
    )
    request = asyncio.create_task(
        stocked.services.actions.restart(
            context.authority(), ENV_A, WEB, note={"trigger": "unhealthy"}
        )
    )
    await gate.entered.wait()

    request.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await request

    [row] = await event_rows(stocked.db, "container_restarted")
    assert (row["result"], row["actor_type"], row["actor_subsystem"]) == (
        "success",
        "subsystem",
        HEALTH,
    )
    assert (row["rule_id"], row["job_run_id"]) == (
        context.actor.rule_id,
        context.actor.job_run_id,
    )
    assert row["detail"] == {
        "name": "web",
        "trigger": "unhealthy",
        "interrupted": True,
        "rule_id": context.actor.rule_id,
        "rule_name": RULE_NAME,
    }
    assert len(stocked.locks) == 0


CUT_ROWS = MAX_FALLBACK_CHECKS + 5


async def standard_owner_gate(rig: ContainerRig) -> OwnerGate:
    async def current() -> bool:
        return True

    return OwnerGate(STANDARD, rig.access.authoriser, current)


def listed(host: ContainerEngine, *, cut: bool = False) -> list[ContainerSummary]:
    return [
        ContainerSummary.model_validate({**box.summary(), "LabelsTruncated": cut})
        for box in host.boxes.values()
    ]


async def test_a_batch_standing_equals_the_per_object_standing_for_every_kind_of_control(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    ids = [container_id(number) for number in range(1, 7)]
    plain, granted_write, granted_read, admins_only, inheriting, stranger = ids
    for number, identifier in enumerate(ids):
        host.add(
            identifier,
            f"box{number}",
            labels={STACK_LABEL: STACK} if identifier == inheriting else {},
        )
    await control(
        rig.access, container_key_of(granted_write), level="write", users=(STANDARD.user_id,)
    )
    await control(
        rig.access, container_key_of(granted_read), level="read", users=(STANDARD.user_id,)
    )
    await control(rig.access, container_key_of(admins_only), administrators_only=True)
    await control(rig.access, stack_key(STACK), level="write", users=(STANDARD.user_id,))
    await control(rig.access, container_key_of(stranger), level="write")
    gate = await standard_owner_gate(rig)
    rows = listed(host)

    found = await gate.standings(ENV_A, rows, after=None)

    expected = {row.id: await gate.standing(ObjectKey(ENV_A, "container", row.id)) for row in rows}
    assert found.by_id == expected
    assert {"writable", "readable", "hidden"} <= set(expected.values())
    assert found.not_checked == ()
    assert plain in found.by_id


async def test_rows_with_cut_labels_are_checked_up_to_the_cap_and_the_rest_wait_for_the_next_run(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    for number in range(CUT_ROWS):
        host.add(container_id(number + 1), f"box{number}")
    gate = await standard_owner_gate(rig)
    rows = listed(host, cut=True)

    first = await gate.standings(ENV_A, rows, after=None)
    second = await gate.standings(ENV_A, rows, after=first.last_checked)

    assert (len(first.by_id), len(first.not_checked), first.fallbacks) == (
        MAX_FALLBACK_CHECKS,
        CUT_ROWS - MAX_FALLBACK_CHECKS,
        MAX_FALLBACK_CHECKS,
    )
    assert set(second.by_id) >= set(first.not_checked)
    assert set(first.by_id) | set(second.by_id) == {row.id for row in rows}
