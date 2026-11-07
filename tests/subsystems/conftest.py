"""Subsystem fixtures: the scheduler rig, subsystem dependencies and a bound on async tests."""

from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.services.object_locks import ObjectLockRegistry
from app.subsystems.base import SubsystemDeps
from app.subsystems.container_health_checker import ContainerHealthChecker
from app.subsystems.stack_restarter import StackRestarter
from tests.automation_support import Subject
from tests.scheduler.conftest import rig  # noqa: F401
from tests.scheduler.support import Rig, bound_async_tests
from tests.services.conftest import (  # noqa: F401
    access_template,
    clock,
    db,
    host,
    image_rig,
    runtime,
    stack_daemon,
    stack_rig,
    swarm,
    swarm_host,
    swarm_stack_rig,
    tls_workdir,
    vault,
)
from tests.services.conftest import rig as container_rig  # noqa: F401
from tests.stack_support import Rig as StackRig
from tests.swarm_updater_support import (
    Built,
    Shop,
    build_service,
    deployed_shop,
    inject_sleeper,
)
from tests.updater_support import World, build_shop, build_world

DIRECTORY = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    bound_async_tests(items, DIRECTORY)


@pytest.fixture
def wiring(rig: Rig, settings: Settings) -> tuple[SubsystemDeps, ObjectLockRegistry]:  # noqa: F811
    """Return subsystem dependencies over the rig's database, and the lock registry they share."""
    return rig.deps(settings)


@pytest.fixture
def deps(wiring: tuple[SubsystemDeps, ObjectLockRegistry]) -> SubsystemDeps:
    return wiring[0]


@pytest.fixture
def locks(wiring: tuple[SubsystemDeps, ObjectLockRegistry]) -> ObjectLockRegistry:
    return wiring[1]


@pytest.fixture
def health(container_rig: Any, settings: Settings) -> Subject:  # noqa: F811
    """The health checker against the container rig's fake daemon."""
    return Subject(container_rig, settings, ContainerHealthChecker)


@pytest.fixture
def restarter(stack_rig: StackRig, settings: Settings) -> Subject:  # noqa: F811
    """The stack restarter against the stack rig's fake daemon and scripted Compose."""
    return Subject(stack_rig, settings, StackRestarter)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Keep the settings' data directory apart from the one the stack rig makes."""
    path = tmp_path / "settings-data"
    path.mkdir()
    return path


@pytest.fixture
async def world(image_rig: Any, settings: Settings) -> World:  # noqa: F811
    """The updater over a running container `web` bound to a reference whose image moved."""
    return await build_world(image_rig, settings)


@pytest.fixture
async def shop(stack_rig: StackRig, settings: Settings) -> World:  # noqa: F811
    """The updater over a deployed Compose stack `shop` with two running members."""
    return await build_shop(stack_rig, settings)


@pytest.fixture
async def swarm_web(swarm: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Built:  # noqa: F811
    """The updater over a Swarm service `web` whose bound image moved, with no real waiting."""
    inject_sleeper(monkeypatch)
    return await build_service(swarm, settings)


@pytest.fixture
async def swarm_shop(swarm_stack_rig: StackRig, settings: Settings) -> Shop:  # noqa: F811
    """The updater over a deployed Swarm stack `Shop` of two services on bound references."""
    return await deployed_shop(swarm_stack_rig, settings)
