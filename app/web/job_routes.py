"""Jobs pages: the job list, job and run detail, and the pause, resume, run and enable actions."""

import re
from dataclasses import dataclass
from typing import Annotated, Any, Final, cast, get_args

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentAdministrator, require_administrator
from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.clocked import parse_timestamp
from app.db.repositories.job_run import JobRun
from app.db.repositories.pagination import MAX_PAGE_SIZE, MAX_ROW_ID, InvalidCursorError
from app.scheduler.loop import SchedulerBusy
from app.services.activity import SUBSYSTEMS
from app.services.assembly import Jobs, JobServices
from app.services.jobs import JOB_KINDS, PAUSE_OPTIONS, JobNotRunnable, RefusalReason
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.web.flash import Flash
from app.web.navigation import no_store
from app.web.object_pages import Failure, problem_page, redirect, route
from app.web.pagination import DEFAULT_LIMIT, Column, PageView
from app.web.templating import is_htmx_request, render_fragment, render_page

router = APIRouter(prefix="/jobs", dependencies=[Depends(require_administrator)])

JOBS_PATH: Final = "/jobs"
TABLES: Final = "jobs/_table.html"
RUNS: Final = "jobs/_runs.html"
RUN_REGION: Final = "jobs/_run_region.html"
SUBSYSTEM_NAME: Final = re.compile(r"[a-z][a-z0-9-]{0,63}")
MAX_ID_DIGITS: Final = len(str(MAX_ROW_ID))
NOTICE_MAX: Final = 32
FILTER_MAX: Final = 64
DIGEST_MAX: Final = 128
DURATION_MAX: Final = 16
LISTED_JOBS: Final = 25
REFUSAL_STATES: Final = frozenset(get_args(RefusalReason.__value__))
DRY_RUN_STATES: Final = frozenset({"rule_disabled", "auto_disabled"})
POLICY_LABELS: Final = {
    "skip": "Skip the new run",
    "queue": "Queue one new run",
    "cancel_previous": "Cancel the running one",
}

NOTICES: Final = {
    "paused": "The job is paused.",
    "resumed": "The job is resumed.",
    "enabled": "The job is enabled.",
    "started": "The run has started.",
    "dry_run": "The dry run has started.",
    "subsystem_enabled": "The subsystem is enabled.",
    "subsystem_disabled": "The subsystem is disabled.",
}

KIND_LABELS: Final = {"system": "System task", "automation": "Automation"}


@dataclass(frozen=True, slots=True)
class SystemTaskNote:
    """Where a built-in task's behaviour is set; `route_name` links the page that sets it."""

    text: str
    route_name: str | None = None
    link_label: str = ""


SYSTEM_TASK_NOTES: Final = {
    "notification-sender": SystemTaskNote(
        "Built in. It delivers queued notifications to the channels, which are set on",
        "notification_channels",
        "the Notifications page",
    ),
    "environment-snapshot-refresher": SystemTaskNote(
        "Built in. It refreshes each environment's status and object counts. Its interval is "
        "the DOCKMIN_SNAPSHOT_INTERVAL_SECONDS setting, which takes effect at the next start."
    ),
}

JOB_COLUMNS: Final = (
    Column("job", "Name"),
    Column("kind", "Kind", optional=True, width="short"),
    Column("environment", "Environment", optional=True),
    Column("schedule", "Schedule", optional=True),
    Column("state", "State", width="badge"),
    Column("next_run", "Next run", optional=True, width="time"),
    Column("last_run", "Last run", width="time"),
)
RUN_COLUMNS: Final = (
    Column("started_at", "Started", width="time"),
    Column("trigger", "Trigger", optional=True, width="short"),
    Column("outcome", "Outcome", width="badge"),
    Column("result", "Result"),
    Column("duration", "Duration", numeric=True, optional=True, width="count"),
)

Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]


@dataclass(frozen=True, slots=True)
class RunRow:
    """A run with the seconds it took, for the history table."""

    run: JobRun
    elapsed: int | None


def run_row(run: JobRun) -> RunRow:
    if run.finished_at is None:
        return RunRow(run, None)
    seconds = (parse_timestamp(run.finished_at) - parse_timestamp(run.started_at)).total_seconds()
    return RunRow(run, max(int(seconds), 0))


def path_identifier(raw: str) -> int:
    """Read a path id; anything that is not a plain number is a missing object."""
    if not (raw.isascii() and raw.isdigit() and len(raw) <= MAX_ID_DIGITS):
        raise HTTPException(status_code=404)
    return int(raw)


def job_id_of(job_id: str) -> int:
    """Read the job id of the path."""
    return path_identifier(job_id)


