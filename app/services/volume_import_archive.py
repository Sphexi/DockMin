"""Validates a volume export archive and rewrites its data as a tar for the Engine; blocking."""

import gzip
import json
import posixpath
import tarfile
import zlib
from collections.abc import Callable, Generator, Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal

from pydantic import ValidationError

from app.services.volume_export_writer import (
    BLOCK,
    ChunkSource,
    EntryBody,
    ReadEntry,
    StreamMalformed,
    TarReader,
    TooManyHeaders,
    UnsupportedMember,
)
from app.services.volume_manifest import (
    DATA_DIRECTORY,
    FORMAT_VERSION,
    MANIFEST_NAME,
    SKIPPED_NAME,
    VolumeManifest,
)

MAX_UPLOAD_BYTES: Final = 7 * 1024**3
MAX_MEMBERS: Final = 2_000_000
MAX_EXTRACTED_BYTES: Final = 64 * 1024**3
MAX_PATH_BYTES: Final = 4096
MAX_COMPONENT_BYTES: Final = 255
MAX_PATH_DEPTH: Final = 128
MAX_NAME_BYTES: Final = 64 * 1024 * 1024
MANIFEST_MAX_BYTES: Final = 1024 * 1024
SKIPPED_MAX_BYTES: Final = 16 * 1024 * 1024
OUTPUT_CHUNK_BYTES: Final = 256 * 1024

_GZIP_MAGIC: Final = b"\x1f\x8b"
_READ_BYTES: Final = 256 * 1024
_HEADER_ALLOWANCE: Final = 8192
_SETID_BITS: Final = 0o6000
_MODE_BITS: Final = 0o7777
_ID_LIMIT: Final = 2**32
_NAME_OVERHEAD: Final = 32
_MTIME_LIMIT: Final = 2**34
_USER_XATTR: Final = "SCHILY.xattr.user."
_XATTR: Final = "SCHILY.xattr."
_DISPLAY_CHARS: Final = 80
_REGULAR_TYPES: Final = (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.CONTTYPE)
_ZERO_END: Final = bytes(2 * BLOCK)

type Kind = Literal["root", "directory", "file", "symlink", "hardlink"]


class ArchiveRefusal(StrEnum):
    """Why an archive was refused."""

    NOT_GZIP = "not_gzip"
    MALFORMED = "malformed"
    NOT_AN_EXPORT = "not_an_export"
    MANIFEST_INVALID = "manifest_invalid"
    NEWER_VERSION = "newer_version"
    MEMBER_TYPE = "member_type"
    ABSOLUTE_NAME = "absolute_name"
    PARENT_COMPONENT = "parent_component"
    NAME_TOO_LONG = "name_too_long"
    PATH_TOO_DEEP = "path_too_deep"
    NAMES_TOO_LARGE = "names_too_large"
    BAD_NAME = "bad_name"
    OUTSIDE_DATA = "outside_data"
    DUPLICATE_NAME = "duplicate_name"
    NAME_CONFLICT = "name_conflict"
    SYMLINK_PARENT = "symlink_parent"
    SYMLINK_ESCAPE = "symlink_escape"
    HARDLINK_TARGET = "hardlink_target"
    BAD_OWNER = "bad_owner"
    TOO_MANY_MEMBERS = "too_many_members"
    TOTAL_TOO_LARGE = "total_too_large"
    EMPTY = "empty"


