"""Stack import pages for Compose and Swarm stacks: upload, review, passphrase, import, result."""

import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import require_administrator_or_not_found, require_user
from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.services.assembly import Environments, Stacks, StackServices, start_stack_job
from app.services.object_inspector import EnvironmentUnavailable
from app.services.object_locks import ObjectBusy
from app.services.pull_credentials import CredentialChoice
from app.services.stack_deploy import (
    ChangedSinceReview,
    PreexistingObjects,
    StackError,
    StackNameInUse,
    StackRejected,
)
from app.services.stack_import import (
    ImportBlocked,
    ImportReview,
    StackImportChanged,
    StackImported,
    StackImportFailed,
    StackUploadGone,
    UnlockFailed,
    UnlockLimited,
)
from app.services.stack_import_assembly import StackImports
from app.services.stack_import_plan import ImportChoices
from app.services.stack_jobs import TooManyStackJobs
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.image_helpers import credential_choice_of
from app.web.object_pages import (
    Failure,
    busy_message,
    page_response,
    problem_page,
    redirect,
    route,
)
from app.web.problems import unreachable_copy
from app.web.registry_providers import CATALOGUE
from app.web.restore_upload import read_archive_upload
from app.web.stack_pages import OfferChoice
from app.web.stack_routes import problem_of
from app.web.swarm_routes import swarm_router

UPLOAD_ID_PATTERN: Final = re.compile(r"[0-9a-f]{32}")
FIELD: Final = re.compile(
    r"(?P<group>path|credential|secret)-(?P<index>[0-9]{1,3})-(?P<part>source|choice|reference|name|value)"
)
SCALARS: Final = frozenset({"name", "action", "expected_plan"})
MAX_FIELDS: Final = 400
MAX_NAME_FIELD: Final = 512
MAX_VALUE_FIELD: Final = 70_000
HEADING: Final = "Import stack"
CANCELLED: Final = "The upload was discarded. Nothing was changed."
OPENED: Final = "The sealed values were opened."
MAX_FLASHED: Final = 3

type Kind = Literal["compose", "swarm"]


@dataclass(frozen=True, slots=True)
class _Names:
    """The route names and templates of one stack kind."""

    kind: Kind
    form: str
    upload: str
    review: str
    run: str
    unlock: str
    cancel: str
    list: str
    detail: str
    job: str
    label: str


COMPOSE: Final = _Names(
    "compose",
    "environment_stack_import_form",
    "environment_stack_import_upload",
    "environment_stack_import_review",
    "environment_stack_import_run",
    "environment_stack_import_unlock",
    "environment_stack_import_cancel",
    "environment_stack_list",
    "environment_stack_detail",
    "environment_stack_job",
    "Compose stack",
)
SWARM: Final = _Names(
    "swarm",
    "environment_swarm_stack_import_form",
    "environment_swarm_stack_import_upload",
    "environment_swarm_stack_import_review",
    "environment_swarm_stack_import_run",
    "environment_swarm_stack_import_unlock",
    "environment_swarm_stack_import_cancel",
    "environment_swarm_stack_list",
    "environment_swarm_stack_detail",
    "environment_swarm_stack_job",
    "Swarm stack",
)

router = APIRouter(
    prefix="/environments/{environment_id}/stacks",
    tags=["stacks"],
    dependencies=[Depends(require_user)],
)
swarm = swarm_router()
Administrator = Annotated[Principal, Depends(require_administrator_or_not_found)]


def _upload_id(upload_id: Annotated[str, Path(max_length=64)]) -> str:
    if UPLOAD_ID_PATTERN.fullmatch(upload_id) is None:
        raise ResourceNotFound("not found")
    return upload_id


UploadId = Annotated[str, Depends(_upload_id)]


def _session_id(actor: Principal) -> int:
    if actor.session_id is None:
        raise HTTPException(status_code=403)
    return actor.session_id


@dataclass(frozen=True, slots=True)
class _Posted:
    """The posted review form: the choices, the action and the raw rows to show again."""

    choices: ImportChoices
    action: str
    expected_plan: str
    chosen: dict[str, str]
    errors: dict[str, str]


