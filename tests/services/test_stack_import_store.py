"""The store that holds a stack upload between its review and the import."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.services.stack_import_archive import StackArchiveSummary, inspect_stack_archive
from app.services.stack_import_store import (
    CAPACITY,
    LIFETIME_SECONDS,
    SPOOL_DIRECTORY,
    StackImportStore,
)
from app.services.volume_import_store import SpooledUpload, spool_upload, sweep_import_leftovers
from tests.export_archive_support import FIXTURE

USER, OTHER_USER, SESSION, ENVIRONMENT = 1, 2, 10, 3


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def parts(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def spooled(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / SPOOL_DIRECTORY).glob("*"))


@pytest.fixture
def summary() -> StackArchiveSummary:
    return inspect_stack_archive(FIXTURE)


async def make_upload(tmp_path: Path, name: str) -> SpooledUpload:
    return await spool_upload(parts(b"data"), tmp_path, name, directory_name=SPOOL_DIRECTORY)


async def hold(
    store: StackImportStore,
    tmp_path: Path,
    summary: StackArchiveSummary,
    upload_id: str,
    *,
    user: int = USER,
    session: int = SESSION,
    environment: int = ENVIRONMENT,
    kind: str = "compose",
) -> None:
    await store.hold(
        upload_id,
        user_id=user,
        session_id=session,
        environment_id=environment,
        kind=kind,
        upload=await make_upload(tmp_path, upload_id),
        summary=summary,
    )


async def test_a_hold_is_usable_only_by_the_session_user_and_environment_that_made_it(
    tmp_path: Path, summary: StackArchiveSummary
) -> None:
    store = StackImportStore(Clock())
    await hold(store, tmp_path, summary, "a" * 32)

    mine = await store.get("a" * 32, user_id=USER, session_id=SESSION, environment_id=ENVIRONMENT)
    other_session = await store.get(
        "a" * 32, user_id=USER, session_id=SESSION + 1, environment_id=ENVIRONMENT
    )
    other_environment = await store.get(
        "a" * 32, user_id=USER, session_id=SESSION, environment_id=ENVIRONMENT + 1
    )

    assert mine is not None and other_session is None and other_environment is None
    await store.aclose()


async def test_a_newer_upload_by_the_same_user_removes_the_earlier_file(
    tmp_path: Path, summary: StackArchiveSummary
) -> None:
    store = StackImportStore(Clock())
    await hold(store, tmp_path, summary, "a" * 32)
    await hold(store, tmp_path, summary, "b" * 32)

    assert [path.name for path in spooled(tmp_path)] == [f"{'b' * 32}.tar.gz"]
    await store.aclose()
    assert spooled(tmp_path) == []


async def test_capacity_drops_the_oldest_and_expiry_removes_the_file_and_the_opened_secrets(
    tmp_path: Path, summary: StackArchiveSummary
) -> None:
    clock = Clock()
    store = StackImportStore(clock)
    for number in range(CAPACITY + 1):
        await hold(store, tmp_path, summary, f"{number:032x}", user=USER + number)
    assert len(spooled(tmp_path)) == CAPACITY

    held = await store.get(
        f"{CAPACITY:032x}", user_id=USER + CAPACITY, session_id=SESSION, environment_id=ENVIRONMENT
    )
    assert held is not None
    held.opened = {"variable:X": b"secret"}
    clock.now = LIFETIME_SECONDS + 1
    await store.sweep()

    assert spooled(tmp_path) == [] and held.opened is None
    await store.aclose()


async def test_a_hold_whose_import_has_begun_does_not_expire(
    tmp_path: Path, summary: StackArchiveSummary
) -> None:
    clock = Clock()
    store = StackImportStore(clock)
    await hold(store, tmp_path, summary, "a" * 32)
    held = await store.get("a" * 32, user_id=USER, session_id=SESSION, environment_id=ENVIRONMENT)
    assert held is not None
    held.importing = True
    clock.now = LIFETIME_SECONDS * 2

    await store.sweep()

    assert len(spooled(tmp_path)) == 1
    await store.aclose()


async def test_a_container_upload_and_a_stack_upload_by_one_user_are_held_together(
    tmp_path: Path, summary: StackArchiveSummary
) -> None:
    store = StackImportStore(Clock())
    await hold(store, tmp_path, summary, "a" * 32, kind="container")
    await hold(store, tmp_path, summary, "b" * 32, kind="compose")
    await hold(store, tmp_path, summary, "c" * 32, kind="compose")

    kept = [path.name for path in spooled(tmp_path)]

    assert kept == [f"{'a' * 32}.tar.gz", f"{'c' * 32}.tar.gz"]
    await store.aclose()


async def test_the_startup_sweep_empties_the_stack_spool_only(tmp_path: Path) -> None:
    await make_upload(tmp_path, "a" * 32)
    (tmp_path / "volume-import").mkdir()
    (tmp_path / "volume-import" / "keep.tar.gz").write_bytes(b"x")

    sweep_import_leftovers(tmp_path, SPOOL_DIRECTORY)

    assert not (tmp_path / SPOOL_DIRECTORY).exists()
    assert (tmp_path / "volume-import" / "keep.tar.gz").exists()