MESSAGES: Final = {
    ArchiveRefusal.NOT_GZIP: "The file is not a gzip archive.",
    ArchiveRefusal.MALFORMED: "The file is not a readable volume export archive.",
    ArchiveRefusal.NOT_AN_EXPORT: (
        "The archive was not written by a volume export: it does not start with manifest.json."
    ),
    ArchiveRefusal.MANIFEST_INVALID: "The archive's manifest.json is not a valid volume manifest.",
    ArchiveRefusal.NEWER_VERSION: "The archive's format version is newer than this build reads.",
    ArchiveRefusal.MEMBER_TYPE: (
        "The archive holds an entry that is not a file, directory or link, "
        "such as a device, FIFO or socket."
    ),
    ArchiveRefusal.ABSOLUTE_NAME: "The archive holds an entry with an absolute name.",
    ArchiveRefusal.PARENT_COMPONENT: "The archive holds an entry whose name contains `..`.",
    ArchiveRefusal.NAME_TOO_LONG: "The archive holds an entry with a name that is too long.",
    ArchiveRefusal.PATH_TOO_DEEP: "The archive holds an entry nested too deeply.",
    ArchiveRefusal.NAMES_TOO_LARGE: "The archive's entry names take more space than allowed.",
    ArchiveRefusal.BAD_NAME: "The archive holds an entry with an unusable name or link target.",
    ArchiveRefusal.OUTSIDE_DATA: "The archive holds an entry outside its data directory.",
    ArchiveRefusal.DUPLICATE_NAME: "The archive holds the same name twice.",
    ArchiveRefusal.NAME_CONFLICT: "The archive uses a name as both a file and a directory.",
    ArchiveRefusal.SYMLINK_PARENT: "The archive holds an entry below a symbolic link.",
    ArchiveRefusal.SYMLINK_ESCAPE: (
        "The archive holds a symbolic link whose relative target leaves the volume."
    ),
    ArchiveRefusal.HARDLINK_TARGET: (
        "The archive holds a hard link to a file that does not come earlier in the archive."
    ),
    ArchiveRefusal.BAD_OWNER: "The archive holds an entry with an owner id out of range.",
    ArchiveRefusal.TOO_MANY_MEMBERS: "The archive holds more entries than the limit allows.",
    ArchiveRefusal.TOTAL_TOO_LARGE: "The archive extracts to more than the size limit.",
    ArchiveRefusal.EMPTY: "The archive holds no data.",
}


