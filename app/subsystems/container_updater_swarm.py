"""The Swarm paths of container-updater: one service, or the services of a Swarm stack."""

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal

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
from app.db.repositories.image_binding import ImageBindingRepository
from app.db.repositories.stack import StoredStack
from app.docker_engine.client import EngineClient
from app.docker_engine.models import SwarmService
from app.scheduler.model import RunFailed, RunSkipped, reason_of
from app.scheduler.schedules import IntervalSchedule, ScheduleInvalid, parse_schedule
from app.services.access import ResourceKey, canonical_stack_key, is_canonical_id
from app.services.acting import AutomationAuthority
from app.services.authorisation import Authoriser
from app.services.containers import parse_engine_time
from app.services.image_bindings import parse_pull_reference
from app.services.image_updates import UpdateStatus
from app.services.object_inspector import HostClients, ObjectTooLarge
from app.services.object_locks import ObjectKey
from app.services.pull_credentials import PullCredentials
from app.services.service_spec import image_of, mode_of, replicas_of
from app.services.stack_common import changed_since_deploy
from app.services.stack_deploy import StackRejected
from app.services.swarm_common import ServiceImage, split_service_image
from app.services.swarm_service_views import ServiceUpdateFailed, ServiceVerifyPolicy
from app.services.swarm_stacks import read_namespace
from app.subsystems.acting_run import (
    PERMISSION_DENIED,
    act_on,
    check_suppressed,
    forecast_busy,
    permitted,
    rechecking,
    record_unwritable,
)
from app.subsystems.base import TargetPreview
from app.subsystems.container_updater import (
    COMPOSE_UNAVAILABLE,
    MAX_REDEPLOYS,
    REDEPLOYED_ACTION,
    REFRESHED,
    REFUSALS,
    REFUSED_ACTION,
    SIGNATURE_LIMIT,
    TARGET_MISSING,
    _Pass,
    _pull_refusal,
    _Refusal,
    _refusal_reason,
    credential_refusals,
    refusal_signature,
)
from app.subsystems.run_services import REGISTRY_RUNTIME_MESSAGE, REGISTRY_RUNTIME_MISSING
from app.subsystems.selectors import (
    Selector,
    ServiceTarget,
    StackLookup,
    StackTarget,
    find_service,
)

UPDATED_ACTION: Final = "service_updated"
FORCE_UPDATED_ACTION: Final = "service_force_updated"
ROLLED_BACK_ACTION: Final = "service_rolled_back"
REFRESHED_ACTION: Final = "service_credentials_refreshed"
REFRESH_INTERVAL: Final = timedelta(hours=6)
MAX_SERVICES: Final = 200
MAX_REFRESHES: Final = 25
ROLLBACK_REASONS: Final = frozenset({"update_paused", "tasks_not_running"})
ROLLED_BACK_BY_SWARM: Final = frozenset({"rolled_back_by_swarm", "rollback_paused"})
IN_PROGRESS: Final = frozenset({"updating", "rollback_started"})
PAUSED: Final = frozenset({"paused", "rollback_paused"})
SERVICE_HOLD_ACTIONS: Final = frozenset({REFUSED_ACTION, UPDATED_ACTION, REFRESHED_ACTION})
STACK_HOLD_ACTIONS: Final = frozenset({REFUSED_ACTION, REDEPLOYED_ACTION})
STACK_REFUSED_FOR_GOOD: Final = frozenset({"would_remove", "would_create", "drifted", "restricted"})
REFRESH_REFUSED_FOR_GOOD: Final = frozenset({"legacy_networks"})
ROLLBACK_STATES: Final = frozenset({"rollback_completed", "rollback_paused"})
_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")

type _Credentials = PullCredentials | BaseException
type _CredentialKey = tuple[str, timedelta | None]


@dataclass(frozen=True, slots=True)
class _Member:
    service: SwarmService
    key: ObjectKey
    name: str
    image: ServiceImage
    mode: Literal["replicated", "global", "job"] | None
    replicas: int | None
    running: int
    state: str | None
    rollback_at: datetime | None
    previous: ServiceImage | None

    @property
    def reference(self) -> str | None:
        """Return the text the service's binding is found by, None when it has none."""
        match self.image.kind:
            case "tagged_digest" | "unpinned":
                return self.image.reference
            case "digest_only":
                return self.image.text.strip()
            case _:
                return None


@dataclass(frozen=True, slots=True)
class _Judged:
    member: _Member
    reference: str
    status: UpdateStatus
    refusal: str | None
    available: bool


