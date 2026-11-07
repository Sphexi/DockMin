"""Container recreation: the confirmation and the audited recreate operation."""

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Final

import anyio

from app.auth.errors import SELF_PROTECTED_REASON, ProductError, ResourceNotFound, SelfProtected
from app.auth.principal import Principal
from app.db.repositories.activity_event import DetailValue
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError
from app.docker_engine.errors import NotFound as EngineNotFound
from app.docker_engine.models import ContainerInspect
from app.logging import get_logger
from app.registry_client.references import ImageReference, parse_reference
from app.services.access import ResourceKey, is_canonical_id
from app.services.acting import (
    Acting,
    AutomationAuthority,
    actor_of,
    holder_label,
    is_administrator,
)
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    AuditState,
    audited,
    current_audit,
    environment_argument,
)
from app.services.container_actions import (
    FORCEABLE_STATES,
    ContainerGone,
    ContainerStateConflict,
    engine_detail,
    retrieve_exception,
)
from app.services.container_derivation import (
    BLOCKER_MESSAGES,
    derive_recreate,
    image_is_id,
    recreate_blockers,
)
from app.services.container_provisioning import (
    MAX_VERIFY_SECONDS,
    ControlStep,
    Copy,
    Failure,
    NameChanged,
    NotRecreatable,
    NotRunning,
    ProvisionFeed,
    ProvisioningBase,
    RecreateFailed,
    RecreateRun,
    RestrictedOptions,
    RolledBack,
    Stage,
    StepKind,
    SubmissionRejected,
    Transfer,
    VerifyPolicy,
    failure_of,
    field_names,
    note_stage,
    settle_seconds,
    step_kind,
)
from app.services.container_restrictions import option_label, refused_options, require_no_self_reach
from app.services.container_review import (
    UNREADABLE_CHANGES,
    BlockerView,
    RecreateConfirmation,
    RecreateOutcome,
    RestrictedView,
    changed_categories,
    container_document,
    image_document,
)
from app.services.containers import display_name, short_id
from app.services.host_objects import settled
from app.services.image_bindings import observe_image
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.pull_credentials import (
    BindingCredentialMissing,
    CredentialChoice,
    CredentialChoiceRequired,
    CredentialSetMismatch,
    PullCredentials,
)
from app.services.registry_resolution import CredentialSetUnavailable

__all__ = ["INTERRUPTED_FAILURE", "PARKED_INFIX", "ContainerRecreateService"]

logger = get_logger(__name__)

PARKED_INFIX: Final = "-recreating-"

MAX_DISPLAYED_ID: Final = 64

MAX_VOLUMES_LISTED: Final = 50

HEALTH_WINDOW_MARGIN_SECONDS: Final = 5.0

DEFAULT_HEALTH_INTERVAL_SECONDS: Final = 30.0

DEFAULT_HEALTH_TIMEOUT_SECONDS: Final = 30.0

_NANOSECONDS: Final = 1_000_000_000

NOT_RECREATABLE_STATE: Final = "The container cannot be recreated in its current state."

RECREATE_REFUSED: Final = "Docker refused to recreate the container; nothing was changed."

RECREATE_RESTORED: Final = (
    "Docker could not recreate the container. The original container was restored."
)

RECREATE_IMAGE_MISSING: Final = (
    "The image is no longer on this host. The original container was restored."
)

RECREATE_RESOURCE_MISSING: Final = (
    "Something the container's configuration refers to is no longer on this host."
    " The original container was restored."
)

RECREATE_INTERRUPTED: Final = "The request was interrupted before the container was recreated."

VERIFICATION_RESTORED: Final = (
    "The new container did not pass verification. The original container was restored."
)

VERIFICATION_INTERRUPTED: Final = (
    "The request was interrupted while the new container was verified."
    " The original container was restored."
)

INTERRUPTED_FAILURE: Final = "interrupted"


@dataclass(frozen=True, slots=True)
class _Verdict:
    failure: str | None = None
    exit_code: int | None = None
    healthcheck: bool = False


