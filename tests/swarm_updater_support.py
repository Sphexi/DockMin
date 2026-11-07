"""Builders for the Swarm updater tests: a fake manager, a scripted registry, a replayed rollout."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.config import Settings
from app.services.swarm_services import SwarmServiceActions
from app.subsystems.container_updater import ContainerUpdater
from tests.access_support import ENV_A
from tests.automation_support import Subject, detail_of
from tests.docker_engine.swarm_engine import SwarmEngine, plain_spec
from tests.image_support import REFERENCE, REFERENCE_KEY, ScriptedManifests, digest, register
from tests.registry_support import ADDRESS, ECR_ADDRESS, add_ecr, bind_image, grant_environment
from tests.swarm_stack_support import NAME, StackDaemon, create_stack, deploy_stack
from tests.updater_support import updater_for

SAMPLES = Path(__file__).parent / "fixtures" / "swarm_updater"
UPDATE = {"action": "update", "verify_seconds": 0, "suppression_seconds": 0}
REPORT = {"action": "report", "suppression_seconds": 0}
SERVICE = {"kind": "service", "names": ["web"]}
STACK = {"kind": "stack", "names": [NAME]}
SERVICE_NAME = "web"
REPLICAS = 2
DB_REFERENCE = f"{ADDRESS}/db:1"
ECR_REFERENCE = f"{ECR_ADDRESS}/app:1"
ECR_DB_REFERENCE = f"{ECR_ADDRESS}/db:1"
STARTED_AT = "2026-06-01T00:00:00Z"
NEW_DIGEST = "sha256:" + "c" * 64
ENGINE_TEXT = "ENGINE TEXT"
REFRESH_HOURS = 6


class Sleeper:
    """Stands in for the verification poll's sleep and records the pauses it was asked for."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


type Sleep = Callable[[float], Awaitable[None]]


def inject_sleeper(monkeypatch: pytest.MonkeyPatch, sleep: Sleep | None = None) -> Sleep:
    """Make every SwarmServiceActions built from here on poll with this sleep.

    A later call replaces an earlier one.
    """
    sleeper = sleep or Sleeper()
    current = SwarmServiceActions.__init__
    original = getattr(current, "original", current)

    def init(self: SwarmServiceActions, *args: Any, **kwargs: Any) -> None:
        kwargs["sleep"] = sleeper
        original(self, *args, **kwargs)

    init.original = original  # type: ignore[attr-defined]
    monkeypatch.setattr(SwarmServiceActions, "__init__", init)
    return sleeper


async def interrupted(seconds: float) -> None:
    """A sleep that is cancelled, as a run is when its task is stopped during a watch."""
    raise asyncio.CancelledError


type Step = Callable[[SwarmEngine, dict[str, Any]], None]


def after_update(host: SwarmEngine, *steps: Step) -> None:
    """Apply one step to the service after each update the daemon accepts, rollbacks excepted."""
    original = host._service_update
    queue = list(steps)

    def wrapped(service: Any, query: Any, body: Any, headers: Any) -> Any:
        reply = original(service, query, body, headers)
        if query.get("rollback") != "previous" and queue:
            queue.pop(0)(host, service)
        return reply

    host._service_update = wrapped  # type: ignore[method-assign]


def set_state(service: dict[str, Any], state: str, **fields: Any) -> None:
    """Put the service in an update state the way the Engine moves it, moving its version."""
    service["UpdateStatus"] = {"State": state, "Message": ENGINE_TEXT, **fields}
    service["Version"]["Index"] += 1


def paused(host: SwarmEngine, service: dict[str, Any]) -> None:
    set_state(service, "paused")


def swarm_rolled_back(host: SwarmEngine, service: dict[str, Any]) -> None:
    set_state(service, "rollback_completed")


def still_updating(host: SwarmEngine, service: dict[str, Any]) -> None:
    set_state(service, "updating", StartedAt=STARTED_AT)


