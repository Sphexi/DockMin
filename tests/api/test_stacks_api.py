"""The stack JSON routes: bearer-only access, the lifecycle, request limits and hostile input."""

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from app.api.stacks import MAX_BODY_BYTES
from app.services.stack_views import containers_digest
from tests.auth_support import Browsers, csrf_headers
from tests.docker_engine.container_engine import ContainerEngine
from tests.stack_support import install_scripted_compose
from tests.web.environment_support import World, bearer, issue_token, rows

OK, CREATED, BAD_REQUEST, UNAUTHENTICATED, FORBIDDEN, NOT_FOUND = 200, 201, 400, 401, 403, 404
CONFLICT, BAD_GATEWAY = 409, 502
PAYLOAD_TOO_LARGE, UNPROCESSABLE = 413, 422
JSON_HEADERS = {"Content-Type": "application/json"}
IMAGE = "nginx:1"
SECRET = "SECRET_SENTINEL_VALUE"
SCRIPTED_TOKEN = "hunter2secretvalue"
SCRIPTED_FAILURE = "port is already allocated"
SCRIPTED_EXIT_CODE = 3
GREETING = "hi"
ZERO_DIGEST = "0" * 64
HOST_PROJECT = "shop"
LONE_SURROGATE = "\\ud800"
LOWER_SURROGATE = "x\\udc80y"

WEB_SERVICE = (
    "services:\n"
    "  web:\n"
    f"    image: {IMAGE}\n"
    "    environment:\n"
    "      GREETING: ${GREETING:-hello}\n"
    "      TOKEN: ${TOKEN:-none}\n"
)
CACHE_SERVICE = f"  cache:\n    image: {IMAGE}\n"
GREETING_VARIABLE = {"name": "GREETING", "value": GREETING}
SECRET_VARIABLE = {"name": "TOKEN", "value": SECRET, "secret": True}
KEPT_SECRET_VARIABLE = {"name": "TOKEN", "secret": True}
OUTCOME_KEYS = {
    "operation",
    "message",
    "status",
    "events",
    "exit_code",
    "pulled",
    "services",
    "services_running",
    "orphans_removed",
    "preexisting_after_deploy",
    "interrupted",
}


def base(world: World, environment_id: int | None = None) -> str:
    chosen = world.remote_id if environment_id is None else environment_id
    return f"/api/v1/environments/{chosen}/stacks"


def scripted(text: str, **script: str) -> str:
    lines = "".join(f"  {key}: {value}\n" for key, value in script.items())
    return f"{text}x-fake:\n{lines}"


def stack(name: str, text: str = WEB_SERVICE, **more: Any) -> dict[str, Any]:
    return {"name": name, "compose_file": text} | more


