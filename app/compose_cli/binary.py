"""Locates the standalone Compose binary once, so no later call consults PATH."""

import os
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.compose_cli.errors import ComposeBinaryInvalid
from app.logging import get_logger

logger = get_logger(__name__)

SEARCH_PATHS: Final[tuple[Path, ...]] = (
    Path("/usr/local/libexec/docker/cli-plugins/docker-compose"),
    Path("/usr/libexec/docker/cli-plugins/docker-compose"),
    Path("/usr/local/lib/docker/cli-plugins/docker-compose"),
    Path("/usr/lib/docker/cli-plugins/docker-compose"),
)
PATH_NAME: Final = "docker-compose"


@dataclass(frozen=True)
class ComposeBinary:
    """The command prefix that starts Compose; a single absolute path in production."""

    argv0: tuple[str, ...]


def locate(
    configured: Path | None,
    *,
    search_paths: Sequence[Path] = SEARCH_PATHS,
    path_variable: str | None = None,
) -> ComposeBinary | None:
    """Return the Compose binary, or None when there is none.

    The binary and its directory must belong to root or the server's user and be writable by
    nobody else. A configured path that is not an absolute, trusted executable file raises
    ComposeBinaryInvalid instead of falling through to the search; a discovered candidate that
    is not trusted is skipped.
    """
    if configured is not None:
        if not (is_executable_file(configured) and is_trusted(configured)):
            raise ComposeBinaryInvalid
        return ComposeBinary((str(configured),))
    for candidate in search_paths:
        if usable(candidate):
            return ComposeBinary((str(candidate),))
    found = shutil.which(PATH_NAME, path=path_variable)
    if found is not None and usable(Path(found)):
        return ComposeBinary((found,))
    return None


def usable(path: Path, *, event: str = "compose_binary_untrusted") -> bool:
    """Report whether the path is a trusted executable file; log `event` for an untrusted one."""
    if not is_executable_file(path):
        return False
    if is_trusted(path):
        return True
    logger.warning(event, extra={"path": str(path)})
    return False


def is_executable_file(path: Path) -> bool:
    return path.is_absolute() and path.is_file() and os.access(path, os.X_OK)


def is_trusted(path: Path) -> bool:
    if os.name != "posix":
        return True
    try:
        resolved = path.resolve(strict=True)
        return _owned_and_closed(resolved) and _owned_and_closed(resolved.parent)
    except OSError:
        return False


def _owned_and_closed(path: Path) -> bool:
    info = path.stat()
    return info.st_uid in {0, os.geteuid()} and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
