"""Validation and safe extraction of build context archives; blocking, call through to_thread."""

import asyncio
import hashlib
import os
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from app.db.repositories import stack_build_context as limits
from app.services.tar_members import (
    BytesSource,
    TarEntry,
    TarLimits,
    TarRefused,
    TarSource,
    iter_tar_entries,
)

__all__ = [
    "ContextRefused",
    "InspectedContext",
    "extract_context_archive",
    "inspect_context",
    "read_context_file",
    "run_limited",
]

_GZIP_MAGIC: Final = b"\x1f\x8b"
_GZIP_WBITS: Final = 31
_COMPRESSED_STEP: Final = 64 * 1024
_TAR_BYTES_PER_MEMBER: Final = 2048
_TAR_FIXED_BYTES: Final = 1024 * 1024
_FILE_MODE: Final = 0o644
_EXECUTABLE_FILE_MODE: Final = 0o755
_DIRECTORY_MODE: Final = 0o755
_CONCURRENT_ARCHIVES: Final = 2

_slots = asyncio.Semaphore(_CONCURRENT_ARCHIVES)

_MESSAGES: Final = {
    "empty": "The archive holds no files.",
    "upload_too_large": "The archive is larger than the upload limit.",
    "malformed": "The file is not a readable .tar or .tar.gz archive.",
    "too_many_members": "The archive holds more entries than the limit allows.",
    "member_too_large": "A file in the archive is larger than the extracted size limit.",
    "total_too_large": "The archive extracts to more than the extracted size limit.",
    "member_type": "The archive holds an entry that is not a regular file or a directory.",
    "absolute_name": "The archive holds an entry with an absolute name.",
    "parent_component": "The archive holds an entry whose name contains `..`.",
    "name_too_long": "The archive holds an entry with a name that is too long.",
    "bad_name": "The archive holds an entry with an unusable name.",
    "duplicate_name": "The archive holds the same name twice.",
    "name_conflict": "The archive uses a file name as a directory.",
    "digest_mismatch": "The stored archive does not match its digest.",
}


class ContextRefused(Exception):
    """An archive broke a rule; `member` is a printable form of the entry at fault, if any."""

    def __init__(self, reason: str, member: str | None = None) -> None:
        super().__init__(_MESSAGES.get(reason, _MESSAGES["malformed"]))
        self.reason = reason
        self.member = member


@dataclass(frozen=True, slots=True)
class InspectedContext:
    """An archive that passed every check, with what is stored beside it."""

    content: bytes = field(repr=False)
    content_digest: str
    compressed: bool
    member_count: int
    extracted_size: int


