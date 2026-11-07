"""Whole-stack logs: member resolution, the merged tail, the follow multiplexer and the download."""

import asyncio
import re
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal, Self

import anyio

from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.config import DEFAULT_INSTANCE_NAME
from app.db.repositories.stack import StackRepository, StoredStack
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError
from app.docker_engine.frames import LogFrame
from app.docker_engine.models import ContainerSummary, SwarmService
from app.docker_engine.resources import LOG_FRAME_BYTES
from app.logging import get_logger
from app.services.access import (
    COMPOSE_PROJECT_LABEL,
    ResourceKey,
    ResourceRef,
    canonical_stack_key,
)
from app.services.authorisation import Authoriser
from app.services.container_logs import (
    DEFAULT_PAGE_TAIL,
    DEFAULT_TAIL,
    DOWNLOAD_MEDIA_TYPES,
    DOWNLOAD_SUFFIXES,
    LIVE_STATES,
    MAX_BATCH_LINES,
    MAX_PAGE_TAIL,
    MAX_TAIL,
    PAGE_FRAME_BYTES,
    PAGE_SECONDS,
    ContainerLogSource,
    FrameReader,
    FrameRequest,
    LogBatch,
    LogLimits,
    LogQuery,
    LogSource,
    LogsUnavailable,
    OriginLine,
    ReadTimeout,
    cap_line,
    read_log_context,
    render_batch,
    render_line,
    render_notice,
    resume_point,
)
from app.services.container_stats import KEEP_ALIVE, KeepAlive, StreamEnd
from app.services.containers import display_name
from app.services.host_objects import permitted_container_ids
from app.services.log_lines import LogLine, LogLineSplitter
from app.services.log_ndjson import (
    LogContext,
    SourceKeys,
    container_source,
    encode_log_line,
    for_task,
    service_image,
    service_source,
)
from app.services.object_inspector import EngineObjectInspector
from app.services.object_locks import StreamSlot, StreamSlots
from app.services.service_log_lines import ServiceLogLine, TailMerger
from app.services.service_logs import MAX_TASKS_LABELLED, ServiceLogSource, TaskLabels
from app.services.stack_common import read_labelled_containers
from app.services.stack_deploy import in_project
from app.services.swarm import label_safe_hostname, node_hostnames, swarm_read
from app.services.swarm_gate import require_swarm
from app.services.swarm_stacks import NAMESPACE_ROUTE, read_namespace

if TYPE_CHECKING:
    from app.services.stack_queries import StackQueryService

__all__ = [
    "MAX_MEMBERS",
    "MERGE_BYTES",
    "StackLine",
    "StackLogDownload",
    "StackLogItem",
    "StackLogMembers",
    "StackLogNotice",
    "StackLogService",
    "StackLogStream",
    "StackLogTail",
]

logger = get_logger(__name__)

MAX_MEMBERS: Final = 20
MERGE_BYTES: Final = 4 * 1024 * 1024
SERVICE_LABEL: Final = "com.docker.compose.service"
NUMBER_LABEL: Final = "com.docker.compose.container-number"
UNKNOWN_SERVICE: Final = "(unknown)"
QUEUE_ITEMS: Final = 32
FILENAME_NAME_CHARS: Final = 64

_FILENAME_UNSAFE: Final = re.compile(r"[^A-Za-z0-9_.-]")
_NO_LABELS: Final = TaskLabels({})


@dataclass(frozen=True, slots=True)
class StackLine(OriginLine):
    """A log line with the member it came from.

    `origin` is the prefix shown before the text. `container_name` is None for a Swarm service.
    `keys` is the member's NDJSON key set.
    """

    service: str = ""
    container_name: str | None = None
    keys: SourceKeys = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class StackLogMembers:
    """What a stack's log view covers.

    `services` are the service names the caller may read logs of, the filter's choices.
    `selected` are the services in effect: `services` when no filter applies. `members` counts
    the containers (Compose) or services (Swarm) the view reads. `hidden` counts the ones the
    caller may not read; they are never named. `over_cap` counts selected ones left out past
    MAX_MEMBERS. `unreadable` counts the ones whose configuration could not be read.
    """

    name: str
    services: tuple[str, ...]
    selected: tuple[str, ...]
    members: int
    hidden: int
    over_cap: int
    unreadable: int = 0


@dataclass(frozen=True, slots=True)
class StackLogNotice:
    """A line of the view's own: older lines left out, or one member stopped."""

    text: str


@dataclass(frozen=True, slots=True)
class StackLogTail:
    """The newest merged lines for a page without scripting.

    `cut` is set when older lines were left out. `unreadable` counts members that gave no lines
    because they could not be read.
    """

    lines: tuple[StackLine, ...]
    cut: bool
    members: StackLogMembers
    unreadable: int


type StackLogItem = LogBatch | KeepAlive | StackLogNotice


