"""activity-log-exporter: copies the activity log to object storage as gzipped CSV or NDJSON."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar, Final

import httpx
from pydantic import Field

from app.auth.errors import PermissionDenied
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.activity_event import (
    MAX_EXPORT_COUNT,
    ActivityEvent,
    ActivityEventRepository,
    DetailValue,
)
from app.db.repositories.activity_export_cursor import ActivityExportCursorRepository
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.clocked import Clock
from app.db.repositories.errors import MissingReferenceError
from app.db.repositories.pagination import MAX_PAGE_SIZE, MAX_ROW_ID
from app.db.repositories.scheduled_job import OverrunPolicy, ScheduleKind
from app.logging import get_logger
from app.scheduler.model import RunFailed, RunResult
from app.services.activity_csv import ExportFormat, ExportRow, export_rows
from app.services.activity_ndjson import ExportEncoding, export_encoding
from app.services.authorisation import Authoriser
from app.services.object_storage import ObjectStorageClient, ObjectStorageTargets, activity_log_key
from app.subsystems.base import RuleParameters, RunContext, Subsystem, SubsystemDeps
from app.subsystems.object_export import (
    PREFIX_INVALID,
    RULE_MISSING,
    START_BUDGET,
    TARGET_MISSING,
    TARGET_TYPE,
    ExportStopped,
    connect_target,
    gzip_body,
    put_with_retry,
    record_stop,
    resolve_target,
)

logger = get_logger(__name__)

NAME: Final = "activity-log-exporter"
EXPORTED_ACTION: Final = "activity_log_exported"
ROWS_PER_OBJECT: Final = 20_000
MAX_OBJECT_BYTES: Final = 32 * 1024 * 1024
MAX_OBJECTS_PER_RUN: Final = 50
READ_ROWS: Final = 500
PENDING_COUNT_CAP: Final = 1_000_000
PROBE_TIME: Final = "2000-01-01T00:00:00Z"


class ActivityLogExporterParameters(RuleParameters):
    """Which object storage target receives the export, and in which format."""

    object_storage_target_id: int = Field(ge=1, le=MAX_ROW_ID)
    format: ExportFormat = "csv"


@dataclass(frozen=True, slots=True)
class _Batch:
    key: str
    body: bytes
    first_id: int
    last_id: int
    rows: int


@dataclass(slots=True)
class _Progress:
    rule_id: int
    rule_created_at: str
    target_id: int
    export_format: ExportFormat = "csv"
    target_name: str | None = None
    attempted: int = 0
    objects: int = 0
    rows: int = 0
    first_id: int | None = None
    last_id: int | None = None
    more_pending: bool = True


class ActivityLogExporter(Subsystem):
    """Copies events to the bucket in id order and moves a cursor after each stored object."""

    name = NAME
    parameters_model = ActivityLogExporterParameters
    event_policy = "failures_only"
    schedule_kinds: ClassVar[frozenset[ScheduleKind]] = frozenset({"cron", "interval"})
    min_interval_seconds = 3600
    overrun_policy = "skip"
    allowed_overrun_policies: ClassVar[frozenset[OverrunPolicy]] = frozenset({"skip"})
    cancellation_safe = True
    catch_up = False
    run_timeout = timedelta(minutes=30)
    failure_threshold = 5
    requires_environment = False
    supports_dry_run = True

    def __init__(
        self,
        deps: SubsystemDeps,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(deps)
        self._transport = transport
        self._sleep = sleep

    async def authorise_target(
        self,
        principal: Principal,
        environment_id: int | None,
        selector: str,
        authoriser: Authoriser,
    ) -> None:
        """Raise unless the principal is an administrator."""
        if not principal.is_administrator:
            raise PermissionDenied("insufficient access")

    async def run(self, context: RunContext) -> RunResult:
        """Export every event past the rule's cursor; a dry run only counts them."""
        rule, params = context.rule, context.parameters
        if rule is None or not isinstance(params, ActivityLogExporterParameters):
            raise RunFailed("parameters_invalid", "The automation's parameters are not valid.")
        progress = _Progress(
            rule_id=rule.id,
            rule_created_at=rule.created_at,
            target_id=params.object_storage_target_id,
            export_format=params.format,
        )
        cursors = ActivityExportCursorRepository(context.db, context.clock)
        stored = await cursors.get(rule.id)
        after = 0 if stored is None else stored.last_event_id
        if context.dry_run:
            return await self._forecast(context, progress, after)
        try:
            await self._export(context, progress, cursors, after)
        except ExportStopped as stopped:
            return await self._record_stop(context, progress, stopped)
        return RunResult("success", attempted=progress.attempted, detail=self._summary(progress))

    async def _forecast(self, context: RunContext, progress: _Progress, after: int) -> RunResult:
        targets = ObjectStorageTargets(context.db, self._deps.engine.vault)
        known = await targets.prefix_of(progress.target_id)
        if known is None:
            raise RunFailed("target_missing", TARGET_MISSING)
        events = ActivityEventRepository(context.db, context.clock)
        pending = await events.count_after(after, cap=PENDING_COUNT_CAP + 1)
        detail: dict[str, DetailValue] = {
            "target_id": progress.target_id,
            "pending_rows": min(pending, PENDING_COUNT_CAP),
            "pending_rows_capped": pending > PENDING_COUNT_CAP,
        }
        first = await events.first_after(after)
        try:
            if first is not None:
                detail["next_object_path"] = activity_log_key(
                    known[1],
                    progress.rule_id,
                    progress.rule_created_at,
                    first.id,
                    first.occurred_at,
                    progress.export_format,
                )
            else:
                activity_log_key(
                    known[1],
                    progress.rule_id,
                    progress.rule_created_at,
                    1,
                    PROBE_TIME,
                    progress.export_format,
                )
        except ValueError:
            raise RunFailed("prefix_invalid", PREFIX_INVALID) from None
        return RunResult("success", detail=detail)

    async def _export(
        self,
        context: RunContext,
        progress: _Progress,
        cursors: ActivityExportCursorRepository,
        after: int,
    ) -> None:
        targets = ObjectStorageTargets(context.db, self._deps.engine.vault)
        progress.target_name, prefix = await resolve_target(targets, progress.target_id)
        try:
            activity_log_key(
                prefix,
                progress.rule_id,
                progress.rule_created_at,
                1,
                PROBE_TIME,
                progress.export_format,
            )
        except ValueError:
            raise ExportStopped("prefix_invalid", PREFIX_INVALID) from None
        client = await connect_target(
            targets, progress.target_id, transport=self._transport, clock=context.clock
        )
        events = ActivityEventRepository(context.db, context.clock)
        started = context.clock()
        last = after
        exhausted = False
        async with client:
            for _ in range(MAX_OBJECTS_PER_RUN):
                if context.clock() - started > START_BUDGET:
                    break
                batch = await self._read_batch(context, events, progress, prefix, last)
                if batch is None:
                    exhausted = True
                    break
                progress.attempted += 1
                await self._store(client, batch)
                await self._move_cursor(context, cursors, progress.rule_id, batch)
                last = batch.last_id
                progress.objects += 1
                progress.rows += batch.rows
                if progress.first_id is None:
                    progress.first_id = batch.first_id
                progress.last_id = batch.last_id
                logger.info(
                    "activity_log_object_exported",
                    extra={
                        "rule_id": progress.rule_id,
                        "rows": batch.rows,
                        "object_path": batch.key,
                    },
                )
        progress.more_pending = not exhausted and await events.first_after(last) is not None

    async def _read_batch(
        self,
        context: RunContext,
        events: ActivityEventRepository,
        progress: _Progress,
        prefix: str,
        after: int,
    ) -> _Batch | None:
        encoding = export_encoding(progress.export_format)
        pieces = [encoding.header()]
        size = len(pieces[0])
        rows = 0
        first: ActivityEvent | None = None
        last = after
        while rows < ROWS_PER_OBJECT:
            wanted = min(READ_ROWS, ROWS_PER_OBJECT - rows)
            chunk = await events.after_id(last, limit=wanted)
            if not chunk:
                break
            export = await export_rows(context.db, chunk)
            encoded = await asyncio.to_thread(encoding.rows, export)
            full = size + len(encoded) > MAX_OBJECT_BYTES
            if full:
                fit = await asyncio.to_thread(
                    _rows_within, encoding, export, MAX_OBJECT_BYTES - size, rows
                )
                if fit == 0:
                    break
                chunk = chunk[:fit]
                encoded = await asyncio.to_thread(encoding.rows, export[:fit])
            pieces.append(encoded)
            size += len(encoded)
            rows += len(chunk)
            first = chunk[0] if first is None else first
            last = chunk[-1].id
            if full or len(chunk) < wanted:
                break
        if first is None:
            return None
        key = activity_log_key(
            prefix,
            progress.rule_id,
            progress.rule_created_at,
            first.id,
            first.occurred_at,
            progress.export_format,
        )
        body = await asyncio.to_thread(gzip_body, pieces)
        return _Batch(key=key, body=body, first_id=first.id, last_id=last, rows=rows)

    async def _store(self, client: ObjectStorageClient, batch: _Batch) -> None:
        failure = await put_with_retry(client, batch.key, batch.body, sleep=self._sleep)
        if failure is not None:
            raise ExportStopped(
                failure.kind, failure.message, failure=failure, detail=_batch_detail(batch)
            )

    async def _move_cursor(
        self,
        context: RunContext,
        cursors: ActivityExportCursorRepository,
        rule_id: int,
        batch: _Batch,
    ) -> None:
        try:
            async with context.db.transaction():
                await cursors.move_export_cursor(
                    rule_id, last_event_id=batch.last_id, object_key=batch.key
                )
        except MissingReferenceError:
            raise ExportStopped("rule_missing", RULE_MISSING, detail=_batch_detail(batch)) from None

    async def _record_stop(
        self, context: RunContext, progress: _Progress, stopped: ExportStopped
    ) -> RunResult:
        detail: dict[str, DetailValue] = {"objects_exported": progress.objects}
        if progress.target_name is not None:
            detail["target_name"] = progress.target_name
        detail.update(stopped.detail)
        await record_stop(
            context,
            action=EXPORTED_ACTION,
            target_type=TARGET_TYPE,
            target_id=str(progress.target_id),
            stopped=stopped,
            detail=detail,
        )
        return RunResult(
            "partial" if progress.objects else "failure",
            attempted=progress.attempted,
            reason=stopped.reason,
            message=stopped.message,
            detail=self._summary(progress),
        )

    @staticmethod
    def _summary(progress: _Progress) -> dict[str, DetailValue]:
        detail: dict[str, DetailValue] = {
            "target_id": progress.target_id,
            "objects": progress.objects,
            "rows": progress.rows,
            "more_pending": progress.more_pending,
        }
        if progress.first_id is not None:
            detail["first_id"] = progress.first_id
        if progress.last_id is not None:
            detail["last_id"] = progress.last_id
        return detail


