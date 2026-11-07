"""Shared reads for networks and volumes: bounded host lists, who uses an object, and paging."""

import asyncio
import contextvars
import hashlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Final, Literal

from app.auth.errors import ProductError, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Database, Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.db.repositories.pagination import (
    MAX_PAGE_SIZE,
    CursorPosition,
    InvalidCursorError,
    decode_cursor,
)
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import (
    Conflict,
    EngineError,
    EnvironmentUnreachable,
    NotFound,
    RequestOutcomeUnknown,
    ResponseTooLarge,
)
from app.docker_engine.models import ContainerSummary, Network, SwarmService, Volume
from app.docker_engine.resources import NETWORK_LIST_BYTES, SERVICE_LIST_BYTES, VOLUME_LIST_BYTES
from app.logging import DEFAULT_SCRUBBER, REDACTED, get_logger
from app.services.access import AccessLevel, ResourceRef
from app.services.acting import Acting, actor_of
from app.services.activity import ActivityRecorder, SubsystemActor, UserActor, current_audit
from app.services.authorisation import Authoriser
from app.services.container_actions import BusyView, engine_detail, retrieve_exception
from app.services.containers import LabelView, display_name, redact_label, short_id
from app.services.image_listing import read_containers
from app.services.object_inspector import (
    NETWORK_ID,
    EnvironmentUnavailable,
    HostClients,
    ObjectTooLarge,
    swarm_unavailable_from,
    unavailable_from,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.service_restrictions import references

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PRUNE",
    "MAX_USERS_SHOWN",
    "USAGE_FILTERS",
    "NetworkListTooLarge",
    "ObjectUser",
    "OptionView",
    "PruneStatus",
    "ServiceListTooLarge",
    "SettleStep",
    "VolumeListTooLarge",
    "audited_client",
    "audited_list_read",
    "busy_view",
    "check_limit",
    "create_failure",
    "decode_position",
    "describe_service_users",
    "describe_users",
    "failure_reason",
    "hold_all",
    "in_transaction",
    "label_views",
    "network_users",
    "option_views",
    "permitted_container_ids",
    "permitted_service_ids",
    "read_containers",
    "read_networks",
    "read_services",
    "read_volumes",
    "row_id",
    "run_shielded",
    "service_mode",
    "service_network_users",
    "settled",
    "volume_users",
    "write_settled_row",
]

logger = get_logger(__name__)

DEFAULT_PAGE_SIZE: Final = 25
MAX_USERS_SHOWN: Final = 10
MAX_PRUNE: Final = 100
MAX_DETAIL_ROWS: Final = 500
MAX_NAME_CHARS: Final = 255
MAX_OPTION_CHARS: Final = 4096
MAX_LABEL_VALUE_CHARS: Final = 4096
ROW_ID_HEX_DIGITS: Final = 15
USAGE_FILTERS: Final = ("used", "unused")
_SERVICE_MODES: Final = (
    ("Replicated", "replicated"),
    ("Global", "global"),
    ("ReplicatedJob", "replicated-job"),
    ("GlobalJob", "global-job"),
)

_CIFS_PASSWORD: Final = re.compile(
    r"(?<![\w.-])(?P<head>user(?:name)?\s{0,8}=\s{0,8}[^%\s,;]{0,128})%[^\s,;]*", re.IGNORECASE
)
_USERINFO_PASSWORD: Final = re.compile(
    r"(?<![\w.:+-])(?P<user>[^\s:/@,;]{1,128}):[^\s@,;/]{1,512}(?=@)"
)

type PruneStatus = Literal["removed", "skipped_changed", "skipped_in_use", "busy", "gone", "failed"]


class NetworkListTooLarge(ObjectTooLarge):
    """The host's network list is larger than the read limit; the message names the limit."""

    def __init__(self) -> None:
        limit = NETWORK_LIST_BYTES // (1024 * 1024)
        super().__init__(
            f"This host reports more network data than can be listed here (limit {limit} MiB)."
        )


class VolumeListTooLarge(ObjectTooLarge):
    """The host's volume list is larger than the read limit; the message names the limit."""

    def __init__(self) -> None:
        limit = VOLUME_LIST_BYTES // (1024 * 1024)
        super().__init__(
            f"This host reports more volume data than can be listed here (limit {limit} MiB)."
        )


