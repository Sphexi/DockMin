"""The Instance page's facts, the restart of the container dockmin runs in, and its update."""

import asyncio
import json
import re
import secrets
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from app.auth.errors import ProductError, ResourceNotFound
from app.auth.principal import Principal
from app.db.engine import DatabaseError, Session, retry_busy
from app.db.repositories.activity_event import ActivityEventRepository
from app.db.repositories.environment import ConnectionKind
from app.db.repositories.setting import SettingRepository
from app.docker_engine.errors import EnvironmentUnreachable, RequestOutcomeUnknown
from app.logging import get_logger
from app.registry_client.addresses import DOCKER_HUB_ADDRESS
from app.registry_client.manifests import ManifestCheckFailed, ManifestClient
from app.registry_client.probe import RegistryLogin, RegistryTarget
from app.registry_client.providers import ProviderKind, parse_extras
from app.registry_client.references import ImageReference, ReferenceInvalid, parse_reference
from app.registry_client.tags import TagClient, TagListFailed
from app.self_identity import SelfIdentity, SelfIdentitySource
from app.services.activity import ActivityRecorder, Arguments, audited, current_audit
from app.services.containers import display_name, short_id
from app.services.guards import require_administrator
from app.services.image_bindings import CredentialOffer, ImageBindingService
from app.services.image_updates import UpdateCheckState
from app.services.object_inspector import (
    ContainerTooLarge,
    EngineObjectInspector,
    EnvironmentUnavailable,
)
from app.services.pull_credentials import CredentialChoice
from app.services.registry_resolution import CredentialSetUnavailable, RegistryCredentialResolver
from app.services.restore import update_files_present
from app.services.restore_gate import RestoreGate
from app.services.self_update import (
    COMPOSE_UPDATE_COMMANDS,
    HELPER_SUFFIX,
    MAX_DISPLAYED_NAME,
    OUTCOME_SETTING,
    UPDATE_ID_PATTERN,
    UPDATE_REFUSALS,
    OwnContainer,
    UpdateDispatcher,
    UpdateJob,
    UpdatePhase,
    UpdateRefusal,
    UpdateRefused,
    find_request_event,
    new_update_id,
    read_own_container,
    repository_name,
    reserve_update,
    stop_timeout_of,
    update_root,
)
from app.services.self_update import DEFAULT_STOP_TIMEOUT as DEFAULT_STOP_TIMEOUT
from app.version import product_version

logger = get_logger(__name__)

BOOT_ID_BYTES: Final = 8
MAX_OFFERED_TAGS: Final = 10
RESTART_SEND_DELAY_SECONDS: Final = 0.5

type RestartRefusal = Literal[
    "not_containerised",
    "not_restartable",
    "restore_in_progress",
    "rekey_in_progress",
    "container_missing",
    "container_unreadable",
    "record_failed",
    "update_in_progress",
]

_REFUSALS: Final[dict[RestartRefusal, str]] = {
    "not_containerised": (
        "dockmin could not identify a container of its own, so it cannot restart itself."
    ),
    "not_restartable": (
        "dockmin could not confirm its container through an environment on the local Docker "
        "socket, so it cannot restart itself."
    ),
    "restore_in_progress": "A restore is in progress. Restart is unavailable until it finishes.",
    "rekey_in_progress": (
        "Stored secrets are being re-encrypted. Restart is unavailable until it finishes."
    ),
    "container_missing": "dockmin's container is no longer on the host that was used to find it.",
    "container_unreadable": "dockmin's container could not be read from the host.",
    "record_failed": "The restart could not be recorded, so it was not sent. Try again.",
    "update_in_progress": (
        "An update is in progress, or an earlier one left files in the data directory. "
        "Restart is unavailable until they are gone."
    ),
}
_SOURCE_LABELS: Final = {
    "mountinfo": "Mounts of /etc/hostname, /etc/hosts and /etc/resolv.conf",
    "cgroup": "Control group path",
}


