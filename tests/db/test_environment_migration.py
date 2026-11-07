"""Migration 0006: the empty-table guard, the rebuilt environment table and its grant tables."""

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db.engine import Database, Session, connect
from app.db.migrations import MIGRATIONS_DIR, migrate
from app.db.repositories.user import UserRepository
from tests.db.helpers import CREATED_AT, CREATED_STAMP, PASSWORD_HASH, at
from tests.support import present

MIGRATION = "0006_environments.sql"
EARLIER = (
    "0001_initial.sql",
    "0002_json_column_checks.sql",
    "0003_authentication_state.sql",
    "0004_stack_key_case.sql",
    "0005_activity_event_indexes.sql",
)
INSERT_OLD_ENVIRONMENT = (
    "INSERT INTO environment (name, type, connection_kind, socket_path, created_at, updated_at) "
    "VALUES ('legacy', 'standalone', 'unix_socket', '/var/run/docker.sock', ?, ?)"
)
INSERT_ENVIRONMENT = (
    "INSERT INTO environment (name, connection_kind, socket_path, url, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
SOCKET = "/var/run/docker.sock"
URL = "tcp://docker.example.test:2376"


@pytest.fixture
def before_environments(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    directory = tmp_path / "migrations"
    directory.mkdir()
    for name in EARLIER:
        shutil.copyfile(MIGRATIONS_DIR / name, directory / name)
    connection = connect(tmp_path / "old.db")
    migrate(connection, directory)
    yield connection, directory
    connection.close()


def test_the_migration_aborts_without_touching_a_database_that_already_holds_an_environment(
    before_environments: tuple[sqlite3.Connection, Path],
) -> None:
    connection, directory = before_environments
    connection.execute(INSERT_OLD_ENVIRONMENT, (CREATED_STAMP, CREATED_STAMP))
    connection.execute("INSERT INTO tag (name, created_at) VALUES ('blue', ?)", (CREATED_STAMP,))
    connection.execute("INSERT INTO environment_tag (environment_id, tag_id) VALUES (1, 1)")
    shutil.copyfile(MIGRATIONS_DIR / MIGRATION, directory / MIGRATION)

    with pytest.raises(sqlite3.IntegrityError, match="environment_is_empty"):
        migrate(connection, directory)

    survivors = connection.execute("SELECT name FROM environment").fetchall()
    links = connection.execute("SELECT count(*) FROM environment_tag").fetchone()
    versions = connection.execute("SELECT max(version) FROM schema_migration").fetchone()
    assert [row[0] for row in survivors] == ["legacy"]
    assert links[0] == 1
    assert versions[0] == len(EARLIER)


def test_the_migration_applies_to_an_empty_environment_table(
    before_environments: tuple[sqlite3.Connection, Path],
) -> None:
    connection, directory = before_environments
    shutil.copyfile(MIGRATIONS_DIR / MIGRATION, directory / MIGRATION)

    applied = migrate(connection, directory)

    assert [migration.filename for migration in applied] == [MIGRATION]
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


async def insert(db: Session, name: str, *, kind: str = "unix_socket", **columns: str) -> int:
    path = columns.get("socket_path", SOCKET if kind == "unix_socket" else None)
    url = columns.get("url", URL if kind == "tcp_tls" else None)
    result = await db.execute(
        INSERT_ENVIRONMENT, (name, kind, path, url, CREATED_STAMP, CREATED_STAMP)
    )
    return present(result.last_row_id)


async def test_two_environments_cannot_share_an_endpoint_and_a_deleted_id_is_never_reissued(
    database: Database,
) -> None:
    async with database.session() as db:
        first = await insert(db, "first")
        with pytest.raises(sqlite3.IntegrityError):
            await insert(db, "second")
        await insert(db, "remote", kind="tcp_tls")
        with pytest.raises(sqlite3.IntegrityError):
            await insert(db, "another-remote", kind="tcp_tls")
        await db.execute("DELETE FROM environment WHERE id = ?", (first,))

        successor = await insert(db, "successor")

    assert successor > first


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE environment SET status = 'up' WHERE id = ?",
        "UPDATE environment SET failed_stage = 'tcp' WHERE id = ?",
        "UPDATE environment SET status = 'unreachable', failed_stage = 'dns2' WHERE id = ?",
        "UPDATE environment SET swarm_state = 'worker' WHERE id = ?",
        "UPDATE environment SET type = 'kubernetes' WHERE id = ?",
    ],
    ids=[
        "old status value",
        "stage on a status that did not fail",
        "unknown stage",
        "swarm state",
        "type",
    ],
)
async def test_the_checks_reject_values_the_service_never_writes(
    database: Database, statement: str
) -> None:
    async with database.session() as db:
        identifier = await insert(db, "host")

        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(statement, (identifier,))