class ServiceListTooLarge(ObjectTooLarge):
    """The host's service list is larger than the read limit; the message names the limit."""

    def __init__(self) -> None:
        limit = SERVICE_LIST_BYTES // (1024 * 1024)
        super().__init__(
            f"This host reports more service data than can be listed here (limit {limit} MiB)."
        )


@dataclass(frozen=True, slots=True)
class ObjectUser:
    """A container that uses a network or volume, named only when the viewer may see it."""

    id: str
    short_id: str
    name: str
    state: str


@dataclass(frozen=True, slots=True)
class OptionView:
    """A driver option as shown to an administrator; redacted means the value was withheld."""

    key: str
    value: str
    redacted: bool


async def read_networks(client: EngineClient) -> list[Network]:
    """Read every network; NetworkListTooLarge past the limit, EnvironmentUnavailable otherwise."""
    try:
        return await client.networks.list()
    except ResponseTooLarge:
        raise NetworkListTooLarge() from None
    except EngineError as error:
        raise unavailable_from(error) from None


async def read_volumes(client: EngineClient) -> list[Volume]:
    """Read every volume; VolumeListTooLarge past the limit, EnvironmentUnavailable otherwise."""
    try:
        return await client.volumes.list()
    except ResponseTooLarge:
        raise VolumeListTooLarge() from None
    except EngineError as error:
        raise unavailable_from(error) from None


async def read_services(
    client: EngineClient,
    environment_id: int,
    poke: Callable[[int], None] | None = None,
    *,
    filters: Mapping[str, list[str]] | None = None,
) -> list[SwarmService]:
    """Read every service; ServiceListTooLarge past the limit, EnvironmentUnavailable otherwise.

    A 503 from the host is SwarmUnavailable, and asks `poke` for a fresh snapshot of the host.
    """
    try:
        return await client.swarm_services.list(filters=filters)
    except ResponseTooLarge:
        raise ServiceListTooLarge() from None
    except EngineError as error:
        raise swarm_unavailable_from(error, environment_id, poke) from None


def volume_users(containers: Sequence[ContainerSummary]) -> dict[str, list[ContainerSummary]]:
    """Map each volume name to the containers, in any state, that mount it."""
    used: dict[str, list[ContainerSummary]] = {}
    for container in containers:
        names = dict.fromkeys(
            mount.name for mount in container.mounts if mount.type == "volume" and mount.name
        )
        for name in names:
            used.setdefault(name, []).append(container)
    return used


def network_users(
    containers: Sequence[ContainerSummary], networks: Sequence[Network]
) -> dict[str, list[ContainerSummary]]:
    """Map each listed network's id to the containers, in any state, with an endpoint on it."""
    known = {network.id for network in networks}
    by_name = {network.name: network.id for network in networks}
    used: dict[str, list[ContainerSummary]] = {}
    for container in containers:
        for network_id in _network_ids_of(container, known, by_name):
            used.setdefault(network_id, []).append(container)
    return used


def _network_ids_of(
    container: ContainerSummary, known: set[str], by_name: Mapping[str, str]
) -> dict[str, None]:
    """Resolve endpoints to listed ids, by name when the id is empty; NetworkMode is not read."""
    found: dict[str, None] = {}
    for endpoint in container.endpoints:
        if endpoint.network_id:
            if endpoint.network_id in known:
                found[endpoint.network_id] = None
        elif endpoint.name in by_name:
            found[by_name[endpoint.name]] = None
    return found


def service_mode(service: SwarmService) -> str:
    """Name the scheduling mode of a service, or an empty string when its spec names none."""
    for key, name in _SERVICE_MODES:
        if key in service.spec.mode:
            return name
    return ""


def service_network_users(
    services: Sequence[SwarmService], networks: Sequence[Network]
) -> dict[str, list[SwarmService]]:
    """Map each listed network's id to the services that attach to it.

    A service attaches through a task template target, by id or by name, or holds a virtual
    address on the network.
    """
    known = {network.id for network in networks}
    by_name = {network.name: network.id for network in networks}
    used: dict[str, list[SwarmService]] = {}
    for service in services:
        for network_id in _service_network_ids(service, known, by_name):
            used.setdefault(network_id, []).append(service)
    return used