class VolumeArchiveRefused(Exception):
    """The archive broke a rule; `member` is a printable, truncated form of the entry at fault."""

    def __init__(
        self, reason: ArchiveRefusal, member: str | None = None, version: int | None = None
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.member = None if member is None else _display(member)
        self.version = version

    @property
    def message(self) -> str:
        """The text shown to the administrator, naming the version or entry when there is one."""
        if self.version is not None:
            return (
                f"The archive's format version is {self.version}; "
                f"this build reads up to {FORMAT_VERSION}."
            )
        return MESSAGES[self.reason]


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    """Bounds on an archive: entries, bytes of file data, and bytes held for entry names."""

    max_members: int = MAX_MEMBERS
    max_bytes: int = MAX_EXTRACTED_BYTES
    max_name_bytes: int = MAX_NAME_BYTES

    @property
    def ceiling(self) -> int:
        """The most decompressed bytes read before the archive is refused."""
        return self.max_bytes + self.max_members * _HEADER_ALLOWANCE + MANIFEST_MAX_BYTES * 2


@dataclass(frozen=True, slots=True)
class ArchiveSummary:
    """What a validated archive holds."""

    manifest: VolumeManifest
    files: int
    directories: int
    symlinks: int
    hardlinks: int
    bytes: int
    skipped: int
    xattr_members_dropped: int

    @property
    def members(self) -> int:
        """Return the number of entries below the data directory."""
        return self.files + self.directories + self.symlinks + self.hardlinks


@dataclass(slots=True)
class Member:
    """A checked entry below the data directory; `name` is relative to the volume's root."""

    kind: Kind
    name: str
    size: int
    mode: int
    uid: int
    gid: int
    mtime: int
    linkname: str
    xattrs: dict[str, str]
    _body: EntryBody | None = field(repr=False, default=None)

    def read(self, size: int) -> bytes:
        """Read up to `size` bytes of a file's data, in order."""
        if self._body is None:
            raise VolumeArchiveRefused(ArchiveRefusal.MALFORMED, self.name)
        try:
            return self._body.read(size)
        except StreamMalformed:
            raise VolumeArchiveRefused(ArchiveRefusal.MALFORMED, self.name) from None


def _display(name: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in name)[:_DISPLAY_CHARS]


def _has_control(text: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in text)


def _utf8_size(text: str, member: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise VolumeArchiveRefused(ArchiveRefusal.BAD_NAME, member) from None


def _relative_path(raw: str, *, member: str) -> str:
    """Return the path below the data directory; "" for the directory itself."""
    if raw.startswith("/"):
        raise VolumeArchiveRefused(ArchiveRefusal.ABSOLUTE_NAME, member)
    if _utf8_size(raw, member) > MAX_PATH_BYTES:
        raise VolumeArchiveRefused(ArchiveRefusal.NAME_TOO_LONG, member)
    if _has_control(raw):
        raise VolumeArchiveRefused(ArchiveRefusal.BAD_NAME, member)
    parts = raw.split("/")
    if ".." in parts:
        raise VolumeArchiveRefused(ArchiveRefusal.PARENT_COMPONENT, member)
    if parts[0] != DATA_DIRECTORY:
        raise VolumeArchiveRefused(ArchiveRefusal.OUTSIDE_DATA, member)
    rest = parts[1:]
    if len(rest) > MAX_PATH_DEPTH:
        raise VolumeArchiveRefused(ArchiveRefusal.PATH_TOO_DEEP, member)
    if any(part in ("", ".") for part in rest):
        raise VolumeArchiveRefused(ArchiveRefusal.BAD_NAME, member)
    if any(_utf8_size(part, member) > MAX_COMPONENT_BYTES for part in rest):
        raise VolumeArchiveRefused(ArchiveRefusal.NAME_TOO_LONG, member)
    return "/".join(rest)


class _Node:
    __slots__ = ("children", "kind")

    def __init__(self) -> None:
        self.kind: Kind | None = None
        self.children: dict[str, _Node] | None = None


def _name_cost(part: str) -> int:
    return (len(part) if part.isascii() else len(part.encode("utf-8"))) + _NAME_OVERHEAD


class _Layout:
    """Tracks the names seen so far as a tree of components, within a budget of held bytes."""

    def __init__(self, limits: ArchiveLimits) -> None:
        self._limits = limits
        self._root = _Node()
        self._held = 0
        self.root_seen = False
        self.members = 0
        self.total = 0

    def kind_of(self, name: str) -> Kind | None:
        """Return the kind an accepted entry of this name has, or None."""
        node: _Node | None = self._root
        for part in name.split("/") if name else ():
            node = None if node is None or node.children is None else node.children.get(part)
            if node is None:
                return None
        return None if node is None else node.kind

    def accept_name(self, name: str, kind: Kind, size: int, display: str) -> None:
        parts = name.split("/")
        last = len(parts) - 1
        node = self._root
        depth = 0
        while depth <= last:
            child = None if node.children is None else node.children.get(parts[depth])
            if child is None:
                break
            if depth < last:
                if child.kind == "symlink":
                    raise VolumeArchiveRefused(ArchiveRefusal.SYMLINK_PARENT, display)
                if child.kind is not None and child.kind != "directory":
                    raise VolumeArchiveRefused(ArchiveRefusal.NAME_CONFLICT, display)
            node = child
            depth += 1
        if depth > last:
            if node.kind is not None:
                raise VolumeArchiveRefused(ArchiveRefusal.DUPLICATE_NAME, display)
            if kind != "directory":
                raise VolumeArchiveRefused(ArchiveRefusal.NAME_CONFLICT, display)
        if self.members >= self._limits.max_members:
            raise VolumeArchiveRefused(ArchiveRefusal.TOO_MANY_MEMBERS, display)
        if self.total + size > self._limits.max_bytes:
            raise VolumeArchiveRefused(ArchiveRefusal.TOTAL_TOO_LARGE, display)
        cost = sum(_name_cost(part) for part in parts[depth:])
        if self._held + cost > self._limits.max_name_bytes:
            raise VolumeArchiveRefused(ArchiveRefusal.NAMES_TOO_LARGE, display)
        for part in parts[depth:]:
            child = _Node()
            if node.children is None:
                node.children = {}
            node.children[part] = child
            node = child
        node.kind = kind
        self._held += cost
        self.members += 1
        self.total += size


def _checked_target(name: str, target: str, display: str) -> None:
    if not target or _has_control(target) or _utf8_size(target, display) > MAX_PATH_BYTES:
        raise VolumeArchiveRefused(ArchiveRefusal.BAD_NAME, display)
    if target.startswith("/"):
        return
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    if resolved == ".." or resolved.startswith("../"):
        raise VolumeArchiveRefused(ArchiveRefusal.SYMLINK_ESCAPE, display)


def _hardlink_target(raw: str, layout: _Layout, display: str) -> str:
    try:
        target = _relative_path(raw, member=display)
    except VolumeArchiveRefused:
        raise VolumeArchiveRefused(ArchiveRefusal.HARDLINK_TARGET, display) from None
    if layout.kind_of(target) != "file":
        raise VolumeArchiveRefused(ArchiveRefusal.HARDLINK_TARGET, display)
    return target


class VolumeChecker:
    """Checks the entries below one volume's data directory in archive order."""

    def __init__(self, limits: ArchiveLimits | None = None) -> None:
        self.limits = limits or ArchiveLimits()
        self.layout = _Layout(self.limits)
        self.counts = {"directory": 0, "file": 0, "symlink": 0, "hardlink": 0}
        self.skipped = 0
        self.xattr_dropped = 0

    def summary(self, manifest: VolumeManifest, *, require_data: bool = True) -> ArchiveSummary:
        """Return what the checked entries hold; EMPTY when `require_data` and there are none."""
        if require_data and not self.layout.members:
            raise VolumeArchiveRefused(ArchiveRefusal.EMPTY)
        return ArchiveSummary(
            manifest=manifest,
            files=self.counts["file"],
            directories=self.counts["directory"],
            symlinks=self.counts["symlink"],
            hardlinks=self.counts["hardlink"],
            bytes=self.layout.total,
            skipped=self.skipped,
            xattr_members_dropped=self.xattr_dropped,
        )

    def check(self, entry: ReadEntry) -> Member:
        """Return the checked entry, or raise VolumeArchiveRefused naming it."""
        info = entry.info
        raw = info.name
        if info.type in _REGULAR_TYPES and info.sparse is None:
            kind: Kind = "file"
        elif info.type == tarfile.DIRTYPE:
            kind = "directory"
        elif info.type == tarfile.SYMTYPE:
            kind = "symlink"
        elif info.type == tarfile.LNKTYPE:
            kind = "hardlink"
        else:
            raise VolumeArchiveRefused(ArchiveRefusal.MEMBER_TYPE, raw)
        name = _relative_path(raw, member=raw)
        if info.size < 0:
            raise VolumeArchiveRefused(ArchiveRefusal.MALFORMED, raw)
        if not 0 <= info.uid < _ID_LIMIT or not 0 <= info.gid < _ID_LIMIT:
            raise VolumeArchiveRefused(ArchiveRefusal.BAD_OWNER, raw)
        linkname = ""
        if kind == "symlink":
            linkname = info.linkname
            _checked_target(name, linkname, raw)
        elif kind == "hardlink":
            linkname = _hardlink_target(info.linkname, self.layout, raw)
        if not name:
            if kind != "directory" or self.layout.root_seen:
                raise VolumeArchiveRefused(ArchiveRefusal.NAME_CONFLICT, raw)
            self.layout.root_seen = True
            return self._member("root", name, entry, linkname, size=0)
        size = info.size if kind == "file" else 0
        if kind == "directory" and info.size != 0:
            raise VolumeArchiveRefused(ArchiveRefusal.MALFORMED, raw)
        self.layout.accept_name(name, kind, size, raw)
        self.counts[kind] += 1
        return self._member(kind, name, entry, linkname, size=size)

    def _member(
        self, kind: Kind, name: str, entry: ReadEntry, linkname: str, *, size: int
    ) -> Member:
        info = entry.info
        try:
            mtime = max(0, min(int(info.mtime), _MTIME_LIMIT))
        except (ValueError, OverflowError):
            raise VolumeArchiveRefused(ArchiveRefusal.MALFORMED, info.name) from None
        xattrs = {k: v for k, v in info.pax_headers.items() if k.startswith(_USER_XATTR)}
        if any(k.startswith(_XATTR) and k not in xattrs for k in info.pax_headers):
            self.xattr_dropped += 1
        return Member(
            kind=kind,
            name=name,
            size=size,
            mode=info.mode & _MODE_BITS & ~_SETID_BITS,
            uid=info.uid,
            gid=info.gid,
            mtime=mtime,
            linkname=linkname,
            xattrs=xattrs,
            _body=entry.body if kind == "file" else None,
        )


class VolumeArchive:
    """One export archive on disk; `members` validates it as it yields, `summary` follows it."""

    def __init__(self, path: Path, limits: ArchiveLimits | None = None) -> None:
        self._path = path
        self._limits = limits or ArchiveLimits()
        self.manifest: VolumeManifest | None = None
        self._checker = VolumeChecker(self._limits)
        self._complete = False

    def summary(self) -> ArchiveSummary:
        """Return what the archive holds; only after `members` was read to its end."""
        if not self._complete or self.manifest is None:
            raise RuntimeError("the archive has not been read to its end")
        return self._checker.summary(self.manifest)

    def members(self) -> Iterator[Member]:
        """Yield every entry below the data directory, refusing the archive at the first fault.

        A file's data must be read, or left alone, before the next entry is asked for. The end
        of the archive is checked once the last entry has been taken.
        """
        with self._path.open("rb") as raw:
            if raw.read(len(_GZIP_MAGIC)) != _GZIP_MAGIC:
                raise VolumeArchiveRefused(ArchiveRefusal.NOT_GZIP)
            raw.seek(0)
            try:
                with gzip.GzipFile(fileobj=raw, mode="rb") as unzipped:
                    yield from self._walk(self._source(unzipped))
            except UnsupportedMember:
                raise VolumeArchiveRefused(ArchiveRefusal.MEMBER_TYPE) from None
            except TooManyHeaders:
                raise VolumeArchiveRefused(ArchiveRefusal.TOO_MANY_MEMBERS) from None
            except (OSError, EOFError, zlib.error, StreamMalformed):
                raise VolumeArchiveRefused(ArchiveRefusal.MALFORMED) from None

    def _source(self, unzipped: gzip.GzipFile) -> ChunkSource:
        produced = 0
        ceiling = self._limits.ceiling

        def pull() -> bytes | None:
            nonlocal produced
            chunk = unzipped.read(_READ_BYTES)
            produced += len(chunk)
            if produced > ceiling:
                raise VolumeArchiveRefused(ArchiveRefusal.TOTAL_TOO_LARGE)
            return chunk or None

        return ChunkSource(pull)

    def _walk(self, source: ChunkSource) -> Iterator[Member]:
        reader = TarReader(source, max_extensions=self._limits.max_members)
        self.manifest = self._read_manifest(reader.next())
        finished = False
        while (entry := reader.next()) is not None:
            if finished:
                raise VolumeArchiveRefused(ArchiveRefusal.OUTSIDE_DATA, entry.info.name)
            if entry.info.name == SKIPPED_NAME:
                self._checker.skipped = read_skipped(entry)
                finished = True
                continue
            yield self._checker.check(entry)
        source.finish()
        if not self._checker.layout.members:
            raise VolumeArchiveRefused(ArchiveRefusal.EMPTY)
        self._complete = True

    def _read_manifest(self, entry: ReadEntry | None) -> VolumeManifest:
        if (
            entry is None
            or entry.info.name != MANIFEST_NAME
            or entry.info.type not in _REGULAR_TYPES
        ):
            raise VolumeArchiveRefused(ArchiveRefusal.NOT_AN_EXPORT)
        if entry.info.size > MANIFEST_MAX_BYTES:
            raise VolumeArchiveRefused(ArchiveRefusal.MANIFEST_INVALID, MANIFEST_NAME)
        raw = entry.body.read()
        try:
            document = json.loads(raw)
        except (ValueError, RecursionError):
            raise VolumeArchiveRefused(ArchiveRefusal.MANIFEST_INVALID, MANIFEST_NAME) from None
        version = document.get("format_version") if isinstance(document, dict) else None
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise VolumeArchiveRefused(ArchiveRefusal.MANIFEST_INVALID, MANIFEST_NAME)
        if version > FORMAT_VERSION:
            raise VolumeArchiveRefused(ArchiveRefusal.NEWER_VERSION, version=version)
        try:
            manifest = VolumeManifest.model_validate(document)
        except ValidationError:
            raise VolumeArchiveRefused(ArchiveRefusal.MANIFEST_INVALID, MANIFEST_NAME) from None
        if manifest.data_path != DATA_DIRECTORY:
            raise VolumeArchiveRefused(ArchiveRefusal.MANIFEST_INVALID, MANIFEST_NAME)
        return manifest


def read_skipped(entry: ReadEntry) -> int:
    """Return the count a skipped.json entry records; VolumeArchiveRefused when it is not one."""
    if entry.info.size > SKIPPED_MAX_BYTES or entry.info.type not in _REGULAR_TYPES:
        raise VolumeArchiveRefused(ArchiveRefusal.MALFORMED, SKIPPED_NAME)
    try:
        document = json.loads(entry.body.read())
    except (ValueError, RecursionError):
        raise VolumeArchiveRefused(ArchiveRefusal.MALFORMED, SKIPPED_NAME) from None
    count = document.get("count") if isinstance(document, dict) else None
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise VolumeArchiveRefused(ArchiveRefusal.MALFORMED, SKIPPED_NAME)
    return count


def inspect_volume_archive(path: Path, limits: ArchiveLimits | None = None) -> ArchiveSummary:
    """Read the whole archive and return what it holds, or raise VolumeArchiveRefused."""
    archive = VolumeArchive(path, limits)
    for _ in archive.members():
        pass
    return archive.summary()


def _header(member: Member) -> bytes:
    info = tarfile.TarInfo(member.name)
    info.mode, info.uid, info.gid, info.mtime = member.mode, member.uid, member.gid, member.mtime
    info.uname = info.gname = ""
    info.pax_headers = dict(member.xattrs)
    match member.kind:
        case "directory":
            info.type = tarfile.DIRTYPE
        case "symlink":
            info.type, info.linkname = tarfile.SYMTYPE, member.linkname
        case "hardlink":
            info.type, info.linkname = tarfile.LNKTYPE, member.linkname
        case _:
            info.type, info.size = tarfile.REGTYPE, member.size
    return info.tobuf(tarfile.PAX_FORMAT, "utf-8", "surrogateescape")


def rewrite_volume_archive(
    path: Path,
    limits: ArchiveLimits | None = None,
    *,
    on_member: Callable[[Member], None] | None = None,
) -> Generator[bytes, None, None]:
    """Yield the archive's data as a tar rooted at the volume, validating it again as it goes.

    Setuid and setgid bits are cleared, owner ids kept, names and link targets made relative
    to the data directory, and everything outside it left out. The data directory's own entry is
    not written, since the Engine does not apply it. VolumeArchiveRefused if the file no longer
    passes validation.
    """
    archive = VolumeArchive(path, limits)
    return rewrite_members(archive.members(), on_member=on_member)


def rewrite_members(
    members: Iterable[Member], *, on_member: Callable[[Member], None] | None = None
) -> Generator[bytes, None, None]:
    """Yield the tar of checked members; each file's data is read as the tar is produced."""
    buffer = bytearray()
    for member in members:
        if member.kind == "root":
            continue
        if on_member is not None:
            on_member(member)
        buffer += _header(member)
        if member.kind == "file":
            remaining = member.size
            while remaining:
                data = member.read(min(OUTPUT_CHUNK_BYTES, remaining))
                if not data:
                    raise VolumeArchiveRefused(ArchiveRefusal.MALFORMED, member.name)
                remaining -= len(data)
                buffer += data
                if len(buffer) >= OUTPUT_CHUNK_BYTES:
                    yield bytes(buffer)
                    buffer.clear()
            buffer += bytes(-member.size % BLOCK)
        if len(buffer) >= OUTPUT_CHUNK_BYTES:
            yield bytes(buffer)
            buffer.clear()
    buffer += _ZERO_END
    yield bytes(buffer)
