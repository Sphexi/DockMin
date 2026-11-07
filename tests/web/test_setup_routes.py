"""Tests for first-run setup over HTTP."""

import io

from fastapi import FastAPI
from httpx import AsyncClient, Response

from tests.auth_support import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    CLIENT_ADDRESS,
    Browsers,
    auth_events,
    csrf_form,
    fetch_scalar,
    outcomes,
)

WRONG_TOKEN = "definitely-not-the-setup-token"
SEE_OTHER = 303
FORBIDDEN = 403
NOT_FOUND = 404
UNAVAILABLE = 503
OK = 200
SETUP = "/setup"
LOGIN = "/login"
LOGIN_THEN_HOME = "/login?next=%2F"

OPEN_WINDOW_REQUESTS = [
    ("GET", "/"),
    ("GET", "/users"),
    ("GET", "/account"),
    ("GET", "/ui-kit"),
    ("GET", LOGIN),
    ("GET", "/api/settings/public"),
    ("GET", SETUP),
    ("GET", "/api/health"),
    ("GET", "/api/ready"),
]
OPEN_WINDOW_OUTCOMES = {
    ("GET", "/"): (SEE_OTHER, SETUP),
    ("GET", "/users"): (SEE_OTHER, SETUP),
    ("GET", "/account"): (SEE_OTHER, SETUP),
    ("GET", "/ui-kit"): (SEE_OTHER, SETUP),
    ("GET", LOGIN): (SEE_OTHER, SETUP),
    ("GET", "/api/settings/public"): (UNAVAILABLE, None),
    ("GET", SETUP): (OK, None),
    ("GET", "/api/health"): (OK, None),
    ("GET", "/api/ready"): (OK, None),
}


async def _submit_setup(client: AsyncClient, token: str, **overrides: str) -> Response:
    fields = {
        "setup_token": token,
        "username": ADMIN_USERNAME,
        "password": ADMIN_PASSWORD,
        "password_confirm": ADMIN_PASSWORD,
    }
    return await client.post(SETUP, data=csrf_form(client, **(fields | overrides)))


def _token(app: FastAPI) -> str:
    token = app.state.auth.setup.token
    assert token is not None
    return str(token)


async def test_a_fresh_instance_offers_the_setup_form_and_sends_everything_else_to_it(
    fresh_browsers: Browsers,
) -> None:
    client = fresh_browsers.open()

    result = await outcomes(client, OPEN_WINDOW_REQUESTS)

    assert result == OPEN_WINDOW_OUTCOMES


async def test_a_wrong_setup_token_is_refused_and_recorded_without_creating_an_account(
    fresh_app: FastAPI, fresh_browsers: Browsers
) -> None:
    client = fresh_browsers.open()

    response = await _submit_setup(client, WRONG_TOKEN)

    [event] = await auth_events(fresh_app)
    assert response.status_code == FORBIDDEN
    assert WRONG_TOKEN not in response.text
    assert await fetch_scalar(fresh_app, "SELECT COUNT(*) FROM user") == 0
    assert (event["action"], event["result"]) == ("bootstrap", "failure")
    assert event["detail"] == {"address": CLIENT_ADDRESS, "reason": "bad_setup_token"}


async def test_the_right_setup_token_creates_the_first_administrator_and_sends_them_to_sign_in(
    fresh_app: FastAPI, fresh_browsers: Browsers
) -> None:
    client = fresh_browsers.open()

    response = await _submit_setup(client, _token(fresh_app))

    assert (response.status_code, response.headers["location"]) == (SEE_OTHER, LOGIN)
    role = await fetch_scalar(
        fresh_app, "SELECT role FROM user WHERE username = ?", (ADMIN_USERNAME,)
    )
    assert role == "administrator"


async def test_once_setup_is_done_the_setup_route_answers_404_not_a_redirect(
    fresh_app: FastAPI, fresh_browsers: Browsers
) -> None:
    client = fresh_browsers.open()
    token = _token(fresh_app)
    await _submit_setup(client, token)

    paths = await outcomes(client, [("GET", SETUP), ("GET", "/")])
    replay = await _submit_setup(client, token, username="second-admin")

    assert paths == {("GET", SETUP): (NOT_FOUND, None), ("GET", "/"): (SEE_OTHER, LOGIN_THEN_HOME)}
    assert replay.status_code == NOT_FOUND
    assert await fetch_scalar(fresh_app, "SELECT COUNT(*) FROM user") == 1


async def test_after_a_restart_the_setup_route_is_still_404_and_no_token_exists(
    started_app: FastAPI, anonymous_client: AsyncClient
) -> None:
    paths = await outcomes(anonymous_client, [("GET", SETUP)])
    submission = await _submit_setup(anonymous_client, WRONG_TOKEN)

    assert paths == {("GET", SETUP): (NOT_FOUND, None)}
    assert submission.status_code == NOT_FOUND
    assert started_app.state.auth.setup.token is None


async def test_the_setup_token_appears_in_no_response_body_or_header(
    fresh_app: FastAPI, fresh_browsers: Browsers
) -> None:
    client = fresh_browsers.open()
    token = _token(fresh_app)

    responses = [
        await client.get(SETUP),
        await _submit_setup(client, WRONG_TOKEN),
        await _submit_setup(client, token, password="short", password_confirm="short"),
        await _submit_setup(client, token),
        await client.get(SETUP),
    ]

    assert [r for r in responses if token in r.text or token in str(r.headers)] == []


def test_the_setup_token_is_announced_in_the_log_in_full(
    fresh_app: FastAPI, log_stream: io.StringIO
) -> None:
    assert _token(fresh_app) in log_stream.getvalue()
