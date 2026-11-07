"""JSON routes for automation rules: read, pause, resume, disable and run, by bearer token."""

import json
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Annotated, Any, Final, Literal, NoReturn, Self

from fastapi import APIRouter, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.containers import MAX_CURSOR, Token
from app.api.job_models import (
    JobRunModel,
    JobRunPageModel,
    JobScheduleModel,
    run_model,
    schedule_model,
)
from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE, InvalidCursorError
from app.scheduler.loop import SchedulerBusy
from app.services.assembly import Jobs
from app.services.automation_rules import RuleView, rule_digest
from app.services.environments import DEFAULT_PAGE_SIZE
from app.services.jobs import JobNotRunnable
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.web.identifiers import PathId, QueryId

router = APIRouter(prefix="/api/v1/rules", tags=["rules"])

MAX_SUBSYSTEM: Final = 64
DIGEST_PATTERN: Final = r"^[0-9a-f]{64}$"
DIGEST_REQUIRED: Final = "A real run needs expected_digest."
CHANGED_FIELD: Final = "job"


class RuleOwnerModel(BaseModel):
    """The user who created a rule."""

    id: int
    username: str | None


class RuleWebhookModel(BaseModel):
    """A webhook rule's endpoint. The token is never returned; it is shown once, when issued."""

    prefix: str
    state: str
    issued_at: str
    last_called_at: str | None


class RuleModel(BaseModel):
    """An automation rule with its job's state; selector is null for an instance-wide rule."""

    id: int
    name: str
    subsystem: str
    environment_id: int | None
    environment_name: str | None
    selector: Any
    target: str
    parameters: Any
    enabled: bool
    paused_until: str | None
    state: str
    schedule: JobScheduleModel
    next_run_at: str | None
    last_run: JobRunModel | None
    supports_dry_run: bool
    subsystem_enabled: bool
    available: bool
    owner: RuleOwnerModel | None
    created_at: str
    updated_at: str
    digest: str
    webhook: RuleWebhookModel | None


class RulePageModel(BaseModel):
    """One page of rules by id; next_cursor is null on the last page."""

    items: list[RuleModel]
    next_cursor: str | None


class PauseRequest(BaseModel):
    """How long to pause a rule."""

    model_config = ConfigDict(extra="forbid")

    duration: Literal["1h", "4h", "24h", "7d"]


class RunRequest(BaseModel):
    """A run now; a real run names the digest of the rule as reviewed."""

    model_config = ConfigDict(extra="forbid")

    dry_run: bool = False
    expected_digest: Annotated[str | None, Field(pattern=DIGEST_PATTERN)] = None

    @model_validator(mode="after")
    def _digest_for_real_run(self) -> Self:
        if not self.dry_run and self.expected_digest is None:
            raise ValueError(DIGEST_REQUIRED)
        return self


class RunStartedModel(BaseModel):
    """The run a request started."""

    run_id: int


def _structured(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _rule(view: RuleView, principal: Principal) -> RuleModel:
    rule = view.rule
    owner_id = rule.created_by_user_id
    return RuleModel(
        id=rule.id,
        name=rule.name,
        subsystem=rule.subsystem,
        environment_id=rule.environment_id,
        environment_name=view.environment_name,
        selector=_structured(rule.target_selector) if rule.target_selector else None,
        target=view.target,
        parameters=_structured(rule.parameters),
        enabled=rule.is_enabled,
        paused_until=view.paused_until,
        state=view.state,
        schedule=schedule_model(view.job, view.schedule),
        next_run_at=view.next_run_at,
        last_run=None if view.last_run is None else run_model(view.last_run),
        supports_dry_run=view.supports_dry_run,
        subsystem_enabled=view.subsystem_enabled,
        available=view.available,
        owner=(
            RuleOwnerModel(id=owner_id, username=view.owner)
            if principal.is_administrator and owner_id is not None
            else None
        ),
        created_at=rule.created_at,
        updated_at=rule.updated_at,
        digest=rule_digest(rule, view.job),
        webhook=(
            None
            if view.webhook is None
            else RuleWebhookModel(
                prefix=view.webhook.prefix,
                state=view.webhook.state,
                issued_at=view.webhook.issued_at,
                last_called_at=view.webhook.last_called_at,
            )
        ),
    )


def _problem(status: int, message: str, **extra: object) -> JSONResponse:
    body = {"error": {"status": status, "message": message, **extra}}
    return JSONResponse(body, status_code=status)


def _unprocessable(error: ValidationFailed, location: str) -> RequestValidationError:
    return RequestValidationError(
        [
            {"loc": (location, name), "msg": message, "type": "value_error"}
            for name, message in error.fields.items()
        ]
    )


def _invalid_cursor() -> NoReturn:
    raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail="invalid cursor")


async def _attempt[T](call: Callable[[], Awaitable[T]]) -> T | JSONResponse:
    """Run a service call and answer its refusals; not found and denial propagate."""
    try:
        return await call()
    except ValidationFailed as error:
        if CHANGED_FIELD in error.fields:
            message = error.fields[CHANGED_FIELD]
            return _problem(HTTPStatus.CONFLICT, message, reason="changed_since_review")
        raise _unprocessable(error, "body") from None
    except SchedulerBusy as error:
        reason = error.reason if isinstance(error, JobNotRunnable) else "busy"
        return _problem(HTTPStatus.CONFLICT, str(error), reason=reason)
    except EnvironmentUnavailable as error:
        return _problem(HTTPStatus.SERVICE_UNAVAILABLE, str(error), reason=error.kind)
    except ObjectTooLarge as error:
        return _problem(HTTPStatus.SERVICE_UNAVAILABLE, str(error))


