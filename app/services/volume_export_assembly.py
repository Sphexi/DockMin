"""Wiring for volume export: the service for a request and for a response that outlives it."""

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
    build_access_services,
    build_host_clients,
    get_access_services,
    get_engine_runtime,
    get_object_inspector,
)
from app.services.authorisation import ObjectInspector
from app.services.engine_setup import EngineRuntime
from app.services.object_inspector import EngineObjectInspector
from app.services.volume_export import VolumeExportService


def build_volume_export_service(
    runtime: AuthRuntime,
    engine: EngineRuntime,
    access: AccessServices,
    inspector: EngineObjectInspector,
    activity: ActivityRecorder,
    self_identity: SelfIdentitySource | None = None,
) -> VolumeExportService:
    """Wire the volume export service over the lock registry and the recorder."""
    return VolumeExportService(
        authoriser=access.authoriser,
        inspector=inspector,
        locks=engine.locks,
        activity=activity,
        clock=runtime.clock,
        self_identity=self_identity,
    )


async def get_volume_export_service(
    request: Request,
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> VolumeExportService:
    """Provide the volume export service bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    return build_volume_export_service(
        runtime, engine, access, inspector, activity, source_of(request.app)
    )


VolumeExports = Annotated[VolumeExportService, Depends(get_volume_export_service)]


@asynccontextmanager
async def open_volume_export_service(app: FastAPI) -> AsyncIterator[VolumeExportService]:
    """Open the export service on its own session, for a response that outlives the request."""
    engine: EngineRuntime | None = getattr(app.state, "engine", None)
    if engine is None:
        raise RuntimeError("the Engine runtime has not been started")
    runtime: AuthRuntime = app.state.auth
    database: Database = app.state.database
    async with database.session() as db:
        activity = ActivityRecorder(db, runtime.clock)
        inspector = EngineObjectInspector(
            build_host_clients(db, runtime, engine), poke=engine.refresher.poke
        )
        access = build_access_services(db, runtime, inspector, activity)
        yield build_volume_export_service(
            runtime, engine, access, inspector, activity, source_of(app)
        )
