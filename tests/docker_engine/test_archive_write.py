"""Tests for ContainersApi.put_archive: a tar streamed into a container directory."""

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from app.docker_engine import EngineClient
from app.docker_engine.archive import (
    STALL_SECONDS,
    WRITE_REPLY_SECONDS,
    ArchiveRejected,
    FileMissing,
)
from app.docker_engine.errors import EngineError
from tests.docker_engine.fake_engine import (
    FakeEngine,
    Message,
    Receive,
    Reply,
    Scope,
    Send,
    error_reply,
)

CONTAINER = "ab" * 32
ARCHIVE_PATH = f"/containers/{CONTAINER}/archive"
DIRECTORY = "/dockmin-read"
PIECES = [b"first-", b"second-", b"third"]


class Receiving(FakeEngine):
    """A fake whose PUT answers once it has read the whole request body, keeping its pieces."""

    def __init__(self) -> None:
        super().__init__()
        self.pieces: list[bytes] = []
        self.reply = Reply(200)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["method"] != "PUT":
            await super().__call__(scope, receive, send)
            return
        more = True
        while more:
            message: Message = await receive()
            if message["type"] != "http.request":
                return
            self.pieces.append(message.get("body", b""))
            more = message.get("more_body", False)
        await send({"type": "http.response.start", "status": self.reply.status, "headers": []})
        await send({"type": "http.response.body", "body": self.reply.body})


@pytest.fixture
def receiving() -> Receiving:
    return Receiving()


@pytest.fixture
def client(receiving: Receiving, make_client: Callable[[FakeEngine], EngineClient]) -> EngineClient:
    return make_client(receiving)


async def pieces() -> AsyncIterator[bytes]:
    for piece in PIECES:
        yield piece


async def test_the_tar_is_sent_as_a_chunked_body_with_the_directory_and_the_overwrite_guard(
    receiving: Receiving, client: EngineClient
) -> None:
    await client.containers.put_archive(CONTAINER, DIRECTORY, pieces())

    (call,) = receiving.calls("PUT", ARCHIVE_PATH)
    assert b"".join(receiving.pieces) == b"".join(PIECES)
    assert call.query == {"path": DIRECTORY, "noOverwriteDirNonDir": "true"}
    assert call.headers["content-type"] == "application/x-tar"
    assert call.headers["transfer-encoding"] == "chunked" and "content-length" not in call.headers


@pytest.mark.parametrize(
    ("status", "expected"),
    [(400, ArchiveRejected), (404, FileMissing), (500, EngineError)],
)
async def test_an_engine_refusal_is_mapped_without_its_text(
    receiving: Receiving, client: EngineClient, status: int, expected: type[EngineError]
) -> None:
    receiving.reply = error_reply(status)

    with pytest.raises(expected) as raised:
        await client.containers.put_archive(CONTAINER, DIRECTORY, pieces())

    assert type(raised.value) is expected
    assert "engine detail" not in str(raised.value)


async def test_the_write_has_a_stall_limit_and_a_bounded_wait_for_the_reply(
    receiving: Receiving, client: EngineClient
) -> None:
    await client.containers.put_archive(CONTAINER, DIRECTORY, pieces())

    timeout = receiving.calls("PUT", ARCHIVE_PATH)[0].timeout
    assert timeout is not None
    assert (timeout["write"], timeout["read"]) == (STALL_SECONDS, WRITE_REPLY_SECONDS)


async def test_cancelling_the_write_closes_the_source_of_the_body(
    receiving: Receiving, client: EngineClient
) -> None:
    closed = asyncio.Event()
    started = asyncio.Event()

    async def source() -> AsyncIterator[bytes]:
        try:
            yield b"first"
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    writing = asyncio.ensure_future(client.containers.put_archive(CONTAINER, DIRECTORY, source()))
    await started.wait()
    writing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writing

    assert closed.is_set()


@pytest.mark.parametrize(
    ("container_id", "path"),
    [("short", DIRECTORY), (CONTAINER, "dockmin-read"), (CONTAINER, "/a/../b")],
    ids=["short-id", "relative-path", "unnormalised-path"],
)
async def test_a_bad_container_or_path_is_refused_unsent(
    receiving: Receiving, client: EngineClient, container_id: str, path: str
) -> None:
    with pytest.raises(EngineError):
        await client.containers.put_archive(container_id, path, pieces())

    assert receiving.calls("PUT") == []