async def test_deleting_an_environment_removes_every_dependent_row_and_detaches_its_history(
    database: Database,
) -> None:
    async with database.session() as db:
        host = await insert(db, "host")
        await db.execute(
            "INSERT INTO environment_group (name, created_at) VALUES ('group', ?)", (CREATED_STAMP,)
        )
        await UserRepository(db, at(CREATED_AT)).create("grantee", PASSWORD_HASH, "standard")
        await db.execute(
            "INSERT INTO environment_user_access (environment_id, user_id) VALUES (?, 1)", (host,)
        )
        await db.execute(
            "INSERT INTO environment_tls_material (environment_id, created_at, updated_at) "
            "VALUES (?, ?, ?)",
            (host, CREATED_STAMP, CREATED_STAMP),
        )
        await db.execute(
            "INSERT INTO environment_snapshot (environment_id, captured_at) VALUES (?, ?)",
            (host, CREATED_STAMP),
        )
        await db.execute(
            "INSERT INTO activity_event (category, actor_type, actor_user_id, environment_id, "
            "target_type, action, result, occurred_at) "
            "VALUES ('user_action', 'user', 1, ?, 'container', 'start', 'success', ?)",
            (host, CREATED_STAMP),
        )

        await db.execute("DELETE FROM environment WHERE id = ?", (host,))

        dependents = await db.fetch_limited(
            "SELECT (SELECT count(*) FROM environment_user_access) AS grants, "
            "(SELECT count(*) FROM environment_tls_material) AS tls, "
            "(SELECT count(*) FROM environment_snapshot) AS snapshots, "
            "(SELECT count(*) FROM activity_event WHERE environment_id IS NULL) AS detached, "
            "(SELECT count(*) FROM activity_event) AS events",
            (),
            limit=1,
        )
        check = await db.fetch_limited("PRAGMA foreign_key_check", (), limit=10)
    assert dict(dependents[0]) == {
        "grants": 0,
        "tls": 0,
        "snapshots": 0,
        "detached": 1,
        "events": 1,
    }
    assert check == []


async def test_deleting_a_group_or_a_user_removes_the_grants_that_named_them(
    database: Database,
) -> None:
    async with database.session() as db:
        host = await insert(db, "host")
        await db.execute(
            "INSERT INTO environment_group (name, created_at) VALUES ('group', ?)", (CREATED_STAMP,)
        )
        await UserRepository(db, at(CREATED_AT)).create("grantee", PASSWORD_HASH, "standard")
        await db.execute(
            "INSERT INTO environment_group_user_access (environment_group_id, user_id) "
            "VALUES (1, 1)"
        )
        await db.execute("UPDATE environment SET environment_group_id = 1 WHERE id = ?", (host,))

        await db.execute("DELETE FROM environment_group WHERE id = 1")

        remaining = await db.fetch_limited(
            "SELECT environment_group_id FROM environment WHERE id = ?", (host,), limit=1
        )
        grants = await db.fetch_limited(
            "SELECT id FROM environment_group_user_access", (), limit=10
        )
    assert remaining[0]["environment_group_id"] is None
    assert grants == []