@dataclass(frozen=True, slots=True)
class _RecreatePlan:
    actor: Acting
    environment_id: int
    old_id: str
    short: str
    name: str
    parked: str
    was_live: bool
    stop_wait: int | None
    body: dict[str, Any]
    image: str
    old_image_id: str
    old_document: Mapping[str, Any]
    client: EngineClient
    pull: bool
    credentials: PullCredentials
    holder: LockHolder
    reference: ImageReference | None = None
    verify: VerifyPolicy | None = None
    on_rolled_back: RolledBack | None = None


def _target_id(arguments: Arguments) -> str:
    return str(arguments["container_id"])[:MAX_DISPLAYED_ID]


def _rolled_back_call(
    plan: _RecreatePlan, error: BaseException
) -> Callable[[ActivityRecorder], Awaitable[None]] | None:
    if plan.on_rolled_back is None or not isinstance(error, RecreateFailed):
        return None
    if not error.rolled_back:
        return None
    return partial(plan.on_rolled_back, error)


def _healthcheck_of(config: Mapping[str, Any]) -> Mapping[str, Any] | None:
    check = config.get("Healthcheck")
    if not isinstance(check, Mapping):
        return None
    test = check.get("Test")
    if not isinstance(test, list) or not test or test[0] == "NONE":
        return None
    return check


def _health_seconds(check: Mapping[str, Any], name: str, default: float) -> float:
    value = check.get(name)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value / _NANOSECONDS
    return default


def _health_bound(check: Mapping[str, Any]) -> float:
    start = _health_seconds(check, "StartPeriod", 0.0)
    interval = _health_seconds(check, "Interval", DEFAULT_HEALTH_INTERVAL_SECONDS)
    timeout = _health_seconds(check, "Timeout", DEFAULT_HEALTH_TIMEOUT_SECONDS)
    return min(float(MAX_VERIFY_SECONDS), start + interval + timeout + HEALTH_WINDOW_MARGIN_SECONDS)


def _crash_verdict(found: ContainerInspect, baseline_restarts: int) -> _Verdict | None:
    state = found.state
    if state.dead or state.status in ("exited", "dead"):
        return _Verdict("exited", exit_code=state.exit_code)
    if state.restarting or state.status == "restarting":
        return _Verdict("restarting")
    if found.restart_count > baseline_restarts:
        return _Verdict("restarted")
    if not state.running or state.paused or state.status != "running":
        return _Verdict("not_running")
    return None


def _control_step(old: ResourceKey, new: ResourceKey, *, old_removed: bool) -> Transfer | Copy:
    return Transfer(old, new) if old_removed else Copy(old, new)


def _original_name(plan: _RecreatePlan, run: RecreateRun) -> str:
    return plan.parked if run.renamed else plan.name


def _names_image(error: EngineError) -> bool:
    return "image" in (error.engine_message or "").casefold()


