"""Imports a stack export archive as a managed stack on an environment; administrators only."""

import asyncio
import unicodedata
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

import anyio

from app.auth.errors import ProductError, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Database
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.db.repositories.stack_build_context import ContextWrite
from app.docker_engine.archive import ArchiveError
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, NotFound
from app.docker_engine.models import Volume
from app.logging import get_logger
from app.services.access import COMPOSE_PROJECT_LABEL, SWARM_NAMESPACE_LABEL, canonical_stack_key
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
from app.services.containers import display_name
from app.services.export_manifest import ContextEntry, sealed_name
from app.services.export_sealing import SealError, open_sealed
from app.services.guards import require_administrator
from app.services.host_files import (
    HelperImageMissing,
    HelperMountRefused,
    HostFileReader,
    helper_image,
)
from app.services.host_objects import (
    audited_client,
    failure_reason,
    hold_all,
    write_settled_row,
)
from app.services.image_bindings import parse_pull_reference
from app.services.image_listing import read_containers, read_images
from app.services.imported_volumes import ImportedVolumes
from app.services.object_inspector import EngineObjectInspector, unavailable_from
from app.services.object_locks import LockHolder, ObjectKey, ObjectLockRegistry
from app.services.stack_collisions import REASON_NAME, REASON_PORT
from app.services.stack_common import declared_service_images
from app.services.stack_deploy import PreexistingObjects, StackNameInUse, StackRejected
from app.services.stack_document import CompanionFile, VariableInput
from app.services.stack_export_host import image_references
from app.services.stack_import_archive import (
    MAX_UPLOAD_BYTES,
    StackArchiveRefused,
    VolumeSummary,
    inspect_stack_archive,
    read_contexts,
    stack_volume_tar,
)
from app.services.stack_import_plan import (
    HOST_PATH_CHOICES,
    Collision,
    HostPath,
    ImageEntry,
    ImportChoices,
    ImportPlan,
    PlanObject,
    SecretEntry,
    SkippedVolume,
    VolumePlan,
    host_path_uses,
    match_volumes,
    replace_host_paths,
    volume_key_for_path,
)
from app.services.stack_import_store import (
    SPOOL_DIRECTORY,
    UNSEAL_FAILURE_LIMIT,
    HeldStack,
    StackImportStore,
)
from app.services.stack_queries import StackQueryService
from app.services.stack_views import checked_name
from app.services.stacks import StackService
from app.services.swarm_gate import require_swarm
from app.services.swarm_stack_actions import SwarmStackActions
from app.services.swarm_stack_plan import SwarmStackPlanner
from app.services.volume_import_store import SpoolFailure, spool_upload
from app.services.volume_import_stream import RewrittenArchive
from app.services.volume_views import VolumeNameInUse

logger = get_logger(__name__)

__all__ = [
    "ImportBlocked",
    "ImportReview",
    "StackImportChanged",
    "StackImportFailed",
    "StackImportService",
    "StackImported",
    "StackUploadGone",
    "UnlockFailed",
    "UnlockLimited",
]

type Kind = Literal["compose", "swarm"]

_ACTION: Final = "stack_imported"
_NAME_LIMIT: Final = 255
_PLACEHOLDER: Final = "placeholder"
_REMOVE_SECONDS: Final = 60.0
_COMPOSE_VOLUME_LABEL: Final = "com.docker.compose.volume"
_KIND_TEXT: Final = {"compose": "a Compose stack", "swarm": "a Swarm stack"}
_ARCHIVE_KIND: Final = {"compose_stack": "compose", "swarm_stack": "swarm"}
_SPOOL_TEXT: Final = {
    "too_large": "The archive is larger than the upload limit.",
    "insufficient_space": "There is not enough free space on the data volume for this upload.",
}
_COLLISION_TEXT: Final = {
    "stack": "A stack, Compose project or Swarm namespace with this name already exists.",
    "volume": "A volume with this name already exists and does not belong to this stack.",
    "network": "A network with this name already exists and does not belong to this stack.",
    "container": "A container with this name already exists.",
    "service": "A service with this name already exists and does not belong to this stack.",
    "config": "A config with this name already exists and does not belong to this stack.",
    "secret": "A secret with this name already exists and does not belong to this stack.",
}


class StackUploadGone(ProductError):
    """The upload is not held for this session: it expired, was cancelled or was replaced."""

    def __init__(self) -> None:
        super().__init__(
            "This upload is no longer held. Upload the archive again; nothing was changed."
        )


class StackImportChanged(ValidationFailed):
    """What the import would create is not what the review showed; nothing was changed."""

    reason = "changed_since_review"

    def __init__(self) -> None:
        super().__init__({"plan": "The plan changed since you reviewed it; nothing was changed."})


