"""Application factory, lifespan, and error handling."""

import asyncio
import secrets
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from typing import Literal, Self

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import State
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import activity as activity_api
from app.api import containers as containers_api
from app.api import environments as environments_api
from app.api import health
from app.api import hooks as hooks_api
from app.api import jobs as jobs_api
from app.api import rules as rules_api
from app.api import settings as public_settings
from app.api import stacks as stacks_api
from app.auth.bearer import is_api_path
from app.auth.bootstrap import announce_setup_token, issue_setup_token
from app.auth.dependencies import gate_setup, resolve_principal
from app.auth.errors import (
    NotFound,
    PermissionDenied,
    RedirectRequired,
    SelfProtected,
)
from app.auth.passwords import HashingBusy
from app.auth.services import AuthRuntime
from app.config import Settings, load_settings
from app.crypto.managed_key import ManagedKeyRefused, mount_point, resolve_master_keys
from app.crypto.startup import KeyMismatchError, establish_vault
from app.db import initialise_database
from app.db.engine import DatabaseError
from app.db.migrations import MigrationError
from app.db.repositories.setting import RESTORE_AUTOMATION_PAUSED_KEY, SettingRepository
from app.db.repositories.user import UserRepository
from app.instance_lock import InstanceLock, InstanceLocked
from app.logging import (
    REQUEST_ID_HEADER,
    AccessLogMiddleware,
    RequestIdMiddleware,
    bound_request_id,
    configure_logging,
    get_logger,
)
from app.netpolicy import AddressPolicy, configure
from app.notifications.poker import NotificationPoker
from app.paths import FAVICON_PATH
from app.scheduler.setup import build_scheduler_runtime
from app.self_identity import (
    SelfIdentity,
    SelfIdentityResolver,
    source_of,
)
from app.selfcheck import (
    StartupCheckFailed,
    check_buildx,
    check_compose,
    check_data_directory,
    check_sqlite_tmpdir,
    compose_directories_unusable,
    from_compose_binary_error,
    from_database_error,
    from_instance_lock_error,
    from_key_mismatch,
    from_managed_key_refusal,
    log_check_failure,
    workdir_unusable,
)
from app.services.activity import (
    install_environment_change_hook,
    install_notification_hook,
    remove_environment_change_hook,
    remove_notification_hook,
)
from app.services.backups import BackupProduction
from app.services.compose_setup import ComposeBinaryInvalid, ComposeRuntime
from app.services.encryption import EncryptionService
from app.services.engine_setup import build_engine_runtime, prepare_engine_workdir
from app.services.instance import ProcessBoot, RestartDispatcher
from app.services.registry_setup import build_registry_runtime
from app.services.restore import (
    RestoreCoordinator,
    RestoreRuntime,
    restore_leftover,
    sweep_restore_leftovers,
)
from app.services.restore_gate import RestoreGate, RestoreGateMiddleware
from app.services.self_identity_probe import EngineSelfProbe
from app.services.self_update import finish_updates
from app.services.stacks import sweep_orphan_directories
from app.services.volume_import_store import sweep_import_leftovers
from app.services.webhooks import WebhookCallLimiter
from app.subsystems.activity_log_exporter import ActivityLogExporter
from app.subsystems.backup_syncer import BackupSyncer
from app.subsystems.base import SubsystemDeps, SubsystemFactory
from app.subsystems.container_health_checker import ContainerHealthChecker
from app.subsystems.container_health_watch import HealthEventWatchers
from app.subsystems.container_log_exporter import ContainerLogExporter
from app.subsystems.container_updater import ContainerUpdater
from app.subsystems.environment_snapshot_refresher import EnvironmentSnapshotRefresher
from app.subsystems.notification_sender import INTERVAL_SECONDS, NotificationSender
from app.subsystems.stack_restarter import StackRestarter
from app.subsystems.webhook_receiver import WebhookReceiver
from app.version import build_revision, product_version
from app.web import (
    activity_routes,
    auth_routes,
    automation_routes,
    automation_webhook_routes,
    backup_routes,
    container_create_routes,
    container_export_routes,
    container_job_routes,
    container_routes,
    container_stream_routes,
    container_terminal_routes,
    encryption_routes,
    environment_routes,
    group_routes,
    image_routes,
    image_stream_routes,
    job_routes,
    network_routes,
    notification_routes,
    object_storage_routes,
    onboarding_routes,
    pages,
    registry_routes,
    restore_routes,
    results,
    server_routes,
    stack_dismissal_routes,
    stack_export_routes,
    stack_file_routes,
    stack_log_routes,
    stack_read_routes,
    stack_routes,
    stack_stream_routes,
    stack_variable_routes,
    suggestion_routes,
    swarm_node_routes,
    swarm_object_routes,
    swarm_routes,
    swarm_service_routes,
    swarm_stack_manage_routes,
    swarm_stack_routes,
    swarm_stream_routes,
    tag_routes,
    team_routes,
    template_catalogue_routes,
    template_routes,
    token_routes,
    user_routes,
    volume_export_routes,
    volume_import_routes,
    volume_routes,
)
from app.web.csrf import enforce_csrf
from app.web.flash import FLASH_KEY_BYTES
from app.web.limits import (
    BodySizeLimitMiddleware,
    ConcurrencyLimitMiddleware,
    SendIdleTimeoutMiddleware,
)
from app.web.navigation import no_store
from app.web.problems import ErrorCopy, error_copy
from app.web.queued_cookies import QueuedCookiesMiddleware
from app.web.rate_limit import AddressRateLimiter, limit_anonymous, unenforced_listener
from app.web.security_headers import STARTUP_FAILURE_ATTRIBUTE, DockminApp
from app.web.sidebar import resolve_navigation
from app.web.templating import (
    STATIC_DIR,
    build_templates,
    is_htmx_request,
    render_page,
    render_partial,
)

