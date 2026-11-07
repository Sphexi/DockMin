"""Object storage target pages: list, form, detail, connection test and deletion."""

from dataclasses import dataclass, field
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import SecretStr

from app.auth.dependencies import CurrentAdministrator, require_administrator
from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE, InvalidCursorError
from app.services.assembly import TargetAdmin
from app.services.object_storage_admin import (
    CREDENTIAL_ADVICE,
    DEFAULT_ACTIVITY_LOG_PREFIX,
    DEFAULT_BACKUP_PREFIX,
    DEFAULT_REGION,
    PERMISSION_NOTE,
    RETENTION_FILES,
    RETENTION_FOLDER,
    SECRET_MAX,
    StorageCheck,
    TargetInput,
    TargetView,
    stage_message,
    target_warnings,
)
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.navigation import no_store
from app.web.object_pages import redirect, route
from app.web.pagination import DEFAULT_LIMIT, Column, PageView
from app.web.templating import is_htmx_request, render_fragment, render_page

router = APIRouter(prefix="/object-storage", dependencies=[Depends(require_administrator)])

TABLES: Final = "object_storage/_table.html"
CHECKS: Final = "object_storage/_checks.html"
FIELD_MAX: Final = 512
ENDPOINT_MAX: Final = 2048
PREFIX_MAX: Final = 1100
RETENTION_FIELD_MAX: Final = 8
STAMP_MAX: Final = 64
BUNDLE_MAX: Final = 64 * 1024
CHANGED_FORM_FIELD: Final = "form"

NOTICES: Final = {
    "created": "The target is saved. Test the connection to check it.",
    "updated": "Changes saved.",
    "deleted": "The target is deleted. Objects in its bucket are untouched.",
}
COLUMNS: Final = (
    Column("name", "Name"),
    Column("endpoint", "Endpoint"),
    Column("bucket", "Bucket"),
    Column("addressing", "Addressing", width="short"),
    Column("retention", "Remote retention", numeric=True, width="badge"),
    Column("notes", "Notes"),
)
STAGE_LABELS: Final = {
    "bucket": "Bucket",
    "backup_prefix": "Backup prefix",
    "activity_log_prefix": "Activity log prefix",
}
Text = Annotated[str, Form(max_length=FIELD_MAX)]


@dataclass(frozen=True, slots=True)
class FormState:
    """The values the target form shows; the secret and certificate text are never among them."""

    name: str = ""
    endpoint_url: str = ""
    region: str = DEFAULT_REGION
    bucket: str = ""
    access_key_id: str = ""
    backup_prefix: str = DEFAULT_BACKUP_PREFIX
    activity_log_prefix: str = DEFAULT_ACTIVITY_LOG_PREFIX
    retention_count: str = ""
    use_path_style: bool = True
    verify_tls: bool = True
    confirm_retention: bool = False
    remove_ca_bundle: bool = False


@dataclass(slots=True)
class SubmittedTarget:
    """The target form as posted."""

    form: TargetInput = field(repr=False)
    state: FormState
    expected_updated_at: str
    problems: dict[str, str]


@dataclass(frozen=True, slots=True)
class CheckRow:
    """One probe of the connection test, with the product's own wording."""

    stage: str
    label: str
    passed: bool
    message: str
    http: str | None


def _state_of(view: TargetView) -> FormState:
    return FormState(
        name=view.name,
        endpoint_url=view.endpoint_url,
        region=view.region,
        bucket=view.bucket,
        access_key_id=view.access_key_id,
        backup_prefix=view.backup_prefix,
        activity_log_prefix=view.activity_log_prefix,
        retention_count="" if view.retention_count is None else str(view.retention_count),
        use_path_style=view.use_path_style,
        verify_tls=view.verify_tls,
    )


def _check_row(check: StorageCheck) -> CheckRow:
    http = None
    if check.status is not None:
        http = f"HTTP {check.status}" + (f", {check.code}" if check.code else "")
    elif check.code:
        http = check.code
    return CheckRow(
        stage=check.stage,
        label=STAGE_LABELS[check.stage],
        passed=check.passed,
        message=stage_message(check),
        http=http,
    )


async def _bundle_text(text: str, upload: UploadFile | None) -> tuple[str, str | None]:
    """Return the certificates from the chosen file, else the pasted text."""
    if upload is not None and upload.filename:
        data = await upload.read(BUNDLE_MAX + 1)
        if len(data) > BUNDLE_MAX:
            return "", "That file is larger than 64 KiB."
        try:
            return data.decode("utf-8"), None
        except UnicodeDecodeError:
            return "", "That file is not text. Upload a PEM file."
    if len(text) > BUNDLE_MAX:
        return "", "That text is longer than 64 KiB."
    return text, None


