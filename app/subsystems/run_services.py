"""The services a subsystem run acts through, built on the run's own session and recorder."""

import copy
from dataclasses import dataclass
from typing import Final

from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.stack import StackKind, StackRepository
from app.scheduler.model import RunFailed
from app.self_identity import SelfIdentitySource
from app.services.assembly import (
    build_access_services,
    build_host_clients,
    build_image_bindings,
    build_image_services,
    build_pull_credentials,
    build_replaced_images,
    build_stack_services,
    build_swarm_services,
)
from app.services.authorisation import Authoriser
from app.services.container_actions import ContainerActionService
from app.services.container_recreate import ContainerRecreateService
from app.services.image_bindings import BindingPullCredentials
from app.services.image_updates import ImageUpdateService
from app.services.images import ImageActionService
from app.services.object_inspector import EngineObjectInspector, HostClients
from app.services.replaced_images import ReplacedImages
from app.services.stacks import StackService
from app.services.swarm_services import SwarmServiceActions
from app.services.swarm_stack_actions import SwarmStackActions
from app.subsystems.base import RunContext, SubsystemDeps
from app.subsystems.selectors import SelectorKind, authorise_selector, parse_selector_text

REGISTRY_RUNTIME_MISSING: Final = "registry_runtime_missing"
REGISTRY_RUNTIME_MESSAGE: Final = "The registry runtime is not available."


@dataclass(frozen=True, slots=True)
class RunServices:
    """What a run reads and acts through.

    `stacks` and `swarm_stack_actions` are None without a Compose runtime. `image_actions`,
    `updates`, `recreation` and `replaced` are None unless the run asked for images;
    `service_actions` and `pull_credentials` unless it asked for Swarm.
    """

    hosts: HostClients
    inspector: EngineObjectInspector
    container_actions: ContainerActionService
    stacks: StackService | None
    image_actions: ImageActionService | None = None
    updates: ImageUpdateService | None = None
    recreation: ContainerRecreateService | None = None
    replaced: ReplacedImages | None = None
    service_actions: SwarmServiceActions | None = None
    swarm_stack_actions: SwarmStackActions | None = None
    pull_credentials: BindingPullCredentials | None = None


def run_services(
    context: RunContext,
    deps: SubsystemDeps,
    *,
    stacks: bool = True,
    images: bool = False,
    swarm: bool = False,
    self_identity: SelfIdentitySource | None = None,
) -> RunServices:
    """Build the run's services on its session and recorder, taking locks through its registry.

    A dry run's registry refuses every lock, so a service reached by mistake fails the run.
    Pass `stacks=False` when the handler never runs a Compose stack operation. With `images` the
    run also gets image pulls, update checks and container recreation; with `swarm` the Swarm
    service and stack operations and the bound-credential resolver. Either raises RunFailed
    `registry_runtime_missing` when the application has no registry runtime. `self_identity`
    is handed to every service that refuses to act on the container dockmin runs in.
    """
    registries = deps.registries
    if (images or swarm) and registries is None:
        raise RunFailed(REGISTRY_RUNTIME_MISSING, REGISTRY_RUNTIME_MESSAGE)
    engine = copy.copy(deps.engine)
    engine.locks = context.locks
    hosts = build_host_clients(context.db, deps.auth, engine)
    inspector = EngineObjectInspector(hosts, poke=engine.refresher.poke)
    access = build_access_services(context.db, deps.auth, inspector, context.activity)
    container_actions = ContainerActionService(
        db=context.db,
        database=deps.database,
        authoriser=access.authoriser,
        inspector=inspector,
        locks=engine.locks,
        controls=access.controls,
        activity=context.activity,
        clock=deps.auth.clock,
        self_identity=self_identity,
    )
    stack_service = None
    swarm_stack_actions = None
    if (stacks or swarm) and engine.compose is not None:
        stack_parts = build_stack_services(
            context.db,
            deps.database,
            deps.auth,
            engine,
            registries if images or swarm else None,
            access,
            inspector,
            context.activity,
            self_identity,
        )
        stack_service = stack_parts.actions if stacks else None
        swarm_stack_actions = stack_parts.swarm_actions if swarm else None
    service_actions = None
    pull_credentials = None
    if swarm and registries is not None:
        service_actions = build_swarm_services(
            context.db,
            deps.database,
            deps.auth,
            engine,
            registries,
            access,
            inspector,
            context.activity,
            self_identity,
        ).service_actions
        pull_credentials = build_pull_credentials(context.db, deps.auth, registries)
    if not images or registries is None:
        return RunServices(
            hosts,
            inspector,
            container_actions,
            stack_service,
            service_actions=service_actions,
            swarm_stack_actions=swarm_stack_actions,
            pull_credentials=pull_credentials,
        )
    image_services = build_image_services(
        context.db,
        deps.database,
        deps.auth,
        engine,
        registries,
        access,
        inspector,
        context.activity,
    )
    recreation = ContainerRecreateService(
        db=context.db,
        database=deps.database,
        authoriser=access.authoriser,
        inspector=inspector,
        locks=engine.locks,
        controls=access.controls,
        activity=context.activity,
        clock=deps.auth.clock,
        credentials=build_pull_credentials(context.db, deps.auth, registries),
        bindings=build_image_bindings(
            context.db, deps.auth, access.authoriser, hosts, context.activity
        ),
        self_identity=self_identity,
    )
    return RunServices(
        hosts,
        inspector,
        container_actions,
        stack_service,
        image_actions=image_services.actions,
        updates=image_services.updates,
        recreation=recreation,
        replaced=build_replaced_images(
            context.db, deps.auth, hosts, image_services.actions, engine.refresher.poke
        ),
        service_actions=service_actions,
        swarm_stack_actions=swarm_stack_actions,
        pull_credentials=pull_credentials,
    )


async def check_target(
    deps: SubsystemDeps,
    principal: Principal,
    environment_id: int | None,
    text: str,
    authoriser: Authoriser,
    allowed: frozenset[SelectorKind],
    *,
    stack_kinds: frozenset[StackKind] = frozenset({"compose"}),
    refuse_compose_managed: bool = False,
) -> None:
    """Raise unless the principal may write to what the stored or submitted selector names."""
    if environment_id is None:
        raise ValidationFailed({"environment_id": "Choose an environment."})
    selector = parse_selector_text(text, allowed)
    async with deps.database.session() as db:
        hosts = build_host_clients(db, deps.auth, deps.engine)
        stacks = StackRepository(db, deps.engine.vault, deps.auth.clock)
        await authorise_selector(
            principal,
            environment_id,
            selector,
            authoriser,
            hosts,
            stacks,
            stack_kinds=stack_kinds,
            refuse_compose_managed=refuse_compose_managed,
        )
