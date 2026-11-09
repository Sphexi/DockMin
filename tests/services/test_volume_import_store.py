"""The spool that stores an upload, and the store that holds it between review and import."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import app.services.volume_import_store as spool_module
from app.services.volume_import_archive import ArchiveSummary, inspect_volume_archive
from app.services.volume_import_store import (
    CAPACITY,
    LIFETIME_SECONDS,
    SPOOL_DIRECTORY,
    ImportStoreFull,
    SpooledUpload,
    SpoolFailure,
    VolumeImportStore,
    spool_upload,
    sweep_import_leftovers,
)
from tests.volume_import_support import export_archive, file

USER, OTHER_USER, SESSION, ENVIRONMENT = 1, 2, 10, 3
UPLOAD_ID = "a" * 32


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def parts(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def broken(first: bytes) -> AsyncIterator[bytes]:
    yield first
    raise ConnectionResetError


def spooled(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / SPOOL_DIRECTORY).glob("*"))


@pytest.fixture
def summary(tmp_path: Path) -> ArchiveSummary:
    path = tmp_path / "summary.tar.gz"
    path.write_bytes(export_archive(file("a")))
    return inspect_volume_archive(path)


async def make_upload(tmp_path: Path, name: str) -> SpooledUpload:
    return await spool_upload(parts(b"data"), tmp_path, name)


async def test_an_upload_is_written_to_a_private_file_and_its_digest_returned(
    tmp_path: Path,
) -> None:
    upload = await spool_upload(parts(b"abc", b"def"), tmp_path, UPLOAD_ID)

    assert upload.path.read_bytes() == b"abcdef" and upload.size == 6
    assert upload.digest == "bef57ec7f53a6d40beb640a780a639c83bc29ac8a9816f1fc6c5c6dcd93c4721"
    assert upload.path.stat().st_mode & 0o777 == 0o600
    assert spooled(tmp_path) == [upload.path]


async def test_an_upload_over_the_cap_is_refused_and_leaves_no_file(tmp_path: Path) -> None:
    with pytest.raises(SpoolFailure) as raised:
        await spool_upload(parts(b"abcdef"), tmp_path, UPLOAD_ID, limit=5)

    assert raised.value.reason == "too_large" and spooled(tmp_path) == []


async def test_an_upload_is_refused_before_a_byte_is_read_when_the_disk_is_too_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spool_module, "_free_bytes", lambda directory: 1024)
    consumed: list[bytes] = []

    async def spy() -> AsyncIterator[bytes]:
        consumed.append(b"read")
        yield b"abc"

    with pytest.raises(SpoolFailure) as raised:
        await spool_upload(spy(), tmp_path, UPLOAD_ID)

    assert raised.value.reason == "insufficient_space" and consumed == []
    assert spooled(tmp_path) == []


async def test_a_declared_size_larger_than_the_free_space_is_refused_up_front(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spool_module, "_free_bytes", lambda directory: 1024**3)

    with pytest.raises(SpoolFailure) as raised:
        await spool_upload(parts(b"a"), tmp_path, UPLOAD_ID, declared_size=2 * 1024**3)

    assert raised.value.reason == "insufficient_space"


async def test_the_disk_filling_during_the_upload_stops_it_and_removes_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    free = iter([1024**3, 1024**3, 1024])
    monkeypatch.setattr(spool_module, "_free_bytes", lambda directory: next(free, 1024))
    monkeypatch.setattr(spool_module, "WRITE_BUFFER_BYTES", 1)

    with pytest.raises(SpoolFailure):
        await spool_upload(parts(b"a", b"b", b"c"), tmp_path, UPLOAD_ID)

    assert spooled(tmp_path) == []


async def test_a_connection_that_breaks_mid_upload_leaves_no_file(tmp_path: Path) -> None:
    with pytest.raises(ConnectionResetError):
        await spool_upload(broken(b"abc"), tmp_path, UPLOAD_ID)

    assert spooled(tmp_path) == []


async def test_a_cancelled_upload_leaves_no_file(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def stalled() -> AsyncIterator[bytes]:
        yield b"abc"
        started.set()
        await asyncio.Event().wait()

    task = asyncio.ensure_future(spool_upload(stalled(), tmp_path, UPLOAD_ID))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert spooled(tmp_path) == []


async def test_a_hold_is_usable_only_by_its_user_session_and_environment(
    tmp_path: Path, summary: ArchiveSummary
) -> None:
    store = VolumeImportStore(Clock())
    upload = await make_upload(tmp_path, "one")
    held = await store.hold(
        UPLOAD_ID,
        user_id=USER,
        session_id=SESSION,
        environment_id=ENVIRONMENT,
        upload=upload,
        summary=summary,
    )

    async def look(**changes: int) -> object:
        keys = {"user_id": USER, "session_id": SESSION, "environment_id": ENVIRONMENT, **changes}
        return await store.get(UPLOAD_ID, **keys)

    assert await look() is held
    assert await look(user_id=OTHER_USER) is None
    assert await look(session_id=SESSION + 1) is None
    assert await look(environment_id=ENVIRONMENT + 1) is None
    await store.aclose()


async def test_a_hold_expires_and_its_file_is_removed(
    tmp_path: Path, summary: ArchiveSummary
) -> None:
    clock = Clock()
    store = VolumeImportStore(clock)
    upload = await make_upload(tmp_path, "one")
    await store.hold(
        UPLOAD_ID,
        user_id=USER,
        session_id=SESSION,
        environment_id=ENVIRONMENT,
        upload=upload,
        summary=summary,
    )

    clock.now = LIFETIME_SECONDS + 1
    gone = await store.get(UPLOAD_ID, user_id=USER, session_id=SESSION, environment_id=ENVIRONMENT)

    assert gone is None and not upload.path.exists()
    await store.aclose()


async def test_a_hold_whose_import_has_begun_does_not_expire(
    tmp_path: Path, summary: ArchiveSummary
) -> None:
    clock = Clock()
    store = VolumeImportStore(clock)
    upload = await make_upload(tmp_path, "one")
    held = await store.hold(
        UPLOAD_ID,
        user_id=USER,
        session_id=SESSION,
        environment_id=ENVIRONMENT,
        upload=upload,
        summary=summary,
    )
    held.importing = True

    clock.now = LIFETIME_SECONDS * 2
    await store.sweep()

    assert upload.path.exists()
    await store.discard(UPLOAD_ID)
    assert not upload.path.exists()
    await store.aclose()


async def test_a_newer_upload_by_the_same_user_replaces_the_earlier_one(
    tmp_path: Path, summary: ArchiveSummary
) -> None:
    store = VolumeImportStore(Clock())
    first, second = await make_upload(tmp_path, "one"), await make_upload(tmp_path, "two")
    for name, upload in (("1" * 32, first), ("2" * 32, second)):
        await store.hold(
            name,
            user_id=USER,
            session_id=SESSION,
            environment_id=ENVIRONMENT,
            upload=upload,
            summary=summary,
        )

    assert not first.path.exists() and second.path.exists()
    assert (
        await store.get("1" * 32, user_id=USER, session_id=SESSION, environment_id=ENVIRONMENT)
        is None
    )
    await store.aclose()


async def test_a_new_hold_is_refused_when_other_users_fill_the_store_and_theirs_survive(
    tmp_path: Path, summary: ArchiveSummary
) -> None:
    store = VolumeImportStore(Clock())
    uploads = [await make_upload(tmp_path, f"u{index}") for index in range(CAPACITY + 1)]
    for index, upload in enumerate(uploads[:CAPACITY]):
        await store.hold(
            f"{index:032x}",
            user_id=index,
            session_id=SESSION,
            environment_id=ENVIRONMENT,
            upload=upload,
            summary=summary,
        )
    newcomer = CAPACITY

    assert not await store.has_room(newcomer)
    with pytest.raises(ImportStoreFull):
        await store.hold(
            f"{newcomer:032x}",
            user_id=newcomer,
            session_id=SESSION,
            environment_id=ENVIRONMENT,
            upload=uploads[newcomer],
            summary=summary,
        )

    survivors = [
        await store.get(
            f"{index:032x}", user_id=index, session_id=SESSION, environment_id=ENVIRONMENT
        )
        for index in range(CAPACITY)
    ]
    assert all(upload.path.exists() for upload in uploads[:CAPACITY])
    assert all(held is not None for held in survivors)
    await store.aclose()


async def test_a_full_store_still_takes_a_replacement_from_a_user_who_holds_one(
    tmp_path: Path, summary: ArchiveSummary
) -> None:
    store = VolumeImportStore(Clock())
    uploads = [await make_upload(tmp_path, f"u{index}") for index in range(CAPACITY + 1)]
    for index, upload in enumerate(uploads[:CAPACITY]):
        await store.hold(
            f"{index:032x}",
            user_id=index,
            session_id=SESSION,
            environment_id=ENVIRONMENT,
            upload=upload,
            summary=summary,
        )

    assert await store.has_room(0)
    await store.hold(
        f"{CAPACITY:032x}",
        user_id=0,
        session_id=SESSION,
        environment_id=ENVIRONMENT,
        upload=uploads[CAPACITY],
        summary=summary,
    )

    assert not uploads[0].path.exists() and uploads[CAPACITY].path.exists()
    await store.aclose()
    assert not any(upload.path.exists() for upload in uploads)


def test_the_startup_sweep_removes_every_spooled_upload(tmp_path: Path) -> None:
    directory = tmp_path / SPOOL_DIRECTORY
    directory.mkdir()
    (directory / "left.tar.gz").write_bytes(b"x")

    sweep_import_leftovers(tmp_path)
    sweep_import_leftovers(tmp_path)

    assert not directory.exists()
