"""The audited Swarm service operations."""

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from typing import Any, Final, Literal

import anyio
from pydantic import SecretStr

from app.auth.errors import (
    SELF_PROTECTED_REASON,
    ProductError,
    ResourceNotFound,
    SelfProtected,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.db.engine import Database, Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.db.repositories.resource_control import ResourceControlRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import (
    Conflict,
    EngineError,
    NotFound,
)
from app.docker_engine.models import SwarmService, SwarmTask
from app.logging import get_logger
from app.scheduler.model import RunSkipped
from app.self_identity import SelfIdentitySource, no_identity
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
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.container_actions import BusyView, EngineRefused, engine_detail
from app.services.container_restrictions import refused_options
from app.services.containers import (
    stack_name,
)
from app.services.host_objects import (
    SettleStep,
    audited_client,
    busy_view,
    failure_reason,
    hold_all,
    in_transaction,
    run_shielded,
    settled,
    write_settled_row,
)
from app.services.image_bindings import ImageBindingService, parse_pull_reference
from app.services.object_inspector import (
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.pull_credentials import (
    CredentialChoice,
    PullCredentialResolver,
    PullCredentials,
)
from app.services.resource_controls import ResourceControlService
from app.services.service_restrictions import (
    find_restricted_service,
    pinned_to,
    references,
)
from app.services.service_spec import (
    changed_categories,
    force_counter,
    forced,
    image_of,
    parse_service_form,
    replicas_of,
    scaled,
)
from app.services.swarm import (
    SETTLE_SECONDS,
    audited_swarm_read,
    engine_failure,
    swarm_read,
)
from app.services.swarm_common import (
    PinnedImage,
    mapping_of,
    pinned_text,
    split_service_image,
)
from app.services.swarm_gate import require_swarm
from app.services.swarm_service_base import (
    MAX_NAME,
    CreateRun,
    SwarmServiceBase,
    as_list,
    binding_actor,
    clip,
    guarded,
    migrate_legacy_networks,
    mode_text,
    reference_key,
    require_binding,
    rollout_bound,
    service_argument,
    service_status,
    state_name,
)
from app.services.swarm_service_queries import check_mode, parse_replicas_for
from app.services.swarm_service_views import (
    NoPreviousSpec,
    NotReplicated,
    Plan,
    RestrictedService,
    ServiceChangedSinceReview,
    ServiceCreated,
    ServiceCredentialsRefreshed,
    ServiceForceUpdated,
    ServiceGone,
    ServiceImageUpdated,
    ServiceInvalid,
    ServiceNameInUse,
    ServiceRefused,
    ServiceRemoved,
    ServiceRolledBack,
    ServiceScaled,
    ServiceUpdated,
    ServiceUpdateFailed,
    ServiceVerifyPolicy,
    Verdict,
)

logger = get_logger(__name__)

__all__ = [
    "SwarmServiceActions",
]

UNPINNED_WARNING: Final = "Nodes may run different images of this tag."


ROLLOUT_IN_FLIGHT: Final = frozenset({"updating", "paused", "rollback_started", "rollback_paused"})


MAX_VERIFY_POLLS: Final = 600


_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")


class SwarmServiceActions(SwarmServiceBase):
    """Creates, updates, scales, rolls back and removes services, each one audited operation."""

    def __init__(
        self,
        *,
        db: Session,
        database: Database,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        locks: ObjectLockRegistry,
        controls: ResourceControlService,
        pull: PullCredentialResolver,
        bindings: ImageBindingService | None,
        activity: ActivityRecorder,
        clock: Clock,
        poke: Callable[[int], None],
        bindings_factory: Callable[[Session, ActivityRecorder], ImageBindingService] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        self_identity: SelfIdentitySource | None = None,
    ) -> None:
        self._self_identity = self_identity or no_identity
        self._db = db
        self._database = database
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._locks = locks
        self._controls = controls
        self._pull = pull
        self._bindings = bindings
        self._bindings_factory = bindings_factory
        self._activity = activity
        self._clock = clock
        self._poke = poke
        self._sleep = sleep
        self._background: set[asyncio.Future[None]] = set()

    def describe_busy(self, busy: ObjectBusy, viewer: Principal) -> BusyView:
        """Describe the lock holder to a viewer: names only for administrators."""
        return busy_view(busy, viewer, self._locks.now())

    @audited(action="service_created", target_type="service", environment_id=environment_argument)
    async def create(
        self,
        actor: Principal,
        environment_id: int,
        form: Mapping[str, str],
        *,
        credential_choice: CredentialChoice | None = None,
    ) -> ServiceCreated:
        """Create a service from a submitted form; the caller owns it afterwards.

        Registry credentials are chosen, and the image pinned to its digest, before anything is
        sent; a failed digest lookup creates the service unpinned and says so. A standard user's
        spec passes the restriction set and the reach rule first. ServiceInvalid or
        RestrictedService with fields keyed by form field name, CredentialChoiceRequired,
        ServiceNameInUse, ServiceRefused, EnvironmentUnavailable.
        """
        audit = current_audit()
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        await self._authoriser.require_create(actor, environment_id)
        client = await audited_client(self._hosts, environment_id)
        with guarded():
            form_input = parse_service_form(
                form, creating=True, administrator=actor.is_administrator
            )
            plan = await self._assemble(
                actor,
                environment_id,
                client,
                form_input,
                current=None,
                choice=credential_choice,
                credentials=True,
            )
        self._describe_create(actor, plan)
        run = CreateRun()
        auth = None if plan.pinned is None else plan.pinned.credentials.registry_auth
        try:
            service_id = await run_shielded(
                partial(self._create_on_host, client, plan.spec, auth, run),
                partial(self._interrupted_create, actor, environment_id, run, plan),
            )
        except EngineError as error:
            raise await self._create_failure(error, environment_id, plan) from None
        audit.target_id = service_id
        created = await self._settle_create(actor, environment_id, service_id, plan)
        pinned = plan.pinned
        return ServiceCreated(
            environment_id=environment_id,
            service_id=service_id,
            name=form_input.name,
            digest_pinned=pinned is not None and pinned.pinned,
            warning=UNPINNED_WARNING if pinned is not None and not pinned.pinned else None,
            control_created=created,
        )

    @audited(
        action="service_updated",
        target_type="service",
        target_id=service_argument,
        environment_id=environment_argument,
    )
    async def update(
        self,
        actor: Principal,
        environment_id: int,
        service_id: str,
        form: Mapping[str, str],
        *,
        expected_version: int,
        credential_choice: CredentialChoice | None = None,
    ) -> ServiceUpdated:
        """Apply an edit the caller reviewed, to exactly the version they reviewed.

        Only the fields the form manages change; every other field of the stored spec is sent
        back as it was. ServiceChangedSinceReview when the version differs, ServiceGone,
        ObjectBusy, ServiceInvalid, RestrictedService, CredentialChoiceRequired.
        """
        audit = current_audit()
        client = await self._authorise_write(actor, environment_id, service_id)
        async with self._hold(actor, environment_id, service_id, "update"):
            document = await self._fresh(environment_id, service_id)
            name = str(document.spec.get("Name") or "")[:MAX_NAME]
            audit.detail.update(name=name, version_before=document.version)
            if document.version != expected_version:
                audit.reason = "changed_since_review"
                raise ServiceChangedSinceReview()
            current = migrate_legacy_networks(document.spec)
            with guarded():
                parsed = parse_service_form(
                    form, creating=False, administrator=actor.is_administrator, current=current
                )
                check_mode(parsed, current)
                plan = await self._assemble(
                    actor,
                    environment_id,
                    client,
                    parsed,
                    current=current,
                    choice=credential_choice,
                    credentials=True,
                )
            categories = changed_categories(current, plan.spec)
            pinned = plan.pinned
            audit.detail.update(
                changed=",".join(categories),
                image_changed=plan.image_changed,
                digest_pinned=pinned is not None and pinned.pinned,
            )
            self._describe_credentials(plan)
            if actor.is_administrator and plan.findings:
                audit.detail["restricted"] = ",".join(refused_options(plan.findings))
            warning = UNPINNED_WARNING if pinned is not None and not pinned.pinned else None
            if not categories:
                audit.unchanged()
                return ServiceUpdated(
                    environment_id,
                    service_id,
                    name,
                    categories,
                    pinned is not None and pinned.pinned,
                    warning,
                    True,
                )
            pulled_image = plan.image_changed and pinned is not None
            await self._send(
                actor,
                client,
                environment_id,
                service_id,
                plan.spec,
                version=document.version,
                action="service_updated",
                auth=pinned.credentials.registry_auth if plan.image_changed and pinned else None,
                auth_from=None if plan.image_changed else "spec",
                bind=pinned if pulled_image else None,
            )
            if pulled_image:
                await self._bind_after_send(actor, environment_id, plan.pinned)
        return ServiceUpdated(
            environment_id,
            service_id,
            name,
            categories,
            pinned is not None and pinned.pinned,
            warning,
            False,
        )

    @audited(
        action="service_scaled",
        target_type="service",
        target_id=service_argument,
        environment_id=environment_argument,
    )
    async def scale(
        self,
        actor: Principal,
        environment_id: int,
        service_id: str,
        *,
        replicas: int,
        expected_version: int,
        expected_replicas: int,
    ) -> ServiceScaled:
        """Set the replica count of a replicated service the caller reviewed.

        The row records the counts before and after. NotReplicated for another mode,
        ServiceChangedSinceReview when the version or the count differs from the review.
        """
        audit = current_audit()
        client = await self._authorise_write(actor, environment_id, service_id)
        try:
            parse_replicas_for(actor, replicas)
        except ValidationFailed:
            audit.reason = "invalid"
            raise
        async with self._hold(actor, environment_id, service_id, "scale"):
            document = await self._fresh(environment_id, service_id)
            name = str(document.spec.get("Name") or "")[:MAX_NAME]
            current = migrate_legacy_networks(document.spec)
            before = replicas_of(current)
            audit.detail.update(name=name, replicas_after=replicas)
            if before is None:
                audit.reason = "not_replicated"
                raise NotReplicated()
            audit.detail["replicas_before"] = before
            if document.version != expected_version or before != expected_replicas:
                audit.reason = "changed_since_review"
                raise ServiceChangedSinceReview()
            if before == replicas:
                audit.unchanged()
                return ServiceScaled(environment_id, service_id, name, before, replicas)
            await self._send(
                actor,
                client,
                environment_id,
                service_id,
                scaled(current, replicas),
                version=document.version,
                action="service_scaled",
                auth_from="spec",
            )
        return ServiceScaled(environment_id, service_id, name, before, replicas)

    @audited(
        action="service_force_updated",
        target_type="service",
        target_id=service_argument,
        environment_id=environment_argument,
    )
    async def force_update(
        self,
        actor: Acting,
        environment_id: int,
        service_id: str,
        *,
        expected_version: int,
        note: Mapping[str, DetailValue] | None = None,
    ) -> ServiceForceUpdated:
        """Replace every running task of the service without changing its configuration.

        Only an automation run passes a `note`; ValueError otherwise.
        """
        audit = current_audit()
        if note is not None and not isinstance(actor, AutomationAuthority):
            raise ValueError("only an automation run attaches a note")
        client = await self._authorise_write(actor, environment_id, service_id)
        async with self._hold(actor, environment_id, service_id, "force_update"):
            document = await self._fresh(environment_id, service_id)
            name = str(document.spec.get("Name") or "")[:MAX_NAME]
            current = migrate_legacy_networks(document.spec)
            audit.detail.update(note or {})
            audit.detail.update(name=name, force_update_before=force_counter(current))
            if document.version != expected_version:
                audit.reason = "changed_since_review"
                raise ServiceChangedSinceReview()
            running = len(await self._running(client, environment_id, service_id))
            spec = forced(current)
            audit.detail.update(
                force_update_after=force_counter(spec),
                tasks_running=running,
            )
            await self._send(
                actor,
                client,
                environment_id,
                service_id,
                spec,
                version=document.version,
                action="service_force_updated",
                auth_from="spec",
            )
        return ServiceForceUpdated(environment_id, service_id, name, running)

    @audited(
        action="service_rolled_back",
        target_type="service",
        target_id=service_argument,
        environment_id=environment_argument,
    )
    async def rollback(
        self,
        actor: Acting,
        environment_id: int,
        service_id: str,
        *,
        expected_version: int,
        note: Mapping[str, DetailValue] | None = None,
        expected_image: str | None = None,
        expected_previous_image: str | None = None,
    ) -> ServiceRolledBack:
        """Return the service to its previous configuration, as the caller reviewed it.

        The restriction walker and the reach rule run on the previous spec unless the caller is
        an administrator. NoPreviousSpec when there is none. Only an automation run passes a
        `note`, and it must pass both image texts: the spec image now and the one it replaces,
        either differing is ServiceChangedSinceReview. ValueError otherwise.
        """
        audit = current_audit()
        automated = isinstance(actor, AutomationAuthority)
        if note is not None and not automated:
            raise ValueError("only an automation run attaches a note")
        named = expected_image is not None and expected_previous_image is not None
        if automated and not named:
            raise ValueError("an automated rollback names the images it undoes")
        if not automated and (expected_image is not None or expected_previous_image is not None):
            raise ValueError("only an automation run names the images a rollback undoes")
        client = await self._authorise_write(actor, environment_id, service_id)
        async with self._hold(actor, environment_id, service_id, "rollback"):
            document = await self._fresh(environment_id, service_id)
            name = str(document.spec.get("Name") or "")[:MAX_NAME]
            audit.detail.update(note or {})
            audit.detail.update(name=name, version_before=document.version)
            if document.version != expected_version:
                audit.reason = "changed_since_review"
                raise ServiceChangedSinceReview()
            if document.previous_spec is None:
                audit.reason = "no_previous_spec"
                raise NoPreviousSpec()
            current = migrate_legacy_networks(document.spec)
            previous = migrate_legacy_networks(document.previous_spec)
            if automated and (
                image_of(current) != expected_image or image_of(previous) != expected_previous_image
            ):
                audit.reason = "changed_since_review"
                raise ServiceChangedSinceReview()
            with guarded():
                findings = await self._rollback_checks(
                    actor, environment_id, client, current, previous
                )
            categories = changed_categories(current, previous)
            audit.detail.update(
                image_before=clip(image_of(current), 512),
                image_after=clip(image_of(previous), 512),
                changed=",".join(categories),
            )
            if is_administrator(actor) and findings:
                audit.detail["restricted"] = ",".join(refused_options(findings))
            await self._send(
                actor,
                client,
                environment_id,
                service_id,
                current,
                version=document.version,
                action="service_rolled_back",
                auth_from="previous-spec",
                rollback=True,
            )
        return ServiceRolledBack(environment_id, service_id, name, categories)

    @audited(
        action="service_updated",
        target_type="service",
        target_id=service_argument,
        environment_id=environment_argument,
    )
    async def update_image(
        self,
        actor: AutomationAuthority,
        environment_id: int,
        service_id: str,
        *,
        reference: str,
        remote_digest: str,
        credentials: PullCredentials,
        expected_version: int,
        verify: ServiceVerifyPolicy,
        note: Mapping[str, DetailValue] | None = None,
    ) -> ServiceImageUpdated:
        """Move a service to a newer digest of the image it runs and watch the rollout.

        Only the image changes, and it is sent with the credentials of the binding, or with none
        for an unbound image. The service stays locked while the rollout is watched.
        ServiceUpdateFailed when the rollout ended badly or could not be read; the service is not
        rolled back.
        """
        audit = current_audit()
        if not isinstance(actor, AutomationAuthority):
            raise TypeError("an image update is made by an automation run")
        require_binding(credentials)
        if _DIGEST.fullmatch(remote_digest) is None:
            audit.reason = "invalid_digest"
            raise ValueError("remote_digest must be sha256: followed by 64 lower-case hex digits")
        client = await self._authorise_write(actor, environment_id, service_id)
        async with self._hold(actor, environment_id, service_id, "update_image"):
            document = await self._fresh(environment_id, service_id)
            name = str(document.spec.get("Name") or "")[:MAX_NAME]
            audit.detail.update(note or {})
            audit.detail.update(name=name, version_before=document.version)
            if document.version != expected_version:
                audit.reason = "changed_since_review"
                raise ServiceChangedSinceReview()
            spec = migrate_legacy_networks(document.spec)
            image_text = image_of(spec) or ""
            image = split_service_image(image_text)
            with guarded():
                wanted = parse_pull_reference(reference)
            if image.kind != "tagged_digest" or reference_key(image) != wanted.key:
                audit.reason = "changed_since_review"
                raise ServiceChangedSinceReview()
            pinned_image = pinned_text(image, remote_digest)
            audit.detail.update(
                image_before=clip(image_text, 512),
                image_after=clip(pinned_image, 512),
                digest_before=image.digest,
                digest_after=remote_digest,
            )
            if image.digest == remote_digest:
                audit.unchanged()
                return ServiceImageUpdated(
                    environment_id, service_id, name, image_text, pinned_image, None, None, None
                )
            with guarded():
                if credentials.credential_set_id is not None:
                    await actor.authorise_credential_set(credentials.credential_set_id)
                if not is_administrator(actor):
                    findings = find_restricted_service(spec)
                    if findings:
                        fields = {finding.key: finding.message for finding in reversed(findings)}
                        raise RestrictedService(findings, fields)
            container = mapping_of(mapping_of(spec.get("TaskTemplate")).get("ContainerSpec"))
            if not isinstance(container, dict):
                audit.reason = "changed_since_review"
                raise ServiceChangedSinceReview()
            container["Image"] = pinned_image
            update_config = mapping_of(spec.get("UpdateConfig"))
            audit.detail.update(
                changed="image",
                image_changed=True,
                digest_pinned=True,
                credential_set_id=credentials.credential_set_id,
                anonymous=credentials.credential_set_id is None,
                credential_source=credentials.source,
                failure_action=str(update_config.get("FailureAction") or "pause")[:32],
                verify_seconds=verify.seconds,
            )
            before = (await self._inspect_fresh(environment_id, service_id)).update_status
            started_before = "" if before is None else before.started_at
            running_before = len(await self._running(client, environment_id, service_id))
            audit.detail["tasks_running_before"] = running_before
            pinned = PinnedImage(pinned_image, credentials, wanted, remote_digest, True)
            await self._send(
                actor,
                client,
                environment_id,
                service_id,
                spec,
                version=document.version,
                action="service_updated",
                auth=credentials.registry_auth,
                bind=pinned,
            )
            verdict = await self._watch_rollout(
                actor,
                client,
                environment_id,
                service_id,
                spec,
                running_before,
                verify,
                started_before,
            )
            await self._bind_after_send(actor, environment_id, pinned)
        return ServiceImageUpdated(
            environment_id,
            service_id,
            name,
            image_text,
            pinned_image,
            verdict.verified,
            verdict.update_state,
            verdict.version,
        )

    @audited(
        action="service_credentials_refreshed",
        target_type="service",
        target_id=service_argument,
        environment_id=environment_argument,
    )
    async def refresh_credentials(
        self,
        actor: AutomationAuthority,
        environment_id: int,
        service_id: str,
        *,
        reference: str,
        credentials: PullCredentials,
        note: Mapping[str, DetailValue] | None = None,
    ) -> ServiceCredentialsRefreshed:
        """Send the service's own spec again with a fresh registry credential, replacing no task.

        ServiceUpdateFailed("update_in_progress") while a rollout is running or paused.
        """
        audit = current_audit()
        if not isinstance(actor, AutomationAuthority):
            raise TypeError("a credential refresh is made by an automation run")
        require_binding(credentials)
        client = await self._authorise_write(actor, environment_id, service_id)
        async with self._hold(actor, environment_id, service_id, "refresh_credentials"):
            document = await self._fresh(environment_id, service_id)
            name = str(document.spec.get("Name") or "")[:MAX_NAME]
            labels = {
                key: value
                for key, value in mapping_of(document.spec.get("Labels")).items()
                if isinstance(value, str)
            }
            audit.detail.update(trigger="refresh", provider="ecr")
            audit.detail.update(note or {})
            audit.detail.update(
                name=name,
                version_before=document.version,
                reference=clip(reference, 255),
                credential_set_id=credentials.credential_set_id,
            )
            stack = stack_name(labels)
            if stack is not None:
                audit.detail["stack"] = clip(stack)
            inspected = await self._inspect_fresh(environment_id, service_id)
            update = inspected.update_status
            state = None if update is None else state_name(update.state)
            if state in ROLLOUT_IN_FLIGHT:
                audit.reason = "update_in_progress"
                raise ServiceUpdateFailed("update_in_progress", state, inspected.version.index)
            with guarded():
                wanted = parse_pull_reference(reference).key
            if reference_key(split_service_image(image_of(document.spec) or "")) != wanted:
                audit.reason = "changed_since_review"
                raise ServiceChangedSinceReview()
            sendable = migrate_legacy_networks(document.spec)
            if sendable.get("TaskTemplate") != document.spec.get("TaskTemplate"):
                audit.reason = "legacy_networks"
                raise ServiceUpdateFailed("legacy_networks", state, inspected.version.index)
            with guarded():
                if credentials.credential_set_id is not None:
                    await actor.authorise_credential_set(credentials.credential_set_id)
            await self._send(
                actor,
                client,
                environment_id,
                service_id,
                sendable,
                version=document.version,
                action="service_credentials_refreshed",
                auth=credentials.registry_auth,
            )
        return ServiceCredentialsRefreshed(environment_id, service_id, name)

    @audited(
        action="service_removed",
        target_type="service",
        target_id=service_argument,
        environment_id=environment_argument,
    )
    async def remove(
        self,
        actor: Principal,
        environment_id: int,
        service_id: str,
        *,
        expected_name: str,
        expected_version: int,
    ) -> ServiceRemoved:
        """Remove a service the caller reviewed, then release its control.

        Its containers are removed and its volumes are kept. The control is released in the
        transaction that commits the success row, only after the Engine removed the service; if
        the release fails the removal still counts and the row records control_released as false.
        """
        audit = current_audit()
        client = await self._authorise_write(actor, environment_id, service_id)
        async with self._hold(actor, environment_id, service_id, "remove"):
            document = await self._fresh(environment_id, service_id)
            name = str(document.spec.get("Name") or "")[:MAX_NAME]
            view = migrate_legacy_networks(document.spec)
            running = len(await self._running(client, environment_id, service_id))
            audit.detail.update(
                name=name,
                mode=mode_text(view),
                replicas=replicas_of(view) or 0,
                tasks=running,
            )
            if expected_name != name or expected_version != document.version:
                audit.reason = "changed_since_review"
                raise ServiceChangedSinceReview()
            try:
                await run_shielded(
                    partial(client.swarm_services.remove, service_id),
                    partial(self._interrupted_remove, actor, environment_id, service_id),
                )
            except EngineError as error:
                raise await self._remove_failure(error, environment_id, service_id) from None
            released = await self._release(actor, environment_id, service_id)
        return ServiceRemoved(environment_id, service_id, name, released)

    @asynccontextmanager
    async def _hold(
        self, actor: Acting, environment_id: int, service_id: str, action: str
    ) -> AsyncIterator[None]:
        key = ObjectKey(environment_id, "service", service_id)
        if isinstance(actor, AutomationAuthority):
            async with self._held_for_automation(actor, key, action):
                yield
            return
        holder = LockHolder(actor_of(actor), holder_label(actor), action)
        async with hold_all(self._locks, [key], holder):
            yield

    @asynccontextmanager
    async def _held_for_automation(
        self, authority: AutomationAuthority, key: ObjectKey, action: str
    ) -> AsyncIterator[None]:
        """Take the service's lock for a run; a refusal to take it leaves no event."""
        audit = current_audit()
        async with AsyncExitStack() as held:
            try:
                await held.enter_async_context(authority.hold(key, action))
            except (ObjectBusy, RunSkipped):
                audit.refuse()
                raise
            yield

    async def _authorise_write(
        self, actor: Acting, environment_id: int, service_id: str
    ) -> EngineClient:
        audit = current_audit()
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        if not is_canonical_id("service", service_id):
            raise ResourceNotFound("not found")
        try:
            if isinstance(actor, AutomationAuthority):
                await actor.authorise(ObjectKey(environment_id, "service", service_id))
            else:
                await self._authoriser.require_access(
                    actor, ResourceKey(environment_id, "service", service_id), "write"
                )
        except (EnvironmentUnavailable, ObjectTooLarge):
            audit.refuse()
            raise
        if self._self_identity().protects_service(environment_id, service_id):
            audit.reason = SELF_PROTECTED_REASON
            raise SelfProtected.service()
        return await audited_client(self._hosts, environment_id)

    async def _fresh(self, environment_id: int, service_id: str) -> Any:
        audit = current_audit()
        self._inspector.forget_service(environment_id, service_id)
        try:
            return await self._inspector.service_document(environment_id, service_id)
        except ResourceNotFound:
            audit.reason = "service_gone"
            raise ServiceGone() from None
        except EnvironmentUnavailable as error:
            audit.reason = error.kind
            raise
        except ObjectTooLarge:
            audit.reason = "too_large"
            raise

    async def _inspect_fresh(self, environment_id: int, service_id: str) -> SwarmService:
        audit = current_audit()
        self._inspector.forget_service(environment_id, service_id)
        try:
            return await self._inspector.service(environment_id, service_id)
        except ResourceNotFound:
            audit.reason = "service_gone"
            raise ServiceGone() from None
        except EnvironmentUnavailable as error:
            audit.reason = error.kind
            raise
        except ObjectTooLarge:
            audit.reason = "too_large"
            raise

    async def _watch_rollout(
        self,
        actor: AutomationAuthority,
        client: EngineClient,
        environment_id: int,
        service_id: str,
        spec: Mapping[str, Any],
        running_before: int,
        policy: ServiceVerifyPolicy,
        started_before: str,
    ) -> Verdict:
        """Poll the rollout the update started; the service's lock is held throughout.

        An interrupted watch writes a failure row, because the outcome of the sent update is
        unknown.
        """
        audit = current_audit()
        try:
            return await self._poll_rollout(
                client, environment_id, service_id, spec, running_before, policy, started_before
            )
        except ServiceUpdateFailed as failed:
            audit.reason = failed.reason
            audit.detail.update(verified=False, verify_failure=failed.reason)
            if failed.update_state is not None:
                audit.detail["update_state"] = failed.update_state
            raise
        except asyncio.CancelledError:
            audit.detail.update(verified=False, verify_failure="interrupted")
            with anyio.CancelScope(shield=True):
                await self._write(
                    actor,
                    "service_updated",
                    environment_id,
                    service_id,
                    {**audit.detail, "interrupted": True},
                    failure="interrupted",
                )
            raise

    async def _poll_rollout(
        self,
        client: EngineClient,
        environment_id: int,
        service_id: str,
        spec: Mapping[str, Any],
        running_before: int,
        policy: ServiceVerifyPolicy,
        started_before: str,
    ) -> Verdict:
        audit = current_audit()
        window = float(policy.seconds)
        bound = max(window, rollout_bound(spec, running_before))
        elapsed = 0.0
        state: str | None = None
        version: int | None = None
        seen_updating = False
        own_rollout = False
        for _ in range(MAX_VERIFY_POLLS):
            service = await self._read_rollout(client, environment_id, service_id)
            update = service.update_status
            state = None if update is None else state_name(update.state)
            version = service.version.index
            seen_updating = seen_updating or state == "updating"
            own_rollout = seen_updating or (
                update is not None and update.started_at != started_before
            )
            if state is not None:
                audit.detail["update_state"] = state
            match state:
                case "paused":
                    raise ServiceUpdateFailed("update_paused", state, version)
                case "rollback_paused":
                    raise ServiceUpdateFailed("rollback_paused", state, version)
                case "rollback_completed":
                    raise ServiceUpdateFailed("rolled_back_by_swarm", state, version)
                case "other":
                    raise ServiceUpdateFailed("unverifiable", state, version)
                case "updating" | "rollback_started":
                    limit = bound
                case "completed" if not own_rollout:
                    limit = window
                case _:
                    await self._require_running(
                        client, environment_id, service_id, running_before, state, version
                    )
                    limit = window
            if elapsed >= limit:
                break
            step = min(policy.interval, limit - elapsed)
            await self._sleep(step)
            elapsed += step
        if state == "rollback_started":
            raise ServiceUpdateFailed("rolled_back_by_swarm", state, version)
        if state == "updating":
            audit.detail.update(verified=False, verify_failure="still_updating")
            return Verdict(False, state, version)
        if state == "completed" and not own_rollout:
            audit.detail.update(verified=False, verify_failure="not_observed")
            return Verdict(False, state, version)
        audit.detail["verified"] = True
        return Verdict(True, state, version)

    async def _read_rollout(
        self, client: EngineClient, environment_id: int, service_id: str
    ) -> SwarmService:
        try:
            service = await swarm_read(
                client.swarm_services.inspect(service_id), environment_id, self._poke
            )
        except ResourceNotFound:
            raise ServiceUpdateFailed("service_gone", None, None) from None
        except ProductError:
            raise ServiceUpdateFailed("unverifiable", None, None) from None
        if service.id != service_id:
            raise ServiceUpdateFailed("unverifiable", None, None)
        return service

    async def _require_running(
        self,
        client: EngineClient,
        environment_id: int,
        service_id: str,
        running_before: int,
        state: str | None,
        version: int,
    ) -> None:
        try:
            counts = await service_status(client, environment_id, service_id, self._poke)
        except ProductError:
            raise ServiceUpdateFailed("unverifiable", state, version) from None
        current_audit().detail["tasks_running_after"] = counts.running_tasks
        if counts.running_tasks < min(counts.desired_tasks, running_before):
            raise ServiceUpdateFailed("tasks_not_running", state, version)

    async def _running(
        self, client: EngineClient, environment_id: int, service_id: str
    ) -> list[SwarmTask]:
        tasks = await audited_swarm_read(
            client.swarm_tasks.list(filters={"service": [service_id]}),
            environment_id,
            self._poke,
        )
        return [t for t in tasks if t.status.state == "running" and t.desired_state == "running"]

    def _describe_create(self, actor: Principal, plan: Plan) -> None:
        audit = current_audit()
        spec = plan.spec
        task = mapping_of(spec.get("TaskTemplate"))
        container = mapping_of(task.get("ContainerSpec"))
        view = migrate_legacy_networks(spec)
        audit.detail.update(
            name=str(spec.get("Name") or "")[:MAX_NAME],
            mode=mode_text(view),
            replicas=replicas_of(view) or 0,
            image=clip(image_of(spec), 512),
            digest_pinned=plan.pinned is not None and plan.pinned.pinned,
            networks=len(as_list(task.get("Networks"))),
            ports=len(as_list(mapping_of(spec.get("EndpointSpec")).get("Ports"))),
            mounts=len(as_list(container.get("Mounts"))),
            configs=len(as_list(container.get("Configs"))),
            secrets=len(as_list(container.get("Secrets"))),
            constraints=len(as_list(mapping_of(task.get("Placement")).get("Constraints"))),
            labels=len(mapping_of(spec.get("Labels"))),
            pinned_to_manager=plan.manager_node_id is not None
            and pinned_to(spec, plan.manager_node_id),
        )
        self._describe_credentials(plan)
        if actor.is_administrator and plan.findings:
            audit.detail["restricted"] = ",".join(refused_options(plan.findings))

    def _describe_credentials(self, plan: Plan) -> None:
        pinned = plan.pinned
        if pinned is None:
            return
        credentials = pinned.credentials
        audit = current_audit()
        audit.detail.update(
            credential_set_id=credentials.credential_set_id,
            anonymous=credentials.credential_set_id is None,
            credential_source=credentials.source,
        )

    async def _create_on_host(
        self,
        client: EngineClient,
        spec: Mapping[str, Any],
        auth: SecretStr | None,
        run: CreateRun,
    ) -> str:
        service_id = await client.swarm_services.create(spec, registry_auth=auth)
        run.service_id = service_id
        return service_id

    async def _send(
        self,
        actor: Acting,
        client: EngineClient,
        environment_id: int,
        service_id: str,
        spec: Mapping[str, Any],
        *,
        version: int,
        action: str,
        auth: SecretStr | None = None,
        auth_from: Literal["spec", "previous-spec"] | None = None,
        rollback: bool = False,
        bind: PinnedImage | None = None,
    ) -> None:
        try:
            await run_shielded(
                partial(
                    client.swarm_services.update,
                    service_id,
                    spec,
                    version=version,
                    registry_auth=auth,
                    registry_auth_from=auth_from,
                    rollback=rollback,
                ),
                partial(self._interrupted_update, actor, action, environment_id, service_id, bind),
            )
        except EngineError as error:
            raise await self._update_failure(
                error, client, environment_id, service_id, version
            ) from None
        self._inspector.forget_service(environment_id, service_id)

    async def _create_failure(
        self, error: EngineError, environment_id: int, plan: Plan
    ) -> ProductError:
        audit = current_audit()
        logger.warning(
            "service_create_refused",
            extra={"error": type(error).__name__, "status": error.status_code},
        )
        if isinstance(error, Conflict):
            audit.reason = "name_in_use"
            return ServiceNameInUse()
        classified = engine_failure(error, environment_id, self._poke)
        if classified is not None:
            audit.reason = classified[0]
            return classified[1]
        gone = await self._missing_reference(environment_id, plan.spec)
        if gone is not None:
            kind, field_name = gone
            audit.reason = f"{kind}_gone"
            return ServiceInvalid({field_name: f"A {kind} this service uses no longer exists."})
        audit.reason = "engine_rejected"
        return ServiceRefused(engine_detail(error))

    async def _missing_reference(
        self, environment_id: int, spec: Mapping[str, Any]
    ) -> tuple[str, str] | None:
        held = references(spec)
        for object_id, _name, _file in held.configs:
            self._inspector.forget_config(environment_id, object_id)
            try:
                await self._inspector.config(environment_id, object_id)
            except ResourceNotFound:
                return "config", "configs"
            except ProductError:
                continue
        for object_id, _name, _file in held.secrets:
            self._inspector.forget_secret(environment_id, object_id)
            try:
                await self._inspector.secret(environment_id, object_id)
            except ResourceNotFound:
                return "secret", "secrets"
            except ProductError:
                continue
        return None

    async def _update_failure(
        self,
        error: EngineError,
        client: EngineClient,
        environment_id: int,
        service_id: str,
        version: int,
    ) -> ProductError:
        audit = current_audit()
        logger.warning(
            "service_update_refused",
            extra={"error": type(error).__name__, "status": error.status_code},
        )
        if isinstance(error, NotFound):
            audit.reason = "service_gone"
            return ServiceGone()
        classified = engine_failure(error, environment_id, self._poke)
        if classified is not None:
            audit.reason = classified[0]
            return classified[1]
        try:
            fresh = await client.swarm_services.document(service_id)
        except NotFound:
            audit.reason = "service_gone"
            return ServiceGone()
        except EngineError:
            audit.reason = "engine_rejected"
            return ServiceRefused(engine_detail(error))
        if fresh.version != version:
            audit.reason = "changed_since_review"
            return ServiceChangedSinceReview()
        audit.reason = "engine_rejected"
        return ServiceRefused(engine_detail(error))

    async def _remove_failure(
        self, error: EngineError, environment_id: int, service_id: str
    ) -> ProductError:
        audit = current_audit()
        logger.warning(
            "service_remove_refused",
            extra={"error": type(error).__name__, "status": error.status_code},
        )
        if isinstance(error, NotFound):
            audit.reason = "service_gone"
            await self._release_gone(environment_id, service_id)
            return ServiceGone()
        classified = engine_failure(error, environment_id, self._poke)
        if classified is not None:
            audit.reason = classified[0]
            return classified[1]
        audit.reason = "engine_rejected"
        return EngineRefused(engine_detail(error))

    async def _settle_create(
        self, actor: Principal, environment_id: int, service_id: str, plan: Plan
    ) -> bool:
        """Claim the new service's control and bind its image in the transaction of the success row.

        A second attempt without the binding runs if the first fails; if both fail the service
        stays uncontrolled for non-administrators to find, and the row says so.
        """
        audit = current_audit()
        key = ResourceKey(environment_id, "service", service_id)

        def note(created: bool) -> None:
            audit.detail["control_created"] = created

        bind = self._binding_work(actor, environment_id, plan.pinned)
        attempts: list[Callable[[], Any] | None] = [bind, None] if bind is not None else [None]
        for attempt in attempts:
            try:
                return await retry_busy(
                    partial(
                        in_transaction,
                        self._db,
                        partial(self._controls.claim, key, actor.user_id),
                        attempt,
                        note,
                    )
                )
            except asyncio.CancelledError:
                await self._write(
                    actor,
                    "service_created",
                    environment_id,
                    service_id,
                    {**audit.detail, "interrupted": True},
                    claim=True,
                    pinned=plan.pinned,
                )
                raise
            except Exception as error:
                self._log_control_failure("service_control_claim_failed", key, error)
                if attempt is not None:
                    audit.detail["binding"] = "not_recorded"
        audit.detail["control_created"] = False
        return False

    def _binding_work(
        self, actor: Acting, environment_id: int, pinned: PinnedImage | None
    ) -> Callable[[], Any] | None:
        if self._bindings is None or pinned is None:
            return None
        return partial(self._record_binding, actor, environment_id, pinned)

    async def _record_binding(
        self, actor: Acting, environment_id: int, pinned: PinnedImage
    ) -> None:
        assert self._bindings is not None
        outcome = await self._bindings.record_pull(
            binding_actor(actor),
            environment_id,
            pinned.reference,
            pinned.credentials,
            pinned.digest,
        )
        current_audit().detail["binding"] = outcome

    async def _bind_after_send(
        self, actor: Acting, environment_id: int, pinned: PinnedImage | None
    ) -> None:
        audit = current_audit()
        work = self._binding_work(actor, environment_id, pinned)
        if work is None:
            return
        try:
            await retry_busy(partial(in_transaction, self._db, work))
        except asyncio.CancelledError:
            await self._write(
                actor,
                "service_updated",
                environment_id,
                str(audit.target_id),
                {**audit.detail, "interrupted": True},
                pinned=pinned,
            )
            raise
        except Exception as error:
            logger.error(
                "service_binding_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            audit.detail["binding"] = "not_recorded"

    async def _release(self, actor: Principal, environment_id: int, service_id: str) -> bool:
        audit = current_audit()
        key = ResourceKey(environment_id, "service", service_id)

        def note(_: bool) -> None:
            audit.detail["control_released"] = True

        try:
            await retry_busy(
                partial(
                    in_transaction,
                    self._db,
                    partial(self._controls.release, key),
                    None,
                    note,
                )
            )
        except asyncio.CancelledError:
            await self._write(
                actor,
                "service_removed",
                environment_id,
                service_id,
                {**audit.detail, "interrupted": True},
                claim=False,
            )
            raise
        except Exception as error:
            self._log_control_failure("service_control_release_failed", key, error)
            audit.detail["control_released"] = False
            return False
        return True

    async def _release_gone(self, environment_id: int, service_id: str) -> None:
        audit = current_audit()
        key = ResourceKey(environment_id, "service", service_id)
        try:
            async with self._database.session() as db:
                recorder = ActivityRecorder(db, self._clock)
                controls = ResourceControlService(
                    db=db,
                    controls=ResourceControlRepository(db, self._clock),
                    activity=recorder,
                )
                released = await retry_busy(
                    partial(in_transaction, db, partial(controls.release, key))
                )
        except Exception as error:
            self._log_control_failure("service_control_release_failed", key, error)
            audit.detail["control_released"] = False
        else:
            audit.detail["control_released"] = released

    async def _interrupted_create(
        self,
        actor: Principal,
        environment_id: int,
        run: CreateRun,
        plan: Plan,
        task: asyncio.Future[str],
    ) -> None:
        audit = current_audit()
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, SETTLE_SECONDS)
            detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
            if run.service_id is not None:
                await self._write(
                    actor,
                    "service_created",
                    environment_id,
                    run.service_id,
                    detail,
                    claim=True,
                    pinned=plan.pinned,
                )
            elif not finished:
                task.add_done_callback(
                    partial(self._settle_late, actor, environment_id, run, dict(detail), plan)
                )
                await self._write(
                    actor,
                    "service_created",
                    environment_id,
                    None,
                    detail,
                    failure="outcome_unknown",
                    pinned=plan.pinned,
                )
            else:
                await self._write(
                    actor,
                    "service_created",
                    environment_id,
                    None,
                    detail,
                    failure=failure_reason(error) if error is not None else "interrupted",
                    pinned=plan.pinned,
                )

    def _settle_late(
        self,
        actor: Principal,
        environment_id: int,
        run: CreateRun,
        detail: dict[str, DetailValue],
        plan: Plan,
        task: asyncio.Future[str],
    ) -> None:
        if task.cancelled() or task.exception() is not None or run.service_id is None:
            return
        future = asyncio.ensure_future(
            self._write(
                actor,
                "service_created",
                environment_id,
                run.service_id,
                {**detail, "settled_late": True},
                claim=True,
                pinned=plan.pinned,
            )
        )
        self._background.add(future)
        future.add_done_callback(self._background.discard)

    async def _interrupted_update(
        self,
        actor: Acting,
        action: str,
        environment_id: int,
        service_id: str,
        pinned: PinnedImage | None,
        task: asyncio.Future[None],
    ) -> None:
        audit = current_audit()
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, SETTLE_SECONDS)
            failure = None
            if not (finished and error is None):
                failure = failure_reason(error) if error is not None else "outcome_unknown"
            await self._write(
                actor,
                action,
                environment_id,
                service_id,
                {**audit.detail, "interrupted": True},
                failure=failure,
                pinned=pinned,
            )

    async def _interrupted_remove(
        self, actor: Principal, environment_id: int, service_id: str, task: asyncio.Future[None]
    ) -> None:
        audit = current_audit()
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, SETTLE_SECONDS)
            detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
            if finished and error is None:
                await self._write(
                    actor, "service_removed", environment_id, service_id, detail, claim=False
                )
                return
            await self._write(
                actor,
                "service_removed",
                environment_id,
                service_id,
                detail,
                failure=failure_reason(error) if error is not None else "outcome_unknown",
            )

    async def _write(
        self,
        actor: Acting,
        action: str,
        environment_id: int,
        target_id: str | None,
        detail: dict[str, DetailValue],
        *,
        claim: bool | None = None,
        failure: str | None = None,
        pinned: PinnedImage | None = None,
    ) -> None:
        """Write the row of an operation that outlived its request, on a session of its own.

        `claim` True claims the new service's control in the row's transaction, False releases
        the removed service's; None leaves controls alone. `pinned` is the image the operation
        pulled: its binding is written in the same transaction, and a row that cannot carry it
        says `not_recorded`.
        """
        due = pinned if self._bindings is not None else None
        bind = due if self._bindings_factory is not None and failure is None else None
        if due is not None and bind is None:
            detail["binding"] = "not_recorded"
        steps = (
            []
            if target_id is None
            else self._row_steps(actor, environment_id, target_id, detail, claim, bind)
        )
        await write_settled_row(
            self._database,
            self._clock,
            actor,
            action=action,
            target_type="service",
            target_id=target_id,
            environment_id=environment_id,
            detail=detail,
            failure=failure,
            steps=steps,
        )

    def _row_steps(
        self,
        actor: Acting,
        environment_id: int,
        target_id: str,
        detail: dict[str, DetailValue],
        claim: bool | None,
        bind: PinnedImage | None,
    ) -> list[SettleStep]:
        """Build the attempts that settle the control and the binding, then the control alone."""
        key = ResourceKey(environment_id, "service", target_id)
        field_name = "control_created" if claim else "control_released"
        claimant = actor.user_id if isinstance(actor, Principal) else None

        def attempt(with_binding: bool) -> SettleStep:
            def work(
                db: Session, recorder: ActivityRecorder
            ) -> Callable[[], Awaitable[tuple[bool | None, str | None]]]:
                controls = ResourceControlService(
                    db=db, controls=ResourceControlRepository(db, self._clock), activity=recorder
                )
                bindings = (
                    self._bindings_factory(db, recorder)
                    if with_binding and self._bindings_factory is not None
                    else None
                )

                async def run() -> tuple[bool | None, str | None]:
                    result: bool | None = None
                    if claim is True and claimant is not None:
                        result = await controls.claim(key, claimant)
                    elif claim is False:
                        result = await controls.release(key)
                    outcome: str | None = None
                    if bindings is not None and bind is not None:
                        outcome = await bindings.record_pull(
                            binding_actor(actor),
                            environment_id,
                            bind.reference,
                            bind.credentials,
                            bind.digest,
                        )
                    return result, outcome

                return run

            def note(result: tuple[bool | None, str | None]) -> None:
                if claim is not None:
                    detail[field_name] = bool(result[0]) if claim else True
                if result[1] is not None:
                    detail["binding"] = result[1]

            def failed(error: Exception) -> None:
                self._log_control_failure("service_control_settle_failed", key, error)
                if with_binding:
                    detail["binding"] = "not_recorded"
                elif claim is not None:
                    detail[field_name] = False

            return SettleStep(work, note, failed)

        steps = [attempt(True)] if bind is not None else []
        if claim is not None:
            steps.append(attempt(False))
        return steps

    def _log_control_failure(self, event: str, key: ResourceKey, error: Exception) -> None:
        logger.error(
            event,
            extra={
                "environment_id": key.environment_id,
                "service_id": key.resource_id,
                "error": type(error).__name__,
            },
        )
