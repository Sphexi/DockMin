"""Container queries: the paged list, detail, filtered counts, and the access views."""

import asyncio
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final, Literal, get_args

from app.auth.errors import PermissionDenied, ProductError, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import (
    MAX_PAGE_SIZE,
    CursorPosition,
    InvalidCursorError,
    Page,
    decode_cursor,
    encode_cursor,
)
from app.db.repositories.team import TeamRepository
from app.db.repositories.user import UserRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, ResponseTooLarge
from app.docker_engine.models import (
    ContainerInspect,
    ContainerState,
    ContainerSummary,
    EndpointSettings,
    PortBinding,
)
from app.docker_engine.resources import CONTAINER_LIST_BYTES
from app.logging import DEFAULT_SCRUBBER, REDACTED
from app.services.access import (
    COMPOSE_PROJECT_LABEL,
    SWARM_NAMESPACE_LABEL,
    ResourceKey,
    ResourceRef,
    canonical_stack_key,
    is_canonical_id,
    stack_of,
)
from app.services.authorisation import Authoriser
from app.services.effective_access import EffectiveAccess, EffectiveAccessService
from app.services.grantees import GranteeDirectory, GranteeNames, HasGrantees, names_for
from app.services.object_inspector import ContainerTooLarge as ContainerTooLarge
from app.services.object_inspector import (
    EngineObjectInspector,
    ObjectTooLarge,
    unavailable_from,
)
from app.services.resource_controls import ControlView, ResourceControlService, control_digest

DEFAULT_PAGE_SIZE = 25
MAX_NAME_FILTER = 64
MAX_IMAGE_FILTER = 128
MAX_STACK_FILTER = 128
MAX_LABEL_FILTERS = 5
MAX_LABEL_FILTER_LENGTH = 256
MAX_USERNAME_LENGTH = 128
MAX_PORTS_SHOWN = 20
MAX_NAMES_SHOWN = 10
MAX_DETAIL_ROWS = 500
HEALTH_OUTPUT_SHOWN = 1000
START_ERROR_READ = 4096
START_ERROR_SHOWN = 300
MAX_COMMAND_CHARS = 2048
MAX_RESOLVED_NETWORKS = 16
ROW_ID_HEX_DIGITS = 15

CONTAINER_STATES: tuple[str, ...] = (
    "created",
    "running",
    "paused",
    "restarting",
    "removing",
    "exited",
    "dead",
)
HEALTH_FILTERS: tuple[str, ...] = ("unhealthy", "starting")
STOPPED_FILTER = "stopped"
STOPPED_STATES = frozenset({"exited", "created", "dead"})

ContainerAction = Literal["start", "stop", "restart", "kill", "pause", "unpause", "remove"]
CONTAINER_ACTIONS: tuple[ContainerAction, ...] = get_args(ContainerAction)
SELF_REFUSED_ACTIONS: Final = frozenset({"stop", "restart", "kill", "pause", "remove"})

_OFFERED_IN: Mapping[ContainerAction, frozenset[str] | None] = {
    "start": frozenset({"created", "exited"}),
    "stop": frozenset({"running", "restarting", "paused"}),
    "restart": frozenset({"running", "exited", "paused"}),
    "kill": frozenset({"running", "paused", "restarting"}),
    "pause": frozenset({"running"}),
    "unpause": frozenset({"paused"}),
    "remove": None,
}
_WILDCARD_ADDRESSES = frozenset({"", "0.0.0.0", "::"})
_PORT_KEY = re.compile(r"(\d{1,5})/([A-Za-z0-9]+)")
_ENGINE_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})")
_EPOCH = datetime.fromtimestamp(0, UTC)
_ZERO_YEAR = "0001-01-01"


class ContainerListTooLarge(ObjectTooLarge):
    """The host's container list is larger than the read limit; the message names the limit."""

    def __init__(self) -> None:
        limit = CONTAINER_LIST_BYTES // (1024 * 1024)
        super().__init__(
            f"This host reports more container data than can be listed here (limit {limit} MiB)."
        )


@dataclass(frozen=True, slots=True)
class ContainerFilters:
    """The list filters as submitted; validated before any Engine call."""

    state: str | None = None
    health: str | None = None
    q: str | None = None
    image: str | None = None
    labels: Sequence[str] = ()
    stack: str | None = None


@dataclass(frozen=True, slots=True)
class PortView:
    """One published or exposed port; ip is None when bound to every interface."""

    private_port: int
    public_port: int | None
    protocol: str
    ip: str | None = None

    @property
    def text(self) -> str:
        """Return the port the way a person writes it, such as 0.0.0.0:80->8080/tcp."""
        if self.public_port is None:
            return f"{self.private_port}/{self.protocol}"
        host = "" if self.ip is None else f"{self.ip}:"
        return f"{host}{self.public_port}->{self.private_port}/{self.protocol}"


