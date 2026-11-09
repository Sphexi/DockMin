"""Wiring for volume import: the service bound to a request's session and inspector."""

from typing import Annotated

from fastapi import Depends, Request

from app.auth.dependencies import get_activity, get_runtime
from app.auth.services import AuthRuntime
from app.db.engine import Database, Session, get_session
from app.self_identity import source_of
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    AccessServices,
    get_access_services,
    get_engine_runtime,
    get_object_inspector,
)
from app.services.authorisation import ObjectInspector
from app.services.engine_setup import EngineRuntime
from app.services.object_inspector import EngineObjectInspector
from app.services.volume_import import VolumeImportService


async def get_volume_import_service(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> VolumeImportService:
    """Provide the volume import service bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    database: Database = request.app.state.database
    return VolumeImportService(
        db=db,
        database=database,
        authoriser=access.authoriser,
        inspector=inspector,
        locks=engine.locks,
        controls=access.controls,
        activity=activity,
        clock=runtime.clock,
        store=engine.volume_imports,
        data_dir=request.app.state.settings.data_dir,
        self_identity=source_of(request.app),
    )


VolumeImports = Annotated[VolumeImportService, Depends(get_volume_import_service)]
