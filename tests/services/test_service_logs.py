"""Service logs across tasks: labelled lines, the merged tail, resume, slots and the gate."""

import json
import threading
from typing import Any, cast

import pytest

from app.auth.errors import ResourceNotFound
from app.services import container_logs
from app.services.access import ResourceKey
from app.services.container_logs import LogBatch, LogLimits, LogQuery, LogsUnavailable, OriginLine
from app.services.log_lines import LogLine
from app.services.object_locks import StreamSlots, TooManyStreams
from app.services.service_logs import SERVICE_DEFAULT_TAIL, SERVICE_MAX_TAIL, ServiceLogService
from tests.access_support import ADMIN, ENV_A, PEER, STANDARD, control
from tests.docker_engine.fake_engine import Reply
from tests.docker_engine.helpers import STDOUT, log_frame
from tests.docker_engine.swarm_engine import MANAGER, WORKER, plain_spec, stamp
from tests.services.conftest import SwarmRig
from tests.stream_support import encoding_threads

LOGS = "/services/{}/logs"
FIRST, SECOND = stamp(1), stamp(2)
FIRST_NS = 1_767_225_601_000_000_000
BASE_NS = 1_767_225_600_000_000_000
EVERYTHING = LogQuery(tail=None, follow=False)
EVERYTHING_NDJSON = LogQuery(tail=None, follow=False, format="ndjson")
SCHEMA_VERSION = 2
INSTANCE_NAME = "dockmin"
ENVIRONMENT_NAME = "environment-a"
IMAGE = "busybox:latest"
SERVICE_KEYS = [
    "service_id",
    "service_name",
    "task_id",
    "node_id",
    "image",
    "stack_namespace",
]
NODE_ONLY_TOKEN = "com.docker.swarm.node.id=" + "n" * 25


def query(**params: str) -> LogQuery:
    return LogQuery.from_params({"follow": "0", "tail": "10", **params})


def record_of(
    service_id: str, time: str, stream: str, message: str, task_id: str | None, node_id: str | None
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "time": time,
        "stream": stream,
        "format": "text",
        "message": message,
        "instance_name": INSTANCE_NAME,
        "environment_id": ENV_A,
        "environment_name": ENVIRONMENT_NAME,
        "host_name": None,
        "service_id": service_id,
        "service_name": "web",
        "task_id": task_id,
        "node_id": node_id,
        "image": IMAGE,
        "stack_namespace": None,
    }


def two_task_service(rig: SwarmRig, **extra: Any) -> tuple[str, list[str]]:
    service_id = rig.host.add_service(plain_spec("web", replicas=2, **extra))
    tasks = rig.host.service_tasks(service_id)
    rig.host.add_log(tasks[0], stamp(1), "a1")
    rig.host.add_log(tasks[0], stamp(3), "a2")
    rig.host.add_log(tasks[1], stamp(2), "b1", "err")
    return service_id, tasks


async def drained(stream: Any) -> list[Any]:
    items = [item async for item in stream]
    await stream.aclose()
    return items


async def test_the_tail_merges_tasks_by_time_and_labels_each_line_with_its_slot_and_task(
    swarm: SwarmRig,
) -> None:
    service_id, tasks = two_task_service(swarm)

    tail = await swarm.sw.logs.tail(ADMIN, ENV_A, service_id, query())

    assert [line.text for line in tail.lines] == ["a1", "b1", "a2"]
    assert [line.origin for line in tail.lines] == [
        f"web.1.{tasks[0][:12]}",
        f"web.2.{tasks[1][:12]}",
        f"web.1.{tasks[0][:12]}",
    ]
    assert (tail.name, tail.short_id, tail.tasks, tail.cut) == ("web", service_id[:12], 2, False)
    assert swarm.engine.stream_slots.open_count == 0


async def test_the_engine_applies_the_tail_per_task_so_the_product_cuts_the_merge_to_it(
    swarm: SwarmRig,
) -> None:
    service_id, _ = two_task_service(swarm)

    tail = await swarm.sw.logs.tail(ADMIN, ENV_A, service_id, query(tail="2"))

    assert [line.text for line in tail.lines] == ["b1", "a2"]
    assert tail.cut
    assert swarm.host.calls("GET", LOGS.format(service_id))[-1].query["tail"] == "2"


async def test_the_tail_keeps_only_the_streams_asked_for(swarm: SwarmRig) -> None:
    service_id, _ = two_task_service(swarm)

    tail = await swarm.sw.logs.tail(ADMIN, ENV_A, service_id, query(stderr="1", stdout="0"))

    assert [line.text for line in tail.lines] == ["b1"]


