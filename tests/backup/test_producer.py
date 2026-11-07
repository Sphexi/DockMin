"""Producing an archive: members, normalised secrets, stripped sessions and every refusal."""

import shutil
import sqlite3
import stat
import tarfile
from pathlib import Path

import pytest

from app.backup import archive, paths, producer
from app.backup.manifest import MAX_COMPOSE_BYTES, MAX_STACK_MEMBERS, ProductionFailed
from app.crypto.registry import SECRET_FIELDS
from tests.backup.support import (
    ARCHIVE_FILE_NAME,
    KEY_VERSION_AFTER_ROTATION,
    NEW_STACK,
    NEW_STACK_FILE,
    NOW,
    OLD_STACK,
    OPERATOR,
    UNDECRYPTABLE_OLD,
    Stored,
    World,
    clock,
    latest_version,
    make_world,
    read_members,
)
from tests.crypto.helpers import STAMP

ADMINISTRATOR_LIMIT = 100
OWNER_ONLY = 0o600
SNAPSHOT_SIZE = 4096


@pytest.fixture(scope="module")
def snapshot_copy(stored: Stored, tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("snapshot") / "snapshot.sqlite3"
    path.write_bytes(stored.members["database.sqlite3"])
    return path


def scalar(path: Path, sql: str) -> list[object]:
    connection = sqlite3.connect(path)
    try:
        return [row[0] for row in connection.execute(sql).fetchall()]
    finally:
        connection.close()


def test_the_archive_holds_the_manifest_the_database_and_each_stack_file_in_that_order(
    stored: Stored,
) -> None:
    assert list(stored.members) == [
        "manifest.json",
        "database.sqlite3",
        f"stacks/{OLD_STACK}/compose.yaml",
        f"stacks/{NEW_STACK}/compose.yaml",
    ]
    assert stored.members[f"stacks/{NEW_STACK}/compose.yaml"] == NEW_STACK_FILE


def test_every_member_header_is_fixed_so_nothing_of_the_host_is_recorded(stored: Stored) -> None:
    infos = [info for info, _ in read_members(stored.path).values()]

    assert {
        (i.mode, i.uid, i.gid, i.uname, i.gname, i.mtime, i.type, tuple(i.pax_headers))
        for i in infos
    } == {(OWNER_ONLY, 0, 0, "", "", int(NOW.timestamp()), tarfile.REGTYPE, ())}


def test_the_archive_is_the_only_file_left_in_the_work_directory_and_is_owner_only(
    stored: Stored,
) -> None:
    assert sorted(path.name for path in stored.path.parent.iterdir()) == ["archive.tar.gz"]
    assert stat.S_IMODE(stored.path.stat().st_mode) == OWNER_ONLY


def test_the_manifest_states_the_key_the_schema_the_operators_and_the_inventory(
    stored: Stored,
) -> None:
    manifest = stored.manifest

    assert manifest.key_version == KEY_VERSION_AFTER_ROTATION
    assert manifest.administrators == (OPERATOR,)
    assert manifest.schema_version == latest_version()
    assert [member.name for member in manifest.members] == list(stored.members)[1:]


async def test_the_produced_file_is_named_for_the_moment_it_was_made(world: World) -> None:
    workdir = paths.new_work_directory(world.data_dir)

    produced = await producer.produce_archive(world.database, world.vault, workdir, clock=clock)

    assert produced.file_name == ARCHIVE_FILE_NAME
    assert produced.size == produced.path.stat().st_size


def test_every_secret_in_the_snapshot_is_sealed_at_the_current_key_version(
    snapshot_copy: Path,
) -> None:
    versions = {
        str(value).split(":")[0]
        for field in SECRET_FIELDS
        for value in scalar(
            snapshot_copy,
            f"SELECT {field.column} FROM {field.table} WHERE {field.column} IS NOT NULL",
        )
    }

    assert versions == {f"v{KEY_VERSION_AFTER_ROTATION}"}


def test_sessions_and_login_throttles_are_absent_from_the_snapshot_and_present_in_the_live_database(
    snapshot_copy: Path, world: World
) -> None:
    live = world.database.connect()
    try:
        live_sessions = live.execute("SELECT count(*) FROM session").fetchone()[0]
        old_values = {
            row[0]
            for row in live.execute(
                "SELECT substr(secret_ciphertext, 1, 2) FROM registry_credential"
            )
        }
    finally:
        live.close()

    assert scalar(snapshot_copy, "SELECT count(*) FROM session") == [0]
    assert scalar(snapshot_copy, "SELECT count(*) FROM login_throttle") == [0]
    assert live_sessions == 1
    assert "v1" in old_values


def test_the_stack_files_in_the_archive_are_the_files_in_the_stack_rows(
    snapshot_copy: Path, stored: Stored
) -> None:
    rows = scalar(snapshot_copy, "SELECT project_directory FROM stack ORDER BY project_directory")

    assert [f"stacks/{directory}/compose.yaml" for directory in rows] == list(stored.members)[2:]


async def test_too_little_free_space_refuses_before_a_snapshot_is_taken(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = paths.new_work_directory(world.data_dir)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(100, 99, 1))

    with pytest.raises(ProductionFailed) as raised:
        await producer.produce_archive(world.database, world.vault, workdir, clock=clock)

    assert raised.value.reason == "insufficient_space"
    assert list(workdir.iterdir()) == []


async def test_a_secret_the_loaded_key_cannot_read_refuses_the_backup_and_leaves_no_file(
    tmp_path: Path,
) -> None:
    world = await make_world(tmp_path)
    world.run(
        "UPDATE registry_credential SET secret_ciphertext = ? WHERE id = 1", (UNDECRYPTABLE_OLD,)
    )
    workdir = paths.new_work_directory(world.data_dir)

    try:
        with pytest.raises(ProductionFailed) as raised:
            await producer.produce_archive(world.database, world.vault, workdir, clock=clock)
    finally:
        await world.database.aclose()

    assert raised.value.reason == "secret_unreadable"
    assert list(workdir.iterdir()) == []


async def test_more_administrators_than_the_manifest_lists_are_cut_and_the_cut_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    world = await make_world(tmp_path)
    for index in range(ADMINISTRATOR_LIMIT + 1):
        world.run(
            "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
            "VALUES (?, 'h', 'administrator', 1, ?)",
            (f"admin{index:03d}", STAMP),
        )
    workdir = paths.new_work_directory(world.data_dir)

    try:
        produced = await producer.produce_archive(world.database, world.vault, workdir, clock=clock)
    finally:
        await world.database.aclose()

    assert len(produced.manifest.administrators) == ADMINISTRATOR_LIMIT
    assert "backup_administrators_truncated" in caplog.text


@pytest.mark.parametrize(
    "stacks",
    [
        [(f"{index:032x}", b"x") for index in range(MAX_STACK_MEMBERS + 1)],
        [(OLD_STACK, b"")],
        [(OLD_STACK, b"x" * (MAX_COMPOSE_BYTES + 1))],
    ],
    ids=["too-many-stacks", "empty-stack-file", "oversize-stack-file"],
)
def test_an_inventory_with_too_many_or_impossibly_sized_stack_files_is_refused(
    tmp_path: Path, stacks: list[tuple[str, bytes]]
) -> None:
    snapshot = tmp_path / "snapshot.sqlite3"
    snapshot.write_bytes(b"x" * SNAPSHOT_SIZE)

    with pytest.raises(ProductionFailed) as raised:
        archive.inventory(snapshot, stacks)

    assert raised.value.reason == "production_failed"
