"""Server pages: the served certificate, and the Instance page with its restart and update."""

from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Form, Path, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import (
    CurrentAdministrator,
    get_activity,
    get_runtime,
    require_administrator,
)
from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.services import AuthRuntime
from app.config import Settings, get_settings
from app.db.engine import Session, get_session
from app.db.repositories.activity_event import ActivityEventRepository
from app.db.repositories.setting import SettingRepository
from app.self_identity import source_of
from app.services.activity import ActivityRecorder
from app.services.assembly import Images, build_host_clients, build_registry_resolver
from app.services.image_updates import TooManyUpdateChecks
from app.services.instance import (
    InstanceService,
    InstanceView,
    RestartRefused,
    UpdateReview,
    UpdateSupport,
)
from app.services.object_inspector import EngineObjectInspector, EnvironmentUnavailable
from app.services.pull_credentials import CHOICE_FIELD, CredentialChoice
from app.services.self_update import UPDATE_ID_PATTERN, UpdateRefused, update_dispatcher_of
from app.services.server_tls import certificate_view
from app.tls import EXPIRY_WARNING, CertificateSummary, ExpiryState
from app.web.image_helpers import credential_choice_of, selected_choice
from app.web.navigation import no_store
from app.web.problems import unreachable_copy
from app.web.registry_providers import CATALOGUE
from app.web.templating import render_fragment, render_page

router = APIRouter(prefix="/server", dependencies=[Depends(require_administrator)])

STATE_BADGES: Final[dict[ExpiryState, tuple[str, str]]] = {
    "valid": ("Valid", "ok"),
    "expiring": (f"Expires within {EXPIRY_WARNING.days} days", "warn"),
    "expired": ("Expired", "danger"),
    "not_yet_valid": ("Not yet valid", "danger"),
}
INSTANCE_TEMPLATE: Final = "server/instance.html"
RESTART_CONFIRM_TEMPLATE: Final = "server/instance_restart_confirm.html"
RESTARTING_TEMPLATE: Final = "server/instance_restarting.html"
UPDATE_CHECK_TEMPLATE: Final = "server/instance_update_check.html"
UPDATE_CONFIRM_TEMPLATE: Final = "server/instance_update_confirm.html"
UPDATE_STATUS_TEMPLATE: Final = "server/instance_update_status.html"
UPDATE_STATUS_MACRO: Final = "update_status"
RESTART_CONFIRMED: Final = "yes"
UPDATE_CONFIRMED: Final = "yes"
TAG_MAX: Final = 128
DIGEST_MAX: Final = 80
CHOICE_MAX: Final = 20
UPDATE_STATUS_POLL_SECONDS: Final = 2
UPDATE_PROGRESS_MARGIN_SECONDS: Final = 600
TERMINAL_PHASES: Final = ("abandoned", "finished")
BOOT_POLL_SECONDS: Final = 2
RESTART_GIVE_UP_MARGIN_SECONDS: Final = 60
NO_SCRIPT_REFRESH_SECONDS: Final = 10
CHANGED_SINCE_REVIEW: Final = (
    "The container dockmin runs in changed since you reviewed it. Review it again."
)
RESTARTED_SINCE_REVIEW: Final = (
    "dockmin has restarted since this page was shown. Review the instance again."
)
RECORD_FAILED_STATUS: Final = 503
REGISTRY_FAILED_STATUS: Final = 502
TOO_MANY_CHECKS_STATUS: Final = 429
CHOICE_REQUIRED: Final = "Choose which credential set to check with."
HTTP_MODE_TEXT: Final = {
    "serve": "Serves the application",
    "redirect": "Redirects to HTTPS",
    "off": "Not listening",
}


