"""A running instance with stubbed runtime hooks, and an archive of another instance to restore."""

import asyncio
import base64
import hashlib
import json
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from app.auth.principal import Principal
from app.backup import paths, producer
from app.config import ENV_PREFIX, SECRET_KEY_VAR, TLS_SELF_SIGNED_VAR, Settings, load_settings
from app.crypto.keys import MasterKeys
from app.crypto.registry import SECRET_FIELDS
from app.crypto.startup import establish_vault
from app.crypto.vault import Vault
from app.db.engine import Database, connect
from app.logging import Scrubber
from app.main import create_app
from app.services.activity import ActivityRecorder
from app.services.backups import BackupProduction, BackupService
from app.services.restore import RestoreCoordinator, RestoreProgress, RestoreRuntime
from app.services.restore_gate import RestoreGate
from tests.backup.support import World, make_world
from tests.conftest import COMPOSE_STUB, serving_app
from tests.crypto.helpers import SECOND_KEY, CredentialRepository, seed_row
from tests.scheduler.support import Clock
from tests.support import CHEAP_ARGON2_ENV, VALID_KEY

ADMINISTRATOR = Principal(user_id=1, username="admin-user", role="administrator", session_id=1)
OPERATOR = Principal(user_id=1, username="alice", role="administrator", session_id=1)
OTHER_SESSION = Principal(user_id=1, username="alice", role="administrator", session_id=77)
STANDARD = Principal(user_id=2, username="bob", role="standard", session_id=2)
LIVE_KEYS = (bytes([5]) * 32, bytes([6]) * 32)
LIVE_ENVIRONMENT = "live-only"
LIVE_STACK = "b" * 32
CHUNK = 65536
SETTLE_POLLS = 1200
SETTLE_PAUSE = 0.01
IDLE_SECONDS = 900.0


async def feed(path: Path, size: int = CHUNK) -> AsyncIterator[bytes]:
    data = await asyncio.to_thread(path.read_bytes)
    for start in range(0, len(data), size):
        yield data[start : start + size]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Jobs:
    def __init__(self) -> None:
        self.count = 0

    def running_count(self) -> int:
        return self.count


class Suspension:
    """Records how often the scheduler was suspended and released."""

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    def suspend(self) -> "Suspension.Context":
        return Suspension.Context(self)

    class Context:
        def __init__(self, owner: "Suspension") -> None:
            self.owner = owner

        async def __aenter__(self) -> None:
            self.owner.entered += 1

        async def __aexit__(self, *exc: object) -> None:
            self.owner.exited += 1


@dataclass(slots=True)
class Instance:
    """The live side of a restore: a database and vault that stop and start on request."""

    world: World
    gate: RestoreGate
    production: BackupProduction
    suspension: Suspension
    images: Jobs
    stacks: Jobs
    clock: Clock
    slept: list[float] = field(default_factory=list)
    waiting: list[asyncio.Future[None]] = field(default_factory=list)
    starts: int = 0
    failing_starts: int = 0
    failing_stops: int = 0
    stops: int = 0
    coordinator: RestoreCoordinator | None = None

    @property
    def data_dir(self) -> Path:
        return self.world.data_dir

    @property
    def database(self) -> Database:
        return self.world.database

    def runtime(self) -> RestoreRuntime:
        return RestoreRuntime(
            self.world.database,
            self.world.vault,
            self.suspension,  # type: ignore[arg-type]
            self.images,  # type: ignore[arg-type]
            self.stacks,  # type: ignore[arg-type]
            LIVE_KEYS[1],
        )

    async def stop(self) -> None:
        self.stops += 1
        if self.stops <= self.failing_stops:
            raise RuntimeError("the runtime would not stop")
        await self.world.database.aclose()

    async def start(self) -> None:
        self.starts += 1
        if self.starts <= self.failing_starts:
            raise RuntimeError("the restored database would not start")
        database = Database(self.world.data_dir / "dockmin.db")
        vault: Vault = await establish_vault(database, MasterKeys(*reversed(LIVE_KEYS)), Scrubber())
        self.world.database, self.world.vault = database, vault

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiting.append(future)
        await future

    def live_rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        connection = self.world.database.connect()
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def actions(self, like: str) -> list[Any]:
        return self.live_rows(
            "SELECT action, result, actor_user_id, detail FROM activity_event "
            "WHERE action LIKE ? ORDER BY id",
            (like,),
        )

    def reason_of(self, row: Any) -> str:
        return str(json.loads(row["detail"])["reason"])

    async def close(self) -> None:
        if self.coordinator is not None:
            await self.coordinator.aclose()
        await self.world.database.aclose()


