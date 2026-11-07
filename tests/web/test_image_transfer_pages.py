"""Push from the image page and the update check: who may, what they see, what is asked."""

import asyncio
import re

import pytest
from httpx import AsyncClient, Response

from app.registry_client.manifests import ManifestCheckKind
from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.image_support import binding_rows, digest, full_id, image_id
from tests.web.environment_support import World
from tests.web.image_support import (
    NGINX,
    SPARE,
    ScriptedManifests,
    bind,
    hidden_values,
    image_path,
    images_path,
    make_set,
    text_of,
)
from tests.web.registry_support import HTMX

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN, NOT_FOUND, METHOD_NOT_ALLOWED = 200, 303, 400, 403, 404, 405
PUSHED = 7
TARGET = "registry.example.com/team/app:1"
TARGET_REPOSITORY = "registry.example.com/team/app"
LOCAL_DIGEST = digest(1)
JOB = re.compile(r"/jobs/([A-Za-z0-9_-]{22})$")
MAX_REFERENCES = 25


@pytest.fixture
def manifests(world: World) -> ScriptedManifests:
    scripted = ScriptedManifests()
    world.app.state.registries.manifests = scripted
    return scripted


@pytest.fixture
def pushable(daemon: ContainerEngine) -> ContainerEngine:
    daemon.add_image(TARGET, full_id(PUSHED), digests=(f"{TARGET_REPOSITORY}@{LOCAL_DIGEST}",))
    return daemon


def job_of(response: Response) -> str:
    assert response.status_code == SEE_OTHER, response.text
    found = JOB.search(response.headers["location"])
    assert found is not None
    return found.group(1)


async def settled(world: World, job_id: str) -> None:
    await asyncio.wait({world.app.state.engine.image_jobs._jobs[job_id].task})


async def test_push_offers_each_tag_asks_for_a_set_and_starts_a_job_that_binds_the_reference(
    admin_client: AsyncClient, world: World, pushable: ContainerEngine
) -> None:
    first = await make_set(admin_client, world, "alpha", default=True)
    second = await make_set(admin_client, world, "beta")
    path = image_path(world, image_id(PUSHED), "/push")

    page = await admin_client.get(path)
    open_choice = await admin_client.post(path, data=csrf_form(admin_client, reference=TARGET))
    foreign = await admin_client.post(
        path, data=csrf_form(admin_client, reference="registry.example.com/other:1")
    )
    no_jobs_yet = len(world.app.state.engine.image_jobs)
    started = await admin_client.post(
        path, data=csrf_form(admin_client, reference=TARGET, credential_choice=str(second))
    )
    job_id = job_of(started)
    await settled(world, job_id)
    job = await admin_client.get(started.headers["location"])

    async with world.app.state.database.session() as db:
        [binding] = await binding_rows(db)
    assert page.status_code == OK
    assert TARGET in page.text and "Push image" in text_of(page)
    assert f'value="{first}"' in page.text and f'value="{second}"' in page.text
    assert "Default for this registry" in text_of(page)
    assert open_choice.status_code == BAD_REQUEST
    assert "Choose which credential set to pull with." in text_of(open_choice)
    assert foreign.status_code == BAD_REQUEST
    assert "That reference is not on this image." in text_of(foreign)
    assert no_jobs_yet == 0
    assert "Pushed" in text_of(job) and TARGET in text_of(job)
    assert (binding["image_reference"], binding["registry_credential_id"]) == (TARGET, second)
    calls = pushable.calls("POST", f"/images/{TARGET_REPOSITORY}/push")
    assert [call.query for call in calls] == [{"tag": "1"}]


async def test_an_untagged_image_cannot_be_pushed_and_only_a_creator_on_a_remote_host_may_push(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    pushable: ContainerEngine,
) -> None:
    pushable.add_image(None, full_id(PUSHED + 1))
    untagged = image_path(world, image_id(PUSHED + 1), "/push")
    tagged = image_path(world, image_id(PUSHED), "/push")
    local = image_path(world, image_id(PUSHED), "/push", environment_id=world.local_id)

    by_admin = await admin_client.get(untagged)
    by_standard = await standard_client.get(untagged)
    remote = await standard_client.get(tagged)
    local_page = await standard_client.get(local)
    local_post = await standard_client.post(
        local, data=csrf_form(standard_client, reference=TARGET)
    )

    assert "This image has no tag to push. Tag it with the repository name first." in text_of(
        by_admin
    )
    assert "An administrator has to tag it" in text_of(by_standard)
    assert remote.status_code == OK
    assert (local_page.status_code, local_post.status_code) == (FORBIDDEN, FORBIDDEN)
    assert pushable.calls("POST", f"/images/{TARGET_REPOSITORY}/push") == []


