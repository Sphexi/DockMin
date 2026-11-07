"""Stack services over a fake daemon and a scripted Compose, shared by the stack tests."""

import asyncio
import inspect
import json
import re
import shutil
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import Response

from app.auth.principal import Principal
from app.auth.services import AuthRuntime
from app.compose_cli.binary import ComposeBinary
from app.compose_cli.buildx import BuildResult, BuildSpec, BuildxCli
from app.compose_cli.cli import ComposeCli, ComposeProject, ComposeRun
from app.compose_cli.parsers import PsEntry, ResolvedProject
from app.compose_cli.process import Completed
from app.compose_cli.target import ComposeTarget
from app.crypto.vault import Vault
from app.db.engine import Database, Session
from app.db.repositories.environment_tls import EnvironmentTlsRepository
from app.docker_engine.registry import EngineClientRegistry
from app.logging import Scrubber
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    AccessServices,
    StackServices,
    build_access_services,
    build_host_clients,
    build_stack_services,
)
from app.services.compose_setup import ComposeRuntime
from app.services.engine_setup import EngineRuntime
from app.services.object_inspector import EngineObjectInspector
from app.services.object_locks import ObjectLockRegistry, StreamSlots
from app.services.pull_credentials import CredentialChoice
from app.services.stack_document import VariableInput
from app.services.stack_jobs import StackJobRegistry
from app.services.stack_views import ProgressSink, StackOutcome
from app.services.stacks import StackService
from app.services.terminals import TerminalTickets
from tests.access_support import ENV_A
from tests.auth_support import FakeClock
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.tls_material import valid_material
from tests.environment_support import RecordingPoker, ScriptedProbes
from tests.registry_support import RegistryRig, build_rig

SCRIPTED_COMPOSE = Path(__file__).with_name("scripted_compose.py")
BUILDX_STUB = Path(__file__).with_name("buildx_stub.sh")
BUILT_IMAGE_ID = f"sha256:{0xB0117:064x}"
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"


class SimRun:
    """A Compose run whose mutations also change the fake daemon."""

    def __init__(self, run: ComposeRun, sim: "SimCli", project: ComposeProject) -> None:
        self._run = run
        self._sim = sim
        self._project = project

    async def config(self, **kwargs: Any) -> ResolvedProject:
        return await self._run.config(**kwargs)

    async def ps(self) -> tuple[PsEntry, ...]:
        host = self._sim.host
        entries = []
        for box in host.boxes.values():
            if box.labels.get(PROJECT_LABEL) == self._project.name:
                entries.append(
                    PsEntry(
                        box.name, box.labels.get(SERVICE_LABEL, ""), box.state, None, box.exit_code
                    )
                )
        return tuple(entries)

    async def compose_up(self, **kwargs: Any) -> Completed:
        done = await self._run.compose_up(**kwargs)
        resolved = await self._run.config()
        self._sim.apply_up(self._project.name, resolved, bool(kwargs.get("remove_orphans")))
        return done

    async def compose_down(self, **kwargs: Any) -> Completed:
        done = await self._run.compose_down(**kwargs)
        self._sim.apply_down(self._project.name)
        return done

    async def compose_start(self, **kwargs: Any) -> Completed:
        done = await self._run.compose_start(**kwargs)
        self._sim.set_state(self._project.name, "running", kwargs.get("services") or None)
        return done

    async def compose_stop(self, **kwargs: Any) -> Completed:
        done = await self._run.compose_stop(**kwargs)
        self._sim.set_state(self._project.name, "exited", kwargs.get("services") or None)
        return done


