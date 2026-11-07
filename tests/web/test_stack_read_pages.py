"""Read from host and Reconstruct from the cluster: the result pages and Adopt as is."""

import re
import sys
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient, Response

from app.compose_cli.binary import ComposeBinary
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.docker_engine.registry import EngineClientRegistry
from tests.auth_support import STANDARD_ID, csrf_form
from tests.host_read_support import (
    PROJECT,
    WORKING_DIR,
    ReaderEngine,
    add_project_container,
    compose_labels,
)
from tests.stack_files_support import FILES_COMPOSE
from tests.stack_support import SCRIPTED_COMPOSE, SECRET_SENTINEL, stopped_stack_jobs  # noqa: F401
from tests.web.environment_support import World
from tests.web.flash_support import flash_texts, land, location
from tests.web.registry_support import visible_text

pytestmark = pytest.mark.usefixtures("stopped_stack_jobs")

OK, SEE_OTHER, NOT_FOUND, CONFLICT = 200, 303, 404, 409
NAMESPACE = "Cli"
COMPOSE = """\
services:
  web:
    image: nginx:1
    env_file: ./env/app.env
"""
READ_TOKEN = re.compile(r'name="read_token" value="([^"]+)"')


@pytest.fixture
async def container_host(world: World) -> AsyncIterator[ReaderEngine]:
    fake = ReaderEngine()
    engine = world.app.state.engine
    registry = EngineClientRegistry(lambda settings: fake.bundle())
    engine.registry = registry
    async with world.app.state.database.session() as db:
        await EnvironmentAccessRepository(db).add_user("environment", world.remote_id, STANDARD_ID)
    yield fake
    fake.release_gates()
    await registry.aclose()


@pytest.fixture
async def files_compose(world: World, stack_host: ReaderEngine) -> None:
    compose = world.app.state.engine.compose
    compose.use_binary(
        ComposeBinary((sys.executable, "-I", str(FILES_COMPOSE), str(SCRIPTED_COMPOSE)))
    )
    await compose.detect()


def base(world: World) -> str:
    return f"/environments/{world.remote_id}/stacks/-/adopt/{PROJECT}"


def swarm_base(world: World) -> str:
    return f"/environments/{world.remote_id}/swarm/stacks/-/adopt/{NAMESPACE}"


def text_of(response: Response) -> str:
    return " ".join(visible_text(response.text).split())


def token_of(response: Response) -> str:
    found = READ_TOKEN.search(response.text)
    assert found is not None, "the page carries no read token"
    return found.group(1)


def serve_project(host: ReaderEngine) -> None:
    host.serve("compose.yaml", COMPOSE)
    host.serve("env/app.env", f"API_TOKEN={SECRET_SENTINEL}\n")
    host.serve(".env", f"REGION=eu\nDB_PASSWORD={SECRET_SENTINEL}\n")


async def test_a_project_read_from_the_host_is_shown_then_adopted_as_is(
    admin_client: AsyncClient, world: World, stack_host: ReaderEngine, files_compose: None
) -> None:
    serve_project(stack_host)
    add_project_container(
        stack_host, 1, compose_labels(environment_file=f"{WORKING_DIR}/.env"), state="running"
    )
    boxes = set(stack_host.boxes)

    paste = await admin_client.get(base(world))
    result = await admin_client.post(f"{base(world)}/read", data=csrf_form(admin_client))
    token = token_of(result)
    prefilled = await admin_client.get(f"{base(world)}?read={token}")
    expired = await admin_client.post(
        f"{base(world)}/as-is", data=csrf_form(admin_client, read_token="B" * 22)
    )
    adopted = await admin_client.post(
        f"{base(world)}/as-is", data=csrf_form(admin_client, read_token=token)
    )
    detail = await land(admin_client, adopted)

    assert f'action="{base(world)}/read"' in paste.text and "Read from host" in paste.text
    text = text_of(result)
    assert result.status_code == OK
    assert "Read from the project's files on the host" in text
    assert "env_app.env" in text and "REGION" in text and "DB_PASSWORD" in text
    assert "Adopt shop as is" in text
    assert "Nothing will be redeployed" in text and "web" in text
    assert SECRET_SENTINEL not in result.text
    assert "Not shown" in text
    assert prefilled.status_code == OK and "image: nginx:1" in prefilled.text
    assert "REGION" in prefilled.text and SECRET_SENTINEL not in prefilled.text
    assert adopted.status_code == SEE_OTHER
    assert location(adopted) == f"/environments/{world.remote_id}/stacks/{PROJECT}"
    assert any("adopted as is" in flash for flash in flash_texts(detail.text))
    assert expired.status_code == CONFLICT and "The read has expired" in text_of(expired)
    assert set(stack_host.boxes) == boxes and stack_host.helper_boxes() == []


async def test_a_project_whose_files_are_gone_is_shown_as_a_generated_file(
    admin_client: AsyncClient, world: World, stack_host: ReaderEngine
) -> None:
    add_project_container(
        stack_host, 1, compose_labels(), env=["PATH=/usr/bin", f"API_TOKEN={SECRET_SENTINEL}"]
    )

    result = await admin_client.post(f"{base(world)}/read", data=csrf_form(admin_client))

    text = text_of(result)
    assert result.status_code == OK
    assert "Generated from the project's running containers" in text
    assert "Not used. The file is not on the host. (compose.yaml)" in text
    assert "Refused" in text and "What is not carried over" in text
    assert "API_TOKEN" in text and SECRET_SENTINEL not in result.text
    assert "Adopt as is" in text and "Edit in the paste form" in text


async def test_a_project_with_no_usable_file_shows_why_and_offers_only_the_paste_form(
    admin_client: AsyncClient, world: World, stack_host: ReaderEngine
) -> None:
    stack_host.serve("compose.yaml", f"services:\n  web:\n    command: {SECRET_SENTINEL}\n")
    labels = {k: v for k, v in compose_labels(working_dir="/").items() if "service" not in k}
    add_project_container(stack_host, 1, labels)

    result = await admin_client.post(f"{base(world)}/read", data=csrf_form(admin_client))

    text = text_of(result)
    assert result.status_code == OK
    assert "A file is needed" in text and "root of the host" in text
    assert "Adopt as is" not in text and 'name="read_token"' not in result.text
    assert SECRET_SENTINEL not in result.text
    assert f'href="{base(world)}"' in result.text and stack_host.verbs() == []


async def test_standard_users_get_not_found_on_every_new_route(
    standard_client: AsyncClient, world: World, stack_host: ReaderEngine
) -> None:
    serve_project(stack_host)
    add_project_container(stack_host, 1, compose_labels())
    responses = [
        await standard_client.post(f"{base(world)}/read", data=csrf_form(standard_client)),
        await standard_client.post(
            f"{base(world)}/as-is", data=csrf_form(standard_client, read_token="A" * 22)
        ),
        await standard_client.post(f"{swarm_base(world)}/read", data=csrf_form(standard_client)),
        await standard_client.post(
            f"{swarm_base(world)}/as-is", data=csrf_form(standard_client, read_token="A" * 22)
        ),
    ]

    assert [response.status_code for response in responses] == [NOT_FOUND] * len(responses)
    assert stack_host.verbs() == []