async def _read_form(request: Request, fallback: str) -> _Posted:
    posted = await request.form()
    scalars: dict[str, str] = {}
    paths: dict[int, dict[str, str]] = {}
    credentials: dict[int, dict[str, str]] = {}
    secrets: dict[int, dict[str, str]] = {}
    for count, (key, value) in enumerate(posted.multi_items()):
        if count >= MAX_FIELDS:
            break
        if not isinstance(value, str):
            continue
        if key in SCALARS:
            scalars[key] = value
        elif (found := FIELD.fullmatch(key)) is not None:
            group = {"path": paths, "credential": credentials, "secret": secrets}[found["group"]]
            group.setdefault(int(found["index"]), {})[found["part"]] = value
    errors: dict[str, str] = {}
    host_paths = {
        row["source"]: row.get("choice", "")
        for row in paths.values()
        if row.get("source") and row.get("choice")
    }
    chosen: dict[str, str] = {}
    made: dict[str, CredentialChoice] = {}
    for row in credentials.values():
        reference = row.get("reference", "").strip()
        if not reference:
            continue
        chosen[reference] = row.get("choice", "")
        try:
            choice = credential_choice_of(row.get("choice", ""))
        except ValidationFailed as invalid:
            errors[reference] = "; ".join(invalid.fields.values())
            continue
        if choice is not None:
            made[reference] = choice
    entered = {
        row["name"]: row["value"][:MAX_VALUE_FIELD]
        for row in secrets.values()
        if row.get("name") and row.get("value")
    }
    name = scalars.get("name", fallback)[:MAX_NAME_FIELD]
    return _Posted(
        ImportChoices(name=name, host_paths=host_paths, credentials=made, entered=entered),
        scalars.get("action", ""),
        scalars.get("expected_plan", "")[:128],
        chosen,
        errors,
    )


def _failure_of(error: Exception, stacks: StackServices, actor: Principal) -> Failure:
    if isinstance(error, StackUploadGone):
        return Failure(409, "The upload is gone", str(error))
    if isinstance(error, StackImportFailed):
        return Failure(502, "The import failed", str(error))
    if isinstance(error, ObjectBusy):
        busy = stacks.queries.describe_busy(error, actor)
        return Failure(409, "This stack is busy", busy_message(busy, "the stack"))
    if isinstance(error, ChangedSinceReview | StackNameInUse | PreexistingObjects):
        return Failure(409, "Names are in use or changed", str(error))
    if isinstance(error, TooManyStackJobs):
        return Failure(429, "Too many operations are running", str(error))
    if isinstance(error, EnvironmentUnavailable):
        return Failure(503, *unreachable_copy(error))
    if isinstance(error, StackRejected):
        found = problem_of(error, stacks, actor)
        rejection = found.rejection
        detail = "; ".join(
            [
                *found.errors.values(),
                *(text for _, text in (rejection.problems if rejection else ())),
            ]
        )
        title = rejection.title if rejection is not None else "The stack file is refused"
        return Failure(found.status, title, detail or str(error))
    if isinstance(error, StackError):
        return problem_of(error, stacks, actor).failure or Failure(
            400, "Check the request", str(error)
        )
    return Failure(503, *unreachable_copy(error))


