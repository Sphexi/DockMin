"""The create review and the recreate confirmation asking which credential set pulls the image."""

import asyncio

from httpx import AsyncClient, Response

from tests.auth_support import csrf_form
from tests.container_support import plant_control
from tests.docker_engine.container_engine import ContainerEngine, created_id
from tests.image_support import binding_rows, decode_auth, mark_missing
from tests.provisioning_support import IMAGE, OLD, create_form, seed_recreatable
from tests.web.environment_support import World
from tests.web.helpers import parse_tags
from tests.web.image_support import bind, make_set, text_of

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN, CONFLICT = 200, 303, 400, 403, 409
REGISTRY_IMAGE = "registry.example.com/app:1"
ASK = "Choose which credential set to pull with."
NAME = "recreate-me"


def new_path(world: World) -> str:
    return f"/environments/{world.remote_id}/containers/new"


def create_path(world: World) -> str:
    return f"/environments/{world.remote_id}/containers"


def recreate_path(world: World) -> str:
    return f"/environments/{world.remote_id}/containers/{OLD}/recreate"


def choices(response: Response) -> list[str]:
    return [
        tag.attrs["value"]
        for tag in parse_tags(response.text)
        if tag.name == "input" and tag.attrs.get("name") == "credential_choice"
    ]


def checked(response: Response) -> list[str]:
    return [
        tag.attrs["value"]
        for tag in parse_tags(response.text)
        if tag.name == "input"
        and tag.attrs.get("name") == "credential_choice"
        and "checked" in tag.attrs
    ]


async def review(client: AsyncClient, world: World, **fields: str) -> Response:
    form = csrf_form(client, intent="review", **create_form(**fields))
    return await client.post(new_path(world), data=form)


async def settled(world: World, response: Response) -> None:
    """Wait for the job a pulling create or recreate started."""
    job_id = response.headers["location"].rpartition("/")[2]
    await asyncio.wait({world.app.state.engine.image_jobs._jobs[job_id].task})


def username_sent(host: ContainerEngine) -> str:
    [pull] = host.calls("POST", "/images/create")
    return decode_auth(pull.headers["x-registry-auth"])["username"]


