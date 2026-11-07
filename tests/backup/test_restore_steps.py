"""The restore steps that act on a staged database: the key, the checks, the re-seal, the record."""

import asyncio
import hashlib
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from app.backup import archive, paths, producer, restore_steps
from app.backup.manifest import (
    ArchiveRefused,
    Manifest,
    decode_manifest,
    encode_manifest,
)
from app.crypto.envelope import SecretLocation
from app.crypto.keys import MasterKeys
from app.crypto.registry import SECRET_FIELDS
from app.crypto.startup import KeyMismatchError, establish_vault
from app.db.engine import Database
from app.db.repositories import crypto_state, encrypted, rekey
from app.db.repositories.crypto_state import CryptoStateRepository
from app.logging import Scrubber
from tests.backup import support
from tests.backup.support import (
    NEW_STACK,
    NEW_STACK_FILE,
    OLD_STACK,
    OPERATOR,
    PLACEHOLDER_MAC,
    STANDARD_USER,
    UNDECRYPTABLE,
    Stored,
    clock,
    copy_tree,
    edit_staged_database,
    key_text,
    latest_version,
    make_world,
    prepare,
    stage,
)
from tests.crypto.helpers import FIRST_KEY, SECOND_KEY, THIRD_KEY

STAGED = "dockmin.db"
NEWER_SCHEMA = 99
OLD_SCHEMA = 14
TABLES_AFTER_OLD_SCHEMA = frozenset({"notification_channel", "stack_file"})
GAP = restore_steps.ACTIVITY_ID_GAP
LIVE_FLOOR = 7_000
RESTORE_ID = "e" * 32
RESEALED_VERSION = 3


