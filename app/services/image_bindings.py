"""Image-to-credential bindings: the offer, the pull-time resolver, the writer and the reaper."""

import asyncio
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Final, Literal

from app.auth.errors import PermissionDenied, ProductError, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.clocked import Clock
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.image_binding import ImageBindingRepository, StoredBinding
from app.db.repositories.registry_access import (
    OfferedCredentialSet,
    OfferedSetsOverLimit,
    RegistryAccessRepository,
)
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, NotFound
from app.docker_engine.models import ContainerSummary, ImageSummary
from app.logging import get_logger
from app.registry_client.addresses import DOCKER_HUB_ADDRESS
from app.registry_client.manifests import ManifestCheckFailed, ManifestCheckKind, ManifestClient
from app.registry_client.probe import RegistryTarget
from app.registry_client.providers import ProviderKind, parse_extras
from app.registry_client.references import (
    ImageReference,
    ReferenceInvalid,
    digests_for,
    parse_reference,
)
from app.services.activity import ActivityRecorder, Actor, audited, current_audit
from app.services.authorisation import Authoriser
from app.services.guards import require_administrator
from app.services.image_listing import ImageListTooLarge as ImageListTooLarge
from app.services.image_listing import read_host_listing
from app.services.object_inspector import HostClients
from app.services.pull_credentials import (
    ANONYMOUS_CHOICE,
    CandidateListTooLarge,
    CredentialChoice,
    CredentialChoiceRequired,
    CredentialSetMismatch,
    PullCredentials,
    PullSource,
    SeenBinding,
)
from app.services.registry_resolution import CredentialSetUnavailable, RegistryCredentialResolver

logger = get_logger(__name__)

MAX_REAP: Final = 200
MAX_CONFIGURED_INSPECTS: Final = 100
CONFIGURED_INSPECT_CONCURRENCY: Final = 8
UNSHOWN_SET: Final = "a credential set"
REBIND_REFUSED: Final = "Only an administrator can change which credential set this image uses."
REAP_UNVERIFIED: Final = "Some containers could not be checked, so no binding was removed."
REBIND_PINNED: Final = "A reference pinned by digest is rebound by pulling it."
REBIND_UNBOUND: Final = "Pull the image to bind it."
BIND_ANONYMOUS_REFUSED: Final = (
    "Choose a credential set; an image pulled anonymously needs no binding."
)
REBIND_CHANGED: Final = "The binding changed since you reviewed it. Review it again."
VERIFIED_DIGEST_INVALID: Final = "The digest must be sha256: followed by 64 hexadecimal digits."
BINDING_EXISTS: Final = "binding_exists"
ANONYMOUS_CHOICE_REASON: Final = "anonymous_choice"

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_IMAGE_ID = re.compile(r"[0-9a-f]{64}")
_ID_AS_IMAGE = re.compile(r"(?:sha256:)?[0-9a-f]{12,64}")

type BindingState = Literal["bound", "credential_missing", "unbound"]
type RecordOutcome = Literal["created", "rebound", "removed", "kept", "skipped"]
type VerifiedBind = Literal["bound", "skipped"]


class RebindRefused(PermissionDenied):
    """A standard user would change which credential set an existing binding names."""

    def __init__(self) -> None:
        super().__init__(REBIND_REFUSED)


class BindingChangedSinceReview(ProductError):
    """The binding is no longer the one the administrator reviewed; nothing was changed."""

    def __init__(self) -> None:
        super().__init__(REBIND_CHANGED)


@dataclass(frozen=True, slots=True)
class BindingView:
    """The binding of one reference as shown.

    `state` is unbound when no row exists. `set_name` is the set's name where the viewer may see
    it and "a credential set" where not; it is None when no set is involved. `missing_reason` is
    deleted or address_changed for credential_missing.
    """

    reference: str
    state: BindingState
    credential_set_id: int | None
    set_name: str | None
    observed_digest: str | None
    missing_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CredentialOffer:
    """What a pull form shows for one reference: the sets that fit it and what to preselect.

    `preselected` is None when the viewer has to choose. `may_rebind` is true for
    administrators, the only role that may change which set a binding names.
    """

    reference: ImageReference
    candidates: tuple[OfferedCredentialSet, ...]
    preselected: CredentialChoice | None
    binding: BindingView
    may_rebind: bool

    @property
    def locked(self) -> bool:
        """Report whether the viewer is held to the existing binding and may not change it."""
        return not self.may_rebind and self.binding.state == "bound"

    @property
    def preselected_value(self) -> str | None:
        """Return the form value to check: `anonymous`, a set id as text, or None."""
        if self.preselected is None:
            return None
        set_id = self.preselected.credential_set_id
        return ANONYMOUS_CHOICE if set_id is None else str(set_id)


