"""Value types, result records and digests the stack operations exchange."""

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Final, Protocol, runtime_checkable

from app.auth.principal import Principal
from app.compose_cli.buildx import BuildEvent
from app.compose_cli.parsers import ProgressEvent
from app.db.repositories.stack import StackKind, StackRepository, StackStatus, VariableWrite
from app.services.compose_setup import ComposeStatus
from app.services.container_actions import BusyView
from app.services.image_jobs import JobProgress
from app.services.secret_mask import MASK, mask
from app.services.stack_deploy import ServiceSummary, StackRejected
from app.services.stack_dismissal_views import DismissalView, LeftoverObject, LeftoverVolume
from app.services.stack_document import (
    ComposeDocumentRejected,
    FileUse,
    decode_compose,
    stack_name,
)

__all__ = [
    "MAX_CHOICES",
    "BuildSink",
    "BuildSummary",
    "BuiltView",
    "ContainerEntry",
    "ContextView",
    "CreateContext",
    "FileForm",
    "FileRemoveConfirmation",
    "FileView",
    "FilesPanel",
    "ForgetConfirmation",
    "ManagedStackName",
    "NotRunning",
    "PartlyStopped",
    "ProgressSink",
    "RedeployConfirmation",
    "RemoveConfirmation",
    "RemoveVolume",
    "RestrictedOption",
    "ServiceState",
    "ServiceView",
    "StackAdopted",
    "StackCreated",
    "StackDetail",
    "StackEdit",
    "StackOutcome",
    "StackPage",
    "StackRow",
    "UnmanagedProject",
    "ValidationReport",
    "VariableSet",
    "VariableView",
    "VolumePlan",
    "checked_name",
    "containers_digest",
    "networks_digest",
    "read_intake",
    "unwritable_members",
    "variable_views",
    "volumes_digest",
]

MAX_CHOICES: Final = 200


class ProgressSink(Protocol):
    """Where a running operation reports pulls and Compose resource events."""

    def on_event(self, event: ProgressEvent) -> None:
        """Take one Compose progress event."""
        ...

    def pull(self, reference: str) -> JobProgress:
        """Return the collector for one image's pull."""
        ...


@runtime_checkable
class BuildSink(Protocol):
    """A progress sink that also takes the output of image builds."""

    def build(self, service: str, tag: str) -> Callable[[BuildEvent], None]:
        """Return the callback that takes the events of one service's build."""
        ...


@dataclass(frozen=True, slots=True)
class BuildSummary:
    """A service that builds, as a confirmation names it; `reuse` is true when its image stands."""

    service: str
    tag: str
    context: str
    bases: tuple[str, ...]
    reuse: bool


@dataclass(frozen=True, slots=True)
class BuiltView:
    """An image a deployment ran its service from."""

    service: str
    tag: str
    reused: bool


@dataclass(frozen=True, slots=True)
class StackRow:
    """A stack as listed."""

    environment_id: int
    name: str
    origin: str
    status: StackStatus
    last_operation: str | None
    last_outcome: str | None
    last_operation_at: str | None
    last_deployed_at: str | None
    changed_since_deploy: bool


@dataclass(frozen=True, slots=True)
class UnmanagedProject:
    """A Compose project found on the host that dockmin does not manage.

    `networks` and `volumes` count every such object; the leftover tuples list at most 20 of
    each. `objects_digest` covers every volume and network, which is what a dismissal names, and
    `dismissible` is false for a name a dismissal cannot be stored under.
    """

    name: str
    containers: int
    running: int
    services: tuple[str, ...]
    adoptable: bool
    container_names: tuple[str, ...] = ()
    networks: int = 0
    volumes: int = 0
    leftovers_only: bool = False
    leftover_volumes: tuple[LeftoverVolume, ...] = ()
    leftover_networks: tuple[LeftoverObject, ...] = ()
    objects_digest: str = ""
    dismissible: bool = False
    dismissal: DismissalView | None = None