def _rows_within(
    encoding: ExportEncoding, rows: Sequence[ExportRow], budget: int, already: int
) -> int:
    """Count the leading rows whose encoding fits the budget; an empty object takes at least one."""
    used = 0
    count = 0
    for row in rows:
        used += len(encoding.rows([row]))
        if used > budget and (count or already):
            break
        count += 1
    return count


def _batch_detail(batch: _Batch) -> dict[str, DetailValue]:
    return {
        "object_path": batch.key,
        "first_id": batch.first_id,
        "last_id": batch.last_id,
        "rows": batch.rows,
    }


@dataclass(frozen=True, slots=True)
class ExportRelease:
    """How many events stop being held back from retention when an exporter rule is deleted.

    rows is counted up to MAX_EXPORT_COUNT; more_than says the true figure is above it.
    """

    rows: int
    more_than: bool


async def export_release(db: Session, clock: Clock, rule_id: int) -> ExportRelease:
    """Count the events a rule's deletion hands to retention.

    The hold is the lowest cursor over all exporter rules, so deleting a rule releases the events
    between its cursor and the next lowest, or every later event when it is the only rule.
    """
    cursors = ActivityExportCursorRepository(db, clock)
    own = await cursors.get(rule_id)
    floor = 0 if own is None else own.last_event_id
    others: list[int] = []
    cursor: str | None = None
    while True:
        page = await AutomationRuleRepository(db, clock).page(
            limit=MAX_PAGE_SIZE, cursor=cursor, subsystem=NAME
        )
        for other in page.items:
            if other.id != rule_id:
                stored = await cursors.get(other.id)
                others.append(0 if stored is None else stored.last_event_id)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    ceiling = min(others) if others else None
    if ceiling is not None and ceiling <= floor:
        return ExportRelease(rows=0, more_than=False)
    return await _rows_between(ActivityEventRepository(db, clock), floor, ceiling)


async def _rows_between(
    events: ActivityEventRepository, floor: int, ceiling: int | None
) -> ExportRelease:
    cap = MAX_EXPORT_COUNT + 1
    if ceiling is None:
        counted = await events.count_after(floor, cap=cap)
    else:
        counted = await events.count_between(floor, ceiling, cap=cap)
    return ExportRelease(min(counted, MAX_EXPORT_COUNT), counted > MAX_EXPORT_COUNT)
