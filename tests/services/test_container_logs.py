"""Tests for the container log service: the live stream, the capped download and the page tail."""

import asyncio
import json
import struct
import threading
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.services import container_logs
from app.services.access import ResourceKey
from app.services.container_logs import (
    MAX_BATCH_LINES,
    ContainerLogService,
    LogBatch,
    LogLimits,
    LogQuery,
    LogsUnavailable,
    download_filename,
)
from app.services.container_stats import KeepAlive
from app.services.log_lines import INLINE_DECODE_BYTES
from app.services.object_locks import StreamSlots, TooManyStreams
from tests.docker_engine.helpers import STDOUT, log_frame
from tests.docker_engine.terminal_engine import Call, TerminalEngine, serve_terminal_engine
from tests.stream_support import encoding_threads, leftover_tasks
from tests.terminal_support import err, nanos, out, stamped

CONTAINER = "d" * 64
ENVIRONMENT = 7
ACTOR = Principal(5, "reader", "standard", 9)
MIB = 1024 * 1024
RAW_STREAM = "application/vnd.docker.raw-stream"
FOLLOWING = LogQuery(follow=True)
HISTORY = LogQuery(follow=False)
NDJSON = LogQuery(follow=False, format="ndjson")
SCHEMA_VERSION = 2
IMAGE = "nginx:1.27"
INSTANCE_NAME = "dockmin-test"
ENVIRONMENT_NAME = "production"
HOST_NAME = "docker-host-1"
PROJECT = "shop"
SERVICE = "web"
FIRST_TIME = "2026-10-19T12:00:01.000000001Z"
SECOND_TIME = "2026-10-19T12:00:02.000000001Z"
LINE_SEPARATOR = "\N{LINE SEPARATOR}"
PARAGRAPH_SEPARATOR = "\N{PARAGRAPH SEPARATOR}"
NEXT_LINE = "\N{NEXT LINE}"


class Authoriser:
    def __init__(self) -> None:
        self.refuse = False
        self.checks: list[tuple[ResourceKey, str]] = []

    async def require_access(self, actor: Principal, key: ResourceKey, level: str) -> None:
        self.checks.append((key, level))
        if self.refuse:
            raise ResourceNotFound("not found")


@dataclass
class Host:
    """The service under test, the daemon it talks to and the knobs the stubs read."""

    daemon: TerminalEngine
    authoriser: Authoriser = field(default_factory=Authoriser)
    slots: StreamSlots = field(default_factory=StreamSlots)
    status: str = "running"
    tty: bool = False
    inspected: int = 0

    def service(self, limits: LogLimits | None = None) -> ContainerLogService:
        client = self.daemon.client()

        async def inspect(environment_id: int, container_id: str) -> Any:
            self.inspected += 1
            return SimpleNamespace(
                name="/web",
                id=container_id,
                config=SimpleNamespace(
                    tty=self.tty,
                    image=IMAGE,
                    labels={
                        "com.docker.compose.project": PROJECT,
                        "com.docker.compose.service": SERVICE,
                    },
                ),
                state=SimpleNamespace(status=self.status),
            )

        async def host_client(environment_id: int) -> Any:
            return client

        async def host_environment(environment_id: int) -> Any:
            return SimpleNamespace(name=ENVIRONMENT_NAME, engine_host_name=HOST_NAME)

        inspector = SimpleNamespace(
            inspect=inspect,
            hosts=SimpleNamespace(client=host_client, environment=host_environment),
        )
        return ContainerLogService(
            authoriser=self.authoriser,  # type: ignore[arg-type]
            inspector=inspector,  # type: ignore[arg-type]
            slots=self.slots,
            limits=limits,
            instance_name=INSTANCE_NAME,
        )

    def requests(self) -> list[Mapping[str, str]]:
        return [stream.request.query for stream in self.daemon.log_streams]


@pytest.fixture
async def host() -> AsyncIterator[Host]:
    async with serve_terminal_engine() as daemon:
        yield Host(daemon)


