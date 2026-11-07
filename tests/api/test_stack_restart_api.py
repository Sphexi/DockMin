"""The stack JSON restart, and the name and port collision checks on JSON create and redeploy."""

from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from app.services.activity import UserActor
from app.services.object_locks import LockHolder, ObjectKey
from app.services.stack_views import containers_digest
from tests.access_support import container_id
from tests.api.test_stacks_api import (
    BAD_REQUEST,
    CONFLICT,
    CREATED,
    FORBIDDEN,
    NOT_FOUND,
    OK,
    OUTCOME_KEYS,
    UNAUTHENTICATED,
    UNPROCESSABLE,
    WEB_SERVICE,
    ZERO_DIGEST,
    base,
    recorded,
    scripted,
    stack,
)
from tests.auth_support import ADMIN_ID, ADMIN_USERNAME, Browsers
from tests.container_support import STACK_LABEL, plant_control
from tests.docker_engine.container_engine import ContainerEngine, Publish
from tests.self_support import home_identity
from tests.services.test_stack_restart import calls as lifecycle_calls
from tests.stack_support import SERVICE_LABEL, install_scripted_compose
from tests.web.environment_support import World, bearer, issue_token
from tests.web.stack_file_support import seed_stack, stack_rows

NAME = "alpha"
MINE = "mine"
IMAGE = "nginx:1"
PULLED_IMAGE = "nginx:2"
THREE_SERVICES = (
    f"services:\n  web:\n    image: {IMAGE}\n  db:\n    image: {IMAGE}\n"
    f"  cache:\n    image: {IMAGE}\n"
)
RUNNING_PORT, STOPPED_PORT, FREE_PORT = 18080, 19090, 17070
WIDE_RANGE = "9000-10100"
HOLDER_NAME = "legacy-web"
EXTRA, HIDDEN = container_id(0xE1), container_id(0xE2)
RESTART_ROWS = (
    "SELECT result, actor_type, actor_user_id, json_extract(detail, '$.method') AS method, "
    "json_extract(detail, '$.reason') AS reason FROM activity_event "
    "WHERE action = 'stack_restarted' ORDER BY id"
)
STOPPED_WARNING = (
    f"Service web: port {STOPPED_PORT}/tcp on every address is published by the container old, "
    "which is not running."
)


@pytest.fixture
async def stack_host(world: World, container_host: ContainerEngine) -> ContainerEngine:
    container_host.add_image(IMAGE)
    await install_scripted_compose(world.app.state.engine.compose, container_host)
    return container_host


@pytest.fixture
async def admin_api(browsers: Browsers, admin_client: AsyncClient) -> AsyncClient:
    caller = browsers.open()
    caller.headers.update(bearer(await issue_token(admin_client)))
    return caller


@pytest.fixture
async def user_api(browsers: Browsers, standard_client: AsyncClient) -> AsyncClient:
    caller = browsers.open()
    caller.headers.update(bearer(await issue_token(standard_client)))
    return caller


def error_of(reply: Any) -> dict[str, Any]:
    found: dict[str, Any] = reply.json()["error"]
    return found


def stop_service(host: ContainerEngine, project: str, service: str) -> None:
    for box in host.boxes.values():
        if box.labels.get(STACK_LABEL) == project and box.labels.get(SERVICE_LABEL) == service:
            box.state = "exited"


def project_ids(host: ContainerEngine, project: str) -> list[str]:
    return [b.id for b in host.boxes.values() if b.labels.get(STACK_LABEL) == project]


def publishing(*ports: int | str, container_name: str | None = None, image: str = IMAGE) -> str:
    lines = ["services:", "  web:", f"    image: {image}"]
    if container_name is not None:
        lines.append(f"    container_name: {container_name}")
    lines.append("    ports:")
    lines.extend(f"      - '{port}:80'" for port in ports)
    return "\n".join(lines) + "\n"


def hold_ports(host: ContainerEngine) -> None:
    host.add(container_id(0x61), "api", publishes=[Publish(RUNNING_PORT)])
    host.add(container_id(0x62), "old", state="exited", publishes=[Publish(STOPPED_PORT)])
    host.add(container_id(0x63), HOLDER_NAME)


