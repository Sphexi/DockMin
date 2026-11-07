"""Restart holds: which containers a health-checker rule holds, listing them, and releasing one."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.automation_rule import AutomationRule, AutomationRuleRepository
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.restart_hold import RestartHoldRepository
from app.services import automation_rules
from app.services.activity import ActivityRecorder, SubsystemActor, UserActor
from app.services.automation_rules import HOLDS_SHOWN, HOLDS_UNAVAILABLE, NOT_HELD
from app.services.restart_holds import (
    EXHAUSTED_ACTION,
    HEALTH_CHECKER,
    RELEASED_ACTION,
    environment_holds,
    rule_holds,
)
from tests.access_support import ENV_A
from tests.auth_support import ADMIN_ID, STANDARD_ID, FakeClock
from tests.container_support import CACHE, DB, JOB, WEB
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.fake_engine import Refuse
from tests.job_support import ADMIN, STANDARD, events, job_services_on_hosts, last_event_id, rows
from tests.web.environment_support import World

RESTARTER = "stack-restarter"
HEALTH_INTERVAL = "60"
RESTARTER_INTERVAL = "3600"
FIRST = "a" * 64
SECOND = "b" * 64
THIRD = "c" * 64
REMOVED = "d" * 64
OTHER_REMOVED = "e" * 64
STEP = timedelta(seconds=10)
STRANGER = Principal(user_id=STANDARD_ID + 50, username="stranger", role="standard")
DB_SELECTOR = json.dumps({"kind": "container", "names": ["db"]})
EXHAUSTED_REASON = "restart_budget_exhausted"


async def make_rule(
    db: Session,
    clock: FakeClock,
    *,
    subsystem: str = HEALTH_CHECKER,
    name: str = "Heal",
    owner: int = ADMIN_ID,
    environment_id: int | None = ENV_A,
) -> AutomationRule:
    return await AutomationRuleRepository(db, clock).insert(
        name=name,
        subsystem=subsystem,
        environment_id=environment_id,
        target_selector="{}",
        parameters="{}",
        is_enabled=True,
        created_by_user_id=owner,
    )


async def exhaust(
    db: Session,
    clock: FakeClock,
    rule: AutomationRule,
    container_id: str,
    name: str = "web",
    *,
    environment_id: int = ENV_A,
) -> None:
    await ActivityRecorder(db, clock).record_failure(
        SubsystemActor(rule.subsystem, rule.id, None, rule.name),
        action=EXHAUSTED_ACTION,
        target_type="container",
        target_id=container_id,
        environment_id=environment_id,
        reason=EXHAUSTED_REASON,
        detail={"name": name},
    )


async def user_acts(
    db: Session, clock: FakeClock, container_id: str, *, succeeds: bool = True
) -> None:
    recorder = ActivityRecorder(db, clock)
    if succeeds:
        await recorder.record_event(
            UserActor(ADMIN_ID),
            action="container_restarted",
            target_type="container",
            target_id=container_id,
            environment_id=ENV_A,
            result="success",
        )
    else:
        await recorder.record_failure(
            UserActor(ADMIN_ID),
            action="container_restarted",
            target_type="container",
            target_id=container_id,
            environment_id=ENV_A,
            reason="engine_error",
        )


async def held_ids(db: Session, clock: FakeClock, rule: AutomationRule) -> list[str]:
    return [hold.container_id for hold in await rule_holds(db, clock, rule)]


async def test_a_rule_holds_the_containers_it_exhausted_with_the_time_and_name_recorded(
    db: Session, clock: FakeClock
) -> None:
    rule = await make_rule(db, clock)
    await exhaust(db, clock, rule, FIRST, "web")
    stamp = clock().strftime("%Y-%m-%dT%H:%M:%SZ")

    [hold] = await rule_holds(db, clock, rule)

    assert (hold.rule_id, hold.container_id, hold.name) == (rule.id, FIRST, "web")
    assert (hold.held_since, hold.last_unhealthy_at) == (stamp, stamp)


async def test_the_last_sighting_is_shown_when_it_is_later_than_the_hold(
    db: Session, clock: FakeClock
) -> None:
    rule = await make_rule(db, clock)
    await exhaust(db, clock, rule, FIRST)
    clock.advance(STEP)
    await RestartHoldRepository(db, clock).note_unhealthy(rule.id, ENV_A, FIRST)

    [hold] = await rule_holds(db, clock, rule)

    assert hold.last_unhealthy_at > hold.held_since


async def test_a_sighting_older_than_the_hold_is_ignored(db: Session, clock: FakeClock) -> None:
    rule = await make_rule(db, clock)
    await RestartHoldRepository(db, clock).note_unhealthy(rule.id, ENV_A, FIRST)
    clock.advance(STEP)
    await exhaust(db, clock, rule, FIRST)

    [hold] = await rule_holds(db, clock, rule)

    assert hold.last_unhealthy_at == hold.held_since


async def test_an_earlier_sighting_never_replaces_a_later_one(
    db: Session, clock: FakeClock
) -> None:
    rule = await make_rule(db, clock)
    sightings = RestartHoldRepository(db, clock)
    clock.advance(STEP)
    await sightings.note_unhealthy(rule.id, ENV_A, FIRST)
    later = (await sightings.sightings(rule.id, ENV_A, [FIRST]))[FIRST]
    clock.advance(-STEP)

    await sightings.note_unhealthy(rule.id, ENV_A, FIRST)

    assert (await sightings.sightings(rule.id, ENV_A, [FIRST]))[FIRST] == later


async def test_a_successful_user_action_after_the_hold_ends_it(
    db: Session, clock: FakeClock
) -> None:
    rule = await make_rule(db, clock)
    await exhaust(db, clock, rule, FIRST)
    await exhaust(db, clock, rule, SECOND)
    clock.advance(STEP)

    await user_acts(db, clock, FIRST)

    assert await held_ids(db, clock, rule) == [SECOND]


async def test_a_failed_user_action_after_the_hold_does_not_end_it(
    db: Session, clock: FakeClock
) -> None:
    rule = await make_rule(db, clock)
    await exhaust(db, clock, rule, FIRST)
    clock.advance(STEP)

    await user_acts(db, clock, FIRST, succeeds=False)

    assert await held_ids(db, clock, rule) == [FIRST]


async def test_a_user_action_before_the_hold_does_not_end_it(db: Session, clock: FakeClock) -> None:
    rule = await make_rule(db, clock)
    await user_acts(db, clock, FIRST)
    clock.advance(STEP)

    await exhaust(db, clock, rule, FIRST)

    assert await held_ids(db, clock, rule) == [FIRST]


async def test_a_write_to_the_rule_after_the_hold_ends_every_hold_it_made(
    db: Session, clock: FakeClock
) -> None:
    rule = await make_rule(db, clock)
    await exhaust(db, clock, rule, FIRST)
    await exhaust(db, clock, rule, SECOND)
    clock.advance(STEP)

    await AutomationRuleRepository(db, clock).set_enabled(rule.id, True)

    changed = await AutomationRuleRepository(db, clock).get(rule.id)
    assert changed is not None
    assert await held_ids(db, clock, changed) == []


async def test_a_release_by_this_rule_ends_the_hold_and_a_release_by_another_rule_does_not(
    db: Session, clock: FakeClock
) -> None:
    rule = await make_rule(db, clock, name="Heal")
    other = await make_rule(db, clock, name="Other")
    await exhaust(db, clock, rule, FIRST)
    await exhaust(db, clock, rule, SECOND)
    clock.advance(STEP)
    recorder = ActivityRecorder(db, clock)
    for owner, container_id in ((rule, FIRST), (other, SECOND)):
        await recorder.record_event(
            SubsystemActor(HEALTH_CHECKER, owner.id, None, owner.name),
            action=RELEASED_ACTION,
            target_type="container",
            target_id=container_id,
            environment_id=ENV_A,
            result="success",
        )

    assert await held_ids(db, clock, rule) == [SECOND]


async def test_a_rule_holds_nothing_for_rows_of_another_rule_or_another_environment(
    db: Session, clock: FakeClock
) -> None:
    rule = await make_rule(db, clock, name="Heal")
    other = await make_rule(db, clock, name="Other")
    await exhaust(db, clock, other, FIRST)

    assert await held_ids(db, clock, rule) == []
    assert await rule_holds(db, clock, rule) == []


async def test_a_rule_of_another_subsystem_or_with_no_environment_holds_nothing(
    db: Session, clock: FakeClock
) -> None:
    restarter = await make_rule(db, clock, subsystem=RESTARTER, name="Restart")
    unscoped = await make_rule(db, clock, name="Unscoped", environment_id=None)
    await exhaust(db, clock, restarter, FIRST)

    assert await rule_holds(db, clock, restarter) == []
    assert await rule_holds(db, clock, unscoped) == []


async def test_the_listing_is_cut_to_the_newest_holds_before_anything_else_reads_them(
    db: Session, clock: FakeClock
) -> None:
    rule = await make_rule(db, clock)
    for container_id in (FIRST, SECOND, THIRD):
        await exhaust(db, clock, rule, container_id)
        clock.advance(STEP)

    holds = await rule_holds(db, clock, rule, limit=2)

    assert [hold.container_id for hold in holds] == [THIRD, SECOND]


async def test_an_environments_holds_come_from_the_rules_of_the_owner_asked_for(
    db: Session, clock: FakeClock
) -> None:
    mine = await make_rule(db, clock, name="Mine", owner=STANDARD_ID)
    theirs = await make_rule(db, clock, name="Theirs", owner=ADMIN_ID)
    await exhaust(db, clock, mine, FIRST)
    await exhaust(db, clock, theirs, SECOND)

    owned = await environment_holds(db, clock, ENV_A, owner_id=STANDARD_ID)
    everyone = await environment_holds(db, clock, ENV_A, owner_id=None)

    assert [(hold.rule_id, hold.container_id) for hold in owned] == [(mine.id, FIRST)]
    assert {(hold.rule_id, hold.container_id) for hold in everyone} == {
        (mine.id, FIRST),
        (theirs.id, SECOND),
    }


@pytest.fixture
def ticking(started_app: FastAPI) -> Iterator[FakeClock]:
    """Run the application's services on a clock that moves only when told to."""
    runtime = started_app.state.auth
    original = runtime.clock
    runtime.clock = FakeClock(datetime.now(UTC).replace(microsecond=0))
    yield runtime.clock
    runtime.clock = original


