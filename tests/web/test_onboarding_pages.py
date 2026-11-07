"""The onboarding pages: the review job, the review, its confirmation, the apply, and access."""

import asyncio
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.auth.principal import Principal
from app.compose_cli.binary import ComposeBinary
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.resource_control import (
    ResourceControl,
    ResourceControlRepository,
    ResourceType,
)
from app.docker_engine.registry import EngineClientRegistry
from app.services.image_jobs import JobProgress, JobResult
from app.services.onboarding_plan import OnboardingReview
from tests.auth_support import ADMIN_ID, STANDARD_ID, STANDARD_USERNAME, csrf_form
from tests.host_read_support import ReaderEngine, add_project_container, compose_labels
from tests.stack_files_support import FILES_COMPOSE
from tests.stack_support import PROJECT_LABEL, SCRIPTED_COMPOSE, stopped_stack_jobs  # noqa: F401
from tests.web.environment_support import World
from tests.web.flash_support import land, location
from tests.web.registry_support import visible_text

pytestmark = pytest.mark.usefixtures("stopped_stack_jobs")

OK, SEE_OTHER, NOT_FOUND, CONFLICT = 200, 303, 404, 409
PROJECT = "shop"
GHOST = "ghost"
VOLUME = "data"
COMPOSE = "services:\n  web:\n    image: nginx:1\n"
TOKEN_IN_LINK = re.compile(r"/onboarding/reviews/([A-Za-z0-9_-]{22})")
JOB_IN_LINK = re.compile(r"/onboarding/jobs/([A-Za-z0-9_-]{22})")
EXPECTED_PLAN = re.compile(r'name="expected_plan" value="([0-9a-f]{64})"')
STRANGER = Principal(999, "stranger", "administrator")
LEVEL = "read"
SECRET_VALUE = "hunter2-sentinel"


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
    return f"/environments/{world.remote_id}/onboarding"


def text_of(response: Response) -> str:
    return " ".join(visible_text(response.text).split())


def stock(host: ReaderEngine, tmp_path: Path) -> None:
    host.serve("compose.yaml", f"x-fake:\n  record: {tmp_path / 'record'}\n{COMPOSE}")
    add_project_container(host, 1, compose_labels(), state="running")
    host.add_network("ghost_default", 0x99, Labels={PROJECT_LABEL: GHOST})
    host.add_volume(VOLUME)
    host.add(f"{0xC1:064x}", "loose", image="nginx:1")


async def settle(app: FastAPI, job_id: str) -> None:
    task = app.state.engine.image_jobs._jobs[job_id].task
    done, _ = await asyncio.wait({task}, timeout=30)
    assert done, "the review job did not finish"


async def start(client: AsyncClient, world: World) -> tuple[Response, str]:
    started = await client.post(base(world), data=csrf_form(client))
    assert started.status_code == SEE_OTHER, started.text
    found = JOB_IN_LINK.search(location(started))
    assert found is not None
    await settle(world.app, found.group(1))
    return started, found.group(1)


async def reviewed(client: AsyncClient, world: World) -> tuple[str, str]:
    """Run the review and return the job id and the review token."""
    _, job_id = await start(client, world)
    page = await client.get(f"{base(world)}/jobs/{job_id}")
    found = TOKEN_IN_LINK.search(page.text)
    assert found is not None, text_of(page)
    return job_id, found.group(1)


async def control_over(
    app: FastAPI, environment_id: int, kind: ResourceType, name: str
) -> ResourceControl | None:
    async with app.state.database.session() as db:
        return await ResourceControlRepository(db, app.state.auth.clock).get_for(
            environment_id, kind, name
        )


