"""Tests for the terminal WebSocket, served by uvicorn on a loopback port and driven as a client."""

import asyncio
import base64
import json
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

import pytest
from fastapi import FastAPI

import app.logging as app_logging
import app.web.container_stream_routes as stream_routes
from app.services import terminals
from app.services.object_locks import TooManyStreams
from app.services.terminals import TerminalLimits
from tests.container_support import CACHE, DB, JOB
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.terminal_engine import Call, TerminalEngine
from tests.terminal_support import (
    LONG_AGO,
    PLAIN_CONTAINER,
    SENTINEL,
    STANDARD_ACCOUNT,
    Account,
    Live,
    add_account,
    connect,
    drain,
    first_message_close,
    issue,
    leave,
    read_until,
    receive_burst,
    redeem,
    refused_status,
    rows_of,
    serving,
    socket_path,
    sql,
    until,
)
from tests.web.environment_support import World

BURST_BYTES = 3_000_000
EXIT_CODE = 3
NORMAL, POLICY, TOO_LARGE, ACCESS_ENDED, IDLE, NO_SLOT = 1000, 1008, 1009, 4403, 4408, 4429
BEARER = "Bearer dkm_AAAAAAAA_" + "A" * 43
REFUSED = 403


@pytest.fixture(autouse=True)
def quick_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminals, "PROCESS_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(terminals, "PROCESS_POLL_SECONDS", 0.005)


def bridge_tasks() -> list[asyncio.Task[Any]]:
    """Return the live tasks that belong to a terminal bridge."""
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and "TerminalBridge." in getattr(task.get_coro(), "__qualname__", "")
    ]


async def ended_rows(
    app: FastAPI, action: str = "container_exec_ended", count: int = 1
) -> list[Any]:
    """Wait for the session's ended row, which is written after the socket closes."""
    rows: list[Any] = []

    async def written() -> bool:
        rows[:] = await rows_of(app, action)
        return len(rows) >= count

    await until(written)
    return rows


async def send_resizes(socket: Any, sizes: list[tuple[int, int]]) -> None:
    for rows, cols in sizes:
        await socket.send(json.dumps({"t": "resize", "cols": cols, "rows": rows}))


async def refusals(live: Live, variants: dict[str, tuple[str, dict[str, str]]]) -> dict[str, int]:
    return {
        label: await refused_status(live, path, headers)
        for label, (path, headers) in variants.items()
    }


async def test_a_handshake_that_is_not_a_same_origin_browser_session_is_refused_before_accept(
    live: Live, world: World, terminal_daemon: TerminalEngine, caplog: pytest.LogCaptureFixture
) -> None:
    path = socket_path(world.local_id)
    caplog.set_level(logging.DEBUG)
    variants: dict[str, tuple[str, dict[str, str]]] = {
        "no origin": (path, {"Cookie": live.cookie()}),
        "null origin": (path, live.headers(origin="null")),
        "foreign origin": (path, live.headers(origin="http://evil.test")),
        "foreign origin on the same host name": (
            path,
            live.headers(origin=f"http://attacker.localhost:{live.port}"),
        ),
        "origin that is not a url": (path, live.headers(origin="evil.test")),
        "no session cookie": (path, live.headers(token=None)),
        "bearer token only": (path, {**live.headers(token=None), "Authorization": BEARER}),
        "cookie that is not a session": (path, live.headers(token="not-a-session")),
        "well-formed cookie nobody holds": (path, live.headers(token="Z" * 43)),
        "standard user on the local socket": (
            path,
            live.headers(token=STANDARD_ACCOUNT.token),
        ),
        "container that does not exist": (socket_path(world.local_id, "c" * 64), live.headers()),
        "name instead of an id": (socket_path(world.local_id, "web"), live.headers()),
        "environment that does not exist": (socket_path(9999), live.headers()),
    }

    statuses = await refusals(live, variants)

    assert statuses == dict.fromkeys(variants, REFUSED)
    assert terminal_daemon.execs == {}
    assert terminal_daemon.sessions == []
    assert [r for r in terminal_daemon.requests if r.method == "POST"] == []
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


