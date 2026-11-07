"""Read side of the activity log, and the interface retention policies implement."""

import asyncio
import weakref
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol, Self

from app.auth.errors import ProductError, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.activity_event import (
    ActivityEvent,
    ActivityEventRepository,
    ActivityFilter,
)
from app.db.repositories.clocked import Clock, format_timestamp, utc_now
from app.db.repositories.pagination import Page
from app.db.repositories.user import UserRepository
from app.services.activity import SUBSYSTEMS
from app.services.activity_csv import ExportFormat, export_rows, resolve_names
from app.services.activity_ndjson import ExportEncoding, export_encoding
from app.services.guards import require_administrator

MAX_CONCURRENT_EXPORTS: Final = 2
EXPORT_PAGE_SIZE: Final = 500
STACK_PANEL_SCAN_ROWS: Final = 200
STACK_PANEL_SCAN_PAGES: Final = 5


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """What one retention pass did."""

    removed: int


class RetentionPolicy(Protocol):
    """Decides which old events to remove."""

    async def apply(self, now: datetime) -> RetentionResult:
        """Remove the events past retention at `now`."""
        ...


class NoRetention:
    """Keeps every event."""

    async def apply(self, now: datetime) -> RetentionResult:
        """Remove nothing."""
        return RetentionResult(removed=0)


@dataclass(frozen=True, slots=True)
class ActivityEventView:
    """An event with the current names of its actor and environment."""

    event: ActivityEvent
    actor_username: str | None
    environment_name: str | None


@dataclass(frozen=True, slots=True)
class RetentionStatus:
    """The retention setting and what an exporter currently holds back from it."""

    days: int | None
    held_since: str | None
    exporter_rules: int


type PanelKind = Literal["container", "stack", "environment"]

_PANEL_TARGET_TYPE: Final[dict[str, str]] = {"container": "container", "stack": "stack"}


class ExportBusy(ProductError):
    """Raised when the process is already serving the most exports it allows."""


class _ExportSlots:
    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._taken = 0

    def acquire(self) -> "_Slot":
        if self._taken >= self._capacity:
            raise ExportBusy("Too many exports are running. Try again shortly.")
        self._taken += 1
        return _Slot(self)

    def _release(self) -> None:
        self._taken -= 1


class _Slot:
    def __init__(self, owner: _ExportSlots) -> None:
        self._owner: _ExportSlots | None = owner

    def release(self) -> None:
        owner, self._owner = self._owner, None
        if owner is not None:
            owner._release()


_EXPORT_SLOTS = _ExportSlots(MAX_CONCURRENT_EXPORTS)


class ExportStream:
    """The chunks of one export; holds a slot until the stream ends or aclose is called."""

    def __init__(
        self, chunks: AsyncGenerator[bytes, None], slot: _Slot, encoding: ExportEncoding
    ) -> None:
        self._chunks = chunks
        self._slot = slot
        self.media_type = encoding.media_type
        self.suffix = encoding.suffix
        weakref.finalize(self, slot.release)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self._chunks.__anext__()
        except BaseException:
            self._slot.release()
            raise

    async def aclose(self) -> None:
        """Stop the export and free its slot; safe to call after the last chunk or twice."""
        try:
            await self._chunks.aclose()
        finally:
            self._slot.release()


