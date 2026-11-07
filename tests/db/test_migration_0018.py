"""Migration 0018: the last time a rule saw one of its held containers unhealthy."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import migrate
from app.db.repositories import backup_snapshot
from tests.db.backup_support import build, latest_version
from tests.db.helpers import CREATED_STAMP, LATER_STAMP

PREVIOUS_VERSION = 17
MIGRATION_VERSION = 18
ENVIRONMENT_ID = 1
RULE_ONE, RULE_TWO, RULE_THREE = 1, 2, 3
UNKNOWN_RULE = 9999
CONTAINER = "a" * 64
OTHER_CONTAINER = "b" * 64
THIRD_CONTAINER = "c" * 64
INSERT_SIGHTING = (
    "INSERT INTO restart_hold_sighting (rule_id, environment_id, container_id, "
    "last_unhealthy_at) VALUES (?, ?, ?, ?)"
)
COUNT_SIGHTINGS = "SELECT COUNT(*) FROM restart_hold_sighting WHERE rule_id = ?"


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


def test_a_database_at_the_previous_schema_upgrades_with_an_empty_sighting_table(
    upgraded: sqlite3.Connection,
) -> None:
    count = upgraded.execute("SELECT COUNT(*) FROM restart_hold_sighting").fetchone()[0]

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


def test_a_sighting_is_stored_by_rule_environment_and_container(
    upgraded: sqlite3.Connection,
) -> None:
    upgraded.execute(INSERT_SIGHTING, (RULE_ONE, ENVIRONMENT_ID, CONTAINER, CREATED_STAMP))

    row = upgraded.execute(
        "SELECT last_unhealthy_at FROM restart_hold_sighting "
        "WHERE rule_id = ? AND environment_id = ? AND container_id = ?",
        (RULE_ONE, ENVIRONMENT_ID, CONTAINER),
    ).fetchone()

    assert row[0] == CREATED_STAMP


def test_a_second_sighting_of_the_same_container_by_the_same_rule_is_refused(
    upgraded: sqlite3.Connection,
) -> None:
    upgraded.execute(INSERT_SIGHTING, (RULE_ONE, ENVIRONMENT_ID, OTHER_CONTAINER, CREATED_STAMP))

    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(INSERT_SIGHTING, (RULE_ONE, ENVIRONMENT_ID, OTHER_CONTAINER, LATER_STAMP))


def test_deleting_a_rule_deletes_its_sightings_and_no_other_rules(
    upgraded: sqlite3.Connection,
) -> None:
    upgraded.execute(INSERT_SIGHTING, (RULE_TWO, ENVIRONMENT_ID, THIRD_CONTAINER, CREATED_STAMP))
    upgraded.execute(INSERT_SIGHTING, (RULE_THREE, ENVIRONMENT_ID, THIRD_CONTAINER, CREATED_STAMP))

    upgraded.execute("DELETE FROM automation_rule WHERE id = ?", (RULE_THREE,))

    kept = upgraded.execute(COUNT_SIGHTINGS, (RULE_TWO,)).fetchone()[0]
    gone = upgraded.execute(COUNT_SIGHTINGS, (RULE_THREE,)).fetchone()[0]
    assert (kept, gone) == (1, 0)


def test_deleting_an_environment_deletes_the_sightings_made_in_it(
    upgraded: sqlite3.Connection,
) -> None:
    cursor = upgraded.execute(
        "INSERT INTO environment (name, type, connection_kind, socket_path, created_at, "
        "updated_at) VALUES ('second', 'standalone', 'unix_socket', '/run/second.sock', ?, ?)",
        (CREATED_STAMP, CREATED_STAMP),
    )
    environment_id = cursor.lastrowid
    upgraded.execute(INSERT_SIGHTING, (RULE_ONE, environment_id, CONTAINER, CREATED_STAMP))

    upgraded.execute("DELETE FROM environment WHERE id = ?", (environment_id,))

    remaining = upgraded.execute(
        "SELECT COUNT(*) FROM restart_hold_sighting WHERE environment_id = ?", (environment_id,)
    ).fetchone()[0]
    assert remaining == 0


def test_a_sighting_of_a_rule_that_does_not_exist_is_refused(
    upgraded: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(INSERT_SIGHTING, (UNKNOWN_RULE, ENVIRONMENT_ID, CONTAINER, CREATED_STAMP))


@pytest.mark.parametrize(
    "container_id",
    ["a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 63 + " ", ""],
    ids=["short", "long", "upper-case", "not-hex", "trailing-space", "empty"],
)
def test_a_container_id_that_is_not_sixty_four_lower_case_hex_digits_is_refused(
    upgraded: sqlite3.Connection, container_id: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(INSERT_SIGHTING, (RULE_TWO, ENVIRONMENT_ID, container_id, CREATED_STAMP))


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
def test_a_time_that_is_not_the_stored_timestamp_form_is_refused(
    upgraded: sqlite3.Connection, stamp: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(INSERT_SIGHTING, (RULE_TWO, ENVIRONMENT_ID, CONTAINER, stamp))
