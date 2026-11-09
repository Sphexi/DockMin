"""The stack export pages: the Export action, the confirmation and the archive download."""

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator, MutableMapping
from contextlib import AsyncExitStack
from typing import Any

import pytest
from httpx import AsyncClient
from starlette.requests import ClientDisconnect

import app.services.stack_export as stack_export
import app.services.volume_export as volume_export
from app.docker_engine.registry import EngineClientRegistry
from app.services.export_manifest import parse_manifest
from app.services.export_sealing import MINIMUM_COST, open_sealed
from app.services.stack_export import review_digest
from app.services.stack_export_assembly import open_stack_export_service
from app.services.volume_export import ExportSlots
from app.web.stack_export_routes import _ExportResponse
from tests.auth_support import csrf_form
from tests.environment_support import activity_rows
from tests.object_support import add_container, hidden_values, visible_text, volume_mount
from tests.self_support import SELF_VOLUME, home_identity
from tests.stack_export_support import (
    COMPOSE_LABEL,
    CONTEXT,
    PASSPHRASE,
    PLAIN_FILE,
    PLAIN_NAME,
    PLAIN_VALUE,
    SECRET_FILE_BODY,
    SECRET_NAME,
    SECRET_VALUE,
    STACK,
    SWARM_LABEL,
    VOLUME,
    WEB_DIGEST,
    SwarmVolumeEngine,
    seed_export_stack,
    stocked,
)
from tests.volume_export_support import NOTE, NOTE_FILE, unpack
from tests.web.environment_support import World

OK, BAD_REQUEST, FORBIDDEN, NOT_FOUND, CONFLICT, TOO_MANY = 200, 400, 403, 404, 409, 429
NO_STORE = "no-store"


@pytest.fixture(autouse=True)
def cheap_sealing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stack_export, "DEFAULT_COST", MINIMUM_COST)


@pytest.fixture
async def export_host(world: World) -> AsyncIterator[SwarmVolumeEngine]:
    fake = stocked(SwarmVolumeEngine())
    registry = EngineClientRegistry(lambda settings: fake.bundle())
    world.app.state.engine.registry = registry
    yield fake
    fake.release_gates()
    await registry.aclose()


@pytest.fixture
async def swarm_manager(world: World, export_host: SwarmVolumeEngine) -> SwarmVolumeEngine:
    export_host.volume_labels[VOLUME] = {SWARM_LABEL: STACK}
    async with world.app.state.database.session() as db:
        await db.execute(
            "UPDATE environment SET type = 'swarm', swarm_state = 'active', "
            "swarm_control_available = 1 WHERE id = ?",
            (world.remote_id,),
        )
    return export_host


async def seed(world: World, kind: str = "compose", **options: Any) -> str:
    async with world.app.state.database.session() as db:
        stack = await seed_export_stack(
            db,
            world.app.state.vault,
            world.app.state.auth.clock,
            environment_id=world.remote_id,
            kind=kind,  # type: ignore[arg-type]
            **options,
        )
    return review_digest(stack)


def export_path(world: World, kind: str = "compose") -> str:
    base = f"/environments/{world.remote_id}"
    return (
        f"{base}/stacks/{STACK}/export"
        if kind == "compose"
        else f"{base}/swarm/stacks/{STACK}/export"
    )


def detail_path(world: World, kind: str = "compose") -> str:
    return export_path(world, kind).removesuffix("/export")


async def export_rows(world: World) -> list[dict[str, Any]]:
    async with world.app.state.database.session() as db:
        return [r for r in await activity_rows(db) if str(r["action"]).startswith("stack_export")]


def sealed_form(client: AsyncClient, digest: str, **fields: Any) -> dict[str, Any]:
    posted: dict[str, Any] = {
        "expected_digest": digest,
        "secrets": "sealed",
        "passphrase": PASSPHRASE,
        "passphrase_confirm": PASSPHRASE,
        "volume": [VOLUME],
    }
    return csrf_form(client, **(posted | fields))


@pytest.mark.parametrize("kind", ["compose", "swarm"])
async def test_the_detail_page_offers_export_to_an_administrator_only(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    swarm_manager: SwarmVolumeEngine,
    kind: str,
) -> None:
    await seed(world, kind)

    admin = await admin_client.get(detail_path(world, kind))
    standard = await standard_client.get(detail_path(world, kind))

    assert admin.status_code == OK
    assert f'href="{export_path(world, kind)}"' in admin.text and ">Export<" in admin.text
    assert "/export" not in standard.text


