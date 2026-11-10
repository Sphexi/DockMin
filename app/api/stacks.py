"""JSON routes for Compose stacks, authenticated by bearer token only."""

from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import asdict
from http import HTTPStatus
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from starlette.types import Message, Receive

from app.api.containers import MAX_CURSOR, CredentialChoiceModel, EnvironmentId, Token
from app.auth.errors import ProductError, ResourceNotFound, SelfProtected, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.services.assembly import Stacks, StackServices
from app.services.containers import ContainerListTooLarge
from app.services.host_objects import NetworkListTooLarge, VolumeListTooLarge
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectBusy
from app.services.pull_credentials import CredentialChoice
from app.services.stack_deploy import (
    ComposeNotReady,
    PreexistingObjects,
    StackBusy,
    StackError,
    StackOperationFailed,
    StackRejected,
)
from app.services.stack_document import (
    FILE_NAME,
    MAX_FILES,
    MAX_VARIABLES,
    STACK_NAME,
    CompanionFile,
    VariableInput,
)
from app.services.stack_jobs import TooManyStackJobs
from app.services.stack_queries import DEFAULT_PAGE_SIZE, MAX_NAME_CHARS
from app.services.stack_views import (
    MAX_CHOICES,
    FileView,
    StackDetail,
    StackOutcome,
    StackRow,
    VariableView,
    read_intake,
)

MAX_BODY_BYTES: Final = 256 * 1024
MAX_REFERENCE: Final = 512
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
CHOICE_REASONS: Final = frozenset({"credential_choice_required", "credential_unavailable"})
CONFLICT_REASONS: Final = frozenset(
    {"container_name_in_use", "port_in_use", "not_running", "file_in_use", "file_missing"}
)
DIGEST_PATTERN: Final = r"^[0-9a-f]{64}$"
OPTIONAL_DIGEST_PATTERN: Final = r"^([0-9a-f]{64})?$"
FIELD_ROW_PREFIX: Final = "variable-"
BODY_FIELDS: Final = frozenset(
    {"name", "compose_file", "variables", "expected_containers", "files", "content"}
)

type Handler = Callable[[Request], Coroutine[Any, Any, Response]]
type Refusal = tuple[int, str, dict[str, object]]


def _problem(status: int, message: str, **extra: object) -> JSONResponse:
    body = {"error": {"status": status, "message": message, **extra}}
    return JSONResponse(body, status_code=status)


def _rejection(error: ValidationFailed) -> JSONResponse:
    fields = [{"loc": ["query", name], "msg": text} for name, text in error.fields.items()]
    return _problem(HTTPStatus.BAD_REQUEST, HTTPStatus.BAD_REQUEST.phrase, fields=fields)


