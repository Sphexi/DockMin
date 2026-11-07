"""Sign-in from an access platform: exchange on safe requests, binding, and sign-out."""

import io
from collections.abc import Callable

import pytest
from fastapi import FastAPI

from app.auth.dependencies import resolve_principal
from app.auth.jwt_identity import Assertion
from app.auth.sessions import SESSION_COOKIE, SIGNED_OUT_COOKIE, hash_token
from app.web.csrf import CSRF_COOKIE
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_SESSION_TOKEN,
    ADMIN_USERNAME,
    CLIENT_ADDRESS,
    SEEDED_CSRF_TOKEN,
    STANDARD_ID,
    STANDARD_SESSION_TOKEN,
    STANDARD_USERNAME,
    Browsers,
    cookie_set_by,
    csrf_form,
    csrf_headers,
    fetch_scalar,
)
from tests.jwt_support import EMAIL, HEADER, Signer, ec_signer, platform_claims
from tests.web.platform_support import (
    ACCOUNT_PATH,
    JWT_SESSIONS,
    OK,
    OTHER_EMAIL,
    SEE_OTHER,
    SEEDED_SESSIONS,
    UNLINKED_EMAIL,
    events_of,
    everything_recorded,
    handshake,
    link,
    link_standard_and_admin,
    session_count,
    signed_in,
)

ENDED = "platform_assertion_ended"
CHANGED = "platform_identity_changed"
FORBIDDEN = 403
UNAUTHORIZED = 401
EXPIRED_SECONDS = 600


@pytest.fixture
def env(platform_env: dict[str, str]) -> dict[str, str]:
    return platform_env


def _login_destination(path: str = ACCOUNT_PATH) -> str:
    return f"/login?next={path.replace('/', '%2F')}"


async def test_a_safe_request_with_a_valid_token_for_a_linked_account_signs_in(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)

    client, response = await signed_in(browsers, signer)

    session = cookie_set_by(response, SESSION_COOKIE)
    [login] = await events_of(started_app, "login")
    assert STANDARD_USERNAME in response.text
    assert (session["httponly"], session["secure"]) == (True, True)
    assert session["samesite"].lower() == "lax"
    assert (login["actor_user_id"], login["result"]) == (STANDARD_ID, "success")
    assert login["detail"] == {"address": CLIENT_ADDRESS, "method": "jwt"}
    assert (
        await fetch_scalar(
            started_app,
            "SELECT sign_in_method FROM session WHERE token_hash = ?",
            (hash_token(session.value),),
        )
        == "jwt"
    )
    assert cookie_set_by(response, CSRF_COOKIE).value != SEEDED_CSRF_TOKEN
    again = await client.get(ACCOUNT_PATH, headers={HEADER: signer.token()})
    assert (again.status_code, "set-cookie" in again.headers) == (OK, False)


async def test_an_unsafe_request_with_a_valid_token_does_not_sign_in(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)
    client = browsers.open()

    response = await client.post(
        ACCOUNT_PATH,
        data=csrf_form(client, username="renamed-user"),
        headers={HEADER: signer.token()},
    )

    assert (response.status_code, response.headers["location"]) == (SEE_OTHER, "/login")
    assert await session_count(started_app) == SEEDED_SESSIONS
    assert await events_of(started_app, "login") == []


@pytest.mark.parametrize(
    "present",
    [
        pytest.param(lambda s: None, id="no-header"),
        pytest.param(lambda s: s.token(platform_claims(issued_ago=5) | {"exp": 1}), id="expired"),
        pytest.param(lambda s: ec_signer().token(), id="signed-by-another-key"),
        pytest.param(lambda s: "garbage", id="not-a-token"),
    ],
)
async def test_a_platform_session_ends_when_the_token_is_absent_or_fails(
    started_app: FastAPI,
    browsers: Browsers,
    signer: Signer,
    present: Callable[[Signer], str | None],
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)
    client, _ = await signed_in(browsers, signer)
    token = present(signer)
    headers = {} if token is None else {HEADER: token}

    response = await client.get(ACCOUNT_PATH, headers=headers)

    [logout] = await events_of(started_app, "logout")
    cleared = cookie_set_by(response, SESSION_COOKIE)
    assert (response.status_code, response.headers["location"]) == (
        SEE_OTHER,
        _login_destination(),
    )
    assert (logout["actor_user_id"], logout["detail"]["reason"]) == (STANDARD_ID, ENDED)
    assert logout["detail"]["method"] == "jwt"
    assert (cleared.value, cleared["max-age"]) == ("", "0")
    assert await session_count(started_app, JWT_SESSIONS) == 0


