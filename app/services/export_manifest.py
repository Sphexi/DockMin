"""The manifest of a stack or container export archive, and the layout of its members."""

import json
import re
from collections.abc import Collection
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.auth.errors import ProductError
from app.services.export_sealing import (
    SealedValues,
    SealError,
    is_valid_archive_id,
    open_sealed,
    verify_sealed,
)
from app.services.volume_manifest import FORMAT_VERSION as VOLUME_FORMAT_VERSION
from app.services.volume_manifest import VolumeManifest

FORMAT_VERSION: Final = 1
MANIFEST_NAME: Final = "manifest.json"
STACK_FILE_MEMBER: Final = "stack/compose.yaml"
FILES_DIRECTORY: Final = "stack/files"
CONTEXTS_DIRECTORY: Final = "stack/contexts"
VOLUMES_DIRECTORY: Final = "volumes"

MANIFEST_MAX_BYTES: Final = 16 * 1024 * 1024
MAX_COMPOSE_FILE_BYTES: Final = 131_072
MAX_COMPANION_FILE_BYTES: Final = 65_536
MAX_CONTEXT_BYTES: Final = 16 * 1024 * 1024
MAX_CONTEXTS_BYTES: Final = 64 * 1024 * 1024
MAX_VARIABLES: Final = 1000
MAX_COMPANION_FILES: Final = 20
MAX_CONTEXTS: Final = 32
MAX_VOLUMES: Final = 64
MAX_ENVIRONMENT: Final = 100

NOT_EXPORTED: Final = (
    "resource controls and team grants",
    "automation rules and webhooks",
    "image bindings and credential sets",
    "activity rows",
    "dismissals",
)

type ExportKind = Literal["compose_stack", "swarm_stack", "container"]
type SecretsMode = Literal["none", "sealed", "omitted"]

_SHA256 = r"[0-9a-f]{64}"
_VOLUME_PREFIX = re.compile(rf"{VOLUMES_DIRECTORY}/[0-9]{{1,3}}")
_INDEX_PREFIX = re.compile(r"[0-9]{1,3}")


class ExportRefusal(StrEnum):
    """Why an export archive was refused."""

    NOT_GZIP = "not_gzip"
    MALFORMED = "malformed"
    NOT_AN_EXPORT = "not_an_export"
    MANIFEST_INVALID = "manifest_invalid"
    NEWER_VERSION = "newer_version"
    MEMBER_MISMATCH = "member_mismatch"
    TOO_LARGE = "too_large"


class ExportFormatError(ProductError):
    """An archive is not a readable export; `version` is set when a newer one was refused.

    `nested` marks a version refused in a volume's own manifest rather than the archive's.
    """

    def __init__(
        self, reason: ExportRefusal, version: int | None = None, *, nested: bool = False
    ) -> None:
        super().__init__(_message(reason, version, nested))
        self.reason = reason
        self.version = version
        self.nested = nested


def _message(reason: ExportRefusal, version: int | None, nested: bool) -> str:
    if reason is ExportRefusal.NEWER_VERSION and version is not None:
        if nested:
            return (
                f"A volume's format version in the archive is {version}; "
                f"this build reads up to {VOLUME_FORMAT_VERSION}."
            )
        return (
            f"The archive's format version is {version}; this build reads up to {FORMAT_VERSION}."
        )
    return _MESSAGES[reason]