async def test_every_onboarding_route_answers_a_standard_user_as_missing(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: ReaderEngine,
    stack_host: ReaderEngine,
    files_compose: None,
    tmp_path: Path,
) -> None:
    stock(container_host, tmp_path)
    job_id, token = await reviewed(admin_client, world)
    form = csrf_form(standard_client, expected_plan="x")

    answers = [
        (await standard_client.post(base(world), data=form)).status_code,
        (await standard_client.get(f"{base(world)}/jobs/{job_id}")).status_code,
        (await standard_client.post(f"{base(world)}/jobs/{job_id}/cancel", data=form)).status_code,
        (await standard_client.get(f"{base(world)}/reviews/{token}")).status_code,
        (await standard_client.get(f"{base(world)}/reviews/{token}/stacks/1")).status_code,
        (
            await standard_client.post(f"{base(world)}/reviews/{token}/confirm", data=form)
        ).status_code,
        (await standard_client.post(f"{base(world)}/reviews/{token}/apply", data=form)).status_code,
    ]

    assert answers == [NOT_FOUND] * len(answers)


async def test_a_review_job_and_its_review_are_visible_only_to_the_administrator_who_started_them(
    admin_client: AsyncClient,
    world: World,
    container_host: ReaderEngine,
    stack_host: ReaderEngine,
    files_compose: None,
    tmp_path: Path,
) -> None:
    stock(container_host, tmp_path)
    engine = world.app.state.engine

    async def run(progress: JobProgress) -> JobResult:
        return JobResult("done")

    foreign_job = engine.image_jobs.start(
        "onboarding_review", STRANGER, world.remote_id, "someone else's", run
    )
    foreign_token = engine.onboarding_reviews.put(
        STRANGER.user_id, world.remote_id, OnboardingReview(world.remote_id, "remote", (), (), ())
    )
    await settle(world.app, foreign_job)

    job = await admin_client.get(f"{base(world)}/jobs/{foreign_job}")
    review = await admin_client.get(f"{base(world)}/reviews/{foreign_token}")
    confirm = await admin_client.post(
        f"{base(world)}/reviews/{foreign_token}/confirm", data=csrf_form(admin_client)
    )

    assert job.status_code == NOT_FOUND
    assert (review.status_code, confirm.status_code) == (CONFLICT, CONFLICT)
    assert "has expired" in text_of(review) and "has expired" in text_of(confirm)


async def test_the_review_confirmation_and_apply_pages_adopt_what_was_ticked_and_nothing_else(
    admin_client: AsyncClient,
    world: World,
    container_host: ReaderEngine,
    stack_host: ReaderEngine,
    files_compose: None,
    tmp_path: Path,
) -> None:
    stock(container_host, tmp_path)
    job_id, token = await reviewed(admin_client, world)
    review_path = f"{base(world)}/reviews/{token}"

    job = await admin_client.get(f"{base(world)}/jobs/{job_id}")
    review = await admin_client.get(review_path)
    file_link = re.search(r'href="([^"]*/stacks/\d+)"', review.text)
    assert file_link is not None
    file_page = await admin_client.get(file_link.group(1))
    refused_page = await admin_client.get(f"{review_path}/stacks/1")

    review_text = text_of(review)
    for heading in ("Stacks", "Images", "Objects without access settings", "Ownership"):
        assert heading in review_text
    assert PROJECT in review_text and GHOST in review_text and "Needs a file" in review_text
    assert "loose" in review_text and VOLUME in review_text
    assert "Succeeded" in text_of(job) and "Open the review" in text_of(job)
    assert file_page.status_code == OK and "image: nginx:1" in file_page.text
    assert refused_page.status_code == NOT_FOUND
    stack_boxes = re.findall(r'name="stack" value="(\d+)" checked', review.text)
    object_boxes = re.findall(r'name="object" value="(\d+)" checked', review.text)
    assert len(stack_boxes) == 1 and object_boxes

    confirmed = await admin_client.post(
        f"{review_path}/confirm",
        data=csrf_form(
            admin_client,
            stack=stack_boxes,
            object=object_boxes,
            owner=STANDARD_USERNAME,
            team="",
            level=LEVEL,
        ),
    )
    confirm_text = text_of(confirmed)
    plan = EXPECTED_PLAN.search(confirmed.text)

    assert confirmed.status_code == OK and plan is not None
    assert "Nothing is pulled, redeployed or restarted" in confirm_text
    assert PROJECT in confirm_text and "loose" in confirm_text and VOLUME in confirm_text
    assert f"owner {STANDARD_USERNAME}" in confirm_text
    assert await control_over(world.app, world.remote_id, "stack", PROJECT) is None

    stale = await admin_client.post(
        f"{review_path}/apply", data=csrf_form(admin_client, expected_plan="0" * 64)
    )
    assert stale.status_code == CONFLICT
    assert await control_over(world.app, world.remote_id, "stack", PROJECT) is None

    applied = await admin_client.post(
        f"{review_path}/apply", data=csrf_form(admin_client, expected_plan=plan.group(1))
    )
    result = await land(admin_client, applied)

    assert applied.status_code == SEE_OTHER
    result_text = text_of(result)
    assert "Stacks: 1 done, 0 skipped, 0 failed" in result_text
    assert PROJECT in result_text and "Adopted as is" in result_text
    stack_control = await control_over(world.app, world.remote_id, "stack", PROJECT)
    volume_control = await control_over(world.app, world.remote_id, "volume", VOLUME)
    assert stack_control is not None and stack_control.owner_user_id == STANDARD_ID
    assert volume_control is not None and volume_control.owner_user_id == STANDARD_ID
    again = await admin_client.get(review_path)
    assert again.status_code == NOT_FOUND or "expired" in text_of(again)


