"""GET and POST under /api/v1/environments/{id}/containers: bearer-only and the same services."""

from typing import Any

import pytest
from httpx import AsyncClient

import app.docker_engine.resources as resources
from app.services.activity import UserActor
from app.services.container_recreate import ContainerRecreateService
from app.services.image_bindings import REBIND_REFUSED, RebindRefused
from app.services.object_locks import LockHolder, ObjectKey
from tests.auth_support import ADMIN_ID, ADMIN_USERNAME, STANDARD_USERNAME, Browsers, csrf_headers
from tests.container_support import (
    ABSENT,
    API,
    BIND_SOURCE,
    CACHE,
    DB,
    JOB,
    SECRET_ENV_VALUE,
    SECRET_LABEL_VALUE,
    WEB,
    plant_control,
)
from tests.docker_engine.container_engine import ENGINE_TEXT, ContainerEngine, created_id
from tests.docker_engine.fake_engine import Refuse, Reply, Stall
from tests.image_support import binding_rows, decode_auth, mark_missing
from tests.provisioning_support import IMAGE, OLD, seed_recreatable
from tests.registry_support import bind_image
from tests.support import present
from tests.web.environment_support import REMOTE_NAME, World, bearer, issue_token
from tests.web.registry_support import USERNAME, create_set, custom_form, registry_form

OK, BAD_REQUEST, UNAUTHENTICATED, FORBIDDEN, NOT_FOUND, CONFLICT = 200, 400, 401, 403, 404, 409
UNPROCESSABLE, BAD_GATEWAY, UNAVAILABLE, METHOD_NOT_ALLOWED = 422, 502, 503, 405
BEYOND_INT64 = 2**63
OVER_THE_CAP = {"Content-Length": "999"}
PORT_80 = {"private_port": 80, "public_port": 8080, "protocol": "tcp", "ip": None}


def base(world: World, environment_id: int | None = None) -> str:
    chosen = world.remote_id if environment_id is None else environment_id
    return f"/api/v1/environments/{chosen}/containers"


def names(body: dict[str, Any]) -> list[str]:
    return [item["name"] for item in body["items"]]


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


async def test_only_a_valid_bearer_token_is_accepted_not_a_session_and_the_host_is_not_called(
    browsers: Browsers,
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
) -> None:
    anonymous = browsers.open()
    forged = browsers.open()
    forged.headers.update(bearer("dkm_notatoken"))
    paths = [base(world), f"{base(world)}/{WEB}"]

    statuses = [
        (await client.get(path)).status_code
        for client in (anonymous, admin_client, forged)
        for path in paths
    ]
    start = f"{base(world)}/{WEB}/start"
    posted = await admin_client.post(start, headers=csrf_headers(admin_client))

    assert set(statuses) == {UNAUTHENTICATED}
    assert posted.status_code == UNAUTHENTICATED
    assert container_host.requests == []


