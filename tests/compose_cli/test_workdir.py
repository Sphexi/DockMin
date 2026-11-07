"""Tests for app/compose_cli/workdir.py."""

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.compose_cli.workdir import (
    EMPTY_ENV_FILE,
    configure_compose_workdir,
    create_invocation_dir,
    invocation_dir,
    populate_invocation_dir,
    remove_invocation_dir,
    write_private,
)

PRIVATE_DIRECTORY = 0o700
PRIVATE_FILE = 0o600
NARROWING_UMASK = 0o277
OPEN_UMASK = 0o000


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return configure_compose_workdir(tmp_path)


@pytest.fixture(params=[NARROWING_UMASK, OPEN_UMASK], ids=["narrow-umask", "open-umask"])
def umask(request: pytest.FixtureRequest) -> Iterator[int]:
    previous = os.umask(request.param)
    yield request.param
    os.umask(previous)


def test_configure_creates_a_private_base_and_returns_its_resolved_path(tmp_path: Path) -> None:
    base = configure_compose_workdir(tmp_path)

    assert base == (tmp_path / "compose-work").resolve()
    assert mode_of(base) == PRIVATE_DIRECTORY


def test_configure_sweeps_whatever_a_crash_left_and_follows_no_symlink(tmp_path: Path) -> None:
    base = configure_compose_workdir(tmp_path)
    leftover = base / "run-abc" / "tls"
    leftover.mkdir(parents=True)
    (leftover / "key.pem").write_text("key", encoding="utf-8")
    (base / "stray-file").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("kept", encoding="utf-8")
    (base / "run-link").symlink_to(outside)
    base.chmod(0o755)

    swept = configure_compose_workdir(tmp_path)

    assert list(swept.iterdir()) == []
    assert mode_of(swept) == PRIVATE_DIRECTORY
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "kept"


def test_configure_refuses_a_base_that_is_a_symlink(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "compose-work").symlink_to(elsewhere)

    with pytest.raises(ValueError, match="symlink"):
        configure_compose_workdir(tmp_path)


def test_an_invocation_directory_is_private_and_lives_under_the_base_not_tmpdir(
    base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "tmpdir"
    elsewhere.mkdir()
    monkeypatch.setenv("TMPDIR", str(elsewhere))

    created = create_invocation_dir(base)

    assert created.parent == base
    assert mode_of(created) == PRIVATE_DIRECTORY
    assert list(elsewhere.iterdir()) == []


def test_populate_lays_out_home_config_temp_and_an_empty_env_file(base: Path) -> None:
    run_dir = create_invocation_dir(base)

    populate_invocation_dir(run_dir)

    assert sorted(p.name for p in run_dir.iterdir()) == ["config", "empty.env", "home", "tmp"]
    assert [mode_of(run_dir / name) for name in ("home", "config", "tmp")] == [
        PRIVATE_DIRECTORY
    ] * 3
    assert (run_dir / "config" / "config.json").read_text(encoding="utf-8") == "{}\n"
    assert mode_of(run_dir / "config" / "config.json") == PRIVATE_FILE
    assert (run_dir / EMPTY_ENV_FILE).read_bytes() == b""
    assert mode_of(run_dir / EMPTY_ENV_FILE) == PRIVATE_FILE


def test_the_invocation_directory_is_removed_after_normal_exit_and_after_an_error(
    base: Path,
) -> None:
    with invocation_dir(base) as completed:
        populate_invocation_dir(completed)
        assert completed.is_dir()
    with pytest.raises(RuntimeError), invocation_dir(base) as failed:
        populate_invocation_dir(failed)
        raise RuntimeError

    assert not completed.exists()
    assert not failed.exists()
    assert list(base.iterdir()) == []


def test_removing_a_directory_leaves_what_a_symlink_inside_it_points_at(
    base: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("kept", encoding="utf-8")
    run_dir = create_invocation_dir(base)
    (run_dir / "link").symlink_to(outside)

    remove_invocation_dir(run_dir)
    remove_invocation_dir(run_dir)

    assert not run_dir.exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "kept"


def test_write_private_creates_the_file_at_0600_whatever_the_umask(
    tmp_path: Path, umask: int
) -> None:
    target = tmp_path / "secret.pem"

    write_private(target, b"key-bytes")

    assert target.read_bytes() == b"key-bytes"
    assert mode_of(target) == PRIVATE_FILE


def test_write_private_never_overwrites_a_file_or_follows_a_symlink(tmp_path: Path) -> None:
    planted = tmp_path / "planted"
    planted.write_text("original", encoding="utf-8")
    victim = tmp_path / "victim"
    victim.write_text("victim", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(victim)
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "does-not-exist")

    with pytest.raises(FileExistsError):
        write_private(planted, b"new")
    with pytest.raises(FileExistsError):
        write_private(link, b"new")
    with pytest.raises(FileExistsError):
        write_private(dangling, b"new")

    assert planted.read_text(encoding="utf-8") == "original"
    assert victim.read_text(encoding="utf-8") == "victim"
    assert not (tmp_path / "does-not-exist").exists()
