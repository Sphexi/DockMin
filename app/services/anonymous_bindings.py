"""Bind unbound images anonymously in bulk: a read-only review, then one confirmed apply."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final

from app.auth.errors import ProductError, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.image_binding import ImageBindingRepository
from app.db.repositories.registry_access import OfferedCredentialSet, RegistryAccessRepository
from app.logging import get_logger
from app.registry_client.manifests import ManifestCheckKind, check_message
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
    plan_line,
)
from app.services.anonymous_binding_store import AnonymousBindingReviewStore, ResumePoint
from app.services.authorisation import Authoriser
from app.services.binding_candidates import BindingCandidateScanner, ReferenceOutcome, local_digests
from app.services.guards import require_administrator
from app.services.image_bindings import (
    ImageBindingService,
    canonical_keys,
    resolve_live_references,
)
from app.services.image_jobs import JobProgress
from app.services.image_listing import read_host_listing, read_images
from app.services.object_inspector import HostClients
from app.services.pull_credentials import CredentialChoice

logger = get_logger(__name__)

CHECKING: Final = "checking_images"
NO_REGISTRY_CHOSEN: Final = "Confirm at least one registry."
UNKNOWN_REGISTRY: Final = "That registry is not in the review."
REASON_MISMATCH: Final = "mismatch"
REASON_NOT_CHECKED: Final = "not_checked"
FAILED_MESSAGE: Final = "The operation failed."
MISMATCH_MESSAGE: Final = "The registry holds a different digest for this tag."
NOT_CHECKED_MESSAGE: Final = "Not checked: the limit on references or the time budget was reached."
CHECK_FAILED_MESSAGE: Final = "The check failed."
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
        """Check the tagged references of local images that have no binding; administrators only.

        References used by containers come first, then the rest, each in key order, starting
        after the position the user's previous review of the environment reached. At most the
        scanner's limit are checked; the others are counted as not checked.
        EnvironmentUnavailable and ImageListTooLarge when the host's images cannot be read in
        full. Pinned references, images with no recorded digest, and references at an address
        with no granted entry that reads as anonymous are listed, not checked.
        """
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        images, containers = await read_host_listing(client)
        in_use = (await resolve_live_references(client, images, containers)).containers
        if progress is not None:
            progress.on_stage(CHECKING)
        entries = await self._access.anonymous_entries(environment_id)
        plain = _plain_entries(entries)
        own_tls = {entry.url for entry in entries} - plain.keys()
        keys = sorted({key for image in images for key in canonical_keys(image.repo_tags)})
        stored = await self._rows.for_references(environment_id, keys)
        local = local_digests(images)
        left_out: list[LeftOutImage] = []
        candidates: list[_Candidate] = []
        for key in keys:
            if key in stored:
                continue
            reference = _parsed(key)
            if reference is None:
                continue
            digests = local.get(key, ())
            reason = _left_out_reason(reference, digests, plain.keys(), own_tls)
            if reason is not None:
                left_out.append(LeftOutImage(key, reference.familiar, reason))
            else:
                candidates.append(_Candidate((0 if key in in_use else 1, key), reference, digests))
        candidates.sort(key=lambda candidate: candidate.position)
        resume = self._reviews.resume_point(actor.user_id, environment_id)
        ordered = _from_resume(candidates, resume)
        chosen, beyond = (
            ordered[: self._scanner.check_limit],
            len(ordered) - self._scanner.check_limit,
        )
        outcomes = await self._scanner.check_anonymously(
            actor,
            environment_id,
            [(candidate.reference, candidate.digests) for candidate in chosen],
            plain.keys(),
        )
        not_checked = max(beyond, 0) + sum(
            1 for found in outcomes if found.outcome == "not_checked"
        )
        self._remember_position(actor, environment_id, chosen, outcomes, not_checked)
        counts = await self._access.authenticated_set_counts(environment_id)
        groups = _groups_of(outcomes, plain, counts)
        left_out.sort(key=lambda item: (item.reason, item.reference))
        logger.info(
            "anonymous_binding_reviewed",
            extra={
                "environment_id": environment_id,
                "checked": len(outcomes),
                "matched": sum(len(group.matched) for group in groups),
                "not_checked": not_checked,
                "left_out": len(left_out),
            },
        )
        return AnonymousBindingReview(
            environment_id=environment_id,
            environment_name=environment.name,
            groups=groups,
            left_out=tuple(left_out[:MAX_LEFT_OUT_SHOWN]),
            left_out_total=len(left_out),
            not_checked_total=not_checked,
        )

    def _remember_position(
        self,
        actor: Principal,
        environment_id: int,
        chosen: Sequence["_Candidate"],
        outcomes: Sequence[ReferenceOutcome],
        not_checked: int,
    ) -> None:
        if not_checked == 0:
            self._reviews.set_resume_point(actor.user_id, environment_id, None)
            return
        settled = {found.reference for found in outcomes if found.outcome != "not_checked"}
        done = 0
        for candidate in chosen:
            if candidate.reference.key not in settled:
                break
            done += 1
        if done:
            self._reviews.set_resume_point(actor.user_id, environment_id, chosen[done - 1].position)

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
            await self._require_same_sets(environment_id, review, chosen)
            try:
                items = await self._bind(actor, environment_id, review, chosen)
            finally:
                self._reviews.replace(token, review.without(chosen))
        finally:
            self._reviews.end_apply(token)
        return AnonymousBindingResult(environment_id, review.environment_name, chosen, items)

    async def _current_lines(
        self, environment_id: int, review: AnonymousBindingReview, chosen: Sequence[str]
    ) -> tuple[str, ...]:
        client = await self._hosts.client(environment_id)
        local = local_digests(await read_images(client))
        configured = _plain_entries(await self._access.anonymous_entries(environment_id)).keys()
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
                plan_line(item.reference, item.digest)
                for address in chosen
                if address in configured and (group := review.group(address)) is not None
                for item in group.matched
                if item.reference not in stored and item.digest in local.get(item.reference, ())
            )
        )

    async def _require_same_sets(
        self, environment_id: int, review: AnonymousBindingReview, chosen: Sequence[str]
    ) -> None:
        counts = await self._access.authenticated_set_counts(environment_id)
        for address in chosen:
            group = review.group(address)
            if group is not None and counts.get(address, 0) != group.authenticated_sets:
                raise AnonymousBindingChanged()

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
        except Exception as error:
            logger.error("anonymous_binding_write_failed", extra={"error": type(error).__name__})
            return BoundItem(item.reference, item.display, address, "failed", FAILED_MESSAGE)
        if outcome == "skipped":
            return BoundItem(
                item.reference, item.display, address, "skipped", "It was bound in the meantime."
            )
        return BoundItem(
            item.reference, item.display, address, "bound", "Bound anonymously; nothing was pulled."
        )


@dataclass(frozen=True, slots=True)
class _Candidate:
    position: ResumePoint
    reference: ImageReference
    digests: tuple[str, ...]


def _parsed(key: str) -> ImageReference | None:
    try:
        return parse_reference(key)
    except ReferenceInvalid:
        return None


def _plain_entries(
    entries: Sequence[OfferedCredentialSet],
) -> dict[str, list[OfferedCredentialSet]]:
    found: dict[str, list[OfferedCredentialSet]] = {}
    for entry in entries:
        if entry.reads_as_anonymous:
            found.setdefault(entry.url, []).append(entry)
    return found


def _from_resume(candidates: list[_Candidate], resume: ResumePoint | None) -> list[_Candidate]:
    if resume is None:
        return candidates
    after = [candidate for candidate in candidates if candidate.position > resume]
    before = [candidate for candidate in candidates if candidate.position <= resume]
    return [*after, *before]


def _left_out_reason(
    reference: ImageReference,
    digests: tuple[str, ...],
    plain: Collection[str],
    own_tls: Collection[str],
) -> LeftOutReason | None:
    if reference.pinned:
        return "pinned"
    if not digests:
        return "no_local_digest"
    if reference.registry_address in plain:
        return None
    return "entry_tls_settings" if reference.registry_address in own_tls else "no_anonymous_entry"


def _groups_of(
    outcomes: Sequence[ReferenceOutcome],
    plain: dict[str, list[OfferedCredentialSet]],
    authenticated: dict[str, int],
) -> tuple[RegistryGroup, ...]:
    matched: dict[str, list[MatchedImage]] = {}
    unmatched: dict[str, list[UnmatchedImage]] = {}
    unmatched_total: dict[str, int] = {}
    for outcome in sorted(outcomes, key=lambda found: found.reference):
        address = parse_reference(outcome.reference).registry_address
        match = outcome.matches[0] if outcome.matches else None
        if outcome.outcome == "binds" and match is not None and match.digest is not None:
            matched.setdefault(address, []).append(
                MatchedImage(outcome.reference, outcome.display, match.digest)
            )
            continue
        unmatched_total[address] = unmatched_total.get(address, 0) + 1
        rows = unmatched.setdefault(address, [])
        if len(rows) < MAX_LEFT_OUT_SHOWN:
            reason = _reason_of(outcome)
            rows.append(
                UnmatchedImage(outcome.reference, outcome.display, reason, _message_of(reason))
            )
    return tuple(
        RegistryGroup(
            address=address,
            entries=tuple(entry.name for entry in entries),
            authenticated_sets=authenticated.get(address, 0),
            matched=tuple(matched.get(address, ())),
            unmatched=tuple(unmatched.get(address, ())),
            unmatched_total=unmatched_total.get(address, 0),
        )
        for address, entries in sorted(plain.items())
        if address in matched or address in unmatched
    )


def _reason_of(outcome: ReferenceOutcome) -> str:
    if outcome.outcome == "not_checked":
        return REASON_NOT_CHECKED
    check = outcome.tried[0] if outcome.tried else None
    if check is None or check.status == "mismatch":
        return REASON_MISMATCH
    return check.error or REASON_MISMATCH


def _message_of(reason: str) -> str:
    match reason:
        case "mismatch":
            return MISMATCH_MESSAGE
        case "not_checked":
            return NOT_CHECKED_MESSAGE
    try:
        return check_message(ManifestCheckKind(reason))
    except ValueError:
        return CHECK_FAILED_MESSAGE
