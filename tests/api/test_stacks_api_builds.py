"""The stack JSON routes never build: a file with a service that builds is refused."""

from httpx import AsyncClient

from app.services.stack_deploy import file_digest
from tests.api.test_stacks_api import BAD_REQUEST, base, stack
from tests.api.test_stacks_api_errors import admin_api, stack_host  # noqa: F401
from tests.docker_engine.container_engine import ContainerEngine
from tests.web.environment_support import World
from tests.web.stack_file_support import seed_stack, stack_rows

BUILD_FILE = "services:\n  web:\n    build: ./web\n"
REASON = "build_not_available"


async def test_create_and_redeploy_of_a_stack_that_builds_are_refused_with_one_reason(
    admin_api: AsyncClient,  # noqa: F811
    world: World,
    stack_host: ContainerEngine,  # noqa: F811
) -> None:
    await seed_stack(world.app, world.remote_id, "built", BUILD_FILE)
    url = base(world)
    [row] = await stack_rows(world, "SELECT inputs_digest FROM stack WHERE name = 'built'")

    created = await admin_api.post(url, json=stack("fresh", BUILD_FILE))
    redeployed = await admin_api.post(
        f"{url}/built/redeploy",
        json={
            "expected_file_digest": file_digest(BUILD_FILE),
            "expected_inputs_digest": row["inputs_digest"],
        },
    )

    assert created.status_code == redeployed.status_code == BAD_REQUEST
    assert created.json()["error"]["reason"] == redeployed.json()["error"]["reason"] == REASON
    assert stack_host.calls("POST", "/images/create") == []
