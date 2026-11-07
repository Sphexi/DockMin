"""Fixtures for the notification clients and a bound on every async test."""

from pathlib import Path

import pytest

from tests.notification_fixtures import (  # noqa: F401
    certificates,
    poked,
    receivers,
    sinks,
    trusting,
)
from tests.scheduler.conftest import rig  # noqa: F401
from tests.scheduler.support import bound_async_tests

DIRECTORY = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    bound_async_tests(items, DIRECTORY)