async def misused_tickets(live: Live, world: World, other: Account) -> dict[str, int | None]:
    env = world.local_id
    path = socket_path(env)
    results: dict[str, int | None] = {}

    used = await issue(live, env)
    async with connect(live, path) as socket:
        await redeem(socket, used)
    results["replayed"] = await first_message_close(
        live, path, json.dumps({"t": "ticket", "v": used.ticket})
    )

    results["binary first message"] = await first_message_close(live, path, b"\x00\x01")
    results["text that is not json"] = await first_message_close(live, path, "ticket please")
    results["oversize message"] = await first_message_close(
        live, path, json.dumps({"t": "ticket", "v": "a" * 600})
    )

    elsewhere = await issue(live, env)
    results["another container"] = await first_message_close(
        live,
        socket_path(env, PLAIN_CONTAINER),
        json.dumps({"t": "ticket", "v": elsewhere.ticket}),
    )

    borrowed = await issue(live, env)
    results["another users session"] = await first_message_close(
        live, path, json.dumps({"t": "ticket", "v": borrowed.ticket}), token=other.token
    )

    results["nothing sent"] = await first_message_close(live, path, None)
    return results


async def test_a_ticket_that_is_missing_replayed_or_meant_for_something_else_closes_the_socket(
    live: Live,
    world: World,
    terminal_daemon: TerminalEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream_routes, "HANDSHAKE_SECONDS", 0.3)
    other = await add_account(live.app, "other-admin")

    closes = await misused_tickets(live, world, other)

    assert set(closes.values()) == {POLICY}
    assert len(closes) == 7
    assert len(terminal_daemon.execs) == 1


async def test_an_exec_session_echoes_resizes_survives_a_burst_and_reports_the_exit(
    live: Live, world: World, terminal_daemon: TerminalEngine
) -> None:
    ticket = await issue(
        live,
        world.local_id,
        form={"command": "/bin/sh -c ok", "working_dir": "/srv", "rows": "30", "cols": "100"},
    )
    burst_command = f"burst {BURST_BYTES}\r".encode()

    async with connect(live, ticket.websocket_path) as socket:
        await redeem(socket, ticket)
        await socket.send(b"hello\r")
        echoed = await read_until(socket, b"hello\r")
        await send_resizes(socket, [(40, 101), (40, 102), (40, 103)])
        await until(
            lambda: bool(terminal_daemon.resizes) and terminal_daemon.resizes[-1].cols == 103
        )
        await socket.send(burst_command)
        received = await receive_burst(socket, len(burst_command) + BURST_BYTES)
        await socket.send(f"exit {EXIT_CODE}\r".encode())
        closed = await drain(socket)

    started = await rows_of(live.app, "container_exec_started")
    ended = await ended_rows(live.app)
    created = terminal_daemon.execs[next(iter(terminal_daemon.execs))].body
    assert echoed == b"hello\r"
    assert received == len(burst_command) + BURST_BYTES
    assert closed.end == {"t": "end", "reason": "process_exited", "exit_code": EXIT_CODE}
    assert closed.code == NORMAL
    assert created["ConsoleSize"] == [30, 100]
    assert created["WorkingDir"] == "/srv"
    assert created["Cmd"] == ["/bin/sh", "-c", "ok"]
    assert "Privileged" not in created
    assert terminal_daemon.resizes[-1].rows == 40
    assert len(terminal_daemon.resizes) <= 3
    assert started[0]["detail"]["session"] == ended[0]["detail"]["session"]
    assert ended[0]["detail"]["end_reason"] == "process_exited"
    assert live.app.state.engine.stream_slots.open_count == 0
    await terminal_daemon.wait_for_no_connections()


async def leave_after_ready(live: Live, world: World, how: str) -> int:
    ticket = await issue(live, world.local_id)
    async with connect(live, ticket.websocket_path) as socket:
        await redeem(socket, ticket)
        opened = len(bridge_tasks())
        await leave(socket, how)
    return opened


async def test_leaving_the_page_closes_the_engine_stream_and_leaves_no_task_slot_or_connection(
    live: Live, world: World, terminal_daemon: TerminalEngine
) -> None:
    polite = await leave_after_ready(live, world, "close")
    dropped = await leave_after_ready(live, world, "abort")

    ended = await ended_rows(live.app, count=2)
    await terminal_daemon.wait_for_no_connections()
    assert min(polite, dropped) >= 1
    assert [row["detail"]["end_reason"] for row in ended] == ["client_closed"] * 2
    assert [row["detail"]["process_running"] for row in ended] == [True, True]
    assert live.app.state.engine.stream_slots.open_count == 0
    assert len(live.app.state.engine.attach_locks) == 0
    await until(lambda: bridge_tasks() == [])


