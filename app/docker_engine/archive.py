"""Read, stream and write container paths through the Engine archive endpoint."""

import asyncio
import base64
import binascii
import io
import json
import posixpath
import re
import tarfile
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Final

from app.docker_engine.engine_http import EngineHttp
from app.docker_engine.errors import EngineError, EnvironmentUnreachable, NotFound
from app.docker_engine.timeouts import Timeouts

STAT_HEADER: Final = "X-Docker-Container-Path-Stat"
STAT_HEADER_MAX_CHARS: Final = 8192
TAR_OVERHEAD_BYTES: Final = 64 * 1024
ARCHIVE_SECONDS: Final = 120.0
STALL_SECONDS: Final = 60.0
WRITE_REPLY_SECONDS: Final = 600.0
WRITE_REPLY_BYTES: Final = 64 * 1024
PATH_MAX_BYTES: Final = 4096

_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_BAD_REQUEST: Final = 400
_GO_MODE_SYMLINK: Final = 1 << 27
_GO_MODE_DIR: Final = 1 << 31
# Go's os.FileMode type bits: dir, symlink, named pipe, socket, device, char device, irregular.
_GO_MODE_TYPE_MASK: Final = 0x8F280000
_REGULAR_TYPES: Final = (tarfile.REGTYPE, tarfile.AREGTYPE)


class ArchiveError(EngineError):
    """The archive read was refused; `code` is the stable reason."""

    code: ClassVar[str] = "refused"


class FileMissing(ArchiveError):
    """The Engine has no file at the path."""

    code = "missing"


class SymlinkRefused(ArchiveError):
    """The path is a symbolic link."""

    code = "symlink"


class NotARegularFile(ArchiveError):
    """The path is a directory or another kind of file that is not regular."""

    code = "not_regular_file"


class NotADirectory(ArchiveError):
    """The path is not a directory."""

    code = "not_directory"


class FileTooLarge(ArchiveError):
    """The file, or the archive carrying it, is over the limit."""

    code = "too_large"


class ArchiveMalformed(ArchiveError):
    """The stat header or the archive body is not what the Engine sends for a regular file."""

    code = "malformed"


class MemberCountMismatch(ArchiveError):
    """The archive holds a number of members other than one."""

    code = "member_count"


class MemberNameMismatch(ArchiveError):
    """The archive's member is not named after the requested path."""

    code = "member_name"


class ArchiveRejected(ArchiveError):
    """The Engine refused the archive that was sent."""

    code = "rejected"


class InvalidArchivePath(ArchiveError):
    """The requested path is not an absolute, normalised path."""

    code = "invalid_path"


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """A regular file read from a container."""

    name: str
    content: bytes


@dataclass(frozen=True, slots=True)
class PathStat:
    """The decoded `X-Docker-Container-Path-Stat` header."""

    name: str
    size: int
    mode: int
    link_target: str


async def read_archive(
    http: EngineHttp, container_id: str, path: str, *, limit: int
) -> ArchiveEntry:
    """Return the regular file at `path`, refusing everything else before the body is read.

    The body is capped at `limit` plus tar overhead and unpacked in a worker thread.
    """
    environment_id = http.environment_id
    if limit < 0:
        raise ValueError("limit must not be negative")
    if _CONTAINER_ID.fullmatch(container_id) is None:
        raise EngineError(environment_id, "archive reads need a full container id")
    name = _requested_name(environment_id, path)
    stats: list[PathStat] = []

    def check_stat(headers: Mapping[str, str]) -> None:
        stats.append(_checked_stat(environment_id, headers.get(STAT_HEADER), limit))

    parts: list[bytes] = []
    received = 0
    cap = limit + TAR_OVERHEAD_BYTES
    try:
        async with asyncio.timeout(ARCHIVE_SECONDS):
            async with aclosing(
                http.stream_bytes(
                    "GET",
                    f"/containers/{container_id}/archive",
                    params={"path": path},
                    timeouts=Timeouts.standard(),
                    on_headers=check_stat,
                )
            ) as chunks:
                async for chunk in chunks:
                    received += len(chunk)
                    if received > cap:
                        raise FileTooLarge(environment_id, "the archive is over the limit")
                    parts.append(chunk)
    except ArchiveError:
        raise
    except TimeoutError:
        raise EnvironmentUnreachable(environment_id, "the archive read timed out") from None
    except NotFound:
        raise FileMissing(environment_id, "the path does not exist") from None
    except EngineError as error:
        if error.status_code == _BAD_REQUEST:
            raise FileMissing(environment_id, "the path does not exist") from None
        raise
    if not stats:
        raise ArchiveMalformed(environment_id, "the path stat header was not checked")
    content = await asyncio.to_thread(_single_member, environment_id, b"".join(parts), name, limit)
    return ArchiveEntry(name=name, content=content)


