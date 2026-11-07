"""Base class for the named automation actors the scheduler runs."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import ClassVar, Final, Literal, NoReturn, final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.auth.errors import NotFound, PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.auth.services import AuthRuntime
from app.config import Settings
from app.db.engine import Database, Session
from app.db.repositories.activity_event import (
    ActivityEventRepository,
    Category,
    DetailValue,
    Outcome,
)
from app.db.repositories.automation_rule import AutomationRule, AutomationRuleRepository
from app.db.repositories.clocked import Clock, format_timestamp, parse_timestamp
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.registry_access import RegistryAccessRepository
from app.db.repositories.scheduled_job import OverrunPolicy, ScheduleKind
from app.db.repositories.stack import StackKind
from app.db.repositories.user import UserRepository
from app.docker_engine.models import ContainerSummary
from app.logging import get_logger
from app.scheduler.model import (
    Dispatch,
    EventPolicy,
    RunFailed,
    RunResult,
    RunSkipped,
    reason_of,
)
from app.scheduler.schedules import MIN_RULE_INTERVAL_SECONDS
from app.scheduler.suppression import SuppressionRegistry
from app.services.access import RESOURCE_TYPES, ResourceKey, ResourceRef, is_canonical_id
from app.services.acting import AutomationAuthority
from app.services.activity import (
    SUBSYSTEMS,
    ActivityRecorder,
    Actor,
    AnonymousActor,
    SubsystemActor,
    UserActor,
)
from app.services.authorisation import Authoriser
from app.services.backups import BackupProduction
from app.services.engine_setup import EngineRuntime
from app.services.object_inspector import (
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
    UnavailableKind,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.registry_resolution import CredentialSetUnavailable
from app.services.registry_setup import RegistryRuntime
from app.subsystems.selectors import SelectorKind

logger = get_logger(__name__)

MAX_LOCK_WAIT_SECONDS = 60.0
OWNER_ACCESS_LOST = "owner_access_lost"
ENVIRONMENT_MISSING = "environment_missing"
ENVIRONMENT_MISSING_MESSAGE = "This automation has no environment."
NO_ACTIVITY_EVENT = "no_activity_event"
DRY_RUN_TOOK_LOCK = "dry_run_took_lock"
DRY_RUN_WROTE_EVENT = "dry_run_wrote_event"
MANUAL_TRIGGERS = frozenset({"manual", "dry_run"})
MAX_FALLBACK_CHECKS: Final = 25

type Standing = Literal["writable", "readable", "hidden"]


@dataclass(frozen=True, slots=True)
class Standings:
    """The owner's standing over a batch of containers.

    `not_checked` names the rows whose listing labels were cut and whose per-object check was
    over the allowance; they have no entry in `by_id`. `fallbacks` counts the checks made and
    `last_checked` is the last of them taken.
    """

    by_id: Mapping[str, Standing]
    not_checked: tuple[str, ...] = ()
    fallbacks: int = 0
    last_checked: str | None = None


class FallbackCursors:
    """Where each rule's previous run stopped checking cut-label containers one by one."""

    def __init__(self) -> None:
        self._after: dict[int, str] = {}

    def after(self, rule_id: int) -> str | None:
        """Return the id the rule's next run resumes after, or None to start from the lowest."""
        return self._after.get(rule_id)

    def advance(self, rule_id: int, last_checked: str | None) -> None:
        """Remember the last id a run checked; None leaves the cursor where it was."""
        if last_checked is not None:
            self._after[rule_id] = last_checked


def suppression_skip(acted_at: datetime) -> RunSkipped:
    """Return the skip a run is recorded under when a user acted on its object recently."""
    return RunSkipped(
        "suppressed",
        "A user acted on the object recently.",
        {"acted_at": format_timestamp(acted_at)},
    )


_ENVIRONMENT_FAILURES: Final[Mapping[UnavailableKind, str]] = MappingProxyType(
    {
        "engine": "engine_error",
        "credentials": "environment_credentials",
        "unsupported": "environment_unsupported",
    }
)


