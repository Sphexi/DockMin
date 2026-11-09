"""Wiring for container export: the service for a request and for a response that outlives it."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request

from app.auth.dependencies import get_activity, get_runtime
from app.auth.services import AuthRuntime
from app.db.engine import Database
from app.self_identity import SelfIdentitySource, source_of
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    AccessServices,
    ContainerServices,
    build_access_services,
    build_container_services,
    build_host_clients,
    get_access_services,
    get_container_services,
    get_engine_runtime,
    get_object_inspector,
)
from app.services.authorisation import ObjectInspector
from app.services.container_export import ContainerExportService
from app.services.engine_setup import EngineRuntime
from app.services.object_inspector import EngineObjectInspector


def build_container_export_service(
    runtime: AuthRuntime,
    engine: EngineRuntime,
    access: AccessServices,
    inspector: EngineObjectInspector,
    activity: ActivityRecorder,
    containers: ContainerServices,
    self_identity: SelfIdentitySource | None = None,
) -> ContainerExportService:
    """Wire the container export service over the creation service's form state."""
    return ContainerExportService(
        authoriser=access.authoriser,
        inspector=inspector,
        creation=containers.creation,
        locks=engine.locks,
        activity=activity,
        clock=runtime.clock,
        self_identity=self_identity,
    )


async def get_container_export_service(
    request: Request,
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
    containers: Annotated[ContainerServices, Depends(get_container_services)],
) -> ContainerExportService:
    """Provide the container export service bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    return build_container_export_service(
        runtime, engine, access, inspector, activity, containers, source_of(request.app)
    )


ContainerExports = Annotated[ContainerExportService, Depends(get_container_export_service)]


@asynccontextmanager
async def open_container_export_service(app: FastAPI) -> AsyncIterator[ContainerExportService]:
    """Open the export service on its own session, for a response that outlives the request."""
    engine: EngineRuntime | None = getattr(app.state, "engine", None)
    if engine is None:
        raise RuntimeError("the Engine runtime has not been started")
    runtime: AuthRuntime = app.state.auth
    database: Database = app.state.database
    identity = source_of(app)
    async with database.session() as db:
        activity = ActivityRecorder(db, runtime.clock)
        inspector = EngineObjectInspector(
            build_host_clients(db, runtime, engine), poke=engine.refresher.poke
        )
        access = build_access_services(db, runtime, inspector, activity)
        containers = build_container_services(
            db,
            database,
            runtime,
            engine,
            access,
            inspector,
            activity,
            app.state.settings,
            getattr(app.state, "registries", None),
            identity,
        )
        yield build_container_export_service(
            runtime, engine, access, inspector, activity, containers, identity
        )
