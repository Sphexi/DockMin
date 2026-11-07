"""Produces a backup archive from the live database: snapshot, normalise, strip, pack, hash."""

import asyncio
import shutil
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Final

from app.backup import archive, paths
from app.backup.manifest import (
    FORMAT_VERSION,
    MAX_ADMINISTRATORS,
    MAX_STACK_MEMBERS,
    ArchiveRefused,
    Manifest,
    ProductionFailed,
    decode_manifest,
    encode_manifest,
)
from app.backup.parts import PartDigest
from app.crypto.vault import Vault
from app.db.engine import Database, connect
from app.db.repositories import backup_snapshot
from app.db.repositories.clocked import Clock, format_timestamp
from app.db.repositories.crypto_state import CryptoState, CryptoStateRepository
from app.db.repositories.rekey import RekeyFailure, RekeyPass
from app.logging import get_logger
from app.version import product_version

SNAPSHOT_NAME: Final = "snapshot.sqlite3"
ARCHIVE_NAME: Final = "archive.tar.gz"
SPACE_HEADROOM: Final = 64 * 1024 * 1024
MAX_LOGGED_LOCATIONS: Final = 10

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProducedArchive:
    """A finished archive in the work directory, with what the uploader and the log need."""

    path: Path
    size: int
    sha256: str
    md5_b64: str
    parts: tuple[PartDigest, ...]
    manifest: Manifest
    file_name: str


@dataclass(frozen=True, slots=True)
class _SnapshotFacts:
    schema_version: int
    administrators: tuple[str, ...]
    stacks: list[tuple[str, bytes]]


def _failed(message: str) -> ProductionFailed:
    return ProductionFailed("production_failed", message)


def _sidecars(database: Path) -> tuple[Path, Path]:
    return database.with_name(f"{database.name}-wal"), database.with_name(f"{database.name}-shm")


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _check_space(database: Database, workdir: Path) -> None:
    used = _size_of(database.path) + _size_of(_sidecars(database.path)[0])
    if shutil.disk_usage(workdir).free < 2 * used + SPACE_HEADROOM:
        raise ProductionFailed(
            "insufficient_space",
            "There is not enough free space on the data volume to take a snapshot.",
        )


def _take_snapshot(database: Database, target: Path) -> None:
    connection = database.connect()
    try:
        backup_snapshot.vacuum_into(connection, target)
    finally:
        connection.close()
    target.chmod(paths.FILE_MODE)


def _locations(failures: tuple[RekeyFailure, ...]) -> str:
    return ", ".join(
        f"{failure.field.table}.{failure.field.column} row {failure.row_id}"
        for failure in failures[:MAX_LOGGED_LOCATIONS]
    )


def _unreadable(failures: tuple[RekeyFailure, ...] = ()) -> ProductionFailed:
    message = "A stored secret could not be read with the loaded key, so no backup was made."
    if failures:
        message = f"{message} Affected values: {_locations(failures)}."
    return ProductionFailed("secret_unreadable", message)


async def _normalise(snapshot: Path, vault: Vault) -> CryptoState:
    """Re-seal every secret in the snapshot at the current key version.

    Raises ProductionFailed when any value cannot be opened or is not shaped like ciphertext.
    """
    snapshot_database = Database(snapshot, secure_delete=True)
    try:
        async with snapshot_database.session() as session:
            state_repository = CryptoStateRepository(session)
            state = await state_repository.get()
            if state is None or state.key_version != vault.current_version:
                raise _failed("The database and the loaded key disagree on the key version.")
            failures: tuple[RekeyFailure, ...] = ()
            async for progress in RekeyPass(session, vault, verify_current=True).run():
                failures = progress.failures
            if failures:
                logger.error("backup_secret_unreadable", extra={"locations": _locations(failures)})
                raise _unreadable(failures)
            counts = await state_repository.count_by_version()
            if counts and set(counts) != {vault.current_version}:
                raise _unreadable()
            return state
    finally:
        await snapshot_database.aclose()


