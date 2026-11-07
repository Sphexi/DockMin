"""Tests for exec, attach, resize and log streaming against a fake daemon on a real socket."""

import asyncio
import json
from contextlib import aclosing

import pytest

from app.docker_engine import (
    Conflict,
    EngineError,
    EnvironmentUnreachable,
    NotFound,
    PermissionDenied,
    RequestOutcomeUnknown,
    ResponseTooLarge,
)
from app.docker_engine.client import EngineClient
from app.docker_engine.resources import (
    DETACH_KEYS,
    EXEC_INSPECT_BYTES,
    LOG_FRAME_BYTES,
    MAX_TERMINAL_COLUMNS,
    MAX_TERMINAL_ROWS,
    exec_create_body,
    format_engine_timestamp,
)
from app.docker_engine.transport import (
    HijackedConnection,
    TcpSettings,
    TlsMaterial,
)
from tests.docker_engine.fake_engine import BODY_TOKEN
from tests.docker_engine.helpers import STDERR, STDOUT, drain, log_frame
from tests.docker_engine.pki import LOOPBACK, Pki, server_ssl_context
from tests.docker_engine.terminal_engine import (
    MULTIPLEXED_STREAM,
    RAW_STREAM,
    Call,
    serve_terminal_engine,
    serve_terminal_engine_tls,
)

CONTAINER = "c" * 64
ROWS, COLS = 24, 80
BURST_BYTES = 1024 * 1024
EXIT_CODE = 3
API_VERSION = "1.51"
FIRST_STAMP_NS = 1_700_000_000_123_456_789
UNTIL_SECONDS = 1_800_000_000


async def read_until(connection: HijackedConnection, needle: bytes) -> bytes:
    received = b""
    async with asyncio.timeout(5):
        while needle not in received:
            chunk = await connection.read()
            assert chunk, "the stream ended before the expected output"
            received += chunk
    return received


async def read_exactly_n(connection: HijackedConnection, count: int) -> int:
    total = 0
    async with asyncio.timeout(10):
        while total < count:
            total += len(await connection.read(65536))
    return total


async def hijack(client: EngineClient, call: Call, exec_id: str) -> HijackedConnection:
    if call is Call.START_EXEC:
        return await client.execs.start(exec_id)
    return await client.containers.attach(CONTAINER, detach_keys=DETACH_KEYS)


async def new_exec(client: EngineClient) -> str:
    return await client.containers.create_exec(CONTAINER, exec_create_body(["sh"], rows=1, cols=1))


async def test_an_exec_session_round_trips_input_resizes_bursts_and_reports_its_exit() -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        body = exec_create_body(
            ["/bin/sh", "-c", "a b"], rows=ROWS, cols=COLS, working_dir="/tmp", user="root"
        )

        exec_id = await client.containers.create_exec(CONTAINER, body)
        connection = await client.execs.start(exec_id)
        await connection.write(b"hello\r")
        echoed = await read_until(connection, b"hello\r")
        await client.execs.resize(exec_id, 30, 100)
        running = await client.execs.inspect(exec_id)
        command = f"burst {BURST_BYTES}\r".encode()
        await connection.write(command)
        burst = await read_exactly_n(connection, len(command) + BURST_BYTES)
        await connection.write(f"exit {EXIT_CODE}\r".encode())
        await read_until(connection, f"exit {EXIT_CODE}\r".encode())
        finished = await client.execs.inspect(exec_id)
        held = client.open_hijack_count
        await connection.close()

        created = daemon.calls("POST", f"/containers/{CONTAINER}/exec")[0]
        started = daemon.calls("POST", f"/exec/{exec_id}/start")[0]
        sent = json.loads(created.body)
        assert created.version == API_VERSION
        assert sent == {
            "Cmd": ["/bin/sh", "-c", "a b"],
            "ConsoleSize": [ROWS, COLS],
            "WorkingDir": "/tmp",
            "User": "root",
            "AttachStdin": True,
            "AttachStdout": True,
            "AttachStderr": True,
            "Tty": True,
            "DetachKeys": DETACH_KEYS,
        }
        assert started.version == API_VERSION
        assert started.headers["upgrade"] == "tcp"
        assert json.loads(started.body) == {"Detach": False, "Tty": True}
        assert echoed == b"hello\r"
        assert burst == len(command) + BURST_BYTES
        assert [(r.kind, r.rows, r.cols) for r in daemon.resizes] == [("exec", 30, 100)]
        assert (running.running, running.exit_code) == (True, None)
        assert (finished.running, finished.exit_code) == (False, EXIT_CODE)
        assert (held, client.open_hijack_count) == (1, 0)


