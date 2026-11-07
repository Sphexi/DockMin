"""Migration 0012: the job_run rebuild, new scheduled_job columns and owner-scoped rule names."""

import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import MIGRATIONS_DIR, migrate

MIGRATION_VERSION = 12
STAMP = "2026-01-02T03:04:05Z"
UNIQUE_FAILED = "UNIQUE constraint failed"
CHECK_FAILED = "CHECK constraint failed"
NOT_NULL_FAILED = "NOT NULL constraint failed"
FOREIGN_KEY_FAILED = "FOREIGN KEY constraint failed"
MAX_REASON = 64
MAX_MESSAGE = 512
SCHEDULED_JOB_INSERT = (
    "INSERT INTO scheduled_job (subsystem, rule_id, schedule_kind, schedule_expression, "
    "created_at, updated_at) VALUES (?, ?, 'interval', '60', ?, ?)"
)
RULE_INSERT = (
    "INSERT INTO automation_rule (name, subsystem, target_selector, created_by_user_id, "
    "created_at, updated_at) VALUES (?, ?, '{}', ?, ?, ?)"
)
RUN_INSERT = (
    "INSERT INTO job_run (scheduled_job_id, trigger, scheduled_for, started_at, finished_at, "
    "outcome, reason, message, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
EVENT_INSERT = (
    "INSERT INTO activity_event (category, actor_type, actor_subsystem, rule_id, job_run_id, "
    "target_type, action, result, occurred_at) "
    "VALUES ('automation', 'subsystem', 'container-updater', ?, ?, 't', 'a', 'success', ?)"
)
NEW_JOB_RUN_COLUMNS = [
    "id",
    "scheduled_job_id",
    "trigger",
    "requested_by_user_id",
    "scheduled_for",
    "started_at",
    "finished_at",
    "heartbeat_at",
    "outcome",
    "reason",
    "message",
    "detail",
]
SEEDED_TABLES = ("environment", "scheduled_job", "activity_event", "job_run")


def seed_0011(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
        "VALUES ('e', 'unix_socket', '/run/e.sock', ?, ?)",
        (STAMP, STAMP),
    )
    for subsystem, interval in (
        ("backup-syncer", "3600"),
        ("environment-snapshot-refresher", "15"),
    ):
        connection.execute(
            "INSERT INTO scheduled_job (subsystem, rule_id, schedule_kind, schedule_expression, "
            "created_at, updated_at) VALUES (?, NULL, 'interval', ?, ?, ?)",
            (subsystem, interval, STAMP, STAMP),
        )
    for _ in range(3):
        connection.execute(
            "INSERT INTO activity_event (category, actor_type, actor_subsystem, target_type, "
            "action, result, occurred_at) VALUES ('automation', 'subsystem', "
            "'environment-snapshot-refresher', 'container', 'restart', 'success', ?)",
            (STAMP,),
        )
    connection.execute(
        "INSERT INTO activity_event (category, actor_type, target_type, action, result, "
        "occurred_at) VALUES ('automation', 'anonymous', 'x', 'y', 'success', ?)",
        (STAMP,),
    )


def row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in SEEDED_TABLES
    }


def schema(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        tuple(row)
        for row in connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name")
    ]


@pytest.fixture
def before(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    """A database migrated up to 0011, and the directory holding those migrations."""
    directory = tmp_path / "migrations"
    directory.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name[:4]) < MIGRATION_VERSION:
            shutil.copyfile(path, directory / path.name)
    connection = connect(tmp_path / "old.db")
    migrate(connection, directory)
    yield connection, directory
    connection.close()


def test_the_migration_keeps_every_row_and_leaves_the_foreign_keys_consistent(
    before: tuple[sqlite3.Connection, Path],
) -> None:
    connection, _ = before
    seed_0011(connection)
    counted = row_counts(connection)

    applied = migrate(connection)

    assert [m.version for m in applied if m.version <= MIGRATION_VERSION] == [MIGRATION_VERSION]
    assert row_counts(connection) == counted
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert migrate(connection) == []


