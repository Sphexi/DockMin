"""Per-request assembly of the authorisation services from a database session."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Annotated, cast

from fastapi import Depends, FastAPI, Request

from app.auth.dependencies import get_activity, get_auth_services, get_runtime
from app.auth.principal import Principal
from app.auth.services import AuthRuntime, AuthServices, build_session_service
from app.config import Settings, get_settings
from app.crypto.vault import Vault
from app.db.engine import Database, Session, get_session
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.environment import (
    EnvironmentDashboardRepository,
    EnvironmentRepository,
)
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.environment_group import (
    EnvironmentGroupRepository,
    GroupSummaryRepository,
)
from app.db.repositories.environment_snapshot import EnvironmentSnapshotRepository
from app.db.repositories.environment_tls import EnvironmentTlsRepository
from app.db.repositories.image_binding import ImageBindingRepository
from app.db.repositories.image_update_status import ImageUpdateStatusRepository
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.registry_access import RegistryAccessRepository
from app.db.repositories.registry_credential import (
    RegistryCredentialRepository,
    RegistryListRepository,
)
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.db.repositories.setting import SettingRepository
from app.db.repositories.stack import StackKind, StackListRepository, StackRepository
from app.db.repositories.stack_dismissal import StackDismissalRepository
from app.db.repositories.tag import TagRepository, TagSummaryRepository
from app.db.repositories.team import TeamRepository
from app.db.repositories.team_membership import TeamMembershipRepository
from app.db.repositories.team_roster import TeamRosterRepository
from app.db.repositories.user import UserRepository
from app.db.repositories.webhook import WebhookRepository
from app.registry_client.manifests import ManifestClient
from app.scheduler.setup import SchedulerRuntime
from app.self_identity import SelfIdentitySource, source_of
from app.services.access_policy import AccessPolicyService
from app.services.activity import ActivityRecorder, track_request_changes
from app.services.activity_log import ActivityLogService
from app.services.activity_targets import ActivityTargets
from app.services.authorisation import Authoriser, ObjectInspector, UnavailableInspector
from app.services.automation_rules import AutomationRuleService
from app.services.backups import BackupProduction, BackupService
from app.services.binding_candidates import BindingCandidateScanner
from app.services.compose_setup import ORPHAN_MIN_AGE_SECONDS, ComposeRuntime
from app.services.container_actions import ContainerActionService
from app.services.container_create import ContainerCreateService
from app.services.container_logs import ContainerLogService
from app.services.container_recreate import ContainerRecreateService
from app.services.container_stats import ContainerStatsService
from app.services.containers import ContainerService
from app.services.effective_access import EffectiveAccessService
from app.services.engine_setup import EngineRuntime
from app.services.environment_access import EnvironmentAccessService
from app.services.environment_connector import EnvironmentConnector
from app.services.environment_groups import EnvironmentGroupService
from app.services.environment_overview import EnvironmentOverview
from app.services.environments import EnvironmentService
from app.services.image_bindings import BindingPullCredentials, ImageBindingService, SetCandidates
from app.services.image_jobs import ImageJobRegistry, JobProgress, JobResult
from app.services.image_queries import ImageQueryService
from app.services.image_updates import ImageUpdateService
from app.services.image_views import PullPlan, PushPlan
from app.services.images import ImageActionService
from app.services.jobs import JobService
from app.services.name_suggestions import NameSuggestions, SuggestionSources
from app.services.network_attachments import NetworkAttachmentService
from app.services.networks import NetworkQueryService, NetworkService
from app.services.notifications import NotificationService
from app.services.object_inspector import EngineObjectInspector, HostClients
from app.services.object_storage_admin import ObjectStorageTargetService
from app.services.pull_credentials import AnonymousPullCredentials, PullCredentialResolver
from app.services.registries import RegistryCredentialService
from app.services.registry_resolution import RegistryCredentialResolver
from app.services.registry_setup import RegistryRuntime
from app.services.replaced_images import ReplacedImages
from app.services.resource_controls import ResourceControlService
from app.services.restore import RestoreCoordinator
from app.services.restore_gate import RestoreGate
from app.services.service_logs import ServiceLogService
from app.services.stack_contexts import StackContextService
from app.services.stack_deploy import StackValidator
from app.services.stack_host_read import StackHostReader
from app.services.stack_jobs import JobResult as StackJobResult
from app.services.stack_jobs import StackJobKind, StackJobRegistry, StackProgress
from app.services.stack_logs import StackLogService
from app.services.stack_queries import StackQueryService
from app.services.stacks import StackService, remove_orphan_directories
from app.services.swarm import NodeService, SwarmQueryService
from app.services.swarm_configs import (
    AccessPanelReader,
    SwarmConfigActions,
    SwarmConfigQueries,
    SwarmSecretActions,
    SwarmSecretQueries,
)
from app.services.swarm_service_queries import SwarmServiceQueries
from app.services.swarm_services import SwarmServiceActions
from app.services.swarm_stack_actions import SwarmStackActions
from app.services.swarm_stack_adopt import SwarmStackAdopter
from app.services.swarm_stack_plan import SwarmStackPlanner
from app.services.swarm_stacks import SwarmStackQueries
from app.services.tags import TagService
from app.services.teams import TeamService
from app.services.template_catalogues import TemplateCatalogueService
from app.services.template_fetch import DocumentFetcher
from app.services.template_reader import TemplateReader
from app.services.template_targets import TemplateTargetService
from app.services.terminals import TerminalService
from app.services.volumes import VolumeQueryService, VolumeService
from app.services.webhooks import AuthoriseWrite, WebhookCallLimiter, WebhookService


@dataclass(frozen=True, slots=True)
class AccessServices:
    """The authorisation services for one request."""

    authoriser: Authoriser
    policy: AccessPolicyService
    teams: TeamService
    controls: ResourceControlService
    effective: EffectiveAccessService


def build_access_services(
    db: Session,
    runtime: AuthRuntime,
    inspector: ObjectInspector,
    activity: ActivityRecorder,
) -> AccessServices:
    """Wire the services for one request; pass the request's recorder so every service shares it."""
    clock = runtime.clock
    controls = ResourceControlRepository(db, clock)
    users = UserRepository(db, clock)
    policy = AccessPolicyService(db=db, settings=SettingRepository(db, clock), activity=activity)
    authoriser = Authoriser(
        controls=controls,
        environments=EnvironmentAccessRepository(db),
        policy=policy,
        inspector=inspector,
    )
    return AccessServices(
        authoriser=authoriser,
        policy=policy,
        teams=TeamService(
            db=db,
            teams=TeamRepository(db, clock),
            memberships=TeamMembershipRepository(db, clock),
            roster=TeamRosterRepository(db, clock),
            users=users,
            activity=activity,
        ),
        controls=ResourceControlService(db=db, controls=controls, activity=activity),
        effective=EffectiveAccessService(
            authoriser=authoriser, policy=policy, controls=controls, users=users
        ),
    )


