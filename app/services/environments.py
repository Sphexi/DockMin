"""Environments: creation with a staged connection test, editing, status, tags, grants, deletion."""

import asyncio
import errno
import os
import stat
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal, get_args

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.compose_cli.parsers import FLOOR, parse_version_text
from app.db.engine import Session
from app.db.repositories.clocked import Clock, format_timestamp, parse_timestamp
from app.db.repositories.environment import (
    MAX_NAME_FILTER,
    MAX_TAG_FILTERS,
    ConnectionKind,
    DashboardQuery,
    DashboardRow,
    Environment,
    EnvironmentDashboardRepository,
    EnvironmentRepository,
    EnvironmentStatus,
    EnvironmentType,
    FailedStage,
    SwarmState,
)
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.environment_group import MAX_GROUP_OPTIONS, EnvironmentGroupRepository
from app.db.repositories.environment_snapshot import (
    EnvironmentSnapshotRepository,
    SnapshotCounts,
    observed_times,
)
from app.db.repositories.environment_tls import EnvironmentTlsRepository, StoredTls
from app.db.repositories.errors import DuplicateError, MissingReferenceError
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.pagination import Page
from app.db.repositories.registry_access import RegistryAccessRepository
from app.db.repositories.tag import (
    MAX_TAG_OPTIONS,
    MAX_TAGS_PER_ENVIRONMENT,
    Tag,
    TagRepository,
)
from app.db.repositories.team import TeamRepository
from app.db.repositories.user import UserRepository
from app.docker_engine.registry import EngineClientRegistry
from app.docker_engine.tls_validation import (
    TlsField,
    TlsMetadata,
    TlsValidation,
    describe_certificate,
    normalise_pem,
    validate_tls_material,
)
from app.docker_engine.transport import (
    DEFAULT_SOCKET_PATH,
    DEFAULT_TLS_PORT,
    PLAIN_TCP_REFUSAL,
    EndpointError,
    TlsMaterial,
    canonical_tcp_endpoint,
    canonical_unix_endpoint,
    has_hidden_characters,
    split_tcp_endpoint,
    validate_socket_path,
)
from app.logging import get_logger
from app.netpolicy import literal_host_refusal
from app.scheduler.loop import SchedulerBusy
from app.services.activity import ActivityRecorder, audited, current_audit
from app.services.authorisation import Authoriser
from app.services.environment_access import (
    GrantList,
    GroupReference,
    list_grants,
    resolve_grantee,
)
from app.services.environment_connector import (
    ConnectionTestFailed,
    ConnectionTestReport,
    EnvironmentConnector,
    MaterialUnavailable,
    connection_fingerprint,
    report_of,
    status_record_of,
)
from app.services.environment_snapshots import SnapshotPoker
from app.services.guards import require_administrator

logger = get_logger(__name__)

NAME_MAX = 64
DEFAULT_PAGE_SIZE = 25
MAX_BATCH = 50
MAX_IMPACT_NAMES = 50
MAX_DETAIL_NAMES = 400
LOCAL_NOTE = "This is the Docker daemon of the host this product runs on."
ACTIVITY_NOTE = (
    "Activity history is kept. Its entries stay in the log, detached from the environment."
)
MAX_ID = 2**63 - 1
HOST_NOTE = (
    "Nothing is changed on the host. Its containers keep running and are no longer managed here."
)
ADMINISTRATORS_NOTE = "Administrators lose access to it too."

STATUSES: tuple[EnvironmentStatus, ...] = get_args(EnvironmentStatus)

_KINDS: dict[str, ConnectionKind] = {"unix_socket": "unix_socket", "tcp_tls": "tcp_tls"}
_TLS_FIELDS = {
    TlsField.CA_CERTIFICATE: "ca_certificate",
    TlsField.CLIENT_CERTIFICATE: "client_certificate",
    TlsField.CLIENT_KEY: "client_key",
    TlsField.MATERIAL: "tls",
}
_TLS_REQUIRED = "Upload the CA certificate, the client certificate and the client key together."
_TLS_LOCAL_ONLY = "TLS material applies only to a remote environment."
_REVIEW_REQUIRED = "Review the endpoint before saving; the one you were shown no longer matches."
_NAME_TAKEN = "An environment with this name or endpoint already exists."
_NAME_EXISTS = "An environment with this name already exists."
CHANGED_SINCE_REVIEW = "Changed since it was reviewed; nothing was deleted."
ENVIRONMENT_BUSY = "An automation is running against this environment. Try again when it ends."


class EnvironmentBusy(SchedulerBusy):
    """Raised when an environment cannot be deleted while a run of one of its jobs is in flight."""

    def __init__(self) -> None:
        super().__init__(ENVIRONMENT_BUSY)


@dataclass(frozen=True, slots=True)
class ConnectionInput:
    """The connection fields of the form: a kind, and the fields that kind uses."""

    kind: str
    socket_path: str = DEFAULT_SOCKET_PATH
    host: str = ""
    port: int | str = DEFAULT_TLS_PORT


@dataclass(frozen=True, slots=True)
class TlsInput:
    """The three TLS items as uploaded or pasted; empty on an edit means keep what is stored."""

    ca_certificate: str = field(default="", repr=False)
    client_certificate: str = field(default="", repr=False)
    client_key: str = field(default="", repr=False)

    @property
    def any_provided(self) -> bool:
        """Report whether at least one item was supplied."""
        return any(
            item.strip() for item in (self.ca_certificate, self.client_certificate, self.client_key)
        )


