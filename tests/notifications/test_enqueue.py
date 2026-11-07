"""Queuing deliveries: in the event's transaction, per channel, scoped to environments."""

import json

import pytest

from app.db.repositories.activity_event import Outcome
from app.db.repositories.notification_channel import NotificationChannelRepository
from app.db.repositories.notification_outbox import NotificationOutboxRepository
from app.notifications import catalogue
from app.notifications.catalogue import EventType
from app.services import notifications as notification_service
from app.services.activity import (
    SUBSYSTEMS,
    ActivityRecorder,
    SubsystemActor,
    UserActor,
    install_notification_hook,
)
from tests.notification_support import add_channel
from tests.scheduler.support import Rig

REFRESHER = "environment-snapshot-refresher"
UPDATER = "container-updater"
SENDER = "notification-sender"
RECEIVER = "webhook-receiver"
ABSENT_ENVIRONMENT = 9999

STARTING_EVENTS: list[tuple[str, str, Outcome, str | None]] = [
    ("automation_run_failed", "automation_run_failed", "failure", "stack-restarter"),
    ("automation_disabled", "scheduled_job_disabled", "success", "stack-restarter"),
    ("update_applied", "container_recreated", "success", UPDATER),
    ("update_rolled_back", "container_update_rolled_back", "success", UPDATER),
    ("webhook_deployed", "container_recreated", "success", RECEIVER),
    ("webhook_rolled_back", "container_update_rolled_back", "success", RECEIVER),
    (
        "restart_hold_placed",
        "container_restart_budget_exhausted",
        "success",
        "container-health-checker",
    ),
    ("restart_hold_released", "container_restart_budget_released", "success", None),
    ("backup_sync_failed", "backup_uploaded", "failure", "backup-syncer"),
    ("log_export_failed", "activity_log_exported", "failure", "activity-log-exporter"),
    ("environment_unreachable", "environment_unreachable", "success", REFRESHER),
    ("environment_reachable", "environment_reachable", "success", REFRESHER),
    ("dockmin_updated", "instance_updated", "success", None),
    ("dockmin_update_rolled_back", "instance_update_rolled_back", "success", None),
]


async def actor_of(rig: Rig, subsystem: str | None) -> SubsystemActor | UserActor:
    if subsystem is None:
        users = rig.sql("SELECT id FROM user")
        return UserActor(users[0]["id"] if users else rig.user("ops"))
    if not SUBSYSTEMS[subsystem]:
        return SubsystemActor(subsystem, None)
    found = rig.sql("SELECT id FROM automation_rule WHERE subsystem = ?", (subsystem,))
    rule_id = found[0]["id"] if found else await rig.rule(subsystem, subsystem=subsystem)
    return SubsystemActor(subsystem, rule_id)


async def emit(
    rig: Rig,
    action: str,
    *,
    subsystem: str | None = REFRESHER,
    result: Outcome = "success",
    environment_id: int | None = None,
) -> None:
    async with rig.database.session() as db:
        await ActivityRecorder(db, rig.clock).record_event(
            await actor_of(rig, subsystem),
            action=action,
            target_type="environment",
            target_id="1",
            environment_id=environment_id,
            result=result,
        )


def queued(rig: Rig) -> list[tuple[int, str]]:
    rows = rig.sql("SELECT channel_id, event_type FROM notification_outbox ORDER BY id")
    return [(row["channel_id"], row["event_type"]) for row in rows]


def event_count(rig: Rig) -> int:
    return int(rig.sql("SELECT count(*) AS total FROM activity_event")[0]["total"])


async def test_every_starting_event_type_is_queued_once_from_its_activity_row(rig: Rig) -> None:
    keys = [key for key, *_ in STARTING_EVENTS]
    await add_channel(rig.database, rig.clock, "all", keys)

    for _, action, result, subsystem in STARTING_EVENTS:
        await emit(rig, action, subsystem=subsystem, result=result)

    assert [key for _, key in queued(rig)] == keys
    assert event_count(rig) == len(STARTING_EVENTS)