def get_engine_runtime(request: Request) -> EngineRuntime:
    """Return the Engine runtime the application started with."""
    engine: EngineRuntime | None = getattr(request.app.state, "engine", None)
    if engine is None:
        raise RuntimeError("the Engine runtime has not been started")
    return engine


def build_host_clients(db: Session, runtime: AuthRuntime, engine: EngineRuntime) -> HostClients:
    """Wire the per-request lookup from environment id to a negotiated Engine client."""
    clock = runtime.clock
    tls = EnvironmentTlsRepository(db, engine.vault, clock)
    return HostClients(
        environments=EnvironmentRepository(db, clock),
        tls=tls,
        connector=EnvironmentConnector(tls=tls, registry=engine.registry, probes=engine.probes),
    )


async def open_request_changes(request: Request) -> None:
    """Start recording whether an audited mutation commits during this request."""
    track_request_changes(request.scope)


async def get_object_inspector(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    _tracking: Annotated[None, Depends(open_request_changes)],
) -> ObjectInspector:
    """Return the request's container inspector, or one that fails if the Engine is not started."""
    engine: EngineRuntime | None = getattr(request.app.state, "engine", None)
    if engine is None:
        return UnavailableInspector()
    return EngineObjectInspector(
        build_host_clients(db, runtime, engine), poke=engine.refresher.poke
    )


