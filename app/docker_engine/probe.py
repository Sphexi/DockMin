"""Staged connection test for one Docker endpoint, naming the stage that fails."""

import asyncio
import socket
import ssl
import stat
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, get_args

import httpx

from app.docker_engine.client import EngineClient
from app.docker_engine.errors import (
    EngineError,
    EnvironmentUnreachable,
    PermissionDenied,
    VersionUnsupported,
)
from app.docker_engine.models import DaemonInfo, SystemInfo
from app.docker_engine.tls_validation import clean_text
from app.docker_engine.transport import (
    ConnectionSettings,
    TcpSettings,
    TransportBundle,
    UnixSocketSettings,
)
from app.docker_engine.versioning import (
    API_VERSION_FLOOR,
    CLIENT_MAX_API_VERSION,
    format_api_version,
    parse_api_version,
)
from app.logging import get_logger
from app.netcheck import (
    Address,
    CertificateFault,
    FactValue,
    TlsFailure,
    classify_certificate_fault,
    classify_tls_failure,
    connect_first,
    dns_failure_message,
    ip_literal,
    literal_address,
    peer_facts,
    seconds_text,
    system_resolver,
    tcp_failure_message,
    usable_addresses,
)
from app.netcheck import AddrInfo as AddrInfo
from app.netcheck import BoundedResolver as BoundedResolver
from app.netcheck import Resolver as Resolver
from app.netcheck import StageOutcome as StageOutcome

MAX_REPLY_HEAD_BYTES = 64 * 1024
MAX_ENGINE_RESPONSE_BYTES = 1024 * 1024

_MAX_UNIX_PATH_BYTES = 107
_INT32_MAX = 2**31 - 1
_INT64_MAX = 2**63 - 1
_PING_REQUEST = (
    "GET /_ping HTTP/1.1\r\nHost: {host}\r\nUser-Agent: dockmin-probe\r\nConnection: close\r\n\r\n"
)

_log = get_logger(__name__)

SwarmStateName = Literal["inactive", "pending", "active", "error", "locked"]
EnvironmentTypeName = Literal["standalone", "swarm"]
_SWARM_STATES: tuple[SwarmStateName, ...] = get_args(SwarmStateName)


class Stage(StrEnum):
    """One step of the connection test; the values match the stored `failed_stage` column."""

    DNS = "dns"
    TCP = "tcp"
    SOCKET = "socket"
    TLS_HANDSHAKE = "tls_handshake"
    CERTIFICATE = "certificate"
    ENGINE = "engine"


_STAGE_SUBJECTS = {
    Stage.DNS: "The name lookup",
    Stage.TCP: "The connection attempt",
    Stage.SOCKET: "Opening the socket",
    Stage.TLS_HANDSHAKE: "The TLS handshake",
    Stage.CERTIFICATE: "The certificate check",
    Stage.ENGINE: "The Engine",
}


class ProbeStatus(StrEnum):
    """The environment status the whole result maps to; the values match the stored column."""

    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    VERSION_UNSUPPORTED = "version_unsupported"


@dataclass(frozen=True, slots=True)
class ProbeTimeouts:
    """Seconds each stage may take, and the cap on the whole test."""

    dns: float = 5.0
    tcp: float = 5.0
    socket: float = 5.0
    tls_handshake: float = 10.0
    engine: float = 10.0
    total: float = 30.0


