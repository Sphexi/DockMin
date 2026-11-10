"""Bind unbound images anonymously in bulk: a read-only review, then one confirmed apply."""

from collections.abc import Sequence
from typing import Final

from app.auth.errors import ProductError, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.image_binding import ImageBindingRepository
from app.db.repositories.registry_access import OfferedCredentialSet, RegistryAccessRepository
from app.logging import get_logger
from app.registry_client.references import ImageReference, ReferenceInvalid, parse_reference
from app.services.anonymous_binding_plan import (
    CHANGED_MESSAGE,
    MAX_LEFT_OUT_SHOWN,
    AnonymousBindingResult,
    AnonymousBindingReview,
    BoundItem,
    LeftOutImage,
    LeftOutReason,
    MatchedImage,
    RegistryGroup,
    UnmatchedImage,
    plan_digest,
)
from app.services.anonymous_binding_store import AnonymousBindingReviewStore
from app.services.authorisation import Authoriser
from app.services.binding_candidates import BindingCandidateScanner, ReferenceOutcome, local_digests
from app.services.guards import require_administrator
from app.services.image_bindings import ImageBindingService, canonical_keys
from app.services.image_jobs import JobProgress
from app.services.image_listing import read_images
from app.services.object_inspector import HostClients
from app.services.pull_credentials import CredentialChoice

logger = get_logger(__name__)

CHECKING: Final = "checking_images"
NO_REGISTRY_CHOSEN: Final = "Confirm at least one registry."
UNKNOWN_REGISTRY: Final = "That registry is not in the review."
REASON_MISMATCH: Final = "mismatch"
REASON_NOT_CHECKED: Final = "not_checked"
FAILED_MESSAGE: Final = "The operation failed."
_ANONYMOUS: Final = CredentialChoice(None)


class AnonymousBindingChanged(ValidationFailed):
    """The plan recomputed now differs from the one confirmed; nothing was written."""

    reason = "changed_since_review"

    def __init__(self) -> None:
        super().__init__({"plan": CHANGED_MESSAGE})


class AnonymousReviewExpired(ProductError):
    """The review is gone: it expired, was applied, or was never this user's."""


class AnonymousReviewBusy(ProductError):
    """The review is already being applied."""


