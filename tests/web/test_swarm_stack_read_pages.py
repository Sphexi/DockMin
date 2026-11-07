"""Reconstruct from the cluster: the result page and Adopt as is for a Swarm namespace."""

import re

from httpx import AsyncClient, Response

from tests.auth_support import csrf_form
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.swarm_support import NAMESPACE_LABEL
from tests.web.environment_support import World
from tests.web.flash_support import location
from tests.web.registry_support import visible_text

OK, SEE_OTHER = 200, 303
NAMESPACE = "Cli"
READ_TOKEN = re.compile(r'name="read_token" value="([^"]+)"')


def swarm_base(world: World) -> str:
    return f"/environments/{world.remote_id}/swarm/stacks/-/adopt/{NAMESPACE}"


def text_of(response: Response) -> str:
    return " ".join(visible_text(response.text).split())


async def test_a_namespace_is_reconstructed_shown_and_adopted_as_is(
    admin_client: AsyncClient,
    swarm_stacks_world: World,
    swarm_host: SwarmEngine,
) -> None:
    world = swarm_stacks_world
    swarm_host.add_service(
        {
            "Name": f"{NAMESPACE}_app",
            "Labels": {NAMESPACE_LABEL: NAMESPACE},
            "Mode": {"Replicated": {"Replicas": 1}},
            "TaskTemplate": {"ContainerSpec": {"Image": "busybox:latest"}},
        }
    )
    versions = {i: s["Version"]["Index"] for i, s in swarm_host.services.items()}
    writes = len(swarm_host.mutating())

    paste = await admin_client.get(swarm_base(world))
    result = await admin_client.post(f"{swarm_base(world)}/read", data=csrf_form(admin_client))
    token = READ_TOKEN.search(result.text)
    assert token is not None
    adopted = await admin_client.post(
        f"{swarm_base(world)}/as-is", data=csrf_form(admin_client, read_token=token.group(1))
    )

    text = text_of(result)
    assert "Reconstruct from the cluster" in paste.text
    assert result.status_code == OK
    assert "Reconstructed from the cluster's service specifications" in text
    assert "Adopt Cli as is" in text and "app" in text
    assert "replaces the running tasks of every service" in text
    assert adopted.status_code == SEE_OTHER
    assert location(adopted) == f"/environments/{world.remote_id}/swarm/stacks/{NAMESPACE}"
    assert len(swarm_host.mutating()) == writes
    assert versions == {i: s["Version"]["Index"] for i, s in swarm_host.services.items()}
