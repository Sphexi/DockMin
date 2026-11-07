"""The inbound webhook routes: what a call is answered, and what it can and cannot change."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.auth.sessions import session_cookie_name
from app.db.engine import get_session
from app.paths import HOOKS_PATH
from tests.auth_support import ADMIN_SESSION_TOKEN
from tests.webhook_support import (
    ACCEPTED,
    BAD_REQUEST,
    CLIENT_ADDRESS,
    CONFLICT,
    NOT_FOUND,
    PLAIN_BASE_URL,
    TOO_MANY,
    BodySpy,
    Clients,
    Hook,
    HookFactory,
    ListenerMark,
    Untouched,
    call,
    fresh_token,
    plain_client,
    request_id_free,
    unknown_path_answer,
    with_wrong_secret,
)

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.xdist_group("test_hooks_api"),
    pytest.mark.usefixtures("module_app_guard"),
]

FORMS = ["path", "header"]
BURST = 8
BURST_ADMITTED = 6
BURST_SECONDS = 20
UNSHARED_ADDRESS = "203.0.113.77"
SETTING_KEY = "subsystem.webhook-receiver.enabled"
FAR_FUTURE = "2099-01-01T00:00:00Z"
SEEDED_AT = "2026-01-01T00:00:00Z"
SECOND_CALL = 2
TLS_FILES = {"tls_cert_file": Path("server.pem"), "tls_key_file": Path("server.key")}

type Prepare = Callable[[HookFactory, Hook], Awaitable[str | None]]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def held(module_started_app: FastAPI) -> AsyncIterator[FastAPI]:
    """The module's application with its scheduler suspended, so an armed call never runs."""
    scheduler = module_started_app.state.scheduler.scheduler
    async with scheduler.suspend():
        yield module_started_app
        factory = HookFactory(module_started_app)
        await factory.sql("UPDATE scheduled_job SET next_run_at = NULL WHERE rule_id IS NOT NULL")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def hooks(held: FastAPI) -> HookFactory:
    return HookFactory(held)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def environment_id(hooks: HookFactory) -> int:
    return await hooks.environment()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def clients(held: FastAPI) -> AsyncIterator[Clients]:
    opened = Clients(held)
    yield opened
    await opened.aclose()


async def malformed(hooks: HookFactory, hook: Hook) -> str | None:
    return "not-a-token"


async def unknown_prefix(hooks: HookFactory, hook: Hook) -> str | None:
    return fresh_token()[0]


async def wrong_secret(hooks: HookFactory, hook: Hook) -> str | None:
    return with_wrong_secret(hook.token)


async def revoked(hooks: HookFactory, hook: Hook) -> str | None:
    await hooks.revoke(hook)
    return hook.token


async def rotated_out(hooks: HookFactory, hook: Hook) -> str | None:
    await hooks.rotate(hook)
    return hook.token


async def deleted(hooks: HookFactory, hook: Hook) -> str | None:
    await hooks.delete(hook)
    return hook.token


async def missing_header(hooks: HookFactory, hook: Hook) -> str | None:
    return None


async def disabled_owner(hooks: HookFactory, hook: Hook) -> str | None:
    await hooks.sql("UPDATE user SET is_enabled = 0 WHERE id = ?", (hook.owner_id,))
    return hook.token


async def missing_owner(hooks: HookFactory, hook: Hook) -> str | None:
    await hooks.sql(
        "UPDATE automation_rule SET created_by_user_id = NULL WHERE id = ?", (hook.rule_id,)
    )
    return hook.token


UNUSABLE: list[Prepare] = [
    malformed,
    unknown_prefix,
    wrong_secret,
    revoked,
    rotated_out,
    deleted,
    missing_header,
    disabled_owner,
    missing_owner,
]


@pytest.mark.parametrize("by", FORMS)
@pytest.mark.parametrize("prepare", UNUSABLE, ids=[case.__name__ for case in UNUSABLE])
async def test_an_unusable_token_is_answered_exactly_as_an_unknown_api_path_is(
    hooks: HookFactory, environment_id: int, clients: Clients, prepare: Prepare, by: str
) -> None:
    hook = await hooks.hook(environment_id)
    presented = await prepare(hooks, hook)
    client = clients.open()

    answer = await call(client, presented, by=by)
    baseline = await unknown_path_answer(client, presented, by=by)

    assert answer.status_code == NOT_FOUND
    assert request_id_free(answer) == request_id_free(baseline)