@pytest.fixture(scope="module")
def staged(stored: Stored, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return stage(stored, tmp_path_factory.mktemp("staged") / "staging")


@pytest.fixture
def staging(staged: Path, tmp_path: Path) -> Path:
    return copy_tree(staged, tmp_path / "staging")


@pytest.fixture(scope="module")
def archive_key() -> bytearray:
    return bytearray(SECOND_KEY)


@pytest.mark.parametrize(
    ("typed", "reason"),
    [
        ("  ", "key_missing"),
        ("!!!", "key_invalid"),
        (key_text(b"short"), "key_invalid"),
        (key_text(THIRD_KEY), "fingerprint_mismatch"),
        (key_text(FIRST_KEY), "fingerprint_mismatch"),
    ],
    ids=["missing", "not-base64", "wrong-length", "other-key", "previous-key"],
)
def test_a_key_that_is_missing_malformed_or_not_the_archives_is_refused_at_the_key_step(
    stored: Stored, typed: str, reason: str
) -> None:
    with pytest.raises(ArchiveRefused) as raised:
        restore_steps.check_key(stored.manifest, stored.raw_manifest, typed)

    assert raised.value.reason == reason
    assert typed.strip() not in raised.value.message or not typed.strip()


def test_a_wrong_key_names_the_archives_key_version_and_fingerprint_and_nothing_typed(
    stored: Stored,
) -> None:
    with pytest.raises(ArchiveRefused) as raised:
        restore_steps.check_key(stored.manifest, stored.raw_manifest, key_text(THIRD_KEY))

    assert f"key version {stored.manifest.key_version}" in raised.value.message
    assert stored.manifest.key_fingerprint in raised.value.message
    assert key_text(THIRD_KEY) not in raised.value.message


def test_the_right_key_with_trailing_whitespace_is_returned_as_a_mutable_buffer(
    stored: Stored,
) -> None:
    key = restore_steps.check_key(stored.manifest, stored.raw_manifest, key_text(SECOND_KEY) + "\n")

    assert isinstance(key, bytearray) and bytes(key) == SECOND_KEY


def test_the_right_fingerprint_on_a_manifest_not_signed_by_that_key_is_refused(
    stored: Stored,
) -> None:
    forged = Manifest(
        1,
        stored.manifest.schema_version,
        stored.manifest.created_at,
        stored.manifest.product_version,
        stored.manifest.instance_id,
        stored.manifest.key_version,
        stored.manifest.key_fingerprint,
        ("mallory",),
        stored.manifest.members,
        "",
    )
    raw = encode_manifest(forged, mac=lambda _: PLACEHOLDER_MAC)

    with pytest.raises(ArchiveRefused) as raised:
        restore_steps.check_key(decode_manifest(raw), raw, key_text(SECOND_KEY))

    assert raised.value.reason == "manifest_tampered"


async def test_an_archive_of_the_current_schema_prepares_and_the_operator_is_matched_ignoring_case(
    staging: Path,
) -> None:
    checked = await prepare(staging, "ALICE")

    assert checked.migrated_from == checked.schema_version == latest_version()
    assert checked.operator_user_id == 1


@pytest.mark.parametrize("operator", [STANDARD_USER, "nobody"])
async def test_an_operator_who_is_not_an_enabled_administrator_in_the_archive_is_refused(
    staging: Path, operator: str
) -> None:
    with pytest.raises(ArchiveRefused) as raised:
        await prepare(staging, operator)

    assert raised.value.reason == "operator_not_in_archive"


async def test_an_archive_from_a_newer_schema_is_refused_naming_both_versions(
    staging: Path,
) -> None:
    edit_staged_database(
        staging, "INSERT INTO schema_migration VALUES (?, 'future', 'x', 'y')", (NEWER_SCHEMA,)
    )

    with pytest.raises(ArchiveRefused) as raised:
        await prepare(staging)

    assert raised.value.reason == "schema_newer"
    assert (
        str(NEWER_SCHEMA) in raised.value.message and str(latest_version()) in raised.value.message
    )


SCHEMA_TAMPERING = {
    "extra-trigger": "CREATE TRIGGER evil AFTER INSERT ON activity_event BEGIN SELECT 1; END",
    "extra-index": "CREATE INDEX evil_idx ON setting (value)",
    "extra-view": "CREATE VIEW evil_view AS SELECT 1",
}


@pytest.mark.parametrize("statement", SCHEMA_TAMPERING.values(), ids=SCHEMA_TAMPERING.keys())
async def test_an_archive_with_an_object_the_schema_does_not_define_is_refused(
    staging: Path, statement: str
) -> None:
    edit_staged_database(staging, statement)

    with pytest.raises(ArchiveRefused) as raised:
        await prepare(staging)

    assert raised.value.reason == "schema_mismatch"


ANNOTATE_SETTING_TABLE = (
    "UPDATE sqlite_master SET sql = replace(sql, 'CREATE TABLE setting (', "
    "'CREATE TABLE setting ( -- note' || char(10) || '      /* kept */   ') "
    "WHERE name = 'setting'"
)
WIDEN_SETTING_TYPES = (
    "UPDATE sqlite_master SET sql = replace(sql, \"'json')\", \"'json', 'extra')\") "
    "WHERE name = 'setting'"
)
ADD_SETTING_COLUMN = (
    "UPDATE sqlite_master SET sql = replace(sql, 'updated_at  TEXT NOT NULL', "
    "'updated_at  TEXT NOT NULL, extra TEXT') WHERE name = 'setting'"
)


def rewrite_schema(staging: Path, statement: str) -> None:
    connection = sqlite3.connect(staging / STAGED)
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(statement)
    connection.commit()
    connection.close()


async def test_an_archive_whose_schema_text_carries_comments_and_extra_whitespace_passes(
    staging: Path,
) -> None:
    rewrite_schema(staging, ANNOTATE_SETTING_TABLE)

    checked = await prepare(staging)

    assert checked.schema_version == latest_version()


HIDE_AFTER_NUL = {
    "line-comment": "UPDATE sqlite_master SET sql = sql || ' -- a' || char(0) || 'b'",
    "block-comment": "UPDATE sqlite_master SET sql = sql || ' /* a' || char(0) || 'b */'",
    "blob": "UPDATE sqlite_master SET sql = CAST(sql AS BLOB)",
}


@pytest.mark.parametrize("statement", HIDE_AFTER_NUL.values(), ids=HIDE_AFTER_NUL.keys())
async def test_an_archive_whose_schema_text_hides_bytes_after_a_nul_or_is_a_blob_is_refused(
    staging: Path, statement: str
) -> None:
    rewrite_schema(staging, f"{statement} WHERE name = 'setting'")

    with pytest.raises(ArchiveRefused) as raised:
        await prepare(staging)

    assert raised.value.reason == "schema_mismatch"


@pytest.mark.parametrize(
    "statement",
    [WIDEN_SETTING_TYPES, ADD_SETTING_COLUMN],
    ids=["constraint", "column"],
)
async def test_an_archive_whose_table_differs_in_a_column_or_constraint_is_refused(
    staging: Path, statement: str
) -> None:
    rewrite_schema(staging, ANNOTATE_SETTING_TABLE)
    rewrite_schema(staging, statement)

    with pytest.raises(ArchiveRefused) as raised:
        await prepare(staging)

    assert raised.value.reason == "schema_mismatch"


RESERVED_NAMES = {
    "trigger": (
        "CREATE TRIGGER t_x AFTER INSERT ON setting BEGIN SELECT 1; END",
        "UPDATE sqlite_master SET name = 'sqlite_x', sql = replace(sql, 't_x', 'sqlite_x') "
        "WHERE name = 't_x'",
    ),
    "view": (
        "CREATE VIEW t_x AS SELECT 1",
        "UPDATE sqlite_master SET name = 'sqlite_x', tbl_name = 'sqlite_x', "
        "sql = replace(sql, 't_x', 'sqlite_x') WHERE name = 't_x'",
    ),
}


@pytest.mark.parametrize(("create", "rename"), RESERVED_NAMES.values(), ids=RESERVED_NAMES.keys())
async def test_a_trigger_or_view_renamed_to_a_reserved_name_does_not_pass_the_schema_check(
    staging: Path, create: str, rename: str
) -> None:
    connection = sqlite3.connect(staging / STAGED)
    connection.execute(create)
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(rename)
    connection.commit()
    connection.close()

    with pytest.raises(ArchiveRefused) as raised:
        await prepare(staging)

    assert raised.value.reason == "schema_mismatch"


async def test_a_row_naming_a_missing_parent_makes_the_database_invalid(staging: Path) -> None:
    connection = sqlite3.connect(staging / STAGED)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "INSERT INTO session (user_id, token_hash, issued_at, expires_at) "
        "VALUES (999, 'z', 'a', 'b')"
    )
    connection.commit()
    connection.close()

    with pytest.raises(ArchiveRefused) as raised:
        await prepare(staging)

    assert raised.value.reason == "database_invalid"


