"""Migration 0016: the last-run columns of a scheduled job."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import migrate
from app.db.repositories import backup_snapshot
from tests.db.backup_support import build, latest_version
from tests.db.helpers import CREATED_STAMP

PREVIOUS_VERSION = 15
MIGRATION_VERSION = 16
REFRESHER = "environment-snapshot-refresher"
UPDATER = "container-updater"
INSERT_JOB = (
    "INSERT INTO scheduled_job (subsystem, rule_id, schedule_kind, schedule_expression, "
    "created_at, updated_at) VALUES (?, NULL, 'interval', '60', ?, ?)"
)
INSERT_JOB_WITH_LAST_RUN = (
    "INSERT INTO scheduled_job (subsystem, rule_id, schedule_kind, schedule_expression, "
    "created_at, updated_at, last_run_at, last_run_outcome) "
    "VALUES (?, NULL, 'interval', '60', ?, ?, ?, ?)"
)


@pytest.fixture(scope="module")
def upgraded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    path = build(tmp_path_factory.mktemp("upgrade") / "dockmin.db", up_to=PREVIOUS_VERSION)
    connection = connect(path)
    connection.execute(INSERT_JOB, (REFRESHER, CREATED_STAMP, CREATED_STAMP))
    applied = [migration.version for migration in migrate(connection)]
    assert applied == list(range(MIGRATION_VERSION, latest_version() + 1))
    yield connection
    connection.close()


def test_a_job_that_existed_before_the_migration_keeps_its_row_with_no_last_run(
    upgraded: sqlite3.Connection,
) -> None:
    row = upgraded.execute(
        "SELECT subsystem, schedule_expression, last_run_at, last_run_outcome "
        "FROM scheduled_job WHERE subsystem = ?",
        (REFRESHER,),
    ).fetchone()

    assert tuple(row) == (REFRESHER, "60", None, None)
    assert backup_snapshot.integrity_findings(upgraded, limit=10) == []


def test_a_job_can_store_the_start_and_success_of_its_latest_run(
    upgraded: sqlite3.Connection,
) -> None:
    upgraded.execute(
        INSERT_JOB_WITH_LAST_RUN, (UPDATER, CREATED_STAMP, CREATED_STAMP, CREATED_STAMP, "success")
    )

    row = upgraded.execute(
        "SELECT last_run_at, last_run_outcome FROM scheduled_job WHERE subsystem = ?", (UPDATER,)
    ).fetchone()

    assert tuple(row) == (CREATED_STAMP, "success")


@pytest.mark.parametrize("outcome", ["failure", "interrupted", "SUCCESS"])
def test_a_last_run_outcome_other_than_success_is_refused(
    upgraded: sqlite3.Connection, outcome: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(
            INSERT_JOB_WITH_LAST_RUN,
            ("backup-syncer", CREATED_STAMP, CREATED_STAMP, CREATED_STAMP, outcome),
        )


def test_the_upgraded_schema_equals_a_freshly_migrated_one(
    upgraded: sqlite3.Connection, migrated_template: Path
) -> None:
    fresh = connect(migrated_template)
    try:
        expected = backup_snapshot.schema_objects(fresh)
    finally:
        fresh.close()

    assert backup_snapshot.schema_objects(upgraded) == expected
