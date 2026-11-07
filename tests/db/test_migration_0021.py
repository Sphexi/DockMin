"""Migration 0021: notification channels, what each subscribes to, and the queue of deliveries."""

import itertools
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.db.engine import connect
from app.db.migrations import migrate
from app.db.repositories import backup_snapshot
from tests.db.backup_support import INSERT_ACTIVITY, build, latest_version
from tests.db.helpers import CREATED_STAMP

PREVIOUS_VERSION = 20
MIGRATION_VERSION = 21
KINDS = ["webhook", "ntfy", "gotify", "discord", "slack", "teams", "email"]
MAX_NAME = 100
MAX_ERROR = 512
MAX_EVENT_TYPE = 64
MAX_ATTEMPTS = 3
UNKNOWN_ID = 9999
NAMES = itertools.count(1)
COUNT_FOR_CHANNEL = "SELECT COUNT(*) FROM {table} WHERE channel_id = ?"
INSERT_SUBSCRIPTION = "INSERT INTO notification_subscription (channel_id, event_type) VALUES (?, ?)"
INSERT_SCOPE = (
    "INSERT INTO notification_channel_environment (channel_id, environment_id) VALUES (?, ?)"
)
INSERT_OUTBOX = (
    "INSERT INTO notification_outbox (channel_id, event_id, event_type, attempts, "
    "next_attempt_at, created_at) VALUES (?, ?, 'scheduled_job_disabled', ?, ?, ?)"
)
INSERT_ENVIRONMENT = (
    "INSERT INTO environment (name, type, connection_kind, socket_path, created_at, "
    "updated_at) VALUES (?, 'standalone', 'unix_socket', ?, ?, ?)"
)


def add_channel(connection: sqlite3.Connection, **columns: Any) -> int:
    """Insert a channel with a fresh name unless one is given and return its id."""
    values: dict[str, Any] = {
        "name": f"channel {next(NAMES)}",
        "kind": "webhook",
        "settings": "{}",
        "created_at": CREATED_STAMP,
        "updated_at": CREATED_STAMP,
    }
    values.update(columns)
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO notification_channel ({names}) VALUES ({marks})", tuple(values.values())
    )
    return int(cursor.lastrowid or 0)


def add_event(connection: sqlite3.Connection) -> int:
    return int(connection.execute(INSERT_ACTIVITY, (CREATED_STAMP,)).lastrowid or 0)


def add_environment(connection: sqlite3.Connection) -> int:
    name = f"environment-{next(NAMES)}"
    socket = f"/run/{name}.sock"
    cursor = connection.execute(INSERT_ENVIRONMENT, (name, socket, CREATED_STAMP, CREATED_STAMP))
    return int(cursor.lastrowid or 0)


def queue(
    connection: sqlite3.Connection, channel_id: int, event_id: int, attempts: int = 0
) -> None:
    connection.execute(
        INSERT_OUTBOX, (channel_id, event_id, attempts, CREATED_STAMP, CREATED_STAMP)
    )


def count(connection: sqlite3.Connection, table: str, channel_id: int) -> int:
    statement = COUNT_FOR_CHANNEL.format(table=table)
    return int(connection.execute(statement, (channel_id,)).fetchone()[0])


@pytest.fixture(scope="module")
def upgraded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    path = build(tmp_path_factory.mktemp("upgrade") / "dockmin.db", up_to=PREVIOUS_VERSION)
    connection = connect(path)
    events_before = connection.execute("SELECT COUNT(*) FROM activity_event").fetchone()[0]
    applied = [migration.version for migration in migrate(connection)]
    assert applied == list(range(MIGRATION_VERSION, latest_version() + 1))
    assert connection.execute("SELECT COUNT(*) FROM activity_event").fetchone()[0] == events_before
    yield connection
    connection.close()


def test_a_database_at_the_previous_schema_upgrades_with_every_notification_table_empty(
    upgraded: sqlite3.Connection,
) -> None:
    counts = [
        upgraded.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "notification_channel",
            "notification_subscription",
            "notification_channel_environment",
            "notification_outbox",
        )
    ]

    assert counts == [0, 0, 0, 0]
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


def test_a_channel_given_only_its_required_columns_is_enabled_for_all_environments_with_no_secret(
    upgraded: sqlite3.Connection,
) -> None:
    channel = add_channel(upgraded)

    row = upgraded.execute(
        "SELECT is_enabled, environment_scope, secrets_ciphertext, last_success_at, "
        "last_error_at, last_error FROM notification_channel WHERE id = ?",
        (channel,),
    ).fetchone()

    assert tuple(row) == (1, "all", None, None, None, None)