async def consume(stream: Any) -> None:
    async for _ in stream:
        pass


async def close_all(streams: list[Any]) -> None:
    for stream in streams:
        await stream.aclose()


async def collect(stream: Any) -> list[Any]:
    batches = [item async for item in stream]
    await stream.aclose()
    return batches


def texts(batches: list[Any]) -> list[tuple[str, list[str]]]:
    return [(b.stream, [line.text for line in b.lines]) for b in batches if isinstance(b, LogBatch)]


@pytest.mark.parametrize("operation", ["open", "download", "tail"])
async def test_a_container_the_caller_may_not_read_is_refused_before_the_engine_is_contacted(
    host: Host, operation: str
) -> None:
    host.authoriser.refuse = True
    service = host.service()

    with pytest.raises(ResourceNotFound):
        await getattr(service, operation)(ACTOR, ENVIRONMENT, CONTAINER, HISTORY)

    assert host.daemon.log_streams == []
    assert host.daemon.requests == []
    assert host.inspected == 0
    assert host.slots.open_count == 0


@pytest.mark.parametrize("operation", ["open", "download", "tail"])
async def test_an_id_that_is_not_canonical_is_refused_without_asking_anything(
    host: Host, operation: str
) -> None:
    service = host.service()

    with pytest.raises(ResourceNotFound):
        await getattr(service, operation)(ACTOR, ENVIRONMENT, "web", HISTORY)

    assert (host.authoriser.checks, host.daemon.requests) == ([], [])


async def test_reading_logs_asks_for_read_access_on_the_container() -> None:
    async with serve_terminal_engine() as daemon:
        host = Host(daemon)
        daemon.script_logs(out(1, "line"))

        await collect(await host.service().open(ACTOR, ENVIRONMENT, CONTAINER, HISTORY))

        assert host.authoriser.checks == [
            (ResourceKey(ENVIRONMENT, "container", CONTAINER), "read")
        ]


async def test_a_full_user_is_told_so_only_after_authorisation_passed(host: Host) -> None:
    held = [host.slots.acquire(ACTOR.user_id) for _ in range(4)]
    service = host.service()

    with pytest.raises(TooManyStreams) as full:
        await service.open(ACTOR, ENVIRONMENT, CONTAINER, FOLLOWING)
    host.authoriser.refuse = True
    with pytest.raises(ResourceNotFound):
        await service.open(ACTOR, ENVIRONMENT, CONTAINER, FOLLOWING)

    assert full.value.scope == "user"
    assert host.daemon.log_streams == []
    assert len(held) == host.slots.open_count


async def test_a_stream_delivers_batches_per_stream_and_ends_finished_when_docker_ends_it(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, "one"), err(2, "two"), out(3, "three") + out(4, "four"))
    stream = await host.service().open(ACTOR, ENVIRONMENT, CONTAINER, FOLLOWING)

    batches = await collect(stream)

    assert texts(batches) == [("out", ["one"]), ("err", ["two"]), ("out", ["three", "four"])]
    assert [b.event_id for b in batches] == [nanos(1), nanos(2), nanos(4)]
    assert batches[0].data == "2026-10-19T12:00:01.000000001Z one"
    assert stream.end_reason == "finished"
    assert host.slots.open_count == 0


async def test_a_stream_asks_the_engine_for_timestamps_and_the_selected_range(host: Host) -> None:
    query = LogQuery(tail=50, since_ns=nanos(1), until_ns=nanos(9), stdout=True, stderr=False)
    stream = await host.service().open(ACTOR, ENVIRONMENT, CONTAINER, query)

    await collect(stream)

    assert host.requests() == [
        {
            "follow": "true",
            "stdout": "true",
            "stderr": "false",
            "timestamps": "true",
            "tail": "50",
            "since": "1792411201.000000001",
            "until": "1792411209.000000001",
        }
    ]