@dataclass(frozen=True, slots=True)
class StageResult:
    """The outcome of one stage; `message` is product-written and `facts` hold bounded values."""

    stage: Stage
    outcome: StageOutcome
    duration_ms: int
    message: str
    facts: Mapping[str, FactValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EngineFacts:
    """What the daemon reported; every string is cut to 64 characters without control marks."""

    engine_version: str
    api_version: str
    negotiated_api_version: str
    min_api_version: str | None
    swarm_state: SwarmStateName | None
    swarm_control_available: bool | None
    environment_type: EnvironmentTypeName
    operating_system: str
    os_type: str
    architecture: str
    host_name: str
    cpu_count: int
    memory_bytes: int


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The whole test: every stage of the endpoint's kind, in order, and the status they imply."""

    stages: tuple[StageResult, ...]
    status: ProbeStatus
    failed_stage: Stage | None
    message: str
    duration_ms: int
    api_version: str | None
    engine: EngineFacts | None


async def probe_connection(
    settings: ConnectionSettings,
    *,
    environment_id: int = 0,
    timeouts: ProbeTimeouts | None = None,
    resolver: Resolver | None = None,
) -> ProbeResult:
    """Test an endpoint stage by stage and return what each stage found.

    Never raises for a connection problem. The TLS work directory must be configured before a
    remote endpoint is probed.
    """
    run = _Run(timeouts or ProbeTimeouts(), environment_id)
    order: tuple[Stage, ...]
    match settings:
        case UnixSocketSettings():
            order = (Stage.SOCKET, Stage.ENGINE)
            await _probe_local(run, settings)
        case TcpSettings():
            order = (Stage.DNS, Stage.TCP, Stage.TLS_HANDSHAKE, Stage.CERTIFICATE, Stage.ENGINE)
            await _probe_remote(run, settings, resolver or system_resolver)
        case _:
            raise TypeError("only Unix socket and TCP endpoints can be tested")
    return run.finish(order)


class _Run:
    def __init__(self, timeouts: ProbeTimeouts, environment_id: int) -> None:
        self.timeouts = timeouts
        self.environment_id = environment_id
        self.results: list[StageResult] = []
        self.api_version: str | None = None
        self.engine: EngineFacts | None = None
        self.version_unsupported = False
        self._started = time.monotonic()
        self._deadline = asyncio.get_running_loop().time() + timeouts.total

    def budget(self, stage_limit: float) -> tuple[float, bool]:
        """Return the seconds a stage may use and whether the overall cap is what limits it."""
        remaining = self._deadline - asyncio.get_running_loop().time()
        return max(0.0, min(stage_limit, remaining)), remaining < stage_limit

    def add(
        self,
        stage: Stage,
        outcome: StageOutcome,
        message: str,
        started: float,
        facts: Mapping[str, FactValue] | None = None,
    ) -> None:
        self.results.append(
            StageResult(
                stage, outcome, int((time.monotonic() - started) * 1000), message, facts or {}
            )
        )

    def fail(
        self, stage: Stage, message: str, started: float, error: BaseException | None = None
    ) -> None:
        if error is not None:
            _log.debug(
                "connection test stage failed",
                extra={"stage": stage.value, "error_class": type(error).__name__},
            )
        self.add(stage, StageOutcome.FAILED, message, started)

    def timed_out(self, stage: Stage, seconds: float, overall: bool, started: float) -> None:
        if overall:
            message = (
                f"The test reached its overall time limit of {seconds_text(self.timeouts.total)}."
            )
        else:
            message = f"{_STAGE_SUBJECTS[stage]} did not finish within {seconds_text(seconds)}."
        self.fail(stage, message, started)

    def finish(self, order: tuple[Stage, ...]) -> ProbeResult:
        by_stage = {result.stage: result for result in self.results}
        stages = tuple(
            by_stage.get(stage) or StageResult(stage, StageOutcome.NOT_RUN, 0, "Not run.")
            for stage in order
        )
        failed = next((s for s in stages if s.outcome is StageOutcome.FAILED), None)
        duration = int((time.monotonic() - self._started) * 1000)
        if failed is None:
            status = ProbeStatus.REACHABLE
            message = "Connected to the Docker Engine."
            if self.engine is not None:
                message = (
                    f"Connected to Docker Engine {self.engine.engine_version} "
                    f"(API {self.engine.api_version})."
                )
        else:
            status = (
                ProbeStatus.VERSION_UNSUPPORTED
                if self.version_unsupported
                else ProbeStatus.UNREACHABLE
            )
            message = failed.message
        return ProbeResult(
            stages=stages,
            status=status,
            failed_stage=failed.stage if failed else None,
            message=message,
            duration_ms=duration,
            api_version=self.api_version,
            engine=self.engine,
        )


async def _probe_local(run: _Run, settings: UnixSocketSettings) -> None:
    if await _socket_stage(run, settings.path):
        await _engine_stage(run, settings)


async def _probe_remote(run: _Run, settings: TcpSettings, resolver: Resolver) -> None:
    addresses = await _dns_stage(run, settings, resolver)
    if addresses is None:
        return
    connected = await _tcp_stage(run, settings.port, addresses)
    if connected is None:
        return
    try:
        passed = await _tls_stages(run, settings, connected)
    finally:
        connected.close()
    if passed:
        await _engine_stage(run, settings)


async def _socket_stage(run: _Run, path: str) -> bool:
    started = time.monotonic()
    seconds, overall = run.budget(run.timeouts.socket)
    if len(path.encode("utf-8")) > _MAX_UNIX_PATH_BYTES:
        run.fail(Stage.SOCKET, "The path is too long to be a Unix socket address.", started)
        return False
    try:
        async with asyncio.timeout(seconds):
            mode = (await asyncio.to_thread(Path(path).stat)).st_mode
            if not stat.S_ISSOCK(mode):
                run.fail(Stage.SOCKET, "The path exists but is not a Unix socket.", started)
                return False
            _, writer = await asyncio.open_unix_connection(path)
            writer.transport.abort()
    except TimeoutError:
        run.timed_out(Stage.SOCKET, seconds, overall, started)
        return False
    except (FileNotFoundError, NotADirectoryError) as exc:
        run.fail(
            Stage.SOCKET,
            "Nothing exists at this path. Check the path, and that the Docker socket is mounted "
            "into this container if this product runs in one.",
            started,
            exc,
        )
        return False
    except PermissionError as exc:
        run.fail(
            Stage.SOCKET,
            "Permission denied. The account this product runs as cannot open the socket; check "
            "its owner, group and mode.",
            started,
            exc,
        )
        return False
    except ConnectionRefusedError as exc:
        run.fail(
            Stage.SOCKET,
            "The socket exists but nothing is listening on it. Check that the Docker daemon is "
            "running.",
            started,
            exc,
        )
        return False
    except Exception as exc:
        run.fail(Stage.SOCKET, "The socket could not be opened.", started, exc)
        return False
    run.add(Stage.SOCKET, StageOutcome.PASSED, "The socket accepts connections.", started)
    return True


async def _dns_stage(run: _Run, settings: TcpSettings, resolver: Resolver) -> list[Address] | None:
    started = time.monotonic()
    literal = ip_literal(settings.host)
    if literal is not None:
        run.add(
            Stage.DNS,
            StageOutcome.SKIPPED,
            "The host is an IP address, so no name lookup is needed.",
            started,
        )
        return [literal_address(literal, settings.port)]

    seconds, overall = run.budget(run.timeouts.dns)
    try:
        async with asyncio.timeout(seconds):
            infos = await resolver(settings.host, settings.port)
    except TimeoutError:
        run.timed_out(Stage.DNS, seconds, overall, started)
        return None
    except socket.gaierror as exc:
        run.fail(Stage.DNS, dns_failure_message(exc.errno), started, exc)
        return None
    except Exception as exc:
        run.fail(Stage.DNS, "The name lookup failed.", started, exc)
        return None

    addresses = usable_addresses(infos)
    if not addresses:
        run.fail(Stage.DNS, "The name resolved, but to no usable address.", started)
        return None
    run.add(
        Stage.DNS,
        StageOutcome.PASSED,
        f"The name resolved to {len(addresses)} address{'' if len(addresses) == 1 else 'es'}.",
        started,
        {"addresses": len(addresses)},
    )
    return addresses


async def _tcp_stage(run: _Run, port: int, addresses: list[Address]) -> socket.socket | None:
    started = time.monotonic()

    def log_failure(exc: OSError) -> None:
        _log.debug(
            "connection test stage failed",
            extra={"stage": Stage.TCP.value, "error_class": type(exc).__name__},
        )

    result = await connect_first(
        addresses, attempt_limit=run.timeouts.tcp, budget=run.budget, observe=log_failure
    )
    if result.sock is None:
        run.fail(
            Stage.TCP,
            tcp_failure_message(
                result.kinds, tcp_timeout=run.timeouts.tcp, total_timeout=run.timeouts.total
            ),
            started,
        )
        return None
    run.add(
        Stage.TCP,
        StageOutcome.PASSED,
        f"Connected to {result.sockaddr[0]} port {port}.",
        started,
        {"address": str(result.sockaddr[0]), "attempts": result.attempts},
    )
    return result.sock


async def _tls_stages(run: _Run, settings: TcpSettings, connected: socket.socket) -> bool:
    started = time.monotonic()
    assert settings.tls is not None
    try:
        context = await asyncio.to_thread(settings.tls.ssl_context)
    except ValueError as exc:
        run.fail(
            Stage.TLS_HANDSHAKE,
            "The stored TLS material could not be loaded. Replace the CA certificate, client "
            "certificate and client key.",
            started,
            exc,
        )
        return False

    seconds, overall = run.budget(run.timeouts.tls_handshake)
    writer: asyncio.StreamWriter | None = None
    exchanging = False
    try:
        async with asyncio.timeout(seconds):
            reader, writer = await asyncio.open_connection(
                sock=connected,
                ssl=context,
                server_hostname=settings.host,
                limit=MAX_REPLY_HEAD_BYTES,
            )
            exchanging = True
            host_header = settings.host_header
            if "\r" in host_header or "\n" in host_header:
                raise ValueError("Host header must not contain line breaks")
            writer.write(_PING_REQUEST.format(host=host_header).encode("ascii"))
            await writer.drain()
            await _read_reply_head(reader)
            peer = peer_facts(writer.get_extra_info("ssl_object"))
    except TimeoutError:
        if exchanging:
            run.fail(
                Stage.TLS_HANDSHAKE,
                "The TLS handshake completed, but the server did not answer a request within "
                f"{seconds_text(seconds)}. It may be rejecting the client certificate or may not "
                "be a Docker daemon.",
                started,
            )
        else:
            run.timed_out(Stage.TLS_HANDSHAKE, seconds, overall, started)
        return False
    except ssl.SSLCertVerificationError as exc:
        _log.debug(
            "connection test stage failed",
            extra={"stage": Stage.CERTIFICATE.value, "error_class": type(exc).__name__},
        )
        run.add(
            Stage.TLS_HANDSHAKE,
            StageOutcome.PASSED,
            "The server speaks TLS and a protocol version was agreed.",
            started,
        )
        run.add(
            Stage.CERTIFICATE, StageOutcome.FAILED, _certificate_message(exc.verify_code), started
        )
        return False
    except ssl.SSLError as exc:
        run.fail(Stage.TLS_HANDSHAKE, _tls_failure_message(exc, exchanging), started, exc)
        return False
    except (ConnectionError, asyncio.IncompleteReadError, EOFError) as exc:
        run.fail(Stage.TLS_HANDSHAKE, _closed_message(exchanging), started, exc)
        return False
    except OSError as exc:
        run.fail(Stage.TLS_HANDSHAKE, "The TLS connection failed.", started, exc)
        return False
    finally:
        if writer is not None:
            writer.transport.abort()

    run.add(
        Stage.TLS_HANDSHAKE,
        StageOutcome.PASSED,
        "The TLS handshake completed and the server accepted the client certificate.",
        started,
        {"tls_version": peer.version},
    )
    run.add(
        Stage.CERTIFICATE,
        StageOutcome.PASSED,
        "The server's certificate was issued by the supplied CA certificate and covers the "
        "address.",
        started,
        {"expires": peer.expires} if peer.expires else {},
    )
    return True


async def _read_reply_head(reader: asyncio.StreamReader) -> None:
    """Wait for the first reply bytes; the content is not interpreted at this stage."""
    try:
        await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        return
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            return
        raise


def _closed_message(exchanging: bool) -> str:
    if exchanging:
        return (
            "The server closed the connection after the handshake without answering. A daemon "
            "that requires client certificates may have rejected this one."
        )
    return (
        "The server closed the connection during the TLS handshake. It may have rejected the "
        "client certificate or may not support TLS 1.2 or later."
    )


def _tls_failure_message(exc: ssl.SSLError, exchanging: bool) -> str:
    failure = classify_tls_failure(exc)
    if failure is TlsFailure.NOT_TLS and exchanging:
        failure = TlsFailure.OTHER
    match failure:
        case TlsFailure.CLOSED:
            return _closed_message(exchanging)
        case TlsFailure.NOT_TLS:
            return (
                "The server does not speak TLS on this port. Docker's TLS port is normally 2376; "
                "2375 is the unencrypted port."
            )
        case TlsFailure.PROTOCOL_VERSION:
            return (
                "The server does not offer TLS 1.2 or later, which is the minimum this product "
                "uses."
            )
        case TlsFailure.CERTIFICATE_ALERT:
            return (
                "The server rejected the client certificate. Check that the daemon trusts the CA "
                "that issued it and that the certificate has not expired."
            )
        case TlsFailure.HANDSHAKE_REJECTED:
            return (
                "The server rejected the handshake. It may not accept the client certificate, or "
                "it shares no cipher suite with this server."
            )
        case TlsFailure.ALERT:
            return "The server ended the handshake with a TLS alert."
        case _:
            if exchanging:
                return "The TLS connection failed after the handshake."
            return "The TLS handshake failed."


def _certificate_message(code: int) -> str:
    match classify_certificate_fault(code):
        case CertificateFault.UNKNOWN_ISSUER:
            return (
                "The server's certificate was not issued by the supplied CA certificate. Upload "
                "the CA certificate the daemon's certificate was signed with."
            )
        case CertificateFault.EXPIRED:
            return "The server's certificate has expired."
        case CertificateFault.NOT_YET_VALID:
            return (
                "The server's certificate is not valid yet. Check the clocks on the daemon's host "
                "and on this server."
            )
        case CertificateFault.NAME_MISMATCH:
            return (
                "The server's certificate does not cover the host name or IP address used to "
                "reach it. It must list that name or address as a Subject Alternative Name."
            )
        case CertificateFault.WRONG_PURPOSE:
            return "The server's certificate is not valid for server authentication."
        case CertificateFault.WEAK_KEY:
            return "The server's certificate, or its issuer, uses a key or digest that is too weak."
        case _:
            return (
                "The server's certificate could not be verified against the supplied CA "
                "certificate."
            )


class _Overflow:
    exceeded = False


class _CappedStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, limit: int, overflow: _Overflow) -> None:
        self._inner = inner
        self._limit = limit
        self._overflow = overflow

    async def __aiter__(self) -> AsyncIterator[bytes]:
        received = 0
        async for chunk in self._inner:
            received += len(chunk)
            if received > self._limit:
                self._overflow.exceeded = True
                raise httpx.ReadError("response exceeds the probe limit")
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class _CappedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, limit: int, overflow: _Overflow) -> None:
        self._inner = inner
        self._limit = limit
        self._overflow = overflow

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        assert isinstance(response.stream, httpx.AsyncByteStream)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_CappedStream(response.stream, self._limit, self._overflow),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


