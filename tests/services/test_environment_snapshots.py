"""The snapshot refresh: counts from fake Engines, failures recorded, the pokes' lifetime."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

import app.docker_engine.resources as engine_resources
import app.services.environment_snapshots as snapshot_module
from app.crypto.vault import Vault
from app.db.engine import Database
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.environment import (
    ConnectionKind,
    Environment,
    EnvironmentDashboardRepository,
    EnvironmentRepository,
    EnvironmentStatus,
    StatusRecord,
)
from app.db.repositories.environment_snapshot import (
    EnvironmentSnapshot,
    EnvironmentSnapshotRepository,
)
from app.docker_engine.client import EngineClient
from app.docker_engine.probe import ProbeStatus, Stage
from app.docker_engine.registry import EngineClientRegistry
from app.docker_engine.transport import ConnectionSettings, TransportBundle, UnixSocketSettings
from app.notifications.catalogue import match
from app.services.engine_setup import build_engine_runtime
from app.services.environment_snapshots import (
    STORED_TIME_REFRESH,
    EnvironmentSnapshotService,
    RefreshResult,
    SnapshotPokes,
)
from tests.access_support import STAMP
from tests.auth_support import FakeClock
from tests.crypto.helpers import FIRST_KEY, vault_with
from tests.db.helpers import CREATED_STAMP
from tests.docker_engine.fake_engine import Ending, FakeEngine, Reply, Stream, json_reply
from tests.environment_support import (
    ScriptedProbes,
    failed_result,
    seed_environment,
)
from tests.notification_support import add_channel

CONTAINERS, RUNNING, STOPPED, IMAGES = 5, 3, 2, 4
PAUSED, UNHEALTHY = 1, 2
VOLUMES, NETWORKS = 2, 3
SERVICES, NODES = 6, 2
OVER_THE_CAP_BYTES = 5 * 1024 * 1024
INT64_MAX = 2**63 - 1
TOO_MANY = "Too many objects to count."
HOST_BUDGET = 0.05
ABSENT_ID = 9999
ONE_MINUTE = timedelta(minutes=1)
ONE_SECOND = timedelta(seconds=1)
ANOTHER_CONTAINER = CONTAINERS + 1


def info_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "ServerVersion": "29.2.1",
        "Containers": CONTAINERS,
        "ContainersRunning": RUNNING,
        "ContainersStopped": STOPPED,
        "Images": IMAGES,
        "Swarm": {"LocalNodeState": "inactive", "ControlAvailable": False},
    }
    return document | overrides


def hanging_engine() -> FakeEngine:
    engine = stocked_engine()
    engine.route("GET", "/info", Stream([], Ending.HOLD))
    return engine


def stocked_engine(**info: object) -> FakeEngine:
    engine = FakeEngine()
    engine.route("GET", "/info", json_reply(info_document(**info)))
    engine.route(
        "GET", "/volumes", json_reply({"Volumes": [{"Name": f"v{n}"} for n in range(VOLUMES)]})
    )
    engine.route("GET", "/networks", json_reply([{"Id": f"n{n}"} for n in range(NETWORKS)]))
    return engine


@dataclass(slots=True)
class Rig:
    """A snapshot service over a real database and one fake Engine per socket path."""

    database: Database
    clock: FakeClock
    engines: dict[str, FakeEngine]
    probes: ScriptedProbes
    registry: EngineClientRegistry
    service: EnvironmentSnapshotService
    ids: dict[str, int] = field(default_factory=dict)

    async def add(
        self,
        name: str,
        engine: FakeEngine,
        *,
        status: EnvironmentStatus = "reachable",
        kind: ConnectionKind = "unix_socket",
        endpoint: str | None = None,
    ) -> int:
        path = endpoint or f"/run/{name}.sock"
        self.engines[path] = engine
        async with self.database.session() as db:
            created = await seed_environment(
                db, self.clock, name, kind=kind, endpoint=path, status=status
            )
        self.ids[name] = created.id
        return created.id

    async def add_many(self, count: int) -> None:
        for number in range(count):
            await self.add(f"host{number}", stocked_engine())

    async def environment(self, name: str) -> Environment:
        async with self.database.session() as db:
            found = await EnvironmentRepository(db, self.clock).get(self.ids[name])
        assert found is not None
        return found

    async def snapshot(self, name: str) -> EnvironmentSnapshot | None:
        async with self.database.session() as db:
            return await EnvironmentSnapshotRepository(db).get(self.ids[name])

    async def events(self) -> list[dict[str, Any]]:
        async with self.database.session() as db:
            rows = await db.fetch_limited(
                "SELECT action, actor_type, actor_subsystem, rule_id, target_type, target_id, "
                "environment_id, result, detail FROM activity_event ORDER BY id",
                (),
                limit=100,
            )
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    async def event_count(self) -> int:
        async with self.database.session() as db:
            row = await db.fetch_one("SELECT count(*) AS total FROM activity_event")
        assert row is not None
        return int(row["total"])


def build_rig(
    database: Database,
    clock: FakeClock,
    vault: Vault,
    *,
    registry_type: type[EngineClientRegistry] = EngineClientRegistry,
    **service_options: Any,
) -> Rig:
    engines: dict[str, FakeEngine] = {}

    def bundle(settings: ConnectionSettings) -> TransportBundle:
        assert isinstance(settings, UnixSocketSettings)
        return engines[settings.path].bundle()

    probes = ScriptedProbes(result=failed_result(Stage.SOCKET, "Nothing exists at this path."))
    registry = registry_type(bundle)
    service = EnvironmentSnapshotService(
        database=database,
        vault=vault,
        registry=registry,
        probes=probes.as_coordinator(),
        clock=clock,
        **service_options,
    )
    return Rig(database, clock, engines, probes, registry, service)


@pytest.fixture
def vault() -> Vault:
    return vault_with(FIRST_KEY)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def make_rig(
    database: Database, clock: FakeClock, vault: Vault
) -> AsyncIterator[Callable[..., Rig]]:
    """Return a factory for snapshot rigs; every rig and the database are closed afterwards."""
    built: list[Rig] = []

    def make(**options: Any) -> Rig:
        rig = build_rig(database, clock, vault, **options)
        built.append(rig)
        return rig

    yield make
    for rig in built:
        await rig.service.aclose()
        await rig.registry.aclose()
    await database.aclose()


@pytest.fixture
def rig(make_rig: Callable[..., Rig]) -> Rig:
    return make_rig()


async def test_a_refresh_stores_the_counts_the_host_reports_and_marks_it_reachable(
    rig: Rig,
) -> None:
    await rig.add("alpha", stocked_engine(), status="unknown")

    result = await rig.service.refresh_one(rig.ids["alpha"])

    snapshot = await rig.snapshot("alpha")
    environment = await rig.environment("alpha")
    assert result == RefreshResult(rig.ids["alpha"], "reachable", True)
    assert snapshot is not None and snapshot.error is None
    counts = snapshot.counts
    assert (counts.container_count, counts.container_running_count) == (CONTAINERS, RUNNING)
    assert (counts.container_stopped_count, counts.image_count) == (STOPPED, IMAGES)
    assert (counts.volume_count, counts.network_count) == (VOLUMES, NETWORKS)
    assert (counts.service_count, counts.swarm_node_count) == (None, None)
    assert snapshot.captured_at == format_timestamp(rig.clock.now)
    assert (environment.status, environment.last_contacted_at) == (
        "reachable",
        format_timestamp(rig.clock.now),
    )


async def test_a_refresh_stores_the_paused_and_unhealthy_counts(
    rig: Rig,
) -> None:
    engine = stocked_engine(ContainersPaused=PAUSED)
    engine.route("GET", "/containers/json", json_reply([{"Id": "a"}, {"Id": "b"}]))
    await rig.add("alpha", engine)

    await rig.service.refresh_one(rig.ids["alpha"])

    snapshot = await rig.snapshot("alpha")
    assert snapshot is not None
    assert (snapshot.counts.container_paused_count, snapshot.counts.container_unhealthy_count) == (
        PAUSED,
        UNHEALTHY,
    )
    assert engine.calls("GET", "/containers/json")[0].query["filters"] == (
        '{"health": ["unhealthy"]}'
    )


async def test_a_failed_unhealthy_count_leaves_only_that_count_unknown(rig: Rig) -> None:
    engine = stocked_engine(ContainersPaused=PAUSED)
    engine.route("GET", "/containers/json", Reply(500))
    await rig.add("alpha", engine)

    await rig.service.refresh_one(rig.ids["alpha"])

    snapshot = await rig.snapshot("alpha")
    assert snapshot is not None and snapshot.error is None
    assert snapshot.counts.container_unhealthy_count is None
    assert (snapshot.counts.container_count, snapshot.counts.container_paused_count) == (
        CONTAINERS,
        PAUSED,
    )
    assert snapshot.counts.volume_count == VOLUMES


async def test_a_host_that_stops_answering_records_one_unreachable_row_for_the_refresher(
    rig: Rig,
) -> None:
    engine = FakeEngine()
    engine.route("GET", "/info", Reply(500))
    await rig.add("alpha", engine)

    await rig.service.refresh_one(rig.ids["alpha"])
    await rig.service.refresh_one(rig.ids["alpha"])

    [event] = await rig.events()
    assert (event["action"], event["result"]) == ("environment_unreachable", "success")
    assert (event["actor_type"], event["actor_subsystem"], event["rule_id"]) == (
        "subsystem",
        "environment-snapshot-refresher",
        None,
    )
    assert (event["target_type"], event["target_id"]) == ("environment", str(rig.ids["alpha"]))
    assert event["environment_id"] == rig.ids["alpha"]
    assert event["detail"]["previous_status"] == "reachable"
    assert [
        row.key for row in match(event["action"], event["result"], event["actor_subsystem"])
    ] == ["environment_unreachable"]


async def test_a_host_that_answers_again_records_one_reachable_row(rig: Rig) -> None:
    await rig.add("alpha", stocked_engine(), status="unreachable")

    await rig.service.refresh_one(rig.ids["alpha"])
    await rig.service.refresh_one(rig.ids["alpha"])

    [event] = await rig.events()
    assert (event["action"], event["actor_subsystem"]) == (
        "environment_reachable",
        "environment-snapshot-refresher",
    )
    assert event["detail"]["previous_status"] == "unreachable"
    assert [
        row.key for row in match(event["action"], event["result"], event["actor_subsystem"])
    ] == ["environment_reachable"]


async def test_a_host_that_goes_down_and_comes_back_records_both_changes_in_order(
    rig: Rig,
) -> None:
    engine = stocked_engine()
    await rig.add("alpha", engine)
    engine.route("GET", "/info", Reply(500))
    await rig.service.refresh_one(rig.ids["alpha"])
    engine.route("GET", "/info", json_reply(info_document()))
    rig.clock.now += ONE_MINUTE

    await rig.service.refresh_one(rig.ids["alpha"])

    assert [event["action"] for event in await rig.events()] == [
        "environment_unreachable",
        "environment_reachable",
    ]


async def test_a_refresh_that_leaves_the_status_as_it_was_records_nothing(rig: Rig) -> None:
    await rig.add("alpha", stocked_engine())
    await rig.add("beta", stocked_engine(), status="unknown")
    await rig.add("gamma", FakeEngine(), status="unknown")
    await rig.add("delta", FakeEngine(), status="unreachable")
    rig.engines["/run/gamma.sock"].route("GET", "/info", Reply(500))
    rig.engines["/run/delta.sock"].route("GET", "/info", Reply(500))

    await rig.service.refresh_all()

    assert await rig.event_count() == 0


async def test_a_transition_is_queued_for_a_channel_subscribed_to_it(rig: Rig) -> None:
    channel = await add_channel(
        rig.database, rig.clock, "chat", ["environment_unreachable", "environment_reachable"]
    )
    engine = FakeEngine()
    engine.route("GET", "/info", Reply(500))
    await rig.add("alpha", engine)

    await rig.service.refresh_one(rig.ids["alpha"])

    async with rig.database.session() as db:
        queued = await db.fetch_limited(
            "SELECT channel_id, event_type FROM notification_outbox", (), limit=10
        )
    assert [(row["channel_id"], row["event_type"]) for row in queued] == [
        (channel, "environment_unreachable")
    ]


async def test_a_swarm_manager_also_reports_its_services_and_nodes(rig: Rig) -> None:
    engine = stocked_engine(Swarm={"LocalNodeState": "active", "ControlAvailable": True})
    engine.route("GET", "/services", json_reply([{"ID": f"s{n}"} for n in range(SERVICES)]))
    engine.route("GET", "/nodes", json_reply([{"ID": f"n{n}"} for n in range(NODES)]))
    await rig.add("alpha", engine)

    await rig.service.refresh_one(rig.ids["alpha"])

    snapshot = await rig.snapshot("alpha")
    environment = await rig.environment("alpha")
    assert snapshot is not None
    assert (snapshot.counts.service_count, snapshot.counts.swarm_node_count) == (SERVICES, NODES)
    assert (environment.type, environment.swarm_control_available) == ("swarm", True)


async def test_a_listing_over_the_cap_records_an_error_and_null_counts(rig: Rig) -> None:
    engine = stocked_engine()
    engine.route("GET", "/volumes", Reply(200, b"[" + b"0," * OVER_THE_CAP_BYTES + b"0]"))
    await rig.add("alpha", engine)

    result = await rig.service.refresh_one(rig.ids["alpha"])

    snapshot = await rig.snapshot("alpha")
    assert result.status == "reachable"
    assert snapshot is not None
    assert snapshot.error == TOO_MANY
    assert snapshot.counts.container_count is None
    assert snapshot.counts.volume_count is None


async def test_counts_outside_the_storable_range_are_clamped(rig: Rig) -> None:
    await rig.add("alpha", stocked_engine(Containers=-5, Images=2**70))

    await rig.service.refresh_one(rig.ids["alpha"])

    snapshot = await rig.snapshot("alpha")
    assert snapshot is not None
    assert (snapshot.counts.container_count, snapshot.counts.image_count) == (0, INT64_MAX)


async def test_an_unreachable_host_records_the_stage_an_error_and_null_counts(rig: Rig) -> None:
    engine = stocked_engine()
    engine.route("GET", "/info", Reply(500))
    await rig.add("alpha", engine)

    result = await rig.service.refresh_one(rig.ids["alpha"])

    snapshot = await rig.snapshot("alpha")
    environment = await rig.environment("alpha")
    assert result.status == "unreachable"
    assert snapshot is not None
    assert snapshot.error == "Nothing exists at this path."
    assert snapshot.counts.container_count is None
    assert (environment.status, environment.failed_stage) == ("unreachable", "socket")
    assert environment.last_contacted_at is not None


async def test_a_host_whose_api_is_too_old_is_recorded_as_version_unsupported(rig: Rig) -> None:
    rig.probes.result = failed_result(
        Stage.ENGINE, "Upgrade Docker.", ProbeStatus.VERSION_UNSUPPORTED
    )
    engine = stocked_engine()
    engine.route("GET", "/_ping", Reply(headers={"Api-Version": "1.40"}))
    await rig.add("alpha", engine)

    await rig.service.refresh_one(rig.ids["alpha"])

    environment = await rig.environment("alpha")
    assert (environment.status, environment.failed_stage) == ("version_unsupported", "engine")


async def test_a_remote_environment_whose_material_is_missing_is_recorded_unreachable(
    rig: Rig,
) -> None:
    await rig.add("remote", FakeEngine(), kind="tcp_tls", endpoint="tcp://docker.example.test:2376")

    result = await rig.service.refresh_one(rig.ids["remote"])

    stored = await rig.environment("remote")
    assert result.status == "unreachable"
    assert stored.failed_stage == "tls_handshake"


async def test_a_refresh_of_an_environment_that_is_gone_records_nothing(rig: Rig) -> None:
    result = await rig.service.refresh_one(ABSENT_ID)

    assert result == RefreshResult(ABSENT_ID, None, False)


async def test_a_daemon_upgrade_is_noticed_by_renegotiating_once(rig: Rig) -> None:
    engine = stocked_engine()
    await rig.add("alpha", engine)
    await rig.service.refresh_one(rig.ids["alpha"])
    await rig.service.refresh_one(rig.ids["alpha"])
    unchanged = len(engine.calls("GET", "/version"))
    engine.route("GET", "/info", json_reply(info_document(ServerVersion="30.0.0")))

    await rig.service.refresh_one(rig.ids["alpha"])

    environment = await rig.environment("alpha")
    assert unchanged == 1
    assert len(engine.calls("GET", "/version")) == 2
    assert environment.engine_version == "30.0.0"


async def test_one_host_that_never_answers_does_not_delay_the_others(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(host_budget=HOST_BUDGET)
    await rig.add("hung", hanging_engine())
    await rig.add("fine", stocked_engine())

    attempted = await rig.service.refresh_all()

    fine, stuck = await rig.snapshot("fine"), await rig.snapshot("hung")
    assert attempted == 2
    assert fine is not None and fine.counts.container_count == CONTAINERS
    assert stuck is not None
    assert "refresh time limit" in (stuck.error or "")
    assert (await rig.environment("hung")).status == "unreachable"


class MeteredRegistry(EngineClientRegistry):
    """Holds every caller of client_for until released, counting how many are held at once."""

    active = 0
    peak = 0
    wanted = 0
    saturated: asyncio.Event
    release: asyncio.Event

    async def client_for(
        self,
        environment_id: int,
        fingerprint: str,
        settings_factory: Callable[[], ConnectionSettings | Awaitable[ConnectionSettings]],
    ) -> EngineClient:
        cls = type(self)
        cls.active += 1
        cls.peak = max(cls.peak, cls.active)
        if cls.active >= cls.wanted:
            cls.saturated.set()
        try:
            await cls.release.wait()
            return await super().client_for(environment_id, fingerprint, settings_factory)
        finally:
            cls.active -= 1


async def settle(turns: int = 500) -> None:
    """Give every runnable task, and every finished worker-thread call, time to be picked up."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def test_no_more_hosts_than_the_concurrency_limit_are_refreshed_at_once(
    make_rig: Callable[..., Rig],
) -> None:
    limit = 2
    MeteredRegistry.active = MeteredRegistry.peak = 0
    MeteredRegistry.wanted = limit
    MeteredRegistry.saturated, MeteredRegistry.release = asyncio.Event(), asyncio.Event()
    rig = make_rig(registry_type=MeteredRegistry, concurrency=limit)
    await rig.add_many(5)
    refreshing = asyncio.create_task(rig.service.refresh_all())
    await asyncio.wait_for(MeteredRegistry.saturated.wait(), 1)
    await settle()
    held_at_once = MeteredRegistry.peak

    MeteredRegistry.release.set()
    attempted = await refreshing

    assert held_at_once == limit
    assert attempted == 5


