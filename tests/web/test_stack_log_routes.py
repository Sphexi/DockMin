"""The whole-stack log routes: the Logs tab, the event stream and the downloads."""

import asyncio
import json
import re
from typing import Any, NamedTuple

import pytest
from httpx import AsyncClient, Response

from app.db.repositories.resource_control import ResourceControlRepository
from app.services.access import ResourceKey
from tests.access_support import plant
from tests.auth_support import STANDARD_ID
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.fake_engine import Ending, Reply, Stream
from tests.docker_engine.helpers import STDERR, STDOUT, log_frame
from tests.docker_engine.swarm_engine import SwarmEngine, plain_spec, stamp
from tests.stack_support import PROJECT_LABEL, SERVICE_LABEL
from tests.stream_support import leftover_tasks, read_stream
from tests.swarm_support import NAMESPACE_LABEL
from tests.web.environment_support import World, seed_stack_rows

SHOP = "logshop"
NAMESPACE = "Cli"
MULTIPLEXED = {"Content-Type": "application/vnd.docker.multiplexed-stream"}
NUMBER_LABEL = "com.docker.compose.container-number"
EPOCH_NS = 1_767_225_600 * 1_000_000_000
SECOND_NS = 1_000_000_000
SCHEMA_VERSION = 2
HOSTILE_TEXT = "<b>x</b>\r\nevent: end\ndata: unavailable"
UNAVAILABLE = "Logs are not available for this stack."
HOST_FAILURE = Reply(500, b'{"message":"no"}')
TOO_MANY = "Too many live views are open"
FILE_NAME = re.compile(r'attachment; filename="logshop-\d{8}T\d{6}Z\.(log|ndjson)"')
INVALID_QUERIES = {
    "tail=99999": 400,
    "stdout=0&stderr=0": 400,
    "since=garbage": 400,
    "format=xml": 400,
}


class Entry(NamedTuple):
    second: int
    text: str
    stderr: bool = False


def clock_text(second: int) -> str:
    return f"2026-01-01T00:{second // 60:02d}:{second % 60:02d}.000000000Z"


def ns(second: int) -> int:
    return EPOCH_NS + second * SECOND_NS


def frames(*entries: Entry) -> bytes:
    return b"".join(
        log_frame(
            STDERR if entry.stderr else STDOUT,
            f"{clock_text(entry.second)} {entry.text}\n".encode(),
        )
        for entry in entries
    )


def stack_url(world: World, name: str = SHOP) -> str:
    return f"/environments/{world.remote_id}/stacks/{name}"


def swarm_url(world: World, namespace: str = NAMESPACE) -> str:
    return f"/environments/{world.remote_id}/swarm/stacks/{namespace}"


def member(host: ContainerEngine, number: int, service: str, *, state: str = "running") -> Any:
    return host.add(
        f"{0xC0000 + number:064x}",
        f"{SHOP}-{service}-{number}",
        labels={PROJECT_LABEL: SHOP, SERVICE_LABEL: service, NUMBER_LABEL: str(number)},
        state=state,
    )


def script(host: ContainerEngine, box: Any, *entries: Entry) -> None:
    host.route("GET", f"/containers/{box.id}/logs", Reply(200, frames(*entries), MULTIPLEXED))


def log_paths(host: ContainerEngine | SwarmEngine) -> list[str]:
    return [request.path for request in host.requests if request.path.endswith("/logs")]


async def seed_shop(world: World, host: ContainerEngine) -> dict[str, Any]:
    async with world.app.state.database.session() as db:
        await seed_stack_rows(db, world.remote_id, (SHOP, "compose"))
    boxes = {
        "web1": member(host, 1, "web"),
        "web2": member(host, 2, "web"),
        "db": member(host, 3, "db", state="exited"),
    }
    script(host, boxes["web1"], Entry(1, "w1"), Entry(4, HOSTILE_TEXT, stderr=True))
    script(host, boxes["web2"], Entry(2, "w2"))
    script(host, boxes["db"], Entry(3, "d1"))
    return boxes


def events(body: str) -> list[tuple[str, str | None, list[str]]]:
    parsed: list[tuple[str, str | None, list[str]]] = []
    for block in body.split("\n\n"):
        if not block or block.startswith(":") or block.startswith("retry"):
            continue
        name = next((line[7:] for line in block.split("\n") if line.startswith("event: ")), "")
        identifier = next((line[4:] for line in block.split("\n") if line.startswith("id: ")), None)
        data = [line[6:] for line in block.split("\n") if line.startswith("data: ")]
        parsed.append((name, identifier, data))
    return parsed