async def test_the_file_page_hides_secret_environment_values_of_the_file_it_shows(
    admin_client: AsyncClient,
    world: World,
    container_host: ReaderEngine,
    stack_host: ReaderEngine,
    files_compose: None,
    tmp_path: Path,
) -> None:
    container_host.serve(
        "compose.yaml",
        f"x-fake:\n  record: {tmp_path / 'record'}\n{COMPOSE}"
        f"    environment:\n      DB_PASS: {SECRET_VALUE}\n      REGION: eu\n",
    )
    add_project_container(container_host, 1, compose_labels(), state="running")
    _, token = await reviewed(admin_client, world)

    page = await admin_client.get(f"{base(world)}/reviews/{token}/stacks/1")

    assert page.status_code == OK
    assert SECRET_VALUE not in page.text
    assert "DB_PASS: &#39;(hidden)&#39;" in page.text or "DB_PASS: '(hidden)'" in page.text
    assert "REGION: eu" in page.text


async def test_a_review_that_is_already_being_applied_refuses_a_second_apply(
    admin_client: AsyncClient,
    world: World,
    container_host: ReaderEngine,
    stack_host: ReaderEngine,
    files_compose: None,
    tmp_path: Path,
) -> None:
    stock(container_host, tmp_path)
    _, token = await reviewed(admin_client, world)
    review_path = f"{base(world)}/reviews/{token}"
    review = await admin_client.get(review_path)
    stack_boxes = re.findall(r'name="stack" value="(\d+)" checked', review.text)
    confirmed = await admin_client.post(
        f"{review_path}/confirm", data=csrf_form(admin_client, stack=stack_boxes, owner="", team="")
    )
    plan = EXPECTED_PLAN.search(confirmed.text)
    assert plan is not None
    reviews = world.app.state.engine.onboarding_reviews
    assert reviews.begin_apply(token, ADMIN_ID, world.remote_id)

    second = await admin_client.post(
        f"{review_path}/apply", data=csrf_form(admin_client, expected_plan=plan.group(1))
    )

    assert second.status_code == CONFLICT and "Already being applied" in text_of(second)
    assert await control_over(world.app, world.remote_id, "stack", PROJECT) is None
    reviews.end_apply(token)
    third = await admin_client.post(
        f"{review_path}/apply", data=csrf_form(admin_client, expected_plan=plan.group(1))
    )
    assert third.status_code == SEE_OTHER


async def test_the_environment_page_offers_the_flow_to_an_administrator_on_a_reachable_host(
    admin_client: AsyncClient, world: World, container_host: ReaderEngine
) -> None:
    reachable = await admin_client.get(f"/environments/{world.remote_id}")
    unreachable = await admin_client.get(f"/environments/{world.down_id}")
    refused = await admin_client.post(
        f"/environments/{world.down_id}/onboarding", data=csrf_form(admin_client)
    )

    assert "Adopt everything on this host" in text_of(reachable)
    assert f'action="{base(world)}"' in reachable.text
    assert "Adopt everything on this host" not in text_of(unreachable)
    assert refused.status_code == CONFLICT
