"""JSON routes for containers, authenticated by bearer token only."""

from collections.abc import Callable, Coroutine, Mapping
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth.dependencies import require_token_user
from app.auth.errors import (
    NotFound,
    ProductError,
    ResourceNotFound,
    SelfProtected,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.services.access import is_canonical_id
from app.services.assembly import Containers
from app.services.container_actions import (
    ActionOutcome,
    BusyView,
    ContainerGone,
    ContainerStateConflict,
    EngineRefused,
)
from app.services.container_provisioning import (
    NameChanged,
    NotRecreatable,
    PullFailed,
    RecreateFailed,
    SubmissionRejected,
)
from app.services.container_review import RecreateOutcome
from app.services.containers import (
    DEFAULT_PAGE_SIZE,
    HEALTH_FILTERS,
    MAX_IMAGE_FILTER,
    MAX_LABEL_FILTER_LENGTH,
    MAX_LABEL_FILTERS,
    MAX_NAME_FILTER,
    MAX_STACK_FILTER,
    ContainerDetail,
    ContainerFilters,
    ContainerListTooLarge,
    ContainerRow,
    PortView,
)
from app.services.image_bindings import RebindRefused
from app.services.object_inspector import ContainerTooLarge, EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectBusy
from app.services.pull_credentials import (
    CredentialChoice,
    CredentialChoiceRequired,
    CredentialSetMismatch,
)
from app.services.registry_resolution import CredentialSetUnavailable

MAX_ID = 2**63 - 1
MAX_STATE_FILTER = 16
MAX_CURSOR = 512

type Handler = Callable[[Request], Coroutine[Any, Any, Response]]
type Verb = Literal["start", "stop", "restart"]


def _problem(status: int, message: str, **extra: object) -> JSONResponse:
    body = {"error": {"status": status, "message": message, **extra}}
    return JSONResponse(body, status_code=status)


def _rejection(error: ValidationFailed) -> JSONResponse:
    fields = [{"loc": ["query", name], "msg": text} for name, text in error.fields.items()]
    return _problem(HTTPStatus.BAD_REQUEST, HTTPStatus.BAD_REQUEST.phrase, fields=fields)


_STATUS_OF: Mapping[type[ProductError], HTTPStatus] = {
    EnvironmentUnavailable: HTTPStatus.SERVICE_UNAVAILABLE,
    ContainerListTooLarge: HTTPStatus.SERVICE_UNAVAILABLE,
    ContainerTooLarge: HTTPStatus.SERVICE_UNAVAILABLE,
    ContainerStateConflict: HTTPStatus.CONFLICT,
    ContainerGone: HTTPStatus.NOT_FOUND,
    EngineRefused: HTTPStatus.BAD_GATEWAY,
}


class ContainerRoute(APIRoute):
    """Answers the product errors the container services raise, which have no global handler."""

    def get_route_handler(self) -> Handler:
        handle = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            try:
                return await handle(request)
            except ValidationFailed as error:
                return _rejection(error)
            except SelfProtected as error:
                return _problem(HTTPStatus.CONFLICT, str(error), reason=error.reason)
            except ProductError as error:
                status = next(
                    (status for kind, status in _STATUS_OF.items() if isinstance(error, kind)),
                    None,
                )
                if status is None:
                    raise
                return _problem(status, str(error))

        return guarded


router = APIRouter(
    prefix="/api/v1/environments/{environment_id}/containers",
    tags=["containers"],
    route_class=ContainerRoute,
)

Token = Annotated[Principal, Depends(require_token_user)]
EnvironmentId = Annotated[int, Path(ge=1, le=MAX_ID)]


def _canonical_container_id(container_id: Annotated[str, Path()]) -> str:
    if not is_canonical_id("container", container_id):
        raise ResourceNotFound("not found")
    return container_id


ContainerId = Annotated[str, Depends(_canonical_container_id)]


class PortModel(BaseModel):
    """A published or exposed port; ip is null when bound to every interface."""

    private_port: int
    public_port: int | None
    protocol: str
    ip: str | None


class ContainerFields(BaseModel):
    """What the list and the detail of a container have in common."""

    id: str
    name: str
    names: list[str]
    image: str
    state: str
    health: str | None
    ports: list[PortModel]
    stack: str | None
    labels: dict[str, str]
    labels_truncated: bool


class ContainerModel(ContainerFields):
    """A container of the list. Label values the caller may not see are redacted."""

    created_at: datetime


class ContainerPageModel(BaseModel):
    """One page of containers; total counts every match, next_cursor is null on the last page."""

    items: list[ContainerModel]
    next_cursor: str | None
    total: int


class MountModel(BaseModel):
    """A mount; source is the host path of a bind mount and is set for administrators only."""

    type: str
    destination: str
    volume_name: str | None
    read_write: bool
    source: str | None


class NetworkModel(BaseModel):
    """A container's attachment to one network."""

    name: str
    ip_address: str
    gateway: str
    mac_address: str
    aliases: list[str]


class ContainerDetailModel(ContainerFields):
    """A container with its mounts and networks; truncated is true when a list was cut."""

    created_at: datetime | None
    restart_count: int
    mounts: list[MountModel]
    networks: list[NetworkModel]
    truncated: bool


class ActionModel(BaseModel):
    """What an action did; unchanged means the container was already in the requested state.

    state and health are read after the action and are null when that read failed.
    """

    action: str
    result: Literal["done", "unchanged"]
    id: str
    name: str
    state: str | None
    health: str | None


def _port(port: PortView) -> PortModel:
    return PortModel(
        private_port=port.private_port,
        public_port=port.public_port,
        protocol=port.protocol,
        ip=port.ip,
    )


def _summary_fields(row: ContainerRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "names": list(row.names),
        "image": row.image,
        "state": row.state,
        "health": row.health,
        "created_at": row.created_at,
        "ports": [_port(port) for port in row.ports],
        "stack": row.stack,
        "labels": dict(row.labels),
        "labels_truncated": row.labels_truncated,
    }


def _detail_model(detail: ContainerDetail) -> ContainerDetailModel:
    health = detail.health_status if detail.health_status in HEALTH_FILTERS else None
    return ContainerDetailModel.model_validate(
        {
            "id": detail.id,
            "name": detail.name,
            "names": [detail.name],
            "image": detail.image,
            "state": detail.state,
            "health": health,
            "created_at": detail.created_at,
            "ports": [_port(port) for port in detail.ports],
            "stack": detail.stack,
            "labels": {label.key: label.value for label in detail.labels},
            "labels_truncated": detail.labels_truncated,
            "restart_count": detail.restart_count,
            "mounts": [
                MountModel(
                    type=mount.type,
                    destination=mount.destination,
                    volume_name=mount.volume_name,
                    read_write=mount.read_write,
                    source=mount.source,
                )
                for mount in detail.mounts
            ],
            "networks": [
                NetworkModel(
                    name=network.name,
                    ip_address=network.ip_address,
                    gateway=network.gateway,
                    mac_address=network.mac_address,
                    aliases=list(network.aliases),
                )
                for network in detail.networks
            ],
            "truncated": detail.truncated,
        }
    )


def _busy(view: BusyView) -> JSONResponse:
    return _problem(
        HTTPStatus.CONFLICT, "The object is busy with another action.", holder=asdict(view)
    )


@router.get("", response_model=ContainerPageModel)
async def list_containers(
    principal: Token,
    environment_id: EnvironmentId,
    services: Containers,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR)] = None,
    state: Annotated[str | None, Query(max_length=MAX_STATE_FILTER)] = None,
    health: Annotated[str | None, Query(max_length=MAX_STATE_FILTER)] = None,
    q: Annotated[str | None, Query(max_length=MAX_NAME_FILTER)] = None,
    image: Annotated[str | None, Query(max_length=MAX_IMAGE_FILTER)] = None,
    label: Annotated[
        list[Annotated[str, Field(max_length=MAX_LABEL_FILTER_LENGTH)]] | None,
        Query(max_length=MAX_LABEL_FILTERS),
    ] = None,
    stack: Annotated[str | None, Query(max_length=MAX_STACK_FILTER)] = None,
) -> ContainerPageModel:
    """List the containers the token's user may see, by name, read live from the host."""
    filters = ContainerFilters(
        state=state, health=health, q=q, image=image, labels=tuple(label or ()), stack=stack
    )
    result = await services.queries.page(
        principal, environment_id, filters, limit=limit, cursor=cursor
    )
    return ContainerPageModel(
        items=[ContainerModel(**_summary_fields(row)) for row in result.page.items],
        next_cursor=result.page.next_cursor,
        total=result.total,
    )