async def test_a_batch_never_holds_more_than_its_line_limit(host: Host) -> None:
    lines = b"".join(stamped(1, f"line {n}") for n in range(2 * MAX_BATCH_LINES + 50))
    host.daemon.script_logs(log_frame(STDOUT, lines))

    batches = await collect(await host.service().open(ACTOR, ENVIRONMENT, CONTAINER, HISTORY))

    assert [len(b.lines) for b in batches] == [MAX_BATCH_LINES, MAX_BATCH_LINES, 50]


async def test_an_unterminated_last_line_is_delivered_when_the_stream_ends(host: Host) -> None:
    host.daemon.script_logs(log_frame(STDOUT, b"first\nsecond-without-newline"))

    batches = await collect(await host.service().open(ACTOR, ENVIRONMENT, CONTAINER, HISTORY))

    assert texts(batches) == [("out", ["first", "second-without-newline"])]


async def test_a_tty_containers_raw_output_is_read_without_frame_headers(host: Host) -> None:
    host.tty = True
    host.daemon.script_logs(stamped(1, "raw one") + stamped(2, "raw two"), content_type=RAW_STREAM)

    batches = await collect(await host.service().open(ACTOR, ENVIRONMENT, CONTAINER, HISTORY))

    assert texts(batches) == [("out", ["raw one", "raw two"])]


@pytest.mark.parametrize(
    ("status", "asked", "followed"),
    [
        ("running", True, "true"),
        ("restarting", True, "true"),
        ("paused", True, "true"),
        ("exited", True, "false"),
        ("created", True, "false"),
        ("running", False, "false"),
    ],
)
async def test_follow_is_asked_of_the_engine_only_for_a_live_container_that_the_caller_follows(
    host: Host, status: str, asked: bool, followed: str
) -> None:
    host.status = status
    stream = await host.service().open(ACTOR, ENVIRONMENT, CONTAINER, LogQuery(follow=asked))

    await collect(stream)

    assert host.requests()[0]["follow"] == followed


async def test_resuming_after_a_line_neither_repeats_it_nor_skips_the_next_and_drops_tail(
    host: Host,
) -> None:
    host.daemon.script_logs(
        out(1, "one") + out(2, "two"), out(3, "three"), log_frame(STDOUT, b"no timestamp\n")
    )
    stream = await host.service().open(
        ACTOR, ENVIRONMENT, CONTAINER, LogQuery(tail=500), str(nanos(2))
    )

    batches = await collect(stream)

    assert texts(batches) == [("out", ["three", "no timestamp"])]
    request = host.requests()[0]
    assert "tail" not in request
    assert request["since"] == "1792411202.000000001"


async def test_the_after_value_resumes_like_last_event_id_and_the_later_one_applies(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, "one") + out(2, "two") + out(3, "three"))
    stream = await host.service().open(
        ACTOR, ENVIRONMENT, CONTAINER, HISTORY, str(nanos(1)), str(nanos(2))
    )

    batches = await collect(stream)

    assert texts(batches) == [("out", ["three"])]
    assert host.requests()[0]["since"] == "1792411202.000000001"


@pytest.mark.parametrize("reported", ["12abc", "", "-5", "1" * 21, "1.5"])
async def test_an_unusable_resume_value_is_ignored_and_the_tail_applies(
    host: Host, reported: str
) -> None:
    stream = await host.service().open(
        ACTOR, ENVIRONMENT, CONTAINER, LogQuery(tail=25), reported, reported
    )

    await collect(stream)

    request = host.requests()[0]
    assert (request["tail"], "since" in request) == ("25", False)


async def test_a_resume_never_asks_for_earlier_lines_than_the_selected_since(host: Host) -> None:
    stream = await host.service().open(
        ACTOR, ENVIRONMENT, CONTAINER, LogQuery(since_ns=nanos(5)), str(nanos(2))
    )

    await collect(stream)

    assert host.requests()[0]["since"] == "1792411205.000000001"


