"""Backup archive production for downloads, the production lock, and the download stream."""

import asyncio
import shutil
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import BinaryIO, Final, Self

from app.auth.errors import ProductError
from app.auth.principal import Principal
from app.backup import ProducedArchive, ProductionFailed, paths, produce_archive
from app.backup.manifest import ProductionReason
from app.crypto.vault import Vault
from app.db.engine import Database, Session
from app.db.repositories.backup_record import BackupRecord, BackupRecordRepository
from app.db.repositories.clocked import Clock
from app.db.repositories.pagination import Page
from app.logging import get_logger
from app.services.activity import ActivityRecorder, audited, completed, current_audit
from app.services.guards import require_administrator

logger = get_logger(__name__)

CHUNK: Final = 1024 * 1024
CONTENT_TYPE: Final = "application/gzip"
BUSY_MESSAGE: Final = "A backup or a restore is in progress. Try again when it has finished."
REKEY_BUSY_MESSAGE: Final = "Stored secrets are being re-encrypted. Try again when it has finished."


class ProductionBusy(ProductError):
    """Another production holds the lock, or a restore does."""


class BackupFailed(ProductError):
    """An archive could not be produced; the message is product text."""

    def __init__(self, reason: ProductionReason, message: str) -> None:
        super().__init__(message)
        self.reason: ProductionReason = reason


class BackupProduction:
    """The one-at-a-time lock over archive production, and where its work directories live."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._held = False
        self._for_rekey = False
        self._freed = asyncio.Event()
        self._freed.set()

    @property
    def data_dir(self) -> Path:
        """Return the data directory the work directories are created under."""
        return self._data_dir

    @property
    def held(self) -> bool:
        """Report whether a production or a restore holds the lock."""
        return self._held

    @property
    def busy_message(self) -> str:
        """Return the refusal text for a caller that finds the lock held."""
        return REKEY_BUSY_MESSAGE if self._for_rekey else BUSY_MESSAGE

    def try_hold(self, *, for_rekey: bool = False) -> bool:
        """Take the lock if it is free; report whether it was taken."""
        if self._held:
            return False
        self._held = True
        self._for_rekey = for_rekey
        self._freed.clear()
        return True

    def free(self) -> None:
        """Give the lock back and wake whoever waits for it."""
        self._held = False
        self._for_rekey = False
        self._freed.set()

    async def hold(self, *, for_rekey: bool = False) -> None:
        """Wait for the lock and take it; the caller frees it."""
        while not self.try_hold(for_rekey=for_rekey):
            await self._freed.wait()

    @asynccontextmanager
    async def holding(self) -> AsyncIterator[None]:
        """Wait for the lock and hold it for the block."""
        await self.hold()
        try:
            yield
        finally:
            self.free()


def _discard(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def _open_archive(path: Path) -> BinaryIO:
    return path.open("rb")


class DownloadStream:
    """The bytes of a finished archive, read in chunks; the work directory goes with the stream."""

    def __init__(self, produced: ProducedArchive, workdir: Path) -> None:
        self.file_name = produced.file_name
        self.size = produced.size
        self.sha256 = produced.sha256
        self.content_type = CONTENT_TYPE
        self._path = produced.path
        self._workdir = workdir
        self._handle: BinaryIO | None = None
        self._closed = False
        self._finalizer = weakref.finalize(self, _discard, workdir)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        try:
            if self._handle is None:
                self._handle = await asyncio.to_thread(_open_archive, self._path)
            chunk = await asyncio.to_thread(self._handle.read, CHUNK)
        except BaseException:
            await completed(self.aclose())
            raise
        if not chunk:
            await self.aclose()
            raise StopAsyncIteration
        return chunk

    async def aclose(self) -> None:
        """Close the file and remove the work directory; safe to call twice."""
        if self._closed:
            return
        self._closed = True
        handle, self._handle = self._handle, None
        try:
            if handle is not None:
                await asyncio.to_thread(handle.close)
        finally:
            self._finalizer.detach()
            await asyncio.to_thread(_discard, self._workdir)


class BackupService:
    """Produces archives on demand and lists the records of the ones made; administrators only."""

    def __init__(
        self,
        db: Session,
        vault: Vault,
        database: Database,
        production: BackupProduction,
        clock: Clock,
        activity: ActivityRecorder,
    ) -> None:
        self._db = db
        self._vault = vault
        self._database = database
        self._production = production
        self._clock = clock
        self._activity = activity

    async def records(
        self, actor: Principal, *, limit: int, cursor: str | None = None
    ) -> Page[BackupRecord]:
        """Return one page of backup records, newest first."""
        require_administrator(actor)
        return await BackupRecordRepository(self._db, self._clock).page_backup_records(
            limit=limit, cursor=cursor
        )

    @audited(action="backup_downloaded", target_type="backup")
    async def download(self, actor: Principal) -> DownloadStream:
        """Produce an archive and return it as a stream for the caller to send.

        Raises ProductionBusy while another production, a restore or a re-key holds the lock, and
        BackupFailed when the archive cannot be produced. The caller closes the stream.
        """
        require_administrator(actor)
        audit = current_audit()
        if not self._production.try_hold():
            audit.refuse()
            raise ProductionBusy(self._production.busy_message)
        workdir: Path | None = None
        delivered = False
        try:
            workdir = await asyncio.to_thread(paths.new_work_directory, self._production.data_dir)
            try:
                produced = await produce_archive(
                    self._database, self._vault, workdir, clock=self._clock
                )
            except ProductionFailed as failure:
                audit.reason = failure.reason
                await self._note_failure(failure)
                raise BackupFailed(failure.reason, failure.message) from None
            manifest = produced.manifest
            async with self._db.transaction():
                record_id = await BackupRecordRepository(self._db, self._clock).add_backup_record(
                    origin="download",
                    target_id=None,
                    size_bytes=produced.size,
                    key_version=manifest.key_version,
                    key_fingerprint=manifest.key_fingerprint,
                    destination_key=None,
                    archive_sha256=produced.sha256,
                    schema_version=manifest.schema_version,
                    outcome="success",
                    error=None,
                )
                audit.detail.update(
                    archive_sha256=produced.sha256,
                    size_bytes=produced.size,
                    schema_version=manifest.schema_version,
                    key_version=manifest.key_version,
                    key_fingerprint=manifest.key_fingerprint,
                    backup_record_id=record_id,
                )
            delivered = True
            return DownloadStream(produced, workdir)
        finally:
            self._production.free()
            if workdir is not None and not delivered:
                await completed(asyncio.to_thread(_discard, workdir))

    async def _note_failure(self, failure: ProductionFailed) -> None:
        """Record the failed attempt on a session of its own, apart from the audit scope's."""
        try:
            async with self._database.session() as db:
                await BackupRecordRepository(db, self._clock).add_backup_record(
                    origin="download",
                    target_id=None,
                    size_bytes=None,
                    key_version=self._vault.current_version,
                    key_fingerprint=self._vault.key_fingerprint(),
                    destination_key=None,
                    archive_sha256=None,
                    schema_version=None,
                    outcome="failure",
                    error=failure.message,
                )
        except Exception as error:
            logger.error("backup_record_write_failed", extra={"error": type(error).__name__})
