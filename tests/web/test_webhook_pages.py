"""The webhook pages: the URL shown once, rotating and revoking it, and who may do which."""

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.paths import HOOKS_PATH
from app.services.webhooks import WEBHOOK_SUBSYSTEM
from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.job_support import execute, rows
from tests.web.environment_support import World, bearer, issue_token
from tests.web.flash_support import flash_cookie_of, flash_texts, land
from tests.web.helpers import hidden_inputs
from tests.webhook_support import (
    ACCEPTED,
    CONFLICT,
    NOT_FOUND,
    plain_client,
)

OK = 200
SEE_OTHER = 303
UNPROCESSABLE = 422
FORBIDDEN = 403
NO_STORE = "no-store"
HTMX = {"HX-Request": "true"}
TOKEN_SHAPE = re.compile(r"dkwh_([A-Za-z0-9]{8})_([A-Za-z0-9_-]{20,})")
RULE_LINK = re.compile(r"/automation/rules/(\d+)")
STALE_PREFIX = "StalePfx"
CHANGED = "The webhook changed since you reviewed it; nothing was changed."
REVOKED_NOTICE = "The webhook URL is revoked. It no longer starts the automation."
NOTICE_DISABLED = "The automation is disabled."
NOTICE_DELETED = "The automation is deleted."
RULE_NAME = "Deploy"
STACK = {"target_kind": "stack", "target_name": "shop"}


@dataclass(frozen=True, slots=True)
class Issued:
    rule_id: int
    prefix: str
    secret: str
    token: str


def webhook_form(client: AsyncClient, world: World, **changes: str) -> dict[str, str]:
    data = {
        "subsystem": WEBHOOK_SUBSYSTEM,
        "environment_id": str(world.remote_id),
        "name": RULE_NAME,
        "target_kind": "container",
        "target_name": "cache",
        "param_action": "redeploy",
        "param_verify_seconds": "0",
        "param_require_healthy": "false",
    }
    return csrf_form(client, **(data | changes))


def issued_by(response: Response) -> Issued:
    assert response.status_code == OK, re.sub(
        r"\s+", " ", re.sub(r"<[^>]+>", " ", response.text.split("Check the form", 1)[-1])
    )[:600]
    token = TOKEN_SHAPE.search(response.text)
    link = RULE_LINK.search(response.text)
    assert token is not None and link is not None, response.text[:1500]
    return Issued(int(link.group(1)), token.group(1), token.group(2), token.group(0))


async def create(client: AsyncClient, world: World, **changes: str) -> Issued:
    return issued_by(
        await client.post("/automation/rules", data=webhook_form(client, world, **changes))
    )


async def enable(world: World, issued: Issued) -> None:
    await execute(
        world.app, "UPDATE automation_rule SET is_enabled = 1 WHERE id = ?", (issued.rule_id,)
    )


async def call_with(world: World, token: str) -> int:
    async with plain_client(world.app) as http:
        return (await http.post(f"{HOOKS_PATH}/{token}")).status_code


async def webhook_row(world: World, issued: Issued) -> dict[str, object]:
    [row] = await rows(world.app, "SELECT * FROM webhook WHERE rule_id = ?", (issued.rule_id,))
    return row


async def job_of(world: World, issued: Issued) -> dict[str, object]:
    [row] = await rows(
        world.app, "SELECT * FROM scheduled_job WHERE rule_id = ?", (issued.rule_id,)
    )
    return row


@pytest.fixture
async def held(started_app: FastAPI) -> AsyncIterator[None]:
    """Keep the scheduler from running what a call arms."""
    async with started_app.state.scheduler.scheduler.suspend():
        yield
        await execute(started_app, "UPDATE scheduled_job SET next_run_at = NULL")


async def surfaces(client: AsyncClient, issued: Issued, api: dict[str, str]) -> dict[str, Response]:
    """Fetch every page, fragment and JSON route that could carry a rule's webhook."""
    rule = issued.rule_id
    job = await job_id_of(client, rule)
    pages = {
        "overview": "/automation",
        "detail": f"/automation/rules/{rule}",
        "edit": f"/automation/rules/{rule}/edit",
        "enable": f"/automation/rules/{rule}/enable",
        "run": f"/automation/rules/{rule}/run",
        "delete": f"/automation/rules/{rule}/delete",
        "rotate": f"/automation/rules/{rule}/webhook/rotate",
        "revoke": f"/automation/rules/{rule}/webhook/revoke",
        "jobs": "/jobs",
        "job": f"/jobs/{job}",
        "activity": "/activity",
    }
    found = {label: await client.get(path) for label, path in pages.items()}
    found |= {
        f"{label}-fragment": await client.get(path, headers=HTMX) for label, path in pages.items()
    }
    found["rules-json"] = await client.get("/api/v1/rules", headers=api)
    found["rule-json"] = await client.get(f"/api/v1/rules/{rule}", headers=api)
    return found


