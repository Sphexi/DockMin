"""A dockmin that runs in a container on a fake daemon, with a recording registry in front of it."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.db.repositories.image_binding import ImageBindingRepository
from app.registry_client.manifests import ManifestClient
from app.registry_client.references import ImageReference
from app.registry_client.tags import TagClient
from app.services.self_update import (
    UpdateDispatcher,
    UpdatePlan,
    UpdateProgress,
    update_dispatcher_of,
)
from tests.auth_support import ADMIN_ID, csrf_form
from tests.docker_engine.container_engine import Box, ContainerEngine, Publish
from tests.web.environment_support import World
from tests.web.helpers import parse_tags
from tests.web.image_support import hidden_values
from tests.web.self_support import SELF_ID, SELF_NAME, adopt, events, identity_of
from updater.result import Outcome, Reason, Result, write_result

INSTANCE = "/server/instance"
CHECK = "/server/instance/update/check"
CONFIRM = "/server/instance/update/confirm"
REQUEST = "/server/instance/update"
REPOSITORY = "ghcr.io/example/dockmin"
HELPER_REPOSITORY = f"{REPOSITORY}-updater"
RUNNING_TAG = "1.0.0"
TARGET_TAG = "1.2.0"
RUNNING_IMAGE = f"{REPOSITORY}:{RUNNING_TAG}"
TARGET_IMAGE = f"{REPOSITORY}:{TARGET_TAG}"
HELPER_IMAGE = f"{HELPER_REPOSITORY}:{TARGET_TAG}"
RUNNING_ID = "sha256:" + "1" * 64
TARGET_ID = "sha256:" + "2" * 64
HELPER_ID = "sha256:" + "3" * 64
RUNNING_DIGEST = "sha256:" + "a" * 64
TARGET_DIGEST = "sha256:" + "b" * 64
HELPER_DIGEST = "sha256:" + "c" * 64
FOREIGN_DIGEST = "sha256:" + "d" * 64
OTHER_IMAGE_ID = "sha256:" + "4" * 64
OFFERED_TAGS = ("0.9.0", "1.0.0", "1.1.0", "1.2.0", "latest", "1.1.0-rc1")
ANONYMOUS = "anonymous"
SOCKET_PATH = "/var/run/docker.sock"
DATA_VOLUME = "dockmin-data"
GROUP = "999"
STOP_TIMEOUT = 30
UPDATE_ID = "ab" * 16
OTHER_UPDATE_ID = "cd" * 16
SWEEP_ID = "ef" * 16
WAIT_SECONDS = 10.0
STEP_SECONDS = 0.005
POLL_SECONDS = 0.01
IMAGE_ENVIRONMENT = ["PATH=/usr/bin"]
SELF_ENVIRONMENT = ["PATH=/usr/bin", "DOCKMIN_SECRET_KEY_FILE=/run/key", "DOCKMIN_PORT=8080"]
SELF_LABELS = {"app": "dockmin", "tier": "control"}
RESTART_POLICY = {"Name": "unless-stopped", "MaximumRetryCount": 0}
PORT_BINDINGS = {"8080/tcp": [{"HostIp": "", "HostPort": "8080"}]}
FINISHED_AT = "2026-11-03T10:05:00Z"


@dataclass(slots=True)
class Registry:
    """What the registry answers, and every request that reached it."""

    calls: list[str] = field(default_factory=list)
    same_tag_digest: str = RUNNING_DIGEST
    target_digest: str = TARGET_DIGEST
    helper_digest: str = HELPER_DIGEST
    tags: tuple[str, ...] = OFFERED_TAGS

    def digest_of(self, reference: ImageReference) -> str:
        if reference.path.endswith("-updater"):
            return self.helper_digest
        return self.same_tag_digest if reference.tag == RUNNING_TAG else self.target_digest


@dataclass(slots=True)
class Hosting:
    """The application, the daemon holding dockmin's own container, and the pulls seen."""

    app: FastAPI
    world: World
    host: ContainerEngine
    registry: Registry
    pulled: list[str] = field(default_factory=list)
    served: dict[str, str] = field(default_factory=dict)
    baseline: frozenset[str] = frozenset()
    on_pull: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    @property
    def data_dir(self) -> Path:
        return Path(self.app.state.settings.data_dir)

    @property
    def update_root(self) -> Path:
        return self.data_dir / "update"

    @property
    def own(self) -> Box:
        return self.host.boxes[SELF_ID]

    async def rows(self, where: str) -> list[dict[str, Any]]:
        """Return action, result, target and reason of the activity rows matching the condition."""
        return await events(self.app, where)

    async def details(self, action: str) -> list[dict[str, Any]]:
        """Return the decoded detail of every row of the action, oldest first."""
        async with self.app.state.database.session() as db:
            found = await db.fetch_limited(
                "SELECT detail FROM activity_event WHERE action = ? ORDER BY id",
                (action,),
                limit=100,
            )
        return [json.loads(row["detail"]) for row in found]

    async def event_id(self, action: str) -> int:
        """Return the id of the one row of the action."""
        async with self.app.state.database.session() as db:
            row = await db.fetch_one("SELECT id FROM activity_event WHERE action = ?", (action,))
        return int(row["id"])

    def others(self) -> list[str]:
        """Return the ids of the containers a test added, dockmin's own and the stock ones apart."""
        return sorted(i for i in self.host.boxes if i not in self.baseline and i != SELF_ID)

    def update_ids(self) -> list[str]:
        """Return the ids of the updates that have a directory."""
        if not self.update_root.exists():
            return []
        return sorted(entry.name for entry in self.update_root.iterdir() if entry.is_dir())

    def helpers(self) -> list[Box]:
        return [b for b in self.host.boxes.values() if b.labels.get("dockmin.role") == "updater"]

    def dispatcher(self) -> UpdateDispatcher:
        """Replace the application's dispatcher with one that polls and waits in milliseconds."""

        async def quick(seconds: float) -> None:
            await asyncio.sleep(STEP_SECONDS)

        replaced = UpdateDispatcher(self.app, sleep=quick, poll_seconds=POLL_SECONDS)
        self.app.state.update_dispatcher = replaced
        return replaced


