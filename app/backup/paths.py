"""Work and restore directories under the data directory, and the swap that replaces live data."""

import os
import re
import secrets
import shutil
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from app.config import DB_FILENAME

WORK_DIRECTORY: Final = "backup-work"
RESTORE_DIRECTORY: Final = "restore"
STACKS_DIRECTORY: Final = "stacks"
PREVIOUS_DIRECTORY: Final = "previous"
STAGING_DIRECTORY: Final = "staging"
REJECTED_DIRECTORY: Final = "rejected"
STAGED_DATABASE: Final = DB_FILENAME
DIRECTORY_MODE: Final = 0o700
FILE_MODE: Final = 0o600

_RESTORE_ID = re.compile(r"[0-9a-f]{32}")
_DATABASE_SIDECARS = (f"{DB_FILENAME}-wal", f"{DB_FILENAME}-shm")


class UnsafePath(Exception):
    """A path the library works in is a symlink, not a directory, or not empty."""


class SwapFailed(Exception):
    """A swap or revert step failed; journal holds the moves still in effect."""

    def __init__(self, message: str, journal: "SwapJournal") -> None:
        super().__init__(message)
        self.message = message
        self.journal = journal


@dataclass(slots=True)
class SwapJournal:
    """Completed moves in order, so a revert undoes exactly those."""

    moves: list[tuple[Path, Path]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SwapPlan:
    """The live entries that move aside, where they go, and the staged data that replaces them."""

    live: tuple[Path, ...]
    previous: Path
    staged: Path
    data_dir: Path


def restore_leftover(data_dir: Path) -> Path | None:
    """Return the first previous or rejected directory a restore left under data_dir, if any."""
    try:
        entries = sorted((data_dir / RESTORE_DIRECTORY).iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return None
    for entry in entries:
        for name in (PREVIOUS_DIRECTORY, REJECTED_DIRECTORY):
            if os.path.lexists(entry / name):
                return entry / name
    return None


def ensure_private_directory(path: Path) -> Path:
    """Create path as a 0700 directory if absent; refuse a symlink or a non-directory."""
    if path.is_symlink():
        raise UnsafePath("the directory is a symbolic link")
    if not path.exists():
        path.mkdir(mode=DIRECTORY_MODE)
    if not path.is_dir():
        raise UnsafePath("the path is not a directory")
    if stat.S_IMODE(path.stat().st_mode) != DIRECTORY_MODE:
        path.chmod(DIRECTORY_MODE)
    return path


def new_work_directory(data_dir: Path) -> Path:
    """Create and return a fresh 0700 directory under <data_dir>/backup-work."""
    base = ensure_private_directory(data_dir / WORK_DIRECTORY)
    return ensure_private_directory(base / secrets.token_hex(16))


def new_restore_directory(data_dir: Path, restore_id: str) -> Path:
    """Create and return <data_dir>/restore/<restore_id> as a 0700 directory."""
    if _RESTORE_ID.fullmatch(restore_id) is None:
        raise ValueError("restore_id must be 32 lowercase hexadecimal characters")
    base = ensure_private_directory(data_dir / RESTORE_DIRECTORY)
    return ensure_private_directory(base / restore_id)


def remove_directory(path: Path) -> None:
    """Delete a directory tree that is not itself a symlink; a missing path is fine."""
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _live_candidates(data_dir: Path) -> tuple[Path, ...]:
    return (
        data_dir / DB_FILENAME,
        *(data_dir / name for name in _DATABASE_SIDECARS),
        data_dir / STACKS_DIRECTORY,
    )


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def plan_swap(data_dir: Path, restore_dir: Path) -> SwapPlan:
    """Name the live entries present now, where they go, and the staged data that replaces them."""
    return SwapPlan(
        live=tuple(path for path in _live_candidates(data_dir) if _exists(path)),
        previous=restore_dir / PREVIOUS_DIRECTORY,
        staged=restore_dir / STAGING_DIRECTORY,
        data_dir=data_dir,
    )


def _fsync_directory(path: Path) -> None:
    with suppress(OSError):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _move(source: Path, target: Path, journal: SwapJournal) -> None:
    if _exists(target):
        raise OSError(f"{target.name} already exists")
    source.rename(target)
    journal.moves.append((source, target))
    _fsync_directory(source.parent)
    _fsync_directory(target.parent)


def _staged_entries(staged: Path) -> list[tuple[Path, str]]:
    database = staged / STAGED_DATABASE
    if database.is_symlink() or not database.is_file():
        raise OSError("the staged database is missing")
    if any(_exists(staged / name) for name in _DATABASE_SIDECARS):
        raise OSError("the staged database has unmerged journal files")
    entries = [(database, DB_FILENAME)]
    stacks = staged / STACKS_DIRECTORY
    if stacks.is_symlink() or not stacks.is_dir():
        raise OSError("the staged stack directory is missing")
    entries.append((stacks, STACKS_DIRECTORY))
    return entries


def apply_swap(plan: SwapPlan) -> SwapJournal:
    """Move the live entries into plan.previous, then the staged data into place.

    Raises SwapFailed carrying the moves completed before the failure; nothing is deleted. Every
    entry of the live data directory is moved, including database journal files created after the
    plan was made, so no stale journal sits beside the restored database.
    """
    journal = SwapJournal()
    try:
        entries = _staged_entries(plan.staged)
        ensure_private_directory(plan.previous)
        for source in _live_candidates(plan.data_dir):
            if _exists(source):
                _move(source, plan.previous / source.name, journal)
        for source, name in entries:
            _move(source, plan.data_dir / name, journal)
    except (OSError, UnsafePath) as error:
        raise SwapFailed(f"the data directory could not be swapped ({error})", journal) from None
    return journal


def _undo(moves: list[tuple[Path, Path]]) -> SwapJournal:
    remaining = SwapJournal()
    failed = False
    for source, target in reversed(moves):
        if failed:
            remaining.moves.insert(0, (source, target))
            continue
        try:
            if _exists(source):
                raise OSError(f"{source.name} is already present")
            target.rename(source)
            _fsync_directory(source.parent)
        except OSError:
            failed = True
            remaining.moves.insert(0, (source, target))
    return remaining


def revert_swap(journal: SwapJournal) -> None:
    """Undo the journaled moves in reverse order; SwapFailed names the moves left in effect."""
    remaining = _undo(journal.moves)
    if remaining.moves:
        raise SwapFailed("the swap could not be reverted", remaining)
    journal.moves.clear()


def reject_restored(plan: SwapPlan, journal: SwapJournal, rejected: Path) -> None:
    """Move whatever now occupies the live paths into rejected, then put the previous data back.

    Raises SwapFailed when a move fails. Database journal files the restored data created are
    moved with it.
    """
    moved_in = SwapJournal()
    try:
        ensure_private_directory(rejected)
        for path in _live_candidates(plan.data_dir):
            if _exists(path):
                _move(path, rejected / path.name, moved_in)
    except (OSError, UnsafePath) as error:
        raise SwapFailed(
            f"the restored data could not be moved aside ({error})",
            SwapJournal([*journal.moves, *moved_in.moves]),
        ) from None
    originals = [move for move in journal.moves if plan.previous in move[1].parents]
    remaining = _undo(originals)
    if remaining.moves:
        raise SwapFailed("the previous data could not be put back", remaining)
    journal.moves.clear()


def open_private_file(path: Path) -> int:
    """Create path as a new 0600 file, refusing an existing path or a symlink; return its fd."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