async def test_the_confirmation_names_what_goes_in_and_what_stays_out(
    admin_client: AsyncClient, world: World, export_host: SwarmVolumeEngine
) -> None:
    digest = await seed(world)
    add_container(
        export_host,
        1,
        "shop-web-1",
        state="running",
        labels={COMPOSE_LABEL: STACK},
        mounts=[volume_mount(VOLUME)],
    )

    page = await admin_client.get(export_path(world))

    text = visible_text(page)
    assert page.status_code == OK and page.headers["cache-control"] == NO_STORE
    for shown in (
        "stack/compose.yaml",
        f"stack/files/{PLAIN_FILE}",
        "stack/contexts/0",
        CONTEXT,
        PLAIN_NAME,
        SECRET_NAME,
        WEB_DIGEST,
        "./site",
        "shop-web-1",
        "can be inconsistent",
        "Nothing is stopped",
        "Resource controls and team grants",
        "Image bindings and credential sets",
        VOLUME,
    ):
        assert shown in text, shown
    assert hidden_values(page, "expected_digest") == [digest]
    assert hidden_values(page, "csrf_token") and 'method="post"' in page.text
    assert SECRET_VALUE not in page.text and SECRET_FILE_BODY.strip() not in page.text
    assert "hx-" not in page.text.split("<main", 1)[1]
    assert "create" not in export_host.verbs()


async def test_the_passphrase_is_asked_twice_and_never_filled_in(
    admin_client: AsyncClient, world: World, export_host: SwarmVolumeEngine
) -> None:
    await seed(world)

    page = await admin_client.get(export_path(world))

    inputs = [
        tag for tag in page.text.split("<input")[1:] if 'name="passphrase' in tag.split(">", 1)[0]
    ]
    assert len(inputs) == 2
    for tag in inputs:
        head = tag.split(">", 1)[0]
        assert 'type="password"' in head and 'autocomplete="new-password"' in head
        assert "value=" not in head


async def test_a_stack_without_secrets_is_not_asked_for_a_passphrase(
    admin_client: AsyncClient, world: World, export_host: SwarmVolumeEngine
) -> None:
    await seed(world, secrets=False)

    page = await admin_client.get(export_path(world))

    assert 'name="passphrase"' not in page.text and "no secret variables or files" in visible_text(
        page
    )


async def test_the_swarm_confirmation_states_what_a_cluster_cannot_give(
    admin_client: AsyncClient, world: World, swarm_manager: SwarmVolumeEngine
) -> None:
    await seed(world, "swarm")

    page = await admin_client.get(export_path(world, "swarm"))

    text = visible_text(page)
    assert page.status_code == OK and "Swarm secrets that exist only in the cluster" in text
    assert VOLUME in text and f"/swarm/stacks/{STACK}/export" in page.text


@pytest.mark.parametrize("method", ["get", "post"])
async def test_a_standard_user_gets_a_not_found_page_and_nothing_is_recorded(
    standard_client: AsyncClient, world: World, export_host: SwarmVolumeEngine, method: str
) -> None:
    digest = await seed(world)

    if method == "get":
        response = await standard_client.get(export_path(world))
    else:
        response = await standard_client.post(
            export_path(world), data=csrf_form(standard_client, expected_digest=digest)
        )

    assert response.status_code == NOT_FOUND
    assert await export_rows(world) == [] and "create" not in export_host.verbs()


async def test_the_post_downloads_an_archive_with_the_headers_of_an_attachment(
    admin_client: AsyncClient, world: World, export_host: SwarmVolumeEngine
) -> None:
    digest = await seed(world)

    response = await admin_client.post(export_path(world), data=sealed_form(admin_client, digest))

    members = unpack(response.content)
    manifest = parse_manifest(members["manifest.json"][1])
    disposition = response.headers["content-disposition"]
    assert response.status_code == OK
    assert disposition.startswith(f'attachment; filename="stack-{STACK}-')
    assert disposition.endswith('.tar.gz"')
    assert response.headers["cache-control"] == NO_STORE
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "application/gzip"
    assert (manifest.format_version, manifest.kind) == (1, "compose_stack")
    assert members[f"volumes/0/data/{NOTE_FILE}"][1] == NOTE
    assert manifest.sealed is not None
    assert (
        open_sealed(manifest.sealed, PASSPHRASE, manifest.archive_id)[f"variable:{SECRET_NAME}"]
        == SECRET_VALUE.encode()
    )
    assert PLAIN_VALUE in members["manifest.json"][1].decode()
    rows = await export_rows(world)
    assert [(r["action"], r["result"]) for r in rows] == [
        ("stack_export_started", "success"),
        ("stack_exported", "success"),
    ]
    assert export_host.helpers() == []


async def test_leaving_the_secrets_out_downloads_names_only(
    admin_client: AsyncClient, world: World, export_host: SwarmVolumeEngine
) -> None:
    digest = await seed(world)

    response = await admin_client.post(
        export_path(world),
        data=csrf_form(
            admin_client, expected_digest=digest, secrets="omitted", passphrase="", volume=[]
        ),
    )

    manifest = parse_manifest(unpack(response.content)["manifest.json"][1])
    assert response.status_code == OK and (manifest.secrets, manifest.sealed) == ("omitted", None)
    assert SECRET_VALUE.encode() not in response.content


