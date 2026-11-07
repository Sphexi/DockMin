"""Automation rules: create, edit, switch and pause, each checked against the owner's access."""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from pydantic import ValidationError

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import DatabaseError, Session
from app.db.repositories.automation_rule import AutomationRule, AutomationRuleRepository
from app.db.repositories.clocked import Clock, format_timestamp
from app.db.repositories.environment import EnvironmentRepository
from app.db.repositories.errors import DuplicateError, MissingReferenceError
from app.db.repositories.job_run import JobRun, JobRunRepository, RunOutcome
from app.db.repositories.object_storage_target import ObjectStorageTargetRepository
from app.db.repositories.pagination import MAX_ROW_ID, Page
from app.db.repositories.replaced_image import ReplacedImageRepository
from app.db.repositories.resource_control import canonical_stack_key
from app.db.repositories.scheduled_job import (
    OverrunPolicy,
    ScheduledJob,
    ScheduledJobRepository,
    ScheduleKind,
)
from app.db.repositories.setting import SettingRepository
from app.db.repositories.user import UserRepository
from app.db.repositories.webhook import WebhookRepository
from app.scheduler.loop import SchedulerBusy
from app.scheduler.registry import HandlerRegistry
from app.scheduler.schedules import Schedule, parse_schedule
from app.services.access import ResourceKey, is_canonical_id
from app.services.activity import SUBSYSTEMS, ActivityRecorder, audited, current_audit
from app.services.authorisation import Authoriser
from app.services.containers import ContainerService
from app.services.jobs import (
    NOT_AVAILABLE,
    PAUSE_CHOICES,
    PAUSE_DURATIONS,
    PAUSE_OPTIONS,
    JobService,
    JobState,
    RunGuard,
    after_commit,
    describe_schedule,
    effective_pause,
    job_state,
    next_run_text,
    read_subsystem_enabled,
)
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.replaced_images import WaitingSummary, summarise_waiting
from app.services.restart_holds import (
    HEALTH_CHECKER,
    RELEASED_ACTION,
    exhaustions,
    is_held,
    rule_holds,
)
from app.services.webhooks import WEBHOOK_SUBSYSTEM, IssuedWebhook, WebhookView, issue_token
from app.subsystems.activity_log_exporter import NAME as EXPORTER
from app.subsystems.activity_log_exporter import ExportRelease, export_release
from app.subsystems.backup_syncer import NAME as SYNCER
from app.subsystems.base import Subsystem, TargetPreview
from app.subsystems.selectors import (
    ALL_KINDS,
    MAX_SELECTOR_BYTES,
    TOO_LARGE,
    EnvironmentTarget,
    ServiceTarget,
    StackTarget,
    describe_stored,
    matches,
    parse_selector_text,
)

MAX_NAME_CHARS: Final = 64
MAX_PARAMETER_BYTES: Final = 16 * 1024
MAX_REPORTED_FIELDS: Final = 5
MAX_REPORTED_FIELD_CHARS: Final = 32
# The log exporter is named, not imported: its module imports the services that import this one.
TARGETED_SUBSYSTEMS: Final = frozenset({EXPORTER, "container-log-exporter", SYNCER})
UPDATER: Final = "container-updater"
DEFAULT_PRUNE_HOURS: Final = 24
PRUNE_NEEDS_UPDATE: Final = "Removing replaced images needs an automation that updates."
PRUNE_NOT_FOR_SERVICES: Final = "Replaced images are not removed for a Swarm service target."
PRUNE_NEEDS_ADMINISTRATOR: Final = "Only an administrator's automation can remove images."
TARGET_MISSING: Final = "That object storage target no longer exists."
NAME_RULE: Final = f"Use 1 to {MAX_NAME_CHARS} printable characters."
MAX_RULES_PER_OWNER: Final = 50
NAME_TAKEN: Final = "You already have an automation with that name."
INSTANCE_TARGET: Final = "This instance"
WEBHOOK_NEEDS_TOKEN: Final = "A webhook automation is created where its token can be shown."
TOO_MANY_RULES: Final = f"You can have at most {MAX_RULES_PER_OWNER} automations."
ADMINISTRATOR_ONLY: Final = "administrator role required"
RULE_BUSY: Final = "This automation is running. Try again when the run ends."
CHANGED_SINCE_REVIEW: Final = "The automation changed since you reviewed it; nothing was deleted."
CHANGED_BEFORE_ENABLE: Final = (
    "The automation changed since you reviewed it; review it again to enable it."
)
CHANGED_BEFORE_RUN: Final = (
    "The automation changed since you reviewed it; review it again to run it."
)
NOT_HELD: Final = "This container is not held by this automation."
HOLDS_UNAVAILABLE: Final = "Held containers cannot be listed while the environment is unreachable."
HOLDS_SHOWN: Final = 50
RECENT_RUNS: Final = 5
PANEL_RULES_SHOWN: Final = 10
PANEL_SCAN_LIMIT: Final = 500

__all__ = [
    "PAUSE_DURATIONS",
    "PAUSE_OPTIONS",
    "AutomationRuleService",
    "CreatedRule",
    "HeldContainer",
    "PanelRule",
    "PanelRules",
    "RuleBusy",
    "RuleDeletePreview",
    "RuleHolds",
    "RulePreview",
    "RuleView",
    "describe_target",
    "rule_digest",
]

type ObjectKind = Literal["container", "stack", "swarm_stack", "service"]


@dataclass(frozen=True, slots=True)
class CreatedRule:
    """A new rule; webhook holds the token of a webhook rule, which is shown once."""

    rule: AutomationRule
    webhook: IssuedWebhook | None


class RuleBusy(SchedulerBusy):
    """Raised when a rule cannot be deleted because a run of it has not finished."""

    def __init__(self) -> None:
        super().__init__(RULE_BUSY)