def run_id_of(run_id: str) -> int:
    """Read the run id of the path."""
    return path_identifier(run_id)


def subsystem_name_of(subsystem: str) -> str:
    """Read the subsystem name of the path; a name that cannot be one is a missing object."""
    if SUBSYSTEM_NAME.fullmatch(subsystem) is None:
        raise HTTPException(status_code=404)
    return subsystem


JobId = Annotated[int, Depends(job_id_of)]
RunId = Annotated[int, Depends(run_id_of)]
SubsystemPath = Annotated[str, Depends(subsystem_name_of)]


def html_page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    """Render a full page that no cache may keep."""
    return no_store(render_page(request, template, context, status_code=status_code))


def html_fragment(request: Request, template: str, macro: str, **arguments: Any) -> Response:
    """Render one macro of a template as a fragment that no cache may keep."""
    return no_store(render_fragment(request, template, macro, **arguments))


def bad_cursor() -> HTTPException:
    """Return the answer to a page cursor that does not decode."""
    return HTTPException(status_code=400, detail="invalid cursor")


def _job_href(request: Request, job_id: int) -> str:
    return route(request, "job_detail", job_id=job_id)


def _noted(request: Request, location: str, notice: str) -> Response:
    return redirect(request, location, [Flash("ok", NOTICES[notice])])


def blocked_reason(state: str) -> str | None:
    """Say why a run cannot start in this state, in the service's own words."""
    if state not in REFUSAL_STATES:
        return None
    return str(JobNotRunnable(cast(RefusalReason, state)))


def dry_run_offered(state: str, *, supported: bool) -> bool:
    """Report whether a dry run is offered in this state."""
    return supported and (state not in REFUSAL_STATES or state in DRY_RUN_STATES)


def _refused(
    request: Request,
    job_id: int,
    refusal: SchedulerBusy | ValidationFailed,
    *,
    status: int = 409,
) -> Response:
    message = (
        " ".join(refusal.fields.values()) if isinstance(refusal, ValidationFailed) else str(refusal)
    )
    return problem_page(
        request,
        heading="Run job",
        failure=Failure(status, "The job was not started", message),
        back_href=_job_href(request, job_id),
        back_label="Back to the job",
        region=None,
    )


