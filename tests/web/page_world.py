"""Renders one representative page of every kind once, for both roles, for the structure tests."""

import asyncio
import io
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import ENV_PREFIX, SECRET_KEY_VAR, TLS_SELF_SIGNED_VAR, Settings, load_settings
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.docker_engine.registry import EngineClientRegistry
from app.logging import configure_logging
from app.main import AppOptions, attach_state
from app.self_identity import HomeEnvironment, SelfIdentity
from app.services.activity import ActivityRecorder
from app.services.template_catalogues import CatalogueInput, TemplateCatalogueService
from tests.access_support import container_id
from tests.auth_support import (
    ADMIN_SESSION_TOKEN,
    STANDARD_ID,
    STANDARD_SESSION_TOKEN,
    Browsers,
    csrf_form,
)
from tests.conftest import COMPOSE_STUB, SharedApp, serving_app
from tests.container_support import DB, JOB, STACK, SWARM_LABEL, WEB, stock
from tests.docker_engine.swarm_engine import MANAGER, SwarmEngine, plain_spec
from tests.image_support import image_id
from tests.job_support import ADMIN
from tests.object_support import volume_mount
from tests.support import CHEAP_ARGON2_ENV, VALID_KEY
from tests.template_support import (
    PORTAINER_URL,
    ScriptedFetcher,
    linuxserver_document,
    linuxserver_image,
)
from tests.web.environment_support import seed_stack_rows, seed_world
from tests.web.html_tree import Page
from tests.web.image_support import NGINX, stock_images
from tests.web.registry_support import create_set, custom_form

SHOP_NAMESPACE = "Shop"
IDLE_VOLUME = "idle"
TEAM_NAME = "platform"
REGISTRY_NAME = "registry"
RULE_NAME = "Watch db"
WEBHOOK_RECEIVER = "webhook-receiver"
WEBHOOK_RULE_NAME = "Deploy job"
DOCKMIN_CONTAINER = container_id(0xF0)
DOCKMIN_NAME = "dockmin"
HEALTH_CHECKER = "container-health-checker"
EXECUTABLE_FILE = 0o755
COMPOSE_STACK_NAME = "tracker"
SWARM_STACK_NAME = "Lumen"
UPDATED_REFERENCE = "docker.io/library/nginx:1"
UPDATE_DIGEST = "sha256:" + "a" * 64
UPDATE_CHECKED_AT = "2026-01-02T03:04:05Z"
CONTAINER_BASE = "/environments/{remote}/containers"
BATCH_REMOVE = f"{CONTAINER_BASE}/batch?action=remove"
IMAGE_BASE = "/environments/{remote}/images"
NETWORK_BASE = "/environments/{remote}/networks"
VOLUME_BASE = "/environments/{remote}/volumes"
SWARM_BASE = "/environments/{remote}/swarm"
STACK_BASE = "/environments/{local}/stacks"