@dataclass(frozen=True, slots=True)
class EnvironmentSpec:
    """A create or update request.

    reviewed_endpoint is the canonical endpoint the preview showed; the request is refused unless
    it equals the canonical form of the submitted connection fields.
    """

    name: str
    connection: ConnectionInput
    tls: TlsInput = field(default_factory=TlsInput)
    reviewed_endpoint: str | None = None
    save_anyway: bool = False


@dataclass(frozen=True, slots=True)
class DashboardFilters:
    """Filters of the environment list; group_id and ungrouped are mutually exclusive."""

    q: str | None = None
    group_id: int | None = None
    ungrouped: bool = False
    status: str | None = None
    tag_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class TagView:
    """A tag as a template sees it."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class GroupOption:
    """A group offered by the filter."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class FilterOptions:
    """What the dashboard filters may offer the caller."""

    groups: tuple[GroupOption, ...]
    tags: tuple[TagView, ...]
    statuses: tuple[str, ...] = STATUSES


@dataclass(frozen=True, slots=True)
class EndpointPreview:
    """The parsed endpoint shown before a save; endpoint is what reviewed_endpoint must carry."""

    kind: ConnectionKind
    endpoint: str
    is_local: bool
    socket_path: str | None
    host: str | None
    port: int | None
    note: str
    changes_endpoint: bool = False
    discards_tls_material: bool = False


TlsSummary = TlsMetadata


@dataclass(frozen=True, slots=True)
class ConnectionDetails:
    """Where an environment is and how it is reached; administrators only."""

    kind: ConnectionKind
    endpoint: str
    is_local: bool
    socket_path: str | None
    host: str | None
    port: int | None
    tls: TlsSummary | None


@dataclass(frozen=True, slots=True)
class SnapshotView:
    """The cached counts of a host and how fresh they are; administrators only."""

    captured_at: str | None
    stale: bool
    error: str | None
    counts: SnapshotCounts | None


@dataclass(frozen=True, slots=True)
class EnvironmentSummary:
    """One environment as the dashboard and detail pages show it.

    For a standard user the connection, the snapshot, the failed stage and the status message are
    None: they see status, versions, Swarm state, the group, the tags and whether the host is the
    local socket.
    """

    id: int
    name: str
    is_local: bool
    type: EnvironmentType | None
    status: EnvironmentStatus
    failed_stage: FailedStage | None
    status_message: str | None
    last_checked_at: str | None
    last_contacted_at: str | None
    engine_version: str | None
    api_version: str | None
    swarm_state: SwarmState | None
    swarm_control_available: bool | None
    group: GroupReference | None
    tags: tuple[TagView, ...]
    connection: ConnectionDetails | None
    snapshot: SnapshotView | None
    compose_cli_version: str | None = None


@dataclass(frozen=True, slots=True)
class EnvironmentDetail:
    """An environment with the fields only its own page shows."""

    summary: EnvironmentSummary
    compose_cli_version: str | None
    created_at: str | None
    can_manage: bool
    compose_supported: bool | None = None
    compose_required: str = FLOOR.text
    tls_warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecordCount:
    """How many records of one kind are deleted with an environment."""

    label: str
    count: int


@dataclass(frozen=True, slots=True)
class GroupGrantees:
    """The group an environment is in and who is granted through it."""

    group: GroupReference
    grants: GrantList


@dataclass(frozen=True, slots=True)
class EnvironmentImpact:
    """Everything a deletion names: who loses it, what goes with it, what stays."""

    environment_id: int
    name: str
    kind: ConnectionKind
    endpoint: str
    is_local: bool
    direct: GrantList
    group: GroupGrantees | None
    standard_users_affected: int
    records: tuple[RecordCount, ...]
    administrators_note: str = ADMINISTRATORS_NOTE
    activity_note: str = ACTIVITY_NOTE
    host_note: str = HOST_NOTE


@dataclass(frozen=True, slots=True)
class DeletionPreview:
    """The impact of deleting a set of environments, with totals across them."""

    items: tuple[EnvironmentImpact, ...]
    missing: tuple[int, ...]
    totals: tuple[RecordCount, ...]


type BatchOutcome = Literal["deleted", "not_found", "changed", "failed"]


@dataclass(frozen=True, slots=True)
class BatchItemResult:
    """What happened to one environment of a batch delete."""

    environment_id: int
    name: str | None
    outcome: BatchOutcome
    message: str


@dataclass(frozen=True, slots=True)
class BatchDeleteResult:
    """One result per requested environment, in request order."""

    items: tuple[BatchItemResult, ...]

    @property
    def deleted(self) -> int:
        """Return how many environments were deleted."""
        return sum(1 for item in self.items if item.outcome == "deleted")


@dataclass(frozen=True, slots=True)
class _Parsed:
    kind: ConnectionKind
    endpoint: str
    address: str
    socket_path: str | None
    host: str | None
    port: int | None


@dataclass(frozen=True, slots=True)
class _Material:
    ca_certificate: str
    client_certificate: str
    client_key: str = field(repr=False)
    validation: TlsValidation