async def _engine_stage(run: _Run, settings: ConnectionSettings) -> None:
    started = time.monotonic()
    seconds, overall = run.budget(run.timeouts.engine)
    overflow = _Overflow()
    bundle = settings.create_transport()
    capped = TransportBundle(
        _CappedTransport(bundle.transport, MAX_ENGINE_RESPONSE_BYTES, overflow), bundle.base_url
    )
    client = EngineClient(run.environment_id, capped)
    try:
        async with asyncio.timeout(seconds):
            daemon = await client.negotiate()
            run.api_version = clean_text(daemon.daemon_api_version)
            info = await client.system.info()
    except TimeoutError:
        run.timed_out(Stage.ENGINE, seconds, overall, started)
        return
    except VersionUnsupported as exc:
        run.api_version = clean_text(exc.daemon_api_version)
        run.version_unsupported = True
        run.fail(Stage.ENGINE, _unsupported_message(run.api_version), started, exc)
        return
    except EngineError as exc:
        run.fail(Stage.ENGINE, _engine_error_message(exc, overflow.exceeded), started, exc)
        return
    except Exception as exc:
        run.fail(Stage.ENGINE, "The Engine could not be queried.", started, exc)
        return
    finally:
        await client.aclose()

    run.engine = _engine_facts(daemon, info)
    run.add(
        Stage.ENGINE,
        StageOutcome.PASSED,
        "The Docker Engine answered and its API version is supported.",
        started,
        {
            "api_version": run.engine.api_version,
            "negotiated_api_version": run.engine.negotiated_api_version,
        },
    )


