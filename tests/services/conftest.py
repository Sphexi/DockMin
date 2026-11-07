"""Fixtures for the authorisation model: a seeded database, a scripted inspector, real services."""

import asyncio
import shutil
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.docker_engine.transport as transport
from app.auth.passwords import PasswordHasher
from app.auth.services import AuthRuntime
from app.crypto.vault import Vault
from app.db.engine import Database, Session
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.environment_tls import EnvironmentTlsRepository
from app.db.repositories.resource_control import ResourceControlRepository
from app.docker_engine.registry import EngineClientRegistry
from app.docker_engine.transport import ConnectionSettings, TransportBundle, configure_tls_workdir
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    AccessServices,
    ContainerServices,
    EnvironmentServices,
    ImageServices,
    NetworkServices,
    SwarmServices,
    VolumeServices,
    build_access_services,
    build_container_services,
    build_environment_services,
    build_host_clients,
    build_image_services,
    build_network_services,
    build_swarm_services,
    build_volume_services,
)
from app.services.image_jobs import ImageJobRegistry
from app.services.object_inspector import EngineObjectInspector
from app.services.object_locks import ObjectLockRegistry, StreamSlots
from tests import stack_support
from tests.access_support import (
    ENV_A,
    ENV_B,
    LEADER_ID,
    MEMBER_ID,
    PEER_ID,
    FakeInspector,
    seed_world,
)
from tests.auth_support import STANDARD_ID, FakeClock, provision
from tests.crypto.helpers import FIRST_KEY, vault_with
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.fake_engine import FakeEngine
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.docker_engine.tls_material import valid_material
from tests.environment_support import FakeEngineRuntime, RecordingPoker, ScriptedProbes
from tests.registry_support import RegistryRig, build_rig
from tests.support import CHEAP_ARGON2
from tests.swarm_stack_support import DefaultingDaemon, StackDaemon, build_swarm_rig


