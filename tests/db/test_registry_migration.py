"""Migration 0008: the empty-table guard, the rebuilt credential table, its checks and grants."""

import shutil
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.migrations import MIGRATIONS_DIR, migrate
from app.db.repositories.registry_access import credential_usable_clause
from tests.db.helpers import CREATED_STAMP, PASSWORD_HASH

MIGRATION = "0008_registry_credentials.sql"
EARLIER = (
    "0001_initial.sql",
    "0002_json_column_checks.sql",
    "0003_authentication_state.sql",
    "0004_stack_key_case.sql",
    "0005_activity_event_indexes.sql",
    "0006_environments.sql",
    "0007_container_counts.sql",
)
CHECK_FAILED = "CHECK constraint failed"
LONG_NAME = "n" * 65
LONG_ADDRESS = "a" * 263
LONG_USERNAME = "u" * 257
LONG_BUNDLE = "b" * 65537

INSERT_CREDENTIAL = (
    "INSERT INTO registry_credential (name, provider, url, authentication_enabled, username, "
    "secret_kind, secret_ciphertext, derived_secret_ciphertext, derived_secret_expires_at, "
    "provider_extras, verify_tls, ca_bundle, created_at, updated_at) VALUES (:name, :provider, "
    ":url, :authentication_enabled, :username, :secret_kind, :secret_ciphertext, "
    ":derived_secret_ciphertext, :derived_secret_expires_at, :provider_extras, :verify_tls, "
    ":ca_bundle, :created_at, :updated_at)"
)
VALID_STATIC = {
    "name": "registry",
    "provider": "custom",
    "url": "registry.example.com",
    "authentication_enabled": 1,
    "username": "service",
    "secret_kind": "static",
    "secret_ciphertext": "ciphertext",
    "derived_secret_ciphertext": None,
    "derived_secret_expires_at": None,
    "provider_extras": "{}",
    "verify_tls": 1,
    "ca_bundle": None,
    "created_at": CREATED_STAMP,
    "updated_at": CREATED_STAMP,
}
VALID_ANONYMOUS = {
    **VALID_STATIC,
    "authentication_enabled": 0,
    "username": None,
    "secret_ciphertext": None,
}
VALID_DERIVED = {
    **VALID_STATIC,
    "provider": "ecr",
    "secret_kind": "derived",
    "derived_secret_ciphertext": "derived",
    "derived_secret_expires_at": CREATED_STAMP,
    "provider_extras": '{"region":"eu-west-2"}',
}
INSERT_ENVIRONMENT = (
    "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
    "VALUES ('box', 'unix_socket', '/var/run/docker.sock', ?, ?)"
)
INSERT_BINDING = (
    "INSERT INTO image_credential_binding (environment_id, image_reference, "
    "registry_credential_id, state, created_at, updated_at) VALUES (1, 'img:1', 1, 'bound', ?, ?)"
)
INSERT_CURRENT_BINDING = (
    "INSERT INTO image_credential_binding (environment_id, image_reference, "
    "registry_credential_id, state, bound_at, updated_at) VALUES (1, 'img:1', 1, 'bound', ?, ?)"
)
INSERT_USER = (
    "INSERT INTO user (id, username, password_hash, role, created_at) VALUES (1, 'u', ?, "
    "'standard', ?)"
)
INSERT_TEAM = "INSERT INTO team (id, name, created_at) VALUES (1, 'team', ?)"


