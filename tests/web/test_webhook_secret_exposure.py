"""No webhook secret reaches a log line, an activity row, a run's detail or an answer to a call."""

import asyncio
import io
import json
import logging

import pytest
from httpx import AsyncClient, Response

from app.config import ENV_PREFIX
from app.logging import REDACTED
from app.paths import HOOKS_PATH
from tests.docker_engine.container_engine import ContainerEngine
from tests.job_support import rows
from tests.web.environment_support import World
from tests.web.test_webhook_pages import Issued, create, enable
from tests.webhook_support import (
    ACCEPTED,
    TOO_MANY,
    call,
    fresh_token,
    plain_client,
    with_wrong_secret,
)

SETTLE_SECONDS = 20
BURST = 8
IMAGE = "busybox"
CONTROL_LOGGER = "tests.webhook_secret_control"


@pytest.fixture
def env(env: dict[str, str]) -> dict[str, str]:
    return env | {f"{ENV_PREFIX}LOG_LEVEL": "DEBUG"}


def leaks(secret: str, *texts: str) -> list[int]:
    """Return the position of each text that holds the secret."""
    return [position for position, text in enumerate(texts) if secret in text]


async def stored_text(world: World) -> list[str]:
    tables = ("activity_event", "job_run", "scheduled_job", "automation_rule", "webhook")
    return [
        json.dumps(await rows(world.app, f"SELECT * FROM {table}"), default=str) for table in tables
    ]


async def traffic(client: AsyncClient, issued: Issued) -> list[Response]:
    """Present the token every way a sender might, and then some that fail."""
    async with asyncio.timeout(SETTLE_SECONDS):
        burst = await asyncio.gather(*(call(client, issued.token, by="path") for _ in range(BURST)))
    return [
        *burst,
        await call(client, issued.token, by="header"),
        await call(client, with_wrong_secret(issued.token), by="path"),
        await call(client, with_wrong_secret(issued.token), by="header"),
        await call(client, issued.token, by="path", extra={"params": {"tag": "latest"}}),
        await call(client, fresh_token()[0], by="path"),
        await call(client, issued.token[:-4], by="path"),
    ]


async def test_no_log_line_row_run_detail_or_answer_holds_the_secret_while_the_scrubber_still_works(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    log_stream: io.StringIO,
) -> None:
    container_host.add_image(IMAGE)
    issued = await create(admin_client, world, target_name="warm")
    await enable(world, issued)
    scheduler = world.app.state.scheduler.scheduler
    async with plain_client(world.app) as sender:
        answers = await traffic(sender, issued)
    await scheduler.tick()
    async with asyncio.timeout(SETTLE_SECONDS):
        await scheduler.idle()
    logging.getLogger(CONTROL_LOGGER).warning("control %s", issued.token)

    logged = log_stream.getvalue()
    bodies = [f"{r.text}\n{dict(r.headers)}" for r in answers]
    [run] = await rows(world.app, "SELECT trigger, outcome, detail FROM job_run")
    acted = await rows(
        world.app, "SELECT action FROM activity_event WHERE actor_subsystem = 'webhook-receiver'"
    )
    assert TOO_MANY in {r.status_code for r in answers} and ACCEPTED in {
        r.status_code for r in answers
    }
    assert (run["trigger"], run["outcome"]) == ("webhook", "success")
    assert [row["action"] for row in acted] == ["container_recreated"]
    assert "webhook_refused" in logged
    assert f"dkwh_{issued.prefix}_{REDACTED}" in logged
    assert leaks(issued.secret, logged) == []
    assert leaks(issued.secret, *bodies) == []
    assert leaks(issued.secret, *await stored_text(world)) == []
    assert leaks(issued.secret, f"x {issued.token} y") == [0]
    assert HOOKS_PATH not in logged
