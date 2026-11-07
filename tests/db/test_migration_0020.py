"""Migration 0020: how far each container log exporter rule has copied each container's log."""

import itertools
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.db.engine import connect
from app.db.migrations import migrate
from app.db.repositories import backup_snapshot
from tests.db.backup_support import build, latest_version
from tests.db.helpers import CREATED_STAMP, LATER_STAMP

PREVIOUS_VERSION = 19
MIGRATION_VERSION = 20
ENVIRONMENT_ID = 1
RULE_ONE, RULE_TWO, RULE_THREE = 1, 2, 3
UNKNOWN_ID = 9999
RULE_CREATED = "2026-01-01T00:00:00Z"
OBJECT_PATH = "logs/container-logs/rule-1/1/web-aaaaaaaaaaaa/2026/01/02/1.ndjson.gz"
MAX_PATH = 1024
CONTAINERS = itertools.count(1)
INSERT_CURSOR = (
    "INSERT INTO container_log_cursor (rule_id, rule_created_at, environment_id, container_id, "
    "last_timestamp_ns, lines_at_last, object_path, last_seen_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
COUNT_FOR_RULE = "SELECT COUNT(*) FROM container_log_cursor WHERE rule_id = ?"


def new_container() -> str:
    return f"{next(CONTAINERS):064x}"


def add_cursor(connection: sqlite3.Connection, **columns: Any) -> None:
    values: dict[str, Any] = {
        "rule_id": RULE_ONE,
        "rule_created_at": RULE_CREATED,
        "environment_id": ENVIRONMENT_ID,
        "container_id": new_container(),
        "last_timestamp_ns": 1,
        "lines_at_last": 1,
        "object_path": OBJECT_PATH,
        "last_seen_at": CREATED_STAMP,
    }
    values.update(columns)
    connection.execute(INSERT_CURSOR, tuple(values.values()))


@pytest.fixture(scope="module")
def upgraded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    path = build(tmp_path_factory.mktemp("upgrade") / "dockmin.db", up_to=PREVIOUS_VERSION)
    connection = connect(path)
    rules_before = connection.execute("SELECT COUNT(*) FROM automation_rule").fetchone()[0]
    applied = [migration.version for migration in migrate(connection)]
    assert applied == list(range(MIGRATION_VERSION, latest_version() + 1))
    assert connection.execute("SELECT COUNT(*) FROM automation_rule").fetchone()[0] == rules_before
    yield connection
    connection.close()


def test_a_database_at_the_previous_schema_upgrades_with_an_empty_cursor_table(
    upgraded: sqlite3.Connection,
) -> None:
    count = upgraded.execute("SELECT COUNT(*) FROM container_log_cursor").fetchone()[0]

    assert count == 0
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


def test_a_cursor_is_stored_and_read_back_by_rule_environment_and_container(
    upgraded: sqlite3.Connection,
) -> None:
    container = new_container()
    add_cursor(
        upgraded, container_id=container, last_timestamp_ns=1_700_000_000_123, lines_at_last=3
    )

    row = upgraded.execute(
        "SELECT rule_created_at, last_timestamp_ns, lines_at_last, object_path, last_seen_at "
        "FROM container_log_cursor WHERE rule_id = ? AND environment_id = ? AND container_id = ?",
        (RULE_ONE, ENVIRONMENT_ID, container),
    ).fetchone()

    assert tuple(row) == (RULE_CREATED, 1_700_000_000_123, 3, OBJECT_PATH, CREATED_STAMP)


def test_a_second_cursor_for_the_same_rule_environment_and_container_is_refused(
    upgraded: sqlite3.Connection,
) -> None:
    container = new_container()
    add_cursor(upgraded, container_id=container)

    with pytest.raises(sqlite3.IntegrityError):
        add_cursor(upgraded, container_id=container, last_seen_at=LATER_STAMP)


def test_two_rules_may_each_keep_a_cursor_for_the_same_container(
    upgraded: sqlite3.Connection,
) -> None:
    container = new_container()
    add_cursor(upgraded, rule_id=RULE_ONE, container_id=container)

    add_cursor(upgraded, rule_id=RULE_TWO, container_id=container)

    count = upgraded.execute(
        "SELECT COUNT(*) FROM container_log_cursor WHERE container_id = ?", (container,)
    ).fetchone()[0]
    assert count == 2


def test_a_cursor_with_no_object_marks_an_examined_container(upgraded: sqlite3.Connection) -> None:
    container = new_container()

    add_cursor(upgraded, container_id=container, object_path=None)

    stored = upgraded.execute(
        "SELECT object_path FROM container_log_cursor WHERE container_id = ?", (container,)
    ).fetchone()[0]
    assert stored is None


@pytest.mark.parametrize(
    "container_id",
    ["a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 63 + " ", ""],
    ids=["short", "long", "upper-case", "not-hex", "trailing-space", "empty"],
)
def test_a_container_id_that_is_not_sixty_four_lower_case_hex_digits_is_refused(
    upgraded: sqlite3.Connection, container_id: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        add_cursor(upgraded, container_id=container_id)


@pytest.mark.parametrize(
    "columns",
    [
        {"last_timestamp_ns": -1},
        {"lines_at_last": 0},
        {"object_path": ""},
        {"object_path": "p" * (MAX_PATH + 1)},
    ],
    ids=["negative-timestamp", "no-lines", "empty-path", "path-too-long"],
)
def test_a_position_outside_its_bounds_is_refused(
    upgraded: sqlite3.Connection, columns: dict[str, Any]
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        add_cursor(upgraded, **columns)


@pytest.mark.parametrize(
    "columns",
    [{"last_timestamp_ns": 0}, {"object_path": "p" * MAX_PATH}],
    ids=["zero-timestamp", "longest-path"],
)
def test_a_position_at_the_edge_of_its_bounds_is_stored(
    upgraded: sqlite3.Connection, columns: dict[str, Any]
) -> None:
    add_cursor(upgraded, **columns)


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-01-02 03:04:05Z",
        "2026-01-02T03:04:05",
        "2026-01-02T03:04:05.000Z",
        "2026-01-02T03:04:0xZ",
        "2026-01-02T03:04:05+00:00",
        "",
    ],
    ids=["space", "no-zone", "fraction", "not-digits", "offset", "empty"],
)
def test_a_seen_time_that_is_not_the_stored_timestamp_form_is_refused(
    upgraded: sqlite3.Connection, stamp: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        add_cursor(upgraded, last_seen_at=stamp)


def test_deleting_a_rule_deletes_its_cursors_and_no_other_rules(
    upgraded: sqlite3.Connection,
) -> None:
    add_cursor(upgraded, rule_id=RULE_TWO)
    add_cursor(upgraded, rule_id=RULE_THREE)

    upgraded.execute("DELETE FROM automation_rule WHERE id = ?", (RULE_THREE,))

    kept = upgraded.execute(COUNT_FOR_RULE, (RULE_TWO,)).fetchone()[0]
    gone = upgraded.execute(COUNT_FOR_RULE, (RULE_THREE,)).fetchone()[0]
    assert kept >= 1
    assert gone == 0


def test_deleting_an_environment_deletes_the_cursors_kept_in_it(
    upgraded: sqlite3.Connection,
) -> None:
    environment_id = upgraded.execute(
        "INSERT INTO environment (name, type, connection_kind, socket_path, created_at, "
        "updated_at) VALUES ('second', 'standalone', 'unix_socket', '/run/second.sock', ?, ?)",
        (CREATED_STAMP, CREATED_STAMP),
    ).lastrowid
    add_cursor(upgraded, environment_id=environment_id)

    upgraded.execute("DELETE FROM environment WHERE id = ?", (environment_id,))

    remaining = upgraded.execute(
        "SELECT COUNT(*) FROM container_log_cursor WHERE environment_id = ?", (environment_id,)
    ).fetchone()[0]
    assert remaining == 0


@pytest.mark.parametrize(
    "columns",
    [{"rule_id": UNKNOWN_ID}, {"environment_id": UNKNOWN_ID}],
    ids=["rule", "environment"],
)
def test_a_cursor_of_a_rule_or_environment_that_does_not_exist_is_refused(
    upgraded: sqlite3.Connection, columns: dict[str, Any]
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        add_cursor(upgraded, **columns)


def test_the_environment_of_a_cursor_is_indexed(upgraded: sqlite3.Connection) -> None:
    indexed = upgraded.execute(
        "SELECT tbl_name FROM sqlite_master WHERE name = 'idx_container_log_cursor_environment_id'"
    ).fetchone()

    assert indexed[0] == "container_log_cursor"
