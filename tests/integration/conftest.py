"""Integration fixtures: a bound on every async test."""

from pathlib import Path

import pytest

from tests.scheduler.support import bound_async_tests

DIRECTORY = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    bound_async_tests(items, DIRECTORY)
