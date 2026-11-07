"""Object counts and status for each environment, refreshed by the scheduler and on request."""

import asyncio
import itertools
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.crypto.vault import Vault
from app.db.engine import Database, Session
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock, format_timestamp, parse_timestamp, utc_now
from app.db.repositories.environment import (
    Environment,
    EnvironmentRepository,
    StatusRecord,
)
from app.db.repositories.environment_snapshot import (
    EnvironmentSnapshot,
    EnvironmentSnapshotRepository,
    ObservedTimes,
    SnapshotCounts,
    observed_times,
)
from app.db.repositories.environment_tls import EnvironmentTlsRepository, StoredTls
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, ResponseTooLarge, VersionUnsupported
from app.docker_engine.probe import (
    ProbeStatus,
    ProbeTimeouts,
    Stage,
    environment_type_of,
    swarm_state_of,
)
from app.docker_engine.registry import EngineClientRegistry
from app.docker_engine.tls_validation import clean_text
from app.logging import get_logger
from app.notifications.catalogue import (
    ENVIRONMENT_REACHABLE_ACTION,
    ENVIRONMENT_UNREACHABLE_ACTION,
)
from app.services.activity import (
    ActivityRecorder,
    SubsystemActor,
    remove_environment_change_hook,
)
from app.services.environment_connector import (
    MATERIAL_UNAVAILABLE,
    EnvironmentConnector,
    MaterialUnavailable,
    ProbeCoordinator,
    connection_fingerprint,
    status_record_of,
    unavailable_result,
)

logger = get_logger(__name__)

REFRESH_CONCURRENCY = 8
REMOTE_REFRESH_CONCURRENCY = 4
HOST_BUDGET_SECONDS = 20.0
CLIENT_ATTEMPT_SECONDS = 8.0
FALLBACK_PROBE_SECONDS = 10.0
UNHEALTHY_COUNT_SECONDS = 4.0
STACK_COUNT_SECONDS = 4.0
ID_PAGE_SIZE = 200
MAX_SNAPSHOT_ERROR = 256
MAX_ENGINE_HOST_NAME = 255
STORED_TIME_REFRESH = timedelta(hours=1)

REFRESHER_SUBSYSTEM = "environment-snapshot-refresher"

_INT64_MAX = 2**63 - 1
_TIMED_OUT = "The host did not answer within the refresh time limit."
_COUNTS_UNAVAILABLE = "The host answered but its object counts could not be read."
_TOO_MANY_OBJECTS = "Too many objects to count."


def _no_compose_version() -> str | None:
    return None


class SnapshotPoker(Protocol):
    """Asks for an environment to be refreshed soon, without waiting for it."""

    def poke(self, environment_id: int) -> None:
        """Schedule a refresh; a poke during a running refresh queues exactly one more."""
        ...


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """What one refresh did; recorded is False when its observation was discarded.

    superseded is True when it was discarded because the connection changed while it ran.
    """

    environment_id: int
    status: str | None
    recorded: bool
    superseded: bool = False


@dataclass(frozen=True, slots=True)
class _Target:
    environment: Environment
    stored: StoredTls | None
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _Observation:
    record: StatusRecord
    counts: SnapshotCounts | None
    error: str | None


