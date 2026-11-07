"""Seeded environments for the web and API tests, and the helpers that read what they return."""

import re
from dataclasses import dataclass
from html import unescape
from typing import Any, Literal

from fastapi import FastAPI
from httpx import AsyncClient
from starlette.routing import Route

from app.db.engine import Session
from app.db.repositories.clocked import format_timestamp, utc_now
from app.db.repositories.environment import EnvironmentRepository
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.environment_group import EnvironmentGroupRepository
from app.db.repositories.environment_snapshot import EnvironmentSnapshotRepository, SnapshotCounts
from app.db.repositories.environment_tls import EnvironmentTlsRepository
from app.db.repositories.tag import TagRepository
from app.docker_engine.registry import EngineClientRegistry
from app.docker_engine.transport import ConnectionSettings, TransportBundle
from tests.auth_support import STANDARD_ID, csrf_form
from tests.docker_engine.tls_material import Material, valid_material
from tests.environment_support import RecordingPoker, ScriptedProbes, seed_environment
from tests.web.html_tree import Node, parse

TOKEN_SHAPE = re.compile(r"dkm_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{43}")
TOKENS_PATH = "/account/tokens"
MIN_PEM_LINE = 20

LOCAL_NAME = "local-box"
REMOTE_NAME = "remote-box"
DOWN_NAME = "down-box"
GROUP_NAME = "production"
TAG_NAME = "blue"
OTHER_TAG_NAME = "green"
LOCAL_SOCKET = "/run/local-box.sock"
REMOTE_ENDPOINT = "tcp://remote.example.test:2376"
DOWN_ENDPOINT = "tcp://down.example.test:2376"
LOCAL_CONTAINERS, LOCAL_RUNNING, LOCAL_IMAGES = 7, 4, 3
LOCAL_PAUSED, LOCAL_UNHEALTHY = 1, 2
REMOTE_CONTAINERS = 11
DOWN_ERROR = "The host could not be counted."
DOWN_MESSAGE = "The connection was refused."


@dataclass(slots=True)
class World:
    """The environments, group and tag a test starts from, and the fakes that watch the Engine."""

    app: FastAPI
    local_id: int
    remote_id: int
    down_id: int
    group_id: int
    tag_id: int
    other_tag_id: int
    material: Material
    probes: ScriptedProbes
    poker: RecordingPoker
    engine_clients: list[ConnectionSettings]


class EngineTripwire(EngineClientRegistry):
    """A registry that builds no client and records every attempt to."""

    def __init__(self, attempts: list[ConnectionSettings]) -> None:
        def refuse(settings: ConnectionSettings) -> TransportBundle:
            attempts.append(settings)
            raise AssertionError("an Engine client was built")

        super().__init__(refuse)


def pem_lines(*pems: str) -> list[str]:
    """Return the base64 body lines of PEM items, which must never appear in any output."""
    return [
        line
        for pem in pems
        for line in pem.splitlines()
        if len(line) >= MIN_PEM_LINE and not line.startswith("-----")
    ]


def material_lines(material: Material) -> list[str]:
    return pem_lines(material.ca, material.cert, material.key)