def test_the_migrated_tables_have_the_new_shape_and_existing_jobs_get_defaults(
    before: tuple[sqlite3.Connection, Path],
) -> None:
    connection, _ = before
    seed_0011(connection)

    migrate(connection)

    columns = [row[1] for row in connection.execute("PRAGMA table_info(job_run)")]
    targets = [row[2] for row in connection.execute("PRAGMA foreign_key_list(activity_event)")]
    defaults = connection.execute(
        "SELECT time_zone, failure_threshold, environment_id FROM scheduled_job"
    ).fetchall()
    leftovers = connection.execute(
        "SELECT name FROM sqlite_master WHERE name IN ('migration_guard', 'job_run_new')"
    ).fetchall()
    assert columns == NEW_JOB_RUN_COLUMNS
    assert "job_run" in targets
    assert [tuple(row) for row in defaults] == [("UTC", None, None)] * 2
    assert leftovers == []
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'idx_activity_event_job_run_id'"
    ).fetchone()


def guard_with_a_rule(connection: sqlite3.Connection) -> None:
    connection.execute(RULE_INSERT, ("r", "container-updater", None, STAMP, STAMP))


def guard_with_a_run(connection: sqlite3.Connection) -> None:
    connection.execute("INSERT INTO job_run (scheduled_job_id, started_at) VALUES (1, ?)", (STAMP,))


