"""Swarm stacks over a fake manager and a Compose stand-in that only resolves a file."""

import copy
import json
import shutil
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml

from app.auth.principal import Principal
from app.compose_cli.binary import ComposeBinary
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.stack import StackRepository, StoredStack, VariableWrite
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    SwarmServices,
    build_access_services,
    build_host_clients,
    build_swarm_services,
    start_stack_job,
)
from app.services.object_inspector import EngineObjectInspector
from app.services.stack_deploy import file_digest
from app.services.swarm_stack_plan import PlanLine
from tests import stack_support
from tests.access_support import ADMIN, ENV_A
from tests.docker_engine.fake_engine import json_reply, split_version
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.stack_support import Rig, wait_job

SWARM_COMPOSE = Path(__file__).with_name("swarm_compose.py")
NAMESPACE_LABEL = "com.docker.stack.namespace"
NAME = "Shop"
CONFIG_VARIABLE = "CFGVAR"
SECRET_VARIABLE = "SECVAR"
SECRET_VALUE = "S3CR3T-SENTINEL-VALUE-3B"
CONFIG_VALUE = "config v1"
PLAIN_IMAGE = "busybox:latest"
TOKEN_LENGTH = 12


class StackDaemon(SwarmEngine):
    """The fake manager with the refusals the stack scenarios script."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse_create: set[str] = set()
        self.network_busy = 0
        self.network_remove_calls = 0
        self.busy_networks: set[str] = set()
        self.busy_secrets: set[str] = set()

    async def _reply(self, scope: Any, body: bytes) -> Any:
        _, path = split_version(scope["path"])
        method = scope["method"]
        if (
            method == "POST"
            and path == "/services/create"
            and json.loads(body).get("Name") in self.refuse_create
        ):
            return json_reply({"message": "engine detail"}, 400)
        if method == "DELETE" and path.startswith("/networks/"):
            self.network_remove_calls += 1
            if self.network_busy > 0:
                self.network_busy -= 1
                return json_reply({"message": "in use by service"}, 400)
            if path.rsplit("/", 1)[-1] in self.busy_networks:
                return json_reply({"message": "network is in use by task"}, 400)
        if (
            method == "DELETE"
            and path.startswith("/secrets/")
            and path.rsplit("/", 1)[-1] in self.busy_secrets
        ):
            return json_reply({"message": "secret is in use by the following service"}, 400)
        return await super()._reply(scope, body)

    def mutations(self, verb: str = "") -> list[str]:
        return [entry for entry in self.mutating() if entry.startswith(verb)]

    def service(self, name: str) -> dict[str, Any]:
        """Return the stored Spec of the service with this name."""
        spec: dict[str, Any] = next(s for s in self.services.values() if s["Spec"]["Name"] == name)[
            "Spec"
        ]
        return spec

    def service_id(self, name: str) -> str:
        return next(i for i, s in self.services.items() if s["Spec"]["Name"] == name)

    def config_names(self) -> list[str]:
        return sorted(c["Spec"]["Name"] for c in self.configs.values())

    def secret_names(self) -> list[str]:
        return sorted(c["Spec"]["Name"] for c in self.secrets.values())

    def service_names(self) -> list[str]:
        return sorted(s["Spec"]["Name"] for s in self.services.values())


class DefaultingDaemon(StackDaemon):
    """The fake manager as the real Engine answers: it fills in defaults the caller never sent.

    `Isolation: default` and `Runtime: container` appear in every stored spec, and a network
    attachment names the network by id where the file named it.
    """

    def _view(self, service: dict[str, Any], status: bool) -> dict[str, Any]:
        found = copy.deepcopy(super()._view(service, status))
        for key in ("Spec", "PreviousSpec"):
            if isinstance(found.get(key), dict):
                self._complete(found[key])
        return found

    def _complete(self, spec: dict[str, Any]) -> None:
        template = spec.setdefault("TaskTemplate", {})
        template.setdefault("Runtime", "container")
        template.setdefault("ContainerSpec", {}).setdefault("Isolation", "default")
        by_name = {network["Name"]: network_id for network_id, network in self.networks.items()}
        for attachment in template.get("Networks", []):
            attachment["Target"] = by_name.get(attachment.get("Target"), attachment.get("Target"))


def shop_file(*, api: bool = True, web_replicas: int = 2, image: str = PLAIN_IMAGE) -> str:
    """A two-service stack using a network, a config and a secret."""
    services: dict[str, Any] = {
        "web": {
            "image": image,
            "deploy": {"replicas": web_replicas},
            "networks": ["front"],
            "configs": ["cfg"],
            "secrets": ["sec"],
        },
    }
    if api:
        services["api"] = {"image": PLAIN_IMAGE, "deploy": {"replicas": 1}}
    return yaml.safe_dump(
        {
            "services": services,
            "networks": {"front": {}},
            "configs": {"cfg": {"environment": CONFIG_VARIABLE}},
            "secrets": {"sec": {"environment": SECRET_VARIABLE}},
        }
    )


def simple_file(**service: Any) -> str:
    """A one-service stack with no network, config or secret of its own."""
    return yaml.safe_dump({"services": {"web": {"image": PLAIN_IMAGE, **service}}})


def shop_variables(config: str = CONFIG_VALUE, secret: str = SECRET_VALUE) -> list[Any]:
    return stack_support.form_variables(
        (CONFIG_VARIABLE, config, False), (SECRET_VARIABLE, secret, True)
    )


def kept_secret_variables(config: str = CONFIG_VALUE) -> list[Any]:
    """Variables that set the config value and keep the stored secret value."""
    return stack_support.form_variables(
        (CONFIG_VARIABLE, config, False), (SECRET_VARIABLE, None, True)
    )


def namespaced_service(name: str, namespace: str, **fields: Any) -> dict[str, Any]:
    """A service spec for the fake manager that carries a stack namespace."""
    return {
        "Name": name,
        "Labels": {NAMESPACE_LABEL: namespace},
        "Mode": {"Replicated": {"Replicas": 1}},
        "TaskTemplate": {"ContainerSpec": {"Image": PLAIN_IMAGE}},
        **fields,
    }


def install_swarm_compose(directory: Path) -> ComposeBinary:
    """Copy the stand-in into `directory`, where it logs its argv, and return its binary."""
    script = directory / SWARM_COMPOSE.name
    shutil.copyfile(SWARM_COMPOSE, script)
    return ComposeBinary((sys.executable, "-I", str(script)))


def compose_log(directory: Path) -> list[dict[str, Any]]:
    """Return every invocation the stand-in in `directory` recorded."""
    path = directory / "argv.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def compose_commands(directory: Path) -> list[str]:
    return [call["cmd"] for call in compose_log(directory)]


@asynccontextmanager
async def build_swarm_rig(
    access_template: Path,
    tmp_path: Path,
    runtime: Any,
    clock: Any,
    vault: Any,
    daemon: SwarmEngine,
) -> AsyncIterator[Rig]:
    """The stack services with both seeded environments typed as managers on `daemon`."""
    directory = tmp_path / "compose"
    directory.mkdir()
    async with stack_support.build(
        access_template, tmp_path, runtime, clock, vault, daemon, operation_timeout=20.0
    ) as built:
        await built.db.execute("UPDATE environment SET type = 'swarm' WHERE id IN (1, 2)")
        built.compose.use_binary(install_swarm_compose(directory))
        await built.compose.detect()
        yield built


async def run_job(
    rig: Rig,
    kind: str,
    name: str,
    work: Callable[[Any, Any], Awaitable[Any]],
    who: Principal = ADMIN,
) -> Any:
    """Run `work` as a stack job of the caller and return the finished job view."""
    job = start_stack_job(rig.app, who, ENV_A, kind, name, work, stack_kind="swarm")  # type: ignore[arg-type]
    return await wait_job(rig, who, job)


async def create_stack(
    rig: Rig,
    text: str | None = None,
    variables: list[Any] | None = None,
    name: str = NAME,
    who: Principal = ADMIN,
) -> Any:
    return await rig.fresh().swarm_actions.create(
        who,
        ENV_A,
        name=name,
        compose_text=text if text is not None else shop_file(),
        variables=shop_variables() if variables is None else variables,
    )


async def deploy_stack(rig: Rig, name: str = NAME, who: Principal = ADMIN) -> Any:
    return await run_job(
        rig,
        "create",
        name,
        lambda services, progress: services.swarm_actions.deploy(who, ENV_A, name, sink=progress),
        who,
    )


async def reviewed_plan(
    rig: Rig, name: str = NAME, *, orphans: bool = False, who: Principal = ADMIN
) -> tuple[Any, Any]:
    """Return the stored stack and the deploy plan a reviewer would be shown."""
    services = rig.fresh()
    row = await services.swarm_planner.require_stack(who, ENV_A, name, "write")
    plan = await services.swarm_planner.deploy_plan(
        who, ENV_A, row, remove_orphans=orphans, resolve_images=True
    )
    return row, plan


async def redeploy_stack(
    rig: Rig,
    name: str = NAME,
    *,
    orphans: bool = False,
    who: Principal = ADMIN,
    **overrides: Any,
) -> Any:
    """Review and run a redeploy; `overrides` replace the bound fields."""
    row, plan = await reviewed_plan(rig, name, orphans=orphans, who=who)
    bound = {
        "expected_file_digest": row.compose_file_digest,
        "expected_inputs_digest": row.inputs_digest or "",
        "expected_plan": plan.digest,
        "remove_orphans": orphans,
        **overrides,
    }
    return await run_job(
        rig,
        "redeploy",
        name,
        lambda svc, progress: svc.swarm_actions.redeploy(who, ENV_A, name, sink=progress, **bound),
        who,
    )


async def remove_stack(rig: Rig, name: str = NAME, who: Principal = ADMIN) -> Any:
    """Review and run a remove."""
    services = rig.fresh()
    row = await services.swarm_planner.require_stack(who, ENV_A, name, "write")
    plan = await services.swarm_planner.remove_plan(who, ENV_A, row)
    return await run_job(
        rig,
        "remove",
        name,
        lambda svc, progress: svc.swarm_actions.remove(
            who, ENV_A, name, stack_id=row.id, expected_members=plan.digest, sink=progress
        ),
        who,
    )


def plan_lines(plan: Any) -> list[str]:
    """Return the plan as `action kind name` lines in plan order."""
    lines: list[PlanLine] = list(plan.lines)
    return [f"{line.action} {line.kind} {line.name}" for line in lines]


def swarm_services(rig: Rig) -> SwarmServices:
    """The Swarm services, with their stack queries, of one request over the rig's database."""
    recorder = ActivityRecorder(rig.db, rig.clock)
    inspector = EngineObjectInspector(build_host_clients(rig.db, rig.runtime, rig.engine))
    access = build_access_services(rig.db, rig.runtime, inspector, recorder)
    return build_swarm_services(
        rig.db,
        rig.database,
        rig.runtime,
        rig.engine,
        rig.registry.registries,
        access,
        inspector,
        recorder,
    )


