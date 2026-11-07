"""Errors, verification policy and the shared steps of container creation and recreation."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Final, Literal, Protocol

from pydantic import SecretStr

from app.auth.errors import PermissionDenied, ProductError, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Database, Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.db.repositories.environment import Environment
from app.db.repositories.resource_control import ResourceControlRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import (
    Conflict,
    EngineError,
    EnvironmentUnreachable,
    RequestOutcomeUnknown,
    ResponseTooLarge,
)
from app.docker_engine.errors import NotFound as EngineNotFound
from app.docker_engine.models import ImageInspect, Network, PullProgress, Volume
from app.logging import get_logger
from app.registry_client.references import ImageReference, parse_reference
from app.scheduler.model import RunSkipped
from app.self_identity import SelfIdentitySource, no_identity
from app.services.access import ResourceKey, is_canonical_id, stack_of
from app.services.acting import Acting, AutomationAuthority, actor_of, is_administrator
from app.services.activity import ActivityRecorder, Actor, UserActor, current_audit
from app.services.authorisation import Authoriser
from app.services.container_derivation import BLOCKER_MESSAGES, Blocker, DaemonDefaults, Derivation
from app.services.container_restrictions import (
    Finding,
    find_restricted,
    needs_default_runtime,
    reach_finding,
    refused_options,
    require_no_self_reach,
)
from app.services.container_review import as_mapping, endpoint_keys, volume_uses
from app.services.container_spec import (
    ContainerSpec,
    FormState,
    parse_form,
    row_slots,
    to_engine_body,
)
from app.services.host_objects import in_transaction
from app.services.image_bindings import ImageBindingService
from app.services.object_inspector import (
    NETWORK_ID,
    VOLUME_OR_NETWORK_NAME,
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
    unavailable_from,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.pull_credentials import CredentialChoice, PullCredentialResolver, PullCredentials
from app.services.resource_controls import ResourceControlService

__all__ = [
    "MAX_VERIFY_SECONDS",
    "NETWORK_UNAVAILABLE",
    "START_FAILED",
    "VOLUME_UNAVAILABLE",
    "Claim",
    "ConfigurationRejected",
    "ControlStep",
    "Copy",
    "CreateRefused",
    "CreateRun",
    "Failure",
    "NameChanged",
    "NameInUse",
    "NotRecreatable",
    "NotRunning",
    "ProvisionFeed",
    "ProvisioningBase",
    "PullFailed",
    "RecreateFailed",
    "RecreateRun",
    "RestrictedOptions",
    "RolledBack",
    "SeenVolume",
    "Stage",
    "StepKind",
    "Submission",
    "SubmissionRejected",
    "Transfer",
    "VerifyPolicy",
    "VolumeChanged",
    "VolumeClaim",
    "failure_of",
    "field_names",
    "note_stage",
    "settle_seconds",
    "step_kind",
]

logger = get_logger(__name__)

SETTLE_WAIT_SECONDS: Final = 120.0

MAX_VERIFY_SECONDS: Final = 300

IMAGE_MISSING: Final = "The image is not on this host."

PULL_FAILED: Final = "The image could not be pulled."

NAME_IN_USE: Final = "A container with this name already exists."

CONFIGURATION_REFUSED: Final = "Docker refused this configuration."

START_FAILED: Final = "Created, but Docker could not start it."

VOLUME_UNAVAILABLE: Final = "No volume with this name is available to you."

NETWORK_UNAVAILABLE: Final = "No network with this name is available to you."

NETWORK_MISSING: Final = "No network with this name exists on this host."

NETWORK_DUPLICATE: Final = "This network is already listed."

CHANGED_SINCE_REVIEW: Final = "This container changed since you reviewed it; nothing was recreated."

VOLUME_CHANGED: Final = (
    "A volume changed while the container was being created. The container was not kept."
)

type Stage = Literal["stop", "rename", "create", "start", "verify"]


class ProvisionFeed(Protocol):
    """Receives the steps of a create or recreate that runs as a job, and the pull's progress.

    `cancel_requested` is true once the starter asked to cancel rather than the process closing.
    """

    @property
    def cancel_requested(self) -> bool:
        """Report whether the starter asked to cancel."""
        ...

    def on_pull(self, event: PullProgress) -> None:
        """Take one pull progress message."""
        ...

    def on_stage(self, stage: str) -> None:
        """Take the step the sequence has reached."""
        ...


type StepKind = Literal["unknown", "unreachable", "not_found", "conflict", "rejected"]


class SubmissionRejected(ValidationFailed):
    """A create submission or recreate configuration refused with named fields.

    fields maps a form field name (a group name for a group error) to the message. state holds
    every submitted value with the errors attached for re-rendering the form; it is None for a
    recreate, whose fields are keyed by Engine path. reason and detail go on the failure row.
    """

    def __init__(
        self,
        fields: Mapping[str, str],
        *,
        state: FormState | None,
        reason: str,
        detail: Mapping[str, DetailValue] | None = None,
    ) -> None:
        super().__init__(dict(fields))
        self.state = state
        self.reason = reason
        self.detail: dict[str, DetailValue] = dict(detail or {})


class RestrictedOptions(SubmissionRejected):
    """A standard user's configuration uses options only administrators may set.

    findings names each use, refused the distinct option ids in vocabulary order.
    """

    def __init__(
        self,
        findings: Sequence[Finding],
        fields: Mapping[str, str],
        *,
        state: FormState | None,
    ) -> None:
        refused = refused_options(findings)
        super().__init__(
            fields,
            state=state,
            reason="restricted_option",
            detail={"refused": ",".join(refused)},
        )
        self.findings = tuple(findings)
        self.refused = refused


class NameChanged(ValidationFailed):
    """The container's name is no longer the one the caller reviewed; nothing was recreated."""

    def __init__(self) -> None:
        super().__init__({"name": CHANGED_SINCE_REVIEW})


