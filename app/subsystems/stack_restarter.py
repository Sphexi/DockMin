"""The stack-restarter subsystem: scheduled restarts of one container or one Compose stack."""

from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar, Final, Literal

from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.activity_event import ActivityEventRepository, ActivityFilter, DetailValue
from app.db.repositories.clocked import parse_timestamp
from app.db.repositories.resource_control import canonical_stack_key
from app.db.repositories.scheduled_job import OverrunPolicy, ScheduleKind
from app.db.repositories.stack import StackRepository
from app.scheduler.model import RunFailed, RunResult
from app.self_identity import SelfIdentitySource, no_identity
from app.services.access import is_canonical_id
from app.services.acting import AutomationAuthority
from app.services.authorisation import Authoriser
from app.services.container_derivation import SWARM_SERVICE_LABEL
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import ObjectKey
from app.services.stack_deploy import in_project
from app.services.stack_queries import SERVICE_LABEL, read_project_containers
from app.subsystems.acting_run import (
    PERMISSION_DENIED,
    ObjectTally,
    act_on,
    check_authorised,
    check_suppressed,
    failure_reason,
    forecast_busy,
    permitted,
    rechecking,
)
from app.subsystems.base import (
    RuleParameters,
    RunContext,
    Subsystem,
    SubsystemDeps,
    environment_outcome,
)
from app.subsystems.run_services import RunServices, check_target, run_services
from app.subsystems.selectors import (
    ContainerTarget,
    SelectorKind,
    StackTarget,
    canonical,
    find_container,
    parse_selector_text,
)

NAME: Final = "stack-restarter"
TARGET_MISSING: Final = "The automation's target was not found."
COMPOSE_UNAVAILABLE: Final = "Compose is not available."

type _Verdict = Literal[
    "act",
    "not_running",
    "swarm_managed",
    "unaddressable",
    "permission_denied",
    "self_protected",
    "suppressed",
]


class StackRestarterParameters(RuleParameters):
    """The restarter takes no parameters of its own."""


@dataclass(frozen=True, slots=True)
class _Planned:
    kind: Literal["container", "stack"]
    label: str
    key: ObjectKey
    verdict: _Verdict
    container_id: str = ""
    stack_name: str = ""
    members: tuple[ObjectKey, ...] = ()
    services: int = 0
    also_start: tuple[str, ...] = ()


