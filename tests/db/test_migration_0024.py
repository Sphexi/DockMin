"""Migration 0024: the webhook table and the names admitted by the rebuilt tables."""

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
    BACKUP_JOB,
    BACKUP_RULE,
    CONTAINER,
    DANGLING_RULE,
    ENVIRONMENT_ID,
    HIGHEST_EVENT,
    INSERT_RULE,
    INSERT_RULE_EVENT,
    INSERT_RUN,
    INSERT_SUBSYSTEM_EVENT,
    PRUNED_EVENT,
    PRUNED_RUN,
    Snapshot,
    Upgrade,
    ledger_version,
    seed_webhook_children,
    seeded_previous,
    snapshot,
    upgrade_seeded,
)

WEBHOOK_PREVIOUS_VERSION = 23
WEBHOOK_VERSION = 24


WEBHOOK_RECEIVER = "webhook-receiver"
WEBHOOK_PREFIX = "AbCd1234"
WEBHOOK_HASH = "ab" * 32
WEBHOOK_TOKEN_COLUMNS = (
    "rule_id, token_prefix, token_hash, issued_at, revoked_at, revoked_reason, last_called_at, "
    "last_call_address, pending_calls"
)
INSERT_WEBHOOK_JOB = (
    "INSERT INTO scheduled_job (subsystem, rule_id, schedule_kind, schedule_expression, "
    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
)
INSERT_WEBHOOK = (
    f"INSERT INTO webhook ({WEBHOOK_TOKEN_COLUMNS}) "
    "VALUES (:rule_id, :token_prefix, :token_hash, :issued_at, :revoked_at, :revoked_reason, "
    ":last_called_at, :last_call_address, :pending_calls)"
)
ACTIVE_WEBHOOK = {
    "token_prefix": WEBHOOK_PREFIX,
    "token_hash": WEBHOOK_HASH,
    "issued_at": CREATED_STAMP,
    "revoked_at": None,
    "revoked_reason": None,
    "last_called_at": None,
    "last_call_address": None,
    "pending_calls": 0,
}
REVOKED_WEBHOOK = {
    **ACTIVE_WEBHOOK,
    "token_hash": None,
    "revoked_at": CREATED_STAMP,
    "revoked_reason": "manual",
}
MAX_ADDRESS = 64
MAX_PENDING = 1_000_000


def snapshot_without_webhook(after: Snapshot) -> Snapshot:
    """Return the snapshot with the table and index this migration added taken out."""
    return Snapshot(
        rows={name: rows for name, rows in after.rows.items() if name != "webhook"},
        indexes=[row for row in after.indexes if row[1] != "webhook"],
        triggers=after.triggers,
        sequence=after.sequence,
    )


def add_webhook_rule(connection: sqlite3.Connection) -> int:
    rule = connection.execute(INSERT_RULE, (WEBHOOK_RECEIVER, CREATED_STAMP, CREATED_STAMP))
    assert rule.lastrowid is not None
    return rule.lastrowid


def add_webhook(connection: sqlite3.Connection, rule_id: int, **overrides: object) -> None:
    connection.execute(INSERT_WEBHOOK, {"rule_id": rule_id, **ACTIVE_WEBHOOK, **overrides})


@pytest.fixture(scope="module")
def webhook_upgrade(tmp_path_factory: pytest.TempPathFactory) -> Upgrade:
    return upgrade_seeded(
        tmp_path_factory.mktemp("webhook-upgrade"),
        WEBHOOK_PREVIOUS_VERSION,
        WEBHOOK_VERSION,
        seed_webhook_children,
    )


