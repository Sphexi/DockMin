"""Holds uploaded volume archives on disk between their review and the import."""

import asyncio
import hashlib
import os
import secrets
import shutil
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from app.backup import paths
from app.services.activity import completed
from app.services.volume_import_archive import MAX_UPLOAD_BYTES, ArchiveSummary

__all__ = [
    "SPOOL_DIRECTORY",
    "UPLOAD_ID",
    "HeldImport",
    "SpoolFailure",
    "SpooledUpload",
    "VolumeImportStore",
    "spool_upload",
    "sweep_import_leftovers",
    "unlink_quietly",
]

SPOOL_DIRECTORY: Final = "volume-import"
UPLOAD_ID: Final = r"[0-9a-f]{32}"
LIFETIME_SECONDS: Final = 1800.0
CAPACITY: Final = 4
FREE_FLOOR_BYTES: Final = 256 * 1024 * 1024
WRITE_BUFFER_BYTES: Final = 1024 * 1024
SUFFIX: Final = ".tar.gz"
PARTIAL_SUFFIX: Final = ".partial"
_ID_BYTES: Final = 16


class SpoolFailure(Exception):
    """The upload could not be stored; `reason` is `too_large` or `insufficient_space`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SpooledUpload:
    """An upload written to the spool directory."""

    path: Path
    digest: str
    size: int


@dataclass(slots=True)
class HeldImport:
    """A validated upload, usable only by the session, user and environment that made it."""

    id: str
    user_id: int
    session_id: int
    environment_id: int
    path: Path
    digest: str
    size: int
    summary: ArchiveSummary
    expires: float
    importing: bool = False


def sweep_import_leftovers(data_dir: Path, name: str = SPOOL_DIRECTORY) -> None:
    """Remove every spooled upload in the named spool directory; blocking, for startup."""
    directory = data_dir / name
    if directory.is_symlink():
        directory.unlink()
    elif directory.exists():
        shutil.rmtree(directory)


def _free_bytes(directory: Path) -> int:
    return shutil.disk_usage(directory).free


class _Spool:
    def __init__(self, partial: Path) -> None:
        self.handle: BinaryIO = os.fdopen(paths.open_private_file(partial), "wb")
        self.digest = hashlib.sha256()
        self.size = 0
        self.directory = partial.parent


def _append(spool: _Spool, data: bytes, limit: int) -> None:
    if spool.size + len(data) > limit:
        raise SpoolFailure("too_large")
    if _free_bytes(spool.directory) < FREE_FLOOR_BYTES + len(data):
        raise SpoolFailure("insufficient_space")
    spool.handle.write(data)
    spool.digest.update(data)
    spool.size += len(data)


def _abandon(spool: _Spool | None, partial: Path) -> None:
    if spool is not None:
        with suppress(OSError):
            spool.handle.close()
    with suppress(OSError):
        partial.unlink(missing_ok=True)


def _ensure_directory(data_dir: Path, declared: int | None, name: str) -> Path:
    directory = paths.ensure_private_directory(data_dir / name)
    if _free_bytes(directory) < FREE_FLOOR_BYTES + (declared or 0):
        raise SpoolFailure("insufficient_space")
    return directory


def _finish(spool: _Spool, partial: Path, final: Path) -> None:
    spool.handle.flush()
    os.fsync(spool.handle.fileno())
    spool.handle.close()
    partial.replace(final)


async def spool_upload(
    parts: AsyncIterator[bytes],
    data_dir: Path,
    upload_id: str,
    *,
    declared_size: int | None = None,
    limit: int = MAX_UPLOAD_BYTES,
    directory_name: str = SPOOL_DIRECTORY,
) -> SpooledUpload:
    """Write the upload to a private file in the spool directory and return its digest.

    Free space is checked before the first byte, with `declared_size` when the request carries
    one, and again with every write. The file is removed on every way out but success; the
    final name exists only for a complete upload. SpoolFailure, OSError.
    """
    directory = await asyncio.to_thread(_ensure_directory, data_dir, declared_size, directory_name)
    final = directory / f"{upload_id}{SUFFIX}"
    partial = directory / f"{upload_id}{PARTIAL_SUFFIX}"
    spool: _Spool | None = None
    try:
        spool = await completed(asyncio.to_thread(_Spool, partial))
        buffer = bytearray()
        async for chunk in parts:
            buffer += chunk
            if len(buffer) >= WRITE_BUFFER_BYTES:
                await completed(asyncio.to_thread(_append, spool, bytes(buffer), limit))
                buffer.clear()
        if buffer:
            await completed(asyncio.to_thread(_append, spool, bytes(buffer), limit))
        await completed(asyncio.to_thread(_finish, spool, partial, final))
    except BaseException:
        await completed(asyncio.to_thread(_abandon, spool, partial))
        raise
    return SpooledUpload(final, spool.digest.hexdigest(), spool.size)


def unlink_quietly(path: Path) -> None:
    """Remove a spooled file; a file already gone is not an error."""
    with suppress(OSError):
        path.unlink(missing_ok=True)


class VolumeImportStore:
    """In-memory holds on spooled uploads: one per user, four in all, 30 minutes each.

    A hold is dropped, and its file removed, on a newer upload by the same user, on discard, and
    on expiry; a hold whose import has begun does not expire.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._held: OrderedDict[str, HeldImport] = OrderedDict()
        self._timers: set[asyncio.TimerHandle] = set()
        self._sweeps: set[asyncio.Future[None]] = set()

    @staticmethod
    def new_id() -> str:
        """Return a fresh upload id."""
        return secrets.token_hex(_ID_BYTES)

    async def hold(
        self,
        upload_id: str,
        *,
        user_id: int,
        session_id: int,
        environment_id: int,
        upload: SpooledUpload,
        summary: ArchiveSummary,
    ) -> HeldImport:
        """Hold a validated upload, dropping the user's earlier one and the oldest past four."""
        doomed = self._expire()
        held = HeldImport(
            upload_id,
            user_id,
            session_id,
            environment_id,
            upload.path,
            upload.digest,
            upload.size,
            summary,
            self._clock() + LIFETIME_SECONDS,
        )
        for token, other in list(self._held.items()):
            if other.user_id == user_id and not other.importing:
                doomed.append(self._held.pop(token))
        self._held[upload_id] = held
        while len(self._held) > CAPACITY:
            oldest = next((t for t, h in self._held.items() if not h.importing), None)
            if oldest is None:
                break
            doomed.append(self._held.pop(oldest))
        await self._remove(doomed)
        self._arm()
        return held

    async def get(
        self, upload_id: str, *, user_id: int, session_id: int, environment_id: int
    ) -> HeldImport | None:
        """Return the hold when it is live and was made by exactly this session and environment."""
        await self._remove(self._expire())
        held = self._held.get(upload_id)
        if held is None or (held.user_id, held.session_id, held.environment_id) != (
            user_id,
            session_id,
            environment_id,
        ):
            return None
        return held

    async def discard(self, upload_id: str) -> None:
        """Drop a hold and remove its file."""
        held = self._held.pop(upload_id, None)
        if held is not None:
            await self._remove([held])

    async def sweep(self) -> None:
        """Remove what has expired."""
        await self._remove(self._expire())

    async def aclose(self) -> None:
        """Cancel the timers and remove every held file."""
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        held = list(self._held.values())
        self._held.clear()
        await self._remove(held)

    def _expire(self) -> list[HeldImport]:
        now = self._clock()
        gone = [t for t, h in self._held.items() if h.expires <= now and not h.importing]
        return [self._held.pop(token) for token in gone]

    async def _remove(self, held: list[HeldImport]) -> None:
        for item in held:
            await completed(asyncio.to_thread(unlink_quietly, item.path))

    def _arm(self) -> None:
        loop = asyncio.get_running_loop()
        timer: asyncio.TimerHandle | None = None

        def fire() -> None:
            if timer is not None:
                self._timers.discard(timer)
            future = asyncio.ensure_future(self.sweep())
            self._sweeps.add(future)
            future.add_done_callback(self._sweeps.discard)

        timer = loop.call_later(LIFETIME_SECONDS + 1, fire)
        self._timers.add(timer)
