"""Backup records: adding, the newest-500 limit and newest-first paging."""

from datetime import UTC, datetime, timedelta

import pytest

from app.db.engine import Session
from app.db.repositories.backup_record import KEPT_BACKUP_RECORDS, BackupRecordRepository

START = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
EXTRA = 5
PAGE_SIZE = 137
DIGEST = "ab" * 32
SCHEMA = 15


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def records(session: Session, clock: Clock) -> BackupRecordRepository:
    return BackupRecordRepository(session, clock)


async def add(records: BackupRecordRepository, size: int, **changes: object) -> int:
    values: dict[str, object] = {
        "origin": "sync",
        "target_id": None,
        "size_bytes": size,
        "key_version": 1,
        "key_fingerprint": "f",
        "destination_key": f"k{size}",
        "archive_sha256": DIGEST,
        "schema_version": SCHEMA,
        "outcome": "success",
        "error": None,
    }
    return await records.add_backup_record(**(values | changes))  # type: ignore[arg-type]


async def test_a_download_record_that_names_a_target_is_refused(
    records: BackupRecordRepository,
) -> None:
    with pytest.raises(ValueError, match="no object storage target"):
        await add(records, 1, origin="download", target_id=1)


async def test_only_the_newest_records_are_kept_and_each_page_continues_where_the_last_ended(
    records: BackupRecordRepository, session: Session, clock: Clock
) -> None:
    async with session.transaction():
        for index in range(KEPT_BACKUP_RECORDS + EXTRA):
            clock.now += timedelta(seconds=index % 2)
            await add(records, index)
    newest = await add(records, KEPT_BACKUP_RECORDS + EXTRA, outcome="failure", error="x")
    seen: list[int] = []
    cursor = None

    while True:
        page = await records.page_backup_records(limit=PAGE_SIZE, cursor=cursor)
        seen += [record.id for record in page.items]
        cursor = page.next_cursor
        if cursor is None:
            break

    oldest = await session.fetch_one("SELECT min(size_bytes) AS lowest FROM backup_record")
    assert oldest is not None and oldest["lowest"] == EXTRA + 1
    assert len(seen) == len(set(seen)) == KEPT_BACKUP_RECORDS
    assert seen[0] == newest
