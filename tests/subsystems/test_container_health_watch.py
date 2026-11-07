"""The health event watchers: which jobs an unhealthy event brings forward, and their lifecycle."""

import asyncio
import json
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.db.repositories.clocked import format_timestamp
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.scheduler.loop import Scheduler
from app.subsystems.container_health_watch import HealthEventWatchers
from tests.access_support import ENV_A, ENV_B
from tests.automation_support import Subject, cid
from tests.docker_engine.fake_engine import Ending, Reply, Stream

FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"
DEBOUNCE_SECONDS = 15
MIN_GAP_SECONDS = 60
EVENT_BASE = 1_900_000_000
UNHEALTHY = "health_status: unhealthy"
HEALTHY = "health_status: healthy"
STEP = timedelta(seconds=DEBOUNCE_SECONDS + 1)
Logged = Callable[[str], list[dict[str, Any]]]


def event(action: str = UNHEALTHY, **attributes: str) -> bytes:
    """Build one event line; each call gets a later time so the stream keeps it."""
    event.count += 1  # type: ignore[attr-defined]
    seconds = EVENT_BASE + event.count  # type: ignore[attr-defined]
    document = {
        "Type": "container",
        "Action": action,
        "Actor": {"ID": "x", "Attributes": attributes},
        "scope": "local",
        "time": seconds,
        "timeNano": seconds * 10**9,
    }
    return json.dumps(document).encode() + b"\n"


event.count = 0  # type: ignore[attr-defined]


async def until(predicate: Callable[[], object]) -> None:
    """Yield to the loop until the condition holds; a wait that cannot end hits the bound."""
    async with asyncio.timeout(5):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0)


class Spy:
    """Wraps a scheduler's expedite to record the jobs it was asked to bring forward."""

    def __init__(self, scheduler: Scheduler) -> None:
        self.calls: list[int] = []
        self.results: list[bool] = []
        self._real = scheduler.expedite
        scheduler.expedite = self.expedite  # type: ignore[method-assign]

    async def expedite(self, job_id: int) -> bool:
        self.calls.append(job_id)
        moved = await self._real(job_id)
        self.results.append(moved)
        return moved


class SteppingClock:
    """A clock that moves a fixed step on every reading, so each event meets a later moment."""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self._now = start
        self._step = step

    def __call__(self) -> datetime:
        self._now += self._step
        return self._now


class Watching:
    """The watchers over the health subject's database, a spied scheduler and a held stream."""

    def __init__(self, health: Subject) -> None:
        self.health = health
        self.scheduler = health.scheduling.scheduler(health.subsystem)
        self.spy = Spy(self.scheduler)
        self.watchers: list[HealthEventWatchers] = []

    def stream(self, *lines: bytes, ending: Ending = Ending.HOLD) -> None:
        self.health.host.route("GET", "/events", Stream(lines, ending))

    def build(self, **options: Any) -> HealthEventWatchers:
        options.setdefault("reconcile_every", 3600)
        options.setdefault("clock", self.health.clock)
        built = HealthEventWatchers(
            database=self.health.database,
            auth=self.health.rig.runtime,
            engine=self.health.rig.engine,
            scheduler=self.scheduler,
            **options,
        )
        self.watchers.append(built)
        return built


@pytest.fixture
async def watching(health: Subject) -> Any:
    built = Watching(health)
    yield built
    for watchers in built.watchers:
        await watchers.aclose()
    await built.scheduler.aclose()