_MESSAGES: Final = {
    ExportRefusal.NOT_GZIP: "The file is not a gzip archive.",
    ExportRefusal.MALFORMED: "The file is not a readable export archive.",
    ExportRefusal.NOT_AN_EXPORT: "The archive does not start with manifest.json.",
    ExportRefusal.MANIFEST_INVALID: "The archive's manifest.json is not a valid export manifest.",
    ExportRefusal.NEWER_VERSION: "The archive's format version is newer than this build reads.",
    ExportRefusal.MEMBER_MISMATCH: "The archive's members do not match its manifest.",
    ExportRefusal.TOO_LARGE: "The archive holds a member larger than the manifest allows.",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemberRef(_Model):
    """A document member: its path in the archive, byte size and SHA-256."""

    member: str
    size: int = Field(ge=1)
    sha256: str = Field(pattern=_SHA256)


class VariableEntry(_Model):
    """A stack variable; a plain value is inline, a secret's value is sealed or absent."""

    name: str = Field(pattern=r"[A-Za-z_][A-Za-z0-9_]{0,127}")
    secret: bool
    value: str | None = Field(default=None, max_length=8192, repr=False)

    @model_validator(mode="after")
    def _plain_has_a_value_and_a_secret_has_none(self) -> Self:
        if self.secret == (self.value is not None):
            raise ValueError("a plain variable has a value and a secret has none")
        return self


class CompanionFile(_Model):
    """A companion file; a secret's content is sealed or absent and has no member or digest."""

    name: str = Field(pattern=r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    secret: bool
    size: int = Field(ge=1, le=MAX_COMPANION_FILE_BYTES)
    member: str | None = None
    sha256: str | None = Field(default=None, pattern=_SHA256)

    @model_validator(mode="after")
    def _plain_files_have_a_member(self) -> Self:
        if self.secret:
            if self.member is not None or self.sha256 is not None:
                raise ValueError("a secret file has no member or digest")
        elif self.member != f"{FILES_DIRECTORY}/{self.name}" or self.sha256 is None:
            raise ValueError("a plain file has its member and digest")
        return self


class ContextEntry(_Model):
    """A build context archive exactly as stored; `member` is `stack/contexts/<index>`."""

    name: str = Field(min_length=1, max_length=128)
    member: str
    compressed: bool
    member_count: int = Field(ge=1, le=10_000)
    extracted_size: int = Field(ge=1, le=268_435_456)
    size: int = Field(ge=1, le=MAX_CONTEXT_BYTES)
    sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def _member_is_an_index(self) -> Self:
        directory, _, index = self.member.rpartition("/")
        if directory != CONTEXTS_DIRECTORY or _INDEX_PREFIX.fullmatch(index) is None:
            raise ValueError("a context member is stack/contexts/<index>")
        return self


class StackSection(_Model):
    """What a stack export carries of the stack."""

    stack_kind: Literal["compose", "swarm"]
    compose_file: MemberRef
    variables: list[VariableEntry] = Field(max_length=MAX_VARIABLES)
    files: list[CompanionFile] = Field(max_length=MAX_COMPANION_FILES)
    contexts: list[ContextEntry] = Field(max_length=MAX_CONTEXTS)

    @model_validator(mode="after")
    def _names_are_unique_and_contexts_fit(self) -> Self:
        for label, names in (
            ("variable", [item.name for item in self.variables]),
            ("file", [item.name.casefold() for item in self.files]),
            ("context", [item.name.casefold() for item in self.contexts]),
            ("context member", [item.member for item in self.contexts]),
        ):
            if len(set(names)) != len(names):
                raise ValueError(f"{label} names must be unique")
        if self.compose_file.member != STACK_FILE_MEMBER:
            raise ValueError("the stack file member is stack/compose.yaml")
        if self.compose_file.size > MAX_COMPOSE_FILE_BYTES:
            raise ValueError("the stack file exceeds 128 KiB")
        if sum(item.size for item in self.contexts) > MAX_CONTEXTS_BYTES:
            raise ValueError("build contexts exceed 64 MiB in total")
        return self


class EnvironmentEntry(_Model):
    """A container environment variable; the administrator marks which values are secret."""

    name: str = Field(min_length=1, max_length=512)
    secret: bool
    value: str | None = Field(default=None, max_length=65_536, repr=False)

    @model_validator(mode="after")
    def _plain_has_a_value_and_a_secret_has_none(self) -> Self:
        if self.secret == (self.value is not None):
            raise ValueError("a plain variable has a value and a secret has none")
        return self


class IpamPoolEntry(_Model):
    """One address pool of a network."""

    subnet: str = ""
    gateway: str = ""
    ip_range: str = ""


class NetworkEntry(_Model):
    """A user-defined network a container is attached to, with its create options.

    Options named as credentials, and the labels that grant access, are removed and listed.
    """

    name: str = Field(min_length=1, max_length=256)
    driver: str
    internal: bool = False
    attachable: bool = False
    enable_ipv6: bool = False
    ipam_driver: str = ""
    pools: list[IpamPoolEntry] = Field(default_factory=list, max_length=64)
    labels: dict[str, str] = Field(default_factory=dict)
    options: dict[str, str] = Field(default_factory=dict)
    removed_option_keys: list[str] = Field(default_factory=list)


class ContainerSection(_Model):
    """What a container export carries: the create form's state and the networks it uses.

    `form` holds every form field except the environment rows, which are in `environment`.
    """

    form: dict[str, str]
    environment: list[EnvironmentEntry] = Field(max_length=MAX_ENVIRONMENT)
    networks: list[NetworkEntry] = Field(max_length=16)

    @model_validator(mode="after")
    def _form_has_no_environment_rows(self) -> Self:
        if any(re.fullmatch(r"env-[0-9]+-(key|value)", key) for key in self.form):
            raise ValueError("environment rows belong in the environment list")
        return self


class ImageReference(_Model):
    """An image an exported object uses: as written, its tag and the digest present at export."""

    reference: str = Field(min_length=1, max_length=512)
    tag: str | None = None
    digest: str | None = Field(default=None, pattern=rf"sha256:{_SHA256}")
    used_by: list[str] = Field(default_factory=list)


class VolumeSection(_Model):
    """A volume's data and its own manifest; the members are laid out as a volume export."""

    prefix: str
    manifest: VolumeManifest

    @model_validator(mode="after")
    def _prefix_is_an_index(self) -> Self:
        if _VOLUME_PREFIX.fullmatch(self.prefix) is None:
            raise ValueError("a volume prefix is volumes/<index>")
        return self


class NotExportedGroup(_Model):
    """Fields of the source the create form cannot carry, with why they were not exported."""

    reason: str = Field(pattern=r"[a-z_]{1,40}")
    items: list[str]


class ExportManifest(_Model):
    """What an archive records of the object it holds and of every member that follows it."""

    format_version: int = Field(ge=1)
    kind: ExportKind
    archive_id: str
    exported_at: datetime
    instance_version: str
    name: str
    description: str
    secrets: SecretsMode
    sealed: SealedValues | None = None
    stack: StackSection | None = None
    container: ContainerSection | None = None
    images: list[ImageReference] = Field(default_factory=list, max_length=256)
    volumes: list[VolumeSection] = Field(default_factory=list, max_length=MAX_VOLUMES)
    not_exported: list[str] = Field(default_factory=lambda: list(NOT_EXPORTED))
    not_exported_fields: list[NotExportedGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sections_match_the_kind(self) -> Self:
        if not is_valid_archive_id(self.archive_id):
            raise ValueError("archive_id must be 32 lowercase hexadecimal characters")
        if self.kind == "container":
            if self.container is None or self.stack is not None:
                raise ValueError("a container export carries a container section only")
        elif self.stack is None or self.container is not None:
            raise ValueError("a stack export carries a stack section only")
        elif self.stack.stack_kind != self.kind.removesuffix("_stack"):
            raise ValueError("the stack section does not match the kind")
        prefixes = [section.prefix for section in self.volumes]
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("volume prefixes must be unique")
        return self

    @model_validator(mode="after")
    def _sealed_records_cover_the_secrets(self) -> Self:
        names = set(secret_names(self))
        match self.secrets:
            case "none":
                consistent = not names and self.sealed is None
            case "omitted":
                consistent = bool(names) and self.sealed is None
            case "sealed":
                consistent = (
                    bool(names)
                    and self.sealed is not None
                    and self.sealed.mac is not None
                    and {record.name for record in self.sealed.records} == names
                )
        if not consistent:
            raise ValueError("the secrets mode and sealed records do not match the secret entries")
        return self

    @property
    def integrity_protected(self) -> bool:
        """Report whether the manifest carries a MAC; False means nothing in it can be verified."""
        return self.sealed is not None and self.sealed.mac is not None

    def documents(self) -> dict[str, MemberRef | CompanionFile | ContextEntry]:
        """Return the non-volume members the manifest declares, by member path."""
        found: dict[str, MemberRef | CompanionFile | ContextEntry] = {}
        if self.stack is not None:
            found[self.stack.compose_file.member] = self.stack.compose_file
            for item in self.stack.files:
                if item.member is not None:
                    found[item.member] = item
            for context in self.stack.contexts:
                found[context.member] = context
        return found


def sealed_name(kind: Literal["variable", "file", "environment"], key: str | int) -> str:
    """Return the name a secret is sealed under; an environment entry is keyed by its position."""
    return f"{kind}:{key}"


def secret_names(manifest: ExportManifest) -> list[str]:
    """Return the sealed name of every secret entry of the manifest, in a fixed order."""
    names: list[str] = []
    if manifest.stack is not None:
        names.extend(sealed_name("variable", v.name) for v in manifest.stack.variables if v.secret)
        names.extend(sealed_name("file", f.name) for f in manifest.stack.files if f.secret)
    if manifest.container is not None:
        names.extend(
            sealed_name("environment", index)
            for index, entry in enumerate(manifest.container.environment)
            if entry.secret
        )
    return names


def manifest_bytes(manifest: ExportManifest) -> bytes:
    """Encode the manifest as JSON with sorted keys."""
    document = manifest.model_dump(mode="json")
    return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")


def authenticated_bytes(manifest: ExportManifest) -> bytes:
    """Return the bytes the MAC covers: the whole manifest except the MAC itself."""
    exclude = {"sealed": {"mac"}} if manifest.sealed is not None else None
    document = manifest.model_dump(mode="json", exclude=exclude)
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_manifest(manifest: ExportManifest, passphrase: str) -> None:
    """Check the manifest against its MAC; SealError for a modified or unprotected manifest.

    Blocking and memory-hungry: run it with `asyncio.to_thread`.
    """
    if manifest.sealed is None:
        raise SealError
    verify_sealed(manifest.sealed, passphrase, manifest.archive_id, authenticated_bytes(manifest))


def open_manifest_secrets(
    manifest: ExportManifest, passphrase: str, names: Collection[str] | None = None
) -> dict[str, bytes]:
    """Verify the manifest, then open its sealed values, with one key derivation.

    Blocking and memory-hungry: run it with `asyncio.to_thread`. SealError for a wrong
    passphrase, a modified manifest or sealed record, or a manifest with nothing sealed.
    """
    if manifest.sealed is None:
        raise SealError
    return open_sealed(
        manifest.sealed,
        passphrase,
        manifest.archive_id,
        names,
        authenticated=authenticated_bytes(manifest),
    )


def parse_manifest(data: bytes) -> ExportManifest:
    """Parse a manifest, reading every version up to FORMAT_VERSION.

    ExportFormatError: NEWER_VERSION names the version of an archive written by a later release,
    MANIFEST_INVALID for anything else that is not a manifest of a version this build reads.
    """
    if len(data) > MANIFEST_MAX_BYTES:
        raise ExportFormatError(ExportRefusal.TOO_LARGE)
    try:
        document = json.loads(data)
    except (ValueError, RecursionError):
        raise ExportFormatError(ExportRefusal.MANIFEST_INVALID) from None
    version = document.get("format_version") if isinstance(document, dict) else None
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ExportFormatError(ExportRefusal.MANIFEST_INVALID)
    if version > FORMAT_VERSION:
        raise ExportFormatError(ExportRefusal.NEWER_VERSION, version)
    volumes = document.get("volumes")
    for section in volumes if isinstance(volumes, list) else ():
        nested = _nested_version(section)
        if nested > VOLUME_FORMAT_VERSION:
            raise ExportFormatError(ExportRefusal.NEWER_VERSION, nested, nested=True)
    try:
        return ExportManifest.model_validate(document)
    except (ValidationError, RecursionError):
        raise ExportFormatError(ExportRefusal.MANIFEST_INVALID) from None


def _nested_version(section: object) -> int:
    manifest = section.get("manifest") if isinstance(section, dict) else None
    version = manifest.get("format_version") if isinstance(manifest, dict) else None
    return version if isinstance(version, int) and not isinstance(version, bool) else 0
