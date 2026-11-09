"""Wiring for stack export: the service for a request and for a response that outlives it."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request

from app.auth.dependencies import get_activity, get_runtime
from app.auth.services import AuthRuntime
from app.db.engine import Database, Session, get_session
from app.db.repositories.stack import StackRepository
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
from app.services.stack_export import StackExportService


def build_stack_export_service(
    db: Session,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    access: AccessServices,
    inspector: EngineObjectInspector,
    activity: ActivityRecorder,
    self_identity: SelfIdentitySource | None = None,
) -> StackExportService:
    """Wire the stack export service over the stack repository and the lock registry."""
    return StackExportService(
        authoriser=access.authoriser,
        inspector=inspector,
        stacks=StackRepository(db, engine.vault, runtime.clock),
        locks=engine.locks,
        activity=activity,
        clock=runtime.clock,
        self_identity=self_identity,
    )


async def get_stack_export_service(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> StackExportService:
    """Provide the stack export service bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    return build_stack_export_service(
        db, runtime, engine, access, inspector, activity, source_of(request.app)
    )


StackExports = Annotated[StackExportService, Depends(get_stack_export_service)]


@asynccontextmanager
async def open_stack_export_service(app: FastAPI) -> AsyncIterator[StackExportService]:
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
        yield build_stack_export_service(
            db, runtime, engine, access, inspector, activity, source_of(app)
        )