async def read_target_form(
    name: Text = "",
    endpoint_url: Annotated[str, Form(max_length=ENDPOINT_MAX)] = "",
    region: Text = "",
    bucket: Text = "",
    access_key_id: Text = "",
    secret_access_key: Annotated[str, Form(max_length=SECRET_MAX + 1)] = "",
    use_path_style: Text = "",
    verify_tls: Text = "",
    remove_ca_bundle: Text = "",
    ca_bundle: Annotated[str, Form()] = "",
    ca_bundle_file: Annotated[UploadFile | None, File()] = None,
    backup_prefix: Annotated[str, Form(max_length=PREFIX_MAX)] = "",
    activity_log_prefix: Annotated[str, Form(max_length=PREFIX_MAX)] = "",
    retention_count: Annotated[str, Form(max_length=RETENTION_FIELD_MAX)] = "",
    confirm_retention: Text = "",
    expected_updated_at: Annotated[str, Form(max_length=STAMP_MAX)] = "",
) -> SubmittedTarget:
    """Read the target form; a checkbox is on when it was posted."""
    text, problem = await _bundle_text(ca_bundle, ca_bundle_file)
    state = FormState(
        name=name,
        endpoint_url=endpoint_url,
        region=region,
        bucket=bucket,
        access_key_id=access_key_id,
        backup_prefix=backup_prefix,
        activity_log_prefix=activity_log_prefix,
        retention_count=retention_count,
        use_path_style=use_path_style == "on",
        verify_tls=verify_tls == "on",
        confirm_retention=confirm_retention == "on",
        remove_ca_bundle=remove_ca_bundle == "on",
    )
    form = TargetInput(
        name=name,
        endpoint_url=endpoint_url,
        bucket=bucket,
        access_key_id=access_key_id,
        backup_prefix=backup_prefix,
        activity_log_prefix=activity_log_prefix,
        region=region,
        secret_access_key=SecretStr(secret_access_key),
        use_path_style=state.use_path_style,
        verify_tls=state.verify_tls,
        ca_bundle=text,
        remove_ca_bundle=state.remove_ca_bundle,
        retention_count=retention_count,
        confirm_retention=state.confirm_retention,
    )
    return SubmittedTarget(
        form=form,
        state=state,
        expected_updated_at=expected_updated_at,
        problems={} if problem is None else {"ca_bundle": problem},
    )


Submitted = Annotated[SubmittedTarget, Depends(read_target_form)]


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


def _fragment(
    request: Request, template: str, macro: str, status_code: int = 200, **arguments: Any
) -> Response:
    return no_store(render_fragment(request, template, macro, status_code=status_code, **arguments))


