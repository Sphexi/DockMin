"""The staged connection test in app/docker_engine/probe.py: which stage fails and how."""

import asyncio
import socket
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.docker_engine.probe import (
    AddrInfo,
    ProbeResult,
    ProbeStatus,
    Stage,
    StageOutcome,
    probe_connection,
)
from app.docker_engine.transport import TcpSettings, TlsMaterial, UnixSocketSettings
from tests.docker_engine.fake_engine import FakeEngine, Reply, json_reply, serve_unix
from tests.docker_engine.pki import LOOPBACK

ENGINE_VERSION = "29.2.1"
ENGINE_HOST_NAME = "probe-host"
UNSUPPORTED_API = "1.40"
UNRESOLVABLE_HOST = "no-such-host.test"
TLS_PORT = 2376
REMOTE_ORDER = [Stage.DNS, Stage.TCP, Stage.TLS_HANDSHAKE, Stage.CERTIFICATE, Stage.ENGINE]
LOCAL_ORDER = [Stage.SOCKET, Stage.ENGINE]


def outcomes(result: ProbeResult) -> dict[Stage, StageOutcome]:
    return {stage.stage: stage.outcome for stage in result.stages}


def stage_order(result: ProbeResult) -> list[Stage]:
    return [stage.stage for stage in result.stages]


def with_info(engine: FakeEngine) -> FakeEngine:
    engine.route(
        "GET",
        "/info",
        json_reply(
            {
                "ServerVersion": ENGINE_VERSION,
                "Name": ENGINE_HOST_NAME,
                "Swarm": {"LocalNodeState": "inactive", "ControlAvailable": False},
            }
        ),
    )
    return engine


@asynccontextmanager
async def plain_listener() -> AsyncIterator[int]:
    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(1)
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, LOOPBACK, 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def closed_port() -> int:
    with socket.socket() as holder:
        holder.bind((LOOPBACK, 0))
        return int(holder.getsockname()[1])


@pytest.fixture
def engine() -> FakeEngine:
    return with_info(FakeEngine())


async def resolver_without_a_name(host: str, port: int) -> Sequence[AddrInfo]:
    raise socket.gaierror(socket.EAI_NONAME, "unknown")


async def test_an_unresolvable_name_fails_at_dns_and_runs_nothing_after_it(
    tls_material: TlsMaterial,
) -> None:
    settings = TcpSettings(UNRESOLVABLE_HOST, TLS_PORT, tls_material)

    result = await probe_connection(settings, resolver=resolver_without_a_name)

    assert result.status is ProbeStatus.UNREACHABLE
    assert result.failed_stage is Stage.DNS
    assert "does not exist" in result.message
    assert stage_order(result) == REMOTE_ORDER
    assert [outcome for stage, outcome in outcomes(result).items() if stage is not Stage.DNS] == [
        StageOutcome.NOT_RUN
    ] * 4


@pytest.mark.usefixtures("permit_loopback")
async def test_a_closed_port_fails_at_tcp_after_dns_is_skipped_for_an_address(
    tls_material: TlsMaterial,
) -> None:
    settings = TcpSettings(LOOPBACK, closed_port(), tls_material)

    result = await probe_connection(settings)

    assert result.failed_stage is Stage.TCP
    assert "refused" in result.message
    assert outcomes(result)[Stage.DNS] is StageOutcome.SKIPPED
    assert outcomes(result)[Stage.TLS_HANDSHAKE] is StageOutcome.NOT_RUN


@pytest.mark.usefixtures("permit_loopback")
async def test_a_listener_that_does_not_speak_tls_fails_at_the_handshake(
    tls_material: TlsMaterial,
) -> None:
    async with plain_listener() as port:
        result = await probe_connection(TcpSettings(LOOPBACK, port, tls_material))

    assert result.failed_stage is Stage.TLS_HANDSHAKE
    assert "does not speak TLS" in result.message
    assert outcomes(result)[Stage.TCP] is StageOutcome.PASSED


