"""Tests for the WebSocket access record and the request id bound to a WebSocket connection."""

import io
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth.token_format import WEBHOOK_TOKEN_MARK, new_token
from app.logging import (
    ACCESS_LOGGER,
    REDACTED,
    REQUEST_ID_HEADER,
    AccessLogMiddleware,
    RequestIdMiddleware,
    request_id_var,
)
from app.paths import HEALTH_PATH

Logged = Callable[[str], list[dict[str, Any]]]
Behaviour = Callable[[Receive, Send], Awaitable[None]]

CLIENT_ADDRESS = "203.0.113.9"
USER_ID = 7
TERMINAL_PATH = "/environments/1/containers/abc/terminal/ws"
GOING_AWAY = 1001
POLICY = 1008
NORMAL = 1000
ACCESS_ENDED = 4403
INTERNAL_ERROR = 1011
ACCEPTED = 101
REFUSED = 403
UNAUTHORISED = 401
FAILED = 500
BAD_REQUEST = 400
REDIRECTED = 308
QUERY_SENTINEL = "ticket-sentinel-3c71"
FRAME_SENTINEL = "frame-sentinel-9d42"
CUSTOM_ID = "client-chosen.id-1"
GENERATED_ID_LENGTH = 32


def socket_scope(
    path: str = TERMINAL_PATH,
    *,
    query: bytes = b"",
    headers: tuple[tuple[bytes, bytes], ...] = (),
    user_id: int | None = None,
) -> Scope:
    state = {} if user_id is None else {"authenticated_user_id": user_id}
    return {
        "type": "websocket",
        "path": path,
        "query_string": query,
        "headers": list(headers),
        "client": (CLIENT_ADDRESS, 4000),
        "state": state,
    }


class Peer:
    """The server end of a connection: queued messages in, everything the application sent out."""

    def __init__(self, *incoming: Message) -> None:
        self._incoming = [{"type": "websocket.connect"}, *incoming]
        self.sent: list[Message] = []

    async def receive(self) -> Message:
        return self._incoming.pop(0) if self._incoming else {"type": "websocket.disconnect"}

    async def send(self, message: Message) -> None:
        self.sent.append(message)


async def serve(app: ASGIApp, scope: Scope, *incoming: Message, **options: Any) -> Peer:
    peer = Peer(*incoming)
    await AccessLogMiddleware(app, **options)(scope, peer.receive, peer.send)
    return peer


def disconnect(code: int) -> Message:
    return {"type": "websocket.disconnect", "code": code}


def text_frame(text: str) -> Message:
    return {"type": "websocket.receive", "text": text}


def accepting(*, then_close: int | None = None) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send({"type": "websocket.accept"})
        if then_close is not None:
            await send({"type": "websocket.close", "code": then_close})
            return
        while (await receive())["type"] != "websocket.disconnect":
            pass

    return app


def refusing(code: int | None) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        message: Message = {"type": "websocket.close"}
        if code is not None:
            message["code"] = code
        await send(message)

    return app


def refusing_with_response(status: int) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send({"type": "websocket.http.response.start", "status": status, "headers": []})
        await send({"type": "websocket.http.response.body", "body": b""})

    return app


async def raising_before_accept(scope: Scope, receive: Receive, send: Send) -> None:
    raise RuntimeError("boom")


async def raising_after_accept(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "websocket.accept"})
    raise RuntimeError("boom")


async def returning_without_answer(scope: Scope, receive: Receive, send: Send) -> None:
    await receive()


async def test_an_accepted_connection_writes_one_line_at_close_with_the_clients_close_code(
    logged: Logged,
) -> None:
    await serve(
        accepting(), socket_scope(user_id=USER_ID), text_frame("hello"), disconnect(GOING_AWAY)
    )

    [record] = logged("websocket_connection")
    assert logged("http_request") == []
    assert record["logger"] == ACCESS_LOGGER
    assert {key: record[key] for key in ("path", "status", "close_code", "address", "user_id")} == {
        "path": TERMINAL_PATH,
        "status": ACCEPTED,
        "close_code": GOING_AWAY,
        "address": CLIENT_ADDRESS,
        "user_id": USER_ID,
    }
    assert isinstance(record["duration_ms"], int)
    assert "method" not in record
    assert "listener" not in record


