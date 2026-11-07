"""Container logs: the bounded live stream, the capped download, and the server-side tail."""

import asyncio
import re
from collections import deque
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, Protocol, Self, cast

import anyio

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.config import DEFAULT_INSTANCE_NAME
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError
from app.docker_engine.frames import LogFrame
from app.docker_engine.resources import LOG_FRAME_BYTES, format_engine_timestamp
from app.logging import DEFAULT_SCRUBBER
from app.services.access import ResourceKey, is_canonical_id
from app.services.activity_ndjson import NDJSON_MEDIA_TYPE
from app.services.authorisation import Authoriser
from app.services.container_stats import (
    KEEP_ALIVE,
    KEEPALIVE_SECONDS,
    MAX_STREAM_SECONDS,
    KeepAlive,
    StreamEnd,
)
from app.services.containers import display_name, short_id
from app.services.log_lines import INLINE_DECODE_BYTES, LogLine, LogLineSplitter
from app.services.log_ndjson import (
    LogContext,
    SourceKeys,
    container_source,
    encode_log_line,
    encode_notice,
    for_task,
)
from app.services.object_inspector import EngineObjectInspector, EnvironmentUnavailable, HostClients
from app.services.object_locks import StreamSlot, StreamSlots

MAX_TAIL = 10_000
DEFAULT_TAIL = 500
MAX_PAGE_TAIL = 1_000
LAST_OUTPUT_LINES = 20
DEFAULT_PAGE_TAIL = 200
MAX_FILTER_CHARS = 128
MAX_BATCH_LINES = 200
FLUSH_SECONDS = 0.1
BATCH_BYTES = 128 * 1024
RETRY_MILLISECONDS = 3000
LAST_EVENT_ID = re.compile(r"[0-9]{1,20}")
PAGE_BYTES = 1024 * 1024
PAGE_FRAME_BYTES = 64 * 1024
PAGE_SECONDS = 30.0
DOWNLOAD_MAX_LINES = 100_000
DOWNLOAD_MAX_BYTES = 32 * 1024 * 1024
DOWNLOAD_MAX_SECONDS = 120.0
DOWNLOAD_MEDIA_TYPE = "text/plain; charset=utf-8"
LOG_FORMATS: Final = ("text", "ndjson")
DOWNLOAD_MEDIA_TYPES: Final = {"text": DOWNLOAD_MEDIA_TYPE, "ndjson": NDJSON_MEDIA_TYPE}
DOWNLOAD_SUFFIXES: Final = {"text": ".log", "ndjson": ".ndjson"}
LIVE_STATES = frozenset({"running", "restarting", "paused"})

_RELATIVE = re.compile(r"([0-9]{1,4})([smhd])")
_EPOCH_SECONDS = re.compile(r"([0-9]{1,12})(?:\.([0-9]{1,9}))?")
_UNIT_SECONDS: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NANOS = 1_000_000_000
_LATEST_NS = (datetime(2200, 1, 1, tzinfo=UTC) - _EPOCH) // timedelta(seconds=1) * _NANOS
_MAX_TIME_CHARS = 40
_FALSE = frozenset({"0", "false", "off", "no"})
_TRUE = frozenset({"1", "true", "on", "yes"})
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_FILENAME_NAME_CHARS = 64
UNAVAILABLE_LINE = "[output stopped: the Docker host stopped answering]"
_TIME_HINT = "Enter a date and time such as 2026-10-19T14:30 (UTC), or a duration such as 15m."


type LogFormat = Literal["text", "ndjson"]


class ReadTimeout:
    """Marks a wait that ended with nothing to read."""

    __slots__ = ()


TIMEOUT = ReadTimeout()


class _Unread:
    """Marks a download whose first frame has not been read ahead."""

    __slots__ = ()


_UNREAD = _Unread()


class LogsUnavailable(EnvironmentUnavailable):
    """The Docker host could not deliver an object's logs; the message holds no Engine text."""

    def __init__(self, subject: str = "container") -> None:
        super().__init__("engine")
        self.args = (f"Logs are not available for this {subject}.",)