async def make_instance(base: Path, *, clock: Clock | None = None) -> Instance:
    """Build a live instance with an environment and a stack the archive does not hold."""
    world = await make_world(base, keys=LIVE_KEYS)
    world.run(
        "INSERT INTO environment (name, type, connection_kind, socket_path, created_at, "
        "updated_at) VALUES (?, 'standalone', 'unix_socket', '/live.sock', "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        (LIVE_ENVIRONMENT,),
    )
    stacks = world.data_dir / "stacks" / LIVE_STACK
    stacks.mkdir(parents=True)
    (stacks / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    instance = Instance(
        world=world,
        gate=RestoreGate(),
        production=BackupProduction(world.data_dir),
        suspension=Suspension(),
        images=Jobs(),
        stacks=Jobs(),
        clock=clock or Clock(),
    )
    instance.coordinator = RestoreCoordinator(
        gate=instance.gate,
        production=instance.production,
        data_dir=world.data_dir,
        runtime=instance.runtime,
        stop_runtime=instance.stop,
        start_runtime=instance.start,
        clock=instance.clock,
        sleep=instance.sleep,
    )
    return instance


async def source_archive(base: Path) -> tuple[Path, World]:
    """Produce an archive of an instance made with the second test key, and return it."""
    world = await make_world(base / "source")
    work = paths.new_work_directory(world.data_dir)
    produced = await producer.produce_archive(world.database, world.vault, work, clock=Clock())
    target = base / "source-archive.tar.gz"
    target.write_bytes(produced.path.read_bytes())
    return target, world


def key_for_archive() -> SecretStr:
    return SecretStr(base64.b64encode(SECOND_KEY).decode())


async def confirm_arguments(instance: Instance, *, session: int = 1) -> dict[str, Any]:
    assert instance.coordinator is not None
    review = await instance.coordinator.review(OPERATOR, session)
    return {
        "key_text": key_for_archive(),
        "archive_sha256": review.archive_sha256,
        "replace_confirmed": True,
    }


async def settle(
    instance: Instance, restore_id: str, session_id: int | None = 1
) -> RestoreProgress:
    assert instance.coordinator is not None
    for _ in range(SETTLE_POLLS):
        progress = instance.coordinator.progress(restore_id, session_id=session_id)
        if progress is not None and progress.outcome != "running":
            return progress
        await asyncio.sleep(SETTLE_PAUSE)
    raise TimeoutError("the restore did not settle")


async def begin_and_upload(instance: Instance, archive: Path) -> str:
    assert instance.coordinator is not None
    restore_id = await instance.coordinator.begin(OPERATOR, session_id=1)
    await instance.coordinator.receive_archive(OPERATOR, 1, feed(archive))
    return restore_id


async def restore_fully(instance: Instance, archive: Path) -> RestoreProgress:
    assert instance.coordinator is not None
    restore_id = await begin_and_upload(instance, archive)
    await instance.coordinator.confirm(OPERATOR, 1, **await confirm_arguments(instance))
    return await settle(instance, restore_id)


SECOND_KEY_TEXT = base64.b64encode(bytes(range(32, 64))).decode("ascii")
COMPOSE_STACK_TEXT = "services:\n  web: {image: x}\n"
STAMP = "2026-01-01T00:00:00Z"


def settings_in(root: Path, name: str, key: str) -> Settings:
    """Settings for an application whose data directory is root/name/data."""
    data_dir = root / name / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o700)
    binary_dir = root / name / "bin"
    binary_dir.mkdir(exist_ok=True)
    binary_dir.chmod(0o700)
    binary = binary_dir / "docker-compose"
    if not binary.exists():
        shutil.copyfile(COMPOSE_STUB, binary)
        binary.chmod(0o755)
    env = {
        SECRET_KEY_VAR: key,
        f"{ENV_PREFIX}DATA_DIR": str(data_dir),
        f"{ENV_PREFIX}COMPOSE_BINARY": str(binary),
        f"{ENV_PREFIX}DEVELOPMENT_CHECKS": "true",
        TLS_SELF_SIGNED_VAR: "false",
    } | CHEAP_ARGON2_ENV
    return load_settings(env)