async def get_access_services(
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> AccessServices:
    """Provide the authorisation services bound to this request's database session."""
    return build_access_services(db, runtime, inspector, activity)


Access = Annotated[AccessServices, Depends(get_access_services)]


@dataclass(frozen=True, slots=True)
class EnvironmentServices:
    """The environment, group and tag services for one request."""

    environments: EnvironmentService
    groups: EnvironmentGroupService
    tags: TagService
    access: EnvironmentAccessService


def build_environment_services(
    db: Session,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    authoriser: Authoriser,
    activity: ActivityRecorder,
    connections_changed: Callable[[], None] | None = None,
) -> EnvironmentServices:
    """Wire the environment services for one request around the request's authoriser."""
    clock = runtime.clock
    environments = EnvironmentRepository(db, clock)
    groups = EnvironmentGroupRepository(db, clock)
    tags = TagRepository(db, clock)
    grants = EnvironmentAccessRepository(db)
    users = UserRepository(db, clock)
    teams = TeamRepository(db, clock)
    tls = EnvironmentTlsRepository(db, engine.vault, clock)
    environment_service = EnvironmentService(
        db=db,
        environments=environments,
        dashboard=EnvironmentDashboardRepository(db),
        tls=tls,
        groups=groups,
        tags=tags,
        snapshots=EnvironmentSnapshotRepository(db),
        grants=grants,
        users=users,
        teams=teams,
        authoriser=authoriser,
        connector=EnvironmentConnector(tls=tls, registry=engine.registry, probes=engine.probes),
        registry=engine.registry,
        poker=engine.refresher,
        activity=activity,
        clock=clock,
        stale_after=engine.stale_after,
        connections_changed=connections_changed,
        after_delete=(
            None
            if engine.compose is None
            else partial(
                remove_orphan_directories,
                db,
                engine.compose.files,
                clock,
                min_age_seconds=ORPHAN_MIN_AGE_SECONDS,
            )
        ),
    )
    return EnvironmentServices(
        environments=environment_service,
        groups=EnvironmentGroupService(
            db=db,
            groups=groups,
            summaries=GroupSummaryRepository(db),
            environments=environments,
            grants=grants,
            users=users,
            teams=teams,
            environment_service=environment_service,
            activity=activity,
        ),
        tags=TagService(db=db, tags=tags, summaries=TagSummaryRepository(db), activity=activity),
        access=EnvironmentAccessService(
            grants=grants, environments=environments, groups=groups, users=users, teams=teams
        ),
    )


def connections_changed_of(app: FastAPI) -> Callable[[], None]:
    """Return a callback that asks the running application to resolve its own container again."""

    def changed() -> None:
        resolver = getattr(app.state, "self_identity_resolver", None)
        if resolver is not None:
            resolver.request()

    return changed


async def get_environment_services(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> EnvironmentServices:
    """Provide the environment services bound to this request's session and authoriser."""
    return build_environment_services(
        db, runtime, engine, access.authoriser, activity, connections_changed_of(request.app)
    )


Environments = Annotated[EnvironmentServices, Depends(get_environment_services)]


@dataclass(frozen=True, slots=True)
class ContainerServices:
    """The container services for one request."""

    queries: ContainerService
    actions: ContainerActionService
    creation: ContainerCreateService
    recreation: ContainerRecreateService
    stats: ContainerStatsService
    logs: ContainerLogService
    terminals: TerminalService
    bindings: ImageBindingService


def build_image_bindings(
    db: Session,
    runtime: AuthRuntime,
    authoriser: Authoriser,
    hosts: HostClients,
    activity: ActivityRecorder,
    *,
    resolver: RegistryCredentialResolver | None = None,
    manifests: ManifestClient | None = None,
) -> ImageBindingService:
    """Wire the binding service: what the pull forms offer and what pulls write."""
    return ImageBindingService(
        db=db,
        bindings=ImageBindingRepository(db),
        candidates=SetCandidates(
            access=RegistryAccessRepository(db), environments=EnvironmentAccessRepository(db)
        ),
        authoriser=authoriser,
        hosts=hosts,
        activity=activity,
        clock=runtime.clock,
        resolver=resolver,
        manifests=manifests,
    )


def build_pull_credentials(
    db: Session, runtime: AuthRuntime, registries: RegistryRuntime
) -> BindingPullCredentials:
    """Wire the resolver every pull uses: choice, then binding, then the only fitting set."""
    return BindingPullCredentials(
        bindings=ImageBindingRepository(db),
        candidates=SetCandidates(
            access=RegistryAccessRepository(db), environments=EnvironmentAccessRepository(db)
        ),
        resolver=build_registry_resolver(db, runtime, registries),
    )


def build_container_services(
    db: Session,
    database: Database,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    access: AccessServices,
    inspector: EngineObjectInspector,
    activity: ActivityRecorder,
    settings: Settings | None = None,
    registries: RegistryRuntime | None = None,
    self_identity: SelfIdentitySource | None = None,
) -> ContainerServices:
    """Wire the container services around the request's authoriser, inspector and recorder.

    Terminals need `settings` to re-check the browser session while one is open; without it a
    terminal cannot be opened. Without a registry runtime every pull is anonymous and writes
    no binding.
    """
    clock = runtime.clock
    bindings = build_image_bindings(db, runtime, access.authoriser, inspector.hosts, activity)
    credentials: PullCredentialResolver = (
        AnonymousPullCredentials()
        if registries is None
        else build_pull_credentials(db, runtime, registries)
    )
    return ContainerServices(
        queries=ContainerService(
            authoriser=access.authoriser,
            inspector=inspector,
            controls=access.controls,
            effective=access.effective,
            users=UserRepository(db, clock),
            teams=TeamRepository(db, clock),
        ),
        actions=ContainerActionService(
            db=db,
            database=database,
            authoriser=access.authoriser,
            inspector=inspector,
            locks=engine.locks,
            controls=access.controls,
            activity=activity,
            clock=clock,
            self_identity=self_identity,
        ),
        creation=ContainerCreateService(
            db=db,
            database=database,
            authoriser=access.authoriser,
            inspector=inspector,
            locks=engine.locks,
            controls=access.controls,
            activity=activity,
            clock=clock,
            credentials=credentials,
            bindings=None if registries is None else bindings,
            self_identity=self_identity,
        ),
        recreation=ContainerRecreateService(
            db=db,
            database=database,
            authoriser=access.authoriser,
            inspector=inspector,
            locks=engine.locks,
            controls=access.controls,
            activity=activity,
            clock=clock,
            credentials=credentials,
            bindings=None if registries is None else bindings,
            self_identity=self_identity,
        ),
        stats=ContainerStatsService(
            authoriser=access.authoriser, inspector=inspector, slots=engine.stream_slots
        ),
        logs=ContainerLogService(
            authoriser=access.authoriser,
            inspector=inspector,
            slots=engine.stream_slots,
            instance_name=engine.instance_name,
        ),
        terminals=TerminalService(
            database=database,
            authoriser=access.authoriser,
            inspector=inspector,
            runtime=engine,
            activity=activity,
            clock=clock,
            sessions=None if settings is None else build_session_service(db, settings, runtime),
            scope=partial(_terminal_scope, database, runtime, engine, settings, self_identity),
            self_identity=self_identity,
        ),
        bindings=bindings,
    )


@asynccontextmanager
async def open_container_services(
    database: Database,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    settings: Settings | None,
    registries: RegistryRuntime | None = None,
    self_identity: SelfIdentitySource | None = None,
) -> AsyncIterator[ContainerServices]:
    """Open the container services on a database session of their own, closed with the block.

    For work that outlives a request's session: a stream or terminal authorises on one of these,
    and holds no session while it runs.
    """
    async with database.session() as db:
        activity = ActivityRecorder(db, runtime.clock)
        inspector = EngineObjectInspector(
            build_host_clients(db, runtime, engine), poke=engine.refresher.poke
        )
        access = build_access_services(db, runtime, inspector, activity)
        yield build_container_services(
            db,
            database,
            runtime,
            engine,
            access,
            inspector,
            activity,
            settings,
            registries,
            self_identity,
        )


@asynccontextmanager
async def _terminal_scope(
    database: Database,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    settings: Settings | None,
    self_identity: SelfIdentitySource | None,
) -> AsyncIterator[TerminalService]:
    async with open_container_services(
        database, runtime, engine, settings, self_identity=self_identity
    ) as services:
        yield services.terminals


def app_container_services(app: FastAPI) -> AbstractAsyncContextManager[ContainerServices]:
    """Open the container services on a fresh session of the running application's database."""
    engine: EngineRuntime | None = getattr(app.state, "engine", None)
    if engine is None:
        raise RuntimeError("the Engine runtime has not been started")
    return open_container_services(
        app.state.database,
        app.state.auth,
        engine,
        app.state.settings,
        getattr(app.state, "registries", None),
        source_of(app),
    )


async def get_container_services(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> ContainerServices:
    """Provide the container services bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    database: Database = request.app.state.database
    return build_container_services(
        db,
        database,
        runtime,
        engine,
        access,
        inspector,
        activity,
        request.app.state.settings,
        getattr(request.app.state, "registries", None),
        source_of(request.app),
    )


Containers = Annotated[ContainerServices, Depends(get_container_services)]


def get_registry_runtime(request: Request) -> RegistryRuntime:
    """Return the registry runtime the application started with."""
    registries: RegistryRuntime | None = getattr(request.app.state, "registries", None)
    if registries is None:
        raise RuntimeError("the registry runtime has not been started")
    return registries


def build_registry_resolver(
    db: Session, runtime: AuthRuntime, registries: RegistryRuntime
) -> RegistryCredentialResolver:
    """Wire the pull-time resolver around a database session.

    The resolver reads on the session it is given; derived-secret refreshes write on sessions of
    their own. Automation that outlives a request builds one on a session it opened itself.
    """
    return RegistryCredentialResolver(
        credentials=RegistryCredentialRepository(db, registries.vault, runtime.clock),
        access=RegistryAccessRepository(db),
        environments=EnvironmentAccessRepository(db),
        cache=registries.cache,
    )


@dataclass(frozen=True, slots=True)
class RegistryServices:
    """The registry services and the pull-time resolver for one request."""

    credentials: RegistryCredentialService
    resolver: RegistryCredentialResolver


def build_registry_services(
    db: Session, runtime: AuthRuntime, registries: RegistryRuntime, activity: ActivityRecorder
) -> RegistryServices:
    """Wire the registry services for one request."""
    clock = runtime.clock
    environments = EnvironmentRepository(db, clock)
    users = UserRepository(db, clock)
    teams = TeamRepository(db, clock)
    return RegistryServices(
        credentials=RegistryCredentialService(
            db=db,
            credentials=RegistryCredentialRepository(db, registries.vault, clock),
            listing=RegistryListRepository(db),
            access=RegistryAccessRepository(db),
            bindings=ImageBindingRepository(db),
            environments=environments,
            environment_access=EnvironmentAccessRepository(db),
            grantees=EnvironmentAccessService(
                grants=EnvironmentAccessRepository(db),
                environments=environments,
                groups=EnvironmentGroupRepository(db, clock),
                users=users,
                teams=teams,
            ),
            users=users,
            teams=teams,
            probes=registries.probes,
            activity=activity,
            clock=clock,
        ),
        resolver=build_registry_resolver(db, runtime, registries),
    )


async def get_registry_services(
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    registries: Annotated[RegistryRuntime, Depends(get_registry_runtime)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> RegistryServices:
    """Provide the registry services bound to this request's session."""
    return build_registry_services(db, runtime, registries, activity)


Registries = Annotated[RegistryServices, Depends(get_registry_services)]


@dataclass(frozen=True, slots=True)
class ImageServices:
    """The image services for one request, and the registry of running jobs."""

    queries: ImageQueryService
    actions: ImageActionService
    bindings: ImageBindingService
    binding_candidates: BindingCandidateScanner
    updates: ImageUpdateService
    jobs: ImageJobRegistry


def build_image_services(
    db: Session,
    database: Database,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    registries: RegistryRuntime,
    access: AccessServices,
    inspector: EngineObjectInspector,
    activity: ActivityRecorder,
) -> ImageServices:
    """Wire the image services around the request's authoriser, inspector and recorder."""
    clock = runtime.clock
    hosts = inspector.hosts
    bindings = build_image_bindings(
        db,
        runtime,
        access.authoriser,
        hosts,
        activity,
        resolver=build_registry_resolver(db, runtime, registries),
        manifests=registries.manifests,
    )
    statuses = ImageUpdateStatusRepository(db)
    return ImageServices(
        queries=ImageQueryService(
            authoriser=access.authoriser, hosts=hosts, bindings=bindings, statuses=statuses
        ),
        actions=ImageActionService(
            db=db,
            database=database,
            authoriser=access.authoriser,
            hosts=hosts,
            locks=engine.locks,
            credentials=build_pull_credentials(db, runtime, registries),
            bindings=bindings,
            activity=activity,
            clock=clock,
        ),
        bindings=bindings,
        binding_candidates=BindingCandidateScanner(
            bindings=ImageBindingRepository(db),
            candidates=SetCandidates(
                access=RegistryAccessRepository(db),
                environments=EnvironmentAccessRepository(db),
            ),
            resolver=build_registry_resolver(db, runtime, registries),
            manifests=registries.manifests,
            hosts=hosts,
            authoriser=access.authoriser,
        ),
        updates=ImageUpdateService(
            bindings=ImageBindingRepository(db),
            resolver=build_registry_resolver(db, runtime, registries),
            hosts=hosts,
            authoriser=access.authoriser,
            manifests=registries.manifests,
            state=registries.update_checks,
            clock=clock,
            statuses=statuses,
        ),
        jobs=engine.image_jobs,
    )


def build_replaced_images(
    db: Session,
    runtime: AuthRuntime,
    hosts: HostClients,
    images: ImageActionService,
    poke: Callable[[int], None],
) -> ReplacedImages:
    """Wire the replaced-image records and their removal around a run's own session."""
    return ReplacedImages(db=db, hosts=hosts, images=images, clock=runtime.clock, poke=poke)


@asynccontextmanager
async def open_image_services(
    database: Database, runtime: AuthRuntime, engine: EngineRuntime, registries: RegistryRuntime
) -> AsyncIterator[ImageServices]:
    """Open the image services on a database session of their own, closed with the block.

    For work that outlives a request: a job opens its services here and holds the session only
    while it runs.
    """
    async with database.session() as db:
        activity = ActivityRecorder(db, runtime.clock)
        inspector = EngineObjectInspector(
            build_host_clients(db, runtime, engine), poke=engine.refresher.poke
        )
        access = build_access_services(db, runtime, inspector, activity)
        yield build_image_services(
            db, database, runtime, engine, registries, access, inspector, activity
        )


def app_image_services(app: FastAPI) -> AbstractAsyncContextManager[ImageServices]:
    """Open the image services on a fresh session of the running application's database."""
    engine: EngineRuntime | None = getattr(app.state, "engine", None)
    registries: RegistryRuntime | None = getattr(app.state, "registries", None)
    if engine is None or registries is None:
        raise RuntimeError("the Engine and registry runtimes have not been started")
    return open_image_services(app.state.database, app.state.auth, engine, registries)


async def get_image_services(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    registries: Annotated[RegistryRuntime, Depends(get_registry_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> ImageServices:
    """Provide the image services bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    database: Database = request.app.state.database
    return build_image_services(
        db, database, runtime, engine, registries, access, inspector, activity
    )


Images = Annotated[ImageServices, Depends(get_image_services)]


def start_pull_job(app: FastAPI, principal: Principal, environment_id: int, plan: PullPlan) -> str:
    """Start a pull in the background and return the job id; TooManyJobs at the limits.

    The job authorises again on a session of its own, so a grant revoked since the plan was made
    stops it.
    """
    engine: EngineRuntime | None = getattr(app.state, "engine", None)
    registries: RegistryRuntime | None = getattr(app.state, "registries", None)
    if engine is None or registries is None:
        raise RuntimeError("the Engine and registry runtimes have not been started")

    async def run(progress: JobProgress) -> JobResult:
        async with open_image_services(
            app.state.database, app.state.auth, engine, registries
        ) as services:
            outcome = await services.actions.pull(
                principal, environment_id, plan, progress=progress
            )
        return JobResult(f"Pulled {outcome.reference.familiar}.", outcome.image_id, outcome.binding)

    return engine.image_jobs.start("pull", principal, environment_id, plan.reference.familiar, run)


def start_push_job(app: FastAPI, principal: Principal, environment_id: int, plan: PushPlan) -> str:
    """Start a push in the background and return the job id; TooManyJobs at the limits."""
    engine: EngineRuntime | None = getattr(app.state, "engine", None)
    registries: RegistryRuntime | None = getattr(app.state, "registries", None)
    if engine is None or registries is None:
        raise RuntimeError("the Engine and registry runtimes have not been started")

    async def run(progress: JobProgress) -> JobResult:
        async with open_image_services(
            app.state.database, app.state.auth, engine, registries
        ) as services:
            outcome = await services.actions.push(
                principal, environment_id, plan, progress=progress
            )
        return JobResult(f"Pushed {outcome.reference.familiar}.", outcome.image_id, outcome.binding)

    return engine.image_jobs.start("push", principal, environment_id, plan.reference.familiar, run)


@dataclass(frozen=True, slots=True)
class NetworkServices:
    """The network services for one request."""

    queries: NetworkQueryService
    actions: NetworkService
    attachments: NetworkAttachmentService


def build_network_services(
    db: Session,
    database: Database,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    access: AccessServices,
    inspector: EngineObjectInspector,
    activity: ActivityRecorder,
    self_identity: SelfIdentitySource | None = None,
) -> NetworkServices:
    """Wire the network services around the request's authoriser, inspector and recorder."""
    clock = runtime.clock
    return NetworkServices(
        queries=NetworkQueryService(
            authoriser=access.authoriser,
            inspector=inspector,
            controls=access.controls,
            users=UserRepository(db, clock),
            teams=TeamRepository(db, clock),
            poke=engine.refresher.poke,
        ),
        actions=NetworkService(
            db=db,
            database=database,
            authoriser=access.authoriser,
            inspector=inspector,
            locks=engine.locks,
            controls=access.controls,
            activity=activity,
            clock=clock,
            poke=engine.refresher.poke,
        ),
        attachments=NetworkAttachmentService(
            database=database,
            authoriser=access.authoriser,
            inspector=inspector,
            locks=engine.locks,
            activity=activity,
            clock=clock,
            self_identity=self_identity,
        ),
    )


async def get_network_services(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> NetworkServices:
    """Provide the network services bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    database: Database = request.app.state.database
    return build_network_services(
        db, database, runtime, engine, access, inspector, activity, source_of(request.app)
    )


Networks = Annotated[NetworkServices, Depends(get_network_services)]


@dataclass(frozen=True, slots=True)
class VolumeServices:
    """The volume services for one request."""

    queries: VolumeQueryService
    actions: VolumeService


def build_volume_services(
    db: Session,
    database: Database,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    access: AccessServices,
    inspector: EngineObjectInspector,
    activity: ActivityRecorder,
    self_identity: SelfIdentitySource | None = None,
) -> VolumeServices:
    """Wire the volume services; the force path removes containers through the audited action."""
    clock = runtime.clock
    containers = ContainerActionService(
        db=db,
        database=database,
        authoriser=access.authoriser,
        inspector=inspector,
        locks=engine.locks,
        controls=access.controls,
        activity=activity,
        clock=clock,
        self_identity=self_identity,
    )
    return VolumeServices(
        queries=VolumeQueryService(
            authoriser=access.authoriser,
            inspector=inspector,
            controls=access.controls,
            users=UserRepository(db, clock),
            teams=TeamRepository(db, clock),
            self_identity=self_identity,
        ),
        actions=VolumeService(
            db=db,
            database=database,
            authoriser=access.authoriser,
            inspector=inspector,
            locks=engine.locks,
            controls=access.controls,
            activity=activity,
            clock=clock,
            remove_container=containers.remove,
            self_identity=self_identity,
        ),
    )


async def get_volume_services(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> VolumeServices:
    """Provide the volume services bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    database: Database = request.app.state.database
    return build_volume_services(
        db, database, runtime, engine, access, inspector, activity, source_of(request.app)
    )


Volumes = Annotated[VolumeServices, Depends(get_volume_services)]


@dataclass(frozen=True, slots=True)
class StackServices:
    """The stack services for one request, and the running jobs."""

    queries: StackQueryService
    actions: StackService
    jobs: StackJobRegistry
    swarm_planner: SwarmStackPlanner
    swarm_actions: SwarmStackActions
    swarm_adopter: SwarmStackAdopter
    logs: StackLogService
    reader: StackHostReader
    contexts: StackContextService


@dataclass(frozen=True, slots=True)
class _StackParts:
    """What the Compose and Swarm stack services share, built once per request."""

    stacks: StackRepository
    control_repo: ResourceControlRepository
    tls: EnvironmentTlsRepository
    bindings: ImageBindingService | None
    validator: StackValidator
    queries: StackQueryService
    planner: SwarmStackPlanner
    logs: StackLogService


def _stack_parts(
    db: Session,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    compose: ComposeRuntime,
    registries: RegistryRuntime | None,
    access: AccessServices,
    inspector: EngineObjectInspector,
    activity: ActivityRecorder,
    self_identity: SelfIdentitySource | None = None,
) -> _StackParts:
    clock = runtime.clock
    hosts = inspector.hosts
    poke = engine.refresher.poke
    tls = EnvironmentTlsRepository(db, engine.vault, clock)
    stacks = StackRepository(db, engine.vault, clock)
    control_repo = ResourceControlRepository(db, clock)
    credentials: PullCredentialResolver = (
        AnonymousPullCredentials()
        if registries is None
        else build_pull_credentials(db, runtime, registries)
    )
    bindings = (
        None
        if registries is None
        else build_image_bindings(db, runtime, access.authoriser, hosts, activity)
    )
    validator = StackValidator(
        authoriser=access.authoriser,
        inspector=inspector,
        controls=control_repo,
        credentials=credentials,
        imported=engine.imported_volumes,
    )
    queries = StackQueryService(
        authoriser=access.authoriser,
        inspector=inspector,
        controls=access.controls,
        control_repo=control_repo,
        stacks=stacks,
        listing=StackListRepository(db),
        validator=validator,
        users=UserRepository(db, clock),
        teams=TeamRepository(db, clock),
        runtime=compose,
        tls=tls,
        locks=engine.locks,
        bindings=bindings,
        dismissals=StackDismissalRepository(db, clock),
        poke=poke,
        self_identity=self_identity,
    )
    planner = SwarmStackPlanner(
        authoriser=access.authoriser,
        inspector=inspector,
        hosts=hosts,
        stacks=stacks,
        queries=queries,
        credentials=credentials,
        control_repo=control_repo,
        tls=tls,
        locks=engine.locks,
        poke=poke,
        imported=engine.imported_volumes,
    )
    logs = StackLogService(
        authoriser=access.authoriser,
        inspector=inspector,
        stacks=queries,
        rows=stacks,
        slots=engine.stream_slots,
        poke=poke,
        instance_name=engine.instance_name,
    )
    return _StackParts(stacks, control_repo, tls, bindings, validator, queries, planner, logs)


def build_stack_services(
    db: Session,
    database: Database,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    registries: RegistryRuntime | None,
    access: AccessServices,
    inspector: EngineObjectInspector,
    activity: ActivityRecorder,
    self_identity: SelfIdentitySource | None = None,
) -> StackServices:
    """Wire the stack services around the request's authoriser, inspector and recorder.

    Without a registry runtime every pull is anonymous and writes no binding.
    """
    compose = engine.compose
    if compose is None:
        raise RuntimeError("the Compose runtime has not been started")
    clock = runtime.clock
    hosts = inspector.hosts
    parts = _stack_parts(
        db, runtime, engine, compose, registries, access, inspector, activity, self_identity
    )
    stacks, control_repo, tls = parts.stacks, parts.control_repo, parts.tls
    bindings, validator, queries, planner = (
        parts.bindings,
        parts.validator,
        parts.queries,
        parts.planner,
    )
    bindings_factory = (
        None
        if registries is None
        else lambda session, recorder: build_image_bindings(
            session, runtime, access.authoriser, hosts, recorder
        )
    )
    poke = engine.refresher.poke
    return StackServices(
        queries=queries,
        actions=StackService(
            db=db,
            database=database,
            authoriser=access.authoriser,
            inspector=inspector,
            queries=queries,
            stacks=stacks,
            environments=EnvironmentRepository(db, clock),
            controls=access.controls,
            control_repo=control_repo,
            validator=validator,
            tls=tls,
            locks=engine.locks,
            runtime=compose,
            activity=activity,
            clock=clock,
            vault=engine.vault,
            bindings=bindings,
            bindings_factory=bindings_factory,
            self_identity=self_identity,
            imported=engine.imported_volumes,
        ),
        jobs=engine.stack_jobs,
        logs=parts.logs,
        reader=StackHostReader(
            authoriser=access.authoriser,
            inspector=inspector,
            queries=queries,
            planner=planner,
            locks=engine.locks,
            activity=activity,
            poke=poke,
            self_identity=self_identity,
        ),
        contexts=StackContextService(
            db=db,
            inspector=inspector,
            queries=queries,
            stacks=stacks,
            locks=engine.locks,
            activity=activity,
        ),
        swarm_planner=planner,
        swarm_actions=SwarmStackActions(
            db=db,
            database=database,
            authoriser=access.authoriser,
            inspector=inspector,
            planner=planner,
            queries=queries,
            stacks=stacks,
            environments=EnvironmentRepository(db, clock),
            controls=access.controls,
            control_repo=control_repo,
            locks=engine.locks,
            runtime=compose,
            activity=activity,
            clock=clock,
            vault=engine.vault,
            bindings=bindings,
            poke=poke,
            bindings_factory=bindings_factory,
            self_identity=self_identity,
            imported=engine.imported_volumes,
        ),
        swarm_adopter=SwarmStackAdopter(
            db=db,
            authoriser=access.authoriser,
            inspector=inspector,
            planner=planner,
            queries=queries,
            stacks=stacks,
            environments=EnvironmentRepository(db, clock),
            controls=access.controls,
            locks=engine.locks,
            runtime=compose,
            activity=activity,
            clock=clock,
            poke=poke,
        ),
    )


@asynccontextmanager
async def open_stack_services(
    database: Database,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    registries: RegistryRuntime | None,
    self_identity: SelfIdentitySource | None = None,
) -> AsyncIterator[StackServices]:
    """Open the stack services on a database session of their own, closed with the block.

    For work that outlives a request: a job opens its services here and holds the session only
    while it runs.
    """
    async with database.session() as db:
        activity = ActivityRecorder(db, runtime.clock)
        inspector = EngineObjectInspector(
            build_host_clients(db, runtime, engine), poke=engine.refresher.poke
        )
        access = build_access_services(db, runtime, inspector, activity)
        yield build_stack_services(
            db, database, runtime, engine, registries, access, inspector, activity, self_identity
        )


def app_stack_services(app: FastAPI) -> AbstractAsyncContextManager[StackServices]:
    """Open the stack services on a fresh session of the running application's database."""
    engine: EngineRuntime | None = getattr(app.state, "engine", None)
    if engine is None:
        raise RuntimeError("the Engine runtime has not been started")
    return open_stack_services(
        app.state.database,
        app.state.auth,
        engine,
        getattr(app.state, "registries", None),
        source_of(app),
    )


async def get_stack_services(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> StackServices:
    """Provide the stack services bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    database: Database = request.app.state.database
    return build_stack_services(
        db,
        database,
        runtime,
        engine,
        getattr(request.app.state, "registries", None),
        access,
        inspector,
        activity,
        source_of(request.app),
    )


Stacks = Annotated[StackServices, Depends(get_stack_services)]

type StackWork = Callable[[StackServices, StackProgress], Awaitable[StackJobResult]]


def start_stack_job(
    app: FastAPI,
    principal: Principal,
    environment_id: int,
    kind: StackJobKind,
    stack: str,
    work: StackWork,
    *,
    stack_kind: StackKind = "compose",
) -> str:
    """Start a stack operation in the background and return the job id; TooManyStackJobs.

    `work` runs on services opened for the job, so the operation authorises again on a session
    of its own and a grant revoked since the form was shown stops it.
    """
    engine: EngineRuntime | None = getattr(app.state, "engine", None)
    if engine is None:
        raise RuntimeError("the Engine runtime has not been started")
    registries: RegistryRuntime | None = getattr(app.state, "registries", None)

    async def run(progress: StackProgress) -> StackJobResult:
        async with open_stack_services(
            app.state.database, app.state.auth, engine, registries, source_of(app)
        ) as services:
            return await work(services, progress)

    return engine.stack_jobs.start(
        kind, principal, environment_id, stack, run, stack_kind=stack_kind
    )


@dataclass(frozen=True, slots=True)
class SwarmServices:
    """The Swarm services for one request."""

    queries: SwarmQueryService
    nodes: NodeService
    services: SwarmServiceQueries
    service_actions: SwarmServiceActions
    logs: ServiceLogService
    configs: SwarmConfigQueries
    config_actions: SwarmConfigActions
    secrets: SwarmSecretQueries
    secret_actions: SwarmSecretActions
    stacks: SwarmStackQueries
    stack_logs: StackLogService
    access: AccessServices
    bindings: ImageBindingService | None


def build_swarm_services(
    db: Session,
    database: Database,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    registries: RegistryRuntime | None,
    access: AccessServices,
    inspector: EngineObjectInspector,
    activity: ActivityRecorder,
    self_identity: SelfIdentitySource | None = None,
) -> SwarmServices:
    """Wire the Swarm services around the request's authoriser, inspector and recorder.

    Without a registry runtime every pull is anonymous and writes no binding.
    """
    clock = runtime.clock
    hosts = inspector.hosts
    poke = engine.refresher.poke
    credentials: PullCredentialResolver = (
        AnonymousPullCredentials()
        if registries is None
        else build_pull_credentials(db, runtime, registries)
    )
    bindings = (
        None
        if registries is None
        else build_image_bindings(db, runtime, access.authoriser, hosts, activity)
    )
    panels = AccessPanelReader(
        controls=access.controls, users=UserRepository(db, clock), teams=TeamRepository(db, clock)
    )
    # Only the stack operations use Compose, and without it they fail at their first call.
    stack_parts = _stack_parts(
        db,
        runtime,
        engine,
        cast(ComposeRuntime, engine.compose),
        registries,
        access,
        inspector,
        activity,
        self_identity,
    )
    return SwarmServices(
        queries=SwarmQueryService(authoriser=access.authoriser, inspector=inspector, poke=poke),
        nodes=NodeService(
            database=database,
            authoriser=access.authoriser,
            inspector=inspector,
            locks=engine.locks,
            activity=activity,
            clock=clock,
            poke=poke,
            self_identity=self_identity,
        ),
        services=SwarmServiceQueries(
            authoriser=access.authoriser,
            inspector=inspector,
            pull=credentials,
            controls_read=panels,
            poke=poke,
        ),
        service_actions=SwarmServiceActions(
            db=db,
            database=database,
            authoriser=access.authoriser,
            inspector=inspector,
            locks=engine.locks,
            controls=access.controls,
            pull=credentials,
            bindings=bindings,
            activity=activity,
            clock=clock,
            poke=poke,
            bindings_factory=(
                None
                if registries is None
                else lambda session, recorder: build_image_bindings(
                    session, runtime, access.authoriser, hosts, recorder
                )
            ),
            self_identity=self_identity,
        ),
        logs=ServiceLogService(
            authoriser=access.authoriser,
            inspector=inspector,
            slots=engine.stream_slots,
            poke=poke,
            instance_name=engine.instance_name,
        ),
        configs=SwarmConfigQueries(
            authoriser=access.authoriser,
            inspector=inspector,
            hosts=hosts,
            locks=engine.locks,
            controls_read=panels,
            poke=poke,
        ),
        config_actions=SwarmConfigActions(
            db=db,
            database=database,
            authoriser=access.authoriser,
            inspector=inspector,
            hosts=hosts,
            locks=engine.locks,
            controls=access.controls,
            activity=activity,
            clock=clock,
            poke=poke,
        ),
        secrets=SwarmSecretQueries(
            authoriser=access.authoriser,
            inspector=inspector,
            hosts=hosts,
            locks=engine.locks,
            controls_read=panels,
            poke=poke,
        ),
        secret_actions=SwarmSecretActions(
            db=db,
            database=database,
            authoriser=access.authoriser,
            inspector=inspector,
            hosts=hosts,
            locks=engine.locks,
            controls=access.controls,
            activity=activity,
            clock=clock,
            poke=poke,
        ),
        stacks=SwarmStackQueries(
            authoriser=access.authoriser,
            inspector=inspector,
            hosts=hosts,
            controls_read=panels,
            stacks=StackListRepository(db),
            rows=stack_parts.stacks,
            planner=stack_parts.planner,
            locks=engine.locks,
            dismissals=StackDismissalRepository(db, clock),
            poke=poke,
        ),
        stack_logs=stack_parts.logs,
        access=access,
        bindings=bindings,
    )


@asynccontextmanager
async def open_swarm_services(
    database: Database,
    runtime: AuthRuntime,
    engine: EngineRuntime,
    registries: RegistryRuntime | None,
    self_identity: SelfIdentitySource | None = None,
) -> AsyncIterator[SwarmServices]:
    """Open the Swarm services on a database session of their own, closed with the block.

    For work that outlives a request: a log stream authorises on one of these and holds no
    session while it runs.
    """
    async with database.session() as db:
        activity = ActivityRecorder(db, runtime.clock)
        inspector = EngineObjectInspector(
            build_host_clients(db, runtime, engine), poke=engine.refresher.poke
        )
        access = build_access_services(db, runtime, inspector, activity)
        yield build_swarm_services(
            db, database, runtime, engine, registries, access, inspector, activity, self_identity
        )


def app_swarm_services(app: FastAPI) -> AbstractAsyncContextManager[SwarmServices]:
    """Open the Swarm services on a fresh session of the running application's database."""
    engine: EngineRuntime | None = getattr(app.state, "engine", None)
    if engine is None:
        raise RuntimeError("the Engine runtime has not been started")
    return open_swarm_services(
        app.state.database,
        app.state.auth,
        engine,
        getattr(app.state, "registries", None),
        source_of(app),
    )


async def get_swarm_services(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> SwarmServices:
    """Provide the Swarm services bound to this request's session and inspector."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    database: Database = request.app.state.database
    return build_swarm_services(
        db,
        database,
        runtime,
        engine,
        getattr(request.app.state, "registries", None),
        access,
        inspector,
        activity,
        source_of(request.app),
    )


Swarm = Annotated[SwarmServices, Depends(get_swarm_services)]


@dataclass(frozen=True, slots=True)
class JobServices:
    """The job and rule services for one request."""

    rules: AutomationRuleService
    jobs: JobService


def get_scheduler_runtime(request: Request) -> SchedulerRuntime:
    """Return the scheduler runtime the application started with."""
    scheduler: SchedulerRuntime | None = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise RuntimeError("the scheduler has not been started")
    return scheduler


def build_job_services(
    db: Session,
    runtime: AuthRuntime,
    access: AccessServices,
    scheduler: SchedulerRuntime,
    activity: ActivityRecorder,
    inspector: EngineObjectInspector | None = None,
) -> JobServices:
    """Wire the job and rule services around the request's authoriser and recorder.

    Without an inspector the rule detail cannot read the host, so it lists no held containers.
    """
    clock = runtime.clock
    rules = AutomationRuleRepository(db, clock)
    jobs = ScheduledJobRepository(db, clock)
    settings = SettingRepository(db, clock)
    job_service = JobService(
        db=db,
        jobs=jobs,
        runs=JobRunRepository(db, clock),
        rules=rules,
        settings_repo=settings,
        subsystems=scheduler.handlers,
        scheduler=scheduler.scheduler,
        activity=activity,
        clock=clock,
    )
    return JobServices(
        rules=AutomationRuleService(
            db=db,
            rules=rules,
            jobs=jobs,
            settings_repo=settings,
            authoriser=access.authoriser,
            subsystems=scheduler.handlers,
            activity=activity,
            clock=clock,
            wake=scheduler.scheduler.wake,
            job_service=job_service,
            containers=(
                None
                if inspector is None
                else ContainerService(
                    authoriser=access.authoriser,
                    inspector=inspector,
                    controls=access.controls,
                    effective=access.effective,
                    users=UserRepository(db, clock),
                    teams=TeamRepository(db, clock),
                )
            ),
        ),
        jobs=job_service,
    )


async def get_job_services(
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    scheduler: Annotated[SchedulerRuntime, Depends(get_scheduler_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
) -> JobServices:
    """Provide the job and rule services bound to this request's session."""
    return build_job_services(
        db,
        runtime,
        access,
        scheduler,
        activity,
        inspector if isinstance(inspector, EngineObjectInspector) else None,
    )


Jobs = Annotated[JobServices, Depends(get_job_services)]


def build_webhook_service(
    db: Session,
    runtime: AuthRuntime,
    scheduler: SchedulerRuntime | None,
    *,
    limiter: WebhookCallLimiter,
    tls_enabled: bool,
    authorise_write: AuthoriseWrite | None = None,
) -> WebhookService:
    """Wire the webhook service; rotating needs authorise_write and is refused without it."""
    clock = runtime.clock
    return WebhookService(
        db=db,
        webhooks=WebhookRepository(db, clock),
        rules=AutomationRuleRepository(db, clock),
        jobs=ScheduledJobRepository(db, clock),
        users=UserRepository(db, clock),
        settings_repo=SettingRepository(db, clock),
        subsystems=None if scheduler is None else scheduler.handlers,
        limiter=limiter,
        activity=ActivityRecorder(db, clock),
        clock=clock,
        poke=(lambda: None) if scheduler is None else scheduler.scheduler.poke,
        tls_enabled=tls_enabled,
        authorise_write=authorise_write,
    )


def build_activity_log_service(
    db: Session, runtime: AuthRuntime, settings: Settings
) -> ActivityLogService:
    """Wire the activity log service around a session and the retention setting."""
    return ActivityLogService(
        db=db, clock=runtime.clock, retention_days=settings.activity_retention_days
    )


async def get_activity_log_service(
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ActivityLogService:
    """Provide the activity log service bound to this request's session."""
    return build_activity_log_service(db, runtime, settings)


ActivityLog = Annotated[ActivityLogService, Depends(get_activity_log_service)]


async def get_activity_targets(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
) -> ActivityTargets:
    """Provide the target lookup of the event page, reading hosts only once the Engine runs."""
    engine: EngineRuntime | None = getattr(request.app.state, "engine", None)
    inspector = (
        None
        if engine is None
        else EngineObjectInspector(
            build_host_clients(db, runtime, engine), poke=engine.refresher.poke
        )
    )
    return ActivityTargets(db, inspector)


TargetLookup = Annotated[ActivityTargets, Depends(get_activity_targets)]


@asynccontextmanager
async def open_activity_log_service(
    database: Database, runtime: AuthRuntime, settings: Settings
) -> AsyncIterator[ActivityLogService]:
    """Open the activity log service on its own session, for a response that outlives a request."""
    async with database.session() as db:
        yield build_activity_log_service(db, runtime, settings)


def app_activity_log_service(app: FastAPI) -> AbstractAsyncContextManager[ActivityLogService]:
    """Open the activity log service on a fresh session of the running application's database."""
    return open_activity_log_service(app.state.database, app.state.auth, app.state.settings)


def build_object_storage_service(
    db: Session, runtime: AuthRuntime, vault: Vault, activity: ActivityRecorder
) -> ObjectStorageTargetService:
    """Wire the object storage target service around a session and the recorder."""
    return ObjectStorageTargetService(db, vault, runtime.clock, activity)


async def get_object_storage_service(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> ObjectStorageTargetService:
    """Provide the object storage target service bound to this request's session."""
    return build_object_storage_service(db, runtime, request.app.state.vault, activity)


TargetAdmin = Annotated[ObjectStorageTargetService, Depends(get_object_storage_service)]


def build_template_catalogue_service(
    db: Session,
    runtime: AuthRuntime,
    activity: ActivityRecorder,
    *,
    fetcher: DocumentFetcher | None = None,
) -> TemplateCatalogueService:
    """Wire the template catalogue service around a session and the recorder."""
    return TemplateCatalogueService(db, runtime.clock, activity, fetcher=fetcher)


async def get_template_catalogue_service(
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> TemplateCatalogueService:
    """Provide the template catalogue service bound to this request's session."""
    return build_template_catalogue_service(db, runtime, activity)


CatalogueAdmin = Annotated[TemplateCatalogueService, Depends(get_template_catalogue_service)]


async def get_template_reader(db: Annotated[Session, Depends(get_session)]) -> TemplateReader:
    """Provide the template reader bound to this request's session."""
    return TemplateReader(db)


TemplateReads = Annotated[TemplateReader, Depends(get_template_reader)]


async def get_template_targets(
    environments: Annotated[EnvironmentServices, Depends(get_environment_services)],
    access: Annotated[AccessServices, Depends(get_access_services)],
) -> TemplateTargetService:
    """Provide the lookup of environments the viewer may create from a template in."""
    return TemplateTargetService(environments.environments, access.authoriser)


TemplateTargets = Annotated[TemplateTargetService, Depends(get_template_targets)]


def build_notification_service(
    db: Session, runtime: AuthRuntime, vault: Vault, activity: ActivityRecorder
) -> NotificationService:
    """Wire the notification channel service around a session and the recorder."""
    return NotificationService(db, vault, runtime.clock, activity)


async def get_notification_service(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> NotificationService:
    """Provide the notification channel service bound to this request's session."""
    return build_notification_service(db, runtime, request.app.state.vault, activity)


NotificationAdmin = Annotated[NotificationService, Depends(get_notification_service)]


def build_backup_service(
    db: Session,
    runtime: AuthRuntime,
    vault: Vault,
    database: Database,
    production: BackupProduction,
    activity: ActivityRecorder,
) -> BackupService:
    """Wire the backup service around a session, the database and the production lock."""
    return BackupService(db, vault, database, production, runtime.clock, activity)


async def get_backup_service(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> BackupService:
    """Provide the backup service bound to this request's session."""
    state = request.app.state
    return build_backup_service(
        db, runtime, state.vault, state.database, state.backup_production, activity
    )


Backups = Annotated[BackupService, Depends(get_backup_service)]


def get_restore_coordinator(request: Request) -> RestoreCoordinator:
    """Return the restore coordinator the application was built with."""
    coordinator: RestoreCoordinator = request.app.state.restore
    return coordinator


Restores = Annotated[RestoreCoordinator, Depends(get_restore_coordinator)]


def get_restore_gate(request: Request) -> RestoreGate:
    """Return the offline gate the application was built with."""
    gate: RestoreGate = request.app.state.restore_gate
    return gate


RestoreGates = Annotated[RestoreGate, Depends(get_restore_gate)]


async def get_name_suggestions(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    auth: Annotated[AuthServices, Depends(get_auth_services)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> NameSuggestions:
    """Provide name suggestions over this request's list services, built for the kind asked."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    hosts: EngineObjectInspector = inspector
    database: Database = request.app.state.database
    registries: RegistryRuntime | None = getattr(request.app.state, "registries", None)

    def images() -> ImageQueryService:
        if registries is None:
            raise RuntimeError("the registry runtime has not been started")
        return build_image_services(
            db, database, runtime, engine, registries, access, hosts, activity
        ).queries

    def swarm() -> SwarmServices:
        return build_swarm_services(
            db, database, runtime, engine, registries, access, hosts, activity
        )

    def stacks() -> StackQueryService:
        compose = cast(ComposeRuntime, engine.compose)
        return _stack_parts(
            db, runtime, engine, compose, registries, access, hosts, activity
        ).queries

    return NameSuggestions(
        SuggestionSources(
            containers=lambda: (
                build_container_services(
                    db, database, runtime, engine, access, hosts, activity
                ).queries
            ),
            images=images,
            volumes=lambda: (
                build_volume_services(
                    db, database, runtime, engine, access, hosts, activity
                ).queries
            ),
            networks=lambda: (
                build_network_services(
                    db, database, runtime, engine, access, hosts, activity
                ).queries
            ),
            stacks=stacks,
            services=lambda: swarm().services,
            configs=lambda: swarm().configs,
            secrets=lambda: swarm().secrets,
            users=lambda: auth.user_admin,
            teams=lambda: access.teams,
        )
    )


Suggestions = Annotated[NameSuggestions, Depends(get_name_suggestions)]


async def get_environment_overview(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> EnvironmentOverview:
    """Provide the environment page's figures over this request's container and stack queries."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    database: Database = request.app.state.database
    registries: RegistryRuntime | None = getattr(request.app.state, "registries", None)
    compose = cast(ComposeRuntime, engine.compose)
    return EnvironmentOverview(
        authoriser=access.authoriser,
        hosts=inspector.hosts,
        containers=build_container_services(
            db, database, runtime, engine, access, inspector, activity
        ).queries,
        stacks=_stack_parts(
            db, runtime, engine, compose, registries, access, inspector, activity
        ).queries,
        images=(
            None
            if registries is None
            else build_image_services(
                db, database, runtime, engine, registries, access, inspector, activity
            ).queries
        ),
        db=db,
        clock=runtime.clock,
        poke=engine.refresher.poke,
    )


Overview = Annotated[EnvironmentOverview, Depends(get_environment_overview)]