@dataclass(frozen=True, slots=True)
class RebindReview:
    """The rebind page's offer and the binding it was built on, posted back as the review."""

    offer: CredentialOffer
    seen: SeenBinding


@dataclass(frozen=True, slots=True)
class ReapableBinding:
    """A binding whose reference is on no image and in no container's configured image."""

    id: int
    reference: str
    display: str
    state: BindingState
    credential_set_id: int | None
    set_name: str | None
    missing_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReapableList:
    """The bindings a reap would remove; `more` is true when the list was cut at 200.

    `unverified` means containers configured by image id could not all be checked, so no
    binding is offered.
    """

    bindings: tuple[ReapableBinding, ...]
    more: bool
    unverified: bool = False


@dataclass(frozen=True, slots=True)
class PullDecision:
    """Which set a pull will use and why, decided without reading any secret.

    `seen` is the binding the decision was made against.
    """

    reference: ImageReference
    choice: CredentialChoice
    source: PullSource
    seen: SeenBinding


@dataclass(frozen=True, slots=True)
class LiveReferences:
    """The references present on a host, by canonical key.

    `complete` is false when a container configured by image id could not be resolved to the
    reference it was created with.
    """

    images: frozenset[str]
    containers: frozenset[str]
    complete: bool = True


def parse_pull_reference(text: str) -> ImageReference:
    """Parse an image reference, or raise ValidationFailed keyed `reference`."""
    try:
        return parse_reference(text)
    except ReferenceInvalid as error:
        raise ValidationFailed({"reference": error.message}) from None


def daemon_references(repo_tags: Sequence[str], repo_digests: Sequence[str] = ()) -> dict[str, str]:
    """Map each canonical key an image carries to the daemon's text for it."""
    texts: dict[str, str] = {}
    for text in (*repo_tags, *repo_digests):
        try:
            key = parse_reference(text).key
        except ReferenceInvalid:
            continue
        texts.setdefault(key, text)
    return texts


def canonical_keys(repo_tags: Sequence[str], repo_digests: Sequence[str] = ()) -> tuple[str, ...]:
    """Return the canonical keys of the references an image carries; malformed ones are skipped."""
    return tuple(daemon_references(repo_tags, repo_digests))


def bare_image_id(image_id: str) -> str | None:
    """Return the 64-hex id without its `sha256:` prefix, or None when it is not one."""
    bare = image_id.removeprefix("sha256:")
    return bare if _IMAGE_ID.fullmatch(bare) else None


async def observe_image(
    client: EngineClient, reference: ImageReference, *, name: str | None = None
) -> tuple[str | None, str | None]:
    """Inspect a reference just pulled or pushed and return its image id and digest.

    Both are None, except a digest the reference itself names, when the Engine cannot say.
    """
    try:
        inspected = await client.images.inspect(name or reference.key)
    except EngineError as error:
        logger.warning("image_inspect_after_transfer_failed", extra={"error": type(error).__name__})
        return None, reference.digest
    digest = reference.digest or next(
        iter(digests_for(inspected.repo_digests, reference.repository)), None
    )
    return bare_image_id(inspected.id), digest


def live_references(
    images: Sequence[ImageSummary], containers: Sequence[ContainerSummary]
) -> LiveReferences:
    """Collect the canonical keys of every image reference and container configured image.

    A container whose summary names an image id is not counted; `resolve_live_references` reads
    the reference it was created with.
    """
    on_images: set[str] = set()
    for image in images:
        on_images.update(canonical_keys(image.repo_tags, image.repo_digests))
    configured: set[str] = set()
    for container in containers:
        if not _ID_AS_IMAGE.fullmatch(container.image):
            configured.update(canonical_keys([container.image]))
    return LiveReferences(frozenset(on_images), frozenset(configured))


async def resolve_live_references(
    client: EngineClient, images: Sequence[ImageSummary], containers: Sequence[ContainerSummary]
) -> LiveReferences:
    """Collect the live references, inspecting containers that the list shows by image id.

    At most 100 containers are inspected, 8 at a time; beyond that, or when one cannot be read,
    the result is marked incomplete.
    """
    live = live_references(images, containers)
    pending = sorted({c.id for c in containers if _ID_AS_IMAGE.fullmatch(c.image)})
    if not pending:
        return live
    if len(pending) > MAX_CONFIGURED_INSPECTS:
        return replace(live, complete=False)
    gate = asyncio.Semaphore(CONFIGURED_INSPECT_CONCURRENCY)

    async def configured_image(container_id: str) -> str | None:
        async with gate:
            try:
                return (await client.containers.inspect(container_id)).config.image
            except NotFound:
                return ""
            except EngineError:
                return None

    found = await asyncio.gather(*(configured_image(container_id) for container_id in pending))
    if any(image is None for image in found):
        return replace(live, complete=False)
    configured = set(live.containers)
    for image in found:
        if image:
            configured.update(canonical_keys([image]))
    return replace(live, containers=frozenset(configured))


