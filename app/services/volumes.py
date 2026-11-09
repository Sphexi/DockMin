"""Volumes on a host: lists and details, forms and reviews, and the audited volume operations."""

import asyncio
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Final, Literal, NoReturn

import anyio

from app.auth.errors import (
    SELF_PROTECTED_REASON,
    PermissionDenied,
    ProductError,
    ResourceNotFound,
    SelfProtected,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.db.engine import Database, Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.db.repositories.pagination import CursorPosition, Page, encode_cursor
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.team import TeamRepository
from app.db.repositories.user import UserRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import (
    Conflict,
    EngineError,
    EnvironmentUnreachable,
    NotFound,
    RequestOutcomeUnknown,
    ResponseTooLarge,
)
from app.docker_engine.models import ContainerSummary, Volume
from app.logging import get_logger
from app.self_identity import SelfIdentitySource, no_identity
from app.services.access import ResourceKey, ResourceRef, SubmittedLabels, is_canonical_id, stack_of
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.container_actions import (
    ANONYMOUS_VOLUME_LABEL,
    FORCEABLE_STATES,
    MAX_BATCH,
    BusyView,
    ContainerGone,
    ContainerStateConflict,
    EngineRefused,
    engine_detail,
)
from app.services.containers import (
    AccessPanel,
    ContainerListTooLarge,
    build_access_panel,
    display_name,
    parse_engine_time,
    short_id,
    stack_kind,
    stack_name,
)
from app.services.grantees import GranteeDirectory
from app.services.guards import require_administrator
from app.services.host_objects import (
    DEFAULT_PAGE_SIZE,
    MAX_PRUNE,
    USAGE_FILTERS,
    PruneStatus,
    audited_client,
    audited_list_read,
    busy_view,
    check_limit,
    create_failure,
    decode_position,
    describe_users,
    failure_reason,
    hold_all,
    label_views,
    option_views,
    permitted_container_ids,
    read_containers,
    read_volumes,
    row_id,
    run_shielded,
    settled,
    volume_users,
)
from app.services.object_inspector import (
    VOLUME_OR_NETWORK_NAME,
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
    unavailable_from,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.resource_controls import ControlView, ResourceControlService
from app.services.volume_views import (
    CHANGED_SINCE_REVIEW,
    MAX_FORCE_CONTAINERS,
    ContainerRemover,
    ForceContainer,
    ForceImpact,
    ForceRefused,
    ForceResult,
    ForceStatus,
    ForceStep,
    PruneMode,
    RestrictedVolume,
    VolumeBatchConfirmation,
    VolumeBatchEntry,
    VolumeBatchItem,
    VolumeBatchResult,
    VolumeBatchStatus,
    VolumeBatchTarget,
    VolumeChangedSinceReview,
    VolumeCreated,
    VolumeDetail,
    VolumeFilters,
    VolumeGone,
    VolumeInUse,
    VolumeKept,
    VolumeKind,
    VolumeNameInUse,
    VolumePage,
    VolumePruneCandidate,
    VolumePruneItem,
    VolumePrunePlan,
    VolumePruneResult,
    VolumePruneTarget,
    VolumeRefused,
    VolumeRemoved,
    VolumeRemoveImpact,
    VolumeRow,
    containers_digest,
)

logger = get_logger(__name__)

__all__ = [
    "DRIVER_FILTERS",
    "KIND_FILTERS",
    "PRUNE_MODES",
    "VolumeQueryService",
    "VolumeService",
]

MAX_QUERY: Final = 128


MAX_NAME: Final = 255


MAX_TEXT: Final = 64


MAX_OPTIONS: Final = 32


MAX_OPTION_VALUE: Final = 1024


MAX_LABELS: Final = 64


MAX_LABEL_CHARS: Final = 64 * 1024


SHORT_NAME_CHARS: Final = 12


SETTLE_SECONDS: Final = 120.0


DRIVER_FILTERS: Final = ("local", "other")


KIND_FILTERS: Final = ("named", "anonymous")


PRUNE_MODES: Final = ("anonymous", "all")


_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,254}")


_HEX64: Final = re.compile(r"[0-9a-f]{64}")


_DRIVER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")


_OPTION_KEY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


_ROW: Final = re.compile(r"(option|label)-([0-9]{1,4})-(key|value)")


@dataclass(frozen=True, slots=True)
class _Request:
    name: str
    driver: str
    options: Mapping[str, str] = field(repr=False)
    option_fields: Mapping[str, str]
    labels: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Entry:
    volume: Volume
    ref: ResourceRef
    kind: VolumeKind
    sort_name: str
    row_id: int


@dataclass(slots=True)
class _Criteria:
    q: str | None
    driver: str | None
    usage: str | None
    kind: str | None
    stack: str | None


@dataclass(slots=True)
class _CreateRun:
    created: bool = False


def _name_argument(arguments: Arguments) -> str:
    return str(arguments["name"])[:MAX_NAME]


def _clean(value: str | None) -> str | None:
    stripped = None if value is None else value.strip()
    return stripped or None


def _is_anonymous(volume: Volume) -> bool:
    return ANONYMOUS_VOLUME_LABEL in volume.labels or _HEX64.fullmatch(volume.name) is not None


def _is_daemon_anonymous(volume: Volume) -> bool:
    return ANONYMOUS_VOLUME_LABEL in volume.labels and _HEX64.fullmatch(volume.name) is not None


def _kind(volume: Volume) -> VolumeKind:
    return "anonymous" if _is_anonymous(volume) else "named"


def _short_name(volume: Volume) -> str:
    return volume.name[:SHORT_NAME_CHARS] if _is_anonymous(volume) else volume.name


def _short_of(name: str) -> str:
    return name[:SHORT_NAME_CHARS] if _HEX64.fullmatch(name) else name


def _compile(filters: VolumeFilters) -> _Criteria:
    fields: dict[str, str] = {}
    text = _clean(filters.q)
    if text is not None and len(text) > MAX_QUERY:
        fields["q"] = f"Search for at most {MAX_QUERY} characters."
    chosen: dict[str, str | None] = {}
    for name, allowed, message, value in (
        ("driver", DRIVER_FILTERS, "Choose local or other.", filters.driver),
        ("usage", USAGE_FILTERS, "Choose used or unused.", filters.usage),
        ("kind", KIND_FILTERS, "Choose named or anonymous.", filters.kind),
    ):
        cleaned = _clean(value)
        if cleaned is not None and cleaned not in allowed:
            fields[name] = message
        chosen[name] = cleaned
    stack = _clean(filters.stack)
    if stack is not None and len(stack) > MAX_QUERY:
        fields["stack"] = f"Use at most {MAX_QUERY} characters."
    if fields:
        raise ValidationFailed(fields)
    return _Criteria(
        None if text is None else text.casefold(),
        chosen["driver"],
        chosen["usage"],
        chosen["kind"],
        None if stack is None else stack.casefold(),
    )


def _entry_of(volume: Volume, environment_id: int) -> _Entry | None:
    if not is_canonical_id("volume", volume.name):
        logger.warning("volume_list_entry_skipped", extra={"reason": "malformed_name"})
        return None
    return _Entry(
        volume=volume,
        ref=ResourceRef(environment_id, "volume", volume.name, volume.labels),
        kind=_kind(volume),
        sort_name=volume.name.casefold(),
        row_id=row_id(volume.name),
    )


def _order(entry: _Entry) -> tuple[str, int, str]:
    return entry.sort_name, entry.row_id, entry.volume.name


def _matches(entry: _Entry, criteria: _Criteria, used: int) -> bool:
    volume = entry.volume
    if criteria.driver is not None and (
        ("local" if volume.driver == "local" else "other") != criteria.driver
    ):
        return False
    if criteria.usage == "used" and used == 0:
        return False
    if criteria.usage == "unused" and used > 0:
        return False
    if criteria.kind is not None and entry.kind != criteria.kind:
        return False
    if criteria.stack is not None and stack_of(volume.labels) != criteria.stack:
        return False
    return criteria.q is None or criteria.q in volume.name.casefold()


def _is_plain(created: Volume, request: _Request) -> bool:
    return (
        created.driver == request.driver
        and dict(created.options) == dict(request.options)
        and dict(created.labels) == dict(request.labels)
    )


def _prune_candidates(
    volumes: Sequence[Volume],
    containers: Sequence[ContainerSummary],
    mode: PruneMode,
    protected: frozenset[str] = frozenset(),
) -> list[VolumePruneCandidate]:
    users = volume_users(containers)
    found: list[VolumePruneCandidate] = []
    for volume in volumes:
        if (
            not is_canonical_id("volume", volume.name)
            or users.get(volume.name)
            or volume.name in protected
        ):
            continue
        if mode == "anonymous" and not _is_daemon_anonymous(volume):
            continue
        found.append(
            VolumePruneCandidate(
                name=volume.name,
                short_name=_short_name(volume),
                driver=volume.driver,
                kind=_kind(volume),
                stack=stack_name(volume.labels),
                created_at_text=volume.created_at,
                created_at=parse_engine_time(volume.created_at),
            )
        )
    found.sort(key=lambda c: (c.name.casefold(), c.name))
    return found


def _kept_by_self(
    volumes: Sequence[Volume], mode: PruneMode, protected: frozenset[str]
) -> tuple[VolumeKept, ...]:
    kept = [
        VolumeKept(volume.name, _short_name(volume))
        for volume in volumes
        if volume.name in protected and (mode == "all" or _is_daemon_anonymous(volume))
    ]
    kept.sort(key=lambda entry: (entry.name.casefold(), entry.name))
    return tuple(kept)


def _distinct_volumes[T](items: Sequence[T], key: Callable[[T], str]) -> list[T]:
    unique = list({key(item): item for item in items}.values())
    if not unique:
        raise ValidationFailed({"volumes": "Select at least one volume."})
    if len(unique) > MAX_BATCH:
        raise ValidationFailed({"volumes": f"Select at most {MAX_BATCH} volumes."})
    return unique


def _has_control(text: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in text)


def _form_rows(form: Mapping[str, str], kind: str) -> list[tuple[int, str, str]]:
    rows: dict[int, dict[str, str]] = {}
    for name, value in form.items():
        match = _ROW.fullmatch(name)
        if match is not None and match[1] == kind:
            rows.setdefault(int(match[2]), {})[match[3]] = value
    return [(i, rows[i].get("key", ""), rows[i].get("value", "")) for i in sorted(rows)]


def _parse_options(
    form: Mapping[str, str], errors: dict[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    options: dict[str, str] = {}
    fields: dict[str, str] = {}
    for index, raw_key, raw_value in _form_rows(form, "option"):
        key, value = raw_key.strip(), raw_value.strip()
        if not key and not value:
            continue
        key_field, value_field = f"option-{index}-key", f"option-{index}-value"
        if len(options) >= MAX_OPTIONS:
            errors.setdefault("options", f"At most {MAX_OPTIONS} options are allowed.")
            break
        if _OPTION_KEY.fullmatch(key) is None:
            errors.setdefault(
                key_field,
                "Use letters, digits, period, underscore and hyphen, starting with a letter or "
                "digit, up to 128 characters.",
            )
        elif len(value) > MAX_OPTION_VALUE or _has_control(value):
            errors.setdefault(
                value_field, "Use at most 1024 characters without control characters."
            )
        elif key in options:
            errors.setdefault(key_field, "This option key is already used in another row.")
        else:
            options[key] = value
            fields[key] = key_field
    return options, fields


def _parse_labels(form: Mapping[str, str], errors: dict[str, str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    chars = 0
    for index, key, value in _form_rows(form, "label"):
        if not key.strip() and not value.strip():
            continue
        if len(labels) >= MAX_LABELS:
            errors.setdefault("labels", f"At most {MAX_LABELS} labels are allowed.")
            break
        if not key.strip() or len(key) > MAX_NAME or _has_control(key):
            errors.setdefault(
                f"label-{index}-key",
                "Label key is required, up to 255 characters, no control characters.",
            )
        elif key.strip().casefold() == ANONYMOUS_VOLUME_LABEL:
            errors.setdefault(f"label-{index}-key", "This label is set by Docker.")
        elif "\0" in value:
            errors.setdefault(f"label-{index}-value", "Label value must not contain NUL.")
        elif key in labels:
            errors.setdefault(
                f"label-{index}-key", "This label key is already used in another row."
            )
        else:
            labels[key] = value
            chars += len(key) + len(value)
    if chars > MAX_LABEL_CHARS:
        errors.setdefault("labels", "Labels must total at most 64 KiB.")
    return labels


def _parse_form(form: Mapping[str, str]) -> _Request:
    errors: dict[str, str] = {}
    name = form.get("name", "").strip()
    if not name:
        errors["name"] = "Enter a name."
    elif _NAME.fullmatch(name) is None:
        errors["name"] = (
            "Use letters, digits, underscore, period and hyphen, starting with a letter or "
            "digit, between 2 and 255 characters."
        )
    elif _HEX64.fullmatch(name) is not None:
        errors["name"] = "This name has the form of an anonymous volume's id and cannot be used."
    driver = form.get("driver", "").strip() or "local"
    if _DRIVER.fullmatch(driver) is None:
        errors["driver"] = "Enter a driver name, such as local."
    options, option_fields = _parse_options(form, errors)
    labels = _parse_labels(form, errors)
    if errors:
        raise ValidationFailed(errors)
    return _Request(name, driver, options, option_fields, labels)


async def _take(
    controls: ResourceControlService, key: ResourceKey, owner_user_id: int | None
) -> tuple[bool, bool]:
    """Claim a just-created volume, first dropping a control an earlier volume of that name left.

    Only for a name that was shown absent under its lock. Returns (claimed, stale control replaced).
    """
    replaced = await controls.release(key)
    return await controls.claim(key, owner_user_id), replaced


async def _settle[T](
    db: Session,
    work: Callable[[], Awaitable[T]],
    row: Callable[[], Awaitable[None]] | None = None,
    note: Callable[[T], None] | None = None,
) -> T:
    async with db.transaction():
        result = await work()
        if note is not None:
            note(result)
        if row is not None:
            await row()
    return result


class VolumeQueryService:
    """Reads volumes through the authoriser; nothing here changes a volume."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        controls: ResourceControlService,
        users: UserRepository,
        teams: TeamRepository,
        self_identity: SelfIdentitySource | None = None,
    ) -> None:
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._controls = controls
        self._self_identity = self_identity or no_identity
        self.grantees = GranteeDirectory(users, teams)

    async def page(
        self,
        actor: Principal,
        environment_id: int,
        filters: VolumeFilters | None = None,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> VolumePage:
        """Return one page of the volumes the caller may see, read live from the host.

        ValidationFailed for a bad filter, limit or cursor; EnvironmentUnavailable,
        VolumeListTooLarge and ContainerListTooLarge when the host cannot be read in full.
        """
        criteria = _compile(filters or VolumeFilters())
        size = check_limit(limit)
        position = decode_position(cursor)
        await self._authoriser.require_environment(actor, environment_id)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        volumes, containers = await asyncio.gather(read_volumes(client), read_containers(client))
        entries = [e for e in (_entry_of(v, environment_id) for v in volumes) if e is not None]
        visible = await self._visible(actor, entries)
        users = volume_users(containers)
        matched = [e for e in visible if _matches(e, criteria, len(users.get(e.volume.name, ())))]
        ordered = sorted(matched, key=_order)
        remaining = (
            ordered
            if position is None
            else [e for e in ordered if _order(e)[:2] > (position.sort_value, position.row_id)]
        )
        window = remaining[:size]
        permitted = await permitted_container_ids(
            self._authoriser, actor, environment_id, containers
        )
        writable = await self._writable(actor, window)
        protected = self._self_identity().volumes.get(environment_id, frozenset())
        rows = [
            self._row_of(
                environment_id, e, users.get(e.volume.name, []), permitted, writable, protected
            )
            for e in window
        ]
        next_cursor = None
        if len(remaining) > size and window:
            last = _order(window[-1])
            next_cursor = encode_cursor(CursorPosition(last[0], last[1]))
        return VolumePage(
            page=Page(rows, next_cursor),
            total=len(ordered),
            visible=len(visible),
            limit=size,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            can_create=await self._authoriser.may_create(actor, environment_id),
            can_prune=actor.is_administrator,
        )

    async def names(
        self, actor: Principal, environment_id: int, prefix: str, *, limit: int
    ) -> list[tuple[str, bool]]:
        """Return the first `limit` visible named volumes, by name, starting with `prefix`.

        Each name comes with whether the caller may write the volume. Case is ignored. One host
        read; EnvironmentUnavailable and VolumeListTooLarge when the host cannot be read.
        """
        await self._authoriser.require_environment(actor, environment_id)
        client = await self._hosts.client(environment_id)
        wanted = prefix.casefold()
        entries = [
            e
            for e in (_entry_of(v, environment_id) for v in await read_volumes(client))
            if e is not None and e.kind == "named" and e.sort_name.startswith(wanted)
        ]
        shown = sorted(await self._visible(actor, entries), key=_order)[:limit]
        writable = await self._writable(actor, shown)
        return [(e.volume.name, e.volume.name in writable) for e in shown]

    async def require_create(self, actor: Principal, environment_id: int) -> None:
        """Refuse unless the caller may create volumes here: ResourceNotFound, PermissionDenied."""
        await self._authoriser.require_create(actor, environment_id)

    async def origin_name(self, actor: Principal, environment_id: int, name: str) -> str:
        """Return the name of a volume the caller may read; ResourceNotFound otherwise."""
        return (await self._readable(actor, environment_id, name, "read")).name

    async def detail(self, actor: Principal, environment_id: int, name: str) -> VolumeDetail:
        """Return one volume for the detail page; ResourceNotFound unless the caller may see it."""
        volume = await self._readable(actor, environment_id, name, "read")
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        containers = await read_containers(client)
        using = volume_users(containers).get(volume.name, [])
        permitted = await permitted_container_ids(self._authoriser, actor, environment_id, using)
        named, more, hidden = describe_users(using, permitted)
        ref = ResourceRef(environment_id, "volume", volume.name, volume.labels)
        writable = bool(await self._authoriser.filter_visible(actor, [ref], "write"))
        return VolumeDetail(
            environment_id=environment_id,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            name=volume.name,
            short_name=_short_name(volume),
            driver=volume.driver,
            scope=volume.scope,
            kind=_kind(volume),
            created_at=parse_engine_time(volume.created_at),
            stack=stack_name(volume.labels),
            stack_kind=stack_kind(volume.labels),
            labels=label_views(volume.labels, actor.is_administrator),
            labels_truncated=volume.labels_truncated,
            mountpoint=volume.mountpoint[:MAX_NAME] if actor.is_administrator else None,
            options=option_views(volume.options) if actor.is_administrator else None,
            containers=named,
            containers_total=len(using),
            containers_more=more,
            containers_hidden=hidden,
            can_remove=writable,
            can_manage_access=actor.is_administrator,
            protected=self._self_identity().protects_volume(environment_id, volume.name),
        )

    async def access_panel(self, actor: Principal, environment_id: int, name: str) -> AccessPanel:
        """Return the access tab's content; administrators only."""
        volume = await self._readable(actor, environment_id, name, "read")
        if not actor.is_administrator:
            raise PermissionDenied("administrator role required")
        key = ResourceKey(environment_id, "volume", volume.name)
        control: ControlView | None = await self._controls.get(actor, key)
        return await build_access_panel(self, key, volume.name, _short_name(volume), control)

    async def remove_impact(
        self, actor: Principal, environment_id: int, name: str
    ) -> VolumeRemoveImpact:
        """Name what removing the volume takes away; write holders only."""
        volume = await self._readable(actor, environment_id, name, "write")
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        using = volume_users(await read_containers(client)).get(volume.name, [])
        permitted = await permitted_container_ids(self._authoriser, actor, environment_id, using)
        named, more, hidden = describe_users(using, permitted)
        return VolumeRemoveImpact(
            environment_id=environment_id,
            environment_name=environment.name,
            name=volume.name,
            short_name=_short_name(volume),
            driver=volume.driver,
            kind=_kind(volume),
            stack=stack_name(volume.labels),
            created_at_text=volume.created_at,
            in_use=len(using),
            containers=named,
            containers_more=more,
            containers_hidden=hidden,
            protected=self._self_identity().protects_volume(environment_id, volume.name),
        )

    async def force_impact(self, actor: Principal, environment_id: int, name: str) -> ForceImpact:
        """Name every container a force removal would remove first, and what would block it.

        Write holders only. A container the caller may not change is counted, never named.
        """
        volume = await self._readable(actor, environment_id, name, "write")
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        using = volume_users(await read_containers(client)).get(volume.name, [])
        readable = await permitted_container_ids(self._authoriser, actor, environment_id, using)
        changeable = await permitted_container_ids(
            self._authoriser, actor, environment_id, using, "write"
        )
        changeable_all = sorted(
            (
                ForceContainer(c.id, short_id(c.id), display_name(c.names, c.id), c.state)
                for c in using
                if c.id in changeable
            ),
            key=lambda c: (c.name.casefold(), c.id),
        )
        listed = tuple(changeable_all[:MAX_FORCE_CONTAINERS])
        return ForceImpact(
            environment_id=environment_id,
            environment_name=environment.name,
            name=volume.name,
            short_name=_short_name(volume),
            driver=volume.driver,
            created_at_text=volume.created_at,
            containers=listed,
            running=tuple(c for c in listed if c.state in FORCEABLE_STATES),
            containers_more=len(changeable_all) - len(listed),
            unwritable=len(using) - len(changeable_all),
            containers_hidden=len(using) - len(readable),
            too_many=len(using) > MAX_FORCE_CONTAINERS,
            containers_digest=containers_digest([c.id for c in using]),
            protected=self._self_identity().protects_volume(environment_id, volume.name),
        )

    async def prune_plan(
        self, actor: Principal, environment_id: int, mode: PruneMode
    ) -> VolumePrunePlan:
        """List the volumes a prune would remove, at most 100; administrators only."""
        if mode not in PRUNE_MODES:
            raise ValidationFailed({"mode": "Choose anonymous or all."})
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        volumes, containers = await asyncio.gather(read_volumes(client), read_containers(client))
        protected = self._self_identity().volumes.get(environment_id, frozenset())
        found = _prune_candidates(volumes, containers, mode, protected)
        return VolumePrunePlan(
            environment_id,
            environment.name,
            mode,
            tuple(found[:MAX_PRUNE]),
            len(found),
            _kept_by_self(volumes, mode, protected),
        )

    async def batch_confirmation(
        self, actor: Principal, environment_id: int, names: Sequence[str]
    ) -> VolumeBatchConfirmation:
        """Name every volume of a batch removal the caller may change; the rest are only counted.

        ValidationFailed when nothing is selected or more than 25 volumes are. Volumes dockmin's
        own container has mounted are listed as kept and are not targets.
        """
        unique = _distinct_volumes(names, str)
        await self._authoriser.require_environment(actor, environment_id)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        users = volume_users(await read_containers(client))
        identity = self._self_identity()
        targets: list[VolumeBatchEntry] = []
        kept: list[VolumeKept] = []
        unavailable = 0
        for name in unique:
            try:
                volume = await self._readable(actor, environment_id, name, "write")
            except (ResourceNotFound, PermissionDenied):
                unavailable += 1
                continue
            if identity.protects_volume(environment_id, volume.name):
                kept.append(VolumeKept(volume.name, _short_name(volume)))
                continue
            using = users.get(volume.name, [])
            permitted = await permitted_container_ids(
                self._authoriser, actor, environment_id, using
            )
            named, more, hidden = describe_users(using, permitted)
            targets.append(
                VolumeBatchEntry(
                    name=volume.name,
                    short_name=_short_name(volume),
                    driver=volume.driver,
                    kind=_kind(volume),
                    stack=stack_name(volume.labels),
                    created_at_text=volume.created_at,
                    in_use=len(using),
                    containers=named,
                    containers_more=more,
                    containers_hidden=hidden,
                )
            )
        return VolumeBatchConfirmation(
            environment_id=environment_id,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            targets=tuple(targets),
            kept=tuple(kept),
            unavailable=unavailable,
        )

    async def _readable(
        self, actor: Principal, environment_id: int, name: str, level: Literal["read", "write"]
    ) -> Volume:
        if VOLUME_OR_NETWORK_NAME.fullmatch(name) is None:
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "volume", name), level
        )
        return await self._inspector.volume(environment_id, name)

    async def _visible(self, actor: Principal, entries: Sequence[_Entry]) -> list[_Entry]:
        permitted = {
            ref.resource_id
            for ref in await self._authoriser.filter_visible(actor, [e.ref for e in entries])
        }
        return [e for e in entries if e.volume.name in permitted]

    async def _writable(self, actor: Principal, entries: Sequence[_Entry]) -> frozenset[str]:
        held = await self._authoriser.filter_visible(actor, [e.ref for e in entries], "write")
        return frozenset(ref.resource_id for ref in held)

    def _row_of(
        self,
        environment_id: int,
        entry: _Entry,
        using: Sequence[ContainerSummary],
        permitted: frozenset[str],
        writable: frozenset[str],
        protected: frozenset[str],
    ) -> VolumeRow:
        volume = entry.volume
        named, more, hidden = describe_users(using, permitted)
        return VolumeRow(
            environment_id=environment_id,
            name=volume.name,
            short_name=_short_name(volume),
            driver=volume.driver,
            scope=volume.scope,
            kind=entry.kind,
            created_at=parse_engine_time(volume.created_at),
            stack=stack_name(volume.labels),
            stack_kind=stack_kind(volume.labels),
            in_use=len(using),
            containers=named,
            containers_more=more,
            containers_hidden=hidden,
            can_remove=volume.name in writable,
            protected=volume.name in protected,
        )


class VolumeService:
    """Create and remove volumes, each one audited operation, plus the force and prune runs."""

    def __init__(
        self,
        *,
        db: Session,
        database: Database,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        locks: ObjectLockRegistry,
        controls: ResourceControlService,
        activity: ActivityRecorder,
        clock: Clock,
        remove_container: ContainerRemover,
        self_identity: SelfIdentitySource | None = None,
    ) -> None:
        self._self_identity = self_identity or no_identity
        self._db = db
        self._database = database
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._locks = locks
        self._controls = controls
        self._activity = activity
        self._clock = clock
        self._remove_container = remove_container
        self._background: set[asyncio.Future[None]] = set()

    @audited(action="volume_created", target_type="volume", environment_id=environment_argument)
    async def create(
        self, actor: Principal, environment_id: int, form: Mapping[str, str]
    ) -> VolumeCreated:
        """Create a volume from a submitted form; the caller owns it afterwards.

        A standard user may create only a plain `local` volume: any other driver or any driver
        option binds a host path or a remote filesystem. A name that already exists is refused,
        never adopted. A control left on the name by a volume removed outside the product is
        replaced by the new owner's. ValidationFailed keyed by form field; VolumeRefused when
        Docker rejects it; EnvironmentUnavailable.
        """
        audit = current_audit()
        await self._authoriser.require_create(actor, environment_id)
        await self._hosts.environment(environment_id)
        request = self._validated(actor, form)
        audit.target_id = request.name
        audit.detail.update(
            driver=request.driver,
            option_keys=",".join(request.options),
            labels=len(request.labels),
        )
        restricted = ",".join(
            token
            for token, used in (("driver", request.driver != "local"), ("options", request.options))
            if used
        )
        if restricted and actor.is_administrator:
            audit.detail["restricted"] = restricted
        client = await audited_client(self._hosts, environment_id)
        holder = LockHolder(UserActor.of(actor), actor.username, "create")
        key = ObjectKey(environment_id, "volume", request.name)
        async with hold_all(self._locks, [key], holder):
            await self._require_absent(client, request.name)
            run = _CreateRun()
            try:
                created = await run_shielded(
                    partial(self._create_on_host, client, request, run),
                    partial(self._interrupted_create, actor, environment_id, request, run),
                )
            except EngineError as error:
                raise create_failure(
                    error,
                    event="volume_create_refused",
                    name_in_use=VolumeNameInUse,
                    refused=VolumeRefused,
                ) from None
            if not _is_plain(created, request):
                audit.detail.update(preexisting=True, control_created=False)
                return VolumeCreated(environment_id, request.name, created.driver, False, True)
            claimed = await self._claim(actor, environment_id, request.name)
        return VolumeCreated(environment_id, request.name, request.driver, claimed, False)

    @audited(
        action="volume_removed",
        target_type="volume",
        target_id=_name_argument,
        environment_id=environment_argument,
    )
    async def remove(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        *,
        expected_created_at: str,
        expected_in_use: int,
        via_force: bool = False,
        containers_removed: int = 0,
        pruned: bool = False,
        batched: bool = False,
    ) -> VolumeRemoved:
        """Remove a volume the caller reviewed, then release its control.

        `expected_created_at` and `expected_in_use` are what the confirmation showed; either
        differing refuses the removal, and both are checked once the volume's lock is held. A
        volume that any container mounts is refused, whatever `via_force` says: the force path
        removes those containers first, then sets `via_force` and `containers_removed`, which
        only label the audit row. Docker is never asked to force the removal. The control is
        released in the transaction that commits the success row; if that fails the removal
        still counts and the row records control_released as false. SelfProtected, after the
        caller is authorised, for a volume the container dockmin runs in has mounted.
        """
        audit = current_audit()
        if VOLUME_OR_NETWORK_NAME.fullmatch(name) is None:
            raise ResourceNotFound("not found")
        try:
            await self._authoriser.require_access(
                actor, ResourceKey(environment_id, "volume", name), "write"
            )
        except (EnvironmentUnavailable, ObjectTooLarge):
            audit.refuse()
            raise
        await self._hosts.environment(environment_id)
        if self._self_identity().protects_volume(environment_id, name):
            audit.reason = SELF_PROTECTED_REASON
            raise SelfProtected.volume(name)
        holder = LockHolder(UserActor.of(actor), actor.username, "remove")
        key = ObjectKey(environment_id, "volume", name)
        async with hold_all(self._locks, [key], holder):
            self._inspector.forget_volume(environment_id, name)
            volume, client = await self._load(environment_id, name)
            audit.detail.update(
                driver=volume.driver, force=via_force, containers_removed=containers_removed
            )
            if pruned:
                audit.detail["pruned"] = True
            if batched:
                audit.detail["batch"] = True
            in_use = await self._count_in_use(client, name)
            audit.detail["in_use_containers"] = in_use
            if volume.created_at != expected_created_at or in_use != expected_in_use:
                audit.reason = "changed_since_review"
                raise VolumeChangedSinceReview()
            if in_use:
                audit.reason = "volume_in_use"
                raise VolumeInUse(in_use)
            try:
                await run_shielded(
                    partial(client.volumes.remove, name, force=False),
                    partial(self._interrupted_remove, actor, environment_id, name),
                )
            except EngineError as error:
                raise await self._remove_failure(error, client, environment_id, name) from None
            released = await self._release(actor, environment_id, name)
        return VolumeRemoved(environment_id, name, volume.driver, released)

    async def remove_with_containers(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        *,
        expected_created_at: str,
        expected_containers: str,
        expected_names: Mapping[str, str],
    ) -> ForceResult:
        """Remove every container that uses the volume, then the volume.

        Everything the confirmation showed is checked first and any difference refuses the whole
        run: the volume's creation time, the digest of the containers that use it, and each
        container's name. ForceRefused when a container is running, paused or restarting, when
        the caller may not change one, or when more than 25 use the volume. Each container goes
        through the audited container removal, without its anonymous volumes, in name order; the
        run stops at the first container that is not removed. The volume is then removed through
        the audited removal.
        """
        if VOLUME_OR_NETWORK_NAME.fullmatch(name) is None:
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "volume", name), "write"
        )
        await self._hosts.environment(environment_id)
        if self._self_identity().protects_volume(environment_id, name):
            await self._record_protected_removal(actor, environment_id, name)
        volume = await self._inspector.volume(environment_id, name)
        client = await self._hosts.client(environment_id)
        using = volume_users(await read_containers(client)).get(name, [])
        if (
            volume.created_at != expected_created_at
            or containers_digest([c.id for c in using]) != expected_containers
        ):
            raise VolumeChangedSinceReview()
        changeable = await permitted_container_ids(
            self._authoriser, actor, environment_id, using, "write"
        )
        listed = sorted(
            (c for c in using if c.id in changeable),
            key=lambda c: (display_name(c.names, c.id).casefold(), c.id),
        )
        running = tuple(
            ForceContainer(c.id, short_id(c.id), display_name(c.names, c.id), c.state)
            for c in listed
            if c.state in FORCEABLE_STATES
        )
        unwritable = len(using) - len(listed)
        too_many = len(using) > MAX_FORCE_CONTAINERS
        if running or unwritable or too_many:
            raise ForceRefused(running, unwritable, too_many=too_many)
        if any(expected_names.get(c.id) != display_name(c.names, c.id) for c in listed):
            raise VolumeChangedSinceReview()
        steps: list[ForceStep] = []
        for container in listed:
            step = await self._force_step(actor, environment_id, container)
            steps.append(step)
            if step.status not in ("removed", "gone"):
                return ForceResult(
                    environment_id,
                    name,
                    tuple(steps),
                    None,
                    f"Stopped at {step.name}; the volume was not removed.",
                )
        removed = sum(1 for step in steps if step.status in ("removed", "gone"))
        try:
            outcome = await self.remove(
                actor,
                environment_id,
                name,
                expected_created_at=expected_created_at,
                expected_in_use=0,
                via_force=True,
                containers_removed=removed,
            )
        except (
            VolumeInUse,
            VolumeChangedSinceReview,
            VolumeGone,
            ObjectBusy,
            EnvironmentUnavailable,
            EngineRefused,
            ObjectTooLarge,
            ResourceNotFound,
            PermissionDenied,
            ContainerListTooLarge,
        ) as error:
            return ForceResult(environment_id, name, tuple(steps), None, _failure_text(error))
        return ForceResult(environment_id, name, tuple(steps), outcome, None)

    async def run_prune(
        self,
        actor: Principal,
        environment_id: int,
        mode: PruneMode,
        targets: Sequence[VolumePruneTarget],
    ) -> VolumePruneResult:
        """Remove exactly the reviewed volumes that are still candidates, one at a time.

        Each listed volume is its own audited removal with its own lock and row; nothing is
        rolled back or stopped early. The Engine's own prune endpoint is never called.
        Administrators only, at most 100 volumes. `anonymous` takes only volumes labelled as
        anonymous by the daemon; `all` takes every unused volume, named ones included.
        """
        if mode not in PRUNE_MODES:
            raise ValidationFailed({"mode": "Choose anonymous or all."})
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        listed = list({t.name: t for t in targets}.values())
        if not listed or len(listed) > MAX_PRUNE:
            raise ValidationFailed({"volumes": f"Select between 1 and {MAX_PRUNE} volumes."})
        if any(VOLUME_OR_NETWORK_NAME.fullmatch(t.name) is None for t in listed):
            raise ValidationFailed({"volumes": "A volume name in the selection is not valid."})
        await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        volumes, containers = await asyncio.gather(read_volumes(client), read_containers(client))
        identity = self._self_identity()
        protected = identity.volumes.get(environment_id, frozenset())
        current = {c.name: c for c in _prune_candidates(volumes, containers, mode, protected)}
        items: list[VolumePruneItem] = []
        for target in listed:
            if target.name in protected:
                items.append(VolumePruneItem(target.name, _short_of(target.name), "protected"))
                continue
            candidate = current.get(target.name)
            if candidate is None or candidate.created_at_text != target.created_at:
                items.append(
                    VolumePruneItem(target.name, target.name[:SHORT_NAME_CHARS], "skipped_changed")
                )
                continue
            items.append(await self._prune_one(actor, environment_id, candidate))
        return VolumePruneResult(mode, tuple(items))

    async def run_batch_remove(
        self, actor: Principal, environment_id: int, targets: Sequence[VolumeBatchTarget]
    ) -> VolumeBatchResult:
        """Remove up to 25 reviewed volumes in order, each its own audited removal.

        Not atomic and never stopped early; a refusal on one volume is that volume's result.
        Force is not available: a volume in use is reported, not removed. ValidationFailed only
        when the selection is empty or over the limit.
        """
        listed = _distinct_volumes(targets, lambda target: target.name)
        await self._authoriser.require_environment(actor, environment_id)
        await self._hosts.environment(environment_id)
        return VolumeBatchResult(
            tuple([await self._batch_item(actor, environment_id, target) for target in listed])
        )

    def describe_busy(self, busy: ObjectBusy, viewer: Principal) -> BusyView:
        """Describe the lock holder to a viewer: names only for administrators."""
        return busy_view(busy, viewer, self._locks.now())

    @audited(
        action="volume_removed",
        target_type="volume",
        target_id=_name_argument,
        environment_id=environment_argument,
    )
    async def _record_protected_removal(
        self, actor: Principal, environment_id: int, name: str
    ) -> NoReturn:
        current_audit().reason = SELF_PROTECTED_REASON
        raise SelfProtected.volume(name)

    async def _batch_item(
        self, actor: Principal, environment_id: int, target: VolumeBatchTarget
    ) -> VolumeBatchItem:
        short = _short_of(target.name)

        def item(status: VolumeBatchStatus, message: str) -> VolumeBatchItem:
            return VolumeBatchItem(target.name, short, status, message)

        try:
            await self.remove(
                actor,
                environment_id,
                target.name,
                expected_created_at=target.created_at,
                expected_in_use=target.in_use,
                batched=True,
            )
        except SelfProtected as error:
            return item("protected", str(error))
        except VolumeInUse:
            return item("in_use", "In use by a container. Stop and remove the container first.")
        except VolumeChangedSinceReview:
            return item("changed", CHANGED_SINCE_REVIEW)
        except ObjectBusy:
            return item("busy", "Another action on this volume is in progress.")
        except (VolumeGone, ResourceNotFound):
            return item("gone", "Not found, or already removed.")
        except PermissionDenied:
            return item("forbidden", "You can read this volume but not change it.")
        except (EnvironmentUnavailable, ObjectTooLarge) as error:
            return item("unreachable", str(error))
        except EngineRefused as error:
            return item("failed", str(error))
        except Exception as error:
            logger.error(
                "volume_batch_item_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            return item("failed", "The volume could not be removed.")
        return item("removed", "Removed.")

    async def _prune_one(
        self, actor: Principal, environment_id: int, candidate: VolumePruneCandidate
    ) -> VolumePruneItem:
        status: PruneStatus
        try:
            await self.remove(
                actor,
                environment_id,
                candidate.name,
                expected_created_at=candidate.created_at_text,
                expected_in_use=0,
                pruned=True,
            )
        except VolumeInUse:
            status = "skipped_in_use"
        except VolumeChangedSinceReview:
            status = "skipped_changed"
        except ObjectBusy:
            status = "busy"
        except (VolumeGone, ResourceNotFound):
            status = "gone"
        except Exception as error:
            logger.warning(
                "volume_prune_item_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            status = "failed"
        else:
            status = "removed"
        return VolumePruneItem(candidate.name, candidate.short_name, status)

    async def _force_step(
        self, actor: Principal, environment_id: int, container: ContainerSummary
    ) -> ForceStep:
        name = display_name(container.names, container.id)

        def step(status: ForceStatus, message: str) -> ForceStep:
            return ForceStep(container.id, short_id(container.id), name, status, message)

        try:
            await self._remove_container(
                actor, environment_id, container.id, expected_name=name, force=False, volumes=False
            )
        except ContainerGone:
            return step("gone", "Already removed.")
        except ResourceNotFound:
            return step("not_found", "Not found.")
        except PermissionDenied:
            return step("forbidden", "You can read this container but not change it.")
        except ObjectBusy:
            return step("busy", "Another action on this container is in progress.")
        except (ContainerStateConflict, ValidationFailed) as error:
            return step("conflict", str(error))
        except EnvironmentUnavailable as error:
            return step("unreachable", str(error))
        except (EngineRefused, ObjectTooLarge) as error:
            return step("failed", str(error))
        except Exception as error:
            logger.error(
                "volume_force_step_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            return step("failed", "The container could not be removed.")
        return step("removed", "Removed.")

    def _validated(self, actor: Principal, form: Mapping[str, str]) -> _Request:
        audit = current_audit()
        try:
            request = _parse_form(form)
            SubmittedLabels(request.labels)
        except ValidationFailed as invalid:
            audit.reason = "invalid"
            audit.detail["fields"] = ",".join(sorted(invalid.fields))
            raise
        if not actor.is_administrator:
            refused: dict[str, str] = {}
            if request.driver != "local":
                refused["driver"] = "Only administrators can choose a volume driver."
            for key in request.options:
                refused[request.option_fields[key]] = (
                    f"Only administrators can set the option {key}."
                )
            if refused:
                audit.reason = "restricted"
                audit.detail["fields"] = ",".join(sorted(refused))
                if request.options:
                    audit.detail["option_keys"] = ",".join(request.options)
                raise RestrictedVolume(refused)
        return request

    async def _require_absent(self, client: EngineClient, name: str) -> None:
        audit = current_audit()
        try:
            await client.volumes.inspect(name)
        except NotFound:
            return
        except ResponseTooLarge:
            audit.reason = "too_large"
            raise ObjectTooLarge() from None
        except EngineError as error:
            unavailable = unavailable_from(error)
            audit.reason = unavailable.kind
            raise unavailable from None
        audit.reason = "name_in_use"
        raise VolumeNameInUse()

    async def _create_on_host(
        self, client: EngineClient, request: _Request, run: _CreateRun
    ) -> Volume:
        volume = await client.volumes.create(
            name=request.name,
            driver=request.driver,
            driver_opts=request.options,
            labels=request.labels,
        )
        run.created = True
        return volume

    async def _claim(self, actor: Principal, environment_id: int, name: str) -> bool:
        audit = current_audit()
        key = ResourceKey(environment_id, "volume", name)

        def note(taken: tuple[bool, bool]) -> None:
            audit.detail["control_created"] = taken[0]
            if taken[1]:
                audit.detail["stale_control_replaced"] = True

        try:
            created, _ = await retry_busy(
                partial(
                    _settle,
                    self._db,
                    partial(_take, self._controls, key, actor.user_id),
                    None,
                    note,
                )
            )
        except asyncio.CancelledError:
            await self._write_settled(
                actor,
                "volume_created",
                environment_id,
                name,
                {**audit.detail, "interrupted": True},
                claim=True,
            )
            raise
        except Exception as error:
            self._log_control_failure("volume_control_claim_failed", key, error)
            audit.detail["control_created"] = False
            return False
        return created

    async def _release(self, actor: Principal, environment_id: int, name: str) -> bool:
        audit = current_audit()
        key = ResourceKey(environment_id, "volume", name)

        def note(_: bool) -> None:
            audit.detail["control_released"] = True

        try:
            await retry_busy(
                partial(_settle, self._db, partial(self._controls.release, key), None, note)
            )
        except asyncio.CancelledError:
            await self._write_settled(
                actor,
                "volume_removed",
                environment_id,
                name,
                {**audit.detail, "interrupted": True},
                claim=False,
            )
            raise
        except Exception as error:
            self._log_control_failure("volume_control_release_failed", key, error)
            audit.detail["control_released"] = False
            return False
        return True

    async def _release_gone(self, environment_id: int, name: str) -> None:
        audit = current_audit()
        key = ResourceKey(environment_id, "volume", name)
        try:
            async with self._database.session() as db:
                recorder = ActivityRecorder(db, self._clock)
                controls = ResourceControlService(
                    db=db, controls=ResourceControlRepository(db, self._clock), activity=recorder
                )
                released = await retry_busy(partial(_settle, db, partial(controls.release, key)))
        except Exception as error:
            self._log_control_failure("volume_control_release_failed", key, error)
            audit.detail["control_released"] = False
        else:
            audit.detail["control_released"] = released

    async def _load(self, environment_id: int, name: str) -> tuple[Volume, EngineClient]:
        audit = current_audit()
        try:
            volume = await self._inspector.volume(environment_id, name)
            client = await self._hosts.client(environment_id)
        except EnvironmentUnavailable as error:
            audit.reason = error.kind
            raise
        except ObjectTooLarge:
            audit.reason = "too_large"
            raise
        except ResourceNotFound:
            audit.reason = "volume_gone"
            raise VolumeGone() from None
        return volume, client

    async def _count_in_use(self, client: EngineClient, name: str) -> int:
        containers = await audited_list_read(read_containers(client))
        return len(volume_users(containers).get(name, ()))

    async def _remove_failure(
        self, error: EngineError, client: EngineClient, environment_id: int, name: str
    ) -> ProductError:
        audit = current_audit()
        logger.warning(
            "volume_remove_refused",
            extra={"error": type(error).__name__, "status": error.status_code},
        )
        if isinstance(error, NotFound):
            audit.reason = "volume_gone"
            await self._release_gone(environment_id, name)
            return VolumeGone()
        if isinstance(error, RequestOutcomeUnknown):
            audit.reason = "outcome_unknown"
            return EnvironmentUnavailable("outcome_unknown")
        if isinstance(error, EnvironmentUnreachable):
            audit.reason = "unreachable"
            return EnvironmentUnavailable("unreachable")
        if isinstance(error, Conflict):
            in_use = 0
            try:
                in_use = await self._count_in_use(client, name)
            except ProductError:
                in_use = 0
            audit.reason = "volume_in_use"
            return VolumeInUse(in_use)
        audit.reason = "engine_rejected"
        return EngineRefused(engine_detail(error))

    async def _interrupted_create(
        self,
        actor: Principal,
        environment_id: int,
        request: _Request,
        run: _CreateRun,
        task: asyncio.Future[Volume],
    ) -> None:
        audit = current_audit()
        name = request.name
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, SETTLE_SECONDS)
            detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
            if run.created and finished and error is None:
                if _is_plain(task.result(), request):
                    await self._write_settled(
                        actor, "volume_created", environment_id, name, detail, claim=True
                    )
                else:
                    detail.update(preexisting=True, control_created=False)
                    await self._write_settled(actor, "volume_created", environment_id, name, detail)
            elif not finished:
                task.add_done_callback(
                    partial(self._settle_late_create, actor, environment_id, request, dict(detail))
                )
                await self._write_settled(
                    actor,
                    "volume_created",
                    environment_id,
                    name,
                    detail,
                    failure="outcome_unknown",
                )
            else:
                await self._write_settled(
                    actor,
                    "volume_created",
                    environment_id,
                    name,
                    detail,
                    failure=failure_reason(error) if error is not None else "interrupted",
                )

    def _settle_late_create(
        self,
        actor: Principal,
        environment_id: int,
        request: _Request,
        detail: dict[str, DetailValue],
        task: asyncio.Future[Volume],
    ) -> None:
        if task.cancelled() or task.exception() is not None:
            return
        plain = _is_plain(task.result(), request)
        future = asyncio.ensure_future(
            self._write_settled(
                actor,
                "volume_created",
                environment_id,
                request.name,
                {**detail, "settled_late": True, **({} if plain else {"preexisting": True})},
                claim=plain,
            )
        )
        self._background.add(future)
        future.add_done_callback(self._background.discard)

    async def _interrupted_remove(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        task: asyncio.Future[None],
    ) -> None:
        audit = current_audit()
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, SETTLE_SECONDS)
            detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
            if finished and error is None:
                await self._write_settled(
                    actor, "volume_removed", environment_id, name, detail, claim=False
                )
                return
            await self._write_settled(
                actor,
                "volume_removed",
                environment_id,
                name,
                detail,
                failure=failure_reason(error) if error is not None else "outcome_unknown",
            )

    async def _write_settled(
        self,
        actor: Principal,
        action: str,
        environment_id: int,
        name: str,
        detail: dict[str, DetailValue],
        *,
        claim: bool | None = None,
        failure: str | None = None,
    ) -> None:
        user = UserActor.of(actor)
        try:
            async with self._database.session() as db:
                recorder = ActivityRecorder(db, self._clock)
                if failure is not None:
                    await recorder.record_failure(
                        user,
                        action=action,
                        target_type="volume",
                        target_id=name,
                        environment_id=environment_id,
                        reason=failure,
                        detail=detail,
                        message=str(EnvironmentUnavailable("outcome_unknown"))
                        if failure == "outcome_unknown"
                        else None,
                    )
                    return
                write = partial(
                    recorder.record_event,
                    user,
                    action=action,
                    target_type="volume",
                    target_id=name,
                    environment_id=environment_id,
                    result="success",
                    detail=detail,
                )
                if claim is None:
                    await retry_busy(write)
                    return
                controls = ResourceControlService(
                    db=db, controls=ResourceControlRepository(db, self._clock), activity=recorder
                )
                key = ResourceKey(environment_id, "volume", name)

                def note_taken(taken: tuple[bool, bool]) -> None:
                    detail["control_created"] = taken[0]
                    if taken[1]:
                        detail["stale_control_replaced"] = True

                def note_released(_: bool) -> None:
                    detail["control_released"] = True

                try:
                    if claim:
                        await retry_busy(
                            partial(
                                _settle,
                                db,
                                partial(_take, controls, key, actor.user_id),
                                write,
                                note_taken,
                            )
                        )
                    else:
                        await retry_busy(
                            partial(
                                _settle, db, partial(controls.release, key), write, note_released
                            )
                        )
                except Exception as error:
                    self._log_control_failure("volume_control_settle_failed", key, error)
                    detail["control_created" if claim else "control_released"] = False
                    await retry_busy(write)
        except Exception as error:
            logger.error(
                "activity_event_write_failed",
                extra={
                    "action": action,
                    "result": "failure" if failure else "success",
                    "actor_user_id": actor.user_id,
                    "target_type": "volume",
                    "target_id": name,
                    "environment_id": environment_id,
                    "reason": failure,
                    "error": type(error).__name__,
                },
            )

    def _log_control_failure(self, event: str, key: ResourceKey, error: Exception) -> None:
        logger.error(
            event,
            extra={
                "environment_id": key.environment_id,
                "volume_name": key.resource_id,
                "error": type(error).__name__,
            },
        )


def _failure_text(error: Exception) -> str:
    if isinstance(error, ObjectBusy):
        return "The volume is busy with another action."
    return str(error)