@dataclass(frozen=True, slots=True)
class _Update:
    kind: Literal["service", "swarm_stack"]
    key: ObjectKey
    target_id: str
    subject: str
    name: str
    update: bool
    reference: str = ""
    digest: str = ""
    version: int = 0
    image_before: str = ""
    refusals: tuple[tuple[str, str], ...] = ()
    members: tuple[ObjectKey, ...] = ()
    updated: int = 0


@dataclass(frozen=True, slots=True)
class _Refresh:
    key: ObjectKey
    service_id: str
    name: str
    reference: str
    stack: ObjectKey | None


@dataclass(slots=True)
class SwarmPass:
    """What one run does to a Swarm service or stack, sharing the run's tally and counts."""

    host: _Pass
    updates: list[_Update] = field(default_factory=list)
    refreshes: list[_Refresh] = field(default_factory=list)
    would_refresh: list[str] = field(default_factory=list)
    credentials: dict[_CredentialKey, _Credentials] = field(default_factory=dict)
    engaged: bool = False

    async def plan_service(self) -> None:
        """Decide whether the named service is moved to a newer digest of its image."""
        host = self.host
        context, tally = host.context, host.tally
        self.engaged = True
        selector = host.selector
        if not isinstance(selector, ServiceTarget):
            raise RunFailed("selector_invalid", "The automation's target is not valid.")
        hosts = host.services.hosts
        try:
            environment = await hosts.environment(host.environment_id)
        except ResourceNotFound:
            raise RunFailed("target_missing", TARGET_MISSING) from None
        if environment.type != "swarm":
            raise RunFailed("target_missing", TARGET_MISSING)
        found = await find_service(
            await hosts.client(host.environment_id),
            selector.names[0],
            host.environment_id,
            host.deps.engine.refresher.poke,
        )
        if found is None:
            raise RunFailed("target_missing", TARGET_MISSING)
        member = self._member(found)
        standing = await context.standing(member.key)
        if standing == "hidden":
            raise RunFailed("target_missing", TARGET_MISSING)
        host.matched += 1
        if found.spec.labels_truncated:
            tally.skip("unaddressable")
            return
        if standing != "writable":
            record_unwritable(tally, explicit=True, name=member.name)
            return
        if host.self_identity.protects_service(host.environment_id, member.service.id):
            tally.skip(SELF_PROTECTED_REASON)
            return
        if await host.suppressed([member.key]):
            return
        skip = _service_skip(member)
        if skip is not None:
            tally.skip(skip)
            return
        if host.settings.redeploy:
            await self._plan_force_update(member)
            return
        judged = await self._judge([member])
        if not host.planning_update:
            return
        for item in judged:
            await self._plan_service_update(item)
        queued = {planned.target_id for planned in self.updates}
        await self._plan_refresh([member], queued, stack=None)

    async def plan_stack(self, row: StoredStack) -> None:
        """Decide whether the Swarm stack is redeployed, from the images of its services."""
        host = self.host
        context, tally = host.context, host.tally
        self.engaged = True
        environment_id = host.environment_id
        key = ObjectKey(environment_id, "stack", canonical_stack_key(row.name))
        if await context.standing(key) == "hidden":
            raise RunFailed("target_missing", TARGET_MISSING)
        try:
            client = await host.services.hosts.client(environment_id)
            namespace = await read_namespace(
                client, environment_id, host.deps.engine.refresher.poke, key.object_id
            )
        except ObjectTooLarge as error:
            tally.count("failed", reason=reason_of(error))
            return
        services = sorted(namespace.services, key=lambda service: service.spec.name)
        if len(services) > MAX_SERVICES:
            tally.count("deferred")
            return
        host.matched += 1
        keys = tuple(
            ObjectKey(environment_id, "service", service.id)
            for service in services
            if is_canonical_id("service", service.id)
        )
        if len(keys) != len(services) or not await permitted(context, key, *keys):
            tally.count("failed", reason=PERMISSION_DENIED)
            return
        if any(host.self_identity.protects_service(environment_id, s.id) for s in services):
            tally.skip(SELF_PROTECTED_REASON)
            return
        if await host.suppressed((key, *keys)):
            return
        if changed_since_deploy(
            row.compose_file_digest,
            row.deployed_file_digest,
            row.inputs_digest,
            row.deployed_inputs_digest,
        ):
            tally.skip("pending_changes")
            return
        members = [self._member(service) for service in services]
        skip = _stack_skip(members)
        if skip is not None:
            tally.skip(skip)
            return
        if host.settings.redeploy:
            await self._plan_stack_redeploy(row.name, key, keys, members)
            return
        references = {member.image.reference for member in members if _is_checked(member)}
        if not host.fits_references([r for r in references if r is not None]):
            tally.count("deferred")
            return
        judged = await self._judge(members)
        if not host.planning_update:
            return
        queued = await self._plan_redeploy(row.name, key, keys, judged, members)
        excluded: set[str] = set()
        if queued == "update":
            excluded = {member.service.id for member in members}
        elif queued == "refusal":
            excluded = {item.member.service.id for item in judged if item.refusal is not None}
        await self._plan_refresh(members, excluded, stack=key)

    async def carry_out(self) -> None:
        """Do what the plans hold in name order, then the refreshes; a dry run only forecasts."""
        host = self.host
        context, tally = host.context, host.tally
        for plan in sorted(self.updates, key=lambda planned: planned.name):
            tally.begin(host.origin.get(plan.key))
            if plan.update:
                if host.redeploys >= MAX_REDEPLOYS or host.late():
                    tally.count("deferred")
                    continue
                host.redeploys += 1
            if context.dry_run:
                self._forecast(plan)
                continue
            if (
                plan.update
                and plan.kind == "swarm_stack"
                and host.services.swarm_stack_actions is None
            ):
                raise RunFailed("compose_unavailable", COMPOSE_UNAVAILABLE)
            await act_on(
                context,
                plan.key,
                explicit=True,
                single=host.single,
                work=self._service_work(plan) if plan.kind == "service" else self._stack_work(plan),
                tally=tally,
                name=plan.name,
            )
        await self._carry_out_refreshes()

    def _member(self, service: SwarmService) -> _Member:
        view = {"Mode": service.spec.mode, "TaskTemplate": service.spec.task_template}
        counts = service.service_status
        update = service.update_status
        previous = service.previous_spec
        return _Member(
            service=service,
            key=ObjectKey(self.host.environment_id, "service", service.id),
            name=service.spec.name,
            image=split_service_image(image_of(view) or ""),
            mode=mode_of(view),
            replicas=replicas_of(view),
            running=0 if counts is None else counts.running_tasks,
            state=None if update is None else update.state,
            rollback_at=None
            if update is None
            else parse_engine_time(update.completed_at) or parse_engine_time(update.started_at),
            previous=None
            if previous is None
            else split_service_image(image_of({"TaskTemplate": previous.task_template}) or ""),
        )

    async def _judge(self, members: Sequence[_Member]) -> list[_Judged]:
        """Count each member's image and check the ones that carry a tag and a digest."""
        host = self.host
        tally, counts = host.tally, host.counts
        tagged: list[_Member] = []
        for member in members:
            match member.image.kind:
                case "digest_only":
                    tally.skip("pinned")
                case "unpinned":
                    tally.skip("unpinned")
                case "image_id":
                    tally.skip("image_id_reference")
                case "tagged_digest":
                    tagged.append(member)
        pairs = list(dict.fromkeys(_pair_of(member) for member in tagged))
        references = list(dict.fromkeys(reference for reference, _ in pairs))
        if not host.fits_references(references):
            tally.count("deferred")
            return []
        host.charge_references(references)
        statuses = await self._statuses(pairs)
        judged: list[_Judged] = []
        for member in tagged:
            reference, deployed = _pair_of(member)
            status = statuses[(reference, deployed)]
            refusal = _refusal_reason(status)
            if refusal is not None:
                if host.planning_update:
                    judged.append(_Judged(member, reference, status, refusal, False))
                else:
                    counts["unknown"] += 1
                continue
            match status.state:
                case "update_available" if _usable(status, deployed):
                    counts["update_available"] += 1
                    host.available.append(status.display)
                    judged.append(_Judged(member, reference, status, None, True))
                case "up_to_date":
                    counts["up_to_date"] += 1
                case "pinned":
                    tally.skip("pinned")
                case _:
                    counts["unknown"] += 1
        return judged

    async def _statuses(
        self, pairs: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], UpdateStatus]:
        """Check each reference against the digest it runs, never one reference twice per call."""
        rounds: list[dict[str, str]] = []
        for reference, deployed in pairs:
            for batch in rounds:
                if reference not in batch:
                    batch[reference] = deployed
                    break
            else:
                rounds.append({reference: deployed})
        found: dict[tuple[str, str], UpdateStatus] = {}
        for batch in rounds:
            results = await self.host.check_references(list(batch), batch)
            for reference, status in results.items():
                found[(reference, batch[reference])] = status
        return found

    async def _plan_force_update(self, member: _Member) -> None:
        """Plan a force update of the service, or count it held."""
        host = self.host
        service_id = member.service.id
        if await self._refused_for_good(
            "service", service_id, FORCE_UPDATED_ACTION, frozenset({"restricted"})
        ):
            host.tally.count("held")
            return
        self.updates.append(
            _Update(
                "service",
                member.key,
                service_id,
                member.name,
                f"service {member.name}",
                True,
                version=member.service.version.index,
            )
        )

    async def _plan_stack_redeploy(
        self,
        stack_name: str,
        key: ObjectKey,
        keys: tuple[ObjectKey, ...],
        members: Sequence[_Member],
    ) -> None:
        """Plan the stack's redeploy, or count it held."""
        host = self.host
        if await self._refused_for_good(
            "stack",
            stack_name,
            REDEPLOYED_ACTION,
            STACK_REFUSED_FOR_GOOD,
            members=[member.service.id for member in members],
        ):
            host.tally.count("held")
            return
        self.updates.append(
            _Update(
                "swarm_stack",
                key,
                stack_name,
                stack_name,
                f"stack {stack_name}",
                True,
                members=keys,
            )
        )

    async def _plan_service_update(self, item: _Judged) -> None:
        """Plan the service's update or refusal, or count it held."""
        host = self.host
        member = item.member
        service_id = member.service.id
        name = f"service {member.name}"
        if item.refusal is not None:
            refusals = ((item.reference, item.refusal),)
            held = await host.refusal_held(
                "service",
                service_id,
                SERVICE_HOLD_ACTIONS,
                lambda detail: detail.get("refused") == refusal_signature(refusals),
            )
            if held:
                host.tally.count("held")
                return
            self.updates.append(
                _Update(
                    "service",
                    member.key,
                    service_id,
                    member.name,
                    name,
                    False,
                    reference=item.reference,
                    refusals=refusals,
                )
            )
            return
        digest = item.status.remote_digest or ""
        if await self._rolled_back(member, digest) or await self._refused_for_good(
            "service", service_id, UPDATED_ACTION, frozenset({"restricted"})
        ):
            host.tally.count("held")
            return
        self.updates.append(
            _Update(
                "service",
                member.key,
                service_id,
                member.name,
                name,
                True,
                reference=item.reference,
                digest=digest,
                version=member.service.version.index,
                image_before=member.image.text,
            )
        )

    async def _plan_redeploy(
        self,
        stack_name: str,
        key: ObjectKey,
        keys: tuple[ObjectKey, ...],
        judged: Sequence[_Judged],
        members: Sequence[_Member],
    ) -> Literal["update", "refusal"] | None:
        """Plan the stack's redeploy or refusal; None when neither is queued."""
        host = self.host
        refused = {item.reference: item.refusal for item in judged if item.refusal is not None}
        updatable = {item.reference for item in judged if item.available}
        name = f"stack {stack_name}"
        if not refused and updatable and await self._standing_refusal(stack_name):
            host.tally.count("held")
            return None
        if refused:
            owed = tuple(sorted(refused.items()))
            held = await host.refusal_held(
                "stack",
                stack_name,
                STACK_HOLD_ACTIONS,
                lambda detail: detail.get("refused") == refusal_signature(owed),
            )
            if held:
                host.tally.count("held")
                return None
            self.updates.append(
                _Update("swarm_stack", key, stack_name, stack_name, name, False, refusals=owed)
            )
            return "refusal"
        if not updatable:
            return None
        if await self._refused_for_good(
            "stack",
            stack_name,
            REDEPLOYED_ACTION,
            STACK_REFUSED_FOR_GOOD,
            members=[member.service.id for member in members],
        ) or await self._undone_by_swarm(stack_name, members, judged):
            host.tally.count("held")
            return None
        self.updates.append(
            _Update(
                "swarm_stack",
                key,
                stack_name,
                stack_name,
                name,
                True,
                members=keys,
                updated=len(updatable),
            )
        )
        return "update"

    async def _undone_by_swarm(
        self, stack_name: str, members: Sequence[_Member], judged: Sequence[_Judged]
    ) -> bool:
        """Report whether Swarm rolled a member back after this rule's newest redeploy.

        Held while the member would be sent the digest that failed. The failed digest is the
        previous spec's when the Engine kept one, else the digest the binding last saw sent.
        """
        host = self.host
        rolled_at = {
            member.service.id: member.rollback_at
            for member in members
            if member.state in ROLLBACK_STATES and member.rollback_at is not None
        }
        again = [item for item in judged if item.available and item.member.service.id in rolled_at]
        if not again:
            return False
        events = ActivityEventRepository(host.context.db)
        newest = await events.latest_subsystem_event(
            host.environment_id,
            "stack",
            stack_name,
            subsystem=host.settings.subsystem,
            rule_id=host.rule_id,
            actions={REDEPLOYED_ACTION},
        )
        if newest is None:
            return False
        sent_at = parse_timestamp(newest.occurred_at)
        after = [item for item in again if rolled_at[item.member.service.id] > sent_at]
        failed = await self._failed_digests(after)
        if not any(failed[item.reference] in (None, item.status.remote_digest) for item in after):
            return False
        return not await host.released(
            events,
            "stack",
            stack_name,
            newest.occurred_at,
            also=[("service", member.service.id) for member in members],
        )

    async def _failed_digests(self, items: Sequence[_Judged]) -> dict[str, str | None]:
        """Return, by reference, the digest a rolled-back member was last sent."""
        host = self.host
        found: dict[str, str | None] = {}
        keyed: dict[str, str] = {}
        for item in items:
            previous = item.member.previous
            if previous is not None and previous.digest is not None:
                found[item.reference] = previous.digest
                continue
            try:
                keyed[item.reference] = parse_pull_reference(item.reference).key
            except ValidationFailed:
                found[item.reference] = None
        if keyed:
            bindings = await ImageBindingRepository(host.context.db).for_references(
                host.environment_id, list(dict.fromkeys(keyed.values()))
            )
            for reference, binding_key in keyed.items():
                binding = bindings.get(binding_key)
                found[reference] = None if binding is None else binding.observed_digest
        return found

    async def _standing_refusal(self, stack_name: str) -> bool:
        """Report whether a refusal the redeploy itself raised still holds the stack.

        The redeploy resolves every reference of the stack's file, including ones the update check
        never sees, so the references it refused are read back from the refusal row and judged
        again from the bindings.
        """
        host = self.host
        events = ActivityEventRepository(host.context.db)
        newest = await events.latest_subsystem_event(
            host.environment_id,
            "stack",
            stack_name,
            subsystem=host.settings.subsystem,
            rule_id=host.rule_id,
            actions=STACK_HOLD_ACTIONS,
        )
        if newest is None or newest.action != REFUSED_ACTION:
            return False
        if await host.released(events, "stack", stack_name, newest.occurred_at):
            return False
        signature = (newest.detail or {}).get("refused")
        if not isinstance(signature, str):
            return False
        if len(signature) >= SIGNATURE_LIMIT:
            return True
        for item in signature.split(","):
            reference, _, reason = item.rpartition("=")
            if not reference or reason not in REFUSALS or await self._still_refused(reference):
                return True
        return False

    async def _still_refused(self, reference: str) -> bool:
        """Report whether the reference's binding still fails the checks a pull makes.

        The binding, the environment and set grants and the registry address are judged as the
        redeploy judges them, without reading the secret.
        """
        host = self.host
        resolver = host.services.pull_credentials
        if resolver is None:
            return True
        try:
            decision = await resolver.check_usable(host.environment_id, reference, principal=None)
            if decision.choice.credential_set_id is not None:
                await host.context.authorise_credential_set(decision.choice.credential_set_id)
        except ProductError:
            return True
        return False

    async def _rolled_back(self, member: _Member, remote_digest: str) -> bool:
        """Report whether the newest update of this digest was undone, by this rule or by Swarm."""
        host = self.host
        service_id = member.service.id
        events = ActivityEventRepository(host.context.db)
        newest = await events.latest_subsystem_event(
            host.environment_id,
            "service",
            service_id,
            subsystem=host.settings.subsystem,
            rule_id=host.rule_id,
            actions={UPDATED_ACTION, ROLLED_BACK_ACTION},
        )
        if newest is None:
            return False
        detail = newest.detail or {}
        if newest.action == ROLLED_BACK_ACTION:
            same = detail.get("failed_digest") == remote_digest
        elif newest.result == "failure":
            same = (
                detail.get("verify_failure") in ROLLED_BACK_BY_SWARM
                and detail.get("digest_after") == remote_digest
            )
        else:
            same = (
                detail.get("verify_failure") == "still_updating"
                and detail.get("digest_after") == remote_digest
                and member.state in ROLLBACK_STATES
            )
        return same and not await host.released(events, "service", service_id, newest.occurred_at)

    async def _refused_for_good(
        self,
        target_type: str,
        target_id: str,
        action: str,
        reasons: frozenset[str],
        *,
        members: Sequence[str] = (),
    ) -> bool:
        """Report whether the rule's newest `action` on the target failed for a repeating reason.

        A user's action on one of the member services releases it like one on the target.
        """
        return await self.host.refused_for_good(
            target_type,
            target_id,
            action,
            reasons,
            members=[("service", member) for member in members],
        )

    async def _plan_refresh(
        self, members: Sequence[_Member], excluded: set[str], *, stack: ObjectKey | None
    ) -> None:
        """Plan the renewal of the stored credential of each member bound to an Amazon ECR set."""
        host = self.host
        tally = host.tally
        if not host.settings.refresh:
            return
        wanted: dict[str, _Member] = {}
        keys: dict[str, str] = {}
        for member in members:
            reference = member.reference
            if reference is None or member.service.id in excluded:
                continue
            try:
                keys[member.service.id] = parse_pull_reference(reference).key
            except ValidationFailed:
                continue
            wanted[member.service.id] = member
        if not wanted:
            return
        bindings = await ImageBindingRepository(host.context.db).for_references(
            host.environment_id, list(dict.fromkeys(keys.values()))
        )
        for service_id, member in wanted.items():
            binding = bindings.get(keys[service_id])
            reference = member.reference
            if (
                reference is None
                or binding is None
                or binding.state != "bound"
                or binding.credential_provider != "ecr"
            ):
                continue
            if await self._refused_for_good(
                "service", service_id, REFRESHED_ACTION, REFRESH_REFUSED_FOR_GOOD
            ):
                tally.count("held")
                continue
            if not await self._refresh_due(service_id):
                tally.skip("refresh_not_due")
                continue
            if await host.refusal_held(
                "service", service_id, SERVICE_HOLD_ACTIONS, _names(reference)
            ):
                tally.count("held")
                continue
            self.refreshes.append(_Refresh(member.key, service_id, member.name, reference, stack))

    async def _refresh_due(self, service_id: str) -> bool:
        host = self.host
        newest = await ActivityEventRepository(host.context.db).latest_subsystem_event(
            host.environment_id,
            "service",
            service_id,
            subsystem=host.settings.subsystem,
            rule_id=None,
            actions={REFRESHED_ACTION, UPDATED_ACTION},
        )
        if newest is None or newest.result != "success":
            return True
        return host.context.clock() - parse_timestamp(newest.occurred_at) >= REFRESH_INTERVAL

    def _forecast(self, plan: _Update) -> None:
        """Say what a real run would do with the plan, taking no lock."""
        host = self.host
        if forecast_busy(host.deps.engine.locks, [plan.key, *plan.members], single=host.single):
            host.tally.count("busy")
            return
        host.counts["refused"] += len(plan.refusals)
        if plan.update:
            host.tally.count("done", name=plan.name)

    async def _carry_out_refreshes(self) -> None:
        host = self.host
        context, tally = host.context, host.tally
        started = 0
        for refresh in sorted(self.refreshes, key=lambda planned: planned.name):
            tally.begin(host.origin.get(refresh.key))
            if started >= MAX_REFRESHES or host.late():
                tally.count("deferred")
                continue
            started += 1
            alone = refresh.stack is None
            if context.dry_run:
                keys = [refresh.key] if refresh.stack is None else [refresh.key, refresh.stack]
                if forecast_busy(host.deps.engine.locks, keys, single=alone and host.single):
                    tally.count("busy")
                else:
                    self.would_refresh.append(f"service {refresh.name}")
                continue
            await act_on(
                context,
                refresh.key,
                explicit=True,
                single=alone and host.single,
                work=self._refresh_work(refresh),
                tally=tally,
                name=refresh.name,
                on_success="skipped",
                success_reason=REFRESHED,
            )

    def _service_work(self, plan: _Update) -> Callable[[AutomationAuthority], Awaitable[None]]:
        host = self.host

        async def work(authority: AutomationAuthority) -> None:
            if not plan.update:
                await self._refuse(authority, "service", plan, plan.refusals)
                raise REFUSALS[plan.refusals[0][1]]()
            actions = host.services.service_actions
            if actions is None:
                raise RunFailed(REGISTRY_RUNTIME_MISSING, REGISTRY_RUNTIME_MESSAGE)
            if host.settings.redeploy:
                await actions.force_update(
                    authority,
                    host.environment_id,
                    plan.target_id,
                    expected_version=plan.version,
                    note=host.note(),
                )
                return
            credentials = await self._credentials_or_refuse(authority, "service", plan)
            host.require_time()
            try:
                await actions.update_image(
                    authority,
                    host.environment_id,
                    plan.target_id,
                    reference=plan.reference,
                    remote_digest=plan.digest,
                    credentials=credentials,
                    expected_version=plan.version,
                    verify=ServiceVerifyPolicy(host.params.verify_seconds),
                    note=host.note(reference=plan.reference[:SIGNATURE_LIMIT]),
                )
            except ServiceUpdateFailed as failure:
                if failure.reason in ROLLBACK_REASONS:
                    await self._roll_back(authority, plan, failure)
                raise

        return work

    async def _roll_back(
        self, authority: AutomationAuthority, plan: _Update, failure: ServiceUpdateFailed
    ) -> None:
        """Return the service to its previous configuration; the update stays failed either way."""
        host = self.host
        actions = host.services.service_actions
        if actions is None or failure.version is None:
            host.counts["rollback_failed"] += 1
            return
        note = host.note(
            rollback_reason=failure.reason,
            reference=plan.reference[:SIGNATURE_LIMIT],
            failed_digest=plan.digest,
        )
        try:
            await actions.rollback(
                authority,
                host.environment_id,
                plan.target_id,
                expected_version=failure.version,
                note=note,
                expected_image=f"{plan.reference}@{plan.digest}",
                expected_previous_image=plan.image_before,
            )
        except (ProductError, RunSkipped):
            host.counts["rollback_failed"] += 1
        else:
            host.counts["rolled_back"] += 1

    def _stack_work(self, plan: _Update) -> Callable[[AutomationAuthority], Awaitable[None]]:
        host = self.host

        async def work(authority: AutomationAuthority) -> None:
            if not plan.update:
                await self._refuse(authority, "stack", plan, plan.refusals)
                raise REFUSALS[plan.refusals[0][1]]()
            actions = host.services.swarm_stack_actions
            if actions is None:
                raise RunFailed("compose_unavailable", COMPOSE_UNAVAILABLE)
            host.require_time()
            for held in (plan.key, *plan.members):
                await check_suppressed(host.context, held, single=True)
            try:
                await actions.redeploy(
                    rechecking(host.context, authority, plan.members),
                    host.environment_id,
                    plan.subject,
                    note=host.note()
                    if host.settings.redeploy
                    else host.note(references_updated=plan.updated),
                )
            except StackRejected as rejected:
                refusals = credential_refusals(rejected)
                if not refusals:
                    raise
                await self._refuse(authority, "stack", plan, refusals)
                raise REFUSALS[refusals[0][1]]() from rejected

        return work

    def _refresh_work(self, refresh: _Refresh) -> Callable[[AutomationAuthority], Awaitable[None]]:
        host = self.host

        async def work(authority: AutomationAuthority) -> None:
            actions = host.services.service_actions
            if actions is None:
                raise RunFailed(REGISTRY_RUNTIME_MISSING, REGISTRY_RUNTIME_MESSAGE)
            try:
                credentials = await self._credentials(
                    authority, refresh.reference, min_remaining=self._refresh_horizon()
                )
            except _Refusal as refusal:
                await host.refuse(
                    authority,
                    "service",
                    refresh.service_id,
                    refresh.name,
                    ((refresh.reference, refusal.reason),),
                )
                raise
            host.require_time()
            if refresh.stack is not None:
                await check_suppressed(host.context, refresh.stack, single=True)
            await actions.refresh_credentials(
                authority,
                host.environment_id,
                refresh.service_id,
                reference=refresh.reference,
                credentials=credentials,
            )

        return work

    async def _credentials_or_refuse(
        self, authority: AutomationAuthority, target_type: str, plan: _Update
    ) -> PullCredentials:
        try:
            return await self._credentials(authority, plan.reference)
        except _Refusal as refusal:
            await self._refuse(authority, target_type, plan, ((plan.reference, refusal.reason),))
            raise

    async def _refuse(
        self,
        authority: AutomationAuthority,
        target_type: str,
        plan: _Update,
        refusals: Sequence[tuple[str, str]],
    ) -> None:
        name = plan.subject if target_type == "service" else plan.name
        await self.host.refuse(authority, target_type, plan.target_id, name, refusals)

    def _refresh_horizon(self) -> timedelta:
        """Return how long a refreshed token must last: until the next refresh can run."""
        job = self.host.context.dispatch.job
        try:
            schedule = parse_schedule(
                job.schedule_kind, job.schedule_expression, job.time_zone, min_interval=1
            )
            if isinstance(schedule, IntervalSchedule):
                period = timedelta(seconds=schedule.seconds)
            else:
                following = schedule.next_after(self.host.context.dispatch.now)
                period = schedule.next_after(following) - following
        except ScheduleInvalid:
            return REFRESH_INTERVAL
        return max(period, REFRESH_INTERVAL)

    async def _credentials(
        self,
        authority: AutomationAuthority,
        reference: str,
        *,
        min_remaining: timedelta | None = None,
    ) -> PullCredentials:
        """Resolve the reference's bound credentials once per run; no other set is ever tried.

        A derived token with less than `min_remaining` of life left is derived anew; results are
        kept per reference and threshold.
        """
        cache_key = (reference, min_remaining)
        known = self.credentials.get(cache_key)
        if known is not None:
            if isinstance(known, BaseException):
                raise known.with_traceback(None)
            return known
        resolver = self.host.services.pull_credentials
        if resolver is None:
            raise RunFailed(REGISTRY_RUNTIME_MISSING, REGISTRY_RUNTIME_MESSAGE)
        try:
            resolved = await (
                resolver.resolve(self.host.environment_id, reference, principal=None, choice=None)
                if min_remaining is None
                else resolver.resolve(
                    self.host.environment_id,
                    reference,
                    principal=None,
                    choice=None,
                    min_remaining=min_remaining,
                )
            )
            if resolved.credential_set_id is not None:
                await authority.authorise_credential_set(resolved.credential_set_id)
        except Exception as error:
            refusal = _pull_refusal(error)
            self.credentials[cache_key] = refusal or error
            if refusal is None:
                raise
            raise refusal from error
        self.credentials[cache_key] = resolved
        return resolved