def _register(target: APIRouter, names: _Names, base: str) -> None:
    """Add the import routes of one stack kind to its router; `base` is the path below it."""
    kind = names.kind

    def href(request: Request, name: str, environment_id: int, **params: Any) -> str:
        return route(request, name, environment_id=environment_id, **params)

    def problem(
        request: Request,
        stacks: StackServices,
        error: Exception,
        actor: Principal,
        environment_id: int,
        review_href: str | None = None,
    ) -> Response:
        gone = isinstance(error, StackUploadGone)
        return problem_page(
            request,
            heading=HEADING,
            failure=_failure_of(error, stacks, actor),
            back_href=href(request, names.form if gone else names.list, environment_id),
            back_label="Upload the archive again" if gone else "Back to stacks",
            review_href=None if gone else review_href,
            region=None,
        )

    async def upload_page(
        request: Request,
        environments: Environments,
        actor: Principal,
        environment_id: int,
        *,
        errors: Mapping[str, str] | None = None,
        status_code: int = 200,
    ) -> Response:
        environment = (await environments.environments.get(actor, environment_id)).summary
        return page_response(
            request,
            "stacks/import_upload.html",
            {
                "environment": environment,
                "kind_label": names.label,
                "errors": errors or {},
                "post_href": href(request, names.upload, environment_id),
                "list_href": href(request, names.list, environment_id),
            },
            status_code,
        )

    async def review_page(
        request: Request,
        stacks: StackServices,
        actor: Principal,
        review: ImportReview,
        posted: _Posted | None,
        errors: Mapping[str, str],
        reference_errors: Mapping[str, str],
        status_code: int = 200,
    ) -> Response:
        chosen = posted.chosen if posted is not None else {}
        return page_response(
            request,
            "stacks/import_review.html",
            {
                "review": review,
                "plan": review.plan,
                "kind_label": names.label,
                "errors": errors,
                "entered": sorted(posted.choices.entered) if posted is not None else [],
                "offers": await _offers(stacks, actor, review, chosen, reference_errors),
                "catalogue": CATALOGUE,
                "post_href": href(
                    request,
                    names.run,
                    review.environment_id,
                    upload_id=review.upload_id,
                ),
                "unlock_href": href(
                    request,
                    names.unlock,
                    review.environment_id,
                    upload_id=review.upload_id,
                ),
                "cancel_href": href(
                    request,
                    names.cancel,
                    review.environment_id,
                    upload_id=review.upload_id,
                ),
                "list_href": href(request, names.list, review.environment_id),
            },
            status_code,
        )

    async def show_review(
        request: Request,
        stacks: StackServices,
        imports: Any,
        actor: Principal,
        environment_id: int,
        upload_id: str,
        posted: _Posted | None = None,
        errors: Mapping[str, str] | None = None,
        reference_errors: Mapping[str, str] | None = None,
        status_code: int = 200,
        notices: Mapping[str, str] | None = None,
    ) -> Response:
        try:
            review = await imports.review(
                actor,
                _session_id(actor),
                environment_id,
                kind,
                upload_id,
                None if posted is None else posted.choices,
            )
        except (
            StackError,
            StackRejected,
            StackUploadGone,
            EnvironmentUnavailable,
            ObjectBusy,
        ) as error:
            return problem(request, stacks, error, actor, environment_id)
        merged = {**(errors or {}), **(posted.errors if posted is not None else {})}
        return await review_page(
            request,
            stacks,
            actor,
            review,
            posted,
            {**merged, **(notices or {})},
            {**merged, **(reference_errors or {})},
            status_code,
        )

    @target.get(f"{base}/-/import", response_class=HTMLResponse, name=names.form)
    async def import_form(
        request: Request, environments: Environments, actor: Administrator, environment_id: PathId
    ) -> Response:
        """Show the archive upload form; administrators only."""
        return await upload_page(request, environments, actor, environment_id)

    @target.post(f"{base}/-/import", response_class=HTMLResponse, name=names.upload)
    async def upload_archive(
        request: Request,
        imports: StackImports,
        stacks: Stacks,
        environments: Environments,
        actor: Administrator,
        environment_id: PathId,
    ) -> Response:
        """Receive the archive as a stream, validate it and redirect to its review.

        The first part of the body is the CSRF token; no file byte is read before it is checked.
        """
        session_id = _session_id(actor)
        declared = request.headers.get("content-length", "")

        async def receive(parts: AsyncIterator[bytes]) -> str:
            return await imports.receive(
                actor,
                session_id,
                environment_id,
                kind,
                parts,
                declared_size=int(declared) if declared.isdigit() else None,
            )

        try:
            upload_id = await read_archive_upload(request, receive)
        except ValidationFailed as invalid:
            return await upload_page(
                request, environments, actor, environment_id, errors=invalid.fields, status_code=422
            )
        except (EnvironmentUnavailable, ObjectBusy, StackError) as error:
            return problem(request, stacks, error, actor, environment_id)
        return redirect(
            request,
            href(request, names.review, environment_id, upload_id=upload_id),
        )

    @target.get(f"{base}/-/import/{{upload_id}}", response_class=HTMLResponse, name=names.review)
    async def review_import(
        request: Request,
        imports: StackImports,
        stacks: Stacks,
        actor: Administrator,
        environment_id: PathId,
        upload_id: UploadId,
    ) -> Response:
        """Name everything the import creates; nothing is changed."""
        return await show_review(request, stacks, imports, actor, environment_id, upload_id)

    @target.post(f"{base}/-/import/{{upload_id}}", response_class=HTMLResponse, name=names.run)
    async def run_import(
        request: Request,
        imports: StackImports,
        stacks: Stacks,
        actor: Administrator,
        environment_id: PathId,
        upload_id: UploadId,
    ) -> Response:
        """Check the choices again, or create the stack and start its deployment as a job."""
        posted = await _read_form(request, "")
        if posted.action != "import":
            return await show_review(
                request, stacks, imports, actor, environment_id, upload_id, posted=posted
            )
        review_href = href(request, names.review, environment_id, upload_id=upload_id)
        try:
            with stacks.jobs.reserve(actor):
                result = await imports.import_stack(
                    actor,
                    environment_id,
                    kind,
                    upload_id,
                    posted.choices,
                    posted.expected_plan,
                )
        except (StackImportChanged, ImportBlocked) as refused:
            text = (
                "; ".join(refused.fields.values())
                if isinstance(refused, ValidationFailed)
                else str(refused)
            )
            return await show_review(
                request,
                stacks,
                imports,
                actor,
                environment_id,
                upload_id,
                posted=posted,
                errors={"import": text},
                status_code=409,
            )
        except (StackNameInUse, PreexistingObjects):
            return await show_review(
                request,
                stacks,
                imports,
                actor,
                environment_id,
                upload_id,
                posted=posted,
                status_code=409,
            )
        except StackRejected as rejected:
            found = problem_of(rejected, stacks, actor)
            return await show_review(
                request,
                stacks,
                imports,
                actor,
                environment_id,
                upload_id,
                posted=posted,
                errors=dict(found.errors) or {"stack": "; ".join(rejected.fields.values())},
                reference_errors=dict(found.reference_errors),
                status_code=found.status,
            )
        except (
            StackError,
            StackUploadGone,
            StackImportFailed,
            ObjectBusy,
            EnvironmentUnavailable,
            TooManyStackJobs,
            ValidationFailed,
        ) as error:
            return problem(request, stacks, error, actor, environment_id, review_href)
        return _deploy(request, stacks, actor, environment_id, result, posted.choices, names)

    @target.post(
        f"{base}/-/import/{{upload_id}}/unlock", response_class=HTMLResponse, name=names.unlock
    )
    async def unlock_import(
        request: Request,
        imports: StackImports,
        stacks: Stacks,
        actor: Administrator,
        environment_id: PathId,
        upload_id: UploadId,
    ) -> Response:
        """Open the sealed values with the passphrase; the passphrase is shown again nowhere."""
        posted = await request.form()
        passphrase = posted.get("passphrase")
        try:
            await imports.unlock(
                actor,
                _session_id(actor),
                environment_id,
                kind,
                upload_id,
                passphrase if isinstance(passphrase, str) else "",
            )
        except UnlockFailed as failed:
            return await show_review(
                request,
                stacks,
                imports,
                actor,
                environment_id,
                upload_id,
                errors={"passphrase": str(failed)},
                status_code=422,
            )
        except UnlockLimited as limited:
            return await show_review(
                request,
                stacks,
                imports,
                actor,
                environment_id,
                upload_id,
                errors={"passphrase": str(limited)},
                status_code=429,
            )
        except ValidationFailed as invalid:
            return await show_review(
                request,
                stacks,
                imports,
                actor,
                environment_id,
                upload_id,
                errors=invalid.fields,
                status_code=422,
            )
        except (StackUploadGone, EnvironmentUnavailable, StackError) as error:
            return problem(request, stacks, error, actor, environment_id)
        return redirect(
            request,
            href(request, names.review, environment_id, upload_id=upload_id),
            [Flash("ok", OPENED)],
        )

    @target.post(
        f"{base}/-/import/{{upload_id}}/cancel", response_class=HTMLResponse, name=names.cancel
    )
    async def cancel_import(
        request: Request,
        imports: StackImports,
        stacks: Stacks,
        actor: Administrator,
        environment_id: PathId,
        upload_id: UploadId,
    ) -> Response:
        """Discard the held upload and remove its file."""
        try:
            await imports.cancel(actor, _session_id(actor), environment_id, kind, upload_id)
        except (EnvironmentUnavailable, StackError) as error:
            return problem(request, stacks, error, actor, environment_id)
        return redirect(
            request, href(request, names.list, environment_id), [Flash("ok", CANCELLED)]
        )


