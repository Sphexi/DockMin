"""Linking a user to an access platform identity from the user page."""

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.web.csrf import CSRF_FORM_FIELD
from tests.auth_support import (
    ADMIN_ID,
    SEE_OTHER,
    STANDARD_ID,
    csrf_form,
)
from tests.jwt_support import EMAIL
from tests.web.platform_support import OTHER_EMAIL, events_of

OK = 200
UNPROCESSABLE = 422
FORBIDDEN = 403
LONG_IDENTITY = "x" * 321


@pytest.fixture
def env(platform_env: dict[str, str]) -> dict[str, str]:
    return platform_env


def _identity_path(user_id: int) -> str:
    return f"/users/{user_id}/identity"


async def _set(client: AsyncClient, user_id: int, identity: str) -> int:
    response = await client.post(
        _identity_path(user_id), data=csrf_form(client, external_identity=identity)
    )
    return response.status_code


async def _identity(app: FastAPI, user_id: int) -> str | None:
    async with app.state.database.session() as db:
        row = await db.fetch_one("SELECT external_identity FROM user WHERE id = ?", (user_id,))
    assert row is not None
    value: str | None = row[0]
    return value


async def test_an_administrator_links_a_user_and_the_change_is_recorded_with_the_value(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    status = await _set(admin_client, STANDARD_ID, f"  {EMAIL}  ")

    [event] = await events_of(started_app, "user_updated")
    assert (status, await _identity(started_app, STANDARD_ID)) == (SEE_OTHER, EMAIL)
    assert (event["actor_user_id"], event["target_id"]) == (ADMIN_ID, str(STANDARD_ID))
    assert event["detail"]["external_identity_set"] == EMAIL


async def test_changing_and_clearing_the_link_are_recorded(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    await _set(admin_client, STANDARD_ID, EMAIL)
    await _set(admin_client, STANDARD_ID, OTHER_EMAIL)
    await _set(admin_client, STANDARD_ID, "")

    events = await events_of(started_app, "user_updated")
    assert [sorted(event["detail"]) for event in events] == [
        ["address", "external_identity_set"],
        ["address", "external_identity_set", "previous_external_identity"],
        ["address", "external_identity_cleared"],
    ]
    assert events[2]["detail"]["external_identity_cleared"] == OTHER_EMAIL
    assert await _identity(started_app, STANDARD_ID) is None


async def test_saving_the_value_a_user_already_has_records_nothing(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    await _set(admin_client, STANDARD_ID, EMAIL)

    status = await _set(admin_client, STANDARD_ID, EMAIL)

    assert status == SEE_OTHER
    assert len(await events_of(started_app, "user_updated")) == 1


async def test_an_identity_another_account_holds_is_refused_ignoring_case(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    await _set(admin_client, STANDARD_ID, EMAIL)

    response = await admin_client.post(
        _identity_path(ADMIN_ID), data=csrf_form(admin_client, external_identity=EMAIL.upper())
    )

    assert response.status_code == UNPROCESSABLE
    assert "linked to another account" in response.text
    assert await _identity(started_app, ADMIN_ID) is None


@pytest.mark.parametrize("identity", [LONG_IDENTITY, "a\x07b"])
async def test_an_identity_that_is_too_long_or_holds_a_control_character_is_refused(
    started_app: FastAPI, admin_client: AsyncClient, identity: str
) -> None:
    status = await _set(admin_client, STANDARD_ID, identity)

    assert (status, await _identity(started_app, STANDARD_ID)) == (UNPROCESSABLE, None)


async def test_a_standard_user_cannot_link_anyone(
    started_app: FastAPI, standard_client: AsyncClient
) -> None:
    status = await _set(standard_client, STANDARD_ID, EMAIL)

    assert (status, await _identity(started_app, STANDARD_ID)) == (FORBIDDEN, None)


async def test_the_user_page_shows_the_field_and_that_the_account_signs_in_through_the_platform(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    unlinked = await admin_client.get(f"/users/{STANDARD_ID}")
    await _set(admin_client, STANDARD_ID, EMAIL)

    linked = await admin_client.get(f"/users/{STANDARD_ID}")

    assert unlinked.status_code == linked.status_code == OK
    assert 'name="external_identity"' in unlinked.text
    assert "Signs in through the access platform" not in unlinked.text
    assert "Signs in through the access platform" in linked.text
    assert f'value="{EMAIL}"' in linked.text
    assert CSRF_FORM_FIELD in linked.text
