"""Wiring for bulk binding: its service, the review jobs and the held reviews."""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, Request

from app.auth.dependencies import get_activity, get_runtime
from app.auth.principal import Principal
from app.auth.services import AuthRuntime
from app.db.engine import Database, Session, get_session
from app.db.repositories.image_binding import ImageBindingRepository
from app.db.repositories.registry_access import RegistryAccessRepository
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    AccessServices,
    build_access_services,
    build_host_clients,
    build_image_services,
    get_access_services,
    get_engine_runtime,
    get_object_inspector,
    get_registry_runtime,
)
from app.services.authorisation import ObjectInspector
from app.services.bulk_binding_store import BulkBindingReviewStore
from app.services.bulk_bindings import BulkBindingService
from app.services.engine_setup import EngineRuntime
from app.services.image_jobs import JobProgress, JobResult
from app.services.object_inspector import EngineObjectInspector
from app.services.registry_setup import RegistryRuntime

REVIEW_JOB_LABEL = "Bind unbound images"


@dataclass(frozen=True, slots=True)
class BulkBindingServices:
    """The bulk binding service for one request and the store of held reviews."""

    service: BulkBindingService
    reviews: BulkBindingReviewStore


def build_bulk_binding_services(
    db: Session,
    database: Database,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    registries: RegistryRuntime,
    access: AccessServices,
    inspector: EngineObjectInspector,
    activity: ActivityRecorder,
) -> BulkBindingServices:
    """Wire the service over the image services of one session."""
    images = build_image_services(
        db, database, runtime, engine, registries, access, inspector, activity
    )
    service = BulkBindingService(
        authoriser=access.authoriser,
        hosts=inspector.hosts,
        scanner=images.binding_candidates,
        bindings=images.bindings,
        binding_rows=ImageBindingRepository(db),
        access=RegistryAccessRepository(db),
        reviews=engine.bulk_binding_reviews,
    )
    return BulkBindingServices(service, engine.bulk_binding_reviews)


@asynccontextmanager
async def open_bulk_binding_services(
    database: Database,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    registries: RegistryRuntime,
) -> AsyncIterator[BulkBindingServices]:
    """Open the services on a database session of their own, closed with the block."""
    async with database.session() as db:
        activity = ActivityRecorder(db, runtime.clock)
        inspector = EngineObjectInspector(
            build_host_clients(db, runtime, engine), poke=engine.refresher.poke
        )
        access = build_access_services(db, runtime, inspector, activity)
        yield build_bulk_binding_services(
            db, database, runtime, engine, registries, access, inspector, activity
        )


async def get_bulk_binding_services(
    db: Annotated[Session, Depends(get_session)],
    request: Request,
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    registries: Annotated[RegistryRuntime, Depends(get_registry_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> BulkBindingServices:
    """Provide the bulk binding services bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    database: Database = request.app.state.database
    return build_bulk_binding_services(
        db, database, runtime, engine, registries, access, inspector, activity
    )


BulkBinding = Annotated[BulkBindingServices, Depends(get_bulk_binding_services)]


def app_bulk_binding_services(
    app: FastAPI,
) -> AbstractAsyncContextManager[BulkBindingServices]:
    """Open the services on a fresh session of the running application's database."""
    engine: EngineRuntime | None = getattr(app.state, "engine", None)
    registries: RegistryRuntime | None = getattr(app.state, "registries", None)
    if engine is None or registries is None:
        raise RuntimeError("the Engine and registry runtimes have not been started")
    return open_bulk_binding_services(app.state.database, app.state.auth, engine, registries)


def start_bulk_binding_review(app: FastAPI, principal: Principal, environment_id: int) -> str:
    """Start the review in the background and return the job id; TooManyJobs at the limits.

    The job authorises again on a session of its own, and keeps its finished review in the
    engine's store under a token it reports in the job's result.
    """
    engine: EngineRuntime | None = getattr(app.state, "engine", None)
    if engine is None:
        raise RuntimeError("the Engine runtime has not been started")

    async def run(progress: JobProgress) -> JobResult:
        async with app_bulk_binding_services(app) as services:
            review = await services.service.review(principal, environment_id, progress)
            token = services.reviews.put(principal.user_id, environment_id, review)
        return JobResult("The review is ready.", flags={"review": token})

    return engine.image_jobs.start(
        "bulk_binding_review", principal, environment_id, REVIEW_JOB_LABEL, run
    )