def _deploy(
    request: Request,
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    result: StackImported,
    choices: ImportChoices,
    names: _Names,
) -> Response:
    """Start the deployment of the stack that now exists and send the caller to its job page."""
    summary = (
        f"Imported {names.label.lower()} {result.name}: "
        f"{len(result.volumes)} volume{'' if len(result.volumes) == 1 else 's'} written, "
        f"{result.variables} variable{'' if result.variables == 1 else 's'}."
    )
    flashes = [
        Flash("ok", summary),
        *(Flash("warn", text) for text in result.warnings[:MAX_FLASHED]),
    ]
    name = result.name
    credentials = dict(choices.credentials)
    try:
        if names.kind == "compose":
            job_id = start_stack_job(
                request.app,
                actor,
                environment_id,
                "create",
                name,
                lambda services, progress: services.actions.deploy(
                    actor,
                    environment_id,
                    name,
                    choices=credentials,
                    progress=progress,
                    check_collisions=True,
                ),
            )
        else:
            job_id = start_stack_job(
                request.app,
                actor,
                environment_id,
                "create",
                name,
                lambda services, progress: services.swarm_actions.deploy(
                    actor, environment_id, name, sink=progress, choices=credentials
                ),
                stack_kind="swarm",
            )
    except TooManyStackJobs as busy:
        return page_response(
            request,
            "stacks/import_result.html",
            {
                "result": result,
                "kind_label": names.label,
                "reason": str(busy),
                "detail_href": route(
                    request,
                    names.detail,
                    environment_id=environment_id,
                    **{_DETAIL[names.kind]: name},
                ),
                "list_href": route(request, names.list, environment_id=environment_id),
            },
            429,
        )
    return redirect(
        request, route(request, names.job, environment_id=environment_id, job_id=job_id), flashes
    )


_DETAIL: Final = {"compose": "stack_name", "swarm": "namespace"}


async def _offers(
    stacks: StackServices,
    actor: Principal,
    review: ImportReview,
    posted: Mapping[str, str],
    errors: Mapping[str, str],
) -> tuple[OfferChoice, ...]:
    """Build the credential pickers, one per image to pull, with nothing chosen until asked."""
    references = list(review.pull_references)
    if not references:
        return ()
    offers = await stacks.queries.credential_pickers(actor, review.environment_id, references)
    shown: list[OfferChoice] = []
    for index, (reference, offer) in enumerate(zip(references, offers, strict=True)):
        valid = {"anonymous", *(str(candidate.id) for candidate in offer.candidates)}
        text = posted.get(reference, "").strip()
        shown.append(
            OfferChoice(
                index, reference, offer, text if text in valid else "", errors.get(reference)
            )
        )
    return tuple(shown)


_register(router, COMPOSE, "")
_register(swarm, SWARM, "/stacks")