@dataclass(frozen=True, slots=True)
class ContainerRow:
    """One container of the list.

    labels holds what the viewer may see: values of sensitive-named keys are redacted for a
    standard user. labels_truncated is true when labels were dropped to fit the per-container
    budget. actions is empty unless the viewer holds write access. is_self marks the container
    dockmin runs in, whose destructive actions are withheld.
    """

    environment_id: int
    id: str
    short_id: str
    name: str
    names: tuple[str, ...]
    image: str
    state: str
    health: str | None
    created_at: datetime
    ports: tuple[PortView, ...]
    ports_hidden: int
    stack: str | None
    labels: Mapping[str, str]
    labels_truncated: bool
    can_write: bool
    actions: tuple[ContainerAction, ...]
    is_self: bool = False


@dataclass(frozen=True, slots=True)
class ContainerPage:
    """A page of rows, how many containers match the filters in all, and the page size used.

    health_unavailable is true when the health reads failed but the list itself was read.
    """

    page: Page[ContainerRow]
    total: int
    limit: int
    health_unavailable: bool = False


@dataclass(frozen=True, slots=True)
class ContainerBrief:
    """What the environment overview shows of one container the caller may see.

    stack is the name as the labels spell it; compose_key and swarm_key are the canonical keys of
    the Compose project and the Swarm namespace the container belongs to, either of which may be
    absent. status is the Engine's own status text.
    """

    id: str
    name: str
    image: str
    state: str
    status: str
    health: str | None
    stack: str | None
    compose_key: str | None
    swarm_key: str | None


@dataclass(frozen=True, slots=True)
class ContainerListing:
    """Every container the caller may see, by name, from one host read."""

    containers: tuple[ContainerBrief, ...]
    health_unavailable: bool = False


@dataclass(frozen=True, slots=True)
class LabelView:
    """A label as shown; redacted means the value was withheld from this viewer."""

    key: str
    value: str
    redacted: bool


@dataclass(frozen=True, slots=True)
class MountView:
    """A mount of a container; source is set only for a bind mount viewed by an administrator."""

    type: str
    destination: str
    volume_name: str | None
    read_write: bool
    source: str | None


@dataclass(frozen=True, slots=True)
class NetworkAttachment:
    """A container's attachment to one network."""

    name: str
    ip_address: str
    gateway: str
    mac_address: str
    aliases: tuple[str, ...]
    network_id: str = ""
    ipv6_address: str = ""


@dataclass(frozen=True, slots=True)
class EnvVar:
    """An environment variable of the container; redacted when its name designates a secret."""

    key: str
    value: str
    redacted: bool


@dataclass(frozen=True, slots=True)
class AdminFacts:
    """Configuration that discloses how the container is isolated; administrators only."""

    privileged: bool
    cap_add: tuple[str, ...]
    network_mode: str
    pid_mode: str


@dataclass(frozen=True, slots=True)
class HealthView:
    """A container's health check as the Overview shows it.

    configured is false when the container has no health check; every other field is then empty.
    last_output is None when the viewer is not an administrator (output_restricted is then true)
    and an empty string when the last check printed nothing.
    """

    configured: bool = False
    status: str | None = None
    failing_streak: int = 0
    last_check_at: datetime | None = None
    last_exit_code: int | None = None
    last_output: str | None = None
    output_restricted: bool = False


@dataclass(frozen=True, slots=True)
class ContainerDetail:
    """One container as the detail page and its tabs show it.

    command, entrypoint, variables and admin are None for a standard user, as is every bind
    source path in mounts, and error (has_error still says the Engine recorded one).
    ever_started is false for a container that was created and never ran; its exit_code is then
    not an exit. truncated is true when a list was cut at the display bound;
    labels_truncated when labels were dropped. can_attach is true when the viewer may change the
    container and its network mode lets it join networks. is_self marks the container dockmin
    runs in, whose destructive actions are withheld.
    """

    environment_id: int
    environment_name: str
    is_local: bool
    id: str
    short_id: str
    name: str
    image: str
    state: str
    health_status: str | None
    failing_streak: int
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int
    restart_count: int
    restart_policy: str
    ports: tuple[PortView, ...]
    command: str | None
    entrypoint: str | None
    stack: str | None
    labels: tuple[LabelView, ...]
    mounts: tuple[MountView, ...]
    networks: tuple[NetworkAttachment, ...]
    variables: tuple[EnvVar, ...] | None
    admin: AdminFacts | None
    can_write: bool
    actions: tuple[ContainerAction, ...]
    truncated: bool
    labels_truncated: bool
    can_attach: bool = False
    health: HealthView = HealthView()
    error: str | None = None
    has_error: bool = False
    oom_killed: bool = False
    ever_started: bool = True
    is_self: bool = False

    @property
    def is_running(self) -> bool:
        """Report whether the container is running, which is when a stats stream can open."""
        return self.state == "running"


