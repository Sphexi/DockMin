"""Calls to a webhook on a fake clock: the per-webhook bucket and the spacing between runs."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta

import pytest

from app.auth.token_format import WEBHOOK_TOKEN_MARK, new_token
from app.auth.tokens import hash_token
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.db.repositories.setting import SettingRepository
from app.db.repositories.user import UserRepository
from app.db.repositories.webhook import WebhookRepository
from app.scheduler.loop import Scheduler
from app.scheduler.registry import HandlerRegistry
from app.services.activity import ActivityRecorder
from app.services.webhooks import (
    CALL_BURST,
    CALL_REFILL_SECONDS,
    WEBHOOK_RUN_SPACING,
    CallResult,
    Outcome,
    WebhookCallLimiter,
    WebhookService,
)
from app.subsystems.webhook_receiver import WebhookReceiver
from tests.scheduler.support import START, WEBHOOK, Rig, ScriptedHandler, stamp

ADDRESS = "192.0.2.10"
BURST = 8
REFILL = timedelta(seconds=CALL_REFILL_SECONDS)
JUST_BEFORE = timedelta(seconds=1)
OFFSET = timedelta(seconds=10)
WEBHOOK_RUN_CAP = 3


@dataclass(slots=True)
class Bench:
    rig: Rig
    service: WebhookService
    scheduler: Scheduler
    handler: ScriptedHandler
    job_id: int
    token: str

    async def call(self) -> CallResult:
        return await self.service.call(self.token, address=ADDRESS, over_tls=True)

    async def start_due(self) -> int:
        started = await self.scheduler.tick()
        await self.scheduler.idle()
        return started

    def run_starts(self) -> list[str]:
        return [row["started_at"] for row in self.rig.runs(self.job_id)]


@pytest.fixture
async def bench(rig: Rig) -> AsyncIterator[Bench]:
    handler = ScriptedHandler(WEBHOOK)
    registry = HandlerRegistry()
    registry.register(handler)
    scheduler = Scheduler(rig.database, registry, clock=rig.clock, sleeper=rig.sleeper)
    job_id = await rig.webhook_job("hook")
    token, prefix = new_token(WEBHOOK_TOKEN_MARK)
    job = await rig.job_row(job_id)
    assert job.rule_id is not None
    async with rig.database.session() as db:
        await WebhookRepository(db, rig.clock).create(job.rule_id, prefix, hash_token(token))
        limiter = WebhookCallLimiter(clock=lambda: (rig.clock() - START).total_seconds())
        service = WebhookService(
            db=db,
            webhooks=WebhookRepository(db, rig.clock),
            rules=AutomationRuleRepository(db, rig.clock),
            jobs=ScheduledJobRepository(db, rig.clock),
            users=UserRepository(db, rig.clock),
            settings_repo=SettingRepository(db, rig.clock),
            subsystems=registry,
            limiter=limiter,
            activity=ActivityRecorder(db, rig.clock),
            clock=rig.clock,
            poke=scheduler.poke,
            tls_enabled=False,
        )
        yield Bench(rig, service, scheduler, handler, job_id, token)
    await scheduler.aclose()


async def test_a_burst_of_eight_calls_admits_six_and_refuses_two_until_a_token_refills(
    bench: Bench,
) -> None:
    answers = [await bench.call() for _ in range(BURST)]
    bench.rig.clock.jump(CALL_REFILL_SECONDS / 2)
    too_soon = await bench.call()
    bench.rig.clock.jump(CALL_REFILL_SECONDS / 2)
    refilled = await bench.call()

    assert [answer.outcome for answer in answers[:CALL_BURST]] == [
        Outcome.ACCEPTED,
        *[Outcome.ALREADY_PENDING] * (CALL_BURST - 1),
    ]
    assert [answer.outcome for answer in answers[CALL_BURST:]] == [Outcome.RATE_LIMITED] * (
        BURST - CALL_BURST
    )
    assert answers[CALL_BURST].retry_after == int(CALL_REFILL_SECONDS)
    assert (too_soon.outcome, too_soon.retry_after) == (
        Outcome.RATE_LIMITED,
        int(CALL_REFILL_SECONDS / 2),
    )
    assert refilled.outcome is Outcome.ALREADY_PENDING


async def test_the_job_runs_once_then_again_no_sooner_than_a_minute_after_the_first_run_started(
    bench: Bench,
) -> None:
    first_call = await bench.call()
    first_started = await bench.start_due()
    bench.rig.clock.jump(OFFSET.total_seconds())
    second_call = await bench.call()
    early = await bench.start_due()
    bench.rig.clock.set(START + WEBHOOK_RUN_SPACING - JUST_BEFORE)
    just_before = await bench.start_due()
    bench.rig.clock.set(START + WEBHOOK_RUN_SPACING)
    on_time = await bench.start_due()

    assert (first_call.outcome, second_call.outcome) == (Outcome.ACCEPTED, Outcome.ACCEPTED)
    assert (first_started, early, just_before, on_time) == (1, 0, 0, 1)
    assert bench.run_starts() == [stamp(START), stamp(START + WEBHOOK_RUN_SPACING)]
    assert [row["scheduled_for"] for row in bench.rig.runs(bench.job_id)] == bench.run_starts()
    assert [row["trigger"] for row in bench.rig.runs(bench.job_id)] == ["webhook", "webhook"]


def test_the_webhook_receiver_declares_the_cap_on_runs_in_flight() -> None:
    assert WebhookReceiver.max_concurrent_runs == WEBHOOK_RUN_CAP
