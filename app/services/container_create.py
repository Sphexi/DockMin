"""Container creation and duplication: the form flow and the audited create operation."""

import asyncio
import re
from collections.abc import Mapping
from contextlib import AsyncExitStack
from functools import partial
from typing import Any, Final

import anyio

from app.auth.errors import SELF_PROTECTED_REASON, ProductError, ResourceNotFound, SelfProtected
from app.auth.principal import Principal
from app.db.engine import retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.resource_control import ResourceControlRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError
from app.docker_engine.errors import NotFound as EngineNotFound
from app.docker_engine.models import Volume
from app.logging import get_logger
from app.services.access import ResourceKey, is_canonical_id
from app.services.activity import ActivityRecorder, audited, current_audit, environment_argument
from app.services.container_actions import engine_detail, retrieve_exception
from app.services.container_derivation import spec_from_inspect
from app.services.container_provisioning import (
    Claim,
    ConfigurationRejected,
    ControlStep,
    CreateRun,
    Failure,
    NameInUse,
    ProvisionFeed,
    ProvisioningBase,
    SeenVolume,
    Submission,
    SubmissionRejected,
    VolumeChanged,
    VolumeClaim,
    failure_of,
    note_stage,
    settle_seconds,
    step_kind,
)
from app.services.container_restrictions import option_label
from app.services.container_review import (
    CreateOutcome,
    FormContext,
    ReviewView,
    container_document,
    image_document,
    review_sections,
)
from app.services.container_spec import ContainerSpec, FormState
from app.services.containers import display_name
from app.services.host_objects import in_transaction, settled
from app.services.image_bindings import observe_image
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.pull_credentials import (
    CredentialChoice,
    CredentialChoiceRequired,
    CredentialSetMismatch,
)
from app.services.registry_resolution import CredentialSetUnavailable

__all__ = ["ContainerCreateService"]

logger = get_logger(__name__)

INTERRUPTED: Final = "The request was interrupted before the container was created."

MAX_CLAIMED_VOLUMES: Final = 64

_ANONYMOUS_VOLUME_NAME = re.compile(r"[0-9a-f]{64}")


