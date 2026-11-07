"""notification-sender: delivers queued notifications, one message per channel per pass."""

import asyncio
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import ClassVar, Final

import httpx

from app import netcheck
from app.db.repositories.activity_event import ActivityEvent, ActivityEventRepository
from app.db.repositories.clocked import parse_timestamp
from app.db.repositories.notification_channel import (
    NotificationChannel,
    NotificationChannelRepository,
)
from app.db.repositories.notification_outbox import NotificationOutboxRepository, OutboxRow
from app.db.repositories.scheduled_job import OverrunPolicy, ScheduleKind
from app.db.repositories.setting import SettingRepository
from app.logging import get_logger
from app.notifications.catalogue import DELIVERY_FAILED_ACTION, EventType, event_type
from app.notifications.content import MAX_EVENTS_PER_MESSAGE, ActorType, NotificationEvent
from app.notifications.delivery import DeliveryError, HttpDeliverer, HttpTarget, message_for
from app.notifications.poker import NotificationPoker
from app.notifications.smtp import SmtpTarget
from app.scheduler.model import RunResult
from app.services.activity import SubsystemActor, completed, linked_environment
from app.services.activity_csv import ExportRow, export_rows
from app.services.notifications import (
    LINK_BASE_URL_KEY,
    SecretsUnreadable,
    build_target,
    open_secrets,
    send_message,
)
from app.subsystems.base import RunContext, Subsystem, SubsystemDeps

logger = get_logger(__name__)

NAME: Final = "notification-sender"
INTERVAL_SECONDS: Final = 60
MAX_PASSES: Final = 5
CHANNELS_PER_PASS: Final = 20
CONCURRENT_DELIVERIES: Final = 4
RETRY_DELAYS: Final = (timedelta(seconds=120), timedelta(seconds=480))
MAX_EVENT_TYPES_TEXT: Final = 512
TARGET_TYPE: Final = "notification_channel"


@dataclass(slots=True)
class _Batch:
    channel: NotificationChannel
    rows: list[OutboxRow]
    events: list[NotificationEvent]
    target: HttpTarget | SmtpTarget | None = field(repr=False)
    code: str | None = None


@dataclass(slots=True)
class _Tally:
    delivered: int = 0
    retried: int = 0
    failed: int = 0