async def test_a_container_that_stops_ends_the_followed_stream_cleanly(host: Host) -> None:
    host.daemon.script_logs(out(1, "running"), hold=True)
    stream = await host.service().open(ACTOR, ENVIRONMENT, CONTAINER, FOLLOWING)
    first = await anext(stream)

    host.daemon.log_streams[0].end()
    rest = [item async for item in stream]

    assert texts([first]) == [("out", ["running"])]
    assert rest == []
    assert stream.end_reason == "finished"
    assert host.slots.open_count == 0


async def test_a_frame_over_the_log_cap_ends_the_stream_as_unavailable(host: Host) -> None:
    host.daemon.script_logs(out(1, "fine"), struct.pack(">BxxxI", 1, 2 * MIB) + b"x" * 100)
    stream = await host.service().open(ACTOR, ENVIRONMENT, CONTAINER, HISTORY)

    batches = await collect(stream)

    assert texts(batches) == [("out", ["fine"])]
    assert stream.end_reason == "unavailable"


async def test_a_connection_lost_mid_stream_ends_it_as_unavailable(host: Host) -> None:
    host.daemon.script_logs(out(1, "before"), hold=False, fail=True)
    stream = await host.service().open(ACTOR, ENVIRONMENT, CONTAINER, FOLLOWING)

    await collect(stream)

    assert stream.end_reason == "unavailable"
    assert host.slots.open_count == 0


async def test_a_refused_log_request_ends_the_stream_as_unavailable(host: Host) -> None:
    host.daemon.refuse(Call.LOGS, 500)
    stream = await host.service().open(ACTOR, ENVIRONMENT, CONTAINER, FOLLOWING)

    batches = await collect(stream)

    assert (batches, stream.end_reason) == ([], "unavailable")


async def test_a_stream_reaching_its_lifetime_says_so_and_sends_keep_alives_while_quiet(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, "only line"), hold=True)
    limits = LogLimits(lifetime=0.3, keepalive=0.05)
    stream = await host.service(limits).open(ACTOR, ENVIRONMENT, CONTAINER, FOLLOWING)

    items = await collect(stream)

    assert stream.end_reason == "lifetime"
    assert texts(items) == [("out", ["only line"])]
    assert any(isinstance(item, KeepAlive) for item in items)
    await asyncio.wait_for(host.daemon.log_streams[0].client_closed.wait(), 5)


async def test_closing_a_followed_stream_early_closes_the_engine_connection_and_frees_the_slot(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, "first"), hold=True)
    before = asyncio.all_tasks()
    stream = await host.service().open(ACTOR, ENVIRONMENT, CONTAINER, FOLLOWING)
    await anext(stream)
    held = host.slots.open_count

    await stream.aclose()

    await asyncio.wait_for(host.daemon.log_streams[0].client_closed.wait(), 5)
    await host.daemon.wait_for_no_connections()
    assert (held, host.slots.open_count) == (1, 0)
    assert stream.end_reason == "closed"
    assert await leftover_tasks(before) == set()


async def test_cancelling_the_reader_of_a_followed_stream_leaks_no_task_or_connection(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, "first"), hold=True)
    before = asyncio.all_tasks()
    stream = await host.service().open(ACTOR, ENVIRONMENT, CONTAINER, FOLLOWING)

    task = asyncio.create_task(consume(stream))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await stream.aclose()

    await asyncio.wait_for(host.daemon.log_streams[0].client_closed.wait(), 5)
    await host.daemon.wait_for_no_connections()
    assert host.slots.open_count == 0
    assert await leftover_tasks(before) == set()


async def test_a_stream_that_is_opened_and_closed_without_being_read_gives_its_slot_back(
    host: Host,
) -> None:
    stream = await host.service().open(ACTOR, ENVIRONMENT, CONTAINER, FOLLOWING)
    held = host.slots.open_count

    await stream.aclose()
    await stream.aclose()

    assert (held, host.slots.open_count) == (1, 0)


