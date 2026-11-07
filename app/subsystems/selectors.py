"""Target selectors of automation rules: parsing, canonical form, matching and the access check."""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, ClassVar, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.stack import StackKind, StoredStack
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError
from app.docker_engine.models import ContainerSummary, SwarmService
from app.services.access import (
    COMPOSE_PROJECT_LABEL,
    ResourceKey,
    canonical_stack_key,
    is_canonical_id,
)
from app.services.authorisation import Authoriser
from app.services.object_inspector import (
    EnvironmentUnavailable,
    HostClients,
    SwarmUnavailable,
    swarm_unavailable_from,
    unavailable_from,
)
from app.services.service_spec import SERVICE_NAME
from app.services.stack_deploy import in_project
from app.services.stack_document import SWARM_STACK_NAME

type SelectorKind = Literal[
    "container", "stack", "labels", "environment", "service", "service_labels"
]

CONTAINER_KINDS: Final[frozenset[SelectorKind]] = frozenset(
    {"container", "stack", "labels", "environment"}
)
ALL_KINDS: Final[frozenset[SelectorKind]] = CONTAINER_KINDS | {"service", "service_labels"}
SELECTOR_FIELD: Final = "target_selector"
MAX_SELECTOR_BYTES: Final = 4096
MAX_TARGET_NAMES: Final = 20
DESCRIBED_NAMES: Final = 3
MAX_LABEL_TERMS: Final = 8
MAX_LABEL_KEY: Final = 128
MAX_LABEL_VALUE: Final = 256
UNREADABLE_TARGET: Final = "Unreadable target"
NOT_VALID: Final = "Choose a valid target."
TOO_LARGE: Final = "Use at most 4 KiB."
NOT_OFFERED: Final = "This automation cannot target that."
COMPOSE_ONLY: Final = "This automation works on Compose stacks."
NOT_SWARM: Final = "This environment is not a Swarm manager."
UNVERIFIED: Final = "The environment is unreachable; the target could not be checked."

_CONTAINER_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class TargetUnverified(EnvironmentUnavailable):
    """The host could not be asked whether the target exists, so its access was not checked."""

    def __init__(self) -> None:
        super().__init__("unreachable")
        self.args = (UNVERIFIED,)


type Terms = tuple[tuple[str, str | None], ...]


