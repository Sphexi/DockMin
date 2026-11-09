"""Swarm configs and secrets: lists, details, create and remove, and the services that use them."""

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Final, Literal, NoReturn

import anyio

from app.auth.errors import PermissionDenied, ProductError, ResourceNotFound, ValidationFailed
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
from app.docker_engine.models import SwarmConfig, SwarmSecret, SwarmService
from app.docker_engine.resources import CONFIG_LIST_BYTES, SECRET_LIST_BYTES
from app.logging import get_logger
from app.services.access import ResourceKey, ResourceRef, SubmittedLabels, is_canonical_id
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.container_actions import BusyView, EngineRefused, engine_detail
from app.services.containers import (
    AccessPanel,
    LabelView,
    build_access_panel,
    parse_engine_time,
    short_id,
    stack_name,
)
from app.services.grantees import GranteeDirectory
from app.services.host_objects import (
    DEFAULT_PAGE_SIZE,
    ObjectUser,
    ServiceListTooLarge,
    SettleStep,
    audited_client,
    audited_list_read,
    busy_view,
    check_limit,
    decode_position,
    describe_service_users,
    failure_reason,
    hold_all,
    in_transaction,
    label_views,
    permitted_service_ids,
    read_services,
    row_id,
    run_shielded,
    settled,
    write_settled_row,
)
from app.services.object_inspector import (
    EngineObjectInspector,
    EnvironmentUnavailable,
    HostClients,
    ObjectTooLarge,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.resource_controls import ControlView, ResourceControlService
from app.services.service_restrictions import references
from app.services.service_spec import OBJECT_NAME
from app.services.swarm_gate import SwarmUnavailable, require_swarm, swarm_unavailable_from

logger = get_logger(__name__)

__all__ = [
    "CONTENT_FIELD",
    "MAX_CONFIG_BYTES",
    "MAX_SECRET_BYTES",
    "MAX_SHOWN_BYTES",
    "VALUE_FIELD",
    "AccessPanelReader",
    "ConfigContent",
    "ConfigDetail",
    "ObjectDetail",
    "ObjectFormContext",
    "ObjectKind",
    "ObjectRemoveImpact",
    "ObjectRemoved",
    "ObjectRequest",
    "SecretDetail",
    "SecretValue",
    "SwarmConfigActions",
    "SwarmConfigQueries",
    "SwarmObjectChangedSinceReview",
    "SwarmObjectCreated",
    "SwarmObjectFilters",
    "SwarmObjectGone",
    "SwarmObjectInUse",
    "SwarmObjectInvalid",
    "SwarmObjectListTooLarge",
    "SwarmObjectNameInUse",
    "SwarmObjectPage",
    "SwarmObjectRefused",
    "SwarmObjectRow",
    "SwarmSecretActions",
    "SwarmSecretQueries",
    "parse_object_form",
    "read_configs",
    "read_secrets",
    "redisplay",
]

MAX_CONFIG_BYTES: Final = 128 * 1024
MAX_SECRET_BYTES: Final = 64 * 1024
MAX_SHOWN_BYTES: Final = 64 * 1024
MAX_LABELS: Final = 64
MAX_LABEL_CHARS: Final = 64 * 1024
MAX_LABEL_KEY: Final = 255
MAX_NAME: Final = 255
MAX_QUERY: Final = 128
SETTLE_SECONDS: Final = 120.0
CONTENT_FIELD: Final = "content"
VALUE_FIELD: Final = "value"

_ID_SHAPED: Final = re.compile(r"[0-9a-z]{25}")
_LABEL_ROW: Final = re.compile(r"label-([0-9]{1,4})-(key|value)")
_CONTROL_CHARACTERS: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TOO_LARGE: Final = {
    "config": f"Content must be at most {MAX_CONFIG_BYTES // 1024} KiB.",
    "secret": f"The value must be at most {MAX_SECRET_BYTES // 1024} KiB.",
}

type ObjectKind = Literal["config", "secret"]


class SecretValue:
    """The bytes of a config's content or a secret's value; every printable form is redacted.

    It cannot be copied or pickled, has no length, and compares by identity.
    """

    __slots__ = ("_data",)

    def __init__(self, data: bytes) -> None:
        self._data = data

    def reveal(self) -> bytes:
        """Return the bytes, for the one call that sends them to the Engine."""
        return self._data

    def __repr__(self) -> str:
        """Return a fixed text that holds none of the value."""
        return "SecretValue(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        """Refuse to be copied or pickled."""
        raise TypeError("a secret value cannot be copied or serialised")


class SwarmObjectGone(ProductError):
    """The config or secret vanished between authorisation and the Engine call."""

    def __init__(self, kind: ObjectKind) -> None:
        super().__init__(f"The {kind} no longer exists.")
        self.kind = kind


class SwarmObjectInUse(ProductError):
    """Services use the config or secret; `users` names the ones the caller may see."""

    def __init__(
        self,
        kind: ObjectKind,
        services: int,
        users: tuple[ObjectUser, ...] = (),
        more: int = 0,
        hidden: int = 0,
    ) -> None:
        super().__init__(f"The {kind} is used by one or more services.")
        self.kind = kind
        self.services = services
        self.users = users
        self.more = more
        self.hidden = hidden


class SwarmObjectRefused(ProductError):
    """Docker rejected the config or secret; the Engine's text is not part of the message.

    engine_detail is that text, scrubbed and cut, for a page shown to an administrator.
    """

    def __init__(self, kind: ObjectKind, engine_detail: str | None = None) -> None:
        super().__init__(f"Docker refused this {kind}.")
        self.kind = kind
        self.engine_detail = engine_detail


class SwarmObjectChangedSinceReview(ValidationFailed):
    """The object is no longer the one the caller reviewed; nothing was changed."""

    def __init__(self, kind: ObjectKind) -> None:
        super().__init__({kind: f"This {kind} changed since you reviewed it; nothing was changed."})


class SwarmObjectNameInUse(ValidationFailed):
    """A config or secret with the requested name already exists."""

    def __init__(self, kind: ObjectKind) -> None:
        super().__init__({"name": f"A {kind} with this name already exists."})


class SwarmObjectInvalid(ValidationFailed):
    """A create form the product refuses before asking Docker; messages never quote the input."""

    def __init__(self, fields: dict[str, str], *, too_large: bool = False) -> None:
        super().__init__(fields)
        self.too_large = too_large


class SwarmObjectListTooLarge(ObjectTooLarge):
    """The host's list of configs or secrets is over the read limit; the message names it."""

    def __init__(self, noun: str, limit_bytes: int) -> None:
        super().__init__(
            f"This host reports more {noun} data than can be listed here "
            f"(limit {limit_bytes // (1024 * 1024)} MiB)."
        )


@dataclass(frozen=True, slots=True)
class ObjectRequest:
    """A validated create form; `size` is set for a config and never for a secret."""

    kind: ObjectKind
    name: str
    labels: Mapping[str, str]
    value: SecretValue
    size: int | None


@dataclass(frozen=True, slots=True)
class SwarmObjectFilters:
    """The list filters as submitted."""

    q: str | None = None
    stack: str | None = None


@dataclass(frozen=True, slots=True)
class SwarmObjectRow:
    """One config or secret of a list."""

    environment_id: int
    kind: ObjectKind
    id: str
    short_id: str
    name: str
    created_at: datetime | None
    updated_at: datetime | None
    stack: str | None
    can_remove: bool


@dataclass(frozen=True, slots=True)
class SwarmObjectPage:
    """A page of rows; `total` matches the filters and `visible` is the caller's count."""

    page: Page[SwarmObjectRow]
    total: int
    visible: int
    limit: int
    environment_name: str
    is_local: bool
    can_create: bool


@dataclass(frozen=True, slots=True)
class ObjectFormContext:
    """What the create form needs: the environment and the byte limit on the content."""

    environment_id: int
    environment_name: str
    kind: ObjectKind
    max_bytes: int


@dataclass(frozen=True, slots=True)
class ConfigContent:
    """A config's content as shown: text, or None for binary; `truncated` when cut for display."""

    size: int
    text: str | None
    truncated: bool


@dataclass(frozen=True, slots=True)
class ObjectDetail:
    """The fields a config and a secret detail share.

    `services` names the services that reference the object, with their mode in `state`; when
    `usage_known` is false the service list was too large to read and the counts are zero.
    """

    environment_id: int
    environment_name: str
    is_local: bool
    kind: ObjectKind
    id: str
    short_id: str
    name: str
    created_at: datetime | None
    updated_at: datetime | None
    stack: str | None
    labels: tuple[LabelView, ...]
    labels_truncated: bool
    services: tuple[ObjectUser, ...]
    services_more: int
    services_hidden: int
    usage_known: bool
    in_use: int
    busy: BusyView | None
    can_remove: bool
    can_manage_access: bool


@dataclass(frozen=True, slots=True)
class ConfigDetail(ObjectDetail):
    """One config, with its content."""

    templated: bool
    content: ConfigContent


@dataclass(frozen=True, slots=True)
class SecretDetail(ObjectDetail):
    """One secret. It carries no value and no size."""

    has_driver: bool


@dataclass(frozen=True, slots=True)
class ObjectRemoveImpact:
    """What removing a config or secret names: the object and the services that use it."""

    environment_id: int
    environment_name: str
    kind: ObjectKind
    id: str
    short_id: str
    name: str
    in_use: int
    services: tuple[ObjectUser, ...]
    services_more: int
    services_hidden: int

    @property
    def removable(self) -> bool:
        """Report whether no service references the object."""
        return self.in_use == 0


@dataclass(frozen=True, slots=True)
class SwarmObjectCreated:
    """What a create did; `control_created` is false when the claim failed."""

    environment_id: int
    kind: ObjectKind
    id: str
    name: str
    control_created: bool


@dataclass(frozen=True, slots=True)
class ObjectRemoved:
    """What a removal did; `control_released` is false when the control could not be dropped."""

    environment_id: int
    kind: ObjectKind
    id: str
    name: str
    control_released: bool


@dataclass(frozen=True, slots=True)
class _Entry:
    swarm_object: SwarmConfig | SwarmSecret
    ref: ResourceRef
    sort_name: str
    row_id: int


@dataclass(slots=True)
class _Criteria:
    q: str | None
    stack: str | None


@dataclass(slots=True)
class _CreateRun:
    object_id: str | None = None


def _kind_of(arguments: Arguments) -> ObjectKind:
    kind: ObjectKind = arguments["self"].kind
    return kind


def _created_action(arguments: Arguments) -> str:
    return f"{_kind_of(arguments)}_created"


def _removed_action(arguments: Arguments) -> str:
    return f"{_kind_of(arguments)}_removed"


def _target_type(arguments: Arguments) -> str:
    return _kind_of(arguments)


def _object_argument(arguments: Arguments) -> str:
    return str(arguments["object_id"])[:64]


def _clean(value: str | None) -> str | None:
    stripped = None if value is None else value.strip()
    return stripped or None


def _compile(filters: SwarmObjectFilters) -> _Criteria:
    fields: dict[str, str] = {}
    text = _clean(filters.q)
    if text is not None and len(text) > MAX_QUERY:
        fields["q"] = f"Search for at most {MAX_QUERY} characters."
    stack = _clean(filters.stack)
    if stack is not None and len(stack) > MAX_QUERY:
        fields["stack"] = f"Use at most {MAX_QUERY} characters."
    if fields:
        raise ValidationFailed(fields)
    return _Criteria(
        None if text is None else text.casefold(), None if stack is None else stack.casefold()
    )


def _matches(entry: _Entry, criteria: _Criteria) -> bool:
    if criteria.stack is not None:
        namespace = stack_name(entry.swarm_object.labels)
        if namespace is None or namespace.casefold() != criteria.stack:
            return False
    return criteria.q is None or criteria.q in entry.swarm_object.name.casefold()


def _order(entry: _Entry) -> tuple[str, int, str]:
    return entry.sort_name, entry.row_id, entry.swarm_object.id


def _entry_of(
    swarm_object: SwarmConfig | SwarmSecret, kind: ObjectKind, environment_id: int
) -> _Entry | None:
    if not is_canonical_id(kind, swarm_object.id):
        logger.warning("swarm_object_list_entry_skipped", extra={"kind": kind})
        return None
    return _Entry(
        swarm_object=swarm_object,
        ref=ResourceRef(environment_id, kind, swarm_object.id, swarm_object.labels),
        sort_name=swarm_object.name[:MAX_NAME].casefold(),
        row_id=row_id(swarm_object.id),
    )


async def read_configs(
    client: EngineClient,
    environment_id: int,
    poke: Callable[[int], None] | None = None,
    *,
    filters: Mapping[str, list[str]] | None = None,
) -> list[SwarmConfig]:
    """Read every config without its content; SwarmObjectListTooLarge past the limit."""
    try:
        return await client.swarm_configs.list(filters=filters)
    except ResponseTooLarge:
        raise SwarmObjectListTooLarge("config", CONFIG_LIST_BYTES) from None
    except EngineError as error:
        raise swarm_unavailable_from(error, environment_id, poke) from None


async def read_secrets(
    client: EngineClient,
    environment_id: int,
    poke: Callable[[int], None] | None = None,
    *,
    filters: Mapping[str, list[str]] | None = None,
) -> list[SwarmSecret]:
    """Read every secret; it carries no value. SwarmObjectListTooLarge past the limit."""
    try:
        return await client.swarm_secrets.list(filters=filters)
    except ResponseTooLarge:
        raise SwarmObjectListTooLarge("secret", SECRET_LIST_BYTES) from None
    except EngineError as error:
        raise swarm_unavailable_from(error, environment_id, poke) from None


def _referencing(
    kind: ObjectKind, services: Sequence[SwarmService], object_id: str
) -> list[SwarmService]:
    found: list[SwarmService] = []
    for service in services:
        held = references({"TaskTemplate": service.spec.task_template})
        used = held.configs if kind == "config" else held.secrets
        if any(entry[0] == object_id for entry in used):
            found.append(service)
    return found


def _content_of(data: bytes) -> ConfigContent:
    head = data[:MAX_SHOWN_BYTES]
    cut = len(data) > len(head)
    text: str | None
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError as error:
        splits_a_character = cut and error.start >= len(head) - 3
        text = head[: error.start].decode("utf-8") if splits_a_character else None
    if text is not None and _CONTROL_CHARACTERS.search(text):
        text = None
    return ConfigContent(size=len(data), text=text, truncated=cut and text is not None)


def parse_object_form(
    kind: ObjectKind, form: Mapping[str, str], upload: bytes | None = None
) -> ObjectRequest:
    """Validate a create form into a request, or raise SwarmObjectInvalid.

    The content (or value) comes from the typed field or the uploaded bytes, not both. Typed
    text has CRLF line endings turned into LF; uploaded bytes are kept as given. No message
    quotes the input.
    """
    errors: dict[str, str] = {}
    name = _name(kind, form.get("name", "").strip(), errors)
    labels = _labels(form, errors)
    field = CONTENT_FIELD if kind == "config" else VALUE_FIELD
    data, too_large = _payload(kind, form.get(field, ""), upload, field, errors)
    if errors:
        raise SwarmObjectInvalid(errors, too_large=too_large)
    return ObjectRequest(
        kind=kind,
        name=name,
        labels=labels,
        value=SecretValue(data),
        size=len(data) if kind == "config" else None,
    )


def _name(kind: ObjectKind, name: str, errors: dict[str, str]) -> str:
    if not name:
        errors["name"] = "Name is required."
    elif OBJECT_NAME.fullmatch(name) is None:
        errors["name"] = (
            "Name must be 1 to 64 letters, digits, hyphens, underscores and periods, "
            "starting and ending with a letter or digit."
        )
    elif _ID_SHAPED.fullmatch(name):
        errors["name"] = "Name must not be 25 lower-case letters and digits, which is an id."
    return name


def _payload(
    kind: ObjectKind,
    typed: str,
    upload: bytes | None,
    field: str,
    errors: dict[str, str],
) -> tuple[bytes, bool]:
    noun = "content" if kind == "config" else "value"
    uploaded = upload if upload else None
    if uploaded is not None and typed:
        errors[field] = f"Enter the {noun} or choose a file, not both."
        return b"", False
    if uploaded is not None:
        data = uploaded
    else:
        try:
            data = typed.replace("\r\n", "\n").encode("utf-8")
        except UnicodeEncodeError:
            errors[field] = f"The {noun} contains characters that cannot be stored; use a file."
            return b"", False
    limit = MAX_CONFIG_BYTES if kind == "config" else MAX_SECRET_BYTES
    if not data:
        errors[field] = f"The {noun} is required."
    elif len(data) > limit:
        errors[field] = _TOO_LARGE[kind]
        return b"", True
    return data, False


def _labels(form: Mapping[str, str], errors: dict[str, str]) -> Mapping[str, str]:
    rows: dict[int, dict[str, str]] = {}
    for field, value in form.items():
        match = _LABEL_ROW.fullmatch(field)
        if match is not None:
            rows.setdefault(int(match[1]), {})[match[2]] = value
    labels: dict[str, str] = {}
    chars = 0
    for index in sorted(rows):
        key, value = rows[index].get("key", ""), rows[index].get("value", "")
        if not key.strip() and not value.strip():
            continue
        key_field = f"label-{index}-key"
        if len(labels) >= MAX_LABELS:
            errors["labels"] = f"At most {MAX_LABELS} labels are allowed."
            break
        if not key.strip():
            errors[key_field] = "Label key is required."
        elif len(key) > MAX_LABEL_KEY or _CONTROL_CHARACTERS.search(key):
            errors[key_field] = (
                f"Label key must be at most {MAX_LABEL_KEY} characters without control characters."
            )
        elif key in labels:
            errors[key_field] = "Label keys must be unique."
        elif "\0" in value:
            errors[f"label-{index}-value"] = "Label value must not contain NUL."
        else:
            labels[key] = value
            chars += len(key) + len(value)
    if chars > MAX_LABEL_CHARS:
        errors["labels"] = "Labels must total at most 64 KiB."
    try:
        SubmittedLabels(labels)
    except ValidationFailed as reserved:
        errors.update(reserved.fields)
    return labels


def redisplay(kind: ObjectKind, form: Mapping[str, str]) -> dict[str, str]:
    """Return the submitted form without a secret's value, for rendering a refused create."""
    if kind == "config":
        return dict(form)
    return {name: value for name, value in form.items() if name != VALUE_FIELD}


class AccessPanelReader:
    """Builds the access tab's content: the control over an object and who a grant may name."""

    def __init__(
        self,
        *,
        controls: ResourceControlService,
        users: UserRepository,
        teams: TeamRepository,
    ) -> None:
        self._controls = controls
        self.grantees = GranteeDirectory(users, teams)

    async def panel(self, actor: Principal, key: ResourceKey, name: str, short: str) -> AccessPanel:
        """Return the panel for the object; PermissionDenied for anyone but an administrator."""
        control: ControlView | None = await self._controls.get(actor, key)
        return await build_access_panel(self, key, name[:MAX_NAME], short, control)


class _SwarmObjectQueries:
    """Reads configs or secrets through the authoriser; nothing here changes an object."""

    kind: ObjectKind

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        hosts: HostClients,
        locks: ObjectLockRegistry,
        controls_read: AccessPanelReader,
        poke: Callable[[int], None],
    ) -> None:
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = hosts
        self._locks = locks
        self._controls_read = controls_read
        self._poke = poke

    async def page(
        self,
        actor: Principal,
        environment_id: int,
        filters: SwarmObjectFilters | None = None,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> SwarmObjectPage:
        """Return one page of the objects the caller may see, read live from the manager.

        ResourceNotFound unless the environment is a Swarm manager the caller may see;
        ValidationFailed for a bad filter, limit or cursor; EnvironmentUnavailable and
        SwarmObjectListTooLarge when the host cannot be read in full.
        """
        criteria = _compile(filters or SwarmObjectFilters())
        size = check_limit(limit)
        position = decode_position(cursor)
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        client = await self._hosts.client(environment_id)
        listed = await self._list(client, environment_id)
        entries = [
            e for e in (_entry_of(o, self.kind, environment_id) for o in listed) if e is not None
        ]
        permitted = {
            ref.resource_id
            for ref in await self._authoriser.filter_visible(actor, [e.ref for e in entries])
        }
        visible = [e for e in entries if e.swarm_object.id in permitted]
        ordered = sorted((e for e in visible if _matches(e, criteria)), key=_order)
        remaining = (
            ordered
            if position is None
            else [e for e in ordered if _order(e)[:2] > (position.sort_value, position.row_id)]
        )
        window = remaining[:size]
        held = await self._authoriser.filter_visible(actor, [e.ref for e in window], "write")
        writable = frozenset(ref.resource_id for ref in held)
        next_cursor = None
        if len(remaining) > size and window:
            last = _order(window[-1])
            next_cursor = encode_cursor(CursorPosition(last[0], last[1]))
        return SwarmObjectPage(
            page=Page([self._row_of(environment_id, e, writable) for e in window], next_cursor),
            total=len(ordered),
            visible=len(visible),
            limit=size,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            can_create=await self._authoriser.may_create(actor, environment_id),
        )

    async def names(
        self, actor: Principal, environment_id: int, prefix: str, *, limit: int
    ) -> list[str]:
        """Return the first `limit` names, by name, of visible objects starting with `prefix`.

        Case is ignored. ResourceNotFound unless the environment is a Swarm manager the caller
        may see; one manager read, EnvironmentUnavailable when it cannot be read.
        """
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        client = await self._hosts.client(environment_id)
        wanted = prefix.casefold()
        entries = [
            e
            for e in (
                _entry_of(o, self.kind, environment_id)
                for o in await self._list(client, environment_id)
            )
            if e is not None and e.swarm_object.name.casefold().startswith(wanted)
        ]
        permitted = {
            ref.resource_id
            for ref in await self._authoriser.filter_visible(actor, [e.ref for e in entries])
        }
        visible = sorted((e for e in entries if e.swarm_object.id in permitted), key=_order)
        return [e.swarm_object.name for e in visible[:limit]]

    async def create_context(self, actor: Principal, environment_id: int) -> ObjectFormContext:
        """Describe the create form; ResourceNotFound or PermissionDenied for a non-creator."""
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        await self._authoriser.require_create(actor, environment_id)
        limit = MAX_CONFIG_BYTES if self.kind == "config" else MAX_SECRET_BYTES
        return ObjectFormContext(environment_id, environment.name, self.kind, limit)

    async def remove_impact(
        self, actor: Principal, environment_id: int, object_id: str
    ) -> ObjectRemoveImpact:
        """Name the services that reference the object; write holders only."""
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        await self._authorise(actor, environment_id, object_id, "write")
        swarm_object = await self._fetch(environment_id, object_id)
        client = await self._hosts.client(environment_id)
        services = await read_services(client, environment_id, self._poke)
        using = _referencing(self.kind, services, swarm_object.id)
        permitted = await permitted_service_ids(self._authoriser, actor, environment_id, using)
        named, more, hidden = describe_service_users(using, permitted)
        return ObjectRemoveImpact(
            environment_id=environment_id,
            environment_name=environment.name,
            kind=self.kind,
            id=swarm_object.id,
            short_id=short_id(swarm_object.id),
            name=swarm_object.name[:MAX_NAME],
            in_use=len(using),
            services=named,
            services_more=more,
            services_hidden=hidden,
        )

    async def access_panel(
        self, actor: Principal, environment_id: int, object_id: str
    ) -> AccessPanel:
        """Return the access tab's content; administrators only."""
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        await self._authorise(actor, environment_id, object_id, "read")
        swarm_object = await self._fetch(environment_id, object_id)
        if not actor.is_administrator:
            raise PermissionDenied("administrator role required")
        return await self._controls_read.panel(
            actor,
            ResourceKey(environment_id, self.kind, swarm_object.id),
            swarm_object.name,
            short_id(swarm_object.id),
        )

    def describe_busy(self, busy: ObjectBusy, viewer: Principal) -> BusyView:
        """Describe the lock holder to a viewer: names only for administrators."""
        return busy_view(busy, viewer, self._locks.now())

    async def origin_name(self, actor: Principal, environment_id: int, object_id: str) -> str:
        """Return the name of an object the caller may read; ResourceNotFound otherwise."""
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        await self._authorise(actor, environment_id, object_id, "read")
        return (await self._fetch(environment_id, object_id)).name[:MAX_NAME]

    async def _authorise(
        self, actor: Principal, environment_id: int, object_id: str, level: Literal["read", "write"]
    ) -> None:
        if not is_canonical_id(self.kind, object_id):
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, self.kind, object_id), level
        )

    async def _fetch(self, environment_id: int, object_id: str) -> SwarmConfig | SwarmSecret:
        if self.kind == "config":
            return (await self._inspector.config(environment_id, object_id)).config
        return await self._inspector.secret(environment_id, object_id)

    async def _list(
        self, client: EngineClient, environment_id: int
    ) -> Sequence[SwarmConfig | SwarmSecret]:
        if self.kind == "config":
            return await read_configs(client, environment_id, self._poke)
        return await read_secrets(client, environment_id, self._poke)

    def _row_of(
        self, environment_id: int, entry: _Entry, writable: frozenset[str]
    ) -> SwarmObjectRow:
        swarm_object = entry.swarm_object
        return SwarmObjectRow(
            environment_id=environment_id,
            kind=self.kind,
            id=swarm_object.id,
            short_id=short_id(swarm_object.id),
            name=swarm_object.name[:MAX_NAME],
            created_at=parse_engine_time(swarm_object.created_at),
            updated_at=parse_engine_time(swarm_object.updated_at),
            stack=stack_name(swarm_object.labels),
            can_remove=swarm_object.id in writable,
        )

    async def _detail_fields(
        self,
        actor: Principal,
        environment_id: int,
        object_id: str,
        swarm_object: SwarmConfig | SwarmSecret,
    ) -> dict[str, Any]:
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        usage_known = True
        using: list[SwarmService] = []
        try:
            services = await read_services(client, environment_id, self._poke)
        except ServiceListTooLarge:
            usage_known = False
        else:
            using = _referencing(self.kind, services, object_id)
        permitted = await permitted_service_ids(self._authoriser, actor, environment_id, using)
        named, more, hidden = describe_service_users(using, permitted)
        ref = ResourceRef(environment_id, self.kind, object_id, swarm_object.labels)
        writable = bool(await self._authoriser.filter_visible(actor, [ref], "write"))
        holder = self._locks.holder_of(ObjectKey(environment_id, self.kind, object_id))
        return {
            "environment_id": environment_id,
            "environment_name": environment.name,
            "is_local": environment.connection_kind == "unix_socket",
            "kind": self.kind,
            "id": object_id,
            "short_id": short_id(object_id),
            "name": swarm_object.name[:MAX_NAME],
            "created_at": parse_engine_time(swarm_object.created_at),
            "updated_at": parse_engine_time(swarm_object.updated_at),
            "stack": stack_name(swarm_object.labels),
            "labels": label_views(swarm_object.labels, actor.is_administrator),
            "labels_truncated": swarm_object.labels_truncated,
            "services": named,
            "services_more": more,
            "services_hidden": hidden,
            "usage_known": usage_known,
            "in_use": len(using),
            "busy": None
            if holder is None
            else busy_view(ObjectBusy(holder), actor, self._locks.now()),
            "can_remove": writable,
            "can_manage_access": actor.is_administrator,
        }