@router.get("", response_class=HTMLResponse, name="jobs")
async def list_jobs(
    request: Request,
    services: Jobs,
    actor: CurrentAdministrator,
    subsystem: Annotated[str, Query(max_length=FILTER_MAX)] = "",
    kind: Annotated[str, Query(max_length=FILTER_MAX)] = "",
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Response:
    """List scheduled jobs, optionally for one subsystem or one kind."""
    names = [name for name, _enabled, _available in await services.jobs.subsystems(actor)]
    chosen = subsystem.strip() or None
    status_code = 200
    problem: str | None = None
    if chosen is not None and chosen not in SUBSYSTEMS:
        status_code = 422
        problem = "Choose a known subsystem."
        chosen = None
    if chosen is not None and chosen not in names:
        names = sorted([*names, chosen])
    chosen_kind = kind.strip() or None
    if chosen_kind is not None and chosen_kind not in JOB_KINDS:
        status_code = 422
        problem = "Choose a known kind."
        chosen_kind = None
    try:
        page = await services.jobs.page(
            actor, limit=limit, cursor=cursor, subsystem=chosen, kind=chosen_kind
        )
    except (InvalidCursorError, TypeError):
        raise bad_cursor() from None
    pager = PageView.of(
        page,
        base_path=JOBS_PATH,
        limit=limit,
        cursor=cursor,
        params=[
            (name, value)
            for name, value in (("subsystem", chosen), ("kind", chosen_kind))
            if value is not None
        ],
    )
    filtered = chosen is not None or chosen_kind is not None
    if is_htmx_request(request) and status_code == 200:
        return html_fragment(
            request,
            TABLES,
            "jobs_table",
            rows=page.items,
            columns=JOB_COLUMNS,
            pager=pager,
            filtered=filtered,
        )
    return html_page(
        request,
        "jobs/list.html",
        {
            "rows": page.items,
            "columns": JOB_COLUMNS,
            "pager": pager,
            "subsystem": chosen or "",
            "subsystems": names,
            "kind": chosen_kind or "",
            "kinds": [(value, KIND_LABELS[value]) for value in JOB_KINDS],
            "problem": problem,
            "filtered": filtered,
        },
        status_code,
    )


@router.get("/-/subsystems", response_class=HTMLResponse, name="job_subsystems")
async def subsystem_page(request: Request, services: Jobs, actor: CurrentAdministrator) -> Response:
    """Show each subsystem with its switch."""
    return html_page(
        request,
        "jobs/subsystems.html",
        {"subsystems": await services.jobs.subsystems(actor)},
    )


async def _subsystem_confirm(
    request: Request, services: JobServices, actor: Principal, name: str, *, enable: bool
) -> Response:
    entries = await services.jobs.subsystems(actor)
    entry = next((found for found in entries if found[0] == name), None)
    if entry is None:
        raise HTTPException(status_code=404)
    page = await services.jobs.page(actor, limit=LISTED_JOBS, subsystem=name)
    job_count = await services.jobs.subsystem_job_count(actor, name)
    return html_page(
        request,
        "jobs/subsystem_confirm.html",
        {
            "name": name,
            "enable": enable,
            "enabled": entry[1],
            "available": entry[2],
            "jobs": page.items,
            "job_count": job_count,
        },
    )


@router.get(
    "/-/subsystems/{subsystem}/enable",
    response_class=HTMLResponse,
    name="job_subsystem_enable_confirm",
)
async def confirm_subsystem_enable(
    request: Request, services: Jobs, actor: CurrentAdministrator, subsystem: SubsystemPath
) -> Response:
    """Name the jobs a switch-on restores; nothing changes until it is confirmed."""
    return await _subsystem_confirm(request, services, actor, subsystem, enable=True)


@router.post("/-/subsystems/{subsystem}/enable", name="job_subsystem_enable")
async def enable_subsystem(
    request: Request, services: Jobs, actor: CurrentAdministrator, subsystem: SubsystemPath
) -> Response:
    """Switch a subsystem on."""
    await services.jobs.set_subsystem_enabled(actor, subsystem, enabled=True)
    return _noted(request, route(request, "job_subsystems"), "subsystem_enabled")


@router.get(
    "/-/subsystems/{subsystem}/disable",
    response_class=HTMLResponse,
    name="job_subsystem_disable_confirm",
)
async def confirm_subsystem_disable(
    request: Request, services: Jobs, actor: CurrentAdministrator, subsystem: SubsystemPath
) -> Response:
    """Name the jobs a switch-off stops; nothing changes until it is confirmed."""
    return await _subsystem_confirm(request, services, actor, subsystem, enable=False)


@router.post("/-/subsystems/{subsystem}/disable", name="job_subsystem_disable")
async def disable_subsystem(
    request: Request, services: Jobs, actor: CurrentAdministrator, subsystem: SubsystemPath
) -> Response:
    """Switch a subsystem off; its jobs stop firing and nothing is deleted."""
    await services.jobs.set_subsystem_enabled(actor, subsystem, enabled=False)
    return _noted(request, route(request, "job_subsystems"), "subsystem_disabled")


@router.get("/-/runs/{run_id}", response_class=HTMLResponse, name="job_run_detail")
async def run_detail(
    request: Request,
    services: Jobs,
    actor: CurrentAdministrator,
    run_id: RunId,
) -> Response:
    """Show one run: when it ran, how it ended, why, and the detail it recorded."""
    view, run = await services.jobs.run(actor, run_id)
    context: dict[str, Any] = {
        "view": view,
        "row": run_row(run),
        "detail": sorted(run.detail.items()),
        "finished": run.outcome is not None,
        "region_href": route(request, "job_run_detail", run_id=run_id),
    }
    if is_htmx_request(request):
        return html_fragment(request, RUN_REGION, "run_region", **context)
    return html_page(request, "jobs/run.html", context)


async def _detail(
    request: Request,
    services: JobServices,
    actor: Principal,
    job_id: int,
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    status_code: int = 200,
    errors: dict[str, str] | None = None,
) -> Response:
    view = await services.jobs.detail(actor, job_id)
    try:
        page = await services.jobs.runs(actor, job_id, limit=limit, cursor=cursor)
    except (InvalidCursorError, TypeError):
        raise bad_cursor() from None
    pager = PageView.of(
        page, base_path=route(request, "job_detail", job_id=job_id), limit=limit, cursor=cursor
    )
    rows = [run_row(run) for run in page.items]
    if is_htmx_request(request) and status_code == 200:
        return html_fragment(
            request, RUNS, "runs_table", rows=rows, columns=RUN_COLUMNS, pager=pager
        )
    return html_page(
        request,
        "jobs/detail.html",
        {
            "view": view,
            "rows": rows,
            "columns": RUN_COLUMNS,
            "pager": pager,
            "blocked": blocked_reason(view.state),
            "dry_run_offered": dry_run_offered(view.state, supported=view.supports_dry_run),
            "durations": PAUSE_OPTIONS,
            "policy_label": POLICY_LABELS.get(view.summary.job.overrun_policy),
            "system_note": (
                SYSTEM_TASK_NOTES.get(view.summary.job.subsystem)
                if view.summary.job.rule_id is None
                else None
            ),
            "errors": errors or {},
        },
        status_code,
    )


@router.get("/{job_id}", response_class=HTMLResponse, name="job_detail")
async def job_detail(
    request: Request,
    services: Jobs,
    actor: CurrentAdministrator,
    job_id: JobId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Response:
    """Show a job with its actions and its run history, newest first."""
    return await _detail(request, services, actor, job_id, limit=limit, cursor=cursor)


@router.post("/{job_id}/pause", name="job_pause")
async def pause_job(
    request: Request,
    services: Jobs,
    actor: CurrentAdministrator,
    job_id: JobId,
    duration: Annotated[str, Form(max_length=DURATION_MAX)] = "",
) -> Response:
    """Pause a job, or the rule that owns it, for a fixed time."""
    view = await services.jobs.detail(actor, job_id)
    rule_id = view.summary.job.rule_id
    try:
        if rule_id is None:
            await services.jobs.pause(actor, job_id, duration=duration)
        else:
            await services.rules.pause(actor, rule_id, duration=duration)
    except ValidationFailed as invalid:
        return await _detail(
            request, services, actor, job_id, status_code=422, errors=invalid.fields
        )
    return _noted(request, _job_href(request, job_id), "paused")


@router.post("/{job_id}/resume", name="job_resume")
async def resume_job(
    request: Request, services: Jobs, actor: CurrentAdministrator, job_id: JobId
) -> Response:
    """Clear the pause on a job, or on the rule that owns it."""
    view = await services.jobs.detail(actor, job_id)
    rule_id = view.summary.job.rule_id
    try:
        if rule_id is None:
            await services.jobs.resume(actor, job_id)
        else:
            await services.rules.resume(actor, rule_id)
    except ValidationFailed as invalid:
        return await _detail(
            request, services, actor, job_id, status_code=422, errors=invalid.fields
        )
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return problem_page(
            request,
            heading="Resume job",
            failure=Failure(503, "The job was not resumed", str(unavailable)),
            back_href=_job_href(request, job_id),
            back_label="Back to the job",
            region=None,
        )
    return _noted(request, _job_href(request, job_id), "resumed")


@router.get("/{job_id}/enable", response_class=HTMLResponse, name="job_enable_confirm")
async def confirm_enable(
    request: Request, services: Jobs, actor: CurrentAdministrator, job_id: JobId
) -> Response:
    """Name the job that will run again; nothing changes until it is confirmed."""
    view = await services.jobs.detail(actor, job_id)
    return html_page(request, "jobs/enable_confirm.html", {"view": view})


@router.post("/{job_id}/enable", name="job_enable")
async def enable_job(
    request: Request, services: Jobs, actor: CurrentAdministrator, job_id: JobId
) -> Response:
    """Enable a job that was disabled after repeated failures."""
    await services.jobs.enable(actor, job_id)
    return _noted(request, _job_href(request, job_id), "enabled")


@router.get("/{job_id}/run", response_class=HTMLResponse, name="job_run_confirm")
async def confirm_run(
    request: Request, services: Jobs, actor: CurrentAdministrator, job_id: JobId
) -> Response:
    """Name the job that will run outside its schedule; nothing starts until it is confirmed."""
    view, digest = await services.jobs.run_confirmation(actor, job_id)
    return html_page(
        request,
        "jobs/run_confirm.html",
        {"view": view, "blocked": blocked_reason(view.state), "digest": digest},
    )


@router.post("/{job_id}/run", name="job_run")
async def run_job(
    request: Request,
    services: Jobs,
    actor: CurrentAdministrator,
    job_id: JobId,
    dry_run: Annotated[str, Form(max_length=DURATION_MAX)] = "",
    expected_digest: Annotated[str, Form(max_length=DIGEST_MAX)] = "",
) -> Response:
    """Start a run as the job was reviewed, or a dry run that changes nothing, and open it."""
    is_dry = dry_run == "yes"
    try:
        run_id = await services.jobs.run_now(
            actor, job_id, dry_run=is_dry, expected_digest=expected_digest
        )
    except SchedulerBusy as refusal:
        return _refused(request, job_id, refusal)
    except ValidationFailed as invalid:
        return _refused(request, job_id, invalid, status=422)
    notice = "dry_run" if is_dry else "started"
    return _noted(request, route(request, "job_run_detail", run_id=run_id), notice)
