"""Migration 0019: the names admitted by the rebuilt tables, with no row lost."""

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import MigrationError, migrate
from app.db.repositories import backup_snapshot
from tests.db.backup_support import migrations_up_to
from tests.db.helpers import CREATED_STAMP
from tests.db.migration_support import (
    BACKUP_RULE,
    DANGLING_RULE,
    ENVIRONMENT_ID,
    EXPORT_RULE,
    HIGHEST_EVENT,
    INSERT_INFRASTRUCTURE_JOB,
    INSERT_RULE,
    INSERT_SUBSYSTEM_EVENT,
    KEPT_EVENTS,
    LOG_EXPORTER,
    MIGRATION_VERSION,
    PREVIOUS_VERSION,
    PRUNED_EVENT,
    SENDER,
    Upgrade,
    ledger_version,
    migrate_to_this,
    seeded_previous,
    snapshot,
    upgrade_seeded,
)


@pytest.fixture(scope="module")
def upgrade(tmp_path_factory: pytest.TempPathFactory) -> Upgrade:
    return upgrade_seeded(tmp_path_factory.mktemp("upgrade"), PREVIOUS_VERSION, MIGRATION_VERSION)


@pytest.fixture
def upgraded(upgrade: Upgrade, tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A private copy of the upgraded database, safe to change."""
    copy = tmp_path / "copy.db"
    shutil.copyfile(upgrade.path, copy)
    connection = connect(copy)
    yield connection
    connection.close()


def test_every_row_of_every_table_survives_the_rebuild_with_its_id(upgrade: Upgrade) -> None:
    populated = {name for name, rows in upgrade.before.rows.items() if rows}

    assert upgrade.after.rows == upgrade.before.rows
    assert {
        "automation_rule",
        "scheduled_job",
        "job_run",
        "activity_event",
        "activity_export_cursor",
        "restart_hold_sighting",
    } <= populated


def test_every_index_and_trigger_is_recreated_unchanged(upgrade: Upgrade) -> None:
    assert upgrade.after.indexes == upgrade.before.indexes
    assert upgrade.after.triggers == upgrade.before.triggers


def test_the_activity_sequence_keeps_a_pruned_id_from_being_reissued(upgrade: Upgrade) -> None:
    assert upgrade.after.sequence == upgrade.before.sequence
    assert upgrade.after.sequence["activity_event"] == PRUNED_EVENT
    assert [row[0] for row in upgrade.after.rows["activity_event"]] == KEPT_EVENTS


def test_the_rebuild_leaves_a_consistent_database_with_foreign_keys_back_on(
    upgrade: Upgrade, upgraded: sqlite3.Connection
) -> None:
    assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
    assert upgraded.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert upgrade.foreign_keys_after == 1
    assert upgrade.leftovers == []


def test_an_event_from_each_new_name_is_inserted_above_every_id_already_used(
    upgraded: sqlite3.Connection,
) -> None:
    for name in (LOG_EXPORTER, SENDER):
        upgraded.execute(INSERT_SUBSYSTEM_EVENT, (name, CREATED_STAMP))

    ids = [row[0] for row in upgraded.execute("SELECT id FROM activity_event ORDER BY id")]

    assert ids == [*KEPT_EVENTS, PRUNED_EVENT + 1, PRUNED_EVENT + 2]


def test_a_job_is_admitted_for_each_new_name(upgraded: sqlite3.Connection) -> None:
    rule = upgraded.execute(INSERT_RULE, (LOG_EXPORTER, CREATED_STAMP, CREATED_STAMP)).lastrowid
    upgraded.execute(
        "INSERT INTO scheduled_job (subsystem, rule_id, schedule_kind, schedule_expression, "
        "created_at, updated_at) VALUES (?, ?, 'interval', '3600', ?, ?)",
        (LOG_EXPORTER, rule, CREATED_STAMP, CREATED_STAMP),
    )
    upgraded.execute(INSERT_INFRASTRUCTURE_JOB, (SENDER, CREATED_STAMP, CREATED_STAMP))

    count = upgraded.execute(
        "SELECT COUNT(*) FROM scheduled_job WHERE subsystem IN (?, ?)", (LOG_EXPORTER, SENDER)
    ).fetchone()[0]

    assert count == 2


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        (INSERT_RULE, (SENDER, CREATED_STAMP, CREATED_STAMP)),
        (INSERT_RULE, ("bogus", CREATED_STAMP, CREATED_STAMP)),
        (INSERT_SUBSYSTEM_EVENT, ("bogus", CREATED_STAMP)),
        (INSERT_INFRASTRUCTURE_JOB, ("bogus", CREATED_STAMP, CREATED_STAMP)),
    ],
    ids=["rule-for-the-sender", "rule-unknown", "event-unknown", "job-unknown"],
)
def test_a_name_outside_the_list_is_still_refused(
    upgraded: sqlite3.Connection, statement: str, parameters: tuple[str, ...]
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(statement, parameters)


def test_deleting_a_rebuilt_parent_still_reaches_its_children(
    upgraded: sqlite3.Connection,
) -> None:
    upgraded.execute("DELETE FROM automation_rule WHERE id = ?", (BACKUP_RULE,))
    upgraded.execute("DELETE FROM automation_rule WHERE id = ?", (EXPORT_RULE,))

    def count(table: str) -> int:
        return int(upgraded.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    event = upgraded.execute(
        "SELECT rule_id, job_run_id FROM activity_event WHERE id = ?", (HIGHEST_EVENT,)
    ).fetchone()
    assert (count("scheduled_job"), count("job_run")) == (1, 0)
    assert (count("restart_hold_sighting"), count("activity_export_cursor")) == (0, 0)
    assert tuple(event) == (None, None)
    assert count("activity_event") == len(KEPT_EVENTS)


def test_a_database_with_no_surviving_events_keeps_its_sequence(tmp_path: Path) -> None:
    path = seeded_previous(tmp_path / "dockmin.db")
    connection = connect(path)
    connection.execute("DELETE FROM activity_event")
    connection.close()

    connection = migrate_to_this(path)
    try:
        connection.execute(INSERT_SUBSYSTEM_EVENT, (LOG_EXPORTER, CREATED_STAMP))
        [reissued] = connection.execute("SELECT id FROM activity_event").fetchone()
    finally:
        connection.close()

    assert reissued == PRUNED_EVENT + 1


def test_a_dangling_reference_rolls_the_migration_back_and_leaves_version_eighteen(
    tmp_path: Path,
) -> None:
    path = seeded_previous(tmp_path / "dockmin.db")
    connection = connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "INSERT INTO restart_hold_sighting (rule_id, environment_id, container_id, "
        "last_unhealthy_at) VALUES (?, ?, ?, ?)",
        (DANGLING_RULE, ENVIRONMENT_ID, "b" * 64, CREATED_STAMP),
    )
    connection.execute("PRAGMA foreign_keys = ON")
    before = snapshot(connection)
    schema = backup_snapshot.schema_objects(connection)
    to_this = migrations_up_to(MIGRATION_VERSION, tmp_path / "to-this")

    try:
        with pytest.raises(MigrationError, match="restart_hold_sighting"):
            migrate(connection, to_this)

        assert ledger_version(connection) == PREVIOUS_VERSION
        assert snapshot(connection) == before
        assert backup_snapshot.schema_objects(connection) == schema
        assert connection.in_transaction is False
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_the_upgraded_schema_equals_a_freshly_migrated_one_once_every_migration_is_applied(
    upgrade: Upgrade, tmp_path: Path, migrated_template: Path
) -> None:
    copy = tmp_path / "latest.db"
    shutil.copyfile(upgrade.path, copy)
    connection = connect(copy)
    fresh = connect(migrated_template)
    try:
        migrate(connection)
        upgraded_objects = backup_snapshot.schema_objects(connection)
        fresh_objects = backup_snapshot.schema_objects(fresh)
    finally:
        connection.close()
        fresh.close()

    assert upgraded_objects == fresh_objects