class DirectoryStream:
    """The tar body of one directory, yielded as it arrives; `received` counts the bytes so far.

    The first chunk has already been read when this is returned, so the path stat has been
    checked. Closing the stream, or cancelling a read from it, closes the response.
    """

    def __init__(self, chunks: AsyncGenerator[bytes, None], first: bytes, name: str) -> None:
        self._chunks = chunks
        self._pending: bytes | None = first
        self.name = name
        self.received = 0

    def __aiter__(self) -> "DirectoryStream":
        return self

    async def __anext__(self) -> bytes:
        if self._pending is not None:
            chunk, self._pending = self._pending, None
        else:
            chunk = await self._chunks.__anext__()
        self.received += len(chunk)
        return chunk

    async def aclose(self) -> None:
        """Close the response; safe to call after the last chunk or more than once."""
        await self._chunks.aclose()


async def open_directory(http: EngineHttp, container_id: str, path: str) -> DirectoryStream:
    """Start streaming the directory at `path`, refusing anything that is not a directory.

    There is no size limit and no total time limit: the read ends when the Engine ends it, and
    fails when no byte arrives for `STALL_SECONDS`. ArchiveError subclasses for a missing or
    non-directory path and for a stream with no body.
    """
    environment_id = http.environment_id
    if _CONTAINER_ID.fullmatch(container_id) is None:
        raise EngineError(environment_id, "archive reads need a full container id")
    _requested_name(environment_id, path)
    stats: list[PathStat] = []

    def check_stat(headers: Mapping[str, str]) -> None:
        stats.append(_checked_directory(environment_id, headers.get(STAT_HEADER)))

    chunks = http.stream_bytes(
        "GET",
        f"/containers/{container_id}/archive",
        params={"path": path},
        timeouts=replace(Timeouts.standard(), read=STALL_SECONDS),
        on_headers=check_stat,
    )
    try:
        first = await chunks.__anext__()
    except StopAsyncIteration:
        raise ArchiveMalformed(environment_id, "the archive has no body") from None
    except NotFound:
        raise FileMissing(environment_id, "the path does not exist") from None
    except EngineError as error:
        if error.status_code == _BAD_REQUEST:
            raise FileMissing(environment_id, "the path does not exist") from None
        raise
    except BaseException:
        await chunks.aclose()
        raise
    return DirectoryStream(chunks, first, stats[0].name)


async def write_archive(
    http: EngineHttp, container_id: str, path: str, chunks: AsyncIterator[bytes]
) -> None:
    """Send a tar as a chunked body to be extracted into the directory at `path`.

    The Engine never replaces a directory with a non-directory or the reverse. There is no total
    time limit; a write that stalls for `STALL_SECONDS` fails, and the reply is waited for
    `WRITE_REPLY_SECONDS`. Cancelling closes the request. ArchiveRejected when the Engine
    answers 400, FileMissing when it answers 404.
    """
    environment_id = http.environment_id
    if _CONTAINER_ID.fullmatch(container_id) is None:
        raise EngineError(environment_id, "archive writes need a full container id")
    _requested_name(environment_id, path)
    try:
        await http.request(
            "PUT",
            f"/containers/{container_id}/archive",
            params={"path": path, "noOverwriteDirNonDir": "true"},
            content=chunks,
            headers={"Content-Type": "application/x-tar"},
            timeouts=Timeouts(connect=5.0, read=WRITE_REPLY_SECONDS, write=STALL_SECONDS, pool=5.0),
            max_bytes=WRITE_REPLY_BYTES,
        )
    except NotFound:
        raise FileMissing(environment_id, "the path does not exist") from None
    except EngineError as error:
        if error.status_code == _BAD_REQUEST:
            raise ArchiveRejected(environment_id, "the Engine refused the archive") from None
        raise


