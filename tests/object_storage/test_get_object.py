"""Streaming an object down: declared length, caps, short and long bodies, and failures."""

from collections.abc import AsyncIterator

import httpx
import pytest

from app.object_storage import StorageFailure
from tests.object_storage.support import BUCKET
from tests.object_storage.wire_support import Wire, client_for, error_reply

KEY = "backups/rule-1/dockmin-backup-20260101T000000Z.tar.gz"
BODY = b"0123456789"
HALVES = (BODY[:4], BODY[4:])
LIMIT = 1024
HTTP_ERROR = 500
PARTIAL_CONTENT = 206


class Chunked(httpx.AsyncByteStream):
    """A body of several chunks that notes whether anything read it, and can fail midway."""

    def __init__(self, *chunks: bytes, then: Exception | None = None) -> None:
        self.chunks = chunks
        self.then = then
        self.read = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read = True
        for chunk in self.chunks:
            yield chunk
        if self.then is not None:
            raise self.then


def reply(
    stream: Chunked, *, length: int | None, extra: dict[str, str] | None = None
) -> httpx.Response:
    headers = {} if length is None else {"content-length": str(length)}
    return httpx.Response(200, headers=headers | (extra or {}), stream=stream)


async def download(wire: Wire, *, limit: int = LIMIT) -> tuple[int, bytes]:
    async with client_for(wire).get_object(KEY, max_bytes=limit) as body:
        return body.size, b"".join([chunk async for chunk in body.chunks()])


async def test_an_object_is_streamed_whole_with_its_declared_size() -> None:
    wire = Wire(reply(Chunked(*HALVES), length=len(BODY)))

    size, received = await download(wire)

    [sent] = wire.seen
    assert (size, received) == (len(BODY), BODY)
    assert (sent.method, sent.path) == ("GET", f"/{BUCKET}/{KEY}")
    assert sent.headers["accept-encoding"] == "identity"


async def test_an_object_exactly_at_the_limit_is_accepted() -> None:
    size, received = await download(Wire(reply(Chunked(BODY), length=len(BODY))), limit=len(BODY))

    assert (size, received) == (len(BODY), BODY)


async def test_an_empty_object_streams_no_chunks() -> None:
    size, received = await download(Wire(reply(Chunked(), length=0)))

    assert (size, received) == (0, b"")


async def test_an_object_over_the_limit_is_refused_before_any_byte_is_read() -> None:
    stream = Chunked(BODY)

    with pytest.raises(StorageFailure) as raised:
        await download(Wire(reply(stream, length=len(BODY))), limit=len(BODY) - 1)

    assert (raised.value.kind, stream.read) == ("storage_error", False)


@pytest.mark.parametrize("length", [None, "", "abc", "-1", "1.5"])
async def test_a_reply_without_a_usable_content_length_is_refused_before_any_byte_is_read(
    length: str | None,
) -> None:
    stream = Chunked(BODY)
    headers = {} if length is None else {"content-length": length}

    with pytest.raises(StorageFailure) as raised:
        await download(Wire(httpx.Response(200, headers=headers, stream=stream)))

    assert (raised.value.kind, raised.value.status, stream.read) == ("storage_error", 200, False)


async def test_a_compressed_reply_is_refused() -> None:
    wire = Wire(reply(Chunked(BODY), length=len(BODY), extra={"content-encoding": "gzip"}))

    with pytest.raises(StorageFailure) as raised:
        await download(wire)

    assert raised.value.kind == "storage_error"


async def test_a_success_status_other_than_200_is_refused() -> None:
    wire = Wire(httpx.Response(PARTIAL_CONTENT, content=BODY))

    with pytest.raises(StorageFailure) as raised:
        await download(wire)

    assert (raised.value.kind, raised.value.status) == ("storage_error", PARTIAL_CONTENT)


async def test_a_body_longer_than_its_declared_size_fails() -> None:
    wire = Wire(reply(Chunked(BODY), length=len(BODY) - 1))

    with pytest.raises(StorageFailure) as raised:
        await download(wire)

    assert raised.value.kind == "storage_error"


async def test_a_body_shorter_than_its_declared_size_fails() -> None:
    wire = Wire(reply(Chunked(BODY[:-1]), length=len(BODY)))

    with pytest.raises(StorageFailure) as raised:
        await download(wire)

    assert raised.value.kind == "storage_error"


async def test_a_body_that_breaks_off_midway_fails_as_unreachable() -> None:
    stream = Chunked(HALVES[0], then=httpx.ReadError("reset"))

    with pytest.raises(StorageFailure) as raised:
        await download(Wire(reply(stream, length=len(BODY))))

    assert raised.value.kind == "unreachable"


async def test_the_body_can_be_read_only_once() -> None:
    wire = Wire(reply(Chunked(BODY), length=len(BODY)))

    async with client_for(wire).get_object(KEY, max_bytes=LIMIT) as body:
        _ = [chunk async for chunk in body.chunks()]
        with pytest.raises(RuntimeError):
            _ = [chunk async for chunk in body.chunks()]


@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        (error_reply(404, "NoSuchKey"), "object_missing"),
        (error_reply(404, "NoSuchBucket"), "bucket_missing"),
        (error_reply(403, "AccessDenied"), "refused"),
        (error_reply(403, "RequestTimeTooSkewed"), "clock_skew"),
        (httpx.Response(HTTP_ERROR), "storage_error"),
    ],
)
async def test_an_error_reply_is_classified_by_its_code(answer: httpx.Response, kind: str) -> None:
    with pytest.raises(StorageFailure) as raised:
        await download(Wire(answer))

    assert raised.value.kind == kind


async def test_a_request_that_cannot_connect_fails_as_unreachable() -> None:
    with pytest.raises(StorageFailure) as raised:
        await download(Wire(httpx.ConnectError("down")))

    assert raised.value.kind == "unreachable"


async def test_a_key_outside_the_alphabet_or_a_negative_limit_sends_nothing() -> None:
    wire = Wire()
    client = client_for(wire)

    with pytest.raises(StorageFailure) as bad_key:
        async with client.get_object("../x", max_bytes=LIMIT):
            pass
    with pytest.raises(ValueError, match="max_bytes"):
        async with client.get_object(KEY, max_bytes=-1):
            pass

    assert (bad_key.value.kind, wire.seen) == ("key_invalid", [])
