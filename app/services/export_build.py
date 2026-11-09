"""Builds the manifest and the small members of a stack or container export from a description."""

import base64
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.services.export_manifest import (
    CONTEXTS_DIRECTORY,
    FILES_DIRECTORY,
    FORMAT_VERSION,
    MANIFEST_NAME,
    STACK_FILE_MEMBER,
    VOLUMES_DIRECTORY,
    CompanionFile,
    ContainerSection,
    ContextEntry,
    EnvironmentEntry,
    ExportKind,
    ExportManifest,
    ImageReference,
    MemberRef,
    NetworkEntry,
    NotExportedGroup,
    SecretsMode,
    StackSection,
    VariableEntry,
    VolumeSection,
    authenticated_bytes,
    manifest_bytes,
    sealed_name,
)
from app.services.export_sealing import (
    DEFAULT_COST,
    MAC_BYTES,
    SealCost,
    SealedValues,
    new_archive_id,
    seal,
)
from app.services.volume_manifest import VolumeManifest

_NO_MAC = base64.b64encode(bytes(MAC_BYTES)).decode("ascii")


@dataclass(frozen=True, slots=True)
class VariableInput:
    """A stack variable to export; the value is kept out of the repr."""

    name: str
    secret: bool
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class FileInput:
    """A companion file to export; the content is kept out of the repr."""

    name: str
    secret: bool
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ContextInput:
    """A build context archive exactly as stored."""

    name: str
    content: bytes = field(repr=False)
    compressed: bool
    member_count: int
    extracted_size: int


@dataclass(frozen=True, slots=True)
class EnvironmentInput:
    """A container environment variable to export; the value is kept out of the repr."""

    name: str
    secret: bool
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ExportDescription:
    """Everything an export carries, with secret values still in the clear.

    A stack export sets `compose_file` and leaves `form` None; a container export sets `form`.
    `volumes` holds the manifest of each chosen volume, in archive order.
    """

    kind: ExportKind
    name: str
    description: str
    instance_version: str
    exported_at: datetime
    compose_file: str | None = field(default=None, repr=False)
    variables: Sequence[VariableInput] = ()
    files: Sequence[FileInput] = ()
    contexts: Sequence[ContextInput] = ()
    form: Mapping[str, str] | None = field(default=None, repr=False)
    environment: Sequence[EnvironmentInput] = ()
    networks: Sequence[NetworkEntry] = ()
    images: Sequence[ImageReference] = ()
    volumes: Sequence[VolumeManifest] = ()
    not_exported_fields: Sequence[NotExportedGroup] = ()


@dataclass(frozen=True, slots=True)
class ArchiveDocument:
    """A small member held in memory: its path in the archive and its bytes."""

    member: str
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedExport:
    """The manifest and the documents that follow it; volume data is added by the writer."""

    manifest: ExportManifest
    documents: tuple[ArchiveDocument, ...]

    @property
    def manifest_document(self) -> ArchiveDocument:
        """The manifest as the archive's first member."""
        return ArchiveDocument(MANIFEST_NAME, manifest_bytes(self.manifest))


