"""Service logs: the live stream, the capped download and the page tail across a service's tasks."""

import asyncio
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, Self

import anyio

from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.config import DEFAULT_INSTANCE_NAME
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError
from app.docker_engine.frames import LogFrame
from app.docker_engine.models import SwarmService
from app.docker_engine.resources import LOG_FRAME_BYTES, format_engine_timestamp
from app.services.access import ResourceKey, is_canonical_id
from app.services.authorisation import Authoriser
from app.services.container_logs import (
    DOWNLOAD_MEDIA_TYPES,
    MAX_BATCH_LINES,
    PAGE_FRAME_BYTES,
    PAGE_SECONDS,
    FrameReader,
    FrameRequest,
    LogDownload,
    LogLimits,
    LogQuery,
    LogRecords,
    LogStream,
    LogsUnavailable,
    OriginLine,
    ReadTimeout,
    cap_line,
    download_filename,
    read_log_context,
    render_batch,
    render_download_line,
    render_notice,
    resume_point,
)
from app.services.log_lines import LogLine, LogLineSplitter
from app.services.log_ndjson import service_image, service_source
from app.services.object_inspector import EngineObjectInspector
from app.services.object_locks import StreamSlots
from app.services.service_log_lines import (
    LogDeduper,
    ServiceLogLine,
    TailMerger,
    resume_since,
    split_details,
)
from app.services.swarm import label_safe_hostname, node_hostnames, swarm_read
from app.services.swarm_gate import require_swarm

__all__ = [
    "SERVICE_DEFAULT_TAIL",
    "SERVICE_MAX_TAIL",
    "MergedDownload",
    "ServiceLogService",
    "ServiceLogSource",
    "ServiceLogTail",
    "TaskLabels",
]

SERVICE_DEFAULT_TAIL: Final = 200
SERVICE_MAX_TAIL: Final = 5000
MAX_TASKS_LABELLED: Final = 20_000


@dataclass(frozen=True, slots=True)
class TaskLabels:
    """The label each task's lines carry: `<service>.<slot>.<task>` or `<service>.<node>.<task>`."""

    names: Mapping[str, str]

    def label(self, task_id: str | None) -> str:
        """Return the label for a task id, its short id when the task is not known, else `-`."""
        if task_id is None:
            return "-"
        return self.names.get(task_id) or task_id[:12]


class ServiceLogSource:
    """The logs of one service across its tasks, each line labelled with the task that wrote it."""

    subject = "service"

    def __init__(
        self,
        client: EngineClient,
        service_id: str,
        *,
        tty: bool,
        labels: TaskLabels,
        dedupe: bool,
        resume_after: int | None = None,
    ) -> None:
        self._client = client
        self._service_id = service_id
        self._tty = tty
        self._labels = labels
        self._deduper = LogDeduper(floor=resume_after) if dedupe else None

    def frames(self, request: FrameRequest) -> AsyncGenerator[LogFrame, None]:
        """Open the service's log stream."""
        since: str | None = None
        if request.since_ns is not None:
            since = (
                resume_since(request.since_ns)
                if request.since_exclusive
                else format_engine_timestamp(request.since_ns)
            )
        return self._client.swarm_services.logs(
            self._service_id,
            tty=self._tty,
            follow=request.follow,
            stdout=request.stdout,
            stderr=request.stderr,
            since=since,
            until=None if request.until_ns is None else format_engine_timestamp(request.until_ns),
            tail=request.tail,
            max_frame_bytes=request.max_frame_bytes or LOG_FRAME_BYTES,
        )

    def lines(self, lines: list[LogLine]) -> list[LogLine]:
        """Take the attribute token off each line and label it; drop lines already delivered."""
        finished: list[LogLine] = []
        for line in lines:
            item = split_details(line)
            if self._deduper is not None and not self._deduper.accept(item):
                continue
            finished.append(_labelled(item, self._labels))
        return finished


def _labelled(item: ServiceLogLine, labels: TaskLabels) -> OriginLine:
    line = item.line
    return OriginLine(
        line.stream,
        line.text,
        line.timestamp,
        line.timestamp_ns,
        line.truncated,
        labels.label(item.task_id),
        item.task_id,
        item.node_id,
    )


@dataclass(frozen=True, slots=True)
class ServiceLogTail:
    """The newest lines of a service's output across its tasks, for a page without scripting."""

    lines: tuple[OriginLine, ...]
    cut: bool
    name: str
    short_id: str
    tasks: int


