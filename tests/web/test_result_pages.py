"""Stored outcomes of multi-target posts: who may read one, how long, how many, and refresh."""

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.web.results import MAX_RESULTS, MAX_RESULTS_PER_USER, RESULT_TTL_SECONDS, ResultStore
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_PASSWORD,
    NEW_PASSWORD,
    STANDARD_ID,
    STANDARD_USERNAME,
    csrf_form,
    csrf_headers,
)
from tests.web.environment_support import DOWN_NAME, World, rows
from tests.web.error_support import without_reference
from tests.web.flash_support import SEE_OTHER, Ticker, land, location
from tests.web.registry_support import create_set, custom_form, visible_text

TEMPLATE = "results/anything.html"
CONTEXT = {"names": ["a"]}
RESULT_PREFIX = "/results/"
NOT_FOUND = 404
UNKNOWN_TOKEN = "A" * 32
MALFORMED_TOKEN = "short"
USERS = range(1, 12)


@pytest.fixture
def store() -> ResultStore:
    return ResultStore(clock=Ticker(start=0.0))


def stored(store: ResultStore, user_id: int) -> str:
    return store.put(user_id, TEMPLATE, CONTEXT)


def alive(store: ResultStore, user_id: int, tokens: list[str]) -> int:
    return sum(store.get(user_id, token) is not None for token in tokens)


def test_a_result_is_returned_to_the_user_it_was_stored_for_and_to_no_one_else(
    store: ResultStore,
) -> None:
    token = stored(store, ADMIN_ID)

    assert store.get(ADMIN_ID, token) == (TEMPLATE, CONTEXT)
    assert store.get(STANDARD_ID, token) is None
    assert store.get(ADMIN_ID, UNKNOWN_TOKEN) is None


@pytest.mark.parametrize(
    ("elapsed", "present"),
    [(RESULT_TTL_SECONDS - 1, True), (RESULT_TTL_SECONDS, False)],
    ids=["just-inside", "at-the-limit"],
)
def test_a_result_lives_for_its_time_to_live(elapsed: float, present: bool) -> None:
    clock = Ticker(start=0.0)
    store = ResultStore(clock=clock)
    token = stored(store, ADMIN_ID)

    clock.advance(elapsed)

    assert (store.get(ADMIN_ID, token) is not None) is present


def test_a_users_oldest_result_is_evicted_past_their_cap_and_other_users_keep_theirs(
    store: ResultStore,
) -> None:
    theirs = stored(store, STANDARD_ID)
    own = [stored(store, ADMIN_ID) for _ in range(MAX_RESULTS_PER_USER + 1)]

    assert store.get(ADMIN_ID, own[0]) is None
    assert alive(store, ADMIN_ID, own[1:]) == MAX_RESULTS_PER_USER
    assert store.get(STANDARD_ID, theirs) is not None


def test_the_store_drops_its_oldest_result_overall_past_the_global_cap() -> None:
    store = ResultStore(clock=Ticker(start=0.0))
    issued = {user: [stored(store, user) for _ in range(MAX_RESULTS_PER_USER)] for user in USERS}

    survivors = sum(alive(store, user, tokens) for user, tokens in issued.items())

    assert len(USERS) * MAX_RESULTS_PER_USER > MAX_RESULTS
    assert survivors == MAX_RESULTS
    assert store.get(USERS[0], issued[USERS[0]][0]) is None
    assert store.get(USERS[-1], issued[USERS[-1]][-1]) is not None


def test_forgetting_a_user_drops_all_of_their_results_and_none_of_anyone_elses(
    store: ResultStore,
) -> None:
    mine = [stored(store, ADMIN_ID) for _ in range(2)]
    theirs = stored(store, STANDARD_ID)

    store.forget_user(ADMIN_ID)

    assert alive(store, ADMIN_ID, mine) == 0
    assert store.get(STANDARD_ID, theirs) is not None


async def delete_environment(client: AsyncClient, world: World) -> str:
    response = await client.post(
        "/environments/delete",
        data=csrf_form(client) | {"id": [str(world.down_id)], "expected_name": [DOWN_NAME]},
    )
    assert response.status_code == SEE_OTHER
    return location(response)


