"""Reads the generated master key, and creates it for a new database in a mounted key directory."""

import asyncio
import base64
import binascii
import errno
import hmac
import os
import re
import secrets
import stat
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Final

from app.config import (
    KEY_BYTES,
    MANAGED_KEY_FILENAME,
    SECRET_KEY_FILE_VAR,
    Settings,
)
from app.crypto.keys import MasterKeys, key_fingerprint, key_forms
from app.db.engine import Database
from app.db.repositories.crypto_state import CryptoStateRepository
from app.logging import DEFAULT_SCRUBBER, Scrubber, get_logger

KEY_FILE_MODE: Final = 0o400
MAX_KEY_FILE_BYTES: Final = 1024
BROAD_MODE_BITS: Final = 0o077
SHARED_WRITE_BITS: Final = 0o022
TEMPORARY_KEY_PREFIX: Final = f".{MANAGED_KEY_FILENAME}.tmp-"
MOUNTINFO_FILE: Final = Path("/proc/self/mountinfo")
VOLATILE_FILESYSTEMS: Final = frozenset({"tmpfs", "ramfs"})
MAX_ANCESTOR_DEPTH: Final = 64

logger = get_logger(__name__)

_OPEN_NO_FOLLOW: Final = os.O_NOFOLLOW | os.O_CLOEXEC
_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")
_KEY_FILE_RESTORE: Final = (
    "Restore the key file from its copy. dockmin never replaces an existing key file"
)


class ManagedKeyRefused(Exception):
    """The generated key cannot be read or created; problem and remedy are product text."""

    def __init__(self, problem: str, remedy: str) -> None:
        super().__init__(problem)
        self.problem = problem
        self.remedy = remedy


def mount_point(path: Path) -> bool:
    """Report whether path is a mount point."""
    return os.path.ismount(path)


type LeftoverFinder = Callable[[Path], Path | None]


async def resolve_master_keys(
    settings: Settings,
    database: Database,
    *,
    restore_leftover: LeftoverFinder,
    scrubber: Scrubber = DEFAULT_SCRUBBER,
) -> MasterKeys:
    """Return the master keys, generating the key for a new database when none is configured.

    Must run after migration and before the vault is established. The generated key is durable
    on disk before this returns, so the canary sealed next is never under a key a crash can lose.
    """
    if settings.key_source != "generated":
        return MasterKeys.from_settings(settings)
    key_file = settings.managed_key_file
    assert key_file is not None
    directory = settings.secret_key_dir
    previous = settings.previous_encryption_key
    current = await _off_loop(partial(_read_existing, key_file, scrubber), key_file, directory)
    if current is None:
        _refuse_when_key_cannot_be_new(settings, key_file)
        await _refuse_when_data_exists(settings, database, key_file, restore_leftover)
        current = await _off_loop(
            partial(_generate, directory, key_file, settings.data_dir, scrubber),
            key_file,
            directory,
        )
    if previous is not None and hmac.compare_digest(previous, current):
        raise ManagedKeyRefused(
            f"The previous key, from {settings.previous_key_description}, must differ from the "
            f"key in {key_file}",
            f"Remove {settings.previous_key_variable}, or set it to the key the stored data was "
            "sealed with before the current one",
        )
    return MasterKeys(current, previous)


async def _off_loop[T](function: Callable[[], T], key_file: Path, directory: Path) -> T:
    try:
        return await asyncio.to_thread(function)
    except OSError as error:
        raise ManagedKeyRefused(
            f"The key file {key_file} could not be used: {error.strerror or 'I/O error'}",
            f"Check the permissions and free space of {directory}",
        ) from None


def _refuse_when_key_cannot_be_new(settings: Settings, key_file: Path) -> None:
    if settings.previous_encryption_key is not None:
        raise _missing_key_refusal(
            f"The previous key, from {settings.previous_key_description}, is set but {key_file} "
            "does not exist",
            settings.secret_key_dir,
        )


def _missing_key_refusal(
    problem: str, directory: Path, *, discard: Path | None = None
) -> ManagedKeyRefused:
    remedy = (
        f"Mount the key volume this instance was created with at {directory}, or supply its key "
        f"with {SECRET_KEY_FILE_VAR}. A new key cannot read the stored credentials"
    )
    if discard is not None:
        remedy += f"; if that data is no longer needed, remove {discard}"
    return ManagedKeyRefused(problem, remedy)


async def _refuse_when_data_exists(
    settings: Settings, database: Database, key_file: Path, restore_leftover: LeftoverFinder
) -> None:
    directory = settings.secret_key_dir
    if await _holds_encrypted_data(database):
        raise _missing_key_refusal(
            f"No encryption key is configured and {key_file} does not exist, but the "
            "database already holds encrypted data",
            directory,
        )
    leftover = await _off_loop(partial(restore_leftover, settings.data_dir), key_file, directory)
    if leftover is not None:
        raise _missing_key_refusal(
            f"No encryption key is configured and {key_file} does not exist, but {leftover} "
            "holds data kept by an earlier restore",
            directory,
            discard=leftover,
        )