class MergedDownload:
    """A download of the newest lines across tasks, in time order.

    The lines are read before the object exists, so it holds no Engine stream and no slot.
    """

    def __init__(
        self,
        lines: Sequence[OriginLine],
        *,
        query: LogQuery,
        filename: str,
        limits: LogLimits,
        timed_out: bool,
        records: LogRecords,
    ) -> None:
        self._lines = lines
        self._query = query
        self._limits = limits
        self._timed_out = timed_out
        self._records = records
        self.filename = filename
        self.media_type = DOWNLOAD_MEDIA_TYPES[query.format]
        self.end_reason: Literal["finished", "capped", "closed"] | None = None
        self._chunks = self._generate()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        return await self._chunks.__anext__()

    async def aclose(self) -> None:
        """End the download; safe to repeat."""
        await self._chunks.aclose()
        if self.end_reason is None:
            self.end_reason = "closed"

    async def _generate(self) -> AsyncGenerator[bytes, None]:
        for start in range(0, len(self._lines), MAX_BATCH_LINES):
            batch = await render_batch(
                self._query,
                self._lines[start : start + MAX_BATCH_LINES],
                lambda line: render_download_line(self._query, line, self._records),
            )
            rendered = [encoded for encoded in batch if encoded is not None]
            if rendered:
                yield b"".join(rendered)
        if self._timed_out:
            yield render_notice(self._query, cap_line("seconds", self._limits))
            self.end_reason = "capped"
        else:
            self.end_reason = "finished"


@dataclass(frozen=True, slots=True)
class _Prepared:
    client: EngineClient
    service: SwarmService
    tty: bool
    labels: TaskLabels
    tasks: int


