"""Whole-stack logs: members, the merged tail, follow, downloads and the view slots."""

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable, Collection, Iterator
from typing import Any, NamedTuple

import pytest

from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.compose_cli.cli import ComposeCli
from app.db.repositories.stack import StackRepository
from app.services import container_logs, stack_logs
from app.services.access import ResourceKey
from app.services.container_logs import (
    LogBatch,
    LogLimits,
    LogQuery,
    LogsUnavailable,
)
from app.services.object_locks import TooManyStreams
from app.services.stack_logs import (
    MAX_MEMBERS,
    StackLogDownload,
    StackLogItem,
    StackLogNotice,
    StackLogService,
    StackLogStream,
)
from tests.access_support import ADMIN, ENV_A, MEMBER, PEER, STANDARD, control, stack_key
from tests.docker_engine.fake_engine import Ending, Reply, Stream
from tests.docker_engine.helpers import STDERR, STDOUT, log_frame
from tests.docker_engine.swarm_engine import plain_spec, stamp
from tests.services.conftest import SwarmRig
from tests.stack_support import (
    PROJECT_LABEL,
    SERVICE_LABEL,
    Rig,
    compose_doc,
    project_box,
    seed_stack_row,
)
from tests.stream_support import encoding_threads, leftover_tasks
from tests.swarm_support import NAMESPACE_LABEL

SHOP = "shop"
NUMBER_LABEL = "com.docker.compose.container-number"
MULTIPLEXED = {"Content-Type": "application/vnd.docker.multiplexed-stream"}
EPOCH_NS = 1_767_225_600 * 1_000_000_000
SECOND_NS = 1_000_000_000
WAIT_SECONDS = 5.0
MIB = 1024 * 1024
LINE_SEPARATOR = "\N{LINE SEPARATOR}"
SCHEMA_VERSION = 2
CONTEXT_KEYS = ["instance_name", "environment_id", "environment_name", "host_name"]
CONTAINER_KEYS = [
    "container_id",
    "container_name",
    "image",
    "compose_project",
    "compose_service",
]
SERVICE_KEYS = ["service_id", "service_name", "task_id", "node_id", "image", "stack_namespace"]
COMMON_KEYS = ["schema_version", "time", "stream", "format", "message"]
HOST_FAILURE = Reply(500, b'{"message":"no reading"}')


class Entry(NamedTuple):
    second: int
    text: str
    stderr: bool = False
    nano: int = 0


def ns(second: int, nano: int = 0) -> int:
    return EPOCH_NS + second * SECOND_NS + nano


def clock_text(second: int, nano: int = 0) -> str:
    return f"2026-01-01T00:{second // 60:02d}:{second % 60:02d}.{nano:09d}Z"


def frames(*entries: Entry) -> bytes:
    return b"".join(
        log_frame(
            STDERR if entry.stderr else STDOUT,
            f"{clock_text(entry.second, entry.nano)} {entry.text}\n".encode(),
        )
        for entry in entries
    )


def history(host: Any, box: Any, *entries: Entry) -> None:
    host.route("GET", f"/containers/{box.id}/logs", Reply(200, frames(*entries), MULTIPLEXED))


def history_then_live(host: Any, box: Any, past: list[Entry], live: list[Entry]) -> None:
    host.route(
        "GET",
        f"/containers/{box.id}/logs",
        Reply(200, frames(*past), MULTIPLEXED),
        Stream([frames(*live)], Ending.HOLD),
    )


def replica(host: Any, number: int, service: str, project: str = SHOP, **fields: Any) -> Any:
    return host.add(
        f"{0xB000 * 16 + number:064x}",
        f"{project.lower()}-{service}-{number}",
        labels={PROJECT_LABEL: project, SERVICE_LABEL: service, NUMBER_LABEL: str(number)},
        **fields,
    )


def log_requests(host: Any, box: Any) -> list[Any]:
    return [r for r in host.requests if r.path == f"/containers/{box.id}/logs"]


def line_texts(items: Collection[Any]) -> list[str]:
    return [line.text for item in items if isinstance(item, LogBatch) for line in item.lines]


def notices(items: Collection[StackLogItem]) -> list[str]:
    return [item.text for item in items if isinstance(item, StackLogNotice)]


async def seed(rig: Rig) -> None:
    await seed_stack_row(rig, ENV_A, SHOP, compose_doc(image="nginx:1"))


async def drained(stream: StackLogStream) -> list[StackLogItem]:
    try:
        return [item async for item in stream]
    finally:
        await stream.aclose()


async def read_until(stream: StackLogStream, wanted: set[str]) -> list[StackLogItem]:
    got: list[StackLogItem] = []
    try:
        async with asyncio.timeout(WAIT_SECONDS):
            async for item in stream:
                got.append(item)
                if wanted <= set(line_texts(got)):
                    break
    finally:
        await stream.aclose()
    return got


async def body_of(download: StackLogDownload) -> str:
    try:
        return b"".join([chunk async for chunk in download]).decode()
    finally:
        await download.aclose()


def records_of(body: str) -> list[dict[str, Any]]:
    assert body.endswith("\n")
    return [json.loads(line) for line in body.split("\n")[:-1]]


