"""The audited image operations."""

import asyncio
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any, Final, Literal

import anyio

from app.auth.errors import PermissionDenied, ProductError, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Database, Session
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import (
    Conflict,
    EngineError,
    EnvironmentUnreachable,
    NotFound,
    RequestOutcomeUnknown,
)
from app.docker_engine.errors import PermissionDenied as EnginePermissionDenied
from app.docker_engine.models import (
    ImageDeleteItem,
    ImageInspect,
)
from app.logging import get_logger
from app.registry_client.references import (
    DEFAULT_TAG,
    ImageReference,
    ReferenceInvalid,
    parse_reference,
    split_reference,
)
from app.scheduler.model import RunSkipped
from app.services.acting import Acting, AutomationAuthority, actor_of, holder_label
from app.services.activity import (
    ActivityRecorder,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.container_actions import (
    BusyView,
    EngineRefused,
    engine_detail,
    retrieve_exception,
)
from app.services.containers import (
    short_id,
)
from app.services.guards import require_administrator
from app.services.image_bindings import (
    BindingPullCredentials,
    ImageBindingService,
    RecordOutcome,
    bare_image_id,
    canonical_keys,
    daemon_references,
    observe_image,
    parse_pull_reference,
)
from app.services.image_listing import (
    read_containers,
    read_host_listing,
)
from app.services.image_queries import (
    IMAGE_ID,
    MAX_PRUNE,
    PRUNE_MODES,
    Holder,
    distinct_images,
    holder_of,
    image_argument,
    inspect_image,
    parse_tag_target,
    plan_image,
    prune_candidates,
    remaining_after_untag,
    require_id,
    usage_of,
)
from app.services.image_views import (
    CHANGED_SINCE_REVIEW,
    NO_REPLACED_IMAGE,
    BatchStatus,
    ImageBatchItem,
    ImageBatchResult,
    ImageBatchTarget,
    ImageChangedMidway,
    ImageChangedSinceReview,
    ImageGone,
    ImageHasDependents,
    ImageInUse,
    ImagePullFailed,
    ImagePushFailed,
    ImageTooLarge,
    ProgressSink,
    PruneCandidate,
    PruneItem,
    PruneMode,
    PruneResult,
    PruneStatus,
    PruneTarget,
    PullOutcome,
    PullPlan,
    PushOutcome,
    PushPlan,
    RemoveOutcome,
    TagOutcome,
    references_digest,
)
from app.services.object_inspector import (
    EnvironmentUnavailable,
    HostClients,
    ObjectTooLarge,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.pull_credentials import (
    BindingCredentialMissing,
    CredentialChoice,
    CredentialChoiceRequired,
    CredentialSetMismatch,
    PullCredentials,
    PullSource,
)
from app.services.registry_resolution import CredentialSetUnavailable

logger = get_logger(__name__)

__all__ = [
    "ImageActionService",
]

MAX_LABEL: Final = 512


PULL_OVERALL_SECONDS: Final = 3600.0


AUTOMATION_PULL_SECONDS: Final = 900.0


SETTLE_SECONDS: Final = 120.0


@dataclass(slots=True)
class _Removal:
    untagged: int = 0
    deleted: bool = False
    unexpected: int = 0


@dataclass(frozen=True, slots=True)
class _Operation:
    actor: Acting
    action: str
    environment_id: int
    target_id: str | None
    removal: _Removal | None = None
    removing: bool = False


class ImageActionService:
    """Pull, push, tag, untag and remove, each one audited operation, and the prune orchestrator."""

    def __init__(
        self,
        *,
        db: Session,
        database: Database,
        authoriser: Authoriser,
        hosts: HostClients,
        locks: ObjectLockRegistry,
        credentials: BindingPullCredentials,
        bindings: ImageBindingService,
        activity: ActivityRecorder,
        clock: Clock,
    ) -> None:
        self._db = db
        self._database = database
        self._authoriser = authoriser
        self._hosts = hosts
        self._locks = locks
        self._credentials = credentials
        self._bindings = bindings
        self._activity = activity
        self._clock = clock

    async def prepare_pull(
        self,
        actor: Principal,
        environment_id: int,
        reference_text: str,
        choice: CredentialChoice | None = None,
    ) -> PullPlan:
        """Validate a pull and decide its credentials without starting it; nothing is recorded.

        Raises ValidationFailed for a bad reference, CredentialChoiceRequired when several sets fit
        and none was chosen, BindingCredentialMissing, CredentialSetUnavailable, or
        RebindRefused when a standard user would change an existing binding. The plan is decided
        again when the pull runs; only an explicit choice is carried over.
        """
        await self._authoriser.require_create(actor, environment_id)
        await self._hosts.environment(environment_id)
        decision = await self._credentials.decide(
            environment_id, reference_text, principal=actor, choice=choice
        )
        return PullPlan(environment_id, decision.reference, decision.choice, decision.source)

    async def prepare_push(
        self,
        actor: Principal,
        environment_id: int,
        image_id: str,
        reference_text: str,
        choice: CredentialChoice | None = None,
    ) -> PushPlan:
        """Validate a push of one tag of an image and decide its credentials; records nothing."""
        require_id(image_id)
        await self._authoriser.require_create(actor, environment_id)
        await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        inspected = await inspect_image(client, image_id)
        reference = parse_pull_reference(reference_text)
        if reference.pinned:
            raise ValidationFailed({"reference": "Push a tag, not a digest."})
        if reference.key not in canonical_keys(inspected.repo_tags):
            raise ValidationFailed({"reference": "That reference is not on this image."})
        decision = await self._credentials.decide(
            environment_id, reference.key, principal=actor, choice=choice
        )
        return PushPlan(environment_id, image_id, reference, decision.choice, decision.source)

    @audited(action="image_pulled", target_type="image", environment_id=environment_argument)
    async def pull(
        self,
        actor: Acting,
        environment_id: int,
        plan: PullPlan,
        *,
        progress: ProgressSink | None = None,
        on_behalf_of: ObjectKey | None = None,
        note: Mapping[str, DetailValue] | None = None,
    ) -> PullOutcome:
        """Pull an image with the credentials the plan chose and bind the reference to them.

        The credentials are decided again under the reference's lock. The binding is written, with
        the success row, only after the pull ended cleanly; a failed or cancelled pull writes none.
        Cancelling ends the stream and records an interrupted failure whose outcome is unknown.
        Raises ObjectBusy, ImagePullFailed, EnvironmentUnavailable, or the credential errors of
        `prepare_pull`.

        An automation run passes its authority and names, in `on_behalf_of`, the container or
        stack the image is for; the rule's owner is checked against that object. The reference's
        binding alone chooses the credentials, whatever the plan carries, and the pull ends after
        AUTOMATION_PULL_SECONDS. `note` is merged into the row's detail.
        """
        audit = current_audit()
        if plan.environment_id != environment_id:
            raise ValueError("the plan is for another environment")
        automated = isinstance(actor, AutomationAuthority)
        if automated and on_behalf_of is None:
            raise ValueError("an automated pull names the object it is for")
        if not automated and on_behalf_of is not None:
            raise ValueError("only an automated pull is made on behalf of an object")
        await self._authorise_pull(actor, environment_id, on_behalf_of)
        reference = plan.reference
        audit.detail["reference"] = reference.key
        audit.detail.update(note or {})
        client = await self._client(environment_id)
        key = ObjectKey(environment_id, "image_reference", reference.key)
        limit = AUTOMATION_PULL_SECONDS if automated else PULL_OVERALL_SECONDS
        sent = False
        try:
            async with self._hold_for_pull(actor, key):
                credentials = await self._credentials_for(
                    actor, environment_id, reference, plan.choice, plan.source
                )
                sent = True
                await self._pull_stream(
                    client, environment_id, reference, credentials, progress, limit
                )
                image_id, digest = await observe_image(client, reference)
                binding = await self._commit_binding(
                    actor, environment_id, reference, credentials, digest, image_id
                )
        except asyncio.CancelledError:
            if sent:
                await self._write_interrupted(
                    actor, "image_pulled", environment_id, None, audit.detail
                )
            raise
        return PullOutcome(reference, image_id, digest, binding)

    @audited(
        action="image_pushed",
        target_type="image",
        target_id=plan_image,
        environment_id=environment_argument,
    )
    async def push(
        self,
        actor: Principal,
        environment_id: int,
        plan: PushPlan,
        *,
        progress: ProgressSink | None = None,
    ) -> PushOutcome:
        """Push one tag of an image with the credentials the plan chose and bind the reference.

        The tag must still belong to the image when the lock is held, and the push names it as the
        daemon does. A push that ended cleanly proves the set can reach the repository, so the
        binding is written then. Cancelling behaves as for a pull.
        """
        audit = current_audit()
        if plan.environment_id != environment_id:
            raise ValueError("the plan is for another environment")
        await self._authorise(actor, environment_id, "create")
        reference = plan.reference
        audit.detail["reference"] = reference.key
        client = await self._client(environment_id)
        holder = LockHolder(UserActor.of(actor), actor.username, "push")
        key = ObjectKey(environment_id, "image_reference", reference.key)
        sent = False
        try:
            async with self._hold([key], holder):
                inspected = await self._require_image(client, plan.image_id)
                name = daemon_references(inspected.repo_tags).get(reference.key)
                if name is None:
                    audit.reason = "reference_changed"
                    raise ImageChangedSinceReview()
                credentials = await self._credentials_for(
                    actor, environment_id, reference, plan.choice, plan.source
                )
                sent = True
                pushed = await self._push_stream(
                    client, environment_id, reference, name, credentials, progress
                )
                digest = pushed or (await observe_image(client, reference, name=name))[1]
                binding = await self._commit_binding(
                    actor, environment_id, reference, credentials, digest, plan.image_id
                )
        except asyncio.CancelledError:
            if sent:
                await self._write_interrupted(
                    actor, "image_pushed", environment_id, plan.image_id, audit.detail
                )
            raise
        return PushOutcome(reference, plan.image_id, digest, binding)

    @audited(
        action="image_tagged",
        target_type="image",
        target_id=image_argument,
        environment_id=environment_argument,
    )
    async def tag(
        self,
        actor: Principal,
        environment_id: int,
        image_id: str,
        target_text: str,
        *,
        expected_replaced_id: str,
    ) -> TagOutcome:
        """Give an image another reference; administrators only, and no binding is written.

        `expected_replaced_id` is the id of the image that held the reference when the caller
        reviewed it, or "none"; the holder is checked again under the lock and a different one
        refuses the tag. An image that already has the reference is left alone and no row is
        written.
        """
        audit = current_audit()
        require_id(image_id)
        await self._authorise(actor, environment_id, "administrator")
        expected = _expected_replaced(expected_replaced_id)
        reference = parse_tag_target(target_text)
        audit.detail["reference"] = reference.key
        client = await self._client(environment_id)
        await self._require_image(client, image_id)
        holder = LockHolder(UserActor.of(actor), actor.username, "tag")
        keys = [
            ObjectKey(environment_id, "image", image_id),
            ObjectKey(environment_id, "image_reference", reference.key),
        ]
        operation = _Operation(actor, "image_tagged", environment_id, image_id)
        async with self._hold(keys, holder):
            current = await self._current_holder(client, reference)
            replaced = None if current is None else current.id
            if replaced == image_id:
                audit.unchanged()
                return TagOutcome(reference, image_id, None, True)
            if replaced != expected:
                audit.reason = "replaced_changed"
                raise ImageChangedSinceReview()
            await self._shielded(
                operation,
                lambda: client.images.tag(
                    f"sha256:{image_id}", repository=reference.repository, tag=reference.tag or ""
                ),
            )
        if replaced is not None:
            audit.detail["replaced_image_id"] = replaced
        return TagOutcome(reference, image_id, replaced, False)

    @audited(
        action="image_untagged",
        target_type="image",
        target_id=image_argument,
        environment_id=environment_argument,
    )
    async def untag(
        self,
        actor: Principal,
        environment_id: int,
        image_id: str,
        reference_text: str,
        *,
        expected_last: bool,
        expected_in_use: int,
        force: bool = False,
    ) -> RemoveOutcome:
        """Remove one reference of an image; administrators only.

        When no reference remains, counting as the daemon does, the image is left untagged and
        the daemon removes it; that is refused while containers use it unless `force` is set, and
        a forced untag leaves an image that a running container uses untagged but present.
        `expected_last` and `expected_in_use` are what the caller reviewed; a different state, or
        a name that now belongs to another image, refuses the untag.
        """
        audit = current_audit()
        require_id(image_id)
        await self._authorise(actor, environment_id, "administrator")
        reference = parse_pull_reference(reference_text)
        audit.detail["reference"] = reference.key
        client = await self._client(environment_id)
        inspected = await self._require_image(client, image_id)
        texts = daemon_references(inspected.repo_tags, inspected.repo_digests)
        keys = tuple(texts)
        if reference.key not in texts:
            audit.reason = "references_changed"
            raise ImageChangedSinceReview()
        is_last = not remaining_after_untag(keys, reference.key)
        if is_last != expected_last:
            audit.reason = "references_changed"
            raise ImageChangedSinceReview()
        in_use = await self._count_in_use(client, image_id) if is_last else 0
        audit.detail.update(force=force, in_use_containers=in_use, deleted=False)
        if is_last and in_use != expected_in_use:
            audit.reason = "in_use_changed"
            raise ImageChangedSinceReview()
        if is_last and in_use and not force:
            audit.reason = "image_in_use"
            raise ImageInUse(in_use)
        holder = LockHolder(UserActor.of(actor), actor.username, "untag")
        locks = [
            ObjectKey(environment_id, "image", image_id),
            ObjectKey(environment_id, "image_reference", reference.key),
        ]
        removal = _Removal()
        operation = _Operation(actor, "image_untagged", environment_id, image_id, removal, True)
        async with self._hold(locks, holder):
            await self._recheck(client, image_id, keys, in_use if is_last else None)
            await self._shielded(
                operation,
                partial(
                    self._remove_names,
                    client,
                    image_id,
                    [texts[reference.key]],
                    frozenset(keys),
                    force and is_last,
                    removal,
                    exact=True,
                ),
            )
        audit.detail.update(deleted=removal.deleted, untagged=removal.untagged)
        return RemoveOutcome(
            image_id, len(keys), removal.untagged, removal.deleted, force, in_use, inspected.size
        )

    @audited(
        action="image_removed",
        target_type="image",
        target_id=image_argument,
        environment_id=environment_argument,
    )
    async def remove(
        self,
        actor: Acting,
        environment_id: int,
        image_id: str,
        *,
        expected_references: str,
        expected_in_use: int,
        force: bool = False,
        pruned: bool = False,
        batched: bool = False,
        note: Mapping[str, DetailValue] | None = None,
    ) -> RemoveOutcome:
        """Remove an image the caller reviewed; administrators only.

        `expected_references` is the digest of the reference list and `expected_in_use` the
        container count the confirmation showed; either differing refuses the removal. An image
        in use is refused unless `force`. Without force each reference is removed by name, in the
        daemon's own text and only while the name still belongs to this image, and then the image
        by id; with force the image is removed by id in one call. Bindings are kept.

        An automation run passes its authority, whose owner must be an administrator, and never
        forces. `note` is merged into the row's detail.
        """
        audit = current_audit()
        require_id(image_id)
        await self._authorise_removal(actor, environment_id, image_id, force)
        audit.detail.update(note or {})
        client = await self._client(environment_id)
        inspected = await self._require_image(client, image_id)
        texts = daemon_references(inspected.repo_tags, inspected.repo_digests)
        keys = tuple(texts)
        in_use = await self._count_in_use(client, image_id)
        audit.detail.update(
            references=len(keys), force=force, in_use_containers=in_use, size=inspected.size
        )
        if pruned:
            audit.detail["pruned"] = True
        if batched:
            audit.detail["batch"] = True
        if references_digest(keys) != expected_references:
            audit.reason = "references_changed"
            raise ImageChangedSinceReview()
        if in_use != expected_in_use:
            audit.reason = "in_use_changed"
            raise ImageChangedSinceReview()
        if in_use and not force:
            audit.reason = "image_in_use"
            raise ImageInUse(in_use)
        locks = [
            ObjectKey(environment_id, "image", image_id),
            *(ObjectKey(environment_id, "image_reference", key) for key in keys),
        ]
        removal = _Removal()
        operation = _Operation(actor, "image_removed", environment_id, image_id, removal, True)
        async with self._hold_for_removal(actor, locks):
            await self._recheck(client, image_id, keys, in_use)
            await self._shielded(
                operation,
                partial(
                    self._remove_names,
                    client,
                    image_id,
                    list(texts.values()),
                    frozenset(keys),
                    force,
                    removal,
                    exact=False,
                ),
            )
        audit.detail.update(untagged=removal.untagged, deleted=removal.deleted)
        if removal.unexpected:
            audit.detail["unreviewed_untagged"] = removal.unexpected
        return RemoveOutcome(
            image_id, len(keys), removal.untagged, removal.deleted, force, in_use, inspected.size
        )

    async def run_prune(
        self,
        actor: Principal,
        environment_id: int,
        mode: PruneMode,
        targets: Sequence[PruneTarget],
    ) -> PruneResult:
        """Remove exactly the reviewed images that are still candidates, one at a time.

        Each listed image is its own audited removal with its own lock and row, and nothing is
        rolled back or stopped early; one that changed, is in use, is busy or fails is that
        item's result. The Engine's own prune endpoint is never called. Administrators only, at
        most 100 images.
        """
        if mode not in PRUNE_MODES:
            raise ValidationFailed({"mode": "Choose dangling or unused."})
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        listed = list({t.image_id: t for t in targets}.values())
        if not listed or len(listed) > MAX_PRUNE:
            raise ValidationFailed({"images": f"Select between 1 and {MAX_PRUNE} images."})
        if any(IMAGE_ID.fullmatch(t.image_id) is None for t in listed):
            raise ValidationFailed({"images": "An image id in the selection is not valid."})
        await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        images, containers = await read_host_listing(client)
        current = {c.id: c for c in prune_candidates(images, containers, mode)}
        items: list[PruneItem] = []
        for target in listed:
            candidate = current.get(target.image_id)
            if candidate is None or candidate.references_digest != target.references_digest:
                items.append(
                    PruneItem(target.image_id, short_id(target.image_id), (), "skipped_changed", 0)
                )
                continue
            items.append(await self._prune_one(actor, environment_id, candidate))
        return PruneResult(mode, tuple(items))

    async def run_batch_remove(
        self, actor: Principal, environment_id: int, targets: Sequence[ImageBatchTarget]
    ) -> ImageBatchResult:
        """Remove up to 25 reviewed images in order, each its own audited removal.

        Administrators only. Not atomic and never stopped early; a refusal on one image is that
        image's result. Force is not available: an image in use is reported, not removed.
        ValidationFailed only when the selection is empty or over the limit.
        """
        listed = distinct_images(targets, lambda target: target.image_id)
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        if any(IMAGE_ID.fullmatch(t.image_id) is None for t in listed):
            raise ValidationFailed({"images": "An image id in the selection is not valid."})
        await self._hosts.environment(environment_id)
        return ImageBatchResult(
            tuple([await self._batch_item(actor, environment_id, target) for target in listed])
        )

    async def _batch_item(
        self, actor: Principal, environment_id: int, target: ImageBatchTarget
    ) -> ImageBatchItem:
        shown = short_id(target.image_id)

        def item(status: BatchStatus, message: str, size: int = 0) -> ImageBatchItem:
            return ImageBatchItem(
                target.image_id, shown, target.label[:MAX_LABEL] or shown, status, message, size
            )

        try:
            outcome = await self.remove(
                actor,
                environment_id,
                target.image_id,
                expected_references=target.references_digest,
                expected_in_use=target.in_use,
                force=False,
                batched=True,
            )
        except ImageInUse:
            return item("in_use", "In use by a container. Remove it on its own page to force.")
        except ImageChangedSinceReview:
            return item("changed", CHANGED_SINCE_REVIEW)
        except ObjectBusy:
            return item("busy", "Another action on this image is in progress.")
        except (ImageGone, ResourceNotFound):
            return item("gone", "Not found, or already removed.")
        except (EnvironmentUnavailable, ObjectTooLarge) as error:
            return item("unreachable", str(error))
        except (ImageHasDependents, EngineRefused) as error:
            return item("failed", str(error))
        except Exception as error:
            logger.error(
                "image_batch_item_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            return item("failed", "The image could not be removed.")
        return item("removed", "Removed.", outcome.size)

    def describe_busy(self, busy: ObjectBusy, viewer: Principal) -> BusyView:
        """Describe the lock holder to a viewer: names only for administrators."""
        holder = busy.holder
        age = int(holder.age_seconds(self._locks.now()))
        match holder.actor:
            case UserActor(user_id=user_id):
                mine = user_id == viewer.user_id
                if viewer.is_administrator:
                    return BusyView(holder.action, age, holder.label, mine)
                return BusyView(holder.action, age, "you" if mine else "another user", mine)
            case _:
                return BusyView(holder.action, age, holder.label, False)

    async def _prune_one(
        self, actor: Principal, environment_id: int, candidate: PruneCandidate
    ) -> PruneItem:
        status: PruneStatus
        try:
            await self.remove(
                actor,
                environment_id,
                candidate.id,
                expected_references=candidate.references_digest,
                expected_in_use=0,
                force=False,
                pruned=True,
            )
        except ImageInUse:
            status = "skipped_in_use"
        except ImageChangedSinceReview:
            status = "skipped_changed"
        except ObjectBusy:
            status = "busy"
        except (ImageGone, ResourceNotFound):
            status = "gone"
        except Exception as error:
            logger.warning(
                "image_prune_item_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            status = "failed"
        else:
            status = "removed"
        return PruneItem(
            candidate.id, candidate.short_id, candidate.references, status, candidate.size
        )

    async def _authorise_removal(
        self, actor: Acting, environment_id: int, image_id: str, force: bool
    ) -> None:
        if not isinstance(actor, AutomationAuthority):
            await self._authorise(actor, environment_id, "administrator")
            return
        if force:
            raise ValueError("an automated removal never forces")
        try:
            await actor.authorise(ObjectKey(environment_id, "image", image_id))
        except (EnvironmentUnavailable, ObjectTooLarge):
            current_audit().refuse()
            raise
        if not actor.owner_is_administrator:
            raise PermissionDenied("administrator role required")
        await self._hosts.environment(environment_id)

    @asynccontextmanager
    async def _hold_for_removal(
        self, actor: Acting, keys: Sequence[ObjectKey]
    ) -> AsyncIterator[None]:
        if not isinstance(actor, AutomationAuthority):
            async with self._hold(keys, LockHolder(actor_of(actor), holder_label(actor), "remove")):
                yield
            return
        ordered = sorted(set(keys), key=lambda k: (k.object_type, k.object_id))
        async with AsyncExitStack() as held:
            try:
                for key in ordered:
                    await held.enter_async_context(actor.hold(key, "remove"))
            except (ObjectBusy, RunSkipped):
                current_audit().refuse()
                raise
            yield

    async def _authorise_pull(
        self, actor: Acting, environment_id: int, on_behalf_of: ObjectKey | None
    ) -> None:
        if not isinstance(actor, AutomationAuthority):
            await self._authorise(actor, environment_id, "create")
            return
        if on_behalf_of is None:
            raise ValueError("an automated pull names the object it is for")
        try:
            await actor.authorise(on_behalf_of)
        except (EnvironmentUnavailable, ObjectTooLarge):
            current_audit().refuse()
            raise
        await self._hosts.environment(environment_id)

    @asynccontextmanager
    async def _hold_for_pull(self, actor: Acting, key: ObjectKey) -> AsyncIterator[None]:
        if not isinstance(actor, AutomationAuthority):
            holder = LockHolder(actor_of(actor), holder_label(actor), "pull")
            async with self._hold([key], holder):
                yield
            return
        async with AsyncExitStack() as held:
            try:
                await held.enter_async_context(actor.hold(key, "pull"))
            except (ObjectBusy, RunSkipped):
                current_audit().refuse()
                raise
            yield

    async def _authorise(
        self,
        actor: Principal,
        environment_id: int,
        level: Literal["create", "administrator"],
    ) -> None:
        if level == "create":
            await self._authoriser.require_create(actor, environment_id)
        else:
            await self._authoriser.require_environment(actor, environment_id)
            require_administrator(actor)
        await self._hosts.environment(environment_id)

    async def _client(self, environment_id: int) -> EngineClient:
        try:
            return await self._hosts.client(environment_id)
        except EnvironmentUnavailable as error:
            current_audit().reason = error.kind
            raise

    async def _require_image(self, client: EngineClient, image_id: str) -> ImageInspect:
        audit = current_audit()
        try:
            return await inspect_image(client, image_id)
        except ResourceNotFound:
            audit.reason = "not_found"
        except ImageTooLarge:
            audit.reason = "too_large"
            raise
        except EnvironmentUnavailable as error:
            audit.reason = error.kind
            raise
        raise ImageGone()

    async def _current_holder(
        self, client: EngineClient, reference: ImageReference
    ) -> Holder | None:
        try:
            return await holder_of(client, reference)
        except ImageTooLarge:
            current_audit().reason = "too_large"
            raise
        except EnvironmentUnavailable as error:
            current_audit().reason = error.kind
            raise

    async def _recheck(
        self,
        client: EngineClient,
        image_id: str,
        keys: tuple[str, ...],
        in_use: int | None,
    ) -> None:
        """Refuse when the references, or the container count if given, changed after the lock."""
        audit = current_audit()
        again = await self._require_image(client, image_id)
        if tuple(daemon_references(again.repo_tags, again.repo_digests)) != keys:
            audit.reason = "references_changed"
            raise ImageChangedSinceReview()
        if in_use is not None and await self._count_in_use(client, image_id) != in_use:
            audit.reason = "in_use_changed"
            raise ImageChangedSinceReview()

    async def _count_in_use(self, client: EngineClient, image_id: str) -> int:
        try:
            containers = await read_containers(client)
        except EnvironmentUnavailable as error:
            current_audit().reason = error.kind
            raise
        except ObjectTooLarge:
            current_audit().reason = "too_large"
            raise
        return len(usage_of(containers).get(image_id, ()))

    async def _credentials_for(
        self,
        actor: Acting,
        environment_id: int,
        reference: ImageReference,
        choice: CredentialChoice,
        source: PullSource,
    ) -> PullCredentials:
        audit = current_audit()
        principal = actor if isinstance(actor, Principal) else None
        try:
            credentials = await self._credentials.resolve(
                environment_id,
                reference.key,
                principal=principal,
                choice=choice if source == "choice" and principal is not None else None,
            )
            if isinstance(actor, AutomationAuthority) and credentials.credential_set_id is not None:
                await actor.authorise_credential_set(credentials.credential_set_id)
        except BindingCredentialMissing:
            audit.reason = "credential_missing"
            raise
        except CredentialSetUnavailable:
            audit.reason = "credential_unavailable"
            raise
        except CredentialSetMismatch:
            audit.reason = "credential_mismatch"
            raise
        except CredentialChoiceRequired:
            audit.reason = "credential_choice_required"
            raise
        audit.detail.update(
            credential_set_id=credentials.credential_set_id,
            anonymous=credentials.credential_set_id is None,
            credential_source=credentials.source,
        )
        return credentials

    @asynccontextmanager
    async def _hold(self, keys: Sequence[ObjectKey], holder: LockHolder) -> AsyncIterator[None]:
        ordered = sorted(set(keys), key=lambda k: (k.object_type, k.object_id))
        try:
            async with AsyncExitStack() as stack:
                for key in ordered:
                    await stack.enter_async_context(self._locks.hold(key, holder))
                yield
        except ObjectBusy as busy:
            audit = current_audit()
            audit.reason = "object_busy"
            audit.detail["holder_action"] = busy.holder.action
            raise

    async def _pull_stream(
        self,
        client: EngineClient,
        environment_id: int,
        reference: ImageReference,
        credentials: PullCredentials,
        progress: ProgressSink | None,
        limit: float = PULL_OVERALL_SECONDS,
    ) -> None:
        started = False
        failure: EngineError | None = None
        timed_out = False
        try:
            async with asyncio.timeout(limit):
                if reference.digest is not None:
                    stream = client.images.pull(
                        f"{reference.repository}@{reference.digest}",
                        registry_auth=credentials.registry_auth,
                    )
                else:
                    stream = client.images.pull(
                        reference.repository,
                        tag=reference.tag,
                        registry_auth=credentials.registry_auth,
                    )
                async with aclosing(stream) as events:
                    async for event in events:
                        started = True
                        if progress is not None:
                            progress.on_pull(event)
        except TimeoutError:
            timed_out = True
        except EngineError as error:
            failure = error
        if timed_out:
            current_audit().reason = "outcome_unknown"
            raise EnvironmentUnavailable("outcome_unknown")
        if failure is not None:
            raise self._stream_failure(failure, environment_id, started=started, pushing=False)

    async def _push_stream(
        self,
        client: EngineClient,
        environment_id: int,
        reference: ImageReference,
        name: str,
        credentials: PullCredentials,
        progress: ProgressSink | None,
    ) -> str | None:
        started = False
        digest: str | None = None
        failure: EngineError | None = None
        timed_out = False
        repository, tag = _push_target(name, reference)
        try:
            async with asyncio.timeout(PULL_OVERALL_SECONDS):
                stream = client.images.push(
                    repository, tag=tag, registry_auth=credentials.registry_auth
                )
                async with aclosing(stream) as events:
                    async for event in events:
                        started = True
                        if progress is not None:
                            progress.on_push(event)
                        if event.aux is not None and event.aux.digest:
                            digest = event.aux.digest
        except TimeoutError:
            timed_out = True
        except EngineError as error:
            failure = error
        if timed_out:
            current_audit().reason = "outcome_unknown"
            raise EnvironmentUnavailable("outcome_unknown")
        if failure is not None:
            raise self._stream_failure(failure, environment_id, started=started, pushing=True)
        return digest

    def _stream_failure(
        self, error: EngineError, environment_id: int, *, started: bool, pushing: bool
    ) -> ProductError:
        audit = current_audit()
        logger.warning(
            "image_stream_failed",
            extra={
                "environment_id": environment_id,
                "operation": "push" if pushing else "pull",
                "error": type(error).__name__,
                "status": error.status_code,
            },
        )
        if isinstance(error, RequestOutcomeUnknown) or (
            isinstance(error, EnvironmentUnreachable) and started
        ):
            audit.reason = "outcome_unknown"
            return EnvironmentUnavailable("outcome_unknown")
        if isinstance(error, EnvironmentUnreachable):
            audit.reason = "unreachable"
            return EnvironmentUnavailable("unreachable")
        denied = isinstance(error, NotFound | EnginePermissionDenied)
        if pushing:
            audit.reason = "not_found_or_denied" if denied else "push_failed"
            return ImagePushFailed("not_found_or_denied" if denied else "push_failed")
        audit.reason = "not_found_or_denied" if denied else "pull_failed"
        return ImagePullFailed("not_found_or_denied" if denied else "pull_failed")

    async def _commit_binding(
        self,
        actor: Acting,
        environment_id: int,
        reference: ImageReference,
        credentials: PullCredentials,
        digest: str | None,
        image_id: str | None,
    ) -> RecordOutcome:
        audit = current_audit()
        async with self._db.transaction():
            outcome = await self._bindings.record_pull(
                actor if isinstance(actor, Principal) else actor_of(actor),
                environment_id,
                reference,
                credentials,
                digest,
            )
            audit.detail.update(digest=digest, binding=outcome)
            audit.target_id = image_id
        return outcome

    async def _remove_names(
        self,
        client: EngineClient,
        image_id: str,
        names: Sequence[str],
        expected: frozenset[str],
        force: bool,
        removal: _Removal,
        *,
        exact: bool,
    ) -> None:
        """Remove the image's references by the daemon's own text, then the image if untagged.

        `exact` is an untag: the one name must still be there and the image's. Otherwise a name
        already gone is skipped, and the image is removed by id once its names are.
        """
        full = f"sha256:{image_id}"
        if force and not exact:
            items = await client.images.remove(full, force=True, noprune=True)
            self._tally(removal, full, expected, items)
            return
        for name in names:
            try:
                inspected = await client.images.inspect(name)
            except NotFound:
                if exact:
                    raise self._moved(removal) from None
                continue
            if bare_image_id(inspected.id) != image_id:
                raise self._moved(removal)
            try:
                items = await client.images.remove(name, force=force, noprune=True)
            except NotFound:
                if exact:
                    raise self._moved(removal) from None
                continue
            self._tally(removal, full, expected, items)
            if removal.unexpected:
                raise self._moved(removal)
            if removal.deleted:
                return
        if not exact and not removal.deleted:
            try:
                items = await client.images.remove(full, force=False, noprune=True)
            except NotFound:
                return
            self._tally(removal, full, expected, items)

    @staticmethod
    def _moved(removal: _Removal) -> ImageChangedSinceReview:
        audit = current_audit()
        audit.reason = "references_changed"
        audit.detail.update(untagged=removal.untagged, deleted=removal.deleted)
        if removal.unexpected:
            audit.detail["unreviewed_untagged"] = removal.unexpected
        return (
            ImageChangedMidway()
            if removal.untagged or removal.deleted
            else ImageChangedSinceReview()
        )

    @staticmethod
    def _tally(
        removal: _Removal, full: str, expected: frozenset[str], items: Sequence[ImageDeleteItem]
    ) -> None:
        for item in items:
            if item.untagged is not None:
                removal.untagged += 1
                if _key_or_none(item.untagged) not in expected:
                    removal.unexpected += 1
            if item.deleted == full:
                removal.deleted = True

    async def _shielded[T](self, operation: _Operation, call: Callable[[], Awaitable[T]]) -> T:
        task: asyncio.Future[T] = asyncio.ensure_future(call())
        task.add_done_callback(retrieve_exception)
        failure: EngineError | None = None
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._record_interrupted(operation, task)
            raise
        except EngineError as error:
            failure = error
        raise await self._engine_failure(operation, failure)

    async def _engine_failure(self, operation: _Operation, error: EngineError) -> ProductError:
        audit = current_audit()
        logger.warning(
            "image_action_refused",
            extra={
                "environment_id": operation.environment_id,
                "operation": operation.action,
                "error": type(error).__name__,
                "status": error.status_code,
            },
        )
        if isinstance(error, NotFound):
            audit.reason = "not_found"
            return ImageGone()
        if isinstance(error, Conflict) and operation.removing:
            return await self._conflict(operation)
        if isinstance(error, RequestOutcomeUnknown):
            audit.reason = "outcome_unknown"
            return EnvironmentUnavailable("outcome_unknown")
        if isinstance(error, EnvironmentUnreachable):
            audit.reason = "unreachable"
            return EnvironmentUnavailable("unreachable")
        audit.reason = "engine_error"
        return EngineRefused(engine_detail(error))

    async def _conflict(self, operation: _Operation) -> ProductError:
        audit = current_audit()
        in_use = 0
        try:
            client = await self._hosts.client(operation.environment_id)
            in_use = len(usage_of(await read_containers(client)).get(operation.target_id or "", ()))
        except ProductError:
            in_use = 0
        if in_use:
            audit.reason = "image_in_use"
            return ImageInUse(in_use)
        audit.reason = "image_has_dependents"
        return ImageHasDependents()

    async def _record_interrupted(self, operation: _Operation, task: asyncio.Future[Any]) -> None:
        audit = current_audit()
        with anyio.CancelScope(shield=True):
            done, _ = await asyncio.wait({task}, timeout=SETTLE_SECONDS)
            detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
            if operation.removal is not None:
                detail.update(
                    untagged=operation.removal.untagged, deleted=operation.removal.deleted
                )
            failure = "outcome_unknown"
            if task in done and not task.cancelled():
                error = task.exception()
                if error is None:
                    failure = ""
                elif isinstance(error, NotFound):
                    failure = "not_found"
                elif isinstance(error, Conflict):
                    failure = "conflict"
                elif isinstance(error, RequestOutcomeUnknown):
                    failure = "outcome_unknown"
                elif isinstance(error, EnvironmentUnreachable):
                    failure = "unreachable"
                else:
                    failure = "engine_error"
            await self._write_interrupted(
                operation.actor,
                operation.action,
                operation.environment_id,
                operation.target_id,
                detail,
                failure=failure,
            )

    async def _write_interrupted(
        self,
        actor: Acting,
        action: str,
        environment_id: int,
        target_id: str | None,
        detail: Mapping[str, DetailValue],
        *,
        failure: str = "outcome_unknown",
    ) -> None:
        if current_audit().committed:
            return
        user = actor_of(actor)
        body: dict[str, DetailValue] = {**detail, "interrupted": True}
        try:
            with anyio.CancelScope(shield=True):
                async with self._database.session() as db:
                    recorder = ActivityRecorder(db, self._clock)
                    if failure:
                        await recorder.record_failure(
                            user,
                            action=action,
                            target_type="image",
                            target_id=target_id,
                            environment_id=environment_id,
                            reason=failure,
                            detail=body,
                            message=str(EnvironmentUnavailable("outcome_unknown"))
                            if failure == "outcome_unknown"
                            else None,
                        )
                    else:
                        await recorder.record_event(
                            user,
                            action=action,
                            target_type="image",
                            target_id=target_id,
                            environment_id=environment_id,
                            result="success",
                            detail=body,
                        )
        except Exception as error:
            logger.error(
                "activity_event_write_failed",
                extra={
                    "action": action,
                    "actor_user_id": actor.user_id if isinstance(actor, Principal) else None,
                    "target_type": "image",
                    "environment_id": environment_id,
                    "error": type(error).__name__,
                },
            )


def _key_or_none(text: str) -> str | None:
    try:
        return parse_reference(text).key
    except ReferenceInvalid:
        return None


def _push_target(name: str, reference: ImageReference) -> tuple[str, str]:
    try:
        parts = split_reference(name)
    except ReferenceInvalid:
        return reference.repository, reference.tag or DEFAULT_TAG
    repository = f"{parts.domain}/{parts.path}" if parts.domain is not None else parts.path
    return repository, parts.tag or DEFAULT_TAG


def _expected_replaced(text: str) -> str | None:
    if text == NO_REPLACED_IMAGE:
        return None
    if IMAGE_ID.fullmatch(text) is None:
        raise ValidationFailed({"image": CHANGED_SINCE_REVIEW})
    return text