async def test_attach_hijacks_the_container_with_detach_keys_and_resizes_the_container() -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()

        connection = await client.containers.attach(CONTAINER, detach_keys=DETACH_KEYS)
        await client.containers.resize(CONTAINER, 10, 20)
        await connection.write(b"ping\r")
        echoed = await read_until(connection, b"ping\r")
        await connection.close()

        request = daemon.calls("POST", f"/containers/{CONTAINER}/attach")[0]
        assert request.query == {
            "stream": "1",
            "stdin": "1",
            "stdout": "1",
            "stderr": "1",
            "logs": "0",
            "detachKeys": DETACH_KEYS,
        }
        assert request.version == API_VERSION
        assert echoed == b"ping\r"
        assert [(r.kind, r.target, r.rows, r.cols) for r in daemon.resizes] == [
            ("container", CONTAINER, 10, 20)
        ]


@pytest.mark.parametrize(
    "body",
    [
        {"Cmd": ["sh"], "Privileged": True},
        {"Cmd": ["sh"], "Env": ["A=1"]},
        {"Cmd": "sh -c x"},
        {"Cmd": []},
        {"Cmd": ["sh", 5]},
        {},
    ],
    ids=[
        "privileged",
        "env",
        "string-command",
        "empty-command",
        "non-string-argument",
        "no-command",
    ],
)
async def test_an_exec_body_outside_the_allow_list_or_malformed_is_refused_unsent(
    body: dict[str, object],
) -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()

        with pytest.raises(EngineError):
            await client.containers.create_exec(CONTAINER, body)

        assert daemon.calls("POST", f"/containers/{CONTAINER}/exec") == []


async def test_the_terminal_options_and_detach_keys_are_forced_whatever_the_body_asks() -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        asked = {"Cmd": ["sh"], "Tty": False, "DetachKeys": "ctrl-p,ctrl-q", "AttachStdin": False}

        await client.containers.create_exec(CONTAINER, asked)

        sent = json.loads(daemon.calls("POST", f"/containers/{CONTAINER}/exec")[0].body)
        assert (sent["Tty"], sent["DetachKeys"], sent["AttachStdin"]) == (True, DETACH_KEYS, True)


@pytest.mark.parametrize(
    ("rows", "cols"),
    [(0, COLS), (MAX_TERMINAL_ROWS + 1, COLS), (ROWS, 0), (ROWS, MAX_TERMINAL_COLUMNS + 1)],
)
async def test_a_resize_outside_the_supported_range_is_refused_unsent(rows: int, cols: int) -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        exec_id = await new_exec(client)

        with pytest.raises(EngineError):
            await client.execs.resize(exec_id, rows, cols)
        with pytest.raises(EngineError):
            await client.containers.resize(CONTAINER, rows, cols)

        assert daemon.resizes == []


def test_the_exec_body_builder_refuses_an_empty_command_and_an_out_of_range_size() -> None:
    with pytest.raises(ValueError):
        exec_create_body([], rows=ROWS, cols=COLS)
    with pytest.raises(ValueError):
        exec_create_body(["sh"], rows=0, cols=COLS)
    with pytest.raises(ValueError):
        exec_create_body(["sh"], rows=ROWS, cols=MAX_TERMINAL_COLUMNS + 1)


async def test_a_reply_that_is_not_a_container_id_is_refused_as_malformed() -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        daemon.malformed_exec_id = "not-a-hex-id"

        with pytest.raises(EngineError, match="malformed id"):
            await new_exec(client)


