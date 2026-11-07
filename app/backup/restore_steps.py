"""The restore steps that act on a staged database: key check, migration, decrypt test, finalise."""

import asyncio
import base64
import binascii
import hmac
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from app.backup import paths
from app.backup.manifest import (
    MAX_STACK_MEMBERS,
    ArchiveRefused,
    Manifest,
    manifest_mac_matches,
)
from app.config import KEY_BYTES
from app.crypto import envelope
from app.crypto.envelope import MAX_KEY_VERSION, DecryptionError, SecretLocation
from app.crypto.keys import Keyring, key_fingerprint
from app.crypto.startup import CANARY_PLAINTEXT
from app.crypto.vault import Vault
from app.db.engine import Database, Session, connect
from app.db.migrations.runner import MigrationError, load_migrations, migrate
from app.db.repositories import backup_snapshot
from app.db.repositories.activity_event import (
    DetailValue,
    activity_sequence,
    raise_activity_sequence,
)
from app.db.repositories.clocked import Clock
from app.db.repositories.crypto_state import CryptoState, CryptoStateRepository, canary_location
from app.db.repositories.rekey import RekeyFailure, RekeyPass
from app.db.repositories.setting import RESTORE_AUTOMATION_PAUSED_KEY, SettingRepository
from app.db.sql_text import normalise_sql
from app.logging import get_logger
from app.services.activity import ActivityRecorder, UserActor

type RestoreSource = Literal["upload", "object_storage", "operator_command"]

ACTIVITY_ID_GAP: Final = 1_000_000_000
MAX_FINDINGS: Final = 10

_HEX_DIRECTORY = re.compile(r"[0-9a-f]{32}")
_COMPOSE_FILE = "compose.yaml"

logger = get_logger(__name__)


def check_key(manifest: Manifest, raw_manifest: bytes, key_text: str) -> bytearray:
    """Return the key in key_text after checking it against the manifest's fingerprint and MAC.

    The caller owns the returned bytearray and overwrites it when done. Nothing is staged or
    changed by this check.
    """
    text = key_text.strip()
    if not text:
        raise ArchiveRefused("key_missing", "Enter the encryption key to continue.")
    try:
        key = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != KEY_BYTES:
        raise ArchiveRefused(
            "key_invalid", "The key must be the base64 text of exactly 32 bytes, as configured."
        )
    if not hmac.compare_digest(key_fingerprint(key), manifest.key_fingerprint):
        raise ArchiveRefused(
            "fingerprint_mismatch",
            "This key does not match the archive. It was made with key version "
            f"{manifest.key_version}, fingerprint {manifest.key_fingerprint}.",
        )
    if not manifest_mac_matches(raw_manifest, key):
        raise ArchiveRefused(
            "manifest_tampered",
            "The archive manifest does not carry a valid signature for this key.",
        )
    return bytearray(key)


@dataclass(frozen=True, slots=True)
class StagedCheck:
    """What preparing the staged database established."""

    migrated_from: int
    schema_version: int
    operator_user_id: int


def _database_invalid(message: str) -> ArchiveRefused:
    return ArchiveRefused("database_invalid", message)


def _schema_mismatch(names: list[str]) -> ArchiveRefused:
    logger.error("restore_schema_mismatch", extra={"objects": ", ".join(sorted(names)[:10])})
    return ArchiveRefused(
        "schema_mismatch", "The archive's database does not have the expected schema."
    )


def _schema(connection: sqlite3.Connection) -> list[backup_snapshot.SchemaObject]:
    objects: list[backup_snapshot.SchemaObject] = []
    for kind, name, table, sql in backup_snapshot.schema_objects(connection):
        value: object = sql
        if value is None:
            objects.append((kind, name, table, None))
        elif isinstance(value, str) and "\x00" not in value:
            objects.append((kind, name, table, normalise_sql(value)))
        else:
            raise _schema_mismatch([name])
    return objects


