"""Image errors, view types and operation outcomes."""

import hashlib
from collections.abc import (
    Iterable,
)
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol

from app.auth.errors import ProductError, ValidationFailed
from app.db.repositories.pagination import (
    Page,
)
from app.docker_engine.models import (
    PullProgress,
    PushProgress,
)
from app.registry_client.references import (
    ImageReference,
)
from app.services.containers import (
    EnvVar,
    LabelView,
)
from app.services.image_bindings import (
    BindingView,
    ReapableList,
    RecordOutcome,
)
from app.services.object_inspector import (
    ObjectTooLarge,
)
from app.services.pull_credentials import (
    CredentialChoice,
    PullSource,
)

__all__ = [
    "CHANGED_SINCE_REVIEW",
    "NO_REPLACED_IMAGE",
    "BatchStatus",
    "ContainerUse",
    "HistoryRow",
    "ImageBatchConfirmation",
    "ImageBatchItem",
    "ImageBatchResult",
    "ImageBatchTarget",
    "ImageBatchTargetView",
    "ImageChangedMidway",
    "ImageChangedSinceReview",
    "ImageDetail",
    "ImageFilters",
    "ImageGone",
    "ImageHasDependents",
    "ImageInUse",
    "ImagePage",
    "ImagePullFailed",
    "ImagePushFailed",
    "ImageReferenceView",
    "ImageRow",
    "ImageTooLarge",
    "ProgressSink",
    "PruneCandidate",
    "PruneItem",
    "PruneMode",
    "PrunePlan",
    "PruneResult",
    "PruneStatus",
    "PruneTarget",
    "PullOutcome",
    "PullPlan",
    "PushOutcome",
    "PushPlan",
    "RemoveImpact",
    "RemoveOutcome",
    "ReplacedImage",
    "TagImpact",
    "TagOutcome",
    "UntagImpact",
    "references_digest",
]

NO_REPLACED_IMAGE: Final = "none"


CHANGED_SINCE_REVIEW: Final = "This image changed since you reviewed it; nothing was changed."


CHANGED_DURING_ACTION: Final = (
    "This image changed while the action ran, and part of it may already be done. "
    "Review the image and try again."
)


type ImageKind = Literal["tag", "digest"]


type PruneMode = Literal["dangling", "unused"]


type PruneStatus = Literal["removed", "skipped_changed", "skipped_in_use", "busy", "gone", "failed"]


type BatchStatus = Literal["removed", "in_use", "changed", "busy", "gone", "unreachable", "failed"]


class ImageGone(ProductError):
    """The image vanished between authorisation and the Engine call."""

    def __init__(self) -> None:
        super().__init__("The image no longer exists.")


class ImageInUse(ProductError):
    """Containers use the image; the message and count are the product's own, never the Engine's."""

    def __init__(self, containers: int) -> None:
        super().__init__("The image is used by one or more containers.")
        self.containers = containers


class ImageHasDependents(ProductError):
    """Other images are built on this one."""

    def __init__(self) -> None:
        super().__init__("Other images are built on this one. Remove them first.")


class ImageChangedSinceReview(ValidationFailed):
    """The image is no longer the one the caller reviewed; nothing was changed."""

    def __init__(self) -> None:
        super().__init__({"image": CHANGED_SINCE_REVIEW})


class ImageChangedMidway(ImageChangedSinceReview):
    """The image changed while an action was under way; some of it may already have happened."""

    def __init__(self) -> None:
        ValidationFailed.__init__(self, {"image": CHANGED_DURING_ACTION})


class ImagePullFailed(ProductError):
    """A pull did not complete; `reason` is not_found_or_denied or pull_failed."""

    def __init__(self, reason: Literal["not_found_or_denied", "pull_failed"]) -> None:
        super().__init__(
            "The registry has no such image, or refused access to it."
            if reason == "not_found_or_denied"
            else "The image could not be pulled."
        )
        self.reason = reason


class ImagePushFailed(ProductError):
    """A push did not complete; `reason` is not_found_or_denied or push_failed."""

    def __init__(self, reason: Literal["not_found_or_denied", "push_failed"]) -> None:
        super().__init__(
            "The registry refused the push, or the repository does not exist."
            if reason == "not_found_or_denied"
            else "The image could not be pushed."
        )
        self.reason = reason


class ImageTooLarge(ObjectTooLarge):
    """An image's inspect or history reply is over the read limit; nothing partial is shown."""

    def __init__(self) -> None:
        super().__init__("This image's configuration is too large to show.")


class ProgressSink(Protocol):
    """Receives the Engine's progress for a pull or push; display only, never an outcome."""

    def on_pull(self, event: PullProgress) -> None:
        """Take one pull progress message."""
        ...

    def on_push(self, event: PushProgress) -> None:
        """Take one push progress message."""
        ...


