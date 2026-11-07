"""Request limits: body size and speed, requests in flight, and the idle bound on sends."""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging import LOG_INTERVAL_SECONDS
from app.web.limits import (
    BODY_MESSAGE_SECONDS,
    BODY_TOTAL_SECONDS,
    MAX_BODY_BYTES,
    BodySizeLimitMiddleware,
    ConcurrencyLimitMiddleware,
    SendIdleTimeoutMiddleware,
)
from app.web.restore_upload import UPLOAD_IDLE_SECONDS, read_archive_upload
from tests.auth_support import csrf_headers, csrf_token_of
from tests.serving.asgi import call, http_scope

PAYLOAD_TOO_LARGE = 413
REQUEST_TIMEOUT = 408
UNAUTHORIZED = 401
UNAVAILABLE = 503
FORM = {"Content-Type": "application/x-www-form-urlencoded"}
CHUNK_BYTES = 64 * 1024
CHUNKS_PAST_THE_CAP = MAX_BODY_BYTES // CHUNK_BYTES + 2
SHORT = 0.05
SMALL_CAP = 100


async def read_body(request: Request) -> Response:
    body = await request.body()
    return PlainTextResponse(f"read {len(body)}")


def reading_app() -> ASGIApp:
    return Starlette(routes=[Route("/", read_body, methods=["POST"])])


async def drive(
    app: ASGIApp,
    messages: AsyncIterator[Message],
    *,
    headers: tuple[tuple[bytes, bytes], ...] = (),
    path: str = "/",
) -> tuple[list[Message], int]:
    sent: list[Message] = []
    pulled = 0

    async def receive() -> Message:
        nonlocal pulled
        pulled += 1
        return await anext(messages, {"type": "http.disconnect"})

    async def send(message: Message) -> None:
        sent.append(message)

    scope = http_scope(method="POST", path=path, host="testserver", extra_headers=headers)
    await app(scope, receive, send)
    return sent, pulled


def status_of(sent: list[Message]) -> int:
    status: int = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status


def header_of(sent: list[Message], name: bytes) -> bytes | None:
    start = next(m for m in sent if m["type"] == "http.response.start")
    return {k.lower(): v for k, v in start["headers"]}.get(name)


async def chunks(*sizes: int) -> AsyncIterator[Message]:
    for index, size in enumerate(sizes):
        yield {"type": "http.request", "body": b"a" * size, "more_body": index < len(sizes) - 1}


async def stalls() -> AsyncIterator[Message]:
    await asyncio.Event().wait()
    yield {}


async def stalls_after(first: int) -> AsyncIterator[Message]:
    yield {"type": "http.request", "body": b"a" * first, "more_body": True}
    await asyncio.Event().wait()


async def trickle() -> AsyncIterator[Message]:
    while True:
        await asyncio.sleep(0.01)
        yield {"type": "http.request", "body": b"a", "more_body": True}


@pytest.mark.parametrize(
    ("sizes", "status"),
    [
        pytest.param((SMALL_CAP,), 200, id="at-the-cap"),
        pytest.param((SMALL_CAP + 1,), PAYLOAD_TOO_LARGE, id="one-over-in-one-message"),
        pytest.param((60, 60), PAYLOAD_TOO_LARGE, id="over-across-chunks"),
        pytest.param((40, 40, 20), 200, id="at-the-cap-across-chunks"),
    ],
)
async def test_a_body_is_counted_as_it_arrives_whether_or_not_it_declares_a_length(
    sizes: tuple[int, ...], status: int
) -> None:
    app = BodySizeLimitMiddleware(reading_app(), max_bytes=SMALL_CAP)

    sent, _ = await drive(app, chunks(*sizes))

    assert status_of(sent) == status


async def test_a_declared_length_over_the_cap_is_refused_before_anything_is_read() -> None:
    app = BodySizeLimitMiddleware(reading_app(), max_bytes=SMALL_CAP)

    sent, pulled = await drive(
        app, chunks(10), headers=((b"content-length", str(SMALL_CAP + 1).encode()),)
    )

    assert status_of(sent) == PAYLOAD_TOO_LARGE
    assert pulled == 0


