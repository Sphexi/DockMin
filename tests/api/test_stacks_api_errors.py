"""The stack JSON routes' refusals, host failures and limits, and what a standard user may do."""

import asyncio
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

import app.docker_engine.resources as resources
from app.auth.principal import Principal
from app.compose_cli.errors import ComposeUnavailable, ComposeVersionUnsupported
from app.services.activity import UserActor
from app.services.object_inspector import ObjectTooLarge
from app.services.object_locks import LockHolder, ObjectKey
from app.services.stacks import StackService
from tests.api.test_stacks_api import (
    BAD_GATEWAY,
    BAD_REQUEST,
    CONFLICT,
    CREATED,
    FORBIDDEN,
    IMAGE,
    NOT_FOUND,
    OK,
    UNPROCESSABLE,
    WEB_SERVICE,
    ZERO_DIGEST,
    base,
    recorded,
    scripted,
    stack,
)
from tests.auth_support import ADMIN_ID, ADMIN_USERNAME, Browsers
from tests.docker_engine.container_engine import ENGINE_TEXT, ContainerEngine
from tests.docker_engine.fake_engine import Reply
from tests.stack_support import install_scripted_compose, use_compose_limits
from tests.web.environment_support import REMOTE_NAME, World, bearer, issue_token
from tests.web.registry_support import create_set, custom_form, registry_form

TOO_MANY_REQUESTS, UNAVAILABLE, DAEMON_FAILURE = 429, 503, 500
OVER_THE_CAP = {"Content-Length": "999"}
TINY_CAP = 10
UNKNOWN_ENVIRONMENT = 99999
BEYOND_INT64 = 2**63
PULLED = "nginx:2"
BLOCKED_PULL = "nginx:3"
SHORT_DEADLINE = 1.5
BOUNDED_SECONDS = 10
HOLDER_PROCESS = 100
INSTALLED, REQUIRED = "v2.20.2", "v2.32.0"


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


def ups(path: Path) -> int:
    return sum(entry["cmd"] == "up" for entry in recorded(path))


def error_of(reply: Any) -> dict[str, Any]:
    returned: dict[str, Any] = reply.json()["error"]
    return returned


async def test_two_candidate_credential_sets_make_a_create_ask_for_a_choice_and_a_failed_pull_502(
    admin_api: AsyncClient,
    admin_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
) -> None:
    url = base(world)
    for name in ("first-set", "second-set"):
        set_id = await create_set(
            admin_client, custom_form(admin_client, name, address="docker.io")
        )
        await admin_client.post(
            f"/registries/{set_id}/environments",
            data=registry_form(admin_client, environment_name=REMOTE_NAME),
        )
    text = WEB_SERVICE.replace(IMAGE, PULLED)

    asked = await admin_api.post(url, json=stack("alpha", text))
    absent = await admin_api.get(f"{url}/alpha")
    offer = error_of(asked)["references"][0]
    chosen = await admin_api.post(
        url,
        json=stack(
            "alpha",
            text,
            credential_choices={
                offer["reference"]: {"credential_set_id": offer["candidates"][0]["id"]}
            },
        ),
    )
    ambiguous = await admin_api.post(
        url,
        json=stack(
            "other",
            text,
            credential_choices={offer["reference"]: {"credential_set_id": 1, "anonymous": True}},
        ),
    )
    stack_host.pull_status = DAEMON_FAILURE
    blocked = await admin_api.post(
        url,
        json=stack(
            "blocked",
            text.replace(PULLED, BLOCKED_PULL),
            credential_choices={BLOCKED_PULL: {"anonymous": True}},
        ),
    )

    assert asked.status_code == CONFLICT
    assert error_of(asked)["reason"] == "credential_choice_required"
    assert "stack_created" not in error_of(asked)
    assert offer["reference"] == PULLED and len(offer["candidates"]) == 2
    assert {candidate["name"] for candidate in offer["candidates"]} == {"first-set", "second-set"}
    assert absent.status_code == NOT_FOUND
    assert chosen.status_code == CREATED and chosen.json()["outcome"]["pulled"] == [PULLED]
    assert ambiguous.status_code == UNPROCESSABLE
    assert blocked.status_code == BAD_GATEWAY
    assert error_of(blocked)["reason"] == "pull_failed"
    assert error_of(blocked)["stack_created"] is True
    assert ENGINE_TEXT not in blocked.text


