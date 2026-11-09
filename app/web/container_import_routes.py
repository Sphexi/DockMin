"""Container import pages: upload, review, passphrase, and the hand-off to the create form."""

import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import require_administrator_or_not_found, require_user
from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.services.assembly import Environments
from app.services.container_import import (
    ContainerImportFailed,
    ContainerImportReview,
    ContainerPrepared,
)
from app.services.container_import_assembly import ContainerImports
from app.services.container_import_plan import ContainerChoices
from app.services.host_objects import (
    NetworkListTooLarge,
    VolumeListTooLarge,
    busy_view,
)
from app.services.networks import NetworkRefused
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectBusy
from app.services.stack_import import (
    ImportBlocked,
    StackImportChanged,
    StackUploadGone,
    UnlockFailed,
    UnlockLimited,
)
from app.services.volume_views import VolumeRefused
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.object_pages import (
    Failure,
    administrator_detail,
    busy_message,
    page_response,
    problem_page,
    redirect,
    route,
)
from app.web.problems import unreachable_copy
from app.web.restore_upload import read_archive_upload

UPLOAD_ID_PATTERN: Final = re.compile(r"[0-9a-f]{32}")
FIELD: Final = re.compile(
    r"(?P<group>volume|path|secret)-(?P<index>[0-9]{1,3})-(?P<part>source|choice|name|value)"
)
SCALARS: Final = frozenset({"action", "expected_plan"})
MAX_FIELDS: Final = 600
MAX_NAME_FIELD: Final = 512
MAX_VALUE_FIELD: Final = 70_000
HEADING: Final = "Import container"
CANCELLED: Final = "The upload was discarded. Nothing was changed."
OPENED: Final = "The sealed values were opened."

router = APIRouter(
    prefix="/environments/{environment_id}/containers",
    tags=["containers"],
    dependencies=[Depends(require_user)],
)
Administrator = Annotated[Principal, Depends(require_administrator_or_not_found)]

REFUSALS: Final = (
    StackUploadGone,
    ContainerImportFailed,
    ImportBlocked,
    StackImportChanged,
    ValidationFailed,
    NetworkRefused,
    VolumeRefused,
    ObjectBusy,
    EnvironmentUnavailable,
    ObjectTooLarge,
    NetworkListTooLarge,
    VolumeListTooLarge,
)


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
    """The posted review form: the choices and the action."""

    choices: ContainerChoices
    action: str
    expected_plan: str


async def _read_form(request: Request) -> _Posted:
    posted = await request.form()
    scalars: dict[str, str] = {}
    groups: dict[str, dict[int, dict[str, str]]] = {"volume": {}, "path": {}, "secret": {}}
    for count, (key, value) in enumerate(posted.multi_items()):
        if count >= MAX_FIELDS:
            break
        if not isinstance(value, str):
            continue
        if key in SCALARS:
            scalars[key] = value
        elif (found := FIELD.fullmatch(key)) is not None:
            groups[found["group"]].setdefault(int(found["index"]), {})[found["part"]] = value
    volume_names = {
        row["source"]: row["name"][:MAX_NAME_FIELD]
        for row in groups["volume"].values()
        if row.get("source") and row.get("name")
    }
    host_paths = {
        row["source"]: row["choice"]
        for row in groups["path"].values()
        if row.get("source") and row.get("choice")
    }
    path_volumes = {
        row["source"]: row["name"][:MAX_NAME_FIELD]
        for row in groups["path"].values()
        if row.get("source") and row.get("name")
    }
    entered = {
        row["name"]: row["value"][:MAX_VALUE_FIELD]
        for row in groups["secret"].values()
        if row.get("name") and row.get("value")
    }
    return _Posted(
        ContainerChoices(volume_names, host_paths, path_volumes, entered),
        scalars.get("action", ""),
        scalars.get("expected_plan", "")[:128],
    )