def environment_outcome(error: EnvironmentUnavailable) -> RunFailed | RunSkipped:
    """Return how a run ends without its environment.

    A host that answers wrongly, or whose stored connection or API version cannot be used, fails
    the run, since only a person can fix it. A host that cannot be reached, or is not a Swarm
    manager, skips it with the kind in the detail.
    """
    reason = _ENVIRONMENT_FAILURES.get(error.kind)
    if reason is not None:
        return RunFailed(reason, str(error))
    return RunSkipped("environment_unreachable", str(error), {"kind": error.kind})


class RuleParameters(BaseModel):
    """Parameters every rule accepts; a subsystem's own model adds to these and holds no secret."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    suppression_seconds: int | None = Field(None, ge=0, le=3600)
    max_consecutive_failures: int | None = Field(None, ge=1, le=100)


@dataclass(frozen=True, slots=True)
class SubsystemDeps:
    """What a subsystem is built from."""

    database: Database
    engine: EngineRuntime
    auth: AuthRuntime
    settings: Settings
    suppressions: SuppressionRegistry
    registries: RegistryRuntime | None = None
    backup_production: BackupProduction | None = None


@dataclass(frozen=True, slots=True)
class TargetPreview:
    """What a selector matches now, as one viewer may see it.

    `names` are the matched objects the viewer can read, `others` counts the readable matches
    beyond the names listed, and `note` carries a fact about the match that the names do not.
    Objects the viewer cannot read are in neither.
    """

    names: tuple[str, ...]
    others: int
    note: str | None = None


class DryRunGuard:
    """Remembers the first thing a dry run did that a dry run must not."""

    def __init__(self) -> None:
        self.violation: tuple[str, str] | None = None

    def violate(self, reason: str, message: str) -> NoReturn:
        """Record the violation if none is recorded yet, and fail the run."""
        if self.violation is None:
            self.violation = (reason, message)
        raise RunFailed(reason, message)


class _DryRunLocks(ObjectLockRegistry):
    """A lock registry that refuses every lock, for a run that must change nothing."""

    def __init__(self, guard: DryRunGuard) -> None:
        super().__init__()
        self._guard = guard

    def hold(self, key: ObjectKey, holder: LockHolder, *, wait: float = 0.0) -> NoReturn:
        """Refuse: a dry run takes no object lock."""
        self._guard.violate(DRY_RUN_TOOK_LOCK, "A dry run must not take an object lock.")


class RuleEventRecorder(ActivityRecorder):
    """The recorder a run writes through; a dry run's recorder refuses every write."""

    def __init__(self, db: Session, clock: Clock) -> None:
        super().__init__(db, clock)
        self._guard: DryRunGuard | None = None
        self.wrote_unlinked = False

    def bind(self, guard: DryRunGuard | None) -> None:
        """Set the guard a dry run's writes trip."""
        self._guard = guard

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
        if self._guard is not None:
            self._guard.violate(DRY_RUN_WROTE_EVENT, "A dry run must not record activity events.")
        await super()._insert(
            actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            environment_id=environment_id,
            result=result,
            detail=detail,
            category=category,
            unlinked=unlinked,
        )
        if unlinked:
            self.wrote_unlinked = True