async def test_the_fifth_live_view_of_a_user_is_refused_with_the_sentence_that_explains_it(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, "x"), hold=True)
    service = host.service()
    streams = [await service.open(ACTOR, ENVIRONMENT, CONTAINER, FOLLOWING) for _ in range(4)]

    with pytest.raises(TooManyStreams) as refused:
        await service.open(ACTOR, ENVIRONMENT, CONTAINER, FOLLOWING)
    await close_all(streams)

    assert str(refused.value) == (
        "You have 4 live views open (stats, logs or downloads). Close one, then try again."
    )
    assert host.slots.open_count == 0


async def rendered(host: Host, query: LogQuery, limits: LogLimits | None = None) -> str:
    download = await host.service(limits).download(ACTOR, ENVIRONMENT, CONTAINER, query)
    body = b"".join([chunk async for chunk in download])
    await download.aclose()
    return body.decode()


async def test_a_download_prefixes_each_line_with_its_stream_when_both_are_selected(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, "alpha"), err(2, "beta"))

    body = await rendered(host, HISTORY)

    assert body == (
        "stdout 2026-10-19T12:00:01.000000001Z alpha\nstderr 2026-10-19T12:00:02.000000001Z beta\n"
    )


async def test_a_download_of_one_stream_has_no_prefix_and_can_leave_out_timestamps(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, "alpha"))

    body = await rendered(host, LogQuery(stderr=False, timestamps=False, follow=False))

    assert body == "alpha\n"


async def test_a_download_filter_keeps_only_matching_lines_ignoring_case(host: Host) -> None:
    host.daemon.script_logs(out(1, "alpha"), err(2, "Beta \x1b[31mred\x1b[0m"), out(3, "gamma"))

    body = await rendered(host, LogQuery(filter="BETA RED", timestamps=False))

    assert body == "stderr Beta red\n"


async def test_a_download_ignores_follow_and_asks_for_every_line_when_tail_is_all(
    host: Host,
) -> None:
    await rendered(host, LogQuery(tail=None, follow=True))

    request = host.requests()[0]
    assert (request["follow"], "tail" in request) == ("false", False)


async def test_a_download_stops_at_its_line_cap_and_says_so(host: Host) -> None:
    host.daemon.script_logs(*(out(1, f"line {n}") for n in range(10)))

    body = await rendered(host, LogQuery(timestamps=False), LogLimits(download_lines=3))

    assert body.splitlines() == [
        "stdout line 0",
        "stdout line 1",
        "stdout line 2",
        "[output stopped at the 3-line limit]",
    ]


async def test_a_download_stops_at_its_byte_cap_and_says_so(host: Host) -> None:
    host.daemon.script_logs(*(out(1, "x" * 1000) for _ in range(1300)))

    body = await rendered(host, LogQuery(timestamps=False), LogLimits(download_bytes=MIB))

    assert body.splitlines()[-1] == "[output stopped at the 1 MiB limit]"
    assert len(body) <= MIB + 100


async def test_a_download_stops_at_its_time_cap_and_says_so(host: Host) -> None:
    host.daemon.script_logs(out(1, "first"), hold=True)

    body = await rendered(host, LogQuery(timestamps=False), LogLimits(download_seconds=0.2))

    assert body.splitlines() == ["stdout first", "[output stopped at the 0.2-second limit]"]
    await asyncio.wait_for(host.daemon.log_streams[0].client_closed.wait(), 5)


async def test_a_download_that_fails_before_its_first_line_is_unavailable_and_frees_its_slot(
    host: Host,
) -> None:
    host.daemon.refuse(Call.LOGS, 500)
    service = host.service()

    with pytest.raises(LogsUnavailable) as refused:
        await service.download(ACTOR, ENVIRONMENT, CONTAINER, HISTORY)

    assert str(refused.value) == "Logs are not available for this container."
    assert host.slots.open_count == 0