async def test_a_read_stops_at_the_first_message_past_the_cap() -> None:
    app = BodySizeLimitMiddleware(reading_app(), max_bytes=SMALL_CAP)

    sent, pulled = await drive(app, chunks(SMALL_CAP + 1, 10, 10, 10))

    assert status_of(sent) == PAYLOAD_TOO_LARGE
    assert pulled == 1


async def test_a_sender_that_goes_quiet_between_messages_is_answered_408_and_closed() -> None:
    app = BodySizeLimitMiddleware(reading_app(), message_seconds=SHORT)

    sent, _ = await drive(app, stalls_after(5))

    assert status_of(sent) == REQUEST_TIMEOUT
    assert header_of(sent, b"connection") == b"close"


async def test_a_sender_that_never_sends_the_body_is_answered_408() -> None:
    app = BodySizeLimitMiddleware(reading_app(), message_seconds=SHORT)

    sent, _ = await drive(app, stalls())

    assert status_of(sent) == REQUEST_TIMEOUT


async def test_a_sender_that_keeps_trickling_is_stopped_by_the_total_time_allowed() -> None:
    app = BodySizeLimitMiddleware(reading_app(), message_seconds=5.0, total_seconds=SHORT)

    sent, _ = await drive(app, trickle())

    assert status_of(sent) == REQUEST_TIMEOUT


def late_reader(outcome: list[Message]) -> ASGIApp:
    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        outcome.append(await receive())
        await send({"type": "http.response.body", "body": b"done"})

    return application


async def test_a_limit_hit_after_the_response_started_ends_the_request_as_a_disconnect() -> None:
    outcome: list[Message] = []
    app = BodySizeLimitMiddleware(late_reader(outcome), max_bytes=SMALL_CAP)

    sent, _ = await drive(app, chunks(SMALL_CAP + 1))

    assert status_of(sent) == 200
    assert outcome == [{"type": "http.disconnect"}]


async def test_a_stall_after_the_response_started_ends_the_request_as_a_disconnect() -> None:
    outcome: list[Message] = []
    app = BodySizeLimitMiddleware(late_reader(outcome), message_seconds=SHORT)

    sent, _ = await drive(app, stalls())

    assert status_of(sent) == 200
    assert outcome == [{"type": "http.disconnect"}]


async def test_the_restore_upload_route_is_not_subject_to_the_cap() -> None:
    app = BodySizeLimitMiddleware(
        Starlette(routes=[Route("/restore/archive", read_body, methods=["POST"])]),
        max_bytes=SMALL_CAP,
    )

    sent, _ = await drive(app, chunks(SMALL_CAP * 5), path="/restore/archive")

    assert status_of(sent) == 200


async def test_a_websocket_scope_is_not_counted_or_limited() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    await BodySizeLimitMiddleware(inner)({"type": "websocket"}, None, None)  # type: ignore[arg-type]

    assert seen == ["websocket"]


def test_the_body_deadlines_leave_a_slow_but_honest_upload_room_and_the_total_is_bounded() -> None:
    assert BODY_MESSAGE_SECONDS < BODY_TOTAL_SECONDS
    assert MAX_BODY_BYTES == 256 * 1024


async def test_an_undeclared_form_body_past_the_cap_is_refused_by_the_application(
    anonymous_client: AsyncClient,
) -> None:
    async def oversized() -> AsyncIterator[bytes]:
        for _ in range(CHUNKS_PAST_THE_CAP):
            yield b"a" * CHUNK_BYTES

    response = await anonymous_client.post(
        "/login", content=oversized(), headers=FORM | csrf_headers(anonymous_client)
    )

    assert "content-length" not in response.request.headers
    assert response.status_code == PAYLOAD_TOO_LARGE
    assert response.headers["content-security-policy"]


