"""Tests for the error handling and the startup and shutdown wired up in app/main.py."""

import asyncio
import io
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, routing
from fastapi.dependencies.utils import get_dependant
from httpx import AsyncClient

from app import main as application_module
from app import version as version_module
from app.config import (
    MANAGED_KEY_FILENAME,
    SECRET_KEY_DIR_VAR,
    SECRET_KEY_VAR,
    Settings,
    load_settings,
)
from app.crypto import managed_key
from app.logging import DEFAULT_SCRUBBER, REQUEST_ID_HEADER, configure_logging
from app.main import (
    AppOptions,
    attach_state,
    build_app,
    create_app,
    on_shutdown,
    start_runtime,
    stop_runtime,
)
from app.selfcheck import StartupCheckFailed
from app.services.activity import SUBSYSTEMS
from app.version import product_version
from app.web.security_headers import STARTUP_FAILURE_ATTRIBUTE
from tests.conftest import serving_app
from tests.job_support import build_job_app

INTERNAL_DETAIL = "db-connection-refused-at-10.0.0.7"
REQUEST_ID = "req-12345"
NOT_A_NUMBER = "not-a-number"

Logged = Callable[[str], list[dict[str, Any]]]


async def _explode() -> None:
    raise RuntimeError(INTERNAL_DETAIL)


async def _count(count: int) -> dict[str, int]:
    return {"count": count}


@pytest.fixture
def app(private_app: FastAPI) -> FastAPI:
    app = private_app
    # These routes exist only to drive the error handlers.
    app.add_api_route("/api/boom", _explode)
    app.add_api_route("/boom", _explode)
    app.add_api_route("/api/count", _count)
    return app


@pytest.mark.parametrize("path", ["/api", "/api/missing"])
async def test_unknown_api_path_returns_a_json_error(client: AsyncClient, path: str) -> None:
    response = await client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"error": {"status": 404, "message": "Not Found"}}


@pytest.mark.parametrize("path", ["/missing", "/apiary"])
async def test_unknown_page_path_returns_an_html_error(client: AsyncClient, path: str) -> None:
    response = await client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "404" in response.text


async def test_wrong_method_returns_405_and_keeps_the_allow_header(client: AsyncClient) -> None:
    response = await client.post("/api/health")

    assert response.status_code == 405
    assert "GET" in response.headers["allow"]
    assert response.json()["error"]["status"] == 405