async def test_a_token_that_is_foreign_unknown_malformed_or_expired_is_the_same_404(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    result_clock: Ticker,
) -> None:
    path = await delete_environment(admin_client, world)
    expired_path = await delete_environment(admin_client, world)
    result_clock.advance(RESULT_TTL_SECONDS)

    foreign = await standard_client.get(path)
    unknown = await standard_client.get(f"{RESULT_PREFIX}{UNKNOWN_TOKEN}")
    malformed = await standard_client.get(f"{RESULT_PREFIX}{MALFORMED_TOKEN}")
    expired = await standard_client.get(expired_path)
    own_expired = await admin_client.get(expired_path)
    own_unknown = await admin_client.get(f"{RESULT_PREFIX}{UNKNOWN_TOKEN}")

    refusals = (foreign, unknown, malformed, expired, own_expired, own_unknown)
    assert {r.status_code for r in refusals} == {NOT_FOUND}
    assert {without_reference(r.text) for r in (foreign, unknown, malformed, expired)} == {
        without_reference(unknown.text)
    }
    assert without_reference(own_expired.text) == without_reference(own_unknown.text)


async def test_a_deletion_redirects_to_its_result_and_refreshing_the_result_repeats_nothing(
    admin_client: AsyncClient, anonymous_client: AsyncClient, world: World
) -> None:
    path = await delete_environment(admin_client, world)

    first = await admin_client.get(path)
    second = await admin_client.get(path)
    signed_out = await anonymous_client.get(path)

    environments = await rows(world, "SELECT id FROM environment")
    deletions = await rows(
        world, "SELECT id FROM activity_event WHERE action = 'environment_deleted'"
    )
    assert path.startswith(RESULT_PREFIX)
    assert (first.status_code, first.headers["cache-control"]) == (200, "no-store")
    assert first.text.count(DOWN_NAME) >= 1
    assert second.text == first.text
    assert len(environments) == 2
    assert len(deletions) == 1
    assert signed_out.status_code == SEE_OTHER and "/login" in location(signed_out)


async def test_deleting_a_registry_set_redirects_to_its_result_and_shows_it_on_the_landing(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    credential_id = await create_set(admin_client, custom_form(admin_client, "doomed"))

    deleted = await admin_client.post(
        f"/registries/{credential_id}/delete",
        data=csrf_form(admin_client, expected_name="doomed", expected_bindings="0"),
    )
    page = await land(admin_client, deleted)

    assert location(deleted).startswith(RESULT_PREFIX)
    assert page.status_code == 200
    assert "Registry deleted" in visible_text(page.text)
    assert "doomed" in page.text


def plant(app: FastAPI) -> dict[int, str]:
    results: ResultStore = app.state.results
    return {user: stored(results, user) for user in (ADMIN_ID, STANDARD_ID)}


def survivors(app: FastAPI, planted: dict[int, str]) -> dict[int, bool]:
    results: ResultStore = app.state.results
    return {user: results.get(user, token) is not None for user, token in planted.items()}


async def test_a_users_results_are_forgotten_when_their_access_changes_and_no_one_elses(
    started_app: FastAPI, admin_client: AsyncClient, standard_client: AsyncClient
) -> None:
    planted = plant(started_app)
    await admin_client.post(
        f"/users/{STANDARD_ID}/password",
        data={"new_password": NEW_PASSWORD},
        headers=csrf_headers(admin_client),
    )
    after_reset = survivors(started_app, planted)

    planted = plant(started_app)
    await admin_client.post(
        f"/users/{STANDARD_ID}",
        data={"username": STANDARD_USERNAME, "role": "standard"},
        headers=csrf_headers(admin_client),
    )
    after_update = survivors(started_app, planted)

    planted = plant(started_app)
    await admin_client.post(
        "/account/password",
        data={
            "current_password": ADMIN_PASSWORD,
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD,
        },
        headers=csrf_headers(admin_client),
    )
    after_own_change = survivors(started_app, planted)

    planted = plant(started_app)
    await admin_client.post("/logout", headers=csrf_headers(admin_client))
    after_logout = survivors(started_app, planted)

    assert after_reset == {ADMIN_ID: True, STANDARD_ID: False}
    assert after_update == {ADMIN_ID: True, STANDARD_ID: False}
    assert after_own_change == {ADMIN_ID: False, STANDARD_ID: True}
    assert after_logout == {ADMIN_ID: False, STANDARD_ID: True}