def _failure_of(error: Exception, request: Request, actor: Principal) -> Failure:
    if isinstance(error, StackUploadGone):
        return Failure(409, "The upload is gone", str(error))
    if isinstance(error, ContainerImportFailed):
        return Failure(502, "The import failed", str(error))
    if isinstance(error, ObjectBusy):
        busy = busy_view(error, actor, request.app.state.auth.clock())
        return Failure(409, "An object is busy", busy_message(busy, "it"))
    if isinstance(error, NetworkRefused | VolumeRefused):
        return Failure(
            502, "Docker refused the request", str(error), administrator_detail(error, actor)
        )
    if isinstance(error, ImportBlocked):
        return Failure(409, "The import cannot start", str(error))
    if isinstance(error, ValidationFailed):
        return Failure(400, "Check the request", "; ".join(error.fields.values()))
    return Failure(503, *unreachable_copy(error))


def _href(request: Request, name: str, environment_id: int, **params: Any) -> str:
    return route(request, name, environment_id=environment_id, **params)


def _problem(
    request: Request,
    error: Exception,
    actor: Principal,
    environment_id: int,
    review_href: str | None = None,
) -> Response:
    gone = isinstance(error, StackUploadGone)
    return problem_page(
        request,
        heading=HEADING,
        failure=_failure_of(error, request, actor),
        back_href=_href(
            request,
            "environment_container_import_form" if gone else "environment_container_list",
            environment_id,
        ),
        back_label="Upload the archive again" if gone else "Back to containers",
        review_href=None if gone else review_href,
        region=None,
    )


async def _upload_page(
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
        "containers/import_upload.html",
        {
            "environment": environment,
            "errors": errors or {},
            "post_href": _href(request, "environment_container_import_upload", environment_id),
            "list_href": _href(request, "environment_container_list", environment_id),
        },
        status_code,
    )


def _review_page(
    request: Request,
    review: ContainerImportReview,
    posted: _Posted | None,
    errors: Mapping[str, str],
    status_code: int = 200,
) -> Response:
    environment_id = review.environment_id
    identifiers = {"upload_id": review.upload_id}
    return page_response(
        request,
        "containers/import_review.html",
        {
            "review": review,
            "plan": review.plan,
            "errors": errors,
            "entered": sorted(posted.choices.entered) if posted is not None else [],
            "post_href": _href(
                request, "environment_container_import_run", environment_id, **identifiers
            ),
            "unlock_href": _href(
                request, "environment_container_import_unlock", environment_id, **identifiers
            ),
            "cancel_href": _href(
                request, "environment_container_import_cancel", environment_id, **identifiers
            ),
            "list_href": _href(request, "environment_container_list", environment_id),
        },
        status_code,
    )


