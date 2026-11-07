"""Migration 0017: container, stack and service selectors hold a list of names."""

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import migrate
from app.subsystems.selectors import (
    ALL_KINDS,
    ContainerTarget,
    ServiceTarget,
    StackTarget,
    canonical,
    parse_selector_text,
)
from tests.db.backup_support import migrations_up_to
from tests.db.helpers import CREATED_STAMP

PREVIOUS_VERSION = 16
MIGRATION_VERSION = 17
UPDATER = "container-updater"
RESTARTER = "stack-restarter"
SYNCER = "backup-syncer"
GUARD_COLUMN = "every_name_selector_holds_a_list"
INSERT_ENVIRONMENT = (
    "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
    "VALUES ('e', 'unix_socket', '/run/e.sock', ?, ?)"
)
INSERT_RULE = (
    "INSERT INTO automation_rule (name, subsystem, environment_id, target_selector, parameters, "
    'created_at, updated_at) VALUES (?, ?, 1, ?, \'{"mode":"x"}\', ?, ?)'
)
OLD_SHAPES = {
    "container": (UPDATER, '{"kind":"container","name":"web"}'),
    "stack": (RESTARTER, '{"kind":"stack","name":"Shop"}'),
    "service": (UPDATER, '{"kind":"service","name":"web-1"}'),
}
UNTOUCHED = {
    "labels": (UPDATER, '{"kind":"labels","labels":[["app","web"],["tier",null]]}'),
    "environment": (UPDATER, '{"kind":"environment"}'),
    "empty": (SYNCER, "{}"),
    "not-json": (UPDATER, "not json"),
    "already-a-list": (UPDATER, '{"kind":"container","names":["db","web"]}'),
}
TAMPERED = {
    "name-and-names": '{"kind":"container","name":"web","names":["web"]}',
    "name-not-text": '{"kind":"stack","name":5}',
    "neither": '{"kind":"service"}',
}


def add_rules(connection: sqlite3.Connection, rules: dict[str, tuple[str, str]]) -> None:
    connection.execute(INSERT_ENVIRONMENT, (CREATED_STAMP, CREATED_STAMP))
    for name, (subsystem, selector) in rules.items():
        connection.execute(INSERT_RULE, (name, subsystem, selector, CREATED_STAMP, CREATED_STAMP))


def selector_of(connection: sqlite3.Connection, name: str) -> str:
    row = connection.execute(
        "SELECT target_selector FROM automation_rule WHERE name = ?", (name,)
    ).fetchone()
    return str(row[0])


@pytest.fixture(scope="module")
def before(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("before")
    path = root / "dockmin.db"
    connection = connect(path)
    try:
        migrate(connection, migrations_up_to(PREVIOUS_VERSION, root / "migrations"))
        add_rules(connection, OLD_SHAPES | UNTOUCHED)
    finally:
        connection.close()
    return path


@pytest.fixture(scope="module")
def upgraded(
    before: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[sqlite3.Connection]:
    root = tmp_path_factory.mktemp("after")
    path = root / "dockmin.db"
    shutil.copyfile(before, path)
    connection = connect(path)
    applied = migrate(connection, migrations_up_to(MIGRATION_VERSION, root / "migrations"))
    assert [migration.version for migration in applied] == [MIGRATION_VERSION]
    yield connection
    connection.close()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("container", ContainerTarget(names=("web",))),
        ("stack", StackTarget(names=("Shop",))),
        ("service", ServiceTarget(names=("web-1",))),
    ],
)
def test_a_single_name_selector_becomes_the_canonical_one_name_list(
    upgraded: sqlite3.Connection, name: str, expected: ContainerTarget | StackTarget | ServiceTarget
) -> None:
    stored = selector_of(upgraded, name)

    assert parse_selector_text(stored, ALL_KINDS) == expected
    assert stored == canonical(expected)


@pytest.mark.parametrize("name", list(UNTOUCHED))
def test_every_other_selector_is_left_exactly_as_it_was(
    upgraded: sqlite3.Connection, name: str
) -> None:
    assert selector_of(upgraded, name) == UNTOUCHED[name][1]


def test_a_rewritten_rule_keeps_its_name_subsystem_environment_and_parameters(
    upgraded: sqlite3.Connection,
) -> None:
    row = upgraded.execute(
        "SELECT name, subsystem, environment_id, parameters FROM automation_rule WHERE name = ?",
        ("container",),
    ).fetchone()

    assert tuple(row) == ("container", UPDATER, 1, '{"mode":"x"}')


def test_the_guard_table_is_not_left_behind(upgraded: sqlite3.Connection) -> None:
    left = upgraded.execute("SELECT 1 FROM sqlite_master WHERE name = 'migration_guard'").fetchone()

    assert left is None


@pytest.mark.parametrize("tampered", list(TAMPERED))
def test_a_row_the_rewrite_cannot_fix_aborts_the_migration_and_leaves_version_16(
    before: Path, tmp_path: Path, tampered: str
) -> None:
    path = tmp_path / "dockmin.db"
    shutil.copyfile(before, path)
    connection = connect(path)
    try:
        connection.execute(
            INSERT_RULE,
            ("bad", UPDATER, TAMPERED[tampered], CREATED_STAMP, CREATED_STAMP),
        )

        with pytest.raises(sqlite3.IntegrityError, match=GUARD_COLUMN):
            migrate(connection, migrations_up_to(MIGRATION_VERSION, tmp_path / "migrations"))

        version = connection.execute("SELECT max(version) FROM schema_migration").fetchone()[0]
        assert version == PREVIOUS_VERSION
        assert selector_of(connection, "container") == OLD_SHAPES["container"][1]
        assert selector_of(connection, "bad") == TAMPERED[tampered]
        assert not connection.in_transaction
    finally:
        connection.close()
