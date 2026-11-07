"""Per-invocation private work directories for Compose processes."""

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from app.logging import get_logger

logger = get_logger(__name__)

WORK_DIRECTORY_NAME: Final = "compose-work"
HOME_DIRECTORY: Final = "home"
CONFIG_DIRECTORY: Final = "config"
TEMP_DIRECTORY: Final = "tmp"
EMPTY_ENV_FILE: Final = "empty.env"

_PRIVATE_DIRECTORY_MODE: Final = 0o700
_PRIVATE_FILE_MODE: Final = 0o600
_EMPTY_DOCKER_CONFIG: Final = b"{}\n"


def configure_compose_workdir(data_dir: Path) -> Path:
    """Create the private base for work directories and remove whatever a crash left in it.

    Call once at startup, before the first invocation.
    """
    base = data_dir / WORK_DIRECTORY_NAME
    if base.is_symlink():
        raise ValueError("Compose work directory must not be a symlink")
    base.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    # mkdir's mode is masked by umask and ignored for an existing directory.
    base.chmod(_PRIVATE_DIRECTORY_MODE)
    resolved = base.resolve()
    for entry in resolved.iterdir():
        _remove(entry)
    return resolved


def create_invocation_dir(base: Path) -> Path:
    """Create a fresh `0700` directory beneath base, never consulting TMPDIR."""
    return Path(tempfile.mkdtemp(prefix="run-", dir=base))


def remove_invocation_dir(path: Path) -> None:
    """Delete an invocation directory; a failure is logged and left to the startup sweep."""
    _remove(path)


@contextmanager
def invocation_dir(base: Path) -> Iterator[Path]:
    """Yield a fresh private directory beneath base and delete it on exit."""
    path = create_invocation_dir(base)
    try:
        yield path
    finally:
        remove_invocation_dir(path)


def populate_invocation_dir(path: Path) -> None:
    """Create the empty home, Docker config, temp directory and empty env file Compose is given."""
    (path / HOME_DIRECTORY).mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    (path / CONFIG_DIRECTORY).mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    (path / TEMP_DIRECTORY).mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    write_private(path / CONFIG_DIRECTORY / "config.json", _EMPTY_DOCKER_CONFIG)
    write_private(path / EMPTY_ENV_FILE, b"")


def write_sources_override(workdir: Path, document: str, name: str) -> Path:
    """Write the product-authored override Compose file into the work directory at `0600`."""
    path = workdir / name
    write_private(path, document.encode("utf-8"))
    return path


def write_private(path: Path, data: bytes) -> None:
    """Create path at `0600` and write data; an existing file or symlink at path is an error."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, _PRIVATE_FILE_MODE), "wb") as handle:
        os.fchmod(handle.fileno(), _PRIVATE_FILE_MODE)
        handle.write(data)


def _remove(path: Path) -> None:
    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)
    except OSError:
        logger.warning("compose_workdir_cleanup_failed", extra={"path": str(path)})
