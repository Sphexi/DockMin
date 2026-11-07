"""container-log-exporter: what it copies, where it puts it, and how its cursor moves."""

import asyncio
import gzip
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

import app.subsystems.container_log_exporter as exporter
from app.config import Settings
from app.crypto.envelope import SecretLocation
from app.scheduler.model import RunResult
from app.services.container_logs import LogQuery
from app.services.containers import ContainerListTooLarge
from app.subsystems.base import RunContext
from app.subsystems.container_log_exporter import ContainerLogExporter
from tests.access_support import ADMIN, ENV_A
from tests.auth_support import ADMIN_ID
from tests.automation_support import Subject, cid, counts, detail_of
from tests.docker_engine.container_engine import CREATED, ContainerEngine, engine_time
from tests.docker_engine.fake_engine import Reply, Scope, json_reply, split_version
from tests.docker_engine.helpers import log_frame
from tests.docker_engine.swarm_engine import MANAGER, WORKER, SwarmEngine, plain_spec
from tests.object_storage.support import ACCESS, BUCKET, ENDPOINT, SECRET, verify_signature
from tests.subsystems.exporter_support import PREFIX, RETRY_PAUSE, Step, error, nth_put, put_times

ACTION = "container_log_exported"
NANOS = 1_000_000_000
FIRST_SECOND = CREATED + 1000
STDOUT, STDERR = 1, 2
LOGS_ENDPOINT_FAILED = 500
LOGS_NOT_SUPPORTED = 501
SERVER_ERROR = 503
SHORT_READ = 0.05
GONE_AFTER = 7200
RUN_SPACING = 60
FIRST_CONTAINER, SECOND_CONTAINER, THIRD_CONTAINER = 1, 2, 3
KEPT_ALL = "SELECT container_id FROM container_log_cursor ORDER BY container_id"
EVERYTHING = {"kind": "environment"}
WEB = {"kind": "container", "names": ["web"]}
ACCESS_DENIED = error(403, "AccessDenied")
STAMP = "2026-01-01T00:00:00Z"
RULE_FOLDER = "rule-1-20300101T120000"
JSON = {"Content-Type": "application/json"}
MULTIPLEXED = {"Content-Type": "application/vnd.docker.multiplexed-stream"}

type Line = tuple[int, int, str]


def at(second: int, nano: int = 0) -> int:
    """Return Unix nanoseconds `second` seconds after the first log second."""
    return (FIRST_SECOND + second) * NANOS + nano


def stamped(nanoseconds: int) -> str:
    seconds, nano = divmod(nanoseconds, NANOS)
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S") + f".{nano:09d}Z"


def since_ns(text: str) -> int:
    seconds, _, nano = text.partition(".")
    return int(seconds) * NANOS + int((nano or "0").ljust(9, "0"))


