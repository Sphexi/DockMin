"""Migration 0009: the empty-table guard, the rebuilt binding table and the default index."""

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db.engine import Database, Session, connect
from app.db.migrations import MIGRATIONS_DIR, migrate
from app.db.repositories.registry_access import RegistryAccessRepository
from tests.db.helpers import CREATED_STAMP, LATER_STAMP

MIGRATION = "0009_image_bindings.sql"
EARLIER = (
    "0001_initial.sql",
    "0002_json_column_checks.sql",
    "0003_authentication_state.sql",
    "0004_stack_key_case.sql",
    "0005_activity_event_indexes.sql",
    "0006_environments.sql",
    "0007_container_counts.sql",
    "0008_registry_credentials.sql",
)
CHECK_FAILED = "CHECK constraint failed"
DIGEST = "sha256:" + "a" * 64
REFERENCE = "registry.example.com/app:1"

INSERT_ENVIRONMENT = (
    "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
    "VALUES (?, 'unix_socket', ?, ?, ?)"
)
INSERT_CREDENTIAL = (
    "INSERT INTO registry_credential (name, provider, url, authentication_enabled, secret_kind, "
    "is_default_for_host, created_at, updated_at) "
    "VALUES (?, 'custom', ?, 0, 'static', ?, ?, ?)"
)
INSERT_BINDING = (
    "INSERT INTO image_credential_binding (environment_id, image_reference, "
    "registry_credential_id, missing_credential_id, missing_credential_name, missing_reason, "
    "observed_digest, state, bound_at, updated_at, credential_missing_at) "
    "VALUES (:environment_id, :image_reference, :registry_credential_id, :missing_credential_id, "
    ":missing_credential_name, :missing_reason, :observed_digest, :state, :bound_at, :updated_at, "
    ":credential_missing_at)"
)

BOUND = {
    "environment_id": 1,
    "image_reference": REFERENCE,
    "registry_credential_id": 1,
    "missing_credential_id": None,
    "missing_credential_name": None,
    "missing_reason": None,
    "observed_digest": None,
    "state": "bound",
    "bound_at": CREATED_STAMP,
    "updated_at": CREATED_STAMP,
    "credential_missing_at": None,
}
ANONYMOUS = {**BOUND, "registry_credential_id": None}
MISSING = {
    **BOUND,
    "registry_credential_id": None,
    "missing_credential_id": 1,
    "missing_credential_name": "registry",
    "missing_reason": "deleted",
    "state": "credential_missing",
    "credential_missing_at": CREATED_STAMP,
}
REFUSED = [
    pytest.param({"state": "unknown"}, id="unknown-state"),
    pytest.param({"image_reference": ""}, id="empty-reference"),
    pytest.param({"image_reference": "r" * 513}, id="long-reference"),
    pytest.param({"observed_digest": "sha256:" + "a" * 63}, id="short-digest"),
    pytest.param({"observed_digest": "sha256:" + "A" * 64}, id="upper-case-digest"),
    pytest.param({"observed_digest": "md5:" + "a" * 67}, id="other-algorithm"),
    pytest.param({"missing_credential_id": 1}, id="bound-with-a-lost-set-id"),
    pytest.param({"missing_credential_name": "x"}, id="bound-with-a-lost-set-name"),
    pytest.param({"missing_reason": "deleted"}, id="bound-with-a-reason"),
    pytest.param({"credential_missing_at": CREATED_STAMP}, id="bound-with-a-missing-time"),
    pytest.param({**MISSING, "registry_credential_id": 1}, id="missing-and-still-bound-to-a-set"),
    pytest.param({**MISSING, "missing_credential_id": None}, id="missing-without-the-lost-id"),
    pytest.param({**MISSING, "missing_credential_name": None}, id="missing-without-a-name"),
    pytest.param({**MISSING, "missing_credential_name": ""}, id="missing-with-an-empty-name"),
    pytest.param({**MISSING, "missing_credential_name": "n" * 65}, id="missing-with-a-long-name"),
    pytest.param({**MISSING, "missing_reason": None}, id="missing-without-a-reason"),
    pytest.param({**MISSING, "missing_reason": "removed"}, id="missing-with-an-unknown-reason"),
    pytest.param({**MISSING, "credential_missing_at": None}, id="missing-without-a-time"),
]