def _service_network_ids(
    service: SwarmService, known: set[str], by_name: Mapping[str, str]
) -> dict[str, None]:
    found: dict[str, None] = {}
    template = {"TaskTemplate": service.spec.task_template}
    for target, _ in references(template).networks:
        if target in known:
            found[target] = None
        elif NETWORK_ID.fullmatch(target) is None and target in by_name:
            found[by_name[target]] = None
    for address in service.endpoint.virtual_ips:
        if address.network_id in known:
            found[address.network_id] = None
    return found


async def permitted_service_ids(
    authoriser: Authoriser,
    actor: Principal,
    environment_id: int,
    services: Sequence[SwarmService],
    level: AccessLevel = "read",
) -> frozenset[str]:
    """Return the ids of the services the caller holds the level over, in one pass."""
    if actor.is_administrator:
        return frozenset(s.id for s in services)
    refs = [ResourceRef(environment_id, "service", s.id, s.spec.labels) for s in services]
    return frozenset(r.resource_id for r in await authoriser.filter_visible(actor, refs, level))


def describe_service_users(
    using: Sequence[SwarmService], permitted: frozenset[str], limit: int = MAX_USERS_SHOWN
) -> tuple[tuple[ObjectUser, ...], int, int]:
    """Name the services the caller may see, at most `limit`; `state` holds the scheduling mode.

    The second value counts permitted services cut by the limit, the third the services the
    caller may not see.
    """
    allowed = sorted(
        (s for s in using if s.id in permitted),
        key=lambda s: (s.spec.name.casefold(), s.id),
    )
    shown = allowed[:limit]
    return (
        tuple(
            ObjectUser(s.id, short_id(s.id), s.spec.name[:MAX_NAME_CHARS], service_mode(s))
            for s in shown
        ),
        len(allowed) - len(shown),
        len(using) - len(allowed),
    )


async def permitted_container_ids(
    authoriser: Authoriser,
    actor: Principal,
    environment_id: int,
    containers: Sequence[ContainerSummary],
    level: AccessLevel = "read",
) -> frozenset[str]:
    """Return the ids of the containers the caller holds the level over, in one pass."""
    if actor.is_administrator:
        return frozenset(c.id for c in containers)
    refs = [ResourceRef(environment_id, "container", c.id, c.labels) for c in containers]
    return frozenset(r.resource_id for r in await authoriser.filter_visible(actor, refs, level))


def describe_users(
    using: Sequence[ContainerSummary], permitted: frozenset[str], limit: int = MAX_USERS_SHOWN
) -> tuple[tuple[ObjectUser, ...], int, int]:
    """Name the users the caller may see, at most `limit`.

    The second value counts permitted users cut by the limit, the third the users the caller may
    not see.
    """
    allowed = sorted(
        (c for c in using if c.id in permitted),
        key=lambda c: (display_name(c.names, c.id).casefold(), c.id),
    )
    shown = allowed[:limit]
    return (
        tuple(
            ObjectUser(c.id, short_id(c.id), display_name(c.names, c.id)[:MAX_NAME_CHARS], c.state)
            for c in shown
        ),
        len(allowed) - len(shown),
        len(using) - len(allowed),
    )


def row_id(text: str) -> int:
    """Return the integer a keyset cursor pairs with a sort value for the object `text`."""
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:ROW_ID_HEX_DIGITS], 16)


def decode_position(cursor: str | None) -> CursorPosition | None:
    """Decode a page cursor whose sort value is text; ValidationFailed when it is not."""
    if cursor is None:
        return None
    try:
        position = decode_cursor(cursor)
    except InvalidCursorError:
        raise ValidationFailed({"cursor": "The page link is not valid."}) from None
    if not isinstance(position.sort_value, str):
        raise ValidationFailed({"cursor": "The page link is not valid."})
    return position


def check_limit(limit: int) -> int:
    """Return the page size if it is in range; ValidationFailed otherwise."""
    if isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValidationFailed({"limit": f"Choose a page size from 1 to {MAX_PAGE_SIZE}."})
    return limit


def label_views(labels: Mapping[str, str], viewer_is_admin: bool) -> tuple[LabelView, ...]:
    """Return the labels sorted by key, with secrets withheld from a standard user."""
    views: list[LabelView] = []
    for key, value in sorted(labels.items())[:MAX_DETAIL_ROWS]:
        if viewer_is_admin:
            views.append(LabelView(key, _clip_label(value), False))
        else:
            shown, redacted = redact_label(key, value)
            views.append(LabelView(key, _clip_label(shown), redacted))
    return tuple(views)


