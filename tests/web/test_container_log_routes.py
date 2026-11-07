"""Tests for the container log routes: the event stream, the download and the Logs tab."""

import asyncio
import json
import re
from dataclasses import dataclass

from httpx import AsyncClient, Response

from tests.auth_support import ADMIN_ID
from tests.docker_engine.helpers import STDOUT, log_frame
from tests.docker_engine.terminal_engine import RAW_STREAM, Call, TerminalEngine
from tests.stream_support import leftover_tasks, read_stream
from tests.terminal_support import (
    CONTAINER,
    PLAIN_CONFIG,
    PLAIN_CONTAINER,
    err,
    nanos,
    out,
    release_all,
    stamped,
)
from tests.web.environment_support import World

HOSTILE = "5" * 64
HOSTILE_NAME = 'bad"name\r\nX-Evil: 1;../x'
FILE_NAME = re.compile(
    r'attachment; filename="bad_name__X-Evil__1_\.\._x-555555555555-\d{8}T\d{6}Z\.log"'
)
NDJSON_FILE_NAME = re.compile(
    r'attachment; filename="bad_name__X-Evil__1_\.\._x-555555555555-\d{8}T\d{6}Z\.ndjson"'
)
SCHEMA_VERSION = 2
RECORD_KEYS = [
    "schema_version",
    "time",
    "stream",
    "format",
    "message",
    "instance_name",
    "environment_id",
    "environment_name",
    "host_name",
    "container_id",
    "container_name",
    "image",
    "compose_project",
    "compose_service",
]
ONE = "2026-10-19T12:00:01.000000001Z"
TWO = "2026-10-19T12:00:02.000000001Z"
RESUMED_SINCE = "1792411202.000000001"
FOUR_VIEWS = "You have 4 live views open (stats, logs or downloads). Close one, then try again."
UNAVAILABLE = "Logs are not available for this container."
INVALID_QUERIES = {
    "tail=99999": "Choose a number of lines from 0 to 10000.",
    "stdout=0&stderr=0": "Select at least one of stdout and stderr.",
    "since=garbage": "Enter a date and time such as 2026-10-19T14:30 (UTC), or a duration",
    "until=1999-01-01&since=2000-01-01": "Until must be after since.",
    "q=" + "x" * 200: "The filter is at most 128 characters of plain text.",
    "format=xml": "Choose text or ndjson.",
}


def base(world: World, container: str = PLAIN_CONTAINER) -> str:
    return f"/environments/{world.local_id}/containers/{container}"


async def fetch_all(client: AsyncClient, urls: list[str]) -> list[Response]:
    return [await client.get(url) for url in urls]


async def statuses(client: AsyncClient, urls: list[str]) -> list[int]:
    return [response.status_code for response in await fetch_all(client, urls)]


@dataclass(slots=True)
class StreamCases:
    plain: Response
    forged: Response
    resumed_by_header: Response
    resumed_by_parameter: Response


async def stream_cases(client: AsyncClient, world: World, daemon: TerminalEngine) -> StreamCases:
    url = base(world) + "/logs/stream"
    daemon.script_logs(out(1, "one"), err(2, "two"))
    plain = await client.get(url + "?tail=50")
    daemon.script_logs(log_frame(STDOUT, b"x\r\nevent: end\ndata: unavailable\n"))
    forged = await client.get(url)
    daemon.script_logs(out(1, "one") + out(2, "two") + out(3, "three"))
    by_header = await client.get(url, headers={"Last-Event-ID": str(nanos(2))})
    by_parameter = await client.get(f"{url}?after={nanos(2)}")
    return StreamCases(plain, forged, by_header, by_parameter)


async def test_the_event_stream_has_typed_events_resists_forgery_and_resumes(
    admin_client: AsyncClient, world: World, terminal_daemon: TerminalEngine
) -> None:
    cases = await stream_cases(admin_client, world, terminal_daemon)

    assert cases.plain.status_code == 200
    assert cases.plain.headers["content-type"].startswith("text/event-stream")
    assert cases.plain.headers["cache-control"] == "no-store"
    assert cases.plain.headers["x-accel-buffering"] == "no"
    assert cases.plain.text == (
        f"event: out\nid: {nanos(1)}\ndata: {ONE} one\n\n"
        f"event: err\nid: {nanos(2)}\ndata: {TWO} two\n\n"
        "event: end\ndata: finished\n\n"
    )
    assert cases.forged.text.split("\n").count("event: end") == 1
    assert cases.forged.text.endswith("event: end\ndata: finished\n\n")
    assert "data: - event: end" in cases.forged.text
    assert "three" in cases.resumed_by_header.text and "three" in cases.resumed_by_parameter.text
    assert "one" not in cases.resumed_by_header.text + cases.resumed_by_parameter.text
    queries = [stream.request.query for stream in terminal_daemon.log_streams]
    assert queries[0]["tail"] == "50"
    assert [("tail" in q, q.get("since")) for q in queries[2:]] == [
        (False, RESUMED_SINCE),
        (False, RESUMED_SINCE),
    ]


