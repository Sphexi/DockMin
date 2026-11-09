"""Wiring for stack import: the service bound to a request's stack services and recorder."""

from typing import Annotated

from fastapi import Depends, Request

from app.auth.dependencies import get_activity, get_runtime
from app.auth.services import AuthRuntime
from app.db.engine import Database
from app.self_identity import source_of
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    AccessServices,
    StackServices,
    get_access_services,
    get_engine_runtime,
    get_object_inspector,
    get_stack_services,
)
from app.services.authorisation import ObjectInspector
from app.services.engine_setup import EngineRuntime
from app.services.object_inspector import EngineObjectInspector
from app.services.stack_import import StackImportService


async def get_stack_import_service(
    request: Request,
    stacks: Annotated[StackServices, Depends(get_stack_services)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> StackImportService:
    """Provide the stack import service bound to this request's stack services."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    database: Database = request.app.state.database
    return StackImportService(
        database=database,
        authoriser=access.authoriser,
        inspector=inspector,
        queries=stacks.queries,
        planner=stacks.swarm_planner,
        compose=stacks.actions,
        swarm=stacks.swarm_actions,
        locks=engine.locks,
        activity=activity,
        clock=runtime.clock,
        store=engine.stack_imports,
        imported=engine.imported_volumes,
        data_dir=request.app.state.settings.data_dir,
        self_identity=source_of(request.app),
    )


StackImports = Annotated[StackImportService, Depends(get_stack_import_service)]