class RestartRefused(ProductError):
    """Raised when the instance cannot be restarted now; the message is fixed product text."""

    def __init__(self, reason: RestartRefusal) -> None:
        super().__init__(_REFUSALS[reason])
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ProcessBoot:
    """Identifies one run of the process; it changes only when the process starts again."""

    boot_id: str
    started_at: datetime

    @classmethod
    def at(cls, started_at: datetime) -> "ProcessBoot":
        """Return a new boot with a random id."""
        return cls(secrets.token_hex(BOOT_ID_BYTES), started_at)


@dataclass(frozen=True, slots=True)
class HomeView:
    """An environment whose Engine reports the container dockmin runs in."""

    environment_id: int
    environment_name: str
    connection_kind: ConnectionKind
    container_name: str
    restarts_through: bool


@dataclass(frozen=True, slots=True)
class UnreadableView:
    """An environment that could not be read when dockmin looked for its own container."""

    environment_id: int
    environment_name: str


@dataclass(frozen=True, slots=True)
class UpdateOutcomeView:
    """How the latest update ended, as stored; `excerpt` is the new container's scrubbed log."""

    update_id: str
    outcome: str
    reason: str | None
    message: str
    finished_at: str
    request_event_id: int | None
    event_id: int | None
    previous_container_left: bool
    excerpt: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UpdateView:
    """What the Instance page shows about updating dockmin.

    `refusal` and `message` are set when no update is offered; `commands` then holds the Compose
    commands to run instead, for a container that belongs to a Compose project. `offer` is the
    credential picker for the check, set only when an update can be checked.
    """

    refusal: UpdateRefusal | None
    message: str | None
    commands: tuple[str, ...]
    reference: str | None
    digest: str | None
    version: str
    environment_id: int | None
    offer: CredentialOffer | None
    outcome: UpdateOutcomeView | None
    health_timeout_seconds: int
    stop_timeout: int | None

    @property
    def can_check(self) -> bool:
        """Report whether the page offers "Check for updates"."""
        return self.refusal is None and self.offer is not None


@dataclass(frozen=True, slots=True)
class InstanceView:
    """What the Instance page shows; the container fields are empty when not containerised."""

    containerised: bool
    container_id: str | None
    short_id: str | None
    container_name: str | None
    identified_by: str | None
    homes: tuple[HomeView, ...]
    restart_refusal: RestartRefusal | None
    restart_message: str | None
    boot: ProcessBoot
    unreadable: tuple[UnreadableView, ...] = ()
    incomplete: bool = False
    update: UpdateView | None = None

    @property
    def can_restart(self) -> bool:
        """Report whether a restart would be accepted now."""
        return self.restart_refusal is None


type SameTagState = Literal["current", "newer", "unknown", "pinned"]


@dataclass(frozen=True, slots=True)
class UpdateCandidate:
    """A tag an update can move to; `same_tag` marks the running tag at a new digest."""

    tag: str
    reference: str
    same_tag: bool
    digest: str | None


@dataclass(frozen=True, slots=True)
class UpdateCheck:
    """What a registry says about dockmin's own image.

    `same_tag_error` and `tags_error` hold the product text of a read that failed; the other
    half of the check still stands.
    """

    reference: str
    digest: str
    version: str
    registry_address: str
    credential_choice: str
    same_tag: SameTagState
    same_tag_digest: str | None
    same_tag_error: str | None
    candidates: tuple[UpdateCandidate, ...]
    tags_error: str | None


@dataclass(frozen=True, slots=True)
class UpdateConfirmation:
    """Everything the confirmation names before an update is requested."""

    container_id: str
    container_name: str
    current_reference: str
    current_digest: str
    target_tag: str
    target_reference: str
    target_digest: str
    helper_reference: str
    helper_digest: str
    stop_timeout: int
    health_timeout_seconds: int
    boot_id: str
    credential_choice: str
    registry_address: str


