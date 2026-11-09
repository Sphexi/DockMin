"""The Swarm stack import pages: the list action, the upload gates and an import end to end."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.docker_engine.registry import EngineClientRegistry
from app.services.stack_import_store import SPOOL_DIRECTORY
from tests.auth_support import STANDARD_ID
from tests.object_support import hidden_values, visible_text
from tests.services.test_stack_import import (
    SWARM_FILE,
    SWARM_NAME,
    SWARM_VOLUME,
    SwarmImportDaemon,
)
from tests.stack_import_support import NOTE, NOTE_FILE, build_archive
from tests.stack_support import job_id_of, settle_stack_job
from tests.swarm_stack_support import install_swarm_compose
from tests.web.environment_support import World
from tests.web.test_stack_import_pages import import_fields, rows, upload

OK, SEE_OTHER, NOT_FOUND, UNPROCESSABLE = 200, 303, 404, 422


@pytest.fixture
async def swarm_import_host(world: World, tmp_path: Path) -> AsyncIterator[SwarmImportDaemon]:
    fake = SwarmImportDaemon()
    fake.add_image("busybox:latest")
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
    directory = tmp_path / "swarm-compose"
    directory.mkdir()
    engine.compose.use_binary(install_swarm_compose(directory))
    await engine.compose.detect()
    yield fake
    for job in list(engine.stack_jobs._jobs.values()):
        if job.task is not None and not job.task.done():
            job.task.cancel()
    fake.release_gates()
    await registry.aclose()


def swarm_base(world: World) -> str:
    return f"/environments/{world.remote_id}/swarm/stacks"


async def swarm_archive() -> bytes:
    return await build_archive(
        kind="swarm_stack",
        text=SWARM_FILE,
        name=SWARM_NAME,
        secrets=False,
        passphrase=None,
        volumes={SWARM_VOLUME: {NOTE_FILE: NOTE}},
        variables=(),
        files=(),
    )


async def test_the_swarm_list_offers_import_to_an_administrator_only(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    swarm_import_host: SwarmImportDaemon,
) -> None:
    admin = await admin_client.get(swarm_base(world))
    standard = await standard_client.get(swarm_base(world))

    assert f'href="{swarm_base(world)}/-/import"' in admin.text and ">Import stack<" in admin.text
    assert "/-/import" not in standard.text


async def test_the_swarm_routes_answer_a_standard_user_and_a_compose_environment_not_found(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    swarm_import_host: SwarmImportDaemon,
) -> None:
    on_local = f"/environments/{world.local_id}/swarm/stacks/-/import"

    responses = [
        await standard_client.get(f"{swarm_base(world)}/-/import"),
        await upload(
            standard_client, world, await swarm_archive(), path=f"{swarm_base(world)}/-/import"
        ),
        await admin_client.get(on_local),
        await upload(admin_client, world, await swarm_archive(), path=on_local),
    ]

    assert [r.status_code for r in responses] == [NOT_FOUND] * 4
    assert await rows(world, "stack_imported") == []


async def test_each_route_refuses_an_archive_of_the_other_kind_naming_where_it_belongs(
    admin_client: AsyncClient, world: World, swarm_import_host: SwarmImportDaemon
) -> None:
    swarm_on_compose = await upload(admin_client, world, await swarm_archive())
    compose_on_swarm = await upload(
        admin_client, world, await build_archive(), path=f"{swarm_base(world)}/-/import"
    )

    assert swarm_on_compose.status_code == compose_on_swarm.status_code == UNPROCESSABLE
    assert "Swarm stacks list" in visible_text(swarm_on_compose)
    assert "Compose stacks list" in visible_text(compose_on_swarm)
    directory = world.app.state.settings.data_dir / SPOOL_DIRECTORY
    assert not directory.exists() or not any(directory.iterdir())


async def test_a_swarm_archive_is_reviewed_and_imported_and_its_deployment_starts(
    admin_client: AsyncClient, world: World, swarm_import_host: SwarmImportDaemon
) -> None:
    path = f"{swarm_base(world)}/-/import"
    uploaded = await upload(admin_client, world, await swarm_archive(), path=path)
    assert uploaded.status_code == SEE_OTHER
    location = str(uploaded.headers["location"])
    review = await admin_client.get(location)
    text = visible_text(review)

    assert f"{SWARM_NAME}_web" in text and SWARM_VOLUME in text and "Swarm stack" in text
    assert "busybox:latest" in text and hidden_values(review, "expected_plan")

    fields = import_fields(
        admin_client,
        review,
        **{"credential-0-reference": "busybox:latest", "credential-0-choice": "anonymous"},
    ) | {"name": SWARM_NAME}
    response = await admin_client.post(location, data=fields)
    await settle_stack_job(world.app, job_id_of(response))

    assert swarm_import_host.files_of(SWARM_VOLUME)[NOTE_FILE].data == NOTE
    [imported] = await rows(world, "stack_imported")
    assert imported["result"] == "success"
