"""No registry secret reaches an image page, a job, a stream, a log record or an activity row."""

import asyncio
import io
import json
from dataclasses import dataclass

import pytest
from httpx import AsyncClient, Response
from pydantic import SecretStr

from app.config import ENV_PREFIX
from app.registry_client.auth_config import basic_token
from tests.access_support import ADMIN
from tests.auth_support import csrf_form, read_events
from tests.docker_engine.container_engine import ContainerEngine
from tests.image_support import digest, full_id, image_id
from tests.stream_support import read_stream
from tests.web.environment_support import World
from tests.web.image_support import (
    ScriptedManifests,
    bind,
    image_path,
    images_path,
    make_set,
)
from tests.web.registry_support import HTMX, leaks, rendered

SECRET = "image-set-secret-marker-9931"
USERNAME = "image-set-user-marker"
TARGET = "registry.example.com/team/app:1"
REPOSITORY = "registry.example.com/team/app"
PUSHED = 7
OK = 200


@pytest.fixture
def env(env: dict[str, str]) -> dict[str, str]:
    return env | {f"{ENV_PREFIX}LOG_LEVEL": "DEBUG"}


@dataclass(slots=True)
class Traffic:
    responses: list[Response]
    streams: list[str]
    states: list[str]
    markers: list[str]


async def settled(world: World, job_id: str) -> None:
    await asyncio.wait({world.app.state.engine.image_jobs._jobs[job_id].task})


async def exercise(
    admin: AsyncClient,
    standard: AsyncClient,
    world: World,
    host: ContainerEngine,
    manifests: ScriptedManifests,
) -> Traffic:
    """Pull, push, check and browse with a set whose secret the daemon and registry would see."""
    chosen = await make_set(admin, world, "alpha", users=True, username=USERNAME, secret=SECRET)
    host.add_image(TARGET, full_id(PUSHED), digests=(f"{REPOSITORY}@{digest(1)}",))
    await bind(world, chosen, TARGET)
    host.pull_id = full_id(PUSHED)
    responses: list[Response] = []
    streams: list[str] = []
    states: list[str] = []
    for client in (admin, standard):
        for path in (
            images_path(world),
            f"{images_path(world)}?binding=bound",
            f"{images_path(world)}/pull",
            f"{images_path(world)}/pull?reference={TARGET}",
            image_path(world, image_id(PUSHED)),
            f"{image_path(world, image_id(PUSHED))}?tab=configuration",
            f"{image_path(world, image_id(PUSHED))}?tab=history",
            image_path(world, image_id(PUSHED), "/push"),
            f"{image_path(world, image_id(PUSHED), '/push')}?reference={TARGET}",
        ):
            responses.append(await client.get(path))
        responses.append(
            await client.post(
                f"{images_path(world)}/pull/choices",
                data=csrf_form(client, reference=TARGET),
                headers=HTMX,
            )
        )
        responses.append(
            await client.post(
                f"{images_path(world)}/updates", data=csrf_form(client, reference=TARGET)
            )
        )
    for path in (
        image_path(world, image_id(PUSHED), "/remove"),
        image_path(world, image_id(PUSHED), f"/untag?reference={TARGET}"),
        image_path(world, image_id(PUSHED), f"/tag?reference={TARGET}x"),
        f"{images_path(world)}/prune?mode=unused",
        f"{images_path(world)}/bindings/reap",
    ):
        responses.append(await admin.get(path))
    for transfer, form in (
        ("/pull", {"reference": TARGET, "credential_choice": str(chosen)}),
        (f"/{image_id(PUSHED)}/push", {"reference": TARGET, "credential_choice": str(chosen)}),
    ):
        started = await admin.post(f"{images_path(world)}{transfer}", data=csrf_form(admin, **form))
        responses.append(started)
        job_id = started.headers["location"].rsplit("/", 1)[1]
        await settled(world, job_id)
        responses.append(await admin.get(started.headers["location"]))
        streamed = await read_stream(world.app, admin, f"{images_path(world)}/jobs/{job_id}/events")
        streams.append(streamed.body)
        states.append(repr(world.app.state.engine.image_jobs.view(job_id, ADMIN)))
    states.append(repr(world.app.state.engine.image_jobs._jobs))
    headers = [
        request.headers["x-registry-auth"]
        for request in host.requests
        if "x-registry-auth" in request.headers
    ]
    basic = basic_token(USERNAME, SecretStr(SECRET)).get_secret_value()
    return Traffic(responses, streams, states, [SECRET, basic, *headers])


async def test_no_registry_secret_reaches_a_response_a_stream_a_job_a_log_or_an_activity_row(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    log_stream: io.StringIO,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    manifests = ScriptedManifests()
    world.app.state.registries.manifests = manifests

    traffic = await exercise(admin_client, standard_client, world, container_host, manifests)

    async with world.app.state.database.session() as db:
        events = await read_events(db)
    raw_records = [
        f"{record.getMessage()} {json.dumps(record.__dict__, default=str)}"
        for record in caplog.records
    ]
    assert len(traffic.markers) >= 4
    assert {r.status_code for r in traffic.responses} <= {OK, 303}
    assert leaks([rendered(response) for response in traffic.responses], traffic.markers) == []
    assert leaks(traffic.streams, traffic.markers) == []
    assert leaks(traffic.states, traffic.markers) == []
    assert any("Succeeded" in body for body in traffic.streams)
    assert leaks(raw_records, traffic.markers) == []
    assert leaks([log_stream.getvalue()], traffic.markers) == []
    assert leaks([json.dumps(events)], traffic.markers) == []
    assert {"image_pulled", "image_pushed"} <= {event["action"] for event in events}
    assert [call[2].username for call in manifests.calls if call[2]] == [USERNAME]
