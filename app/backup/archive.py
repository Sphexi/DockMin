"""Writes a backup archive through the key scanner and reads one without extracting it."""

import base64
import gzip
import hashlib
import io
import os
import shutil
import stat
import tarfile
import zlib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import IO, Final, NamedTuple, cast

from app.backup import paths
from app.backup.manifest import (
    ARCHIVE_MEMBER,
    DATABASE_NAME,
    MANIFEST_NAME,
    MAX_ARCHIVE_BYTES,
    MAX_COMPOSE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_STACK_MEMBERS,
    ArchiveRefused,
    Manifest,
    MemberEntry,
    ProductionFailed,
    RestoreReason,
    decode_manifest,
)
from app.backup.parts import MULTIPART_PART_BYTES, PartDigest
from app.crypto.keys import KeyScanner
from app.logging import get_logger

CHUNK: Final = 1024 * 1024
BLOCK: Final = 512
STREAM_OVERHEAD: Final = 16 * 1024 * 1024
MAX_TRAILING_ZEROS: Final = 1024 * 1024
STAGING_HEADROOM: Final = 64 * 1024 * 1024
MEMBER_MODE: Final = 0o600

_ZERO_BLOCK = bytes(BLOCK)
_REGULAR_TYPES = (b"0", b"\0")
_NAME_END = 100
_SIZE_FIELD = slice(124, 136)
_CHECKSUM_FIELD = slice(148, 156)
_TYPE_FIELD = slice(156, 157)
_LINK_FIELD = slice(157, 257)
_MAGIC_FIELD = slice(257, 262)
_PREFIX_FIELD = slice(345, 500)
_CHECKSUM_PLACEHOLDER = 8 * 0x20
_TYPE_NAMES = {
    b"1": "a hard link",
    b"2": "a symbolic link",
    b"3": "a device file",
    b"4": "a device file",
    b"5": "a directory",
    b"6": "a named pipe",
}

logger = get_logger(__name__)


class StopRequested(Exception):
    """The caller asked a long read to stop between chunks."""


def _invalid(message: str) -> ArchiveRefused:
    return ArchiveRefused("archive_invalid", message)


class FileDigests(NamedTuple):
    """A file's whole-file digests and, when it spans several parts, each part's."""

    sha256: str
    md5_b64: str
    size: int
    parts: tuple[PartDigest, ...]


def hash_file(
    path: Path, *, with_parts: bool = False, part_bytes: int = MULTIPART_PART_BYTES
) -> FileDigests:
    """Return a file's SHA-256 hex, base64 MD5 and size, read in one pass.

    With with_parts, a file larger than part_bytes also yields the digests of each part.
    """
    sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    size = 0
    parts: list[PartDigest] = []
    with path.open("rb") as handle:
        split = with_parts and os.fstat(handle.fileno()).st_size > part_bytes
        part = _PartHash()
        while chunk := handle.read(min(CHUNK, part_bytes - part.size) if split else CHUNK):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
            if split:
                part.absorb(chunk)
                if part.size == part_bytes:
                    parts.append(part.finish(size - part.size))
                    part = _PartHash()
        if split and part.size:
            parts.append(part.finish(size - part.size))
    whole_md5 = base64.b64encode(md5.digest()).decode("ascii")
    return FileDigests(sha.hexdigest(), whole_md5, size, tuple(parts))


class _PartHash:
    def __init__(self) -> None:
        self._sha = hashlib.sha256()
        self._md5 = hashlib.md5(usedforsecurity=False)
        self.size = 0

    def absorb(self, chunk: bytes) -> None:
        self._sha.update(chunk)
        self._md5.update(chunk)
        self.size += len(chunk)

    def finish(self, offset: int) -> PartDigest:
        md5_b64 = base64.b64encode(self._md5.digest()).decode("ascii")
        return PartDigest(offset, self.size, self._sha.hexdigest(), md5_b64)


def stack_member_name(project_directory: str) -> str:
    """Return the archive member name of a stack's compose file."""
    return f"stacks/{project_directory}/compose.yaml"