class NotificationSender(Subsystem):
    """Sends the oldest queued events of each channel as one message and retries failures."""

    name = NAME
    parameters_model = None
    event_policy = "none"
    schedule_kinds: ClassVar[frozenset[ScheduleKind]] = frozenset({"interval"})
    min_interval_seconds = 15
    overrun_policy = "skip"
    allowed_overrun_policies: ClassVar[frozenset[OverrunPolicy]] = frozenset({"skip"})
    cancellation_safe = True
    catch_up = True
    run_timeout = timedelta(minutes=15)
    failure_threshold = None
    requires_environment = False

    def __init__(
        self,
        deps: SubsystemDeps,
        *,
        poker: NotificationPoker | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: netcheck.Resolver | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(deps)
        self._poker = poker
        self._transport = transport
        self._resolver = resolver
        self._ssl_context = ssl_context

    async def run(self, context: RunContext) -> RunResult:
        """Deliver what is due, in at most a few passes, and report how many messages were tried."""
        outbox = NotificationOutboxRepository(context.db, context.clock)
        tally = _Tally()
        if self._poker is not None:
            self._poker.take()
        async with HttpDeliverer(transport=self._transport, clock=context.clock) as http:
            for _ in range(MAX_PASSES):
                channel_ids = await outbox.due_channel_ids(limit=CHANNELS_PER_PASS)
                if not channel_ids:
                    if self._poker is not None and self._poker.take():
                        continue
                    break
                await self._pass(context, http, channel_ids, tally)
        return RunResult(
            "success",
            attempted=tally.delivered + tally.retried + tally.failed,
            detail={
                "delivered": tally.delivered,
                "retried": tally.retried,
                "failed": tally.failed,
            },
        )

    async def _pass(
        self, context: RunContext, http: HttpDeliverer, channel_ids: list[int], tally: _Tally
    ) -> None:
        link_base = await self._link_base(context)
        batches = [
            batch
            for channel_id in channel_ids
            if (batch := await self._load_batch(context, channel_id, link_base)) is not None
        ]
        gate = asyncio.Semaphore(CONCURRENT_DELIVERIES)
        recording = asyncio.Lock()
        now = context.clock()

        async def handle(batch: _Batch) -> None:
            await self._deliver(batch, http, gate, now)
            async with recording:
                await completed(self._settle(context, batch, tally))

        outcomes = await asyncio.gather(
            *(handle(batch) for batch in batches), return_exceptions=True
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome

    async def _link_base(self, context: RunContext) -> str | None:
        stored = await SettingRepository(context.db, context.clock).get(LINK_BASE_URL_KEY)
        return None if stored is None or not stored.value else stored.value

    async def _load_batch(
        self, context: RunContext, channel_id: int, link_base: str | None
    ) -> _Batch | None:
        db, clock = context.db, context.clock
        channels = NotificationChannelRepository(db, clock)
        outbox = NotificationOutboxRepository(db, clock)
        rows = await outbox.due_rows(channel_id, limit=MAX_EVENTS_PER_MESSAGE)
        channel = await channels.get(channel_id)
        if channel is None or not rows:
            return None
        subscribed = await channels.subscriptions(channel_id)
        listed = (
            await channels.environment_ids(channel_id)
            if channel.environment_scope == "listed"
            else frozenset[int]()
        )
        found = await ActivityEventRepository(db, clock).by_ids([row.event_id for row in rows])
        events = {event.id: event for event in found}
        wanted: list[tuple[OutboxRow, ActivityEvent, EventType]] = []
        unwanted: list[int] = []
        attempts = rows[0].attempts
        for row in rows:
            if row.attempts != attempts:
                continue
            event, kind = events.get(row.event_id), event_type(row.event_type)
            scope = (
                None
                if event is None
                else event.environment_id
                if event.environment_id is not None
                else linked_environment(event.detail)
            )
            if (
                event is None
                or kind is None
                or not channel.is_enabled
                or row.event_type not in subscribed
                or (
                    scope is not None
                    and channel.environment_scope == "listed"
                    and scope not in listed
                )
            ):
                unwanted.append(row.id)
            else:
                wanted.append((row, event, kind))
        if unwanted:
            await outbox.delete(unwanted)
        if not wanted:
            return None
        named = {
            item.event.id: item for item in await export_rows(db, [event for _, event, _ in wanted])
        }
        return _Batch(
            channel=channel,
            rows=[row for row, _, _ in wanted],
            events=[
                _notification_event(named[event.id], kind, link_base) for _, event, kind in wanted
            ],
            target=self._target_of(channel),
        )

    def _target_of(self, channel: NotificationChannel) -> HttpTarget | SmtpTarget | None:
        try:
            return build_target(channel, open_secrets(self._deps.engine.vault, channel))
        except (SecretsUnreadable, KeyError, ValueError, TypeError):
            return None

    async def _deliver(
        self, batch: _Batch, http: HttpDeliverer, gate: asyncio.Semaphore, now: datetime
    ) -> None:
        if batch.target is None:
            batch.code = "invalid_target"
            return
        async with gate:
            try:
                await send_message(
                    batch.target,
                    batch.events,
                    channel_name=batch.channel.name,
                    http=http,
                    now=now,
                    resolver=self._resolver,
                    ssl_context=self._ssl_context,
                )
            except DeliveryError as failure:
                batch.code = failure.code
            except Exception as error:
                logger.error("notification_delivery_error", extra={"error": type(error).__name__})
                batch.code = "protocol"

    async def _settle(self, context: RunContext, batch: _Batch, tally: _Tally) -> None:
        db, clock = context.db, context.clock
        outbox = NotificationOutboxRepository(db, clock)
        channels = NotificationChannelRepository(db, clock)
        channel = batch.channel
        if batch.code is None:
            async with db.transaction():
                await outbox.delete([row.id for row in batch.rows])
                await channels.record_success(channel.id)
            tally.delivered += 1
            return
        ids = [row.id for row in batch.rows]
        tries = batch.rows[0].attempts
        if tries < len(RETRY_DELAYS):
            async with db.transaction():
                await outbox.defer(ids, clock() + RETRY_DELAYS[tries])
            tally.retried += 1
            return
        async with db.transaction():
            await outbox.delete(ids)
            await channels.record_failure(channel.id, message_for(batch.code))
            await context.activity.record_event(
                _actor_of(context),
                action=DELIVERY_FAILED_ACTION,
                target_type=TARGET_TYPE,
                target_id=str(channel.id),
                result="failure",
                detail={
                    "reason": batch.code,
                    "message": message_for(batch.code),
                    "name": channel.name,
                    "kind": channel.kind,
                    "events": len(ids),
                    "event_types": ",".join(sorted({row.event_type for row in batch.rows}))[
                        :MAX_EVENT_TYPES_TEXT
                    ],
                },
            )
        tally.failed += 1


def _actor_of(context: RunContext) -> SubsystemActor:
    run_id = context.actor.job_run_id
    return SubsystemActor(NAME, None, run_id if run_id is not None and run_id > 0 else None)


def _notification_event(
    row: ExportRow, kind: EventType, link_base: str | None
) -> NotificationEvent:
    event = row.event
    detail = event.detail or {}
    rule = detail.get("rule_name")
    name = detail.get("name")
    actor_type: ActorType
    if event.actor_type == "subsystem":
        actor_type, actor_name = "subsystem", event.actor_subsystem or "unknown"
    else:
        fallback = "anonymous" if event.actor_user_id is None else f"user {event.actor_user_id}"
        actor_type, actor_name = "user", row.actor_username or fallback
    return NotificationEvent(
        id=event.id,
        type=kind.key,
        label=kind.label,
        result=event.result,
        actor_type=actor_type,
        actor_name=actor_name,
        actor_rule=rule if isinstance(rule, str) and rule else None,
        target_type=event.target_type,
        target_name=name if isinstance(name, str) and name else (event.target_id or ""),
        environment_id=event.environment_id,
        environment_name=row.environment_name,
        time=parse_timestamp(event.occurred_at),
        link=None if link_base is None else f"{link_base}/activity/{event.id}",
    )
