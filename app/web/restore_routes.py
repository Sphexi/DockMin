"""Restore pages: begin, upload, review, key and confirmation, cancel, and the progress page."""

import re
from collections.abc import AsyncIterator
from typing import Annotated, Any, Final

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import SecretStr

from app.auth.dependencies import CurrentAdministrator
from app.auth.errors import NotFound, ValidationFailed
from app.auth.principal import Principal
from app.services.assembly import Restores
from app.services.restore import Manifest, RestoreBusy, RestoreNotFound
from app.web.flash import Flash
from app.web.navigation import no_store
from app.web.object_pages import Failure, problem_page, redirect, route
from app.web.restore_upload import read_archive_upload
from app.web.templating import is_htmx_request, render_fragment, render_page

router = APIRouter(prefix="/restore")

PROGRESS: Final = "restore/_progress.html"
FETCH_PROGRESS: Final = "restore/_fetch.html"
MAX_OBJECT_PATH: Final = 1024
MAX_ID: Final = 2**63 - 1
RESTORE_ID: Final = re.compile(r"[0-9a-f]{32}")
KEY_MAX: Final = 512
HASH_MAX: Final = 64
FIELD_MAX: Final = 16
YES: Final = "yes"

NOTICES: Final = {
    "cancelled": "The restore is cancelled. This instance accepts changes again.",
    "resumed": "Automation is resumed. Scheduled jobs run again.",
}
OFFLINE_REQUIRED: Final = "Confirm that this instance goes offline."
STEP_LABELS: Final = {
    "gate": "Take the instance offline",
    "upload": "Receive the archive",
    "key": "Check the key",
    "verify": "Verify the archive",
    "stage": "Stage the data",
    "migrate": "Migrate and check the database",
    "decrypt": "Check the secrets and re-seal them",
    "swap": "Replace the data",
    "restart": "Restart on the restored data",
}


def _session_id(actor: Principal) -> int:
    """Return the session a restore belongs to; a token principal has none and is refused."""
    if actor.session_id is None:
        raise HTTPException(status_code=403)
    return actor.session_id


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


def _busy(request: Request, busy: RestoreBusy) -> Response:
    return problem_page(
        request,
        heading="Restore",
        failure=Failure(409, "The restore cannot do that now", str(busy)),
        back_href=route(request, "restore"),
        back_label="Back to the restore",
        region=None,
    )


