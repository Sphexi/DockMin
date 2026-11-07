"""Tests for the CHECK constraints on activity_event, environment and registry_credential."""

import sqlite3

import pytest

from tests.db.helpers import ACTIVITY_INSERT, CREATED_STAMP, PASSWORD_HASH, activity_row

USER_ID = 7
SUBSYSTEM = "container-updater"
EXPIRES_STAMP = "2026-01-02T05:04:05Z"
CHECK_FAILED = "CHECK constraint failed"
MAX_REQUEST_ID = 64
MAX_HOST_NAME = 255
SET_REQUEST_ID = "UPDATE activity_event SET request_id = ?"
INSERT_ENVIRONMENT_HOST = (
    "INSERT INTO environment (name, connection_kind, socket_path, engine_host_name, created_at, "
    "updated_at) VALUES ('local', 'unix_socket', '/var/run/docker.sock', ?, ?, ?)"
)
PERMITTED_SUBSYSTEMS = [
    "container-updater",
    "container-health-checker",
    "stack-restarter",
    "backup-syncer",
    "activity-log-exporter",
    "activity-log-pruner",
    "environment-snapshot-refresher",
    "webhook-receiver",
]

VALID_ACTORS = [
    pytest.param("user", USER_ID, None, id="user"),
    pytest.param("subsystem", None, SUBSYSTEM, id="subsystem"),
    pytest.param("anonymous", None, None, id="anonymous"),
]
INVALID_ACTORS = [
    pytest.param("user", USER_ID, SUBSYSTEM, id="user-with-both-identities"),
    pytest.param("user", None, SUBSYSTEM, id="user-with-a-subsystem-only"),
    pytest.param("user", None, None, id="user-with-no-identity"),
    pytest.param("subsystem", USER_ID, SUBSYSTEM, id="subsystem-with-both-identities"),
    pytest.param("subsystem", USER_ID, None, id="subsystem-with-a-user-id-only"),
    pytest.param("subsystem", None, None, id="subsystem-with-no-identity"),
    pytest.param("anonymous", USER_ID, SUBSYSTEM, id="anonymous-with-both-identities"),
    pytest.param("anonymous", USER_ID, None, id="anonymous-with-a-user-id"),
    pytest.param("anonymous", None, SUBSYSTEM, id="anonymous-with-a-subsystem"),
    pytest.param("robot", None, None, id="unrecognised-actor-type"),
]

CREDENTIAL_INSERT = (
    "INSERT INTO registry_credential (name, provider, url, authentication_enabled, username, "
    "secret_kind, secret_ciphertext, derived_secret_ciphertext, derived_secret_expires_at, "
    "derived_secret_refreshed_at, created_at, updated_at) VALUES (:name, :provider, :url, "
    ":authentication_enabled, :username, :secret_kind, :secret_ciphertext, "
    ":derived_secret_ciphertext, :derived_secret_expires_at, :derived_secret_refreshed_at, "
    ":created_at, :updated_at)"
)
STATIC_CREDENTIAL = {
    "name": "registry",
    "provider": "custom",
    "url": "https://registry.example",
    "authentication_enabled": 1,
    "username": "service",
    "secret_kind": "static",
    "secret_ciphertext": "static-ciphertext",
    "derived_secret_ciphertext": None,
    "derived_secret_expires_at": None,
    "derived_secret_refreshed_at": None,
    "created_at": CREATED_STAMP,
    "updated_at": CREATED_STAMP,
}
DERIVED_SECRET = {
    "provider": "ecr",
    "secret_kind": "derived",
    "derived_secret_ciphertext": "derived-ciphertext",
    "derived_secret_expires_at": EXPIRES_STAMP,
    "derived_secret_refreshed_at": CREATED_STAMP,
}
INVALID_CREDENTIALS = [
    pytest.param(
        {**DERIVED_SECRET, "derived_secret_expires_at": None}, id="derived-secret-without-expiry"
    ),
    pytest.param(
        {**DERIVED_SECRET, "derived_secret_ciphertext": None}, id="expiry-without-derived-secret"
    ),
    pytest.param(
        {**DERIVED_SECRET, "provider": "custom", "secret_kind": "static"},
        id="derived-columns-on-static-kind",
    ),
    pytest.param({"authentication_enabled": 0}, id="anonymous-set-still-carrying-credentials"),
]


@pytest.fixture
def schema(raw_connection: sqlite3.Connection) -> sqlite3.Connection:
    """Return a migrated connection holding one user, the target of user-actor events."""
    raw_connection.execute(
        "INSERT INTO user (id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
        (USER_ID, "operator", PASSWORD_HASH, "standard", CREATED_STAMP),
    )
    return raw_connection


def insert_credential(connection: sqlite3.Connection, **overrides: object) -> None:
    connection.execute(CREDENTIAL_INSERT, {**STATIC_CREDENTIAL, **overrides})


