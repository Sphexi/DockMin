"""Builders for the webhook tests: rules with a live token, and the requests that present it."""

import itertools
import json
from dataclasses import dataclass, field
from typing import Any, Final

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from starlette.types import ASGIApp

from app.auth.token_format import WEBHOOK_TOKEN_MARK, new_token
from app.auth.tokens import hash_token
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.db.repositories.user import UserRepository
from app.db.repositories.webhook import WebhookRepository
from app.logging import REQUEST_ID_HEADER
from app.paths import HOOKS_PATH
from app.services.webhooks import WEBHOOK_SUBSYSTEM
from app.tls import TLS_LISTENER_SCOPE_KEY

UNKNOWN_API_PATH: Final = "/api/v1/no-such-route"
CLIENT_ADDRESS: Final = "198.51.100.40"
PLAIN_BASE_URL: Final = "http://testserver"
SECURE_BASE_URL: Final = "https://testserver"
OWNER_PASSWORD_HASH: Final = "not-a-real-hash"
SEEDED_AT: Final = "2026-01-01T00:00:00Z"
ACCEPTED: Final = 202
NOT_FOUND: Final = 404
CONFLICT: Final = 409
TOO_MANY: Final = 429
BAD_REQUEST: Final = 400
CONTAINER_SELECTOR: Final = json.dumps({"kind": "container", "names": ["web"]})

_numbers = itertools.count(1)


@dataclass(frozen=True, slots=True)
class Hook:
    """A webhook rule that exists in the database, with the one token that opens it."""

    rule_id: int
    job_id: int
    owner_id: int
    prefix: str
    token: str = field(repr=False)

    @property
    def path(self) -> str:
        return f"{HOOKS_PATH}/{self.token}"

    @property
    def header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def fresh_token() -> tuple[str, str]:
    """Return a well-formed token no database holds, and its prefix."""
    return new_token(WEBHOOK_TOKEN_MARK)


def with_wrong_secret(token: str) -> str:
    """Return the token with its last character changed to another that the format allows."""
    return token[:-1] + ("A" if token[-1] != "A" else "B")


class HookFactory:
    """Creates owners, environments and webhook rules directly in an application's database."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._clock = app.state.auth.clock

    async def environment(self) -> int:
        number = next(_numbers)
        name = f"hook-host-{number}"
        async with self._app.state.database.session() as db:
            await db.execute(
                "INSERT INTO environment (name, connection_kind, socket_path, created_at, "
                "updated_at) VALUES (?, 'unix_socket', ?, ?, ?)",
                (name, f"/nonexistent/hook-{number}.sock", SEEDED_AT, SEEDED_AT),
            )
            row = await db.fetch_one("SELECT id FROM environment WHERE name = ?", (name,))
        assert row is not None
        return int(row["id"])

    async def owner(self, *, role: str = "administrator") -> int:
        number = next(_numbers)
        async with self._app.state.database.session() as db:
            user = await UserRepository(db, self._clock).create(
                f"hook-owner-{number}",
                OWNER_PASSWORD_HASH,
                role,  # type: ignore[arg-type]
            )
        return user.id

    async def hook(
        self,
        environment_id: int,
        *,
        owner_id: int | None = None,
        enabled: bool = True,
        selector: str = CONTAINER_SELECTOR,
        parameters: str = '{"action": "redeploy", "verify_seconds": 0}',
    ) -> Hook:
        owner = await self.owner() if owner_id is None else owner_id
        value, prefix = fresh_token()
        async with self._app.state.database.session() as db, db.transaction():
            rule = await AutomationRuleRepository(db, self._clock).insert(
                name=f"hook-{next(_numbers)}",
                subsystem=WEBHOOK_SUBSYSTEM,
                environment_id=environment_id,
                target_selector=selector,
                parameters=parameters,
                is_enabled=enabled,
                created_by_user_id=owner,
            )
            job = await ScheduledJobRepository(db, self._clock).insert(
                subsystem=WEBHOOK_SUBSYSTEM,
                rule_id=rule.id,
                environment_id=environment_id,
                schedule_kind="webhook",
                schedule_expression="",
                time_zone="UTC",
                overrun_policy="skip",
                catch_up=False,
                failure_threshold=5,
                next_run_at=None,
            )
            await WebhookRepository(db, self._clock).create(rule.id, prefix, hash_token(value))
        return Hook(rule.id, job.id, owner, prefix, value)

    @property
    def app(self) -> FastAPI:
        return self._app

    async def sql(self, statement: str, params: tuple[Any, ...] = ()) -> None:
        async with self._app.state.database.session() as db:
            await db.execute(statement, params)

    async def revoke(self, hook: Hook) -> None:
        async with self._app.state.database.session() as db, db.transaction():
            revoked = await WebhookRepository(db, self._clock).revoke(
                hook.rule_id, expected_prefix=hook.prefix, reason="manual"
            )
        assert revoked

    async def rotate(self, hook: Hook) -> Hook:
        """Replace the token and return the hook holding the new one."""
        value, prefix = fresh_token()
        async with self._app.state.database.session() as db, db.transaction():
            rotated = await WebhookRepository(db, self._clock).rotate(
                hook.rule_id,
                expected_prefix=hook.prefix,
                prefix=prefix,
                token_hash=hash_token(value),
            )
        assert rotated
        return Hook(hook.rule_id, hook.job_id, hook.owner_id, prefix, value)

    async def delete(self, hook: Hook) -> None:
        await self.sql("DELETE FROM automation_rule WHERE id = ?", (hook.rule_id,))

    async def webhook_row(self, hook: Hook) -> dict[str, Any]:
        async with self._app.state.database.session() as db:
            row = await db.fetch_one("SELECT * FROM webhook WHERE rule_id = ?", (hook.rule_id,))
        assert row is not None
        return dict(row)

    async def job_row(self, hook: Hook) -> dict[str, Any]:
        async with self._app.state.database.session() as db:
            row = await db.fetch_one("SELECT * FROM scheduled_job WHERE id = ?", (hook.job_id,))
        assert row is not None
        return dict(row)


def plain_client(
    app: ASGIApp,
    *,
    base_url: str = SECURE_BASE_URL,
    address: str = CLIENT_ADDRESS,
    cookies: dict[str, str] | None = None,
) -> AsyncClient:
    """Return a client with no Origin header and no CSRF cookie, as a pipeline's would be."""
    transport = ASGITransport(app=app, raise_app_exceptions=False, client=(address, 5000))
    return AsyncClient(transport=transport, base_url=base_url, cookies=cookies)


