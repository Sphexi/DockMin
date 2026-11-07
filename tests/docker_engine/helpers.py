"""Constants and small builders shared by the Docker access layer tests."""

import json
import struct
import traceback
from collections.abc import AsyncIterator
from typing import Any

from app.docker_engine.streams import BackoffPolicy

ENV_ID = 7
OTHER_ENV_ID = 8
CONTAINER_ID = "abc123"
CONNECT_TIMEOUT_SECONDS = 5.0
HANG_GUARD_SECONDS = 5.0

STDOUT = 1
STDERR = 2
FRAME_HEADER_BYTES = 8

EVENT_BASE_SECONDS = 1_700_000_000

# Valid for BackoffPolicy, yet short enough that a real reconnect sleep would be imperceptible.
NEAR_ZERO_BACKOFF = BackoffPolicy(initial=0.001, maximum=0.001, jitter=0.0)

CONTAINER_SUMMARY = {
    "Id": "c0ffee",
    "Names": ["/web"],
    "Image": "nginx:1.27",
    "ImageID": "sha256:aaa",
    "Created": 1_700_000_000,
    "State": "running",
    "Status": "Up 2 hours",
    "Labels": {"com.docker.compose.project": "site"},
    "Ports": [{"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"}],
}


def log_frame(kind: int, payload: bytes) -> bytes:
    """Encode one multiplexed log frame."""
    return struct.pack(">BxxxI", kind, len(payload)) + payload


def json_line(payload: dict[str, Any]) -> bytes:
    """Encode one newline-terminated JSON object."""
    return json.dumps(payload).encode("utf-8") + b"\n"


def event_payload(number: int) -> dict[str, Any]:
    """Build an Engine event whose actor id is `e<number>` and whose time increases with it."""
    seconds = EVENT_BASE_SECONDS + number
    return {
        "Type": "container",
        "Action": "start",
        "Actor": {"ID": f"e{number}", "Attributes": {}},
        "scope": "local",
        "time": seconds,
        "timeNano": seconds * 1_000_000_000,
    }


def stats_payload(usage: int) -> dict[str, Any]:
    """Build a stats frame whose memory usage identifies it."""
    return {"read": "2026-01-01T00:00:00Z", "memory_stats": {"usage": usage, "limit": 1_000_000}}


async def drain[T](stream: AsyncIterator[T]) -> list[T]:
    """Collect every item of an async iterator."""
    return [item async for item in stream]


async def drain_until_error[T](stream: AsyncIterator[T]) -> tuple[list[T], Exception | None]:
    """Collect items until the iterator ends or raises; return what arrived and the error."""
    items: list[T] = []
    try:
        async for item in stream:
            items.append(item)
    except Exception as exc:
        return items, exc
    return items, None


async def take[T](stream: AsyncIterator[T], count: int) -> list[T]:
    """Pull exactly `count` items without exhausting the iterator."""
    return [await anext(stream) for _ in range(count)]


def feed_each_byte[T](decoder: Any, data: bytes) -> list[list[T]]:
    """Feed a decoder one byte at a time and return what each byte completed."""
    return [decoder.feed(data[i : i + 1]) for i in range(len(data))]


def failure_texts(error: BaseException) -> list[str]:
    """Return every text a log line or error report could derive from an exception."""
    visible: list[BaseException] = []
    current: BaseException | None = error
    while current is not None:
        visible.append(current)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return [
        "".join(traceback.format_exception(error)),
        *(f"{e}|{e!r}|{e.args!r}" for e in visible),
    ]