@pytest.fixture
def before_registry(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    directory = tmp_path / "migrations"
    directory.mkdir()
    for name in EARLIER:
        shutil.copyfile(MIGRATIONS_DIR / name, directory / name)
    connection = connect(tmp_path / "old.db")
    migrate(connection, directory)
    yield connection, directory
    connection.close()


@pytest.fixture
def credential(raw_connection: sqlite3.Connection) -> sqlite3.Connection:
    """The migrated database holding one credential set, a user, a team and an environment."""
    raw_connection.execute(INSERT_CREDENTIAL, VALID_STATIC)
    raw_connection.execute(INSERT_USER, (PASSWORD_HASH, CREATED_STAMP))
    raw_connection.execute(INSERT_TEAM, (CREATED_STAMP,))
    raw_connection.execute(INSERT_ENVIRONMENT, (CREATED_STAMP, CREATED_STAMP))
    return raw_connection


def seed_environment_grant(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO registry_environment_access (registry_credential_id, environment_id) "
        "VALUES (1, 1)"
    )


def seed_binding(connection: sqlite3.Connection) -> None:
    connection.execute(INSERT_BINDING, (CREATED_STAMP, CREATED_STAMP))


@pytest.mark.parametrize("seed", [seed_environment_grant, seed_binding], ids=["grant", "binding"])
def test_the_migration_aborts_and_keeps_the_rows_when_a_registry_table_is_populated(
    before_registry: tuple[sqlite3.Connection, Path], seed: Callable[[sqlite3.Connection], None]
) -> None:
    connection, directory = before_registry
    connection.execute(INSERT_ENVIRONMENT, (CREATED_STAMP, CREATED_STAMP))
    connection.execute(
        "INSERT INTO registry_credential (name, provider, url, authentication_enabled, "
        "secret_kind, created_at, updated_at) VALUES ('old', 'custom', 'r.example', 0, 'static', "
        "?, ?)",
        (CREATED_STAMP, CREATED_STAMP),
    )
    seed(connection)
    shutil.copyfile(MIGRATIONS_DIR / MIGRATION, directory / MIGRATION)

    with pytest.raises(sqlite3.IntegrityError, match="registry_tables_are_empty"):
        migrate(connection, directory)

    assert connection.execute("SELECT name FROM registry_credential").fetchall()[0][0] == "old"
    assert connection.execute("SELECT max(version) FROM schema_migration").fetchone()[0] == len(
        EARLIER
    )


def test_the_migration_aborts_when_only_a_credential_set_exists(
    before_registry: tuple[sqlite3.Connection, Path],
) -> None:
    connection, directory = before_registry
    connection.execute(
        "INSERT INTO registry_credential (name, provider, url, authentication_enabled, "
        "secret_kind, created_at, updated_at) VALUES ('old', 'custom', 'r.example', 0, 'static', "
        "?, ?)",
        (CREATED_STAMP, CREATED_STAMP),
    )
    shutil.copyfile(MIGRATIONS_DIR / MIGRATION, directory / MIGRATION)

    with pytest.raises(sqlite3.IntegrityError, match="registry_tables_are_empty"):
        migrate(connection, directory)


def test_the_migration_applies_to_empty_registry_tables(
    before_registry: tuple[sqlite3.Connection, Path],
) -> None:
    connection, directory = before_registry
    shutil.copyfile(MIGRATIONS_DIR / MIGRATION, directory / MIGRATION)

    applied = migrate(connection, directory)

    assert [migration.filename for migration in applied] == [MIGRATION]
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("row", [VALID_STATIC, VALID_ANONYMOUS, VALID_DERIVED])
def test_each_shape_of_credential_set_the_product_stores_is_accepted(
    raw_connection: sqlite3.Connection, row: dict[str, object]
) -> None:
    raw_connection.execute(INSERT_CREDENTIAL, row)

    assert raw_connection.execute("SELECT count(*) FROM registry_credential").fetchone()[0] == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"name": LONG_NAME},
        {"url": ""},
        {"url": LONG_ADDRESS},
        {"username": ""},
        {"username": LONG_USERNAME},
        {"ca_bundle": ""},
        {"ca_bundle": LONG_BUNDLE},
        {"provider": "quay"},
        {"username": None},
        {"verify_tls": 0},
        {
            "verify_tls": 0,
            "authentication_enabled": 0,
            "username": None,
            "secret_ciphertext": None,
            "ca_bundle": "pem",
        },
        {"provider_extras": "[]"},
        {"provider_extras": '"text"'},
        {"provider_extras": "{not json"},
        {"secret_kind": "derived"},
        {"provider": "ecr"},
        {"authentication_enabled": 0},
        {**VALID_ANONYMOUS, "secret_kind": "derived", "provider": "ecr"},
    ],
    ids=[
        "empty-name",
        "long-name",
        "empty-address",
        "long-address",
        "empty-username",
        "long-username",
        "empty-bundle",
        "long-bundle",
        "unknown-provider",
        "authentication-without-username",
        "unverified-with-authentication",
        "bundle-without-verification",
        "extras-array",
        "extras-string",
        "extras-malformed",
        "derived-for-a-non-ecr-provider",
        "ecr-with-a-static-secret",
        "anonymous-with-credentials",
        "anonymous-derived",
    ],
)
def test_every_check_on_the_credential_table_refuses_its_case(
    raw_connection: sqlite3.Connection, overrides: dict[str, object]
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        raw_connection.execute(INSERT_CREDENTIAL, {**VALID_STATIC, **overrides})


def test_a_deleted_credential_id_is_never_issued_again(raw_connection: sqlite3.Connection) -> None:
    raw_connection.execute(INSERT_CREDENTIAL, VALID_STATIC)
    raw_connection.execute("DELETE FROM registry_credential WHERE id = 1")

    raw_connection.execute(INSERT_CREDENTIAL, VALID_STATIC)

    assert raw_connection.execute("SELECT id FROM registry_credential").fetchone()[0] == 2


@pytest.mark.parametrize("table", ["registry_environment_access", "image_credential_binding"])
def test_the_older_tables_still_reference_the_rebuilt_credential_table(
    raw_connection: sqlite3.Connection, table: str
) -> None:
    references = raw_connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()

    assert "registry_credential" in {row["table"] for row in references}
    assert raw_connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_credential_set_with_a_bound_image_cannot_be_deleted(
    credential: sqlite3.Connection,
) -> None:
    credential.execute(INSERT_CURRENT_BINDING, (CREATED_STAMP, CREATED_STAMP))

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        credential.execute("DELETE FROM registry_credential WHERE id = 1")


@pytest.mark.parametrize(
    ("table", "column", "owner_table"),
    [
        ("registry_credential_user_access", "user_id", "user"),
        ("registry_credential_team_access", "team_id", "team"),
    ],
)
def test_a_grant_to_a_user_or_team_is_unique_per_pair_and_needs_both_to_exist(
    credential: sqlite3.Connection, table: str, column: str, owner_table: str
) -> None:
    insert = f"INSERT INTO {table} (registry_credential_id, {column}) VALUES (?, ?)"
    credential.execute(insert, (1, 1))

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        credential.execute(insert, (1, 1))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        credential.execute(insert, (1, 99))


@pytest.mark.parametrize(
    "deleted",
    ["registry_credential", "user", "team"],
)
def test_deleting_a_credential_set_a_user_or_a_team_removes_the_grants_that_name_it(
    credential: sqlite3.Connection, deleted: str
) -> None:
    credential.execute(
        "INSERT INTO registry_credential_user_access (registry_credential_id, user_id) "
        "VALUES (1, 1)"
    )
    credential.execute(
        "INSERT INTO registry_credential_team_access (registry_credential_id, team_id) "
        "VALUES (1, 1)"
    )
    seed_environment_grant(credential)

    credential.execute(f'DELETE FROM "{deleted}" WHERE id = 1')

    remaining = {
        table: credential.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "registry_credential_user_access",
            "registry_credential_team_access",
            "registry_environment_access",
        )
    }
    assert (
        remaining
        == {
            "registry_credential": {
                "registry_credential_user_access": 0,
                "registry_credential_team_access": 0,
                "registry_environment_access": 0,
            },
            "user": {
                "registry_credential_user_access": 0,
                "registry_credential_team_access": 1,
                "registry_environment_access": 1,
            },
            "team": {
                "registry_credential_user_access": 1,
                "registry_credential_team_access": 0,
                "registry_environment_access": 1,
            },
        }[deleted]
    )


