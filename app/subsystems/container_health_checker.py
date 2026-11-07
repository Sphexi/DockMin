"""container-health-checker: restarts running containers that report unhealthy, within a budget."""

from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import ClassVar, Final, Literal

from pydantic import Field

from app.auth.errors import SELF_PROTECTED_REASON, ProductError, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.activity_event import ActivityEventRepository
from app.db.repositories.automation_rule import AutomationRule
from app.db.repositories.clocked import format_timestamp, parse_timestamp
from app.db.repositories.job_run import MAX_RUN_DETAIL_KEYS
from app.db.repositories.restart_hold import RestartHoldRepository
from app.db.repositories.scheduled_job import OverrunPolicy, ScheduleKind
from app.db.repositories.stack import StackRepository
from app.docker_engine.errors import EngineError
from app.docker_engine.models import ContainerSummary
from app.docker_engine.resources import Filters
from app.scheduler.model import RunFailed, RunResult, reason_of
from app.self_identity import SelfIdentity, SelfIdentitySource, no_identity
from app.services.access import COMPOSE_PROJECT_LABEL, canonical_stack_key, is_canonical_id
from app.services.acting import AutomationAuthority
from app.services.activity import ActivityRecorder
from app.services.authorisation import Authoriser
from app.services.container_derivation import SWARM_SERVICE_LABEL
from app.services.containers import display_name
from app.services.object_inspector import EnvironmentUnavailable, unavailable_from
from app.services.object_locks import ObjectKey
from app.services.restart_holds import (
    EXHAUSTED_ACTION,
    HEALTH_CHECKER,
    RELEASED_ACTION,
    Hold,
    exhaustions,
    is_held,
    last_seen_unhealthy,
    released_at,
    rule_holds,
)
from app.services.stack_deploy import in_project
from app.subsystems.acting_run import (
    ObjectTally,
    act_on,
    check_suppressed,
    failure_reason,
    forecast_busy,
    record_unwritable,
)
from app.subsystems.base import (
    FallbackCursors,
    RuleParameters,
    RunContext,
    Standing,
    Subsystem,
    SubsystemDeps,
    environment_outcome,
)
from app.subsystems.run_services import RunServices, check_target, run_services
from app.subsystems.selectors import (
    CONTAINER_KINDS,
    ContainerTarget,
    EnvironmentTarget,
    LabelsTarget,
    Selector,
    StackTarget,
    canonical,
    find_containers,
    matches,
    parse_selector_text,
    term_text,
)

NAME: Final = HEALTH_CHECKER
RESTARTED_ACTION: Final = "container_restarted"
EXHAUSTED_REASON: Final = "restart_budget_exhausted"
EXHAUSTED_MESSAGE: Final = "The container is still unhealthy and its restart budget is used up."
TARGET_MISSING: Final = "The automation's target was not found."
TRIGGER: Final = "unhealthy"
UNHEALTHY: Final = "unhealthy"
HEALTHY: Final = "healthy"
MAX_RESTARTS_PER_RUN: Final = 25
MAX_EVALUATED_PER_RUN: Final = 200

type Kind = Literal["restart", "exhaust"]


class HealthCheckerParameters(RuleParameters):
    """The restart budget: at most `max_restarts` per container in `window_seconds`.

    With `auto_release`, a held container that stays healthy for a full window is released.
    """

    max_restarts: int = Field(3, ge=1, le=20)
    window_seconds: int = Field(3600, ge=300, le=86400)
    auto_release: bool = False


@dataclass(frozen=True, slots=True)
class _Candidate:
    row: ContainerSummary
    key: ObjectKey
    standing: Standing


@dataclass(slots=True)
class _Matched:
    """The containers a selector names, and for a name selector how each name fared."""

    rows: list[ContainerSummary]
    origin: dict[str, str] = field(default_factory=dict)
    resolved: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Pending:
    kind: Kind
    id: str
    name: str
    key: ObjectKey
    streak: int
    restarts: int


@dataclass(frozen=True, slots=True)
class _Look:
    name: str = ""
    streak: int = 0
    health: str = ""
    origin: str | None = None
    skip: str | None = None
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class _Budget:
    """A container's standing: the exhaustion time while held, else the restarts counted."""

    held_since: str | None = None
    restarts: int = 0


