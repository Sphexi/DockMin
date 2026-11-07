"""Vocabulary and the one evaluation rule shared by every authorisation path."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol, get_args

from app.auth.errors import ValidationFailed
from app.db.repositories.resource_control import (
    AccessLevel,
    ControlStanding,
    ResourceType,
    canonical_stack_key,
)

__all__ = [
    "COMPOSE_PROJECT_LABEL",
    "INHERITING_TYPES",
    "RESERVED_LABELS",
    "RESOURCE_TYPES",
    "SWARM_NAMESPACE_LABEL",
    "AccessLevel",
    "Controllable",
    "Decision",
    "Reason",
    "ResourceKey",
    "ResourceRef",
    "ResourceType",
    "SubmittedLabels",
    "canonical_stack_key",
    "evaluate",
    "is_canonical_id",
    "satisfies",
    "stack_label",
    "stack_of",
]

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
SWARM_NAMESPACE_LABEL = "com.docker.stack.namespace"

# The two labels that place an object in a stack. Inheritance believes them, so only the product
# may write them.
RESERVED_LABELS = frozenset({COMPOSE_PROJECT_LABEL, SWARM_NAMESPACE_LABEL})

RESOURCE_TYPES: tuple[ResourceType, ...] = get_args(ResourceType)
# Listed, not derived: a type added to ResourceType must be opted in here before it inherits a
# stack's control. A stack is the source of inheritance and has nothing above it.
INHERITING_TYPES: frozenset[ResourceType] = frozenset(
    {"container", "service", "volume", "secret", "config", "network"}
)

# Only the types whose keys have a fixed shape. A control keyed by a short id would govern nothing
# while the panel displayed it as governing something.
_CANONICAL_ID: dict[ResourceType, re.Pattern[str]] = {
    "container": re.compile(r"[0-9a-f]{64}"),
    "service": re.compile(r"[0-9a-z]{25}"),
    "config": re.compile(r"[0-9a-z]{25}"),
    "secret": re.compile(r"[0-9a-z]{25}"),
    "network": re.compile(r"[0-9a-f]{64}|[0-9a-z]{25}"),
    "volume": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}"),
    "stack": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}"),
}

_RANK: dict[AccessLevel, int] = {"read": 1, "write": 2}


class Reason(StrEnum):
    """Why an object is, or is not, reachable; the effective-access view reports these."""

    ADMINISTRATOR = "administrator"
    OWNER = "owner"
    USER_GRANT = "user_grant"
    TEAM_GRANT = "team_grant"
    PUBLIC = "public"
    UNCONTROLLED_DEFAULT = "uncontrolled_default"
    ADMINISTRATORS_ONLY = "administrators_only"
    NOT_GRANTED = "not_granted"
    UNCONTROLLED_DENIED = "uncontrolled_denied"
    NO_ENVIRONMENT_ACCESS = "no_environment_access"
    LOCAL_SOCKET_READ_ONLY = "local_socket_read_only"


@dataclass(frozen=True, slots=True)
class ResourceKey:
    """Identifies one controlled object; for a stack, resource_id is its name."""

    environment_id: int
    resource_type: ResourceType
    resource_id: str


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """An object together with the labels Docker reported for it.

    For objects read from the Engine only. No authorisation entry point accepts one: those take a
    ResourceKey and fetch the labels themselves.
    """

    environment_id: int
    resource_type: ResourceType
    resource_id: str
    # Required, never defaulted: an object whose labels were left out would be judged as having
    # no stack, which under a permissive default reads as visible.
    labels: Mapping[str, str] = field(compare=False, hash=False)

    @property
    def key(self) -> ResourceKey:
        """Return the object's identity without its labels."""
        return ResourceKey(self.environment_id, self.resource_type, self.resource_id)


class Controllable(Protocol):
    """What an item must expose to be filtered; a view model carries these four."""

    @property
    def environment_id(self) -> int:
        """Return the environment the object belongs to."""
        ...

    @property
    def resource_type(self) -> ResourceType:
        """Return the controlled type."""
        ...

    @property
    def resource_id(self) -> str:
        """Return the object id, or the name for a stack."""
        ...

    @property
    def labels(self) -> Mapping[str, str]:
        """Return the object's Docker labels."""
        ...


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome for one object: the level held, or None, and every reason that applies."""

    level: AccessLevel | None
    reasons: tuple[Reason, ...]
    control_id: int | None = None
    inherited_from: str | None = None

    @property
    def visible(self) -> bool:
        """Report whether the caller may see the object at all."""
        return self.level is not None


def satisfies(held: AccessLevel | None, required: AccessLevel) -> bool:
    """Report whether a held level meets a required one; None meets nothing."""
    return held is not None and _RANK[held] >= _RANK[required]


def stack_of(labels: Mapping[str, str]) -> str | None:
    """Return the canonical key of the stack an object's labels place it in, or None."""
    for key in (COMPOSE_PROJECT_LABEL, SWARM_NAMESPACE_LABEL):
        value = labels.get(key)
        if value:
            return canonical_stack_key(value)
    return None


def is_canonical_id(resource_type: ResourceType, resource_id: str) -> bool:
    """Report whether an id has the form the model keys this type by; other types always do."""
    pattern = _CANONICAL_ID.get(resource_type)
    return pattern is None or pattern.fullmatch(resource_id) is not None


@dataclass(frozen=True, slots=True)
class SubmittedLabels:
    """Labels a caller asked for on a new object.

    Any creation path takes this type for what the caller supplied, so the two stack-membership
    labels cannot reach Docker from a submission: a caller who could label an object into a stack
    would inherit that stack's access.
    """

    values: Mapping[str, str]

    def __post_init__(self) -> None:
        """Refuse a submission that names either reserved label."""
        if any(key.strip().casefold() in _RESERVED_FOLDED for key in self.values):
            raise ValidationFailed(
                {"labels": "The stack membership labels are set by the product."}
            )


_RESERVED_FOLDED = frozenset(label.casefold() for label in RESERVED_LABELS)


def stack_label(kind: Literal["compose", "swarm"], name: str) -> dict[str, str]:
    """Return the membership label the product sets when it deploys a stack."""
    key = COMPOSE_PROJECT_LABEL if kind == "compose" else SWARM_NAMESPACE_LABEL
    return {key: name}


def evaluate(
    standing: ControlStanding | None,
    *,
    default: AccessLevel | None,
    inherited_from: str | None = None,
) -> Decision:
    """Decide one object for one non-administrator from its control, or from the default.

    Every in-memory access check calls it; the SQL membership filter restates it and must agree.
    """
    if standing is None:
        if default is None:
            return Decision(None, (Reason.UNCONTROLLED_DENIED,))
        return Decision(default, (Reason.UNCONTROLLED_DEFAULT,))
    if standing.is_administrators_only:
        # Wins over any grant still attached to the control.
        return Decision(None, (Reason.ADMINISTRATORS_ONLY,), standing.control_id, inherited_from)
    reasons = tuple(
        reason
        for reason, held in (
            (Reason.PUBLIC, standing.is_public),
            (Reason.OWNER, standing.is_owner),
            (Reason.USER_GRANT, standing.user_granted),
            (Reason.TEAM_GRANT, standing.team_granted),
        )
        if held
    )
    if not reasons:
        return Decision(None, (Reason.NOT_GRANTED,), standing.control_id, inherited_from)
    return Decision(standing.access_level, reasons, standing.control_id, inherited_from)
