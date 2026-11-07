"""Bounded JSON reads and atomic file writes shared by the plan and result formats."""

import fcntl
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

FILE_MODE = 0o600


def read_json_object(path: Path, limit: int) -> dict[str, Any]:
    """Read a regular, non-symlink file of at most `limit` bytes holding one JSON object."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(f"{path.name} is not a regular file")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"{path.name} exceeds {limit} bytes")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} does not hold a JSON object")
    return data


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write `data` to `path` through a temporary file, fsync and rename, with mode 0600."""
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), FILE_MODE)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    fsync_directory(path.parent)


def copy_file_atomic(source: Path, destination: Path) -> None:
    """Copy `source` over `destination` through a temporary file, keeping the source's mode."""
    temporary = destination.with_name(f".{destination.name}.copy")
    temporary.unlink(missing_ok=True)
    mode = stat.S_IMODE(source.stat().st_mode)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, FILE_MODE)
    try:
        with os.fdopen(descriptor, "wb") as target, source.open("rb") as origin:
            shutil.copyfileobj(origin, target)
            os.fchmod(target.fileno(), mode)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    fsync_directory(destination.parent)


def fsync_directory(directory: Path) -> None:
    """Flush a directory's entries to disk."""
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def try_exclusive_lock(path: Path) -> Iterator[bool]:
    """Hold an exclusive advisory lock on `path` for the block; yield False when it is taken."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)
