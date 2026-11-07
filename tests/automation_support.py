"""Builders for the automation tests: a run context over the scheduler rig, and event readers."""

import json
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

from app.auth.errors import PermissionDenied
from app.config import Settings
from app.db.engine import Database, Session
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.scheduler.model import Dispatch, RunResult
from app.scheduler.suppression import SuppressionRegistry
from app.services.access import ResourceKey
from app.services.activity import ActivityRecorder, SubsystemActor
from app.services.object_locks import ObjectKey, ObjectLockRegistry
from app.services.resource_controls import ControlSpec
from app.subsystems.base import OwnerGate, RunContext, Subsystem, SubsystemDeps
from tests.access_support import ADMIN, ENV_A, container_id, digest_of
from tests.auth_support import ADMIN_ID
from tests.scheduler.support import RESTARTER, Rig, dispatch_of

HEALTH = "container-health-checker"
STACK_RESTARTER = "stack-restarter"
RULE_NAME = "Heal web"
LOCK_WAIT = 0.2
INTERVAL_SECONDS = "3600"
DAY = timedelta(hours=24)


class Gate(OwnerGate):
    """An owner gate that records what it was asked and refuses what the test says to."""

    def __init__(self, allow: Callable[[ObjectKey], bool] | None = None) -> None:
        async def current() -> bool:
            return True

        super().__init__(ADMIN, None, current)
        self.checked: list[ObjectKey] = []
        self._allow = allow

    async def authorise(self, key: ObjectKey) -> None:
        self.checked.append(key)
        if self._allow is not None and not self._allow(key):
            raise PermissionDenied("insufficient access")


async def run_context(
    rig: Rig,
    db: Session,
    *,
    subsystem: str = RESTARTER,
    gate: OwnerGate | None = None,
    suppressions: SuppressionRegistry | None = None,
    locks: ObjectLockRegistry | None = None,
    dry_run: bool = False,
    window: timedelta = timedelta(minutes=5),
) -> RunContext:
    """Insert a rule, its job and a run, and return the context a subsystem run would receive."""
    rule_id = await rig.rule(RULE_NAME, subsystem=subsystem, environment_id=rig.environment())
    job_id = await rig.job(subsystem=subsystem, rule_id=rule_id)
    run = await rig.start_run(job_id, "dry_run" if dry_run else "schedule")
    job = await rig.job_row(job_id)
    clock = rig.clock
    return RunContext(
        dispatch=Dispatch(job=job, run=run, now=clock(), dry_run=dry_run),
        rule=None,
        parameters=None,
        actor=SubsystemActor(subsystem, rule_id, run.id, RULE_NAME),
        db=db,
        activity=ActivityRecorder(db, clock),
        now=clock(),
        dry_run=dry_run,
        locks=ObjectLockRegistry(clock) if locks is None else locks,
        suppressions=SuppressionRegistry(clock) if suppressions is None else suppressions,
        clock=clock,
        lock_wait=LOCK_WAIT,
        suppression_window=window,
        owner_gate=Gate() if gate is None else gate,
    )


def detail_of(row: dict[str, Any]) -> dict[str, Any]:
    """Decode the detail column of an activity row read as a dictionary."""
    return json.loads(row["detail"]) if row["detail"] else {}


def cid(number: int) -> str:
    """Return the id of the numbered container on the fake daemon."""
    return container_id(number)


