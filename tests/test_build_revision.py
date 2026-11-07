"""build_revision: the commit the image records at build time."""

from pathlib import Path

import pytest

from app import version
from app.version import build_revision

REVISION = "0123456789abcdef0123456789abcdef01234567"


def point_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(version, "REVISION_FILE", path)


@pytest.mark.parametrize("text", [REVISION, f"{REVISION}\n", f" {REVISION}\n"])
def test_a_file_holding_a_full_lower_case_hash_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    recorded = tmp_path / "revision"
    recorded.write_text(text, encoding="utf-8")
    point_at(monkeypatch, recorded)

    assert build_revision() == REVISION


@pytest.mark.parametrize(
    "text",
    [
        "",
        REVISION[:-1],
        REVISION + "0",
        REVISION.upper(),
        "g" * len(REVISION),
        f"{REVISION}\nextra",
        "not a hash",
    ],
)
def test_a_file_holding_anything_else_is_no_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    recorded = tmp_path / "revision"
    recorded.write_text(text, encoding="utf-8")
    point_at(monkeypatch, recorded)

    assert build_revision() is None


def test_a_file_that_is_not_text_is_no_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = tmp_path / "revision"
    recorded.write_bytes(b"\xff\xfe" + REVISION.encode())
    point_at(monkeypatch, recorded)

    assert build_revision() is None


def test_an_absent_file_is_no_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    point_at(monkeypatch, tmp_path / "absent")

    assert build_revision() is None


def test_an_unreadable_file_is_no_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    point_at(monkeypatch, tmp_path)

    assert build_revision() is None