logger = get_logger(__name__)

type StartPhase = Literal["startup", "restore"]

DRAIN_SECONDS = 15.0
_RUNTIME_ATTRIBUTES = (
    "database",
    "vault",
    "running_key",
    "engine",
    "registries",
    "scheduler",
    "health_watchers",
    "self_identity",
    "self_identity_resolver",
    "environment_change_hook",
    "notification_poker",
    "update_dispatcher",
)


class RequestsNotDrained(RuntimeError):
    """Requests admitted before a restore's swap were still running when the wait ended."""


async def on_startup(app: FastAPI) -> None:
    """Check the data directory, take the instance lock, clear leftovers, build the runtime."""
    settings: Settings = app.state.settings
    try:
        await asyncio.to_thread(check_data_directory, settings.data_dir)
    except StartupCheckFailed as failure:
        log_check_failure(failure)
        raise
    app.state.instance_lock = _take_instance_lock(app)
    if settings.key_source == "generated" and not await asyncio.to_thread(
        mount_point, settings.data_dir
    ):
        logger.warning(
            "data_directory_not_mounted",
            extra={
                "path": str(settings.data_dir),
                "note": "Data kept in the container's writable layer is lost when the container "
                "is replaced; mount a volume there",
            },
        )
    if (listener := unenforced_listener(settings)) is not None:
        logger.warning("rate_limit_not_enforced", extra={"listener": listener})
    if not settings.cookie_secure:
        logger.warning("cookie_secure_disabled")
    await _sweep_leftovers(app)
    app.url_path_for("index")  # builds the route tree, so the first request does not pay for it
    await start_runtime(app)


def _forget_runtime(app: FastAPI) -> None:
    """Drop what a stopped runtime left on the application, so nothing of it is used again."""
    for name in _RUNTIME_ATTRIBUTES:
        if hasattr(app.state, name):
            delattr(app.state, name)


