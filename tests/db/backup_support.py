"""A database at a given schema version holding a few of every row the backup code reads."""

import shutil
import sqlite3
from pathlib import Path

from app.db.engine import connect
from app.db.migrations import MIGRATIONS_DIR, migrate
from tests.db.helpers import CREATED_STAMP

OLD_VERSION = 14
NEWEST_ID = 3
TARGET_REFERENCE = '{"object_storage_target_id":1}'
INSERT_ACTIVITY = (
    "INSERT INTO activity_event (category, actor_type, actor_user_id, target_type, action, "
    "result, occurred_at) VALUES ('user_action', 'user', 1, 'x', 'y', 'success', ?)"
)


def latest_version() -> int:
    return len(list(MIGRATIONS_DIR.glob("*.sql")))


def migrations_up_to(version: int, directory: Path) -> Path:
    directory.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name[:4]) <= version:
            shutil.copy(path, directory / path.name)
    return directory


def seed(connection: sqlite3.Connection) -> None:
    """Insert users, a session, a throttle, two stacks, a target, records, rules and events."""
    run = connection.execute
    run(
        "INSERT INTO user (username, password_hash, role, is_enabled, created_at) VALUES "
        "('Admin', 'h', 'administrator', 1, ?), ('zed', 'h', 'administrator', 1, ?), "
        "('off', 'h', 'administrator', 0, ?), ('std', 'h', 'standard', 1, ?)",
        (CREATED_STAMP,) * 4,
    )
    run(
        "INSERT INTO session (user_id, token_hash, issued_at, expires_at) VALUES (1, 't', ?, ?)",
        (CREATED_STAMP, CREATED_STAMP),
    )
    run(
        "INSERT INTO login_throttle (kind, subject, failure_count, window_started_at) "
        "VALUES ('username', 'x', 1, ?)",
        (CREATED_STAMP,),
    )
    run(
        "INSERT INTO environment (name, type, connection_kind, socket_path, created_at, "
        "updated_at) VALUES ('local', 'standalone', 'unix_socket', '/var/run/docker.sock', ?, ?)",
        (CREATED_STAMP, CREATED_STAMP),
    )
    for directory in ("b" * 32, "a" * 32):
        run(
            "INSERT INTO stack (environment_id, name, kind, origin, project_directory, "
            "compose_file, compose_file_digest, created_at, updated_at) VALUES "
            "(1, ?, 'compose', 'created', ?, 'services: {}\n', ?, ?, ?)",
            (directory[:3], directory, "0" * 64, CREATED_STAMP, CREATED_STAMP),
        )
    run(
        "INSERT INTO object_storage_target (name, endpoint_url, region, bucket, access_key_id, "
        "secret_access_key_ciphertext, use_path_style, verify_tls, backup_prefix, "
        "activity_log_prefix, created_at, updated_at) VALUES ('t1', 'https://s3.example', "
        "'us-east-1', 'b', 'AK', 'v1:x', 1, 1, 'backups', 'logs', ?, ?)",
        (CREATED_STAMP, CREATED_STAMP),
    )
    run(
        "INSERT INTO backup_record (object_storage_target_id, created_at, size_bytes, "
        "key_version, key_fingerprint, destination_key, outcome) VALUES "
        "(1, ?, 10, 1, 'ab', 'k', 'success'), (NULL, ?, NULL, 1, 'ab', NULL, 'failure')",
        (CREATED_STAMP, CREATED_STAMP),
    )
    run(
        "INSERT INTO automation_rule (name, subsystem, target_selector, parameters, created_at, "
        "updated_at) VALUES ('nightly', 'backup-syncer', '{}', ?, ?, ?), "
        "('export', 'activity-log-exporter', '{}', ?, ?, ?), "
        "('other', 'backup-syncer', '{}', '{\"object_storage_target_id\":2}', ?, ?)",
        (TARGET_REFERENCE, CREATED_STAMP, CREATED_STAMP) * 2 + (CREATED_STAMP,) * 2,
    )
    for _ in range(NEWEST_ID):
        run(INSERT_ACTIVITY, (CREATED_STAMP,))
    run("DELETE FROM activity_event WHERE id = ?", (NEWEST_ID,))


def build(path: Path, *, up_to: int | None = None) -> Path:
    """Create a seeded database at path, migrated to up_to or to the newest schema."""
    connection = connect(path)
    try:
        directory = (
            MIGRATIONS_DIR
            if up_to is None
            else migrations_up_to(up_to, path.parent / f"migrations-{up_to}")
        )
        migrate(connection, directory)
        seed(connection)
    finally:
        connection.close()
    return path
