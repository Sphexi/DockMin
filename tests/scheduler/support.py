"""Fakes and builders for the scheduler tests: clock, sleeper, handler and a seeded database."""

import asyncio
import inspect
import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from app.config import Settings
from app.db.engine import Database
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.job_run import JobRun, JobRunRepository
from app.db.repositories.scheduled_job import ScheduledJob, ScheduledJobRepository
from app.scheduler.loop import Scheduler
from app.scheduler.model import Dispatch, RunFailed, RunResult
from app.scheduler.registry import HandlerRegistry
from app.scheduler.setup import build_scheduler_runtime
from app.services.object_locks import ObjectLockRegistry
from app.subsystems.base import RuleParameters, Subsystem, SubsystemDeps
from tests.auth_support import FakeClock

START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
RESTARTER = "stack-restarter"
REFRESHER = "environment-snapshot-refresher"
UPDATER = "container-updater"
WEBHOOK = "webhook-receiver"
DEADLINE_SECONDS = 20.0
RUN_POLL_SECONDS = 0.005
SEEDED_AT = "2026-01-01T00:00:00Z"
NEW_OWNER: Literal["new"] = "new"

type Behaviour = Callable[[Dispatch], Awaitable[RunResult]]
type Owner = Literal["new"] | int | None


def at(seconds: float) -> datetime:
    """Return the moment a number of seconds after the fixed start."""
    return START + timedelta(seconds=seconds)


def stamp(moment: datetime) -> str:
    """Render a moment the way the database stores it."""
    return format_timestamp(moment)


def detail_of(row: sqlite3.Row) -> dict[str, Any]:
    """Decode a row's detail column."""
    return json.loads(row["detail"]) if row["detail"] else {}


