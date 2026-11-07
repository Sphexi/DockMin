"""The restore steps from an archive to a finalised staged database, shared by every caller."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

from app.backup.archive import StopRequested, stage_members, verify_members
from app.backup.manifest import ArchiveRefused, Manifest, RestoreReason
from app.backup.restore_steps import (
    Reseal,
    RestoreRecord,
    RestoreSource,
    StagedCheck,
    decrypt_and_reseal,
    finalise_staged,
    prepare_staged,
)
from app.db.repositories.clocked import Clock
from app.services.activity import completed

type PipelineStep = Literal["verify", "stage", "migrate", "decrypt"]
type StepStatus = Literal["running", "done"]


@dataclass(frozen=True, slots=True)
class RestoreContext:
    """What the restored activity log records about where the restore came from."""

    restore_id: str
    archive_sha256: str
    previous_path: str
    source: RestoreSource = "upload"
    object_storage_target_id: int | None = None
    object_path: str | None = None


@dataclass(frozen=True, slots=True)
class StagedRestore:
    """A staged database that is checked, re-sealed and finalised, ready to be swapped in."""

    check: StagedCheck
    reseal: Reseal | None


class StepRefused(ArchiveRefused):
    """An ArchiveRefused that names the step the pipeline had reached."""

    def __init__(self, reason: RestoreReason, message: str, step: PipelineStep) -> None:
        super().__init__(reason, message)
        self.step: PipelineStep = step


def _until_stopped(work: Callable[[], None]) -> bool:
    try:
        work()
    except StopRequested:
        return False
    return True


async def _run_stoppable(work: Callable[[], None]) -> None:
    if not await completed(asyncio.to_thread(_until_stopped, work)):
        raise asyncio.CancelledError


async def run_restore_steps(
    *,
    archive: Path,
    staging: Path,
    manifest: Manifest,
    archive_key: bytearray,
    running_key: bytes,
    operator_username: str,
    live_activity_floor: int,
    record_context: RestoreContext,
    clock: Clock,
    stop: Callable[[], bool],
    on_step: Callable[[PipelineStep, StepStatus], None],
) -> StagedRestore:
    """Verify, stage, migrate and check, decrypt and re-seal, then finalise the staged database.

    Raises StepRefused naming the step reached, and CancelledError once stop returns true. Any
    other failure propagates unchanged; the step last reported as running is the one that failed.
    """
    step: PipelineStep = "verify"
    try:
        on_step(step, "running")
        await _run_stoppable(partial(verify_members, archive, manifest, stop=stop))
        on_step(step, "done")
        step = "stage"
        on_step(step, "running")
        await _run_stoppable(partial(stage_members, archive, manifest, staging, stop=stop))
        on_step(step, "done")
        step = "migrate"
        on_step(step, "running")
        checked = await completed(prepare_staged(staging, operator_username=operator_username))
        on_step(step, "done")
        step = "decrypt"
        on_step(step, "running")
        reseal = await completed(decrypt_and_reseal(staging, manifest, archive_key, running_key))
        record = RestoreRecord(
            operator_user_id=checked.operator_user_id,
            operator_username=operator_username,
            archive_sha256=record_context.archive_sha256,
            manifest=manifest,
            migrated_from=checked.migrated_from,
            schema_version=checked.schema_version,
            previous_path=record_context.previous_path,
            restore_id=record_context.restore_id,
            source=record_context.source,
            object_storage_target_id=record_context.object_storage_target_id,
            object_path=record_context.object_path,
        )
        await completed(
            finalise_staged(
                staging,
                record=record,
                reseal=reseal,
                live_activity_floor=live_activity_floor,
                clock=clock,
            )
        )
        on_step(step, "done")
    except ArchiveRefused as refused:
        raise StepRefused(refused.reason, refused.message, step) from None
    return StagedRestore(checked, reseal)
