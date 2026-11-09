"""Stack reads: host projects, listings, details and the confirmations that precede a change."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final, Literal

from app.auth.errors import (
    PermissionDenied,
    ProductError,
    ResourceNotFound,
    SelfProtected,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.compose_cli.cli import ComposeCli, ComposeProject
from app.compose_cli.errors import (
    ComposeCommandFailed,
    ComposeError,
    ComposeUnavailable,
    ComposeVersionUnsupported,
)
from app.compose_cli.parsers import ResolvedProject
from app.compose_cli.target import ComposeTarget
from app.db.repositories.environment import Environment
from app.db.repositories.environment_tls import EnvironmentTlsRepository
from app.db.repositories.pagination import CursorPosition, decode_cursor, encode_cursor
from app.db.repositories.registry_credential import SecretUnreadableError
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.stack import (
    StackKind,
    StackListRepository,
    StackRepository,
    StackSummary,
    StoredStack,
    VariableWrite,
)
from app.db.repositories.stack_build_context import ContextWrite
from app.db.repositories.stack_dismissal import StackDismissalRepository, StoredDismissal
from app.db.repositories.team import TeamRepository
from app.db.repositories.user import UserRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.models import ContainerSummary, Network, Volume
from app.self_identity import SelfIdentitySource, no_identity
from app.services.access import (
    COMPOSE_PROJECT_LABEL,
    SWARM_NAMESPACE_LABEL,
    ResourceKey,
    canonical_stack_key,
)
from app.services.acting import Acting, is_administrator
from app.services.authorisation import Authoriser
from app.services.compose_setup import ComposeRuntime, ComposeStatus, build_target
from app.services.container_actions import BusyView
from app.services.containers import AccessPanel, build_access_panel, display_name, short_id
from app.services.grantees import GranteeDirectory
from app.services.guards import require_administrator
from app.services.host_objects import (
    busy_view,
    check_limit,
    permitted_container_ids,
)
from app.services.image_bindings import CredentialOffer, ImageBindingService
from app.services.list_order import ListOrder, Ordering, text_key
from app.services.object_inspector import (
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
)
from app.services.object_locks import ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.pull_credentials import CredentialChoice
from app.services.resource_controls import ResourceControlService
from app.services.stack_build_plan import (
    BuildInputs,
    StoredContexts,
    plan_builds,
    services_with_build,
)
from app.services.stack_builds import BuildAdmission
from app.services.stack_collisions import NOT_CHECKED, CollisionChecker
from app.services.stack_common import (
    changed_since_deploy,
    concurrent,
    declared_service_images,
    read_labelled_containers,
    read_labelled_names,
    read_labelled_networks,
    read_labelled_volumes,
    swarm_namespaces_apply,
)
from app.services.stack_context_source import HeldContexts
from app.services.stack_context_views import context_views
from app.services.stack_deploy import (
    BUILD_ADMINISTRATOR_ONLY,
    CONFIG_SLOT_WAIT_SECONDS,
    ComposeNotReady,
    StackError,
    StackNameInUse,
    StackRejected,
    StackValidator,
    Validated,
    ValidationRequest,
    compose_to_stack_error,
    file_secret_values,
    hide_keys,
    in_project,
    make_scrubber,
    require_build_rights,
    secret_env_keys,
)
from app.services.stack_dismissal_views import (
    DISMISSIBLE_NAME,
    compose_members,
    dismissal_view,
    leftover_objects,
    leftover_volumes,
    members_digest,
)
from app.services.stack_document import (
    FILE_NAME,
    STACK_NAME,
    VARIABLE_NAME,
    CompanionFile,
    ComposeDocumentRejected,
    ServiceBuildCheck,
    VariableInput,
    check_variables,
    is_reserved_variable,
    precheck,
)
from app.services.stack_file_views import file_views
from app.services.stack_restrictions import scan, stack_option_label
from app.services.stack_views import (
    BuildSummary,
    ContainerEntry,
    CreateContext,
    FileForm,
    FileRemoveConfirmation,
    FilesPanel,
    FileView,
    ForgetConfirmation,
    ManagedStackName,
    RedeployConfirmation,
    RemoveConfirmation,
    RemoveVolume,
    RestrictedOption,
    ServiceView,
    StackDetail,
    StackEdit,
    StackPage,
    StackRow,
    UnmanagedProject,
    ValidationReport,
    VariableSet,
    VolumePlan,
    checked_name,
    containers_digest,
    networks_digest,
    variable_views,
    volumes_digest,
)
from app.services.swarm_stacks import NAMESPACE_ROUTE, namespace_keys

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_NAME_CHARS",
    "SERVICE_LABEL",
    "VOLUME_REASON_CONTROLLED",
    "VOLUME_REASON_FOREIGN",
    "VOLUME_REASON_IN_USE",
    "VOLUME_REASON_SELF",
    "RestartConfirmation",
    "StackQueryService",
    "host_projects",
    "labelled_networks",
    "labelled_volumes",
    "read_project_containers",
    "stack_key",
    "validation_request",
]

SERVICE_LABEL: Final = "com.docker.compose.service"

DEFAULT_PAGE_SIZE: Final = 25

LIST_ORDER: Final = ListOrder[StackSummary](
    default="name",
    keys={"name": text_key(lambda s: s.name)},
    name=lambda s: s.name,
    row_id=lambda s: s.id,
    entry_id=lambda s: str(s.id),
)

MAX_SHOWN_CONTAINERS: Final = 200

MAX_NAME_CHARS: Final = 128

MAX_UNMANAGED: Final = 200

MAX_UNMANAGED_SERVICES: Final = 200

VOLUME_REASON_FOREIGN: Final = "It does not belong to this stack."

VOLUME_REASON_CONTROLLED: Final = "It has its own access control."

VOLUME_REASON_IN_USE: Final = "A container outside this stack uses it."

VOLUME_REASON_SELF: Final = "The container dockmin runs in has it mounted."

type Level = Literal["read", "write"]
type _Listing = Literal["all", "active", "dismissed"]


@dataclass(frozen=True, slots=True)
class RestartConfirmation:
    """What a restart names before it runs; `expected_containers` binds the POST to it.

    `services` and `containers` are those the viewer may see among the running services;
    `hidden_containers` counts the rest and `unwritable_containers` those the viewer may not
    change, which a standard user's restart is refused for. No `services` means nothing runs.
    """

    environment_id: int
    environment_name: str
    stack: str
    services: tuple[str, ...]
    containers: tuple[ContainerEntry, ...]
    hidden_containers: int
    more_containers: int
    expected_containers: str
    compose: ComposeStatus
    unwritable_containers: int = 0


async def read_project_containers(client: EngineClient) -> list[ContainerSummary]:
    """Read every container that carries a Compose project label, in any state."""
    return await read_labelled_containers(client, COMPOSE_PROJECT_LABEL)


async def labelled_networks(client: EngineClient) -> list[Network]:
    return await read_labelled_networks(client, COMPOSE_PROJECT_LABEL)


async def labelled_volumes(client: EngineClient) -> list[Volume]:
    return await read_labelled_volumes(client, COMPOSE_PROJECT_LABEL)


async def host_projects(client: EngineClient, *, namespaces: bool = True) -> frozenset[str]:
    """Return the case-folded stack names any container, network or volume carries.

    Compose project labels count on all three. With `namespaces`, the Swarm namespace label
    counts on containers and volumes too, because either label names a stack that owns the object.
    """
    return await read_labelled_names(
        client,
        [COMPOSE_PROJECT_LABEL, SWARM_NAMESPACE_LABEL] if namespaces else [COMPOSE_PROJECT_LABEL],
        network_labels=[COMPOSE_PROJECT_LABEL],
    )


def _row_of(item: StackSummary | StoredStack) -> StackRow:
    return StackRow(
        environment_id=item.environment_id,
        name=item.name,
        origin=item.origin,
        status=item.status,
        last_operation=item.last_operation,
        last_outcome=item.last_outcome,
        last_operation_at=item.last_operation_at,
        last_deployed_at=item.last_deployed_at,
        changed_since_deploy=changed_since_deploy(
            item.compose_file_digest,
            item.deployed_file_digest,
            item.inputs_digest,
            item.deployed_inputs_digest,
        ),
    )


def _entry_of(container: ContainerSummary) -> ContainerEntry:
    return ContainerEntry(
        id=container.id,
        short_id=short_id(container.id),
        name=display_name(container.names, container.id)[:MAX_NAME_CHARS],
        state=container.state,
        status=container.status[:MAX_NAME_CHARS],
        image=container.image[:MAX_NAME_CHARS],
        service=(container.labels.get(SERVICE_LABEL) or None),
    )


def _project_key(labels: Mapping[str, str]) -> str | None:
    value = labels.get(COMPOSE_PROJECT_LABEL)
    return canonical_stack_key(value) if value else None


def _by_project[T: (Network, Volume)](
    objects: Sequence[T], managed: set[str]
) -> dict[str, list[T]]:
    grouped: dict[str, list[T]] = {}
    for item in objects:
        key = _project_key(item.labels)
        if key is not None and key not in managed:
            grouped.setdefault(key, []).append(item)
    return grouped


def _unmanaged_project(
    key: str,
    members: Sequence[ContainerSummary],
    networks: Sequence[Network],
    volumes: Sequence[Volume],
    stored: StoredDismissal | None,
) -> UnmanagedProject:
    names = sorted(display_name(c.names, c.id)[:MAX_NAME_CHARS] for c in members)
    held = compose_members(members, volumes, networks)
    return UnmanagedProject(
        name=key,
        containers=len(members),
        running=sum(1 for c in members if c.state == "running"),
        services=tuple(
            sorted({s[:MAX_NAME_CHARS] for c in members if (s := c.labels.get(SERVICE_LABEL))})
        )[:MAX_UNMANAGED_SERVICES],
        adoptable=STACK_NAME.fullmatch(key) is not None,
        container_names=tuple(names[:MAX_SHOWN_CONTAINERS]),
        networks=len(networks),
        volumes=len(volumes),
        leftovers_only=not members,
        leftover_volumes=leftover_volumes(volumes),
        leftover_networks=leftover_objects(networks),
        objects_digest=members_digest(held.tokens),
        dismissible=DISMISSIBLE_NAME.fullmatch(key) is not None,
        dismissal=dismissal_view(stored, held),
    )


def _covered(project: UnmanagedProject) -> bool:
    return project.dismissal is not None and project.dismissal.applies


def _declared_services(text: str) -> dict[str, str | None]:
    """Return each service the file declares with its image, cut for display."""
    return {
        key[:MAX_NAME_CHARS]: image[:MAX_NAME_CHARS] if image is not None else None
        for key, image in declared_service_images(text).items()
    }


class StackQueryService:
    """Reads stacks through the authoriser and checks candidates; nothing here changes a stack."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        controls: ResourceControlService,
        control_repo: ResourceControlRepository,
        stacks: StackRepository,
        listing: StackListRepository,
        validator: StackValidator,
        users: UserRepository,
        teams: TeamRepository,
        runtime: ComposeRuntime,
        tls: EnvironmentTlsRepository,
        locks: ObjectLockRegistry,
        bindings: ImageBindingService | None,
        dismissals: StackDismissalRepository,
        poke: Callable[[int], None] | None = None,
        self_identity: SelfIdentitySource | None = None,
    ) -> None:
        self._dismissals = dismissals
        self._self_identity = self_identity or no_identity
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._controls = controls
        self._control_repo = control_repo
        self._stacks = stacks
        self._listing = listing
        self._poke = poke
        self._validator = validator
        self.grantees = GranteeDirectory(users, teams)
        self._runtime = runtime
        self._tls = tls
        self._locks = locks
        self._bindings = bindings
        self._collisions = CollisionChecker(authoriser=authoriser, inspector=inspector)

    async def ready(self) -> ComposeCli:
        """Return the Compose CLI; ComposeNotReady when Compose is missing or below the floor."""
        try:
            return await self._runtime.ready()
        except ComposeUnavailable as error:
            raise ComposeNotReady(message=str(error)) from None
        except ComposeVersionUnsupported as error:
            raise ComposeNotReady(
                installed=error.installed, required=error.required, message=str(error)
            ) from None

    def compose_status(self) -> ComposeStatus:
        """Describe Compose from what is already known; it never starts a process."""
        return self._runtime.status()

    def server_paths(self) -> tuple[str, ...]:
        """Return the directories on this server that a stack must not name as a host path.

        The data directory, with the stack files and the Compose scratch homes beneath it.
        """
        files = self._runtime.files.base
        work = self._runtime.work_base
        return tuple(dict.fromkeys(path.as_posix() for path in (work.parent, work, files)))

    def describe_busy(self, busy: ObjectBusy, viewer: Principal) -> BusyView:
        """Describe the lock holder to a viewer: names only for administrators."""
        return busy_view(busy, viewer, self._locks.now())

    async def require_environment(self, actor: Principal, environment_id: int) -> None:
        """Raise ResourceNotFound unless the caller may see the environment, and it exists."""
        await self._authoriser.require_environment(actor, environment_id)
        await self._hosts.environment(environment_id)

    async def busy(self, actor: Principal, environment_id: int, name: str) -> BusyView | None:
        """Describe who is operating on the stack now, or None; the caller needs read."""
        busy = await self.busy_error(actor, environment_id, name)
        return None if busy is None else self.describe_busy(busy, actor)

    async def busy_error(
        self, actor: Principal, environment_id: int, name: str
    ) -> ObjectBusy | None:
        """Return the refusal an action on the stack would meet now, or None; needs read."""
        await self.require_stack(actor, environment_id, name, "read")
        holder = self._locks.holder_of(stack_key(environment_id, name))
        return None if holder is None else ObjectBusy(holder)

    async def require_stack(
        self, actor: Principal, environment_id: int, name: str, level: Level = "read"
    ) -> StoredStack:
        """Return the stack the caller holds the level over.

        ResourceNotFound for a missing or hidden stack or a malformed name, alike; PermissionDenied
        for a caller who may see it but not at the level, which on a local socket is every write.
        """
        await self._authoriser.require_environment(actor, environment_id)
        await self._hosts.environment(environment_id)
        if STACK_NAME.fullmatch(name) is None:
            raise ResourceNotFound("not found")
        row = await self._stacks.get(environment_id, name)
        if row is None or row.kind != "compose":
            raise ResourceNotFound("not found")
        if not actor.is_administrator:
            await self._authoriser.require_access(
                actor, ResourceKey(environment_id, "stack", name), level
            )
        return row

    async def require_free(self, environment_id: int, name: str, client: EngineClient) -> None:
        """Raise StackNameInUse when any stack, host object or Swarm namespace has the name.

        Names compare case-folded. Containers and volumes under either the Compose project label
        or the Swarm namespace label count, so a name that only a surviving volume carries is not
        free. EnvironmentUnavailable when the host cannot be read in full, since the name is then
        not known to be free.
        """
        key = canonical_stack_key(name)
        if await self._stacks.get_folded(environment_id, name) is not None:
            raise StackNameInUse
        if key in await host_projects(client):
            raise StackNameInUse
        if await self._namespace_taken(environment_id, key, client):
            raise StackNameInUse

    async def namespace_taken(self, environment_id: int, name: str, client: EngineClient) -> bool:
        """Report whether a Swarm namespace may carry the name; True when the host cannot say.

        False without reading the host unless the daemon reports Swarm.
        """
        try:
            return await self._namespace_taken(
                environment_id, canonical_stack_key(name), client, compose_projects=False
            )
        except ProductError:
            return True

    async def _namespace_taken(
        self, environment_id: int, key: str, client: EngineClient, *, compose_projects: bool = True
    ) -> bool:
        environment = await self._hosts.environment(environment_id)
        if not swarm_namespaces_apply(environment):
            return False
        return key in await namespace_keys(
            client, environment_id, self._poke, compose_projects=compose_projects
        )

    async def require_adoptable(self, environment_id: int, name: str, client: EngineClient) -> None:
        """Raise unless a Compose project of this name is on the host and nothing else covers it.

        A stack row, or a Swarm namespace of the same folded name, covers it.
        """
        if await self._stacks.get_folded(environment_id, name) is not None:
            raise StackNameInUse
        key = canonical_stack_key(name)
        if key not in await host_projects(client, namespaces=False):
            raise StackRejected(
                {"name": "No Compose project has this name on this environment."}, "invalid"
            )
        if await self._namespace_taken(environment_id, key, client, compose_projects=False):
            raise StackNameInUse

    async def variable_set(
        self, stack: StoredStack | None, inputs: Sequence[VariableInput] | None
    ) -> VariableSet:
        """Combine the stored variables with a submission.

        With no submission the stored set is used as is. A submitted secret with no value keeps
        the stored one. StackRejected keyed by variable row when a name or value is refused, and
        keyed `variables` when a stored name is reserved or malformed.
        """
        stored_plain: dict[str, str] = {}
        stored_secret: dict[str, str] = {}
        if stack is not None:
            for variable in await self._stacks.variables(stack.id):
                if not variable.is_secret and variable.value is not None:
                    stored_plain[variable.name] = variable.value
            try:
                stored_secret = await self._stacks.reveal_secrets(stack.id)
            except SecretUnreadableError:
                raise StackRejected(
                    {"variables": "A stored secret cannot be read with this server's key."},
                    "secrets_unreadable",
                ) from None
        if inputs is None:
            if any(
                VARIABLE_NAME.fullmatch(name) is None or is_reserved_variable(name)
                for name in (*stored_plain, *stored_secret)
            ):
                raise StackRejected(
                    {
                        "variables": "A stored variable name is no longer accepted. "
                        "Edit the variables to rename or remove it."
                    },
                    "invalid",
                )
            return VariableSet(
                {**stored_plain, **stored_secret},
                frozenset(stored_secret.values()),
                None,
                len(stored_secret),
            )
        try:
            check_variables(inputs)
        except ComposeDocumentRejected as rejected:
            raise StackRejected(rejected.fields, rejected.reason) from None
        values: dict[str, str] = {}
        secret_values: set[str] = set()
        writes: list[VariableWrite] = []
        errors: dict[str, str] = {}
        for item in inputs:
            if item.remove:
                continue
            value = item.value
            if item.secret and value is None:
                value = stored_secret.get(item.name)
                if value is None:
                    errors[f"variable-{item.row}-value"] = "Enter a value for this secret."
                    continue
            values[item.name] = value or ""
            if item.secret:
                secret_values.add(value or "")
            writes.append(VariableWrite(item.name, item.secret, item.value))
        if errors:
            raise StackRejected(errors, "invalid")
        return VariableSet(
            values, frozenset(secret_values), tuple(writes), sum(w.is_secret for w in writes)
        )

    async def stored_files(self, stack: StoredStack | None) -> tuple[CompanionFile, ...]:
        """Return the stack's companion files with their content, secret ones decrypted.

        StackRejected (`secrets_unreadable`) when a secret file cannot be read with the keys
        this server holds.
        """
        if stack is None:
            return ()
        repository = self._stacks.files
        stored = await repository.files(stack.id)
        if not stored:
            return ()
        try:
            revealed = await repository.reveal_files(stack.id)
        except SecretUnreadableError:
            raise _unreadable_files() from None
        files: list[CompanionFile] = []
        for item in stored:
            content = revealed.get(item.name) if item.is_secret else item.content
            if content is None:
                raise _unreadable_files()
            files.append(CompanionFile(item.name, content, item.is_secret))
        return tuple(files)

    async def build_check(
        self, stack: StoredStack, actor: Acting | None = None
    ) -> ServiceBuildCheck | None:
        """Return the check that admits a service's `build` against the stack's contexts.

        With an `actor` the check is only for an administrator, and None is returned for anyone
        else; without one it is for reading a file already stored.
        """
        if actor is not None and not is_administrator(actor):
            return None
        names = frozenset(await StoredContexts(self._stacks.contexts, stack.id).digests())
        return BuildAdmission(names).check

    async def build_inputs(self, stack: StoredStack, *, rebuild: bool = False) -> BuildInputs:
        """Return what a validation of the stack needs to admit and plan its builds."""
        return BuildInputs(
            StoredContexts(self._stacks.contexts, stack.id),
            buildx_available=self._runtime.buildx.available,
            rebuild=rebuild,
        )

    @asynccontextmanager
    async def scratch(
        self,
        name: str,
        text: str,
        *,
        kind: StackKind,
        stack: StoredStack | None = None,
        files: Sequence[CompanionFile] | None = None,
        builds: ServiceBuildCheck | None = None,
    ) -> AsyncIterator[ComposeProject]:
        """Write the text and its companion files where a check can run them, and remove them.

        The directory is always a throwaway one, never the stack's own. Without `files` the
        stack's stored files are used. The sources are read as a `kind` document. `builds`
        admits a service's `build` section, as for `precheck`.
        """
        companions = await self.stored_files(stack) if files is None else files
        async with self._runtime.files.scratch(
            name, text, companions, kind=kind, builds=builds
        ) as project:
            yield project

    async def page(
        self,
        actor: Principal,
        environment_id: int,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        q: str | None = None,
        include_unmanaged: bool = True,
        show_dismissed: bool = False,
        sort: str | None = None,
        direction: str | None = None,
    ) -> StackPage:
        """List the stacks the caller may see, by name unless `sort` says otherwise.

        Administrators also get the unmanaged projects found on the host, unless
        `include_unmanaged` is false, which skips reading the host. A dismissal hides its
        project from that list; with `show_dismissed` the list holds only the dismissed projects.
        ValidationFailed keyed `cursor`, `limit`, `sort` or `dir` for a bad page request.
        """
        await self._authoriser.require_environment(actor, environment_id)
        environment = await self._hosts.environment(environment_id)
        limit = check_limit(limit)
        ordering = Ordering.resolve(LIST_ORDER, sort, direction)
        position = ordering.position(cursor)
        needle = (q or "").strip().casefold()[:MAX_NAME_CHARS] or None
        visible = None
        if not actor.is_administrator:
            visibility = await self._authoriser.resolve_visibility(actor, environment_id, "stack")
            visible = visibility.sql_filter("s.name", "s.environment_id")
        page = await self._listing.page(
            environment_id,
            limit=limit,
            cursor=None
            if position is None
            else encode_cursor(CursorPosition(str(position.value), position.row_id)),
            name_contains=needle,
            visible=visible,
            kind="compose",
            descending=ordering.descending,
        )
        next_cursor = None
        if page.next_cursor is not None:
            last = decode_cursor(page.next_cursor)
            next_cursor = ordering.encode(
                str(last.sort_value), last.row_id, str(last.sort_value).casefold()
            )
        items = tuple(_row_of(item) for item in page.items)
        unmanaged: tuple[UnmanagedProject, ...] = ()
        dismissed_hidden = 0
        unavailable = False
        if include_unmanaged and actor.is_administrator and cursor is None and needle is None:
            try:
                unmanaged, dismissed_hidden = await self._unmanaged(
                    environment_id, "dismissed" if show_dismissed else "active"
                )
            except (EnvironmentUnavailable, ObjectTooLarge):
                unavailable = True
        return StackPage(
            environment_id=environment_id,
            environment_name=environment.name,
            items=items,
            next_cursor=next_cursor,
            unmanaged=unmanaged,
            unmanaged_unavailable=unavailable,
            can_create=await self._authoriser.may_create(actor, environment_id),
            visible_count=len(items) if cursor is None and page.next_cursor is None else None,
            compose=self.compose_status(),
            dismissed_hidden=dismissed_hidden,
            show_dismissed=show_dismissed,
        )

    async def managed_names(
        self,
        actor: Principal,
        environment_id: int,
        *,
        kind: StackKind | None = None,
        q: str | None = None,
        prefix: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> tuple[ManagedStackName, ...]:
        """List the managed stacks the caller may read, by name, from the database alone.

        `kind` restricts the list to one kind, `q` to names containing the text and `prefix` to
        names starting with it. Nothing is read from the host. ResourceNotFound when the caller
        may not see the environment.
        """
        await self._authoriser.require_environment(actor, environment_id)
        await self._hosts.environment(environment_id)
        visible = None
        if not actor.is_administrator:
            visibility = await self._authoriser.resolve_visibility(actor, environment_id, "stack")
            visible = visibility.sql_filter("s.name", "s.environment_id")
        needle = (q or "").strip().casefold()[:MAX_NAME_CHARS] or None
        start = (prefix or "").strip().casefold()[:MAX_NAME_CHARS] or None
        page = await self._listing.page(
            environment_id,
            limit=check_limit(limit),
            name_contains=needle,
            name_prefix=start,
            visible=visible,
            kind=kind,
        )
        return tuple(ManagedStackName(item.name, item.kind) for item in page.items)

    async def managed_stacks(
        self, actor: Principal, environment_id: int, names: Iterable[str]
    ) -> dict[str, tuple[str, StackKind]]:
        """Map each name that is a managed stack the caller may read to its name and kind.

        Names compare ignoring ASCII case. An environment the caller may not see maps nothing.
        """
        try:
            await self._authoriser.require_environment(actor, environment_id)
        except ResourceNotFound:
            return {}
        visible = None
        if not actor.is_administrator:
            visibility = await self._authoriser.resolve_visibility(actor, environment_id, "stack")
            visible = visibility.sql_filter("s.name", "s.environment_id")
        return await self._listing.kinds_folded(environment_id, names, visible)

    async def stack_links(
        self, actor: Principal, environment_id: int, refs: Iterable[tuple[StackKind, str]]
    ) -> dict[str, str]:
        """Map `kind:name` of each stack whose page the caller may open to the name it is opened by.

        A Compose project has a page once it is a managed stack; a Swarm namespace has one on a
        Swarm environment. Nothing is read from the host, and an environment the caller may not see
        links nothing.
        """
        wanted = {f"{kind}:{name}": (kind, name) for kind, name in refs}
        if not wanted:
            return {}
        try:
            await self._authoriser.require_environment(actor, environment_id)
            environment = await self._hosts.environment(environment_id)
        except ResourceNotFound:
            return {}
        visibility = None
        if not actor.is_administrator:
            visibility = await self._authoriser.resolve_visibility(actor, environment_id, "stack")
        links: dict[str, str] = {}
        compose = [name for kind, name in wanted.values() if kind == "compose"]
        if compose:
            visible = (
                None if visibility is None else visibility.sql_filter("s.name", "s.environment_id")
            )
            found = await self._listing.kinds_folded(environment_id, compose, visible)
            for key, (kind, name) in wanted.items():
                match = found.get(name) if kind == "compose" else None
                if match is not None and match[1] == "compose":
                    links[key] = match[0]
        if environment.type == "swarm":
            for key, (kind, name) in wanted.items():
                if kind != "swarm" or NAMESPACE_ROUTE.fullmatch(name) is None:
                    continue
                if visibility is None or visibility.permits(name, {}):
                    links[key] = name
        return links

    async def unmanaged(
        self, actor: Principal, environment_id: int
    ) -> tuple[UnmanagedProject, ...]:
        """List the Compose projects on the host that no stack covers; administrators only.

        Dismissed projects are listed too, each with its dismissal.
        """
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        await self._hosts.environment(environment_id)
        return (await self._unmanaged(environment_id, "all"))[0]

    async def undismissed_unmanaged(
        self, actor: Principal, environment_id: int
    ) -> tuple[tuple[UnmanagedProject, ...], int]:
        """List the unmanaged projects no dismissal covers, capped, and count those one covers.

        Administrators only.
        """
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        await self._hosts.environment(environment_id)
        return await self._unmanaged(environment_id, "active")

    async def adopt_context(
        self, actor: Principal, environment_id: int, project: str
    ) -> UnmanagedProject:
        """Return the unmanaged project to adopt; ResourceNotFound when the host has no such one."""
        key = canonical_stack_key(project)
        for found in await self.unmanaged(actor, environment_id):
            if found.name == key:
                return found
        raise ResourceNotFound("not found")

    async def _unmanaged(
        self, environment_id: int, listing: _Listing
    ) -> tuple[tuple[UnmanagedProject, ...], int]:
        """Return the projects of the listing, capped, and how many a dismissal covers."""
        client = await self._hosts.client(environment_id)
        names = await self._stacks.names(environment_id, kind="compose")
        managed = {canonical_stack_key(name) for name in names}
        stored = {
            held.project_key: held
            for held in await self._dismissals.dismissals(environment_id, "compose")
        }
        async with concurrent() as group:
            containers_read = group.create_task(read_project_containers(client))
            networks_read = group.create_task(labelled_networks(client))
            volumes_read = group.create_task(labelled_volumes(client))
        grouped: dict[str, list[ContainerSummary]] = {}
        for container in containers_read.result():
            key = _project_key(container.labels)
            if key is not None and key not in managed:
                grouped.setdefault(key, []).append(container)
        networks = _by_project(networks_read.result(), managed)
        volumes = _by_project(volumes_read.result(), managed)
        projects = [
            _unmanaged_project(
                key,
                grouped.get(key, []),
                networks.get(key, []),
                volumes.get(key, []),
                stored.get(key),
            )
            for key in sorted({*grouped, *networks, *volumes})
        ]
        covered = sum(1 for project in projects if _covered(project))
        match listing:
            case "active":
                projects = [project for project in projects if not _covered(project)]
            case "dismissed":
                projects = [project for project in projects if _covered(project)]
        return tuple(projects[:MAX_UNMANAGED]), covered

    async def origin_name(self, actor: Principal, environment_id: int, name: str) -> str:
        """Return the name of a Compose stack the caller may read; ResourceNotFound otherwise."""
        return (await self.require_stack(actor, environment_id, name, "read")).name

    async def detail(
        self, actor: Principal, environment_id: int, name: str, *, open_masks: bool = False
    ) -> StackDetail:
        """Return a stack's detail for a reader: file, variable names, services and containers.

        Secret values are opened only when `open_masks` is set and the reader is an
        administrator, and then only to build each secret's mask.
        """
        stack = await self.require_stack(actor, environment_id, name, "read")
        environment = await self._hosts.environment(environment_id)
        variables = await variable_views(self._stacks, stack.id, actor, open_masks=open_masks)
        declared = await asyncio.to_thread(_declared_services, stack.compose_file)
        services, hidden, available = await self._services(actor, stack, declared)
        holder = self._locks.holder_of(stack_key(environment_id, name))
        return StackDetail(
            row=_row_of(stack),
            stack_id=stack.id,
            compose_file=stack.compose_file,
            compose_file_digest=stack.compose_file_digest,
            deployed_file_digest=stack.deployed_file_digest,
            inputs_digest=stack.inputs_digest,
            deployed_inputs_digest=stack.deployed_inputs_digest,
            resolved_digest=stack.resolved_digest,
            variables=variables,
            services=services,
            hidden_containers=hidden,
            containers_available=available,
            can_write=await self._may_write(actor, environment_id, name),
            can_manage=actor.is_administrator,
            environment_name=environment.name,
            busy=None if holder is None else self.describe_busy(ObjectBusy(holder), actor),
            compose=self.compose_status(),
            files=await file_views(self._stacks.files, stack),
            contexts=(
                await context_views(self._stacks.contexts, stack) if actor.is_administrator else ()
            ),
        )

    async def files_panel(self, actor: Principal, environment_id: int, name: str) -> FilesPanel:
        """Return the Files tab: each file with its uses, plain content shown to any reader."""
        stack = await self.require_stack(actor, environment_id, name, "read")
        environment = await self._hosts.environment(environment_id)
        return FilesPanel(
            row=_row_of(stack),
            environment_name=environment.name,
            inputs_digest=stack.inputs_digest or "",
            files=await file_views(self._stacks.files, stack),
            can_write=await self._may_write(actor, environment_id, name),
            compose=self.compose_status(),
        )

    async def file_form(
        self, actor: Principal, environment_id: int, name: str, file: str | None
    ) -> FileForm:
        """Return what the add page (`file` None) or the edit page of one file starts from.

        Write holders only. ResourceNotFound for a name outside the grammar or no such file.
        """
        stack = await self.require_stack(actor, environment_id, name, "write")
        environment = await self._hosts.environment(environment_id)
        return FileForm(
            row=_row_of(stack),
            environment_name=environment.name,
            inputs_digest=stack.inputs_digest or "",
            file=None if file is None else await self._file_of(stack, file),
            compose=self.compose_status(),
        )

    async def file_remove_confirmation(
        self, actor: Principal, environment_id: int, name: str, file: str
    ) -> FileRemoveConfirmation:
        """Name the file a remove deletes and where it is still used; write holders only."""
        stack = await self.require_stack(actor, environment_id, name, "write")
        environment = await self._hosts.environment(environment_id)
        view = await self._file_of(stack, file)
        return FileRemoveConfirmation(
            environment_id=environment_id,
            environment_name=environment.name,
            stack=stack.name,
            file=view,
            expected_inputs_digest=stack.inputs_digest or "",
            in_use=bool(view.uses),
            compose=self.compose_status(),
        )

    async def _file_of(self, stack: StoredStack, file: str) -> FileView:
        if FILE_NAME.fullmatch(file) is None:
            raise ResourceNotFound("not found")
        found = next(
            (v for v in await file_views(self._stacks.files, stack) if v.name == file), None
        )
        if found is None:
            raise ResourceNotFound("not found")
        return found

    async def edit_context(self, actor: Principal, environment_id: int, name: str) -> StackEdit:
        """Return what the editor starts from; write holders only."""
        stack = await self.require_stack(actor, environment_id, name, "write")
        environment = await self._hosts.environment(environment_id)
        return StackEdit(
            row=_row_of(stack),
            compose_file=stack.compose_file,
            compose_file_digest=stack.compose_file_digest,
            variables=await variable_views(self._stacks, stack.id, actor, open_masks=False),
            environment_name=environment.name,
            compose=self.compose_status(),
        )

    async def create_context(self, actor: Principal, environment_id: int) -> CreateContext:
        """Describe the create form; ResourceNotFound or PermissionDenied for a non-creator."""
        await self._authoriser.require_create(actor, environment_id)
        environment = await self._hosts.environment(environment_id)
        status = self.compose_status()
        return CreateContext(
            environment_id,
            environment.name,
            status.ready,
            status.message,
            status.required,
            status,
        )

    async def access_panel(self, actor: Principal, environment_id: int, name: str) -> AccessPanel:
        """Return the access tab's content; administrators only."""
        stack = await self.require_stack(actor, environment_id, name, "read")
        require_administrator(actor)
        key = ResourceKey(environment_id, "stack", stack.name)
        control = await self._controls.get(actor, key)
        return await build_access_panel(self, key, stack.name, "", control)

    async def credential_offers(
        self, actor: Principal, environment_id: int, references: Sequence[str]
    ) -> tuple[CredentialOffer, ...]:
        """Describe the credential choice each reference needs; nothing without registry support."""
        if self._bindings is None:
            return ()
        return tuple(
            [
                await self._bindings.offer(actor, environment_id, reference)
                for reference in dict.fromkeys(references)
            ]
        )

    async def declared_images(
        self, actor: Principal, environment_id: int, name: str
    ) -> tuple[str, ...]:
        """List the images the stored file names outright, without running Compose.

        An image written with a variable is left out, since only Compose can resolve it. Write
        holders only.
        """
        stack = await self.require_stack(actor, environment_id, name, "write")
        declared = await asyncio.to_thread(declared_service_images, stack.compose_file)
        return tuple(
            dict.fromkeys(
                image for image in declared.values() if image is not None and "$" not in image
            )
        )

    async def credential_pickers(
        self, actor: Principal, environment_id: int, references: Sequence[str]
    ) -> tuple[CredentialOffer, ...]:
        """Describe the credential picker each reference shows; nothing without registry support."""
        if self._bindings is None:
            return ()
        return tuple(
            [
                await self._bindings.picker(actor, environment_id, reference)
                for reference in dict.fromkeys(references)
            ]
        )

    async def validate(
        self,
        actor: Principal,
        environment_id: int,
        *,
        name: str,
        compose_text: str,
        variables: Sequence[VariableInput] | None,
        existing: bool = False,
        adopt: bool = False,
        deploy: bool = False,
        pull: bool = False,
        choices: Mapping[str, CredentialChoice] | None = None,
        check_collisions: bool = False,
        files: Sequence[CompanionFile] | None = None,
        rebuild: bool = False,
        contexts: Mapping[str, ContextWrite] | None = None,
    ) -> ValidationReport:
        """Check a candidate file and variables without storing or running anything.

        `existing` validates a change to the stack called `name`, which needs write. `adopt`
        validates a file for adopting the unmanaged project called `name`, which only an
        administrator may do. Otherwise the caller needs create and the name must be free.
        `deploy` also resolves the pull credentials, so a choice that is still needed is raised
        before anything is created. `check_collisions` also compares the file's container names
        and published ports with the host: a running holder refuses, a stopped one is a warning
        on the report. `files` is the candidate companion file set; without it a stack's stored
        files are used and a new stack has none. StackRejected carries the fields, findings and
        Compose's scrubbed stderr.
        """
        checked, validated = await self.resolve_candidate(
            actor,
            environment_id,
            name=name,
            compose_text=compose_text,
            variables=variables,
            existing=existing,
            adopt=adopt,
            deploy=deploy,
            pull=pull,
            choices=choices,
            check_collisions=check_collisions,
            files=files,
            rebuild=rebuild,
            contexts=contexts,
        )
        return _report_of(checked, validated)

    async def resolve_candidate(
        self,
        actor: Principal,
        environment_id: int,
        *,
        name: str,
        compose_text: str,
        variables: Sequence[VariableInput] | None,
        existing: bool = False,
        adopt: bool = False,
        deploy: bool = False,
        pull: bool = False,
        choices: Mapping[str, CredentialChoice] | None = None,
        check_collisions: bool = False,
        files: Sequence[CompanionFile] | None = None,
        rebuild: bool = False,
        contexts: Mapping[str, ContextWrite] | None = None,
    ) -> tuple[str, Validated]:
        """Run `validate` and return the checked name with everything the validation resolved.

        `contexts` are build contexts a new stack is about to be stored with; they admit and
        plan the file's builds as stored ones would.
        """
        stack: StoredStack | None = None
        if existing and adopt:
            raise ValueError("a validation is for an existing stack or an adoption, not both")
        if existing:
            stack = await self.require_stack(actor, environment_id, name, "write")
            if deploy:
                await require_build_rights(actor, compose_text)
        elif adopt:
            await self._authoriser.require_environment(actor, environment_id)
            require_administrator(actor)
            name = checked_name(name)
        else:
            await self._authoriser.require_create(actor, environment_id)
            name = checked_name(name)
        environment = await self._hosts.environment(environment_id)
        cli = await self.ready()
        values = await self.variable_set(stack, variables)
        client = await self._hosts.client(environment_id)
        target = await build_target(environment, self._tls)
        if adopt:
            await self.require_adoptable(environment_id, name, client)
        elif stack is None:
            await self.require_free(environment_id, name, client)
        companions = await self.stored_files(stack) if files is None else tuple(files)
        async with self.scratch(
            name,
            compose_text,
            kind="compose" if stack is None else stack.kind,
            files=companions,
            builds=await self._builds_for(stack, actor, contexts),
        ) as project:
            validated = await self._validator.validate(
                actor,
                validation_request(
                    environment=environment,
                    client=client,
                    cli=cli,
                    target=target,
                    project=project,
                    text=compose_text,
                    values=values,
                    deploy=deploy,
                    pull=pull,
                    choices=choices,
                    collisions=check_collisions,
                    files=companions,
                    builds=await self._inputs_for(stack, contexts, rebuild),
                ),
            )
        return name, validated

    async def _builds_for(
        self,
        stack: StoredStack | None,
        actor: Principal,
        contexts: Mapping[str, ContextWrite] | None,
    ) -> ServiceBuildCheck | None:
        if stack is not None:
            return await self.build_check(stack, actor)
        return BuildAdmission(frozenset(contexts)).check if contexts else None

    async def _inputs_for(
        self, stack: StoredStack | None, contexts: Mapping[str, ContextWrite] | None, rebuild: bool
    ) -> BuildInputs | None:
        if stack is not None:
            return await self.build_inputs(stack, rebuild=rebuild)
        if not contexts:
            return None
        return BuildInputs(
            HeldContexts(contexts), buildx_available=self._runtime.buildx.available, rebuild=rebuild
        )

    async def redeploy_confirmation(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        *,
        check_collisions: bool = False,
    ) -> RedeployConfirmation:
        """Name the containers a redeploy may recreate and the orphans it may remove.

        With `check_collisions` the stored file is resolved with `config` and its container names
        and published ports are compared with the host. `conflicts` then holds what a running
        container already holds, as (Compose path, sentence), which the redeploy itself would
        refuse; `warnings` holds the rest, including a stored file that could not be checked.
        """
        stack = await self.require_stack(actor, environment_id, name, "write")
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        key = canonical_stack_key(name)
        project = [c for c in await read_project_containers(client) if in_project(c.labels, key)]
        self._require_not_self(environment_id, name, project)
        permitted = await permitted_container_ids(self._authoriser, actor, environment_id, project)
        unwritable = await self._unwritable(actor, environment_id, project)
        declared = await asyncio.to_thread(_declared_services, stack.compose_file)
        orphans = [c for c in project if c.labels.get(SERVICE_LABEL) not in declared]
        shown = [c for c in project if c.id in permitted]
        shown_orphans = [c for c in orphans if c.id in permitted]
        conflicts: tuple[tuple[str, str], ...] = ()
        warnings: tuple[str, ...] = ()
        building = bool(await services_with_build(stack.compose_file))
        model = (
            await self._stored_model_or_none(actor, stack) if check_collisions or building else None
        )
        if check_collisions:
            conflicts, warnings = await self._stored_collisions(actor, stack, client, model)
        builds, expected_builds, refused = await self._stored_builds(actor, stack, client, model)
        return RedeployConfirmation(
            environment_id=environment_id,
            environment_name=environment.name,
            stack=name,
            expected_file_digest=stack.compose_file_digest,
            expected_inputs_digest=stack.inputs_digest or "",
            expected_containers=containers_digest([c.id for c in project]),
            containers=tuple(_entry_of(c) for c in shown[:MAX_SHOWN_CONTAINERS]),
            hidden_containers=len(project) - len(shown),
            orphans=tuple(_entry_of(c) for c in shown_orphans[:MAX_SHOWN_CONTAINERS]),
            hidden_orphans=len(orphans) - len(shown_orphans),
            changed_since_deploy=_row_of(stack).changed_since_deploy,
            compose=self.compose_status(),
            unwritable_containers=unwritable,
            more_containers=max(len(shown) - MAX_SHOWN_CONTAINERS, 0),
            more_orphans=max(len(shown_orphans) - MAX_SHOWN_CONTAINERS, 0),
            conflicts=(*conflicts, *refused),
            warnings=warnings,
            builds=builds,
            expected_builds=expected_builds,
        )

    async def restart_confirmation(
        self, actor: Principal, environment_id: int, name: str
    ) -> RestartConfirmation:
        """Name the running services a restart stops and starts, and their containers."""
        stack = await self.require_stack(actor, environment_id, name, "write")
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        key = canonical_stack_key(name)
        project = [c for c in await read_project_containers(client) if in_project(c.labels, key)]
        self._require_not_self(environment_id, name, project)
        permitted = await permitted_container_ids(self._authoriser, actor, environment_id, project)
        unwritable = await self._unwritable(actor, environment_id, project)
        running = {c.labels.get(SERVICE_LABEL) for c in project if c.state == "running"}
        affected = [c for c in project if c.labels.get(SERVICE_LABEL) in running]
        shown = [c for c in affected if c.id in permitted]
        return RestartConfirmation(
            environment_id=environment_id,
            environment_name=environment.name,
            stack=stack.name,
            services=tuple(
                sorted({service for c in shown if (service := c.labels.get(SERVICE_LABEL))})
            ),
            containers=tuple(_entry_of(c) for c in shown[:MAX_SHOWN_CONTAINERS]),
            hidden_containers=len(affected) - len(shown),
            more_containers=max(len(shown) - MAX_SHOWN_CONTAINERS, 0),
            expected_containers=containers_digest([c.id for c in project]),
            compose=self.compose_status(),
            unwritable_containers=unwritable,
        )

    async def _stored_model_or_none(
        self, actor: Principal, stack: StoredStack
    ) -> "StoredModel | None":
        try:
            return await self._stored_model(actor, stack)
        except (StackError, ValidationFailed):
            return None

    async def _stored_collisions(
        self,
        actor: Principal,
        stack: StoredStack,
        client: EngineClient,
        model: "StoredModel | None",
    ) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
        if model is None:
            return (), (NOT_CHECKED,)
        report = await self._collisions.compose(
            actor,
            stack.environment_id,
            client,
            project=stack.name,
            services=model.resolved.services,
            scrubber=make_scrubber(model.secrets),
        )
        return tuple(report.fields.items()), report.warnings

    async def _stored_builds(
        self,
        actor: Principal,
        stack: StoredStack,
        client: EngineClient,
        model: "StoredModel | None",
    ) -> tuple[tuple[BuildSummary, ...], str, tuple[tuple[str, str], ...]]:
        """Name the services the stored file builds, with the digest that binds a redeploy to them.

        The third item holds what stops a redeploy, by Compose path: a build a standard user may
        not deploy, a file that could not be resolved, or a plan that is refused.
        """
        named = await services_with_build(stack.compose_file)
        if not named:
            return (), "", ()
        where = f"services.{named[0]}.build"
        if not actor.is_administrator:
            return (), "", ((where, BUILD_ADMINISTRATOR_ONLY),)
        if model is None:
            return (), "", ((where, "The stored file could not be resolved to name its builds."),)
        contexts = StoredContexts(self._stacks.contexts, stack.id)
        planned = await plan_builds(
            model.resolved.services,
            project=stack.name,
            admission=BuildAdmission(frozenset(await contexts.digests()), model.directory),
            contexts=contexts,
            client=client,
            rebuild=False,
            allow_build=True,
        )
        if planned.refusals:
            return (), "", tuple(planned.refusals.items())
        summaries = tuple(
            BuildSummary(plan.service, plan.tag, plan.context, plan.bases, plan.reuse)
            for plan in planned.plans
        )
        return summaries, planned.builds_digest, ()

    async def remove_confirmation(
        self, actor: Principal, environment_id: int, name: str, *, volumes: bool = False
    ) -> RemoveConfirmation:
        """Name what a remove takes away; the volumes page also checks each volume."""
        stack = await self.require_stack(actor, environment_id, name, "write")
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        key = canonical_stack_key(name)
        project = [c for c in await read_project_containers(client) if in_project(c.labels, key)]
        self._require_not_self(environment_id, name, project)
        permitted = await permitted_container_ids(self._authoriser, actor, environment_id, project)
        unwritable = await self._unwritable(actor, environment_id, project)
        declared = await asyncio.to_thread(_declared_services, stack.compose_file)
        members = [c for c in project if c.labels.get(SERVICE_LABEL) in declared]
        orphans = [c for c in project if c.labels.get(SERVICE_LABEL) not in declared]
        shown = [c for c in members if c.id in permitted]
        shown_orphans = [c for c in orphans if c.id in permitted]
        project_networks = [n for n in await labelled_networks(client) if in_project(n.labels, key)]
        networks = tuple(sorted(n.name for n in project_networks))
        plan = VolumePlan((), (), volumes_digest(()), 0)
        if volumes:
            plan = await self.plan_volumes(actor, stack, client=client)
        return RemoveConfirmation(
            environment_id=environment_id,
            environment_name=environment.name,
            stack=name,
            containers=tuple(_entry_of(c) for c in shown[:MAX_SHOWN_CONTAINERS]),
            hidden_containers=len(members) - len(shown),
            networks=networks,
            volumes=plan.volumes,
            expected_volumes=plan.digest,
            cannot_change=plan.cannot_change,
            stack_id=stack.id,
            expected_containers=containers_digest([c.id for c in project]),
            expected_networks=networks_digest([n.id for n in project_networks]),
            compose=self.compose_status(),
            unwritable_containers=unwritable,
            more_containers=max(len(shown) - MAX_SHOWN_CONTAINERS, 0),
            orphans=tuple(_entry_of(c) for c in shown_orphans[:MAX_SHOWN_CONTAINERS]),
            hidden_orphans=len(orphans) - len(shown_orphans),
            more_orphans=max(len(shown_orphans) - MAX_SHOWN_CONTAINERS, 0),
        )

    async def forget_confirmation(
        self, actor: Principal, environment_id: int, name: str
    ) -> ForgetConfirmation:
        """Describe what forgetting leaves behind; administrators only."""
        stack = await self.require_stack(actor, environment_id, name, "write")
        require_administrator(actor)
        environment = await self._hosts.environment(environment_id)
        key = canonical_stack_key(name)
        try:
            client = await self._hosts.client(environment_id)
            count = sum(
                1 for c in await read_project_containers(client) if in_project(c.labels, key)
            )
        except (EnvironmentUnavailable, ObjectTooLarge):
            count = 0
        return ForgetConfirmation(
            environment_id, environment.name, stack.name, count, stack.id, self.compose_status()
        )

    def _require_not_self(
        self, environment_id: int, name: str, project: Sequence[ContainerSummary]
    ) -> None:
        identity = self._self_identity()
        if identity.protects_stack(environment_id, name) or any(
            identity.protects_container(container.id) for container in project
        ):
            raise SelfProtected.stack(name)

    async def _unwritable(
        self, actor: Principal, environment_id: int, containers: Sequence[ContainerSummary]
    ) -> int:
        if actor.is_administrator:
            return 0
        writable = await permitted_container_ids(
            self._authoriser, actor, environment_id, containers, "write"
        )
        return sum(1 for c in containers if c.id not in writable)

    async def plan_volumes(
        self, actor: Principal, stack: StoredStack, *, client: EngineClient
    ) -> "VolumePlan":
        """Classify the project's volumes for a remove with volumes.

        A volume is deleted only when it exists, carries this project's label and has no control
        of its own; a standard user also needs write on it. Runs `config` against the stored file.
        """
        key = canonical_stack_key(stack.name)
        resolved = (await self._stored_model(actor, stack)).resolved
        entries: list[RemoveVolume] = []
        for volume_name in scan(resolved, default_runtime=None).project_volumes:
            try:
                volume = await self._inspector.volume(stack.environment_id, volume_name)
            except ResourceNotFound:
                continue
            except ObjectTooLarge:
                entries.append(RemoveVolume(volume_name, "", False, False, VOLUME_REASON_FOREIGN))
                continue
            entries.append(await self._classify(actor, stack, volume, key))
        entries.sort(key=lambda entry: entry.name)
        names = tuple(entry.name for entry in entries if entry.delete)
        return VolumePlan(
            tuple(entries),
            names,
            volumes_digest(names),
            sum(1 for entry in entries if entry.delete and not entry.writable),
        )

    async def _stored_model(self, actor: Principal, stack: StoredStack) -> "StoredModel":
        """Resolve the stored file and variables with `config`; return it with the secret values."""
        cli = await self.ready()
        environment = await self._hosts.environment(stack.environment_id)
        values = await self.variable_set(stack, None)
        target = await build_target(environment, self._tls)
        companions = await self.stored_files(stack)
        builds = await self.build_check(stack)
        try:
            uses = await asyncio.to_thread(
                precheck, stack.compose_file, kind=stack.kind, files=companions, builds=builds
            )
        except ComposeDocumentRejected as rejected:
            raise StackRejected(rejected.fields, rejected.reason, lines=rejected.lines) from None
        secrets = values.secrets | file_secret_values(companions, uses)
        async with self.scratch(
            stack.name, stack.compose_file, kind=stack.kind, files=companions, builds=builds
        ) as project:
            scrubber = make_scrubber(secrets)
            try:
                async with cli.invocation(project, target, values.values, scrubber) as run:
                    resolved = await run.config(slot_wait=CONFIG_SLOT_WAIT_SECONDS)
                return StoredModel(
                    hide_keys(resolved, secret_env_keys(companions, uses)),
                    secrets,
                    project.directory.as_posix(),
                )
            except ComposeCommandFailed as error:
                raise StackRejected(
                    {"compose_file": "Docker Compose rejected the stored file."},
                    "invalid_compose",
                    stderr=error.stderr if actor.is_administrator else "",
                ) from None
            except ComposeError as error:
                raise compose_to_stack_error(error) from None

    async def _classify(
        self, actor: Principal, stack: StoredStack, volume: Volume, key: str
    ) -> RemoveVolume:
        reason: str | None = None
        protected = self._self_identity().protects_volume(stack.environment_id, volume.name)
        if not in_project(volume.labels, key) and not (protected and actor.is_administrator):
            reason = VOLUME_REASON_FOREIGN
        elif protected:
            reason = VOLUME_REASON_SELF if actor.is_administrator else VOLUME_REASON_IN_USE
        elif await self._has_control(stack.environment_id, volume.name):
            reason = VOLUME_REASON_CONTROLLED
        writable = True
        if reason is None and not actor.is_administrator:
            try:
                await self._authoriser.require_access(
                    actor, ResourceKey(stack.environment_id, "volume", volume.name), "write"
                )
            except (ResourceNotFound, PermissionDenied):
                writable = False
        return RemoveVolume(volume.name, volume.driver, reason is None, writable, reason)

    async def _has_control(self, environment_id: int, volume_name: str) -> bool:
        return await self._control_repo.get_for(environment_id, "volume", volume_name) is not None

    async def _may_write(self, actor: Principal, environment_id: int, name: str) -> bool:
        if actor.is_administrator:
            return True
        try:
            await self._authoriser.require_access(
                actor, ResourceKey(environment_id, "stack", name), "write"
            )
        except (ResourceNotFound, PermissionDenied):
            return False
        return True

    async def _services(
        self, actor: Principal, stack: StoredStack, declared: Mapping[str, str | None]
    ) -> tuple[tuple[ServiceView, ...], int, bool]:
        environment_id = stack.environment_id
        key = canonical_stack_key(stack.name)
        members: list[ContainerSummary] = []
        available = True
        try:
            client = await self._hosts.client(environment_id)
            members = [
                c for c in await read_project_containers(client) if in_project(c.labels, key)
            ]
        except (EnvironmentUnavailable, ObjectTooLarge):
            available = False
        permitted = await permitted_container_ids(self._authoriser, actor, environment_id, members)
        by_service: dict[str, list[ContainerSummary]] = {}
        for container in members:
            by_service.setdefault(container.labels.get(SERVICE_LABEL) or "", []).append(container)
        views: list[ServiceView] = []
        for service_name in sorted({*declared, *by_service}):
            group = by_service.get(service_name, [])
            seen = [c for c in group if c.id in permitted]
            image = declared.get(service_name) or (group[0].image if group else None)
            views.append(
                ServiceView(
                    name=service_name or "(unknown)",
                    image=image,
                    declared=service_name in declared,
                    containers=tuple(_entry_of(c) for c in seen[:MAX_SHOWN_CONTAINERS]),
                    hidden=len(group) - len(seen),
                )
            )
        return tuple(views), len(members) - sum(len(v.containers) for v in views), available


@dataclass(frozen=True, slots=True)
class StoredModel:
    """A stored file resolved with `config`, its secret values and the directory it was read in."""

    resolved: ResolvedProject
    secrets: frozenset[str]
    directory: str


def _report_of(name: str, validated: Validated) -> ValidationReport:
    return ValidationReport(
        name=name,
        services=validated.services,
        networks=validated.networks,
        volumes=validated.volumes,
        images=validated.images,
        to_pull=validated.to_pull,
        restricted=validated.restricted,
        compose_digest=validated.compose_digest,
        resolved_digest=validated.resolved_digest,
        restricted_options=tuple(
            RestrictedOption(option, stack_option_label(option)) for option in validated.restricted
        ),
        warnings=validated.warnings,
    )


def stack_key(environment_id: int, name: str) -> ObjectKey:
    return ObjectKey(environment_id, "stack", canonical_stack_key(name))


def _unreadable_files() -> StackRejected:
    return StackRejected(
        {"files": "A stored secret file cannot be read with this server's key."},
        "secrets_unreadable",
    )


def validation_request(
    *,
    environment: Environment,
    client: EngineClient,
    cli: ComposeCli,
    target: ComposeTarget,
    project: ComposeProject,
    text: str,
    values: VariableSet,
    deploy: bool = False,
    pull: bool = False,
    choices: Mapping[str, CredentialChoice] | None = None,
    collisions: bool = False,
    files: Sequence[CompanionFile] = (),
    builds: BuildInputs | None = None,
) -> ValidationRequest:
    return ValidationRequest(
        environment=environment,
        client=client,
        cli=cli,
        target=target,
        project=project,
        text=text,
        values=values.values,
        secrets=values.secrets,
        deploy=deploy,
        pull=pull,
        choices=choices or {},
        collisions=collisions,
        files=files,
        builds=builds,
    )