async def real_archive(root: Path, template: Path) -> Path:
    """Run an application, give it a secret in every column and a Swarm stack, and download it."""
    settings = settings_in(root, "archive-source", VALID_KEY)
    application = create_app(settings)
    clock = Clock()
    async with serving_app(application, settings, template):
        database, vault = application.state.database, application.state.vault
        async with database.session() as session:
            repository = CredentialRepository(session, vault)
            seeded: dict[str, int] = {}
            for field_ in SECRET_FIELDS:
                if field_.table not in seeded:
                    seeded[field_.table] = await seed_row(session, field_.table)
                    await session.execute(
                        "UPDATE environment SET name = name || ?, socket_path = socket_path || ?",
                        (field_.table, field_.table),
                    )
                text = f"plain-{field_.table}-{field_.column}"
                assert await repository.seal(field_, seeded[field_.table], text)
            await session.execute(
                "INSERT INTO stack (environment_id, name, kind, origin, project_directory, "
                "compose_file, compose_file_digest, created_at, updated_at) VALUES "
                "(1, 'swarm-one', 'swarm', 'created', ?, ?, ?, ?, ?)",
                ("e" * 32, COMPOSE_STACK_TEXT, "f" * 64, STAMP, STAMP),
            )
        async with database.session() as session:
            service = BackupService(
                session,
                vault,
                database,
                application.state.backup_production,
                clock,
                ActivityRecorder(session, clock),
            )
            stream = await service.download(ADMINISTRATOR)
            data = b"".join([chunk async for chunk in stream])
    target = root / "real-archive.tar.gz"
    target.write_bytes(data)
    return target


def target_template(root: Path, provisioned: Path) -> Path:
    """A provisioned database holding an environment and a stack the archive does not."""
    path = root / "target-template.db"
    shutil.copyfile(provisioned, path)
    connection = connect(path)
    try:
        connection.execute(
            "INSERT INTO environment (name, type, connection_kind, socket_path, created_at, "
            "updated_at) VALUES (?, 'standalone', 'unix_socket', '/b.sock', ?, ?)",
            (LIVE_ENVIRONMENT, STAMP, STAMP),
        )
        connection.execute(
            "INSERT INTO stack (environment_id, name, kind, origin, project_directory, "
            "compose_file, compose_file_digest, created_at, updated_at) VALUES "
            "(1, 'b-stack', 'compose', 'created', ?, 'services: {}\n', ?, ?, ?)",
            (LIVE_STACK, "f" * 64, STAMP, STAMP),
        )
    finally:
        connection.close()
    return path


def target_settings(root: Path, name: str) -> Settings:
    """Settings for an instance that runs with the second key and holds the live stack's files."""
    settings = settings_in(root, name, SECOND_KEY_TEXT)
    stacks = settings.data_dir / "stacks" / LIVE_STACK
    stacks.mkdir(parents=True, exist_ok=True)
    (stacks / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    return settings