@dataclass(slots=True)
class _Pass:
    context: RunContext
    deps: SubsystemDeps
    services: RunServices
    params: HealthCheckerParameters
    selector: Selector
    environment_id: int
    rule: AutomationRule
    tally: ObjectTally
    rotation: int
    cursors: FallbackCursors
    self_identity: SelfIdentity
    origin: dict[str, str] = field(default_factory=dict)
    pending: list[_Pending] = field(default_factory=list)
    unhealthy: int = 0
    restarts_planned: int = 0
    evaluated: int = 0
    resolved: frozenset[str] = frozenset()
    not_checked: int = 0
    released_gone: int = 0

    @property
    def rule_id(self) -> int:
        return self.rule.id

    @property
    def explicit(self) -> bool:
        return isinstance(self.selector, ContainerTarget | StackTarget)

    @property
    def single(self) -> bool:
        return isinstance(self.selector, ContainerTarget) and len(self.selector.names) == 1

    async def visible(self, rows: Sequence[ContainerSummary]) -> list[_Candidate]:
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

    async def examine(
        self, candidates: Sequence[_Candidate], unlisted: Sequence[Hold] = ()
    ) -> None:
        """Classify the candidates from a start point that moves from run to run.

        `unlisted` are held containers the Engine's listing did not return; they come last.
        """
        tally = self.tally
        ordered: list[_Candidate] = []
        if candidates:
            start = self.rotation % len(candidates)
            ordered = [*candidates[start:], *candidates[:start]]
        for index, candidate in enumerate(ordered):
            if self.evaluated >= MAX_EVALUATED_PER_RUN or (
                self.restarts_planned >= MAX_RESTARTS_PER_RUN
            ):
                for left in ordered[index:]:
                    tally.begin(self.origin.get(left.row.id))
                    tally.record("deferred", attempted=False)
                break
            if await self._classify(candidate):
                self.evaluated += 1
        for index, held in enumerate(unlisted):
            if self.evaluated >= MAX_EVALUATED_PER_RUN:
                for _ in unlisted[index:]:
                    tally.begin(None)
                    tally.record("deferred", attempted=False)
                return
            if await self._classify_unlisted(held):
                self.evaluated += 1

    async def held_outside(self, listed: Collection[str]) -> list[Hold]:
        """Return the held containers in scope that the Engine's listing did not return."""
        context = self.context
        holds = await rule_holds(context.db, context.clock, self.rule)
        return [hold for hold in holds if hold.container_id not in listed]

    async def release_gone(self, unlisted: Sequence[Hold]) -> None:
        """End the hold on each unlisted container the host does not have; others are untouched."""
        for held in unlisted[:MAX_EVALUATED_PER_RUN]:
            if (await self._look(held.container_id, held=True)).skip == "gone":
                await self._release_gone(held)

    async def _classify(self, candidate: _Candidate) -> bool:
        """Decide what the run does about one candidate; True when it cost an inspection."""
        tally, context = self.tally, self.context
        row, key = candidate.row, candidate.key
        name = display_name(row.names, row.id)
        tally.begin(self.origin.get(row.id))
        if self.self_identity.protects_container(row.id):
            tally.record("skipped", attempted=False, reason=SELF_PROTECTED_REASON)
            return False
        if SWARM_SERVICE_LABEL in row.labels:
            tally.record("skipped", attempted=False, reason="swarm_managed")
            return False
        if row.state != "running":
            tally.record("skipped", attempted=False, reason="not_running")
            return False
        if row.labels_truncated or not is_canonical_id("container", row.id):
            tally.record("skipped", attempted=False, reason="unaddressable")
            return False
        if candidate.standing != "writable":
            record_unwritable(tally, explicit=self.explicit, name=name)
            return False
        budget = await self._budget(key)
        if budget.held_since is not None:
            return await self._count_held(row, key, budget.held_since)
        if await check_suppressed(context, key, single=self.single, tally=tally):
            return False
        if context.dry_run and forecast_busy(self.deps.engine.locks, [key], single=self.single):
            tally.record("busy", attempted=False)
            return False
        await self._confirm(row, key, budget.restarts)
        return True

    async def _classify_unlisted(self, held: Hold) -> bool:
        """Decide what the run does about a held container the listing missed; True if inspected."""
        context = self.context
        key = ObjectKey(self.environment_id, "container", held.container_id)
        look = await self._look(held.container_id, held=True)
        if look.skip == "gone":
            await self._release_gone(held)
            return True
        standing = await context.standing(key)
        if standing == "hidden":
            return True
        if look.failure is not None:
            self.tally.begin(None)
            self._record_look(look)
            return True
        if look.skip is not None or (
            isinstance(self.selector, StackTarget) and look.origin is None
        ):
            return True
        self.tally.begin(look.origin)
        if standing != "writable":
            record_unwritable(self.tally, explicit=self.explicit, name=look.name)
            return True
        await self._settle_hold(key, look, held.held_since)
        return True

    async def _release_gone(self, held: Hold) -> None:
        """End the hold on a container the host does not have; a dry run only counts it."""
        context = self.context
        self.released_gone += 1
        if context.dry_run:
            return
        await context.activity.record_event(
            context.actor,
            action=RELEASED_ACTION,
            target_type="container",
            target_id=held.container_id,
            environment_id=self.environment_id,
            result="success",
            detail={"name": held.name, "released": "gone"},
        )

    async def _count_held(self, row: ContainerSummary, key: ObjectKey, held_since: str) -> bool:
        """Count a held container, read first so its health decides what else happens."""
        look = await self._look(row.id, held=True)
        if look.skip is not None or look.failure is not None:
            self._record_look(look)
            return True
        await self._settle_hold(key, look, held_since)
        return True

    async def _settle_hold(self, key: ObjectKey, look: _Look, held_since: str) -> None:
        """Count a held container, note it unhealthy, or release one healthy for a full window."""
        context, tally = self.context, self.tally
        if look.health == UNHEALTHY:
            self.unhealthy += 1
            if not context.dry_run:
                await RestartHoldRepository(context.db, context.clock).note_unhealthy(
                    self.rule_id, self.environment_id, key.object_id
                )
        elif (
            self.params.auto_release
            and look.health == HEALTHY
            and await self._healthy_for_window(key, held_since)
        ):
            await self._release(key, look)
            return
        tally.record("held", attempted=False)

    async def _healthy_for_window(self, key: ObjectKey, held_since: str) -> bool:
        context = self.context
        seen = await RestartHoldRepository(context.db, context.clock).sightings(
            self.rule_id, self.environment_id, [key.object_id]
        )
        last = parse_timestamp(last_seen_unhealthy(held_since, seen.get(key.object_id)))
        return context.clock() - last >= timedelta(
            seconds=max(self.params.window_seconds, self._run_interval())
        )

    def _run_interval(self) -> int:
        job = self.context.dispatch.job
        return int(job.schedule_expression) if job.schedule_expression.isdecimal() else 0

    async def _release(self, key: ObjectKey, look: _Look) -> None:
        if self.context.dry_run:
            self.tally.record("released", attempted=False, name=look.name)
            return
        await act_on(
            self.context,
            key,
            explicit=self.explicit,
            work=self._release_work(key.object_id, look.name),
            tally=self.tally,
            name=look.name,
            single=self.single,
            on_success="released",
        )

    async def _confirm(self, row: ContainerSummary, key: ObjectKey, restarts: int) -> None:
        look = await self._look(row.id)
        if look.skip is not None or look.failure is not None:
            self._record_look(look)
            return
        self.unhealthy += 1
        kind: Kind = "exhaust" if restarts >= self.params.max_restarts else "restart"
        if kind == "restart":
            self.restarts_planned += 1
        self.pending.append(_Pending(kind, row.id, look.name, key, look.streak, restarts))

    def _record_look(self, look: _Look) -> None:
        if look.skip is not None:
            self.tally.record("skipped", attempted=False, reason=look.skip)
        else:
            self.tally.record("failed", attempted=False, reason=look.failure)

    async def _look(self, container_id: str, *, held: bool = False) -> _Look:
        """Read the container now and say whether it is still one this rule should act on.

        A held container is still one when it is running, whatever its health.
        """
        inspector = self.services.inspector
        inspector.forget(self.environment_id, container_id)
        try:
            inspected = await inspector.inspect(self.environment_id, container_id)
        except ResourceNotFound:
            return _Look(skip="gone")
        except EnvironmentUnavailable:
            raise
        except ProductError as error:
            return _Look(failure=reason_of(error))
        labels = inspected.config.labels
        name = display_name([inspected.name], inspected.id)
        if self.self_identity.protects_container(inspected.id):
            return _Look(skip=SELF_PROTECTED_REASON)
        if SWARM_SERVICE_LABEL in labels:
            return _Look(skip="swarm_managed")
        if inspected.labels_truncated:
            return _Look(skip="unaddressable")
        if not matches(self.selector, name=name, labels=labels):
            return _Look(skip="not_matched")
        health = inspected.state.health
        if held:
            if inspected.state.status != "running":
                return _Look(skip="not_running")
            return _Look(
                name=name,
                streak=0 if health is None else health.failing_streak,
                health="" if health is None else health.status,
                origin=self._named_by(labels),
            )
        if inspected.state.status != "running" or health is None or health.status != UNHEALTHY:
            return _Look(skip="recovered")
        return _Look(name=name, streak=health.failing_streak, health=health.status)

    def _named_by(self, labels: Mapping[str, str]) -> str | None:
        selector = self.selector
        if isinstance(selector, StackTarget):
            return next(
                (
                    each
                    for each in selector.names
                    if each in self.resolved and in_project(labels, canonical_stack_key(each))
                ),
                None,
            )
        return None

    async def _budget(self, key: ObjectKey) -> _Budget:
        """Return whether the container is held, else how many restarts count against its budget.

        An exhaustion row holds the container until a user acts on it, the rule changes, or it is
        released automatically; the budget then starts again after that row.
        """
        context = self.context
        events = ActivityEventRepository(context.db)
        environment_id, target = key.environment_id, key.object_id
        since = context.clock() - timedelta(seconds=self.params.window_seconds)
        found = await exhaustions(
            context.db, context.clock, self.rule_id, environment_id, container_ids=[target]
        )
        if found:
            exhausted = found[0]
            if is_held(self.rule, exhausted):
                return _Budget(held_since=exhausted.occurred_at)
            counted_from = released_at(exhausted) or parse_timestamp(exhausted.occurred_at)
            since = max(since, counted_from + timedelta(seconds=1))
        restarts = await events.count_subsystem_actions(
            environment_id,
            "container",
            target,
            subsystem=NAME,
            rule_id=self.rule_id,
            action=RESTARTED_ACTION,
            since=format_timestamp(since),
        )
        return _Budget(restarts=restarts)

    async def carry_out(self) -> None:
        """Do what the plan holds, reading each container again first; a dry run only reports."""
        context, tally = self.context, self.tally
        for planned in self.pending:
            item = planned
            tally.begin(self.origin.get(item.id))
            if context.dry_run:
                if item.kind == "restart":
                    tally.record("done", attempted=False, name=item.name)
                else:
                    tally.record("exhausted", attempted=False, reason=EXHAUSTED_REASON)
                continue
            try:
                look = await self._look(item.id)
            except EnvironmentUnavailable as error:
                tally.record("failed", attempted=False, reason=failure_reason(error))
                continue
            if look.skip is not None or look.failure is not None:
                if look.skip is not None:
                    self.unhealthy -= 1
                self._record_look(look)
                continue
            item = replace(item, streak=look.streak)
            await act_on(
                context,
                item.key,
                explicit=self.explicit,
                work=self._restart(item) if item.kind == "restart" else self._exhaust(item),
                tally=tally,
                name=item.name,
                single=self.single,
                on_success="done" if item.kind == "restart" else "exhausted",
                success_reason=None if item.kind == "restart" else EXHAUSTED_REASON,
            )

    def _restart(self, item: _Pending) -> Callable[[AutomationAuthority], Awaitable[None]]:
        async def work(authority: AutomationAuthority) -> None:
            await self.services.container_actions.restart(
                authority,
                self.environment_id,
                item.id,
                note={
                    "trigger": TRIGGER,
                    "failing_streak": item.streak,
                    "restarts_in_window": item.restarts + 1,
                },
            )

        return work

    def _release_work(
        self, container_id: str, name: str
    ) -> Callable[[AutomationAuthority], Awaitable[None]]:
        async def work(authority: AutomationAuthority) -> None:
            await self.context.activity.record_event(
                authority.actor,
                action=RELEASED_ACTION,
                target_type="container",
                target_id=container_id,
                environment_id=self.environment_id,
                result="success",
                detail={"name": name, "released": "automatic"},
            )

        return work

    def _exhaust(self, item: _Pending) -> Callable[[AutomationAuthority], Awaitable[None]]:
        async def work(authority: AutomationAuthority) -> None:
            await record_exhaustion(
                self.context.activity,
                authority,
                environment_id=self.environment_id,
                container_id=item.id,
                name=item.name,
                restarts=item.restarts,
                params=self.params,
            )

        return work