async def test_an_idle_terminal_is_warned_closed_by_the_timeout_and_the_closure_is_recorded(
    live: Live,
    world: World,
    terminal_daemon: TerminalEngine,
    terminal_limits: Callable[[TerminalLimits], None],
) -> None:
    terminal_limits(TerminalLimits(idle=0.5, idle_warning=0.25, recheck=60))
    ticket = await issue(live, world.local_id)

    async with connect(live, ticket.websocket_path) as socket:
        await redeem(socket, ticket)
        closed = await drain(socket)

    ended = await ended_rows(live.app)
    assert {"t": "notice", "code": "idle_warning"} in closed.texts
    assert closed.end["reason"] == "idle_timeout"
    assert closed.code == IDLE
    assert ended[0]["detail"]["end_reason"] == "idle_timeout"


async def open_sessions(
    stack: AsyncExitStack, live: Live, environment_id: int, accounts: dict[str, Account]
) -> dict[str, Any]:
    """Open a ready terminal for each account; the stack closes the sockets."""
    sockets: dict[str, Any] = {}
    for name, account in accounts.items():
        ticket = await issue(live, environment_id, account=account)
        sockets[name] = await stack.enter_async_context(
            connect(live, ticket.websocket_path, token=account.token)
        )
        await redeem(sockets[name], ticket)
    return sockets


async def test_a_session_ends_when_its_browser_session_or_the_users_standing_is_lost(
    live: Live,
    world: World,
    terminal_daemon: TerminalEngine,
    terminal_limits: Callable[[TerminalLimits], None],
) -> None:
    terminal_limits(TerminalLimits(recheck=0.2))
    names = ("signed-out", "disabled", "demoted", "expired", "steady")
    accounts = {name: await add_account(live.app, name) for name in names}
    app = live.app

    async with AsyncExitStack() as stack:
        sockets = await open_sessions(stack, live, world.local_id, accounts)
        await sql(
            app, "DELETE FROM session WHERE id = ?", accounts["signed-out"].principal.session_id
        )
        await sql(
            app,
            "UPDATE user SET is_enabled = 0 WHERE id = ?",
            accounts["disabled"].principal.user_id,
        )
        await sql(
            app,
            "UPDATE user SET role = 'standard' WHERE id = ?",
            accounts["demoted"].principal.user_id,
        )
        await sql(
            app,
            "UPDATE session SET expires_at = ? WHERE id = ?",
            LONG_AGO,
            accounts["expired"].principal.session_id,
        )
        closed = {name: await drain(sockets[name]) for name in names[:4]}
        await sockets["steady"].close()
        rows = await ended_rows(app, count=5)

    assert {name: (c.end["reason"], c.code) for name, c in closed.items()} == {
        "signed-out": ("access_revoked", ACCESS_ENDED),
        "disabled": ("access_revoked", ACCESS_ENDED),
        "demoted": ("access_revoked", ACCESS_ENDED),
        "expired": ("session_expired", ACCESS_ENDED),
    }
    assert sorted(row["detail"]["end_reason"] for row in rows) == [
        "access_revoked",
        "access_revoked",
        "access_revoked",
        "client_closed",
        "session_expired",
    ]


async def test_closing_the_environments_connections_ends_an_open_session_as_environment_closed(
    live: Live, world: World, terminal_daemon: TerminalEngine
) -> None:
    ticket = await issue(live, world.local_id)

    async with connect(live, ticket.websocket_path) as socket:
        await redeem(socket, ticket)
        await live.app.state.engine.registry.aclose()
        closed = await drain(socket)

    ended = await ended_rows(live.app)
    assert closed.end["reason"] == "environment_closed"
    assert closed.code == NORMAL
    assert ended[0]["detail"]["end_reason"] == "environment_closed"


