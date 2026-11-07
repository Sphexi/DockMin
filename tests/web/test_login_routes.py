"""Tests for sign-in, sign-out, the redirect to sign-in, and the public settings endpoint."""

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.auth.passwords import PasswordHasher
from app.auth.sessions import SESSION_COOKIE, hash_token
from app.web.csrf import CSRF_COOKIE
from tests.auth_support import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    CLIENT_ADDRESS,
    OTHER_ADDRESS,
    SEE_OTHER,
    Browsers,
    auth_events,
    cookie_set_by,
    csrf_form,
    csrf_headers,
    fetch_scalar,
    outcomes,
)
from tests.support import LOCKOUT_ENV

UNAUTHORIZED = 401
TOO_MANY_REQUESTS = 429
OK = 200
HTMX = {"HX-Request": "true"}
UNKNOWN_USERNAME = "nobody-here"
WRONG_PASSWORD = "not-the-password"
SESSION_TOKEN_LENGTH = 43
SESSION_ROWS = "SELECT COUNT(*) FROM session WHERE token_hash = ?"
ALL_SESSION_ROWS = "SELECT COUNT(*) FROM session"
PUBLIC_SETTINGS = {"authentication_methods": ["internal"], "instance_name": "dockmin"}


@pytest.fixture
def env(env: dict[str, str]) -> dict[str, str]:
    return env | LOCKOUT_ENV


async def _submit_login(
    client: AsyncClient, username: str, password: str, **headers: str
) -> Response:
    return await client.post(
        "/login", data=csrf_form(client, username=username, password=password), headers=headers
    )


async def test_a_successful_login_sets_a_hardened_session_cookie_and_records_the_address(
    started_app: FastAPI, browsers: Browsers
) -> None:
    client = browsers.open(OTHER_ADDRESS)
    client.cookies.clear()
    await client.get("/login")

    response = await _submit_login(client, ADMIN_USERNAME, ADMIN_PASSWORD)

    session = cookie_set_by(response, SESSION_COOKIE)
    [event] = await auth_events(started_app)
    assert (response.status_code, response.headers["location"]) == (SEE_OTHER, "/")
    assert len(session.value) == SESSION_TOKEN_LENGTH
    assert session["httponly"] is True
    assert session["secure"] is True
    assert session["samesite"].lower() == "lax"
    assert (event["action"], event["result"]) == ("login", "success")
    assert event["detail"] == {"address": OTHER_ADDRESS, "method": "password"}


async def test_a_successful_login_replaces_the_csrf_token_that_was_in_force_before(
    browsers: Browsers,
) -> None:
    client = browsers.open()
    client.cookies.clear()
    await client.get("/login")
    before = client.cookies[CSRF_COOKIE]

    response = await _submit_login(client, ADMIN_USERNAME, ADMIN_PASSWORD)

    assert cookie_set_by(response, CSRF_COOKIE).value != before


async def test_logout_makes_the_old_session_cookie_useless_at_once(
    started_app: FastAPI, browsers: Browsers
) -> None:
    client = await browsers.sign_in()
    captured = client.cookies[SESSION_COOKIE]

    response = await client.post("/logout", headers=csrf_headers(client))

    replay = await outcomes(browsers.open(session_token=captured), [("GET", "/account")])
    assert (response.status_code, response.headers["location"]) == (SEE_OTHER, "/login")
    assert replay == {("GET", "/account"): (SEE_OTHER, "/login?next=%2Faccount")}
    assert await fetch_scalar(started_app, SESSION_ROWS, (hash_token(captured),)) == 0


async def test_logout_instructs_the_browser_to_drop_both_cookies(
    standard_client: AsyncClient,
) -> None:
    response = await standard_client.post("/logout", headers=csrf_headers(standard_client))

    session = cookie_set_by(response, SESSION_COOKIE)
    csrf = cookie_set_by(response, CSRF_COOKIE)
    assert (session.value, session["max-age"]) == ("", "0")
    assert (csrf.value, csrf["max-age"]) == ("", "0")
    assert session["secure"] is True
    assert csrf["secure"] is True


async def test_a_request_without_a_session_is_sent_to_sign_in_in_the_form_the_caller_can_follow(
    anonymous_client: AsyncClient,
) -> None:
    page = await anonymous_client.get("/account")
    fragment = await anonymous_client.get("/account", headers=HTMX)
    submission = await anonymous_client.post("/account", headers=csrf_headers(anonymous_client))

    assert (page.status_code, page.headers["location"]) == (SEE_OTHER, "/login?next=%2Faccount")
    assert (fragment.status_code, fragment.headers["hx-redirect"]) == (
        UNAUTHORIZED,
        "/login?next=%2Faccount",
    )
    assert (submission.status_code, submission.headers["location"]) == (SEE_OTHER, "/login")


async def test_public_settings_answer_without_a_session_and_disclose_only_methods_and_name(
    anonymous_client: AsyncClient,
) -> None:
    response = await anonymous_client.get("/api/settings/public")

    assert response.status_code == OK
    assert response.json() == PUBLIC_SETTINGS


async def test_repeated_failures_lock_the_username_out_and_every_attempt_is_recorded(
    started_app: FastAPI, browsers: Browsers
) -> None:
    client = browsers.open()
    rows_before = await fetch_scalar(started_app, ALL_SESSION_ROWS)
    attempts = [
        await _submit_login(client, ADMIN_USERNAME, WRONG_PASSWORD),
        await _submit_login(client, ADMIN_USERNAME, WRONG_PASSWORD),
        await _submit_login(client, ADMIN_USERNAME, WRONG_PASSWORD),
        await _submit_login(client, ADMIN_USERNAME, ADMIN_PASSWORD),
    ]

    events = await auth_events(started_app)

    assert [attempt.status_code for attempt in attempts] == [
        UNAUTHORIZED,
        UNAUTHORIZED,
        UNAUTHORIZED,
        TOO_MANY_REQUESTS,
    ]
    assert int(attempts[-1].headers["retry-after"]) > 0
    assert SESSION_COOKIE not in attempts[-1].headers.get("set-cookie", "")
    assert await fetch_scalar(started_app, ALL_SESSION_ROWS) == rows_before
    assert [
        (e["actor_type"], e["result"], e["detail"]["reason"], e["detail"]["address"])
        for e in events
    ] == [
        ("anonymous", "failure", "bad_password", CLIENT_ADDRESS),
        ("anonymous", "failure", "bad_password", CLIENT_ADDRESS),
        ("anonymous", "failure", "bad_password", CLIENT_ADDRESS),
        ("anonymous", "failure", "locked_out", CLIENT_ADDRESS),
    ]


async def test_a_failed_login_for_a_nonexistent_username_is_recorded_as_anonymous_over_http(
    started_app: FastAPI, browsers: Browsers
) -> None:
    client = browsers.open()

    await _submit_login(client, UNKNOWN_USERNAME, WRONG_PASSWORD)

    [event] = await auth_events(started_app)
    assert event["actor_type"] == "anonymous"
    assert event["actor_user_id"] is None
    assert event["actor_subsystem"] is None
    assert event["detail"]["username"] == UNKNOWN_USERNAME


async def test_the_first_login_for_an_unknown_username_finds_the_dummy_hash_already_built(
    browsers: Browsers, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(self: PasswordHasher, password: str) -> str:
        raise AssertionError("a hash was built on the login path")

    monkeypatch.setattr(PasswordHasher, "hash", refuse)
    client = browsers.open()

    response = await _submit_login(client, UNKNOWN_USERNAME, WRONG_PASSWORD)

    assert response.status_code == UNAUTHORIZED
