"""Accounts, a controllable clock, and a signed-in HTTP client for the authentication tests."""

import asyncio
import json
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from http.cookies import Morsel, SimpleCookie
from pathlib import Path
from typing import Any, overload

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from app.auth.passwords import PasswordHasher
from app.auth.sessions import hash_token, session_cookie_name
from app.db.engine import Database, Session
from app.db.repositories.login_throttle import LoginThrottleRepository, ThrottleKind
from app.db.repositories.user import UserRepository
from app.db.repositories.user_session import UserSessionRepository
from app.web.csrf import CSRF_FORM_FIELD, CSRF_HEADER, csrf_cookie_name
from tests.support import CHEAP_ARGON2, MAX_LOGIN_FAILURES

ADMIN_USERNAME = "admin-user"
ADMIN_PASSWORD = "admin-password-1234"
STANDARD_USERNAME = "standard-user"
STANDARD_PASSWORD = "standard-password-1234"
ADMIN_ID = 1
STANDARD_ID = 2
NEW_PASSWORD = "replacement-password-1234"
WEAK_PASSWORD = "short"

CLIENT_ADDRESS = "198.51.100.7"
OTHER_ADDRESS = "203.0.113.9"
BASE_URL = "https://testserver"

SEE_OTHER = 303
LOCKOUT_WINDOW = timedelta(minutes=15)
CLOCK_START = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)

# Seeded sessions are stamped far in the future so they stay valid however long a run takes.
SEEDED_SESSION_STAMP = datetime(2099, 1, 1, tzinfo=UTC)
SEEDED_SESSION_LIFETIME = timedelta(days=1)
ADMIN_SESSION_TOKEN = "A" * 43
STANDARD_SESSION_TOKEN = "B" * 43
SEEDED_CSRF_TOKEN = "C" * 43


