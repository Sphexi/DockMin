"""Starts one Compose process with bounded capture and kills its whole process group on failure."""

import asyncio
import json
import os
import signal
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from app.compose_cli.binary import ComposeBinary
from app.compose_cli.errors import ComposeOutputTooLarge, ComposeTimedOut, ComposeUnavailable
from app.compose_cli.parsers import MAX_EVENT_LINE_BYTES, ProgressEvent, parse_event
from app.logging import get_logger

logger = get_logger(__name__)

MAX_LINE_BYTES: Final = 64 * 1024
SCRUB_MARGIN_BYTES: Final = 64 * 1024
MAX_EVENTS: Final = 500
MAX_STDERR_LINES: Final = 200
MAX_STDERR_BYTES: Final = 64 * 1024
TERM_GRACE_SECONDS: Final = 10.0
REAP_SECONDS: Final = 5.0
EOF_GRACE_SECONDS: Final = 2.0
DRAIN_SECONDS: Final = 1.0

_READ_CHUNK: Final = 64 * 1024
_POLL_SECONDS: Final = 0.05


@dataclass(frozen=True)
class Invocation:
    """One process to run; `command` is the argv tail without paths, kept for error reports.

    With stdout_events, stdout is scanned for progress events like stderr and not captured;
    otherwise stdout is captured whole, up to stdout_cap bytes.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str] = field(repr=False)
    cwd: Path
    timeout: float
    stdout_cap: int
    command: tuple[str, ...] = ()
    stdout_events: bool = False


@dataclass(frozen=True)
class Completed:
    """The outcome of a process that ran to its own exit."""

    exit_code: int
    stdout: bytes = field(repr=False)
    events: tuple[ProgressEvent, ...]
    stderr: str = field(repr=False)


async def run(
    binary: ComposeBinary,
    invocation: Invocation,
    *,
    on_event: Callable[[ProgressEvent], None] | None = None,
    scrub: Callable[[str], str] | None = None,
) -> Completed:
    """Run one Compose process to completion.

    Event text and stderr pass through scrub before reaching on_event or a result. Timeout,
    cancellation and output over the cap kill the process group and everything in it before
    the call returns or raises.
    """
    collector = _Collector(on_event, scrub)
    proc = await spawn(binary, invocation)
    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_reader = (
        pump(proc.stdout, collector.stdout_line)
        if invocation.stdout_events
        else _capture(proc.stdout, invocation.stdout_cap)
    )
    stdout_task = asyncio.ensure_future(stdout_reader)
    stderr_task = asyncio.ensure_future(pump(proc.stderr, collector.stderr_line))
    readers = (stdout_task, stderr_task)

    clean = False
    timed_out = False
    try:
        async with asyncio.timeout(invocation.timeout):
            stuck = await drive(proc, readers)
        clean = not stuck
        if stuck:
            logger.warning(
                "compose_pipe_held_open", extra={"command": " ".join(invocation.command)}
            )
    except TimeoutError:
        timed_out = True
    finally:
        await uninterrupted(release(proc, readers, clean=clean))

    if timed_out:
        raise ComposeTimedOut(
            invocation.command, proc.returncode, collector.stderr_text(), tuple(collector.events)
        )
    assert proc.returncode is not None
    captured = b"" if invocation.stdout_events else stdout_task.result()
    return Completed(proc.returncode, captured, tuple(collector.events), collector.stderr_text())


async def uninterrupted(work: Awaitable[None], *, event: str = "compose_cleanup_failed") -> None:
    """Run cleanup to its end however many times the caller is cancelled meanwhile.

    A cancellation that arrives during the cleanup is re-raised once the cleanup has finished;
    a failure of the cleanup is logged under `event`.
    """
    task = asyncio.ensure_future(work)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:
            break
    if not task.cancelled() and (failure := task.exception()) is not None:
        logger.error(event, extra={"error": type(failure).__name__})
    if interrupted:
        raise asyncio.CancelledError


async def release(
    proc: asyncio.subprocess.Process, readers: Sequence[asyncio.Future[Any]], *, clean: bool
) -> None:
    """Kill the process group unless the run was clean, then collect the readers."""
    if not clean:
        await _kill_group(proc)
        _, pending = await asyncio.wait(readers, timeout=DRAIN_SECONDS)
        for task in pending:
            task.cancel()
    await asyncio.gather(*readers, return_exceptions=True)
    if not clean:
        await _discard_to_eof(proc)


async def _discard_to_eof(proc: asyncio.subprocess.Process) -> None:
    """Read both pipes to EOF; a transport whose pipe is never drained is never closed."""
    streams = [stream for stream in (proc.stdout, proc.stderr) if stream is not None]
    drains = [asyncio.ensure_future(_discard(stream)) for stream in streams]
    _, pending = await asyncio.wait(drains, timeout=DRAIN_SECONDS)
    for task in pending:
        task.cancel()
    await asyncio.gather(*drains, return_exceptions=True)


async def _discard(reader: asyncio.StreamReader) -> None:
    while await reader.read(_READ_CHUNK):
        pass


async def spawn(binary: ComposeBinary, invocation: Invocation) -> asyncio.subprocess.Process:
    """Start the process in its own session; ComposeUnavailable when the binary cannot be run."""
    try:
        return await asyncio.create_subprocess_exec(
            *binary.argv0,
            *invocation.argv,
            env=dict(invocation.env),
            cwd=invocation.cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        if exc.filename is not None and os.fsdecode(exc.filename) == binary.argv0[0]:
            raise ComposeUnavailable from None
        raise


async def drive(proc: asyncio.subprocess.Process, readers: Sequence[asyncio.Future[Any]]) -> bool:
    """Wait for exit and EOF on both pipes; return True when a pipe outlived the process.

    Process.wait() only returns once every pipe has closed, so a descendant holding one open
    would hide the exit; the returncode is polled instead.
    """
    live = set(readers)
    while proc.returncode is None and live:
        done, live = await asyncio.wait(live, timeout=_POLL_SECONDS)
        _raise_failures(done)
    if live:
        done, live = await asyncio.wait(live, timeout=EOF_GRACE_SECONDS)
        _raise_failures(done)
        return bool(live)
    await proc.wait()
    return False


def _raise_failures(done: set[asyncio.Future[Any]]) -> None:
    for future in done:
        if not future.cancelled() and (failure := future.exception()) is not None:
            raise failure


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    pgid = proc.pid
    loop = asyncio.get_running_loop()
    if _signal_group(pgid, signal.SIGTERM):
        deadline = loop.time() + TERM_GRACE_SECONDS
        while loop.time() < deadline:
            if proc.returncode is not None and not _signal_group(pgid, 0):
                return
            await asyncio.sleep(_POLL_SECONDS)
        _signal_group(pgid, signal.SIGKILL)
    try:
        await asyncio.wait_for(proc.wait(), REAP_SECONDS)
    except TimeoutError:
        logger.error("compose_process_not_reaped", extra={"pid": pgid})
        return
    deadline = loop.time() + REAP_SECONDS
    while loop.time() < deadline:
        if not _signal_group(pgid, 0):
            return
        await asyncio.sleep(_POLL_SECONDS)


def _signal_group(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class LineSplitter:
    """Splits a byte stream into lines, keeping at most `limit` bytes of each."""

    def __init__(self, limit: int = MAX_LINE_BYTES) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self._skipping = False

    def feed(self, chunk: bytes) -> list[bytes]:
        """Return the lines the chunk completes."""
        lines: list[bytes] = []
        start = 0
        while (newline := chunk.find(b"\n", start)) != -1:
            self._append(chunk[start:newline])
            lines.append(self._take())
            start = newline + 1
        self._append(chunk[start:])
        return lines

    def finish(self) -> list[bytes]:
        """Return the unterminated last line, if any."""
        return [self._take()] if self._buffer else []

    def _append(self, data: bytes) -> None:
        if self._skipping or not data:
            return
        room = self._limit - len(self._buffer)
        self._buffer += data[:room]
        self._skipping = len(data) > room

    def _take(self) -> bytes:
        line = bytes(self._buffer).rstrip(b"\r")
        self._buffer.clear()
        self._skipping = False
        return line


class _Collector:
    """Sorts output lines into progress events and scrubbed stderr text, both bounded."""

    def __init__(
        self,
        on_event: Callable[[ProgressEvent], None] | None,
        scrub: Callable[[str], str] | None,
    ) -> None:
        self.events: deque[ProgressEvent] = deque(maxlen=MAX_EVENTS)
        self._lines: deque[str] = deque()
        self._line_bytes: deque[int] = deque()
        self._total_bytes = 0
        self._on_event = on_event
        self._scrub = scrub

    def stdout_line(self, line: bytes) -> None:
        if (event := parse_event(line)) is not None:
            self._record(event)

    def stderr_line(self, line: bytes) -> None:
        if (event := parse_event(line)) is not None:
            self._record(event)
            return
        text = _plain_text(line)
        if len(text.encode("utf-8")) > MAX_LINE_BYTES:
            text = _clip(self._scrubbed(text) or "")
        size = len(text.encode("utf-8"))
        self._lines.append(text)
        self._line_bytes.append(size)
        self._total_bytes += size
        while len(self._lines) > MAX_STDERR_LINES or self._total_bytes > MAX_STDERR_BYTES:
            self._lines.popleft()
            self._total_bytes -= self._line_bytes.popleft()

    def stderr_text(self) -> str:
        text = "\n".join(self._lines)
        return self._scrub(text) if self._scrub is not None else text

    def _record(self, event: ProgressEvent) -> None:
        if self._scrub is not None:
            event = replace(
                event,
                resource=self._scrubbed(event.resource),
                parent=self._scrubbed(event.parent),
                status=self._scrubbed(event.status),
                text=self._scrubbed(event.text),
            )
        event = event.bounded()
        self.events.append(event)
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception as exc:
            logger.warning("compose_event_callback_failed", extra={"error": type(exc).__name__})
            self._on_event = None

    def _scrubbed(self, value: str | None) -> str | None:
        return self._scrub(value) if self._scrub is not None and value is not None else value


def _plain_text(line: bytes) -> str:
    """Return a stderr line as text; a JSON error or warning object becomes its message.

    Compose's JSON escaping (`\\u003c` for `<`, `\\"` for a quote) would otherwise keep a secret
    from matching its literal value.
    """
    if len(line) <= MAX_EVENT_LINE_BYTES and line.lstrip().startswith(b"{"):
        try:
            document = json.loads(line)
        except (ValueError, RecursionError):
            document = None
        if isinstance(document, dict):
            for key in ("message", "msg"):
                message = document.get(key)
                if isinstance(message, str):
                    return message
    return line.decode("utf-8", errors="replace")


def _clip(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_LINE_BYTES:
        return text
    return encoded[:MAX_LINE_BYTES].decode("utf-8", errors="ignore")


async def pump(
    reader: asyncio.StreamReader,
    handle: Callable[[bytes], None],
    limit: int = MAX_LINE_BYTES + SCRUB_MARGIN_BYTES,
) -> bytes:
    """Pass each line of the stream to `handle`, keeping at most `limit` bytes of a line."""
    splitter = LineSplitter(limit)
    while chunk := await reader.read(_READ_CHUNK):
        for line in splitter.feed(chunk):
            handle(line)
    for line in splitter.finish():
        handle(line)
    return b""


async def _capture(reader: asyncio.StreamReader, cap: int) -> bytes:
    captured = bytearray()
    while chunk := await reader.read(_READ_CHUNK):
        captured += chunk
        if len(captured) > cap:
            raise ComposeOutputTooLarge
    return bytes(captured)
