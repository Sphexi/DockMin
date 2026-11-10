"""Which credential sets can read each unbound image in use on a host, proved by manifest HEAD."""

import asyncio
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from app.auth.principal import Principal
from app.db.repositories.image_binding import ImageBindingRepository
from app.db.repositories.registry_access import OfferedCredentialSet
from app.docker_engine.models import ImageSummary, SwarmService
from app.logging import get_logger
from app.registry_client.manifests import ManifestCheckFailed, ManifestClient
from app.registry_client.probe import RegistryLogin, RegistryTarget
from app.registry_client.providers import PROVIDERS
from app.registry_client.references import (
    ImageReference,
    ReferenceInvalid,
    digests_for,
    parse_reference,
)
from app.services.authorisation import Authoriser
from app.services.guards import require_administrator
from app.services.host_objects import read_services
from app.services.image_bindings import (
    SetCandidates,
    canonical_keys,
    public_target,
    resolve_live_references,
)
from app.services.image_listing import read_host_listing
from app.services.object_inspector import HostClients, SwarmUnavailable
from app.services.pull_credentials import CandidateListTooLarge, CredentialChoice
from app.services.registry_resolution import CredentialSetUnavailable, RegistryCredentialResolver
from app.services.swarm_common import split_service_image

logger = get_logger(__name__)

MAX_CHECKED_REFERENCES: Final = 200
MAX_CANDIDATE_SETS: Final = 10
MAX_CHECKS_IN_FLIGHT: Final = 5
CHECK_SECONDS: Final = 10.0
TOTAL_SECONDS: Final = 120.0

type Outcome = Literal["binds", "ambiguous", "no_match", "no_local_digest", "pinned", "not_checked"]
type CheckStatus = Literal["match", "mismatch", "failed", "not_checked"]


@dataclass(frozen=True, slots=True)
class ScanLimits:
    """The bounds of one scan."""

    references: int = MAX_CHECKED_REFERENCES
    in_flight: int = MAX_CHECKS_IN_FLIGHT
    check_seconds: float = CHECK_SECONDS
    total_seconds: float = TOTAL_SECONDS


@dataclass(frozen=True, slots=True)
class CandidateCheck:
    """One credential set, or anonymous (name None), tried against one reference.

    `error` is a manifest failure kind or a credential-set reason, set when `status` is failed;
    `digest` is what the registry reported.
    """

    choice: CredentialChoice
    name: str | None
    status: CheckStatus
    digest: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReferenceOutcome:
    """What the scan found for one unbound reference in use.

    `local_digests` are the `RepoDigests` of the local image whose tags carry the reference.
    `candidates_capped` means more sets fit the registry than were tried, so `binds` is never
    the outcome.
    """

    reference: str
    display: str
    local_digests: tuple[str, ...]
    outcome: Outcome
    tried: tuple[CandidateCheck, ...] = ()
    candidates_capped: bool = False

    @property
    def matches(self) -> tuple[CandidateCheck, ...]:
        """Return the candidates whose registry digest is one of the local digests."""
        return tuple(check for check in self.tried if check.status == "match")

    @property
    def choices(self) -> tuple[CredentialChoice, ...]:
        """Return the choices that matched; the only options an administrator may pick between."""
        return tuple(check.choice for check in self.matches)


@dataclass(frozen=True, slots=True)
class UnboundImageScan:
    """The unbound references in use on one environment, in reference-key order.

    `complete` is false when a container's configured image or the Swarm services could not be
    read, so a reference may be missing.
    """

    environment_id: int
    outcomes: tuple[ReferenceOutcome, ...]
    complete: bool = True


@dataclass(frozen=True, slots=True)
class _Run:
    principal: Principal
    environment_id: int
    gate: asyncio.Semaphore
    deadline: float
    limits: ScanLimits


@dataclass(frozen=True, slots=True)
class _Registry:
    sets: tuple[OfferedCredentialSet, ...]
    anonymous: bool
    capped: bool
    too_many: bool = False


