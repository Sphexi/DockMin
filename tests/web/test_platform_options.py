"""Just-in-time accounts and the password sign-in setting, each under its own configuration."""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI

from app.auth.sessions import SESSION_COOKIE
from tests.auth_support import (
    ADMIN_PASSWORD,
    ADMIN_SESSION_TOKEN,
    ADMIN_USERNAME,
    SEE_OTHER,
    STANDARD_PASSWORD,
    STANDARD_SESSION_TOKEN,
    STANDARD_USERNAME,
    Browsers,
    cookie_set_by,
    csrf_form,
    fetch_scalar,
)
from tests.jwt_support import EMAIL, HEADER, USERNAME, Signer, platform_claims
from tests.support import LOCKOUT_ENV
from tests.web.platform_support import (
    ACCOUNT_PATH,
    JWT_SESSIONS,
    OK,
    SEEDED_SESSIONS,
    UNLINKED_EMAIL,
    events_of,
    session_count,
)

CREATE_USERS = {"DOCKMIN_JWT_CREATE_USERS": "true"}
ADMINISTRATORS_ONLY = {"DOCKMIN_JWT_PASSWORD_SIGN_IN": "administrators"}
UNAUTHORIZED = 401
USER_ROW = "SELECT username, role, external_identity, password_hash FROM user WHERE id > 2"
FAILURES = "SELECT COUNT(*) FROM activity_event WHERE action = 'login' AND result = 'failure'"


@pytest.fixture
def env(platform_env: dict[str, str]) -> dict[str, str]:
    return platform_env | LOCKOUT_ENV


async def _rows(app: FastAPI, sql: str) -> list[tuple[object, ...]]:
    async with app.state.database.session() as db:
        return [tuple(row) for row in await db.fetch_limited(sql, (), limit=10)]