class NotRecreatable(ProductError):
    """The container cannot be recreated; the message names every reason."""

    def __init__(self, blockers: Sequence[Blocker]) -> None:
        super().__init__(" ".join(BLOCKER_MESSAGES[blocker] for blocker in blockers))
        self.blockers = tuple(blockers)


class NotRunning(ProductError):
    """A verified recreate was refused because the container is not running; nothing was touched."""

    def __init__(self) -> None:
        super().__init__("The container is not running.")


class CreateRefused(ProductError):
    """Docker refused a create; field_name is the form field the message belongs beside, if any."""

    field_name: str | None = None


class NameInUse(CreateRefused):
    """A container with the requested name already exists."""

    field_name = "name"

    def __init__(self) -> None:
        super().__init__(NAME_IN_USE)


class PullFailed(CreateRefused):
    """The image could not be pulled; the registry's text is never carried."""

    field_name = "image"

    def __init__(self) -> None:
        super().__init__(PULL_FAILED)


class ConfigurationRejected(CreateRefused):
    """Docker refused the configuration; the Engine's text is never part of the message.

    engine_detail is that text, scrubbed and cut, for a page shown to an administrator. It is
    never logged or recorded.
    """

    def __init__(self, engine_detail: str | None = None) -> None:
        super().__init__(CONFIGURATION_REFUSED)
        self.engine_detail = engine_detail


class VolumeChanged(CreateRefused):
    """A volume the container mounts turned out to be backed by a host path or a plugin.

    rolled_back is true when the container just created was removed again.
    """

    def __init__(self, *, rolled_back: bool) -> None:
        super().__init__(VOLUME_CHANGED)
        self.rolled_back = rolled_back