def inventory(snapshot: Path, stacks: Sequence[tuple[str, bytes]]) -> tuple[MemberEntry, ...]:
    """Return the manifest inventory of a snapshot and stack files, in archive order."""
    if len(stacks) > MAX_STACK_MEMBERS:
        raise ProductionFailed("production_failed", "There are too many stacks to back up.")
    database = hash_file(snapshot)
    entries = [MemberEntry(DATABASE_NAME, database.size, database.sha256)]
    for project_directory, compose in sorted(stacks):
        if not 1 <= len(compose) <= MAX_COMPOSE_BYTES:
            raise ProductionFailed("production_failed", "A stack file has an impossible size.")
        name = stack_member_name(project_directory)
        entries.append(MemberEntry(name, len(compose), hashlib.sha256(compose).hexdigest()))
    if sum(entry.size for entry in entries) + MAX_MANIFEST_BYTES > MAX_ARCHIVE_BYTES:
        raise ProductionFailed("production_failed", "The backup is larger than the archive limit.")
    return tuple(entries)


class _ScanningSink:
    """Passes the uncompressed tar stream to the key scanner on its way to the compressor."""

    def __init__(self, target: gzip.GzipFile, scanner: KeyScanner) -> None:
        self._target = target
        self._scanner = scanner
        self.member = MANIFEST_NAME
        self.total = 0

    def write(self, data: bytes) -> int:
        self._scanner.feed(data)
        if self._scanner.found:
            logger.error("backup_key_material_found", extra={"member": self.member})
            raise ProductionFailed(
                "key_material_found", "The backup was refused because it contained key material."
            )
        self.total += len(data)
        return self._target.write(data)


def _tar_info(name: str, size: int, mtime: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = MEMBER_MODE
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = mtime
    info.type = tarfile.REGTYPE
    return info


def write_archive(
    destination: Path,
    *,
    manifest: bytes,
    members: Sequence[MemberEntry],
    snapshot: Path,
    stacks: Sequence[tuple[str, bytes]],
    mtime: int,
    scanner: KeyScanner,
) -> None:
    """Write the gzipped tar of manifest, snapshot and stack files to a new 0600 file.

    Member names come only from the allow-list. Raises ProductionFailed when the scanner finds
    key material or a source changed after it was inventoried.
    """
    compose_by_name = {stack_member_name(directory): data for directory, data in stacks}
    if [member.name for member in members[1:]] != sorted(compose_by_name):
        raise ProductionFailed(
            "production_failed", "The backup inventory does not match its files."
        )
    descriptor = paths.open_private_file(destination)
    with (
        os.fdopen(descriptor, "wb") as raw,
        gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=mtime
        ) as compressed,
    ):
        sink = _ScanningSink(compressed, scanner)
        with tarfile.open(
            fileobj=cast(IO[bytes], sink), mode="w|", format=tarfile.PAX_FORMAT
        ) as tar:
            _add_bytes(tar, sink, MANIFEST_NAME, manifest, mtime)
            sink.member = DATABASE_NAME
            _add_snapshot(tar, snapshot, members[0], mtime)
            for member in members[1:]:
                sink.member = member.name
                _add_bytes(tar, sink, member.name, compose_by_name[member.name], mtime)
        raw.flush()
    if destination.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ProductionFailed("production_failed", "The backup is larger than the archive limit.")


def _add_bytes(
    tar: tarfile.TarFile, sink: _ScanningSink, name: str, data: bytes, mtime: int
) -> None:
    sink.member = name
    tar.addfile(_tar_info(name, len(data), mtime), io.BytesIO(data))


def _add_snapshot(tar: tarfile.TarFile, snapshot: Path, member: MemberEntry, mtime: int) -> None:
    with snapshot.open("rb") as handle:
        tar.addfile(_tar_info(DATABASE_NAME, member.size, mtime), handle)
    if hash_file(snapshot).sha256 != member.sha256:
        raise ProductionFailed("production_failed", "The database snapshot changed while writing.")


class _Entry:
    """One member of an archive being read: its name, declared size and a one-shot data stream."""

    def __init__(self, reader: "_Reader", name: str, size: int) -> None:
        self.name = name
        self.size = size
        self._reader = reader
        self._remaining = size

    def chunks(self) -> Iterator[bytes]:
        """Yield the member's data in chunks, raising when the archive ends early."""
        while self._remaining:
            chunk = self._reader.take(min(CHUNK, self._remaining))
            if not chunk:
                raise self._reader.damaged_error()
            self._remaining -= len(chunk)
            yield chunk

    def drain(self) -> None:
        """Read past whatever the caller did not consume."""
        for _ in self.chunks():
            pass


