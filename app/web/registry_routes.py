"""Registry credential set pages: list, form, detail, connection test, grants and deletion."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import SecretStr

from app.auth.dependencies import CurrentAdministrator, require_administrator
from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE, InvalidCursorError
from app.services.assembly import Registries, RegistryServices
from app.services.registries import RegistryFormState, RegistryTestReport
from app.services.registry_input import (
    PROVIDER_ORDER,
    SECRET_MAX,
    RegistrySpec,
    extras_defaults,
)
from app.web.flash import Flash
from app.web.identifiers import PathId, QueryId
from app.web.navigation import no_store, redirect
from app.web.pagination import DEFAULT_LIMIT, Column, PageView
from app.web.registry_providers import CATALOGUE
from app.web.results import show_result
from app.web.templating import is_htmx_request, render_fragment, render_page

router = APIRouter(prefix="/registries", dependencies=[Depends(require_administrator)])

REGISTRIES_PATH = "/registries"
FIELD_MAX = 512
BUNDLE_MAX = 64 * 1024
ID_DIGITS = 18
GRANTEE_KINDS = ("user", "team")
YES = "yes"
INCOMPLETE = "The confirmation was incomplete. Review it again."

TABLES = "registries/_tables.html"
FORM_PARTS = "registries/_form.html"
PARTS = "registries/_parts.html"

NOTICES = {
    "saved": "Changes saved.",
    "environment-granted": "Environment granted.",
    "environment-removed": "Environment removed.",
    "access-granted": "Access granted.",
    "access-removed": "Access removed.",
    "default-set": "Marked as the default for its registry address.",
    "default-cleared": "No longer the default for its registry address.",
}

COLUMNS = (
    Column("name", "Name"),
    Column("provider", "Provider", width="short"),
    Column("address", "Registry address"),
    Column("authentication", "Authentication", width="badge"),
    Column("environments", "Environments", numeric=True),
    Column("grantees", "Users and teams", numeric=True),
)

Field = Annotated[str, Form(max_length=FIELD_MAX)]


@dataclass(slots=True)
class SubmittedRegistry:
    """The registry form as posted; the secret and the certificate text are never echoed."""

    spec: RegistrySpec = field(repr=False)
    credential_id: str


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


def _fragment(
    request: Request, template: str, macro: str, status_code: int = 200, **arguments: Any
) -> Response:
    return no_store(render_fragment(request, template, macro, status_code=status_code, **arguments))


def _back_to(request: Request, credential_id: int, notice: str) -> Response:
    return redirect(
        request, f"{REGISTRIES_PATH}/{credential_id}", flashes=[Flash("ok", NOTICES[notice])]
    )


def _digits(value: str) -> bool:
    return value.isascii() and value.isdigit() and 0 < len(value) <= ID_DIGITS


async def _bundle_text(text: str, upload: UploadFile | None) -> tuple[str, str | None]:
    """Return the CA certificates from the file when one was chosen, else the pasted text."""
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


def _extras(provider: str, raw: Mapping[str, str]) -> dict[str, str | bool]:
    """Keep only the extras the chosen provider has, with its flags read from checkboxes."""
    if provider not in PROVIDER_ORDER:
        return {}
    chosen: dict[str, str | bool] = {}
    for name, default in extras_defaults(provider).items():
        value = raw.get(name, "")
        chosen[name] = value == "on" if isinstance(default, bool) else value
    return chosen


async def read_registry_form(
    name: Field = "",
    provider: Field = "",
    address: Field = "",
    authentication_enabled: Field = "",
    username: Field = "",
    secret: Annotated[str, Form(max_length=SECRET_MAX + 1)] = "",
    verify_tls: Field = "",
    remove_ca_bundle: Field = "",
    ca_bundle: Annotated[str, Form()] = "",
    ca_bundle_file: Annotated[UploadFile | None, File()] = None,
    organisation: Field = "",
    use_organisation: Field = "",
    instance_url: Field = "",
    project_id: Field = "",
    project_path: Field = "",
    region: Field = "",
    edited_id: Annotated[str, Form(alias="credential_id", max_length=FIELD_MAX)] = "",
) -> SubmittedRegistry:
    """Read the registry form, taking the CA certificates from the file when one was chosen."""
    text, problem = await _bundle_text(ca_bundle, ca_bundle_file)
    raw = {
        "organisation": organisation,
        "use_organisation": use_organisation,
        "instance_url": instance_url,
        "project_id": project_id,
        "project_path": project_path,
        "region": region,
    }
    spec = RegistrySpec(
        name=name,
        provider=provider,
        address=address,
        authentication_enabled=authentication_enabled == "on",
        username=username,
        secret=SecretStr(secret),
        verify_tls=verify_tls == "on",
        ca_bundle=text,
        ca_bundle_problem=problem or "",
        remove_ca_bundle=remove_ca_bundle == "on",
        extras=_extras(provider, raw),
    )
    return SubmittedRegistry(spec=spec, credential_id=edited_id)


Submitted = Annotated[SubmittedRegistry, Depends(read_registry_form)]


def _form_page(
    request: Request,
    state: RegistryFormState,
    *,
    fields: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return _page(
        request,
        "registries/form.html",
        {
            "state": state,
            "editing": state.credential_id is not None,
            "catalogue": CATALOGUE,
            "fields": fields or {},
        },
        status_code,
    )


async def _refused_form(
    request: Request,
    services: RegistryServices,
    actor: Principal,
    submitted: SubmittedRegistry,
    credential_id: int | None,
    fields: dict[str, str],
) -> Response:
    state = await services.credentials.form_state(
        actor, submitted.spec, credential_id=credential_id
    )
    return _form_page(request, state, fields=fields, status_code=422)


async def _detail_page(
    request: Request,
    services: RegistryServices,
    actor: Principal,
    credential_id: int,
    *,
    status_code: int = 200,
    fields: dict[str, str] | None = None,
    report: RegistryTestReport | None = None,
    test_error: str | None = None,
    grantee_kind: str = "user",
    grantee_name: str = "",
    environment_name: str = "",
) -> Response:
    detail = await services.credentials.detail(actor, credential_id)
    return _page(
        request,
        "registries/detail.html",
        {
            "detail": detail,
            "catalogue": CATALOGUE,
            "fields": fields or {},
            "report": report,
            "test_error": test_error,
            "grantee_kind": grantee_kind,
            "grantee_name": grantee_name,
            "environment_name": environment_name,
        },
        status_code,
    )


async def _delete_page(
    request: Request,
    services: RegistryServices,
    actor: Principal,
    credential_id: int,
    *,
    fields: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    impact = await services.credentials.delete_impact(actor, credential_id)
    return _page(
        request,
        "registries/delete_confirm.html",
        {"impact": impact, "catalogue": CATALOGUE, "fields": fields or {}},
        status_code,
    )


@router.get("", response_class=HTMLResponse, name="registries")
async def list_registries(
    request: Request,
    services: Registries,
    actor: CurrentAdministrator,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Response:
    """List the credential sets by name, as a page or as the table fragment."""
    try:
        page = await services.credentials.page(actor, limit=limit, cursor=cursor)
    except (InvalidCursorError, TypeError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    pager = PageView.of(page, base_path=REGISTRIES_PATH, limit=limit, cursor=cursor)
    if is_htmx_request(request):
        return _fragment(
            request,
            TABLES,
            "registries_table",
            rows=page.items,
            columns=COLUMNS,
            pager=pager,
            catalogue=CATALOGUE,
        )
    total = await services.credentials.count(actor)
    return _page(
        request,
        "registries/list.html",
        {
            "rows": page.items,
            "columns": COLUMNS,
            "pager": pager,
            "catalogue": CATALOGUE,
            "total": total,
        },
    )


@router.get("/new", response_class=HTMLResponse, name="registry_new")
async def new_registry_form(
    request: Request,
    services: Registries,
    actor: CurrentAdministrator,
    provider: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
) -> Response:
    """Render the create form for the chosen provider, Docker Hub when none is chosen."""
    state = await services.credentials.form_context(actor, provider=provider)
    return _form_page(request, state)


@router.post("/form-fields", name="registry_form_fields")
async def form_fields(
    request: Request, services: Registries, actor: CurrentAdministrator, submitted: Submitted
) -> Response:
    """Render the provider-dependent fields for the chosen provider; nothing is saved."""
    credential_id = int(submitted.credential_id) if _digits(submitted.credential_id) else None
    spec = submitted.spec
    state = await services.credentials.form_context(
        actor, credential_id=credential_id, provider=spec.provider
    )
    state = replace(
        state,
        name=spec.name,
        username=spec.username,
        authentication_enabled=spec.authentication_enabled or not state.shape.anonymous_allowed,
    )
    if not is_htmx_request(request):
        return _form_page(request, state)
    return _fragment(
        request,
        FORM_PARTS,
        "provider_fields",
        state=state,
        presentation=CATALOGUE[state.provider],
        fields={},
    )


@router.post("", name="registry_create")
async def create_registry(
    request: Request, services: Registries, actor: CurrentAdministrator, submitted: Submitted
) -> Response:
    """Create a credential set, then open its page, where any duplicate warning shows."""
    try:
        created = await services.credentials.create(actor, submitted.spec)
    except ValidationFailed as invalid:
        return await _refused_form(request, services, actor, submitted, None, invalid.fields)
    return redirect(request, f"{REGISTRIES_PATH}/{created.id}")


@router.get("/{credential_id}", response_class=HTMLResponse, name="registry_detail")
async def registry_detail(
    request: Request,
    services: Registries,
    actor: CurrentAdministrator,
    credential_id: PathId,
) -> Response:
    """Show a credential set with its grants; the secret is reported only as stored."""
    return await _detail_page(request, services, actor, credential_id)


@router.get("/{credential_id}/edit", response_class=HTMLResponse, name="registry_edit")
async def edit_registry_form(
    request: Request,
    services: Registries,
    actor: CurrentAdministrator,
    credential_id: PathId,
    provider: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
) -> Response:
    """Render the edit form; the secret and the certificates are never filled in."""
    state = await services.credentials.form_context(
        actor, credential_id=credential_id, provider=provider
    )
    return _form_page(request, state)


@router.post("/{credential_id}/edit", name="registry_update")
async def update_registry(
    request: Request,
    services: Registries,
    actor: CurrentAdministrator,
    credential_id: PathId,
    submitted: Submitted,
) -> Response:
    """Save the credential set; a blank secret keeps the stored one where that is safe."""
    try:
        await services.credentials.update(actor, credential_id, submitted.spec)
    except ValidationFailed as invalid:
        return await _refused_form(
            request, services, actor, submitted, credential_id, invalid.fields
        )
    return _back_to(request, credential_id, "saved")


@router.post("/{credential_id}/test", name="registry_test")
async def test_registry(
    request: Request, services: Registries, actor: CurrentAdministrator, credential_id: PathId
) -> Response:
    """Test the saved set from this server; htmx receives the staged report, a browser the page."""
    htmx = is_htmx_request(request)
    try:
        report = await services.credentials.test_connection(actor, credential_id)
    except ValidationFailed as invalid:
        error = next(iter(invalid.fields.values()), "The connection could not be tested.")
        if htmx:
            return _fragment(request, PARTS, "registry_test_result", 422, report=None, error=error)
        return await _detail_page(
            request, services, actor, credential_id, status_code=422, test_error=error
        )
    if htmx:
        return _fragment(request, PARTS, "registry_test_result", report=report, error=None)
    return await _detail_page(request, services, actor, credential_id, report=report)


@router.post("/{credential_id}/environments", name="registry_environment_grant")
async def grant_environment(
    request: Request,
    services: Registries,
    actor: CurrentAdministrator,
    credential_id: PathId,
    environment_name: Field = "",
) -> Response:
    """Grant the set to the environment the form names."""
    try:
        if not environment_name.strip():
            raise ValidationFailed({"environment": "Enter an environment name."})
        reference = await services.credentials.resolve_environment(actor, environment_name)
        await services.credentials.grant_environment(actor, credential_id, reference.id)
    except ValidationFailed as invalid:
        return await _detail_page(
            request,
            services,
            actor,
            credential_id,
            status_code=422,
            fields=invalid.fields,
            environment_name=environment_name,
        )
    return _back_to(request, credential_id, "environment-granted")


@router.post("/{credential_id}/environments/remove", name="registry_environment_revoke")
async def revoke_environment(
    request: Request,
    services: Registries,
    actor: CurrentAdministrator,
    credential_id: PathId,
    environment_id: QueryId = None,
) -> Response:
    """Withdraw the set from an environment; the environment is named in the query."""
    try:
        if environment_id is None:
            raise ValidationFailed({"environment": "Choose an environment to remove."})
        await services.credentials.revoke_environment(actor, credential_id, environment_id)
    except ValidationFailed as invalid:
        return await _detail_page(
            request, services, actor, credential_id, status_code=422, fields=invalid.fields
        )
    return _back_to(request, credential_id, "environment-removed")


@router.post("/{credential_id}/access", name="registry_access_grant")
async def grant_access(
    request: Request,
    services: Registries,
    actor: CurrentAdministrator,
    credential_id: PathId,
    grantee_kind: Field = "user",
    grantee_name: Field = "",
) -> Response:
    """Let the user or team the form names use the set."""
    text = grantee_name.strip()
    try:
        if grantee_kind not in GRANTEE_KINDS:
            raise ValidationFailed({"grantee": "Choose whether the grant is for a user or a team."})
        if not text:
            raise ValidationFailed(
                {"grantee": "Enter a username." if grantee_kind == "user" else "Enter a team name."}
            )
        if grantee_kind == "user":
            grantee = await services.credentials.resolve_grantee(actor, username=text)
        else:
            grantee = await services.credentials.resolve_grantee(actor, team_name=text)
        await services.credentials.grant(
            actor, credential_id, user_id=grantee.user_id, team_id=grantee.team_id
        )
    except ValidationFailed as invalid:
        return await _detail_page(
            request,
            services,
            actor,
            credential_id,
            status_code=422,
            fields=invalid.fields,
            grantee_kind=grantee_kind,
            grantee_name=grantee_name,
        )
    return _back_to(request, credential_id, "access-granted")


@router.post("/{credential_id}/access/remove", name="registry_access_revoke")
async def revoke_access(
    request: Request,
    services: Registries,
    actor: CurrentAdministrator,
    credential_id: PathId,
    user_id: QueryId = None,
    team_id: QueryId = None,
) -> Response:
    """Withdraw a user's or a team's grant on the set; the grantee is named in the query."""
    try:
        await services.credentials.revoke(actor, credential_id, user_id=user_id, team_id=team_id)
    except ValidationFailed as invalid:
        return await _detail_page(
            request, services, actor, credential_id, status_code=422, fields=invalid.fields
        )
    return _back_to(request, credential_id, "access-removed")


@router.get("/{credential_id}/delete", response_class=HTMLResponse, name="registry_delete_confirm")
async def confirm_delete(
    request: Request, services: Registries, actor: CurrentAdministrator, credential_id: PathId
) -> Response:
    """List what deleting the set takes away; nothing is deleted by this page."""
    return await _delete_page(request, services, actor, credential_id)


@router.post("/{credential_id}/default", name="registry_default")
async def set_default(
    request: Request,
    services: Registries,
    actor: CurrentAdministrator,
    credential_id: PathId,
    enabled: Field = "",
) -> Response:
    """Mark the set as its address's default, or clear the mark; the default only preselects."""
    make_default = enabled == YES
    try:
        await services.credentials.set_default_for_host(actor, credential_id, enabled=make_default)
    except ValidationFailed as invalid:
        return await _detail_page(
            request, services, actor, credential_id, status_code=422, fields=invalid.fields
        )
    return _back_to(request, credential_id, "default-set" if make_default else "default-cleared")


@router.post("/{credential_id}/delete", name="registry_delete")
async def delete_registry(
    request: Request,
    services: Registries,
    actor: CurrentAdministrator,
    credential_id: PathId,
    expected_name: Field = "",
    expected_bindings: Annotated[str, Form(max_length=ID_DIGITS)] = "",
) -> Response:
    """Delete the set if its name and the number of bound images are still the reviewed ones.

    The bound images stay and read "credential set deleted".
    """
    try:
        if not _digits(expected_bindings):
            raise ValidationFailed({"bindings": INCOMPLETE})
        count = int(expected_bindings)
        await services.credentials.delete(
            actor, credential_id, expected_name=expected_name, expected_bindings=count
        )
    except ValidationFailed as invalid:
        return await _delete_page(
            request, services, actor, credential_id, fields=invalid.fields, status_code=422
        )
    return show_result(
        request, "registries/delete_result.html", {"name": expected_name, "bindings": count}
    )