@router.get("/{container_id}", response_model=ContainerDetailModel)
async def get_container(
    principal: Token,
    environment_id: EnvironmentId,
    container_id: ContainerId,
    services: Containers,
) -> ContainerDetailModel:
    """Return one container by its full id; one the user may not see is not found."""
    return _detail_model(await services.queries.detail(principal, environment_id, container_id))


async def _state_after(
    outcome: ActionOutcome, principal: Principal, services: Containers
) -> ActionModel:
    state: str | None = None
    health: str | None = None
    try:
        row = await services.queries.row(principal, outcome.environment_id, outcome.container_id)
    except (NotFound, EnvironmentUnavailable, ContainerTooLarge):
        pass
    else:
        state, health = row.state, row.health
    return ActionModel(
        action=outcome.action,
        result=outcome.result,
        id=outcome.container_id,
        name=outcome.name,
        state=state,
        health=health,
    )


async def _act(
    verb: Verb,
    environment_id: int,
    container_id: str,
    principal: Principal,
    services: Containers,
) -> ActionModel | JSONResponse:
    actions = services.actions
    try:
        match verb:
            case "start":
                outcome = await actions.start(principal, environment_id, container_id)
            case "stop":
                outcome = await actions.stop(principal, environment_id, container_id)
            case "restart":
                outcome = await actions.restart(principal, environment_id, container_id)
    except ObjectBusy as busy:
        return _busy(actions.describe_busy(busy, principal))
    return await _state_after(outcome, principal, services)