@pytest.mark.parametrize("extra_env", [CREATE_USERS], indirect=True)
async def test_an_unlinked_identity_creates_a_standard_user_and_signs_them_in(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    client = browsers.open()

    response = await client.get(
        ACCOUNT_PATH, headers={HEADER: signer.token(platform_claims(username="new.person"))}
    )

    [(username, role, identity, password_hash)] = await _rows(started_app, USER_ROW)
    [created] = await events_of(started_app, "user_created")
    [login] = await events_of(started_app, "login")
    assert response.status_code == OK
    assert cookie_set_by(response, SESSION_COOKIE).value
    assert (username, role, identity) == ("new.person", "standard", EMAIL)
    assert str(password_hash).startswith("$argon2id$")
    assert created["actor_user_id"] == login["actor_user_id"]
    assert created["detail"]["method"] == "jwt"
    assert created["detail"]["role"] == "standard"
    assert await session_count(started_app, JWT_SESSIONS) == 1


@pytest.mark.parametrize("extra_env", [CREATE_USERS], indirect=True)
async def test_a_created_account_has_no_password_that_signs_in(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await browsers.open().get(ACCOUNT_PATH, headers={HEADER: signer.token()})
    client = browsers.open()
    client.cookies.clear()
    await client.get("/login")

    response = await client.post(
        "/login", data=csrf_form(client, username=USERNAME, password="a-guess-at-the-password-1")
    )

    assert response.status_code == UNAUTHORIZED


@pytest.mark.parametrize(
    ("username", "reason"),
    [
        pytest.param("a b", "jwt_username_invalid", id="fails-the-grammar"),
        pytest.param("ab", "jwt_username_invalid", id="too-short"),
        pytest.param(ADMIN_USERNAME, "jwt_username_taken", id="taken-by-an-administrator"),
    ],
)
@pytest.mark.parametrize("extra_env", [CREATE_USERS], indirect=True)
async def test_a_name_that_is_unusable_or_taken_is_refused_with_a_row_and_creates_nothing(
    started_app: FastAPI, browsers: Browsers, signer: Signer, username: str, reason: str
) -> None:
    token = signer.token(platform_claims(username=username))

    response = await browsers.open().get(ACCOUNT_PATH, headers={HEADER: token})

    [refused] = [e for e in await events_of(started_app, "login") if e["result"] == "failure"]
    assert response.status_code == SEE_OTHER
    assert refused["detail"]["reason"] == reason
    assert await _rows(started_app, USER_ROW) == []
    assert await session_count(started_app) == SEEDED_SESSIONS
    assert await events_of(started_app, "user_created") == []


@pytest.mark.parametrize("extra_env", [CREATE_USERS], indirect=True)
async def test_just_in_time_creation_makes_no_administrator_whatever_the_names_look_like(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    for name in ("administrator", "root-admin"):
        await browsers.open().get(
            ACCOUNT_PATH,
            headers={HEADER: signer.token(platform_claims(username=name, email=f"{name}@x.test"))},
        )

    rows = await _rows(started_app, USER_ROW)
    assert sorted(str(row[1]) for row in rows) == ["standard", "standard"]
    assert (
        await fetch_scalar(started_app, "SELECT COUNT(*) FROM user WHERE role = 'administrator'")
        == 1
    )


@pytest.mark.parametrize("extra_env", [CREATE_USERS], indirect=True)
async def test_an_unlinked_identity_creates_no_account_while_the_instance_is_offline(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    started_app.state.restore_gate.engage("restore-id", datetime.now(UTC), purpose="restore")

    await browsers.open().get(
        ACCOUNT_PATH, headers={HEADER: signer.token(platform_claims(username="new.person"))}
    )

    assert await _rows(started_app, USER_ROW) == []
    assert await events_of(started_app, "user_created") == []


async def test_without_the_option_an_unlinked_identity_creates_no_account(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await browsers.open().get(
        ACCOUNT_PATH, headers={HEADER: signer.token(platform_claims(email=UNLINKED_EMAIL))}
    )

    assert await _rows(started_app, USER_ROW) == []
    assert await fetch_scalar(started_app, FAILURES) == 1


@pytest.mark.parametrize("extra_env", [ADMINISTRATORS_ONLY], indirect=True)
async def test_password_sign_in_for_standard_users_can_be_refused_while_administrators_keep_it(
    started_app: FastAPI, browsers: Browsers
) -> None:
    refused = await _password_login(browsers, STANDARD_USERNAME, STANDARD_PASSWORD)
    admitted = await _password_login(browsers, ADMIN_USERNAME, ADMIN_PASSWORD)

    [failure] = [e for e in await events_of(started_app, "login") if e["result"] == "failure"]
    assert (refused, admitted) == (UNAUTHORIZED, SEE_OTHER)
    assert failure["detail"]["reason"] == "password_sign_in_disabled"
    assert failure["detail"]["method"] == "password"


@pytest.mark.parametrize("extra_env", [ADMINISTRATORS_ONLY], indirect=True)
async def test_restricting_password_sign_in_ends_a_standard_users_existing_password_session(
    started_app: FastAPI, browsers: Browsers
) -> None:
    standard = browsers.open(session_token=STANDARD_SESSION_TOKEN)
    admin = browsers.open(session_token=ADMIN_SESSION_TOKEN)

    refused = await standard.get(ACCOUNT_PATH)
    kept = await admin.get(ACCOUNT_PATH)

    [logout] = await events_of(started_app, "logout")
    assert refused.status_code != OK
    assert kept.status_code == OK
    assert (logout["detail"]["reason"], logout["detail"]["method"]) == (
        "password_sign_in_disabled",
        "password",
    )
    assert await session_count(started_app) == SEEDED_SESSIONS - 1


async def test_by_default_every_account_may_sign_in_with_a_password(
    browsers: Browsers,
) -> None:
    standard = await _password_login(browsers, STANDARD_USERNAME, STANDARD_PASSWORD)
    admin = await _password_login(browsers, ADMIN_USERNAME, ADMIN_PASSWORD)

    assert (standard, admin) == (SEE_OTHER, SEE_OTHER)


async def _password_login(browsers: Browsers, username: str, password: str) -> int:
    client = browsers.open()
    client.cookies.clear()
    await client.get("/login")
    response = await client.post(
        "/login", data=csrf_form(client, username=username, password=password)
    )
    return response.status_code
