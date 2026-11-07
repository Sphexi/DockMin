"""container-log-exporter: copies container logs to object storage as gzipped NDJSON."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import ClassVar, Final

import anyio
import httpx
from pydantic import Field

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.automation_rule import AutomationRule
from app.db.repositories.container_log_cursor import ContainerLogCursorRepository, LogCursor
from app.db.repositories.errors import MissingReferenceError
from app.db.repositories.pagination import MAX_ROW_ID
from app.db.repositories.scheduled_job import OverrunPolicy, ScheduleKind
from app.db.repositories.service_log_cursor import (
    NO_TASK,
    ServiceLogCursor,
    ServiceLogCursorRepository,
)
from app.db.repositories.stack import StackKind, StackRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, NotFound
from app.docker_engine.frames import LogFrame
from app.docker_engine.models import ContainerSummary, SwarmService, SwarmTask
from app.docker_engine.resources import format_engine_timestamp
from app.scheduler.model import RunFailed, RunResult
from app.services.access import (
    COMPOSE_PROJECT_LABEL,
    SWARM_NAMESPACE_LABEL,
    canonical_stack_key,
    is_canonical_id,
)
from app.services.assembly import build_host_clients
from app.services.authorisation import Authoriser
from app.services.container_logs import (
    ContainerLogSource,
    FrameReader,
    FrameRequest,
    ReadTimeout,
    read_log_context,
)
from app.services.containers import display_name
from app.services.host_objects import read_services
from app.services.log_lines import LogLine, LogLineSplitter, parse_timestamp_ns
from app.services.log_ndjson import (
    LogContext,
    SourceKeys,
    container_source,
    encode_log_line,
    for_task,
    service_image,
    service_source,
)
from app.services.object_inspector import (
    EnvironmentUnavailable,
    ObjectTooLarge,
    swarm_unavailable_from,
    unavailable_from,
)
from app.services.object_locks import ObjectKey
from app.services.object_storage import (
    NANOS_PER_SECOND,
    ObjectStorageClient,
    ObjectStorageTargets,
    StorageFailure,
    container_log_folder,
    container_log_key,
    service_log_folder,
    service_log_key,
)
from app.services.service_log_lines import split_details
from app.services.stack_deploy import in_project
from app.subsystems.acting_run import ObjectTally, check_suppressed, failure_reason
from app.subsystems.base import (
    RuleParameters,
    RunContext,
    Subsystem,
    SubsystemDeps,
    environment_outcome,
)
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
from app.subsystems.run_services import RunServices, run_services
from app.subsystems.selectors import (
    CONTAINER_KINDS,
    NOT_SWARM,
    SELECTOR_FIELD,
    ContainerTarget,
    EnvironmentTarget,
    LabelsTarget,
    Selector,
    ServiceLabelsTarget,
    ServiceTarget,
    StackTarget,
    canonical,
    carries,
    find_containers,
    find_service,
    matches,
    parse_selector_text,
    term_text,
)

NAME: Final = "container-log-exporter"
EXPORTED_ACTION: Final = "container_log_exported"
CONTAINER_TYPE: Final = "container"
SERVICE_TYPE: Final = "service"
MAX_CONTAINERS_PER_RUN: Final = 50
MAX_SERVICES_PER_RUN: Final = 20
MAX_OBJECTS_PER_RUN: Final = 100
TERMINAL_TASK_STATES: Final = frozenset(
    {"complete", "failed", "shutdown", "rejected", "orphaned", "remove"}
)
MAX_LINES_PER_OBJECT: Final = 100_000
MAX_OBJECT_BYTES: Final = 32 * 1024 * 1024
READ_SECONDS: Final = 120.0
SEEN_REFRESH: Final = timedelta(hours=1)
ENCODE_IN_THREAD_LINES: Final = 256
PROBE_CONTAINER_ID: Final = "0" * 64
PROBE_SERVICE_ID: Final = "s" * 25

TARGET_NOT_FOUND: Final = "The automation's target was not found."
LOGS_UNAVAILABLE: Final = "The container's logs could not be read."
NOT_PERMITTED: Final = "The automation's owner may not read this container."
NOT_READABLE: Final = "logs_not_readable"
NO_LOG_DRIVER: Final = "none"
NOT_IMPLEMENTED: Final = 501
KEY_INVALID: Final = "The object path for this container's logs is not valid."
SERVICE_KEY_INVALID: Final = "The object path for this service's logs is not valid."
SERVICE_LOGS_UNAVAILABLE: Final = "The service's logs could not be read."
TASKS_UNAVAILABLE: Final = "The service's tasks could not be listed."
NO_TASK_LABEL: Final = "no-task"


def _created_ns(created: str, fallback_seconds: int) -> int:
    parsed = parse_timestamp_ns(f"{created} ")
    return fallback_seconds * NANOS_PER_SECOND if parsed is None else parsed


def _position(cursor: ServiceLogCursor | None) -> ServiceLogCursor | None:
    """The cursor when it records copied lines, None for a row that only marks a task seen."""
    return cursor if cursor is not None and cursor.object_path is not None else None


def _log_type(host_config: Mapping[str, object]) -> object:
    config = host_config.get("LogConfig")
    return config.get("Type") if isinstance(config, dict) else None


def _combined(
    containers: Mapping[str, DetailValue], services: Mapping[str, DetailValue]
) -> dict[str, DetailValue]:
    merged = {**containers, **services}
    for key in ("with_cursor", "without_cursor"):
        first, second = containers[key], services[key]
        if isinstance(first, int) and isinstance(second, int):
            merged[key] = first + second
    if "next_object_path" in containers:
        merged["next_object_path"] = containers["next_object_path"]
    return merged


class ContainerLogExporterParameters(RuleParameters):
    """Which object storage target receives the container logs."""

    object_storage_target_id: int = Field(ge=1, le=MAX_ROW_ID)


class _ObjectStopped(ExportStopped):
    def __init__(
        self,
        reason: str,
        message: str,
        target_type: str,
        target_id: str,
        *,
        failure: StorageFailure | None = None,
        detail: Mapping[str, DetailValue] | None = None,
    ) -> None:
        super().__init__(reason, message, failure=failure, detail=detail)
        self.target_type = target_type
        self.target_id = target_id


@dataclass(frozen=True, slots=True)
class _Candidate:
    row: ContainerSummary
    name: str
    cursor: LogCursor | None
    origin: str | None

    @property
    def position(self) -> LogCursor | None:
        """The cursor when it records copied lines, None for a row that only marks an exam."""
        cursor = self.cursor
        return cursor if cursor is not None and cursor.object_path is not None else None


@dataclass(frozen=True, slots=True)
class _ServiceCandidate:
    name: str
    service: SwarmService
    seen: str | None
    origin: str | None


@dataclass(frozen=True, slots=True)
class _TaskStart:
    """Where a task's lines resume: `skip` lines stamped exactly `start_ns` were copied before."""

    start_ns: int
    skip: int
    stored: bool