_ACTION_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"description": status.phrase}
    for status in (
        HTTPStatus.FORBIDDEN,
        HTTPStatus.NOT_FOUND,
        HTTPStatus.CONFLICT,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
    )
}


@router.post("/{container_id}/start", response_model=ActionModel, responses=_ACTION_RESPONSES)
async def start_container(
    principal: Token,
    environment_id: EnvironmentId,
    container_id: ContainerId,
    services: Containers,
) -> ActionModel | JSONResponse:
    """Start a container; the result is unchanged if it was already running."""
    return await _act("start", environment_id, container_id, principal, services)


@router.post("/{container_id}/stop", response_model=ActionModel, responses=_ACTION_RESPONSES)
async def stop_container(
    principal: Token,
    environment_id: EnvironmentId,
    container_id: ContainerId,
    services: Containers,
) -> ActionModel | JSONResponse:
    """Stop a container; the result is unchanged if it was already stopped."""
    return await _act("stop", environment_id, container_id, principal, services)


@router.post("/{container_id}/restart", response_model=ActionModel, responses=_ACTION_RESPONSES)
async def restart_container(
    principal: Token,
    environment_id: EnvironmentId,
    container_id: ContainerId,
    services: Containers,
) -> ActionModel | JSONResponse:
    """Restart a container."""
    return await _act("restart", environment_id, container_id, principal, services)


class CredentialChoiceModel(BaseModel):
    """The credential set to pull with: a set id, or `anonymous` for a pull with no credentials."""

    model_config = ConfigDict(extra="forbid")

    credential_set_id: int | None = Field(default=None, ge=1, le=MAX_ID)
    anonymous: bool = False

    @model_validator(mode="after")
    def _exactly_one(self) -> "CredentialChoiceModel":
        if (self.credential_set_id is None) == (not self.anonymous):
            raise ValueError("give either credential_set_id or anonymous: true")
        return self

    def choice(self) -> CredentialChoice:
        """Return the choice this body states."""
        return CredentialChoice(self.credential_set_id)


