"""The sidebar: which environments and links each role sees, and which item a page marks current."""

import asyncio
import re
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from starlette.requests import Request

import app.web.environment_routes as environment_routes
import app.web.sidebar as sidebar
from app.auth.principal import Principal
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.environment_snapshot import EnvironmentSnapshotRepository, SnapshotCounts
from app.services.environment_snapshots import SnapshotPokes
from app.web.nav_keys import navigation_key
from app.web.sidebar import environment_id_of
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    STANDARD_ID,
    Browsers,
    csrf_form,
)
from tests.container_support import WEB
from tests.docker_engine.container_engine import ContainerEngine
from tests.web.environment_support import (
    DOWN_NAME,
    LOCAL_CONTAINERS,
    LOCAL_NAME,
    REMOTE_NAME,
    World,
    hold_unreachable,
    rows,
)
from tests.web.helpers import hx_triggers, parse_tags
from tests.web.html_tree import parse
from tests.web.image_support import assert_no_store

NOT_FOUND = 404
HTMX = {"HX-Request": "true"}
NO_CONTENT = 204
REFRESHED_CONTAINERS = 99
BOUND_SECONDS = 0.01
COUNTS_CHANGED = "counts-changed"
CURRENT_LINK = re.compile(r'class="nav__link" href="([^"]+)" aria-current="page"')
SWITCHER_OPTION = re.compile(r'class="env-switch__option"[^>]*>.*?<span>([^<]+)</span>', re.S)
ADMINISTRATOR_LINKS = ("/groups", "/tags", "/users", "/registries", "/jobs", "/backups")


def _request_with(environment_id: str) -> Request:
    return Request({"type": "http", "path_params": {"environment_id": environment_id}})


@pytest.mark.parametrize(
    ("route_name", "key"),
    [
        ("index", "environments"),
        ("environment_detail", "dashboard"),
        ("environment_new", "environments"),
        ("environment_delete_confirm", "environments"),
        ("environment_container_detail", "containers"),
        ("environment_image_pull", "images"),
        ("environment_swarm_overview", "swarm"),
        ("environment_swarm_task_detail", "services"),
        ("environment_swarm_stack_list", "swarm-stacks"),
        ("environment_stack_list", "stacks"),
        ("registry_detail", "registries"),
        ("restore_review", "backups"),
        ("user_token_revoke", "users"),
        ("tokens", "tokens"),
        ("login", ""),
        ("", ""),
    ],
)
def test_navigation_key_maps_a_route_to_its_sidebar_item(route_name: str, key: str) -> None:
    assert navigation_key(route_name) == key


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12", 12),
        ("0", None),
        ("-1", None),
        ("abc", None),
        ("١٢", None),
        ("9" * 20, None),
        (str(2**63), None),
    ],
)
def test_environment_id_accepts_only_a_positive_decimal_id(raw: str, expected: int | None) -> None:
    assert environment_id_of(_request_with(raw)) == expected


async def test_an_administrator_sidebar_lists_every_environment_with_counts(
    admin_client: AsyncClient, world: World
) -> None:
    page = await admin_client.get(f"/environments/{world.local_id}")

    assert SWITCHER_OPTION.findall(page.text) == sorted([LOCAL_NAME, REMOTE_NAME, DOWN_NAME])
    assert f'<span class="nav__count">{LOCAL_CONTAINERS}</span>' in page.text
    assert CURRENT_LINK.findall(page.text) == [f"/environments/{world.local_id}"]
    assert all(f'href="{path}"' in page.text for path in ADMINISTRATOR_LINKS)


async def test_a_standard_user_sidebar_lists_only_their_environments_without_counts(
    standard_client: AsyncClient, world: World
) -> None:
    page = await standard_client.get(f"/environments/{world.local_id}/containers")

    assert SWITCHER_OPTION.findall(page.text) == [LOCAL_NAME]
    assert REMOTE_NAME not in page.text and DOWN_NAME not in page.text
    assert re.findall(r'class="nav__count">(\d+)<', page.text) == ["1"]
    assert CURRENT_LINK.findall(page.text) == [f"/environments/{world.local_id}/containers"]
    assert not any(f'href="{path}"' in page.text for path in ADMINISTRATOR_LINKS)


def sidebar_groups(html: str) -> dict[str, list[str]]:
    """Map each sidebar group to the labels of its links, in order."""
    return {
        group.attrs["data-nav-group"]: [link.text() for link in group.walk() if link.tag == "a"]
        for group in parse(html).walk()
        if group.tag == "details" and "data-nav-group" in group.attrs
    }


