"""Turns container log frames into bounded, display-safe lines. No I/O."""

import asyncio
import codecs
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

from app.docker_engine.frames import LogFrame

MAX_LINE_BYTES = 16 * 1024
INLINE_DECODE_BYTES = 16 * 1024
TRUNCATION_MARKER = "…[line truncated]"
REPLACEMENT = "�"

type Stream = Literal["stdout", "stderr"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NANOS_PER_SECOND = 1_000_000_000
_TIMESTAMP = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2}) "
)
# CSI and OSC introduced by ESC or the C1 byte; an unterminated one runs to the end of the line.
_ANSI = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*(?:[@-~]|\Z)|(?:\x1b\]|\x9d)[^\x07\x1b]*(?:\x07|\x1b\\|\Z)"
)
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class LogLine:
    """One display-safe line; `timestamp` and `timestamp_ns` are None when the line carried none."""

    stream: Stream
    text: str
    timestamp: str | None = None
    timestamp_ns: int | None = None
    truncated: bool = False


@dataclass(slots=True)
class _Pending:
    buffer: bytearray = field(default_factory=bytearray)
    overflowed: bool = False


def parse_timestamp_ns(token: str) -> int | None:
    """Return the Unix nanoseconds of an RFC 3339 time with its trailing space, else None."""
    match = _TIMESTAMP.fullmatch(token)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    fraction = (match.group(7) or "").ljust(9, "0")
    zone = match.group(8)
    try:
        offset = (
            timedelta(0)
            if zone == "Z"
            else timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
            * (-1 if zone[0] == "-" else 1)
        )
        moment = datetime(year, month, day, hour, minute, second, tzinfo=timezone(offset))
    except ValueError:
        return None
    return (moment - _EPOCH) // timedelta(seconds=1) * _NANOS_PER_SECOND + int(fraction)


def frames_size(frames: Iterable[LogFrame]) -> int:
    """Return the raw byte size of a batch, for choosing between inline and worker decoding."""
    return sum(len(frame.data) for frame in frames)


def sanitise(text: str) -> str:
    """Remove ANSI CSI and OSC sequences and replace other control characters except tab."""
    return _CONTROL.sub(REPLACEMENT, _ANSI.sub("", text))


class LogLineSplitter:
    """Splits frames into lines with one partial-line buffer per stream.

    Lines are cut at `MAX_LINE_BYTES` and marked; the rest of that line is dropped, so a stream
    without newlines holds at most one capped buffer per stream. `feed_frames` on a large batch
    belongs in a worker thread; one thread at a time may use an instance.
    """

    def __init__(self, *, timestamps: bool = True) -> None:
        self._timestamps = timestamps
        self._pending: dict[Stream, _Pending] = {"stdout": _Pending(), "stderr": _Pending()}

    def feed(self, stream: Stream, data: bytes) -> list[LogLine]:
        """Consume a chunk of one stream and return the lines it completed."""
        pending = self._pending[stream]
        lines: list[LogLine] = []
        position = 0
        while position < len(data):
            newline = data.find(b"\n", position)
            end = len(data) if newline == -1 else newline
            self._append(pending, data[position:end])
            position = end + 1
            if newline != -1:
                lines.append(self._complete(stream, pending))
        return lines

    def feed_frames(self, frames: Iterable[LogFrame]) -> list[LogLine]:
        """Consume a batch of frames in order; stdin frames are ignored."""
        lines: list[LogLine] = []
        for frame in frames:
            if frame.stream != "stdin":
                lines.extend(self.feed(frame.stream, frame.data))
        return lines

    def flush(self) -> list[LogLine]:
        """Return the unterminated last line of each stream that has one."""
        lines: list[LogLine] = []
        for stream, pending in self._pending.items():
            if pending.buffer or pending.overflowed:
                lines.append(self._complete(stream, pending))
        return lines

    @staticmethod
    def _append(pending: _Pending, segment: bytes) -> None:
        if pending.overflowed:
            return
        room = MAX_LINE_BYTES - len(pending.buffer)
        if len(segment) > room:
            pending.buffer += segment[:room]
            pending.overflowed = True
        else:
            pending.buffer += segment

    async def split(self, frames: Sequence[LogFrame]) -> list[LogLine]:
        """Run `feed_frames`, in a worker thread when the batch exceeds `INLINE_DECODE_BYTES`.

        A cancelled call may leave the splitter part-way through the batch; discard it then.
        """
        if frames_size(frames) > INLINE_DECODE_BYTES:
            return await asyncio.to_thread(self.feed_frames, frames)
        return self.feed_frames(frames)

    def _complete(self, stream: Stream, pending: _Pending) -> LogLine:
        raw = bytes(pending.buffer).removesuffix(b"\r")
        truncated = pending.overflowed
        pending.buffer.clear()
        pending.overflowed = False
        # An incremental decoder holds back a multi-byte character cut by the limit.
        text = codecs.getincrementaldecoder("utf-8")("replace").decode(raw, final=not truncated)
        timestamp: str | None = None
        timestamp_ns: int | None = None
        if self._timestamps:
            match = _TIMESTAMP.match(text)
            if match is not None:
                timestamp_ns = parse_timestamp_ns(match.group())
                if timestamp_ns is not None:
                    timestamp = match.group().rstrip(" ")
                    text = text[match.end() :]
        text = sanitise(text)
        return LogLine(
            stream,
            text + TRUNCATION_MARKER if truncated else text,
            timestamp,
            timestamp_ns,
            truncated,
        )