async def test_a_malformed_detach_key_or_container_reference_is_refused_unsent() -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()

        with pytest.raises(EngineError):
            await client.containers.attach(CONTAINER, detach_keys="a\r\nb")
        with pytest.raises(EngineError):
            await client.containers.attach("a/../b", detach_keys=DETACH_KEYS)

        assert daemon.calls("POST", f"/containers/{CONTAINER}/attach") == []


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (404, NotFound),
        (409, Conflict),
        (401, PermissionDenied),
        (403, PermissionDenied),
        (500, EngineError),
        (502, EnvironmentUnreachable),
        (504, EnvironmentUnreachable),
    ],
)
@pytest.mark.parametrize("call", [Call.START_EXEC, Call.ATTACH])
async def test_a_refused_hijack_is_mapped_by_status_and_leaks_neither_body_nor_connection(
    call: Call, status: int, error: type[EngineError]
) -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        exec_id = await new_exec(client)
        daemon.refuse(call, status)

        with pytest.raises(error) as refused:
            await hijack(client, call, exec_id)

        caught = refused.value
        assert type(caught) is error
        assert (caught.__context__, caught.__cause__) == (None, None)
        assert BODY_TOKEN not in f"{caught}{caught!r}"
        assert client.open_hijack_count == 0
        await daemon.wait_for_no_connections()


@pytest.mark.parametrize("status", [409, 500, 403])
async def test_only_a_not_found_refusal_may_carry_engine_text(status: int) -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        exec_id = await new_exec(client)
        daemon.refuse(Call.START_EXEC, status)

        with pytest.raises(EngineError) as refused:
            await client.execs.start(exec_id)

        assert refused.value.engine_message is None


async def test_a_connection_dropped_after_the_request_was_written_is_an_unknown_outcome() -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        exec_id = await new_exec(client)
        daemon.drop(Call.START_EXEC)

        with pytest.raises(RequestOutcomeUnknown):
            await client.execs.start(exec_id)

        assert client.open_hijack_count == 0


async def test_a_host_that_cannot_be_reached_is_unreachable_not_an_unknown_outcome() -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        exec_id = await new_exec(client)
        await daemon.stop_listening()

        with pytest.raises(EnvironmentUnreachable) as refused:
            await client.execs.start(exec_id)

        assert not isinstance(refused.value, RequestOutcomeUnknown)


async def test_a_client_built_without_connection_settings_refuses_a_hijack() -> None:
    async with serve_terminal_engine() as daemon:
        bare = EngineClient(1, daemon.settings().create_transport())

        with pytest.raises(EnvironmentUnreachable, match="no connection settings"):
            await bare.execs.start("e" * 64)

        await bare.aclose()
        assert daemon.calls("POST") == []


async def test_closing_the_client_closes_every_open_hijack_and_refuses_new_ones() -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        exec_id = await new_exec(client)
        connection = await client.execs.start(exec_id)
        reading = asyncio.create_task(connection.read())
        await asyncio.sleep(0)

        await client.aclose()

        assert await asyncio.wait_for(reading, 5) == b""
        assert client.open_hijack_count == 0
        await daemon.wait_for_no_connections()
        with pytest.raises(EnvironmentUnreachable):
            await client.execs.start(exec_id)


@pytest.mark.parametrize("call", [Call.START_EXEC, Call.ATTACH])
async def test_a_multiplexed_stream_is_refused_and_closed_because_the_bridge_carries_a_tty(
    call: Call,
) -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        exec_id = await new_exec(client)
        daemon.hijack_content_type = MULTIPLEXED_STREAM

        with pytest.raises(EngineError, match="multiplexed"):
            await hijack(client, call, exec_id)

        assert client.open_hijack_count == 0
        await daemon.wait_for_no_connections()


async def test_an_exec_inspect_reply_over_the_cap_is_refused() -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        exec_id = await new_exec(client)
        daemon.inspect_padding_bytes = EXEC_INSPECT_BYTES

        with pytest.raises(ResponseTooLarge):
            await client.execs.inspect(exec_id)