async def test_the_update_check_compares_bound_references_and_never_says_up_to_date_when_unsure(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    pushable: ContainerEngine,
    manifests: ScriptedManifests,
) -> None:
    bound = await make_set(admin_client, world, "alpha")
    other_reference = "registry.example.com/team/other:1"
    unreachable = "registry.example.com/team/down:1"
    open_reference = "unconfigured.example.com/team/app:1"
    for reference, number in ((other_reference, 1), (unreachable, 2), (open_reference, 3)):
        repository = reference.rpartition(":")[0]
        pushable.add_image(
            reference, full_id(PUSHED + number), digests=(f"{repository}@{LOCAL_DIGEST}",)
        )
    for reference in (TARGET, other_reference, unreachable):
        await bind(world, bound, reference)
    await bind(world, None, open_reference)
    manifests.answers[TARGET] = LOCAL_DIGEST
    manifests.answers[unreachable] = ManifestCheckKind.TCP
    references = (TARGET, other_reference, unreachable, "never-pulled:1")
    updates = f"{images_path(world)}/updates"
    token = csrf_form(admin_client)["csrf_token"]

    listing = await admin_client.get(images_path(world))
    detail = await admin_client.get(image_path(world, image_id(PUSHED)))
    plain = await admin_client.get(image_path(world, image_id(SPARE)))
    other = await admin_client.get(image_path(world, image_id(NGINX)))
    before = len(manifests.calls)
    fragment = await admin_client.post(
        updates, data={"csrf_token": token, "reference": references}, headers=HTMX
    )
    page = await admin_client.post(updates, data={"csrf_token": token, "reference": references})
    unconfigured = await admin_client.post(
        updates, data=csrf_form(admin_client, reference=open_reference), headers=HTMX
    )
    no_grant = await standard_client.post(
        updates, data=csrf_form(standard_client, reference=TARGET), headers=HTMX
    )
    invalid = {
        count: await admin_client.post(
            updates,
            data={
                "csrf_token": token,
                "reference": [f"registry.example.com/team/app:{n}" for n in range(count)],
            },
            headers=HTMX,
        )
        for count in (0, MAX_REFERENCES + 1)
    }
    by_get = await admin_client.get(updates)
    hidden = await standard_client.post(
        f"{images_path(world, world.down_id)}/updates",
        data=csrf_form(standard_client, reference="nginx:1"),
    )

    text = text_of(fragment)
    assert set(hidden_values(listing, "reference")) == {
        TARGET,
        other_reference,
        unreachable,
        open_reference,
    }
    assert hidden_values(detail, "reference") == [TARGET]
    assert hidden_values(detail, "back") == [image_id(PUSHED)]
    assert hidden_values(plain, "reference") == [] and hidden_values(other, "reference") == []
    assert "Only bound tags can be checked." in text_of(other)
    assert before == 0
    assert fragment.status_code == OK
    assert "<html" not in fragment.text and 'id="update-results"' in fragment.text
    assert re.search(rf"{re.escape(TARGET)} Up to date", text)
    assert re.search(r"team/other:1 Update available", text)
    assert re.search(r"team/down:1 Unknown This server could not connect to the registry\.", text)
    assert re.search(r"never-pulled:1 Not bound", text)
    assert "Checked from this server, not from the Docker host" in text
    assert "<html" in page.text and "Update check" in text_of(page)
    assert "No credential set for this registry is granted to this environment" in text_of(
        unconfigured
    )
    assert "You hold no grant on the credential set this image is bound to." in text_of(no_grant)
    assert {call[1].key for call in manifests.calls} == {TARGET, other_reference, unreachable}
    assert {count: r.status_code for count, r in invalid.items()} == {0: 400, 26: 400}
    assert all("Check between 1 and 25 references at once." in text_of(r) for r in invalid.values())
    assert by_get.status_code in (METHOD_NOT_ALLOWED, NOT_FOUND)
    assert hidden.status_code == NOT_FOUND
