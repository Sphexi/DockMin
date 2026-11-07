"""Helpers for the access platform sign-in page tests: linking, signing in and reading rows."""

import json
from collections.abc import AsyncIterator, MutableMapping
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient, Response
from starlette.websockets import WebSocket

from tests.auth_support import (
    ADMIN_ID,
    CLIENT_ADDRESS,
    STANDARD_ID,
    Browsers,
    auth_events,
    fetch_scalar,
)
from tests.jwt_support import EMAIL, HEADER, Signer

OTHER_EMAIL = "sam@example.test"
UNLINKED_EMAIL = "stranger@example.test"
ACCOUNT_PATH = "/account"
OK = 200
SEE_OTHER = 303
SEEDED_SESSIONS = 2
ALL_SESSIONS = "SELECT COUNT(*) FROM session"
JWT_SESSIONS = "SELECT COUNT(*) FROM session WHERE sign_in_method = 'jwt'"
WEBSOCKET_PATH = "/environments/1/containers/c/terminal/ws"


async def link(app: FastAPI, user_id: int, identity: str) -> None:
    async with app.state.database.session() as db:
        await db.execute("UPDATE user SET external_identity = ? WHERE id = ?", (identity, user_id))


async def link_standard_and_admin(app: FastAPI) -> None:
    await link(app, STANDARD_ID, EMAIL)
    await link(app, ADMIN_ID, OTHER_EMAIL)


async def signed_in(browsers: Browsers, signer: Signer) -> tuple[AsyncClient, Response]:
    """Open a client and make the first request that exchanges the standard user's token."""
    client = browsers.open()
    client.cookies.clear()
    response = await client.get(ACCOUNT_PATH, headers={HEADER: signer.token()})
    assert response.status_code == OK, response.text
    return client, response


async def events_of(app: FastAPI, action: str) -> list[dict[str, Any]]:
    return [event for event in await auth_events(app) if event["action"] == action]


async def session_count(app: FastAPI, sql: str = ALL_SESSIONS) -> int:
    return int(await fetch_scalar(app, sql))


async def everything_recorded(app: FastAPI) -> str:
    """Every activity row, as one string, for searching."""
    return json.dumps(await auth_events(app))


def handshake(
    app: FastAPI, cookies: dict[str, str], headers: dict[str, str] | None = None
) -> WebSocket:
    """Build a WebSocket connection as the server sees one when a handshake arrives."""
    raw = {"host": "testserver", "origin": "https://testserver", **(headers or {})}
    if cookies:
        raw["cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        return None

    scope: dict[str, Any] = {
        "type": "websocket",
        "scheme": "wss",
        "path": WEBSOCKET_PATH,
        "query_string": b"",
        "headers": [(name.lower().encode(), value.encode()) for name, value in raw.items()],
        "server": ("testserver", 443),
        "client": (CLIENT_ADDRESS, 5000),
        "app": app,
        "state": {},
    }
    return WebSocket(scope, receive, send)


async def drain(clients: AsyncIterator[AsyncClient]) -> None:
    async for client in clients:
        await client.aclose()