async def test_a_small_chunked_form_body_is_read_in_full_by_the_application(
    anonymous_client: AsyncClient,
) -> None:
    async def small() -> AsyncIterator[bytes]:
        yield b"username=nobody-here&"
        yield f"password=wrong&csrf_token={csrf_token_of(anonymous_client)}".encode("ascii")

    response = await anonymous_client.post("/login", content=small(), headers=FORM)

    assert "content-length" not in response.request.headers
    assert response.status_code == UNAUTHORIZED


async def test_a_declared_length_past_the_cap_is_refused_by_the_application_unread(
    anonymous_client: AsyncClient,
) -> None:
    response = await anonymous_client.post(
        "/login",
        content=b"username=x",
        headers=FORM | csrf_headers(anonymous_client) | {"Content-Length": str(MAX_BODY_BYTES + 1)},
    )

    assert response.status_code == PAYLOAD_TOO_LARGE


async def test_json_and_other_content_types_are_capped_like_forms(
    admin_client: AsyncClient,
) -> None:
    json_body = await admin_client.post(
        "/api/v1/environments",
        content=b"[" + b"0," * MAX_BODY_BYTES + b"0]",
        headers={"Content-Type": "application/json"} | csrf_headers(admin_client),
    )
    other = await admin_client.post(
        "/login",
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/octet-stream"} | csrf_headers(admin_client),
    )

    assert (json_body.status_code, other.status_code) == (PAYLOAD_TOO_LARGE,) * 2


def upload_request(receive: Receive) -> Request:
    scope = http_scope(
        method="POST",
        path="/restore/archive",
        extra_headers=((b"content-type", b"multipart/form-data; boundary=xyz"),),
    )
    return Request(scope, receive)


async def never_used(_: AsyncIterator[bytes]) -> None:
    raise AssertionError("the sink must not run")


def test_the_restore_upload_waits_up_to_two_minutes_for_a_chunk() -> None:
    assert UPLOAD_IDLE_SECONDS == 120.0


async def test_a_restore_upload_that_sends_nothing_is_answered_408_with_the_connection_closed() -> (
    None
):
    async def receive() -> Message:
        await asyncio.Event().wait()
        return {}

    with pytest.raises(HTTPException) as raised:
        await read_archive_upload(upload_request(receive), never_used, idle_seconds=SHORT)

    assert raised.value.status_code == REQUEST_TIMEOUT
    assert raised.value.headers == {"Connection": "close"}


async def test_a_restore_upload_that_stalls_after_its_first_bytes_is_answered_408() -> None:
    sent = [False]

    async def receive() -> Message:
        if not sent[0]:
            sent[0] = True
            return {"type": "http.request", "body": b"--xyz\r\n", "more_body": True}
        await asyncio.Event().wait()
        return {}

    with pytest.raises(HTTPException) as raised:
        await read_archive_upload(upload_request(receive), never_used, idle_seconds=SHORT)

    assert raised.value.status_code == REQUEST_TIMEOUT


class Gate:
    """Holds requests inside the guarded application until released."""

    def __init__(self) -> None:
        self.entered = 0
        self.release = asyncio.Event()
        self.arrived = asyncio.Event()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.entered += 1
        self.arrived.set()
        await self.release.wait()
        if scope["type"] == "http":
            await PlainTextResponse("done")(scope, receive, send)


async def hold(app: ASGIApp, path: str) -> asyncio.Task[Any]:
    return asyncio.create_task(call(app, http_scope(path=path)))


async def test_requests_beyond_the_limit_get_retry_after_and_freed_slots_are_reused() -> None:
    gate = Gate()
    app = ConcurrencyLimitMiddleware(gate, limit=2)
    held = [await hold(app, "/a"), await hold(app, "/b")]
    while gate.entered < 2:  # noqa: ASYNC110
        await asyncio.sleep(0)

    refused = await call(app, http_scope(path="/c"))
    gate.release.set()
    done = await asyncio.gather(*held)
    again = await call(app, http_scope(path="/d"))

    assert refused.status == UNAVAILABLE
    assert refused.headers["retry-after"] == "1"
    assert [answer.status for answer in done] == [200, 200]
    assert again.status == 200
    assert app.active == 0


