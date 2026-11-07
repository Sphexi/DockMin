"""The create, duplicate and recreate pages: plain posts, retained values, refusals and flags."""

import asyncio
import io
import json
import re
from collections.abc import Callable
from html import unescape
from typing import Any
from urllib.parse import urlsplit

import pytest
from httpx import AsyncClient, Response

from app.auth.errors import ValidationFailed
from app.services.activity import UserActor
from app.services.container_create import ContainerCreateService
from app.services.container_spec import GROUPS
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.resource_controls import ResourceControlService
from tests.access_support import container_id
from tests.auth_support import csrf_form
from tests.container_support import WEB, plant_control
from tests.docker_engine.container_engine import ENGINE_TEXT, ContainerEngine, created_id
from tests.docker_engine.fake_engine import Refuse, Stall
from tests.provisioning_support import IMAGE, OLD, create_form, seed_recreatable
from tests.support import present
from tests.web.engine_detail_support import recorded_text
from tests.web.environment_support import World
from tests.web.flash_support import flash_texts, land
from tests.web.helpers import (
    checked_values,
    field_error,
    form_values,
    hidden_inputs,
    inline_code_in,
    inputs_named,
    selected_option,
    value_of,
)

OK, SEE_OTHER, BAD_REQUEST, FORBIDDEN, NOT_FOUND, CONFLICT = 200, 303, 400, 403, 404, 409
BAD_GATEWAY, UNAVAILABLE = 502, 503
NEW = created_id(1)
SOURCE = container_id(0xB7)
UNSHARED = container_id(0xB8)
ABSENT = container_id(0xFE)
SECRET = "hunter2-env-value"
MARKER = "Zq9-marker"
WRONG_TOKEN = "W" * 43
NAME = "recreate-me"
ABSENT_IMAGE = "ghcr.io/org/absent:1"
SECTIONS = (
    "image",
    "command",
    "env",
    "labels",
    "ports",
    "mounts",
    "network",
    "restart",
    "resources",
    "healthcheck",
    "logging",
    "dns",
    "security",
    "start",
)
ADMIN_ONLY_NAMES = ("privileged", "cap_add", "runtime", "pid_host", "ipc_host")
SUMMARY = re.compile(r'<div class="state state--error error-summary".*?</div>', re.S)
Arrange = Callable[[ContainerEngine], object]


def nothing(host: ContainerEngine) -> None:
    """Arrange no failure."""


def new_path(world: World, environment_id: int | None = None) -> str:
    return f"/environments/{environment_id or world.remote_id}/containers/new"


def create_path(world: World, environment_id: int | None = None) -> str:
    return f"/environments/{environment_id or world.remote_id}/containers"


def recreate_path(world: World, container: str = OLD) -> str:
    return f"/environments/{world.remote_id}/containers/{container}/recreate"


def detail_path(world: World, container: str) -> str:
    return f"/environments/{world.remote_id}/containers/{container}"


def flashes(response: Response) -> list[str]:
    return flash_texts(response.text)


async def shown(client: AsyncClient, response: Response) -> Response:
    """Return the page a post leads to: the one it redirects to, or its own answer."""
    return await land(client, response) if response.status_code == SEE_OTHER else response


def assert_no_store(*responses: Response) -> None:
    assert {r.headers.get("cache-control") for r in responses} == {"no-store"}


async def review(client: AsyncClient, world: World, **fields: str) -> Response:
    data = csrf_form(client, intent="review", **create_form(**fields))
    return await client.post(new_path(world), data=data)


async def create_with(
    client: AsyncClient, world: World, host: ContainerEngine, arrange: Arrange, **fields: str
) -> Response:
    host.clear_faults()
    arrange(host)
    return await client.post(create_path(world), data=csrf_form(client, **create_form(**fields)))


async def recreate_with(
    client: AsyncClient, world: World, host: ContainerEngine, arrange: Arrange, **fields: str
) -> Response:
    host.clear_faults()
    arrange(host)
    return await client.post(recreate_path(world), data=csrf_form(client, **fields))


@pytest.fixture
def daemon(container_host: ContainerEngine) -> ContainerEngine:
    container_host.add_image(IMAGE)
    return container_host