@pytest.mark.parametrize("by", FORMS)
async def test_a_valid_call_arms_the_job_once_and_counts_every_caller_after_the_first(
    hooks: HookFactory, environment_id: int, clients: Clients, by: str
) -> None:
    hook = await hooks.hook(environment_id)
    client = clients.open()

    first = await call(client, hook.token, by=by)
    second = await call(client, hook.token, by=by)

    stored = await hooks.webhook_row(hook)
    job = await hooks.job_row(hook)
    assert (first.status_code, first.json()) == (ACCEPTED, {"status": "accepted"})
    assert (second.status_code, second.json()) == (ACCEPTED, {"status": "already_pending"})
    assert first.headers["cache-control"] == "no-store"
    assert job["next_run_at"] is not None
    assert (stored["pending_calls"], stored["last_call_address"]) == (
        SECOND_CALL,
        CLIENT_ADDRESS,
    )


CLOSINGS = [
    pytest.param(
        "disabled", "UPDATE automation_rule SET is_enabled = 0 WHERE id = ?", id="disabled"
    ),
    pytest.param(
        "paused",
        f"UPDATE automation_rule SET paused_until = '{FAR_FUTURE}' WHERE id = ?",
        id="paused",
    ),
    pytest.param(
        "auto_disabled",
        "UPDATE scheduled_job SET is_enabled = 0 WHERE rule_id = ?",
        id="auto-disabled",
    ),
]


@pytest_asyncio.fixture(loop_scope="module")
async def subsystem_switched_off(hooks: HookFactory) -> AsyncIterator[None]:
    await hooks.sql(
        "INSERT INTO setting (key, value_type, value, updated_at) "
        "VALUES (?, 'boolean', 'false', ?)",
        (SETTING_KEY, SEEDED_AT),
    )
    yield
    await hooks.sql("DELETE FROM setting WHERE key = ?", (SETTING_KEY,))


def closed_answer_is(reason: str) -> dict[str, object]:
    return {"error": {"status": CONFLICT, "message": "Conflict", "reason": reason}}


@pytest.mark.parametrize("by", FORMS)
@pytest.mark.parametrize(("reason", "closing"), CLOSINGS)
async def test_a_valid_token_on_a_closed_webhook_is_answered_409_with_the_reason_and_arms_nothing(
    hooks: HookFactory,
    environment_id: int,
    clients: Clients,
    reason: str,
    closing: str,
    by: str,
) -> None:
    hook = await hooks.hook(environment_id)
    await hooks.sql(closing, (hook.rule_id,))

    answer = await call(clients.open(), hook.token, by=by)

    job = await hooks.job_row(hook)
    assert answer.status_code == CONFLICT
    assert answer.json() == closed_answer_is(reason)
    assert answer.headers["cache-control"] == "no-store"
    assert job["next_run_at"] is None


@pytest.mark.usefixtures("subsystem_switched_off")
@pytest.mark.parametrize("by", FORMS)
async def test_a_valid_token_while_the_subsystem_is_switched_off_is_answered_409_and_arms_nothing(
    hooks: HookFactory, environment_id: int, clients: Clients, by: str
) -> None:
    hook = await hooks.hook(environment_id)

    answer = await call(clients.open(), hook.token, by=by)

    job = await hooks.job_row(hook)
    assert (answer.status_code, answer.json()) == (CONFLICT, closed_answer_is("subsystem_disabled"))
    assert job["next_run_at"] is None


async def test_a_burst_of_eight_calls_is_answered_six_times_and_refused_twice_with_retry_after(
    hooks: HookFactory, environment_id: int, clients: Clients
) -> None:
    hook = await hooks.hook(environment_id)
    client = clients.open()

    async with asyncio.timeout(BURST_SECONDS):
        answers = await asyncio.gather(*(call(client, hook.token, by="path") for _ in range(BURST)))

    admitted = [answer for answer in answers if answer.status_code == ACCEPTED]
    refused = [answer for answer in answers if answer.status_code == TOO_MANY]
    stored = await hooks.webhook_row(hook)
    assert (len(admitted), len(refused)) == (BURST_ADMITTED, BURST - BURST_ADMITTED)
    assert sorted(answer.json()["status"] for answer in admitted) == [
        "accepted",
        *["already_pending"] * (BURST_ADMITTED - 1),
    ]
    assert all(int(answer.headers["retry-after"]) >= 1 for answer in refused)
    assert stored["pending_calls"] == BURST_ADMITTED


