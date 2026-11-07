"""Builders for the container-updater tests: captured inspect samples on a virtual clock."""

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import SecretStr

from app.config import Settings
from app.docker_engine.models import ContainerInspect
from app.services.image_updates import UpdateCheckState
from app.subsystems.container_updater import ContainerUpdater
from app.subsystems.webhook_receiver import WebhookReceiver
from tests.access_support import ENV_A
from tests.automation_support import Subject, detail_of
from tests.image_support import (
    REFERENCE,
    REFERENCE_KEY,
    ScriptedManifests,
    digest,
    full_id,
    register,
)
from tests.provisioning_support import NETWORK, NETWORK_NUMBER, OLD, VOLUME, old_body
from tests.registry_support import ADDRESS, SECRET, bind_image, grant_environment, static_fields
from tests.stack_support import project_box, seed_stack_row

SAMPLES = Path(__file__).parent / "fixtures" / "updater"
HEALTH_NS = 1_000_000_000


class Replay:
    """Serve the inspect samples of one captured shape, picking the one nearest the virtual time.

    The samples were taken about a second apart after the start call returned; `sleep` moves the
    virtual time, so a poll loop sees what a real one would have seen at the same moment.
    """

    def __init__(self, shape: str) -> None:
        self._directory = SAMPLES / f"recreate-{shape}"
        lines = (self._directory / "timeline.txt").read_text(encoding="utf-8").splitlines()
        self._times = [float(line.split("\t")[0]) for line in lines[1:] if line[0].isdigit()]
        self.now = 0.0
        self.sleeps: list[float] = []
        self.containers = SimpleNamespace(inspect=self.inspect)

    def sample(self, index: int) -> ContainerInspect:
        """Return one sample by its position in the timeline."""
        body = (self._directory / f"t{index:02d}.body").read_bytes()
        return ContainerInspect.model_validate_json(body)

    async def inspect(self, container_id: str) -> ContainerInspect:
        """Return the sample nearest the virtual time."""
        nearest = min(range(len(self._times)), key=lambda i: abs(self._times[i] - self.now))
        return self.sample(nearest)

    async def sleep(self, seconds: float) -> None:
        """Move the virtual time without waiting."""
        self.sleeps.append(seconds)
        self.now += seconds


UPDATER = "container-updater"
RECEIVER = "webhook-receiver"
CALLER_ADDRESS = "203.0.113.7"
PENDING_CALLS = 3
ISSUED_AT = "2026-01-01T00:00:00Z"
ACTION = {"action": "update", "verify_seconds": 0}
SHOP = "shop"
SHOP_MEMBERS = ((1, "web"), (2, "worker"))
SHOP_FILE = f"services:\n  web:\n    image: {REFERENCE}\n  worker:\n    image: {REFERENCE}\n"


def container(*names: str) -> dict[str, Any]:
    """Return a container selector naming these containers."""
    return {"kind": "container", "names": list(names)}


def labelled(key: str = "tier", value: str = "front") -> dict[str, Any]:
    """Return a label selector."""
    return {"kind": "labels", "labels": [[key, value]]}


def stack_selector(*names: str) -> dict[str, Any]:
    """Return a stack selector, for the shop stack unless names are given."""
    return {"kind": "stack", "names": list(names or (SHOP,))}


@dataclass(slots=True)
class World:
    """The updater run against a fake daemon, a scripted registry and one rig's database."""

    subject: Subject
    manifests: ScriptedManifests
    rig: Any

    @property
    def host(self) -> Any:
        return self.subject.host

    @property
    def db(self) -> Any:
        return self.rig.db

    async def rule(
        self,
        selector: dict[str, Any],
        parameters: dict[str, Any] | None = None,
        **keywords: Any,
    ) -> tuple[int, int]:
        """Insert a rule in update mode, unless parameters say otherwise, and return its ids."""
        return await self.subject.rule(
            selector, ACTION if parameters is None else parameters, **keywords
        )

    async def run(self, job: int, *, dry: bool = False) -> Any:
        return await self.subject.run(job, dry=dry)

    def advance(self, seconds: float) -> None:
        self.subject.advance(seconds)

    def rows(self, action: str | None = None) -> list[dict[str, Any]]:
        """Return the activity rows of one action with their detail decoded."""
        return [{**row, "detail": detail_of(row)} for row in self.subject.events(action)]

    def verbs(self) -> list[str]:
        return list(self.host.verbs())

    async def save_rule(self, rule_id: int) -> None:
        """Make the rule look edited after everything recorded so far."""
        await self.db.execute(
            "UPDATE automation_rule SET updated_at = ? WHERE id = ?", (FAR_FUTURE, rule_id)
        )

    def new_image(self, number: int) -> None:
        """Make the registry and the next pull answer with the numbered image."""
        self.manifests.default = digest(number)
        self.host.pull_id = full_id(number)
        self.host.pull_digest = digest(number)

    def container_like_web(self, name: str, identifier: int) -> None:
        """Add a second running container built like the first."""
        network_id = next(iter(self.host.networks))
        self.host.add_configured(
            f"{identifier:064x}", name, old_body(network_id, config={"Image": REFERENCE}), "running"
        )