@pytest.mark.usefixtures("permit_loopback")
async def test_a_server_certificate_from_an_unknown_ca_fails_at_certificate_verification(
    untrusted_settings: TcpSettings,
) -> None:
    result = await probe_connection(untrusted_settings)

    assert result.failed_stage is Stage.CERTIFICATE
    assert result.status is ProbeStatus.UNREACHABLE
    assert "not issued by the supplied CA" in result.message
    assert outcomes(result)[Stage.TLS_HANDSHAKE] is StageOutcome.PASSED
    assert outcomes(result)[Stage.ENGINE] is StageOutcome.NOT_RUN


def answer_like_a_web_server(engine: FakeEngine) -> None:
    engine.route("GET", "/_ping", Reply(200, b"<html>hello</html>"))
    engine.route("GET", "/version", Reply(404, b"not found"))


def answer_with_an_old_api(engine: FakeEngine) -> None:
    engine.route("GET", "/_ping", Reply(headers={"Api-Version": UNSUPPORTED_API}))


@pytest.mark.parametrize(
    ("script", "status", "fragment"),
    [
        (answer_like_a_web_server, ProbeStatus.UNREACHABLE, "not like a Docker Engine API"),
        (answer_with_an_old_api, ProbeStatus.VERSION_UNSUPPORTED, "Upgrade Docker"),
    ],
    ids=["not an Engine", "unsupported API version"],
)
@pytest.mark.usefixtures("permit_loopback")
async def test_an_endpoint_that_is_not_a_usable_engine_fails_at_the_engine_stage(
    engine: FakeEngine,
    tls_settings: TcpSettings,
    script: Callable[[FakeEngine], None],
    status: ProbeStatus,
    fragment: str,
) -> None:
    script(engine)

    result = await probe_connection(tls_settings)

    assert result.failed_stage is Stage.ENGINE
    assert result.status is status
    assert fragment in result.message
    assert outcomes(result)[Stage.CERTIFICATE] is StageOutcome.PASSED


@pytest.mark.usefixtures("permit_loopback")
async def test_a_reachable_tls_engine_passes_every_stage_and_reports_what_it_is(
    tls_settings: TcpSettings,
) -> None:
    result = await probe_connection(tls_settings)

    assert result.status is ProbeStatus.REACHABLE
    assert result.failed_stage is None
    assert stage_order(result) == REMOTE_ORDER
    assert outcomes(result) == {
        Stage.DNS: StageOutcome.SKIPPED,
        Stage.TCP: StageOutcome.PASSED,
        Stage.TLS_HANDSHAKE: StageOutcome.PASSED,
        Stage.CERTIFICATE: StageOutcome.PASSED,
        Stage.ENGINE: StageOutcome.PASSED,
    }
    assert result.engine is not None
    assert (result.engine.engine_version, result.engine.host_name) == (
        ENGINE_VERSION,
        ENGINE_HOST_NAME,
    )
    assert result.engine.environment_type == "standalone"


@pytest.fixture
def short_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="dmp") as directory:
        yield Path(directory)


def missing_path(directory: Path) -> Path:
    return directory / "docker.sock"


def regular_file(directory: Path) -> Path:
    path = directory / "docker.sock"
    path.write_text("", encoding="utf-8")
    return path


@pytest.mark.parametrize("make", [missing_path, regular_file], ids=["missing", "regular file"])
async def test_a_local_path_that_is_not_a_socket_fails_at_the_socket_stage(
    tmp_path: Path, make: Callable[[Path], Path]
) -> None:
    path = make(tmp_path)

    result = await probe_connection(UnixSocketSettings(str(path)))

    assert result.failed_stage is Stage.SOCKET
    assert stage_order(result) == LOCAL_ORDER
    assert outcomes(result)[Stage.ENGINE] is StageOutcome.NOT_RUN


async def test_a_local_socket_that_serves_an_engine_passes_both_stages(
    engine: FakeEngine, short_dir: Path
) -> None:
    path = str(short_dir / "e.sock")

    async with serve_unix(engine, path):
        result = await probe_connection(UnixSocketSettings(path))

    assert result.status is ProbeStatus.REACHABLE
    assert outcomes(result) == {
        Stage.SOCKET: StageOutcome.PASSED,
        Stage.ENGINE: StageOutcome.PASSED,
    }