async def test_logs_send_their_bounds_and_decode_frames_split_across_reads() -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        first = log_frame(STDOUT, b"one\n")
        daemon.script_logs(first[:5], first[5:] + log_frame(STDERR, b"two\n"))

        frames = await drain(
            client.containers.logs(
                CONTAINER,
                tty=False,
                since=format_engine_timestamp(FIRST_STAMP_NS),
                until=UNTIL_SECONDS,
                timestamps=True,
                tail=10,
            )
        )

        query = daemon.log_streams[0].request.query
        assert [(f.stream, f.data) for f in frames] == [("stdout", b"one\n"), ("stderr", b"two\n")]
        assert query["since"] == "1700000000.123456789"
        assert query["until"] == str(UNTIL_SECONDS)
        assert (query["timestamps"], query["tail"]) == ("true", "10")


@pytest.mark.parametrize("stamp", ["1e5", "-5", "12.5.5", "abc", "1" * 40])
async def test_a_malformed_log_time_bound_is_refused_unsent(stamp: str) -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()

        with pytest.raises(EngineError):
            await drain(client.containers.logs(CONTAINER, tty=False, since=stamp))

        assert daemon.log_streams == []


async def test_a_log_frame_over_the_given_cap_ends_the_stream_with_an_engine_error() -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        daemon.script_logs(log_frame(STDOUT, b"x" * 2000))

        with pytest.raises(EngineError, match="size limit"):
            await drain(client.containers.logs(CONTAINER, tty=False, max_frame_bytes=1000))


async def test_a_frame_at_the_default_log_cap_is_accepted_and_one_byte_over_is_not() -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        daemon.script_logs(log_frame(STDOUT, b"x" * LOG_FRAME_BYTES))
        accepted = await drain(client.containers.logs(CONTAINER, tty=False))
        daemon.script_logs(log_frame(STDOUT, b"x" * (LOG_FRAME_BYTES + 1)))

        with pytest.raises(EngineError, match="size limit"):
            await drain(client.containers.logs(CONTAINER, tty=False))

        assert [len(f.data) for f in accepted] == [LOG_FRAME_BYTES]


async def test_raw_tty_logs_are_split_at_the_frame_cap_and_reported_as_stdout() -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        daemon.script_logs(b"y" * 2500, content_type=RAW_STREAM)

        frames = await drain(client.containers.logs(CONTAINER, tty=True, max_frame_bytes=1000))

        assert [(f.stream, len(f.data)) for f in frames] == [
            ("stdout", 1000),
            ("stdout", 1000),
            ("stdout", 500),
        ]


async def test_closing_a_followed_log_stream_early_closes_the_connection_to_the_daemon() -> None:
    async with serve_terminal_engine() as daemon:
        client = daemon.client()
        daemon.script_logs(log_frame(STDOUT, b"a\n"), hold=True)
        stream = client.containers.logs(CONTAINER, tty=False)

        async with aclosing(stream):
            first = await anext(stream)
        await asyncio.wait_for(daemon.log_streams[0].client_closed.wait(), 5)

        assert first.data == b"a\n"
        await daemon.wait_for_no_connections()


@pytest.mark.usefixtures("permit_loopback")
async def test_a_hijack_over_mutual_tls_reaches_a_server_the_client_trusts(pki: Pki) -> None:
    material = TlsMaterial(pki.ca_pem, pki.client.cert_pem, pki.client.key_pem)
    async with serve_terminal_engine_tls(server_ssl_context(pki.server, pki.ca_pem)) as daemon:
        client = daemon.client(TcpSettings(LOOPBACK, daemon.port or 0, material))

        connection = await client.execs.start(await new_exec(client))
        await connection.write(b"yo\r")
        echoed = await read_until(connection, b"yo\r")
        await connection.close()

        assert echoed == b"yo\r"


async def test_a_hijack_to_a_server_with_an_untrusted_certificate_is_refused(pki: Pki) -> None:
    material = TlsMaterial(pki.ca_pem, pki.client.cert_pem, pki.client.key_pem)
    context = server_ssl_context(pki.rogue_server, pki.ca_pem)
    async with serve_terminal_engine_tls(context) as daemon:
        client = daemon.client(TcpSettings(LOOPBACK, daemon.port or 0, material))

        with pytest.raises(EnvironmentUnreachable):
            await client.execs.start("e" * 64)

        assert daemon.sessions == []
