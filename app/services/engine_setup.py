"""Start-up and shutdown of what the Docker Engine access needs: workdir, clients, refreshes."""

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from app.config import DEFAULT_INSTANCE_NAME
from app.crypto.vault import Vault
from app.db.engine import Database
from app.db.repositories.clocked import Clock
from app.docker_engine.registry import EngineClientRegistry
from app.docker_engine.transport import configure_tls_workdir
from app.services.compose_setup import ComposeRuntime
from app.services.container_import_store import PreparedContainerStore
from app.services.environment_connector import ProbeCoordinator
from app.services.environment_snapshots import (
    EnvironmentSnapshotService,
    SnapshotPokes,
)
from app.services.host_read_stash import HostReadStash
from app.services.image_jobs import ImageJobRegistry
from app.services.imported_volumes import ImportedVolumes
from app.services.object_locks import ObjectLockRegistry, StreamSlots
from app.services.onboarding_store import OnboardingReviewStore
from app.services.stack_import_store import StackImportStore
from app.services.stack_jobs import StackJobRegistry
from app.services.terminals import RestrictedCache, TerminalTickets
from app.services.volume_import_store import VolumeImportStore

STALE_AFTER_INTERVALS = 3


def prepare_engine_workdir(data_dir: Path) -> Path:
    """Create the private directory TLS client identities are written to; call once at startup."""
    return configure_tls_workdir(data_dir)


@dataclass(slots=True)
class EngineRuntime:
    """The process-wide Engine state: clients, connection tests, refreshes and the limits."""

    registry: EngineClientRegistry
    probes: ProbeCoordinator
    refresher: SnapshotPokes
    snapshots: EnvironmentSnapshotService
    vault: Vault
    snapshot_interval: timedelta
    locks: ObjectLockRegistry
    stream_slots: StreamSlots
    terminal_tickets: TerminalTickets
    attach_locks: ObjectLockRegistry
    compose: ComposeRuntime | None = None
    restricted_containers: RestrictedCache = field(default_factory=RestrictedCache)
    image_jobs: ImageJobRegistry = field(default_factory=ImageJobRegistry)
    stack_jobs: StackJobRegistry = field(default_factory=StackJobRegistry)
    host_reads: HostReadStash = field(default_factory=HostReadStash)
    onboarding_reviews: OnboardingReviewStore = field(default_factory=OnboardingReviewStore)
    volume_imports: VolumeImportStore = field(default_factory=VolumeImportStore)
    stack_imports: StackImportStore = field(default_factory=StackImportStore)
    imported_volumes: ImportedVolumes = field(default_factory=ImportedVolumes)
    prepared_containers: PreparedContainerStore = field(default_factory=PreparedContainerStore)
    instance_name: str = DEFAULT_INSTANCE_NAME

    @property
    def stale_after(self) -> timedelta:
        """Return how old a snapshot may be before it is shown as stale."""
        return self.snapshot_interval * STALE_AFTER_INTERVALS

    async def aclose(self) -> None:
        """End the image and stack jobs and snapshot refreshes, then release every client and test.

        Jobs are cancelled first: each records its interruption on the database and talks to its
        host while it ends.
        """
        await self.image_jobs.aclose()
        await self.stack_jobs.aclose()
        await self.refresher.aclose()
        await self.volume_imports.aclose()
        await self.stack_imports.aclose()
        self.prepared_containers.clear()
        await self.probes.aclose()
        await self.registry.aclose()


def build_engine_runtime(
    *,
    database: Database,
    vault: Vault,
    snapshot_interval_seconds: int,
    clock: Clock,
    compose: ComposeRuntime | None = None,
    instance_name: str = DEFAULT_INSTANCE_NAME,
) -> EngineRuntime:
    """Assemble the Engine runtime; snapshot refreshes run only when asked for."""
    registry = EngineClientRegistry()
    probes = ProbeCoordinator()
    service = EnvironmentSnapshotService(
        database=database,
        vault=vault,
        registry=registry,
        probes=probes,
        clock=clock,
        compose_version=(lambda: None) if compose is None else (lambda: compose.version_text),
    )
    return EngineRuntime(
        registry=registry,
        probes=probes,
        refresher=SnapshotPokes(service),
        snapshots=service,
        vault=vault,
        snapshot_interval=timedelta(seconds=snapshot_interval_seconds),
        locks=ObjectLockRegistry(clock),
        stream_slots=StreamSlots(),
        terminal_tickets=TerminalTickets(),
        attach_locks=ObjectLockRegistry(clock),
        compose=compose,
        image_jobs=ImageJobRegistry(clock),
        stack_jobs=StackJobRegistry(clock),
        instance_name=instance_name,
    )