async def test_two_requests_for_one_environment_share_a_single_refresh(rig: Rig) -> None:
    engine = stocked_engine()
    await rig.add("alpha", engine)

    first, second = await asyncio.gather(
        rig.service.refresh_one(rig.ids["alpha"]), rig.service.refresh_one(rig.ids["alpha"])
    )

    assert first == second
    assert len(engine.calls("GET", "/info")) == 1


class MovingRegistry(EngineClientRegistry):
    """Repoints the environment at another socket as its client is obtained."""

    database: Database
    clock: FakeClock
    moves_left = 0
    targets: list[str]

    async def client_for(
        self,
        environment_id: int,
        fingerprint: str,
        settings_factory: Callable[[], ConnectionSettings | Awaitable[ConnectionSettings]],
    ) -> EngineClient:
        client = await super().client_for(environment_id, fingerprint, settings_factory)
        if type(self).moves_left > 0:
            type(self).moves_left -= 1
            path = type(self).targets.pop(0)
            async with self.database.session() as db:
                current = await EnvironmentRepository(db, self.clock).get(environment_id)
                assert current is not None
                await EnvironmentRepository(db, self.clock).replace_connection(
                    environment_id,
                    connection_kind="unix_socket",
                    socket_path=path,
                    url=None,
                    record=StatusRecord("unknown", None, None, STAMP),
                )
        return client


