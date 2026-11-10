"""Image update availability: compare the local digest of a bound reference with the registry's."""

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from typing import Final, Literal

from app.auth.errors import PermissionDenied, ProductError, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import retry_busy
from app.db.repositories.clocked import Clock, format_timestamp, utc_now
from app.db.repositories.image_binding import ImageBindingRepository, StoredBinding
from app.db.repositories.image_update_status import (
    ImageUpdateStatusRepository,
    StoredUpdateStatus,
)
from app.logging import get_logger
from app.registry_client.manifests import ManifestCheckFailed, ManifestCheckKind, ManifestClient
from app.registry_client.probe import RegistryLogin, RegistryTarget
from app.registry_client.references import (
    ImageReference,
    ReferenceInvalid,
    digests_for,
    parse_reference,
)
from app.services.authorisation import Authoriser
from app.services.image_bindings import bare_image_id, canonical_keys, public_target
from app.services.image_listing import read_images
from app.services.object_inspector import HostClients
from app.services.registry_resolution import CredentialSetUnavailable, RegistryCredentialResolver

logger = get_logger(__name__)

MAX_CHECK_REFERENCES: Final = 25
MAX_CONCURRENT_CHECKS: Final = 4
MAX_REQUESTS_PER_USER: Final = 2
SLOT_WAIT_SECONDS: Final = 10.0
BUSY_REASON: Final = "busy"
CACHE_TTL: Final = timedelta(seconds=30)
STATUS_RETENTION: Final = timedelta(days=30)
MAX_CACHED_RESULTS: Final = 2048
IMAGE_ID_REASON: Final = "image_id_reference"
INVALID_REASON: Final = "invalid_reference"
AUTHENTICATION_REQUIRED: Final = "authentication_required"
_NOT_STORED: Final = frozenset({BUSY_REASON, INVALID_REASON, IMAGE_ID_REASON})
_CALLER_SCOPED: Final = frozenset({"credential_not_available", "credential_mismatch"})
_IMAGE_ID_REFERENCE: Final = re.compile(r"(?:sha256:)?[0-9a-f]{12,64}")
_DEPLOYED_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")

type UpdateStateName = Literal["up_to_date", "update_available", "unknown", "pinned", "not_bound"]
type _Key = tuple[int, str, int | None, tuple[str, ...]]


class TooManyUpdateChecks(ProductError):
    """The user already has the most update checks running that one user may."""

    def __init__(self) -> None:
        super().__init__("You have update checks running. Wait for them to finish.")


@dataclass(frozen=True, slots=True)
class UpdateStatus:
    """Whether a reference has an update on its registry.

    `reference` is the canonical key and `display` the form people write. `reason` explains an
    unknown: no_local_digest, no_digest, credential_not_available, credential_mismatch,
    invalid_reference, image_id_reference (the text names an image id, not a registry
    reference), busy (no check slot came free in time), authentication_required (the registry
    refused an anonymous check), or a registry failure (dns, tcp, tls, certificate,
    authentication, not_found, rate_limited, protocol, timeout). `local_image_id` is the bare
    64-hex id of the local image carrying the tag, None when no image does, and for a reference
    checked against a deployed digest. `anonymous_fallback` is true when the reference's bound
    set is gone and the check ran anonymously instead; `missing_reason` then says whether the set
    was deleted or the registry address changed.
    """

    reference: str
    display: str
    state: UpdateStateName
    reason: str | None
    local_digest: str | None
    remote_digest: str | None
    checked_at: datetime
    local_image_id: str | None = None
    anonymous_fallback: bool = False
    missing_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _LocalImage:
    image_id: str | None
    digests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Verdict:
    state: Literal["up_to_date", "update_available", "unknown"]
    reason: str | None
    remote_digest: str | None


