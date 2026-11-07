"""Capped, validating streaming reader for tar archives of regular files and directories."""

import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import IO, Final, Protocol, cast

MAX_NAME_BYTES: Final = 255
_DISPLAY_NAME_CHARS: Final = 80
_FILE_TYPES: Final = (tarfile.REGTYPE, tarfile.AREGTYPE)
_EXEC_BITS: Final = 0o111
_CHUNK: Final = 64 * 1024


class TarRefusal(StrEnum):
    """Why an archive was refused."""

    MALFORMED = "malformed"
    TOO_MANY_MEMBERS = "too_many_members"
    MEMBER_TOO_LARGE = "member_too_large"
    TOTAL_TOO_LARGE = "total_too_large"
    MEMBER_TYPE = "member_type"
    ABSOLUTE_NAME = "absolute_name"
    PARENT_COMPONENT = "parent_component"
    NAME_TOO_LONG = "name_too_long"
    BAD_NAME = "bad_name"
    DUPLICATE_NAME = "duplicate_name"


class TarRefused(Exception):
    """The archive broke a rule; `member` is a printable, truncated form of the offending name."""

    def __init__(self, reason: TarRefusal, member: str | None = None) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.member = None if member is None else _display(member)


@dataclass(frozen=True, slots=True)
class TarLimits:
    """Bounds on an archive: members counted, one member's bytes, and all members' bytes."""

    max_members: int
    max_member_bytes: int
    max_total_bytes: int


@dataclass(frozen=True, slots=True)
class TarMember:
    """A regular file or directory; `name` is relative and normalised.

    `executable` is true when any execute bit was set in the archive; no other mode bit is kept.
    """

    name: str
    is_directory: bool
    data: bytes
    executable: bool = False


class TarSource(Protocol):
    """A byte stream; `read(size)` returns at most `size` bytes and b"" at the end."""

    def read(self, size: int, /) -> bytes:
        """Return up to `size` bytes."""
        ...


class TarEntry:
    """One header of a streamed archive; read its data before asking for the next entry."""

    def __init__(
        self,
        name: str,
        is_directory: bool,
        size: int,
        executable: bool,
        tar: tarfile.TarFile,
        info: tarfile.TarInfo,
    ) -> None:
        self.name = name
        self.is_directory = is_directory
        self.size = size
        self.executable = executable
        self._tar = tar
        self._info = info

    def chunks(self, limit: int | None = None) -> Iterator[bytes]:
        """Yield the entry's data in chunks, at most `limit` bytes of it when `limit` is given."""
        remaining = self.size if limit is None else min(self.size, limit)
        if self.is_directory or remaining == 0:
            return
        try:
            source = self._tar.extractfile(self._info)
            while remaining:
                chunk = b"" if source is None else source.read(min(_CHUNK, remaining))
                if not chunk:
                    raise TarRefused(TarRefusal.MALFORMED, self.name)
                remaining -= len(chunk)
                yield chunk
        except (tarfile.TarError, EOFError, ValueError):
            raise TarRefused(TarRefusal.MALFORMED, self.name) from None


def iter_tar_entries(source: TarSource, limits: TarLimits) -> Iterator[TarEntry]:
    """Yield each entry of the archive in `source`, or raise TarRefused.

    Only regular files and directories are accepted. The root entry (`.` or `./`) is skipped
    and neither counted nor yielded. Every check on an entry's header runs before the entry is
    yielded, and the end of the archive is checked once the last entry has been taken. Stopping
    early leaves the rest of the stream unread and unchecked.
    """
    seen: set[str] = set()
    total = 0
    try:
        with tarfile.open(fileobj=cast(IO[bytes], source), mode="r|", bufsize=_CHUNK) as tar:
            while (info := tar.next()) is not None:
                is_directory = _is_directory(info)
                name = _normalised(info.name, is_directory)
                if name is None:
                    continue
                if name in seen:
                    raise TarRefused(TarRefusal.DUPLICATE_NAME, name)
                if len(seen) >= limits.max_members:
                    raise TarRefused(TarRefusal.TOO_MANY_MEMBERS, name)
                if is_directory:
                    if info.size != 0:
                        raise TarRefused(TarRefusal.MALFORMED, name)
                elif info.size > limits.max_member_bytes:
                    raise TarRefused(TarRefusal.MEMBER_TOO_LARGE, name)
                elif total + info.size > limits.max_total_bytes:
                    raise TarRefused(TarRefusal.TOTAL_TOO_LARGE, name)
                seen.add(name)
                total += info.size
                yield TarEntry(
                    name, is_directory, info.size, bool(info.mode & _EXEC_BITS), tar, info
                )
            _require_zero_padding(tar)
    except (tarfile.TarError, EOFError, ValueError):
        raise TarRefused(TarRefusal.MALFORMED) from None


def read_tar_members(data: bytes, limits: TarLimits) -> tuple[TarMember, ...]:
    """Return every member of `data`, or raise TarRefused; nothing is returned in part."""
    members: list[TarMember] = []
    for entry in iter_tar_entries(BytesSource(data), limits):
        content = b"".join(entry.chunks())
        members.append(TarMember(entry.name, entry.is_directory, content, entry.executable))
    return tuple(members)


class BytesSource:
    """A TarSource over an in-memory buffer."""

    def __init__(self, data: bytes) -> None:
        self._view = memoryview(data)
        self._position = 0

    def read(self, size: int, /) -> bytes:
        chunk = self._view[self._position : self._position + size]
        self._position += len(chunk)
        return bytes(chunk)


def _require_zero_padding(tar: tarfile.TarFile) -> None:
    stream = tar.fileobj
    while stream is not None and (chunk := stream.read(_CHUNK)):
        if chunk.count(0) != len(chunk):
            raise TarRefused(TarRefusal.MALFORMED)


def _is_directory(info: tarfile.TarInfo) -> bool:
    if info.type == tarfile.DIRTYPE:
        return True
    if info.type in _FILE_TYPES and info.sparse is None:
        return False
    raise TarRefused(TarRefusal.MEMBER_TYPE, info.name)


def _normalised(raw: str, is_directory: bool) -> str | None:
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError:
        raise TarRefused(TarRefusal.BAD_NAME, raw) from None
    if len(encoded) > MAX_NAME_BYTES:
        raise TarRefused(TarRefusal.NAME_TOO_LONG, raw)
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise TarRefused(TarRefusal.BAD_NAME, raw)
    if raw.startswith("/"):
        raise TarRefused(TarRefusal.ABSOLUTE_NAME, raw)
    parts = raw.split("/")
    if ".." in parts:
        raise TarRefused(TarRefusal.PARENT_COMPONENT, raw)
    kept = [part for part in parts if part not in ("", ".")]
    if not kept:
        if is_directory:
            return None
        raise TarRefused(TarRefusal.BAD_NAME, raw)
    return "/".join(kept)


def _display(name: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in name)[:_DISPLAY_NAME_CHARS]