@pytest.fixture
def make_moving_rig(
    database: Database, clock: FakeClock, make_rig: Callable[..., Rig]
) -> Callable[..., Rig]:
    """Return a factory for rigs whose environment is repointed while it is being refreshed."""

    def make(moves: int, *, hanging: bool = False, **options: Any) -> Rig:
        MovingRegistry.moves_left = moves
        MovingRegistry.targets = [f"/run/moved{number}.sock" for number in range(moves)]
        MovingRegistry.database = database
        MovingRegistry.clock = clock
        rig = make_rig(registry_type=MovingRegistry, **options)
        destination = hanging_engine if hanging else stocked_engine
        rig.engines |= {f"/run/moved{number}.sock": destination() for number in range(moves)}
        return rig

    return make


async def test_an_observation_of_a_host_the_environment_no_longer_points_at_is_not_stored(
    make_moving_rig: Callable[..., Rig],
) -> None:
    rig = make_moving_rig(2)
    await rig.add("alpha", stocked_engine(Containers=99))

    result = await rig.service.refresh_one(rig.ids["alpha"])

    assert (result.recorded, result.superseded) == (False, True)
    assert await rig.snapshot("alpha") is None


async def test_a_refresh_superseded_once_is_repeated_against_the_new_host(
    make_moving_rig: Callable[..., Rig],
) -> None:
    rig = make_moving_rig(1)
    await rig.add("alpha", stocked_engine(Containers=99))

    result = await rig.service.refresh_one(rig.ids["alpha"])

    snapshot = await rig.snapshot("alpha")
    assert result.recorded
    assert snapshot is not None and snapshot.counts.container_count == CONTAINERS