def _capped(receive: Receive) -> Receive:
    received = 0

    async def limited() -> Message:
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > MAX_BODY_BYTES:
                raise HTTPException(status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        return message

    return limited


class StackRoute(APIRoute):
    """Refuses a body over the cap while it is read, and answers query errors the services raise."""

    def get_route_handler(self) -> Handler:
        handle = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            if request.method not in SAFE_METHODS:
                declared = request.headers.get("content-length", "")
                if declared.isascii() and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                    raise HTTPException(status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                request = Request(request.scope, _capped(request.receive))
            try:
                return await handle(request)
            except ValidationFailed as error:
                return _rejection(error)

        return guarded


router = APIRouter(
    prefix="/api/v1/environments/{environment_id}/stacks",
    tags=["stacks"],
    route_class=StackRoute,
)


def _stack_name(name: Annotated[str, Path()]) -> str:
    if STACK_NAME.fullmatch(name) is None:
        raise ResourceNotFound("not found")
    return name


def _file_name(file: Annotated[str, Path()]) -> str:
    if FILE_NAME.fullmatch(file) is None:
        raise ResourceNotFound("not found")
    return file


StackName = Annotated[str, Depends(_stack_name)]
FileName = Annotated[str, Depends(_file_name)]
ImageReference = Annotated[str, StringConstraints(min_length=1, max_length=MAX_REFERENCE)]


class StackModel(BaseModel):
    """A managed stack as listed."""

    name: str
    origin: str
    status: str
    last_operation: str | None
    last_outcome: str | None
    last_operation_at: str | None
    last_deployed_at: str | None
    changed_since_deploy: bool


class StackPageModel(BaseModel):
    """One page of stacks; next_cursor is null on the last page."""

    items: list[StackModel]
    next_cursor: str | None


class StackVariableModel(BaseModel):
    """A variable by name; the value of a secret is always null."""

    name: str
    secret: bool
    value: str | None


class StackFileUseModel(BaseModel):
    """A place a stack's documents name a file: the Compose path and how the file is read."""

    path: str
    role: str
    document: str | None


class StackFileModel(BaseModel):
    """A companion file by name; size, digest and content of a secret file are always null."""

    name: str
    secret: bool
    size: int | None
    digest: str | None
    content: str | None
    used_as: list[StackFileUseModel]


class StackContainerModel(BaseModel):
    """A container of a service, listed only when the caller may see it."""

    id: str
    name: str
    state: str
    status: str
    image: str


class StackServiceModel(BaseModel):
    """A service with the containers the caller may see; hidden counts the rest."""

    name: str
    image: str | None
    declared: bool
    containers: list[StackContainerModel]
    hidden: int


class StackDetailModel(StackModel):
    """A stack with its file, variables and services.

    The services list only what the file declares when containers_available is false.
    """

    compose_file: str
    compose_file_digest: str
    deployed_file_digest: str | None
    inputs_digest: str | None
    deployed_inputs_digest: str | None
    resolved_digest: str | None
    variables: list[StackVariableModel]
    services: list[StackServiceModel]
    hidden_containers: int
    containers_available: bool
    can_write: bool
    files: list[StackFileModel]


class StackServiceStateModel(BaseModel):
    """The containers of one service after an operation."""

    service: str
    containers: int
    running: int


class StackOutcomeModel(BaseModel):
    """What an operation did; message is product text and carries no Compose output."""

    operation: str
    message: str
    status: str | None
    events: int
    exit_code: int | None
    pulled: list[str]
    services: list[StackServiceStateModel]
    services_running: int
    orphans_removed: int
    preexisting_after_deploy: list[str]
    interrupted: bool


class StackActionModel(BaseModel):
    """The stack an operation ran on, what it did, and the collision warnings it raised."""

    name: str
    outcome: StackOutcomeModel
    warnings: list[str]


class StackVariableRequest(BaseModel):
    """A variable to set; a secret with no value keeps the value already stored."""

    model_config = ConfigDict(extra="forbid")

    name: str
    value: str | None = None
    secret: bool = False

    @model_validator(mode="after")
    def _plain_values_are_given(self) -> "StackVariableRequest":
        if not self.secret and self.value is None:
            raise ValueError("a variable that is not secret needs a value")
        return self


class StackFileEntryRequest(BaseModel):
    """A companion file to add with a new stack."""

    model_config = ConfigDict(extra="forbid")

    name: str
    content: str
    secret: bool = False


class StackFileRequest(BaseModel):
    """A companion file to add or replace.

    A null content keeps the value of a secret file that is already stored. A given
    expected_inputs_digest binds the write to the stack's inputs digest from the detail, the
    empty text standing for a null one; null leaves the write unbound.
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None
    secret: bool = False
    expected_inputs_digest: str | None = Field(default=None, pattern=OPTIONAL_DIGEST_PATTERN)


type Choices = dict[ImageReference, CredentialChoiceModel]
type Variables = list[StackVariableRequest]
type Files = list[StackFileEntryRequest]


class StackCreateRequest(BaseModel):
    """A new stack's name, file and variables, and the credential sets to pull its images with."""

    model_config = ConfigDict(extra="forbid")

    name: str
    compose_file: str
    variables: Annotated[Variables, Field(max_length=MAX_VARIABLES)] = Field(default_factory=list)
    files: Annotated[Files, Field(max_length=MAX_FILES)] = Field(default_factory=list)
    credential_choices: Annotated[Choices, Field(max_length=MAX_CHOICES)] = Field(
        default_factory=dict
    )


class StackRedeployRequest(BaseModel):
    """What the caller reviewed, and the changes to deploy.

    A null compose_file or variables keeps what is stored; a given variables list replaces the
    stored set. The new file and variables are stored only when the redeploy succeeds.
    """

    model_config = ConfigDict(extra="forbid")

    expected_file_digest: str = Field(pattern=DIGEST_PATTERN)
    expected_inputs_digest: str | None = Field(pattern=DIGEST_PATTERN)
    expected_containers: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    compose_file: str | None = None
    variables: Annotated[Variables, Field(max_length=MAX_VARIABLES)] | None = None
    pull: bool = False
    remove_orphans: bool = False
    credential_choices: Annotated[Choices, Field(max_length=MAX_CHOICES)] = Field(
        default_factory=dict
    )


class StackRestartRequest(BaseModel):
    """Optionally binds the restart to the containers a confirmation showed."""

    model_config = ConfigDict(extra="forbid")

    expected_containers: str | None = Field(default=None, pattern=DIGEST_PATTERN)


def _inputs(items: Variables | None) -> tuple[VariableInput, ...] | None:
    if items is None:
        return None
    return tuple(
        VariableInput(item.name, item.value, item.secret, False, row)
        for row, item in enumerate(items)
    )


def _companions(items: Files) -> tuple[CompanionFile, ...]:
    return tuple(CompanionFile(item.name, item.content, item.secret) for item in items)


def _choices(given: Choices) -> dict[str, CredentialChoice]:
    return {reference: choice.choice() for reference, choice in given.items()}


def _stack(row: StackRow) -> StackModel:
    return StackModel(
        name=row.name,
        origin=row.origin,
        status=row.status,
        last_operation=row.last_operation,
        last_outcome=row.last_outcome,
        last_operation_at=row.last_operation_at,
        last_deployed_at=row.last_deployed_at,
        changed_since_deploy=row.changed_since_deploy,
    )


def _variable(variable: VariableView) -> StackVariableModel:
    return StackVariableModel(
        name=variable.name,
        secret=variable.secret,
        value=None if variable.secret else variable.value,
    )


def _file(view: FileView) -> StackFileModel:
    return StackFileModel(
        name=view.name,
        secret=view.secret,
        size=None if view.secret else view.size,
        digest=None if view.secret else view.digest,
        content=None if view.secret else view.content,
        used_as=[
            StackFileUseModel(path=use.path, role=use.role, document=use.document)
            for use in view.uses
        ],
    )


def _detail(detail: StackDetail) -> StackDetailModel:
    return StackDetailModel(
        **_stack(detail.row).model_dump(),
        compose_file=detail.compose_file,
        compose_file_digest=detail.compose_file_digest,
        deployed_file_digest=detail.deployed_file_digest,
        inputs_digest=detail.inputs_digest,
        deployed_inputs_digest=detail.deployed_inputs_digest,
        resolved_digest=detail.resolved_digest,
        variables=[_variable(variable) for variable in detail.variables],
        services=[
            StackServiceModel(
                name=service.name,
                image=service.image,
                declared=service.declared,
                containers=[
                    StackContainerModel(
                        id=container.id,
                        name=container.name,
                        state=container.state,
                        status=container.status,
                        image=container.image,
                    )
                    for container in service.containers
                ],
                hidden=service.hidden,
            )
            for service in detail.services
        ],
        hidden_containers=detail.hidden_containers,
        containers_available=detail.containers_available,
        can_write=detail.can_write,
        files=[_file(view) for view in detail.files],
    )


def _action(outcome: StackOutcome, earlier: Sequence[str] = ()) -> StackActionModel:
    return StackActionModel(
        name=outcome.name,
        warnings=list(dict.fromkeys((*earlier, *outcome.warnings))),
        outcome=StackOutcomeModel(
            operation=outcome.operation,
            message=outcome.message,
            status=outcome.status,
            events=outcome.events,
            exit_code=outcome.exit_code,
            pulled=list(outcome.pulled),
            services=[
                StackServiceStateModel(
                    service=state.service, containers=state.containers, running=state.running
                )
                for state in outcome.services
            ],
            services_running=outcome.services_running,
            orphans_removed=outcome.orphans_removed,
            preexisting_after_deploy=list(outcome.preexisting_after_deploy),
            interrupted=outcome.interrupted,
        ),
    )


def _location(key: str) -> list[str | int]:
    if key in BODY_FIELDS:
        return ["body", key]
    if key.startswith(FIELD_ROW_PREFIX):
        row, _, part = key.removeprefix(FIELD_ROW_PREFIX).partition("-")
        if row.isascii() and row.isdigit() and part in {"name", "value"}:
            return ["body", "variables", int(row), part]
    if key == "stack":
        return []
    if key == "file":
        return ["path", "file"]
    return ["compose", key]


async def _references(
    error: StackRejected, principal: Principal, services: StackServices, environment_id: int
) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for reference, message in error.fields.items():
        try:
            offers = await services.queries.credential_offers(
                principal, environment_id, [reference]
            )
        except ProductError:
            offers = ()
        candidates = [
            {"id": candidate.id, "name": candidate.name}
            for offer in offers
            for candidate in offer.candidates
        ]
        found.append({"reference": reference, "message": message, "candidates": candidates})
    return found


async def _rejected(
    error: StackRejected, principal: Principal, services: StackServices, environment_id: int
) -> Refusal:
    if error.reason in CHOICE_REASONS:
        references = await _references(error, principal, services, environment_id)
        return HTTPStatus.CONFLICT, str(error), {"reason": error.reason, "references": references}
    if error.reason == "secrets_unreadable":
        return HTTPStatus.CONFLICT, str(error), {"reason": error.reason}
    extra: dict[str, object] = {
        "reason": error.reason,
        "fields": [{"loc": _location(key), "msg": text} for key, text in error.fields.items()],
    }
    if error.findings:
        extra["findings"] = [asdict(finding) for finding in error.findings]
    if error.stderr:
        extra["stderr"] = error.stderr
    if error.warnings:
        extra["warnings"] = list(error.warnings)
    if error.reason == "restricted":
        status = HTTPStatus.FORBIDDEN
    elif error.reason in CONFLICT_REASONS:
        status = HTTPStatus.CONFLICT
    else:
        status = HTTPStatus.BAD_REQUEST
    return status, status.phrase, extra


def _failed(error: StackOperationFailed) -> Refusal:
    status = (
        HTTPStatus.SERVICE_UNAVAILABLE
        if error.reason == "outcome_unknown"
        else HTTPStatus.BAD_GATEWAY
    )
    extra: dict[str, object] = {
        "reason": error.reason,
        "exit_code": error.exit_code,
        "events": error.event_count,
        "progress": error.progress,
    }
    if error.stderr:
        extra["stderr"] = error.stderr
    return status, str(error), extra


async def _refusal(
    error: ProductError, principal: Principal, services: StackServices, environment_id: int
) -> Refusal | None:
    match error:
        case ObjectBusy():
            holder = asdict(services.actions.describe_busy(error, principal))
            return HTTPStatus.CONFLICT, str(error), {"reason": "object_busy", "holder": holder}
        case StackRejected():
            return await _rejected(error, principal, services, environment_id)
        case StackOperationFailed():
            return _failed(error)
        case ComposeNotReady():
            extra: dict[str, object] = {
                "reason": error.reason,
                "installed": error.installed,
                "required": error.required,
            }
            return HTTPStatus.SERVICE_UNAVAILABLE, str(error), extra
        case SelfProtected():
            return HTTPStatus.CONFLICT, str(error), {"reason": error.reason}
        case StackBusy():
            return HTTPStatus.SERVICE_UNAVAILABLE, str(error), {"reason": error.reason}
        case PreexistingObjects():
            extra = {"reason": error.reason, "objects": list(error.objects)}
            return HTTPStatus.CONFLICT, str(error), extra
        case StackError():
            return HTTPStatus.CONFLICT, str(error), {"reason": error.reason}
        case EnvironmentUnavailable():
            return HTTPStatus.SERVICE_UNAVAILABLE, str(error), {"reason": error.kind}
        case TooManyStackJobs():
            return HTTPStatus.TOO_MANY_REQUESTS, str(error), {"reason": "too_many_operations"}
        case (
            ContainerListTooLarge()
            | NetworkListTooLarge()
            | VolumeListTooLarge()
            | ObjectTooLarge()
        ):
            return HTTPStatus.SERVICE_UNAVAILABLE, str(error), {}
        case _:
            return None


async def _attempt[T](
    call: Callable[[], Awaitable[T]],
    principal: Principal,
    services: StackServices,
    environment_id: int,
    created: list[str] | None = None,
) -> T | JSONResponse:
    """Run a service call and answer the refusals it raises; any other error propagates.

    A name in `created` marks a stack that exists although the call failed, and the answer says so.
    """
    try:
        return await call()
    except ProductError as error:
        refusal = await _refusal(error, principal, services, environment_id)
        if refusal is None:
            raise
        status, message, extra = refusal
        if created:
            extra = {**extra, "stack_created": True, "stack": created[0]}
        return _problem(status, message, **extra)


def _responses(*statuses: HTTPStatus) -> dict[int | str, dict[str, Any]]:
    return {status: {"description": status.phrase} for status in statuses}


_READ_RESPONSES = _responses(
    HTTPStatus.BAD_REQUEST,
    HTTPStatus.UNAUTHORIZED,
    HTTPStatus.FORBIDDEN,
    HTTPStatus.NOT_FOUND,
    HTTPStatus.SERVICE_UNAVAILABLE,
)
_WRITE_RESPONSES = _responses(
    HTTPStatus.BAD_REQUEST,
    HTTPStatus.UNAUTHORIZED,
    HTTPStatus.FORBIDDEN,
    HTTPStatus.NOT_FOUND,
    HTTPStatus.CONFLICT,
    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
    HTTPStatus.UNPROCESSABLE_ENTITY,
    HTTPStatus.TOO_MANY_REQUESTS,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.SERVICE_UNAVAILABLE,
)


@router.get("", response_model=StackPageModel, responses=_READ_RESPONSES)
async def list_stacks(
    principal: Token,
    environment_id: EnvironmentId,
    services: Stacks,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR)] = None,
    q: Annotated[str | None, Query(max_length=MAX_NAME_CHARS)] = None,
) -> StackPageModel | JSONResponse:
    """List the managed stacks the token's user may see, by name."""

    async def read() -> StackPageModel:
        page = await services.queries.page(
            principal, environment_id, limit=limit, cursor=cursor, q=q, include_unmanaged=False
        )
        return StackPageModel(
            items=[_stack(row) for row in page.items], next_cursor=page.next_cursor
        )

    return await _attempt(read, principal, services, environment_id)