def binding_view(
    reference: str, stored: StoredBinding | None, *, name_visible: bool
) -> BindingView:
    """Describe a stored binding, or the absence of one, hiding the set's name when not visible."""
    if stored is None:
        return BindingView(reference, "unbound", None, None, None)
    if stored.state == "credential_missing":
        return BindingView(
            reference,
            "credential_missing",
            stored.missing_credential_id,
            stored.missing_credential_name if name_visible else UNSHOWN_SET,
            stored.observed_digest,
            stored.missing_reason,
        )
    return BindingView(
        reference,
        "bound",
        stored.registry_credential_id,
        stored.credential_name if name_visible else UNSHOWN_SET,
        stored.observed_digest,
    )


class SetCandidates:
    """The credential sets a caller may pull with on an environment, as the selector offers them."""

    def __init__(
        self, *, access: RegistryAccessRepository, environments: EnvironmentAccessRepository
    ) -> None:
        self._access = access
        self._environments = environments

    async def eligible(
        self, principal: Principal | None, environment_id: int
    ) -> tuple[OfferedCredentialSet, ...]:
        """List the sets granted to the environment and, for a standard user, to the user.

        ResourceNotFound for a standard user who cannot see the environment. None for the
        principal is the automation path, which needs the environment grant alone. The list is
        cut at 200, so it is for display and never decides which set a pull uses.
        """
        user_id = await self._scope(principal, environment_id)
        return tuple(await self._access.eligible_for_pull(environment_id, user_id=user_id))

    async def at(
        self, principal: Principal | None, environment_id: int, address: str
    ) -> tuple[OfferedCredentialSet, ...]:
        """List the eligible sets whose address is exactly the registry's.

        CandidateListTooLarge when more than 200 fit; the list is never cut.
        """
        user_id = await self._scope(principal, environment_id)
        try:
            return tuple(
                await self._access.eligible_for_pull(environment_id, user_id=user_id, url=address)
            )
        except OfferedSetsOverLimit:
            raise CandidateListTooLarge() from None

    async def get(
        self, principal: Principal | None, environment_id: int, credential_id: int
    ) -> OfferedCredentialSet | None:
        """Return the set if the caller may pull with it on the environment, else None."""
        user_id = await self._scope(principal, environment_id)
        return await self._access.offered_set(environment_id, credential_id, user_id=user_id)

    async def _scope(self, principal: Principal | None, environment_id: int) -> int | None:
        if principal is None or principal.is_administrator:
            return None
        if await self._environments.gate(principal.user_id, environment_id) is None:
            raise ResourceNotFound("not found")
        return principal.user_id


