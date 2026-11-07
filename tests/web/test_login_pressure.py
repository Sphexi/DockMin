"""Tests for sign-in under load: concurrent guesses and a saturated hashing queue."""

import asyncio
from collections import Counter

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.auth.passwords import HashingBusy
from app.auth.principal import Credentials, Principal
from tests.auth_support import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    SEE_OTHER,
    Browsers,
    auth_events,
    csrf_form,
    fetch_scalar,
)
from tests.support import LOCKOUT_ENV, MAX_LOGIN_FAILURES

UNAUTHORIZED = 401
TOO_MANY_REQUESTS = 429
SERVICE_UNAVAILABLE = 503
WRONG_PASSWORD = "not-the-password"
BURST = MAX_LOGIN_FAILURES * 7
BUSY_PROVIDER = "busy"
USERNAME_FAILURES = "SELECT failure_count FROM login_throttle WHERE kind = 'username'"


class _SaturatedProvider:
    async def authenticate(self, credentials: Credentials) -> Principal:
        raise HashingBusy


@pytest.fixture
def env(env: dict[str, str]) -> dict[str, str]:
    return env | LOCKOUT_ENV


async def _submit(client: AsyncClient, password: str, provider: str = "internal") -> Response:
    return await client.post(
        "/login",
        data=csrf_form(client, username=ADMIN_USERNAME, password=password, provider=provider),
    )


async def test_a_burst_of_wrong_logins_is_refused_past_the_limit_and_every_attempt_is_recorded(
    started_app: FastAPI, browsers: Browsers
) -> None:
    client = browsers.open()

    responses = await asyncio.gather(*(_submit(client, WRONG_PASSWORD) for _ in range(BURST)))

    events = await auth_events(started_app)
    assert Counter(response.status_code for response in responses) == {
        UNAUTHORIZED: MAX_LOGIN_FAILURES,
        TOO_MANY_REQUESTS: BURST - MAX_LOGIN_FAILURES,
    }
    assert Counter(event["detail"]["reason"] for event in events) == {
        "bad_password": MAX_LOGIN_FAILURES,
        "locked_out": BURST - MAX_LOGIN_FAILURES,
    }


async def test_the_correct_password_is_refused_once_a_burst_has_locked_the_account(
    browsers: Browsers,
) -> None:
    client = browsers.open()
    await asyncio.gather(*(_submit(client, WRONG_PASSWORD) for _ in range(BURST)))

    response = await _submit(client, ADMIN_PASSWORD)

    assert response.status_code == TOO_MANY_REQUESTS


async def test_a_saturated_hashing_queue_answers_503_refunds_the_attempt_and_records_it(
    started_app: FastAPI, browsers: Browsers
) -> None:
    started_app.state.auth.registry.register(BUSY_PROVIDER, lambda _context: _SaturatedProvider())
    client = browsers.open()

    refused = await asyncio.gather(
        *(_submit(client, WRONG_PASSWORD, BUSY_PROVIDER) for _ in range(MAX_LOGIN_FAILURES + 2))
    )

    events = await auth_events(started_app)
    assert {response.status_code for response in refused} == {SERVICE_UNAVAILABLE}
    assert {response.headers["retry-after"] for response in refused} == {"1"}
    assert [event["detail"]["reason"] for event in events] == ["server_busy"] * len(refused)
    assert await fetch_scalar(started_app, USERNAME_FAILURES) == 0


async def test_a_correct_login_succeeds_straight_after_the_queue_refused_a_burst(
    started_app: FastAPI, browsers: Browsers
) -> None:
    started_app.state.auth.registry.register(BUSY_PROVIDER, lambda _context: _SaturatedProvider())
    client = browsers.open()
    await asyncio.gather(
        *(_submit(client, WRONG_PASSWORD, BUSY_PROVIDER) for _ in range(MAX_LOGIN_FAILURES + 2))
    )

    response = await _submit(client, ADMIN_PASSWORD)

    assert response.status_code == SEE_OTHER