@dataclass(frozen=True, slots=True)
class UpdateReview:
    """The facts an update was reviewed against; a change to any of them refuses the request."""

    container_id: str
    boot_id: str
    current_digest: str
    target_tag: str
    target_digest: str
    helper_digest: str
    choice: CredentialChoice


@dataclass(frozen=True, slots=True)
class AcceptedUpdate:
    """An accepted update: its request row is committed, and `job` is handed to the dispatcher."""

    update_id: str
    container_name: str
    stop_timeout: int
    health_timeout_seconds: int
    job: UpdateJob


@dataclass(frozen=True, slots=True)
class UpdateStatusView:
    """Where an update is, for the status fragment; `unknown` when nothing is known of it."""

    update_id: str
    phase: UpdatePhase | Literal["unknown"]
    reason: str | None
    message: str | None
    outcome: str | None


@dataclass(frozen=True, slots=True)
class UpdateSupport:
    """What the service needs, beyond the restart, to check for updates and to request one."""

    data_dir: Path
    health_timeout_seconds: int
    bindings: ImageBindingService
    resolver: RegistryCredentialResolver
    manifests: ManifestClient
    checks: UpdateCheckState
    settings: SettingRepository
    events: ActivityEventRepository
    dispatcher: UpdateDispatcher


@dataclass(frozen=True, slots=True)
class RestartRequest:
    """An accepted restart: what was recorded, and the call that sends it.

    `send` is awaited once, after the response, and raises what the Engine call raises.
    """

    environment_id: int
    container_id: str
    container_name: str
    stop_timeout: int
    send: Callable[[], Awaitable[None]]