def _clip_label(value: str) -> str:
    if len(value) <= MAX_LABEL_VALUE_CHARS:
        return value
    return f"{value[:MAX_LABEL_VALUE_CHARS]}…"


def option_views(options: Mapping[str, str]) -> tuple[OptionView, ...]:
    """Return driver options sorted by key; secrets are withheld or scrubbed, never shown raw."""
    views: list[OptionView] = []
    for key, value in sorted(options.items())[:MAX_DETAIL_ROWS]:
        key = key[:MAX_OPTION_CHARS]
        if DEFAULT_SCRUBBER.is_sensitive(key):
            views.append(OptionView(key, REDACTED, True))
            continue
        value = value[:MAX_OPTION_CHARS]
        scrubbed = DEFAULT_SCRUBBER.scrub_text(_scrub_embedded_passwords(value))
        views.append(OptionView(key, scrubbed, scrubbed != value))
    return tuple(views)


def _scrub_embedded_passwords(value: str) -> str:
    value = _CIFS_PASSWORD.sub(lambda m: f"{m['head']}%{REDACTED}", value)
    return _USERINFO_PASSWORD.sub(lambda m: f"{m['user']}:{REDACTED}", value)


def busy_view(busy: ObjectBusy, viewer: Principal, now: datetime) -> BusyView:
    """Describe the lock holder to a viewer: names only for administrators."""
    holder = busy.holder
    age = int(holder.age_seconds(now))
    match holder.actor:
        case UserActor(user_id=user_id):
            mine = user_id == viewer.user_id
            if viewer.is_administrator:
                return BusyView(holder.action, age, holder.label, mine)
            return BusyView(holder.action, age, "you" if mine else "another user", mine)
        case SubsystemActor():
            return BusyView(holder.action, age, holder.label, False)


def create_failure(
    error: EngineError,
    *,
    event: str,
    name_in_use: Callable[[], ProductError],
    refused: Callable[[str | None], ProductError],
) -> ProductError:
    """Return the error a refused create maps to and set the audit reason; the caller raises it."""
    audit = current_audit()
    logger.warning(event, extra={"error": type(error).__name__, "status": error.status_code})
    if isinstance(error, Conflict):
        audit.reason = "name_in_use"
        return name_in_use()
    if isinstance(error, RequestOutcomeUnknown) or (
        not isinstance(error, EnvironmentUnreachable)
        and (error.status_code is None or error.status_code < 400)
    ):
        audit.reason = "outcome_unknown"
        return EnvironmentUnavailable("outcome_unknown")
    if isinstance(error, EnvironmentUnreachable):
        audit.reason = "unreachable"
        return EnvironmentUnavailable("unreachable")
    audit.reason = "engine_rejected"
    return refused(engine_detail(error))


def failure_reason(error: BaseException) -> str:
    """Classify the way an Engine call ended, for the failure row of an interrupted request."""
    if isinstance(error, NotFound):
        return "not_found"
    if isinstance(error, Conflict):
        return "conflict"
    if isinstance(error, RequestOutcomeUnknown):
        return "outcome_unknown"
    if isinstance(error, EnvironmentUnreachable):
        return "unreachable"
    return "engine_error"


async def settled(task: asyncio.Future[Any], seconds: float) -> tuple[bool, BaseException | None]:
    """Wait up to `seconds` for a task; report whether it finished and how it ended."""
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if task not in done:
        return False, None
    if task.cancelled():
        return True, asyncio.CancelledError()
    return True, task.exception()


async def run_shielded[T](
    call: Callable[[], Awaitable[T]],
    interrupted: Callable[[asyncio.Future[T]], Awaitable[None]],
) -> T:
    """Run an Engine call to completion even if the request is cancelled.

    On cancellation `interrupted` receives the still-running task, to record what became of it;
    the cancellation is then re-raised. Engine errors propagate to the caller.
    """
    task: asyncio.Future[T] = asyncio.ensure_future(call())
    task.add_done_callback(retrieve_exception)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await interrupted(task)
        raise


async def audited_client(hosts: HostClients, environment_id: int) -> EngineClient:
    """Return the host's client; inside an audit scope an unavailable host names its reason."""
    try:
        return await hosts.client(environment_id)
    except EnvironmentUnavailable as error:
        current_audit().reason = error.kind
        raise


