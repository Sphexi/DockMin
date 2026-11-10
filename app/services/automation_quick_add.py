"""Quick add: whether an automation can take one more object by name, and the resulting target."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from app.auth.errors import ValidationFailed
from app.db.repositories.automation_rule import AutomationRule
from app.db.repositories.resource_control import ResourceType, canonical_stack_key
from app.db.repositories.stack import StackKind
from app.services.access import ResourceKey
from app.services.container_derivation import SWARM_SERVICE_LABEL
from app.subsystems.base import Subsystem
from app.subsystems.selectors import (
    ALL_KINDS,
    MAX_TARGET_NAMES,
    ContainerTarget,
    SelectorKind,
    ServiceTarget,
    StackTarget,
    canonical,
    compose_managed,
    parse_selector_text,
)

type ObjectKind = Literal["container", "stack", "swarm_stack", "service"]
type Reason = Literal[
    "other_environment",
    "rule_disabled",
    "not_offered",
    "compose_managed",
    "swarm_task",
    "already_target",
    "target_full",
    "name_not_valid",
    "target_not_valid",
]
type OwnerStanding = Literal["write", "read", "hidden", "lost"]

SELECTOR_KINDS: Final[Mapping[ObjectKind, SelectorKind]] = {
    "container": "container",
    "stack": "stack",
    "swarm_stack": "stack",
    "service": "service",
}
RESOURCE_TYPES: Final[Mapping[ObjectKind, ResourceType]] = {
    "container": "container",
    "stack": "stack",
    "swarm_stack": "stack",
    "service": "service",
}
STACK_KINDS: Final[Mapping[ObjectKind, StackKind]] = {"stack": "compose", "swarm_stack": "swarm"}
OBJECT_WORDS: Final[Mapping[ObjectKind, str]] = {
    "container": "container",
    "stack": "Compose stack",
    "swarm_stack": "Swarm stack",
    "service": "Swarm service",
}
NAMED_TARGETS: Final = {
    "container": ContainerTarget,
    "stack": StackTarget,
    "service": ServiceTarget,
}


@dataclass(frozen=True, slots=True)
class QuickAddObject:
    """An object resolved under the viewer's read access; key is its id, or a stack's name."""

    environment_id: int
    kind: ObjectKind
    key: str
    name: str
    labels: Mapping[str, str] = field(default_factory=dict)

    @property
    def resource_key(self) -> ResourceKey:
        """Return the key access to the object is decided by."""
        return ResourceKey(self.environment_id, RESOURCE_TYPES[self.kind], self.key)

    @property
    def is_swarm_task(self) -> bool:
        """Report whether the object is a container that Swarm runs as a service task."""
        return self.kind == "container" and SWARM_SERVICE_LABEL in self.labels


@dataclass(frozen=True, slots=True)
class QuickAddOption:
    """One automation the object can be added to, as the list shows it."""

    rule_id: int
    name: str
    subsystem: str
    target: str


@dataclass(frozen=True, slots=True)
class QuickAddHelp:
    """Facts behind the lines that say where an automation can be added instead.

    compose_left_out is set when an automation that refuses Compose-managed containers was left
    out; compose_project then names the project when it is a valid stack name, and compose_stack
    is the name of that managed stack's page when the viewer may read it. swarm_service_id is the
    service of a Swarm task container when an automation naming services exists and the viewer
    may read the service.
    """

    compose_left_out: bool = False
    compose_project: str | None = None
    compose_stack: str | None = None
    swarm_service_id: str | None = None


@dataclass(frozen=True, slots=True)
class QuickAddOffer:
    """The automations an object can be added to, and the help lines that go with them."""

    options: tuple[QuickAddOption, ...] = ()
    help: QuickAddHelp = field(default_factory=QuickAddHelp)


@dataclass(frozen=True, slots=True)
class Declined:
    """An automation that cannot take the object, with the reason in words."""

    reason: Reason
    message: str


@dataclass(frozen=True, slots=True)
class Addition:
    """The stored selector with the object added, in canonical form, and the names around it."""

    selector: str
    before: tuple[str, ...]
    after: tuple[str, ...]


def names_in(selector: str) -> tuple[str, ...]:
    """Return the names a stored selector lists; none for a selector that lists no names."""
    try:
        parsed = parse_selector_text(selector, ALL_KINDS)
    except ValidationFailed:
        return ()
    return parsed.names if isinstance(parsed, ContainerTarget | StackTarget | ServiceTarget) else ()


def names_services(selector: str) -> bool:
    """Report whether a stored selector names Swarm services."""
    try:
        return isinstance(parse_selector_text(selector, ALL_KINDS), ServiceTarget)
    except ValidationFailed:
        return False


def qualify(handler: Subsystem, rule: AutomationRule, obj: QuickAddObject) -> Addition | Declined:
    """Return the rule's selector with the object added, or why the rule cannot take it."""
    words = OBJECT_WORDS[obj.kind]
    if rule.environment_id != obj.environment_id:
        return Declined("other_environment", f"This {words} is in another environment.")
    if not rule.is_enabled:
        return Declined("rule_disabled", "Only an enabled automation can be added to.")
    if obj.is_swarm_task:
        return Declined(
            "swarm_task",
            "Swarm replaces a task container under a new name, so a name added here would stop "
            "matching. Add its service to the automation instead.",
        )
    wanted = SELECTOR_KINDS[obj.kind]
    try:
        parsed = parse_selector_text(rule.target_selector, ALL_KINDS)
    except ValidationFailed:
        return Declined("not_offered", f"This automation cannot take a {words}.")
    target_class = NAMED_TARGETS[wanted]
    stack_kind = STACK_KINDS.get(obj.kind)
    if (
        not isinstance(parsed, target_class)
        or wanted not in handler.target_kinds
        or (stack_kind is not None and stack_kind not in handler.stack_kinds)
    ):
        return Declined("not_offered", f"This automation cannot take a {words}.")
    if handler.refuses_compose_managed and obj.kind == "container":
        managed = compose_managed(obj.name, obj.labels)
        if managed is not None:
            return Declined(
                "compose_managed", f"{managed}. Add its stack to the automation instead."
            )
    if _already_named(parsed, obj):
        return Declined("already_target", f"The automation already acts on this {words}.")
    if len(parsed.names) >= MAX_TARGET_NAMES:
        return Declined(
            "target_full", f"The automation already names the most targets, {MAX_TARGET_NAMES}."
        )
    if target_class.NAME_PATTERN.fullmatch(obj.name) is None:
        return Declined(
            "name_not_valid",
            f"This name cannot be added to an automation. {target_class.NAME_MESSAGE}",
        )
    extended = canonical(parsed.model_copy(update={"names": (*parsed.names, obj.name)}))
    try:
        added = handler.parse_selector(extended)
    except ValidationFailed as invalid:
        return Declined("target_not_valid", " ".join(invalid.fields.values()))
    return Addition(added, parsed.names, names_in(added))


def _already_named(
    parsed: ContainerTarget | StackTarget | ServiceTarget, obj: QuickAddObject
) -> bool:
    if isinstance(parsed, StackTarget):
        return canonical_stack_key(obj.name) in {canonical_stack_key(each) for each in parsed.names}
    return obj.name in parsed.names
