"""Connection settings for stored environments, the fingerprint that names them, and tests."""

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from app.auth.errors import ProductError
from app.crypto.envelope import DecryptionError
from app.db.repositories.environment import (
    ConnectionKind,
    Environment,
    EnvironmentStatus,
    FailedStage,
    StatusRecord,
)
from app.db.repositories.environment_tls import EnvironmentTlsRepository, StoredTls
from app.docker_engine.client import EngineClient
from app.docker_engine.probe import (
    BoundedResolver,
    EngineFacts,
    ProbeResult,
    ProbeStatus,
    ProbeTimeouts,
    Resolver,
    Stage,
    probe_connection,
)
from app.docker_engine.registry import EngineClientRegistry
from app.docker_engine.tls_validation import clean_text
from app.docker_engine.transport import (
    ConnectionSettings,
    TcpSettings,
    TlsMaterial,
    UnixSocketSettings,
    split_tcp_endpoint,
)
from app.logging import get_logger

logger = get_logger(__name__)

FINGERPRINT_TAG = b"dockmin-env-connection-v1"
MAX_STATUS_MESSAGE = 256

MATERIAL_UNAVAILABLE = (
    "The stored TLS material is missing or cannot be read. "
    "Edit the environment and upload it again."
)


def connection_fingerprint(environment: Environment, tls: StoredTls | None) -> str:
    """Name the connection an environment's row describes.

    Covers the kind, the endpoint, both certificates and the encrypted key, and nothing else, so
    it changes exactly when the connection does.
    """
    return digest_of(
        environment.connection_kind,
        environment.endpoint,
        None if tls is None else tls.ca_certificate,
        None if tls is None else tls.client_certificate,
        None if tls is None else tls.client_key_ciphertext,
    )


def digest_of(
    kind: str,
    endpoint: str,
    ca_certificate: str | None,
    client_certificate: str | None,
    client_key_ciphertext: str | None,
) -> str:
    """Hash length-prefixed parts, so no two different inputs can concatenate to the same bytes."""
    digest = hashlib.sha256()
    for part in (
        FINGERPRINT_TAG.decode("ascii"),
        kind,
        endpoint,
        ca_certificate or "",
        client_certificate or "",
        client_key_ciphertext or "",
    ):
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class StageReport:
    """One stage of a connection test as a page shows it."""

    stage: str
    outcome: str
    duration_ms: int
    message: str
    facts: Mapping[str, str | int]


@dataclass(frozen=True, slots=True)
class ConnectionTestReport:
    """The result of a connection test: the status, the stage that failed, and every stage."""

    status: str
    failed_stage: str | None
    message: str
    duration_ms: int
    stages: tuple[StageReport, ...]
    api_version: str | None
    engine_version: str | None

    @property
    def reachable(self) -> bool:
        """Report whether the test connected."""
        return self.status == "reachable"


class ConnectionTestFailed(ProductError):
    """Raised when a save is refused because the connection test failed; carries the report."""

    def __init__(self, report: ConnectionTestReport) -> None:
        super().__init__(report.message)
        self.report = report


def report_of(result: ProbeResult) -> ConnectionTestReport:
    """Convert a probe result into the plain report pages receive."""
    return ConnectionTestReport(
        status=result.status.value,
        failed_stage=None if result.failed_stage is None else result.failed_stage.value,
        message=result.message,
        duration_ms=result.duration_ms,
        stages=tuple(
            StageReport(
                stage=stage.stage.value,
                outcome=stage.outcome.value,
                duration_ms=stage.duration_ms,
                message=stage.message,
                facts=dict(stage.facts),
            )
            for stage in result.stages
        ),
        api_version=result.api_version,
        engine_version=None if result.engine is None else result.engine.engine_version,
    )


def status_record_of(result: ProbeResult, checked_at: str) -> StatusRecord:
    """Map a probe result to what is written on the environment row."""
    reachable = result.status is ProbeStatus.REACHABLE
    failed_stage: FailedStage | None = None if reachable else _failed_stage(result.failed_stage)
    engine = result.engine
    return StatusRecord(
        status=_STATUSES[result.status],
        failed_stage=failed_stage,
        message=None if reachable else clean_text(result.message, MAX_STATUS_MESSAGE),
        checked_at=checked_at,
        contacted_at=checked_at if reachable else None,
        engine_version=None if engine is None else clean_text(engine.engine_version),
        api_version=_api_version(result, engine),
        type=None if engine is None else engine.environment_type,
        swarm_state=None if engine is None else engine.swarm_state,
        swarm_control_available=None if engine is None else engine.swarm_control_available,
    )


def _api_version(result: ProbeResult, engine: EngineFacts | None) -> str | None:
    version = result.api_version if engine is None else engine.api_version
    return None if version is None else clean_text(version)


_STATUSES: dict[ProbeStatus, EnvironmentStatus] = {
    ProbeStatus.REACHABLE: "reachable",
    ProbeStatus.UNREACHABLE: "unreachable",
    ProbeStatus.VERSION_UNSUPPORTED: "version_unsupported",
}
_STAGES: dict[Stage, FailedStage] = {
    Stage.DNS: "dns",
    Stage.TCP: "tcp",
    Stage.SOCKET: "socket",
    Stage.TLS_HANDSHAKE: "tls_handshake",
    Stage.CERTIFICATE: "certificate",
    Stage.ENGINE: "engine",
}


def _failed_stage(stage: Stage | None) -> FailedStage | None:
    return None if stage is None else _STAGES[stage]


def unavailable_result(stage: Stage, message: str) -> ProbeResult:
    """Build the result for a test that could not start, such as unreadable stored material."""
    return ProbeResult(
        stages=(),
        status=ProbeStatus.UNREACHABLE,
        failed_stage=stage,
        message=message,
        duration_ms=0,
        api_version=None,
        engine=None,
    )


