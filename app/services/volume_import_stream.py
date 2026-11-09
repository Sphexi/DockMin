"""The rewritten tar of an export archive as an async iterator, read in a worker thread."""

import asyncio
from collections.abc import Generator
from pathlib import Path

from app.services.volume_import_archive import ArchiveLimits, rewrite_volume_archive


class RewrittenArchive:
    """The tar the Engine receives, one chunk per worker-thread step; close it when done.

    A refusal of the file is raised from `__anext__` as VolumeArchiveRefused.
    """

    def __init__(
        self,
        path: Path | None = None,
        limits: ArchiveLimits | None = None,
        *,
        chunks: Generator[bytes, None, None] | None = None,
    ) -> None:
        if (path is None) == (chunks is None):
            raise ValueError("give the archive's path or the chunks of its rewritten tar")
        if chunks is None and path is not None:
            chunks = rewrite_volume_archive(path, limits)
        if chunks is None:
            raise ValueError("no archive")
        self._chunks: Generator[bytes, None, None] = chunks
        self._pending: asyncio.Future[bytes | None] | None = None
        self._done = False
        self.bytes_out = 0

    def __aiter__(self) -> "RewrittenArchive":
        return self

    async def __anext__(self) -> bytes:
        if self._done:
            raise StopAsyncIteration
        self._pending = asyncio.ensure_future(asyncio.to_thread(next, self._chunks, None))
        try:
            chunk = await self._pending
        except BaseException:
            self._done = True
            raise
        if chunk is None:
            self._done = True
            raise StopAsyncIteration
        self.bytes_out += len(chunk)
        return chunk

    async def aclose(self) -> None:
        """Wait for a step in flight, then release the file; safe to call twice."""
        self._done = True
        pending, self._pending = self._pending, None
        if pending is not None:
            await asyncio.wait({pending})
            if not pending.cancelled():
                pending.exception()
        await asyncio.to_thread(self._chunks.close)