def plan_of(connection: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> str:
    rows = connection.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return " | ".join(str(row["detail"]) for row in rows)


def test_the_duplicate_lookup_searches_the_address_index(
    raw_connection: sqlite3.Connection,
) -> None:
    plan = plan_of(
        raw_connection,
        "SELECT id, name, authentication_enabled, username FROM registry_credential "
        "WHERE url = ? AND id <> ? ORDER BY name, id LIMIT ?",
        ("registry.example.com", -1, 50),
    )

    assert "idx_registry_credential_url" in plan
    assert "SCAN" not in plan


def test_the_offer_query_for_a_standard_user_scans_no_grant_table(
    raw_connection: sqlite3.Connection,
) -> None:
    fragment = credential_usable_clause(1, "rc.id")
    plan = plan_of(
        raw_connection,
        "SELECT rc.id FROM registry_credential rc JOIN registry_environment_access rea "
        f"ON rea.registry_credential_id = rc.id WHERE rea.environment_id = ? AND {fragment.clause} "
        "ORDER BY rc.name, rc.id LIMIT ?",
        (1, *fragment.params, 200),
    )

    assert "SCAN rea" not in plan
    assert "SCAN rua" not in plan
    assert "SCAN rta" not in plan
    assert "SCAN tm" not in plan
