"""Fixtures for the Compose wrapper tests: the recording fake binary and its work base."""

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.compose_cli.binary import ComposeBinary
from app.compose_cli.cli import ComposeCli, ComposeProject
from app.compose_cli.workdir import configure_compose_workdir
from app.logging import Scrubber

FAKE_COMPOSE = Path(__file__).with_name("fake_compose.py")
FIXTURES = Path(__file__).with_name("fixtures")
OPERATION_TIMEOUT_SECONDS = 20.0
PROJECT_NAME = "stack-a"


@pytest.fixture
def sample() -> Callable[[str], bytes]:
    return lambda name: (FIXTURES / name).read_bytes()


@pytest.fixture
def fake_binary() -> ComposeBinary:
    return ComposeBinary((sys.executable, "-I", str(FAKE_COMPOSE)))


@pytest.fixture
def work_base(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    return configure_compose_workdir(data)


@pytest.fixture
def cli(fake_binary: ComposeBinary, work_base: Path) -> ComposeCli:
    return ComposeCli(fake_binary, work_base=work_base, operation_timeout=OPERATION_TIMEOUT_SECONDS)


@pytest.fixture
def project(tmp_path: Path) -> ComposeProject:
    directory = tmp_path / "project"
    directory.mkdir()
    compose_file = directory / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    return ComposeProject(PROJECT_NAME, directory, compose_file)


@pytest.fixture
def scrubber() -> Scrubber:
    return Scrubber()


@pytest.fixture
def record_path(tmp_path: Path) -> Path:
    return tmp_path / "records.jsonl"


@pytest.fixture
def records(record_path: Path) -> Callable[[], list[dict[str, Any]]]:
    def read() -> list[dict[str, Any]]:
        lines = record_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    return read