async def test_jobs_and_activity_sit_under_administration_and_not_under_operations(
    admin_client: AsyncClient, world: World
) -> None:
    page = await admin_client.get("/")

    groups = sidebar_groups(page.text)
    assert groups["operations"] == ["Automation", "Templates"]
    assert groups["admin"][:2] == ["Jobs", "Activity"]
    assert "Jobs" not in groups["operations"] and "Activity" not in groups["operations"]


async def test_a_standard_user_sidebar_has_no_administration_group_and_no_jobs_or_activity(
    standard_client: AsyncClient, world: World
) -> None:
    page = await standard_client.get("/")

    groups = sidebar_groups(page.text)
    assert "admin" not in groups
    assert groups["operations"] == ["Automation", "Templates"]
    assert not any(link in ("Jobs", "Activity") for links in groups.values() for link in links)


async def test_a_denied_environment_never_reaches_the_sidebar(
    standard_client: AsyncClient, world: World
) -> None:
    page = await standard_client.get(f"/environments/{world.remote_id}")

    assert page.status_code == NOT_FOUND
    assert REMOTE_NAME not in page.text
    assert f"/environments/{world.remote_id}/" not in page.text


class GatedRefresh:
    """A snapshot service whose refresh holds until released, then records new counts."""

    def __init__(self, world: World) -> None:
        self._world = world
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def refresh_one(self, environment_id: int) -> None:
        self.entered.set()
        await self.release.wait()
        stamp = format_timestamp(self._world.app.state.auth.clock())
        async with self._world.app.state.database.session() as db:
            await EnvironmentSnapshotRepository(db).record_counts(
                environment_id, stamp, SnapshotCounts(container_count=REFRESHED_CONTAINERS)
            )

    async def aclose(self) -> None:
        """Nothing is held."""


@pytest.fixture
async def pending(world: World) -> AsyncIterator[tuple[SnapshotPokes, GatedRefresh]]:
    """A refresh queue holding one refresh of the local environment open."""
    service = GatedRefresh(world)
    pokes = SnapshotPokes(service)
    world.app.state.engine.refresher = pokes
    pokes.poke(world.local_id)
    await service.entered.wait()
    yield pokes, service
    service.release.set()
    await pokes.aclose()


def counts_path(world: World, environment_id: int | None = None, active: str = "") -> str:
    return f"/environments/{environment_id or world.local_id}/navigation-counts?active={active}"


def navigation_list(html: str) -> dict[str, str]:
    [tag] = [tag.attrs for tag in parse_tags(html) if tag.name == "ul" and "hx-get" in tag.attrs]
    return tag


async def test_the_counts_fragment_renders_the_environments_list_and_marks_only_a_known_key(
    admin_client: AsyncClient, world: World
) -> None:
    marked = await admin_client.get(counts_path(world, active="images"))
    hostile = await admin_client.get(counts_path(world, active="<script>"))
    unknown = await admin_client.get(counts_path(world, active="nothing"))

    assert marked.status_code == 200 and "<html" not in marked.text
    assert f'<span class="nav__count">{LOCAL_CONTAINERS}</span>' in marked.text
    assert CURRENT_LINK.findall(marked.text) == [f"/environments/{world.local_id}/images"]
    assert "aria-current" not in hostile.text and "<script>" not in hostile.text
    assert "aria-current" not in unknown.text
    assert navigation_list(marked.text)["hx-trigger"] == "counts-changed from:body"
    assert_no_store(marked)


async def test_the_counts_fragment_waits_for_a_refresh_that_is_pending_and_shows_its_result(
    admin_client: AsyncClient,
    world: World,
    pending: tuple[SnapshotPokes, GatedRefresh],
) -> None:
    _, service = pending

    request = asyncio.ensure_future(admin_client.get(counts_path(world)))
    await asyncio.sleep(0)
    service.release.set()
    response = await request

    assert f'<span class="nav__count">{REFRESHED_CONTAINERS}</span>' in response.text