async def test_the_server_shutting_down_ends_an_open_session_as_environment_closed(
    live: Live, world: World, terminal_daemon: TerminalEngine
) -> None:
    ticket = await issue(live, world.local_id)

    async with connect(live, ticket.websocket_path) as socket:
        await redeem(socket, ticket)
        live.server.should_exit = True
        await drain(socket)

    ended = await ended_rows(live.app)
    assert ended[0]["detail"]["end_reason"] == "environment_closed"
    assert live.app.state.engine.stream_slots.open_count == 0


DISALLOWED: dict[str, tuple[str | bytes, int]] = {
    "oversize input": (b"x" * 65_537, TOO_LARGE),
    "unknown control message": ('{"t":"zzz"}', POLICY),
    "oversize control message": ("x" * 513, TOO_LARGE),
}


async def break_the_protocol(live: Live, world: World, message: str | bytes) -> int | None:
    ticket = await issue(live, world.local_id)
    async with connect(live, ticket.websocket_path) as socket:
        await redeem(socket, ticket)
        await socket.send(message)
        return (await drain(socket)).code


async def break_every_rule(live: Live, world: World) -> dict[str, int | None]:
    return {
        label: await break_the_protocol(live, world, message)
        for label, (message, _) in DISALLOWED.items()
    }


async def test_a_message_the_protocol_does_not_allow_closes_the_session_and_records_why(
    live: Live, world: World, terminal_daemon: TerminalEngine
) -> None:
    codes = await break_every_rule(live, world)

    ended = await ended_rows(live.app, count=3)
    assert codes == {label: code for label, (_, code) in DISALLOWED.items()}
    assert [row["detail"]["end_reason"] for row in ended] == ["protocol_error"] * 3


async def test_an_attach_session_resizes_the_container_and_a_second_attach_is_told_it_is_busy(
    live: Live, world: World, terminal_daemon: TerminalEngine
) -> None:
    first = await issue(live, world.local_id, mode="attach", form={"rows": "20", "cols": "90"})
    second = await issue(live, world.local_id, mode="attach")

    async with connect(live, first.websocket_path) as socket:
        await redeem(socket, first)
        async with connect(live, second.websocket_path) as rival:
            await rival.send(json.dumps({"t": "ticket", "v": second.ticket}))
            refused = await drain(rival)
        await socket.send(b"hello\r")
        await read_until(socket, b"hello\r")
        await until(lambda: any(r.kind == "container" for r in terminal_daemon.resizes))
        terminal_daemon.hang_up()
        closed = await drain(socket)

    started = await rows_of(live.app, "container_attach_started")
    ended = await ended_rows(live.app, "container_attach_ended")
    assert refused.end == {
        "t": "end",
        "reason": "attach_busy",
        "exit_code": None,
        "message": "Another session is already attached to this container.",
    }
    assert (refused.code, closed.code) == (NORMAL, NORMAL)
    assert closed.end["reason"] == "detached"
    assert [(r.kind, r.rows, r.cols) for r in terminal_daemon.resizes] == [("container", 20, 90)]
    assert [(r["result"], r["detail"].get("reason")) for r in started] == [
        ("success", None),
        ("failure", "attach_busy"),
    ]
    assert ended[0]["detail"]["end_reason"] == "detached"
    assert len(live.app.state.engine.attach_locks) == 0


async def test_the_third_terminal_of_a_user_is_refused_with_the_sentence_that_explains_it(
    live: Live, world: World, terminal_daemon: TerminalEngine
) -> None:
    env = world.local_id
    first, second = await issue(live, env), await issue(live, env)

    async with connect(live, first.websocket_path) as one:
        await redeem(one, first)
        third = await issue(live, env)
        async with connect(live, second.websocket_path) as two:
            await redeem(two, second)
            async with connect(live, third.websocket_path) as late:
                await late.send(json.dumps({"t": "ticket", "v": third.ticket}))
                refused = await drain(late)
            with pytest.raises(TooManyStreams) as at_prepare:
                await issue(live, env)

    sentence = "You have 2 terminals open. Close one, then try again."
    assert refused.end == {
        "t": "end",
        "reason": "slots_user",
        "exit_code": None,
        "message": sentence,
    }
    assert refused.code == NO_SLOT
    assert str(at_prepare.value) == sentence
    assert len(terminal_daemon.sessions) == 2