def swarm_state_of(value: str | None) -> SwarmStateName | None:
    """Return the Swarm state a daemon reported in its stored form, or None if unrecognised."""
    folded = (value or "").lower()
    return next((known for known in _SWARM_STATES if known == folded), None)


def environment_type_of(
    swarm_state: SwarmStateName | None, control_available: bool | None
) -> EnvironmentTypeName:
    """Return swarm for a manager that can control the cluster, standalone for anything else."""
    return "swarm" if swarm_state == "active" and control_available else "standalone"


def _engine_facts(daemon: DaemonInfo, info: SystemInfo) -> EngineFacts:
    swarm_state = swarm_state_of(info.swarm.local_node_state)
    control = info.swarm.control_available if swarm_state is not None else None
    return EngineFacts(
        engine_version=clean_text(daemon.engine_version or info.server_version),
        api_version=clean_text(daemon.daemon_api_version),
        negotiated_api_version=clean_text(daemon.negotiated_api_version),
        min_api_version=clean_text(daemon.min_api_version) if daemon.min_api_version else None,
        swarm_state=swarm_state,
        swarm_control_available=control,
        environment_type=environment_type_of(swarm_state, control),
        operating_system=clean_text(info.operating_system),
        os_type=clean_text(info.os_type),
        architecture=clean_text(info.architecture),
        host_name=clean_text(info.name),
        cpu_count=max(0, min(info.ncpu, _INT32_MAX)),
        memory_bytes=max(0, min(info.mem_total, _INT64_MAX)),
    )