class OwnerGate:
    """A rule owner's authority, usable only to check access to one object."""

    __slots__ = ("__authoriser", "__current", "__may_use_credential", "__principal")

    def __init__(
        self,
        principal: Principal,
        authoriser: Authoriser | None,
        current: Callable[[], Awaitable[bool]],
        may_use_credential: Callable[[int], Awaitable[bool]] | None = None,
    ) -> None:
        self.__principal = principal
        self.__authoriser = authoriser
        self.__current = current
        self.__may_use_credential = may_use_credential

    @property
    def is_administrator(self) -> bool:
        """Report the owner's role when the run started."""
        return self.__principal.is_administrator

    async def authorise(self, key: ObjectKey) -> None:
        """Raise unless the owner, as they are now, may write to the object."""
        await self._require(key, "write")

    async def authorise_read(self, key: ObjectKey) -> None:
        """Raise unless the owner, as they are now, may read the object."""
        await self._require(key, "read")

    async def authorise_credential_set(self, set_id: int) -> None:
        """Raise unless the owner, as they are now, may pull with the credential set.

        Administrators need nothing further; a standard owner needs the grant a pull by that user
        needs.
        """
        if not await self.__current():
            raise PermissionDenied("insufficient access")
        if self.__principal.is_administrator:
            return
        may_use = self.__may_use_credential
        if may_use is None or not await may_use(set_id):
            raise CredentialSetUnavailable("not_granted_user")

    async def _require(self, key: ObjectKey, level: Literal["read", "write"]) -> None:
        principal = self.__principal
        authoriser = self.__authoriser
        if not await self.__current():
            raise PermissionDenied("insufficient access")
        if principal.is_administrator and authoriser is None:
            return
        if authoriser is None or key.object_type not in RESOURCE_TYPES:
            raise PermissionDenied("insufficient access")
        authoriser.forget_decisions()
        await authoriser.require_access(
            principal,
            ResourceKey(key.environment_id, key.object_type, key.object_id),
            level,
        )

    async def standing(self, key: ObjectKey) -> Standing:
        """Say whether the owner may write to the object, only read it, or cannot see it."""
        try:
            await self.authorise(key)
        except (PermissionDenied, ObjectTooLarge):
            return "readable"
        except ResourceNotFound:
            return "hidden"
        return "writable"

    async def standings(
        self,
        environment_id: int,
        rows: Sequence[ContainerSummary],
        *,
        after: str | None,
        allowance: int = MAX_FALLBACK_CHECKS,
    ) -> Standings:
        """Return the owner's standing over listed containers, reading the owner once.

        Rows with complete listing labels are checked together from them. A row whose labels were
        cut needs its own check, at most `allowance` of them, taken in id order starting after
        `after` and wrapping; the rest come back in `not_checked`.
        """
        principal, authoriser = self.__principal, self.__authoriser
        if not await self.__current():
            return Standings({row.id: "readable" for row in rows})
        if principal.is_administrator:
            return Standings({row.id: "writable" for row in rows})
        if authoriser is None:
            return Standings({row.id: "readable" for row in rows})
        whole = [
            row for row in rows if not row.labels_truncated and is_canonical_id("container", row.id)
        ]
        refs = [ResourceRef(environment_id, "container", row.id, row.labels) for row in whole]
        authoriser.forget_decisions()
        writable = {
            ref.resource_id for ref in await authoriser.filter_visible(principal, refs, "write")
        }
        rest = [ref for ref in refs if ref.resource_id not in writable]
        readable = {
            ref.resource_id for ref in await authoriser.filter_visible(principal, rest, "read")
        }
        by_id: dict[str, Standing] = {
            ref.resource_id: "writable"
            if ref.resource_id in writable
            else "readable"
            if ref.resource_id in readable
            else "hidden"
            for ref in refs
        }
        complete = {row.id for row in whole}
        cut = sorted({row.id for row in rows if row.id not in complete})
        resume = 0 if after is None else sum(1 for container_id in cut if container_id <= after)
        ordered = [*cut[resume:], *cut[:resume]]
        checked, skipped = ordered[: max(allowance, 0)], ordered[max(allowance, 0) :]
        for container_id in checked:
            by_id[container_id] = await self.standing(
                ObjectKey(environment_id, "container", container_id)
            )
        return Standings(by_id, tuple(skipped), len(checked), checked[-1] if checked else None)