async def audited_list_read[T](read: Awaitable[T]) -> T:
    """Await a bounded host list read, naming the reason on the open audit scope if it fails."""
    try:
        return await read
    except EnvironmentUnavailable as error:
        current_audit().reason = error.kind
        raise
    except ObjectTooLarge:
        current_audit().reason = "too_large"
        raise


@asynccontextmanager
async def hold_all(
    locks: ObjectLockRegistry, keys: Sequence[ObjectKey], holder: LockHolder
) -> AsyncIterator[None]:
    """Hold every object, without waiting and in a fixed order; ObjectBusy names it on the scope."""
    ordered = sorted(set(keys), key=lambda key: (key.object_type, key.object_id))
    try:
        async with AsyncExitStack() as stack:
            for key in ordered:
                await stack.enter_async_context(locks.hold(key, holder))
            yield
    except ObjectBusy as busy:
        audit = current_audit()
        audit.reason = "object_busy"
        audit.detail["holder_action"] = busy.holder.action
        raise


@dataclass(frozen=True, slots=True)
class SettleStep:
    """One attempt to settle an operation's side effects in the transaction of its row.

    `work` builds the function to run from the new session and its recorder, `note` sees its
    result before the row is written, and `failed` sees the error of an attempt that did not
    complete.
    """

    work: Callable[[Session, ActivityRecorder], Callable[[], Awaitable[Any]]]
    note: Callable[[Any], None]
    failed: Callable[[Exception], None]


async def in_transaction[T](
    db: Session,
    work: Callable[[], Awaitable[T]],
    row: Callable[[], Awaitable[None]] | None = None,
    note: Callable[[T], None] | None = None,
) -> T:
    """Run `work` and then `row` in one transaction; `note` sees the work's result in between."""
    async with db.transaction():
        result = await work()
        if note is not None:
            note(result)
        if row is not None:
            await row()
    return result


async def write_settled_row(
    database: Database,
    clock: Clock,
    actor: Acting,
    *,
    action: str,
    target_type: str,
    target_id: str | None,
    environment_id: int,
    detail: dict[str, DetailValue],
    failure: str | None = None,
    steps: Sequence[SettleStep] = (),
) -> None:
    """Write the row of an operation that outlived its request, on a session of its own.

    With `failure` the row is a failure row. Otherwise each step is tried in order in one
    transaction with the success row, and the row alone is written if none completes. The write
    runs outside the caller's audit scope and survives the caller's cancellation.
    """
    task = asyncio.get_running_loop().create_task(
        _write_row(
            database,
            clock,
            actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            environment_id=environment_id,
            detail=detail,
            failure=failure,
            steps=steps,
        ),
        context=contextvars.Context(),
    )
    task.add_done_callback(retrieve_exception)
    await asyncio.shield(task)


async def _write_row(
    database: Database,
    clock: Clock,
    actor: Acting,
    *,
    action: str,
    target_type: str,
    target_id: str | None,
    environment_id: int,
    detail: dict[str, DetailValue],
    failure: str | None,
    steps: Sequence[SettleStep],
) -> None:
    recorded_as = actor_of(actor)
    try:
        async with database.session() as db:
            recorder = ActivityRecorder(db, clock)
            if failure is not None:
                await recorder.record_failure(
                    recorded_as,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
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
                recorded_as,
                action=action,
                target_type=target_type,
                target_id=target_id,
                environment_id=environment_id,
                result="success",
                detail=detail,
            )
            if not steps or target_id is None:
                await retry_busy(write)
                return
            async with recorder.audit(
                recorded_as,
                action=action,
                target_type=target_type,
                target_id=target_id,
                environment_id=environment_id,
            ) as state:
                state.unchanged()
                for step in steps:
                    try:
                        await retry_busy(
                            partial(in_transaction, db, step.work(db, recorder), write, step.note)
                        )
                    except Exception as error:
                        step.failed(error)
                    else:
                        return
                await retry_busy(write)
    except Exception as error:
        logger.error(
            "activity_event_write_failed",
            extra={
                "action": action,
                "result": "failure" if failure else "success",
                "actor_user_id": recorded_as.user_id
                if isinstance(recorded_as, UserActor)
                else None,
                "actor_subsystem": recorded_as.name
                if isinstance(recorded_as, SubsystemActor)
                else None,
                "target_type": target_type,
                "target_id": target_id,
                "environment_id": environment_id,
                "reason": failure,
                "error": type(error).__name__,
            },
        )
