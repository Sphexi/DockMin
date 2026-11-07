"""The dashboard and the environment detail page: what each role sees, and no Engine call."""

import re
import shutil
from html import unescape
from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient, Response

from app.compose_cli.binary import ComposeBinary
from app.db.repositories.environment import EnvironmentRepository
from app.logging import REQUEST_ID_HEADER
from app.services import compose_setup
from tests.auth_support import STANDARD_ID, csrf_form, outcomes
from tests.container_support import STACK
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.fake_engine import Refuse
from tests.image_support import ScriptedManifests
from tests.support import present
from tests.web.environment_support import (
    DOWN_ENDPOINT,
    DOWN_MESSAGE,
    DOWN_NAME,
    GROUP_NAME,
    LOCAL_CONTAINERS,
    LOCAL_NAME,
    LOCAL_SOCKET,
    REMOTE_CONTAINERS,
    REMOTE_ENDPOINT,
    REMOTE_NAME,
    Figure,
    World,
    cards_in,
    container_rows_in,
    hold_unreachable,
    listed_names,
    names_by_query,
    next_page_path,
    seed_stack_rows,
    stack_tiles_in,
)
from tests.web.flash_support import land
from tests.web.helpers import disclosures_in, resource_tiles_in
from tests.web.image_support import stock_images

OK = 200
BAD_REQUEST = 400
FORBIDDEN = 403
NOT_FOUND = 404
UNPROCESSABLE = 422
ABSENT_ID = 9999
HTMX = {"HX-Request": "true"}
ALL_NAMES = [DOWN_NAME, LOCAL_NAME, REMOTE_NAME]
LONG_AGO = "2020-01-01T00:00:00Z"
HOST_CONTAINERS, HOST_RUNNING, HOST_UNHEALTHY = 6, 3, 1


async def test_the_dashboard_lists_every_environment_and_shows_the_unreachable_one_as_a_state(
    admin_client: AsyncClient, world: World
) -> None:
    response = await admin_client.get("/")

    page = response.text
    assert response.status_code == OK
    assert listed_names(page) == ALL_NAMES
    assert "Unreachable" in page
    assert "Failed at TCP connection" in page
    assert "Counts unavailable: the last refresh failed" in page
    assert "Local socket" in page
    assert f"{LOCAL_CONTAINERS} <span" in page
    assert str(REMOTE_CONTAINERS) in page
    assert world.engine_clients == []
    assert world.probes.calls == []


async def test_the_dashboard_makes_no_engine_call_and_still_renders_when_no_snapshot_exists(
    admin_client: AsyncClient, world: World
) -> None:
    async with world.app.state.database.session() as db:
        await db.execute("DELETE FROM environment_snapshot")

    response = await admin_client.get("/")

    assert response.status_code == OK
    assert listed_names(response.text) == ALL_NAMES
    assert "Counts unavailable" not in response.text
    assert world.engine_clients == []
    assert world.probes.calls == []


async def test_each_filter_narrows_the_dashboard(admin_client: AsyncClient, world: World) -> None:
    queries = [
        "q=REMOTE",
        "status=unreachable",
        f"group={world.group_id}",
        "group=none",
        f"tag={world.tag_id}",
        f"tag={world.tag_id}&tag={world.other_tag_id}",
        f"q=box&status=reachable&tag={world.tag_id}",
    ]

    listed = await names_by_query(admin_client, queries)

    assert listed == {
        "q=REMOTE": [REMOTE_NAME],
        "status=unreachable": [DOWN_NAME],
        f"group={world.group_id}": [LOCAL_NAME],
        "group=none": [DOWN_NAME, REMOTE_NAME],
        f"tag={world.tag_id}": [DOWN_NAME, LOCAL_NAME],
        f"tag={world.tag_id}&tag={world.other_tag_id}": [DOWN_NAME],
        f"q=box&status=reachable&tag={world.tag_id}": [LOCAL_NAME],
    }


