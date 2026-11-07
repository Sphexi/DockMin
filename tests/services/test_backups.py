"""Producing an archive on demand: the stream, its records and rows, and every refusal."""

import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import app.services.backups as backups_module
from app.auth.errors import PermissionDenied
from app.auth.principal import Principal
from app.backup import ProductionFailed, read_manifest
from app.services.activity import ActivityRecorder
from app.services.backups import BackupFailed, BackupProduction, BackupService, ProductionBusy
from tests.backup.support import World, latest_version, make_world
from tests.scheduler.support import Clock

ADMIN = Principal(user_id=1, username="alice", role="administrator")
STANDARD = Principal(user_id=2, username="bob", role="standard")
FILE_NAME = "dockmin-backup-20260101T120000Z.tar.gz"
GZIP_MAGIC = b"\x1f\x8b"
LOW_SPACE = "There is not enough free space on the data volume to take a snapshot."


class Rig:
    def __init__(self, world: World) -> None:
        self.world = world
        self.clock = Clock()
        self.production = BackupProduction(world.data_dir)

    async def download(self, actor: Principal = ADMIN) -> backups_module.DownloadStream:
        async with self.world.database.session() as session:
            service = BackupService(
                session,
                self.world.vault,
                self.world.database,
                self.production,
                self.clock,
                ActivityRecorder(session, self.clock),
            )
            return await service.download(actor)

    def work_directories(self) -> list[str]:
        base = self.world.data_dir / "backup-work"
        return sorted(path.name for path in base.iterdir()) if base.exists() else []

    def rows(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        connection = self.world.database.connect()
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    world = await make_world(tmp_path)
    yield Rig(world)
    await world.database.aclose()


async def read_all(stream: backups_module.DownloadStream) -> bytes:
    return b"".join([chunk async for chunk in stream])


async def test_a_download_streams_the_whole_archive_and_the_work_directory_goes_with_it(
    rig: Rig, tmp_path: Path
) -> None:
    stream = await rig.download()
    data = await read_all(stream)

    saved = tmp_path / "saved.tar.gz"
    saved.write_bytes(data)
    manifest, _ = read_manifest(saved)
    assert hashlib.sha256(data).hexdigest() == stream.sha256 and len(data) == stream.size
    assert stream.file_name == FILE_NAME and data[:2] == GZIP_MAGIC
    assert manifest.schema_version == latest_version()
    assert manifest.key_fingerprint == rig.world.vault.key_fingerprint()
    assert rig.work_directories() == [] and not rig.production.held


async def test_a_download_writes_one_record_and_one_audited_row_with_the_facts(rig: Rig) -> None:
    stream = await rig.download()
    await read_all(stream)

    record = rig.rows(
        "SELECT origin, object_storage_target_id, size_bytes, archive_sha256, outcome "
        "FROM backup_record"
    )
    [row] = rig.rows(
        "SELECT result, actor_type, actor_user_id, detail FROM activity_event "
        "WHERE action = 'backup_downloaded'"
    )
    detail = json.loads(row["detail"])
    assert [tuple(r) for r in record] == [("download", None, stream.size, stream.sha256, "success")]
    assert (row["result"], row["actor_type"], row["actor_user_id"]) == ("success", "user", 1)
    assert {
        "archive_sha256",
        "size_bytes",
        "schema_version",
        "key_version",
        "key_fingerprint",
        "backup_record_id",
    } <= set(detail)


async def test_a_download_while_the_lock_is_held_is_refused_and_writes_nothing(rig: Rig) -> None:
    assert rig.production.try_hold()
    before = len(rig.rows("SELECT 1 FROM activity_event"))

    with pytest.raises(ProductionBusy):
        await rig.download()

    assert len(rig.rows("SELECT 1 FROM activity_event")) == before
    assert rig.production.held


async def test_a_standard_user_is_denied_and_the_lock_is_untouched(rig: Rig) -> None:
    with pytest.raises(PermissionDenied):
        await rig.download(STANDARD)

    assert not rig.production.held


async def test_a_failed_production_raises_with_its_reason_and_records_the_failure(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(*arguments: object, **options: object) -> object:
        raise ProductionFailed("insufficient_space", LOW_SPACE)

    monkeypatch.setattr(backups_module, "produce_archive", failing)

    with pytest.raises(BackupFailed) as raised:
        await rig.download()

    [event] = rig.rows(
        "SELECT result, detail FROM activity_event WHERE action = 'backup_downloaded'"
    )
    [record] = rig.rows("SELECT origin, outcome, error, size_bytes FROM backup_record")
    detail = json.loads(event["detail"])
    assert raised.value.reason == "insufficient_space"
    assert (event["result"], detail["reason"]) == ("failure", "insufficient_space")
    assert "free space" in detail["message"]
    assert (record["origin"], record["outcome"], record["size_bytes"]) == (
        "download",
        "failure",
        None,
    )
    assert "free space" in record["error"]
    assert rig.work_directories() == [] and not rig.production.held


async def test_closing_a_stream_early_removes_its_work_directory(rig: Rig) -> None:
    stream = await rig.download()
    first = await stream.__anext__()
    during = rig.work_directories()

    await stream.aclose()

    assert len(first) > 0 and during != []
    assert rig.work_directories() == []


async def test_the_records_are_listed_to_an_administrator_only(rig: Rig) -> None:
    await read_all(await rig.download())
    async with rig.world.database.session() as session:
        service = BackupService(
            session,
            rig.world.vault,
            rig.world.database,
            rig.production,
            rig.clock,
            ActivityRecorder(session, rig.clock),
        )
        page = await service.records(ADMIN, limit=10)
        with pytest.raises(PermissionDenied):
            await service.records(STANDARD, limit=10)

    assert [(record.origin, record.outcome) for record in page.items] == [("download", "success")]
