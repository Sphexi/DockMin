"""Tests for the template environment built by app/web/templating.py."""

import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from jinja2 import Environment

from app.web import templating
from app.web.templating import build_environment, build_templates

ASSET = "app.css"
UNLISTED_ASSET = "later.css"
FIRST_MODIFIED = 1_700_000_000
SECOND_MODIFIED = 1_700_000_500


@pytest.fixture
def static_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "static"
    directory.mkdir()
    (directory / ASSET).write_text("body {}", encoding="utf-8")
    os.utime(directory / ASSET, (FIRST_MODIFIED, FIRST_MODIFIED))
    monkeypatch.setattr(templating, "STATIC_DIR", directory)
    return directory


def static_url(environment: Environment, path: str) -> str:
    context: Any = {"request": SimpleNamespace(scope={})}
    url: str = cast(Callable[..., str], environment.globals["static_path"])(context, path)
    return url


def test_templates_are_not_reloaded_unless_development_checks_are_on() -> None:
    assert build_environment().auto_reload is False
    assert build_environment(development_checks=False).auto_reload is False
    assert build_environment(development_checks=True).auto_reload is True
    assert build_templates(development_checks=True).env.auto_reload is True


def test_a_listed_static_file_keeps_the_version_it_had_when_the_environment_was_built(
    static_dir: Path,
) -> None:
    environment = build_environment()
    os.utime(static_dir / ASSET, (SECOND_MODIFIED, SECOND_MODIFIED))

    assert static_url(environment, ASSET) == f"/static/{ASSET}?v={FIRST_MODIFIED}"


def test_a_static_file_missing_from_the_map_is_versioned_from_the_file_itself(
    static_dir: Path,
) -> None:
    environment = build_environment()
    (static_dir / UNLISTED_ASSET).write_text("p {}", encoding="utf-8")
    os.utime(static_dir / UNLISTED_ASSET, (SECOND_MODIFIED, SECOND_MODIFIED))

    assert (
        static_url(environment, UNLISTED_ASSET) == f"/static/{UNLISTED_ASSET}?v={SECOND_MODIFIED}"
    )
    assert static_url(environment, "absent.css") == "/static/absent.css?v=0"


def test_with_development_checks_a_static_file_follows_its_modification_time(
    static_dir: Path,
) -> None:
    environment = build_environment(development_checks=True)
    first = static_url(environment, ASSET)
    os.utime(static_dir / ASSET, (SECOND_MODIFIED, SECOND_MODIFIED))

    assert first == f"/static/{ASSET}?v={FIRST_MODIFIED}"
    assert static_url(environment, ASSET) == f"/static/{ASSET}?v={SECOND_MODIFIED}"
