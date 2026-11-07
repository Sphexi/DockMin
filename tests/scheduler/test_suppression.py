"""The suppression registry: what it remembers, for how long, and what limits do to it."""

from datetime import timedelta

import pytest

from app.scheduler.suppression import MAX_SUPPRESSION, MAX_SUPPRESSION_KEYS, SuppressionRegistry
from app.services.activity import SubsystemActor, UserActor
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from tests.scheduler.support import START, Clock, Rig, at, stamp

CONTAINER = ObjectKey(1, "container", "a" * 64)
OTHER_CONTAINER = ObjectKey(1, "container", "b" * 64)
USER_HOLDER = LockHolder(UserActor(9), "alice", "stop")
SUBSYSTEM_HOLDER = LockHolder(SubsystemActor("stack-restarter", 1), "stack-restarter", "restart")
OVERFLOW = 50


def registry_with_observer() -> tuple[SuppressionRegistry, ObjectLockRegistry, Clock]:
    clock = Clock()
    suppressions = SuppressionRegistry(clock)
    locks = ObjectLockRegistry(clock)
    locks.set_observer(suppressions.observe)
    return suppressions, locks, clock


async def test_a_users_lock_release_is_recorded_at_the_release_time() -> None:
    suppressions, locks, clock = registry_with_observer()

    async with locks.hold(CONTAINER, USER_HOLDER):
        clock.jump(5)

    assert suppressions.last_user_action(CONTAINER) == clock()


async def test_a_subsystems_lock_release_is_not_recorded() -> None:
    suppressions, locks, _ = registry_with_observer()

    async with locks.hold(CONTAINER, SUBSYSTEM_HOLDER):
        pass

    assert suppressions.last_user_action(CONTAINER) is None


async def test_a_refused_user_action_that_never_held_the_lock_is_not_recorded() -> None:
    suppressions, locks, _ = registry_with_observer()

    async with locks.hold(CONTAINER, SUBSYSTEM_HOLDER):
        with pytest.raises(ObjectBusy):
            async with locks.hold(CONTAINER, USER_HOLDER):
                pass

    assert suppressions.last_user_action(CONTAINER) is None


def test_a_noted_action_without_a_lock_is_recorded_for_a_user_holder() -> None:
    suppressions, locks, clock = registry_with_observer()

    locks.note(CONTAINER, LockHolder(UserActor(1), "root", "kill"))

    assert suppressions.last_user_action(CONTAINER) == clock()


def test_an_entry_older_than_the_longest_window_is_forgotten() -> None:
    suppressions, locks, clock = registry_with_observer()
    locks.note(CONTAINER, USER_HOLDER)

    clock.jump(MAX_SUPPRESSION.total_seconds() + 1)

    assert suppressions.last_user_action(CONTAINER) is None


def test_a_note_never_replaces_a_newer_time_with_an_older_one() -> None:
    suppressions, _, clock = registry_with_observer()
    suppressions.note(CONTAINER)

    suppressions.note(CONTAINER, clock() - timedelta(seconds=100))

    assert suppressions.last_user_action(CONTAINER) == clock()


def test_a_note_with_a_time_records_that_time() -> None:
    suppressions, _, clock = registry_with_observer()
    earlier = clock() - timedelta(seconds=30)

    suppressions.note(CONTAINER, earlier)

    assert suppressions.last_user_action(CONTAINER) == earlier


def test_memory_is_bounded_and_the_oldest_entries_go_first() -> None:
    suppressions, _, _ = registry_with_observer()

    for number in range(MAX_SUPPRESSION_KEYS + OVERFLOW):
        suppressions.note(ObjectKey(1, "volume", f"v{number}"))

    assert len(suppressions) == MAX_SUPPRESSION_KEYS
    assert suppressions.last_user_action(ObjectKey(1, "volume", "v0")) is None
    assert (
        suppressions.last_user_action(
            ObjectKey(1, "volume", f"v{MAX_SUPPRESSION_KEYS + OVERFLOW - 1}")
        )
        is not None
    )


def insert_user_action(
    rig: Rig,
    *,
    user: int,
    environment: int | None,
    target_type: str,
    target_id: str,
    seconds: float,
) -> None:
    rig.sql(
        "INSERT INTO activity_event (category, actor_type, actor_user_id, environment_id, "
        "target_type, target_id, action, result, occurred_at) "
        "VALUES ('user_action', 'user', ?, ?, ?, ?, 'a', 'success', ?)",
        (user, environment, target_type, target_id, stamp(at(seconds))),
    )


