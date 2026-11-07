"""Drives one streamed GET against an application directly, as a browser holding it open."""

import asyncio
import threading
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

WAIT_SECONDS = 5.0
SETTLE_TURNS = 20
CLIENT_ADDRESS = ("198.51.100.7", 40000)


@dataclass(slots=True)
class Capture:
    """What an application wrote to a client that left after `events` events."""

    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""


async def read_stream(
    app: FastAPI,
    client: AsyncClient,
    path: str,
    *,
    stop_after_events: int | None = None,
    stop_when: str | None = None,
    query: str = "",
    headers: dict[str, str] | None = None,
) -> Capture:
    """Run one GET against the application directly, as a browser holding the stream open.

    The response is read as it is written. The client disconnects once `stop_after_events`
    events have arrived or the body holds `stop_when`, and waits for the application to end the
    stream on its own if both are None.
    """
    cookie = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "scheme": "https",
        "server": ("testserver", 443),
        "client": CLIENT_ADDRESS,
        "headers": [
            (b"host", b"testserver"),
            (b"cookie", cookie.encode()),
            *((name.lower().encode(), value.encode()) for name, value in (headers or {}).items()),
        ],
    }
    captured = Capture()
    enough = asyncio.Event()
    left = asyncio.Event()
    asked = False

    async def receive() -> dict[str, Any]:
        nonlocal asked
        if not asked:
            asked = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await left.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            captured.status = message["status"]
            captured.headers = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            captured.body += message.get("body", b"").decode()
            events_seen = stop_after_events is not None and (
                captured.body.count("\n\n") >= stop_after_events
            )
            if events_seen or (stop_when is not None and stop_when in captured.body):
                enough.set()

    running = asyncio.create_task(app(scope, receive, send))
    async with asyncio.timeout(WAIT_SECONDS):
        if stop_after_events is None and stop_when is None:
            await running
            return captured
        waiter = asyncio.create_task(enough.wait())
        await asyncio.wait({running, waiter}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        left.set()
        await running
    return captured


async def leftover_tasks(before: set[asyncio.Task[Any]]) -> set[asyncio.Task[Any]]:
    """Return the tasks started since `before` that outlive a few turns of the event loop.

    Closing an abandoned HTTP body schedules its generators' finalisers as tasks, which end on
    the next turns; anything still alive after that is a leak.
    """
    for _ in range(SETTLE_TURNS):
        remaining = asyncio.all_tasks() - before
        if not remaining:
            return remaining
        await asyncio.sleep(0)
    return asyncio.all_tasks() - before


def encoding_threads(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> set[int]:
    """Record the id of each thread that encodes an NDJSON record through `module`."""
    real = module.encode_log_line
    threads: set[int] = set()

    def spy(*args: Any) -> bytes:
        threads.add(threading.get_ident())
        encoded: bytes = real(*args)
        return encoded

    monkeypatch.setattr(module, "encode_log_line", spy)
    return threads