class ProbeCoordinator:
    """Runs connection tests, coalescing concurrent tests of the same connection onto one."""

    def __init__(self, *, resolver: Resolver | None = None) -> None:
        self._owned: BoundedResolver | None
        if resolver is None:
            self._owned = BoundedResolver()
            self._resolver: Resolver = self._owned
        else:
            self._owned = None
            self._resolver = resolver
        self._flights: dict[tuple[int, str], asyncio.Task[ProbeResult]] = {}
        self._closed = False

    async def probe(
        self,
        settings: ConnectionSettings,
        *,
        environment_id: int,
        fingerprint: str | None,
        timeouts: ProbeTimeouts | None = None,
    ) -> ProbeResult:
        """Test the connection; callers passing the same environment and fingerprint share a run.

        A fingerprint of None means the connection belongs to no saved environment, so the test
        is never shared.
        """
        if self._closed:
            raise RuntimeError("the probe coordinator is closed")
        if fingerprint is None:
            return await self._run_probe(settings, environment_id, timeouts)
        key = (environment_id, fingerprint)
        flight = self._flights.get(key)
        if flight is None:
            flight = asyncio.create_task(self._run_probe(settings, environment_id, timeouts))
            self._flights[key] = flight
            flight.add_done_callback(lambda done: self._forget(key, done))
        return await asyncio.shield(flight)

    async def aclose(self) -> None:
        """Cancel tests still running; call once at shutdown."""
        self._closed = True
        flights = list(self._flights.values())
        for flight in flights:
            flight.cancel()
        await asyncio.gather(*flights, return_exceptions=True)
        self._flights.clear()
        if self._owned is not None:
            self._owned.close()

    def _forget(self, key: tuple[int, str], done: asyncio.Task[ProbeResult]) -> None:
        if self._flights.get(key) is done:
            del self._flights[key]

    async def _run_probe(
        self, settings: ConnectionSettings, environment_id: int, timeouts: ProbeTimeouts | None
    ) -> ProbeResult:
        try:
            return await probe_connection(
                settings,
                environment_id=environment_id,
                timeouts=timeouts,
                resolver=self._resolver,
            )
        except Exception as error:
            logger.error(
                "connection_test_crashed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            return unavailable_result(Stage.ENGINE, "The connection test could not be completed.")


class MaterialUnavailable(Exception):
    """Raised when a stored environment's connection settings cannot be built."""


class EnvironmentConnector:
    """Turns stored environments into settings, clients and connection tests."""

    def __init__(
        self,
        *,
        tls: EnvironmentTlsRepository,
        registry: EngineClientRegistry,
        probes: ProbeCoordinator,
    ) -> None:
        self._tls = tls
        self._registry = registry
        self._probes = probes

    async def settings_for(
        self, environment: Environment, stored: StoredTls | None
    ) -> ConnectionSettings:
        """Build settings from a stored row, decrypting the client key if it has one.

        Raises MaterialUnavailable when a remote environment's material is incomplete or its key
        cannot be opened. Settings are single-use: building the context blanks the PEM text.
        """
        if environment.connection_kind == "unix_socket":
            return UnixSocketSettings(environment.endpoint)
        material = await self.material_for(environment.id, stored)
        host, port = split_tcp_endpoint(environment.endpoint)
        return TcpSettings(host, port, material)

    async def material_for(self, environment_id: int, stored: StoredTls | None) -> TlsMaterial:
        """Return the stored TLS material with its key decrypted, to test a changed endpoint.

        Raises MaterialUnavailable when it is incomplete or its key cannot be opened.
        """
        if (
            stored is None
            or not stored.ca_certificate
            or not stored.client_certificate
            or not stored.client_key_ciphertext
        ):
            raise MaterialUnavailable
        try:
            key = await self._tls.reveal_client_key(environment_id)
        except DecryptionError:
            logger.error("tls_key_unreadable", extra={"environment_id": environment_id})
            raise MaterialUnavailable from None
        if not key:
            raise MaterialUnavailable
        return TlsMaterial(stored.ca_certificate, stored.client_certificate, key)

    async def client(self, environment: Environment, stored: StoredTls | None) -> EngineClient:
        """Return the registry's client for the row; the key is decrypted only on a miss."""
        return await self._registry.client_for(
            environment.id,
            connection_fingerprint(environment, stored),
            lambda: self.settings_for(environment, stored),
        )

    async def test_stored(
        self,
        environment: Environment,
        stored: StoredTls | None,
        *,
        timeouts: ProbeTimeouts | None = None,
    ) -> ProbeResult:
        """Test a saved environment's connection as it is stored."""
        try:
            settings = await self.settings_for(environment, stored)
        except MaterialUnavailable:
            return unavailable_result(Stage.TLS_HANDSHAKE, MATERIAL_UNAVAILABLE)
        return await self._probes.probe(
            settings,
            environment_id=environment.id,
            fingerprint=connection_fingerprint(environment, stored),
            timeouts=timeouts,
        )

    async def test_proposed(
        self,
        kind: ConnectionKind,
        endpoint: str,
        material: TlsMaterial | None,
        *,
        environment_id: int = 0,
    ) -> ProbeResult:
        """Test a connection that is not saved, never sharing the run with another test."""
        settings: ConnectionSettings
        if kind == "unix_socket":
            settings = UnixSocketSettings(endpoint)
        else:
            host, port = split_tcp_endpoint(endpoint)
            settings = TcpSettings(host, port, material)
        return await self._probes.probe(settings, environment_id=environment_id, fingerprint=None)