async def test_integrity_is_checked_before_the_schema_version_so_a_damaged_file_is_never_migrated(
    staging: Path,
) -> None:
    connection = sqlite3.connect(staging / STAGED)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "INSERT INTO session (user_id, token_hash, issued_at, expires_at) "
        "VALUES (999, 'z', 'a', 'b')"
    )
    connection.execute(
        "INSERT INTO schema_migration VALUES (?, 'future', 'x', 'y')", (NEWER_SCHEMA,)
    )
    connection.commit()
    connection.close()

    with pytest.raises(ArchiveRefused) as raised:
        await prepare(staging)

    assert raised.value.reason == "database_invalid"


async def test_a_file_that_is_not_a_database_is_refused_as_an_invalid_database(
    staging: Path,
) -> None:
    (staging / STAGED).write_bytes(b"this is not a database" * 100)

    with pytest.raises(ArchiveRefused) as raised:
        await prepare(staging)

    assert raised.value.reason == "database_invalid"


def _write_other_content(stacks: Path) -> None:
    (stacks / OLD_STACK / "compose.yaml").write_bytes(b"different")


def _add_file_without_a_row(stacks: Path) -> None:
    (stacks / ("a" * 32)).mkdir()
    (stacks / ("a" * 32) / "compose.yaml").write_bytes(b"x")


def _add_a_second_file(stacks: Path) -> None:
    (stacks / OLD_STACK / "notes.txt").write_bytes(b"x")


def _replace_with_a_link(stacks: Path) -> None:
    (stacks / ("a" * 32)).symlink_to(stacks / OLD_STACK)


STACK_TAMPERING = {
    "differs": _write_other_content,
    "has-no-row": _add_file_without_a_row,
    "extra-entry": _add_a_second_file,
    "link": _replace_with_a_link,
}


@pytest.mark.parametrize("tamper", STACK_TAMPERING.values(), ids=STACK_TAMPERING.keys())
async def test_stack_files_that_do_not_match_the_stack_rows_are_refused(
    staging: Path, tamper: Callable[[Path], None]
) -> None:
    tamper(staging / "stacks")

    with pytest.raises(ArchiveRefused) as raised:
        await prepare(staging)

    assert raised.value.reason == "stack_files_inconsistent"


