"""Shared fixtures: settings, an ASGI client, captured JSON logs and a migrated database."""

import io
import ipaddress
import json
import logging
import shutil
import socket
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from httpx import ASGITransport, AsyncClient
from jinja2 import pass_context
from jinja2.runtime import Context

from app import netpolicy
from app.config import ENV_PREFIX, SECRET_KEY_VAR, TLS_SELF_SIGNED_VAR, Settings, load_settings
from app.db import engine as db_engine
from app.db.engine import Database, connect
from app.db.migrations import migrate
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.docker_engine.registry import EngineClientRegistry
from app.logging import DEFAULT_SCRUBBER, configure_logging
from app.main import AppOptions, attach_state, build_app, create_app
from app.services import assembly
from app.services.terminals import TerminalLimits, TerminalService
from app.subsystems.base import SubsystemFactory
from app.web.limits import ConcurrencyLimitMiddleware
from app.web.templating import build_templates
from tests.auth_support import (
    ADMIN_SESSION_TOKEN,
    STANDARD_ID,
    STANDARD_SESSION_TOKEN,
    Browsers,
    provision,
)
from tests.container_support import STACK, STACK_LABEL, share_with_standard_user, stock
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.terminal_engine import TerminalEngine, serve_terminal_engine
from tests.support import CHEAP_ARGON2_ENV, VALID_KEY
from tests.terminal_support import (
    CONTAINER,
    HOST_CONFIGS,
    INTERACTIVE_CONFIG,
    PLAIN_CONFIG,
    PLAIN_CONTAINER,
    Live,
    serving,
)
from tests.web.environment_support import EngineTripwire, World, seed_world

REVERSE_ORDER_OPTION = "--reverse-order"

DATA_DIR_VAR = f"{ENV_PREFIX}DATA_DIR"
COMPOSE_BINARY_VAR = f"{ENV_PREFIX}COMPOSE_BINARY"
DEVELOPMENT_CHECKS_VAR = f"{ENV_PREFIX}DEVELOPMENT_CHECKS"
COMPOSE_STUB = Path(__file__).with_name("compose_stub.sh")
LOOPBACK_NETWORKS = ("127.0.0.0/8", "::1/128")
TEST_DOMAIN = ".example.test"
TEST_DOMAIN_ADDRESS = "203.0.113.7"
OWNER_ONLY_DIRECTORY = 0o700
EXECUTABLE_FILE = 0o755


@dataclass
class AppClaim:
    module: bool = False


@dataclass(frozen=True)
class SharedApp:
    """The application object and template environment built once per process, and the options."""

    app: FastAPI
    options: AppOptions
    templates: Jinja2Templates
    claim: AppClaim = field(default_factory=AppClaim)


type AttachApp = Callable[..., FastAPI]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        REVERSE_ORDER_OPTION,
        action="store_true",
        default=False,
        help="run the collected tests in reverse order, to expose order dependence",
    )