def install_registry(monkeypatch: pytest.MonkeyPatch) -> Registry:
    """Answer every registry read from a script and record it; nothing touches a network."""
    script = Registry()

    async def head(self: ManifestClient, target: Any, reference: ImageReference, login: Any) -> str:
        script.calls.append(f"head {reference.path}:{reference.tag}")
        return script.digest_of(reference)

    async def tags(
        self: TagClient, target: Any, reference: ImageReference, login: Any
    ) -> tuple[str, ...]:
        script.calls.append(f"tags {reference.path}")
        return script.tags

    async def granted(self: ImageBindingRepository, environment_id: int, address: str) -> bool:
        return True

    monkeypatch.setattr(ManifestClient, "head_digest", head)
    monkeypatch.setattr(TagClient, "list_tags", tags)
    monkeypatch.setattr(ImageBindingRepository, "has_granted_set_at", granted)
    monkeypatch.setattr("app.services.instance.product_version", lambda: RUNNING_TAG)
    return script


async def make_selfhost(
    world: World, container_host: ContainerEngine, registry: Registry
) -> Hosting:
    """Make dockmin run as a container on the local environment, started with `docker run`."""
    app = world.app
    baseline = frozenset(container_host.boxes)
    async with app.state.database.session() as db:
        row = await db.fetch_one(
            "SELECT socket_path FROM environment WHERE id = ?", (world.local_id,)
        )
    socket = row["socket_path"]
    data_dir = str(app.state.settings.data_dir)
    container_host.add_image(
        RUNNING_IMAGE,
        RUNNING_ID,
        config={"Env": IMAGE_ENVIRONMENT},
        digests=(f"{REPOSITORY}@{RUNNING_DIGEST}",),
    )
    container_host.add(
        SELF_ID,
        SELF_NAME,
        image=RUNNING_IMAGE,
        image_id=RUNNING_ID,
        env=SELF_ENVIRONMENT,
        labels=SELF_LABELS,
        stop_timeout=STOP_TIMEOUT,
        mounts=[
            {"Type": "bind", "Source": SOCKET_PATH, "Destination": socket, "RW": True},
            {
                "Type": "volume",
                "Name": DATA_VOLUME,
                "Source": f"/var/lib/docker/volumes/{DATA_VOLUME}/_data",
                "Destination": data_dir,
                "RW": True,
            },
        ],
        publishes=[Publish(8080, 8080)],
        host_config={
            "NetworkMode": "bridge",
            "GroupAdd": [GROUP],
            "RestartPolicy": RESTART_POLICY,
            "PortBindings": PORT_BINDINGS,
            "Binds": [f"{SOCKET_PATH}:{socket}", f"{DATA_VOLUME}:{data_dir}"],
        },
    )
    hosting = Hosting(app, world, container_host, registry, baseline=baseline)

    async def by_image() -> None:
        query = container_host.requests[-1].query
        helper = query["fromImage"].endswith("-updater")
        hosting.pulled.append(f"{query['fromImage']}:{query.get('tag', '')}")
        container_host.pull_id = HELPER_ID if helper else TARGET_ID
        kind = "helper" if helper else "target"
        container_host.pull_digest = hosting.served.get(
            kind, registry.helper_digest if helper else registry.target_digest
        )
        for observer in hosting.on_pull:
            await observer()

    container_host.on("pull", by_image)
    adopt(app, identity_of(world))
    return hosting