def _service_skip(member: _Member) -> str | None:
    if member.mode == "job":
        return "job_mode"
    if member.replicas == 0:
        return "not_running"
    if member.state in IN_PROGRESS:
        return "update_in_progress"
    if member.state in PAUSED:
        return "update_paused"
    return None


def _stack_skip(members: Sequence[_Member]) -> str | None:
    if any(member.mode == "job" for member in members):
        return "job_mode"
    if any(member.state in IN_PROGRESS for member in members):
        return "update_in_progress"
    if any(member.state in PAUSED for member in members):
        return "update_paused"
    if not any(member.running > 0 for member in members):
        return "not_running"
    return None


def _names(reference: str) -> Callable[[Mapping[str, DetailValue]], bool]:
    return lambda detail: detail.get("reference") == reference[:SIGNATURE_LIMIT]


def _is_checked(member: _Member) -> bool:
    return member.image.kind == "tagged_digest"


def _pair_of(member: _Member) -> tuple[str, str]:
    reference, digest = member.image.reference, member.image.digest
    assert reference is not None and digest is not None
    return reference, digest


def _usable(status: UpdateStatus, deployed: str) -> bool:
    remote = status.remote_digest
    return remote is not None and remote != deployed and _DIGEST.fullmatch(remote) is not None


async def preview_swarm(
    selector: Selector,
    principal: Principal,
    environment_id: int,
    authoriser: Authoriser,
    hosts: HostClients,
    client: EngineClient,
    stacks: StackLookup,
    poke: Callable[[int], None],
) -> TargetPreview | None:
    """List the Swarm service or stack the viewer can read; None for any other target."""
    match selector:
        case ServiceTarget(names=[name]):
            if (await hosts.environment(environment_id)).type != "swarm":
                return TargetPreview((), 0)
            found = await find_service(client, name, environment_id, poke)
            if found is None or not await _may_read(
                authoriser, principal, ResourceKey(environment_id, "service", found.id)
            ):
                return TargetPreview((), 0)
            return TargetPreview((found.spec.name,), 0, _mode_note(found))
        case StackTarget(names=[name]):
            row = await stacks.get_folded(environment_id, name)
            if row is None or row.kind != "swarm":
                return None
            if not await _may_read(
                authoriser, principal, ResourceKey(environment_id, "stack", row.name)
            ):
                return TargetPreview((), 0)
            try:
                namespace = await read_namespace(
                    client, environment_id, poke, canonical_stack_key(row.name)
                )
            except ObjectTooLarge:
                return TargetPreview((row.name,), 0)
            count = 0
            for service in namespace.services:
                if is_canonical_id("service", service.id) and await _may_read(
                    authoriser, principal, ResourceKey(environment_id, "service", service.id)
                ):
                    count += 1
            return TargetPreview(
                (row.name,), 0, f"{count} {'service' if count == 1 else 'services'}"
            )
        case _:
            return None


async def _may_read(authoriser: Authoriser, principal: Principal, key: ResourceKey) -> bool:
    try:
        await authoriser.require_access(principal, key, "read")
    except (PermissionDenied, ResourceNotFound, ObjectTooLarge):
        return False
    return True


def _mode_note(service: SwarmService) -> str | None:
    view = {"Mode": service.spec.mode}
    match mode_of(view):
        case "global":
            return "global"
        case "replicated":
            count = replicas_of(view) or 0
            return f"{count} {'replica' if count == 1 else 'replicas'}"
        case _:
            return None