class RecreateRequest(BaseModel):
    """The name the caller reviewed, whether to pull first, and the credential set to pull with."""

    model_config = ConfigDict(extra="forbid")

    expected_name: str = Field(min_length=1, max_length=256)
    pull: bool = False
    credential_choice: CredentialChoiceModel | None = None


class RecreateModel(BaseModel):
    """What a recreate did; changed names categories of configuration, never values."""

    old_id: str
    new_id: str
    name: str
    pulled: bool
    image_changed: bool
    changed: list[str]
    started: bool
    old_removed: bool
    remove_outcome_unknown: bool
    kept_as: str | None
    control_moved: bool
    old_control_kept: bool


def _recreate_model(outcome: RecreateOutcome) -> RecreateModel:
    return RecreateModel(
        old_id=outcome.old_id,
        new_id=outcome.new_id,
        name=outcome.name,
        pulled=outcome.pulled,
        image_changed=outcome.image_changed,
        changed=list(outcome.changed),
        started=outcome.started,
        old_removed=outcome.old_removed,
        remove_outcome_unknown=outcome.remove_outcome_unknown,
        kept_as=outcome.kept_as,
        control_moved=outcome.control_moved,
        old_control_kept=outcome.old_control_kept,
    )


_RECREATE_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"description": status.phrase}
    for status in (
        HTTPStatus.BAD_REQUEST,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.NOT_FOUND,
        HTTPStatus.CONFLICT,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
    )
}


@router.post(
    "/{container_id}/recreate", response_model=RecreateModel, responses=_RECREATE_RESPONSES
)
async def recreate_container(
    principal: Token,
    environment_id: EnvironmentId,
    container_id: ContainerId,
    body: RecreateRequest,
    services: Containers,
) -> RecreateModel | JSONResponse:
    """Replace a container with one built from its own configuration.

    The body names the container as the caller reviewed it. A configuration that uses options
    only administrators may set is refused with fields keyed by Engine path. With `pull`, the
    reference's binding picks the credential set; with none and several fitting sets the answer
    is 409 `credential_choice_required` listing them, and `credential_choice` answers it. Changing
    an existing binding is for administrators.
    """
    choice = None if body.credential_choice is None else body.credential_choice.choice()
    try:
        outcome = await services.recreation.recreate(
            principal,
            environment_id,
            container_id,
            expected_name=body.expected_name,
            pull=body.pull,
            credential_choice=choice,
        )
    except ObjectBusy as busy:
        return _busy(services.actions.describe_busy(busy, principal))
    except RebindRefused as denied:
        return _problem(HTTPStatus.FORBIDDEN, str(denied), reason="restricted")
    except CredentialChoiceRequired as required:
        candidates = [{"id": c.id, "name": c.name} for c in required.candidates]
        return _problem(
            HTTPStatus.CONFLICT,
            str(required),
            reason="credential_choice_required",
            candidates=candidates,
        )
    except (CredentialSetUnavailable, CredentialSetMismatch) as unavailable:
        return _problem(HTTPStatus.CONFLICT, str(unavailable), reason="credential_unavailable")
    except SubmissionRejected as refused:
        fields = [
            {"loc": ["configuration", path], "msg": msg} for path, msg in refused.fields.items()
        ]
        return _problem(HTTPStatus.BAD_REQUEST, HTTPStatus.BAD_REQUEST.phrase, fields=fields)
    except NameChanged as error:
        return _problem(HTTPStatus.CONFLICT, str(error), reason="name_changed")
    except NotRecreatable as error:
        return _problem(HTTPStatus.CONFLICT, str(error), blockers=list(error.blockers))
    except RecreateFailed as error:
        status = HTTPStatus.SERVICE_UNAVAILABLE if error.outcome_unknown else HTTPStatus.BAD_GATEWAY
        return _problem(status, str(error))
    except PullFailed as error:
        return _problem(HTTPStatus.BAD_GATEWAY, str(error))
    except ObjectTooLarge as error:
        return _problem(HTTPStatus.SERVICE_UNAVAILABLE, str(error))
    return _recreate_model(outcome)
