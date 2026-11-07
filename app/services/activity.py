"""Actor model, subsystem registry, and the single writer of activity events."""

import asyncio
import functools
import inspect
import sqlite3
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import partial
from types import MappingProxyType
from typing import Any, Concatenate, Final, Protocol, runtime_checkable

import anyio

from app.auth.errors import NotFound, PermissionDenied, ProductError
from app.auth.principal import Principal
from app.db.engine import Session, retry_busy
from app.db.repositories.activity_event import (
    MAX_DETAIL_KEYS,
    ActivityEventRepository,
    ActorType,
    Category,
    DetailValue,
    Outcome,
)
from app.db.repositories.clocked import Clock, utc_now
from app.db.repositories.errors import FOREIGN_KEY_PREFIX, RepositoryError
from app.db.repositories.notification_outbox import NotificationOutboxRepository
from app.logging import get_logger, request_id_var
from app.notifications import catalogue

logger = get_logger(__name__)

LOGIN_ACTION = "login"
BOOTSTRAP_ACTION = "bootstrap"
CLEAR_LOCKOUT_ACTION = "lockout_cleared"
ANONYMOUS_ACTIONS = frozenset({LOGIN_ACTION, BOOTSTRAP_ACTION, CLEAR_LOCKOUT_ACTION})

# Maps each name to whether its actions carry a rule.
SUBSYSTEMS: Mapping[str, bool] = MappingProxyType(
    {
        "container-updater": True,
        "container-health-checker": True,
        "stack-restarter": True,
        "backup-syncer": True,
        "activity-log-exporter": True,
        "container-log-exporter": True,
        "notification-sender": False,
        "environment-snapshot-refresher": False,
        "activity-log-pruner": False,
        "webhook-receiver": True,
    }
)

# Names that act in the log but own no job, so no handler, switch or schedule exists for them.
UNSCHEDULED_SUBSYSTEMS: Final[frozenset[str]] = frozenset({"activity-log-pruner"})

# Exceptions whose text the product wrote, so it may be stored; any other text may carry data.
_PRODUCT_ERRORS = (ProductError, RepositoryError)

_RESERVED_DETAIL_KEYS = 2
_RULE_DETAIL_KEYS = 2

EnvironmentChangeHook = Callable[[int], None]
_environment_change_hook: EnvironmentChangeHook | None = None


def install_environment_change_hook(hook: EnvironmentChangeHook | None) -> None:
    """Set the callable told, once per audited mutation, which environment it changed.

    It runs on the event loop after the mutation committed and must not block or raise; None
    removes it. One hook serves the process.
    """
    global _environment_change_hook
    _environment_change_hook = hook


def remove_environment_change_hook(hook: EnvironmentChangeHook) -> None:
    """Remove the installed hook if it is this one; any other state is left as it is."""
    if _environment_change_hook is not None and _environment_change_hook == hook:
        install_environment_change_hook(None)


NotificationHook = Callable[[], None]
_notification_hook: NotificationHook | None = None


def install_notification_hook(hook: NotificationHook | None) -> None:
    """Set the callable told that deliveries were queued; None removes it.

    It runs on the event loop after the transaction that queued them committed, at most once per
    transaction, and must not block or raise. One hook serves the process.
    """
    global _notification_hook
    _notification_hook = hook


def remove_notification_hook(hook: NotificationHook) -> None:
    """Remove the installed hook if it is this one; any other state is left as it is."""
    if _notification_hook is not None and _notification_hook == hook:
        install_notification_hook(None)


def _announce_notifications() -> None:
    hook = _notification_hook
    if hook is None:
        return
    try:
        hook()
    except Exception as error:
        logger.error("notification_hook_failed", extra={"error": type(error).__name__})


class RequestChanges:
    """Whether an audited mutation of an environment committed while one request ran."""

    __slots__ = ("committed", "scope")

    def __init__(self, scope: object) -> None:
        self.scope = scope
        self.committed = False


_request_changes: ContextVar[RequestChanges | None] = ContextVar(
    "activity_request_changes", default=None
)


def track_request_changes(scope: object) -> None:
    """Start recording committed environment mutations for the request that owns `scope`.

    Must run in the request's own task, before any mutation, so work spawned from it shares the
    record.
    """
    _request_changes.set(RequestChanges(scope))