async def job_id_of(client: AsyncClient, rule: int) -> int:
    page = await client.get(f"/automation/rules/{rule}")
    link = re.search(r"/jobs/(\d+)", page.text)
    assert link is not None
    return int(link.group(1))


async def test_the_url_is_shown_once_on_the_create_response_and_nowhere_after_it(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    created = await admin_client.post("/automation/rules", data=webhook_form(admin_client, world))
    issued = issued_by(created)
    api = bearer(await issue_token(admin_client))

    seen = await surfaces(admin_client, issued, api)
    paused = await admin_client.post(
        f"/automation/rules/{issued.rule_id}/pause", data=csrf_form(admin_client, duration="1h")
    )
    landed = await land(admin_client, paused)
    flash_cookie = flash_cookie_of(admin_client)

    assert created.headers["cache-control"] == NO_STORE
    assert issued.token in created.text
    assert {label: r.status_code for label, r in seen.items()} == dict.fromkeys(seen, OK)
    assert [label for label, r in seen.items() if issued.secret in r.text] == []
    assert issued.secret not in landed.text
    assert flash_cookie is None or issued.secret not in flash_cookie
    assert all(issued.secret not in value for value in admin_client.cookies.values())
    assert f"dkwh_{issued.prefix}_" in seen["detail"].text


async def test_rotating_shows_the_new_url_once_and_stops_the_previous_one_at_once(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine, held: None
) -> None:
    first = await create(admin_client, world)
    await enable(world, first)
    api = bearer(await issue_token(admin_client))
    armed_by_old = await call_with(world, first.token)
    confirmation = await admin_client.get(f"/automation/rules/{first.rule_id}/webhook/rotate")

    rotated = await admin_client.post(
        f"/automation/rules/{first.rule_id}/webhook/rotate",
        data=csrf_form(admin_client, **hidden_inputs(confirmation.text)),
    )

    second = issued_by(rotated)
    seen = await surfaces(admin_client, second, api)
    job = await job_of(world, second)
    assert (confirmation.status_code, armed_by_old) == (OK, ACCEPTED)
    assert rotated.headers["cache-control"] == NO_STORE
    assert first.token not in rotated.text and "previous URL no longer works" in rotated.text
    assert (second.rule_id, second.prefix != first.prefix) == (first.rule_id, True)
    assert job["next_run_at"] is None
    assert await call_with(world, first.token) == NOT_FOUND
    assert await call_with(world, second.token) == ACCEPTED
    assert [
        label
        for label, response in seen.items()
        if first.secret in response.text or second.secret in response.text
    ] == []


async def test_a_stale_expected_prefix_is_refused_with_409_and_changes_nothing(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine, held: None
) -> None:
    issued = await create(admin_client, world)
    await enable(world, issued)
    before = await webhook_row(world, issued)

    rotate = await admin_client.post(
        f"/automation/rules/{issued.rule_id}/webhook/rotate",
        data=csrf_form(admin_client, expected_prefix=STALE_PREFIX),
    )
    revoke = await admin_client.post(
        f"/automation/rules/{issued.rule_id}/webhook/revoke",
        data=csrf_form(admin_client, expected_prefix=STALE_PREFIX),
    )

    assert (rotate.status_code, revoke.status_code) == (CONFLICT, CONFLICT)
    assert CHANGED in rotate.text and CHANGED in revoke.text
    assert issued.secret not in rotate.text
    assert await webhook_row(world, issued) == before
    assert await call_with(world, issued.token) == ACCEPTED


async def test_an_administrator_ends_another_users_webhook_but_cannot_rotate_it(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
    held: None,
) -> None:
    issued = await create(standard_client, world, **STACK)
    await enable(world, issued)
    before = await webhook_row(world, issued)
    base = f"/automation/rules/{issued.rule_id}"

    rotate_page = await admin_client.get(f"{base}/webhook/rotate")
    rotate = await admin_client.post(
        f"{base}/webhook/rotate", data=csrf_form(admin_client, expected_prefix=issued.prefix)
    )
    revoke_page = await admin_client.get(f"{base}/webhook/revoke")
    revoke = await admin_client.post(
        f"{base}/webhook/revoke",
        data=csrf_form(admin_client, **hidden_inputs(revoke_page.text)),
    )
    revoked_notice = flash_texts((await land(admin_client, revoke)).text)
    after_revoke = await call_with(world, issued.token)
    disable = await admin_client.post(f"{base}/disable", data=csrf_form(admin_client))
    disabled_notice = flash_texts((await land(admin_client, disable)).text)
    delete = await admin_client.post(
        f"{base}/delete",
        data=csrf_form(admin_client, expected_name=RULE_NAME, typed_name=RULE_NAME),
    )
    deleted_notice = flash_texts((await land(admin_client, delete)).text)

    assert (rotate_page.status_code, rotate.status_code) == (NOT_FOUND, NOT_FOUND)
    assert before["token_prefix"] == issued.prefix
    assert (revoke_page.status_code, revoke.status_code) == (OK, SEE_OTHER)
    assert revoked_notice == [REVOKED_NOTICE]
    assert after_revoke == NOT_FOUND
    assert (disable.status_code, disabled_notice) == (SEE_OTHER, [NOTICE_DISABLED])
    assert (delete.status_code, deleted_notice) == (SEE_OTHER, [NOTICE_DELETED])
    assert (
        await rows(world.app, "SELECT rule_id FROM webhook WHERE rule_id = ?", (issued.rule_id,))
        == []
    )
    assert (await standard_client.get(base)).status_code == NOT_FOUND


@pytest.mark.parametrize(
    ("target", "status"),
    [
        pytest.param("job", FORBIDDEN, id="read-only"),
        pytest.param("db", UNPROCESSABLE, id="managed-by-compose"),
    ],
)
async def test_a_standard_user_cannot_create_a_webhook_for_a_container_they_may_not_change(
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
    target: str,
    status: int,
) -> None:
    refused = await standard_client.post(
        "/automation/rules", data=webhook_form(standard_client, world, target_name=target)
    )

    assert refused.status_code == status
    assert TOKEN_SHAPE.search(refused.text) is None
    assert await rows(world.app, "SELECT rule_id FROM webhook") == []


async def test_a_standard_user_creates_a_webhook_on_a_remote_host_and_never_on_a_local_socket(
    standard_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    allowed = await standard_client.post(
        "/automation/rules", data=webhook_form(standard_client, world, **STACK)
    )
    local = await standard_client.post(
        "/automation/rules",
        data=webhook_form(standard_client, world, environment_id=str(world.local_id), name="Local"),
    )

    assert issued_by(allowed).prefix
    assert local.status_code == UNPROCESSABLE
    assert TOKEN_SHAPE.search(local.text) is None
    assert len(await rows(world.app, "SELECT rule_id FROM webhook")) == 1


async def test_the_json_rule_routes_show_the_webhook_without_its_secret_and_cannot_change_it(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    issued = await create(admin_client, world)
    api = bearer(await issue_token(admin_client))

    one = await admin_client.get(f"/api/v1/rules/{issued.rule_id}", headers=api)
    listing = await admin_client.get("/api/v1/rules", headers=api)
    created = await admin_client.post(
        "/api/v1/rules", headers=api, json={"subsystem": WEBHOOK_SUBSYSTEM}
    )
    rotated = await admin_client.post(f"/api/v1/rules/{issued.rule_id}/webhook/rotate", headers=api)
    revoked = await admin_client.post(f"/api/v1/rules/{issued.rule_id}/webhook/revoke", headers=api)

    stored = await webhook_row(world, issued)
    assert set(one.json()["webhook"]) == {"prefix", "state", "issued_at", "last_called_at"}
    assert (one.json()["webhook"]["prefix"], one.json()["webhook"]["state"]) == (
        issued.prefix,
        "active",
    )
    assert [item["webhook"]["prefix"] for item in listing.json()["items"]] == [issued.prefix]
    assert issued.secret not in one.text + listing.text
    assert str(stored["token_hash"]) not in one.text + listing.text
    assert {created.status_code, rotated.status_code, revoked.status_code} <= {NOT_FOUND, 405}
