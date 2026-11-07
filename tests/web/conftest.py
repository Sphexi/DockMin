"""Fixtures for the web tests: daemons, stacks, and the clocks notices and results run on."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.docker_engine.registry import EngineClientRegistry
from app.web.results import ResultStore
from tests.auth_support import STANDARD_ID
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.jwt_support import Signer, ec_signer, jwt_env, write_jwks
from tests.stack_support import install_scripted_compose
from tests.swarm_stack_support import install_swarm_compose
from tests.web.environment_support import World
from tests.web.flash_support import Ticker
from tests.web.image_support import stock_images


@pytest.fixture
def flash_clock(started_app: FastAPI) -> Ticker:
    """A clock the notice cookie is stamped and checked against."""
    ticker = Ticker()
    started_app.state.flash_clock = ticker
    return ticker


@pytest.fixture
def result_clock(started_app: FastAPI) -> Ticker:
    """A clock the stored results expire against; the store starts empty."""
    ticker = Ticker(start=0.0)
    started_app.state.results = ResultStore(clock=ticker)
    return ticker


@pytest.fixture
def daemon(container_host: ContainerEngine) -> ContainerEngine:
    """The seeded daemon with a used two-tag image, a pulled image, a dangling one and a spare."""
    return stock_images(container_host)


@pytest.fixture
async def stack_host(world: World, container_host: ContainerEngine) -> ContainerEngine:
    """The seeded daemon holding `nginx:1`; scripted Compose runs change it."""
    container_host.add_image("nginx:1")
    await install_scripted_compose(world.app.state.engine.compose, container_host)
    return container_host


@pytest.fixture
async def swarm_host(world: World) -> AsyncIterator[SwarmEngine]:
    """Serve every seeded environment from one fake Swarm manager.

    The remote environment is typed as a manager and open to the standard user; the local one
    is a worker and the unreachable one has never been contacted.
    """
    fake = SwarmEngine()
    engine = world.app.state.engine
    registry = EngineClientRegistry(lambda settings: fake.bundle())
    engine.registry = registry
    async with world.app.state.database.session() as db:
        await EnvironmentAccessRepository(db).add_user("environment", world.remote_id, STANDARD_ID)
        await db.execute(
            "UPDATE environment SET type = 'swarm', swarm_state = 'active', "
            "swarm_control_available = 1 WHERE id = ?",
            (world.remote_id,),
        )
        await db.execute(
            "UPDATE environment SET type = 'standalone', swarm_state = 'active', "
            "swarm_control_available = 0 WHERE id = ?",
            (world.local_id,),
        )
        await db.execute("UPDATE environment SET type = NULL WHERE id = ?", (world.down_id,))
    yield fake
    fake.release_gates()
    await registry.aclose()


@pytest.fixture
async def swarm_stacks_world(
    world: World, swarm_host: SwarmEngine, tmp_path: Path
) -> AsyncIterator[World]:
    """The seeded world with a Swarm manager, and a Compose that only resolves files."""
    compose = world.app.state.engine.compose
    directory = tmp_path / "swarm-compose"
    directory.mkdir()
    compose.use_binary(install_swarm_compose(directory))
    await compose.detect()
    yield world
    jobs = world.app.state.engine.stack_jobs
    for job in list(jobs._jobs.values()):
        if job.task is not None and not job.task.done():
            job.task.cancel()


@pytest.fixture
def signer() -> Signer:
    return ec_signer()


@pytest.fixture
def extra_env(request: pytest.FixtureRequest) -> dict[str, str]:
    """Settings a test adds to the access platform ones, passed with an indirect parameter."""
    params: dict[str, str] = getattr(request, "param", {})
    return params


@pytest.fixture
def key_file(tmp_path: Path, signer: Signer) -> Path:
    return write_jwks(tmp_path / "jwks.json", signer)


@pytest.fixture
def platform_env(env: dict[str, str], key_file: Path, extra_env: dict[str, str]) -> dict[str, str]:
    return env | jwt_env(key_file) | extra_env
