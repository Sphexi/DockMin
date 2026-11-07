"""Incremental decoders for the Engine's multiplexed log frames and newline-delimited JSON."""

import json
import struct
from dataclasses import dataclass
from typing import Any, Literal, Protocol

_FRAME_HEADER = struct.Struct(">BxxxI")
_STREAM_NAMES: dict[int, Literal["stdin", "stdout", "stderr"]] = {
    0: "stdin",
    1: "stdout",
    2: "stderr",
}
# A single daemon frame is a few KiB; a larger declared size means the stream is corrupt.
MAX_FRAME_BYTES = 8 * 1024 * 1024


class FrameDecodeError(Exception):
    """The byte stream is not valid frame data."""


class TruncatedStream(FrameDecodeError):
    """The stream ended part-way through a frame."""


@dataclass(frozen=True, slots=True)
class LogFrame:
    """One chunk of container output and the stream it came from."""

    stream: Literal["stdin", "stdout", "stderr"]
    data: bytes


class Decoder[T](Protocol):
    """Turns arbitrary byte chunks into complete objects."""

    def feed(self, chunk: bytes) -> list[T]:
        """Consume a chunk and return the objects it completed."""
        ...

    def finish(self) -> None:
        """Assert the stream ended on an object boundary."""
        ...


class LogFrameDecoder:
    """Decode 8-byte-header multiplexed frames; a partial frame is never yielded."""

    def __init__(self, max_frame_bytes: int | None = None) -> None:
        self._buffer = bytearray()
        self._limit = MAX_FRAME_BYTES if max_frame_bytes is None else max_frame_bytes

    def feed(self, chunk: bytes) -> list[LogFrame]:
        """Consume a chunk and return the frames it completed.

        A header declaring more than `max_frame_bytes` raises, so the buffer never holds more than
        that plus one chunk.
        """
        self._buffer += chunk
        frames: list[LogFrame] = []
        offset = 0
        while len(self._buffer) - offset >= _FRAME_HEADER.size:
            kind, size = _FRAME_HEADER.unpack_from(self._buffer, offset)
            stream = _STREAM_NAMES.get(kind)
            if stream is None:
                raise FrameDecodeError("log stream is not multiplexed frame data")
            if size > self._limit:
                raise FrameDecodeError("log frame exceeds the size limit")
            end = offset + _FRAME_HEADER.size + size
            if len(self._buffer) < end:
                break
            if size:
                frames.append(
                    LogFrame(stream, bytes(self._buffer[offset + _FRAME_HEADER.size : end]))
                )
            offset = end
        del self._buffer[:offset]
        return frames

    def finish(self) -> None:
        """Raise TruncatedStream if bytes of an unfinished frame remain."""
        if self._buffer:
            raise TruncatedStream("log stream ended mid-frame")


class LineDecoder:
    """Split a byte stream into non-blank lines without interpreting them."""

    def __init__(self, max_line_bytes: int | None = None) -> None:
        self._buffer = bytearray()
        self._limit = MAX_FRAME_BYTES if max_line_bytes is None else max_line_bytes

    def feed(self, chunk: bytes) -> list[bytes]:
        """Consume a chunk and return the lines it completed."""
        self._buffer += chunk
        lines: list[bytes] = []
        start = 0
        while (newline := self._buffer.find(b"\n", start)) != -1:
            if newline - start > self._limit:
                raise FrameDecodeError("stream line exceeds the size limit")
            line = bytes(self._buffer[start:newline]).strip()
            start = newline + 1
            if line:
                lines.append(line)
        del self._buffer[:start]
        if len(self._buffer) > self._limit:
            raise FrameDecodeError("stream line exceeds the size limit")
        return lines

    def finish(self) -> None:
        """Raise TruncatedStream if an unterminated line remains."""
        if self._buffer.strip():
            raise TruncatedStream("stream ended mid-object")


class JsonLinesDecoder:
    """Decode one JSON object per newline-terminated line."""

    def __init__(self, max_line_bytes: int | None = None) -> None:
        self._lines = LineDecoder(max_line_bytes)

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        """Consume a chunk and return the objects it completed."""
        objects: list[dict[str, Any]] = []
        for line in self._lines.feed(chunk):
            try:
                value = json.loads(line)
            except (ValueError, RecursionError):
                raise FrameDecodeError("stream line is not valid JSON") from None
            if not isinstance(value, dict):
                raise FrameDecodeError("stream line is not a JSON object")
            objects.append(value)
        return objects

    def finish(self) -> None:
        """Raise TruncatedStream if an unterminated line remains."""
        self._lines.finish()