async def test_another_linked_identitys_token_ends_the_session_and_signs_that_user_in(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await link_standard_and_admin(started_app)
    client, first = await signed_in(browsers, signer)

    response = await client.get(
        ACCOUNT_PATH, headers={HEADER: signer.token(platform_claims(email=OTHER_EMAIL))}
    )

    [logout] = await events_of(started_app, "logout")
    logins = await events_of(started_app, "login")
    replaced = cookie_set_by(response, SESSION_COOKIE)
    assert ADMIN_USERNAME in response.text
    assert logout["detail"]["reason"] == CHANGED
    assert [login["actor_user_id"] for login in logins] == [STANDARD_ID, ADMIN_ID]
    assert replaced.value != cookie_set_by(first, SESSION_COOKIE).value
    assert await session_count(started_app, JWT_SESSIONS) == 1


async def test_an_unlinked_identitys_token_ends_the_session_and_leaves_the_caller_signed_out(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)
    client, _ = await signed_in(browsers, signer)

    response = await client.get(
        ACCOUNT_PATH, headers={HEADER: signer.token(platform_claims(email=UNLINKED_EMAIL))}
    )

    [logout] = await events_of(started_app, "logout")
    [refused] = [e for e in await events_of(started_app, "login") if e["result"] == "failure"]
    assert response.status_code == SEE_OTHER
    assert logout["detail"]["reason"] == CHANGED
    assert refused["detail"]["reason"] == "jwt_unlinked"


async def test_clearing_the_link_ends_the_users_platform_session_on_its_next_request(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)
    client, _ = await signed_in(browsers, signer)
    async with started_app.state.database.session() as db:
        await db.execute("UPDATE user SET external_identity = NULL WHERE id = ?", (STANDARD_ID,))

    response = await client.get(ACCOUNT_PATH, headers={HEADER: signer.token()})

    [logout] = await events_of(started_app, "logout")
    assert response.status_code == SEE_OTHER
    assert logout["detail"]["reason"] == CHANGED


async def test_a_password_session_ignores_the_platform_header(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await link_standard_and_admin(started_app)
    client = browsers.open(session_token=ADMIN_SESSION_TOKEN)

    plain = await client.get(ACCOUNT_PATH)
    other = await client.get(ACCOUNT_PATH, headers={HEADER: signer.token()})
    garbage = await client.get(ACCOUNT_PATH, headers={HEADER: "garbage"})

    assert [plain.status_code, other.status_code, garbage.status_code] == [OK, OK, OK]
    assert ADMIN_USERNAME in other.text
    assert await session_count(started_app) == SEEDED_SESSIONS
    assert await events_of(started_app, "logout") == []


async def test_a_websocket_handshake_on_a_platform_session_is_bound_to_the_token(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await link_standard_and_admin(started_app)
    _, response = await signed_in(browsers, signer)
    cookie = {SESSION_COOKIE: cookie_set_by(response, SESSION_COOKIE).value}

    bound = handshake(started_app, cookie, {HEADER: signer.token()})
    await resolve_principal(bound)
    other = handshake(
        started_app, cookie, {HEADER: signer.token(platform_claims(email=OTHER_EMAIL))}
    )
    await resolve_principal(other)

    assert bound.state.principal.username == STANDARD_USERNAME
    assert other.state.principal is None
    [logout] = await events_of(started_app, "logout")
    assert logout["detail"]["reason"] == CHANGED
    assert await session_count(started_app, JWT_SESSIONS) == 0


async def test_a_websocket_handshake_without_the_header_ends_the_platform_session(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)
    _, response = await signed_in(browsers, signer)
    cookie = {SESSION_COOKIE: cookie_set_by(response, SESSION_COOKIE).value}

    connection = handshake(started_app, cookie)
    await resolve_principal(connection)

    [logout] = await events_of(started_app, "logout")
    assert connection.state.principal is None
    assert logout["detail"]["reason"] == ENDED


async def test_a_websocket_handshake_never_opens_a_session_from_the_header_alone(
    started_app: FastAPI, signer: Signer
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)

    connection = handshake(started_app, {}, {HEADER: signer.token()})
    await resolve_principal(connection)

    assert connection.state.principal is None
    assert await session_count(started_app) == SEEDED_SESSIONS


async def test_signing_out_of_a_platform_session_sets_a_marker_and_stops_exchange(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)
    client, _ = await signed_in(browsers, signer)

    response = await client.post("/logout", headers=csrf_headers(client) | {HEADER: signer.token()})
    after = await client.get(ACCOUNT_PATH, headers={HEADER: signer.token()})

    marker = cookie_set_by(response, SIGNED_OUT_COOKIE)
    assert (marker.value, marker["httponly"], marker["secure"]) == ("1", True, True)
    assert marker["samesite"].lower() == "lax"
    assert not marker["max-age"] and not marker["expires"]
    assert (after.status_code, after.headers["location"]) == (SEE_OTHER, _login_destination())
    assert await session_count(started_app, JWT_SESSIONS) == 0
    assert len(await events_of(started_app, "login")) == 1


async def test_signing_out_of_a_password_session_sets_no_marker(
    browsers: Browsers,
) -> None:
    client = await browsers.sign_in()

    response = await client.post("/logout", headers=csrf_headers(client))

    assert SIGNED_OUT_COOKIE not in response.headers.get_list("set-cookie").__str__()


async def test_continue_as_requires_a_csrf_token_and_then_signs_in_and_clears_the_marker(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)
    client, _ = await signed_in(browsers, signer)
    await client.post("/logout", headers=csrf_headers(client) | {HEADER: signer.token()})
    page = await client.get("/login", headers={HEADER: signer.token()})

    refused = await client.post("/login/continue", headers={HEADER: signer.token()})
    accepted = await client.post(
        "/login/continue", data=csrf_form(client, next="/"), headers={HEADER: signer.token()}
    )

    assert f"Continue as {STANDARD_USERNAME}" in page.text
    assert refused.status_code == FORBIDDEN
    assert (accepted.status_code, accepted.headers["location"]) == (SEE_OTHER, "/")
    assert cookie_set_by(accepted, SESSION_COOKIE).value
    assert cookie_set_by(accepted, SIGNED_OUT_COOKIE)["max-age"] == "0"
    assert await session_count(started_app, JWT_SESSIONS) == 1
    followed = await client.get(ACCOUNT_PATH, headers={HEADER: signer.token()})
    assert followed.status_code == OK


async def test_continue_as_ends_the_session_the_caller_still_holds(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)
    client = browsers.open(session_token=STANDARD_SESSION_TOKEN)

    accepted = await client.post(
        "/login/continue", data=csrf_form(client, next="/"), headers={HEADER: signer.token()}
    )

    [logout] = await events_of(started_app, "logout")
    assert accepted.status_code == SEE_OTHER
    assert (logout["actor_user_id"], logout["detail"]["reason"]) == (
        STANDARD_ID,
        "session_replaced",
    )
    assert await session_count(started_app) == SEEDED_SESSIONS
    assert await session_count(started_app, JWT_SESSIONS) == 1


async def test_an_unexpected_verifier_error_is_logged_with_its_traceback_and_signs_nobody_in(
    started_app: FastAPI, browsers: Browsers, signer: Signer, log_stream: io.StringIO
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)

    async def broken(value: str) -> Assertion:
        raise RuntimeError("keystore exploded")

    started_app.state.auth.platform._verifier.verify = broken
    token = signer.token()

    response = await browsers.open().get(ACCOUNT_PATH, headers={HEADER: token})

    logged = log_stream.getvalue()
    assert response.status_code == SEE_OTHER
    assert '"message": "jwt_verifier_failed"' in logged
    assert "RuntimeError" in logged
    assert token not in logged


async def test_the_login_page_offers_continue_as_only_for_a_linked_enabled_account(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)
    marker = {SIGNED_OUT_COOKIE: "1"}
    unlinked = signer.token(platform_claims(email=UNLINKED_EMAIL))

    async def page(token: str | None) -> str:
        client = browsers.open()
        client.cookies.update(marker)
        headers = {} if token is None else {HEADER: token}
        return (await client.get("/login", headers=headers)).text

    linked_page = await page(signer.token())
    unlinked_page = await page(unlinked)
    bare_page = await page(None)

    assert "Continue as" in linked_page
    assert "Continue as" not in unlinked_page
    assert "Continue as" not in bare_page


async def test_a_continue_as_post_without_a_verifiable_token_signs_nobody_in(
    started_app: FastAPI, browsers: Browsers
) -> None:
    client = browsers.open()

    response = await client.post(
        "/login/continue", data=csrf_form(client, next="/"), headers={HEADER: "garbage"}
    )

    assert (response.status_code, await session_count(started_app)) == (
        UNAUTHORIZED,
        SEEDED_SESSIONS,
    )


async def test_an_unlinked_identity_is_refused_once_per_token_not_once_per_request(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    client = browsers.open()
    first = signer.token(platform_claims(email=UNLINKED_EMAIL))
    second = signer.token(platform_claims(email=UNLINKED_EMAIL))

    for token in (first, first, first, second):
        await client.get(ACCOUNT_PATH, headers={HEADER: token})

    failures = [e for e in await events_of(started_app, "login") if e["result"] == "failure"]
    assert [failure["detail"]["reason"] for failure in failures] == ["jwt_unlinked"] * 2
    assert failures[0]["detail"]["method"] == "jwt"
    assert await session_count(started_app) == SEEDED_SESSIONS


async def test_a_disabled_linked_account_is_refused_and_recorded(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)
    async with started_app.state.database.session() as db:
        await db.execute("UPDATE user SET is_enabled = 0 WHERE id = ?", (STANDARD_ID,))

    response = await browsers.open().get(ACCOUNT_PATH, headers={HEADER: signer.token()})

    [refused] = [e for e in await events_of(started_app, "login") if e["result"] == "failure"]
    assert response.status_code == SEE_OTHER
    assert (refused["target_id"], refused["detail"]["reason"]) == (
        str(STANDARD_ID),
        "jwt_account_disabled",
    )


async def test_the_api_ignores_the_platform_header_and_refuses_a_jwt_as_a_token(
    started_app: FastAPI, browsers: Browsers, signer: Signer
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)
    client = browsers.open()
    token = signer.token()

    by_header = await client.get("/api/v1/environments", headers={HEADER: token})
    as_bearer = await client.get(
        "/api/v1/environments", headers={"Authorization": f"Bearer {token}"}
    )
    public = await client.get("/api/settings/public")

    assert (by_header.status_code, as_bearer.status_code) == (UNAUTHORIZED, UNAUTHORIZED)
    assert await session_count(started_app) == SEEDED_SESSIONS
    assert await events_of(started_app, "login") == []
    assert public.json()["authentication_methods"] == ["internal", "jwt"]


async def test_api_tokens_work_unchanged_whatever_the_platform_header_carries(
    started_app: FastAPI, browsers: Browsers
) -> None:
    from tests.web.environment_support import bearer, issue_token

    admin = browsers.open(session_token=ADMIN_SESSION_TOKEN)
    token = await issue_token(admin)

    response = await browsers.open().get(
        "/api/v1/environments", headers=bearer(token) | {HEADER: "garbage"}
    )

    assert response.status_code == OK


async def test_no_token_value_reaches_a_log_line_a_row_or_a_page(
    started_app: FastAPI, browsers: Browsers, signer: Signer, log_stream: io.StringIO
) -> None:
    await link(started_app, STANDARD_ID, EMAIL)
    good = signer.token()
    unlinked = signer.token(platform_claims(email=UNLINKED_EMAIL))
    tampered = good.rsplit(".", 1)[0] + ".AAAA"
    client = browsers.open()
    pages = [
        (await client.get(ACCOUNT_PATH, headers={HEADER: token})).text
        for token in (good, unlinked, tampered, f"Bearer {good}", f"{good}.extra")
    ]
    pages.append((await client.get("/login", headers={HEADER: unlinked})).text)

    searchable = "\n".join([log_stream.getvalue(), await everything_recorded(started_app), *pages])
    fragments = [part for token in (good, unlinked, tampered) for part in token.split(".")]
    assert [fragment for fragment in fragments if fragment in searchable] == []
    assert '"message": "jwt_refused"' in searchable
    assert '"message": "login_succeeded"' in searchable