class BindingPullCredentials:
    """Decides which credential set a pull uses and turns it into an `X-Registry-Auth` value.

    The order is an explicit choice, then the reference's binding, then the only set that fits
    the registry. A reference is never matched to a set by host once it is bound, a default for
    the host only preselects a form, and no ordering ever decides.
    """

    def __init__(
        self,
        *,
        bindings: ImageBindingRepository,
        candidates: SetCandidates,
        resolver: RegistryCredentialResolver,
    ) -> None:
        self._bindings = bindings
        self._candidates = candidates
        self._resolver = resolver

    async def decide(
        self,
        environment_id: int,
        reference: str,
        *,
        principal: Principal | None,
        choice: CredentialChoice | None = None,
    ) -> PullDecision:
        """Apply the precedence without reading a secret.

        An unbound reference, and one whose binding lost its set, pulls anonymously for the
        automation path. Raises CredentialChoiceRequired, CredentialSetMismatch,
        CredentialSetUnavailable, CandidateListTooLarge, or RebindRefused when a standard user
        would change which set a binding names.
        """
        parsed = parse_pull_reference(reference)
        binding = await self._bindings.get(environment_id, parsed.key)
        seen = _seen_of(binding)
        if choice is not None:
            await self._check_choice(environment_id, parsed, binding, choice, principal)
            return PullDecision(parsed, choice, "choice", seen)
        if binding is not None and binding.state == "bound":
            if (
                binding.credential_url is not None
                and binding.credential_url != parsed.registry_address
            ):
                raise CredentialSetUnavailable("credential_mismatch")
            return PullDecision(
                parsed, CredentialChoice(binding.registry_credential_id), "binding", seen
            )
        if principal is None:
            return PullDecision(parsed, CredentialChoice(None), "no_match", seen)
        fitting = tuple(
            candidate
            for candidate in await self._candidates.at(
                principal, environment_id, parsed.registry_address
            )
            if not candidate.reads_as_anonymous
        )
        match fitting:
            case ():
                return PullDecision(parsed, CredentialChoice(None), "no_match", seen)
            case (only,):
                return PullDecision(parsed, CredentialChoice(only.id), "only_match", seen)
            case _:
                raise CredentialChoiceRequired(fitting)

    async def resolve(
        self,
        environment_id: int,
        reference: str,
        *,
        principal: Principal | None,
        choice: CredentialChoice | None = None,
        min_remaining: timedelta | None = None,
    ) -> PullCredentials:
        """Return the credentials for the reference; RuntimeError inside a write transaction.

        A derived token with less than `min_remaining` of life left is derived anew.
        """
        if self._bindings.in_transaction:
            raise RuntimeError("credentials are resolved outside a write transaction")
        decision = await self.decide(environment_id, reference, principal=principal, choice=choice)
        set_id = decision.choice.credential_set_id
        if set_id is None:
            return PullCredentials(source=decision.source, seen=decision.seen)
        resolved = await self._resolver.resolve_pull_credentials(
            set_id,
            environment_id=environment_id,
            principal=principal,
            registry_address=decision.reference.registry_address,
            min_remaining=min_remaining,
        )
        return replace(resolved, source=decision.source, seen=decision.seen)

    async def check_usable(
        self, environment_id: int, reference: str, *, principal: Principal | None
    ) -> PullDecision:
        """Apply the precedence and the grant and address checks of a pull without reading a secret.

        Raises what `resolve` raises before it would read the secret.
        """
        decision = await self.decide(environment_id, reference, principal=principal)
        set_id = decision.choice.credential_set_id
        if set_id is not None:
            await self._resolver.check_pull_set(
                set_id,
                environment_id=environment_id,
                principal=principal,
                registry_address=decision.reference.registry_address,
            )
        return decision

    async def _check_choice(
        self,
        environment_id: int,
        reference: ImageReference,
        binding: StoredBinding | None,
        choice: CredentialChoice,
        principal: Principal | None,
    ) -> None:
        changes = (
            binding is not None
            and binding.state == "bound"
            and binding.registry_credential_id != choice.credential_set_id
        )
        if changes and principal is not None and not principal.is_administrator:
            raise RebindRefused()
        if choice.credential_set_id is None:
            return
        offered = await self._candidates.get(principal, environment_id, choice.credential_set_id)
        if offered is None:
            raise CredentialSetUnavailable("missing")
        if offered.url != reference.registry_address:
            raise CredentialSetMismatch()


