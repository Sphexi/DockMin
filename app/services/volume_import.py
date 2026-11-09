"""Imports a volume export archive as a new volume on an environment; administrators only."""

import asyncio
import hashlib
import hmac
import posixpath
import re
import secrets
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Final

import anyio

from app.auth.errors import (
    SELF_PROTECTED_REASON,
    ProductError,
    SelfProtected,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.db.engine import Database, Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.docker_engine.archive import ArchiveError
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, NotFound, ResponseTooLarge
from app.docker_engine.models import Volume
from app.logging import get_logger
from app.self_identity import SelfIdentitySource, no_identity
from app.services.access import ResourceKey, SubmittedLabels
from app.services.acting import Acting
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.build_context_archive import run_limited
from app.services.container_actions import ANONYMOUS_VOLUME_LABEL
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
    audited_client,
    create_failure,
    failure_reason,
    hold_all,
    in_transaction,
    run_shielded,
    settled,
    write_settled_row,
)
from app.services.object_inspector import (
    EngineObjectInspector,
    ObjectTooLarge,
    unavailable_from,
)
from app.services.object_locks import LockHolder, ObjectKey, ObjectLockRegistry
from app.services.resource_controls import ResourceControlService
from app.services.volume_export_writer import ChunkSource, StreamMalformed, TarReader
from app.services.volume_import_archive import (
    ArchiveSummary,
    VolumeArchiveRefused,
    inspect_volume_archive,
)
from app.services.volume_import_store import (
    HeldImport,
    ImportStoreFull,
    SpoolFailure,
    VolumeImportStore,
    spool_upload,
)
from app.services.volume_import_stream import RewrittenArchive
from app.services.volume_manifest import filter_labels
from app.services.volume_views import VolumeNameInUse, VolumeRefused
from app.services.volumes import VolumeRequest, created_as_requested, parse_volume_form

logger = get_logger(__name__)

__all__ = [
    "ImportChangedSinceReview",
    "ImportReview",
    "ImportUploadGone",
    "MountConfirmation",
    "VolumeImportFailed",
    "VolumeImportRefused",
    "VolumeImportService",
    "VolumeImported",
    "mount_confirmation",
    "option_rows",
    "options_of_rows",
]

SUPPORTED_DRIVER: Final = "local"
SETTLE_SECONDS: Final = 120.0
REMOVE_SECONDS: Final = 60.0
LABEL_VALUE_CHARS: Final = 256
IMPORT_LABEL: Final = "dockmin.import"
EMPTY_READ_BYTES: Final = 64 * 1024
_NAME_LIMIT: Final = 255
_ACTION: Final = "volume_imported"
_ROOT_ENTRY: Final = posixpath.basename(MOUNT_POINT)
_OPTION_ROW: Final = re.compile(r"option-(\d+)-(key|value)")
_MEMORY_TYPES: Final = frozenset({"", "tmpfs"})

PLUGIN_DRIVER_TEXT: Final = (
    "Only volumes of the local driver can be imported. "
    "A plugin driver can run code on the host when its volume is mounted."
)
_SPOOL_TEXT: Final = {
    "too_large": "The archive is larger than the upload limit.",
    "insufficient_space": "There is not enough free space on the data volume for this upload.",
}
STORE_FULL_TEXT: Final = (
    "Other uploads are waiting for review. Try again when one has been imported or discarded."
)
MOUNT_UNCONFIRMED_TEXT: Final = (
    "Confirm that the archive's files may be written to the host path or share named above."
)


class ImportUploadGone(ProductError):
    """The upload is not held for this session: it expired, was cancelled or was replaced."""

    def __init__(self) -> None:
        super().__init__(
            "This upload is no longer held. Upload the archive again; nothing was changed."
        )


class ImportChangedSinceReview(ValidationFailed):
    """The held archive is not the one the review showed; nothing was changed."""

    def __init__(self) -> None:
        super().__init__(
            {"archive": "The archive changed since you reviewed it; nothing was changed."}
        )