async def test_validation_error_lists_fields_without_echoing_the_input(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.get("/api/count", params={"count": NOT_A_NUMBER})

    assert response.status_code == 422
    assert response.json()["error"]["fields"][0]["loc"] == ["query", "count"]
    assert NOT_A_NUMBER not in response.text


@pytest.mark.parametrize("path", ["/api/boom", "/boom"])
async def test_unhandled_exception_response_carries_no_internal_detail(
    admin_client: AsyncClient, path: str
) -> None:
    response = await admin_client.get(path)

    assert response.status_code == 500
    assert INTERNAL_DETAIL not in response.text
    assert "Traceback" not in response.text
    assert ".py" not in response.text


async def test_unhandled_exception_on_api_path_returns_the_generic_json_body(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.get("/api/boom")

    assert response.json() == {"error": {"status": 500, "message": "Internal Server Error"}}


async def test_unhandled_exception_echoes_the_supplied_request_id_on_header_and_log(
    admin_client: AsyncClient, logged: Logged
) -> None:
    response = await admin_client.get("/api/boom", headers={REQUEST_ID_HEADER: REQUEST_ID})

    [record] = logged("unhandled_exception")
    assert response.headers[REQUEST_ID_HEADER] == REQUEST_ID
    assert record["request_id"] == REQUEST_ID


async def test_unhandled_exception_ties_a_generated_request_id_to_its_log_record(
    admin_client: AsyncClient, logged: Logged
) -> None:
    response = await admin_client.get("/api/boom")

    [record] = logged("unhandled_exception")
    assert record["request_id"]
    assert response.headers[REQUEST_ID_HEADER] == record["request_id"]


async def test_unhandled_exception_logs_the_traceback_server_side(
    admin_client: AsyncClient, logged: Logged
) -> None:
    await admin_client.get("/api/boom")

    [record] = logged("unhandled_exception")
    assert INTERNAL_DETAIL in record["exception"]


class Closer:
    def __init__(self, name: str, order: list[str], **parts: "Closer | None") -> None:
        self._name = name
        self._order = order
        self.__dict__.update(parts)

    async def aclose(self) -> None:
        self._order.append(self._name)


async def test_shutdown_closes_jobs_compose_registries_engine_connections_then_the_database() -> (
    None
):
    order: list[str] = []
    state = SimpleNamespace(
        engine=Closer(
            "engine",
            order,
            image_jobs=Closer("jobs", order),
            stack_jobs=Closer("stack jobs", order),
            compose=Closer("compose", order),
        ),
        registries=Closer("registries", order),
        database=Closer("database", order),
    )

    await on_shutdown(SimpleNamespace(state=state))  # type: ignore[arg-type]

    assert order == ["jobs", "stack jobs", "compose", "registries", "engine", "database"]


async def test_shutdown_closes_the_scheduler_first_and_releases_the_lock_last() -> None:
    order: list[str] = []
    state = SimpleNamespace(
        scheduler=SimpleNamespace(scheduler=Closer("scheduler", order)),
        engine=Closer(
            "engine",
            order,
            image_jobs=Closer("jobs", order),
            stack_jobs=Closer("stack jobs", order),
            compose=None,
        ),
        registries=Closer("registries", order),
        database=Closer("database", order),
        instance_lock=SimpleNamespace(release=lambda: order.append("lock")),
    )

    await on_shutdown(SimpleNamespace(state=state))  # type: ignore[arg-type]

    assert order == ["scheduler", "jobs", "stack jobs", "registries", "engine", "database", "lock"]


async def test_a_shutdown_step_that_fails_does_not_stop_the_ones_after_it_nor_the_lock_release(
    logged: Logged,
) -> None:
    class Failing(Closer):
        async def aclose(self) -> None:
            await super().aclose()
            raise RuntimeError("scheduler would not stop")

    order: list[str] = []
    state = SimpleNamespace(
        scheduler=SimpleNamespace(scheduler=Failing("scheduler", order)),
        database=Closer("database", order),
        instance_lock=SimpleNamespace(release=lambda: order.append("lock")),
    )

    await on_shutdown(SimpleNamespace(state=state))  # type: ignore[arg-type]

    assert order == ["scheduler", "database", "lock"]
    assert [r["step"] for r in logged("shutdown_step_failed")] == ["scheduler"]


async def test_a_shutdown_that_is_cancelled_still_releases_the_lock() -> None:
    class Cancelling(Closer):
        async def aclose(self) -> None:
            raise asyncio.CancelledError

    order: list[str] = []
    state = SimpleNamespace(
        scheduler=SimpleNamespace(scheduler=Cancelling("scheduler", order)),
        instance_lock=SimpleNamespace(release=lambda: order.append("lock")),
    )

    with pytest.raises(asyncio.CancelledError):
        await on_shutdown(SimpleNamespace(state=state))  # type: ignore[arg-type]

    assert order == ["lock"]


async def test_the_scheduler_runs_while_the_application_serves_and_stops_with_it(
    app: FastAPI, settings: Settings, provisioned_template: Path
) -> None:
    async with serving_app(app, settings, provisioned_template):
        loops_while_serving = [t for t in asyncio.all_tasks() if t.get_name() == "scheduler-loop"]
        lock_held = app.state.instance_lock.held
        registered = app.state.scheduler.handlers.names()

    loops_after = [
        t for t in asyncio.all_tasks() if t.get_name() == "scheduler-loop" and not t.done()
    ]
    assert len(loops_while_serving) == 1 and lock_held
    assert "environment-snapshot-refresher" in registered
    assert loops_after == []
    assert not app.state.instance_lock.held


async def test_a_second_application_on_one_data_directory_is_refused_and_opens_nothing(
    app: FastAPI, settings: Settings, provisioned_template: Path, log_stream: io.StringIO
) -> None:
    second = create_app(settings)
    configure_logging(settings.log_level, stream=log_stream)

    async with serving_app(app, settings, provisioned_template):
        with pytest.raises(StartupCheckFailed) as refused:
            async with second.router.lifespan_context(second):
                pass
        refused_without_resources = not hasattr(second.state, "database")
        first_still_serving = app.state.instance_lock.held

    assert refused_without_resources and first_still_serving
    assert refused.value.check == "data_directory"
    assert "startup_check_failed" in log_stream.getvalue()


async def test_the_data_directory_can_be_used_again_once_the_first_application_has_stopped(
    app: FastAPI, settings: Settings, provisioned_template: Path
) -> None:
    async with serving_app(app, settings, provisioned_template):
        pass
    third = create_app(settings)

    async with third.router.lifespan_context(third):
        assert third.state.instance_lock.held


@pytest.mark.parametrize(("enabled", "warned"), [(True, True), (False, False)])
async def test_a_start_with_development_checks_on_warns_that_they_are_on(
    settings: Settings,
    provisioned_template: Path,
    log_stream: io.StringIO,
    enabled: bool,
    warned: bool,
) -> None:
    configured = settings.model_copy(update={"development_checks": enabled})
    application = create_app(configured)
    configure_logging(configured.log_level, stream=log_stream)

    async with serving_app(application, configured, provisioned_template):
        pass

    assert ("development_checks_enabled" in log_stream.getvalue()) is warned


STARTED_FIELDS = ("version", "instance_name", "http_mode", "https_port", "http_port", "access_log")
INSTANCE = "edge-01"
HTTP_PORT = 8000
HTTPS_PORT = 8443


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            {"tls_self_signed": False},
            {"http_mode": "serve", "https_port": None, "http_port": HTTP_PORT},
        ),
        (
            {"tls_self_signed": True, "access_log": True},
            {"http_mode": "redirect", "https_port": HTTPS_PORT, "http_port": HTTP_PORT},
        ),
        (
            {"tls_self_signed": True, "http_mode": "off"},
            {"http_mode": "off", "https_port": HTTPS_PORT, "http_port": None},
        ),
    ],
)
async def test_application_started_carries_the_effective_listener_and_instance_values(
    settings: Settings,
    provisioned_template: Path,
    log_stream: io.StringIO,
    logged: Logged,
    changes: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    configured = settings.model_copy(update={"instance_name": INSTANCE} | changes)
    application = create_app(configured)
    configure_logging(configured.log_level, stream=log_stream)

    async with serving_app(application, configured, provisioned_template):
        pass

    [record] = logged("application_started")
    assert {name: record[name] for name in STARTED_FIELDS} == {
        "version": product_version(),
        "instance_name": INSTANCE,
        "access_log": configured.access_log,
    } | expected


REVISION = "0123456789abcdef0123456789abcdef01234567"


def recorded_revision(directory: Path, text: str | None) -> Path:
    """Return the path of a revision file holding the text, or of one that does not exist."""
    path = directory / "revision"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("written", "expected"), [(f"{REVISION}\n", REVISION), (None, None)], ids=["present", "absent"]
)
async def test_application_started_carries_the_revision_the_build_recorded_or_null(
    settings: Settings,
    provisioned_template: Path,
    log_stream: io.StringIO,
    logged: Logged,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    written: str | None,
    expected: str | None,
) -> None:
    monkeypatch.setattr(version_module, "REVISION_FILE", recorded_revision(tmp_path, written))
    application = create_app(settings)
    configure_logging(settings.log_level, stream=log_stream)

    async with serving_app(application, settings, provisioned_template):
        pass

    [record] = logged("application_started")
    assert record["revision"] == expected