def form_actions(response: Response) -> list[str]:
    return [
        unescape(tag.attrs["action"])
        for tag in parse_tags(response.text)
        if tag.name == "form" and "action" in tag.attrs
    ]


async def confirmation(client: AsyncClient, tag: str = TARGET_TAG) -> Response:
    """Post the check's choice of target and return the confirmation page."""
    return await client.post(
        CONFIRM, data=csrf_form(client, target_tag=tag, credential_choice=ANONYMOUS)
    )


def request_fields(page: Response, **override: str) -> dict[str, str]:
    """Return what the confirmation page posts, with fields replaced from the arguments."""
    fields = {
        "confirm": "yes",
        "container_id": SELF_ID,
        "boot_id": hidden_values(page, "boot_id")[0],
        "current_digest": RUNNING_DIGEST,
        "target_tag": TARGET_TAG,
        "target_digest": TARGET_DIGEST,
        "helper_digest": HELPER_DIGEST,
        "credential_choice": ANONYMOUS,
    }
    fields.update(override)
    return fields


async def request_update(client: AsyncClient, page: Response, **override: str) -> Response:
    return await client.post(REQUEST, data=csrf_form(client, **request_fields(page, **override)))


async def until(condition: Callable[[], bool], *, within: float = WAIT_SECONDS) -> None:
    """Let the application run until the condition holds, failing after `within` seconds."""
    for _ in range(int(within / STEP_SECONDS)):
        if condition():
            return
        await asyncio.sleep(STEP_SECONDS)
    raise TimeoutError("the condition did not become true")


def update_id_of(response: Response) -> str:
    """Return the id of the update a restarting page follows."""
    found = re.search(r"/server/instance/update/([0-9a-f]{32})", response.text)
    assert found is not None, response.text
    return found.group(1)


async def reaches(app: FastAPI, update_id: str, *phases: str) -> UpdateProgress:
    """Wait until the update is in one of the phases and return where it is."""
    dispatcher = update_dispatcher_of(app)
    await until(lambda: getattr(dispatcher.progress(update_id), "phase", None) in phases)
    progress = dispatcher.progress(update_id)
    assert progress is not None
    return progress


def seeded_plan(update_id: str = UPDATE_ID, event_id: int = 1) -> UpdatePlan:
    return UpdatePlan(
        update_id=update_id,
        created_at="2026-11-03T10:00:00Z",
        requested_by_user_id=ADMIN_ID,
        request_event_id=event_id,
        engine_api_version="1.45",
        old_id=SELF_ID,
        old_name=SELF_NAME,
        stop_timeout=STOP_TIMEOUT,
        old_image=RUNNING_IMAGE,
        new_image=TARGET_IMAGE,
        new_image_id=TARGET_ID,
        create_body={"Image": TARGET_IMAGE},
        health_timeout_seconds=180,
    )


def seed_update(
    hosting: Hosting,
    update_id: str = UPDATE_ID,
    outcome: Outcome | None = None,
    reason: Reason | None = None,
    *,
    excerpt: tuple[str, ...] = (),
    event_id: int = 1,
) -> Path:
    """Leave an update's directory as the helper would: a plan and, for an outcome, a result."""
    directory = hosting.update_root / update_id
    directory.mkdir(parents=True)
    (directory / "plan.json").write_text(
        json.dumps(seeded_plan(update_id, event_id).to_dict()), encoding="utf-8"
    )
    if outcome is not None:
        write_result(
            directory / "result.json",
            Result(
                update_id,
                outcome,
                reason,
                FINISHED_AT,
                "c" * 64 if outcome == "succeeded" else None,
                excerpt,
            ),
        )
    return directory


def seed_result(directory: Path, outcome: Outcome, update_id: str = UPDATE_ID) -> None:
    """Write the result a helper leaves when its swap ended in the outcome."""
    write_result(
        directory / "result.json",
        Result(update_id, outcome, None, FINISHED_AT, "c" * 64, ()),
    )


def add_helper(
    hosting: Hosting,
    update_id: str,
    number: int,
    state: str,
    *,
    name: str | None = None,
    image: str = HELPER_IMAGE,
    image_id: str = HELPER_ID,
    labelled: bool = True,
) -> Box:
    """Put an update helper on the daemon, named and imaged as the dispatcher makes one."""
    hosting.host.add_image(image, image_id)
    labels = {"dockmin.role": "updater", "dockmin.update.id": update_id} if labelled else {}
    return hosting.host.add(
        f"{number:064x}",
        name or f"{SELF_NAME}-updater-{update_id[:8]}",
        state=state,
        image=image,
        image_id=image_id,
        labels=labels,
    )
