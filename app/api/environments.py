"""JSON routes for environments, authenticated by bearer token only."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from app.auth.dependencies import require_token_user
from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE, InvalidCursorError
from app.services.assembly import Environments
from app.services.environments import (
    DEFAULT_PAGE_SIZE,
    DashboardFilters,
    EnvironmentSummary,
)

router = APIRouter(prefix="/api/v1/environments", tags=["environments"])

MAX_QUERY_TEXT = 64
MAX_ID = 2**63 - 1
MAX_TAG_PARAMS = 10
UNGROUPED = "none"

Token = Annotated[Principal, Depends(require_token_user)]
ObjectId = Annotated[int, Path(ge=1, le=MAX_ID)]


class TagModel(BaseModel):
    """A tag on an environment."""

    id: int
    name: str


class GroupModel(BaseModel):
    """The group an environment is in."""

    id: int
    name: str


class ConnectionModel(BaseModel):
    """Where an environment is; present for administrators only."""

    kind: str
    endpoint: str
    is_local: bool


class CountsModel(BaseModel):
    """Object counts of a host, which include objects outside a caller's own grants."""

    container_count: int | None
    container_running_count: int | None
    container_stopped_count: int | None
    container_paused_count: int | None
    container_unhealthy_count: int | None
    image_count: int | None
    volume_count: int | None
    network_count: int | None
    stack_count: int | None
    service_count: int | None
    swarm_node_count: int | None


class SnapshotModel(BaseModel):
    """The cached counts and how fresh they are; present for administrators only."""

    captured_at: str | None
    stale: bool
    error: str | None
    counts: CountsModel | None


class EnvironmentModel(BaseModel):
    """An environment. No TLS material or metadata is ever part of this model."""

    id: int
    name: str
    type: str | None
    status: str
    failed_stage: str | None
    status_message: str | None
    last_checked_at: str | None
    last_contacted_at: str | None
    engine_version: str | None
    api_version: str | None
    swarm_state: str | None
    swarm_control_available: bool | None
    group: GroupModel | None
    tags: list[TagModel]
    connection: ConnectionModel | None
    snapshot: SnapshotModel | None


class EnvironmentDetailModel(EnvironmentModel):
    """An environment with the fields only its own resource carries."""

    compose_cli_version: str | None
    created_at: str | None


class EnvironmentPageModel(BaseModel):
    """One page of environments; next_cursor is null on the last page."""

    items: list[EnvironmentModel]
    next_cursor: str | None


def _fields_of(summary: EnvironmentSummary) -> dict[str, Any]:
    connection = summary.connection
    snapshot = summary.snapshot
    return {
        "id": summary.id,
        "name": summary.name,
        "type": summary.type,
        "status": summary.status,
        "failed_stage": summary.failed_stage,
        "status_message": summary.status_message,
        "last_checked_at": summary.last_checked_at,
        "last_contacted_at": summary.last_contacted_at,
        "engine_version": summary.engine_version,
        "api_version": summary.api_version,
        "swarm_state": summary.swarm_state,
        "swarm_control_available": summary.swarm_control_available,
        "group": (
            None
            if summary.group is None
            else GroupModel(id=summary.group.id, name=summary.group.name)
        ),
        "tags": [TagModel(id=tag.id, name=tag.name) for tag in summary.tags],
        "connection": (
            None
            if connection is None
            else ConnectionModel(
                kind=connection.kind, endpoint=connection.endpoint, is_local=connection.is_local
            )
        ),
        "snapshot": (
            None
            if snapshot is None
            else SnapshotModel(
                captured_at=snapshot.captured_at,
                stale=snapshot.stale,
                error=snapshot.error,
                counts=None
                if snapshot.counts is None
                else CountsModel(
                    container_count=snapshot.counts.container_count,
                    container_running_count=snapshot.counts.container_running_count,
                    container_stopped_count=snapshot.counts.container_stopped_count,
                    container_paused_count=snapshot.counts.container_paused_count,
                    container_unhealthy_count=snapshot.counts.container_unhealthy_count,
                    image_count=snapshot.counts.image_count,
                    volume_count=snapshot.counts.volume_count,
                    network_count=snapshot.counts.network_count,
                    stack_count=snapshot.counts.stack_count,
                    service_count=snapshot.counts.service_count,
                    swarm_node_count=snapshot.counts.swarm_node_count,
                ),
            )
        ),
    }


def _unprocessable(error: ValidationFailed) -> RequestValidationError:
    return RequestValidationError(
        [
            {"loc": ("query", name), "msg": message, "type": "value_error"}
            for name, message in error.fields.items()
        ]
    )


@router.get("", response_model=EnvironmentPageModel)
async def list_environments(
    principal: Token,
    services: Environments,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    q: Annotated[str | None, Query(max_length=MAX_QUERY_TEXT)] = None,
    group: Annotated[str | None, Query(max_length=20)] = None,
    status: Annotated[str | None, Query(max_length=32)] = None,
    tag: Annotated[
        list[Annotated[int, Field(ge=1, le=MAX_ID)]] | None, Query(max_length=MAX_TAG_PARAMS)
    ] = None,
) -> EnvironmentPageModel:
    """List the environments the token's user may see, by name."""
    group_id: int | None = None
    if group is not None and group != UNGROUPED:
        if not (group.isascii() and group.isdigit() and 1 <= int(group[:20]) <= MAX_ID):
            raise RequestValidationError(
                [
                    {
                        "loc": ("query", "group"),
                        "msg": "Use a group id or 'none'.",
                        "type": "value_error",
                    }
                ]
            )
        group_id = int(group)
    filters = DashboardFilters(
        q=q,
        group_id=group_id,
        ungrouped=group == UNGROUPED,
        status=status,
        tag_ids=tuple(tag or ()),
    )
    try:
        page = await services.environments.dashboard(principal, filters, limit=limit, cursor=cursor)
    except ValidationFailed as error:
        raise _unprocessable(error) from None
    except InvalidCursorError:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return EnvironmentPageModel(
        items=[EnvironmentModel(**_fields_of(item)) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{environment_id}", response_model=EnvironmentDetailModel)
async def get_environment(
    environment_id: ObjectId, principal: Token, services: Environments
) -> EnvironmentDetailModel:
    """Return one environment; an environment the user may not see is not found."""
    detail = await services.environments.get(principal, environment_id)
    return EnvironmentDetailModel(
        **_fields_of(detail.summary),
        compose_cli_version=detail.compose_cli_version,
        created_at=detail.created_at,
    )
