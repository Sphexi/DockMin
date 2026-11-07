"""No route, log line, event or snapshot ever carries a line of an uploaded certificate or key."""

import io
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.crypto.envelope import key_version_of
from app.db.repositories.environment_tls import EnvironmentTlsRepository
from tests.auth_support import Browsers, csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.tls_material import Material, valid_material
from tests.web.environment_support import (
    NEW_ENDPOINT,
    World,
    bearer,
    filled,
    get_paths,
    issue_token,
    local_form,
    material_lines,
    pem_lines,
    remote_form,
)

MIN_GET_PATHS = 30
MIN_MARKER_LINES = 8
MIN_SUCCESSFUL_RESPONSES = 40
HTMX = {"HX-Request": "true"}
REDIRECT = 303


async def scan_get_routes(
    app: FastAPI, clients: dict[str, AsyncClient], world: World, extra_ids: list[int]
) -> tuple[dict[str, Response], int]:
    paths = get_paths(app)
    environments = [world.local_id, world.remote_id, world.down_id, *extra_ids]
    ids = {"group_id": world.group_id, "tag_id": world.tag_id}
    targets = [
        filled(path, environment_id=environment_id, **ids)
        for path in paths
        for environment_id in (environments if "{environment_id}" in path else [1])
    ]
    responses: dict[str, Response] = {}
    for who, client in clients.items():
        for target in targets:
            responses[f"{who} GET {target}"] = await client.get(target)
            responses[f"{who} HTMX GET {target}"] = await client.get(target, headers=HTMX)
    return responses, len(paths)


async def post_every_flow(
    admin: AsyncClient, world: World, other: Material, new_id: int
) -> dict[str, Response]:
    remote = f"/environments/{world.remote_id}"
    broken = remote_form(admin, other, name="")
    elsewhere = remote_form(admin, other, reviewed_endpoint="tcp://elsewhere.example.test:2376")
    flows: dict[str, tuple[str, str, dict[str, Any]]] = {
        "test": ("POST", f"{remote}/test", {"data": csrf_form(admin)}),
        "test htmx": ("POST", f"{remote}/test", {"data": csrf_form(admin), "headers": HTMX}),
        "refresh": ("POST", f"{remote}/refresh", {"data": csrf_form(admin)}),
        "preview": ("POST", "/environments/preview", {"data": remote_form(admin, other)}),
        "preview htmx": (
            "POST",
            "/environments/preview",
            {"data": remote_form(admin, other), "headers": HTMX},
        ),
        "create refused for its name": ("POST", "/environments", {"data": broken}),
        "create refused for its endpoint": ("POST", "/environments", {"data": elsewhere}),
        "edit refused": ("POST", f"{remote}/edit", {"data": broken}),
        "edit refused for its endpoint": ("POST", f"{remote}/edit", {"data": elsewhere}),
        "delete confirmation": (
            "GET",
            f"/environments/delete?id={world.remote_id}&id={new_id}",
            {},
        ),
        "delete result": (
            "POST",
            "/environments/delete",
            {"data": csrf_form(admin) | {"id": [str(9999)], "expected_name": ["x"]}},
        ),
    }
    return {
        name: await admin.request(method, path, **arguments)
        for name, (method, path, arguments) in flows.items()
    }


async def test_no_response_log_event_or_snapshot_contains_a_line_of_the_uploaded_material(
    started_app: FastAPI,
    world: World,
    browsers: Browsers,
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    log_stream: io.StringIO,
    container_host: ContainerEngine,
) -> None:
    uploaded = valid_material("ed25519")
    created = await admin_client.post("/environments", data=remote_form(admin_client, uploaded))
    new_id = int(created.headers["location"].rsplit("/", 1)[1])
    token = await issue_token(admin_client)
    caller = browsers.open()
    markers = [*material_lines(world.material), *material_lines(uploaded)]

    scanned, route_count = await scan_get_routes(
        started_app,
        {"administrator": admin_client, "standard user": standard_client},
        world,
        [new_id],
    )
    posted = await post_every_flow(admin_client, world, uploaded, new_id)
    api = {
        f"bearer GET {path}": await caller.get(path, headers=bearer(token))
        for path in (
            "/api/v1/environments",
            *(
                f"/api/v1/environments/{i}"
                for i in (world.local_id, world.remote_id, world.down_id, new_id)
            ),
        )
    }

    responses = scanned | posted | api
    leaking = [name for name, r in responses.items() if any(line in r.text for line in markers)]
    async with started_app.state.database.session() as db:
        events = await db.fetch_limited("SELECT detail FROM activity_event", (), limit=1000)
        snapshots = await db.fetch_limited("SELECT * FROM environment_snapshot", (), limit=100)
        stored = await db.fetch_limited(
            "SELECT environment_id, client_key_ciphertext FROM environment_tls_material",
            (),
            limit=10,
        )
        revealed = await EnvironmentTlsRepository(
            db, started_app.state.vault, started_app.state.auth.clock
        ).reveal_client_key(new_id)
    stored_text = " ".join(str(tuple(row)) for row in stored)

    assert created.status_code == REDIRECT
    assert len(markers) >= MIN_MARKER_LINES
    assert route_count >= MIN_GET_PATHS
    assert sum(1 for r in responses.values() if r.status_code == 200) >= MIN_SUCCESSFUL_RESPONSES
    assert leaking == []
    assert not any(line in log_stream.getvalue() for line in markers)
    assert not any(line in str(tuple(row)) for row in events for line in markers)
    assert not any(line in str(tuple(row)) for row in snapshots for line in markers)
    assert len(stored) == 3
    assert {key_version_of(row["client_key_ciphertext"]) for row in stored} == {1}
    assert not any(line in stored_text for line in pem_lines(world.material.key, uploaded.key))
    assert revealed == uploaded.key
    assert container_host.mutations() == []
    assert container_host.calls("GET", "/containers/json") != []
    assert NEW_ENDPOINT in (await admin_client.get(f"/environments/{new_id}")).text


async def test_a_local_environment_form_posted_with_pem_fields_does_not_echo_them(
    admin_client: AsyncClient, world: World
) -> None:
    form = local_form(
        admin_client, name="", ca_certificate=world.material.ca, client_key=world.material.key
    )

    response = await admin_client.post("/environments", data=form)

    assert response.status_code == 422
    assert not any(line in response.text for line in material_lines(world.material))