@dataclass(frozen=True, slots=True)
class StackPage:
    """One page of the stacks list.

    `visible_count` is set only when this is the whole list. `unmanaged` is filled for
    administrators; `unmanaged_unavailable` is true when the host could not be read.
    `dismissed_hidden` counts the unmanaged projects a dismissal covers, whether they are listed
    (`show_dismissed`) or left out.
    """

    environment_id: int
    environment_name: str
    items: tuple[StackRow, ...]
    next_cursor: str | None
    unmanaged: tuple[UnmanagedProject, ...]
    unmanaged_unavailable: bool
    can_create: bool
    visible_count: int | None
    compose: ComposeStatus
    dismissed_hidden: int = 0
    show_dismissed: bool = False


@dataclass(frozen=True, slots=True)
class ManagedStackName:
    """The name and kind of a stack the product manages."""

    name: str
    kind: StackKind


@dataclass(frozen=True, slots=True)
class VariableView:
    """A variable as shown; a secret carries a mask and its value is always None.

    `mask` is None for a plain variable. `readable` is false for a secret the server's keys
    cannot open. `can_reveal` is true when the viewer may ask for the secret's value.
    """

    id: int
    name: str
    secret: bool
    value: str | None
    mask: str | None = field(default=None, repr=False)
    readable: bool = True
    can_reveal: bool = False


async def variable_views(
    stacks: StackRepository,
    stack_id: int,
    viewer: Principal,
    *,
    open_masks: bool,
    only: int | None = None,
) -> tuple[VariableView, ...]:
    """Return a stack's variables as the viewer sees them, optionally just the one `only` names.

    A secret is opened, and its partly exposed mask built, only for an administrator when
    `open_masks` is set; every other secret carries the full mask and nothing is opened.
    """
    stored = [v for v in await stacks.variables(stack_id) if only is None or v.id == only]
    administrator = viewer.is_administrator
    revealer = administrator and viewer.session_id is not None
    opened = open_masks and administrator and any(v.is_secret for v in stored)
    masks = (
        await stacks.shape_secrets(stack_id, partial(mask, expose=True), only=only)
        if opened
        else {}
    )
    views: list[VariableView] = []
    for variable in stored:
        if not variable.is_secret:
            views.append(VariableView(variable.id, variable.name, False, variable.value))
            continue
        shown = masks.get(variable.id) if opened else MASK
        readable = shown is not None
        views.append(
            VariableView(
                variable.id,
                variable.name,
                True,
                None,
                shown or MASK,
                readable,
                readable and revealer,
            )
        )
    return tuple(views)


@dataclass(frozen=True, slots=True)
class FileView:
    """A companion file as shown.

    A secret file is present by name only: its size, digest and content are None. `uses` is
    where the stack's documents name the file.
    """

    name: str
    secret: bool
    size: int | None
    digest: str | None
    content: str | None = field(repr=False)
    uses: tuple[FileUse, ...]


@dataclass(frozen=True, slots=True)
class ContextView:
    """A stored context for the Files tab; `uses` are the Compose paths that name it."""

    name: str
    size: int
    digest: str
    compressed: bool
    member_count: int
    extracted_size: int
    updated_at: str
    uses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContainerEntry:
    """A container of a stack, named only when the viewer may see it."""

    id: str
    short_id: str
    name: str
    state: str
    status: str
    image: str
    service: str | None


@dataclass(frozen=True, slots=True)
class ServiceView:
    """A service with the containers the viewer may see; `hidden` counts the rest."""

    name: str
    image: str | None
    declared: bool
    containers: tuple[ContainerEntry, ...]
    hidden: int


@dataclass(frozen=True, slots=True)
class StackDetail:
    """A stack's detail page.

    `containers_available` is false when the host could not be read; the services then list
    only what the file declares.
    """

    row: StackRow
    stack_id: int
    compose_file: str = field(repr=False)
    compose_file_digest: str
    deployed_file_digest: str | None
    inputs_digest: str | None
    deployed_inputs_digest: str | None
    resolved_digest: str | None
    variables: tuple[VariableView, ...]
    services: tuple[ServiceView, ...]
    hidden_containers: int
    containers_available: bool
    can_write: bool
    can_manage: bool
    environment_name: str
    busy: BusyView | None
    compose: ComposeStatus
    files: tuple[FileView, ...] = ()
    contexts: tuple[ContextView, ...] = ()


