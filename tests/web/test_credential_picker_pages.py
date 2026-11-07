"""Choosing which credential set pulls an image, on each page that can pull one."""

import re

from httpx import AsyncClient, Response

from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.provisioning_support import create_form
from tests.web.environment_support import World
from tests.web.helpers import parse_tags
from tests.web.image_support import bind, hidden_values, make_set, text_of

HTMX = {"HX-Request": "true"}
OK, BAD_REQUEST, FORBIDDEN = 200, 400, 403
ANONYMOUS = "anonymous"
REGISTRY_IMAGE = "registry.example.com/app:1"
SECOND_IMAGE = "registry.example.com/worker:1"
HUB_IMAGE = "docker.io/library/nginx:1"
UNSERVED_IMAGE = "ghcr.io/o/app:2"
NOT_A_CANDIDATE = "That credential set is for a different registry."
NO_CANDIDATE_TEXT = "No credential set is available for this registry."
ANONYMOUS_LABEL = "Anonymous (no credentials)"
LOCKED = "Only an administrator can change which credential set"
STACK_FILE = (
    f"services:\n  web:\n    image: {REGISTRY_IMAGE}\n  worker:\n    image: {SECOND_IMAGE}\n"
)


def container_choices(world: World) -> str:
    return f"/environments/{world.remote_id}/containers/new/choices"


def service_choices(world: World) -> str:
    return f"/environments/{world.remote_id}/swarm/services/new/choices"


def offered(response: Response, name: str = "credential_choice") -> list[str]:
    return [
        tag.attrs["value"]
        for tag in parse_tags(response.text)
        if tag.name == "input"
        and tag.attrs.get("type") == "radio"
        and tag.attrs.get("name") == name
    ]


def checked(response: Response, name: str = "credential_choice") -> list[str]:
    return [
        tag.attrs["value"]
        for tag in parse_tags(response.text)
        if tag.name == "input"
        and tag.attrs.get("type") == "radio"
        and tag.attrs.get("name") == name
        and "checked" in tag.attrs
    ]


async def pick(client: AsyncClient, world: World, **fields: str) -> Response:
    return await client.post(
        container_choices(world), data=csrf_form(client, **fields), headers=HTMX
    )


