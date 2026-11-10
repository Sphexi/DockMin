"""The change-credential-set pages: who may open them, what they show and what a post changes."""

import io
from html import unescape

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.registry_client.manifests import ManifestCheckKind
from app.services.image_bindings import (
    REBIND_CHANGED,
    REBIND_PINNED,
    REBIND_UNBOUND,
)
from app.web.csrf import CSRF_FORM_FIELD
from tests.auth_support import csrf_form, read_events
from tests.docker_engine.container_engine import ContainerEngine
from tests.image_support import binding_rows, mark_missing, rows_for
from tests.web.environment_support import World
from tests.web.flash_support import landed_texts
from tests.web.helpers import checked_values, hidden_inputs, inputs_named
from tests.web.image_support import (
    ScriptedManifests,
    bind,
    images_path,
    links_in,
    make_set,
    text_of,
)
from tests.web.registry_support import leaks, rendered

OK, SEE_OTHER, BAD_REQUEST, NOT_FOUND, CONFLICT = 200, 303, 400, 404, 409
REFERENCE = "registry.example.com/app:1"
GHCR_REFERENCE = "ghcr.io/o/app:2"
PINNED = f"registry.example.com/app@sha256:{'1' * 64}"
SECRET = "rebind-page-secret-marker-6641"
SECRET_USER = "rebind-page-user-marker"
ANONYMOUS = "anonymous"
SUCCESS_NOTICE = "The binding now uses the chosen credential set."
REMOVED_NOTICE = "The binding was removed."
AUTHENTICATION_MESSAGE = "The registry refused access"
REBOUND = "image_rebound"


@pytest.fixture
def manifests(world: World) -> ScriptedManifests:
    scripted = ScriptedManifests()
    world.app.state.registries.manifests = scripted
    return scripted


def rebind_path(world: World) -> str:
    return f"{images_path(world)}/bindings/rebind"


async def review(client: AsyncClient, world: World, reference: str = REFERENCE) -> Response:
    return await client.get(rebind_path(world), params={"reference": reference})


async def reviewed_form(
    client: AsyncClient, world: World, reference: str = REFERENCE, **changes: str
) -> dict[str, str]:
    """Fill the rebind form as a browser would: the page's hidden fields, then the choice."""
    page = await review(client, world, reference)
    hidden = {k: v for k, v in hidden_inputs(page.text).items() if k != CSRF_FORM_FIELD}
    return csrf_form(client, **(hidden | changes))


async def bound_pair(admin_client: AsyncClient, world: World) -> tuple[int, int]:
    original = await make_set(admin_client, world, "original")
    replacement = await make_set(admin_client, world, "replacement")
    await bind(world, original, REFERENCE)
    return original, replacement


def offered_choices(response: Response) -> set[str]:
    return {tag.attrs["value"] for tag in inputs_named(response.text, "credential_choice")}


async def test_the_page_shows_the_reference_the_current_binding_and_the_sets_that_fit(
    admin_client: AsyncClient, world: World
) -> None:
    original, replacement = await bound_pair(admin_client, world)
    elsewhere = await make_set(admin_client, world, "elsewhere", address="mirror.example.com:5000")

    page = await review(admin_client, world)

    text = text_of(page)
    pull = f"{images_path(world)}/pull?reference="
    assert page.status_code == OK
    assert page.headers["cache-control"] == "no-store"
    assert REFERENCE in text and "Bound to original" in text
    assert "Verify and change binding" in text and "Pull the image instead" in text
    assert any(link.startswith(pull) for link in links_in(page))
    assert checked_values(page.text, "credential_choice") == [str(original)]
    assert offered_choices(page) == {str(original), str(replacement), ANONYMOUS}
    assert str(elsewhere) not in offered_choices(page)
    assert hidden_inputs(page.text)["expected_state"] == "bound"
    assert hidden_inputs(page.text)["expected_set"] == str(original)


async def test_a_post_that_the_registry_accepts_repoints_the_binding_and_lands_with_a_notice(
    admin_client: AsyncClient, world: World, manifests: ScriptedManifests
) -> None:
    _, replacement = await bound_pair(admin_client, world)
    form = await reviewed_form(admin_client, world, credential_choice=str(replacement))

    response = await admin_client.post(rebind_path(world), data=form)

    async with world.app.state.database.session() as db:
        [binding] = await binding_rows(db)
        [event] = await rows_for(db, REBOUND)
    assert response.status_code == SEE_OTHER
    assert response.headers["location"] == images_path(world)
    assert SUCCESS_NOTICE in await landed_texts(admin_client, response)
    assert binding["registry_credential_id"] == replacement
    assert (event["result"], event["detail"]["verified"]) == ("success", True)
    assert [key.key for _, key, _ in manifests.calls] == [REFERENCE]


async def test_a_set_the_registry_refuses_leaves_the_binding_and_explains_beside_the_choice(
    admin_client: AsyncClient, world: World, manifests: ScriptedManifests
) -> None:
    original, replacement = await bound_pair(admin_client, world)
    manifests.answers[REFERENCE] = ManifestCheckKind.AUTHENTICATION
    form = await reviewed_form(admin_client, world, credential_choice=str(replacement))

    response = await admin_client.post(rebind_path(world), data=form)

    async with world.app.state.database.session() as db:
        [binding] = await binding_rows(db)
        [event] = await rows_for(db, REBOUND)
    assert response.status_code == BAD_REQUEST
    assert AUTHENTICATION_MESSAGE in text_of(response)
    assert binding["registry_credential_id"] == original
    assert (event["result"], event["detail"]["reason"]) == ("failure", "authentication")


