"""Volume import pages: the upload, the review, the import and its result."""

import re
from collections.abc import AsyncIterator, Mapping
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import require_administrator_or_not_found, require_user
from app.auth.errors import ResourceNotFound, SelfProtected, ValidationFailed
from app.auth.principal import Principal
from app.services.assembly import Environments
from app.services.containers import ContainerListTooLarge
from app.services.environments import EnvironmentSummary
from app.services.host_objects import VolumeListTooLarge, busy_view
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectBusy
from app.services.volume_import import (
    ImportReview,
    ImportUploadGone,
    VolumeImported,
    VolumeImportFailed,
    VolumeImportRefused,
    option_rows,
)
from app.services.volume_import_assembly import VolumeImports
from app.services.volume_import_store import UPLOAD_ID
from app.services.volume_views import VolumeNameInUse, VolumeRefused
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.object_pages import (
    ROW_CAP,
    DigestField,
    Failure,
    administrator_detail,
    busy_message,
    page_response,
    problem_page,
    read_form,
    redirect,
    route,
    row_count,
)
from app.web.problems import unreachable_copy
from app.web.restore_upload import read_archive_upload

router = APIRouter(
    prefix="/environments/{environment_id}/volumes",
    tags=["volumes"],
    dependencies=[Depends(require_user)],
)

HEADING: Final = "Import volume"
UPLOAD_ID_PATTERN: Final = re.compile(UPLOAD_ID)
IMPORT_FIELDS: Final = frozenset({"name", "driver", "rows", "expected_digest"})
NAME_FIELD_MAX: Final = 512
CANCELLED: Final = "The upload was discarded. Nothing was changed."
Administrator = Annotated[Principal, Depends(require_administrator_or_not_found)]

