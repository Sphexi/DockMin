"""Scripted connection tests, a recording refresh trigger and seed helpers for environment tests."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.clocked import Clock
from app.db.repositories.environment import (
    ConnectionKind,
    Environment,
    EnvironmentRepository,
    EnvironmentStatus,
    StatusRecord,
)
from app.docker_engine.probe import (
    EngineFacts,
    ProbeResult,
    ProbeStatus,
    ProbeTimeouts,
    Stage,
    StageOutcome,
    StageResult,
)
from app.docker_engine.registry import EngineClientRegistry
from app.docker_engine.transport import ConnectionSettings
from app.services.bulk_binding_store import BulkBindingReviewStore
from app.services.engine_setup import EngineRuntime
from app.services.environment_connector import ProbeCoordinator
from app.services.environments import ConnectionInput, EnvironmentSpec, TlsInput
from app.services.image_jobs import ImageJobRegistry
from app.services.imported_volumes import ImportedVolumes
from app.services.object_locks import ObjectLockRegistry, StreamSlots
from app.services.stack_jobs import StackJobRegistry
from app.services.terminals import RestrictedCache, TerminalTickets
from tests.access_support import STAMP
from tests.docker_engine.tls_material import Material

ENGINE_VERSION = "29.2.1"
API_VERSION = "1.51"
LOCAL_PATH = "/run/local-docker.sock"
LOCAL_ENDPOINT = f"unix://{LOCAL_PATH}"
REMOTE_HOST = "docker.example.test"
REMOTE_ENDPOINT = f"tcp://{REMOTE_HOST}:2376"
SECOND_REMOTE_ENDPOINT = "tcp://other.example.test:2376"
STALE_AFTER = timedelta(minutes=3)
NEW_NAME = "edge"

FACTS = EngineFacts(
    engine_version=ENGINE_VERSION,
    api_version=API_VERSION,
    negotiated_api_version=API_VERSION,
    min_api_version="1.44",
    swarm_state="inactive",
    swarm_control_available=False,
    environment_type="standalone",
    operating_system="Test OS",
    os_type="linux",
    architecture="x86_64",
    host_name="test-host",
    cpu_count=4,
    memory_bytes=8 * 1024**3,
)


def reachable_result() -> ProbeResult:
    """A connection test that reached an ordinary standalone Engine."""
    return ProbeResult(
        stages=(StageResult(Stage.ENGINE, StageOutcome.PASSED, 1, "The Docker Engine answered."),),
        status=ProbeStatus.REACHABLE,
        failed_stage=None,
        message=f"Connected to Docker Engine {ENGINE_VERSION} (API {API_VERSION}).",
        duration_ms=1,
        api_version=API_VERSION,
        engine=FACTS,
    )


def failed_result(
    stage: Stage,
    message: str = "The connection was refused.",
    status: ProbeStatus = ProbeStatus.UNREACHABLE,
) -> ProbeResult:
    """A connection test that failed at the given stage."""
    return ProbeResult(
        stages=(StageResult(stage, StageOutcome.FAILED, 1, message),),
        status=status,
        failed_stage=stage,
        message=message,
        duration_ms=1,
        api_version=None,
        engine=None,
    )


@dataclass(frozen=True, slots=True)
class ProbeCall:
    """One connection test the service asked for."""

    settings: ConnectionSettings
    environment_id: int
    fingerprint: str | None


@dataclass(slots=True)
class ScriptedProbes:
    """Stands in for the connection-test coordinator and answers with a fixed result."""

    result: ProbeResult = field(default_factory=reachable_result)
    calls: list[ProbeCall] = field(default_factory=list)
    before: Callable[[], Awaitable[None]] | None = None

    async def probe(
        self,
        settings: ConnectionSettings,
        *,
        environment_id: int,
        fingerprint: str | None,
        timeouts: ProbeTimeouts | None = None,
    ) -> ProbeResult:
        """Record the request, run the hook if one is set, and return the scripted result."""
        self.calls.append(ProbeCall(settings, environment_id, fingerprint))
        if self.before is not None:
            await self.before()
        return self.result

    async def aclose(self) -> None:
        """Nothing is held."""

    def as_coordinator(self) -> ProbeCoordinator:
        return cast(ProbeCoordinator, self)


@dataclass(slots=True)
class RecordingPoker:
    """Stands in for the snapshot pokes and notes which environments were asked for."""

    poked: list[int] = field(default_factory=list)

    def poke(self, environment_id: int) -> None:
        """Note the request."""
        self.poked.append(environment_id)

    async def aclose(self) -> None:
        """Nothing is running."""


@dataclass(slots=True)
class FakeEngineRuntime:
    """The parts of the process-wide Engine state the request-scoped services read."""

    registry: EngineClientRegistry
    probes: Any
    refresher: Any
    vault: Vault
    stale_after: timedelta = STALE_AFTER
    locks: ObjectLockRegistry = field(default_factory=ObjectLockRegistry)
    stream_slots: StreamSlots = field(default_factory=StreamSlots)
    terminal_tickets: TerminalTickets = field(default_factory=TerminalTickets)
    attach_locks: ObjectLockRegistry = field(default_factory=ObjectLockRegistry)
    restricted_containers: RestrictedCache = field(default_factory=RestrictedCache)
    image_jobs: ImageJobRegistry = field(default_factory=ImageJobRegistry)
    stack_jobs: StackJobRegistry = field(default_factory=StackJobRegistry)
    imported_volumes: ImportedVolumes = field(default_factory=ImportedVolumes)
    bulk_binding_reviews: BulkBindingReviewStore = field(default_factory=BulkBindingReviewStore)
    compose: Any = None
    instance_name: str = "dockmin"

    def as_runtime(self) -> EngineRuntime:
        return cast(EngineRuntime, self)


async def seed_environment(
    db: Session,
    clock: Clock,
    name: str,
    *,
    kind: ConnectionKind = "unix_socket",
    endpoint: str = LOCAL_PATH,
    status: EnvironmentStatus = "reachable",
) -> Environment:
    """Insert an environment directly, as a finished test would have left it."""
    record = StatusRecord(
        status=status,
        failed_stage=None,
        message=None,
        checked_at=STAMP,
        contacted_at=STAMP if status == "reachable" else None,
        engine_version=ENGINE_VERSION,
        api_version=API_VERSION,
        type="standalone",
        swarm_state="inactive",
        swarm_control_available=False,
    )
    return await EnvironmentRepository(db, clock).create(
        name=name,
        connection_kind=kind,
        socket_path=endpoint if kind == "unix_socket" else None,
        url=endpoint if kind == "tcp_tls" else None,
        record=record,
    )


async def activity_rows(db: Session) -> list[dict[str, Any]]:
    """Return every recorded event, oldest first, with detail decoded."""
    rows = await db.fetch_limited(
        "SELECT action, result, actor_type, actor_user_id, target_type, target_id, "
        "environment_id, detail FROM activity_event ORDER BY id",
        (),
        limit=1000,
    )
    return [{**dict(row), "detail": json.loads(row["detail"] or "{}")} for row in rows]


async def actions_recorded(db: Session) -> list[tuple[str, str]]:
    """Return (action, result) of every recorded event, oldest first."""
    return [(row["action"], row["result"]) for row in await activity_rows(db)]


def local_spec(name: str = NEW_NAME, **overrides: object) -> EnvironmentSpec:
    fields: dict[str, Any] = {
        "name": name,
        "connection": ConnectionInput(kind="unix_socket", socket_path=LOCAL_PATH),
        "reviewed_endpoint": LOCAL_ENDPOINT,
    }
    return EnvironmentSpec(**(fields | overrides))


def remote_spec(
    material: Material | None, name: str = NEW_NAME, host: str = REMOTE_HOST, **overrides: object
) -> EnvironmentSpec:
    tls = TlsInput() if material is None else TlsInput(material.ca, material.cert, material.key)
    fields: dict[str, Any] = {
        "name": name,
        "connection": ConnectionInput(kind="tcp_tls", host=host, port=2376),
        "tls": tls,
        "reviewed_endpoint": f"tcp://{host}:2376",
    }
    return EnvironmentSpec(**(fields | overrides))