@pytest.mark.parametrize(("actor_type", "user_id", "subsystem"), VALID_ACTORS)
def test_activity_event_accepts_each_actor_type_with_its_own_identity(
    schema: sqlite3.Connection, actor_type: str, user_id: int | None, subsystem: str | None
) -> None:
    schema.execute(ACTIVITY_INSERT, activity_row(actor_type, user_id, subsystem))

    stored = schema.execute("SELECT actor_type FROM activity_event").fetchone()[0]
    assert stored == actor_type


@pytest.mark.parametrize(("actor_type", "user_id", "subsystem"), INVALID_ACTORS)
def test_activity_event_rejects_an_actor_type_that_disagrees_with_its_identity(
    schema: sqlite3.Connection, actor_type: str, user_id: int | None, subsystem: str | None
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        schema.execute(ACTIVITY_INSERT, activity_row(actor_type, user_id, subsystem))


@pytest.mark.parametrize("subsystem", PERMITTED_SUBSYSTEMS)
def test_activity_event_accepts_each_permitted_subsystem_name(
    schema: sqlite3.Connection, subsystem: str
) -> None:
    schema.execute(ACTIVITY_INSERT, activity_row("subsystem", None, subsystem))

    stored = schema.execute("SELECT actor_subsystem FROM activity_event").fetchone()[0]
    assert stored == subsystem


@pytest.mark.parametrize("subsystem", ["unknown-subsystem", "container_updater"])
def test_activity_event_rejects_a_subsystem_name_outside_the_permitted_names(
    schema: sqlite3.Connection, subsystem: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        schema.execute(ACTIVITY_INSERT, activity_row("subsystem", None, subsystem))


def test_registry_credential_accepts_static_and_derived_secrets_with_expiry_and_refresh(
    schema: sqlite3.Connection,
) -> None:
    insert_credential(schema, **DERIVED_SECRET)

    stored = schema.execute(
        "SELECT secret_ciphertext, derived_secret_ciphertext, derived_secret_expires_at, "
        "derived_secret_refreshed_at FROM registry_credential"
    ).fetchone()
    assert tuple(stored) == (
        STATIC_CREDENTIAL["secret_ciphertext"],
        DERIVED_SECRET["derived_secret_ciphertext"],
        EXPIRES_STAMP,
        CREATED_STAMP,
    )


def test_registry_credential_accepts_an_anonymous_set(schema: sqlite3.Connection) -> None:
    insert_credential(schema, authentication_enabled=0, username=None, secret_ciphertext=None)

    stored = schema.execute("SELECT authentication_enabled FROM registry_credential").fetchone()[0]
    assert stored == 0


@pytest.mark.parametrize("overrides", INVALID_CREDENTIALS)
def test_registry_credential_rejects_an_inconsistent_secret_shape(
    schema: sqlite3.Connection, overrides: dict[str, object]
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        insert_credential(schema, **overrides)


@pytest.mark.parametrize(
    "request_id",
    [None, "a", "0123456789abcdef.-_ABC", "x" * MAX_REQUEST_ID],
)
def test_activity_event_accepts_a_request_id_of_the_logged_shape_or_none(
    schema: sqlite3.Connection, request_id: str | None
) -> None:
    schema.execute(ACTIVITY_INSERT, activity_row("anonymous", None, None))

    schema.execute(SET_REQUEST_ID, (request_id,))

    assert schema.execute("SELECT request_id FROM activity_event").fetchone()[0] == request_id


@pytest.mark.parametrize("request_id", ["", "x" * (MAX_REQUEST_ID + 1), "has space", "a/b", "é"])
def test_activity_event_rejects_a_request_id_outside_the_logged_shape(
    schema: sqlite3.Connection, request_id: str
) -> None:
    schema.execute(ACTIVITY_INSERT, activity_row("anonymous", None, None))

    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        schema.execute(SET_REQUEST_ID, (request_id,))


@pytest.mark.parametrize("host_name", [None, "h", "h" * MAX_HOST_NAME])
def test_environment_accepts_an_engine_host_name_of_one_to_255_characters_or_none(
    schema: sqlite3.Connection, host_name: str | None
) -> None:
    schema.execute(INSERT_ENVIRONMENT_HOST, (host_name, CREATED_STAMP, CREATED_STAMP))

    assert schema.execute("SELECT engine_host_name FROM environment").fetchone()[0] == host_name


@pytest.mark.parametrize("host_name", ["", "h" * (MAX_HOST_NAME + 1)])
def test_environment_rejects_an_empty_or_overlong_engine_host_name(
    schema: sqlite3.Connection, host_name: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        schema.execute(INSERT_ENVIRONMENT_HOST, (host_name, CREATED_STAMP, CREATED_STAMP))
