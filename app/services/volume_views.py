"""Volume errors, view types and operation outcomes."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol

from app.auth.errors import (
    ProductError,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.db.repositories.pagination import Page
from app.db.repositories.stack import StackKind
from app.services.container_actions import (
    ActionOutcome,
)
from app.services.containers import (
    LabelView,
)
from app.services.host_objects import (
    ObjectUser,
    OptionView,
    PruneStatus,
)

__all__ = [
    "CHANGED_SINCE_REVIEW",
    "MAX_FORCE_CONTAINERS",
    "ContainerRemover",
    "ForceContainer",
    "ForceImpact",
    "ForceRefused",
    "ForceResult",
    "ForceStatus",
    "ForceStep",
    "PruneMode",
    "RestrictedVolume",
    "VolumeBatchConfirmation",
    "VolumeBatchEntry",
    "VolumeBatchItem",
    "VolumeBatchResult",
    "VolumeBatchStatus",
    "VolumeBatchTarget",
    "VolumeChangedSinceReview",
    "VolumeCreated",
    "VolumeDetail",
    "VolumeFilters",
    "VolumeGone",
    "VolumeInUse",
    "VolumeKept",
    "VolumeKind",
    "VolumeNameInUse",
    "VolumePage",
    "VolumePruneCandidate",
    "VolumePruneItem",
    "VolumePrunePlan",
    "VolumePruneResult",
    "VolumePruneTarget",
    "VolumeRefused",
    "VolumeRemoveImpact",
    "VolumeRemoved",
    "VolumeRow",
    "containers_digest",
]

MAX_FORCE_CONTAINERS: Final = 25


CHANGED_SINCE_REVIEW: Final = "This volume changed since you reviewed it; nothing was changed."


type VolumeKind = Literal["named", "anonymous"]


type PruneMode = Literal["anonymous", "all"]


type ForceStatus = Literal[
    "removed", "gone", "not_found", "forbidden", "busy", "conflict", "unreachable", "failed"
]


type VolumePruneStatus = PruneStatus | Literal["protected"]


type VolumeBatchStatus = Literal[
    "removed",
    "in_use",
    "changed",
    "busy",
    "gone",
    "forbidden",
    "protected",
    "unreachable",
    "failed",
]


class VolumeGone(ProductError):
    """The volume vanished between authorisation and the Engine call."""

    def __init__(self) -> None:
        super().__init__("The volume no longer exists.")


class VolumeInUse(ProductError):
    """Containers use the volume; the message and count are the product's own."""

    def __init__(self, containers: int) -> None:
        super().__init__("The volume is used by one or more containers.")
        self.containers = containers


class VolumeRefused(ProductError):
    """Docker rejected a volume configuration; the Engine's text is not part of the message.

    engine_detail is that text, scrubbed and cut, for a page shown to an administrator.
    """

    def __init__(self, engine_detail: str | None = None) -> None:
        super().__init__("Docker refused this volume configuration.")
        self.engine_detail = engine_detail


class VolumeChangedSinceReview(ValidationFailed):
    """The volume is no longer the one the caller reviewed; nothing was changed."""

    def __init__(self) -> None:
        super().__init__({"volume": CHANGED_SINCE_REVIEW})


class VolumeNameInUse(ValidationFailed):
    """A volume with the requested name already exists."""

    def __init__(self) -> None:
        super().__init__({"name": "A volume with this name already exists."})


class RestrictedVolume(ValidationFailed):
    """A standard user's request names a driver or driver options only administrators may use."""


@dataclass(frozen=True, slots=True)
class ForceContainer:
    """A container a force removal would remove first."""

    id: str
    short_id: str
    name: str
    state: str


class ForceRefused(ProductError):
    """A force removal the product will not start; the fields say why, and nothing was changed.

    `running` are the listed containers that must be stopped first, `unwritable` counts the
    containers the caller may not change, and `too_many` is set past the per-run limit.
    """

    def __init__(
        self, running: tuple[ForceContainer, ...], unwritable: int, *, too_many: bool = False
    ) -> None:
        reasons: list[str] = []
        if running:
            reasons.append("Containers that are running must be stopped first.")
        if unwritable:
            reasons.append(f"{unwritable} containers you cannot change use this volume.")
        if too_many:
            reasons.append(f"More than {MAX_FORCE_CONTAINERS} containers use this volume.")
        super().__init__(" ".join(reasons) or "This volume cannot be force-removed.")
        self.running = running
        self.unwritable = unwritable
        self.too_many = too_many


