"""Tokens over HTTP: the page that shows a value once, and the bearer path onto the JSON surface."""

import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.auth.dependencies import CurrentUser
from tests.auth_support import ADMIN_USERNAME, STANDARD_USERNAME, Browsers, csrf_form

TOKEN_SHAPE = re.compile(r"dkm_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{43}")
TOKENS_PATH = "/account/tokens"
WHOAMI_PATH = "/api/whoami"
ECHO_PATH = "/api/echo"
FIRST_TOKEN_ID = 1
OK = 200
UNAUTHENTICATED = 401
FORBIDDEN = 403
INVALID = {
    "no header": {},
    "empty bearer": {"Authorization": "Bearer "},
    "malformed bearer": {"Authorization": "Bearer not-a-token"},
    "unknown token": {"Authorization": "Bearer dkm_AAAAAAAA_" + "A" * 43},
}


@pytest.fixture
def json_surface(started_app: FastAPI) -> Iterator[FastAPI]:
    """Give the running application one authenticated JSON route that reads and one that writes."""
    routes = started_app.router.routes
    shared_route_count = len(routes)

    @started_app.get(WHOAMI_PATH)
    async def whoami(principal: CurrentUser) -> dict[str, str]:
        return {"username": principal.username, "role": principal.role}

    @started_app.post(ECHO_PATH)
    async def echo(principal: CurrentUser) -> dict[str, str]:
        return {"username": principal.username}

    yield started_app
    del routes[shared_route_count:]


async def issue_through_the_page(client: AsyncClient, label: str = "ci") -> str:
    """Create a token as the signed-in user would, and return the value the page showed."""
    response = await client.post(TOKENS_PATH, data=csrf_form(client, label=label))
    found = TOKEN_SHAPE.search(response.text)
    assert found is not None, response.text
    return found.group(0)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_a_token_authenticates_the_json_surface_as_its_owner_with_its_owners_role(
    json_surface: FastAPI,
    browsers: Browsers,
    standard_client: AsyncClient,
    admin_client: AsyncClient,
) -> None:
    standard_token = await issue_through_the_page(standard_client)
    admin_token = await issue_through_the_page(admin_client)
    caller = browsers.open()

    standard = await caller.get(WHOAMI_PATH, headers=bearer(standard_token))
    administrator = await caller.get(WHOAMI_PATH, headers=bearer(admin_token))

    assert standard.json() == {"username": STANDARD_USERNAME, "role": "standard"}
    assert administrator.json() == {"username": ADMIN_USERNAME, "role": "administrator"}


async def test_a_token_stops_working_the_instant_it_is_revoked(
    json_surface: FastAPI, browsers: Browsers, standard_client: AsyncClient
) -> None:
    token = await issue_through_the_page(standard_client)
    caller = browsers.open()
    assert (await caller.get(WHOAMI_PATH, headers=bearer(token))).status_code == OK

    await standard_client.post(
        f"{TOKENS_PATH}/{FIRST_TOKEN_ID}/revoke", data=csrf_form(standard_client)
    )

    assert (await caller.get(WHOAMI_PATH, headers=bearer(token))).status_code == UNAUTHENTICATED


async def test_the_json_surface_refuses_a_request_without_a_valid_token(
    json_surface: FastAPI, browsers: Browsers
) -> None:
    caller = browsers.open()

    responses = {
        name: await caller.get(WHOAMI_PATH, headers=headers) for name, headers in INVALID.items()
    }

    assert {name: response.status_code for name, response in responses.items()} == dict.fromkeys(
        INVALID, UNAUTHENTICATED
    )
    assert {response.headers["www-authenticate"] for response in responses.values()} == {"Bearer"}


async def test_a_bad_bearer_token_is_refused_even_alongside_a_valid_session_cookie(
    json_surface: FastAPI, standard_client: AsyncClient
) -> None:
    response = await standard_client.get(WHOAMI_PATH, headers=bearer("not-a-token"))

    assert response.status_code == UNAUTHENTICATED


async def test_a_token_means_nothing_off_the_json_surface(
    json_surface: FastAPI, browsers: Browsers, standard_client: AsyncClient
) -> None:
    token = await issue_through_the_page(standard_client)

    response = await browsers.open().get(TOKENS_PATH, headers=bearer(token), follow_redirects=False)

    assert response.headers["location"].startswith("/login")


async def test_a_state_changing_bearer_request_needs_no_csrf_token(
    json_surface: FastAPI, browsers: Browsers, standard_client: AsyncClient
) -> None:
    token = await issue_through_the_page(standard_client)

    response = await browsers.open().post(ECHO_PATH, headers=bearer(token))

    assert response.status_code == OK


async def test_a_state_changing_cookie_request_to_the_json_surface_still_needs_a_csrf_token(
    json_surface: FastAPI, standard_client: AsyncClient
) -> None:
    plain = await standard_client.post(ECHO_PATH)
    other_scheme = await standard_client.post(
        ECHO_PATH, headers={"Authorization": "Basic Zm9vOmJhcg=="}
    )

    assert (plain.status_code, other_scheme.status_code) == (FORBIDDEN, FORBIDDEN)


async def test_a_tokens_value_is_shown_on_the_page_that_creates_it_and_never_again(
    started_app: FastAPI, standard_client: AsyncClient
) -> None:
    created = await standard_client.post(TOKENS_PATH, data=csrf_form(standard_client, label="ci"))
    value = TOKEN_SHAPE.search(created.text)
    assert value is not None

    listing = await standard_client.get(TOKENS_PATH)

    assert "no-store" in created.headers["cache-control"]
    assert value.group(0) not in listing.text
    assert value.group(0)[: len("dkm_") + 8] in listing.text