async def test_a_download_that_fails_part_way_ends_with_a_line_saying_the_host_stopped(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, "kept"), fail=True)
    download = await host.service().download(
        ACTOR, ENVIRONMENT, CONTAINER, LogQuery(timestamps=False)
    )

    body = b"".join([chunk async for chunk in download]).decode()
    await download.aclose()

    assert body.splitlines() == [
        "stdout kept",
        "[output stopped: the Docker host stopped answering]",
    ]
    assert download.end_reason == "unavailable"
    assert host.slots.open_count == 0


async def test_a_download_holds_a_view_slot_until_it_is_closed(host: Host) -> None:
    host.daemon.script_logs(out(1, "x"))
    download = await host.service().download(ACTOR, ENVIRONMENT, CONTAINER, HISTORY)
    held = host.slots.open_count

    await download.aclose()

    assert (held, host.slots.open_count) == (1, 0)


async def test_the_page_tail_returns_every_line_when_it_fits_and_never_follows(host: Host) -> None:
    host.daemon.script_logs(out(1, "one") + out(2, "two"))
    query = LogQuery(tail=200, follow=True)

    tail = await host.service().tail(ACTOR, ENVIRONMENT, CONTAINER, query)

    assert [line.text for line in tail.lines] == ["one", "two"]
    assert (tail.cut, tail.name, tail.running, tail.short_id) == (False, "web", True, "d" * 12)
    request = host.requests()[0]
    assert (request["follow"], request["tail"]) == ("false", "200")


async def test_the_page_tail_keeps_the_newest_complete_lines_that_fit_the_byte_cap(
    host: Host,
) -> None:
    host.daemon.script_logs(*(out(1, f"{n:03d} " + "y" * 96) for n in range(100)))
    limits = LogLimits(page_bytes=4096)

    tail = await host.service(limits).tail(ACTOR, ENVIRONMENT, CONTAINER, LogQuery(tail=1000))

    numbers = [int(line.text[:3]) for line in tail.lines]
    assert tail.cut
    assert numbers[-1] == 99
    assert numbers == list(range(numbers[0], 100))
    assert 20 < len(numbers) < 60


async def test_the_page_tail_claims_a_slot_and_gives_it_back_on_every_path(host: Host) -> None:
    host.daemon.script_logs(out(1, "one"))
    service = host.service()
    await service.tail(ACTOR, ENVIRONMENT, CONTAINER, HISTORY)
    host.daemon.refuse(Call.LOGS, 500)

    with pytest.raises(LogsUnavailable):
        await service.tail(ACTOR, ENVIRONMENT, CONTAINER, HISTORY)

    assert host.slots.open_count == 0


async def test_the_page_tail_is_refused_when_the_user_has_no_free_view_slot(host: Host) -> None:
    held = [host.slots.acquire(ACTOR.user_id) for _ in range(4)]

    with pytest.raises(TooManyStreams):
        await host.service().tail(ACTOR, ENVIRONMENT, CONTAINER, HISTORY)

    assert host.daemon.log_streams == []
    assert host.slots.open_count == len(held)


async def test_a_stopped_container_is_reported_as_not_running_by_the_page_tail(host: Host) -> None:
    host.status = "exited"
    host.daemon.script_logs(out(1, "last words"))

    tail = await host.service().tail(ACTOR, ENVIRONMENT, CONTAINER, HISTORY)

    assert (tail.running, [line.text for line in tail.lines]) == (False, ["last words"])


async def test_a_page_read_that_times_out_is_marked_cut_and_keeps_every_line_it_received(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(container_logs, "PAGE_SECONDS", 0.2)
    host.daemon.script_logs(out(1, "first") + out(2, "second"), hold=True)

    tail = await host.service().tail(ACTOR, ENVIRONMENT, CONTAINER, HISTORY)

    assert [line.text for line in tail.lines] == ["first", "second"]
    assert tail.cut
    assert host.slots.open_count == 0


def records_of(body: str) -> list[dict[str, Any]]:
    assert body.endswith("\n")
    return [json.loads(line) for line in body.split("\n")[:-1]]


def record_of(time: str | None, stream: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "time": time,
        "stream": stream,
        "format": "text",
        "message": message,
        "instance_name": INSTANCE_NAME,
        "environment_id": ENVIRONMENT,
        "environment_name": ENVIRONMENT_NAME,
        "host_name": HOST_NAME,
        "container_id": CONTAINER,
        "container_name": "web",
        "image": IMAGE,
        "compose_project": PROJECT,
        "compose_service": SERVICE,
    }


def notice(text: str) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "notice": text}


