"""The onboarding review, the selection made from it, the plan an apply binds to, and its result."""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from app.db.repositories.stack import StackKind
from app.services.access import ResourceType
from app.services.binding_candidates import ReferenceOutcome
from app.services.onboarding_ownership import Ownership
from app.services.pull_credentials import CredentialChoice
from app.services.stack_host_read import HostReadReport
from app.services.stack_read_result import HostReadResult, NeedsFile
from app.services.volume_backfill import Candidate, Source

ANONYMOUS: Final = "anonymous"
NONE: Final = "-"
CHANGED_MESSAGE: Final = (
    "The host or the access settings changed since you reviewed it; nothing was changed."
)

type Section = Literal["stack", "image", "object"]
type ItemStatus = Literal["done", "skipped", "failed"]


@dataclass(frozen=True, slots=True)
class StackTarget:
    """An unmanaged Compose project or Swarm namespace and what reading its files produced.

    `report` is None, and `reason` says why, when nothing could be read at all.
    """

    kind: StackKind
    name: str
    report: HostReadReport | None = None
    reason: str | None = None

    @property
    def result(self) -> HostReadResult | None:
        """Return the prepared stack, or None when a file is still needed."""
        return None if self.report is None else self.report.result

    @property
    def offered(self) -> bool:
        """Report whether the stack can be adopted as is."""
        return self.result is not None

    @property
    def needs(self) -> NeedsFile | None:
        """Return why no file was produced, or None when one was."""
        if self.report is not None and isinstance(self.report.outcome, NeedsFile):
            return self.report.outcome
        if self.report is None:
            return NeedsFile(self.reason or "unreadable", "name")
        return None

    @property
    def digest(self) -> str:
        """Return the digest of what would be stored; empty when no file was produced."""
        result = self.result
        return "" if result is None else result_digest(result)


@dataclass(frozen=True, slots=True)
class ImageTarget:
    """An unbound image reference in use and what the digest check found."""

    outcome: ReferenceOutcome

    @property
    def reference(self) -> str:
        """Return the canonical reference."""
        return self.outcome.reference

    @property
    def offered(self) -> bool:
        """Report whether a verified set exists to bind it to."""
        return self.outcome.outcome in ("binds", "ambiguous")

    @property
    def digest_of(self) -> Mapping[CredentialChoice, str]:
        """Return the registry digest of every set that read the image."""
        return {
            check.choice: check.digest for check in self.outcome.matches if check.digest is not None
        }


@dataclass(frozen=True, slots=True)
class ObjectTarget:
    """An object with no control of its own and none inherited from a stack.

    `owner` and `source` are a volume's candidate from the ownership backfill, when it has one.
    """

    type: ResourceType
    id: str
    name: str
    owner: Candidate | None = None
    source: Source | None = None

    @property
    def key(self) -> tuple[str, str]:
        """Return the identity the selection and the plan use."""
        return self.type, self.id


@dataclass(frozen=True, slots=True)
class ProtectedObject:
    """One of dockmin's own objects: listed, never offered."""

    type: ResourceType
    name: str
    id: str


@dataclass(frozen=True, slots=True)
class OnboardingReview:
    """Everything the review found on one environment, held for its starter.

    `dismissed` counts the unmanaged stack entries a dismissal covers, which are not reviewed.
    `objects_beyond` counts uncontrolled objects past the cap, which are left alone. The
    `*_complete` flags are false when a list could not be read in full.
    """

    environment_id: int
    environment_name: str
    stacks: tuple[StackTarget, ...]
    images: tuple[ImageTarget, ...]
    objects: tuple[ObjectTarget, ...]
    protected: tuple[ProtectedObject, ...] = ()
    dismissed: int = 0
    objects_beyond: int = 0
    images_complete: bool = True
    objects_complete: bool = True
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Selection:
    """What an administrator ticked, as indexes into the review, and the ownership to give.

    `images` maps an image's index to the choice for it, None for a set that is not chosen.
    """

    stacks: frozenset[int] = frozenset()
    images: Mapping[int, CredentialChoice | None] = field(default_factory=dict)
    objects: frozenset[int] = frozenset()
    ownership: Ownership = field(default_factory=Ownership)