def recorded(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def last_up_environment(path: Path) -> dict[str, str]:
    env: dict[str, str] = [entry for entry in recorded(path) if entry["cmd"] == "up"][-1]["env"]
    return env


def container_ids(detail: dict[str, Any]) -> list[str]:
    return [
        container["id"] for service in detail["services"] for container in service["containers"]
    ]


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


async def test_only_a_valid_bearer_token_reaches_a_stack_route_and_the_host_is_not_called(
    browsers: Browsers, admin_client: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    anonymous = browsers.open()
    forged = browsers.open()
    forged.headers.update(bearer("dkm_notatoken"))
    url = f"{base(world)}/alpha"
    reads = [base(world), url]
    writes = [
        (base(world), stack("alpha")),
        (f"{url}/redeploy", {"expected_file_digest": ZERO_DIGEST, "expected_inputs_digest": None}),
        (f"{url}/start", None),
        (f"{url}/stop", None),
    ]

    anonymous_reads = [(await anonymous.get(path)).status_code for path in reads]
    anonymous_writes = [
        (await anonymous.post(path, json=data)).status_code for path, data in writes
    ]
    forged_statuses = [(await forged.get(path)).status_code for path in reads] + [
        (await forged.post(path, json=data)).status_code for path, data in writes
    ]
    session_reads = [(await admin_client.get(path)).status_code for path in reads]
    headers = csrf_headers(admin_client)
    session_with_csrf = [
        (await admin_client.post(path, json=data, headers=headers)).status_code
        for path, data in writes
    ]
    session_without_csrf = [
        (await admin_client.post(path, json=data)).status_code for path, data in writes
    ]

    assert anonymous_reads == [UNAUTHENTICATED] * len(reads)
    assert anonymous_writes == [FORBIDDEN] * len(writes)
    assert forged_statuses == [UNAUTHENTICATED] * (len(reads) + len(writes))
    assert session_reads == [UNAUTHENTICATED] * len(reads)
    assert session_with_csrf == [UNAUTHENTICATED] * len(writes)
    assert session_without_csrf == [FORBIDDEN] * len(writes)
    assert stack_host.requests == []


async def test_an_administrator_runs_a_stack_through_create_list_get_redeploy_stop_and_start(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    url = base(world)
    record = tmp_path / "calls.jsonl"
    text = scripted(WEB_SERVICE + CACHE_SERVICE, record=str(record))
    changed = scripted(WEB_SERVICE.replace("hello", "bonjour"), record=str(record))

    created = await admin_api.post(
        url, json=stack("alpha", text, variables=[GREETING_VARIABLE, SECRET_VARIABLE])
    )
    await admin_api.post(url, json=stack("beta"))
    first_page = (await admin_api.get(url, params={"limit": 1})).json()
    second_page = (
        await admin_api.get(url, params={"limit": 1, "cursor": first_page["next_cursor"]})
    ).json()
    filtered = (await admin_api.get(url, params={"q": "BET"})).json()
    detail = (await admin_api.get(f"{url}/alpha")).json()

    assert created.status_code == CREATED
    assert set(created.json()) == {"name", "outcome", "warnings"}
    assert created.json()["name"] == "alpha"
    assert set(created.json()["outcome"]) == OUTCOME_KEYS
    assert created.json()["outcome"]["operation"] == "deploy"
    assert created.json()["outcome"]["services_running"] == 2
    assert [item["name"] for item in first_page["items"]] == ["alpha"]
    assert [item["name"] for item in second_page["items"]] == ["beta"]
    assert (first_page["next_cursor"] is None, second_page["next_cursor"] is None) == (False, True)
    assert [item["name"] for item in filtered["items"]] == ["beta"]
    assert (detail["origin"], detail["status"]) == ("created", "active")
    assert detail["compose_file"] == text
    assert {v["name"]: v for v in detail["variables"]} == {
        "GREETING": {"name": "GREETING", "secret": False, "value": GREETING},
        "TOKEN": {"name": "TOKEN", "secret": True, "value": None},
    }
    assert SECRET not in created.text + json.dumps(detail)
    assert last_up_environment(record)["TOKEN"] == SECRET

    stopped = await admin_api.post(f"{url}/alpha/stop")
    stopped_detail = (await admin_api.get(f"{url}/alpha")).json()
    started = await admin_api.post(f"{url}/alpha/start")

    assert stopped.status_code == started.status_code == OK
    assert set(stopped.json()["outcome"]) == OUTCOME_KEYS
    assert stopped.json()["outcome"]["operation"] == "stop"
    assert {c["state"] for s in stopped_detail["services"] for c in s["containers"]} == {"exited"}
    assert started.json()["outcome"]["operation"] == "start"
    assert started.json()["outcome"]["services_running"] == 2

    kept = await admin_api.post(
        f"{url}/alpha/redeploy",
        json={
            "expected_file_digest": detail["compose_file_digest"],
            "expected_inputs_digest": detail["inputs_digest"],
            "pull": True,
        },
    )
    unchanged = (await admin_api.get(f"{url}/alpha")).json()

    assert kept.status_code == OK and kept.json()["outcome"]["operation"] == "redeploy"
    assert unchanged["compose_file"] == text
    assert unchanged["variables"] == detail["variables"]
    assert last_up_environment(record)["TOKEN"] == SECRET

    ids = container_ids(unchanged)
    replaced = await admin_api.post(
        f"{url}/alpha/redeploy",
        json={
            "expected_file_digest": detail["compose_file_digest"],
            "expected_inputs_digest": detail["inputs_digest"],
            "expected_containers": containers_digest(ids),
            "remove_orphans": True,
            "compose_file": changed,
            "variables": [KEPT_SECRET_VARIABLE],
        },
    )
    after = (await admin_api.get(f"{url}/alpha")).json()

    assert replaced.status_code == OK and replaced.json()["outcome"]["orphans_removed"] == 1
    assert after["compose_file"] == changed
    assert after["compose_file_digest"] != detail["compose_file_digest"]
    assert after["variables"] == [{"name": "TOKEN", "secret": True, "value": None}]
    assert last_up_environment(record)["TOKEN"] == SECRET
    assert [s["name"] for s in after["services"]] == ["web"]
    assert SECRET not in replaced.text + json.dumps(after)
    actions = await rows(
        world, "SELECT action, result, target_id FROM activity_event WHERE target_type = 'stack'"
    )
    alpha_actions = [
        (row["action"], row["result"]) for row in actions if row["target_id"] == "alpha"
    ]
    assert alpha_actions == [
        ("stack_created", "success"),
        ("stack_deployed", "success"),
        ("stack_stopped", "success"),
        ("stack_started", "success"),
        ("stack_redeployed", "success"),
        ("stack_redeployed", "success"),
    ]


def refused_bodies() -> dict[str, tuple[dict[str, Any], int]]:
    refused_construct = f"services:\n  w:\n    image: {IMAGE}\n    env_file: /etc/passwd\n"
    many = [{"name": f"V{number}", "value": "x"} for number in range(101)]
    return {
        "unknown field": (stack("alpha", extra=1), UNPROCESSABLE),
        "unknown variable field": (
            stack("alpha", variables=[{"name": "A", "value": "b", "extra": 1}]),
            UNPROCESSABLE,
        ),
        "plain variable without a value": (
            stack("alpha", variables=[{"name": "A"}]),
            UNPROCESSABLE,
        ),
        "too many variables": (stack("alpha", variables=many), UNPROCESSABLE),
        "invalid name": (stack("Bad Name"), BAD_REQUEST),
        "secret without a value": (
            stack("alpha", variables=[{"name": "A", "secret": True}]),
            BAD_REQUEST,
        ),
        "reserved variable": (
            stack("alpha", variables=[{"name": "DOCKER_HOST", "value": "x"}]),
            BAD_REQUEST,
        ),
        "not YAML": (stack("alpha", "a: ["), BAD_REQUEST),
        "empty file": (stack("alpha", ""), BAD_REQUEST),
        "refused construct": (stack("alpha", refused_construct), BAD_REQUEST),
    }


async def test_a_create_the_api_refuses_gets_its_status_and_changes_nothing(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    cases = refused_bodies()

    replies = {
        label: await admin_api.post(base(world), json=data) for label, (data, _) in cases.items()
    }
    not_json = await admin_api.post(base(world), content=b"nope", headers=JSON_HEADERS)
    form = await admin_api.post(base(world), data={"name": "alpha"})
    listing = await admin_api.get(base(world))

    assert {label: reply.status_code for label, reply in replies.items()} == {
        label: status for label, (_, status) in cases.items()
    }
    assert (not_json.status_code, form.status_code) == (UNPROCESSABLE, UNPROCESSABLE)
    assert replies["invalid name"].json()["error"]["fields"][0]["loc"] == ["body", "name"]
    assert replies["secret without a value"].json()["error"]["fields"][0]["loc"] == [
        "body",
        "variables",
        0,
        "value",
    ]
    assert replies["refused construct"].json()["error"]["reason"] == "refused_construct"
    assert replies["not YAML"].json()["error"]["reason"] == "invalid_compose"
    assert listing.json()["items"] == []
    assert stack_host.mutations() == []


async def test_a_failing_deployment_leaves_an_error_stack_and_surfaces_only_scrubbed_stderr(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    url = base(world)
    data_dir = str(world.app.state.settings.data_dir)
    unset_variable = f"services:\n  w:\n    image: {IMAGE}\n    command: ${{REQUIRED?needed}}\n"

    invalid = await admin_api.post(url, json=stack("alpha", unset_variable))
    failed = await admin_api.post(
        url, json=stack("alpha", scripted(WEB_SERVICE, up="fail"), variables=[SECRET_VARIABLE])
    )
    detail = await admin_api.get(f"{url}/alpha")
    taken = await admin_api.post(url, json=stack("alpha"))
    host_taken = await admin_api.post(url, json=stack(HOST_PROJECT))
    not_deployed = await admin_api.post(f"{url}/alpha/start")
    failures = await rows(
        world,
        "SELECT action, result FROM activity_event "
        "WHERE target_id = 'alpha' AND result = 'failure'",
    )

    assert invalid.status_code == BAD_REQUEST
    assert invalid.json()["error"]["reason"] == "invalid_compose"
    assert "REQUIRED" in invalid.json()["error"]["stderr"]
    assert failed.status_code == BAD_GATEWAY
    error = failed.json()["error"]
    assert (error["reason"], error["exit_code"], error["stack_created"], error["stack"]) == (
        "compose_failed",
        SCRIPTED_EXIT_CODE,
        True,
        "alpha",
    )
    assert error["events"] >= 1 and SCRIPTED_FAILURE in error["stderr"]
    for leaked in (SECRET, SCRIPTED_TOKEN, data_dir):
        assert leaked not in invalid.text + failed.text
    assert detail.json()["status"] == "error"
    assert (taken.status_code, taken.json()["error"]["reason"]) == (CONFLICT, "name_in_use")
    assert (host_taken.status_code, host_taken.json()["error"]["reason"]) == (
        CONFLICT,
        "name_in_use",
    )
    assert (not_deployed.status_code, not_deployed.json()["error"]["reason"]) == (
        CONFLICT,
        "not_deployed",
    )
    assert [row["action"] for row in failures] == [
        "stack_created",
        "stack_deployed",
        "stack_created",
        "stack_started",
    ]


def json_body_of_size(size: int) -> bytes:
    skeleton = len(json.dumps({"name": "cap", "compose_file": ""}))
    return json.dumps({"name": "cap", "compose_file": "x" * (size - skeleton)}).encode("utf-8")


async def test_a_body_over_256_kib_is_refused_declared_or_chunked_and_one_at_the_cap_is_read(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    url = base(world)
    at_cap = json_body_of_size(MAX_BODY_BYTES)
    over_cap = json_body_of_size(MAX_BODY_BYTES + 1)

    async def chunks(body: bytes) -> Any:
        for start in range(0, len(body), 8192):
            yield body[start : start + 8192]

    declared = await admin_api.post(url, content=over_cap, headers=JSON_HEADERS)
    chunked = await admin_api.post(url, content=chunks(over_cap), headers=JSON_HEADERS)
    chunked_at_cap = await admin_api.post(url, content=chunks(at_cap), headers=JSON_HEADERS)
    declared_at_cap = await admin_api.post(url, content=at_cap, headers=JSON_HEADERS)
    on_action = await admin_api.post(f"{url}/alpha/start", content=over_cap)

    assert (declared.status_code, chunked.status_code) == (PAYLOAD_TOO_LARGE, PAYLOAD_TOO_LARGE)
    assert on_action.status_code == PAYLOAD_TOO_LARGE
    assert declared.json()["error"]["status"] == PAYLOAD_TOO_LARGE
    assert (chunked_at_cap.status_code, declared_at_cap.status_code) == (BAD_REQUEST,) * 2
    assert chunked_at_cap.json()["error"]["reason"] == "too_large"
    assert stack_host.mutations() == []


def hostile_values(canary: Path) -> dict[str, str]:
    return {
        "SUBSHELL": f"$(touch {canary})",
        "BACKTICKS": f"`touch {canary}`",
        "SEQUENCE": f"x; touch {canary} && touch {canary} | cat > {canary}",
        "QUOTES": "\"double\" 'single' \\ back ${HOME} $HOME",
        "REDIRECT": f"> {canary} < {canary}",
    }


HOSTILE_NAMES = [
    "a;touch-canary",
    "$(touch-canary)",
    "`touch-canary`",
    "a b",
    "a&&b",
    "../escape",
    "a|b",
]


async def test_shell_metacharacters_in_a_name_are_refused_and_elsewhere_reach_compose_verbatim(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine, tmp_path: Path
) -> None:
    url = base(world)
    canary = tmp_path / "canary"
    record = tmp_path / "calls.jsonl"
    values = hostile_values(canary)
    text = scripted(
        f"services:\n  web:\n    image: {IMAGE}\n"
        f'    command: ["sh", "-c", "touch {canary}; $$(touch {canary}); `touch {canary}`"]\n',
        record=str(record),
    )
    variables = [{"name": name, "value": value} for name, value in values.items()]

    named = [
        (await admin_api.post(url, json=stack(name, text))).status_code for name in HOSTILE_NAMES
    ]
    listed_before = (await admin_api.get(url)).json()["items"]
    ran_for_names = record.exists()
    created = await admin_api.post(url, json=stack("hostile", text, variables=variables))
    detail = (await admin_api.get(f"{url}/hostile")).json()

    assert named == [BAD_REQUEST] * len(HOSTILE_NAMES)
    assert listed_before == [] and not ran_for_names
    assert created.status_code == CREATED
    assert detail["compose_file"] == text
    assert {v["name"]: v["value"] for v in detail["variables"]} == values
    environment = last_up_environment(record)
    assert {name: environment[name] for name in values} == values
    assert not canary.exists()
    command_lines = " ".join(arg for entry in recorded(record) for arg in entry["argv"])
    assert all(value not in command_lines for value in values.values())


async def test_a_lone_surrogate_in_the_json_text_is_a_400_and_never_a_500(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    url = base(world)
    in_file = (
        '{"name": "surr", "compose_file": '
        f'"services:\\n  w:\\n    image: {IMAGE}\\n# {LONE_SURROGATE}\\n"}}'
    )
    in_variable = (
        '{"name": "surv", "compose_file": '
        f'"services:\\n  w:\\n    image: {IMAGE}\\n", '
        f'"variables": [{{"name": "A", "value": "{LOWER_SURROGATE}", "secret": true}}]}}'
    )

    file_reply = await admin_api.post(url, content=in_file, headers=JSON_HEADERS)
    variable_reply = await admin_api.post(url, content=in_variable, headers=JSON_HEADERS)
    listing = await admin_api.get(url)

    assert (file_reply.status_code, variable_reply.status_code) == (BAD_REQUEST,) * 2
    assert listing.json()["items"] == []


async def test_the_administrator_list_does_not_read_host_containers_for_unmanaged_projects(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    listing = await admin_api.get(base(world))

    assert listing.status_code == OK and listing.json()["items"] == []
    assert stack_host.requests == []


async def test_the_json_routes_list_and_return_compose_stacks_only_and_a_swarm_name_is_not_found(
    admin_api: AsyncClient, world: World, stack_host: ContainerEngine
) -> None:
    url = base(world)
    await admin_api.post(url, json=stack("alpha"))
    async with world.app.state.database.session() as db:
        await db.execute(
            "INSERT INTO stack (environment_id, name, kind, origin, project_directory, "
            "compose_file, compose_file_digest, created_at, updated_at) "
            "VALUES (?, 'Swarmed', 'swarm', 'created', ?, 'services: {}', ?, "
            "'2026-01-02T03:04:05Z', '2026-01-02T03:04:05Z')",
            (world.remote_id, "c" * 32, ZERO_DIGEST),
        )

    listing = await admin_api.get(url)
    detail = await admin_api.get(f"{url}/Swarmed")

    assert [item["name"] for item in listing.json()["items"]] == ["alpha"]
    assert detail.status_code == NOT_FOUND