@dataclass(slots=True)
class RunContext:
    """Everything one run of a subsystem works with; db belongs to the run alone.

    The rule owner's principal is deliberately absent: a run acts as its subsystem and asks
    authorise_object whether the owner may write to an object before it acts on it.
    """

    dispatch: Dispatch
    rule: AutomationRule | None
    parameters: RuleParameters | None
    actor: SubsystemActor
    db: Session
    activity: ActivityRecorder
    now: datetime
    dry_run: bool
    locks: ObjectLockRegistry
    suppressions: SuppressionRegistry
    clock: Clock
    lock_wait: float
    suppression_window: timedelta
    guard: DryRunGuard | None = field(default=None, repr=False)
    owner_gate: OwnerGate | None = field(default=None, repr=False)
    fallback_checks: int = field(default=0, init=False, repr=False)

    def hold(self, key: ObjectKey, action: str) -> AbstractAsyncContextManager[None]:
        """Hold the object's lock for the block, waiting for a user's action to finish.

        Raises RunSkipped("suppressed") on entry if a user acted on the object while the run
        waited, so a run never acts straight after the action it queued behind.
        """
        if self.db.in_transaction:
            raise RuntimeError("an object lock is not taken inside a transaction")
        return self._hold_unless_suppressed(key, action)

    @asynccontextmanager
    async def _hold_unless_suppressed(self, key: ObjectKey, action: str) -> AsyncIterator[None]:
        holder = LockHolder(self.actor, self.actor.name, action)
        async with self.locks.hold(key, holder, wait=self.lock_wait):
            acted_at = await self.suppressed(key)
            if acted_at is not None:
                raise suppression_skip(acted_at)
            yield

    def authority(self) -> AutomationAuthority:
        """Return what an audited service needs to act for this run."""
        gate = self.owner_gate
        return AutomationAuthority(
            actor=self.actor,
            authorise=self.authorise_object,
            hold=self.hold,
            lock_wait=self.lock_wait,
            authorise_read=self.authorise_read_object,
            owner_is_administrator=gate is not None and gate.is_administrator,
            authorise_credential_set=self.authorise_credential_set,
        )

    async def authorise_object(self, key: ObjectKey) -> None:
        """Raise PermissionDenied or ResourceNotFound unless the rule's owner may write to it.

        Call before acting on each object a selector matched; the check reads the owner's
        access as it is now.
        """
        if self.owner_gate is None:
            raise RuntimeError("this run has no rule owner to authorise against")
        await self.owner_gate.authorise(key)

    async def authorise_read_object(self, key: ObjectKey) -> None:
        """Raise PermissionDenied or ResourceNotFound unless the rule's owner may read it."""
        if self.owner_gate is None:
            raise RuntimeError("this run has no rule owner to authorise against")
        await self.owner_gate.authorise_read(key)

    async def authorise_credential_set(self, set_id: int) -> None:
        """Raise CredentialSetUnavailable unless the rule's owner may pull with the set."""
        if self.owner_gate is None:
            raise RuntimeError("this run has no rule owner to authorise against")
        await self.owner_gate.authorise_credential_set(set_id)

    async def standing(self, key: ObjectKey) -> Standing:
        """Say whether the rule's owner may write to the object, only read it, or cannot see it."""
        if self.owner_gate is None:
            raise RuntimeError("this run has no rule owner to authorise against")
        return await self.owner_gate.standing(key)

    async def standings(
        self, environment_id: int, rows: Sequence[ContainerSummary], *, cursors: FallbackCursors
    ) -> Standings:
        """Say what the rule's owner may do to each listed container, within the run's allowance.

        Per-object checks for rows with cut labels are capped across every call of the run and
        resume where the rule's previous run stopped.
        """
        gate = self.owner_gate
        if gate is None:
            raise RuntimeError("this run has no rule owner to authorise against")
        rule, _ = self.rule_scope()
        found = await gate.standings(
            environment_id,
            rows,
            after=cursors.after(rule.id),
            allowance=MAX_FALLBACK_CHECKS - self.fallback_checks,
        )
        self.fallback_checks += found.fallbacks
        cursors.advance(rule.id, found.last_checked)
        return found

    def reportable_not_checked(self, found: Standings) -> int:
        """Return how many unchecked containers run detail may state; otherwise log the count.

        The count of rows an owner could not be checked against reveals containers the owner
        cannot see, so only an administrator's run reports it.
        """
        count = len(found.not_checked)
        gate = self.owner_gate
        if gate is not None and gate.is_administrator:
            return count
        if count:
            logger.info(
                "owner_rows_not_checked",
                extra={"rule_id": None if self.rule is None else self.rule.id, "count": count},
            )
        return 0

    def rule_scope(self) -> tuple[AutomationRule, int]:
        """Return the rule and its environment; RunFailed when the rule has none."""
        rule = self.rule
        if rule is None or rule.environment_id is None:
            raise RunFailed(ENVIRONMENT_MISSING, ENVIRONMENT_MISSING_MESSAGE)
        return rule, rule.environment_id

    async def suppressed(self, key: ObjectKey) -> datetime | None:
        """Return when a user last acted on the object if that is inside the window, else None."""
        at = await self.suppressions.lookup(key, self.db)
        if at is None:
            return None
        window = self.suppression_window
        if self.parameters is not None and self.parameters.suppression_seconds is not None:
            window = timedelta(seconds=self.parameters.suppression_seconds)
        return at if self.clock() - at < window else None