@pytest.fixture
async def source(world: World, daemon: ContainerEngine) -> ContainerEngine:
    """Two containers carrying a secret and an administrator-only option; one is shared."""
    daemon.add_volume("data")
    body = {
        "Image": IMAGE,
        "Env": [f"DB_PASSWORD={SECRET}", "MODE=fast"],
        "Cmd": ["serve", "--fast"],
        "Labels": {"tier": "front", "com.docker.compose.project": "shop"},
        "HostConfig": {"Binds": ["data:/data"], "Privileged": True},
    }
    daemon.add_configured(SOURCE, "source", body)
    daemon.add_configured(UNSHARED, "unshared", body)
    await plant_control(world.app, world.remote_id, "container", SOURCE, public=True, level="read")
    return daemon


@pytest.fixture
def recreatable(daemon: ContainerEngine) -> ContainerEngine:
    seed_recreatable(daemon, name=NAME)
    return daemon


EDITED = {
    "name": "webapp",
    "restart_policy": "on-failure",
    "restart_max_retries": "3",
    "mount-0-type": "tmpfs",
    "mount-0-target": "/tmp",
    "port-0-container_port": "53",
    "port-0-protocol": "udp",
    "healthcheck_mode": "command",
    "healthcheck_test": "true",
    "env-0-key": "A",
    "env-0-value": "1",
    "init": "on",
    "pull": "never",
    "start": "",
}


