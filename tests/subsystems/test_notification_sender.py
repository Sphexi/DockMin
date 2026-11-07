"""The notification sender: grouping, retries, per-channel recording and what is sent."""

# ruff: noqa: F811

import asyncio
import json
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.auth.errors import ValidationFailed
from app.config import Settings
from app.crypto.vault import Vault
from app.db.repositories.activity_event import Outcome
from app.db.repositories.clocked import format_timestamp
from app.notifications import catalogue
from app.notifications.catalogue import EventType
from app.notifications.delivery import message_for
from app.scheduler.model import RunResult
from app.services import notifications as notification_module
from app.services.activity import SUBSYSTEMS, ActivityRecorder, SubsystemActor, UserActor
from app.services.notifications import ChannelInput, NotificationService, SubscriptionInput
from app.subsystems.notification_sender import NotificationSender
from tests.job_support import ADMIN
from tests.notification_fixtures import (  # noqa: F401
    StartReceiver,
    StartSink,
    certificates,
    receivers,
    sinks,
    trusting,
)
from tests.notification_support import SMTP_PASSWORD, SMTP_USER
from tests.scheduler.support import Rig, dispatch_of

HMAC = "hmac-key-sentinel"
HEADER = "header-value-sentinel"
DETAIL_NOTE = "detail-note-sentinel"
URL = "https://hooks.example.test/receive"
SLOW_URL = "https://slow.example.test/receive"
REFRESHER = "environment-snapshot-refresher"
UNREACHABLE = "environment_unreachable"
FIRST_RETRY = timedelta(seconds=120)
SECOND_RETRY = timedelta(seconds=480)
SETTING = "DOCKMIN_PERMITTED_INTERNAL_NETWORKS"
MORE_THAN_ONE_MESSAGE = 25
FULL_MESSAGE = 20
LINK_BASE = "https://dock.example.test"
ABSENT_ENVIRONMENT = 4321