@dataclass(frozen=True, slots=True)
class ImageFilters:
    """The list filters as submitted; validated before any Engine call."""

    q: str | None = None
    dangling: str | None = None
    usage: str | None = None
    binding: str | None = None
    update: str | None = None


@dataclass(frozen=True, slots=True)
class ContainerUse:
    """A container that uses an image, named only when the viewer may see it."""

    id: str
    short_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ImageReferenceView:
    """One reference of an image with the binding it has and its stored update check, if any."""

    text: str
    key: str
    kind: ImageKind
    binding: BindingView
    update_state: str | None = None
    update_checked_at: str | None = None


@dataclass(frozen=True, slots=True)
class ImageRow:
    """One image of the list.

    `containers` names the containers the viewer may see, at most ten; `in_use` counts every
    container on the host that uses the image, so `containers_hidden` is what the names omit.
    """

    environment_id: int
    id: str
    short_id: str
    references: tuple[ImageReferenceView, ...]
    references_hidden: int
    size: int
    created_at: datetime
    dangling: bool
    in_use: int
    containers: tuple[ContainerUse, ...]
    containers_hidden: int


@dataclass(frozen=True, slots=True)
class ImagePage:
    """A page of rows, how many images match the filters, and the page size used.

    `orphans` lists bindings whose reference is on no image; it is set on the first page for
    administrators and is None otherwise.
    """

    page: Page[ImageRow]
    total: int
    limit: int
    environment_name: str
    is_local: bool
    can_create: bool
    can_remove: bool
    orphans: ReapableList | None


@dataclass(frozen=True, slots=True)
class ImageDetail:
    """One image as the detail page shows it.

    `command`, `entrypoint` and `variables` are None for a standard user. Label values of
    sensitive keys are redacted for them, and `variables` redacts sensitive names for everyone.
    """

    environment_id: int
    environment_name: str
    is_local: bool
    id: str
    short_id: str
    references: tuple[ImageReferenceView, ...]
    references_hidden: int
    size: int
    created_at: datetime | None
    architecture: str
    os: str
    containers: tuple[ContainerUse, ...]
    containers_total: int
    containers_hidden: int
    command: str | None
    entrypoint: str | None
    variables: tuple[EnvVar, ...] | None
    labels: tuple[LabelView, ...]
    working_dir: str
    user: str
    exposed_ports: tuple[str, ...]
    can_create: bool
    can_remove: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """One layer of the history; `created_by` is withheld from standard users."""

    layer: str | None
    created_at: datetime | None
    created_by: str | None
    tags: tuple[str, ...]
    size: int


@dataclass(frozen=True, slots=True)
class RemoveImpact:
    """What removing an image names: the image, every reference, and the containers using it."""

    environment_id: int
    environment_name: str
    image_id: str
    short_id: str
    size: int
    references: tuple[str, ...]
    references_total: int
    references_digest: str
    in_use: int
    containers: tuple[ContainerUse, ...]
    containers_hidden: int

    @property
    def references_hidden(self) -> int:
        """Return how many references the list leaves out."""
        return self.references_total - len(self.references)


@dataclass(frozen=True, slots=True)
class UntagImpact:
    """What untagging one reference does.

    `is_last` means no reference remains once the daemon has also dropped the digest references
    of the repository, which leaves the image untagged. `deletes_image` means the daemon then
    removes it, which it does only while no container uses it.
    """

    environment_id: int
    environment_name: str
    image_id: str
    short_id: str
    reference: str
    reference_key: str
    is_last: bool
    deletes_image: bool
    in_use: int
    containers: tuple[ContainerUse, ...]
    containers_hidden: int


@dataclass(frozen=True, slots=True)
class ReplacedImage:
    """The image a new tag would be taken from."""

    id: str
    short_id: str
    references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TagImpact:
    """What tagging an image as a reference does.

    `replaced` is the image that holds the reference now, if any; `expected_replaced_id` is the
    value the confirmation posts back, an image id or "none". `unchanged` means the image
    already has the reference.
    """

    environment_id: int
    environment_name: str
    image_id: str
    short_id: str
    reference: str
    reference_key: str
    replaced: ReplacedImage | None
    unchanged: bool

    @property
    def expected_replaced_id(self) -> str:
        """Return what the confirmation must post back."""
        return NO_REPLACED_IMAGE if self.replaced is None else self.replaced.id


@dataclass(frozen=True, slots=True)
class PruneCandidate:
    """An image a prune would remove; the digest binds the reference list that was shown."""

    id: str
    short_id: str
    references: tuple[str, ...]
    references_total: int
    references_digest: str
    size: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PrunePlan:
    """The images a prune would remove, at most 100, and how many matched in all."""

    environment_id: int
    environment_name: str
    mode: PruneMode
    candidates: tuple[PruneCandidate, ...]
    total: int

    @property
    def truncated(self) -> bool:
        """Report whether more matched than are listed."""
        return self.total > len(self.candidates)

    @property
    def reclaimable(self) -> int:
        """Return the most space the listed images could free; shared layers make it a ceiling."""
        return sum(candidate.size for candidate in self.candidates)