def test_a_sealed_secret_is_stored_as_text(upgraded: sqlite3.Connection) -> None:
    channel = add_channel(upgraded, secrets_ciphertext="v1:sealed")

    stored = upgraded.execute(
        "SELECT typeof(secrets_ciphertext) FROM notification_channel WHERE id = ?", (channel,)
    ).fetchone()[0]

    assert stored == "text"


@pytest.mark.parametrize("kind", KINDS)
def test_every_documented_channel_kind_is_stored(upgraded: sqlite3.Connection, kind: str) -> None:
    add_channel(upgraded, kind=kind)


@pytest.mark.parametrize(
    "columns",
    [
        {"kind": "sms"},
        {"kind": "Webhook"},
        {"name": ""},
        {"name": "n" * (MAX_NAME + 1)},
        {"settings": "{not json"},
        {"is_enabled": 2},
        {"environment_scope": "some"},
        {"last_error": "e" * (MAX_ERROR + 1)},
    ],
    ids=[
        "unknown-kind",
        "kind-case",
        "empty-name",
        "long-name",
        "settings-not-json",
        "enabled-flag",
        "scope",
        "long-error",
    ],
)
def test_a_channel_outside_its_constraints_is_refused(
    upgraded: sqlite3.Connection, columns: dict[str, Any]
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        add_channel(upgraded, **columns)


@pytest.mark.parametrize(
    "columns",
    [{"name": "n" * MAX_NAME}, {"last_error": "e" * MAX_ERROR}, {"environment_scope": "listed"}],
    ids=["longest-name", "longest-error", "listed-scope"],
)
def test_a_channel_at_the_edge_of_its_constraints_is_stored(
    upgraded: sqlite3.Connection, columns: dict[str, Any]
) -> None:
    add_channel(upgraded, **columns)


def test_channel_names_are_unique_without_regard_to_case(upgraded: sqlite3.Connection) -> None:
    add_channel(upgraded, name="Ops Alerts")

    with pytest.raises(sqlite3.IntegrityError):
        add_channel(upgraded, name="OPS alerts", kind="ntfy")


def test_a_channel_id_is_never_reissued_after_the_newest_channel_is_deleted(
    upgraded: sqlite3.Connection,
) -> None:
    newest = add_channel(upgraded)
    upgraded.execute("DELETE FROM notification_channel WHERE id = ?", (newest,))

    replacement = add_channel(upgraded)

    assert replacement > newest


def test_a_subscription_is_stored_once_per_channel_and_event_type(
    upgraded: sqlite3.Connection,
) -> None:
    channel = add_channel(upgraded)
    upgraded.execute(INSERT_SUBSCRIPTION, (channel, "automation_disabled"))
    upgraded.execute(INSERT_SUBSCRIPTION, (add_channel(upgraded), "automation_disabled"))

    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(INSERT_SUBSCRIPTION, (channel, "automation_disabled"))


@pytest.mark.parametrize(
    "event_type",
    ["", "Upper", "with-dash", "digit1", "with space", "e" * (MAX_EVENT_TYPE + 1)],
    ids=["empty", "upper-case", "dash", "digit", "space", "too-long"],
)
def test_an_event_type_that_is_not_lower_case_letters_and_underscores_is_refused(
    upgraded: sqlite3.Connection, event_type: str
) -> None:
    channel = add_channel(upgraded)

    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(INSERT_SUBSCRIPTION, (channel, event_type))


def test_an_event_type_of_the_longest_length_is_stored(upgraded: sqlite3.Connection) -> None:
    channel = add_channel(upgraded)

    upgraded.execute(INSERT_SUBSCRIPTION, (channel, "e" * MAX_EVENT_TYPE))


def test_a_subscription_to_a_channel_that_does_not_exist_is_refused(
    upgraded: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(INSERT_SUBSCRIPTION, (UNKNOWN_ID, "automation_disabled"))


def test_deleting_a_channel_deletes_its_subscriptions_scope_and_queue(
    upgraded: sqlite3.Connection,
) -> None:
    channel = add_channel(upgraded)
    kept = add_channel(upgraded)
    event = add_event(upgraded)
    upgraded.execute(INSERT_SUBSCRIPTION, (channel, "automation_disabled"))
    upgraded.execute(INSERT_SUBSCRIPTION, (kept, "automation_disabled"))
    upgraded.execute(INSERT_SCOPE, (channel, add_environment(upgraded)))
    queue(upgraded, channel, event)
    queue(upgraded, kept, event)

    upgraded.execute("DELETE FROM notification_channel WHERE id = ?", (channel,))

    tables = (
        "notification_subscription",
        "notification_channel_environment",
        "notification_outbox",
    )
    assert [count(upgraded, table, channel) for table in tables] == [0, 0, 0]
    assert count(upgraded, "notification_subscription", kept) == 1
    assert count(upgraded, "notification_outbox", kept) == 1


def test_a_channel_may_list_an_environment_once(upgraded: sqlite3.Connection) -> None:
    channel = add_channel(upgraded)
    environment = add_environment(upgraded)
    upgraded.execute(INSERT_SCOPE, (channel, environment))

    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(INSERT_SCOPE, (channel, environment))


def test_deleting_an_environment_removes_it_from_every_channels_list_and_keeps_the_channel(
    upgraded: sqlite3.Connection,
) -> None:
    channel = add_channel(upgraded, environment_scope="listed")
    environment = add_environment(upgraded)
    upgraded.execute(INSERT_SCOPE, (channel, environment))

    upgraded.execute("DELETE FROM environment WHERE id = ?", (environment,))

    scope = upgraded.execute(
        "SELECT environment_scope FROM notification_channel WHERE id = ?", (channel,)
    ).fetchone()[0]
    assert count(upgraded, "notification_channel_environment", channel) == 0
    assert scope == "listed"


@pytest.mark.parametrize("which", ["channel", "environment"])
def test_a_scope_entry_for_a_channel_or_environment_that_does_not_exist_is_refused(
    upgraded: sqlite3.Connection, which: str
) -> None:
    channel = add_channel(upgraded)
    parameters = (UNKNOWN_ID, 1) if which == "channel" else (channel, UNKNOWN_ID)

    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(INSERT_SCOPE, parameters)


def test_a_queued_delivery_starts_with_no_attempts(upgraded: sqlite3.Connection) -> None:
    channel = add_channel(upgraded)
    queue(upgraded, channel, add_event(upgraded))

    attempts = upgraded.execute(
        "SELECT attempts FROM notification_outbox WHERE channel_id = ?", (channel,)
    ).fetchone()[0]

    assert attempts == 0


def test_an_event_is_queued_once_per_channel_and_may_be_queued_for_several(
    upgraded: sqlite3.Connection,
) -> None:
    first, second = add_channel(upgraded), add_channel(upgraded)
    event = add_event(upgraded)
    queue(upgraded, first, event)
    queue(upgraded, second, event)

    with pytest.raises(sqlite3.IntegrityError):
        queue(upgraded, first, event)


@pytest.mark.parametrize("attempts", [-1, MAX_ATTEMPTS + 1], ids=["negative", "past-the-limit"])
def test_a_queued_delivery_outside_its_attempt_limit_is_refused(
    upgraded: sqlite3.Connection, attempts: int
) -> None:
    channel = add_channel(upgraded)

    with pytest.raises(sqlite3.IntegrityError):
        queue(upgraded, channel, add_event(upgraded), attempts)


def test_deleting_an_activity_event_deletes_the_deliveries_queued_for_it(
    upgraded: sqlite3.Connection,
) -> None:
    channel = add_channel(upgraded)
    other = add_channel(upgraded)
    event = add_event(upgraded)
    surviving = add_event(upgraded)
    queue(upgraded, channel, event)
    queue(upgraded, other, event)
    queue(upgraded, channel, surviving)

    upgraded.execute("DELETE FROM activity_event WHERE id = ?", (event,))

    assert count(upgraded, "notification_outbox", channel) == 1
    assert count(upgraded, "notification_outbox", other) == 0
    remaining = upgraded.execute(
        "SELECT event_id FROM notification_outbox WHERE channel_id = ?", (channel,)
    ).fetchone()[0]
    assert remaining == surviving


def test_a_delivery_queued_for_an_event_or_channel_that_does_not_exist_is_refused(
    upgraded: sqlite3.Connection,
) -> None:
    channel = add_channel(upgraded)
    event = add_event(upgraded)

    with pytest.raises(sqlite3.IntegrityError):
        queue(upgraded, channel, UNKNOWN_ID)
    with pytest.raises(sqlite3.IntegrityError):
        queue(upgraded, UNKNOWN_ID, event)


@pytest.mark.parametrize(
    ("index", "table"),
    [
        ("idx_notification_channel_name", "notification_channel"),
        ("idx_notification_channel_environment_environment_id", "notification_channel_environment"),
        ("idx_notification_outbox_due", "notification_outbox"),
        ("idx_notification_outbox_event_id", "notification_outbox"),
    ],
)
def test_the_lookup_indexes_exist(upgraded: sqlite3.Connection, index: str, table: str) -> None:
    owner = upgraded.execute(
        "SELECT tbl_name FROM sqlite_master WHERE name = ?", (index,)
    ).fetchone()

    assert owner[0] == table