def guard_with_two_jobs_for_one_rule(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = OFF")
    for _ in range(2):
        connection.execute(SCHEDULED_JOB_INSERT, ("container-updater", 77, STAMP, STAMP))
    connection.execute("PRAGMA foreign_keys = ON")


def guard_with_two_rule_less_jobs(connection: sqlite3.Connection) -> None:
    connection.execute(SCHEDULED_JOB_INSERT, ("environment-snapshot-refresher", None, STAMP, STAMP))


@pytest.mark.parametrize(
    ("populate", "column"),
    [
        pytest.param(guard_with_a_rule, "automation_rule_is_empty", id="rule-present"),
        pytest.param(guard_with_a_run, "job_run_is_empty", id="run-present"),
        pytest.param(
            guard_with_two_jobs_for_one_rule, "one_job_per_rule", id="two-jobs-for-a-rule"
        ),
        pytest.param(
            guard_with_two_rule_less_jobs, "one_infrastructure_job_each", id="two-rule-less-jobs"
        ),
    ],
)
def test_a_guard_aborts_naming_its_column_and_leaves_the_database_as_it_was(
    before: tuple[sqlite3.Connection, Path], populate: object, column: str
) -> None:
    connection, _ = before
    seed_0011(connection)
    populate(connection)  # type: ignore[operator]
    schema_before = schema(connection)
    counted = row_counts(connection)

    with pytest.raises(sqlite3.IntegrityError, match=column):
        migrate(connection)

    ledger = connection.execute("SELECT max(version) FROM schema_migration").fetchone()[0]
    assert ledger == MIGRATION_VERSION - 1
    assert schema(connection) == schema_before
    assert row_counts(connection) == counted
    assert not connection.in_transaction


@dataclass(frozen=True)
class Ids:
    environment: int
    owner: int
    other: int
    rule: int
    job: int


@pytest.fixture(scope="module")
def migrated(
    migrated_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[sqlite3.Connection, Ids]]:
    """One migrated database with a few parents; each test works inside a rolled-back savepoint."""
    path = tmp_path_factory.mktemp("scheduler-schema") / "dockmin.db"
    shutil.copyfile(migrated_template, path)
    connection = connect(path)
    environment = connection.execute(
        "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
        "VALUES ('e', 'unix_socket', '/run/e.sock', ?, ?) RETURNING id",
        (STAMP, STAMP),
    ).fetchone()[0]
    users = [
        connection.execute(
            "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
            "VALUES (?, 'x', 'administrator', 1, ?) RETURNING id",
            (name, STAMP),
        ).fetchone()[0]
        for name in ("owner", "other")
    ]
    rule = connection.execute(
        RULE_INSERT + " RETURNING id", ("r", "container-updater", users[0], STAMP, STAMP)
    ).fetchone()[0]
    job = connection.execute(
        SCHEDULED_JOB_INSERT + " RETURNING id", ("container-updater", rule, STAMP, STAMP)
    ).fetchone()[0]
    yield connection, Ids(environment, users[0], users[1], rule, job)
    connection.close()


@pytest.fixture
def db(migrated: tuple[sqlite3.Connection, Ids]) -> Iterator[sqlite3.Connection]:
    connection = migrated[0]
    connection.execute("SAVEPOINT test")
    yield connection
    connection.execute("ROLLBACK TO test")
    connection.execute("RELEASE test")


@pytest.fixture
def ids(migrated: tuple[sqlite3.Connection, Ids]) -> Ids:
    return migrated[1]


def insert_run(
    db: sqlite3.Connection,
    job: int,
    *,
    trigger: str = "schedule",
    scheduled_for: str | None = STAMP,
    finished_at: str | None = STAMP,
    outcome: str | None = "success",
    reason: str | None = None,
    message: str | None = None,
    detail: str | None = None,
) -> None:
    db.execute(
        RUN_INSERT,
        (job, trigger, scheduled_for, STAMP, finished_at, outcome, reason, message, detail),
    )


@pytest.mark.parametrize("trigger", ["schedule", "catch_up", "queued", "manual", "dry_run"])
def test_a_run_accepts_each_trigger(db: sqlite3.Connection, ids: Ids, trigger: str) -> None:
    insert_run(db, ids.job, trigger=trigger)


@pytest.mark.parametrize("trigger", ["cron", "", "SCHEDULE"])
def test_a_run_refuses_an_unknown_trigger(db: sqlite3.Connection, ids: Ids, trigger: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        insert_run(db, ids.job, trigger=trigger)


def test_a_run_requires_a_trigger(db: sqlite3.Connection, ids: Ids) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=NOT_NULL_FAILED):
        insert_run(db, ids.job, trigger=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("outcome", ["success", "partial"])
def test_a_success_or_partial_run_needs_no_reason(
    db: sqlite3.Connection, ids: Ids, outcome: str
) -> None:
    insert_run(db, ids.job, outcome=outcome)


@pytest.mark.parametrize("outcome", ["failure", "skipped", "interrupted"])
def test_any_other_outcome_needs_a_reason(db: sqlite3.Connection, ids: Ids, outcome: str) -> None:
    insert_run(db, ids.job, outcome=outcome, reason="some_reason")

    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        insert_run(db, ids.job, outcome=outcome)


def test_a_run_refuses_an_unknown_outcome(db: sqlite3.Connection, ids: Ids) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        insert_run(db, ids.job, outcome="error", reason="x")


def test_a_run_in_flight_has_neither_outcome_nor_finish_time(
    db: sqlite3.Connection, ids: Ids
) -> None:
    insert_run(db, ids.job, outcome=None, finished_at=None)


@pytest.mark.parametrize(
    ("outcome", "finished_at"),
    [pytest.param("success", None, id="outcome-only"), pytest.param(None, STAMP, id="finish-only")],
)
def test_an_outcome_and_a_finish_time_come_together(
    db: sqlite3.Connection, ids: Ids, outcome: str | None, finished_at: str | None
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        insert_run(db, ids.job, outcome=outcome, finished_at=finished_at)


def test_a_run_that_no_schedule_fired_has_no_fire_time(db: sqlite3.Connection, ids: Ids) -> None:
    insert_run(db, ids.job, trigger="manual", scheduled_for=None)


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param("a", id="shortest"),
        pytest.param("a" * MAX_REASON, id="longest"),
        pytest.param("under_score", id="underscore"),
    ],
)
def test_a_reason_is_a_short_machine_code(db: sqlite3.Connection, ids: Ids, reason: str) -> None:
    insert_run(db, ids.job, outcome="failure", reason=reason)


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param("a" * (MAX_REASON + 1), id="too-long"),
        pytest.param("", id="empty"),
        pytest.param("Upper", id="upper-case"),
        pytest.param("has-dash", id="dash"),
        pytest.param("has space", id="space"),
        pytest.param("digit1", id="digit"),
    ],
)
def test_a_reason_that_is_not_a_machine_code_is_refused(
    db: sqlite3.Connection, ids: Ids, reason: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        insert_run(db, ids.job, outcome="failure", reason=reason)


def test_a_message_may_be_five_hundred_and_twelve_characters(
    db: sqlite3.Connection, ids: Ids
) -> None:
    insert_run(db, ids.job, message="m" * MAX_MESSAGE)

    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        insert_run(db, ids.job, message="m" * (MAX_MESSAGE + 1))


def test_run_detail_must_be_valid_json(db: sqlite3.Connection, ids: Ids) -> None:
    insert_run(db, ids.job, detail='{"a":1}')

    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        insert_run(db, ids.job, detail="{nope")


def test_the_old_error_column_is_gone(db: sqlite3.Connection, ids: Ids) -> None:
    with pytest.raises(sqlite3.OperationalError, match="no column"):
        db.execute(
            "INSERT INTO job_run (scheduled_job_id, trigger, started_at, error) "
            "VALUES (?, 'manual', ?, 'x')",
            (ids.job, STAMP),
        )


def test_a_pruned_runs_id_is_never_issued_again(db: sqlite3.Connection, ids: Ids) -> None:
    insert_run(db, ids.job)
    first = db.execute("SELECT max(id) FROM job_run").fetchone()[0]
    db.execute("DELETE FROM job_run WHERE id = ?", (first,))

    insert_run(db, ids.job)

    assert db.execute("SELECT max(id) FROM job_run").fetchone()[0] > first


def test_a_run_names_a_job_that_exists(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=FOREIGN_KEY_FAILED):
        insert_run(db, 9999)


def test_a_run_remembers_the_user_who_asked_for_it_until_that_user_is_deleted(
    db: sqlite3.Connection, ids: Ids
) -> None:
    db.execute(
        "INSERT INTO job_run (scheduled_job_id, trigger, requested_by_user_id, started_at) "
        "VALUES (?, 'manual', ?, ?)",
        (ids.job, ids.other, STAMP),
    )

    db.execute("DELETE FROM user WHERE id = ?", (ids.other,))

    assert db.execute("SELECT requested_by_user_id FROM job_run").fetchone()[0] is None


def test_a_run_cannot_name_a_user_who_does_not_exist(db: sqlite3.Connection, ids: Ids) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=FOREIGN_KEY_FAILED):
        db.execute(
            "INSERT INTO job_run (scheduled_job_id, trigger, requested_by_user_id, started_at) "
            "VALUES (?, 'manual', 9999, ?)",
            (ids.job, STAMP),
        )