class UpdateCheckState:
    """Process-wide state of update checks: the concurrency cap, the cache and single flight.

    Results are held in memory only, every one for the same short time. A check that finds no
    free slot within ten seconds is unknown with reason busy and is not kept.
    """

    def __init__(
        self,
        clock: Clock = utc_now,
        *,
        concurrency: int = MAX_CONCURRENT_CHECKS,
        per_user: int = MAX_REQUESTS_PER_USER,
        ttl: timedelta = CACHE_TTL,
        slot_wait: float = SLOT_WAIT_SECONDS,
    ) -> None:
        self._clock = clock
        self._gate = asyncio.Semaphore(concurrency)
        self._slot_wait = slot_wait
        self._per_user = per_user
        self._ttl = ttl
        self._cache: dict[_Key, tuple[datetime, _Verdict]] = {}
        self._inflight: dict[_Key, asyncio.Future[_Verdict]] = {}
        self._requests: dict[int, int] = {}

    @contextmanager
    def request(self, user_id: int | None) -> Iterator[None]:
        """Count one check request against the user for the block; TooManyUpdateChecks past two."""
        if user_id is None:
            yield
            return
        if self._requests.get(user_id, 0) >= self._per_user:
            raise TooManyUpdateChecks()
        self._requests[user_id] = self._requests.get(user_id, 0) + 1
        try:
            yield
        finally:
            remaining = self._requests[user_id] - 1
            if remaining:
                self._requests[user_id] = remaining
            else:
                del self._requests[user_id]

    async def verdict(self, key: _Key, work: Callable[[], Awaitable[_Verdict]]) -> _Verdict:
        """Return the cached answer for the key, or run `work` once however many ask at once."""
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        running = self._inflight.get(key)
        if running is None:
            running = asyncio.ensure_future(self._gated(key, work))
            self._inflight[key] = running
            running.add_done_callback(partial(self._finished, key))
        return await asyncio.shield(running)

    async def aclose(self) -> None:
        """Cancel the checks still running; none survives."""
        pending = [task for task in self._inflight.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=5.0)
        self._cache.clear()

    async def _gated(self, key: _Key, work: Callable[[], Awaitable[_Verdict]]) -> _Verdict:
        try:
            async with asyncio.timeout(self._slot_wait):
                await self._gate.acquire()
        except TimeoutError:
            return _Verdict("unknown", BUSY_REASON, None)
        try:
            outcome = await work()
        finally:
            self._gate.release()
        now = self._clock()
        if len(self._cache) >= MAX_CACHED_RESULTS:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        if len(self._cache) >= MAX_CACHED_RESULTS:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (now + self._ttl, outcome)
        return outcome

    def _finished(self, key: _Key, task: asyncio.Future[_Verdict]) -> None:
        self._inflight.pop(key, None)
        if not task.cancelled():
            task.exception()