async def test_one_row_is_queued_for_each_enabled_channel_that_subscribes(rig: Rig) -> None:
    first = await add_channel(rig.database, rig.clock, "first", ["update_applied"])
    second = await add_channel(
        rig.database, rig.clock, "second", ["update_applied", "dockmin_updated"]
    )
    await add_channel(rig.database, rig.clock, "elsewhere", ["dockmin_updated"])
    await add_channel(rig.database, rig.clock, "off", ["update_applied"], enabled=False)

    await emit(rig, "container_recreated", subsystem=UPDATER)

    assert queued(rig) == [(first, "update_applied"), (second, "update_applied")]


async def test_a_key_no_longer_in_the_catalogue_matches_nothing(rig: Rig) -> None:
    await add_channel(rig.database, rig.clock, "old", ["retired_kind"])

    await emit(rig, "container_recreated", subsystem=UPDATER)

    assert queued(rig) == []


async def test_a_row_added_to_the_catalogue_queues_deliveries_without_another_change(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    added = EventType("disk_nearly_full", "Disk nearly full", frozenset({"disk_check"}), "failure")
    extended = (*catalogue.CATALOGUE, added)
    monkeypatch.setattr(catalogue, "CATALOGUE", extended)
    monkeypatch.setattr(notification_service, "CATALOGUE", extended)
    channel = await add_channel(rig.database, rig.clock, "disks", ["disk_nearly_full"])

    await emit(rig, "disk_check", subsystem=None, result="failure")

    assert queued(rig) == [(channel, "disk_nearly_full")]


async def test_a_delivery_failure_never_queues_a_delivery(rig: Rig, poked: list[int]) -> None:
    await add_channel(
        rig.database, rig.clock, "everything", [row.key for row in catalogue.CATALOGUE]
    )

    await emit(rig, "notification_delivery_failed", subsystem=SENDER, result="failure")

    assert event_count(rig) == 1
    assert queued(rig) == [] and poked == []


async def test_an_event_that_matches_no_event_type_writes_its_row_and_queues_nothing(
    rig: Rig, poked: list[int]
) -> None:
    await add_channel(
        rig.database, rig.clock, "everything", [row.key for row in catalogue.CATALOGUE]
    )

    await emit(rig, "container_started", subsystem=None)

    assert event_count(rig) == 1
    assert queued(rig) == [] and poked == []


async def test_the_row_is_queued_in_the_events_transaction_and_a_rollback_leaves_neither(
    rig: Rig, poked: list[int]
) -> None:
    await add_channel(rig.database, rig.clock, "chat", ["environment_unreachable"])

    with pytest.raises(RuntimeError, match="abandoned"):
        async with rig.database.session() as db, db.transaction():
            await ActivityRecorder(db, rig.clock).record_event(
                SubsystemActor(REFRESHER, None),
                action="environment_unreachable",
                target_type="environment",
                target_id="1",
                result="success",
            )
            raise RuntimeError("abandoned")

    assert event_count(rig) == 0 and queued(rig) == []
    assert poked == []


async def test_events_in_one_committed_transaction_queue_together_and_poke_once(
    rig: Rig, poked: list[int]
) -> None:
    await add_channel(rig.database, rig.clock, "chat", ["environment_unreachable"])

    async with rig.database.session() as db, db.transaction():
        recorder = ActivityRecorder(db, rig.clock)
        for target in ("1", "2"):
            await recorder.record_event(
                SubsystemActor(REFRESHER, None),
                action="environment_unreachable",
                target_type="environment",
                target_id=target,
                result="success",
            )
        assert poked == []

    assert len(queued(rig)) == 2
    assert poked == [1]


async def test_an_event_written_outside_a_transaction_is_queued_with_it_and_pokes_after_commit(
    rig: Rig, poked: list[int]
) -> None:
    await add_channel(rig.database, rig.clock, "chat", ["environment_unreachable"])

    await emit(rig, "environment_unreachable")

    assert len(queued(rig)) == 1 and event_count(rig) == 1
    assert poked == [1]


async def test_a_queuing_that_fails_leaves_no_event_row_and_no_poke(
    rig: Rig, poked: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_channel(rig.database, rig.clock, "chat", ["environment_unreachable"])

    async def refuse(*args: object, **kwargs: object) -> int:
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(NotificationOutboxRepository, "enqueue", refuse)

    with pytest.raises(RuntimeError, match="unavailable"):
        await emit(rig, "environment_unreachable")

    assert event_count(rig) == 0 and poked == []


async def test_an_event_nobody_subscribed_to_pokes_nothing(rig: Rig, poked: list[int]) -> None:
    await emit(rig, "environment_unreachable")

    assert event_count(rig) == 1 and poked == []


async def test_a_hook_that_raises_does_not_stop_the_event_being_recorded(rig: Rig) -> None:
    def broken() -> None:
        raise RuntimeError("hook failed")

    await add_channel(rig.database, rig.clock, "chat", ["environment_unreachable"])
    install_notification_hook(broken)
    try:
        await emit(rig, "environment_unreachable")
    finally:
        install_notification_hook(None)

    assert len(queued(rig)) == 1


async def test_a_channel_limited_to_listed_environments_receives_only_those(rig: Rig) -> None:
    prod, staging = rig.environment("prod"), rig.environment("staging")
    listed = await add_channel(
        rig.database,
        rig.clock,
        "prod only",
        ["environment_unreachable"],
        scope="listed",
        environments=[prod],
    )

    await emit(rig, "environment_unreachable", environment_id=staging)
    await emit(rig, "environment_unreachable", environment_id=prod)

    assert queued(rig) == [(listed, "environment_unreachable")]
    assert rig.sql("SELECT event_id FROM notification_outbox")[0]["event_id"] == 2


async def test_an_event_that_names_no_environment_reaches_a_limited_channel(rig: Rig) -> None:
    prod = rig.environment("prod")
    listed = await add_channel(
        rig.database,
        rig.clock,
        "prod only",
        ["backup_sync_failed"],
        scope="listed",
        environments=[prod],
    )

    await emit(rig, "backup_uploaded", subsystem="backup-syncer", result="failure")

    assert queued(rig) == [(listed, "backup_sync_failed")]


async def test_deleting_the_last_listed_environment_leaves_a_channel_that_matches_no_environment(
    rig: Rig,
) -> None:
    prod, staging = rig.environment("prod"), rig.environment("staging")
    listed = await add_channel(
        rig.database,
        rig.clock,
        "prod only",
        ["environment_unreachable", "backup_sync_failed"],
        scope="listed",
        environments=[prod],
    )
    rig.sql("DELETE FROM environment WHERE id = ?", (prod,))

    await emit(rig, "environment_unreachable", environment_id=staging)
    await emit(rig, "backup_uploaded", subsystem="backup-syncer", result="failure")

    async with rig.database.session() as db:
        channels = NotificationChannelRepository(db, rig.clock)
        channel = await channels.get(listed)
        remaining = await channels.environment_ids(listed)
    assert channel is not None and channel.environment_scope == "listed"
    assert remaining == frozenset()
    assert queued(rig) == [(listed, "backup_sync_failed")]


async def test_a_row_whose_environment_was_deleted_under_it_is_scoped_by_the_environment_in_detail(
    rig: Rig,
) -> None:
    prod = rig.environment("prod")
    everywhere = await add_channel(
        rig.database, rig.clock, "everywhere", ["environment_unreachable"]
    )
    await add_channel(
        rig.database,
        rig.clock,
        "prod only",
        ["environment_unreachable"],
        scope="listed",
        environments=[prod],
    )

    async with rig.database.session() as db:
        await ActivityRecorder(db, rig.clock).record_failure(
            SubsystemActor(REFRESHER, None),
            action="environment_unreachable",
            target_type="environment",
            target_id=str(ABSENT_ENVIRONMENT),
            environment_id=ABSENT_ENVIRONMENT,
            reason="host_down",
        )

    [stored] = rig.sql("SELECT environment_id, detail FROM activity_event")
    assert stored["environment_id"] is None
    assert json.loads(stored["detail"])["environment_id"] == str(ABSENT_ENVIRONMENT)
    assert queued(rig) == [(everywhere, "environment_unreachable")]