async def test_the_create_choices_offer_anonymous_first_and_preselect_the_default_for_the_host(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    default = await make_set(admin_client, world, "alpha", default=True)
    other = await make_set(admin_client, world, "beta")
    first_hub = await make_set(admin_client, world, "hub-alpha", address="docker.io")
    second_hub = await make_set(admin_client, world, "hub-beta", address="docker.io")

    with_default = await pick(admin_client, world, image=REGISTRY_IMAGE, pull="missing")
    without_default = await pick(admin_client, world, image=HUB_IMAGE, pull="missing")

    assert with_default.status_code == OK and "<html" not in with_default.text
    assert offered(with_default) == [ANONYMOUS, str(default), str(other)]
    assert checked(with_default) == [str(default)]
    assert offered(without_default) == [ANONYMOUS, str(first_hub), str(second_hub)]
    assert checked(without_default) == [ANONYMOUS]
    assert 'id="image-choices"' in with_default.text


async def test_the_create_choices_keep_the_posted_choice_when_the_pull_policy_changes(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await make_set(admin_client, world, "alpha", default=True)
    other = await make_set(admin_client, world, "beta")

    always = await pick(
        admin_client, world, image=REGISTRY_IMAGE, pull="always", credential_choice=str(other)
    )
    never = await pick(
        admin_client, world, image=REGISTRY_IMAGE, pull="never", credential_choice=str(other)
    )

    assert checked(always) == [str(other)]
    assert offered(never) == []
    assert "The image is not pulled, so no credential set is used." in text_of(never)


async def test_the_create_choices_show_anonymous_as_plain_text_when_no_set_fits_the_registry(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await make_set(admin_client, world, "elsewhere", address="mirror.example.com")

    response = await pick(admin_client, world, image=UNSERVED_IMAGE, pull="missing")

    assert response.status_code == OK
    assert offered(response) == []
    assert ANONYMOUS_LABEL in text_of(response) and NO_CANDIDATE_TEXT in text_of(response)
    assert hidden_values(response, "credential_choice") == [ANONYMOUS]


async def test_the_create_choices_for_a_blank_or_malformed_image_render_no_picker_or_an_error(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    blank = await pick(admin_client, world, image="", pull="missing")
    malformed = await pick(admin_client, world, image="Not A Reference", pull="missing")

    assert blank.status_code == OK and offered(blank) == []
    assert malformed.status_code == BAD_REQUEST
    assert "Image is not a valid image reference." in text_of(malformed)


async def test_a_standard_user_sees_the_bound_set_locked_in_the_create_choices(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
) -> None:
    await make_set(admin_client, world, "alpha", default=True, users=True)
    bound = await make_set(admin_client, world, "beta", users=True)
    await bind(world, bound, REGISTRY_IMAGE)

    response = await pick(standard_client, world, image=REGISTRY_IMAGE, pull="missing")

    assert response.status_code == OK
    assert offered(response) == []
    assert LOCKED in text_of(response)
    assert hidden_values(response, "credential_choice") == [str(bound)]


async def test_a_create_review_with_no_fitting_set_shows_anonymous_as_plain_text(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    review = await admin_client.post(
        f"/environments/{world.remote_id}/containers/new",
        data=csrf_form(admin_client, intent="review", **create_form(image=UNSERVED_IMAGE)),
    )

    assert review.status_code == OK
    assert "Credential set for the pull" in text_of(review)
    assert offered(review) == []
    assert ANONYMOUS_LABEL in text_of(review)


async def test_a_create_review_refuses_a_set_that_is_not_a_candidate_for_the_registry(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await make_set(admin_client, world, "alpha")
    elsewhere = await make_set(admin_client, world, "elsewhere", address="mirror.example.com")

    refused = await admin_client.post(
        f"/environments/{world.remote_id}/containers",
        data=csrf_form(
            admin_client, **create_form(image=REGISTRY_IMAGE), credential_choice=str(elsewhere)
        ),
    )

    assert refused.status_code == BAD_REQUEST
    assert NOT_A_CANDIDATE in text_of(refused)
    assert container_host.calls("POST", "/images/create") == []
    assert container_host.calls("POST", "/containers/create") == []


async def test_the_image_pull_page_refuses_a_set_that_is_not_a_candidate_and_shows_plain_anonymous(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await make_set(admin_client, world, "alpha")
    elsewhere = await make_set(admin_client, world, "elsewhere", address="mirror.example.com")
    pull = f"/environments/{world.remote_id}/images/pull"

    plain = await admin_client.get(f"{pull}?reference={UNSERVED_IMAGE}")
    refused = await admin_client.post(
        pull,
        data=csrf_form(admin_client, reference=REGISTRY_IMAGE, credential_choice=str(elsewhere)),
    )

    assert offered(plain) == [] and ANONYMOUS_LABEL in text_of(plain)
    assert refused.status_code == BAD_REQUEST and NOT_A_CANDIDATE in text_of(refused)
    assert container_host.calls("POST", "/images/create") == []
    assert len(world.app.state.engine.image_jobs) == 0


async def test_compose_stack_validation_offers_a_picker_for_every_image_it_would_pull(
    admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    await make_set(admin_client, world, "alpha", default=True)
    await make_set(admin_client, world, "beta")

    response = await admin_client.post(
        f"/environments/{world.remote_id}/stacks/-/validate",
        data=csrf_form(admin_client, name="pickers", compose_file=STACK_FILE),
        headers=HTMX,
    )

    assert response.status_code == OK
    assert hidden_values(response, "credential-0-reference") == [REGISTRY_IMAGE]
    assert hidden_values(response, "credential-1-reference") == [SECOND_IMAGE]
    assert len(offered(response, "credential-0-choice")) == 3
    assert len(offered(response, "credential-1-choice")) == 3
    assert re.findall(r"Credential set for", text_of(response)) != []


async def test_the_swarm_service_create_choices_offer_the_sets_and_refuse_a_plain_request(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    default = await make_set(admin_client, world, "alpha", default=True)
    other = await make_set(admin_client, world, "beta")

    fragment = await admin_client.post(
        service_choices(world), data=csrf_form(admin_client, image=REGISTRY_IMAGE), headers=HTMX
    )
    plain = await admin_client.post(
        service_choices(world), data=csrf_form(admin_client, image=REGISTRY_IMAGE)
    )

    assert fragment.status_code == OK and "<html" not in fragment.text
    assert offered(fragment) == [ANONYMOUS, str(default), str(other)]
    assert checked(fragment) == [str(default)]
    assert plain.status_code == 303
    assert swarm_host.services == {}


async def test_the_swarm_service_create_choices_offer_a_standard_user_only_the_sets_granted_to_them(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
) -> None:
    granted = await make_set(admin_client, world, "alpha", users=True)
    await make_set(admin_client, world, "beta")

    response = await standard_client.post(
        service_choices(world), data=csrf_form(standard_client, image=REGISTRY_IMAGE), headers=HTMX
    )

    assert response.status_code == OK
    assert offered(response) == [ANONYMOUS, str(granted)]