def request_changed_environment(scope: object) -> bool:
    """Report whether an audited environment mutation committed during the request of `scope`."""
    tracked = _request_changes.get()
    return tracked is not None and tracked.scope is scope and tracked.committed


def _announce_environment_change(environment_id: int | None) -> None:
    if environment_id is None:
        return
    tracked = _request_changes.get()
    if tracked is not None:
        tracked.committed = True
    hook = _environment_change_hook
    if hook is None:
        return
    try:
        hook(environment_id)
    except Exception as error:
        logger.error(
            "environment_change_hook_failed",
            extra={"environment_id": environment_id, "error": type(error).__name__},
        )


class UnknownSubsystem(ValueError):
    """Raised when a subsystem name is not in the registry; always a programming error."""


class MultipleCommits(RuntimeError):
    """Raised when an audited operation commits a second transaction."""


def _check_subsystem(name: str, rule_id: int | None) -> None:
    if name not in SUBSYSTEMS:
        raise UnknownSubsystem(f"{name!r} is not a registered subsystem")
    if SUBSYSTEMS[name] != (rule_id is not None):
        raise ValueError(f"{name} {'requires' if SUBSYSTEMS[name] else 'takes no'} rule_id")


@dataclass(frozen=True, slots=True)
class UserActor:
    """A signed-in user."""

    user_id: int

    @classmethod
    def of(cls, principal: Principal) -> "UserActor":
        """Convert the authenticated caller."""
        return cls(principal.user_id)


@dataclass(frozen=True, slots=True)
class SubsystemActor:
    """A registered subsystem, with the rule that fired and the job run it belongs to.

    `rule_name` is stamped into the detail of every row the actor writes, beside `rule_id`, so
    a row stays answerable after its rule is deleted. It takes no part in comparison.
    """

    name: str
    rule_id: int | None
    job_run_id: int | None = None
    rule_name: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        _check_subsystem(self.name, self.rule_id)


@dataclass(frozen=True, slots=True)
class AnonymousActor:
    """An actor with no account, accepted only for authentication events."""


type Actor = UserActor | SubsystemActor


@runtime_checkable
class ActsAsSubsystem(Protocol):
    """Something that acts as one subsystem run and can be named as an audited actor."""

    @property
    def subsystem_actor(self) -> SubsystemActor:
        """Return the actor the rows are recorded under."""
        ...


@dataclass(frozen=True, slots=True)
class _Origin:
    actor_type: ActorType
    user_id: int | None = None
    subsystem: str | None = None
    rule_id: int | None = None
    job_run_id: int | None = None


def _origin_of(actor: Actor | AnonymousActor, action: str) -> _Origin:
    """Validate the actor from its exact type and values, which a frozen dataclass cannot pin."""
    if type(actor) is UserActor:
        return _Origin("user", user_id=actor.user_id)
    if type(actor) is SubsystemActor:
        _check_subsystem(actor.name, actor.rule_id)
        if actor.job_run_id is not None and actor.job_run_id < 1:
            raise ValueError("an event cannot name a run that has no job_run row")
        return _Origin(
            "subsystem", subsystem=actor.name, rule_id=actor.rule_id, job_run_id=actor.job_run_id
        )
    if type(actor) is AnonymousActor:
        if action not in ANONYMOUS_ACTIONS:
            raise ValueError(f"an anonymous actor cannot record {action!r}")
        return _Origin("anonymous")
    raise TypeError(f"{type(actor).__name__} is not an actor")


@dataclass(slots=True)
class AuditState:
    """The values a mutation fills in while its audit scope is open."""

    target_id: str | None
    detail: dict[str, DetailValue] = field(default_factory=dict)
    reason: str | None = None
    category: Category | None = None
    session: Session | None = field(default=None, repr=False)
    suppressed: bool = field(default=False, init=False)
    refused: bool = field(default=False, init=False)
    written: bool = field(default=False, init=False)
    committed: bool = field(default=False, init=False)

    def unchanged(self) -> None:
        """Suppress the success row because the operation changed nothing."""
        self.suppressed = True

    def refuse(self) -> None:
        """Leave no failure row, for a refusal given before the caller's access was established."""
        self.refused = True


_current: ContextVar[AuditState | None] = ContextVar("activity_audit", default=None)


def current_audit() -> AuditState:
    """Return the innermost open audit scope's state."""
    state = _current.get()
    if state is None:
        raise RuntimeError("no audit scope is open")
    return state