@pytest.fixture
def webhook_upgraded(webhook_upgrade: Upgrade, tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A private copy of the database upgraded to the webhook schema, safe to change."""
    copy = tmp_path / "copy.db"
    shutil.copyfile(webhook_upgrade.path, copy)
    connection = connect(copy)
    yield connection
    connection.close()


def test_the_webhook_rebuild_keeps_every_row_with_its_id_and_every_index_and_trigger(
    webhook_upgrade: Upgrade,
) -> None:
    populated = {name for name, rows in webhook_upgrade.before.rows.items() if rows}

    kept = snapshot_without_webhook(webhook_upgrade.after)

    assert kept == webhook_upgrade.before
    assert webhook_upgrade.after.rows["webhook"] == []
    assert {
        "automation_rule",
        "scheduled_job",
        "job_run",
        "activity_event",
        "replaced_image",
        "notification_outbox",
    } <= populated


def test_the_webhook_rebuild_carries_the_run_and_event_sequences_past_pruned_ids(
    webhook_upgrade: Upgrade,
) -> None:
    assert webhook_upgrade.after.sequence["job_run"] == PRUNED_RUN
    assert webhook_upgrade.after.sequence["activity_event"] == PRUNED_EVENT
    assert webhook_upgrade.after.sequence == webhook_upgrade.before.sequence


def test_the_webhook_rebuild_leaves_a_consistent_database_with_foreign_keys_back_on(
    webhook_upgrade: Upgrade, webhook_upgraded: sqlite3.Connection
) -> None:
    assert webhook_upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
    assert webhook_upgraded.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert webhook_upgrade.foreign_keys_after == 1
    assert webhook_upgrade.leftovers == []


def test_deleting_a_rebuilt_rule_still_reaches_its_replaced_images_and_unlinks_its_events(
    webhook_upgraded: sqlite3.Connection,
) -> None:
    webhook_upgraded.execute("DELETE FROM automation_rule WHERE id = ?", (BACKUP_RULE,))

    images = webhook_upgraded.execute("SELECT COUNT(*) FROM replaced_image").fetchone()[0]
    event = webhook_upgraded.execute(
        "SELECT rule_id, job_run_id FROM activity_event WHERE id = ?", (HIGHEST_EVENT,)
    ).fetchone()
    queued = webhook_upgraded.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
    assert images == 0
    assert tuple(event) == (None, None)
    assert queued == 1


def test_a_run_and_an_event_are_numbered_above_every_id_already_used(
    webhook_upgraded: sqlite3.Connection,
) -> None:
    webhook_upgraded.execute(INSERT_RUN, (None, BACKUP_JOB, CREATED_STAMP, CREATED_STAMP))
    webhook_upgraded.execute(INSERT_SUBSYSTEM_EVENT, (WEBHOOK_RECEIVER, CREATED_STAMP))

    run = webhook_upgraded.execute("SELECT max(id) FROM job_run").fetchone()[0]
    event = webhook_upgraded.execute("SELECT max(id) FROM activity_event").fetchone()[0]

    assert (run, event) == (PRUNED_RUN + 1, PRUNED_EVENT + 1)


def test_a_webhook_rule_job_run_and_event_are_admitted(
    webhook_upgraded: sqlite3.Connection,
) -> None:
    rule = add_webhook_rule(webhook_upgraded)
    job = webhook_upgraded.execute(
        INSERT_WEBHOOK_JOB,
        (WEBHOOK_RECEIVER, rule, "webhook", "", CREATED_STAMP, CREATED_STAMP),
    ).lastrowid
    run = webhook_upgraded.execute(
        "INSERT INTO job_run (scheduled_job_id, trigger, started_at) VALUES (?, 'webhook', ?)",
        (job, CREATED_STAMP),
    ).lastrowid
    webhook_upgraded.execute(
        INSERT_RULE_EVENT,
        (None, WEBHOOK_RECEIVER, rule, run, ENVIRONMENT_ID, CONTAINER, CREATED_STAMP),
    )

    stored = webhook_upgraded.execute(
        "SELECT (SELECT subsystem FROM automation_rule WHERE id = ?), "
        "(SELECT schedule_kind FROM scheduled_job WHERE id = ?), "
        "(SELECT trigger FROM job_run WHERE id = ?), "
        "(SELECT actor_subsystem FROM activity_event WHERE job_run_id = ?)",
        (rule, job, run, run),
    ).fetchone()
    assert tuple(stored) == (WEBHOOK_RECEIVER, "webhook", "webhook", WEBHOOK_RECEIVER)


@pytest.mark.parametrize(
    ("kind", "expression", "has_rule"),
    [
        ("webhook", "", False),
        ("webhook", "60", True),
        ("push", "", True),
    ],
    ids=["webhook-with-no-rule", "webhook-with-an-expression", "unknown-kind"],
)
def test_a_job_outside_the_webhook_shape_is_refused(
    webhook_upgraded: sqlite3.Connection, kind: str, expression: str, has_rule: bool
) -> None:
    rule = add_webhook_rule(webhook_upgraded) if has_rule else None

    with pytest.raises(sqlite3.IntegrityError):
        webhook_upgraded.execute(
            INSERT_WEBHOOK_JOB,
            (WEBHOOK_RECEIVER, rule, kind, expression, CREATED_STAMP, CREATED_STAMP),
        )


def test_a_run_with_an_unknown_trigger_is_still_refused(
    webhook_upgraded: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        webhook_upgraded.execute(
            "INSERT INTO job_run (scheduled_job_id, trigger, started_at) VALUES (?, 'bogus', ?)",
            (BACKUP_JOB, CREATED_STAMP),
        )


def test_an_active_and_a_revoked_webhook_are_stored_and_a_prefix_is_stored_once(
    webhook_upgraded: sqlite3.Connection,
) -> None:
    first, second, third = (add_webhook_rule(webhook_upgraded) for _ in range(3))
    add_webhook(webhook_upgraded, first)
    webhook_upgraded.execute(
        INSERT_WEBHOOK, {"rule_id": second, **REVOKED_WEBHOOK, "token_prefix": "Zz9Yy8Xx"}
    )

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        add_webhook(webhook_upgraded, third)

    stored = webhook_upgraded.execute(
        "SELECT token_hash IS NULL FROM webhook ORDER BY rule_id"
    ).fetchall()
    assert [row[0] for row in stored] == [0, 1]


@pytest.mark.parametrize(
    "overrides",
    [
        {"token_prefix": "AbCd123"},
        {"token_prefix": "AbCd12345"},
        {"token_prefix": "AbCd-234"},
        {"token_hash": "ab" * 31 + "a"},
        {"token_hash": "AB" * 32},
        {"issued_at": "2026-01-02 03:04:05"},
        {"issued_at": "2026-01-02T03:04:05.000Z"},
        {"revoked_at": CREATED_STAMP},
        {"token_hash": None},
        {"revoked_reason": "bogus"},
        {"last_call_address": "a" * (MAX_ADDRESS + 1)},
        {"pending_calls": -1},
        {"pending_calls": MAX_PENDING + 1},
    ],
    ids=[
        "prefix-too-short",
        "prefix-too-long",
        "prefix-not-alphanumeric",
        "hash-too-short",
        "hash-not-lower-case-hex",
        "issued-without-a-zone-mark",
        "issued-with-fractional-seconds",
        "revoked-time-with-a-hash",
        "no-hash-without-a-revoked-time",
        "unknown-revoke-reason",
        "address-too-long",
        "negative-pending-calls",
        "pending-calls-over-the-ceiling",
    ],
)
def test_a_webhook_outside_its_constraints_is_refused(
    webhook_upgraded: sqlite3.Connection, overrides: dict[str, object]
) -> None:
    rule = add_webhook_rule(webhook_upgraded)

    with pytest.raises(sqlite3.IntegrityError):
        add_webhook(webhook_upgraded, rule, **overrides)


def test_a_webhook_at_the_edge_of_its_limits_is_stored(
    webhook_upgraded: sqlite3.Connection,
) -> None:
    rule = add_webhook_rule(webhook_upgraded)

    add_webhook(
        webhook_upgraded,
        rule,
        last_call_address="a" * MAX_ADDRESS,
        pending_calls=MAX_PENDING,
    )

    stored = webhook_upgraded.execute("SELECT pending_calls FROM webhook").fetchone()[0]
    assert stored == MAX_PENDING


def test_a_webhook_goes_with_its_rule_and_a_reused_rule_id_inherits_nothing(
    webhook_upgraded: sqlite3.Connection,
) -> None:
    rule = add_webhook_rule(webhook_upgraded)
    add_webhook(webhook_upgraded, rule)

    webhook_upgraded.execute("DELETE FROM automation_rule WHERE id = ?", (rule,))
    reused = add_webhook_rule(webhook_upgraded)

    count = webhook_upgraded.execute("SELECT COUNT(*) FROM webhook").fetchone()[0]
    assert (reused, count) == (rule, 0)


def test_a_webhook_for_a_rule_that_does_not_exist_is_refused(
    webhook_upgraded: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        add_webhook(webhook_upgraded, DANGLING_RULE)


def test_a_dangling_reference_rolls_the_webhook_migration_back_and_leaves_version_twenty_three(
    tmp_path: Path,
) -> None:
    path = seeded_previous(
        tmp_path / "dockmin.db", WEBHOOK_PREVIOUS_VERSION, extra=seed_webhook_children
    )
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
    to_this = migrations_up_to(WEBHOOK_VERSION, tmp_path / "to-this")

    try:
        with pytest.raises(MigrationError, match="restart_hold_sighting"):
            migrate(connection, to_this)

        assert ledger_version(connection) == WEBHOOK_PREVIOUS_VERSION
        assert snapshot(connection) == before
        assert backup_snapshot.schema_objects(connection) == schema
        assert connection.in_transaction is False
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_the_webhook_upgraded_schema_equals_a_freshly_migrated_one(
    webhook_upgrade: Upgrade, tmp_path: Path, migrated_template: Path
) -> None:
    copy = tmp_path / "latest.db"
    shutil.copyfile(webhook_upgrade.path, copy)
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
