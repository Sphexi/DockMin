"""Tests that every mutating request must carry the CSRF token, whatever sent it."""

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import ENV_PREFIX
from app.web.csrf import CSRF_FORM_FIELD, CSRF_HEADER
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_SESSION_TOKEN,
    ADMIN_USERNAME,
    BASE_URL,
    Browsers,
    csrf_headers,
    fetch_scalar,
)
from tests.web.flash_support import location

FORBIDDEN = 403
SEE_OTHER = 303
NO_CONTENT = 204
ACCOUNT_PATH = "/account"
HTMX = {"HX-Request": "true"}
WRONG_TOKEN = "W" * 43
RENAMED = "renamed-admin"
FOREIGN_ORIGIN = "https://evil.example"
FOREIGN_REFERER = "https://evil.example/forms/attack.html"
SAME_SITE_REFERER = f"{BASE_URL}/account"
NULL_ORIGIN = "null"
PROXY_ORIGIN = "https://proxy.example"
USERNAME_SQL = "SELECT username FROM user WHERE id = ?"

REFUSED = (
    "no token",
    "wrong form token",
    "wrong header token",
    "foreign origin with a valid token",
    "htmx without a token",
    "htmx with a wrong token",
    "login without a token",
)


def _refused_requests(client: AsyncClient) -> dict[str, tuple[str, dict[str, Any]]]:
    valid = csrf_headers(client)
    form = {"username": RENAMED}
    return {
        "no token": ("/account", {"data": form}),
        "wrong form token": ("/account", {"data": form | {CSRF_FORM_FIELD: WRONG_TOKEN}}),
        "wrong header token": ("/account", {"data": form, "headers": {CSRF_HEADER: WRONG_TOKEN}}),
        "foreign origin with a valid token": (
            "/account",
            {"data": form, "headers": valid | {"Origin": FOREIGN_ORIGIN}},
        ),
        "htmx without a token": ("/account", {"data": form, "headers": HTMX}),
        "htmx with a wrong token": (
            "/account",
            {"data": form, "headers": HTMX | {CSRF_HEADER: WRONG_TOKEN}},
        ),
        "login without a token": ("/login", {"data": {"username": "u", "password": "p"}}),
    }


async def _send_all(
    client: AsyncClient, requests: dict[str, tuple[str, dict[str, Any]]]
) -> dict[str, int]:
    return {
        name: (await client.post(path, **arguments)).status_code
        for name, (path, arguments) in requests.items()
    }


async def test_a_mutating_request_without_a_valid_token_is_refused_and_changes_nothing(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    result = await _send_all(admin_client, _refused_requests(admin_client))

    assert result == dict.fromkeys(REFUSED, FORBIDDEN)
    assert await fetch_scalar(started_app, USERNAME_SQL, (ADMIN_ID,)) == ADMIN_USERNAME


async def test_a_request_with_a_valid_token_is_refused_when_it_names_no_acceptable_source(
    started_app: FastAPI, browsers: Browsers
) -> None:
    client = browsers.open(session_token=ADMIN_SESSION_TOKEN, origin=None)
    valid = csrf_headers(client)
    form = {"username": RENAMED}

    neither = await client.post("/account", data=form, headers=valid)
    null_origin = await client.post("/account", data=form, headers=valid | {"Origin": NULL_ORIGIN})
    foreign_referer = await client.post(
        "/account", data=form, headers=valid | {"Referer": FOREIGN_REFERER}
    )

    assert [neither.status_code, null_origin.status_code, foreign_referer.status_code] == [
        FORBIDDEN,
        FORBIDDEN,
        FORBIDDEN,
    ]
    assert await fetch_scalar(started_app, USERNAME_SQL, (ADMIN_ID,)) == ADMIN_USERNAME


async def test_a_request_carrying_only_a_same_site_referer_passes(
    started_app: FastAPI, browsers: Browsers
) -> None:
    client = browsers.open(session_token=ADMIN_SESSION_TOKEN, origin=None)

    response = await client.post(
        "/account",
        data={"username": RENAMED},
        headers=csrf_headers(client) | {"Referer": SAME_SITE_REFERER},
    )

    assert (response.status_code, location(response)) == (SEE_OTHER, ACCOUNT_PATH)
    assert await fetch_scalar(started_app, USERNAME_SQL, (ADMIN_ID,)) == RENAMED


async def test_a_mutating_request_carrying_the_token_in_the_header_passes(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.post(
        "/account", data={"username": RENAMED}, headers=csrf_headers(admin_client)
    )

    assert (response.status_code, location(response)) == (SEE_OTHER, ACCOUNT_PATH)


async def test_a_mutating_request_carrying_the_token_in_the_form_passes(
    admin_client: AsyncClient,
) -> None:
    token = csrf_headers(admin_client)[CSRF_HEADER]

    response = await admin_client.post(
        "/account", data={"username": RENAMED, CSRF_FORM_FIELD: token}
    )

    assert (response.status_code, location(response)) == (SEE_OTHER, ACCOUNT_PATH)


async def test_an_htmx_request_carrying_the_token_in_its_header_passes(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    response = await admin_client.post(
        "/account",
        data={"username": RENAMED},
        headers=HTMX | csrf_headers(admin_client),
    )

    assert (response.status_code, response.headers["HX-Redirect"]) == (NO_CONTENT, ACCOUNT_PATH)
    assert await fetch_scalar(started_app, USERNAME_SQL, (ADMIN_ID,)) == RENAMED


class TestTrustedOrigins:
    @pytest.fixture
    def env(self, env: dict[str, str]) -> dict[str, str]:
        return env | {f"{ENV_PREFIX}TRUSTED_ORIGINS": PROXY_ORIGIN}

    async def test_an_origin_on_the_trusted_list_passes_with_a_valid_token(
        self, admin_client: AsyncClient
    ) -> None:
        response = await admin_client.post(
            "/account",
            data={"username": RENAMED},
            headers=csrf_headers(admin_client) | {"Origin": PROXY_ORIGIN},
        )

        assert (response.status_code, location(response)) == (SEE_OTHER, ACCOUNT_PATH)

    async def test_an_origin_off_the_list_is_still_refused(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post(
            "/account",
            data={"username": RENAMED},
            headers=csrf_headers(admin_client) | {"Origin": FOREIGN_ORIGIN},
        )

        assert response.status_code == FORBIDDEN