async def _sweep_leftovers(app: FastAPI) -> None:
    try:
        await asyncio.to_thread(sweep_restore_leftovers, app.state.settings.data_dir)
    except OSError as error:
        logger.error("restore_sweep_failed", extra={"error": type(error).__name__})
    try:
        await asyncio.to_thread(sweep_import_leftovers, app.state.settings.data_dir)
    except OSError as error:
        logger.error("volume_import_sweep_failed", extra={"error": type(error).__name__})


async def start_runtime(app: FastAPI, *, during: StartPhase = "startup") -> None:
    """Build everything the application serves with, except the instance lock.

    Must run after the instance lock is taken.
    """
    _forget_runtime(app)
    configure(AddressPolicy.from_settings(app.state.settings))
    try:
        await asyncio.to_thread(check_sqlite_tmpdir)
    except StartupCheckFailed as failure:
        raise _reported(failure, during) from failure
    try:
        app.state.database = await initialise_database(app.state.settings)
    except (MigrationError, sqlite3.Error, OSError) as error:
        raise _reported(from_database_error(error), during) from error
    try:
        await asyncio.to_thread(prepare_engine_workdir, app.state.settings.data_dir)
    except (ValueError, OSError) as error:
        raise _reported(workdir_unusable(), during) from error
    try:
        keys = await resolve_master_keys(
            app.state.settings, app.state.database, restore_leftover=restore_leftover
        )
    except ManagedKeyRefused as error:
        raise _reported(from_managed_key_refusal(error), during) from error
    except (sqlite3.Error, DatabaseError) as error:
        raise _reported(from_database_error(error, during_key_check=True), during) from error
    try:
        app.state.vault = await establish_vault(
            app.state.database,
            keys,
            allow_rotation=app.state.settings.allow_key_rotation,
            key_description=app.state.settings.key_description,
            previous_key_description=app.state.settings.previous_key_description,
        )
    except KeyMismatchError as error:
        raise _reported(from_key_mismatch(error), during) from error
    except (sqlite3.Error, DatabaseError) as error:
        raise _reported(from_database_error(error, during_key_check=True), during) from error
    app.state.running_key = keys.current
    # Registered here because the check needs the database, which exists only after migration.
    app.state.readiness_checks["database"] = health.database_check(app.state.database)
    # Built now so the first unknown-username login costs the same as every later one.
    await app.state.auth.hasher.prepare()
    await _prepare_bootstrap(app)
    compose = await asyncio.to_thread(_prepare_compose, app, during)
    await sweep_orphan_directories(app.state.database, compose.files, app.state.auth.clock)
    app.state.engine = build_engine_runtime(
        database=app.state.database,
        vault=app.state.vault,
        snapshot_interval_seconds=app.state.settings.snapshot_interval_seconds,
        clock=app.state.auth.clock,
        compose=compose,
        instance_name=app.state.settings.instance_name,
    )
    app.state.environment_change_hook = app.state.engine.refresher.poke
    install_environment_change_hook(app.state.environment_change_hook)
    await compose.detect()
    if (unusable := check_compose(compose)) is not None:
        log_check_failure(unusable, during=during)
    if (no_buildx := check_buildx(available=compose.buildx.available)) is not None:
        log_check_failure(no_buildx, during=during)
    app.state.registries = build_registry_runtime(
        database=app.state.database, vault=app.state.vault, clock=app.state.auth.clock
    )
    await _start_self_identity(app)
    await finish_updates(app)
    await _start_scheduler(app)


async def _start_self_identity(app: FastAPI) -> None:
    """Resolve which container the process runs in before any automation can act."""

    def publish(identity: SelfIdentity) -> None:
        app.state.self_identity = identity

    resolver = SelfIdentityResolver(probe=EngineSelfProbe(app), publish=publish)
    app.state.self_identity_resolver = resolver
    identity = await resolver.resolve()
    if identity.containerised:
        logger.info(
            "self_identity_resolved",
            extra={
                "source": identity.source,
                "verified_environments": len(identity.verified),
                "restartable": identity.restartable_via is not None,
                "unreadable_environments": len(identity.unreadable),
            },
        )
    resolver.watch()