def crashed(host: SwarmEngine, service: dict[str, Any]) -> None:
    """Complete the rollout while every task that should run has failed."""
    set_state(service, "completed", StartedAt=STARTED_AT)
    for task in host.tasks.values():
        if task["ServiceID"] == service["ID"] and task["DesiredState"] == "running":
            task["Status"]["State"] = "failed"


def image_of(host: SwarmEngine, service_id: str) -> str:
    return str(host.services[service_id]["Spec"]["TaskTemplate"]["ContainerSpec"]["Image"])


def rows(subject: Subject, action: str | None = None) -> list[dict[str, Any]]:
    """Return the subsystem's activity rows of one action, detail decoded."""
    return [
        {**row, "detail": detail_of(row)}
        for row in subject.events(action)
        if row["actor_type"] == "subsystem"
    ]


def compact(subject: Subject) -> list[tuple[str, str, str | None]]:
    """Return (action, result, reason) of every row the run left."""
    return [(r["action"], r["result"], r["detail"].get("reason")) for r in rows(subject)]


class Built:
    """One Swarm service, its bound set and the updater that watches them."""

    def __init__(
        self, rig: Any, subject: Subject, manifests: ScriptedManifests, service_id: str, set_id: int
    ) -> None:
        self.rig = rig
        self.subject = subject
        self.manifests = manifests
        self.sid = service_id
        self.set_id = set_id
        self.host: SwarmEngine = rig.host

    @property
    def service(self) -> dict[str, Any]:
        return self.host.services[self.sid]

    @property
    def spec(self) -> dict[str, Any]:
        spec: dict[str, Any] = self.host.services[self.sid]["Spec"]
        return spec

    def image(self) -> str:
        return image_of(self.host, self.sid)

    def set_image(self, text: str) -> None:
        self.spec["TaskTemplate"]["ContainerSpec"]["Image"] = text


async def build_service(
    rig: Any,
    settings: Settings,
    *,
    name: str = SERVICE_NAME,
    replicas: int = REPLICAS,
    remote: int = 2,
    deployed: int = 1,
    image: str | None = None,
) -> Built:
    """A running service on a bound reference, deployed at digest `deployed`, moved to `remote`."""
    subject = Subject(rig, settings, ContainerUpdater)
    manifests = ScriptedManifests(default=digest(remote))
    updater_for(subject, rig, manifests)
    set_id = await register(rig, "main")
    await bind_image(rig.db, set_id, ENV_A, REFERENCE_KEY)
    spec = plain_spec(name, image=image or f"{REFERENCE}@{digest(deployed)}", replicas=replicas)
    return Built(rig, subject, manifests, rig.host.add_service(spec), set_id)


async def ecr_service(
    rig: Any, settings: Settings, *, image: str | None = None, remote: int = 1
) -> tuple[Subject, ScriptedManifests, str]:
    """A service on an Amazon ECR reference whose bound set is derived, at the registry's digest."""
    subject = Subject(rig, settings, ContainerUpdater)
    manifests = ScriptedManifests(default=digest(remote))
    updater_for(subject, rig, manifests)
    created = await add_ecr(rig.registry)
    await grant_environment(rig.db, created.id, ENV_A)
    await bind_image(rig.db, created.id, ENV_A, ECR_REFERENCE)
    spec = plain_spec(
        SERVICE_NAME, image=image or f"{ECR_REFERENCE}@{digest(1)}", replicas=REPLICAS
    )
    return subject, manifests, rig.host.add_service(spec)


def shop_text(web: str = REFERENCE, api: str = DB_REFERENCE) -> str:
    """The Compose text of a two-service Swarm stack on two registry references."""
    return yaml.safe_dump(
        {
            "services": {
                "web": {"image": web, "deploy": {"replicas": 2}},
                "api": {"image": api, "deploy": {"replicas": 1}},
            }
        }
    )


