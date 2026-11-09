"""Imports a container export archive: its networks and volumes, then the create form's state."""

import asyncio
import secrets
import unicodedata
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Final

import anyio
import yaml

from app.auth.errors import ProductError, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Database, Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.docker_engine.archive import ArchiveError
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, NotFound
from app.docker_engine.models import Network, Volume
from app.logging import get_logger
from app.services.access import ResourceKey, SubmittedLabels
from app.services.activity import (
    ActivityRecorder,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.build_context_archive import run_limited
from app.services.container_import_plan import (
    HOST_PATH_CHOICES,
    VOLUME,
    ContainerChoices,
    ContainerImportPlan,
    EnvironmentPlan,
    HostPathPlan,
    ImageStatus,
    MountEntry,
    NetworkPlan,
    SecretState,
    SkippedVolume,
    VolumePlan,
    mount_entries,
    suggest_name,
)
from app.services.container_import_store import PreparedContainer, PreparedContainerStore
from app.services.container_spec import FormState
from app.services.containers import display_name
from app.services.export_manifest import (
    ContainerSection,
    ExportManifest,
    NetworkEntry,
    open_manifest_secrets,
    sealed_name,
)
from app.services.export_sealing import SealError
from app.services.export_unlock import (
    UnlockAttemptsExhausted,
    UnlockGate,
    shared_unlock_gate,
)
from app.services.guards import require_administrator
from app.services.host_files import (
    HelperImageMissing,
    HelperMountRefused,
    HostFileReader,
    helper_image,
)
from app.services.host_objects import (
    audited_client,
    create_failure,
    failure_reason,
    hold_all,
    in_transaction,
    read_networks,
    read_volumes,
    write_settled_row,
)
from app.services.image_listing import read_containers, read_images
from app.services.imported_volumes import RUN_LABEL, created_by_this_call
from app.services.network_addressing import (
    NetworkRequest,
    overlapping,
    parse_network_form,
)
from app.services.networks import NetworkNameInUse, NetworkRefused, network_engine_body
from app.services.object_inspector import EngineObjectInspector, unavailable_from
from app.services.object_locks import LockHolder, ObjectKey, ObjectLockRegistry
from app.services.resource_controls import ResourceControlService
from app.services.stack_export_host import image_references
from app.services.stack_import import (
    ImportBlocked,
    StackImportChanged,
    StackUploadGone,
    UnlockFailed,
    UnlockLimited,
)
from app.services.stack_import_archive import (
    MAX_UPLOAD_BYTES,
    StackArchiveRefused,
    VolumeFeed,
    VolumeSummary,
    inspect_container_archive,
)
from app.services.stack_import_store import SPOOL_DIRECTORY, HeldStack, StackImportStore
from app.services.volume_import_store import SpoolFailure, spool_upload
from app.services.volume_import_stream import RewrittenArchive
from app.services.volume_views import VolumeNameInUse, VolumeRefused
from app.services.volumes import parse_volume_form

logger = get_logger(__name__)

__all__ = [
    "ContainerImportFailed",
    "ContainerImportReview",
    "ContainerImportService",
    "ContainerPrepared",
]

KIND: Final = "container"
_ACTION: Final = "container_imported"
_NAME_LIMIT: Final = 255
_REMOVE_SECONDS: Final = 60.0
_PLUGIN_TEXT: Final = "Only volumes of the local driver are written."
_SPOOL_TEXT: Final = {
    "too_large": "The archive is larger than the upload limit.",
    "insufficient_space": "There is not enough free space on the data volume for this upload.",
}
_PATH_VOLUME: Final = "hostpath"


class ContainerImportFailed(ProductError):
    """The import failed after objects were created; the ones it made were removed if it could.

    `cause` is the product's own text for the refusal that followed, when there was one.
    """

    def __init__(
        self, reason: str, *, removed: Sequence[str], kept: Sequence[str], cause: str = ""
    ) -> None:
        tail = (
            " Every object this import created was removed."
            if removed and not kept
            else " Nothing was created."
            if not removed and not kept
            else " These could not be removed; remove them from the networks and volumes "
            f"lists: {', '.join(kept)}."
        )
        head = cause or "The archive's networks and volumes could not be created."
        super().__init__(f"{head}{tail}")
        self.reason = reason
        self.removed = tuple(removed)
        self.kept = tuple(kept)


@dataclass(frozen=True, slots=True)
class ContainerImportReview:
    """What the review page shows: the archive, the plan and what the host can do."""

    upload_id: str
    environment_id: int
    environment_name: str
    archive_size: int
    archive_digest: str
    exported_at: datetime
    format_version: int
    instance_version: str
    secrets_mode: str
    opened: bool
    unlock_remaining: int
    integrity_protected: bool
    plan: ContainerImportPlan


@dataclass(frozen=True, slots=True)
class ContainerPrepared:
    """The outcome of an import: the held form state and what was created for it."""

    prepared_id: str
    environment_id: int
    networks: tuple[str, ...]
    volumes: tuple[str, ...]
    bytes_written: int
    environment_variables: int
    secret_values: int


@dataclass(slots=True)
class _Made:
    kind: str
    name: str
    ref: str
    created_at: str = ""


@dataclass(slots=True)
class _Run:
    made: list[_Made] = field(default_factory=list)
    bytes_written: int = 0
    helpers: int = 0
    token: str = field(default_factory=lambda: secrets.token_hex(16))


def _section(manifest: ExportManifest) -> ContainerSection:
    if manifest.container is None:
        raise StackUploadGone()
    return manifest.container


def _volume_name_problem(name: str) -> str | None:
    try:
        parse_volume_form({"name": name, "driver": "local"})
    except ValidationFailed as invalid:
        return "; ".join(invalid.fields.values())
    return None


def _network_form(entry: NetworkEntry) -> dict[str, str]:
    form: dict[str, str] = {"name": entry.name, "driver": entry.driver}
    for key, on in (
        ("internal", entry.internal),
        ("attachable", entry.attachable),
        ("enable_ipv6", entry.enable_ipv6),
    ):
        if on:
            form[key] = "on"
    for pool in entry.pools:
        version = 6 if ":" in pool.subnet else 4
        suffix = f"_v{version}"
        if f"subnet{suffix}" in form:
            raise ValidationFailed({"pools": "More than one address pool per family."})
        form[f"subnet{suffix}"] = pool.subnet
        if pool.gateway:
            form[f"gateway{suffix}"] = pool.gateway
        if pool.ip_range:
            form[f"ip_range{suffix}"] = pool.ip_range
    for index, (key, value) in enumerate(entry.options.items()):
        form[f"option-{index}-key"] = key
        form[f"option-{index}-value"] = value
    for index, (key, value) in enumerate(entry.labels.items()):
        form[f"label-{index}-key"] = key
        form[f"label-{index}-value"] = value
    return form


def _network_request(entry: NetworkEntry, environment_type: str | None) -> NetworkRequest:
    """Validate an archived network as a create form would; ValidationFailed otherwise."""
    if entry.ipam_driver not in ("", "default"):
        raise ValidationFailed({"ipam": "Only the default address management driver is created."})
    request = parse_network_form(_network_form(entry), environment_type=environment_type)
    SubmittedLabels(request.labels)
    return request


def _settings_of(entry: NetworkEntry) -> dict[str, Any]:
    return {
        "flags": tuple(
            label
            for label, on in (
                ("internal", entry.internal),
                ("attachable", entry.attachable),
                ("IPv6", entry.enable_ipv6),
            )
            if on
        ),
        "pools": tuple((pool.subnet, pool.gateway, pool.ip_range) for pool in entry.pools),
        "options": tuple(entry.options.items()),
        "labels": tuple(entry.labels.items()),
    }


def _differences(entry: NetworkEntry, found: Network) -> tuple[str, ...]:
    archived = {pool.subnet for pool in entry.pools if pool.subnet}
    present = {pool.subnet for pool in found.ipam if pool.subnet}
    pairs = (
        ("driver", entry.driver, found.driver),
        ("internal", entry.internal, found.internal),
        ("IPv6", entry.enable_ipv6, found.enable_ipv6),
        ("subnets", archived, present),
    )
    return tuple(name for name, wanted, held in pairs if wanted != held)


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationFailed({"secrets": "A secret value is not UTF-8 text."}) from None


def _failure_of(error: Exception) -> str:
    if isinstance(error, HelperMountRefused):
        return "helper_refused"
    if isinstance(error, StackArchiveRefused):
        return "archive_invalid"
    if isinstance(error, ArchiveError):
        return "archive_rejected"
    if isinstance(error, EngineError):
        return failure_reason(error)
    return type(error).__name__


def _failed(error: Exception, removed: Sequence[str], kept: Sequence[str]) -> ContainerImportFailed:
    """Describe a failure after objects were created, keeping the reason a refusal carried."""
    if isinstance(error, ProductError):
        reason = getattr(error, "reason", None)
        return ContainerImportFailed(
            reason if isinstance(reason, str) else type(error).__name__,
            removed=removed,
            kept=kept,
            cause=str(error),
        )
    return ContainerImportFailed(_failure_of(error), removed=removed, kept=kept)


class ContainerImportService:
    """Receives, reviews and imports container archives; each import is one audited operation."""

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
        store: StackImportStore,
        prepared: PreparedContainerStore,
        data_dir: Path,
        gate: UnlockGate | None = None,
    ) -> None:
        self._db = db
        self._database = database
        self._authoriser = authoriser
        self._hosts = inspector.hosts
        self._locks = locks
        self._controls = controls
        self._activity = activity
        self._clock = clock
        self._store = store
        self._prepared = prepared
        self._data_dir = data_dir
        self._gate = gate or shared_unlock_gate()

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
        upload_id = self._store.new_id()
        try:
            upload = await spool_upload(
                parts,
                self._data_dir,
                upload_id,
                declared_size=declared_size,
                limit=MAX_UPLOAD_BYTES,
                directory_name=SPOOL_DIRECTORY,
            )
        except SpoolFailure as failure:
            raise ValidationFailed({"archive": _SPOOL_TEXT[failure.reason]}) from None
        try:
            summary = await run_limited(inspect_container_archive, upload.path)
        except StackArchiveRefused as refused:
            await self._unlink(upload.path)
            member = f" Entry: {refused.member}." if refused.member else ""
            raise ValidationFailed({"archive": f"{refused.message}{member}"}) from None
        except BaseException:
            await self._unlink(upload.path)
            raise
        held = await self._store.hold(
            upload_id,
            user_id=actor.user_id,
            session_id=session_id,
            environment_id=environment_id,
            kind=KIND,
            upload=upload,
            summary=summary,
        )
        return held.id

    async def review(
        self,
        actor: Principal,
        session_id: int,
        environment_id: int,
        upload_id: str,
        choices: ContainerChoices | None = None,
    ) -> ContainerImportReview:
        """Describe what importing the held archive creates; nothing is changed.

        StackUploadGone when the upload is not held for this session.
        """
        environment = await self._require_environment(actor, environment_id)
        held = await self._held(actor, session_id, environment_id, upload_id)
        manifest = held.summary.manifest
        plan = await self._plan(held, environment_id, choices or ContainerChoices())
        return ContainerImportReview(
            upload_id=held.id,
            environment_id=environment_id,
            environment_name=environment.name,
            archive_size=held.size,
            archive_digest=held.digest,
            exported_at=manifest.exported_at,
            format_version=manifest.format_version,
            instance_version=manifest.instance_version,
            secrets_mode=manifest.secrets,
            opened=held.opened is not None,
            unlock_remaining=self._gate.remaining(session_id),
            integrity_protected=manifest.integrity_protected,
            plan=plan,
        )

    async def unlock(
        self,
        actor: Principal,
        session_id: int,
        environment_id: int,
        upload_id: str,
        passphrase: str,
    ) -> None:
        """Open the sealed values with the passphrase and keep them with the held upload.

        The passphrase is used once, in a worker thread, and kept nowhere. UnlockFailed counts
        against the session's attempts; UnlockLimited once they are used. The manifest's MAC is
        verified first, so a modified archive does not open.
        """
        await self._require_environment(actor, environment_id)
        held = await self._held(actor, session_id, environment_id, upload_id)
        manifest = held.summary.manifest
        if manifest.secrets != "sealed" or manifest.sealed is None:
            raise ValidationFailed({"passphrase": "This archive holds no sealed values."})
        if not passphrase:
            raise ValidationFailed({"passphrase": "Enter the passphrase."})
        try:
            reservation = self._gate.reserve(session_id)
        except UnlockAttemptsExhausted:
            raise UnlockLimited() from None
        async with self._gate.slot():
            try:
                opened = await asyncio.to_thread(open_manifest_secrets, manifest, passphrase)
            except SealError:
                logger.warning(
                    "container_import_unlock_failed", extra={"environment_id": environment_id}
                )
                raise UnlockFailed(reservation.remaining) from None
        reservation.refund()
        held.opened = opened

    async def cancel(
        self, actor: Principal, session_id: int, environment_id: int, upload_id: str
    ) -> None:
        """Drop the held upload and remove its file; nothing else is changed."""
        await self._require_environment(actor, environment_id)
        held = await self._store.get(
            upload_id,
            user_id=actor.user_id,
            session_id=session_id,
            environment_id=environment_id,
        )
        if held is not None and held.kind == KIND and not held.importing:
            await self._store.discard(held.id)

    @audited(
        action=_ACTION,
        target_type="container",
        environment_id=environment_argument,
    )
    async def prepare(
        self,
        actor: Principal,
        environment_id: int,
        upload_id: str,
        choices: ContainerChoices,
        expected_plan: str,
    ) -> ContainerPrepared:
        """Create the networks and volumes the archive needs and hold the create form's state.

        The plan is made again and must be the one `expected_plan` names. Each missing network is
        created from the archive's options, and each archived volume is created and written
        before the form state is produced. A failure removes, in reverse order, what this import
        created and names it on the failure row. The held upload is removed once anything has
        been created. An upload already being imported is refused. ImportBlocked,
        StackImportChanged, StackUploadGone, ValidationFailed, ContainerImportFailed,
        NetworkNameInUse, NetworkRefused, VolumeNameInUse, VolumeRefused, EnvironmentUnavailable,
        ObjectBusy.
        """
        require_administrator(actor)
        await self._require_environment(actor, environment_id)
        held = await self._held_for(actor, environment_id, upload_id)
        try:
            return await self._prepare(actor, environment_id, held, choices, expected_plan)
        finally:
            held.importing = False

    async def _prepare(
        self,
        actor: Principal,
        environment_id: int,
        held: HeldStack,
        choices: ContainerChoices,
        expected_plan: str,
    ) -> ContainerPrepared:
        audit = current_audit()
        manifest = held.summary.manifest
        section = _section(manifest)
        audit.target_id = manifest.name[:_NAME_LIMIT]
        audit.detail.update(
            format_version=manifest.format_version,
            volumes_archived=len(held.summary.volumes),
            environment=len(section.environment),
            secret_values=sum(1 for item in section.environment if item.secret),
        )
        plan = await self._plan(held, environment_id, choices)
        audit.detail.update(
            networks=len(plan.networks),
            volumes=len(plan.volumes),
            volumes_skipped=len(plan.skipped_volumes),
            host_paths_kept=sum(1 for item in plan.host_paths if item.choice == "keep"),
            host_paths_replaced=sum(1 for item in plan.host_paths if item.choice == VOLUME),
            secrets_opened=sum(1 for item in plan.environment if item.state == "opened"),
            secrets_entered=sum(1 for item in plan.environment if item.state == "entered"),
            networks_created=0,
            volumes_created=0,
            bytes_written=0,
            helpers_created=0,
        )
        if plan.digest != expected_plan:
            audit.reason = "changed_since_review"
            raise StackImportChanged()
        self._require_clear(plan)
        values = self._form_values(held, plan, choices)
        client = await audited_client(self._hosts, environment_id)
        image = await self._image_for(client, plan)
        run = _Run()
        holder = LockHolder(UserActor.of(actor), actor.username, "import")
        keys = [ObjectKey(environment_id, "volume", item.name) for item in plan.volumes]
        keys.extend(
            ObjectKey(environment_id, "volume", item.volume)
            for item in plan.host_paths
            if item.choice == VOLUME
        )
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(hold_all(self._locks, keys, holder))
                try:
                    await self._create_networks(client, held, plan, run)
                    await self._create_volumes(actor, client, held, plan, image, run)
                    await self._claim(actor, environment_id, run)
                except BaseException as error:
                    await self._roll_back(actor, environment_id, client, run, error)
                    if run.made and isinstance(error, Exception):
                        failure = _failed(
                            error,
                            _names(audit.detail, "objects_removed"),
                            _names(audit.detail, "objects_kept"),
                        )
                        audit.reason = failure.reason
                        raise failure from None
                    raise
        finally:
            if run.made:
                with anyio.CancelScope(shield=True):
                    await self._store.discard(held.id)
        await self._store.discard(held.id)
        prepared_id = self._prepared.new_id()
        self._prepared.hold(
            PreparedContainer(
                id=prepared_id,
                user_id=actor.user_id,
                session_id=held.session_id,
                environment_id=environment_id,
                source_name=manifest.name,
                exported=manifest.exported_at.isoformat(),
                values=values,
                not_exported=plan.not_exported,
                networks_created=tuple(item.name for item in run.made if item.kind == "network"),
                volumes_created=tuple(item.name for item in run.made if item.kind == "volume"),
                environment_variables=len(section.environment),
                secret_values=sum(1 for item in section.environment if item.secret),
                expires=self._prepared.expiry(),
            )
        )
        return ContainerPrepared(
            prepared_id,
            environment_id,
            tuple(item.name for item in run.made if item.kind == "network"),
            tuple(item.name for item in run.made if item.kind == "volume"),
            run.bytes_written,
            len(section.environment),
            sum(1 for item in section.environment if item.secret),
        )

    async def _require_environment(self, actor: Principal, environment_id: int) -> Any:
        require_administrator(actor)
        await self._authoriser.require_environment(actor, environment_id)
        return await self._hosts.environment(environment_id)

    async def _held(
        self, actor: Principal, session_id: int, environment_id: int, upload_id: str
    ) -> HeldStack:
        held = await self._store.get(
            upload_id,
            user_id=actor.user_id,
            session_id=session_id,
            environment_id=environment_id,
        )
        if held is None or held.kind != KIND:
            raise StackUploadGone()
        return held

    async def _held_for(self, actor: Principal, environment_id: int, upload_id: str) -> HeldStack:
        """Return the held upload and mark it as being imported; refuse one already marked.

        The caller clears the mark, and only the request that set it may.
        """
        audit = current_audit()
        if actor.session_id is None:
            audit.reason = "upload_gone"
            raise StackUploadGone()
        try:
            held = await self._held(actor, actor.session_id, environment_id, upload_id)
        except StackUploadGone:
            audit.reason = "upload_gone"
            raise
        if held.importing:
            audit.reason = "import_running"
            raise ImportBlocked("import_running", "This upload is already being imported.")
        held.importing = True
        return held

    def _require_clear(self, plan: ContainerImportPlan) -> None:
        audit = current_audit()
        if plan.problems:
            audit.reason = "collision"
            audit.detail["problems"] = len(plan.problems)
            raise ImportBlocked("collision", "; ".join(plan.problems[:10]))
        if plan.secrets_needed:
            audit.reason = "secrets_needed"
            raise ImportBlocked(
                "secrets_needed", "The values of some secrets are neither opened nor entered."
            )
        if plan.paths_undecided:
            audit.reason = "paths_undecided"
            raise ImportBlocked(
                "paths_undecided", "Choose what to do with each host path the container binds."
            )

    async def _image_for(self, client: EngineClient, plan: ContainerImportPlan) -> str:
        if not plan.has_data:
            return ""
        audit = current_audit()
        try:
            return await helper_image(client)
        except HelperImageMissing:
            audit.reason = "helper_image_missing"
            raise ImportBlocked(
                "helper_image_missing",
                "The host has no image to create the helper container from. "
                "Pull or load an image on the host, then try again.",
            ) from None
        except EngineError as error:
            unavailable = unavailable_from(error)
            audit.reason = unavailable.kind
            raise unavailable from None

    def _secret_text(
        self, held: HeldStack, choices: ContainerChoices, index: int
    ) -> tuple[SecretState, str | None]:
        found = held.opened or {}
        key = sealed_name("environment", index)
        entered = unicodedata.normalize("NFC", choices.entered.get(key, ""))
        if key in found:
            return "opened", _text(found[key])
        if entered.strip():
            return "entered", entered
        return "needed", None

    def _environment(
        self, held: HeldStack, choices: ContainerChoices
    ) -> tuple[tuple[EnvironmentPlan, ...], dict[int, str]]:
        section = _section(held.summary.manifest)
        sealed = held.summary.manifest.secrets == "sealed"
        rows: list[EnvironmentPlan] = []
        known: dict[int, str] = {}
        for index, entry in enumerate(section.environment):
            if not entry.secret:
                known[index] = entry.value or ""
                rows.append(EnvironmentPlan(index, entry.name, False, "plain", None, False))
                continue
            state, text = self._secret_text(held, choices, index)
            if text is not None:
                known[index] = text
            rows.append(
                EnvironmentPlan(
                    index, entry.name, True, state, sealed_name("environment", index), sealed
                )
            )
        return tuple(rows), known

    def _form_values(
        self, held: HeldStack, plan: ContainerImportPlan, choices: ContainerChoices
    ) -> dict[str, str]:
        manifest = held.summary.manifest
        section = _section(manifest)
        values = dict(section.form)
        _, known = self._environment(held, choices)
        for index, entry in enumerate(section.environment):
            values[f"env-{index}-key"] = entry.name
            values[f"env-{index}-value"] = known.get(index, "")
        if not values.get("name"):
            values["name"] = manifest.name
        renames = {item.source: item.name for item in plan.volumes if item.source != item.name}
        replaced = {item.source: item.volume for item in plan.host_paths if item.choice == VOLUME}
        for mount in mount_entries(section.form):
            if mount.kind == "volume" and mount.source in renames:
                values[f"mount-{mount.index}-source"] = renames[mount.source]
            elif mount.kind == "bind" and mount.source in replaced:
                values[f"mount-{mount.index}-type"] = "volume"
                values[f"mount-{mount.index}-source"] = replaced[mount.source]
        return dict(FormState.from_form(values).values)

    async def _plan(
        self, held: HeldStack, environment_id: int, choices: ContainerChoices
    ) -> ContainerImportPlan:
        manifest = held.summary.manifest
        section = _section(manifest)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        host_networks = await read_networks(client)
        host_volumes = await read_volumes(client)
        taken = {item.name for item in host_volumes}
        networks = self._networks(section, host_networks, environment.type)
        mounts = mount_entries(section.form)
        chosen: set[str] = set()
        volumes, skipped = self._volumes(held, mounts, choices, taken, chosen)
        host_paths = self._host_paths(manifest.name, mounts, choices, taken, chosen)
        environment_rows, _ = self._environment(held, choices)
        image = section.form.get("image", "")
        status, archive_digest, local_digest = await self._image_status(client, manifest, image)
        names = {display_name(item.names, item.id) for item in await read_containers(client)}
        helper: str | None = None
        if any(item.entries > 0 for item in volumes):
            try:
                helper = await helper_image(client)
            except (HelperImageMissing, EngineError):
                helper = None
        return ContainerImportPlan(
            source_name=manifest.name,
            image=image,
            image_status=status,
            archive_digest=archive_digest,
            local_digest=local_digest,
            name_taken=(section.form.get("name") or manifest.name) in names,
            networks=networks,
            volumes=volumes,
            skipped_volumes=skipped,
            host_paths=host_paths,
            environment=environment_rows,
            not_exported=tuple(
                (group.reason, tuple(group.items)) for group in manifest.not_exported_fields
            ),
            helper_image=helper,
        ).with_digest(held.digest)

    def _networks(
        self, section: ContainerSection, host: Sequence[Network], environment_type: str | None
    ) -> tuple[NetworkPlan, ...]:
        present = {item.name: item for item in host}
        others = [(item.name, pool.subnet) for item in host for pool in item.ipam if pool.subnet]
        plans: list[NetworkPlan] = []
        for entry in section.networks:
            found = present.get(entry.name)
            settings = _settings_of(entry)
            if found is not None:
                plans.append(
                    NetworkPlan(
                        entry.name, found.driver, "reuse", _differences(entry, found), **settings
                    )
                )
                continue
            problem: str | None = None
            try:
                request = _network_request(entry, environment_type)
                clash = overlapping(request, others)
                if clash:
                    problem = f"its subnet overlaps the existing network {clash[0]}."
            except ValidationFailed as invalid:
                problem = "; ".join(invalid.fields.values())
            plans.append(NetworkPlan(entry.name, entry.driver, "create", (), problem, **settings))
        return tuple(plans)

    def _volumes(
        self,
        held: HeldStack,
        mounts: Sequence[MountEntry],
        choices: ContainerChoices,
        taken: set[str],
        chosen: set[str],
    ) -> tuple[tuple[VolumePlan, ...], tuple[SkippedVolume, ...]]:
        targets: dict[str, list[str]] = {}
        for mount in mounts:
            if mount.kind == "volume" and mount.source:
                targets.setdefault(mount.source, []).append(mount.target)
        plans: list[VolumePlan] = []
        skipped: list[SkippedVolume] = []
        for archived in held.summary.volumes:
            source = archived.manifest.name
            if archived.manifest.driver != "local":
                skipped.append(SkippedVolume(source, _PLUGIN_TEXT))
                continue
            if source not in targets:
                skipped.append(SkippedVolume(source, "No mount of the container uses this volume."))
                continue
            plans.append(self._volume_plan(archived, targets[source], choices, taken, chosen))
        return tuple(plans), tuple(skipped)

    def _volume_plan(
        self,
        archived: VolumeSummary,
        targets: Sequence[str],
        choices: ContainerChoices,
        taken: set[str],
        chosen: set[str],
    ) -> VolumePlan:
        source = archived.manifest.name
        name = choices.volume_names.get(source, "").strip() or source
        problem = _volume_name_problem(name)
        if problem is None and name in chosen:
            problem = "another volume of this import uses this name."
        chosen.add(name)
        exists = name in taken
        return VolumePlan(
            source=source,
            name=name,
            prefix=archived.prefix,
            entries=archived.summary.members,
            size=archived.summary.bytes,
            targets=tuple(targets),
            taken=exists,
            suggestion=suggest_name(name, taken | chosen) if exists else None,
            problem=problem,
        )

    def _host_paths(
        self,
        source_name: str,
        mounts: Sequence[MountEntry],
        choices: ContainerChoices,
        taken: set[str],
        chosen: set[str],
    ) -> tuple[HostPathPlan, ...]:
        uses: dict[str, list[str]] = {}
        for mount in mounts:
            if mount.kind == "bind" and mount.source:
                uses.setdefault(mount.source, []).append(mount.target)
        base = source_name if _volume_name_problem(source_name) is None else "container"
        plans: list[HostPathPlan] = []
        for position, (path, where) in enumerate(uses.items(), start=1):
            choice = choices.host_paths.get(path)
            if choice not in HOST_PATH_CHOICES:
                choice = None
            default = f"{base}-{_PATH_VOLUME}-{position}"
            name = choices.path_volumes.get(path, "").strip() or default
            problem: str | None = None
            exists = False
            if choice == VOLUME:
                problem = _volume_name_problem(name)
                if problem is None and name in chosen:
                    problem = "another volume of this import uses this name."
                chosen.add(name)
                exists = name in taken
            plans.append(
                HostPathPlan(
                    path,
                    tuple(where),
                    choice,
                    name,
                    exists,
                    suggest_name(name, taken | chosen) if exists else None,
                    problem,
                )
            )
        return tuple(plans)

    async def _image_status(
        self, client: EngineClient, manifest: ExportManifest, image: str
    ) -> tuple[ImageStatus, str | None, str | None]:
        archived = next((item for item in manifest.images if item.reference == image), None)
        archive_digest = archived.digest if archived is not None else None
        if not image:
            return "not_recorded", None, None
        try:
            local = await read_images(client)
        except ProductError:
            local = []
        text = yaml.safe_dump({"services": {"container": {"image": image}}})
        found = image_references(text, local)
        local_digest = found[0].digest if found else None
        status: ImageStatus
        if archive_digest is None:
            status = "not_recorded"
        elif local_digest is None:
            status = "not_present"
        elif local_digest == archive_digest:
            status = "same"
        else:
            status = "differs"
        return status, archive_digest, local_digest

    async def _create_networks(
        self, client: EngineClient, held: HeldStack, plan: ContainerImportPlan, run: _Run
    ) -> None:
        section = _section(held.summary.manifest)
        environment = await self._hosts.environment(client.environment_id)
        entries = {entry.name: entry for entry in section.networks}
        for item in plan.networks_to_create:
            request = _network_request(entries[item.name], environment.type)
            body = network_engine_body(request)
            task: asyncio.Future[str] = asyncio.ensure_future(client.networks.create(body))
            try:
                network_id = await asyncio.shield(task)
            except asyncio.CancelledError:
                await self._settle_creation(task, "network", item.name, run)
                raise
            except EngineError as error:
                raise create_failure(
                    error,
                    event="container_import_network_refused",
                    name_in_use=NetworkNameInUse,
                    refused=NetworkRefused,
                ) from None
            run.made.append(_Made("network", item.name, network_id))
        current_audit().detail["networks_created"] = len(run.made)

    async def _create_volumes(
        self,
        actor: Principal,
        client: EngineClient,
        held: HeldStack,
        plan: ContainerImportPlan,
        image: str,
        run: _Run,
    ) -> None:
        audit = current_audit()
        for path in plan.host_paths:
            if path.choice == VOLUME:
                await self._require_absent(client, path.volume)
        feed = VolumeFeed(held.path)
        try:
            for item in plan.volumes:
                await self._require_absent(client, item.name)
                await self._create_volume(client, item.name, run)
                if item.entries > 0:
                    stream = RewrittenArchive(chunks=feed.tar(item.prefix))
                    try:
                        async with HostFileReader(client).volume_session(
                            actor, item.name, image=image, writable=True
                        ) as helper:
                            run.helpers += 1
                            audit.detail["helper_container"] = helper.container_id
                            await helper.write(stream)
                    finally:
                        with anyio.CancelScope(shield=True):
                            await stream.aclose()
                    run.bytes_written += stream.bytes_out
        finally:
            feed.close()
        volumes = sum(1 for made in run.made if made.kind == "volume")
        audit.detail.update(
            volumes_created=volumes,
            bytes_written=run.bytes_written,
            helpers_created=run.helpers,
        )

    async def _require_absent(self, client: EngineClient, name: str) -> None:
        try:
            await client.volumes.inspect(name)
        except NotFound:
            return
        except EngineError as error:
            raise unavailable_from(error) from None
        current_audit().reason = "name_in_use"
        raise VolumeNameInUse()

    async def _create_volume(self, client: EngineClient, name: str, run: _Run) -> Volume:
        labels = {RUN_LABEL: run.token}
        task: asyncio.Future[Volume] = asyncio.ensure_future(
            client.volumes.create(name=name, driver="local", driver_opts={}, labels=labels)
        )
        try:
            volume = await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._settle_creation(task, "volume", name, run)
            raise
        except EngineError as error:
            raise create_failure(
                error,
                event="container_import_volume_refused",
                name_in_use=VolumeNameInUse,
                refused=VolumeRefused,
            ) from None
        if not volume.created_at:
            with suppress(EngineError):
                volume = await client.volumes.inspect(name)
        if not created_by_this_call(volume, labels):
            current_audit().reason = "name_in_use"
            raise VolumeNameInUse()
        run.made.append(_Made("volume", name, name, volume.created_at))
        return volume

    async def _settle_creation(
        self,
        task: asyncio.Future[Any],
        kind: str,
        name: str,
        run: _Run,
    ) -> None:
        with anyio.CancelScope(shield=True):
            done, _ = await asyncio.wait({task}, timeout=_REMOVE_SECONDS)
            if task in done and not task.cancelled() and task.exception() is None:
                result = task.result()
                if kind == "network":
                    run.made.append(_Made("network", name, str(result)))
                elif created_by_this_call(result, {RUN_LABEL: run.token}):
                    run.made.append(_Made("volume", name, name, getattr(result, "created_at", "")))

    async def _claim(self, actor: Principal, environment_id: int, run: _Run) -> None:
        audit = current_audit()

        async def take() -> int:
            claimed = 0
            for made in run.made:
                if made.kind == "network":
                    key = ResourceKey(environment_id, "network", made.ref)
                else:
                    key = ResourceKey(environment_id, "volume", made.name)
                    await self._controls.release(key)
                claimed += int(await self._controls.claim(key, actor.user_id))
            return claimed

        def note(claimed: int) -> None:
            audit.detail["controls_created"] = claimed

        try:
            await retry_busy(partial(in_transaction, self._db, take, None, note))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "container_import_control_claim_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            audit.detail["controls_created"] = 0

    async def _roll_back(
        self,
        actor: Principal,
        environment_id: int,
        client: EngineClient,
        run: _Run,
        error: BaseException,
    ) -> None:
        audit = current_audit()
        removed: list[str] = []
        kept: list[str] = []
        with anyio.CancelScope(shield=True):
            for made in reversed(run.made):
                label = f"{made.kind} {made.name}"
                (removed if await self._remove(client, made) else kept).append(label)
            if run.made:
                audit.detail["objects_removed"] = ",".join(removed)
            if kept:
                audit.detail["objects_kept"] = ",".join(kept)
            if not isinstance(error, Exception):
                await self._write_interrupted(actor, environment_id, audit.target_id, audit.detail)

    async def _remove(self, client: EngineClient, made: _Made) -> bool:
        try:
            async with asyncio.timeout(_REMOVE_SECONDS):
                if made.kind == "network":
                    await client.networks.remove(made.ref)
                else:
                    await client.volumes.remove(made.name, force=False)
        except NotFound:
            return True
        except (EngineError, TimeoutError) as failure:
            logger.warning(
                "container_import_cleanup_failed",
                extra={"environment_id": client.environment_id, "error": type(failure).__name__},
            )
            return False
        return True

    async def _write_interrupted(
        self,
        actor: Principal,
        environment_id: int,
        name: str | None,
        detail: Mapping[str, DetailValue],
    ) -> None:
        await write_settled_row(
            self._database,
            self._clock,
            actor,
            action=_ACTION,
            target_type="container",
            target_id=name,
            environment_id=environment_id,
            detail={**detail, "interrupted": True},
            failure="interrupted",
        )

    @staticmethod
    async def _unlink(path: Path) -> None:
        await asyncio.to_thread(path.unlink, missing_ok=True)


def _names(detail: Mapping[str, DetailValue], key: str) -> tuple[str, ...]:
    text = detail.get(key)
    return tuple(part for part in str(text).split(",") if part) if text else ()
