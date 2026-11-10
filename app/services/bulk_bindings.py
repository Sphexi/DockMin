"""Bind unbound images to credential sets in bulk: a read-only review, then confirmed applies."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from app.auth.errors import ProductError, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.image_binding import ImageBindingRepository
from app.db.repositories.registry_access import OfferedCredentialSet, RegistryAccessRepository
from app.logging import get_logger
from app.registry_client.manifests import ManifestCheckKind, check_message
from app.registry_client.references import ImageReference, ReferenceInvalid, parse_reference
from app.services.authorisation import Authoriser
from app.services.binding_candidates import (
    BindingCandidateScanner,
    CandidateCheck,
    ReferenceOutcome,
    local_digests,
)
from app.services.bulk_binding_plan import (
    MAX_LEFT_OUT_SHOWN,
    BindableImage,
    BoundItem,
    BulkBindingResult,
    BulkBindingReview,
    LeftOutImage,
    LeftOutReason,
    OtherImage,
    RegistryGroup,
    SetOption,
    SetRef,
    plan_digest,
)
from app.services.bulk_binding_store import BulkBindingReviewStore, ResumePoint
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
PLAN_NOT_OFFERED: Final = "The confirmed plan is not the one the review offered. Reload the review."
CHOICE_NOT_OFFERED: Final = "That credential set is not one the review offered for the image."
FAILED_MESSAGE: Final = "The operation failed."
MISMATCH_MESSAGE: Final = "The registry holds a different digest for this tag."
NOT_CHECKED_MESSAGE: Final = "Not checked: the limit on references or the time budget was reached."
CHECK_FAILED_MESSAGE: Final = "The check failed."
WORKS_MESSAGE: Final = "Works without binding."
NEEDS_SET_MESSAGE: Final = "Needs a credential set."
LEFT_MESSAGE: Final = "No credential set was chosen; it stays in the review."
BOUND_MEANWHILE: Final = "It was bound in the meantime."


class BulkReviewExpired(ProductError):
    """The review is gone: it expired, or was never this user's."""


class BulkReviewBusy(ProductError):
    """The review is already being applied."""