def test_a_rule_can_have_only_one_job(db: sqlite3.Connection, ids: Ids) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=UNIQUE_FAILED):
        db.execute(SCHEDULED_JOB_INSERT, ("container-updater", ids.rule, STAMP, STAMP))


def test_a_subsystem_can_have_only_one_job_that_has_no_rule(db: sqlite3.Connection) -> None:
    db.execute(SCHEDULED_JOB_INSERT, ("environment-snapshot-refresher", None, STAMP, STAMP))

    with pytest.raises(sqlite3.IntegrityError, match=UNIQUE_FAILED):
        db.execute(SCHEDULED_JOB_INSERT, ("environment-snapshot-refresher", None, STAMP, STAMP))


def test_different_subsystems_may_each_have_a_job_with_no_rule(db: sqlite3.Connection) -> None:
    db.execute(SCHEDULED_JOB_INSERT, ("environment-snapshot-refresher", None, STAMP, STAMP))

    db.execute(SCHEDULED_JOB_INSERT, ("backup-syncer", None, STAMP, STAMP))


@pytest.mark.parametrize("threshold", [0, 101, -1])
def test_a_failure_threshold_outside_one_to_a_hundred_is_refused(
    db: sqlite3.Connection, threshold: int
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        db.execute("UPDATE scheduled_job SET failure_threshold = ?", (threshold,))


@pytest.mark.parametrize("threshold", [1, 100, None])
def test_a_failure_threshold_of_one_to_a_hundred_or_none_is_accepted(
    db: sqlite3.Connection, threshold: int | None
) -> None:
    db.execute("UPDATE scheduled_job SET failure_threshold = ?", (threshold,))


def test_a_job_always_has_a_time_zone(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=NOT_NULL_FAILED):
        db.execute("UPDATE scheduled_job SET time_zone = NULL")


def test_a_job_names_an_environment_that_exists(db: sqlite3.Connection, ids: Ids) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=FOREIGN_KEY_FAILED):
        db.execute("UPDATE scheduled_job SET environment_id = 9999 WHERE id = ?", (ids.job,))


def test_deleting_an_environment_removes_the_jobs_that_name_it(
    db: sqlite3.Connection, ids: Ids
) -> None:
    db.execute(
        "UPDATE scheduled_job SET environment_id = ? WHERE id = ?", (ids.environment, ids.job)
    )

    db.execute("DELETE FROM environment WHERE id = ?", (ids.environment,))

    assert (
        db.execute("SELECT count(*) FROM scheduled_job WHERE id = ?", (ids.job,)).fetchone()[0] == 0
    )


