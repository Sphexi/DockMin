"""Reads and writes on a database snapshot: copying, facts, stripping and schema comparison."""

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import migrate
from app.db.repositories import backup_snapshot
from app.db.repositories.activity_event import activity_sequence, raise_activity_sequence
from tests.db.backup_support import INSERT_ACTIVITY, NEWEST_ID, build, latest_version
from tests.db.helpers import CREATED_STAMP

ADMINISTRATORS = ["Admin", "zed"]
STACK_DIRECTORIES = ["a" * 32, "b" * 32]
RAISED_FLOOR = 1_000_000_003
LOWER_FLOOR = 5
PLANTED = "sqlite_persist"
HIDDEN_VIEW = "sqlite_view"
SEEDED_REGISTRY_ENTRIES = 2


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("snapshot-source") / "dockmin.db")


@pytest.fixture(scope="module")
def snapshot(source: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("snapshot-copy") / "snapshot.sqlite3"
    connection = connect(source)
    try:
        backup_snapshot.vacuum_into(connection, target)
    finally:
        connection.close()
    return target


@pytest.fixture
def copy(snapshot: Path, tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = shutil.copyfile(snapshot, tmp_path / "copy.sqlite3")
    connection = connect(Path(path))
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def reading(snapshot: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"{snapshot.as_uri()}?mode=ro", uri=True)
    yield connection
    connection.close()


def schema_names(connection: sqlite3.Connection) -> set[str]:
    return {item[1] for item in backup_snapshot.schema_objects(connection)}


def test_a_snapshot_is_a_complete_copy_at_the_current_schema(
    reading: sqlite3.Connection,
) -> None:
    assert backup_snapshot.schema_version(reading) == latest_version()
    assert backup_snapshot.integrity_findings(reading, limit=10) == []


def test_a_snapshot_refuses_an_existing_target_and_a_caller_inside_a_transaction(
    source: Path, tmp_path: Path
) -> None:
    existing = tmp_path / "existing.sqlite3"
    existing.write_bytes(b"")
    unwritten = tmp_path / "unwritten.sqlite3"
    connection = connect(source)

    try:
        with pytest.raises(FileExistsError):
            backup_snapshot.vacuum_into(connection, existing)
        connection.execute("BEGIN")
        with pytest.raises(RuntimeError):
            backup_snapshot.vacuum_into(connection, unwritten)
        connection.execute("ROLLBACK")
    finally:
        connection.close()

    assert not unwritten.exists()


def test_a_snapshot_taken_beside_an_open_write_holds_only_committed_rows(
    source: Path, tmp_path: Path
) -> None:
    writer = connect(source)
    reader = connect(source)
    target = tmp_path / "beside-a-writer.sqlite3"
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(INSERT_ACTIVITY, (CREATED_STAMP,))

        backup_snapshot.vacuum_into(reader, target)

        writer.execute("ROLLBACK")
    finally:
        writer.close()
        reader.close()
    copied = connect(target)
    try:
        counts = copied.execute("SELECT count(*) FROM activity_event").fetchone()[0]
        findings = backup_snapshot.integrity_findings(copied, limit=10)
    finally:
        copied.close()
    assert (counts, findings) == (NEWEST_ID - 1, [])


def test_the_enabled_administrators_come_back_in_code_point_order(
    reading: sqlite3.Connection,
) -> None:
    assert backup_snapshot.enabled_administrators(reading, limit=100) == ADMINISTRATORS
    assert backup_snapshot.enabled_administrators(reading, limit=1) == ADMINISTRATORS[:1]


@pytest.mark.parametrize(
    ("username", "found"),
    [("ADMIN", 1), ("off", None), ("std", None), ("nobody", None)],
    ids=["ignores-case", "disabled", "standard-user", "unknown"],
)
def test_an_operator_is_found_only_as_an_enabled_administrator(
    reading: sqlite3.Connection, username: str, found: int | None
) -> None:
    assert backup_snapshot.administrator_id(reading, username) == found


def test_stack_files_come_back_by_directory_as_utf8_bytes(reading: sqlite3.Connection) -> None:
    files = backup_snapshot.stack_files(reading, limit=10)

    assert [directory for directory, _ in files] == STACK_DIRECTORIES
    assert files[0][1] == b"services: {}\n"


def test_the_instance_counts_cover_every_kind_of_object(reading: sqlite3.Connection) -> None:
    counts = backup_snapshot.instance_counts(reading)

    assert (
        counts.users,
        counts.environments,
        counts.stacks,
        counts.registry_credentials,
        counts.object_storage_targets,
        counts.automation_rules,
    ) == (4, 1, 2, SEEDED_REGISTRY_ENTRIES, 1, 3)


@pytest.mark.parametrize("limit", [0, -1, True])
def test_a_limit_that_is_not_a_positive_integer_is_refused(
    reading: sqlite3.Connection, limit: int
) -> None:
    with pytest.raises(ValueError, match="limit"):
        backup_snapshot.stack_files(reading, limit=limit)


def test_the_activity_sequence_reads_the_counter_past_a_deleted_newest_row(
    reading: sqlite3.Connection,
) -> None:
    assert activity_sequence(reading) == NEWEST_ID


def test_raising_the_activity_sequence_never_lowers_it_and_the_next_id_follows_the_floor(
    copy: sqlite3.Connection,
) -> None:
    raise_activity_sequence(copy, RAISED_FLOOR)
    raise_activity_sequence(copy, LOWER_FLOOR)
    sequence = activity_sequence(copy)
    copy.execute(INSERT_ACTIVITY, (CREATED_STAMP,))

    newest = copy.execute("SELECT max(id) FROM activity_event").fetchone()[0]

    assert (sequence, newest) == (RAISED_FLOOR, RAISED_FLOOR + 1)


def test_raising_the_activity_sequence_creates_the_counter_when_none_exists(tmp_path: Path) -> None:
    connection = connect(tmp_path / "fresh.db")
    migrate(connection)
    try:
        raise_activity_sequence(connection, 10)

        assert activity_sequence(connection) == 10
    finally:
        connection.close()


def test_stripping_clears_sessions_and_throttles_and_leaves_secure_delete_on(
    copy: sqlite3.Connection,
) -> None:
    backup_snapshot.strip_ephemeral(copy)

    remaining = copy.execute(
        "SELECT (SELECT count(*) FROM session), (SELECT count(*) FROM login_throttle)"
    ).fetchone()
    assert tuple(remaining) == (0, 0)
    assert copy.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_stripping_and_raising_run_inside_a_callers_transaction(copy: sqlite3.Connection) -> None:
    copy.execute("BEGIN")

    backup_snapshot.strip_ephemeral(copy)
    raise_activity_sequence(copy, RAISED_FLOOR)
    copy.execute("ROLLBACK")

    assert copy.execute("SELECT count(*) FROM session").fetchone()[0] == 1
    assert activity_sequence(copy) == NEWEST_ID


def test_a_row_naming_a_missing_parent_is_reported_by_table_and_row(
    copy: sqlite3.Connection,
) -> None:
    copy.execute("PRAGMA foreign_keys = OFF")
    copy.execute(
        "INSERT INTO session (user_id, token_hash, issued_at, expires_at) VALUES (999, 'z', ?, ?)",
        (CREATED_STAMP, CREATED_STAMP),
    )

    findings = backup_snapshot.integrity_findings(copy, limit=10)

    assert findings == ["session row 2 refers to a missing user row"]


def test_a_file_with_no_ledger_has_schema_version_zero(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "empty.db")
    try:
        assert backup_snapshot.schema_version(connection) == 0
    finally:
        connection.close()


@pytest.fixture(scope="module")
def expected_schema(
    tmp_path_factory: pytest.TempPathFactory,
) -> list[tuple[str, str, str, str | None]]:
    connection = connect(tmp_path_factory.mktemp("expected") / "fresh.db")
    try:
        migrate(connection)
        return backup_snapshot.schema_objects(connection)
    finally:
        connection.close()


def test_a_trigger_renamed_to_a_reserved_name_is_reported_and_analyze_tables_are_not(
    tmp_path: Path, expected_schema: list[tuple[str, str, str, str | None]]
) -> None:
    path = tmp_path / "planted.db"
    connection = connect(path)
    migrate(connection)
    connection.execute("CREATE TRIGGER t_persist AFTER INSERT ON setting BEGIN SELECT 1; END")
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        f"UPDATE sqlite_master SET name = '{PLANTED}', "
        f"sql = replace(sql, 't_persist', '{PLANTED}') WHERE name = 't_persist'"
    )
    connection.execute("PRAGMA writable_schema = OFF")
    connection.close()
    reopened = connect(path)
    try:
        before_analyze = backup_snapshot.schema_objects(reopened)
        reopened.execute("ANALYZE")
        after_analyze = backup_snapshot.schema_objects(reopened)
    finally:
        reopened.close()
    expected_names = {item[1] for item in expected_schema}
    assert {item[1] for item in before_analyze} - expected_names == {PLANTED}
    assert {item[1] for item in after_analyze} - expected_names == {PLANTED}


def test_a_view_renamed_to_a_reserved_name_is_reported(
    tmp_path: Path, expected_schema: list[tuple[str, str, str, str | None]]
) -> None:
    path = tmp_path / "view.db"
    connection = connect(path)
    migrate(connection)
    connection.execute("CREATE VIEW v_x AS SELECT 1")
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        f"UPDATE sqlite_master SET name = '{HIDDEN_VIEW}', tbl_name = '{HIDDEN_VIEW}', "
        f"sql = 'CREATE VIEW {HIDDEN_VIEW} AS SELECT 1' WHERE name = 'v_x'"
    )
    connection.execute("PRAGMA writable_schema = OFF")
    connection.close()
    reopened = connect(path)
    try:
        found = schema_names(reopened) - {item[1] for item in expected_schema}
    finally:
        reopened.close()

    assert found == {HIDDEN_VIEW}