async def test_the_counts_fragment_gives_up_waiting_after_its_bound_and_shows_what_is_stored(
    admin_client: AsyncClient,
    world: World,
    pending: tuple[SnapshotPokes, GatedRefresh],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(environment_routes, "COUNTS_WAIT_SECONDS", BOUND_SECONDS)

    response = await admin_client.get(counts_path(world))

    assert response.status_code == 200
    assert f'<span class="nav__count">{LOCAL_CONTAINERS}</span>' in response.text


async def test_the_counts_fragment_is_empty_for_a_standard_user_and_missing_for_an_unknown_one(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World
) -> None:
    standard = await standard_client.get(counts_path(world))
    standard_unknown = await standard_client.get(counts_path(world, 999999))
    admin_unknown = await admin_client.get(counts_path(world, 999999))

    assert (standard.status_code, standard_unknown.status_code) == (NO_CONTENT, NO_CONTENT)
    assert standard.content == b"" and standard_unknown.content == b""
    assert admin_unknown.status_code == NOT_FOUND
    assert_no_store(standard)


async def test_the_sidebar_loads_counts_at_once_only_while_a_refresh_is_pending(
    admin_client: AsyncClient,
    world: World,
    pending: tuple[SnapshotPokes, GatedRefresh],
) -> None:
    pokes, service = pending
    page_path = f"/environments/{world.local_id}"

    during = await admin_client.get(page_path)
    service.release.set()
    await pokes.wait_settled(world.local_id, BOUND_SECONDS * 100)
    after = await admin_client.get(page_path)

    assert navigation_list(during.text)["hx-trigger"] == "counts-changed from:body, load"
    assert navigation_list(after.text)["hx-trigger"] == "counts-changed from:body"


async def test_the_sidebar_of_a_standard_user_does_not_listen_for_count_changes(
    standard_client: AsyncClient, world: World
) -> None:
    page = await standard_client.get(f"/environments/{world.local_id}")

    assert "navigation-counts" not in page.text
    assert COUNTS_CHANGED not in page.text


async def test_a_mutating_htmx_response_announces_changed_counts_and_a_picker_fragment_does_not(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    base = f"/environments/{world.remote_id}/containers"

    stopped = await admin_client.post(
        f"{base}/{WEB}/actions/stop", data=csrf_form(admin_client), headers=HTMX
    )
    picked = await admin_client.post(
        f"{base}/new/choices", data=csrf_form(admin_client, image="nginx:1"), headers=HTMX
    )

    assert stopped.status_code == 200 and COUNTS_CHANGED in hx_triggers(stopped)
    assert picked.status_code == 200 and COUNTS_CHANGED not in hx_triggers(picked)


NO_BLOCK = "Choose an environment"
WATCH_NAVIGATION_WRITES = (
    "CREATE TABLE nav_writes (session_id INTEGER, value INTEGER)",
    "CREATE TRIGGER nav_watch AFTER UPDATE OF nav_environment_id ON session "
    "BEGIN INSERT INTO nav_writes (session_id, value) VALUES (NEW.id, NEW.nav_environment_id); END",
)
USERS_PATH = "/users"
AUTOMATION_PATH = "/automation"
ENVIRONMENT_LINK_SUFFIXES = ("", "/containers", "/images", "/volumes", "/networks", "/stacks")


@pytest.fixture
async def watched(world: World) -> World:
    """The seeded world with both environment pages free of host reads and session writes logged."""
    await hold_unreachable(world, world.local_id)
    await hold_unreachable(world, world.remote_id)
    async with world.app.state.database.session() as db:
        for statement in WATCH_NAVIGATION_WRITES:
            await db.execute(statement)
    return world


async def remembered_by(world: World, user_id: int) -> list[int | None]:
    found = await rows(
        world, "SELECT nav_environment_id FROM session WHERE user_id = ? ORDER BY id", (user_id,)
    )
    return [row["nav_environment_id"] for row in found]


async def navigation_writes(world: World) -> list[int | None]:
    found = await rows(world, "SELECT value FROM nav_writes ORDER BY rowid")
    return [row["value"] for row in found]


def switcher_shows(html: str, name: str) -> bool:
    return f"Environment {name}. Switch environment" in html


def environment_links(html: str, environment_id: int) -> list[str]:
    return [
        f"/environments/{environment_id}{suffix}"
        for suffix in ENVIRONMENT_LINK_SUFFIXES
        if f'href="/environments/{environment_id}{suffix}"' in html
    ]


async def test_viewing_an_environment_stores_it_once_and_viewing_another_replaces_it(
    admin_client: AsyncClient, watched: World
) -> None:
    local, remote = f"/environments/{watched.local_id}", f"/environments/{watched.remote_id}"

    await admin_client.get(local)
    after_first = await remembered_by(watched, ADMIN_ID)
    await admin_client.get(local)
    await admin_client.get(local)
    writes_after_repeats = await navigation_writes(watched)
    await admin_client.get(remote)

    assert after_first == [watched.local_id]
    assert writes_after_repeats == [watched.local_id]
    assert await navigation_writes(watched) == [watched.local_id, watched.remote_id]
    assert await remembered_by(watched, ADMIN_ID) == [watched.remote_id]


async def test_the_users_and_an_operations_page_show_the_remembered_environment_and_mark_themselves(
    admin_client: AsyncClient, watched: World
) -> None:
    await admin_client.get(f"/environments/{watched.local_id}")
    watched.engine_clients.clear()
    watched.poker.poked.clear()

    users = await admin_client.get(USERS_PATH)
    automation = await admin_client.get(AUTOMATION_PATH)

    for page, own_link in ((users, USERS_PATH), (automation, AUTOMATION_PATH)):
        assert page.status_code == 200
        assert switcher_shows(page.text, LOCAL_NAME)
        assert environment_links(page.text, watched.local_id) == [
            f"/environments/{watched.local_id}{suffix}" for suffix in ENVIRONMENT_LINK_SUFFIXES
        ]
        assert f'<span class="nav__count">{LOCAL_CONTAINERS}</span>' in page.text
        assert CURRENT_LINK.findall(page.text) == [own_link]
    assert watched.engine_clients == []
    assert watched.poker.poked == []
    assert await navigation_writes(watched) == [watched.local_id]


async def test_a_standard_user_whose_access_is_removed_gets_no_block_and_the_value_is_cleared(
    standard_client: AsyncClient, watched: World
) -> None:
    await standard_client.get(f"/environments/{watched.local_id}")
    async with watched.app.state.database.session() as db:
        await EnvironmentAccessRepository(db).remove_user(
            "environment", watched.local_id, STANDARD_ID
        )

    page = await standard_client.get("/")

    assert NO_BLOCK in page.text
    assert LOCAL_NAME not in page.text
    assert await remembered_by(watched, STANDARD_ID) == [None]


async def test_an_administrator_whose_remembered_environment_is_deleted_gets_no_block(
    admin_client: AsyncClient, watched: World
) -> None:
    await admin_client.get(f"/environments/{watched.remote_id}")
    async with watched.app.state.database.session() as db:
        await db.execute("DELETE FROM environment WHERE id = ?", (watched.remote_id,))

    page = await admin_client.get(USERS_PATH)

    assert NO_BLOCK in page.text
    assert REMOTE_NAME not in page.text
    assert await remembered_by(watched, ADMIN_ID) == [None]


async def test_a_new_session_starts_without_one_and_two_sessions_remember_independently(
    admin_client: AsyncClient, browsers: Browsers, watched: World
) -> None:
    second = await browsers.sign_in(ADMIN_USERNAME, ADMIN_PASSWORD)

    fresh = await second.get(USERS_PATH)
    await admin_client.get(f"/environments/{watched.local_id}")
    await second.get(f"/environments/{watched.remote_id}")
    first_users = await admin_client.get(USERS_PATH)
    second_users = await second.get(USERS_PATH)

    assert NO_BLOCK in fresh.text
    assert await remembered_by(watched, ADMIN_ID) == [watched.local_id, watched.remote_id]
    assert switcher_shows(first_users.text, LOCAL_NAME)
    assert switcher_shows(second_users.text, REMOTE_NAME)


async def test_an_htmx_fragment_neither_reads_nor_writes_the_remembered_environment(
    admin_client: AsyncClient, watched: World
) -> None:
    await admin_client.get(f"/environments/{watched.local_id}")

    fragment = await admin_client.get(f"/environments/{watched.remote_id}", headers=HTMX)

    assert fragment.status_code == 200
    assert not switcher_shows(fragment.text, REMOTE_NAME)
    assert await remembered_by(watched, ADMIN_ID) == [watched.local_id]
    assert await navigation_writes(watched) == [watched.local_id]


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        pytest.param("/users", HTMX, id="htmx-fragment"),
        pytest.param("/api/whoami", {"authorization": "Bearer dkm_placeholder"}, id="bearer"),
    ],
)
async def test_resolve_navigation_loads_and_stores_nothing_for_a_fragment_or_a_bearer_request(
    monkeypatch: pytest.MonkeyPatch, path: str, headers: dict[str, str]
) -> None:
    async def forbidden(*_: object) -> None:
        raise AssertionError("the sidebar was loaded")

    monkeypatch.setattr(sidebar, "load_navigation", forbidden)
    monkeypatch.setattr(sidebar, "remember_navigation", forbidden)
    request = Request(
        {
            "type": "http",
            "path": path,
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
            "app": SimpleNamespace(state=SimpleNamespace(database=object())),
        }
    )
    request.state.principal = Principal(STANDARD_ID, "user", "standard", 1, None, 1)

    await sidebar.resolve_navigation(request)

    assert request.state.navigation is None
