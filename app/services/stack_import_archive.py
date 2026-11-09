"""Validates a stack export archive and streams each volume's data for the Engine; blocking."""

import gzip
import hashlib
import tarfile
import zlib
from collections.abc import Generator, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from app.services.export_manifest import (
    MANIFEST_MAX_BYTES,
    MANIFEST_NAME,
    VOLUMES_DIRECTORY,
    CompanionFile,
    ContextEntry,
    ExportFormatError,
    ExportManifest,
    MemberRef,
    parse_manifest,
)
from app.services.volume_export_writer import ChunkSource, ReadEntry, StreamMalformed, TarReader
from app.services.volume_import_archive import (
    MAX_EXTRACTED_BYTES,
    MAX_MEMBERS,
    ArchiveLimits,
    ArchiveSummary,
    Member,
    VolumeArchiveRefused,
    VolumeChecker,
    read_skipped,
    rewrite_members,
)
from app.services.volume_manifest import FORMAT_VERSION as VOLUME_FORMAT_VERSION
from app.services.volume_manifest import MANIFEST_NAME as VOLUME_MANIFEST_NAME
from app.services.volume_manifest import SKIPPED_NAME, VolumeManifest

__all__ = [
    "MAX_UPLOAD_BYTES",
    "StackArchiveRefused",
    "StackArchiveSummary",
    "StackLimits",
    "VolumeSummary",
    "inspect_stack_archive",
    "read_contexts",
    "stack_volume_tar",
]

MAX_UPLOAD_BYTES: Final = 7 * 1024**3
_GZIP_MAGIC: Final = b"\x1f\x8b"
_READ_BYTES: Final = 256 * 1024
_DOCUMENT_CHUNK: Final = 1024 * 1024
_VOLUMES_PREFIX: Final = f"{VOLUMES_DIRECTORY}/"
_VOLUME_MANIFEST_MAX: Final = 1024 * 1024
_SKIPPED_MAX: Final = 16 * 1024 * 1024
_DOCUMENTS_ALLOWANCE: Final = 128 * 1024 + 20 * 65_536 + 64 * 1024 * 1024
_REGULAR: Final = (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.CONTTYPE)
_DISPLAY_CHARS: Final = 80