def forms_of(marker: str) -> list[str]:
    return [marker, marker.encode().hex(), base64.b64encode(marker.encode()).decode()]


def leaks(records: list[logging.LogRecord], rendered: str, forms: list[str]) -> list[str]:
    """Return every place a marker form shows up: log records of any logger, and the JSON output."""
    places = [
        f"{record.name}: {text}"
        for record in records
        for text in (record.getMessage(), repr(record.__dict__), repr(record.exc_info))
        if any(form in text for form in forms)
    ]
    return places + [line for line in rendered.splitlines() if any(f in line for f in forms)]


async def drive_marker_sessions(live: Live, world: World, daemon: TerminalEngine) -> None:
    env = world.local_id
    command = f"/bin/sh -c {SENTINEL}-argument"
    first = await issue(live, env, form={"command": command, "working_dir": f"/srv/{SENTINEL}"})
    async with connect(live, first.websocket_path) as socket:
        await redeem(socket, first)
        await socket.send(f"{SENTINEL}-typed\r".encode())
        await read_until(socket, f"{SENTINEL}-typed".encode())
        await socket.send(json.dumps({"t": "resize", "cols": SENTINEL, "rows": 2}))
        await drain(socket)
    await ended_rows(live.app)
    daemon.refuse(Call.START_EXEC, 500)
    second = await issue(live, env, form={"command": command})
    async with connect(live, second.websocket_path) as socket:
        await socket.send(json.dumps({"t": "ticket", "v": second.ticket}))
        await drain(socket)
    await ended_rows(live.app, "container_exec_started", count=2)


@pytest.fixture
def every_logger_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """Defeat the level pins: the libraries that could log frame contents are asked to."""
    caplog.set_level(logging.DEBUG)
    for name in ("websockets", "websockets.server", "websockets.protocol", "uvicorn.asgi"):
        caplog.set_level(logging.DEBUG, logger=name)
    caplog.set_level(5, logger="uvicorn.error")


async def test_no_terminal_content_reaches_any_log_record_or_activity_row(
    live: Live,
    world: World,
    terminal_daemon: TerminalEngine,
    every_logger_at_debug: None,
    caplog: pytest.LogCaptureFixture,
    log_stream: Any,
) -> None:
    forms = forms_of(SENTINEL)

    await drive_marker_sessions(live, world, terminal_daemon)

    rows = json.dumps(
        [await rows_of(live.app, a) for a in ("container_exec_started", "container_exec_ended")]
    )
    assert terminal_daemon.sessions, "the marker session must have run"
    assert leaks(caplog.records, log_stream.getvalue(), forms) == []
    assert [form for form in forms if form in rows] == []
    assert "-argument" not in rows


async def test_the_marker_check_sees_a_leak_when_the_payload_filter_is_removed(
    live: Live,
    world: World,
    terminal_daemon: TerminalEngine,
    every_logger_at_debug: None,
    caplog: pytest.LogCaptureFixture,
    log_stream: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_logging, "WEBSOCKETS_PACKAGE_DIRECTORY", "/nowhere-that-matches/")

    await drive_marker_sessions(live, world, terminal_daemon)

    assert leaks(caplog.records, log_stream.getvalue(), forms_of(SENTINEL)) != []


async def test_a_read_only_or_hidden_container_is_refused_at_the_handshake_a_writable_one_is_not(
    live: Live, world: World, shared_host: ContainerEngine
) -> None:
    headers = live.headers(STANDARD_ACCOUNT.token)
    variants = {
        "read-only grant": (socket_path(world.remote_id, JOB), headers),
        "no grant": (socket_path(world.remote_id, CACHE), headers),
        "write grant": (socket_path(world.remote_id, DB), headers),
    }

    statuses = await refusals(live, variants)

    assert statuses == {"read-only grant": REFUSED, "no grant": REFUSED, "write grant": 101}
    assert [r for r in shared_host.requests if "/exec" in r.path or "/attach" in r.path] == []


async def test_a_handshake_made_before_setup_is_complete_is_refused(fresh_app: FastAPI) -> None:
    async with serving(fresh_app) as served:
        status = await refused_status(served, socket_path(1), served.headers(token=None))

    assert status == REFUSED