@pytest.mark.parametrize("by", FORMS)
@pytest.mark.parametrize("known", [True, False], ids=["valid-token", "unknown-token"])
async def test_a_query_string_is_refused_before_the_token_is_looked_at_and_arms_nothing(
    hooks: HookFactory, environment_id: int, clients: Clients, known: bool, by: str
) -> None:
    hook = await hooks.hook(environment_id)
    presented = hook.token if known else fresh_token()[0]

    answer = await call(clients.open(), presented, by=by, extra={"params": {"tag": "latest"}})

    job = await hooks.job_row(hook)
    assert answer.status_code == BAD_REQUEST
    assert answer.json()["error"]["reason"] == "query_not_supported"
    assert job["next_run_at"] is None


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        pytest.param("application/json", b'{"tag": ', id="json-that-does-not-parse"),
        pytest.param("application/x-www-form-urlencoded", b"tag=latest&=", id="form"),
        pytest.param("multipart/form-data; boundary=x", b"--x\r\nbroken", id="multipart"),
        pytest.param("text/plain", b"tag latest", id="text"),
        pytest.param("application/octet-stream", bytes(range(256)), id="binary"),
    ],
)
async def test_a_body_of_any_type_is_never_read_and_does_not_change_the_answer(
    hooks: HookFactory, held: FastAPI, environment_id: int, content_type: str, body: bytes
) -> None:
    hook = await hooks.hook(environment_id)
    spy = BodySpy(held)
    async with plain_client(spy) as client:
        answer = await client.post(hook.path, content=body, headers={"Content-Type": content_type})

    assert (answer.status_code, answer.json()) == (ACCEPTED, {"status": "accepted"})
    assert spy.reads == 0


@pytest.mark.parametrize("by", FORMS)
async def test_a_valid_session_cookie_neither_authorises_nor_changes_an_answer_and_none_is_set(
    hooks: HookFactory, held: FastAPI, environment_id: int, clients: Clients, by: str
) -> None:
    hook = await hooks.hook(environment_id)
    cookie = {session_cookie_name(held.state.settings): ADMIN_SESSION_TOKEN}
    signed_in = clients.open(cookies=cookie)
    anonymous = clients.open()
    unknown = fresh_token()[0]

    refused = await call(signed_in, unknown, by=by)
    refused_without = await call(anonymous, unknown, by=by)
    absent = await call(signed_in, None, by=by)
    absent_without = await call(anonymous, None, by=by)
    served = await call(signed_in, hook.token, by=by)

    assert request_id_free(refused) == request_id_free(refused_without)
    assert request_id_free(absent) == request_id_free(absent_without)
    assert (refused.status_code, absent.status_code) == (NOT_FOUND, NOT_FOUND)
    assert (served.status_code, served.json()) == (ACCEPTED, {"status": "accepted"})
    assert not any(
        "set-cookie" in answer.headers for answer in (refused, absent, served, refused_without)
    )


async def test_re_enabling_the_owner_restores_a_webhook_that_answered_404_while_disabled(
    hooks: HookFactory, environment_id: int, clients: Clients
) -> None:
    hook = await hooks.hook(environment_id)
    client = clients.open()
    await hooks.sql("UPDATE user SET is_enabled = 0 WHERE id = ?", (hook.owner_id,))
    while_disabled = await call(client, hook.token, by="path")
    await hooks.sql("UPDATE user SET is_enabled = 1 WHERE id = ?", (hook.owner_id,))

    after = await call(client, hook.token, by="path")

    assert (while_disabled.status_code, after.status_code) == (NOT_FOUND, ACCEPTED)


async def test_with_tls_enabled_a_call_off_the_tls_listener_is_refused_before_any_database_read(
    hooks: HookFactory,
    held: FastAPI,
    environment_id: int,
    clients: Clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = await hooks.hook(environment_id)
    monkeypatch.setattr(held.state, "settings", held.state.settings.model_copy(update=TLS_FILES))
    monkeypatch.setitem(held.dependency_overrides, get_session, lambda: Untouched())
    client = clients.open(base_url=PLAIN_BASE_URL)

    answer = await call(client, hook.token, by="path")

    assert answer.status_code == BAD_REQUEST
    assert answer.json() == {
        "error": {"status": BAD_REQUEST, "message": "Bad Request", "reason": "use_https"}
    }
    assert answer.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("tls_files", "base_url", "listener_marked"),
    [
        pytest.param({}, PLAIN_BASE_URL, False, id="tls-off"),
        pytest.param(TLS_FILES, "https://testserver", False, id="forwarded-https"),
        pytest.param(TLS_FILES, PLAIN_BASE_URL, True, id="tls-listener"),
    ],
)
async def test_a_call_is_served_where_tls_is_off_or_the_request_arrived_over_tls(
    hooks: HookFactory,
    held: FastAPI,
    environment_id: int,
    monkeypatch: pytest.MonkeyPatch,
    tls_files: dict[str, Path],
    base_url: str,
    listener_marked: bool,
) -> None:
    hook = await hooks.hook(environment_id)
    monkeypatch.setattr(held.state, "settings", held.state.settings.model_copy(update=tls_files))
    application = ListenerMark(held) if listener_marked else held
    async with plain_client(application, base_url=base_url, address=UNSHARED_ADDRESS) as client:
        answer = await call(client, hook.token, by="path")

    assert (answer.status_code, answer.json()) == (ACCEPTED, {"status": "accepted"})


async def test_the_webhook_paths_are_post_only(clients: Clients) -> None:
    client = clients.open()

    answers = [await client.request(method, HOOKS_PATH) for method in ("GET", "PUT", "DELETE")]

    assert {answer.status_code for answer in answers} == {405}