@dataclass(frozen=True, slots=True)
class AccessPanel:
    """What the access tab renders: the object, its control if any, and who that control names."""

    key: ResourceKey
    name: str
    short_id: str
    control: ControlView | None
    names: GranteeNames
    submitted_digest: str | None = None

    @property
    def digest(self) -> str:
        """Return the digest a save or removal must repeat: the submitted one on a re-render."""
        if self.submitted_digest is not None:
            return self.submitted_digest
        return control_digest(self.control)


async def build_access_panel(
    services: HasGrantees, key: ResourceKey, name: str, short: str, control: ControlView | None
) -> AccessPanel:
    """Assemble a panel, looking up the names of the users and teams the control holds."""
    names = await names_for(
        services,
        user_ids=() if control is None else control.user_ids,
        team_ids=() if control is None else control.team_ids,
        owner_id=None if control is None else control.owner_user_id,
    )
    return AccessPanel(key=key, name=name, short_id=short, control=control, names=names)


@dataclass(frozen=True, slots=True)
class ContainerEffectiveAccess:
    """One user's reach over every container on the host; names maps container id to its name."""

    access: EffectiveAccess
    names: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _Criteria:
    state: str | None
    health: str | None
    name: str | None
    image: str | None
    labels: tuple[tuple[str, str | None], ...]
    stack: str | None

    @property
    def is_empty(self) -> bool:
        return not (
            self.state or self.health or self.name or self.image or self.labels or self.stack
        )


@dataclass(frozen=True, slots=True)
class _Entry:
    summary: ContainerSummary
    name: str
    health: str | None
    ref: ResourceRef


@dataclass(frozen=True, slots=True)
class _Loaded:
    entries: list[_Entry]
    health_unavailable: bool


def offered(action: ContainerAction, state: str) -> bool:
    """Report whether the interface offers the action for a container in this state."""
    states = _OFFERED_IN[action]
    return states is None or state in states


def actions_for(state: str) -> tuple[ContainerAction, ...]:
    """Return the actions offered for a state, in display order."""
    return tuple(action for action in CONTAINER_ACTIONS if offered(action, state))


def marked_self[T: (ContainerRow, ContainerDetail)](item: T) -> T:
    """Return the row or detail of dockmin's own container, without the actions it refuses."""
    return replace(
        item,
        is_self=True,
        actions=tuple(a for a in item.actions if a not in SELF_REFUSED_ACTIONS),
    )


def parse_action(value: str) -> ContainerAction | None:
    """Return the action a path segment names, or None."""
    return next((action for action in CONTAINER_ACTIONS if action == value), None)


def short_id(container_id: str) -> str:
    """Return the 12-character form shown in the interface."""
    return container_id[:12]


def display_name(names: Sequence[str], container_id: str) -> str:
    """Return the container's own name: the first without a link path, slash stripped."""
    stripped = [name.removeprefix("/") for name in names]
    for name in stripped:
        if name and "/" not in name:
            return name
    return next((name for name in stripped if name), short_id(container_id))


def _shown_names(names: Sequence[str]) -> tuple[str, ...]:
    return tuple(name.removeprefix("/") for name in names[:MAX_NAMES_SHOWN] if name)


def redact_label(key: str, value: str) -> tuple[str, bool]:
    """Return a label value as a standard user may see it, and whether it was altered."""
    if DEFAULT_SCRUBBER.is_sensitive_label(key):
        return REDACTED, True
    scrubbed = DEFAULT_SCRUBBER.scrub_text(value)
    return scrubbed, scrubbed != value


def visible_labels(labels: Mapping[str, str], viewer: Principal) -> dict[str, str]:
    """Return the labels, with secrets redacted for a standard user."""
    if viewer.is_administrator:
        return dict(labels)
    return {key: redact_label(key, value)[0] for key, value in labels.items()}


def stack_name(labels: Mapping[str, str]) -> str | None:
    """Return the stack name as the labels spell it, preferring Compose over Swarm."""
    for key in (COMPOSE_PROJECT_LABEL, SWARM_NAMESPACE_LABEL):
        value = labels.get(key)
        if value:
            return value
    return None


def _label_key(labels: Mapping[str, str], label: str) -> str | None:
    value = labels.get(label)
    return canonical_stack_key(value) if value else None


def _readable(text: str, *, keep: str = "") -> str:
    return "".join(
        char for char in text if char in keep or not unicodedata.category(char).startswith("C")
    )


def start_error(state: ContainerState) -> str | None:
    """Return the Engine's start error scrubbed to one line, or None when it recorded none."""
    spaced = "".join(" " if char.isspace() else char for char in state.error[:START_ERROR_READ])
    text = " ".join(DEFAULT_SCRUBBER.scrub_text(_readable(spaced)).split())
    return text[:START_ERROR_SHOWN] or None


