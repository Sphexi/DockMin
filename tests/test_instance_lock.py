"""The data directory lock: one process at a time, and only a file this user alone owns."""

import errno
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import app.instance_lock as instance_lock_module
from app.instance_lock import LOCK_FILENAME, InstanceLock, InstanceLocked, LockFileUnsafe

ROOT = Path(__file__).parents[1]
OWNER_ONLY_FILE = 0o600
GROUP_AND_WORLD_WRITABLE = 0o777
REFUSED_EXIT_CODE = 7
SUBPROCESS_SECONDS = 30
ANOTHER_USER = 4_000_000_000
TRY_TO_TAKE_THE_LOCK = """
import sys
from pathlib import Path
from app.instance_lock import InstanceLock, InstanceLocked
try:
    InstanceLock(Path(sys.argv[1])).acquire()
except InstanceLocked:
    sys.exit(7)
"""


def test_a_second_holder_on_one_directory_is_refused_and_the_first_is_unaffected(
    data_dir: Path,
) -> None:
    first, second = InstanceLock(data_dir), InstanceLock(data_dir)
    first.acquire()

    with pytest.raises(InstanceLocked, match="Another dockmin process"):
        second.acquire()

    assert first.held and not second.held


def test_a_released_lock_can_be_taken_again_and_releasing_twice_is_harmless(
    data_dir: Path,
) -> None:
    first, second = InstanceLock(data_dir), InstanceLock(data_dir)
    first.acquire()
    first.release()
    first.release()

    second.acquire()

    assert second.held and not first.held


def test_taking_a_lock_already_held_changes_nothing(data_dir: Path) -> None:
    lock = InstanceLock(data_dir)
    lock.acquire()

    lock.acquire()

    assert lock.held


def test_the_lock_file_is_owner_only_and_nothing_is_written_into_it(data_dir: Path) -> None:
    lock = InstanceLock(data_dir)

    lock.acquire()

    assert lock.path == data_dir / LOCK_FILENAME
    assert stat.S_IMODE(lock.path.stat().st_mode) == OWNER_ONLY_FILE
    assert lock.path.stat().st_size == 0


def test_a_lock_file_with_looser_permissions_is_tightened(data_dir: Path) -> None:
    path = data_dir / LOCK_FILENAME
    path.touch()
    path.chmod(0o644)

    InstanceLock(data_dir).acquire()

    assert stat.S_IMODE(path.stat().st_mode) == OWNER_ONLY_FILE


def test_a_missing_data_directory_is_created(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "not" / "yet")

    lock.acquire()

    assert lock.path.is_file()


def test_another_process_on_the_same_directory_is_refused(data_dir: Path) -> None:
    held = InstanceLock(data_dir)
    held.acquire()

    completed = subprocess.run(
        [sys.executable, "-c", TRY_TO_TAKE_THE_LOCK, str(data_dir)],
        capture_output=True,
        timeout=SUBPROCESS_SECONDS,
        cwd=data_dir,
        env={"PYTHONPATH": str(ROOT)},
        check=False,
    )

    assert completed.returncode == REFUSED_EXIT_CODE, completed.stderr.decode()


def test_a_symlinked_lock_file_is_refused_and_its_target_left_alone(data_dir: Path) -> None:
    target = data_dir / "elsewhere"
    target.write_text("keep", encoding="utf-8")
    (data_dir / LOCK_FILENAME).symlink_to(target)

    with pytest.raises(OSError) as refused:
        InstanceLock(data_dir).acquire()

    assert refused.value.errno == errno.ELOOP
    assert target.read_text(encoding="utf-8") == "keep"


def test_a_lock_file_with_another_hard_link_is_refused(data_dir: Path) -> None:
    path = data_dir / LOCK_FILENAME
    path.touch()
    os.link(path, data_dir / "second-name")

    with pytest.raises(LockFileUnsafe):
        InstanceLock(data_dir).acquire()


def test_a_lock_file_owned_by_another_user_is_refused(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (data_dir / LOCK_FILENAME).touch()
    monkeypatch.setattr(os, "geteuid", lambda: ANOTHER_USER)

    with pytest.raises(LockFileUnsafe):
        InstanceLock(data_dir).acquire()


def test_a_lock_path_that_is_not_a_regular_file_is_refused(data_dir: Path) -> None:
    os.mkfifo(data_dir / LOCK_FILENAME)

    with pytest.raises(OSError) as refused:
        InstanceLock(data_dir).acquire()

    assert refused.value.errno == errno.EINVAL


def test_a_lock_path_that_is_a_directory_is_refused(data_dir: Path) -> None:
    (data_dir / LOCK_FILENAME).mkdir()

    with pytest.raises(OSError):
        InstanceLock(data_dir).acquire()


def test_a_data_directory_others_can_write_to_is_warned_about(data_dir: Path, logged: Any) -> None:
    data_dir.chmod(GROUP_AND_WORLD_WRITABLE)

    InstanceLock(data_dir).acquire()

    assert [record["path"] for record in logged("data_dir_writable_by_others")] == [str(data_dir)]


def test_a_private_data_directory_is_not_warned_about(data_dir: Path, logged: Any) -> None:
    data_dir.chmod(0o700)

    InstanceLock(data_dir).acquire()

    assert logged("data_dir_writable_by_others") == []


def test_a_platform_without_file_locking_runs_unlocked_and_says_so(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, logged: Any
) -> None:
    monkeypatch.setattr(instance_lock_module, "_load_fcntl", lambda: None)
    lock = InstanceLock(data_dir)

    lock.acquire()

    assert not lock.held
    assert not lock.path.exists()
    assert len(logged("instance_lock_unavailable")) == 1


def test_a_refusal_is_logged_with_the_path(data_dir: Path, logged: Any) -> None:
    InstanceLock(data_dir).acquire()

    with pytest.raises(InstanceLocked):
        InstanceLock(data_dir).acquire()

    assert [record["path"] for record in logged("instance_already_running")] == [
        str(data_dir / LOCK_FILENAME)
    ]
