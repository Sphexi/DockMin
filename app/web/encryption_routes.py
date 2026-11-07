"""Encryption pages: key and stored-secret overview, the check, and the re-encryption run."""

import re
from dataclasses import dataclass
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Form, Path, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentAdministrator, require_administrator
from app.auth.errors import ResourceNotFound, ValidationFailed
from app.db.repositories.rekey import RekeyFailure
from app.services.encryption import (
    CONFIRM_FIELD,
    EncryptionConflict,
    EncryptionOverview,
    EncryptionService,
    RekeyRun,
)
from app.web.navigation import no_store
from app.web.object_pages import redirect, route
from app.web.templating import is_htmx_request, render_fragment, render_page

router = APIRouter(prefix="/server", dependencies=[Depends(require_administrator)])

OVERVIEW_TEMPLATE: Final = "server/encryption.html"
CONFIRM_TEMPLATE: Final = "server/encryption_rekey_confirm.html"
RUN_TEMPLATE: Final = "server/encryption_run.html"
RUN_FRAGMENT: Final = "server/_encryption_run.html"
RUN_MACRO: Final = "encryption_run"
RUN_ID: Final = re.compile(r"[0-9a-f]{32}")
RUN_POLL_SECONDS: Final = 2
DIGEST_MAX: Final = 64
FIELD_MAX: Final = 16
CONFIRMED: Final = "yes"
CONFLICT_STATUS: Final = 409
UNPROCESSABLE_STATUS: Final = 422

OWNING_ROUTES: Final = {
    "registry_credential": ("registry_detail", "credential_id", "Open the credential set"),
    "object_storage_target": (
        "object_storage_target_detail",
        "target_id",
        "Open the object storage target",
    ),
    "notification_channel": (
        "notification_channel_detail",
        "channel_id",
        "Open the notification channel",
    ),
}
LISTING_ROUTES: Final = {
    "environment_tls_material": ("index", "Open the environments"),
}


def get_encryption(request: Request) -> EncryptionService:
    """Return the encryption service the application was built with."""
    service: EncryptionService = request.app.state.encryption
    return service


Encryption = Annotated[EncryptionService, Depends(get_encryption)]


@dataclass(frozen=True, slots=True)
class FailureRow:
    """One value that did not open, as the run page lists it."""

    location: str
    kind: str
    href: str | None
    link_label: str | None


@dataclass(frozen=True, slots=True)
class BelowCurrent:
    """One secret column that still holds values below the current key version."""

    name: str
    detail: str


def _failure_row(request: Request, failure: RekeyFailure) -> FailureRow:
    table = failure.field.table
    href: str | None = None
    link_label: str | None = None
    if table in OWNING_ROUTES:
        name, parameter, link_label = OWNING_ROUTES[table]
        href = route(request, name, **{parameter: failure.row_id})
    elif table in LISTING_ROUTES:
        name, link_label = LISTING_ROUTES[table]
        href = route(request, name)
    return FailureRow(
        location=f"{failure.field} row {failure.row_id}",
        kind=failure.kind,
        href=href,
        link_label=link_label,
    )


def _below_current(overview: EncryptionOverview) -> list[BelowCurrent]:
    current = overview.key_version
    rows: list[BelowCurrent] = []
    for column in overview.columns:
        older = [(version, total) for version, total in column.by_version if version < current]
        if older:
            detail = ", ".join(f"{total} at version {version}" for version, total in older)
            rows.append(BelowCurrent(f"{column.table}.{column.column}", detail))
    return rows


def _versions(overview: EncryptionOverview) -> list[int]:
    found = {version for column in overview.columns for version, _ in column.by_version}
    return sorted(found | {overview.key_version})


def _overview_context(
    request: Request, overview: EncryptionOverview, error: str | None = None
) -> dict[str, Any]:
    last = overview.last_run
    return {
        "overview": overview,
        "versions": _versions(overview),
        "counts": {
            f"{column.table}.{column.column}": dict(column.by_version)
            for column in overview.columns
        },
        "error": error,
        "check_href": route(request, "server_encryption_check"),
        "rekey_href": route(request, "server_encryption_rekey_confirm"),
        "last_run_href": (
            None if last is None else route(request, "server_encryption_run", run_id=last.id)
        ),
    }