def test_deleting_a_rule_removes_its_job_and_runs_and_leaves_events_unlinked(
    db: sqlite3.Connection, ids: Ids
) -> None:
    insert_run(db, ids.job)
    run = db.execute("SELECT max(id) FROM job_run").fetchone()[0]
    db.execute(EVENT_INSERT, (ids.rule, run, STAMP))

    db.execute("DELETE FROM automation_rule WHERE id = ?", (ids.rule,))

    left = db.execute("SELECT count(*) FROM scheduled_job WHERE id = ?", (ids.job,)).fetchone()[0]
    runs = db.execute("SELECT count(*) FROM job_run").fetchone()[0]
    event = db.execute("SELECT rule_id, job_run_id FROM activity_event").fetchone()
    assert (left, runs) == (0, 0)
    assert tuple(event) == (None, None)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_an_event_cannot_name_a_run_that_does_not_exist(db: sqlite3.Connection, ids: Ids) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=FOREIGN_KEY_FAILED):
        db.execute(EVENT_INSERT, (ids.rule, 99999, STAMP))


def test_an_owner_cannot_repeat_a_rule_name_in_a_subsystem_ignoring_ascii_case(
    db: sqlite3.Connection, ids: Ids
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=UNIQUE_FAILED):
        db.execute(RULE_INSERT, ("R", "container-updater", ids.owner, STAMP, STAMP))


@pytest.mark.parametrize(
    ("owner_is_other", "subsystem"),
    [
        pytest.param(True, "container-updater", id="another-owner"),
        pytest.param(False, "stack-restarter", id="another-subsystem"),
    ],
)
def test_a_rule_name_may_repeat_across_owners_and_across_subsystems(
    db: sqlite3.Connection, ids: Ids, owner_is_other: bool, subsystem: str
) -> None:
    owner = ids.other if owner_is_other else ids.owner

    db.execute(RULE_INSERT, ("r", subsystem, owner, STAMP, STAMP))


def test_rules_whose_owner_is_gone_do_not_collide_on_name(db: sqlite3.Connection) -> None:
    db.execute(RULE_INSERT, ("orphan", "container-updater", None, STAMP, STAMP))

    db.execute(RULE_INSERT, ("orphan", "container-updater", None, STAMP, STAMP))


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(
            "SELECT id FROM scheduled_job WHERE is_enabled = 1 AND next_run_at IS NOT NULL "
            "ORDER BY next_run_at LIMIT 10",
            id="due-jobs",
        ),
        pytest.param(
            "SELECT * FROM job_run WHERE scheduled_job_id = 1 AND id < 5 ORDER BY id DESC LIMIT 25",
            id="history-page",
        ),
        pytest.param(
            "SELECT * FROM job_run WHERE scheduled_job_id = 1 ORDER BY id DESC LIMIT 1",
            id="latest-run",
        ),
        pytest.param(
            "SELECT id FROM job_run WHERE outcome IS NULL ORDER BY heartbeat_at", id="in-flight"
        ),
        pytest.param("SELECT id FROM scheduled_job WHERE rule_id = 1", id="rule-cascade"),
        pytest.param("SELECT id FROM job_run WHERE scheduled_job_id = 1", id="job-cascade"),
        pytest.param("SELECT id FROM activity_event WHERE job_run_id = 1", id="event-link"),
        pytest.param(
            "SELECT id FROM scheduled_job WHERE environment_id = 1", id="environment-cascade"
        ),
        pytest.param(
            "SELECT count(*) FROM automation_rule WHERE created_by_user_id = 1", id="owner-count"
        ),
        pytest.param("SELECT id FROM job_run WHERE requested_by_user_id = 1", id="requesting-user"),
        pytest.param(
            "SELECT id FROM scheduled_job WHERE rule_id IS NULL AND subsystem = 'backup-syncer'",
            id="rule-less-job",
        ),
    ],
)
def test_the_hot_queries_are_served_by_an_index(db: sqlite3.Connection, statement: str) -> None:
    plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + statement)]

    assert plan and all("USING" in step for step in plan if step.startswith(("SCAN", "SEARCH")))