async def test_a_stack_row_with_no_file_gets_its_file_written_from_the_row(staging: Path) -> None:
    shutil.rmtree(staging / "stacks" / NEW_STACK)

    await prepare(staging)

    assert (staging / "stacks" / NEW_STACK / "compose.yaml").read_bytes() == NEW_STACK_FILE


async def test_an_archive_made_before_the_latest_migration_is_migrated_on_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets_then = tuple(f for f in SECRET_FIELDS if f.table not in TABLES_AFTER_OLD_SCHEMA)
    for module in (crypto_state, encrypted, rekey, support):
        monkeypatch.setattr(module, "SECRET_FIELDS", secrets_then)
    old = await make_world(tmp_path / "old", up_to=OLD_SCHEMA)
    workdir = paths.new_work_directory(old.data_dir)
    try:
        produced = await producer.produce_archive(old.database, old.vault, workdir, clock=clock)
    finally:
        await old.database.aclose()
    archive.verify_members(produced.path, produced.manifest)
    staging = tmp_path / "stage-old"
    archive.stage_members(produced.path, produced.manifest, staging)

    checked = await prepare(staging)

    assert produced.manifest.schema_version == OLD_SCHEMA
    assert (checked.migrated_from, checked.schema_version) == (OLD_SCHEMA, latest_version())


class Resealed:
    """A staged copy after the key step and the final record, and the facts the steps returned."""

    def __init__(self, staging: Path, reseal: restore_steps.Reseal | None) -> None:
        self.staging = staging
        self.reseal = reseal


@pytest.fixture(scope="module")
def resealed(
    stored: Stored, staged: Path, archive_key: bytearray, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Resealed]:
    staging = copy_tree(staged, tmp_path_factory.mktemp("resealed") / "staging")
    result = asyncio.run(
        restore_steps.decrypt_and_reseal(staging, stored.manifest, archive_key, THIRD_KEY)
    )
    edit_staged_database(
        staging,
        "INSERT INTO session (user_id, token_hash, issued_at, expires_at) "
        "VALUES (1, 'zz', 'a', 'b')",
    )
    record = restore_steps.RestoreRecord(
        1,
        OPERATOR,
        stored.sha256,
        stored.manifest,
        latest_version(),
        latest_version(),
        "/data/restore/previous",
        RESTORE_ID,
    )
    asyncio.run(
        restore_steps.finalise_staged(
            staging, record=record, reseal=result, live_activity_floor=LIVE_FLOOR, clock=clock
        )
    )
    yield Resealed(staging, result)


