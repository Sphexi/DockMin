"""Shared seeding, snapshots and upgrade fixtures for the rebuild-migration tests."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.db.engine import connect
from app.db.migrations import migrate
from tests.db.backup_support import build, migrations_up_to
from tests.db.helpers import CREATED_STAMP

PREVIOUS_VERSION = 18
MIGRATION_VERSION = 19
ENVIRONMENT_ID = 1
BACKUP_RULE, EXPORT_RULE, OTHER_RULE = 1, 2, 3
BACKUP_JOB, EXPORT_JOB, REFRESH_JOB = 1, 2, 3
BACKUP_RUN, EXPORT_RUN = 1, 2
HIGHEST_EVENT = 4
PRUNED_EVENT = 5
PRUNED_RUN = 3
KEPT_EVENTS = [1, 2, HIGHEST_EVENT]
CONTAINER = "a" * 64
DANGLING_RULE = 999
LOG_EXPORTER = "container-log-exporter"
SENDER = "notification-sender"
LEDGER = "schema_migration"
SEQUENCE = "sqlite_sequence"
INSERT_JOB = (
    "INSERT INTO scheduled_job (id, subsystem, rule_id, schedule_kind, schedule_expression, "
    "next_run_at, created_at, updated_at, failure_threshold, environment_id, last_run_at, "
    "last_run_outcome) VALUES (?, ?, ?, 'interval', '300', ?, ?, ?, 3, ?, ?, 'success')"
)
INSERT_RUN = (
    "INSERT INTO job_run (id, scheduled_job_id, trigger, started_at, finished_at, outcome) "
    "VALUES (?, ?, 'schedule', ?, ?, 'success')"
)
INSERT_RULE_EVENT = (
    "INSERT INTO activity_event (id, category, actor_type, actor_subsystem, rule_id, job_run_id, "
    "environment_id, target_type, target_id, action, result, detail, occurred_at) VALUES "
    "(?, 'automation', 'subsystem', ?, ?, ?, ?, 'container', ?, 'container_recreated', "
    "'success', '{}', ?)"
)
INSERT_SUBSYSTEM_EVENT = (
    "INSERT INTO activity_event (category, actor_type, actor_subsystem, target_type, action, "
    "result, occurred_at) VALUES ('automation', 'subsystem', ?, 'x', 'y', 'success', ?)"
)
INSERT_RULE = (
    "INSERT INTO automation_rule (name, subsystem, target_selector, created_at, updated_at) "
    "VALUES ('n', ?, '{}', ?, ?)"
)
INSERT_INFRASTRUCTURE_JOB = (
    "INSERT INTO scheduled_job (subsystem, schedule_kind, schedule_expression, created_at, "
    "updated_at) VALUES (?, 'interval', '60', ?, ?)"
)

type Rows = dict[str, list[tuple[Any, ...]]]


@dataclass(frozen=True, slots=True)
class Snapshot:
    rows: Rows
    indexes: list[tuple[Any, ...]]
    triggers: list[tuple[Any, ...]]
    sequence: dict[str, int]


def snapshot(connection: sqlite3.Connection) -> Snapshot:
    """Capture every row, index, trigger and sequence value apart from the ledger."""
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
        if row[0] not in (LEDGER, SEQUENCE)
    ]
    rows = {
        name: sorted(
            (tuple(row) for row in connection.execute(f'SELECT * FROM "{name}"')), key=repr
        )
        for name in names
    }
    indexes = [
        tuple(row)
        for row in connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'index' ORDER BY name"
        )
    ]
    triggers = [
        tuple(row)
        for row in connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        )
    ]
    sequence = {
        str(name): int(seq)
        for name, seq in connection.execute("SELECT name, seq FROM sqlite_sequence")
    }
    return Snapshot(rows, indexes, triggers, sequence)


def ledger_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute(f"SELECT max(version) FROM {LEDGER}").fetchone()[0])


def seed_children(connection: sqlite3.Connection) -> None:
    """Give every child of the rebuilt tables a row, and leave the top event id pruned."""
    run = connection.execute
    for job, subsystem, rule in (
        (BACKUP_JOB, "backup-syncer", BACKUP_RULE),
        (EXPORT_JOB, "activity-log-exporter", EXPORT_RULE),
        (REFRESH_JOB, "environment-snapshot-refresher", None),
    ):
        environment = ENVIRONMENT_ID if rule is not None else None
        run(INSERT_JOB, (job, subsystem, rule, *[CREATED_STAMP] * 3, environment, CREATED_STAMP))
    run(INSERT_RUN, (BACKUP_RUN, BACKUP_JOB, CREATED_STAMP, CREATED_STAMP))
    run(INSERT_RUN, (EXPORT_RUN, EXPORT_JOB, CREATED_STAMP, CREATED_STAMP))
    run(
        "INSERT INTO activity_export_cursor (rule_id, last_event_id, object_key, exported_at) "
        "VALUES (?, 2, 'p/a.csv.gz', ?)",
        (EXPORT_RULE, CREATED_STAMP),
    )
    run(
        "INSERT INTO restart_hold_sighting (rule_id, environment_id, container_id, "
        "last_unhealthy_at) VALUES (?, ?, ?, ?)",
        (BACKUP_RULE, ENVIRONMENT_ID, CONTAINER, CREATED_STAMP),
    )
    for event, rule, job_run in ((HIGHEST_EVENT, BACKUP_RULE, BACKUP_RUN), (PRUNED_EVENT, 2, 2)):
        run(
            INSERT_RULE_EVENT,
            (event, "backup-syncer", rule, job_run, ENVIRONMENT_ID, CONTAINER, CREATED_STAMP),
        )
    run("DELETE FROM activity_event WHERE id = ?", (PRUNED_EVENT,))


def seed_webhook_children(connection: sqlite3.Connection) -> None:
    """Add a replaced image, a queued delivery and a pruned run id above the surviving runs."""
    run = connection.execute
    run(INSERT_RUN, (PRUNED_RUN, BACKUP_JOB, CREATED_STAMP, CREATED_STAMP))
    run("DELETE FROM job_run WHERE id = ?", (PRUNED_RUN,))
    run(
        "INSERT INTO replaced_image (rule_id, rule_created_at, environment_id, image_id, "
        "replaced_at, target_type, target_id) VALUES (?, ?, ?, ?, ?, 'container', ?)",
        (BACKUP_RULE, CREATED_STAMP, ENVIRONMENT_ID, CONTAINER, CREATED_STAMP, CONTAINER),
    )
    run(
        "INSERT INTO notification_channel (name, kind, settings, created_at, updated_at) "
        "VALUES ('c', 'ntfy', '{}', ?, ?)",
        (CREATED_STAMP, CREATED_STAMP),
    )
    run(
        "INSERT INTO notification_outbox (channel_id, event_id, event_type, next_attempt_at, "
        "created_at) VALUES (1, ?, 'x', ?, ?)",
        (HIGHEST_EVENT, CREATED_STAMP, CREATED_STAMP),
    )


def seeded_previous(
    path: Path,
    previous: int = PREVIOUS_VERSION,
    *,
    extra: Callable[[sqlite3.Connection], None] | None = None,
) -> Path:
    build(path, up_to=previous)
    connection = connect(path)
    try:
        seed_children(connection)
        if extra is not None:
            extra(connection)
    finally:
        connection.close()
    return path


def migrate_to_this(path: Path, version: int = MIGRATION_VERSION) -> sqlite3.Connection:
    connection = connect(path)
    migrate(connection, migrations_up_to(version, path.parent / "to-this"))
    return connection


@dataclass(frozen=True, slots=True)
class Upgrade:
    path: Path
    before: Snapshot
    after: Snapshot
    foreign_keys_after: int
    leftovers: list[str]


def upgrade_seeded(
    directory: Path,
    previous: int,
    version: int,
    extra: Callable[[sqlite3.Connection], None] | None = None,
) -> Upgrade:
    """Seed a database at previous, migrate it to version and report before and after."""
    path = seeded_previous(directory / "dockmin.db", previous, extra=extra)
    connection = connect(path)
    before = snapshot(connection)
    connection.close()
    connection = migrate_to_this(path, version)
    try:
        assert ledger_version(connection) == version
        return Upgrade(
            path=path,
            before=before,
            after=snapshot(connection),
            foreign_keys_after=int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
            leftovers=[
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE '%\\_new' ESCAPE '\\'"
                )
            ],
        )
    finally:
        connection.close()