async def test_a_create_that_will_pull_asks_which_set_and_binds_the_one_chosen(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    default = await make_set(admin_client, world, "alpha", default=True)
    other = await make_set(admin_client, world, "beta")
    bound_image = "registry.example.com/bound:1"
    await bind(world, other, bound_image)
    container_host.add_image(IMAGE)

    asked = await review(admin_client, world, image=REGISTRY_IMAGE)
    present = await review(admin_client, world, image=IMAGE)
    preselected = await review(admin_client, world, image=bound_image)
    refused = await admin_client.post(
        create_path(world), data=csrf_form(admin_client, **create_form(image=REGISTRY_IMAGE))
    )
    pulls_after_refusal = len(container_host.calls("POST", "/images/create"))
    created = await admin_client.post(
        create_path(world),
        data=csrf_form(
            admin_client, **create_form(image=REGISTRY_IMAGE), credential_choice=str(other)
        ),
    )

    await settled(world, created)
    async with world.app.state.database.session() as db:
        bindings = {row["image_reference"]: row for row in await binding_rows(db)}
    assert asked.status_code == OK
    assert "Credential set for the pull" in text_of(asked)
    assert choices(asked) == ["anonymous", str(default), str(other)]
    assert checked(asked) == [str(default)]
    assert "Default for this registry" in text_of(asked)
    assert "pull the image on its own page" in text_of(asked)
    assert "Credential set for the pull" not in text_of(present) and choices(present) == []
    assert checked(preselected) == [str(other)]
    assert "changes the binding for every user" in text_of(preselected)
    assert refused.status_code == BAD_REQUEST
    assert ASK in text_of(refused) and "Credential set for the pull" in text_of(refused)
    assert pulls_after_refusal == 0
    assert container_host.calls("POST", "/containers/create") != []
    assert created.status_code == SEE_OTHER
    assert "/containers/jobs/" in created.headers["location"]
    assert created_id(1) in container_host.boxes
    assert username_sent(container_host) == "service-account"
    assert bindings[REGISTRY_IMAGE]["registry_credential_id"] == other


async def test_standard_users_cannot_change_a_bound_set_and_only_an_administrator_repairs_one(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
) -> None:
    seed_recreatable(container_host, name=NAME)
    bound = await make_set(admin_client, world, "alpha", users=True)
    other = await make_set(admin_client, world, "beta", users=True)
    gone = await make_set(admin_client, world, "gone")
    replacement = await make_set(admin_client, world, "replacement")
    hub_bound = await make_set(admin_client, world, "hub-alpha", address="docker.io", users=True)
    hub_other = await make_set(admin_client, world, "hub-beta", address="docker.io", users=True)
    missing_image = "registry.example.com/missing:1"
    await bind(world, bound, REGISTRY_IMAGE)
    await bind(world, gone, missing_image)
    await bind(world, hub_bound, "docker.io/library/nginx:1")
    async with world.app.state.database.session() as db:
        await mark_missing(db, gone)
    await plant_control(world.app, world.remote_id, "container", OLD, public=True, level="write")

    create = await standard_client.post(
        create_path(world),
        data=csrf_form(
            standard_client, **create_form(image=REGISTRY_IMAGE), credential_choice=str(other)
        ),
    )
    recreate = await standard_client.post(
        recreate_path(world),
        data=csrf_form(
            standard_client, expected_name=NAME, pull="on", credential_choice=str(hub_other)
        ),
    )
    missing_form = create_form(image=missing_image)
    no_choice = await admin_client.post(
        create_path(world), data=csrf_form(admin_client, **missing_form)
    )
    repaired = await admin_client.post(
        create_path(world),
        data=csrf_form(admin_client, **missing_form, credential_choice=str(replacement)),
    )

    await settled(world, repaired)
    async with world.app.state.database.session() as db:
        bindings = {row["image_reference"]: row for row in await binding_rows(db)}
    assert (create.status_code, recreate.status_code) == (FORBIDDEN, FORBIDDEN)
    assert "Only an administrator can change which credential set" in text_of(create)
    assert "Only an administrator can change which credential set" in text_of(recreate)
    assert no_choice.status_code == BAD_REQUEST
    assert "Choose which credential set to pull with." in text_of(no_choice)
    assert repaired.status_code == SEE_OTHER
    assert bindings[REGISTRY_IMAGE]["registry_credential_id"] == bound
    assert bindings["docker.io/library/nginx:1"]["registry_credential_id"] == hub_bound
    assert (
        bindings[missing_image]["state"],
        bindings[missing_image]["registry_credential_id"],
    ) == (
        "bound",
        replacement,
    )
    assert len(container_host.calls("POST", "/containers/create")) == 1


async def test_the_recreate_confirmation_asks_which_set_and_a_pull_uses_and_binds_the_choice(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seed_recreatable(container_host, name=NAME)
    first = await make_set(admin_client, world, "alpha", address="docker.io", default=True)
    second = await make_set(admin_client, world, "beta", address="docker.io")

    page = await admin_client.get(recreate_path(world))
    refused = await admin_client.post(
        recreate_path(world), data=csrf_form(admin_client, expected_name=NAME, pull="on")
    )
    mutations_after_refusal = container_host.mutations()
    done = await admin_client.post(
        recreate_path(world),
        data=csrf_form(admin_client, expected_name=NAME, pull="on", credential_choice=str(second)),
    )

    await settled(world, done)
    async with world.app.state.database.session() as db:
        [binding] = await binding_rows(db)
    assert page.status_code == OK
    assert "Pull the image again first" in text_of(page)
    assert "used only when the image is pulled again" in text_of(page)
    assert choices(page) == ["anonymous", str(first), str(second)]
    assert checked(page) == [str(first)]
    assert refused.status_code == BAD_REQUEST and ASK in text_of(refused)
    assert mutations_after_refusal == []
    assert done.status_code == SEE_OTHER
    assert binding["registry_credential_id"] == second
    assert binding["image_reference"] == "docker.io/library/nginx:1"
