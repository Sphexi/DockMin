"""Hourly upkeep the loop runs itself: run-history pruning and the retention hook."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.db.engine import Database
from app.db.repositories.clocked import Clock
from app.db.repositories.job_run import KEEP_RUNS_PER_JOB, JobRunRepository
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.logging import get_logger
from app.scheduler.schedules import next_grid_point
from app.services.activity_log import NoRetention, RetentionPolicy

logger = get_logger(__name__)

HOUSEKEEPING_INTERVAL = timedelta(hours=1)
_INTERVAL_SECONDS = int(HOUSEKEEPING_INTERVAL.total_seconds())


@dataclass(frozen=True, slots=True)
class HousekeepingResult:
    """What one pass removed."""

    runs_pruned: int
    events_removed: int


class Housekeeping:
    """Prunes finished runs beyond the per-job bound, then applies the retention policy."""

    def __init__(
        self, database: Database, *, clock: Clock, retention: RetentionPolicy | None = None
    ) -> None:
        self._database = database
        self._clock = clock
        self._retention: RetentionPolicy = NoRetention() if retention is None else retention
        self._next_due: datetime | None = None

    @property
    def next_due(self) -> datetime | None:
        """Return when the next pass is due, or None before the first pass."""
        return self._next_due

    async def run_if_due(self) -> HousekeepingResult | None:
        """Run a pass if none has run yet or the next top of the hour has come."""
        if self._next_due is not None and self._clock() < self._next_due:
            return None
        return await self.run()

    async def run(self) -> HousekeepingResult:
        """Run one pass now; a failure is logged and the pass reports what it managed."""
        self._next_due = next_grid_point(self._clock(), _INTERVAL_SECONDS)
        pruned = await self._prune_runs()
        removed = await self._apply_retention()
        if pruned or removed:
            logger.info(
                "scheduler_housekeeping", extra={"runs_pruned": pruned, "events_removed": removed}
            )
        return HousekeepingResult(runs_pruned=pruned, events_removed=removed)

    async def _prune_runs(self) -> int:
        pruned = 0
        try:
            async with self._database.session() as db:
                jobs = ScheduledJobRepository(db, self._clock)
                runs = JobRunRepository(db, self._clock)
                after = 0
                while page := await jobs.page_after(after, limit=MAX_PAGE_SIZE):
                    after = page[-1].id
                    for job in page:
                        pruned += await runs.prune(job.id, keep=KEEP_RUNS_PER_JOB)
        except Exception as error:
            logger.error("run_pruning_failed", extra={"error": type(error).__name__})
        return pruned

    async def _apply_retention(self) -> int:
        try:
            return (await self._retention.apply(self._clock())).removed
        except Exception as error:
            logger.error("retention_failed", extra={"error": type(error).__name__})
            return 0