async def test_the_unhealthy_transitions_of_a_captured_stream_bring_their_jobs_forward_once(
    watching: Watching,
) -> None:
    health = watching.health
    lines = (FIXTURES / "events_health_filter.ndjson").read_bytes().splitlines(keepends=True)
    watching.stream(*lines)
    ids = {}
    for name in ("dockmin-e2e-health", "dockmin-e2e-h-other", "dockmin-e2e-h-stopped"):
        ids[name] = (await health.rule({"kind": "container", "names": [name]}, name=name))[1]
    quiet = (await health.rule({"kind": "container", "names": ["dockmin-e2e-none"]}, name="quiet"))[
        1
    ]
    by_label = (await health.rule({"kind": "labels", "labels": [["app", "api"]]}, name="api"))[1]
    watchers = watching.build()

    await watchers.reconcile()
    await until(lambda: ids["dockmin-e2e-h-stopped"] in watching.spy.calls)

    assert Counter(watching.spy.calls) == Counter(
        [
            ids["dockmin-e2e-health"],
            ids["dockmin-e2e-h-other"],
            by_label,
            ids["dockmin-e2e-h-stopped"],
        ]
    )
    assert quiet not in watching.spy.calls


async def test_the_watcher_asks_for_health_events_only_and_starts_a_minute_back(
    watching: Watching,
) -> None:
    watching.stream(event())
    await watching.health.rule({"kind": "environment"})
    watchers = watching.build()

    await watchers.reconcile()
    await until(lambda: watching.health.host.calls("GET", "/events"))

    [request] = watching.health.host.calls("GET", "/events")
    assert json.loads(request.query["filters"]) == {
        "type": ["container"],
        "event": ["health_status"],
    }
    assert "since" in request.query


async def test_only_unhealthy_events_of_containers_a_selector_names_move_a_job(
    watching: Watching,
) -> None:
    health = watching.health
    jobs = {
        "named": (await health.rule({"kind": "container", "names": ["web"]}, name="named"))[1],
        "other": (await health.rule({"kind": "container", "names": ["other"]}, name="other"))[1],
        "labelled": (
            await health.rule({"kind": "labels", "labels": [["app", "x"]]}, name="labelled")
        )[1],
        "stacked": (await health.rule({"kind": "stack", "names": ["shop"]}, name="stacked"))[1],
        "all": (await health.rule({"kind": "environment"}, name="all"))[1],
        "end": (await health.rule({"kind": "container", "names": ["sentinel"]}, name="end"))[1],
    }
    watching.stream(
        event(HEALTHY, name="web"),
        event(name="web"),
        event(name="zzz", app="x"),
        event(name="db", **{"com.docker.compose.project": "Shop"}),
        event("exec_start: sh", name="sentinel"),
        event(name="sentinel"),
    )
    watchers = watching.build(debounce=0)

    await watchers.reconcile()
    await until(lambda: jobs["end"] in watching.spy.calls)

    assert Counter(watching.spy.calls) == Counter(
        [jobs[name] for name in ("named", "labelled", "stacked", "end")] + [jobs["all"]] * 4
    )


async def test_an_event_of_any_container_or_stack_a_selector_lists_moves_its_job(
    watching: Watching,
) -> None:
    health = watching.health
    containers = (await health.rule({"kind": "container", "names": ["db", "web"]}, name="c"))[1]
    stacks = (await health.rule({"kind": "stack", "names": ["blog", "shop"]}, name="s"))[1]
    other = (await health.rule({"kind": "container", "names": ["cache"]}, name="o"))[1]
    end = (await health.rule({"kind": "container", "names": ["sentinel"]}, name="end"))[1]
    watching.stream(
        event(name="db"),
        event(name="web"),
        event(name="x", **{"com.docker.compose.project": "shop"}),
        event(name="y", **{"com.docker.compose.project": "Blog"}),
        event(name="z", **{"com.docker.compose.project": "wiki"}),
        event(name="sentinel"),
    )
    watchers = watching.build(debounce=0)

    await watchers.reconcile()
    await until(lambda: end in watching.spy.calls)

    assert Counter(watching.spy.calls) == Counter([containers] * 2 + [stacks] * 2 + [end])
    assert other not in watching.spy.calls