class BulkBindingService:
    """Administrator-only bulk binding of one environment's unbound images to credential sets.

    `review` reads the host's image list and sends one manifest HEAD per granted set for each
    tagged, unbound reference at an address that has one, then one anonymous HEAD when no set
    matched; it writes nothing. `apply` binds the references of the confirmed registries through
    `bind_verified`, each as its own audited operation, and neither pulls nor writes to the host.
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
        reviews: BulkBindingReviewStore,
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
    ) -> BulkBindingReview:
        """Check the tagged references of local images that have no binding; administrators only.

        References used by containers come first, then the rest, each in key order, starting
        after the position the user's previous review of the environment reached. At most the
        scanner's limit are checked; the others are counted as not checked.
        EnvironmentUnavailable and ImageListTooLarge when the host's images cannot be read in
        full. Pinned references, images with no recorded digest, and references at an address
        with no granted credential set are listed, not checked.
        """
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        images, containers = await read_host_listing(client)
        in_use = (await resolve_live_references(client, images, containers)).containers
        if progress is not None:
            progress.on_stage(CHECKING)
        keys = sorted({key for image in images for key in canonical_keys(image.repo_tags)})
        stored = await self._rows.for_references(environment_id, keys)
        local = local_digests(images)
        unbound = [
            reference
            for key in keys
            if key not in stored and (reference := _parsed(key)) is not None
        ]
        at_address = _by_address(
            await self._access.separate_sets(
                environment_id, {reference.registry_address for reference in unbound}
            )
        )
        left_out: list[LeftOutImage] = []
        candidates: list[_Candidate] = []
        for reference in unbound:
            key = reference.key
            digests = local.get(key, ())
            reason = _left_out_reason(reference, digests, at_address)
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
        outcomes = await self._scanner.check_references(
            actor,
            environment_id,
            [(candidate.reference, candidate.digests) for candidate in chosen],
        )
        not_checked = max(beyond, 0) + sum(
            1 for found in outcomes if found.outcome == "not_checked"
        )
        self._remember_position(actor, environment_id, chosen, outcomes, not_checked)
        groups = _groups_of(outcomes, at_address)
        left_out.sort(key=lambda item: (item.reason, item.reference))
        logger.info(
            "bulk_binding_reviewed",
            extra={
                "environment_id": environment_id,
                "checked": len(outcomes),
                "bindable": sum(len(group.bindable) for group in groups),
                "not_checked": not_checked,
                "left_out": len(left_out),
            },
        )
        return BulkBindingReview(
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
        expected_plans: Sequence[str],
        choices: Mapping[str, int | None] | None = None,
    ) -> BulkBindingResult:
        """Bind what the confirmed registries offered; each registry stands or falls on its own.

        `expected_plans` pairs with `registries` by position, and `choices` maps a reference the
        review offered a pick for to the set chosen, None to leave it unbound. BulkReviewExpired
        when the review is gone, BulkReviewBusy while it is being applied, and ValidationFailed,
        before anything is written, for no registry, one the review does not hold, no registry
        whose plan is the one it offered, or a choice outside what it offered. A registry whose
        plan, host digests or granted sets differ from the review is reported as changed and left
        in the review while the others apply. A reference bound meanwhile is skipped and the rest
        of its registry applies; a binding the write refuses is reported and the others proceed.
        EnvironmentUnavailable, with the review kept, when the host's images cannot be read.
        """
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        review = self._reviews.get(token, actor.user_id, environment_id)
        if review is None:
            raise BulkReviewExpired()
        picked = choices or {}
        groups, stale = _confirmed(review, registries, expected_plans)
        _require_offered_choices(review, picked)
        if not self._reviews.begin_apply(token, actor.user_id, environment_id):
            raise BulkReviewBusy()
        try:
            client = await self._hosts.client(environment_id)
            local = local_digests(await read_images(client))
            current = _by_address(
                await self._access.separate_sets(
                    environment_id, {group.address for group in groups}
                )
            )
            items: list[BoundItem] = []
            changed: list[str] = list(stale)
            try:
                for group in groups:
                    remaining, meanwhile = await self._reconcile(
                        environment_id, group, local, current
                    )
                    items.extend(meanwhile)
                    if remaining is None:
                        changed.append(group.address)
                    else:
                        items.extend(
                            await self._bind_group(actor, environment_id, remaining, picked)
                        )
            finally:
                done = {item.reference for item in items if item.status in ("bound", "skipped")}
                result = BulkBindingResult(
                    environment_id,
                    review.environment_name,
                    tuple(registries),
                    tuple(items),
                    tuple(changed),
                )
                self._reviews.replace(token, review.settled(done), result)
        finally:
            self._reviews.end_apply(token)
        return result

    async def _reconcile(
        self,
        environment_id: int,
        group: RegistryGroup,
        local: Mapping[str, tuple[str, ...]],
        current: Mapping[str, list[OfferedCredentialSet]],
    ) -> tuple[RegistryGroup | None, list[BoundItem]]:
        """Return what of the group still applies, None when it changed, and what was bound since.

        A reference bound meanwhile is reported skipped and left out of the comparison, so it
        does not make the rest of the registry look changed.
        """
        if sorted(entry.id for entry in current.get(group.address, ())) != sorted(
            entry.set_id for entry in group.sets
        ):
            return None, []
        stored = await self._rows.for_references(
            environment_id, [item.reference for item in group.bindable]
        )
        meanwhile = [
            BoundItem(item.reference, item.display, group.address, "skipped", BOUND_MEANWHILE)
            for item in group.bindable
            if item.reference in stored
        ]
        remaining = group.without(stored)
        holds = all(
            option.digest in local.get(item.reference, ())
            for item in remaining.bindable
            for option in item.options
        )
        return (remaining if holds else None), meanwhile

    async def _bind_group(
        self,
        actor: Principal,
        environment_id: int,
        group: RegistryGroup,
        picked: Mapping[str, int | None],
    ) -> list[BoundItem]:
        items: list[BoundItem] = []
        for image in sorted(group.bindable, key=lambda offered: offered.reference):
            if not image.pick:
                (option,) = image.options
            else:
                set_id = picked.get(image.reference)
                found = None if set_id is None else image.option(set_id)
                if found is None:
                    items.append(
                        BoundItem(
                            image.reference, image.display, group.address, "left", LEFT_MESSAGE
                        )
                    )
                    continue
                option = found
            items.append(await self._bind_one(actor, environment_id, group.address, image, option))
        return items

    async def _bind_one(
        self,
        actor: Principal,
        environment_id: int,
        address: str,
        image: BindableImage,
        option: SetOption,
    ) -> BoundItem:
        try:
            outcome = await self._bindings.bind_verified(
                actor,
                environment_id,
                image.reference,
                CredentialChoice(option.set_id),
                digest=option.digest,
                bulk=True,
            )
        except ProductError as error:
            return BoundItem(image.reference, image.display, address, "failed", str(error))
        except Exception as error:
            logger.error("bulk_binding_write_failed", extra={"error": type(error).__name__})
            return BoundItem(image.reference, image.display, address, "failed", FAILED_MESSAGE)
        if outcome == "skipped":
            return BoundItem(image.reference, image.display, address, "skipped", BOUND_MEANWHILE)
        return BoundItem(
            image.reference,
            image.display,
            address,
            "bound",
            f"Bound to {option.name}; nothing was pulled.",
        )


@dataclass(frozen=True, slots=True)
class _Candidate:
    position: ResumePoint
    reference: ImageReference
    digests: tuple[str, ...]


def _confirmed(
    review: BulkBindingReview, registries: Sequence[str], expected_plans: Sequence[str]
) -> tuple[tuple[RegistryGroup, ...], tuple[str, ...]]:
    """Return the confirmed groups and the addresses whose plan is no longer the review's."""
    if not registries:
        raise ValidationFailed({"registries": NO_REGISTRY_CHOSEN})
    if len(registries) != len(expected_plans) or len(set(registries)) != len(registries):
        raise ValidationFailed({"plan": PLAN_NOT_OFFERED})
    groups: list[RegistryGroup] = []
    stale: list[str] = []
    for address, expected in zip(registries, expected_plans, strict=True):
        group = review.group(address)
        if group is None or not group.bindable:
            raise ValidationFailed({"registries": UNKNOWN_REGISTRY})
        if plan_digest(group.plan_lines) != expected:
            stale.append(address)
        else:
            groups.append(group)
    if not groups:
        raise ValidationFailed({"plan": PLAN_NOT_OFFERED})
    return tuple(groups), tuple(stale)