async def test_the_core_flow_works_with_plain_posts_from_form_through_review_to_the_container(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    form = await admin_client.get(new_path(world))
    fields = {
        "name": "webapp",
        "env-0-key": "DB_PASSWORD",
        "env-0-value": SECRET,
        "env-1-key": "MODE",
        "env-1-value": "fast",
        "port-0-container_port": "80",
        "port-0-host_port": "8080",
        "mount-0-source": "data",
        "mount-0-target": "/data",
        "memory": "256m",
        "privileged": "on",
    }
    reviewed = await review(admin_client, world, **fields)
    mutations_after_review = daemon.mutations()
    created = await admin_client.post(create_path(world), data=hidden_inputs(reviewed.text))
    landed = await admin_client.get(created.headers["location"])

    assert form.status_code == OK
    assert all(f'id="section-{name}"' in form.text for name in SECTIONS)
    assert checked_values(form.text, "start") == ["on"]
    assert checked_values(form.text, "pull") == ["missing"]
    assert inline_code_in(form.text) == []
    assert reviewed.status_code == OK
    assert "Review container" in reviewed.text and "webapp" in reviewed.text
    assert "Administrator options in use" in reviewed.text
    assert "Value hidden" in reviewed.text
    assert f'<span class="mono">{SECRET}</span>' not in reviewed.text
    assert "8080 → 80/tcp" in reviewed.text and "256 MiB" in reviewed.text
    assert mutations_after_review == []
    assert daemon.verbs() == ["create", "start"]
    assert created.status_code == SEE_OTHER
    assert created.headers["location"] == f"{detail_path(world, NEW)}?created=started"
    assert landed.status_code == OK
    assert flashes(landed) == ["Container created and started."]
    assert_no_store(form, reviewed, created)


async def test_add_rows_and_back_to_edit_work_without_script_and_keep_every_value(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    posted = csrf_form(admin_client, **create_form(**EDITED))

    grown = await admin_client.post(new_path(world), data=posted | {"rows": "env"})
    networks = await admin_client.post(new_path(world), data=posted | {"rows": "networks"})
    reviewed = await review(admin_client, world, **EDITED)
    edited = await admin_client.post(
        new_path(world), data=hidden_inputs(reviewed.text) | {"intent": "edit"}
    )

    assert grown.status_code == networks.status_code == OK
    assert inputs_named(grown.text, "env-6-key") != []
    assert inputs_named(grown.text, "env-7-key") == []
    assert value_of(grown.text, "env-0-key") == "A" and value_of(grown.text, "name") == "webapp"
    assert inputs_named(networks.text, "network-1-name") != []
    assert inputs_named(networks.text, "network-2-name") == []
    assert all(f'name="rows" value="{g}"' in grown.text for g in GROUPS)
    assert reviewed.status_code == edited.status_code == OK
    assert "Create container" in edited.text and "Review container" not in edited.text
    assert value_of(edited.text, "name") == "webapp"
    assert checked_values(edited.text, "restart_policy") == ["on-failure"]
    assert checked_values(edited.text, "pull") == ["never"]
    assert checked_values(edited.text, "start") == []
    assert checked_values(edited.text, "init") == ["on"]
    assert checked_values(edited.text, "healthcheck_mode") == ["command"]
    assert selected_option(edited.text, "mount-0-type") == "tmpfs"
    assert selected_option(edited.text, "port-0-protocol") == "udp"
    assert value_of(edited.text, "restart_max_retries") == "3"
    assert_no_store(grown, networks, reviewed, edited)


def assert_validation_failures(page: Response) -> None:
    summary = SUMMARY.search(page.text)
    assert page.status_code == BAD_REQUEST and summary is not None
    assert MARKER not in summary.group(0)
    assert field_error(page.text, "field-memory") == "Memory must be at least 6 MiB."
    assert field_error(page.text, "field-user") is not None
    assert field_error(page.text, "field-env-1-key") is not None
    assert field_error(page.text, "field-port-0-container_port") is not None
    assert value_of(page.text, "name") == "webapp"
    assert value_of(page.text, "memory") == "5m"
    assert value_of(page.text, "user") == f"{MARKER} bad user"
    assert value_of(page.text, "command") == "serve --fast"
    assert (value_of(page.text, "env-0-value"), value_of(page.text, "env-1-value")) == (
        "kept",
        "also kept",
    )
    assert checked_values(page.text, "network_mode") == ["none"]
    assert checked_values(page.text, "init") == ["on"]


async def test_validation_failures_render_beside_their_fields_keep_every_value_and_quote_nothing(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    fields = {
        "name": "webapp",
        "memory": "5m",
        "user": f"{MARKER} bad user",
        "command": "serve --fast",
        "env-0-key": "A",
        "env-0-value": "kept",
        "env-1-key": "B=C",
        "env-1-value": "also kept",
        "port-0-container_port": "99999",
        "network_mode": "none",
        "init": "on",
    }
    submitted = csrf_form(admin_client, intent="review", **create_form(**fields))

    reviewed = await admin_client.post(new_path(world), data=submitted)
    created = await admin_client.post(create_path(world), data=submitted)

    assert_validation_failures(reviewed)
    assert_validation_failures(created)
    assert daemon.mutations() == []
    assert_no_store(reviewed, created)


async def test_a_standard_user_is_not_offered_administrator_controls_and_hand_posted_ones_fail(
    standard_client: AsyncClient,
    admin_client: AsyncClient,
    world: World,
    daemon: ContainerEngine,
) -> None:
    fields = {
        "privileged": "on",
        "cap_add": "NET_ADMIN",
        "network_mode": "host",
        "mount-0-type": "bind",
        "mount-0-source": "/etc",
        "mount-0-target": "/x",
        "device-0-host_path": "/dev/mem",
    }

    standard = await standard_client.get(new_path(world))
    administrator = await admin_client.get(new_path(world))
    refused = await standard_client.post(
        create_path(world), data=csrf_form(standard_client, **create_form(**fields))
    )

    assert standard.status_code == administrator.status_code == OK
    assert [inputs_named(standard.text, name) for name in ADMIN_ONLY_NAMES] == [[]] * 5
    assert all(inputs_named(administrator.text, name) != [] for name in ADMIN_ONLY_NAMES)
    assert inputs_named(standard.text, "device-0-host_path") == []
    assert inputs_named(administrator.text, "device-0-host_path") != []
    assert "host" not in [t.attrs["value"] for t in inputs_named(standard.text, "network_mode")]
    assert "host" in [t.attrs["value"] for t in inputs_named(administrator.text, "network_mode")]
    assert '<option value="bind"' not in standard.text
    assert '<option value="bind"' in administrator.text
    assert "available to administrators only" in standard.text
    assert "available to administrators only" not in administrator.text
    summary = SUMMARY.search(refused.text)
    assert refused.status_code == BAD_REQUEST and summary is not None
    assert summary.group(0).count("Only administrators may use") >= 4
    assert field_error(refused.text, "field-mount-0-type") == (
        "Only administrators may use host bind mounts."
    )
    assert value_of(refused.text, "mount-0-source") == "/etc"
    assert daemon.mutations() == []
    assert_no_store(standard, administrator, refused)


async def test_the_local_socket_is_closed_to_standard_users_and_an_unshared_environment_is_hidden(
    standard_client: AsyncClient, admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    posted = csrf_form(standard_client, **create_form())
    local, down = world.local_id, world.down_id

    on_local = [
        (await standard_client.get(new_path(world, local))).status_code,
        (await standard_client.post(create_path(world, local), data=posted)).status_code,
    ]
    on_down = [
        (await standard_client.get(new_path(world, down))).status_code,
        (await standard_client.post(new_path(world, down), data=posted)).status_code,
        (await standard_client.post(create_path(world, down), data=posted)).status_code,
    ]
    allowed = await admin_client.get(new_path(world, local))

    assert on_local == [FORBIDDEN, FORBIDDEN]
    assert on_down == [NOT_FOUND] * 3
    assert allowed.status_code == OK and "the host dockmin runs on" in allowed.text
    assert daemon.mutations() == []


async def test_a_duplicate_form_is_prefilled_per_role_and_an_unreadable_source_is_not_found(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World, source: ContainerEngine
) -> None:
    administrator = await admin_client.get(f"{new_path(world)}?from={SOURCE}")
    standard = await standard_client.get(f"{new_path(world)}?from={SOURCE}")
    unshared = await standard_client.get(f"{new_path(world)}?from={UNSHARED}")
    absent = await standard_client.get(f"{new_path(world)}?from={ABSENT}")

    assert administrator.status_code == standard.status_code == OK
    assert "Duplicate container" in administrator.text and "From source on" in administrator.text
    assert value_of(administrator.text, "image") == IMAGE
    assert not value_of(administrator.text, "name")
    assert 'placeholder="source-copy"' in administrator.text
    assert value_of(administrator.text, "from") == SOURCE
    assert value_of(administrator.text, "env-0-key") == "DB_PASSWORD"
    assert value_of(administrator.text, "env-0-value") == SECRET
    assert checked_values(administrator.text, "privileged") == ["on"]
    assert "Not carried over from the source" in administrator.text
    assert value_of(standard.text, "image") == IMAGE
    assert SECRET not in standard.text and "serve --fast" not in standard.text
    assert not value_of(standard.text, "env-0-key") and not value_of(standard.text, "command")
    assert inputs_named(standard.text, "privileged") == []
    assert value_of(standard.text, "label-0-key") == "tier"
    assert not value_of(standard.text, "label-1-key")
    assert "com.docker.compose.project" not in form_values(standard.text).values()
    assert "Not copied, only administrators may set them" in unescape(standard.text)
    assert value_of(standard.text, "from") == SOURCE
    assert unshared.status_code == absent.status_code == NOT_FOUND
    assert_no_store(administrator, standard)


async def test_creating_a_duplicate_records_the_source_and_lands_on_the_new_container(
    admin_client: AsyncClient, world: World, source: ContainerEngine
) -> None:
    page = await admin_client.get(f"{new_path(world)}?from={SOURCE}")
    data = form_values(page.text) | {"name": "source-copy", "intent": "review"}
    reviewed = await admin_client.post(new_path(world), data=data)

    created = await admin_client.post(create_path(world), data=hidden_inputs(reviewed.text))

    assert created.status_code == SEE_OTHER
    assert value_of(reviewed.text, "from") == SOURCE
    assert source.verbs() == ["create", "start"]
    async with world.app.state.database.session() as db:
        rows = await db.fetch_limited(
            "SELECT detail FROM activity_event WHERE action = 'container_created'", (), limit=5
        )
    assert json.loads(rows[0]["detail"])["duplicate_of"] == SOURCE


FAILURES: dict[str, tuple[Arrange, dict[str, str], int, str]] = {
    "name in use": (
        lambda host: host.add("c" * 64, "taken", image=IMAGE),
        {"name": "taken"},
        CONFLICT,
        "A container with this name already exists.",
    ),
    "refused": (
        lambda host: host.fail("create", 400),
        {},
        BAD_REQUEST,
        "Docker refused this configuration",
    ),
    "unreachable": (
        lambda host: host.route("POST", "/containers/create", Refuse()),
        {},
        UNAVAILABLE,
        "The Docker host could not be reached.",
    ),
    "outcome unknown": (
        lambda host: host.route("POST", "/containers/create", Stall()),
        {},
        UNAVAILABLE,
        "The result of the request is not known",
    ),
}


async def test_a_refusal_by_docker_is_shown_where_it_belongs_and_the_form_keeps_its_values(
    admin_client: AsyncClient, world: World, daemon: ContainerEngine
) -> None:
    pages = {
        label: await create_with(
            admin_client, world, daemon, arrange, **({"name": "webapp"} | fields)
        )
        for label, (arrange, fields, _, _) in FAILURES.items()
    }

    assert {label: page.status_code for label, page in pages.items()} == {
        label: status for label, (_, _, status, _) in FAILURES.items()
    }
    assert {
        label: text in unescape(pages[label].text) for label, (_, _, _, text) in FAILURES.items()
    } == dict.fromkeys(FAILURES, True)
    assert field_error(pages["name in use"].text, "field-name") == FAILURES["name in use"][3]
    assert {label: value_of(page.text, "name") for label, page in pages.items()} == {
        label: fields.get("name", "webapp") for label, (_, fields, _, _) in FAILURES.items()
    }
    assert [label for label, page in pages.items() if ENGINE_TEXT in page.text] == ["refused"]
    assert not any("registry detail" in page.text for page in pages.values())


async def test_docker_text_on_a_refused_create_is_shown_to_administrators_only_and_recorded_nowhere(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    daemon: ContainerEngine,
    log_stream: io.StringIO,
) -> None:
    def refuse(host: ContainerEngine) -> None:
        host.fail("create", BAD_REQUEST)

    as_administrator = await create_with(admin_client, world, daemon, refuse)
    as_standard_user = await create_with(standard_client, world, daemon, refuse)

    assert as_administrator.status_code == as_standard_user.status_code == BAD_REQUEST
    assert "Docker said" in as_administrator.text and ENGINE_TEXT in as_administrator.text
    assert "Docker said" not in as_standard_user.text
    assert ENGINE_TEXT not in as_standard_user.text
    assert ENGINE_TEXT not in await recorded_text(world.app, log_stream)


async def test_the_page_a_created_container_lands_on_says_whether_it_started_and_is_owned(
    admin_client: AsyncClient,
    world: World,
    daemon: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = await create_with(admin_client, world, daemon, nothing, start="")
    unstarted = await create_with(admin_client, world, daemon, lambda h: h.fail("start", 500))

    async def broken(self: ResourceControlService, *args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("database is busy")

    monkeypatch.setattr(ResourceControlService, "claim", broken)
    unowned = await create_with(admin_client, world, daemon, nothing)
    landings = {
        label: await admin_client.get(page.headers["location"])
        for label, page in (("stopped", stopped), ("unstarted", unstarted), ("unowned", unowned))
    }

    assert [p.headers["location"].split("?")[1] for p in (stopped, unstarted, unowned)] == [
        "created=stopped",
        "created=start_failed",
        "created=started&control=missing",
    ]
    assert {label: flashes(page) for label, page in landings.items()} == {
        "stopped": ["Container created. It was not started."],
        "unstarted": ["Created, but Docker could not start it."],
        "unowned": [
            "Container created and started.",
            "Created, but its access settings could not be saved.",
        ],
    }


FLAG_CASES: dict[str, list[str]] = {
    "created=started": ["Container created and started."],
    "created=<script>alert(1)</script>": [],
    "created=evil": [],
    "created=" + "a" * 5000: [],
    "recreated=started&changed=image,env&pulled=1&old=removed&control=moved": [
        "Container recreated and started. The image was pulled again.",
        "Changed: Image; Environment variables.",
    ],
    "recreated=started&changed=<b>x</b>,image,evil&pulled=maybe&old=<i>&control=": [
        "Container recreated and started.",
        "Changed: Image.",
    ],
    "recreated=<script>alert(1)</script>&changed=image": [],
    "recreated=stopped&changed=none&old=kept&control=copied": [
        "Container recreated. It was left stopped, as the original was.",
        "Changed: No configuration changed.",
        "The original container was not removed. It is still on the host under a name "
        "containing -recreating-, with the same access settings as the new container. "
        "Remove it when it is no longer needed.",
    ],
    "recreated=started&old=unknown&control=missing": [
        "Container recreated and started.",
        "Docker did not confirm whether the original container was removed. "
        "Check the container list.",
        "Recreated, but its access settings could not be moved. "
        "An administrator can review them on its Access tab.",
    ],
}


async def test_the_flags_on_a_landing_page_render_only_known_values_and_never_markup(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    pages = {
        query: await admin_client.get(f"{detail_path(world, WEB)}?{query}") for query in FLAG_CASES
    }

    assert {query: page.status_code for query, page in pages.items()} == dict.fromkeys(
        FLAG_CASES, OK
    )
    assert {query: flashes(page) for query, page in pages.items()} == FLAG_CASES
    assert not any("<script>alert" in page.text for page in pages.values())


async def test_the_recreate_confirmation_names_the_container_and_posting_it_lands_on_the_new_one(
    admin_client: AsyncClient, world: World, recreatable: ContainerEngine
) -> None:
    recreatable.pull_id = "sha256:" + "d" * 64
    recreatable.pull_config = {"Env": ["PATH=/bin", "FOO=new"], "Cmd": ["nginx"]}
    listing = await admin_client.get(f"/environments/{world.remote_id}/containers")
    detail = await admin_client.get(detail_path(world, OLD))
    confirm = await admin_client.get(recreate_path(world))
    mutations_before_posting = recreatable.mutations()

    recreated = await admin_client.post(
        recreate_path(world), data=hidden_inputs(confirm.text) | {"pull": "on"}
    )
    job_id = urlsplit(recreated.headers["location"]).path.rpartition("/")[2]
    await asyncio.wait({world.app.state.engine.image_jobs._jobs[job_id].task})
    landed = await admin_client.get(recreated.headers["location"])

    assert f'href="{new_path(world)}"' in listing.text
    assert f'href="{new_path(world)}?from={OLD}"' in detail.text
    assert f'href="{recreate_path(world)}"' in detail.text
    assert f'aria-label="Recreate {NAME}"' in listing.text
    assert f'aria-label="Duplicate {NAME}"' in listing.text
    assert confirm.status_code == OK
    assert NAME in confirm.text and OLD[:12] in confirm.text and IMAGE in confirm.text
    assert "remote-box" in confirm.text and "appnet" in confirm.text
    assert "1 anonymous volume" in confirm.text
    assert "Pull the image again first" in confirm.text
    assert value_of(confirm.text, "expected_name") == NAME
    assert checked_values(confirm.text, "pull") == []
    assert mutations_before_posting == []
    assert recreated.status_code == SEE_OTHER
    assert urlsplit(recreated.headers["location"]).path == (
        f"/environments/{world.remote_id}/containers/jobs/{job_id}"
    )
    assert recreatable.verbs() == ["pull", "stop", "rename", "create", "start", "remove"]
    assert f'href="{detail_path(world, NEW)}"' in landed.text
    assert "Container recreated and started. The image was pulled again." in landed.text
    assert "Changed: Image; Environment variables." in landed.text
    assert_no_store(confirm, recreated)


async def test_a_container_with_restricted_options_cannot_be_recreated_by_a_standard_user(
    standard_client: AsyncClient, world: World, recreatable: ContainerEngine
) -> None:
    present(recreatable.boxes[OLD].host_config).update({"Privileged": True})
    await plant_control(world.app, world.remote_id, "container", OLD, public=True, level="write")

    confirm = await standard_client.get(recreate_path(world))
    refused = await standard_client.post(
        recreate_path(world), data=csrf_form(standard_client, expected_name=NAME)
    )

    assert confirm.status_code == OK
    assert "Only an administrator can recreate this container" in confirm.text
    assert "privileged mode" in confirm.text
    assert 'name="expected_name"' not in confirm.text
    assert refused.status_code == BAD_REQUEST
    assert "Only administrators can recreate this container" in unescape(refused.text)
    assert recreatable.verbs() == []
    assert_no_store(confirm, refused)


RECREATE_FAILURES: dict[str, tuple[Arrange, dict[str, str], int, str]] = {
    "name changed": (nothing, {"expected_name": "other"}, CONFLICT, "The container changed"),
    "rolled back": (
        lambda host: host.fail("create", 400),
        {"expected_name": NAME},
        SEE_OTHER,
        "The container was not recreated",
    ),
    "outcome unknown": (
        lambda host: host.route("POST", f"/containers/{OLD}/stop", Stall()),
        {"expected_name": NAME},
        SEE_OTHER,
        "The result of the recreate is not known",
    ),
}


async def test_a_failed_recreate_shows_why_with_a_way_back_and_no_engine_text(
    admin_client: AsyncClient, world: World, recreatable: ContainerEngine
) -> None:
    holder = LockHolder(UserActor(999), "someone-else", "restart")
    async with world.app.state.engine.locks.hold(
        ObjectKey(world.remote_id, "container", OLD), holder
    ):
        busy = await admin_client.post(
            recreate_path(world), data=csrf_form(admin_client, expected_name=NAME)
        )
    answers = {
        label: await recreate_with(admin_client, world, recreatable, arrange, **fields)
        for label, (arrange, fields, _, _) in RECREATE_FAILURES.items()
    }
    pages = {label: await shown(admin_client, answer) for label, answer in answers.items()}

    assert busy.status_code == CONFLICT
    assert "This container is busy" in busy.text and "someone-else" in busy.text
    assert {label: answer.status_code for label, answer in answers.items()} == {
        label: status for label, (_, _, status, _) in RECREATE_FAILURES.items()
    }
    assert {
        label: heading in unescape(pages[label].text)
        for label, (_, _, _, heading) in RECREATE_FAILURES.items()
    } == dict.fromkeys(RECREATE_FAILURES, True)
    assert all("Review again" in page.text for page in pages.values())
    assert [label for label, page in pages.items() if ENGINE_TEXT in page.text] == ["rolled back"]
    assert not any("registry detail" in page.text for page in pages.values())
    assert recreatable.boxes[OLD].state == "running" and recreatable.boxes[OLD].name == NAME
    assert_no_store(busy, *answers.values())


async def test_every_post_of_the_create_pages_needs_the_csrf_token(
    admin_client: AsyncClient, world: World, recreatable: ContainerEngine
) -> None:
    paths = {
        "new": new_path(world),
        "create": create_path(world),
        "recreate": recreate_path(world),
    }
    body = {**create_form(), "expected_name": NAME}
    bodies = {"no token": body, "wrong token": body | {"csrf_token": WRONG_TOKEN}}

    statuses = {
        (target, kind): (await admin_client.post(path, data=data)).status_code
        for target, path in paths.items()
        for kind, data in bodies.items()
    }

    assert set(statuses.values()) == {FORBIDDEN}
    assert len(statuses) == 6
    assert recreatable.mutations() == []


async def test_a_create_blocked_by_another_action_on_an_object_it_uses_is_a_409_review_page(
    admin_client: AsyncClient,
    world: World,
    recreatable: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def busy(*arguments: object, **options: object) -> None:
        raise ObjectBusy(LockHolder(UserActor(999), "someone-else", "remove"))

    monkeypatch.setattr(ContainerCreateService, "create", busy)

    response = await admin_client.post(
        create_path(world), data=csrf_form(admin_client, **create_form())
    )

    assert response.status_code == CONFLICT
    assert "started a remove on the image or volume this container uses" in response.text
    assert "Internal Server Error" not in response.text
    assert recreatable.mutations() == []


async def test_a_create_refused_with_an_unmapped_validation_error_is_a_400_form_not_a_500(
    admin_client: AsyncClient,
    world: World,
    recreatable: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def invalid(*arguments: object, **options: object) -> None:
        raise ValidationFailed({"image": "The image name is not usable."})

    monkeypatch.setattr(ContainerCreateService, "create", invalid)

    response = await admin_client.post(
        create_path(world), data=csrf_form(admin_client, **create_form())
    )

    assert response.status_code == BAD_REQUEST
    assert "The image name is not usable." in response.text
