"""The restore operator command: review an archive, or replace this instance's data with it."""

import argparse
import asyncio
import hashlib
import hmac
import io
import os
import secrets
import shutil
import sqlite3
import sys
from contextlib import suppress
from pathlib import Path
from typing import Final, NoReturn, cast

from app.backup import paths
from app.backup.archive import CHUNK, hash_file, read_manifest, verify_members
from app.backup.manifest import ArchiveRefused, Manifest
from app.backup.pipeline import (
    PipelineStep,
    RestoreContext,
    StepRefused,
    StepStatus,
    run_restore_steps,
)
from app.backup.restore_steps import check_key
from app.config import DB_FILENAME, ConfigError, Settings, load_settings
from app.crypto.keys import MasterKeys, key_fingerprint, key_forms
from app.crypto.managed_key import ManagedKeyRefused, read_managed_key, resolve_master_keys
from app.crypto.startup import KeyMismatchError, establish_vault
from app.db import DatabaseError, initialise_database
from app.db.migrations import MigrationError, load_migrations
from app.db.repositories.backup_snapshot import LiveDatabase, read_live_database
from app.db.repositories.clocked import utc_now
from app.instance_lock import InstanceLock, InstanceLocked
from app.logging import DEFAULT_SCRUBBER, configure_logging
from app.selfcheck import (
    StartupCheckFailed,
    from_database_error,
    from_key_mismatch,
    from_managed_key_refusal,
)
from app.services.restore import UPLOAD_FREE_FLOOR, update_files_present

EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1
EXIT_USAGE: Final = 2
EXIT_BUSY: Final = 3
EXIT_UNRECOVERABLE: Final = 4

MAX_KEY_BYTES: Final = 1024
STDIN_SOURCE: Final = "-"
OPERATOR_SOURCE: Final = "operator_command"
ARCHIVE_COPY_NAME: Final = "archive.tar.gz"

_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_STEP_PHRASES: Final[dict[PipelineStep, str]] = {
    "verify": "verifying the archive members",
    "stage": "staging the archive",
    "migrate": "migrating and checking the staged database",
    "decrypt": "decrypting and finalising the staged database",
}
_STEP_COUNT: Final = len(_STEP_PHRASES)