async def issue_token(client: AsyncClient) -> str:
    """Create a token as the signed-in user would, and return the value the page showed."""
    response = await client.post(TOKENS_PATH, data=csrf_form(client, label="tests"))
    found = TOKEN_SHAPE.search(response.text)
    assert found is not None, response.text
    return found.group(0)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def seed_world(app: FastAPI) -> World:
    """Add a local, a remote and an unreachable environment, with a group, tags and snapshots."""
    clock = app.state.auth.clock
    stamp = format_timestamp(clock())
    material = valid_material()
    async with app.state.database.session() as db:
        local = await seed_environment(db, clock, LOCAL_NAME, endpoint=LOCAL_SOCKET)
        remote = await seed_environment(
            db, clock, REMOTE_NAME, kind="tcp_tls", endpoint=REMOTE_ENDPOINT
        )
        down = await seed_environment(
            db, clock, DOWN_NAME, kind="tcp_tls", endpoint=DOWN_ENDPOINT, status="unreachable"
        )
        await db.execute(
            "UPDATE environment SET failed_stage = 'tcp', status_message = ? WHERE id = ?",
            (DOWN_MESSAGE, down.id),
        )
        tls = EnvironmentTlsRepository(db, app.state.vault, clock)
        for environment_id in (remote.id, down.id):
            await tls.replace(
                environment_id,
                ca_certificate=material.ca,
                client_certificate=material.cert,
                client_key=material.key,
            )
        snapshots = EnvironmentSnapshotRepository(db)
        await snapshots.record_counts(
            local.id,
            stamp,
            SnapshotCounts(
                container_count=LOCAL_CONTAINERS,
                container_running_count=LOCAL_RUNNING,
                container_stopped_count=LOCAL_CONTAINERS - LOCAL_RUNNING,
                container_paused_count=LOCAL_PAUSED,
                container_unhealthy_count=LOCAL_UNHEALTHY,
                image_count=LOCAL_IMAGES,
                volume_count=2,
                network_count=1,
            ),
        )
        await snapshots.record_counts(
            remote.id, stamp, SnapshotCounts(container_count=REMOTE_CONTAINERS)
        )
        await snapshots.record_error(down.id, stamp, DOWN_ERROR)
        group = await EnvironmentGroupRepository(db, clock).create(GROUP_NAME)
        await EnvironmentRepository(db, clock).set_group(local.id, group.id)
        tags = TagRepository(db, clock)
        blue, green = await tags.create(TAG_NAME), await tags.create(OTHER_TAG_NAME)
        await tags.replace_for_environment(local.id, {blue.id})
        await tags.replace_for_environment(down.id, {blue.id, green.id})
        await EnvironmentAccessRepository(db).add_user("environment", local.id, STANDARD_ID)
    return World(
        app=app,
        local_id=local.id,
        remote_id=remote.id,
        down_id=down.id,
        group_id=group.id,
        tag_id=blue.id,
        other_tag_id=green.id,
        material=material,
        probes=ScriptedProbes(),
        poker=RecordingPoker(),
        engine_clients=[],
    )


ENVIRONMENT_LINK = re.compile(r'<a href="/environments/\d+">([^<]+)</a>')
NEXT_LINK = re.compile(r'<a class="pagination__link" href="([^"]+)" rel="next"')


def listed_names(html: str) -> list[str]:
    """Return the environment names a list page links to, in page order."""
    return [unescape(name) for name in ENVIRONMENT_LINK.findall(html)]


def next_page_path(html: str) -> str | None:
    """Return the path of the page's Next page link, or None on the last page."""
    found = NEXT_LINK.search(html)
    return None if found is None else unescape(found.group(1))


async def names_by_query(client: AsyncClient, queries: list[str]) -> dict[str, list[str]]:
    """Map each dashboard query string to the environment names it lists."""
    return {query: listed_names((await client.get(f"/?{query}")).text) for query in queries}


NEW_SOCKET = "/run/new-box.sock"
NEW_SOCKET_ENDPOINT = f"unix://{NEW_SOCKET}"
NEW_HOST = "new.example.test"
NEW_ENDPOINT = f"tcp://{NEW_HOST}:2376"


def local_form(client: AsyncClient, name: str = "new-box", **overrides: str) -> dict[str, str]:
    return (
        csrf_form(
            client,
            name=name,
            kind="unix_socket",
            socket_path=NEW_SOCKET,
            reviewed_endpoint=NEW_SOCKET_ENDPOINT,
        )
        | overrides
    )


def remote_form(
    client: AsyncClient, material: Material, name: str = "new-remote", **overrides: str
) -> dict[str, str]:
    return (
        csrf_form(
            client,
            name=name,
            kind="tcp_tls",
            host=NEW_HOST,
            port="2376",
            reviewed_endpoint=NEW_ENDPOINT,
            ca_certificate=material.ca,
            client_certificate=material.cert,
            client_key=material.key,
        )
        | overrides
    )


PATH_PARAMETER = re.compile(r"\{(\w+)\}")


