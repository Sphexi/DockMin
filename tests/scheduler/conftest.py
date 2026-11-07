"""Scheduler fixtures: a fake clock over a migrated database, and a bound on every async test."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.db.engine import Database
from tests.scheduler.support import Rig, bound_async_tests

DIRECTORY = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    bound_async_tests(items, DIRECTORY)


@pytest.fixture
async def rig(database: Database) -> AsyncIterator[Rig]:
    """Return the builders over a migrated database; schedulers made through it are closed after."""
    built = Rig(database)
    yield built
    await built.close()