async def test_a_swarm_stack_downloads_with_its_namespace_volume(
    admin_client: AsyncClient, world: World, swarm_manager: SwarmVolumeEngine
) -> None:
    digest = await seed(world, "swarm")

    response = await admin_client.post(
        export_path(world, "swarm"), data=sealed_form(admin_client, digest)
    )

    members = unpack(response.content)
    manifest = parse_manifest(members["manifest.json"][1])
    assert response.status_code == OK and manifest.kind == "swarm_stack"
    assert members[f"volumes/0/data/{NOTE_FILE}"][1] == NOTE


@pytest.mark.parametrize(
    "fields",
    [
        {"passphrase_confirm": "another passphrase 99"},
        {"passphrase": "short", "passphrase_confirm": "short"},
        {"secrets": "plain"},
    ],
    ids=["mismatch", "length", "unknown-choice"],
)
async def test_a_bad_passphrase_form_is_answered_without_the_passphrase_or_a_helper(
    admin_client: AsyncClient,
    world: World,
    export_host: SwarmVolumeEngine,
    fields: dict[str, str],
) -> None:
    digest = await seed(world)

    response = await admin_client.post(
        export_path(world), data=sealed_form(admin_client, digest, **fields)
    )

    assert response.status_code == BAD_REQUEST
    assert PASSPHRASE not in response.text and "content-disposition" not in response.headers
    assert "create" not in export_host.verbs()


async def test_a_stack_that_changed_since_the_review_is_answered_with_a_page_to_review_again(
    admin_client: AsyncClient, world: World, export_host: SwarmVolumeEngine
) -> None:
    await seed(world)

    response = await admin_client.post(export_path(world), data=sealed_form(admin_client, "0" * 64))

    assert response.status_code == CONFLICT
    assert "changed" in visible_text(response).lower() and "Review again" in response.text
    assert "content-disposition" not in response.headers and "create" not in export_host.verbs()


async def test_a_busy_instance_answers_429_with_a_retry_header(
    admin_client: AsyncClient,
    world: World,
    export_host: SwarmVolumeEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = await seed(world)
    full = ExportSlots(per_environment=1, per_instance=1)
    full.acquire(world.remote_id)
    monkeypatch.setattr(volume_export, "_SLOTS", full)

    response = await admin_client.post(export_path(world), data=sealed_form(admin_client, digest))

    assert response.status_code == TOO_MANY and response.headers["retry-after"]
    assert "create" not in export_host.verbs()


async def test_dockmins_own_stack_and_volume_are_refused(
    admin_client: AsyncClient, world: World, export_host: SwarmVolumeEngine
) -> None:
    digest = await seed(world)
    export_host.add_volume(SELF_VOLUME, labels={COMPOSE_LABEL: STACK})
    known = {world.remote_id: frozenset({"/srv"})}
    world.app.state.self_identity = dataclasses.replace(
        home_identity(world.remote_id, volumes=(SELF_VOLUME,)), mount_sources=known
    )

    page = await admin_client.get(export_path(world))
    refused = await admin_client.post(
        export_path(world), data=sealed_form(admin_client, digest, volume=[SELF_VOLUME])
    )
    world.app.state.self_identity = dataclasses.replace(
        home_identity(world.remote_id, compose_project=STACK), mount_sources=known
    )
    own = await admin_client.get(export_path(world))

    assert "dockmin runs in has this volume mounted" in visible_text(page)
    assert refused.status_code == CONFLICT and own.status_code == CONFLICT
    assert "contains the container dockmin runs in" in visible_text(own)
    assert "create" not in export_host.verbs()


async def test_a_response_that_the_client_abandons_releases_the_export(
    world: World, export_host: SwarmVolumeEngine
) -> None:
    from tests.access_support import ADMIN
    from tests.volume_export_support import volume_tar

    digest = await seed(world)
    export_host.serve_volume(VOLUME, volume_tar({"big": b"x" * (1 << 20)}))
    slots = ExportSlots()
    holder = AsyncExitStack()
    service = await holder.enter_async_context(open_stack_export_service(world.app))
    service._slots = slots
    stream = await service.export(
        ADMIN, world.remote_id, "compose", STACK, digest, PASSPHRASE, [VOLUME]
    )
    response = _ExportResponse(stream, holder, "stack.tar.gz")
    sent: list[MutableMapping[str, Any]] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.body":
            raise OSError("client went away")

    async def receive() -> MutableMapping[str, Any]:
        await asyncio.Event().wait()
        return {}

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "POST", "headers": []}
    with pytest.raises(ClientDisconnect):
        await response(scope, receive, send)

    assert export_host.helpers() == [] and slots.acquire(world.remote_id) is not None
    finished = [r for r in await export_rows(world) if r["action"] == "stack_exported"]
    assert [json.dumps(r["detail"]["completed"]) for r in finished] == ["false"]
