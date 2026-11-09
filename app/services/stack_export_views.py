"""What the stack export confirmation names."""

from dataclasses import dataclass
from typing import Final

from app.db.repositories.stack import StackKind
from app.services.export_manifest import (
    CONTEXTS_DIRECTORY,
    FILES_DIRECTORY,
    MANIFEST_NAME,
    NOT_EXPORTED,
    STACK_FILE_MEMBER,
    ImageReference,
)
from app.services.host_files import HELPER_NAME_PREFIX
from app.services.host_objects import ObjectUser

PLUGIN_DRIVER_REASON: Final = (
    "Only volumes of the local driver can be exported. "
    "A plugin driver can run code on the host when its volume is mounted."
)
UNREADABLE_VOLUME_REASON: Final = "Docker's description of the volume is too large to read."
OWN_VOLUME_REASON: Final = "The container dockmin runs in has this volume mounted."


@dataclass(frozen=True, slots=True)
class ExportVariable:
    """A variable of the stack; a secret's value is sealed or left out, a plain one is written."""

    name: str
    secret: bool


@dataclass(frozen=True, slots=True)
class ExportFileEntry:
    """A companion file; `member` is its place in the archive, none for a secret."""

    name: str
    secret: bool
    size: int
    member: str | None


@dataclass(frozen=True, slots=True)
class ExportContextEntry:
    """A build context archive and its place in the archive."""

    name: str
    size: int
    member: str


@dataclass(frozen=True, slots=True)
class ExportVolumeOffer:
    """A volume of the stack that may be chosen; `refusal` says why it cannot be."""

    name: str
    driver: str
    refusal: str | None
    running: int

    @property
    def selectable(self) -> bool:
        """Report whether the volume can be chosen."""
        return self.refusal is None


@dataclass(frozen=True, slots=True)
class StackExportConfirmation:
    """Everything the archive will hold and leave out, as the confirmation shows it.

    `expected_digest` is what the page posts back; the export is refused if the stack differs.
    """

    environment_id: int
    environment_name: str
    kind: StackKind
    name: str
    expected_digest: str
    compose_bytes: int
    variables: tuple[ExportVariable, ...]
    files: tuple[ExportFileEntry, ...]
    contexts: tuple[ExportContextEntry, ...]
    images: tuple[ImageReference, ...]
    host_paths: tuple[str, ...]
    host_paths_more: int
    volumes: tuple[ExportVolumeOffer, ...]
    volumes_more: int
    running: tuple[ObjectUser, ...]
    running_total: int
    helper_image: str | None
    helper_name_prefix: str = HELPER_NAME_PREFIX

    @property
    def secret_count(self) -> int:
        """Count the secret variables and secret files."""
        return sum(item.secret for item in self.variables) + sum(item.secret for item in self.files)

    @property
    def secret_variables(self) -> tuple[ExportVariable, ...]:
        """Return the secret variables."""
        return tuple(item for item in self.variables if item.secret)

    @property
    def plain_variables(self) -> tuple[ExportVariable, ...]:
        """Return the variables whose values are written to the archive as they are."""
        return tuple(item for item in self.variables if not item.secret)

    @property
    def members(self) -> tuple[str, ...]:
        """Return the archive members other than volume data, in archive order."""
        return (
            MANIFEST_NAME,
            STACK_FILE_MEMBER,
            *(item.member for item in self.files if item.member is not None),
            *(item.member for item in self.contexts),
        )

    @property
    def can_export_volumes(self) -> bool:
        """Report whether a helper image is available on the host."""
        return self.helper_image is not None

    @property
    def not_exported(self) -> tuple[str, ...]:
        """Return what no export carries."""
        return NOT_EXPORTED


def file_member(name: str) -> str:
    """Return the archive member of a plain companion file."""
    return f"{FILES_DIRECTORY}/{name}"


def context_member(index: int) -> str:
    """Return the archive member of the build context at the position."""
    return f"{CONTEXTS_DIRECTORY}/{index}"