@dataclass(frozen=True, slots=True, eq=False)
class _Member:
    service: str
    name: str | None
    prefix: str
    object_id: str
    client: EngineClient
    tty: bool
    live: bool
    keys: SourceKeys
    task_prefixes: Mapping[str, str] = field(default_factory=dict)

    def source(self, *, dedupe: bool = False, floor: int | None = None) -> LogSource:
        if self.name is None:
            return ServiceLogSource(
                self.client,
                self.object_id,
                tty=self.tty,
                labels=_NO_LABELS,
                dedupe=dedupe,
                resume_after=floor,
            )
        return ContainerLogSource(self.client, self.object_id, tty=self.tty)

    def line(self, line: LogLine) -> StackLine:
        task_id = line.task_id if isinstance(line, OriginLine) else None
        origin = self.prefix if task_id is None else self.task_prefixes.get(task_id, self.prefix)
        return StackLine(
            line.stream,
            line.text,
            line.timestamp,
            line.timestamp_ns,
            line.truncated,
            origin,
            task_id,
            line.node_id if isinstance(line, OriginLine) else None,
            self.service,
            self.name,
            self.keys,
        )


@dataclass(frozen=True, slots=True)
class _Plan:
    members: tuple[_Member, ...]
    info: StackLogMembers


@dataclass(frozen=True, slots=True)
class _Candidate:
    service: str
    number: int
    container: ContainerSummary


@dataclass(slots=True)
class _Read:
    lines: list[StackLine] = field(default_factory=list)
    cut: bool = False
    timed_out: bool = False
    failed: list[int] = field(default_factory=list)
    marks: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Lines:
    lines: tuple[StackLine, ...]

    @property
    def size(self) -> int:
        return sum(len(line.text) for line in self.lines)


@dataclass(frozen=True, slots=True)
class _Ended:
    index: int
    failed: bool

    @property
    def size(self) -> int:
        return 0


def _choose(names: Collection[str], requested: Collection[str]) -> frozenset[str]:
    picked = frozenset(requested) & frozenset(names)
    return picked or frozenset(names)


def _service_of(container: ContainerSummary) -> str:
    return container.labels.get(SERVICE_LABEL) or UNKNOWN_SERVICE


def _number_of(container: ContainerSummary) -> int:
    text = container.labels.get(NUMBER_LABEL, "")
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 9 else 0


def _batches(lines: Iterable[StackLine]) -> list[LogBatch]:
    batches: list[LogBatch] = []
    run: list[StackLine] = []
    stream: Literal["out", "err"] = "out"

    def flush() -> None:
        for start in range(0, len(run), MAX_BATCH_LINES):
            batches.append(LogBatch(stream, tuple(run[start : start + MAX_BATCH_LINES])))
        run.clear()

    for line in lines:
        name: Literal["out", "err"] = "err" if line.stream == "stderr" else "out"
        if run and name != stream:
            flush()
        stream = name
        run.append(line)
    flush()
    return batches


def _render(query: LogQuery, line: StackLine, context: LogContext) -> bytes | None:
    if query.format == "text":
        rendered = render_line(query, line)
        return None if rendered is None else rendered.encode("utf-8")
    if query.filter and render_line(query, line) is None:
        return None
    keys = line.keys
    if line.container_name is None:
        keys = for_task(keys, line.task_id, line.node_id)
    return encode_log_line(line, context, keys)


def _download_name(name: str, moment: datetime, query: LogQuery) -> str:
    safe = _FILENAME_UNSAFE.sub("_", name)[:FILENAME_NAME_CHARS] or "stack"
    stamp = moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{safe}-{stamp}{DOWNLOAD_SUFFIXES[query.format]}"


def _unreadable_text(count: int) -> str:
    return f"[{count} container(s) could not be read and are not shown]"


async def _read_members(
    members: Sequence[_Member],
    query: LogQuery,
    *,
    tail: int | None,
    count: int,
    max_bytes: int,
    seconds: float,
    frame_bytes: int,
) -> _Read:
    """Read every member's history at once and merge it into time order within the budget."""
    merger = TailMerger(count, max_bytes=max_bytes)
    result = _Read()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds

    def take(index: int, member: _Member, lines: Iterable[LogLine]) -> None:
        for line in lines:
            stacked = member.line(line)
            stamp = stacked.timestamp_ns
            if stamp is not None and stamp > result.marks.get(index, -1):
                result.marks[index] = stamp
            merger.add(ServiceLogLine(stacked, stacked.task_id, None))

    async def read_one(index: int, member: _Member) -> None:
        source = member.source()
        frames = FrameReader(
            source.frames(
                FrameRequest(
                    follow=False,
                    stdout=query.stdout,
                    stderr=query.stderr,
                    since_ns=query.since_ns,
                    until_ns=query.until_ns,
                    tail=tail,
                    max_frame_bytes=frame_bytes,
                )
            )
        )
        splitter = LogLineSplitter()
        try:
            while True:
                frame = await frames.next(deadline - loop.time())
                if frame is None:
                    break
                if isinstance(frame, ReadTimeout):
                    result.timed_out = True
                    break
                take(index, member, source.lines(await splitter.split([frame])))
            take(index, member, source.lines(splitter.flush()))
        except EngineError as error:
            logger.warning(
                "stack_log_read_failed",
                extra={"service": member.service, "error": type(error).__name__},
            )
            result.failed.append(index)
        finally:
            with anyio.CancelScope(shield=True):
                await frames.aclose()

    await asyncio.gather(*(read_one(i, m) for i, m in enumerate(members)))
    result.lines = [item.line for item in merger.result() if isinstance(item.line, StackLine)]
    result.cut = merger.cut
    result.failed.sort()
    return result