def arguments(world: World, **changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "subsystem": HEALTH_CHECKER,
        "name": "Heal",
        "environment_id": world.remote_id,
        "target_selector": DB_SELECTOR,
        "parameters": {},
        "schedule_kind": "interval",
        "schedule_expression": "60",
    }
    return base | changes


async def hold_on(
    app: FastAPI,
    clock: FakeClock,
    world: World,
    rule: AutomationRule,
    container_id: str,
    name: str = "db",
) -> None:
    async with app.state.database.session() as db:
        await exhaust(db, clock, rule, container_id, name, environment_id=world.remote_id)


async def released_rows(app: FastAPI, since: int = 0) -> list[dict[str, Any]]:
    return [row for row in await events(app, since=since) if row["action"] == RELEASED_ACTION]


async def test_the_holds_of_a_rule_list_name_and_times_newest_first(
    started_app: FastAPI, world: World, shared_host: ContainerEngine, ticking: FakeClock
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(ADMIN, **arguments(world))
        ticking.advance(STEP)
        await hold_on(started_app, ticking, world, rule, DB, "db")
        ticking.advance(STEP)
        await hold_on(started_app, ticking, world, rule, WEB, "web")
        ticking.advance(STEP)

        listed = await services.rules.holds(ADMIN, rule.id)

    assert [(held.container_id, held.name) for held in listed.containers] == [
        (WEB, "web"),
        (DB, "db"),
    ]
    assert listed.containers[0].held_since > listed.containers[1].held_since
    assert listed.containers[0].last_unhealthy_at == listed.containers[0].held_since
    assert (listed.more, listed.note) == (0, None)


async def test_a_standard_owner_is_shown_only_the_held_containers_they_may_read(
    started_app: FastAPI, world: World, shared_host: ContainerEngine, ticking: FakeClock
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(STANDARD, **arguments(world))
        ticking.advance(STEP)
        for container_id, name in ((DB, "db"), (JOB, "job"), (WEB, "web"), (CACHE, "cache")):
            await hold_on(started_app, ticking, world, rule, container_id, name)
            ticking.advance(STEP)

        listed = await services.rules.holds(STANDARD, rule.id)

    assert {held.container_id for held in listed.containers} == {DB, JOB}


async def test_only_the_newest_fifty_holds_are_checked_and_listed_and_the_rest_are_counted(
    started_app: FastAPI, world: World, shared_host: ContainerEngine, ticking: FakeClock
) -> None:
    total = HOLDS_SHOWN + 2
    for number in range(total):
        shared_host.add(f"{number:064x}", f"held-{number}")
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(ADMIN, **arguments(world))
        ticking.advance(STEP)
        for number in range(total):
            await hold_on(started_app, ticking, world, rule, f"{number:064x}", f"held-{number}")
            ticking.advance(timedelta(seconds=1))

        listed = await services.rules.holds(ADMIN, rule.id)

    newest = [f"held-{number}" for number in range(total - 1, total - 1 - HOLDS_SHOWN, -1)]
    assert [held.name for held in listed.containers] == newest
    assert listed.more == total - HOLDS_SHOWN


@pytest.mark.parametrize("owner", [ADMIN, STANDARD], ids=["administrator", "standard"])
async def test_a_hold_on_a_container_the_host_no_longer_has_is_not_listed(
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
    owner: Principal,
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(owner, **arguments(world))
        ticking.advance(STEP)
        await hold_on(started_app, ticking, world, rule, REMOVED)
        ticking.advance(STEP)
        await hold_on(started_app, ticking, world, rule, DB, "db")
        ticking.advance(STEP)

        listed = await services.rules.holds(owner, rule.id)

    assert [held.container_id for held in listed.containers] == [DB]
    assert (listed.more, listed.note) == (0, None)


@pytest.mark.parametrize(
    ("owner", "listed_first", "more"),
    [
        pytest.param(ADMIN, WEB, 2, id="administrator"),
        pytest.param(STANDARD, JOB, 1, id="standard"),
    ],
)
async def test_the_count_of_further_holds_includes_only_containers_the_viewer_can_see_on_the_host(
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    owner: Principal,
    listed_first: str,
    more: int,
) -> None:
    monkeypatch.setattr(automation_rules, "HOLDS_SHOWN", 1)
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(owner, **arguments(world))
        ticking.advance(STEP)
        for container_id in (REMOVED, DB, OTHER_REMOVED, JOB, WEB):
            await hold_on(started_app, ticking, world, rule, container_id)
            ticking.advance(STEP)

        listed = await services.rules.holds(owner, rule.id)

    assert [held.container_id for held in listed.containers] == [listed_first]
    assert listed.more == more


async def test_an_owner_who_lost_the_environment_sees_the_rule_with_no_holds(
    started_app: FastAPI, world: World, shared_host: ContainerEngine, ticking: FakeClock
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(STANDARD, **arguments(world))
        ticking.advance(STEP)
        await hold_on(started_app, ticking, world, rule, DB)
    async with started_app.state.database.session() as db:
        await EnvironmentAccessRepository(db).remove_user(
            "environment", world.remote_id, STANDARD_ID
        )

    async with job_services_on_hosts(started_app) as services:
        listed = await services.rules.holds(STANDARD, rule.id)

    assert (listed.containers, listed.more, listed.note) == ((), 0, None)


async def test_a_host_that_cannot_be_read_leaves_a_note_in_place_of_the_list(
    started_app: FastAPI, world: World, shared_host: ContainerEngine, ticking: FakeClock
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(STANDARD, **arguments(world))
        ticking.advance(STEP)
        await hold_on(started_app, ticking, world, rule, DB)
    shared_host.route("GET", "/containers/json", Refuse())
    shared_host.route("GET", f"/containers/{DB}/json", Refuse())

    async with job_services_on_hosts(started_app) as services:
        listed = await services.rules.holds(STANDARD, rule.id)

    assert (listed.containers, listed.note) == ((), HOLDS_UNAVAILABLE)


async def test_the_holds_of_a_rule_are_not_shown_to_a_user_who_does_not_own_it(
    started_app: FastAPI, world: World, shared_host: ContainerEngine, ticking: FakeClock
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(ADMIN, **arguments(world))

        with pytest.raises(ResourceNotFound):
            await services.rules.holds(STRANGER, rule.id)


async def test_a_manual_release_is_a_user_action_naming_the_rule_and_ends_the_hold(
    started_app: FastAPI, world: World, shared_host: ContainerEngine, ticking: FakeClock
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(ADMIN, **arguments(world, name="Heal db"))
        ticking.advance(STEP)
        await hold_on(started_app, ticking, world, rule, DB)
        ticking.advance(STEP)
        mark = await last_event_id(started_app)

        await services.rules.release_hold(ADMIN, rule.id, DB)
        remaining = await services.rules.holds(ADMIN, rule.id)

    [row] = await released_rows(started_app, mark)
    assert (row["actor_type"], row["actor_user_id"], row["result"]) == (
        "user",
        ADMIN.user_id,
        "success",
    )
    assert (row["target_type"], row["target_id"], row["environment_id"]) == (
        "container",
        DB,
        world.remote_id,
    )
    assert json.loads(row["detail"]) == {
        "rule_id": rule.id,
        "rule_name": "Heal db",
        "name": "db",
        "released": "manual",
    }
    assert remaining.containers == ()


async def test_the_owner_of_a_rule_may_release_a_container_they_may_change(
    started_app: FastAPI, world: World, shared_host: ContainerEngine, ticking: FakeClock
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(STANDARD, **arguments(world))
        ticking.advance(STEP)
        await hold_on(started_app, ticking, world, rule, DB)
        ticking.advance(STEP)

        await services.rules.release_hold(STANDARD, rule.id, DB)

        assert (await services.rules.holds(STANDARD, rule.id)).containers == ()


async def test_an_administrator_may_release_a_hold_of_a_rule_another_user_owns(
    started_app: FastAPI, world: World, shared_host: ContainerEngine, ticking: FakeClock
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(STANDARD, **arguments(world))
        ticking.advance(STEP)
        await hold_on(started_app, ticking, world, rule, DB)
        ticking.advance(STEP)

        await services.rules.release_hold(ADMIN, rule.id, DB)

        assert (await services.rules.holds(STANDARD, rule.id)).containers == ()


async def test_a_user_who_does_not_own_the_rule_cannot_release_and_nothing_is_recorded(
    started_app: FastAPI, world: World, shared_host: ContainerEngine, ticking: FakeClock
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(ADMIN, **arguments(world))
        ticking.advance(STEP)
        await hold_on(started_app, ticking, world, rule, DB)
        ticking.advance(STEP)
        mark = await last_event_id(started_app)

        with pytest.raises(ResourceNotFound):
            await services.rules.release_hold(STRANGER, rule.id, DB)

        assert [
            held.container_id for held in (await services.rules.holds(ADMIN, rule.id)).containers
        ] == [DB]
    assert await events(started_app, since=mark) == []


@pytest.mark.parametrize(
    ("container_id", "refusal"),
    [(JOB, PermissionDenied), (WEB, ResourceNotFound), (CACHE, ResourceNotFound)],
    ids=["read-only", "administrators-only", "not-granted"],
)
async def test_a_container_the_owner_cannot_change_is_refused_with_no_row_and_the_hold_stands(
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
    container_id: str,
    refusal: type[Exception],
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(STANDARD, **arguments(world))
        ticking.advance(STEP)
        await hold_on(started_app, ticking, world, rule, container_id)
        ticking.advance(STEP)
        mark = await last_event_id(started_app)

        with pytest.raises(refusal):
            await services.rules.release_hold(STANDARD, rule.id, container_id)

    assert await events(started_app, since=mark) == []
    async with job_services_on_hosts(started_app) as services:
        still = await services.rules.holds(ADMIN, rule.id)
    assert [held.container_id for held in still.containers] == [container_id]


async def test_a_release_of_a_container_the_rule_does_not_hold_is_refused_and_leaves_a_failure_row(
    started_app: FastAPI, world: World, shared_host: ContainerEngine, ticking: FakeClock
) -> None:
    async with job_services_on_hosts(started_app) as services:
        holder = await services.rules.create(ADMIN, **arguments(world, name="Holder"))
        bystander = await services.rules.create(ADMIN, **arguments(world, name="Bystander"))
        ticking.advance(STEP)
        await hold_on(started_app, ticking, world, holder, DB)
        ticking.advance(STEP)
        mark = await last_event_id(started_app)

        with pytest.raises(ValidationFailed) as refused:
            await services.rules.release_hold(ADMIN, bystander.id, DB)
        ticking.advance(STEP)
        standing = await services.rules.holds(ADMIN, holder.id)

    assert refused.value.fields == {"container": NOT_HELD}
    [row] = await released_rows(started_app, mark)
    assert (row["result"], row["actor_type"]) == ("failure", "user")
    assert json.loads(row["detail"])["reason"] == "not_held"
    assert [held.container_id for held in standing.containers] == [DB]


async def test_a_hold_a_rule_write_has_already_ended_cannot_be_released(
    started_app: FastAPI, world: World, shared_host: ContainerEngine, ticking: FakeClock
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(ADMIN, **arguments(world))
        ticking.advance(STEP)
        await hold_on(started_app, ticking, world, rule, DB)
        ticking.advance(STEP)
        await services.rules.enable(ADMIN, rule.id)
        ticking.advance(STEP)

        with pytest.raises(ValidationFailed) as refused:
            await services.rules.release_hold(ADMIN, rule.id, DB)

    assert refused.value.fields == {"container": NOT_HELD}


@pytest.mark.parametrize(
    ("subsystem", "interval", "container_id"),
    [
        (HEALTH_CHECKER, HEALTH_INTERVAL, "not-an-id"),
        (HEALTH_CHECKER, HEALTH_INTERVAL, DB.upper()),
        (RESTARTER, RESTARTER_INTERVAL, DB),
    ],
    ids=["malformed-id", "upper-case-id", "other-subsystem"],
)
async def test_a_release_that_cannot_name_a_hold_is_not_found_and_records_nothing(
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    ticking: FakeClock,
    subsystem: str,
    interval: str,
    container_id: str,
) -> None:
    async with job_services_on_hosts(started_app) as services:
        rule = await services.rules.create(
            ADMIN,
            **arguments(world, subsystem=subsystem, schedule_expression=interval),
        )
        mark = await last_event_id(started_app)

        with pytest.raises(ResourceNotFound):
            await services.rules.release_hold(ADMIN, rule.id, container_id)

    assert await events(started_app, since=mark) == []


STORED_WITHOUT_AUTO_RELEASE = json.dumps(
    {
        "suppression_seconds": None,
        "max_consecutive_failures": None,
        "max_restarts": 3,
        "window_seconds": 3600,
    }
)


async def enabled_rule_stored_before_auto_release(
    app: FastAPI, services: Any, world: World
) -> AutomationRule:
    rule = await services.rules.create(ADMIN, **arguments(world))
    await services.rules.enable(ADMIN, rule.id)
    async with app.state.database.session() as db:
        await db.execute(
            "UPDATE automation_rule SET parameters = ? WHERE id = ?",
            (STORED_WITHOUT_AUTO_RELEASE, rule.id),
        )
    stored: AutomationRule = await services.rules.get(ADMIN, rule.id)
    return stored


async def save(services: Any, rule: AutomationRule, parameters: dict[str, Any]) -> AutomationRule:
    saved: AutomationRule = await services.rules.update(
        ADMIN,
        rule.id,
        name=rule.name,
        target_selector=rule.target_selector,
        parameters=parameters,
        schedule_kind="interval",
        schedule_expression="60",
        time_zone="UTC",
        overrun_policy=None,
    )
    return saved


async def test_saving_a_stored_rule_untouched_without_auto_release_changes_and_disables_nothing(
    started_app: FastAPI, world: World, shared_host: ContainerEngine, ticking: FakeClock
) -> None:
    async with job_services_on_hosts(started_app) as services:
        before = await enabled_rule_stored_before_auto_release(started_app, services, world)
        ticking.advance(STEP)

        saved = await save(services, before, {"max_restarts": 3, "window_seconds": 3600})

    assert saved.is_enabled is True
    assert (saved.parameters, saved.updated_at) == (before.parameters, before.updated_at)


async def test_turning_auto_release_on_for_a_stored_rule_is_a_parameter_change_and_disables_it(
    started_app: FastAPI, world: World, shared_host: ContainerEngine, ticking: FakeClock
) -> None:
    async with job_services_on_hosts(started_app) as services:
        before = await enabled_rule_stored_before_auto_release(started_app, services, world)
        ticking.advance(STEP)

        saved = await save(
            services,
            before,
            {"max_restarts": 3, "window_seconds": 3600, "auto_release": True},
        )

    assert saved.is_enabled is False
    assert json.loads(saved.parameters)["auto_release"] is True
    assert (
        await rows(started_app, "SELECT is_enabled FROM automation_rule WHERE id = ?", (saved.id,))
    )[0]["is_enabled"] == 0