async def test_a_json_restart_restarts_only_the_running_services_and_returns_the_action_object(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    url = f"{base(world)}/{NAME}"
    record = tmp_path / "calls.jsonl"
    await admin_api.post(
        base(world), json=stack(NAME, scripted(THREE_SERVICES, record=str(record)))
    )
    stop_service(stack_host, NAME, "cache")
    bound = containers_digest(project_ids(stack_host, NAME))
    calls_before = lifecycle_calls(record)

    stale = await admin_api.post(f"{url}/restart", json={"expected_containers": ZERO_DIGEST})
    stale_calls = lifecycle_calls(record)
    restarted = await admin_api.post(f"{url}/restart")
    bound_restart = await admin_api.post(f"{url}/restart", json={"expected_containers": bound})
    null_binding = await admin_api.post(f"{url}/restart", json={"expected_containers": None})
    malformed = await admin_api.post(f"{url}/restart", json={"expected_containers": "nothex"})
    extra = await admin_api.post(f"{url}/restart", json={"surprise": 1})
    detail = (await admin_api.get(url)).json()
    events = await stack_rows(world, RESTART_ROWS)

    assert stale.status_code == CONFLICT and error_of(stale)["reason"] == "changed_since_review"
    assert stale_calls == calls_before == []
    assert restarted.status_code == OK
    assert set(restarted.json()) == {"name", "outcome", "warnings"}
    assert set(restarted.json()["outcome"]) == OUTCOME_KEYS
    assert restarted.json()["outcome"]["operation"] == "restart"
    assert restarted.json()["outcome"]["services_running"] == 2
    assert [bound_restart.status_code, null_binding.status_code] == [OK, OK]
    assert [malformed.status_code, extra.status_code] == [UNPROCESSABLE, UNPROCESSABLE]
    assert lifecycle_calls(record) == [["stop", "db", "web"], ["start", "db", "web"]] * 3
    states = {c["name"]: c["state"] for s in detail["services"] for c in s["containers"]}
    assert states == {"alpha-web-1": "running", "alpha-db-1": "running", "alpha-cache-1": "exited"}
    assert [
        (e["result"], e["actor_type"], e["actor_user_id"], e["method"], e["reason"]) for e in events
    ] == [
        ("failure", "user", ADMIN_ID, "stop_start", "changed_since_review"),
        ("success", "user", ADMIN_ID, "stop_start", None),
        ("success", "user", ADMIN_ID, "stop_start", None),
        ("success", "user", ADMIN_ID, "stop_start", None),
    ]


async def test_a_json_restart_that_cannot_run_answers_a_stable_reason_and_runs_nothing(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    url = f"{base(world)}/{NAME}"
    record = tmp_path / "calls.jsonl"
    await seed_stack(world.app, world.remote_id, "fresh", WEB_SERVICE)
    await admin_api.post(base(world), json=stack(NAME, scripted(WEB_SERVICE, record=str(record))))
    key = ObjectKey(world.remote_id, "stack", NAME)
    holder = LockHolder(UserActor(ADMIN_ID), ADMIN_USERNAME, "redeploy")

    async with world.app.state.engine.locks.hold(key, holder):
        busy = await admin_api.post(f"{url}/restart")
    world.app.state.self_identity = home_identity(world.remote_id, compose_project=NAME)
    protected = await admin_api.post(f"{url}/restart")
    world.app.state.self_identity = None
    never_deployed = await admin_api.post(f"{base(world)}/fresh/restart")
    stop_service(stack_host, NAME, "web")
    stopped = await admin_api.post(f"{url}/restart")
    unknown = [
        (await admin_api.post(f"{base(world)}/{name}/restart")).status_code
        for name in ("nothere", "Alpha", "a b")
    ]

    assert busy.status_code == CONFLICT and error_of(busy)["reason"] == "object_busy"
    assert protected.status_code == CONFLICT and error_of(protected)["reason"] == "self_protected"
    assert never_deployed.status_code == CONFLICT
    assert error_of(never_deployed)["reason"] == "not_deployed"
    assert stopped.status_code == CONFLICT and error_of(stopped)["reason"] == "not_running"
    assert unknown == [NOT_FOUND] * 3
    assert lifecycle_calls(record) == []


async def test_a_json_restart_is_refused_to_a_user_who_cannot_change_every_container(
    admin_api: AsyncClient,
    user_api: AsyncClient,
    browsers: Browsers,
    world: World,
    stack_host: ContainerEngine,
    tmp_path: Path,
) -> None:
    url = f"{base(world)}/{MINE}"
    record = tmp_path / "calls.jsonl"
    await user_api.post(base(world), json=stack(MINE, scripted(WEB_SERVICE, record=str(record))))
    await seed_stack(world.app, world.local_id, "shared", WEB_SERVICE)
    await plant_control(world.app, world.local_id, "stack", "shared", public=True, level="write")
    stack_host.add(EXTRA, "mine-extra-1", labels={STACK_LABEL: MINE, SERVICE_LABEL: "extra"})
    await plant_control(world.app, world.remote_id, "container", EXTRA, public=True, level="read")
    anonymous, forged = browsers.open(), browsers.open()
    forged.headers.update(bearer("dkm_notatoken"))

    restricted = await user_api.post(f"{url}/restart")
    local_socket = await user_api.post(f"{base(world, world.local_id)}/shared/restart")
    anonymous_restart = await anonymous.post(f"{url}/restart")
    forged_restart = await forged.post(f"{url}/restart")
    allowed = await admin_api.post(f"{url}/restart")

    assert restricted.status_code == FORBIDDEN and error_of(restricted)["reason"] == "restricted"
    assert local_socket.status_code == FORBIDDEN
    assert anonymous_restart.status_code == FORBIDDEN
    assert forged_restart.status_code == UNAUTHENTICATED
    assert allowed.status_code == OK
    assert lifecycle_calls(record) == [["stop", "extra", "web"], ["start", "extra", "web"]]
    assert [r["reason"] for r in await stack_rows(world, RESTART_ROWS) if r["reason"]] == [
        "restricted"
    ]


async def test_a_json_create_is_refused_for_a_held_name_or_port_before_any_image_is_pulled(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    url = base(world)
    hold_ports(stack_host)

    named = await admin_api.post(
        url, json=stack("named", publishing(7000, container_name=HOLDER_NAME, image=PULLED_IMAGE))
    )
    ported = await admin_api.post(
        url, json=stack("ported", publishing(RUNNING_PORT, STOPPED_PORT, image=PULLED_IMAGE))
    )
    wide = await admin_api.post(url, json=stack("wide", publishing(WIDE_RANGE)))
    listing = await admin_api.get(url)
    assert stack_host.verbs() == []
    free = await admin_api.post(url, json=stack("free", publishing(FREE_PORT, image=PULLED_IMAGE)))

    assert named.status_code == CONFLICT
    assert error_of(named)["reason"] == "container_name_in_use"
    assert HOLDER_NAME in error_of(named)["fields"][0]["msg"]
    assert ported.status_code == CONFLICT and error_of(ported)["reason"] == "port_in_use"
    assert f"Port {RUNNING_PORT}/tcp" in error_of(ported)["fields"][0]["msg"]
    assert error_of(ported)["warnings"] == [STOPPED_WARNING]
    assert wide.status_code == BAD_REQUEST
    assert error_of(wide)["reason"] == "port_range_too_large"
    assert listing.json()["items"] == []
    assert free.status_code == CREATED and "pull" in stack_host.verbs()


async def test_json_create_and_redeploy_return_the_warnings_a_stopped_holder_raises(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    url = base(world)
    record = tmp_path / "calls.jsonl"
    hold_ports(stack_host)
    text = scripted(publishing(STOPPED_PORT), record=str(record))

    created = await admin_api.post(url, json=stack(NAME, text))
    detail = (await admin_api.get(f"{url}/{NAME}")).json()
    review = {
        "expected_file_digest": detail["compose_file_digest"],
        "expected_inputs_digest": detail["inputs_digest"],
    }
    redeployed = await admin_api.post(f"{url}/{NAME}/redeploy", json=review)
    stack_host.add(container_id(0x64), "late", publishes=[Publish(STOPPED_PORT)])
    ups_before = [c["cmd"] for c in recorded(record)].count("up")
    refused = await admin_api.post(f"{url}/{NAME}/redeploy", json=review)

    assert created.status_code == CREATED and created.json()["warnings"] == [STOPPED_WARNING]
    assert redeployed.status_code == OK and redeployed.json()["warnings"] == [STOPPED_WARNING]
    assert refused.status_code == CONFLICT and error_of(refused)["reason"] == "port_in_use"
    assert STOPPED_WARNING in error_of(refused)["warnings"]
    assert [c["cmd"] for c in recorded(record)].count("up") == ups_before
    assert stack_host.verbs().count("pull") == 0