@router.get("/{name}", response_model=StackDetailModel, responses=_READ_RESPONSES)
async def get_stack(
    principal: Token,
    environment_id: EnvironmentId,
    name: StackName,
    services: Stacks,
) -> StackDetailModel | JSONResponse:
    """Return one stack by name; a stack the user may not see is not found."""

    async def read() -> StackDetailModel:
        return _detail(await services.queries.detail(principal, environment_id, name))

    return await _attempt(read, principal, services, environment_id)


@router.post(
    "",
    status_code=HTTPStatus.CREATED,
    response_model=StackActionModel,
    responses=_WRITE_RESPONSES,
)
async def create_stack(
    principal: Token,
    environment_id: EnvironmentId,
    body: StackCreateRequest,
    services: Stacks,
) -> StackActionModel | JSONResponse:
    """Create a stack and deploy it, answering when the deployment has finished.

    Nothing is created while a credential choice is outstanding (409 with the references that
    need one). A stack whose deployment fails exists afterwards with status `error`, and the
    failure answer says `stack_created`.
    """
    created: list[str] = []

    async def make() -> StackActionModel:
        text = read_intake(pasted=body.compose_file, uploaded=None)
        choices = _choices(body.credential_choices)
        with services.jobs.reserve(principal):
            made = await services.actions.create(
                principal,
                environment_id,
                name=body.name,
                compose_text=text,
                variables=_inputs(body.variables),
                choices=choices,
                resolve_pulls=True,
                check_collisions=True,
                files=_companions(body.files),
                allow_builds=False,
            )
            created.append(made.name)
            outcome = await services.actions.deploy(
                principal,
                environment_id,
                made.name,
                choices=choices,
                check_collisions=True,
                allow_builds=False,
            )
        return _action(outcome, made.warnings)

    return await _attempt(make, principal, services, environment_id, created)


