"""Rewrites the Engine's tar of a volume into an export archive on the fly, in a worker thread."""

import asyncio
import concurrent.futures
import gzip
import json
import tarfile
import threading
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final

from app.services.volume_manifest import DATA_DIRECTORY, FORMAT_VERSION, MANIFEST_NAME, SKIPPED_NAME

BLOCK: Final = tarfile.BLOCKSIZE
OUTPUT_CHUNK_BYTES: Final = 64 * 1024
OUTPUT_QUEUE_CHUNKS: Final = 4
EXTENSION_MAX_BYTES: Final = 1024 * 1024
SKIPPED_LIST_MAX: Final = 10_000
GZIP_LEVEL: Final = 6
XATTR_PREFIX: Final = "SCHILY.xattr."

_ZERO_BLOCK: Final = bytes(BLOCK)
_REGULAR: Final = (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.CONTTYPE)
_SKIPPED_TYPES: Final = {
    tarfile.FIFOTYPE: "fifo",
    tarfile.CHRTYPE: "character_device",
    tarfile.BLKTYPE: "block_device",
}
_COPIED_TYPES: Final = (*_REGULAR, tarfile.DIRTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE)
_PAX_TEXT: Final = ("path", "linkpath", "uname", "gname")
_PAX_INTEGER: Final = ("uid", "gid", "size")


class StreamMalformed(Exception):
    """The Engine's tar is not the archive of one directory; the message names no member."""


class _Aborted(Exception):
    """The stream was closed while the worker was running."""


@dataclass(frozen=True, slots=True)
class SkippedMember:
    """A member left out of the archive."""

    path: str
    kind: str


class _Source:
    """A blocking reader over the Engine's chunks; `pull` returns the next chunk or None."""

    def __init__(self, pull: Callable[[], bytes | None]) -> None:
        self._pull = pull
        self._chunk = b""
        self._position = 0
        self._ended = False

    def read_exact(self, size: int) -> bytes:
        """Return `size` bytes, fewer only at the end of the stream."""
        parts: list[bytes] = []
        wanted = size
        while wanted > 0:
            if self._position >= len(self._chunk):
                if self._ended:
                    break
                chunk = self._pull()
                if chunk is None:
                    self._ended = True
                    break
                self._chunk, self._position = chunk, 0
                continue
            part = self._chunk[self._position : self._position + wanted]
            self._position += len(part)
            wanted -= len(part)
            parts.append(part)
        return parts[0] if len(parts) == 1 else b"".join(parts)

    def skip(self, size: int) -> None:
        """Discard `size` bytes; StreamMalformed if the stream ends first."""
        while size > 0:
            taken = len(self.read_exact(min(size, OUTPUT_CHUNK_BYTES)))
            if taken == 0:
                raise StreamMalformed("the archive ends inside a member")
            size -= taken

    def finish(self) -> None:
        """Check that the rest of the stream is the end-of-archive padding."""
        remaining = 0
        while chunk := self.read_exact(OUTPUT_CHUNK_BYTES):
            if chunk.count(0) != len(chunk):
                raise StreamMalformed("the archive has data after its end")
            remaining += len(chunk)
        if remaining < BLOCK:
            raise StreamMalformed("the archive has no end marker")


class _Body:
    """The data of one member, readable once and consumed to its padding before the next."""

    def __init__(self, source: _Source, size: int) -> None:
        self._source = source
        self.left = size
        self._padding = -size % BLOCK

    def read(self, size: int = -1) -> bytes:
        wanted = self.left if size < 0 else min(size, self.left)
        data = self._source.read_exact(wanted)
        if len(data) < wanted:
            raise StreamMalformed("the archive ends inside a member")
        self.left -= len(data)
        return data

    def drain(self) -> None:
        self._source.skip(self.left + self._padding)
        self.left = 0
        self._padding = 0


@dataclass(slots=True)
class _Entry:
    info: tarfile.TarInfo
    body: _Body


