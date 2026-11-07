"""Splits Swarm's per-line attribute token off service log lines and merges lines across tasks."""

import heapq
import re
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Final
from urllib.parse import unquote_plus

from app.services.log_lines import LogLine

__all__ = [
    "MAX_DETAILS_BYTES",
    "LogDeduper",
    "ServiceLogLine",
    "TailMerger",
    "merge_tail",
    "resume_since",
    "split_details",
]

MAX_DETAILS_BYTES: Final = 4 * 1024
DEDUPE_WINDOW: Final = 4096

_NODE_KEY: Final = "com.docker.swarm.node.id"
_TASK_KEY: Final = "com.docker.swarm.task.id"
_ID: Final = re.compile(r"[0-9a-z]{25}")
# Go's url.QueryEscape leaves only these unescaped.
_TOKEN: Final = re.compile(r"[A-Za-z0-9_.~+%-]*=[A-Za-z0-9_.~+%=,-]*")
_NANOS_PER_SECOND: Final = 1_000_000_000


@dataclass(frozen=True, slots=True)
class ServiceLogLine:
    """A log line with its text stripped of the attribute token, and the ids the token named."""

    line: LogLine
    task_id: str | None
    node_id: str | None


def split_details(line: LogLine) -> ServiceLogLine:
    """Take the task and node ids from a line's attribute token and drop the token from its text.

    Only Swarm's own two keys are read; every other attribute is user-controlled and ignored. A
    line whose first word is not an attribute token naming one of them is returned unchanged. A
    token over MAX_DETAILS_BYTES is dropped from the text and names no task or node.
    """
    if line.timestamp is None:
        return ServiceLogLine(line, None, None)
    token, _, message = line.text.partition(" ")
    if _TOKEN.fullmatch(token) is None:
        return ServiceLogLine(line, None, None)
    if len(token) > MAX_DETAILS_BYTES:
        return ServiceLogLine(replace(line, text=message), None, None)
    if not _names(token) & {_TASK_KEY, _NODE_KEY}:
        return ServiceLogLine(line, None, None)
    return ServiceLogLine(
        replace(line, text=message), _single(token, _TASK_KEY), _single(token, _NODE_KEY)
    )


def _names(token: str) -> set[str]:
    return {unquote_plus(pair.partition("=")[0]) for pair in token.split(",")}


def _single(token: str, key: str) -> str | None:
    found: set[str] = set()
    for pair in token.split(","):
        name, separator, value = pair.partition("=")
        if separator and unquote_plus(name) == key:
            found.add(unquote_plus(value))
    if len(found) != 1:
        return None
    (value,) = found
    return value if _ID.fullmatch(value) else None


class TailMerger:
    """Keeps the newest `count` lines across tasks within a text byte budget as lines arrive.

    The Engine does not order lines across tasks, so lines are held on (timestamp, arrival
    position) and the oldest leaves when the count or the budget is exceeded. A line without a
    timestamp sorts as the line before it. `cut` is set once any line has left.
    """

    def __init__(self, count: int, *, max_bytes: int) -> None:
        self._count = count
        self._max_bytes = max_bytes
        self._heap: list[tuple[int, int, int, ServiceLogLine]] = []
        self._bytes = 0
        self._position = 0
        self._previous = 0
        self.cut = False

    def add(self, item: ServiceLogLine) -> None:
        """Take one line in arrival order."""
        stamp = item.line.timestamp_ns
        self._previous = self._previous if stamp is None else stamp
        position, self._position = self._position, self._position + 1
        if self._count <= 0:
            self.cut = True
            return
        size = len(item.line.text.encode("utf-8"))
        heapq.heappush(self._heap, (self._previous, position, size, item))
        self._bytes += size
        while len(self._heap) > self._count or self._bytes > self._max_bytes:
            _, _, dropped, _ = heapq.heappop(self._heap)
            self._bytes -= dropped
            self.cut = True

    def result(self) -> list[ServiceLogLine]:
        """Return the kept lines, oldest first."""
        return [entry[3] for entry in sorted(self._heap, key=lambda entry: entry[:2])]


def merge_tail(
    lines: Iterable[ServiceLogLine], count: int, *, max_bytes: int
) -> tuple[list[ServiceLogLine], bool]:
    """Return the newest `count` lines across tasks, oldest first, and whether any were cut."""
    merger = TailMerger(count, max_bytes=max_bytes)
    for item in lines:
        merger.add(item)
    return merger.result(), merger.cut


def resume_since(last_timestamp_ns: int) -> str:
    """Return the `seconds.nanoseconds` `since` value that excludes the line stamped last.

    The Engine's `since` is inclusive and reads nothing but that form.
    """
    seconds, nanos = divmod(max(last_timestamp_ns, 0) + 1, _NANOS_PER_SECOND)
    return f"{seconds}.{nanos:09d}"


class LogDeduper:
    """Drops a line already delivered, so a resumed stream does not repeat it.

    Keyed by task, timestamp and stream, with the text so two different lines stamped alike both
    pass; remembers the last DEDUPE_WINDOW keys. A line without a timestamp always passes. A
    line stamped at or before `floor`, the resume point of a reconnect, was delivered by the
    stream before it and is dropped.
    """

    def __init__(self, window: int = DEDUPE_WINDOW, *, floor: int | None = None) -> None:
        self._window = window
        self._floor = floor
        self._seen: OrderedDict[tuple[str | None, int, str, int], None] = OrderedDict()

    def accept(self, item: ServiceLogLine) -> bool:
        """Report whether the line is new, and remember it."""
        stamp = item.line.timestamp_ns
        if stamp is None:
            return True
        if self._floor is not None and stamp <= self._floor:
            return False
        key = (item.task_id, stamp, item.line.stream, hash(item.line.text))
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = None
        if len(self._seen) > self._window:
            self._seen.popitem(last=False)
        return True

    def filter(self, items: Sequence[ServiceLogLine]) -> list[ServiceLogLine]:
        """Return the lines of a batch that are new, in order."""
        return [item for item in items if self.accept(item)]
