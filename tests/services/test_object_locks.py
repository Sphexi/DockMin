"""The object lock registry and the stream slot counters: pure, in-process."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.activity import UserActor
from app.services.object_locks import (
    MAX_STANDARD_STREAMS,
    MAX_STREAMS_PER_PROCESS,
    MAX_STREAMS_PER_USER,
    MAX_TERMINALS_PER_PROCESS,
    MAX_TERMINALS_PER_USER,
    LockHolder,
    ObjectBusy,
    ObjectKey,
    ObjectLockRegistry,
    StreamSlots,
    TooManyStreams,
)
from tests.access_support import container_id

START = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
FIRST_USER, SECOND_USER = 11, 12
ENVIRONMENT = 1
OTHER_ENVIRONMENT = 2
SHORT_WAIT = 0.01
LONG_WAIT = 5.0


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def key(number: int = 1, environment_id: int = ENVIRONMENT) -> ObjectKey:
    return ObjectKey(environment_id, "container", container_id(number))


def holder(user_id: int = FIRST_USER, action: str = "stop") -> LockHolder:
    return LockHolder(UserActor(user_id), f"user-{user_id}", action)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def locks(clock: Clock) -> ObjectLockRegistry:
    return ObjectLockRegistry(clock)


@pytest.mark.parametrize("object_id", ["web", "A" * 64, "a" * 63, ""])
def test_a_key_refuses_an_id_that_is_not_the_canonical_form(object_id: str) -> None:
    with pytest.raises(ValueError, match="canonical"):
        ObjectKey(ENVIRONMENT, "container", object_id)


SWARM_LOCK_ID = "n" + "0" * 24


@pytest.mark.parametrize("lock_type", ["service", "config", "secret", "node"])
def test_a_swarm_key_takes_a_25_character_base_36_id(lock_type: str) -> None:
    assert ObjectKey(ENVIRONMENT, lock_type, SWARM_LOCK_ID).object_id == SWARM_LOCK_ID  # type: ignore[arg-type]


@pytest.mark.parametrize("lock_type", ["service", "config", "secret", "node"])
@pytest.mark.parametrize("object_id", ["web", "N" * 25, "n" * 24, "n" * 26, "n" * 64, ""])
def test_a_swarm_key_refuses_anything_else(lock_type: str, object_id: str) -> None:
    with pytest.raises(ValueError, match="canonical"):
        ObjectKey(ENVIRONMENT, lock_type, object_id)  # type: ignore[arg-type]


async def test_a_second_hold_on_the_same_object_raises_busy_naming_the_first_holder(
    locks: ObjectLockRegistry,
) -> None:
    async with locks.hold(key(), holder(FIRST_USER, "restart")):
        with pytest.raises(ObjectBusy) as busy:
            async with locks.hold(key(), holder(SECOND_USER)):
                pytest.fail("the second hold must not be granted")

    assert busy.value.holder.actor == UserActor(FIRST_USER)
    assert busy.value.holder.action == "restart"


async def test_the_holder_is_stamped_with_the_time_the_lock_was_taken(
    locks: ObjectLockRegistry, clock: Clock
) -> None:
    async with locks.hold(key(), holder()):
        clock.now = START + timedelta(seconds=90)
        current = locks.holder_of(key())

    assert current is not None and current.since == START
    assert current.age_seconds(locks.now()) == 90.0


async def test_a_release_tells_the_observer_who_held_the_object_even_when_the_body_failed(
    clock: Clock,
) -> None:
    seen: list[tuple[ObjectKey, LockHolder]] = []
    locks = ObjectLockRegistry(clock, observer=lambda noted, held: seen.append((noted, held)))

    with pytest.raises(RuntimeError):
        async with locks.hold(key(), holder(FIRST_USER, "restart")):
            raise RuntimeError("body failed")

    [(noted, held)] = seen
    assert noted == key()
    assert (held.actor, held.action) == (UserActor(FIRST_USER), "restart")


async def test_a_hold_that_was_refused_is_not_reported_to_the_observer(clock: Clock) -> None:
    seen: list[LockHolder] = []
    locks = ObjectLockRegistry(clock, observer=lambda noted, held: seen.append(held))

    async with locks.hold(key(), holder(FIRST_USER)):
        with pytest.raises(ObjectBusy):
            async with locks.hold(key(), holder(SECOND_USER)):
                pytest.fail("the second hold must not be granted")
        assert seen == []


def test_an_action_noted_without_a_hold_reaches_the_observer(clock: Clock) -> None:
    seen: list[tuple[ObjectKey, str]] = []
    locks = ObjectLockRegistry(
        clock, observer=lambda noted, held: seen.append((noted, held.action))
    )

    locks.note(key(), holder(FIRST_USER, "kill"))

    assert seen == [(key(), "kill")]


async def test_the_observer_can_be_replaced_or_removed(clock: Clock) -> None:
    first: list[str] = []
    second: list[str] = []
    locks = ObjectLockRegistry(clock, observer=lambda noted, held: first.append(held.action))
    locks.set_observer(lambda noted, held: second.append(held.action))
    async with locks.hold(key(), holder(FIRST_USER, "stop")):
        pass
    locks.set_observer(None)

    async with locks.hold(key(), holder(FIRST_USER, "start")):
        pass

    assert (first, second) == ([], ["stop"])


async def test_an_observer_that_raises_is_logged_and_does_not_break_the_release(
    clock: Clock, logged: Any
) -> None:
    def failing(noted: ObjectKey, held: LockHolder) -> None:
        raise RuntimeError("observer broke")

    locks = ObjectLockRegistry(clock, observer=failing)

    async with locks.hold(key(), holder()):
        pass

    assert len(locks) == 0
    assert [r["error"] for r in logged("lock_observer_failed")] == ["RuntimeError"]


async def test_holds_on_different_objects_and_environments_do_not_contend(
    locks: ObjectLockRegistry,
) -> None:
    async with (
        locks.hold(key(1), holder()),
        locks.hold(key(2), holder()),
        locks.hold(key(1, OTHER_ENVIRONMENT), holder()),
    ):
        assert len(locks) == 3


async def test_waiting_for_a_free_object_returns_at_once_and_claims_nothing(
    locks: ObjectLockRegistry,
) -> None:
    blocker = await locks.wait_free(key(), LONG_WAIT)

    assert blocker is None
    assert locks.holder_of(key()) is None
    assert len(locks) == 0


async def test_waiting_for_a_held_object_returns_the_holder_when_the_time_runs_out(
    locks: ObjectLockRegistry,
) -> None:
    async with locks.hold(key(), holder()):
        blocker = await locks.wait_free(key(), SHORT_WAIT)

    assert blocker is not None
    assert (blocker.label, blocker.action) == ("user-11", "stop")


async def test_waiting_for_a_held_object_returns_none_once_the_holder_releases(
    locks: ObjectLockRegistry,
) -> None:
    holding = locks.hold(key(), holder())
    await holding.__aenter__()
    waiting = asyncio.create_task(locks.wait_free(key(), LONG_WAIT))
    await asyncio.sleep(0)

    await holding.__aexit__(None, None, None)
    blocker = await waiting

    assert blocker is None
    assert locks.holder_of(key()) is None


async def test_a_waiting_hold_is_granted_once_the_holder_releases(
    locks: ObjectLockRegistry,
) -> None:
    release = asyncio.Event()
    taken = asyncio.Event()

    async def first() -> None:
        async with locks.hold(key(), holder(FIRST_USER)):
            taken.set()
            await release.wait()

    task = asyncio.create_task(first())
    await taken.wait()

    async def second() -> UserActor | None:
        async with locks.hold(key(), holder(SECOND_USER), wait=LONG_WAIT):
            current = locks.holder_of(key())
            return None if current is None else UserActor(SECOND_USER)

    waiter = asyncio.create_task(second())
    await asyncio.sleep(0)
    release.set()

    assert await waiter == UserActor(SECOND_USER)
    await task


async def test_a_hold_that_waits_past_its_limit_raises_busy_with_the_holder(
    locks: ObjectLockRegistry,
) -> None:
    async with locks.hold(key(), holder(FIRST_USER)):
        with pytest.raises(ObjectBusy) as busy:
            async with locks.hold(key(), holder(SECOND_USER), wait=SHORT_WAIT):
                pytest.fail("the second hold must not be granted")

    assert busy.value.holder.actor == UserActor(FIRST_USER)


async def test_the_entry_is_removed_when_the_body_ends(locks: ObjectLockRegistry) -> None:
    async with locks.hold(key(), holder()):
        pass

    assert (len(locks), locks.holder_of(key())) == (0, None)


async def test_the_lock_is_released_when_the_body_raises(locks: ObjectLockRegistry) -> None:
    with pytest.raises(RuntimeError):
        async with locks.hold(key(), holder()):
            raise RuntimeError

    assert len(locks) == 0


async def test_the_lock_is_released_when_the_holder_is_cancelled(
    locks: ObjectLockRegistry,
) -> None:
    taken = asyncio.Event()

    async def hold_forever() -> None:
        async with locks.hold(key(), holder()):
            taken.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold_forever())
    await taken.wait()

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(locks) == 0


async def test_a_waiter_cancelled_while_waiting_leaves_the_holders_lock_in_place(
    locks: ObjectLockRegistry,
) -> None:
    async with locks.hold(key(), holder(FIRST_USER)):

        async def wait_for_it() -> None:
            async with locks.hold(key(), holder(SECOND_USER), wait=LONG_WAIT):
                pytest.fail("the lock was not free")

        waiter = asyncio.create_task(wait_for_it())
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)

        current = locks.holder_of(key())

    assert current is not None and current.actor == UserActor(FIRST_USER)


def test_a_user_beyond_the_per_user_cap_is_refused_and_another_user_is_not() -> None:
    slots = StreamSlots()
    held = [slots.acquire(FIRST_USER) for _ in range(MAX_STREAMS_PER_USER)]

    with pytest.raises(TooManyStreams) as refused:
        slots.acquire(FIRST_USER)
    other = slots.acquire(SECOND_USER)

    assert refused.value.scope == "user"
    assert slots.open_count == len(held) + 1
    other.close()


def test_closing_a_slot_gives_it_back_once_even_if_closed_twice() -> None:
    slots = StreamSlots()
    slot = slots.acquire(FIRST_USER)

    slot.close()
    slot.close()

    assert slots.open_count == 0


def test_a_slot_that_is_dropped_without_being_closed_is_given_back() -> None:
    slots = StreamSlots()
    slots.acquire(FIRST_USER)

    assert slots.open_count == 0


def test_standard_users_share_a_smaller_pool_than_the_process_total() -> None:
    slots = StreamSlots(per_user=1000)
    held = [slots.acquire(number) for number in range(MAX_STANDARD_STREAMS)]

    with pytest.raises(TooManyStreams) as refused:
        slots.acquire(MAX_STANDARD_STREAMS + 1)

    assert refused.value.scope == "process"
    assert len(held) == MAX_STANDARD_STREAMS


def test_an_administrator_can_still_claim_once_standard_users_have_filled_their_pool() -> None:
    slots = StreamSlots(per_user=1000)
    standard = [slots.acquire(number) for number in range(MAX_STANDARD_STREAMS)]

    administrator = slots.acquire(SECOND_USER, is_administrator=True)

    assert slots.open_count == len(standard) + 1
    administrator.close()


def test_nobody_can_claim_beyond_the_process_total() -> None:
    slots = StreamSlots(per_user=1000, standard_total=1000)
    held = [slots.acquire(number) for number in range(MAX_STREAMS_PER_PROCESS)]

    with pytest.raises(TooManyStreams) as refused:
        slots.acquire(MAX_STREAMS_PER_PROCESS + 1, is_administrator=True)

    assert refused.value.scope == "process"
    assert len(held) == MAX_STREAMS_PER_PROCESS


def test_giving_slots_back_reopens_the_standard_pool() -> None:
    slots = StreamSlots(per_user=1000)
    held = [slots.acquire(number) for number in range(MAX_STANDARD_STREAMS)]
    held[0].close()

    again = slots.acquire(MAX_STANDARD_STREAMS + 1)

    assert slots.open_count == MAX_STANDARD_STREAMS
    again.close()


def test_views_and_terminals_are_counted_separately_for_a_user() -> None:
    slots = StreamSlots()
    views = [slots.acquire(FIRST_USER) for _ in range(MAX_STREAMS_PER_USER)]
    terminals = [slots.acquire(FIRST_USER, kind="terminal") for _ in range(MAX_TERMINALS_PER_USER)]

    assert slots.open_count == len(views) + len(terminals)
    with pytest.raises(TooManyStreams) as view_refused:
        slots.acquire(FIRST_USER)
    with pytest.raises(TooManyStreams) as terminal_refused:
        slots.acquire(FIRST_USER, kind="terminal")

    assert (view_refused.value.scope, view_refused.value.kind) == ("user", "view")
    assert (terminal_refused.value.scope, terminal_refused.value.kind) == ("user", "terminal")


@pytest.mark.parametrize(
    ("scope", "kind", "sentence"),
    [
        (
            "user",
            "view",
            "You have 4 live views open (stats, logs or downloads). Close one, then try again.",
        ),
        ("user", "terminal", "You have 2 terminals open. Close one, then try again."),
        (
            "process",
            "view",
            "This server has reached its limit of open live views. Try again shortly.",
        ),
        (
            "process",
            "terminal",
            "This server has reached its limit of open terminals. Try again shortly.",
        ),
    ],
)
def test_each_refusal_explains_which_limit_was_reached(
    scope: str, kind: str, sentence: str
) -> None:
    assert str(TooManyStreams(scope, kind)) == sentence  # type: ignore[arg-type]


def test_the_terminal_total_applies_to_administrators_while_views_stay_available() -> None:
    slots = StreamSlots(terminals_per_user=1000)
    held = [slots.acquire(number, kind="terminal") for number in range(MAX_TERMINALS_PER_PROCESS)]

    with pytest.raises(TooManyStreams) as refused:
        slots.acquire(SECOND_USER, is_administrator=True, kind="terminal")
    view = slots.acquire(SECOND_USER, is_administrator=True)

    assert (refused.value.scope, refused.value.kind) == ("process", "terminal")
    assert slots.open_count == len(held) + 1
    view.close()


def test_terminals_count_towards_the_standard_pool_and_the_process_total() -> None:
    slots = StreamSlots(per_user=1000, terminals_per_user=1000, terminals_total=1000)
    held = [slots.acquire(number, kind="terminal") for number in range(MAX_STANDARD_STREAMS)]

    with pytest.raises(TooManyStreams) as standard_refused:
        slots.acquire(MAX_STANDARD_STREAMS + 1)
    administrator = slots.acquire(SECOND_USER, is_administrator=True, kind="terminal")

    assert standard_refused.value.scope == "process"
    assert slots.open_count == len(held) + 1
    administrator.close()


def test_closing_a_terminal_reopens_the_terminal_cap_and_leaves_views_alone() -> None:
    slots = StreamSlots()
    view = slots.acquire(FIRST_USER)
    first = slots.acquire(FIRST_USER, kind="terminal")
    second = slots.acquire(FIRST_USER, kind="terminal")
    first.close()

    again = slots.acquire(FIRST_USER, kind="terminal")

    assert slots.open_count == 3
    view.close()
    second.close()
    again.close()


def test_checking_claims_nothing_and_refuses_when_a_claim_would_be() -> None:
    slots = StreamSlots()
    held = [slots.acquire(FIRST_USER, kind="terminal") for _ in range(MAX_TERMINALS_PER_USER)]

    with pytest.raises(TooManyStreams):
        slots.check(FIRST_USER, kind="terminal")
    slots.check(SECOND_USER, kind="terminal")

    assert slots.open_count == len(held)
