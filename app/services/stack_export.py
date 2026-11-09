"""Exports a managed Compose or Swarm stack as one gzip archive; administrators only."""

import asyncio
import hashlib
import hmac
import posixpath
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial
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
from app.db.repositories.environment import Environment
from app.db.repositories.registry_credential import SecretUnreadableError
from app.db.repositories.stack import StackKind, StackRepository, StoredStack
from app.docker_engine.archive import ArchiveError, DirectoryStream
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, EnvironmentUnreachable
from app.docker_engine.models import Volume
from app.logging import get_logger
from app.self_identity import SelfIdentity, SelfIdentitySource, no_identity
from app.services.access import canonical_stack_key
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
from app.services.export_archive_writer import ExportArchiveStream, VolumeSource
from app.services.export_build import (
    ContextInput,
    ExportDescription,
    FileInput,
    PreparedExport,
    VariableInput,
    prepare_export,
)
from app.services.export_manifest import FORMAT_VERSION, MAX_VOLUMES, ExportKind, ImageReference
from app.services.export_sealing import DEFAULT_COST, SealCost, check_passphrase
from app.services.guards import require_administrator
from app.services.host_files import (
    MOUNT_POINT,
    HelperImageMissing,
    HelperMountRefused,
    HostFileReader,
    VolumeHelper,
    helper_image,
)
from app.services.host_objects import (
    audited_client,
    audited_list_read,
    describe_users,
    failure_reason,
    hold_all,
    volume_users,
)
from app.services.image_listing import read_images
from app.services.object_inspector import (
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
    unavailable_from,
)
from app.services.object_locks import LockHolder, ObjectKey, ObjectLockRegistry
from app.services.stack_document import STACK_NAME
from app.services.stack_export_host import (
    MAX_HOST_PATHS,
    ProjectHost,
    declared_host_paths,
    image_references,
    read_project_host,
)
from app.services.stack_export_views import (
    OWN_VOLUME_REASON,
    PLUGIN_DRIVER_REASON,
    UNREADABLE_VOLUME_REASON,
    ExportContextEntry,
    ExportFileEntry,
    ExportVariable,
    ExportVolumeOffer,
    StackExportConfirmation,
    context_member,
    file_member,
)
from app.services.swarm_gate import require_swarm
from app.services.swarm_stacks import NAMESPACE_ROUTE
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
    "CHANGED_SINCE_REVIEW",
    "StackChangedSinceReview",
    "StackExportBusy",
    "StackExportRefused",
    "StackExportService",
    "StackExportStream",
    "review_digest",
]

CHANGED_SINCE_REVIEW: Final = "changed_since_review"
MAX_OFFERED_VOLUMES: Final = MAX_VOLUMES
MAX_RUNNING_SHOWN: Final = 25

_ROOT: Final = posixpath.basename(MOUNT_POINT)
_NAME_LIMIT: Final = 255
_KINDS: Final[dict[StackKind, ExportKind]] = {"compose": "compose_stack", "swarm": "swarm_stack"}
_LABELS: Final[dict[StackKind, str]] = {"compose": "Compose stack", "swarm": "Swarm stack"}


class StackExportBusy(ProductError):
    """No export slot is free; the message says which limit was reached."""