def _fresh_schema() -> list[backup_snapshot.SchemaObject]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        migrate(connection)
        return _schema(connection)
    finally:
        connection.close()


def _migrate_staged(connection: sqlite3.Connection) -> tuple[int, int]:
    archive_version = backup_snapshot.schema_version(connection)
    if archive_version < 1:
        raise _database_invalid("The archive's database is not a dockmin database.")
    supported = load_migrations()[-1].version
    if archive_version > supported:
        raise ArchiveRefused(
            "schema_newer",
            f"The archive's schema version is {archive_version}; "
            f"this build supports up to {supported}.",
        )
    try:
        migrate(connection)
    except MigrationError as error:
        raise ArchiveRefused("migration_failed", str(error)) from None
    except sqlite3.Error:
        raise ArchiveRefused(
            "migration_failed", "The staged database could not be migrated."
        ) from None
    return archive_version, backup_snapshot.schema_version(connection)


def _check_integrity(connection: sqlite3.Connection) -> None:
    findings = backup_snapshot.integrity_findings(connection, limit=MAX_FINDINGS)
    if findings:
        logger.error("restore_database_findings", extra={"findings": "; ".join(findings)})
        raise _database_invalid(
            f"The restored database failed its integrity checks ({len(findings)} findings)."
        )


def _check_schema(connection: sqlite3.Connection) -> None:
    expected = _fresh_schema()
    actual = _schema(connection)
    if actual != expected:
        raise _schema_mismatch([item[1] for item in set(actual) ^ set(expected)])


def _inconsistent() -> ArchiveRefused:
    return ArchiveRefused(
        "stack_files_inconsistent", "The archive's stack files do not match its stack records."
    )


