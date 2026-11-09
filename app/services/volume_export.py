"""Exports one volume as a gzip archive of its files and a manifest; administrators only."""

import posixpath
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Final

import anyio

from app.auth.errors import (
    SELF_PROTECTED_REASON,
    ProductError,
    ResourceNotFound,
    SelfProtected,
)
from app.auth.principal import Principal
from app.db.repositories.clocked import Clock
from app.docker_engine.archive import ArchiveError, DirectoryStream
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, EnvironmentUnreachable
from app.docker_engine.models import Volume
from app.logging import get_logger
from app.self_identity import SelfIdentitySource, no_identity
from app.services.access import ResourceKey
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.container_actions import FORCEABLE_STATES
from app.services.guards import require_administrator
from app.services.host_files import (
    HELPER_NAME_PREFIX,
    MOUNT_POINT,
    HelperImageMissing,
    HelperImageUnusable,
    HelperMountRefused,
    HostFileReader,
    VolumeHelper,
    check_helper_image,
    helper_image,
)
from app.services.host_objects import (
    ObjectUser,
    audited_client,
    audited_list_read,
    describe_users,
    failure_reason,
    hold_all,
    read_containers,
    volume_users,
)
from app.services.object_inspector import (
    VOLUME_OR_NETWORK_NAME,
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
    unavailable_from,
)
from app.services.object_locks import LockHolder, ObjectKey, ObjectLockRegistry
from app.services.volume_export_writer import RewriteStream, StreamMalformed
from app.services.volume_manifest import FORMAT_VERSION, build_manifest, manifest_bytes
from app.services.volume_views import VolumeChangedSinceReview, VolumeGone
from app.version import product_version

logger = get_logger(__name__)

__all__ = [
    "ExportSlots",
    "VolumeExportBusy",
    "VolumeExportConfirmation",
    "VolumeExportRefused",
    "VolumeExportService",
    "VolumeExportStream",
    "shared_export_slots",
]

MAX_EXPORTS_PER_ENVIRONMENT: Final = 1
MAX_EXPORTS_PER_INSTANCE: Final = 2
SUPPORTED_DRIVER: Final = "local"
ARCHIVE_MEDIA_TYPE: Final = "application/gzip"
ARCHIVE_SUFFIX: Final = ".tar.gz"

_ROOT: Final = posixpath.basename(MOUNT_POINT)
_NAME_LIMIT: Final = 255
_DOCKER_REFUSED_TEXT: Final = "Docker could not mount or read the volume."


class VolumeExportBusy(ProductError):
    """No export slot is free; the message says which limit was reached."""


class VolumeExportRefused(ProductError):
    """The volume cannot be exported now; `reason` is the stable code recorded on the row."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class ExportIncomplete(ProductError):
    """The archive was not fully produced; raised only to leave the failure row."""


class ExportSlot:
    """One running export's claim on the limits; releasing it twice is harmless."""

    def __init__(self, owner: "ExportSlots", environment_id: int) -> None:
        self._owner: ExportSlots | None = owner
        self._environment_id = environment_id

    def release(self) -> None:
        """Give the claim back."""
        owner, self._owner = self._owner, None
        if owner is not None:
            owner.give_back(self._environment_id)


class ExportSlots:
    """The exports in flight: one per environment and a small number across the instance."""

    def __init__(
        self,
        per_environment: int = MAX_EXPORTS_PER_ENVIRONMENT,
        per_instance: int = MAX_EXPORTS_PER_INSTANCE,
    ) -> None:
        self._per_environment = per_environment
        self._per_instance = per_instance
        self._running: dict[int, int] = {}

    def acquire(self, environment_id: int) -> ExportSlot:
        """Claim a slot; VolumeExportBusy when the environment or the instance has none free."""
        if self._running.get(environment_id, 0) >= self._per_environment:
            raise VolumeExportBusy(
                "A volume export is already running on this environment. "
                "Try again when it has finished."
            )
        if sum(self._running.values()) >= self._per_instance:
            raise VolumeExportBusy("Too many volume exports are running. Try again shortly.")
        self._running[environment_id] = self._running.get(environment_id, 0) + 1
        return ExportSlot(self, environment_id)

    def give_back(self, environment_id: int) -> None:
        """Release one claim on an environment."""
        remaining = self._running.get(environment_id, 0) - 1
        if remaining > 0:
            self._running[environment_id] = remaining
        else:
            self._running.pop(environment_id, None)


_SLOTS: Final = ExportSlots()


def shared_export_slots() -> ExportSlots:
    """Return the instance's export slots, shared by every kind of export."""
    return _SLOTS