class _Target(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Named(_Target):
    """A target that lists the names of the objects it acts on, sorted and without repeats."""

    names: tuple[str, ...]
    NAME_PATTERN: ClassVar[re.Pattern[str]]
    NAME_MESSAGE: ClassVar[str]

    @field_validator("names")
    @classmethod
    def _names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if cls.NAME_PATTERN.fullmatch(name) is None:
                raise ValueError(cls.NAME_MESSAGE)
        unique = tuple(sorted(set(value)))
        if not 1 <= len(unique) <= MAX_TARGET_NAMES:
            raise ValueError(f"Name 1 to {MAX_TARGET_NAMES} targets.")
        return unique


class ContainerTarget(_Named):
    """The containers with these names, resolved on every run."""

    kind: Literal["container"] = "container"
    NAME_PATTERN = _CONTAINER_NAME
    NAME_MESSAGE = (
        "Use a container name: letters, digits, underscores, periods and hyphens, "
        "up to 128 characters."
    )


class StackTarget(_Named):
    """The stacks with these names, of either kind."""

    kind: Literal["stack"] = "stack"
    NAME_PATTERN = SWARM_STACK_NAME
    NAME_MESSAGE = (
        "Use a stack name: letters, digits, underscores and hyphens, up to 63 characters."
    )

    @field_validator("names")
    @classmethod
    def _once(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len({canonical_stack_key(name) for name in value}) != len(value):
            raise ValueError("Name each stack once.")
        return value


class ServiceTarget(_Named):
    """The Swarm services with these names, resolved on every run."""

    kind: Literal["service"] = "service"
    NAME_PATTERN = SERVICE_NAME
    NAME_MESSAGE = (
        "Use a service name: letters, digits, underscores and hyphens, up to 63 "
        "characters, starting and ending with a letter or digit."
    )


class _Labelled(_Target):
    labels: Terms

    @field_validator("labels")
    @classmethod
    def _terms(cls, value: Terms) -> Terms:
        if not 1 <= len(value) <= MAX_LABEL_TERMS:
            raise ValueError(f"Use 1 to {MAX_LABEL_TERMS} label terms.")
        for key, text in value:
            if not 1 <= len(key) <= MAX_LABEL_KEY or "=" in key or not key.isprintable():
                raise ValueError(
                    f"A label key is 1 to {MAX_LABEL_KEY} printable characters without '='."
                )
            if text is not None and (len(text) > MAX_LABEL_VALUE or not text.isprintable()):
                raise ValueError(
                    f"A label value is at most {MAX_LABEL_VALUE} printable characters."
                )
        if len({key for key, _ in value}) != len(value):
            raise ValueError("Each label key may appear once.")
        return tuple(sorted(value, key=lambda term: term[0]))


class LabelsTarget(_Labelled):
    """Every container carrying all of the terms; a term without a value matches the key alone."""

    kind: Literal["labels"] = "labels"


class ServiceLabelsTarget(_Labelled):
    """Every Swarm service whose own labels carry all of the terms."""

    kind: Literal["service_labels"] = "service_labels"


class EnvironmentTarget(_Target):
    """Every container in the environment."""

    kind: Literal["environment"] = "environment"


type NamedSelector = ContainerTarget | StackTarget | ServiceTarget
type Selector = NamedSelector | LabelsTarget | ServiceLabelsTarget | EnvironmentTarget

_ADAPTER: Final[TypeAdapter[Selector]] = TypeAdapter(
    Annotated[
        ContainerTarget
        | StackTarget
        | LabelsTarget
        | EnvironmentTarget
        | ServiceTarget
        | ServiceLabelsTarget,
        Field(discriminator="kind"),
    ]
)


class StackLookup(Protocol):
    """Reads stack rows; the stack repository satisfies it."""

    async def get_folded(self, environment_id: int, name: str) -> StoredStack | None:
        """Return the stack whose name equals `name` ignoring ASCII case, of either kind."""
        ...


def parse_selector_text(text: str, allowed: frozenset[SelectorKind]) -> Selector:
    """Parse and validate stored or submitted selector text; ValidationFailed otherwise."""
    if len(text.encode("utf-8")) > MAX_SELECTOR_BYTES:
        raise ValidationFailed({SELECTOR_FIELD: TOO_LARGE})
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise ValidationFailed({SELECTOR_FIELD: NOT_VALID}) from None
    try:
        selector = _ADAPTER.validate_python(data)
    except ValidationError as error:
        raise ValidationFailed({SELECTOR_FIELD: _message(error)}) from None
    if selector.kind not in allowed:
        raise ValidationFailed({SELECTOR_FIELD: NOT_OFFERED})
    if len(canonical(selector).encode("utf-8")) > MAX_SELECTOR_BYTES:
        raise ValidationFailed({SELECTOR_FIELD: TOO_LARGE})
    return selector


def canonical(selector: Selector) -> str:
    """Return the one JSON text that names the selector."""
    data: dict[str, object]
    match selector:
        case ContainerTarget(names=names) | StackTarget(names=names) | ServiceTarget(names=names):
            data = {"kind": selector.kind, "names": list(names)}
        case LabelsTarget(labels=terms) | ServiceLabelsTarget(labels=terms):
            data = {"kind": selector.kind, "labels": [[key, value] for key, value in terms]}
        case EnvironmentTarget():
            data = {"kind": "environment"}
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def describe(selector: Selector) -> str:
    """Describe a selector in words; a long list of names ends with a count of the rest."""
    match selector:
        case ContainerTarget(names=names):
            return _list_words("Container", "Containers", names)
        case StackTarget(names=names):
            return _list_words("Stack", "Stacks", names)
        case ServiceTarget(names=names):
            return _list_words("Swarm service", "Swarm services", names)
        case LabelsTarget(labels=terms):
            return "Containers labelled " + ", ".join(term_text(term) for term in terms)
        case ServiceLabelsTarget(labels=terms):
            return "Swarm services labelled " + ", ".join(term_text(term) for term in terms)
        case EnvironmentTarget():
            return "Every container in the environment"


def _list_words(one: str, many: str, names: Sequence[str]) -> str:
    if len(names) == 1:
        return f"{one} {names[0]}"
    shown = ", ".join(names[:DESCRIBED_NAMES])
    rest = len(names) - DESCRIBED_NAMES
    return f"{many} {shown}" + (f" and {rest} more" if rest > 0 else "")


def describe_stored(text: str) -> str:
    """Describe a stored selector, or say that it cannot be read."""
    try:
        selector = parse_selector_text(text, ALL_KINDS)
    except ValidationFailed:
        return UNREADABLE_TARGET
    return describe(selector)


def term_text(term: tuple[str, str | None]) -> str:
    """Render a label term as `key` or `key=value`, which is also the Engine's filter form."""
    key, value = term
    return key if value is None else f"{key}={value}"


def matches(selector: Selector, *, name: str, labels: Mapping[str, str]) -> bool:
    """Report whether a container with this name and these labels is one the selector names."""
    match selector:
        case ContainerTarget(names=wanted):
            return name in wanted
        case StackTarget(names=wanted):
            return any(in_project(labels, canonical_stack_key(each)) for each in wanted)
        case LabelsTarget(labels=terms):
            return carries(terms, labels)
        case EnvironmentTarget():
            return True
        case ServiceTarget() | ServiceLabelsTarget():
            return False


def carries(terms: Terms, labels: Mapping[str, str]) -> bool:
    """Report whether the labels hold every term, a term without a value matching its key alone."""
    return all(key in labels and (value is None or labels[key] == value) for key, value in terms)


async def find_container(client: EngineClient, name: str) -> ContainerSummary | None:
    """Return the container whose own name is exactly `name`, in any state, or None.

    The Engine's name filter is a pattern, so its answer is only a narrowing; the match is made
    on the names it returns.
    """
    try:
        rows = await client.containers.list(all=True, filters={"name": [f"^/{re.escape(name)}$"]})
    except EngineError as error:
        raise unavailable_from(error) from None
    wanted = f"/{name}"
    return next((row for row in rows if wanted in row.names), None)


async def find_containers(
    client: EngineClient, names: Sequence[str]
) -> list[tuple[str, ContainerSummary | None]]:
    """Return each name with the container it names, or None, in the order given."""
    return [(name, await find_container(client, name)) for name in names]


def only_name(selector: NamedSelector, name: str) -> NamedSelector:
    """Return the selector narrowed to the one name."""
    return selector.model_copy(update={"names": (name,)})


def compose_managed(name: str, labels: Mapping[str, str]) -> str | None:
    """Say that the container belongs to a Compose project, or None when it does not."""
    project = labels.get(COMPOSE_PROJECT_LABEL)
    if project is None:
        return None
    if SWARM_STACK_NAME.fullmatch(project) is None:
        return f"{name} is managed by a Compose project"
    return f"{name} is managed by the Compose project {project}"


async def find_service(
    client: EngineClient,
    name: str,
    environment_id: int,
    poke: Callable[[int], None] | None = None,
) -> SwarmService | None:
    """Return the service whose own name is exactly `name`, or None.

    The Engine's name filter matches a prefix, so its answer is only a narrowing; the match is
    made on the names it returns, and a row without a canonical id is never returned. A node that
    is not a usable manager raises SwarmUnavailable.
    """
    try:
        rows = await client.swarm_services.list(filters={"name": [name]})
    except EngineError as error:
        raise swarm_unavailable_from(error, environment_id, poke) from None
    return next(
        (row for row in rows if row.spec.name == name and is_canonical_id("service", row.id)),
        None,
    )


async def authorise_selector(
    principal: Principal,
    environment_id: int,
    selector: Selector,
    authoriser: Authoriser,
    hosts: HostClients,
    stacks: StackLookup,
    *,
    stack_kinds: frozenset[StackKind] = frozenset({"compose"}),
    refuse_compose_managed: bool = False,
) -> None:
    """Raise unless the principal may write to everything the selector names today.

    A container, stack or service that does not exist yet passes, so a rule can be made ahead of
    it, and so does one the principal cannot see, which is saved as if it did not exist.
    TargetUnverified when the host cannot say whether a container or service exists.
    ValidationFailed when a stack is of a kind not in `stack_kinds`, or, with
    `refuse_compose_managed`, a container belongs to a Compose project; each is said only to a
    principal who may write to the object. ValidationFailed also when a service, or services
    selected by labels, is the target in an environment that is not a Swarm manager.
    """
    match selector:
        case ContainerTarget(names=names):
            try:
                found = await find_containers(await hosts.client(environment_id), names)
            except EnvironmentUnavailable:
                raise TargetUnverified from None
            for name, container in found:
                if container is None or not await _may_read(
                    authoriser, principal, ResourceKey(environment_id, "container", container.id)
                ):
                    continue
                await authoriser.require_access(
                    principal, ResourceKey(environment_id, "container", container.id), "write"
                )
                managed = compose_managed(name, container.labels)
                if refuse_compose_managed and managed is not None:
                    raise ValidationFailed({SELECTOR_FIELD: _stack_instead(managed, container)})
        case StackTarget(names=names):
            for name in names:
                row = await stacks.get_folded(environment_id, name)
                if row is not None and await _may_read(
                    authoriser, principal, ResourceKey(environment_id, "stack", row.name)
                ):
                    await authoriser.require_access(
                        principal, ResourceKey(environment_id, "stack", row.name), "write"
                    )
                    if row.kind not in stack_kinds:
                        raise ValidationFailed({SELECTOR_FIELD: COMPOSE_ONLY})
        case ServiceTarget(names=names):
            if (await hosts.environment(environment_id)).type != "swarm":
                raise ValidationFailed({SELECTOR_FIELD: NOT_SWARM})
            for name in names:
                try:
                    client = await hosts.client(environment_id)
                    located = await find_service(client, name, environment_id)
                except SwarmUnavailable:
                    raise
                except EnvironmentUnavailable:
                    raise TargetUnverified from None
                if located is not None and await _may_read(
                    authoriser, principal, ResourceKey(environment_id, "service", located.id)
                ):
                    await authoriser.require_access(
                        principal, ResourceKey(environment_id, "service", located.id), "write"
                    )
        case ServiceLabelsTarget():
            if (await hosts.environment(environment_id)).type != "swarm":
                raise ValidationFailed({SELECTOR_FIELD: NOT_SWARM})
        case LabelsTarget() | EnvironmentTarget():
            return


async def _may_read(authoriser: Authoriser, principal: Principal, key: ResourceKey) -> bool:
    """Report whether the principal can see the object; one it cannot see is as good as absent."""
    try:
        await authoriser.require_access(principal, key, "read")
    except (PermissionDenied, ResourceNotFound):
        return False
    return True


def _stack_instead(managed: str, container: ContainerSummary) -> str:
    project = container.labels.get(COMPOSE_PROJECT_LABEL, "")
    if SWARM_STACK_NAME.fullmatch(project) is None:
        return f"{managed}. Choose its stack as the target instead."
    return f"{managed}. Choose the stack {project} as the target instead."


def _message(error: ValidationError) -> str:
    for item in error.errors():
        context = item.get("ctx")
        if item["type"] == "value_error" and context and "error" in context:
            return str(context["error"])
    return NOT_VALID
