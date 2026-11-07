"""Streaming a file to the bucket: signed length, digests, failures and the file handle."""

import base64
import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.object_storage import MAX_PUT_FILE_BYTES, StorageFailure
from app.object_storage import client as client_module
from tests.object_storage.support import GZIP, make_client, replay, verify_signature

KEY = "backups/rule-1/dockmin-backup-20260101T000000Z.tar.gz"
CHUNK = client_module.UPLOAD_CHUNK
FILE_SIZE = 2 * CHUNK + 17
MIB = 1024 * 1024
ALL_SIZES = [
    (0, 120.0),
    (3 * MIB, 123.0),
    (4 * 1024**3, 4096.0 + 120.0),
    (10**13, client_module.UPLOAD_MAX_TIMEOUT),
]


class Archive:
    """A file on disk with the digests a caller computes before uploading it."""

    def __init__(self, path: Path, data: bytes) -> None:
        self.path = path
        self.size = len(data)
        self.md5 = base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()
        self.sha256 = hashlib.sha256(data).hexdigest()


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Archive:
    data = os.urandom(FILE_SIZE)
    path = tmp_path_factory.mktemp("archive") / "backup.tar.gz"
    path.write_bytes(data)
    return Archive(path, data)


class Store:
    """Accepts a streamed upload after checking it, and keeps what arrived."""

    def __init__(self) -> None:
        self.received: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        verify_signature(request)
        self.received.append(request)
        return httpx.Response(200)


async def upload(
    handler: Callable[[httpx.Request], httpx.Response], file: Archive, **changes: Any
) -> None:
    arguments: dict[str, Any] = {
        "content_type": GZIP,
        "size": file.size,
        "md5_b64": file.md5,
        "sha256_hex": file.sha256,
    }
    await make_client(handler).put_object_file(
        changes.pop("key", KEY), changes.pop("path", file.path), **(arguments | changes)
    )


async def test_a_file_is_streamed_with_its_length_signed_and_the_digests_it_was_given(
    archive: Archive,
) -> None:
    store = Store()

    await upload(store, archive)

    request = store.received[0]
    assert request.content == archive.path.read_bytes()
    assert request.headers["content-length"] == str(archive.size)
    assert request.headers["x-amz-content-sha256"] == archive.sha256
    assert request.headers["content-md5"] == archive.md5


async def test_an_empty_file_is_uploaded_with_a_zero_length(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.write_bytes(b"")
    empty = Archive(path, b"")
    store = Store()

    await upload(store, empty)

    assert store.received[0].headers["content-length"] == "0"


async def test_a_digest_the_server_rejects_is_a_digest_mismatch(archive: Archive) -> None:
    with pytest.raises(StorageFailure) as raised:
        await upload(replay("signed-put-stream-badmd5"), archive)

    assert (raised.value.kind, raised.value.status, raised.value.code) == (
        "digest_mismatch",
        400,
        "BadDigest",
    )


async def test_a_refusal_to_a_large_upload_is_a_refusal_and_not_a_lost_connection(
    archive: Archive,
) -> None:
    with pytest.raises(StorageFailure) as raised:
        await upload(replay("signed-refused-readonly-put-stream-20mib"), archive)

    assert (raised.value.kind, raised.value.status, raised.value.code) == (
        "refused",
        403,
        "AccessDenied",
    )


async def test_a_connection_that_fails_during_the_upload_is_unreachable(archive: Archive) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(StorageFailure) as raised:
        await upload(down, archive)

    assert raised.value.kind == "unreachable"


@pytest.mark.parametrize(
    "changes",
    [
        {"size": FILE_SIZE - 1},
        {"size": MAX_PUT_FILE_BYTES + 1},
        {"size": -1},
        {"md5_b64": "nope"},
        {"sha256_hex": "A" * 64},
        {"content_type": "application/gzip\r\nX-Injected: yes"},
    ],
    ids=["size-differs", "size-over-the-cap", "size-negative", "md5", "sha256", "content-type"],
)
async def test_an_argument_that_does_not_describe_the_file_is_refused_before_any_request(
    archive: Archive, changes: dict[str, Any]
) -> None:
    store = Store()

    with pytest.raises(ValueError):
        await upload(store, archive, **changes)

    assert store.received == []


async def test_a_key_outside_the_alphabet_is_refused_before_any_request(archive: Archive) -> None:
    store = Store()

    with pytest.raises(StorageFailure) as raised:
        await upload(store, archive, key="../x")

    assert (raised.value.kind, store.received) == ("key_invalid", [])


async def test_a_missing_file_raises_and_sends_nothing(archive: Archive, tmp_path: Path) -> None:
    store = Store()

    with pytest.raises(FileNotFoundError):
        await upload(store, archive, path=tmp_path / "absent")

    assert store.received == []


async def test_the_file_is_closed_after_a_success_a_failed_check_and_a_failed_send(
    archive: Archive, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Any] = []
    real_open = Path.open

    def spy(self: Path, *arguments: Any, **options: Any) -> Any:
        handle = real_open(self, *arguments, **options)
        opened.append(handle)
        return handle

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(Path, "open", spy)

    await upload(Store(), archive)
    with pytest.raises(ValueError):
        await upload(Store(), archive, size=FILE_SIZE - 1)
    with pytest.raises(StorageFailure):
        await upload(down, archive)

    assert len(opened) == 3
    assert [handle.closed for handle in opened] == [True, True, True]


@pytest.mark.parametrize(("size", "seconds"), ALL_SIZES)
def test_the_time_allowed_for_an_upload_grows_with_its_size_up_to_a_ceiling(
    size: int, seconds: float
) -> None:
    assert client_module.upload_timeout(size) == seconds