def _reported(failure: StartupCheckFailed, during: StartPhase = "startup") -> StartupCheckFailed:
    """Log a failed check's one line and return it for raising."""
    log_check_failure(failure, during=during)
    return failure


def _take_instance_lock(app: FastAPI) -> InstanceLock:
    """Take the data directory's exclusive lock, which a second process cannot share."""
    lock = InstanceLock(app.state.settings.data_dir)
    try:
        lock.acquire()
    except (InstanceLocked, OSError) as error:
        raise _reported(from_instance_lock_error(error)) from error
    return lock


async def _start_scheduler(app: FastAPI) -> None:
    """Register the subsystems, ensure the snapshot job, seed suppressions, start the loop.

    The health event watchers start last: they only ask the running scheduler to move a fire.
    """
    settings: Settings = app.state.settings
    if settings.development_checks:
        logger.warning("development_checks_enabled")
    runtime = build_scheduler_runtime(
        database=app.state.database,
        settings=settings,
        clock=app.state.auth.clock,
        locks=app.state.engine.locks,
    )
    app.state.scheduler = runtime
    deps = SubsystemDeps(
        database=app.state.database,
        engine=app.state.engine,
        auth=app.state.auth,
        settings=settings,
        suppressions=runtime.suppressions,
        registries=getattr(app.state, "registries", None),
        backup_production=getattr(app.state, "backup_production", None),
    )
    refresher = EnvironmentSnapshotRefresher(deps)
    runtime.handlers.register(refresher)
    self_identity = source_of(app)
    runtime.handlers.register(ContainerHealthChecker(deps, self_identity=self_identity))
    runtime.handlers.register(StackRestarter(deps, self_identity=self_identity))
    runtime.handlers.register(ContainerUpdater(deps, self_identity=self_identity))
    runtime.handlers.register(WebhookReceiver(deps, self_identity=self_identity))
    runtime.handlers.register(ActivityLogExporter(deps))
    runtime.handlers.register(ContainerLogExporter(deps))
    runtime.handlers.register(BackupSyncer(deps))
    poker = NotificationPoker()
    app.state.notification_poker = poker
    sender = NotificationSender(deps, poker=poker)
    runtime.handlers.register(sender)
    for factory in app.state.extra_subsystems:
        runtime.handlers.replace(factory(deps))
    await runtime.scheduler.ensure_infrastructure_job(
        refresher.name,
        kind="interval",
        expression=str(settings.snapshot_interval_seconds),
        catch_up=refresher.catch_up,
        overrun_policy=refresher.overrun_policy,
        failure_threshold=refresher.failure_threshold,
    )
    sending = await runtime.scheduler.ensure_infrastructure_job(
        sender.name,
        kind="interval",
        expression=str(INTERVAL_SECONDS),
        catch_up=sender.catch_up,
        overrun_policy=sender.overrun_policy,
        failure_threshold=sender.failure_threshold,
    )
    poker.bind(partial(runtime.scheduler.expedite, sending.id))
    await runtime.suppressions.seed(app.state.database)
    if await _automation_paused(app):
        gate: RestoreGate | None = getattr(app.state, "restore_gate", None)
        if gate is not None:
            gate.hold_automation()
        coordinator: RestoreCoordinator | None = getattr(app.state, "restore", None)
        if coordinator is not None:
            await coordinator.hold_automation(runtime.scheduler)
    await runtime.scheduler.start()
    install_notification_hook(poker.poke)
    app.state.health_watchers = HealthEventWatchers(
        database=app.state.database,
        auth=app.state.auth,
        engine=app.state.engine,
        scheduler=runtime.scheduler,
        clock=app.state.auth.clock,
    )
    await app.state.health_watchers.start()