async def _holds_encrypted_data(database: Database) -> bool:
    async with database.session() as session:
        repo = CryptoStateRepository(session)
        if await repo.get() is not None or await repo.has_secret_values():
            return True
        return await repo.has_crypto_settings()


def read_managed_key(key_file: Path, scrubber: Scrubber) -> bytes | None:
    """Return the key in key_file, None when the file is absent; never creates the file."""
    return _read_existing(key_file, scrubber)


def _read_existing(key_file: Path, scrubber: Scrubber) -> bytes | None:
    """Return the key in key_file, None when the file is absent; refuse a file that is unusable."""
    try:
        descriptor = os.open(key_file, os.O_RDONLY | os.O_NONBLOCK | _OPEN_NO_FOLLOW)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as error:
        if error.errno == errno.EACCES and not os.access(key_file.parent, os.X_OK):
            raise _ownership_refusal(key_file.parent) from None
        reason = "is a symbolic link" if error.errno == errno.ELOOP else "cannot be read"
        raise ManagedKeyRefused(f"The key file {key_file} {reason}", _KEY_FILE_RESTORE) from None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise ManagedKeyRefused(
                f"The key file {key_file} is not a regular file", _KEY_FILE_RESTORE
            )
        try:
            content = os.read(descriptor, MAX_KEY_FILE_BYTES + 1)
        except OSError:
            raise ManagedKeyRefused(
                f"The key file {key_file} cannot be read", _KEY_FILE_RESTORE
            ) from None
    finally:
        os.close(descriptor)
    key = _decode(content)
    if key is None:
        raise ManagedKeyRefused(
            f"The key file {key_file} does not hold base64 of {KEY_BYTES} bytes", _KEY_FILE_RESTORE
        )
    _register(key, scrubber)
    if status.st_mode & BROAD_MODE_BITS:
        logger.warning(
            "encryption_key_file_permissions",
            extra={"key_file": str(key_file), "mode": f"{stat.S_IMODE(status.st_mode):04o}"},
        )
    return key


def _decode(content: bytes) -> bytes | None:
    if len(content) > MAX_KEY_FILE_BYTES:
        return None
    try:
        key = base64.b64decode(content.decode("ascii").strip(), validate=True)
    except (UnicodeDecodeError, binascii.Error, ValueError):
        return None
    return key if len(key) == KEY_BYTES else None


def _register(key: bytes, scrubber: Scrubber) -> None:
    for form in key_forms(key):
        scrubber.register_secret(form)


def _generate(directory: Path, key_file: Path, data_dir: Path, scrubber: Scrubber) -> bytes:
    """Create key_file in directory, durably, and return its key; read the winner of a race."""
    directory_fd = _open_key_directory(directory, data_dir)
    try:
        key = secrets.token_bytes(KEY_BYTES)
        _register(key, scrubber)
        if not _publish(directory_fd, key):
            winner = _read_existing(key_file, scrubber)
            if winner is None:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))
            return winner
        _remove_leftover_temporaries(directory_fd)
    finally:
        os.close(directory_fd)
    logger.warning(
        "encryption_key_generated",
        extra={
            "key_file": str(key_file),
            "key_fingerprint": key_fingerprint(key),
            "note": f"Copy {key_file} somewhere apart from the data volume; without it the "
            "stored credentials cannot be read",
        },
    )
    return key