class SimCli(ComposeCli):
    """The real wrapper and subprocess machinery, with daemon side effects added to each run."""

    def __init__(self, *args: Any, host: ContainerEngine, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.host = host
        self.created = 0

    @asynccontextmanager
    async def invocation(  # type: ignore[override]
        self,
        project: ComposeProject,
        target: ComposeTarget,
        variables: Mapping[str, str],
        scrubber: Scrubber,
        *,
        deadline: float | None = None,
    ) -> AsyncIterator[SimRun]:
        async with super().invocation(
            project, target, variables, scrubber, deadline=deadline
        ) as run:
            yield SimRun(run, self, project)

    def next_id(self) -> str:
        self.created += 1
        return f"{0xE0000 + self.created:064x}"

    def apply_up(self, name: str, resolved: ResolvedProject, remove_orphans: bool) -> None:
        host = self.host
        for box_id in [
            b.id
            for b in host.boxes.values()
            if b.labels.get(PROJECT_LABEL) == name
            and (remove_orphans or b.labels.get(SERVICE_LABEL) in resolved.services)
        ]:
            del host.boxes[box_id]
        for network in resolved.networks.values():
            if network.get("external"):
                continue
            if not any(n.get("Name") == network["name"] for n in host.networks.values()):
                host.add_network(
                    network["name"], 0xF000 + len(host.networks), Labels={PROJECT_LABEL: name}
                )
        for volume in resolved.volumes.values():
            if volume.get("external"):
                continue
            if volume["name"] not in host.volumes:
                host.add_volume(volume["name"], labels={PROJECT_LABEL: name})
        for key, service in resolved.services.items():
            mounts = [
                {
                    "Type": "volume",
                    "Name": resolved.volumes[m["source"]]["name"],
                    "Destination": m["target"],
                }
                for m in service.get("volumes", [])
                if m.get("type") == "volume" and m.get("source") in resolved.volumes
            ]
            host.add(
                self.next_id(),
                f"{name}-{key}-1",
                state="running",
                labels={PROJECT_LABEL: name, SERVICE_LABEL: key},
                image=service["image"],
                mounts=mounts,
            )

    def apply_down(self, name: str) -> None:
        host = self.host
        for box_id in [b.id for b in host.boxes.values() if b.labels.get(PROJECT_LABEL) == name]:
            del host.boxes[box_id]
        for network_id in [
            i for i, n in host.networks.items() if n.get("Labels", {}).get(PROJECT_LABEL) == name
        ]:
            del host.networks[network_id]

    def set_state(self, name: str, state: str, services: Sequence[str] | None = None) -> None:
        for box in self.host.boxes.values():
            if box.labels.get(PROJECT_LABEL) == name and (
                services is None or box.labels.get(SERVICE_LABEL) in services
            ):
                box.state = state


class SimBuildx(BuildxCli):
    """The real wrapper over the recording stub, with the built image added to the fake daemon."""

    def __init__(self, binary: ComposeBinary, *, host: ContainerEngine, directory: Path) -> None:
        super().__init__(binary)
        self.host = host
        self.directory = directory
        self.built: list[BuildSpec] = []
        self.counter = 0

    async def build(
        self, target: ComposeTarget, workdir: Path, spec: BuildSpec, **kwargs: Any
    ) -> BuildResult:
        self.built.append(spec)
        self.counter += 1
        result = await super().build(target, workdir, spec, **kwargs)
        self.host.add_image(
            spec.tag,
            f"sha256:{0xB0000 + self.counter:064x}",
            {"Labels": dict(spec.labels)},
        )
        return result

    def recorded(self, name: str) -> str:
        """Return what the stub wrote for its last run: `argv`, `env`, `cwd` or `probe`."""
        return (self.directory / name).read_text(encoding="utf-8")


def install_buildx(compose: ComposeRuntime, host: ContainerEngine, directory: Path) -> SimBuildx:
    """Give a runtime a buildx that records into `directory` and builds into `host`."""
    directory.mkdir()
    directory.chmod(0o755)
    shutil.copyfile(BUILDX_STUB, directory / "docker-buildx")
    (directory / "docker-buildx").chmod(0o755)
    sim = SimBuildx(
        ComposeBinary((str(directory / "docker-buildx"),)), host=host, directory=directory
    )
    compose.buildx = sim
    return sim


@dataclass(slots=True)
class Rig:
    services: StackServices
    access: AccessServices
    engine: EngineRuntime
    host: ContainerEngine
    database: Database
    db: Session
    clock: FakeClock
    runtime: AuthRuntime
    registry: RegistryRig
    compose: ComposeRuntime
    sim: SimCli
    inspector: EngineObjectInspector
    data_dir: Path
    buildx: SimBuildx | None = None

    @property
    def app(self) -> Any:
        return SimpleNamespace(
            state=SimpleNamespace(
                engine=self.engine,
                registries=self.registry.registries,
                database=self.database,
                auth=self.runtime,
            )
        )

    def fresh(self) -> StackServices:
        """Services for a new request: nothing remembered from an earlier one."""
        recorder = ActivityRecorder(self.db, self.clock)
        inspector = EngineObjectInspector(build_host_clients(self.db, self.runtime, self.engine))
        access = build_access_services(self.db, self.runtime, inspector, recorder)
        from app.services.assembly import build_stack_services as build

        return build(
            self.db,
            self.database,
            self.runtime,
            self.engine,
            self.registry.registries,
            access,
            inspector,
            recorder,
        )

    async def rows(self, action: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT action, result, target_type, target_id, environment_id, detail, "
            "actor_user_id FROM activity_event"
        )
        params: tuple[Any, ...] = ()
        if action:
            sql += " WHERE action = ?"
            params = (action,)
        found = await self.db.fetch_limited(sql + " ORDER BY id", params, limit=1000)
        return [
            {**dict(row), "detail": json.loads(row["detail"]) if row["detail"] else {}}
            for row in found
        ]


async def install_scripted_compose(
    compose: ComposeRuntime, host: ContainerEngine, operation_timeout: float = 20.0
) -> SimCli:
    """Point a runtime at the scripted Compose and make its runs change `host`."""
    compose.use_binary(ComposeBinary((sys.executable, "-I", str(SCRIPTED_COMPOSE))))
    await compose.detect()
    assert compose.binary is not None
    sim = SimCli(
        compose.binary,
        work_base=compose.work_base,
        operation_timeout=operation_timeout,
        host=host,
    )
    compose._cli = sim
    return sim


@asynccontextmanager
async def build(
    access_template: Path,
    tmp_path: Path,
    runtime: AuthRuntime,
    clock: FakeClock,
    vault: Vault,
    host: ContainerEngine,
    operation_timeout: float = 20.0,
) -> AsyncIterator[Rig]:
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
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        compose = ComposeRuntime(
            data_dir=data_dir, configured=None, timeout_seconds=operation_timeout
        )
        sim = await install_scripted_compose(compose, host, operation_timeout)
        buildx = install_buildx(compose, host, tmp_path / "buildx")
        engine = EngineRuntime(
            registry=daemons,
            probes=ScriptedProbes(),  # type: ignore[arg-type]
            refresher=RecordingPoker(),  # type: ignore[arg-type]
            snapshots=None,  # type: ignore[arg-type]
            vault=vault,
            snapshot_interval=timedelta(minutes=1),
            locks=ObjectLockRegistry(clock),
            stream_slots=StreamSlots(),
            terminal_tickets=TerminalTickets(),
            attach_locks=ObjectLockRegistry(clock),
            compose=compose,
            stack_jobs=StackJobRegistry(clock),
        )
        recorder = ActivityRecorder(db, clock)
        inspector = EngineObjectInspector(build_host_clients(db, runtime, engine))
        access = build_access_services(db, runtime, inspector, recorder)
        services = build_stack_services(
            db, database, runtime, engine, registry.registries, access, inspector, recorder
        )
        rig = Rig(
            services,
            access,
            engine,
            host,
            database,
            db,
            clock,
            runtime,
            registry,
            compose,
            sim,
            inspector,
            data_dir,
            buildx,
        )
        try:
            yield rig
        finally:
            await engine.stack_jobs.aclose()
            await registry.http.aclose()
    await daemons.aclose()


async def create_then_deploy(
    actions: StackService,
    actor: Principal,
    environment_id: int,
    *,
    name: str,
    compose_text: str,
    variables: Sequence[VariableInput] | None = None,
    choices: Mapping[str, CredentialChoice] | None = None,
    progress: ProgressSink | None = None,
) -> StackOutcome:
    """Create a stack and deploy it, as two audited operations in sequence."""
    created = await actions.create(
        actor,
        environment_id,
        name=name,
        compose_text=compose_text,
        variables=variables,
        choices=choices,
        resolve_pulls=True,
    )
    return await actions.deploy(
        actor, environment_id, created.name, choices=choices, progress=progress
    )


async def adopt_then_redeploy(
    actions: StackService,
    actor: Principal,
    environment_id: int,
    *,
    name: str,
    compose_text: str,
    variables: Sequence[VariableInput] | None = None,
    choices: Mapping[str, CredentialChoice] | None = None,
    progress: ProgressSink | None = None,
) -> StackOutcome:
    """Adopt a project and redeploy it, as two audited operations in sequence."""
    adopted = await actions.adopt(
        actor,
        environment_id,
        name=name,
        compose_text=compose_text,
        variables=variables,
        deploy_requested=True,
        choices=choices,
        resolve_pulls=True,
    )
    return await actions.redeploy(
        actor, environment_id, adopted.name, choices=choices, progress=progress
    )


async def wait_job(rig: Rig, principal: Principal, job_id: str, seconds: float = 30.0) -> Any:
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        view = rig.engine.stack_jobs.view(job_id, principal)
        if not view.running:
            return view
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("job did not finish")
        await asyncio.sleep(0.05)


STACK_JOB_PATH = re.compile(r"/jobs/([A-Za-z0-9_-]{22})$")


def job_id_of(response: Response) -> str:
    """Return the job id a stack form post redirected to."""
    assert response.status_code == 303, response.text
    found = STACK_JOB_PATH.search(response.headers["location"])
    assert found is not None, response.headers["location"]
    return found.group(1)


async def settle_stack_job(app: FastAPI, job_id: str, seconds: float = 30.0) -> None:
    """Wait for the task running a stack job to end."""
    task = app.state.engine.stack_jobs._jobs[job_id].task
    done, _ = await asyncio.wait({task}, timeout=seconds)
    assert done, "the stack job did not finish"


@pytest.fixture
async def stopped_stack_jobs(world: Any, stack_host: ContainerEngine) -> AsyncIterator[None]:
    """Cancel every stack job still running before the daemon and the application close."""
    yield
    registry = world.app.state.engine.stack_jobs
    tasks = [job.task for job in registry._jobs.values() if job.task and not job.task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks)


SECRET_SENTINEL = "SENTINEL-secret-value-7731"


@dataclass(frozen=True, slots=True)
class SeededStack:
    stack_id: int
    token: str


def form_variables(*items: tuple[str, str | None, bool]) -> list[Any]:
    """Build submitted variable rows from (name, value, secret) triples."""
    from app.services.stack_document import VariableInput

    return [VariableInput(n, v, s, False, i) for i, (n, v, s) in enumerate(items)]


async def removal_binding(
    rig: Rig, actor: Principal, environment_id: int, name: str, *, volumes: bool = False
) -> dict[str, Any]:
    """Return the arguments that bind a remove to the confirmation a reviewer would be shown."""
    review = await rig.fresh().queries.remove_confirmation(
        actor, environment_id, name, volumes=volumes
    )
    bound: dict[str, Any] = {
        "expected_stack_id": review.stack_id,
        "expected_containers": review.expected_containers,
        "expected_networks": review.expected_networks,
    }
    if volumes:
        bound |= {"remove_volumes": True, "expected_volumes": review.expected_volumes}
    return bound


async def stored_file_digest(rig: Rig, actor: Principal, environment_id: int, name: str) -> str:
    """Return the digest of the stack's stored file, as an edit form would carry it."""
    row = await rig.fresh().queries.require_stack(actor, environment_id, name, "write")
    return str(row.compose_file_digest)


def compose_doc(**service: Any) -> str:
    """Return a Compose file with one nginx service named web, extended by `service`."""
    import yaml

    return yaml.safe_dump({"services": {"web": {"image": "nginx:1", **service}}})


async def seed_stack_row(
    rig: Rig,
    environment_id: int,
    name: str,
    text: str,
    *,
    owner_user_id: int | None = None,
    origin: str = "created",
    variables: tuple[tuple[str, bool, str | None], ...] = (),
) -> SeededStack:
    """Insert a stack row (and optionally its control) as a finished creation would have left it."""
    from app.db.repositories.resource_control import ResourceControlRepository
    from app.db.repositories.stack import StackRepository, VariableWrite
    from app.services.stack_deploy import file_digest

    repository = StackRepository(rig.db, rig.registry.vault, rig.clock)
    token = rig.compose.files.new_token()
    stack_id = await repository.insert(
        environment_id=environment_id,
        name=name,
        kind="compose",
        origin=origin,  # type: ignore[arg-type]
        project_directory=token,
        compose_file=text,
        compose_file_digest=file_digest(text),
        created_by_user_id=owner_user_id,
    )
    if variables:
        await repository.replace_variables(
            stack_id, [VariableWrite(n, secret, value) for n, secret, value in variables]
        )
    if owner_user_id is not None:
        await ResourceControlRepository(rig.db, rig.clock).create(
            environment_id,
            "stack",
            name,
            is_public=False,
            is_administrators_only=False,
            access_level="write",
            owner_user_id=owner_user_id,
        )
    return SeededStack(stack_id, token)


async def control_on_own_session(
    rig: Rig, environment_id: int, resource_type: str, resource_id: str
) -> None:
    """Put an object under a control from a session of its own, as another request would."""
    from app.db.repositories.resource_control import ResourceControlRepository

    async with rig.database.session() as db:
        await ResourceControlRepository(db, rig.clock).create(
            environment_id,
            resource_type,  # type: ignore[arg-type]
            resource_id,
            is_public=False,
            is_administrators_only=False,
            access_level="read",
            owner_user_id=None,
        )


def project_box(
    host: ContainerEngine, number: int, project: str, service: str = "web", **fields: Any
) -> Any:
    """Add a container that carries a Compose project and service label to the fake daemon."""
    return host.add(
        f"{0xA0000 + number:064x}",
        f"{project.lower()}-{service}-{number}",
        labels={PROJECT_LABEL: project, SERVICE_LABEL: service},
        **fields,
    )


async def activity_dump(rig: Rig) -> str:
    """Return every activity row as one JSON text, for a search that must find nothing."""
    found = await rig.db.fetch_limited("SELECT * FROM activity_event", (), limit=10_000)
    return json.dumps([dict(row) for row in found], default=str)


async def validation_outcomes(
    queries: Any, actor: Principal, environment_id: int, documents: Mapping[str, str]
) -> dict[str, Any]:
    """Validate each document as `shop` and return, per key, the report or the error raised."""
    outcomes: dict[str, Any] = {}
    for key, text in documents.items():
        try:
            outcomes[key] = await queries.validate(
                actor, environment_id, name="shop", compose_text=text, variables=None
            )
        except Exception as error:
            outcomes[key] = error
    return outcomes


SECRET_SENTINEL = "S3CR3T-SENTINEL-VALUE"


def variable_fields(*rows: tuple[str, str, bool]) -> dict[str, str]:
    """Return the form fields of variable rows given as (name, value, secret)."""
    fields: dict[str, str] = {}
    for index, (name, value, secret) in enumerate(rows):
        fields[f"variable-{index}-name"] = name
        fields[f"variable-{index}-value"] = value
        if secret:
            fields[f"variable-{index}-secret"] = "on"
    return fields


async def use_compose_limits(
    compose: ComposeRuntime, host: ContainerEngine, *, operation_timeout: float, slots: int
) -> SimCli:
    """Replace the runtime's scripted Compose with one that has a set deadline and slot count."""
    assert compose.binary is not None
    sim = SimCli(
        compose.binary,
        work_base=compose.work_base,
        operation_timeout=operation_timeout,
        slots=slots,
        host=host,
    )
    compose._cli = sim
    return sim


def read_records(path: Path) -> list[dict[str, Any]]:
    """Return the invocations the scripted Compose recorded to `path`, oldest first."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def files_containing(root: Path, needle: str) -> list[str]:
    """Return every file beneath root whose name or contents hold the text."""
    encoded = needle.encode()
    return [
        str(path)
        for path in root.rglob("*")
        if needle in str(path.relative_to(root))
        or (path.is_file() and not path.is_symlink() and encoded in path.read_bytes())
    ]


async def poll_until(condition: Any, seconds: float = 10.0) -> None:
    """Wait for a condition, plain or awaitable, with no event to wait on; fail after a bound."""
    async with asyncio.timeout(seconds):
        while True:
            outcome = condition()
            if inspect.isawaitable(outcome):
                outcome = await outcome
            if outcome:
                return
            await asyncio.sleep(0.02)


def recorded_call(path: Path, command: str) -> dict[str, Any]:
    """Return the one invocation of a Compose command the scripted Compose recorded."""
    [found] = [call for call in read_records(path) if call["cmd"] == command]
    return found


def logged_text(caplog: pytest.LogCaptureFixture) -> str:
    """Return every captured log record, message and extra fields, as one text."""
    return " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