class BindingCandidateScanner:
    """Finds, without writing or pulling, the credential sets an unbound image could be bound to.

    Candidates are the sets granted to the environment at the reference's registry address (ten
    at most), then anonymous where one of them is of a provider that admits it. A set with
    authentication off that connects as an anonymous read does is not tried as a second
    candidate; it enables the anonymous one. Each gets one manifest HEAD and matches when the
    digest returned is among the local `RepoDigests`.
    """

    def __init__(
        self,
        *,
        bindings: ImageBindingRepository,
        candidates: SetCandidates,
        resolver: RegistryCredentialResolver,
        manifests: ManifestClient,
        hosts: HostClients,
        authoriser: Authoriser,
        limits: ScanLimits | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bindings = bindings
        self._candidates = candidates
        self._resolver = resolver
        self._manifests = manifests
        self._hosts = hosts
        self._authoriser = authoriser
        self._limits = limits or ScanLimits()
        self._monotonic = monotonic

    async def scan(self, principal: Principal, environment_id: int) -> UnboundImageScan:
        """Check every unbound image reference in use on the environment; administrators only.

        Raises RuntimeError inside a write transaction. A candidate that cannot be tried is that
        candidate's failure; past the time budget and the reference cap the rest are `not_checked`.
        """
        if self._bindings.in_transaction:
            raise RuntimeError("registry checks run outside a write transaction")
        await self._authoriser.require_environment(principal, environment_id)
        require_administrator(principal)
        client = await self._hosts.client(environment_id)
        environment = await self._hosts.environment(environment_id)
        images, containers = await read_host_listing(client)
        live = await resolve_live_references(client, images, containers)
        references = set(live.containers)
        complete = live.complete
        if environment.type == "swarm":
            try:
                services = await read_services(client, environment_id)
            except SwarmUnavailable:
                complete = False
            else:
                references.update(service_references(services))
        stored = await self._bindings.for_references(environment_id, sorted(references))
        unbound = sorted(references - stored.keys())
        outcomes = await self._outcomes(principal, environment_id, unbound, local_digests(images))
        logger.info(
            "binding_candidates_scanned",
            extra={
                "environment_id": environment_id,
                "references": len(outcomes),
                **{
                    name: sum(1 for o in outcomes if o.outcome == name)
                    for name in ("binds", "ambiguous", "no_match", "not_checked")
                },
            },
        )
        return UnboundImageScan(environment_id, outcomes, complete)

    async def check_anonymously(
        self,
        principal: Principal,
        environment_id: int,
        references: Sequence[tuple[ImageReference, tuple[str, ...]]],
    ) -> tuple[ReferenceOutcome, ...]:
        """Send one anonymous manifest HEAD for each reference, within the scan's bounds.

        References past the cap, and those the time budget does not reach, come back
        `not_checked`. Raises RuntimeError inside a write transaction. No credential set is read.
        """
        if self._bindings.in_transaction:
            raise RuntimeError("registry checks run outside a write transaction")
        limits = self._limits
        selected, left = references[: limits.references], references[limits.references :]
        run = _Run(
            principal,
            environment_id,
            asyncio.Semaphore(limits.in_flight),
            self._monotonic() + limits.total_seconds,
            limits,
        )
        anonymous = _Registry((), True, False)
        checked = await _all(
            [self._reference(run, reference, digests, anonymous) for reference, digests in selected]
        )
        skipped = [
            ReferenceOutcome(reference.key, reference.familiar, digests, "not_checked")
            for reference, digests in left
        ]
        return (*checked, *skipped)

    async def _outcomes(
        self,
        principal: Principal,
        environment_id: int,
        keys: Sequence[str],
        local: Mapping[str, tuple[str, ...]],
    ) -> tuple[ReferenceOutcome, ...]:
        settled: dict[str, ReferenceOutcome] = {}
        checkable: list[tuple[ImageReference, tuple[str, ...]]] = []
        for key in keys:
            try:
                reference = parse_reference(key)
            except ReferenceInvalid:
                continue
            digests = local.get(key, ())
            if reference.pinned:
                settled[key] = ReferenceOutcome(key, reference.familiar, digests, "pinned")
            elif not digests:
                settled[key] = ReferenceOutcome(key, reference.familiar, digests, "no_local_digest")
            else:
                checkable.append((reference, digests))
        limits = self._limits
        selected, left = checkable[: limits.references], checkable[limits.references :]
        for reference, digests in left:
            settled[reference.key] = ReferenceOutcome(
                reference.key, reference.familiar, digests, "not_checked"
            )
        run = _Run(
            principal,
            environment_id,
            asyncio.Semaphore(limits.in_flight),
            self._monotonic() + limits.total_seconds,
            limits,
        )
        registries = await self._registries(
            principal, environment_id, {reference.registry_address for reference, _ in selected}
        )
        checked = await _all(
            [
                self._reference(run, reference, digests, registries[reference.registry_address])
                for reference, digests in selected
            ]
        )
        settled.update((outcome.reference, outcome) for outcome in checked)
        return tuple(settled[key] for key in sorted(settled))

    async def _registries(
        self, principal: Principal, environment_id: int, addresses: set[str]
    ) -> dict[str, _Registry]:
        found: dict[str, _Registry] = {}
        for address in sorted(addresses):
            try:
                sets = await self._candidates.at(principal, environment_id, address)
            except CandidateListTooLarge:
                found[address] = _Registry((), False, False, too_many=True)
                continue
            separate = [offered for offered in sets if not offered.reads_as_anonymous]
            found[address] = _Registry(
                tuple(separate[:MAX_CANDIDATE_SETS]),
                any(PROVIDERS[offered.provider].anonymous_allowed for offered in sets),
                len(separate) > MAX_CANDIDATE_SETS,
            )
        return found

    async def _reference(
        self,
        run: _Run,
        reference: ImageReference,
        digests: tuple[str, ...],
        registry: _Registry,
    ) -> ReferenceOutcome:
        if registry.too_many:
            return ReferenceOutcome(reference.key, reference.familiar, digests, "not_checked")
        tries: list[OfferedCredentialSet | None] = [*registry.sets]
        if registry.anonymous:
            tries.append(None)
        tried = tuple(
            await _all([self._candidate(run, reference, digests, offered) for offered in tries])
        )
        return ReferenceOutcome(
            reference.key,
            reference.familiar,
            digests,
            _outcome_of(tried, capped=registry.capped),
            tried,
            registry.capped,
        )

    async def _candidate(
        self,
        run: _Run,
        reference: ImageReference,
        digests: tuple[str, ...],
        offered: OfferedCredentialSet | None,
    ) -> CandidateCheck:
        choice = CredentialChoice(None if offered is None else offered.id)
        name = None if offered is None else offered.name
        async with run.gate:
            if self._remaining(run) <= 0:
                return CandidateCheck(choice, name, "not_checked")
            target: RegistryTarget
            login: RegistryLogin | None = None
            if offered is None:
                target = public_target(reference.registry_address)
            else:
                try:
                    resolved = await self._resolver.resolve_login(
                        offered.id,
                        environment_id=run.environment_id,
                        principal=run.principal,
                        registry_address=reference.registry_address,
                    )
                except CredentialSetUnavailable as error:
                    return CandidateCheck(choice, name, "failed", error=error.reason)
                target, login = resolved.target, resolved.login
            remaining = self._remaining(run)
            if remaining <= 0:
                return CandidateCheck(choice, name, "not_checked")
            try:
                async with asyncio.timeout(min(run.limits.check_seconds, remaining)):
                    found = await self._manifests.head_digest(target, reference, login)
            except ManifestCheckFailed as failure:
                return CandidateCheck(choice, name, "failed", error=failure.kind.value)
            except TimeoutError:
                return CandidateCheck(choice, name, "failed", error="timeout")
            except Exception as error:
                logger.error(
                    "binding_candidate_check_crashed", extra={"error": type(error).__name__}
                )
                return CandidateCheck(choice, name, "failed", error="protocol")
        return CandidateCheck(choice, name, "match" if found in digests else "mismatch", found)

    def _remaining(self, run: _Run) -> float:
        return run.deadline - self._monotonic()


def local_digests(images: Sequence[ImageSummary]) -> dict[str, tuple[str, ...]]:
    """Map each canonical key an image's tags carry to the digests that image holds for it."""
    found: dict[str, tuple[str, ...]] = {}
    for image in images:
        for key in canonical_keys(image.repo_tags):
            found.setdefault(key, digests_for(image.repo_digests, parse_reference(key).repository))
    return found


def service_references(services: Sequence[SwarmService]) -> frozenset[str]:
    """Return the canonical keys of the images services run, any digest suffix removed.

    A service pinned by digest alone has no tag, so its pinned key stays; an image id is skipped.
    """
    keys: set[str] = set()
    for service in services:
        container = service.spec.task_template.get("ContainerSpec")
        text = container.get("Image") if isinstance(container, dict) else None
        if not isinstance(text, str):
            continue
        image = split_service_image(text)
        match image.kind:
            case "image_id":
                continue
            case "digest_only":
                candidate: str | None = text.strip()
            case _:
                candidate = image.reference
        if candidate is not None:
            keys.update(canonical_keys([candidate]))
    return frozenset(keys)


def _outcome_of(tried: Sequence[CandidateCheck], *, capped: bool) -> Outcome:
    if any(check.status == "not_checked" for check in tried):
        return "not_checked"
    matched = sum(1 for check in tried if check.status == "match")
    if matched == 0:
        return "no_match"
    return "binds" if matched == 1 and not capped else "ambiguous"


async def _all[T](coroutines: Sequence[Coroutine[Any, Any, T]]) -> list[T]:
    tasks = [asyncio.ensure_future(coroutine) for coroutine in coroutines]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