@dataclass(frozen=True, slots=True)
class _ServicePlan:
    """A service's tasks and where the next read of each begins.

    `reading` holds the tasks read together from the service's log; `terminal` holds the ended
    tasks not yet read to their end, each read from its own log.
    """

    service: SwarmService
    tasks: list[SwarmTask]
    starts: dict[str, int]
    reading: dict[str, _TaskStart]
    ignored: frozenset[str]
    terminal: dict[str, _TaskStart] = field(default_factory=dict)

    @property
    def lower_ns(self) -> int | None:
        """The earliest position the service read resumes from.

        Lines that named no task bound the read only when no live task does.
        """
        live = [start.start_ns for task_id, start in self.reading.items() if task_id != NO_TASK]
        if live:
            return min(live)
        loose = self.reading.get(NO_TASK)
        return loose.start_ns if loose is not None else None

    @property
    def readable(self) -> dict[str, _TaskStart]:
        """Every entry a run would read, the service read's first."""
        return {**self.reading, **self.terminal}


@dataclass(slots=True)
class _Matched:
    rows: list[ContainerSummary] = field(default_factory=list)
    origin: dict[str, str] = field(default_factory=dict)
    resolved: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Run:
    context: RunContext
    rule: AutomationRule
    environment_id: int
    target_id: int
    selector: Selector
    engine: EngineClient
    services: RunServices
    tally: ObjectTally
    cursors: ContainerLogCursorRepository
    service_cursors: ServiceLogCursorRepository
    log_context: LogContext
    prefix: str = ""
    target_name: str | None = None
    client: ObjectStorageClient | None = None
    attempted: int = 0
    objects: int = 0
    lines: int = 0
    pending: bool = False
    rotating: bool = False
    seen: dict[str, int] = field(default_factory=dict)
    services_read: int = 0
    tasks_listed: int = 0
    unlisted_lines: int = 0
    matched_services: int = 0
    compose_stacks: list[str] = field(default_factory=list)
    swarm_stacks: list[str] = field(default_factory=list)

    @property
    def explicit(self) -> bool:
        return isinstance(self.selector, ContainerTarget | StackTarget | ServiceTarget)

    @property
    def serving(self) -> bool:
        """Whether the run reads services; a stack target does when it names a Swarm stack."""
        match self.selector:
            case ServiceTarget() | ServiceLabelsTarget():
                return True
            case StackTarget():
                return bool(self.swarm_stacks)
            case _:
                return False

    @property
    def containering(self) -> bool:
        """Whether the run reads containers; a stack target does when it names a Compose stack."""
        match self.selector:
            case ServiceTarget() | ServiceLabelsTarget():
                return False
            case StackTarget():
                return bool(self.compose_stacks)
            case _:
                return True


class _Budget:
    """The lines and bytes that one object, or one service's read, may still take."""

    def __init__(self) -> None:
        self._lines = 0
        self._size = 0

    def admits(self, size: int) -> bool:
        return self._lines < MAX_LINES_PER_OBJECT and self._size + size <= MAX_OBJECT_BYTES

    def take(self, size: int) -> None:
        self._lines += 1
        self._size += size


class _Collector:
    """Encodes the lines past the cursor, up to the caps, and tracks where they end."""

    def __init__(
        self,
        start_ns: int,
        skip: int,
        context: LogContext,
        source: SourceKeys,
        budget: _Budget | None = None,
    ) -> None:
        self.start_ns = start_ns
        self._skip_left = skip
        self._context = context
        self._source = source
        self._budget = budget or _Budget()
        self.pieces: list[bytes] = []
        self.lines = 0
        self.first_ns: int | None = None
        self.max_ns: int | None = None
        self.at_max = 0
        self.capped = False

    def add(self, lines: Sequence[LogLine]) -> bool:
        """Take the lines in order; return True once a cap stops the read.

        The first `skip` lines stamped exactly `start_ns` were copied before, wherever they sit.
        """
        for line in lines:
            stamp = line.timestamp_ns
            if stamp is not None:
                if stamp < self.start_ns:
                    continue
                if stamp == self.start_ns and self._skip_left:
                    self._skip_left -= 1
                    continue
            encoded = encode_log_line(line, self._context, self._source)
            if not self._budget.admits(len(encoded)):
                self.capped = True
                return True
            self._budget.take(len(encoded))
            self.pieces.append(encoded)
            self.lines += 1
            if stamp is None:
                continue
            if self.first_ns is None:
                self.first_ns = stamp
            if self.max_ns is None or stamp > self.max_ns:
                self.max_ns = stamp
                self.at_max = 1
            elif stamp == self.max_ns:
                self.at_max += 1
        return False


class _ServiceRead:
    """Routes a service's lines to one collector per task and counts the lines of unknown tasks."""

    def __init__(
        self,
        plan: _ServicePlan,
        context: LogContext,
        source: SourceKeys,
        only: str | None = None,
    ) -> None:
        self._plan = plan
        self._context = context
        self._source = source
        self._only = only
        self.budget = _Budget()
        self.collectors: dict[str, _Collector] = {}
        self.unlisted = 0
        self.capped = False

    def add(self, lines: Sequence[LogLine]) -> bool:
        """Take the lines in order; return True once a cap stops the read."""
        plan = self._plan
        for line in lines:
            item = split_details(line)
            task_id = self._only or item.task_id or NO_TASK
            if task_id in plan.ignored or task_id in plan.terminal:
                continue
            collector = self.collectors.get(task_id)
            if collector is None:
                start = plan.reading.get(task_id)
                if start is None:
                    if task_id != NO_TASK:
                        self.unlisted += 1
                        continue
                    start = _TaskStart(plan.lower_ns or 0, 0, False)
                collector = _Collector(
                    start.start_ns,
                    start.skip,
                    self._context,
                    for_task(self._source, self._only or item.task_id, item.node_id),
                    self.budget,
                )
                self.collectors[task_id] = collector
            if collector.add([item.line]):
                self.capped = True
                return True
        return False