@router.post("/{name}/redeploy", response_model=StackActionModel, responses=_WRITE_RESPONSES)
async def redeploy_stack(
    principal: Token,
    environment_id: EnvironmentId,
    name: StackName,
    body: StackRedeployRequest,
    services: Stacks,
) -> StackActionModel | JSONResponse:
    """Redeploy a stack the caller reviewed, answering when the deployment has finished.

    The stored file must still have `expected_file_digest`, the stored variables and files
    `expected_inputs_digest` (null when the detail returned null), and when `expected_containers`
    is given the project's containers must still have that digest; otherwise nothing runs (409
    `changed_since_review`). `remove_orphans` requires `expected_containers`. Changing the file
    or variables, pulling again and removing services no longer in the file are chosen in the
    body.
    """

    async def deploy() -> StackActionModel:
        text = (
            None
            if body.compose_file is None
            else read_intake(pasted=body.compose_file, uploaded=None)
        )
        with services.jobs.reserve(principal):
            outcome = await services.actions.redeploy(
                principal,
                environment_id,
                name,
                expected_file_digest=body.expected_file_digest,
                expected_inputs_digest=body.expected_inputs_digest or "",
                expected_containers=body.expected_containers,
                pull=body.pull,
                remove_orphans=body.remove_orphans,
                choices=_choices(body.credential_choices),
                compose_text=text,
                variables=_inputs(body.variables),
                check_collisions=True,
                allow_builds=False,
            )
        return _action(outcome)

    return await _attempt(deploy, principal, services, environment_id)