REFUSALS: Final = (
    SelfProtected,
    ObjectBusy,
    ValidationFailed,
    ImportUploadGone,
    VolumeImportRefused,
    VolumeImportFailed,
    VolumeRefused,
    EnvironmentUnavailable,
    ObjectTooLarge,
    VolumeListTooLarge,
    ContainerListTooLarge,
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


def _failure_of(request: Request, error: Exception, actor: Principal) -> Failure:
    if isinstance(error, ObjectBusy):
        busy = busy_view(error, actor, request.app.state.auth.clock())
        return Failure(409, "This volume is busy", busy_message(busy, "it"))
    if isinstance(error, SelfProtected):
        return Failure(409, "Protected", str(error))
    if isinstance(error, ImportUploadGone):
        return Failure(409, "The upload is gone", str(error))
    if isinstance(error, VolumeImportRefused):
        return Failure(409, "The volume cannot be imported", str(error))
    if isinstance(error, VolumeImportFailed):
        return Failure(502, "The import failed", str(error))
    if isinstance(error, ValidationFailed):
        return Failure(400, "Check the request", "; ".join(error.fields.values()))
    if isinstance(error, VolumeRefused):
        return Failure(
            502, "Docker refused this configuration", str(error), administrator_detail(error, actor)
        )
    return Failure(503, *unreachable_copy(error))


def _import_href(request: Request, environment_id: int) -> str:
    return route(request, "environment_volume_import_form", environment_id=environment_id)


def _list_href(request: Request, environment_id: int) -> str:
    return route(request, "environment_volume_list", environment_id=environment_id)


def _problem(
    request: Request,
    error: Exception,
    actor: Principal,
    environment_id: int,
    review_href: str | None = None,
) -> Response:
    gone = isinstance(error, ImportUploadGone)
    return problem_page(
        request,
        heading=HEADING,
        failure=_failure_of(request, error, actor),
        back_href=_import_href(request, environment_id)
        if gone
        else _list_href(request, environment_id),
        back_label="Upload the archive again" if gone else "Back to volumes",
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
        "volumes/import_upload.html",
        {
            "environment": environment,
            "errors": errors or {},
            "post_href": route(
                request, "environment_volume_import_upload", environment_id=environment_id
            ),
            "list_href": _list_href(request, environment_id),
        },
        status_code,
    )


@router.get("/-/import", response_class=HTMLResponse, name="environment_volume_import_form")
async def import_form(
    request: Request, environments: Environments, actor: Administrator, environment_id: PathId
) -> Response:
    """Show the archive upload form; administrators only."""
    return await _upload_page(request, environments, actor, environment_id)


@router.post("/-/import", response_class=HTMLResponse, name="environment_volume_import_upload")
async def upload_archive(
    request: Request,
    imports: VolumeImports,
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
    except REFUSALS as error:
        return _problem(request, error, actor, environment_id)
    return redirect(
        request,
        route(
            request,
            "environment_volume_import_review",
            environment_id=environment_id,
            upload_id=upload_id,
        ),
    )


def _review_page(
    request: Request,
    review: ImportReview,
    values: Mapping[str, str],
    errors: Mapping[str, str],
    *,
    status_code: int = 200,
    adding: bool = False,
) -> Response:
    return page_response(
        request,
        "volumes/import_review.html",
        {
            "review": review,
            "values": values,
            "errors": errors,
            "option_count": row_count(values, "option", add=adding),
            "row_cap": ROW_CAP,
            "post_href": route(
                request,
                "environment_volume_import_run",
                environment_id=review.environment_id,
                upload_id=review.upload_id,
            ),
            "cancel_href": route(
                request,
                "environment_volume_import_cancel",
                environment_id=review.environment_id,
                upload_id=review.upload_id,
            ),
            "list_href": _list_href(request, review.environment_id),
        },
        status_code,
    )


def _defaults(review: ImportReview) -> dict[str, str]:
    return {"name": review.name, "driver": review.driver, **option_rows(dict(review.options))}


@router.get(
    "/-/import/{upload_id}", response_class=HTMLResponse, name="environment_volume_import_review"
)
async def review_import(
    request: Request,
    imports: VolumeImports,
    actor: Administrator,
    environment_id: PathId,
    upload_id: UploadId,
) -> Response:
    """Name everything the import creates and everything it drops; nothing is changed."""
    try:
        review = await imports.review(actor, _session_id(actor), environment_id, upload_id)
    except REFUSALS as error:
        return _problem(request, error, actor, environment_id)
    return _review_page(request, review, _defaults(review), {})


@router.post(
    "/-/import/{upload_id}", response_class=HTMLResponse, name="environment_volume_import_run"
)
async def run_import(
    request: Request,
    imports: VolumeImports,
    environments: Environments,
    actor: Administrator,
    environment_id: PathId,
    upload_id: UploadId,
    expected_digest: DigestField = "",
) -> Response:
    """Create the volume and write the archive into it, or show the review again."""
    values = await read_form(request, IMPORT_FIELDS)
    if values.get("rows") == "option":
        return await _review_again(
            request, imports, actor, environment_id, upload_id, values, {}, 200, adding=True
        )
    options = {key: value for key, value in values.items() if key.startswith("option-")}
    try:
        result = await imports.import_volume(
            actor,
            environment_id,
            upload_id,
            values.get("name", "").strip(),
            expected_digest,
            driver=values.get("driver", ""),
            options=options,
        )
    except (VolumeNameInUse, ValidationFailed) as invalid:
        status = 409 if isinstance(invalid, VolumeNameInUse) else 400
        return await _review_again(
            request, imports, actor, environment_id, upload_id, values, invalid.fields, status
        )
    except REFUSALS as error:
        return _problem(
            request,
            error,
            actor,
            environment_id,
            review_href=route(
                request,
                "environment_volume_import_review",
                environment_id=environment_id,
                upload_id=upload_id,
            ),
        )
    environment = (await environments.environments.get(actor, environment_id)).summary
    return _result_page(request, environment, result)


async def _review_again(
    request: Request,
    imports: VolumeImports,
    actor: Principal,
    environment_id: int,
    upload_id: str,
    values: Mapping[str, str],
    errors: Mapping[str, str],
    status_code: int,
    *,
    adding: bool = False,
) -> Response:
    try:
        review = await imports.review(actor, _session_id(actor), environment_id, upload_id)
    except REFUSALS as error:
        return _problem(request, error, actor, environment_id)
    return _review_page(request, review, values, errors, status_code=status_code, adding=adding)


def _result_page(
    request: Request, environment: EnvironmentSummary, result: VolumeImported
) -> Response:
    return page_response(
        request,
        "volumes/import_result.html",
        {
            "environment": environment,
            "result": result,
            "detail_href": route(
                request,
                "environment_volume_detail",
                environment_id=result.environment_id,
                volume_name=result.name,
            ),
            "list_href": _list_href(request, result.environment_id),
        },
    )


@router.post(
    "/-/import/{upload_id}/cancel",
    response_class=HTMLResponse,
    name="environment_volume_import_cancel",
)
async def cancel_import(
    request: Request,
    imports: VolumeImports,
    actor: Administrator,
    environment_id: PathId,
    upload_id: UploadId,
) -> Response:
    """Discard the held upload and remove its file."""
    try:
        await imports.cancel(actor, _session_id(actor), environment_id, upload_id)
    except REFUSALS as error:
        return _problem(request, error, actor, environment_id)
    return redirect(request, _list_href(request, environment_id), [Flash("ok", CANCELLED)])