async def test_following_the_next_page_link_walks_the_list_without_repeats(
    admin_client: AsyncClient, world: World
) -> None:
    first = (await admin_client.get("/?limit=2")).text
    second_path = next_page_path(first)
    assert second_path is not None
    second = (await admin_client.get(second_path)).text

    assert listed_names(first) == ALL_NAMES[:2]
    assert listed_names(second) == ALL_NAMES[2:]
    assert next_page_path(second) is None


async def test_a_filter_the_list_cannot_honour_is_refused_and_never_a_server_error(
    admin_client: AsyncClient, world: World
) -> None:
    queries = [
        "status=bogus",
        "group=abc",
        "tag=x",
        f"q={'x' * 65}",
        f"group={world.group_id}&limit=0",
        "cursor=not-a-cursor",
    ]

    statuses = {query: (await admin_client.get(f"/?{query}")).status_code for query in queries}

    assert statuses == {
        "status=bogus": UNPROCESSABLE,
        "group=abc": UNPROCESSABLE,
        "tag=x": UNPROCESSABLE,
        f"q={'x' * 65}": UNPROCESSABLE,
        f"group={world.group_id}&limit=0": UNPROCESSABLE,
        "cursor=not-a-cursor": BAD_REQUEST,
    }


async def test_a_refused_filter_replaces_the_table_with_a_message_naming_the_problem(
    admin_client: AsyncClient, world: World
) -> None:
    response = await admin_client.get("/?status=bogus", headers=HTMX)

    assert response.status_code == UNPROCESSABLE
    assert "Check the filters" in response.text
    assert LOCAL_NAME not in response.text


async def test_an_htmx_request_gets_the_table_fragment_and_a_history_restore_gets_the_page(
    admin_client: AsyncClient, world: World
) -> None:
    fragment = await admin_client.get("/", headers=HTMX)
    restored = await admin_client.get("/", headers=HTMX | {"HX-History-Restore-Request": "true"})
    boosted = await admin_client.get("/", headers=HTMX | {"HX-Boosted": "true"})

    assert "<html" not in fragment.text
    assert "environments-table" in fragment.text
    assert "<html" in restored.text and "<html" in boosted.text


async def test_a_snapshot_older_than_three_refresh_intervals_is_badged_stale_on_the_dashboard(
    admin_client: AsyncClient, world: World
) -> None:
    async with world.app.state.database.session() as db:
        await db.execute(
            "UPDATE environment_snapshot SET captured_at = ? WHERE environment_id = ?",
            (LONG_AGO, world.local_id),
        )

    dashboard = await admin_client.get("/")

    assert dashboard.text.count("Stale") == 1