class StackLogStream:
    """The merged live log of a stack: the history, then lines as members print them.

    Holds no database session and one view slot until `aclose`, which every path must call.
    Iterate it for `LogBatch` (up to 200 lines of one stream; `event_id` is the newest timestamp
    delivered across members), `KEEP_ALIVE` and `StackLogNotice` items. `end_reason` says why
    iteration ended: "lifetime" means the caller should invite a reconnect, "finished" that
    every member's output ended, "unavailable" that every member failed.
    """

    def __init__(
        self,
        members: Sequence[_Member],
        info: StackLogMembers,
        *,
        query: LogQuery,
        resume_after: int | None,
        slot: StreamSlot,
        limits: LogLimits,
    ) -> None:
        self._members = tuple(members)
        self._query = query
        self._resume_after = resume_after
        self._slot = slot
        self._limits = limits
        self._high_water = resume_after
        self._events = self._generate()
        self.info = info
        self.end_reason: StreamEnd | None = None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StackLogItem:
        return await self._events.__anext__()

    async def aclose(self) -> None:
        """End the stream: close every Engine response and give the slot back; safe to repeat."""
        try:
            with anyio.CancelScope(shield=True):
                await self._events.aclose()
        finally:
            self._slot.close()
            if self.end_reason is None:
                self.end_reason = "closed"

    def _marked(self, batch: LogBatch) -> LogBatch:
        for line in batch.lines:
            stamp = line.timestamp_ns
            if stamp is not None and (self._high_water is None or stamp > self._high_water):
                self._high_water = stamp
        return replace(batch, high_water=self._high_water)

    async def _history(self) -> tuple[_Read, list[StackLogNotice]]:
        query = self._query
        tail = DEFAULT_TAIL if query.tail is None else min(query.tail, DEFAULT_TAIL)
        read = await _read_members(
            self._members,
            query,
            tail=tail,
            count=len(self._members) * tail,
            max_bytes=MERGE_BYTES,
            seconds=PAGE_SECONDS,
            frame_bytes=PAGE_FRAME_BYTES,
        )
        notices: list[StackLogNotice] = []
        if self.info.unreadable:
            notices.append(StackLogNotice(_unreadable_text(self.info.unreadable)))
        notices.extend(
            StackLogNotice(f"[output of {self._members[i].prefix} could not be read]")
            for i in read.failed
        )
        if read.cut:
            notices.append(StackLogNotice("[older lines were left out of the merged output]"))
        if read.timed_out:
            notices.append(
                StackLogNotice(
                    f"[older lines may be missing: reading stopped at the {PAGE_SECONDS:g}-second"
                    " limit]"
                )
            )
        return read, notices

    async def _generate(self) -> AsyncGenerator[StackLogItem, None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._limits.lifetime
        readers: list[asyncio.Task[None]] = []
        try:
            marks: dict[int, int] = {}
            failed: set[int] = set()
            if self._resume_after is None:
                read, notices = await self._history()
                if self._members and len(read.failed) == len(self._members):
                    self.end_reason = "unavailable"
                    return
                for notice in notices:
                    yield notice
                for batch in _batches(read.lines):
                    yield self._marked(batch)
                marks, failed = read.marks, set(read.failed)
            following = [
                i
                for i, member in enumerate(self._members)
                if self._query.follow and member.live and i not in failed
            ]
            if not following:
                self.end_reason = "finished"
                return
            queue: asyncio.Queue[_Lines | _Ended] = asyncio.Queue(maxsize=QUEUE_ITEMS)
            for index in following:
                floor = marks.get(index, self._resume_after)
                readers.append(
                    asyncio.create_task(self._follow(queue, index, floor), name="stack-log")
                )
            running = len(following)
            stopped_badly = 0
            while running:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    self.end_reason = "lifetime"
                    return
                first = await _take(queue, min(self._limits.keepalive, remaining))
                if first is None:
                    if deadline - loop.time() > 0:
                        yield KEEP_ALIVE
                    continue
                items = [first]
                size = first.size
                flush_at = loop.time() + self._limits.flush
                while size < self._limits.batch_bytes:
                    more = await _take(queue, flush_at - loop.time())
                    if more is None:
                        break
                    items.append(more)
                    size += more.size
                lines: list[StackLine] = []
                stopped: list[StackLogNotice] = []
                for item in items:
                    if isinstance(item, _Lines):
                        lines.extend(item.lines)
                        continue
                    running -= 1
                    if item.failed:
                        stopped_badly += 1
                        stopped.append(
                            StackLogNotice(
                                f"[output of {self._members[item.index].prefix} stopped: "
                                "the Docker host stopped answering]"
                            )
                        )
                for batch in _batches(lines):
                    yield self._marked(batch)
                for notice in stopped:
                    yield notice
            self.end_reason = "unavailable" if stopped_badly == len(following) else "finished"
        finally:
            for reader in readers:
                reader.cancel()
            try:
                with anyio.CancelScope(shield=True):
                    await asyncio.gather(*readers, return_exceptions=True)
            finally:
                self._slot.close()

    async def _follow(
        self, queue: "asyncio.Queue[_Lines | _Ended]", index: int, floor: int | None
    ) -> None:
        member = self._members[index]
        query = self._query
        source = member.source(dedupe=True, floor=floor)
        frames = FrameReader(
            source.frames(
                FrameRequest(
                    follow=True,
                    stdout=query.stdout,
                    stderr=query.stderr,
                    since_ns=floor,
                    since_exclusive=floor is not None,
                    tail=0 if floor is None else None,
                )
            )
        )
        splitter = LogLineSplitter()
        failed = False
        try:
            while True:
                frame = await frames.next(None)
                if isinstance(frame, ReadTimeout):
                    continue
                lines = splitter.flush() if frame is None else await splitter.split([frame])
                found = tuple(
                    member.line(line)
                    for line in source.lines(lines)
                    if floor is None or line.timestamp_ns is None or line.timestamp_ns > floor
                )
                if found:
                    await queue.put(_Lines(found))
                if frame is None:
                    break
        except EngineError as error:
            logger.warning(
                "stack_log_follow_failed",
                extra={"service": member.service, "error": type(error).__name__},
            )
            failed = True
        finally:
            with anyio.CancelScope(shield=True):
                await frames.aclose()
        await queue.put(_Ended(index, failed))


async def _take(queue: "asyncio.Queue[_Lines | _Ended]", wait: float) -> _Lines | _Ended | None:
    try:
        return queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    if wait <= 0:
        return None
    try:
        return await asyncio.wait_for(queue.get(), wait)
    except TimeoutError:
        return None


class StackLogDownload:
    """A capped, streamed rendering of a stack's merged logs.

    Holds no database session. A download of the newest lines (`tail` set) is read before the
    object exists and holds no slot; one without a tail (`tail=all`) holds one view slot and the
    Engine responses until `aclose`, which every path must call. Iterating yields UTF-8 chunks;
    a capped download ends with one line naming the limit.
    """

    def __init__(self, filename: str, query: LogQuery) -> None:
        self.filename = filename
        self.media_type = DOWNLOAD_MEDIA_TYPES[query.format]
        self.end_reason: Literal["finished", "capped", "unavailable", "closed"] | None = None
        self._chunks: AsyncGenerator[bytes, None] = self._generate()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        return await self._chunks.__anext__()

    async def prime(self) -> None:
        """Read ahead of the response, so a failing host is reported before it starts."""

    async def aclose(self) -> None:
        """End the download; safe to repeat."""
        with anyio.CancelScope(shield=True):
            await self._chunks.aclose()
        if self.end_reason is None:
            self.end_reason = "closed"

    def _generate(self) -> AsyncGenerator[bytes, None]:
        raise NotImplementedError


class _MergedDownload(StackLogDownload):
    def __init__(
        self,
        read: _Read,
        members: Sequence[_Member],
        *,
        query: LogQuery,
        filename: str,
        limits: LogLimits,
        unreadable: int,
        context: LogContext,
    ) -> None:
        self._read = read
        self._members = tuple(members)
        self._query = query
        self._limits = limits
        self._unreadable = unreadable
        self._context = context
        super().__init__(filename, query)

    async def _generate(self) -> AsyncGenerator[bytes, None]:
        query, limits = self._query, self._limits
        if self._read.cut:
            yield render_notice(
                query,
                f"[older output was left out: the download is limited to {limits.download_lines}"
                f" lines and {limits.download_bytes // (1024 * 1024)} MiB]",
            )
        if self._unreadable:
            yield render_notice(query, _unreadable_text(self._unreadable))
        written_lines = written_bytes = 0
        capped: Literal["lines", "bytes"] | None = None
        pending: list[bytes] = []
        lines = self._read.lines
        for start in range(0, len(lines), MAX_BATCH_LINES):
            rendered = await render_batch(
                query,
                lines[start : start + MAX_BATCH_LINES],
                lambda line: _render(query, line, self._context),
            )
            for encoded in rendered:
                if encoded is None:
                    continue
                if written_lines >= limits.download_lines:
                    capped = "lines"
                    break
                if written_bytes + len(encoded) > limits.download_bytes:
                    capped = "bytes"
                    break
                pending.append(encoded)
                written_lines += 1
                written_bytes += len(encoded)
            if pending:
                yield b"".join(pending)
                pending.clear()
            if capped is not None:
                break
        if capped is not None:
            yield render_notice(query, cap_line(capped, limits))
        elif self._read.timed_out:
            yield render_notice(query, cap_line("seconds", limits))
        for index in self._read.failed:
            yield render_notice(
                query, f"[output of {self._members[index].prefix} could not be read]"
            )
        self.end_reason = "capped" if capped or self._read.timed_out else "finished"


class _Unread:
    __slots__ = ()


_UNREAD = _Unread()


@dataclass(slots=True)
class _Open:
    member: _Member
    frames: FrameReader
    source: LogSource
    splitter: LogLineSplitter
    head: LogFrame | ReadTimeout | _Unread | None


class _SequentialDownload(StackLogDownload):
    def __init__(
        self,
        members: Sequence[_Member],
        *,
        query: LogQuery,
        filename: str,
        slot: StreamSlot,
        limits: LogLimits,
        unreadable: int,
        context: LogContext,
    ) -> None:
        self._members = tuple(members)
        self._query = query
        self._slot = slot
        self._limits = limits
        self._unreadable = unreadable
        self._context = context
        self._cursor = 0
        self._current: _Open | None = None
        self._failed: list[_Member] = []
        self._deadline: float | None = None
        super().__init__(filename, query)

    def _remaining(self) -> float:
        assert self._deadline is not None
        return self._deadline - asyncio.get_running_loop().time()

    async def aclose(self) -> None:
        """End the download: close the Engine response and give the slot back; safe to repeat."""
        try:
            await super().aclose()
            await self._release()
        finally:
            self._slot.close()

    async def _release(self) -> None:
        current, self._current = self._current, None
        if current is not None:
            with anyio.CancelScope(shield=True):
                await current.frames.aclose()

    async def prime(self) -> None:
        """Open members until one answers; LogsUnavailable when every member fails first.

        The download is closed first, on that path and when the caller is cancelled.
        """
        self._deadline = asyncio.get_running_loop().time() + self._limits.download_seconds
        try:
            while self._current is None and self._cursor < len(self._members):
                member = self._members[self._cursor]
                self._cursor += 1
                try:
                    self._current = await self._open(member)
                except EngineError:
                    self._failed.append(member)
            if self._current is None and self._members:
                raise LogsUnavailable("stack")
        except BaseException:
            await self.aclose()
            raise

    async def _open(self, member: _Member) -> _Open:
        source = member.source()
        query = self._query
        frames = FrameReader(
            source.frames(
                FrameRequest(
                    follow=False,
                    stdout=query.stdout,
                    stderr=query.stderr,
                    since_ns=query.since_ns,
                    until_ns=query.until_ns,
                    tail=None,
                )
            )
        )
        try:
            head = await frames.next(self._remaining())
        except BaseException:
            with anyio.CancelScope(shield=True):
                await frames.aclose()
            raise
        return _Open(member, frames, source, LogLineSplitter(), head)

    async def _generate(self) -> AsyncGenerator[bytes, None]:
        query, limits = self._query, self._limits
        if self._deadline is None:
            self._deadline = asyncio.get_running_loop().time() + limits.download_seconds
        written_lines = written_bytes = 0
        try:
            if self._unreadable:
                yield render_notice(query, _unreadable_text(self._unreadable))
            while True:
                if self._current is None:
                    if self._cursor >= len(self._members):
                        break
                    member = self._members[self._cursor]
                    self._cursor += 1
                    try:
                        self._current = await self._open(member)
                    except EngineError:
                        self._failed.append(member)
                        continue
                current = self._current
                while True:
                    try:
                        if isinstance(current.head, _Unread):
                            frame = await current.frames.next(self._remaining())
                        else:
                            frame, current.head = current.head, _UNREAD
                    except EngineError:
                        self._failed.append(current.member)
                        break
                    if isinstance(frame, ReadTimeout):
                        yield render_notice(query, cap_line("seconds", limits))
                        self.end_reason = "capped"
                        return
                    splitter = current.splitter
                    lines = splitter.flush() if frame is None else await splitter.split([frame])
                    out: list[bytes] = []
                    capped: Literal["lines", "bytes"] | None = None
                    member_lines = [
                        current.member.line(line) for line in current.source.lines(lines)
                    ]
                    rendered = await render_batch(
                        query,
                        member_lines,
                        lambda line: _render(query, line, self._context),
                    )
                    for encoded in rendered:
                        if encoded is None:
                            continue
                        if written_lines >= limits.download_lines:
                            capped = "lines"
                            break
                        if written_bytes + len(encoded) > limits.download_bytes:
                            capped = "bytes"
                            break
                        out.append(encoded)
                        written_lines += 1
                        written_bytes += len(encoded)
                    if out:
                        yield b"".join(out)
                    if capped is not None:
                        yield render_notice(query, cap_line(capped, limits))
                        self.end_reason = "capped"
                        return
                    if frame is None:
                        break
                await self._release()
            for member in self._failed:
                yield render_notice(query, f"[output of {member.prefix} could not be read]")
            self.end_reason = "finished"
        finally:
            await self._release()
            self._slot.close()


class StackLogService:
    """Authorises a stack at read access, then hands out its members' logs merged.

    Compose members are the project's containers, stopped ones included; Swarm members are the
    namespace's services. Only members the caller may read are read, the rest are counted and
    never named. Methods whose names start `compose_` take a Compose stack name, those starting
    `swarm_` a Swarm stack namespace; both give ResourceNotFound for a stack the caller may not
    see, before any Engine request. The view slot is claimed right after authorisation and
    before the members are listed.
    EnvironmentUnavailable and ObjectTooLarge (or their list-too-large forms) when the host
    cannot be read, TooManyStreams when the user, the process or the environment has no free
    view slot. `services` is the filter: repeated values from the query, unknown names ignored,
    none left meaning every service.
    """

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        stacks: "StackQueryService",
        rows: StackRepository,
        slots: StreamSlots,
        limits: LogLimits | None = None,
        poke: Callable[[int], None] | None = None,
        instance_name: str = DEFAULT_INSTANCE_NAME,
    ) -> None:
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._stacks = stacks
        self._rows = rows
        self._slots = slots
        self._limits = limits or LogLimits()
        self._poke: Callable[[int], None] = poke or (lambda _: None)
        self._instance_name = instance_name

    async def compose_members(
        self, actor: Principal, environment_id: int, stack: str, services: Collection[str] = ()
    ) -> StackLogMembers:
        """Return what the Compose stack's log view covers, reading no logs."""
        stored = await self._stacks.require_stack(actor, environment_id, stack, "read")
        plan = await self._compose_plan(actor, environment_id, stored, services, resolve=False)
        return plan.info

    async def swarm_members(
        self, actor: Principal, environment_id: int, namespace: str, services: Collection[str] = ()
    ) -> StackLogMembers:
        """Return what the Swarm stack's log view covers, reading no logs."""
        await self._swarm_gate(actor, environment_id, namespace)
        plan = await self._swarm_plan(actor, environment_id, namespace, services, resolve=False)
        return plan.info

    async def compose_open(
        self,
        actor: Principal,
        environment_id: int,
        stack: str,
        query: LogQuery,
        services: Collection[str] = (),
        last_event_id: str | None = None,
        after: str | None = None,
    ) -> StackLogStream:
        """Open the merged live log of a Compose stack; the caller must close the stream.

        The history is at most 500 lines per member; a reconnecting browser's Last-Event-ID, or
        `after`, resumes every member after the nanosecond timestamp it names and drops it.
        """
        stored = await self._stacks.require_stack(actor, environment_id, stack, "read")
        slot = self._claim(actor, environment_id)
        plan = await self._guarded(
            slot, self._compose_plan(actor, environment_id, stored, services)
        )
        return self._stream(plan, slot, query, last_event_id, after)

    async def swarm_open(
        self,
        actor: Principal,
        environment_id: int,
        namespace: str,
        query: LogQuery,
        services: Collection[str] = (),
        last_event_id: str | None = None,
        after: str | None = None,
    ) -> StackLogStream:
        """Open the merged live log of a Swarm stack; the caller must close the stream."""
        await self._swarm_gate(actor, environment_id, namespace)
        slot = self._claim(actor, environment_id)
        plan = await self._guarded(
            slot, self._swarm_plan(actor, environment_id, namespace, services)
        )
        return self._stream(plan, slot, query, last_event_id, after)

    async def compose_download(
        self,
        actor: Principal,
        environment_id: int,
        stack: str,
        query: LogQuery,
        services: Collection[str] = (),
    ) -> StackLogDownload:
        """Prepare a capped download of a Compose stack's merged logs; the caller must close it."""
        stored = await self._stacks.require_stack(actor, environment_id, stack, "read")
        slot = self._claim(actor, environment_id)
        plan = await self._guarded(
            slot, self._compose_plan(actor, environment_id, stored, services)
        )
        return await self._download(plan, slot, environment_id, query)

    async def swarm_download(
        self,
        actor: Principal,
        environment_id: int,
        namespace: str,
        query: LogQuery,
        services: Collection[str] = (),
    ) -> StackLogDownload:
        """Prepare a capped download of a Swarm stack's merged logs; the caller must close it."""
        await self._swarm_gate(actor, environment_id, namespace)
        slot = self._claim(actor, environment_id)
        plan = await self._guarded(
            slot, self._swarm_plan(actor, environment_id, namespace, services)
        )
        return await self._download(plan, slot, environment_id, query)

    async def compose_tail(
        self,
        actor: Principal,
        environment_id: int,
        stack: str,
        query: LogQuery,
        services: Collection[str] = (),
    ) -> StackLogTail:
        """Read the newest merged lines of a Compose stack for a page without scripting."""
        stored = await self._stacks.require_stack(actor, environment_id, stack, "read")
        slot = self._claim(actor, environment_id)
        plan = await self._guarded(
            slot, self._compose_plan(actor, environment_id, stored, services)
        )
        return await self._tail(plan, slot, query)

    async def swarm_tail(
        self,
        actor: Principal,
        environment_id: int,
        namespace: str,
        query: LogQuery,
        services: Collection[str] = (),
    ) -> StackLogTail:
        """Read the newest merged lines of a Swarm stack for a page without scripting."""
        await self._swarm_gate(actor, environment_id, namespace)
        slot = self._claim(actor, environment_id)
        plan = await self._guarded(
            slot, self._swarm_plan(actor, environment_id, namespace, services)
        )
        return await self._tail(plan, slot, query)

    def _claim(self, actor: Principal, environment_id: int) -> StreamSlot:
        return self._slots.acquire_stack(
            actor.user_id, environment_id, is_administrator=actor.is_administrator
        )

    @staticmethod
    async def _guarded(slot: StreamSlot, planning: Awaitable[_Plan]) -> _Plan:
        try:
            return await planning
        except BaseException:
            slot.close()
            raise

    def _stream(
        self,
        plan: _Plan,
        slot: StreamSlot,
        query: LogQuery,
        last_event_id: str | None,
        after: str | None,
    ) -> StackLogStream:
        return StackLogStream(
            plan.members,
            plan.info,
            query=query,
            resume_after=resume_point(last_event_id, after),
            slot=slot,
            limits=self._limits,
        )

    async def _download(
        self, plan: _Plan, slot: StreamSlot, environment_id: int, query: LogQuery
    ) -> StackLogDownload:
        limits = self._limits
        try:
            context = await read_log_context(self._hosts, environment_id, self._instance_name)
        except BaseException:
            slot.close()
            raise
        filename = _download_name(plan.info.name, datetime.now(UTC), query)
        if query.tail is None:
            download = _SequentialDownload(
                plan.members,
                query=query,
                filename=filename,
                slot=slot,
                limits=limits,
                unreadable=plan.info.unreadable,
                context=context,
            )
            await download.prime()
            return download
        tail = min(query.tail, MAX_TAIL)
        try:
            read = await _read_members(
                plan.members,
                query,
                tail=tail,
                count=min(len(plan.members) * tail, limits.download_lines),
                max_bytes=limits.download_bytes,
                seconds=limits.download_seconds,
                frame_bytes=LOG_FRAME_BYTES,
            )
        finally:
            slot.close()
        if plan.members and len(read.failed) == len(plan.members):
            raise LogsUnavailable("stack")
        return _MergedDownload(
            read,
            plan.members,
            query=query,
            filename=filename,
            limits=limits,
            unreadable=plan.info.unreadable,
            context=context,
        )

    async def _tail(self, plan: _Plan, slot: StreamSlot, query: LogQuery) -> StackLogTail:
        tail = min(DEFAULT_PAGE_TAIL if query.tail is None else query.tail, MAX_PAGE_TAIL)
        try:
            read = await _read_members(
                plan.members,
                query,
                tail=tail,
                count=len(plan.members) * tail,
                max_bytes=self._limits.page_bytes,
                seconds=PAGE_SECONDS,
                frame_bytes=PAGE_FRAME_BYTES,
            )
        finally:
            slot.close()
        if plan.members and len(read.failed) == len(plan.members):
            raise LogsUnavailable("stack")
        return StackLogTail(
            tuple(read.lines),
            read.cut or read.timed_out,
            plan.info,
            plan.info.unreadable + len(read.failed),
        )

    async def _compose_plan(
        self,
        actor: Principal,
        environment_id: int,
        stack: StoredStack,
        services: Collection[str],
        *,
        resolve: bool = True,
    ) -> _Plan:
        client = await self._hosts.client(environment_id)
        key = canonical_stack_key(stack.name)
        found = [
            c
            for c in await read_labelled_containers(client, COMPOSE_PROJECT_LABEL)
            if in_project(c.labels, key)
        ]
        permitted = await permitted_container_ids(self._authoriser, actor, environment_id, found)
        readable = sorted(
            (_Candidate(_service_of(c), _number_of(c), c) for c in found if c.id in permitted),
            key=lambda e: (e.service.casefold(), e.service, e.number, e.container.id),
        )
        names = tuple(sorted({entry.service for entry in readable}))
        chosen = _choose(names, services)
        selected = [entry for entry in readable if entry.service in chosen]
        shown = selected[:MAX_MEMBERS]
        members: list[_Member] = []
        unreadable = 0
        if resolve and shown:
            members, unreadable = await self._compose_members(environment_id, client, shown)
        info = StackLogMembers(
            name=stack.name,
            services=names,
            selected=tuple(sorted(chosen)),
            members=len(shown) - unreadable if not resolve else len(members),
            hidden=len(found) - len(readable),
            over_cap=len(selected) - len(shown),
            unreadable=unreadable,
        )
        return _Plan(tuple(members), info)

    async def _compose_members(
        self, environment_id: int, client: EngineClient, shown: Sequence[_Candidate]
    ) -> tuple[list[_Member], int]:
        inspected = await asyncio.gather(
            *(self._inspector.inspect(environment_id, c.container.id) for c in shown),
            return_exceptions=True,
        )
        counts: dict[str, int] = {}
        for entry in shown:
            counts[entry.service] = counts.get(entry.service, 0) + 1
        members: list[_Member] = []
        unreadable = 0
        failure: Exception | None = None
        for entry, outcome in zip(shown, inspected, strict=True):
            if isinstance(outcome, ResourceNotFound):
                continue
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, Exception):
                    raise outcome
                unreadable += 1
                failure = failure or outcome
                continue
            container = entry.container
            prefix = entry.service
            if counts[entry.service] > 1:
                prefix = f"{entry.service}.{entry.number or container.id[:12]}"
            members.append(
                _Member(
                    service=entry.service,
                    name=display_name(container.names, container.id),
                    prefix=prefix,
                    object_id=container.id,
                    client=client,
                    tty=outcome.config.tty,
                    live=container.state in LIVE_STATES,
                    keys=container_source(
                        container_id=container.id,
                        container_name=display_name(container.names, container.id),
                        image=container.image,
                        labels=container.labels,
                    ),
                )
            )
        if failure is not None and not members:
            raise failure
        return members, unreadable

    async def _swarm_gate(self, actor: Principal, environment_id: int, namespace: str) -> None:
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        if NAMESPACE_ROUTE.fullmatch(namespace) is None:
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "stack", namespace), "read"
        )

    async def _swarm_plan(
        self,
        actor: Principal,
        environment_id: int,
        namespace: str,
        services: Collection[str],
        *,
        resolve: bool = True,
    ) -> _Plan:
        client = await self._hosts.client(environment_id)
        found = await read_namespace(
            client, environment_id, self._poke, canonical_stack_key(namespace)
        )
        if not found.count:
            row = await self._rows.get_folded(environment_id, namespace)
            if row is None or row.kind != "swarm":
                raise ResourceNotFound("not found")
        refs = [ResourceRef(environment_id, "service", s.id, s.spec.labels) for s in found.services]
        allowed = {r.resource_id for r in await self._authoriser.filter_visible(actor, refs)}
        readable = sorted(
            (s for s in found.services if s.id in allowed),
            key=lambda s: (s.spec.name.casefold(), s.spec.name, s.id),
        )
        names = tuple(sorted({s.spec.name for s in readable}))
        chosen = _choose(names, services)
        selected = [s for s in readable if s.spec.name in chosen]
        shown = selected[:MAX_MEMBERS]
        members = await self._swarm_members(environment_id, client, shown) if resolve else []
        info = StackLogMembers(
            name=namespace,
            services=names,
            selected=tuple(sorted(chosen)),
            members=len(members) if resolve else len(shown),
            hidden=len(found.services) - len(readable),
            over_cap=len(selected) - len(shown),
        )
        return _Plan(tuple(members), info)

    async def _swarm_members(
        self, environment_id: int, client: EngineClient, shown: Sequence[SwarmService]
    ) -> list[_Member]:
        task_lists = await asyncio.gather(
            *(
                swarm_read(
                    client.swarm_tasks.list(filters={"service": [service.id]}),
                    environment_id,
                    self._poke,
                )
                for service in shown
            )
        )
        hostnames: dict[str, str] = {}
        if any("Global" in service.spec.mode for service in shown):
            nodes = await swarm_read(client.swarm_nodes.list(), environment_id, self._poke)
            hostnames = {
                node_id: label_safe_hostname(name)
                for node_id, name in node_hostnames(nodes).items()
            }
        members: list[_Member] = []
        for service, tasks in zip(shown, task_lists, strict=True):
            is_global = "Global" in service.spec.mode
            name = label_safe_hostname(service.spec.name)
            prefixes: dict[str, str] = {}
            for task in tasks[:MAX_TASKS_LABELLED]:
                if is_global:
                    where = hostnames.get(task.node_id, "-")
                else:
                    where = str(task.slot) if task.slot is not None else "-"
                prefixes[task.id] = f"{name}.{where}"
            container = service.spec.task_template.get("ContainerSpec")
            members.append(
                _Member(
                    service=service.spec.name,
                    name=None,
                    prefix=name,
                    object_id=service.id,
                    client=client,
                    tty=isinstance(container, dict) and container.get("TTY") is True,
                    live=True,
                    keys=service_source(
                        service_id=service.id,
                        service_name=service.spec.name,
                        image=service_image(service.spec.task_template),
                        labels=service.spec.labels,
                    ),
                    task_prefixes=prefixes,
                )
            )
        return members