def _health_view(state: ContainerState, viewer: Principal) -> HealthView:
    health = state.health
    if health is None:
        return HealthView()
    newest = health.log[-1] if health.log else None
    shown = viewer.is_administrator
    output: str | None = None
    if shown and newest is not None:
        cleaned = _readable(newest.output.replace("\r\n", "\n"), keep="\n\t")
        output = DEFAULT_SCRUBBER.scrub_text(cleaned).strip()[:HEALTH_OUTPUT_SHOWN]
    return HealthView(
        configured=True,
        status=health.status,
        failing_streak=health.failing_streak,
        last_check_at=None if newest is None else parse_engine_time(newest.end),
        last_exit_code=None if newest is None else newest.exit_code,
        last_output=output,
        output_restricted=not shown,
    )


def _ever_started(state: ContainerState) -> bool:
    started = state.started_at.strip()
    return bool(started) and not started.startswith(_ZERO_YEAR)


def parse_engine_time(text: str) -> datetime | None:
    """Parse an Engine timestamp; the zero time and anything unreadable are None."""
    match = _ENGINE_TIME.fullmatch(text.strip())
    if match is None:
        return None
    base, fraction, offset = match.groups()
    micros = (fraction or "0")[:6].ljust(6, "0")
    zone = "+00:00" if offset == "Z" else offset
    try:
        moment = datetime.fromisoformat(f"{base}.{micros}{zone}")
    except ValueError:
        return None
    return None if moment.year <= 1 else moment.astimezone(UTC)


def _from_epoch(seconds: int) -> datetime:
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return _EPOCH


def _row_id(container_id: str) -> int:
    return int(container_id[:ROW_ID_HEX_DIGITS], 16)


def _order(entry: _Entry) -> tuple[str, int, str]:
    return entry.name.casefold(), _row_id(entry.summary.id), entry.summary.id


def _ports_of(bindings: Sequence[PortBinding]) -> tuple[tuple[PortView, ...], int]:
    seen: set[tuple[str, int, int | None, str]] = set()
    views: list[PortView] = []
    for binding in bindings:
        address = "" if binding.ip is None else binding.ip
        ip = None if address in _WILDCARD_ADDRESSES else address
        key = (address if ip else "", binding.private_port, binding.public_port, binding.type)
        if key in seen:
            continue
        seen.add(key)
        views.append(PortView(binding.private_port, binding.public_port, binding.type, ip))
    views.sort(key=lambda p: (p.private_port, p.public_port or 0, p.protocol))
    return tuple(views[:MAX_PORTS_SHOWN]), max(0, len(views) - MAX_PORTS_SHOWN)


def _published(
    specs: Mapping[str, Sequence[tuple[str | None, str | None]] | None],
) -> list[PortBinding]:
    found: list[PortBinding] = []
    for spec, targets in list(specs.items())[:MAX_DETAIL_ROWS]:
        matched = _PORT_KEY.fullmatch(spec)
        if matched is None:
            continue
        private, protocol = int(matched.group(1)), matched.group(2)
        for host_ip, host_port in (targets or [(None, None)])[:MAX_DETAIL_ROWS]:
            public = int(host_port) if host_port is not None and host_port.isdigit() else None
            found.append(
                PortBinding(
                    ip=host_ip or None, private_port=private, public_port=public, type=protocol
                )
            )
    return found[:MAX_DETAIL_ROWS]


def _bindings_of(inspected: ContainerInspect) -> list[PortBinding]:
    published = inspected.network_settings.ports
    if published:
        return _published(
            {
                spec: None if hosts is None else [(h.host_ip, h.host_port) for h in hosts]
                for spec, hosts in published.items()
            }
        )
    configured = inspected.host_config.get("PortBindings")
    if not isinstance(configured, dict):
        return []
    return _published(
        {
            spec: _configured_targets(hosts)
            for spec, hosts in configured.items()
            if isinstance(spec, str)
        }
    )


def _configured_targets(hosts: object) -> list[tuple[str | None, str | None]]:
    if not isinstance(hosts, list):
        return []
    targets: list[tuple[str | None, str | None]] = []
    for target in hosts[:MAX_DETAIL_ROWS]:
        if isinstance(target, dict):
            ip, port = target.get("HostIp"), target.get("HostPort")
            targets.append(
                (ip if isinstance(ip, str) else None, port if isinstance(port, str) else None)
            )
    return targets


def _restart_policy(host_config: Mapping[str, Any]) -> str:
    policy = host_config.get("RestartPolicy")
    if not isinstance(policy, dict):
        return "no"
    name = policy.get("Name")
    if not isinstance(name, str) or not name:
        return "no"
    retries = policy.get("MaximumRetryCount")
    if name == "on-failure" and isinstance(retries, int) and retries > 0:
        return f"on-failure:{retries}"
    return name


def _text(host_config: Mapping[str, Any], key: str) -> str:
    value = host_config.get(key)
    return value if isinstance(value, str) else ""


def _canonical_network_id(network_id: str) -> str:
    return network_id if is_canonical_id("network", network_id) else ""