class _Reader:
    """Reads tar members one at a time, holding at most `EXTENSION_MAX_BYTES` for any header."""

    def __init__(self, source: _Source) -> None:
        self._source = source
        self._current: _Body | None = None

    def next(self) -> _Entry | None:
        """Return the next member, or None after the end-of-archive marker."""
        if self._current is not None:
            self._current.drain()
            self._current = None
        long_name: str | None = None
        long_link: str | None = None
        records: dict[str, str] = {}
        while True:
            block = self._source.read_exact(BLOCK)
            if len(block) < BLOCK:
                raise StreamMalformed("the archive has no end marker")
            if block == _ZERO_BLOCK:
                return None
            try:
                info = tarfile.TarInfo.frombuf(block, "utf-8", "surrogateescape")
            except tarfile.HeaderError:
                raise StreamMalformed("the archive has a header that cannot be read") from None
            if info.type in (tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
                text = self._extension(info).split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
                if info.type == tarfile.GNUTYPE_LONGNAME:
                    long_name = text
                else:
                    long_link = text
            elif info.type in (tarfile.XHDTYPE, tarfile.SOLARIS_XHDTYPE):
                records.update(_pax_records(self._extension(info)))
            elif info.type == tarfile.XGLTYPE:
                self._extension(info)
            else:
                return self._entry(info, long_name, long_link, records)

    def _extension(self, info: tarfile.TarInfo) -> bytes:
        if info.size > EXTENSION_MAX_BYTES:
            raise StreamMalformed("the archive has a header that is too large")
        data = self._source.read_exact(info.size)
        if len(data) < info.size:
            raise StreamMalformed("the archive ends inside a header")
        self._source.skip(-info.size % BLOCK)
        return data

    def _entry(
        self,
        info: tarfile.TarInfo,
        long_name: str | None,
        long_link: str | None,
        records: dict[str, str],
    ) -> _Entry:
        if long_name is not None:
            info.name = long_name
        if long_link is not None:
            info.linkname = long_link
        _apply_pax(info, records)
        info.name = info.name.rstrip("/") or info.name
        carries_data = (
            info.type in tarfile.REGULAR_TYPES or info.type not in tarfile.SUPPORTED_TYPES
        )
        body = _Body(self._source, info.size if carries_data else 0)
        self._current = body
        return _Entry(info, body)


def _pax_records(data: bytes) -> dict[str, str]:
    records: dict[str, str] = {}
    position = 0
    try:
        while position < len(data):
            space = data.index(b" ", position)
            end = position + int(data[position:space])
            if end <= space + 1 or end > len(data) or data[end - 1 : end] != b"\n":
                raise StreamMalformed("the archive has a header that cannot be read")
            key, _, value = data[space + 1 : end - 1].partition(b"=")
            records[key.decode("utf-8")] = value.decode("utf-8", "surrogateescape")
            position = end
    except ValueError:
        raise StreamMalformed("the archive has a header that cannot be read") from None
    return records


def _apply_pax(info: tarfile.TarInfo, records: dict[str, str]) -> None:
    try:
        for key in _PAX_TEXT:
            if key in records:
                setattr(info, {"path": "name", "linkpath": "linkname"}.get(key, key), records[key])
        for key in _PAX_INTEGER:
            if key in records:
                setattr(info, key, int(records[key]))
        if "mtime" in records:
            info.mtime = float(records["mtime"])
    except ValueError:
        raise StreamMalformed("the archive has a header that cannot be read") from None
    info.pax_headers = {k: v for k, v in records.items() if k.startswith(XATTR_PREFIX)}


class _Sink:
    """Collects the gzip stream into chunks and hands each to `emit`."""

    def __init__(self, emit: Callable[[bytes], None]) -> None:
        self._emit = emit
        self._buffer = bytearray()
        self._dropped = False

    def write(self, data: bytes) -> int:
        if self._dropped:
            return len(data)
        self._buffer += data
        if len(self._buffer) >= OUTPUT_CHUNK_BYTES:
            self._hand_over()
        return len(data)

    def drop(self) -> None:
        """Discard buffered and later output, so a failed archive has no gzip trailer."""
        self._dropped = True
        self._buffer.clear()

    def flush(self) -> None:
        return None

    def finish(self) -> None:
        if self._buffer:
            self._hand_over()

    def _hand_over(self) -> None:
        chunk = bytes(self._buffer)
        self._buffer.clear()
        self._emit(chunk)


class RewriteStream:
    """The export archive as gzip chunks, manifest first, with the volume's files under `data/`.

    A worker thread asks `source` for a chunk only when it needs one and hands output to a queue
    of four chunks, so memory stays bounded however large a member is. Devices and FIFOs are
    left out and listed in a trailing `skipped.json`. StreamMalformed unless `source` is the
    complete tar of one directory; an error of `source` is raised as it came. Call `aclose`
    when done reading.
    """

    def __init__(self, source: AsyncIterator[bytes], manifest: bytes, *, root: str) -> None:
        self._source = source
        self._manifest = manifest
        self._root = root
        self._output: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue(
            OUTPUT_QUEUE_CHUNKS
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._finished = asyncio.Event()
        self._aborted = threading.Event()
        self._lock = threading.Lock()
        self._pending: set[concurrent.futures.Future[Any]] = set()
        self._done = False
        self.skipped: list[SkippedMember] = []
        self.skipped_count = 0
        self.entries = 0
        self.bytes_out = 0

    def __aiter__(self) -> "RewriteStream":
        return self

    async def __anext__(self) -> bytes:
        if self._done:
            raise StopAsyncIteration
        if self._thread is None:
            self._start()
        item = await self._output.get()
        if item is None:
            self._done = True
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            self._done = True
            raise item
        self.bytes_out += len(item)
        return item

    async def aclose(self) -> None:
        """Stop the worker and wait for it; safe to call after the last chunk or twice."""
        self._done = True
        self._abort()
        if self._thread is not None:
            await self._finished.wait()

    def _start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._run, name="volume-export", daemon=True)
        self._thread.start()

    def _abort(self) -> None:
        self._aborted.set()
        with self._lock:
            pending = list(self._pending)
        for future in pending:
            future.cancel()

    def _run(self) -> None:
        loop = self._loop
        assert loop is not None
        try:
            self._rewrite()
            outcome: BaseException | None = None
        except _Aborted:
            outcome = _Aborted()
        except BaseException as error:
            outcome = error
        try:
            if not isinstance(outcome, _Aborted):
                self._call(self._output.put(outcome))
        except _Aborted:
            pass
        finally:
            loop.call_soon_threadsafe(self._finished.set)

    def _call[T](self, work: Coroutine[Any, Any, T]) -> T:
        loop = self._loop
        assert loop is not None
        if self._aborted.is_set():
            work.close()
            raise _Aborted
        future = asyncio.run_coroutine_threadsafe(work, loop)
        with self._lock:
            self._pending.add(future)
        if self._aborted.is_set():
            future.cancel()
        try:
            return future.result()
        except concurrent.futures.CancelledError:
            raise _Aborted from None
        finally:
            with self._lock:
                self._pending.discard(future)

    async def _next_chunk(self) -> bytes | None:
        try:
            return await self._source.__anext__()
        except StopAsyncIteration:
            return None

    def _rewrite(self) -> None:
        source = _Source(lambda: self._call(self._next_chunk()))
        sink = _Sink(lambda chunk: self._call(self._output.put(chunk)))
        with (
            gzip.GzipFile(fileobj=sink, mode="wb", compresslevel=GZIP_LEVEL, mtime=0) as compressed,
            tarfile.open(
                fileobj=compressed,
                mode="w|",
                format=tarfile.PAX_FORMAT,
                bufsize=OUTPUT_CHUNK_BYTES,
            ) as out,
        ):
            try:
                self._add_document(out, MANIFEST_NAME, self._manifest)
                self._copy(_Reader(source), out)
                source.finish()
                self._add_document(out, SKIPPED_NAME, self._skipped_document())
            except BaseException:
                sink.drop()
                raise
        sink.finish()

    def _copy(self, reader: _Reader, out: tarfile.TarFile) -> None:
        first = reader.next()
        if first is None or first.info.name != self._root or not first.info.isdir():
            raise StreamMalformed("the archive does not start with the directory that was read")
        self._copy_entry(out, first, DATA_DIRECTORY)
        while (entry := reader.next()) is not None:
            relative = self._relative(entry.info.name)
            self._copy_entry(out, entry, f"{DATA_DIRECTORY}/{relative}", relative)

    def _relative(self, name: str) -> str:
        prefix = f"{self._root}/"
        relative = name.removeprefix(prefix) if name.startswith(prefix) else ""
        if not relative or any(part in ("", ".", "..") for part in relative.split("/")):
            raise StreamMalformed("the archive has a member outside the directory that was read")
        return relative

    def _copy_entry(
        self, out: tarfile.TarFile, entry: _Entry, target: str, relative: str = ""
    ) -> None:
        info = entry.info
        if info.type in _SKIPPED_TYPES or info.type not in _COPIED_TYPES:
            self._skip(relative, _SKIPPED_TYPES.get(info.type, "other"))
            return
        written = tarfile.TarInfo(target)
        written.mode, written.uid, written.gid = info.mode, info.uid, info.gid
        written.uname, written.gname, written.mtime = info.uname, info.gname, info.mtime
        written.pax_headers = dict(info.pax_headers)
        if info.type == tarfile.DIRTYPE:
            written.type = tarfile.DIRTYPE
        elif info.type == tarfile.SYMTYPE:
            written.type, written.linkname = tarfile.SYMTYPE, info.linkname
        elif info.type == tarfile.LNKTYPE:
            written.type = tarfile.LNKTYPE
            written.linkname = f"{DATA_DIRECTORY}/{self._relative(info.linkname)}"
        else:
            written.type, written.size = tarfile.REGTYPE, info.size
        out.addfile(written, entry.body if written.size else None)
        self.entries += 1

    def _skip(self, path: str, kind: str) -> None:
        self.skipped_count += 1
        if len(self.skipped) < SKIPPED_LIST_MAX:
            self.skipped.append(SkippedMember(path, kind))

    def _skipped_document(self) -> bytes:
        document = {
            "format_version": FORMAT_VERSION,
            "count": self.skipped_count,
            "truncated": self.skipped_count > len(self.skipped),
            "skipped": [{"path": item.path, "type": item.kind} for item in self.skipped],
        }
        return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")

    @staticmethod
    def _add_document(out: tarfile.TarFile, name: str, content: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        info.mode = 0o644
        out.addfile(info, _Memory(content))


class _Memory:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        end = len(self._content) if size < 0 else self._position + size
        data = self._content[self._position : end]
        self._position += len(data)
        return data