async def test_a_global_service_labels_lines_by_node_hostname(swarm: SwarmRig) -> None:
    service_id = swarm.host.add_service(
        {
            "Name": "agent",
            "Mode": {"Global": {}},
            "TaskTemplate": {"ContainerSpec": {"Image": "x"}},
        }
    )
    [task, _] = swarm.host.service_tasks(service_id)
    swarm.host.add_log(task, FIRST, "hello")

    tail = await swarm.sw.logs.tail(ADMIN, ENV_A, service_id, query())

    assert tail.lines[0].origin == f"agent.mgr.{task[:12]}"


async def test_a_hostname_with_markup_cannot_reach_the_label(swarm: SwarmRig) -> None:
    swarm.host.nodes[MANAGER]["Description"]["Hostname"] = "<svg/onload=alert(1)>"
    service_id = swarm.host.add_service(
        {"Name": "agent", "Mode": {"Global": {}}, "TaskTemplate": {"ContainerSpec": {"Image": "x"}}}
    )
    task = swarm.host.service_tasks(service_id)[0]
    swarm.host.add_log(task, FIRST, "hello")

    tail = await swarm.sw.logs.tail(ADMIN, ENV_A, service_id, query())

    assert "<" not in tail.lines[0].origin and ">" not in tail.lines[0].origin


async def test_a_tty_service_is_read_as_a_raw_stream_with_its_carriage_returns_dropped(
    swarm: SwarmRig,
) -> None:
    spec = plain_spec("tty")
    spec["TaskTemplate"]["ContainerSpec"]["TTY"] = True
    service_id = swarm.host.add_service(spec)
    [task] = swarm.host.service_tasks(service_id)
    swarm.host.add_log(task, FIRST, "first")
    swarm.host.add_log(task, SECOND, "second")

    tail = await swarm.sw.logs.tail(ADMIN, ENV_A, service_id, query())

    assert [line.text for line in tail.lines] == ["first", "second"]


async def test_a_service_whose_log_driver_cannot_be_read_back_is_logs_unavailable(
    swarm: SwarmRig,
) -> None:
    service_id, _ = two_task_service(swarm)
    swarm.host.route("GET", LOGS.format(service_id), Reply(500, b'{"message":"no reading"}'))

    with pytest.raises(LogsUnavailable):
        await swarm.sw.logs.tail(ADMIN, ENV_A, service_id, query())

    assert swarm.engine.stream_slots.open_count == 0


async def test_the_stream_delivers_labelled_lines_and_its_event_id_is_the_newest_timestamp(
    swarm: SwarmRig,
) -> None:
    service_id, tasks = two_task_service(swarm)

    items = await drained(await swarm.sw.logs.open(ADMIN, ENV_A, service_id, query()))

    batches = [b for b in items if isinstance(b, LogBatch)]
    delivered = cast(list[OriginLine], [line for b in batches for line in b.lines])
    assert sorted((line.text, line.origin) for line in delivered) == [
        ("a1", f"web.1.{tasks[0][:12]}"),
        ("a2", f"web.1.{tasks[0][:12]}"),
        ("b1", f"web.2.{tasks[1][:12]}"),
    ]
    assert max(b.event_id or 0 for b in batches) == FIRST_NS + 2_000_000_000
    assert swarm.engine.stream_slots.open_count == 0


async def test_resuming_asks_the_engine_for_the_first_nanosecond_after_the_last_event(
    swarm: SwarmRig,
) -> None:
    service_id, _ = two_task_service(swarm)

    stream = await swarm.sw.logs.open(
        ADMIN, ENV_A, service_id, EVERYTHING, last_event_id=str(FIRST_NS)
    )
    await drained(stream)

    sent = swarm.host.calls("GET", LOGS.format(service_id))[-1].query
    assert sent["since"] == "1767225601.000000001" and "tail" not in sent


async def test_lines_at_or_before_the_resume_point_are_not_delivered_again(
    swarm: SwarmRig,
) -> None:
    service_id, _ = two_task_service(swarm)

    items = await drained(
        await swarm.sw.logs.open(ADMIN, ENV_A, service_id, EVERYTHING, last_event_id=str(FIRST_NS))
    )

    texts = [line.text for b in items if isinstance(b, LogBatch) for line in b.lines]
    assert texts == ["a2", "b1"]


async def test_a_stream_beyond_the_slot_limit_is_refused_and_a_closed_one_frees_its_slot(
    swarm: SwarmRig,
) -> None:
    swarm.engine.stream_slots = StreamSlots(per_user=1)
    swarm.fresh()
    service_id, _ = two_task_service(swarm)

    held = await swarm.sw.logs.open(ADMIN, ENV_A, service_id, EVERYTHING)
    with pytest.raises(TooManyStreams):
        await swarm.sw.logs.open(ADMIN, ENV_A, service_id, EVERYTHING)
    await held.aclose()
    again = await swarm.sw.logs.open(ADMIN, ENV_A, service_id, EVERYTHING)
    await again.aclose()

    assert swarm.engine.stream_slots.open_count == 0


