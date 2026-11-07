"""The repository reads the automation subsystems and pages rely on."""

import sqlite3
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest

from app.db.engine import Database
from app.db.repositories.activity_event import ActivityEventRepository
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.image_binding import ImageBindingRepository
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.services.activity import ActivityRecorder, SubsystemActor
from tests.scheduler.support import RESTARTER, UPDATER, Rig, at, stamp

CONTAINER = "abc"
SERVICE = "a" * 25
ACTION = "container_restarted"
LIMIT = 50
SEEDED = "2026-01-01T00:00:00.000000Z"
ECR_EXTRAS = '{"region": "eu-west-1"}'
ECR_REFERENCE = "e/x:1"
CUSTOM_REFERENCE = "c/x:1"
PLAIN_REFERENCE = "p/x:1"
ANONYMOUS_REFERENCE = "anon/x:1"
UNKNOWN_ID = 1_000_000
MANY = 600
VARIABLE_LIMIT = 520


@pytest.fixture
async def rig(database: Database) -> AsyncIterator[Rig]:
    built = Rig(database)
    yield built
    await built.close()


@pytest.fixture
def few_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open every connection with a small bound-variable limit, as an older SQLite build has."""
    original = Database._open

    def limited(self: Database, *, create: bool) -> sqlite3.Connection:
        connection = original(self, create=create)
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, VARIABLE_LIMIT)
        return connection

    monkeypatch.setattr(Database, "_open", limited)


async def finished_runs(rig: Rig, job: int, count: int) -> list[int]:
    ids = []
    async with rig.database.session() as db:
        runs = JobRunRepository(db, rig.clock)
        for _ in range(count):
            started = await runs.start(job, trigger="schedule", scheduled_for=None)
            await runs.finish(started.id, outcome="success")
            ids.append(started.id)
    return ids


async def test_runs_are_counted_by_job_and_in_flight_ones_are_found_by_job_and_by_environment(
    rig: Rig,
) -> None:
    environment = rig.environment()
    other = rig.environment("other")
    mine = await rig.job(rule_id=await rig.rule("a"), environment_id=environment)
    theirs = await rig.job(rule_id=await rig.rule("b"), environment_id=other)
    await finished_runs(rig, mine, 3)
    await finished_runs(rig, theirs, 1)
    async with rig.database.session() as db:
        runs = JobRunRepository(db, rig.clock)
        quiet = (
            await runs.count_for_job(mine),
            await runs.any_in_flight([mine, theirs]),
            await runs.in_flight_for_environment(environment),
        )
        await runs.start(theirs, trigger="manual", scheduled_for=None)
        busy = (
            await runs.any_in_flight([mine]),
            await runs.any_in_flight([mine, theirs]),
            await runs.in_flight_for_environment(environment),
            await runs.in_flight_for_environment(other),
        )

    assert quiet == (3, False, False)
    assert busy == (False, True, False, True)


async def test_recent_outcomes_gives_each_job_its_newest_runs_newest_first(rig: Rig) -> None:
    first = await rig.rule_job("a")
    second = await rig.rule_job("b")
    empty = await rig.rule_job("c")
    first_ids = await finished_runs(rig, first, 7)
    second_ids = await finished_runs(rig, second, 2)
    async with rig.database.session() as db:
        runs = JobRunRepository(db, rig.clock)
        recent = await runs.recent_outcomes([first, second, empty], per_job=5)
        with pytest.raises(ValueError):
            await runs.recent_outcomes([first], per_job=0)

    assert [r.id for r in recent[first]] == first_ids[:1:-1]
    assert [r.id for r in recent[second]] == second_ids[::-1]
    assert recent[empty] == []


async def test_jobs_are_fetched_by_rule_keyed_by_rule_id_and_a_rule_without_one_is_absent(
    rig: Rig,
) -> None:
    first = await rig.rule("a")
    second = await rig.rule("b")
    without_job = await rig.rule("c")
    first_job = await rig.job(rule_id=first)
    second_job = await rig.job(rule_id=second)
    await rig.refresher_job()
    async with rig.database.session() as db:
        jobs = ScheduledJobRepository(db, rig.clock)
        found = await jobs.get_for_rules([second, first, first, without_job, UNKNOWN_ID])
        nothing = await jobs.get_for_rules([])

    assert {rule: job.id for rule, job in found.items()} == {first: first_job, second: second_job}
    assert all(job.rule_id == rule for rule, job in found.items())
    assert nothing == {}


async def test_jobs_for_more_rules_than_one_query_takes_are_all_found(
    rig: Rig, few_variables: None
) -> None:
    rig.bulk_rule_jobs(MANY, next_run=at(0))
    rule_ids = [row["rule_id"] for row in rig.sql("SELECT rule_id FROM scheduled_job")]
    async with rig.database.session() as db:
        found = await ScheduledJobRepository(db, rig.clock).get_for_rules(rule_ids)

    assert set(found) == set(rule_ids)


async def test_the_latest_dry_run_of_each_job_is_its_newest_dry_run_whatever_ran_after_it(
    rig: Rig,
) -> None:
    dry_then_manual = await rig.rule_job("a")
    two_dry = await rig.rule_job("b")
    only_manual = await rig.rule_job("c")
    never_run = await rig.rule_job("d")
    async with rig.database.session() as db:
        runs = JobRunRepository(db, rig.clock)
        await runs.start(two_dry, trigger="dry_run", scheduled_for=None)
        only_dry = await runs.start(dry_then_manual, trigger="dry_run", scheduled_for=None)
        newest_of_two = await runs.start(two_dry, trigger="dry_run", scheduled_for=None)
        await runs.start(dry_then_manual, trigger="manual", scheduled_for=None)
        await runs.start(dry_then_manual, trigger="schedule", scheduled_for=None)
        await runs.start(only_manual, trigger="manual", scheduled_for=None)
        found = await runs.latest_dry_runs(
            [dry_then_manual, two_dry, only_manual, never_run, UNKNOWN_ID, two_dry]
        )
        nothing = await runs.latest_dry_runs([])

    assert {job: run.id for job, run in found.items()} == {
        dry_then_manual: only_dry.id,
        two_dry: newest_of_two.id,
    }
    assert nothing == {}


async def test_latest_dry_runs_are_found_in_every_chunk_of_a_long_job_list(
    rig: Rig, few_variables: None
) -> None:
    job_ids = rig.bulk_rule_jobs(MANY, next_run=at(0))
    first, last = min(job_ids), max(job_ids)
    async with rig.database.session() as db:
        runs = JobRunRepository(db, rig.clock)
        early = await runs.start(first, trigger="dry_run", scheduled_for=None)
        late = await runs.start(last, trigger="dry_run", scheduled_for=None)
        found = await runs.latest_dry_runs(job_ids)

    assert {job: run.id for job, run in found.items()} == {first: early.id, last: late.id}


async def test_deleting_a_rule_removes_its_job_and_runs_and_reports_whether_it_existed(
    rig: Rig,
) -> None:
    rule = await rig.rule("a")
    job = await rig.job(rule_id=rule)
    await finished_runs(rig, job, 2)
    async with rig.database.session() as db:
        rules = AutomationRuleRepository(db, rig.clock)
        first, second = await rules.delete(rule), await rules.delete(rule)

    assert (first, second) == (True, False)
    assert rig.sql("SELECT id FROM scheduled_job") == []
    assert rig.sql("SELECT id FROM job_run") == []


async def test_rules_are_listed_by_environment_and_subsystem_and_paged_by_owner_and_host(
    rig: Rig,
) -> None:
    one, two = rig.environment("one"), rig.environment("two")
    owner = rig.user("owner")
    a = await rig.rule("a", owner=owner, environment_id=one)
    b = await rig.rule("b", owner=owner, environment_id=two, subsystem=UPDATER)
    c = await rig.rule("c", environment_id=one)
    async with rig.database.session() as db:
        rules = AutomationRuleRepository(db, rig.clock)
        listed = await rules.for_environment(one, [RESTARTER, UPDATER], limit=LIMIT)
        only_updater = await rules.for_environment(two, [UPDATER], limit=LIMIT)
        nothing = await rules.for_environment(one, [], limit=LIMIT)
        by_owner = await rules.page(limit=LIMIT, owner_id=owner)
        by_host = await rules.page(limit=LIMIT, environment_id=one)

    assert [r.id for r in listed] == [a, c]
    assert [r.id for r in only_updater] == [b]
    assert nothing == []
    assert [r.id for r in by_owner.items] == [a, b]
    assert [r.id for r in by_host.items] == [a, c]


async def test_a_job_is_brought_forward_only_when_nothing_else_holds_it_back(rig: Rig) -> None:
    ready = await rig.rule_job("ready", next_run=at(5000))
    failing = await rig.rule_job("failing", next_run=at(5000))
    rig.sql("UPDATE scheduled_job SET consecutive_failures = 1 WHERE id = ?", (failing,))
    now = stamp(at(100))
    async with rig.database.session() as db:
        jobs = ScheduledJobRepository(db, rig.clock)
        moved = [await jobs.bring_forward(ready, now), await jobs.bring_forward(failing, now)]
        again = await jobs.bring_forward(ready, now)

    assert (moved, again) == ([True, False], False)
    assert (await rig.job_row(ready)).next_run_at == now


async def test_open_jobs_are_the_enabled_ones_with_a_next_fire_in_the_environment(
    rig: Rig,
) -> None:
    one, two = rig.environment("one"), rig.environment("two")
    open_job = await rig.job(rule_id=await rig.rule("a"), environment_id=one, next_run=at(100))
    await rig.job(rule_id=await rig.rule("b"), environment_id=one, next_run=None)
    off = await rig.job(rule_id=await rig.rule("c"), environment_id=one, next_run=at(100))
    other_subsystem = await rig.job(
        subsystem=UPDATER,
        rule_id=await rig.rule("d", subsystem=UPDATER),
        environment_id=two,
        next_run=at(100),
    )
    rig.sql("UPDATE scheduled_job SET is_enabled = 0 WHERE id = ?", (off,))
    async with rig.database.session() as db:
        jobs = ScheduledJobRepository(db, rig.clock)
        in_one = await jobs.open_job_ids(RESTARTER, one)
        hosts = await jobs.environments_with_open_jobs(RESTARTER, limit=LIMIT)
        updater_hosts = await jobs.environments_with_open_jobs(UPDATER, limit=LIMIT)

    assert in_one == [open_job]
    assert hosts == [one]
    assert updater_hosts == [two]
    assert other_subsystem > open_job


async def test_a_subsystems_actions_are_counted_and_dated_per_rule_container_and_action(
    rig: Rig,
) -> None:
    environment = rig.environment()
    rule = await rig.rule("a", environment_id=environment)
    other = await rig.rule("b", environment_id=environment)
    start = rig.clock()
    async with rig.database.session() as db:
        recorder = ActivityRecorder(db, rig.clock)
        for who, minutes in ((rule, 0), (rule, 10), (other, 20)):
            rig.clock.now = start + timedelta(minutes=minutes)
            await recorder.record_event(
                SubsystemActor(RESTARTER, who),
                action=ACTION,
                target_type="container",
                target_id=CONTAINER,
                environment_id=environment,
                result="success",
            )
        events = ActivityEventRepository(db, rig.clock)
        keys: dict[str, Any] = {"subsystem": RESTARTER, "rule_id": rule, "action": ACTION}

        def since(minutes: int) -> str:
            return format_timestamp(start + timedelta(minutes=minutes))

        counts = [
            await events.count_subsystem_actions(
                environment, "container", CONTAINER, since=since(0), **keys
            ),
            await events.count_subsystem_actions(
                environment, "container", CONTAINER, since=since(5), **keys
            ),
            await events.count_subsystem_actions(
                environment, "container", "zzz", since=since(0), **keys
            ),
        ]
        latest = await events.latest_subsystem_action(environment, "container", CONTAINER, **keys)
        absent = await events.latest_subsystem_action(environment, "container", "zzz", **keys)

    assert counts == [2, 1, 0]
    assert latest == since(10)
    assert absent is None


async def test_the_newest_event_of_the_named_actions_is_found_by_time_then_id_for_one_rule(
    rig: Rig,
) -> None:
    environment = rig.environment()
    rule = await rig.rule("a", environment_id=environment)
    other = await rig.rule("b", environment_id=environment)
    start = rig.clock()
    plan = (
        (rule, "image_update_refused", 0),
        (rule, "container_recreated", 10),
        (rule, "container_recreated", 10),
        (rule, "container_update_rolled_back", 5),
        (other, "container_recreated", 30),
    )
    async with rig.database.session() as db:
        recorder = ActivityRecorder(db, rig.clock)
        for who, action, minutes in plan:
            rig.clock.now = start + timedelta(minutes=minutes)
            await recorder.record_event(
                SubsystemActor(UPDATER, who),
                action=action,
                target_type="container",
                target_id=CONTAINER,
                environment_id=environment,
                result="success",
            )
        events = ActivityEventRepository(db, rig.clock)
        keys: dict[str, Any] = {"subsystem": UPDATER, "rule_id": rule}
        newest = await events.latest_subsystem_event(
            environment,
            "container",
            CONTAINER,
            actions={"image_update_refused", "container_recreated"},
            **keys,
        )
        refused_only = await events.latest_subsystem_event(
            environment, "container", CONTAINER, actions={"image_update_refused"}, **keys
        )
        elsewhere = await events.latest_subsystem_event(
            environment, "container", "zzz", actions={"container_recreated"}, **keys
        )
        with pytest.raises(ValueError):
            await events.latest_subsystem_event(
                environment, "container", CONTAINER, actions=set(), **keys
            )

    assert newest is not None and newest.action == "container_recreated"
    assert newest.rule_id == rule
    assert newest.occurred_at == format_timestamp(start + timedelta(minutes=10))
    assert refused_only is not None and refused_only.action == "image_update_refused"
    assert elsewhere is None


async def test_the_newest_event_can_be_read_across_every_rule_of_a_subsystem(rig: Rig) -> None:
    environment = rig.environment()
    one = await rig.rule("a", environment_id=environment, subsystem=UPDATER)
    two = await rig.rule("b", environment_id=environment, subsystem=UPDATER)
    start = rig.clock()
    plan = (
        (one, "service_credentials_refreshed", 0),
        (two, "service_updated", 10),
        (one, "image_update_refused", 20),
    )
    async with rig.database.session() as db:
        recorder = ActivityRecorder(db, rig.clock)
        for who, action, minutes in plan:
            rig.clock.now = start + timedelta(minutes=minutes)
            await recorder.record_event(
                SubsystemActor(UPDATER, who),
                action=action,
                target_type="service",
                target_id=SERVICE,
                environment_id=environment,
                result="success",
            )
        events = ActivityEventRepository(db, rig.clock)
        named = {"service_credentials_refreshed", "service_updated"}
        any_rule = await events.latest_subsystem_event(
            environment, "service", SERVICE, subsystem=UPDATER, rule_id=None, actions=named
        )
        one_rule = await events.latest_subsystem_event(
            environment, "service", SERVICE, subsystem=UPDATER, rule_id=one, actions=named
        )
        other_subsystem = await events.latest_subsystem_event(
            environment, "service", SERVICE, subsystem=RESTARTER, rule_id=None, actions=named
        )

    assert any_rule is not None and (any_rule.rule_id, any_rule.action) == (two, "service_updated")
    assert one_rule is not None and one_rule.rule_id == one
    assert other_subsystem is None


async def test_a_binding_reports_the_provider_of_its_credential_set_or_none(rig: Rig) -> None:
    environment = rig.environment()
    ids = {}
    for name, provider, kind, extras in (
        ("ecr", "ecr", "derived", ECR_EXTRAS),
        ("custom", "custom", "static", "{}"),
        ("plain", "custom", "static", "{}"),
    ):
        ids[name] = int(
            rig.sql(
                "INSERT INTO registry_credential (name, provider, url, authentication_enabled, "
                "username, secret_kind, secret_ciphertext, provider_extras, created_at, "
                "updated_at) VALUES (?, ?, ?, 1, 'u', ?, 'x', ?, ?, ?) RETURNING id",
                (name, provider, f"{name}.example.com", kind, extras, SEEDED, SEEDED),
            )[0]["id"]
        )
    async with rig.database.session() as db:
        repository = ImageBindingRepository(db)
        async with db.transaction():
            for reference, credential in (
                (ECR_REFERENCE, ids["ecr"]),
                (CUSTOM_REFERENCE, ids["custom"]),
                (PLAIN_REFERENCE, ids["plain"]),
                (ANONYMOUS_REFERENCE, None),
            ):
                await repository.insert(
                    environment_id=environment,
                    image_reference=reference,
                    credential_id=credential,
                    observed_digest=None,
                    now=rig.clock(),
                )
            await repository.mark_missing_for_credential(
                ids["custom"], name="custom", reason="deleted", now=rig.clock()
            )
        listed = await repository.for_references(
            environment,
            [ECR_REFERENCE, CUSTOM_REFERENCE, PLAIN_REFERENCE, ANONYMOUS_REFERENCE],
        )
        single = await repository.get(environment, ECR_REFERENCE)

    assert single is not None and single.credential_provider == "ecr"
    assert listed[ECR_REFERENCE].credential_provider == "ecr"
    assert listed[CUSTOM_REFERENCE].credential_provider is None
    assert listed[CUSTOM_REFERENCE].state == "credential_missing"
    assert listed[PLAIN_REFERENCE].credential_provider == "custom"
    assert listed[ANONYMOUS_REFERENCE].credential_provider is None