async def run_limited[**P, T](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run a blocking archive function in a thread, two archives at a time across the process.

    The slot is held until the thread has ended, even when the caller is cancelled first.
    """
    async with _slots:
        running = asyncio.ensure_future(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(running)
        except asyncio.CancelledError:
            while not running.done():
                with suppress(asyncio.CancelledError):
                    await asyncio.wait({running})
            if not running.cancelled():
                running.exception()
            raise


def _tar_limits() -> TarLimits:
    return TarLimits(
        max_members=limits.MAX_CONTEXT_MEMBERS,
        max_member_bytes=limits.MAX_CONTEXT_EXTRACTED_BYTES,
        max_total_bytes=limits.MAX_CONTEXT_EXTRACTED_BYTES,
    )


class _Inflater:
    """Inflates one gzip member on demand, refusing output past the extracted limit plus framing."""

    def __init__(self, data: bytes) -> None:
        self._view = memoryview(data)
        self._position = 0
        self._inflater = zlib.decompressobj(_GZIP_WBITS)
        self._produced = 0
        self._ceiling = (
            limits.MAX_CONTEXT_EXTRACTED_BYTES
            + limits.MAX_CONTEXT_MEMBERS * _TAR_BYTES_PER_MEMBER
            + _TAR_FIXED_BYTES
        )

    def read(self, size: int, /) -> bytes:
        while not self._inflater.eof:
            pending = self._inflater.unconsumed_tail or self._next_input()
            if not pending:
                raise ContextRefused("malformed")
            try:
                piece = self._inflater.decompress(pending, size)
            except zlib.error:
                raise ContextRefused("malformed") from None
            if piece:
                self._produced += len(piece)
                if self._produced > self._ceiling:
                    raise ContextRefused("total_too_large")
                return piece
        if self._inflater.unused_data or self._position < len(self._view):
            raise ContextRefused("malformed")
        return b""

    def _next_input(self) -> bytes:
        chunk = self._view[self._position : self._position + _COMPRESSED_STEP]
        self._position += len(chunk)
        return bytes(chunk)


def _check_layout(entry: TarEntry, files: set[str], directories: set[str]) -> None:
    """Refuse an entry that makes a name both a file and a directory, in either order."""
    parts = entry.name.split("/")
    for end in range(1, len(parts)):
        prefix = "/".join(parts[:end])
        if prefix in files:
            raise ContextRefused("name_conflict", entry.name)
        directories.add(prefix)
    if not entry.is_directory:
        if entry.name in directories:
            raise ContextRefused("name_conflict", entry.name)
        files.add(entry.name)


@contextmanager
def _refusals() -> Iterator[None]:
    try:
        yield
    except TarRefused as refused:
        raise ContextRefused(refused.reason.value, refused.member) from None


def _entries(data: bytes) -> Iterator[TarEntry]:
    source: TarSource = _Inflater(data) if data[:2] == _GZIP_MAGIC else BytesSource(data)
    files: set[str] = set()
    directories: set[str] = set()
    for entry in iter_tar_entries(source, _tar_limits()):
        _check_layout(entry, files, directories)
        yield entry


def inspect_context(data: bytes) -> InspectedContext:
    """Check an uploaded archive and return it with its digest, member count and extracted size.

    ContextRefused for anything the checks refuse; nothing is extracted.
    """
    if len(data) > limits.MAX_CONTEXT_UPLOAD_BYTES:
        raise ContextRefused("upload_too_large")
    if not data:
        raise ContextRefused("malformed")
    count = 0
    extracted = 0
    with _refusals():
        for entry in _entries(data):
            count += 1
            extracted += entry.size
    if not count or extracted == 0:
        raise ContextRefused("empty")
    return InspectedContext(
        content=data,
        content_digest=hashlib.sha256(data).hexdigest(),
        compressed=data[:2] == _GZIP_MAGIC,
        member_count=count,
        extracted_size=extracted,
    )


def extract_context_archive(data: bytes, destination: Path, *, digest: str | None = None) -> None:
    """Write a stored archive's files and directories under an empty directory, re-validated.

    The archive passes the same checks as an upload while it is read, and `digest`, when given,
    must match it. Files get mode 0644, or 0755 when the archive set an execute bit; directories
    0755. A refusal or a failure while writing leaves the part already written for the caller to
    remove with the directory.
    """
    if digest is not None and hashlib.sha256(data).hexdigest() != digest:
        raise ContextRefused("digest_mismatch")
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise ValueError("a context is extracted into an empty directory")
    made: set[str] = set()
    with _refusals():
        for entry in _entries(data):
            parts = entry.name.split("/")
            for end in range(1, len(parts) + (1 if entry.is_directory else 0)):
                _make_directory(destination, "/".join(parts[:end]), made)
            if not entry.is_directory:
                _write_file(destination / entry.name, entry)


def read_context_file(
    data: bytes, path: str, *, digest: str | None = None, max_bytes: int | None = None
) -> bytes | None:
    """Return the content of one regular file of a stored archive, or None when it has none.

    The archive passes the same checks as an upload up to the file, and `digest`, when given,
    must match it. A file longer than `max_bytes` comes back cut to `max_bytes + 1` bytes.
    """
    if digest is not None and hashlib.sha256(data).hexdigest() != digest:
        raise ContextRefused("digest_mismatch")
    with _refusals():
        for entry in _entries(data):
            if entry.name == path and not entry.is_directory:
                limit = None if max_bytes is None else max_bytes + 1
                return b"".join(entry.chunks(limit))
    return None


def _make_directory(root: Path, relative: str, made: set[str]) -> None:
    if relative in made:
        return
    path = root / relative
    path.mkdir()
    path.chmod(_DIRECTORY_MODE)
    made.add(relative)


def _write_file(path: Path, entry: TarEntry) -> None:
    mode = _EXECUTABLE_FILE_MODE if entry.executable else _FILE_MODE
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(descriptor, mode)
        for chunk in entry.chunks():
            view = memoryview(chunk)
            while view:
                view = view[os.write(descriptor, view) :]
    finally:
        os.close(descriptor)