async def test_startup_seeds_canonical_lock_typed_rows_from_the_last_hour(rig: Rig) -> None:
    user = rig.user("seeder", "standard")
    environment = rig.environment()
    rows = [
        ("container", "c" * 64, -30),
        ("container", "short", -30),
        ("stack", "WebApp", -10),
        ("volume", "data", -20),
        ("container", "d" * 64, -4000),
        ("setting", "x", -5),
        ("network", "n" * 64, -50),
    ]
    for target_type, target_id, seconds in rows:
        insert_user_action(
            rig,
            user=user,
            environment=environment,
            target_type=target_type,
            target_id=target_id,
            seconds=seconds,
        )
    suppressions = SuppressionRegistry(Clock(at(0)))

    loaded = await suppressions.seed(rig.database)

    assert loaded == 3
    assert suppressions.last_user_action(ObjectKey(environment, "container", "c" * 64)) == at(-30)
    assert suppressions.last_user_action(ObjectKey(environment, "stack", "webapp")) == at(-10)
    assert suppressions.last_user_action(ObjectKey(environment, "container", "d" * 64)) is None
    assert len(suppressions) == 3


async def test_seeding_skips_a_row_with_no_environment_or_no_target_id(rig: Rig) -> None:
    user = rig.user("seeder", "standard")
    insert_user_action(
        rig, user=user, environment=None, target_type="container", target_id="c" * 64, seconds=-5
    )
    rig.sql(
        "INSERT INTO activity_event (category, actor_type, actor_user_id, environment_id, "
        "target_type, action, result, occurred_at) "
        "VALUES ('user_action', 'user', ?, ?, 'container', 'a', 'success', ?)",
        (user, rig.environment(), stamp(at(-5))),
    )
    suppressions = SuppressionRegistry(Clock(at(0)))

    loaded = await suppressions.seed(rig.database)

    assert loaded == 0


async def test_a_miss_after_an_eviction_is_answered_from_the_activity_log(rig: Rig) -> None:
    user = rig.user("u", "standard")
    environment = rig.environment()
    victim = ObjectKey(environment, "container", "a" * 64)
    insert_user_action(
        rig,
        user=user,
        environment=environment,
        target_type="container",
        target_id=victim.object_id,
        seconds=-30,
    )
    clock = Clock(at(0))
    suppressions = SuppressionRegistry(clock)
    suppressions.note(victim, at(-30))
    for number in range(MAX_SUPPRESSION_KEYS):
        suppressions.note(ObjectKey(environment, "volume", f"v{number}"))

    async with rig.database.session() as db:
        found = await suppressions.lookup(victim, db)
        absent = await suppressions.lookup(ObjectKey(environment, "container", "b" * 64), db)

    assert suppressions.last_user_action(victim) is None
    assert found == at(-30)
    assert absent is None


async def test_a_miss_with_nothing_evicted_never_reads_the_log(rig: Rig) -> None:
    user = rig.user("u", "standard")
    environment = rig.environment()
    object_id = "a" * 64
    insert_user_action(
        rig,
        user=user,
        environment=environment,
        target_type="container",
        target_id=object_id,
        seconds=-30,
    )
    suppressions = SuppressionRegistry(Clock(at(0)))

    async with rig.database.session() as db:
        found = await suppressions.lookup(ObjectKey(environment, "container", object_id), db)

    assert found is None


async def test_the_log_fallback_ends_once_the_evicted_entry_could_no_longer_matter(
    rig: Rig,
) -> None:
    user = rig.user("u", "standard")
    environment = rig.environment()
    victim = ObjectKey(environment, "container", "a" * 64)
    insert_user_action(
        rig,
        user=user,
        environment=environment,
        target_type="container",
        target_id=victim.object_id,
        seconds=-30,
    )
    clock = Clock(START)
    suppressions = SuppressionRegistry(clock)
    for number in range(MAX_SUPPRESSION_KEYS + 1):
        suppressions.note(ObjectKey(environment, "volume", f"v{number}"))
    clock.jump(MAX_SUPPRESSION.total_seconds() + 1)

    async with rig.database.session() as db:
        found = await suppressions.lookup(victim, db)

    assert found is None


async def test_a_stack_miss_after_an_eviction_matches_the_log_with_case_folded(rig: Rig) -> None:
    user = rig.user("u", "standard")
    environment = rig.environment()
    insert_user_action(
        rig,
        user=user,
        environment=environment,
        target_type="stack",
        target_id="WebApp",
        seconds=-10,
    )
    suppressions = SuppressionRegistry(Clock(at(0)))
    for number in range(MAX_SUPPRESSION_KEYS + 1):
        suppressions.note(ObjectKey(environment, "volume", f"v{number}"))

    async with rig.database.session() as db:
        found = await suppressions.lookup(ObjectKey(environment, "stack", "webapp"), db)

    assert found == at(-10)