@pytest.fixture
def before_binding(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    directory = tmp_path / "migrations"
    directory.mkdir()
    for name in EARLIER:
        shutil.copyfile(MIGRATIONS_DIR / name, directory / name)
    connection = connect(tmp_path / "old.db")
    migrate(connection, directory)
    yield connection, directory
    connection.close()


@pytest.fixture
def seeded(raw_connection: sqlite3.Connection) -> sqlite3.Connection:
    """The migrated database holding two environments and two credential sets at one address."""
    for name in ("one", "two"):
        raw_connection.execute(
            INSERT_ENVIRONMENT, (name, f"/run/{name}.sock", CREATED_STAMP, CREATED_STAMP)
        )
    for name in ("registry", "second"):
        raw_connection.execute(
            INSERT_CREDENTIAL, (name, "registry.example.com", 0, CREATED_STAMP, CREATED_STAMP)
        )
    return raw_connection


def test_the_migration_aborts_and_keeps_the_row_when_a_binding_exists(
    before_binding: tuple[sqlite3.Connection, Path],
) -> None:
    connection, directory = before_binding
    connection.execute(INSERT_ENVIRONMENT, ("box", "/run/box.sock", CREATED_STAMP, CREATED_STAMP))
    connection.execute(
        "INSERT INTO image_credential_binding (environment_id, image_reference, state, "
        "created_at, updated_at) VALUES (1, 'img:1', 'bound', ?, ?)",
        (CREATED_STAMP, CREATED_STAMP),
    )
    shutil.copyfile(MIGRATIONS_DIR / MIGRATION, directory / MIGRATION)

    with pytest.raises(sqlite3.IntegrityError, match="binding_table_is_empty"):
        migrate(connection, directory)

    assert (
        connection.execute("SELECT image_reference FROM image_credential_binding").fetchall()[0][0]
        == "img:1"
    )
    assert connection.execute("SELECT max(version) FROM schema_migration").fetchone()[0] == len(
        EARLIER
    )


def test_the_migration_applies_to_an_empty_binding_table_and_leaves_the_foreign_keys_intact(
    before_binding: tuple[sqlite3.Connection, Path],
) -> None:
    connection, directory = before_binding
    shutil.copyfile(MIGRATIONS_DIR / MIGRATION, directory / MIGRATION)

    applied = migrate(connection, directory)

    references = {
        row["table"]: row["on_delete"]
        for row in connection.execute("PRAGMA foreign_key_list(image_credential_binding)")
    }
    assert [migration.filename for migration in applied] == [MIGRATION]
    assert references == {"environment": "CASCADE", "registry_credential": "RESTRICT"}
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("row", [BOUND, ANONYMOUS, MISSING], ids=["bound", "anonymous", "missing"])
def test_each_of_the_three_binding_cases_is_accepted(
    seeded: sqlite3.Connection, row: dict[str, object]
) -> None:
    seeded.execute(INSERT_BINDING, {**row, "observed_digest": DIGEST})

    assert seeded.execute("SELECT count(*) FROM image_credential_binding").fetchone()[0] == 1


@pytest.mark.parametrize("overrides", REFUSED)
def test_every_check_on_the_binding_table_refuses_its_case(
    seeded: sqlite3.Connection, overrides: dict[str, object]
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        seeded.execute(INSERT_BINDING, {**BOUND, **overrides})


def test_a_reference_is_bound_once_per_environment_and_a_second_environment_has_its_own(
    seeded: sqlite3.Connection,
) -> None:
    seeded.execute(INSERT_BINDING, BOUND)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        seeded.execute(INSERT_BINDING, BOUND)
    seeded.execute(INSERT_BINDING, {**BOUND, "environment_id": 2})
    assert seeded.execute("SELECT count(*) FROM image_credential_binding").fetchone()[0] == 2


def test_a_set_with_a_bound_row_is_refused_until_the_row_moves_to_credential_missing(
    seeded: sqlite3.Connection,
) -> None:
    seeded.execute(INSERT_BINDING, BOUND)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        seeded.execute("DELETE FROM registry_credential WHERE id = 1")
    seeded.execute(
        "UPDATE image_credential_binding SET state = 'credential_missing', "
        "registry_credential_id = NULL, missing_credential_id = 1, "
        "missing_credential_name = 'registry', missing_reason = 'deleted', "
        "credential_missing_at = ?, updated_at = ? WHERE registry_credential_id = 1",
        (LATER_STAMP, LATER_STAMP),
    )
    seeded.execute("DELETE FROM registry_credential WHERE id = 1")

    assert seeded.execute(
        "SELECT state, missing_credential_id FROM image_credential_binding"
    ).fetchone()[:] == ("credential_missing", 1)


def test_a_deleted_binding_id_is_never_issued_again_and_an_environment_delete_cascades(
    seeded: sqlite3.Connection,
) -> None:
    seeded.execute(INSERT_BINDING, BOUND)
    seeded.execute("DELETE FROM image_credential_binding WHERE id = 1")

    seeded.execute(INSERT_BINDING, BOUND)
    seeded.execute("DELETE FROM environment WHERE id = 1")

    assert seeded.execute("SELECT id FROM image_credential_binding").fetchall() == []
    seeded.execute(INSERT_BINDING, {**BOUND, "environment_id": 2})
    assert seeded.execute("SELECT id FROM image_credential_binding").fetchone()[0] == 3


def test_one_default_per_address_is_stored_and_a_second_address_has_its_own(
    seeded: sqlite3.Connection,
) -> None:
    seeded.execute("UPDATE registry_credential SET is_default_for_host = 1 WHERE id = 1")

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        seeded.execute("UPDATE registry_credential SET is_default_for_host = 1 WHERE id = 2")
    seeded.execute("UPDATE registry_credential SET url = 'mirror.example.com' WHERE id = 2")
    seeded.execute("UPDATE registry_credential SET is_default_for_host = 1 WHERE id = 2")
    assert (
        seeded.execute(
            "SELECT count(*) FROM registry_credential WHERE is_default_for_host = 1"
        ).fetchone()[0]
        == 2
    )


@pytest.mark.parametrize(
    ("query", "parameters", "index"),
    [
        pytest.param(
            "SELECT id FROM image_credential_binding WHERE environment_id = ? "
            "AND image_reference = ?",
            (1, REFERENCE),
            "sqlite_autoindex_image_credential_binding_1",
            id="by-environment-and-reference",
        ),
        pytest.param(
            "SELECT b.id, e.name, b.image_reference FROM environment e CROSS JOIN "
            "image_credential_binding b ON b.environment_id = e.id "
            "AND b.registry_credential_id = ? "
            "ORDER BY e.name, b.image_reference LIMIT 501",
            (1,),
            "idx_image_credential_binding_registry_credential_id",
            id="affected-by-a-set",
        ),
        pytest.param(
            "SELECT id FROM registry_credential WHERE url = ? AND is_default_for_host = 1",
            ("registry.example.com",),
            "idx_registry_credential_default_for_host",
            id="default-for-an-address",
        ),
    ],
)
def test_the_lookups_the_services_make_use_an_index(
    seeded: sqlite3.Connection, query: str, parameters: tuple[object, ...], index: str
) -> None:
    plan = " ".join(
        row["detail"] for row in seeded.execute(f"EXPLAIN QUERY PLAN {query}", parameters)
    )

    assert index in plan
    assert "SCAN image_credential_binding" not in plan


async def test_the_candidate_lookup_by_address_searches_the_address_index(
    database: Database, session: Session
) -> None:
    statements: list[str] = []
    session._connection.set_trace_callback(statements.append)
    await RegistryAccessRepository(session).eligible_for_pull(
        1, user_id=None, url="registry.example.com"
    )
    session._connection.set_trace_callback(None)
    select = next(s for s in statements if s.lstrip().startswith("SELECT rc.id"))
    connection = database.connect()
    try:
        plan = " ".join(
            row["detail"] for row in connection.execute(f"EXPLAIN QUERY PLAN {select}").fetchall()
        )
    finally:
        connection.close()

    assert "idx_registry_credential_url" in plan
    assert "SCAN rc" not in plan
