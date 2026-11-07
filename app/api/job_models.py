"""Response models shared by the job and automation rule JSON routes."""

from pydantic import BaseModel

from app.db.repositories.job_run import JobRun
from app.db.repositories.scheduled_job import ScheduledJob


class JobScheduleModel(BaseModel):
    """When a job fires; description is the schedule in words."""

    kind: str
    expression: str
    time_zone: str
    description: str


class JobRunModel(BaseModel):
    """One execution of a job. Message and detail are bounded and scrubbed when stored."""

    id: int
    job_id: int
    trigger: str
    requested_by_user_id: int | None
    scheduled_for: str | None
    started_at: str
    finished_at: str | None
    outcome: str | None
    reason: str | None
    message: str | None
    detail: dict[str, str | int | bool | None]


class JobRunPageModel(BaseModel):
    """One page of runs, newest first; next_cursor is null on the last page."""

    items: list[JobRunModel]
    next_cursor: str | None


def schedule_model(job: ScheduledJob, description: str) -> JobScheduleModel:
    return JobScheduleModel(
        kind=job.schedule_kind,
        expression=job.schedule_expression,
        time_zone=job.time_zone,
        description=description,
    )


def run_model(run: JobRun) -> JobRunModel:
    return JobRunModel(
        id=run.id,
        job_id=run.scheduled_job_id,
        trigger=run.trigger,
        requested_by_user_id=run.requested_by_user_id,
        scheduled_for=run.scheduled_for,
        started_at=run.started_at,
        finished_at=run.finished_at,
        outcome=run.outcome,
        reason=run.reason,
        message=run.message,
        detail=dict(run.detail),
    )