@dataclass(frozen=True, slots=True)
class LogLimits:
    """Timing and size bounds of a log stream; the defaults are the product's."""

    keepalive: float = KEEPALIVE_SECONDS
    lifetime: float = MAX_STREAM_SECONDS
    flush: float = FLUSH_SECONDS
    batch_bytes: int = BATCH_BYTES
    download_lines: int = DOWNLOAD_MAX_LINES
    download_bytes: int = DOWNLOAD_MAX_BYTES
    download_seconds: float = DOWNLOAD_MAX_SECONDS
    page_bytes: int = PAGE_BYTES


@dataclass(frozen=True, slots=True)
class LogQuery:
    """A validated log selection; tail None means every line, times are Unix nanoseconds."""

    tail: int | None = DEFAULT_TAIL
    since_ns: int | None = None
    until_ns: int | None = None
    stdout: bool = True
    stderr: bool = True
    follow: bool = True
    timestamps: bool = True
    filter: str = ""
    format: LogFormat = "text"

    @classmethod
    def from_params(
        cls,
        params: Mapping[str, str],
        *,
        default_tail: int = DEFAULT_TAIL,
        max_tail: int = MAX_TAIL,
        allow_all: bool = False,
        now: datetime | None = None,
    ) -> Self:
        """Read a query string; ValidationFailed, keyed by field, for every unusable value.

        `since` and `until` take a duration back from now (`15m`, `6h`, `7d`), Unix seconds, or
        an ISO 8601 date and time read as UTC when it names no zone; `since` also takes `all`.
        Setting `until` turns follow off. `stdout` and `stderr` are both on unless one of them is
        given, and then only those given as on are. `format` is `text` (the default) or `ndjson`
        and only changes how a download is written.
        """
        moment = now or datetime.now(UTC)
        errors: dict[str, str] = {}
        tail = _tail(params.get("tail", ""), default_tail, max_tail, allow_all, errors)
        since = _time(params.get("since", ""), "since", moment, errors, allow_all_word=True)
        until = _time(params.get("until", ""), "until", moment, errors, allow_all_word=False)
        if since is not None and until is not None and until <= since:
            errors["until"] = "Until must be after since."
        out, err = _streams(params, errors)
        text = params.get("q", "")
        if len(text) > MAX_FILTER_CHARS or any(ord(char) < 32 for char in text):
            errors["q"] = f"The filter is at most {MAX_FILTER_CHARS} characters of plain text."
        follow = _flag(params.get("follow"), default=True) and until is None
        timestamps = _flag(params.get("timestamps"), default=True)
        log_format = params.get("format", "text")
        if log_format not in LOG_FORMATS:
            errors["format"] = "Choose text or ndjson."
        if errors:
            raise ValidationFailed(errors)
        return cls(
            tail, since, until, out, err, follow, timestamps, text, cast(LogFormat, log_format)
        )