class ServiceLogService:
    """Authorises at read access once, then hands out service log output."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        slots: StreamSlots,
        limits: LogLimits | None = None,
        poke: Callable[[int], None] | None = None,
        instance_name: str = DEFAULT_INSTANCE_NAME,
    ) -> None:
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._slots = slots
        self._limits = limits or LogLimits()
        self._poke: Callable[[int], None] = poke or (lambda _: None)
        self._instance_name = instance_name

    async def open(
        self,
        actor: Principal,
        environment_id: int,
        service_id: str,
        query: LogQuery,
        last_event_id: str | None = None,
        after: str | None = None,
    ) -> LogStream:
        """Authorise at read access and claim a view slot; the caller must close the stream.

        ResourceNotFound unless the environment is a Swarm manager the caller can see and the
        caller may read the service, with no log request made. TooManyStreams when the user or
        the process has no free view slot; the slot is claimed last. A reconnecting browser's
        Last-Event-ID, or `after`, resumes after the nanosecond timestamp it names and drops
        `tail`.
        """
        prepared = await self._prepare(actor, environment_id, service_id)
        slot = self._slots.acquire(actor.user_id, is_administrator=actor.is_administrator)
        resume_after = resume_point(last_event_id, after)
        return LogStream(
            ServiceLogSource(
                prepared.client,
                service_id,
                tty=prepared.tty,
                labels=prepared.labels,
                dedupe=True,
                resume_after=resume_after,
            ),
            query=query,
            follow=query.follow,
            resume_after=resume_after,
            slot=slot,
            limits=self._limits,
        )

    async def download(
        self, actor: Principal, environment_id: int, service_id: str, query: LogQuery
    ) -> LogDownload | MergedDownload:
        """Authorise at read access and prepare a capped download; the caller must close it.

        With a tail, the newest lines across all tasks are merged into time order. Without one
        the output streams in arrival order, one task after another. LogsUnavailable when the
        Engine fails before the first line, with the slot given back.
        """
        prepared = await self._prepare(actor, environment_id, service_id)
        records = LogRecords(
            await read_log_context(self._hosts, environment_id, self._instance_name),
            service_source(
                service_id=service_id,
                service_name=prepared.service.spec.name,
                image=service_image(prepared.service.spec.task_template),
                labels=prepared.service.spec.labels,
            ),
        )
        slot = self._slots.acquire(actor.user_id, is_administrator=actor.is_administrator)
        filename = download_filename(
            prepared.service.spec.name, service_id, datetime.now(UTC), query.format
        )
        if query.tail is None:
            download = LogDownload(
                ServiceLogSource(
                    prepared.client,
                    service_id,
                    tty=prepared.tty,
                    labels=prepared.labels,
                    dedupe=False,
                ),
                query=query,
                filename=filename,
                slot=slot,
                limits=self._limits,
                records=records,
            )
            await download.prime()
            return download
        try:
            lines, _, timed_out = await self._merged(
                prepared,
                service_id,
                query,
                count=query.tail,
                max_bytes=self._limits.download_bytes,
                seconds=self._limits.download_seconds,
                frame_bytes=LOG_FRAME_BYTES,
            )
        finally:
            slot.close()
        return MergedDownload(
            lines,
            query=query,
            filename=filename,
            limits=self._limits,
            timed_out=timed_out,
            records=records,
        )

    async def tail(
        self, actor: Principal, environment_id: int, service_id: str, query: LogQuery
    ) -> ServiceLogTail:
        """Read the newest lines across tasks for a page without scripting, at most 1 MiB.

        Holds a view slot while it reads, so TooManyStreams when none is free. The Engine
        applies the tail to each task, so the lines are merged by time and cut to the tail;
        `cut` is set when older lines were left out. LogsUnavailable when the Engine fails.
        """
        prepared = await self._prepare(actor, environment_id, service_id)
        slot = self._slots.acquire(actor.user_id, is_administrator=actor.is_administrator)
        try:
            lines, cut, _ = await self._merged(
                prepared,
                service_id,
                query,
                count=query.tail if query.tail is not None else SERVICE_MAX_TAIL,
                max_bytes=self._limits.page_bytes,
                seconds=PAGE_SECONDS,
                frame_bytes=PAGE_FRAME_BYTES,
            )
        finally:
            slot.close()
        return ServiceLogTail(
            tuple(lines),
            cut,
            prepared.service.spec.name,
            service_id[:12],
            prepared.tasks,
        )

    async def _merged(
        self,
        prepared: _Prepared,
        service_id: str,
        query: LogQuery,
        *,
        count: int,
        max_bytes: int,
        seconds: float,
        frame_bytes: int,
    ) -> tuple[list[OriginLine], bool, bool]:
        source = ServiceLogSource(
            prepared.client, service_id, tty=prepared.tty, labels=prepared.labels, dedupe=False
        )
        request = FrameRequest(
            follow=False,
            stdout=query.stdout,
            stderr=query.stderr,
            since_ns=query.since_ns,
            until_ns=query.until_ns,
            tail=query.tail,
            max_frame_bytes=frame_bytes,
        )
        frames = FrameReader(source.frames(request))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        splitter = LogLineSplitter()
        merger = TailMerger(count, max_bytes=max_bytes)
        timed_out = False
        try:
            while True:
                frame = await frames.next(deadline - loop.time())
                if frame is None:
                    break
                if isinstance(frame, ReadTimeout):
                    timed_out = True
                    break
                for line in await splitter.split([frame]):
                    merger.add(split_details(line))
            for line in splitter.flush():
                merger.add(split_details(line))
        except EngineError:
            raise LogsUnavailable(source.subject) from None
        finally:
            with anyio.CancelScope(shield=True):
                await frames.aclose()
        lines = [_labelled(item, prepared.labels) for item in merger.result()]
        return lines, merger.cut or timed_out, timed_out

    async def authorise(self, actor: Principal, environment_id: int, service_id: str) -> None:
        """Check the Swarm gate and read access to the service, with no log request made.

        ResourceNotFound unless the environment is a Swarm manager the caller can see and the
        caller may read the service; EnvironmentUnavailable and ObjectTooLarge when the host
        cannot be read. Call it before reading the query.
        """
        if not is_canonical_id("service", service_id):
            raise ResourceNotFound("not found")
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "service", service_id), "read"
        )

    async def _prepare(self, actor: Principal, environment_id: int, service_id: str) -> _Prepared:
        await self.authorise(actor, environment_id, service_id)
        service = await self._inspector.service(environment_id, service_id)
        client = await self._hosts.client(environment_id)
        tasks = await swarm_read(
            client.swarm_tasks.list(filters={"service": [service_id]}),
            environment_id,
            self._poke,
        )
        is_global = "Global" in service.spec.mode
        hostnames: dict[str, str] = {}
        if is_global:
            nodes = await swarm_read(client.swarm_nodes.list(), environment_id, self._poke)
            hostnames = {
                node_id: label_safe_hostname(name)
                for node_id, name in node_hostnames(nodes).items()
            }
        name = label_safe_hostname(service.spec.name)
        names: dict[str, str] = {}
        for task in tasks[:MAX_TASKS_LABELLED]:
            if is_global:
                where = hostnames.get(task.node_id, "-")
            else:
                where = str(task.slot) if task.slot is not None else "-"
            names[task.id] = f"{name}.{where}.{task.id[:12]}"
        container = service.spec.task_template.get("ContainerSpec")
        tty = isinstance(container, dict) and container.get("TTY") is True
        return _Prepared(client, service, tty, TaskLabels(names), len(tasks))