class SwarmConfigQueries(_SwarmObjectQueries):
    """Reads configs; the content is shown to anyone who may read the config."""

    kind: ObjectKind = "config"

    async def detail(self, actor: Principal, environment_id: int, object_id: str) -> ConfigDetail:
        """Return one config for the detail page; ResourceNotFound unless the caller may see it."""
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        await self._authorise(actor, environment_id, object_id, "read")
        document = await self._inspector.config(environment_id, object_id)
        fields = await self._detail_fields(actor, environment_id, object_id, document.config)
        return ConfigDetail(
            **fields, templated=document.config.templated, content=_content_of(document.data)
        )


class SwarmSecretQueries(_SwarmObjectQueries):
    """Reads secrets; no call returns a value."""

    kind: ObjectKind = "secret"

    async def detail(self, actor: Principal, environment_id: int, object_id: str) -> SecretDetail:
        """Return one secret for the detail page; ResourceNotFound unless the caller may see it."""
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        await self._authorise(actor, environment_id, object_id, "read")
        secret = await self._inspector.secret(environment_id, object_id)
        fields = await self._detail_fields(actor, environment_id, object_id, secret)
        return SecretDetail(**fields, has_driver=secret.has_driver)


class _SwarmObjectActions:
    """Creates and removes configs or secrets, each one audited operation."""

    kind: ObjectKind

    def __init__(
        self,
        *,
        db: Session,
        database: Database,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        hosts: HostClients,
        locks: ObjectLockRegistry,
        controls: ResourceControlService,
        activity: ActivityRecorder,
        clock: Clock,
        poke: Callable[[int], None],
    ) -> None:
        self._db = db
        self._database = database
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = hosts
        self._locks = locks
        self._controls = controls
        self._activity = activity
        self._clock = clock
        self._poke = poke
        self._background: set[asyncio.Future[None]] = set()

    @audited(action=_created_action, target_type=_target_type, environment_id=environment_argument)
    async def create(
        self,
        actor: Principal,
        environment_id: int,
        form: Mapping[str, str],
        *,
        upload: bytes | None = None,
    ) -> SwarmObjectCreated:
        """Create a config or secret from a submitted form; the caller owns it afterwards.

        `upload` holds the bytes of a chosen file. ResourceNotFound unless the environment is a
        Swarm manager the caller may see; PermissionDenied on a local socket for a standard
        user; SwarmObjectInvalid keyed by form field, whose messages never quote the input;
        SwarmObjectNameInUse; SwarmObjectRefused when Docker rejects it; EnvironmentUnavailable.
        """
        audit = current_audit()
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        await self._authoriser.require_create(actor, environment_id)
        request = self._validated(form, upload)
        audit.detail.update(name=request.name, labels=len(request.labels))
        if request.size is not None:
            audit.detail["size"] = request.size
        client = await audited_client(self._hosts, environment_id)
        run = _CreateRun()
        try:
            object_id = await run_shielded(
                partial(self._create_on_host, client, request, run),
                partial(self._interrupted_create, actor, environment_id, run),
            )
        except EngineError as error:
            raise self._create_failure(error, environment_id) from None
        audit.target_id = object_id
        control_created = await self._claim(actor, environment_id, object_id)
        return SwarmObjectCreated(
            environment_id, self.kind, object_id, request.name, control_created
        )

    @audited(
        action=_removed_action,
        target_type=_target_type,
        target_id=_object_argument,
        environment_id=environment_argument,
    )
    async def remove(
        self,
        actor: Principal,
        environment_id: int,
        object_id: str,
        *,
        expected_name: str,
        expected_in_use: int,
    ) -> ObjectRemoved:
        """Remove an object the caller reviewed, then release its control.

        `expected_name` and `expected_in_use` are what the confirmation showed; either differing
        refuses the removal. A config or secret that any service references is refused.
        SwarmObjectInUse names the services the caller may see. The control is released in the
        transaction that commits the success row, and only after the Engine removed the object.
        """
        audit = current_audit()
        if not is_canonical_id(self.kind, object_id):
            raise ResourceNotFound("not found")
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        try:
            await self._authoriser.require_access(
                actor, ResourceKey(environment_id, self.kind, object_id), "write"
            )
        except (EnvironmentUnavailable, ObjectTooLarge):
            audit.refuse()
            raise
        swarm_object, client = await self._load(environment_id, object_id)
        audit.detail["name"] = swarm_object.name[:MAX_NAME]
        holder = LockHolder(UserActor.of(actor), actor.username, "remove")
        async with hold_all(self._locks, [ObjectKey(environment_id, self.kind, object_id)], holder):
            self._forget(environment_id, object_id)
            swarm_object, client = await self._load(environment_id, object_id)
            if expected_name != swarm_object.name:
                audit.reason = "changed_since_review"
                raise SwarmObjectChangedSinceReview(self.kind)
            using = await self._using(client, environment_id, object_id)
            if len(using) != expected_in_use:
                audit.reason = "changed_since_review"
                raise SwarmObjectChangedSinceReview(self.kind)
            if using:
                audit.reason = f"{self.kind}_in_use"
                raise await self._in_use(actor, environment_id, using)
            try:
                await run_shielded(
                    partial(self._remove_on_host, client, object_id),
                    partial(self._interrupted_remove, actor, environment_id, object_id),
                )
            except EngineError as error:
                raise await self._remove_failure(
                    actor, error, client, environment_id, object_id
                ) from None
            released = await self._release(actor, environment_id, object_id)
        return ObjectRemoved(
            environment_id, self.kind, object_id, swarm_object.name[:MAX_NAME], released
        )

    def describe_busy(self, busy: ObjectBusy, viewer: Principal) -> BusyView:
        """Describe the lock holder to a viewer: names only for administrators."""
        return busy_view(busy, viewer, self._locks.now())

    def _validated(self, form: Mapping[str, str], upload: bytes | None) -> ObjectRequest:
        audit = current_audit()
        try:
            return parse_object_form(self.kind, form, upload)
        except SwarmObjectInvalid as invalid:
            audit.reason = "too_large" if invalid.too_large else "invalid"
            audit.detail["fields"] = ",".join(sorted(invalid.fields))
            raise

    async def _create_on_host(
        self, client: EngineClient, request: ObjectRequest, run: _CreateRun
    ) -> str:
        data = request.value.reveal()
        if request.kind == "config":
            object_id = await client.swarm_configs.create(
                name=request.name, data=data, labels=dict(request.labels)
            )
        else:
            object_id = await client.swarm_secrets.create(
                name=request.name, data=data, labels=dict(request.labels)
            )
        run.object_id = object_id
        return object_id

    async def _remove_on_host(self, client: EngineClient, object_id: str) -> None:
        if self.kind == "config":
            await client.swarm_configs.remove(object_id)
        else:
            await client.swarm_secrets.remove(object_id)

    def _create_failure(self, error: EngineError, environment_id: int) -> ProductError:
        audit = current_audit()
        logger.warning(
            "swarm_object_create_refused",
            extra={"kind": self.kind, "error": type(error).__name__, "status": error.status_code},
        )
        if isinstance(error, Conflict):
            audit.reason = "name_in_use"
            return SwarmObjectNameInUse(self.kind)
        if isinstance(error, RequestOutcomeUnknown) or (
            not isinstance(error, EnvironmentUnreachable)
            and (error.status_code is None or error.status_code < 400)
        ):
            audit.reason = "outcome_unknown"
            return EnvironmentUnavailable("outcome_unknown")
        if isinstance(error, EnvironmentUnreachable):
            audit.reason = "unreachable"
            return EnvironmentUnavailable("unreachable")
        unavailable = swarm_unavailable_from(error, environment_id, self._poke)
        if isinstance(unavailable, SwarmUnavailable):
            audit.reason = "not_swarm_manager"
            return unavailable
        audit.reason = "engine_rejected"
        return SwarmObjectRefused(self.kind, engine_detail(error))

    async def _load(
        self, environment_id: int, object_id: str
    ) -> tuple[SwarmConfig | SwarmSecret, EngineClient]:
        audit = current_audit()
        try:
            if self.kind == "config":
                swarm_object: SwarmConfig | SwarmSecret = (
                    await self._inspector.config(environment_id, object_id)
                ).config
            else:
                swarm_object = await self._inspector.secret(environment_id, object_id)
            client = await self._hosts.client(environment_id)
        except EnvironmentUnavailable as error:
            audit.reason = error.kind
            raise
        except ObjectTooLarge:
            audit.reason = "too_large"
            raise
        except ResourceNotFound:
            audit.reason = f"{self.kind}_gone"
            await self._release_gone(environment_id, object_id)
            raise SwarmObjectGone(self.kind) from None
        return swarm_object, client

    def _forget(self, environment_id: int, object_id: str) -> None:
        if self.kind == "config":
            self._inspector.forget_config(environment_id, object_id)
        else:
            self._inspector.forget_secret(environment_id, object_id)

    async def _using(
        self, client: EngineClient, environment_id: int, object_id: str
    ) -> list[SwarmService]:
        services = await audited_list_read(read_services(client, environment_id, self._poke))
        return _referencing(self.kind, services, object_id)

    async def _in_use(
        self, actor: Principal, environment_id: int, using: Sequence[SwarmService]
    ) -> SwarmObjectInUse:
        permitted = await permitted_service_ids(self._authoriser, actor, environment_id, using)
        named, more, hidden = describe_service_users(using, permitted)
        return SwarmObjectInUse(self.kind, len(using), named, more, hidden)

    async def _remove_failure(
        self,
        actor: Principal,
        error: EngineError,
        client: EngineClient,
        environment_id: int,
        object_id: str,
    ) -> ProductError:
        audit = current_audit()
        logger.warning(
            "swarm_object_remove_refused",
            extra={"kind": self.kind, "error": type(error).__name__, "status": error.status_code},
        )
        if isinstance(error, NotFound):
            audit.reason = f"{self.kind}_gone"
            await self._release_gone(environment_id, object_id)
            return SwarmObjectGone(self.kind)
        if isinstance(error, RequestOutcomeUnknown):
            audit.reason = "outcome_unknown"
            return EnvironmentUnavailable("outcome_unknown")
        if isinstance(error, EnvironmentUnreachable):
            audit.reason = "unreachable"
            return EnvironmentUnavailable("unreachable")
        unavailable = swarm_unavailable_from(error, environment_id, self._poke)
        if isinstance(unavailable, SwarmUnavailable):
            audit.reason = "not_swarm_manager"
            return unavailable
        try:
            using = await self._using(client, environment_id, object_id)
        except ProductError:
            using = []
        if using:
            audit.reason = f"{self.kind}_in_use"
            return await self._in_use(actor, environment_id, using)
        audit.reason = "engine_rejected"
        return EngineRefused(engine_detail(error))

    async def _claim(self, actor: Principal, environment_id: int, object_id: str) -> bool:
        audit = current_audit()
        key = ResourceKey(environment_id, self.kind, object_id)

        def note(created: bool) -> None:
            audit.detail["control_created"] = created

        try:
            return await retry_busy(
                partial(
                    in_transaction,
                    self._db,
                    partial(self._controls.claim, key, actor.user_id),
                    None,
                    note,
                )
            )
        except asyncio.CancelledError:
            await self._write_settled(
                actor,
                self._created_name,
                environment_id,
                object_id,
                {**audit.detail, "interrupted": True},
                claim=True,
            )
            raise
        except Exception as error:
            self._log_control_failure("swarm_object_control_claim_failed", key, error)
            audit.detail["control_created"] = False
            return False

    async def _release(self, actor: Principal, environment_id: int, object_id: str) -> bool:
        audit = current_audit()
        key = ResourceKey(environment_id, self.kind, object_id)

        def note(_: bool) -> None:
            audit.detail["control_released"] = True

        try:
            await retry_busy(
                partial(in_transaction, self._db, partial(self._controls.release, key), None, note)
            )
        except asyncio.CancelledError:
            await self._write_settled(
                actor,
                self._removed_name,
                environment_id,
                object_id,
                {**audit.detail, "interrupted": True},
                claim=False,
            )
            raise
        except Exception as error:
            self._log_control_failure("swarm_object_control_release_failed", key, error)
            audit.detail["control_released"] = False
            return False
        return True

    async def _release_gone(self, environment_id: int, object_id: str) -> None:
        audit = current_audit()
        key = ResourceKey(environment_id, self.kind, object_id)
        try:
            async with self._database.session() as db:
                recorder = ActivityRecorder(db, self._clock)
                controls = ResourceControlService(
                    db=db, controls=ResourceControlRepository(db, self._clock), activity=recorder
                )
                released = await retry_busy(
                    partial(in_transaction, db, partial(controls.release, key))
                )
        except Exception as error:
            self._log_control_failure("swarm_object_control_release_failed", key, error)
            audit.detail["control_released"] = False
        else:
            audit.detail["control_released"] = released

    @property
    def _created_name(self) -> str:
        return f"{self.kind}_created"

    @property
    def _removed_name(self) -> str:
        return f"{self.kind}_removed"

    async def _interrupted_create(
        self,
        actor: Principal,
        environment_id: int,
        run: _CreateRun,
        task: asyncio.Future[str],
    ) -> None:
        audit = current_audit()
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, SETTLE_SECONDS)
            detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
            action = self._created_name
            if run.object_id is not None:
                await self._write_settled(
                    actor, action, environment_id, run.object_id, detail, claim=True
                )
            elif not finished:
                task.add_done_callback(
                    partial(self._settle_late_create, actor, environment_id, run, dict(detail))
                )
                await self._write_settled(
                    actor, action, environment_id, None, detail, failure="outcome_unknown"
                )
            else:
                await self._write_settled(
                    actor,
                    action,
                    environment_id,
                    None,
                    detail,
                    failure=failure_reason(error) if error is not None else "interrupted",
                )

    def _settle_late_create(
        self,
        actor: Principal,
        environment_id: int,
        run: _CreateRun,
        detail: dict[str, DetailValue],
        task: asyncio.Future[str],
    ) -> None:
        if task.cancelled() or task.exception() is not None or run.object_id is None:
            return
        future = asyncio.ensure_future(
            self._write_settled(
                actor,
                self._created_name,
                environment_id,
                run.object_id,
                {**detail, "settled_late": True},
                claim=True,
            )
        )
        self._background.add(future)
        future.add_done_callback(self._background.discard)

    async def _interrupted_remove(
        self,
        actor: Principal,
        environment_id: int,
        object_id: str,
        task: asyncio.Future[None],
    ) -> None:
        audit = current_audit()
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, SETTLE_SECONDS)
            detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
            if finished and error is None:
                await self._write_settled(
                    actor, self._removed_name, environment_id, object_id, detail, claim=False
                )
                return
            await self._write_settled(
                actor,
                self._removed_name,
                environment_id,
                object_id,
                detail,
                failure=failure_reason(error) if error is not None else "outcome_unknown",
            )

    async def _write_settled(
        self,
        actor: Principal,
        action: str,
        environment_id: int,
        target_id: str | None,
        detail: dict[str, DetailValue],
        *,
        claim: bool | None = None,
        failure: str | None = None,
    ) -> None:
        steps = (
            ()
            if claim is None or target_id is None
            else (self._control_step(actor, environment_id, target_id, detail, claim),)
        )
        await write_settled_row(
            self._database,
            self._clock,
            actor,
            action=action,
            target_type=self.kind,
            target_id=target_id,
            environment_id=environment_id,
            detail=detail,
            failure=failure,
            steps=steps,
        )

    def _control_step(
        self,
        actor: Principal,
        environment_id: int,
        object_id: str,
        detail: dict[str, DetailValue],
        claim: bool,
    ) -> SettleStep:
        key = ResourceKey(environment_id, self.kind, object_id)
        field_name = "control_created" if claim else "control_released"

        def work(db: Session, recorder: ActivityRecorder) -> Callable[[], Awaitable[bool]]:
            controls = ResourceControlService(
                db=db, controls=ResourceControlRepository(db, self._clock), activity=recorder
            )
            if claim:
                return partial(controls.claim, key, actor.user_id)
            return partial(controls.release, key)

        def note(result: bool) -> None:
            detail[field_name] = result if claim else True

        def failed(error: Exception) -> None:
            self._log_control_failure("swarm_object_control_settle_failed", key, error)
            detail[field_name] = False

        return SettleStep(work, note, failed)

    def _log_control_failure(self, event: str, key: ResourceKey, error: Exception) -> None:
        logger.error(
            event,
            extra={
                "environment_id": key.environment_id,
                "kind": key.resource_type,
                "object_id": key.resource_id,
                "error": type(error).__name__,
            },
        )


class SwarmConfigActions(_SwarmObjectActions):
    """Create and remove configs."""

    kind: ObjectKind = "config"


class SwarmSecretActions(_SwarmObjectActions):
    """Create and remove secrets; the value is never stored, logged or returned."""

    kind: ObjectKind = "secret"