async def test_an_ndjson_download_is_one_record_per_line_with_the_common_and_container_keys(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, "alpha"), err(2, "beta"))
    download = await host.service().download(ACTOR, ENVIRONMENT, CONTAINER, NDJSON)

    body = b"".join([chunk async for chunk in download]).decode()
    await download.aclose()

    parsed = records_of(body)
    expected = [
        record_of(FIRST_TIME, "stdout", "alpha"),
        record_of(SECOND_TIME, "stderr", "beta"),
    ]
    assert parsed == expected
    assert [list(record) for record in parsed] == [list(record) for record in expected]
    assert download.media_type == "application/x-ndjson"
    assert download.filename.endswith(".ndjson")
    assert download.end_reason == "finished"
    assert host.slots.open_count == 0


async def test_an_ndjson_download_keeps_the_time_whatever_the_text_options_say(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, "alpha"))
    query = LogQuery(follow=False, format="ndjson", timestamps=False)

    body = await rendered(host, query)

    assert records_of(body) == [record_of(FIRST_TIME, "stdout", "alpha")]


@pytest.mark.parametrize(("threshold", "off_loop"), [(0, True), (INLINE_DECODE_BYTES, False)])
async def test_an_ndjson_download_encodes_off_the_event_loop_only_for_a_large_batch(
    host: Host, monkeypatch: pytest.MonkeyPatch, threshold: int, off_loop: bool
) -> None:
    host.daemon.script_logs(out(1, "alpha"))
    monkeypatch.setattr(container_logs, "INLINE_DECODE_BYTES", threshold)
    threads = encoding_threads(monkeypatch, container_logs)

    body = await rendered(host, NDJSON)

    assert [record["message"] for record in records_of(body)] == ["alpha"]
    assert (threads == {threading.get_ident()}) is not off_loop


async def test_a_text_download_of_a_large_batch_renders_on_the_event_loop(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.daemon.script_logs(out(1, "alpha"))
    monkeypatch.setattr(container_logs, "INLINE_DECODE_BYTES", 0)
    threads = encoding_threads(monkeypatch, container_logs)

    body = await rendered(host, HISTORY)

    assert "alpha" in body
    assert threads == set()


async def test_an_ndjson_download_filter_keeps_only_matching_lines_ignoring_case(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, "alpha"), err(2, "Beta \x1b[31mred\x1b[0m"), out(3, "gamma"))

    body = await rendered(host, LogQuery(follow=False, format="ndjson", filter="BETA RED"))

    assert [record["message"] for record in records_of(body)] == ["Beta red"]


async def test_an_ndjson_download_writes_a_json_object_line_as_a_payload_with_its_level(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, '{"level":"WARN","msg":"slow"}'))

    body = await rendered(host, NDJSON)

    [parsed] = records_of(body)
    assert list(parsed)[:7] == [
        "schema_version",
        "time",
        "stream",
        "format",
        "payload",
        "level",
        "instance_name",
    ]
    assert (parsed["format"], parsed["payload"], parsed["level"]) == (
        "json",
        {"level": "WARN", "msg": "slow"},
        "warn",
    )
    assert "message" not in parsed


async def test_a_line_the_engine_sent_without_a_timestamp_has_a_null_time(host: Host) -> None:
    host.daemon.script_logs(log_frame(STDOUT, b"no timestamp\n"))

    body = await rendered(host, NDJSON)

    assert records_of(body) == [record_of(None, "stdout", "no timestamp")]