async def record_exhaustion(
    activity: ActivityRecorder,
    authority: AutomationAuthority,
    *,
    environment_id: int,
    container_id: str,
    name: str,
    restarts: int,
    params: HealthCheckerParameters,
) -> None:
    """Write the row that holds a container whose restart budget is used up."""
    await activity.record_failure(
        authority.actor,
        action=EXHAUSTED_ACTION,
        target_type="container",
        target_id=container_id,
        environment_id=environment_id,
        reason=EXHAUSTED_REASON,
        detail={
            "name": name,
            "restarts": restarts,
            "max_restarts": params.max_restarts,
            "window_seconds": params.window_seconds,
        },
        message=EXHAUSTED_MESSAGE,
    )


class ContainerHealthChecker(Subsystem):
    """Restarts running containers whose healthcheck reports unhealthy, a few times at most."""

    name = NAME
    parameters_model = HealthCheckerParameters
    event_policy = "per_action"
    schedule_kinds: ClassVar[frozenset[ScheduleKind]] = frozenset({"interval"})
    overrun_policy = "skip"
    allowed_overrun_policies: ClassVar[frozenset[OverrunPolicy]] = frozenset({"skip"})
    cancellation_safe = True
    catch_up = False
    run_timeout = timedelta(minutes=30)
    lock_wait = 10.0
    suppression_window = timedelta(minutes=5)
    failure_threshold = 5
    requires_environment = True
    supports_dry_run = True
    target_kinds = CONTAINER_KINDS

    def __init__(
        self, deps: SubsystemDeps, *, self_identity: SelfIdentitySource | None = None
    ) -> None:
        super().__init__(deps)
        self._self_identity = self_identity or no_identity
        self._cursors = FallbackCursors()

    def parse_selector(self, text: str) -> str:
        """Validate the rule's target and return it in canonical form."""
        return canonical(parse_selector_text(text, self.target_kinds))

    async def authorise_target(
        self,
        principal: Principal,
        environment_id: int | None,
        selector: str,
        authoriser: Authoriser,
    ) -> None:
        """Raise unless the principal may write to the container or stack the selector names."""
        await check_target(
            self._deps, principal, environment_id, selector, authoriser, self.target_kinds
        )

    async def run(self, context: RunContext) -> RunResult:
        """Restart each matched container that is running and unhealthy, within its budget."""
        rule, environment_id = context.rule_scope()
        params = context.parameters
        if not isinstance(params, HealthCheckerParameters):
            raise RunFailed("parameters_invalid", "The automation's parameters are not valid.")
        try:
            selector = parse_selector_text(rule.target_selector, self.target_kinds)
        except ValidationFailed:
            raise RunFailed("selector_invalid", "The automation's target is not valid.") from None
        services = run_services(
            context, self._deps, stacks=False, self_identity=self._self_identity
        )
        try:
            matched = await self._matched(context, services, environment_id, selector)
        except EnvironmentUnavailable as error:
            raise environment_outcome(error) from None
        tally = ObjectTally(done_name="restarted", plan_name="would_restart")
        run = _Pass(
            context=context,
            deps=self._deps,
            services=services,
            params=params,
            selector=selector,
            environment_id=environment_id,
            rule=rule,
            tally=tally,
            rotation=context.dispatch.run.id,
            cursors=self._cursors,
            self_identity=self._self_identity(),
            origin=matched.origin,
        )
        try:
            candidates = await run.visible(matched.rows)
            unlisted = await run.held_outside({row.id for row in matched.rows})
            if isinstance(selector, ContainerTarget | StackTarget):
                try:
                    self._settle_names(run, selector, matched, candidates)
                except RunFailed:
                    await run.release_gone(unlisted)
                    raise
            await run.examine(candidates, unlisted)
        except EnvironmentUnavailable as error:
            raise environment_outcome(error) from None
        await run.carry_out()
        base = run.tally.result(dry_run=context.dry_run)
        if not context.dry_run:
            base = replace(base, attempted=base.attempted + run.released_gone)
        detail = dict(base.detail)
        gone_key = "would_release_gone" if context.dry_run else "released_gone"
        extras = (
            (gone_key, run.released_gone),
            ("not_checked", run.not_checked),
            ("matched", len(candidates)),
            ("unhealthy", run.unhealthy),
        )
        for key, value in extras:
            if len(detail) < MAX_RUN_DETAIL_KEYS and (value or key in ("matched", "unhealthy")):
                detail[key] = value
        return replace(base, detail=detail)

    def _settle_names(
        self,
        run: _Pass,
        selector: ContainerTarget | StackTarget,
        matched: _Matched,
        candidates: Sequence[_Candidate],
    ) -> None:
        """Report each named target; a container the owner cannot see is one that was not found."""
        seen = {candidate.row.id for candidate in candidates}
        if isinstance(selector, ContainerTarget):
            for row in matched.rows:
                if row.id not in seen:
                    name = matched.origin[row.id]
                    matched.resolved.remove(name)
                    matched.missing.append(name)
        if not matched.resolved:
            raise RunFailed("target_missing", TARGET_MISSING)
        tally = run.tally
        run.resolved = frozenset(matched.resolved)
        tally.track_targets()
        for name in selector.names:
            if name in matched.missing:
                tally.missing(name)
            elif name in matched.resolved:
                tally.resolved(name)

    async def _matched(
        self,
        context: RunContext,
        services: RunServices,
        environment_id: int,
        selector: Selector,
    ) -> _Matched:
        """List the containers the selector names that are running and report unhealthy.

        The Engine's filters narrow the list; every row is matched again here and each candidate
        is inspected before anything is done to it. A named container is listed whatever its
        health, since it may have recovered. A name that is not found fails a one-name rule and
        is reported by a rule with several.
        """
        client = await services.hosts.client(environment_id)
        try:
            match selector:
                case ContainerTarget(names=names):
                    found = _Matched([])
                    for name, container in await find_containers(client, names):
                        if container is None:
                            found.missing.append(name)
                            continue
                        found.rows.append(container)
                        found.origin[container.id] = name
                        found.resolved.append(name)
                    rows = found.rows
                case StackTarget(names=names):
                    found = _Matched([])
                    stacks = StackRepository(context.db, self._deps.engine.vault, context.clock)
                    listed: list[ContainerSummary] | None = None
                    for name in names:
                        stack = await stacks.get_folded(environment_id, name)
                        if stack is None or stack.kind != "compose":
                            found.missing.append(name)
                            continue
                        key = canonical_stack_key(name)
                        if (
                            await context.standing(
                                ObjectKey(environment_id, "stack", canonical_stack_key(stack.name))
                            )
                            == "hidden"
                        ):
                            found.missing.append(name)
                            continue
                        if listed is None:
                            listed = await client.containers.list(
                                filters=_unhealthy({"label": [COMPOSE_PROJECT_LABEL]})
                            )
                        found.resolved.append(name)
                        for row in listed:
                            if in_project(row.labels, key) and row.id not in found.origin:
                                found.rows.append(row)
                                found.origin[row.id] = name
                    rows = found.rows
                case LabelsTarget(labels=terms):
                    listed = await client.containers.list(
                        filters=_unhealthy({"label": [term_text(term) for term in terms]})
                    )
                    rows = [row for row in listed if row.labels_truncated or _fits(selector, row)]
                case EnvironmentTarget():
                    rows = await client.containers.list(filters=_unhealthy({}))
        except EngineError as error:
            raise unavailable_from(error) from None
        if isinstance(selector, ContainerTarget | StackTarget):
            found.rows = sorted(rows, key=lambda row: display_name(row.names, row.id))
            return found
        return _Matched(sorted(rows, key=lambda row: display_name(row.names, row.id)))


def _unhealthy(extra: Filters) -> Filters:
    return {"health": [UNHEALTHY], "status": ["running"], **extra}


def _fits(selector: Selector, row: ContainerSummary) -> bool:
    return matches(selector, name=display_name(row.names, row.id), labels=row.labels)