class _Reader:
    """Reads an untrusted gzipped ustar stream of regular files named from the allow-list."""

    def __init__(
        self, archive: Path, damaged: RestoreReason, stop: Callable[[], bool] | None = None
    ) -> None:
        self._archive = archive
        self._damaged = damaged
        self._stop = stop
        self._limit = MAX_ARCHIVE_BYTES + STREAM_OVERHEAD
        self._total = 0
        self._file: gzip.GzipFile | None = None

    def __enter__(self) -> "_Reader":
        try:
            status = self._archive.lstat()
        except OSError:
            raise _invalid("The archive file cannot be read.") from None
        if not stat.S_ISREG(status.st_mode):
            raise _invalid("The archive is not a regular file.")
        if status.st_size > MAX_ARCHIVE_BYTES:
            raise _invalid("The archive is larger than the size limit.")
        self._file = gzip.GzipFile(filename=str(self._archive), mode="rb")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._file is not None:
            self._file.close()

    def damaged_error(self) -> ArchiveRefused:
        """Return the refusal for a stream that is truncated or fails decompression."""
        return ArchiveRefused(self._damaged, "The archive is damaged or incomplete.")

    def take(self, count: int) -> bytes:
        """Read up to count uncompressed bytes; fewer only at the end of the stream."""
        assert self._file is not None
        if self._stop is not None and self._stop():
            raise StopRequested
        try:
            data = self._file.read(count)
        except (EOFError, OSError, zlib.error):
            raise self.damaged_error() from None
        self._total += len(data)
        if self._total > self._limit:
            raise _invalid("The archive expands beyond the size limit.")
        return data

    def _skip(self, count: int) -> None:
        while count:
            chunk = self.take(min(CHUNK, count))
            if not chunk:
                raise self.damaged_error()
            count -= len(chunk)

    def _header(self, block: bytes) -> tuple[str, int]:
        if _octal(block[_CHECKSUM_FIELD]) != (
            sum(block[: _CHECKSUM_FIELD.start]) + _CHECKSUM_PLACEHOLDER + sum(block[156:])
        ):
            raise _invalid("A member header is damaged.")
        kind = block[_TYPE_FIELD]
        if kind not in _REGULAR_TYPES:
            what = _TYPE_NAMES.get(kind, "an entry that is not a plain file")
            raise _invalid(f"The archive contains {what}.")
        if block[_PREFIX_FIELD].strip(b"\0") or block[_LINK_FIELD].strip(b"\0"):
            raise _invalid("A member header carries a path prefix or link target.")
        if block[_MAGIC_FIELD] != b"ustar":
            raise _invalid("A member header is not in tar format.")
        raw_name = block[:_NAME_END].rstrip(b"\0")
        try:
            name = raw_name.decode("ascii")
        except UnicodeDecodeError:
            raise _invalid("The archive contains a member that is not part of a backup.") from None
        if b"\0" in raw_name or ARCHIVE_MEMBER.fullmatch(name) is None:
            raise _invalid("The archive contains a member that is not part of a backup.")
        return name, _octal(block[_SIZE_FIELD])

    def entries(self) -> Iterator[_Entry]:
        """Yield each member in archive order, enforcing names, order, sizes and the end marker."""
        previous = (-1, "")
        stacks = declared = 0
        while True:
            block = self.take(BLOCK)
            if len(block) < BLOCK:
                raise self.damaged_error()
            if block == _ZERO_BLOCK:
                self._finish()
                return
            name, size = self._header(block)
            rank = _rank(name)
            if rank <= previous:
                raise _invalid("The archive members are out of order or repeated.")
            previous = rank
            if size > _cap(name):
                raise _invalid("The archive contains a member larger than its limit.")
            stacks += rank[0] == 2
            declared += size
            if stacks > MAX_STACK_MEMBERS or declared > MAX_ARCHIVE_BYTES:
                raise _invalid("The archive contains more than the limits allow.")
            entry = _Entry(self, name, size)
            yield entry
            entry.drain()
            self._skip(-size % BLOCK)

    def _finish(self) -> None:
        trailing = 0
        while chunk := self.take(CHUNK):
            trailing += len(chunk)
            if chunk.count(0) != len(chunk) or trailing > MAX_TRAILING_ZEROS:
                raise _invalid("The archive has data after its end marker.")


def _octal(field: bytes) -> int:
    if field[0] & 0x80:
        raise _invalid("A member header uses an unsupported number format.")
    text = field.strip(b"\0 ")
    if not text or text.strip(b"01234567"):
        raise _invalid("A member header is damaged.")
    return int(text, 8)


