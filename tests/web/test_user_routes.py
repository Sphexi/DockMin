"""Tests for the user-management routes: who may reach them, and what they refuse or end."""

import re

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import AsyncClient

from app.web import user_routes
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_USERNAME,
    NEW_PASSWORD,
    SEE_OTHER,
    STANDARD_ID,
    STANDARD_PASSWORD,
    STANDARD_USERNAME,
    Browsers,
    auth_events,
    csrf_form,
    csrf_headers,
    fetch_scalar,
    lock_out,
    outcomes,
)
from tests.support import LOCKOUT_ENV, present
from tests.web.flash_support import flash_texts, land, landed_texts, location

TOO_MANY_REQUESTS = 429
FORBIDDEN = 403
CONFLICT = 409
OK = 200
LOGIN_PREFIX = "/login"
USER_MANAGEMENT_ENDPOINTS = 13
LAST_ADMIN_PHRASE = "last administrator"
ADMIN_STATE = "SELECT role || ':' || is_enabled FROM user WHERE id = ?"
ADMIN_STATE_UNCHANGED = "administrator:1"
PASSWORD_RESET_NOTICE = (
    "Password reset. Their sessions were ended and their API tokens and webhook URLs revoked."
)
PASSWORD_CHANGED_NOTICE = (
    "Password changed. Your other sessions have been signed out"
    " and your API tokens and webhook URLs revoked."
)
LOCK_LIFTED_NOTICE = "Sign-in lock lifted."
# Valid for every route that reads a form, so a missing guard shows as a success and not a 422.
VALID_FORM = {
    "username": "someone-new",
    "role": "standard",
    "password": NEW_PASSWORD,
    "new_password": NEW_PASSWORD,
}


def user_management_requests() -> list[tuple[str, str]]:
    """Return every (method, path) the user-management router serves."""
    return [
        (method, route.path.replace("{user_id}", str(ADMIN_ID)))
        for route in user_routes.router.routes
        if isinstance(route, APIRoute)
        for method in sorted(present(route.methods) - {"HEAD", "OPTIONS"})
    ]


def test_every_user_management_route_is_found_for_the_access_tests() -> None:
    assert len(user_management_requests()) == USER_MANAGEMENT_ENDPOINTS


async def test_a_standard_user_is_forbidden_every_user_management_route_and_method(
    started_app: FastAPI, standard_client: AsyncClient
) -> None:
    requests = user_management_requests()

    result = await outcomes(
        standard_client, requests, data=VALID_FORM, headers=csrf_headers(standard_client)
    )

    assert result == dict.fromkeys(requests, (FORBIDDEN, None))
    assert await fetch_scalar(started_app, ADMIN_STATE, (ADMIN_ID,)) == ADMIN_STATE_UNCHANGED


async def test_a_caller_without_a_session_is_sent_to_sign_in_from_every_user_management_route(
    started_app: FastAPI, anonymous_client: AsyncClient
) -> None:
    requests = user_management_requests()

    result = await outcomes(
        anonymous_client, requests, data=VALID_FORM, headers=csrf_headers(anonymous_client)
    )

    assert {(status, (location or "").split("?")[0]) for status, location in result.values()} == {
        (SEE_OTHER, LOGIN_PREFIX)
    }


async def test_an_administrator_reaches_the_user_list(admin_client: AsyncClient) -> None:
    response = await admin_client.get("/users")

    assert response.status_code == OK
    assert ADMIN_USERNAME in response.text
    assert STANDARD_USERNAME in response.text