async def seed_swarm_stack(
    rig: Rig,
    text: str | None = None,
    name: str = NAME,
    *,
    origin: str = "created",
    owner: int | None = None,
    secret: str = SECRET_VALUE,
) -> StoredStack:
    """Insert a swarm stack row with the shop variables, and a control for `owner` if given."""
    body = text if text is not None else shop_file()
    repository = StackRepository(rig.db, rig.registry.vault, rig.clock)
    stack_id = await repository.insert(
        environment_id=ENV_A,
        name=name,
        kind="swarm",
        origin=origin,  # type: ignore[arg-type]
        project_directory=rig.compose.files.new_token(),
        compose_file=body,
        compose_file_digest=file_digest(body),
        created_by_user_id=owner,
    )
    await repository.replace_variables(
        stack_id,
        [
            VariableWrite(CONFIG_VARIABLE, False, CONFIG_VALUE),
            VariableWrite(SECRET_VARIABLE, True, secret),
        ],
    )
    if owner is not None:
        await ResourceControlRepository(rig.db, rig.clock).create(
            ENV_A,
            "stack",
            name,
            is_public=False,
            is_administrators_only=False,
            access_level="write",
            owner_user_id=owner,
        )
    stored = await repository.get(ENV_A, name)
    assert stored is not None
    return stored