def pytest_configure(config: pytest.Config) -> None:
    # Durability across power loss is not under test.
    opened = db_engine.connect

    def connect_without_fsync(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = opened(*args, **kwargs)
        connection.execute("PRAGMA synchronous = OFF")
        return connection

    db_engine.connect = connect_without_fsync


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption(REVERSE_ORDER_OPTION):
        items.reverse()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Return an existing empty directory standing in for /data."""
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def _address_policy_restored() -> Iterator[None]:
    """Put the default policy back after any test, including one whose application set its own."""
    previous = netpolicy.current()
    yield
    netpolicy.configure(previous)


@pytest.fixture
def address_policy() -> Iterator[Callable[..., None]]:
    """Return a setter for the process-wide address policy; the default is put back afterwards."""
    previous = netpolicy.current()

    def permit(*networks: str) -> None:
        netpolicy.configure(
            netpolicy.AddressPolicy(tuple(ipaddress.ip_network(text) for text in networks))
        )

    yield permit
    netpolicy.configure(previous)


@pytest.fixture
def permit_loopback(address_policy: Callable[..., None]) -> None:
    """Allow connections to this machine's loopback addresses for the test."""
    address_policy(*LOOPBACK_NETWORKS)


@pytest.fixture
def fake_hosts(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Return a table of host names that resolve to the address given.

    Names under the reserved test domain resolve to a documentation address unless the table
    says otherwise; every other name resolves normally.
    """
    table: dict[str, str] = {}
    real = socket.getaddrinfo

    def resolve(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        address = table.get(str(host))
        if address is None and str(host).endswith(TEST_DOMAIN):
            address = TEST_DOMAIN_ADDRESS
        if address is None:
            return real(host, port, *args, **kwargs)
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, port or 0, 0, 0) if family == socket.AF_INET6 else (address, port or 0)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return table


def install_compose_stub(parent: Path) -> Path:
    directory = parent / "compose-bin"
    directory.mkdir(mode=OWNER_ONLY_DIRECTORY)
    directory.chmod(OWNER_ONLY_DIRECTORY)
    binary = directory / "docker-compose"
    shutil.copyfile(COMPOSE_STUB, binary)
    binary.chmod(EXECUTABLE_FILE)
    return binary


def minimal_env(data_dir: Path, compose_binary: Path) -> dict[str, str]:
    return {
        SECRET_KEY_VAR: VALID_KEY,
        DATA_DIR_VAR: str(data_dir),
        COMPOSE_BINARY_VAR: str(compose_binary),
        DEVELOPMENT_CHECKS_VAR: "true",
        TLS_SELF_SIGNED_VAR: "false",
    } | CHEAP_ARGON2_ENV


@pytest.fixture
def compose_stub(tmp_path: Path) -> Path:
    """Return an executable that only reports a Compose version, so no test runs the host's."""
    return install_compose_stub(tmp_path)


@pytest.fixture
def env(data_dir: Path, compose_stub: Path) -> dict[str, str]:
    """Return the smallest valid environment, independent of the process environment."""
    return minimal_env(data_dir, compose_stub)


@pytest.fixture
def settings(env: dict[str, str]) -> Settings:
    """Build settings from the env mapping."""
    return load_settings(env)


@pytest.fixture
def root_logger_restored() -> Iterator[None]:
    """Restore the root logger's handlers and level after a test reconfigures logging."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def log_stream(root_logger_restored: None) -> io.StringIO:
    """Route JSON log output into a buffer."""
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    return stream


@pytest.fixture
def logged(log_stream: io.StringIO) -> Callable[[str], list[dict[str, Any]]]:
    """Return a reader for emitted JSON records matching a message."""

    def read(message: str) -> list[dict[str, Any]]:
        records = (json.loads(line) for line in log_stream.getvalue().splitlines())
        return [record for record in records if record["message"] == message]

    return read


@pytest.fixture(scope="session")
def shared_app(tmp_path_factory: pytest.TempPathFactory) -> SharedApp:
    """Build the application object once per process; each test attaches its own state to it."""
    data_dir = tmp_path_factory.mktemp("shared-app")
    settings = load_settings(
        {
            SECRET_KEY_VAR: VALID_KEY,
            DATA_DIR_VAR: str(data_dir),
            DEVELOPMENT_CHECKS_VAR: "true",
        }
        | CHEAP_ARGON2_ENV
    )
    options = AppOptions.from_settings(settings)
    application = build_app(options)
    templates = build_templates(development_checks=options.development_checks)
    templates.env.globals["route_path"] = memoised_route_path(
        application, cast(Callable[..., str], templates.env.globals["route_path"])
    )
    return SharedApp(application, options, templates)


def memoised_route_path(application: FastAPI, build: Callable[..., str]) -> Callable[..., str]:
    """Wrap the template link builder so each route and parameter set is resolved once."""
    resolved: dict[tuple[str, tuple[tuple[str, Any], ...]], str] = {}

    @pass_context
    def route_path(context: Context, name: str, **params: Any) -> str:
        if context["request"].app is not application:
            return build(context, name, **params)
        key = (name, tuple(sorted(params.items())))
        try:
            found = resolved.get(key)
        except TypeError:
            return build(context, name, **params)
        if found is None:
            found = resolved[key] = build(context, name, **params)
        return found

    return route_path


def concurrency_limiter(application: FastAPI) -> ConcurrencyLimitMiddleware | None:
    layer: Any = application.middleware_stack
    while layer is not None:
        if isinstance(layer, ConcurrencyLimitMiddleware):
            return layer
        layer = getattr(layer, "app", None)
    return None


@pytest.fixture
def attach_app(shared_app: SharedApp, log_stream: io.StringIO) -> Iterator[AttachApp]:
    """Return a function that gives the shared application this test's settings and fresh state.

    Everything on the application's state and every dependency override is dropped first. A
    test whose settings differ from the shared application's in a setting read when the
    application is built, or that changes the application object itself, uses private_app.
    """

    def attach(settings: Settings, extra_subsystems: Sequence[SubsystemFactory] = ()) -> FastAPI:
        assert not shared_app.claim.module, (
            "the shared application is held by this module's module-scoped fixtures; "
            "a test in it cannot also request a function-scoped application fixture"
        )
        options = AppOptions.from_settings(settings)
        assert options == shared_app.options, (
            "these settings are read when the application is built; use private_app for a test "
            f"that changes them. this test: {options}; shared application: {shared_app.options}"
        )
        application = shared_app.app
        application.dependency_overrides.clear()
        attach_state(
            application,
            settings,
            extra_subsystems=extra_subsystems,
            templates=shared_app.templates,
        )
        configure_logging(settings.log_level, stream=log_stream)
        return application

    yield attach
    limiter = concurrency_limiter(shared_app.app)
    in_flight = 0 if limiter is None else limiter.active
    if limiter is not None:
        limiter._active = 0
    assert in_flight == 0, "a request was still in flight after the test"


@pytest.fixture
def app(attach_app: AttachApp, settings: Settings) -> FastAPI:
    """Return the shared application with this test's settings and fresh state, logging captured."""
    return attach_app(settings)


@pytest.fixture
def private_app(settings: Settings, log_stream: io.StringIO) -> FastAPI:
    """Build an application of the test's own, with its log output captured."""
    application = create_app(settings)
    # create_app attaches a stdout handler of its own; re-point it at the buffer.
    configure_logging(settings.log_level, stream=log_stream)
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Return an HTTP client bound to the app in-process."""
    # Unhandled exceptions must come back as the 500 response, not re-raise into the test.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Migrate the real schema once; every test works on its own copy of the file."""
    path = tmp_path_factory.mktemp("template") / "dockmin.db"
    connection = connect(path)
    try:
        migrate(connection)
    finally:
        connection.close()
    return path


@pytest.fixture
def database(migrated_template: Path, tmp_path: Path) -> Database:
    """Return a fully migrated database in a temporary directory."""
    path = tmp_path / "dockmin.db"
    shutil.copyfile(migrated_template, path)
    return Database(path)


@pytest.fixture(scope="session")
def provisioned_template(migrated_template: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build once a migrated database holding an administrator and a standard user."""
    path = tmp_path_factory.mktemp("provisioned") / "dockmin.db"
    provision(migrated_template, path)
    return path


@asynccontextmanager
async def serving_app(app: FastAPI, settings: Settings, template: Path) -> AsyncIterator[FastAPI]:
    shutil.copyfile(template, settings.db_path)
    # Startup registers the key forms with the process-wide scrubber; later tests start clean.
    held = DEFAULT_SCRUBBER._secret_values
    try:
        async with app.router.lifespan_context(app):
            # The scheduler's catch-up refresh starts with the lifespan; let it finish before a
            # test seeds environments, or it races the seeding.
            await app.state.scheduler.scheduler.idle()
            yield app
    finally:
        DEFAULT_SCRUBBER._secret_values = held


@pytest.fixture
async def started_app(
    app: FastAPI, settings: Settings, provisioned_template: Path
) -> AsyncIterator[FastAPI]:
    """Run the lifespan over a database that already holds an administrator."""
    async with serving_app(app, settings, provisioned_template):
        yield app


@pytest.fixture
async def fresh_app(
    app: FastAPI, settings: Settings, migrated_template: Path
) -> AsyncIterator[FastAPI]:
    """Run the lifespan over a database with no users, so the setup window is open."""
    async with serving_app(app, settings, migrated_template):
        yield app


@pytest.fixture(scope="module")
def module_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """Settings for the one application a read-only module starts."""
    directory = tmp_path_factory.mktemp("module-app")
    data_dir = directory / "data"
    data_dir.mkdir()
    return load_settings(minimal_env(data_dir, install_compose_stub(directory)))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def module_started_app(
    shared_app: SharedApp, module_settings: Settings, provisioned_template: Path
) -> AsyncIterator[FastAPI]:
    """Run the lifespan once for a module whose tests only read; pair it with module_app_guard."""
    assert AppOptions.from_settings(module_settings) == shared_app.options
    assert not shared_app.claim.module, "another module-scoped fixture holds the shared application"
    shared_app.claim.module = True
    policy = netpolicy.current()
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    application = shared_app.app
    application.dependency_overrides.clear()
    attach_state(application, module_settings, templates=shared_app.templates)
    configure_logging(module_settings.log_level, stream=io.StringIO())
    try:
        async with serving_app(application, module_settings, provisioned_template):
            yield application
    finally:
        shared_app.claim.module = False
        root.handlers[:] = handlers
        root.setLevel(level)
        netpolicy.configure(policy)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def module_browsers(module_started_app: FastAPI) -> AsyncIterator[Browsers]:
    """Open clients against the application a read-only module started."""
    opened = Browsers(module_started_app)
    yield opened
    await opened.close()


@pytest.fixture
def module_app_guard(module_started_app: FastAPI, module_settings: Settings) -> Iterator[None]:
    """Put the application's address policy back before a test and check nothing is in flight."""
    netpolicy.configure(netpolicy.AddressPolicy.from_settings(module_settings))
    yield
    limiter = concurrency_limiter(module_started_app)
    in_flight = 0 if limiter is None else limiter.active
    if limiter is not None:
        limiter._active = 0
    assert in_flight == 0, "a request was still in flight after the test"


@pytest.fixture
async def browsers(started_app: FastAPI) -> AsyncIterator[Browsers]:
    """Open clients against the started application."""
    opened = Browsers(started_app)
    yield opened
    await opened.close()


@pytest.fixture
async def fresh_browsers(fresh_app: FastAPI) -> AsyncIterator[Browsers]:
    """Open clients against the application with the setup window open."""
    opened = Browsers(fresh_app)
    yield opened
    await opened.close()


@pytest.fixture
def admin_client(browsers: Browsers) -> AsyncClient:
    """Return a client holding the administrator's seeded session."""
    return browsers.open(session_token=ADMIN_SESSION_TOKEN)


@pytest.fixture
def standard_client(browsers: Browsers) -> AsyncClient:
    """Return a client holding the standard user's seeded session."""
    return browsers.open(session_token=STANDARD_SESSION_TOKEN)


@pytest.fixture
def anonymous_client(browsers: Browsers) -> AsyncClient:
    """Return a client with no session."""
    return browsers.open()


@pytest.fixture
async def world(started_app: FastAPI, fake_hosts: dict[str, str]) -> AsyncIterator[World]:
    """Seed three environments and make any use of a real host fail or be recorded.

    The registry builds no client, the connection tests answer from a script and refresh
    requests are recorded, not run; the real ones are put back before shutdown.
    """
    seeded = await seed_world(started_app)
    engine = started_app.state.engine
    real = (engine.registry, engine.probes, engine.refresher)
    engine.registry = EngineTripwire(seeded.engine_clients)
    engine.probes = seeded.probes
    engine.refresher = seeded.poker
    try:
        yield seeded
    finally:
        engine.registry, engine.probes, engine.refresher = real


@pytest.fixture
async def container_host(world: World) -> AsyncIterator[ContainerEngine]:
    """Serve every seeded environment from one fake daemon holding the standard containers.

    The standard user is also granted the remote environment, so both roles reach containers.
    """
    fake = stock(ContainerEngine())
    engine = world.app.state.engine
    registry = EngineClientRegistry(lambda settings: fake.bundle())
    engine.registry = registry
    async with world.app.state.database.session() as db:
        await EnvironmentAccessRepository(db).add_user("environment", world.remote_id, STANDARD_ID)
    yield fake
    fake.release_gates()
    await registry.aclose()


@pytest.fixture
async def shared_host(world: World, container_host: ContainerEngine) -> ContainerEngine:
    """The daemon with the standard user's grants over it planted."""
    await share_with_standard_user(world, container_host)
    return container_host


@pytest.fixture
async def terminal_daemon(world: World) -> AsyncIterator[TerminalEngine]:
    """Serve the local environment from a fake daemon on a real socket.

    It holds an interactive container (CONTAINER) and a plain one (PLAIN_CONTAINER), and
    answers exec, attach and log requests. Remote environments stay unreachable.
    """
    engine = world.app.state.engine
    async with serve_terminal_engine() as daemon:
        async with world.app.state.database.session() as db:
            await db.execute(
                "UPDATE environment SET socket_path = ? WHERE id = ?",
                (daemon.unix_path, world.local_id),
            )
        daemon.script_container(CONTAINER, "web", config=INTERACTIVE_CONFIG)
        daemon.script_container(PLAIN_CONTAINER, "plain", config=PLAIN_CONFIG)
        previous = engine.registry
        engine.registry = EngineClientRegistry()
        try:
            yield daemon
        finally:
            await engine.registry.aclose()
            engine.registry = previous


@pytest.fixture
async def live(started_app: FastAPI) -> AsyncIterator[Live]:
    """Serve the started application on a loopback port with uvicorn's most verbose logging."""
    async with serving(started_app) as served:
        yield served


@pytest.fixture
def terminal_limits(monkeypatch: pytest.MonkeyPatch) -> Callable[[TerminalLimits], None]:
    """Return a setter for the timing limits terminals opened by the application will use."""
    chosen: dict[str, TerminalLimits] = {}
    original = TerminalService

    def build(**kwargs: Any) -> TerminalService:
        return original(**kwargs, limits=chosen.get("limits"))

    monkeypatch.setattr(assembly, "TerminalService", build)
    return lambda limits: chosen.update(limits=limits)


@pytest.fixture
async def restricted_stack(world: World, shared_host: ContainerEngine) -> list[str]:
    """Put a container for each class of restricted option, and a plain one, in the shared stack."""
    for cid, host_config in HOST_CONFIGS.items():
        shared_host.add(
            cid,
            f"box-{cid[:4]}",
            labels={STACK_LABEL: STACK},
            host_config={"NetworkMode": "bridge", **host_config},
        )
    return list(HOST_CONFIGS)
