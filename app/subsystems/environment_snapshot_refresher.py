"""The infrastructure job that refreshes every environment's status and object counts."""

from datetime import timedelta
from typing import ClassVar

from app.db.repositories.scheduled_job import OverrunPolicy, ScheduleKind
from app.scheduler.model import RunResult
from app.subsystems.base import RunContext, Subsystem, SubsystemDeps


class EnvironmentSnapshotRefresher(Subsystem):
    """Refreshes all environments on an interval; it writes no activity events."""

    name = "environment-snapshot-refresher"
    parameters_model = None
    event_policy = "none"
    schedule_kinds: ClassVar[frozenset[ScheduleKind]] = frozenset({"interval"})
    min_interval_seconds = 15
    overrun_policy = "skip"
    allowed_overrun_policies: ClassVar[frozenset[OverrunPolicy]] = frozenset({"skip"})
    cancellation_safe = False
    catch_up = True
    run_timeout = timedelta(minutes=10)
    failure_threshold = None
    requires_environment = False

    def __init__(self, deps: SubsystemDeps) -> None:
        super().__init__(deps)
        self._snapshots = deps.engine.snapshots

    async def run(self, context: RunContext) -> RunResult:
        """Refresh every environment and report how many were attempted."""
        attempted = await self._snapshots.refresh_all()
        return RunResult("success", attempted=attempted)