async def test_the_log_routes_refuse_before_the_engine_for_whoever_may_not_read_the_container(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    terminal_daemon: TerminalEngine,
) -> None:
    tails = ["/logs/stream", "/logs/download", "?tab=logs&recent=1"]
    ids = ["c" * 64, "abc", "web", "A" * 64, PLAIN_CONTAINER]
    urls = [f"{base(world, cid)}{tail}" for cid in ids for tail in tails]
    oversize = [f"{base(world, 'a' * 200)}{tail}" for tail in tails]

    admin = await statuses(admin_client, urls)
    standard = await statuses(standard_client, urls)
    admin_oversize = await statuses(admin_client, oversize)
    standard_oversize = await statuses(standard_client, oversize)

    assert admin == [404] * 12 + [200] * 3
    assert standard == [404] * 15
    assert admin_oversize == standard_oversize
    assert len(terminal_daemon.log_streams) == 3


async def invalid_responses(client: AsyncClient, world: World) -> dict[str, list[Response]]:
    return {
        query: [
            await client.get(f"{base(world)}/logs/stream?{query}"),
            await client.get(f"{base(world)}/logs/download?{query}"),
        ]
        for query in INVALID_QUERIES
    }


def explained(responses: dict[str, list[Response]]) -> dict[str, bool]:
    return {
        query: pair[0].text.startswith(INVALID_QUERIES[query]) for query, pair in responses.items()
    }


async def test_an_invalid_query_is_a_plain_text_400_before_the_engine_is_asked(
    admin_client: AsyncClient, world: World, terminal_daemon: TerminalEngine
) -> None:
    responses = await invalid_responses(admin_client, world)

    flat = [response for pair in responses.values() for response in pair]
    assert {r.status_code for r in flat} == {400}
    assert all(r.headers["content-type"].startswith("text/plain") for r in flat)
    assert explained(responses) == dict.fromkeys(INVALID_QUERIES, True)
    assert [pair[0].text == pair[1].text for pair in responses.values()] == [True] * len(
        INVALID_QUERIES
    )
    assert terminal_daemon.log_streams == []


async def test_a_full_user_is_refused_with_the_sentence_and_the_engine_is_not_asked(
    admin_client: AsyncClient, world: World, terminal_daemon: TerminalEngine
) -> None:
    slots = world.app.state.engine.stream_slots
    held = [slots.acquire(ADMIN_ID) for _ in range(4)]
    urls = [
        f"{base(world)}{tail}" for tail in ("/logs/stream", "/logs/download", "?tab=logs&recent=1")
    ]
    pages = await fetch_all(admin_client, urls)
    release_all(held)

    assert [page.status_code for page in pages] == [429, 429, 429]
    assert all(FOUR_VIEWS in page.text for page in pages)
    assert terminal_daemon.log_streams == []
    assert slots.open_count == 0


async def test_a_host_that_cannot_deliver_logs_is_reported_as_unavailable_on_every_route(
    admin_client: AsyncClient, world: World, terminal_daemon: TerminalEngine
) -> None:
    terminal_daemon.refuse(Call.LOGS, 500)

    stream = await admin_client.get(f"{base(world)}/logs/stream")
    download = await admin_client.get(f"{base(world)}/logs/download")
    tab = await admin_client.get(f"{base(world)}?tab=logs&recent=1")

    assert stream.text == "event: end\ndata: unavailable\n\n"
    assert (download.status_code, tab.status_code) == (503, 503)
    assert UNAVAILABLE in download.text and UNAVAILABLE in tab.text
    assert world.app.state.engine.stream_slots.open_count == 0


async def leave_events(
    world: World, client: AsyncClient, daemon: TerminalEngine, path: str, seen_text: str
) -> str:
    daemon.script_logs(out(1, seen_text), hold=True)
    opened = len(daemon.log_streams)
    stream = await read_stream(world.app, client, base(world) + path, stop_when=seen_text)
    await asyncio.wait_for(daemon.log_streams[opened].client_closed.wait(), 5)
    return stream.body


