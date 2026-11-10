"""container-updater: pulls newer images at bound references and redeploys what runs them."""

from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, ClassVar, Final, Literal

from pydantic import Field

from app.auth.errors import (
    SELF_PROTECTED_REASON,
    PermissionDenied,
    ProductError,
    ResourceNotFound,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.db.repositories.activity_event import ActivityEventRepository, DetailValue
from app.db.repositories.clocked import parse_timestamp
from app.db.repositories.job_run import MAX_RUN_DETAIL_KEYS
from app.db.repositories.scheduled_job import OverrunPolicy, ScheduleKind
from app.db.repositories.stack import StackKind, StackRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError
from app.docker_engine.models import ContainerSummary
from app.logging import get_logger
from app.scheduler.model import RunFailed, RunResult, RunSkipped, reason_of
from app.self_identity import NOT_CONTAINERISED, SelfIdentity, SelfIdentitySource, no_identity
from app.services.access import (
    COMPOSE_PROJECT_LABEL,
    ResourceKey,
    ResourceRef,
    canonical_stack_key,
    is_canonical_id,
)
from app.services.acting import AutomationAuthority
from app.services.activity import ActivityRecorder
from app.services.assembly import build_host_clients
from app.services.authorisation import Authoriser
from app.services.container_derivation import SWARM_SERVICE_LABEL
from app.services.container_provisioning import RecreateFailed, VerifyPolicy
from app.services.container_recreate import INTERRUPTED_FAILURE, ContainerRecreateService
from app.services.container_review import RecreateOutcome
from app.services.containers import display_name
from app.services.image_bindings import bare_image_id, parse_pull_reference
from app.services.image_queries import automation_plan
from app.services.image_updates import (
    MAX_CHECK_REFERENCES,
    ImageUpdateService,
    UpdateStatus,
    is_image_id_reference,
)
from app.services.object_inspector import (
    EnvironmentUnavailable,
    HostClients,
    ObjectTooLarge,
    unavailable_from,
)
from app.services.object_locks import ObjectBusy, ObjectKey
from app.services.pull_credentials import (
    CredentialSetMismatch,
)
from app.services.registry_resolution import CredentialSetUnavailable
from app.services.replaced_images import PruneReport
from app.services.stack_build_plan import services_with_build
from app.services.stack_common import changed_since_deploy
from app.services.stack_deploy import StackRejected, in_project
from app.services.stack_queries import read_project_containers
from app.services.swarm_stack_plan import StackCredentialsRefused
from app.subsystems.acting_run import (
    NAMES_LISTING_LIMIT,
    PERMISSION_DENIED,
    ObjectStatus,
    ObjectTally,
    act_on,
    check_suppressed,
    failure_reason,
    forecast_busy,
    listing,
    permitted,
    rechecking,
    record_unwritable,
)
from app.subsystems.base import (
    FallbackCursors,
    RuleParameters,
    RunContext,
    Standing,
    Subsystem,
    SubsystemDeps,
    TargetPreview,
    environment_outcome,
)
from app.subsystems.run_services import RunServices, check_target, run_services
from app.subsystems.selectors import (
    ContainerTarget,
    LabelsTarget,
    NamedSelector,
    Selector,
    SelectorKind,
    ServiceTarget,
    StackTarget,
    canonical,
    compose_managed,
    find_containers,
    matches,
    only_name,
    parse_selector_text,
    term_text,
)

if TYPE_CHECKING:
    from app.subsystems.container_updater_swarm import SwarmPass

logger = get_logger(__name__)

NAME: Final = "container-updater"
REFUSED_ACTION: Final = "image_update_refused"
ROLLED_BACK_ACTION: Final = "container_update_rolled_back"
RECREATED_ACTION: Final = "container_recreated"
REDEPLOYED_ACTION: Final = "stack_redeployed"
TRIGGER: Final = "update"
TARGET_MISSING: Final = "The automation's target was not found."
COMPOSE_UNAVAILABLE: Final = "Compose is not available."
MAX_EXAMINED: Final = 200
MAX_REFERENCES: Final = 100
MAX_REDEPLOYS: Final = 25
MAX_PULLS: Final = 25
MAX_PREVIEW_NAMES: Final = 20
START_BUDGET: Final = timedelta(minutes=30)
SIGNATURE_LIMIT: Final = 400
STACK_SKIPS: Final = frozenset({"pending_changes", "partly_stopped", "not_running", "not_deployed"})
REFRESHED: Final = "refreshed"
DEFERRED: Final = "deferred"
PERMANENT_REFUSALS: Final = frozenset({"restricted_option", "not_recreatable", "invalid"})
CHECK_REFUSALS: Final = {
    "authentication_required": "authentication_required",
    "private_address": "private_address",
    "credential_mismatch": "credential_mismatch",
    "credential_not_available": "credential_unavailable",
}
UNCHECKABLE: Final = frozenset({"no_local_digest"})
# Stored refusal rows can still name these; they hold nothing because neither is refused any more.
RETIRED_REFUSALS: Final = frozenset({"not_bound", "credential_missing"})

COMPOSE_REFUSED_FOR_GOOD: Final = frozenset({"restricted", "refused_construct", "invalid"})
CREDENTIAL_REJECTIONS: Final = {"credential_unavailable": "credential_unavailable"}

type Verdict = Literal["update_available", "local_newer"]
type PlanReason = Verdict | Literal["redeploy"]


@dataclass(frozen=True, slots=True)
class PassSettings:
    """Who a pass acts as and what it leaves out.

    `subsystem` and `trigger` name the actor whose holds are read and the trigger noted on every
    row; `notes` are added to each note. `redeploy` recreates the targets from their own
    configuration with no registry request. `detail` is added to the result; `mode` replaces the
    parameters' action in it.
    """

    subsystem: str = NAME
    trigger: str = TRIGGER
    notes: Mapping[str, DetailValue] = field(default_factory=dict)
    refresh: bool = True
    redeploy: bool = False
    mode: str = ""
    detail: Mapping[str, DetailValue] = field(default_factory=dict)


class UpdaterParameters(RuleParameters):
    """Whether the rule only reports newer images or applies them, and how it watches the result."""

    action: Literal["report", "update"] = "report"
    verify_seconds: int = Field(30, ge=0, le=300)
    require_healthy: bool = True
    prune_replaced: bool = False
    prune_after_hours: int = Field(24, ge=1, le=720)


class _Refusal(ProductError):
    """An image cannot be pulled with the credentials it has, so its update stops; never retried."""

    reason: ClassVar[str]
    message: ClassVar[str]

    def __init__(self) -> None:
        super().__init__(self.message)


class AuthenticationRequired(_Refusal):
    """The registry requires credentials and the image is bound to none."""

    reason = "authentication_required"
    message = "The registry requires authentication. Bind the image to a credential set."


class PrivateAddress(_Refusal):
    """The registry is on a private network and no entry for its address is granted."""

    reason = "private_address"
    message = (
        "The registry is on a private network. Add a registry entry for its address, with "
        "authentication off if it needs none."
    )


class CredentialMismatch(_Refusal):
    """The bound credential set belongs to another registry address."""

    reason = "credential_mismatch"
    message = "The bound credential set belongs to another registry address."


class CredentialUnavailable(_Refusal):
    """The bound credential set is not available to the rule's owner here."""

    reason = "credential_unavailable"
    message = "The bound credential set is not available to this automation's owner here."


class Deferred(ProductError):
    """The run's start budget ended before this object's work began."""

    reason = DEFERRED

    def __init__(self) -> None:
        super().__init__("The run's start budget ended before this object was reached.")


REFUSALS: Final[dict[str, type[_Refusal]]] = {
    cls.reason: cls
    for cls in (AuthenticationRequired, PrivateAddress, CredentialMismatch, CredentialUnavailable)
}


class _Tally(ObjectTally):
    """The run's tally, keeping skip reasons apart so the run detail stays within its key limit."""

    def __init__(self, *, done_name: str = "updated", plan_name: str = "would_update") -> None:
        super().__init__(done_name=done_name, plan_name=plan_name)
        self.skips: Counter[str] = Counter()
        self.refreshed = 0

    def record(
        self,
        status: ObjectStatus,
        *,
        attempted: bool,
        name: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Count one object; a stack's refused state is a skip and an unreached object deferred.

        A refresh is recorded as a skipped object whose reason is `REFRESHED`, so it is counted
        apart from the updates.
        """
        if status == "failed" and reason in STACK_SKIPS:
            status = "skipped"
        elif status == "failed" and reason == DEFERRED:
            status = "deferred"
        if status == "skipped" and reason == REFRESHED:
            self.note(status, reason)
            self.refreshed += 1
            if attempted:
                self._attempted += 1
            return
        if status == "skipped":
            self.note(status, reason)
            self.skips[reason or "skipped"] += 1
            if attempted:
                self._attempted += 1
            return
        super().record(status, attempted=attempted, name=name, reason=reason)

    def count(
        self, status: ObjectStatus, *, name: str | None = None, reason: str | None = None
    ) -> None:
        """Count one object that wrote no event."""
        self.record(status, attempted=False, name=name, reason=reason)

    def skip(self, reason: str) -> None:
        """Count one object left alone, with the reason."""
        self.note("skipped", reason)
        self.skips[reason] += 1


@dataclass(frozen=True, slots=True)
class _Candidate:
    row: ContainerSummary
    key: ObjectKey
    standing: Standing


@dataclass(frozen=True, slots=True)
class _Seen:
    container_id: str
    key: ObjectKey
    name: str
    reference: str
    image_id: str | None


@dataclass(frozen=True, slots=True)
class _Pull:
    reference: str
    display: str
    remote_digest: str | None


@dataclass(frozen=True, slots=True)
class _Plan:
    kind: Literal["container", "stack"]
    key: ObjectKey
    target_id: str
    name: str
    reason: PlanReason | None = None
    reference: str = ""
    digest: str | None = None
    image_before: str | None = None
    image_after: str | None = None
    pulls: tuple[_Pull, ...] = ()
    refusals: tuple[tuple[str, str], ...] = ()
    members: tuple[ObjectKey, ...] = ()
    updated: int = 0


@dataclass(slots=True)
class _Pulled:
    digest: str | None
    image_id: str | None
    error: BaseException | None = None


def refusal_signature(refusals: Sequence[tuple[str, str]]) -> str:
    """Return the text that says which references were refused and why; equal text, same refusal."""
    return ",".join(f"{reference}={reason}" for reference, reason in sorted(refusals))[
        :SIGNATURE_LIMIT
    ]


async def record_refusal(
    activity: ActivityRecorder,
    authority: AutomationAuthority,
    *,
    environment_id: int,
    target_type: str,
    target_id: str,
    name: str,
    reference: str,
    reason: str,
    refused: str | None = None,
) -> None:
    """Write the row that holds an object whose bound credentials cannot pull its image."""
    await activity.record_failure(
        authority.actor,
        action=REFUSED_ACTION,
        target_type=target_type,
        target_id=target_id,
        environment_id=environment_id,
        reason=reason,
        detail={
            "name": name,
            "reference": reference[:SIGNATURE_LIMIT],
            "refused": refused if refused is not None else refusal_signature([(reference, reason)]),
        },
        message=REFUSALS[reason].message,
    )


async def record_rolled_back(
    activity: ActivityRecorder,
    authority: AutomationAuthority,
    *,
    environment_id: int,
    container_id: str,
    detail: dict[str, DetailValue],
) -> None:
    """Write the row that holds a container whose update failed and was undone."""
    await activity.record_event(
        authority.actor,
        action=ROLLED_BACK_ACTION,
        target_type="container",
        target_id=container_id,
        environment_id=environment_id,
        result="success",
        detail={key: value for key, value in detail.items() if value is not None},
    )


def _refusal_reason(status: UpdateStatus) -> str | None:
    if status.state == "unknown":
        return CHECK_REFUSALS.get(status.reason or "")
    return None


def check_failure(status: UpdateStatus) -> str | None:
    """Return why a check reached no verdict, or None when it did or could not be made."""
    if status.state == "unknown" and status.reason not in UNCHECKABLE:
        return status.reason
    return None


def credential_refusals(rejected: StackRejected) -> tuple[tuple[str, str], ...]:
    """Return, sorted, the references a redeploy refused for their credentials and why."""
    if isinstance(rejected, StackCredentialsRefused):
        return tuple(
            sorted(
                (reference, reason)
                for reference, reason in rejected.detail["refused_references"].items()
                if reason in REFUSALS
            )
        )
    reason = CREDENTIAL_REJECTIONS.get(rejected.reason)
    if reason is None:
        return ()
    return tuple(
        sorted((name, reason) for name in rejected.fields if not name.startswith("services."))
    )


def _pull_refusal(error: Exception) -> _Refusal | None:
    if isinstance(error, CredentialSetMismatch):
        return CredentialMismatch()
    if isinstance(error, CredentialSetUnavailable):
        return CredentialUnavailable()
    return None


@dataclass(slots=True)
class _Pass:
    context: RunContext
    deps: SubsystemDeps
    services: RunServices
    updates: ImageUpdateService
    recreation: ContainerRecreateService
    params: UpdaterParameters
    selector: Selector
    environment_id: int
    rule_id: int
    rotation: int
    cursors: FallbackCursors
    started: datetime
    settings: PassSettings = field(default_factory=PassSettings)
    tally: _Tally = field(default_factory=_Tally)
    counts: Counter[str] = field(default_factory=Counter)
    seen: list[_Seen] = field(default_factory=list)
    plans: list[_Plan] = field(default_factory=list)
    pulled: dict[str, _Pulled] = field(default_factory=dict)
    available: list[str] = field(default_factory=list)
    would_pull: list[str] = field(default_factory=list)
    matched: int = 0
    swarm: "SwarmPass | None" = None
    self_identity: SelfIdentity = NOT_CONTAINERISED
    name_count: int = 0
    redeploys: int = 0
    origin: dict[ObjectKey, str] = field(default_factory=dict)
    acts_on_nothing: list[str] = field(default_factory=list)
    charged: set[str] = field(default_factory=set)
    not_checked: int = 0
    pruning: PruneReport | None = None

    @property
    def explicit(self) -> bool:
        return isinstance(self.selector, ContainerTarget | StackTarget | ServiceTarget)

    def note(self, **more: DetailValue) -> dict[str, DetailValue]:
        """Return the note for a row this pass writes: its trigger, its extras and `more`."""
        return {"trigger": self.settings.trigger, **self.settings.notes, **more}

    @property
    def single(self) -> bool:
        """Report whether the rule names exactly one object, whose refusals end the run."""
        return self.explicit and self.name_count == 1

    async def plan_named(self, whole: NamedSelector) -> None:
        """Plan each named target in turn; with several, one that is not found is only reported."""
        tally = self.tally
        tally.track_targets()
        self.name_count = len(whole.names)
        for name in whole.names:
            self.selector = only_name(whole, name)
            tally.begin(name)
            plans = len(self.plans)
            updates = refreshes = 0
            if self.swarm is not None:
                updates, refreshes = len(self.swarm.updates), len(self.swarm.refreshes)
            try:
                await self._plan_one()
            except RunFailed as failed:
                if self.single or failed.reason != "target_missing":
                    raise
                tally.missing(name)
                continue
            tally.resolved(name)
            for plan in self.plans[plans:]:
                self.origin[plan.key] = name
            if self.swarm is not None:
                for update in self.swarm.updates[updates:]:
                    self.origin[update.key] = name
                for refresh in self.swarm.refreshes[refreshes:]:
                    self.origin[refresh.key] = name
        self.selector = whole
        if tally.targets_found == 0:
            raise RunFailed("target_missing", TARGET_MISSING)

    async def _plan_one(self) -> None:
        if isinstance(self.selector, ServiceTarget):
            assert self.swarm is not None
            await self.swarm.plan_service()
        elif isinstance(self.selector, StackTarget):
            await self.plan_stack()
        else:
            await self.plan_containers()

    def fits_references(self, references: Collection[str]) -> bool:
        """Report whether the run can still check all of these references within its budget."""
        return len(self.charged | set(references)) <= MAX_REFERENCES

    def charge_references(self, references: Sequence[str]) -> list[str]:
        """Charge the references to the run's budget and return those that fit, in order.

        A reference already checked in this run costs nothing again.
        """
        kept: list[str] = []
        for reference in references:
            if reference in self.charged or len(self.charged) < MAX_REFERENCES:
                self.charged.add(reference)
                kept.append(reference)
        return kept

    async def suppressed(self, keys: Sequence[ObjectKey]) -> bool:
        """Report whether a user acted on any of the objects recently, counting it unless single.

        A rule naming one object raises RunSkipped instead, so the run is recorded as skipped.
        """
        for key in keys:
            if await check_suppressed(self.context, key, single=self.single, tally=self.tally):
                return True
        return False

    @property
    def planning_update(self) -> bool:
        return self.params.action == "update" or self.settings.redeploy or self.context.dry_run

    async def plan_containers(self) -> None:
        """Decide, for each matched running container, whether and how it is updated."""
        rows = await self._matched()
        candidates = await self._visible(rows)
        if rows and not candidates and self.explicit:
            raise RunFailed("target_missing", TARGET_MISSING)
        self.matched += len(candidates)
        await self._examine(candidates)
        await self._decide()

    async def plan_stack(self) -> None:
        """Decide whether the stack is redeployed, from the images of its containers or services."""
        context, tally = self.context, self.tally
        selector = self.selector
        if not isinstance(selector, StackTarget):
            raise RunFailed("selector_invalid", "The automation's target is not valid.")
        environment_id = self.environment_id
        row = await StackRepository(context.db, self.deps.engine.vault, context.clock).get_folded(
            environment_id, selector.names[0]
        )
        if row is not None and row.kind == "swarm" and self.swarm is not None:
            await self.swarm.plan_stack(row)
            return
        if row is None or row.kind != "compose":
            raise RunFailed("target_missing", TARGET_MISSING)
        key = ObjectKey(environment_id, "stack", canonical_stack_key(row.name))
        if await context.standing(key) == "hidden":
            raise RunFailed("target_missing", TARGET_MISSING)
        try:
            client = await self.services.hosts.client(environment_id)
            listed = await read_project_containers(client)
        except EngineError as error:
            raise unavailable_from(error) from None
        except ObjectTooLarge as error:
            self.matched += 1
            tally.count("failed", reason=failure_reason(error))
            return
        project = [c for c in listed if in_project(c.labels, key.object_id)]
        members = tuple(
            ObjectKey(environment_id, "container", c.id)
            for c in project
            if is_canonical_id("container", c.id)
        )
        self.matched += 1
        try:
            allowed = len(members) == len(project) and await permitted(context, key, *members)
        except ObjectTooLarge as error:
            tally.count("failed", reason=failure_reason(error))
            return
        if not allowed:
            tally.count("failed", reason=PERMISSION_DENIED)
            return
        identity = self.self_identity
        if identity.protects_stack(environment_id, row.name) or any(
            identity.protects_container(member.object_id) for member in members
        ):
            tally.skip(SELF_PROTECTED_REASON)
            return
        if await self.suppressed((key, *members)):
            return
        if not self.settings.redeploy and await services_with_build(row.compose_file):
            tally.skip("built_image")
            return
        if changed_since_deploy(
            row.compose_file_digest,
            row.deployed_file_digest,
            row.inputs_digest,
            row.deployed_inputs_digest,
        ):
            tally.skip("pending_changes")
            return
        running = [c for c in project if c.state == "running"]
        if not running:
            tally.skip("not_running")
            return
        if len(running) != len(project):
            tally.skip("partly_stopped")
            return
        if self.settings.redeploy:
            await self._plan_stack_redeploy(row.name, key, members)
            return
        await self._classify_stack(row.name, key, members, project)

    async def _plan_stack_redeploy(
        self, stack_name: str, key: ObjectKey, members: tuple[ObjectKey, ...]
    ) -> None:
        if await self.refused_for_good(
            "stack",
            stack_name,
            REDEPLOYED_ACTION,
            COMPOSE_REFUSED_FOR_GOOD,
            members=[("container", member.object_id) for member in members],
        ):
            self.tally.count("held")
            return
        self.plans.append(
            _Plan(
                "stack",
                key,
                stack_name,
                f"stack {stack_name}",
                reason="redeploy",
                members=members,
            )
        )

    async def carry_out(self) -> None:
        """Do what the plan holds in name order; a dry run only forecasts it."""
        if not self.planning_update:
            return
        context = self.context
        if self.swarm is not None and self.swarm.engaged:
            await self.swarm.carry_out()
        pulls: set[str] = set()
        for plan in sorted(self.plans, key=lambda planned: planned.name):
            self.tally.begin(self.origin.get(plan.key))
            if plan.reason is not None:
                needed = {pull.reference for pull in plan.pulls} - pulls
                over = self.redeploys >= MAX_REDEPLOYS or len(pulls) + len(needed) > MAX_PULLS
                if over or self.late():
                    self.tally.count("deferred")
                    continue
                self.redeploys += 1
                pulls |= needed
            if plan.kind == "stack" and plan.reason is not None and self._compose_missing():
                raise RunFailed("compose_unavailable", COMPOSE_UNAVAILABLE)
            if context.dry_run:
                self._forecast(plan)
                continue
            await act_on(
                context,
                plan.key,
                explicit=self.explicit,
                single=self.single,
                work=self._container_work(plan)
                if plan.kind == "container"
                else self._stack_work(plan),
                tally=self.tally,
                name=plan.name,
            )

    @property
    def prunes(self) -> bool:
        """Report whether the run removes the images its updates replaced."""
        return (
            self.params.prune_replaced
            and self.params.action == "update"
            and not isinstance(self.selector, ServiceTarget)
        )

    async def prune(self) -> None:
        """Remove replaced images whose grace period ended; a dry run only counts them.

        Runs after the updates and never once the start budget has ended.
        """
        replaced = self.services.replaced
        if replaced is None or not self.prunes or self.late():
            return
        rule, _ = self.context.rule_scope()
        try:
            self.pruning = await replaced.prune_due(
                self.context.authority(),
                rule,
                grace=timedelta(hours=self.params.prune_after_hours),
                suppressed=self.context.suppressed,
                dry_run=self.context.dry_run,
            )
        except Exception as error:
            logger.warning(
                "image_prune_phase_failed",
                extra={"rule_id": self.rule_id, "error": type(error).__name__},
            )
            self.pruning = PruneReport(failed=1)
            return
        if self.pruning.stopped is not None:
            logger.info(
                "image_prune_stopped",
                extra={"rule_id": self.rule_id, "reason": self.pruning.stopped},
            )

    def late(self) -> bool:
        """Report whether the run's start budget has ended, so no further work begins."""
        context = self.context
        return not context.dry_run and context.clock() - self.started >= START_BUDGET

    def require_time(self) -> None:
        """Raise Deferred once the start budget has ended."""
        if self.late():
            raise Deferred()

    def result(self) -> RunResult:
        """Build the run's result with its counts, within the run detail's key limit."""
        base = self.tally.result(
            dry_run=self.context.dry_run,
            extra={"acts_on_nothing": listing(self.acts_on_nothing, NAMES_LISTING_LIMIT, "; ")},
        )
        detail: dict[str, DetailValue] = {
            key: value for key, value in base.detail.items() if value not in (0, "")
        }
        room = MAX_RUN_DETAIL_KEYS - len(self.settings.detail)
        for key, value in self._extras():
            if len(detail) >= room:
                break
            detail[key] = value
        detail.update(self.settings.detail)
        return replace(base, detail=detail)

    def _extras(self) -> list[tuple[str, DetailValue]]:
        counts = self.counts
        extras: list[tuple[str, DetailValue]] = [
            ("mode", self.settings.mode or self.params.action),
            ("matched", self.matched),
            ("references", len(self.charged)),
            ("not_checked", self.not_checked),
            *self._prune_extras(),
        ]
        for name in (
            "update_available",
            "local_newer",
            "up_to_date",
            "unknown",
            "anonymous_fallback",
            "pulled",
            "rolled_back",
            "rollback_failed",
            "refused",
        ):
            extras.append((name, counts[name]))
        extras.append(("refreshed", self.tally.refreshed))
        if self.context.dry_run:
            pulls = sorted(set(self.would_pull))
            extras.append(("would_pull_count", len(pulls)))
            extras.append(("would_pull", listing(pulls)))
            if self.swarm is not None and self.swarm.engaged:
                refreshes = sorted(set(self.swarm.would_refresh))
                extras.append(("would_refresh_count", len(refreshes)))
                extras.append(("would_refresh", listing(refreshes)))
        elif self.params.action == "report":
            extras.append(("update_available_refs", listing(sorted(set(self.available)))))
        skips = sorted(self.tally.skips.items(), key=lambda item: (-item[1], item[0]))
        extras.extend(skips)
        return [(key, value) for key, value in extras if value not in (0, "")] or extras[:1]

    def _prune_extras(self) -> list[tuple[str, DetailValue]]:
        found = self.pruning
        if found is None:
            return []
        if self.context.dry_run:
            return [
                ("would_prune", found.would_prune),
                ("would_prune_images", listing(found.would_prune_images)),
            ]
        return [
            ("pruned", found.pruned),
            ("prune_waiting", found.waiting),
            ("prune_kept", found.kept),
            ("prune_failed", found.failed),
        ]

    def _compose_missing(self) -> bool:
        return self.deps.engine.compose is None

    async def _matched(self) -> list[ContainerSummary]:
        try:
            client = await self.services.hosts.client(self.environment_id)
            return await _containers_of(client, self.selector)
        except EngineError as error:
            raise unavailable_from(error) from None

    async def _visible(self, rows: Sequence[ContainerSummary]) -> list[_Candidate]:
        """Return the rows the rule's owner can see; nothing is said about the others."""
        standings = await self.context.standings(self.environment_id, rows, cursors=self.cursors)
        self.not_checked += self.context.reportable_not_checked(standings)
        seen: list[_Candidate] = []
        for row in rows:
            standing = standings.by_id.get(row.id)
            if standing is not None and standing != "hidden":
                seen.append(
                    _Candidate(row, ObjectKey(self.environment_id, "container", row.id), standing)
                )
        return seen

    async def _examine(self, candidates: Sequence[_Candidate]) -> None:
        if not candidates:
            return
        start = self.rotation % len(candidates)
        ordered = [*candidates[start:], *candidates[:start]]
        inspected = 0
        for index, candidate in enumerate(ordered):
            if inspected >= MAX_EXAMINED:
                for _ in ordered[index:]:
                    self.tally.count("deferred")
                return
            if await self._inspect(candidate):
                inspected += 1

    async def _inspect(self, candidate: _Candidate) -> bool:
        """Leave the container out for a stated reason, or read its image; True when it was read."""
        context, tally = self.context, self.tally
        row, key = candidate.row, candidate.key
        name = display_name(row.names, row.id)
        if self.self_identity.protects_container(row.id):
            tally.skip(SELF_PROTECTED_REASON)
            return False
        if SWARM_SERVICE_LABEL in row.labels:
            tally.skip("swarm_managed")
            return False
        if COMPOSE_PROJECT_LABEL in row.labels:
            self._note_compose_managed(row.labels)
            tally.skip("compose_managed")
            return False
        if row.labels_truncated or not is_canonical_id("container", row.id):
            tally.skip("unaddressable")
            return False
        if candidate.standing != "writable":
            record_unwritable(tally, explicit=self.explicit, name=name)
            return False
        if await check_suppressed(context, key, single=self.single, tally=tally, name=name):
            return False
        if row.state != "running":
            tally.skip("not_running")
            return False
        inspector = self.services.inspector
        inspector.forget(self.environment_id, row.id)
        try:
            inspected = await inspector.inspect(self.environment_id, row.id)
        except ResourceNotFound:
            tally.skip("gone")
            return True
        except EnvironmentUnavailable:
            raise
        except ProductError as error:
            tally.count("failed", reason=reason_of(error))
            return True
        labels = inspected.config.labels
        current = display_name([inspected.name], inspected.id)
        skip = None
        if self.self_identity.protects_container(inspected.id):
            skip = SELF_PROTECTED_REASON
        elif SWARM_SERVICE_LABEL in labels:
            skip = "swarm_managed"
        elif COMPOSE_PROJECT_LABEL in labels:
            self._note_compose_managed(labels)
            skip = "compose_managed"
        elif inspected.labels_truncated:
            skip = "unaddressable"
        elif not matches(self.selector, name=current, labels=labels):
            skip = "not_matched"
        elif inspected.state.status != "running":
            skip = "not_running"
        if skip is not None:
            tally.skip(skip)
            return True
        if self.settings.redeploy:
            self.seen.append(_Seen(row.id, key, current, "", bare_image_id(inspected.image)))
            return True
        reference = self._reference_of(inspected.config.image)
        if reference is not None:
            self.seen.append(_Seen(row.id, key, current, reference, bare_image_id(inspected.image)))
        return True

    def _note_compose_managed(self, labels: Mapping[str, str]) -> None:
        """Remember that the named container is run by Compose, so the rule acts on nothing."""
        selector = self.selector
        if not isinstance(selector, ContainerTarget):
            return
        note = compose_managed(selector.names[0], labels)
        if note and note not in self.acts_on_nothing:
            self.acts_on_nothing.append(note)

    def _reference_of(self, text: str) -> str | None:
        """Return the canonical reference to check, or count why the container is left out."""
        if is_image_id_reference(text):
            self.tally.skip("image_id_reference")
            return None
        try:
            parsed = parse_pull_reference(text)
        except ValidationFailed:
            self.counts["unknown"] += 1
            return None
        if parsed.pinned:
            self.tally.skip("pinned")
            return None
        return parsed.key

    async def check_references(
        self, references: Sequence[str], deployed: Mapping[str, str] | None = None
    ) -> dict[str, UpdateStatus]:
        """Check the references against their registries, or the digests `deployed` gives."""
        found: dict[str, UpdateStatus] = {}
        for start in range(0, len(references), MAX_CHECK_REFERENCES):
            chunk = list(references[start : start + MAX_CHECK_REFERENCES])
            extra = {} if deployed is None else {"deployed": {r: deployed[r] for r in chunk}}
            try:
                results = await self.updates.check(
                    None,
                    self.environment_id,
                    chunk,
                    authorise_set=self.context.authority().authorise_credential_set,
                    **extra,
                )
            except EnvironmentUnavailable:
                raise
            except ProductError:
                raise RunFailed(
                    "update_check_failed", "The update check could not be completed."
                ) from None
            for reference, status in zip(chunk, results, strict=True):
                found[reference] = status
        return found

    async def _plan_redeploys(self) -> None:
        for item in sorted(self.seen, key=lambda seen: seen.name):
            if await self._recreate_refused(item.container_id):
                self.tally.count("held")
                continue
            self.plans.append(
                _Plan(
                    "container",
                    item.key,
                    item.container_id,
                    item.name,
                    reason="redeploy",
                    image_before=item.image_id,
                )
            )
        self.seen = []

    async def _decide(self) -> None:
        if self.settings.redeploy:
            await self._plan_redeploys()
            return
        references = self.charge_references(
            list(dict.fromkeys(item.reference for item in self.seen))
        )
        within = set(references)
        for item in self.seen:
            if item.reference not in within:
                self.tally.count("deferred")
        statuses = await self.check_references(references)
        for item in sorted(self.seen, key=lambda seen: seen.name):
            if item.reference in within:
                await self._decide_container(item, statuses[item.reference])
        self.seen = []

    async def _decide_container(self, item: _Seen, status: UpdateStatus) -> None:
        tally, counts = self.tally, self.counts
        if status.anonymous_fallback:
            counts["anonymous_fallback"] += 1
        refusal = _refusal_reason(status)
        if refusal is not None:
            if not self.planning_update:
                tally.count("failed", reason=refusal)
                return
            refusals = ((item.reference, refusal),)
            if await self._refusal_held("container", item.container_id, refusals):
                tally.count("held")
                return
            self.plans.append(
                _Plan(
                    "container",
                    item.key,
                    item.container_id,
                    item.name,
                    reference=item.reference,
                    refusals=refusals,
                )
            )
            return
        failure = check_failure(status)
        if failure is not None:
            tally.count("failed", reason=failure)
            return
        verdict = _verdict_of(status, item.image_id)
        if verdict is None:
            match status.state:
                case "up_to_date":
                    counts["up_to_date"] += 1
                case "pinned":
                    tally.skip("pinned")
                case _:
                    counts["unknown"] += 1
            return
        counts[verdict] += 1
        digest = status.remote_digest if verdict == "update_available" else status.local_digest
        if verdict == "update_available":
            self.available.append(status.display)
        if not self.planning_update:
            return
        if await self._rollback_held(item.container_id, verdict, status) or (
            await self._recreate_refused(item.container_id)
        ):
            tally.count("held")
            return
        pulls = (
            (_Pull(item.reference, status.display, status.remote_digest),)
            if verdict == "update_available"
            else ()
        )
        self.plans.append(
            _Plan(
                "container",
                item.key,
                item.container_id,
                item.name,
                reason=verdict,
                reference=item.reference,
                digest=digest,
                image_before=item.image_id,
                image_after=status.local_image_id if verdict == "local_newer" else None,
                pulls=pulls,
            )
        )

    async def _classify_stack(
        self,
        stack_name: str,
        key: ObjectKey,
        members: tuple[ObjectKey, ...],
        project: Sequence[ContainerSummary],
    ) -> None:
        tally, counts = self.tally, self.counts
        inspector = self.services.inspector
        seen: list[_Seen] = []
        for box in sorted(project, key=lambda c: c.id):
            inspector.forget(self.environment_id, box.id)
            try:
                inspected = await inspector.inspect(self.environment_id, box.id)
            except ResourceNotFound:
                tally.skip("gone")
                return
            except EnvironmentUnavailable:
                raise
            except ProductError as error:
                tally.count("failed", reason=reason_of(error))
                return
            reference = self._reference_of(inspected.config.image)
            if reference is not None:
                container_key = ObjectKey(self.environment_id, "container", box.id)
                seen.append(
                    _Seen(
                        box.id,
                        container_key,
                        display_name([inspected.name], inspected.id),
                        reference,
                        bare_image_id(inspected.image),
                    )
                )
        references = list(dict.fromkeys(item.reference for item in seen))
        if not self.fits_references(references):
            tally.count("deferred")
            return
        self.charge_references(references)
        statuses = await self.check_references(references) if references else {}
        pulls: dict[str, _Pull] = {}
        local: set[str] = set()
        refusals: dict[str, str] = {}
        failures: dict[str, str] = {}
        for item in seen:
            status = statuses[item.reference]
            if status.anonymous_fallback:
                counts["anonymous_fallback"] += 1
            refusal = _refusal_reason(status)
            if refusal is not None and self.planning_update:
                refusals[item.reference] = refusal
                continue
            failure = refusal or check_failure(status)
            if failure is not None:
                failures.setdefault(item.reference, failure)
                continue
            verdict = _verdict_of(status, item.image_id)
            if verdict is None:
                match status.state:
                    case "up_to_date":
                        counts["up_to_date"] += 1
                    case "pinned":
                        tally.skip("pinned")
                    case _:
                        counts["unknown"] += 1
                continue
            counts[verdict] += 1
            if verdict == "update_available":
                self.available.append(status.display)
                pulls.setdefault(
                    item.reference, _Pull(item.reference, status.display, status.remote_digest)
                )
            else:
                local.add(item.reference)
        for failure in failures.values():
            tally.count("failed", reason=failure)
        if not self.planning_update:
            return
        owed = tuple(sorted(refusals.items()))
        held = bool(owed) and await self._refusal_held("stack", stack_name, owed)
        if held:
            owed = ()
        if not pulls and not local and not owed:
            if held:
                tally.count("held")
            return
        if not owed and await self.refused_for_good(
            "stack",
            stack_name,
            REDEPLOYED_ACTION,
            COMPOSE_REFUSED_FOR_GOOD,
            members=[("container", member.object_id) for member in members],
        ):
            tally.count("held")
            return
        changed = len(set(pulls) | local)
        self.plans.append(
            _Plan(
                "stack",
                key,
                stack_name,
                f"stack {stack_name}",
                reason=("update_available" if pulls else "local_newer") if changed else None,
                pulls=tuple(sorted(pulls.values(), key=lambda pull: pull.reference)),
                refusals=owed,
                members=members,
                updated=changed,
            )
        )

    async def _refusal_held(
        self, target_type: str, target_id: str, refusals: Sequence[tuple[str, str]]
    ) -> bool:
        """Report whether the newest row on the target already says what this run would say."""
        return await self.refusal_held(
            target_type,
            target_id,
            {REFUSED_ACTION, RECREATED_ACTION, REDEPLOYED_ACTION},
            lambda detail: detail.get("refused") == refusal_signature(refusals),
        )

    async def refusal_held(
        self,
        target_type: str,
        target_id: str,
        actions: Collection[str],
        says_the_same: Callable[[Mapping[str, DetailValue]], bool],
    ) -> bool:
        """Report whether this rule's newest row of `actions` on the target is a standing refusal.

        `says_the_same` judges the refusal row's detail; a user's action on the target or a save
        of the rule after the row releases it.
        """
        events = ActivityEventRepository(self.context.db)
        newest = await events.latest_subsystem_event(
            self.environment_id,
            target_type,
            target_id,
            subsystem=self.settings.subsystem,
            rule_id=self.rule_id,
            actions=actions,
        )
        if newest is None or newest.action != REFUSED_ACTION:
            return False
        if not says_the_same(newest.detail or {}):
            return False
        return not await self.released(events, target_type, target_id, newest.occurred_at)

    async def refused_for_good(
        self,
        target_type: str,
        target_id: str,
        action: str,
        reasons: frozenset[str],
        *,
        members: Sequence[tuple[str, str]] = (),
    ) -> bool:
        """Report whether the rule's newest `action` on the target failed for a repeating reason.

        A user's action on the target, or on a `members` object given as (type, id), releases it.
        """
        events = ActivityEventRepository(self.context.db)
        newest = await events.latest_subsystem_event(
            self.environment_id,
            target_type,
            target_id,
            subsystem=self.settings.subsystem,
            rule_id=self.rule_id,
            actions={action},
        )
        if newest is None or newest.result != "failure":
            return False
        if (newest.detail or {}).get("reason") not in reasons:
            return False
        return not await self.released(
            events, target_type, target_id, newest.occurred_at, also=members
        )

    async def _rollback_held(
        self, container_id: str, verdict: Verdict, status: UpdateStatus
    ) -> bool:
        """Report whether this container's last update to what this run would deploy was undone.

        A newer remote digest is compared by digest; a newer local image by image id, since the
        local tag's digest need not be the one the failed update recorded.
        """
        if verdict == "update_available":
            field_name, wanted = "digest_after", status.remote_digest
        else:
            field_name, wanted = "image_after", status.local_image_id
        if wanted is None:
            return False
        if field_name == "image_after":
            wanted = bare_image_id(wanted)
        events = ActivityEventRepository(self.context.db)
        newest = await events.latest_subsystem_event(
            self.environment_id,
            "container",
            container_id,
            subsystem=self.settings.subsystem,
            rule_id=self.rule_id,
            actions={ROLLED_BACK_ACTION, RECREATED_ACTION},
        )
        if newest is None or newest.action != ROLLED_BACK_ACTION:
            return False
        if (newest.detail or {}).get("verify_failure") == INTERRUPTED_FAILURE:
            return False
        recorded = (newest.detail or {}).get(field_name)
        if field_name == "image_after" and isinstance(recorded, str):
            recorded = bare_image_id(recorded)
        if recorded is None or recorded != wanted:
            return False
        return not await self.released(events, "container", container_id, newest.occurred_at)

    async def _recreate_refused(self, container_id: str) -> bool:
        """Report whether the newest recreate by this rule failed for a reason a retry repeats."""
        events = ActivityEventRepository(self.context.db)
        newest = await events.latest_subsystem_event(
            self.environment_id,
            "container",
            container_id,
            subsystem=self.settings.subsystem,
            rule_id=self.rule_id,
            actions={RECREATED_ACTION},
        )
        if newest is None or newest.result != "failure":
            return False
        if (newest.detail or {}).get("reason") not in PERMANENT_REFUSALS:
            return False
        return not await self.released(events, "container", container_id, newest.occurred_at)

    async def released(
        self,
        events: ActivityEventRepository,
        target_type: str,
        target_id: str,
        at_text: str,
        *,
        also: Sequence[tuple[str, str]] = (),
    ) -> bool:
        """Report whether the rule was saved, or a user acted on the target or an `also` object.

        The time is the row's; `also` names objects, as (type, id), whose user actions release
        the row like the target's own.
        """
        rule = self.context.rule
        assert rule is not None
        at = parse_timestamp(at_text)
        if parse_timestamp(rule.updated_at) > at:
            return True
        for kind, identifier in ((target_type, target_id), *also):
            acted = await events.latest_user_action(self.environment_id, kind, identifier, at_text)
            if acted is not None and parse_timestamp(acted) > at:
                return True
        return False

    def _forecast(self, plan: _Plan) -> None:
        """Say what a real run would do with the plan, taking no lock."""
        environment_id = self.environment_id
        keys = [
            plan.key,
            *plan.members,
            *(ObjectKey(environment_id, "image_reference", pull.reference) for pull in plan.pulls),
        ]
        if forecast_busy(self.deps.engine.locks, keys, single=self.single):
            self.tally.count("busy")
            return
        self.counts["refused"] += len(plan.refusals)
        if plan.reason is not None:
            self.tally.count("done", name=plan.name)
            self.would_pull.extend(pull.display for pull in plan.pulls)

    def _container_work(self, plan: _Plan) -> Callable[[AutomationAuthority], Awaitable[None]]:
        async def work(authority: AutomationAuthority) -> None:
            if plan.reason is None:
                await self._refuse(authority, plan, plan.refusals)
                raise REFUSALS[plan.refusals[0][1]]()
            pulled: _Pulled | None = None
            if plan.pulls:
                pulled = await self._pull_or_refuse(authority, plan, plan.pulls[0])
            digest = pulled.digest if pulled and pulled.digest else plan.digest
            image_after = pulled.image_id if pulled and pulled.image_id else plan.image_after

            def rollback_detail(failure: RecreateFailed) -> dict[str, DetailValue]:
                return {
                    "name": plan.name,
                    "reference": plan.reference,
                    "image_before": _engine_image_id(plan.image_before),
                    "image_after": _engine_image_id(image_after),
                    "digest_after": digest,
                    "failed_container_id": failure.failed_container_id,
                    "stage": failure.stage,
                    "verify_failure": failure.verify_failure,
                }

            async def interrupted_rollback(
                failure: RecreateFailed, activity: ActivityRecorder
            ) -> None:
                detail = {**rollback_detail(failure), "interrupted": True}
                if failure.settled_late:
                    detail["settled_late"] = True
                await record_rolled_back(
                    activity,
                    authority,
                    environment_id=self.environment_id,
                    container_id=plan.target_id,
                    detail=detail,
                )

            try:
                outcome = await self.recreation.recreate(
                    authority,
                    self.environment_id,
                    plan.target_id,
                    expected_name=plan.name,
                    pull=False,
                    verify=VerifyPolicy(self.params.verify_seconds, self.params.require_healthy),
                    note=self.note()
                    if plan.reason == "redeploy"
                    else self.note(update_reason=plan.reason, reference=plan.reference),
                    on_rolled_back=interrupted_rollback,
                )
            except RecreateFailed as failure:
                if failure.rolled_back:
                    await record_rolled_back(
                        self.context.activity,
                        authority,
                        environment_id=self.environment_id,
                        container_id=plan.target_id,
                        detail=rollback_detail(failure),
                    )
                    self.counts["rolled_back"] += 1
                raise
            await self._remember_replaced(plan, outcome, image_after)

        return work

    async def _remember_replaced(
        self, plan: _Plan, outcome: RecreateOutcome, image_after: str | None
    ) -> None:
        replaced = self.services.replaced
        if replaced is None or not self.prunes or not outcome.image_changed:
            return
        if plan.image_before is None:
            return
        new_image = image_after or await self._image_of(outcome.new_id)
        if new_image is None:
            return
        rule, _ = self.context.rule_scope()
        await replaced.record_container(
            rule,
            self.environment_id,
            old_image_id=plan.image_before,
            new_image_id=new_image,
            container_id=outcome.new_id,
        )

    async def _image_of(self, container_id: str) -> str | None:
        try:
            client = await self.services.hosts.client(self.environment_id)
            return (await client.containers.inspect(container_id)).image
        except (EngineError, ProductError):
            return None

    def _stack_work(self, plan: _Plan) -> Callable[[AutomationAuthority], Awaitable[None]]:
        async def work(authority: AutomationAuthority) -> None:
            stacks = self.services.stacks
            if stacks is None:
                raise RunFailed("compose_unavailable", COMPOSE_UNAVAILABLE)
            if plan.refusals:
                await self._refuse(authority, plan, plan.refusals)
            if plan.reason is None:
                raise REFUSALS[plan.refusals[0][1]]()
            for pull in plan.pulls:
                self.require_time()
                await self._pull_or_refuse(authority, plan, pull)
            self.require_time()
            for held in (plan.key, *plan.members):
                await check_suppressed(self.context, held, single=True)
            replaced = self.services.replaced if self.prunes else None
            before = None
            if replaced is not None:
                before = await replaced.stack_images(self.environment_id, plan.target_id)
            try:
                await stacks.redeploy(
                    rechecking(self.context, authority, plan.members),
                    self.environment_id,
                    plan.target_id,
                    pull=False,
                    note=self.note()
                    if plan.reason == "redeploy"
                    else self.note(references_updated=plan.updated),
                )
            except StackRejected as rejected:
                refusals = credential_refusals(rejected)
                if not refusals:
                    raise
                await self._refuse(authority, plan, refusals)
                raise REFUSALS[refusals[0][1]]() from rejected
            if replaced is not None:
                rule, _ = self.context.rule_scope()
                await replaced.record_stack(rule, self.environment_id, plan.target_id, before)

        return work

    async def _refuse(
        self, authority: AutomationAuthority, plan: _Plan, refusals: Sequence[tuple[str, str]]
    ) -> None:
        await self.refuse(authority, plan.kind, plan.target_id, plan.name, refusals)

    async def refuse(
        self,
        authority: AutomationAuthority,
        target_type: str,
        target_id: str,
        name: str,
        refusals: Sequence[tuple[str, str]],
    ) -> None:
        """Write the row that holds an object whose references cannot be used."""
        await record_refusal(
            self.context.activity,
            authority,
            environment_id=self.environment_id,
            target_type=target_type,
            target_id=target_id,
            name=name,
            reference=",".join(reference for reference, _ in sorted(refusals)),
            reason=sorted(refusals)[0][1],
            refused=refusal_signature(refusals),
        )
        self.counts["refused"] += 1

    async def _pull_or_refuse(
        self, authority: AutomationAuthority, plan: _Plan, pull: _Pull
    ) -> _Pulled:
        try:
            return await self._pull(authority, pull, plan.key)
        except _Refusal as refusal:
            await self._refuse(authority, plan, ((pull.reference, refusal.reason),))
            raise

    async def _pull(self, authority: AutomationAuthority, pull: _Pull, key: ObjectKey) -> _Pulled:
        """Pull the reference once per run; a failed pull fails every later object the same way."""
        known = self.pulled.get(pull.reference)
        if known is not None:
            if known.error is not None:
                raise known.error.with_traceback(None)
            return known
        note = self.note()
        if pull.remote_digest is not None:
            note["remote_digest"] = pull.remote_digest
        actions = self.services.image_actions
        if actions is None:
            raise RunFailed("registry_runtime_missing", "The registry runtime is not available.")
        try:
            outcome = await actions.pull(
                authority,
                self.environment_id,
                automation_plan(self.environment_id, pull.reference),
                on_behalf_of=key,
                note=note,
            )
        except (ObjectBusy, RunSkipped):
            raise
        except Exception as error:
            refusal = _pull_refusal(error)
            self.pulled[pull.reference] = _Pulled(None, None, refusal or error)
            if refusal is None:
                raise
            raise refusal from error
        self.counts["pulled"] += 1
        known = _Pulled(outcome.digest, bare_image_id(outcome.image_id or ""))
        self.pulled[pull.reference] = known
        return known


def _engine_image_id(bare: str | None) -> str | None:
    return f"sha256:{bare}" if bare else None


def _verdict_of(status: UpdateStatus, image_id: str | None) -> Verdict | None:
    if status.state == "update_available":
        return "update_available"
    if (
        status.state == "up_to_date"
        and status.local_image_id is not None
        and image_id is not None
        and image_id != status.local_image_id
    ):
        return "local_newer"
    return None


async def _containers_of(client: EngineClient, selector: Selector) -> list[ContainerSummary]:
    """List the containers the selector names, in any state, sorted by name.

    The Engine's filters only narrow the list; each row is matched again.
    """
    match selector:
        case ContainerTarget(names=names):
            rows = [row for _, row in await find_containers(client, names) if row is not None]
            if not rows:
                raise RunFailed("target_missing", TARGET_MISSING)
        case LabelsTarget(labels=terms):
            listed = await client.containers.list(
                all=True, filters={"label": [term_text(term) for term in terms]}
            )
            rows = [
                row
                for row in listed
                if row.labels_truncated
                or matches(selector, name=display_name(row.names, row.id), labels=row.labels)
            ]
        case _:
            raise RunFailed("selector_invalid", "The automation's target is not valid.")
    return sorted(rows, key=lambda row: display_name(row.names, row.id))


async def _readable(
    principal: Principal,
    environment_id: int,
    rows: Sequence[ContainerSummary],
    authoriser: Authoriser,
) -> set[str]:
    """Return the ids of the rows the viewer can read.

    A row whose listing labels were cut is judged on the labels of its own inspection, since
    the cut ones may be the labels that decide.
    """
    whole = [row for row in rows if not row.labels_truncated]
    visible = await authoriser.filter_visible(
        principal, [ResourceRef(environment_id, "container", row.id, row.labels) for row in whole]
    )
    found = {ref.resource_id for ref in visible}
    for row in rows:
        if row.labels_truncated:
            try:
                await authoriser.require_access(
                    principal, ResourceKey(environment_id, "container", row.id), "read"
                )
            except (PermissionDenied, ResourceNotFound, ObjectTooLarge):
                continue
            found.add(row.id)
    return found


class ContainerUpdater(Subsystem):
    """Reports newer images at bound references, or applies them to what runs them."""

    name = NAME
    parameters_model: ClassVar[type[RuleParameters]] = UpdaterParameters
    event_policy = "per_action"
    schedule_kinds: ClassVar[frozenset[ScheduleKind]] = frozenset({"cron", "interval"})
    min_interval_seconds = 900
    overrun_policy = "skip"
    allowed_overrun_policies: ClassVar[frozenset[OverrunPolicy]] = frozenset({"skip"})
    cancellation_safe = False
    catch_up = False
    run_timeout = timedelta(minutes=60)
    lock_wait = 60.0
    suppression_window = timedelta(minutes=5)
    failure_threshold = 5
    requires_environment = True
    supports_dry_run = True
    target_kinds: ClassVar[frozenset[SelectorKind]] = frozenset(
        {"container", "labels", "stack", "service"}
    )
    stack_kinds: ClassVar[frozenset[StackKind]] = frozenset({"compose", "swarm"})
    refuses_compose_managed: ClassVar[bool] = True

    def __init__(
        self, deps: SubsystemDeps, *, self_identity: SelfIdentitySource | None = None
    ) -> None:
        super().__init__(deps)
        self._self_identity = self_identity or no_identity
        self._cursors = FallbackCursors()

    def parse_selector(self, text: str) -> str:
        """Validate a container, label, stack or Swarm service target; return its canonical form."""
        return canonical(parse_selector_text(text, self.target_kinds))

    async def authorise_target(
        self,
        principal: Principal,
        environment_id: int | None,
        selector: str,
        authoriser: Authoriser,
    ) -> None:
        """Raise unless the principal may write to what the selector names.

        A container that Compose manages is refused, with the stack to name instead, since the
        updater leaves it alone.
        """
        await check_target(
            self._deps,
            principal,
            environment_id,
            selector,
            authoriser,
            self.target_kinds,
            stack_kinds=self.stack_kinds,
            refuse_compose_managed=self.refuses_compose_managed,
        )

    async def preview_targets(
        self, principal: Principal, environment_id: int, selector: str
    ) -> TargetPreview | None:
        """List what the selector matches that the viewer can read; an unreachable host raises."""
        try:
            parsed = parse_selector_text(selector, self.target_kinds)
        except ValidationFailed:
            return None
        deps = self._deps
        async with deps.database.session() as db:
            authoriser = self._authoriser(db, ActivityRecorder(db, deps.auth.clock))
            try:
                await authoriser.require_environment(principal, environment_id)
            except (PermissionDenied, ResourceNotFound):
                return TargetPreview((), 0)
            hosts = build_host_clients(db, deps.auth, deps.engine)
            stacks = StackRepository(db, deps.engine.vault, deps.auth.clock)
            try:
                client = await hosts.client(environment_id)
                if isinstance(parsed, ServiceTarget | StackTarget):
                    return await self._preview_named(
                        parsed, principal, environment_id, authoriser, hosts, client, stacks
                    )
                listed = await _containers_of(client, parsed)
            except EngineError as error:
                raise unavailable_from(error) from None
            except RunFailed:
                return TargetPreview((), 0)
            rows = [row for row in listed if row.state == "running"]
            readable = await _readable(principal, environment_id, rows, authoriser)
        acted = [
            row
            for row in rows
            if row.id in readable
            and SWARM_SERVICE_LABEL not in row.labels
            and COMPOSE_PROJECT_LABEL not in row.labels
        ]
        names = [display_name(row.names, row.id) for row in acted]
        unmanaged = [
            note
            for row in rows
            if isinstance(parsed, ContainerTarget)
            and row.id in readable
            and (note := compose_managed(display_name(row.names, row.id), row.labels))
        ]
        return TargetPreview(
            tuple(names[:MAX_PREVIEW_NAMES]),
            max(0, len(names) - MAX_PREVIEW_NAMES),
            f"Not acted on: {'; '.join(unmanaged)}" if unmanaged else None,
        )

    async def _preview_named(
        self,
        parsed: ServiceTarget | StackTarget,
        principal: Principal,
        environment_id: int,
        authoriser: Authoriser,
        hosts: HostClients,
        client: EngineClient,
        stacks: StackRepository,
    ) -> TargetPreview:
        """List the services or stacks the selector names that the viewer can read."""
        # Imported here: the Swarm module imports this one.
        from app.subsystems.container_updater_swarm import preview_swarm

        names: list[str] = []
        notes: list[str | None] = []
        for name in parsed.names:
            one = only_name(parsed, name)
            found = await preview_swarm(
                one,
                principal,
                environment_id,
                authoriser,
                hosts,
                client,
                stacks,
                self._deps.engine.refresher.poke,
            )
            if found is None and isinstance(one, StackTarget):
                found = await self._preview_stack(
                    principal, environment_id, name, authoriser, client, stacks
                )
            if found is not None:
                names.extend(found.names)
                notes.append(found.note)
        note = notes[0] if len(parsed.names) == 1 and notes else None
        return TargetPreview(
            tuple(names[:MAX_PREVIEW_NAMES]), max(0, len(names) - MAX_PREVIEW_NAMES), note
        )

    async def _preview_stack(
        self,
        principal: Principal,
        environment_id: int,
        name: str,
        authoriser: Authoriser,
        client: EngineClient,
        stacks: StackRepository,
    ) -> TargetPreview:
        row = await stacks.get_folded(environment_id, name)
        if row is None or row.kind != "compose":
            return TargetPreview((), 0)
        try:
            await authoriser.require_access(
                principal, ResourceKey(environment_id, "stack", row.name), "read"
            )
        except (PermissionDenied, ResourceNotFound):
            return TargetPreview((), 0)
        key = canonical_stack_key(row.name)
        count = sum(1 for c in await read_project_containers(client) if in_project(c.labels, key))
        return TargetPreview(
            (row.name,), 0, f"{count} {'container' if count == 1 else 'containers'}"
        )

    async def run(self, context: RunContext) -> RunResult:
        """Report or apply the newer images of the rule's target; a dry run only plans."""
        params = context.parameters
        if not isinstance(params, UpdaterParameters):
            raise RunFailed("parameters_invalid", "The automation's parameters are not valid.")
        return await run_pass(
            context,
            self._deps,
            params,
            target_kinds=self.target_kinds,
            cursors=self._cursors,
            self_identity=self._self_identity,
        )


async def run_pass(
    context: RunContext,
    deps: SubsystemDeps,
    params: UpdaterParameters,
    *,
    target_kinds: frozenset[SelectorKind],
    cursors: FallbackCursors,
    self_identity: SelfIdentitySource,
    settings: PassSettings | None = None,
) -> RunResult:
    """Plan and carry out one pass over the rule's target as `settings` says."""
    settings = settings or PassSettings()
    rule, environment_id = context.rule_scope()
    try:
        selector = parse_selector_text(rule.target_selector, target_kinds)
    except ValidationFailed:
        raise RunFailed("selector_invalid", "The automation's target is not valid.") from None
    stacked = isinstance(selector, StackTarget)
    services = run_services(
        context,
        deps,
        stacks=stacked and not context.dry_run,
        images=True,
        swarm=stacked or isinstance(selector, ServiceTarget),
        self_identity=self_identity,
    )
    updates, recreation = services.updates, services.recreation
    if services.image_actions is None or updates is None or recreation is None:
        raise RunFailed("registry_runtime_missing", "The registry runtime is not available.")
    run = _Pass(
        context=context,
        deps=deps,
        services=services,
        updates=updates,
        recreation=recreation,
        params=params,
        selector=selector,
        environment_id=environment_id,
        rule_id=rule.id,
        rotation=context.dispatch.run.id,
        cursors=cursors,
        started=context.clock(),
        settings=settings,
        tally=_Tally(done_name="redeployed", plan_name="would_redeploy")
        if settings.redeploy
        else _Tally(),
        self_identity=self_identity(),
    )
    if stacked or isinstance(selector, ServiceTarget):
        from app.subsystems.container_updater_swarm import SwarmPass

        run.swarm = SwarmPass(run)
    try:
        if isinstance(selector, ContainerTarget | StackTarget | ServiceTarget):
            await run.plan_named(selector)
        else:
            await run.plan_containers()
    except EnvironmentUnavailable as error:
        raise environment_outcome(error) from None
    await run.carry_out()
    await run.prune()
    return run.result()