async def test_a_download_with_a_tail_is_merged_in_time_order_with_stream_and_task(
    swarm: SwarmRig,
) -> None:
    service_id, tasks = two_task_service(swarm)

    download = await swarm.sw.logs.download(ADMIN, ENV_A, service_id, query())
    body = b"".join([chunk async for chunk in download]).decode()
    await download.aclose()

    lines = body.splitlines()
    assert [line.rsplit(" ", 1)[-1] for line in lines] == ["a1", "b1", "a2"]
    assert lines[1].startswith(f"stderr {SECOND} web.2.{tasks[1][:12]} ")
    assert download.filename.startswith("web-") and download.filename.endswith(".log")
    assert download.end_reason == "finished" and swarm.engine.stream_slots.open_count == 0


async def test_a_download_of_everything_streams_one_task_after_another_unmerged(
    swarm: SwarmRig,
) -> None:
    service_id, _ = two_task_service(swarm)

    download = await swarm.sw.logs.download(ADMIN, ENV_A, service_id, EVERYTHING)
    body = b"".join([chunk async for chunk in download]).decode()
    await download.aclose()

    assert [line.rsplit(" ", 1)[-1] for line in body.splitlines()] == ["a1", "a2", "b1"]
    assert swarm.engine.stream_slots.open_count == 0


async def test_a_reader_without_access_is_not_found_before_any_log_request_for_every_entry(
    swarm: SwarmRig,
) -> None:
    service_id, _ = two_task_service(swarm)
    before = len(swarm.host.calls("GET", LOGS.format(service_id)))

    for call in (
        lambda: swarm.sw.logs.open(PEER, ENV_A, service_id, query()),
        lambda: swarm.sw.logs.download(PEER, ENV_A, service_id, query()),
        lambda: swarm.sw.logs.tail(PEER, ENV_A, service_id, query()),
        lambda: swarm.sw.logs.authorise(PEER, ENV_A, service_id),
    ):
        with pytest.raises(ResourceNotFound):
            await call()

    assert len(swarm.host.calls("GET", LOGS.format(service_id))) == before
    assert swarm.engine.stream_slots.open_count == 0


@pytest.mark.parametrize("service_id", ["web", "short", "A" * 25, "../x"])
async def test_a_service_id_that_is_not_an_id_is_not_found_by_the_gate(
    swarm: SwarmRig, service_id: str
) -> None:
    with pytest.raises(ResourceNotFound):
        await swarm.sw.logs.authorise(ADMIN, ENV_A, service_id)


async def test_read_access_is_enough_to_read_the_logs(swarm: SwarmRig) -> None:
    service_id, _ = two_task_service(swarm)
    await control(
        swarm.access, ResourceKey(ENV_A, "service", service_id), users=(STANDARD.user_id,)
    )
    swarm.fresh()

    tail = await swarm.sw.logs.tail(STANDARD, ENV_A, service_id, query())

    assert [line.text for line in tail.lines] == ["a1", "b1", "a2"]


async def test_the_default_tail_is_200_and_the_largest_is_5000_for_a_service() -> None:
    default = LogQuery.from_params({}, default_tail=SERVICE_DEFAULT_TAIL, max_tail=SERVICE_MAX_TAIL)

    assert (default.tail, SERVICE_MAX_TAIL) == (200, 5000)


def line_at(ns: int) -> LogLine:
    return LogLine("stdout", "x", None, BASE_NS + ns)


def test_a_batch_event_id_is_the_highest_timestamp_delivered_not_the_last_lines() -> None:
    late_then_early = (line_at(5), line_at(3))

    assert LogBatch("out", late_then_early).event_id == BASE_NS + 3
    assert LogBatch("out", late_then_early, high_water=BASE_NS + 5).event_id == BASE_NS + 5


async def test_a_service_name_with_markup_or_spaces_cannot_reach_a_line_label(
    swarm: SwarmRig,
) -> None:
    service_id = swarm.host.add_service(plain_spec("a b<script>"))
    [task] = swarm.host.service_tasks(service_id)
    swarm.host.add_log(task, FIRST, "hello")

    tail = await swarm.sw.logs.tail(ADMIN, ENV_A, service_id, query())

    origin = tail.lines[0].origin
    assert "<" not in origin and " " not in origin and origin.endswith(f".1.{task[:12]}")


async def test_the_page_tail_stays_inside_its_byte_budget_however_much_the_tasks_wrote(
    swarm: SwarmRig,
) -> None:
    service_id = swarm.host.add_service(plain_spec("web", replicas=3))
    for task in swarm.host.service_tasks(service_id):
        for number in range(40):
            swarm.host.add_log(task, stamp(number), "x" * 1000)
    small = ServiceLogService(
        authoriser=swarm.access.authoriser,
        inspector=swarm.inspector,
        slots=swarm.engine.stream_slots,
        limits=LogLimits(page_bytes=5000),
    )

    tail = await small.tail(ADMIN, ENV_A, service_id, query(tail="1000"))

    assert tail.cut
    assert sum(len(line.text.encode()) for line in tail.lines) <= 5000