PAGES: dict[str, str] = {
    "dashboard": "/",
    "account": "/account",
    "tokens": "/account/tokens",
    "ui-kit": "/ui-kit",
    "environment-new": "/environments/new",
    "environment-detail": "/environments/{remote}",
    "environment-edit": "/environments/{remote}/edit",
    "environment-unreachable": "/environments/{down}",
    "container-list": CONTAINER_BASE,
    "container-detail": f"{CONTAINER_BASE}/{WEB}",
    "container-new": f"{CONTAINER_BASE}/new",
    "container-new-template": f"{CONTAINER_BASE}/new?template={{catalogue}}:heimdall",
    "container-stop-confirm": f"{CONTAINER_BASE}/{WEB}/actions/stop",
    "container-remove-confirm": f"{CONTAINER_BASE}/{JOB}/actions/remove",
    "container-remove-force-confirm": f"{CONTAINER_BASE}/{WEB}/actions/remove?force=1",
    "container-batch-confirm": f"{BATCH_REMOVE}&container={JOB}",
    "container-batch-force-confirm": f"{BATCH_REMOVE}&force=1&container={JOB}",
    "container-recreate-confirm": f"{CONTAINER_BASE}/{WEB}/recreate",
    "container-connect": f"{CONTAINER_BASE}/{DB}/networks/connect",
    "container-networks": f"{CONTAINER_BASE}/{WEB}?tab=networks",
    "container-stats": f"{CONTAINER_BASE}/{WEB}?tab=stats",
    "container-automation": f"{CONTAINER_BASE}/{WEB}?tab=automation",
    "container-access": f"{CONTAINER_BASE}/{WEB}?tab=access",
    "container-terminal": f"{CONTAINER_BASE}/{WEB}/terminal",
    "image-list": IMAGE_BASE,
    "image-detail": f"{IMAGE_BASE}/{image_id(NGINX)}",
    "image-pull": f"{IMAGE_BASE}/pull",
    "image-remove-confirm": f"{IMAGE_BASE}/{image_id(NGINX)}/remove",
    "image-remove-force-confirm": f"{IMAGE_BASE}/{image_id(NGINX)}/remove?force=1",
    "image-untag-confirm": f"{IMAGE_BASE}/{image_id(NGINX)}/untag?reference=nginx:1",
    "image-prune-confirm": f"{IMAGE_BASE}/prune",
    "image-batch-confirm": f"{IMAGE_BASE}/remove?image={image_id(NGINX)}",
    "network-list": NETWORK_BASE,
    "network-detail": f"{NETWORK_BASE}/{{network}}",
    "network-new": f"{NETWORK_BASE}/new",
    "network-remove-confirm": f"{NETWORK_BASE}/{{network}}/remove",
    "volume-list": VOLUME_BASE,
    "volume-detail": f"{VOLUME_BASE}/data",
    "volume-new": f"{VOLUME_BASE}/-/new",
    "volume-remove-confirm": f"{VOLUME_BASE}/data/remove",
    "volume-prune-confirm": f"{VOLUME_BASE}/-/prune",
    "volume-batch-confirm": f"{VOLUME_BASE}/-/remove?volume={IDLE_VOLUME}",
    "volume-remove-force-confirm": f"{VOLUME_BASE}/{IDLE_VOLUME}/remove?force=1",
    "stack-list": STACK_BASE,
    "stack-new": f"{STACK_BASE}/-/new",
    "stack-new-template": f"{STACK_BASE}/-/new?template={{catalogue}}:heimdall",
    "stack-adopt": f"{STACK_BASE}/-/adopt/{STACK}",
    "swarm-overview": SWARM_BASE,
    "swarm-service-list": f"{SWARM_BASE}/services",
    "swarm-service-detail": f"{SWARM_BASE}/services/{{service}}",
    "swarm-service-automation": f"{SWARM_BASE}/services/{{service}}?tab=automation",
    "swarm-service-access": f"{SWARM_BASE}/services/{{service}}?tab=access",
    "swarm-service-new": f"{SWARM_BASE}/services/new",
    "swarm-service-scale-confirm": f"{SWARM_BASE}/services/{{service}}/scale?replicas=1",
    "swarm-node-list": f"{SWARM_BASE}/nodes",
    "swarm-node-detail": f"{SWARM_BASE}/nodes/{MANAGER}",
    "swarm-config-list": f"{SWARM_BASE}/configs",
    "swarm-config-detail": f"{SWARM_BASE}/configs/{{config}}",
    "swarm-secret-list": f"{SWARM_BASE}/secrets",
    "swarm-stack-list": f"{SWARM_BASE}/stacks",
    "swarm-stack-adopt": f"{SWARM_BASE}/stacks/-/adopt/{SHOP_NAMESPACE}",
    "swarm-task-detail": "/environments/{remote}/swarm/tasks/{task}",
    "template-list": "/templates",
    "template-detail": "/templates/{catalogue}/heimdall",
    "registry-list": "/registries",
    "registry-new": "/registries/new",
    "registry-detail": "/registries/{registry}",
    "object-storage-list": "/object-storage",
    "object-storage-new": "/object-storage/new",
    "backup-list": "/backups",
    "restore": "/restore",
    "server-certificate": "/server/certificate",
    "job-list": "/jobs",
    "job-detail": "/jobs/{job}",
    "job-subsystems": "/jobs/-/subsystems",
    "job-run-detail": "/jobs/-/runs/{run}",
    "automation-overview": "/automation",
    "automation-new": "/automation/rules/new",
    "automation-rule-detail": "/automation/rules/{rule}",
    "automation-rule-edit": "/automation/rules/{rule}/edit",
    "automation-rule-enable": "/automation/rules/{rule}/enable",
    "automation-rule-run": "/automation/rules/{rule}/run",
    "automation-rule-delete": "/automation/rules/{rule}/delete",
    "automation-run-detail": "/automation/rules/{rule}/runs/{run}",
    "automation-webhook-new": (
        f"/automation/rules/new?subsystem={WEBHOOK_RECEIVER}&environment_id={{remote}}"
    ),
    "automation-webhook-detail": "/automation/rules/{webhook}",
    "automation-webhook-edit": "/automation/rules/{webhook}/edit",
    "automation-webhook-enable": "/automation/rules/{webhook}/enable",
    "automation-webhook-run": "/automation/rules/{webhook}/run",
    "automation-webhook-delete": "/automation/rules/{webhook}/delete",
    "automation-webhook-rotate-confirm": "/automation/rules/{webhook}/webhook/rotate",
    "automation-webhook-revoke-confirm": "/automation/rules/{webhook}/webhook/revoke",
    "instance": "/server/instance",
    "instance-restart-confirm": "/server/instance/restart",
    "activity-list": "/activity",
    "activity-detail": "/activity/{event}",
    "user-list": "/users",
    "user-new": "/users/new",
    "user-detail": f"/users/{STANDARD_ID}",
    "team-list": "/teams",
    "team-new": "/teams/new",
    "team-detail": "/teams/{team}",
    "group-list": "/groups",
    "group-new": "/groups/new",
    "group-detail": "/groups/{group}",
    "tag-list": "/tags",
    "not-found": "/environments/999999",
}
ROLES = ("admin", "standard")