@dataclass(frozen=True, slots=True)
class VolumeExportConfirmation:
    """What the export confirmation names; `created_at_text` is what the page posts back."""

    environment_id: int
    environment_name: str
    name: str
    driver: str
    created_at_text: str
    running: tuple[ObjectUser, ...]
    running_total: int
    helper_image: str | None
    helper_name_prefix: str = HELPER_NAME_PREFIX

    @property
    def can_export(self) -> bool:
        """Report whether a helper image is available on the host."""
        return self.helper_image is not None


def _name_argument(arguments: Arguments) -> str:
    return str(arguments["name"])[:_NAME_LIMIT]


class VolumeExportStream:
    """The chunks of one export; releases the helper, the lock and the slot when it ends.

    `aclose` ends it from outside and is safe to call after the last chunk or more than once.
    """

    media_type: Final = ARCHIVE_MEDIA_TYPE
    suffix: Final = ARCHIVE_SUFFIX

    def __init__(
        self,
        *,
        service: "VolumeExportService",
        actor: Principal,
        environment_id: int,
        name: str,
        driver: str,
        rewrite: RewriteStream,
        directory: DirectoryStream,
        stack: AsyncExitStack,
        slot: ExportSlot,
        helper: VolumeHelper,
    ) -> None:
        self._service = service
        self._actor = actor
        self._environment_id = environment_id
        self._name = name
        self._driver = driver
        self._rewrite = rewrite
        self._directory = directory
        self._stack = stack
        self._slot = slot
        self.helper = helper
        self._closed = False
        self._completed = False
        self._reason = "interrupted"

    def __aiter__(self) -> "VolumeExportStream":
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await self._rewrite.__anext__()
        except StopAsyncIteration:
            self._completed = True
            await self.aclose()
            raise
        except Exception as error:
            self._reason = _reason_of(error)
            await self.aclose()
            raise

    async def aclose(self) -> None:
        """End the export, remove the helper and record how it ended."""
        if self._closed:
            return
        self._closed = True
        try:
            try:
                await self._rewrite.aclose()
            finally:
                await self._directory.aclose()
        finally:
            try:
                await self._stack.aclose()
            finally:
                self._slot.release()
                await self._service.record_end(self)

    @property
    def snapshot(self) -> "_Outcome":
        """The figures the finished row records."""
        return _Outcome(
            actor=self._actor,
            environment_id=self._environment_id,
            name=self._name,
            driver=self._driver,
            archive_bytes=self._rewrite.bytes_out,
            source_bytes=self._directory.received,
            skipped=self._rewrite.skipped_count,
            completed=self._completed,
            reason=self._reason,
            helper_removed=self.helper.removed,
        )


@dataclass(frozen=True, slots=True)
class _Outcome:
    actor: Principal
    environment_id: int
    name: str
    driver: str
    archive_bytes: int
    source_bytes: int
    skipped: int
    completed: bool
    reason: str
    helper_removed: bool


def _refused_by_docker(error: EngineError) -> bool:
    return error.status_code is not None and not isinstance(error, EnvironmentUnreachable)


def _reason_of(error: Exception) -> str:
    if isinstance(error, StreamMalformed):
        return "malformed_stream"
    if isinstance(error, ArchiveError):
        return "unreadable"
    if isinstance(error, EnvironmentUnreachable):
        return "unreachable"
    if isinstance(error, EngineError):
        return failure_reason(error)
    return type(error).__name__