def _rank(name: str) -> tuple[int, str]:
    if name == MANIFEST_NAME:
        return (0, "")
    if name == DATABASE_NAME:
        return (1, "")
    return (2, name)


def _cap(name: str) -> int:
    if name == MANIFEST_NAME:
        return MAX_MANIFEST_BYTES
    if name == DATABASE_NAME:
        return MAX_ARCHIVE_BYTES
    return MAX_COMPOSE_BYTES


def read_manifest(archive: Path) -> tuple[Manifest, bytes]:
    """Return the decoded manifest and its raw bytes from the archive's first member."""
    with _Reader(archive, "archive_invalid") as reader:
        first = next(reader.entries(), None)
        if first is None or first.name != MANIFEST_NAME:
            raise _invalid("The archive does not begin with a manifest.")
        raw = b"".join(first.chunks())
    return decode_manifest(raw), raw


def _members_in_step(reader: _Reader, manifest: Manifest) -> Iterator[tuple[MemberEntry, _Entry]]:
    entries = reader.entries()
    first = next(entries, None)
    if first is None or first.name != MANIFEST_NAME:
        raise _invalid("The archive does not begin with a manifest.")
    if decode_manifest(b"".join(first.chunks())) != manifest:
        raise ArchiveRefused("digest_mismatch", "The manifest differs from the one reviewed.")
    for expected in manifest.members:
        entry = next(entries, None)
        if entry is None:
            raise ArchiveRefused("digest_mismatch", f"The archive ends before {expected.name}.")
        if entry.name != expected.name:
            raise ArchiveRefused(
                "digest_mismatch", f"{entry.name} is where the manifest lists {expected.name}."
            )
        if entry.size > expected.size:
            raise _invalid(f"{entry.name} is larger than the manifest declares.")
        if entry.size != expected.size:
            raise ArchiveRefused(
                "digest_mismatch", f"{entry.name} is shorter than the manifest declares."
            )
        yield expected, entry
    if next(entries, None) is not None:
        raise _invalid("The archive contains members the manifest does not list.")


def _consume(expected: MemberEntry, entry: _Entry, sink: Callable[[bytes], object] | None) -> None:
    digest = hashlib.sha256()
    for chunk in entry.chunks():
        digest.update(chunk)
        if sink is not None:
            sink(chunk)
    if digest.hexdigest() != expected.sha256:
        raise ArchiveRefused("digest_mismatch", f"The digest of {expected.name} does not match.")


def verify_members(
    archive: Path, manifest: Manifest, *, stop: Callable[[], bool] | None = None
) -> None:
    """Check every member's name, size and SHA-256 against the manifest without writing anything.

    Raises StopRequested between chunks once stop returns true.
    """
    with _Reader(archive, "digest_mismatch", stop) as reader:
        for expected, entry in _members_in_step(reader, manifest):
            _consume(expected, entry, None)


def stage_members(
    archive: Path, manifest: Manifest, staging: Path, *, stop: Callable[[], bool] | None = None
) -> None:
    """Write the database and stack files into an empty staging directory at paths built here.

    Raises paths.UnsafePath when staging is a symlink or not empty, and StopRequested between
    chunks once stop returns true. Every member is digested again while it is written.
    """
    paths.ensure_private_directory(staging)
    if any(staging.iterdir()):
        raise paths.UnsafePath("the staging directory is not empty")
    needed = 2 * sum(member.size for member in manifest.members) + STAGING_HEADROOM
    if shutil.disk_usage(staging).free < needed:
        raise ArchiveRefused(
            "insufficient_space",
            "There is not enough free space on the data volume to stage the archive.",
        )
    stacks = paths.ensure_private_directory(staging / paths.STACKS_DIRECTORY)
    with _Reader(archive, "digest_mismatch", stop) as reader:
        for expected, entry in _members_in_step(reader, manifest):
            if expected.name == DATABASE_NAME:
                target = staging / paths.STAGED_DATABASE
            else:
                directory = paths.ensure_private_directory(stacks / expected.name.split("/")[1])
                target = directory / "compose.yaml"
            descriptor = paths.open_private_file(target)
            with os.fdopen(descriptor, "wb") as handle:
                _consume(expected, entry, handle.write)
                handle.flush()
                os.fsync(handle.fileno())
