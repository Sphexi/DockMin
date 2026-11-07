"""Migration 0015: how each backup record was produced, its archive digest and schema."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import migrate
from app.db.repositories import backup_snapshot
from tests.db.backup_support import OLD_VERSION, build, latest_version
from tests.db.helpers import CREATED_STAMP

DIGEST = "0123456789abcdef" * 4
INSERT_RECORD = (
    "INSERT INTO backup_record (created_at, key_version, key_fingerprint, outcome, origin, "
    "archive_sha256, schema_version) VALUES (?, 1, 'f', 'success', ?, ?, ?)"
)
FIRST_UPGRADED = 15


@pytest.fixture(scope="module")
def upgraded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    path = build(tmp_path_factory.mktemp("upgrade") / "dockmin.db", up_to=OLD_VERSION)
    connection = connect(path)
    applied = [migration.version for migration in migrate(connection)]
    assert applied == list(range(FIRST_UPGRADED, latest_version() + 1))
    yield connection
    connection.close()


def test_a_database_at_the_previous_schema_upgrades_and_keeps_its_records_as_syncs(
    upgraded: sqlite3.Connection,
) -> None:
    rows = upgraded.execute(
        "SELECT origin, archive_sha256, schema_version FROM backup_record ORDER BY id"
    ).fetchall()

    assert [tuple(row) for row in rows] == [("sync", None, None)] * 2
    assert backup_snapshot.schema_version(upgraded) == latest_version()
    assert backup_snapshot.integrity_findings(upgraded, limit=10) == []


def test_the_upgraded_schema_equals_a_freshly_migrated_one(
    upgraded: sqlite3.Connection, migrated_template: Path
) -> None:
    fresh = connect(migrated_template)
    try:
        expected = backup_snapshot.schema_objects(fresh)
    finally:
        fresh.close()

    assert backup_snapshot.schema_objects(upgraded) == expected


@pytest.mark.parametrize(
    ("origin", "digest", "schema"),
    [
        ("upload", None, None),
        ("sync", DIGEST.upper(), None),
        ("sync", DIGEST[:63], None),
        ("sync", DIGEST[:63] + "g", None),
        ("sync", None, 0),
        (None, None, None),
    ],
    ids=[
        "origin",
        "upper-case-digest",
        "short-digest",
        "non-hex-digest",
        "schema-zero",
        "no-origin",
    ],
)
def test_a_record_with_an_unknown_origin_a_malformed_digest_or_a_zero_schema_is_refused(
    upgraded: sqlite3.Connection, origin: str | None, digest: str | None, schema: int | None
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(INSERT_RECORD, (CREATED_STAMP, origin, digest, schema))


def test_a_download_record_with_a_digest_and_a_schema_is_accepted(
    upgraded: sqlite3.Connection,
) -> None:
    upgraded.execute("BEGIN")
    try:
        upgraded.execute(INSERT_RECORD, (CREATED_STAMP, "download", DIGEST, latest_version()))
        stored = upgraded.execute(
            "SELECT origin FROM backup_record WHERE archive_sha256 = ?", (DIGEST,)
        ).fetchone()
    finally:
        upgraded.execute("ROLLBACK")

    assert stored[0] == "download"