class ImageBindingService:
    """Reads bindings for display, writes them after a pull or push, and removes orphaned ones."""

    def __init__(
        self,
        *,
        db: Session,
        bindings: ImageBindingRepository,
        candidates: SetCandidates,
        authoriser: Authoriser,
        hosts: HostClients,
        activity: ActivityRecorder,
        clock: Clock,
        resolver: RegistryCredentialResolver | None = None,
        manifests: ManifestClient | None = None,
    ) -> None:
        self._db = db
        self._bindings = bindings
        self._candidates = candidates
        self._authoriser = authoriser
        self._hosts = hosts
        self._activity = activity
        self._clock = clock
        self._resolver = resolver
        self._manifests = manifests

    async def offer(
        self, principal: Principal, environment_id: int, reference_text: str
    ) -> CredentialOffer:
        """Describe the choice a pull of the reference needs; creators only.

        Preselects the bound set when the reference is bound and the viewer may use it, and
        nothing when they may not; otherwise the default set for the registry, the only fitting
        set, or anonymous when none fits. With several fitting sets and no default nothing is
        preselected.
        """
        await self._authoriser.require_create(principal, environment_id)
        return await self._describe(
            principal, environment_id, reference_text, anonymous_default=False
        )

    async def picker(
        self, principal: Principal, environment_id: int, reference_text: str
    ) -> CredentialOffer:
        """Describe the credential picker a pull form shows for the reference.

        Preselects the bound set when the viewer may use it, and nothing when they may not;
        without a bound set, the default set for the registry, else anonymous. The caller pulls
        through its own checks, so this needs only sight of the environment.
        """
        await self._authoriser.require_environment(principal, environment_id)
        return await self._describe(
            principal, environment_id, reference_text, anonymous_default=True
        )

    async def _describe(
        self,
        principal: Principal,
        environment_id: int,
        reference_text: str,
        *,
        anonymous_default: bool,
    ) -> CredentialOffer:
        reference = parse_pull_reference(reference_text)
        stored = await self._bindings.get(environment_id, reference.key)
        return await self._offer_of(
            principal, environment_id, reference, stored, anonymous_default=anonymous_default
        )

    async def _offer_of(
        self,
        principal: Principal,
        environment_id: int,
        reference: ImageReference,
        stored: StoredBinding | None,
        *,
        anonymous_default: bool,
    ) -> CredentialOffer:
        named = None if stored is None else stored.registry_credential_id
        fitting = tuple(
            candidate
            for candidate in await self._candidates.at(
                principal, environment_id, reference.registry_address
            )
            if not candidate.reads_as_anonymous or candidate.id == named
        )
        ids = {candidate.id for candidate in fitting}
        view = binding_view(
            reference.key,
            stored,
            name_visible=principal.is_administrator or _may_name(stored, ids),
        )
        return CredentialOffer(
            reference=reference,
            candidates=fitting,
            preselected=_preselection(stored, fitting, ids, anonymous_default=anonymous_default),
            binding=view,
            may_rebind=principal.is_administrator,
        )

    async def views(
        self, principal: Principal, environment_id: int, references: Sequence[str]
    ) -> Mapping[str, BindingView]:
        """Return the binding view of each canonical reference, unbound where none exists."""
        await self._authoriser.require_environment(principal, environment_id)
        found = await self._bindings.for_references(environment_id, references)
        ids = await self._visible_set_ids(principal, environment_id)
        return {
            reference: binding_view(
                reference,
                found.get(reference),
                name_visible=principal.is_administrator or _may_name(found.get(reference), ids),
            )
            for reference in references
        }

    async def reapable(self, principal: Principal, environment_id: int) -> ReapableList:
        """List up to 200 bindings whose reference is on no image and in no container.

        Administrators only.
        """
        await self._authoriser.require_environment(principal, environment_id)
        require_administrator(principal)
        client = await self._hosts.client(environment_id)
        images, containers = await read_host_listing(client)
        live = await resolve_live_references(client, images, containers)
        return await self.orphaned(principal, environment_id, live)

    async def orphaned(
        self, principal: Principal, environment_id: int, live: LiveReferences
    ) -> ReapableList:
        """Find the bindings that are absent from the live references, scanning in id order.

        Administrators only. Nothing is offered when the live references are incomplete.
        """
        require_administrator(principal)
        if not live.complete:
            return ReapableList((), False, unverified=True)
        found: list[StoredBinding] = []
        more = False
        after = 0
        while not more:
            page = await self._bindings.scan_environment(environment_id, after_id=after)
            if not page:
                break
            after = page[-1].id
            for stored in page:
                if _is_orphan(stored.image_reference, live):
                    if len(found) == MAX_REAP:
                        more = True
                        break
                    found.append(stored)
        ids = await self._visible_set_ids(principal, environment_id)
        return ReapableList(
            tuple(
                _reapable_of(stored, principal.is_administrator or _may_name(stored, ids))
                for stored in found
            ),
            more,
        )

    async def record_pull(
        self,
        actor: Actor | Principal,
        environment_id: int,
        reference: ImageReference,
        credentials: PullCredentials,
        digest: str | None,
    ) -> RecordOutcome:
        """Bind a reference to the set a pull or push used, inside the caller's transaction.

        Returns created for a first binding, rebound when the set changed or a credential_missing
        row was repaired, removed when an administrator chose anonymous against a set binding,
        kept when only the digest was refreshed or the existing binding stands, and skipped when
        the set no longer exists, a credential_missing row stays unrepaired, or an anonymous pull
        had no binding to change. A set binding is changed only by an administrator's explicit
        choice made against the row that is still stored; a credential_missing row is repaired by
        any pull that chose or was offered a set. A created, rebound or removed binding writes a
        related activity row.
        """
        observed = digest if digest is not None and _DIGEST.fullmatch(digest) else None
        set_id = credentials.credential_set_id
        if set_id is not None and not await self._bindings.credential_exists(set_id):
            logger.warning("image_binding_set_gone", extra={"environment_id": environment_id})
            return "skipped"
        now = self._clock()
        stored = await self._bindings.get(environment_id, reference.key)
        if set_id is None:
            return await self._record_anonymous(
                actor, environment_id, reference, credentials, stored
            )
        detail: dict[str, str | int | bool | None] = {
            "credential_set_id": set_id,
            "anonymous": False,
        }
        if stored is None:
            await self._bindings.insert(
                environment_id=environment_id,
                image_reference=reference.key,
                credential_id=set_id,
                observed_digest=observed,
                now=now,
            )
            await self._related(actor, "image_bound", environment_id, reference.key, detail)
            return "created"
        if stored.state == "bound" and stored.registry_credential_id == set_id:
            if stored.observed_digest != observed:
                await self._bindings.update_digest(stored.id, observed_digest=observed, now=now)
            return "kept"
        if not _may_rebind(actor, credentials, stored):
            logger.warning(
                "image_binding_rebind_declined",
                extra={"environment_id": environment_id, "binding_id": stored.id},
            )
            return "skipped" if stored.state == "credential_missing" else "kept"
        await self._bindings.rebind(
            stored.id, credential_id=set_id, observed_digest=observed, now=now
        )
        previous = (
            stored.missing_credential_id
            if stored.state == "credential_missing"
            else stored.registry_credential_id
        )
        detail.update(previous_credential_set_id=previous, previous_state=stored.state)
        await self._related(actor, "image_rebound", environment_id, reference.key, detail)
        return "rebound"

    async def _record_anonymous(
        self,
        actor: Actor | Principal,
        environment_id: int,
        reference: ImageReference,
        credentials: PullCredentials,
        stored: StoredBinding | None,
    ) -> RecordOutcome:
        if stored is None or stored.state == "credential_missing":
            return "skipped"
        if not _may_rebind(actor, credentials, stored):
            logger.warning(
                "image_binding_rebind_declined",
                extra={"environment_id": environment_id, "binding_id": stored.id},
            )
            return "kept"
        if not await self._bindings.delete_if_same(
            stored.id, environment_id=environment_id, image_reference=reference.key
        ):
            return "skipped"
        await self._related(
            actor,
            "image_binding_removed",
            environment_id,
            reference.key,
            {
                "credential_set_id": stored.registry_credential_id,
                "state": stored.state,
                "reason": ANONYMOUS_CHOICE_REASON,
            },
        )
        return "removed"

    async def rebind_review(
        self, principal: Principal, environment_id: int, reference_text: str
    ) -> RebindReview:
        """Describe the rebind choice for a bound or credential_missing reference.

        Administrators only. Raises ValidationFailed for a reference pinned by digest and for one
        with no binding. `seen` is what the form posts back as `expected`.
        """
        await self._authoriser.require_environment(principal, environment_id)
        require_administrator(principal)
        reference = parse_pull_reference(reference_text)
        stored = await self._rebindable(environment_id, reference)
        offer = await self._offer_of(
            principal, environment_id, reference, stored, anonymous_default=True
        )
        return RebindReview(offer, _seen_of(stored))

    @audited(
        action="image_rebound",
        target_type="image_binding",
        target_id=lambda a: _binding_target(str(a["reference"])),
        environment_id=lambda a: int(a["environment_id"]),
    )
    async def rebind(
        self,
        actor: Principal,
        environment_id: int,
        reference: str,
        choice: CredentialChoice,
        expected: SeenBinding,
    ) -> None:
        """Point a binding at another set after a manifest HEAD succeeds with it, or remove it.

        Administrators only. An anonymous choice removes the binding and verifies nothing. A set
        is verified outside any transaction; the binding is then rebound, or removed, only if it
        still matches `expected`. The stored observed digest is kept. Raises ValidationFailed
        for a pinned or unbound reference and a registry that refuses the choice;
        CredentialSetUnavailable for a set the environment cannot use; and
        BindingChangedSinceReview when the binding is not the one reviewed.
        """
        audit = current_audit()
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        parsed = parse_pull_reference(reference)
        try:
            stored = await self._rebindable(environment_id, parsed)
        except ValidationFailed:
            audit.reason = "pinned" if parsed.pinned else "not_bound"
            raise
        set_id = choice.credential_set_id
        previous = _set_of(stored)
        audit.detail.update(
            credential_set_id=set_id,
            anonymous=set_id is None,
            previous_credential_set_id=previous,
            previous_state=stored.state,
        )
        if set_id is not None:
            if stored.state == "bound" and previous == set_id:
                audit.unchanged()
                return
            audit.detail["registry_digest"] = await self._verify(
                actor, environment_id, parsed, set_id
            )
        async with self._db.transaction():
            current = await self._bindings.get(environment_id, parsed.key)
            if current is None or _seen_of(current) != expected:
                audit.reason = "binding_changed"
                raise BindingChangedSinceReview()
            if set_id is None:
                await self._bindings.delete_if_same(
                    current.id, environment_id=environment_id, image_reference=parsed.key
                )
                audit.detail["removed"] = True
                return
            await self._bindings.rebind(
                current.id,
                credential_id=set_id,
                observed_digest=current.observed_digest,
                now=self._clock(),
            )
            audit.detail["verified"] = True

    @audited(
        action="image_bound",
        target_type="image_binding",
        target_id=lambda a: _binding_target(str(a["reference"])),
        environment_id=lambda a: int(a["environment_id"]),
    )
    async def bind_verified(
        self,
        actor: Principal,
        environment_id: int,
        reference: str,
        choice: CredentialChoice,
        *,
        digest: str,
        bulk: bool = False,
    ) -> VerifiedBind:
        """Bind an unbound reference to a set whose registry digest matched the local image.

        Administrators only. Nothing is pulled and the host is not contacted. `bulk` marks a
        binding written from a reviewed list of many. Returns skipped, and writes no row, when
        the reference already has a binding. Raises ValidationFailed for a pinned reference, a
        malformed digest, or an anonymous choice; CredentialSetUnavailable for a set the
        environment cannot use; CredentialSetMismatch for a set at another registry.
        """
        audit = current_audit()
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        parsed = parse_pull_reference(reference)
        if parsed.pinned:
            raise ValidationFailed({"reference": REBIND_PINNED})
        if not _DIGEST.fullmatch(digest):
            raise ValidationFailed({"digest": VERIFIED_DIGEST_INVALID})
        set_id = choice.credential_set_id
        audit.detail.update(
            credential_set_id=set_id,
            anonymous=set_id is None,
            digest=digest,
            verified=True,
            pulled=False,
        )
        if bulk:
            audit.detail["bulk"] = True
        if set_id is None:
            audit.reason = "anonymous_choice"
            raise ValidationFailed({"credential_choice": BIND_ANONYMOUS_REFUSED})
        offered = await self._candidates.get(actor, environment_id, set_id)
        if offered is None:
            audit.reason = "missing"
            raise CredentialSetUnavailable("missing")
        if offered.url != parsed.registry_address:
            audit.reason = "credential_mismatch"
            raise CredentialSetMismatch()
        async with self._db.transaction():
            if await self._bindings.get(environment_id, parsed.key) is not None:
                audit.reason = BINDING_EXISTS
                audit.unchanged()
                return "skipped"
            if not await self._bindings.credential_exists(set_id):
                audit.reason = "missing"
                raise CredentialSetUnavailable("missing")
            await self._bindings.insert(
                environment_id=environment_id,
                image_reference=parsed.key,
                credential_id=set_id,
                observed_digest=digest,
                now=self._clock(),
            )
        return "bound"

    async def _rebindable(self, environment_id: int, reference: ImageReference) -> StoredBinding:
        if reference.pinned:
            raise ValidationFailed({"reference": REBIND_PINNED})
        stored = await self._bindings.get(environment_id, reference.key)
        if stored is None:
            raise ValidationFailed({"reference": REBIND_UNBOUND})
        return stored

    async def _verify(
        self,
        actor: Principal,
        environment_id: int,
        reference: ImageReference,
        set_id: int,
    ) -> str:
        audit = current_audit()
        if self._resolver is None or self._manifests is None:
            raise RuntimeError("rebinding needs a credential resolver and a manifest client")
        try:
            resolved = await self._resolver.resolve_login(
                set_id,
                environment_id=environment_id,
                principal=actor,
                registry_address=reference.registry_address,
            )
        except CredentialSetUnavailable as error:
            audit.reason = error.reason
            raise
        try:
            return await self._manifests.head_digest(resolved.target, reference, resolved.login)
        except ManifestCheckFailed as failure:
            audit.reason = failure.kind.value
            raise ValidationFailed({"credential_choice": failure.message}) from None
        except Exception as error:
            logger.error("image_rebind_check_crashed", extra={"error": type(error).__name__})
            fallback = ManifestCheckFailed(ManifestCheckKind.PROTOCOL)
            audit.reason = fallback.kind.value
            raise ValidationFailed({"credential_choice": fallback.message}) from None

    @audited(
        action="image_bindings_reaped",
        target_type="environment",
        target_id=lambda a: str(a["environment_id"]),
        environment_id=lambda a: int(a["environment_id"]),
    )
    async def reap(
        self, actor: Principal, environment_id: int, *, expected: Mapping[int, str]
    ) -> int:
        """Delete the listed bindings that are still orphaned and still name the same reference.

        Administrators only. `expected` maps binding id to the reference the reviewer saw, at most
        200. A binding that changed, or whose image came back, is left alone. Returns how many
        were deleted.
        """
        audit = current_audit()
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        if not expected or len(expected) > MAX_REAP:
            raise ValidationFailed({"bindings": f"Select between 1 and {MAX_REAP} bindings."})
        client = await self._hosts.client(environment_id)
        images, containers = await read_host_listing(client)
        live = await resolve_live_references(client, images, containers)
        if not live.complete:
            raise ValidationFailed({"bindings": REAP_UNVERIFIED})
        listed = {
            binding_id: reference
            for binding_id, reference in expected.items()
            if _is_orphan(reference, live)
        }
        removed = 0
        async with self._db.transaction():
            for binding_id, reference in sorted(listed.items()):
                stored = await self._bindings.get_by_id(binding_id)
                if (
                    stored is None
                    or stored.environment_id != environment_id
                    or stored.image_reference != reference
                ):
                    continue
                if not await self._bindings.delete_if_same(
                    binding_id, environment_id=environment_id, image_reference=reference
                ):
                    continue
                removed += 1
                await self._related(
                    actor,
                    "image_binding_removed",
                    environment_id,
                    reference,
                    {"credential_set_id": _set_of(stored), "state": stored.state},
                )
            audit.detail["count"] = removed
            if removed == 0:
                audit.unchanged()
        return removed

    async def _related(
        self,
        actor: Actor | Principal,
        action: str,
        environment_id: int,
        reference: str,
        detail: Mapping[str, str | int | bool | None],
    ) -> None:
        await self._activity.record_related(
            actor,
            action=action,
            target_type="image_binding",
            target_id=reference,
            environment_id=environment_id,
            detail=detail,
        )

    async def _visible_set_ids(self, principal: Principal, environment_id: int) -> frozenset[int]:
        if principal.is_administrator:
            return frozenset()
        return frozenset(s.id for s in await self._candidates.eligible(principal, environment_id))


