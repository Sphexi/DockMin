"""The redeploy confirmation and the job page of a stack that builds its images."""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.db.repositories.stack import StackRepository
from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.image_support import REFERENCE
from tests.stack_build_support import CONTEXT, DOCKERFILE, store_context
from tests.stack_support import (  # noqa: F401
    install_buildx,
    job_id_of,
    settle_stack_job,
    stopped_stack_jobs,
)
from tests.web.environment_support import World
from tests.web.helpers import form_values
from tests.web.registry_support import visible_text
from tests.web.stack_file_support import seed_stack

pytestmark = pytest.mark.usefixtures("stopped_stack_jobs")

OK, BAD_REQUEST, CONFLICT = 200, 400, 409
NAME = "blog"
TAG = f"{NAME}-web"
BUILD_FILE = f"services:\n  web:\n    build: ./{CONTEXT}\n"
STEP = "sha256:" + "a" * 64
STARTED, COMPLETED = "2026-11-06T10:00:00Z", "2026-11-06T10:00:05Z"
RUN_STATEMENT = "RUN steps of each Dockerfile below execute on"


def flat(text: str) -> str:
    return " ".join(visible_text(text).split())


async def seed(app: FastAPI, environment_id: int, files: dict[str, str]) -> int:
    stack_id = await seed_stack(app, environment_id, NAME, BUILD_FILE)
    async with app.state.database.session() as db:
        repository = StackRepository(db, app.state.vault, app.state.auth.clock)
        await store_context(repository, stack_id, CONTEXT, files)
    return stack_id


async def test_the_confirmation_names_the_build_and_the_job_page_shows_it_building(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    sim = install_buildx(world.app.state.engine.compose, stack_host, tmp_path / "buildx")
    stack_host.add_image(REFERENCE)
    steps = [
        {"vertexes": [{"digest": STEP, "name": "[1/2] RUN echo built", "started": STARTED}]},
        {"vertexes": [{"digest": STEP, "name": "[1/2] RUN echo built", "completed": COMPLETED}]},
    ]
    (sim.directory / "stderr.txt").write_text(
        "\n".join(json.dumps(step) for step in steps) + "\n", encoding="utf-8"
    )
    await seed(world.app, world.remote_id, {"Dockerfile": DOCKERFILE})
    base = f"/environments/{world.remote_id}/stacks"

    review = await admin_client.get(f"{base}/{NAME}/redeploy")
    posted = await admin_client.post(
        f"{base}/{NAME}/redeploy", data=form_values(review.text) | csrf_form(admin_client)
    )
    job_id = job_id_of(posted)
    await settle_stack_job(world.app, job_id)
    job = await admin_client.get(f"{base}/-/jobs/{job_id}")

    text = flat(review.text)
    assert review.status_code == OK
    assert "Images built on" in text and TAG in text and REFERENCE in text
    assert RUN_STATEMENT in text
    assert 'name="rebuild"' in review.text and 'name="expected_builds"' in review.text
    job_text = flat(job.text)
    assert "Image builds" in job_text and "RUN echo built" in job_text
    assert f"{TAG} (built)" in job_text


async def test_a_context_changed_after_the_review_is_refused_as_changed_since_review(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    sim = install_buildx(world.app.state.engine.compose, stack_host, tmp_path / "buildx")
    stack_host.add_image(REFERENCE)
    stack_id = await seed(world.app, world.remote_id, {"Dockerfile": DOCKERFILE})
    base = f"/environments/{world.remote_id}/stacks"
    review = await admin_client.get(f"{base}/{NAME}/redeploy")
    async with world.app.state.database.session() as db:
        repository = StackRepository(db, world.app.state.vault, world.app.state.auth.clock)
        await store_context(
            repository, stack_id, CONTEXT, {"Dockerfile": DOCKERFILE, "app.py": "changed"}
        )

    stale = await admin_client.post(
        f"{base}/{NAME}/redeploy", data=form_values(review.text) | csrf_form(admin_client)
    )

    assert stale.status_code in (BAD_REQUEST, CONFLICT)
    assert "changed since you reviewed" in flat(stale.text)
    assert sim.counter == 0


async def test_without_buildx_a_stack_that_builds_is_refused_on_the_confirmation_post(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    stack_host.add_image(REFERENCE)
    world.app.state.engine.compose.buildx.binary = None
    await seed(world.app, world.remote_id, {"Dockerfile": DOCKERFILE})
    base = f"/environments/{world.remote_id}/stacks"
    review = await admin_client.get(f"{base}/{NAME}/redeploy")

    refused = await admin_client.post(
        f"{base}/{NAME}/redeploy", data=form_values(review.text) | csrf_form(admin_client)
    )

    assert review.status_code == OK
    assert refused.status_code == BAD_REQUEST
    assert "Docker buildx is not available" in flat(refused.text)