async def test_leaving_the_page_closes_the_engine_stream_and_leaks_no_task_or_slot(
    admin_client: AsyncClient, world: World, terminal_daemon: TerminalEngine
) -> None:
    before = asyncio.all_tasks()

    event = await leave_events(world, admin_client, terminal_daemon, "/logs/stream", "first")
    download = await leave_events(world, admin_client, terminal_daemon, "/logs/download", "second")

    await terminal_daemon.wait_for_no_connections()
    assert "first" in event and "second" in download
    assert world.app.state.engine.stream_slots.open_count == 0
    assert await leftover_tasks(before) == set()


@dataclass(slots=True)
class DownloadCases:
    hostile_name: Response
    filtered: Response
    tty: Response


async def download_cases(
    client: AsyncClient, world: World, daemon: TerminalEngine
) -> DownloadCases:
    daemon.script_container(HOSTILE, HOSTILE_NAME, config=PLAIN_CONFIG)
    daemon.script_logs(out(1, "alpha"), err(2, "beta"))
    hostile = await client.get(f"{base(world, HOSTILE)}/logs/download?tail=all&timestamps=0")
    daemon.script_logs(out(1, "keep me"), out(2, "drop"), err(3, "Keep Too"))
    filtered = await client.get(f"{base(world)}/logs/download?q=KEEP&timestamps=0&tail=all")
    daemon.script_logs(stamped(1, "raw one") + stamped(2, "raw two"), content_type=RAW_STREAM)
    tty = await client.get(f"{base(world, CONTAINER)}/logs/download?timestamps=0&tail=all")
    return DownloadCases(hostile, filtered, tty)


async def test_a_download_is_a_safe_filtered_attachment_for_plain_and_tty_containers(
    admin_client: AsyncClient, world: World, terminal_daemon: TerminalEngine
) -> None:
    cases = await download_cases(admin_client, world, terminal_daemon)

    hostile = cases.hostile_name
    assert hostile.status_code == 200
    assert FILE_NAME.fullmatch(hostile.headers["content-disposition"])
    assert hostile.headers["content-type"] == "text/plain; charset=utf-8"
    assert hostile.headers["cache-control"] == "no-store"
    assert hostile.headers["x-content-type-options"] == "nosniff"
    assert hostile.text == "stdout alpha\nstderr beta\n"
    assert "tail" not in terminal_daemon.log_streams[0].request.query
    assert cases.filtered.text == "stdout keep me\nstderr Keep Too\n"
    assert cases.tty.text == "stdout raw one\nstdout raw two\n"


@dataclass(slots=True)
class TabPage:
    response: Response
    log_requests_so_far: int


async def tab_pages(
    client: AsyncClient, world: World, daemon: TerminalEngine
) -> dict[str, TabPage]:
    daemon.script_logs(out(1, "first line"), err(2, "<b>second</b> line"))
    url = f"{base(world)}?tab=logs"
    requests: dict[str, tuple[str, dict[str, str]]] = {
        "script": ("", {}),
        "no script": ("&recent=1&tail=100&timestamps=1", {}),
        "htmx": ("&recent=1", {"HX-Request": "true"}),
        "bad field": ("&recent=1&since=garbage", {}),
        "clamped": ("&recent=1&tail=9999", {}),
    }
    pages: dict[str, TabPage] = {}
    for label, (suffix, headers) in requests.items():
        response = await client.get(url + suffix, headers=headers)
        pages[label] = TabPage(response, len(daemon.log_streams))
    return pages


async def test_the_logs_tab_serves_script_and_the_bounded_page_without_it(
    admin_client: AsyncClient, world: World, terminal_daemon: TerminalEngine
) -> None:
    pages = await tab_pages(admin_client, world, terminal_daemon)

    script, plain, htmx = pages["script"], pages["no script"], pages["htmx"]
    invalid, clamped = pages["bad field"], pages["clamped"]
    assert script.response.status_code == 200 and script.log_requests_so_far == 0
    assert f'data-log-stream="{base(world)}/logs/stream"' in script.response.text
    assert f'data-log-download="{base(world)}/logs/download"' in script.response.text
    assert 'data-log-running="true"' in script.response.text
    assert "Show recent lines" in script.response.text
    assert plain.response.status_code == 200 and plain.log_requests_so_far == 1
    assert 'class="log-line__text">first line<' in plain.response.text
    assert "&lt;b&gt;second&lt;/b&gt; line" in plain.response.text
    assert "<b>second</b>" not in plain.response.text
    assert 'visually-hidden">stderr ' in plain.response.text
    assert "Refresh" in plain.response.text and "Download" in plain.response.text
    assert htmx.response.status_code == 200 and htmx.log_requests_so_far == 1
    assert invalid.response.status_code == 400 and invalid.log_requests_so_far == 1
    assert "Enter a date and time such as" in invalid.response.text
    assert 'aria-invalid="true"' in invalid.response.text
    assert clamped.response.status_code == 200 and clamped.log_requests_so_far == 2
    assert "Without script this page shows at most 1000 lines." in clamped.response.text
    requests = [stream.request.query for stream in terminal_daemon.log_streams]
    assert (requests[0]["follow"], requests[0]["tail"]) == ("false", "100")
    assert requests[1]["tail"] == "1000"
    assert world.app.state.engine.stream_slots.open_count == 0


