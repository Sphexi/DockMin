"""A populated instance to back up, and builders for archives and staged restores."""

import base64
import gzip
import hashlib
import io
import shutil
import sqlite3
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.backup import archive, paths, restore_steps
from app.backup.manifest import Manifest, MemberEntry, decode_manifest, encode_manifest
from app.crypto.registry import SECRET_FIELDS
from app.crypto.vault import Vault
from app.db.engine import Database, connect
from app.db.migrations import MIGRATIONS_DIR, migrate
from tests.crypto.helpers import (
    FIRST_KEY,
    SECOND_KEY,
    STAMP,
    CredentialRepository,
    seed_row,
    start,
)

NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
OPERATOR = "alice"
STANDARD_USER = "bob"
OLD_STACK = "c" * 32
NEW_STACK = "e" * 32
NEW_STACK_FILE = b"services:\n  web: {image: x}\n"
ARCHIVE_FILE_NAME = "dockmin-backup-20260304T050607Z.tar.gz"
PLACEHOLDER_MAC = "0" * 64
UNDECRYPTABLE = "v2:AAAAAAAAAAAAAAAAAAAAAAAA:BBBBBBBBBBBBBBBBBBBBBB=="
UNDECRYPTABLE_OLD = "v1:AAAAAAAAAAAAAAAAAAAAAAAA:BBBBBBBBBBBBBBBBBBBBBB=="
SEEDED_ACTIVITY_EVENTS = 3
KEY_VERSION_AFTER_ROTATION = 2


def clock() -> datetime:
    return NOW


def latest_version() -> int:
    return len(list(MIGRATIONS_DIR.glob("*.sql")))


@dataclass(slots=True)
class World:
    """A running instance's database and vault, and the data directory they live in."""

    database: Database
    vault: Vault
    data_dir: Path

    def run(self, sql: str, params: tuple[object, ...] = ()) -> None:
        connection = self.database.connect()
        try:
            connection.execute(sql, params)
        finally:
            connection.close()


def _migrated(path: Path, up_to: int | None) -> Database:
    directory = MIGRATIONS_DIR
    if up_to is not None:
        directory = path.parent / "old-migrations"
        directory.mkdir(exist_ok=True)
        for file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if int(file.name[:4]) <= up_to:
                shutil.copy(file, directory / file.name)
    connection = connect(path)
    migrate(connection, directory)
    connection.close()
    return Database(path)


async def make_world(
    base: Path,
    *,
    up_to: int | None = None,
    keys: tuple[bytes, bytes] = (FIRST_KEY, SECOND_KEY),
) -> World:
    """Build an instance with a secret in every registered column, rotated to a second key."""
    data_dir = base / "data"
    data_dir.mkdir(parents=True)
    database = _migrated(data_dir / "dockmin.db", up_to)
    first = await start(database, keys[0])
    async with database.session() as session:
        repository = CredentialRepository(session, first)
        rows: dict[str, int] = {}
        for field in SECRET_FIELDS:
            if field.table not in rows:
                rows[field.table] = await seed_row(session, field.table)
                await session.execute(
                    "UPDATE environment SET name = name || ?, socket_path = socket_path || ?",
                    (field.table, field.table),
                )
            assert await repository.seal(
                field, rows[field.table], f"plain-{field.table}-{field.column}"
            )
    vault = await start(database, keys[1], keys[0], allow_rotation=True)
    async with database.session() as session:
        await CredentialRepository(session, vault).add("second-credential", "plain-new")
    world = World(database, vault, data_dir)
    for username, role in ((OPERATOR, "administrator"), (STANDARD_USER, "standard")):
        world.run(
            "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
            "VALUES (?, 'hash', ?, 1, ?)",
            (username, role, STAMP),
        )
    world.run(
        "INSERT INTO session (user_id, token_hash, issued_at, expires_at) VALUES (1, 'h', ?, ?)",
        (STAMP, STAMP),
    )
    world.run(
        "INSERT INTO login_throttle (kind, subject, failure_count, window_started_at) "
        "VALUES ('username', ?, 1, ?)",
        (OPERATOR, STAMP),
    )
    world.run(
        "INSERT INTO stack (environment_id, name, kind, origin, project_directory, compose_file, "
        "compose_file_digest, created_at, updated_at) "
        "VALUES (1, 'stack-two', 'swarm', 'created', ?, ?, ?, ?, ?)",
        (NEW_STACK, NEW_STACK_FILE.decode(), "f" * 64, STAMP, STAMP),
    )
    for _ in range(SEEDED_ACTIVITY_EVENTS):
        world.run(
            "INSERT INTO activity_event (category, actor_type, actor_user_id, target_type, "
            "action, result, occurred_at) VALUES ('user_action', 'user', 1, 'x', 'seed', "
            "'success', ?)",
            (STAMP,),
        )
    (data_dir / "dockmin.lock").write_bytes(b"")
    return world


@dataclass(frozen=True, slots=True)
class Stored:
    """An archive written by the producer and what a restore reads out of it first."""

    path: Path
    manifest: Manifest
    raw_manifest: bytes
    sha256: str
    members: dict[str, bytes]


def read_members(path: Path) -> dict[str, tuple[tarfile.TarInfo, bytes]]:
    result: dict[str, tuple[tarfile.TarInfo, bytes]] = {}
    with tarfile.open(path, "r:gz") as tar:
        for info in tar:
            handle = tar.extractfile(info)
            assert handle is not None
            result[info.name] = (info, handle.read())
    return result


def tar_info(
    name: str, size: int, kind: bytes = tarfile.REGTYPE, link: str = ""
) -> tarfile.TarInfo:
    item = tarfile.TarInfo(name)
    item.size = size
    item.type = kind
    item.mode = 0o600
    item.linkname = link
    return item


def write_tar(
    path: Path, items: list[tuple[tarfile.TarInfo, bytes]], fmt: int = tarfile.PAX_FORMAT
) -> Path:
    with (
        gzip.open(path, "wb") as compressed,
        tarfile.open(fileobj=compressed, mode="w|", format=fmt) as tar,
    ):
        for info, data in items:
            tar.addfile(info, io.BytesIO(data))
    return path


def rebuilt(path: Path, stored: Stored) -> Path:
    """Write an archive holding exactly the members of stored, in the order the reader needs."""
    items = [(tar_info(name, len(data)), data) for name, data in stored.members.items()]
    return write_tar(path, items)


def sealed_manifest(
    template: Manifest, members: tuple[MemberEntry, ...], **changes: Any
) -> tuple[Manifest, bytes]:
    """Encode a manifest like template over other members; the signature is a placeholder."""
    fields = Manifest(
        1,
        int(changes.get("schema_version", template.schema_version)),
        template.created_at,
        template.product_version,
        template.instance_id,
        template.key_version,
        template.key_fingerprint,
        tuple(changes.get("administrators", template.administrators)),
        members,
        "",
    )
    raw = encode_manifest(fields, mac=lambda _payload: PLACEHOLDER_MAC)
    return decode_manifest(raw), raw


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def key_text(key: bytes) -> str:
    return base64.b64encode(key).decode()


def copy_tree(source: Path, target: Path) -> Path:
    shutil.copytree(source, target)
    return target


def stage(stored: Stored, staging: Path) -> Path:
    archive.stage_members(stored.path, stored.manifest, staging)
    return staging


def edit_staged_database(staging: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    connection = sqlite3.connect(staging / paths.STAGED_DATABASE)
    try:
        connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


async def prepare(staging: Path, operator: str = OPERATOR) -> restore_steps.StagedCheck:
    return await restore_steps.prepare_staged(staging, operator_username=operator)