async def test_the_liveness_path_is_neither_counted_nor_refused() -> None:
    gate = Gate()
    app = ConcurrencyLimitMiddleware(gate, limit=1)
    held = await hold(app, "/a")
    while gate.entered < 1:  # noqa: ASYNC110
        await asyncio.sleep(0)

    probe = asyncio.create_task(call(app, http_scope(path="/api/health")))
    while gate.entered < 2:  # noqa: ASYNC110
        await asyncio.sleep(0)
    active_during = app.active
    gate.release.set()
    answers = await asyncio.gather(held, probe)

    assert active_during == 1
    assert [answer.status for answer in answers] == [200, 200]


async def test_a_websocket_handshake_beyond_the_limit_is_closed_with_try_again_later() -> None:
    gate = Gate()
    app = ConcurrencyLimitMiddleware(gate, limit=1)
    held = await hold(app, "/a")
    while gate.entered < 1:  # noqa: ASYNC110
        await asyncio.sleep(0)
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app({"type": "websocket", "path": "/ws", "headers": []}, receive, send)
    gate.release.set()
    await held

    assert sent == [{"type": "websocket.close", "code": 1013}]


async def test_a_slot_is_released_when_the_request_raises() -> None:
    async def failing(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    app = ConcurrencyLimitMiddleware(failing, limit=1)

    with pytest.raises(RuntimeError):
        await call(app, http_scope())

    assert app.active == 0


async def test_refusals_are_logged_once_per_interval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = [1000.0]
    gate = Gate()
    app = ConcurrencyLimitMiddleware(gate, limit=1, clock=lambda: now[0])
    held = await hold(app, "/a")
    while gate.entered < 1:  # noqa: ASYNC110
        await asyncio.sleep(0)
    caplog.set_level(logging.WARNING)

    await call(app, http_scope())
    await call(app, http_scope())
    now[0] += LOG_INTERVAL_SECONDS + 1
    await call(app, http_scope())
    gate.release.set()
    await held

    assert [r.message for r in caplog.records].count("concurrency_limit_reached") == 2


class StallingSend:
    """A send channel that accepts the first body chunk and then stops accepting."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def __call__(self, message: Message) -> None:
        if message["type"] == "http.response.body" and any(
            m["type"] == "http.response.body" for m in self.sent
        ):
            await asyncio.Event().wait()
        self.sent.append(message)


async def test_a_response_the_client_stops_reading_is_cancelled_and_its_cleanup_runs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cleaned = asyncio.Event()

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"first-chunk", "more_body": True})
            await send({"type": "http.response.body", "body": b"second-chunk"})
        finally:
            cleaned.set()

    app = SendIdleTimeoutMiddleware(inner, seconds=SHORT)
    caplog.set_level(logging.WARNING)
    channel = StallingSend()

    async def receive() -> Message:
        await asyncio.Event().wait()
        return {}

    await app(http_scope(), receive, channel)

    assert cleaned.is_set()
    assert [m["type"] for m in channel.sent] == ["http.response.start", "http.response.body"]
    stalled = [r for r in caplog.records if r.message == "response_send_stalled"]
    assert len(stalled) == 1 and stalled[0].bytes_sent == len(b"first-chunk")  # type: ignore[attr-defined]


async def test_a_response_that_is_read_in_time_is_passed_through_unchanged() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("fine")(scope, receive, send)

    answer = await call(SendIdleTimeoutMiddleware(inner, seconds=SHORT), http_scope())

    assert (answer.status, answer.body) == (200, b"fine")


async def test_an_error_in_the_application_still_reaches_the_server() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await call(SendIdleTimeoutMiddleware(inner, seconds=SHORT), http_scope())


async def test_a_websocket_scope_is_not_guarded() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    await SendIdleTimeoutMiddleware(inner)({"type": "websocket"}, None, None)  # type: ignore[arg-type]

    assert seen == ["websocket"]