async def test_a_close_sent_by_the_application_is_the_close_code_even_when_the_client_answers(
    logged: Logged,
) -> None:
    await serve(accepting(then_close=ACCESS_ENDED), socket_scope(), disconnect(NORMAL))

    [record] = logged("websocket_connection")
    assert (record["status"], record["close_code"]) == (ACCEPTED, ACCESS_ENDED)


@pytest.mark.parametrize(
    ("app", "status", "close_code"),
    [
        pytest.param(refusing(POLICY), REFUSED, POLICY, id="close-before-accept"),
        pytest.param(refusing(None), REFUSED, NORMAL, id="close-before-accept-without-a-code"),
        pytest.param(
            refusing_with_response(UNAUTHORISED), UNAUTHORISED, None, id="denial-response"
        ),
        pytest.param(returning_without_answer, FAILED, None, id="returned-without-an-answer"),
    ],
)
async def test_a_connection_refused_before_accept_writes_one_line_with_the_refusal_status(
    logged: Logged, app: ASGIApp, status: int, close_code: int | None
) -> None:
    await serve(app, socket_scope())

    [record] = logged("websocket_connection")
    assert (record["status"], record["close_code"]) == (status, close_code)


async def test_an_application_that_raises_before_accept_is_logged_as_500_and_the_error_passes(
    logged: Logged,
) -> None:
    with pytest.raises(RuntimeError):
        await serve(raising_before_accept, socket_scope())

    [record] = logged("websocket_connection")
    assert (record["status"], record["close_code"]) == (FAILED, None)


async def test_an_application_that_raises_after_accept_is_logged_as_accepted_with_1011(
    logged: Logged,
) -> None:
    with pytest.raises(RuntimeError):
        await serve(raising_after_accept, socket_scope())

    [record] = logged("websocket_connection")
    assert (record["status"], record["close_code"]) == (ACCEPTED, INTERNAL_ERROR)


async def test_a_connection_on_a_skipped_path_writes_no_line(logged: Logged) -> None:
    await serve(accepting(), socket_scope(HEALTH_PATH), disconnect(NORMAL))

    assert logged("websocket_connection") == []


async def test_lifespan_scopes_write_no_line(logged: Logged) -> None:
    async def lifespan(scope: Scope, receive: Receive, send: Send) -> None:
        return None

    await AccessLogMiddleware(lifespan)({"type": "lifespan"}, Peer().receive, Peer().send)

    assert logged("websocket_connection") == [] and logged("http_request") == []


async def test_the_line_carries_no_query_string_ticket_or_frame_and_a_path_token_is_redacted(
    logged: Logged, log_stream: io.StringIO
) -> None:
    token, _ = new_token(WEBHOOK_TOKEN_MARK)
    path = f"/hooks/{token}"

    await serve(
        accepting(),
        socket_scope(path, query=f"ticket={QUERY_SENTINEL}".encode()),
        text_frame(FRAME_SENTINEL),
        disconnect(NORMAL),
    )

    [record] = logged("websocket_connection")
    assert record["path"].startswith(f"/hooks/{WEBHOOK_TOKEN_MARK}_")
    assert record["path"].endswith(REDACTED)
    output = log_stream.getvalue()
    assert [s for s in (QUERY_SENTINEL, FRAME_SENTINEL, token) if s in output] == []