class ContainerCreateService(ProvisioningBase):
    """Create and duplicate containers, with the restrictions enforced on the body."""

    async def form_context(
        self, actor: Principal, environment_id: int, *, source_id: str | None = None
    ) -> FormContext:
        """Describe the create form, blank or prefilled from an existing container.

        ResourceNotFound or PermissionDenied when the caller may not create in the environment or
        may not read the source. A standard user's prefill follows the detail page's redaction.
        """
        await self._authoriser.require_create(actor, environment_id)
        environment = await self._hosts.environment(environment_id)
        is_local = environment.connection_kind == "unix_socket"
        if source_id is None:
            return FormContext(
                environment_id,
                environment.name,
                is_local,
                actor.is_administrator,
                FormState.from_spec(ContainerSpec.model_construct(image="")),
            )
        if not is_canonical_id("container", source_id):
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "container", source_id), "read"
        )
        inspected = await self._inspector.inspect(environment_id, source_id)
        client = await self._hosts.client(environment_id)
        image = await self._image_of(client, inspected.image)
        defaults = None if actor.is_administrator else await self._daemon_defaults(client)
        document = await asyncio.to_thread(container_document, inspected)
        duplicate = await asyncio.to_thread(
            partial(
                spec_from_inspect,
                document,
                is_administrator=actor.is_administrator,
                image_inspect=image_document(image),
                image_reference=(
                    image.repo_tags[0] if image is not None and image.repo_tags else None
                ),
                defaults=defaults,
            )
        )
        return FormContext(
            environment_id,
            environment.name,
            is_local,
            actor.is_administrator,
            FormState.from_spec(duplicate.spec),
            duplicate.notices,
            duplicate_of=source_id,
            source_name=display_name([inspected.name], inspected.id),
            suggested_name=duplicate.suggested_name,
        )

    async def review(
        self, actor: Principal, environment_id: int, form: Mapping[str, Any]
    ) -> ReviewView:
        """Validate a submission as create would and describe the resolved configuration.

        Changes nothing and records nothing. SubmissionRejected, with the form state, when a
        field is invalid or a restricted option is used.
        """
        submission = await self._prepare(actor, environment_id, form, resolve=False)
        spec = submission.spec
        return ReviewView(
            environment_id=environment_id,
            environment_name=submission.environment.name,
            is_local=submission.environment.connection_kind == "unix_socket",
            image=spec.image,
            name=spec.name,
            will_pull=submission.will_pull,
            start=spec.start,
            state=submission.state,
            sections=review_sections(spec),
            restricted_used=tuple(option_label(option) for option in submission.restricted),
        )

    @audited(
        action="container_created", target_type="container", environment_id=environment_argument
    )
    async def prepare_create(
        self,
        actor: Principal,
        environment_id: int,
        form: Mapping[str, Any],
        *,
        credential_choice: CredentialChoice | None = None,
    ) -> bool:
        """Validate a submission and resolve its credentials as create does; say whether it pulls.

        Changes nothing and records nothing unless the submission is refused, which records the
        refusal create would have recorded.
        """
        async with AsyncExitStack() as held:
            submission = await self._prepared(actor, environment_id, form, credential_choice, held)
        current_audit().unchanged()
        return submission.will_pull

    @audited(
        action="container_created", target_type="container", environment_id=environment_argument
    )
    async def create(
        self,
        actor: Principal,
        environment_id: int,
        form: Mapping[str, Any],
        *,
        duplicate_of: str | None = None,
        credential_choice: CredentialChoice | None = None,
        progress: ProvisionFeed | None = None,
        template: str | None = None,
    ) -> CreateOutcome:
        """Create a container from a submitted form, pulling the image first when needed.

        The caller owns the container afterwards. SubmissionRejected carries the form state;
        NameInUse, PullFailed, ConfigurationRejected and EnvironmentUnavailable report Docker's
        answer. duplicate_of is recorded only when the caller may read that container; `template`, a
        reference the caller has already resolved, is recorded as given. When a
        pull happens, `credential_choice` picks the credential set; CredentialChoiceRequired
        when several fit and none was chosen. The pulled reference is bound to the set used in
        the transaction that claims the container, with the reference's lock held from the choice
        of credentials to the binding; ObjectBusy when another action holds it. Volumes Docker made
        for the container are claimed for the caller in that transaction; VolumeChanged, after the
        container is removed again, when a standard user's named volume turns out host-backed.

        With `progress`, the pull reports its layers to it and a cancel during the pull stops the
        pull and records the failure `cancelled`, or `interrupted` when the process is closing.
        """
        async with AsyncExitStack() as held:
            return await self._create(
                actor,
                environment_id,
                form,
                duplicate_of,
                credential_choice,
                held,
                progress,
                template,
            )

    async def _prepared(
        self,
        actor: Principal,
        environment_id: int,
        form: Mapping[str, Any],
        credential_choice: CredentialChoice | None,
        held: AsyncExitStack,
    ) -> Submission:
        audit = current_audit()
        try:
            return await self._prepare(
                actor, environment_id, form, choice=credential_choice, held=held
            )
        except SubmissionRejected as rejected:
            audit.reason = rejected.reason
            audit.detail.update(rejected.detail)
            raise
        except SelfProtected:
            audit.reason = SELF_PROTECTED_REASON
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
        except CredentialSetUnavailable:
            audit.reason = "credential_unavailable"
            raise

    async def _create(
        self,
        actor: Principal,
        environment_id: int,
        form: Mapping[str, Any],
        duplicate_of: str | None,
        credential_choice: CredentialChoice | None,
        held: AsyncExitStack,
        progress: ProvisionFeed | None,
        template: str | None,
    ) -> CreateOutcome:
        audit = current_audit()
        submission = await self._prepared(actor, environment_id, form, credential_choice, held)
        spec = submission.spec
        source = await self._verified_source(actor, environment_id, duplicate_of)
        audit.detail.update(
            image=spec.image,
            pull=spec.pull,
            pulled=False,
            credential_set_id=submission.credentials.credential_set_id,
            started=False,
        )
        if submission.will_pull:
            audit.detail.update(
                anonymous=submission.credentials.credential_set_id is None,
                credential_source=submission.credentials.source,
            )
        if spec.name:
            audit.detail["name"] = spec.name
        if source is not None:
            audit.detail["duplicate_of"] = source
        if template is not None:
            audit.detail["template"] = template
        if submission.restricted:
            audit.detail["restricted"] = ",".join(submission.restricted)
        run = CreateRun(feed=progress)
        task: asyncio.Future[str | None] = asyncio.ensure_future(
            self._create_sequence(submission, run)
        )
        task.add_done_callback(retrieve_exception)
        try:
            container_id = await self._sequence_result(task, run)
        except asyncio.CancelledError:
            run.abandoned = True
            await self._interrupted_create(actor, environment_id, task, run)
            raise
        if container_id is None:
            raise RuntimeError("the create sequence ended without a container")
        key = ResourceKey(environment_id, "container", container_id)
        step = Claim(key, actor.user_id, volumes=run.volumes)
        control_created = False
        if run.volumes_preexisting:
            audit.detail["volumes_preexisting"] = run.volumes_preexisting

        def note_claim(created: bool) -> None:
            if not created:
                audit.detail["control_created"] = False

        bind = (
            partial(
                self._bind, actor, environment_id, submission.reference, submission.credentials, run
            )
            if run.pulled and submission.reference is not None
            else None
        )
        try:
            control_created = await self._apply_control(step, note_claim, bind)
        except asyncio.CancelledError:
            await self._write_interrupted(
                actor,
                action="container_created",
                environment_id=environment_id,
                target_id=container_id,
                detail={**audit.detail, "interrupted": True},
                failure=None,
                control=step,
            )
            raise
        except Exception as error:
            self._log_control_failure("container_control_claim_failed", key, error)
            audit.detail.pop("volumes_claimed", None)
        if not control_created:
            audit.detail["control_created"] = False
        return CreateOutcome(
            environment_id=environment_id,
            container_id=container_id,
            name=spec.name,
            image=spec.image,
            pulled=run.pulled,
            started=run.started,
            start_failed=run.start_failed,
            control_created=control_created,
        )

    async def _create_sequence(self, submission: Submission, run: CreateRun) -> str | None:
        audit = current_audit()
        client, spec = submission.client, submission.spec
        environment_id = submission.environment.id
        if run.abandoned:
            return None
        if submission.will_pull:
            await self._pull(
                client, environment_id, spec.image, submission.credentials.registry_auth, run
            )
            run.pulled = True
            audit.detail["pulled"] = True
            if submission.reference is not None:
                run.digest = (await observe_image(client, submission.reference))[1]
            if run.abandoned:
                return None
        note_stage(run, "create")
        try:
            container_id = await client.containers.create(submission.body, name=spec.name)
        except EngineError as error:
            raise self._create_error(error, environment_id, named=spec.name is not None) from None
        run.container_id = container_id
        audit.target_id = container_id
        await self._scan_volumes(submission, run)
        if not spec.start:
            return container_id
        note_stage(run, "start")
        try:
            await client.containers.start(container_id)
        except EngineError as error:
            logger.warning(
                "container_start_after_create_failed",
                extra={
                    "environment_id": environment_id,
                    "container_id": container_id,
                    "error": type(error).__name__,
                },
            )
            run.start_failed = True
            audit.detail["start_failed"] = True
        else:
            run.started = True
            audit.detail["started"] = True
        return container_id

    async def _scan_volumes(self, submission: Submission, run: CreateRun) -> None:
        """Find the volumes Docker made for the new container and fix which of them are ours.

        Runs after the create and before the start. For a standard user every named volume the
        reach rule approved is read again; one that changed, or that the caller can no longer
        reach, undoes the create. An absent volume is ours only if it has no control or still
        has the one it had when it was shown absent.
        """
        client, container_id = submission.client, run.container_id
        environment_id = submission.environment.id
        if container_id is None:
            return
        if submission.restricted_actor:
            await self._recheck_sources(submission, run, container_id)
        controls = ResourceControlRepository(self._db, self._clock)
        found: list[VolumeClaim] = []
        preexisting = 0
        for name in submission.absent_volumes:
            volume: Volume | None = None
            try:
                volume = await client.volumes.inspect(name)
            except EngineNotFound:
                continue
            except EngineError as error:
                logger.warning(
                    "volume_scan_failed",
                    extra={"environment_id": environment_id, "error": type(error).__name__},
                )
            if volume is None:
                if submission.restricted_actor:
                    raise await self._roll_back_create(client, environment_id, container_id, run)
                preexisting += 1
                continue
            backed = volume.driver != "local" or bool(volume.options)
            if backed and submission.restricted_actor:
                raise await self._roll_back_create(client, environment_id, container_id, run)
            if backed or volume.labels:
                preexisting += 1
                continue
            control = await controls.get_for(environment_id, "volume", name)
            if control is None:
                found.append(VolumeClaim(name, False))
            elif control.id == submission.stale_controls.get(name):
                found.append(VolumeClaim(name, False, stale=True))
            elif submission.restricted_actor:
                raise await self._roll_back_create(client, environment_id, container_id, run)
            else:
                preexisting += 1
        if not submission.inherits_volumes:
            found.extend(
                await self._anonymous_volumes(client, environment_id, container_id, submission)
            )
        run.volumes = tuple(found[:MAX_CLAIMED_VOLUMES])
        run.volumes_preexisting = preexisting

    async def _recheck_sources(
        self, submission: Submission, run: CreateRun, container_id: str
    ) -> None:
        client, actor = submission.client, submission.actor
        environment_id = submission.environment.id
        self._authoriser.forget_decisions()
        for name, seen in submission.seen_volumes.items():
            if actor is None or not await self._source_unchanged(
                client, actor, environment_id, name, seen
            ):
                raise await self._roll_back_create(client, environment_id, container_id, run)

    async def _source_unchanged(
        self,
        client: EngineClient,
        actor: Principal,
        environment_id: int,
        name: str,
        seen: SeenVolume,
    ) -> bool:
        try:
            volume = await client.volumes.inspect(name)
            await self._authoriser.require_access(
                actor, ResourceKey(environment_id, "volume", name), seen.level
            )
        except (EngineError, ProductError) as error:
            logger.warning(
                "volume_recheck_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            return False
        return (
            volume.driver == seen.driver
            and dict(volume.options) == dict(seen.options)
            and volume.created_at == seen.created_at
        )

    async def _anonymous_volumes(
        self,
        client: EngineClient,
        environment_id: int,
        container_id: str,
        submission: Submission,
    ) -> list[VolumeClaim]:
        try:
            inspected = await client.containers.inspect(container_id)
        except EngineError as error:
            logger.warning(
                "volume_scan_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            current_audit().detail["volume_scan"] = "failed"
            return []
        names = dict.fromkeys(
            mount.name
            for mount in inspected.mounts
            if mount.type == "volume"
            and mount.name is not None
            and _ANONYMOUS_VOLUME_NAME.fullmatch(mount.name)
            and mount.name not in submission.volume_sources
        )
        return [VolumeClaim(name, True) for name in names]

    async def _roll_back_create(
        self, client: EngineClient, environment_id: int, container_id: str, run: CreateRun
    ) -> VolumeChanged:
        """Remove the container this create made, keeping its volumes, and return the failure."""
        audit = current_audit()
        audit.reason = "volume_changed"
        try:
            await client.containers.remove(container_id, force=False, volumes=False)
        except EngineError as error:
            logger.error(
                "container_rollback_failed",
                extra={
                    "environment_id": environment_id,
                    "container_id": container_id,
                    "error": type(error).__name__,
                },
            )
            audit.detail["rolled_back"] = False
            audit.detail["restricted_to_administrators"] = await self._quarantine(
                environment_id, container_id
            )
            return VolumeChanged(rolled_back=False)
        run.container_id = None
        audit.target_id = None
        audit.detail["rolled_back"] = True
        return VolumeChanged(rolled_back=True)

    async def _quarantine(self, environment_id: int, container_id: str) -> bool:
        key = ResourceKey(environment_id, "container", container_id)
        step = Claim(key, None, administrators_only=True)
        try:
            async with self._database.session() as db:
                controls = self._fresh_controls(db, ActivityRecorder(db, self._clock))
                await retry_busy(partial(in_transaction, db, self._control_work(controls, step)))
        except Exception as error:
            self._log_control_failure("container_control_restrict_failed", key, error)
            return False
        return True

    def _create_error(
        self, error: EngineError, environment_id: int, *, named: bool
    ) -> ProductError:
        audit = current_audit()
        kind = step_kind(error)
        logger.warning(
            "container_create_refused",
            extra={
                "environment_id": environment_id,
                "error": type(error).__name__,
                "status": error.status_code,
            },
        )
        if kind == "unknown":
            audit.reason = "outcome_unknown"
            return EnvironmentUnavailable("outcome_unknown")
        if kind == "unreachable":
            audit.reason = "unreachable"
            return EnvironmentUnavailable("unreachable")
        if kind == "conflict" and named:
            audit.reason = "name_in_use"
            return NameInUse()
        audit.reason = "engine_rejected"
        return ConfigurationRejected(engine_detail(error))

    async def _interrupted_create(
        self,
        actor: Principal,
        environment_id: int,
        task: asyncio.Future[str | None],
        run: CreateRun,
    ) -> None:
        audit = current_audit()
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, settle_seconds())
            detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
            created = run.container_id
            control: ControlStep | None = None
            failure: Failure | None = None
            if isinstance(error, VolumeChanged):
                failure = failure_of(error, audit.reason)
            elif created is not None:
                control = Claim(
                    ResourceKey(environment_id, "container", created),
                    actor.user_id,
                    volumes=run.volumes,
                )
                if run.volumes_preexisting:
                    detail["volumes_preexisting"] = run.volumes_preexisting
            elif not finished:
                failure = Failure("outcome_unknown", str(EnvironmentUnavailable("outcome_unknown")))
                task.add_done_callback(
                    partial(self._settle_late_create, actor, environment_id, run, audit.detail)
                )
            elif isinstance(error, asyncio.CancelledError):
                failure = self._pull_stopped(run, "created")
            elif error is not None:
                failure = failure_of(error, audit.reason)
            elif run.pulled:
                failure = Failure("interrupted", INTERRUPTED)
            else:
                return
            await self._write_interrupted(
                actor,
                action="container_created",
                environment_id=environment_id,
                target_id=created,
                detail=detail,
                failure=failure,
                control=control,
            )

    def _settle_late_create(
        self,
        actor: Principal,
        environment_id: int,
        run: CreateRun,
        audit_detail: Mapping[str, DetailValue],
        task: asyncio.Future[str | None],
    ) -> None:
        created = run.container_id
        if task.cancelled() or task.exception() is not None or created is None:
            return
        self._spawn(
            self._write_interrupted(
                actor,
                action="container_created",
                environment_id=environment_id,
                target_id=created,
                detail={
                    **audit_detail,
                    "interrupted": True,
                    "settled_late": True,
                    **(
                        {"volumes_preexisting": run.volumes_preexisting}
                        if run.volumes_preexisting
                        else {}
                    ),
                },
                failure=None,
                control=Claim(
                    ResourceKey(environment_id, "container", created),
                    actor.user_id,
                    volumes=run.volumes,
                ),
            )
        )