async def test_a_request_the_host_state_contradicts_is_a_409_or_400_and_runs_nothing(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    url = base(world)
    record = tmp_path / "calls.jsonl"
    stack_host.add_network("delta_default", 0xF100)
    alpha = scripted(WEB_SERVICE, record=str(record))
    await admin_api.post(url, json=stack("alpha", alpha))
    detail = (await admin_api.get(f"{url}/alpha")).json()
    digest = detail["compose_file_digest"]
    reviewed = {"expected_file_digest": digest, "expected_inputs_digest": detail["inputs_digest"]}
    deployed = ups(record)
    key = ObjectKey(world.remote_id, "stack", "alpha")
    holder = LockHolder(UserActor(ADMIN_ID), ADMIN_USERNAME, "redeploy")

    preexisting = await admin_api.post(url, json=stack("delta"))
    stale_file = await admin_api.post(
        f"{url}/alpha/redeploy",
        json={"expected_file_digest": ZERO_DIGEST, "expected_inputs_digest": None},
    )
    stale_containers = await admin_api.post(
        f"{url}/alpha/redeploy",
        json={**reviewed, "expected_containers": ZERO_DIGEST},
    )
    orphans_unreviewed = await admin_api.post(
        f"{url}/alpha/redeploy", json={**reviewed, "remove_orphans": True}
    )
    stale_orphans = await admin_api.post(
        f"{url}/alpha/redeploy",
        json={**reviewed, "expected_containers": ZERO_DIGEST, "remove_orphans": True},
    )
    missing_digest = await admin_api.post(f"{url}/alpha/redeploy", json={})
    missing_inputs = await admin_api.post(
        f"{url}/alpha/redeploy", json={"expected_file_digest": digest}
    )
    async with world.app.state.engine.locks.hold(key, holder):
        busy = await admin_api.post(f"{url}/alpha/stop")

    assert preexisting.status_code == CONFLICT
    assert error_of(preexisting)["reason"] == "preexisting_objects"
    assert error_of(preexisting)["objects"] == ["Network delta_default"]
    assert [r.status_code for r in (stale_file, stale_containers, stale_orphans)] == [CONFLICT] * 3
    assert {error_of(r)["reason"] for r in (stale_file, stale_containers, stale_orphans)} == {
        "changed_since_review"
    }
    assert orphans_unreviewed.status_code == BAD_REQUEST
    assert error_of(orphans_unreviewed)["fields"][0]["loc"] == ["body", "expected_containers"]
    assert missing_digest.status_code == missing_inputs.status_code == UNPROCESSABLE
    assert busy.status_code == CONFLICT and error_of(busy)["reason"] == "object_busy"
    assert error_of(busy)["holder"]["holder"] == ADMIN_USERNAME
    assert ups(record) == deployed
    assert {box.state for box in stack_host.boxes.values() if "alpha" in box.name} == {"running"}
    assert (await admin_api.get(f"{url}/delta")).status_code == NOT_FOUND


async def test_an_unknown_stack_environment_or_id_is_refused_before_anything_runs(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    url = base(world)
    paths = ["nothere", "Alpha", "a b", "x" * 200]
    digest = {"expected_file_digest": ZERO_DIGEST, "expected_inputs_digest": None}

    statuses = {
        path: [
            (await admin_api.get(f"{url}/{path}")).status_code,
            (await admin_api.post(f"{url}/{path}/start")).status_code,
            (await admin_api.post(f"{url}/{path}/stop")).status_code,
            (await admin_api.post(f"{url}/{path}/redeploy", json=digest)).status_code,
        ]
        for path in paths
    }
    unknown_environment = await admin_api.get(base(world, UNKNOWN_ENVIRONMENT))
    invalid_environments = [
        (await admin_api.get(base(world, environment))).status_code
        for environment in (0, -1, BEYOND_INT64)
    ]
    bad_ranges = [
        (await admin_api.get(url, params=params)).status_code
        for params in ({"limit": 0}, {"limit": 501}, {"limit": "many"}, {"cursor": "x" * 513})
    ]
    bad_cursor = await admin_api.get(url, params={"cursor": "not-a-cursor"})

    assert statuses == {path: [NOT_FOUND] * 4 for path in paths}
    assert unknown_environment.status_code == NOT_FOUND
    assert invalid_environments == [UNPROCESSABLE] * 3
    assert bad_ranges == [UNPROCESSABLE] * 4
    assert bad_cursor.status_code == BAD_REQUEST
    assert bad_cursor.json()["error"]["fields"][0]["loc"] == ["query", "cursor"]
    assert stack_host.requests == []


async def test_a_missing_or_too_old_compose_is_a_503_naming_both_versions_before_anything_runs(
    admin_api: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    url = base(world)
    record = tmp_path / "calls.jsonl"
    await admin_api.post(url, json=stack("alpha", scripted(WEB_SERVICE, record=str(record))))
    deployed = len(recorded(record))
    compose = world.app.state.engine.compose
    digest = {"expected_file_digest": ZERO_DIGEST, "expected_inputs_digest": None}

    async def too_old() -> None:
        raise ComposeVersionUnsupported(INSTALLED, REQUIRED)

    async def missing() -> None:
        raise ComposeUnavailable

    monkeypatch.setattr(compose, "ready", too_old)
    old = [
        await admin_api.post(url, json=stack("beta")),
        await admin_api.post(f"{url}/alpha/start"),
        await admin_api.post(f"{url}/alpha/redeploy", json=digest),
    ]
    monkeypatch.setattr(compose, "ready", missing)
    absent = await admin_api.post(f"{url}/alpha/stop")

    assert [reply.status_code for reply in old] == [UNAVAILABLE] * 3
    assert [error_of(reply)["installed"] for reply in old] == [INSTALLED] * 3
    assert [error_of(reply)["required"] for reply in old] == [REQUIRED] * 3
    assert error_of(old[0])["reason"] == "compose_version_unsupported"
    assert absent.status_code == UNAVAILABLE and error_of(absent)["reason"] == "compose_unavailable"
    assert (error_of(absent)["installed"], error_of(absent)["required"]) == (None, None)
    assert len(recorded(record)) == deployed


async def test_a_run_past_its_deadline_is_an_unknown_outcome_and_frees_the_stack_lock(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    url = base(world)
    compose = world.app.state.engine.compose
    key = ObjectKey(world.remote_id, "stack", "slow")
    await use_compose_limits(compose, stack_host, operation_timeout=SHORT_DEADLINE, slots=4)
    created = await admin_api.post(url, json=stack("slow"))
    slow = (await admin_api.get(f"{url}/slow")).json()
    digest = slow["compose_file_digest"]

    async with asyncio.timeout(BOUNDED_SECONDS):
        hung = await admin_api.post(
            f"{url}/slow/redeploy",
            json={
                "expected_file_digest": digest,
                "expected_inputs_digest": slow["inputs_digest"],
                "compose_file": scripted(WEB_SERVICE, up="hang"),
            },
        )
    after = await admin_api.post(f"{url}/slow/stop")

    assert created.status_code == CREATED
    assert hung.status_code == UNAVAILABLE and error_of(hung)["reason"] == "outcome_unknown"
    assert str(world.app.state.settings.data_dir) not in hung.text
    assert world.app.state.engine.locks.holder_of(key) is None
    assert after.status_code == OK


async def test_a_mutation_that_finds_no_compose_slot_gives_up_within_its_deadline(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    url = base(world)
    compose = world.app.state.engine.compose
    key = ObjectKey(world.remote_id, "stack", "alpha")
    cli = await use_compose_limits(compose, stack_host, operation_timeout=1.0, slots=1)
    await admin_api.post(url, json=stack("alpha"))

    async with cli.slot(None):
        async with asyncio.timeout(BOUNDED_SECONDS):
            starved = await admin_api.post(f"{url}/alpha/stop")
    after = await admin_api.post(f"{url}/alpha/stop")

    assert starved.status_code == UNAVAILABLE and error_of(starved)["reason"] == "compose_busy"
    assert world.app.state.engine.locks.holder_of(key) is None
    assert after.status_code == OK


async def test_json_operations_count_against_the_job_caps_and_a_refused_one_runs_nothing(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    url = base(world)
    record = tmp_path / "calls.jsonl"
    registry = world.app.state.engine.stack_jobs
    administrator = Principal(ADMIN_ID, ADMIN_USERNAME, "administrator")
    others = [Principal(HOLDER_PROCESS + n, f"holder-{n}", "standard") for n in range(4)]
    await admin_api.post(url, json=stack("alpha", scripted(WEB_SERVICE, record=str(record))))
    deployed = len(recorded(record))
    digest = {"expected_file_digest": ZERO_DIGEST, "expected_inputs_digest": None}

    async def attempts() -> list[Any]:
        return [
            await admin_api.post(url, json=stack("beta")),
            await admin_api.post(f"{url}/alpha/redeploy", json=digest),
            await admin_api.post(f"{url}/alpha/start"),
            await admin_api.post(f"{url}/alpha/stop"),
        ]

    with registry.reserve(administrator), registry.reserve(administrator):
        per_user = await attempts()
    with ExitStack() as held:
        for other in others:
            held.enter_context(registry.reserve(other))
        per_process = await attempts()
    ran_during_refusals = len(recorded(record))
    released = await admin_api.post(f"{url}/alpha/stop")

    assert [r.status_code for r in per_user + per_process] == [TOO_MANY_REQUESTS] * 8
    assert {error_of(r)["reason"] for r in per_user + per_process} == {"too_many_operations"}
    assert "You have 2 stack operations running" in error_of(per_user[0])["message"]
    assert "as many stack operations as it allows" in error_of(per_process[0])["message"]
    assert (await admin_api.get(f"{url}/beta")).status_code == NOT_FOUND
    assert ran_during_refusals == deployed
    assert released.status_code == OK


async def create_over_a_host_list_limit(
    caller: AsyncClient,
    world: World,
    host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
    path: str,
) -> Any:
    with monkeypatch.context() as patch:
        patch.setattr(resources, limit, TINY_CAP)
        host.route("GET", path, Reply(200, b"[]", OVER_THE_CAP))
        reply = await caller.post(base(world), json=stack("alpha"))
    host.clear_faults()
    return reply


async def test_a_host_list_over_its_read_limit_is_a_503_naming_the_limit(
    admin_api: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = base(world)

    containers = await create_over_a_host_list_limit(
        admin_api, world, stack_host, monkeypatch, "CONTAINER_LIST_BYTES", "/containers/json"
    )
    networks = await create_over_a_host_list_limit(
        admin_api, world, stack_host, monkeypatch, "NETWORK_LIST_BYTES", "/networks"
    )
    volumes = await create_over_a_host_list_limit(
        admin_api, world, stack_host, monkeypatch, "VOLUME_LIST_BYTES", "/volumes"
    )

    async def too_large(*args: Any, **kwargs: Any) -> None:
        raise ObjectTooLarge

    monkeypatch.setattr(StackService, "start", too_large)
    object_reply = await admin_api.post(f"{url}/alpha/start")

    assert [r.status_code for r in (containers, networks, volumes)] == [UNAVAILABLE] * 3
    assert all("limit" in error_of(r)["message"] for r in (containers, networks, volumes))
    assert object_reply.status_code == UNAVAILABLE
    assert (await admin_api.get(url)).json()["items"] == []


PRIVILEGED = f"services:\n  w:\n    image: {IMAGE}\n    privileged: true\n"


async def grant_everyone(world: World, environment_id: int, name: str, level: str) -> None:
    async with world.app.state.database.session() as db:
        await db.execute(
            "UPDATE resource_control SET is_public = 1, is_administrators_only = 0, "
            "access_level = ? WHERE environment_id = ? AND resource_type = 'stack' "
            "AND resource_id = ?",
            (level, environment_id, name),
        )


async def test_a_standard_user_works_only_in_their_own_stacks_and_never_on_a_local_socket(
    user_api: AsyncClient, admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    url = base(world)
    digest = {"expected_file_digest": ZERO_DIGEST, "expected_inputs_digest": None}
    await admin_api.post(url, json=stack("admins"))
    await admin_api.post(base(world, world.local_id), json=stack("onsocket"))
    await grant_everyone(world, world.local_id, "onsocket", "write")

    on_socket_create = await user_api.post(base(world, world.local_id), json=stack("mine"))
    on_socket_read = await user_api.get(f"{base(world, world.local_id)}/onsocket")
    on_socket_writes = [
        await user_api.post(f"{base(world, world.local_id)}/onsocket/start"),
        await user_api.post(f"{base(world, world.local_id)}/onsocket/stop"),
        await user_api.post(f"{base(world, world.local_id)}/onsocket/redeploy", json=digest),
    ]
    restricted = await user_api.post(url, json=stack("mine", PRIVILEGED))
    mine = await user_api.post(url, json=stack("mine"))
    mine_detail = (await user_api.get(f"{url}/mine")).json()
    own_privileged = await user_api.post(
        f"{url}/mine/redeploy",
        json={
            "expected_file_digest": mine_detail["compose_file_digest"],
            "expected_inputs_digest": mine_detail["inputs_digest"],
            "compose_file": PRIVILEGED,
        },
    )
    own_stop = await user_api.post(f"{url}/mine/stop")
    own_start = await user_api.post(f"{url}/mine/start")
    listed = await user_api.get(url)
    hidden = [
        await user_api.get(f"{url}/admins"),
        await user_api.post(f"{url}/admins/start"),
        await user_api.post(f"{url}/admins/stop"),
        await user_api.post(f"{url}/admins/redeploy", json=digest),
    ]
    unknown = [
        await user_api.get(f"{url}/nothere"),
        await user_api.post(f"{url}/nothere/start"),
        await user_api.post(f"{url}/nothere/stop"),
        await user_api.post(f"{url}/nothere/redeploy", json=digest),
    ]

    assert on_socket_create.status_code == FORBIDDEN
    assert on_socket_read.status_code == OK and on_socket_read.json()["can_write"] is False
    assert [r.status_code for r in on_socket_writes] == [FORBIDDEN] * 3
    assert restricted.status_code == FORBIDDEN
    assert error_of(restricted)["reason"] == "restricted"
    assert error_of(restricted)["findings"][0]["path"] == "services.w.privileged"
    assert error_of(restricted)["fields"][0]["loc"][0] == "compose"
    assert mine.status_code == CREATED
    assert error_of(own_privileged)["reason"] == "restricted"
    assert own_stop.status_code == own_start.status_code == OK
    assert [item["name"] for item in listed.json()["items"]] == ["mine"]
    assert [r.status_code for r in hidden] == [NOT_FOUND] * 4
    assert [r.json() for r in hidden] == [r.json() for r in unknown]


async def test_a_read_grant_lets_a_standard_user_see_a_stack_and_a_write_grant_operate_it(
    user_api: AsyncClient, admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    url = base(world)
    await admin_api.post(url, json=stack("readonly"))
    await admin_api.post(url, json=stack("writable"))
    await grant_everyone(world, world.remote_id, "readonly", "read")
    await grant_everyone(world, world.remote_id, "writable", "write")

    listed = await user_api.get(url)
    seen = await user_api.get(f"{url}/readonly")
    digest = {
        "expected_file_digest": seen.json()["compose_file_digest"],
        "expected_inputs_digest": seen.json()["inputs_digest"],
    }
    refused = [
        await user_api.post(f"{url}/readonly/start"),
        await user_api.post(f"{url}/readonly/stop"),
        await user_api.post(f"{url}/readonly/redeploy", json=digest),
    ]
    writable = (await user_api.get(f"{url}/writable")).json()
    stopped = await user_api.post(f"{url}/writable/stop")
    started = await user_api.post(f"{url}/writable/start")
    redeployed = await user_api.post(
        f"{url}/writable/redeploy",
        json={
            "expected_file_digest": writable["compose_file_digest"],
            "expected_inputs_digest": writable["inputs_digest"],
        },
    )

    assert [item["name"] for item in listed.json()["items"]] == ["readonly", "writable"]
    assert seen.status_code == OK and seen.json()["can_write"] is False
    assert [r.status_code for r in refused] == [FORBIDDEN] * 3
    assert writable["can_write"] is True
    assert [r.status_code for r in (stopped, started, redeployed)] == [OK] * 3