async def test_a_timed_out_refresh_of_a_host_that_was_replaced_meanwhile_is_not_stored(
    make_moving_rig: Callable[..., Rig],
) -> None:
    rig = make_moving_rig(2, hanging=True, host_budget=HOST_BUDGET)
    await rig.add("alpha", hanging_engine())

    result = await rig.service.refresh_one(rig.ids["alpha"])

    environment = await rig.environment("alpha")
    assert (result.recorded, result.superseded) == (False, True)
    assert await rig.snapshot("alpha") is None
    assert environment.status == "unknown"


@dataclass(slots=True)
class StubService:
    """Records what the pokes asked of the snapshot service."""

    polled: list[int] = field(default_factory=list)
    closed: bool = False

    async def refresh_one(self, environment_id: int) -> object:
        self.polled.append(environment_id)
        await asyncio.Event().wait()
        return None

    async def aclose(self) -> None:
        self.closed = True


@dataclass(slots=True)
class FailingPoke(StubService):
    async def refresh_one(self, environment_id: int) -> object:
        self.polled.append(environment_id)
        raise RuntimeError("database busy")


async def test_closing_the_pokes_leaves_no_task_running() -> None:
    before = asyncio.all_tasks()
    stub = StubService()
    pokes = SnapshotPokes(stub)
    pokes.poke(7)
    await asyncio.sleep(0)

    await pokes.aclose()

    leftover = {task for task in asyncio.all_tasks() - before if not task.done()}
    assert stub.polled == [7]
    assert stub.closed
    assert leftover <= {asyncio.current_task()}