class ContainerLogExporter(Subsystem):
    """Copies each matched container's or service task's new log lines to the bucket.

    A cursor moves after each stored object.
    """

    name = NAME
    parameters_model = ContainerLogExporterParameters
    event_policy = "failures_only"
    schedule_kinds: ClassVar[frozenset[ScheduleKind]] = frozenset({"cron", "interval"})
    min_interval_seconds = 900
    overrun_policy = "skip"
    allowed_overrun_policies: ClassVar[frozenset[OverrunPolicy]] = frozenset({"skip"})
    cancellation_safe = True
    catch_up = False
    run_timeout = timedelta(minutes=30)
    failure_threshold = 5
    requires_environment = True
    administrators_only = True
    supports_dry_run = True
    target_kinds = CONTAINER_KINDS | {"service", "service_labels"}
    stack_kinds: ClassVar[frozenset[StackKind]] = frozenset({"compose", "swarm"})

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

    def parse_selector(self, text: str) -> str:
        """Validate the rule's target and return it in canonical form."""
        return canonical(parse_selector_text(text, self.target_kinds))

    async def authorise_target(
        self,
        principal: Principal,
        environment_id: int | None,
        selector: str,
        authoriser: Authoriser,
    ) -> None:
        """Raise unless the principal is an administrator and Swarm targets are in a manager."""
        if not principal.is_administrator:
            raise PermissionDenied("insufficient access")
        try:
            parsed = parse_selector_text(selector, self.target_kinds)
        except ValidationFailed:
            return
        if not isinstance(parsed, ServiceTarget | ServiceLabelsTarget | StackTarget):
            return
        if environment_id is None:
            if isinstance(parsed, StackTarget):
                return
            raise ValidationFailed({"environment_id": "Choose an environment."})
        async with self._deps.database.session() as db:
            hosts = build_host_clients(db, self._deps.auth, self._deps.engine)
            if isinstance(parsed, StackTarget):
                stacks = StackRepository(db, self._deps.engine.vault, self._deps.auth.clock)
                rows = [await stacks.get_folded(environment_id, name) for name in parsed.names]
                if not any(row is not None and row.kind == "swarm" for row in rows):
                    return
            if (await hosts.environment(environment_id)).type != "swarm":
                raise ValidationFailed({SELECTOR_FIELD: NOT_SWARM})

    async def run(self, context: RunContext) -> RunResult:
        """Copy the new log lines of each matched container or task; a dry run only lists them."""
        try:
            return await self._run(context)
        except RunFailed as failed:
            if not context.dry_run:
                await self._record_failed(context, failed)
            raise

    async def _record_failed(self, context: RunContext, failed: RunFailed) -> None:
        params = context.parameters
        target_id = (
            str(params.object_storage_target_id)
            if isinstance(params, ContainerLogExporterParameters)
            else None
        )
        await context.activity.record_failure(
            context.actor,
            action=EXPORTED_ACTION,
            target_type=TARGET_TYPE,
            target_id=target_id,
            environment_id=context.rule.environment_id if context.rule is not None else None,
            reason=failed.reason,
            message=failed.message,
        )

    async def _run(self, context: RunContext) -> RunResult:
        rule, environment_id = context.rule_scope()
        params = context.parameters
        if not isinstance(params, ContainerLogExporterParameters):
            raise RunFailed("parameters_invalid", "The automation's parameters are not valid.")
        try:
            selector = parse_selector_text(rule.target_selector, self.target_kinds)
        except ValidationFailed:
            raise RunFailed("selector_invalid", "The automation's target is not valid.") from None
        services = run_services(context, self._deps, stacks=False)
        try:
            engine = await services.hosts.client(environment_id)
            if (
                isinstance(selector, ServiceTarget | ServiceLabelsTarget)
                and (await services.hosts.environment(environment_id)).type != "swarm"
            ):
                raise RunFailed("target_missing", TARGET_NOT_FOUND)
        except EnvironmentUnavailable as error:
            raise environment_outcome(error) from None
        run = _Run(
            context=context,
            rule=rule,
            environment_id=environment_id,
            target_id=params.object_storage_target_id,
            selector=selector,
            engine=engine,
            services=services,
            tally=ObjectTally(done_name="exported", plan_name="would_export"),
            cursors=ContainerLogCursorRepository(context.db, context.clock),
            service_cursors=ServiceLogCursorRepository(context.db, context.clock),
            log_context=await read_log_context(
                services.hosts, environment_id, self._deps.settings.instance_name
            ),
        )
        if isinstance(selector, StackTarget):
            await self._resolve_stacks(run)
        targets = ObjectStorageTargets(context.db, self._deps.engine.vault)
        if context.dry_run:
            return await self._forecast_all(run, targets)
        try:
            if run.containering:
                await self._export(run, targets)
            if run.serving:
                await self._export_services(run, targets)
        except ExportStopped as stopped:
            return await self._record_stop(run, stopped)
        return self._summary(run)

    async def _resolve_stacks(self, run: _Run) -> None:
        """Sort the visible named stacks into Compose and Swarm; none visible fails the run."""
        context, selector, tally = run.context, run.selector, run.tally
        if not isinstance(selector, StackTarget):
            return
        stacks = StackRepository(context.db, self._deps.engine.vault, context.clock)
        missing: set[str] = set()
        for name in selector.names:
            stack = await stacks.get_folded(run.environment_id, name)
            if stack is None:
                missing.add(name)
                continue
            key = ObjectKey(run.environment_id, "stack", canonical_stack_key(stack.name))
            if await context.standing(key) == "hidden":
                missing.add(name)
            elif stack.kind == "swarm":
                run.swarm_stacks.append(name)
            else:
                run.compose_stacks.append(name)
        if not run.compose_stacks and not run.swarm_stacks:
            raise RunFailed("target_missing", TARGET_NOT_FOUND)
        tally.track_targets()
        for name in selector.names:
            if name in missing:
                tally.missing(name)
            else:
                tally.resolved(name)
        if run.swarm_stacks:
            try:
                environment = await run.services.hosts.environment(run.environment_id)
            except EnvironmentUnavailable as error:
                raise environment_outcome(error) from None
            if environment.type != "swarm":
                raise RunFailed("target_missing", TARGET_NOT_FOUND)

    async def _forecast_all(self, run: _Run, targets: ObjectStorageTargets) -> RunResult:
        extra: dict[str, DetailValue] = {}
        if run.containering:
            extra = await self._forecast(run, targets)
        if run.serving:
            services = await self._forecast_services(run, targets)
            extra = _combined(extra, services) if extra else services
        return run.tally.result(dry_run=True, extra=extra)

    async def _forecast(self, run: _Run, targets: ObjectStorageTargets) -> dict[str, DetailValue]:
        known = await targets.prefix_of(run.target_id)
        if known is None:
            raise RunFailed("target_missing", TARGET_MISSING)
        run.prefix = known[1]
        if not self._prefix_is_valid(run):
            raise RunFailed("prefix_invalid", PREFIX_INVALID)
        ordered, left = await self._candidates(run)
        planned: list[_Candidate] = []
        for candidate in ordered:
            if await self._cleared(run, candidate):
                planned.append(candidate)
                run.tally.record("done", attempted=False, name=candidate.name)
        self._defer(run, [each.origin for each in left])
        extra: dict[str, DetailValue] = {
            "target_id": run.target_id,
            "with_cursor": sum(1 for each in planned if each.position is not None),
            "without_cursor": sum(1 for each in planned if each.position is None),
            "more_pending": run.pending,
        }
        if planned:
            first = planned[0]
            extra["next_object_path"] = f"{self._folder(run, first.name, first.row.id)}/"
        return extra

    async def _export(self, run: _Run, targets: ObjectStorageTargets) -> None:
        context = run.context
        run.target_name, run.prefix = await resolve_target(targets, run.target_id)
        if not self._prefix_is_valid(run):
            raise ExportStopped("prefix_invalid", PREFIX_INVALID)
        ordered, left = await self._candidates(run)
        client = await connect_target(
            targets, run.target_id, transport=self._transport, clock=context.clock
        )
        started = context.clock()
        async with client:
            run.client = client
            for index, candidate in enumerate(ordered):
                if context.clock() - started > START_BUDGET:
                    left = [*ordered[index:], *left]
                    break
                await self._export_container(run, candidate)
        self._defer(run, [each.origin for each in left])
        await self._settle_cursors(run, rotating=bool(left))

    async def _export_container(self, run: _Run, candidate: _Candidate) -> None:
        tally, row, name = run.tally, candidate.row, candidate.name
        if not await self._cleared(run, candidate):
            return
        key = ObjectKey(run.environment_id, CONTAINER_TYPE, row.id)
        try:
            inspected = await run.services.inspector.inspect(run.environment_id, row.id)
        except ResourceNotFound:
            tally.record("skipped", attempted=False, reason="gone")
            return
        except EnvironmentUnavailable as error:
            raise environment_outcome(error) from None
        except ObjectTooLarge as error:
            await self._fail(run, key, name, failure_reason(error), LOGS_UNAVAILABLE)
            return
        position = candidate.position
        source = container_source(
            container_id=row.id,
            container_name=name,
            image=inspected.config.image,
            labels=inspected.config.labels,
        )
        start_ns = (
            position.last_timestamp_ns
            if position is not None
            else _created_ns(inspected.created, row.created)
        )
        run.seen[row.id] = start_ns
        if _log_type(inspected.host_config) == NO_LOG_DRIVER:
            tally.record("skipped", attempted=False, reason=NOT_READABLE)
            return
        collector = _Collector(
            start_ns, position.lines_at_last if position else 0, run.log_context, source
        )
        if not await self._collect(run, key, name, inspected.config.tty, collector):
            return
        run.pending = run.pending or collector.capped
        if collector.first_ns is None:
            if collector.lines:
                reason = "no_timestamps"
            else:
                reason = "timed_out" if collector.capped else "up_to_date"
            tally.record("skipped", attempted=False, reason=reason)
            return
        await self._store(run, key, candidate, collector)

    async def _collect(
        self, run: _Run, key: ObjectKey, name: str, tty: bool, collector: _Collector
    ) -> bool:
        """Read the container's log into the collector; False when the read failed."""
        source = ContainerLogSource(run.engine, key.object_id, tty=tty)
        reader = FrameReader(
            source.frames(
                FrameRequest(
                    follow=False,
                    stdout=True,
                    stderr=True,
                    since_ns=collector.start_ns,
                )
            )
        )
        splitter = LogLineSplitter()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READ_SECONDS
        try:
            while True:
                frame = await reader.next(deadline - loop.time())
                if isinstance(frame, ReadTimeout):
                    collector.capped = True
                    return True
                lines = splitter.flush() if frame is None else await splitter.split([frame])
                if len(lines) > ENCODE_IN_THREAD_LINES:
                    stopped = await asyncio.to_thread(collector.add, lines)
                else:
                    stopped = collector.add(lines)
                if stopped or frame is None:
                    return True
        except EngineError as error:
            if error.status_code == NOT_IMPLEMENTED:
                run.tally.record("skipped", attempted=False, reason=NOT_READABLE)
            else:
                await self._fail(run, key, name, "logs_unavailable", LOGS_UNAVAILABLE)
            return False
        finally:
            with anyio.CancelScope(shield=True):
                await reader.aclose()

    async def _store(
        self, run: _Run, key: ObjectKey, candidate: _Candidate, collector: _Collector
    ) -> None:
        rule, row, position = run.rule, candidate.row, candidate.position
        first_ns, max_ns = collector.first_ns, collector.max_ns
        if first_ns is None or max_ns is None or run.client is None:
            raise RuntimeError("a stored object needs a timestamped line and a storage client")
        continued_after = (
            position.lines_at_last
            if position is not None and first_ns == position.last_timestamp_ns
            else 0
        )
        try:
            path = container_log_key(
                run.prefix,
                rule.id,
                rule.created_at,
                run.environment_id,
                candidate.name,
                row.id,
                first_ns,
                continued_after,
            )
        except ValueError:
            await self._fail(run, key, candidate.name, "key_invalid", KEY_INVALID)
            return
        detail: dict[str, DetailValue] = {"object_path": path, "lines": collector.lines}
        failure = await self._upload(run, run.client, path, collector)
        if failure is not None:
            raise _ObjectStopped(
                failure.kind,
                failure.message,
                CONTAINER_TYPE,
                row.id,
                failure=failure,
                detail=detail,
            )
        carried = (
            position.lines_at_last
            if position is not None and max_ns == position.last_timestamp_ns
            else 0
        )
        try:
            async with run.context.db.transaction():
                await run.cursors.store_position(
                    rule.id,
                    rule.created_at,
                    run.environment_id,
                    row.id,
                    last_timestamp_ns=max_ns,
                    lines_at_last=carried + collector.at_max,
                    object_path=path,
                )
        except MissingReferenceError:
            raise _ObjectStopped(
                "rule_missing", RULE_MISSING, CONTAINER_TYPE, row.id, detail=detail
            ) from None
        run.objects += 1
        run.lines += collector.lines
        run.tally.record("done", attempted=False)

    async def _upload(
        self, run: _Run, client: ObjectStorageClient, path: str, collector: _Collector
    ) -> StorageFailure | None:
        body = await asyncio.to_thread(gzip_body, collector.pieces)
        run.attempted += 1
        return await put_with_retry(client, path, body, sleep=self._sleep)

    async def _cleared(self, run: _Run, candidate: _Candidate) -> bool:
        """Report whether the container may be exported now, recording why not in the tally."""
        row, name = candidate.row, candidate.name
        run.tally.begin(candidate.origin)
        if not is_canonical_id(CONTAINER_TYPE, row.id):
            run.tally.record("skipped", attempted=False, reason="unaddressable")
            return False
        key = ObjectKey(run.environment_id, CONTAINER_TYPE, row.id)
        if not await self._permitted(run, key, name):
            return False
        return not await check_suppressed(
            run.context, key, single=False, tally=run.tally, name=name
        )

    async def _permitted(self, run: _Run, key: ObjectKey, name: str) -> bool:
        try:
            await run.context.authorise_object(key)
        except (PermissionDenied, ResourceNotFound):
            if run.explicit:
                await self._fail(run, key, name, "permission_denied", NOT_PERMITTED)
            else:
                run.tally.record("not_permitted", attempted=False)
            return False
        except ObjectTooLarge as error:
            message = (
                SERVICE_LOGS_UNAVAILABLE if key.object_type == SERVICE_TYPE else LOGS_UNAVAILABLE
            )
            await self._fail(run, key, name, failure_reason(error), message)
            return False
        except EnvironmentUnavailable as error:
            raise environment_outcome(error) from None
        return True

    async def _fail(
        self,
        run: _Run,
        key: ObjectKey,
        name: str,
        reason: str,
        message: str,
        *,
        task_id: str | None = None,
    ) -> None:
        if not run.context.dry_run:
            detail: dict[str, DetailValue] = {
                "service_name" if key.object_type == SERVICE_TYPE else "container_name": name
            }
            if task_id:
                detail["task_id"] = task_id
            await run.context.activity.record_failure(
                run.context.actor,
                action=EXPORTED_ACTION,
                target_type=key.object_type,
                target_id=key.object_id,
                environment_id=run.environment_id,
                reason=reason,
                detail=detail,
                message=message,
            )
            run.attempted += 1
        run.tally.record("failed", attempted=False, reason=reason)

    def _defer(self, run: _Run, origins: Sequence[str | None]) -> None:
        for origin in origins:
            run.tally.begin(origin)
            run.tally.record("deferred", attempted=False)
        run.pending = run.pending or bool(origins)

    async def _settle_cursors(self, run: _Run, *, rotating: bool) -> None:
        rule = run.rule
        try:
            present: frozenset[str] | None = await run.engine.containers.list_ids(all=True)
        except EngineError:
            present = None
        async with run.context.db.transaction():
            await run.cursors.mark_examined(rule.id, rule.created_at, run.environment_id, run.seen)
            await run.cursors.refresh_seen(
                rule.id,
                rule.created_at,
                run.environment_id,
                list(run.seen),
                stale_after=timedelta(0) if rotating else SEEN_REFRESH,
            )
            await run.cursors.drop_stale(rule.id, rule.created_at, run.environment_id, present)

    def _folder(self, run: _Run, name: str, container_id: str) -> str:
        return container_log_folder(
            run.prefix,
            run.rule.id,
            run.rule.created_at,
            run.environment_id,
            name,
            container_id,
        )

    def _prefix_is_valid(self, run: _Run) -> bool:
        try:
            self._folder(run, "probe", PROBE_CONTAINER_ID)
        except ValueError:
            return False
        return True

    async def _candidates(self, run: _Run) -> tuple[list[_Candidate], list[_Candidate]]:
        """Return the containers to examine this run and the ones left for the next."""
        try:
            matched = await self._matched(run)
        except EnvironmentUnavailable as error:
            raise environment_outcome(error) from None
        selector, tally = run.selector, run.tally
        if isinstance(selector, ContainerTarget):
            if not matched.resolved:
                raise RunFailed("target_missing", TARGET_NOT_FOUND)
            tally.track_targets()
            for name in selector.names:
                if name in matched.missing:
                    tally.missing(name)
                elif name in matched.resolved:
                    tally.resolved(name)
        rule = run.rule
        cursors = await run.cursors.for_containers(
            rule.id, rule.created_at, run.environment_id, [row.id for row in matched.rows]
        )
        candidates = [
            _Candidate(
                row,
                display_name(row.names, row.id),
                cursors.get(row.id),
                matched.origin.get(row.id),
            )
            for row in matched.rows
        ]
        candidates.sort(
            key=lambda each: (
                each.cursor is not None,
                each.cursor.last_seen_at if each.cursor is not None else "",
                each.name,
                each.row.id,
            )
        )
        return candidates[:MAX_CONTAINERS_PER_RUN], candidates[MAX_CONTAINERS_PER_RUN:]

    async def _matched(self, run: _Run) -> _Matched:
        """List the containers, in any state, that the rule's selector names today."""
        context, client, selector = run.context, run.engine, run.selector
        found = _Matched()
        try:
            match selector:
                case ContainerTarget(names=names):
                    for name, container in await find_containers(client, names):
                        if container is None or (
                            await context.standing(
                                ObjectKey(run.environment_id, CONTAINER_TYPE, container.id)
                            )
                            == "hidden"
                        ):
                            found.missing.append(name)
                            continue
                        found.rows.append(container)
                        found.origin[container.id] = name
                        found.resolved.append(name)
                case StackTarget():
                    if run.compose_stacks:
                        listed = await client.containers.list(
                            all=True, filters={"label": [COMPOSE_PROJECT_LABEL]}
                        )
                        for name in run.compose_stacks:
                            project = canonical_stack_key(name)
                            for row in listed:
                                if in_project(row.labels, project) and row.id not in found.origin:
                                    found.rows.append(row)
                                    found.origin[row.id] = name
                case LabelsTarget(labels=terms):
                    labelled = await client.containers.list(
                        all=True, filters={"label": [term_text(term) for term in terms]}
                    )
                    found.rows = [
                        row
                        for row in labelled
                        if row.labels_truncated
                        or matches(
                            selector, name=display_name(row.names, row.id), labels=row.labels
                        )
                    ]
                case EnvironmentTarget():
                    found.rows = await client.containers.list(all=True)
        except EngineError as error:
            raise unavailable_from(error) from None
        found.rows.sort(key=lambda row: display_name(row.names, row.id))
        return found

    async def _forecast_services(
        self, run: _Run, targets: ObjectStorageTargets
    ) -> dict[str, DetailValue]:
        known = await targets.prefix_of(run.target_id)
        if known is None:
            raise RunFailed("target_missing", TARGET_MISSING)
        run.prefix = known[1]
        if not self._service_prefix_is_valid(run):
            raise RunFailed("prefix_invalid", PREFIX_INVALID)
        ordered, left = await self._service_candidates(run)
        planned: list[tuple[_ServiceCandidate, _ServicePlan]] = []
        for candidate in ordered:
            key = ObjectKey(run.environment_id, SERVICE_TYPE, candidate.service.id)
            run.tally.begin(candidate.origin)
            if not await self._service_cleared(run, key, candidate.name):
                continue
            plan = await self._plan(run, candidate)
            if plan is None:
                continue
            entries = plan.readable
            if not entries:
                run.tally.record("skipped", attempted=False, reason="up_to_date")
                continue
            planned.append((candidate, plan))
            for task_id in entries:
                label = task_id[:12] if task_id else NO_TASK_LABEL
                run.tally.record("done", attempted=False, name=f"{candidate.name}/{label}")
        self._defer(run, [each.origin for each in left])
        readable = [plan.readable for _, plan in planned]
        run.pending = run.pending or sum(map(len, readable)) > MAX_OBJECTS_PER_RUN - run.objects
        extra: dict[str, DetailValue] = {
            "target_id": run.target_id,
            "services": len(planned),
            "tasks": sum(len(each) for each in readable),
            "with_cursor": sum(1 for each in readable for task in each.values() if task.stored),
            "without_cursor": sum(
                1 for each in readable for task in each.values() if not task.stored
            ),
            "more_pending": run.pending,
        }
        if isinstance(run.selector, ServiceLabelsTarget):
            extra["matched"] = run.matched_services
        if planned:
            candidate, plan = planned[0]
            first = next(iter(plan.readable))
            extra["next_object_path"] = f"{self._service_folder(run, candidate, first)}/"
        return extra

    async def _export_services(self, run: _Run, targets: ObjectStorageTargets) -> None:
        context = run.context
        run.target_name, run.prefix = await resolve_target(targets, run.target_id)
        if not self._service_prefix_is_valid(run):
            raise ExportStopped("prefix_invalid", PREFIX_INVALID)
        ordered, left = await self._service_candidates(run)
        run.rotating = bool(left)
        client = await connect_target(
            targets, run.target_id, transport=self._transport, clock=context.clock
        )
        started = context.clock()
        async with client:
            run.client = client
            for index, candidate in enumerate(ordered):
                if context.clock() - started > START_BUDGET:
                    left = [*ordered[index:], *left]
                    run.rotating = True
                    break
                await self._export_service(run, candidate)
        self._defer(run, [each.origin for each in left])
        await self._forget_unresolved_services(run, [*ordered, *left])

    async def _export_service(self, run: _Run, candidate: _ServiceCandidate) -> None:
        tally, name = run.tally, candidate.name
        key = ObjectKey(run.environment_id, SERVICE_TYPE, candidate.service.id)
        tally.begin(candidate.origin)
        if not await self._service_cleared(run, key, name):
            return
        plan = await self._plan(run, candidate)
        if plan is None:
            return
        service = plan.service
        run.services_read += 1
        run.tasks_listed += len(plan.tasks)
        lower = plan.lower_ns
        finishing: set[str] = set()
        if lower is None and not plan.terminal:
            tally.record("skipped", attempted=False, reason="up_to_date")
        else:
            spec = service.spec
            source = service_source(
                service_id=service.id,
                service_name=spec.name,
                image=service_image(spec.task_template),
                labels=spec.labels,
            )
            sound = True
            if lower is not None:
                read = _ServiceRead(plan, run.log_context, source)
                sound = await self._collect_service(run, key, plan, read, lower)
                if sound:
                    run.unlisted_lines += read.unlisted
                    run.pending = run.pending or read.capped
                    await self._store_tasks(run, plan, read)
            if sound:
                finishing = await self._export_terminal(run, key, plan, source)
        await self._settle_service(run, plan, finishing)

    def _service_tty(self, service: SwarmService) -> bool:
        container = service.spec.task_template.get("ContainerSpec")
        return isinstance(container, dict) and container.get("TTY") is True

    async def _collect_service(
        self, run: _Run, key: ObjectKey, plan: _ServicePlan, read: _ServiceRead, lower_ns: int
    ) -> bool:
        """Read the service's log into the per-task collectors; False when the read failed."""
        frames = run.engine.swarm_services.logs(
            key.object_id,
            tty=self._service_tty(plan.service),
            follow=False,
            stdout=True,
            stderr=True,
            since=format_engine_timestamp(lower_ns),
        )
        return await self._drain(run, key, plan.service.spec.name, frames, read)

    async def _export_terminal(
        self, run: _Run, key: ObjectKey, plan: _ServicePlan, source: SourceKeys
    ) -> set[str]:
        """Read each ended task from its own log; return the tasks read to their end."""
        finishing: set[str] = set()
        tty = self._service_tty(plan.service)
        name = plan.service.spec.name
        for task_id, start in plan.terminal.items():
            alone = replace(plan, reading={task_id: start}, terminal={})
            read = _ServiceRead(alone, run.log_context, source, only=task_id)
            frames = run.engine.swarm_tasks.logs(
                task_id,
                tty=tty,
                follow=False,
                stdout=True,
                stderr=True,
                since=format_engine_timestamp(start.start_ns),
            )
            if not await self._drain(run, key, name, frames, read, task_id=task_id):
                break
            run.pending = run.pending or read.capped
            left = await self._store_tasks(run, alone, read, quiet=True)
            if not read.capped and not left:
                finishing.add(task_id)
        return finishing

    async def _drain(
        self,
        run: _Run,
        key: ObjectKey,
        name: str,
        frames: AsyncGenerator[LogFrame, None],
        read: _ServiceRead,
        *,
        task_id: str | None = None,
    ) -> bool:
        """Feed the log's lines to `read`; False when the read failed."""
        reader = FrameReader(frames)
        splitter = LogLineSplitter()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READ_SECONDS
        try:
            while True:
                frame = await reader.next(deadline - loop.time())
                if isinstance(frame, ReadTimeout):
                    read.capped = True
                    return True
                lines = splitter.flush() if frame is None else await splitter.split([frame])
                if len(lines) > ENCODE_IN_THREAD_LINES:
                    stopped = await asyncio.to_thread(read.add, lines)
                else:
                    stopped = read.add(lines)
                if stopped or frame is None:
                    return True
        except NotFound:
            if task_id is None:
                await self._fail(run, key, name, "logs_unavailable", SERVICE_LOGS_UNAVAILABLE)
            else:
                run.tally.record("skipped", attempted=False, reason="gone")
            return False
        except EngineError as error:
            if error.status_code == NOT_IMPLEMENTED:
                run.tally.record("skipped", attempted=False, reason=NOT_READABLE)
            else:
                await self._fail(
                    run, key, name, "logs_unavailable", SERVICE_LOGS_UNAVAILABLE, task_id=task_id
                )
            return False
        finally:
            with anyio.CancelScope(shield=True):
                await reader.aclose()

    async def _store_tasks(
        self, run: _Run, plan: _ServicePlan, read: _ServiceRead, *, quiet: bool = False
    ) -> set[str]:
        """Store one object per task with new lines; return the tasks left for the next run.

        `quiet` leaves a read that found nothing new out of the tally.
        """
        tally = run.tally
        left: set[str] = set()
        stored = 0
        skipped_lines = 0
        for task_id in self._task_order(plan, read):
            collector = read.collectors[task_id]
            if collector.first_ns is None:
                if collector.lines:
                    tally.record("skipped", attempted=False, reason="no_timestamps")
                    skipped_lines += 1
                continue
            if run.objects >= MAX_OBJECTS_PER_RUN:
                left.add(task_id)
                run.pending = True
                tally.record("deferred", attempted=False)
                continue
            await self._store_task(run, plan.service, task_id, collector, plan.reading.get(task_id))
            stored += 1
        if not stored and not left and not skipped_lines and (read.capped or not quiet):
            reason = "timed_out" if read.capped else "up_to_date"
            tally.record("skipped", attempted=False, reason=reason)
        return left

    def _task_order(self, plan: _ServicePlan, read: _ServiceRead) -> list[str]:
        known = [task.id for task in plan.tasks if task.id in read.collectors]
        return [*known, *(each for each in read.collectors if each not in plan.starts)]

    async def _store_task(
        self,
        run: _Run,
        service: SwarmService,
        task_id: str,
        collector: _Collector,
        position: _TaskStart | None,
    ) -> None:
        rule, name = run.rule, service.spec.name
        first_ns, max_ns = collector.first_ns, collector.max_ns
        if first_ns is None or max_ns is None or run.client is None:
            raise RuntimeError("a stored object needs a timestamped line and a storage client")
        stored = position if position is not None and position.stored else None
        continued_after = stored.skip if stored is not None and first_ns == stored.start_ns else 0
        key = ObjectKey(run.environment_id, SERVICE_TYPE, service.id)
        try:
            path = service_log_key(
                run.prefix,
                rule.id,
                rule.created_at,
                run.environment_id,
                name,
                service.id,
                task_id or None,
                first_ns,
                continued_after,
            )
        except ValueError:
            await self._fail(run, key, name, "key_invalid", SERVICE_KEY_INVALID, task_id=task_id)
            return
        detail: dict[str, DetailValue] = {
            "object_path": path,
            "lines": collector.lines,
            "service_name": name,
        }
        if task_id:
            detail["task_id"] = task_id
        failure = await self._upload(run, run.client, path, collector)
        if failure is not None:
            raise _ObjectStopped(
                failure.kind,
                failure.message,
                SERVICE_TYPE,
                service.id,
                failure=failure,
                detail=detail,
            )
        carried = stored.skip if stored is not None and max_ns == stored.start_ns else 0
        try:
            async with run.context.db.transaction():
                await run.service_cursors.store_position(
                    rule.id,
                    rule.created_at,
                    run.environment_id,
                    service.id,
                    task_id,
                    last_timestamp_ns=max_ns,
                    lines_at_last=carried + collector.at_max,
                    object_path=path,
                )
        except MissingReferenceError:
            raise _ObjectStopped(
                "rule_missing", RULE_MISSING, SERVICE_TYPE, service.id, detail=detail
            ) from None
        run.objects += 1
        run.lines += collector.lines
        run.tally.record("done", attempted=False)

    async def _settle_service(self, run: _Run, plan: _ServicePlan, finishing: set[str]) -> None:
        rule, service_id = run.rule, plan.service.id
        cursors = run.service_cursors
        async with run.context.db.transaction():
            await cursors.drop_other_creations(rule.id, rule.created_at)
            await cursors.drop_unlisted_tasks(
                rule.id, rule.created_at, run.environment_id, service_id, plan.starts
            )
            await cursors.mark_seen(
                rule.id, rule.created_at, run.environment_id, service_id, plan.starts
            )
            await cursors.mark_finished(
                rule.id, rule.created_at, run.environment_id, service_id, finishing
            )
            await cursors.refresh_seen(
                rule.id,
                rule.created_at,
                run.environment_id,
                service_id,
                stale_after=timedelta(0) if run.rotating else SEEN_REFRESH,
            )

    async def _forget_unresolved_services(
        self, run: _Run, candidates: Sequence[_ServiceCandidate]
    ) -> None:
        rule = run.rule
        try:
            present = {each.id for each in await run.engine.swarm_services.list()}
        except EngineError:
            return
        keep = present | {each.service.id for each in candidates}
        async with run.context.db.transaction():
            await run.service_cursors.drop_unresolved_services(
                rule.id, rule.created_at, run.environment_id, keep
            )

    async def _plan(self, run: _Run, candidate: _ServiceCandidate) -> _ServicePlan | None:
        """List the service's tasks and decide where each resumes; None when the service is gone."""
        rule, service = run.rule, candidate.service
        try:
            listed = await run.engine.swarm_tasks.list(filters={"service": [service.id]})
        except NotFound:
            run.tally.record("skipped", attempted=False, reason="gone")
            return None
        except EngineError as error:
            raise environment_outcome(
                swarm_unavailable_from(error, run.environment_id, self._deps.engine.refresher.poke)
            ) from None
        tasks = sorted(listed, key=lambda each: (each.created_at, each.id))
        cursors = await run.service_cursors.for_service(
            rule.id, rule.created_at, run.environment_id, service.id
        )
        fallback = _created_ns(service.created_at, 0)
        starts: dict[str, int] = {}
        reading: dict[str, _TaskStart] = {}
        terminal: dict[str, _TaskStart] = {}
        ignored: set[str] = set()
        for task in tasks:
            cursor = cursors.get(task.id)
            position = _position(cursor)
            start = (
                position.last_timestamp_ns
                if position is not None
                else _created_ns(task.created_at, fallback // NANOS_PER_SECOND)
            )
            starts[task.id] = start
            if cursor is not None and cursor.finished:
                ignored.add(task.id)
                continue
            resume = _TaskStart(
                start, position.lines_at_last if position is not None else 0, position is not None
            )
            if task.status.state in TERMINAL_TASK_STATES:
                terminal[task.id] = resume
            else:
                reading[task.id] = resume
        loose = _position(cursors.get(NO_TASK))
        if loose is not None:
            reading[NO_TASK] = _TaskStart(loose.last_timestamp_ns, loose.lines_at_last, True)
        return _ServicePlan(service, tasks, starts, reading, frozenset(ignored), terminal)

    async def _service_cleared(self, run: _Run, key: ObjectKey, name: str) -> bool:
        """Report whether the service may be exported now, recording why not in the tally."""
        if not await self._permitted(run, key, name):
            return False
        return not await check_suppressed(
            run.context, key, single=False, tally=run.tally, name=name
        )

    async def _service_candidates(
        self, run: _Run
    ) -> tuple[list[_ServiceCandidate], list[_ServiceCandidate]]:
        """Return the services to examine this run and the ones left for the next."""
        found: list[tuple[str | None, SwarmService]] = []
        try:
            match run.selector:
                case ServiceTarget():
                    found = await self._named_services(run)
                case ServiceLabelsTarget(labels=terms):
                    found = [
                        (None, service)
                        for service in await self._list_services(run)
                        if not service.spec.labels_truncated
                        and carries(terms, service.spec.labels)
                        and not await self._hidden_service(run, service)
                    ]
                case StackTarget():
                    found = await self._stack_services(run)
                case _:
                    raise RunFailed("selector_invalid", "The automation's target is not valid.")
        except EnvironmentUnavailable as error:
            raise environment_outcome(error) from None
        run.matched_services = len(found)
        rule = run.rule
        seen = await run.service_cursors.last_seen(
            rule.id, rule.created_at, run.environment_id, [each.id for _, each in found]
        )
        candidates = [
            _ServiceCandidate(each.spec.name, each, seen.get(each.id), origin)
            for origin, each in found
        ]
        candidates.sort(
            key=lambda each: (each.seen is not None, each.seen or "", each.name, each.service.id)
        )
        return candidates[:MAX_SERVICES_PER_RUN], candidates[MAX_SERVICES_PER_RUN:]

    async def _named_services(self, run: _Run) -> list[tuple[str | None, SwarmService]]:
        selector, tally = run.selector, run.tally
        if not isinstance(selector, ServiceTarget):
            return []
        found: list[tuple[str | None, SwarmService]] = []
        for name in selector.names:
            located = await find_service(
                run.engine, name, run.environment_id, self._deps.engine.refresher.poke
            )
            if located is not None and not await self._hidden_service(run, located):
                found.append((name, located))
        if not found:
            raise RunFailed("target_missing", TARGET_NOT_FOUND)
        tally.track_targets()
        resolved = {name for name, _ in found}
        for name in selector.names:
            if name in resolved:
                tally.resolved(name)
            else:
                tally.missing(name)
        return found

    async def _stack_services(self, run: _Run) -> list[tuple[str | None, SwarmService]]:
        spellings = {canonical_stack_key(name): name for name in run.swarm_stacks}
        found: list[tuple[str | None, SwarmService]] = []
        for service in await self._list_services(run):
            namespace = service.spec.labels.get(SWARM_NAMESPACE_LABEL)
            origin = spellings.get(canonical_stack_key(namespace)) if namespace else None
            if origin is not None and not await self._hidden_service(run, service):
                found.append((origin, service))
        return found

    async def _list_services(self, run: _Run) -> list[SwarmService]:
        try:
            listed = await read_services(
                run.engine, run.environment_id, self._deps.engine.refresher.poke
            )
        except ObjectTooLarge as error:
            raise RunFailed(failure_reason(error), str(error)) from None
        return [each for each in listed if is_canonical_id(SERVICE_TYPE, each.id)]

    async def _hidden_service(self, run: _Run, service: SwarmService) -> bool:
        key = ObjectKey(run.environment_id, SERVICE_TYPE, service.id)
        return await run.context.standing(key) == "hidden"

    def _service_folder(self, run: _Run, candidate: _ServiceCandidate, task_id: str) -> str:
        return service_log_folder(
            run.prefix,
            run.rule.id,
            run.rule.created_at,
            run.environment_id,
            candidate.service.spec.name,
            candidate.service.id,
            task_id or None,
        )

    def _service_prefix_is_valid(self, run: _Run) -> bool:
        try:
            service_log_folder(
                run.prefix,
                run.rule.id,
                run.rule.created_at,
                run.environment_id,
                "probe",
                PROBE_SERVICE_ID,
                None,
            )
        except ValueError:
            return False
        return True

    async def _record_stop(self, run: _Run, stopped: ExportStopped) -> RunResult:
        detail: dict[str, DetailValue] = {}
        if isinstance(stopped, _ObjectStopped):
            target_type, target_id = stopped.target_type, stopped.target_id
        else:
            target_type, target_id = TARGET_TYPE, str(run.target_id)
            if run.target_name is not None:
                detail["target_name"] = run.target_name
        detail.update(stopped.detail)
        await record_stop(
            run.context,
            action=EXPORTED_ACTION,
            target_type=target_type,
            target_id=target_id,
            stopped=stopped,
            detail=detail,
            environment_id=run.environment_id,
        )
        run.pending = True
        return RunResult(
            "partial" if run.objects else "failure",
            attempted=run.attempted,
            reason=stopped.reason,
            message=stopped.message,
            detail=self._summary(run).detail,
        )

    def _summary(self, run: _Run) -> RunResult:
        extra: dict[str, DetailValue] = {
            "target_id": run.target_id,
            "lines": run.lines,
            "more_pending": run.pending,
        }
        if run.serving:
            extra["services"] = run.services_read
            extra["tasks"] = run.tasks_listed
            extra["unlisted_task"] = run.unlisted_lines
        if isinstance(run.selector, ServiceLabelsTarget):
            extra["matched"] = run.matched_services
        return replace(
            run.tally.result(dry_run=False, extra=extra),
            attempted=run.attempted,
        )