def records_of(response: Response) -> list[dict[str, object]]:
    assert response.text.endswith("\n")
    return [json.loads(line) for line in response.text.split("\n")[:-1]]


@dataclass(slots=True)
class NdjsonCases:
    hostile_name: Response
    filtered: Response
    tty: Response
    default: Response


async def ndjson_cases(client: AsyncClient, world: World, daemon: TerminalEngine) -> NdjsonCases:
    daemon.script_container(HOSTILE, HOSTILE_NAME, config=PLAIN_CONFIG)
    daemon.script_logs(out(1, "alpha"), err(2, "beta"))
    hostile = await client.get(f"{base(world, HOSTILE)}/logs/download?tail=all&format=ndjson")
    daemon.script_logs(out(1, "keep me"), out(2, "drop"), err(3, "Keep Too"))
    filtered = await client.get(f"{base(world)}/logs/download?q=KEEP&tail=all&format=ndjson")
    daemon.script_logs(stamped(1, "raw one") + stamped(2, "raw two"), content_type=RAW_STREAM)
    tty = await client.get(f"{base(world, CONTAINER)}/logs/download?tail=all&format=ndjson")
    daemon.script_logs(out(1, "alpha"))
    default = await client.get(f"{base(world)}/logs/download?tail=all&format=text")
    return NdjsonCases(hostile, filtered, tty, default)


async def test_an_ndjson_download_is_a_safe_attachment_of_typed_records_for_plain_and_tty(
    admin_client: AsyncClient, world: World, terminal_daemon: TerminalEngine
) -> None:
    cases = await ndjson_cases(admin_client, world, terminal_daemon)

    hostile = cases.hostile_name
    assert hostile.status_code == 200
    assert NDJSON_FILE_NAME.fullmatch(hostile.headers["content-disposition"])
    assert hostile.headers["content-type"] == "application/x-ndjson"
    assert hostile.headers["cache-control"] == "no-store"
    assert hostile.headers["x-content-type-options"] == "nosniff"
    records = records_of(hostile)
    assert [
        (r["schema_version"], r["time"], r["stream"], r["format"], r["message"]) for r in records
    ] == [
        (SCHEMA_VERSION, ONE, "stdout", "text", "alpha"),
        (SCHEMA_VERSION, TWO, "stderr", "text", "beta"),
    ]
    assert [list(r) for r in records] == [RECORD_KEYS] * 2
    assert {r["container_name"] for r in records} == {HOSTILE_NAME}
    assert [r["message"] for r in records_of(cases.filtered)] == ["keep me", "Keep Too"]
    assert [r["message"] for r in records_of(cases.tty)] == ["raw one", "raw two"]
    assert cases.default.headers["content-type"] == "text/plain; charset=utf-8"
    assert cases.default.headers["content-disposition"].endswith('.log"')


async def test_the_logs_tab_offers_text_and_ndjson_downloads_that_carry_the_selection(
    admin_client: AsyncClient, world: World, terminal_daemon: TerminalEngine
) -> None:
    page = await admin_client.get(f"{base(world)}?tab=logs&q=err&stdout=1&stderr=0")

    html = page.text
    assert page.status_code == 200
    assert re.search(r'href="[^"]*format=text[^"]*"[^>]*data-log-download-link', html)
    ndjson_link = re.search(r'href="([^"]*)"[^>]*data-log-download-ndjson', html)
    assert ndjson_link is not None
    href = ndjson_link.group(1).replace("&amp;", "&")
    assert href.startswith(f"{base(world)}/logs/download?")
    assert {"tail=all", "q=err", "stderr=0", "format=ndjson"} <= set(href.split("?")[1].split("&"))