def joins_networks(network_mode: str) -> bool:
    """Report whether a container with this `HostConfig.NetworkMode` can have endpoints added."""
    return network_mode not in ("host", "none") and not network_mode.startswith("container:")


def _joined(arguments: Sequence[str], viewer: Principal) -> str | None:
    if not viewer.is_administrator:
        return None
    return " ".join(arguments)[:MAX_COMMAND_CHARS]


def _compile(filters: ContainerFilters) -> _Criteria:
    fields: dict[str, str] = {}
    state = _clean(filters.state)
    if state is not None and state != STOPPED_FILTER and state not in CONTAINER_STATES:
        fields["state"] = "Choose one of the listed states."
    health = _clean(filters.health)
    if health is not None and health not in HEALTH_FILTERS:
        fields["health"] = "Choose unhealthy or starting."
    name = _clean(filters.q)
    if name is not None and len(name) > MAX_NAME_FILTER:
        fields["q"] = f"Search for at most {MAX_NAME_FILTER} characters."
    image = _clean(filters.image)
    if image is not None and len(image) > MAX_IMAGE_FILTER:
        fields["image"] = f"Filter on at most {MAX_IMAGE_FILTER} characters."
    stack = _clean(filters.stack)
    if stack is not None and len(stack) > MAX_STACK_FILTER:
        fields["stack"] = f"Filter on at most {MAX_STACK_FILTER} characters."
    labels = _parse_labels(filters.labels, fields)
    if fields:
        raise ValidationFailed(fields)
    return _Criteria(
        state=state,
        health=health,
        name=None if name is None else name.removeprefix("/").casefold(),
        image=None if image is None else image.casefold(),
        labels=labels,
        stack=None if stack is None else canonical_stack_key(stack),
    )


def _clean(value: str | None) -> str | None:
    stripped = None if value is None else value.strip()
    return stripped or None


def _parse_labels(
    submitted: Sequence[str], fields: dict[str, str]
) -> tuple[tuple[str, str | None], ...]:
    wanted = [item for item in (_clean(raw) for raw in submitted) if item is not None]
    if len(wanted) > MAX_LABEL_FILTERS:
        fields["label"] = f"Filter on at most {MAX_LABEL_FILTERS} labels."
        return ()
    parsed: list[tuple[str, str | None]] = []
    for item in wanted:
        if len(item) > MAX_LABEL_FILTER_LENGTH:
            fields["label"] = f"Each label filter is at most {MAX_LABEL_FILTER_LENGTH} characters."
            return ()
        key, separator, value = item.partition("=")
        if not key:
            fields["label"] = "A label filter is a key or key=value."
            return ()
        parsed.append((key, value if separator else None))
    return tuple(parsed)


def _matches(entry: _Entry, criteria: _Criteria) -> bool:
    summary = entry.summary
    if criteria.state == STOPPED_FILTER:
        if summary.state not in STOPPED_STATES:
            return False
    elif criteria.state is not None and summary.state != criteria.state:
        return False
    if criteria.health is not None and entry.health != criteria.health:
        return False
    if criteria.name is not None and not any(
        criteria.name in name.removeprefix("/").casefold() for name in summary.names
    ):
        return False
    if criteria.image is not None and criteria.image not in summary.image.casefold():
        return False
    labels = summary.labels
    for key, value in criteria.labels:
        if key not in labels or (value is not None and labels[key] != value):
            return False
    return criteria.stack is None or stack_of(labels) == criteria.stack


def _decode_position(cursor: str | None) -> CursorPosition | None:
    if cursor is None:
        return None
    try:
        position = decode_cursor(cursor)
    except InvalidCursorError:
        raise ValidationFailed({"cursor": "The page link is not valid."}) from None
    if not isinstance(position.sort_value, str):
        raise ValidationFailed({"cursor": "The page link is not valid."})
    return position


