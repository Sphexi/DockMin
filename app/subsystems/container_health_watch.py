"""One Engine events consumer per environment that has a health rule, to run its rules sooner."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from app.auth.errors import ValidationFailed
from app.auth.services import AuthRuntime
from app.db.engine import Database
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.clocked import Clock, parse_timestamp
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.models import EngineEvent
from app.logging import get_logger
from app.scheduler.loop import Scheduler
from app.services.assembly import build_host_clients
from app.services.engine_setup import EngineRuntime
from app.services.object_inspector import EnvironmentUnavailable
from app.subsystems.container_health_checker import NAME, ContainerHealthChecker
from app.subsystems.selectors import ALL_KINDS, Selector, matches, parse_selector_text

logger = get_logger(__name__)

UNHEALTHY_ACTION: Final = "health_status: unhealthy"
EVENT_FILTERS: Final = {"type": ["container"], "event": ["health_status"]}
REPLAY_SECONDS: Final = 60
JOB_LIST_SECONDS: Final = 1.0
MIN_START_GAP: Final = timedelta(seconds=ContainerHealthChecker.min_interval_seconds)


@dataclass(frozen=True, slots=True)
class _OpenJob:
    id: int
    selector: Selector


class HealthEventWatchers:
    """Brings a health rule's next run forward when a container on its host turns unhealthy.

    The watchers write nothing and act as nobody: they only move a job's own next fire to now,
    through the scheduler, which keeps every gate. A job moves only for an event about a
    container its selector names, and not within its minimum interval of its last start. A missed
    event costs at most one interval.
    """

    def __init__(
        self,
        *,
        database: Database,
        auth: AuthRuntime,
        engine: EngineRuntime,
        scheduler: Scheduler,
        clock: Clock,
        reconcile_every: float = 60.0,
        max_watched: int = 64,
        debounce: float = 15.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._database = database
        self._auth = auth
        self._engine = engine
        self._scheduler = scheduler
        self._clock = clock
        self._reconcile_every = reconcile_every
        self._max_watched = max_watched
        self._debounce = debounce
        self._sleep = sleep
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._loop: asyncio.Task[None] | None = None
        self._last_expedited: dict[int, datetime] = {}
        self._listed: dict[int, tuple[datetime, list[_OpenJob]]] = {}
        self._gate = asyncio.Lock()
        self._closing = False
        self._capacity_logged = False

    async def start(self) -> None:
        """Watch the environments that need it now, then keep the set current."""
        if self._loop is not None or self._closing:
            return
        await self._reconcile_quietly()
        self._loop = asyncio.create_task(self._reconcile_forever(), name="health-watch-reconcile")

    async def reconcile(self) -> None:
        """Start a watcher for each environment with an open health job and stop the rest."""
        async with self._gate:
            if self._closing:
                return
            async with self._database.session() as db:
                found = await ScheduledJobRepository(db, self._clock).environments_with_open_jobs(
                    NAME, limit=self._max_watched + 1
                )
            wanted = found[: self._max_watched]
            self._note_capacity(over=len(found) > self._max_watched)
            for environment_id in [e for e in self._tasks if e not in wanted]:
                await self._stop(environment_id)
            for environment_id in wanted:
                task = self._tasks.get(environment_id)
                if task is None or task.done():
                    self._tasks[environment_id] = asyncio.create_task(
                        self._watch(environment_id), name=f"health-watch-{environment_id}"
                    )
            self._forget_stale()

    async def aclose(self) -> None:
        """Stop the reconcile loop and every watcher, and wait for them to end."""
        self._closing = True
        tasks = list(self._tasks.values())
        if self._loop is not None:
            tasks.append(self._loop)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._loop = None

    async def _reconcile_forever(self) -> None:
        while True:
            await self._sleep(self._reconcile_every)
            await self._reconcile_quietly()

    async def _reconcile_quietly(self) -> None:
        try:
            await self.reconcile()
        except Exception as error:
            logger.error("health_watch_reconcile_failed", extra={"error": type(error).__name__})

    async def _stop(self, environment_id: int) -> None:
        task = self._tasks.pop(environment_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _note_capacity(self, *, over: bool) -> None:
        if over and not self._capacity_logged:
            logger.warning("health_watch_capacity", extra={"max_watched": self._max_watched})
        self._capacity_logged = over

    def _forget_stale(self) -> None:
        now = self._clock()
        self._last_expedited = {
            job_id: at
            for job_id, at in self._last_expedited.items()
            if (now - at).total_seconds() < self._debounce
        }
        self._listed = {
            environment_id: held
            for environment_id, held in self._listed.items()
            if environment_id in self._tasks
        }

    async def _client(self, environment_id: int) -> EngineClient:
        async with self._database.session() as db:
            return await build_host_clients(db, self._auth, self._engine).client(environment_id)

    async def _watch(self, environment_id: int) -> None:
        logger.info("health_watch_started", extra={"environment_id": environment_id})
        try:
            client = await self._client(environment_id)
            events = client.system.events(filters=EVENT_FILTERS, replay_seconds=REPLAY_SECONDS)
            async with aclosing(events):
                async for event in events:
                    if event.type == "container" and event.action == UNHEALTHY_ACTION:
                        await self._expedite(environment_id, event)
        except EnvironmentUnavailable:
            logger.info("health_watch_unavailable", extra={"environment_id": environment_id})
        except Exception as error:
            logger.warning(
                "health_watch_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
        finally:
            logger.info("health_watch_stopped", extra={"environment_id": environment_id})

    async def _expedite(self, environment_id: int, event: EngineEvent) -> None:
        now = self._clock()
        attributes = event.actor.attributes
        name = attributes.get("name", "")
        try:
            for job in await self._open_jobs(environment_id, now):
                if not matches(job.selector, name=name, labels=attributes):
                    continue
                last = self._last_expedited.get(job.id)
                if last is not None and (now - last).total_seconds() < self._debounce:
                    continue
                if await self._started_recently(job.id, now):
                    continue
                self._last_expedited[job.id] = now
                await self._scheduler.expedite(job.id)
        except Exception as error:
            logger.warning(
                "health_watch_expedite_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )

    async def _started_recently(self, job_id: int, now: datetime) -> bool:
        async with self._database.session() as db:
            latest = await JobRunRepository(db, self._clock).latest(job_id)
        return latest is not None and now - parse_timestamp(latest.started_at) < MIN_START_GAP

    async def _open_jobs(self, environment_id: int, now: datetime) -> list[_OpenJob]:
        held = self._listed.get(environment_id)
        if held is not None and (now - held[0]).total_seconds() < JOB_LIST_SECONDS:
            return held[1]
        found: list[_OpenJob] = []
        async with self._database.session() as db:
            jobs = ScheduledJobRepository(db, self._clock)
            rules = AutomationRuleRepository(db, self._clock)
            for job_id in await jobs.open_job_ids(NAME, environment_id):
                job = await jobs.get(job_id)
                rule = None if job is None or job.rule_id is None else await rules.get(job.rule_id)
                if rule is None:
                    continue
                try:
                    found.append(
                        _OpenJob(job_id, parse_selector_text(rule.target_selector, ALL_KINDS))
                    )
                except ValidationFailed:
                    continue
        self._listed[environment_id] = (now, found)
        return found