class Shop:
    """A deployed Swarm stack, the updater that watches it and the scripted registry."""

    def __init__(
        self, rig: Any, subject: Subject, manifests: ScriptedManifests, set_id: int
    ) -> None:
        self.rig = rig
        self.subject = subject
        self.manifests = manifests
        self.set_id = set_id
        self.host: StackDaemon = rig.host

    def move(self, new: str = NEW_DIGEST, key: str = REFERENCE_KEY) -> None:
        """Make the registry, and the manager's own digest lookup, answer `new` for the tag."""
        self.manifests.answers[key] = new
        self.host.digests[key] = new

    def member(self, name: str) -> dict[str, Any]:
        return self.host.service(name)

    def member_id(self, name: str) -> str:
        return self.host.service_id(name)

    def sent(self) -> int:
        return len(self.host.mutating())


async def deployed_shop(
    rig: Any, settings: Settings, text: str | None = None, *, ecr: bool = False
) -> Shop:
    """A deployed Swarm stack whose images the scripted registry answers for."""
    subject = Subject(rig, settings, ContainerUpdater)
    manifests = ScriptedManifests(default=rig.host.digest)
    updater_for(subject, rig, manifests)
    if ecr:
        created = await add_ecr(rig.registry)
        await grant_environment(rig.db, created.id, ENV_A)
        set_id = created.id
    else:
        set_id = await register(rig, "main")
    await create_stack(rig, text or shop_text(), variables=[])
    await deploy_stack(rig)
    return Shop(rig, subject, manifests, set_id)


def stack_images(host: SwarmEngine) -> dict[str, str]:
    return {
        s["Spec"]["Name"]: s["Spec"]["TaskTemplate"]["ContainerSpec"]["Image"]
        for s in host.services.values()
    }


class Rollout:
    """Replays a captured rollout onto a fake service, second by second of virtual time.

    Each captured second holds the service document and its task list as the Engine returned
    them. The status, the version and the tasks of the fake service are replaced by the sample
    nearest the virtual time; the spec stays the fake's own.
    """

    def __init__(self, host: SwarmEngine, service_id: str, shape: str, desired: int) -> None:
        self._host = host
        self._service_id = service_id
        self._desired = desired
        self._directory = SAMPLES / shape
        lines = (self._directory / "timeline.txt").read_text(encoding="utf-8").splitlines()
        self._times = [float(line.split()[1]) for line in lines if line.startswith("t=")]
        self.now = 0.0
        self.slept: list[float] = []

    def install(self) -> None:
        """Start the replay when the daemon accepts the update.

        The list with status reports the desired count the service is configured for, which the
        captured task lists cannot: they hold only the tasks that are not yet shut down.
        """
        host = self._host
        update = host._service_update
        view = host._view

        def wrapped(service: Any, query: Any, body: Any, headers: Any) -> Any:
            reply = update(service, query, body, headers)
            self.apply()
            return reply

        def shown(service: dict[str, Any], status: bool) -> dict[str, Any]:
            found = view(service, status)
            if status and service["ID"] == self._service_id:
                found["ServiceStatus"]["DesiredTasks"] = self._desired
            return found

        host._service_update = wrapped  # type: ignore[method-assign]
        host._view = shown  # type: ignore[method-assign]

    def apply(self) -> None:
        index = min(range(len(self._times)), key=lambda i: abs(self._times[i] - self.now))
        document = json.loads((self._directory / f"{index:03d}.service.body").read_bytes())
        service = self._host.services[self._service_id]
        if "UpdateStatus" in document:
            service["UpdateStatus"] = document["UpdateStatus"]
        else:
            service.pop("UpdateStatus", None)
        service["Version"]["Index"] = document["Version"]["Index"]
        for task_id in self._host.service_tasks(self._service_id):
            del self._host.tasks[task_id]
        for task in json.loads((self._directory / f"{index:03d}.tasks.json").read_bytes()):
            self._host.tasks[task["ID"]] = {**task, "ServiceID": self._service_id}

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        self.apply()

    def version(self) -> int:
        """Return the version of the service at the virtual time."""
        return int(self._host.services[self._service_id]["Version"]["Index"])