def _confirm_context(
    request: Request,
    overview: EncryptionOverview,
    *,
    fields: dict[str, str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    versions = sorted(
        {
            version
            for column in overview.columns
            for version, _ in column.by_version
            if version < overview.key_version
        }
    )
    return {
        "overview": overview,
        "targets": _below_current(overview),
        "from_versions": versions,
        "fields": fields or {},
        "error": error,
        "rekey_href": route(request, "server_encryption_rekey"),
        "back_href": route(request, "server_encryption"),
        "confirm_field": CONFIRM_FIELD,
        "confirm_value": CONFIRMED,
    }


def _run_context(request: Request, run: RekeyRun) -> dict[str, Any]:
    return {
        "run": run,
        "rows": [_failure_row(request, failure) for failure in run.failures],
        "href": route(request, "server_encryption_run", run_id=run.id),
        "poll_seconds": RUN_POLL_SECONDS,
    }


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


@router.get("/encryption", response_class=HTMLResponse, name="server_encryption")
async def server_encryption(
    request: Request, principal: CurrentAdministrator, encryption: Encryption
) -> Response:
    """Show the key in use, the previous key, and the stored secrets by column and key version."""
    overview = await encryption.overview(principal)
    return _page(request, OVERVIEW_TEMPLATE, _overview_context(request, overview))


@router.post("/encryption/check", name="server_encryption_check")
async def server_encryption_check(
    request: Request, principal: CurrentAdministrator, encryption: Encryption
) -> Response:
    """Start a read-only check of every stored secret and open its run page."""
    try:
        run_id = await encryption.check(principal)
    except EncryptionConflict as conflict:
        overview = await encryption.overview(principal)
        return _page(
            request,
            OVERVIEW_TEMPLATE,
            _overview_context(request, overview, str(conflict)),
            CONFLICT_STATUS,
        )
    return redirect(request, route(request, "server_encryption_run", run_id=run_id))


@router.get(
    "/encryption/rekey", response_class=HTMLResponse, name="server_encryption_rekey_confirm"
)
async def server_encryption_rekey_confirm(
    request: Request, principal: CurrentAdministrator, encryption: Encryption
) -> Response:
    """Name every column and count a re-encryption would rewrite; nothing happens until posted."""
    overview = await encryption.overview(principal)
    if not overview.values_below_current:
        message = f"Every stored secret is already at key version {overview.key_version}."
        return _page(
            request,
            CONFIRM_TEMPLATE,
            _confirm_context(request, overview, error=message),
            CONFLICT_STATUS,
        )
    return _page(request, CONFIRM_TEMPLATE, _confirm_context(request, overview))


@router.post("/encryption/rekey", name="server_encryption_rekey")
async def server_encryption_rekey(
    request: Request,
    principal: CurrentAdministrator,
    encryption: Encryption,
    expected_counts: Annotated[str, Form(max_length=DIGEST_MAX)] = "",
    confirm_rekey: Annotated[str, Form(max_length=FIELD_MAX)] = "",
) -> Response:
    """Take the instance offline and start re-encrypting; the run page follows."""
    try:
        run_id = await encryption.begin_rekey(
            principal, expected_counts=expected_counts, confirmed=confirm_rekey == CONFIRMED
        )
    except ValidationFailed as invalid:
        overview = await encryption.overview(principal)
        return _page(
            request,
            CONFIRM_TEMPLATE,
            _confirm_context(request, overview, fields=invalid.fields),
            UNPROCESSABLE_STATUS,
        )
    except EncryptionConflict as conflict:
        overview = await encryption.overview(principal)
        return _page(
            request,
            CONFIRM_TEMPLATE,
            _confirm_context(request, overview, error=str(conflict)),
            CONFLICT_STATUS,
        )
    return redirect(request, route(request, "server_encryption_run", run_id=run_id))


@router.get("/encryption/runs/{run_id}", response_class=HTMLResponse, name="server_encryption_run")
async def server_encryption_run(
    request: Request,
    principal: CurrentAdministrator,
    encryption: Encryption,
    run_id: Annotated[str, Path(max_length=64)],
) -> Response:
    """Show how a check or re-encryption is going; polled until it ends."""
    if RUN_ID.fullmatch(run_id) is None:
        raise ResourceNotFound("not found")
    run = await encryption.run(principal, run_id)
    context = _run_context(request, run)
    if is_htmx_request(request):
        return no_store(render_fragment(request, RUN_FRAGMENT, RUN_MACRO, **context))
    return _page(
        request,
        RUN_TEMPLATE,
        {**context, "overview_href": route(request, "server_encryption")},
    )