class VolumeImportRefused(ProductError):
    """The import cannot start; `reason` is the stable code recorded on the row."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _removal_text(*, volume_removed: bool, host_backed: bool) -> str:
    if not volume_removed:
        return (
            "The volume this import created could not be removed; remove it from the volumes list."
        )
    if host_backed:
        return (
            "The volume this import created was removed. Files already written to its host path "
            "or share are not removed with it."
        )
    return "The volume this import created was removed."


class VolumeImportFailed(ProductError):
    """The data could not be written after the volume was created.

    `volume_removed` says whether the volume this import created is gone; `host_backed` that its
    options bound a host path or share, where removing the volume does not remove written files.
    """

    def __init__(self, reason: str, *, volume_removed: bool, host_backed: bool = False) -> None:
        tail = _removal_text(volume_removed=volume_removed, host_backed=host_backed)
        super().__init__(f"The archive could not be written to the new volume. {tail}")
        self.reason = reason
        self.volume_removed = volume_removed
        self.host_backed = host_backed


def _not_empty_text(*, volume_removed: bool) -> str:
    tail = _removal_text(volume_removed=volume_removed, host_backed=False)
    return (
        "The new volume already holds files, so nothing was written to it and the files that "
        f"are there were not touched. {tail}"
    )


class VolumeNotEmpty(Exception):
    """The new volume already holds files, or its contents could not be read as empty."""


@dataclass(frozen=True, slots=True)
class MountConfirmation:
    """The host path or share the options bind; `token` is bound to the digest, type and device."""

    token: str
    device: str
    type: str


def mount_confirmation(digest: str, options: Mapping[str, str]) -> MountConfirmation | None:
    """Return what needs confirming when the options bind a host path or mount a share."""
    device = options.get("device", "").strip()
    kind = options.get("type", "").strip()
    if not device and kind.casefold() in _MEMORY_TYPES:
        return None
    token = hashlib.sha256("\0".join((digest, kind, device)).encode("utf-8")).hexdigest()
    return MountConfirmation(token, device, kind)


def options_of_rows(rows: Mapping[str, str]) -> dict[str, str]:
    """Return the options the numbered form rows name; blank rows and repeated keys are skipped."""
    found: dict[int, dict[str, str]] = {}
    for name, value in rows.items():
        if (row := _OPTION_ROW.fullmatch(name)) is not None:
            found.setdefault(int(row[1]), {})[row[2]] = value
    options: dict[str, str] = {}
    for index in sorted(found):
        key, value = found[index].get("key", "").strip(), found[index].get("value", "").strip()
        if key and key not in options:
            options[key] = value
    return options


@dataclass(frozen=True, slots=True)
class ImportReview:
    """What the review page names: everything the import creates and everything it drops."""

    upload_id: str
    environment_id: int
    environment_name: str
    digest: str
    archive_size: int
    summary: ArchiveSummary
    name: str
    driver: str
    options: tuple[tuple[str, str], ...]
    labels: tuple[tuple[str, str], ...]
    labels_dropped: tuple[str, ...]
    removed_option_keys: tuple[str, ...]
    name_taken: bool
    helper_image: str | None
    helper_name_prefix: str = HELPER_NAME_PREFIX

    @property
    def can_import(self) -> bool:
        """Report whether a helper image is available on the host."""
        return self.helper_image is not None

    def mount_for(self, rows: Mapping[str, str]) -> MountConfirmation | None:
        """Return what the options in these form rows need confirmed."""
        return mount_confirmation(self.digest, options_of_rows(rows))


@dataclass(frozen=True, slots=True)
class VolumeImported:
    """The outcome of an import."""

    environment_id: int
    name: str
    driver: str
    members: int
    bytes: int
    format_version: int
    labels_applied: int
    labels_dropped: tuple[str, ...]
    control_created: bool


@dataclass(slots=True)
class _Run:
    created: bool = False
    keep_upload: bool = False
    token: str = field(default_factory=lambda: secrets.token_hex(16))


def _name_argument(arguments: Arguments) -> str:
    return str(arguments["name"])[:_NAME_LIMIT]


def split_labels(labels: Mapping[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """Return the labels an import applies, and the keys it drops.

    Stack, product and Docker-set labels are never applied: the first two grant access or belong
    to the product, and Docker sets the third itself.
    """
    kept = {
        key: value
        for key, value in filter_labels(labels).items()
        if key.strip().casefold() != ANONYMOUS_VOLUME_LABEL
    }
    return kept, tuple(sorted(key for key in labels if key not in kept))


def _label_rows(labels: Mapping[str, str]) -> dict[str, str]:
    rows: dict[str, str] = {}
    for index, (key, value) in enumerate(labels.items()):
        rows[f"label-{index}-key"] = key
        rows[f"label-{index}-value"] = value
    return rows


def option_rows(options: Mapping[str, str]) -> dict[str, str]:
    """Return an options mapping as the numbered form rows the review page submits."""
    rows: dict[str, str] = {}
    for index, (key, value) in enumerate(options.items()):
        rows[f"option-{index}-key"] = key
        rows[f"option-{index}-value"] = value
    return rows


def _option_fields(rows: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in rows.items() if key.startswith("option-")}


def _clip(value: str) -> str:
    return value if len(value) <= LABEL_VALUE_CHARS else f"{value[:LABEL_VALUE_CHARS]}..."


async def _take(
    controls: ResourceControlService, key: ResourceKey, owner_user_id: int | None
) -> tuple[bool, bool]:
    replaced = await controls.release(key)
    return await controls.claim(key, owner_user_id), replaced


class VolumeImportService:
    """Receives, reviews and imports volume archives; every import is one audited operation."""

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
        store: VolumeImportStore,
        data_dir: Path,
        self_identity: SelfIdentitySource | None = None,
    ) -> None:
        self._db = db
        self._database = database
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._locks = locks
        self._controls = controls
        self._activity = activity
        self._clock = clock
        self._store = store
        self._data_dir = data_dir
        self._self_identity = self_identity or no_identity

    async def receive(
        self,
        actor: Principal,
        session_id: int,
        environment_id: int,
        parts: AsyncIterator[bytes],
        *,
        declared_size: int | None = None,
    ) -> str:
        """Spool and fully validate an upload, hold it, and return its upload id.

        Nothing is created on any host and no row is written. The file is removed on every way
        out but success. ResourceNotFound unless the caller is an administrator who can see the
        environment; ValidationFailed on the `archive` field for a refused archive.
        """
        await self._require_environment(actor, environment_id)
        if not await self._store.has_room(actor.user_id):
            raise ValidationFailed({"archive": STORE_FULL_TEXT})
        upload_id = self._store.new_id()
        try:
            upload = await spool_upload(
                parts, self._data_dir, upload_id, declared_size=declared_size
            )
        except SpoolFailure as failure:
            raise ValidationFailed({"archive": _SPOOL_TEXT[failure.reason]}) from None
        try:
            summary = await run_limited(inspect_volume_archive, upload.path)
            if summary.manifest.driver != SUPPORTED_DRIVER:
                raise ValidationFailed({"archive": PLUGIN_DRIVER_TEXT})
        except VolumeArchiveRefused as refused:
            await self._unlink(upload.path)
            member = f" Entry: {refused.member}." if refused.member else ""
            raise ValidationFailed({"archive": f"{refused.message}{member}"}) from None
        except BaseException:
            await self._unlink(upload.path)
            raise
        try:
            held = await self._store.hold(
                upload_id,
                user_id=actor.user_id,
                session_id=session_id,
                environment_id=environment_id,
                upload=upload,
                summary=summary,
            )
        except ImportStoreFull:
            await self._unlink(upload.path)
            raise ValidationFailed({"archive": STORE_FULL_TEXT}) from None
        except BaseException:
            await self._unlink(upload.path)
            raise
        return held.id

    async def review(
        self, actor: Principal, session_id: int, environment_id: int, upload_id: str
    ) -> ImportReview:
        """Describe what importing the held archive creates and drops; nothing is changed.

        ImportUploadGone when the upload is not held for this session.
        """
        await self._require_environment(actor, environment_id)
        held = await self._held(actor, session_id, environment_id, upload_id)
        manifest = held.summary.manifest
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        labels, dropped = split_labels(manifest.labels)
        try:
            taken = await self._exists(client, manifest.name)
            try:
                image: str | None = await helper_image(client)
            except HelperImageMissing:
                image = None
        except EngineError as error:
            raise unavailable_from(error) from None
        return ImportReview(
            upload_id=held.id,
            environment_id=environment_id,
            environment_name=environment.name,
            digest=held.digest,
            archive_size=held.size,
            summary=held.summary,
            name=manifest.name,
            driver=manifest.driver,
            options=tuple(manifest.options.items()),
            labels=tuple((key, _clip(value)) for key, value in labels.items()),
            labels_dropped=dropped,
            removed_option_keys=tuple(manifest.removed_option_keys),
            name_taken=taken,
            helper_image=image,
        )

    async def cancel(
        self, actor: Principal, session_id: int, environment_id: int, upload_id: str
    ) -> bool:
        """Drop the held upload and remove its file; nothing else is changed.

        False when the upload is being imported and so was not dropped.
        """
        await self._require_environment(actor, environment_id)
        held = await self._store.get(
            upload_id,
            user_id=actor.user_id,
            session_id=session_id,
            environment_id=environment_id,
        )
        if held is None:
            return True
        if held.importing:
            return False
        await self._store.discard(held.id)
        return True

    @audited(
        action=_ACTION,
        target_type="volume",
        target_id=_name_argument,
        environment_id=environment_argument,
    )
    async def import_volume(
        self,
        actor: Principal,
        environment_id: int,
        upload_id: str,
        name: str,
        expected_digest: str,
        *,
        driver: str | None = None,
        options: Mapping[str, str] | None = None,
        confirm_mount: str = "",
        helper_image: str | None = None,
    ) -> VolumeImported:
        """Create a volume from the held archive and write its data; the caller owns it.

        `options` is the numbered `option-N-key` and `option-N-value` rows of the review, the
        archive's own when omitted. Options that bind a host path or mount a share need
        `confirm_mount`, the token of `mount_confirmation`. `helper_image` is the image id the
        review named, resolved now when omitted. The name must be absent on the host under the
        volume lock, the created volume must carry this import's label, and it must hold no
        file before the data is written. A volume created by this import is removed if the data
        cannot be written. The held upload is removed once data may have been written, and kept
        after a refusal that wrote nothing. ImportUploadGone, ImportChangedSinceReview,
        ValidationFailed, SelfProtected, VolumeNameInUse, VolumeImportRefused,
        VolumeImportFailed, VolumeRefused, EnvironmentUnavailable, ObjectBusy.
        """
        audit = current_audit()
        require_administrator(actor)
        await self._hosts.environment(environment_id)
        held = await self._held_for(actor, environment_id, upload_id)
        manifest = held.summary.manifest
        audit.detail.update(
            format_version=manifest.format_version,
            members=held.summary.members,
            bytes=held.summary.bytes,
        )
        if not hmac.compare_digest(held.digest.encode(), expected_digest.encode("utf-8")):
            audit.reason = "changed_since_review"
            raise ImportChangedSinceReview()
        if held.importing:
            audit.reason = "import_running"
            raise VolumeImportRefused("import_running", "This upload is already being imported.")
        held.importing = True
        run = _Run()
        try:
            labels, dropped = split_labels(manifest.labels)
            form: dict[str, str] = {
                "name": name,
                "driver": manifest.driver if driver is None else driver,
                **(option_rows(manifest.options) if options is None else _option_fields(options)),
                **_label_rows(labels),
            }
            request = self._validated(form)
            audit.target_id = request.name
            audit.detail.update(
                name=request.name,
                driver=request.driver,
                option_keys=",".join(request.options),
                labels=len(request.labels),
                labels_dropped=len(dropped),
            )
            self._refuse_protected(environment_id, request.options)
            host_backed = self._require_mount_confirmed(held.digest, request.options, confirm_mount)
            client = await audited_client(self._hosts, environment_id)
            holder = LockHolder(UserActor.of(actor), actor.username, "import")
            key = ObjectKey(environment_id, "volume", request.name)
            async with hold_all(self._locks, [key], holder):
                await self._require_absent(client, request.name)
                image = await self._image(client, helper_image)
                await self._create(actor, environment_id, client, request, run)
                await self._write(
                    actor, environment_id, client, request, image, held, run, host_backed
                )
                claimed = await self._claim(actor, environment_id, request.name)
        finally:
            held.importing = False
            if run.created and not run.keep_upload:
                with anyio.CancelScope(shield=True):
                    await self._store.discard(held.id)
        return VolumeImported(
            environment_id,
            request.name,
            request.driver,
            held.summary.members,
            held.summary.bytes,
            manifest.format_version,
            len(request.labels),
            dropped,
            claimed,
        )

    async def _require_environment(self, actor: Principal, environment_id: int) -> None:
        require_administrator(actor)
        await self._authoriser.require_environment(actor, environment_id)
        await self._hosts.environment(environment_id)

    async def _held(
        self, actor: Principal, session_id: int, environment_id: int, upload_id: str
    ) -> HeldImport:
        held = await self._store.get(
            upload_id,
            user_id=actor.user_id,
            session_id=session_id,
            environment_id=environment_id,
        )
        if held is None:
            raise ImportUploadGone()
        return held

    async def _held_for(self, actor: Principal, environment_id: int, upload_id: str) -> HeldImport:
        audit = current_audit()
        if actor.session_id is None:
            audit.reason = "upload_gone"
            raise ImportUploadGone()
        try:
            return await self._held(actor, actor.session_id, environment_id, upload_id)
        except ImportUploadGone:
            audit.reason = "upload_gone"
            raise

    def _validated(self, form: Mapping[str, str]) -> VolumeRequest:
        audit = current_audit()
        try:
            request = parse_volume_form(form)
            SubmittedLabels(request.labels)
            if request.driver != SUPPORTED_DRIVER:
                raise ValidationFailed({"driver": PLUGIN_DRIVER_TEXT})
        except ValidationFailed as invalid:
            audit.reason = "invalid"
            audit.detail["fields"] = ",".join(sorted(invalid.fields))
            raise
        return request

    def _refuse_protected(self, environment_id: int, options: Mapping[str, str]) -> None:
        device = options.get("device")
        if device and self._self_identity().protects_path(environment_id, device):
            current_audit().reason = SELF_PROTECTED_REASON
            raise SelfProtected.import_target()

    def _require_mount_confirmed(
        self, digest: str, options: Mapping[str, str], confirm_mount: str
    ) -> bool:
        needed = mount_confirmation(digest, options)
        if needed is None:
            return False
        audit = current_audit()
        audit.detail["host_mount"] = True
        if not hmac.compare_digest(needed.token.encode(), confirm_mount.encode("utf-8", "replace")):
            audit.reason = "mount_not_confirmed"
            raise ValidationFailed({"confirm_mount": MOUNT_UNCONFIRMED_TEXT})
        return True

    async def _exists(self, client: EngineClient, name: str) -> bool:
        try:
            await client.volumes.inspect(name)
        except NotFound:
            return False
        return True

    async def _require_absent(self, client: EngineClient, name: str) -> None:
        audit = current_audit()
        try:
            exists = await self._exists(client, name)
        except ResponseTooLarge:
            audit.reason = "too_large"
            raise ObjectTooLarge() from None
        except EngineError as error:
            unavailable = unavailable_from(error)
            audit.reason = unavailable.kind
            raise unavailable from None
        if exists:
            audit.reason = "name_in_use"
            raise VolumeNameInUse()

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
            raise VolumeImportRefused(
                "helper_image_missing",
                "The host has no image to create the helper container from. "
                "Pull or load an image on the host, then review the import again.",
            ) from None
        except HelperImageUnusable:
            audit.reason = "helper_image_unusable"
            raise VolumeImportRefused(
                "helper_image_unusable",
                "The image the review named declares a volume where the helper mounts the new "
                "volume. Review the import again.",
            ) from None
        except EngineError as error:
            unavailable = unavailable_from(error)
            audit.reason = unavailable.kind
            raise unavailable from None
        return reviewed

    async def _create(
        self,
        actor: Principal,
        environment_id: int,
        client: EngineClient,
        request: VolumeRequest,
        run: _Run,
    ) -> None:
        try:
            await run_shielded(
                partial(self._create_on_host, client, request, run),
                partial(self._interrupted_create, actor, environment_id, client, request, run),
            )
        except VolumeNameInUse:
            current_audit().reason = "name_in_use"
            raise
        except EngineError as error:
            raise create_failure(
                error,
                event="volume_import_create_refused",
                name_in_use=VolumeNameInUse,
                refused=VolumeRefused,
            ) from None

    async def _create_on_host(
        self, client: EngineClient, request: VolumeRequest, run: _Run
    ) -> Volume:
        labelled = replace(request, labels={**request.labels, IMPORT_LABEL: run.token})
        volume = await client.volumes.create(
            name=labelled.name,
            driver=labelled.driver,
            driver_opts=labelled.options,
            labels=labelled.labels,
        )
        if not created_as_requested(volume, labelled):
            raise VolumeNameInUse
        run.created = True
        return volume

    async def _write(
        self,
        actor: Principal,
        environment_id: int,
        client: EngineClient,
        request: VolumeRequest,
        image: str,
        held: HeldImport,
        run: _Run,
        host_backed: bool,
    ) -> None:
        audit = current_audit()
        stream = RewrittenArchive(held.path)
        helper: VolumeHelper | None = None
        try:
            async with HostFileReader(client).volume_session(
                actor, request.name, image=image, writable=True
            ) as helper:
                audit.detail["helper_container"] = helper.container_id
                await self._require_empty(helper)
                await helper.write(stream)
        except BaseException as error:
            with anyio.CancelScope(shield=True):
                try:
                    await stream.aclose()
                finally:
                    removed = await self._remove_volume(client, request.name)
            audit.detail.update(
                volume_removed=removed, helper_removed=None if helper is None else helper.removed
            )
            if not isinstance(error, Exception):
                await self._write_interrupted(actor, environment_id, request.name, audit.detail)
                raise
            if isinstance(error, VolumeNotEmpty):
                audit.reason = "volume_not_empty"
                run.keep_upload = True
                raise VolumeImportRefused(
                    "volume_not_empty", _not_empty_text(volume_removed=removed)
                ) from None
            reason = _failure_of(error)
            audit.reason = reason
            raise VolumeImportFailed(
                reason, volume_removed=removed, host_backed=host_backed
            ) from None
        audit.detail["helper_removed"] = helper.removed
        audit.detail["bytes_written"] = stream.bytes_out

    @staticmethod
    async def _require_empty(helper: VolumeHelper) -> None:
        stream = await helper.stream()
        held = bytearray()
        try:
            async for chunk in stream:
                held += chunk
                if len(held) > EMPTY_READ_BYTES:
                    raise VolumeNotEmpty
        finally:
            await stream.aclose()
        if not _holds_only_the_root(bytes(held)):
            raise VolumeNotEmpty

    async def _remove_volume(self, client: EngineClient, name: str) -> bool:
        try:
            async with asyncio.timeout(REMOVE_SECONDS):
                await client.volumes.remove(name, force=False)
        except (EngineError, TimeoutError) as error:
            logger.warning(
                "volume_import_cleanup_failed",
                extra={"environment_id": client.environment_id, "error": type(error).__name__},
            )
            return isinstance(error, NotFound)
        return True

    async def _claim(self, actor: Principal, environment_id: int, name: str) -> bool:
        audit = current_audit()
        key = ResourceKey(environment_id, "volume", name)

        def note(taken: tuple[bool, bool]) -> None:
            audit.detail["control_created"] = taken[0]
            audit.detail["stale_control_replaced"] = taken[1]

        try:
            created, _ = await retry_busy(
                partial(
                    in_transaction,
                    self._db,
                    partial(_take, self._controls, key, actor.user_id),
                    None,
                    note,
                )
            )
        except asyncio.CancelledError:
            await self._write_interrupted(
                actor,
                environment_id,
                name,
                {**audit.detail, "control_created": False},
                failed=False,
            )
            raise
        except Exception as error:
            logger.error(
                "volume_control_claim_failed",
                extra={
                    "environment_id": environment_id,
                    "volume_name": name,
                    "error": type(error).__name__,
                },
            )
            audit.detail["control_created"] = False
            audit.detail.setdefault("stale_control_replaced", False)
            return False
        return created

    async def _interrupted_create(
        self,
        actor: Principal,
        environment_id: int,
        client: EngineClient,
        request: VolumeRequest,
        run: _Run,
        task: asyncio.Future[Volume],
    ) -> None:
        audit = current_audit()
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, SETTLE_SECONDS)
            detail: dict[str, DetailValue] = dict(audit.detail)
            if run.created and finished and error is None:
                detail["volume_removed"] = await self._remove_volume(client, request.name)
                await self._write_interrupted(actor, environment_id, request.name, detail)
                return
            failure = (
                "outcome_unknown"
                if not finished
                else "name_in_use"
                if isinstance(error, VolumeNameInUse)
                else failure_reason(error)
                if error is not None
                else "interrupted"
            )
            await self._write_interrupted(
                actor, environment_id, request.name, detail, reason=failure
            )

    async def _write_interrupted(
        self,
        actor: Acting,
        environment_id: int,
        name: str,
        detail: Mapping[str, DetailValue],
        *,
        reason: str = "interrupted",
        failed: bool = True,
    ) -> None:
        body: dict[str, DetailValue] = {**detail, "interrupted": True}
        await write_settled_row(
            self._database,
            self._clock,
            actor,
            action=_ACTION,
            target_type="volume",
            target_id=name,
            environment_id=environment_id,
            detail=body,
            failure=reason if failed else None,
        )

    @staticmethod
    async def _unlink(path: Path) -> None:
        await asyncio.to_thread(path.unlink, missing_ok=True)


def _holds_only_the_root(archive: bytes) -> bool:
    pieces = iter([archive])
    reader = TarReader(ChunkSource(lambda: next(pieces, None)))
    try:
        root = reader.next()
        return (
            root is not None
            and root.info.isdir()
            and root.info.name == _ROOT_ENTRY
            and reader.next() is None
        )
    except StreamMalformed:
        return False


def _failure_of(error: Exception) -> str:
    if isinstance(error, HelperMountRefused):
        return "helper_refused"
    if isinstance(error, VolumeArchiveRefused):
        return "archive_invalid"
    if isinstance(error, ArchiveError):
        return "archive_rejected"
    if isinstance(error, EngineError):
        return failure_reason(error)
    return type(error).__name__