class ActivityRecorder:
    """Writes activity events for one database session; the only writer of the table."""

    def __init__(self, db: Session, clock: Clock = utc_now) -> None:
        self._db = db
        self._events = ActivityEventRepository(db, clock)
        self._outbox = NotificationOutboxRepository(db, clock)
        self._disarm_poke: Callable[[], None] | None = None

    async def record_event(
        self,
        actor: Actor | AnonymousActor,
        *,
        action: str,
        target_type: str,
        target_id: str | None = None,
        environment_id: int | None = None,
        result: Outcome,
        detail: Mapping[str, DetailValue] | None = None,
        category: Category | None = None,
    ) -> None:
        """Write one event row, inside the open transaction if there is one."""
        await self._insert(
            actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            environment_id=environment_id,
            result=result,
            detail=detail,
            category=category,
        )

    async def record_failure(
        self,
        actor: Actor | AnonymousActor,
        *,
        action: str,
        target_type: str,
        reason: str,
        target_id: str | None = None,
        environment_id: int | None = None,
        detail: Mapping[str, DetailValue] | None = None,
        category: Category | None = None,
        message: str | None = None,
    ) -> None:
        """Write a failure row outside any transaction; a row that cannot be written is logged.

        `message` must be product-written text: it is stored as given.
        """
        await self._write_failure(
            actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            environment_id=environment_id,
            category=category,
            reason=reason,
            message=message,
            body=detail or {},
        )

    async def record_related(
        self,
        actor: Actor | Principal,
        *,
        action: str,
        target_type: str,
        target_id: str | None,
        environment_id: int | None,
        detail: Mapping[str, DetailValue] | None = None,
    ) -> None:
        """Write a success row about another object inside the open audit scope's transaction.

        The row takes the scope's category, and commits or rolls back with the operation. Raises
        RuntimeError unless a scope is open on this recorder's session and a transaction is active.
        """
        scope = _current.get()
        if scope is None or scope.session is not self._db:
            raise RuntimeError("related rows are written inside an audit scope of this recorder")
        if not self._db.in_transaction:
            raise RuntimeError("related rows are written inside the operation's transaction")
        await self._insert(
            _actor_of(actor),
            action=action,
            target_type=target_type,
            target_id=target_id,
            environment_id=environment_id,
            result="success",
            detail=detail,
            category=scope.category,
        )

    @asynccontextmanager
    async def audit(
        self,
        actor: Actor,
        *,
        action: str,
        target_type: str,
        target_id: str | None = None,
        environment_id: int | None = None,
        category: Category | None = None,
        announces: bool = True,
    ) -> AsyncIterator[AuditState]:
        """Record the outcome of the block: a success row, or a failure row naming the reason.

        `announces=False` keeps `environment_id` on the row but does not report the commit as an
        environment change.

        Must be entered outside any transaction. The success row commits with the block's own
        transaction, so an audited block commits at most one; a second commit raises
        MultipleCommits and rolls back. PermissionDenied and NotFound leave no row, and neither
        does an exception raised after the block's transaction committed.
        """
        if isinstance(actor, AnonymousActor):
            raise ValueError("an anonymous actor cannot own a mutation")
        if self._db.in_transaction:
            raise RuntimeError("an audit scope must be entered outside any transaction")
        if _current.get() is not None:
            raise RuntimeError("audit scopes do not nest")
        _origin_of(actor, action)
        _resolve_category(actor, category)
        state = AuditState(target_id, category=category, session=self._db)

        async def write_success() -> None:
            if state.suppressed:
                return
            await self._insert(
                actor,
                action=action,
                target_type=target_type,
                target_id=state.target_id,
                environment_id=environment_id,
                result="success",
                detail=state.detail,
                category=category,
            )
            state.written = True

        async def single_commit() -> None:
            if state.committed:
                raise MultipleCommits("an audited operation committed more than one transaction")

        def mark_committed() -> None:
            state.committed = True
            if announces and not state.suppressed:
                _announce_environment_change(environment_id)

        disarm = (
            self._db.before_commit(single_commit),
            self._db.before_commit(write_success, once=True),
            self._db.after_commit(mark_committed),
        )
        token = _current.set(state)
        try:
            try:
                yield state
            except Exception as error:
                if _leaves_failure_row(error, state):
                    await self._write_failure(
                        actor,
                        action=action,
                        target_type=target_type,
                        target_id=state.target_id,
                        environment_id=environment_id,
                        category=category,
                        reason=_reason_of(error, state),
                        message=str(error) if isinstance(error, _PRODUCT_ERRORS) else None,
                        body=state.detail,
                    )
                raise
            if not state.suppressed and not state.committed:
                if announces:
                    _announce_environment_change(environment_id)

                async def write_logged() -> None:
                    try:
                        await retry_busy(write_success)
                    except Exception as error:
                        if (
                            isinstance(error, sqlite3.IntegrityError)
                            and str(error).startswith(FOREIGN_KEY_PREFIX)
                            and await self._write_success_unlinked(
                                actor,
                                action=action,
                                target_type=target_type,
                                state=state,
                                environment_id=environment_id,
                                category=category,
                            )
                        ):
                            return
                        self._log_write_failed(
                            actor,
                            action=action,
                            result="success",
                            target_type=target_type,
                            target_id=state.target_id,
                            environment_id=environment_id,
                            reason=None,
                            error=error,
                        )

                await completed(write_logged())
        finally:
            _current.reset(token)
            for remove in disarm:
                remove()

    async def _write_failure(
        self,
        actor: Actor | AnonymousActor,
        *,
        action: str,
        target_type: str,
        target_id: str | None,
        environment_id: int | None,
        category: Category | None,
        reason: str,
        message: str | None,
        body: Mapping[str, DetailValue],
    ) -> None:
        try:
            await retry_busy(
                partial(
                    self._insert,
                    actor,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    environment_id=environment_id,
                    result="failure",
                    detail=_failure_detail(body, reason, message, reserved=_reserved_keys(actor)),
                    category=category,
                )
            )
        except sqlite3.IntegrityError as error:
            if str(error).startswith(FOREIGN_KEY_PREFIX):
                await self._write_failure_unlinked(
                    actor,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    environment_id=environment_id,
                    category=category,
                    reason=reason,
                    message=message,
                    body=body,
                )
                return
            self._log_write_failed(
                actor,
                action=action,
                result="failure",
                target_type=target_type,
                target_id=target_id,
                environment_id=environment_id,
                reason=reason,
                error=error,
            )
        except Exception as error:
            self._log_write_failed(
                actor,
                action=action,
                result="failure",
                target_type=target_type,
                target_id=target_id,
                environment_id=environment_id,
                reason=reason,
                error=error,
            )

    async def _write_failure_unlinked(
        self,
        actor: Actor | AnonymousActor,
        *,
        action: str,
        target_type: str,
        target_id: str | None,
        environment_id: int | None,
        category: Category | None,
        reason: str,
        message: str | None,
        body: Mapping[str, DetailValue],
    ) -> None:
        """Retry a failure row whose references no longer resolve, naming them in detail instead."""
        try:
            moved = _moved_references(actor, action, environment_id)
            await retry_busy(
                partial(
                    self._insert,
                    actor,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    environment_id=None,
                    result="failure",
                    detail=_failure_detail(
                        body, reason, message, moved, reserved=_reserved_keys(actor)
                    ),
                    category=category,
                    unlinked=True,
                )
            )
        except Exception as error:
            self._log_write_failed(
                actor,
                action=action,
                result="failure",
                target_type=target_type,
                target_id=target_id,
                environment_id=environment_id,
                reason=reason,
                error=error,
            )

    async def _write_success_unlinked(
        self,
        actor: Actor,
        *,
        action: str,
        target_type: str,
        state: AuditState,
        environment_id: int | None,
        category: Category | None,
    ) -> bool:
        """Retry a success row whose references no longer resolve; report whether it was written."""
        try:
            moved = _moved_references(actor, action, environment_id)
            room = MAX_DETAIL_KEYS - _RULE_DETAIL_KEYS - len(moved)
            body = dict(list((state.detail or {}).items())[:room])
            await retry_busy(
                partial(
                    self._insert,
                    actor,
                    action=action,
                    target_type=target_type,
                    target_id=state.target_id,
                    environment_id=None,
                    result="success",
                    detail={**body, **moved},
                    category=category,
                    unlinked=True,
                )
            )
        except Exception:
            return False
        state.written = True
        return True

    def _log_write_failed(
        self,
        actor: Actor | AnonymousActor,
        *,
        action: str,
        result: Outcome,
        target_type: str,
        target_id: str | None,
        environment_id: int | None,
        reason: str | None,
        error: Exception,
    ) -> None:
        logger.error(
            "activity_event_write_failed",
            extra={
                "action": action,
                "result": result,
                "actor_type": _actor_label(actor),
                "actor_user_id": actor.user_id if isinstance(actor, UserActor) else None,
                "actor_subsystem": actor.name if isinstance(actor, SubsystemActor) else None,
                "target_type": target_type,
                "target_id": target_id,
                "environment_id": environment_id,
                "reason": reason,
                "error": type(error).__name__,
            },
        )

    async def _insert(
        self,
        actor: Actor | AnonymousActor,
        *,
        action: str,
        target_type: str,
        target_id: str | None,
        environment_id: int | None,
        result: Outcome,
        detail: Mapping[str, DetailValue] | None,
        category: Category | None,
        unlinked: bool = False,
    ) -> None:
        origin = _origin_of(actor, action)
        resolved = _resolve_category(actor, category)
        if unlinked:
            origin = replace(origin, rule_id=None, job_run_id=None)
        if isinstance(actor, SubsystemActor) and actor.rule_id is not None:
            detail = _with_rule(detail, actor)
        raised = catalogue.match(action, result, origin.subsystem)

        async def write() -> None:
            event_id = await self._events.insert(
                category=resolved,
                actor_type=origin.actor_type,
                actor_user_id=origin.user_id,
                actor_subsystem=origin.subsystem,
                rule_id=origin.rule_id,
                job_run_id=origin.job_run_id,
                request_id=None if isinstance(actor, SubsystemActor) else request_id_var.get(),
                environment_id=environment_id,
                target_type=target_type,
                target_id=target_id,
                action=action,
                result=result,
                detail=detail,
            )
            if raised:
                scope = linked_environment(detail) if unlinked else environment_id
                await self._enqueue(event_id, raised, scope)

        if not raised or self._db.in_transaction:
            await write()
            return
        try:
            async with self._db.transaction(standalone=True):
                await write()
        except BaseException:
            self._cancel_poke()
            raise

    async def _enqueue(
        self, event_id: int, raised: tuple[catalogue.EventType, ...], environment_id: int | None
    ) -> None:
        queued = await self._outbox.enqueue(event_id, [row.key for row in raised], environment_id)
        if queued and self._disarm_poke is None:
            self._disarm_poke = self._db.after_commit(self._poke_after_commit)

    def _poke_after_commit(self) -> None:
        self._cancel_poke()
        _announce_notifications()

    def _cancel_poke(self) -> None:
        disarm, self._disarm_poke = self._disarm_poke, None
        if disarm is not None:
            disarm()