@router.post("/{name}/start", response_model=StackActionModel, responses=_WRITE_RESPONSES)
async def start_stack(
    principal: Token,
    environment_id: EnvironmentId,
    name: StackName,
    services: Stacks,
) -> StackActionModel | JSONResponse:
    """Start the containers of a deployed stack."""

    async def start() -> StackActionModel:
        with services.jobs.reserve(principal):
            return _action(await services.actions.start(principal, environment_id, name))

    return await _attempt(start, principal, services, environment_id)


@router.post("/{name}/stop", response_model=StackActionModel, responses=_WRITE_RESPONSES)
async def stop_stack(
    principal: Token,
    environment_id: EnvironmentId,
    name: StackName,
    services: Stacks,
) -> StackActionModel | JSONResponse:
    """Stop the containers of a deployed stack."""

    async def stop() -> StackActionModel:
        with services.jobs.reserve(principal):
            return _action(await services.actions.stop(principal, environment_id, name))

    return await _attempt(stop, principal, services, environment_id)


@router.post("/{name}/restart", response_model=StackActionModel, responses=_WRITE_RESPONSES)
async def restart_stack(
    principal: Token,
    environment_id: EnvironmentId,
    name: StackName,
    services: Stacks,
    body: StackRestartRequest | None = None,
) -> StackActionModel | JSONResponse:
    """Stop the running services of a deployed stack and start them again.

    Services that are stopped stay stopped. When `expected_containers` is given, the project's
    containers must still have that digest; otherwise nothing runs (409 `changed_since_review`).
    """

    async def restart() -> StackActionModel:
        with services.jobs.reserve(principal):
            return _action(
                await services.actions.restart(
                    principal,
                    environment_id,
                    name,
                    expected_containers=body.expected_containers if body else None,
                )
            )

    return await _attempt(restart, principal, services, environment_id)