class EnvironmentSnapshotService:
    """Reads each host's status and counts into the database, never on a page load."""

    def __init__(
        self,
        *,
        database: Database,
        vault: Vault,
        registry: EngineClientRegistry,
        probes: ProbeCoordinator,
        clock: Clock = utc_now,
        concurrency: int = REFRESH_CONCURRENCY,
        remote_concurrency: int = REMOTE_REFRESH_CONCURRENCY,
        host_budget: float = HOST_BUDGET_SECONDS,
        compose_version: Callable[[], str | None] = _no_compose_version,
    ) -> None:
        self._database = database
        self._vault = vault
        self._registry = registry
        self._probes = probes
        self._clock = clock
        self._host_budget = host_budget
        self._compose_version = compose_version
        self._slots = asyncio.Semaphore(concurrency)
        self._remote_slots = asyncio.Semaphore(remote_concurrency)
        self._flights: dict[int, asyncio.Task[RefreshResult]] = {}
        self._reading_since: dict[int, int] = {}
        self._sequence = itertools.count()
        self._written: set[int] = set()

    async def refresh_one(
        self, environment_id: int, *, now: datetime | None = None
    ) -> RefreshResult:
        """Refresh one environment from a request, so the host is read after the call began.

        A refresh that had begun reading the host when the call was made is waited for, not
        joined: it may have read the host before the change that prompted this call. A refresh
        that has not yet begun reading is shared.
        """
        call = next(self._sequence)
        while (running := self._flights.get(environment_id)) is not None:
            if self._reading_since.get(environment_id, call + 1) > call:
                break
            await asyncio.wait({running})
        return await self._flight(environment_id, now)

    async def refresh_all(self, now: datetime | None = None) -> int:
        """Refresh every environment, a few at a time; return how many were attempted.

        Each host is stamped with the clock when its turn starts, unless `now` is given.
        """
        attempted = 0
        after_id = 0
        while True:
            ids = await self._next_ids(after_id)
            if not ids:
                return attempted
            outcomes = await asyncio.gather(
                *(self._flight(environment_id, now) for environment_id in ids),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
            attempted += len(ids)
            after_id = ids[-1]

    async def aclose(self) -> None:
        """Cancel refreshes still running; call once at shutdown."""
        flights = list(self._flights.values())
        for flight in flights:
            flight.cancel()
        await asyncio.gather(*flights, return_exceptions=True)
        self._flights.clear()
        self._reading_since.clear()

    async def _next_ids(self, after_id: int) -> list[int]:
        async with self._database.session() as db:
            return await EnvironmentRepository(db, self._clock).list_ids_page(
                limit=ID_PAGE_SIZE, after_id=after_id
            )

    async def _flight(self, environment_id: int, now: datetime | None) -> RefreshResult:
        result = await self._shared_flight(environment_id, now)
        if result.superseded:
            result = await self._shared_flight(environment_id, now)
        return result

    async def _shared_flight(self, environment_id: int, now: datetime | None) -> RefreshResult:
        flight = self._flights.get(environment_id)
        if flight is None:
            flight = asyncio.create_task(self._run_one(environment_id, now))
            self._flights[environment_id] = flight
            flight.add_done_callback(lambda done: self._forget(environment_id, done))
        return await asyncio.shield(flight)

    def _forget(self, environment_id: int, done: asyncio.Task[RefreshResult]) -> None:
        if self._flights.get(environment_id) is done:
            del self._flights[environment_id]
            self._reading_since.pop(environment_id, None)

    async def _run_one(self, environment_id: int, now: datetime | None) -> RefreshResult:
        async with self._slots:
            try:
                async with self._database.session() as db:
                    target = await self._load(db, environment_id)
                    if target is None:
                        return RefreshResult(environment_id, None, False)
                    async with self._remote_slot(target):
                        self._reading_since[environment_id] = next(self._sequence)
                        moment = now or self._clock()
                        try:
                            async with asyncio.timeout(self._host_budget):
                                return await self._refresh(db, target, moment)
                        except TimeoutError:
                            return await self._record_failure(
                                db, target, moment, Stage.ENGINE, _TIMED_OUT
                            )
            except Exception as error:
                logger.error(
                    "snapshot_refresh_failed",
                    extra={"environment_id": environment_id, "error": type(error).__name__},
                )
                return RefreshResult(environment_id, None, False)

    @asynccontextmanager
    async def _remote_slot(self, target: _Target) -> AsyncIterator[None]:
        if target.environment.connection_kind != "tcp_tls":
            yield
            return
        async with self._remote_slots:
            yield

    async def _load(self, db: Session, environment_id: int) -> _Target | None:
        environment = await EnvironmentRepository(db, self._clock).get(environment_id)
        if environment is None:
            return None
        stored = await EnvironmentTlsRepository(db, self._vault, self._clock).get(environment_id)
        return _Target(environment, stored, connection_fingerprint(environment, stored))

    async def _refresh(self, db: Session, target: _Target, moment: datetime) -> RefreshResult:
        checked_at = format_timestamp(moment)
        connector = EnvironmentConnector(
            tls=EnvironmentTlsRepository(db, self._vault, self._clock),
            registry=self._registry,
            probes=self._probes,
        )
        try:
            async with asyncio.timeout(CLIENT_ATTEMPT_SECONDS):
                observation = await self._read(connector, target, checked_at)
        except MaterialUnavailable:
            return await self._record_failure(
                db, target, moment, Stage.TLS_HANDSHAKE, MATERIAL_UNAVAILABLE
            )
        except Exception as error:
            logger.debug(
                "snapshot_read_failed",
                extra={"environment_id": target.environment.id, "error": type(error).__name__},
            )
            if isinstance(error, VersionUnsupported):
                await self._registry.invalidate(target.environment.id)
            return await self._classify(db, connector, target, checked_at)
        return await self._store(
            db, target, observation.record, observation.counts, observation.error
        )

    async def _read(
        self, connector: EnvironmentConnector, target: _Target, checked_at: str
    ) -> _Observation:
        client = await connector.client(target.environment, target.stored)
        info = await client.system.info()
        reported = clean_text(info.server_version)
        daemon = client.daemon
        if daemon is None or (reported and reported != clean_text(daemon.engine_version)):
            daemon = await client.negotiate()
        state = swarm_state_of(info.swarm.local_node_state)
        control = info.swarm.control_available if state is not None else None
        record = StatusRecord(
            status="reachable",
            failed_stage=None,
            message=None,
            checked_at=checked_at,
            contacted_at=checked_at,
            engine_version=reported or clean_text(daemon.engine_version) or None,
            api_version=clean_text(daemon.daemon_api_version),
            type=environment_type_of(state, control),
            swarm_state=state,
            swarm_control_available=control,
            compose_cli_version=self._compose_version(),
            engine_host_name=clean_text(info.name, MAX_ENGINE_HOST_NAME) or None,
        )
        manager = state == "active" and bool(control)
        unhealthy_count = asyncio.create_task(_unhealthy_count(client))
        stack_count = asyncio.create_task(_stack_count(client, manager=manager))
        try:
            try:
                volumes, networks, *swarm = await _counted(
                    client.volumes.count(),
                    client.networks.count(),
                    *(
                        (client.swarm.count_services(), client.swarm.count_nodes())
                        if manager
                        else ()
                    ),
                )
            except ResponseTooLarge:
                return _Observation(record, None, _TOO_MANY_OBJECTS)
            unhealthy = await unhealthy_count
            stacks = await stack_count
        finally:
            unhealthy_count.cancel()
            stack_count.cancel()
        counts = SnapshotCounts(
            container_count=_clamped(info.containers),
            container_running_count=_clamped(info.containers_running),
            container_stopped_count=_clamped(info.containers_stopped),
            container_paused_count=_clamped(info.containers_paused),
            container_unhealthy_count=unhealthy,
            image_count=_clamped(info.images),
            volume_count=volumes,
            network_count=networks,
            stack_count=stacks,
            service_count=swarm[0] if swarm else None,
            swarm_node_count=swarm[1] if swarm else None,
        )
        return _Observation(record, counts, None)

    async def _classify(
        self, db: Session, connector: EnvironmentConnector, target: _Target, checked_at: str
    ) -> RefreshResult:
        result = await connector.test_stored(
            target.environment,
            target.stored,
            timeouts=ProbeTimeouts(total=FALLBACK_PROBE_SECONDS),
        )
        record = status_record_of(result, checked_at)
        error = (
            _COUNTS_UNAVAILABLE
            if result.status is ProbeStatus.REACHABLE
            else clean_text(result.message, MAX_SNAPSHOT_ERROR)
        )
        return await self._store(db, target, record, None, error)

    async def _record_failure(
        self, db: Session, target: _Target, moment: datetime, stage: Stage, message: str
    ) -> RefreshResult:
        record = status_record_of(unavailable_result(stage, message), format_timestamp(moment))
        return await self._store(db, target, record, None, message)

    async def _store(
        self,
        db: Session,
        target: _Target,
        record: StatusRecord,
        counts: SnapshotCounts | None,
        error: str | None,
    ) -> RefreshResult:
        environment_id = target.environment.id
        environments = EnvironmentRepository(db, self._clock)
        tls = EnvironmentTlsRepository(db, self._vault, self._clock)
        snapshots = EnvironmentSnapshotRepository(db)
        failure = error or _COUNTS_UNAVAILABLE
        stored_error = None if counts is not None else failure
        stored_counts = counts if counts is not None else SnapshotCounts()
        previous = await environments.get_stored(environment_id)
        if previous is None:
            return RefreshResult(environment_id, None, False)
        snapshot = await snapshots.get_stored(environment_id)
        fingerprint = connection_fingerprint(previous, await tls.get(environment_id))
        observed = observed_times(db)
        if (
            environment_id in self._written
            and fingerprint == target.fingerprint
            and _nothing_to_store(previous, snapshot, record, stored_counts, stored_error)
        ):
            observed.hold(
                environment_id,
                ObservedTimes(
                    checked_at=record.checked_at,
                    contacted_at=record.contacted_at
                    or observed.contacted_at(environment_id, previous.last_contacted_at),
                    captured_at=record.checked_at,
                ),
            )
            return RefreshResult(environment_id, record.status, True)
        async with db.transaction():
            current = await environments.get_stored(environment_id)
            if current is None:
                return RefreshResult(environment_id, None, False)
            if connection_fingerprint(current, await tls.get(environment_id)) != target.fingerprint:
                return RefreshResult(environment_id, current.status, False, superseded=True)
            await environments.record_status(environment_id, record)
            await _record_transition(db, self._clock, environment_id, current.status, record)
            if counts is not None:
                await snapshots.record_counts(environment_id, record.checked_at, counts)
            else:
                await snapshots.record_error(environment_id, record.checked_at, failure)
        self._written.add(environment_id)
        if current.status != record.status:
            logger.info(
                "environment_status_changed",
                extra={
                    "environment_id": environment_id,
                    "previous_status": current.status,
                    "status": record.status,
                    "failed_stage": record.failed_stage,
                },
            )
        return RefreshResult(environment_id, record.status, True)


async def _record_transition(
    db: Session, clock: Clock, environment_id: int, previous: str, record: StatusRecord
) -> None:
    match previous, record.status:
        case "reachable", "unreachable":
            action = ENVIRONMENT_UNREACHABLE_ACTION
        case "unreachable", "reachable":
            action = ENVIRONMENT_REACHABLE_ACTION
        case _:
            return
    detail: dict[str, DetailValue] = {"previous_status": previous}
    if record.failed_stage is not None:
        detail["failed_stage"] = record.failed_stage
    await ActivityRecorder(db, clock).record_event(
        SubsystemActor(REFRESHER_SUBSYSTEM, None),
        action=action,
        target_type="environment",
        target_id=str(environment_id),
        environment_id=environment_id,
        result="success",
        detail=detail,
    )


def _nothing_to_store(
    environment: Environment,
    snapshot: EnvironmentSnapshot | None,
    record: StatusRecord,
    counts: SnapshotCounts,
    error: str | None,
) -> bool:
    if snapshot is None or environment.last_checked_at is None:
        return False
    checked = parse_timestamp(record.checked_at)
    if checked - parse_timestamp(environment.last_checked_at) >= STORED_TIME_REFRESH:
        return False
    if checked - parse_timestamp(snapshot.captured_at) >= STORED_TIME_REFRESH:
        return False
    return (
        snapshot.counts == counts
        and snapshot.error == error
        and environment.status == record.status
        and environment.failed_stage == record.failed_stage
        and environment.status_message == record.message
        and environment.engine_version == _kept(record.engine_version, environment.engine_version)
        and environment.api_version == _kept(record.api_version, environment.api_version)
        and environment.type == _kept(record.type, environment.type)
        and environment.swarm_state == _kept(record.swarm_state, environment.swarm_state)
        and environment.swarm_control_available
        == _kept(record.swarm_control_available, environment.swarm_control_available)
        and environment.compose_cli_version
        == _kept(record.compose_cli_version, environment.compose_cli_version)
        and environment.engine_host_name
        == _kept(record.engine_host_name, environment.engine_host_name)
    )


def _kept[T](new: T | None, old: T | None) -> T | None:
    return old if new is None else new


def _clamped(value: int) -> int:
    return max(0, min(value, _INT64_MAX))


async def _unhealthy_count(client: EngineClient) -> int | None:
    try:
        async with asyncio.timeout(UNHEALTHY_COUNT_SECONDS):
            return await client.containers.count(all=True, filters={"health": ["unhealthy"]})
    except (EngineError, TimeoutError) as error:
        logger.debug(
            "snapshot_unhealthy_count_failed",
            extra={"environment_id": client.environment_id, "error": type(error).__name__},
        )
        return None


async def _stack_count(client: EngineClient, *, manager: bool) -> int | None:
    try:
        async with asyncio.timeout(STACK_COUNT_SECONDS):
            reads: list[Awaitable[frozenset[str]]] = [client.containers.compose_projects()]
            if manager:
                reads.append(client.swarm.stack_namespaces())
            names: set[str] = set()
            for outcome in await asyncio.gather(*reads, return_exceptions=True):
                if isinstance(outcome, BaseException):
                    raise outcome
                names |= outcome
            return len(names)
    except (EngineError, TimeoutError) as error:
        logger.debug(
            "snapshot_stack_count_failed",
            extra={"environment_id": client.environment_id, "error": type(error).__name__},
        )
        return None


async def _counted(*calls: Awaitable[int]) -> list[int]:
    outcomes = await asyncio.gather(*calls, return_exceptions=True)
    failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    if failures:
        raise next((f for f in failures if not isinstance(f, ResponseTooLarge)), failures[0])
    return [outcome for outcome in outcomes if isinstance(outcome, int)]


class SnapshotRefresher(Protocol):
    """What the refresh queue needs from the service it drives."""

    async def refresh_one(self, environment_id: int) -> object: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class _Drain:
    task: asyncio.Task[None]
    settled: asyncio.Event


class SnapshotPokes:
    """Refreshes one environment on request, in the background, and nothing else.

    Each environment has at most one run of refreshes at a time. A poke while one is running marks
    the environment dirty, and the running refresh's completion starts exactly one more.
    """

    def __init__(self, service: SnapshotRefresher) -> None:
        self._service = service
        self._drains: dict[int, _Drain] = {}
        self._dirty: set[int] = set()
        self._closed = False

    def poke(self, environment_id: int) -> None:
        """Refresh one environment soon; a poke while one is running queues exactly one more."""
        if self._closed:
            return
        if environment_id in self._drains:
            self._dirty |= {environment_id}
            return
        settled = asyncio.Event()
        task = asyncio.get_running_loop().create_task(self._drain(environment_id))
        drain = _Drain(task, settled)
        self._drains[environment_id] = drain
        task.add_done_callback(lambda _: self._release(environment_id, drain))

    def is_pending(self, environment_id: int) -> bool:
        """Report whether a refresh is running or queued for the environment."""
        return environment_id in self._drains

    async def wait_settled(self, environment_id: int, seconds: float) -> bool:
        """Wait until no refresh is running or queued; False if `seconds` pass first.

        Never starts a refresh.
        """
        drain = self._drains.get(environment_id)
        if drain is None:
            return True
        try:
            async with asyncio.timeout(seconds):
                await drain.settled.wait()
        except TimeoutError:
            return False
        return True

    async def aclose(self) -> None:
        """Stop every refresh requested and the service's own, leaving no task running."""
        self._closed = True
        remove_environment_change_hook(self.poke)
        tasks = [drain.task for drain in self._drains.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._service.aclose()

    async def _drain(self, environment_id: int) -> None:
        try:
            while True:
                self._dirty.discard(environment_id)
                await self._refresh(environment_id)
                if environment_id not in self._dirty:
                    break
        finally:
            self._release(environment_id, self._drains.get(environment_id))

    def _release(self, environment_id: int, drain: _Drain | None) -> None:
        if drain is None or self._drains.get(environment_id) is not drain:
            return
        del self._drains[environment_id]
        self._dirty.discard(environment_id)
        drain.settled.set()

    async def _refresh(self, environment_id: int) -> None:
        try:
            await self._service.refresh_one(environment_id)
        except Exception as error:
            logger.error(
                "snapshot_poke_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