class ImportBlocked(ProductError):
    """The import cannot start: names that collide, secrets without values or paths undecided."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class UnlockFailed(ProductError):
    """The passphrase did not open the sealed values; `remaining` attempts are left."""

    def __init__(self, remaining: int) -> None:
        super().__init__(
            "The passphrase did not open the archive. "
            f"{remaining} {'attempt' if remaining == 1 else 'attempts'} left in this session."
        )
        self.remaining = remaining


class UnlockLimited(ProductError):
    """The session has used its attempts to open sealed values."""

    def __init__(self) -> None:
        super().__init__(
            "Too many wrong passphrases in this session. Sign in again to try more, or enter the "
            "secret values by hand."
        )


class StackImportFailed(ProductError):
    """The volumes could not be written; the ones this import created were removed if it could."""

    def __init__(self, reason: str, *, removed: Sequence[str], kept: Sequence[str]) -> None:
        tail = (
            " Every volume this import created was removed."
            if removed and not kept
            else " No volume was created."
            if not removed and not kept
            else f" These volumes could not be removed; remove them from the volumes list: "
            f"{', '.join(kept)}."
        )
        super().__init__(f"The archive's data could not be written to the host.{tail}")
        self.reason = reason
        self.removed = tuple(removed)
        self.kept = tuple(kept)


@dataclass(frozen=True, slots=True)
class ImportReview:
    """What the review page shows: the archive, the plan and what the host can do."""

    upload_id: str
    environment_id: int
    environment_name: str
    kind: Kind
    archive_size: int
    archive_digest: str
    exported_at: datetime
    format_version: int
    instance_version: str
    secrets_mode: str
    opened: bool
    unlock_remaining: int
    plan: ImportPlan
    helper_image: str | None
    pull_references: tuple[str, ...]
    typed_name: str = ""
    name_error: str | None = None

    @property
    def can_import(self) -> bool:
        """Report whether the plan can be confirmed and the host can take the volume data."""
        return self.plan.can_import and (self.helper_image is not None or not self.plan.has_data)


@dataclass(frozen=True, slots=True)
class StackImported:
    """The outcome of an import; the deployment is a separate operation that follows."""

    environment_id: int
    name: str
    kind: Kind
    stack_id: int
    volumes: tuple[str, ...]
    bytes_written: int
    variables: int
    files: int
    contexts: int
    warnings: tuple[str, ...]
    control_created: bool


@dataclass(slots=True)
class _Made:
    name: str
    labels: Mapping[str, str]
    created_at: str = ""
    confirmed: bool = False


@dataclass(slots=True)
class _Run:
    made: list[_Made] = field(default_factory=list)
    bytes_written: int = 0
    helpers: int = 0


def _target_of(arguments: Arguments) -> str | None:
    choices: ImportChoices = arguments["choices"]
    text = choices.name.strip()[:_NAME_LIMIT]
    return text or None


class StackImportService:
    """Receives, reviews and imports stack archives; every import is one audited operation."""

    def __init__(
        self,
        *,
        database: Database,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        queries: StackQueryService,
        planner: SwarmStackPlanner,
        compose: StackService,
        swarm: SwarmStackActions,
        locks: ObjectLockRegistry,
        activity: ActivityRecorder,
        clock: Clock,
        store: StackImportStore,
        imported: ImportedVolumes,
        data_dir: Path,
    ) -> None:
        self._database = database
        self._authoriser = authoriser
        self._hosts = inspector.hosts
        self._queries = queries
        self._planner = planner
        self._compose = compose
        self._swarm = swarm
        self._locks = locks
        self._activity = activity
        self._clock = clock
        self._store = store
        self._imported = imported
        self._data_dir = data_dir
        self._unseal_slots = asyncio.Semaphore(2)

    async def receive(
        self,
        actor: Principal,
        session_id: int,
        environment_id: int,
        kind: Kind,
        parts: AsyncIterator[bytes],
        *,
        declared_size: int | None = None,
    ) -> str:
        """Spool and fully validate an upload, hold it, and return its upload id.

        Nothing is created on any host and no row is written. The file is removed on every way
        out but success. ResourceNotFound unless the caller is an administrator who can see an
        environment of the right type; ValidationFailed on the `archive` field for a refused
        archive.
        """
        await self._require_environment(actor, environment_id, kind)
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
            summary = await run_limited(inspect_stack_archive, upload.path)
            archive_kind = _ARCHIVE_KIND.get(summary.manifest.kind)
            if archive_kind is None:
                raise StackArchiveRefused("The archive holds a container, not a stack.")
            if archive_kind != kind:
                raise StackArchiveRefused(
                    f"The archive holds {_KIND_TEXT[archive_kind]}. Import it from the "
                    f"{'Swarm stacks' if archive_kind == 'swarm' else 'Compose stacks'} list."
                )
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
            kind=kind,
            upload=upload,
            summary=summary,
        )
        return held.id

    async def review(
        self,
        actor: Principal,
        session_id: int,
        environment_id: int,
        kind: Kind,
        upload_id: str,
        choices: ImportChoices | None = None,
    ) -> ImportReview:
        """Describe what importing the held archive creates; nothing is changed.

        StackUploadGone when the upload is not held for this session; StackRejected when the
        stack file is refused as a create would refuse it.
        """
        environment = await self._require_environment(actor, environment_id, kind)
        held = await self._held(actor, session_id, environment_id, kind, upload_id)
        manifest = held.summary.manifest
        chosen = choices or ImportChoices(name=manifest.name)
        typed_name = chosen.name
        name_error: str | None = None
        try:
            checked_name(chosen.name)
        except StackRejected as bad:
            name_error = "; ".join(bad.fields.values())
            chosen = replace(chosen, name=manifest.name)
        plan = await self._plan(actor, held, environment_id, kind, chosen)
        client = await self._hosts.client(environment_id)
        image: str | None = None
        if plan.has_data:
            try:
                image = await helper_image(client)
            except HelperImageMissing:
                image = None
            except EngineError:
                image = None
        return ImportReview(
            upload_id=held.id,
            environment_id=environment_id,
            environment_name=environment.name,
            kind=kind,
            archive_size=held.size,
            archive_digest=held.digest,
            exported_at=manifest.exported_at,
            format_version=manifest.format_version,
            instance_version=manifest.instance_version,
            secrets_mode=manifest.secrets,
            opened=held.opened is not None,
            unlock_remaining=max(0, UNSEAL_FAILURE_LIMIT - self._store.failures(session_id)),
            plan=plan,
            helper_image=image,
            pull_references=tuple(
                item.reference for item in plan.images if item.pulled and _pullable(item.reference)
            ),
            typed_name=typed_name,
            name_error=name_error,
        )

    async def unlock(
        self,
        actor: Principal,
        session_id: int,
        environment_id: int,
        kind: Kind,
        upload_id: str,
        passphrase: str,
    ) -> None:
        """Open the sealed values with the passphrase and keep them with the held upload.

        The passphrase is used once, in a worker thread, and kept nowhere. UnlockFailed counts
        against the session's attempts; UnlockLimited once they are used.
        """
        await self._require_environment(actor, environment_id, kind)
        held = await self._held(actor, session_id, environment_id, kind, upload_id)
        manifest = held.summary.manifest
        if manifest.secrets != "sealed" or manifest.sealed is None:
            raise ValidationFailed({"passphrase": "This archive holds no sealed values."})
        if not passphrase:
            raise ValidationFailed({"passphrase": "Enter the passphrase."})
        if self._store.failures(session_id) >= UNSEAL_FAILURE_LIMIT:
            raise UnlockLimited()
        async with self._unseal_slots:
            try:
                opened = await asyncio.to_thread(
                    open_sealed, manifest.sealed, passphrase, manifest.archive_id
                )
            except SealError:
                logger.warning(
                    "stack_import_unlock_failed", extra={"environment_id": environment_id}
                )
                raise UnlockFailed(self._store.note_failure(session_id)) from None
        held.opened = opened

    async def cancel(
        self, actor: Principal, session_id: int, environment_id: int, kind: Kind, upload_id: str
    ) -> None:
        """Drop the held upload and remove its file; nothing else is changed."""
        await self._require_environment(actor, environment_id, kind)
        held = await self._store.get(
            upload_id,
            user_id=actor.user_id,
            session_id=session_id,
            environment_id=environment_id,
        )
        if held is not None and held.kind == kind and not held.importing:
            await self._store.discard(held.id)

    @audited(
        action=_ACTION,
        target_type="stack",
        target_id=_target_of,
        environment_id=environment_argument,
    )
    async def import_stack(
        self,
        actor: Principal,
        environment_id: int,
        kind: Kind,
        upload_id: str,
        choices: ImportChoices,
        expected_plan: str,
    ) -> StackImported:
        """Create the stack, its volumes with their data and its record; the deploy follows.

        The plan is made again and must be the one `expected_plan` names. The stack passes the
        checks of a create. Each volume the archive fills is created, labelled as the stack's own
        and written before the stack row is, so a failure leaves nothing but what this import
        removed: those volumes are named on the failure row. The held upload is removed once any
        volume has been created. ImportBlocked, StackImportChanged, StackUploadGone, StackRejected,
        StackNameInUse, StackImportFailed, VolumeNameInUse, EnvironmentUnavailable, ObjectBusy.
        """
        audit = current_audit()
        require_administrator(actor)
        await self._require_environment(actor, environment_id, kind)
        held = await self._held_for(actor, environment_id, kind, upload_id)
        manifest = held.summary.manifest
        audit.detail.update(
            stack_kind=kind,
            format_version=manifest.format_version,
            volumes_archived=len(held.summary.volumes),
        )
        name = checked_name(choices.name)
        audit.target_id = name
        plan = await self._plan(actor, held, environment_id, kind, choices)
        audit.detail.update(
            renamed=name != manifest.name,
            host_paths_kept=sum(1 for item in plan.host_paths if item.choice == "keep"),
            host_paths_replaced=sum(1 for item in plan.host_paths if item.choice == "volume"),
            volumes_filled=sum(1 for item in plan.volumes if item.filled),
            volumes_skipped=len(plan.skipped_volumes),
            images=len(plan.images),
            secrets=sum(1 for item in plan.secrets),
        )
        if plan.digest != expected_plan:
            audit.reason = "changed_since_review"
            raise StackImportChanged()
        self._require_clear(plan)
        self._require_choices(plan, choices)
        audit.detail.update(volumes_created=0, bytes_written=0, helpers_created=0)
        client = await audited_client(self._hosts, environment_id)
        image = await self._image_for(client, plan)
        contexts = await self._contexts(held)
        variables, files, _ = self._inputs(held, choices)
        run = _Run()
        holder = LockHolder(UserActor.of(actor), actor.username, "import")
        held.importing = True
        created: Any = None
        try:
            async with AsyncExitStack() as volume_locks:

                async def write_volumes() -> None:
                    keys = [
                        ObjectKey(environment_id, "volume", item.name)
                        for item in plan.volumes
                        if item.filled
                    ]
                    await volume_locks.enter_async_context(hold_all(self._locks, keys, holder))
                    await self._write_volumes(actor, environment_id, client, plan, held, image, run)

                try:
                    before_store = write_volumes if plan.has_data else None
                    if kind == "compose":
                        created = await self._compose.create_in_scope(
                            actor,
                            environment_id,
                            name=plan.name,
                            compose_text=plan.compose_text,
                            variables=variables,
                            choices=choices.credentials,
                            resolve_pulls=True,
                            check_collisions=True,
                            files=files,
                            contexts=contexts,
                            before_store=before_store,
                        )
                    else:
                        created = await self._swarm.create_in_scope(
                            actor,
                            environment_id,
                            name=plan.name,
                            compose_text=plan.compose_text,
                            variables=variables,
                            choices=choices.credentials,
                            files=files,
                            contexts=contexts,
                            before_store=before_store,
                        )
                except BaseException as error:
                    removed, kept = await self._roll_back(
                        actor, environment_id, client, name, run, error
                    )
                    if (
                        run.made
                        and isinstance(error, Exception)
                        and not isinstance(error, ProductError)
                    ):
                        audit.reason = _failure_of(error)
                        raise StackImportFailed(audit.reason, removed=removed, kept=kept) from None
                    raise
        finally:
            held.importing = False
            if run.made or created is not None:
                with anyio.CancelScope(shield=True):
                    await self._store.discard(held.id)
        return StackImported(
            environment_id,
            created.name,
            kind,
            created.stack_id,
            tuple(item.name for item in run.made),
            run.bytes_written,
            len(variables),
            len(files),
            len(contexts),
            created.warnings,
            created.control_created,
        )

    async def _require_environment(self, actor: Principal, environment_id: int, kind: Kind) -> Any:
        require_administrator(actor)
        if kind == "swarm":
            return await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        await self._authoriser.require_environment(actor, environment_id)
        return await self._hosts.environment(environment_id)

    async def _held(
        self, actor: Principal, session_id: int, environment_id: int, kind: Kind, upload_id: str
    ) -> HeldStack:
        held = await self._store.get(
            upload_id,
            user_id=actor.user_id,
            session_id=session_id,
            environment_id=environment_id,
        )
        if held is None or held.kind != kind:
            raise StackUploadGone()
        return held

    async def _held_for(
        self, actor: Principal, environment_id: int, kind: Kind, upload_id: str
    ) -> HeldStack:
        audit = current_audit()
        if actor.session_id is None:
            audit.reason = "upload_gone"
            raise StackUploadGone()
        try:
            return await self._held(actor, actor.session_id, environment_id, kind, upload_id)
        except StackUploadGone:
            audit.reason = "upload_gone"
            raise

    def _require_clear(self, plan: ImportPlan) -> None:
        audit = current_audit()
        if plan.collisions:
            audit.reason = "collision"
            audit.detail["collisions"] = len(plan.collisions)
            raise ImportBlocked(
                "collision",
                "Names this import would create are already in use: "
                + "; ".join(f"{item.kind} {item.name}" for item in plan.collisions[:10]),
            )
        if plan.secrets_needed:
            audit.reason = "secrets_needed"
            raise ImportBlocked(
                "secrets_needed", "The values of some secrets are neither opened nor entered."
            )
        if plan.paths_undecided:
            audit.reason = "paths_undecided"
            raise ImportBlocked(
                "paths_undecided", "Choose what to do with each host path the stack binds."
            )

    def _require_choices(self, plan: ImportPlan, choices: ImportChoices) -> None:
        missing: dict[str, str] = {}
        for item in plan.images:
            if not item.pulled or item.reference in choices.credentials:
                continue
            if _pullable(item.reference):
                missing[item.reference] = "Choose how to pull this image."
        if missing:
            current_audit().reason = "credential_choice_required"
            raise StackRejected(missing, "credential_choice_required")

    async def _image_for(self, client: EngineClient, plan: ImportPlan) -> str:
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

    async def _contexts(self, held: HeldStack) -> dict[str, ContextWrite]:
        stack = held.summary.manifest.stack
        if stack is None or not stack.contexts:
            return {}
        try:
            data = await run_limited(read_contexts, held.path, held.summary.manifest)
        except StackArchiveRefused:
            current_audit().reason = "archive_invalid"
            raise StackUploadGone() from None
        by_name = {context.name: context for context in stack.contexts}
        return {name: _write_of(by_name[name], content) for name, content in data.items()}

    def _secret_values(
        self, held: HeldStack, choices: ImportChoices
    ) -> tuple[dict[str, bytes], tuple[SecretEntry, ...]]:
        stack = held.summary.manifest.stack
        sealed = held.summary.manifest.secrets == "sealed"
        values: dict[str, bytes] = {}
        entries: list[SecretEntry] = []
        found = held.opened or {}
        if stack is None:
            return values, ()
        items: list[tuple[Literal["variable", "file"], str]] = [
            ("variable", item.name) for item in stack.variables if item.secret
        ] + [("file", item.name) for item in stack.files if item.secret]
        for kind, name in items:
            key = sealed_name(kind, name)
            entered = unicodedata.normalize("NFC", choices.entered.get(key, ""))
            if key in found:
                values[key] = found[key]
                state: Literal["opened", "entered", "needed"] = "opened"
            elif entered.strip():
                values[key] = entered.encode("utf-8")
                state = "entered"
            else:
                state = "needed"
            entries.append(SecretEntry(key, kind, name, state, sealed))
        return values, tuple(entries)

    def _inputs(
        self, held: HeldStack, choices: ImportChoices
    ) -> tuple[list[VariableInput], list[CompanionFile], dict[str, bytes]]:
        stack = held.summary.manifest.stack
        values, _ = self._secret_values(held, choices)
        variables: list[VariableInput] = []
        files: list[CompanionFile] = []
        if stack is None:
            return variables, files, values
        for row, item in enumerate(stack.variables):
            if item.secret:
                raw = values.get(sealed_name("variable", item.name))
                text = _PLACEHOLDER if raw is None else _text(raw)
                variables.append(VariableInput(item.name, text, True, False, row))
            else:
                variables.append(VariableInput(item.name, item.value or "", False, False, row))
        for entry in stack.files:
            if entry.secret:
                raw = values.get(sealed_name("file", entry.name))
                files.append(
                    CompanionFile(entry.name, _PLACEHOLDER if raw is None else _text(raw), True)
                )
            else:
                files.append(CompanionFile(entry.name, held.summary.files[entry.name], False))
        return variables, files, values

    async def _plan(
        self,
        actor: Principal,
        held: HeldStack,
        environment_id: int,
        kind: Kind,
        choices: ImportChoices,
    ) -> ImportPlan:
        manifest = held.summary.manifest
        stack = manifest.stack
        if stack is None:
            raise StackUploadGone()
        name = choices.name.strip() or manifest.name
        variables, files, _ = self._inputs(held, choices)
        _, secrets = self._secret_values(held, choices)
        uses = host_path_uses(held.summary.compose_text, frozenset(item.name for item in files))
        host_paths, replacements = _host_paths(uses, choices.host_paths)
        try:
            text = replace_host_paths(held.summary.compose_text, replacements)
        except ValueError:
            raise StackRejected(
                {"compose_file": "The stack file cannot be rewritten to replace host paths."},
                "invalid",
            ) from None
        stubs = {context.name: _write_of(context, b"") for context in stack.contexts}
        collisions: list[Collision] = []
        try:
            if kind == "compose":
                found = await self._resolve_compose(
                    actor, environment_id, name, text, variables, files, stubs
                )
            else:
                found = await self._resolve_swarm(
                    actor, environment_id, name, text, variables, files
                )
        except StackNameInUse:
            found = _Resolved.unchecked(name, text)
            collisions.append(Collision("stack", name, _COLLISION_TEXT["stack"]))
        except PreexistingObjects as clash:
            found = _Resolved.unchecked(name, text)
            collisions.extend(_collisions_of_objects(clash.objects))
        except StackRejected as rejected:
            if rejected.reason not in (REASON_NAME, REASON_PORT):
                raise
            found = _Resolved.unchecked(name, text)
            collisions.extend(
                Collision("container" if rejected.reason == REASON_NAME else "port", path, message)
                for path, message in rejected.fields.items()
            )
        name = found.name
        volumes, skipped = _volumes_of(found, held, host_paths, kind)
        client = await self._hosts.client(environment_id)
        if found.checked:
            collisions.extend(await self._held_container_names(client, found.objects))
        images = await self._images(client, manifest, found.references, found.pulls, text)
        return ImportPlan(
            name=name,
            kind=kind,
            source_name=manifest.name,
            compose_text=text,
            objects=(*found.objects, *(PlanObject("volume", item.name) for item in volumes)),
            volumes=volumes,
            skipped_volumes=skipped,
            host_paths=host_paths,
            images=tuple(images),
            collisions=tuple(collisions),
            secrets=secrets,
            variables=tuple((item.name, item.secret) for item in stack.variables),
            files=tuple((item.name, item.size, item.secret) for item in stack.files),
            contexts=tuple((item.name, item.size) for item in stack.contexts),
            warnings=found.warnings,
            checked=found.checked,
        ).with_digest(held.digest)

    async def _resolve_compose(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        text: str,
        variables: Sequence[VariableInput],
        files: Sequence[CompanionFile],
        contexts: Mapping[str, ContextWrite],
    ) -> "_Resolved":
        checked, validated = await self._queries.resolve_candidate(
            actor,
            environment_id,
            name=name,
            compose_text=text,
            variables=variables,
            deploy=False,
            check_collisions=True,
            files=files,
            contexts=contexts or None,
        )
        declarations = {
            key: decl for key, decl in validated.project.volumes.items() if not decl.get("external")
        }
        return _Resolved(
            name=checked,
            checked=True,
            declared=tuple((key, str(decl.get("name", key))) for key, decl in declarations.items()),
            declarations=declarations,
            objects=(
                *_compose_containers(validated.project.services, checked),
                *(PlanObject("network", item) for item in validated.networks),
            ),
            references=tuple(validated.images),
            pulls=frozenset(validated.to_pull),
            warnings=tuple(validated.warnings),
        )

    async def _resolve_swarm(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        text: str,
        variables: Sequence[VariableInput],
        files: Sequence[CompanionFile],
    ) -> "_Resolved":
        plan = await self._planner.candidate(
            actor,
            environment_id,
            name=name,
            compose_text=text,
            variables=variables,
            check_collisions=True,
            files=files,
        )
        translation = plan.translation
        sources = sorted({use.source for use in translation.volumes if not use.external})
        return _Resolved(
            name=plan.stack_name,
            checked=True,
            declared=tuple((source, source) for source in sources),
            declarations={},
            objects=(
                *(
                    PlanObject("service", str(item.spec.get("Name", item.key)))
                    for item in translation.services
                ),
                *(
                    PlanObject("network", item.name)
                    for item in translation.networks
                    if not item.external
                ),
                *(
                    PlanObject(item.kind, item.key, "named when it is deployed")
                    for item in translation.files
                    if not item.external
                ),
            ),
            references=tuple(translation.images),
            pulls=frozenset(translation.images),
            warnings=tuple(plan.warnings),
        )

    async def _held_container_names(
        self, client: EngineClient, objects: Sequence[PlanObject]
    ) -> list[Collision]:
        wanted = {item.name for item in objects if item.kind == "container"}
        if not wanted:
            return []
        held = {display_name(item.names, item.id) for item in await read_containers(client)}
        return [
            Collision("container", name, _COLLISION_TEXT["container"])
            for name in sorted(wanted & held)
        ]

    async def _images(
        self,
        client: EngineClient,
        manifest: Any,
        references: Sequence[str],
        pulls: frozenset[str],
        text: str,
    ) -> list[ImageEntry]:
        try:
            local = await read_images(client)
        except ProductError:
            local = []
        here = {item.reference: item for item in image_references(text, local)}
        archived = {item.reference: item for item in manifest.images}
        entries: list[ImageEntry] = []
        for reference in references:
            held_here = here.get(reference)
            recorded = archived.get(reference)
            local_digest = held_here.digest if held_here is not None else None
            archive_digest = recorded.digest if recorded is not None else None
            if archive_digest is None:
                status: Any = "not_recorded"
            elif local_digest is None:
                status = "not_present"
            elif local_digest == archive_digest:
                status = "same"
            else:
                status = "differs"
            entries.append(
                ImageEntry(
                    reference,
                    held_here.tag if held_here is not None else None,
                    archive_digest,
                    local_digest,
                    status,
                    tuple(held_here.used_by) if held_here is not None else (),
                    reference in pulls,
                )
            )
        return entries

    async def _write_volumes(
        self,
        actor: Principal,
        environment_id: int,
        client: EngineClient,
        plan: ImportPlan,
        held: HeldStack,
        image: str,
        run: _Run,
    ) -> None:
        audit = current_audit()
        for item in plan.volumes:
            if not item.filled or item.prefix is None:
                continue
            await self._require_absent(client, item.name)
            made = _Made(item.name, _labels_of(plan, item))
            run.made.append(made)
            volume = await self._create_volume(client, item, made)
            made.created_at = volume.created_at
            made.confirmed = True
            stream = RewrittenArchive(chunks=stack_volume_tar(held.path, item.prefix))
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
        audit.detail.update(
            volumes_created=len(run.made),
            bytes_written=run.bytes_written,
            helpers_created=run.helpers,
        )
        registered = {item.name: item.created_at for item in run.made}
        if registered and all(registered.values()):
            self._imported.register(environment_id, canonical_stack_key(plan.name), registered)

    async def _require_absent(self, client: EngineClient, name: str) -> None:
        try:
            await client.volumes.inspect(name)
        except NotFound:
            return
        except EngineError as error:
            raise unavailable_from(error) from None
        current_audit().reason = "name_in_use"
        raise VolumeNameInUse()

    async def _create_volume(self, client: EngineClient, item: VolumePlan, made: _Made) -> Volume:
        task: asyncio.Future[Volume] = asyncio.ensure_future(
            client.volumes.create(
                name=item.name,
                driver=item.driver,
                driver_opts=dict(item.options),
                labels=dict(made.labels),
            )
        )
        try:
            volume = await asyncio.shield(task)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await asyncio.wait({task}, timeout=_REMOVE_SECONDS)
            raise
        if not volume.created_at:
            try:
                volume = await client.volumes.inspect(item.name)
            except EngineError:
                return volume
        return volume

    async def _roll_back(
        self,
        actor: Principal,
        environment_id: int,
        client: EngineClient,
        name: str,
        run: _Run,
        error: BaseException,
    ) -> tuple[list[str], list[str]]:
        audit = current_audit()
        removed: list[str] = []
        kept: list[str] = []
        with anyio.CancelScope(shield=True):
            self._imported.release(environment_id, canonical_stack_key(name))
            for made in reversed(run.made):
                (removed if await self._remove_volume(client, made) else kept).append(made.name)
            if run.made:
                audit.detail["volumes_removed"] = ",".join(removed)
            if kept:
                audit.detail["volumes_kept"] = ",".join(kept)
            if not isinstance(error, Exception):
                await self._write_interrupted(actor, environment_id, name, audit.detail)
        return removed, kept

    async def _remove_volume(self, client: EngineClient, made: _Made) -> bool:
        try:
            async with asyncio.timeout(_REMOVE_SECONDS):
                if not made.confirmed:
                    try:
                        found = await client.volumes.inspect(made.name)
                    except NotFound:
                        return True
                    if dict(found.labels) != dict(made.labels):
                        return False
                await client.volumes.remove(made.name, force=False)
        except NotFound:
            return True
        except (EngineError, TimeoutError) as failure:
            logger.warning(
                "stack_import_cleanup_failed",
                extra={"environment_id": client.environment_id, "error": type(failure).__name__},
            )
            return False
        return True

    async def _write_interrupted(
        self,
        actor: Principal,
        environment_id: int,
        name: str,
        detail: Mapping[str, DetailValue],
    ) -> None:
        await write_settled_row(
            self._database,
            self._clock,
            actor,
            action=_ACTION,
            target_type="stack",
            target_id=name,
            environment_id=environment_id,
            detail={**detail, "interrupted": True},
            failure="interrupted",
        )

    @staticmethod
    async def _unlink(path: Path) -> None:
        await asyncio.to_thread(path.unlink, missing_ok=True)


def _pullable(reference: str) -> bool:
    try:
        parse_pull_reference(reference)
    except ValidationFailed:
        return False
    return True


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationFailed({"secrets": "A secret value is not UTF-8 text."}) from None


def _write_of(context: ContextEntry, content: bytes) -> ContextWrite:
    return ContextWrite(
        content=content,
        content_digest=context.sha256,
        compressed=context.compressed,
        member_count=context.member_count,
        extracted_size=context.extracted_size,
    )


def _host_paths(
    uses: Mapping[str, tuple[str, ...]], chosen: Mapping[str, str]
) -> tuple[tuple[HostPath, ...], dict[str, str]]:
    paths: list[HostPath] = []
    replacements: dict[str, str] = {}
    index = 0
    for source, users in uses.items():
        choice = chosen.get(source)
        if choice not in HOST_PATH_CHOICES:
            choice = None
        volume = None
        if choice == "volume":
            index += 1
            volume = volume_key_for_path(index)
            replacements[source] = volume
        paths.append(HostPath(source, users, choice, volume))
    return tuple(paths), replacements


def _compose_containers(
    services: Mapping[str, Mapping[str, Any]], project: str
) -> list[PlanObject]:
    found: list[PlanObject] = []
    for key, service in services.items():
        explicit = service.get("container_name")
        if isinstance(explicit, str) and explicit:
            found.append(PlanObject("container", explicit))
            continue
        scale = _scale_of(service)
        found.extend(PlanObject("container", f"{project}-{key}-{n}") for n in range(1, scale + 1))
    return found


def _scale_of(service: Mapping[str, Any]) -> int:
    raw = service.get("scale")
    if raw is None:
        raw = (
            (service.get("deploy") or {}).get("replicas")
            if isinstance(service.get("deploy"), dict)
            else None
        )
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 20 else 1


def _declared_images(text: str) -> list[str]:
    return list(dict.fromkeys(ref for ref in declared_service_images(text).values() if ref))


def _collisions_of_objects(objects: Sequence[str]) -> list[Collision]:
    found: list[Collision] = []
    for entry in objects:
        word, _, rest = entry.partition(" ")
        kind = word.casefold()
        found.append(Collision(kind, rest, _COLLISION_TEXT.get(kind, entry)))
    return found


def _is_local(declaration: Mapping[str, Any]) -> bool:
    driver = declaration.get("driver")
    return driver in (None, "", "local")


def _labels_of(plan: ImportPlan, item: VolumePlan) -> dict[str, str]:
    if plan.kind == "swarm":
        return {SWARM_NAMESPACE_LABEL: plan.name, **item.labels}
    return {**item.labels, COMPOSE_PROJECT_LABEL: plan.name, _COMPOSE_VOLUME_LABEL: item.key}


def _volume_plan(
    key: str, target: str, archived: VolumeSummary, declaration: Mapping[str, Any]
) -> VolumePlan:
    options = declaration.get("driver_opts")
    labels = declaration.get("labels")
    return VolumePlan(
        key=key,
        name=target,
        source=archived.manifest.name,
        prefix=archived.prefix,
        entries=archived.summary.members,
        size=archived.summary.bytes,
        origin="archive",
        driver="local",
        options={str(k): str(v) for k, v in options.items()} if isinstance(options, dict) else {},
        labels={str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {},
    )


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


@dataclass(frozen=True, slots=True)
class _Resolved:
    """What validating the file settled: names, objects, images and the volumes it declares."""

    name: str
    checked: bool
    declared: tuple[tuple[str, str], ...]
    declarations: Mapping[str, Mapping[str, Any]]
    objects: tuple[PlanObject, ...]
    references: tuple[str, ...]
    pulls: frozenset[str]
    warnings: tuple[str, ...]

    @classmethod
    def unchecked(cls, name: str, text: str) -> "_Resolved":
        """Return what is known without a validation: the images the file names outright."""
        return cls(name, False, (), {}, (), tuple(_declared_images(text)), frozenset(), ())


def _volumes_of(
    found: _Resolved, held: HeldStack, host_paths: Sequence[HostPath], kind: Kind
) -> tuple[tuple[VolumePlan, ...], tuple[SkippedVolume, ...]]:
    manifest = held.summary.manifest
    replaced = {f"{found.name}_{item.volume}" for item in host_paths if item.choice == "volume"}
    matched, skipped = match_volumes(
        found.declared, held.summary.volumes, found.name, manifest.name
    )
    plans: list[VolumePlan] = []
    left_out = list(skipped)
    for key, target in found.declared:
        archived = matched.get(key)
        declaration = found.declarations.get(key, {})
        if target in replaced:
            plans.append(VolumePlan(key, target, None, None, 0, 0, "host_path"))
        elif archived is not None and _is_local(declaration):
            plans.append(_volume_plan(key, target, archived, declaration))
        else:
            plans.append(VolumePlan(key, target, None, None, 0, 0, "declared"))
            if archived is not None:
                left_out.append(
                    SkippedVolume(
                        archived.manifest.name,
                        "The stack file gives the volume a driver other than local, "
                        "so its data is not written.",
                    )
                )
    return tuple(plans), tuple(left_out)