async def test_closed_pokes_ignore_a_request() -> None:
    stub = StubService()
    pokes = SnapshotPokes(stub)
    await pokes.aclose()

    pokes.poke(3)
    await asyncio.sleep(0)

    assert stub.polled == []


async def test_a_poke_that_fails_is_logged_and_does_not_raise(
    logged: Callable[[str], list[dict[str, Any]]],
) -> None:
    stub = FailingPoke()
    pokes = SnapshotPokes(stub)

    pokes.poke(5)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await pokes.aclose()

    assert stub.polled == [5]
    assert [record["error"] for record in logged("snapshot_poke_failed")] == ["RuntimeError"]


async def test_the_engine_runtime_starts_no_refresh_task_and_closing_it_leaves_none_running(
    database: Database, clock: FakeClock, vault: Vault
) -> None:
    before = asyncio.all_tasks()
    runtime = build_engine_runtime(
        database=database, vault=vault, snapshot_interval_seconds=60, clock=clock
    )
    await asyncio.sleep(0)
    started = asyncio.all_tasks() - before

    await runtime.aclose()

    leftover = {task for task in asyncio.all_tasks() - before if not task.done()}
    assert started == set()
    assert isinstance(runtime.snapshots, EnvironmentSnapshotService)
    assert runtime.stale_after == timedelta(minutes=3)
    assert leftover <= {asyncio.current_task()}
    await database.aclose()


PROJECT_LABEL = "com.docker.compose.project"
PROJECT_CAP = 2048
SHORT_BOUND = 0.05
INSTALLED_COMPOSE = "v2.31.0"
UPGRADED_COMPOSE = "v2.32.1"
STACK_COUNT_BOUND_SECONDS = 4.0


def labelled(number: int, labels: dict[str, str]) -> dict[str, Any]:
    return {"Id": f"{number:064x}", "Labels": labels}


def project_rows(*names: str) -> list[dict[str, Any]]:
    return [labelled(number, {PROJECT_LABEL: name}) for number, name in enumerate(names)]


async def stack_count_of(rig: Rig, name: str) -> int | None:
    snapshot = await rig.snapshot(name)
    assert snapshot is not None
    return snapshot.counts.stack_count


async def test_the_stack_count_is_the_distinct_case_folded_compose_projects_on_the_host(
    rig: Rig,
) -> None:
    engine = stocked_engine()
    rows = [*project_rows("Shop", "shop", "BLOG", "blog"), labelled(9, {"app": "web"})]
    engine.route("GET", "/containers/json", json_reply(rows))
    await rig.add("alpha", engine)

    await rig.service.refresh_one(rig.ids["alpha"])

    snapshot = await rig.snapshot("alpha")
    assert snapshot is not None and snapshot.error is None
    assert snapshot.counts.stack_count == 2
    queries = [call.query for call in engine.calls("GET", "/containers/json")]
    assert {"label": [PROJECT_LABEL]} in [json.loads(q["filters"]) for q in queries]


async def test_a_host_with_no_compose_projects_counts_zero_stacks_not_unknown(rig: Rig) -> None:
    engine = stocked_engine()
    engine.route("GET", "/containers/json", json_reply([]))
    await rig.add("alpha", engine)

    await rig.service.refresh_one(rig.ids["alpha"])

    assert await stack_count_of(rig, "alpha") == 0


def over_the_cap() -> Reply:
    rows = json.dumps(project_rows("shop", "blog", "mail")).encode("utf-8")
    return Reply(200, rows.ljust(PROJECT_CAP + 1), {"Content-Type": "application/json"})


@pytest.mark.parametrize(
    ("behaviour", "unhealthy"),
    [
        pytest.param(over_the_cap(), 3, id="project-list-over-its-cap"),
        pytest.param(Reply(500), None, id="engine-error"),
        pytest.param(Stream([], Ending.HOLD), None, id="no-answer-within-the-bound"),
    ],
)
async def test_an_unreadable_project_list_leaves_only_the_stack_count_unknown(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    behaviour: Reply | Stream,
    unhealthy: int | None,
) -> None:
    monkeypatch.setattr(engine_resources, "CONTAINER_IDS_BYTES", PROJECT_CAP)
    monkeypatch.setattr(snapshot_module, "STACK_COUNT_SECONDS", SHORT_BOUND)
    monkeypatch.setattr(snapshot_module, "UNHEALTHY_COUNT_SECONDS", SHORT_BOUND)
    engine = stocked_engine()
    engine.route("GET", "/containers/json", behaviour)
    await rig.add("alpha", engine)

    result = await rig.service.refresh_one(rig.ids["alpha"])

    snapshot = await rig.snapshot("alpha")
    assert result.status == "reachable"
    assert snapshot is not None and snapshot.error is None
    counts = snapshot.counts
    assert counts.stack_count is None
    assert counts.container_unhealthy_count == unhealthy
    assert (counts.container_count, counts.volume_count) == (CONTAINERS, VOLUMES)
    assert counts.network_count == NETWORKS