def _check_limit(limit: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValidationFailed({"limit": f"Choose a page size from 1 to {MAX_PAGE_SIZE}."})
    return limit


class ContainerService:
    """Reads containers through the authoriser; nothing here changes a container."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        controls: ResourceControlService,
        effective: EffectiveAccessService,
        users: UserRepository,
        teams: TeamRepository,
    ) -> None:
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._controls = controls
        self._effective = effective
        self._users = users
        self.grantees = GranteeDirectory(users, teams)

    async def page(
        self,
        actor: Principal,
        environment_id: int,
        filters: ContainerFilters | None = None,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> ContainerPage:
        """Return one page of the containers the caller may see, read live from the host.

        EnvironmentUnavailable and ContainerListTooLarge when the host cannot be read;
        ValidationFailed for a bad filter, limit or cursor.
        """
        criteria = _compile(filters or ContainerFilters())
        size = _check_limit(limit)
        position = _decode_position(cursor)
        loaded = await self._load(actor, environment_id, criteria, kinds=HEALTH_FILTERS)
        ordered = sorted(loaded.entries, key=_order)
        remaining = (
            ordered
            if position is None
            else [e for e in ordered if _order(e)[:2] > (position.sort_value, position.row_id)]
        )
        window = remaining[:size]
        has_next = len(remaining) > size
        writable = {
            ref.resource_id
            for ref in await self._authoriser.filter_visible(
                actor, [e.ref for e in window], "write"
            )
        }
        rows = [self._row_of(actor, environment_id, e, e.summary.id in writable) for e in window]
        next_cursor = None
        if has_next and window:
            last = _order(window[-1])
            next_cursor = encode_cursor(CursorPosition(last[0], last[1]))
        return ContainerPage(
            Page(rows, next_cursor),
            total=len(ordered),
            limit=size,
            health_unavailable=loaded.health_unavailable,
        )

    async def names(
        self, actor: Principal, environment_id: int, prefix: str, *, limit: int
    ) -> list[str]:
        """Return the first `limit` names, by name, of visible containers starting with `prefix`.

        Case is ignored. One host read; EnvironmentUnavailable and ContainerListTooLarge when the
        host cannot be read.
        """
        await self._authoriser.require_environment(actor, environment_id)
        client = await self._hosts.client(environment_id)
        try:
            summaries = await client.containers.list(all=True)
        except EngineError as failure:
            raise _listing_failure(failure) from None
        wanted = prefix.casefold()
        named = [(display_name(s.names, s.id), s) for s in summaries]
        refs = {
            s.id: ResourceRef(environment_id, "container", s.id, s.labels)
            for name, s in named
            if name.casefold().startswith(wanted)
        }
        permitted = {
            ref.resource_id
            for ref in await self._authoriser.filter_visible(actor, list(refs.values()))
        }
        return sorted((name for name, s in named if s.id in permitted), key=str.casefold)[:limit]

    async def require_visible(self, actor: Principal, environment_id: int) -> None:
        """Raise ResourceNotFound unless the caller may see the environment."""
        await self._authoriser.require_environment(actor, environment_id)

    async def may_create(self, actor: Principal, environment_id: int) -> bool:
        """Report whether the caller may create containers in the environment."""
        return await self._authoriser.may_create(actor, environment_id)

    async def brief_listing(self, actor: Principal, environment_id: int) -> ContainerListing:
        """List the containers the caller may see, by name; one host read.

        EnvironmentUnavailable and ContainerListTooLarge when the host cannot be read.
        """
        loaded = await self._load(actor, environment_id, _compile(ContainerFilters()))
        return ContainerListing(
            containers=tuple(
                ContainerBrief(
                    id=entry.summary.id,
                    name=entry.name,
                    image=entry.summary.image,
                    state=entry.summary.state,
                    status=entry.summary.status,
                    health=entry.health,
                    stack=stack_name(entry.summary.labels),
                    compose_key=_label_key(entry.summary.labels, COMPOSE_PROJECT_LABEL),
                    swarm_key=_label_key(entry.summary.labels, SWARM_NAMESPACE_LABEL),
                )
                for entry in sorted(loaded.entries, key=_order)
            ),
            health_unavailable=loaded.health_unavailable,
        )

    async def origin_name(self, actor: Principal, environment_id: int, container_id: str) -> str:
        """Return the name of a container the caller may read; ResourceNotFound otherwise."""
        inspected = await self._inspect_readable(actor, environment_id, container_id)
        return display_name([inspected.name], inspected.id)

    async def detail(
        self, actor: Principal, environment_id: int, container_id: str
    ) -> ContainerDetail:
        """Return one container for the detail page; ResourceNotFound if it is not visible."""
        inspected = await self._inspect_readable(actor, environment_id, container_id)
        environment = await self._hosts.environment(environment_id)
        can_write = await self._can_write(actor, environment_id, inspected)
        viewer_is_admin = actor.is_administrator
        labels = inspected.config.labels
        host_config = inspected.host_config
        state = inspected.state
        shown_labels = sorted(labels.items())
        shown_mounts = inspected.mounts[:MAX_DETAIL_ROWS]
        shown_networks = sorted(inspected.network_settings.networks.items())
        env_pairs = inspected.config.env[:MAX_DETAIL_ROWS]
        ports, ports_hidden = _ports_of(_bindings_of(inspected))
        labels_truncated = inspected.labels_truncated or len(shown_labels) > MAX_DETAIL_ROWS
        truncated = (
            labels_truncated
            or ports_hidden > 0
            or len(inspected.mounts) > MAX_DETAIL_ROWS
            or len(shown_networks) > MAX_DETAIL_ROWS
            or len(inspected.config.env) > MAX_DETAIL_ROWS
        )
        return ContainerDetail(
            environment_id=environment_id,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            id=inspected.id,
            short_id=short_id(inspected.id),
            name=display_name([inspected.name], inspected.id),
            image=inspected.config.image or inspected.image,
            state=state.status,
            health_status=None if state.health is None else state.health.status,
            failing_streak=0 if state.health is None else state.health.failing_streak,
            created_at=parse_engine_time(inspected.created),
            started_at=parse_engine_time(state.started_at),
            finished_at=parse_engine_time(state.finished_at),
            exit_code=state.exit_code,
            restart_count=inspected.restart_count,
            restart_policy=_restart_policy(host_config),
            ports=ports,
            command=_joined(inspected.config.cmd, actor),
            entrypoint=_joined(inspected.config.entrypoint, actor),
            stack=stack_name(labels),
            labels=tuple(
                _label_view(key, value, viewer_is_admin)
                for key, value in shown_labels[:MAX_DETAIL_ROWS]
            ),
            mounts=tuple(
                MountView(
                    mount.type,
                    mount.destination,
                    mount.name,
                    mount.read_write,
                    mount.source if viewer_is_admin and mount.type == "bind" else None,
                )
                for mount in shown_mounts
            ),
            networks=await self._attachments(environment_id, shown_networks[:MAX_DETAIL_ROWS]),
            variables=tuple(_variable(pair) for pair in env_pairs) if viewer_is_admin else None,
            admin=(
                AdminFacts(
                    privileged=host_config.get("Privileged") is True,
                    cap_add=_strings(host_config.get("CapAdd")),
                    network_mode=_text(host_config, "NetworkMode"),
                    pid_mode=_text(host_config, "PidMode"),
                )
                if viewer_is_admin
                else None
            ),
            can_write=can_write,
            actions=actions_for(state.status) if can_write else (),
            truncated=truncated,
            labels_truncated=labels_truncated,
            can_attach=can_write and joins_networks(_text(host_config, "NetworkMode")),
            health=_health_view(state, actor),
            error=start_error(state) if viewer_is_admin else None,
            has_error=bool(state.error.strip()),
            oom_killed=state.oom_killed,
            ever_started=_ever_started(state),
        )

    async def _attachments(
        self, environment_id: int, endpoints: Sequence[tuple[str, EndpointSettings]]
    ) -> tuple[NetworkAttachment, ...]:
        unresolved = [name for name, e in endpoints if not e.network_id][:MAX_RESOLVED_NETWORKS]
        ids = dict(
            zip(
                unresolved,
                await asyncio.gather(
                    *(self._network_id_named(environment_id, n) for n in unresolved)
                ),
                strict=True,
            )
        )
        return tuple(
            NetworkAttachment(
                name,
                endpoint.ip_address,
                endpoint.gateway,
                endpoint.mac_address,
                tuple(endpoint.aliases[:MAX_NAMES_SHOWN]),
                _canonical_network_id(endpoint.network_id or ids.get(name, "")),
                endpoint.global_ipv6_address,
            )
            for name, endpoint in endpoints
        )

    async def _network_id_named(self, environment_id: int, name: str) -> str:
        try:
            return (await self._inspector.network_named(environment_id, name)).id
        except ProductError:
            return ""

    async def row(self, actor: Principal, environment_id: int, container_id: str) -> ContainerRow:
        """Return one container as a list row, for swapping in after an action."""
        inspected = await self._inspect_readable(actor, environment_id, container_id)
        can_write = await self._can_write(actor, environment_id, inspected)
        names = [inspected.name]
        state = inspected.state
        health = None if state.health is None else state.health.status
        ports, ports_hidden = _ports_of(_bindings_of(inspected))
        return ContainerRow(
            environment_id=environment_id,
            id=inspected.id,
            short_id=short_id(inspected.id),
            name=display_name(names, inspected.id),
            names=_shown_names(names),
            image=inspected.config.image or inspected.image,
            state=state.status,
            health=health if health in HEALTH_FILTERS else None,
            created_at=parse_engine_time(inspected.created) or _EPOCH,
            ports=ports,
            ports_hidden=ports_hidden,
            stack=stack_name(inspected.config.labels),
            labels=visible_labels(inspected.config.labels, actor),
            labels_truncated=inspected.labels_truncated,
            can_write=can_write,
            actions=actions_for(state.status) if can_write else (),
        )

    async def access_panel(
        self, actor: Principal, environment_id: int, container_id: str
    ) -> AccessPanel:
        """Return the access tab's content; administrators only."""
        inspected = await self._inspect_readable(actor, environment_id, container_id)
        if not actor.is_administrator:
            raise PermissionDenied("administrator role required")
        key = ResourceKey(environment_id, "container", inspected.id)
        control = await self._controls.get(actor, key)
        return await build_access_panel(
            self,
            key,
            display_name([inspected.name], inspected.id),
            short_id(inspected.id),
            control,
        )

    async def effective_access(
        self, actor: Principal, environment_id: int, username: str
    ) -> ContainerEffectiveAccess:
        """Explain one user's reach over every container on the host; administrators only."""
        if not actor.is_administrator:
            await self._authoriser.require_environment(actor, environment_id)
            raise PermissionDenied("administrator role required")
        name = username.strip()
        if not name or len(name) > MAX_USERNAME_LENGTH:
            raise ValidationFailed({"username": "Enter the username of an account."})
        user = await self._users.get_by_username(name)
        if user is None:
            raise ValidationFailed({"username": "No account has that username."})
        loaded = await self._load(actor, environment_id, _compile(ContainerFilters()))
        entries = sorted(loaded.entries, key=_order)
        access = await self._effective.explain(
            actor, user.id, environment_id, "container", [entry.ref for entry in entries]
        )
        return ContainerEffectiveAccess(access, {e.summary.id: e.name for e in entries})

    async def _inspect_readable(
        self, actor: Principal, environment_id: int, container_id: str
    ) -> ContainerInspect:
        key = ResourceKey(environment_id, "container", container_id)
        await self._authoriser.require_access(actor, key, "read")
        return await self._inspector.inspect(environment_id, container_id)

    async def _can_write(
        self, actor: Principal, environment_id: int, inspected: ContainerInspect
    ) -> bool:
        ref = ResourceRef(environment_id, "container", inspected.id, inspected.config.labels)
        return bool(await self._authoriser.filter_visible(actor, [ref], "write"))

    async def _load(
        self,
        actor: Principal,
        environment_id: int,
        criteria: _Criteria,
        *,
        kinds: tuple[str, ...] = ("unhealthy",),
    ) -> _Loaded:
        await self._authoriser.require_environment(actor, environment_id)
        client = await self._hosts.client(environment_id)
        wanted = tuple(dict.fromkeys((*kinds, *([criteria.health] if criteria.health else []))))
        summaries, flagged, health_unavailable = await _read_listing(client, wanted)
        entries = [
            _Entry(
                summary=summary,
                name=display_name(summary.names, summary.id),
                health=next(
                    (kind for kind in HEALTH_FILTERS if summary.id in flagged.get(kind, ())), None
                ),
                ref=ResourceRef(environment_id, "container", summary.id, summary.labels),
            )
            for summary in summaries
        ]
        matched = entries if criteria.is_empty else [e for e in entries if _matches(e, criteria)]
        permitted = {
            ref.resource_id
            for ref in await self._authoriser.filter_visible(actor, [e.ref for e in matched])
        }
        return _Loaded([e for e in matched if e.summary.id in permitted], health_unavailable)

    def _row_of(
        self, actor: Principal, environment_id: int, entry: _Entry, can_write: bool
    ) -> ContainerRow:
        summary = entry.summary
        ports, hidden = _ports_of(summary.ports)
        return ContainerRow(
            environment_id=environment_id,
            id=summary.id,
            short_id=short_id(summary.id),
            name=entry.name,
            names=_shown_names(summary.names),
            image=summary.image,
            state=summary.state,
            health=entry.health,
            created_at=_from_epoch(summary.created),
            ports=ports,
            ports_hidden=hidden,
            stack=stack_name(summary.labels),
            labels=visible_labels(summary.labels, actor),
            labels_truncated=summary.labels_truncated,
            can_write=can_write,
            actions=actions_for(summary.state) if can_write else (),
        )


async def _read_listing(
    client: EngineClient, kinds: Sequence[str]
) -> tuple[list[ContainerSummary], dict[str, frozenset[str]], bool]:
    outcomes: list[Any] = await asyncio.gather(
        client.containers.list(all=True),
        *(client.containers.list_ids(all=True, filters={"health": [kind]}) for kind in kinds),
        return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
            raise outcome
    listing, *health = outcomes
    if isinstance(listing, Exception):
        raise _listing_failure(listing)
    flagged: dict[str, frozenset[str]] = {}
    unavailable = False
    for kind, outcome in zip(kinds, health, strict=True):
        if isinstance(outcome, EngineError):
            unavailable = True
        elif isinstance(outcome, Exception):
            raise outcome
        else:
            flagged[kind] = outcome
    return listing, flagged, unavailable


def _listing_failure(failure: Exception) -> Exception:
    if isinstance(failure, ResponseTooLarge):
        return ContainerListTooLarge()
    if isinstance(failure, EngineError):
        return unavailable_from(failure)
    return failure


def _label_view(key: str, value: str, viewer_is_admin: bool) -> LabelView:
    if viewer_is_admin:
        return LabelView(key, value, False)
    shown, redacted = redact_label(key, value)
    return LabelView(key, shown, redacted)


def _variable(pair: str) -> EnvVar:
    key, _, value = pair.partition("=")
    if DEFAULT_SCRUBBER.is_sensitive(key):
        return EnvVar(key, REDACTED, True)
    return EnvVar(key, value, False)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value[:MAX_DETAIL_ROWS] if isinstance(item, str))