@dataclass(frozen=True, slots=True)
class PruneTarget:
    """One image the caller reviewed in a prune confirmation."""

    image_id: str
    references_digest: str


@dataclass(frozen=True, slots=True)
class PruneItem:
    """The outcome for one listed image."""

    image_id: str
    short_id: str
    references: tuple[str, ...]
    status: PruneStatus
    size: int


@dataclass(frozen=True, slots=True)
class PruneResult:
    """One item per listed image, in listed order; reclaimed counts what was removed, at most."""

    mode: PruneMode
    items: tuple[PruneItem, ...]

    @property
    def removed(self) -> int:
        """Return how many images were removed."""
        return sum(1 for item in self.items if item.status == "removed")

    @property
    def reclaimed_up_to(self) -> int:
        """Return the most space the removed images freed; shared layers make it an upper bound."""
        return sum(item.size for item in self.items if item.status == "removed")


@dataclass(frozen=True, slots=True)
class ImageBatchTarget:
    """One image the caller reviewed in a batch removal confirmation.

    `label` is the text the confirmation showed; it names the image in the result and is never
    used to find it.
    """

    image_id: str
    references_digest: str
    in_use: int
    label: str = ""


@dataclass(frozen=True, slots=True)
class ImageBatchTargetView:
    """An image a batch removal confirmation names; `token` is what the run posts back.

    `references` are at most 100 and `references_total` counts them all. An image with
    `in_use` above zero is named but is not removed by the batch: force is on its own page.
    """

    image_id: str
    short_id: str
    size: int
    references: tuple[str, ...]
    references_total: int
    references_digest: str
    in_use: int
    containers: tuple[ContainerUse, ...]
    containers_hidden: int

    @property
    def label(self) -> str:
        """Return the first reference, or the short id of an image with none."""
        return self.references[0] if self.references else self.short_id

    @property
    def token(self) -> str:
        """Return `id:references_digest:in_use`, the hidden field value that binds the run."""
        return f"{self.image_id}:{self.references_digest}:{self.in_use}"


@dataclass(frozen=True, slots=True)
class ImageBatchConfirmation:
    """The images of a batch removal; `unavailable` counts the selection that does not exist."""

    environment_id: int
    environment_name: str
    is_local: bool
    targets: tuple[ImageBatchTargetView, ...]
    unavailable: int


@dataclass(frozen=True, slots=True)
class ImageBatchItem:
    """The outcome for one image of a batch removal; the message is the product's own."""

    image_id: str
    short_id: str
    label: str
    status: BatchStatus
    message: str
    size: int


@dataclass(frozen=True, slots=True)
class ImageBatchResult:
    """One item per distinct image, in the order submitted."""

    items: tuple[ImageBatchItem, ...]

    @property
    def removed(self) -> int:
        """Return how many images were removed."""
        return sum(1 for item in self.items if item.status == "removed")

    @property
    def failed(self) -> int:
        """Return how many images were not removed."""
        return len(self.items) - self.removed

    @property
    def reclaimed_up_to(self) -> int:
        """Return the most space the removed images freed; shared layers make it an upper bound."""
        return sum(item.size for item in self.items if item.status == "removed")


@dataclass(frozen=True, slots=True)
class PullPlan:
    """A pull decided but not started: the reference and the credential choice, with its source."""

    environment_id: int
    reference: ImageReference
    choice: CredentialChoice
    source: PullSource


@dataclass(frozen=True, slots=True)
class PushPlan:
    """A push decided but not started: one tag of one image and the credential choice."""

    environment_id: int
    image_id: str
    reference: ImageReference
    choice: CredentialChoice
    source: PullSource


@dataclass(frozen=True, slots=True)
class PullOutcome:
    """What a pull did; `binding` is whether it created, changed or kept the binding."""

    reference: ImageReference
    image_id: str | None
    digest: str | None
    binding: RecordOutcome


@dataclass(frozen=True, slots=True)
class PushOutcome:
    """What a push did."""

    reference: ImageReference
    image_id: str
    digest: str | None
    binding: RecordOutcome


@dataclass(frozen=True, slots=True)
class TagOutcome:
    """What a tag did; unchanged means the image already had the reference."""

    reference: ImageReference
    image_id: str
    replaced_image_id: str | None
    unchanged: bool


@dataclass(frozen=True, slots=True)
class RemoveOutcome:
    """What a removal or untag did."""

    image_id: str
    references: int
    untagged: int
    deleted: bool
    force: bool
    in_use_containers: int
    size: int


def references_digest(keys: Iterable[str]) -> str:
    """Return the SHA-256 over the sorted distinct reference keys that binds a confirmation."""
    joined = "\n".join(sorted(set(keys)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