async def test_an_administrator_lists_every_container_by_name_with_labels_and_pages_through_them(
    admin_api: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    first = (await admin_api.get(base(world), params={"limit": 4})).json()
    second = (
        await admin_api.get(base(world), params={"limit": 4, "cursor": first["next_cursor"]})
    ).json()

    assert names(first) == ["cache", "db", "job", "warm"]
    assert names(second) == ["web", "worker"] and second["next_cursor"] is None
    assert (first["total"], second["total"]) == (6, 6)
    web = second["items"][0]
    assert web["labels"] == {"com.docker.compose.project": "shop", "tier": "front"}
    assert web["ports"] == [
        {"private_port": 80, "public_port": 8080, "protocol": "tcp", "ip": None}
    ]
    assert (web["stack"], web["state"], web["health"]) == ("shop", "running", None)
    assert web["created_at"].startswith("2023-11-14")
    assert {item["name"]: item["health"] for item in first["items"]}["db"] == "unhealthy"
    assert "engine-status-text" not in str(first)


FILTERS: dict[str, tuple[dict[str, Any], list[str]]] = {
    "running": ({"state": "running"}, ["db", "warm", "web"]),
    "stopped": ({"state": "stopped"}, ["job"]),
    "unhealthy": ({"health": "unhealthy"}, ["db"]),
    "name": ({"q": "WO"}, ["worker"]),
    "image": ({"image": "REDIS"}, ["cache"]),
    "label": ({"label": "tier=front"}, ["web"]),
    "labels": ({"label": ["tier", "com.docker.compose.project=shop"]}, ["web"]),
    "stack": ({"stack": "SHOP"}, ["db", "web"]),
}


async def test_each_filter_narrows_the_list(
    admin_api: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    found = {
        label: names((await admin_api.get(base(world), params=params)).json())
        for label, (params, _) in FILTERS.items()
    }

    assert found == {label: expected for label, (_, expected) in FILTERS.items()}


async def test_a_standard_token_sees_only_permitted_containers_with_secret_labels_redacted(
    user_api: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    shared_host.boxes[DB].labels["db_password"] = SECRET_LABEL_VALUE

    listing = await user_api.get(base(world))

    assert names(listing.json()) == ["api", "db", "job"]
    assert listing.json()["total"] == 3
    assert SECRET_LABEL_VALUE not in listing.text


REFUSED_BY_THE_SERVICE: dict[str, dict[str, str]] = {
    "state": {"state": "bogus"},
    "health": {"health": "healthy"},
    "cursor": {"cursor": "not-a-cursor"},
    "label": {"label": "=value"},
}
OUT_OF_RANGE: list[dict[str, Any]] = [
    {"limit": 0},
    {"limit": 501},
    {"limit": "many"},
    {"q": "x" * 65},
    {"label": ["a"] * 6},
    {"cursor": "x" * 513},
]


NOT_THE_FULL_ID = ["web", "WEB", WEB[:12], WEB.upper(), WEB + "0", "../x", WEB[1:]]
BAD_ENVIRONMENT_IDS = [0, -1, BEYOND_INT64]


async def full_id_statuses(client: AsyncClient, world: World) -> list[int]:
    paths = [f"{base(world)}/{reference}" for reference in NOT_THE_FULL_ID]
    return [
        (await call(path)).status_code
        for path in paths
        for call in (
            client.get,
            lambda p: client.post(f"{p}/start"),
            lambda p: client.post(f"{p}/stop"),
            lambda p: client.post(f"{p}/restart"),
        )
    ]


async def test_a_request_the_api_refuses_gets_the_right_status_and_never_reaches_the_host(
    admin_api: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    refused_by_service = {
        field: await admin_api.get(base(world), params=params)
        for field, params in REFUSED_BY_THE_SERVICE.items()
    }
    out_of_range = [
        (await admin_api.get(base(world), params=params)).status_code for params in OUT_OF_RANGE
    ]
    bad_environments = [
        (await admin_api.get(base(world, i))).status_code for i in BAD_ENVIRONMENT_IDS
    ]
    unknown_environment = await admin_api.get(base(world, 99999))
    not_full_ids = await full_id_statuses(admin_api, world)

    assert {f: r.status_code for f, r in refused_by_service.items()} == dict.fromkeys(
        REFUSED_BY_THE_SERVICE, BAD_REQUEST
    )
    assert {f: r.json()["error"]["fields"][0]["loc"] for f, r in refused_by_service.items()} == {
        field: ["query", field] for field in REFUSED_BY_THE_SERVICE
    }
    assert out_of_range == [UNPROCESSABLE] * len(OUT_OF_RANGE)
    assert bad_environments == [UNPROCESSABLE] * len(BAD_ENVIRONMENT_IDS)
    assert unknown_environment.status_code == NOT_FOUND
    assert set(not_full_ids) == {NOT_FOUND}
    assert container_host.requests == []


async def test_the_detail_shows_an_administrator_bind_paths_and_a_standard_token_none(
    admin_api: AsyncClient, user_api: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    box = shared_host.boxes[DB]
    box.env = [f"DB_PASSWORD={SECRET_ENV_VALUE}"]
    box.mounts = [{"Type": "bind", "Source": BIND_SOURCE, "Destination": "/srv", "RW": False}]
    box.labels["db_password"] = SECRET_LABEL_VALUE
    box.created_text = "not a date"

    by_admin = await admin_api.get(f"{base(world)}/{DB}")
    by_user = await user_api.get(f"{base(world)}/{DB}")
    hidden = await user_api.get(f"{base(world)}/{CACHE}")

    assert by_admin.json()["mounts"][0]["source"] == BIND_SOURCE
    assert by_admin.json()["labels"]["db_password"] == SECRET_LABEL_VALUE
    assert by_admin.json()["created_at"] is None
    assert by_user.status_code == OK
    assert by_user.json()["mounts"][0]["source"] is None
    assert SECRET_LABEL_VALUE not in by_user.text and BIND_SOURCE not in by_user.text
    assert SECRET_ENV_VALUE not in by_admin.text + by_user.text
    assert by_user.json()["networks"][0]["name"] == "bridge"
    assert hidden.status_code == NOT_FOUND


async def test_start_stop_and_restart_report_the_result_and_the_state_after_and_are_audited(
    admin_api: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    path = f"{base(world)}/{JOB}"

    started = await admin_api.post(f"{path}/start")
    again = await admin_api.post(f"{path}/start")
    stopped = await admin_api.post(f"{path}/stop")
    restarted = await admin_api.post(f"{path}/restart")
    unhealthy = await admin_api.post(f"{base(world)}/{DB}/restart")

    assert started.json() == {
        "action": "start",
        "result": "done",
        "id": JOB,
        "name": "job",
        "state": "running",
        "health": None,
    }
    assert (again.json()["result"], again.json()["state"]) == ("unchanged", "running")
    assert (stopped.json()["result"], stopped.json()["state"]) == ("done", "exited")
    assert (restarted.json()["action"], restarted.json()["state"]) == ("restart", "running")
    assert unhealthy.json()["health"] == "unhealthy"
    async with world.app.state.database.session() as db:
        rows = await db.fetch_limited(
            "SELECT action, actor_user_id, environment_id FROM activity_event "
            "WHERE action LIKE 'container_%'",
            (),
            limit=20,
        )
    assert [(r["action"], r["actor_user_id"], r["environment_id"]) for r in rows] == [
        ("container_started", ADMIN_ID, world.remote_id),
        ("container_started", ADMIN_ID, world.remote_id),
        ("container_stopped", ADMIN_ID, world.remote_id),
        ("container_restarted", ADMIN_ID, world.remote_id),
        ("container_restarted", ADMIN_ID, world.remote_id),
    ]


async def test_a_standard_token_may_act_where_it_holds_write_and_not_where_it_holds_read_or_nothing(
    user_api: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    allowed = await user_api.post(f"{base(world)}/{DB}/stop")
    read_only = await user_api.post(f"{base(world)}/{JOB}/start")
    hidden = await user_api.post(f"{base(world)}/{CACHE}/start")
    missing = await user_api.post(f"{base(world)}/{ABSENT}/start")

    assert allowed.status_code == OK
    assert (read_only.status_code, hidden.status_code, missing.status_code) == (
        FORBIDDEN,
        NOT_FOUND,
        NOT_FOUND,
    )
    assert shared_host.mutations() == [f"POST /containers/{DB}/stop"]


async def test_a_standard_token_cannot_change_anything_on_the_local_socket(
    user_api: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await plant_control(world.app, world.local_id, "container", API, public=True, level="write")
    container_host.add(API, "api")

    listed = await user_api.get(base(world, world.local_id))
    started = await user_api.post(f"{base(world, world.local_id)}/{API}/stop")

    assert names(listed.json()) == ["api"]
    assert started.status_code == FORBIDDEN
    assert container_host.mutations() == []


async def test_a_conflict_a_vanished_container_and_a_refusing_host_are_409_404_and_502(
    admin_api: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.fail("stop", 404)
    container_host.fail("restart", 500)

    conflict = await admin_api.post(f"{base(world)}/{CACHE}/start")
    gone = await admin_api.post(f"{base(world)}/{WEB}/stop")
    refused = await admin_api.post(f"{base(world)}/{WEB}/restart")

    assert conflict.status_code == CONFLICT
    assert conflict.json()["error"]["message"] == "The container is paused. Unpause it first."
    assert gone.status_code == NOT_FOUND
    assert refused.status_code == BAD_GATEWAY
    assert refused.json()["error"]["message"] == "Docker refused the action."
    assert ENGINE_TEXT not in conflict.text + gone.text + refused.text


async def test_a_busy_container_is_a_409_naming_the_holder_by_the_callers_standing(
    admin_api: AsyncClient, user_api: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    locks = world.app.state.engine.locks
    key = ObjectKey(world.remote_id, "container", DB)

    async with locks.hold(key, LockHolder(UserActor(ADMIN_ID), ADMIN_USERNAME, "stop")):
        by_admin = await admin_api.post(f"{base(world)}/{DB}/stop")
        by_user = await user_api.post(f"{base(world)}/{DB}/stop")

    assert by_admin.status_code == by_user.status_code == CONFLICT
    assert by_admin.json()["error"]["holder"]["holder"] == ADMIN_USERNAME
    assert by_admin.json()["error"]["holder"]["held_by_you"] is True
    assert by_user.json()["error"]["holder"]["holder"] == "another user"
    assert ADMIN_USERNAME not in by_user.text and STANDARD_USERNAME not in by_user.text


async def test_an_unreachable_oversized_or_unreadable_host_is_a_503_with_fixed_text(
    admin_api: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_host.route("POST", f"/containers/{WEB}/stop", Refuse())
    stop = await admin_api.post(f"{base(world)}/{WEB}/stop")
    container_host.route("GET", "/containers/json", Refuse())
    down = await admin_api.get(base(world))
    monkeypatch.setattr(resources, "CONTAINER_LIST_BYTES", 10)
    container_host.route("GET", "/containers/json", Reply(200, b"[]", OVER_THE_CAP))
    large = await admin_api.get(base(world))

    assert (stop.status_code, down.status_code, large.status_code) == (UNAVAILABLE,) * 3
    assert down.json()["error"]["message"] == "The Docker host could not be reached."
    assert "32 MiB" in large.json()["error"]["message"]


async def test_the_api_offers_only_start_stop_and_restart_and_its_models_hide_secrets_sources(
    admin_api: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    path = f"{base(world)}/{WEB}"

    kill = await admin_api.post(f"{path}/kill")
    remove = await admin_api.delete(path)
    document = world.app.openapi()

    schemas = document["components"]["schemas"]
    models = {
        name: set(schema["properties"])
        for name, schema in schemas.items()
        if name in {"ContainerModel", "ContainerDetailModel", "ActionModel"}
    }
    fields = set().union(*models.values())
    assert (kill.status_code, remove.status_code) == (NOT_FOUND, METHOD_NOT_ALLOWED)
    assert container_host.mutations() == []
    assert set(models) == {"ContainerModel", "ContainerDetailModel", "ActionModel"}
    assert fields.isdisjoint({"variables", "env", "environment", "command", "entrypoint", "status"})


RECREATED = "recreate-me"
RECREATE_KEYS = {
    "old_id",
    "new_id",
    "name",
    "pulled",
    "image_changed",
    "changed",
    "started",
    "old_removed",
    "remove_outcome_unknown",
    "kept_as",
    "control_moved",
    "old_control_kept",
}
BODY_FAULTS: dict[str, dict[str, Any]] = {
    "name missing": {},
    "name empty": {"expected_name": ""},
    "name too long": {"expected_name": "x" * 257},
    "pull not a flag": {"expected_name": RECREATED, "pull": "perhaps"},
    "unknown field": {"expected_name": RECREATED, "extra": 1},
}


def recreate_url(world: World, container: str = OLD, environment_id: int | None = None) -> str:
    return f"{base(world, environment_id)}/{container}/recreate"


@pytest.fixture
def recreatable(container_host: ContainerEngine) -> ContainerEngine:
    container_host.add_image(IMAGE)
    seed_recreatable(container_host, name=RECREATED)
    return container_host


async def test_recreate_answers_what_happened_and_nothing_of_the_configuration(
    admin_api: AsyncClient, world: World, recreatable: ContainerEngine
) -> None:
    reply = await admin_api.post(recreate_url(world), json={"expected_name": RECREATED})

    assert reply.status_code == OK
    assert set(reply.json()) == RECREATE_KEYS
    assert reply.json() == {
        "old_id": OLD,
        "new_id": created_id(1),
        "name": RECREATED,
        "pulled": False,
        "image_changed": False,
        "changed": ["none"],
        "started": True,
        "old_removed": True,
        "remove_outcome_unknown": False,
        "kept_as": None,
        "control_moved": True,
        "old_control_kept": False,
    }
    assert "APP=1" not in reply.text and IMAGE not in reply.text


async def test_recreate_with_pull_pulls_first_and_names_what_changed(
    admin_api: AsyncClient, world: World, recreatable: ContainerEngine
) -> None:
    recreatable.pull_id = "sha256:" + "d" * 64
    recreatable.pull_config = {"Env": ["PATH=/bin", "FOO=new"], "Cmd": ["nginx"]}

    reply = await admin_api.post(
        recreate_url(world), json={"expected_name": RECREATED, "pull": True}
    )

    assert reply.status_code == OK
    assert (reply.json()["pulled"], reply.json()["image_changed"]) == (True, True)
    assert reply.json()["changed"] == ["image", "env"]
    assert recreatable.verbs()[0] == "pull"


async def test_only_a_bearer_token_may_recreate_and_a_session_or_nobody_gets_401(
    browsers: Browsers,
    admin_client: AsyncClient,
    world: World,
    recreatable: ContainerEngine,
) -> None:
    forged = browsers.open()
    forged.headers.update(bearer("dkm_notatoken"))
    body = {"expected_name": RECREATED}

    by_session = await admin_client.post(
        recreate_url(world), json=body, headers=csrf_headers(admin_client)
    )
    by_forgery = await forged.post(recreate_url(world), json=body)

    assert (by_session.status_code, by_forgery.status_code) == (UNAUTHENTICATED, UNAUTHENTICATED)
    assert recreatable.requests == []


async def test_a_malformed_recreate_body_is_refused_before_the_host_is_asked_anything(
    admin_api: AsyncClient, world: World, recreatable: ContainerEngine
) -> None:
    statuses = {
        fault: (await admin_api.post(recreate_url(world), json=body)).status_code
        for fault, body in BODY_FAULTS.items()
    }
    not_json = await admin_api.post(recreate_url(world), content=b"not json")

    assert statuses == dict.fromkeys(BODY_FAULTS, UNPROCESSABLE)
    assert not_json.status_code == UNPROCESSABLE
    assert recreatable.requests == []


async def test_recreate_refuses_what_the_caller_may_not_see_or_do_with_the_right_status(
    admin_api: AsyncClient,
    user_api: AsyncClient,
    world: World,
    recreatable: ContainerEngine,
) -> None:
    body = {"expected_name": RECREATED}
    local = world.local_id
    await plant_control(world.app, world.remote_id, "container", OLD, public=True, level="read")
    await plant_control(world.app, local, "container", OLD, public=True, level="write")

    read_holder = await user_api.post(recreate_url(world), json=body)
    on_local_socket = await user_api.post(recreate_url(world, environment_id=local), json=body)
    unseen = await user_api.post(recreate_url(world, ABSENT), json=body)
    absent = await admin_api.post(recreate_url(world, ABSENT), json=body)
    not_an_id = await admin_api.post(recreate_url(world, "recreate-me"), json=body)
    unknown_environment = await admin_api.post(recreate_url(world, environment_id=99999), json=body)

    assert read_holder.status_code == FORBIDDEN
    assert on_local_socket.status_code == FORBIDDEN
    assert (unseen.status_code, absent.status_code) == (NOT_FOUND, NOT_FOUND)
    assert not_an_id.status_code == NOT_FOUND
    assert unknown_environment.status_code == NOT_FOUND
    assert recreatable.mutations() == []


async def test_a_standard_token_recreating_a_container_with_restricted_options_gets_400_by_path(
    user_api: AsyncClient, world: World, recreatable: ContainerEngine
) -> None:
    present(recreatable.boxes[OLD].host_config).update({"Privileged": True, "PidMode": "host"})
    await plant_control(world.app, world.remote_id, "container", OLD, public=True, level="write")

    reply = await user_api.post(recreate_url(world), json={"expected_name": RECREATED})

    assert reply.status_code == BAD_REQUEST
    fields = reply.json()["error"]["fields"]
    assert [f["loc"] for f in fields] == [
        ["configuration", "HostConfig.PidMode"],
        ["configuration", "HostConfig.Privileged"],
    ]
    assert all("Only administrators may use" in f["msg"] for f in fields)
    assert recreatable.verbs() == []


RECREATE_REFUSALS: dict[str, tuple[Any, dict[str, Any], int, str]] = {
    "name changed": (
        lambda host: None,
        {"expected_name": "other"},
        CONFLICT,
        "This container changed since you reviewed it; nothing was recreated.",
    ),
    "not recreatable": (
        lambda host: host.boxes[OLD].host_config.update({"AutoRemove": True}),
        {"expected_name": RECREATED},
        CONFLICT,
        "This container is removed when it stops, so there is nothing to roll back to.",
    ),
    "rolled back": (
        lambda host: host.fail("create", 500),
        {"expected_name": RECREATED},
        BAD_GATEWAY,
        "Docker could not recreate the container. The original container was restored.",
    ),
    "pull failed": (
        lambda host: setattr(host, "pull_status", 500),
        {"expected_name": RECREATED, "pull": True},
        BAD_GATEWAY,
        "The image could not be pulled.",
    ),
    "outcome unknown": (
        lambda host: host.route("POST", f"/containers/{OLD}/stop", Stall()),
        {"expected_name": RECREATED},
        UNAVAILABLE,
        "The Docker host did not answer in time, so the state of recreate-me is unknown.",
    ),
    "unreachable": (
        lambda host: host.route("GET", f"/containers/{OLD}/json", Refuse()),
        {"expected_name": RECREATED},
        UNAVAILABLE,
        "The Docker host could not be reached.",
    ),
}


async def test_recreate_maps_every_failure_to_its_status_with_a_product_message(
    admin_api: AsyncClient, world: World, recreatable: ContainerEngine
) -> None:
    replies = {
        label: await _refused(admin_api, world, recreatable, arrange, body)
        for label, (arrange, body, _, _) in RECREATE_REFUSALS.items()
    }

    assert {label: r.status_code for label, r in replies.items()} == {
        label: status for label, (_, _, status, _) in RECREATE_REFUSALS.items()
    }
    assert {label: r.json()["error"]["message"] for label, r in replies.items()} == {
        label: message for label, (_, _, _, message) in RECREATE_REFUSALS.items()
    }
    assert replies["not recreatable"].json()["error"]["blockers"] == ["auto_remove"]
    assert not any(ENGINE_TEXT in r.text for r in replies.values())


async def _refused(
    client: AsyncClient, world: World, host: ContainerEngine, arrange: Any, body: dict[str, Any]
) -> Any:
    host.clear_faults()
    present(host.boxes[OLD].host_config).pop("AutoRemove", None)
    arrange(host)
    return await client.post(recreate_url(world), json=body)


async def test_recreate_of_a_busy_container_is_409_and_names_the_action_not_the_user(
    admin_api: AsyncClient, world: World, recreatable: ContainerEngine
) -> None:
    holder = LockHolder(UserActor(ADMIN_ID), ADMIN_USERNAME, "restart")

    async with world.app.state.engine.locks.hold(
        ObjectKey(world.remote_id, "container", OLD), holder
    ):
        reply = await admin_api.post(recreate_url(world), json={"expected_name": RECREATED})

    assert reply.status_code == CONFLICT
    assert "restart" in reply.text
    assert recreatable.verbs() == []


HUB_KEY = "docker.io/library/nginx:1"
CHOICE_UNAVAILABLE = "credential_unavailable"
MALFORMED_CHOICES = {
    "neither": {},
    "both": {"credential_set_id": 1, "anonymous": True},
    "id-zero": {"credential_set_id": 0},
    "id-text": {"credential_set_id": "one"},
    "unknown-field": {"anonymous": True, "extra": 1},
}


async def hub_set(
    client: AsyncClient, world: World, name: str, *, address: str = "docker.io", users: bool = False
) -> int:
    set_id = await create_set(client, custom_form(client, name, address=address))
    await client.post(
        f"/registries/{set_id}/environments",
        data=registry_form(client, environment_name=REMOTE_NAME),
    )
    if users:
        await client.post(
            f"/registries/{set_id}/access",
            data=registry_form(client, grantee_kind="user", grantee_name=STANDARD_USERNAME),
        )
    return set_id


async def bind(world: World, set_id: int | None, reference: str = HUB_KEY) -> None:
    async with world.app.state.database.session() as db:
        await bind_image(db, set_id, world.remote_id, reference)


async def bindings_of(world: World) -> list[dict[str, Any]]:
    async with world.app.state.database.session() as db:
        return await binding_rows(db)


def username_sent(host: ContainerEngine) -> str | None:
    [pull] = host.calls("POST", "/images/create")
    header = pull.headers.get("x-registry-auth")
    return None if header is None else decode_auth(header)["username"]


def recreate_body(**choice: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"expected_name": RECREATED, "pull": True}
    return body | ({"credential_choice": choice} if choice else {})


async def test_a_recreate_with_a_pull_asks_which_set_refuses_bad_choices_and_binds_the_one_named(
    admin_api: AsyncClient,
    admin_client: AsyncClient,
    world: World,
    recreatable: ContainerEngine,
) -> None:
    first = await hub_set(admin_client, world, "alpha")
    chosen = await hub_set(admin_client, world, "beta")
    elsewhere = await hub_set(admin_client, world, "elsewhere", address="ghcr.io")
    url = recreate_url(world)

    asked = await admin_api.post(url, json=recreate_body())
    malformed = {
        fault: await admin_api.post(
            url, json={"expected_name": RECREATED, "pull": True, "credential_choice": choice}
        )
        for fault, choice in MALFORMED_CHOICES.items()
    }
    unusable = {
        which: await admin_api.post(url, json=recreate_body(credential_set_id=set_id))
        for which, set_id in (("unknown-set", 9999), ("set-at-another-registry", elsewhere))
    }
    mutations_before = recreatable.mutations()
    done = await admin_api.post(url, json=recreate_body(credential_set_id=chosen))

    [binding] = await bindings_of(world)
    assert asked.status_code == CONFLICT
    assert asked.json()["error"]["reason"] == "credential_choice_required"
    assert asked.json()["error"]["candidates"] == [
        {"id": first, "name": "alpha"},
        {"id": chosen, "name": "beta"},
    ]
    assert {fault: reply.status_code for fault, reply in malformed.items()} == dict.fromkeys(
        MALFORMED_CHOICES, UNPROCESSABLE
    )
    assert {which: reply.status_code for which, reply in unusable.items()} == {
        "unknown-set": CONFLICT,
        "set-at-another-registry": CONFLICT,
    }
    assert {r.json()["error"]["reason"] for r in unusable.values()} == {CHOICE_UNAVAILABLE}
    assert mutations_before == []
    assert done.status_code == OK
    assert username_sent(recreatable) == USERNAME
    assert (binding["image_reference"], binding["registry_credential_id"]) == (HUB_KEY, chosen)
    assert "x-registry-auth" not in done.text


async def test_a_recreate_may_choose_to_pull_anonymously_and_binds_nothing(
    admin_api: AsyncClient,
    admin_client: AsyncClient,
    world: World,
    recreatable: ContainerEngine,
) -> None:
    await hub_set(admin_client, world, "alpha")
    await hub_set(admin_client, world, "beta")

    reply = await admin_api.post(recreate_url(world), json=recreate_body(anonymous=True))

    assert reply.status_code == OK
    assert username_sent(recreatable) is None
    assert await bindings_of(world) == []


async def test_a_recreate_pulls_with_the_bound_set_when_none_is_named(
    admin_api: AsyncClient,
    admin_client: AsyncClient,
    world: World,
    recreatable: ContainerEngine,
) -> None:
    await hub_set(admin_client, world, "alpha")
    bound = await hub_set(admin_client, world, "beta")
    await bind(world, bound)

    reply = await admin_api.post(recreate_url(world), json=recreate_body())

    assert reply.status_code == OK
    assert username_sent(recreatable) == USERNAME


async def test_a_recreate_of_a_credential_missing_image_asks_for_a_set_like_an_unbound_one(
    admin_api: AsyncClient,
    admin_client: AsyncClient,
    world: World,
    recreatable: ContainerEngine,
) -> None:
    gone = await hub_set(admin_client, world, "gone")
    replacement = await hub_set(admin_client, world, "replacement")
    await bind(world, gone)
    async with world.app.state.database.session() as db:
        await mark_missing(db, gone)

    refused = await admin_api.post(recreate_url(world), json=recreate_body())
    repaired = await admin_api.post(
        recreate_url(world), json=recreate_body(credential_set_id=replacement)
    )

    [binding] = await bindings_of(world)
    assert refused.status_code == CONFLICT
    assert refused.json()["error"]["reason"] == "credential_choice_required"
    assert repaired.status_code == OK
    assert (binding["state"], binding["registry_credential_id"]) == ("bound", replacement)


async def test_a_standard_token_may_not_change_the_set_an_image_is_bound_to(
    user_api: AsyncClient,
    admin_client: AsyncClient,
    world: World,
    recreatable: ContainerEngine,
) -> None:
    bound = await hub_set(admin_client, world, "alpha", users=True)
    other = await hub_set(admin_client, world, "beta", users=True)
    await bind(world, bound)
    await plant_control(world.app, world.remote_id, "container", OLD, public=True, level="write")

    reply = await user_api.post(recreate_url(world), json=recreate_body(credential_set_id=other))

    assert reply.status_code == FORBIDDEN
    assert recreatable.mutations() == []
    assert (await bindings_of(world))[0]["registry_credential_id"] == bound


async def test_a_recreate_that_would_change_a_credential_binding_is_403_with_the_restricted_reason(
    user_api: AsyncClient,
    world: World,
    recreatable: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(*arguments: Any, **options: Any) -> None:
        raise RebindRefused()

    monkeypatch.setattr(ContainerRecreateService, "recreate", refuse)

    reply = await user_api.post(recreate_url(world), json={"expected_name": RECREATED})

    assert reply.status_code == FORBIDDEN
    assert reply.json()["error"]["reason"] == "restricted"
    assert reply.json()["error"]["message"] == REBIND_REFUSED
    assert recreatable.verbs() == []
