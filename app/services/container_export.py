"""Exports one container as an archive of its create form's state, networks and chosen volumes."""

import asyncio
import posixpath
from collections.abc import AsyncIterator, Collection, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

import anyio

from app.auth.errors import (
    SELF_PROTECTED_REASON,
    ProductError,
    ResourceNotFound,
    SelfProtected,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.docker_engine.archive import ArchiveError
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, EnvironmentUnreachable, NotFound
from app.docker_engine.models import ContainerInspect, ImageInspect, Network, Volume
from app.logging import get_logger
from app.self_identity import SelfIdentity, SelfIdentitySource, no_identity
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
from app.services.container_create import ContainerCreateService
from app.services.container_export_content import (
    EnvironmentRow,
    MountRow,
    group_counts,
    image_reference,
    mount_rows,
    network_entry,
    review_digest,
    split_form,
    user_defined_network_names,
)
from app.services.containers import display_name
from app.services.export_archive_writer import ExportArchiveStream, VolumeOpener, VolumeSource
from app.services.export_build import EnvironmentInput, ExportDescription, prepare_export
from app.services.export_manifest import (
    FORMAT_VERSION,
    NOT_EXPORTED,
    ImageReference,
    NetworkEntry,
    NotExportedGroup,
)
from app.services.export_sealing import check_passphrase
from app.services.guards import require_administrator
from app.services.host_files import (
    HELPER_NAME_PREFIX,
    MOUNT_POINT,
    HelperImageMissing,
    HelperMountRefused,
    HostFileReader,
    helper_image,
)
from app.services.host_objects import (
    audited_client,
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
from app.services.volume_export import (
    ARCHIVE_MEDIA_TYPE,
    ARCHIVE_SUFFIX,
    SUPPORTED_DRIVER,
    ExportSlot,
    ExportSlots,
    VolumeExportBusy,
    shared_export_slots,
)
from app.services.volume_export_writer import StreamMalformed
from app.services.volume_manifest import VolumeManifest, build_manifest
from app.version import product_version

logger = get_logger(__name__)

__all__ = [
    "ContainerChangedSinceReview",
    "ContainerExportBusy",
    "ContainerExportConfirmation",
    "ContainerExportRefused",
    "ContainerExportService",
    "ContainerExportStream",
    "VolumeChoice",
]

_ROOT: Final = posixpath.basename(MOUNT_POINT)
_ID_LIMIT: Final = 64
_NAME_LIMIT: Final = 255

CHANGED_MESSAGE: Final = "This container changed since you reviewed it; nothing was exported."
_PLUGIN_DRIVER: Final = (
    "Only volumes of the local driver can be exported. "
    "A plugin driver can run code on the host when its volume is mounted."
)
_VOLUME_GONE: Final = "The volume no longer exists."
_HELPER_MISSING: Final = (
    "The host has no image to create the helper container from. "
    "Pull or load an image on the host, then try again."
)
_SELF_CONTAINER: Final = (
    "This is the container dockmin runs in. It cannot be exported from dockmin."
)
_MOUNTS_UNKNOWN: Final = (
    "dockmin cannot establish which volumes its own container mounts on this host, so "
    "no container here can be exported until it can."
)


class ContainerExportBusy(ProductError):
    """No export slot is free."""


class ContainerExportRefused(ProductError):
    """The container cannot be exported now; `reason` is the stable code recorded on the row."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class ContainerChangedSinceReview(ValidationFailed):
    """The container or its volumes are no longer what the caller reviewed; nothing was read."""

    def __init__(self) -> None:
        super().__init__({"container": CHANGED_MESSAGE})


class ContainerExportIncomplete(ProductError):
    """The archive was not fully produced; raised only to leave the failure row."""


@dataclass(frozen=True, slots=True)
class VolumeChoice:
    """A named volume the container mounts; `refusal` says why its data cannot be exported."""

    name: str
    targets: tuple[str, ...]
    driver: str
    refusal: str | None
    running_users: int

    @property
    def can_export(self) -> bool:
        """Report whether the volume's data may be chosen."""
        return self.refusal is None


@dataclass(frozen=True, slots=True)
class ContainerExportConfirmation:
    """What the export confirmation names; `digest` is what the page posts back."""

    environment_id: int
    environment_name: str
    container_id: str
    name: str
    state: str
    is_running: bool
    image: ImageReference
    digest: str
    groups: tuple[tuple[str, int], ...]
    variable_names: tuple[str, ...]
    networks: tuple[NetworkEntry, ...]
    binds: tuple[MountRow, ...]
    volumes: tuple[VolumeChoice, ...]
    not_exported: tuple[str, ...]
    helper_image: str | None
    left_out: tuple[str, ...] = NOT_EXPORTED
    helper_name_prefix: str = HELPER_NAME_PREFIX

    @property
    def can_copy_volumes(self) -> bool:
        """Report whether a volume's data can be chosen."""
        return self.helper_image is not None and any(v.can_export for v in self.volumes)


@dataclass(frozen=True, slots=True)
class _Candidate:
    choice: VolumeChoice
    volume: Volume | None = field(default=None, repr=False)
    code: str | None = None


@dataclass(frozen=True, slots=True)
class _Snapshot:
    container_id: str
    name: str
    state: str
    is_running: bool
    form: dict[str, str] = field(repr=False)
    environment: tuple[EnvironmentRow, ...]
    networks: tuple[NetworkEntry, ...]
    image: ImageReference
    binds: tuple[MountRow, ...]
    candidates: tuple[_Candidate, ...]
    groups: tuple[tuple[str, int], ...]
    notices: tuple[NotExportedGroup, ...]
    sentences: tuple[str, ...]
    digest: str

    def volume_named(self, name: str) -> _Candidate | None:
        return next((c for c in self.candidates if c.choice.name == name), None)


@dataclass(frozen=True, slots=True)
class _Outcome:
    actor: Principal
    environment_id: int
    container_id: str
    detail: dict[str, DetailValue]
    archive_bytes: int
    skipped: int
    volumes_copied: int
    completed: bool
    reason: str


def _target_id(arguments: Arguments) -> str:
    return str(arguments["container_id"])[:_ID_LIMIT]


class ContainerExportStream:
    """The chunks of one export; releases its volume locks and slot when it ends.

    `aclose` ends it from outside and is safe to call after the last chunk or more than once.
    """

    media_type: Final = ARCHIVE_MEDIA_TYPE
    suffix: Final = ARCHIVE_SUFFIX

    def __init__(
        self,
        *,
        service: "ContainerExportService",
        actor: Principal,
        environment_id: int,
        container_id: str,
        name: str,
        detail: dict[str, DetailValue],
        archive: ExportArchiveStream,
        stack: AsyncExitStack,
        slot: ExportSlot,
    ) -> None:
        self._service = service
        self._actor = actor
        self._environment_id = environment_id
        self._container_id = container_id
        self.name = name
        self._detail = detail
        self._archive = archive
        self._stack = stack
        self._slot = slot
        self._closed = False
        self._completed = False
        self._reason = "interrupted"

    def __aiter__(self) -> "ContainerExportStream":
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await self._archive.__anext__()
        except StopAsyncIteration:
            self._completed = True
            await self.aclose()
            raise
        except Exception as error:
            self._reason = _reason_of(error)
            await self.aclose()
            raise

    async def aclose(self) -> None:
        """End the export, release what it holds and record how it ended."""
        if self._closed:
            return
        self._closed = True
        try:
            try:
                await self._archive.aclose()
            finally:
                await self._stack.aclose()
        finally:
            self._slot.release()
            await self._service.record_end(self)

    @property
    def snapshot(self) -> _Outcome:
        """The figures the finished row records."""
        results = self._archive.volume_results
        return _Outcome(
            actor=self._actor,
            environment_id=self._environment_id,
            container_id=self._container_id,
            detail=dict(self._detail),
            archive_bytes=self._archive.bytes_out,
            skipped=sum(result.skipped_count for result in results),
            volumes_copied=len(results),
            completed=self._completed,
            reason=self._reason,
        )


def _reason_of(error: Exception) -> str:
    if isinstance(error, StreamMalformed):
        return "malformed_stream"
    if isinstance(error, ArchiveError):
        return "unreadable"
    if isinstance(error, HelperMountRefused):
        return "helper_refused"
    if isinstance(error, HelperImageMissing):
        return "helper_image_missing"
    if isinstance(error, EnvironmentUnreachable):
        return "unreachable"
    if isinstance(error, EngineError):
        return failure_reason(error)
    return type(error).__name__


class ContainerExportService:
    """Opens container exports and records them; the caller streams the result and closes it."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        creation: ContainerCreateService,
        locks: ObjectLockRegistry,
        activity: ActivityRecorder,
        clock: Clock,
        self_identity: SelfIdentitySource | None = None,
        slots: ExportSlots | None = None,
    ) -> None:
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._creation = creation
        self._locks = locks
        self._activity = activity
        self._clock = clock
        self._self_identity = self_identity or no_identity
        self._slots = shared_export_slots() if slots is None else slots

    async def confirmation(
        self, actor: Principal, environment_id: int, container_id: str
    ) -> ContainerExportConfirmation:
        """Name what exporting the container involves; nothing is created, read or recorded.

        ResourceNotFound unless the caller is an administrator who can see the container,
        SelfProtected for dockmin's own container or while its mounts are unknown.
        """
        require_administrator(actor)
        await self._readable(actor, environment_id, container_id)
        self._refuse_protected(environment_id, container_id)
        snapshot = await self._snapshot(actor, environment_id, container_id)
        environment = await self._hosts.environment(environment_id)
        helper: str | None = None
        if any(c.choice.can_export for c in snapshot.candidates):
            client = await self._hosts.client(environment_id)
            try:
                helper = await helper_image(client)
            except HelperImageMissing:
                helper = None
            except EngineError as error:
                raise unavailable_from(error) from None
        return ContainerExportConfirmation(
            environment_id=environment_id,
            environment_name=environment.name,
            container_id=container_id,
            name=snapshot.name,
            state=snapshot.state,
            is_running=snapshot.is_running,
            image=snapshot.image,
            digest=snapshot.digest,
            groups=snapshot.groups,
            variable_names=tuple(dict.fromkeys(row.name for row in snapshot.environment)),
            networks=snapshot.networks,
            binds=snapshot.binds,
            volumes=tuple(c.choice for c in snapshot.candidates),
            not_exported=snapshot.sentences,
            helper_image=helper,
        )

    async def export(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        expected_digest: str,
        passphrase: str | None,
        secret_names: Collection[str],
        chosen_volumes: Collection[str],
    ) -> ContainerExportStream:
        """Check the container, seal what is secret and return the archive stream.

        `passphrase` None leaves the values of `secret_names` out of the archive. Everything that
        can refuse the export has run before this returns, and no volume has been read; the
        caller must close the stream. ResourceNotFound, SelfProtected, ContainerChangedSinceReview,
        ContainerExportRefused, ContainerExportBusy, ValidationFailed, ObjectBusy,
        EnvironmentUnavailable.
        """
        opened: list[ContainerExportStream] = []
        try:
            return await self._open(
                actor,
                environment_id,
                container_id,
                expected_digest,
                passphrase,
                tuple(secret_names),
                tuple(chosen_volumes),
                opened,
            )
        except BaseException:
            with anyio.CancelScope(shield=True):
                for stream in opened:
                    await stream.aclose()
            raise

    @audited(
        action="container_exported",
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def _open(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        expected_digest: str,
        passphrase: str | None,
        secret_names: tuple[str, ...],
        chosen_volumes: tuple[str, ...],
        opened: list[ContainerExportStream],
    ) -> ContainerExportStream:
        audit = current_audit()
        require_administrator(actor)
        audit.detail["format_version"] = FORMAT_VERSION
        if secret_names and passphrase is not None:
            check_passphrase(passphrase)
        try:
            await self._readable(actor, environment_id, container_id)
        except (EnvironmentUnavailable, ObjectTooLarge):
            audit.refuse()
            raise
        await self._hosts.environment(environment_id)
        try:
            self._refuse_protected(environment_id, container_id)
        except SelfProtected:
            audit.reason = SELF_PROTECTED_REASON
            raise
        try:
            slot = self._slots.acquire(environment_id)
        except VolumeExportBusy:
            audit.reason = "export_busy"
            raise ContainerExportBusy(
                "Another export is running, or the instance is at its export limit. "
                "Try again when one has finished."
            ) from None
        stack = AsyncExitStack()
        try:
            stream = await self._prepare(
                actor,
                environment_id,
                container_id,
                expected_digest,
                passphrase,
                secret_names,
                chosen_volumes,
                stack,
                slot,
            )
        except BaseException:
            with anyio.CancelScope(shield=True):
                await stack.aclose()
            slot.release()
            raise
        opened.append(stream)
        audit.unchanged()
        return stream

    async def _prepare(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        expected_digest: str,
        passphrase: str | None,
        secret_names: tuple[str, ...],
        chosen_volumes: tuple[str, ...],
        stack: AsyncExitStack,
        slot: ExportSlot,
    ) -> ContainerExportStream:
        audit = current_audit()
        if any(VOLUME_OR_NETWORK_NAME.fullmatch(name) is None for name in chosen_volumes):
            raise ValidationFailed({"volume": "A chosen volume is not one this container mounts."})
        holder = LockHolder(UserActor.of(actor), actor.username, "export")
        await stack.enter_async_context(
            hold_all(
                self._locks,
                [ObjectKey(environment_id, "volume", name) for name in chosen_volumes],
                holder,
            )
        )
        self._inspector.forget(environment_id, container_id)
        for name in chosen_volumes:
            self._inspector.forget_volume(environment_id, name)
        snapshot = await self._audited_snapshot(actor, environment_id, container_id)
        if snapshot.digest != expected_digest:
            audit.reason = "changed_since_review"
            raise ContainerChangedSinceReview()
        marked = self._marked(snapshot, secret_names)
        volumes = self._chosen(snapshot, chosen_volumes)
        client = await audited_client(self._hosts, environment_id)
        helper = await self._helper_image(client) if volumes else None
        mode = "none" if not marked else "omitted" if passphrase is None else "sealed"
        audit.detail.update(
            environment=len(snapshot.environment),
            secret_values=len(marked),
            secrets=mode,
            networks=len(snapshot.networks),
            bind_mounts=len(snapshot.binds),
            volumes=len(volumes),
            not_exported_groups=len(snapshot.notices),
            running=snapshot.is_running,
        )
        description = ExportDescription(
            kind="container",
            name=snapshot.name,
            description=f"Container {snapshot.name}",
            instance_version=product_version(),
            exported_at=self._clock(),
            form=snapshot.form,
            environment=tuple(
                EnvironmentInput(row.name, index in marked, row.value)
                for index, row in enumerate(snapshot.environment)
            ),
            networks=snapshot.networks,
            images=(snapshot.image,),
            volumes=tuple(self._volume_manifest(volume) for volume in volumes),
            not_exported_fields=snapshot.notices,
        )
        sealing = passphrase if marked else None
        failed = False
        try:
            prepared = await asyncio.to_thread(prepare_export, description, sealing)
        except ValidationFailed:
            raise
        except ValueError:
            failed = True
        if failed:
            audit.reason = "not_representable"
            raise ContainerExportRefused(
                "not_representable",
                "This container's configuration cannot be written to an export archive.",
            )
        sources = [
            VolumeSource(_ROOT, self._opener(actor, client, helper or "", volume.name))
            for volume in volumes
        ]
        archive = ExportArchiveStream(prepared, sources)
        return ContainerExportStream(
            service=self,
            actor=actor,
            environment_id=environment_id,
            container_id=container_id,
            name=snapshot.name,
            detail=dict(audit.detail),
            archive=archive,
            stack=stack,
            slot=slot,
        )

    @audited(
        action="container_exported",
        target_type="container",
        target_id=_target_id,
        environment_id=environment_argument,
    )
    async def _record_finish(
        self, actor: Principal, environment_id: int, container_id: str, outcome: _Outcome
    ) -> None:
        audit = current_audit()
        audit.detail.update(outcome.detail)
        audit.detail.update(
            bytes=outcome.archive_bytes,
            skipped=outcome.skipped,
            volumes_copied=outcome.volumes_copied,
            completed=outcome.completed,
        )
        if not outcome.completed:
            audit.reason = outcome.reason
            raise ContainerExportIncomplete("The export did not complete.")

    async def record_end(self, stream: ContainerExportStream) -> None:
        """Write the finished row of a stream; a row that cannot be written is logged."""
        outcome = stream.snapshot
        try:
            await self._record_finish(
                outcome.actor, outcome.environment_id, outcome.container_id, outcome
            )
        except ContainerExportIncomplete:
            pass
        except Exception as error:
            logger.error(
                "activity_event_write_failed",
                extra={"action": "container_exported", "error": type(error).__name__},
            )

    async def _readable(
        self, actor: Principal, environment_id: int, container_id: str
    ) -> ContainerInspect:
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "container", container_id), "read"
        )
        return await self._inspector.inspect(environment_id, container_id)

    def _refuse_protected(self, environment_id: int, container_id: str) -> None:
        identity = self._self_identity()
        if identity.mounts_unknown(environment_id):
            raise SelfProtected(_MOUNTS_UNKNOWN)
        if identity.protects_container(container_id):
            raise SelfProtected(_SELF_CONTAINER)

    async def _audited_snapshot(
        self, actor: Principal, environment_id: int, container_id: str
    ) -> _Snapshot:
        audit = current_audit()
        try:
            return await self._snapshot(actor, environment_id, container_id)
        except EnvironmentUnavailable as error:
            audit.reason = error.kind
            raise
        except ObjectTooLarge:
            audit.reason = "too_large"
            raise
        except ContainerExportRefused as refused:
            audit.reason = refused.reason
            raise
        except ResourceNotFound:
            audit.reason = "container_gone"
            raise

    async def _snapshot(
        self, actor: Principal, environment_id: int, container_id: str
    ) -> _Snapshot:
        context = await self._creation.form_context(actor, environment_id, source_id=container_id)
        inspected = await self._inspector.inspect(environment_id, container_id)
        client = await self._hosts.client(environment_id)
        form, environment = split_form(context.state.values)
        name = display_name([inspected.name], container_id)
        networks = [
            network_entry(await self._network(environment_id, network_name))
            for network_name in user_defined_network_names(context.state.values)
        ]
        image = await self._image(client, inspected.image)
        reference = image_reference(form.get("image") or inspected.config.image, image, name)
        mounts = mount_rows(context.state.values)
        candidates = await self._candidates(actor, environment_id, client, mounts)
        digest = review_digest(
            container_id=container_id,
            form=form,
            environment=environment,
            networks=networks,
            image=reference,
            volumes=[(c.choice.name, c.choice.driver, c.choice.refusal or "") for c in candidates],
        )
        notices = tuple(
            NotExportedGroup(reason=notice.kind, items=list(notice.items))
            for notice in context.notices
        )
        return _Snapshot(
            container_id=container_id,
            name=name,
            state=inspected.state.status,
            is_running=inspected.state.status in FORCEABLE_STATES,
            form=form,
            environment=environment,
            networks=tuple(networks),
            image=reference,
            binds=tuple(row for row in mounts if row.kind == "bind"),
            candidates=tuple(candidates),
            groups=group_counts(context.state.values),
            notices=notices,
            sentences=tuple(notice.text for notice in context.notices),
            digest=digest,
        )

    async def _network(self, environment_id: int, name: str) -> Network:
        try:
            return await self._inspector.network_named(environment_id, name)
        except ResourceNotFound:
            raise ContainerExportRefused(
                "network_unreadable", "A network the container is attached to could not be read."
            ) from None

    async def _image(self, client: EngineClient, image_id: str) -> ImageInspect | None:
        try:
            return await client.images.inspect(image_id)
        except NotFound:
            return None
        except EngineError as error:
            raise unavailable_from(error) from None

    async def _candidates(
        self,
        actor: Principal,
        environment_id: int,
        client: EngineClient,
        mounts: Sequence[MountRow],
    ) -> list[_Candidate]:
        targets: dict[str, list[str]] = {}
        for row in mounts:
            if row.kind == "volume" and row.source:
                targets.setdefault(row.source, []).append(row.target)
        if not targets:
            return []
        users = volume_users(await read_containers(client))
        found: list[_Candidate] = []
        for name, paths in targets.items():
            running = sum(c.state in FORCEABLE_STATES for c in users.get(name, ()))
            found.append(await self._candidate(actor, environment_id, name, tuple(paths), running))
        return found

    async def _candidate(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        targets: tuple[str, ...],
        running: int,
    ) -> _Candidate:
        try:
            await self._authoriser.require_access(
                actor, ResourceKey(environment_id, "volume", name), "read"
            )
            volume = await self._inspector.volume(environment_id, name)
        except ResourceNotFound:
            return _Candidate(
                VolumeChoice(name, targets, "", _VOLUME_GONE, running), code="volume_gone"
            )
        code, refusal = self._volume_refusal(environment_id, volume)
        return _Candidate(
            VolumeChoice(name, targets, volume.driver, refusal, running), volume, code
        )

    def _volume_refusal(
        self, environment_id: int, volume: Volume
    ) -> tuple[str, str] | tuple[None, None]:
        identity: SelfIdentity = self._self_identity()
        if volume.driver != SUPPORTED_DRIVER:
            return "plugin_driver", _PLUGIN_DRIVER
        device = volume.options.get("device")
        if identity.protects_volume(environment_id, volume.name) or (
            device and identity.protects_path(environment_id, device)
        ):
            return SELF_PROTECTED_REASON, str(SelfProtected.export(volume.name))
        return None, None

    @staticmethod
    def _marked(snapshot: _Snapshot, secret_names: Sequence[str]) -> frozenset[int]:
        known = {row.name for row in snapshot.environment}
        if any(name not in known for name in secret_names):
            raise ValidationFailed(
                {"secret": "A variable marked secret is not one of the container's variables."}
            )
        marked = set(secret_names)
        return frozenset(
            index for index, row in enumerate(snapshot.environment) if row.name in marked
        )

    @staticmethod
    def _chosen(snapshot: _Snapshot, chosen: Sequence[str]) -> list[Volume]:
        audit = current_audit()
        volumes: list[Volume] = []
        for name in dict.fromkeys(chosen):
            candidate = snapshot.volume_named(name)
            if candidate is None:
                raise ValidationFailed(
                    {"volume": "A chosen volume is not one this container mounts."}
                )
            if candidate.code is not None or candidate.volume is None:
                audit.reason = candidate.code
                if candidate.code == SELF_PROTECTED_REASON:
                    raise SelfProtected.export(name)
                raise ContainerExportRefused(
                    candidate.code or "volume_gone", candidate.choice.refusal or _VOLUME_GONE
                )
            volumes.append(candidate.volume)
        return volumes

    async def _helper_image(self, client: EngineClient) -> str:
        audit = current_audit()
        try:
            return await helper_image(client)
        except HelperImageMissing:
            audit.reason = "helper_image_missing"
            raise ContainerExportRefused("helper_image_missing", _HELPER_MISSING) from None
        except EngineError as error:
            audit.reason = failure_reason(error)
            raise unavailable_from(error) from None

    def _volume_manifest(self, volume: Volume) -> VolumeManifest:
        return build_manifest(
            exported_at=self._clock(),
            instance_version=product_version(),
            name=volume.name,
            driver=volume.driver,
            labels=volume.labels,
            options=volume.options,
        )

    @staticmethod
    def _opener(
        actor: Principal, client: EngineClient, image: str, volume_name: str
    ) -> VolumeOpener:
        @asynccontextmanager
        async def opened() -> AsyncIterator[AsyncIterator[bytes]]:
            async with HostFileReader(client).volume_session(
                actor, volume_name, image=image
            ) as helper:
                directory = await helper.stream()
                try:
                    yield directory
                finally:
                    await directory.aclose()

        return opened