async def test_the_last_administrator_cannot_be_disabled_demoted_or_deleted_and_is_told_why(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    headers = csrf_headers(admin_client)

    responses = [
        await admin_client.post(f"/users/{ADMIN_ID}/disable", headers=headers),
        await admin_client.post(
            f"/users/{ADMIN_ID}",
            data={"username": ADMIN_USERNAME, "role": "standard"},
            headers=headers,
        ),
        await admin_client.post(
            f"/users/{ADMIN_ID}/delete", data={"expected_name": ADMIN_USERNAME}, headers=headers
        ),
    ]

    assert [response.status_code for response in responses] == [CONFLICT] * 3
    assert [r for r in responses if LAST_ADMIN_PHRASE not in r.text] == []
    assert await fetch_scalar(started_app, ADMIN_STATE, (ADMIN_ID,)) == ADMIN_STATE_UNCHANGED


async def test_an_administrator_reset_signs_the_user_out_everywhere(
    admin_client: AsyncClient, standard_client: AsyncClient
) -> None:
    response = await admin_client.post(
        f"/users/{STANDARD_ID}/password",
        data={"new_password": NEW_PASSWORD},
        headers=csrf_headers(admin_client),
    )

    notices = await landed_texts(admin_client, response)
    afterwards = await outcomes(standard_client, [("GET", "/account")])
    assert (response.status_code, location(response)) == (SEE_OTHER, f"/users/{STANDARD_ID}")
    assert PASSWORD_RESET_NOTICE in notices
    assert afterwards == {("GET", "/account"): (SEE_OTHER, "/login?next=%2Faccount")}


async def test_a_self_service_password_change_signs_out_other_sessions_but_not_this_one(
    browsers: Browsers, standard_client: AsyncClient
) -> None:
    current = await browsers.sign_in(STANDARD_USERNAME, STANDARD_PASSWORD)

    response = await current.post(
        "/account/password",
        data={
            "current_password": STANDARD_PASSWORD,
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD,
        },
        headers=csrf_headers(current),
    )

    landing = await land(current, response)
    other = await outcomes(standard_client, [("GET", "/account")])
    assert (response.status_code, location(response)) == (SEE_OTHER, "/account")
    assert PASSWORD_CHANGED_NOTICE in flash_texts(landing.text)
    assert other == {("GET", "/account"): (SEE_OTHER, "/login?next=%2Faccount")}
    assert landing.status_code == OK


class TestLiftingALockout:
    @pytest.fixture
    def env(self, env: dict[str, str]) -> dict[str, str]:
        return env | LOCKOUT_ENV

    async def test_an_administrator_lifts_the_lock_so_the_user_can_sign_in_and_the_lift_is_recorded(
        self, started_app: FastAPI, admin_client: AsyncClient, browsers: Browsers
    ) -> None:
        await lock_out(started_app.state.database, "username", STANDARD_USERNAME)
        user = browsers.open()
        blocked = await user.post(
            "/login", data=csrf_form(user, username=STANDARD_USERNAME, password=STANDARD_PASSWORD)
        )

        lifted = await admin_client.post(
            f"/users/{STANDARD_ID}/lockout/clear", headers=csrf_headers(admin_client)
        )

        notices = await landed_texts(admin_client, lifted)
        allowed = await user.post(
            "/login", data=csrf_form(user, username=STANDARD_USERNAME, password=STANDARD_PASSWORD)
        )
        [event] = [e for e in await auth_events(started_app) if e["action"] == "lockout_cleared"]
        assert blocked.status_code == TOO_MANY_REQUESTS
        assert (lifted.status_code, location(lifted)) == (SEE_OTHER, f"/users/{STANDARD_ID}")
        assert LOCK_LIFTED_NOTICE in notices
        assert allowed.status_code == SEE_OTHER
        assert (event["actor_user_id"], event["target_id"]) == (ADMIN_ID, str(STANDARD_ID))


async def test_an_administrator_revokes_one_token_from_the_users_page_and_only_that_one(
    admin_client: AsyncClient, standard_client: AsyncClient
) -> None:
    from tests.web.environment_support import TOKEN_SHAPE, bearer, issue_token

    kept = await issue_token(standard_client)
    second = await standard_client.post(
        "/account/tokens", data=csrf_form(standard_client, label="second")
    )
    revoked = TOKEN_SHAPE.search(second.text).group(0)  # type: ignore[union-attr]
    page = await admin_client.get(f"/users/{STANDARD_ID}")
    token_id = int(re.findall(rf"/users/{STANDARD_ID}/tokens/(\d+)/revoke", page.text)[-1])

    response = await admin_client.post(
        f"/users/{STANDARD_ID}/tokens/{token_id}/revoke", headers=csrf_headers(admin_client)
    )
    notices = await landed_texts(admin_client, response)
    after = [
        (await admin_client.get("/api/v1/environments", headers=bearer(value))).status_code
        for value in (kept, revoked)
    ]

    assert re.findall(rf"/users/{STANDARD_ID}/tokens/\d+/revoke", page.text)
    assert (response.status_code, location(response)) == (SEE_OTHER, f"/users/{STANDARD_ID}")
    assert "Token revoked." in notices
    assert sorted(after) == [200, 401]


async def test_a_token_cannot_be_revoked_through_the_page_of_a_different_user(
    admin_client: AsyncClient, standard_client: AsyncClient
) -> None:
    from tests.web.environment_support import bearer, issue_token

    value = await issue_token(standard_client)
    page = await admin_client.get(f"/users/{STANDARD_ID}")
    token_id = re.findall(rf"/users/{STANDARD_ID}/tokens/(\d+)/revoke", page.text)[-1]

    response = await admin_client.post(
        f"/users/{ADMIN_ID}/tokens/{token_id}/revoke", headers=csrf_headers(admin_client)
    )
    still = await admin_client.get("/api/v1/environments", headers=bearer(value))

    assert response.status_code == 404
    assert still.status_code == 200