@pytest.fixture(scope="session")
def access_template(migrated_template: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build once a migrated database holding five users and two environments, no sessions."""
    path = tmp_path_factory.mktemp("access") / "dockmin.db"
    provision(migrated_template, path, with_sessions=False)
    asyncio.run(seed_world(path))
    return path


@pytest.fixture
async def db(access_template: Path, tmp_path: Path) -> AsyncIterator[Session]:
    """Open a session on a private copy of the seeded database."""
    path = tmp_path / "dockmin.db"
    shutil.copyfile(access_template, path)
    async with Database(path).session() as opened:
        yield opened


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def inspector() -> FakeInspector:
    return FakeInspector()


@pytest.fixture
def runtime(clock: FakeClock) -> AuthRuntime:
    return AuthRuntime(hasher=PasswordHasher(CHEAP_ARGON2), clock=clock)


@pytest.fixture
def access(db: Session, runtime: AuthRuntime, inspector: FakeInspector) -> AccessServices:
    """Wire the authorisation services as one request would."""
    return build_access_services(db, runtime, inspector, ActivityRecorder(db, runtime.clock))


@pytest.fixture
def new_request(
    db: Session, runtime: AuthRuntime, inspector: FakeInspector
) -> Callable[[], AccessServices]:
    """Return a factory for a fresh set of services, which remembers nothing of earlier requests."""
    return lambda: build_access_services(
        db, runtime, inspector, ActivityRecorder(db, runtime.clock)
    )


@pytest.fixture
def controls(db: Session, clock: FakeClock) -> ResourceControlRepository:
    """Reach the repository directly, to create rows the service would refuse to write."""
    return ResourceControlRepository(db, clock)


@pytest.fixture
def tls_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the TLS work directory under tmp_path for a test that validates or uses TLS material."""
    monkeypatch.setattr(transport, "_tls_workdir_base", None)
    return configure_tls_workdir(tmp_path)


@pytest.fixture
def probes() -> ScriptedProbes:
    """Connection tests that answer with a reachable Engine until a test scripts otherwise."""
    return ScriptedProbes()


@pytest.fixture
def vault() -> Vault:
    return vault_with(FIRST_KEY)


@pytest.fixture
def poker() -> RecordingPoker:
    return RecordingPoker()


@pytest.fixture
def clients_built() -> list[ConnectionSettings]:
    """The settings of every Engine client the registry has been asked to build."""
    return []


@pytest.fixture
async def client_registry(
    clients_built: list[ConnectionSettings],
) -> AsyncIterator[EngineClientRegistry]:
    """A client registry whose every client talks to a fake Engine."""

    def build(settings: ConnectionSettings) -> TransportBundle:
        clients_built.append(settings)
        return FakeEngine().bundle()

    created = EngineClientRegistry(build)
    yield created
    await created.aclose()


@pytest.fixture
def environments(
    db: Session,
    runtime: AuthRuntime,
    access: AccessServices,
    probes: ScriptedProbes,
    poker: RecordingPoker,
    client_registry: EngineClientRegistry,
    vault: Vault,
    tls_workdir: Path,
) -> EnvironmentServices:
    """Wire the environment services as one request would, over scripted connection tests."""
    engine = FakeEngineRuntime(
        registry=client_registry,
        probes=probes,
        refresher=poker,
        vault=vault,
    )
    return build_environment_services(
        db,
        runtime,
        engine.as_runtime(),
        access.authoriser,
        ActivityRecorder(db, runtime.clock),
    )


@pytest.fixture
async def no_grants(db: Session) -> None:
    """Remove the seeded users' access to both seeded environments."""
    grants = EnvironmentAccessRepository(db)
    for environment_id in (ENV_A, ENV_B):
        for user_id in (STANDARD_ID, MEMBER_ID, PEER_ID, LEADER_ID):
            await grants.remove_user("environment", environment_id, user_id)


@dataclass(slots=True)
class ContainerRig:
    """The container services of one request over a fake daemon, with what a test reaches into."""

    services: ContainerServices
    access: AccessServices
    inspector: EngineObjectInspector
    engine: FakeEngineRuntime
    host: ContainerEngine
    database: Database
    db: Session
    registry: EngineClientRegistry
    runtime: AuthRuntime
    recorder: ActivityRecorder

    @property
    def locks(self) -> ObjectLockRegistry:
        return self.engine.locks

    @property
    def slots(self) -> StreamSlots:
        return self.engine.stream_slots


@pytest.fixture
def host(fake_hosts: dict[str, str]) -> Iterator[ContainerEngine]:
    """An empty daemon; a test adds the containers it needs."""
    daemon = ContainerEngine()
    yield daemon
    daemon.release_gates()


@pytest.fixture
async def rig(
    db: Session,
    tmp_path: Path,
    runtime: AuthRuntime,
    clock: FakeClock,
    vault: Vault,
    host: ContainerEngine,
    tls_workdir: Path,
) -> AsyncIterator[ContainerRig]:
    """Wire the container services as one request would, with every environment on `host`."""
    registry = EngineClientRegistry(lambda settings: host.bundle())
    material = valid_material()
    await EnvironmentTlsRepository(db, vault, clock).replace(
        ENV_A,
        ca_certificate=material.ca,
        client_certificate=material.cert,
        client_key=material.key,
    )
    engine = FakeEngineRuntime(
        registry=registry,
        probes=ScriptedProbes(),
        refresher=RecordingPoker(),
        vault=vault,
        locks=ObjectLockRegistry(clock),
        stream_slots=StreamSlots(),
    )
    recorder = ActivityRecorder(db, clock)
    inspector = EngineObjectInspector(build_host_clients(db, runtime, engine.as_runtime()))
    access = build_access_services(db, runtime, inspector, recorder)
    database = Database(tmp_path / "dockmin.db")
    services = build_container_services(
        db, database, runtime, engine.as_runtime(), access, inspector, recorder
    )
    yield ContainerRig(
        services, access, inspector, engine, host, database, db, registry, runtime, recorder
    )
    await registry.aclose()


@pytest.fixture
def nets(rig: ContainerRig) -> NetworkServices:
    return build_network_services(
        rig.db,
        rig.database,
        rig.runtime,
        rig.engine.as_runtime(),
        rig.access,
        rig.inspector,
        rig.recorder,
    )


@pytest.fixture
def vols(rig: ContainerRig) -> VolumeServices:
    return build_volume_services(
        rig.db,
        rig.database,
        rig.runtime,
        rig.engine.as_runtime(),
        rig.access,
        rig.inspector,
        rig.recorder,
    )


@pytest.fixture
async def registry_rig(
    access_template: Path, tmp_path: Path, runtime: AuthRuntime, clock: FakeClock, vault: Vault
) -> AsyncIterator[RegistryRig]:
    """The registry services over a private copy of the seeded database and scripted fakes."""
    path = tmp_path / "dockmin.db"
    shutil.copyfile(access_template, path)
    database = Database(path)
    async with database.session() as db:
        rig = build_rig(database=database, db=db, runtime=runtime, clock=clock, vault=vault)
        yield rig
        await rig.http.aclose()


@dataclass(slots=True)
class ImageRig:
    """Image, container and registry services of one request over a fake daemon and a database."""

    images: ImageServices
    containers: ContainerServices
    registry: RegistryRig
    access: AccessServices
    engine: FakeEngineRuntime
    host: ContainerEngine
    database: Database
    db: Session
    clock: FakeClock
    runtime: AuthRuntime
    daemon_registry: EngineClientRegistry

    @property
    def app(self) -> Any:
        """The parts of the application state the background jobs read."""
        return SimpleNamespace(
            state=SimpleNamespace(
                engine=self.engine,
                registries=self.registry.registries,
                database=self.database,
                auth=self.runtime,
            )
        )


@pytest.fixture
async def image_rig(
    access_template: Path,
    tmp_path: Path,
    runtime: AuthRuntime,
    clock: FakeClock,
    vault: Vault,
    host: ContainerEngine,
    tls_workdir: Path,
) -> AsyncIterator[ImageRig]:
    """Wire the image, container and registry services over one database and one fake daemon."""
    path = tmp_path / "dockmin.db"
    shutil.copyfile(access_template, path)
    database = Database(path)
    daemons = EngineClientRegistry(lambda settings: host.bundle())
    async with database.session() as db:
        material = valid_material()
        await EnvironmentTlsRepository(db, vault, clock).replace(
            ENV_A,
            ca_certificate=material.ca,
            client_certificate=material.cert,
            client_key=material.key,
        )
        registry = build_rig(database=database, db=db, runtime=runtime, clock=clock, vault=vault)
        engine = FakeEngineRuntime(
            registry=daemons,
            probes=ScriptedProbes(),
            refresher=RecordingPoker(),
            vault=vault,
            locks=ObjectLockRegistry(clock),
            stream_slots=StreamSlots(),
            image_jobs=ImageJobRegistry(clock),
        )
        recorder = ActivityRecorder(db, clock)
        inspector = EngineObjectInspector(build_host_clients(db, runtime, engine.as_runtime()))
        access = build_access_services(db, runtime, inspector, recorder)
        images = build_image_services(
            db,
            database,
            runtime,
            engine.as_runtime(),
            registry.registries,
            access,
            inspector,
            recorder,
        )
        containers = build_container_services(
            db,
            database,
            runtime,
            engine,  # type: ignore[arg-type]
            access,
            inspector,
            recorder,
            registries=registry.registries,
        )
        yield ImageRig(
            images=images,
            containers=containers,
            registry=registry,
            access=access,
            engine=engine,
            host=host,
            database=database,
            db=db,
            clock=clock,
            runtime=runtime,
            daemon_registry=daemons,
        )
        await engine.image_jobs.aclose()
        await registry.http.aclose()
    await daemons.aclose()


@pytest.fixture
async def stack_rig(
    access_template: Path,
    tmp_path: Path,
    runtime: AuthRuntime,
    clock: FakeClock,
    vault: Vault,
    host: ContainerEngine,
    tls_workdir: Path,
) -> AsyncIterator[stack_support.Rig]:
    """The stack services over a fake daemon holding `nginx:1`, run by the scripted Compose."""
    host.add_image("nginx:1")
    async with stack_support.build(access_template, tmp_path, runtime, clock, vault, host) as rig:
        yield rig


@dataclass(slots=True)
class SwarmRig:
    """The Swarm services of one request over a fake manager, real credentials and bindings."""

    sw: SwarmServices
    host: SwarmEngine
    access: AccessServices
    inspector: EngineObjectInspector
    engine: FakeEngineRuntime
    registry: RegistryRig
    database: Database
    db: Session
    clock: FakeClock
    runtime: AuthRuntime
    recorder: ActivityRecorder
    poker: RecordingPoker

    def fresh(self) -> SwarmServices:
        """Rewire the services as the next request would, remembering nothing of the last."""
        self.inspector, self.access, self.sw = _swarm_services(
            self.db,
            self.database,
            self.runtime,
            self.engine,
            self.registry,
            self.recorder,
            self.poker,
        )
        return self.sw


def _swarm_services(
    db: Session,
    database: Database,
    runtime: AuthRuntime,
    engine: FakeEngineRuntime,
    registry: RegistryRig,
    recorder: ActivityRecorder,
    poker: RecordingPoker,
) -> tuple[EngineObjectInspector, AccessServices, SwarmServices]:
    inspector = EngineObjectInspector(
        build_host_clients(db, runtime, engine.as_runtime()), poke=poker.poke
    )
    access = build_access_services(db, runtime, inspector, recorder)
    services = build_swarm_services(
        db, database, runtime, engine.as_runtime(), registry.registries, access, inspector, recorder
    )
    return inspector, access, services


@pytest.fixture
def swarm_host(fake_hosts: dict[str, str]) -> Iterator[SwarmEngine]:
    """A manager with a manager node, a worker node and nothing deployed."""
    daemon = SwarmEngine()
    yield daemon
    daemon.release_gates()


@pytest.fixture
async def swarm(
    access_template: Path,
    tmp_path: Path,
    runtime: AuthRuntime,
    clock: FakeClock,
    vault: Vault,
    swarm_host: SwarmEngine,
    tls_workdir: Path,
) -> AsyncIterator[SwarmRig]:
    """Wire the Swarm services with both seeded environments typed as managers on one daemon."""
    path = tmp_path / "dockmin.db"
    shutil.copyfile(access_template, path)
    database = Database(path)
    daemons = EngineClientRegistry(lambda settings: swarm_host.bundle())
    async with database.session() as db:
        material = valid_material()
        await EnvironmentTlsRepository(db, vault, clock).replace(
            ENV_A,
            ca_certificate=material.ca,
            client_certificate=material.cert,
            client_key=material.key,
        )
        await db.execute("UPDATE environment SET type = 'swarm' WHERE id IN (?, ?)", (ENV_A, ENV_B))
        poker = RecordingPoker()
        engine = FakeEngineRuntime(
            registry=daemons,
            probes=ScriptedProbes(),
            refresher=poker,
            vault=vault,
            locks=ObjectLockRegistry(clock),
            stream_slots=StreamSlots(),
        )
        recorder = ActivityRecorder(db, clock)
        registry = build_rig(database=database, db=db, runtime=runtime, clock=clock, vault=vault)
        inspector, access, services = _swarm_services(
            db, database, runtime, engine, registry, recorder, poker
        )
        rig = SwarmRig(
            sw=services,
            host=swarm_host,
            access=access,
            inspector=inspector,
            engine=engine,
            registry=registry,
            database=database,
            db=db,
            clock=clock,
            runtime=runtime,
            recorder=recorder,
            poker=poker,
        )
        yield rig
        await rig.registry.http.aclose()
    await daemons.aclose()


@pytest.fixture
def stack_daemon(fake_hosts: dict[str, str]) -> Iterator[StackDaemon]:
    """A manager with a manager node, a worker node and the refusals the stack scenarios script."""
    daemon = StackDaemon()
    yield daemon
    daemon.release_gates()


@pytest.fixture
async def swarm_stack_rig(
    access_template: Path,
    tmp_path: Path,
    runtime: AuthRuntime,
    clock: FakeClock,
    vault: Vault,
    stack_daemon: StackDaemon,
    tls_workdir: Path,
) -> AsyncIterator[stack_support.Rig]:
    """The stack services over `stack_daemon`, both seeded environments typed as managers."""
    async with build_swarm_rig(
        access_template, tmp_path, runtime, clock, vault, stack_daemon
    ) as rig:
        yield rig


@pytest.fixture
async def compose_on_swarm_rig(
    access_template: Path,
    tmp_path: Path,
    runtime: AuthRuntime,
    clock: FakeClock,
    vault: Vault,
    stack_daemon: StackDaemon,
    tls_workdir: Path,
) -> AsyncIterator[stack_support.Rig]:
    """The Compose stack services, run by the scripted Compose, on a manager holding `nginx:1`."""
    stack_daemon.add_image("nginx:1")
    async with stack_support.build(
        access_template, tmp_path, runtime, clock, vault, stack_daemon
    ) as rig:
        await rig.db.execute("UPDATE environment SET type = 'swarm' WHERE id = ?", (ENV_A,))
        yield rig


@pytest.fixture
def defaulting_daemon(fake_hosts: dict[str, str]) -> Iterator[DefaultingDaemon]:
    """A manager that fills in the spec defaults the real Engine stores."""
    daemon = DefaultingDaemon()
    yield daemon
    daemon.release_gates()


@pytest.fixture
async def defaulting_stack_rig(
    access_template: Path,
    tmp_path: Path,
    runtime: AuthRuntime,
    clock: FakeClock,
    vault: Vault,
    defaulting_daemon: DefaultingDaemon,
    tls_workdir: Path,
) -> AsyncIterator[stack_support.Rig]:
    """The Swarm stack services over a manager that answers with the Engine's defaults."""
    async with build_swarm_rig(
        access_template, tmp_path, runtime, clock, vault, defaulting_daemon
    ) as rig:
        yield rig