@router.get("/certificate", name="server_certificate")
async def server_certificate(
    request: Request,
    principal: CurrentAdministrator,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Show the served certificate, or say that TLS is terminated in front of dockmin."""
    loaded: CertificateSummary | None = getattr(request.app.state, "served_certificate", None)
    certificate_path = settings.served_cert_file
    view = await certificate_view(principal, settings, loaded, datetime.now(UTC))
    context: dict[str, Any] = {
        "tls_enabled": settings.tls_enabled,
        "tls_source": settings.tls_source,
        "certificate_file": None if certificate_path is None else str(certificate_path),
        "port": settings.port,
        "https_port": settings.https_port,
        "https_public_port": settings.https_public_port,
        "http_mode": HTTP_MODE_TEXT[settings.effective_http_mode],
        "trusted_proxies": settings.trusted_proxies,
        "trusted_origins": settings.trusted_origins,
        "view": view,
    }
    if view is not None:
        label, tone = STATE_BADGES[view.state] if view.state is not None else ("", "neutral")
        context |= {"state_label": label, "state_tone": tone}
    return no_store(render_page(request, "server/certificate.html", context))


async def get_instance_service(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
    images: Images,
    settings: Annotated[Settings, Depends(get_settings)],
) -> InstanceService:
    """Provide the instance service bound to this request's session."""
    state = request.app.state
    engine = state.engine
    inspector = EngineObjectInspector(
        build_host_clients(db, runtime, engine), poke=engine.refresher.poke
    )
    registries = state.registries
    update = UpdateSupport(
        data_dir=settings.data_dir,
        health_timeout_seconds=settings.update_health_timeout_seconds,
        bindings=images.bindings,
        resolver=build_registry_resolver(db, runtime, registries),
        manifests=registries.manifests,
        checks=registries.update_checks,
        settings=SettingRepository(db, runtime.clock),
        events=ActivityEventRepository(db, runtime.clock),
        dispatcher=update_dispatcher_of(request.app),
    )
    return InstanceService(
        db=db,
        activity=activity,
        inspector=inspector,
        identity=source_of(request.app),
        gate=state.restore_gate,
        boot=state.boot,
        update=update,
    )


Instance = Annotated[InstanceService, Depends(get_instance_service)]


def _instance_context(request: Request, view: InstanceView) -> dict[str, Any]:
    app = request.app
    return {
        "view": view,
        "home_links": {
            home.environment_id: str(
                app.url_path_for("environment_detail", environment_id=home.environment_id)
            )
            for home in view.homes
        },
        "restart_confirm_href": str(app.url_path_for("server_instance_restart_confirm")),
        "update_check_href": str(app.url_path_for("server_instance_update_check")),
        "update_confirm_href": str(app.url_path_for("server_instance_update_confirm")),
        "update_form_fields": {"choice": CHOICE_FIELD, "tag": "target_tag"},
        "catalogue": CATALOGUE,
    }


def _confirm_context(
    request: Request, view: InstanceView, error: str | None = None
) -> dict[str, Any]:
    app = request.app
    return {
        **_instance_context(request, view),
        "restart_href": str(app.url_path_for("server_instance_restart")),
        "back_href": str(app.url_path_for("server_instance")),
        "confirm_value": RESTART_CONFIRMED,
        "boot_id": view.boot.boot_id,
        "error": error,
    }


@router.get("/instance", response_class=HTMLResponse, name="server_instance")
async def server_instance(
    request: Request, principal: CurrentAdministrator, instance: Instance
) -> Response:
    """Show which container dockmin runs in, how it was found, and the restart control."""
    view = await instance.view(principal, with_update=True)
    return no_store(render_page(request, INSTANCE_TEMPLATE, _instance_context(request, view)))


@router.get(
    "/instance/restart", response_class=HTMLResponse, name="server_instance_restart_confirm"
)
async def server_instance_restart_confirm(
    request: Request, principal: CurrentAdministrator, instance: Instance
) -> Response:
    """Name the container a restart would stop; nothing happens until it is posted."""
    view = await instance.view(principal)
    status_code = 200 if view.can_restart else 409
    context = _confirm_context(request, view, view.restart_message)
    return no_store(
        render_page(request, RESTART_CONFIRM_TEMPLATE, context, status_code=status_code)
    )


@router.post("/instance/restart", response_class=HTMLResponse, name="server_instance_restart")
async def server_instance_restart(
    request: Request,
    principal: CurrentAdministrator,
    instance: Instance,
    confirm: Annotated[str, Form(max_length=8)] = "",
    container_id: Annotated[str, Form(max_length=64)] = "",
    boot_id: Annotated[str, Form(max_length=64)] = "",
) -> Response:
    """Record the restart, answer with the restarting page, then ask the Engine to restart.

    The Engine call is dispatched to an application-owned task that waits briefly so the
    response reaches the browser first.
    """
    view = await instance.view(principal)

    def refused(status_code: int, message: str) -> Response:
        context = _confirm_context(request, view, message)
        return no_store(
            render_page(request, RESTART_CONFIRM_TEMPLATE, context, status_code=status_code)
        )

    if boot_id != view.boot.boot_id:
        return refused(409, RESTARTED_SINCE_REVIEW)
    if view.restart_message is not None:
        return refused(409, view.restart_message)
    if confirm != RESTART_CONFIRMED:
        return refused(400, "Confirm the restart to continue.")
    if container_id != view.container_id:
        return refused(409, CHANGED_SINCE_REVIEW)
    try:
        accepted = await instance.request_restart(principal)
    except RestartRefused as error:
        return refused(RECORD_FAILED_STATUS if error.reason == "record_failed" else 409, str(error))
    except EnvironmentUnavailable as error:
        return refused(503, unreachable_copy(error).message)
    response = render_page(
        request,
        RESTARTING_TEMPLATE,
        {
            "container_name": accepted.container_name,
            "boot_id": view.boot.boot_id,
            "boot_href": str(request.app.url_path_for("server_instance_boot")),
            "dashboard_href": str(request.app.url_path_for("index")),
            "stop_timeout": accepted.stop_timeout,
            "poll_seconds": BOOT_POLL_SECONDS,
            "give_up_seconds": accepted.stop_timeout + RESTART_GIVE_UP_MARGIN_SECONDS,
            "refresh_seconds": NO_SCRIPT_REFRESH_SECONDS,
        },
    )
    request.app.state.restart_dispatcher.dispatch(accepted)
    return no_store(response)


@router.get("/instance/boot", response_class=HTMLResponse, name="server_instance_boot")
async def server_instance_boot(request: Request) -> Response:
    """Answer with the id of this run of the process, for the restarting page to compare."""
    boot_id = request.app.state.boot.boot_id
    return no_store(HTMLResponse(f'<span id="boot-id">{boot_id}</span>'))


def _update_context(request: Request, view: InstanceView, **extra: Any) -> dict[str, Any]:
    app = request.app
    context: dict[str, Any] = {
        **_instance_context(request, view),
        "back_href": str(app.url_path_for("server_instance")),
        "check_href": str(app.url_path_for("server_instance_update_check")),
        "confirm_href": str(app.url_path_for("server_instance_update_confirm")),
        "request_href": str(app.url_path_for("server_instance_update")),
        "confirm_value": UPDATE_CONFIRMED,
        "boot_id": view.boot.boot_id,
        "selected": "",
        "check": None,
        "confirmation": None,
        "error": None,
    }
    context.update(extra)
    return context


def _update_status_code(error: UpdateRefused) -> int:
    match error.reason:
        case "record_failed":
            return RECORD_FAILED_STATUS
        case "registry_failed":
            return REGISTRY_FAILED_STATUS
        case _:
            return 409


def _choice_of(text: str) -> CredentialChoice:
    choice = credential_choice_of(text[:CHOICE_MAX])
    if choice is None:
        raise ValidationFailed({CHOICE_FIELD: CHOICE_REQUIRED})
    return choice


@router.post(
    "/instance/update/check", response_class=HTMLResponse, name="server_instance_update_check"
)
async def server_instance_update_check(
    request: Request,
    principal: CurrentAdministrator,
    instance: Instance,
    credential_choice: Annotated[str, Form(max_length=CHOICE_MAX)] = "",
) -> Response:
    """Ask the registry about dockmin's own image; the only place a registry is contacted."""
    view = await instance.view(principal, with_update=True)
    offer = None if view.update is None else view.update.offer

    def page(status_code: int, **extra: Any) -> Response:
        selected = "" if offer is None else selected_choice(offer, credential_choice)
        context = _update_context(request, view, selected=selected, **extra)
        return no_store(
            render_page(request, UPDATE_CHECK_TEMPLATE, context, status_code=status_code)
        )

    if view.update is None or not view.update.can_check:
        message = None if view.update is None else view.update.message
        return page(409, error=message or "dockmin cannot be checked for updates.")
    try:
        check = await instance.check_update(principal, _choice_of(credential_choice))
    except ValidationFailed as error:
        return page(400, error=str(error))
    except UpdateRefused as error:
        return page(_update_status_code(error), error=str(error))
    except TooManyUpdateChecks as error:
        return page(TOO_MANY_CHECKS_STATUS, error=str(error))
    except EnvironmentUnavailable as error:
        return page(503, error=unreachable_copy(error).message)
    return page(200, check=check)


@router.post(
    "/instance/update/confirm", response_class=HTMLResponse, name="server_instance_update_confirm"
)
async def server_instance_update_confirm(
    request: Request,
    principal: CurrentAdministrator,
    instance: Instance,
    target_tag: Annotated[str, Form(max_length=TAG_MAX)] = "",
    credential_choice: Annotated[str, Form(max_length=CHOICE_MAX)] = "",
) -> Response:
    """Name everything an update would replace and run; nothing happens until it is posted.

    A POST, because it reads the registry.
    """
    view = await instance.view(principal)

    def page(status_code: int, **extra: Any) -> Response:
        context = _update_context(request, view, **extra)
        return no_store(
            render_page(request, UPDATE_CONFIRM_TEMPLATE, context, status_code=status_code)
        )

    try:
        confirmation = await instance.confirm_update(
            principal, _choice_of(credential_choice), target_tag
        )
    except ValidationFailed as error:
        return page(400, error=str(error))
    except UpdateRefused as error:
        return page(_update_status_code(error), error=str(error))
    except TooManyUpdateChecks as error:
        return page(TOO_MANY_CHECKS_STATUS, error=str(error))
    except EnvironmentUnavailable as error:
        return page(503, error=unreachable_copy(error).message)
    return page(200, confirmation=confirmation)


@router.post("/instance/update", response_class=HTMLResponse, name="server_instance_update")
async def server_instance_update(
    request: Request,
    principal: CurrentAdministrator,
    instance: Instance,
    confirm: Annotated[str, Form(max_length=8)] = "",
    container_id: Annotated[str, Form(max_length=64)] = "",
    boot_id: Annotated[str, Form(max_length=64)] = "",
    current_digest: Annotated[str, Form(max_length=DIGEST_MAX)] = "",
    target_tag: Annotated[str, Form(max_length=TAG_MAX)] = "",
    target_digest: Annotated[str, Form(max_length=DIGEST_MAX)] = "",
    helper_digest: Annotated[str, Form(max_length=DIGEST_MAX)] = "",
    credential_choice: Annotated[str, Form(max_length=CHOICE_MAX)] = "",
) -> Response:
    """Record the update, answer with the restarting page, then hand the update to the helper.

    The images are pulled and the helper started by an application-owned task after the
    response, so the page is on its way before anything on the host changes.
    """
    view = await instance.view(principal)

    def refused(status_code: int, message: str) -> Response:
        context = _update_context(request, view, error=message)
        return no_store(
            render_page(request, UPDATE_CONFIRM_TEMPLATE, context, status_code=status_code)
        )

    if confirm != UPDATE_CONFIRMED:
        return refused(400, "Confirm the update to continue.")
    try:
        review = UpdateReview(
            container_id=container_id,
            boot_id=boot_id,
            current_digest=current_digest,
            target_tag=target_tag,
            target_digest=target_digest,
            helper_digest=helper_digest,
            choice=_choice_of(credential_choice),
        )
        accepted = await instance.request_update(principal, review)
    except ValidationFailed as error:
        return refused(400, str(error))
    except UpdateRefused as error:
        return refused(_update_status_code(error), str(error))
    except TooManyUpdateChecks as error:
        return refused(TOO_MANY_CHECKS_STATUS, str(error))
    except EnvironmentUnavailable as error:
        return refused(503, unreachable_copy(error).message)
    app = request.app
    dispatcher = update_dispatcher_of(app)
    try:
        response = render_page(
            request,
            RESTARTING_TEMPLATE,
            {
                "container_name": accepted.container_name,
                "boot_id": view.boot.boot_id,
                "boot_href": str(app.url_path_for("server_instance_boot")),
                "dashboard_href": str(app.url_path_for("index")),
                "stop_timeout": accepted.stop_timeout,
                "poll_seconds": BOOT_POLL_SECONDS,
                "give_up_seconds": accepted.stop_timeout
                + accepted.health_timeout_seconds
                + UPDATE_PROGRESS_MARGIN_SECONDS,
                "give_up_phase": "handed_over",
                "terminal_phases": TERMINAL_PHASES,
                "refresh_seconds": NO_SCRIPT_REFRESH_SECONDS,
                "update_id": accepted.update_id,
                "status_href": str(
                    app.url_path_for("server_instance_update_status", update_id=accepted.update_id)
                ),
                "status_poll_seconds": UPDATE_STATUS_POLL_SECONDS,
                "health_timeout_seconds": accepted.health_timeout_seconds,
            },
        )
    except BaseException:
        dispatcher.discard(accepted.update_id)
        raise
    dispatcher.dispatch(accepted.job)
    return no_store(response)


@router.get(
    "/instance/update/{update_id}",
    response_class=HTMLResponse,
    name="server_instance_update_status",
)
async def server_instance_update_status(
    request: Request,
    principal: CurrentAdministrator,
    instance: Instance,
    update_id: Annotated[str, Path(max_length=64)],
) -> Response:
    """Report where an update is: pulling, starting the helper, handed over, abandoned or done."""
    if UPDATE_ID_PATTERN.fullmatch(update_id) is None:
        raise ResourceNotFound("not found")
    status = await instance.update_status(principal, update_id)
    return no_store(
        render_fragment(
            request,
            UPDATE_STATUS_TEMPLATE,
            UPDATE_STATUS_MACRO,
            status=status,
            poll_seconds=UPDATE_STATUS_POLL_SECONDS,
            href=str(
                request.app.url_path_for("server_instance_update_status", update_id=update_id)
            ),
        )
    )