async def ndjson_of(download: Any) -> list[dict[str, Any]]:
    body = b"".join([chunk async for chunk in download]).decode()
    await download.aclose()
    assert body.endswith("\n")
    return [json.loads(line) for line in body.split("\n")[:-1]]


async def test_a_merged_ndjson_download_has_time_stream_message_and_task_id_in_time_order(
    swarm: SwarmRig,
) -> None:
    service_id, tasks = two_task_service(swarm)

    download = await swarm.sw.logs.download(ADMIN, ENV_A, service_id, query(format="ndjson"))
    parsed = await ndjson_of(download)

    assert parsed == [
        record_of(service_id, FIRST, "stdout", "a1", tasks[0], MANAGER),
        record_of(service_id, SECOND, "stderr", "b1", tasks[1], WORKER),
        record_of(service_id, stamp(3), "stdout", "a2", tasks[0], MANAGER),
    ]
    assert [list(record)[-6:] for record in parsed] == [SERVICE_KEYS] * 3
    assert [list(record)[:5] for record in parsed] == [
        ["schema_version", "time", "stream", "format", "message"]
    ] * 3
    assert download.media_type == "application/x-ndjson"
    assert download.filename.startswith("web-") and download.filename.endswith(".ndjson")
    assert download.end_reason == "finished"
    assert swarm.engine.stream_slots.open_count == 0


async def test_a_merged_ndjson_download_of_a_large_batch_encodes_off_the_event_loop(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    service_id, _ = two_task_service(swarm)
    monkeypatch.setattr(container_logs, "INLINE_DECODE_BYTES", 0)
    threads = encoding_threads(monkeypatch, container_logs)

    download = await swarm.sw.logs.download(ADMIN, ENV_A, service_id, query(format="ndjson"))
    parsed = await ndjson_of(download)

    assert len(parsed) == 3
    assert threading.get_ident() not in threads


async def test_an_unmerged_ndjson_download_streams_one_task_after_another_with_task_ids(
    swarm: SwarmRig,
) -> None:
    service_id, tasks = two_task_service(swarm)

    download = await swarm.sw.logs.download(ADMIN, ENV_A, service_id, EVERYTHING_NDJSON)
    parsed = await ndjson_of(download)

    assert [(record["message"], record["task_id"]) for record in parsed] == [
        ("a1", tasks[0]),
        ("a2", tasks[0]),
        ("b1", tasks[1]),
    ]
    assert download.filename.endswith(".ndjson")
    assert swarm.engine.stream_slots.open_count == 0


async def test_the_text_filter_applies_to_an_ndjson_download(swarm: SwarmRig) -> None:
    service_id, _ = two_task_service(swarm)

    download = await swarm.sw.logs.download(
        ADMIN, ENV_A, service_id, query(format="ndjson", q="B1")
    )
    parsed = await ndjson_of(download)

    assert [record["message"] for record in parsed] == ["b1"]


async def test_an_unmerged_ndjson_download_that_hits_the_line_cap_ends_with_a_notice_record(
    swarm: SwarmRig,
) -> None:
    service_id, _ = two_task_service(swarm)
    capped = ServiceLogService(
        authoriser=swarm.access.authoriser,
        inspector=swarm.inspector,
        slots=swarm.engine.stream_slots,
        limits=LogLimits(download_lines=2),
    )

    download = await capped.download(ADMIN, ENV_A, service_id, EVERYTHING_NDJSON)
    parsed = await ndjson_of(download)

    assert [record.get("message") for record in parsed] == ["a1", "a2", None]
    assert parsed[-1] == {
        "schema_version": SCHEMA_VERSION,
        "notice": "[output stopped at the 2-line limit]",
    }
    assert download.end_reason == "capped"
    assert swarm.engine.stream_slots.open_count == 0


async def test_a_line_whose_token_names_no_task_has_a_null_task_id(swarm: SwarmRig) -> None:
    service_id, _ = two_task_service(swarm)
    body = log_frame(STDOUT, f"{FIRST} {NODE_ONLY_TOKEN} orphan\n".encode())
    swarm.host.route(
        "GET",
        LOGS.format(service_id),
        Reply(200, body, {"Content-Type": "application/vnd.docker.multiplexed-stream"}),
    )

    download = await swarm.sw.logs.download(ADMIN, ENV_A, service_id, EVERYTHING_NDJSON)
    parsed = await ndjson_of(download)

    assert parsed == [record_of(service_id, FIRST, "stdout", "orphan", None, "n" * 25)]