@pytest.mark.parametrize(("secure", "warned"), [(False, 1), (True, 0)])
async def test_a_start_with_secure_cookies_off_warns_once_and_a_start_with_them_on_does_not(
    settings: Settings,
    provisioned_template: Path,
    log_stream: io.StringIO,
    logged: Logged,
    secure: bool,
    warned: int,
) -> None:
    configured = settings.model_copy(update={"cookie_secure": secure})
    application = create_app(configured)
    configure_logging(configured.log_level, stream=log_stream)

    async with serving_app(application, configured, provisioned_template):
        pass

    assert len(logged("cookie_secure_disabled")) == warned


async def test_shutdown_closes_the_health_watchers_before_the_scheduler() -> None:
    order: list[str] = []
    state = SimpleNamespace(
        health_watchers=Closer("watchers", order),
        scheduler=SimpleNamespace(scheduler=Closer("scheduler", order)),
        database=Closer("database", order),
    )

    await on_shutdown(SimpleNamespace(state=state))  # type: ignore[arg-type]

    assert order == ["watchers", "scheduler", "database"]


async def test_the_built_in_subsystems_are_registered_and_the_watchers_run_only_while_serving(
    app: FastAPI, settings: Settings, provisioned_template: Path
) -> None:
    async with serving_app(app, settings, provisioned_template):
        handlers = app.state.scheduler.handlers
        registered = {name: type(handlers.get(name)).__name__ for name in sorted(handlers.names())}

    assert registered == {
        "activity-log-exporter": "ActivityLogExporter",
        "backup-syncer": "BackupSyncer",
        "container-health-checker": "ContainerHealthChecker",
        "container-log-exporter": "ContainerLogExporter",
        "container-updater": "ContainerUpdater",
        "environment-snapshot-refresher": "EnvironmentSnapshotRefresher",
        "notification-sender": "NotificationSender",
        "stack-restarter": "StackRestarter",
        "webhook-receiver": "WebhookReceiver",
    }
    assert set(registered) <= set(SUBSYSTEMS)
    assert [t for t in asyncio.all_tasks() if (t.get_name() or "").startswith("health-watch")] == []