class VolumeExportService:
    """Opens volume exports and records them; the caller streams the result and closes it."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        locks: ObjectLockRegistry,
        activity: ActivityRecorder,
        clock: Clock,
        self_identity: SelfIdentitySource | None = None,
        slots: ExportSlots | None = None,
    ) -> None:
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._locks = locks
        self._activity = activity
        self._clock = clock
        self._self_identity = self_identity or no_identity
        self._slots = _SLOTS if slots is None else slots

    async def confirmation(
        self, actor: Principal, environment_id: int, name: str
    ) -> VolumeExportConfirmation:
        """Name what exporting the volume involves; nothing is created or recorded.

        ResourceNotFound unless the caller is an administrator who can see the volume,
        SelfProtected and VolumeExportRefused for a volume that cannot be exported.
        """
        require_administrator(actor)
        volume = await self._readable(actor, environment_id, name)
        self._refuse_protected(environment_id, volume)
        self._refuse_unsupported(volume)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        try:
            containers = await read_containers(client)
            using = volume_users(containers).get(volume.name, [])
            running = [c for c in using if c.state in FORCEABLE_STATES]
            named, more, _hidden = describe_users(running, frozenset(c.id for c in running))
            try:
                image: str | None = await helper_image(client)
            except HelperImageMissing:
                image = None
        except EngineError as error:
            raise unavailable_from(error) from None
        return VolumeExportConfirmation(
            environment_id=environment_id,
            environment_name=environment.name,
            name=volume.name,
            driver=volume.driver,
            created_at_text=volume.created_at,
            running=named,
            running_total=len(named) + more,
            helper_image=image,
        )

    async def start(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        *,
        expected_created_at: str,
        helper_image: str | None = None,
    ) -> VolumeExportStream:
        """Check the volume, create the helper and return the archive stream.

        `helper_image` is the image id the confirmation named, resolved now when omitted.
        Everything that can refuse the export has run, and the started row is written, before
        this returns; no byte has been read for the caller yet. The caller must close the stream.
        ResourceNotFound, SelfProtected, VolumeChangedSinceReview, VolumeExportRefused,
        VolumeExportBusy, ObjectBusy, EnvironmentUnavailable.
        """
        opened: list[VolumeExportStream] = []
        try:
            return await self._open(
                actor, environment_id, name, expected_created_at, helper_image, opened
            )
        except BaseException:
            with anyio.CancelScope(shield=True):
                for stream in opened:
                    await stream.aclose()
            raise

    @audited(
        action="volume_export_started",
        target_type="volume",
        target_id=_name_argument,
        environment_id=environment_argument,
    )
    async def _open(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        expected_created_at: str,
        reviewed_image: str | None,
        opened: list[VolumeExportStream],
    ) -> VolumeExportStream:
        audit = current_audit()
        require_administrator(actor)
        try:
            volume = await self._readable(actor, environment_id, name)
        except (EnvironmentUnavailable, ObjectTooLarge):
            audit.refuse()
            raise
        await self._hosts.environment(environment_id)
        try:
            self._refuse_protected(environment_id, volume)
        except SelfProtected:
            audit.reason = SELF_PROTECTED_REASON
            raise
        try:
            slot = self._slots.acquire(environment_id)
        except VolumeExportBusy:
            audit.reason = "export_busy"
            raise
        stack = AsyncExitStack()
        try:
            stream = await self._prepare(
                actor, environment_id, name, expected_created_at, reviewed_image, stack, slot
            )
        except BaseException:
            with anyio.CancelScope(shield=True):
                await stack.aclose()
            slot.release()
            raise
        opened.append(stream)
        return stream

    async def _prepare(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        expected_created_at: str,
        reviewed_image: str | None,
        stack: AsyncExitStack,
        slot: ExportSlot,
    ) -> VolumeExportStream:
        audit = current_audit()
        holder = LockHolder(UserActor.of(actor), actor.username, "export")
        await stack.enter_async_context(
            hold_all(self._locks, [ObjectKey(environment_id, "volume", name)], holder)
        )
        self._inspector.forget_volume(environment_id, name)
        volume, client = await self._load(environment_id, name)
        audit.detail.update(driver=volume.driver, format_version=FORMAT_VERSION)
        if volume.created_at != expected_created_at:
            audit.reason = "changed_since_review"
            raise VolumeChangedSinceReview()
        try:
            self._refuse_unsupported(volume)
        except VolumeExportRefused as refused:
            audit.reason = refused.reason
            raise
        try:
            self._refuse_protected(environment_id, volume)
        except SelfProtected:
            audit.reason = SELF_PROTECTED_REASON
            raise
        try:
            containers = await audited_list_read(read_containers(client))
            using = volume_users(containers).get(name, [])
            audit.detail["containers"] = len(using)
            audit.detail["running_containers"] = sum(c.state in FORCEABLE_STATES for c in using)
            helper, directory, rewrite = await self._open_helper(
                actor, volume, client, stack, reviewed_image
            )
        except EngineError as error:
            audit.reason = failure_reason(error)
            raise unavailable_from(error) from None
        audit.detail["helper_container"] = helper.container_id
        return VolumeExportStream(
            service=self,
            actor=actor,
            environment_id=environment_id,
            name=name,
            driver=volume.driver,
            rewrite=rewrite,
            directory=directory,
            stack=stack,
            slot=slot,
            helper=helper,
        )

    async def _image(self, client: EngineClient, reviewed: str | None) -> str:
        audit = current_audit()
        try:
            if reviewed is None:
                return await helper_image(client)
            if not reviewed:
                raise HelperImageMissing
            await check_helper_image(client, reviewed)
        except HelperImageMissing:
            audit.reason = "helper_image_missing"
            raise VolumeExportRefused(
                "helper_image_missing",
                "The host has no image to create the helper container from. "
                "Pull or load an image on the host, then review the export again.",
            ) from None
        except HelperImageUnusable:
            audit.reason = "helper_image_unusable"
            raise VolumeExportRefused(
                "helper_image_unusable",
                "The image the confirmation named declares a volume where the helper mounts "
                "the volume. Review the export again.",
            ) from None
        except EngineError as error:
            if not _refused_by_docker(error):
                raise
            audit.reason = "helper_refused"
            raise VolumeExportRefused("helper_refused", _DOCKER_REFUSED_TEXT) from None
        return reviewed

    async def _open_helper(
        self,
        actor: Principal,
        volume: Volume,
        client: EngineClient,
        stack: AsyncExitStack,
        reviewed_image: str | None,
    ) -> tuple[VolumeHelper, DirectoryStream, RewriteStream]:
        audit = current_audit()
        image = await self._image(client, reviewed_image)
        try:
            helper = await stack.enter_async_context(
                HostFileReader(client).volume_session(actor, volume.name, image=image)
            )
        except HelperMountRefused:
            audit.reason = "helper_refused"
            raise VolumeExportRefused(
                "helper_refused", "Docker refused to create the helper container."
            ) from None
        except EngineError as error:
            if not _refused_by_docker(error):
                raise
            audit.reason = "helper_refused"
            raise VolumeExportRefused("helper_refused", _DOCKER_REFUSED_TEXT) from None
        try:
            directory = await helper.stream()
        except ArchiveError:
            audit.reason = "unreadable"
            raise VolumeExportRefused(
                "unreadable", "Docker could not read the volume's contents."
            ) from None
        except EngineError as error:
            if not _refused_by_docker(error):
                raise
            audit.reason = "unreadable"
            raise VolumeExportRefused("unreadable", _DOCKER_REFUSED_TEXT) from None
        manifest = build_manifest(
            exported_at=self._clock(),
            instance_version=product_version(),
            name=volume.name,
            driver=volume.driver,
            labels=volume.labels,
            options=volume.options,
        )
        rewrite = RewriteStream(directory, manifest_bytes(manifest), root=_ROOT)
        return helper, directory, rewrite

    @audited(
        action="volume_exported",
        target_type="volume",
        target_id=_name_argument,
        environment_id=environment_argument,
    )
    async def _record_finish(
        self, actor: Principal, environment_id: int, name: str, outcome: _Outcome
    ) -> None:
        audit = current_audit()
        audit.detail.update(
            name=name,
            driver=outcome.driver,
            format_version=FORMAT_VERSION,
            bytes=outcome.archive_bytes,
            source_bytes=outcome.source_bytes,
            skipped=outcome.skipped,
            completed=outcome.completed,
            helper_removed=outcome.helper_removed,
        )
        if not outcome.completed:
            audit.reason = outcome.reason
            raise ExportIncomplete("The export did not complete.")

    async def record_end(self, stream: VolumeExportStream) -> None:
        """Write the finished row of a stream; a row that cannot be written is logged."""
        outcome = stream.snapshot
        try:
            await self._record_finish(outcome.actor, outcome.environment_id, outcome.name, outcome)
        except ExportIncomplete:
            pass
        except Exception as error:
            logger.error(
                "activity_event_write_failed",
                extra={"action": "volume_exported", "error": type(error).__name__},
            )

    async def _readable(self, actor: Principal, environment_id: int, name: str) -> Volume:
        if VOLUME_OR_NETWORK_NAME.fullmatch(name) is None:
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "volume", name), "read"
        )
        return await self._inspector.volume(environment_id, name)

    async def _load(self, environment_id: int, name: str) -> tuple[Volume, EngineClient]:
        audit = current_audit()
        try:
            volume = await self._inspector.volume(environment_id, name)
            client = await audited_client(self._hosts, environment_id)
        except EnvironmentUnavailable as error:
            audit.reason = error.kind
            raise
        except ObjectTooLarge:
            audit.reason = "too_large"
            raise
        except ResourceNotFound:
            audit.reason = "volume_gone"
            raise VolumeGone() from None
        return volume, client

    def _refuse_protected(self, environment_id: int, volume: Volume) -> None:
        identity = self._self_identity()
        if identity.mounts_unknown(environment_id):
            raise SelfProtected.export_unverified()
        if identity.protects_volume(environment_id, volume.name):
            raise SelfProtected.export(volume.name)
        device = volume.options.get("device")
        if device and identity.protects_path(environment_id, device):
            raise SelfProtected.export(volume.name)

    def _refuse_unsupported(self, volume: Volume) -> None:
        if volume.driver != SUPPORTED_DRIVER:
            raise VolumeExportRefused(
                "plugin_driver",
                "Only volumes of the local driver can be exported. "
                "A plugin driver can run code on the host when its volume is mounted.",
            )
