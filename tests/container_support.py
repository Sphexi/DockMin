"""Containers, controls and readers shared by the container service, page and JSON tests."""

from typing import Any

from fastapi import FastAPI

from app.db.repositories.resource_control import AccessLevel, ResourceControlRepository
from app.services.access import ResourceType
from tests.access_support import container_id
from tests.docker_engine.container_engine import ContainerEngine
from tests.web.environment_support import World

STACK_LABEL = "com.docker.compose.project"
SWARM_LABEL = "com.docker.stack.namespace"
STACK = "shop"

WEB = container_id(0xA1)
DB = container_id(0xA2)
CACHE = container_id(0xA3)
WORKER = container_id(0xA4)
JOB = container_id(0xA5)
WARM = container_id(0xA6)
API = container_id(0xA7)
HIDDEN = container_id(0xA8)
ABSENT = container_id(0xFF)

SECRET_ENV_VALUE = "hunter2-env-value"
SECRET_ARGUMENT = "arg-secret-cmd"
SECRET_LABEL_VALUE = "label-secret-value"
URL_CREDENTIAL = "urlpass-secret"
BIND_SOURCE = "/srv/host-layout-path"
PLAIN_ENV_VALUE = "visible-env-value"


OLD_ID = "1" * 64
OLD_IMAGE_ID = "sha256:" + "b" * 64


def inspect_document(
    *,
    name: str = "web",
    config: dict[str, Any] | None = None,
    host: dict[str, Any] | None = None,
    mounts: list[dict[str, Any]] | None = None,
    networks: dict[str, Any] | None = None,
    **top: Any,
) -> dict[str, Any]:
    """Build a container inspect document the way the derivation reads one."""
    return {
        "Id": OLD_ID,
        "Name": f"/{name}",
        "Image": OLD_IMAGE_ID,
        "Config": {"Image": "nginx:1", "Hostname": OLD_ID[:12], **(config or {})},
        "HostConfig": {"NetworkMode": "bridge", **(host or {})},
        "Mounts": mounts or [],
        "NetworkSettings": {"Networks": networks or {}},
        **top,
    }


def stock[E: ContainerEngine](engine: E) -> E:
    """Put one container in each state the list must render on the daemon."""
    engine.add(
        WEB,
        "web",
        labels={STACK_LABEL: STACK, "tier": "front"},
        ports=[{"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"}],
        mounts=[
            {"Type": "bind", "Source": BIND_SOURCE, "Destination": "/srv", "RW": False},
            {"Type": "volume", "Name": "data", "Destination": "/data", "RW": True},
        ],
        env=[f"DB_PASSWORD={SECRET_ENV_VALUE}", f"MODE={PLAIN_ENV_VALUE}"],
        cmd=["serve", f"--token={SECRET_ARGUMENT}"],
        privileged=True,
    )
    engine.add(DB, "db", image="postgres:16", health="unhealthy", labels={STACK_LABEL: STACK})
    engine.add(CACHE, "cache", image="redis:7", state="paused")
    engine.add(WORKER, "worker", image="busybox", state="restarting", restart_count=3)
    engine.add(JOB, "job", image="busybox", state="exited", exit_code=1)
    engine.add(WARM, "warm", image="busybox", health="starting")
    return engine


def stocked_engine() -> ContainerEngine:
    """A new daemon holding the standard set of containers."""
    return stock(ContainerEngine())


async def plant_control(
    app: FastAPI,
    environment_id: int,
    resource_type: ResourceType,
    resource_id: str,
    *,
    public: bool = False,
    administrators_only: bool = False,
    level: AccessLevel = "read",
) -> None:
    """Put a control on an object directly, as an administrator's save would leave it."""
    async with app.state.database.session() as db:
        await ResourceControlRepository(db, app.state.auth.clock).create(
            environment_id,
            resource_type,
            resource_id,
            is_public=public,
            is_administrators_only=administrators_only,
            access_level=level,
            owner_user_id=None,
        )


async def share_with_standard_user(world: World, host: ContainerEngine) -> None:
    """Grant the standard user the shop stack, and one container of each way of missing it.

    The stack is public write. WEB carries a control of its own that only administrators pass,
    JOB is read-only by its own control, API joins the stack under another case of its name,
    HIDDEN spells the stack label key in another case, and CACHE is not granted at all.
    """
    host.add(API, "api", labels={STACK_LABEL: STACK.upper()})
    host.add(HIDDEN, "wrong-key", labels={"Com.Docker.Compose.Project": STACK})
    remote = world.remote_id
    await plant_control(world.app, remote, "stack", STACK, public=True, level="write")
    await plant_control(world.app, remote, "container", WEB, administrators_only=True)
    await plant_control(world.app, remote, "container", JOB, public=True, level="read")