def test_the_stack_count_is_bounded_at_four_seconds() -> None:
    assert snapshot_module.STACK_COUNT_SECONDS == STACK_COUNT_BOUND_SECONDS


async def test_the_compose_version_is_written_with_the_status_and_follows_the_provider(
    make_rig: Callable[..., Rig],
) -> None:
    versions: list[str | None] = [INSTALLED_COMPOSE]
    rig = make_rig(compose_version=lambda: versions[0])
    await rig.add("alpha", stocked_engine())
    await rig.service.refresh_one(rig.ids["alpha"])
    installed = (await rig.environment("alpha")).compose_cli_version
    versions[0] = UPGRADED_COMPOSE

    await rig.service.refresh_one(rig.ids["alpha"])

    assert installed == INSTALLED_COMPOSE
    assert (await rig.environment("alpha")).compose_cli_version == UPGRADED_COMPOSE


async def test_a_refresh_with_no_compose_version_keeps_the_last_one_recorded(
    make_rig: Callable[..., Rig],
) -> None:
    versions: list[str | None] = [INSTALLED_COMPOSE]
    rig = make_rig(compose_version=lambda: versions[0])
    engine = stocked_engine()
    await rig.add("alpha", engine)
    await rig.service.refresh_one(rig.ids["alpha"])
    versions[0] = None
    await rig.service.refresh_one(rig.ids["alpha"])
    kept_while_unknown = (await rig.environment("alpha")).compose_cli_version
    engine.route("GET", "/info", Reply(500))

    await rig.service.refresh_one(rig.ids["alpha"])

    environment = await rig.environment("alpha")
    assert kept_while_unknown == INSTALLED_COMPOSE
    assert environment.status == "unreachable"
    assert environment.compose_cli_version == INSTALLED_COMPOSE


async def test_without_a_compose_provider_no_version_is_recorded(rig: Rig) -> None:
    await rig.add("alpha", stocked_engine())

    await rig.service.refresh_one(rig.ids["alpha"])

    assert (await rig.environment("alpha")).compose_cli_version is None


SWARM_NAMESPACE = "com.docker.stack.namespace"
MANAGER_INFO: dict[str, object] = {"LocalNodeState": "active", "ControlAvailable": True}


def namespace_services(*names: str) -> list[dict[str, Any]]:
    return [
        {
            "ID": f"{number:025d}",
            "Version": {"Index": 1},
            "Spec": {"Name": f"s{number}", "Labels": {SWARM_NAMESPACE: name}},
        }
        for number, name in enumerate(names)
    ]


def manager_engine(
    *, namespaces: Reply | list[dict[str, Any]], projects: list[dict[str, Any]]
) -> FakeEngine:
    engine = stocked_engine(Swarm=MANAGER_INFO)
    engine.route(
        "GET", "/services", namespaces if isinstance(namespaces, Reply) else json_reply(namespaces)
    )
    engine.route("GET", "/nodes", json_reply([{"ID": f"n{n}"} for n in range(NODES)]))
    engine.route("GET", "/containers/json", json_reply(projects))
    return engine


async def test_a_manager_counts_the_union_of_compose_projects_and_swarm_namespaces_case_folded(
    rig: Rig,
) -> None:
    engine = manager_engine(
        namespaces=namespace_services("Shop", "SHOP", "queue"),
        projects=project_rows("shop", "blog"),
    )
    await rig.add("alpha", engine)

    await rig.service.refresh_one(rig.ids["alpha"])

    assert await stack_count_of(rig, "alpha") == 3
    filters = [
        json.loads(c.query["filters"])
        for c in engine.calls("GET", "/services")
        if "filters" in c.query
    ]
    assert {"label": [SWARM_NAMESPACE]} in filters


async def test_a_namespace_list_over_its_cap_leaves_only_the_stack_count_unknown(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_resources, "STACK_NAMESPACES_BYTES", 64)
    engine = manager_engine(
        namespaces=namespace_services("shop", "blog", "mail"), projects=project_rows("shop")
    )
    await rig.add("alpha", engine)

    await rig.service.refresh_one(rig.ids["alpha"])

    snapshot = await rig.snapshot("alpha")
    assert snapshot is not None and snapshot.error is None
    assert snapshot.counts.stack_count is None
    assert snapshot.counts.service_count == 3 and snapshot.counts.swarm_node_count == NODES


async def test_a_standalone_host_reads_no_services_for_its_stack_count(rig: Rig) -> None:
    engine = stocked_engine()
    engine.route("GET", "/containers/json", json_reply(project_rows("shop")))
    await rig.add("alpha", engine)

    await rig.service.refresh_one(rig.ids["alpha"])

    assert await stack_count_of(rig, "alpha") == 1
    assert engine.calls("GET", "/services") == []


async def test_a_worker_reads_no_services_either(rig: Rig) -> None:
    engine = stocked_engine(Swarm={"LocalNodeState": "active", "ControlAvailable": False})
    engine.route("GET", "/containers/json", json_reply(project_rows("shop")))
    await rig.add("alpha", engine)

    await rig.service.refresh_one(rig.ids["alpha"])

    assert await stack_count_of(rig, "alpha") == 1
    assert engine.calls("GET", "/services") == []


SWARM_STACK_DIGEST = "d" * 64