def linked_environment(detail: Mapping[str, DetailValue] | None) -> int | None:
    """Return the environment an unlinked row moved into detail; a scoped channel still sees it."""
    moved = None if detail is None else detail.get("environment_id")
    if isinstance(moved, str) and moved.isdecimal() and len(moved) <= 18:
        return int(moved)
    return None


def _actor_label(actor: Actor | AnonymousActor) -> str:
    match actor:
        case UserActor():
            return "user"
        case SubsystemActor():
            return "subsystem"
        case AnonymousActor():
            return "anonymous"


async def completed[T](work: Coroutine[Any, Any, T]) -> T:
    """Await the work to completion even if the caller is cancelled, then re-raise the cancel."""
    task = asyncio.ensure_future(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            await asyncio.wait({task})
        raise


def _leaves_failure_row(error: Exception, state: AuditState) -> bool:
    if state.refused or isinstance(error, PermissionDenied | NotFound):
        return False
    return not state.committed or isinstance(error, MultipleCommits)


def _reason_of(error: Exception, state: AuditState) -> str:
    if state.reason:
        return state.reason
    if state.written and not state.committed:
        return "commit_failed"
    return type(error).__name__


def _reserved_keys(actor: Actor | AnonymousActor) -> int:
    if isinstance(actor, SubsystemActor) and actor.rule_id is not None:
        return _RESERVED_DETAIL_KEYS + _RULE_DETAIL_KEYS
    return _RESERVED_DETAIL_KEYS


def _moved_references(
    actor: Actor | AnonymousActor, action: str, environment_id: int | None
) -> dict[str, DetailValue]:
    origin = _origin_of(actor, action)
    return {
        name: str(value)
        for name, value in (
            ("environment_id", environment_id),
            ("rule_id", origin.rule_id),
            ("job_run_id", origin.job_run_id),
        )
        if value is not None
    }


def _with_rule(
    detail: Mapping[str, DetailValue] | None, actor: SubsystemActor
) -> dict[str, DetailValue]:
    stamped = dict(detail or {})
    for key, value in (("rule_id", actor.rule_id), ("rule_name", actor.rule_name)):
        if value is not None and key not in stamped and len(stamped) < MAX_DETAIL_KEYS:
            stamped[key] = value
    return stamped


def _failure_detail(
    body: Mapping[str, DetailValue],
    reason: str,
    message: str | None,
    moved: Mapping[str, DetailValue] | None = None,
    *,
    reserved: int = _RESERVED_DETAIL_KEYS,
) -> dict[str, DetailValue]:
    """Build failure detail that always has room for reason and message, dropping body if needed."""
    extra = moved or {}
    detail: dict[str, DetailValue] = {}
    if len(body) + len(extra) <= MAX_DETAIL_KEYS - reserved:
        detail.update(body)
    detail.update(extra)
    detail["reason"] = reason
    if message:
        detail["message"] = message
    return detail


def _resolve_category(actor: Actor | AnonymousActor, category: Category | None) -> Category:
    default: Category
    allowed: tuple[Category, ...]
    match actor:
        case UserActor():
            default, allowed = "user_action", ("user_action", "authentication")
        case SubsystemActor():
            default, allowed = "automation", ("automation",)
        case AnonymousActor():
            default, allowed = "authentication", ("authentication",)
    chosen = default if category is None else category
    if chosen not in allowed:
        raise ValueError(f"a {type(actor).__name__} cannot record a {chosen} event")
    return chosen


def _actor_of(value: object) -> Actor:
    match value:
        case Principal():
            return UserActor.of(value)
        case UserActor() | SubsystemActor():
            return value
        case ActsAsSubsystem():
            return value.subsystem_actor
        case _:
            raise TypeError("actor must be a Principal, UserActor, SubsystemActor or authority")


type Arguments = Mapping[str, Any]


def environment_argument(arguments: Arguments) -> int:
    """Return the `environment_id` argument of an audited call."""
    environment_id: int = arguments["environment_id"]
    return environment_id


class _HasRecorder(Protocol):
    _activity: ActivityRecorder


def audited[S: _HasRecorder, **P, R](
    *,
    action: str | Callable[[Arguments], str],
    target_type: str | Callable[[Arguments], str],
    target_id: str | Callable[[Arguments], str | None] | None = None,
    environment_id: Callable[[Arguments], int | None] | None = None,
    category: Category | None = None,
    announces: bool = True,
) -> Callable[
    [Callable[Concatenate[S, P], Coroutine[Any, Any, R]]],
    Callable[Concatenate[S, P], Coroutine[Any, Any, R]],
]:
    """Wrap a service method in an audit scope on the recorder at self._activity.

    The method needs an actor parameter. A remote_address parameter is recorded as detail
    address. The resolvers receive the call's arguments by parameter name.
    """

    def decorate(
        func: Callable[Concatenate[S, P], Coroutine[Any, Any, R]],
    ) -> Callable[Concatenate[S, P], Coroutine[Any, Any, R]]:
        signature = inspect.signature(func)
        names = list(signature.parameters)
        if "actor" not in names or names[0] == "actor":
            raise TypeError(f"{func.__qualname__} must be a method with an actor parameter")

        @functools.wraps(func)
        async def wrapper(owner: S, /, *args: P.args, **kwargs: P.kwargs) -> R:
            bound = signature.bind(owner, *args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            async with owner._activity.audit(
                _actor_of(values["actor"]),
                action=action if isinstance(action, str) else action(values),
                target_type=target_type if isinstance(target_type, str) else target_type(values),
                target_id=target_id if isinstance(target_id, str | None) else target_id(values),
                environment_id=None if environment_id is None else environment_id(values),
                category=category,
                announces=announces,
            ) as state:
                address = values.get("remote_address")
                if address is not None:
                    state.detail["address"] = address
                return await func(owner, *args, **kwargs)

        return wrapper

    return decorate
