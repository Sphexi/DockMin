"""backup-syncer: stores a backup archive in an object storage bucket and prunes older ones."""

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import ClassVar, Final

import httpx
from pydantic import Field

from app.auth.errors import PermissionDenied
from app.auth.principal import Principal
from app.backup import ProducedArchive, ProductionFailed, paths, produce_archive
from app.backup.parts import PartDigest
from app.db.engine import Database
from app.db.repositories import backup_snapshot
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.backup_record import BackupRecordRepository
from app.db.repositories.clocked import Clock, parse_timestamp
from app.db.repositories.errors import MissingReferenceError, constraints_as_domain_errors
from app.db.repositories.pagination import MAX_ROW_ID
from app.db.repositories.scheduled_job import OverrunPolicy, ScheduleKind
from app.logging import get_logger
from app.scheduler.model import RunFailed, RunResult
from app.services.activity import completed
from app.services.authorisation import Authoriser
from app.services.backups import BackupProduction
from app.services.object_storage import (
    BACKUP_KEY_NAME,
    CredentialUnreadable,
    ListedObject,
    ObjectStorageClient,
    ObjectStorageTargets,
    OpenedTarget,
    StorageFailure,
    backup_folder,
    backup_object_key,
)
from app.subsystems.base import RuleParameters, RunContext, Subsystem, SubsystemDeps

logger = get_logger(__name__)

NAME: Final = "backup-syncer"
UPLOADED_ACTION: Final = "backup_uploaded"
PRUNED_ACTION: Final = "backup_remote_pruned"
TARGET_TYPE: Final = "object_storage_target"
CONTENT_TYPE: Final = "application/gzip"
RETRY_DELAY: Final = 2.0
ABORT_TIMEOUT: Final = 30.0
MAX_DELETES_PER_RUN: Final = 100
MAX_LIST_PAGES: Final = 20
MAX_REPORTED_PATHS: Final = 20
MAX_REPORTED_CHARS: Final = 480
SERVER_ERROR: Final = 500
UPLOADED_NOT_LISTED: Final = "uploaded_not_listed"

TARGET_MISSING: Final = "The object storage target no longer exists."
CREDENTIAL_UNREADABLE: Final = "The target's stored secret cannot be read."
PREFIX_INVALID: Final = "The target's backup prefix does not form a valid object key."
RULE_MISSING: Final = "The automation was deleted during the run."
UNAVAILABLE: Final = "Backups are not available in this process."
ARCHIVE_UNREADABLE: Final = "The finished archive could not be read for upload."


class BackupSyncerParameters(RuleParameters):
    """Which object storage target receives the archives."""

    object_storage_target_id: int = Field(ge=1, le=MAX_ROW_ID)


@dataclass(slots=True)
class _Progress:
    rule_id: int
    rule_created_at: str
    target_id: int
    target_name: str | None = None
    attempted: int = 0
    object_path: str | None = None
    archive: ProducedArchive | None = None
    workdir: Path | None = None
    pruned: "_Pruned | None" = None


@dataclass(slots=True)
class _Pruned:
    folder: str
    retention: int
    deleted: list[str] = field(default_factory=list)
    kept: int = 0
    others: int = 0
    skipped: str | None = None
    failure: StorageFailure | None = None