@dataclass(frozen=True, slots=True)
class PlannedStack:
    """A ticked stack that can still be adopted."""

    target: StackTarget
    digest: str


@dataclass(frozen=True, slots=True)
class PlannedImage:
    """A ticked image to bind, with the set chosen and the digest that proved it."""

    target: ImageTarget
    choice: CredentialChoice
    set_name: str | None
    digest: str


@dataclass(frozen=True, slots=True)
class PlannedControl:
    """A control to create over one object."""

    target: ObjectTarget
    owner_user_id: int | None
    owner_name: str
    team_id: int | None
    team_name: str
    level: str
    source: Source | None


@dataclass(frozen=True, slots=True)
class Dropped:
    """A ticked target that could not go into the plan, and why."""

    section: Section
    label: str
    reason: str


@dataclass(frozen=True, slots=True)
class Plan:
    """The targets an apply would write and the digest it binds to.

    `left` counts ticked objects that stay administrators-only because no owner, team or
    candidate gives them a control.
    """

    stacks: tuple[PlannedStack, ...]
    images: tuple[PlannedImage, ...]
    controls: tuple[PlannedControl, ...]
    dropped: tuple[Dropped, ...]
    left: int
    ownership: Ownership

    @property
    def lines(self) -> tuple[str, ...]:
        """Return the sorted plan lines."""
        return plan_lines(self)

    @property
    def digest(self) -> str:
        """Return the SHA-256 the apply binds to."""
        return digest_of(self.lines)

    @property
    def empty(self) -> bool:
        """Report whether nothing would be written."""
        return not (self.stacks or self.images or self.controls)


@dataclass(frozen=True, slots=True)
class ApplyItem:
    """What happened to one target."""

    section: Section
    label: str
    status: ItemStatus
    message: str


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """The outcome of an apply: one item per target of the confirmed plan."""

    environment_id: int
    environment_name: str
    items: tuple[ApplyItem, ...]
    left: int = 0

    def of(self, section: Section) -> tuple[ApplyItem, ...]:
        """Return the items of one section, in the order they ran."""
        return tuple(item for item in self.items if item.section == section)

    def count(self, section: Section, status: ItemStatus) -> int:
        """Return how many items of a section ended with the status."""
        return sum(1 for item in self.items if item.section == section and item.status == status)


def choice_text(choice: CredentialChoice) -> str:
    """Return a choice as the plan writes it: the set id, or `anonymous`."""
    return ANONYMOUS if choice.credential_set_id is None else str(choice.credential_set_id)


def result_digest(result: HostReadResult) -> str:
    """Return a digest of the text, files and variables a read result would store."""
    document = {
        "compose": result.compose_text,
        "files": [
            [item.name, item.secret, hashlib.sha256(item.content).hexdigest()]
            for item in result.files
        ],
        "variables": [
            [item.name, item.secret, hashlib.sha256(item.value.encode("utf-8")).hexdigest()]
            for item in result.variables
        ],
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_lines(plan: Plan) -> tuple[str, ...]:
    """Return the plan as sorted `stack`, `image` and `control` lines."""
    lines = [f"stack {item.target.kind} {item.target.name} {item.digest}" for item in plan.stacks]
    lines += [
        f"image {item.target.reference} {choice_text(item.choice)} {item.digest}"
        for item in plan.images
    ]
    lines += [
        f"control {item.target.type} {item.target.id} "
        f"{NONE if item.owner_user_id is None else item.owner_user_id} "
        f"{NONE if item.team_id is None else item.team_id} {item.level}"
        for item in plan.controls
    ]
    return tuple(sorted(lines))


def digest_of(lines: Iterable[str]) -> str:
    """Return the SHA-256 over the lines, each ended by a newline."""
    return hashlib.sha256("".join(f"{line}\n" for line in lines).encode("utf-8")).hexdigest()