@dataclass(frozen=True)
class Rendered:
    status: int
    html: str
    path: str


async def _first_id(app: FastAPI, sql: str) -> int:
    async with app.state.database.session() as db:
        row = await db.fetch_one(sql)
    return int(row[0])


async def _seed_catalogue(app: FastAPI) -> int:
    """Add and enable a linuxserver.io catalogue holding one image, as an administrator would."""
    clock = app.state.auth.clock
    document = linuxserver_document(
        linuxserver_image(
            "heimdall",
            env_vars=[{"name": "PUID", "value": "1000", "optional": False}],
            volumes=[{"path": "/config", "optional": False}],
            ports=[{"external": "8081", "internal": "80", "optional": False}],
            caps=[{"cap_add": "NET_ADMIN", "optional": False}],
        )
    )
    async with app.state.database.session() as db:
        service = TemplateCatalogueService(
            db, clock, ActivityRecorder(db, clock), fetcher=ScriptedFetcher(document)
        )
        catalogue_id = await service.create(
            ADMIN, CatalogueInput("linuxserver", "linuxserver", PORTAINER_URL)
        )
        await service.enable(ADMIN, catalogue_id)
    return catalogue_id


async def _create_rule(admin: AsyncClient, environment_id: int) -> int:
    response = await admin.post(
        "/automation/rules",
        data=csrf_form(
            admin,
            subsystem=HEALTH_CHECKER,
            environment_id=str(environment_id),
            name=RULE_NAME,
            target_kind="container",
            target_name="db",
            schedule_kind="interval",
            schedule_expression="300",
            time_zone="UTC",
            param_max_restarts="2",
            param_window_seconds="600",
        ),
    )
    found = re.search(r"/automation/rules/(\d+)", response.headers["location"])
    assert found, response.text[:500]
    return int(found.group(1))