class Subject:
    """One rule-bearing subsystem run against a fake daemon, as the scheduler would run it."""

    def __init__(
        self, rig: Any, settings: Settings, factory: Callable[[SubsystemDeps], Subsystem]
    ) -> None:
        self.rig = rig
        self.host = rig.host
        self.clock = rig.runtime.clock
        self.database: Database = rig.database
        self.scheduling = Rig(rig.database)
        self.scheduling.clock = self.clock
        self.suppressions = SuppressionRegistry(self.clock)
        rig.engine.locks.set_observer(self.suppressions.observe)
        self.deps = SubsystemDeps(
            database=rig.database,
            engine=rig.engine,
            auth=rig.runtime,
            settings=settings,
            suppressions=self.suppressions,
        )
        self.subsystem = factory(self.deps)
        self.name = self.subsystem.name

    async def rule(
        self,
        selector: Mapping[str, Any],
        parameters: Mapping[str, Any] | None = None,
        *,
        name: str = RULE_NAME,
        owner: int = ADMIN_ID,
        environment_id: int = ENV_A,
        enabled: bool = True,
        expression: str = INTERVAL_SECONDS,
    ) -> tuple[int, int]:
        """Insert a rule and its job and return their ids; the job is not due for a day."""
        async with self.database.session() as db:
            rule = await AutomationRuleRepository(db, self.clock).insert(
                name=name,
                subsystem=self.name,
                environment_id=environment_id,
                target_selector=json.dumps(selector, separators=(",", ":")),
                parameters=json.dumps(parameters or {}),
                is_enabled=enabled,
                created_by_user_id=owner,
            )
            job = await ScheduledJobRepository(db, self.clock).insert(
                subsystem=self.name,
                rule_id=rule.id,
                environment_id=environment_id,
                schedule_kind="interval",
                schedule_expression=expression,
                time_zone="UTC",
                overrun_policy="skip",
                catch_up=False,
                failure_threshold=5,
                next_run_at=format_timestamp(self.clock() + DAY),
            )
        return rule.id, job.id

    async def run(self, job_id: int, *, dry: bool = False, trigger: str = "schedule") -> RunResult:
        return await dispatch_of(
            self.scheduling,
            self.subsystem,
            job_id,
            trigger="dry_run" if dry else trigger,
            dry_run=dry,
        )

    def advance(self, seconds: float) -> None:
        self.clock.now += timedelta(seconds=seconds)

    def events(self, action: str | None = None) -> list[dict[str, Any]]:
        found = [dict(row) for row in self.scheduling.events(action)]
        return found

    def posts(self, verb: str) -> list[Any]:
        return [
            request
            for request in self.host.requests
            if request.method == "POST" and request.path.endswith(f"/{verb}")
        ]

    def subsystem_events(self) -> list[dict[str, Any]]:
        return [row for row in self.events() if row["actor_type"] == "subsystem"]

    def restarted(self) -> list[str]:
        """Return the id of each container the daemon was asked to restart, in order."""
        return [request.path.split("/")[-2] for request in self.posts("restart")]

    async def grant(
        self, number: int | str, owner: int, *, access_level: str = "write", **spec: Any
    ) -> None:
        """Put a container, by number or id, under a control that makes `owner` its owner."""
        key = ResourceKey(ENV_A, "container", cid(number) if isinstance(number, int) else number)
        control = ControlSpec(access_level=access_level, owner_user_id=owner, **spec)
        await self.rig.access.controls.create(
            ADMIN, key, control, expected_digest=await digest_of(self.rig.access, key)
        )


def counts(result: RunResult) -> dict[str, Any]:
    """Return a run's detail as a plain dictionary."""
    return dict(result.detail)


async def authority_context(
    db: Session,
    clock: Any,
    locks: ObjectLockRegistry,
    *,
    environment_id: int,
    subsystem: str = HEALTH,
    lock_wait: float = LOCK_WAIT,
    allow: Callable[[ObjectKey], bool] | None = None,
    suppressions: SuppressionRegistry | None = None,
) -> RunContext:
    """Insert a rule, a job and a run on `db` and return the context an administrator's rule has."""
    rule = await AutomationRuleRepository(db, clock).insert(
        name=RULE_NAME,
        subsystem=subsystem,
        environment_id=environment_id,
        target_selector="",
        parameters="{}",
        is_enabled=True,
        created_by_user_id=ADMIN_ID,
    )
    job = await ScheduledJobRepository(db, clock).insert(
        subsystem=subsystem,
        rule_id=rule.id,
        environment_id=environment_id,
        schedule_kind="interval",
        schedule_expression="60",
        time_zone="UTC",
        overrun_policy="skip",
        catch_up=False,
        failure_threshold=5,
        next_run_at=None,
    )
    run = await JobRunRepository(db, clock).start(job.id, trigger="schedule", scheduled_for=None)
    return RunContext(
        dispatch=Dispatch(job=job, run=run, now=clock(), dry_run=False),
        rule=None,
        parameters=None,
        actor=SubsystemActor(subsystem, rule.id, run.id, RULE_NAME),
        db=db,
        activity=ActivityRecorder(db, clock),
        now=clock(),
        dry_run=False,
        locks=locks,
        suppressions=SuppressionRegistry(clock) if suppressions is None else suppressions,
        clock=clock,
        lock_wait=lock_wait,
        suppression_window=timedelta(minutes=5),
        owner_gate=Gate(allow),
    )


async def event_rows(db: Session, action: str | None = None) -> list[dict[str, Any]]:
    """Read activity rows with their detail decoded."""
    sql = (
        "SELECT action, result, actor_type, actor_user_id, actor_subsystem, rule_id, job_run_id, "
        "target_id, detail FROM activity_event"
    )
    params: tuple[Any, ...] = ()
    if action:
        sql += " WHERE action = ?"
        params = (action,)
    found = await db.fetch_limited(sql + " ORDER BY id", params, limit=1000)
    return [{**dict(row), "detail": detail_of(dict(row))} for row in found]
