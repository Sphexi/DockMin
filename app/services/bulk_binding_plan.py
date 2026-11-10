"""The bulk binding review and the result of applying it, as the pages and plan digest read them."""

import hashlib
from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace
from typing import Final, Literal

CHANGED_MESSAGE: Final = (
    "Changed since the review; nothing was bound at this address. Start a new review to see "
    "the current images."
)
MAX_LEFT_OUT_SHOWN: Final = 200

type LeftOutReason = Literal["pinned", "no_local_digest", "no_credential_set"]
type OtherKind = Literal["works", "needs_set", "not_checked"]
type ItemStatus = Literal["bound", "skipped", "failed", "left"]


@dataclass(frozen=True, slots=True)
class SetOption:
    """A credential set whose registry digest for a reference is one the host holds."""

    set_id: int
    name: str
    digest: str


@dataclass(frozen=True, slots=True)
class BindableImage:
    """An unbound reference that at least one granted set can read.

    One option is offered as it is. With `pick` the administrator chooses among the options, or
    leaves the image unbound.
    """

    reference: str
    display: str
    options: tuple[SetOption, ...]
    pick: bool

    def option(self, set_id: int) -> SetOption | None:
        """Return the option for the set, or None when that set did not read the image."""
        return next((option for option in self.options if option.set_id == set_id), None)

    @property
    def plan_line(self) -> str:
        """Return the line that names what the review offered for this reference."""
        if self.pick:
            offered = ",".join(f"{o.set_id}:{o.digest}" for o in self.options)
            return f"image {self.reference} sets {offered}"
        (only,) = self.options
        return f"image {self.reference} set {only.set_id} {only.digest}"


@dataclass(frozen=True, slots=True)
class OtherImage:
    """A checked reference that is not offered for binding.

    `message` says why in one line; `details` carries one line for each set that did not read it.
    """

    reference: str
    display: str
    kind: OtherKind
    message: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LeftOutImage:
    """An unbound reference that was not checked, with why."""

    reference: str
    display: str
    reason: LeftOutReason


@dataclass(frozen=True, slots=True)
class SetRef:
    """A credential set granted at an address."""

    set_id: int
    name: str


@dataclass(frozen=True, slots=True)
class RegistryGroup:
    """The checked references at one registry address, as one confirmation covers them.

    `sets` are the credential sets granted at the address, which an apply must still find
    unchanged. `others` holds at most `MAX_LEFT_OUT_SHOWN` rows of `others_total`.
    """

    address: str
    sets: tuple[SetRef, ...]
    bindable: tuple[BindableImage, ...]
    others: tuple[OtherImage, ...]
    others_total: int

    @property
    def plan_lines(self) -> tuple[str, ...]:
        """Return the sorted plan lines of what the group offers."""
        return tuple(sorted(item.plan_line for item in self.bindable))

    @property
    def digest(self) -> str:
        """Return the SHA-256 that names exactly what binding the group would write."""
        return plan_digest(self.plan_lines)

    def without(self, references: Collection[str]) -> "RegistryGroup":
        """Return the group with the named references taken out of what it offers."""
        return replace(
            self, bindable=tuple(i for i in self.bindable if i.reference not in references)
        )


@dataclass(frozen=True, slots=True)
class BulkBindingReview:
    """What one review found on an environment; it writes nothing.

    `not_checked_total` counts the references that could have been checked and were not, because
    of the cap or the time budget.
    """

    environment_id: int
    environment_name: str
    groups: tuple[RegistryGroup, ...]
    left_out: tuple[LeftOutImage, ...]
    left_out_total: int
    not_checked_total: int

    def group(self, address: str) -> RegistryGroup | None:
        """Return the group at the address."""
        return next((group for group in self.groups if group.address == address), None)

    @property
    def offering(self) -> tuple[RegistryGroup, ...]:
        """Return the groups with something to bind."""
        return tuple(group for group in self.groups if group.bindable)

    def image(self, reference: str) -> BindableImage | None:
        """Return the offered image with the reference, in whichever group holds it."""
        return next(
            (
                item
                for group in self.groups
                for item in group.bindable
                if item.reference == reference
            ),
            None,
        )

    def settled(self, done: Collection[str]) -> "BulkBindingReview":
        """Return the review without the references that are bound or skipped.

        A group that has nothing offered and nothing to show is dropped.
        """
        groups = (group.without(done) for group in self.groups)
        return replace(self, groups=tuple(g for g in groups if g.bindable or g.others))


@dataclass(frozen=True, slots=True)
class BoundItem:
    """What happened to one reference when a confirmed registry was applied."""

    reference: str
    display: str
    address: str
    status: ItemStatus
    message: str


@dataclass(frozen=True, slots=True)
class BulkBindingResult:
    """The outcome of one apply: a row for each reference, and the registries that changed."""

    environment_id: int
    environment_name: str
    registries: tuple[str, ...]
    items: tuple[BoundItem, ...]
    changed: tuple[str, ...] = ()

    def count(self, status: ItemStatus) -> int:
        """Return how many references ended with the status."""
        return sum(1 for item in self.items if item.status == status)


def plan_digest(lines: Iterable[str]) -> str:
    """Return the SHA-256 hex digest over the plan lines, one per line."""
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