async def _create_webhook(client: AsyncClient, environment_id: int) -> tuple[int, Rendered]:
    """Create a webhook automation and return its id with the one response that shows its URL."""
    response = await client.post(
        "/automation/rules",
        data=csrf_form(
            client,
            subsystem=WEBHOOK_RECEIVER,
            environment_id=str(environment_id),
            name=WEBHOOK_RULE_NAME,
            target_kind="container",
            target_name="job",
            param_action="redeploy",
            param_verify_seconds="0",
            param_require_healthy="false",
        ),
    )
    found = re.search(r"/automation/rules/(\d+)", response.text)
    assert found, response.text[:500]
    return int(found.group(1)), Rendered(response.status_code, response.text, "/automation/rules")


def _executable_copy(directory: Path) -> Path:
    binary = directory / "docker-compose"
    shutil.copyfile(COMPOSE_STUB, binary)
    binary.chmod(EXECUTABLE_FILE)
    return binary


async def _render(
    settings: Settings, template: Path, shared: SharedApp
) -> dict[tuple[str, str], Rendered]:
    assert AppOptions.from_settings(settings) == shared.options
    app = shared.app
    app.dependency_overrides.clear()
    attach_state(app, settings, templates=shared.templates)
    configure_logging(settings.log_level, stream=io.StringIO())
    rendered: dict[tuple[str, str], Rendered] = {}
    async with serving_app(app, settings, template):
        world = await seed_world(app)
        host = stock_images(stock(SwarmEngine()))
        host.add_network("shop_default", 0x51, Labels={"com.docker.compose.project": STACK})
        host.add_volume("data")
        host.add_volume(IDLE_VOLUME)
        host.boxes[JOB].mounts = [volume_mount(IDLE_VOLUME)]
        service = host.add_service(plain_spec("web"))
        host.add_service(
            {
                "Name": "agent",
                "Labels": {SWARM_LABEL: SHOP_NAMESPACE},
                "Mode": {"Global": {}},
                "TaskTemplate": {"ContainerSpec": {"Image": "busybox:latest"}},
            }
        )
        config = host.add_config("app-conf", b"line one")
        host.add_secret("db-pass")
        engine = app.state.engine
        registry = EngineClientRegistry(lambda settings: host.bundle())
        engine.registry = registry
        async with app.state.database.session() as db:
            await EnvironmentAccessRepository(db).add_user(
                "environment", world.remote_id, STANDARD_ID
            )
            await db.execute(
                "UPDATE environment SET type = 'swarm', swarm_state = 'active', "
                "swarm_control_available = 1 WHERE id = ?",
                (world.remote_id,),
            )
            await db.execute(
                "UPDATE environment SET type = 'standalone', swarm_state = 'active', "
                "swarm_control_available = 0 WHERE id = ?",
                (world.local_id,),
            )
        app.state.self_identity = SelfIdentity(
            container_id=DOCKMIN_CONTAINER,
            source="mountinfo",
            verified=(HomeEnvironment(world.local_id, "unix_socket", DOCKMIN_NAME),),
            restartable_via=world.local_id,
        )
        browsers = Browsers(app)
        admin = browsers.open(session_token=ADMIN_SESSION_TOKEN)
        standard = browsers.open(session_token=STANDARD_SESSION_TOKEN)
        await admin.post("/teams", data=csrf_form(admin, name=TEAM_NAME))
        registry_id = await create_set(admin, custom_form(admin, REGISTRY_NAME))
        rule_id = await _create_rule(admin, world.remote_id)
        webhooks: dict[str, int] = {}
        for role, client in (("admin", admin), ("standard", standard)):
            webhooks[role], _TOKEN_PAGES[role] = await _create_webhook(client, world.remote_id)
        await admin.post(f"/automation/rules/{rule_id}/dry-run", data=csrf_form(admin))
        await app.state.scheduler.scheduler.idle()
        async with app.state.database.session() as db:
            await db.execute(
                "UPDATE environment SET status = 'reachable', failed_stage = NULL, "
                "status_message = NULL, last_contacted_at = last_checked_at WHERE id = ?",
                (world.remote_id,),
            )
            await seed_stack_rows(
                db, world.remote_id, (COMPOSE_STACK_NAME, "compose"), (SWARM_STACK_NAME, "swarm")
            )
            await db.execute(
                "INSERT INTO image_update_status (environment_id, image_reference, state, "
                "remote_digest, checked_at) VALUES (?, ?, 'update_available', ?, ?)",
                (world.remote_id, UPDATED_REFERENCE, UPDATE_DIGEST, UPDATE_CHECKED_AT),
            )
        ids = {
            "local": world.local_id,
            "remote": world.remote_id,
            "down": world.down_id,
            "group": world.group_id,
            "service": service,
            "config": config,
            "task": host.service_tasks(service)[0],
            "network": f"{0x51:064x}",
            "job": await _first_id(app, "SELECT MIN(id) FROM scheduled_job"),
            "event": await _first_id(app, "SELECT MIN(id) FROM activity_event"),
            "team": await _first_id(app, "SELECT MIN(id) FROM team"),
            "registry": registry_id,
            "catalogue": await _seed_catalogue(app),
            "rule": rule_id,
            "run": await _first_id(app, "SELECT MAX(id) FROM job_run"),
        }
        clients = {"admin": admin, "standard": standard}
        for label in sorted(PAGES, key=lambda name: name != "environment-detail"):
            for role, client in clients.items():
                path = PAGES[label].format(**ids, webhook=webhooks[role])
                response = await client.get(path)
                rendered[(role, label)] = Rendered(response.status_code, response.text, path)
        host.release_gates()
        await registry.aclose()
        await browsers.close()
    return rendered