async def _automation_paused(app: FastAPI) -> bool:
    """Report whether the database carries the pause a restore left."""
    async with app.state.database.session() as db:
        setting = await SettingRepository(db, app.state.auth.clock).get(
            RESTORE_AUTOMATION_PAUSED_KEY
        )
    return setting is not None


def _prepare_compose(app: FastAPI, during: StartPhase) -> ComposeRuntime:
    """Create the Compose work and stack directories and locate the binary; it is not run yet."""
    settings: Settings = app.state.settings
    try:
        runtime = ComposeRuntime(
            data_dir=settings.data_dir,
            configured=settings.compose_binary,
            timeout_seconds=float(settings.compose_timeout_seconds),
            on_version=partial(setattr, app.state, "compose_version"),
        )
        runtime.find_buildx(settings.buildx_binary)
        return runtime
    except ComposeBinaryInvalid as error:
        raise _reported(from_compose_binary_error(error), during) from error
    except (OSError, ValueError) as error:
        raise _reported(compose_directories_unusable(), during) from error


async def _prepare_bootstrap(app: FastAPI) -> None:
    """Issue and announce a setup token unless an administrator already exists."""
    runtime: AuthRuntime = app.state.auth
    async with app.state.database.session() as db:
        has_administrator = await UserRepository(db).count_administrators() > 0
    if has_administrator:
        runtime.setup.complete = True
        return
    announce_setup_token(issue_setup_token(runtime.setup))


async def _attempt(step: str, closing: Awaitable[None]) -> None:
    """Await one shutdown step; a failure is logged so the steps after it still run."""
    try:
        await closing
    except Exception as exc:
        logger.error("shutdown_step_failed", extra={"step": step, "error": type(exc).__name__})


async def _drain_requests(gate: RestoreGate) -> None:
    if not await gate.drain(DRAIN_SECONDS):
        logger.warning("restore_requests_not_drained", extra={"requests": gate.inflight})
        raise RequestsNotDrained


async def stop_runtime(app: FastAPI) -> None:
    """Close what start_runtime built, leaving the instance lock held.

    The event watchers close first, then the scheduler: its runs use the services below it. A step
    that fails does not stop the ones after it. During a restore's swap the database stays open
    until the requests already admitted have finished or the wait ends; then it closes for good
    and is dropped from the application, and RequestsNotDrained is raised if requests remained.
    """
    updates = getattr(app.state, "update_dispatcher", None)
    if updates is not None:
        updates.mark_stopping()
        await _attempt("update_dispatcher", updates.aclose())
    watchers = getattr(app.state, "health_watchers", None)
    if watchers is not None:
        await _attempt("health_watchers", watchers.aclose())
    resolver = getattr(app.state, "self_identity_resolver", None)
    if resolver is not None:
        await _attempt("self_identity", resolver.aclose())
    hook = getattr(app.state, "environment_change_hook", None)
    if hook is not None:
        remove_environment_change_hook(hook)
    poker = getattr(app.state, "notification_poker", None)
    if poker is not None:
        remove_notification_hook(poker.poke)
        await _attempt("notification_poker", poker.aclose())
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        await _attempt("scheduler", scheduler.scheduler.aclose())
    engine = getattr(app.state, "engine", None)
    if engine is not None:
        await asyncio.gather(
            _attempt("image_jobs", engine.image_jobs.aclose()),
            _attempt("stack_jobs", engine.stack_jobs.aclose()),
        )
        if engine.compose is not None:
            await _attempt("compose", engine.compose.aclose())
    registries = getattr(app.state, "registries", None)
    if registries is not None:
        await _attempt("registries", registries.aclose())
    if engine is not None:
        await _attempt("engine", engine.aclose())
    gate = getattr(app.state, "restore_gate", None)
    drained = True
    if gate is not None and gate.phase == "swapping":
        try:
            await _drain_requests(gate)
        except RequestsNotDrained:
            drained = False
    database = getattr(app.state, "database", None)
    if database is not None:
        await _attempt("database", database.aclose())
        del app.state.database
    if not drained:
        raise RequestsNotDrained