STARTING_EVENTS: list[tuple[str, str, Outcome, str | None]] = [
    ("automation_run_failed", "automation_run_failed", "failure", "stack-restarter"),
    ("automation_disabled", "scheduled_job_disabled", "success", "stack-restarter"),
    ("update_applied", "container_recreated", "success", "container-updater"),
    ("update_rolled_back", "container_update_rolled_back", "success", "container-updater"),
    ("webhook_deployed", "container_recreated", "success", "webhook-receiver"),
    ("webhook_rolled_back", "container_update_rolled_back", "success", "webhook-receiver"),
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


class Receiver(httpx.AsyncBaseTransport):
    """A stand-in for the network: records what it is sent and answers with a chosen status."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.statuses: dict[str, int] = {}
        self.default_status = 204
        self.gates: dict[str, asyncio.Event] = {}
        self.poke_on_arrival: Callable[[], None] | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        gate = self.gates.get(request.url.host)
        if gate is not None:
            await gate.wait()
        await request.aread()
        self.requests.append(request)
        status = self.statuses.get(request.url.host, self.default_status)
        return httpx.Response(status, stream=httpx.ByteStream(b""))

    def bodies(self, host: str | None = None) -> list[dict[str, Any]]:
        return [
            json.loads(request.content)
            for request in self.requests
            if host is None or request.url.host == host
        ]

    def sizes(self) -> list[int]:
        return [len(body["events"]) for body in self.bodies()]


@dataclass(slots=True)
class Bench:
    rig: Rig
    vault: Vault
    sender: NotificationSender
    job: int
    channel_id: int
    environment: int

    async def run(self) -> RunResult:
        return await dispatch_of(self.rig, self.sender, self.job)

    async def create_channel(self, name: str, form: ChannelInput, event_types: set[str]) -> int:
        async with self.rig.database.session() as db:
            service = NotificationService(
                db, self.vault, self.rig.clock, ActivityRecorder(db, self.rig.clock)
            )
            channel_id = await service.create(ADMIN, form)
            await service.set_subscriptions(
                ADMIN, channel_id, SubscriptionInput(frozenset(event_types), "all", frozenset())
            )
        return channel_id

    async def emit(
        self,
        count: int = 1,
        *,
        action: str = UNREACHABLE,
        environment_id: int | None = None,
        detail: dict[str, str] | None = None,
    ) -> None:
        async with self.rig.database.session() as db:
            recorder = ActivityRecorder(db, self.rig.clock)
            for number in range(count):
                await recorder.record_event(
                    SubsystemActor(REFRESHER, None),
                    action=action,
                    target_type="environment",
                    target_id=str(self.environment),
                    environment_id=self.environment if environment_id is None else environment_id,
                    result="success",
                    detail={"name": f"prod-{number}"} | (detail or {}),
                )

    async def actor_for(self, subsystem: str | None) -> SubsystemActor | UserActor:
        if subsystem is None:
            return UserActor(1)
        if not SUBSYSTEMS[subsystem]:
            return SubsystemActor(subsystem, None)
        found = self.rig.sql("SELECT id FROM automation_rule WHERE subsystem = ?", (subsystem,))
        rule = found[0]["id"] if found else await self.rig.rule(subsystem, subsystem=subsystem)
        return SubsystemActor(subsystem, rule)

    async def emit_every_starting_event(self) -> None:
        for _, action, result, subsystem in STARTING_EVENTS:
            actor = await self.actor_for(subsystem)
            async with self.rig.database.session() as db:
                await ActivityRecorder(db, self.rig.clock).record_event(
                    actor,
                    action=action,
                    target_type="thing",
                    target_id="1",
                    result=result,
                )

    async def wait_until_only_queued(self, channel_id: int) -> None:
        while [row["channel_id"] for row in self.outbox()] != [channel_id]:  # noqa: ASYNC110
            await asyncio.sleep(0)

    def outbox(self) -> list[Any]:
        return self.rig.sql(
            "SELECT id, channel_id, attempts, next_attempt_at FROM notification_outbox ORDER BY id"
        )

    def channel(self, channel_id: int | None = None) -> Any:
        return self.rig.sql(
            "SELECT last_success_at, last_error, last_error_at FROM notification_channel "
            "WHERE id = ?",
            (self.channel_id if channel_id is None else channel_id,),
        )[0]

    def failures(self) -> list[Any]:
        return self.rig.events("notification_delivery_failed")

    def due_after(self, delay: timedelta) -> str:
        return format_timestamp(self.rig.clock() + delay)


def webhook_form(name: str = "chat", url: str = URL, **secrets: str) -> ChannelInput:
    return ChannelInput(
        name=name,
        kind="webhook",
        values={"url": url, "header_name": "X-Token" if "header_value" in secrets else ""},
        secrets={key: SecretStr(value) for key, value in secrets.items()},
    )


async def build(
    rig: Rig,
    settings: Settings,
    vault: Vault,
    transport: httpx.AsyncBaseTransport | None,
    *,
    form: ChannelInput | None = None,
    event_types: set[str] | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> Bench:
    deps, _ = rig.deps(settings)
    deps.engine.vault = vault
    rig.user("ops")
    environment = rig.environment("prod")
    job = await rig.job(subsystem="notification-sender", rule_id=None, threshold=None)
    bench = Bench(
        rig=rig,
        vault=vault,
        sender=NotificationSender(deps, transport=transport, ssl_context=ssl_context),
        job=job,
        channel_id=0,
        environment=environment,
    )
    bench.channel_id = await bench.create_channel(
        "chat",
        form or webhook_form(hmac_key=HMAC, header_value=HEADER),
        {UNREACHABLE} if event_types is None else event_types,
    )
    return bench


async def test_a_due_event_is_sent_as_one_message_with_only_the_fixed_fields_and_leaves_the_queue(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    receiver = Receiver()
    bench = await build(rig, settings, vault, receiver)
    await bench.emit(detail={"note": DETAIL_NOTE})

    result = await bench.run()

    [request] = receiver.requests
    [body] = receiver.bodies()
    [sent] = body["events"]
    assert (result.outcome, result.attempted) == ("success", 1)
    assert dict(result.detail) == {"delivered": 1, "retried": 0, "failed": 0}
    assert request.headers["x-token"] == HEADER
    assert request.headers["x-dockmin-signature"].startswith("sha256=")
    assert sent["type"] == UNREACHABLE and sent["label"] == "Environment unreachable"
    assert sent["actor"] == {"type": "subsystem", "name": REFRESHER, "rule": None}
    assert sent["target"] == {"type": "environment", "name": "prod-0"}
    assert sent["environment"] == {"id": bench.environment, "name": "prod"}
    assert sent["link"] is None and DETAIL_NOTE not in request.content.decode()
    assert bench.outbox() == [] and bench.channel()["last_success_at"] is not None


async def test_the_message_links_to_the_activity_event_when_a_base_address_is_set(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    receiver = Receiver()
    bench = await build(rig, settings, vault, receiver)
    rig.sql(
        "INSERT INTO setting (key, value_type, value, updated_at) "
        "VALUES ('notification_link_base_url', 'string', ?, 'x')",
        (LINK_BASE,),
    )
    await bench.emit()

    await bench.run()

    event_id = rig.sql("SELECT id FROM activity_event WHERE action = ?", (UNREACHABLE,))[0]["id"]
    assert receiver.bodies()[0]["events"][0]["link"] == f"{LINK_BASE}/activity/{event_id}"


async def test_an_event_by_a_user_and_one_by_a_rule_name_their_actor(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    receiver = Receiver()
    bench = await build(rig, settings, vault, receiver, event_types={"restart_hold_released"})
    rule = await rig.rule("nightly", subsystem="container-health-checker")
    async with rig.database.session() as db:
        recorder = ActivityRecorder(db, rig.clock)
        for actor in (
            SubsystemActor("container-health-checker", rule, None, "nightly"),
            UserActor(1),
        ):
            await recorder.record_event(
                actor,
                action="container_restart_budget_released",
                target_type="container",
                target_id="c1",
                environment_id=bench.environment,
                result="success",
                detail={"name": "web"},
            )

    await bench.run()

    by_actor = [event["actor"] for event in receiver.bodies()[0]["events"]]
    assert by_actor == [
        {"type": "subsystem", "name": "container-health-checker", "rule": "nightly"},
        {"type": "user", "name": "ops", "rule": None},
    ]


async def test_twenty_five_events_arrive_as_two_messages_oldest_first(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    receiver = Receiver()
    bench = await build(rig, settings, vault, receiver)
    await bench.emit(MORE_THAN_ONE_MESSAGE)

    result = await bench.run()

    names = [event["target"]["name"] for body in receiver.bodies() for event in body["events"]]
    assert receiver.sizes() == [FULL_MESSAGE, MORE_THAN_ONE_MESSAGE - FULL_MESSAGE]
    assert names == [f"prod-{number}" for number in range(MORE_THAN_ONE_MESSAGE)]
    assert dict(result.detail)["delivered"] == 2 and bench.outbox() == []


async def test_a_run_with_nothing_due_sends_nothing(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    receiver = Receiver()
    bench = await build(rig, settings, vault, receiver)

    result = await bench.run()

    assert (result.outcome, result.attempted, receiver.requests) == ("success", 0, [])


async def test_a_failing_receiver_is_tried_three_times_on_the_schedule_then_a_row_is_written(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    receiver = Receiver()
    receiver.default_status = 503
    everything = {row.key for row in catalogue.CATALOGUE}
    bench = await build(rig, settings, vault, receiver, event_types=everything)
    await bench.emit()

    first = await bench.run()
    after_first = [(row["attempts"], row["next_attempt_at"]) for row in bench.outbox()]
    due_first = bench.due_after(FIRST_RETRY)
    early = await bench.run()
    rig.clock.jump(FIRST_RETRY.total_seconds())
    second = await bench.run()
    after_second = [(row["attempts"], row["next_attempt_at"]) for row in bench.outbox()]
    due_second = bench.due_after(SECOND_RETRY)
    rig.clock.jump(SECOND_RETRY.total_seconds() - 1)
    too_soon = await bench.run()
    rig.clock.jump(1)
    third = await bench.run()

    [failed] = bench.failures()
    detail = json.loads(failed["detail"])
    assert after_first == [(1, due_first)] and after_second == [(2, due_second)]
    assert [dict(r.detail) for r in (first, early, second, too_soon, third)] == [
        {"delivered": 0, "retried": 1, "failed": 0},
        {"delivered": 0, "retried": 0, "failed": 0},
        {"delivered": 0, "retried": 1, "failed": 0},
        {"delivered": 0, "retried": 0, "failed": 0},
        {"delivered": 0, "retried": 0, "failed": 1},
    ]
    assert len(receiver.requests) == 3 and bench.outbox() == []
    assert (failed["actor_type"], failed["actor_subsystem"], failed["result"]) == (
        "subsystem",
        "notification-sender",
        "failure",
    )
    assert (failed["target_type"], failed["target_id"]) == ("notification_channel", "1")
    assert failed["rule_id"] is None
    assert detail["reason"] == "status_503" and detail["message"] == message_for("status_503")
    assert (detail["name"], detail["kind"], detail["events"]) == ("chat", "webhook", 1)
    assert detail["event_types"] == UNREACHABLE
    channel = bench.channel()
    assert channel["last_error"] == message_for("status_503") and channel["last_error_at"]


async def test_the_failure_row_is_not_itself_notified_even_to_a_channel_subscribed_to_everything(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    receiver = Receiver()
    receiver.default_status = 500
    bench = await build(
        rig, settings, vault, receiver, event_types={row.key for row in catalogue.CATALOGUE}
    )
    await bench.emit()
    await bench.run()
    rig.clock.jump(FIRST_RETRY.total_seconds())
    await bench.run()
    rig.clock.jump(SECOND_RETRY.total_seconds())

    await bench.run()

    assert len(bench.failures()) == 1
    assert bench.outbox() == [] and len(receiver.requests) == 3


async def test_rows_with_the_same_number_of_tries_go_together_and_the_others_a_message_later(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    receiver = Receiver()
    receiver.default_status = 503
    bench = await build(rig, settings, vault, receiver)
    await bench.emit(3)
    rig.sql("UPDATE notification_outbox SET attempts = 1 WHERE id = 3")

    await bench.run()

    rows = {row["id"]: (row["attempts"], row["next_attempt_at"]) for row in bench.outbox()}
    assert sorted(receiver.sizes()) == [1, 2]
    assert rows[1] == rows[2] == (1, bench.due_after(FIRST_RETRY))
    assert rows[3] == (2, bench.due_after(SECOND_RETRY))


async def test_each_channels_result_is_recorded_on_its_own(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    receiver = Receiver()
    receiver.statuses = {"slow.example.test": 502}
    bench = await build(rig, settings, vault, receiver)
    other = await bench.create_channel("slow", webhook_form("slow", SLOW_URL), {UNREACHABLE})
    await bench.emit()

    result = await bench.run()

    assert dict(result.detail) == {"delivered": 1, "retried": 1, "failed": 0}
    assert [(row["channel_id"], row["attempts"]) for row in bench.outbox()] == [(other, 1)]
    assert bench.channel()["last_success_at"] is not None
    assert bench.channel(other)["last_success_at"] is None


async def test_a_cancelled_run_keeps_what_it_recorded_and_the_next_never_resends_it(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    receiver = Receiver()
    receiver.gates["slow.example.test"] = asyncio.Event()
    bench = await build(rig, settings, vault, receiver)
    slow = await bench.create_channel("slow", webhook_form("slow", SLOW_URL), {UNREACHABLE})
    await bench.emit()
    run = asyncio.ensure_future(bench.run())
    await bench.wait_until_only_queued(slow)

    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    survivors = [(row["channel_id"], row["attempts"]) for row in bench.outbox()]
    receiver.gates["slow.example.test"].set()
    await bench.run()

    assert survivors == [(slow, 0)]
    assert len(receiver.bodies("hooks.example.test")) == 1
    assert len(receiver.bodies("slow.example.test")) == 1
    assert bench.outbox() == []


@pytest.mark.parametrize(
    "change",
    [
        pytest.param("DELETE FROM notification_subscription", id="unsubscribed"),
        pytest.param("UPDATE notification_channel SET is_enabled = 0", id="disabled"),
        pytest.param(
            "UPDATE notification_channel SET environment_scope = 'listed'", id="scope-narrowed"
        ),
    ],
)
async def test_a_queued_row_the_channel_no_longer_wants_is_dropped_unsent(
    rig: Rig, settings: Settings, vault: Vault, change: str
) -> None:
    receiver = Receiver()
    bench = await build(rig, settings, vault, receiver)
    await bench.emit()
    rig.sql(change)

    result = await bench.run()

    assert receiver.requests == [] and bench.outbox() == []
    assert dict(result.detail) == {"delivered": 0, "retried": 0, "failed": 0}


async def test_a_row_of_a_type_no_longer_in_the_catalogue_is_dropped_unsent(
    rig: Rig, settings: Settings, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = Receiver()
    bench = await build(rig, settings, vault, receiver)
    await bench.emit()
    monkeypatch.setattr(catalogue, "CATALOGUE", ())

    await bench.run()

    assert receiver.requests == [] and bench.outbox() == []


async def test_an_event_whose_environment_was_deleted_is_scoped_by_the_one_in_its_detail(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    receiver = Receiver()
    bench = await build(rig, settings, vault, receiver)
    async with rig.database.session() as db:
        await ActivityRecorder(db, rig.clock).record_failure(
            SubsystemActor(REFRESHER, None),
            action=UNREACHABLE,
            target_type="environment",
            target_id=str(ABSENT_ENVIRONMENT),
            environment_id=ABSENT_ENVIRONMENT,
            reason="host_down",
        )
    rig.sql("UPDATE notification_channel SET environment_scope = 'listed'")
    rig.sql(
        "INSERT INTO notification_channel_environment (channel_id, environment_id) VALUES (1, ?)",
        (bench.environment,),
    )

    await bench.run()

    assert receiver.requests == [] and bench.outbox() == []


async def test_a_listed_channel_still_receives_an_event_naming_no_environment(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    receiver = Receiver()
    bench = await build(rig, settings, vault, receiver, event_types={"backup_sync_failed"})
    rig.sql("UPDATE notification_channel SET environment_scope = 'listed'")
    async with rig.database.session() as db:
        await ActivityRecorder(db, rig.clock).record_event(
            SubsystemActor("backup-syncer", await rig.rule("sync", subsystem="backup-syncer")),
            action="backup_uploaded",
            target_type="backup",
            target_id="b1",
            result="failure",
        )

    await bench.run()

    [event] = receiver.bodies()[0]["events"]
    assert event["environment"] is None and event["result"] == "failure"


async def test_secrets_the_key_cannot_open_are_an_invalid_target_that_is_retried_not_sent(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    receiver = Receiver()
    bench = await build(rig, settings, vault, receiver)
    rig.sql("UPDATE notification_channel SET secrets_ciphertext = 'v1:garbage'")
    await bench.emit()

    result = await bench.run()

    assert receiver.requests == []
    assert [row["attempts"] for row in bench.outbox()] == [1]
    assert dict(result.detail)["retried"] == 1


async def test_every_starting_event_type_reaches_the_receiver_with_its_label(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    receiver = Receiver()
    keys = {key for key, *_ in STARTING_EVENTS}
    bench = await build(rig, settings, vault, receiver, event_types=keys)
    await bench.emit_every_starting_event()

    await bench.run()

    [body] = receiver.bodies()
    assert [(event["type"], event["label"]) for event in body["events"]] == [
        (key, catalogue.event_type(key).label)  # type: ignore[union-attr]
        for key, *_ in STARTING_EVENTS
    ]


async def test_a_catalogue_row_added_later_is_subscribable_queued_and_sent_without_another_change(
    rig: Rig, settings: Settings, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    added = EventType("disk_nearly_full", "Disk nearly full", frozenset({"disk_check"}), "failure")
    extended = (*catalogue.CATALOGUE, added)
    monkeypatch.setattr(catalogue, "CATALOGUE", extended)
    monkeypatch.setattr(notification_module, "CATALOGUE", extended)
    receiver = Receiver()
    bench = await build(rig, settings, vault, receiver, event_types={"disk_nearly_full"})
    async with rig.database.session() as db:
        await ActivityRecorder(db, rig.clock).record_event(
            UserActor(1),
            action="disk_check",
            target_type="host",
            target_id="h1",
            result="failure",
        )

    await bench.run()

    [event] = receiver.bodies()[0]["events"]
    assert (event["type"], event["label"]) == ("disk_nearly_full", "Disk nearly full")


async def test_subscribing_to_an_unknown_type_is_refused_until_the_catalogue_lists_it(
    rig: Rig, settings: Settings, vault: Vault
) -> None:
    bench = await build(rig, settings, vault, Receiver())

    with pytest.raises(ValidationFailed):
        async with rig.database.session() as db:
            service = NotificationService(db, vault, rig.clock, ActivityRecorder(db, rig.clock))
            await service.set_subscriptions(
                ADMIN,
                bench.channel_id,
                SubscriptionInput(frozenset({"disk_nearly_full"}), "all", frozenset()),
            )


async def test_an_internal_receiver_without_a_permit_fails_naming_the_setting_on_the_channel(
    rig: Rig,
    settings: Settings,
    vault: Vault,
    receivers: StartReceiver,
    address_policy: Callable[..., None],
) -> None:
    receiver = await receivers()
    bench = await build(
        rig, settings, vault, None, form=webhook_form(url=receiver.url("/hook"), hmac_key=HMAC)
    )
    await bench.emit()
    address_policy()

    await bench.run()
    rig.clock.jump(FIRST_RETRY.total_seconds())
    await bench.run()
    rig.clock.jump(SECOND_RETRY.total_seconds())
    await bench.run()

    [failed] = bench.failures()
    assert json.loads(failed["detail"])["reason"] == "address_refused"
    assert SETTING in bench.channel()["last_error"]
    assert receiver.requests == []


async def test_an_internal_receiver_the_permit_covers_is_delivered_to_over_the_real_transport(
    rig: Rig,
    settings: Settings,
    vault: Vault,
    receivers: StartReceiver,
    address_policy: Callable[..., None],
) -> None:
    receiver = await receivers()
    bench = await build(
        rig, settings, vault, None, form=webhook_form(url=receiver.url("/hook"), hmac_key=HMAC)
    )
    await bench.emit()
    address_policy("127.0.0.0/8")

    await bench.run()

    [received] = receiver.requests
    assert received.json()["events"][0]["type"] == UNREACHABLE
    assert bench.outbox() == [] and bench.failures() == []


async def test_events_for_an_email_channel_arrive_as_one_message_over_starttls(
    rig: Rig, settings: Settings, vault: Vault, sinks: StartSink, trusting: ssl.SSLContext
) -> None:
    sink = await sinks("starttls")
    form = ChannelInput(
        name="mail",
        kind="email",
        values={
            "host": "127.0.0.1",
            "port": str(sink.port),
            "security": "starttls",
            "sender": "dockmin@example.test",
            "recipients": "ops@example.test",
            "username": SMTP_USER,
        },
        secrets={"password": SecretStr(SMTP_PASSWORD)},
    )
    bench = await build(rig, settings, vault, None, form=form, ssl_context=trusting)
    await bench.emit(2)

    await bench.run()

    [mail] = sink.messages
    assert mail.parsed()["Subject"] == "dockmin: 2 events"
    assert (mail.encrypted, mail.authenticated_as) == (True, SMTP_USER)
    assert bench.outbox() == []