async def _index(
    request: Request,
    restores: Restores,
    actor: Principal,
    *,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    status = restores.status(actor, _session_id(actor))
    targets = (
        await restores.remote_targets(actor)
        if status is not None and status.owned_by_caller and status.stage != "running"
        else []
    )
    return _page(
        request,
        "restore/index.html",
        {"status": status, "errors": errors or {}, "targets": targets},
        status_code,
    )


async def _remote(
    request: Request,
    restores: Restores,
    actor: Principal,
    *,
    target_id: int,
    page: int,
    refresh: bool,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    try:
        listing = await restores.list_remote(
            actor, _session_id(actor), target_id, refresh=refresh, page=page
        )
    except RestoreNotFound:
        return redirect(request, route(request, "restore"))
    except NotFound:
        raise HTTPException(status_code=404) from None
    return _page(
        request,
        "restore/remote.html",
        {"listing": listing, "errors": errors or {}},
        status_code,
    )


async def _review(
    request: Request,
    restores: Restores,
    actor: Principal,
    *,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    try:
        review = await restores.review(actor, _session_id(actor))
    except RestoreNotFound:
        return redirect(request, route(request, "restore"))
    if review.manifest is None:
        return redirect(request, route(request, "restore"))
    return _page(
        request,
        "restore/review.html",
        {"review": review, "manifest": review.manifest, "errors": errors or {}},
        status_code,
    )


@router.get("", response_class=HTMLResponse, name="restore")
async def restore_index(
    request: Request, restores: Restores, actor: CurrentAdministrator
) -> Response:
    """Show the state of the restore: the form that begins one, or the step it is at."""
    return await _index(request, restores, actor)


@router.post("/begin", name="restore_begin")
async def begin_restore(
    request: Request,
    restores: Restores,
    actor: CurrentAdministrator,
    confirm_offline: Annotated[str, Form(max_length=FIELD_MAX)] = "",
) -> Response:
    """Take the instance offline for a restore, once the administrator has confirmed it."""
    if confirm_offline != YES:
        return await _index(
            request,
            restores,
            actor,
            errors={"confirm_offline": OFFLINE_REQUIRED},
            status_code=422,
        )
    try:
        await restores.begin(actor, session_id=_session_id(actor))
    except RestoreBusy as busy:
        return _busy(request, busy)
    return redirect(request, route(request, "restore"))


@router.post("/archive", name="restore_archive")
async def upload_archive(
    request: Request, restores: Restores, actor: CurrentAdministrator
) -> Response:
    """Receive the archive as a stream; the first part of the body is the CSRF token."""
    session_id = _session_id(actor)

    async def receive(parts: AsyncIterator[bytes]) -> Manifest:
        return await restores.receive_archive(actor, session_id, parts)

    try:
        await read_archive_upload(request, receive)
    except ValidationFailed as invalid:
        return await _index(request, restores, actor, errors=invalid.fields, status_code=422)
    except RestoreNotFound:
        return redirect(request, route(request, "restore"))
    except RestoreBusy as busy:
        return _busy(request, busy)
    return redirect(request, route(request, "restore_review"))


@router.get("/remote", response_class=HTMLResponse, name="restore_remote")
async def restore_remote(
    request: Request,
    restores: Restores,
    actor: CurrentAdministrator,
    target_id: Annotated[int, Query(ge=1, le=MAX_ID)],
    page: Annotated[int, Query(ge=1, le=MAX_ID)] = 1,
    refresh: bool = False,
) -> Response:
    """List the archives in a bucket under the target's backup folder."""
    return await _remote(request, restores, actor, target_id=target_id, page=page, refresh=refresh)


@router.post("/archive/remote", name="restore_remote_fetch")
async def fetch_remote_archive(
    request: Request,
    restores: Restores,
    actor: CurrentAdministrator,
    target_id: Annotated[int, Form(ge=1, le=MAX_ID)],
    object_path: Annotated[str, Form(max_length=MAX_OBJECT_PATH)] = "",
) -> Response:
    """Start fetching a listed archive; the review follows when it has arrived."""
    try:
        await restores.fetch_archive(
            actor, _session_id(actor), target_id=target_id, object_path=object_path
        )
    except ValidationFailed as invalid:
        return await _remote(
            request,
            restores,
            actor,
            target_id=target_id,
            page=1,
            refresh=False,
            errors=invalid.fields,
            status_code=400,
        )
    except RestoreNotFound:
        return redirect(request, route(request, "restore"))
    except RestoreBusy as busy:
        return _busy(request, busy)
    return redirect(request, route(request, "restore_review"))


@router.get("/review", response_class=HTMLResponse, name="restore_review")
async def review_archive(
    request: Request, restores: Restores, actor: CurrentAdministrator
) -> Response:
    """Show the archive's manifest, what it would replace, and the key prompt.

    While an archive is being fetched the page shows the fetch progress instead.
    """
    status = restores.status(actor, _session_id(actor))
    if status is not None and status.owned_by_caller and status.stage == "fetching":
        if is_htmx_request(request):
            return no_store(
                render_fragment(request, FETCH_PROGRESS, "fetch_progress", status=status)
            )
        return _page(request, "restore/fetching.html", {"status": status})
    if is_htmx_request(request):
        return redirect(request, route(request, "restore_review"))
    return await _review(request, restores, actor)


@router.post("/confirm", name="restore_confirm")
async def confirm_restore(
    request: Request,
    restores: Restores,
    actor: CurrentAdministrator,
    key: Annotated[str, Form(max_length=KEY_MAX)] = "",
    archive_sha256: Annotated[str, Form(max_length=HASH_MAX)] = "",
    replace_confirmed: Annotated[str, Form(max_length=FIELD_MAX)] = "",
) -> Response:
    """Check the key and the reviewed archive, then run the restore in the background."""
    try:
        restore_id = await restores.confirm(
            actor,
            _session_id(actor),
            key_text=SecretStr(key),
            archive_sha256=archive_sha256,
            replace_confirmed=replace_confirmed == YES,
        )
    except ValidationFailed as invalid:
        return await _review(request, restores, actor, errors=invalid.fields, status_code=422)
    except RestoreNotFound:
        return redirect(request, route(request, "restore"))
    except RestoreBusy as busy:
        return _busy(request, busy)
    return redirect(request, route(request, "restore_progress", restore_id=restore_id))


@router.post("/cancel", name="restore_cancel")
async def cancel_restore(
    request: Request, restores: Restores, actor: CurrentAdministrator
) -> Response:
    """Cancel the restore and let the instance accept changes again, until the key is confirmed."""
    try:
        await restores.cancel(actor)
    except RestoreBusy as busy:
        return _busy(request, busy)
    return redirect(request, route(request, "restore"), [Flash("ok", NOTICES["cancelled"])])


@router.post("/resume-automation", name="restore_resume_automation")
async def resume_automation(
    request: Request, restores: Restores, actor: CurrentAdministrator
) -> Response:
    """Let the scheduler run again after a restore."""
    try:
        await restores.resume_automation(actor)
    except RestoreBusy as busy:
        return _busy(request, busy)
    return redirect(request, route(request, "restore"), [Flash("ok", NOTICES["resumed"])])


@router.get("/progress/{restore_id}", response_class=HTMLResponse, name="restore_progress")
async def restore_progress(request: Request, restores: Restores, restore_id: str) -> Response:
    """Show the steps of a restore.

    Before the data is replaced only the session that began the restore may look; after that the
    restore id in the path is the authority, because every session is gone with the old data.
    """
    if RESTORE_ID.fullmatch(restore_id) is None:
        raise HTTPException(status_code=404)
    principal: Principal | None = getattr(request.state, "principal", None)
    progress = restores.progress(
        restore_id, session_id=None if principal is None else principal.session_id
    )
    if progress is None:
        raise HTTPException(status_code=404)
    if is_htmx_request(request):
        return no_store(
            render_fragment(
                request,
                PROGRESS,
                "restore_progress",
                progress=progress,
                labels=STEP_LABELS,
                paused=restores.automation_paused,
                can_resume=principal is not None and principal.is_administrator,
            )
        )
    return _page(
        request,
        "restore/progress.html",
        {
            "progress": progress,
            "labels": STEP_LABELS,
            "paused": restores.automation_paused,
            "can_resume": principal is not None and principal.is_administrator,
        },
    )