def _publish(directory_fd: int, key: bytes) -> bool:
    """Make the key file appear complete and durable under its final name; False if one exists.

    The final name is linked to a finished temporary file, so it never exists half written and an
    existing file is never replaced.
    """
    temporary = f"{TEMPORARY_KEY_PREFIX}{secrets.token_hex(8)}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _OPEN_NO_FOLLOW,
        KEY_FILE_MODE,
        dir_fd=directory_fd,
    )
    identity: os.stat_result | None = None
    try:
        try:
            os.fchmod(descriptor, KEY_FILE_MODE)
            _write_all(descriptor, base64.b64encode(key) + b"\n")
            os.fsync(descriptor)
            identity = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(
                temporary,
                MANAGED_KEY_FILENAME,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except (FileExistsError, FileNotFoundError):
            return False
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        if identity is not None:
            _unlink_if_same_file(directory_fd, MANAGED_KEY_FILENAME, identity)
        raise
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
    return True


def _unlink_if_same_file(directory_fd: int, name: str, identity: os.stat_result) -> None:
    with suppress(OSError):
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
            os.unlink(name, dir_fd=directory_fd)


def _remove_leftover_temporaries(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        if name.startswith(TEMPORARY_KEY_PREFIX):
            with suppress(OSError):
                os.unlink(name, dir_fd=directory_fd)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        view = view[os.write(descriptor, view) :]


def _open_key_directory(directory: Path, data_dir: Path) -> int:
    """Open directory for key creation once it passes every directory check, or refuse."""
    remedy = (
        f"Mount a volume at {directory}, for example -v <volume-name>:{directory}, or set "
        f"{SECRET_KEY_FILE_VAR}"
    )
    try:
        status = os.lstat(directory)
    except FileNotFoundError:
        raise ManagedKeyRefused(f"The key directory {directory} does not exist", remedy) from None
    if stat.S_ISLNK(status.st_mode):
        raise ManagedKeyRefused(f"The key directory {directory} is a symbolic link", remedy)
    if not stat.S_ISDIR(status.st_mode):
        raise ManagedKeyRefused(f"The key directory {directory} is not a directory", remedy)
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | _OPEN_NO_FOLLOW)
    except PermissionError:
        raise _ownership_refusal(directory) from None
    except OSError:
        raise ManagedKeyRefused(f"The key directory {directory} cannot be opened", remedy) from None
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (status.st_dev, status.st_ino):
            raise ManagedKeyRefused(f"The key directory {directory} changed while opening", remedy)
        _refuse_data_directory_overlap(directory, data_dir, descriptor, opened)
        if not mount_point(directory):
            raise ManagedKeyRefused(
                f"The key directory {directory} is not a mounted volume; a key written there is "
                "lost when the container is replaced",
                remedy,
            )
        if (filesystem := mounted_filesystem(directory)) in VOLATILE_FILESYSTEMS:
            raise ManagedKeyRefused(
                f"The key directory {directory} is a {filesystem} mount; a key written there is "
                "lost when the container stops",
                remedy,
            )
        _check_ownership(directory, opened, descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _refuse_data_directory_overlap(
    directory: Path, data_dir: Path, descriptor: int, opened: os.stat_result
) -> None:
    remedy = f"Give {directory} its own volume, for example -v <volume-name>:{directory}"
    try:
        target = data_dir.stat()
    except OSError:
        return
    identity = (target.st_dev, target.st_ino)
    if (opened.st_dev, opened.st_ino) == identity:
        raise ManagedKeyRefused(
            f"The key directory {directory} is the same directory as the data directory {data_dir}",
            remedy,
        )
    if _is_beneath(descriptor, opened, identity):
        raise ManagedKeyRefused(
            f"The key directory {directory} is inside the data directory {data_dir}", remedy
        )


def _is_beneath(descriptor: int, start: os.stat_result, identity: tuple[int, int]) -> bool:
    """Report whether the directory with identity is an ancestor of the open directory."""
    current = os.dup(descriptor)
    here = (start.st_dev, start.st_ino)
    try:
        for _ in range(MAX_ANCESTOR_DEPTH):
            try:
                parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=current)
            except OSError:
                return False
            os.close(current)
            current = parent
            status = os.fstat(current)
            above = (status.st_dev, status.st_ino)
            if above == identity:
                return True
            if above == here:
                return False
            here = above
        return False
    finally:
        os.close(current)


def mounted_filesystem(directory: Path) -> str | None:
    """Return the filesystem type mounted at directory, or None when it cannot be determined."""
    try:
        text = MOUNTINFO_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return filesystem_at(text, os.path.realpath(directory))


def filesystem_at(mountinfo: str, mount_path: str) -> str | None:
    """Return the type of the last mount in a mountinfo listing whose mount point is mount_path."""
    found: str | None = None
    for line in mountinfo.splitlines():
        fields = line.split(" ")
        if "-" not in fields[6:]:
            continue
        separator = fields.index("-", 6)
        if separator + 1 < len(fields) and _unescape_mountinfo(fields[4]) == mount_path:
            found = fields[separator + 1]
    return found


def _unescape_mountinfo(field: str) -> str:
    return _MOUNTINFO_ESCAPE.sub(lambda match: chr(int(match[1], 8)), field)


def _check_ownership(directory: Path, status: os.stat_result, descriptor: int) -> None:
    if (
        status.st_uid != os.geteuid()
        or status.st_mode & SHARED_WRITE_BITS
        or not os.access(".", os.W_OK | os.X_OK, dir_fd=descriptor)
    ):
        raise _ownership_refusal(directory)


def _ownership_refusal(directory: Path) -> ManagedKeyRefused:
    uid = os.geteuid()
    return ManagedKeyRefused(
        f"The key directory {directory} must be owned by uid {uid} and writable only by it",
        "A named volume takes this from the image, which runs as uid 10001. For a bind mount, "
        f"give the host directory to uid {uid}, the user this process runs as, with mode 0700",
    )