def _finish_snapshot(snapshot: Path) -> _SnapshotFacts:
    connection = connect(snapshot)
    try:
        backup_snapshot.strip_ephemeral(connection)
        version = backup_snapshot.schema_version(connection)
        names = backup_snapshot.enabled_administrators(connection, limit=MAX_ADMINISTRATORS + 1)
        stacks = backup_snapshot.stack_files(connection, limit=MAX_STACK_MEMBERS + 1)
    finally:
        connection.close()
    if any(sidecar.exists() for sidecar in _sidecars(snapshot)):
        raise _failed("The snapshot kept journal files after it was closed.")
    administrators = tuple(sorted(names))
    if len(administrators) > MAX_ADMINISTRATORS:
        logger.warning("backup_administrators_truncated", extra={"limit": MAX_ADMINISTRATORS})
        administrators = administrators[:MAX_ADMINISTRATORS]
    return _SnapshotFacts(version, administrators, stacks)


def _pack(
    snapshot: Path,
    destination: Path,
    facts: _SnapshotFacts,
    state: CryptoState,
    vault: Vault,
    created_at: str,
    mtime: int,
) -> tuple[Manifest, archive.FileDigests]:
    members = archive.inventory(snapshot, facts.stacks)
    fields = Manifest(
        format_version=FORMAT_VERSION,
        schema_version=facts.schema_version,
        created_at=created_at,
        product_version=product_version(),
        instance_id=state.instance_id,
        key_version=state.key_version,
        key_fingerprint=vault.key_fingerprint(),
        administrators=facts.administrators,
        members=members,
        mac="",
    )
    try:
        raw = encode_manifest(fields, mac=vault.manifest_mac)
        manifest = decode_manifest(raw)
    except (ValueError, ArchiveRefused):
        raise _failed("The manifest could not be built within its limits.") from None
    archive.write_archive(
        destination,
        manifest=raw,
        members=members,
        snapshot=snapshot,
        stacks=facts.stacks,
        mtime=mtime,
        scanner=vault.key_scanner(),
    )
    return manifest, archive.hash_file(destination, with_parts=True)


def _remove(*targets: Path) -> None:
    for target in targets:
        target.unlink(missing_ok=True)


async def produce_archive(
    database: Database, vault: Vault, workdir: Path, *, clock: Clock
) -> ProducedArchive:
    """Write a backup archive into workdir and return it; the caller removes workdir.

    Raises ProductionFailed. Only one production may run at a time per data directory.
    """
    moment = clock()
    created_at = format_timestamp(moment)
    snapshot = workdir / SNAPSHOT_NAME
    destination = workdir / ARCHIVE_NAME
    succeeded = False
    try:
        await asyncio.to_thread(paths.ensure_private_directory, workdir)
        await asyncio.to_thread(_check_space, database, workdir)
        await asyncio.to_thread(_take_snapshot, database, snapshot)
        state = await _normalise(snapshot, vault)
        facts = await asyncio.to_thread(_finish_snapshot, snapshot)
        manifest, digests = await asyncio.to_thread(
            _pack, snapshot, destination, facts, state, vault, created_at, int(moment.timestamp())
        )
        succeeded = True
    except ProductionFailed:
        raise
    except Exception as error:
        logger.error("backup_production_failed", extra={"error": type(error).__name__})
        raise _failed("The backup could not be produced.") from None
    finally:
        await asyncio.to_thread(_remove, snapshot, *_sidecars(snapshot))
        if not succeeded:
            await asyncio.to_thread(_remove, destination)
    logger.info(
        "backup_archive_produced",
        extra={
            "size_bytes": digests.size,
            "schema_version": manifest.schema_version,
            "key_version": manifest.key_version,
            "members": len(manifest.members) + 1,
        },
    )
    return ProducedArchive(
        path=destination,
        size=digests.size,
        sha256=digests.sha256,
        md5_b64=digests.md5_b64,
        parts=digests.parts,
        manifest=manifest,
        file_name=f"dockmin-backup-{moment.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}.tar.gz",
    )
