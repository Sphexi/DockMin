"""Builds the stack dismissal service and the dependency that provides it to a request."""

from typing import Annotated

from fastapi import Depends

from app.auth.dependencies import get_activity, get_runtime
from app.auth.services import AuthRuntime
from app.db.engine import Session, get_session
from app.db.repositories.stack import StackRepository
from app.db.repositories.stack_dismissal import StackDismissalRepository
from app.docker_engine.client import EngineClient
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    AccessServices,
    get_access_services,
    get_engine_runtime,
    get_object_inspector,
)
from app.services.authorisation import ObjectInspector
from app.services.engine_setup import EngineRuntime
from app.services.object_inspector import EngineObjectInspector, HostClients
from app.services.stack_common import concurrent
from app.services.stack_deploy import in_project
from app.services.stack_dismissal_views import HostMembers, compose_members, swarm_members
from app.services.stack_dismissals import MemberReader, StackDismissalService
from app.services.stack_queries import labelled_networks, labelled_volumes, read_project_containers
from app.services.swarm_stacks import read_namespace

__all__ = [
    "StackDismissals",
    "build_stack_dismissal_service",
    "get_stack_dismissal_service",
]


def _compose_reader(hosts: HostClients) -> MemberReader:
    async def read(environment_id: int, key: str) -> HostMembers:
        client: EngineClient = await hosts.client(environment_id)
        async with concurrent() as group:
            containers = group.create_task(read_project_containers(client))
            networks = group.create_task(labelled_networks(client))
            volumes = group.create_task(labelled_volumes(client))
        return compose_members(
            [c for c in containers.result() if in_project(c.labels, key)],
            [v for v in volumes.result() if in_project(v.labels, key)],
            [n for n in networks.result() if in_project(n.labels, key)],
        )

    return read


def _swarm_reader(hosts: HostClients, engine: EngineRuntime) -> MemberReader:
    async def read(environment_id: int, key: str) -> HostMembers:
        client = await hosts.client(environment_id)
        held = await read_namespace(client, environment_id, engine.refresher.poke, key)
        return swarm_members(held.services, held.configs, held.secrets, held.networks)

    return read


def build_stack_dismissal_service(
    db: Session,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    access: AccessServices,
    inspector: EngineObjectInspector,
    activity: ActivityRecorder,
) -> StackDismissalService:
    """Wire the dismissal service around the request's session, authoriser and recorder."""
    hosts = inspector.hosts
    return StackDismissalService(
        db=db,
        authoriser=access.authoriser,
        hosts=hosts,
        stacks=StackRepository(db, engine.vault, runtime.clock),
        dismissals=StackDismissalRepository(db, runtime.clock),
        activity=activity,
        compose=_compose_reader(hosts),
        swarm=_swarm_reader(hosts, engine),
    )


async def get_stack_dismissal_service(
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> StackDismissalService:
    """Provide the dismissal service bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    return build_stack_dismissal_service(db, runtime, engine, access, inspector, activity)


StackDismissals = Annotated[StackDismissalService, Depends(get_stack_dismissal_service)]