@dataclass(frozen=True, slots=True)
class RuleView:
    """A rule as shown: its job's state, the target in words, and its latest runs.

    last_run is the newest run that was not a dry run. recent holds the newest runs of any kind,
    newest first, one of which may still be in flight. last_dry_run_id is read only for a single
    rule and is None in a listing. owner is set for administrators only.
    """

    rule: AutomationRule
    job: ScheduledJob
    state: JobState
    schedule: str
    target: str
    environment_name: str | None
    owner: str | None
    paused_until: str | None
    last_run: JobRun | None
    recent: tuple[JobRun, ...]
    last_dry_run_id: int | None
    supports_dry_run: bool
    subsystem_enabled: bool
    available: bool
    webhook: WebhookView | None = None

    @property
    def next_run_at(self) -> str | None:
        """Return the stored time of the job's next fire, or None when a gate forbids one."""
        return self.job.next_run_at

    @property
    def held(self) -> int:
        """Return how many containers the last real run counted as held."""
        count = None if self.last_run is None else self.last_run.detail.get("held")
        return count if isinstance(count, int) and not isinstance(count, bool) else 0


@dataclass(frozen=True, slots=True)
class HeldContainer:
    """A container a restart budget holds: since when, and when the rule last saw it unhealthy."""

    container_id: str
    name: str
    held_since: str
    last_unhealthy_at: str


@dataclass(frozen=True, slots=True)
class RuleHolds:
    """The held containers the viewer may read, the first few, and how many more there are.

    note is set when the list could not be built.
    """

    containers: tuple[HeldContainer, ...] = ()
    more: int = 0
    note: str | None = None


@dataclass(frozen=True, slots=True)
class RuleDeletePreview:
    """What deleting a rule takes away, shown before it is confirmed.

    export_release is set for an activity-log-exporter rule only.
    """

    rule: AutomationRule
    job_id: int
    runs: int
    environment_name: str | None
    target: str
    export_release: ExportRelease | None = None


@dataclass(frozen=True, slots=True)
class RulePreview:
    """A rule as it stands for a confirmation: its digest and what its selector matches now.

    target is None when the subsystem offers no preview or the rule has no environment. When the
    environment cannot be read, target has no names and its note says so.
    """

    view: RuleView
    digest: str
    target: TargetPreview | None


@dataclass(frozen=True, slots=True)
class PanelRule:
    """A rule as shown on the object it acts on.

    rule_id, name, target and last_run_id are None unless the viewer owns the rule or is an
    administrator.
    """

    subsystem: str
    schedule: str
    schedule_kind: ScheduleKind
    state: JobState
    next_run_at: str | None
    paused_until: str | None
    last_outcome: RunOutcome | None
    manageable: bool
    rule_id: int | None
    name: str | None
    target: str | None
    last_run_id: int | None


@dataclass(frozen=True, slots=True)
class PanelRules:
    """The rules acting on one object: the first few, how many more matched, and the add offer."""

    rules: tuple[PanelRule, ...]
    more: int
    can_add: bool
    pause_options: tuple[tuple[str, str], ...] = PAUSE_OPTIONS


