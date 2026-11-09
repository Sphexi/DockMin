"""Reads a stack or container export archive in order: manifest, documents, then volume members."""

import hashlib
import io
import tarfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import IO, Any, Final

from app.services.export_manifest import (
    MANIFEST_MAX_BYTES,
    MANIFEST_NAME,
    VOLUMES_DIRECTORY,
    ExportFormatError,
    ExportManifest,
    ExportRefusal,
    parse_manifest,
)

_GZIP_MAGIC: Final = b"\x1f\x8b"
_VOLUMES_PREFIX: Final = f"{VOLUMES_DIRECTORY}/"
_READ_ERRORS: Final = (tarfile.TarError, EOFError, zlib.error, OSError)


@dataclass(slots=True)
class VolumeMember:
    """One member of a volume's data; `body` is None for anything but a regular file.

    `name` is relative to the volume's prefix. `body` is valid only until the next member is
    requested.
    """

    prefix: str
    name: str
    info: tarfile.TarInfo
    body: IO[bytes] | None = field(repr=False)


class ExportReader:
    """A blocking reader over a gzip export archive; run it in a worker thread.

    The manifest is read and checked on construction. `documents` then reads the small members
    the manifest declares, and `volume_members` walks the volume data, each after the previous.
    ExportFormatError for anything that is not a conforming archive, with the version named when
    a later release wrote it.
    """

    def __init__(self, source: IO[bytes]) -> None:
        head = source.read(len(_GZIP_MAGIC))
        if head != _GZIP_MAGIC:
            raise ExportFormatError(ExportRefusal.NOT_GZIP)
        try:
            self._tar = tarfile.open(fileobj=_Prefixed(head, source), mode="r|gz")  # noqa: SIM115
        except _READ_ERRORS:
            raise ExportFormatError(ExportRefusal.MALFORMED) from None
        self._head: tarfile.TarInfo | None = None
        self._peeked = False
        self._documents: dict[str, bytes] | None = None
        first = self._peek()
        if first is None or first.name != MANIFEST_NAME or not first.isreg():
            raise ExportFormatError(ExportRefusal.NOT_AN_EXPORT)
        if first.size > MANIFEST_MAX_BYTES:
            raise ExportFormatError(ExportRefusal.TOO_LARGE)
        self.manifest: ExportManifest = parse_manifest(self._read(first))

    def documents(self) -> dict[str, bytes]:
        """Return the stack file, plain companion files and contexts by member path.

        Each is checked against its declared size and SHA-256. ExportFormatError when a declared
        member is missing, an undeclared one appears before the volumes, or a digest differs.
        """
        if self._documents is not None:
            return self._documents
        declared = self.manifest.documents()
        found: dict[str, bytes] = {}
        while (info := self._peek()) is not None and not info.name.startswith(_VOLUMES_PREFIX):
            entry = declared.get(info.name)
            if entry is None or not info.isreg() or info.name in found:
                raise ExportFormatError(ExportRefusal.MEMBER_MISMATCH)
            if info.size != entry.size:
                raise ExportFormatError(ExportRefusal.MEMBER_MISMATCH)
            data = self._read(info)
            if entry.sha256 != hashlib.sha256(data).hexdigest():
                raise ExportFormatError(ExportRefusal.MEMBER_MISMATCH)
            found[info.name] = data
        if found.keys() != declared.keys():
            raise ExportFormatError(ExportRefusal.MEMBER_MISMATCH)
        self._documents = found
        return found

    def volume_members(self) -> Iterator[VolumeMember]:
        """Yield every member under a declared volume prefix, in archive order.

        ExportFormatError for a member outside the declared prefixes or with an unsafe name.
        """
        self.documents()
        prefixes = {section.prefix for section in self.manifest.volumes}
        while (info := self._peek()) is not None:
            prefix, name = _split(info.name)
            if prefix not in prefixes or not _is_relative(name):
                raise ExportFormatError(ExportRefusal.MEMBER_MISMATCH)
            body = self._tar.extractfile(info) if info.isreg() else None
            yield VolumeMember(prefix, name, info, body)
            self._peeked = False

    def close(self) -> None:
        """Release the archive; the underlying file is the caller's to close."""
        self._tar.close()

    def _peek(self) -> tarfile.TarInfo | None:
        if not self._peeked:
            try:
                self._head = self._tar.next()
            except _READ_ERRORS:
                raise ExportFormatError(ExportRefusal.MALFORMED) from None
            self._peeked = True
        return self._head

    def _read(self, info: tarfile.TarInfo) -> bytes:
        try:
            body = self._tar.extractfile(info)
            data = body.read() if body is not None else b""
        except _READ_ERRORS:
            raise ExportFormatError(ExportRefusal.MALFORMED) from None
        if len(data) != info.size:
            raise ExportFormatError(ExportRefusal.MALFORMED)
        self._peeked = False
        return data


class _Prefixed(io.RawIOBase):
    """Returns bytes already read from a source before the rest of it."""

    def __init__(self, head: bytes, source: IO[bytes]) -> None:
        super().__init__()
        self._head = head
        self._source = source

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        if self._head:
            taken = min(len(view), len(self._head))
            view[:taken] = self._head[:taken]
            self._head = self._head[taken:]
            return taken
        data = self._source.read(len(view))
        view[: len(data)] = data
        return len(data)


def _split(name: str) -> tuple[str, str]:
    parts = name.split("/", 2)
    if len(parts) < 3 or parts[0] != VOLUMES_DIRECTORY:
        return "", name
    return f"{parts[0]}/{parts[1]}", parts[2]


def _is_relative(name: str) -> bool:
    return bool(name) and not name.startswith("/") and ".." not in name.split("/")