def limited(rig: Rig, limits: LogLimits) -> StackLogService:
    return StackLogService(
        authoriser=rig.access.authoriser,
        inspector=rig.inspector,
        stacks=rig.services.queries,
        rows=StackRepository(rig.db, rig.registry.vault, rig.clock),
        slots=rig.engine.stream_slots,
        limits=limits,
    )


@pytest.fixture(autouse=True)
def no_compose_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    started: list[str] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        started.append("started")
        raise AssertionError("a Compose process was started")

    monkeypatch.setattr(ComposeCli, "invocation", refuse)
    yield
    assert started == []


async def test_the_tail_merges_every_member_in_time_order_with_service_and_replica_prefixes(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    web_one = replica(rig.host, 1, "web", project="Shop")
    web_two = replica(rig.host, 2, "web", state="exited")
    database = replica(rig.host, 3, "db", project="SHOP")
    other = replica(rig.host, 4, "web", project="other")
    history(rig.host, web_one, Entry(1, "w1a"), Entry(4, "w1b", stderr=True))
    history(rig.host, web_two, Entry(2, "w2a"))
    history(rig.host, database, Entry(3, "dba"))
    history(rig.host, other, Entry(1, "foreign"))

    members = await rig.services.logs.compose_members(ADMIN, ENV_A, SHOP)
    tail = await rig.services.logs.compose_tail(ADMIN, ENV_A, SHOP, LogQuery(follow=False))

    assert (members.members, members.hidden, members.over_cap) == (3, 0, 0)
    assert members.services == ("db", "web")
    assert [line.text for line in tail.lines] == ["w1a", "w2a", "dba", "w1b"]
    assert [line.origin for line in tail.lines] == ["web.1", "web.2", "db", "web.1"]
    assert [line.service for line in tail.lines] == ["web", "web", "db", "web"]
    assert [line.container_name for line in tail.lines] == [
        "shop-web-1",
        "shop-web-2",
        "shop-db-3",
        "shop-web-1",
    ]
    assert [line.stream for line in tail.lines] == ["stdout", "stdout", "stdout", "stderr"]
    assert (tail.cut, tail.unreadable) == (False, 0)
    assert log_requests(rig.host, other) == []
    assert rig.engine.stream_slots.open_count == 0


async def test_a_standard_user_reads_only_the_members_they_may_and_the_rest_are_counted_unnamed(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    visible = project_box(rig.host, 1, SHOP, "web")
    hidden = project_box(rig.host, 2, SHOP, "secretdb")
    history(rig.host, visible, Entry(1, "seen"))
    history(rig.host, hidden, Entry(2, "SECRET-LINE"))
    await control(rig.access, stack_key(SHOP), level="read", users=(MEMBER.user_id,))
    await control(
        rig.access,
        ResourceKey(ENV_A, "container", hidden.id),
        level="read",
        administrators_only=True,
    )
    logs = rig.fresh().logs
    before = len(rig.host.requests)

    members = await logs.compose_members(MEMBER, ENV_A, SHOP)
    tail = await logs.compose_tail(MEMBER, ENV_A, SHOP, LogQuery(follow=False))
    live = await drained(await logs.compose_open(MEMBER, ENV_A, SHOP, LogQuery(follow=False)))
    asked = [r.path for r in rig.host.requests[before:] if r.path.endswith("/logs")]
    everything = await rig.fresh().logs.compose_tail(ADMIN, ENV_A, SHOP, LogQuery(follow=False))

    assert (members.members, members.hidden, members.services) == (1, 1, ("web",))
    assert [line.text for line in tail.lines] == ["seen"]
    assert line_texts(live) == ["seen"]
    assert not any(hidden.id in path for path in asked)
    rendered = repr(members) + repr(tail) + repr(live)
    assert "SECRET-LINE" not in rendered
    assert "secretdb" not in rendered
    assert hidden.name not in rendered
    assert [line.text for line in everything.lines] == ["seen", "SECRET-LINE"]


@pytest.mark.parametrize(
    "call",
    [
        lambda logs, who, name: logs.compose_members(who, ENV_A, name),
        lambda logs, who, name: logs.compose_tail(who, ENV_A, name, LogQuery(follow=False)),
        lambda logs, who, name: logs.compose_open(who, ENV_A, name, LogQuery(follow=False)),
        lambda logs, who, name: logs.compose_download(who, ENV_A, name, LogQuery(follow=False)),
    ],
    ids=["members", "tail", "open", "download"],
)
@pytest.mark.parametrize(("who", "name"), [(STANDARD, SHOP), (ADMIN, "absent")])
async def test_a_stack_the_caller_cannot_see_is_not_found_before_the_host_is_asked_anything(
    stack_rig: Rig,
    call: Callable[[StackLogService, Principal, str], Awaitable[object]],
    who: Principal,
    name: str,
) -> None:
    rig = stack_rig
    await seed(rig)
    history(rig.host, project_box(rig.host, 1, SHOP, "web"), Entry(1, "x"))
    before = len(rig.host.requests)

    with pytest.raises(ResourceNotFound):
        await call(rig.fresh().logs, who, name)

    assert rig.host.requests[before:] == []
    assert rig.engine.stream_slots.open_count == 0


async def test_the_filter_picks_services_before_the_member_cap_and_unknown_names_are_ignored(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    web_count = MAX_MEMBERS + 5
    for number in range(1, web_count + 1):
        history(rig.host, replica(rig.host, number, "web"), Entry(number % 50, f"web{number}"))
    for number in (1, 2):
        history(rig.host, replica(rig.host, 100 + number, "api"), Entry(55, f"api{number}"))
    logs = rig.fresh().logs

    everything = await logs.compose_members(ADMIN, ENV_A, SHOP)
    only_api = await logs.compose_members(ADMIN, ENV_A, SHOP, ["api", "ghost"])
    only_ghost = await logs.compose_members(ADMIN, ENV_A, SHOP, ["ghost"])
    filtered_tail = await rig.fresh().logs.compose_tail(
        ADMIN, ENV_A, SHOP, LogQuery(follow=False), ["api"]
    )

    assert (everything.members, everything.over_cap) == (MAX_MEMBERS, web_count + 2 - MAX_MEMBERS)
    assert (only_api.members, only_api.over_cap, only_api.selected) == (2, 0, ("api",))
    assert (only_ghost.members, only_ghost.selected) == (MAX_MEMBERS, ("api", "web"))
    assert sorted(line.text for line in filtered_tail.lines) == ["api1", "api2"]


async def test_members_past_the_cap_are_taken_by_service_name_then_replica_number(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    boxes = [replica(rig.host, number, "web") for number in range(1, MAX_MEMBERS + 4)]
    for number, box in enumerate(boxes, start=1):
        history(rig.host, box, Entry(1, f"line{number}"))

    tail = await rig.fresh().logs.compose_tail(ADMIN, ENV_A, SHOP, LogQuery(follow=False))

    asked = {request.path for request in rig.host.requests if request.path.endswith("/logs")}
    assert asked == {f"/containers/{box.id}/logs" for box in boxes[:MAX_MEMBERS]}
    assert sorted(line.origin for line in tail.lines) == sorted(
        f"web.{number}" for number in range(1, MAX_MEMBERS + 1)
    )


async def test_the_merge_buffer_drops_the_oldest_lines_and_says_so_first(
    stack_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = stack_rig
    await seed(rig)
    first = project_box(rig.host, 1, SHOP, "a")
    second = project_box(rig.host, 2, SHOP, "b")
    history(rig.host, first, *(Entry(n, "x" * 100) for n in range(0, 10, 2)))
    history(rig.host, second, *(Entry(n, "y" * 100) for n in range(1, 10, 2)))
    monkeypatch.setattr(stack_logs, "MERGE_BYTES", 450)

    items = await drained(
        await rig.services.logs.compose_open(ADMIN, ENV_A, SHOP, LogQuery(follow=False))
    )

    batches = [item for item in items if isinstance(item, LogBatch)]
    stamps = [line.timestamp_ns for batch in batches for line in batch.lines]
    assert notices(items) == ["[older lines were left out of the merged output]"]
    assert isinstance(items[0], StackLogNotice)
    assert stamps == [ns(6), ns(7), ns(8), ns(9)]
    assert batches[-1].event_id == ns(9)


async def test_a_followed_stack_delivers_the_merged_tail_then_each_live_line_as_it_arrives(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    before = set(asyncio.all_tasks())
    running = project_box(rig.host, 1, SHOP, "web")
    stopped = project_box(rig.host, 2, SHOP, "job", state="exited")
    history_then_live(
        rig.host,
        running,
        [Entry(1, "old"), Entry(3, "newer")],
        [Entry(3, "newer"), Entry(9, "live")],
    )
    history(rig.host, stopped, Entry(2, "finished", stderr=True))

    stream = await rig.services.logs.compose_open(ADMIN, ENV_A, SHOP, LogQuery(follow=True))
    items = await read_until(stream, {"live"})

    batches = [item for item in items if isinstance(item, LogBatch)]
    assert line_texts(items) == ["old", "finished", "newer", "live"]
    assert [batch.event_id for batch in batches] == [ns(1), ns(2), ns(3), ns(9)]
    followed = log_requests(rig.host, running)[-1].query
    assert followed["follow"] == "true"
    assert followed["since"] == "1767225603.000000000"
    assert "tail" not in followed
    assert len(log_requests(rig.host, stopped)) == 1
    assert rig.engine.stream_slots.open_count == 0
    assert rig.engine.stream_slots.stack_open_count(ENV_A) == 0
    assert await leftover_tasks(before) == set()


async def test_a_stack_whose_members_have_all_stopped_ends_after_the_tail(stack_rig: Rig) -> None:
    rig = stack_rig
    await seed(rig)
    stopped = project_box(rig.host, 1, SHOP, "job", state="exited")
    history(rig.host, stopped, Entry(1, "done"))
    stream = await rig.services.logs.compose_open(ADMIN, ENV_A, SHOP, LogQuery(follow=True))

    items = await drained(stream)

    assert line_texts(items) == ["done"]
    assert stream.end_reason == "finished"


async def test_a_reconnect_follows_every_running_member_after_the_event_id_and_skips_the_tail(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    running = project_box(rig.host, 1, SHOP, "web")
    stopped = project_box(rig.host, 2, SHOP, "job", state="exited")
    rig.host.route(
        "GET",
        f"/containers/{running.id}/logs",
        Stream([frames(Entry(1, "seen"), Entry(2, "seen too"), Entry(9, "new"))], Ending.HOLD),
    )
    history(rig.host, stopped, Entry(1, "history"))

    stream = await rig.services.logs.compose_open(
        ADMIN, ENV_A, SHOP, LogQuery(follow=True), last_event_id=str(ns(2))
    )
    items = await read_until(stream, {"new"})

    assert line_texts(items) == ["new"]
    assert log_requests(rig.host, stopped) == []
    [request] = log_requests(rig.host, running)
    assert request.query["since"] == "1767225602.000000000"
    assert "tail" not in request.query


async def test_a_quiet_member_is_followed_from_the_hosts_position_when_its_clock_is_behind(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    quiet = project_box(rig.host, 1, SHOP, "quiet")
    busy = project_box(rig.host, 2, SHOP, "busy")
    rig.host.route(
        "GET",
        f"/containers/{quiet.id}/logs",
        Reply(200, b"", MULTIPLEXED),
        Stream([frames(Entry(30, "from-a-slow-clock"))], Ending.HOLD),
    )
    history_then_live(rig.host, busy, [Entry(1, "b1")], [Entry(1, "b1"), Entry(40, "b2")])

    stream = await rig.services.logs.compose_open(ADMIN, ENV_A, SHOP, LogQuery(follow=True))
    items = await read_until(stream, {"b2", "from-a-slow-clock"})

    quiet_follow = log_requests(rig.host, quiet)[-1].query
    busy_follow = log_requests(rig.host, busy)[-1].query
    assert sorted(line_texts(items)) == ["b1", "b2", "from-a-slow-clock"]
    assert (quiet_follow["tail"], "since" in quiet_follow) == ("0", False)
    assert (busy_follow["since"], "tail" in busy_follow) == ("1767225601.000000000", False)


async def test_a_tail_that_runs_out_of_time_keeps_what_it_read_and_says_it_was_cut(
    stack_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = stack_rig
    await seed(rig)
    box = project_box(rig.host, 1, SHOP, "web")
    rig.host.route(
        "GET", f"/containers/{box.id}/logs", Stream([frames(Entry(1, "early"))], Ending.HOLD)
    )
    monkeypatch.setattr(stack_logs, "PAGE_SECONDS", 0.3)

    tail = await rig.services.logs.compose_tail(ADMIN, ENV_A, SHOP, LogQuery(follow=False))
    stream = await rig.fresh().logs.compose_open(ADMIN, ENV_A, SHOP, LogQuery(follow=False))
    items = await drained(stream)

    assert ([line.text for line in tail.lines], tail.cut) == (["early"], True)
    assert line_texts(items) == ["early"]
    assert notices(items) == [
        "[older lines may be missing: reading stopped at the 0.3-second limit]"
    ]
    assert rig.engine.stream_slots.open_count == 0


async def test_a_stream_that_reaches_its_lifetime_asks_the_browser_to_reconnect(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    box = project_box(rig.host, 1, SHOP, "web")
    history_then_live(rig.host, box, [Entry(1, "old")], [])
    service = limited(rig, LogLimits(lifetime=0.3, keepalive=0.1))

    stream = await service.compose_open(ADMIN, ENV_A, SHOP, LogQuery(follow=True))
    items = await drained(stream)

    assert line_texts(items) == ["old"]
    assert stream.end_reason == "lifetime"
    assert rig.engine.stream_slots.open_count == 0


async def test_a_member_that_cannot_be_read_is_a_notice_and_every_member_failing_is_unavailable(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    readable = project_box(rig.host, 1, SHOP, "a")
    broken = project_box(rig.host, 2, SHOP, "b")
    history(rig.host, readable, Entry(1, "ok"))
    rig.host.route("GET", f"/containers/{broken.id}/logs", HOST_FAILURE)

    tail = await rig.fresh().logs.compose_tail(ADMIN, ENV_A, SHOP, LogQuery(follow=False))
    items = await drained(
        await rig.fresh().logs.compose_open(ADMIN, ENV_A, SHOP, LogQuery(follow=False))
    )
    rig.host.route("GET", f"/containers/{readable.id}/logs", HOST_FAILURE)
    dead = await rig.fresh().logs.compose_open(ADMIN, ENV_A, SHOP, LogQuery(follow=False))
    nothing = await drained(dead)

    assert ([line.text for line in tail.lines], tail.unreadable) == (["ok"], 1)
    assert notices(items) == ["[output of b could not be read]"]
    assert (nothing, dead.end_reason) == ([], "unavailable")
    with pytest.raises(LogsUnavailable):
        await rig.fresh().logs.compose_tail(ADMIN, ENV_A, SHOP, LogQuery(follow=False))
    assert rig.engine.stream_slots.open_count == 0


async def test_a_host_that_cannot_list_the_containers_gives_the_view_slot_back(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    rig.host.listing_status = 500

    with pytest.raises(Exception, match=r"."):
        await rig.fresh().logs.compose_open(ADMIN, ENV_A, SHOP, LogQuery(follow=False))

    assert rig.engine.stream_slots.open_count == 0
    assert rig.engine.stream_slots.stack_open_count(ENV_A) == 0


async def test_an_environment_holds_three_stack_views_across_every_entry_point_and_gets_one_back(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    history(rig.host, project_box(rig.host, 1, SHOP, "web"), Entry(1, "x"))
    query = LogQuery(follow=False)
    held = [await rig.fresh().logs.compose_open(ADMIN, ENV_A, SHOP, query) for _ in range(3)]
    refused: list[Awaitable[object]] = [
        rig.fresh().logs.compose_open(ADMIN, ENV_A, SHOP, query),
        rig.fresh().logs.compose_tail(ADMIN, ENV_A, SHOP, query),
        rig.fresh().logs.compose_download(ADMIN, ENV_A, SHOP, query),
    ]

    outcomes = [await asyncio.gather(call, return_exceptions=True) for call in refused]
    await held[0].aclose()
    await held[0].aclose()
    again = await rig.fresh().logs.compose_open(ADMIN, ENV_A, SHOP, query)
    for view in (*held[1:], again):
        await view.aclose()

    assert all(isinstance(outcome[0], TooManyStreams) for outcome in outcomes)
    assert rig.engine.stream_slots.open_count == 0
    assert rig.engine.stream_slots.stack_open_count(ENV_A) == 0


async def test_a_standard_user_holds_one_stack_view_and_standard_users_leave_one_for_an_admin(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    history(rig.host, project_box(rig.host, 1, SHOP, "web"), Entry(1, "x"))
    await control(
        rig.access,
        stack_key(SHOP),
        level="read",
        users=(MEMBER.user_id, STANDARD.user_id, PEER.user_id),
    )
    query = LogQuery(follow=False)
    first = await rig.fresh().logs.compose_open(MEMBER, ENV_A, SHOP, query)

    with pytest.raises(TooManyStreams) as same_user:
        await rig.fresh().logs.compose_open(MEMBER, ENV_A, SHOP, query)
    second = await rig.fresh().logs.compose_open(STANDARD, ENV_A, SHOP, query)
    with pytest.raises(TooManyStreams) as standard_total:
        await rig.fresh().logs.compose_open(PEER, ENV_A, SHOP, query)
    administrator = await rig.fresh().logs.compose_open(ADMIN, ENV_A, SHOP, query)
    with pytest.raises(TooManyStreams) as environment_total:
        await rig.fresh().logs.compose_open(ADMIN, ENV_A, SHOP, query)
    await first.aclose()
    freed = await rig.fresh().logs.compose_open(MEMBER, ENV_A, SHOP, query)
    for view in (second, administrator, freed):
        await view.aclose()

    assert same_user.value.scope == "user"
    assert "stack log view" in str(same_user.value)
    assert standard_total.value.scope == "process"
    assert environment_total.value.scope == "process"
    assert rig.engine.stream_slots.open_count == 0
    assert rig.engine.stream_slots.stack_open_count(ENV_A) == 0


async def test_a_caller_refused_a_slot_costs_the_host_no_request_at_all(stack_rig: Rig) -> None:
    rig = stack_rig
    await seed(rig)
    history(rig.host, project_box(rig.host, 1, SHOP, "web"), Entry(1, "x"))
    query = LogQuery(follow=False)
    held = [await rig.fresh().logs.compose_open(ADMIN, ENV_A, SHOP, query) for _ in range(3)]
    before = len(rig.host.requests)

    for call in (
        rig.fresh().logs.compose_open,
        rig.fresh().logs.compose_download,
        rig.fresh().logs.compose_tail,
    ):
        with pytest.raises(TooManyStreams):
            await call(ADMIN, ENV_A, SHOP, query)
    refused_requests = rig.host.requests[before:]
    for view in held:
        await view.aclose()

    assert refused_requests == []


async def test_a_text_download_merges_the_newest_lines_with_the_prefix_after_the_timestamp(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    first = project_box(rig.host, 1, SHOP, "a")
    second = project_box(rig.host, 2, SHOP, "b")
    history(rig.host, first, Entry(1, "a1"), Entry(4, "a4", stderr=True))
    history(rig.host, second, Entry(2, "b2"), Entry(3, "b3"))

    download = await rig.fresh().logs.compose_download(
        ADMIN, ENV_A, SHOP, LogQuery(tail=10, follow=False)
    )
    body = await body_of(download)

    assert body.splitlines() == [
        f"stdout {clock_text(1)} a a1",
        f"stdout {clock_text(2)} b b2",
        f"stdout {clock_text(3)} b b3",
        f"stderr {clock_text(4)} a a4",
    ]
    assert download.filename.startswith("shop-") and download.filename.endswith(".log")
    assert download.media_type == "text/plain; charset=utf-8"
    assert download.end_reason == "finished"
    assert rig.engine.stream_slots.open_count == 0


async def test_an_ndjson_download_has_the_service_and_container_name_beside_the_log_fields(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    first = project_box(rig.host, 1, SHOP, "a")
    second = project_box(rig.host, 2, SHOP, "b")
    history(rig.host, first, Entry(1, "a1"), Entry(4, "keep a4", stderr=True))
    history(rig.host, second, Entry(2, "b2"), Entry(3, "KEEP b3"))

    all_lines = await rig.fresh().logs.compose_download(
        ADMIN, ENV_A, SHOP, LogQuery(tail=10, follow=False, format="ndjson")
    )
    kept = await rig.fresh().logs.compose_download(
        ADMIN, ENV_A, SHOP, LogQuery(tail=10, follow=False, format="ndjson", filter="keep")
    )
    rows = records_of(await body_of(all_lines))
    filtered = records_of(await body_of(kept))

    assert {key: rows[0][key] for key in ("time", "stream", "message")} == {
        "time": clock_text(1),
        "stream": "stdout",
        "message": "a1",
    }
    assert (rows[0]["schema_version"], rows[0]["format"]) == (SCHEMA_VERSION, "text")
    assert (rows[0]["container_name"], rows[0]["compose_project"], rows[0]["compose_service"]) == (
        "shop-a-1",
        SHOP,
        "a",
    )
    assert (rows[0]["environment_id"], rows[0]["environment_name"], rows[0]["image"]) == (
        ENV_A,
        "environment-a",
        "nginx:1",
    )
    assert [row["message"] for row in rows] == ["a1", "b2", "KEEP b3", "keep a4"]
    assert [list(row) for row in rows] == [COMMON_KEYS + CONTEXT_KEYS + CONTAINER_KEYS] * 4
    assert [row["message"] for row in filtered] == ["KEEP b3", "keep a4"]
    assert all_lines.filename.endswith(".ndjson")
    assert all_lines.media_type == "application/x-ndjson"


async def test_a_unicode_line_break_in_a_message_cannot_split_an_ndjson_record(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    history(
        rig.host,
        project_box(rig.host, 1, SHOP, "a"),
        Entry(1, f"one{LINE_SEPARATOR}two"),
        Entry(2, "next"),
    )

    download = await rig.fresh().logs.compose_download(
        ADMIN, ENV_A, SHOP, LogQuery(tail=10, follow=False, format="ndjson")
    )
    body = await body_of(download)

    assert LINE_SEPARATOR not in body
    assert len(body.splitlines()) == 2
    assert [row["message"] for row in records_of(body)] == [f"one{LINE_SEPARATOR}two", "next"]


async def test_a_download_without_a_tail_streams_member_after_member_and_holds_a_slot_until_closed(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    first = project_box(rig.host, 1, SHOP, "a")
    second = project_box(rig.host, 2, SHOP, "b")
    history(rig.host, first, Entry(1, "a1"), Entry(4, "a4"))
    history(rig.host, second, Entry(2, "b2"), Entry(3, "b3"))

    download = await rig.fresh().logs.compose_download(
        ADMIN, ENV_A, SHOP, LogQuery(tail=None, follow=False, format="ndjson")
    )
    held = rig.engine.stream_slots.stack_open_count(ENV_A)
    rows = records_of(await body_of(download))

    assert [(row["compose_service"], row["message"]) for row in rows] == [
        ("a", "a1"),
        ("a", "a4"),
        ("b", "b2"),
        ("b", "b3"),
    ]
    assert (held, rig.engine.stream_slots.stack_open_count(ENV_A)) == (1, 0)
    assert "tail" not in log_requests(rig.host, first)[0].query
    assert download.end_reason == "finished"


@pytest.mark.parametrize("tail", [10, None])
async def test_an_ndjson_download_of_a_large_batch_encodes_off_the_event_loop(
    stack_rig: Rig, monkeypatch: pytest.MonkeyPatch, tail: int | None
) -> None:
    rig = stack_rig
    await seed(rig)
    history(rig.host, project_box(rig.host, 1, SHOP, "a"), Entry(1, "a1"), Entry(2, "a2"))
    monkeypatch.setattr(container_logs, "INLINE_DECODE_BYTES", 0)
    threads = encoding_threads(monkeypatch, stack_logs)

    download = await rig.fresh().logs.compose_download(
        ADMIN, ENV_A, SHOP, LogQuery(tail=tail, follow=False, format="ndjson")
    )
    rows = records_of(await body_of(download))

    assert [row["message"] for row in rows] == ["a1", "a2"]
    assert threading.get_ident() not in threads


async def test_a_merged_download_stops_at_the_line_cap_and_says_older_output_was_left_out(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    history(rig.host, project_box(rig.host, 1, SHOP, "a"), *(Entry(n, f"a{n}") for n in range(5)))
    history(rig.host, project_box(rig.host, 2, SHOP, "b"), *(Entry(n, f"b{n}") for n in range(5)))
    service = limited(rig, LogLimits(download_lines=3))

    download = await service.compose_download(ADMIN, ENV_A, SHOP, LogQuery(tail=10, follow=False))
    lines = (await body_of(download)).splitlines()

    assert lines[0] == "[older output was left out: the download is limited to 3 lines and 32 MiB]"
    assert len(lines) == 4
    assert download.end_reason == "finished"


async def test_a_sequential_download_stops_at_the_line_cap_with_a_closing_notice(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    history(rig.host, project_box(rig.host, 1, SHOP, "a"), *(Entry(n, f"a{n}") for n in range(5)))
    history(rig.host, project_box(rig.host, 2, SHOP, "b"), *(Entry(n, f"b{n}") for n in range(5)))
    service = limited(rig, LogLimits(download_lines=3))

    download = await service.compose_download(ADMIN, ENV_A, SHOP, LogQuery(tail=None, follow=False))
    lines = (await body_of(download)).splitlines()

    assert [line.split()[-1] for line in lines[:3]] == ["a0", "a1", "a2"]
    assert lines[3:] == ["[output stopped at the 3-line limit]"]
    assert download.end_reason == "capped"
    assert rig.engine.stream_slots.open_count == 0


async def test_a_sequential_ndjson_download_stops_at_the_byte_cap_with_a_notice_record(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    history(
        rig.host,
        project_box(rig.host, 1, SHOP, "a"),
        *(Entry(n % 50, "x" * 1000) for n in range(1300)),
    )
    service = limited(rig, LogLimits(download_bytes=MIB))

    download = await service.compose_download(
        ADMIN, ENV_A, SHOP, LogQuery(tail=None, follow=False, format="ndjson")
    )
    body = await body_of(download)

    rows = records_of(body)
    assert rows[-1] == {
        "schema_version": SCHEMA_VERSION,
        "notice": "[output stopped at the 1 MiB limit]",
    }
    assert all("notice" not in row for row in rows[:-1])
    assert len(body) <= MIB + 100
    assert download.end_reason == "capped"


async def test_a_sequential_download_stops_at_the_time_cap_and_a_merged_one_says_so_too(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    box = project_box(rig.host, 1, SHOP, "a")
    rig.host.route(
        "GET", f"/containers/{box.id}/logs", Stream([frames(Entry(1, "first"))], Ending.HOLD)
    )
    service = limited(rig, LogLimits(download_seconds=0.3))

    sequential = await service.compose_download(
        ADMIN, ENV_A, SHOP, LogQuery(tail=None, follow=False)
    )
    sequential_body = await body_of(sequential)
    merged = await service.compose_download(ADMIN, ENV_A, SHOP, LogQuery(tail=5, follow=False))
    merged_body = await body_of(merged)

    assert sequential_body.splitlines()[-1] == "[output stopped at the 0.3-second limit]"
    assert merged_body.splitlines() == [
        f"stdout {clock_text(1)} a first",
        "[output stopped at the 0.3-second limit]",
    ]
    assert (sequential.end_reason, merged.end_reason) == ("capped", "capped")
    assert rig.engine.stream_slots.open_count == 0


async def test_a_download_names_a_member_it_could_not_read_and_fails_when_none_can_be_read(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed(rig)
    readable = project_box(rig.host, 1, SHOP, "a")
    broken = project_box(rig.host, 2, SHOP, "b")
    history(rig.host, readable, Entry(1, "ok"))
    rig.host.route("GET", f"/containers/{broken.id}/logs", HOST_FAILURE)

    merged = await rig.fresh().logs.compose_download(
        ADMIN, ENV_A, SHOP, LogQuery(tail=5, follow=False)
    )
    sequential = await rig.fresh().logs.compose_download(
        ADMIN, ENV_A, SHOP, LogQuery(tail=None, follow=False)
    )
    merged_lines = (await body_of(merged)).splitlines()
    sequential_lines = (await body_of(sequential)).splitlines()
    rig.host.route("GET", f"/containers/{readable.id}/logs", HOST_FAILURE)

    assert merged_lines[-1] == "[output of b could not be read]"
    assert sequential_lines[-1] == "[output of b could not be read]"
    for tail in (5, None):
        with pytest.raises(LogsUnavailable):
            await rig.fresh().logs.compose_download(
                ADMIN, ENV_A, SHOP, LogQuery(tail=tail, follow=False)
            )
    assert rig.engine.stream_slots.open_count == 0


def swarm_stack(swarm: SwarmRig) -> tuple[list[str], list[str]]:
    host = swarm.host
    web = host.add_service(plain_spec("app_web", replicas=2, Labels={NAMESPACE_LABEL: "App"}))
    worker = host.add_service(plain_spec("app_worker", Labels={NAMESPACE_LABEL: "app"}))
    host.add_service(plain_spec("other_web", Labels={NAMESPACE_LABEL: "Other"}))
    web_tasks = host.service_tasks(web)
    worker_tasks = host.service_tasks(worker)
    host.add_log(web_tasks[0], stamp(1), "w1a")
    host.add_log(web_tasks[1], stamp(2), "w2a", "err")
    host.add_log(web_tasks[0], stamp(4), "w1b")
    host.add_log(worker_tasks[0], stamp(3), "k1")
    return web_tasks, worker_tasks


async def test_a_swarm_tail_merges_the_services_with_service_and_slot_prefixes(
    swarm: SwarmRig,
) -> None:
    web_tasks, worker_tasks = swarm_stack(swarm)

    members = await swarm.sw.stack_logs.swarm_members(ADMIN, ENV_A, "app")
    tail = await swarm.sw.stack_logs.swarm_tail(
        ADMIN, ENV_A, "app", LogQuery(tail=10, follow=False)
    )

    assert (members.members, members.hidden, members.services) == (
        2,
        0,
        ("app_web", "app_worker"),
    )
    assert [line.text for line in tail.lines] == ["w1a", "w2a", "k1", "w1b"]
    assert [line.origin for line in tail.lines] == [
        "app_web.1",
        "app_web.2",
        "app_worker.1",
        "app_web.1",
    ]
    assert [line.task_id for line in tail.lines] == [
        web_tasks[0],
        web_tasks[1],
        worker_tasks[0],
        web_tasks[0],
    ]
    assert swarm.engine.stream_slots.open_count == 0


async def test_a_swarm_stack_counts_the_services_a_reader_may_not_see_and_hides_their_lines(
    swarm: SwarmRig,
) -> None:
    swarm_stack(swarm)
    await control(swarm.access, ResourceKey(ENV_A, "stack", "App"), users=(STANDARD.user_id,))
    hidden_service = next(
        service_id
        for service_id, service in swarm.host.services.items()
        if service["Spec"]["Name"] == "app_worker"
    )
    await control(
        swarm.access,
        ResourceKey(ENV_A, "service", hidden_service),
        level="read",
        administrators_only=True,
    )
    logs = swarm.fresh().stack_logs

    members = await logs.swarm_members(STANDARD, ENV_A, "app")
    tail = await logs.swarm_tail(STANDARD, ENV_A, "app", LogQuery(tail=10, follow=False))

    assert (members.members, members.hidden, members.services) == (1, 1, ("app_web",))
    assert {line.service for line in tail.lines} == {"app_web"}
    assert "k1" not in repr(tail)
    assert "app_worker" not in repr(members) + repr(tail)


@pytest.mark.parametrize(
    ("who", "namespace"),
    [(ADMIN, "ghost"), (STANDARD, "Other")],
    ids=["no-such-stack", "stack-not-readable"],
)
async def test_a_swarm_stack_the_caller_cannot_see_is_not_found_and_takes_no_slot(
    swarm: SwarmRig, who: Principal, namespace: str
) -> None:
    swarm_stack(swarm)

    with pytest.raises(ResourceNotFound):
        await swarm.fresh().stack_logs.swarm_tail(who, ENV_A, namespace, LogQuery(follow=False))

    assert swarm.engine.stream_slots.open_count == 0


async def test_a_swarm_stream_ends_and_its_downloads_carry_the_task_ids(swarm: SwarmRig) -> None:
    web_tasks, _ = swarm_stack(swarm)
    before = set(asyncio.all_tasks())

    stream = await swarm.sw.stack_logs.swarm_open(
        ADMIN, ENV_A, "app", LogQuery(tail=10, follow=True)
    )
    items = await drained(stream)
    merged = await swarm.fresh().stack_logs.swarm_download(
        ADMIN, ENV_A, "app", LogQuery(tail=10, follow=False, format="ndjson")
    )
    sequential = await swarm.fresh().stack_logs.swarm_download(
        ADMIN, ENV_A, "app", LogQuery(tail=None, follow=False)
    )
    rows = records_of(await body_of(merged))
    text_lines = (await body_of(sequential)).splitlines()

    assert line_texts(items)[:4] == ["w1a", "w2a", "k1", "w1b"]
    assert stream.end_reason == "finished"
    assert (rows[0]["schema_version"], rows[0]["time"], rows[0]["message"]) == (
        SCHEMA_VERSION,
        stamp(1),
        "w1a",
    )
    assert (rows[0]["service_name"], rows[0]["task_id"], rows[0]["stack_namespace"]) == (
        "app_web",
        web_tasks[0],
        "App",
    )
    assert [list(row) for row in rows] == [COMMON_KEYS + CONTEXT_KEYS + SERVICE_KEYS] * 4
    assert all("container_name" not in row for row in rows)
    assert [line.split()[-2:] for line in text_lines] == [
        ["app_web.1", "w1a"],
        ["app_web.1", "w1b"],
        ["app_web.2", "w2a"],
        ["app_worker.1", "k1"],
    ]
    assert merged.filename.startswith("app-") and merged.filename.endswith(".ndjson")
    assert swarm.engine.stream_slots.open_count == 0
    assert await leftover_tasks(before) == set()


async def test_a_global_service_is_prefixed_by_the_hostname_of_its_node(swarm: SwarmRig) -> None:
    host = swarm.host
    service_id = host.add_service(
        {
            "Name": "app_agent",
            "Mode": {"Global": {}},
            "TaskTemplate": {"ContainerSpec": {"Image": "x"}},
            "Labels": {NAMESPACE_LABEL: "app"},
        }
    )
    [task, _] = host.service_tasks(service_id)
    host.add_log(task, stamp(1), "hello")

    tail = await swarm.sw.stack_logs.swarm_tail(ADMIN, ENV_A, "app", LogQuery(tail=5, follow=False))

    assert [(line.origin, line.text) for line in tail.lines] == [("app_agent.mgr", "hello")]


async def test_a_swarm_service_with_no_history_is_followed_from_the_hosts_position(
    swarm: SwarmRig,
) -> None:
    service_id = swarm.host.add_service(plain_spec("app_idle", Labels={NAMESPACE_LABEL: "app"}))

    stream = await swarm.sw.stack_logs.swarm_open(
        ADMIN, ENV_A, "app", LogQuery(tail=10, follow=True)
    )
    await drained(stream)

    requests = swarm.host.calls("GET", f"/services/{service_id}/logs")
    assert [request.query["tail"] for request in requests] == ["10", "0"]
    assert "since" not in requests[1].query