class AutomationRuleService:
    """Creates and changes rules; a rule's job is written in the same transaction as the rule.

    Rules start disabled. Administrators manage every rule; a standard user manages their own
    environment rules, and every write that can start work re-checks their access.
    """

    def __init__(
        self,
        *,
        db: Session,
        rules: AutomationRuleRepository,
        jobs: ScheduledJobRepository,
        settings_repo: SettingRepository,
        authoriser: Authoriser,
        subsystems: HandlerRegistry,
        activity: ActivityRecorder,
        clock: Clock,
        wake: Callable[[], None],
        job_service: JobService,
        containers: ContainerService | None = None,
    ) -> None:
        self._containers = containers
        self._db = db
        self._rules = rules
        self._jobs = jobs
        self._runs = JobRunRepository(db, clock)
        self._webhooks = WebhookRepository(db, clock)
        self._job_service = job_service
        self._settings = settings_repo
        self._authoriser = authoriser
        self._subsystems = subsystems
        self._activity = activity
        self._clock = clock
        self._wake = wake

    async def get(self, actor: Principal, rule_id: int) -> AutomationRule:
        """Return a rule the caller owns, or any rule for an administrator."""
        return await self._visible(actor, rule_id)

    async def page(
        self,
        actor: Principal,
        *,
        limit: int,
        cursor: str | None = None,
        subsystem: str | None = None,
    ) -> Page[AutomationRule]:
        """Return one page of rules by ascending id, limited to the caller's own unless admin."""
        if subsystem is not None and subsystem not in SUBSYSTEMS:
            raise ValidationFailed({"subsystem": "Choose a known subsystem."})
        return await self._rules.page(
            limit=limit,
            cursor=cursor,
            subsystem=subsystem,
            owner_id=None if actor.is_administrator else actor.user_id,
        )

    async def overview(
        self,
        actor: Principal,
        *,
        limit: int,
        cursor: str | None = None,
        subsystem: str | None = None,
        environment_id: int | None = None,
    ) -> Page[RuleView]:
        """Return one page of rules by ascending id with their job state and latest runs.

        Administrators see every rule; a standard user sees their own.
        """
        if subsystem is not None and subsystem not in SUBSYSTEMS:
            raise ValidationFailed({"subsystem": "Choose a known subsystem."})
        page = await self._rules.page(
            limit=limit,
            cursor=cursor,
            subsystem=subsystem,
            owner_id=None if actor.is_administrator else actor.user_id,
            environment_id=environment_id,
        )
        views = await self._views(actor, page.items)
        return Page(views, page.next_cursor)

    async def overview_count(
        self,
        actor: Principal,
        *,
        subsystem: str | None = None,
        environment_id: int | None = None,
    ) -> int:
        """Count the rules overview lists for the same caller and filters, across all pages."""
        if subsystem is not None and subsystem not in SUBSYSTEMS:
            raise ValidationFailed({"subsystem": "Choose a known subsystem."})
        return await self._rules.count(
            subsystem=subsystem,
            owner_id=None if actor.is_administrator else actor.user_id,
            environment_id=environment_id,
        )

    async def origin_name(self, actor: Principal, rule_id: int) -> str:
        """Return the name of a rule the caller may see; ResourceNotFound otherwise."""
        return (await self._visible(actor, rule_id)).name

    async def detail(self, actor: Principal, rule_id: int) -> RuleView:
        """Return one rule the caller may see, with its last dry run."""
        rule = await self._visible(actor, rule_id)
        [view] = await self._views(actor, [rule], with_dry_run=True)
        return view

    async def runs(
        self, actor: Principal, rule_id: int, *, limit: int, cursor: str | None = None
    ) -> Page[JobRun]:
        """Return a rule's runs, newest first."""
        rule = await self._visible(actor, rule_id)
        job = await self._job_of(rule)
        return await self._runs.page(job.id, limit=limit, cursor=cursor)

    async def run(self, actor: Principal, rule_id: int, run_id: int) -> tuple[RuleView, JobRun]:
        """Return one run of the rule; a run of any other job is not found."""
        rule = await self._visible(actor, rule_id)
        job = await self._job_of(rule)
        found = await self._runs.get(run_id) if 1 <= run_id <= MAX_ROW_ID else None
        if found is None or found.scheduled_job_id != job.id:
            raise ResourceNotFound("not found")
        [view] = await self._views(actor, [rule], with_dry_run=True)
        return view, found

    async def run_now(
        self,
        actor: Principal,
        rule_id: int,
        *,
        dry_run: bool = False,
        expected_updated_at: str | None = None,
        expected_digest: str | None = None,
    ) -> int:
        """Start a run of the rule outside its schedule and return the run's id.

        The owner and administrators may. A real run re-checks the caller's write access to the
        target; a dry run does not, since it changes nothing, and may be asked of a disabled rule.
        A real run given `expected_updated_at` or `expected_digest` starts only if the rule, read
        again when the run is claimed, still matches what the caller reviewed.
        """
        rule = await self._visible(actor, rule_id)
        guard: RunGuard | None = None
        if not dry_run:
            await self.authorise_write(actor, rule)
            if expected_updated_at is not None or expected_digest is not None:
                guard = _reviewed(rule.id, expected_updated_at, expected_digest)
        return await self._job_service.run_rule_job(actor, rule, dry_run=dry_run, guard=guard)

    async def holds(self, actor: Principal, rule_id: int) -> RuleHolds:
        """Return the containers a restart-budget rule holds that the caller can see on the host.

        Newest hold first, the first 50 listed; `more` counts the rest of those on the host.
        Any other automation holds nothing.
        """
        rule = await self._visible(actor, rule_id)
        environment_id = rule.environment_id
        held = await rule_holds(self._db, self._clock, rule)
        if environment_id is None or not held:
            return RuleHolds()
        if self._containers is None:
            return RuleHolds(note=HOLDS_UNAVAILABLE)
        try:
            listing = await self._containers.brief_listing(actor, environment_id)
        except (ResourceNotFound, PermissionDenied):
            return RuleHolds()
        except (EnvironmentUnavailable, ObjectTooLarge):
            return RuleHolds(note=HOLDS_UNAVAILABLE)
        listed = {container.id for container in listing.containers}
        kept = [hold for hold in held if hold.container_id in listed]
        return RuleHolds(
            containers=tuple(
                HeldContainer(
                    container_id=hold.container_id,
                    name=hold.name,
                    held_since=hold.held_since,
                    last_unhealthy_at=hold.last_unhealthy_at,
                )
                for hold in kept[:HOLDS_SHOWN]
            ),
            more=max(len(kept) - HOLDS_SHOWN, 0),
        )

    async def release_hold(self, actor: Principal, rule_id: int, container_id: str) -> None:
        """Release a held container so the rule's restart budget starts again; owner or admin.

        A caller without write access to the container is refused before anything is recorded.
        """
        rule = await self._visible(actor, rule_id)
        environment_id = rule.environment_id
        if (
            rule.subsystem != HEALTH_CHECKER
            or environment_id is None
            or not is_canonical_id("container", container_id)
        ):
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "container", container_id), "write"
        )
        await self._release_hold(actor, rule_id, container_id, environment_id=environment_id)

    async def waiting_images(self, actor: Principal, rule_id: int) -> WaitingSummary | None:
        """Summarise the replaced images an updater rule is holding; None for any other rule."""
        rule = await self._visible(actor, rule_id)
        if rule.subsystem != UPDATER:
            return None
        found = await ReplacedImageRepository(self._db, self._clock).waiting(rule)
        return summarise_waiting(found, timedelta(hours=_prune_hours(rule.parameters)))

    async def delete_preview(self, actor: Principal, rule_id: int) -> RuleDeletePreview:
        """Name what deleting the rule takes away; deletes nothing."""
        rule = await self._visible(actor, rule_id)
        job = await self._job_of(rule)
        return RuleDeletePreview(
            rule=rule,
            job_id=job.id,
            runs=await self._runs.count_for_job(job.id),
            environment_name=await self._environment_name(rule.environment_id),
            target=describe_target(rule.target_selector),
            export_release=(
                await export_release(self._db, self._clock, rule.id)
                if rule.subsystem == EXPORTER
                else None
            ),
        )

    async def delete(self, actor: Principal, rule_id: int, *, expected_name: str) -> None:
        """Delete a rule, its job and the job's runs; owner or administrator.

        The rule is deleted only while its name is still expected_name, the one the caller
        reviewed, and not while a run of it is in flight. Activity events stay.
        """
        rule = await self._visible(actor, rule_id)
        await self._delete(
            actor, rule_id, environment_id=rule.environment_id, expected_name=expected_name
        )

    async def for_object(
        self,
        viewer: Principal,
        environment_id: int,
        *,
        name: str,
        labels: Mapping[str, str],
        stack: str | None,
        kind: ObjectKind,
    ) -> PanelRules:
        """Return the rules whose target is, contains or matches one object.

        A rule matches a container by its name, its Compose stack, its labels, or because it
        targets the whole environment; a Compose stack by its name or the whole environment; a
        Swarm service by its name or its stack's name; a Swarm stack only by its name, and only
        when the rule's automation acts on Swarm stacks. A rule of another user is returned
        without its id, name or target.
        """
        await self._authoriser.require_environment(viewer, environment_id)
        ruled = [subsystem for subsystem, takes_rules in SUBSYSTEMS.items() if takes_rules]
        candidates = await self._rules.for_environment(
            environment_id, ruled, limit=PANEL_SCAN_LIMIT
        )
        matched = [
            rule
            for rule in candidates
            if (kind != "swarm_stack" or self._acts_on_swarm_stacks(rule.subsystem))
            and _targets(rule.target_selector, name=name, labels=labels, stack=stack, kind=kind)
        ]
        shown = matched[:PANEL_RULES_SHOWN]
        jobs = await self._jobs_of(shown)
        recent = await self._runs.recent_outcomes([job.id for job in jobs], per_job=RECENT_RUNS)
        now = self._clock()
        switches: dict[str, bool] = {}
        rows: list[PanelRule] = []
        for rule, job in zip(shown, jobs, strict=True):
            runs = recent[job.id]
            manageable = viewer.is_administrator or rule.created_by_user_id == viewer.user_id
            last = _last_real_run(runs)
            state = await self._state_of(rule, job, runs, now, switches)
            rows.append(
                PanelRule(
                    subsystem=rule.subsystem,
                    schedule=describe_schedule(job),
                    schedule_kind=job.schedule_kind,
                    state=state,
                    next_run_at=job.next_run_at,
                    paused_until=effective_pause(job, rule, now),
                    last_outcome=None if last is None else last.outcome,
                    manageable=manageable,
                    rule_id=rule.id if manageable else None,
                    name=rule.name if manageable else None,
                    target=describe_target(rule.target_selector) if manageable else None,
                    last_run_id=None if last is None or not manageable else last.id,
                )
            )
        return PanelRules(
            rules=tuple(rows),
            more=len(matched) - len(shown),
            can_add=await self._authoriser.may_create(viewer, environment_id),
        )

    async def create(
        self,
        actor: Principal,
        *,
        subsystem: str,
        name: str,
        environment_id: int | None,
        target_selector: str,
        parameters: Mapping[str, Any],
        schedule_kind: str,
        schedule_expression: str,
        time_zone: str = "UTC",
        overrun_policy: str | None = None,
    ) -> AutomationRule:
        """Create a disabled rule and its job; refuses a webhook rule, whose token would be lost."""
        if subsystem == WEBHOOK_SUBSYSTEM:
            raise ValidationFailed({"subsystem": WEBHOOK_NEEDS_TOKEN})
        created = await self.create_issuing(
            actor,
            subsystem=subsystem,
            name=name,
            environment_id=environment_id,
            target_selector=target_selector,
            parameters=parameters,
            schedule_kind=schedule_kind,
            schedule_expression=schedule_expression,
            time_zone=time_zone,
            overrun_policy=overrun_policy,
        )
        return created.rule

    @audited(
        action="automation_rule_created",
        target_type="automation_rule",
        environment_id=lambda a: a["environment_id"],
    )
    async def create_issuing(
        self,
        actor: Principal,
        *,
        subsystem: str,
        name: str,
        environment_id: int | None,
        target_selector: str,
        parameters: Mapping[str, Any],
        schedule_kind: str,
        schedule_expression: str,
        time_zone: str = "UTC",
        overrun_policy: str | None = None,
    ) -> CreatedRule:
        """Create a disabled rule and its job; refused where the caller could not act directly.

        A webhook rule also gets its webhook in the same transaction, and the token comes back
        once in the result.
        """
        handler = self._handler(subsystem)
        if not actor.is_administrator and not handler.requires_environment:
            raise PermissionDenied(ADMINISTRATOR_ONLY)
        if handler.requires_environment and environment_id is None:
            raise ValidationFailed({"environment": "Choose an environment."})
        clean_name = _check_name(name)
        selector = _check_selector(handler, target_selector)
        canonical, threshold = _check_parameters(handler, parameters)
        _check_prune(subsystem, canonical, selector, owner_is_administrator=actor.is_administrator)
        schedule = _check_schedule(handler, schedule_kind, schedule_expression, time_zone)
        policy = _check_overrun(handler, overrun_policy)
        await self._authorise_write(actor, handler, environment_id, selector)
        with after_commit(self._db, self._wake):
            async with self._db.transaction():
                await self._require_target(subsystem, canonical)
                if await self._rules.count_for_owner(actor.user_id) >= MAX_RULES_PER_OWNER:
                    raise ValidationFailed({"rules": TOO_MANY_RULES})
                if await self._rules.name_taken(subsystem, actor.user_id, clean_name):
                    raise ValidationFailed({"name": NAME_TAKEN})
                try:
                    rule = await self._rules.insert(
                        name=clean_name,
                        subsystem=subsystem,
                        environment_id=environment_id,
                        target_selector=selector,
                        parameters=canonical,
                        is_enabled=False,
                        created_by_user_id=actor.user_id,
                    )
                except DuplicateError:
                    raise ValidationFailed({"name": NAME_TAKEN}) from None
                except MissingReferenceError:
                    raise ValidationFailed(
                        {"environment": "Choose an existing environment."}
                    ) from None
                await self._jobs.insert(
                    subsystem=subsystem,
                    rule_id=rule.id,
                    environment_id=environment_id,
                    schedule_kind=schedule.kind,
                    schedule_expression=schedule.expression,
                    time_zone=schedule.time_zone,
                    overrun_policy=policy,
                    catch_up=handler.catch_up,
                    failure_threshold=threshold or handler.failure_threshold,
                    next_run_at=None,
                )
                issued: IssuedWebhook | None = None
                if subsystem == WEBHOOK_SUBSYSTEM:

                    async def write(prefix: str, token_hash: str) -> None:
                        await self._webhooks.create(rule.id, prefix, token_hash)

                    issued = await issue_token(self._db, write)
                audit = current_audit()
                audit.target_id = str(rule.id)
                audit.detail.update(
                    subsystem=subsystem,
                    name=clean_name,
                    schedule_kind=schedule.kind,
                    schedule_expression=schedule.expression,
                    time_zone=schedule.time_zone,
                    overrun_policy=policy,
                )
                if issued is not None:
                    audit.detail["prefix"] = issued.prefix
        return CreatedRule(rule, issued)

    async def update(
        self,
        actor: Principal,
        rule_id: int,
        *,
        name: str,
        target_selector: str,
        parameters: Mapping[str, Any],
        schedule_kind: str,
        schedule_expression: str,
        time_zone: str,
        overrun_policy: str | None,
    ) -> AutomationRule:
        """Change a rule's name, target, parameters and schedule; its subsystem stays."""
        rule = await self._visible(actor, rule_id)
        return await self._update(
            actor,
            rule_id,
            environment_id=rule.environment_id,
            name=name,
            target_selector=target_selector,
            parameters=parameters,
            schedule_kind=schedule_kind,
            schedule_expression=schedule_expression,
            time_zone=time_zone,
            overrun_policy=overrun_policy,
        )

    async def enable(
        self,
        actor: Principal,
        rule_id: int,
        *,
        expected_updated_at: str | None = None,
        expected_digest: str | None = None,
    ) -> None:
        """Enable a rule so its job runs on schedule, and re-enable the job if it auto-disabled.

        A given `expected_updated_at` or `expected_digest` binds the call to the rule as it was
        reviewed: a rule saved since, or whose digest differs, is refused.
        """
        rule = await self._visible(actor, rule_id)
        await self._set_enabled(
            actor,
            rule_id,
            environment_id=rule.environment_id,
            enabled=True,
            expected_updated_at=expected_updated_at,
            expected_digest=expected_digest,
        )

    async def enable_preview(self, actor: Principal, rule_id: int) -> RulePreview:
        """Return what enabling the rule would act on, as the viewer may see it; changes nothing."""
        return await self._preview(actor, rule_id)

    async def run_preview(self, actor: Principal, rule_id: int) -> RulePreview:
        """Return what a run of the rule would act on, as the viewer may see it; changes nothing."""
        return await self._preview(actor, rule_id)

    async def disable(self, actor: Principal, rule_id: int) -> None:
        """Disable a rule; its job stops firing and nothing is deleted."""
        rule = await self._visible(actor, rule_id)
        await self._set_enabled(actor, rule_id, environment_id=rule.environment_id, enabled=False)

    async def pause(self, actor: Principal, rule_id: int, *, duration: str) -> None:
        """Pause a rule for 1h, 4h, 24h or 7d."""
        rule = await self._visible(actor, rule_id)
        await self._set_paused(
            actor, rule_id, environment_id=rule.environment_id, duration=duration
        )

    async def resume(self, actor: Principal, rule_id: int) -> None:
        """Clear a rule's pause."""
        rule = await self._visible(actor, rule_id)
        await self._set_paused(actor, rule_id, environment_id=rule.environment_id, duration=None)

    async def _require_target(self, subsystem: str, canonical: str) -> None:
        if subsystem not in TARGETED_SUBSYSTEMS:
            return
        target_id = json.loads(canonical).get("object_storage_target_id")
        if await ObjectStorageTargetRepository(self._db).get(target_id) is None:
            raise ValidationFailed({"object_storage_target_id": TARGET_MISSING})

    @audited(
        action="automation_rule_updated",
        target_type="automation_rule",
        target_id=lambda a: str(a["rule_id"]),
        environment_id=lambda a: a["environment_id"],
    )
    async def _update(
        self,
        actor: Principal,
        rule_id: int,
        *,
        environment_id: int | None,
        name: str,
        target_selector: str,
        parameters: Mapping[str, Any],
        schedule_kind: str,
        schedule_expression: str,
        time_zone: str,
        overrun_policy: str | None,
    ) -> AutomationRule:
        rule = await self._visible(actor, rule_id)
        handler = self._handler(rule.subsystem)
        clean_name = _check_name(name)
        selector = _check_selector(handler, target_selector)
        canonical, threshold = _check_parameters(handler, parameters)
        _check_prune(
            rule.subsystem,
            canonical,
            selector,
            owner_is_administrator=await self._owner_is_administrator(rule),
        )
        schedule = _check_schedule(handler, schedule_kind, schedule_expression, time_zone)
        policy = _check_overrun(handler, overrun_policy)
        await self._authorise_write(actor, handler, rule.environment_id, selector)
        with after_commit(self._db, self._wake):
            async with self._db.transaction():
                rule = await self._visible(actor, rule_id)
                await self._require_target(rule.subsystem, canonical)
                job = await self._job_of(rule)
                if rule.created_by_user_id is not None and await self._rules.name_taken(
                    rule.subsystem, rule.created_by_user_id, clean_name, excluding=rule_id
                ):
                    raise ValidationFailed({"name": NAME_TAKEN})
                schedule_changed = (
                    job.schedule_kind,
                    job.schedule_expression,
                    job.time_zone,
                ) != (schedule.kind, schedule.expression, schedule.time_zone)
                changes = {
                    "name_changed": rule.name != clean_name,
                    "target_changed": rule.target_selector != selector,
                    "parameters_changed": _normalised(handler, rule.parameters) != canonical,
                    "schedule_changed": schedule_changed,
                    "overrun_changed": job.overrun_policy != policy,
                }
                if not any(changes.values()):
                    current_audit().unchanged()
                    return rule
                try:
                    await self._rules.update(
                        rule_id, name=clean_name, target_selector=selector, parameters=canonical
                    )
                except DuplicateError:
                    raise ValidationFailed({"name": NAME_TAKEN}) from None
                if (
                    rule.subsystem == UPDATER
                    and _prunes(rule.parameters)
                    and not _prunes(canonical)
                ):
                    await ReplacedImageRepository(self._db, self._clock).delete_for_rule(rule_id)
                disables = rule.is_enabled and (
                    changes["target_changed"]
                    or changes["parameters_changed"]
                    or changes["schedule_changed"]
                )
                if disables:
                    await self._rules.set_enabled(rule_id, False)
                changed_job = replace(
                    job,
                    schedule_kind=schedule.kind,
                    schedule_expression=schedule.expression,
                    time_zone=schedule.time_zone,
                    next_run_at=None,
                )
                await self._jobs.update_schedule(
                    job.id,
                    schedule_kind=schedule.kind,
                    schedule_expression=schedule.expression,
                    time_zone=schedule.time_zone,
                    overrun_policy=policy,
                    catch_up=job.catch_up,
                    failure_threshold=threshold or handler.failure_threshold,
                    next_run_at=(
                        await self._next_run(changed_job, replace(rule, is_enabled=not disables))
                        if schedule_changed or disables
                        else job.next_run_at
                    ),
                )
                current_audit().detail.update(changes)
                if disables:
                    current_audit().detail["rule_disabled"] = True
                updated = await self._rules.get(rule_id)
        if updated is None:
            raise DatabaseError("a rule written by this call could not be read back")
        return updated

    @audited(
        action=lambda a: "automation_rule_enabled" if a["enabled"] else "automation_rule_disabled",
        target_type="automation_rule",
        target_id=lambda a: str(a["rule_id"]),
        environment_id=lambda a: a["environment_id"],
    )
    async def _set_enabled(
        self,
        actor: Principal,
        rule_id: int,
        *,
        environment_id: int | None,
        enabled: bool,
        expected_updated_at: str | None = None,
        expected_digest: str | None = None,
    ) -> None:
        audit = current_audit()
        rule = await self._visible(actor, rule_id)
        if enabled:
            await self.authorise_write(actor, rule)
        with after_commit(self._db, self._wake):
            async with self._db.transaction():
                rule = await self._visible(actor, rule_id)
                if (
                    enabled
                    and expected_updated_at is not None
                    and rule.updated_at != expected_updated_at
                ):
                    audit.reason = "changed_since_review"
                    raise ValidationFailed({"rule": CHANGED_BEFORE_ENABLE})
                job = await self._job_of(rule)
                if (
                    enabled
                    and expected_digest is not None
                    and rule_digest(rule, job) != expected_digest
                ):
                    audit.reason = "changed_since_review"
                    raise ValidationFailed({"rule": CHANGED_BEFORE_ENABLE})
                reenable = enabled and not job.is_enabled
                if rule.is_enabled == enabled and not reenable:
                    audit.unchanged()
                    return
                if rule.is_enabled != enabled:
                    await self._rules.set_enabled(rule_id, enabled)
                now_enabled = replace(rule, is_enabled=enabled)
                if reenable:
                    next_run = await self._next_run(replace(job, is_enabled=True), now_enabled)
                    await self._jobs.enable(job.id, next_run)
                    audit.detail["job_reenabled"] = True
                else:
                    await self._jobs.set_next_run(job.id, await self._next_run(job, now_enabled))

    @audited(
        action=lambda a: (
            "automation_rule_resumed" if a["duration"] is None else "automation_rule_paused"
        ),
        target_type="automation_rule",
        target_id=lambda a: str(a["rule_id"]),
        environment_id=lambda a: a["environment_id"],
    )
    async def _set_paused(
        self, actor: Principal, rule_id: int, *, environment_id: int | None, duration: str | None
    ) -> None:
        length = None if duration is None else PAUSE_DURATIONS.get(duration)
        if duration is not None and length is None:
            raise ValidationFailed({"duration": PAUSE_CHOICES})
        rule = await self._visible(actor, rule_id)
        if duration is None:
            await self.authorise_write(actor, rule)
        with after_commit(self._db, self._wake):
            async with self._db.transaction():
                rule = await self._visible(actor, rule_id)
                if length is None and rule.paused_until is None:
                    current_audit().unchanged()
                    return
                until = None if length is None else format_timestamp(self._clock() + length)
                job = await self._job_of(rule)
                await self._rules.set_paused(rule_id, until)
                await self._jobs.set_next_run(
                    job.id, await self._next_run(job, replace(rule, paused_until=until))
                )
                if until is not None:
                    current_audit().detail.update(duration=duration, paused_until=until)

    @audited(
        action=RELEASED_ACTION,
        target_type="container",
        target_id=lambda a: a["container_id"],
        environment_id=lambda a: a["environment_id"],
    )
    async def _release_hold(
        self, actor: Principal, rule_id: int, container_id: str, *, environment_id: int
    ) -> None:
        audit = current_audit()
        async with self._db.transaction():
            rule = await self._visible(actor, rule_id)
            found = await exhaustions(
                self._db, self._clock, rule.id, environment_id, container_ids=[container_id]
            )
            if not found or not is_held(rule, found[0]):
                audit.reason = "not_held"
                raise ValidationFailed({"container": NOT_HELD})
            audit.detail.update(
                rule_id=rule.id,
                rule_name=rule.name,
                name=found[0].name or container_id[:12],
                released="manual",
            )

    @audited(
        action="automation_rule_deleted",
        target_type="automation_rule",
        target_id=lambda a: str(a["rule_id"]),
        environment_id=lambda a: a["environment_id"],
    )
    async def _delete(
        self, actor: Principal, rule_id: int, *, environment_id: int | None, expected_name: str
    ) -> None:
        audit = current_audit()
        with after_commit(self._db, self._wake):
            async with self._db.transaction():
                rule = await self._visible(actor, rule_id)
                if rule.name != expected_name:
                    audit.reason = "changed_since_review"
                    raise ValidationFailed({"name": CHANGED_SINCE_REVIEW})
                job = await self._job_of(rule)
                if await self._runs.any_in_flight([job.id]):
                    audit.reason = "rule_running"
                    raise RuleBusy
                runs = await self._runs.count_for_job(job.id)
                await self._rules.delete(rule_id)
                audit.detail.update(
                    subsystem=rule.subsystem, name=rule.name, job_id=job.id, runs_deleted=runs
                )

    async def _preview(self, actor: Principal, rule_id: int) -> RulePreview:
        rule = await self._visible(actor, rule_id)
        handler = self._handler(rule.subsystem)
        [view] = await self._views(actor, [rule], with_dry_run=True)
        target: TargetPreview | None = None
        if rule.environment_id is not None:
            try:
                target = await handler.preview_targets(
                    actor, rule.environment_id, rule.target_selector
                )
            except (EnvironmentUnavailable, ObjectTooLarge) as error:
                target = TargetPreview(names=(), others=0, note=str(error))
        return RulePreview(view=view, digest=rule_digest(rule, view.job), target=target)

    async def _visible(self, actor: Principal, rule_id: int) -> AutomationRule:
        rule = await self._rules.get(rule_id) if 1 <= rule_id <= MAX_ROW_ID else None
        if rule is None or not (actor.is_administrator or _owns(actor, rule)):
            raise ResourceNotFound("not found")
        return rule

    async def _views(
        self, actor: Principal, rules: Sequence[AutomationRule], *, with_dry_run: bool = False
    ) -> list[RuleView]:
        jobs = await self._jobs_of(rules)
        dry_runs = (
            await self._runs.latest_dry_runs([job.id for job in jobs]) if with_dry_run else {}
        )
        recent = await self._runs.recent_outcomes([job.id for job in jobs], per_job=RECENT_RUNS)
        now = self._clock()
        switches: dict[str, bool] = {}
        environments: dict[int | None, str | None] = {}
        owners: dict[int | None, str | None] = {}
        views: list[RuleView] = []
        for rule, job in zip(rules, jobs, strict=True):
            runs = recent[job.id]
            if rule.environment_id not in environments:
                environments[rule.environment_id] = await self._environment_name(
                    rule.environment_id
                )
            if actor.is_administrator and rule.created_by_user_id not in owners:
                owners[rule.created_by_user_id] = await self._username(rule.created_by_user_id)
            handler = self._subsystems.get(rule.subsystem)
            dry_run = dry_runs.get(job.id)
            views.append(
                RuleView(
                    rule=rule,
                    job=job,
                    state=await self._state_of(rule, job, runs, now, switches),
                    schedule=describe_schedule(job),
                    target=describe_target(rule.target_selector),
                    environment_name=environments[rule.environment_id],
                    owner=owners[rule.created_by_user_id] if actor.is_administrator else None,
                    paused_until=effective_pause(job, rule, now),
                    last_run=_last_real_run(runs),
                    recent=tuple(runs),
                    last_dry_run_id=None if dry_run is None else dry_run.id,
                    supports_dry_run=handler is not None and handler.supports_dry_run,
                    subsystem_enabled=switches[rule.subsystem],
                    available=handler is not None,
                    webhook=await self._webhook_of(rule),
                )
            )
        return views

    async def _webhook_of(self, rule: AutomationRule) -> WebhookView | None:
        if rule.subsystem != WEBHOOK_SUBSYSTEM:
            return None
        webhook = await self._webhooks.get(rule.id)
        return None if webhook is None else WebhookView.of(webhook)

    async def _state_of(
        self,
        rule: AutomationRule,
        job: ScheduledJob,
        runs: Sequence[JobRun],
        now: datetime,
        switches: dict[str, bool],
    ) -> JobState:
        if rule.subsystem not in switches:
            switches[rule.subsystem] = await read_subsystem_enabled(self._settings, rule.subsystem)
        return job_state(
            job,
            rule,
            running=bool(runs) and runs[0].outcome is None,
            subsystem_enabled=switches[rule.subsystem],
            available=self._subsystems.get(rule.subsystem) is not None,
            now=now,
        )

    async def _environment_name(self, environment_id: int | None) -> str | None:
        if environment_id is None:
            return None
        environment = await EnvironmentRepository(self._db, self._clock).get(environment_id)
        return None if environment is None else environment.name

    async def _owner_is_administrator(self, rule: AutomationRule) -> bool:
        if rule.created_by_user_id is None:
            return False
        owner = await UserRepository(self._db, self._clock).get(rule.created_by_user_id)
        return owner is not None and owner.role == "administrator"

    async def _username(self, user_id: int | None) -> str | None:
        if user_id is None:
            return None
        user = await UserRepository(self._db, self._clock).get(user_id)
        return None if user is None else user.username

    def _acts_on_swarm_stacks(self, subsystem: str) -> bool:
        handler = self._subsystems.get(subsystem)
        return isinstance(handler, Subsystem) and "swarm" in handler.stack_kinds

    def _handler(self, subsystem: str) -> Subsystem:
        handler = self._subsystems.get(subsystem)
        if (
            not SUBSYSTEMS.get(subsystem, False)
            or not isinstance(handler, Subsystem)
            or handler.parameters_model is None
        ):
            raise ValidationFailed({"subsystem": NOT_AVAILABLE})
        return handler

    async def authorise_write(self, actor: Principal, rule: AutomationRule) -> None:
        """Raise unless the actor may write the rule: create access and access to its targets."""
        handler = self._handler(rule.subsystem)
        await self._authorise_write(actor, handler, rule.environment_id, rule.target_selector)

    async def _authorise_write(
        self, actor: Principal, handler: Subsystem, environment_id: int | None, selector: str
    ) -> None:
        if environment_id is None:
            if not actor.is_administrator:
                raise PermissionDenied(ADMINISTRATOR_ONLY)
        else:
            await self._authoriser.require_create(actor, environment_id)
        await handler.authorise_target(actor, environment_id, selector, self._authoriser)

    async def _job_of(self, rule: AutomationRule) -> ScheduledJob:
        job = await self._jobs.get_for_rule(rule.id)
        if job is None:
            raise DatabaseError("a rule has no job")
        return job

    async def _jobs_of(self, rules: Sequence[AutomationRule]) -> list[ScheduledJob]:
        found = await self._jobs.get_for_rules([rule.id for rule in rules])
        try:
            return [found[rule.id] for rule in rules]
        except KeyError:
            raise DatabaseError("a rule has no job") from None

    async def _next_run(self, job: ScheduledJob, rule: AutomationRule) -> str | None:
        now: datetime = self._clock()
        return next_run_text(
            job,
            rule,
            subsystem_enabled=await read_subsystem_enabled(self._settings, rule.subsystem),
            available=self._subsystems.get(rule.subsystem) is not None,
            now=now,
        )