class FakeClock:
    """A clock that moves only when told to."""

    def __init__(self, start: datetime = CLOCK_START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        """Move time forward."""
        self.now += delta


async def _seed(path: Path, *, with_sessions: bool) -> None:
    hasher = PasswordHasher(CHEAP_ARGON2)
    async with Database(path).session() as session:
        users = UserRepository(session)
        await users.create(ADMIN_USERNAME, await hasher.hash(ADMIN_PASSWORD), "administrator")
        await users.create(STANDARD_USERNAME, await hasher.hash(STANDARD_PASSWORD), "standard")
        if not with_sessions:
            return
        sessions = UserSessionRepository(session, clock=lambda: SEEDED_SESSION_STAMP)
        for user_id, token in (
            (ADMIN_ID, ADMIN_SESSION_TOKEN),
            (STANDARD_ID, STANDARD_SESSION_TOKEN),
        ):
            await sessions.create(
                user_id,
                hash_token(token),
                lifetime=SEEDED_SESSION_LIFETIME,
                user_agent=None,
                remote_address=None,
            )


def provision(migrated: Path, destination: Path, *, with_sessions: bool = True) -> None:
    """Copy a migrated database to destination and add an administrator and a standard user.

    Each user also holds a live session unless with_sessions is false.
    """
    shutil.copyfile(migrated, destination)
    asyncio.run(_seed(destination, with_sessions=with_sessions))


def cookie_set_by(response: Response, name: str) -> Morsel[str]:
    """Return the Set-Cookie directive a response carries for one cookie name."""
    jar: SimpleCookie = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        jar.load(header)
    return jar[name]


def csrf_token_of(client: AsyncClient) -> str:
    """Return the CSRF token the client holds, whichever cookie name the configuration uses."""
    return next(value for name, value in client.cookies.items() if name.endswith("dockmin_csrf"))


def csrf_headers(client: AsyncClient) -> dict[str, str]:
    """Return the header that carries the client's current CSRF token."""
    return {CSRF_HEADER: csrf_token_of(client)}


def cookie_header(client: AsyncClient, **overrides: str) -> dict[str, str]:
    """Return a Cookie header holding the client's cookies with some replaced or added."""
    merged = {**dict(client.cookies), **overrides}
    return {"Cookie": "; ".join(f"{name}={value}" for name, value in merged.items())}


@overload
def csrf_form(client: AsyncClient, **fields: str) -> dict[str, str]: ...


@overload
def csrf_form(client: AsyncClient, **fields: str | Sequence[str]) -> dict[str, Any]: ...


def csrf_form(client: AsyncClient, **fields: str | Sequence[str]) -> dict[str, Any]:
    """Return form data carrying the client's current CSRF token."""
    return {CSRF_FORM_FIELD: csrf_token_of(client), **fields}


class Browsers:
    """Opens HTTP clients bound to one application, each with its own cookie jar."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._opened: list[AsyncClient] = []

    def open(
        self,
        address: str = CLIENT_ADDRESS,
        *,
        session_token: str | None = None,
        origin: str | None = BASE_URL,
        base_url: str = BASE_URL,
    ) -> AsyncClient:
        """Return a client whose requests come from address, optionally holding a seeded session.

        Every request carries origin as its Origin header, as a browser's would; None sends none.
        """
        settings = self._app.state.settings
        cookies = {csrf_cookie_name(settings): SEEDED_CSRF_TOKEN}
        if session_token is not None:
            cookies[session_cookie_name(settings)] = session_token
        headers = {} if origin is None else {"Origin": origin}
        transport = ASGITransport(app=self._app, raise_app_exceptions=False, client=(address, 5000))
        client = AsyncClient(
            transport=transport, base_url=base_url, cookies=cookies, headers=headers
        )
        self._opened.append(client)
        return client

    async def sign_in(
        self,
        username: str = ADMIN_USERNAME,
        password: str = ADMIN_PASSWORD,
        address: str = CLIENT_ADDRESS,
    ) -> AsyncClient:
        """Return a client holding a live session for the user."""
        client = self.open(address)
        client.cookies.clear()
        await client.get("/login")
        response = await client.post(
            "/login", data=csrf_form(client, username=username, password=password)
        )
        assert response.status_code == SEE_OTHER, response.text
        return client

    async def close(self) -> None:
        """Close every client this object opened."""
        for client in self._opened:
            await client.aclose()


async def lock_out(database: Database, kind: ThrottleKind, subject: str) -> None:
    """Record enough failures against a subject to lock it for the window."""
    async with database.session() as db:
        repository = LoginThrottleRepository(db)
        for _ in range(MAX_LOGIN_FAILURES):
            await repository.increment(
                kind, subject, max_failures=MAX_LOGIN_FAILURES, window=LOCKOUT_WINDOW
            )


async def read_events(db: Session) -> list[dict[str, Any]]:
    """Return every recorded event, oldest first, with detail decoded."""
    rows = await db.fetch_limited(
        "SELECT actor_type, actor_user_id, actor_subsystem, target_id, action, result, detail "
        "FROM activity_event ORDER BY id",
        (),
        limit=1000,
    )
    return [
        {**dict(row), "detail": json.loads(row["detail"]) if row["detail"] else {}} for row in rows
    ]


async def read_categories(db: Session) -> list[str]:
    """Return the category of every recorded event, oldest first."""
    rows = await db.fetch_limited("SELECT category FROM activity_event ORDER BY id", (), limit=1000)
    return [row["category"] for row in rows]


async def auth_events(app: FastAPI) -> list[dict[str, Any]]:
    """Return every recorded event of a running application."""
    async with app.state.database.session() as db:
        return await read_events(db)


async def fetch_scalar(app: FastAPI, sql: str, params: tuple[Any, ...] = ()) -> Any:
    """Return the first column of the first row of a query."""
    async with app.state.database.session() as db:
        row = await db.fetch_one(sql, params)
    return None if row is None else row[0]


async def outcomes(
    client: AsyncClient, requests: list[tuple[str, str]], **kwargs: Any
) -> dict[tuple[str, str], tuple[int, str | None]]:
    """Send each (method, path) once and map it to its status and Location header."""
    responses = {
        request: await client.request(request[0], request[1], follow_redirects=False, **kwargs)
        for request in requests
    }
    return {
        request: (response.status_code, response.headers.get("location"))
        for request, response in responses.items()
    }