async def on_shutdown(app: FastAPI) -> None:
    """Release resources acquired in on_startup, after in-flight requests finish.

    A restore in progress is stopped first. A step that fails does not stop the ones after it,
    and the instance lock is released whatever happens.
    """
    try:
        dispatcher = getattr(app.state, "restart_dispatcher", None)
        if dispatcher is not None:
            dispatcher.mark_stopping()
        encryption = getattr(app.state, "encryption", None)
        if encryption is not None:
            await _attempt("encryption", encryption.aclose())
        coordinator = getattr(app.state, "restore", None)
        if coordinator is not None:
            await _attempt("restore", coordinator.aclose())
        await _attempt("runtime", stop_runtime(app))
        if coordinator is not None:
            await _attempt("automation_hold", coordinator.release_hold())
    finally:
        instance_lock = getattr(app.state, "instance_lock", None)
        if instance_lock is not None:
            instance_lock.release()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run startup hooks, serve, then run shutdown hooks."""
    try:
        await on_startup(app)
    except BaseException as error:
        if isinstance(error, StartupCheckFailed):
            setattr(app.state, STARTUP_FAILURE_ATTRIBUTE, error)
        await on_shutdown(app)
        raise
    settings: Settings = app.state.settings
    logger.info(
        "application_started",
        extra={
            "version": product_version(),
            "revision": build_revision(),
            "instance_name": settings.instance_name,
            "http_mode": settings.effective_http_mode,
            "https_port": settings.https_port if settings.tls_enabled else None,
            "http_port": None if settings.effective_http_mode == "off" else settings.port,
            "access_log": settings.access_log,
        },
    )
    try:
        yield
    finally:
        await on_shutdown(app)
        logger.info("application_stopped")


def _error_response(
    request: Request,
    status_code: int,
    headers: Mapping[str, str] | None = None,
    fields: list[dict[str, object]] | None = None,
    copy: ErrorCopy | None = None,
) -> Response:
    if is_api_path(request):
        error: dict[str, object] = {
            "status": status_code,
            "message": HTTPStatus(status_code).phrase,
        }
        if fields:
            error["fields"] = fields
        return JSONResponse({"error": error}, status_code=status_code, headers=headers)
    copy = copy or error_copy(status_code)
    request_id = str(getattr(request.state, "request_id", ""))
    if is_htmx_request(request):
        # htmx swaps this into the target in place; the client script opts error statuses in.
        return no_store(
            render_partial(
                request,
                "error_state",
                title=copy.title,
                message=copy.message,
                request_id=request_id or None,
                status_code=status_code,
                headers=headers,
            )
        )
    return no_store(
        render_page(
            request,
            "error.html",
            {
                "status_code": status_code,
                "title": copy.title,
                "message": copy.message,
                "request_id": request_id,
                "back_href": str(request.app.url_path_for("index")),
                "back_label": "Return to the start page",
            },
            status_code=status_code,
            headers=headers,
        )
    )


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    return _error_response(request, exc.status_code, headers=exc.headers)


async def _validation_exception_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    # Only location and message are returned; the submitted input is never echoed.
    fields: list[dict[str, object]] = [
        {"loc": list(error["loc"]), "msg": error["msg"]} for error in exc.errors()
    ]
    return _error_response(request, 422, fields=fields)


async def _redirect_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RedirectRequired)
    return RedirectResponse(exc.location, status_code=303)


async def _permission_denied_handler(request: Request, exc: Exception) -> Response:
    return _error_response(request, 403)


async def _not_found_handler(request: Request, exc: Exception) -> Response:
    return _error_response(request, 404)


async def _self_protected_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, SelfProtected)
    if is_api_path(request):
        body = {"error": {"status": 409, "message": str(exc), "reason": exc.reason}}
        return JSONResponse(body, status_code=409)
    return _error_response(request, 409, copy=ErrorCopy("Protected", str(exc)))


async def _hashing_busy_handler(request: Request, exc: Exception) -> Response:
    return _error_response(request, 503, headers={"Retry-After": "1"})


async def _favicon(request: Request) -> Response:
    root = request.scope.get("root_path", "")
    return RedirectResponse(f"{root}/static/icons/favicon.svg", status_code=308)


async def _unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    request_id = str(getattr(request.state, "request_id", ""))
    with bound_request_id(request_id):
        logger.error("unhandled_exception", exc_info=exc)
    headers = {REQUEST_ID_HEADER: request_id} if request_id else None
    return _error_response(request, 500, headers=headers)


def _restore_runtime(app: FastAPI) -> RestoreRuntime:
    engine = app.state.engine
    return RestoreRuntime(
        database=app.state.database,
        vault=app.state.vault,
        scheduler=app.state.scheduler.scheduler,
        image_jobs=engine.image_jobs,
        stack_jobs=engine.stack_jobs,
        running_key=app.state.running_key,
    )


def create_app(
    settings: Settings | None = None, *, extra_subsystems: Sequence[SubsystemFactory] = ()
) -> FastAPI:
    """Construct the application, validating configuration before anything starts.

    extra_subsystems are registered with the scheduler after the built-in ones.
    """
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    app = build_app(AppOptions.from_settings(settings))
    attach_state(app, settings, extra_subsystems=extra_subsystems)
    return app


def attach_state(
    app: FastAPI,
    settings: Settings,
    *,
    extra_subsystems: Sequence[SubsystemFactory] = (),
    templates: Jinja2Templates | None = None,
) -> None:
    """Replace every per-instance piece of application state; call it only between lifespans."""
    app.state = State()
    app.state.settings = settings
    app.state.extra_subsystems = tuple(extra_subsystems)
    app.state.auth = AuthRuntime.from_settings(settings)
    app.state.boot = ProcessBoot.at(app.state.auth.clock())
    app.state.restart_dispatcher = RestartDispatcher()
    app.state.templates = (
        build_templates(development_checks=settings.development_checks)
        if templates is None
        else templates
    )
    app.state.flash_key = secrets.token_bytes(FLASH_KEY_BYTES)
    app.state.results = results.ResultStore()
    app.state.rate_limiter = AddressRateLimiter()
    app.state.webhook_limiter = WebhookCallLimiter()
    app.state.readiness_checks = {"data_dir": health.data_dir_check(settings)}
    app.state.backup_production = BackupProduction(settings.data_dir)
    app.state.restore_gate = RestoreGate()
    app.state.restore = RestoreCoordinator(
        gate=app.state.restore_gate,
        production=app.state.backup_production,
        data_dir=settings.data_dir,
        runtime=partial(_restore_runtime, app),
        stop_runtime=partial(stop_runtime, app),
        start_runtime=partial(start_runtime, app, during="restore"),
        clock=lambda: app.state.auth.clock(),
        busy=lambda: app.state.encryption.check_running(),
    )
    app.state.encryption = EncryptionService(
        gate=app.state.restore_gate,
        production=app.state.backup_production,
        data_dir=settings.data_dir,
        runtime=partial(_restore_runtime, app),
        settings=settings,
        clock=lambda: app.state.auth.clock(),
    )


@dataclass(frozen=True, slots=True)
class AppOptions:
    """The settings read while the application object is built; the rest are read from state."""

    max_concurrent_requests: int
    development_checks: bool
    access_log: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Take the construction-time values from the settings."""
        return cls(
            settings.max_concurrent_requests, settings.development_checks, settings.access_log
        )