def _reviewed(
    rule_id: int, expected_updated_at: str | None, expected_digest: str | None
) -> RunGuard:
    def guard(job: ScheduledJob, current: AutomationRule | None) -> str | None:
        if (
            current is None
            or current.id != rule_id
            or (expected_updated_at is not None and current.updated_at != expected_updated_at)
            or (expected_digest is not None and rule_digest(current, job) != expected_digest)
        ):
            return CHANGED_BEFORE_RUN
        return None

    return guard


def rule_digest(rule: AutomationRule, job: ScheduledJob) -> str:
    """Return the SHA-256 hex digest of a rule's selector, parameters and schedule."""
    canonical = json.dumps(
        {
            "target_selector": _structured(rule.target_selector),
            "parameters": _structured(rule.parameters),
            "schedule_kind": job.schedule_kind,
            "schedule_expression": job.schedule_expression,
            "time_zone": job.time_zone,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _structured(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def describe_target(selector: str) -> str:
    """Describe a rule's stored target selector in words; an empty selector is the instance."""
    return describe_stored(selector) if selector else INSTANCE_TARGET


def _targets(
    selector: str,
    *,
    name: str,
    labels: Mapping[str, str],
    stack: str | None,
    kind: ObjectKind,
) -> bool:
    try:
        parsed = parse_selector_text(selector, ALL_KINDS)
    except ValidationFailed:
        return False
    if isinstance(parsed, StackTarget):
        owner = name if kind in ("stack", "swarm_stack") else stack
        return owner is not None and canonical_stack_key(owner) in {
            canonical_stack_key(each) for each in parsed.names
        }
    if kind == "service":
        return isinstance(parsed, ServiceTarget) and name in parsed.names
    if kind == "swarm_stack":
        return False
    if kind == "stack":
        return isinstance(parsed, EnvironmentTarget)
    return matches(parsed, name=name, labels=labels)


def _owns(actor: Principal, rule: AutomationRule) -> bool:
    # A rule with no environment is an administrator's alone, whoever created it.
    return rule.environment_id is not None and rule.created_by_user_id == actor.user_id


def _last_real_run(runs: Sequence[JobRun]) -> JobRun | None:
    return next((run for run in runs if run.trigger != "dry_run"), None)


def _check_name(name: str) -> str:
    clean = name.strip()
    if not clean or len(clean) > MAX_NAME_CHARS or not clean.isprintable():
        raise ValidationFailed({"name": NAME_RULE})
    return clean


def _check_selector(handler: Subsystem, text: str) -> str:
    if len(text.encode("utf-8")) > MAX_SELECTOR_BYTES:
        raise ValidationFailed({"target_selector": TOO_LARGE})
    return handler.parse_selector(text)


def _check_parameters(handler: Subsystem, raw: Mapping[str, Any]) -> tuple[str, int | None]:
    model = handler.parameters_model
    if model is None:
        raise ValidationFailed({"subsystem": NOT_AVAILABLE})
    try:
        validated = model.model_validate(dict(raw))
    except ValidationError as error:
        places = dict.fromkeys(
            ".".join(str(part) for part in item["loc"])[:MAX_REPORTED_FIELD_CHARS]
            for item in error.errors()
        )
        listed = ", ".join(list(places)[:MAX_REPORTED_FIELDS])
        raise ValidationFailed(
            {"parameters": f"These parameters are not valid: {listed}."}
        ) from None
    canonical = validated.model_dump_json()
    if len(canonical.encode("utf-8")) > MAX_PARAMETER_BYTES:
        raise ValidationFailed({"parameters": "Use at most 16 KiB."})
    return canonical, validated.max_consecutive_failures


def _stored_object(text: str) -> dict[str, Any]:
    try:
        loaded = json.loads(text)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _prunes(parameters: str) -> bool:
    return _stored_object(parameters).get("prune_replaced") is True


def _prune_hours(parameters: str) -> int:
    hours = _stored_object(parameters).get("prune_after_hours")
    return hours if isinstance(hours, int) and not isinstance(hours, bool) else DEFAULT_PRUNE_HOURS


def _check_prune(
    subsystem: str, canonical: str, selector: str, *, owner_is_administrator: bool
) -> None:
    if subsystem != UPDATER or not _prunes(canonical):
        return
    message = None
    if _stored_object(canonical).get("action") != "update":
        message = PRUNE_NEEDS_UPDATE
    elif isinstance(parse_selector_text(selector, ALL_KINDS), ServiceTarget):
        message = PRUNE_NOT_FOR_SERVICES
    elif not owner_is_administrator:
        message = PRUNE_NEEDS_ADMINISTRATOR
    if message is not None:
        raise ValidationFailed({"prune_replaced": message})


def _normalised(handler: Subsystem, stored: str) -> str:
    """Return stored parameters as the model writes them, so a later default does not differ."""
    model = handler.parameters_model
    if model is None:
        return stored
    try:
        return model.model_validate_json(stored).model_dump_json()
    except ValidationError:
        return stored


def _check_schedule(handler: Subsystem, kind: str, expression: str, time_zone: str) -> Schedule:
    if handler.schedule_kinds == frozenset({"webhook"}):
        return parse_schedule("webhook", "", "UTC")
    if kind not in handler.schedule_kinds:
        raise ValidationFailed({"schedule": "This automation does not support that schedule type."})
    return parse_schedule(
        kind, expression.strip(), time_zone, min_interval=handler.min_interval_seconds
    )


def _check_overrun(handler: Subsystem, requested: str | None) -> OverrunPolicy:
    if requested is None:
        return handler.overrun_policy
    for policy in handler.allowed_overrun_policies:
        if policy == requested:
            if policy == "cancel_previous" and not handler.cancellation_safe:
                raise ValidationFailed(
                    {"overrun_policy": "This automation cannot be cancelled safely."}
                )
            return policy
    raise ValidationFailed({"overrun_policy": "This automation does not allow that policy."})