class _Stopped(Exception):
    def __init__(
        self,
        reason: str,
        message: str,
        *,
        stage: str,
        failure: StorageFailure | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.stage = stage
        self.failure = failure


class BackupSyncer(Subsystem):
    """Produces an archive, uploads it under the rule's folder and applies remote retention."""

    name = NAME
    parameters_model = BackupSyncerParameters
    event_policy = "per_action"
    schedule_kinds: ClassVar[frozenset[ScheduleKind]] = frozenset({"cron", "interval"})
    min_interval_seconds = 3600
    overrun_policy = "skip"
    allowed_overrun_policies: ClassVar[frozenset[OverrunPolicy]] = frozenset({"skip"})
    cancellation_safe = True
    catch_up = True
    run_timeout = timedelta(minutes=180)
    failure_threshold = 5
    requires_environment = False
    supports_dry_run = True

    def __init__(
        self,
        deps: SubsystemDeps,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(deps)
        self._transport = transport
        self._sleep = sleep

    async def authorise_target(
        self,
        principal: Principal,
        environment_id: int | None,
        selector: str,
        authoriser: Authoriser,
    ) -> None:
        """Raise unless the principal is an administrator."""
        if not principal.is_administrator:
            raise PermissionDenied("insufficient access")

    async def run(self, context: RunContext) -> RunResult:
        """Store one archive and prune the rule's folder; a dry run only describes the plan."""
        rule, params = context.rule, context.parameters
        if rule is None or not isinstance(params, BackupSyncerParameters):
            raise RunFailed("parameters_invalid", "The automation's parameters are not valid.")
        progress = _Progress(
            rule_id=rule.id,
            rule_created_at=rule.created_at,
            target_id=params.object_storage_target_id,
        )
        if context.dry_run:
            return await self._forecast(context, progress)
        try:
            await self._sync(context, progress)
        except _Stopped as stopped:
            return await self._record_stop(context, progress, stopped)
        return self._result(progress)

    async def _forecast(self, context: RunContext, progress: _Progress) -> RunResult:
        targets = ObjectStorageTargets(context.db, self._deps.engine.vault)
        known = await targets.backup_prefix_of(progress.target_id)
        if known is None:
            raise RunFailed("target_missing", TARGET_MISSING)
        _, prefix, retention = known
        try:
            key = backup_object_key(
                prefix, progress.rule_id, progress.rule_created_at, context.clock()
            )
        except ValueError:
            raise RunFailed("prefix_invalid", PREFIX_INVALID) from None
        database_bytes, stack_files = await asyncio.to_thread(_database_facts, self._deps.database)
        detail: dict[str, DetailValue] = {
            "target_id": progress.target_id,
            "next_object_path": key,
            "retention_count": retention,
            "database_bytes": database_bytes,
            "stack_files": stack_files,
        }
        return RunResult("success", detail=detail)

    async def _sync(self, context: RunContext, progress: _Progress) -> None:
        targets = ObjectStorageTargets(context.db, self._deps.engine.vault)
        known = await targets.backup_prefix_of(progress.target_id)
        if known is None:
            raise _Stopped("target_missing", TARGET_MISSING, stage="produce")
        progress.target_name, prefix, retention = known
        try:
            backup_object_key(prefix, progress.rule_id, progress.rule_created_at, context.clock())
        except ValueError:
            raise _Stopped("prefix_invalid", PREFIX_INVALID, stage="produce") from None
        try:
            opened = await targets.open(progress.target_id)
        except CredentialUnreadable:
            raise _Stopped("credential_unreadable", CREDENTIAL_UNREADABLE, stage="upload") from None
        if opened is None:
            raise _Stopped("target_missing", TARGET_MISSING, stage="produce")
        production = self._deps.backup_production
        if production is None:
            raise _Stopped("backup_unavailable", UNAVAILABLE, stage="produce")
        client = self._connect(opened, context.clock)
        if isinstance(client, StorageFailure):
            raise _Stopped(client.kind, client.message, stage="upload", failure=client)
        try:
            async with client:
                progress.attempted = 1
                archive = await self._produce(context, production, progress)
                at = parse_timestamp(archive.manifest.created_at)
                key = backup_object_key(prefix, progress.rule_id, progress.rule_created_at, at)
                progress.object_path = key
                await self._upload(client, archive, key)
                await self._note_stored(context, progress, archive, key)
                if retention is not None:
                    folder = f"{backup_folder(prefix, progress.rule_id, progress.rule_created_at)}/"
                    progress.pruned = await self._prune(
                        client, context, progress, folder, retention
                    )
        finally:
            workdir, progress.workdir = progress.workdir, None
            if workdir is not None:
                await completed(asyncio.to_thread(_discard, workdir))

    def _connect(self, opened: OpenedTarget, clock: Clock) -> ObjectStorageClient | StorageFailure:
        try:
            return ObjectStorageClient(
                opened.endpoint, opened.credentials, transport=self._transport, clock=clock
            )
        except StorageFailure as failure:
            return failure

    async def _produce(
        self, context: RunContext, production: BackupProduction, progress: _Progress
    ) -> ProducedArchive:
        failed: ProductionFailed | None = None
        async with production.holding():
            progress.workdir = await asyncio.to_thread(
                paths.new_work_directory, production.data_dir
            )
            try:
                archive = await produce_archive(
                    self._deps.database,
                    self._deps.engine.vault,
                    progress.workdir,
                    clock=context.clock,
                )
            except ProductionFailed as failure:
                failed = failure
        if failed is not None:
            raise _Stopped(failed.reason, failed.message, stage="produce")
        progress.archive = archive
        return archive

    async def _upload(
        self, client: ObjectStorageClient, archive: ProducedArchive, key: str
    ) -> None:
        unreadable = False
        failure: StorageFailure | None = None
        try:
            failure = await self._put(client, archive, key)
        except (OSError, ValueError):
            unreadable = True
        if unreadable:
            raise _Stopped("archive_unreadable", ARCHIVE_UNREADABLE, stage="upload")
        if failure is not None:
            raise _Stopped(failure.kind, failure.message, stage="upload", failure=failure)

    async def _put(
        self, client: ObjectStorageClient, archive: ProducedArchive, key: str
    ) -> StorageFailure | None:
        if archive.parts:
            return await self._put_multipart(client, archive, key)
        try:
            await _send(client, archive, key)
            return None
        except StorageFailure as failure:
            if not _transient(failure):
                return failure
        await self._sleep(RETRY_DELAY)
        try:
            await _send(client, archive, key)
        except StorageFailure as failure:
            return failure
        return None

    async def _put_multipart(
        self, client: ObjectStorageClient, archive: ProducedArchive, key: str
    ) -> StorageFailure | None:
        try:
            upload_id = await client.create_multipart_upload(key, content_type=CONTENT_TYPE)
        except StorageFailure as refused:
            return refused
        failure: StorageFailure | None = None
        try:
            etags = [
                await self._put_part(client, archive.path, key, upload_id, number, part)
                for number, part in enumerate(archive.parts, start=1)
            ]
            await client.complete_multipart_upload(key, upload_id, etags, total_bytes=archive.size)
        except StorageFailure as caught:
            failure = caught
        except BaseException:
            await _abort(client, key, upload_id)
            raise
        if failure is not None:
            await _abort(client, key, upload_id)
        return failure

    async def _put_part(
        self,
        client: ObjectStorageClient,
        path: Path,
        key: str,
        upload_id: str,
        number: int,
        part: PartDigest,
    ) -> str:
        try:
            return await client.upload_part(key, upload_id, number, path, part)
        except StorageFailure as failure:
            if not _transient(failure):
                raise
        await self._sleep(RETRY_DELAY)
        return await client.upload_part(key, upload_id, number, path, part)

    async def _note_stored(
        self, context: RunContext, progress: _Progress, archive: ProducedArchive, key: str
    ) -> None:
        manifest = archive.manifest
        try:
            async with context.db.transaction():
                try:
                    with constraints_as_domain_errors("backup record"):
                        record_id = await BackupRecordRepository(
                            context.db, context.clock
                        ).add_backup_record(
                            origin="sync",
                            target_id=progress.target_id,
                            size_bytes=archive.size,
                            key_version=manifest.key_version,
                            key_fingerprint=manifest.key_fingerprint,
                            destination_key=key,
                            archive_sha256=archive.sha256,
                            schema_version=manifest.schema_version,
                            outcome="success",
                            error=None,
                        )
                except MissingReferenceError:
                    raise _Stopped("target_missing", TARGET_MISSING, stage="upload") from None
                with constraints_as_domain_errors("activity event"):
                    await context.activity.record_event(
                        context.actor,
                        action=UPLOADED_ACTION,
                        target_type=TARGET_TYPE,
                        target_id=str(progress.target_id),
                        result="success",
                        detail={
                            "object_path": key,
                            "size_bytes": archive.size,
                            "archive_sha256": archive.sha256,
                            "schema_version": manifest.schema_version,
                            "key_version": manifest.key_version,
                            "key_fingerprint": manifest.key_fingerprint,
                            "target_name": progress.target_name,
                            "backup_record_id": record_id,
                            "multipart": bool(archive.parts),
                            "parts": len(archive.parts) or 1,
                        },
                    )
        except MissingReferenceError:
            raise _Stopped("rule_missing", RULE_MISSING, stage="upload") from None
        logger.info("backup_archive_stored", extra={"object_path": key, "size_bytes": archive.size})

    async def _prune(
        self,
        client: ObjectStorageClient,
        context: RunContext,
        progress: _Progress,
        folder: str,
        retention: int,
    ) -> _Pruned:
        result = _Pruned(folder=folder, retention=retention)
        stored = progress.object_path
        listed = await _list_folder(client, folder, result)
        if listed is not None and stored is not None:
            doomed = _select(listed, folder, stored, retention, result)
            for key in doomed:
                failure = await _delete(client, folder, key)
                if failure is not None:
                    result.failure = failure
                    break
                result.deleted.append(key)
        await self._note_pruned(context, progress, result)
        return result

    async def _note_pruned(self, context: RunContext, progress: _Progress, result: _Pruned) -> None:
        if not result.deleted and result.failure is None:
            return
        names, truncated = _reported_names(result.folder, result.deleted)
        detail: dict[str, DetailValue] = {
            "folder_path": result.folder,
            "retention_count": result.retention,
            "deleted": len(result.deleted),
            "deleted_paths": names,
            "deleted_paths_truncated": truncated,
            "kept": result.kept,
            "other_objects": result.others,
        }
        if result.failure is None:
            await context.activity.record_event(
                context.actor,
                action=PRUNED_ACTION,
                target_type=TARGET_TYPE,
                target_id=str(progress.target_id),
                result="success",
                detail=detail,
            )
            return
        if result.failure.status is not None:
            detail["storage_status"] = result.failure.status
        if result.failure.code is not None:
            detail["storage_code"] = result.failure.code
        await context.activity.record_failure(
            context.actor,
            action=PRUNED_ACTION,
            target_type=TARGET_TYPE,
            target_id=str(progress.target_id),
            reason=result.failure.kind,
            detail=detail,
            message=result.failure.message,
        )

    async def _record_stop(
        self, context: RunContext, progress: _Progress, stopped: _Stopped
    ) -> RunResult:
        detail: dict[str, DetailValue] = {"stage": stopped.stage}
        if progress.target_name is not None:
            detail["target_name"] = progress.target_name
        if progress.object_path is not None:
            detail["object_path"] = progress.object_path
        if stopped.failure is not None:
            if stopped.failure.status is not None:
                detail["storage_status"] = stopped.failure.status
            if stopped.failure.code is not None:
                detail["storage_code"] = stopped.failure.code
        await context.activity.record_failure(
            context.actor,
            action=UPLOADED_ACTION,
            target_type=TARGET_TYPE,
            target_id=str(progress.target_id),
            reason=stopped.reason,
            detail=detail,
            message=stopped.message,
        )
        if progress.target_name is not None:
            await self._note_failed_record(context, progress, stopped)
        return RunResult(
            "failure",
            attempted=progress.attempted,
            reason=stopped.reason,
            message=stopped.message,
            detail=self._summary(progress),
        )

    async def _note_failed_record(
        self, context: RunContext, progress: _Progress, stopped: _Stopped
    ) -> None:
        archive = progress.archive
        vault = self._deps.engine.vault
        try:
            async with context.db.transaction():
                await BackupRecordRepository(context.db, context.clock).add_backup_record(
                    origin="sync",
                    target_id=progress.target_id,
                    size_bytes=None if archive is None else archive.size,
                    key_version=vault.current_version
                    if archive is None
                    else archive.manifest.key_version,
                    key_fingerprint=vault.key_fingerprint()
                    if archive is None
                    else archive.manifest.key_fingerprint,
                    destination_key=progress.object_path,
                    archive_sha256=None if archive is None else archive.sha256,
                    schema_version=None if archive is None else archive.manifest.schema_version,
                    outcome="failure",
                    error=stopped.message,
                )
        except Exception as error:
            logger.error("backup_record_write_failed", extra={"error": type(error).__name__})

    def _result(self, progress: _Progress) -> RunResult:
        pruned = progress.pruned
        if pruned is not None and pruned.failure is not None:
            return RunResult(
                "partial",
                attempted=progress.attempted,
                reason=pruned.failure.kind,
                message=pruned.failure.message,
                detail=self._summary(progress),
            )
        return RunResult("success", attempted=progress.attempted, detail=self._summary(progress))

    @staticmethod
    def _summary(progress: _Progress) -> dict[str, DetailValue]:
        detail: dict[str, DetailValue] = {"target_id": progress.target_id}
        archive = progress.archive
        if progress.object_path is not None:
            detail["object_path"] = progress.object_path
        if archive is not None:
            detail["size_bytes"] = archive.size
        pruned = progress.pruned
        if pruned is not None:
            detail["retention_deleted"] = len(pruned.deleted)
            detail["retention_kept"] = pruned.kept
            if pruned.skipped is not None:
                detail["retention_skipped"] = pruned.skipped
        return detail


async def _send(client: ObjectStorageClient, archive: ProducedArchive, key: str) -> None:
    await client.put_object_file(
        key,
        archive.path,
        content_type=CONTENT_TYPE,
        size=archive.size,
        md5_b64=archive.md5_b64,
        sha256_hex=archive.sha256,
    )


async def _abort(client: ObjectStorageClient, key: str, upload_id: str) -> None:
    abort = asyncio.ensure_future(_bounded_abort(client, key, upload_id))
    try:
        await asyncio.shield(abort)
    except asyncio.CancelledError:
        # The abort is already bounded by ABORT_TIMEOUT, so waiting it out cannot hang.
        while not abort.done():
            with suppress(asyncio.CancelledError):
                await asyncio.wait({abort})
        _log_abort_failure(abort)
        raise
    except Exception:
        pass
    _log_abort_failure(abort)


def _log_abort_failure(abort: "asyncio.Future[None]") -> None:
    if abort.cancelled():
        kind: str | None = "cancelled"
    else:
        failure = abort.exception()
        match failure:
            case None:
                kind = None
            case StorageFailure():
                kind = failure.kind
            case TimeoutError():
                kind = "timeout"
            case _:
                kind = type(failure).__name__
    if kind is not None:
        logger.warning("backup_multipart_abort_failed", extra={"kind": kind})


async def _bounded_abort(client: ObjectStorageClient, key: str, upload_id: str) -> None:
    async with asyncio.timeout(ABORT_TIMEOUT):
        await client.abort_multipart_upload(key, upload_id)


async def _list_folder(
    client: ObjectStorageClient, folder: str, result: _Pruned
) -> list[ListedObject] | None:
    objects: list[ListedObject] = []
    token: str | None = None
    for _ in range(MAX_LIST_PAGES):
        failure: StorageFailure | None = None
        try:
            page = await client.list_objects(folder, continuation=token)
        except StorageFailure as caught:
            failure = caught
        if failure is not None:
            result.failure = failure
            return None
        objects.extend(page.objects)
        if not page.truncated or page.next_token is None:
            break
        token = page.next_token
    return objects


def _archive_name(key: str, folder: str) -> str | None:
    if not key.startswith(folder):
        return None
    name = key[len(folder) :]
    return name if BACKUP_KEY_NAME.fullmatch(name) else None


def _select(
    listed: list[ListedObject], folder: str, stored: str, retention: int, result: _Pruned
) -> list[str]:
    """Return the archive keys to delete, oldest first; empty when the listing is not trusted.

    The stored archive and the newest older ones are kept. A name stamped after the stored
    archive is never counted or deleted.
    """
    keys = dict.fromkeys(obj.key for obj in listed)
    names = {name for key in keys if (name := _archive_name(key, folder))}
    stored_name = _archive_name(stored, folder)
    if stored_name is None or stored_name not in names:
        result.others = len(keys) - len(names)
        result.skipped = UPLOADED_NOT_LISTED
        return []
    older = sorted((name for name in names if name < stored_name), reverse=True)
    keep = {stored_name, *older[: max(retention - 1, 0)]}
    result.kept = len(keep)
    result.others = len(keys) - len(older) - 1
    return [f"{folder}{name}" for name in reversed(older) if name not in keep][:MAX_DELETES_PER_RUN]


async def _delete(client: ObjectStorageClient, folder: str, key: str) -> StorageFailure | None:
    if _archive_name(key, folder) is None:
        return StorageFailure("key_invalid")
    try:
        await client.delete_object(key)
    except StorageFailure as failure:
        return failure
    return None


def _reported_names(folder: str, deleted: list[str]) -> tuple[str, bool]:
    names: list[str] = []
    used = 0
    for key in deleted[:MAX_REPORTED_PATHS]:
        name = key[len(folder) :]
        if used + len(name) + 2 > MAX_REPORTED_CHARS:
            break
        names.append(name)
        used += len(name) + 2
    return ", ".join(names), len(names) < len(deleted)


def _transient(failure: StorageFailure) -> bool:
    return failure.kind == "unreachable" or (
        failure.kind == "storage_error"
        and failure.status is not None
        and failure.status >= SERVER_ERROR
    )


def _discard(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def _database_facts(database: Database) -> tuple[int, int]:
    size = 0
    for suffix in ("", "-wal"):
        try:
            size += database.path.with_name(f"{database.path.name}{suffix}").stat().st_size
        except FileNotFoundError:
            continue
    connection = database.connect()
    try:
        return size, backup_snapshot.instance_counts(connection).stacks
    finally:
        connection.close()
