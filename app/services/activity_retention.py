"""Age-based removal of activity events, with one recorded row per batch that removes any."""

from datetime import datetime, timedelta
from typing import Final

from app.db.engine import Database
from app.db.repositories.activity_event import ActivityEventRepository
from app.db.repositories.clocked import Clock, format_timestamp
from app.logging import get_logger
from app.scheduler.housekeeping import HOUSEKEEPING_INTERVAL
from app.services.activity import ActivityRecorder, SubsystemActor
from app.services.activity_log import RetentionResult

logger = get_logger(__name__)

PRUNER: Final = "activity-log-pruner"
PRUNED_ACTION: Final = "activity_log_pruned"
PRUNED_TARGET_TYPE: Final = "activity_log"
BATCH_ROWS: Final = 2_000
BATCHES_PER_PASS: Final = 20
SCAN_ROWS: Final = 10_000
MAX_ANCHORS: Final = 50_000
MAX_CUTOFF_ADVANCE: Final = 2 * HOUSEKEEPING_INTERVAL


class ActivityRetention:
    """Removes events past an age cap, except those automation reads or no exporter has taken."""

    def __init__(self, database: Database, *, clock: Clock, days: int) -> None:
        if days < 1:
            raise ValueError("days must be at least 1")
        self._database = database
        self._clock = clock
        self._days = days
        self._accepted_cutoff: datetime | None = None

    async def apply(self, now: datetime) -> RetentionResult:
        """Run one bounded pass; each batch deletes and records itself in one transaction.

        The first pass of a process accepts any cutoff. Later, a cutoff more than twice the
        housekeeping interval past the last accepted one removes nothing until the process restarts.
        """
        moment = now - timedelta(days=self._days)
        cutoff = format_timestamp(moment)
        removed = 0
        batches = 0
        held_watermark: int | None = None
        async with self._database.session() as db:
            events = ActivityEventRepository(db, self._clock)
            accepted = self._accepted_cutoff
            if accepted is not None and moment - accepted > MAX_CUTOFF_ADVANCE:
                logger.warning(
                    "retention_clock_jump",
                    extra={
                        "accepted_cutoff": format_timestamp(accepted),
                        "cutoff": cutoff,
                        "limit_seconds": int(MAX_CUTOFF_ADVANCE.total_seconds()),
                    },
                )
                return RetentionResult(removed=0)
            self._accepted_cutoff = moment
            anchors = await events.retention_anchors(cutoff, limit=MAX_ANCHORS + 1)
            if len(anchors) > MAX_ANCHORS:
                logger.warning("retention_anchor_limit", extra={"limit": MAX_ANCHORS})
                return RetentionResult(removed=0)
            recorder = ActivityRecorder(db, self._clock)
            resume: tuple[str, int] | None = None
            for _ in range(BATCHES_PER_PASS):
                async with db.transaction():
                    batch = await events.prune_batch(
                        cutoff=cutoff,
                        anchors=anchors,
                        resume_after=resume,
                        scan=SCAN_ROWS,
                        limit=BATCH_ROWS,
                    )
                    if batch.removed:
                        await recorder.record_event(
                            SubsystemActor(PRUNER, None),
                            action=PRUNED_ACTION,
                            target_type=PRUNED_TARGET_TYPE,
                            result="success",
                            detail={
                                "removed": batch.removed,
                                "retention_days": self._days,
                                "cutoff": cutoff,
                                "first_removed_id": batch.first_id,
                                "last_removed_id": batch.last_id,
                                "oldest_removed_at": batch.oldest_at,
                                "newest_removed_at": batch.newest_at,
                                "kept_for_automation": batch.kept,
                                "export_watermark": batch.watermark,
                            },
                        )
                batches += 1
                removed += batch.removed
                if batch.blocked_by_export:
                    held_watermark = batch.watermark
                    if not batch.removed:
                        break
                if batch.resume_after is None or batch.resume_after == resume:
                    break
                resume = batch.resume_after
            else:
                if not removed:
                    logger.warning(
                        "retention_scan_exhausted",
                        extra={"batches": batches, "scan_rows": SCAN_ROWS, "cutoff": cutoff},
                    )
            if held_watermark is not None:
                oldest = await events.first_after(held_watermark)
                logger.info(
                    "retention_held_by_export",
                    extra={
                        "watermark": held_watermark,
                        "oldest_unexported_at": None if oldest is None else oldest.occurred_at,
                    },
                )
        if removed:
            logger.info("activity_retention_pass", extra={"removed": removed, "batches": batches})
        return RetentionResult(removed=removed)