def prepare_export(
    description: ExportDescription,
    passphrase: str | None,
    *,
    cost: SealCost = DEFAULT_COST,
) -> PreparedExport:
    """Build the manifest and documents, sealing secret values when a passphrase is given.

    Secret values are sealed under `passphrase`, and the manifest then carries a MAC over all of
    it; they are left out when it is None. Blocking and memory-hungry when it seals: run it with
    `asyncio.to_thread`. ValidationFailed for a passphrase outside the length bounds; ValueError
    when the description breaks a manifest rule.
    """
    archive_id = new_archive_id()
    secrets: dict[str, bytes] = {}
    documents: list[ArchiveDocument] = []
    stack: StackSection | None = None
    container: ContainerSection | None = None
    if description.kind == "container":
        container = _container_section(description, secrets)
    else:
        stack = _stack_section(description, secrets, documents)
    mode: SecretsMode = "none" if not secrets else "omitted" if passphrase is None else "sealed"
    volumes = [
        VolumeSection(prefix=f"{VOLUMES_DIRECTORY}/{index}", manifest=volume)
        for index, volume in enumerate(description.volumes)
    ]

    def assemble(sealed: SealedValues | None) -> ExportManifest:
        return ExportManifest(
            format_version=FORMAT_VERSION,
            kind=description.kind,
            archive_id=archive_id,
            exported_at=description.exported_at,
            instance_version=description.instance_version,
            name=description.name,
            description=description.description,
            secrets=mode,
            sealed=sealed,
            stack=stack,
            container=container,
            images=list(description.images),
            volumes=volumes,
            not_exported_fields=list(description.not_exported_fields),
        )

    def authenticate(unauthenticated: SealedValues) -> bytes:
        return authenticated_bytes(assemble(unauthenticated.model_copy(update={"mac": _NO_MAC})))

    sealed = None
    if mode == "sealed" and passphrase is not None:
        sealed = seal(secrets, passphrase, archive_id, cost=cost, authenticate=authenticate)
    return PreparedExport(assemble(sealed), tuple(documents))


def _stack_section(
    description: ExportDescription, secrets: dict[str, bytes], documents: list[ArchiveDocument]
) -> StackSection:
    if description.compose_file is None:
        raise ValueError("a stack export needs its file")
    compose = description.compose_file.encode("utf-8")
    documents.append(ArchiveDocument(STACK_FILE_MEMBER, compose))
    variables: list[VariableEntry] = []
    for variable in sorted(description.variables, key=lambda item: item.name):
        if variable.secret:
            secrets[sealed_name("variable", variable.name)] = variable.value.encode("utf-8")
        variables.append(
            VariableEntry(
                name=variable.name,
                secret=variable.secret,
                value=None if variable.secret else variable.value,
            )
        )
    files: list[CompanionFile] = []
    for item in sorted(description.files, key=lambda entry: entry.name):
        if item.secret:
            secrets[sealed_name("file", item.name)] = item.content
            files.append(CompanionFile(name=item.name, secret=True, size=len(item.content)))
            continue
        member = f"{FILES_DIRECTORY}/{item.name}"
        documents.append(ArchiveDocument(member, item.content))
        files.append(
            CompanionFile(
                name=item.name,
                secret=False,
                size=len(item.content),
                member=member,
                sha256=_sha256(item.content),
            )
        )
    contexts: list[ContextEntry] = []
    for index, context in enumerate(description.contexts):
        member = f"{CONTEXTS_DIRECTORY}/{index}"
        documents.append(ArchiveDocument(member, context.content))
        contexts.append(
            ContextEntry(
                name=context.name,
                member=member,
                compressed=context.compressed,
                member_count=context.member_count,
                extracted_size=context.extracted_size,
                size=len(context.content),
                sha256=_sha256(context.content),
            )
        )
    return StackSection(
        stack_kind="compose" if description.kind == "compose_stack" else "swarm",
        compose_file=MemberRef(
            member=STACK_FILE_MEMBER, size=len(compose), sha256=_sha256(compose)
        ),
        variables=variables,
        files=files,
        contexts=contexts,
    )


def _container_section(
    description: ExportDescription, secrets: dict[str, bytes]
) -> ContainerSection:
    if description.form is None:
        raise ValueError("a container export needs its form state")
    environment: list[EnvironmentEntry] = []
    for index, entry in enumerate(description.environment):
        if entry.secret:
            secrets[sealed_name("environment", index)] = entry.value.encode("utf-8")
        environment.append(
            EnvironmentEntry(
                name=entry.name, secret=entry.secret, value=None if entry.secret else entry.value
            )
        )
    return ContainerSection(
        form=dict(description.form), environment=environment, networks=list(description.networks)
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
