"""The restore coordinator: owns one restore from engaging the gate to the restart that ends it."""

import asyncio
import contextvars
import hashlib
import hmac
import math
import os
import secrets
import shutil
import string
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any, BinaryIO, Final, Literal

import httpx
from pydantic import SecretStr

from app.auth.errors import (
    NotFound,
    PermissionDenied,
    ProductError,
    ResourceNotFound,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.backup import (
    MAX_ARCHIVE_BYTES,
    ArchiveRefused,
    Manifest,
    RestoreReason,
    SwapPlan,
    check_key,
    paths,
    read_manifest,
)
from app.backup.pipeline import PipelineStep, RestoreContext, StepStatus, run_restore_steps
from app.backup.restore_steps import RestoreSource
from app.config import DB_FILENAME
from app.crypto.keys import key_forms
from app.crypto.vault import Vault
from app.db.engine import Database, Session
from app.db.migrations.runner import load_migrations
from app.db.repositories import backup_snapshot
from app.db.repositories.activity_event import DetailValue, activity_sequence
from app.db.repositories.backup_record import BackupRecordRepository
from app.db.repositories.backup_snapshot import InstanceCounts
from app.db.repositories.clocked import Clock
from app.db.repositories.object_storage_target import ObjectStorageTargetRepository
from app.db.repositories.setting import RESTORE_AUTOMATION_PAUSED_KEY, SettingRepository
from app.logging import DEFAULT_SCRUBBER, get_logger
from app.object_storage import ListedObject, ObjectStorageClient, StorageFailure
from app.scheduler.loop import Scheduler
from app.services.activity import ActivityRecorder, UserActor, audited, completed, current_audit
from app.services.backups import BackupProduction
from app.services.guards import require_administrator
from app.services.image_jobs import ImageJobRegistry
from app.services.object_storage import BACKUP_KEY_NAME, CredentialUnreadable, ObjectStorageTargets
from app.services.restore_gate import RestoreGate
from app.services.stack_jobs import StackJobRegistry

type RestoreStep = Literal[
    "gate", "upload", "key", "verify", "stage", "migrate", "decrypt", "swap", "restart"
]

logger = get_logger(__name__)

__all__ = [
    "Manifest",
    "RemoteArchive",
    "RemoteListing",
    "RestoreBusy",
    "RestoreCoordinator",
    "RestoreNotFound",
    "RestoreProgress",
    "RestoreReview",
    "RestoreRuntime",
    "RestoreStatus",
    "RestoreStep",
    "restore_leftover",
    "sweep_restore_leftovers",
]

STEP_ORDER: Final[tuple[RestoreStep, ...]] = (
    "gate",
    "upload",
    "key",
    "verify",
    "stage",
    "migrate",
    "decrypt",
    "swap",
    "restart",
)
PENDING: Final = "pending"
RUNNING: Final = "running"
DONE: Final = "done"
FAILED: Final = "failed"

STARTED_ACTION: Final = "instance_restore_started"
AUTOMATION_RESUMED_ACTION: Final = "instance_automation_resumed"
FAILED_ACTION: Final = "instance_restore_failed"
KEY_REJECTED_ACTION: Final = "instance_restore_key_rejected"
TARGET_TYPE: Final = "instance"

UPLOAD_NAME: Final = "upload.tar.gz"
PARTIAL_SUFFIX: Final = ".part"
UPLOAD_BUFFER: Final = 1024 * 1024
UPLOAD_FREE_FLOOR: Final = 256 * 1024 * 1024
ENGAGE_WAIT_SECONDS: Final = 30.0
IDLE_TIMEOUT: Final = timedelta(minutes=15)
FINISHED_KEPT: Final = timedelta(minutes=30)
FAILED_HAND_OFF: Final = "Restore the files by hand, then restart the container."
STOP_FAILED_MESSAGE: Final = (
    "The running instance did not stop cleanly, so nothing was replaced and it was started again."
)
LIVE_DATABASE_MISSING_MESSAGE: Final = (
    "The restored data did not include a database, so the previous data was put back."
)
CANCELLED_MESSAGE: Final = "The restore was cancelled."
REMOTE_PAGE_SIZE: Final = 50
MAX_LIST_PAGES: Final = 20
MAX_TARGET_CHOICES: Final = 200
CHOOSE_FROM_LIST: Final = "Choose an archive from the list."
UPLOAD_IN_PROGRESS: Final = "An upload is already in progress."
CREDENTIAL_UNREADABLE: Final = "The target's credential cannot be read; enter it again."
TARGET_GONE: Final = "The object storage target no longer exists."
OBJECT_CHANGED: Final = "The archive changed in the bucket since it was listed."
FETCH_FAILED: Final = "The archive could not be fetched."
LIST_PERMISSION_NOTE: Final = "The credential must be able to list the target's backup folder."

_ASCII_FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_UNEXPECTED_REASON: Final[dict[RestoreStep, RestoreReason]] = {
    "gate": "cancelled",
    "upload": "archive_invalid",
    "key": "key_invalid",
    "verify": "archive_invalid",
    "stage": "archive_invalid",
    "migrate": "migration_failed",
    "decrypt": "decrypt_failed",
    "swap": "swap_failed",
    "restart": "restart_failed",
}


class RestoreBusy(ProductError):
    """A restore is already engaged, or the moment is wrong for the operation asked."""


class RestoreNotFound(NotFound):
    """There is no restore in progress."""


UPDATE_DIRECTORY: Final = "update"
UPDATE_IN_PROGRESS: Final = "An update is in progress. Restore is unavailable until it finishes."


def update_files_present(data_dir: Path) -> bool:
    """Report whether the update directory holds an update; blocks, run it in a thread.

    Only directories count; the helper's lock file and any other file are not an update.
    """
    root = data_dir / UPDATE_DIRECTORY
    if root.is_symlink():
        return True
    try:
        return any(entry.is_dir() for entry in root.iterdir())
    except FileNotFoundError:
        return False


@dataclass(frozen=True, slots=True)
class RestoreRuntime:
    """The parts of the running application a restore uses; read fresh on every use."""

    database: Database
    vault: Vault
    scheduler: Scheduler
    image_jobs: ImageJobRegistry
    stack_jobs: StackJobRegistry
    running_key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class RestoreReview:
    """What the review page shows before the key is asked for."""

    restore_id: str
    manifest: Manifest | None
    archive_sha256: str | None
    counts: InstanceCounts
    swap: SwapPlan
    refusals: tuple[str, ...]
    operator_matched: bool
    source: RestoreSource = "upload"
    object_path: str | None = None
    record_match: Literal["matches", "differs", "none"] | None = None
    recorded_at: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteArchive:
    """One archive in a bucket listing."""

    key: str
    folder: str
    size: int
    last_modified: str | None
    recorded: bool


@dataclass(frozen=True, slots=True)
class RemoteListing:
    """One page of the archives under a target's backup folder, or why there is none."""

    target_id: int
    target_name: str
    archives: tuple[RemoteArchive, ...]
    page: int
    page_count: int
    total: int
    truncated: bool
    failure: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class RestoreProgress:
    """The steps of one restore and how it ended, for the progress page."""

    restore_id: str
    steps: tuple[tuple[RestoreStep, str], ...]
    paths: SwapPlan | None
    outcome: Literal["running", "restored", "failed"]
    reason: RestoreReason | None
    message: str | None


@dataclass(frozen=True, slots=True)
class RestoreStatus:
    """The restore that is engaged now, for the page that offers to begin one."""

    restore_id: str
    owner_username: str
    started_at: datetime
    stage: Literal["awaiting_archive", "fetching", "awaiting_key", "running"]
    owned_by_caller: bool
    fetched_bytes: int = 0
    fetch_size: int = 0
    fetch_error: str | None = None


@dataclass(frozen=True, slots=True)
class _Listing:
    target_name: str
    objects: tuple[ListedObject, ...]
    sizes: dict[str, int]
    truncated: bool


@dataclass(frozen=True, slots=True)
class _ListFailed:
    target_name: str
    message: str
    note: str | None


@dataclass(slots=True)
class _Restore:
    id: str
    owner_user_id: int
    owner_username: str
    session_id: int
    directory: Path
    started_at: datetime
    last_activity: datetime
    steps: dict[RestoreStep, str] = field(default_factory=dict)
    manifest: Manifest | None = None
    raw_manifest: bytes = b""
    sha256: str | None = None
    confirmed: bool = False
    swap_started: bool = False
    lifted: bool = False
    outcome: Literal["running", "restored", "failed"] = "running"
    final: Literal["restored", "failed"] = "failed"
    reason: RestoreReason | None = None
    message: str | None = None
    source: RestoreSource = "upload"
    object_storage_target_id: int | None = None
    object_path: str | None = None
    plan: SwapPlan | None = None
    finished_at: datetime | None = None
    listings: dict[int, _Listing] = field(default_factory=dict)
    fetching: bool = False
    fetched_bytes: int = 0
    fetch_size: int = 0
    fetch_error: str | None = None
    fetch_task: asyncio.Task[None] | None = None
    holder_stop: asyncio.Event = field(default_factory=asyncio.Event)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    holds_production: bool = False
    uploading: bool = False
    ending: bool = False
    stopping: bool = False
    holder: asyncio.Task[None] | None = None
    watchdog: asyncio.Task[None] | None = None
    run_task: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        for step in STEP_ORDER:
            self.steps.setdefault(step, PENDING)
        self.steps["gate"] = DONE

    def is_stopping(self) -> bool:
        return self.stopping


class _Upload:
    def __init__(self, path: Path) -> None:
        self.handle: BinaryIO = os.fdopen(paths.open_private_file(path), "wb")
        self.sha = hashlib.sha256()
        self.size = 0
        self.floor = path.parent


def _append(upload: _Upload, data: bytes) -> None:
    if shutil.disk_usage(upload.floor).free < UPLOAD_FREE_FLOOR:
        raise ArchiveRefused(
            "insufficient_space", "There is not enough free space on the data volume."
        )
    upload.handle.write(data)
    upload.sha.update(data)
    upload.size += len(data)


def _close_upload(upload: _Upload) -> None:
    upload.handle.flush()
    upload.handle.close()


def _clear_directory(directory: Path) -> None:
    """Delete everything in a restore directory but the previous and rejected data."""
    if not directory.exists():
        return
    kept = False
    for entry in directory.iterdir():
        if entry.name in (paths.PREVIOUS_DIRECTORY, paths.REJECTED_DIRECTORY):
            kept = True
        elif entry.is_symlink() or entry.is_file():
            entry.unlink()
        else:
            shutil.rmtree(entry)
    if not kept:
        directory.rmdir()


def _drop_if_empty(directory: Path) -> None:
    with suppress(OSError):
        directory.rmdir()


def _live_counts(database: Database) -> InstanceCounts:
    connection = database.connect()
    try:
        return backup_snapshot.instance_counts(connection)
    finally:
        connection.close()


def _live_activity_floor(database: Database) -> int:
    connection = database.connect()
    try:
        return activity_sequence(connection)
    finally:
        connection.close()


@cache
def _supported_schema() -> int:
    return load_migrations()[-1].version


def _fold(name: str) -> str:
    return name.translate(_ASCII_FOLD)


def _refusals(manifest: Manifest, username: str) -> tuple[str, ...]:
    found: list[str] = []
    supported = _supported_schema()
    if manifest.schema_version > supported:
        found.append(
            f"The archive's schema version is {manifest.schema_version}; "
            f"this build supports up to {supported}."
        )
    if _fold(username) not in {_fold(name) for name in manifest.administrators}:
        found.append("The archive has no enabled administrator with your user name.")
    return tuple(found)


def _stage_of(restore: _Restore) -> RestoreStep:
    return next((step for step in STEP_ORDER if restore.steps[step] != DONE), "restart")


def _live_row_detail(restore: _Restore) -> dict[str, DetailValue]:
    detail: dict[str, DetailValue] = {"restore_id": restore.id, "stage": _stage_of(restore)}
    if restore.manifest is not None:
        detail["archive_created_at"] = restore.manifest.created_at
        detail["archive_schema_version"] = restore.manifest.schema_version
    return detail


async def _record_failure(
    recorder: ActivityRecorder,
    actor_id: int,
    *,
    action: str,
    reason: str,
    message: str | None,
    detail: dict[str, DetailValue],
) -> None:
    await recorder.record_failure(
        UserActor(actor_id),
        action=action,
        target_type=TARGET_TYPE,
        reason=reason,
        detail=detail,
        message=message,
    )


class _LiveLog:
    """Writes the restore's rows to the live activity log on a session of its own."""

    def __init__(self, db: Session, clock: Clock) -> None:
        self._db = db
        self._activity = ActivityRecorder(db, clock)
        self._settings = SettingRepository(db, clock)

    @audited(action=STARTED_ACTION, target_type=TARGET_TYPE)
    async def started(self, actor: Principal, *, restore_id: str) -> None:
        """Record that the instance went offline for a restore."""
        current_audit().detail["restore_id"] = restore_id

    @audited(action=AUTOMATION_RESUMED_ACTION, target_type=TARGET_TYPE)
    async def resumed(self, actor: Principal) -> None:
        """Record that automation was resumed and delete the pause that held it."""
        async with self._db.transaction():
            await self._settings.delete(RESTORE_AUTOMATION_PAUSED_KEY)

    async def key_rejected(
        self, actor: Principal, restore: _Restore, refused: ArchiveRefused
    ) -> None:
        """Record a key the restore refused, by its reason alone."""
        await _record_failure(
            self._activity,
            actor.user_id,
            action=KEY_REJECTED_ACTION,
            reason=refused.reason,
            message=refused.message,
            detail=_live_row_detail(restore),
        )

    async def stopped(
        self, actor_id: int, restore: _Restore, reason: RestoreReason, message: str | None
    ) -> None:
        """Record that the restore ended without success."""
        await _record_failure(
            self._activity,
            actor_id,
            action=FAILED_ACTION,
            reason=reason,
            message=message,
            detail={**_live_row_detail(restore), "source": restore.source},
        )


class RestoreCoordinator:
    """Runs one restore at a time through the gate, the key step, the swap and the restart."""

    def __init__(
        self,
        *,
        gate: RestoreGate,
        production: BackupProduction,
        data_dir: Path,
        runtime: Callable[[], RestoreRuntime],
        stop_runtime: Callable[[], Awaitable[None]],
        start_runtime: Callable[[], Awaitable[None]],
        clock: Clock,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        idle_timeout: timedelta = IDLE_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        busy: Callable[[], bool] = lambda: False,
    ) -> None:
        self._transport = transport
        self._busy = busy
        self._gate = gate
        self._production = production
        self._data_dir = data_dir
        self._runtime = runtime
        self._stop_runtime = stop_runtime
        self._start_runtime = start_runtime
        self._clock = clock
        self._sleep = sleep
        self._idle_timeout = idle_timeout
        self._active: _Restore | None = None
        self._finished: dict[str, _Restore] = {}
        self._lock = asyncio.Lock()
        self._hold: tuple[asyncio.Task[None], asyncio.Event] | None = None

    @property
    def working_directory(self) -> Path | None:
        """Return the directory of the restore in progress, which a startup sweep must keep."""
        return None if self._active is None else self._active.directory

    @property
    def automation_paused(self) -> bool:
        """Report whether the scheduler is held suspended after a restore."""
        return self._gate.automation_paused

    def status(self, actor: Principal, session_id: int | None) -> RestoreStatus | None:
        """Describe the restore that is engaged now, if any."""
        require_administrator(actor)
        restore = self._active
        if restore is None:
            return None
        stage: Literal["awaiting_archive", "fetching", "awaiting_key", "running"]
        if restore.confirmed:
            stage = "running"
        elif restore.fetching:
            stage = "fetching"
        elif restore.manifest is None:
            stage = "awaiting_archive"
        else:
            stage = "awaiting_key"
        return RestoreStatus(
            restore_id=restore.id,
            owner_username=restore.owner_username,
            started_at=restore.started_at,
            stage=stage,
            owned_by_caller=self._is_owner(restore, actor, session_id),
            fetched_bytes=restore.fetched_bytes,
            fetch_size=restore.fetch_size,
            fetch_error=restore.fetch_error,
        )

    async def begin(self, actor: Principal, *, session_id: int) -> str:
        """Engage the gate for a restore and return its id; administrators only.

        Raises RestoreBusy while another restore or a re-key holds the gate, while a stored-secret
        check or an image or stack job runs, while an update is in progress, or when a backup is
        still being produced after thirty seconds.
        """
        require_administrator(actor)
        if self._gate.phase != "open" and self._gate.purpose == "rekey":
            raise RestoreBusy(
                "Stored secrets are being re-encrypted. Try again when it has finished."
            )
        if self._active is not None or self._gate.phase != "open":
            raise RestoreBusy("A restore is already in progress.")
        if self._busy():
            raise RestoreBusy("A stored-secret check is running. Try again when it has finished.")
        runtime = self._runtime()
        self._require_no_jobs(runtime)
        if await asyncio.to_thread(update_files_present, self._data_dir):
            raise RestoreBusy(UPDATE_IN_PROGRESS)
        now = self._clock()
        identifier = secrets.token_hex(16)
        restore = _Restore(
            id=identifier,
            owner_user_id=actor.user_id,
            owner_username=actor.username,
            session_id=session_id,
            directory=self._data_dir / paths.RESTORE_DIRECTORY / identifier,
            started_at=now,
            last_activity=now,
        )
        self._active = restore
        try:
            async with self._lock:
                await self._engage(restore, actor, runtime)
        except BaseException:
            await completed(self._lift(restore))
            raise
        restore.watchdog = self._spawn(self._watch_idle(restore))
        return identifier

    async def receive_archive(
        self, actor: Principal, session_id: int, parts: AsyncIterator[bytes]
    ) -> Manifest:
        """Store the uploaded archive, replacing any earlier one, and return its manifest.

        Raises ValidationFailed on the archive field when the file is not a usable backup.
        """
        restore = self._owned(actor, session_id)
        self._require_quiet(restore, UPLOAD_IN_PROGRESS)
        restore.uploading = True
        try:
            async with self._lock:
                if restore.ending:
                    raise RestoreNotFound("no restore is in progress")
                if restore.confirmed:
                    raise RestoreBusy("This restore is already running.")
                restore.fetch_error = None
                manifest = await self._receive(restore, parts)
                restore.source = "upload"
                restore.object_storage_target_id = None
                restore.object_path = None
                return manifest
        finally:
            restore.uploading = False

    async def review(self, actor: Principal, session_id: int) -> RestoreReview:
        """Describe what the restore would replace and what already rules the archive out."""
        restore = self._owned(actor, session_id)
        self._touch(restore)
        manifest = restore.manifest
        counts = await asyncio.to_thread(_live_counts, self._runtime().database)
        refusals = () if manifest is None else _refusals(manifest, restore.owner_username)
        record_match, recorded_at = await self._record_match(restore)
        matched = manifest is not None and _fold(restore.owner_username) in {
            _fold(name) for name in manifest.administrators
        }
        return RestoreReview(
            restore_id=restore.id,
            manifest=manifest,
            archive_sha256=restore.sha256,
            counts=counts,
            swap=paths.plan_swap(self._data_dir, restore.directory),
            refusals=refusals,
            operator_matched=matched,
            source=restore.source,
            object_path=restore.object_path,
            record_match=record_match,
            recorded_at=recorded_at,
        )

    async def remote_targets(self, actor: Principal) -> list[tuple[int, str]]:
        """Return the id and name of the object storage targets a restore can read from."""
        require_administrator(actor)
        async with self._runtime().database.session() as db:
            return await ObjectStorageTargetRepository(db).summaries(limit=MAX_TARGET_CHOICES)

    async def list_remote(
        self,
        actor: Principal,
        session_id: int,
        target_id: int,
        *,
        refresh: bool = False,
        page: int = 1,
    ) -> RemoteListing:
        """List the archives under a target's backup folder, newest first, 50 to a page.

        The listing is kept on the restore until refreshed; only a key in it can be fetched. A
        refused or failed read is returned in `failure`. Raises NotFound for an unknown target.
        """
        restore = self._owned(actor, session_id)
        self._touch(restore)
        cached = restore.listings.get(target_id)
        if cached is None or refresh:
            read = await self._read_listing(target_id)
            self._touch(restore)
            if isinstance(read, _ListFailed):
                return RemoteListing(
                    target_id=target_id,
                    target_name=read.target_name,
                    archives=(),
                    page=1,
                    page_count=1,
                    total=0,
                    truncated=False,
                    failure=read.message,
                    note=read.note,
                )
            restore.listings[target_id] = cached = read
        return await self._present(cached, target_id, page)

    async def fetch_archive(
        self, actor: Principal, session_id: int, *, target_id: int, object_path: str
    ) -> None:
        """Start fetching a listed archive in the background; its review follows the fetch.

        Raises ValidationFailed on the archive field for a key outside the restore's listing, an
        archive above the size limit, or too little free space, and RestoreBusy during an upload
        or another fetch.
        """
        restore = self._owned(actor, session_id)
        self._require_quiet(restore, UPLOAD_IN_PROGRESS)
        listing = restore.listings.get(target_id)
        size = None if listing is None else listing.sizes.get(object_path)
        if size is None:
            raise ValidationFailed({"archive": CHOOSE_FROM_LIST})
        if size > MAX_ARCHIVE_BYTES:
            raise ValidationFailed({"archive": "The archive is larger than the size limit."})
        free = await asyncio.to_thread(lambda: shutil.disk_usage(self._data_dir).free)
        if free < size + UPLOAD_FREE_FLOOR:
            raise ValidationFailed(
                {"archive": "There is not enough free space on the data volume."}
            )
        self._owned(actor, session_id)
        self._require_quiet(restore, UPLOAD_IN_PROGRESS)
        self._touch(restore)
        restore.uploading = True
        restore.fetching = True
        restore.fetch_error = None
        restore.fetched_bytes = 0
        restore.fetch_size = size
        restore.fetch_task = self._spawn(self._fetch(restore, target_id, object_path, size))

    async def confirm(
        self,
        actor: Principal,
        session_id: int,
        *,
        key_text: SecretStr,
        archive_sha256: str,
        replace_confirmed: bool,
    ) -> str:
        """Check the key and start the restore in the background; return the restore id.

        Raises ValidationFailed for a key the archive refuses, an archive that changed since the
        review, or a missing confirmation. Nothing is staged before the key is accepted.
        """
        restore = self._owned(actor, session_id)
        self._require_quiet(
            restore, "This restore is already running or still receiving its archive."
        )
        async with self._lock:
            if restore.ending:
                raise RestoreNotFound("no restore is in progress")
            self._require_quiet(
                restore, "This restore is already running or still receiving its archive."
            )
            self._touch(restore)
            manifest, digest = restore.manifest, restore.sha256
            if manifest is None or digest is None:
                raise ValidationFailed({"archive": "Upload the archive first."})
            if not hmac.compare_digest(archive_sha256.encode(), digest.encode()):
                raise ValidationFailed(
                    {"archive": "The uploaded archive changed since the review."}
                )
            refusals = _refusals(manifest, restore.owner_username)
            if refusals:
                raise ValidationFailed({"archive": refusals[0]})
            if not replace_confirmed:
                raise ValidationFailed(
                    {"replace_confirmed": "Confirm that this instance's data will be replaced."}
                )
            try:
                key = check_key(manifest, restore.raw_manifest, key_text.get_secret_value())
            except ArchiveRefused as refused:
                await self._note_key_rejected(actor, restore, refused)
                raise ValidationFailed({"key": refused.message}) from None
            for form in key_forms(bytes(key)):
                DEFAULT_SCRUBBER.register_secret(form)
            restore.steps["key"] = DONE
            restore.confirmed = True
            if restore.watchdog is not None:
                restore.watchdog.cancel()
            restore.run_task = self._spawn(self._conduct(restore, key))
            return restore.id

    async def cancel(self, actor: Principal) -> None:
        """Release the gate before the key is confirmed; any administrator may.

        Raises RestoreBusy once the restore is running. Does nothing when none is engaged.
        """
        require_administrator(actor)
        restore = self._active
        if restore is None:
            return
        if restore.confirmed:
            raise RestoreBusy("The restore is running and can no longer be cancelled.")
        self._begin_ending(restore)
        async with self._lock:
            await completed(self._finish_failed(restore, "cancelled", None, actor_id=actor.user_id))

    async def resume_automation(self, actor: Principal) -> None:
        """Let the scheduler run again after a restore; administrators only, and idempotent."""
        require_administrator(actor)
        if not self._gate.automation_paused:
            return
        self._gate.release_automation()
        try:
            async with self._runtime().database.session() as db:
                await _LiveLog(db, self._clock).resumed(actor)
        except BaseException:
            self._gate.hold_automation()
            raise
        await self.release_hold()

    async def hold_automation(self, scheduler: Scheduler) -> None:
        """Suspend a scheduler that has not started and keep it so until automation is resumed."""
        await self.release_hold()
        release = asyncio.Event()
        task, entered = self._start_hold(scheduler, release)
        self._hold = (task, release)
        await self._await_entered(task, entered)

    async def release_hold(self) -> None:
        """End the suspension held after a restore, if there is one."""
        held, self._hold = self._hold, None
        if held is None:
            return
        task, release = held
        release.set()
        await asyncio.gather(task, return_exceptions=True)

    def progress(self, restore_id: str, *, session_id: int | None) -> RestoreProgress | None:
        """Return the steps of a restore, or None when it is unknown to the caller.

        Before the swap only the owning session may look; from the swap on the id is the authority.
        """
        self._expire()
        restore = self._active if self._active and self._active.id == restore_id else None
        restore = restore or self._finished.get(restore_id)
        if restore is None:
            return None
        if not restore.swap_started and session_id != restore.session_id:
            return None
        return RestoreProgress(
            restore_id=restore.id,
            steps=tuple((step, restore.steps[step]) for step in STEP_ORDER),
            paths=restore.plan,
            outcome=restore.outcome,
            reason=restore.reason,
            message=restore.message,
        )

    async def aclose(self) -> None:
        """Stop the restore in progress, if any, and release the gate."""
        restore = self._active
        if restore is None:
            return
        restore.stopping = True
        restore.ending = True
        restore.stop_event.set()
        if restore.watchdog is not None:
            restore.watchdog.cancel()
        task = restore.run_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            await completed(
                self._finish_failed(restore, "shutdown", None, actor_id=restore.owner_user_id)
            )

    def _owned(self, actor: Principal, session_id: int) -> _Restore:
        require_administrator(actor)
        restore = self._active
        if restore is None or restore.ending:
            raise RestoreNotFound("no restore is in progress")
        if not self._is_owner(restore, actor, session_id):
            raise PermissionDenied("this restore belongs to another session")
        return restore

    @staticmethod
    def _is_owner(restore: _Restore, actor: Principal, session_id: int | None) -> bool:
        return actor.user_id == restore.owner_user_id and session_id == restore.session_id

    @staticmethod
    def _require_quiet(restore: _Restore, message: str) -> None:
        if restore.confirmed or restore.uploading:
            raise RestoreBusy(message)

    @staticmethod
    def _begin_ending(restore: _Restore) -> None:
        if restore.confirmed or restore.lifted:
            return
        restore.ending = True
        restore.stop_event.set()

    @staticmethod
    def _require_no_jobs(runtime: RestoreRuntime) -> None:
        running = runtime.image_jobs.running_count() + runtime.stack_jobs.running_count()
        if running:
            raise RestoreBusy(f"{running} jobs are running; wait for them to finish.")

    @staticmethod
    def _abandon_if_ending(restore: _Restore) -> None:
        if restore.ending:
            raise RestoreBusy(CANCELLED_MESSAGE)

    def _touch(self, restore: _Restore) -> None:
        restore.last_activity = self._clock()

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        return asyncio.get_running_loop().create_task(work, context=contextvars.Context())

    def _expire(self) -> None:
        now = self._clock()
        stale = [
            identifier
            for identifier, restore in self._finished.items()
            if restore.finished_at is not None and now - restore.finished_at > FINISHED_KEPT
        ]
        for identifier in stale:
            del self._finished[identifier]

    async def _engage(self, restore: _Restore, actor: Principal, runtime: RestoreRuntime) -> None:
        await self._take_production(restore)
        restore.holds_production = True
        self._abandon_if_ending(restore)
        self._gate.engage(restore.id, restore.started_at, purpose="restore")
        self._require_no_jobs(runtime)
        restore.holder, entered = self._start_hold(runtime.scheduler, restore.holder_stop)
        await self._await_entered(restore.holder, entered)
        self._abandon_if_ending(restore)
        async with runtime.database.session() as db:
            await _LiveLog(db, self._clock).started(actor, restore_id=restore.id)

    async def _take_production(self, restore: _Restore) -> None:
        if self._production.try_hold():
            return
        waiter = asyncio.ensure_future(self._production.hold())
        timer = asyncio.ensure_future(self._sleep(ENGAGE_WAIT_SECONDS))
        stop = asyncio.ensure_future(restore.stop_event.wait())
        try:
            await asyncio.wait({waiter, timer, stop}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            await self._settle_wait(waiter, timer, stop)
            if _acquired(waiter):
                self._production.free()
            raise
        await self._settle_wait(waiter, timer, stop)
        if _acquired(waiter):
            return
        raise RestoreBusy(
            CANCELLED_MESSAGE
            if restore.ending
            else "A backup is still being produced. Try again shortly."
        )

    @staticmethod
    async def _settle_wait(*futures: asyncio.Future[Any]) -> None:
        for pending in futures:
            if not pending.done():
                pending.cancel()
        await asyncio.gather(*futures, return_exceptions=True)

    def _start_hold(
        self, scheduler: Scheduler, release: asyncio.Event
    ) -> tuple[asyncio.Task[None], asyncio.Event]:
        entered = asyncio.Event()

        async def hold() -> None:
            async with scheduler.suspend():
                entered.set()
                await release.wait()

        return self._spawn(hold()), entered

    @staticmethod
    async def _await_entered(holder: asyncio.Task[None], entered: asyncio.Event) -> None:
        waiter = asyncio.ensure_future(entered.wait())
        try:
            await asyncio.wait({holder, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        if holder.done():
            holder.result()
            raise RuntimeError("the scheduler suspension ended before it was needed")

    async def _watch_idle(self, restore: _Restore) -> None:
        while not restore.confirmed and not restore.lifted:
            remaining = (restore.last_activity + self._idle_timeout - self._clock()).total_seconds()
            if remaining <= 0:
                self._begin_ending(restore)
                if not restore.ending:
                    return
                async with self._lock:
                    await completed(
                        self._finish_failed(
                            restore,
                            "timed_out",
                            None,
                            actor_id=restore.owner_user_id,
                            watchdog=True,
                        )
                    )
                return
            await self._sleep(remaining)
            await asyncio.sleep(0)

    async def _receive(self, restore: _Restore, parts: AsyncIterator[bytes]) -> Manifest:
        self._touch(restore)
        restore.manifest, restore.raw_manifest, restore.sha256 = None, b"", None
        restore.steps["upload"] = PENDING
        try:
            sha256 = await self._store_upload(restore, parts)
            manifest, raw = await asyncio.to_thread(read_manifest, restore.directory / UPLOAD_NAME)
        except ArchiveRefused as refused:
            await completed(asyncio.to_thread(_remove_upload, restore.directory))
            raise ValidationFailed({"archive": refused.message}) from None
        except BaseException:
            await completed(asyncio.to_thread(_remove_upload, restore.directory))
            raise
        restore.manifest, restore.raw_manifest, restore.sha256 = manifest, raw, sha256
        restore.steps["upload"] = DONE
        self._touch(restore)
        return manifest

    async def _store_upload(self, restore: _Restore, parts: AsyncIterator[bytes]) -> str:
        directory = await asyncio.to_thread(paths.new_restore_directory, self._data_dir, restore.id)
        partial = directory / f"{UPLOAD_NAME}{PARTIAL_SUFFIX}"
        await asyncio.to_thread(partial.unlink, missing_ok=True)
        upload = await completed(asyncio.to_thread(_Upload, partial))
        writing = asyncio.ensure_future(
            self._write_parts(restore, upload, parts, partial, directory / UPLOAD_NAME)
        )
        stop = asyncio.ensure_future(restore.stop_event.wait())
        try:
            await asyncio.wait({writing, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await self._settle_wait(writing, stop)
        if writing.cancelled():
            raise RestoreNotFound("no restore is in progress")
        return writing.result()

    async def _write_parts(
        self,
        restore: _Restore,
        upload: _Upload,
        parts: AsyncIterator[bytes],
        partial: Path,
        final: Path,
    ) -> str:
        buffer = bytearray()
        try:
            async for chunk in parts:
                buffer += chunk
                if upload.size + len(buffer) > MAX_ARCHIVE_BYTES:
                    raise ArchiveRefused(
                        "archive_invalid", "The archive is larger than the size limit."
                    )
                if len(buffer) >= UPLOAD_BUFFER:
                    await completed(asyncio.to_thread(_append, upload, bytes(buffer)))
                    buffer.clear()
                    self._touch(restore)
            if buffer:
                await completed(asyncio.to_thread(_append, upload, bytes(buffer)))
            await completed(asyncio.to_thread(_close_upload, upload))
            await completed(asyncio.to_thread(partial.replace, final))
        except BaseException:
            await completed(asyncio.to_thread(_abandon_upload, upload, partial))
            raise
        return upload.sha.hexdigest()

    async def _read_listing(self, target_id: int) -> _Listing | _ListFailed:
        runtime = self._runtime()
        async with runtime.database.session() as db:
            targets = ObjectStorageTargets(db, runtime.vault)
            stored = await targets.backup_prefix_of(target_id)
            if stored is None:
                raise ResourceNotFound("no such object storage target")
            name = stored[0]
            try:
                opened = await targets.open(target_id)
            except CredentialUnreadable:
                return _ListFailed(name, CREDENTIAL_UNREADABLE, None)
        if opened is None:
            raise ResourceNotFound("no such object storage target")
        base = opened.backup_prefix.removesuffix("/")
        try:
            async with ObjectStorageClient(
                opened.endpoint, opened.credentials, transport=self._transport, clock=self._clock
            ) as client:
                found, truncated = await _list_archives(client, f"{base}/" if base else "")
        except StorageFailure as failure:
            note = LIST_PERMISSION_NOTE if failure.kind == "refused" else None
            return _ListFailed(name, str(failure), note)
        newest_first = sorted(found, key=_stamp_order, reverse=True)
        return _Listing(
            target_name=name,
            objects=tuple(newest_first),
            sizes={entry.key: entry.size for entry in newest_first},
            truncated=truncated,
        )

    async def _present(self, listing: _Listing, target_id: int, page: int) -> RemoteListing:
        count = max(1, math.ceil(len(listing.objects) / REMOTE_PAGE_SIZE))
        current = min(max(page, 1), count)
        start = (current - 1) * REMOTE_PAGE_SIZE
        shown = listing.objects[start : start + REMOTE_PAGE_SIZE]
        async with self._runtime().database.session() as db:
            records = await BackupRecordRepository(db, self._clock).by_destinations(
                target_id, [entry.key for entry in shown]
            )
        return RemoteListing(
            target_id=target_id,
            target_name=listing.target_name,
            archives=tuple(
                RemoteArchive(
                    key=entry.key,
                    folder=entry.key.rpartition("/")[0],
                    size=entry.size,
                    last_modified=entry.last_modified,
                    recorded=entry.key in records,
                )
                for entry in shown
            ),
            page=current,
            page_count=count,
            total=len(listing.objects),
            truncated=listing.truncated,
        )

    async def _record_match(
        self, restore: _Restore
    ) -> tuple[Literal["matches", "differs", "none"] | None, str | None]:
        target_id, key, digest = (
            restore.object_storage_target_id,
            restore.object_path,
            restore.sha256,
        )
        if restore.source != "object_storage" or target_id is None or key is None or digest is None:
            return None, None
        async with self._runtime().database.session() as db:
            records = await BackupRecordRepository(db, self._clock).by_destinations(
                target_id, [key]
            )
        record = records.get(key)
        if record is None or record.archive_sha256 is None:
            return "none", None
        same = hmac.compare_digest(record.archive_sha256.encode(), digest.encode())
        return ("matches" if same else "differs"), record.created_at

    async def _fetch(self, restore: _Restore, target_id: int, key: str, size: int) -> None:
        try:
            async with self._lock:
                if restore.ending or restore.confirmed:
                    return
                async with aclosing(self._remote_chunks(restore, target_id, key, size)) as chunks:
                    await self._receive(restore, chunks)
                restore.source = "object_storage"
                restore.object_storage_target_id = target_id
                restore.object_path = key
        except RestoreNotFound:
            return
        except ValidationFailed as invalid:
            restore.fetch_error = invalid.fields.get("archive", FETCH_FAILED)
        except StorageFailure as failure:
            restore.fetch_error = str(failure)
        except Exception as error:
            logger.error("restore_fetch_failed", extra={"error": type(error).__name__})
            restore.fetch_error = FETCH_FAILED
        finally:
            restore.fetching = False
            restore.uploading = False

    async def _remote_chunks(
        self, restore: _Restore, target_id: int, key: str, size: int
    ) -> AsyncGenerator[bytes]:
        runtime = self._runtime()
        try:
            async with runtime.database.session() as db:
                opened = await ObjectStorageTargets(db, runtime.vault).open(target_id)
        except CredentialUnreadable:
            raise ArchiveRefused("archive_invalid", CREDENTIAL_UNREADABLE) from None
        if opened is None:
            raise ArchiveRefused("archive_invalid", TARGET_GONE)
        async with (
            ObjectStorageClient(
                opened.endpoint, opened.credentials, transport=self._transport, clock=self._clock
            ) as client,
            client.get_object(key, max_bytes=MAX_ARCHIVE_BYTES) as body,
        ):
            if body.size != size:
                raise ArchiveRefused("archive_invalid", OBJECT_CHANGED)
            async for chunk in body.chunks():
                restore.fetched_bytes += len(chunk)
                yield chunk

    async def _note_key_rejected(
        self, actor: Principal, restore: _Restore, refused: ArchiveRefused
    ) -> None:
        try:
            async with self._runtime().database.session() as db:
                await _LiveLog(db, self._clock).key_rejected(actor, restore, refused)
        except Exception as error:
            logger.error("restore_row_write_failed", extra={"error": type(error).__name__})

    async def _conduct(self, restore: _Restore, key: bytearray) -> None:
        try:
            await self._execute(restore, key)
        except asyncio.CancelledError:
            await completed(
                self._finish_failed(restore, "shutdown", None, actor_id=restore.owner_user_id)
            )
            raise
        finally:
            for index in range(len(key)):
                key[index] = 0

    async def _execute(self, restore: _Restore, key: bytearray) -> None:
        manifest = restore.manifest
        assert manifest is not None
        step: RestoreStep = "verify"

        def note(reached: PipelineStep, status: StepStatus) -> None:
            nonlocal step
            step = reached
            restore.steps[reached] = RUNNING if status == "running" else DONE

        try:
            runtime = self._runtime()
            floor = await asyncio.to_thread(_live_activity_floor, runtime.database)
            plan = paths.plan_swap(self._data_dir, restore.directory)
        except Exception as error:
            await self._fail_unexpectedly(restore, "decrypt", error)
            return
        try:
            await run_restore_steps(
                archive=restore.directory / UPLOAD_NAME,
                staging=restore.directory / paths.STAGING_DIRECTORY,
                manifest=manifest,
                archive_key=key,
                running_key=runtime.running_key,
                operator_username=restore.owner_username,
                live_activity_floor=floor,
                record_context=RestoreContext(
                    restore_id=restore.id,
                    archive_sha256=restore.sha256 or "",
                    previous_path=str(plan.previous),
                    source=restore.source,
                    object_storage_target_id=restore.object_storage_target_id,
                    object_path=restore.object_path,
                ),
                clock=self._clock,
                stop=restore.is_stopping,
                on_step=note,
            )
        except ArchiveRefused as refused:
            restore.steps[step] = FAILED
            await completed(
                self._finish_failed(
                    restore, refused.reason, refused.message, actor_id=restore.owner_user_id
                )
            )
            return
        except Exception as error:
            await self._fail_unexpectedly(restore, step, error)
            return
        await completed(self._swap_and_restart(restore))

    async def _fail_unexpectedly(
        self, restore: _Restore, step: RestoreStep, error: Exception
    ) -> None:
        logger.error("restore_step_failed", extra={"step": step, "error": type(error).__name__})
        restore.steps[step] = FAILED
        await completed(
            self._finish_failed(
                restore,
                _UNEXPECTED_REASON[step],
                f"The restore stopped unexpectedly at the {step} step.",
                actor_id=restore.owner_user_id,
            )
        )

    async def _swap_and_restart(self, restore: _Restore) -> None:
        locations = (
            str(restore.directory / paths.PREVIOUS_DIRECTORY),
            str(restore.directory / paths.STAGING_DIRECTORY),
        )
        try:
            await self._run_swap(restore)
        except Exception as error:
            if restore.lifted:
                raise
            logger.error("restore_swap_interrupted", extra={"error": type(error).__name__})
            await self._fail_for_good(
                restore,
                "swap_failed" if restore.steps["swap"] != DONE else "restart_failed",
                "The restore stopped unexpectedly while the data was being replaced.",
                locations,
                error,
            )

    async def _run_swap(self, restore: _Restore) -> None:
        restore.swap_started = True
        self._gate.begin_swap()
        restore.steps["swap"] = RUNNING
        try:
            await self._stop_runtime()
        except Exception as error:
            logger.error("restore_stop_failed", extra={"error": type(error).__name__})
            plan = paths.plan_swap(self._data_dir, restore.directory)
            await self._abort_swap(restore, plan, paths.SwapJournal(), STOP_FAILED_MESSAGE)
            return
        plan = paths.plan_swap(self._data_dir, restore.directory)
        restore.plan = plan
        logger.info(
            "restore_swap_paths",
            extra={
                "live_paths": ", ".join(str(path) for path in plan.live),
                "previous_path": str(plan.previous),
                "staged_path": str(plan.staged),
            },
        )
        try:
            journal = await asyncio.to_thread(paths.apply_swap, plan)
        except paths.SwapFailed as failure:
            await self._abort_swap(restore, plan, failure.journal, failure.message)
            return
        if not self._live_database_present():
            await self._abort_swap(restore, plan, journal, LIVE_DATABASE_MISSING_MESSAGE)
            return
        self._gate.hold_automation()
        restore.steps["swap"] = DONE
        restore.steps["restart"] = RUNNING
        try:
            await self._start_runtime()
        except Exception as error:
            logger.error("restore_restart_failed", extra={"error": type(error).__name__})
            await self._reject_restored(restore, plan, journal)
            return
        restore.steps["restart"] = DONE
        await self._finish_restored(restore)

    async def _abort_swap(
        self, restore: _Restore, plan: SwapPlan, journal: paths.SwapJournal, message: str
    ) -> None:
        restore.steps["swap"] = FAILED
        try:
            await asyncio.to_thread(paths.revert_swap, journal)
        except paths.SwapFailed as failure:
            await self._fail_for_good(
                restore, "swap_failed", message, (str(plan.previous), str(plan.staged)), failure
            )
            return
        await asyncio.to_thread(_drop_if_empty, plan.previous)
        try:
            self._require_live_database()
            await self._start_runtime()
        except Exception as error:
            logger.error("restore_restart_failed", extra={"error": type(error).__name__})
            await self._stop_quietly()
            await self._fail_for_good(
                restore, "swap_failed", message, (str(plan.previous), str(plan.staged)), error
            )
            return
        await self._finish_failed(restore, "swap_failed", message, actor_id=restore.owner_user_id)

    async def _reject_restored(
        self, restore: _Restore, plan: SwapPlan, journal: paths.SwapJournal
    ) -> None:
        restore.steps["restart"] = FAILED
        rejected = restore.directory / paths.REJECTED_DIRECTORY
        message = (
            "The restored instance did not start, so the previous data was put back. "
            f"The restored files were kept in {rejected}."
        )
        await self._stop_quietly()
        await self._drop_automation_hold()
        try:
            await asyncio.to_thread(paths.reject_restored, plan, journal, rejected)
        except paths.SwapFailed as failure:
            await self._fail_for_good(
                restore, "restart_failed", message, (str(plan.previous), str(rejected)), failure
            )
            return
        try:
            self._require_live_database()
            await self._start_runtime()
        except Exception as error:
            logger.error("restore_restart_failed", extra={"error": type(error).__name__})
            await self._stop_quietly()
            await self._fail_for_good(
                restore, "restart_failed", message, (str(plan.previous), str(rejected)), error
            )
            return
        await self._finish_failed(
            restore, "restart_failed", message, actor_id=restore.owner_user_id
        )

    def _live_database_present(self) -> bool:
        database = self._data_dir / DB_FILENAME
        return database.is_file() and not database.is_symlink()

    def _require_live_database(self) -> None:
        if not self._live_database_present():
            raise FileNotFoundError(DB_FILENAME)

    async def _stop_quietly(self) -> None:
        try:
            await self._stop_runtime()
        except Exception as error:
            logger.error("restore_stop_failed", extra={"error": type(error).__name__})

    async def _drop_automation_hold(self) -> None:
        self._gate.release_automation()
        await self.release_hold()

    async def _fail_for_good(
        self,
        restore: _Restore,
        reason: RestoreReason,
        message: str,
        locations: tuple[str, ...],
        cause: BaseException,
    ) -> None:
        logger.error(
            "restore_failed_unrecoverable",
            extra={
                "locations": ", ".join(locations),
                "reason": reason,
                "error": type(cause).__name__,
            },
        )
        self._gate.mark_failed(locations)
        await self._drop_automation_hold()
        restore.reason = reason
        restore.message = f"{message} {FAILED_HAND_OFF}"
        await self._lift(restore)

    async def _finish_restored(self, restore: _Restore) -> None:
        if restore.lifted:
            return
        restore.final = "restored"
        await self._lift(restore)

    async def _finish_failed(
        self,
        restore: _Restore,
        reason: RestoreReason,
        message: str | None,
        *,
        actor_id: int,
        watchdog: bool = False,
    ) -> None:
        if restore.lifted:
            return
        restore.reason = reason
        restore.message = message
        try:
            await self._note_stopped(restore, reason, message, actor_id)
        finally:
            await self._lift(restore, watchdog=watchdog)

    async def _note_stopped(
        self, restore: _Restore, reason: RestoreReason, message: str | None, actor_id: int
    ) -> None:
        try:
            async with self._runtime().database.session() as db:
                await _LiveLog(db, self._clock).stopped(actor_id, restore, reason, message)
        except Exception as error:
            logger.error("restore_row_write_failed", extra={"error": type(error).__name__})

    async def _lift(self, restore: _Restore, *, watchdog: bool = False) -> None:
        """Release everything the restore holds; every path to the end of a restore comes here."""
        if restore.lifted:
            return
        restore.lifted = True
        restore.ending = True
        restore.stop_event.set()
        restore.finished_at = self._clock()
        try:
            if restore.watchdog is not None and not watchdog:
                restore.watchdog.cancel()
            restore.holder_stop.set()
            if restore.holder is not None:
                await asyncio.gather(restore.holder, return_exceptions=True)
        finally:
            failed = self._gate.phase == "failed"
            if restore.holds_production and not failed:
                self._production.free()
            self._gate.reopen()
            self._finished[restore.id] = restore
            if self._active is restore:
                self._active = None
            try:
                if not failed:
                    await asyncio.to_thread(_clear_directory, restore.directory)
            except OSError as error:
                logger.error("restore_cleanup_failed", extra={"error": type(error).__name__})
            finally:
                restore.outcome = restore.final


def _stamp_order(entry: ListedObject) -> tuple[str, str]:
    return entry.key.rpartition("/")[2], entry.key


async def _list_archives(
    client: ObjectStorageClient, folder: str
) -> tuple[list[ListedObject], bool]:
    found: list[ListedObject] = []
    token: str | None = None
    for _ in range(MAX_LIST_PAGES):
        listed = await client.list_objects(folder, continuation=token)
        found.extend(
            entry
            for entry in listed.objects
            if BACKUP_KEY_NAME.fullmatch(entry.key.rpartition("/")[2])
        )
        if not listed.truncated:
            return found, False
        token = listed.next_token
        if token is None:
            break
    return found, True


def _acquired(waiter: asyncio.Future[None]) -> bool:
    return waiter.done() and not waiter.cancelled() and waiter.exception() is None


def _remove_upload(directory: Path) -> None:
    for name in (UPLOAD_NAME, f"{UPLOAD_NAME}{PARTIAL_SUFFIX}"):
        (directory / name).unlink(missing_ok=True)


def _abandon_upload(upload: _Upload, partial: Path) -> None:
    try:
        upload.handle.close()
    finally:
        partial.unlink(missing_ok=True)


def restore_leftover(data_dir: Path) -> Path | None:
    """Return the first previous or rejected directory a restore left under data_dir, if any."""
    return paths.restore_leftover(data_dir)


def sweep_restore_leftovers(data_dir: Path, *, keep: Path | None = None) -> None:
    """Remove the backup work directory and any restore directory that holds no moved data.

    A directory holding previous or rejected data is kept and logged. Blocks; run it in a thread.
    """
    paths.remove_directory(data_dir / paths.WORK_DIRECTORY)
    base = data_dir / paths.RESTORE_DIRECTORY
    if base.is_symlink() or not base.is_dir():
        return
    for entry in sorted(base.iterdir()):
        if keep is not None and entry == keep:
            continue
        if entry.is_symlink() or not entry.is_dir():
            continue
        if (entry / paths.PREVIOUS_DIRECTORY).exists() or (
            entry / paths.REJECTED_DIRECTORY
        ).exists():
            logger.warning("restore_previous_data_present", extra={"path": str(entry)})
        else:
            paths.remove_directory(entry)