class ActivityLogService:
    """Reads the activity log; administrators only, and every method checks that first."""

    def __init__(
        self, *, db: Session, clock: Clock = utc_now, retention_days: int | None = None
    ) -> None:
        self._db = db
        self._clock = clock
        self._retention_days = retention_days
        self._events = ActivityEventRepository(db, clock)
        self._users = UserRepository(db)

    async def user_id_of(self, actor: Principal, username: str) -> int | None:
        """Return the id of the user with this username, or None."""
        require_administrator(actor)
        user = await self._users.get_by_username(username)
        return None if user is None else user.id

    async def page(
        self, actor: Principal, criteria: ActivityFilter, *, limit: int, cursor: str | None = None
    ) -> Page[ActivityEventView]:
        """Return one page of events matching the criteria, newest first."""
        _require_reader(actor, criteria)
        page = await self._events.page(criteria, limit=limit, cursor=cursor)
        return Page(await self._views(page.items), page.next_cursor)

    async def exists(self, actor: Principal, event_id: int) -> None:
        """Return nothing for an event that is stored; ResourceNotFound otherwise."""
        require_administrator(actor)
        if await self._events.get(event_id) is None:
            raise ResourceNotFound("not found")

    async def get(self, actor: Principal, event_id: int) -> ActivityEventView:
        """Return one event; a missing one is not found."""
        require_administrator(actor)
        event = await self._events.get(event_id)
        if event is None:
            raise ResourceNotFound("not found")
        return (await self._views([event]))[0]

    async def for_object(
        self,
        actor: Principal,
        kind: PanelKind,
        environment_id: int,
        target_id: str,
        *,
        limit: int = 10,
    ) -> list[ActivityEventView]:
        """Return the newest events for a container, a stack or an environment.

        For an environment, target_id is ignored and the environment's own events are included.
        """
        require_administrator(actor)
        if kind == "stack":
            return await self._stack_events(environment_id, target_id, limit)
        criteria = ActivityFilter(environment_id=environment_id)
        if kind != "environment":
            criteria = ActivityFilter(
                environment_id=environment_id,
                target_type=_PANEL_TARGET_TYPE[kind],
                target_id=target_id,
            )
        page = await self._events.page(criteria, limit=limit)
        return await self._views(page.items)

    def export(
        self, actor: Principal, criteria: ActivityFilter, export_format: ExportFormat = "csv"
    ) -> ExportStream:
        """Return the matching events as CSV or NDJSON chunks, newest first.

        The caller's role, the filter and the process-wide cap on running exports are checked
        here, before any byte is produced; ExportBusy means the cap is reached. The caller must
        call aclose on the stream when it stops reading.
        """
        _require_reader(actor, criteria)
        slot = _EXPORT_SLOTS.acquire()
        encoding = export_encoding(export_format)
        return ExportStream(self._stream(criteria, encoding), slot, encoding)

    async def retention_status(self, actor: Principal) -> RetentionStatus:
        """Return the retention setting and, when an exporter holds rows past it, since when."""
        require_administrator(actor)
        exporters = await self._events.exporter_rule_count()
        held_since: str | None = None
        if self._retention_days is not None and exporters:
            watermark = await self._events.export_watermark()
            oldest = None if watermark is None else await self._events.first_after(watermark)
            cutoff = format_timestamp(self._clock() - timedelta(days=self._retention_days))
            if oldest is not None and oldest.occurred_at < cutoff:
                held_since = oldest.occurred_at
        return RetentionStatus(
            days=self._retention_days, held_since=held_since, exporter_rules=exporters
        )

    async def _views(self, events: Sequence[ActivityEvent]) -> list[ActivityEventView]:
        usernames, names = await resolve_names(self._db, events)
        return [
            ActivityEventView(
                event,
                None if event.actor_user_id is None else usernames.get(event.actor_user_id),
                None if event.environment_id is None else names.get(event.environment_id),
            )
            for event in events
        ]

    async def _stack_events(
        self, environment_id: int, name: str, limit: int
    ) -> list[ActivityEventView]:
        # Stack names are recorded as typed, so the match folds case; the scan is bounded.
        wanted = name.casefold()
        criteria = ActivityFilter(environment_id=environment_id, target_type="stack")
        found: list[ActivityEvent] = []
        cursor: str | None = None
        for _ in range(STACK_PANEL_SCAN_PAGES):
            page = await self._events.page(criteria, limit=STACK_PANEL_SCAN_ROWS, cursor=cursor)
            for event in page.items:
                if event.target_id is not None and event.target_id.casefold() == wanted:
                    found.append(event)
                    if len(found) == limit:
                        return await self._views(found)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        return await self._views(found)

    async def _stream(
        self, criteria: ActivityFilter, encoding: ExportEncoding
    ) -> AsyncGenerator[bytes, None]:
        header = encoding.header()
        if header:
            yield header
        cursor: str | None = None
        while True:
            page = await self._events.page(criteria, limit=EXPORT_PAGE_SIZE, cursor=cursor)
            if page.items:
                rows = await export_rows(self._db, page.items)
                yield await asyncio.to_thread(encoding.rows, rows)
            if page.next_cursor is None:
                return
            cursor = page.next_cursor


def _require_reader(actor: Principal, criteria: ActivityFilter) -> None:
    require_administrator(actor)
    if criteria.actor_subsystem is not None and criteria.actor_subsystem not in SUBSYSTEMS:
        raise ValidationFailed({"actor_subsystem": "Choose a known subsystem."})