@dataclass(frozen=True, slots=True)
class VolumeFilters:
    """The list filters as submitted; validated before any Engine call."""

    q: str | None = None
    driver: str | None = None
    usage: str | None = None
    kind: str | None = None
    stack: str | None = None


@dataclass(frozen=True, slots=True)
class VolumeRow:
    """One volume of the list; `short_name` shortens an anonymous volume's id-like name.

    `containers_more` counts permitted users cut by the display limit and `containers_hidden`
    those the viewer may not see.
    """

    environment_id: int
    name: str
    short_name: str
    driver: str
    scope: str
    kind: VolumeKind
    created_at: datetime | None
    stack: str | None
    in_use: int
    containers: tuple[ObjectUser, ...]
    containers_more: int
    containers_hidden: int
    can_remove: bool
    protected: bool = False
    stack_kind: StackKind | None = None


@dataclass(frozen=True, slots=True)
class VolumePage:
    """A page of rows; `total` matches the filters and `visible` is the caller's count."""

    page: Page[VolumeRow]
    total: int
    visible: int
    limit: int
    environment_name: str
    is_local: bool
    can_create: bool
    can_prune: bool


@dataclass(frozen=True, slots=True)
class VolumeDetail:
    """One volume as the detail page shows it.

    `mountpoint` and `options` are None for a standard user: the first is a host path and the
    second can carry credentials. For an administrator each option value is scrubbed first.
    `containers_more` counts permitted users cut by the display limit and `containers_hidden`
    those the viewer may not see.
    """

    environment_id: int
    environment_name: str
    is_local: bool
    name: str
    short_name: str
    driver: str
    scope: str
    kind: VolumeKind
    created_at: datetime | None
    stack: str | None
    labels: tuple[LabelView, ...]
    labels_truncated: bool
    mountpoint: str | None
    options: tuple[OptionView, ...] | None
    containers: tuple[ObjectUser, ...]
    containers_total: int
    containers_more: int
    containers_hidden: int
    can_remove: bool
    can_manage_access: bool
    protected: bool = False
    stack_kind: StackKind | None = None


@dataclass(frozen=True, slots=True)
class VolumeRemoveImpact:
    """What removing a volume names; `created_at_text` is what the confirmation posts back."""

    environment_id: int
    environment_name: str
    name: str
    short_name: str
    driver: str
    kind: VolumeKind
    stack: str | None
    created_at_text: str
    in_use: int
    containers: tuple[ObjectUser, ...]
    containers_more: int
    containers_hidden: int
    protected: bool = False

    @property
    def needs_force(self) -> bool:
        """Report whether containers use the volume, which leaves the force page as the way on."""
        return self.in_use > 0


@dataclass(frozen=True, slots=True)
class ForceImpact:
    """What removing a volume together with its containers names.

    `containers` are those the caller may change, whatever their state, at most
    MAX_FORCE_CONTAINERS, with `containers_more` counting the rest; `running` is the subset that
    blocks the removal; `unwritable` counts the containers the caller may not change, which are
    not named, and `containers_hidden` those of them the caller may not see at all.
    `containers_digest` binds the confirmation to every container that uses the volume.
    """

    environment_id: int
    environment_name: str
    name: str
    short_name: str
    driver: str
    created_at_text: str
    containers: tuple[ForceContainer, ...]
    running: tuple[ForceContainer, ...]
    containers_more: int
    unwritable: int
    containers_hidden: int
    too_many: bool
    containers_digest: str
    protected: bool = False

    @property
    def blocked(self) -> bool:
        """Report whether the removal would be refused as it stands."""
        return bool(self.running) or self.unwritable > 0 or self.too_many


@dataclass(frozen=True, slots=True)
class VolumePruneCandidate:
    """A volume a prune would remove; `created_at_text` is what the confirmation posts back."""

    name: str
    short_name: str
    driver: str
    kind: VolumeKind
    stack: str | None
    created_at_text: str
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class VolumeKept:
    """A volume the container dockmin runs in has mounted, which no removal takes."""

    name: str
    short_name: str


@dataclass(frozen=True, slots=True)
class VolumePrunePlan:
    """The volumes a prune would remove, at most 100, and how many matched in all.

    `kept` are the volumes the prune mode would otherwise reach that dockmin's own container
    has mounted.
    """

    environment_id: int
    environment_name: str
    mode: PruneMode
    candidates: tuple[VolumePruneCandidate, ...]
    total: int
    kept: tuple[VolumeKept, ...] = ()

    @property
    def truncated(self) -> bool:
        """Report whether more matched than are listed."""
        return self.total > len(self.candidates)


