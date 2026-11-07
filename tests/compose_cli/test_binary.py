"""Tests for app/compose_cli/binary.py."""

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from app.compose_cli.binary import ComposeBinary, locate, usable
from app.compose_cli.errors import ComposeBinaryInvalid

SAFE_FILE = 0o755
SAFE_DIRECTORY = 0o755
OTHER_USER = 4242
NO_PATH = ""

UNTRUSTED_MODES = [
    pytest.param(0o775, SAFE_DIRECTORY, id="group-writable-file"),
    pytest.param(0o757, SAFE_DIRECTORY, id="world-writable-file"),
    pytest.param(SAFE_FILE, 0o775, id="group-writable-directory"),
    pytest.param(SAFE_FILE, 0o777, id="world-writable-directory"),
    pytest.param(0o644, SAFE_DIRECTORY, id="not-executable"),
]

Installer = Callable[..., Path]


@pytest.fixture
def install(tmp_path: Path) -> Installer:
    def make(
        directory: str,
        *,
        name: str = "docker-compose",
        file_mode: int = SAFE_FILE,
        directory_mode: int = SAFE_DIRECTORY,
    ) -> Path:
        parent = tmp_path / directory
        parent.mkdir(exist_ok=True)
        parent.chmod(directory_mode)
        binary = parent / name
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(file_mode)
        return binary

    return make


def test_a_trusted_configured_binary_is_returned_as_given(install: Installer) -> None:
    binary = install("good")

    assert locate(binary) == ComposeBinary((str(binary),))


@pytest.mark.parametrize(("file_mode", "directory_mode"), UNTRUSTED_MODES)
def test_a_configured_binary_that_is_untrusted_or_not_executable_is_an_error(
    install: Installer, file_mode: int, directory_mode: int
) -> None:
    binary = install("bad", file_mode=file_mode, directory_mode=directory_mode)

    with pytest.raises(ComposeBinaryInvalid):
        locate(binary)


def test_a_configured_path_that_is_relative_missing_or_a_directory_is_an_error(
    install: Installer, tmp_path: Path
) -> None:
    install("good")

    with pytest.raises(ComposeBinaryInvalid):
        locate(Path("docker-compose"))
    with pytest.raises(ComposeBinaryInvalid):
        locate(tmp_path / "good" / "absent")
    with pytest.raises(ComposeBinaryInvalid):
        locate(tmp_path / "good")


def test_a_configured_binary_that_fails_is_not_replaced_by_a_search(install: Installer) -> None:
    bad = install("bad", file_mode=0o775)
    good = install("good")

    with pytest.raises(ComposeBinaryInvalid):
        locate(bad, search_paths=[good], path_variable=str(good.parent))


@pytest.mark.parametrize(("file_mode", "directory_mode"), UNTRUSTED_MODES)
def test_an_untrusted_candidate_in_the_search_paths_or_on_path_is_skipped(
    install: Installer, file_mode: int, directory_mode: int
) -> None:
    candidate = install("bad", file_mode=file_mode, directory_mode=directory_mode)

    assert locate(None, search_paths=[candidate], path_variable=NO_PATH) is None
    assert locate(None, search_paths=[], path_variable=str(candidate.parent)) is None


def test_an_untrusted_search_candidate_does_not_hide_a_later_trusted_one(
    install: Installer,
) -> None:
    bad = install("bad", file_mode=0o775)
    good = install("good")

    assert locate(None, search_paths=[bad, good], path_variable=NO_PATH) == ComposeBinary(
        (str(good),)
    )


def test_the_search_paths_are_tried_in_order_before_path(install: Installer) -> None:
    first = install("first")
    second = install("second")
    on_path = install("on-path")

    found = locate(None, search_paths=[second, first], path_variable=str(on_path.parent))

    assert found == ComposeBinary((str(second),))


def test_path_is_consulted_only_when_no_search_path_is_usable(install: Installer) -> None:
    on_path = install("on-path")
    missing = on_path.parent.parent / "absent" / "docker-compose"

    found = locate(None, search_paths=[missing], path_variable=str(on_path.parent))

    assert found == ComposeBinary((str(on_path),))


def test_nothing_found_returns_none(install: Installer) -> None:
    other_name = install("other", name="docker")

    assert locate(None, search_paths=[], path_variable=str(other_name.parent)) is None


def test_a_symlink_is_judged_by_its_target_and_returned_unresolved(
    install: Installer, tmp_path: Path
) -> None:
    target = install("target")
    link = tmp_path / "links" / "docker-compose"
    link.parent.mkdir(mode=SAFE_DIRECTORY)
    link.symlink_to(target)

    assert locate(link) == ComposeBinary((str(link),))


@pytest.mark.parametrize(("file_mode", "directory_mode"), UNTRUSTED_MODES[:4])
def test_a_symlink_in_a_safe_directory_to_an_untrusted_target_is_refused(
    install: Installer, tmp_path: Path, file_mode: int, directory_mode: int
) -> None:
    target = install("target", file_mode=file_mode, directory_mode=directory_mode)
    link = tmp_path / "links" / "docker-compose"
    link.parent.mkdir(mode=SAFE_DIRECTORY)
    link.symlink_to(target)

    with pytest.raises(ComposeBinaryInvalid):
        locate(link)
    assert locate(None, search_paths=[link], path_variable=NO_PATH) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root-owned files are trusted for every user")
def test_a_binary_owned_by_neither_root_nor_the_server_user_is_untrusted(
    install: Installer, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = install("good")
    monkeypatch.setattr(os, "geteuid", lambda: OTHER_USER)

    with pytest.raises(ComposeBinaryInvalid):
        locate(binary)
    assert locate(None, search_paths=[binary], path_variable=NO_PATH) is None


def test_an_untrusted_binary_is_logged_under_the_event_it_is_given(
    install: Installer, caplog: pytest.LogCaptureFixture
) -> None:
    candidate = install("bad", file_mode=0o775)

    assert usable(candidate, event="other_binary_untrusted") is False
    assert [record.getMessage() for record in caplog.records] == ["other_binary_untrusted"]
