"""A run started from a confirmation is bound to the job and rule that were reviewed."""

import json
from dataclasses import replace

import pytest
from fastapi import FastAPI

from app.auth.errors import ValidationFailed
from app.services.jobs import JOB_CHANGED_BEFORE_RUN, job_digest
from tests.job_support import ADMIN, events, execute, job_services, last_event_id, rows
from tests.services.test_jobs_service import a_job, a_rule_job, app  # noqa: F401
from tests.web.environment_support import World, seed_world

STALE_DIGEST = "0" * 64


@pytest.fixture
async def seeded(started_app: FastAPI) -> World:
    return await seed_world(started_app)


async def test_a_run_bound_to_the_digest_it_was_shown_starts(
    started_app: FastAPI, seeded: World
) -> None:
    _, job = await a_rule_job(started_app, seeded)
    async with job_services(started_app) as services:
        _, digest = await services.jobs.run_confirmation(ADMIN, job)
        run_id = await services.jobs.run_now(ADMIN, job, expected_digest=digest)
        await started_app.state.scheduler.scheduler.idle()

    [run] = await rows(started_app, "SELECT trigger, outcome FROM job_run WHERE id = ?", (run_id,))
    assert run == {"trigger": "manual", "outcome": "success"}


async def test_a_run_is_refused_and_recorded_when_the_rule_changed_after_the_review(
    started_app: FastAPI, seeded: World
) -> None:
    rule, job = await a_rule_job(started_app, seeded)
    async with job_services(started_app) as services:
        _, digest = await services.jobs.run_confirmation(ADMIN, job)
        await execute(
            started_app,
            "UPDATE automation_rule SET updated_at = '2031-01-01T00:00:00Z' WHERE id = ?",
            (rule,),
        )
        mark = await last_event_id(started_app)
        runs_before = await rows(started_app, "SELECT id FROM job_run")

        with pytest.raises(ValidationFailed) as refused:
            await services.jobs.run_now(ADMIN, job, expected_digest=digest)

    [request] = await events(started_app, since=mark)
    assert refused.value.fields == {"job": JOB_CHANGED_BEFORE_RUN}
    assert await rows(started_app, "SELECT id FROM job_run") == runs_before
    assert (request["action"], request["result"]) == ("job_run_requested", "failure")
    assert json.loads(request["detail"])["reason"] == "changed_since_review"


@pytest.mark.parametrize("digest", ["", STALE_DIGEST], ids=["empty", "stale"])
async def test_a_run_given_a_digest_that_is_not_the_jobs_is_refused_and_starts_nothing(
    started_app: FastAPI, seeded: World, digest: str
) -> None:
    _, job = await a_rule_job(started_app, seeded)
    runs_before = await rows(started_app, "SELECT id FROM job_run")
    async with job_services(started_app) as services:
        with pytest.raises(ValidationFailed):
            await services.jobs.run_now(ADMIN, job, expected_digest=digest)

    assert await rows(started_app, "SELECT id FROM job_run") == runs_before


async def test_a_dry_run_changes_nothing_so_it_needs_no_binding(
    started_app: FastAPI, seeded: World
) -> None:
    _, job = await a_rule_job(started_app, seeded)
    async with job_services(started_app) as services:
        run_id = await services.jobs.run_now(ADMIN, job, dry_run=True, expected_digest=STALE_DIGEST)
        await started_app.state.scheduler.scheduler.idle()

    [run] = await rows(started_app, "SELECT trigger FROM job_run WHERE id = ?", (run_id,))
    assert run == {"trigger": "dry_run"}


def test_the_digest_covers_the_schedule_and_environment_not_per_run_counters() -> None:
    job = a_job()
    base = job_digest(job, None)

    moved = replace(job, next_run_at="2030-06-01T00:00:00Z", consecutive_failures=3)

    assert job_digest(moved, None) == base
    assert job_digest(replace(job, schedule_expression="120"), None) != base
    assert job_digest(replace(job, environment_id=3), None) != base
