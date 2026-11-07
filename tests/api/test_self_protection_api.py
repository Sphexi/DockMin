"""The JSON routes answer 409 with a stable reason for the container dockmin runs in."""

from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from app.auth.errors import SelfProtected
from tests.api.test_containers_api import RECREATED, admin_api, base, recreate_url  # noqa: F401
from tests.api.test_stacks_api import (
    WEB_SERVICE,
    ZERO_DIGEST,
    scripted,
    stack,
)
from tests.api.test_stacks_api import base as stack_base
from tests.container_support import WEB
from tests.docker_engine.container_engine import ContainerEngine
from tests.provisioning_support import IMAGE, OLD, seed_recreatable
from tests.self_support import home_identity
from tests.stack_support import install_scripted_compose
from tests.web.environment_support import World

OK, CREATED, CONFLICT = 200, 201, 409
SELF_PROTECTED = "self_protected"
STACK_NAME = "alpha"


def refusal_of(response: Any) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    error: dict[str, Any] = body.get("error", body)
    return error


@pytest.mark.parametrize("action", ["stop", "restart"])
async def test_an_action_on_dockmins_container_answers_409_with_the_stable_reason(
    admin_api: AsyncClient,  # noqa: F811
    world: World,
    container_host: ContainerEngine,
    action: str,
) -> None:
    world.app.state.self_identity = home_identity(world.remote_id, container=WEB)

    refused = await admin_api.post(f"{base(world)}/{WEB}/{action}")

    assert refused.status_code == CONFLICT
    assert refusal_of(refused) == {
        "status": CONFLICT,
        "message": str(SelfProtected.container()),
        "reason": SELF_PROTECTED,
    }
    assert container_host.mutations() == []


async def test_starting_dockmins_container_is_not_refused(
    admin_api: AsyncClient,  # noqa: F811
    world: World,
    container_host: ContainerEngine,
) -> None:
    world.app.state.self_identity = home_identity(world.remote_id, container=WEB)

    started = await admin_api.post(f"{base(world)}/{WEB}/start")

    assert started.status_code == OK
    assert started.json()["result"] == "unchanged"


async def test_recreating_dockmins_container_answers_409_and_leaves_it_in_place(
    admin_api: AsyncClient,  # noqa: F811
    world: World,
    container_host: ContainerEngine,
) -> None:
    container_host.add_image(IMAGE)
    seed_recreatable(container_host, name=RECREATED)
    world.app.state.self_identity = home_identity(world.remote_id, container=OLD)

    refused = await admin_api.post(recreate_url(world), json={"expected_name": RECREATED})

    assert refused.status_code == CONFLICT
    assert refusal_of(refused)["reason"] == SELF_PROTECTED
    assert container_host.mutations() == []
    assert OLD in container_host.boxes


class TestStacks:
    @pytest.fixture
    async def deployed(
        self,
        admin_api: AsyncClient,  # noqa: F811
        world: World,
        container_host: ContainerEngine,
        tmp_path: Path,
    ) -> ContainerEngine:
        container_host.add_image(IMAGE)
        await install_scripted_compose(world.app.state.engine.compose, container_host)
        text = scripted(WEB_SERVICE, record=str(tmp_path / "calls.jsonl"))
        created = await admin_api.post(stack_base(world), json=stack(STACK_NAME, text))
        assert created.status_code == CREATED
        world.app.state.self_identity = home_identity(world.remote_id, compose_project=STACK_NAME)
        return container_host

    async def test_stopping_the_stack_dockmin_belongs_to_answers_409_with_the_stable_reason(
        self,
        admin_api: AsyncClient,  # noqa: F811
        world: World,
        deployed: ContainerEngine,
    ) -> None:
        before = deployed.mutations()

        refused = await admin_api.post(f"{stack_base(world)}/{STACK_NAME}/stop")

        assert refused.status_code == CONFLICT
        assert refusal_of(refused)["reason"] == SELF_PROTECTED
        assert deployed.mutations() == before

    async def test_redeploying_it_is_refused_before_the_review_digest_is_compared(
        self,
        admin_api: AsyncClient,  # noqa: F811
        world: World,
        deployed: ContainerEngine,
    ) -> None:
        refused = await admin_api.post(
            f"{stack_base(world)}/{STACK_NAME}/redeploy",
            json={"expected_file_digest": ZERO_DIGEST, "expected_inputs_digest": None},
        )

        assert refused.status_code == CONFLICT
        assert refusal_of(refused)["reason"] == SELF_PROTECTED

    async def test_starting_it_is_allowed(
        self,
        admin_api: AsyncClient,  # noqa: F811
        world: World,
        deployed: ContainerEngine,
    ) -> None:
        started = await admin_api.post(f"{stack_base(world)}/{STACK_NAME}/start")

        assert started.status_code == OK
