"""Wiring for container import: the service bound to a request's session and inspector."""

from typing import Annotated

from fastapi import Depends, Request

from app.auth.dependencies import get_activity, get_runtime
from app.auth.services import AuthRuntime
from app.db.engine import Database, Session, get_session
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    AccessServices,
    get_access_services,
    get_engine_runtime,
    get_object_inspector,
)
from app.services.authorisation import ObjectInspector
from app.services.container_import import ContainerImportService
from app.services.container_import_store import PreparedContainerStore
from app.services.engine_setup import EngineRuntime
from app.services.object_inspector import EngineObjectInspector


async def get_container_import_service(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> ContainerImportService:
    """Provide the container import service bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    database: Database = request.app.state.database
    return ContainerImportService(
        db=db,
        database=database,
        authoriser=access.authoriser,
        inspector=inspector,
        locks=engine.locks,
        controls=access.controls,
        activity=activity,
        clock=runtime.clock,
        store=engine.stack_imports,
        prepared=engine.prepared_containers,
        data_dir=request.app.state.settings.data_dir,
    )


def get_prepared_containers(
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
) -> PreparedContainerStore:
    """Provide the store of form state that container imports produced."""
    return engine.prepared_containers


ContainerImports = Annotated[ContainerImportService, Depends(get_container_import_service)]
PreparedContainers = Annotated[PreparedContainerStore, Depends(get_prepared_containers)]