def _require_offered_choices(review: BulkBindingReview, picked: Mapping[str, int | None]) -> None:
    for reference, set_id in picked.items():
        image = review.image(reference)
        if image is None or not image.pick:
            raise ValidationFailed({"choices": CHOICE_NOT_OFFERED})
        if set_id is not None and image.option(set_id) is None:
            raise ValidationFailed({"choices": CHOICE_NOT_OFFERED})


def _parsed(key: str) -> ImageReference | None:
    try:
        return parse_reference(key)
    except ReferenceInvalid:
        return None


def _by_address(sets: Sequence[OfferedCredentialSet]) -> dict[str, list[OfferedCredentialSet]]:
    found: dict[str, list[OfferedCredentialSet]] = {}
    for entry in sets:
        found.setdefault(entry.url, []).append(entry)
    return found


def _from_resume(candidates: list[_Candidate], resume: ResumePoint | None) -> list[_Candidate]:
    if resume is None:
        return candidates
    after = [candidate for candidate in candidates if candidate.position > resume]
    before = [candidate for candidate in candidates if candidate.position <= resume]
    return [*after, *before]


def _left_out_reason(
    reference: ImageReference, digests: tuple[str, ...], addresses: Collection[str]
) -> LeftOutReason | None:
    if reference.pinned:
        return "pinned"
    if not digests:
        return "no_local_digest"
    return None if reference.registry_address in addresses else "no_credential_set"


def _groups_of(
    outcomes: Sequence[ReferenceOutcome], at_address: Mapping[str, list[OfferedCredentialSet]]
) -> tuple[RegistryGroup, ...]:
    bindable: dict[str, list[BindableImage]] = {}
    others: dict[str, list[OtherImage]] = {}
    others_total: dict[str, int] = {}
    for outcome in sorted(outcomes, key=lambda found: found.reference):
        address = parse_reference(outcome.reference).registry_address
        image = _bindable(outcome)
        if image is not None:
            bindable.setdefault(address, []).append(image)
            continue
        others_total[address] = others_total.get(address, 0) + 1
        rows = others.setdefault(address, [])
        if len(rows) < MAX_LEFT_OUT_SHOWN:
            rows.append(_other(outcome))
    return tuple(
        RegistryGroup(
            address=address,
            sets=tuple(SetRef(entry.id, entry.name) for entry in sets),
            bindable=tuple(bindable.get(address, ())),
            others=tuple(others.get(address, ())),
            others_total=others_total.get(address, 0),
        )
        for address, sets in sorted(at_address.items())
        if address in bindable or address in others
    )


def _bindable(outcome: ReferenceOutcome) -> BindableImage | None:
    if outcome.outcome not in ("binds", "ambiguous"):
        return None
    options = tuple(
        sorted(
            (
                SetOption(check.choice.credential_set_id, check.name, check.digest)
                for check in outcome.matches
                if check.choice.credential_set_id is not None
                and check.name is not None
                and check.digest is not None
            ),
            key=lambda option: (option.name.casefold(), option.set_id),
        )
    )
    if not options:
        return None
    return BindableImage(
        outcome.reference, outcome.display, options, outcome.outcome == "ambiguous"
    )


def _other(outcome: ReferenceOutcome) -> OtherImage:
    match outcome.outcome:
        case "anonymous_only":
            return OtherImage(outcome.reference, outcome.display, "works", WORKS_MESSAGE)
        case "not_checked":
            return OtherImage(
                outcome.reference, outcome.display, "not_checked", NOT_CHECKED_MESSAGE
            )
    details = tuple(
        f"{check.name}: {_message_of(check)}"
        for check in outcome.tried
        if check.choice.credential_set_id is not None and check.status in ("failed", "mismatch")
    )
    return OtherImage(outcome.reference, outcome.display, "needs_set", NEEDS_SET_MESSAGE, details)


def _message_of(check: CandidateCheck) -> str:
    if check.status == "mismatch":
        return MISMATCH_MESSAGE
    try:
        return check_message(ManifestCheckKind(check.error or ""))
    except ValueError:
        return CHECK_FAILED_MESSAGE