@router.put("/{name}/files/{file}", response_model=StackFileModel, responses=_WRITE_RESPONSES)
async def put_stack_file(
    principal: Token,
    environment_id: EnvironmentId,
    name: StackName,
    file: FileName,
    body: StackFileRequest,
    services: Stacks,
) -> StackFileModel | JSONResponse:
    """Add a companion file or replace one; the stack is checked with it and nothing is deployed.

    A null `content` keeps the value of a secret file already stored. The write is refused (409
    `changed_since_review`) when `expected_inputs_digest` is given and no longer matches the
    stack's, and (400) when the stack would no longer pass the checks with the file.
    """

    async def write() -> StackFileModel:
        try:
            view = await services.actions.write_file(
                principal,
                environment_id,
                name,
                file,
                content=body.content,
                secret=body.secret,
                expected_inputs_digest=body.expected_inputs_digest,
            )
        except StackRejected as rejected:
            if "name" in rejected.fields:
                rejected.fields = {
                    ("file" if key == "name" else key): text
                    for key, text in rejected.fields.items()
                }
            raise
        return _file(view)

    return await _attempt(write, principal, services, environment_id)


@router.delete(
    "/{name}/files/{file}",
    status_code=HTTPStatus.NO_CONTENT,
    response_class=Response,
    responses=_WRITE_RESPONSES,
)
async def delete_stack_file(
    principal: Token,
    environment_id: EnvironmentId,
    name: StackName,
    file: FileName,
    services: Stacks,
    expected_inputs_digest: Annotated[str | None, Query(pattern=OPTIONAL_DIGEST_PATTERN)] = None,
) -> Response:
    """Delete a companion file while no document names it; nothing is deployed.

    409 `file_in_use` while a document references the file, and 409 `changed_since_review` when
    `expected_inputs_digest` is given and no longer matches the stack's.
    """

    async def remove() -> Response:
        await services.actions.remove_file(
            principal, environment_id, name, file, expected_inputs_digest=expected_inputs_digest
        )
        return Response(status_code=HTTPStatus.NO_CONTENT)

    return await _attempt(remove, principal, services, environment_id)