@dataclass(frozen=True, slots=True)
class VolumePruneTarget:
    """One volume the caller reviewed in a prune confirmation."""

    name: str
    created_at: str


@dataclass(frozen=True, slots=True)
class VolumePruneItem:
    """The outcome for one listed volume."""

    name: str
    short_name: str
    status: VolumePruneStatus


@dataclass(frozen=True, slots=True)
class VolumePruneResult:
    """One item per listed volume, in listed order."""

    mode: PruneMode
    items: tuple[VolumePruneItem, ...]

    @property
    def removed(self) -> int:
        """Return how many volumes were removed."""
        return sum(1 for item in self.items if item.status == "removed")


@dataclass(frozen=True, slots=True)
class VolumeBatchTarget:
    """One volume the caller reviewed in a batch removal confirmation."""

    name: str
    created_at: str
    in_use: int


@dataclass(frozen=True, slots=True)
class VolumeBatchEntry:
    """A volume a batch removal confirmation names; `token` is what the run posts back."""

    name: str
    short_name: str
    driver: str
    kind: VolumeKind
    stack: str | None
    created_at_text: str
    in_use: int
    containers: tuple[ObjectUser, ...]
    containers_more: int
    containers_hidden: int

    @property
    def token(self) -> str:
        """Return `name:created:in_use`, the value of the hidden field that binds the run."""
        return f"{self.name}:{self.created_at_text}:{self.in_use}"


@dataclass(frozen=True, slots=True)
class VolumeBatchConfirmation:
    """The volumes of a batch removal the caller may change.

    `kept` are the volumes dockmin's own container has mounted, left out of the removal, and
    `unavailable` counts the rest of the selection, which does not exist or may not be changed.
    """

    environment_id: int
    environment_name: str
    is_local: bool
    targets: tuple[VolumeBatchEntry, ...]
    kept: tuple[VolumeKept, ...]
    unavailable: int


@dataclass(frozen=True, slots=True)
class VolumeBatchItem:
    """The outcome for one volume of a batch removal; the message is the product's own."""

    name: str
    short_name: str
    status: VolumeBatchStatus
    message: str


@dataclass(frozen=True, slots=True)
class VolumeBatchResult:
    """One item per distinct volume, in the order submitted."""

    items: tuple[VolumeBatchItem, ...]

    @property
    def removed(self) -> int:
        """Return how many volumes were removed."""
        return sum(1 for item in self.items if item.status == "removed")

    @property
    def failed(self) -> int:
        """Return how many volumes were not removed."""
        return len(self.items) - self.removed


@dataclass(frozen=True, slots=True)
class VolumeCreated:
    """What a create did.

    `preexisting` is set when a volume of the name appeared in the moment before the create, so
    the result is not a volume this request made and no control was placed over it.
    """

    environment_id: int
    name: str
    driver: str
    control_created: bool
    preexisting: bool


@dataclass(frozen=True, slots=True)
class VolumeRemoved:
    """What a removal did; `control_released` is false when the control could not be dropped."""

    environment_id: int
    name: str
    driver: str
    control_released: bool


@dataclass(frozen=True, slots=True)
class ForceStep:
    """The outcome of removing one container on the way to removing the volume."""

    container_id: str
    short_id: str
    name: str
    status: ForceStatus
    message: str


@dataclass(frozen=True, slots=True)
class ForceResult:
    """What a force removal did: each container step, and the volume unless a step stopped it.

    `failure` is the product's text when the run ended before the volume was removed.
    """

    environment_id: int
    name: str
    steps: tuple[ForceStep, ...]
    volume: VolumeRemoved | None
    failure: str | None

    @property
    def containers_removed(self) -> int:
        """Return how many containers were removed or already gone."""
        return sum(1 for step in self.steps if step.status in ("removed", "gone"))


class ContainerRemover(Protocol):
    """The audited container removal the force path goes through, one container at a time."""

    async def __call__(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        *,
        expected_name: str,
        force: bool = False,
        volumes: bool = False,
        batch: bool = False,
    ) -> ActionOutcome:
        """Remove one container the caller reviewed."""
        ...


def containers_digest(container_ids: Sequence[str]) -> str:
    """Return the SHA-256 over the sorted distinct container ids that binds a force confirmation."""
    return hashlib.sha256("\n".join(sorted(set(container_ids))).encode("utf-8")).hexdigest()
