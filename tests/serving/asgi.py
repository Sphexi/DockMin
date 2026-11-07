"""A minimal driver that runs one request through an ASGI application."""

from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Message

HTTP_PORT = 8000


@dataclass(frozen=True, slots=True)
class Answer:
    """What an application sent for one request."""

    status: int
    headers: dict[str, str]
    body: bytes


def http_scope(
    *,
    method: str = "GET",
    path: str = "/",
    query: bytes = b"",
    host: str | None = "dockmin.example.test",
    raw_path: bytes | None = None,
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
    **overrides: Any,
) -> dict[str, Any]:
    headers = list(extra_headers)
    if host is not None:
        headers.append((b"host", host.encode("ascii")))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": raw_path if raw_path is not None else path.encode("utf-8"),
        "query_string": query,
        "headers": headers,
        "scheme": "http",
        "server": ("testserver", HTTP_PORT),
        "client": ("203.0.113.9", 50000),
        **overrides,
    }


async def call(app: ASGIApp, scope: dict[str, Any], body: bytes = b"") -> Answer:
    sent: list[Message] = []
    pending = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> Message:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {
        name.decode("latin-1").lower(): value.decode("latin-1") for name, value in start["headers"]
    }
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return Answer(start["status"], headers, payload)