@dataclass(frozen=True, slots=True)
class FilesPanel:
    """The Files tab: the stack's files, and the digest a write is bound to.

    `inputs_digest` is the empty text when the stack has none.
    """

    row: StackRow
    environment_name: str
    inputs_digest: str
    files: tuple[FileView, ...]
    can_write: bool
    compose: ComposeStatus


@dataclass(frozen=True, slots=True)
class FileForm:
    """What the add and edit pages start from; `file` is None when adding a new file."""

    row: StackRow
    environment_name: str
    inputs_digest: str
    file: FileView | None
    compose: ComposeStatus


@dataclass(frozen=True, slots=True)
class FileRemoveConfirmation:
    """What removing a file names. `in_use` is true while a document references the file."""

    environment_id: int
    environment_name: str
    stack: str
    file: FileView
    expected_inputs_digest: str
    in_use: bool
    compose: ComposeStatus


@dataclass(frozen=True, slots=True)
class StackEdit:
    """What the editor starts from: the stored file and variable rows."""

    row: StackRow
    compose_file: str = field(repr=False)
    compose_file_digest: str
    variables: tuple[VariableView, ...]
    environment_name: str
    compose: ComposeStatus


@dataclass(frozen=True, slots=True)
class CreateContext:
    """What the create and adopt forms need before anything is typed."""

    environment_id: int
    environment_name: str
    compose_ready: bool
    compose_message: str | None
    compose_floor: str
    compose: ComposeStatus