class StackRestarter(Subsystem):
    """Restarts one container, or one Compose stack in dependency order, on a schedule."""

    name = NAME
    parameters_model = StackRestarterParameters
    event_policy = "per_action"
    schedule_kinds: ClassVar[frozenset[ScheduleKind]] = frozenset({"cron", "interval"})
    min_interval_seconds = 3600
    overrun_policy = "skip"
    allowed_overrun_policies: ClassVar[frozenset[OverrunPolicy]] = frozenset({"skip"})
    cancellation_safe = True
    catch_up = False
    run_timeout = timedelta(minutes=30)
    lock_wait = 60.0
    suppression_window = timedelta(minutes=5)
    failure_threshold = 5
    requires_environment = True
    supports_dry_run = True
    target_kinds: ClassVar[frozenset[SelectorKind]] = frozenset({"container", "stack"})

    def __init__(
        self, deps: SubsystemDeps, *, self_identity: SelfIdentitySource | None = None
    ) -> None:
        super().__init__(deps)
        self._self_identity = self_identity or no_identity

    def parse_selector(self, text: str) -> str:
        """Validate a container or Compose stack target and return its canonical form."""
        return canonical(parse_selector_text(text, self.target_kinds))

    async def authorise_target(
        self,
        principal: Principal,
        environment_id: int | None,
        selector: str,
        authoriser: Authoriser,
    ) -> None:
        """Raise unless the principal may write to the target; a Swarm stack is refused."""
        await check_target(
            self._deps, principal, environment_id, selector, authoriser, self.target_kinds
        )

    async def run(self, context: RunContext) -> RunResult:
        """Restart the rule's target if it is running; a dry run reports without acting."""
        rule, environment_id = context.rule_scope()
        try:
            selector = parse_selector_text(rule.target_selector, self.target_kinds)
        except ValidationFailed:
            raise RunFailed("selector_invalid", "The automation's target is not valid.") from None
        if not isinstance(selector, ContainerTarget | StackTarget):
            raise RunFailed("selector_invalid", "The automation's target is not valid.")
        services = run_services(
            context,
            self._deps,
            stacks=isinstance(selector, StackTarget) and not context.dry_run,
            self_identity=self._self_identity,
        )
        single = len(selector.names) == 1
        tally = ObjectTally(done_name="restarted", plan_name="would_restart")
        tally.track_targets()
        stack_services = 0
        for name in selector.names:
            tally.begin(name)
            try:
                planned = await self._plan(
                    context, services, environment_id, selector, name, single
                )
            except EnvironmentUnavailable as error:
                raise environment_outcome(error) from None
            except ObjectTooLarge as error:
                if single:
                    raise RunFailed(failure_reason(error), str(error)) from None
                tally.resolved(name)
                tally.record("failed", attempted=False, reason=failure_reason(error))
                continue
            except RunFailed as failed:
                if single or failed.reason != "target_missing":
                    raise
                tally.missing(name)
                continue
            tally.resolved(name)
            match planned.verdict:
                case "permission_denied":
                    tally.record("failed", attempted=False, reason=PERMISSION_DENIED)
                case "suppressed":
                    tally.record("suppressed", attempted=False)
                case "act":
                    await self._act(context, services, environment_id, planned, tally, single)
                case reason:
                    tally.record("skipped", attempted=False, reason=reason)
            stack_services += planned.services
        if tally.targets_found == 0:
            raise RunFailed("target_missing", TARGET_MISSING)
        extra: dict[str, DetailValue] = {"target_kind": selector.kind}
        if isinstance(selector, StackTarget):
            extra["services"] = stack_services
        return tally.result(dry_run=context.dry_run, extra=extra)

    async def _plan(
        self,
        context: RunContext,
        services: RunServices,
        environment_id: int,
        selector: ContainerTarget | StackTarget,
        name: str,
        single: bool,
    ) -> _Planned:
        if isinstance(selector, ContainerTarget):
            return await self._plan_container(context, services, environment_id, name, single)
        return await self._plan_stack(context, services, environment_id, name, single)

    async def _plan_container(
        self,
        context: RunContext,
        services: RunServices,
        environment_id: int,
        name: str,
        single: bool,
    ) -> _Planned:
        container = await find_container(await services.hosts.client(environment_id), name)
        if container is None or not is_canonical_id("container", container.id):
            raise RunFailed("target_missing", TARGET_MISSING)
        key = ObjectKey(environment_id, "container", container.id)
        if await context.standing(key) == "hidden":
            raise RunFailed("target_missing", TARGET_MISSING)

        def planned(verdict: _Verdict) -> _Planned:
            return _Planned("container", name, key, verdict, container_id=container.id)

        if not await check_authorised(context, key, explicit=True):
            return planned("permission_denied")
        if self._self_identity().protects_container(container.id):
            return planned("self_protected")
        if SWARM_SERVICE_LABEL in container.labels:
            return planned("swarm_managed")
        if container.labels_truncated:
            return planned("unaddressable")
        if await check_suppressed(context, key, single=single):
            return planned("suppressed")
        if container.state != "running":
            return planned("not_running")
        return planned("act")

    async def _plan_stack(
        self,
        context: RunContext,
        services: RunServices,
        environment_id: int,
        name: str,
        single: bool,
    ) -> _Planned:
        stacks = StackRepository(context.db, self._deps.engine.vault, context.clock)
        row = await stacks.get_folded(environment_id, name)
        if row is None or row.kind != "compose":
            raise RunFailed("target_missing", TARGET_MISSING)
        key = ObjectKey(environment_id, "stack", canonical_stack_key(row.name))
        if await context.standing(key) == "hidden":
            raise RunFailed("target_missing", TARGET_MISSING)
        client = await services.hosts.client(environment_id)
        project = [
            c for c in await read_project_containers(client) if in_project(c.labels, key.object_id)
        ]
        members = tuple(
            ObjectKey(environment_id, "container", c.id)
            for c in project
            if is_canonical_id("container", c.id)
        )

        def planned(verdict: _Verdict, running: int = 0, also: tuple[str, ...] = ()) -> _Planned:
            return _Planned(
                "stack",
                f"stack {row.name}",
                key,
                verdict,
                stack_name=row.name,
                members=members,
                services=running,
                also_start=also,
            )

        if len(members) != len(project) or not await permitted(context, key, *members):
            return planned("permission_denied")
        identity = self._self_identity()
        if identity.protects_stack(environment_id, row.name) or any(
            identity.protects_container(c.id) for c in project
        ):
            return planned("self_protected")
        for held in (key, *members):
            if await check_suppressed(context, held, single=single):
                return planned("suppressed")
        running = {
            s for c in project if c.state == "running" and (s := c.labels.get(SERVICE_LABEL))
        }
        present = {s for c in project if (s := c.labels.get(SERVICE_LABEL))}
        also = tuple(sorted(set(await self._unstarted(context, row.name)) & present))
        if not any(c.state == "running" for c in project) and not also:
            return planned("not_running")
        return planned("act", len(running | set(also)), also)

    async def _unstarted(self, context: RunContext, stack_name: str) -> list[str]:
        """List the services the rule's last restart of the stack stopped and could not start.

        A user acting on the stack since then ends the obligation: the stack is theirs again.
        """
        rule, environment_id = context.rule_scope()
        events = ActivityEventRepository(context.db)
        page = await events.page(
            ActivityFilter(
                actor_subsystem=NAME,
                rule_id=rule.id,
                environment_id=environment_id,
                target_type="stack",
                target_id=stack_name,
            ),
            limit=1,
        )
        if not page.items:
            return []
        last = page.items[0]
        names = (last.detail or {}).get("unstarted")
        if not isinstance(names, str) or not names:
            return []
        acted = await events.latest_user_action(
            environment_id, "stack", stack_name, last.occurred_at
        )
        if acted is not None and parse_timestamp(acted) > parse_timestamp(last.occurred_at):
            return []
        return [name for name in names.split(",") if name]

    async def _act(
        self,
        context: RunContext,
        services: RunServices,
        environment_id: int,
        planned: _Planned,
        tally: ObjectTally,
        single: bool,
    ) -> None:
        stack_needs_compose = planned.kind == "stack" and self._deps.engine.compose is None
        if context.dry_run:
            if stack_needs_compose:
                raise RunFailed("compose_unavailable", COMPOSE_UNAVAILABLE)
            if forecast_busy(
                self._deps.engine.locks, [planned.key, *planned.members], single=single
            ):
                tally.record("busy", attempted=False)
                return
            tally.record("done", attempted=False, name=planned.label)
            return

        async def restart(authority: AutomationAuthority) -> None:
            if planned.kind == "container":
                await services.container_actions.restart(
                    authority, environment_id, planned.container_id, note={"trigger": "schedule"}
                )
                return
            if services.stacks is None:
                raise RunFailed("compose_unavailable", COMPOSE_UNAVAILABLE)
            await services.stacks.restart(
                rechecking(context, authority, planned.members),
                environment_id,
                planned.stack_name,
                also_start=planned.also_start,
            )

        await act_on(
            context,
            planned.key,
            explicit=True,
            single=single,
            work=restart,
            tally=tally,
            name=planned.label,
        )