class RestartDispatcher:
    """Runs accepted restarts shortly after their response, keeping each task referenced."""

    def __init__(
        self,
        *,
        delay: float = RESTART_SEND_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._delay = delay
        self._sleep = sleep
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    def mark_stopping(self) -> None:
        """Record that the process is shutting down, so a dropped restart call is expected."""
        self._stopping = True

    @property
    def pending(self) -> int:
        """Return how many restarts have been dispatched and not yet finished."""
        return len(self._tasks)

    def dispatch(self, request: RestartRequest) -> None:
        """Send the restart after the delay; must be called from the running event loop."""
        task = asyncio.get_running_loop().create_task(self._send_after_delay(request))
        self._tasks |= {task}
        task.add_done_callback(self._forget)

    def _forget(self, task: asyncio.Task[None]) -> None:
        self._tasks -= {task}

    async def _send_after_delay(self, request: RestartRequest) -> None:
        try:
            await self._sleep(self._delay)
            await request.send()
        except Exception as error:
            extra = {"environment_id": request.environment_id, "error": type(error).__name__}
            if self._stopping and isinstance(error, EnvironmentUnreachable | RequestOutcomeUnknown):
                logger.info("instance_restart_sent", extra=extra)
            elif isinstance(error, RequestOutcomeUnknown):
                logger.warning("instance_restart_unconfirmed", extra=extra)
            else:
                logger.error("instance_restart_failed", extra=extra)


def restart_refusal(identity: SelfIdentity, gate: RestoreGate) -> RestartRefusal | None:
    """Return why a restart is not possible now, or None when it is."""
    if not identity.containerised:
        return "not_containerised"
    if identity.restartable_via is None:
        return "not_restartable"
    if gate.phase != "open":
        return "rekey_in_progress" if gate.purpose == "rekey" else "restore_in_progress"
    return None


def update_refusal(identity: SelfIdentity, gate: RestoreGate) -> UpdateRefusal | None:
    """Return why an update is not possible from what is already known, or None."""
    if not identity.containerised:
        return "not_containerised"
    if identity.incomplete:
        return "identity_incomplete"
    if identity.compose_project:
        return "compose_project"
    if identity.swarm_service:
        return "swarm_task"
    if identity.restartable_via is None:
        return "not_restartable"
    if gate.phase != "open":
        return "rekey_in_progress" if gate.purpose == "rekey" else "restore_in_progress"
    return None


def _container_id(arguments: Arguments) -> str | None:
    service: InstanceService = arguments["self"]
    return service.current_identity().container_id


_SEMANTIC_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


def _version_of(text: str) -> tuple[int, int, int] | None:
    found = _SEMANTIC_VERSION.fullmatch(text)
    return None if found is None else (int(found[1]), int(found[2]), int(found[3]))


def newer_tags(tags: tuple[str, ...], running: str) -> tuple[str, ...]:
    """Return the newest ten release tags newer than the running version, newest first.

    A running version that is not a release number is compared with nothing, so every release
    tag counts.
    """
    current = _version_of(running)
    found = []
    for tag in tags:
        version = _version_of(tag)
        if version is not None and (current is None or version > current):
            found.append((version, tag))
    found.sort(reverse=True)
    return tuple(tag for _, tag in found[:MAX_OFFERED_TAGS])


def _public_target(address: str) -> RegistryTarget:
    provider: ProviderKind = "dockerhub" if address == DOCKER_HUB_ADDRESS else "custom"
    return RegistryTarget(
        provider=provider,
        address=address,
        verify_tls=True,
        ca_bundle=None,
        extras=parse_extras(provider, {}),
    )


def _choice_value(choice: CredentialChoice) -> str:
    return "anonymous" if choice.credential_set_id is None else str(choice.credential_set_id)


def _outcome_view(text: str) -> UpdateOutcomeView | None:
    try:
        stored: Any = json.loads(text)
    except ValueError:
        return None
    if not isinstance(stored, dict):
        return None
    excerpt = stored.get("excerpt")
    request_event = stored.get("request_event_id")
    event = stored.get("event_id")
    reason = stored.get("reason")
    if not (
        isinstance(stored.get("update_id"), str)
        and isinstance(stored.get("outcome"), str)
        and isinstance(stored.get("message"), str)
        and isinstance(stored.get("finished_at"), str)
        and isinstance(excerpt, list)
    ):
        return None
    return UpdateOutcomeView(
        update_id=stored["update_id"],
        outcome=stored["outcome"],
        reason=reason if isinstance(reason, str) else None,
        message=stored["message"],
        finished_at=stored["finished_at"],
        request_event_id=request_event if isinstance(request_event, int) else None,
        event_id=event if isinstance(event, int) else None,
        previous_container_left=stored.get("previous_container_left") is True,
        excerpt=tuple(line for line in excerpt if isinstance(line, str)),
    )


class InstanceService:
    """Describes the container dockmin runs in and restarts it on an administrator's request."""

    def __init__(
        self,
        *,
        db: Session,
        activity: ActivityRecorder,
        inspector: EngineObjectInspector,
        identity: SelfIdentitySource,
        gate: RestoreGate,
        boot: ProcessBoot,
        update: UpdateSupport | None = None,
    ) -> None:
        self._db = db
        self._activity = activity
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._identity = identity
        self._gate = gate
        self._boot = boot
        self._update = update

    def current_identity(self) -> SelfIdentity:
        """Return the identity the process currently holds."""
        return self._identity()

    async def view(self, actor: Principal, *, with_update: bool = False) -> InstanceView:
        """Describe the instance for the Instance page; administrators only.

        `with_update` adds the update section, which reads the container and its image from the
        host; it is empty when the service was built without update support.
        """
        require_administrator(actor)
        identity = self._identity()
        homes = []
        for home in identity.verified:
            try:
                name = (await self._hosts.environment(home.environment_id)).name
            except ResourceNotFound:
                name = f"Environment {home.environment_id}"
            homes.append(
                HomeView(
                    home.environment_id,
                    name,
                    home.connection_kind,
                    home.container_name,
                    home.environment_id == identity.restartable_via,
                )
            )
        unreadable = []
        for environment_id in identity.unreadable:
            try:
                name = (await self._hosts.environment(environment_id)).name
            except ResourceNotFound:
                continue
            unreadable.append(UnreadableView(environment_id, name))
        refusal = await self._restart_refusal(identity)
        container_id = identity.container_id
        update = (
            await self._update_view(actor, identity)
            if with_update and self._update is not None
            else None
        )
        return InstanceView(
            containerised=identity.containerised,
            container_id=container_id,
            short_id=None if container_id is None else short_id(container_id),
            container_name=homes[0].container_name if homes else None,
            identified_by=None if identity.source is None else _SOURCE_LABELS[identity.source],
            homes=tuple(homes),
            restart_refusal=refusal,
            restart_message=None if refusal is None else _REFUSALS[refusal],
            boot=self._boot,
            unreadable=tuple(unreadable),
            incomplete=identity.incomplete,
            update=update,
        )

    @audited(
        action="instance_restart_requested",
        target_type="instance",
        target_id=_container_id,
    )
    async def request_restart(self, actor: Principal) -> RestartRequest:
        """Record a restart of dockmin's own container and return the call that sends it.

        The row is committed before this returns, and RestartRefused("record_failed") is raised
        when it cannot be written. The caller sends the restart only after the response has gone
        out, because the process is stopped by the restart it asks for.
        """
        require_administrator(actor)
        audit = current_audit()
        identity = self._identity()
        refusal = await self._restart_refusal(identity)
        if refusal is not None:
            audit.reason = refusal
            raise RestartRefused(refusal)
        environment_id = identity.restartable_via
        container_id = identity.container_id
        assert environment_id is not None and container_id is not None
        try:
            inspected = await self._inspector.inspect(environment_id, container_id)
            client = await self._hosts.client(environment_id)
        except ResourceNotFound:
            audit.reason = "container_missing"
            raise RestartRefused("container_missing") from None
        except ContainerTooLarge:
            audit.reason = "container_unreadable"
            raise RestartRefused("container_unreadable") from None
        timeout = stop_timeout_of(inspected.config.stop_timeout)
        name = display_name([inspected.name], inspected.id)[:MAX_DISPLAYED_NAME]
        audit.detail["container_name"] = name
        audit.detail["environment_id"] = environment_id
        audit.detail["stop_timeout"] = timeout
        if not await self._commit_row():
            raise RestartRefused("record_failed")

        async def send() -> None:
            await client.containers.restart(
                container_id, grace_seconds=timeout, daemon_wait_seconds=timeout
            )

        return RestartRequest(environment_id, container_id, name, timeout, send)

    async def _restart_refusal(self, identity: SelfIdentity) -> RestartRefusal | None:
        refusal = restart_refusal(identity, self._gate)
        if refusal is None and self._update is not None and await self._update_running():
            return "update_in_progress"
        return refusal

    async def _update_running(self) -> bool:
        support = self._update_support()
        return support.dispatcher.busy() or await asyncio.to_thread(
            update_files_present, support.data_dir
        )

    async def _commit_row(self) -> bool:
        async def commit() -> None:
            async with self._db.transaction():
                pass

        try:
            await retry_busy(commit)
        except (sqlite3.Error, DatabaseError):
            current_audit().reason = "record_failed"
            return False
        return True

    def _update_support(self) -> UpdateSupport:
        if self._update is None:
            raise RuntimeError("the service was built without update support")
        return self._update

    async def _eligible(self, identity: SelfIdentity) -> tuple[OwnContainer, int]:
        """Apply every refusal and return the own container with the environment it is read in."""
        support = self._update_support()
        refusal = update_refusal(identity, self._gate)
        if refusal is not None:
            raise UpdateRefused(refusal)
        if await self._update_running():
            raise UpdateRefused("update_in_progress")
        environment_id = identity.restartable_via
        container_id = identity.container_id
        assert environment_id is not None and container_id is not None
        try:
            environment = await self._hosts.environment(environment_id)
            client = await self._hosts.client(environment_id)
        except ResourceNotFound:
            raise UpdateRefused("container_missing") from None
        if environment.socket_path is None:
            raise UpdateRefused("not_restartable")
        own = await read_own_container(
            client,
            container_id=container_id,
            socket_path=environment.socket_path,
            data_dir=support.data_dir,
        )
        return own, environment_id

    async def _update_view(self, actor: Principal, identity: SelfIdentity) -> UpdateView:
        support = self._update_support()
        refusal: UpdateRefused | None = None
        own: OwnContainer | None = None
        environment_id: int | None = None
        try:
            own, environment_id = await self._eligible(identity)
        except UpdateRefused as error:
            refusal = error
        except EnvironmentUnavailable:
            refusal = UpdateRefused("container_unreadable")
        offer: CredentialOffer | None = None
        if own is not None and environment_id is not None:
            try:
                offer = await support.bindings.picker(actor, environment_id, own.reference_text)
            except ProductError:
                refusal = UpdateRefused("credential_unavailable")
        stored = await support.settings.get(OUTCOME_SETTING)
        return UpdateView(
            refusal=None if refusal is None else refusal.reason,
            message=None if refusal is None else str(refusal),
            commands=COMPOSE_UPDATE_COMMANDS
            if refusal is not None and refusal.reason == "compose_project"
            else (),
            reference=None if own is None else own.reference.familiar,
            digest=None if own is None else own.digest,
            version=product_version(),
            environment_id=environment_id,
            offer=offer,
            outcome=None if stored is None else _outcome_view(stored.value),
            health_timeout_seconds=support.health_timeout_seconds,
            stop_timeout=None if own is None else own.stop_timeout,
        )

    async def _registry_access(
        self,
        actor: Principal,
        environment_id: int,
        reference: ImageReference,
        choice: CredentialChoice,
    ) -> tuple[RegistryTarget, RegistryLogin | None]:
        """Resolve the chosen credential set to a registry target; never inside a transaction."""
        support = self._update_support()
        address = reference.registry_address
        if choice.credential_set_id is None:
            return _public_target(address), None
        try:
            resolved = await support.resolver.resolve_login(
                choice.credential_set_id,
                environment_id=environment_id,
                principal=actor,
                registry_address=address,
            )
        except CredentialSetUnavailable as error:
            raise UpdateRefused("credential_unavailable", str(error)) from None
        return resolved.target, resolved.login

    async def check_update(self, actor: Principal, choice: CredentialChoice) -> UpdateCheck:
        """Ask the registry about dockmin's own image; administrators only.

        Reads the digest of the running tag and the tag list, each of which may fail on its own.
        Raises UpdateRefused, TooManyUpdateChecks and EnvironmentUnavailable.
        """
        require_administrator(actor)
        support = self._update_support()
        own, environment_id = await self._eligible(self._identity())
        reference = own.reference
        version = product_version()
        with support.checks.request(actor.user_id):
            target, login = await self._registry_access(actor, environment_id, reference, choice)
            same_digest, tags = await asyncio.gather(
                self._same_tag_digest(target, reference, login),
                self._release_tags(target, reference, login),
            )
        state: SameTagState
        if reference.pinned:
            state = "pinned"
        elif same_digest[0] is None:
            state = "unknown"
        else:
            state = "current" if same_digest[0] == own.digest else "newer"
        name = repository_name(reference)
        candidates = []
        if state == "newer" and reference.tag is not None:
            candidates.append(
                UpdateCandidate(reference.tag, f"{name}:{reference.tag}", True, same_digest[0])
            )
        candidates.extend(
            UpdateCandidate(tag, f"{name}:{tag}", False, None)
            for tag in newer_tags(tags[0], version)
        )
        return UpdateCheck(
            reference=reference.familiar,
            digest=own.digest,
            version=version,
            registry_address=reference.registry_address,
            credential_choice=_choice_value(choice),
            same_tag=state,
            same_tag_digest=same_digest[0],
            same_tag_error=same_digest[1],
            candidates=tuple(candidates),
            tags_error=tags[1],
        )

    async def _same_tag_digest(
        self, target: RegistryTarget, reference: ImageReference, login: RegistryLogin | None
    ) -> tuple[str | None, str | None]:
        if reference.pinned:
            return None, None
        try:
            return await self._update_support().manifests.head_digest(
                target, reference, login
            ), None
        except ManifestCheckFailed as failure:
            return None, failure.message
        except ValueError:
            return None, UPDATE_REFUSALS["registry_failed"]

    async def _release_tags(
        self, target: RegistryTarget, reference: ImageReference, login: RegistryLogin | None
    ) -> tuple[tuple[str, ...], str | None]:
        try:
            tags = await TagClient(self._update_support().manifests).list_tags(
                target, reference, login
            )
        except TagListFailed as failure:
            return (), failure.message
        except ValueError:
            return (), UPDATE_REFUSALS["registry_failed"]
        return tags, None

    def _references_of(self, own: OwnContainer, tag: str) -> tuple[ImageReference, ImageReference]:
        """Return the image and the helper image an update to the tag would run."""
        version = _version_of(tag)
        running = _version_of(product_version())
        same = not own.reference.pinned and tag == own.reference.tag
        newer = version is not None and (running is None or version > running)
        if _TAG.fullmatch(tag) is None or not (same or newer):
            raise UpdateRefused("target_invalid")
        name = repository_name(own.reference)
        try:
            return (
                parse_reference(f"{name}:{tag}"),
                parse_reference(f"{name}{HELPER_SUFFIX}:{tag}"),
            )
        except ReferenceInvalid:
            raise UpdateRefused("target_invalid") from None

    async def _digests(
        self,
        actor: Principal,
        environment_id: int,
        references: tuple[ImageReference, ImageReference],
        choice: CredentialChoice,
    ) -> tuple[str, str]:
        """Read the registry's digest of the image and of its helper with one credential choice."""
        registry, login = await self._registry_access(actor, environment_id, references[0], choice)
        manifests = self._update_support().manifests

        async def head(reference: ImageReference, subject: str) -> str:
            try:
                return await manifests.head_digest(registry, reference, login)
            except ManifestCheckFailed as failure:
                raise UpdateRefused(
                    "registry_failed", f"The {subject} could not be read: {failure.message}"
                ) from None
            except ValueError:
                raise UpdateRefused("registry_failed") from None

        digest, helper = await asyncio.gather(
            head(references[0], "new image"), head(references[1], "update helper image")
        )
        return digest, helper

    async def confirm_update(
        self, actor: Principal, choice: CredentialChoice, target_tag: str
    ) -> UpdateConfirmation:
        """Name what an update to the tag would replace and run, reading the registry's digests.

        Administrators only. Nothing is recorded or changed. Raises UpdateRefused.
        """
        require_administrator(actor)
        support = self._update_support()
        own, environment_id = await self._eligible(self._identity())
        references = self._references_of(own, target_tag)
        with support.checks.request(actor.user_id):
            digest, helper_digest = await self._digests(actor, environment_id, references, choice)
        return UpdateConfirmation(
            container_id=own.container_id,
            container_name=own.name,
            current_reference=own.reference.familiar,
            current_digest=own.digest,
            target_tag=target_tag,
            target_reference=f"{repository_name(own.reference)}:{target_tag}",
            target_digest=digest,
            helper_reference=f"{repository_name(own.reference)}{HELPER_SUFFIX}:{target_tag}",
            helper_digest=helper_digest,
            stop_timeout=own.stop_timeout,
            health_timeout_seconds=support.health_timeout_seconds,
            boot_id=self._boot.boot_id,
            credential_choice=_choice_value(choice),
            registry_address=own.reference.registry_address,
        )

    @audited(
        action="instance_update_requested",
        target_type="instance",
        target_id=_container_id,
    )
    async def request_update(self, actor: Principal, review: UpdateReview) -> AcceptedUpdate:
        """Record an update of dockmin's own container and return the job that carries it out.

        Every refusal is applied again, and the boot, container, current digest and both target
        digests must be the ones reviewed. The update's directory is created, and the update
        claimed in the dispatcher, before the row is committed, so a second request is refused
        from then on. The row is committed before this returns, and before anything is pulled.
        The caller dispatches the job after the response has gone out.
        """
        require_administrator(actor)
        audit = current_audit()
        support = self._update_support()
        dispatcher = support.dispatcher
        update_id = new_update_id()
        try:
            async with dispatcher.single_flight():
                try:
                    own, environment_id = await self._eligible(self._identity())
                    stale = (
                        review.boot_id != self._boot.boot_id
                        or review.container_id != own.container_id
                        or review.current_digest != own.digest
                    )
                    if stale:
                        audit.refuse()
                        raise UpdateRefused("stale_review")
                    references = self._references_of(own, review.target_tag)
                    with support.checks.request(actor.user_id):
                        digests = await self._digests(
                            actor, environment_id, references, review.choice
                        )
                    if digests != (review.target_digest, review.helper_digest):
                        audit.refuse()
                        raise UpdateRefused("digest_changed")
                except UpdateRefused as error:
                    audit.reason = error.reason
                    raise
                dispatcher.claim(update_id)
                try:
                    await asyncio.to_thread(reserve_update, support.data_dir, update_id)
                except OSError:
                    dispatcher.release(update_id)
                    audit.reason = "record_failed"
                    raise UpdateRefused("record_failed") from None
            name = repository_name(own.reference)
            audit.detail.update(
                {
                    "update_id": update_id,
                    "container_name": own.name,
                    "environment_id": environment_id,
                    "current_reference": own.reference.familiar,
                    "current_digest": own.digest,
                    "target_reference": f"{name}:{review.target_tag}",
                    "target_digest": digests[0],
                    "helper_digest": digests[1],
                    "credential_set_id": review.choice.credential_set_id,
                    "stop_timeout": own.stop_timeout,
                    "health_timeout": support.health_timeout_seconds,
                }
            )
            if not await self._commit_row():
                raise UpdateRefused("record_failed")
            row = await find_request_event(
                support.events, actor.user_id, own.container_id, update_id
            )
            if row is None:
                raise UpdateRefused("record_failed")
        except BaseException:
            dispatcher.discard(update_id)
            raise
        job = UpdateJob(
            update_id=update_id,
            principal=actor,
            request_event_id=row.id,
            environment_id=environment_id,
            container_id=own.container_id,
            container_name=own.name,
            target_reference=f"{name}:{review.target_tag}",
            helper_reference=f"{name}{HELPER_SUFFIX}:{review.target_tag}",
            target_digest=digests[0],
            helper_digest=digests[1],
            choice=review.choice,
            health_timeout_seconds=support.health_timeout_seconds,
        )
        return AcceptedUpdate(
            update_id, own.name, own.stop_timeout, support.health_timeout_seconds, job
        )

    async def update_status(self, actor: Principal, update_id: str) -> UpdateStatusView:
        """Report where an update is; administrators only.

        A process that knows nothing of the update reads the stored outcome, then the update
        directory, and otherwise reports `unknown`.
        """
        require_administrator(actor)
        support = self._update_support()
        if UPDATE_ID_PATTERN.fullmatch(update_id) is None:
            raise ResourceNotFound("not found")
        progress = support.dispatcher.progress(update_id)
        if progress is not None:
            return UpdateStatusView(
                update_id, progress.phase, progress.reason, progress.message, progress.outcome
            )
        stored = await support.settings.get(OUTCOME_SETTING)
        outcome = None if stored is None else _outcome_view(stored.value)
        if outcome is not None and outcome.update_id == update_id:
            return UpdateStatusView(
                update_id, "finished", outcome.reason, outcome.message, outcome.outcome
            )
        directory = update_root(support.data_dir) / update_id
        if await asyncio.to_thread(directory.is_dir):
            return UpdateStatusView(update_id, "handed_over", None, None, None)
        return UpdateStatusView(update_id, "unknown", None, None, None)