class ImageUpdateService:
    """Reports whether bound references have newer images on their registry, and stores the result.

    It changes nothing on the host. The check runs from this server, so a registry reachable only
    from the Docker host reads as unknown. A bound reference is checked with its set; any other,
    including one whose set was deleted, is checked anonymously at its own registry.
    """

    def __init__(
        self,
        *,
        bindings: ImageBindingRepository,
        resolver: RegistryCredentialResolver,
        hosts: HostClients,
        authoriser: Authoriser,
        manifests: ManifestClient,
        state: UpdateCheckState,
        clock: Clock = utc_now,
        statuses: ImageUpdateStatusRepository | None = None,
    ) -> None:
        self._bindings = bindings
        self._resolver = resolver
        self._hosts = hosts
        self._authoriser = authoriser
        self._manifests = manifests
        self._state = state
        self._clock = clock
        self._statuses = statuses

    async def check(
        self,
        principal: Principal | None,
        environment_id: int,
        references: Sequence[str],
        *,
        authorise_set: Callable[[int], Awaitable[None]] | None = None,
        deployed: Mapping[str, str] | None = None,
    ) -> tuple[UpdateStatus, ...]:
        """Check up to 25 references of an environment, in the order given.

        None for the principal is the scheduler, which has no user to rate-limit. A standard
        user needs the environment, and uses only a bound set they hold a grant on. Raises
        ValidationFailed, TooManyUpdateChecks, EnvironmentUnavailable and ImageListTooLarge;
        every failure of one reference is that reference's unknown, never up to date. Results
        are stored afterwards; a failed write is logged and never fails the check.

        `authorise_set` is the scheduler's stand-in for a user's grant: it is called with the
        bound set before the set is used, and a refusal makes that reference unknown with
        `credential_not_available`. A result that depends on who asked is not stored.

        `deployed` maps a reference, as written in `references`, to the digest a Swarm service
        runs. Such a reference is compared with that digest alone: the host's images are not
        read for it (not at all when every reference is deployed), `local_digest` is the
        deployed digest, and the result is not stored, because it describes a service and not
        the host's image. One call must not carry a reference under two deployed digests.
        """
        if principal is not None:
            await self._authoriser.require_environment(principal, environment_id)
        distinct = list(dict.fromkeys(references))
        if not distinct or len(distinct) > MAX_CHECK_REFERENCES:
            raise ValidationFailed(
                {"references": f"Check between 1 and {MAX_CHECK_REFERENCES} references at once."}
            )
        running = deployed or {}
        with self._state.request(None if principal is None else principal.user_id):
            parsed = [_parse(text) for text in distinct]
            valid = [p for p in parsed if isinstance(p, ImageReference)]
            reads_host = any(
                isinstance(p, ImageReference) and text not in running
                for text, p in zip(distinct, parsed, strict=True)
            )
            local = await self._local_images(environment_id) if reads_host else {}
            stored = await self._bindings.for_references(environment_id, [r.key for r in valid])
            scoped: set[str] = set()
            statuses = await asyncio.gather(
                *(
                    self._status(
                        principal,
                        environment_id,
                        p,
                        stored,
                        local,
                        authorise_set,
                        scoped if text not in running else set(),
                        running.get(text),
                    )
                    for text, p in zip(distinct, parsed, strict=True)
                )
            )
        counts = {name: sum(1 for s in statuses if s.state == name) for name in _STATE_NAMES}
        logger.info("image_update_checked", extra={"environment_id": environment_id, **counts})
        hosted = [s for text, s in zip(distinct, statuses, strict=True) if text not in running]
        if principal is not None:
            scoped.update(s.reference for s in hosted if s.reason in _CALLER_SCOPED)
        await self._store(environment_id, hosted, scoped)
        return tuple(statuses)

    async def _local_images(self, environment_id: int) -> dict[str, _LocalImage]:
        images = await read_images(await self._hosts.client(environment_id))
        local: dict[str, _LocalImage] = {}
        for image in images:
            for key in canonical_keys(image.repo_tags):
                local.setdefault(
                    key,
                    _LocalImage(
                        bare_image_id(image.id),
                        digests_for(image.repo_digests, parse_reference(key).repository),
                    ),
                )
        return local

    async def _store(
        self, environment_id: int, statuses: Sequence[UpdateStatus], scoped: set[str]
    ) -> None:
        if self._statuses is None:
            return
        rows: dict[str, StoredUpdateStatus] = {}
        for status in statuses:
            if status.reason not in _NOT_STORED and status.reference not in scoped:
                rows.setdefault(
                    status.reference,
                    StoredUpdateStatus(
                        reference=status.reference,
                        state=status.state,
                        reason=status.reason,
                        local_digest=status.local_digest,
                        remote_digest=status.remote_digest,
                        checked_at=format_timestamp(status.checked_at),
                    ),
                )
        if not rows:
            return
        repository = self._statuses
        prune_before = format_timestamp(self._clock() - STATUS_RETENTION)
        try:
            await retry_busy(
                partial(
                    repository.upsert,
                    environment_id,
                    list(rows.values()),
                    prune_before=prune_before,
                )
            )
        except Exception as error:
            logger.warning(
                "image_update_status_write_failed", extra={"error": type(error).__name__}
            )

    async def _status(
        self,
        principal: Principal | None,
        environment_id: int,
        parsed: ImageReference | str,
        stored: dict[str, StoredBinding],
        local: dict[str, _LocalImage],
        authorise_set: Callable[[int], Awaitable[None]] | None,
        scoped: set[str],
        deployed: str | None = None,
    ) -> UpdateStatus:
        now = self._clock()
        if isinstance(parsed, str):
            reason = IMAGE_ID_REASON if is_image_id_reference(parsed) else INVALID_REASON
            return UpdateStatus(parsed, parsed, "unknown", reason, None, None, now)
        reference = parsed
        image = None if deployed is not None else local.get(reference.key)
        digests: tuple[str, ...]
        if deployed is not None:
            digests = (deployed,) if _DEPLOYED_DIGEST.fullmatch(deployed) else ()
        else:
            digests = image.digests if image else ()

        def made(
            state: UpdateStateName,
            reason: str | None = None,
            remote: str | None = None,
        ) -> UpdateStatus:
            return UpdateStatus(
                reference.key,
                reference.familiar,
                state,
                reason,
                digests[0] if digests else None,
                remote,
                now,
                image.image_id if image else None,
            )

        if reference.pinned:
            return made("pinned")
        if not digests:
            return made("unknown", "no_local_digest")
        binding = stored.get(reference.key)
        fallback = binding is not None and binding.state == "credential_missing"
        target: RegistryTarget
        login: RegistryLogin | None
        set_id = binding.registry_credential_id if binding is not None else None
        if binding is not None and set_id is not None:
            if binding.credential_url != reference.registry_address:
                return made("unknown", "credential_mismatch")
            if authorise_set is not None:
                try:
                    await authorise_set(set_id)
                except (CredentialSetUnavailable, PermissionDenied):
                    scoped.add(reference.key)
                    return made("unknown", "credential_not_available")
            try:
                resolved = await self._resolver.resolve_login(
                    set_id,
                    environment_id=environment_id,
                    principal=principal,
                    registry_address=reference.registry_address,
                )
            except CredentialSetUnavailable as error:
                mismatch = error.reason == "credential_mismatch"
                return made(
                    "unknown", "credential_mismatch" if mismatch else "credential_not_available"
                )
            target, login = resolved.target, resolved.login
        else:
            target, login = public_target(reference.registry_address), None
        key: _Key = (environment_id, reference.key, set_id, tuple(sorted(digests)))
        verdict = await self._state.verdict(
            key, partial(self._compare, target, reference, login, digests)
        )
        refusal = verdict.reason
        if set_id is None and refusal == ManifestCheckKind.AUTHENTICATION.value:
            refusal = AUTHENTICATION_REQUIRED
        return replace(
            made(verdict.state, refusal, verdict.remote_digest),
            anonymous_fallback=fallback,
            missing_reason=binding.missing_reason if fallback and binding is not None else None,
        )

    async def _compare(
        self,
        target: RegistryTarget,
        reference: ImageReference,
        login: RegistryLogin | None,
        digests: tuple[str, ...],
    ) -> _Verdict:
        try:
            remote = await self._manifests.head_digest(target, reference, login)
        except ManifestCheckFailed as failure:
            return _Verdict("unknown", failure.kind.value, None)
        except Exception as error:
            logger.error("image_update_check_crashed", extra={"error": type(error).__name__})
            return _Verdict("unknown", "protocol", None)
        return _Verdict("up_to_date" if remote in digests else "update_available", None, remote)


_STATE_NAMES: Final = ("up_to_date", "update_available", "unknown", "pinned", "not_bound")


def is_image_id_reference(text: str) -> bool:
    """Whether the text is an image id, which Docker resolves before it reads a name."""
    return _IMAGE_ID_REFERENCE.fullmatch(text) is not None


def _parse(text: str) -> ImageReference | str:
    if is_image_id_reference(text):
        return text
    try:
        return parse_reference(text)
    except ReferenceInvalid:
        return text[:128]