_CACHE: dict[tuple[str, str], Rendered] = {}
_TOKEN_PAGES: dict[str, Rendered] = {}


def render_all(
    settings_env: dict[str, str], template: Path, shared: SharedApp
) -> dict[tuple[str, str], Rendered]:
    """Start the shared application over the provisioned database and fetch every page."""
    if _CACHE:
        return _CACHE
    settings = load_settings(settings_env)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        _CACHE.update(asyncio.run(_render(settings, template, shared)))
        return _CACHE
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def world_environment(directory: Path) -> dict[str, str]:
    """Return the smallest valid settings environment rooted in a directory."""
    data = directory / "data"
    data.mkdir()
    binary = _executable_copy(directory)
    return {
        SECRET_KEY_VAR: VALID_KEY,
        f"{ENV_PREFIX}DATA_DIR": str(data),
        f"{ENV_PREFIX}COMPOSE_BINARY": str(binary),
        f"{ENV_PREFIX}DEVELOPMENT_CHECKS": "true",
        TLS_SELF_SIGNED_VAR: "false",
    } | CHEAP_ARGON2_ENV


Key = tuple[str, str]


def _served(
    tmp_path_factory: pytest.TempPathFactory, provisioned_template: Path, shared: SharedApp
) -> dict[Key, Rendered]:
    directory = tmp_path_factory.mktemp("page-world")
    return render_all(world_environment(directory), provisioned_template, shared)


@pytest.fixture(scope="module", name="pages")
def world_pages(
    tmp_path_factory: pytest.TempPathFactory, provisioned_template: Path, shared_app: SharedApp
) -> dict[Key, Page]:
    """Every page of PAGES for both roles, parsed; the application runs once per process."""
    served = _served(tmp_path_factory, provisioned_template, shared_app)
    return {key: Page(value.status, value.html) for key, value in served.items()}


@pytest.fixture(scope="module", name="webhook_token_pages")
def world_webhook_token_pages(pages: dict[Key, Page]) -> dict[str, Page]:
    """The response that shows a new webhook URL, which no GET reaches, for each role."""
    return {role: Page(found.status, found.html) for role, found in _TOKEN_PAGES.items()}


@pytest.fixture(scope="module", name="page_paths")
def world_paths(
    tmp_path_factory: pytest.TempPathFactory, provisioned_template: Path, shared_app: SharedApp
) -> dict[Key, str]:
    """The path each page of PAGES was fetched from."""
    served = _served(tmp_path_factory, provisioned_template, shared_app)
    return {key: value.path for key, value in served.items()}