async def add_swarm_stack_row(rig: Rig, name: str, stack_name: str) -> None:
    async with rig.database.session() as db:
        await db.execute(
            "INSERT INTO stack (environment_id, name, kind, origin, project_directory, "
            "compose_file, compose_file_digest, created_at, updated_at) "
            "VALUES (?, ?, 'swarm', 'created', ?, 'services: {}', ?, ?, ?)",
            (
                rig.ids[name],
                stack_name,
                "a" * 32,
                SWARM_STACK_DIGEST,
                CREATED_STAMP,
                CREATED_STAMP,
            ),
        )


async def test_a_managed_swarm_stack_is_counted_once_through_its_namespace(rig: Rig) -> None:
    engine = manager_engine(namespaces=namespace_services("Shop"), projects=[])
    await rig.add("alpha", engine)
    await add_swarm_stack_row(rig, "alpha", "Shop")

    await rig.service.refresh_one(rig.ids["alpha"])

    assert await stack_count_of(rig, "alpha") == 1


async def test_a_managed_swarm_stack_with_no_objects_yet_is_not_a_stack_on_the_host(
    rig: Rig,
) -> None:
    engine = manager_engine(namespaces=namespace_services(), projects=[])
    await rig.add("alpha", engine)
    await add_swarm_stack_row(rig, "alpha", "Shop")

    await rig.service.refresh_one(rig.ids["alpha"])

    assert await stack_count_of(rig, "alpha") == 0


@dataclass(slots=True)
class GatedService:
    """A snapshot service whose refreshes wait until the test lets them through."""

    polled: list[int] = field(default_factory=list)
    let_through: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False

    async def refresh_one(self, environment_id: int) -> object:
        self.polled.append(environment_id)
        await self.let_through.wait()
        return None

    async def aclose(self) -> None:
        self.closed = True


class TestPokeCoalescing:
    async def test_pokes_made_while_a_refresh_runs_queue_exactly_one_more(self) -> None:
        service = GatedService()
        pokes = SnapshotPokes(service)
        pokes.poke(7)
        await asyncio.sleep(0)

        for _ in range(3):
            pokes.poke(7)
        during = list(service.polled)
        service.let_through.set()
        await pokes.wait_settled(7, 1)

        assert during == [7]
        assert service.polled == [7, 7]
        assert not pokes.is_pending(7)
        await pokes.aclose()

    async def test_environments_are_refreshed_independently(self) -> None:
        service = GatedService()
        pokes = SnapshotPokes(service)

        pokes.poke(1)
        pokes.poke(2)
        await asyncio.sleep(0)

        assert sorted(service.polled) == [1, 2]
        assert pokes.is_pending(1) and pokes.is_pending(2)
        service.let_through.set()
        await pokes.wait_settled(1, 1)
        await pokes.wait_settled(2, 1)
        await pokes.aclose()


class TestWaitSettled:
    async def test_a_wait_gives_up_after_its_time_and_reports_the_refresh_still_pending(
        self,
    ) -> None:
        service = GatedService()
        pokes = SnapshotPokes(service)
        pokes.poke(7)
        await asyncio.sleep(0)

        settled = await pokes.wait_settled(7, 0)

        assert settled is False
        assert pokes.is_pending(7)
        await pokes.aclose()

    async def test_a_wait_returns_true_when_the_running_refresh_finishes(self) -> None:
        service = GatedService()
        pokes = SnapshotPokes(service)
        pokes.poke(7)
        await asyncio.sleep(0)
        service.let_through.set()

        settled = await pokes.wait_settled(7, 1)

        assert settled is True
        await pokes.aclose()

    async def test_a_wait_on_an_environment_with_no_refresh_starts_none(self) -> None:
        service = GatedService()
        pokes = SnapshotPokes(service)

        settled = await pokes.wait_settled(9, 0)
        await asyncio.sleep(0)

        assert settled is True
        assert service.polled == []
        assert not pokes.is_pending(9)
        await pokes.aclose()


class GatedEngine(FakeEngine):
    """A fake Engine whose info replies wait for the test, one gate per request."""

    def __init__(self) -> None:
        super().__init__()
        self.route("GET", "/info", json_reply(info_document()))
        self.route("GET", "/volumes", json_reply({"Volumes": []}))
        self.route("GET", "/networks", json_reply([]))
        self.arrived: list[asyncio.Event] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["path"].endswith("/info"):
            gate = asyncio.Event()
            self.arrived.append(gate)
            await gate.wait()
        await super().__call__(scope, receive, send)


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never held")


async def test_a_refresh_requested_after_another_began_reading_the_host_waits_and_reads_again(
    rig: Rig,
) -> None:
    engine = GatedEngine()
    await rig.add("alpha", engine)
    environment_id = rig.ids["alpha"]
    first = asyncio.create_task(rig.service.refresh_one(environment_id))
    await until(lambda: len(engine.arrived) == 1)

    second = asyncio.create_task(rig.service.refresh_one(environment_id))
    for _ in range(20):
        await asyncio.sleep(0)
    waiting = (second.done(), len(engine.arrived))
    engine.arrived[0].set()
    await first
    await until(lambda: len(engine.arrived) == 2)
    engine.arrived[1].set()
    await second

    assert waiting == (False, 1)
    assert len(engine.calls("GET", "/info")) == 2


async def stored_environment(rig: Rig, name: str) -> Environment:
    async with rig.database.session() as db:
        found = await EnvironmentRepository(db, rig.clock).get_stored(rig.ids[name])
    assert found is not None
    return found


async def stored_snapshot(rig: Rig, name: str) -> EnvironmentSnapshot:
    async with rig.database.session() as db:
        found = await EnvironmentSnapshotRepository(db).get_stored(rig.ids[name])
    assert found is not None
    return found