def get_paths(app: FastAPI) -> list[str]:
    """Return every GET path: the documented ones, and the built-in documentation routes."""
    documented = {
        path for path, operations in app.openapi()["paths"].items() if "get" in operations
    }
    built_in = {
        route.path
        for route in app.routes
        if isinstance(route, Route) and "GET" in (route.methods or ())
    }
    return sorted(documented | built_in)


def filled(path: str, **ids: int) -> str:
    """Replace each path parameter with the id given for it, or 1."""
    return PATH_PARAMETER.sub(lambda found: str(ids.get(found.group(1), 1)), path)


async def rows(world: World, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    async with world.app.state.database.session() as db:
        found = await db.fetch_limited(sql, params, limit=100)
    return [dict(row) for row in found]


STACK_FILE = "services: {}"
FILE_DIGEST = "0" * 64


async def seed_stack_rows(
    db: Session, environment_id: int, *stacks: tuple[str, Literal["compose", "swarm"]]
) -> None:
    """Insert managed stack rows by name and kind; one call per environment."""
    now = format_timestamp(utc_now())
    for offset, (name, kind) in enumerate(stacks):
        await db.execute(
            "INSERT INTO stack (environment_id, name, kind, origin, project_directory, "
            "compose_file, compose_file_digest, created_at, updated_at) "
            "VALUES (?, ?, ?, 'created', ?, ?, ?, ?, ?)",
            (
                environment_id,
                name,
                kind,
                f"{environment_id:016x}{offset:016x}",
                STACK_FILE,
                FILE_DIGEST,
                now,
                now,
            ),
        )


@dataclass(frozen=True, slots=True)
class Figure:
    """One number of a stat card as the page shows it."""

    label: str
    value: str
    href: str


def cards_in(html: str) -> dict[str, list[Figure]]:
    """Map each stat card of an environment page to its figures, headline first."""
    cards: dict[str, list[Figure]] = {}
    for card in parse(html).walk():
        if card.tag == "li" and "stat-card" in card.classes():
            cards[card.attrs["data-card"]] = [
                Figure(
                    _text_of(figure, "stat-figure__label"),
                    _text_of(figure, "stat-figure__value"),
                    figure.attrs["href"],
                )
                for figure in card.walk()
                if figure.tag == "a" and "stat-figure" in figure.classes()
            ]
    return cards


def _text_of(node: Node, class_name: str) -> str:
    return next(n.text() for n in node.walk() if class_name in n.classes())


def stack_tiles_in(html: str) -> list[tuple[str, str, str, str]]:
    """Return (name, state, kind, href) for each tile of the environment page's stack grid."""
    return [
        (
            _text_of(tile, "stack-tile__name"),
            next(n.text() for n in tile.walk() if "badge" in n.classes()),
            _text_of(tile, "stack-tile__kind"),
            tile.attrs["href"],
        )
        for tile in parse(html).walk()
        if tile.tag == "a" and "stack-tile" in tile.classes()
    ]


OVERVIEW_CONTAINERS_CAPTION = "Containers of this environment"


def _captioned(table: Node, caption: str) -> bool:
    return any(
        isinstance(child, Node) and child.tag == "caption" and child.text() == caption
        for child in table.children
    )


def container_rows_in(html: str) -> list[list[str]]:
    """Return the cell texts of each row of the environment page's container table."""
    return [
        [cell.text() for cell in row.children if isinstance(cell, Node)]
        for table in parse(html).walk()
        if table.tag == "table" and _captioned(table, OVERVIEW_CONTAINERS_CAPTION)
        for body in table.children
        if isinstance(body, Node) and body.tag == "tbody"
        for row in body.children
        if isinstance(row, Node)
    ]


async def hold_unreachable(world: World, environment_id: int) -> None:
    """Store the environment as unreachable, so its page makes no read of the host."""
    async with world.app.state.database.session() as db:
        await db.execute(
            "UPDATE environment SET status = 'unreachable', failed_stage = 'tcp' WHERE id = ?",
            (environment_id,),
        )