def _responses(*statuses: HTTPStatus) -> dict[int | str, dict[str, Any]]:
    return {status: {"description": status.phrase} for status in statuses}


_READ_RESPONSES = _responses(
    HTTPStatus.BAD_REQUEST,
    HTTPStatus.UNAUTHORIZED,
    HTTPStatus.NOT_FOUND,
    HTTPStatus.UNPROCESSABLE_ENTITY,
)
_WRITE_RESPONSES = _responses(
    HTTPStatus.UNAUTHORIZED,
    HTTPStatus.FORBIDDEN,
    HTTPStatus.NOT_FOUND,
    HTTPStatus.CONFLICT,
    HTTPStatus.UNPROCESSABLE_ENTITY,
    HTTPStatus.SERVICE_UNAVAILABLE,
)


@router.get("", response_model=RulePageModel, responses=_READ_RESPONSES)
async def list_rules(
    principal: Token,
    services: Jobs,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR)] = None,
    subsystem: Annotated[str | None, Query(max_length=MAX_SUBSYSTEM)] = None,
    environment_id: QueryId = None,
) -> RulePageModel:
    """List rules by id: the caller's own, or every rule for an administrator."""
    try:
        page = await services.rules.overview(
            principal,
            limit=limit,
            cursor=cursor,
            subsystem=subsystem,
            environment_id=environment_id,
        )
    except ValidationFailed as error:
        raise _unprocessable(error, "query") from None
    except InvalidCursorError:
        _invalid_cursor()
    return RulePageModel(
        items=[_rule(view, principal) for view in page.items], next_cursor=page.next_cursor
    )


@router.get("/{rule_id}", response_model=RuleModel, responses=_READ_RESPONSES)
async def get_rule(rule_id: PathId, principal: Token, services: Jobs) -> RuleModel:
    """Return one rule the caller may see."""
    return _rule(await services.rules.detail(principal, rule_id), principal)


@router.get("/{rule_id}/runs", response_model=JobRunPageModel, responses=_READ_RESPONSES)
async def list_rule_runs(
    rule_id: PathId,
    principal: Token,
    services: Jobs,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR)] = None,
) -> JobRunPageModel:
    """List a rule's runs, newest first."""
    try:
        page = await services.rules.runs(principal, rule_id, limit=limit, cursor=cursor)
    except InvalidCursorError:
        _invalid_cursor()
    return JobRunPageModel(
        items=[run_model(run) for run in page.items], next_cursor=page.next_cursor
    )


@router.get("/{rule_id}/runs/{run_id}", response_model=JobRunModel, responses=_READ_RESPONSES)
async def get_rule_run(
    rule_id: PathId, run_id: PathId, principal: Token, services: Jobs
) -> JobRunModel:
    """Return one run of the rule; a run of another job is not found."""
    _, run = await services.rules.run(principal, rule_id, run_id)
    return run_model(run)


@router.post("/{rule_id}/pause", response_model=RuleModel, responses=_WRITE_RESPONSES)
async def pause_rule(
    rule_id: PathId, body: PauseRequest, principal: Token, services: Jobs
) -> RuleModel | JSONResponse:
    """Pause a rule for the chosen duration."""

    async def act() -> RuleModel:
        await services.rules.pause(principal, rule_id, duration=body.duration)
        return _rule(await services.rules.detail(principal, rule_id), principal)

    return await _attempt(act)


@router.post("/{rule_id}/resume", response_model=RuleModel, responses=_WRITE_RESPONSES)
async def resume_rule(
    rule_id: PathId, principal: Token, services: Jobs
) -> RuleModel | JSONResponse:
    """Clear a rule's pause."""

    async def act() -> RuleModel:
        await services.rules.resume(principal, rule_id)
        return _rule(await services.rules.detail(principal, rule_id), principal)

    return await _attempt(act)


@router.post("/{rule_id}/disable", response_model=RuleModel, responses=_WRITE_RESPONSES)
async def disable_rule(
    rule_id: PathId, principal: Token, services: Jobs
) -> RuleModel | JSONResponse:
    """Disable a rule; nothing is deleted."""

    async def act() -> RuleModel:
        await services.rules.disable(principal, rule_id)
        return _rule(await services.rules.detail(principal, rule_id), principal)

    return await _attempt(act)


@router.post(
    "/{rule_id}/run",
    response_model=RunStartedModel,
    status_code=HTTPStatus.ACCEPTED,
    responses=_WRITE_RESPONSES,
)
async def run_rule(
    rule_id: PathId, body: RunRequest, principal: Token, services: Jobs
) -> RunStartedModel | JSONResponse:
    """Start a run now; a real run starts only if the rule still matches expected_digest."""

    async def act() -> RunStartedModel:
        run_id = await services.rules.run_now(
            principal, rule_id, dry_run=body.dry_run, expected_digest=body.expected_digest
        )
        return RunStartedModel(run_id=run_id)

    return await _attempt(act)