class AnonymousBindingService:
    """Administrator-only bulk anonymous binding of one environment's unbound images.

    `review` reads the host's image list and sends one anonymous manifest HEAD per reference at
    an address with a granted entry that has authentication off; it writes nothing. `apply` binds
    the references of the confirmed registries through `bind_verified`, each as its own audited
    operation, and neither pulls nor writes to the host.
    """

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        hosts: HostClients,
        scanner: BindingCandidateScanner,
        bindings: ImageBindingService,
        binding_rows: ImageBindingRepository,
        access: RegistryAccessRepository,
        reviews: AnonymousBindingReviewStore,
    ) -> None:
        self._authoriser = authoriser
        self._hosts = hosts
        self._scanner = scanner
        self._bindings = bindings
        self._rows = binding_rows
        self._access = access
        self._reviews = reviews

    async def review(
        self, actor: Principal, environment_id: int, progress: JobProgress | None = None
    ) -> AnonymousBindingReview:
        """Check every tagged reference of a local image that has no binding; administrators only.

        EnvironmentUnavailable and ImageListTooLarge when the host's images cannot be read in
        full. Pinned references and images with no recorded digest are listed, not checked, and so
        are references at an address with no granted entry that has authentication off.
        """
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        images = await read_images(client)
        if progress is not None:
            progress.on_stage(CHECKING)
        entries = await self._access.anonymous_entries(environment_id)
        by_address: dict[str, list[OfferedCredentialSet]] = {}
        for entry in entries:
            by_address.setdefault(entry.url, []).append(entry)
        keys = sorted({key for image in images for key in canonical_keys(image.repo_tags)})
        stored = await self._rows.for_references(environment_id, keys)
        local = local_digests(images)
        left_out: list[LeftOutImage] = []
        checkable: list[tuple[ImageReference, tuple[str, ...]]] = []
        for key in keys:
            if key in stored:
                continue
            reference = _parsed(key)
            if reference is None:
                continue
            digests = local.get(key, ())
            reason = _left_out_reason(reference, digests, by_address)
            if reason is not None:
                left_out.append(LeftOutImage(key, reference.familiar, reason))
            else:
                checkable.append((reference, digests))
        outcomes = await self._scanner.check_anonymously(actor, environment_id, checkable)
        counts = await self._access.authenticated_set_counts(environment_id)
        groups = _groups_of(outcomes, by_address, counts)
        left_out.sort(key=lambda item: (item.reason, item.reference))
        logger.info(
            "anonymous_binding_reviewed",
            extra={
                "environment_id": environment_id,
                "checked": len(outcomes),
                "matched": sum(len(group.matched) for group in groups),
                "left_out": len(left_out),
            },
        )
        return AnonymousBindingReview(
            environment_id=environment_id,
            environment_name=environment.name,
            groups=groups,
            left_out=tuple(left_out[:MAX_LEFT_OUT_SHOWN]),
            left_out_total=len(left_out),
        )

    async def apply(
        self,
        actor: Principal,
        environment_id: int,
        token: str,
        registries: Sequence[str],
        expected_plan: str,
    ) -> AnonymousBindingResult:
        """Bind the matched references of the confirmed registries anonymously.

        AnonymousReviewExpired when the review is gone, AnonymousReviewBusy while it is being
        applied, ValidationFailed for no registry or one the review does not hold, and
        AnonymousBindingChanged, before anything is written, when the plan recomputed against the
        host and the binding rows now differs from `expected_plan`. A reference bound meanwhile
        is skipped; a binding the write refuses is reported and the others proceed.
        """
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        review = self._reviews.get(token, actor.user_id, environment_id)
        if review is None:
            raise AnonymousReviewExpired()
        chosen = tuple(dict.fromkeys(registries))
        if not chosen:
            raise ValidationFailed({"registries": NO_REGISTRY_CHOSEN})
        if any(review.group(address) is None for address in chosen):
            raise ValidationFailed({"registries": UNKNOWN_REGISTRY})
        if not self._reviews.begin_apply(token, actor.user_id, environment_id):
            raise AnonymousReviewBusy()
        try:
            lines = await self._current_lines(environment_id, review, chosen)
            if plan_digest(lines) != expected_plan or lines != review.plan_lines(chosen):
                raise AnonymousBindingChanged()
            items = await self._bind(actor, environment_id, review, chosen)
        finally:
            self._reviews.end_apply(token)
        self._reviews.replace(token, review.without(chosen))
        return AnonymousBindingResult(environment_id, review.environment_name, chosen, items)

    async def _current_lines(
        self, environment_id: int, review: AnonymousBindingReview, chosen: Sequence[str]
    ) -> tuple[str, ...]:
        client = await self._hosts.client(environment_id)
        local = local_digests(await read_images(client))
        entries = await self._access.anonymous_entries(environment_id)
        configured = {entry.url for entry in entries}
        matched = [
            item
            for address in chosen
            if (group := review.group(address)) is not None
            for item in group.matched
        ]
        stored = await self._rows.for_references(
            environment_id, [item.reference for item in matched]
        )
        return tuple(
            sorted(
                f"image {item.reference} anonymous {item.digest}"
                for address in chosen
                if address in configured and (group := review.group(address)) is not None
                for item in group.matched
                if item.reference not in stored and item.digest in local.get(item.reference, ())
            )
        )

    async def _bind(
        self,
        actor: Principal,
        environment_id: int,
        review: AnonymousBindingReview,
        chosen: Sequence[str],
    ) -> tuple[BoundItem, ...]:
        items: list[BoundItem] = []
        for address in chosen:
            group = review.group(address)
            if group is None:
                continue
            for item in sorted(group.matched, key=lambda matched: matched.reference):
                items.append(await self._bind_one(actor, environment_id, address, item))
        return tuple(items)

    async def _bind_one(
        self, actor: Principal, environment_id: int, address: str, item: MatchedImage
    ) -> BoundItem:
        try:
            outcome = await self._bindings.bind_verified(
                actor,
                environment_id,
                item.reference,
                _ANONYMOUS,
                digest=item.digest,
                bulk=True,
            )
        except ProductError as error:
            message = str(error) if isinstance(error, ValidationFailed) else FAILED_MESSAGE
            return BoundItem(item.reference, item.display, address, "failed", message)
        if outcome == "skipped":
            return BoundItem(
                item.reference, item.display, address, "skipped", "It was bound in the meantime."
            )
        return BoundItem(
            item.reference, item.display, address, "bound", "Bound anonymously; nothing was pulled."
        )


def _parsed(key: str) -> ImageReference | None:
    try:
        return parse_reference(key)
    except ReferenceInvalid:
        return None


def _left_out_reason(
    reference: ImageReference,
    digests: tuple[str, ...],
    by_address: dict[str, list[OfferedCredentialSet]],
) -> LeftOutReason | None:
    if reference.pinned:
        return "pinned"
    if not digests:
        return "no_local_digest"
    if reference.registry_address not in by_address:
        return "no_anonymous_entry"
    return None


def _groups_of(
    outcomes: Sequence[ReferenceOutcome],
    by_address: dict[str, list[OfferedCredentialSet]],
    authenticated: dict[str, int],
) -> tuple[RegistryGroup, ...]:
    matched: dict[str, list[MatchedImage]] = {}
    unmatched: dict[str, list[UnmatchedImage]] = {}
    for outcome in sorted(outcomes, key=lambda found: found.reference):
        address = parse_reference(outcome.reference).registry_address
        match = outcome.matches[0] if outcome.matches else None
        if outcome.outcome == "binds" and match is not None and match.digest is not None:
            matched.setdefault(address, []).append(
                MatchedImage(outcome.reference, outcome.display, match.digest)
            )
        else:
            unmatched.setdefault(address, []).append(
                UnmatchedImage(outcome.reference, outcome.display, _reason_of(outcome))
            )
    return tuple(
        RegistryGroup(
            address=address,
            entries=tuple(entry.name for entry in entries),
            authenticated_sets=authenticated.get(address, 0),
            matched=tuple(matched.get(address, ())),
            unmatched=tuple(unmatched.get(address, ())),
        )
        for address, entries in sorted(by_address.items())
        if address in matched or address in unmatched
    )


def _reason_of(outcome: ReferenceOutcome) -> str:
    if outcome.outcome == "not_checked":
        return REASON_NOT_CHECKED
    check = outcome.tried[0] if outcome.tried else None
    if check is None or check.status == "mismatch":
        return REASON_MISMATCH
    return check.error or REASON_MISMATCH
