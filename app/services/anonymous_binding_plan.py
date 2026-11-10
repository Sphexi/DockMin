"""The anonymous binding review and its apply result, as the pages and the plan digest read them."""

import hashlib
from collections.abc import Collection
from dataclasses import dataclass, replace
from typing import Final, Literal

CHANGED_MESSAGE: Final = (
    "The host or the bindings changed since the review. Start a new review to see the current list."
)
MAX_LEFT_OUT_SHOWN: Final = 200

type LeftOutReason = Literal[
    "pinned", "no_local_digest", "no_anonymous_entry", "entry_tls_settings"
]
type ItemStatus = Literal["bound", "skipped", "failed"]


@dataclass(frozen=True, slots=True)
class MatchedImage:
    """A reference whose local digest the registry reported for an anonymous HEAD."""

    reference: str
    display: str
    digest: str


@dataclass(frozen=True, slots=True)
class UnmatchedImage:
    """A reference checked anonymously that cannot be bound.

    `reason` is a stable identifier and `message` the text the page shows for it.
    """

    reference: str
    display: str
    reason: str
    message: str


@dataclass(frozen=True, slots=True)
class LeftOutImage:
    """An unbound reference that was not checked, with why."""

    reference: str
    display: str
    reason: LeftOutReason


@dataclass(frozen=True, slots=True)
class RegistryGroup:
    """The references at one registry address, as one confirmation covers them.

    `entries` are the names of the granted entries with authentication off that read as
    anonymous; `authenticated_sets` counts the granted sets at the address that have it on.
    `unmatched` holds at most `MAX_LEFT_OUT_SHOWN` rows of `unmatched_total`.
    """

    address: str
    entries: tuple[str, ...]
    authenticated_sets: int
    matched: tuple[MatchedImage, ...]
    unmatched: tuple[UnmatchedImage, ...]
    unmatched_total: int


@dataclass(frozen=True, slots=True)
class AnonymousBindingReview:
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

    def plan_lines(self, addresses: Collection[str]) -> tuple[str, ...]:
        """Return the sorted `image <reference> anonymous <digest>` lines of the named groups."""
        return tuple(
            sorted(
                plan_line(item.reference, item.digest)
                for group in self.groups
                if group.address in addresses
                for item in group.matched
            )
        )

    def digest(self, addresses: Collection[str]) -> str:
        """Return the SHA-256 that names exactly what binding the named groups would write."""
        return plan_digest(self.plan_lines(addresses))

    def without(self, addresses: Collection[str]) -> "AnonymousBindingReview":
        """Return the review with the named groups removed."""
        return replace(self, groups=tuple(g for g in self.groups if g.address not in addresses))


@dataclass(frozen=True, slots=True)
class BoundItem:
    """What happened to one reference when a confirmed registry was applied."""

    reference: str
    display: str
    address: str
    status: ItemStatus
    message: str


@dataclass(frozen=True, slots=True)
class AnonymousBindingResult:
    """The outcome of applying the confirmed registries."""

    environment_id: int
    environment_name: str
    registries: tuple[str, ...]
    items: tuple[BoundItem, ...]

    def count(self, status: ItemStatus) -> int:
        """Return how many references ended with the status."""
        return sum(1 for item in self.items if item.status == status)


def plan_line(reference: str, digest: str) -> str:
    """Return the plan line that names one anonymous binding."""
    return f"image {reference} anonymous {digest}"


def plan_digest(lines: tuple[str, ...]) -> str:
    """Return the SHA-256 hex digest over the plan lines, one per line."""
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