def _flag(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    text = value.strip().lower()
    if text in _FALSE:
        return False
    return True if text in _TRUE else default


def _streams(params: Mapping[str, str], errors: dict[str, str]) -> tuple[bool, bool]:
    given_out, given_err = params.get("stdout"), params.get("stderr")
    if given_out is None and given_err is None:
        return True, True
    out, err = _flag(given_out, default=False), _flag(given_err, default=False)
    if not (out or err):
        errors["streams"] = "Select at least one of stdout and stderr."
    return out, err


def _tail(
    text: str, default: int, maximum: int, allow_all: bool, errors: dict[str, str]
) -> int | None:
    text = text.strip().lower()
    if not text:
        return default
    if allow_all and text == "all":
        return None
    if (
        text.isascii()
        and text.isdigit()
        and len(text) <= len(str(maximum))
        and int(text) <= maximum
    ):
        return int(text)
    errors["tail"] = f"Choose a number of lines from 0 to {maximum}."
    return default


def _time(
    text: str,
    field: str,
    now: datetime,
    errors: dict[str, str],
    *,
    allow_all_word: bool,
) -> int | None:
    text = text.strip()
    if not text or (allow_all_word and text.lower() == "all"):
        return None
    moment = _parse_time(text, now) if len(text) <= _MAX_TIME_CHARS else None
    if moment is None:
        errors[field] = _TIME_HINT
        return None
    return moment


def _datetime_ns(moment: datetime) -> int:
    return (moment - _EPOCH) // timedelta(microseconds=1) * 1000


def _parse_time(text: str, now: datetime) -> int | None:
    relative = _RELATIVE.fullmatch(text)
    if relative is not None:
        return _datetime_ns(now - timedelta(seconds=int(relative[1]) * _UNIT_SECONDS[relative[2]]))
    epoch = _EPOCH_SECONDS.fullmatch(text)
    if epoch is not None:
        moment_ns = int(epoch[1]) * _NANOS + int((epoch[2] or "").ljust(9, "0"))
        return moment_ns if moment_ns < _LATEST_NS else None
    try:
        parsed = datetime.fromisoformat(text)
        moment = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        moment_ns = _datetime_ns(moment)
    except (ValueError, OverflowError):
        return None
    return moment_ns if 0 <= moment_ns < _LATEST_NS else None


@dataclass(frozen=True, slots=True)
class OriginLine(LogLine):
    """A log line with the label of the task that wrote it; "-" when the task is unknown.

    `task_id` and `node_id` are the 25-character ids Swarm's attribute token named, None when it
    named none.
    """

    origin: str = "-"
    task_id: str | None = None
    node_id: str | None = None


@dataclass(frozen=True, slots=True)
class LogRecords:
    """What turns a line into an NDJSON record: the context and the source's key set."""

    context: LogContext
    source: SourceKeys

    def encode(self, line: LogLine) -> bytes:
        """Return the line's record; a line that names a task and node carries them."""
        if isinstance(line, OriginLine):
            return encode_log_line(
                line, self.context, for_task(self.source, line.task_id, line.node_id)
            )
        return encode_log_line(line, self.context, self.source)


async def read_log_context(
    hosts: HostClients, environment_id: int, instance_name: str
) -> LogContext:
    """Read the environment's name and Engine host name for the records of one download or run."""
    environment = await hosts.environment(environment_id)
    return LogContext(instance_name, environment_id, environment.name, environment.engine_host_name)


@dataclass(frozen=True, slots=True)
class FrameRequest:
    """What a log source is asked for; times are Unix nanoseconds.

    `since_exclusive` marks a resume point: the line stamped at `since_ns` was already delivered.
    """

    follow: bool
    stdout: bool
    stderr: bool
    since_ns: int | None = None
    since_exclusive: bool = False
    until_ns: int | None = None
    tail: int | None = None
    max_frame_bytes: int | None = None


class LogSource(Protocol):
    """Where log frames come from and how the lines they make are finished."""

    @property
    def subject(self) -> str:
        """Name what the logs belong to, for messages: "container" or "service"."""
        ...

    def frames(self, request: FrameRequest) -> AsyncGenerator[LogFrame, None]:
        """Open the Engine stream for the request."""
        ...

    def lines(self, lines: list[LogLine]) -> list[LogLine]:
        """Finish lines the splitter made, for example by attaching their origin."""
        ...


def _stamp(nanoseconds: int | None) -> str | None:
    return None if nanoseconds is None else format_engine_timestamp(nanoseconds)


class ContainerLogSource:
    """The logs of one container."""

    subject = "container"

    def __init__(self, client: EngineClient, container_id: str, *, tty: bool) -> None:
        self._client = client
        self._container_id = container_id
        self._tty = tty

    def frames(self, request: FrameRequest) -> AsyncGenerator[LogFrame, None]:
        """Open the container's log stream."""
        return self._client.containers.logs(
            self._container_id,
            tty=self._tty,
            follow=request.follow,
            stdout=request.stdout,
            stderr=request.stderr,
            since=_stamp(request.since_ns),
            until=_stamp(request.until_ns),
            tail=request.tail,
            timestamps=True,
            max_frame_bytes=request.max_frame_bytes or LOG_FRAME_BYTES,
        )

    def lines(self, lines: list[LogLine]) -> list[LogLine]:
        """Return the lines unchanged."""
        return lines


@dataclass(frozen=True, slots=True)
class LogBatch:
    """Up to 200 consecutive lines of one stream, as one server-sent event."""

    stream: Literal["out", "err"]
    lines: tuple[LogLine, ...]
    high_water: int | None = None

    @property
    def data(self) -> str:
        """Return the event body: one `<timestamp> <text>` line per log line, `-` for none."""
        return "\n".join(_data_line(line) for line in self.lines)

    @property
    def event_id(self) -> int | None:
        """Return the highest timestamp delivered so far in nanoseconds, never lower than before.

        Without a high-water mark it is the last line's timestamp, None when it carried none.
        """
        return self.lines[-1].timestamp_ns if self.high_water is None else self.high_water


def _data_line(line: LogLine) -> str:
    if isinstance(line, OriginLine):
        return f"{line.timestamp or '-'} {line.origin} {line.text}"
    return f"{line.timestamp or '-'} {line.text}"


@dataclass(frozen=True, slots=True)
class LogTail:
    """The newest lines of a container's output for a page without scripting."""

    lines: tuple[LogLine, ...]
    cut: bool
    name: str
    short_id: str
    running: bool


def _batches(lines: Iterable[LogLine], resume_after: int | None) -> list[LogBatch]:
    batches: list[LogBatch] = []
    run: list[LogLine] = []
    stream: Literal["out", "err"] = "out"

    def flush() -> None:
        for start in range(0, len(run), MAX_BATCH_LINES):
            batches.append(LogBatch(stream, tuple(run[start : start + MAX_BATCH_LINES])))
        run.clear()

    for line in lines:
        if (
            resume_after is not None
            and line.timestamp_ns is not None
            and line.timestamp_ns <= resume_after
        ):
            continue
        name: Literal["out", "err"] = "err" if line.stream == "stderr" else "out"
        if run and name != stream:
            flush()
        stream = name
        run.append(line)
    flush()
    return batches


def resume_point(*reported: str | None) -> int | None:
    """Return the latest usable nanosecond timestamp among those a browser reports, else None.

    A value is usable when it is 1 to 20 decimal digits; anything else is ignored.
    """
    points = [
        int(value) for value in reported if value is not None and LAST_EVENT_ID.fullmatch(value)
    ]
    return max(points, default=None)


class FrameReader:
    """Reads an Engine log stream one frame at a time, with waits that keep their read."""

    def __init__(self, frames: AsyncGenerator[LogFrame, None]) -> None:
        self._frames = frames
        self._pending: asyncio.Future[LogFrame | None] | None = None

    async def _next(self) -> LogFrame | None:
        try:
            return await anext(self._frames)
        except StopAsyncIteration:
            return None

    async def next(self, wait: float | None) -> LogFrame | ReadTimeout | None:
        """Return the next frame, None at the end of the stream, or TIMEOUT.

        A timed-out read is kept and completes on a later call. EngineError when the stream
        failed.
        """
        if self._pending is None:
            self._pending = asyncio.ensure_future(self._next())
        done, _ = await asyncio.wait(
            {self._pending}, timeout=None if wait is None else max(wait, 0.0)
        )
        if not done:
            return TIMEOUT
        finished, self._pending = self._pending, None
        return finished.result()

    async def aclose(self) -> None:
        """Cancel a read in flight and close the Engine stream; call it shielded."""
        pending, self._pending = self._pending, None
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await self._frames.aclose()


class LogStream:
    """Live log batches of one source, a container or a service, bounded in time.

    Holds no database session. Iterate it and call `aclose` on every path: the stream slot is
    given back and the Engine response closed there. `end_reason` says why iteration ended:
    "lifetime" means the caller should invite a reconnect, "finished" that Docker ended the
    output (the container stopped or `until` was reached), "unavailable" that the host failed or
    sent a frame over the cap.
    """

    def __init__(
        self,
        source: LogSource,
        *,
        query: LogQuery,
        follow: bool,
        resume_after: int | None,
        slot: StreamSlot,
        limits: LogLimits,
    ) -> None:
        self._source = source
        self._query = query
        self._follow = follow
        self._resume_after = resume_after
        self._slot = slot
        self._limits = limits
        self._high_water = resume_after
        self._events = self._generate()
        self.end_reason: StreamEnd | None = None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> LogBatch | KeepAlive:
        return await self._events.__anext__()

    async def aclose(self) -> None:
        """End the stream: close the Engine response and give the slot back; safe to repeat."""
        try:
            with anyio.CancelScope(shield=True):
                await self._events.aclose()
        finally:
            self._slot.close()
            if self.end_reason is None:
                self.end_reason = "closed"

    def _open_frames(self) -> FrameReader:
        query = self._query
        since = query.since_ns
        if self._resume_after is not None:
            since = max(self._resume_after, since or 0)
        return FrameReader(
            self._source.frames(
                FrameRequest(
                    follow=self._follow,
                    stdout=query.stdout,
                    stderr=query.stderr,
                    since_ns=since,
                    since_exclusive=self._resume_after is not None and since == self._resume_after,
                    until_ns=query.until_ns,
                    tail=None if self._resume_after is not None else query.tail,
                )
            )
        )

    def _marked(self, batch: LogBatch) -> LogBatch:
        for line in batch.lines:
            stamp = line.timestamp_ns
            if stamp is not None and (self._high_water is None or stamp > self._high_water):
                self._high_water = stamp
        return replace(batch, high_water=self._high_water)

    async def _generate(self) -> AsyncGenerator[LogBatch | KeepAlive, None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._limits.lifetime
        frames = self._open_frames()
        splitter = LogLineSplitter()
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    self.end_reason = "lifetime"
                    return
                try:
                    first = await frames.next(min(self._limits.keepalive, remaining))
                except EngineError:
                    self.end_reason = "unavailable"
                    return
                if isinstance(first, ReadTimeout):
                    if deadline - loop.time() > 0:
                        yield KEEP_ALIVE
                    continue
                collected = [first] if first is not None else []
                ended = first is None
                failed = False
                size = len(first.data) if first is not None else 0
                flush_at = loop.time() + self._limits.flush
                while not ended and size < self._limits.batch_bytes:
                    try:
                        more = await frames.next(flush_at - loop.time())
                    except EngineError:
                        failed = True
                        break
                    if isinstance(more, ReadTimeout):
                        break
                    if more is None:
                        ended = True
                        break
                    collected.append(more)
                    size += len(more.data)
                lines = await splitter.split(collected) if collected else []
                if ended:
                    lines += splitter.flush()
                for batch in _batches(self._source.lines(lines), self._resume_after):
                    yield self._marked(batch)
                if failed:
                    self.end_reason = "unavailable"
                    return
                if ended:
                    self.end_reason = "finished"
                    return
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    await frames.aclose()
            finally:
                self._slot.close()


def cap_line(kind: Literal["lines", "bytes", "seconds"], limits: LogLimits) -> str:
    match kind:
        case "lines":
            return f"[output stopped at the {limits.download_lines}-line limit]"
        case "bytes":
            return f"[output stopped at the {limits.download_bytes // (1024 * 1024)} MiB limit]"
        case _:
            return f"[output stopped at the {limits.download_seconds:g}-second limit]"


class LogDownload:
    """A capped, streamed text rendering of a log selection.

    Holds no database session and one view slot until `aclose`, which every path must call.
    Iterating yields UTF-8 chunks; a capped download ends with one line naming the limit, and one
    the host stopped answering ends with a line saying so.
    """

    def __init__(
        self,
        source: LogSource,
        *,
        query: LogQuery,
        filename: str,
        slot: StreamSlot,
        limits: LogLimits,
        records: LogRecords,
    ) -> None:
        self._source = source
        self._query = query
        self._slot = slot
        self._limits = limits
        self._records = records
        self._frames = FrameReader(
            source.frames(
                FrameRequest(
                    follow=False,
                    stdout=query.stdout,
                    stderr=query.stderr,
                    since_ns=query.since_ns,
                    until_ns=query.until_ns,
                    tail=query.tail,
                )
            )
        )
        self._deadline: float | None = None
        self._head: LogFrame | ReadTimeout | _Unread | None = _UNREAD
        self._chunks = self._generate()
        self.filename = filename
        self.media_type = DOWNLOAD_MEDIA_TYPES[query.format]
        self.end_reason: Literal["finished", "capped", "unavailable", "closed"] | None = None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        return await self._chunks.__anext__()

    async def aclose(self) -> None:
        """End the download: close the Engine response and give the slot back; safe to repeat."""
        try:
            with anyio.CancelScope(shield=True):
                await self._chunks.aclose()
                await self._frames.aclose()
        finally:
            self._slot.close()
            if self.end_reason is None:
                self.end_reason = "closed"

    async def prime(self) -> None:
        """Read the first frame ahead of the response, so a failing host is reported as one.

        LogsUnavailable when the Engine fails before the first frame; the download is closed
        first, on that path and when the caller is cancelled.
        """
        loop = asyncio.get_running_loop()
        self._deadline = loop.time() + self._limits.download_seconds
        try:
            self._head = await self._frames.next(self._limits.download_seconds)
        except EngineError:
            await self.aclose()
            raise LogsUnavailable(self._source.subject) from None
        except BaseException:
            await self.aclose()
            raise

    async def _generate(self) -> AsyncGenerator[bytes, None]:
        loop = asyncio.get_running_loop()
        if self._deadline is None:
            self._deadline = loop.time() + self._limits.download_seconds
        deadline = self._deadline
        frames = self._frames
        splitter = LogLineSplitter()
        written_lines = 0
        written_bytes = 0
        try:
            while True:
                try:
                    if isinstance(self._head, _Unread):
                        frame = await frames.next(deadline - loop.time())
                    else:
                        frame, self._head = self._head, _UNREAD
                except EngineError:
                    self.end_reason = "unavailable"
                    yield render_notice(self._query, UNAVAILABLE_LINE)
                    return
                if isinstance(frame, ReadTimeout):
                    yield render_notice(self._query, cap_line("seconds", self._limits))
                    self.end_reason = "capped"
                    return
                lines = splitter.flush() if frame is None else await splitter.split([frame])
                lines = self._source.lines(lines)
                out: list[bytes] = []
                capped: Literal["lines", "bytes"] | None = None
                rendered = await render_batch(
                    self._query,
                    lines,
                    lambda line: render_download_line(self._query, line, self._records),
                )
                for encoded in rendered:
                    if encoded is None:
                        continue
                    if written_lines >= self._limits.download_lines:
                        capped = "lines"
                        break
                    if written_bytes + len(encoded) > self._limits.download_bytes:
                        capped = "bytes"
                        break
                    out.append(encoded)
                    written_lines += 1
                    written_bytes += len(encoded)
                if out:
                    yield b"".join(out)
                if capped is not None:
                    yield render_notice(self._query, cap_line(capped, self._limits))
                    self.end_reason = "capped"
                    return
                if frame is None:
                    self.end_reason = "finished"
                    return
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    await frames.aclose()
            finally:
                self._slot.close()


def _filtered_out(query: LogQuery, line: LogLine) -> bool:
    return bool(query.filter) and query.filter.casefold() not in line.text.casefold()


def render_download_line(query: LogQuery, line: LogLine, records: LogRecords) -> bytes | None:
    """Render a line in the query's format, None when the query's text filter drops it."""
    if query.format == "text":
        text = render_line(query, line)
        return None if text is None else text.encode("utf-8")
    if _filtered_out(query, line):
        return None
    return records.encode(line)


async def render_batch[T: LogLine](
    query: LogQuery, lines: Sequence[T], render: Callable[[T], bytes | None]
) -> list[bytes | None]:
    """Render a batch of lines, in a worker thread when an NDJSON batch is large.

    The threshold is `INLINE_DECODE_BYTES` of line text; a text download is always inline.
    """
    if query.format == "ndjson" and sum(len(line.text) for line in lines) > INLINE_DECODE_BYTES:
        return await asyncio.to_thread(lambda: [render(line) for line in lines])
    return [render(line) for line in lines]


def render_notice(query: LogQuery, text: str) -> bytes:
    """Render the line a download appends when it stops early, in the query's format."""
    if query.format == "ndjson":
        return encode_notice(text)
    return text.encode("utf-8") + b"\n"


def render_line(query: LogQuery, line: LogLine) -> str | None:
    """Render a line for a text download, None when the query's text filter drops it."""
    if _filtered_out(query, line):
        return None
    prefix = ""
    if query.stdout and query.stderr:
        prefix = "stderr " if line.stream == "stderr" else "stdout "
    stamp = f"{line.timestamp} " if query.timestamps and line.timestamp else ""
    origin = f"{line.origin} " if isinstance(line, OriginLine) else ""
    return f"{prefix}{stamp}{origin}{line.text}\n"


def download_filename(
    name: str, container_id: str, moment: datetime, log_format: LogFormat = "text"
) -> str:
    """Return `<name>-<short id>-<UTC timestamp>` with `.log` or `.ndjson` after it.

    The name is reduced to safe characters.
    """
    safe = _FILENAME_UNSAFE.sub("_", name)[:_FILENAME_NAME_CHARS] or "container"
    stamp = moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{safe}-{short_id(container_id)}-{stamp}{DOWNLOAD_SUFFIXES[log_format]}"


def _scrubbed(lines: Iterable[LogLine]) -> tuple[LogLine, ...]:
    return tuple(replace(line, text=DEFAULT_SCRUBBER.scrub_text(line.text)) for line in lines)


@dataclass(frozen=True, slots=True)
class _Source:
    client: EngineClient
    name: str
    tty: bool
    live: bool
    image: str
    labels: Mapping[str, str]


class ContainerLogService:
    """Authorises at read access once, then hands out log output; streams touch no session."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        slots: StreamSlots,
        limits: LogLimits | None = None,
        instance_name: str = DEFAULT_INSTANCE_NAME,
    ) -> None:
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._slots = slots
        self._limits = limits or LogLimits()
        self._instance_name = instance_name

    async def open(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        query: LogQuery,
        last_event_id: str | None = None,
        after: str | None = None,
    ) -> LogStream:
        """Authorise at read access and claim a view slot; the caller must close the stream.

        ResourceNotFound when the caller may not read the container or it does not exist, with
        no Engine log request made. TooManyStreams when the user or the process has no free view
        slot; the slot is claimed last, so a refused caller never takes one. A reconnecting
        browser's Last-Event-ID, or `after`, resumes after the nanosecond timestamp it names and
        drops `tail`; given both, the later one applies. A container that is not running gets its
        history with follow off.
        """
        source = await self._authorise(actor, environment_id, container_id)
        slot = self._slots.acquire(actor.user_id, is_administrator=actor.is_administrator)
        return LogStream(
            ContainerLogSource(source.client, container_id, tty=source.tty),
            query=query,
            follow=query.follow and source.live,
            resume_after=resume_point(last_event_id, after),
            slot=slot,
            limits=self._limits,
        )

    async def download(
        self, actor: Principal, environment_id: int, container_id: str, query: LogQuery
    ) -> LogDownload:
        """Authorise at read access and claim a view slot for a capped download.

        The caller must close the download. The query's follow is ignored: a download ends.
        LogsUnavailable when the Engine fails before the first line, with the slot given back.
        """
        source = await self._authorise(actor, environment_id, container_id)
        context = await read_log_context(self._hosts, environment_id, self._instance_name)
        slot = self._slots.acquire(actor.user_id, is_administrator=actor.is_administrator)
        download = LogDownload(
            ContainerLogSource(source.client, container_id, tty=source.tty),
            query=query,
            filename=download_filename(source.name, container_id, datetime.now(UTC), query.format),
            slot=slot,
            limits=self._limits,
            records=LogRecords(
                context,
                container_source(
                    container_id=container_id,
                    container_name=source.name,
                    image=source.image,
                    labels=source.labels,
                ),
            ),
        )
        await download.prime()
        return download

    async def tail(
        self, actor: Principal, environment_id: int, container_id: str, query: LogQuery
    ) -> LogTail:
        """Read the newest lines for a page without scripting, never more than 1 MiB of output.

        Holds a view slot while it reads, so TooManyStreams when none is free. Over the byte cap
        the newest complete lines that fit are kept and `cut` is set. LogsUnavailable when the
        Engine fails.
        """
        source = await self._authorise(actor, environment_id, container_id)
        slot = self._slots.acquire(actor.user_id, is_administrator=actor.is_administrator)
        try:
            kept, trimmed, timed_out = await self._read_page(source, container_id, query)
        finally:
            slot.close()
        splitter = LogLineSplitter()
        lines = [*await splitter.split(list(kept)), *splitter.flush()]
        if trimmed and lines:
            del lines[0]
        return LogTail(
            tuple(lines),
            trimmed or timed_out,
            source.name,
            short_id(container_id),
            source.live,
        )

    async def last_output(
        self, actor: Principal, environment_id: int, container_id: str
    ) -> LogTail:
        """Read the newest lines of a container that has stopped, scrubbed of secrets.

        Reads and failures are those of `tail`, with a fixed line count and no follow.
        """
        query = LogQuery(tail=LAST_OUTPUT_LINES, follow=False)
        tail = await self.tail(actor, environment_id, container_id, query)
        lines = await asyncio.to_thread(_scrubbed, tail.lines)
        return replace(tail, lines=lines)

    async def _read_page(
        self, source: _Source, container_id: str, query: LogQuery
    ) -> tuple[deque[LogFrame], bool, bool]:
        frames = FrameReader(
            ContainerLogSource(source.client, container_id, tty=source.tty).frames(
                FrameRequest(
                    follow=False,
                    stdout=query.stdout,
                    stderr=query.stderr,
                    since_ns=query.since_ns,
                    until_ns=query.until_ns,
                    tail=query.tail,
                    max_frame_bytes=PAGE_FRAME_BYTES,
                )
            )
        )
        kept: deque[LogFrame] = deque()
        retained = 0
        trimmed = False
        timed_out = False
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PAGE_SECONDS
            while True:
                frame = await frames.next(deadline - loop.time())
                if frame is None:
                    break
                if isinstance(frame, ReadTimeout):
                    timed_out = True
                    break
                kept.append(frame)
                retained += len(frame.data)
                while retained > self._limits.page_bytes and len(kept) > 1:
                    retained -= len(kept.popleft().data)
                    trimmed = True
        except EngineError:
            raise LogsUnavailable from None
        finally:
            with anyio.CancelScope(shield=True):
                await frames.aclose()
        return kept, trimmed, timed_out

    async def _authorise(self, actor: Principal, environment_id: int, container_id: str) -> _Source:
        if not is_canonical_id("container", container_id):
            raise ResourceNotFound("not found")
        key = ResourceKey(environment_id, "container", container_id)
        await self._authoriser.require_access(actor, key, "read")
        inspected = await self._inspector.inspect(environment_id, container_id)
        client = await self._hosts.client(environment_id)
        return _Source(
            client,
            display_name([inspected.name], inspected.id),
            inspected.config.tty,
            inspected.state.status in LIVE_STATES,
            inspected.config.image,
            inspected.config.labels,
        )
