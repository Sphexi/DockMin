"""Tests for how the session and CSRF cookies are named and flagged under each configuration."""

import pytest
from fastapi import FastAPI

from app.auth.sessions import INSECURE_SESSION_COOKIE, SESSION_COOKIE, session_cookie_name
from app.config import Settings, load_settings
from app.web.csrf import CSRF_COOKIE, INSECURE_CSRF_COOKIE, csrf_cookie_name
from tests.auth_support import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    SEE_OTHER,
    Browsers,
    cookie_set_by,
    csrf_form,
)
from tests.support import INSECURE_COOKIE_ENV

PLAIN_HTTP_URL = "http://testserver"
HOST_PREFIX = "__Host-"
OK = 200


@pytest.mark.parametrize(
    ("overrides", "session_name", "csrf_name"),
    [
        pytest.param({}, SESSION_COOKIE, CSRF_COOKIE, id="secure-by-default"),
        pytest.param(
            INSECURE_COOKIE_ENV, INSECURE_SESSION_COOKIE, INSECURE_CSRF_COOKIE, id="secure-off"
        ),
    ],
)
def test_cookie_names_follow_the_secure_setting(
    env: dict[str, str], overrides: dict[str, str], session_name: str, csrf_name: str
) -> None:
    settings = load_settings(env | overrides)

    assert session_cookie_name(settings) == session_name
    assert csrf_cookie_name(settings) == csrf_name


def test_the_host_prefix_is_carried_by_the_secure_names_and_by_no_other() -> None:
    names = {SESSION_COOKIE, CSRF_COOKIE, INSECURE_SESSION_COOKIE, INSECURE_CSRF_COOKIE}

    assert {name for name in names if name.startswith(HOST_PREFIX)} == {SESSION_COOKIE, CSRF_COOKIE}


async def test_both_cookies_are_secure_by_configuration_even_when_the_request_arrives_over_http(
    browsers: Browsers,
) -> None:
    client = browsers.open(base_url=PLAIN_HTTP_URL)

    response = await client.post(
        "/login", data=csrf_form(client, username=ADMIN_USERNAME, password=ADMIN_PASSWORD)
    )

    assert response.status_code == SEE_OTHER
    assert cookie_set_by(response, SESSION_COOKIE)["secure"] is True
    assert cookie_set_by(response, CSRF_COOKIE)["secure"] is True


class TestWithSecureCookiesOff:
    @pytest.fixture
    def env(self, env: dict[str, str]) -> dict[str, str]:
        return env | INSECURE_COOKIE_ENV

    async def test_login_sets_unprefixed_cookies_without_the_secure_attribute_over_https(
        self, started_app: FastAPI, browsers: Browsers
    ) -> None:
        settings: Settings = started_app.state.settings
        client = browsers.open()

        response = await client.post(
            "/login", data=csrf_form(client, username=ADMIN_USERNAME, password=ADMIN_PASSWORD)
        )

        session = cookie_set_by(response, session_cookie_name(settings))
        csrf = cookie_set_by(response, csrf_cookie_name(settings))
        assert response.status_code == SEE_OTHER
        assert not session["secure"]
        assert not csrf["secure"]
        assert (await client.get("/account")).status_code == OK
