"""Wiring for the onboarding flow: its service, the review jobs and the held reviews."""

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
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.team import TeamRepository
from app.db.repositories.user import UserRepository
from app.self_identity import SelfIdentitySource, source_of
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    AccessServices,
    build_access_services,
    build_host_clients,
    build_image_services,
    build_stack_services,
    build_swarm_services,
    get_access_services,
    get_engine_runtime,
    get_object_inspector,
    get_registry_runtime,
)
from app.services.authorisation import ObjectInspector
from app.services.engine_setup import EngineRuntime
from app.services.grantees import GranteeDirectory
from app.services.image_jobs import ImageJobRegistry, JobProgress, JobResult
from app.services.object_inspector import EngineObjectInspector
from app.services.onboarding import OnboardingService
from app.services.onboarding_objects import UncontrolledObjectScanner
from app.services.onboarding_store import OnboardingReviewStore
from app.services.registry_setup import RegistryRuntime
from app.services.volume_backfill import build_volume_backfill

REVIEW_JOB_LABEL = "Adopt everything on this host"


@dataclass(frozen=True, slots=True)
class OnboardingServices:
    """The onboarding service for one request, the registry of running reviews, and held ones."""

    onboarding: OnboardingService
    jobs: ImageJobRegistry
    reviews: OnboardingReviewStore


def build_onboarding_services(
    db: Session,
    database: Database,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    registries: RegistryRuntime,
    access: AccessServices,
    inspector: EngineObjectInspector,
    activity: ActivityRecorder,
    self_identity: SelfIdentitySource,
) -> OnboardingServices:
    """Wire the onboarding service over the stack, Swarm and image services of one session."""
    clock = runtime.clock
    hosts = inspector.hosts
    stacks = build_stack_services(
        db, database, runtime, engine, registries, access, inspector, activity, self_identity
    )
    swarm = build_swarm_services(
        db, database, runtime, engine, registries, access, inspector, activity, self_identity
    )
    images = build_image_services(
        db, database, runtime, engine, registries, access, inspector, activity
    )
    control_rows = ResourceControlRepository(db, clock)
    backfill = build_volume_backfill(
        db=db,
        clock=clock,
        authoriser=access.authoriser,
        inspector=inspector,
        locks=engine.locks,
        controls=access.controls,
        activity=activity,
    )
    service = OnboardingService(
        db=db,
        authoriser=access.authoriser,
        hosts=hosts,
        stacks=stacks.actions,
        swarm_stacks=stacks.swarm_adopter,
        reader=stacks.reader,
        stack_queries=stacks.queries,
        swarm_queries=swarm.stacks,
        scanner=images.binding_candidates,
        bindings=images.bindings,
        binding_rows=ImageBindingRepository(db),
        controls=access.controls,
        control_rows=control_rows,
        objects=UncontrolledObjectScanner(
            hosts=hosts,
            controls=control_rows,
            identity=self_identity,
            backfill=backfill,
            poke=engine.refresher.poke,
        ),
        grantees=GranteeDirectory(UserRepository(db, clock), TeamRepository(db, clock)),
        locks=engine.locks,
        activity=activity,
    )
    return OnboardingServices(service, engine.image_jobs, engine.onboarding_reviews)


@asynccontextmanager
async def open_onboarding_services(
    database: Database,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    registries: RegistryRuntime,
    self_identity: SelfIdentitySource,
) -> AsyncIterator[OnboardingServices]:
    """Open the onboarding services on a database session of their own, closed with the block."""
    async with database.session() as db:
        activity = ActivityRecorder(db, runtime.clock)
        inspector = EngineObjectInspector(
            build_host_clients(db, runtime, engine), poke=engine.refresher.poke
        )
        access = build_access_services(db, runtime, inspector, activity)
        yield build_onboarding_services(
            db, database, runtime, engine, registries, access, inspector, activity, self_identity
        )


async def get_onboarding_services(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    registries: Annotated[RegistryRuntime, Depends(get_registry_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> OnboardingServices:
    """Provide the onboarding services bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    database: Database = request.app.state.database
    return build_onboarding_services(
        db,
        database,
        runtime,
        engine,
        registries,
        access,
        inspector,
        activity,
        source_of(request.app),
    )


Onboarding = Annotated[OnboardingServices, Depends(get_onboarding_services)]


def app_onboarding_services(app: FastAPI) -> AbstractAsyncContextManager[OnboardingServices]:
    """Open the onboarding services on a fresh session of the running application's database."""
    engine: EngineRuntime | None = getattr(app.state, "engine", None)
    registries: RegistryRuntime | None = getattr(app.state, "registries", None)
    if engine is None or registries is None:
        raise RuntimeError("the Engine and registry runtimes have not been started")
    return open_onboarding_services(
        app.state.database, app.state.auth, engine, registries, source_of(app)
    )


def start_onboarding_review(app: FastAPI, principal: Principal, environment_id: int) -> str:
    """Start the review in the background and return the job id; TooManyJobs at the limits.

    The job authorises again on a session of its own, and keeps its finished review in the
    engine's store under a token it reports in the job's result.
    """
    engine: EngineRuntime | None = getattr(app.state, "engine", None)
    if engine is None:
        raise RuntimeError("the Engine runtime has not been started")

    async def run(progress: JobProgress) -> JobResult:
        async with app_onboarding_services(app) as services:
            review = await services.onboarding.review(principal, environment_id, progress)
            token = services.reviews.put(principal.user_id, environment_id, review)
        return JobResult("The review is ready.", flags={"review": token})

    return engine.image_jobs.start(
        "onboarding_review", principal, environment_id, REVIEW_JOB_LABEL, run
    )
