"""JSON route for the activity log: read-only, administrators only, bearer token."""

from datetime import UTC, datetime
from typing import Annotated, Final, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from app.auth.dependencies import require_token_user
from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.activity_event import REQUEST_ID_PATTERN, ActivityFilter
from app.db.repositories.pagination import MAX_PAGE_SIZE, MAX_ROW_ID, InvalidCursorError
from app.services.activity_log import ActivityEventView
from app.services.assembly import ActivityLog
from app.services.environments import DEFAULT_PAGE_SIZE
from app.services.guards import require_administrator

router = APIRouter(prefix="/api/v1/activity", tags=["activity"])

MAX_CURSOR: Final = 512
MAX_SUBSYSTEM: Final = 64
MAX_TARGET_ID: Final = 256
NAME_PATTERN: Final = r"^[a-z_]{1,64}$"
MAX_REQUEST_ID: Final = 256
REQUEST_ID_MESSAGE: Final = "Use 1 to 64 letters, digits, dots, hyphens or underscores."
EARLIEST_TIME: Final = datetime(1970, 1, 1, tzinfo=UTC)
LATEST_TIME: Final = datetime(9999, 1, 1, tzinfo=UTC)

OptionalRowId = Annotated[int | None, Query(ge=1, le=MAX_ROW_ID)]


async def require_token_administrator(
    principal: Annotated[Principal, Depends(require_token_user)],
) -> Principal:
    """Return the bearer token's user if an administrator; answered before the query is read."""
    require_administrator(principal)
    return principal


Administrator = Annotated[Principal, Depends(require_token_administrator)]


class ActivityItemModel(BaseModel):
    """One event as stored, with the current names of its actor and environment."""

    id: int
    occurred_at: str
    category: str
    actor_type: str
    actor_user_id: int | None
    actor_username: str | None
    actor_subsystem: str | None
    rule_id: int | None
    job_run_id: int | None
    request_id: str | None
    environment_id: int | None
    environment_name: str | None
    target_type: str
    target_id: str | None
    action: str
    result: str
    detail: dict[str, str | int | bool | None]


class ActivityPageModel(BaseModel):
    """One page of events, newest first; next_cursor is null on the last page."""

    items: list[ActivityItemModel]
    next_cursor: str | None


def _item(view: ActivityEventView) -> ActivityItemModel:
    event = view.event
    return ActivityItemModel(
        id=event.id,
        occurred_at=event.occurred_at,
        category=event.category,
        actor_type=event.actor_type,
        actor_user_id=event.actor_user_id,
        actor_username=view.actor_username,
        actor_subsystem=event.actor_subsystem,
        rule_id=event.rule_id,
        job_run_id=event.job_run_id,
        request_id=event.request_id,
        environment_id=event.environment_id,
        environment_name=view.environment_name,
        target_type=event.target_type,
        target_id=event.target_id,
        action=event.action,
        result=event.result,
        detail=dict(event.detail or {}),
    )


def _unprocessable(fields: dict[str, str]) -> RequestValidationError:
    return RequestValidationError(
        [
            {"loc": ("query", name), "msg": message, "type": "value_error"}
            for name, message in fields.items()
        ]
    )


def _out_of_range(moment: datetime) -> bool:
    try:
        return not EARLIEST_TIME <= moment.astimezone(UTC) < LATEST_TIME
    except (OverflowError, ValueError):
        return True


def _invalid_cursor() -> NoReturn:
    raise HTTPException(status_code=400, detail="invalid cursor")


def _criteria(
    *,
    category: Literal["authentication", "user_action", "automation"] | None,
    actor_type: Literal["user", "subsystem", "anonymous"] | None,
    user_id: int | None,
    subsystem: str | None,
    rule_id: int | None,
    job_run_id: int | None,
    environment_id: int | None,
    target_type: str | None,
    target_id: str | None,
    action: str | None,
    result: Literal["success", "failure"] | None,
    occurred_from: datetime | None,
    occurred_to: datetime | None,
    request_id: str | None,
) -> ActivityFilter:
    problems: dict[str, str] = {}
    if request_id is not None and REQUEST_ID_PATTERN.fullmatch(request_id) is None:
        problems["request_id"] = REQUEST_ID_MESSAGE
    for name, moment in (("from", occurred_from), ("to", occurred_to)):
        if moment is None:
            continue
        if moment.tzinfo is None:
            problems[name] = "Give a time with an offset, such as 2026-01-31T09:00:00Z."
        elif _out_of_range(moment):
            problems[name] = "Give a time between 1970 and 9998 (UTC)."
    if target_id is not None and target_type is None:
        problems["target_type"] = "A target id needs a target type."
    if target_type is not None and target_id is None and environment_id is None:
        problems["target_id"] = "A target type needs a target id or an environment."
    if problems:
        raise _unprocessable(problems)
    return ActivityFilter(
        category=category,
        actor_type=actor_type,
        actor_user_id=user_id,
        actor_subsystem=subsystem,
        rule_id=rule_id,
        environment_id=environment_id,
        target_type=target_type,
        target_id=target_id,
        result=result,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        action=action,
        job_run_id=job_run_id,
        request_id=request_id,
    )


@router.get("", response_model=ActivityPageModel)
async def list_activity(
    principal: Administrator,
    log: ActivityLog,
    category: Literal["authentication", "user_action", "automation"] | None = None,
    actor_type: Literal["user", "subsystem", "anonymous"] | None = None,
    user_id: OptionalRowId = None,
    subsystem: Annotated[str | None, Query(max_length=MAX_SUBSYSTEM)] = None,
    rule_id: OptionalRowId = None,
    job_run_id: OptionalRowId = None,
    request_id: Annotated[str | None, Query(max_length=MAX_REQUEST_ID)] = None,
    environment_id: OptionalRowId = None,
    target_type: Annotated[str | None, Query(pattern=NAME_PATTERN)] = None,
    target_id: Annotated[str | None, Query(min_length=1, max_length=MAX_TARGET_ID)] = None,
    action: Annotated[str | None, Query(pattern=NAME_PATTERN)] = None,
    result: Literal["success", "failure"] | None = None,
    occurred_from: Annotated[datetime | None, Query(alias="from")] = None,
    occurred_to: Annotated[datetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR)] = None,
) -> ActivityPageModel:
    """List events newest first with optional filters; administrators only."""
    criteria = _criteria(
        category=category,
        actor_type=actor_type,
        user_id=user_id,
        subsystem=subsystem,
        rule_id=rule_id,
        job_run_id=job_run_id,
        environment_id=environment_id,
        target_type=target_type,
        target_id=target_id,
        action=action,
        result=result,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        request_id=request_id,
    )
    try:
        page = await log.page(principal, criteria, limit=limit, cursor=cursor)
    except InvalidCursorError:
        _invalid_cursor()
    except ValidationFailed as error:
        raise _unprocessable(
            {
                "subsystem" if name == "actor_subsystem" else name: message
                for name, message in error.fields.items()
            }
        ) from None
    return ActivityPageModel(
        items=[_item(view) for view in page.items], next_cursor=page.next_cursor
    )