class RecreateFailed(ProductError):
    """A recreate did not complete.

    rolled_back is true when the original container was restored; outcome_unknown when a call
    timed out and the state of the host is not known. stage is the step that failed and
    failed_container_id the new container that was removed, when one had been created;
    verify_failure names why verification failed. engine_detail is the Engine's scrubbed text
    for a page shown to an administrator; it is never logged or recorded. settled_late is set
    on a failure that ended after the interrupted request stopped waiting for it.
    """

    def __init__(
        self,
        message: str,
        *,
        rolled_back: bool = False,
        outcome_unknown: bool = False,
        stage: Stage | None = None,
        failed_container_id: str | None = None,
        verify_failure: str | None = None,
        engine_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.engine_detail = engine_detail
        self.rolled_back = rolled_back
        self.outcome_unknown = outcome_unknown
        self.stage = stage
        self.failed_container_id = failed_container_id
        self.verify_failure = verify_failure
        self.settled_late = False


type RolledBack = Callable[[RecreateFailed, ActivityRecorder], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class VerifyPolicy:
    """How long an automated recreate watches the new container before removing the old one.

    A healthcheck that is still starting extends the watch past `seconds`, up to its own
    start period, interval and timeout, when `require_healthy` is set.
    """

    seconds: int
    require_healthy: bool
    interval: float = 2.0

    def __post_init__(self) -> None:
        """Refuse a window or interval outside what a run can wait out."""
        if not 0 <= self.seconds <= MAX_VERIFY_SECONDS:
            raise ValueError(f"seconds is between 0 and {MAX_VERIFY_SECONDS}")
        if self.interval <= 0:
            raise ValueError("interval is positive")

    @property
    def longest_window(self) -> float:
        """Return the longest the watch can last."""
        longest = max(self.seconds, MAX_VERIFY_SECONDS) if self.require_healthy else self.seconds
        return float(longest)


@dataclass(frozen=True, slots=True)
class SeenVolume:
    driver: str
    options: Mapping[str, str]
    created_at: str
    level: Literal["read", "write"]


@dataclass(slots=True)
class _Reach:
    errors: dict[str, str] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    network_ids: dict[str, str] = field(default_factory=dict)
    absent_volumes: list[str] = field(default_factory=list)
    seen_volumes: dict[str, SeenVolume] = field(default_factory=dict)
    locked_volumes: set[str] = field(default_factory=set)


type _HoldVolume = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Submission:
    environment: Environment
    client: EngineClient
    spec: ContainerSpec
    state: FormState
    body: dict[str, Any]
    restricted: tuple[str, ...]
    will_pull: bool
    credentials: PullCredentials
    reference: ImageReference | None = None
    volume_sources: frozenset[str] = frozenset()
    absent_volumes: tuple[str, ...] = ()
    restricted_actor: bool = False
    inherits_volumes: bool = False
    actor: Principal | None = None
    seen_volumes: Mapping[str, SeenVolume] = field(default_factory=dict)
    stale_controls: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VolumeClaim:
    name: str
    anonymous: bool
    stale: bool = False


@dataclass(slots=True)
class CreateRun:
    feed: ProvisionFeed | None = None
    pulling: bool = False
    abandoned: bool = False
    pulled: bool = False
    digest: str | None = None
    container_id: str | None = None
    started: bool = False
    start_failed: bool = False
    volumes: tuple[VolumeClaim, ...] = ()
    volumes_preexisting: int = 0


@dataclass(slots=True)
class RecreateRun:
    feed: ProvisionFeed | None = None
    pulling: bool = False
    pulled: bool = False
    digest: str | None = None
    stopped: bool = False
    renamed: bool = False
    new_id: str | None = None
    started: bool = False
    old_removed: bool = False
    remove_outcome_unknown: bool = False
    abandoned: bool = False
    verifying: bool = False
    interrupt: asyncio.Event = field(default_factory=asyncio.Event)
    observed_failure: str | None = None
    changed: tuple[str, ...] = ()
    image_changed: bool = False
    verified: bool | None = None


@dataclass(frozen=True, slots=True)
class Claim:
    key: ResourceKey
    owner_user_id: int | None
    administrators_only: bool = False
    volumes: tuple[VolumeClaim, ...] = ()


@dataclass(frozen=True, slots=True)
class _Rows:
    recorder: ActivityRecorder
    detail: dict[str, DetailValue]
    related: bool


@dataclass(frozen=True, slots=True)
class Transfer:
    old: ResourceKey
    new: ResourceKey


@dataclass(frozen=True, slots=True)
class Copy:
    old: ResourceKey
    new: ResourceKey


@dataclass(frozen=True, slots=True)
class Failure:
    reason: str
    message: str | None


type ControlStep = Claim | Transfer | Copy


def note_stage(run: CreateRun | RecreateRun, stage: str) -> None:
    if run.feed is not None:
        run.feed.on_stage(stage)


def settle_seconds(stop_wait: int | None = None, verify: VerifyPolicy | None = None) -> float:
    watch = 0.0 if verify is None else verify.longest_window
    return SETTLE_WAIT_SECONDS + (stop_wait or 0) + watch


def step_kind(error: EngineError) -> StepKind:
    if isinstance(error, RequestOutcomeUnknown):
        return "unknown"
    if isinstance(error, EnvironmentUnreachable):
        return "unreachable"
    if isinstance(error, EngineNotFound):
        return "not_found"
    if isinstance(error, Conflict):
        return "conflict"
    return "rejected"


def field_names(fields: Mapping[str, str]) -> str:
    return ",".join(sorted(fields))


def _invalid(state: FormState, fields: Mapping[str, str]) -> SubmissionRejected:
    return SubmissionRejected(
        fields,
        state=state.with_errors(fields),
        reason="invalid",
        detail={"fields": field_names(fields)},
    )


def _control_key(step: ControlStep) -> ResourceKey:
    return step.key if isinstance(step, Claim) else step.new


def failure_of(error: BaseException, reason: str | None) -> Failure:
    if isinstance(error, ProductError):
        return Failure(reason or type(error).__name__, str(error))
    return Failure(reason or type(error).__name__, None)


class ProvisioningBase:
    """Shared state and steps of container creation and recreation."""

    def __init__(
        self,
        *,
        db: Session,
        database: Database,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        locks: ObjectLockRegistry,
        controls: ResourceControlService,
        activity: ActivityRecorder,
        clock: Clock,
        credentials: PullCredentialResolver,
        bindings: ImageBindingService | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        self_identity: SelfIdentitySource | None = None,
    ) -> None:
        self._sleep = sleep
        self._self_identity = self_identity or no_identity
        self._db = db
        self._database = database
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._locks = locks
        self._controls = controls
        self._activity = activity
        self._clock = clock
        self._credentials = credentials
        self._bindings = bindings
        self._background: set[asyncio.Future[None]] = set()

    async def _prepare(
        self,
        actor: Principal,
        environment_id: int,
        form: Mapping[str, Any],
        *,
        choice: CredentialChoice | None = None,
        resolve: bool = True,
        held: AsyncExitStack | None = None,
    ) -> Submission:
        await self._authoriser.require_create(actor, environment_id)
        environment = await self._hosts.environment(environment_id)
        parsed = parse_form(form)
        spec = parsed.spec
        state = parsed.state
        if spec is None:
            raise SubmissionRejected(
                state.errors,
                state=state,
                reason="invalid",
                detail={"fields": field_names(state.errors)},
            )
        client = await self._hosts.client(environment_id)
        slots = row_slots(spec)
        try:
            draft = to_engine_body(spec)
        except ValidationFailed as error:
            raise _invalid(state, error.fields) from None
        reach = await self._reach(
            actor,
            environment_id,
            draft,
            slots=slots,
            own_volumes=frozenset(),
            scan_absent=resolve,
            hold=(
                partial(self._hold_volume, held, actor, environment_id)
                if resolve and held is not None
                else None
            ),
        )
        body = to_engine_body(spec, network_ids=reach.network_ids)
        runtime = (
            (await self._daemon_defaults(client)).runtime if needs_default_runtime(body) else None
        )
        findings = find_restricted(body, default_runtime=runtime, slots=slots)
        restricted: tuple[str, ...] = ()
        if actor.is_administrator:
            restricted = refused_options(findings)
        else:
            findings = [*findings, *reach.findings]
            if findings:
                fields = {finding.key: finding.message for finding in reversed(findings)}
                fields.update(reach.errors)
                raise RestrictedOptions(findings, fields, state=state.with_errors(fields))
        if reach.errors:
            raise _invalid(state, reach.errors)
        require_no_self_reach(body, environment_id=environment_id, identity=self._self_identity())
        present = spec.pull != "always" and await self._image_present(client, spec.image)
        if spec.pull == "never" and not present:
            fields = {"image": IMAGE_MISSING}
            raise SubmissionRejected(
                fields,
                state=state.with_errors(fields),
                reason="image_missing",
                detail={"fields": "image"},
            )
        will_pull = not present
        reference = parse_reference(spec.image) if will_pull else None
        if reference is not None and resolve and held is not None:
            await self._hold_reference(held, actor, environment_id, reference, "create")
        credentials = (
            await self._credentials.resolve(
                environment_id, spec.image, principal=actor, choice=choice
            )
            if will_pull and resolve
            else PullCredentials()
        )
        host = as_mapping(body.get("HostConfig"))
        return Submission(
            environment,
            client,
            spec,
            state,
            body,
            restricted,
            will_pull,
            credentials,
            reference,
            volume_sources=frozenset(use[0] for use in volume_uses(host, None)),
            absent_volumes=tuple(dict.fromkeys(reach.absent_volumes)),
            restricted_actor=not actor.is_administrator,
            inherits_volumes=bool(host.get("VolumesFrom")),
            actor=actor,
            seen_volumes=reach.seen_volumes,
            stale_controls=(
                await self._stale_controls(environment_id, reach.absent_volumes) if resolve else {}
            ),
        )

    async def _require_object(
        self, actor: Acting, key: ResourceKey, level: Literal["read", "write"]
    ) -> None:
        if not isinstance(actor, AutomationAuthority):
            await self._authoriser.require_access(actor, key, level)
            return
        object_key = ObjectKey(key.environment_id, key.resource_type, key.resource_id)
        if level == "write":
            await actor.authorise(object_key)
        else:
            await actor.authorise_read(object_key)

    async def _reach(
        self,
        actor: Acting,
        environment_id: int,
        body: Mapping[str, Any],
        *,
        slots: Mapping[str, Sequence[int]] | None,
        own_volumes: frozenset[str],
        own_networks: frozenset[str] = frozenset(),
        scan_absent: bool = False,
        hold: _HoldVolume | None = None,
    ) -> _Reach:
        reach = _Reach()
        host = as_mapping(body.get("HostConfig"))
        if not is_administrator(actor):
            for name, read_only, path, field_name in volume_uses(host, slots):
                await self._check_volume(
                    actor,
                    environment_id,
                    name,
                    read_only,
                    path,
                    field_name,
                    reach,
                    own=name in own_volumes,
                    hold=hold,
                )
        elif scan_absent:
            await self._note_absent_volumes(environment_id, host, reach, hold)
        rows = None if slots is None else slots.get("networks")
        resolved: set[str] = set()
        for ordinal, key in enumerate(endpoint_keys(body)):
            if key in own_networks:
                continue
            row = rows[ordinal] if rows is not None and ordinal < len(rows) else ordinal
            field_name = None if slots is None else f"network-{row}-name"
            path = f"NetworkingConfig.EndpointsConfig[{key if len(key) <= 64 else '<network>'}]"
            await self._check_network(actor, environment_id, key, path, field_name, reach)
            network_id = reach.network_ids.get(key)
            if network_id is None:
                continue
            if network_id in resolved:
                reach.errors.setdefault(field_name or path, NETWORK_DUPLICATE)
            resolved.add(network_id)
        return reach

    async def _check_volume(
        self,
        actor: Acting,
        environment_id: int,
        name: str,
        read_only: bool,
        path: str,
        field_name: str | None,
        reach: _Reach,
        *,
        own: bool,
        hold: _HoldVolume | None,
    ) -> None:
        if VOLUME_OR_NETWORK_NAME.fullmatch(name) is None:
            reach.errors.setdefault(field_name or path, VOLUME_UNAVAILABLE)
            return
        level: Literal["read", "write"] = "read" if read_only else "write"
        try:
            volume = await self._inspector.volume(environment_id, name)
        except ResourceNotFound:
            found = await self._confirm_absent(environment_id, name, reach, hold)
            if found is None:
                return
            volume = found
        if not own:
            try:
                await self._require_object(
                    actor, ResourceKey(environment_id, "volume", name), level
                )
            except (ResourceNotFound, PermissionDenied):
                reach.errors.setdefault(field_name or path, VOLUME_UNAVAILABLE)
                return
        earlier = reach.seen_volumes.get(name)
        reach.seen_volumes[name] = SeenVolume(
            volume.driver,
            dict(volume.options),
            volume.created_at,
            "write" if earlier is not None and earlier.level == "write" else level,
        )
        if volume.driver != "local" or volume.options:
            reach.findings.append(reach_finding("host_backed_volume", path, field_name))

    async def _note_absent_volumes(
        self,
        environment_id: int,
        host: Mapping[str, Any],
        reach: _Reach,
        hold: _HoldVolume | None,
    ) -> None:
        for name in dict.fromkeys(use[0] for use in volume_uses(host, None)):
            try:
                await self._inspector.volume(environment_id, name)
            except ResourceNotFound:
                await self._confirm_absent(environment_id, name, reach, hold)
            except ObjectTooLarge:
                continue

    async def _confirm_absent(
        self, environment_id: int, name: str, reach: _Reach, hold: _HoldVolume | None
    ) -> Volume | None:
        if hold is not None and name not in reach.locked_volumes:
            await hold(name)
            reach.locked_volumes.add(name)
            try:
                return await self._inspector.volume(environment_id, name)
            except ResourceNotFound:
                pass
        reach.absent_volumes.append(name)
        return None

    async def _stale_controls(self, environment_id: int, names: Sequence[str]) -> dict[str, int]:
        controls = ResourceControlRepository(self._db, self._clock)
        found: dict[str, int] = {}
        for name in dict.fromkeys(names):
            control = await controls.get_for(environment_id, "volume", name)
            if control is not None:
                found[name] = control.id
        return found

    async def _check_network(
        self,
        actor: Acting,
        environment_id: int,
        key: str,
        path: str,
        field_name: str | None,
        reach: _Reach,
    ) -> None:
        administrator = is_administrator(actor)
        failure = NETWORK_MISSING if administrator else NETWORK_UNAVAILABLE
        try:
            network = await self._resolve_network(environment_id, key)
        except ResourceNotFound:
            reach.errors.setdefault(field_name or path, failure)
            return
        if not administrator:
            try:
                await self._require_object(
                    actor, ResourceKey(environment_id, "network", network.id), "write"
                )
            except (ResourceNotFound, PermissionDenied):
                reach.errors.setdefault(field_name or path, failure)
                return
            if network.driver == "host":
                reach.findings.append(reach_finding("network_host", path, field_name))
        reach.network_ids[key] = network.id

    async def _resolve_network(self, environment_id: int, key: str) -> Network:
        if NETWORK_ID.fullmatch(key):
            try:
                return await self._inspector.network(environment_id, key)
            except ResourceNotFound:
                pass
        return await self._inspector.network_named(environment_id, key)

    async def _verified_source(
        self, actor: Principal, environment_id: int, source_id: str | None
    ) -> str | None:
        if source_id is None or not is_canonical_id("container", source_id):
            return None
        try:
            await self._authoriser.require_access(
                actor, ResourceKey(environment_id, "container", source_id), "read"
            )
            await self._inspector.inspect(environment_id, source_id)
        except (ResourceNotFound, PermissionDenied, EnvironmentUnavailable, ObjectTooLarge):
            return None
        return source_id

    async def _daemon_defaults(self, client: EngineClient) -> DaemonDefaults:
        try:
            info = await client.system.info()
        except EngineError as error:
            raise unavailable_from(error) from None
        return DaemonDefaults(
            runtime=info.default_runtime or None,
            logging_driver=info.logging_driver or None,
            cgroup_version=info.cgroup_version or None,
        )

    async def _recreate_restrictions(
        self,
        actor: Acting,
        environment_id: int,
        client: EngineClient,
        derivation: Derivation,
        defaults: DaemonDefaults | None,
    ) -> tuple[list[Finding], _Reach]:
        runtime: str | None = None
        if defaults is not None:
            runtime = defaults.runtime
        elif needs_default_runtime(derivation.body):
            runtime = (await self._daemon_defaults(client)).runtime
        findings = find_restricted(derivation.body, default_runtime=runtime)
        if is_administrator(actor):
            return findings, _Reach()
        reach = await self._reach(
            actor,
            environment_id,
            derivation.body,
            slots=None,
            own_volumes=derivation.own_volumes,
            own_networks=derivation.own_network_ids,
        )
        return findings, reach

    async def _image_present(self, client: EngineClient, reference: str) -> bool:
        try:
            await client.images.inspect(reference)
        except EngineNotFound:
            return False
        except ResponseTooLarge:
            return True
        except EngineError as error:
            raise unavailable_from(error) from None
        return True

    async def _image_of(self, client: EngineClient, image_id: str) -> ImageInspect | None:
        try:
            return await client.images.inspect(image_id)
        except (EngineNotFound, ResponseTooLarge):
            return None
        except EngineError as error:
            raise unavailable_from(error) from None

    @asynccontextmanager
    async def _held(
        self, actor: Acting, environment_id: int, container_id: str, holder: LockHolder
    ) -> AsyncIterator[None]:
        if isinstance(actor, AutomationAuthority):
            async with self._held_for_automation(actor, environment_id, container_id):
                yield
            return
        try:
            async with self._locks.hold(
                ObjectKey(environment_id, "container", container_id), holder
            ):
                yield
        except ObjectBusy as busy:
            audit = current_audit()
            audit.reason = "object_busy"
            audit.detail["holder_action"] = busy.holder.action
            raise

    @asynccontextmanager
    async def _held_for_automation(
        self, authority: AutomationAuthority, environment_id: int, container_id: str
    ) -> AsyncIterator[None]:
        """Take the container's lock for a run; a refusal to take it leaves no event."""
        audit = current_audit()
        stack = await self._stack_of(environment_id, container_id)
        if stack is not None:
            running = await self._locks.wait_free(stack, authority.lock_wait)
            if running is not None:
                audit.refuse()
                raise ObjectBusy(running)
        async with AsyncExitStack() as held:
            key = ObjectKey(environment_id, "container", container_id)
            try:
                await held.enter_async_context(authority.hold(key, "recreate"))
            except (ObjectBusy, RunSkipped):
                audit.refuse()
                raise
            running = None if stack is None else self._locks.holder_of(stack)
            if running is not None:
                audit.refuse()
                raise ObjectBusy(running)
            yield

    async def _stack_of(self, environment_id: int, container_id: str) -> ObjectKey | None:
        audit = current_audit()
        self._inspector.forget(environment_id, container_id)
        try:
            inspected = await self._inspector.inspect(environment_id, container_id)
        except ResourceNotFound:
            return None
        except EnvironmentUnavailable as error:
            audit.reason = error.kind
            raise
        except ObjectTooLarge:
            audit.reason = "too_large"
            raise
        stack = stack_of(inspected.config.labels)
        return None if stack is None else ObjectKey(environment_id, "stack", stack)

    async def _hold_reference(
        self,
        held: AsyncExitStack,
        actor: Principal,
        environment_id: int,
        reference: ImageReference,
        action: str,
    ) -> None:
        key = ObjectKey(environment_id, "image_reference", reference.key)
        await self._hold(held, actor, key, action)

    async def _hold_volume(
        self, held: AsyncExitStack, actor: Principal, environment_id: int, name: str
    ) -> None:
        await self._hold(held, actor, ObjectKey(environment_id, "volume", name), "create")

    async def _hold(
        self, held: AsyncExitStack, actor: Principal, key: ObjectKey, action: str
    ) -> None:
        holder = LockHolder(UserActor.of(actor), actor.username, action)
        try:
            await held.enter_async_context(self._locks.hold(key, holder))
        except ObjectBusy as busy:
            audit = current_audit()
            audit.reason = "object_busy"
            audit.detail["holder_action"] = busy.holder.action
            raise

    async def _bind(
        self,
        actor: Principal,
        environment_id: int,
        reference: ImageReference,
        credentials: PullCredentials,
        run: CreateRun | RecreateRun,
    ) -> None:
        """Bind the pulled reference inside the transaction that settles the control."""
        if self._bindings is None:
            return
        outcome = await self._bindings.record_pull(
            actor, environment_id, reference, credentials, run.digest
        )
        current_audit().detail["binding"] = outcome

    async def _pull(
        self,
        client: EngineClient,
        environment_id: int,
        reference: str,
        auth: SecretStr | None,
        run: CreateRun | RecreateRun,
    ) -> None:
        feed = run.feed
        run.pulling = True
        if feed is not None:
            feed.on_stage("pull")
        try:
            async with aclosing(client.images.pull(reference, registry_auth=auth)) as progress:
                async for event in progress:
                    if feed is not None:
                        feed.on_pull(event)
        except EngineError as error:
            audit = current_audit()
            logger.warning(
                "image_pull_failed",
                extra={
                    "environment_id": environment_id,
                    "error": type(error).__name__,
                    "status": error.status_code,
                },
            )
            if step_kind(error) == "unreachable":
                audit.reason = "unreachable"
                raise EnvironmentUnavailable("unreachable") from None
            audit.reason = "pull_failed"
            raise PullFailed() from None
        finally:
            run.pulling = False
        if feed is not None:
            feed.on_stage("pulled")

    async def _sequence_result[T](self, task: asyncio.Future[T], run: CreateRun | RecreateRun) -> T:
        """Wait for the shielded sequence of a create or recreate.

        With a feed, a cancel during the pull stops the pull, and a cancel the starter asked for
        after the pull is dropped, so the sequence runs to its end. Anything else, and every
        cancel without a feed, ends the wait and leaves the sequence running.
        """
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                feed = run.feed
                if feed is None:
                    raise
                if run.pulling:
                    task.cancel()
                    raise
                current = asyncio.current_task()
                if not feed.cancel_requested or current is None:
                    raise
                current.uncancel()

    def _pull_stopped(self, run: CreateRun | RecreateRun, outcome: str) -> Failure:
        """Return the failure of a stopped pull: cancelled by the starter, else interrupted."""
        if run.feed is not None and run.feed.cancel_requested:
            return Failure(
                "cancelled", f"The pull was cancelled before the container was {outcome}."
            )
        return Failure(
            "interrupted", f"The request was interrupted before the container was {outcome}."
        )

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        future = asyncio.ensure_future(work)
        self._background.add(future)
        future.add_done_callback(self._background.discard)

    def _fresh_controls(self, db: Session, recorder: ActivityRecorder) -> ResourceControlService:
        return ResourceControlService(
            db=db, controls=ResourceControlRepository(db, self._clock), activity=recorder
        )

    def _control_work(
        self, controls: ResourceControlService, step: ControlStep, rows: _Rows | None = None
    ) -> Callable[[], Awaitable[bool]]:
        match step:
            case Claim():
                return partial(self._claim_all, controls, step, rows)
            case Transfer(old=old, new=new):
                return partial(controls.transfer, old, new)
            case Copy(old=old, new=new):
                return partial(controls.copy, old, new)

    async def _claim_all(
        self, controls: ResourceControlService, step: Claim, rows: _Rows | None
    ) -> bool:
        """Claim the container and the volumes made with it, one history row per volume."""
        created = await controls.claim(
            step.key, step.owner_user_id, administrators_only=step.administrators_only
        )
        if not step.volumes or rows is None or step.owner_user_id is None:
            return created
        owner = UserActor(step.owner_user_id)
        environment_id = step.key.environment_id
        claimed = 0
        for volume in step.volumes:
            key = ResourceKey(environment_id, "volume", volume.name)
            replaced = volume.stale and await controls.release(key)
            made = await controls.claim(key, step.owner_user_id)
            claimed += made
            detail: dict[str, DetailValue] = {
                "via_container": step.key.resource_id,
                "anonymous": volume.anonymous,
                "control_created": made,
            }
            if replaced:
                detail["stale_control_replaced"] = True
            if rows.related:
                await rows.recorder.record_related(
                    owner,
                    action="volume_created",
                    target_type="volume",
                    target_id=volume.name,
                    environment_id=environment_id,
                    detail=detail,
                )
            else:
                await rows.recorder.record_event(
                    owner,
                    action="volume_created",
                    target_type="volume",
                    target_id=volume.name,
                    environment_id=environment_id,
                    result="success",
                    detail=detail,
                )
        rows.detail["volumes_claimed"] = claimed
        return created

    async def _apply_control(
        self,
        step: ControlStep,
        note: Callable[[bool], None] | None = None,
        bind: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Settle a control step, once more on a fresh session when the first attempt fails.

        The audit row is written inside the first transaction that commits on the request
        session, so note runs inside it and sets what the row must carry. `bind` writes the
        image binding in that same transaction; the retry on a fresh session does not, and the
        row then says the binding was not recorded.
        """
        detail = current_audit().detail
        try:
            return await retry_busy(
                partial(
                    in_transaction,
                    self._db,
                    self._control_work(self._controls, step, _Rows(self._activity, detail, True)),
                    row=bind,
                    note=note,
                )
            )
        except Exception as error:
            self._log_control_failure("container_control_retrying", _control_key(step), error)
        if bind is not None:
            detail["binding"] = "not_recorded"
        async with self._database.session() as db:
            recorder = ActivityRecorder(db, self._clock)
            controls = self._fresh_controls(db, recorder)
            work = self._control_work(controls, step, _Rows(recorder, detail, False))
            result = await retry_busy(partial(in_transaction, db, work))
        if note is not None:
            note(result)
        return result

    async def _carry_control(
        self,
        step: Transfer | Copy,
        new_key: ResourceKey,
        bind: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[bool, bool]:
        audit = current_audit()

        def note_copy(had_control: bool) -> None:
            if had_control and isinstance(step, Copy):
                audit.detail["old_control_kept"] = True

        try:
            had_control = await self._apply_control(step, note_copy, bind)
        except Exception as error:
            self._log_control_failure("container_control_carry_failed", new_key, error)
            audit.detail["control_moved"] = False
            await self._restrict_to_administrators(new_key)
            return False, False
        return True, had_control and isinstance(step, Copy)

    async def _restrict_to_administrators(self, key: ResourceKey) -> None:
        audit = current_audit()

        def note_restricted(created: bool) -> None:
            audit.detail["control_restricted"] = created

        try:
            await self._apply_control(Claim(key, None, administrators_only=True), note_restricted)
        except Exception as error:
            self._log_control_failure("container_control_restrict_failed", key, error)
            audit.detail["control_restricted"] = False

    async def _restrict_on(
        self, db: Session, controls: ResourceControlService, step: Transfer | Copy
    ) -> bool:
        work = self._control_work(controls, Claim(step.new, None, administrators_only=True))
        try:
            await retry_busy(partial(in_transaction, db, work))
        except Exception as error:
            self._log_control_failure("container_control_restrict_failed", step.new, error)
            return False
        return True

    def _log_control_failure(self, event: str, key: ResourceKey, error: Exception) -> None:
        logger.error(
            event,
            extra={
                "environment_id": key.environment_id,
                "container_id": key.resource_id,
                "error": type(error).__name__,
            },
        )

    async def _write_interrupted(
        self,
        actor: Acting,
        *,
        action: str,
        environment_id: int,
        target_id: str | None,
        detail: dict[str, DetailValue],
        failure: Failure | None,
        control: ControlStep | None,
        then: Callable[[ActivityRecorder], Awaitable[None]] | None = None,
    ) -> None:
        user = actor_of(actor)
        try:
            async with self._database.session() as db:
                recorder = ActivityRecorder(db, self._clock)
                await self._write_interrupted_row(
                    db, recorder, user, action, environment_id, target_id, detail, failure, control
                )
                if then is not None:
                    await self._run_after_interrupted(then, recorder, environment_id, target_id)
        except Exception as error:
            logger.error(
                "activity_event_write_failed",
                extra={
                    "action": action,
                    "result": "failure" if failure else "success",
                    "actor_user_id": actor.user_id if isinstance(actor, Principal) else None,
                    "target_type": "container",
                    "target_id": target_id,
                    "environment_id": environment_id,
                    "reason": failure.reason if failure else None,
                    "error": type(error).__name__,
                },
            )

    async def _run_after_interrupted(
        self,
        then: Callable[[ActivityRecorder], Awaitable[None]],
        recorder: ActivityRecorder,
        environment_id: int,
        target_id: str | None,
    ) -> None:
        try:
            await retry_busy(partial(then, recorder))
        except Exception as error:
            logger.error(
                "container_update_rollback_row_failed",
                extra={
                    "environment_id": environment_id,
                    "container_id": target_id,
                    "error": type(error).__name__,
                },
            )

    async def _write_interrupted_row(
        self,
        db: Session,
        recorder: ActivityRecorder,
        user: Actor,
        action: str,
        environment_id: int,
        target_id: str | None,
        detail: dict[str, DetailValue],
        failure: Failure | None,
        control: ControlStep | None,
    ) -> None:
        if failure is not None:
            await recorder.record_failure(
                user,
                action=action,
                target_type="container",
                target_id=target_id,
                environment_id=environment_id,
                reason=failure.reason,
                detail=detail,
                message=failure.message,
            )
            return
        write = partial(
            recorder.record_event,
            user,
            action=action,
            target_type="container",
            target_id=target_id,
            environment_id=environment_id,
            result="success",
            detail=detail,
        )
        if control is None:
            await retry_busy(write)
            return
        controls = self._fresh_controls(db, recorder)
        work = self._control_work(controls, control, _Rows(recorder, detail, False))
        try:
            await retry_busy(partial(in_transaction, db, work, write))
        except Exception as error:
            self._log_control_failure(
                "container_control_settle_failed", _control_key(control), error
            )
            if isinstance(control, Claim):
                detail["control_created"] = False
                detail.pop("volumes_claimed", None)
            else:
                detail["control_moved"] = False
                detail["control_restricted"] = await self._restrict_on(db, controls, control)
            await retry_busy(write)