async def test_an_anonymous_choice_removes_the_binding_without_asking_the_registry(
    admin_client: AsyncClient, world: World, manifests: ScriptedManifests
) -> None:
    await bound_pair(admin_client, world)
    form = await reviewed_form(admin_client, world, credential_choice=ANONYMOUS)

    response = await admin_client.post(rebind_path(world), data=form)

    async with world.app.state.database.session() as db:
        remaining = await binding_rows(db)
    assert response.status_code == SEE_OTHER
    assert REMOVED_NOTICE in await landed_texts(admin_client, response)
    assert remaining == []
    assert manifests.calls == []


async def test_a_binding_changed_since_the_review_is_a_conflict_that_links_back_to_the_review(
    admin_client: AsyncClient, world: World, manifests: ScriptedManifests
) -> None:
    original, replacement = await bound_pair(admin_client, world)
    form = await reviewed_form(admin_client, world, credential_choice=str(replacement))
    third = await make_set(admin_client, world, "third")
    async with world.app.state.database.session() as db:
        await db.execute("UPDATE image_credential_binding SET registry_credential_id = ?", (third,))

    response = await admin_client.post(rebind_path(world), data=form)

    async with world.app.state.database.session() as db:
        [binding] = await binding_rows(db)
    assert response.status_code == CONFLICT, text_of(response)
    assert REBIND_CHANGED in text_of(response)
    assert any("bindings/rebind?reference=" in link for link in links_in(response))
    assert binding["registry_credential_id"] == third
    assert original != third


async def test_a_credential_missing_binding_is_repaired_from_the_page(
    admin_client: AsyncClient, world: World, manifests: ScriptedManifests
) -> None:
    original, replacement = await bound_pair(admin_client, world)
    async with world.app.state.database.session() as db:
        await mark_missing(db, original)
    page = await review(admin_client, world)
    form = await reviewed_form(admin_client, world, credential_choice=str(replacement))

    response = await admin_client.post(rebind_path(world), data=form)

    async with world.app.state.database.session() as db:
        [binding] = await binding_rows(db)
    assert "Bound set deleted" in text_of(page)
    assert "was bound to was deleted, so updates use anonymous access." in text_of(page)
    assert checked_values(page.text, "credential_choice") == [ANONYMOUS]
    assert response.status_code == SEE_OTHER, text_of(response)
    assert (binding["state"], binding["registry_credential_id"]) == ("bound", replacement)


async def test_a_binding_whose_registry_address_changed_says_so_instead_of_deleted(
    admin_client: AsyncClient, world: World, manifests: ScriptedManifests
) -> None:
    original, _ = await bound_pair(admin_client, world)
    async with world.app.state.database.session() as db:
        await mark_missing(db, original)
        await db.execute("UPDATE image_credential_binding SET missing_reason = 'address_changed'")

    page = await review(admin_client, world)

    assert "Registry address changed" in text_of(page)
    assert "address of the credential set this image was bound to changed, so updates" in text_of(
        page
    )
    assert "was deleted" not in text_of(page)


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        pytest.param(PINNED, REBIND_PINNED, id="pinned-by-digest"),
        pytest.param("registry.example.com/never:1", REBIND_UNBOUND, id="without-a-binding"),
    ],
)
async def test_a_reference_that_cannot_be_rebound_gets_a_page_that_says_why(
    admin_client: AsyncClient, world: World, reference: str, message: str
) -> None:
    await bound_pair(admin_client, world)

    page = await review(admin_client, world, reference)

    assert page.status_code == BAD_REQUEST
    assert message in text_of(page)


async def test_a_standard_user_finds_neither_route_and_changes_nothing(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World
) -> None:
    original, replacement = await bound_pair(admin_client, world)
    form = await reviewed_form(admin_client, world, credential_choice=str(replacement))
    form[CSRF_FORM_FIELD] = csrf_form(standard_client)[CSRF_FORM_FIELD]

    page = await review(standard_client, world)
    post = await standard_client.post(rebind_path(world), data=form)

    async with world.app.state.database.session() as db:
        [binding] = await binding_rows(db)
    assert (page.status_code, post.status_code) == (NOT_FOUND, NOT_FOUND)
    assert binding["registry_credential_id"] == original


async def test_the_list_offers_a_change_link_to_administrators_only(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    daemon: ContainerEngine,
) -> None:
    alpha = await make_set(admin_client, world, "alpha", address="ghcr.io")
    await bind(world, alpha, GHCR_REFERENCE)

    by_admin = await admin_client.get(images_path(world))
    by_standard = await standard_client.get(images_path(world))

    assert unescape(by_admin.text).count("Change the credential set of") == 1
    assert "Change the credential set of" not in by_standard.text


async def test_no_secret_reaches_a_page_a_row_or_the_log_though_the_registry_check_received_it(
    admin_client: AsyncClient,
    world: World,
    manifests: ScriptedManifests,
    log_stream: io.StringIO,
) -> None:
    await bound_pair(admin_client, world)
    chosen = await make_set(admin_client, world, "sentinel", username=SECRET_USER, secret=SECRET)
    responses = [await review(admin_client, world)]
    form = await reviewed_form(admin_client, world, credential_choice=str(chosen))
    posted = await admin_client.post(rebind_path(world), data=form)
    responses += [posted, await admin_client.get(posted.headers["location"])]

    app: FastAPI = world.app
    async with app.state.database.session() as db:
        events = repr(await read_events(db))
        bindings = repr(await binding_rows(db))
    [(_, _, login)] = manifests.calls
    shown = [rendered(response) for response in responses] + [
        events,
        bindings,
        log_stream.getvalue(),
    ]
    assert login is not None and login.secret.get_secret_value() == SECRET
    assert leaks([f"check saw {SECRET} {SECRET_USER}"], [SECRET, SECRET_USER]) != []
    assert leaks(shown, [SECRET, SECRET_USER]) == []