async def test_an_extra_subsystem_replaces_the_built_in_of_the_same_name_and_no_other(
    settings: Settings, provisioned_template: Path, log_stream: io.StringIO
) -> None:
    application = build_job_app(settings, log_stream)

    async with serving_app(application, settings, provisioned_template):
        handlers = application.state.scheduler.handlers
        replaced = (
            type(handlers.get("stack-restarter")).__name__,
            type(handlers.get("container-updater")).__name__,
        )
        kept = type(handlers.get("container-health-checker")).__name__

    assert (replaced, kept) == (("ScriptedRestarter", "ScriptedUpdater"), "ContainerHealthChecker")


RUNTIME_PARTS = ("database", "vault", "engine", "registries", "scheduler", "health_watchers")
STARTUP_ORDER = (
    "encryption_key_loaded",
    "compose_detected",
    "scheduler_recovered",
    "application_started",
)


async def test_the_runtime_starts_in_the_order_the_lifespan_always_used(
    app: FastAPI, settings: Settings, provisioned_template: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")

    async with serving_app(app, settings, provisioned_template):
        pass

    messages = [record.getMessage() for record in caplog.records]
    positions = [messages.index(name) for name in STARTUP_ORDER]
    assert positions == sorted(positions)


async def test_stopping_the_runtime_closes_and_drops_the_database_and_keeps_the_instance_lock(
    app: FastAPI, settings: Settings, provisioned_template: Path
) -> None:
    async with serving_app(app, settings, provisioned_template):
        database = app.state.database
        await stop_runtime(app)
        dropped = not hasattr(app.state, "database")
        lock_held = app.state.instance_lock.held
        await start_runtime(app)

    assert dropped and database.closed
    assert lock_held


async def test_starting_the_runtime_again_builds_new_parts_without_taking_the_lock_a_second_time(
    app: FastAPI, settings: Settings, provisioned_template: Path
) -> None:
    async with serving_app(app, settings, provisioned_template):
        first = {name: getattr(app.state, name) for name in RUNTIME_PARTS}
        lock = app.state.instance_lock
        await stop_runtime(app)
        await start_runtime(app)
        again = {name: getattr(app.state, name) for name in RUNTIME_PARTS}
        loops = [
            t for t in asyncio.all_tasks() if t.get_name() == "scheduler-loop" and not t.done()
        ]

        assert app.state.instance_lock is lock and lock.held
        assert all(again[name] is not first[name] for name in RUNTIME_PARTS)
        assert first["database"].closed
        assert len(loops) == 1
        assert app.state.scheduler.handlers.names() == first["scheduler"].handlers.names()


async def test_shutdown_stops_a_restore_before_the_runtime_and_ends_the_automation_hold_last() -> (
    None
):
    order: list[str] = []
    state = SimpleNamespace(
        restore=SimpleNamespace(
            aclose=_recording(order, "restore"), release_hold=_recording(order, "hold")
        ),
        scheduler=SimpleNamespace(scheduler=Closer("scheduler", order)),
        database=Closer("database", order),
        instance_lock=SimpleNamespace(release=lambda: order.append("lock")),
    )

    await on_shutdown(SimpleNamespace(state=state))  # type: ignore[arg-type]

    assert order == ["restore", "scheduler", "database", "hold", "lock"]


def _recording(order: list[str], name: str) -> Callable[[], Any]:
    async def record() -> None:
        order.append(name)

    return record


async def test_a_process_start_clears_stale_work_but_a_runtime_restart_leaves_a_download_alone(
    app: FastAPI, settings: Settings, provisioned_template: Path
) -> None:
    work = settings.data_dir / "backup-work"
    (work / "stale").mkdir(parents=True)
    (settings.data_dir / "restore" / ("a" * 32) / "staging").mkdir(parents=True)
    (settings.data_dir / "restore" / ("b" * 32) / "previous").mkdir(parents=True)

    async with serving_app(app, settings, provisioned_template):
        cleared = not (work / "stale").exists()
        (work / "in-flight").mkdir(parents=True)
        await stop_runtime(app)
        await start_runtime(app)
        kept = (work / "in-flight").exists()
        empty_restore_removed = not (settings.data_dir / "restore" / ("a" * 32)).exists()
        moved_data_kept = (settings.data_dir / "restore" / ("b" * 32) / "previous").exists()

    assert cleared and kept and empty_restore_removed and moved_data_kept


def test_the_middleware_stack_is_ordered_outermost_first(app: FastAPI) -> None:
    from app.logging import RequestIdMiddleware
    from app.services.restore_gate import RestoreGateMiddleware
    from app.web.limits import (
        BodySizeLimitMiddleware,
        ConcurrencyLimitMiddleware,
        SendIdleTimeoutMiddleware,
    )
    from app.web.queued_cookies import QueuedCookiesMiddleware

    assert [layer.cls for layer in app.user_middleware] == [  # type: ignore[comparison-overlap]
        RequestIdMiddleware,
        QueuedCookiesMiddleware,
        SendIdleTimeoutMiddleware,
        ConcurrencyLimitMiddleware,
        RestoreGateMiddleware,
        BodySizeLimitMiddleware,
    ]


def test_the_application_wide_dependencies_run_in_the_order_that_lets_refusals_be_counted(
    app: FastAPI,
) -> None:
    from app.auth.dependencies import gate_setup, resolve_principal
    from app.web.csrf import enforce_csrf
    from app.web.rate_limit import limit_anonymous
    from app.web.sidebar import resolve_navigation

    assert [dependency.dependency for dependency in app.router.dependencies] == [
        gate_setup,
        resolve_principal,
        limit_anonymous,
        enforce_csrf,
        resolve_navigation,
    ]


def test_the_application_serves_no_interactive_documentation_or_schema_endpoint(
    app: FastAPI,
) -> None:
    assert (app.docs_url, app.redoc_url, app.openapi_url) == (None, None, None)


def test_the_security_headers_wrap_the_whole_stack(app: FastAPI) -> None:
    from app.web.security_headers import DockminApp

    assert isinstance(app, DockminApp)


async def test_the_address_policy_is_set_from_the_settings_before_anything_connects(
    env: dict[str, str], provisioned_template: Path, log_stream: io.StringIO
) -> None:
    from app import netpolicy
    from app.config import load_settings

    settings = load_settings(
        env | {"DOCKMIN_PERMITTED_INTERNAL_NETWORKS": "169.254.169.254, fe80::/10"}
    )
    application = create_app(settings)
    configure_logging(settings.log_level, stream=log_stream)
    seen: list[str] = []

    async with serving_app(application, settings, provisioned_template):
        seen = [str(network) for network in netpolicy.current().permitted]

    assert seen == ["169.254.169.254/32", "fe80::/10"]


@pytest.mark.parametrize(
    ("extra", "listener"),
    [
        pytest.param({}, "http", id="plain-listener-without-proxies"),
        pytest.param({"DOCKMIN_TRUSTED_PROXIES": "10.0.0.0/8"}, None, id="behind-proxies"),
    ],
)
async def test_a_start_that_cannot_tell_clients_apart_logs_which_listener_it_is(
    env: dict[str, str],
    provisioned_template: Path,
    log_stream: io.StringIO,
    logged: Logged,
    extra: dict[str, str],
    listener: str | None,
) -> None:
    from app.config import load_settings

    settings = load_settings(env | extra)
    application = create_app(settings)
    configure_logging(settings.log_level, stream=log_stream)

    async with serving_app(application, settings, provisioned_template):
        pass

    lines = logged("rate_limit_not_enforced")
    assert [line["listener"] for line in lines] == ([] if listener is None else [listener])


async def test_an_unusable_work_directory_stops_the_start_as_a_data_directory_failure(
    app: FastAPI,
    settings: Settings,
    provisioned_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    logged: Logged,
) -> None:
    import shutil

    from app import main
    from app.selfcheck import StartupCheckFailed

    def refuse(_: Path) -> None:
        raise OSError("not a directory")

    monkeypatch.setattr(main, "prepare_engine_workdir", refuse)
    shutil.copyfile(provisioned_template, settings.db_path)

    with pytest.raises(StartupCheckFailed) as raised:
        async with app.router.lifespan_context(app):
            pass

    [line] = logged("startup_check_failed")
    assert raised.value.check == "data_directory" and line["check"] == "data_directory"
    assert not hasattr(app.state, "scheduler")


async def test_an_unusable_sqlite_temporary_directory_stops_the_start(
    env: dict[str, str],
    tmp_path: Path,
    log_stream: io.StringIO,
    logged: Logged,
    monkeypatch: pytest.MonkeyPatch,
    provisioned_template: Path,
) -> None:
    import shutil

    from app.config import load_settings

    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("SQLITE_TMPDIR", str(blocker / "sqlite"))
    settings = load_settings(env)
    application = create_app(settings)
    configure_logging(settings.log_level, stream=log_stream)
    shutil.copyfile(provisioned_template, settings.db_path)

    with pytest.raises(StartupCheckFailed):
        async with application.router.lifespan_context(application):
            pass

    [line] = logged("startup_check_failed")
    assert "SQLITE_TMPDIR" in line["remedy"]


RUNTIME_ATTRIBUTES = (
    *RUNTIME_PARTS,
    "instance_lock",
    "compose_version",
    "served_certificate",
    STARTUP_FAILURE_ATTRIBUTE,
)
REPLACED_OBJECTS = ("auth", "restore_gate", "restore")
CHANGED_INTERVAL_SECONDS = 120


def test_attaching_state_again_leaves_no_runtime_attribute_and_replaces_the_per_instance_objects(
    settings: Settings,
) -> None:
    application = build_app(AppOptions.from_settings(settings))
    attach_state(application, settings)
    for name in RUNTIME_ATTRIBUTES:
        setattr(application.state, name, object())
    earlier = {name: getattr(application.state, name) for name in REPLACED_OBJECTS}
    later = settings.model_copy(update={"snapshot_interval_seconds": CHANGED_INTERVAL_SECONDS})

    attach_state(application, later)

    assert [name for name in RUNTIME_ATTRIBUTES if hasattr(application.state, name)] == []
    assert application.state.settings is later
    assert all(getattr(application.state, name) is not earlier[name] for name in REPLACED_OBJECTS)


def test_create_app_builds_the_application_and_attaches_the_given_state(
    settings: Settings,
) -> None:
    def factory(deps: Any) -> Any:
        raise AssertionError("a subsystem factory is not called before the application starts")

    application = create_app(settings, extra_subsystems=[factory])
    built = build_app(AppOptions.from_settings(settings))

    assert type(application) is type(built)
    assert [layer.cls for layer in application.user_middleware] == [
        layer.cls for layer in built.user_middleware
    ]
    assert len(application.routes) == len(built.routes)
    assert application.state.settings is settings
    assert application.state.extra_subsystems == (factory,)
    assert not hasattr(built.state, "settings")


class DependantBuilds:
    """Counts how many times FastAPI builds a route's dependency tree."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        real = get_dependant

        def counting(*args: Any, **kwargs: Any) -> Any:
            self.count += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(routing, "get_dependant", counting)


def test_the_first_url_lookup_on_a_fresh_application_builds_the_route_tree(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = build_app(AppOptions.from_settings(settings))
    builds = DependantBuilds(monkeypatch)

    application.url_path_for("index")

    assert builds.count > 0


async def test_the_route_tree_is_already_built_once_the_application_has_started(
    settings: Settings,
    provisioned_template: Path,
    log_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_app(settings)
    configure_logging(settings.log_level, stream=log_stream)

    async with serving_app(application, settings, provisioned_template):
        builds = DependantBuilds(monkeypatch)
        application.url_path_for("index")

    assert builds.count == 0


OWNER_ONLY_DIRECTORY = 0o700
OWNER_READ_ONLY = 0o400


def mounted_only(monkeypatch: pytest.MonkeyPatch, *volumes: Path) -> None:
    def is_mounted(path: Path) -> bool:
        return path in volumes

    monkeypatch.setattr(managed_key, "mount_point", is_mounted)
    monkeypatch.setattr(application_module, "mount_point", is_mounted)


async def key_fingerprint_after_one_start(settings: Settings, log_stream: io.StringIO) -> str:
    application = create_app(settings)
    configure_logging(settings.log_level, stream=log_stream)
    held = DEFAULT_SCRUBBER._secret_values
    try:
        async with application.router.lifespan_context(application):
            fingerprint: str = application.state.vault.key_fingerprint()
    finally:
        DEFAULT_SCRUBBER._secret_values = held
    return fingerprint


async def test_a_generated_key_is_made_once_read_on_the_next_start_and_never_made_again(
    env: dict[str, str],
    tmp_path: Path,
    migrated_template: Path,
    log_stream: io.StringIO,
    logged: Logged,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_volume = tmp_path / "key-volume"
    key_volume.mkdir(mode=OWNER_ONLY_DIRECTORY)
    key_file = key_volume / MANAGED_KEY_FILENAME
    settings = load_settings(
        {name: value for name, value in env.items() if name != SECRET_KEY_VAR}
        | {SECRET_KEY_DIR_VAR: str(key_volume)}
    )
    shutil.copyfile(migrated_template, settings.db_path)

    mounted_only(monkeypatch, key_volume)
    first = await key_fingerprint_after_one_start(settings, log_stream)
    created = key_file.stat()
    warned_unmounted = len(logged("data_directory_not_mounted"))
    mounted_only(monkeypatch, key_volume, settings.data_dir)
    second = await key_fingerprint_after_one_start(settings, log_stream)
    key_file.unlink()
    with pytest.raises(StartupCheckFailed) as refused:
        await key_fingerprint_after_one_start(settings, log_stream)

    assert stat.S_IMODE(created.st_mode) == OWNER_READ_ONLY
    assert second == first
    assert len(logged("encryption_key_generated")) == 1
    assert (warned_unmounted, len(logged("data_directory_not_mounted"))) == (1, 1)
    assert refused.value.check == "encryption_key"
    assert refused.value.problem == (
        f"No encryption key is configured and {key_file} does not exist, but the database "
        "already holds encrypted data"
    )
    assert not key_file.exists()


async def test_a_start_with_a_supplied_key_does_not_warn_about_the_data_directory_mount(
    app: FastAPI, settings: Settings, provisioned_template: Path, logged: Logged
) -> None:
    async with serving_app(app, settings, provisioned_template):
        pass

    assert logged("data_directory_not_mounted") == []
