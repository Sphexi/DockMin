"""Tests for the terminal WebSocket's access record, served by uvicorn with the access log on."""

import json
import re
from typing import Any

import pytest
import websockets
from fastapi import FastAPI

from tests.auth_support import ADMIN_ID
from tests.docker_engine.terminal_engine import TerminalEngine
from tests.terminal_support import (
    CLIENT_LOGGER,
    WAIT_SECONDS,
    Live,
    drain,
    issue,
    redeem,
    refused_status,
    socket_path,
    until,
)
from tests.web.environment_support import World

CHOSEN_ID = "terminal-request.id-7"
QUERY_SENTINEL = "ticket-sentinel-6b0e"
GENERATED_ID = re.compile(r"[0-9a-f]{32}")
ACCEPTED = 101
REFUSED = 403
POLICY = 1008
NORMAL = 1000
EXIT_CODE = 3
SESSION_ACTIONS = ("container_exec_started", "container_exec_ended")


@pytest.fixture
def env(env: dict[str, str]) -> dict[str, str]:
    return {**env, "DOCKMIN_ACCESS_LOG": "true"}


@pytest.fixture
def app(private_app: FastAPI) -> FastAPI:
    return private_app


def access_lines(log_stream: Any) -> str:
    """Return the access records of the log, which is all this feature writes."""
    lines = (line for line in log_stream.getvalue().splitlines())
    return "\n".join(line for line in lines if json.loads(line)["logger"] == "dockmin.access")


async def session_request_ids(app: FastAPI) -> list[tuple[str, str | None]]:
    """Return the action and request id of every terminal session row, oldest first."""
    async with app.state.database.session() as db:
        rows = await db.fetch_limited(
            "SELECT action, request_id FROM activity_event "
            "WHERE action IN ('container_exec_started', 'container_exec_ended') ORDER BY id",
            (),
            limit=100,
        )
    return [(row["action"], row["request_id"]) for row in rows]


async def run_session(live: Live, world: World, *, headers: dict[str, str] | None = None) -> Any:
    ticket = await issue(live, world.local_id)
    async with websockets.connect(
        live.socket_url(f"{ticket.websocket_path}?probe={QUERY_SENTINEL}"),
        additional_headers={**live.headers(), **(headers or {})},
        logger=CLIENT_LOGGER,
        open_timeout=WAIT_SECONDS,
    ) as socket:
        await redeem(socket, ticket)
        await socket.send(f"exit {EXIT_CODE}\r".encode())
        closed = await drain(socket)
    return ticket, closed


async def test_a_terminal_connection_writes_one_line_at_close_and_its_rows_carry_the_requests_id(
    live: Live,
    world: World,
    terminal_daemon: TerminalEngine,
    logged: Any,
    log_stream: Any,
) -> None:
    ticket, closed = await run_session(live, world)

    await until(lambda: len(logged("websocket_connection")) == 1)
    [record] = logged("websocket_connection")
    rows = await session_request_ids(live.app)
    assert closed.code == NORMAL
    assert record["path"] == ticket.websocket_path
    assert (record["status"], record["close_code"], record["user_id"]) == (
        ACCEPTED,
        NORMAL,
        ADMIN_ID,
    )
    assert isinstance(record["duration_ms"], int)
    assert GENERATED_ID.fullmatch(record["request_id"])
    assert [action for action, _ in rows] == list(SESSION_ACTIONS)
    assert {request_id for _, request_id in rows} == {record["request_id"]}
    assert QUERY_SENTINEL not in access_lines(log_stream)
    assert ticket.ticket not in access_lines(log_stream)


async def test_an_x_request_id_on_the_upgrade_is_the_id_of_the_line_and_the_rows(
    live: Live, world: World, terminal_daemon: TerminalEngine, logged: Any
) -> None:
    await run_session(live, world, headers={"X-Request-Id": CHOSEN_ID})

    await until(lambda: len(logged("websocket_connection")) == 1)
    [record] = logged("websocket_connection")
    rows = await session_request_ids(live.app)
    assert record["request_id"] == CHOSEN_ID
    assert {request_id for _, request_id in rows} == {CHOSEN_ID}


async def test_two_connections_write_two_lines_with_different_request_ids(
    live: Live, world: World, terminal_daemon: TerminalEngine, logged: Any
) -> None:
    await run_session(live, world)
    await run_session(live, world)

    await until(lambda: len(logged("websocket_connection")) == 2)
    first, second = logged("websocket_connection")
    assert first["request_id"] != second["request_id"]


async def test_a_refused_upgrade_writes_one_line_with_the_refusal_status_and_no_user(
    live: Live, world: World, terminal_daemon: TerminalEngine, logged: Any, log_stream: Any
) -> None:
    path = socket_path(world.local_id)

    status = await refused_status(live, f"{path}?probe={QUERY_SENTINEL}", live.headers(token=None))

    await until(lambda: len(logged("websocket_connection")) == 1)
    [record] = logged("websocket_connection")
    assert status == REFUSED
    assert record["path"] == path
    assert (record["status"], record["close_code"], record["user_id"]) == (REFUSED, POLICY, None)
    assert QUERY_SENTINEL not in access_lines(log_stream)