FAR_FUTURE = "2999-01-01T00:00:00Z"


def updater_for(subject: Subject, rig: Any, manifests: ScriptedManifests) -> None:
    """Give the subject a container-updater that reads the scripted registry."""
    registries = rig.registry.registries
    registries.manifests = manifests
    registries.update_checks = UpdateCheckState(rig.clock)
    subject.subsystem = ContainerUpdater(dataclasses.replace(subject.deps, registries=registries))


async def build_world(rig: Any, settings: Settings) -> World:
    """One running container `web` on a bound reference whose registry answers the same image."""
    subject = Subject(rig, settings, ContainerUpdater)
    manifests = ScriptedManifests(default=digest(1))
    updater_for(subject, rig, manifests)
    host = rig.host
    host.add_image(
        REFERENCE,
        full_id(1),
        config={"Env": ["PATH=/bin"], "Cmd": ["serve"]},
        digests=(f"{ADDRESS}/app@{digest(1)}",),
    )
    network_id = host.add_network(NETWORK, NETWORK_NUMBER)
    host.add_volume(VOLUME)
    host.add_configured(OLD, "web", old_body(network_id, config={"Image": REFERENCE}), "running")
    set_id = await register(rig, "main")
    await bind_image(rig.db, set_id, ENV_A, REFERENCE_KEY)
    host.pull_config = {"Env": ["PATH=/bin"], "Cmd": ["serve"]}
    world = World(subject, manifests, rig)
    world.new_image(2)
    return world


async def build_shop(rig: Any, settings: Settings) -> World:
    """A deployed two-service Compose stack on one bound reference, both containers running."""
    subject = Subject(rig, settings, ContainerUpdater)
    manifests = ScriptedManifests(default=digest(2))
    updater_for(subject, rig, manifests)
    host = rig.host
    host.add_image(
        REFERENCE,
        full_id(1),
        config={"Cmd": ["serve"]},
        digests=(f"{ADDRESS}/app@{digest(1)}",),
    )
    host.pull_id = full_id(2)
    host.pull_digest = digest(2)
    await seed_stack_row(rig, ENV_A, SHOP, SHOP_FILE)
    await rig.db.execute(
        "UPDATE stack SET deployed_file_digest = compose_file_digest, "
        "deployed_inputs_digest = inputs_digest, last_deployed_at = ? WHERE name = ?",
        (DEPLOYED_AT, SHOP),
    )
    for number, service in SHOP_MEMBERS:
        project_box(
            host, number, SHOP, service, state="running", image=REFERENCE, image_id=full_id(1)
        )
    created = await rig.registry.credentials.create(
        static_fields("main", url=ADDRESS), SecretStr(SECRET)
    )
    await grant_environment(rig.db, created.id, ENV_A)
    await bind_image(rig.db, created.id, ENV_A, REFERENCE_KEY)
    return World(subject, manifests, rig)


DEPLOYED_AT = "2026-01-01T00:00:00Z"


def member(world: World, number: int) -> str:
    """Return the id of the numbered shop container."""
    found: str = next(
        box.id for box in world.host.boxes.values() if box.name.endswith(f"-{number}")
    )
    return found


def as_webhook(world: Any) -> None:
    """Swap the world's subsystem for a webhook-receiver that reads the same scripted registry."""
    subject = world.subject
    registries = world.rig.registry.registries
    subject.subsystem = WebhookReceiver(dataclasses.replace(subject.deps, registries=registries))
    subject.name = RECEIVER


async def arm_webhook(world: World, rule_id: int) -> None:
    """Give the rule a webhook row holding calls from one caller."""
    await world.db.execute(
        "INSERT INTO webhook (rule_id, token_prefix, token_hash, issued_at, pending_calls, "
        "last_call_address) VALUES (?, ?, ?, ?, ?, ?)",
        (rule_id, "abcd1234", "0" * 64, ISSUED_AT, PENDING_CALLS, CALLER_ADDRESS),
    )