def request_id_free(response: Response) -> tuple[int, bytes, dict[str, str]]:
    """Return what makes two answers byte-identical: status, body and every header but the id."""
    headers = {
        name: value
        for name, value in response.headers.items()
        if name.lower() != REQUEST_ID_HEADER.lower()
    }
    return response.status_code, response.content, headers


async def call(
    client: AsyncClient, hook_token: str | None, *, by: str, extra: dict[str, Any] | None = None
) -> Response:
    """POST a call with the token in the path or in the Authorization header.

    A missing token sends no credential in either form.
    """
    options = extra or {}
    if hook_token is None:
        return await client.post(HOOKS_PATH, **options)
    if by == "path":
        return await client.post(f"{HOOKS_PATH}/{hook_token}", **options)
    headers = {"Authorization": f"Bearer {hook_token}"} | options.pop("headers", {})
    return await client.post(HOOKS_PATH, headers=headers, **options)


async def unknown_path_answer(client: AsyncClient, hook_token: str | None, *, by: str) -> Response:
    """POST what a call would be, to an /api path that does not exist."""
    if hook_token is None:
        return await client.post(UNKNOWN_API_PATH)
    if by == "path":
        return await client.post(f"{UNKNOWN_API_PATH}/{hook_token}")
    return await client.post(UNKNOWN_API_PATH, headers={"Authorization": f"Bearer {hook_token}"})


class Clients:
    """Opens clients with no cookies and no Origin header against one application."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._opened: list[AsyncClient] = []

    def open(self, **options: Any) -> AsyncClient:
        client = plain_client(self._app, **options)
        self._opened.append(client)
        return client

    async def aclose(self) -> None:
        for client in self._opened:
            await client.aclose()


class Untouched:
    """Stands in for a database session and fails the moment anything reaches for one."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the database was used: {name}")


class BodySpy:
    """Wraps an application and counts the request body messages it asks for."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self.reads = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def counted() -> Any:
            message = await receive()
            if message["type"] == "http.request":
                self.reads += 1
            return message

        await self._app(scope, counted, send)


class ListenerMark:
    """Wraps an application and marks every request as arriving on the process's TLS listener."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        scope[TLS_LISTENER_SCOPE_KEY] = True
        await self._app(scope, receive, send)