def records_of(response: Response) -> list[dict[str, Any]]:
    assert response.text.endswith("\n")
    return [json.loads(line) for line in response.text.split("\n")[:-1]]


async def share_stack_with_standard_user(world: World, name: str = SHOP) -> None:
    async with world.app.state.database.session() as db:
        await plant(
            ResourceControlRepository(db, world.app.state.auth.clock),
            ResourceKey(world.remote_id, "stack", name),
            owner=STANDARD_ID,
        )


async def hide_from_standard_users(world: World, resource_type: Any, resource_id: str) -> None:
    async with world.app.state.database.session() as db:
        await plant(
            ResourceControlRepository(db, world.app.state.auth.clock),
            ResourceKey(world.remote_id, resource_type, resource_id),
            administrators_only=True,
        )


async def test_the_logs_tab_offers_a_checkbox_per_service_and_reads_no_logs_until_asked(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await seed_shop(world, container_host)
    url = stack_url(world)

    page = await admin_client.get(f"{url}?tab=logs")

    html = page.text
    assert page.status_code == 200
    assert f'data-log-stream="{url}/logs/stream"' in html
    assert f'data-log-download="{url}/logs/download"' in html
    assert f'<form class="filters log-form" method="get" action="{url}" data-log-form>' in html
    assert '<input type="hidden" name="tab" value="logs">' in html
    assert '<input type="hidden" name="recent" value="1">' in html
    services = re.findall(
        r'type="checkbox" id="[^"]*" name="services" value="(\w+)"( checked)?', html
    )
    assert services == [("db", " checked"), ("web", " checked")]
    assert "Reading 3 containers." in html
    assert "not shown" not in html
    assert "Show recent lines" in html
    assert "format=text" in html and "format=ndjson" in html
    assert log_paths(container_host) == []


async def test_a_selected_service_is_the_only_one_ticked_and_the_links_carry_the_selection(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await seed_shop(world, container_host)

    page = await admin_client.get(f"{stack_url(world)}?tab=logs&services=web&services=ghost")

    html = page.text
    ticked = dict(re.findall(r'name="services" value="(\w+)"( checked)?', html))
    assert page.status_code == 200
    assert ticked == {"db": "", "web": " checked"}
    assert "Reading 2 containers." in html
    links = [unescaped.replace("&amp;", "&") for unescaped in re.findall(r'href="([^"]*)"', html)]
    carrying = [link for link in links if "/logs/download" in link or "recent=1" in link]
    assert carrying and all("services=web" in link for link in carrying)


async def test_the_page_without_script_shows_the_newest_lines_of_the_ticked_services_only(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await seed_shop(world, container_host)

    page = await admin_client.get(
        f"{stack_url(world)}?tab=logs&recent=1&services=web&tail=5&timestamps=1"
    )

    html = page.text
    assert page.status_code == 200
    assert 'class="log-line__task">web.1<' in html
    assert 'class="log-line__task">web.2<' in html
    assert 'class="log-line__text">w1<' in html and 'class="log-line__text">w2<' in html
    assert "d1" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x</b>" not in html
    assert 'visually-hidden">stderr ' in html
    assert "The newest 5 lines across 2 containers" in html
    requests = [r for r in container_host.requests if r.path.endswith("/logs")]
    assert {request.query["tail"] for request in requests} == {"5"}
    assert len(requests) == 2


async def test_a_standard_user_sees_the_lines_they_may_read_and_a_count_of_the_rest(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    boxes = await seed_shop(world, container_host)
    await share_stack_with_standard_user(world)
    await hide_from_standard_users(world, "container", boxes["db"].id)

    page = await standard_client.get(f"{stack_url(world)}?tab=logs&recent=1")

    html = page.text
    assert page.status_code == 200
    assert "1 container not shown." in html
    assert "Reading 2 containers." in html
    assert 'class="log-line__text">w1<' in html
    assert "d1" not in html
    assert boxes["db"].name not in html
    assert not any(boxes["db"].id in path for path in log_paths(container_host))
    assert 'name="services"' not in html


async def test_a_stack_the_standard_user_may_not_read_is_not_found_on_every_log_route(
    standard_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await seed_shop(world, container_host)
    url = stack_url(world)

    statuses = [
        (await standard_client.get(f"{url}?tab=logs&recent=1")).status_code,
        (await standard_client.get(f"{url}/logs/stream")).status_code,
        (await standard_client.get(f"{url}/logs/download")).status_code,
    ]

    assert statuses == [404, 404, 404]
    assert log_paths(container_host) == []


@pytest.mark.parametrize(
    "suffix", ["?tab=logs&recent=1&since=garbage", "?tab=logs&recent=1&q=" + "x" * 200]
)
async def test_a_bad_field_on_the_tab_is_a_400_page_that_marks_the_field(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine, suffix: str
) -> None:
    await seed_shop(world, container_host)

    page = await admin_client.get(stack_url(world) + suffix)

    assert page.status_code == 400
    assert 'aria-invalid="true"' in page.text
    assert "Check the highlighted fields." in page.text
    assert log_paths(container_host) == []


async def test_a_tail_above_the_page_limit_is_clamped_and_says_so(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await seed_shop(world, container_host)

    page = await admin_client.get(f"{stack_url(world)}?tab=logs&recent=1&tail=9999")

    assert page.status_code == 200
    assert "Without script this page shows at most 1000 lines from each container." in page.text
    assert {r.query["tail"] for r in container_host.requests if r.path.endswith("/logs")} == {
        "1000"
    }


async def test_the_tab_reports_a_stack_whose_logs_cannot_be_read_and_a_full_environment(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    boxes = await seed_shop(world, container_host)
    slots = world.app.state.engine.stream_slots
    held = [slots.acquire_stack(1, world.remote_id, is_administrator=True) for _ in range(3)]
    busy = await admin_client.get(f"{stack_url(world)}?tab=logs&recent=1")
    for slot in held:
        slot.close()
    for box in boxes.values():
        container_host.route("GET", f"/containers/{box.id}/logs", HOST_FAILURE)

    broken = await admin_client.get(f"{stack_url(world)}?tab=logs&recent=1")

    assert busy.status_code == 429
    assert TOO_MANY in busy.text
    assert broken.status_code == 503
    assert UNAVAILABLE in broken.text
    assert slots.open_count == 0 and slots.stack_open_count(world.remote_id) == 0


async def test_the_stream_sends_the_merged_tail_as_typed_events_and_ends_when_members_stop(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await seed_shop(world, container_host)

    response = await admin_client.get(f"{stack_url(world)}/logs/stream?follow=0")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    sent = events(response.text)
    assert sent[:-1] == [
        (
            "out",
            str(ns(3)),
            [
                f"{clock_text(1)} web.1 w1",
                f"{clock_text(2)} web.2 w2",
                f"{clock_text(3)} db d1",
            ],
        ),
        (
            "err",
            str(ns(4)),
            [f"{clock_text(4)} web.1 <b>x</b>", "- web.1 event: end", "- web.1 data: unavailable"],
        ),
    ]
    assert sent[-1] == ("end", None, ["finished"])
    assert response.text.split("\n").count("event: end") == 1
    assert world.app.state.engine.stream_slots.open_count == 0


async def test_one_unreadable_member_is_a_notice_event_and_all_of_them_end_the_stream(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    boxes = await seed_shop(world, container_host)
    container_host.route("GET", f"/containers/{boxes['db'].id}/logs", HOST_FAILURE)
    url = f"{stack_url(world)}/logs/stream?follow=0"

    partial = await admin_client.get(url)
    for box in boxes.values():
        container_host.route("GET", f"/containers/{box.id}/logs", HOST_FAILURE)
    none = await admin_client.get(url)

    assert ("notice", None, ["[output of db could not be read]"]) in events(partial.text)
    assert events(none.text) == [("end", None, ["unavailable"])]


async def test_a_live_line_reaches_the_browser_and_leaving_releases_everything(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    boxes = await seed_shop(world, container_host)
    container_host.route(
        "GET",
        f"/containers/{boxes['web1'].id}/logs",
        Stream([frames(Entry(1, "w1"), Entry(2, "w2"), Entry(9, "live-line"))], Ending.HOLD),
    )
    before = asyncio.all_tasks()

    captured = await read_stream(
        world.app,
        admin_client,
        f"{stack_url(world)}/logs/stream",
        stop_when="live-line",
        headers={"Last-Event-ID": str(ns(2))},
    )

    assert captured.status == 200
    assert events(captured.body) == [("out", str(ns(9)), [f"{clock_text(9)} web.1 live-line"])]
    assert world.app.state.engine.stream_slots.open_count == 0
    assert world.app.state.engine.stream_slots.stack_open_count(world.remote_id) == 0
    assert await leftover_tasks(before) == set()


async def test_the_stream_refuses_a_bad_query_a_missing_stack_and_a_full_environment(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await seed_shop(world, container_host)
    url = stack_url(world)
    slots = world.app.state.engine.stream_slots
    invalid = {
        query: await admin_client.get(f"{url}/logs/stream?{query}") for query in INVALID_QUERIES
    }
    missing = await admin_client.get(stack_url(world, "absent") + "/logs/stream")
    malformed = await admin_client.get(stack_url(world, "A%20B") + "/logs/stream")
    held = [slots.acquire_stack(1, world.remote_id, is_administrator=True) for _ in range(3)]
    full = await admin_client.get(f"{url}/logs/stream")
    for slot in held:
        slot.close()

    assert {query: response.status_code for query, response in invalid.items()} == INVALID_QUERIES
    assert all(r.headers["content-type"].startswith("text/plain") for r in invalid.values())
    assert (missing.status_code, malformed.status_code) == (404, 404)
    assert full.status_code == 429 and TOO_MANY in full.text
    assert log_paths(container_host) == []


async def test_a_text_download_is_a_safe_attachment_of_the_prefixed_merged_lines(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await seed_shop(world, container_host)

    response = await admin_client.get(f"{stack_url(world)}/logs/download?services=web&services=db")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert FILE_NAME.fullmatch(response.headers["content-disposition"])
    assert response.headers["content-disposition"].endswith('.log"')
    assert response.text.splitlines()[:3] == [
        f"stdout {clock_text(1)} web.1 w1",
        f"stdout {clock_text(2)} web.2 w2",
        f"stdout {clock_text(3)} db d1",
    ]
    assert world.app.state.engine.stream_slots.open_count == 0


async def test_an_ndjson_download_of_one_service_has_typed_records_with_the_container_name(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    await seed_shop(world, container_host)

    response = await admin_client.get(
        f"{stack_url(world)}/logs/download?format=ndjson&services=web&tail=all"
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-ndjson"
    assert FILE_NAME.fullmatch(response.headers["content-disposition"])
    assert response.headers["content-disposition"].endswith('.ndjson"')
    parsed = records_of(response)
    assert {
        key: parsed[0][key]
        for key in ("schema_version", "time", "stream", "message", "compose_service")
    } == {
        "schema_version": SCHEMA_VERSION,
        "time": clock_text(1),
        "stream": "stdout",
        "message": "w1",
        "compose_service": "web",
    }
    assert parsed[0]["container_name"] == f"{SHOP}-web-1"
    assert [row["message"] for row in parsed] == [
        "w1",
        "<b>x</b>",
        "event: end",
        "data: unavailable",
        "w2",
    ]
    assert {row["compose_service"] for row in parsed} == {"web"}
    assert parsed[2]["time"] is None
    assert len(response.text.splitlines()) == len(parsed)


async def test_a_download_refuses_a_bad_query_a_missing_stack_and_hosts_that_cannot_answer(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    boxes = await seed_shop(world, container_host)
    url = f"{stack_url(world)}/logs/download"
    invalid = {query: await admin_client.get(f"{url}?{query}") for query in INVALID_QUERIES}
    missing = await admin_client.get(stack_url(world, "absent") + "/logs/download")
    for box in boxes.values():
        container_host.route("GET", f"/containers/{box.id}/logs", HOST_FAILURE)
    unavailable = await admin_client.get(url)

    assert {query: response.status_code for query, response in invalid.items()} == INVALID_QUERIES
    assert missing.status_code == 404
    assert unavailable.status_code == 503 and UNAVAILABLE in unavailable.text
    assert world.app.state.engine.stream_slots.open_count == 0


async def test_a_download_holding_a_slot_gives_it_back_when_the_browser_leaves(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    boxes = await seed_shop(world, container_host)
    container_host.route(
        "GET",
        f"/containers/{boxes['web1'].id}/logs",
        Stream([frames(Entry(1, "first-line"))], Ending.HOLD),
    )
    before = asyncio.all_tasks()

    captured = await read_stream(
        world.app,
        admin_client,
        f"{stack_url(world)}/logs/download",
        query="services=web&tail=all",
        stop_when="first-line",
    )

    assert "first-line" in captured.body
    assert world.app.state.engine.stream_slots.open_count == 0
    assert world.app.state.engine.stream_slots.stack_open_count(world.remote_id) == 0
    assert await leftover_tasks(before) == set()


@pytest.fixture
def swarm_stack(swarm_host: SwarmEngine) -> dict[str, list[str]]:
    web = swarm_host.add_service(
        plain_spec("Cli_web", replicas=2, Labels={NAMESPACE_LABEL: NAMESPACE})
    )
    worker = swarm_host.add_service(plain_spec("Cli_worker", Labels={NAMESPACE_LABEL: NAMESPACE}))
    web_tasks = swarm_host.service_tasks(web)
    worker_tasks = swarm_host.service_tasks(worker)
    swarm_host.add_log(web_tasks[0], stamp(1), "w1")
    swarm_host.add_log(web_tasks[1], stamp(2), "w2", "err")
    swarm_host.add_log(worker_tasks[0], stamp(3), "k1")
    return {"web": web_tasks, "worker": worker_tasks, "worker_id": [worker]}


async def test_the_swarm_namespace_and_managed_stack_pages_both_carry_the_logs_tab(
    admin_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
    swarm_stack: dict[str, list[str]],
) -> None:
    namespace_page = await admin_client.get(f"{swarm_url(world)}?tab=logs")
    swarm_host.add_service(plain_spec("Managed_api", Labels={NAMESPACE_LABEL: "Managed"}))
    async with world.app.state.database.session() as db:
        await seed_stack_rows(db, world.remote_id, ("Managed", "swarm"))
    managed_page = await admin_client.get(f"{swarm_url(world, 'Managed')}?tab=logs")

    assert namespace_page.status_code == 200
    html = namespace_page.text
    assert f'data-log-stream="{swarm_url(world)}/logs/stream"' in html
    assert f'data-log-download="{swarm_url(world)}/logs/download"' in html
    assert re.findall(r'name="services" value="([\w]+)"', html) == ["Cli_web", "Cli_worker"]
    assert "Reading 2 services." in html
    assert "task slot" in html
    assert managed_page.status_code == 200
    assert "data-log-view" in managed_page.text
    assert f'data-log-stream="{swarm_url(world, "Managed")}/logs/stream"' in managed_page.text


async def test_the_swarm_stream_and_downloads_merge_the_services_with_slot_prefixes(
    admin_client: AsyncClient, world: World, swarm_stack: dict[str, list[str]]
) -> None:
    stream = await admin_client.get(f"{swarm_url(world)}/logs/stream?follow=0")
    text = await admin_client.get(f"{swarm_url(world)}/logs/download")
    ndjson = await admin_client.get(f"{swarm_url(world)}/logs/download?format=ndjson&tail=all")

    assert [data for _, _, lines in events(stream.text)[:-1] for data in lines] == [
        f"{stamp(1)} Cli_web.1 w1",
        f"{stamp(2)} Cli_web.2 w2",
        f"{stamp(3)} Cli_worker.1 k1",
    ]
    assert events(stream.text)[-1] == ("end", None, ["finished"])
    assert [line.split()[-2:] for line in text.text.splitlines()] == [
        ["Cli_web.1", "w1"],
        ["Cli_web.2", "w2"],
        ["Cli_worker.1", "k1"],
    ]
    rows = records_of(ndjson)
    assert {row["service_name"] for row in rows} == {"Cli_web", "Cli_worker"}
    assert {row["task_id"] for row in rows} == {
        *swarm_stack["web"],
        *swarm_stack["worker"],
    }
    assert all("container_name" not in row for row in rows)
    assert world.app.state.engine.stream_slots.open_count == 0


async def test_a_standard_user_sees_the_swarm_services_they_may_read_and_a_count_of_the_rest(
    standard_client: AsyncClient, world: World, swarm_stack: dict[str, list[str]]
) -> None:
    await share_stack_with_standard_user(world, NAMESPACE)
    await hide_from_standard_users(world, "service", swarm_stack["worker_id"][0])

    page = await standard_client.get(f"{swarm_url(world)}?tab=logs&recent=1")

    html = page.text
    assert page.status_code == 200
    assert "1 service not shown." in html
    assert "Cli_worker" not in html and "k1" not in html
    assert 'name="services"' not in html
