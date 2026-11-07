"""Assembles the scheduler and the registries its subsystems share."""

from dataclasses import dataclass

from app.config import Settings
from app.db.engine import Database
from app.db.repositories.clocked import Clock
from app.scheduler.loop import Scheduler
from app.scheduler.registry import HandlerRegistry
from app.scheduler.suppression import SuppressionRegistry
from app.services.activity_log import NoRetention, RetentionPolicy
from app.services.activity_retention import ActivityRetention
from app.services.object_locks import ObjectLockRegistry


@dataclass
class SchedulerRuntime:
    """The process-wide scheduler state."""

    scheduler: Scheduler
    handlers: HandlerRegistry
    suppressions: SuppressionRegistry


def build_scheduler_runtime(
    *, database: Database, settings: Settings, clock: Clock, locks: ObjectLockRegistry
) -> SchedulerRuntime:
    """Build the scheduler and connect the lock registry's releases to the suppression registry."""
    suppressions = SuppressionRegistry(clock)
    locks.set_observer(suppressions.observe)
    handlers = HandlerRegistry()
    retention: RetentionPolicy = (
        NoRetention()
        if settings.activity_retention_days is None
        else ActivityRetention(database, clock=clock, days=settings.activity_retention_days)
    )
    scheduler = Scheduler(
        database,
        handlers,
        clock=clock,
        retention=retention,
        development_checks=settings.development_checks,
    )
    return SchedulerRuntime(scheduler=scheduler, handlers=handlers, suppressions=suppressions)