class Clock(FakeClock):
    """A fake clock that can also be set to a moment or moved by seconds."""

    def __init__(self, start: datetime = START) -> None:
        super().__init__(start)

    def set(self, moment: datetime) -> None:
        self.now = moment

    def jump(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class BlockingSleeper:
    """Stands in for the loop's sleep: records each deadline and waits only for a wake-up."""

    def __init__(self) -> None:
        self.deadlines: list[datetime | None] = []
        self.entered = asyncio.Event()

    async def sleep_until(self, deadline: datetime | None, wake: asyncio.Event) -> None:
        self.deadlines.append(deadline)
        self.entered.set()
        await wake.wait()


class ScriptedHandler:
    """A job handler whose behaviour a test replaces; it records every dispatch it receives."""

    def __init__(
        self,
        name: str = RESTARTER,
        *,
        records_events: bool = True,
        cancellation_safe: bool = False,
        run_timeout: timedelta = timedelta(minutes=5),
        supports_dry_run: bool = True,
        max_concurrent_runs: int | None = None,
    ) -> None:
        self.name = name
        self.max_concurrent_runs = max_concurrent_runs
        self.records_events = records_events
        self.cancellation_safe = cancellation_safe
        self.run_timeout = run_timeout
        self.supports_dry_run = supports_dry_run
        self.calls: list[Dispatch] = []
        self.behaviour: Behaviour = succeeds()
        self._entered = asyncio.Condition()

    async def dispatch(self, dispatch: Dispatch) -> RunResult:
        self.calls.append(dispatch)
        async with self._entered:
            self._entered.notify_all()
        return await self.behaviour(dispatch)

    async def entered(self, count: int) -> None:
        """Wait until the handler has been dispatched at least count times."""
        async with self._entered:
            await self._entered.wait_for(lambda: len(self.calls) >= count)


def succeeds(attempted: int = 1) -> Behaviour:
    async def behave(dispatch: Dispatch) -> RunResult:
        return RunResult("success", attempted=attempted)

    return behave


def fails(reason: str = "host_down", message: str = "The host did not answer.") -> Behaviour:
    async def behave(dispatch: Dispatch) -> RunResult:
        raise RunFailed(reason, message)

    return behave


def raises(error: BaseException) -> Behaviour:
    async def behave(dispatch: Dispatch) -> RunResult:
        raise error

    return behave


def blocks(gate: asyncio.Event) -> Behaviour:
    """Wait for the gate, then succeed."""

    async def behave(dispatch: Dispatch) -> RunResult:
        await gate.wait()
        return RunResult("success", attempted=1)

    return behave


def hangs() -> Behaviour:
    """Wait until cancelled."""
    return blocks(asyncio.Event())


class Rig:
    """A migrated database with a fake clock, and builders for the rows a scheduler works on."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.clock = Clock()
        self.sleeper = BlockingSleeper()
        self._schedulers: list[Scheduler] = []

    def sql(self, statement: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        connection = self.database.connect()
        try:
            rows = connection.execute(statement, params).fetchall()
            connection.commit()
            return rows
        finally:
            connection.close()

    def user(self, name: str, role: str = "administrator", *, enabled: bool = True) -> int:
        self.sql(
            "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
            "VALUES (?, 'x', ?, ?, ?)",
            (name, role, int(enabled), SEEDED_AT),
        )
        return int(self.sql("SELECT id FROM user WHERE username = ?", (name,))[0]["id"])

    def environment(self, name: str = "host", *, kind: str = "tcp_tls") -> int:
        endpoint = "socket_path" if kind == "unix_socket" else "url"
        value = "/run/docker.sock" if kind == "unix_socket" else f"tcp://{name}:2376"
        return int(
            self.sql(
                f"INSERT INTO environment (name, connection_kind, {endpoint}, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
                (name, kind, value, SEEDED_AT, SEEDED_AT),
            )[0]["id"]
        )

    async def rule(
        self,
        name: str,
        *,
        owner: Owner = NEW_OWNER,
        subsystem: str = RESTARTER,
        parameters: str = "{}",
        environment_id: int | None = None,
        enabled: bool = True,
    ) -> int:
        """Insert a rule; each rule gets an owner of its own unless one is given."""
        if owner is None:
            self.sql(
                "INSERT INTO automation_rule (name, subsystem, environment_id, target_selector, "
                "parameters, is_enabled, created_by_user_id, created_at, updated_at) "
                "VALUES (?, ?, ?, '', ?, ?, NULL, ?, ?)",
                (name, subsystem, environment_id, parameters, int(enabled), SEEDED_AT, SEEDED_AT),
            )
            return int(self.sql("SELECT id FROM automation_rule WHERE name = ?", (name,))[0]["id"])
        owner_id = self.user(f"owner-{name}") if owner == NEW_OWNER else owner
        async with self.database.session() as db:
            rule = await AutomationRuleRepository(db, self.clock).insert(
                name=name,
                subsystem=subsystem,
                environment_id=environment_id,
                target_selector="",
                parameters=parameters,
                is_enabled=enabled,
                created_by_user_id=int(owner_id),
            )
        return rule.id

    async def job(
        self,
        *,
        subsystem: str = RESTARTER,
        rule_id: int | None = None,
        kind: str = "interval",
        expression: str = "60",
        next_run: datetime | None = None,
        policy: str = "skip",
        catch_up: bool = False,
        threshold: int | None = 5,
        environment_id: int | None = None,
    ) -> int:
        async with self.database.session() as db:
            job = await ScheduledJobRepository(db, self.clock).insert(
                subsystem=subsystem,
                rule_id=rule_id,
                environment_id=environment_id,
                schedule_kind=kind,  # type: ignore[arg-type]
                schedule_expression=expression,
                time_zone="UTC",
                overrun_policy=policy,  # type: ignore[arg-type]
                catch_up=catch_up,
                failure_threshold=threshold,
                next_run_at=None if next_run is None else stamp(next_run),
            )
        return job.id

    async def webhook_job(self, name: str, *, armed: datetime | None = None) -> int:
        """Insert a webhook-receiver rule with its on-call job, armed for the given time or not."""
        rule_id = await self.rule(name, subsystem=WEBHOOK)
        return await self.job(
            subsystem=WEBHOOK,
            rule_id=rule_id,
            kind="webhook",
            expression="",
            next_run=armed,
        )

    async def arm(self, job_id: int, not_before: datetime | None = None) -> bool:
        """Arm an on-call job as a call would, and report whether this call armed it."""
        async with self.database.session() as db, db.transaction():
            return await ScheduledJobRepository(db, self.clock).arm(
                job_id, stamp(not_before or self.clock())
            )

    async def rule_job(self, name: str, **options: Any) -> int:
        """Insert a rule with a new owner and its job, and return the job's id."""
        rule_id = await self.rule(name)
        return await self.job(rule_id=rule_id, **options)

    def bulk_rule_jobs(
        self, count: int, *, next_run: datetime, expression: str = "60", prefix: str = "bulk"
    ) -> list[int]:
        """Insert count rules, each with an owner and a job due at next_run; return the job ids."""
        due = stamp(next_run)
        connection = self.database.connect()
        try:
            connection.execute("BEGIN")
            job_ids: list[int] = []
            for number in range(count):
                name = f"{prefix}-{number}"
                user_id = connection.execute(
                    "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
                    "VALUES (?, 'x', 'administrator', 1, ?) RETURNING id",
                    (name, SEEDED_AT),
                ).fetchone()["id"]
                rule_id = connection.execute(
                    "INSERT INTO automation_rule (name, subsystem, target_selector, parameters, "
                    "is_enabled, created_by_user_id, created_at, updated_at) "
                    "VALUES (?, ?, '', '{}', 1, ?, ?, ?) RETURNING id",
                    (name, RESTARTER, user_id, SEEDED_AT, SEEDED_AT),
                ).fetchone()["id"]
                job_ids.append(
                    connection.execute(
                        "INSERT INTO scheduled_job (subsystem, rule_id, schedule_kind, "
                        "schedule_expression, next_run_at, failure_threshold, created_at, "
                        "updated_at) VALUES (?, ?, 'interval', ?, ?, 5, ?, ?) RETURNING id",
                        (RESTARTER, rule_id, expression, due, SEEDED_AT, SEEDED_AT),
                    ).fetchone()["id"]
                )
            connection.execute("COMMIT")
            return job_ids
        finally:
            connection.close()

    async def refresher_job(self, **options: Any) -> int:
        options.setdefault("kind", "interval")
        options.setdefault("expression", "15")
        options.setdefault("threshold", None)
        return await self.job(subsystem=REFRESHER, rule_id=None, **options)

    async def job_row(self, job_id: int) -> ScheduledJob:
        async with self.database.session() as db:
            found = await ScheduledJobRepository(db, self.clock).get(job_id)
        assert found is not None
        return found

    def runs(self, job_id: int) -> list[sqlite3.Row]:
        return self.sql("SELECT * FROM job_run WHERE scheduled_job_id = ? ORDER BY id", (job_id,))

    def outcomes(self, job_id: int) -> list[tuple[str | None, str | None]]:
        return [(row["outcome"], row["reason"]) for row in self.runs(job_id)]

    async def loop_passes(self, count: int) -> None:
        """Wait until the started loop has gone to sleep at least count times."""
        while len(self.sleeper.deadlines) < count:  # noqa: ASYNC110
            await asyncio.sleep(0)

    async def settled(self, run_id: int) -> None:
        """Wait until the run row carries an outcome."""
        while self.sql("SELECT outcome FROM job_run WHERE id = ?", (run_id,))[0]["outcome"] is None:  # noqa: ASYNC110
            await asyncio.sleep(RUN_POLL_SECONDS)

    def events(self, action: str | None = None) -> list[sqlite3.Row]:
        if action is None:
            return self.sql("SELECT * FROM activity_event ORDER BY id")
        return self.sql("SELECT * FROM activity_event WHERE action = ? ORDER BY id", (action,))

    def setting(self, key: str, value: str) -> None:
        self.sql(
            "INSERT INTO setting (key, value_type, value, updated_at) VALUES (?, 'boolean', ?, ?)",
            (key, value, SEEDED_AT),
        )

    async def start_run(self, job_id: int, trigger: str = "schedule") -> JobRun:
        async with self.database.session() as db:
            return await JobRunRepository(db, self.clock).start(
                job_id,
                trigger=trigger,  # type: ignore[arg-type]
                scheduled_for=None,
            )

    def scheduler(self, *handlers: Any, **options: Any) -> Scheduler:
        """Build a scheduler over the given handlers with the fake clock, sleeper and jitter."""
        registry = HandlerRegistry()
        for handler in handlers:
            registry.register(handler)
        options.setdefault("rng", lambda: 0.0)
        options.setdefault("sleeper", self.sleeper)
        scheduler = Scheduler(self.database, registry, clock=self.clock, **options)
        self._schedulers.append(scheduler)
        return scheduler

    def deps(
        self, settings: Settings, locks: ObjectLockRegistry | None = None
    ) -> tuple[SubsystemDeps, ObjectLockRegistry]:
        """Build subsystem dependencies over this database with no Engine behind them."""
        registry = ObjectLockRegistry(self.clock) if locks is None else locks
        runtime = build_scheduler_runtime(
            database=self.database, settings=settings, clock=self.clock, locks=registry
        )
        engine = SimpleNamespace(
            locks=registry,
            refresher=SimpleNamespace(poke=lambda _id: None),
            snapshots=None,
            vault=None,
            registry=None,
            probes=None,
        )
        deps = SubsystemDeps(
            database=self.database,
            engine=engine,  # type: ignore[arg-type]
            auth=SimpleNamespace(clock=self.clock),  # type: ignore[arg-type]
            settings=settings,
            suppressions=runtime.suppressions,
        )
        return deps, registry

    async def close(self) -> None:
        for scheduler in self._schedulers:
            await scheduler.aclose()
        await self.database.aclose()


class Parameters(RuleParameters):
    """The parameters of the scripted subsystems."""


def scripted_subsystem(
    body: Callable[..., Awaitable[RunResult]],
    *,
    name: str = RESTARTER,
    authorise: Callable[..., Awaitable[None]] | None = None,
    **attributes: Any,
) -> type[Subsystem]:
    """Build a Subsystem class whose run is body(self, context); rule-bearing ones allow all."""

    async def allow(
        self: Subsystem, principal: Any, environment_id: Any, selector: Any, authoriser: Any
    ) -> None:
        return None

    namespace: dict[str, Any] = {
        "name": name,
        "parameters_model": None if name == REFRESHER else Parameters,
        "run": body,
    }
    if name != REFRESHER:
        namespace["authorise_target"] = authorise or allow
    namespace.update(attributes)
    return type("Scripted", (Subsystem,), namespace)


async def dispatch_of(
    rig: Rig,
    subsystem: Subsystem,
    job_id: int,
    *,
    trigger: str = "schedule",
    dry_run: bool = False,
) -> RunResult:
    """Insert a run for the job and hand it to the subsystem as the scheduler would."""
    async with rig.database.session() as db:
        job = await ScheduledJobRepository(db, rig.clock).get(job_id)
        run = await JobRunRepository(db, rig.clock).start(
            job_id,
            trigger=trigger,  # type: ignore[arg-type]
            scheduled_for=None,
        )
    assert job is not None
    return await subsystem.dispatch(Dispatch(job, run, rig.clock(), dry_run))


def bound_async_tests(items: Iterable[pytest.Item], directory: Path) -> None:
    """Wrap every async test under the directory so it fails instead of hanging."""
    for item in items:
        function = getattr(item, "obj", None)
        if (
            function is not None
            and inspect.iscoroutinefunction(function)
            and directory in item.path.parents
            and not getattr(function, "_bounded", False)
        ):
            item.obj = _bounded(function)  # type: ignore[attr-defined]


def _bounded(function: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    async def run(*args: Any, **kwargs: Any) -> None:
        async with asyncio.timeout(DEADLINE_SECONDS):
            await function(*args, **kwargs)

    run._bounded = True  # type: ignore[attr-defined]
    return run