def _binding_target(reference: str) -> str:
    try:
        return parse_reference(reference).key
    except ReferenceInvalid:
        return reference[:128]


def public_target(address: str) -> RegistryTarget:
    """Return the target for reading a registry with no login."""
    provider: ProviderKind = "dockerhub" if address == DOCKER_HUB_ADDRESS else "custom"
    return RegistryTarget(
        provider=provider,
        address=address,
        verify_tls=True,
        ca_bundle=None,
        extras=parse_extras(provider, {}),
    )


def _set_of(stored: StoredBinding) -> int | None:
    if stored.state == "credential_missing":
        return stored.missing_credential_id
    return stored.registry_credential_id


def _seen_of(stored: StoredBinding | None) -> SeenBinding:
    if stored is None:
        return SeenBinding()
    return SeenBinding(stored.state, stored.id, _set_of(stored))


def _may_rebind(
    actor: Actor | Principal, credentials: PullCredentials, stored: StoredBinding
) -> bool:
    seen = credentials.seen
    if (
        seen is None
        or seen.binding_id != stored.id
        or seen.state != stored.state
        or seen.set_id != _set_of(stored)
    ):
        return False
    if stored.state == "credential_missing":
        return credentials.source in ("choice", "only_match")
    return (
        isinstance(actor, Principal) and actor.is_administrator and credentials.source == "choice"
    )