def _checked_directory(environment_id: int, header: str | None) -> PathStat:
    stat = parse_stat_header(environment_id, header)
    if not stat.mode & _GO_MODE_DIR or stat.mode & _GO_MODE_SYMLINK or stat.link_target:
        raise NotADirectory(environment_id, "the path is not a directory")
    return stat


def _requested_name(environment_id: int, path: str) -> str:
    try:
        size = len(path.encode("utf-8"))
    except UnicodeEncodeError:
        size = PATH_MAX_BYTES + 1
    if (
        not path.startswith("/")
        or size > PATH_MAX_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or posixpath.normpath(path) != path
        or path == "/"
    ):
        raise InvalidArchivePath(environment_id, "the path is not an absolute, normalised path")
    return posixpath.basename(path)


def _checked_stat(environment_id: int, header: str | None, limit: int) -> PathStat:
    stat = parse_stat_header(environment_id, header)
    if stat.mode & _GO_MODE_SYMLINK or stat.link_target:
        raise SymlinkRefused(environment_id, "the path is a symbolic link")
    if stat.mode & _GO_MODE_TYPE_MASK:
        raise NotARegularFile(environment_id, "the path is not a regular file")
    if stat.size > limit:
        raise FileTooLarge(environment_id, "the file is over the limit")
    return stat


def parse_stat_header(environment_id: int, header: str | None) -> PathStat:
    """Decode the base64 JSON stat header; ArchiveMalformed unless every field is well typed."""
    malformed = ArchiveMalformed(environment_id, "the path stat header is malformed")
    if not header or len(header) > STAT_HEADER_MAX_CHARS:
        raise malformed
    try:
        document: Any = json.loads(base64.b64decode(header, validate=True))
    except (binascii.Error, ValueError, RecursionError):
        raise malformed from None
    if not isinstance(document, dict):
        raise malformed
    name = document.get("name")
    size = _count(document.get("size"))
    mode = _count(document.get("mode"))
    link_target = document.get("linkTarget") or ""
    if (
        not isinstance(name, str)
        or not isinstance(link_target, str)
        or size is None
        or mode is None
    ):
        raise malformed
    return PathStat(name=name, size=size, mode=mode, link_target=link_target)


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _single_member(environment_id: int, data: bytes, name: str, limit: int) -> bytes:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            first = tar.next()
            if first is None:
                raise ArchiveMalformed(environment_id, "the archive holds no member")
            if tar.next() is not None:
                raise MemberCountMismatch(environment_id, "the archive holds more than one member")
            if data.count(0, tar.offset) != len(data) - tar.offset:
                raise ArchiveMalformed(environment_id, "the archive has data after its end")
            _check_member(environment_id, first, name, limit)
            source = tar.extractfile(first)
            content = source.read() if source is not None else None
    except (tarfile.TarError, EOFError, ValueError):
        raise ArchiveMalformed(environment_id, "the archive could not be read") from None
    if content is None or len(content) != first.size:
        raise ArchiveMalformed(environment_id, "the archive member could not be read")
    return content


def _check_member(environment_id: int, member: tarfile.TarInfo, name: str, limit: int) -> None:
    if member.issym() or member.islnk():
        raise SymlinkRefused(environment_id, "the archive member is a link")
    if member.type not in _REGULAR_TYPES or member.sparse is not None:
        raise NotARegularFile(environment_id, "the archive member is not a regular file")
    if member.name != name:
        raise MemberNameMismatch(environment_id, "the archive member has another name")
    if member.size > limit:
        raise FileTooLarge(environment_id, "the archive member is over the limit")