class ContainerRecreateService(ProvisioningBase):
    """Recreate containers, with the restrictions enforced on the body."""

    async def recreate_confirmation(
        self, actor: Principal, environment_id: int, container_id: str
    ) -> RecreateConfirmation:
        """Describe what a recreate of the container would do; write holders only."""
        if not is_canonical_id("container", container_id):
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "container", container_id), "write"
        )
        if self._self_identity().protects_container(container_id):
            raise SelfProtected.container()
        environment = await self._hosts.environment(environment_id)
        inspected = await self._inspector.inspect(environment_id, container_id)
        client = await self._hosts.client(environment_id)
        document = await asyncio.to_thread(container_document, inspected)
        defaults = None if actor.is_administrator else await self._daemon_defaults(client)
        derivation = await asyncio.to_thread(
            partial(
                derive_recreate,
                document,
                image_inspect=image_document(await self._image_of(client, inspected.image)),
                defaults=defaults,
            )
        )
        require_no_self_reach(
            derivation.body, environment_id=environment_id, identity=self._self_identity()
        )
        restricted: tuple[RestrictedView, ...] = ()
        if not actor.is_administrator:
            findings, reach = await self._recreate_restrictions(
                actor, environment_id, client, derivation, defaults
            )
            restricted = tuple(
                RestrictedView(option, option_label(option))
                for option in refused_options([*findings, *reach.findings])
            )
        named = sorted(derivation.own_volumes - set(derivation.anonymous_volumes))
        networks = (
            tuple(sorted(inspected.network_settings.networks))
            if "NetworkingConfig" in derivation.body
            else ()
        )
        return RecreateConfirmation(
            environment_id=environment_id,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            container_id=inspected.id,
            short_id=short_id(inspected.id),
            name=display_name([inspected.name], inspected.id),
            image=derivation.image,
            state=inspected.state.status,
            was_running=inspected.state.status in FORCEABLE_STATES,
            named_volumes=tuple(named[:MAX_VOLUMES_LISTED]),
            named_volumes_hidden=max(0, len(named) - MAX_VOLUMES_LISTED),
            anonymous_volume_count=len(derivation.anonymous_volumes),
            networks=networks,
            blockers=tuple(
                BlockerView(blocker, BLOCKER_MESSAGES[blocker])
                for blocker in recreate_blockers(document, pull=False)
            ),
            restricted=restricted,
            pull_available=not image_is_id(derivation.image),
            image_defaults_kept=derivation.image_defaults_kept,
        )

    @audited(
        action="container_recreated",
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def recreate(
        self,
        actor: Acting,
        environment_id: int,
        container_id: str,
        *,
        expected_name: str,
        pull: bool = False,
        credential_choice: CredentialChoice | None = None,
        verify: VerifyPolicy | None = None,
        note: Mapping[str, DetailValue] | None = None,
        on_rolled_back: RolledBack | None = None,
        progress: ProvisionFeed | None = None,
    ) -> RecreateOutcome:
        """Replace a container with a new one built from its own configuration.

        Needs write access and the name the caller reviewed. The old container is removed only
        after the new one runs; an earlier failure restores it. Volumes, network attachments and
        the access control carry over. Raises NameChanged, NotRecreatable, RestrictedOptions
        (keyed by Engine path), ObjectBusy, ContainerGone, PullFailed, RecreateFailed or
        EnvironmentUnavailable. With `pull`, `credential_choice` picks the credential set and
        CredentialChoiceRequired is raised when several fit and none was chosen; the pulled
        reference is bound to the set used in the transaction that moves the control.

        An automation run passes its authority: it never pulls or chooses a credential set, and
        it may pass `verify`, which watches the new container before the old one is removed and
        restores the old one when the new one fails; with `verify`, NotRunning is raised and no
        row is written unless the container is running. `note` is merged into the row's detail.
        `on_rolled_back`, which needs `verify`, is called at most once with the failure and a
        fresh recorder when an interrupted request's sequence ends rolled back, after the
        interrupted row; its own failure is logged and dropped.

        With `progress`, the pull reports its layers and the steps to it, and a cancel during the
        pull stops the pull and records the failure `cancelled`, or `interrupted` when the process
        is closing. Once the pull has finished a cancel the starter asked for changes nothing.
        """
        audit = current_audit()
        if isinstance(actor, AutomationAuthority):
            if pull or credential_choice is not None:
                raise ValueError("an automated recreate does not pull or choose a credential set")
        elif verify is not None:
            raise ValueError("only an automated recreate verifies the new container")
        if on_rolled_back is not None and (
            not isinstance(actor, AutomationAuthority) or verify is None
        ):
            raise ValueError("a rollback callback needs an automated recreate that verifies")
        audit.detail.update(note or {})
        await self._admitted(actor, environment_id, container_id)
        old_key = ResourceKey(environment_id, "container", container_id)
        holder = LockHolder(actor_of(actor), holder_label(actor), "recreate")
        async with (
            AsyncExitStack() as held,
            self._held(actor, environment_id, container_id, holder),
        ):
            plan = await self._recorded_plan(
                actor,
                environment_id,
                container_id,
                expected_name,
                pull,
                holder,
                credential_choice,
                held,
                verify,
            )
            plan = replace(plan, on_rolled_back=on_rolled_back)
            run = RecreateRun(feed=progress)
            task: asyncio.Future[None] = asyncio.ensure_future(self._recreate_sequence(plan, run))
            task.add_done_callback(retrieve_exception)
            try:
                await self._sequence_result(task, run)
            except asyncio.CancelledError:
                run.abandoned = True
                if run.verifying:
                    run.interrupt.set()
                await self._interrupted_recreate(plan, task, run)
                raise
            new_id = run.new_id
            if new_id is None:
                raise RuntimeError("the recreate sequence ended without a container")
            new_key = ResourceKey(environment_id, "container", new_id)
            step = _control_step(old_key, new_key, old_removed=run.old_removed)
            bind = (
                partial(self._bind, actor, environment_id, plan.reference, plan.credentials, run)
                if run.pulled and plan.reference is not None and isinstance(actor, Principal)
                else None
            )
            try:
                control_moved, old_control_kept = await self._carry_control(step, new_key, bind)
            except asyncio.CancelledError:
                await self._write_interrupted(
                    actor,
                    action="container_recreated",
                    environment_id=environment_id,
                    target_id=container_id,
                    detail={**audit.detail, "interrupted": True},
                    failure=None,
                    control=step,
                )
                raise
        self._inspector.forget(environment_id, container_id)
        return RecreateOutcome(
            environment_id=environment_id,
            old_id=container_id,
            new_id=new_id,
            name=plan.name,
            image=plan.image,
            pulled=run.pulled,
            image_changed=run.image_changed,
            changed=run.changed,
            started=run.started,
            old_removed=run.old_removed,
            remove_outcome_unknown=run.remove_outcome_unknown,
            kept_as=None if run.old_removed else _original_name(plan, run),
            control_moved=control_moved,
            old_control_kept=old_control_kept,
            verified=run.verified,
        )

    @audited(
        action="container_recreated",
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def prepare_recreate(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        *,
        expected_name: str,
        credential_choice: CredentialChoice | None = None,
    ) -> None:
        """Check a recreate that pulls as recreate does, without changing anything.

        Raises what recreate raises before it acts, and records the failure recreate would
        have recorded; a recreate that would go ahead records nothing.
        """
        await self._admitted(actor, environment_id, container_id)
        holder = LockHolder(actor_of(actor), holder_label(actor), "recreate")
        async with (
            AsyncExitStack() as held,
            self._held(actor, environment_id, container_id, holder),
        ):
            await self._recorded_plan(
                actor,
                environment_id,
                container_id,
                expected_name,
                True,
                holder,
                credential_choice,
                held,
                None,
            )
        current_audit().unchanged()

    async def _admitted(self, actor: Acting, environment_id: int, container_id: str) -> None:
        audit = current_audit()
        if not is_canonical_id("container", container_id):
            raise ResourceNotFound("not found")
        old_key = ResourceKey(environment_id, "container", container_id)
        try:
            await self._require_object(actor, old_key, "write")
        except (EnvironmentUnavailable, ObjectTooLarge):
            audit.refuse()
            raise
        if self._self_identity().protects_container(container_id):
            audit.reason = SELF_PROTECTED_REASON
            raise SelfProtected.container()

    async def _recorded_plan(
        self,
        actor: Acting,
        environment_id: int,
        container_id: str,
        expected_name: str,
        pull: bool,
        holder: LockHolder,
        choice: CredentialChoice | None,
        held: AsyncExitStack,
        verify: VerifyPolicy | None,
    ) -> _RecreatePlan:
        audit = current_audit()
        try:
            return await self._plan_recreate(
                actor,
                environment_id,
                container_id,
                expected_name,
                pull,
                holder,
                choice,
                held,
                verify,
            )
        except SubmissionRejected as rejected:
            audit.reason = rejected.reason
            audit.detail.update(rejected.detail)
            raise
        except EnvironmentUnavailable as error:
            audit.reason = error.kind
            raise
        except ObjectTooLarge:
            audit.reason = "too_large"
            raise
        except (CredentialChoiceRequired, CredentialSetMismatch):
            audit.reason = "credential_choice_required"
            raise
        except BindingCredentialMissing:
            audit.reason = "credential_missing"
            raise
        except CredentialSetUnavailable:
            audit.reason = "credential_unavailable"
            raise

    async def _plan_recreate(
        self,
        actor: Acting,
        environment_id: int,
        container_id: str,
        expected_name: str,
        pull: bool,
        holder: LockHolder,
        choice: CredentialChoice | None,
        held: AsyncExitStack,
        verify: VerifyPolicy | None,
    ) -> _RecreatePlan:
        audit = current_audit()
        client = await self._hosts.client(environment_id)
        self._inspector.forget(environment_id, container_id)
        try:
            inspected = await self._inspector.inspect(environment_id, container_id)
        except ResourceNotFound:
            audit.reason = "not_found"
            raise ContainerGone() from None
        name = display_name([inspected.name], inspected.id)
        audit.detail["name"] = name
        if expected_name != name:
            audit.reason = "name_changed"
            raise NameChanged()
        if verify is not None and inspected.state.status != "running":
            audit.reason = "not_running"
            audit.refuse()
            raise NotRunning()
        document = await asyncio.to_thread(container_document, inspected)
        blockers = recreate_blockers(document, pull=pull)
        if blockers:
            audit.reason = "not_recreatable"
            audit.detail["blockers"] = ",".join(blockers)
            raise NotRecreatable(blockers)
        administrator = is_administrator(actor)
        defaults = None if administrator else await self._daemon_defaults(client)
        derivation = await asyncio.to_thread(
            partial(
                derive_recreate,
                document,
                image_inspect=image_document(await self._image_of(client, inspected.image)),
                defaults=defaults,
            )
        )
        audit.detail["image"] = derivation.image
        if derivation.image_defaults_kept:
            audit.detail["image_defaults_kept"] = True
        findings, reach = await self._recreate_restrictions(
            actor, environment_id, client, derivation, defaults
        )
        if administrator:
            if findings:
                audit.detail["restricted"] = ",".join(refused_options(findings))
        else:
            findings = [*findings, *reach.findings]
            if findings:
                fields = {finding.path: finding.message for finding in reversed(findings)}
                fields.update(reach.errors)
                raise RestrictedOptions(findings, fields, state=None)
            if reach.errors:
                raise SubmissionRejected(
                    reach.errors,
                    state=None,
                    reason="invalid",
                    detail={"fields": field_names(reach.errors)},
                )
        try:
            require_no_self_reach(
                derivation.body, environment_id=environment_id, identity=self._self_identity()
            )
        except SelfProtected:
            audit.reason = SELF_PROTECTED_REASON
            raise
        user = actor if isinstance(actor, Principal) else None
        reference = parse_reference(derivation.image) if pull and user is not None else None
        if reference is not None and user is not None:
            await self._hold_reference(held, user, environment_id, reference, "recreate")
        credentials = (
            await self._credentials.resolve(
                environment_id, derivation.image, principal=user, choice=choice
            )
            if pull and user is not None
            else PullCredentials()
        )
        audit.detail.update(
            pulled=False,
            credential_set_id=credentials.credential_set_id,
            started=False,
            old_removed=False,
        )
        if pull:
            audit.detail.update(
                anonymous=credentials.credential_set_id is None,
                credential_source=credentials.source,
            )
        short = short_id(inspected.id)
        return _RecreatePlan(
            actor=actor,
            environment_id=environment_id,
            old_id=container_id,
            short=short,
            name=name,
            parked=f"{name}{PARKED_INFIX}{short}",
            was_live=inspected.state.status in FORCEABLE_STATES,
            stop_wait=inspected.config.stop_timeout,
            body=derivation.body,
            image=derivation.image,
            old_image_id=inspected.image,
            old_document=document,
            client=client,
            pull=pull,
            credentials=credentials,
            holder=holder,
            reference=reference,
            verify=verify,
        )

    async def _recreate_sequence(self, plan: _RecreatePlan, run: RecreateRun) -> None:
        audit = current_audit()
        client = plan.client
        if run.abandoned:
            return
        if plan.pull:
            await self._pull(
                client, plan.environment_id, plan.image, plan.credentials.registry_auth, run
            )
            run.pulled = True
            audit.detail["pulled"] = True
            if plan.reference is not None:
                run.digest = (await observe_image(client, plan.reference))[1]
            if run.abandoned:
                return
        if plan.was_live:
            note_stage(run, "stop")
            try:
                await client.containers.stop(plan.old_id, daemon_wait_seconds=plan.stop_wait)
            except EngineError as error:
                raise await self._failed(plan, run, "stop", error) from None
            run.stopped = True
        note_stage(run, "rename")
        try:
            await client.containers.rename(plan.old_id, plan.parked)
        except EngineError as error:
            raise await self._failed(plan, run, "rename", error) from None
        run.renamed = True
        async with AsyncExitStack() as held:
            note_stage(run, "create")
            try:
                new_id = await client.containers.create(plan.body, name=plan.name)
            except EngineError as error:
                raise await self._failed(plan, run, "create", error) from None
            run.new_id = new_id
            audit.detail["new_container_id"] = new_id
            try:
                await held.enter_async_context(
                    self._locks.hold(
                        ObjectKey(plan.environment_id, "container", new_id), plan.holder
                    )
                )
            except ObjectBusy:
                logger.warning(
                    "recreated_container_lock_busy",
                    extra={"environment_id": plan.environment_id, "container_id": new_id},
                )
            if plan.was_live:
                note_stage(run, "start")
                try:
                    await client.containers.start(new_id)
                except EngineError as error:
                    raise await self._failed(plan, run, "start", error) from None
                run.started = True
                audit.detail["started"] = True
                run.verifying = True
                try:
                    await self._verify(plan, run, new_id)
                finally:
                    run.verifying = False
            try:
                await client.containers.remove(plan.old_id, force=False, volumes=False)
            except EngineError as error:
                logger.warning(
                    "recreated_container_old_not_removed",
                    extra={
                        "environment_id": plan.environment_id,
                        "container_id": plan.old_id,
                        "error": type(error).__name__,
                        "status": error.status_code,
                    },
                )
                audit.detail["kept_as"] = _original_name(plan, run)
                if step_kind(error) == "unknown":
                    run.remove_outcome_unknown = True
                    audit.detail["remove_outcome_unknown"] = True
            else:
                run.old_removed = True
            audit.detail["old_removed"] = run.old_removed
            await self._describe_change(plan, run, new_id)

    async def _verify(self, plan: _RecreatePlan, run: RecreateRun, new_id: str) -> None:
        policy = plan.verify
        if policy is None:
            return
        audit = current_audit()
        note_stage(run, "verify")
        verdict = await self._watch(plan.client, new_id, policy, run)
        audit.detail.update(verify_seconds=policy.seconds, healthcheck=verdict.healthcheck)
        if verdict.failure is None:
            run.verified = True
            audit.detail["verified"] = True
            return
        run.verified = False
        audit.detail.update(verified=False, verify_failure=verdict.failure)
        if verdict.exit_code is not None:
            audit.detail["exit_code"] = verdict.exit_code
        raise await self._verification_failed(plan, run, verdict.failure)

    async def _watch(
        self, client: EngineClient, new_id: str, policy: VerifyPolicy, run: RecreateRun
    ) -> _Verdict:
        baseline = await self._look(client, new_id)
        if isinstance(baseline, _Verdict):
            return baseline
        check = _healthcheck_of(baseline.config_document)
        has_check = check is not None
        watch_health = policy.require_healthy and check is not None
        base_polls = math.ceil(policy.seconds / policy.interval)
        total_polls = base_polls
        if check is not None and watch_health:
            total_polls = max(base_polls, math.ceil(_health_bound(check) / policy.interval))
        current = baseline
        polls = 0
        while True:
            crashed = _crash_verdict(current, baseline.restart_count)
            if crashed is not None:
                return replace(crashed, healthcheck=has_check)
            health = current.state.health
            status = "" if health is None else health.status
            run.observed_failure = "unhealthy" if watch_health and status == "unhealthy" else None
            if run.interrupt.is_set():
                return _Verdict(run.observed_failure or INTERRUPTED_FAILURE, healthcheck=has_check)
            if polls >= base_polls:
                if not watch_health:
                    return _Verdict(healthcheck=has_check)
                if status == "healthy":
                    return _Verdict(healthcheck=True)
                if status == "unhealthy":
                    return _Verdict("unhealthy", healthcheck=True)
                if polls >= total_polls:
                    return _Verdict("not_healthy", healthcheck=True)
            await self._pause(policy.interval, run.interrupt)
            polls += 1
            found = await self._look(client, new_id)
            if isinstance(found, _Verdict):
                return replace(found, healthcheck=has_check)
            current = found

    async def _pause(self, seconds: float, interrupt: asyncio.Event) -> None:
        """Sleep for `seconds`, or until `interrupt` is set."""
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        stopper = asyncio.ensure_future(interrupt.wait())
        try:
            done, _ = await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            stopper.cancel()
            await asyncio.gather(sleeper, stopper, return_exceptions=True)
        if sleeper in done:
            sleeper.result()

    async def _look(self, client: EngineClient, container_id: str) -> ContainerInspect | _Verdict:
        try:
            return await client.containers.inspect(container_id)
        except EngineNotFound:
            return _Verdict("gone")
        except EngineError:
            return _Verdict("unverifiable")

    async def _verification_failed(
        self, plan: _RecreatePlan, run: RecreateRun, failure: str
    ) -> ProductError:
        audit = current_audit()
        failed_id = run.new_id
        logger.warning(
            "container_recreate_verification_failed",
            extra={
                "environment_id": plan.environment_id,
                "container_id": plan.old_id,
                "failure": failure,
            },
        )
        audit.reason = "verification_failed"
        if await self._rollback(plan, run):
            audit.detail["rolled_back"] = True
            return RecreateFailed(
                VERIFICATION_INTERRUPTED
                if failure == INTERRUPTED_FAILURE
                else VERIFICATION_RESTORED,
                rolled_back=True,
                stage="verify",
                failed_container_id=failed_id,
                verify_failure=failure,
            )
        kept = _original_name(plan, run)
        audit.detail["rollback_failed"] = True
        audit.detail["kept_as"] = kept
        return RecreateFailed(
            "The new container did not pass verification and the original could not be restored."
            f" The original is kept as {kept}.",
            stage="verify",
            failed_container_id=failed_id,
            verify_failure=failure,
        )

    async def _describe_change(self, plan: _RecreatePlan, run: RecreateRun, new_id: str) -> None:
        audit = current_audit()
        try:
            created = await plan.client.containers.inspect(new_id)
        except EngineError:
            run.changed = (UNREADABLE_CHANGES,)
            audit.detail["changed"] = UNREADABLE_CHANGES
            audit.detail["old_image_id"] = plan.old_image_id
            return
        document = await asyncio.to_thread(container_document, created)
        run.changed = await asyncio.to_thread(changed_categories, plan.old_document, document)
        run.image_changed = created.image != plan.old_image_id
        audit.detail.update(
            changed=",".join(run.changed),
            image_changed=run.image_changed,
            old_image_id=plan.old_image_id,
            new_image_id=created.image,
        )

    async def _failed(
        self, plan: _RecreatePlan, run: RecreateRun, stage: Stage, error: EngineError
    ) -> ProductError:
        audit = current_audit()
        kind = step_kind(error)
        logger.warning(
            "container_recreate_step_failed",
            extra={
                "environment_id": plan.environment_id,
                "container_id": plan.old_id,
                "stage": stage,
                "error": type(error).__name__,
                "status": error.status_code,
            },
        )
        if kind == "unknown":
            audit.reason = "outcome_unknown"
            note = ""
            if run.renamed:
                audit.detail["kept_as"] = plan.parked
                note = f" The original is kept as {plan.parked}."
            elif stage == "rename":
                note = f" The original may have been renamed to {plan.parked}."
            message = (
                f"The Docker host did not answer in time, so the state of {plan.name} is unknown."
                + note
            )
            return RecreateFailed(
                message, outcome_unknown=True, stage=stage, failed_container_id=run.new_id
            )
        if stage == "stop" or (stage == "rename" and kind == "not_found"):
            return self._unchanged(plan, kind, error)
        failed_id = run.new_id
        detail = engine_detail(error)
        rolled_back = await self._rollback(plan, run)
        missing = stage == "create" and kind == "not_found"
        if missing:
            audit.reason = "image_missing" if _names_image(error) else "reference_missing"
        elif kind in ("unreachable", "conflict"):
            audit.reason = kind
        else:
            audit.reason = "engine_rejected"
        if rolled_back:
            audit.detail["rolled_back"] = True
            if kind == "unreachable":
                return EnvironmentUnavailable("unreachable")
            if missing:
                message = (
                    RECREATE_IMAGE_MISSING if _names_image(error) else RECREATE_RESOURCE_MISSING
                )
                return RecreateFailed(
                    message,
                    rolled_back=True,
                    stage=stage,
                    failed_container_id=failed_id,
                    engine_detail=detail,
                )
            return RecreateFailed(
                RECREATE_RESTORED,
                rolled_back=True,
                stage=stage,
                failed_container_id=failed_id,
                engine_detail=detail,
            )
        kept = _original_name(plan, run)
        audit.detail["rollback_failed"] = True
        audit.detail["kept_as"] = kept
        if run.new_id is not None:
            audit.detail["new_container_id"] = run.new_id
        return RecreateFailed(
            "Docker could not recreate the container and the original could not be restored."
            f" The original is kept as {kept}.",
            stage=stage,
            failed_container_id=failed_id,
            engine_detail=detail,
        )

    def _unchanged(self, plan: _RecreatePlan, kind: StepKind, error: EngineError) -> ProductError:
        audit = current_audit()
        match kind:
            case "not_found":
                audit.reason = "not_found"
                return ContainerGone()
            case "unreachable":
                audit.reason = "unreachable"
                return EnvironmentUnavailable("unreachable")
            case "conflict":
                audit.reason = "conflict"
                return ContainerStateConflict(NOT_RECREATABLE_STATE)
            case _:
                audit.reason = "engine_rejected"
                return RecreateFailed(RECREATE_REFUSED, engine_detail=engine_detail(error))

    async def _rollback(self, plan: _RecreatePlan, run: RecreateRun) -> bool:
        client = plan.client
        note_stage(run, "rollback")
        try:
            if run.new_id is not None:
                with suppress(EngineNotFound):
                    await client.containers.remove(run.new_id, force=True, volumes=False)
                run.new_id = None
            if run.renamed:
                await client.containers.rename(plan.old_id, plan.name)
                run.renamed = False
            if run.stopped:
                await client.containers.start(plan.old_id)
                run.stopped = False
        except Exception as error:
            logger.error(
                "container_recreate_rollback_failed",
                extra={
                    "environment_id": plan.environment_id,
                    "container_id": plan.old_id,
                    "error": type(error).__name__,
                },
            )
            return False
        return True

    async def _interrupted_recreate(
        self, plan: _RecreatePlan, task: asyncio.Future[None], run: RecreateRun
    ) -> None:
        audit = current_audit()
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, settle_seconds(plan.stop_wait, plan.verify))
            detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
            failure: Failure | None = None
            control: ControlStep | None = None
            then: Callable[[ActivityRecorder], Awaitable[None]] | None = None
            if finished and error is None and run.new_id is not None:
                control = self._late_control(plan, run, run.new_id)
            elif not finished:
                failure = Failure("outcome_unknown", str(EnvironmentUnavailable("outcome_unknown")))
                task.add_done_callback(partial(self._settle_late_recreate, plan, run, audit))
            elif isinstance(error, asyncio.CancelledError):
                failure = self._pull_stopped(run, "recreated")
            elif error is not None:
                failure = failure_of(error, audit.reason)
                then = _rolled_back_call(plan, error)
            elif run.pulled:
                failure = Failure("interrupted", RECREATE_INTERRUPTED)
            if failure is not None or control is not None:
                await self._write_interrupted(
                    plan.actor,
                    action="container_recreated",
                    environment_id=plan.environment_id,
                    target_id=plan.old_id,
                    detail=detail,
                    failure=failure,
                    control=control,
                    then=then,
                )
        self._inspector.forget(plan.environment_id, plan.old_id)

    def _late_control(self, plan: _RecreatePlan, run: RecreateRun, new_id: str) -> ControlStep:
        return _control_step(
            ResourceKey(plan.environment_id, "container", plan.old_id),
            ResourceKey(plan.environment_id, "container", new_id),
            old_removed=run.old_removed,
        )

    def _settle_late_recreate(
        self,
        plan: _RecreatePlan,
        run: RecreateRun,
        audit: AuditState,
        task: asyncio.Future[None],
    ) -> None:
        if task.cancelled():
            return
        error = task.exception()
        detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True, "settled_late": True}
        if error is None and run.new_id is not None:
            self._spawn(
                self._write_interrupted(
                    plan.actor,
                    action="container_recreated",
                    environment_id=plan.environment_id,
                    target_id=plan.old_id,
                    detail=detail,
                    failure=None,
                    control=self._late_control(plan, run, run.new_id),
                )
            )
        elif isinstance(error, RecreateFailed):
            error.settled_late = True
            self._spawn(
                self._write_interrupted(
                    plan.actor,
                    action="container_recreated",
                    environment_id=plan.environment_id,
                    target_id=plan.old_id,
                    detail=detail,
                    failure=failure_of(error, audit.reason),
                    control=None,
                    then=_rolled_back_call(plan, error),
                )
            )
        self._inspector.forget(plan.environment_id, plan.old_id)