class Subsystem(ABC):
    """A named automation actor: declares its policies and implements run."""

    name: ClassVar[str]
    parameters_model: ClassVar[type[RuleParameters] | None] = None
    event_policy: ClassVar[EventPolicy] = "per_action"
    schedule_kinds: ClassVar[frozenset[ScheduleKind]] = frozenset({"cron", "interval"})
    min_interval_seconds: ClassVar[int] = MIN_RULE_INTERVAL_SECONDS
    overrun_policy: ClassVar[OverrunPolicy] = "skip"
    allowed_overrun_policies: ClassVar[frozenset[OverrunPolicy]] = frozenset({"skip", "queue"})
    cancellation_safe: ClassVar[bool] = False
    catch_up: ClassVar[bool] = False
    run_timeout: ClassVar[timedelta] = timedelta(minutes=30)
    lock_wait: ClassVar[float] = 10.0
    max_concurrent_runs: ClassVar[int | None] = None
    suppression_window: ClassVar[timedelta] = timedelta(minutes=5)
    failure_threshold: ClassVar[int | None] = 5
    requires_environment: ClassVar[bool] = False
    administrators_only: ClassVar[bool] = False
    supports_dry_run: ClassVar[bool] = False
    target_kinds: ClassVar[frozenset[SelectorKind]] = frozenset()
    stack_kinds: ClassVar[frozenset[StackKind]] = frozenset({"compose"})

    def __init__(self, deps: SubsystemDeps) -> None:
        if self.name not in SUBSYSTEMS:
            raise ValueError(f"{self.name!r} is not a registered subsystem")
        if (self.parameters_model is not None) != SUBSYSTEMS[self.name]:
            raise ValueError(f"{self.name} parameters disagree with the subsystem registry")
        if not 0 <= self.lock_wait <= MAX_LOCK_WAIT_SECONDS:
            raise ValueError("lock_wait is between 0 and 60 seconds")
        if self.overrun_policy not in self.allowed_overrun_policies:
            raise ValueError("overrun_policy is not in allowed_overrun_policies")
        if SUBSYSTEMS[self.name] and type(self).authorise_target is Subsystem.authorise_target:
            raise TypeError(f"{self.name} has rules, so it implements authorise_target")
        self._deps = deps

    @property
    def records_events(self) -> bool:
        """Report whether runs of this subsystem write activity events."""
        return self.event_policy != "none"

    @abstractmethod
    async def run(self, context: RunContext) -> RunResult:
        """Do the work of one run."""

    def parse_selector(self, text: str) -> str:
        """Validate a rule's target selector and return it in canonical form."""
        if text.strip():
            raise ValidationFailed({"target_selector": "This automation does not take a selector."})
        return ""

    async def authorise_target(
        self,
        principal: Principal,
        environment_id: int | None,
        selector: str,
        authoriser: Authoriser,
    ) -> None:
        """Raise unless the principal may act on what the selector names.

        A subsystem with rules overrides this; the default refuses.
        """
        raise PermissionDenied("insufficient access")

    async def preview_targets(
        self, principal: Principal, environment_id: int, selector: str
    ) -> TargetPreview | None:
        """Describe what the selector matches now for the viewer; None when there is no preview."""
        return None

    @final
    async def dispatch(self, dispatch: Dispatch) -> RunResult:
        """Run the job once: re-authorise the owner, call run, and check the events it left."""
        async with self._deps.database.session() as db:
            activity = RuleEventRecorder(db, self._deps.auth.clock)
            context = await self._prepare(db, activity, dispatch)
            if isinstance(context, RunResult):
                return context
            try:
                result = await self.run(context)
            except ObjectBusy as busy:
                result = _busy_result(busy, self._deps.engine.locks.now())
            except (RunSkipped, RunFailed) as ended:
                result = _result_of(ended)
            if context.guard is not None and context.guard.violation is not None:
                reason, message = context.guard.violation
                return RunResult("failure", reason=reason, message=message)
            return await self._check_events(context, activity, result)

    async def _prepare(
        self, db: Session, activity: RuleEventRecorder, dispatch: Dispatch
    ) -> RunContext | RunResult:
        job = dispatch.job
        clock = self._deps.auth.clock
        rule: AutomationRule | None = None
        parameters: RuleParameters | None = None
        gate: OwnerGate | None = None
        if SUBSYSTEMS[self.name]:
            if job.rule_id is not None:
                rule = await AutomationRuleRepository(db, clock).get(job.rule_id)
            if rule is None:
                return _refusal("rule_missing", "The automation for this job no longer exists.")
            if dispatch.run.trigger not in MANUAL_TRIGGERS:
                if not rule.is_enabled:
                    return _skipped("rule_disabled", "The automation is disabled.")
                if rule.paused_until is not None and parse_timestamp(rule.paused_until) > clock():
                    return _skipped("rule_paused", "The automation is paused.")
            if self.requires_environment and rule.environment_id is None:
                return _refusal(ENVIRONMENT_MISSING, ENVIRONMENT_MISSING_MESSAGE)
            try:
                parameters = self._parse_parameters(rule.parameters)
            except ValidationError:
                return _refusal("parameters_invalid", "The automation's parameters are not valid.")
            try:
                gate = await self._owner_gate(db, activity, rule)
            except EnvironmentUnavailable as unavailable:
                return _result_of(environment_outcome(unavailable))
            except ValidationFailed:
                return _refusal("target_invalid", "The automation's target is not valid.")
            except ObjectTooLarge as too_large:
                return _refusal(reason_of(too_large), str(too_large))
            if gate is None:
                return _refusal(
                    OWNER_ACCESS_LOST, "The automation's owner no longer has access to run it."
                )
        guard = DryRunGuard() if dispatch.dry_run else None
        activity.bind(guard)
        return RunContext(
            dispatch=dispatch,
            rule=rule,
            parameters=parameters,
            actor=SubsystemActor(
                self.name, job.rule_id, dispatch.run.id, None if rule is None else rule.name
            ),
            db=db,
            activity=activity,
            now=dispatch.now,
            dry_run=dispatch.dry_run,
            locks=self._deps.engine.locks if guard is None else _DryRunLocks(guard),
            suppressions=self._deps.suppressions,
            clock=clock,
            lock_wait=self.lock_wait,
            suppression_window=self.suppression_window,
            guard=guard,
            owner_gate=gate,
        )

    def _parse_parameters(self, text: str) -> RuleParameters | None:
        if self.parameters_model is None:
            return None
        return self.parameters_model.model_validate_json(text)

    async def _owner_gate(
        self, db: Session, activity: ActivityRecorder, rule: AutomationRule
    ) -> OwnerGate | None:
        if rule.created_by_user_id is None:
            return None
        user = await UserRepository(db, self._deps.auth.clock).get(rule.created_by_user_id)
        if user is None or not user.is_enabled:
            return None
        principal = Principal(user_id=user.id, username=user.username, role=user.role)
        if self.administrators_only and not principal.is_administrator:
            return None
        users = UserRepository(db, self._deps.auth.clock)

        async def current() -> bool:
            found = await users.get(user.id)
            return found is not None and found.is_enabled and found.role == user.role

        if principal.is_administrator:
            return OwnerGate(principal, None, current)
        if rule.environment_id is None:
            return None
        authoriser = self._authoriser(db, activity)
        try:
            await authoriser.require_create(principal, rule.environment_id)
        except (NotFound, PermissionDenied):
            return None
        environment_id = rule.environment_id
        environments = EnvironmentAccessRepository(db)
        registries = RegistryAccessRepository(db)

        async def may_use_credential(set_id: int) -> bool:
            return await environments.gate(
                user.id, environment_id
            ) is not None and await registries.user_may_use(set_id, user.id)

        return OwnerGate(principal, authoriser, current, may_use_credential)

    def _authoriser(self, db: Session, activity: ActivityRecorder) -> Authoriser:
        # Imported here because the assembly module imports the services that import this one.
        from app.services.assembly import build_access_services, build_host_clients

        engine, auth = self._deps.engine, self._deps.auth
        inspector = EngineObjectInspector(
            build_host_clients(db, auth, engine), poke=engine.refresher.poke
        )
        return build_access_services(db, auth, inspector, activity).authoriser

    async def _check_events(
        self, context: RunContext, activity: RuleEventRecorder, result: RunResult
    ) -> RunResult:
        if context.dry_run or not self._event_required(result):
            return result
        run_id = context.dispatch.run.id
        if await ActivityEventRepository(context.db).count_for_run(run_id) > 0:
            return result
        # Deleting the rule detaches the run's events, so unlinked rows do not count under its id.
        if activity.wrote_unlinked:
            return result
        logger.error(
            "subsystem_event_missing",
            extra={"subsystem": self.name, "rule_id": context.actor.rule_id, "run_id": run_id},
        )
        return RunResult(
            "failure",
            attempted=result.attempted,
            reason=NO_ACTIVITY_EVENT,
            message="The run acted without recording an activity event.",
            detail=result.detail,
        )

    def _event_required(self, result: RunResult) -> bool:
        match self.event_policy:
            case "none":
                return False
            case "failures_only":
                return result.outcome in ("failure", "partial")
            case _:
                return result.attempted > 0


type SubsystemFactory = Callable[[SubsystemDeps], Subsystem]


def _result_of(ended: RunFailed | RunSkipped) -> RunResult:
    if isinstance(ended, RunSkipped):
        return RunResult("skipped", reason=ended.reason, message=ended.message, detail=ended.detail)
    return RunResult("failure", reason=ended.reason, message=ended.message)


def _refusal(reason: str, message: str) -> RunResult:
    return RunResult("failure", reason=reason, message=message)


def _skipped(reason: str, message: str) -> RunResult:
    return RunResult("skipped", reason=reason, message=message)


def _busy_result(busy: ObjectBusy, now: datetime) -> RunResult:
    holder = busy.holder
    return RunResult(
        "skipped",
        reason="object_busy",
        message="The object was busy with another action.",
        detail={
            "holder_type": "user" if isinstance(holder.actor, UserActor) else "subsystem",
            "holder_label": holder.label,
            "holder_action": holder.action,
            "holder_age_seconds": int(holder.age_seconds(now)),
        },
    )