class StackExportRefused(ProductError):
    """The stack cannot be exported now; `reason` is the stable code recorded on the row."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class StackChangedSinceReview(ValidationFailed):
    """The stack is no longer the one the caller reviewed; nothing was read."""

    def __init__(self) -> None:
        super().__init__({"stack": CHANGED_SINCE_REVIEW})


class StackExportIncomplete(ProductError):
    """The archive was not fully produced; raised only to leave the failure row."""


def review_digest(row: StoredStack) -> str:
    """Return the digest of what an export of the stack would carry, for the review to bind to."""
    text = f"{row.id}\n{row.compose_file_digest}\n{row.inputs_digest or ''}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _name_argument(arguments: Arguments) -> str:
    return str(arguments["name"])[:_NAME_LIMIT]


@dataclass(slots=True)
class _Survey:
    environment: Environment
    row: StoredStack
    client: EngineClient
    host: ProjectHost
    offers: dict[str, Volume | None]


@dataclass(frozen=True, slots=True)
class _Outcome:
    actor: Principal
    environment_id: int
    name: str
    shape: dict[str, DetailValue]
    archive_bytes: int
    volumes_done: int
    skipped: int
    completed: bool
    reason: str
    helpers_removed: bool


class StackExportStream:
    """The chunks of one stack export; releases the helpers, the locks and the slot when it ends.

    `aclose` ends it from outside and is safe to call after the last chunk or more than once.
    """

    media_type: Final = ARCHIVE_MEDIA_TYPE
    suffix: Final = ARCHIVE_SUFFIX

    def __init__(
        self,
        *,
        service: "StackExportService",
        actor: Principal,
        environment_id: int,
        name: str,
        shape: dict[str, DetailValue],
        archive: ExportArchiveStream,
        stack: AsyncExitStack,
        slot: ExportSlot,
        helpers: list[VolumeHelper],
    ) -> None:
        self._service = service
        self._actor = actor
        self._environment_id = environment_id
        self._name = name
        self._shape = shape
        self._archive = archive
        self._stack = stack
        self._slot = slot
        self.helpers = helpers
        self._closed = False
        self._completed = False
        self._reason = "interrupted"

    def __aiter__(self) -> "StackExportStream":
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
        """End the export, release what it held and record how it ended."""
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
            name=self._name,
            shape=self._shape,
            archive_bytes=self._archive.bytes_out,
            volumes_done=len(results),
            skipped=sum(item.skipped_count for item in results),
            completed=self._completed,
            reason=self._reason,
            helpers_removed=all(helper.removed for helper in self.helpers),
        )


def _reason_of(error: Exception) -> str:
    if isinstance(error, StreamMalformed):
        return "malformed_stream"
    if isinstance(error, ArchiveError):
        return "unreadable"
    if isinstance(error, HelperMountRefused):
        return "helper_refused"
    if isinstance(error, EnvironmentUnreachable):
        return "unreachable"
    if isinstance(error, EngineError):
        return failure_reason(error)
    return type(error).__name__


class StackExportService:
    """Opens stack exports and records them; the caller streams the result and closes it."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        stacks: StackRepository,
        locks: ObjectLockRegistry,
        activity: ActivityRecorder,
        clock: Clock,
        self_identity: SelfIdentitySource | None = None,
        slots: ExportSlots | None = None,
        seal_cost: SealCost | None = None,
    ) -> None:
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._stacks = stacks
        self._locks = locks
        self._activity = activity
        self._clock = clock
        self._self_identity = self_identity or no_identity
        self._slots = shared_export_slots() if slots is None else slots
        self._cost = DEFAULT_COST if seal_cost is None else seal_cost

    async def confirmation(
        self, actor: Principal, environment_id: int, kind: StackKind, name: str
    ) -> StackExportConfirmation:
        """Name everything an export of the stack holds and leaves out; nothing is recorded.

        ResourceNotFound unless the caller is an administrator and the managed stack exists,
        SelfProtected for dockmin's own stack or while its mounts are unknown, and the
        unavailable and list-too-large errors when the host cannot be read in full.
        """
        require_administrator(actor)
        environment, row = await self._stack(actor, environment_id, kind, name)
        client = await self._hosts.client(environment_id)
        survey = await self._survey(environment, row, client)
        self._refuse_protected(environment_id, survey)
        variables = await self._stacks.variables(row.id)
        files = await self._stacks.files.files(row.id)
        contexts = await self._stacks.contexts.contexts(row.id)
        local_images = await read_images(client)
        references = await asyncio.to_thread(image_references, row.compose_file, local_images)
        paths = await asyncio.to_thread(declared_host_paths, row.compose_file)
        offers = self._offers(environment_id, survey)
        image: str | None = None
        if any(offer.selectable for offer in offers):
            try:
                image = await helper_image(client)
            except HelperImageMissing:
                image = None
            except EngineError as error:
                raise unavailable_from(error) from None
        running = [c for c in survey.host.containers if c.state in FORCEABLE_STATES]
        named, more, _hidden = describe_users(
            running, frozenset(c.id for c in running), MAX_RUNNING_SHOWN
        )
        return StackExportConfirmation(
            environment_id=environment_id,
            environment_name=environment.name,
            kind=kind,
            name=row.name,
            expected_digest=review_digest(row),
            compose_bytes=len(row.compose_file.encode("utf-8")),
            variables=tuple(ExportVariable(item.name, item.is_secret) for item in variables),
            files=tuple(
                ExportFileEntry(
                    item.name,
                    item.is_secret,
                    item.size_bytes,
                    None if item.is_secret else file_member(item.name),
                )
                for item in files
            ),
            contexts=tuple(
                ExportContextEntry(item.name, item.size_bytes, context_member(index))
                for index, item in enumerate(contexts)
            ),
            images=tuple(references),
            host_paths=paths[:MAX_HOST_PATHS],
            host_paths_more=max(len(paths) - MAX_HOST_PATHS, 0),
            volumes=tuple(offers[:MAX_OFFERED_VOLUMES]),
            volumes_more=max(len(offers) - MAX_OFFERED_VOLUMES, 0),
            running=named,
            running_total=len(named) + more,
            helper_image=image,
        )

    async def export(
        self,
        actor: Principal,
        environment_id: int,
        kind: StackKind,
        name: str,
        expected_digest: str,
        passphrase: str | None,
        chosen_volumes: Sequence[str],
    ) -> StackExportStream:
        """Check the stack, seal its secrets and return the archive stream; the caller closes it.

        Secret values are sealed under `passphrase`, or left out when it is None. Everything that
        can refuse the export has run, and the started row is written, before this returns; no
        volume byte has been read. ResourceNotFound, SelfProtected, StackChangedSinceReview,
        StackExportRefused, StackExportBusy, ValidationFailed for a passphrase or a chosen volume
        that is not acceptable, ObjectBusy, EnvironmentUnavailable.
        """
        opened: list[StackExportStream] = []
        try:
            return await self._open(
                actor,
                environment_id,
                kind,
                name,
                expected_digest,
                passphrase,
                tuple(chosen_volumes),
                opened,
            )
        except BaseException:
            with anyio.CancelScope(shield=True):
                for stream in opened:
                    await stream.aclose()
            raise

    @audited(
        action="stack_export_started",
        target_type="stack",
        target_id=_name_argument,
        environment_id=environment_argument,
    )
    async def _open(
        self,
        actor: Principal,
        environment_id: int,
        kind: StackKind,
        name: str,
        expected_digest: str,
        passphrase: str | None,
        chosen_volumes: tuple[str, ...],
        opened: list[StackExportStream],
    ) -> StackExportStream:
        audit = current_audit()
        require_administrator(actor)
        environment, row = await self._stack(actor, environment_id, kind, name)
        audit.target_id = row.name
        audit.detail.update(stack_kind=kind, format_version=FORMAT_VERSION)
        client = await audited_client(self._hosts, environment_id)
        survey = await audited_list_read(self._survey(environment, row, client))
        try:
            self._refuse_protected(environment_id, survey)
        except SelfProtected:
            audit.reason = SELF_PROTECTED_REASON
            raise
        if passphrase is not None:
            try:
                check_passphrase(passphrase)
            except ValidationFailed:
                audit.reason = "invalid_passphrase"
                raise
        chosen = self._chosen(survey, chosen_volumes)
        try:
            slot = self._slots.acquire(environment_id)
        except VolumeExportBusy:
            audit.reason = "export_busy"
            raise StackExportBusy(
                "An export is already running on this environment, or too many are running on "
                "this instance. Try again when one has finished."
            ) from None
        stack = AsyncExitStack()
        try:
            stream = await self._prepare(
                actor, survey, expected_digest, passphrase, chosen, stack, slot
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
        survey: _Survey,
        expected_digest: str,
        passphrase: str | None,
        chosen: list[str],
        stack: AsyncExitStack,
        slot: ExportSlot,
    ) -> StackExportStream:
        audit = current_audit()
        environment_id = survey.environment.id
        holder = LockHolder(UserActor.of(actor), actor.username, "export")
        keys = [
            ObjectKey(environment_id, "stack", canonical_stack_key(survey.row.name)),
            *(ObjectKey(environment_id, "volume", volume) for volume in chosen),
        ]
        await stack.enter_async_context(hold_all(self._locks, keys, holder))
        row = await self._reviewed(survey.row, expected_digest)
        manifests = await self._volume_manifests(survey, chosen)
        image = await self._helper_image(survey.client) if chosen else None
        references = await asyncio.to_thread(
            image_references,
            row.compose_file,
            await audited_list_read(read_images(survey.client)),
        )
        prepared = await self._seal(row, passphrase, references, manifests)
        shape = self._shape(prepared)
        audit.detail.update(shape)
        helpers: list[VolumeHelper] = []
        sources = [
            VolumeSource(
                _ROOT,
                partial(
                    self._volume_chunks, actor, survey.client, section.manifest.name, image, helpers
                ),
            )
            for section in prepared.manifest.volumes
        ]
        archive = ExportArchiveStream(prepared, sources)
        return StackExportStream(
            service=self,
            actor=actor,
            environment_id=environment_id,
            name=row.name,
            shape=shape,
            archive=archive,
            stack=stack,
            slot=slot,
            helpers=helpers,
        )

    async def _reviewed(self, held: StoredStack, expected_digest: str) -> StoredStack:
        current = await self._stacks.get_by_id(held.id)
        if current is None or not hmac.compare_digest(
            review_digest(current).encode(), expected_digest.encode("utf-8")
        ):
            current_audit().reason = CHANGED_SINCE_REVIEW
            raise StackChangedSinceReview()
        return current

    async def _volume_manifests(self, survey: _Survey, chosen: list[str]) -> list[VolumeManifest]:
        audit = current_audit()
        environment_id = survey.environment.id
        manifests: list[VolumeManifest] = []
        for name in chosen:
            self._inspector.forget_volume(environment_id, name)
            try:
                volume = await self._inspector.volume(environment_id, name)
            except ResourceNotFound:
                audit.reason = "volume_gone"
                raise StackExportRefused(
                    "volume_gone", f"The volume {name} no longer exists."
                ) from None
            except EnvironmentUnavailable as error:
                audit.reason = error.kind
                raise
            except ObjectTooLarge:
                audit.reason = "too_large"
                raise
            refusal = self._volume_refusal(environment_id, volume)
            if refusal is not None:
                audit.reason = refusal.reason
                raise refusal
            manifests.append(
                build_manifest(
                    exported_at=self._clock(),
                    instance_version=product_version(),
                    name=volume.name,
                    driver=volume.driver,
                    labels=volume.labels,
                    options=volume.options,
                )
            )
        return manifests

    async def _helper_image(self, client: EngineClient) -> str:
        audit = current_audit()
        try:
            return await helper_image(client)
        except HelperImageMissing:
            audit.reason = "helper_image_missing"
            raise StackExportRefused(
                "helper_image_missing",
                "The host has no image to create the helper container from. "
                "Pull or load an image on the host, then try again.",
            ) from None
        except EngineError as error:
            audit.reason = failure_reason(error)
            raise unavailable_from(error) from None

    async def _seal(
        self,
        row: StoredStack,
        passphrase: str | None,
        references: Sequence[ImageReference],
        manifests: list[VolumeManifest],
    ) -> PreparedExport:
        audit = current_audit()
        variables = await self._stacks.variables(row.id)
        files = await self._stacks.files.files(row.id)
        context_rows = await self._stacks.contexts.contexts(row.id)
        contexts: list[ContextInput] = []
        for item in context_rows:
            archive = await self._stacks.contexts.archive(row.id, item.name)
            if archive is None:
                audit.reason = CHANGED_SINCE_REVIEW
                raise StackChangedSinceReview()
            contexts.append(
                ContextInput(
                    item.name,
                    archive.content,
                    item.compressed,
                    item.member_count,
                    item.extracted_size,
                )
            )
        sealed = passphrase is not None
        open_variables: Callable[[], dict[str, str]] | None = None
        open_files: Callable[[], dict[str, str]] | None = None
        if sealed and any(item.is_secret for item in variables):
            open_variables = await self._stacks.secret_variable_opener(row.id)
        if sealed and any(item.is_secret for item in files):
            open_files = await self._stacks.files.secret_file_opener(row.id)

        def secret_value(opened: dict[str, str], name: str) -> str:
            if not sealed:
                return ""
            try:
                return opened[name]
            except KeyError:
                raise SecretUnreadableError("a stored secret cannot be read") from None

        def build() -> PreparedExport:
            values = open_variables() if open_variables is not None else {}
            contents = open_files() if open_files is not None else {}
            description = ExportDescription(
                kind=_KINDS[row.kind],
                name=row.name,
                description=f"{_LABELS[row.kind]} {row.name}",
                instance_version=product_version(),
                exported_at=self._clock(),
                compose_file=row.compose_file,
                variables=tuple(
                    VariableInput(
                        item.name,
                        item.is_secret,
                        secret_value(values, item.name) if item.is_secret else item.value or "",
                    )
                    for item in variables
                ),
                files=tuple(
                    FileInput(
                        item.name,
                        item.is_secret,
                        _secret_content(sealed, item.size_bytes, secret_value(contents, item.name))
                        if item.is_secret
                        else (item.content or "").encode("utf-8"),
                    )
                    for item in files
                ),
                contexts=tuple(contexts),
                images=tuple(references),
                volumes=tuple(manifests),
            )
            return prepare_export(description, passphrase, cost=self._cost)

        try:
            return await asyncio.to_thread(build)
        except SecretUnreadableError:
            audit.reason = "secrets_unreadable"
            raise StackExportRefused(
                "secrets_unreadable", "A stored secret cannot be read with this server's key."
            ) from None

    def _shape(self, prepared: PreparedExport) -> dict[str, DetailValue]:
        manifest = prepared.manifest
        section = manifest.stack
        assert section is not None
        return {
            "name": manifest.name,
            "stack_kind": section.stack_kind,
            "variables": len(section.variables),
            "secret_values": sum(item.secret for item in section.variables)
            + sum(item.secret for item in section.files),
            "files": len(section.files),
            "contexts": len(section.contexts),
            "volumes": len(manifest.volumes),
            "secrets": manifest.secrets,
            "format_version": manifest.format_version,
        }

    @asynccontextmanager
    async def _volume_chunks(
        self,
        actor: Principal,
        client: EngineClient,
        name: str,
        image: str | None,
        helpers: list[VolumeHelper],
    ) -> AsyncIterator[DirectoryStream]:
        if image is None:
            raise RuntimeError("a volume was chosen without a helper image")
        async with HostFileReader(client).volume_session(actor, name, image=image) as helper:
            helpers.append(helper)
            directory = await helper.stream()
            try:
                yield directory
            finally:
                await directory.aclose()

    @audited(
        action="stack_exported",
        target_type="stack",
        target_id=_name_argument,
        environment_id=environment_argument,
    )
    async def _record_finish(
        self, actor: Principal, environment_id: int, name: str, outcome: _Outcome
    ) -> None:
        audit = current_audit()
        audit.detail.update(outcome.shape)
        audit.detail.update(
            bytes=outcome.archive_bytes,
            volumes_done=outcome.volumes_done,
            skipped=outcome.skipped,
            completed=outcome.completed,
            helpers_removed=outcome.helpers_removed,
        )
        if not outcome.completed:
            audit.reason = outcome.reason
            raise StackExportIncomplete("The export did not complete.")

    async def record_end(self, stream: StackExportStream) -> None:
        """Write the finished row of a stream; a row that cannot be written is logged."""
        outcome = stream.snapshot
        try:
            await self._record_finish(outcome.actor, outcome.environment_id, outcome.name, outcome)
        except StackExportIncomplete:
            pass
        except Exception as error:
            logger.error(
                "activity_event_write_failed",
                extra={"action": "stack_exported", "error": type(error).__name__},
            )

    async def _stack(
        self, actor: Principal, environment_id: int, kind: StackKind, name: str
    ) -> tuple[Environment, StoredStack]:
        if kind == "compose":
            await self._authoriser.require_environment(actor, environment_id)
            environment = await self._hosts.environment(environment_id)
            if STACK_NAME.fullmatch(name) is None:
                raise ResourceNotFound("not found")
            row = await self._stacks.get(environment_id, name)
        else:
            environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
            if NAMESPACE_ROUTE.fullmatch(name) is None:
                raise ResourceNotFound("not found")
            row = await self._stacks.get_folded(environment_id, name)
        if row is None or row.kind != kind:
            raise ResourceNotFound("not found")
        return environment, row

    async def _survey(
        self, environment: Environment, row: StoredStack, client: EngineClient
    ) -> _Survey:
        host = await read_project_host(client, row.kind, environment.id, row.name)
        offers: dict[str, Volume | None] = dict(host.labelled_volumes)
        for name in sorted(host.mounted_volumes - offers.keys()):
            try:
                offers[name] = await self._inspector.volume(environment.id, name)
            except ResourceNotFound:
                continue
            except ObjectTooLarge:
                offers[name] = None
        return _Survey(environment, row, client, host, offers)

    def _offers(self, environment_id: int, survey: _Survey) -> list[ExportVolumeOffer]:
        users = volume_users(survey.host.containers)
        offers: list[ExportVolumeOffer] = []
        for name in sorted(survey.offers):
            volume = survey.offers[name]
            running = sum(c.state in FORCEABLE_STATES for c in users.get(name, ()))
            if volume is None:
                offers.append(ExportVolumeOffer(name, "", UNREADABLE_VOLUME_REASON, running))
                continue
            refusal = self._volume_refusal(environment_id, volume)
            reason = None
            if isinstance(refusal, SelfProtected):
                reason = OWN_VOLUME_REASON
            elif refusal is not None:
                reason = PLUGIN_DRIVER_REASON
            offers.append(ExportVolumeOffer(name, volume.driver, reason, running))
        return offers

    def _chosen(self, survey: _Survey, names: Sequence[str]) -> list[str]:
        audit = current_audit()
        chosen = sorted(set(names))
        if len(chosen) > MAX_OFFERED_VOLUMES:
            audit.reason = "too_many_volumes"
            raise ValidationFailed({"volumes": f"Choose at most {MAX_OFFERED_VOLUMES} volumes."})
        if any(name not in survey.offers for name in chosen):
            audit.reason = "volume_not_offered"
            raise ValidationFailed(
                {"volumes": "A chosen volume is no longer one of this stack's volumes."}
            )
        return chosen

    def _volume_refusal(
        self, environment_id: int, volume: Volume
    ) -> SelfProtected | StackExportRefused | None:
        identity = self._self_identity()
        device = volume.options.get("device")
        if identity.protects_volume(environment_id, volume.name) or (
            device and identity.protects_path(environment_id, device)
        ):
            return SelfProtected.export(volume.name)
        if volume.driver != SUPPORTED_DRIVER:
            return StackExportRefused("plugin_driver", PLUGIN_DRIVER_REASON)
        return None

    def _refuse_protected(self, environment_id: int, survey: _Survey) -> None:
        identity: SelfIdentity = self._self_identity()
        if identity.mounts_unknown(environment_id):
            raise SelfProtected.export_unverified()
        row = survey.row
        if row.kind == "compose":
            own = identity.protects_stack(environment_id, row.name) or any(
                identity.protects_container(container.id) for container in survey.host.containers
            )
        else:
            own = any(
                identity.protects_service(environment_id, service.id)
                for service in survey.host.services
            )
        if own:
            raise SelfProtected.export_stack(row.name)


def _secret_content(sealed: bool, size: int, opened: str) -> bytes:
    """Return the content to seal, or a placeholder of the stored size when secrets are left out."""
    return opened.encode("utf-8") if sealed else b"\0" * max(size, 1)