@dataclass(frozen=True, slots=True)
class RestrictedOption:
    """A restricted option an administrator's file uses: its id and the phrase that names it."""

    option: str
    label: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The result of a successful validation, with every secret value already masked."""

    name: str
    services: tuple[ServiceSummary, ...]
    networks: tuple[str, ...]
    volumes: tuple[str, ...]
    images: tuple[str, ...]
    to_pull: tuple[str, ...]
    restricted: tuple[str, ...]
    compose_digest: str
    resolved_digest: str
    restricted_options: tuple[RestrictedOption, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RedeployConfirmation:
    """What a redeploy names before it runs; the digests bind the POST to what was shown."""

    environment_id: int
    environment_name: str
    stack: str
    expected_file_digest: str
    expected_inputs_digest: str
    expected_containers: str
    containers: tuple[ContainerEntry, ...]
    hidden_containers: int
    orphans: tuple[ContainerEntry, ...]
    hidden_orphans: int
    changed_since_deploy: bool
    compose: ComposeStatus
    unwritable_containers: int = 0
    more_containers: int = 0
    more_orphans: int = 0
    conflicts: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()
    builds: tuple[BuildSummary, ...] = ()
    expected_builds: str = ""


@dataclass(frozen=True, slots=True)
class RemoveVolume:
    """A project volume on the remove page; `delete` says whether the remove deletes it."""

    name: str
    driver: str
    delete: bool
    writable: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class RemoveConfirmation:
    """What a remove names before it runs.

    `volumes` is empty unless the volumes page was asked for. `stack_id`, `expected_containers`,
    `expected_networks` and `expected_volumes` are what the POST carries back, and
    `cannot_change` counts volumes the viewer may not delete. `unwritable_containers` counts
    project containers the viewer may not change, which a standard user's remove is refused for.
    `containers` are those of services the file declares; `orphans` carry the project label for
    a service it does not declare, and only a remove that also removes orphans takes them.
    """

    environment_id: int
    environment_name: str
    stack: str
    containers: tuple[ContainerEntry, ...]
    hidden_containers: int
    networks: tuple[str, ...]
    volumes: tuple[RemoveVolume, ...]
    expected_volumes: str
    cannot_change: int
    stack_id: int
    expected_containers: str
    expected_networks: str
    compose: ComposeStatus
    unwritable_containers: int = 0
    more_containers: int = 0
    orphans: tuple[ContainerEntry, ...] = ()
    hidden_orphans: int = 0
    more_orphans: int = 0


@dataclass(frozen=True, slots=True)
class ForgetConfirmation:
    """What forgetting a stack leaves behind."""

    environment_id: int
    environment_name: str
    stack: str
    containers: int
    stack_id: int
    compose: ComposeStatus


@dataclass(frozen=True, slots=True)
class ServiceState:
    """The containers of one service after an operation."""

    service: str
    containers: int
    running: int


@dataclass(frozen=True, slots=True)
class StackOutcome:
    """What an operation did; `message` is product-written."""

    operation: str
    environment_id: int
    name: str
    message: str
    status: StackStatus | None
    events: int
    exit_code: int | None
    pulled: tuple[str, ...]
    services: tuple[ServiceState, ...]
    services_running: int
    volumes_removed: tuple[str, ...] = ()
    volumes_kept: int = 0
    orphans_removed: int = 0
    preexisting_after_deploy: tuple[str, ...] = ()
    interrupted: bool = False
    warnings: tuple[str, ...] = ()
    built: tuple[BuiltView, ...] = ()


@dataclass(frozen=True, slots=True)
class StackCreated:
    """The result of creating a stack."""

    environment_id: int
    name: str
    stack_id: int
    control_created: bool
    stale_control_replaced: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StackAdopted:
    """The result of adopting a project."""

    environment_id: int
    name: str
    stack_id: int
    containers: int
    control_created: bool


@dataclass(frozen=True, slots=True)
class VariableSet:
    """The variables a run uses and what to persist.

    `values` maps every variable name to the value the process receives; `secrets` holds the
    secret values for scrubbing and masking; `writes` is the complete set to store, or None to
    leave the stored set alone.
    """

    values: Mapping[str, str] = field(repr=False)
    secrets: frozenset[str] = field(repr=False)
    writes: tuple[VariableWrite, ...] | None = field(default=None, repr=False)
    secret_names: int = 0


@dataclass(frozen=True, slots=True)
class VolumePlan:
    volumes: tuple[RemoveVolume, ...]
    names: tuple[str, ...]
    digest: str
    cannot_change: int


class NotRunning(StackRejected):
    """A stack with no running container has nothing to restart."""

    def __init__(self) -> None:
        super().__init__({"stack": "No container of this stack is running."}, "not_running")


class PartlyStopped(StackRejected):
    """A redeploy would start the stopped containers of the stack."""

    def __init__(self) -> None:
        super().__init__({"stack": "A container of this stack is not running."}, "partly_stopped")


def unwritable_members() -> StackRejected:
    """Return the refusal for a caller who cannot change every container of a stack."""
    return StackRejected(
        {"stack": "You cannot change every container in this stack."}, "restricted"
    )


def containers_digest(ids: Sequence[str]) -> str:
    """Return the SHA-256 over the sorted container ids a confirmation named."""
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


def volumes_digest(names: Sequence[str]) -> str:
    """Return the SHA-256 over the sorted volume names a confirmation named."""
    return hashlib.sha256("\n".join(sorted(names)).encode("utf-8")).hexdigest()


def networks_digest(ids: Sequence[str]) -> str:
    """Return the SHA-256 over the sorted network ids a confirmation named."""
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


def checked_name(name: str) -> str:
    """Return the trimmed stack name; StackRejected keyed `name` when it breaks the grammar."""
    try:
        return stack_name(name)
    except ComposeDocumentRejected as rejected:
        raise StackRejected(rejected.fields, rejected.reason) from None


def read_intake(*, pasted: str | None, uploaded: bytes | None) -> str:
    """Return the Compose file from exactly one of a pasted text and an uploaded file.

    StackRejected keyed `compose_file` for neither, both, or a file that breaks the size, UTF-8
    or control-character rules.
    """
    has_paste = pasted is not None and pasted.strip() != ""
    has_upload = uploaded is not None and len(uploaded) > 0
    if has_paste == has_upload:
        message = (
            "Use either the text box or a file, not both."
            if has_paste
            else "Paste a Compose file or choose one to upload."
        )
        raise StackRejected({"compose_file": message}, "invalid")
    try:
        raw = (pasted or "").encode("utf-8") if has_paste else (uploaded or b"")
        return decode_compose(raw)
    except UnicodeEncodeError:
        raise StackRejected(
            {"compose_file": "The Compose file must be UTF-8 text."}, "invalid"
        ) from None
    except ComposeDocumentRejected as rejected:
        raise StackRejected(rejected.fields, rejected.reason) from None