def _form_page(
    request: Request,
    *,
    state: FormState,
    target: TargetView | None = None,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return _page(
        request,
        "object_storage/form.html",
        {
            "state": state,
            "target": target,
            "editing": target is not None,
            "errors": errors or {},
            "warnings": target_warnings(state.endpoint_url, state.verify_tls),
            "advice": CREDENTIAL_ADVICE,
            "retention_folder": RETENTION_FOLDER,
            "retention_files": RETENTION_FILES,
        },
        status_code,
    )


async def _detail_page(
    request: Request,
    services: TargetAdmin,
    actor: Principal,
    target_id: int,
    *,
    status_code: int = 200,
    checks: tuple[CheckRow, ...] | None = None,
    test_error: str | None = None,
) -> Response:
    target = await services.get(actor, target_id)
    preview = await services.delete_preview(actor, target_id)
    return _page(
        request,
        "object_storage/detail.html",
        {
            "target": target,
            "rules": preview.rules,
            "more_rules": preview.more_rules,
            "checks": checks,
            "test_error": test_error,
            "permission_note": PERMISSION_NOTE,
            "advice": CREDENTIAL_ADVICE,
            "retention_folder": RETENTION_FOLDER,
            "retention_files": RETENTION_FILES,
        },
        status_code,
    )


async def _delete_page(
    request: Request,
    services: TargetAdmin,
    actor: Principal,
    target_id: int,
    *,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    preview = await services.delete_preview(actor, target_id)
    return _page(
        request,
        "object_storage/delete_confirm.html",
        {"preview": preview, "errors": errors or {}},
        status_code,
    )


async def _refused_form(
    request: Request,
    services: TargetAdmin,
    actor: Principal,
    submitted: SubmittedTarget,
    target_id: int | None,
    errors: dict[str, str],
) -> Response:
    """Show the form again with its field errors, or the stored values when the target moved on."""
    target = None if target_id is None else await services.get(actor, target_id)
    state = submitted.state
    if target is not None and CHANGED_FORM_FIELD in errors:
        state = _state_of(target)
    return _form_page(request, state=state, target=target, errors=errors, status_code=422)


@router.get("", response_class=HTMLResponse, name="object_storage_targets")
async def list_targets(
    request: Request,
    services: TargetAdmin,
    actor: CurrentAdministrator,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
) -> Response:
    """List the targets by name, as a page or as the table fragment."""
    try:
        page = await services.page(actor, limit=limit, cursor=cursor)
    except (InvalidCursorError, TypeError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    pager = PageView.of(
        page, base_path=route(request, "object_storage_targets"), limit=limit, cursor=cursor
    )
    if is_htmx_request(request):
        return _fragment(
            request, TABLES, "targets_table", rows=page.items, columns=COLUMNS, pager=pager
        )
    total = await services.count(actor)
    return _page(
        request,
        "object_storage/list.html",
        {"rows": page.items, "columns": COLUMNS, "pager": pager, "total": total},
    )


@router.get("/new", response_class=HTMLResponse, name="object_storage_target_new")
async def new_target_form(request: Request, actor: CurrentAdministrator) -> Response:
    """Render the create form with the usual defaults."""
    return _form_page(request, state=FormState())


@router.post("", name="object_storage_target_create")
async def create_target(
    request: Request, services: TargetAdmin, actor: CurrentAdministrator, submitted: Submitted
) -> Response:
    """Create a target, then open its page."""
    if submitted.problems:
        return await _refused_form(request, services, actor, submitted, None, submitted.problems)
    try:
        target_id = await services.create(actor, submitted.form)
    except ValidationFailed as invalid:
        return await _refused_form(request, services, actor, submitted, None, invalid.fields)
    return redirect(
        request,
        route(request, "object_storage_target_detail", target_id=target_id),
        [Flash("ok", NOTICES["created"])],
    )


@router.get("/{target_id}", response_class=HTMLResponse, name="object_storage_target_detail")
async def target_detail(
    request: Request,
    services: TargetAdmin,
    actor: CurrentAdministrator,
    target_id: PathId,
) -> Response:
    """Show a target with its warnings; the secret is only reported as stored."""
    return await _detail_page(request, services, actor, target_id)


@router.get("/{target_id}/edit", response_class=HTMLResponse, name="object_storage_target_edit")
async def edit_target_form(
    request: Request, services: TargetAdmin, actor: CurrentAdministrator, target_id: PathId
) -> Response:
    """Render the edit form; the secret and the certificates are never filled in."""
    target = await services.get(actor, target_id)
    return _form_page(request, state=_state_of(target), target=target)


@router.post("/{target_id}", name="object_storage_target_update")
async def update_target(
    request: Request,
    services: TargetAdmin,
    actor: CurrentAdministrator,
    target_id: PathId,
    submitted: Submitted,
) -> Response:
    """Save the target if it is unchanged since the form was loaded."""
    if submitted.problems:
        return await _refused_form(
            request, services, actor, submitted, target_id, submitted.problems
        )
    try:
        await services.update(
            actor, target_id, submitted.form, expected_updated_at=submitted.expected_updated_at
        )
    except ValidationFailed as invalid:
        return await _refused_form(request, services, actor, submitted, target_id, invalid.fields)
    return redirect(
        request,
        route(request, "object_storage_target_detail", target_id=target_id),
        [Flash("ok", NOTICES["updated"])],
    )


@router.post("/{target_id}/test", name="object_storage_target_test")
async def test_target(
    request: Request, services: TargetAdmin, actor: CurrentAdministrator, target_id: PathId
) -> Response:
    """Test the saved target from this server; htmx receives the result, a browser the page."""
    htmx = is_htmx_request(request)
    try:
        checks = await services.test(actor, target_id)
    except ValidationFailed as invalid:
        error = next(iter(invalid.fields.values()), "The connection could not be tested.")
        if htmx:
            return _fragment(
                request, CHECKS, "target_test_result", 422, checks=None, error=error, note=None
            )
        return await _detail_page(
            request, services, actor, target_id, status_code=422, test_error=error
        )
    rows = tuple(_check_row(check) for check in checks)
    if htmx:
        return _fragment(
            request, CHECKS, "target_test_result", checks=rows, error=None, note=PERMISSION_NOTE
        )
    return await _detail_page(request, services, actor, target_id, checks=rows)


@router.get(
    "/{target_id}/delete", response_class=HTMLResponse, name="object_storage_target_delete_confirm"
)
async def confirm_delete(
    request: Request, services: TargetAdmin, actor: CurrentAdministrator, target_id: PathId
) -> Response:
    """Name the target and the rules that stop its deletion; nothing is deleted by this page."""
    return await _delete_page(request, services, actor, target_id)


@router.post("/{target_id}/delete", name="object_storage_target_delete")
async def delete_target(
    request: Request,
    services: TargetAdmin,
    actor: CurrentAdministrator,
    target_id: PathId,
    expected_name: Text = "",
) -> Response:
    """Delete the target if no rule uses it and its name is still the reviewed one."""
    try:
        await services.delete(actor, target_id, expected_name=expected_name)
    except ValidationFailed as invalid:
        return await _delete_page(
            request, services, actor, target_id, errors=invalid.fields, status_code=422
        )
    return redirect(
        request, route(request, "object_storage_targets"), [Flash("ok", NOTICES["deleted"])]
    )
