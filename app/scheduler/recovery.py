"""Recording interrupted runs, and the sweep that runs before the loop on every start."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.db.engine import Database, retry_busy
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.clocked import Clock
from app.db.repositories.job_run import JobRun, JobRunRepository
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.logging import get_logger
from app.scheduler.events import rule_detail
from app.scheduler.gates import next_run_text
from app.scheduler.registry import HandlerRegistry
from app.services.activity import ActivityRecorder, SubsystemActor

logger = get_logger(__name__)

INTERRUPTED_ACTION = "job_run_interrupted"

_MESSAGES = {
    "process_stopped": "The application stopped while this run was in progress.",
    "shutdown": "The application shut down while this run was in progress.",
    "superseded": "A newer run of the job replaced this one.",
    "suspended": "The scheduler was paused while this run was in progress.",
}
_DEFAULT_MESSAGE = "This run was interrupted."


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """What the start-up sweep changed."""

    interrupted: int
    gated: int
    revived: int


def _message(reason: str, run: JobRun) -> str:
    text = _MESSAGES.get(reason, _DEFAULT_MESSAGE)
    if reason == "process_stopped" and run.heartbeat_at is not None:
        return f"{text} Last heartbeat: {run.heartbeat_at}."
    return text


async def record_interruption(
    database: Database,
    handlers: HandlerRegistry,
    clock: Clock,
    run_id: int,
    *,
    reason: str,
    unrecorded: JobRun | None = None,
) -> bool:
    """Finish a run still in flight as interrupted, with its event in the same transaction.

    An unrecorded run, one that started without a job_run row, is inserted in the same
    transaction. Returns False when the run had already finished.
    """

    async def attempt() -> bool:
        async with database.session() as db:
            runs = JobRunRepository(db, clock)
            async with db.transaction():
                run = (
                    await runs.record_late(unrecorded)
                    if unrecorded is not None
                    else await runs.get(run_id)
                )
                if run is None or run.outcome is not None:
                    return False
                job = await ScheduledJobRepository(db, clock).get(run.scheduled_job_id)
                if not await runs.finish(
                    run.id, outcome="interrupted", reason=reason, message=_message(reason, run)
                ):
                    return False
                handler = handlers.get(job.subsystem) if job is not None else None
                if job is not None and (handler is None or handler.records_events):
                    rule = await rule_detail(db, clock, job)
                    name = rule.get("rule_name")
                    await ActivityRecorder(db, clock).record_event(
                        SubsystemActor(
                            job.subsystem,
                            job.rule_id,
                            run.id,
                            name if isinstance(name, str) else None,
                        ),
                        action=INTERRUPTED_ACTION,
                        target_type="scheduled_job",
                        target_id=str(job.id),
                        environment_id=job.environment_id,
                        result="failure",
                        detail={"reason": reason, "trigger": run.trigger, **rule},
                    )
                return True

    return await retry_busy(attempt)


async def recover(
    database: Database,
    handlers: HandlerRegistry,
    *,
    clock: Clock,
    subsystem_enabled: Callable[[str], Awaitable[bool]],
) -> RecoveryReport:
    """Interrupt every run left in flight, then reconcile each job's next fire with its gates.

    A job that cannot run now has its next fire cleared; an enabled job that can run, and has
    no next fire, gets one.

    The instance lock guarantees no other process is running jobs, so every in-flight row
    belongs to a process that is gone.
    """
    interrupted = await _interrupt_in_flight(database, handlers, clock)
    gated, revived = await _reconcile_gates(database, handlers, clock, subsystem_enabled)
    return RecoveryReport(interrupted=interrupted, gated=gated, revived=revived)


async def _interrupt_in_flight(database: Database, handlers: HandlerRegistry, clock: Clock) -> int:
    async with database.session() as db:
        in_flight = await JobRunRepository(db, clock).in_flight()
    interrupted = 0
    for run in in_flight:
        try:
            if await record_interruption(
                database, handlers, clock, run.id, reason="process_stopped"
            ):
                interrupted += 1
        except Exception as error:
            logger.error(
                "scheduler_recovery_failed",
                extra={
                    "run_id": run.id,
                    "job_id": run.scheduled_job_id,
                    "error": type(error).__name__,
                },
            )
    return interrupted


async def _reconcile_gates(
    database: Database,
    handlers: HandlerRegistry,
    clock: Clock,
    subsystem_enabled: Callable[[str], Awaitable[bool]],
) -> tuple[int, int]:
    enabled: dict[str, bool] = {}
    unavailable: set[str] = set()
    closed = revived = 0
    async with database.session() as db:
        jobs = ScheduledJobRepository(db, clock)
        rules = AutomationRuleRepository(db, clock)
        after = 0
        while page := await jobs.page_after(after, limit=MAX_PAGE_SIZE):
            after = page[-1].id
            for job in page:
                if not job.is_enabled:
                    continue
                if job.subsystem not in enabled:
                    enabled[job.subsystem] = await subsystem_enabled(job.subsystem)
                available = handlers.get(job.subsystem) is not None
                if not available:
                    unavailable.add(job.subsystem)
                if not (available and enabled[job.subsystem]):
                    if job.next_run_at is not None:
                        await jobs.set_next_run(job.id, None)
                        closed += 1
                    continue
                if job.next_run_at is None:
                    rule = None if job.rule_id is None else await rules.get(job.rule_id)
                    following = next_run_text(
                        job, rule, subsystem_enabled=True, available=True, now=clock()
                    )
                    if following is not None:
                        await jobs.set_next_run(job.id, following)
                        revived += 1
    for name in sorted(unavailable):
        logger.warning("scheduler_subsystem_unavailable", extra={"subsystem": name})
    return closed, revived