async def _show_review(
    request: Request,
    imports: ContainerImports,
    actor: Principal,
    environment_id: int,
    upload_id: str,
    *,
    posted: _Posted | None = None,
    errors: Mapping[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    try:
        review = await imports.review(
            actor,
            _session_id(actor),
            environment_id,
            upload_id,
            None if posted is None else posted.choices,
        )
    except REFUSALS as error:
        return _problem(request, error, actor, environment_id)
    return _review_page(request, review, posted, errors or {}, status_code)


def _created(request: Request, result: ContainerPrepared) -> Response:
    form = _href(request, "environment_container_new", result.environment_id)
    summary = (
        f"Created {len(result.networks)} {'network' if len(result.networks) == 1 else 'networks'} "
        f"and {len(result.volumes)} {'volume' if len(result.volumes) == 1 else 'volumes'}. "
        "Review every value, then create the container."
    )
    return redirect(
        request, f"{form}?{urlencode({'imported': result.prepared_id})}", [Flash("ok", summary)]
    )


@router.get("/-/import", response_class=HTMLResponse, name="environment_container_import_form")
async def import_form(
    request: Request, environments: Environments, actor: Administrator, environment_id: PathId
) -> Response:
    """Show the archive upload form; administrators only."""
    return await _upload_page(request, environments, actor, environment_id)


@router.post("/-/import", response_class=HTMLResponse, name="environment_container_import_upload")
async def upload_archive(
    request: Request,
    imports: ContainerImports,
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
            parts,
            declared_size=int(declared) if declared.isdigit() else None,
        )

    try:
        upload_id = await read_archive_upload(request, receive)
    except ValidationFailed as invalid:
        return await _upload_page(
            request, environments, actor, environment_id, errors=invalid.fields, status_code=422
        )
    except (EnvironmentUnavailable, ObjectBusy) as error:
        return _problem(request, error, actor, environment_id)
    return redirect(
        request,
        _href(request, "environment_container_import_review", environment_id, upload_id=upload_id),
    )


@router.get(
    "/-/import/{upload_id}",
    response_class=HTMLResponse,
    name="environment_container_import_review",
)
async def review_import(
    request: Request,
    imports: ContainerImports,
    actor: Administrator,
    environment_id: PathId,
    upload_id: UploadId,
) -> Response:
    """Name everything the import creates; nothing is changed."""
    return await _show_review(request, imports, actor, environment_id, upload_id)


@router.post(
    "/-/import/{upload_id}", response_class=HTMLResponse, name="environment_container_import_run"
)
async def run_import(
    request: Request,
    imports: ContainerImports,
    actor: Administrator,
    environment_id: PathId,
    upload_id: UploadId,
) -> Response:
    """Check the choices again, or create the networks and volumes and open the create form."""
    posted = await _read_form(request)
    if posted.action != "import":
        return await _show_review(request, imports, actor, environment_id, upload_id, posted=posted)
    review_href = _href(
        request, "environment_container_import_review", environment_id, upload_id=upload_id
    )
    try:
        result = await imports.prepare(
            actor, environment_id, upload_id, posted.choices, posted.expected_plan
        )
    except (StackImportChanged, ImportBlocked, ValidationFailed) as refused:
        text = (
            "; ".join(refused.fields.values())
            if isinstance(refused, ValidationFailed)
            else str(refused)
        )
        return await _show_review(
            request,
            imports,
            actor,
            environment_id,
            upload_id,
            posted=posted,
            errors={"import": text},
            status_code=409,
        )
    except REFUSALS as error:
        return _problem(request, error, actor, environment_id, review_href)
    return _created(request, result)


@router.post(
    "/-/import/{upload_id}/unlock",
    response_class=HTMLResponse,
    name="environment_container_import_unlock",
)
async def unlock_import(
    request: Request,
    imports: ContainerImports,
    actor: Administrator,
    environment_id: PathId,
    upload_id: UploadId,
) -> Response:
    """Open the sealed values with the passphrase; the passphrase is shown again nowhere."""
    posted = await request.form()
    passphrase = posted.get("passphrase")
    status_code = 422
    try:
        await imports.unlock(
            actor,
            _session_id(actor),
            environment_id,
            upload_id,
            passphrase if isinstance(passphrase, str) else "",
        )
    except (UnlockFailed, UnlockLimited) as failed:
        status_code = 429 if isinstance(failed, UnlockLimited) else 422
        return await _show_review(
            request,
            imports,
            actor,
            environment_id,
            upload_id,
            errors={"passphrase": str(failed)},
            status_code=status_code,
        )
    except ValidationFailed as invalid:
        return await _show_review(
            request,
            imports,
            actor,
            environment_id,
            upload_id,
            errors=invalid.fields,
            status_code=status_code,
        )
    except (StackUploadGone, EnvironmentUnavailable) as error:
        return _problem(request, error, actor, environment_id)
    return redirect(
        request,
        _href(request, "environment_container_import_review", environment_id, upload_id=upload_id),
        [Flash("ok", OPENED)],
    )


@router.post(
    "/-/import/{upload_id}/cancel",
    response_class=HTMLResponse,
    name="environment_container_import_cancel",
)
async def cancel_import(
    request: Request,
    imports: ContainerImports,
    actor: Administrator,
    environment_id: PathId,
    upload_id: UploadId,
) -> Response:
    """Discard the held upload and remove its file."""
    try:
        await imports.cancel(actor, _session_id(actor), environment_id, upload_id)
    except EnvironmentUnavailable as error:
        return _problem(request, error, actor, environment_id)
    return redirect(
        request,
        _href(request, "environment_container_list", environment_id),
        [Flash("ok", CANCELLED)],
    )