def _unsupported_message(daemon_api: str) -> str:
    supported = (
        f"{format_api_version(API_VERSION_FLOOR)} to {format_api_version(CLIENT_MAX_API_VERSION)}"
    )
    try:
        below = parse_api_version(daemon_api) < API_VERSION_FLOOR
    except ValueError:
        below = False
    if below:
        return (
            f"The daemon's API version is {daemon_api}; this product supports {supported}. "
            "Upgrade Docker on that host."
        )
    return (
        f"The daemon only accepts API versions newer than the {supported} this product "
        "supports. Use a newer release of this product."
    )


def _engine_error_message(exc: EngineError, oversized: bool) -> str:
    if oversized:
        return "The endpoint sent far more data than a Docker Engine API replies with."
    if isinstance(exc, PermissionDenied):
        return (
            f"The Engine refused the request as unauthorised or forbidden (HTTP {exc.status_code})."
        )
    if isinstance(exc, EnvironmentUnreachable):
        if exc.status_code is not None:
            return f"A gateway in front of the Engine answered HTTP {exc.status_code}."
        return "The connection to the Engine was lost or timed out."
    suffix = f" (HTTP {exc.status_code})" if exc.status_code is not None else ""
    return (
        f"The endpoint answered, but not like a Docker Engine API{suffix}. Check that the "
        "address and port belong to the Docker daemon."
    )