def _staged_compose_files(stacks: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for directory in stacks.iterdir():
        content = (
            [] if directory.is_symlink() or not directory.is_dir() else list(directory.iterdir())
        )
        file = directory / _COMPOSE_FILE
        if (
            _HEX_DIRECTORY.fullmatch(directory.name) is None
            or [item.name for item in content] != [_COMPOSE_FILE]
            or file.is_symlink()
            or not file.is_file()
        ):
            raise _inconsistent()
        found[directory.name] = file.read_bytes()
    return found


def _reconcile_stack_files(connection: sqlite3.Connection, staging: Path) -> None:
    rows = backup_snapshot.stack_files(connection, limit=MAX_STACK_MEMBERS + 1)
    if len(rows) > MAX_STACK_MEMBERS or any(
        _HEX_DIRECTORY.fullmatch(directory) is None for directory, _ in rows
    ):
        raise _inconsistent()
    stacks = staging / paths.STACKS_DIRECTORY
    present = _staged_compose_files(stacks)
    expected = dict(rows)
    if any(expected.get(directory) != data for directory, data in present.items()):
        raise _inconsistent()
    for directory, data in expected.items():
        if directory not in present:
            folder = paths.ensure_private_directory(stacks / directory)
            with os.fdopen(paths.open_private_file(folder / _COMPOSE_FILE), "wb") as handle:
                handle.write(data)


def _check_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm"):
        if database.with_name(f"{database.name}{suffix}").exists():
            raise _database_invalid("The staged database kept journal files after it was closed.")


def _prepare(staging: Path, operator_username: str) -> StagedCheck:
    database = staging / paths.STAGED_DATABASE
    try:
        connection = connect(database, create=False)
        try:
            _check_integrity(connection)
            migrated_from, schema_version = _migrate_staged(connection)
            _check_schema(connection)
            operator = backup_snapshot.administrator_id(connection, operator_username)
            if operator is None:
                raise ArchiveRefused(
                    "operator_not_in_archive",
                    "The archive has no enabled administrator with your user name.",
                )
            _reconcile_stack_files(connection, staging)
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        raise _database_invalid("The archive's database could not be read.") from None
    _check_sidecars(database)
    return StagedCheck(migrated_from, schema_version, operator)


async def prepare_staged(staging: Path, *, operator_username: str) -> StagedCheck:
    """Migrate the staged database and check it, its stack files and the operator's account."""
    return await asyncio.to_thread(_prepare, staging, operator_username)


@dataclass(frozen=True, slots=True)
class Reseal:
    """The key change made to a staged database."""

    key_version_before: int
    key_version_after: int
    fingerprint_before: str
    fingerprint_after: str
    values: int


def _decrypt_failed(message: str) -> ArchiveRefused:
    return ArchiveRefused("decrypt_failed", message)


def _canary_opens(state: CryptoState, key: bytes) -> bool:
    try:
        return (
            envelope.decrypt(
                state.canary,
                {state.key_version: key},
                canary_location(state.canary_row_id),
                state.instance_id,
            )
            == CANARY_PLAINTEXT
        )
    except DecryptionError:
        return False


async def _decrypt_test(
    repository: CryptoStateRepository, state: CryptoState, manifest: Manifest, key: bytes
) -> None:
    if state.key_version != manifest.key_version or state.instance_id != manifest.instance_id:
        raise _decrypt_failed("The staged database does not match the manifest.")
    if not envelope.is_valid_instance_id(state.instance_id) or not _canary_opens(state, key):
        raise _decrypt_failed("The key does not open this archive's encrypted data.")
    counts = await repository.count_by_version()
    if counts and set(counts) != {manifest.key_version}:
        raise _decrypt_failed("The archive holds secrets at a key version other than its own.")
    unparseable = await repository.find_unparseable()
    if unparseable:
        first = unparseable[0]
        raise _decrypt_failed(
            f"The value in {first.field.table}.{first.field.column} row {first.row_id} "
            "is not an encrypted secret."
        )
    vault = Vault(Keyring(manifest.key_version, {manifest.key_version: key}), state.instance_id)
    for sample in await repository.sample_at_version(manifest.key_version):
        location = SecretLocation(sample.field.table, sample.field.column, sample.row_id)
        try:
            vault.open(sample.token, location)
        except DecryptionError:
            raise _decrypt_failed(
                f"The value in {sample.field.table}.{sample.field.column} row {sample.row_id} "
                "could not be decrypted with this key."
            ) from None


def _reseal_failed(failures: tuple[RekeyFailure, ...]) -> ArchiveRefused:
    first = failures[0]
    return ArchiveRefused(
        "reseal_failed",
        f"The value in {first.field.table}.{first.field.column} row {first.row_id} "
        "could not be re-encrypted under the running key.",
    )


async def _reseal(
    session: Session,
    repository: CryptoStateRepository,
    state: CryptoState,
    manifest: Manifest,
    key: bytes,
    running_key: bytes,
) -> Reseal:
    if manifest.key_version >= MAX_KEY_VERSION:
        raise ArchiveRefused("reseal_failed", "The archive's key version cannot be advanced.")
    vault = Vault.for_restore(
        running_key=running_key,
        archive_key=key,
        archive_version=manifest.key_version,
        instance_id=state.instance_id,
    )
    rewritten = 0
    async for progress in RekeyPass(session, vault).run():
        if progress.failures:
            raise _reseal_failed(progress.failures)
        rewritten = progress.rewritten_total
    after = manifest.key_version + 1
    counts = await repository.count_by_version()
    if counts and set(counts) != {after}:
        raise ArchiveRefused("reseal_failed", "Some secrets were not re-encrypted.")
    await repository.write(
        after, state.instance_id, lambda where: vault.seal(CANARY_PLAINTEXT, where)
    )
    return Reseal(
        manifest.key_version,
        after,
        manifest.key_fingerprint,
        key_fingerprint(running_key),
        rewritten,
    )


async def decrypt_and_reseal(
    staging: Path, manifest: Manifest, archive_key: bytearray, running_key: bytes
) -> Reseal | None:
    """Prove the key opens the staged secrets; re-seal them to the running key when it differs.

    Returns None when the archive's key is the running key and nothing was rewritten.
    """
    key = bytes(archive_key)
    database = Database(staging / paths.STAGED_DATABASE)
    try:
        async with database.session() as session:
            repository = CryptoStateRepository(session)
            state = await repository.get()
            if state is None:
                raise _decrypt_failed("The staged database has no encryption state.")
            await _decrypt_test(repository, state, manifest, key)
            if hmac.compare_digest(key_fingerprint(key), key_fingerprint(running_key)):
                return None
            return await _reseal(session, repository, state, manifest, key, running_key)
    finally:
        await database.aclose()


@dataclass(frozen=True, slots=True)
class RestoreRecord:
    """What the restored activity log records about the restore."""

    operator_user_id: int
    operator_username: str
    archive_sha256: str
    manifest: Manifest
    migrated_from: int
    schema_version: int
    previous_path: str
    restore_id: str
    source: RestoreSource = "upload"
    object_storage_target_id: int | None = None
    object_path: str | None = None


def _source_detail(record: RestoreRecord) -> dict[str, DetailValue]:
    detail: dict[str, DetailValue] = {"source": record.source}
    if record.source == "object_storage":
        detail["object_storage_target_id"] = record.object_storage_target_id
        detail["object_path"] = record.object_path
    return detail


def _clear_and_gap(database: Path, live_activity_floor: int) -> None:
    connection = connect(database, create=False)
    try:
        backup_snapshot.strip_ephemeral(connection)
        floor = max(activity_sequence(connection), live_activity_floor) + ACTIVITY_ID_GAP
        raise_activity_sequence(connection, floor)
    finally:
        connection.close()


async def finalise_staged(
    staging: Path,
    *,
    record: RestoreRecord,
    reseal: Reseal | None,
    live_activity_floor: int,
    clock: Clock,
) -> None:
    """Clear sessions, gap the activity log, record the restore and set the automation pause."""
    path = staging / paths.STAGED_DATABASE
    await asyncio.to_thread(_clear_and_gap, path, live_activity_floor)
    manifest = record.manifest
    database = Database(path)
    try:
        async with database.session() as session:
            recorder = ActivityRecorder(session, clock)
            actor = UserActor(record.operator_user_id)
            async with session.transaction():
                await recorder.record_event(
                    actor,
                    action="instance_restored",
                    target_type="instance",
                    result="success",
                    category="user_action",
                    detail={
                        "archive_sha256": record.archive_sha256,
                        "archive_created_at": manifest.created_at,
                        "archive_product_version": manifest.product_version,
                        "archive_schema_version": manifest.schema_version,
                        "schema_version": record.schema_version,
                        "key_version": reseal.key_version_after if reseal else manifest.key_version,
                        "key_fingerprint": (
                            reseal.fingerprint_after if reseal else manifest.key_fingerprint
                        ),
                        "key_changed": reseal is not None,
                        "previous_path": record.previous_path,
                        "operator_username": record.operator_username,
                        **_source_detail(record),
                    },
                )
                await SettingRepository(session, clock).put(
                    RESTORE_AUTOMATION_PAUSED_KEY, "string", record.restore_id
                )
                if reseal is not None:
                    await recorder.record_event(
                        actor,
                        action="encryption_key_changed",
                        target_type="instance",
                        result="success",
                        category="user_action",
                        detail={
                            "key_version_before": reseal.key_version_before,
                            "key_version_after": reseal.key_version_after,
                            "key_fingerprint_before": reseal.fingerprint_before,
                            "key_fingerprint_after": reseal.fingerprint_after,
                            "values_resealed": reseal.values,
                        },
                    )
    finally:
        await database.aclose()
    await asyncio.to_thread(_check_sidecars, path)