class _Ending(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def add_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    """Register the restore subcommand."""
    parser = subparsers.add_parser(
        "restore",
        help="review a backup archive, or replace this instance's data with it",
        description=(
            "Without --replace, check the key and every member of the archive and print what "
            "would change. With --replace set to the archive's SHA-256, replace the data. "
            "The instance must be stopped."
        ),
    )
    parser.add_argument("--archive", required=True, type=Path, metavar="PATH")
    parser.add_argument(
        "--key-file",
        required=True,
        metavar="PATH",
        help="file holding the archive's encryption key, or - to read it from standard input",
    )
    parser.add_argument(
        "--operator",
        required=True,
        metavar="USERNAME",
        help="an enabled administrator in the archive, recorded as the actor",
    )
    parser.add_argument(
        "--replace",
        metavar="SHA256",
        help="the archive's SHA-256 as the review prints it; replaces the data",
    )


def execute(arguments: argparse.Namespace) -> int:
    """Run the restore command and return the process exit status."""
    try:
        return asyncio.run(
            _restore(arguments.archive, arguments.key_file, arguments.operator, arguments.replace)
        )
    except ArchiveRefused as refused:
        return _report(EXIT_REFUSED, refused.message)
    except _Ending as ending:
        return _report(ending.status, ending.message)


def _report(status: int, message: str) -> int:
    print(message, file=sys.stderr)
    return status


def _say(line: str) -> None:
    print(line, flush=True)


def _end(status: int, message: str) -> NoReturn:
    raise _Ending(status, message)


def _zero(buffer: bytearray) -> None:
    buffer[:] = bytes(len(buffer))


async def _restore(archive: Path, key_source: str, operator: str, replace: str | None) -> int:
    try:
        settings = load_settings()
    except ConfigError as error:
        _end(EXIT_USAGE, str(error))
    configure_logging(settings.log_level)
    data_dir = settings.data_dir
    _require_owner(data_dir)
    lock = InstanceLock(data_dir)
    try:
        lock.acquire()
    except InstanceLocked:
        _end(EXIT_BUSY, "Another dockmin process is using the data directory; stop it first.")
    except OSError:
        _end(EXIT_USAGE, "The data directory's lock file cannot be used.")
    try:
        return await _locked(settings, archive, key_source, operator, replace)
    finally:
        lock.release()


def _require_owner(data_dir: Path) -> None:
    try:
        owner = data_dir.stat().st_uid
    except OSError:
        _end(EXIT_USAGE, f"The data directory {data_dir} does not exist or cannot be read.")
    if not data_dir.is_dir():
        _end(EXIT_USAGE, f"{data_dir} is not a directory.")
    if os.geteuid() != owner:
        _end(
            EXIT_USAGE,
            f"Run as the user that owns {data_dir} (uid 10001 in the image).",
        )


async def _locked(
    settings: Settings, archive: Path, key_source: str, operator: str, replace: str | None
) -> int:
    data_dir = settings.data_dir
    if await asyncio.to_thread(update_files_present, data_dir):
        _end(EXIT_BUSY, "An update is in progress; restore is unavailable until it finishes.")
    target_key = await asyncio.to_thread(_target_key, settings)
    key_text = _read_key_text(key_source)
    archive_key: bytearray | None = None
    directory: Path | None = None
    restore_id = secrets.token_hex(16)
    try:
        if replace is None:
            sha256, source = await asyncio.to_thread(_hash_archive, archive), archive
        else:
            directory = await asyncio.to_thread(_new_directory, data_dir, restore_id)
            sha256 = await asyncio.to_thread(_copy_archive, archive, directory / ARCHIVE_COPY_NAME)
            source = directory / ARCHIVE_COPY_NAME
        manifest, raw_manifest = await asyncio.to_thread(read_manifest, source)
        # check_key takes str, so one immutable copy of the key text is unavoidable.
        archive_key = check_key(manifest, raw_manifest, key_text.decode("ascii", "replace"))
        _register(bytes(archive_key))
        _refuse_manifest(manifest, operator)
        await asyncio.to_thread(verify_members, source, manifest)
        live = await asyncio.to_thread(read_live_database, data_dir / DB_FILENAME)
        if replace is None:
            _review(settings, archive, sha256, manifest, archive_key, target_key, live)
            return EXIT_OK
        if not hmac.compare_digest(replace.strip().lower().encode(), sha256.encode()):
            _end(
                EXIT_REFUSED,
                f"The --replace value is not the archive's SHA-256, which is {sha256}. "
                "Nothing was changed.",
            )
        owned, directory = directory, None
        assert owned is not None
        return await _replace(
            settings,
            source,
            sha256,
            manifest,
            archive_key,
            target_key,
            operator,
            live,
            owned,
            restore_id,
        )
    finally:
        _zero(key_text)
        if archive_key is not None:
            _zero(archive_key)
        if directory is not None:
            _discard(directory)


def _target_key(settings: Settings) -> bytes:
    if settings.key_source != "generated":
        key = settings.encryption_key
        assert key is not None
        _register(key)
        return key
    key_file = settings.managed_key_file
    assert key_file is not None
    try:
        key = read_managed_key(key_file, DEFAULT_SCRUBBER)
    except ManagedKeyRefused as refused:
        _end(EXIT_USAGE, f"{refused.problem}. {refused.remedy}.")
    if key is None:
        _end(
            EXIT_USAGE,
            f"No encryption key is configured and {key_file} does not exist, and this command "
            "never creates one. Restore the key volume from a copy first (Restoring the key "
            "volume, on the Backups documentation page), then run this command again.",
        )
    return key


def _register(key: bytes) -> None:
    for form in key_forms(key):
        DEFAULT_SCRUBBER.register_secret(form)


def _read_key_text(source: str) -> bytearray:
    text = bytearray(MAX_KEY_BYTES + 1)
    try:
        if source == STDIN_SOURCE:
            if sys.stdin.isatty():
                _end(
                    EXIT_USAGE,
                    "Pipe or redirect the key into the command; it is not read from a terminal.",
                )
            filled = _fill(cast(io.BufferedIOBase, sys.stdin.buffer), text)
        else:
            with Path(source).open("rb") as handle:
                filled = _fill(handle, text)
    except OSError as error:
        _end(EXIT_USAGE, f"The key file cannot be read: {error.strerror or 'I/O error'}.")
    if filled > MAX_KEY_BYTES:
        _zero(text)
        _end(EXIT_USAGE, f"The key is longer than {MAX_KEY_BYTES} bytes.")
    del text[filled:]
    if stripped := text.decode("ascii", "replace").strip():
        DEFAULT_SCRUBBER.register_secret(stripped)
    return text


def _fill(stream: io.BufferedIOBase, buffer: bytearray) -> int:
    view = memoryview(buffer)
    filled = 0
    try:
        while filled < len(buffer) and (count := stream.readinto(view[filled:])):
            filled += count
    finally:
        view.release()
    return filled


def _hash_archive(archive: Path) -> str:
    try:
        return hash_file(archive).sha256
    except OSError as error:
        _end(EXIT_USAGE, f"The archive cannot be read: {error.strerror or 'I/O error'}.")


def _new_directory(data_dir: Path, restore_id: str) -> Path:
    try:
        return paths.new_restore_directory(data_dir, restore_id)
    except (OSError, paths.UnsafePath):
        _end(EXIT_REFUSED, "The restore directory could not be created. Nothing was changed.")


def _copy_archive(archive: Path, target: Path) -> str:
    """Copy archive to a new private file, returning the SHA-256 of the bytes written."""
    digest = hashlib.sha256()
    try:
        with archive.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            if shutil.disk_usage(target.parent).free < size + UPLOAD_FREE_FLOOR:
                raise ArchiveRefused(
                    "insufficient_space", "There is not enough free space on the data volume."
                )
            with os.fdopen(paths.open_private_file(target), "wb") as sink:
                while chunk := source.read(CHUNK):
                    if shutil.disk_usage(target.parent).free < UPLOAD_FREE_FLOOR:
                        raise ArchiveRefused(
                            "insufficient_space",
                            "There is not enough free space on the data volume.",
                        )
                    sink.write(chunk)
                    digest.update(chunk)
    except OSError as error:
        _end(EXIT_USAGE, f"The archive cannot be copied: {error.strerror or 'I/O error'}.")
    return digest.hexdigest()


def _fold(name: str) -> str:
    return name.translate(_ASCII_FOLD)


def _refuse_manifest(manifest: Manifest, operator: str) -> None:
    supported = load_migrations()[-1].version
    if manifest.schema_version > supported:
        raise ArchiveRefused(
            "schema_newer",
            f"The archive's schema version is {manifest.schema_version}; "
            f"this build supports up to {supported}.",
        )
    if _fold(operator) not in {_fold(name) for name in manifest.administrators}:
        raise ArchiveRefused(
            "operator_not_in_archive",
            "The archive has no enabled administrator with the user name given to --operator.",
        )


def _describe_live(live: LiveDatabase) -> str:
    match live.state:
        case "absent":
            return "none (no database)"
        case "unreadable":
            return "unreadable (treated as empty)"
        case "readable":
            counts = live.counts
            assert counts is not None
            return (
                f"{counts.users} users, {counts.environments} environments, "
                f"{counts.stacks} stacks, {counts.registry_credentials} registry credentials, "
                f"{counts.object_storage_targets} object storage targets, "
                f"{counts.automation_rules} automation rules"
            )


def _review(
    settings: Settings,
    archive: Path,
    sha256: str,
    manifest: Manifest,
    archive_key: bytearray,
    target_key: bytes,
    live: LiveDatabase,
) -> None:
    data_dir = settings.data_dir
    plan = paths.plan_swap(data_dir, data_dir / paths.RESTORE_DIRECTORY / "<id>")
    resealed = key_fingerprint(bytes(archive_key)) != key_fingerprint(target_key)
    _say(f"Archive: {archive}")
    _say(f"SHA-256: {sha256}")
    _say(f"Created: {manifest.created_at}")
    _say(f"Product version: {manifest.product_version}")
    _say(f"Schema version: {manifest.schema_version}")
    _say(f"Key: version {manifest.key_version}, fingerprint {manifest.key_fingerprint}")
    _say("Key check: the key matches the archive and signs its manifest")
    _say(f"Administrators: {', '.join(manifest.administrators)}")
    _say(f"Members verified: {len(manifest.members)}")
    _say(f"Live data now: {_describe_live(live)}")
    if plan.live:
        _say(f"These would move to {data_dir / paths.RESTORE_DIRECTORY}/<id>/previous/:")
        for path in plan.live:
            _say(f"  {path}")
    else:
        _say("Nothing in the data directory would move.")
    if resealed:
        _say(
            "Secrets: would be re-sealed to the target key, "
            f"fingerprint {key_fingerprint(target_key)}, as key version "
            f"{manifest.key_version + 1}"
        )
    else:
        _say("Secrets: stay under the archive's key, which is the target key")
    _say("Nothing was changed.")
    _say(f"To replace the data, run again with --replace {sha256}")


async def _replace(
    settings: Settings,
    archive: Path,
    sha256: str,
    manifest: Manifest,
    archive_key: bytearray,
    target_key: bytes,
    operator: str,
    live: LiveDatabase,
    directory: Path,
    restore_id: str,
) -> int:
    data_dir = settings.data_dir
    previous = directory / paths.PREVIOUS_DIRECTORY
    current: list[PipelineStep] = []

    def on_step(step: PipelineStep, status: StepStatus) -> None:
        if status == "running":
            current[:] = [step]
            position = list(_STEP_PHRASES).index(step) + 1
            phrase = _STEP_PHRASES[step]
            _say(f"[{position}/{_STEP_COUNT}] {phrase[0].upper()}{phrase[1:]}")

    try:
        await run_restore_steps(
            archive=archive,
            staging=directory / paths.STAGING_DIRECTORY,
            manifest=manifest,
            archive_key=archive_key,
            running_key=target_key,
            operator_username=operator,
            live_activity_floor=live.activity_floor,
            record_context=RestoreContext(
                restore_id=restore_id,
                archive_sha256=sha256,
                previous_path=str(previous),
                source=OPERATOR_SOURCE,
            ),
            clock=utc_now,
            stop=lambda: False,
            on_step=on_step,
        )
    except StepRefused as refused:
        await asyncio.to_thread(_discard, directory)
        _end(EXIT_REFUSED, f"{refused.message} Nothing was changed.")
    except (OSError, paths.UnsafePath, sqlite3.Error, DatabaseError) as error:
        await asyncio.to_thread(_discard, directory)
        step_name = _STEP_PHRASES[current[0]] if current else "restoring"
        _end(
            EXIT_REFUSED,
            f"The restore stopped while {step_name} ({type(error).__name__}). Nothing was changed.",
        )
    except BaseException:
        await asyncio.to_thread(_discard, directory)
        raise
    await asyncio.to_thread(archive.unlink, missing_ok=True)

    plan = paths.plan_swap(data_dir, directory)
    _say(f"Replacing the data; the previous data is kept in {previous}")
    try:
        journal = await asyncio.to_thread(paths.apply_swap, plan)
    except paths.SwapFailed as failure:
        await _revert_after_failed_swap(plan, failure, directory)
    try:
        _say("Checking that the restored data starts")
        problem = await _start_check(settings)
    except BaseException as error:
        await _reject_after_error(plan, journal, directory, error)
    if problem is not None:
        await _reject(plan, journal, directory, problem)
    await asyncio.to_thread(_discard, directory)
    _say(f"Restored. The previous data is kept in {previous}")
    _say("Automation is held at the next start until an administrator releases it.")
    return EXIT_OK


async def _revert_after_failed_swap(
    plan: paths.SwapPlan, failure: paths.SwapFailed, directory: Path
) -> NoReturn:
    try:
        await asyncio.to_thread(paths.revert_swap, failure.journal)
    except paths.SwapFailed:
        _end(
            EXIT_UNRECOVERABLE,
            f"{failure.message}, and the moves already made could not be undone. The live data "
            f"is in {plan.previous} and {plan.staged}; the restore directory {directory} was kept.",
        )
    _end(
        EXIT_UNRECOVERABLE,
        f"{failure.message}. The previous data was put back; the staged data is in "
        f"{plan.staged}, and the restore directory {directory} was kept.",
    )


async def _start_check(settings: Settings) -> StartupCheckFailed | None:
    database = None
    try:
        database = await initialise_database(settings)
        keys: MasterKeys = await resolve_master_keys(
            settings, database, restore_leftover=paths.restore_leftover
        )
        await establish_vault(
            database,
            keys,
            allow_rotation=settings.allow_key_rotation,
            key_description=settings.key_description,
            previous_key_description=settings.previous_key_description,
        )
    except ManagedKeyRefused as error:
        return from_managed_key_refusal(error)
    except KeyMismatchError as error:
        return from_key_mismatch(error)
    except (MigrationError, sqlite3.Error, DatabaseError, OSError) as error:
        return from_database_error(error, during_key_check=True)
    finally:
        if database is not None:
            await database.aclose()
    return None


async def _reject_after_error(
    plan: paths.SwapPlan, journal: paths.SwapJournal, directory: Path, error: BaseException
) -> NoReturn:
    rejected = directory / paths.REJECTED_DIRECTORY
    cause = (
        "was interrupted"
        if isinstance(error, KeyboardInterrupt | asyncio.CancelledError)
        else f"failed unexpectedly ({type(error).__name__})"
    )
    try:
        await asyncio.to_thread(paths.reject_restored, plan, journal, rejected)
    except paths.SwapFailed as failure:
        _end(
            EXIT_UNRECOVERABLE,
            f"The restore {cause} after the data was replaced, and the previous data could not "
            f"be put back ({failure.message}). The previous data is in {plan.previous}; the "
            f"restored data is in {rejected}; the restore directory {directory} was kept.",
        )
    _end(
        EXIT_UNRECOVERABLE,
        f"The restore {cause} after the data was replaced. The previous data was put back in "
        f"{plan.data_dir}; the restored data is in {rejected}.",
    )


async def _reject(
    plan: paths.SwapPlan, journal: paths.SwapJournal, directory: Path, problem: StartupCheckFailed
) -> NoReturn:
    rejected = directory / paths.REJECTED_DIRECTORY
    try:
        await asyncio.to_thread(paths.reject_restored, plan, journal, rejected)
    except paths.SwapFailed as failure:
        _end(
            EXIT_UNRECOVERABLE,
            f"The restored instance did not start ({problem.problem}), and the previous data "
            f"could not be put back ({failure.message}). The previous data is in {plan.previous}; "
            f"the restored data is in {rejected}; the restore directory {directory} was kept.",
        )
    await asyncio.to_thread(_discard, directory)
    _end(
        EXIT_REFUSED,
        f"The restored instance did not start: {problem.problem}. {problem.remedy}. "
        f"The previous data was put back; the restored files are in {rejected}.",
    )


def _discard(directory: Path) -> None:
    """Remove everything in the restore directory but previous/ and rejected/."""
    keep = {paths.PREVIOUS_DIRECTORY, paths.REJECTED_DIRECTORY}
    with suppress(OSError):
        for entry in directory.iterdir():
            if entry.name in keep:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
    with suppress(OSError):
        directory.rmdir()
        directory.parent.rmdir()
