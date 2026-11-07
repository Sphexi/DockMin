"""Resolves the `from` parameter of an object page into the origin its second back button names."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request

from app.auth.dependencies import CurrentUser, get_activity, get_runtime
from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.auth.services import AuthRuntime
from app.config import Settings, get_settings
from app.db.engine import Session, get_session
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    AccessServices,
    get_access_services,
    get_activity_log_service,
    get_container_services,
    get_engine_runtime,
    get_image_services,
    get_job_services,
    get_network_services,
    get_object_inspector,
    get_registry_runtime,
    get_scheduler_runtime,
    get_stack_services,
    get_swarm_services,
    get_volume_services,
)
from app.services.authorisation import ObjectInspector
from app.services.engine_setup import EngineRuntime
from app.services.object_inspector import EngineObjectInspector
from app.services.origins import OriginResolver, OriginTarget, Reader
from app.web.origin_links import ORIGIN_PARAM

ROUTES: dict[str, str] = {
    "container": "environment_container_detail",
    "service": "environment_swarm_service_detail",
    "network": "environment_network_detail",
    "volume": "environment_volume_detail",
    "image": "environment_image_detail",
    "automation": "automation_rule_detail",
    "activity": "activity_event_detail",
}
PATH_PARAMETERS: dict[str, str] = {
    "container": "container_id",
    "service": "service_id",
    "network": "network_id",
    "volume": "volume_name",
    "image": "image_id",
    "automation": "rule_id",
    "activity": "event_id",
}
STACK_ROUTES: dict[str, tuple[str, str]] = {
    "compose": ("environment_stack_detail", "stack_name"),
    "swarm": ("environment_swarm_stack_detail", "namespace"),
}


def _href_for(request: Request) -> Callable[[str, int | None, str, str], str]:
    def href_for(kind: str, environment_id: int | None, key: str, variant: str) -> str:
        if kind == "stack":
            route, parameter = STACK_ROUTES[variant]
        else:
            route, parameter = ROUTES[kind], PATH_PARAMETERS[kind]
        arguments: dict[str, object] = {parameter: key}
        if environment_id is not None:
            arguments["environment_id"] = environment_id
        return str(request.app.url_path_for(route, **arguments))

    return href_for


def _named(
    read: Callable[[Principal, int, str], Awaitable[str]], variant: str = ""
) -> Callable[[Principal, int | None, str, bool], Awaitable[OriginTarget]]:
    async def reader(
        actor: Principal, environment_id: int | None, key: str, reachable: bool
    ) -> OriginTarget:
        if environment_id is None:
            raise ResourceNotFound("not found")
        name = await read(actor, environment_id, key)
        return OriginTarget(name, key, variant)

    return reader


async def get_origin_resolver(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> OriginResolver:
    """Wire the origin resolver; each kind's services are built only when its origin is read."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")

    async def container(actor: Principal, environment_id: int, key: str) -> str:
        services = await get_container_services(
            request, db, runtime, engine, access, inspector, activity
        )
        return await services.queries.origin_name(actor, environment_id, key)

    async def network(actor: Principal, environment_id: int, key: str) -> str:
        services = await get_network_services(
            request, db, runtime, engine, access, inspector, activity
        )
        return await services.queries.origin_name(actor, environment_id, key)

    async def volume(actor: Principal, environment_id: int, key: str) -> str:
        services = await get_volume_services(
            request, db, runtime, engine, access, inspector, activity
        )
        return await services.queries.origin_name(actor, environment_id, key)

    async def image(actor: Principal, environment_id: int, key: str) -> str:
        services = await get_image_services(
            request, db, runtime, engine, get_registry_runtime(request), access, inspector, activity
        )
        return await services.queries.origin_name(actor, environment_id, key)

    async def service(actor: Principal, environment_id: int, key: str) -> str:
        swarm = await get_swarm_services(request, db, runtime, engine, access, inspector, activity)
        return await swarm.services.origin_name(actor, environment_id, key)

    async def stack(
        actor: Principal, environment_id: int | None, key: str, reachable: bool
    ) -> OriginTarget:
        if environment_id is None:
            raise ResourceNotFound("not found")
        stacks = await get_stack_services(request, db, runtime, engine, access, inspector, activity)
        try:
            name = await stacks.queries.origin_name(actor, environment_id, key)
        except ResourceNotFound:
            if not reachable:
                raise
        else:
            return OriginTarget(name, name, "compose")
        swarm = await get_swarm_services(request, db, runtime, engine, access, inspector, activity)
        name = await swarm.stacks.origin_name(actor, environment_id, key)
        return OriginTarget(name, name, "swarm")

    async def automation(
        actor: Principal, environment_id: int | None, key: str, reachable: bool
    ) -> OriginTarget:
        jobs = await get_job_services(
            db, runtime, get_scheduler_runtime(request), access, activity, inspector
        )
        return OriginTarget(await jobs.rules.origin_name(actor, int(key)), key)

    async def event(
        actor: Principal, environment_id: int | None, key: str, reachable: bool
    ) -> OriginTarget:
        log = await get_activity_log_service(db, runtime, settings)
        await log.exists(actor, int(key))
        return OriginTarget("", key)

    readers: dict[str, Reader] = {
        "container": _named(container),
        "network": _named(network),
        "volume": _named(volume),
        "image": _named(image),
        "service": _named(service),
        "stack": stack,
        "automation": automation,
        "activity": event,
    }

    async def status_of(environment_id: int) -> str:
        return (await inspector.hosts.environment(environment_id)).status

    return OriginResolver(readers, status_of, _href_for(request))


async def resolve_request_origin(
    request: Request,
    actor: CurrentUser,
    resolver: Annotated[OriginResolver, Depends(get_origin_resolver)],
) -> None:
    """Set `request.state.origin` from the one `from` parameter, or to None."""
    request.state.origin = None
    given = request.query_params.getlist(ORIGIN_PARAM)
    if request.method == "GET" and len(given) == 1:
        request.state.origin = await resolver.resolve(actor, given[0])


RequestOrigin = Depends(resolve_request_origin)
