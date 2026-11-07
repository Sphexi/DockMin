"""Tests for ComposeCli.merged_config: Compose files merged without interpolation."""

import sys
from pathlib import Path

import pytest
import yaml

from app.compose_cli.binary import ComposeBinary
from app.compose_cli.cli import MERGE_TAIL, ComposeCli
from app.compose_cli.errors import ComposeCommandFailed
from tests.stack_support import SCRIPTED_COMPOSE

BASE = "services:\n  web:\n    image: nginx:1\n"
OVERRIDE = "services:\n  web:\n    ports:\n      - '8080:80'\n"
NOT_YAML = "services: [unclosed\n"


@pytest.fixture
def merging(work_base: Path) -> ComposeCli:
    binary = ComposeBinary((sys.executable, "-I", str(SCRIPTED_COMPOSE)))
    return ComposeCli(binary, work_base=work_base)


def is_empty(directory: Path) -> bool:
    return not any(directory.iterdir())


def test_the_merge_command_carries_the_documented_flags_and_no_variable_resolution() -> None:
    assert MERGE_TAIL == ("config", "--no-interpolate", "--no-path-resolution", "--format", "yaml")


async def test_files_are_merged_in_order_and_the_work_directory_is_removed(
    merging: ComposeCli, work_base: Path
) -> None:
    merged = await merging.merged_config("shop", [BASE, OVERRIDE])

    assert yaml.safe_load(merged) == {
        "name": "shop",
        "services": {"web": {"image": "nginx:1", "ports": ["8080:80"]}},
    }
    assert is_empty(work_base)


async def test_a_file_compose_refuses_raises_and_leaves_no_work_directory(
    merging: ComposeCli, work_base: Path
) -> None:
    with pytest.raises(ComposeCommandFailed):
        await merging.merged_config("shop", [BASE, NOT_YAML])

    assert is_empty(work_base)