def _may_name(stored: StoredBinding | None, ids: Collection[int]) -> bool:
    return (
        stored is not None
        and stored.state == "bound"
        and stored.registry_credential_id is not None
        and stored.registry_credential_id in ids
    )


def _preselection(
    stored: StoredBinding | None,
    fitting: Sequence[OfferedCredentialSet],
    ids: set[int],
    *,
    anonymous_default: bool,
) -> CredentialChoice | None:
    if stored is not None and stored.state == "bound":
        return (
            CredentialChoice(stored.registry_credential_id)
            if stored.registry_credential_id in ids
            else None
        )
    default = next((s for s in fitting if s.is_default_for_host), None)
    if default is not None:
        return CredentialChoice(default.id)
    if anonymous_default:
        return CredentialChoice(None)
    if len(fitting) == 1:
        return CredentialChoice(fitting[0].id)
    return CredentialChoice(None) if not fitting else None


def _is_orphan(reference: str, live: LiveReferences) -> bool:
    return reference not in live.images and reference not in live.containers


def _reapable_of(stored: StoredBinding, name_visible: bool) -> ReapableBinding:
    view = binding_view(stored.image_reference, stored, name_visible=name_visible)
    try:
        display = parse_reference(stored.image_reference).familiar
    except ReferenceInvalid:
        display = stored.image_reference
    return ReapableBinding(
        id=stored.id,
        reference=stored.image_reference,
        display=display,
        state=view.state,
        credential_set_id=view.credential_set_id,
        set_name=view.set_name,
        missing_reason=view.missing_reason,
    )