def build_app(options: AppOptions) -> FastAPI:
    """Build the application object: middleware, handlers, mounts and routers, without state."""
    # Application-wide, so a router added later is covered without remembering to opt in.
    app = DockminApp(
        title="dockmin",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # limit_anonymous reads the principal, and runs before enforce_csrf so refused posts count.
        dependencies=[
            Depends(gate_setup),
            Depends(resolve_principal),
            Depends(limit_anonymous),
            Depends(enforce_csrf),
            Depends(resolve_navigation),
        ],
    )

    app.add_middleware(BodySizeLimitMiddleware, respond=_error_response)
    app.add_middleware(RestoreGateMiddleware)
    app.add_middleware(
        ConcurrencyLimitMiddleware, limit=options.max_concurrent_requests, respond=_error_response
    )
    app.add_middleware(SendIdleTimeoutMiddleware)
    if options.access_log:
        app.add_middleware(AccessLogMiddleware)
    app.add_middleware(QueuedCookiesMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(RedirectRequired, _redirect_handler)
    app.add_exception_handler(PermissionDenied, _permission_denied_handler)
    app.add_exception_handler(NotFound, _not_found_handler)
    app.add_exception_handler(SelfProtected, _self_protected_handler)
    app.add_exception_handler(HashingBusy, _hashing_busy_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)
    app.include_router(public_settings.router)
    app.include_router(auth_routes.router)
    app.include_router(user_routes.router)
    app.include_router(team_routes.router)
    app.include_router(token_routes.router)
    app.include_router(container_create_routes.router)
    app.include_router(container_export_routes.router)
    app.include_router(container_job_routes.router)
    app.include_router(container_stream_routes.router)
    app.include_router(container_stream_routes.socket_router)
    app.include_router(container_terminal_routes.router)
    app.include_router(container_routes.router)
    app.include_router(image_stream_routes.router)
    app.include_router(image_routes.router)
    app.include_router(stack_stream_routes.router)
    app.include_router(stack_log_routes.router)
    app.include_router(stack_read_routes.router)
    app.include_router(stack_read_routes.swarm)
    app.include_router(stack_variable_routes.router)
    app.include_router(stack_variable_routes.swarm)
    app.include_router(stack_dismissal_routes.router)
    app.include_router(stack_dismissal_routes.swarm)
    app.include_router(stack_export_routes.router)
    app.include_router(stack_export_routes.swarm)
    app.include_router(stack_routes.router)
    app.include_router(stack_file_routes.router)
    app.include_router(swarm_stream_routes.router)
    app.include_router(swarm_routes.router)
    app.include_router(swarm_service_routes.router)
    app.include_router(swarm_node_routes.router)
    app.include_router(swarm_object_routes.router)
    app.include_router(swarm_stack_manage_routes.router)
    app.include_router(swarm_stack_routes.router)
    app.include_router(network_routes.router)
    app.include_router(volume_export_routes.router)
    app.include_router(volume_import_routes.router)
    app.include_router(volume_routes.router)
    app.include_router(suggestion_routes.router)
    app.include_router(job_routes.router)
    app.include_router(activity_routes.router)
    app.include_router(automation_routes.router)
    app.include_router(automation_webhook_routes.router)
    app.include_router(environment_routes.router)
    app.include_router(onboarding_routes.router)
    app.include_router(group_routes.router)
    app.include_router(tag_routes.router)
    app.include_router(registry_routes.router)
    app.include_router(notification_routes.router)
    app.include_router(object_storage_routes.router)
    app.include_router(template_catalogue_routes.router)
    app.include_router(template_routes.router)
    app.include_router(backup_routes.router)
    app.include_router(restore_routes.router)
    app.include_router(server_routes.router)
    app.include_router(encryption_routes.router)
    app.include_router(containers_api.router)
    app.include_router(stacks_api.router)
    app.include_router(environments_api.router)
    app.include_router(jobs_api.router)
    app.include_router(rules_api.router)
    app.include_router(hooks_api.router)
    app.include_router(activity_api.router)
    app.include_router(results.router)
    app.add_api_route(
        FAVICON_PATH, _favicon, methods=["GET"], name="favicon", include_in_schema=False
    )
    app.include_router(pages.router)
    if options.development_checks:
        app.include_router(pages.kit_router)
    return app
