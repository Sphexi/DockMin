"""Exclusive lock on the data directory, so one process at a time runs the scheduler."""

import errno
import importlib
import os
import stat
from pathlib import Path
from typing import Any

from app.logging import get_logger

logger = get_logger(__name__)

LOCK_FILENAME = "dockmin.lock"
LOCK_MODE = 0o600


class InstanceLocked(RuntimeError):
    """Another process holds the data directory's lock."""


class LockFileUnsafe(OSError):
    """The lock path is not a file this process alone owns."""


def _load_fcntl() -> Any:
    try:
        return importlib.import_module("fcntl")
    except ImportError:
        return None


class InstanceLock:
    """Holds an exclusive, non-blocking flock on <data_dir>/dockmin.lock from acquire to release."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / LOCK_FILENAME
        self._descriptor: int | None = None

    @property
    def path(self) -> Path:
        """Return the lock file's path."""
        return self._path

    @property
    def held(self) -> bool:
        """Report whether this object holds the lock."""
        return self._descriptor is not None

    def acquire(self) -> None:
        """Take the lock, or raise InstanceLocked when another process holds it."""
        if self._descriptor is not None:
            return
        fcntl = _load_fcntl()
        if fcntl is None:
            logger.warning("instance_lock_unavailable")
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._warn_if_shared_directory()
        descriptor = self._open()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                logger.error("instance_already_running", extra={"path": str(self._path)})
                raise InstanceLocked(
                    "Another dockmin process is using this data directory."
                ) from None
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        """Drop the lock; releasing a lock that is not held does nothing."""
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            os.close(descriptor)

    def _warn_if_shared_directory(self) -> None:
        try:
            mode = self._path.parent.stat().st_mode
        except OSError:
            return
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            logger.warning("data_dir_writable_by_others", extra={"path": str(self._path.parent)})

    def _open(self) -> int:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags, LOCK_MODE)
        except OSError as error:
            logger.error(
                "instance_lock_failed", extra={"path": str(self._path), "errno": error.errno}
            )
            raise
        found = os.fstat(descriptor)
        if not stat.S_ISREG(found.st_mode):
            os.close(descriptor)
            raise OSError(errno.EINVAL, "the instance lock path is not a regular file")
        owner = getattr(os, "geteuid", None)
        if found.st_nlink != 1 or (owner is not None and found.st_uid != owner()):
            os.close(descriptor)
            logger.error("instance_lock_unsafe", extra={"path": str(self._path)})
            raise LockFileUnsafe(
                errno.EPERM, "the instance lock file must be owned by this user and have one link"
            )
        if stat.S_IMODE(found.st_mode) != LOCK_MODE and hasattr(os, "fchmod"):
            os.fchmod(descriptor, LOCK_MODE)
        return descriptor
