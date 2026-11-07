"""Tests that no password hash or plaintext password reaches a response or a log line."""

import io

from fastapi import FastAPI
from httpx import AsyncClient, Response

from tests.auth_support import (
    ADMIN_ID,
    ADMIN_PASSWORD,
    NEW_PASSWORD,
    STANDARD_ID,
    STANDARD_PASSWORD,
    STANDARD_USERNAME,
    csrf_headers,
)

WRONG_PASSWORD = "not-the-password-anyone-has"
CREATED_USERNAME = "created-user"
CREATED_PASSWORD = "created-password-1234"
RESET_PASSWORD = "reset-password-1234"
ARGON2_MARKER = "$argon2"
PAGES = [
    "/",
    "/account",
    "/users",
    "/users/new",
    f"/users/{ADMIN_ID}",
    f"/users/{STANDARD_ID}",
    "/ui-kit",
    "/api/settings/public",
]
PLAINTEXTS = (
    ADMIN_PASSWORD,
    STANDARD_PASSWORD,
    NEW_PASSWORD,
    CREATED_PASSWORD,
    RESET_PASSWORD,
    WRONG_PASSWORD,
)


async def _stored_hashes(app: FastAPI) -> list[str]:
    async with app.state.database.session() as db:
        rows = await db.fetch_limited("SELECT password_hash FROM user", (), limit=100)
    return [row["password_hash"] for row in rows]


async def _drive(admin: AsyncClient, anonymous: AsyncClient) -> list[Response]:
    headers = csrf_headers(admin)
    responses = [await admin.get(page) for page in PAGES]
    responses += [
        await admin.post(
            "/users",
            data={"username": CREATED_USERNAME, "password": CREATED_PASSWORD, "role": "standard"},
            headers=headers,
        ),
        await admin.post(
            f"/users/{STANDARD_ID}/password", data={"new_password": RESET_PASSWORD}, headers=headers
        ),
        await admin.post(
            "/account/password",
            data={
                "current_password": ADMIN_PASSWORD,
                "new_password": NEW_PASSWORD,
                "new_password_confirm": NEW_PASSWORD,
            },
            headers=headers,
        ),
        await anonymous.post(
            "/login",
            data={"username": STANDARD_USERNAME, "password": WRONG_PASSWORD},
            headers=csrf_headers(anonymous),
        ),
        await anonymous.get("/login"),
    ]
    return responses


async def test_no_response_or_log_line_contains_a_password_hash_or_a_plaintext_password(
    started_app: FastAPI,
    admin_client: AsyncClient,
    anonymous_client: AsyncClient,
    log_stream: io.StringIO,
) -> None:
    hashes = await _stored_hashes(started_app)

    responses = await _drive(admin_client, anonymous_client)

    hashes += await _stored_hashes(started_app)
    emitted = log_stream.getvalue() + "".join(f"{r.headers}{r.text}" for r in responses)
    assert len(set(hashes)) > 2
    assert [value for value in hashes if value in emitted] == []
    assert ARGON2_MARKER not in emitted
    assert [value for value in PLAINTEXTS if value in emitted] == []
