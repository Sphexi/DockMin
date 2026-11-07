"""JSON routes for scheduled jobs and their runs, read-only and authenticated by bearer token."""

from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from app.api.job_models import (
    JobRunModel,
    JobRunPageModel,
    JobScheduleModel,
    run_model,
    schedule_model,
)
from app.auth.dependencies import require_token_user
from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE, MAX_ROW_ID, InvalidCursorError
from app.services.assembly import Jobs
from app.services.environments import DEFAULT_PAGE_SIZE
from app.services.jobs import JobView

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

MAX_CURSOR = 512
MAX_SUBSYSTEM = 64
MAX_KIND = 16

Token = Annotated[Principal, Depends(require_token_user)]
JobId = Annotated[int, Path(ge=1, le=MAX_ROW_ID)]
RunId = Annotated[int, Path(ge=1, le=MAX_ROW_ID)]


class JobRuleModel(BaseModel):
    """The automation rule a job belongs to."""

    id: int
    name: str | None


class JobModel(BaseModel):
    """A scheduled job with its state and latest run."""

    id: int
    subsystem: str
    kind: Literal["system", "automation"]
    rule: JobRuleModel | None
    environment_id: int | None
    schedule: JobScheduleModel
    next_run_at: str | None
    state: str
    running: bool
    enabled: bool
    rule_enabled: bool | None
    subsystem_enabled: bool
    available: bool
    supports_dry_run: bool
    overrun_policy: str
    catch_up: bool
    consecutive_failures: int
    failure_threshold: int | None
    paused_until: str | None
    last_run: JobRunModel | None
    last_run_at: str | None
    last_run_outcome: str | None


class JobPageModel(BaseModel):
    """One page of jobs; next_cursor is null on the last page."""

    items: list[JobModel]
    next_cursor: str | None


class JobRunDetailModel(JobRunModel):
    """A run with the job it belongs to."""

    job: JobModel


def _job(view: JobView) -> JobModel:
    summary = view.summary
    job = summary.job
    return JobModel(
        id=job.id,
        subsystem=job.subsystem,
        kind="system" if job.rule_id is None else "automation",
        rule=(
            None if job.rule_id is None else JobRuleModel(id=job.rule_id, name=summary.rule_name)
        ),
        environment_id=summary.environment_id,
        schedule=schedule_model(job, view.schedule),
        next_run_at=job.next_run_at,
        state=view.state,
        running=view.running,
        enabled=job.is_enabled,
        rule_enabled=view.rule_enabled,
        subsystem_enabled=view.subsystem_enabled,
        available=view.available,
        supports_dry_run=view.supports_dry_run,
        overrun_policy=job.overrun_policy,
        catch_up=job.catch_up,
        consecutive_failures=job.consecutive_failures,
        failure_threshold=job.failure_threshold,
        paused_until=view.paused_until,
        last_run=None if summary.last_run is None else run_model(summary.last_run),
        last_run_at=view.last_run_at,
        last_run_outcome=view.last_run_outcome,
    )


def _unprocessable(error: ValidationFailed) -> RequestValidationError:
    return RequestValidationError(
        [
            {"loc": ("query", name), "msg": message, "type": "value_error"}
            for name, message in error.fields.items()
        ]
    )


def _invalid_cursor() -> NoReturn:
    raise HTTPException(status_code=400, detail="invalid cursor")


@router.get("", response_model=JobPageModel)
async def list_jobs(
    principal: Token,
    services: Jobs,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR)] = None,
    subsystem: Annotated[str | None, Query(max_length=MAX_SUBSYSTEM)] = None,
    kind: Annotated[str | None, Query(max_length=MAX_KIND)] = None,
) -> JobPageModel:
    """List scheduled jobs by id; administrators only."""
    try:
        page = await services.jobs.page(
            principal, limit=limit, cursor=cursor, subsystem=subsystem, kind=kind
        )
    except ValidationFailed as error:
        raise _unprocessable(error) from None
    except InvalidCursorError:
        _invalid_cursor()
    return JobPageModel(items=[_job(view) for view in page.items], next_cursor=page.next_cursor)


@router.get("/-/runs/{run_id}", response_model=JobRunDetailModel)
async def get_job_run(run_id: RunId, principal: Token, services: Jobs) -> JobRunDetailModel:
    """Return one run with its job; administrators only."""
    view, run = await services.jobs.run(principal, run_id)
    return JobRunDetailModel(**run_model(run).model_dump(), job=_job(view))


@router.get("/{job_id}", response_model=JobModel)
async def get_job(job_id: JobId, principal: Token, services: Jobs) -> JobModel:
    """Return one job; administrators only."""
    return _job(await services.jobs.detail(principal, job_id))


@router.get("/{job_id}/runs", response_model=JobRunPageModel)
async def list_job_runs(
    job_id: JobId,
    principal: Token,
    services: Jobs,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR)] = None,
) -> JobRunPageModel:
    """List a job's runs, newest first; administrators only."""
    try:
        page = await services.jobs.runs(principal, job_id, limit=limit, cursor=cursor)
    except InvalidCursorError:
        _invalid_cursor()
    return JobRunPageModel(
        items=[run_model(run) for run in page.items], next_cursor=page.next_cursor
    )