def query(path: Path, sql: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


async def test_the_archives_own_key_as_the_running_key_rewrites_nothing(
    stored: Stored, staging: Path, archive_key: bytearray
) -> None:
    before = hashlib.sha256((staging / STAGED).read_bytes()).hexdigest()

    result = await restore_steps.decrypt_and_reseal(
        staging, stored.manifest, archive_key, SECOND_KEY
    )

    assert result is None
    assert hashlib.sha256((staging / STAGED).read_bytes()).hexdigest() == before


def test_another_running_key_reseals_every_secret_one_version_up(resealed: Resealed) -> None:
    assert resealed.reseal is not None
    assert (resealed.reseal.key_version_before, resealed.reseal.key_version_after) == (
        2,
        RESEALED_VERSION,
    )
    assert resealed.reseal.values >= 4


async def test_the_resealed_database_starts_under_the_running_key_and_no_longer_under_the_archives(
    resealed: Resealed,
) -> None:
    database = Database(resealed.staging / STAGED)
    try:
        vault = await establish_vault(database, MasterKeys(THIRD_KEY), Scrubber())
        async with database.session() as session:
            counts = await CryptoStateRepository(session).count_by_version()
            row = await session.fetch_one(
                "SELECT secret_ciphertext FROM registry_credential WHERE id = 2"
            )
        assert row is not None
        opened = vault.open(
            row["secret_ciphertext"], SecretLocation("registry_credential", "secret_ciphertext", 2)
        )
        with pytest.raises(KeyMismatchError):
            await establish_vault(database, MasterKeys(SECOND_KEY), Scrubber())
    finally:
        await database.aclose()

    assert (vault.current_version, set(counts), opened) == (
        RESEALED_VERSION,
        {RESEALED_VERSION},
        "plain-new",
    )


CORRUPTIONS = {
    "secret": (
        "UPDATE environment_tls_material "
        f"SET client_key_ciphertext = '{UNDECRYPTABLE}' WHERE id = 1",
        "environment_tls_material.client_key_ciphertext row 1",
    ),
    "canary": (
        f"UPDATE setting SET value = '{UNDECRYPTABLE}' WHERE key = 'crypto.canary'",
        "key does not open",
    ),
    "version": (
        "UPDATE registry_credential SET secret_ciphertext = "
        "replace(secret_ciphertext, 'v2:', 'v1:') WHERE id = 2",
        "other than its own",
    ),
}


@pytest.mark.parametrize("label", CORRUPTIONS)
async def test_a_staged_database_the_key_cannot_open_fails_the_decrypt_test_and_says_where(
    stored: Stored, staging: Path, archive_key: bytearray, label: str
) -> None:
    statement, expected = CORRUPTIONS[label]
    edit_staged_database(staging, statement)

    with pytest.raises(ArchiveRefused) as raised:
        await restore_steps.decrypt_and_reseal(staging, stored.manifest, archive_key, SECOND_KEY)

    assert raised.value.reason == "decrypt_failed"
    assert expected in raised.value.message
    assert UNDECRYPTABLE not in raised.value.message


def test_the_restore_rows_are_written_as_the_operator_in_the_order_they_happened(
    resealed: Resealed,
) -> None:
    rows = query(
        resealed.staging / STAGED,
        "SELECT id, action, actor_type, actor_user_id, target_type, result FROM activity_event "
        "ORDER BY id DESC LIMIT 2",
    )

    assert [
        (r["action"], r["actor_type"], r["actor_user_id"], r["target_type"]) for r in reversed(rows)
    ] == [
        ("instance_restored", "user", 1, "instance"),
        ("encryption_key_changed", "user", 1, "instance"),
    ]
    assert min(r["id"] for r in rows) > LIVE_FLOOR + GAP


def test_the_restore_row_records_the_archive_the_previous_path_and_the_operator_unredacted(
    resealed: Resealed,
) -> None:
    detail = query(
        resealed.staging / STAGED,
        "SELECT detail FROM activity_event WHERE action = 'instance_restored'",
    )[0]["detail"]

    assert all(
        name in detail
        for name in (
            "archive_sha256",
            "key_fingerprint",
            "key_changed",
            "previous_path",
            "operator_username",
        )
    )
    assert "[REDACTED]" not in detail


def test_sessions_are_cleared_and_the_sequence_leaves_a_gap_after_the_finish(
    resealed: Resealed,
) -> None:
    sessions = query(resealed.staging / STAGED, "SELECT count(*) AS total FROM session")[0]["total"]
    sequence = query(
        resealed.staging / STAGED, "SELECT seq FROM sqlite_sequence WHERE name = 'activity_event'"
    )[0]["seq"]

    assert sessions == 0
    assert sequence >= LIVE_FLOOR + GAP


def test_the_finished_staged_database_leaves_no_journal_files_and_passes_its_integrity_check(
    resealed: Resealed,
) -> None:
    sidecars = [p.name for p in resealed.staging.iterdir() if p.name.startswith("dockmin.db-")]

    assert sidecars == []
    assert query(resealed.staging / STAGED, "PRAGMA integrity_check")[0][0] == "ok"


async def test_without_a_key_change_only_the_restore_row_is_added(
    stored: Stored, staging: Path
) -> None:
    record = restore_steps.RestoreRecord(
        1,
        OPERATOR,
        stored.sha256,
        stored.manifest,
        latest_version(),
        latest_version(),
        "/data/previous",
        RESTORE_ID,
    )

    await restore_steps.finalise_staged(
        staging, record=record, reseal=None, live_activity_floor=0, clock=clock
    )

    actions = [
        r["action"]
        for r in query(
            staging / STAGED, "SELECT action FROM activity_event ORDER BY id DESC LIMIT 2"
        )
    ]
    assert actions == ["instance_restored", "seed"]
