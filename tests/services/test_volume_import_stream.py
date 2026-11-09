"""RewrittenArchive: the rewritten tar as an async iterator over a worker thread."""

import asyncio
import threading
from collections.abc import Generator

import pytest

from app.services.volume_import_archive import ArchiveRefusal, VolumeArchiveRefused
from app.services.volume_import_stream import RewrittenArchive

CHUNK = b"chunk"
STEP_TIMEOUT = 5.0


class SlowSteps:
    """A chunk generator whose second step blocks until released."""

    def __init__(self) -> None:
        self.inside = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.running_when_closed = False
        self._running = False

    def generate(self) -> Generator[bytes, None, None]:
        try:
            yield CHUNK
            self._running = True
            self.inside.set()
            self.release.wait(STEP_TIMEOUT)
            self._running = False
            yield CHUNK
        finally:
            self.running_when_closed = self._running
            self.closed.set()


async def test_chunks_are_yielded_in_order_and_the_byte_count_follows() -> None:
    stream = RewrittenArchive(chunks=iter_of([b"ab", b"cde"]))

    chunks = [chunk async for chunk in stream]
    await stream.aclose()

    assert chunks == [b"ab", b"cde"] and stream.bytes_out == 5


async def test_a_refusal_of_the_file_is_raised_from_the_step_that_found_it() -> None:
    def refusing() -> Generator[bytes, None, None]:
        yield CHUNK
        raise VolumeArchiveRefused(ArchiveRefusal.MALFORMED)

    stream = RewrittenArchive(chunks=refusing())

    assert await stream.__anext__() == CHUNK
    with pytest.raises(VolumeArchiveRefused):
        await stream.__anext__()
    await stream.aclose()


async def test_closing_after_a_read_cancelled_during_a_slow_step_waits_for_the_step() -> None:
    steps = SlowSteps()
    stream = RewrittenArchive(chunks=steps.generate())
    assert await stream.__anext__() == CHUNK
    reading = asyncio.ensure_future(stream.__anext__())
    await asyncio.to_thread(steps.inside.wait, STEP_TIMEOUT)

    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    closing = asyncio.ensure_future(stream.aclose())
    await asyncio.sleep(0.05)
    still_waiting = not closing.done()
    steps.release.set()
    await closing

    assert still_waiting and steps.closed.is_set() and not steps.running_when_closed


def iter_of(chunks: list[bytes]) -> Generator[bytes, None, None]:
    yield from chunks