async def test_an_administrator_sees_each_hosts_endpoint_state_and_certificate(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    local = await admin_client.get(f"/environments/{world.local_id}")
    remote = await admin_client.get(f"/environments/{world.remote_id}")
    down = await admin_client.get(f"/environments/{world.down_id}")

    assert {local.status_code, remote.status_code, down.status_code} == {OK}
    assert f"unix://{LOCAL_SOCKET}" in local.text
    assert "Local socket" in local.text and "Test connection" in local.text
    assert "Standard users granted it can see its objects but cannot change anything" in local.text
    assert REMOTE_ENDPOINT in remote.text
    assert "EC secp256r1" in remote.text and "test CA" in remote.text
    assert DOWN_ENDPOINT in down.text
    assert "TCP connection" in down.text
    assert DOWN_MESSAGE in down.text
    assert world.probes.calls == []


async def test_the_environment_page_shows_five_cards_that_each_figure_links_to_the_list_behind_it(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    page = await admin_client.get(f"/environments/{world.local_id}")

    base = f"/environments/{world.local_id}"
    assert cards_in(page.text) == {
        "stacks": [Figure("running", "0 / 0", f"{base}/stacks")],
        "containers": [
            Figure(
                "running", f"{HOST_RUNNING} / {HOST_CONTAINERS}", f"{base}/containers?state=running"
            )
        ],
        "health": [
            Figure("unhealthy", str(HOST_UNHEALTHY), f"{base}/containers?health=unhealthy"),
            Figure(
                "held",
                "0",
                f"/automation?environment_id={world.local_id}&subsystem=container-health-checker",
            ),
        ],
        "updates": [Figure("images", "0", f"{base}/images?update=available")],
        "automation": [
            Figure("failed in 24 hours", "0", f"/automation?environment_id={world.local_id}"),
            Figure("last backup", "Never", "/backups"),
        ],
    }
    assert resource_tiles_in(page.text) == []
    assert "Resources" not in page.text


async def test_the_access_section_is_a_disclosure_closed_on_load_and_open_on_request(
    admin_client: AsyncClient, world: World
) -> None:
    await hold_unreachable(world, world.remote_id)
    path = f"/environments/{world.remote_id}"

    closed = await admin_client.get(path)
    requested = await admin_client.get(f"{path}?open=access")
    other = await admin_client.get(f"{path}?open=elsewhere")

    assert disclosures_in(closed.text)["access"] is False
    assert disclosures_in(requested.text)["access"] is True
    assert disclosures_in(other.text)["access"] is False


async def test_a_grant_lands_on_the_page_with_the_access_section_open_and_a_refusal_renders_it_open(
    admin_client: AsyncClient, world: World
) -> None:
    await hold_unreachable(world, world.remote_id)
    target = f"/environments/{world.remote_id}"

    granted = await admin_client.post(
        f"{target}/access",
        data=csrf_form(admin_client, grantee_kind="user", grantee_name="standard-user"),
    )
    landed = await land(admin_client, granted)
    refused = await admin_client.post(
        f"{target}/access",
        data=csrf_form(admin_client, grantee_kind="user", grantee_name="nobody"),
    )

    assert granted.headers["location"] == f"{target}?open=access#access"
    assert disclosures_in(landed.text)["access"] is True
    assert refused.status_code == UNPROCESSABLE
    assert disclosures_in(refused.text)["access"] is True


async def test_a_standard_user_sees_only_their_environment_without_endpoints_controls_or_backups(
    standard_client: AsyncClient, world: World
) -> None:
    await hold_unreachable(world, world.local_id)
    dashboard = await standard_client.get("/")
    detail = await standard_client.get(f"/environments/{world.local_id}")

    assert listed_names(dashboard.text) == [LOCAL_NAME]
    assert "Local socket" in dashboard.text
    assert GROUP_NAME in dashboard.text and "blue" in dashboard.text
    assert "green" not in dashboard.text
    assert detail.status_code == OK
    assert "Local socket" in detail.text
    assert f'href="/environments/{world.local_id}/containers"' in detail.text
    assert "/containers/counts" not in detail.text
    assert not any(
        hidden in page
        for page in (dashboard.text, detail.text)
        for hidden in (
            LOCAL_SOCKET,
            "Add environment",
            "Delete selected",
            "Test connection",
            "Resources",
        )
    )


async def test_a_standard_user_sees_the_five_cards_over_what_they_may_see_and_no_backup_line(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
) -> None:
    administrator = await admin_client.get(f"/environments/{world.local_id}")
    standard = await standard_client.get(f"/environments/{world.local_id}")

    admin_cards, standard_cards = cards_in(administrator.text), cards_in(standard.text)
    assert list(standard_cards) == list(admin_cards)
    assert [f.label for f in admin_cards["automation"]] == ["failed in 24 hours", "last backup"]
    assert [f.label for f in standard_cards["automation"]] == ["failed in 24 hours"]
    assert admin_cards["containers"][0].value == f"{HOST_RUNNING} / {HOST_CONTAINERS}"
    assert standard_cards["containers"][0].value == "0 / 0"
    assert standard_cards["health"][0].value == "0"
    assert "/backups" not in standard.text and "Last backup" not in standard.text


def without_request_id(response: Response) -> str:
    return response.text.replace(response.headers[REQUEST_ID_HEADER], "")


async def test_a_denied_environment_is_answered_like_a_missing_one_on_every_route(
    standard_client: AsyncClient, world: World
) -> None:
    form = csrf_form(standard_client)
    routes = [
        ("GET", "/environments/{}"),
        ("GET", "/environments/{}/edit"),
        ("POST", "/environments/{}/test"),
        ("POST", "/environments/{}/refresh"),
        ("POST", "/environments/{}/access"),
        ("POST", "/environments/{}/access/remove?user_id=" + str(STANDARD_ID)),
        ("POST", "/environments/{}/edit"),
    ]

    denied = [
        await standard_client.request(method, path.format(world.remote_id), data=form)
        for method, path in routes
    ]
    missing = [
        await standard_client.request(method, path.format(ABSENT_ID), data=form)
        for method, path in routes
    ]

    assert [r.status_code for r in denied] == [r.status_code for r in missing]
    assert {r.status_code for r in denied[:-1]} == {NOT_FOUND}
    assert [without_request_id(r) for r in denied[:2]] == [
        without_request_id(r) for r in missing[:2]
    ]
    assert REMOTE_NAME not in "".join(r.text for r in denied)
    assert world.probes.calls == []
    assert world.poker.poked == []


async def test_a_standard_user_who_can_see_an_environment_is_forbidden_its_administrator_routes(
    standard_client: AsyncClient, world: World
) -> None:
    target = f"/environments/{world.local_id}"
    requests = [
        ("GET", f"{target}/edit"),
        ("POST", f"{target}/edit"),
        ("POST", f"{target}/test"),
        ("POST", f"{target}/refresh"),
        ("POST", f"{target}/access"),
        ("POST", f"{target}/access/remove?user_id={STANDARD_ID}"),
        ("GET", "/environments/new"),
        ("POST", "/environments/preview"),
        ("POST", "/environments"),
        ("GET", f"/environments/delete?id={world.local_id}"),
        ("POST", "/environments/delete"),
        ("GET", "/groups"),
        ("GET", "/tags"),
    ]

    answered = await outcomes(standard_client, requests, data=csrf_form(standard_client))

    assert {status for status, _ in answered.values()} == {FORBIDDEN}
    assert world.probes.calls == []
    assert world.poker.poked == []


STACK_COUNT = 4
SUPPORTED_COMPOSE = "v2.38.2"
OLD_COMPOSE = "v2.20.0"
INSTALLED_COMPOSE = "v2.33.0"
NUMERIC_CELL = re.compile(r'<td class="is-numeric">\s*(.*?)\s*</td>', re.DOTALL)
TABLE_ROW = re.compile(r"<tr>.*?</tr>", re.DOTALL)


async def record_stacks(world: World, environment_id: int, count: int | None) -> None:
    async with world.app.state.database.session() as db:
        await db.execute(
            "UPDATE environment_snapshot SET stack_count = ? WHERE environment_id = ?",
            (count, environment_id),
        )


async def record_compose(world: World, environment_id: int, version: str) -> None:
    async with world.app.state.database.session() as db:
        await EnvironmentRepository(db, world.app.state.auth.clock).record_compose_version(
            environment_id, version
        )


def numeric_cells(page: str, environment_name: str) -> list[str]:
    """Return the text of the numeric cells in the dashboard row of one environment."""
    (row,) = [r for r in TABLE_ROW.findall(page) if f">{environment_name}</a>" in r]
    return [unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in NUMERIC_CELL.findall(row)]


async def test_the_dashboard_shows_each_stack_count_to_administrators_and_the_compose_version(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World
) -> None:
    await record_stacks(world, world.remote_id, STACK_COUNT)
    await record_stacks(world, world.local_id, None)
    await record_compose(world, world.remote_id, SUPPORTED_COMPOSE)

    dashboard = await admin_client.get("/")
    fragment = await admin_client.get("/", headers=HTMX)
    standard = await standard_client.get("/")

    headings = re.findall(r'<th scope="col"[^>]*>\s*(.*?)\s*</th>', dashboard.text, re.DOTALL)
    assert [re.sub(r"\s+", " ", h) for h in headings][-3:] == ["Networks", "Stacks", "Last contact"]
    assert numeric_cells(dashboard.text, REMOTE_NAME)[-1] == str(STACK_COUNT)
    assert numeric_cells(dashboard.text, LOCAL_NAME)[-1] == "\u2014"
    assert f"Compose {SUPPORTED_COMPOSE}" in dashboard.text
    assert numeric_cells(fragment.text, REMOTE_NAME)[-1] == str(STACK_COUNT)
    assert "Stacks" not in standard.text


async def test_the_sidebar_links_stacks_and_the_page_names_a_compose_below_the_floor(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
) -> None:
    target = f"/environments/{world.remote_id}"
    stacks = f"{target}/stacks"
    async with world.app.state.database.session() as db:
        await seed_stack_rows(db, world.remote_id, ("blog", "compose"))
    await record_compose(world, world.remote_id, SUPPORTED_COMPOSE)
    supported = await admin_client.get(target)
    await record_compose(world, world.remote_id, OLD_COMPOSE)
    old = await admin_client.get(target)
    local = await standard_client.get(f"/environments/{world.local_id}")

    assert SUPPORTED_COMPOSE in supported.text and "Below the required" not in supported.text
    assert cards_in(supported.text)["stacks"] == [Figure("running", "0 / 1", stacks)]
    assert f'<a class="nav__link" href="{stacks}">' in supported.text
    assert f"{OLD_COMPOSE}" in old.text and "Below the required v2.32.0" in old.text
    assert "Stack operations are refused until Docker Compose is upgraded" in old.text
    assert f'<a class="nav__link" href="/environments/{world.local_id}/stacks">' in local.text


async def test_the_footer_names_the_compose_version_the_runtime_holds_after_a_retry(
    admin_client: AsyncClient,
    world: World,
    compose_stub: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose = world.app.state.engine.compose
    later = tmp_path / "later-compose"
    monkeypatch.setattr(compose_setup, "DETECTION_RETRY_SECONDS", 0.0)

    started = await admin_client.get("/")
    compose.use_binary(ComposeBinary((str(later),)))
    await compose.detect()
    missing = await admin_client.get("/")
    shutil.copyfile(compose_stub, later)
    later.chmod(0o755)
    await compose.ready()
    recovered = await admin_client.get("/")

    footer = re.compile(r"Compose CLI\s*(.*?)\s*</span>\s*</footer>", re.DOTALL)
    assert (
        re.sub(r"<[^>]+>", "", present(footer.search(started.text)).group(1)) == INSTALLED_COMPOSE
    )
    assert re.sub(r"<[^>]+>", "", present(footer.search(missing.text)).group(1)) == "not detected"
    assert (
        re.sub(r"<[^>]+>", "", present(footer.search(recovered.text)).group(1)) == INSTALLED_COMPOSE
    )


UPDATE_DIGEST = "sha256:" + "a" * 64
UPDATE_CHECKED_AT = "2026-01-02T03:04:05Z"
SUMMARY = re.compile(r'<p id="results-summary"[^>]*>\s*(\d+) images?')


async def store_update(world: World, environment_id: int, reference: str, state: str) -> None:
    async with world.app.state.database.session() as db:
        await db.execute(
            "INSERT INTO image_update_status (environment_id, image_reference, state, "
            "remote_digest, checked_at) VALUES (?, ?, ?, ?, ?)",
            (environment_id, reference, state, UPDATE_DIGEST, UPDATE_CHECKED_AT),
        )


async def listed_with_update(client: AsyncClient, environment_id: int) -> str:
    page = await client.get(f"/environments/{environment_id}/images?update=available")
    found = SUMMARY.search(page.text)
    assert found is not None, page.text[:300]
    return found.group(1)


async def test_the_updates_card_equals_the_image_list_filtered_to_available_for_each_role(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
) -> None:
    stock_images(container_host)
    await store_update(world, world.local_id, "docker.io/library/nginx:1", "update_available")
    await store_update(world, world.local_id, "docker.io/library/spare:1", "up_to_date")

    pages = {
        "admin": await admin_client.get(f"/environments/{world.local_id}"),
        "standard": await standard_client.get(f"/environments/{world.local_id}"),
    }

    cards = {role: cards_in(page.text)["updates"][0] for role, page in pages.items()}
    lists = {
        "admin": await listed_with_update(admin_client, world.local_id),
        "standard": await listed_with_update(standard_client, world.local_id),
    }
    assert cards["admin"].value == lists["admin"] == "1"
    assert cards["standard"].value == lists["standard"]
    assert cards["admin"].href == f"/environments/{world.local_id}/images?update=available"


async def test_loading_the_environment_page_asks_no_registry_for_anything(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
) -> None:
    requests: list[httpx.Request] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("a registry was contacted")

    manifests = ScriptedManifests()
    registries = world.app.state.registries
    real = (registries.manifests, registries.http)
    registries.manifests = manifests
    registries.http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    stock_images(container_host)
    await store_update(world, world.local_id, "docker.io/library/nginx:1", "update_available")
    try:
        administrator = await admin_client.get(f"/environments/{world.local_id}")
        standard = await standard_client.get(f"/environments/{world.local_id}")
    finally:
        await registries.http.aclose()
        registries.manifests, registries.http = real

    assert (administrator.status_code, standard.status_code) == (OK, OK)
    assert cards_in(administrator.text)["updates"][0].value == "1"
    assert manifests.calls == [] and requests == []


async def test_an_unreachable_environment_shows_the_figures_kept_in_the_database_and_reads_nothing(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    async with world.app.state.database.session() as db:
        await seed_stack_rows(db, world.down_id, ("blog", "compose"))

    page = await admin_client.get(f"/environments/{world.down_id}")

    base = f"/environments/{world.down_id}"
    cards = cards_in(page.text)
    assert cards["stacks"] == [Figure("running (1 unreadable)", "\u2014", f"{base}/stacks")]
    assert [f.value for f in cards["containers"] + cards["health"] + cards["updates"]] == [
        "\u2014"
    ] * 4
    assert [f.value for f in cards["automation"]] == ["0", "Never"]
    assert "The host is not reachable, so its stacks and containers are not read" in page.text
    assert stack_tiles_in(page.text) == [] and container_rows_in(page.text) == []
    assert container_host.requests == []


async def test_a_host_that_cannot_be_read_says_so_and_shows_dashes_for_what_it_holds(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    container_host.route("GET", "/containers/json", Refuse())
    container_host.route("GET", "/containers/json", Refuse())

    page = await admin_client.get(f"/environments/{world.local_id}")

    cards = cards_in(page.text)
    assert page.status_code == OK
    assert "The host could not be read just now" in page.text
    assert [f.value for f in cards["containers"] + cards["health"]] == ["\u2014"] * 3
    assert [f.value for f in cards["automation"]] == ["0", "Never"]


async def test_the_stack_grid_and_container_table_list_what_the_host_holds(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    async with world.app.state.database.session() as db:
        await seed_stack_rows(db, world.local_id, (STACK, "compose"), ("idle", "compose"))

    page = await admin_client.get(f"/environments/{world.local_id}")

    base = f"/environments/{world.local_id}/stacks"
    assert stack_tiles_in(page.text) == [
        ("idle", "stopped", "Compose", f"{base}/idle"),
        (STACK, "running", "Compose", f"{base}/{STACK}"),
    ]
    rows = container_rows_in(page.text)
    assert [row[0] for row in rows] == ["cache", "db", "job", "warm", "web", "worker"]
    assert [row[1] for row in rows] == ["\u2014", STACK, "\u2014", "\u2014", STACK, "\u2014"]
    assert f"All {HOST_CONTAINERS} containers" in page.text