async def refreshed_twice(rig: Rig, name: str, *, after: timedelta) -> tuple[str, str]:
    """Refresh once, move the clock on, refresh again; return the two check times."""
    await rig.service.refresh_one(rig.ids[name])
    first = format_timestamp(rig.clock.now)
    rig.clock.advance(after)
    await rig.service.refresh_one(rig.ids[name])
    return first, format_timestamp(rig.clock.now)


async def test_a_refresh_that_finds_nothing_new_writes_no_row_yet_readers_see_its_times(
    rig: Rig,
) -> None:
    await rig.add("alpha", stocked_engine())

    first, second = await refreshed_twice(rig, "alpha", after=ONE_MINUTE)

    stored = await stored_environment(rig, "alpha")
    snapshot = await stored_snapshot(rig, "alpha")
    environment = await rig.environment("alpha")
    held_snapshot = await rig.snapshot("alpha")
    async with rig.database.session() as db:
        dashboard = await EnvironmentDashboardRepository(db).get(rig.ids["alpha"])
    assert dashboard is not None and held_snapshot is not None
    assert (stored.last_checked_at, stored.last_contacted_at, snapshot.captured_at) == (
        first,
        first,
        first,
    )
    assert (environment.last_checked_at, environment.last_contacted_at) == (second, second)
    assert held_snapshot.captured_at == second
    assert (dashboard.last_checked_at, dashboard.snapshot_captured_at) == (second, second)


@pytest.mark.parametrize(
    ("after", "rewritten"),
    [
        pytest.param(STORED_TIME_REFRESH - ONE_SECOND, False, id="just-under-an-hour"),
        pytest.param(STORED_TIME_REFRESH, True, id="an-hour"),
    ],
)
async def test_stored_times_are_rewritten_once_they_are_an_hour_old(
    rig: Rig, after: timedelta, rewritten: bool
) -> None:
    await rig.add("alpha", stocked_engine())

    first, second = await refreshed_twice(rig, "alpha", after=after)

    expected = second if rewritten else first
    stored = await stored_environment(rig, "alpha")
    snapshot = await stored_snapshot(rig, "alpha")
    assert (stored.last_checked_at, snapshot.captured_at) == (expected, expected)


async def test_a_changed_status_is_written_at_once(rig: Rig) -> None:
    engine = stocked_engine()
    await rig.add("alpha", engine)
    await rig.service.refresh_one(rig.ids["alpha"])
    engine.route("GET", "/info", Reply(500))
    rig.clock.advance(ONE_MINUTE)

    await rig.service.refresh_one(rig.ids["alpha"])

    stored = await stored_environment(rig, "alpha")
    assert (stored.status, stored.last_checked_at) == (
        "unreachable",
        format_timestamp(rig.clock.now),
    )


async def test_a_changed_count_is_written_at_once(rig: Rig) -> None:
    engine = stocked_engine()
    await rig.add("alpha", engine)
    await rig.service.refresh_one(rig.ids["alpha"])
    engine.route("GET", "/info", json_reply(info_document(Containers=ANOTHER_CONTAINER)))
    rig.clock.advance(ONE_MINUTE)

    await rig.service.refresh_one(rig.ids["alpha"])

    snapshot = await stored_snapshot(rig, "alpha")
    assert (snapshot.counts.container_count, snapshot.captured_at) == (
        ANOTHER_CONTAINER,
        format_timestamp(rig.clock.now),
    )


async def test_the_first_refresh_after_a_start_writes_even_when_nothing_changed(
    make_rig: Callable[..., Rig],
) -> None:
    before_restart = make_rig()
    await before_restart.add("alpha", stocked_engine())
    await before_restart.service.refresh_one(before_restart.ids["alpha"])
    after_restart = make_rig()
    after_restart.engines.update(before_restart.engines)
    after_restart.ids.update(before_restart.ids)
    before_restart.clock.advance(ONE_MINUTE)

    await after_restart.service.refresh_one(after_restart.ids["alpha"])

    stored = await stored_environment(after_restart, "alpha")
    snapshot = await stored_snapshot(after_restart, "alpha")
    now = format_timestamp(after_restart.clock.now)
    assert (stored.last_checked_at, snapshot.captured_at) == (now, now)


HOST_NAME = "docker-host-1"
RENAMED_HOST = "docker-host-2"
MAX_HOST_NAME = 255


async def test_the_engine_host_name_is_stored_once_and_a_stable_name_adds_no_write(
    rig: Rig,
) -> None:
    await rig.add("alpha", stocked_engine(Name=HOST_NAME))

    first, _ = await refreshed_twice(rig, "alpha", after=ONE_MINUTE)

    stored = await stored_environment(rig, "alpha")
    assert (stored.engine_host_name, stored.last_checked_at) == (HOST_NAME, first)


async def test_a_changed_engine_host_name_is_written_at_once(rig: Rig) -> None:
    engine = stocked_engine(Name=HOST_NAME)
    await rig.add("alpha", engine)
    await rig.service.refresh_one(rig.ids["alpha"])
    engine.route("GET", "/info", json_reply(info_document(Name=RENAMED_HOST)))
    rig.clock.advance(ONE_MINUTE)

    await rig.service.refresh_one(rig.ids["alpha"])

    stored = await stored_environment(rig, "alpha")
    assert (stored.engine_host_name, stored.last_checked_at) == (
        RENAMED_HOST,
        format_timestamp(rig.clock.now),
    )


async def test_an_empty_engine_host_name_is_null_and_a_long_one_is_cut(rig: Rig) -> None:
    await rig.add("alpha", stocked_engine(Name=""))
    await rig.add("beta", stocked_engine(Name="h" * (MAX_HOST_NAME + 1)))

    await rig.service.refresh_all()

    assert (await stored_environment(rig, "alpha")).engine_host_name is None
    assert (await stored_environment(rig, "beta")).engine_host_name == "h" * MAX_HOST_NAME