class Bucket:
    """Stores what is put, checks every signature, and can be scripted to fail."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.stored: list[str] = []
        self.attempts: list[str] = []
        self.script: list[Step] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        verify_signature(request)
        if request.method == "PUT":
            self.attempts.append(request.url.path.split("/", 2)[2])
        for step in self.script:
            outcome = step(request)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is not None:
                return outcome
        if request.method == "PUT":
            key = request.url.path.split("/", 2)[2]
            self.stored.append(key)
            self.objects[key] = request.content
        return httpx.Response(200)

    def lines(self, key: str) -> list[dict[str, Any]]:
        text = gzip.decompress(self.objects[key]).decode("utf-8")
        return [json.loads(each) for each in text.splitlines()]

    def messages(self) -> list[str]:
        return [line["message"] for key in self.stored for line in self.lines(key)]


def make_target(subject: Subject, rig: Any) -> int:
    """Insert an object storage target with a sealed secret and return its id."""
    sql = subject.scheduling.sql
    sql(
        "INSERT INTO object_storage_target (name, endpoint_url, region, bucket, "
        "access_key_id, secret_access_key_ciphertext, use_path_style, verify_tls, ca_bundle, "
        "backup_prefix, activity_log_prefix, retention_count, created_at, updated_at) "
        "VALUES ('logs', ?, 'us-east-1', ?, ?, 'x', 1, 1, NULL, 'backups', ?, NULL, ?, ?)",
        (ENDPOINT, BUCKET, ACCESS, PREFIX, STAMP, STAMP),
    )
    target_id = int(sql("SELECT max(id) FROM object_storage_target")[0][0])
    sealed = rig.engine.vault.seal(
        SECRET, SecretLocation("object_storage_target", "secret_access_key_ciphertext", target_id)
    )
    sql(
        "UPDATE object_storage_target SET secret_access_key_ciphertext = ? WHERE id = ?",
        (sealed, target_id),
    )
    return target_id


class World:
    """One exporter over a fake daemon that serves scripted logs and a fake bucket."""

    def __init__(self, rig: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        self.bucket = Bucket()
        self.sleeps: list[float] = []
        self.logs: dict[str, list[Line]] = {}
        self.reads: list[tuple[str, dict[str, str]]] = []
        self.refused: dict[str, int] = {}
        self.stalled: set[str] = set()

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        def build(deps: Any) -> ContainerLogExporter:
            return ContainerLogExporter(
                deps, transport=httpx.MockTransport(self.bucket.handler), sleep=sleep
            )

        self.subject = Subject(rig, settings, build)
        self.rig = rig
        self.host: ContainerEngine = rig.host
        monkeypatch.setattr(self.host, "_reply", self._replying(self.host._reply))
        self.target_id = make_target(self.subject, rig)

    def _replying(self, original: Callable[[Scope, bytes], Any]) -> Callable[[Scope, bytes], Any]:
        async def reply(scope: Scope, body: bytes) -> Reply | None:
            _, path = split_version(scope["path"])
            parts = path.strip("/").split("/")
            if scope["method"] != "GET" or parts[0] != "containers" or parts[-1] != "logs":
                served: Reply | None = await original(scope, body)
                return served
            query = {k: v[-1] for k, v in parse_qs(scope["query_string"].decode()).items()}
            box = self.host.resolve(parts[1])
            assert box is not None
            self.reads.append((box.id, query))
            if box.id in self.stalled:
                await asyncio.Event().wait()
            if box.id in self.refused:
                return Reply(self.refused[box.id], b'{"message":"refused"}', JSON)
            floor = since_ns(query["since"]) if "since" in query else 0
            body_out = b"".join(
                log_frame(stream, f"{stamped(ns)} {text}\n".encode())
                for ns, stream, text in self.logs.get(box.id, [])
                if ns >= floor
            )
            return Reply(200, body_out, MULTIPLEXED)

        return reply

    async def rule(self, selector: dict[str, Any]) -> int:
        """Create a rule over the selector and return its job id."""
        _, job = await self.subject.rule(
            selector, {"object_storage_target_id": self.target_id}, environment_id=ENV_A
        )
        return job

    def add(self, number: int, name: str, *lines: Line, **fields: Any) -> str:
        """Add a container that prints the lines and return its id."""
        box = self.host.add(cid(number), name, **fields)
        self.logs[box.id] = list(lines)
        return box.id

    async def run(self, job: int, *, dry: bool = False) -> RunResult:
        return await self.subject.run(job, dry=dry)

    def rows(self, statement: str) -> list[tuple[Any, ...]]:
        return [tuple(row) for row in self.subject.scheduling.sql(statement)]

    def cursors(self) -> dict[str, tuple[int, int, str | None]]:
        found = self.rows(
            "SELECT container_id, last_timestamp_ns, lines_at_last, object_path "
            "FROM container_log_cursor"
        )
        return {row[0]: (row[1], row[2], row[3]) for row in found}

    def copied(self) -> list[str]:
        """Return the containers whose cursor records copied lines."""
        return [key for key, (_, _, path) in self.cursors().items() if path is not None]

    def failures(self) -> list[dict[str, Any]]:
        return self.subject.events(ACTION)

    def read_ids(self) -> list[str]:
        return [container for container, _ in self.reads]

    def forget_runs_before_now(self) -> None:
        stamp = self.subject.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        self.subject.scheduling.sql("DELETE FROM job_run WHERE started_at < ?", (stamp,))


@pytest.fixture
def world(container_rig: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(container_rig, settings, monkeypatch)


async def test_lines_sharing_a_timestamp_are_neither_repeated_nor_dropped_across_runs(
    world: World,
) -> None:
    world.add(FIRST_CONTAINER, "web", (at(5), STDOUT, "a"))
    job = await world.rule(WEB)

    await world.run(job)
    world.logs[cid(FIRST_CONTAINER)] += [(at(5), STDERR, "b"), (at(5), STDOUT, "c")]
    await world.run(job)
    await world.run(job)

    assert world.bucket.messages() == ["a", "b", "c"]
    assert len(set(world.bucket.stored)) == len(world.bucket.stored)


async def test_a_cap_that_splits_lines_sharing_a_timestamp_resumes_without_a_gap_or_a_repeat(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter, "MAX_LINES_PER_OBJECT", 2)
    world.add(
        FIRST_CONTAINER,
        "web",
        (at(5), STDOUT, "a"),
        (at(5), STDERR, "b"),
        (at(5), STDOUT, "c"),
        (at(6), STDOUT, "d"),
    )
    job = await world.rule(WEB)

    results = [await world.run(job) for _ in range(3)]

    assert world.bucket.messages() == ["a", "b", "c", "d"]
    assert len(set(world.bucket.stored)) == 2
    assert [counts(each)["more_pending"] for each in results] == [True, False, False]


async def test_a_tail_whose_streams_print_out_of_order_is_exported_once(
    world: World,
) -> None:
    world.add(FIRST_CONTAINER, "web", (at(7), STDOUT, "late"), (at(6), STDERR, "early"))
    job = await world.rule(WEB)

    await world.run(job)
    await world.run(job)
    world.logs[cid(FIRST_CONTAINER)].append((at(8), STDOUT, "new"))
    await world.run(job)

    assert world.bucket.messages() == ["late", "early", "new"]
    assert world.cursors()[cid(FIRST_CONTAINER)][:2] == (at(8), 1)


async def test_a_recreated_container_starts_a_cursor_of_its_own_at_its_creation(
    world: World,
) -> None:
    old = world.add(FIRST_CONTAINER, "web", (at(1), STDOUT, "before"))
    job = await world.rule(WEB)
    await world.run(job)
    del world.host.boxes[old]
    created = FIRST_SECOND + 3
    new = world.add(
        SECOND_CONTAINER,
        "web",
        (at(1), STDOUT, "from the old container"),
        (at(4), STDOUT, "after"),
        created_text=engine_time(created),
    )

    await world.run(job)

    assert world.bucket.messages() == ["before", "after"]
    assert since_ns(dict(world.reads)[new]["since"]) == created * NANOS
    assert sorted(world.copied()) == [old, new]


async def test_an_object_is_keyed_by_rule_environment_container_and_first_line_and_holds_the_schema(
    world: World,
) -> None:
    web = world.add(FIRST_CONTAINER, "web", (at(5, 7), STDERR, "hello"))
    job = await world.rule(WEB)

    await world.run(job)

    [key] = world.bucket.stored
    assert key == (
        f"{PREFIX}/container-logs/{RULE_FOLDER}/{ENV_A}/web-{web[:12]}/2023/11/14/"
        f"{at(5, 7):020d}.ndjson.gz"
    )
    [record] = world.bucket.lines(key)
    assert list(record.items()) == [
        ("schema_version", 2),
        ("time", stamped(at(5, 7))),
        ("stream", "stderr"),
        ("format", "text"),
        ("message", "hello"),
        ("instance_name", world.subject.deps.settings.instance_name),
        ("environment_id", ENV_A),
        ("environment_name", "environment-a"),
        ("host_name", None),
        ("container_id", web),
        ("container_name", "web"),
        ("image", "nginx:1"),
        ("compose_project", None),
        ("compose_service", None),
    ]
    assert world.cursors() == {web: (at(5, 7), 1, key)}


async def test_an_exported_object_holds_the_same_records_as_a_download_of_the_same_lines(
    world: World,
) -> None:
    web = world.add(
        FIRST_CONTAINER,
        "web",
        (at(1), STDOUT, "plain line"),
        (at(2), STDERR, '{"level":"Error","msg":"boom"}'),
        (at(3), STDOUT, "[1,2]"),
    )
    job = await world.rule(WEB)

    await world.run(job)
    download = await world.rig.services.logs.download(
        ADMIN, ENV_A, web, LogQuery(follow=False, tail=None, format="ndjson")
    )
    downloaded = b"".join([chunk async for chunk in download])
    await download.aclose()

    [key] = world.bucket.stored
    assert gzip.decompress(world.bucket.objects[key]) == downloaded
    assert [record["format"] for record in world.bucket.lines(key)] == ["text", "json", "text"]


async def test_a_put_that_fails_once_is_retried_after_a_pause_on_the_same_key(
    world: World,
) -> None:
    world.add(FIRST_CONTAINER, "web", (at(1), STDOUT, "x"))
    world.bucket.script.append(put_times(1, error(SERVER_ERROR, "SlowDown")))
    job = await world.rule(WEB)

    result = await world.run(job)

    assert result.outcome == "success"
    assert world.sleeps == [RETRY_PAUSE]
    assert len(world.bucket.attempts) == 2
    assert len(set(world.bucket.attempts)) == 1
    assert world.bucket.stored == world.bucket.attempts[:1]
    assert world.failures() == []


async def test_an_export_repeated_after_a_lost_cursor_overwrites_the_same_object(
    world: World,
) -> None:
    world.add(FIRST_CONTAINER, "web", (at(1), STDOUT, "x"))
    job = await world.rule(WEB)
    await world.run(job)
    world.subject.scheduling.sql("DELETE FROM container_log_cursor")

    await world.run(job)

    assert len(world.bucket.attempts) == 2
    assert len(world.bucket.objects) == 1
    assert world.bucket.messages() == ["x", "x"]


async def test_a_capped_run_reports_more_pending_and_the_next_run_continues(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter, "MAX_LINES_PER_OBJECT", 2)
    world.add(FIRST_CONTAINER, "web", *[(at(n), STDOUT, f"line {n}") for n in range(1, 6)])
    job = await world.rule(WEB)

    results = [await world.run(job) for _ in range(3)]

    assert [counts(each)["more_pending"] for each in results] == [True, True, False]
    assert [counts(each)["lines"] for each in results] == [2, 2, 1]
    assert world.bucket.messages() == [f"line {n}" for n in range(1, 6)]


async def test_a_run_over_more_containers_than_the_cap_examines_the_rest_next_time(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter, "MAX_CONTAINERS_PER_RUN", 2)
    ids = [world.add(n, f"c{n}", (at(n), STDOUT, f"line {n}")) for n in range(1, 5)]
    job = await world.rule(EVERYTHING)

    first = await world.run(job)
    world.subject.advance(RUN_SPACING)
    await world.run(job)

    assert counts(first)["more_pending"] is True
    assert counts(first)["deferred"] == 2
    assert sorted(world.copied()) == ids
    assert world.read_ids() == ids


async def test_containers_never_exported_are_read_first_then_the_one_examined_longest_ago(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter, "MAX_CONTAINERS_PER_RUN", 2)
    first, second, third = (world.add(n, f"c{n}", (at(n), STDOUT, "x")) for n in range(1, 4))
    job = await world.rule(EVERYTHING)
    reads: list[list[str]] = []

    for _ in range(3):
        world.reads.clear()
        await world.run(job)
        reads.append(world.read_ids())
        world.subject.advance(RUN_SPACING)

    assert reads[0] == [first, second]
    assert reads[1] == [third, first]
    assert reads[2][0] == second


async def test_a_cursor_is_pruned_only_for_a_container_that_has_left_the_host(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter, "MAX_CONTAINERS_PER_RUN", 2)
    ids = [world.add(n, f"c{n}", (at(n), STDOUT, "x")) for n in range(1, 5)]
    job = await world.rule(EVERYTHING)
    await world.run(job)
    world.subject.advance(RUN_SPACING)
    await world.run(job)
    assert sorted(world.cursors()) == ids
    del world.host.boxes[ids[0]]
    world.subject.advance(GONE_AFTER)
    world.forget_runs_before_now()
    world.subject.advance(RUN_SPACING)

    await world.run(job)

    assert [row[0] for row in world.rows(KEPT_ALL)] == sorted(ids[1:])


async def test_a_cursor_of_a_gone_container_survives_while_older_runs_are_kept(
    world: World,
) -> None:
    gone = world.add(FIRST_CONTAINER, "web", (at(1), STDOUT, "x"))
    world.add(SECOND_CONTAINER, "db", (at(1), STDOUT, "y"))
    job = await world.rule(EVERYTHING)
    await world.run(job)
    del world.host.boxes[gone]
    world.subject.advance(GONE_AFTER)

    await world.run(job)

    assert gone in world.cursors()


async def test_a_storage_failure_stops_the_run_with_one_failure_row(world: World) -> None:
    ids = [world.add(n, f"c{n}", (at(n), STDOUT, "x")) for n in range(1, 4)]
    world.bucket.script.append(put_times(99, ACCESS_DENIED))
    job = await world.rule(EVERYTHING)

    result = await world.run(job)

    [failure] = world.failures()
    detail = detail_of(failure)
    assert (result.outcome, failure["result"], failure["target_id"]) == (
        "failure",
        "failure",
        ids[0],
    )
    assert result.reason == detail["reason"]
    assert len(world.bucket.attempts) == 1
    assert (detail["storage_status"], detail["storage_code"]) == (403, "AccessDenied")
    assert detail["object_path"] == world.bucket.attempts[0]
    assert detail["lines"] == 1
    assert world.copied() == []
    assert counts(result)["more_pending"] is True


async def test_a_storage_failure_after_a_stored_object_makes_the_run_partial(
    world: World,
) -> None:
    ids = [world.add(n, f"c{n}", (at(n), STDOUT, "x")) for n in range(1, 4)]
    world.bucket.script.append(nth_put(2, ACCESS_DENIED))
    job = await world.rule(EVERYTHING)

    result = await world.run(job)

    [failure] = world.failures()
    assert result.outcome == "partial"
    assert failure["target_id"] == ids[1]
    assert world.copied() == [ids[0]]
    assert len(world.bucket.attempts) == 2


async def test_a_transient_read_failure_fails_one_container_and_the_run_goes_on(
    world: World,
) -> None:
    ids = [world.add(n, f"c{n}", (at(n), STDOUT, f"line {n}")) for n in range(1, 4)]
    world.refused[ids[1]] = LOGS_ENDPOINT_FAILED
    job = await world.rule(EVERYTHING)

    result = await world.run(job)

    [failure] = world.failures()
    assert result.outcome == "partial"
    assert (failure["target_id"], detail_of(failure)["reason"]) == (ids[1], "logs_unavailable")
    assert sorted(world.copied()) == [ids[0], ids[2]]
    assert world.bucket.messages() == ["line 1", "line 3"]


async def test_a_container_whose_logs_cannot_be_read_is_skipped_without_a_failure_on_every_run(
    world: World,
) -> None:
    none = world.add(FIRST_CONTAINER, "silent", host_config={"LogConfig": {"Type": "none"}})
    unsupported = world.add(SECOND_CONTAINER, "remote")
    world.add(THIRD_CONTAINER, "ok", (at(1), STDOUT, "x"))
    world.refused[unsupported] = LOGS_NOT_SUPPORTED
    job = await world.rule(EVERYTHING)

    results = [await world.run(job) for _ in range(ContainerLogExporter.failure_threshold + 1)]

    assert {each.outcome for each in results} == {"success"}
    assert all(counts(each)["logs_not_readable"] == 2 for each in results)
    assert world.failures() == []
    assert none not in world.read_ids()
    assert unsupported in world.read_ids()


async def test_a_read_that_times_out_is_counted_and_the_cursor_does_not_move(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = world.add(FIRST_CONTAINER, "web", (at(1), STDOUT, "x"))
    job = await world.rule(WEB)
    normal = exporter.READ_SECONDS
    monkeypatch.setattr(exporter, "READ_SECONDS", SHORT_READ)
    world.stalled.add(web)

    stalled = await world.run(job)

    assert counts(stalled)["timed_out"] == 1
    assert "up_to_date" not in counts(stalled)
    assert stalled.outcome == "success"
    assert (world.bucket.attempts, world.copied()) == ([], [])

    monkeypatch.setattr(exporter, "READ_SECONDS", normal)
    world.stalled.clear()
    resumed = await world.run(job)

    assert counts(resumed)["exported"] == 1
    assert world.bucket.messages() == ["x"]


async def test_a_dry_run_lists_what_it_would_export_and_reads_and_writes_nothing(
    world: World,
) -> None:
    world.add(FIRST_CONTAINER, "web", (at(1), STDOUT, "x"))
    world.add(SECOND_CONTAINER, "db", (at(1), STDOUT, "y"))
    job = await world.rule(EVERYTHING)

    result = await world.run(job, dry=True)

    detail = counts(result)
    assert result.outcome == "success"
    assert (detail["without_cursor"], detail["with_cursor"]) == (2, 0)
    assert detail["would_export_count"] == 2
    assert detail["next_object_path"].startswith(f"{PREFIX}/container-logs/{RULE_FOLDER}/{ENV_A}/")
    assert world.reads == []
    assert world.bucket.attempts == []
    assert world.cursors() == {}
    assert world.failures() == []


async def test_a_rule_whose_owner_is_no_longer_an_administrator_fails_without_reading(
    world: World,
) -> None:
    world.add(FIRST_CONTAINER, "web", (at(1), STDOUT, "x"))
    job = await world.rule(EVERYTHING)
    world.subject.scheduling.sql("UPDATE user SET role = 'standard' WHERE id = ?", (ADMIN_ID,))

    result = await world.run(job)

    assert (result.outcome, result.reason) == ("failure", "owner_access_lost")
    assert world.reads == []
    assert world.bucket.attempts == []


async def test_a_container_with_nothing_to_export_is_marked_examined_and_loses_no_later_line(
    world: World,
) -> None:
    quiet = world.add(FIRST_CONTAINER, "web", created_text=engine_time(FIRST_SECOND))
    job = await world.rule(WEB)

    first = await world.run(job)
    marked = world.cursors()[quiet]
    world.logs[quiet].append((at(0), STDOUT, "first line"))
    await world.run(job)

    assert counts(first)["up_to_date"] == 1
    assert marked[2] is None
    assert world.bucket.messages() == ["first line"]
    assert world.copied() == [quiet]


SERVICE = {"kind": "service", "names": ["web"]}
SERVICE_FOLDER = f"{PREFIX}/service-logs/{RULE_FOLDER}/{ENV_A}"
NOT_LISTED_SERVICE = "x" * 25
TERMINAL = "shutdown"
UNLISTED_LINES = 2


class ServiceWorld:
    """One exporter over a fake Swarm manager whose tasks print scripted lines."""

    def __init__(self, rig: Any, settings: Settings) -> None:
        self.bucket = Bucket()
        self.sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        def build(deps: Any) -> ContainerLogExporter:
            return ContainerLogExporter(
                deps, transport=httpx.MockTransport(self.bucket.handler), sleep=sleep
            )

        self.subject = Subject(rig, settings, build)
        self.rig = rig
        self.swarm: SwarmEngine = rig.host
        self.target_id = make_target(self.subject, rig)
        self._serve_task_logs()

    def _serve_task_logs(self) -> None:
        listing = self.swarm._tasks

        def tasks(parts: list[str], query: dict[str, str]) -> Reply:
            if len(parts) != 3 or parts[2] != "logs":
                return listing(parts, query)
            task = self.swarm.tasks.get(parts[1])
            if task is None:
                return json_reply({"message": "no task"}, 404)
            owner = self.swarm.services[task["ServiceID"]]
            return self.swarm._service_logs({**owner, "log_tasks": [parts[1]]}, query)

        self.swarm._tasks = tasks  # type: ignore[method-assign]

    def add(self, name: str = "web", replicas: int = 1) -> tuple[str, list[str]]:
        """Add a service whose tasks were all created at the first log second."""
        service = self.swarm.add_service(plain_spec(name, replicas=replicas))
        tasks = self.swarm.service_tasks(service)
        for task in tasks:
            self.swarm.tasks[task]["CreatedAt"] = stamped(at(0))
        return service, tasks

    def print(self, task: str, *lines: Line) -> None:
        for nanoseconds, stream, text in lines:
            self.swarm.add_log(
                task, stamped(nanoseconds), text, "out" if stream == STDOUT else "err"
            )

    async def rule(self, selector: dict[str, Any] = SERVICE) -> int:
        _, job = await self.subject.rule(
            selector, {"object_storage_target_id": self.target_id}, environment_id=ENV_A
        )
        return job

    async def run(self, job: int, *, dry: bool = False) -> RunResult:
        return await self.subject.run(job, dry=dry)

    def reads(self) -> list[dict[str, str]]:
        """Return the query of every service log request."""
        return self._log_queries("/services/")

    def task_reads(self) -> list[dict[str, str]]:
        """Return the query of every task log request."""
        return self._log_queries("/tasks/")

    def _log_queries(self, segment: str) -> list[dict[str, str]]:
        return [
            dict(each.query)
            for each in self.swarm.requests
            if each.method == "GET" and each.path.endswith("/logs") and segment in each.path
        ]

    def messages(self, task: str) -> list[str]:
        """Return the messages stored for the task, in the order the objects were put."""
        return [
            line["message"]
            for key in self.bucket.stored
            if f"/{task}/" in key
            for line in self.bucket.lines(key)
        ]

    def cursors(self) -> dict[str, tuple[int, int, bool]]:
        found = self.subject.scheduling.sql(
            "SELECT task_id, last_timestamp_ns, lines_at_last, finished FROM service_log_cursor"
        )
        return {row[0]: (row[1], row[2], bool(row[3])) for row in found}

    def failures(self) -> list[dict[str, Any]]:
        return self.subject.events(ACTION)

    def vanish_on_read(self, task: str) -> None:
        """Make the task listed but unknown to its own log endpoint."""
        serving = self.swarm._tasks

        def tasks(parts: list[str], query: dict[str, str]) -> Reply:
            if parts[1:] == [task, "logs"]:
                return json_reply({"message": "no task"}, 404)
            return serving(parts, query)

        self.swarm._tasks = tasks  # type: ignore[method-assign]

    def copy_loose_lines(self, service: str, position_ns: int) -> None:
        """Record that lines naming no task were copied up to the position."""
        self.subject.scheduling.sql(
            "INSERT INTO service_log_cursor (rule_id, rule_created_at, environment_id, "
            "service_id, task_id, last_timestamp_ns, lines_at_last, object_path, finished, "
            "last_seen_at) SELECT rule_id, rule_created_at, environment_id, service_id, '', ?, "
            "0, 'earlier', 0, last_seen_at FROM service_log_cursor WHERE service_id = ? LIMIT 1",
            (position_ns, service),
        )


@pytest.fixture
def service_world(swarm: Any, settings: Settings) -> ServiceWorld:
    return ServiceWorld(swarm, settings)


async def test_each_task_of_a_service_exports_to_its_own_object_under_the_documented_key(
    service_world: ServiceWorld,
) -> None:
    service, [first, second] = service_world.add(replicas=2)
    service_world.print(first, (at(5, 7), STDERR, "one"))
    service_world.print(second, (at(6), STDOUT, "two"))
    job = await service_world.rule()

    result = await service_world.run(job)

    assert (result.outcome, counts(result)["exported"], counts(result)["tasks"]) == (
        "success",
        2,
        2,
    )
    stem = f"{SERVICE_FOLDER}/web-{service[:12]}"
    assert sorted(service_world.bucket.stored) == [
        f"{stem}/{first}/2023/11/14/{at(5, 7):020d}.ndjson.gz",
        f"{stem}/{second}/2023/11/14/{at(6):020d}.ndjson.gz",
    ]
    [record] = service_world.bucket.lines(service_world.bucket.stored[0])
    assert list(record.items()) == [
        ("schema_version", 2),
        ("time", stamped(at(5, 7))),
        ("stream", "stderr"),
        ("format", "text"),
        ("message", "one"),
        ("instance_name", service_world.subject.deps.settings.instance_name),
        ("environment_id", ENV_A),
        ("environment_name", "environment-a"),
        ("host_name", None),
        ("service_id", service),
        ("service_name", "web"),
        ("task_id", first),
        ("node_id", MANAGER),
        ("image", "busybox:latest"),
        ("stack_namespace", None),
    ]
    assert set(service_world.cursors()) == {first, second}


async def test_tasks_printing_lines_with_equal_timestamps_neither_repeat_nor_drop_across_runs(
    service_world: ServiceWorld,
) -> None:
    _, [first, second] = service_world.add(replicas=2)
    service_world.print(first, (at(5), STDOUT, "a1"))
    service_world.print(second, (at(5), STDOUT, "b1"))
    job = await service_world.rule()

    await service_world.run(job)
    service_world.print(first, (at(5), STDERR, "a2"), (at(5), STDOUT, "a3"))
    service_world.print(second, (at(5), STDERR, "b2"))
    await service_world.run(job)
    await service_world.run(job)

    assert service_world.messages(first) == ["a1", "a2", "a3"]
    assert service_world.messages(second) == ["b1", "b2"]
    assert len(set(service_world.bucket.stored)) == len(service_world.bucket.stored)


async def test_a_task_created_after_the_first_run_starts_at_its_creation(
    service_world: ServiceWorld,
) -> None:
    service, [first] = service_world.add()
    service_world.print(first, (at(2), STDOUT, "old"))
    job = await service_world.rule()
    await service_world.run(job)
    created = FIRST_SECOND + 10
    newcomer = service_world.swarm._add_task(service, 2, WORKER)
    service_world.swarm.tasks[newcomer]["CreatedAt"] = engine_time(created)
    service_world.print(
        newcomer, (at(8), STDOUT, "before it existed"), (at(12), STDOUT, "after it began")
    )

    await service_world.run(job)

    assert service_world.messages(newcomer) == ["after it began"]
    assert service_world.messages(first) == ["old"]
    assert since_ns(service_world.reads()[-1]["since"]) == at(2)


async def test_a_task_no_longer_listed_loses_its_cursor_and_its_lines_are_dropped_and_counted(
    service_world: ServiceWorld,
) -> None:
    _, [first, second] = service_world.add(replicas=2)
    service_world.print(first, (at(1), STDOUT, "a"))
    service_world.print(second, (at(1), STDOUT, "b"))
    job = await service_world.rule()
    await service_world.run(job)
    service_world.swarm.tasks[second]["ServiceID"] = NOT_LISTED_SERVICE
    service_world.print(second, (at(3), STDOUT, "late"))

    result = await service_world.run(job)

    assert set(service_world.cursors()) == {first}
    assert counts(result)["unlisted_task"] == UNLISTED_LINES
    assert service_world.messages(second) == ["b"]


async def test_a_finished_task_stops_holding_the_lower_bound_once_read_to_its_end(
    service_world: ServiceWorld,
) -> None:
    _, [old, current] = service_world.add(replicas=2)
    service_world.swarm.tasks[old]["Status"]["State"] = TERMINAL
    service_world.print(old, (at(1), STDOUT, "early"))
    service_world.print(current, (at(20), STDOUT, "steady"))
    job = await service_world.rule()
    await service_world.run(job)
    service_world.print(current, (at(30), STDOUT, "next"))

    await service_world.run(job)

    assert [since_ns(each["since"]) for each in service_world.reads()] == [at(0), at(20)]
    assert [since_ns(each["since"]) for each in service_world.task_reads()] == [at(0)]
    assert service_world.cursors()[old][2] is True
    assert service_world.cursors()[current][2] is False
    assert service_world.messages(old) == ["early"]
    assert service_world.messages(current) == ["steady", "next"]


async def test_an_old_terminal_task_does_not_hold_the_lower_bound_of_a_busy_services_capped_reads(
    service_world: ServiceWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter, "MAX_LINES_PER_OBJECT", 2)
    _, [old, current] = service_world.add(replicas=2)
    service_world.swarm.tasks[old]["Status"]["State"] = TERMINAL
    service_world.print(old, *[(at(n), STDOUT, f"o{n}") for n in range(1, 4)])
    service_world.print(current, *[(at(n), STDOUT, f"c{n}") for n in range(10, 16)])
    job = await service_world.rule()

    results = [await service_world.run(job) for _ in range(4)]

    assert [since_ns(each["since"]) for each in service_world.reads()] == [
        at(0),
        at(11),
        at(13),
        at(15),
    ]
    assert [since_ns(each["since"]) for each in service_world.task_reads()] == [at(0), at(2)]
    assert [counts(each)["more_pending"] for each in results] == [True, True, False, False]
    assert service_world.messages(old) == ["o1", "o2", "o3"]
    assert service_world.messages(current) == [f"c{n}" for n in range(10, 16)]
    assert service_world.cursors()[old][2] is True


async def test_lines_naming_no_task_stop_holding_the_lower_bound_once_older_than_the_live_tasks(
    service_world: ServiceWorld,
) -> None:
    service, [task] = service_world.add()
    service_world.print(task, (at(20), STDOUT, "steady"))
    job = await service_world.rule()
    await service_world.run(job)
    service_world.copy_loose_lines(service, at(1))
    service_world.print(task, (at(30), STDOUT, "next"))

    await service_world.run(job)

    assert since_ns(service_world.reads()[-1]["since"]) == at(20)
    assert service_world.messages(task) == ["steady", "next"]


async def test_a_terminal_task_removed_before_its_log_is_read_is_skipped_without_a_failure(
    service_world: ServiceWorld,
) -> None:
    _, [old, current] = service_world.add(replicas=2)
    service_world.swarm.tasks[old]["Status"]["State"] = TERMINAL
    service_world.print(current, (at(5), STDOUT, "steady"))
    service_world.vanish_on_read(old)
    job = await service_world.rule()

    result = await service_world.run(job)

    assert (result.outcome, counts(result)["gone"]) == ("success", 1)
    assert service_world.failures() == []
    assert service_world.cursors()[old][2] is False
    assert service_world.messages(current) == ["steady"]


async def test_a_service_too_large_to_authorise_fails_with_the_service_text(
    service_world: ServiceWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, [task] = service_world.add()
    service_world.print(task, (at(1), STDOUT, "x"))
    job = await service_world.rule()

    async def too_large(self: RunContext, key: Any) -> None:
        raise ContainerListTooLarge()

    monkeypatch.setattr(RunContext, "authorise_object", too_large)

    await service_world.run(job)

    [failure] = service_world.failures()
    assert failure["target_id"] == service
    assert detail_of(failure)["message"] == exporter.SERVICE_LOGS_UNAVAILABLE


async def test_a_put_that_fails_once_is_retried_after_a_pause_on_the_same_service_key(
    service_world: ServiceWorld,
) -> None:
    _, [task] = service_world.add()
    service_world.print(task, (at(1), STDOUT, "x"))
    service_world.bucket.script.append(put_times(1, error(SERVER_ERROR, "SlowDown")))
    job = await service_world.rule()

    result = await service_world.run(job)

    assert result.outcome == "success"
    assert service_world.sleeps == [RETRY_PAUSE]
    assert len(set(service_world.bucket.attempts)) == 1
    assert len(service_world.bucket.attempts) == 2
    assert service_world.messages(task) == ["x"]
    assert service_world.failures() == []


async def test_a_capped_service_run_reports_more_pending_and_the_next_run_continues(
    service_world: ServiceWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter, "MAX_LINES_PER_OBJECT", 2)
    _, [first, second] = service_world.add(replicas=2)
    service_world.print(first, *[(at(n), STDOUT, f"a{n}") for n in range(1, 4)])
    service_world.print(second, *[(at(n), STDOUT, f"b{n}") for n in range(4, 7)])
    job = await service_world.rule()

    results = [await service_world.run(job) for _ in range(3)]

    assert [counts(each)["more_pending"] for each in results] == [True, True, False]
    assert [counts(each)["lines"] for each in results] == [2, 2, 2]
    assert service_world.messages(first) == ["a1", "a2", "a3"]
    assert service_world.messages(second) == ["b4", "b5", "b6"]
    assert service_world.cursors()[first][2] is False


async def test_a_run_stores_at_most_the_object_cap_and_leaves_the_other_tasks_cursors_alone(
    service_world: ServiceWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter, "MAX_OBJECTS_PER_RUN", 1)
    _, [first, second] = service_world.add(replicas=2)
    service_world.print(first, (at(1), STDOUT, "a"))
    service_world.print(second, (at(1), STDOUT, "b"))
    job = await service_world.rule()

    capped = await service_world.run(job)
    next_run = await service_world.run(job)

    assert (counts(capped)["more_pending"], counts(capped)["deferred"]) == (True, 1)
    assert counts(next_run)["more_pending"] is False
    assert service_world.messages(first) == ["a"]
    assert service_world.messages(second) == ["b"]


async def test_a_storage_failure_writes_one_failure_row_for_the_service_and_task(
    service_world: ServiceWorld,
) -> None:
    service, [task] = service_world.add()
    service_world.print(task, (at(1), STDOUT, "x"))
    service_world.bucket.script.append(put_times(99, ACCESS_DENIED))
    job = await service_world.rule()

    result = await service_world.run(job)

    [failure] = service_world.failures()
    detail = detail_of(failure)
    assert (result.outcome, failure["target_type"], failure["target_id"]) == (
        "failure",
        "service",
        service,
    )
    assert (detail["service_name"], detail["task_id"]) == ("web", task)
    assert detail["object_path"] == service_world.bucket.attempts[0]
    assert (detail["storage_status"], detail["storage_code"]) == (403, "AccessDenied")
    assert service_world.cursors() == {}


async def test_a_service_dry_run_lists_the_tasks_it_would_read_and_reads_and_writes_nothing(
    service_world: ServiceWorld,
) -> None:
    service, [first, second] = service_world.add(replicas=2)
    service_world.print(first, (at(1), STDOUT, "x"))
    job = await service_world.rule()

    result = await service_world.run(job, dry=True)

    detail = counts(result)
    assert result.outcome == "success"
    assert (detail["without_cursor"], detail["with_cursor"], detail["tasks"]) == (2, 0, 2)
    assert detail["would_export_count"] == 2
    assert detail["next_object_path"] == f"{SERVICE_FOLDER}/web-{service[:12]}/{first}/"
    assert service_world.reads() == []
    assert service_world.bucket.attempts == []
    assert service_world.cursors() == {}
    assert service_world.failures() == []
    assert detail["would_export"] == f"web/{first[:12]}, web/{second[:12]}"
    assert detail["more_pending"] is False


async def test_a_service_dry_run_names_lines_without_a_task_and_reports_more_than_the_object_cap(
    service_world: ServiceWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter, "MAX_OBJECTS_PER_RUN", 2)
    service, [first, _second] = service_world.add(replicas=2)
    service_world.print(first, (at(1), STDOUT, "x"))
    job = await service_world.rule()
    await service_world.run(job)
    service_world.copy_loose_lines(service, at(1))

    result = await service_world.run(job, dry=True)

    detail = counts(result)
    assert detail["more_pending"] is True
    assert f"web/{exporter.NO_TASK_LABEL}" in str(detail["would_export"])


async def test_a_service_rule_on_an_environment_that_stopped_being_swarm_fails_and_is_recorded(
    service_world: ServiceWorld,
) -> None:
    _, [task] = service_world.add()
    service_world.print(task, (at(1), STDOUT, "x"))
    job = await service_world.rule()
    service_world.subject.scheduling.sql(
        "UPDATE environment SET type = 'standalone' WHERE id = ?", (ENV_A,)
    )

    result = await service_world.run(job)

    assert (result.outcome, result.reason) == ("failure", "target_missing")
    assert len(service_world.failures()) == 1
    assert service_world.reads() == []
    assert service_world.bucket.attempts == []


async def test_a_swarm_host_that_answers_service_calls_with_unavailable_skips_the_run(
    service_world: ServiceWorld,
) -> None:
    service_world.add()
    job = await service_world.rule()
    listing = service_world.swarm._services

    def refuse(
        method: str,
        parts: list[str],
        query: dict[str, str],
        body: bytes,
        headers: dict[str, str],
    ) -> Reply:
        if method == "GET" and len(parts) == 1:
            return json_reply({"message": "not a swarm manager"}, SERVER_ERROR)
        return listing(method, parts, query, body, headers)

    service_world.swarm._services = refuse  # type: ignore[method-assign]

    result = await service_world.run(job)

    assert (result.outcome, result.reason) == ("skipped", "environment_unreachable")
    assert service_world.failures() == []


async def test_a_service_rule_whose_owner_is_no_longer_an_administrator_fails_without_reading(
    service_world: ServiceWorld,
) -> None:
    _, [task] = service_world.add()
    service_world.print(task, (at(1), STDOUT, "x"))
    job = await service_world.rule()
    service_world.subject.scheduling.sql(
        "UPDATE user SET role = 'standard' WHERE id = ?", (ADMIN_ID,)
    )

    result = await service_world.run(job)

    assert (result.outcome, result.reason) == ("failure", "owner_access_lost")
    assert service_world.reads() == []