async def test_a_second_event_for_a_job_within_the_debounce_window_is_ignored(
    watching: Watching,
) -> None:
    health = watching.health
    web = (await health.rule({"kind": "container", "names": ["web"]}, name="web"))[1]
    end = (await health.rule({"kind": "container", "names": ["end"]}, name="end"))[1]
    watching.stream(event(name="web"), event(name="web"), event(name="end"))
    watchers = watching.build()

    await watchers.reconcile()
    await until(lambda: end in watching.spy.calls)

    assert watching.spy.calls == [web, end]


async def test_events_a_debounce_window_apart_each_bring_the_job_forward(
    watching: Watching,
) -> None:
    health = watching.health
    web = (await health.rule({"kind": "container", "names": ["web"]}))[1]
    watching.stream(event(name="web"), event(name="web"))
    watchers = watching.build(clock=SteppingClock(health.clock(), STEP))

    await watchers.reconcile()
    await until(lambda: len(watching.spy.calls) == 2)

    assert watching.spy.calls == [web, web]


async def test_a_job_that_started_within_its_minimum_interval_is_not_brought_forward(
    watching: Watching,
) -> None:
    health = watching.health
    recent = (await health.rule({"kind": "container", "names": ["recent"]}, name="recent"))[1]
    settled = (await health.rule({"kind": "container", "names": ["settled"]}, name="settled"))[1]
    end = (await health.rule({"kind": "container", "names": ["end"]}, name="end"))[1]
    async with health.database.session() as db:
        runs = JobRunRepository(db, health.clock)
        await runs.start(settled, trigger="schedule", scheduled_for=None)
    health.advance(MIN_GAP_SECONDS + 1)
    async with health.database.session() as db:
        await JobRunRepository(db, health.clock).start(
            recent, trigger="schedule", scheduled_for=None
        )
    watching.stream(event(name="recent"), event(name="settled"), event(name="end"))
    watchers = watching.build()

    await watchers.reconcile()
    await until(lambda: end in watching.spy.calls)

    assert watching.spy.calls == [settled, end]


async def test_an_unhealthy_event_makes_the_real_scheduler_run_the_rule_and_restart_the_container(
    watching: Watching,
) -> None:
    health = watching.health
    health.host.add(cid(1), "web", health="unhealthy")
    _, job = await health.rule({"kind": "container", "names": ["web"]})
    await watching.scheduler.tick()
    await watching.scheduler.idle()
    watching.stream(event(name="web"))
    watchers = watching.build()

    await watchers.reconcile()
    await until(lambda: watching.spy.results)
    due = (await health.scheduling.job_row(job)).next_run_at
    await watching.scheduler.tick()
    await watching.scheduler.idle()

    assert due == format_timestamp(health.clock())
    assert health.scheduling.outcomes(job) == [("success", None)]
    assert health.restarted() == [cid(1)]


async def test_without_an_event_the_rule_waits_for_its_interval(watching: Watching) -> None:
    health = watching.health
    health.host.add(cid(1), "web", health="unhealthy")
    _, job = await health.rule({"kind": "container", "names": ["web"]})

    await watching.scheduler.tick()
    await watching.scheduler.idle()
    early = health.scheduling.runs(job)
    health.advance(24 * 3600 + 1)
    await watching.scheduler.tick()
    await watching.scheduler.idle()

    assert early == []
    assert health.scheduling.outcomes(job) == [("success", None)]


async def test_a_watcher_exists_only_for_an_environment_with_an_open_health_job(
    watching: Watching, logged: Logged
) -> None:
    health = watching.health
    watching.stream()
    await health.rule({"kind": "environment"}, name="open")
    _, gated = await health.rule({"kind": "environment"}, name="gated", environment_id=ENV_B)
    async with health.database.session() as db:
        await ScheduledJobRepository(db, health.clock).set_next_run(gated, None)
    watchers = watching.build()

    await watchers.reconcile()
    await until(lambda: logged("health_watch_started"))

    assert [r["environment_id"] for r in logged("health_watch_started")] == [ENV_A]