@pytest.mark.parametrize("separator", [LINE_SEPARATOR, PARAGRAPH_SEPARATOR, NEXT_LINE])
async def test_an_ndjson_message_with_a_unicode_line_break_stays_on_one_line(
    host: Host, separator: str
) -> None:
    text = f"one{separator}two"
    host.daemon.script_logs(out(1, text), out(2, "next"))

    body = await rendered(host, NDJSON)

    assert separator not in body
    assert len(body.splitlines()) == 2
    assert [record["message"] for record in records_of(body)] == [text, "next"]


async def test_an_ndjson_download_that_hits_the_line_cap_ends_with_a_notice_record(
    host: Host,
) -> None:
    host.daemon.script_logs(*(out(1, f"line {n}") for n in range(10)))

    body = await rendered(host, NDJSON, LogLimits(download_lines=3))

    parsed = records_of(body)
    assert [record["message"] for record in parsed[:3]] == ["line 0", "line 1", "line 2"]
    assert parsed[3:] == [notice("[output stopped at the 3-line limit]")]


async def test_an_ndjson_download_that_hits_the_byte_cap_ends_with_a_notice_record(
    host: Host,
) -> None:
    host.daemon.script_logs(*(out(1, "x" * 1000) for _ in range(1300)))

    body = await rendered(host, NDJSON, LogLimits(download_bytes=MIB))

    parsed = records_of(body)
    assert parsed[-1] == notice("[output stopped at the 1 MiB limit]")
    assert all("notice" not in record for record in parsed[:-1])
    assert len(body) <= MIB + 100


async def test_an_ndjson_download_that_hits_the_time_cap_ends_with_a_notice_record(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, "first"), hold=True)

    body = await rendered(host, NDJSON, LogLimits(download_seconds=0.2))

    parsed = records_of(body)
    assert [record.get("message") for record in parsed] == ["first", None]
    assert parsed[-1] == notice("[output stopped at the 0.2-second limit]")
    await asyncio.wait_for(host.daemon.log_streams[0].client_closed.wait(), 5)


async def test_an_ndjson_download_that_loses_the_host_part_way_ends_with_a_notice_record(
    host: Host,
) -> None:
    host.daemon.script_logs(out(1, "kept"), fail=True)
    download = await host.service().download(ACTOR, ENVIRONMENT, CONTAINER, NDJSON)

    body = b"".join([chunk async for chunk in download]).decode()
    await download.aclose()

    parsed = records_of(body)
    assert [record.get("message") for record in parsed] == ["kept", None]
    assert parsed[-1] == notice("[output stopped: the Docker host stopped answering]")
    assert download.end_reason == "unavailable"


@pytest.mark.parametrize(
    ("value", "expected"), [(None, "text"), ("text", "text"), ("ndjson", "ndjson")]
)
def test_a_query_reads_the_log_format_and_defaults_to_text(
    value: str | None, expected: str
) -> None:
    params = {} if value is None else {"format": value}

    assert LogQuery.from_params(params).format == expected


@pytest.mark.parametrize("value", ["xml", "NDJSON", "", "json", "text "])
def test_a_query_with_another_log_format_is_refused_naming_the_choices(value: str) -> None:
    with pytest.raises(ValidationFailed) as refused:
        LogQuery.from_params({"format": value})

    assert refused.value.fields == {"format": "Choose text or ndjson."}


@pytest.mark.parametrize(("log_format", "suffix"), [("text", ".log"), ("ndjson", ".ndjson")])
def test_a_download_filename_ends_with_the_suffix_of_its_format(
    log_format: str, suffix: str
) -> None:
    moment = datetime(2026, 10, 19, 12, 0, 1, tzinfo=UTC)

    name = download_filename("web", CONTAINER, moment, log_format)  # type: ignore[arg-type]

    assert name == f"web-{CONTAINER[:12]}-20261019T120001Z{suffix}"