class StackArchiveRefused(Exception):
    """The archive broke a rule; `member` is a printable, truncated form of the entry at fault."""

    def __init__(
        self, message: str, member: str | None = None, *, version: int | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.member = None if member is None else _display(member)
        self.version = version


@dataclass(frozen=True, slots=True)
class StackLimits:
    """Bounds on a stack archive: entries and file data across every volume."""

    max_members: int = MAX_MEMBERS
    max_bytes: int = MAX_EXTRACTED_BYTES

    @property
    def volumes(self) -> ArchiveLimits:
        """Return the bounds applied to the volumes together."""
        return ArchiveLimits(self.max_members, self.max_bytes)

    @property
    def ceiling(self) -> int:
        """The most decompressed bytes read before the archive is refused."""
        return (
            self.volumes.ceiling
            + _DOCUMENTS_ALLOWANCE
            + MANIFEST_MAX_BYTES
            + 64 * (_VOLUME_MANIFEST_MAX + _SKIPPED_MAX)
        )


@dataclass(frozen=True, slots=True)
class VolumeSummary:
    """One volume of the archive and what its data holds."""

    prefix: str
    manifest: VolumeManifest
    summary: ArchiveSummary


@dataclass(frozen=True, slots=True)
class StackArchiveSummary:
    """What a validated stack archive holds. File contents and the stack file are kept here."""

    manifest: ExportManifest
    compose_text: str = field(repr=False)
    files: dict[str, str] = field(repr=False)
    volumes: tuple[VolumeSummary, ...]

    @property
    def data_bytes(self) -> int:
        """Return the file data of every volume."""
        return sum(item.summary.bytes for item in self.volumes)


def _display(name: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in name)[:_DISPLAY_CHARS]


def _entries(path: Path, limits: StackLimits) -> Generator[ReadEntry, None, None]:
    """Yield each tar entry of the archive in order; its body must be read before the next."""
    produced = 0
    try:
        with path.open("rb") as raw:
            if raw.read(len(_GZIP_MAGIC)) != _GZIP_MAGIC:
                raise StackArchiveRefused("The file is not a gzip archive.")
            raw.seek(0)
            with gzip.GzipFile(fileobj=raw, mode="rb") as unzipped:

                def pull() -> bytes | None:
                    nonlocal produced
                    chunk = unzipped.read(_READ_BYTES)
                    produced += len(chunk)
                    if produced > limits.ceiling:
                        raise StackArchiveRefused("The archive extracts to more than the limit.")
                    return chunk or None

                source = ChunkSource(pull)
                reader = TarReader(source)
                while (entry := reader.next()) is not None:
                    yield entry
                source.finish()
    except (OSError, EOFError, zlib.error, StreamMalformed):
        raise StackArchiveRefused("The file is not a readable export archive.") from None


def _from_format(error: ExportFormatError) -> StackArchiveRefused:
    return StackArchiveRefused(str(error), version=error.version)


def _from_volume(index: str, error: VolumeArchiveRefused) -> StackArchiveRefused:
    return StackArchiveRefused(f"Volume {index}: {error.message}", error.member, version=None)


def _regular(entry: ReadEntry) -> bool:
    return entry.info.type in _REGULAR


def _read_whole(entry: ReadEntry, limit: int, name: str) -> bytes:
    if not _regular(entry) or entry.info.size > limit:
        raise StackArchiveRefused("The archive holds a document that is not usable.", name)
    try:
        return entry.body.read()
    except StreamMalformed:
        raise StackArchiveRefused("The file is not a readable export archive.") from None


def _read_manifest(entries: Iterator[ReadEntry]) -> ExportManifest:
    first = next(entries, None)
    if first is None or first.info.name != MANIFEST_NAME or not _regular(first):
        raise StackArchiveRefused("The archive does not start with manifest.json.")
    if first.info.size > MANIFEST_MAX_BYTES:
        raise StackArchiveRefused("The archive's manifest.json is too large.")
    raw = _read_whole(first, MANIFEST_MAX_BYTES, MANIFEST_NAME)
    try:
        return parse_manifest(raw)
    except ExportFormatError as error:
        raise _from_format(error) from None


def _document_digest(entry: ReadEntry, ref: MemberRef | CompanionFile | ContextEntry) -> bytes:
    """Read a declared document, checking its size and SHA-256; return it."""
    name = entry.info.name
    if not _regular(entry) or entry.info.size != ref.size:
        raise StackArchiveRefused("The archive's members do not match its manifest.", name)
    digest = hashlib.sha256()
    parts: list[bytes] = []
    left = ref.size
    try:
        while left:
            chunk = entry.body.read(min(_DOCUMENT_CHUNK, left))
            if not chunk:
                raise StackArchiveRefused("The file is not a readable export archive.")
            left -= len(chunk)
            digest.update(chunk)
            parts.append(chunk)
    except StreamMalformed:
        raise StackArchiveRefused("The file is not a readable export archive.") from None
    if digest.hexdigest() != ref.sha256:
        raise StackArchiveRefused("The archive's members do not match its manifest.", name)
    return b"".join(parts)


def _decoded(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise StackArchiveRefused("A document of the archive is not UTF-8 text.", name) from None


def _read_documents(
    manifest: ExportManifest, entries: Iterator[ReadEntry]
) -> tuple[str, dict[str, str], ReadEntry | None]:
    """Read the stack file and plain companion files; check every declared document is there.

    Returns the first entry below `volumes/`, which ended the documents, or None.
    """
    declared = manifest.documents()
    stack = manifest.stack
    if stack is None:
        raise StackArchiveRefused("The archive does not hold a stack.")
    seen: set[str] = set()
    compose = ""
    files: dict[str, str] = {}
    pending: ReadEntry | None = None
    for entry in entries:
        name = entry.info.name
        if name.startswith(_VOLUMES_PREFIX):
            pending = entry
            break
        ref = declared.get(name)
        if ref is None or name in seen:
            raise StackArchiveRefused("The archive's members do not match its manifest.", name)
        seen.add(name)
        if isinstance(ref, ContextEntry):
            _document_digest(entry, ref)
            continue
        text = _decoded(_document_digest(entry, ref), name)
        if isinstance(ref, CompanionFile):
            files[ref.name] = text
        else:
            compose = text
    if seen != declared.keys():
        raise StackArchiveRefused("The archive's members do not match its manifest.")
    return compose, files, pending


def _split(name: str) -> tuple[str, str]:
    parts = name.split("/", 2)
    if len(parts) < 3 or parts[0] != VOLUMES_DIRECTORY:
        raise StackArchiveRefused("The archive's members do not match its manifest.", name)
    return f"{parts[0]}/{parts[1]}", parts[2]


def _nested(entry: ReadEntry, prefix: str, rest: str) -> ReadEntry:
    info = entry.info.replace(name=rest, deep=False)
    if info.type == tarfile.LNKTYPE and info.linkname.startswith(f"{prefix}/"):
        info.linkname = info.linkname[len(prefix) + 1 :]
    return ReadEntry(info, entry.body)


@dataclass(slots=True)
class _Current:
    prefix: str
    manifest: VolumeManifest
    checker: VolumeChecker
    seen_manifest: bool = False
    finished: bool = False


def _volume_manifest(entry: ReadEntry, section: VolumeManifest, prefix: str) -> None:
    raw = _read_whole(entry, _VOLUME_MANIFEST_MAX, f"{prefix}/{VOLUME_MANIFEST_NAME}")
    try:
        parsed = VolumeManifest.model_validate_json(raw)
    except ValidationError:
        raise StackArchiveRefused(
            "A volume manifest of the archive is not valid.", f"{prefix}/{VOLUME_MANIFEST_NAME}"
        ) from None
    if parsed.format_version > VOLUME_FORMAT_VERSION or parsed != section:
        raise StackArchiveRefused(
            "A volume manifest of the archive differs from the archive's manifest.",
            f"{prefix}/{VOLUME_MANIFEST_NAME}",
        )


def _check_volumes(
    manifest: ExportManifest,
    pending: ReadEntry | None,
    entries: Iterator[ReadEntry],
    limits: StackLimits,
) -> tuple[VolumeSummary, ...]:
    order = [section.prefix for section in manifest.volumes]
    sections = {section.prefix: section.manifest for section in manifest.volumes}
    done: list[VolumeSummary] = []
    current: _Current | None = None
    used_members = 0
    used_bytes = 0

    def finish(volume: _Current) -> None:
        nonlocal used_members, used_bytes
        if not volume.seen_manifest:
            raise StackArchiveRefused(
                "A volume of the archive has no manifest.", f"{volume.prefix}/manifest.json"
            )
        index = volume.prefix.rpartition("/")[2]
        try:
            summary = volume.checker.summary(volume.manifest, require_data=False)
        except VolumeArchiveRefused as refused:
            raise _from_volume(index, refused) from None
        used_members += volume.checker.layout.members
        used_bytes += summary.bytes
        done.append(VolumeSummary(volume.prefix, volume.manifest, summary))

    def chained() -> Iterator[ReadEntry]:
        if pending is not None:
            yield pending
        yield from entries

    for entry in chained():
        prefix, rest = _split(entry.info.name)
        if prefix not in sections:
            raise StackArchiveRefused(
                "The archive's members do not match its manifest.", entry.info.name
            )
        if current is None or prefix != current.prefix:
            if current is not None:
                finish(current)
            if len(done) >= len(order) or prefix != order[len(done)]:
                raise StackArchiveRefused(
                    "The archive's members do not match its manifest.", entry.info.name
                )
            budget = ArchiveLimits(
                max(0, limits.max_members - used_members), max(0, limits.max_bytes - used_bytes)
            )
            current = _Current(prefix, sections[prefix], VolumeChecker(budget))
        index = prefix.rpartition("/")[2]
        try:
            if current.finished:
                raise StackArchiveRefused(
                    "The archive's members do not match its manifest.", entry.info.name
                )
            if not current.seen_manifest:
                if rest != VOLUME_MANIFEST_NAME:
                    raise StackArchiveRefused(
                        "A volume of the archive has no manifest.", f"{prefix}/manifest.json"
                    )
                _volume_manifest(entry, current.manifest, prefix)
                current.seen_manifest = True
            elif rest == SKIPPED_NAME:
                current.checker.skipped = read_skipped(_nested(entry, prefix, rest))
                current.finished = True
            else:
                current.checker.check(_nested(entry, prefix, rest))
        except VolumeArchiveRefused as refused:
            raise _from_volume(index, refused) from None
    if current is not None:
        finish(current)
    if [item.prefix for item in done] != order:
        raise StackArchiveRefused("The archive's members do not match its manifest.")
    return tuple(done)


def inspect_stack_archive(path: Path, limits: StackLimits | None = None) -> StackArchiveSummary:
    """Read the whole archive and return what it holds, or raise StackArchiveRefused.

    Every member passes the volume importer's rules, each volume under its own prefix, and every
    document its declared size and SHA-256. Nothing is extracted.
    """
    bounds = limits or StackLimits()
    with closing(_entries(path, bounds)) as entries:
        manifest = _read_manifest(entries)
        compose, files, pending = _read_documents(manifest, entries)
        volumes = _check_volumes(manifest, pending, entries, bounds)
    return StackArchiveSummary(manifest, compose, files, volumes)


def read_contexts(
    path: Path, manifest: ExportManifest, limits: StackLimits | None = None
) -> dict[str, bytes]:
    """Return the build contexts as uploaded, by name; each is checked against its digest."""
    bounds = limits or StackLimits()
    if manifest.stack is None or not manifest.stack.contexts:
        return {}
    wanted = {context.member: context for context in manifest.stack.contexts}
    found: dict[str, bytes] = {}
    with closing(_entries(path, bounds)) as entries:
        _read_manifest(entries)
        for entry in entries:
            name = entry.info.name
            if name.startswith(_VOLUMES_PREFIX):
                break
            context = wanted.get(name)
            if context is not None:
                found[context.name] = _document_digest(entry, context)
    if len(found) != len(wanted):
        raise StackArchiveRefused("The archive's members do not match its manifest.")
    return found


def _volume_members(path: Path, prefix: str, limits: StackLimits) -> Iterator[Member]:
    index = prefix.rpartition("/")[2]
    checker = VolumeChecker(limits.volumes)
    with closing(_entries(path, limits)) as entries:
        for entry in entries:
            name = entry.info.name
            if not name.startswith(f"{prefix}/"):
                continue
            rest = name[len(prefix) + 1 :]
            if rest in (VOLUME_MANIFEST_NAME, SKIPPED_NAME):
                continue
            try:
                yield checker.check(_nested(entry, prefix, rest))
            except VolumeArchiveRefused as refused:
                raise _from_volume(index, refused) from None


def stack_volume_tar(
    path: Path, prefix: str, limits: StackLimits | None = None
) -> Generator[bytes, None, None]:
    """Yield one volume's data as a tar rooted at the volume, checking it again as it goes.

    Setuid and setgid bits are cleared and owner ids kept, as for a volume import. The volume's
    own root entry is not written. StackArchiveRefused if the file no longer passes validation.
    """
    members = _volume_members(path, prefix, limits or StackLimits())
    return _refusing(rewrite_members(members), prefix.rpartition("/")[2])


def _refusing(chunks: Generator[bytes, None, None], index: str) -> Generator[bytes, None, None]:
    try:
        yield from chunks
    except VolumeArchiveRefused as refused:
        raise _from_volume(index, refused) from None
    finally:
        chunks.close()