async def test_a_watcher_stops_when_its_last_open_job_closes_and_returns_with_a_new_one(
    watching: Watching, logged: Logged
) -> None:
    health = watching.health
    watching.stream()
    _, job = await health.rule({"kind": "environment"})
    watchers = watching.build()
    await watchers.reconcile()
    await until(lambda: len(logged("health_watch_started")) == 1)
    async with health.database.session() as db:
        await ScheduledJobRepository(db, health.clock).set_next_run(job, None)

    await watchers.reconcile()
    await until(lambda: len(logged("health_watch_stopped")) == 1)
    async with health.database.session() as db:
        await ScheduledJobRepository(db, health.clock).set_next_run(
            job, format_timestamp(health.clock() + timedelta(hours=1))
        )
    await watchers.reconcile()
    await until(lambda: len(logged("health_watch_started")) == 2)


async def test_environments_beyond_the_cap_are_polled_only_and_the_cap_is_logged_once(
    watching: Watching, logged: Logged
) -> None:
    health = watching.health
    watching.stream()
    await health.rule({"kind": "environment"}, name="a")
    await health.rule({"kind": "environment"}, name="b", environment_id=ENV_B)
    watchers = watching.build(max_watched=1)

    await watchers.reconcile()
    await watchers.reconcile()
    await until(lambda: logged("health_watch_started"))

    assert [r["environment_id"] for r in logged("health_watch_started")] == [ENV_A]
    assert len(logged("health_watch_capacity")) == 1


async def test_a_watcher_whose_stream_fails_is_started_again_by_the_next_reconcile(
    watching: Watching, logged: Logged
) -> None:
    health = watching.health
    health.host.route("GET", "/events", Reply(500, b"{}"))
    await health.rule({"kind": "environment"})
    watchers = watching.build()

    await watchers.reconcile()
    await until(lambda: logged("health_watch_failed"))
    await until(lambda: logged("health_watch_stopped"))
    await watchers.reconcile()
    await until(lambda: len(logged("health_watch_started")) == 2)


async def test_start_reconciles_at_once_and_again_on_each_tick_and_never_twice(
    watching: Watching, logged: Logged
) -> None:
    health = watching.health
    watching.stream()
    ticks: asyncio.Queue[None] = asyncio.Queue()

    async def next_tick(_seconds: float) -> None:
        await ticks.get()

    watchers = watching.build(sleep=next_tick)
    await health.rule({"kind": "environment"}, name="a")
    await watchers.start()
    await watchers.start()
    await until(lambda: len(logged("health_watch_started")) == 1)
    await health.rule({"kind": "environment"}, name="b", environment_id=ENV_B)
    ticks.put_nowait(None)
    await until(lambda: len(logged("health_watch_started")) == 2)

    assert sorted(r["environment_id"] for r in logged("health_watch_started")) == [ENV_A, ENV_B]


async def test_a_failing_reconcile_is_logged_and_does_not_stop_start(
    watching: Watching, logged: Logged, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: Any, **_kwargs: Any) -> list[int]:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(ScheduledJobRepository, "environments_with_open_jobs", broken)
    watchers = watching.build()

    await watchers.start()

    [record] = logged("health_watch_reconcile_failed")
    assert record["error"] == "RuntimeError"
    assert "database is gone" not in str(record)


async def test_closing_ends_every_watcher_and_the_reconcile_loop_and_blocks_a_restart(
    watching: Watching, logged: Logged
) -> None:
    health = watching.health
    watching.stream()
    await health.rule({"kind": "environment"}, name="a")
    await health.rule({"kind": "environment"}, name="b", environment_id=ENV_B)
    watchers = watching.build()
    await watchers.start()
    await until(lambda: len(logged("health_watch_started")) == 2)

    await watchers.aclose()
    await watchers.start()
    await watchers.reconcile()

    leftover = [t for t in asyncio.all_tasks() if (t.get_name() or "").startswith("health-watch")]
    assert leftover == []
    assert len(logged("health_watch_stopped")) == 2
    assert len(logged("health_watch_started")) == 2