async def test_an_overlong_or_restore_progress_path_is_cut_and_redacted_as_on_http(
    logged: Logged,
) -> None:
    await serve(accepting(), socket_scope("/restore/progress/abc123"), disconnect(NORMAL))
    await serve(accepting(), socket_scope("/" + "a" * 2000), disconnect(NORMAL))

    restore, long = logged("websocket_connection")
    assert restore["path"] == f"/restore/progress/{REDACTED}"
    assert long["path"] == "/" + "a" * 1023 + "…"


@pytest.mark.parametrize(
    ("status", "written"),
    [
        pytest.param(BAD_REQUEST, True, id="refused"),
        pytest.param(REDIRECTED, False, id="redirected"),
    ],
)
async def test_with_refusals_only_an_http_request_is_written_only_when_refused(
    logged: Logged, status: int, written: bool
) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope: Scope = {**socket_scope(), "type": "http", "method": "GET"}

    await serve(app, scope, listener="redirect", refusals_only=True)

    assert [r["listener"] for r in logged("http_request")] == (["redirect"] if written else [])


@pytest.mark.parametrize(
    ("app", "written"),
    [
        pytest.param(refusing(POLICY), True, id="refused"),
        pytest.param(accepting(), False, id="accepted"),
    ],
)
async def test_with_refusals_only_a_connection_is_written_only_when_refused(
    logged: Logged, app: ASGIApp, written: bool
) -> None:
    await serve(app, socket_scope(), disconnect(NORMAL), listener="redirect", refusals_only=True)

    assert [r["listener"] for r in logged("websocket_connection")] == (
        ["redirect"] if written else []
    )


async def test_a_listener_name_is_added_to_every_line_when_refusals_are_not_the_only_ones(
    logged: Logged,
) -> None:
    await serve(accepting(), socket_scope(), disconnect(NORMAL), listener="redirect")

    [record] = logged("websocket_connection")
    assert record["listener"] == "redirect"


async def inside_stack(app: ASGIApp, scope: Scope, *incoming: Message) -> Peer:
    peer = Peer(*incoming)
    await RequestIdMiddleware(AccessLogMiddleware(app))(scope, peer.receive, peer.send)
    return peer


def id_capturing(seen: list[str | None]) -> ASGIApp:
    inner = accepting()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(request_id_var.get())
        seen.append(scope["state"].get("request_id"))
        await inner(scope, receive, send)

    return app


async def test_a_websocket_connection_gets_a_minted_request_id_that_its_line_carries(
    logged: Logged,
) -> None:
    seen: list[str | None] = []

    await inside_stack(id_capturing(seen), socket_scope(), disconnect(NORMAL))

    [record] = logged("websocket_connection")
    bound, in_scope = seen
    assert bound == in_scope == record["request_id"]
    assert bound is not None and len(bound) == GENERATED_ID_LENGTH
    assert request_id_var.get() is None


async def test_a_well_formed_x_request_id_on_the_upgrade_is_honoured_as_on_http(
    logged: Logged,
) -> None:
    scope = socket_scope(headers=((REQUEST_ID_HEADER.encode(), CUSTOM_ID.encode()),))

    await inside_stack(accepting(), scope, disconnect(NORMAL))

    [record] = logged("websocket_connection")
    assert record["request_id"] == CUSTOM_ID


@pytest.mark.parametrize("header", ["has space", "x" * 65, "semi;colon"])
async def test_a_malformed_x_request_id_on_the_upgrade_is_replaced_as_on_http(
    logged: Logged, header: str
) -> None:
    scope = socket_scope(headers=((REQUEST_ID_HEADER.encode(), header.encode()),))

    await inside_stack(accepting(), scope, disconnect(NORMAL))

    [record] = logged("websocket_connection")
    assert len(record["request_id"]) == GENERATED_ID_LENGTH


async def test_the_upgrade_response_messages_pass_through_the_request_id_middleware_untouched() -> (
    None
):
    peer = await inside_stack(accepting(), socket_scope(), disconnect(NORMAL))

    assert peer.sent == [{"type": "websocket.accept"}]