class EnvironmentService:
    """Applies environment rules; every method takes the acting principal and decides for itself."""

    def __init__(
        self,
        *,
        db: Session,
        environments: EnvironmentRepository,
        dashboard: EnvironmentDashboardRepository,
        tls: EnvironmentTlsRepository,
        groups: EnvironmentGroupRepository,
        tags: TagRepository,
        snapshots: EnvironmentSnapshotRepository,
        grants: EnvironmentAccessRepository,
        registry_access: RegistryAccessRepository,
        users: UserRepository,
        teams: TeamRepository,
        authoriser: Authoriser,
        connector: EnvironmentConnector,
        registry: EngineClientRegistry,
        poker: SnapshotPoker,
        activity: ActivityRecorder,
        clock: Clock,
        stale_after: timedelta,
        after_delete: Callable[[], Awaitable[object]] | None = None,
        connections_changed: Callable[[], None] | None = None,
    ) -> None:
        self._connections_changed = connections_changed
        self._db = db
        self._environments = environments
        self._dashboard = dashboard
        self._tls = tls
        self._groups = groups
        self._tags = tags
        self._snapshots = snapshots
        self._grants = grants
        self._registry_access = registry_access
        self._users = users
        self._teams = teams
        self._authoriser = authoriser
        self._connector = connector
        self._registry = registry
        self._poker = poker
        self._activity = activity
        self._clock = clock
        self._stale_after = stale_after
        self._after_delete = after_delete

    async def dashboard(
        self,
        actor: Principal,
        filters: DashboardFilters | None = None,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> Page[EnvironmentSummary]:
        """Return a page of the environments the caller may see, with no Engine call.

        Administrators see every environment; a standard user sees those granted to them or to a
        group, and no endpoint, TLS detail or count.
        """
        query = _query_of(actor, filters or DashboardFilters())
        page = await self._dashboard.page(query, limit=limit, cursor=cursor)
        tags = await self._tags.tags_for([row.id for row in page.items])
        now = self._clock()
        items = [
            self._summarise(row, tags.get(row.id, []), actor.is_administrator, now, None)
            for row in page.items
        ]
        return Page(items, page.next_cursor)

    async def dashboard_count(
        self, actor: Principal, filters: DashboardFilters | None = None
    ) -> int:
        """Count the environments dashboard lists for the same caller and filters."""
        query = _query_of(actor, filters or DashboardFilters())
        return await self._dashboard.count(query)

    async def check_assignment(
        self,
        actor: Principal,
        *,
        group_id: int | None,
        tag_ids: Sequence[int],
        environment_id: int | None = None,
    ) -> None:
        """Refuse a group or tag selection that assign_group or set_tags would; writes nothing.

        With an environment id, also raises ResourceNotFound when the caller cannot see it.
        """
        if environment_id is None:
            require_administrator(actor)
        else:
            await self._require_manager(actor, environment_id)
        if group_id is not None:
            if not 1 <= group_id <= MAX_ID:
                raise ValidationFailed({"group": "Choose one of the listed groups."})
            if await self._groups.get(group_id) is None:
                raise ValidationFailed({"group": "That group no longer exists."})
        wanted = set(tag_ids)
        if any(not 1 <= tag_id <= MAX_ID for tag_id in wanted):
            raise ValidationFailed({"tags": "Choose from the listed tags."})
        if len(wanted) > MAX_TAGS_PER_ENVIRONMENT:
            raise ValidationFailed(
                {"tags": f"An environment carries at most {MAX_TAGS_PER_ENVIRONMENT} tags."}
            )
        if len(await self._tags.get_many(wanted)) != len(wanted):
            raise ValidationFailed({"tags": "A selected tag no longer exists."})

    async def filter_options(self, actor: Principal) -> FilterOptions:
        """Return the groups and tags the dashboard may filter on; a standard user's are limited."""
        visible_to = None if actor.is_administrator else actor.user_id
        groups = await self._groups.list_options(visible_to=visible_to, limit=MAX_GROUP_OPTIONS)
        tags = await self._tags.list_options(visible_to=visible_to, limit=MAX_TAG_OPTIONS)
        return FilterOptions(
            groups=tuple(GroupOption(group.id, group.name) for group in groups),
            tags=tuple(TagView(tag.id, tag.name) for tag in tags),
        )

    async def get(self, actor: Principal, environment_id: int) -> EnvironmentDetail:
        """Return one environment; ResourceNotFound whether it is missing or not the caller's."""
        await self._authoriser.require_environment(actor, environment_id)
        row = await self._dashboard.get(environment_id)
        if row is None:
            raise ResourceNotFound("not found")
        tags = await self._tags.list_for_environment(environment_id, limit=MAX_TAGS_PER_ENVIRONMENT)
        summary_tls = None
        if actor.is_administrator and row.connection_kind == "tcp_tls":
            stored = await self._tls.get(environment_id)
            if stored is not None and stored.client_certificate:
                summary_tls = describe_certificate(stored.client_certificate, self._clock())
        summary = self._summarise(row, tags, actor.is_administrator, self._clock(), summary_tls)
        return EnvironmentDetail(
            summary=summary,
            compose_cli_version=row.compose_cli_version,
            created_at=row.created_at if actor.is_administrator else None,
            can_manage=actor.is_administrator,
            compose_supported=_compose_supported(row.compose_cli_version),
        )

    async def preview(
        self,
        actor: Principal,
        connection: ConnectionInput,
        *,
        environment_id: int | None = None,
    ) -> EndpointPreview:
        """Parse the connection fields into the endpoint that would be saved; no network, no write.

        With an environment id, also states whether the endpoint changes and whether switching to
        the local kind discards the stored TLS material.
        """
        require_administrator(actor)
        parsed = _parse_connection(connection)
        changes = False
        discards = False
        if environment_id is not None:
            current = await self._environments.get(environment_id)
            if current is None:
                raise ResourceNotFound("not found")
            changes = parsed.kind != current.connection_kind or parsed.address != current.endpoint
            discards = current.connection_kind == "tcp_tls" and parsed.kind == "unix_socket"
        is_local = parsed.kind == "unix_socket"
        note = (
            LOCAL_NOTE
            if is_local
            else f"Connects over mutual TLS to {parsed.host} on port {parsed.port}."
        )
        return EndpointPreview(
            kind=parsed.kind,
            endpoint=parsed.endpoint,
            is_local=is_local,
            socket_path=parsed.socket_path,
            host=parsed.host,
            port=parsed.port,
            note=note,
            changes_endpoint=changes,
            discards_tls_material=discards,
        )

    @audited(action="environment_created", target_type="environment")
    async def create(self, actor: Principal, spec: EnvironmentSpec) -> EnvironmentDetail:
        """Create an environment after a connection test; administrators only.

        A failed test refuses the save with ConnectionTestFailed unless spec.save_anyway is set,
        in which case the environment is saved with the stage that failed.
        """
        require_administrator(actor)
        name = _check_name(spec.name)
        parsed = _parse_connection(spec.connection)
        _check_reviewed(spec.reviewed_endpoint, parsed)
        audit = current_audit()
        audit.detail.update(name=name, endpoint=parsed.endpoint)
        await self._refuse_duplicates(parsed, name, excluding=None)
        material = await _validated_material(spec.tls) if parsed.kind == "tcp_tls" else None
        if parsed.kind == "unix_socket" and spec.tls.any_provided:
            raise ValidationFailed({"tls": _TLS_LOCAL_ONLY})
        result = await self._connector.test_proposed(
            parsed.kind, parsed.address, None if material is None else _tls_material(material)
        )
        report = report_of(result)
        audit.detail.update(status=report.status, failed_stage=report.failed_stage)
        if not report.reachable and not spec.save_anyway:
            raise ConnectionTestFailed(report)
        record = status_record_of(result, format_timestamp(self._clock()))
        async with self._db.transaction():
            await self._refuse_folded_name(name, excluding=None)
            try:
                created = await self._environments.create(
                    name=name,
                    connection_kind=parsed.kind,
                    socket_path=parsed.socket_path if parsed.kind == "unix_socket" else None,
                    url=parsed.address if parsed.kind == "tcp_tls" else None,
                    record=record,
                )
            except DuplicateError:
                raise ValidationFailed({"name": _NAME_TAKEN}) from None
            if material is not None:
                await self._tls.replace(
                    created.id,
                    ca_certificate=material.ca_certificate,
                    client_certificate=material.client_certificate,
                    client_key=material.client_key,
                )
                audit.detail["tls_warnings"] = len(material.validation.warnings)
            granted = await self._registry_access.grant_default_entries(created.id)
            if granted:
                audit.detail["registry_grants"] = ",".join(str(entry) for entry in granted)
            audit.target_id = str(created.id)
        self._poker.poke(created.id)
        self._announce_connection_change()
        detail = await self.get(actor, created.id)
        return replace(detail, tls_warnings=_warnings_of(material))

    async def registry_addresses(self, actor: Principal, environment_id: int) -> tuple[str, ...]:
        """List the registry addresses an image check on the environment contacts, by address.

        Administrators only; an address is listed when a set granted to the environment is at it.
        """
        require_administrator(actor)
        await self._require_manager(actor, environment_id)
        return tuple(await self._registry_access.granted_addresses(environment_id))

    @audited(
        action="environment_updated",
        target_type="environment",
        target_id=lambda a: str(a["environment_id"]),
    )
    async def update(
        self, actor: Principal, environment_id: int, spec: EnvironmentSpec
    ) -> EnvironmentDetail:
        """Rename an environment or change its connection; administrators only.

        The connection is tested again only when the kind, the endpoint or the TLS material
        changes; a rename never reaches the host. Empty TLS items keep the stored material.
        """
        current = await self._require_manager(actor, environment_id)
        stored = await self._tls.get(environment_id)
        name = _check_name(spec.name)
        parsed = _parse_connection(spec.connection)
        _check_reviewed(spec.reviewed_endpoint, parsed)
        audit = current_audit()
        await self._refuse_duplicates(parsed, name, excluding=environment_id)

        material: _Material | None = None
        if parsed.kind == "tcp_tls":
            if spec.tls.any_provided:
                material = await _validated_material(spec.tls)
            elif current.connection_kind != "tcp_tls" or not _complete(stored):
                raise ValidationFailed({"tls": PLAIN_TCP_REFUSAL})
        elif spec.tls.any_provided:
            raise ValidationFailed({"tls": _TLS_LOCAL_ONLY})

        endpoint_changed = (
            parsed.kind != current.connection_kind or parsed.address != current.endpoint
        )
        connection_changed = endpoint_changed or material is not None
        record = None
        if connection_changed:
            proposed = await self._proposed_material(environment_id, stored, material, parsed)
            result = await self._connector.test_proposed(
                parsed.kind, parsed.address, proposed, environment_id=environment_id
            )
            report = report_of(result)
            audit.detail.update(status=report.status, failed_stage=report.failed_stage)
            if not report.reachable and not spec.save_anyway:
                raise ConnectionTestFailed(report)
            record = status_record_of(result, format_timestamp(self._clock()))

        renamed = name != current.name
        async with self._db.transaction():
            latest = await self._environments.get(environment_id)
            if latest is None:
                raise ResourceNotFound("not found")
            latest_tls = await self._tls.get(environment_id)
            if connection_fingerprint(latest, latest_tls) != connection_fingerprint(
                current, stored
            ):
                raise ValidationFailed(
                    {"name": "This environment was changed by someone else. Reload and try again."}
                )
            try:
                if renamed:
                    await self._refuse_folded_name(name, excluding=environment_id)
                    await self._environments.rename(environment_id, name)
                    audit.detail.update(previous_name=current.name, name=name)
                if connection_changed and record is not None:
                    await self._environments.replace_connection(
                        environment_id,
                        connection_kind=parsed.kind,
                        socket_path=parsed.socket_path if parsed.kind == "unix_socket" else None,
                        url=parsed.address if parsed.kind == "tcp_tls" else None,
                        record=record,
                    )
                    await self._snapshots.delete(environment_id)
                    audit.detail.update(
                        previous_endpoint=_display_endpoint(current), endpoint=parsed.endpoint
                    )
                    if material is not None:
                        await self._tls.replace(
                            environment_id,
                            ca_certificate=material.ca_certificate,
                            client_certificate=material.client_certificate,
                            client_key=material.client_key,
                        )
                        audit.detail["tls"] = "replaced"
                    elif parsed.kind == "unix_socket" and stored is not None:
                        await self._tls.delete(environment_id)
                        audit.detail["tls"] = "removed"
            except DuplicateError:
                raise ValidationFailed({"name": _NAME_TAKEN}) from None
            if not renamed and not connection_changed:
                audit.unchanged()
        if connection_changed:
            observed_times(self._db).forget(environment_id)
            await self._registry.invalidate(environment_id)
            self._poker.poke(environment_id)
            self._announce_connection_change()
        detail = await self.get(actor, environment_id)
        return replace(detail, tls_warnings=_warnings_of(material))

    @audited(
        action="environment_connection_tested",
        target_type="environment",
        target_id=lambda a: str(a["environment_id"]),
    )
    async def test_connection(self, actor: Principal, environment_id: int) -> ConnectionTestReport:
        """Test a saved environment now and store the status it finds; administrators only."""
        environment = await self._require_manager(actor, environment_id)
        stored = await self._tls.get(environment_id)
        fingerprint = connection_fingerprint(environment, stored)
        result = await self._connector.test_stored(environment, stored)
        report = report_of(result)
        record = status_record_of(result, format_timestamp(self._clock()))
        audit = current_audit()
        audit.detail.update(status=report.status, failed_stage=report.failed_stage)
        async with self._db.transaction():
            latest = await self._environments.get(environment_id)
            if latest is None:
                raise ResourceNotFound("not found")
            unchanged = fingerprint == connection_fingerprint(
                latest, await self._tls.get(environment_id)
            )
            if unchanged:
                await self._environments.record_status(environment_id, record)
            audit.detail["recorded"] = unchanged
        self._poker.poke(environment_id)
        self._announce_connection_change()
        return report

    async def refresh(self, actor: Principal, environment_id: int) -> None:
        """Ask for the environment's snapshot to be refreshed soon; administrators only."""
        await self._require_manager(actor, environment_id)
        self._poker.poke(environment_id)

    @audited(
        action="environment_deleted",
        target_type="environment",
        target_id=lambda a: str(a["environment_id"]),
    )
    async def delete(self, actor: Principal, environment_id: int, *, expected_name: str) -> None:
        """Delete an environment and everything scoped to it; administrators only.

        Nothing on the host is touched. Activity history is kept and detached. The environment is
        deleted only while its name is still expected_name, the one the caller reviewed, and not
        while a run of one of its jobs is in flight.
        """
        await self._require_manager(actor, environment_id)
        async with self._db.transaction():
            environment = await self._environments.get(environment_id)
            if environment is None:
                raise ResourceNotFound("not found")
            if environment.name != expected_name:
                raise ValidationFailed({"name": CHANGED_SINCE_REVIEW})
            audit = current_audit()
            if await JobRunRepository(self._db, self._clock).in_flight_for_environment(
                environment_id
            ):
                audit.reason = "automation_running"
                raise EnvironmentBusy
            dependents = await self._environments.dependents(environment_id)
            await self._environments.delete(environment_id)
            audit.detail.update(
                name=environment.name,
                kind=environment.connection_kind,
                endpoint=_display_endpoint(environment),
                rules=dependents.automation_rules,
                job_runs=dependents.job_runs,
            )
        await self._registry.invalidate(environment_id)
        self._announce_connection_change()
        if self._after_delete is not None:
            await self._after_delete()

    async def delete_preview(
        self, actor: Principal, environment_ids: Sequence[int]
    ) -> DeletionPreview:
        """Name, before anything is deleted, what deleting these environments takes away."""
        require_administrator(actor)
        ids = _batch_ids(environment_ids)
        items: list[EnvironmentImpact] = []
        missing: list[int] = []
        for environment_id in ids:
            environment = await self._environments.get(environment_id)
            if environment is None:
                missing.append(environment_id)
                continue
            items.append(await self._impact_of(environment))
        return DeletionPreview(tuple(items), tuple(missing), _totals(items))

    async def delete_many(
        self,
        actor: Principal,
        environment_ids: Sequence[int],
        *,
        expected_names: Mapping[int, str],
    ) -> BatchDeleteResult:
        """Delete up to fifty environments, each in its own audited operation; not atomic.

        An environment whose current name differs from the one expected for it is left alone and
        reported as changed.
        """
        require_administrator(actor)
        results: list[BatchItemResult] = []
        for environment_id in _batch_ids(environment_ids):
            existing = await self._environments.get(environment_id)
            name = None if existing is None else existing.name
            try:
                await self.delete(
                    actor, environment_id, expected_name=expected_names.get(environment_id, "")
                )
            except ResourceNotFound:
                results.append(
                    BatchItemResult(environment_id, name, "not_found", "It no longer exists.")
                )
            except ValidationFailed:
                results.append(
                    BatchItemResult(environment_id, name, "changed", CHANGED_SINCE_REVIEW)
                )
            except EnvironmentBusy as busy:
                results.append(BatchItemResult(environment_id, name, "failed", str(busy)))
            except Exception as error:
                logger.error(
                    "environment_delete_failed",
                    extra={"environment_id": environment_id, "error": type(error).__name__},
                )
                results.append(
                    BatchItemResult(environment_id, name, "failed", "It could not be deleted.")
                )
            else:
                results.append(BatchItemResult(environment_id, name, "deleted", "Deleted."))
        return BatchDeleteResult(tuple(results))

    @audited(
        action="environment_tags_set",
        target_type="environment",
        target_id=lambda a: str(a["environment_id"]),
    )
    async def set_tags(
        self, actor: Principal, environment_id: int, tag_ids: Collection[int]
    ) -> tuple[TagView, ...]:
        """Make an environment carry exactly these tags; administrators only."""
        await self._require_manager(actor, environment_id)
        wanted = set(tag_ids)
        if len(wanted) > MAX_TAGS_PER_ENVIRONMENT:
            raise ValidationFailed(
                {"tags": f"An environment carries at most {MAX_TAGS_PER_ENVIRONMENT} tags."}
            )
        known = {tag.id: tag for tag in await self._tags.get_many(wanted)}
        if set(known) != wanted:
            raise ValidationFailed({"tags": "A selected tag no longer exists."})
        before = {
            tag.id: tag
            for tag in await self._tags.list_for_environment(
                environment_id, limit=MAX_TAGS_PER_ENVIRONMENT
            )
        }
        async with self._db.transaction():
            try:
                added, removed = await self._tags.replace_for_environment(environment_id, wanted)
            except MissingReferenceError:
                raise ValidationFailed({"tags": "A selected tag no longer exists."}) from None
            audit = current_audit()
            if not added and not removed:
                audit.unchanged()
            audit.detail["added"] = _names([known[tag_id] for tag_id in added])
            audit.detail["removed"] = _names(
                [before[tag_id] for tag_id in removed if tag_id in before]
            )
        return tuple(TagView(tag.id, tag.name) for tag in known.values())

    @audited(
        action="environment_group_assigned",
        target_type="environment",
        target_id=lambda a: str(a["environment_id"]),
    )
    async def assign_group(
        self, actor: Principal, environment_id: int, group_id: int | None
    ) -> None:
        """Place an environment in a group, or ungroup it with None; administrators only."""
        environment = await self._require_manager(actor, environment_id)
        audit = current_audit()
        if group_id == environment.environment_group_id:
            audit.unchanged()
            return
        target = None if group_id is None else await self._groups.get(group_id)
        if group_id is not None and target is None:
            raise ValidationFailed({"group": "That group no longer exists."})
        previous = (
            None
            if environment.environment_group_id is None
            else await self._groups.get(environment.environment_group_id)
        )
        async with self._db.transaction():
            try:
                if not await self._environments.set_group(environment_id, group_id):
                    raise ResourceNotFound("not found")
            except MissingReferenceError:
                raise ValidationFailed({"group": "That group no longer exists."}) from None
            audit.detail.update(
                group=None if target is None else target.name,
                previous_group=None if previous is None else previous.name,
            )

    @audited(
        action="environment_access_granted",
        target_type="environment",
        target_id=lambda a: str(a["environment_id"]),
    )
    async def grant(
        self,
        actor: Principal,
        environment_id: int,
        *,
        user_id: int | None = None,
        team_id: int | None = None,
    ) -> None:
        """Grant a user or a team access to an environment; administrators only."""
        await self._require_manager(actor, environment_id)
        grantee = await resolve_grantee(self._users, self._teams, user_id=user_id, team_id=team_id)
        async with self._db.transaction():
            try:
                if grantee.kind == "user":
                    await self._grants.add_user("environment", environment_id, grantee.id)
                else:
                    await self._grants.add_team("environment", environment_id, grantee.id)
            except DuplicateError:
                raise ValidationFailed({"grantee": "That grant already exists."}) from None
            except MissingReferenceError:
                raise ValidationFailed({"grantee": "The grantee no longer exists."}) from None
            current_audit().detail.update(grantee_type=grantee.kind, grantee=grantee.name)

    @audited(
        action="environment_access_revoked",
        target_type="environment",
        target_id=lambda a: str(a["environment_id"]),
    )
    async def revoke(
        self,
        actor: Principal,
        environment_id: int,
        *,
        user_id: int | None = None,
        team_id: int | None = None,
    ) -> None:
        """Withdraw a user's or a team's direct grant on an environment; administrators only."""
        await self._require_manager(actor, environment_id)
        grantee = await resolve_grantee(self._users, self._teams, user_id=user_id, team_id=team_id)
        async with self._db.transaction():
            if grantee.kind == "user":
                removed = await self._grants.remove_user("environment", environment_id, grantee.id)
            else:
                removed = await self._grants.remove_team("environment", environment_id, grantee.id)
            if not removed:
                raise ValidationFailed({"grantee": "That grant does not exist."})
            current_audit().detail.update(grantee_type=grantee.kind, grantee=grantee.name)

    def _announce_connection_change(self) -> None:
        if self._connections_changed is not None:
            self._connections_changed()

    async def _require_manager(self, actor: Principal, environment_id: int) -> Environment:
        """Load an environment for an administrator-only operation.

        A standard user who cannot see it gets the same not-found as for a missing one; one who
        can see it is refused with PermissionDenied.
        """
        if not actor.is_administrator:
            await self._authoriser.require_environment(actor, environment_id)
            raise PermissionDenied("administrator role required")
        environment = await self._environments.get(environment_id)
        if environment is None:
            raise ResourceNotFound("not found")
        return environment

    async def _refuse_duplicates(
        self, parsed: _Parsed, name: str, *, excluding: int | None
    ) -> None:
        fields: dict[str, str] = {}
        same_name = await self._environments.get_by_name(name)
        if same_name is not None and same_name.id != excluding:
            fields["name"] = "An environment with this name already exists."
        same_endpoint = await self._environments.find_by_endpoint(parsed.kind, parsed.address)
        if same_endpoint is not None and same_endpoint.id != excluding:
            fields["host" if parsed.kind == "tcp_tls" else "socket_path"] = (
                f"An environment already uses this endpoint: {same_endpoint.name}."
            )
        if fields:
            raise ValidationFailed(fields)

    async def _refuse_folded_name(self, name: str, *, excluding: int | None) -> None:
        clash = await self._environments.find_by_folded_name(name, excluding=excluding)
        if clash is not None:
            raise ValidationFailed({"name": _NAME_EXISTS})

    async def _proposed_material(
        self,
        environment_id: int,
        stored: StoredTls | None,
        material: _Material | None,
        parsed: _Parsed,
    ) -> TlsMaterial | None:
        if parsed.kind != "tcp_tls":
            return None
        if material is not None:
            return _tls_material(material)
        try:
            return await self._connector.material_for(environment_id, stored)
        except MaterialUnavailable:
            raise ValidationFailed({"tls": _TLS_REQUIRED}) from None

    async def _impact_of(self, environment: Environment) -> EnvironmentImpact:
        direct = await list_grants(self._grants, "environment", environment.id)
        group_id = environment.environment_group_id
        group = None if group_id is None else await self._groups.get(group_id)
        grouped = (
            None
            if group is None
            else GroupGrantees(
                GroupReference(group.id, group.name),
                await list_grants(self._grants, "group", group.id),
            )
        )
        dependents = await self._environments.dependents(environment.id)
        controls = dependents.resource_controls
        records = [
            RecordCount("TLS material", dependents.tls_material),
            RecordCount("Tag links", dependents.tag_links),
            RecordCount("Access grants", dependents.access_grants),
            RecordCount("Resource controls", sum(controls.values())),
            *(RecordCount(f"Resource controls: {kind}", count) for kind, count in controls.items()),
            RecordCount("Registry access links", dependents.registry_links),
            RecordCount("Image bindings", dependents.image_bindings),
            RecordCount("Stack records", dependents.stacks),
            RecordCount("Automation rules", dependents.automation_rules),
            RecordCount("Scheduled jobs", dependents.scheduled_jobs),
            RecordCount("Job runs", dependents.job_runs),
            RecordCount("Snapshot", dependents.snapshots),
        ]
        return EnvironmentImpact(
            environment_id=environment.id,
            name=environment.name,
            kind=environment.connection_kind,
            endpoint=_display_endpoint(environment),
            is_local=environment.connection_kind == "unix_socket",
            direct=direct,
            group=grouped,
            standard_users_affected=await self._grants.count_standard_users_with_access(
                environment.id
            ),
            records=tuple(records),
        )

    def _summarise(
        self,
        row: DashboardRow,
        tags: Sequence[Tag],
        administrator: bool,
        now: datetime,
        tls: TlsSummary | None,
    ) -> EnvironmentSummary:
        group = None if row.group_id is None else GroupReference(row.group_id, row.group_name or "")
        connection = snapshot = None
        if administrator:
            connection = _connection_of(row, tls)
            snapshot = self._snapshot_of(row, now)
        return EnvironmentSummary(
            id=row.id,
            name=row.name,
            is_local=row.connection_kind == "unix_socket",
            type=row.type,
            status=row.status,
            failed_stage=row.failed_stage if administrator else None,
            status_message=row.status_message if administrator else None,
            last_checked_at=row.last_checked_at,
            last_contacted_at=row.last_contacted_at,
            engine_version=row.engine_version,
            api_version=row.api_version,
            swarm_state=row.swarm_state,
            swarm_control_available=row.swarm_control_available,
            group=group,
            tags=tuple(TagView(tag.id, tag.name) for tag in tags),
            connection=connection,
            snapshot=snapshot,
            compose_cli_version=row.compose_cli_version,
        )

    def _snapshot_of(self, row: DashboardRow, now: datetime) -> SnapshotView:
        captured = row.snapshot_captured_at
        if captured is None:
            return SnapshotView(None, False, None, None)
        counts = None
        if row.snapshot_error is None:
            counts = SnapshotCounts(
                container_count=row.container_count,
                container_running_count=row.container_running_count,
                container_stopped_count=row.container_stopped_count,
                container_paused_count=row.container_paused_count,
                container_unhealthy_count=row.container_unhealthy_count,
                image_count=row.image_count,
                volume_count=row.volume_count,
                network_count=row.network_count,
                stack_count=row.stack_count,
                service_count=row.service_count,
                swarm_node_count=row.swarm_node_count,
            )
        return SnapshotView(
            captured_at=captured,
            stale=parse_timestamp(captured) < now - self._stale_after,
            error=row.snapshot_error,
            counts=counts,
        )


def _compose_supported(version: str | None) -> bool | None:
    parsed = None if version is None else parse_version_text(version)
    return None if parsed is None else parsed.at_least(FLOOR)


def _check_name(name: str) -> str:
    stripped = name.strip()
    if not stripped or len(stripped) > NAME_MAX or has_hidden_characters(stripped):
        raise ValidationFailed({"name": f"Use 1 to {NAME_MAX} printable characters."})
    return stripped


def _parse_port(port: int | str) -> int:
    if isinstance(port, str):
        text = port.strip()
        if not text:
            return DEFAULT_TLS_PORT
        if not (text.isascii() and text.isdigit()) or len(text) > 5:
            raise EndpointError("port", "Enter a port number between 1 and 65535.")
        return int(text)
    return port


def _parse_connection(connection: ConnectionInput) -> _Parsed:
    kind = _KINDS.get(connection.kind)
    if kind is None:
        raise ValidationFailed({"kind": "Choose the local socket or a remote host."})
    try:
        if kind == "unix_socket":
            path = validate_socket_path(connection.socket_path.strip())
            return _Parsed(kind, canonical_unix_endpoint(path), path, path, None, None)
        port = _parse_port(connection.port)
        url = canonical_tcp_endpoint(connection.host.strip(), port)
        host, parsed_port = split_tcp_endpoint(url)
        refusal = literal_host_refusal(host)
        if refusal is not None:
            raise EndpointError("host", refusal)
        return _Parsed(kind, url, url, None, host, parsed_port)
    except EndpointError as error:
        raise ValidationFailed({error.field: str(error)}) from None


type LocalSocketState = Literal["absent", "not_socket", "no_access", "present"]

_SOCKET_ACCESS: Final = os.R_OK | os.W_OK


def local_socket_endpoint(path: str = DEFAULT_SOCKET_PATH) -> str:
    """Return the canonical endpoint string for a local socket path."""
    return canonical_unix_endpoint(path)


def local_socket_state(path: str = DEFAULT_SOCKET_PATH) -> LocalSocketState:
    """Report whether the Docker socket exists and this process may open it; connects to nothing."""
    try:
        mode = Path(path).stat().st_mode
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            return "absent"
        return "no_access"
    if not stat.S_ISSOCK(mode):
        return "not_socket"
    return "present" if os.access(path, _SOCKET_ACCESS) else "no_access"


def _check_reviewed(reviewed: str | None, parsed: _Parsed) -> None:
    if reviewed != parsed.endpoint:
        raise ValidationFailed({"reviewed_endpoint": _REVIEW_REQUIRED})


def _complete(stored: StoredTls | None) -> bool:
    return (
        stored is not None
        and bool(stored.ca_certificate)
        and bool(stored.client_certificate)
        and bool(stored.client_key_ciphertext)
    )


async def _validated_material(tls: TlsInput) -> _Material:
    items = (tls.ca_certificate, tls.client_certificate, tls.client_key)
    if not tls.any_provided:
        raise ValidationFailed({"tls": PLAIN_TCP_REFUSAL})
    if not all(item.strip() for item in items):
        raise ValidationFailed({"tls": _TLS_REQUIRED})
    ca, certificate, key = (normalise_pem(item) or item for item in items)
    validation = await asyncio.to_thread(validate_tls_material, ca, certificate, key)
    if not validation.ok:
        raise ValidationFailed(
            {_TLS_FIELDS[name]: message for name, message in validation.errors.items()}
        )
    return _Material(ca, certificate, key, validation)


def _warnings_of(material: _Material | None) -> tuple[str, ...]:
    return () if material is None else material.validation.warnings


def _tls_material(material: _Material) -> TlsMaterial:
    return TlsMaterial(material.ca_certificate, material.client_certificate, material.client_key)


def _query_of(actor: Principal, filters: DashboardFilters) -> DashboardQuery:
    fields: dict[str, str] = {}
    status = filters.status
    if status is not None and status not in STATUSES:
        fields["status"] = "Choose one of the listed statuses."
    text = None if filters.q is None else filters.q.strip()
    if text is not None and len(text) > MAX_NAME_FILTER:
        fields["q"] = f"Search for at most {MAX_NAME_FILTER} characters."
    tag_ids = tuple(dict.fromkeys(filters.tag_ids))
    if len(tag_ids) > MAX_TAG_FILTERS:
        fields["tag"] = f"Filter on at most {MAX_TAG_FILTERS} tags."
    if filters.group_id is not None and filters.ungrouped:
        fields["group"] = "Choose a group or ungrouped, not both."
    if fields:
        raise ValidationFailed(fields)
    return DashboardQuery(
        visible_to=None if actor.is_administrator else actor.user_id,
        group_id=filters.group_id,
        ungrouped=filters.ungrouped,
        status=next((known for known in STATUSES if known == status), None),
        name_contains=text or None,
        tag_ids=tag_ids,
    )


def _display_endpoint(environment: Environment) -> str:
    if environment.connection_kind == "unix_socket":
        return canonical_unix_endpoint(environment.endpoint)
    return environment.endpoint


def _connection_of(row: DashboardRow, tls: TlsSummary | None) -> ConnectionDetails:
    if row.connection_kind == "unix_socket":
        assert row.socket_path is not None
        return ConnectionDetails(
            "unix_socket",
            canonical_unix_endpoint(row.socket_path),
            True,
            row.socket_path,
            None,
            None,
            None,
        )
    assert row.url is not None
    host, port = split_tcp_endpoint(row.url)
    return ConnectionDetails("tcp_tls", row.url, False, None, host, port, tls)


def _batch_ids(environment_ids: Sequence[int]) -> list[int]:
    ids = list(dict.fromkeys(environment_ids))
    if not ids:
        raise ValidationFailed({"environments": "Select at least one environment."})
    if len(ids) > MAX_BATCH:
        raise ValidationFailed({"environments": f"Select at most {MAX_BATCH} environments."})
    return ids


def _names(tags: Sequence[Tag]) -> str:
    return ", ".join(tag.name for tag in tags)[:MAX_DETAIL_NAMES]


def _totals(items: Sequence[EnvironmentImpact]) -> tuple[RecordCount, ...]:
    totals: dict[str, int] = {}
    for item in items:
        for record in item.records:
            totals[record.label] = totals.get(record.label, 0) + record.count
    return tuple(RecordCount(label, count) for label, count in totals.items())
