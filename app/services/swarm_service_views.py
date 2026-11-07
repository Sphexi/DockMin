"""Swarm service errors, view types and operation outcomes."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from app.auth.errors import (
    ProductError,
    ValidationFailed,
)
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.pagination import Page
from app.services.container_restrictions import Finding, refused_options
from app.services.containers import (
    LabelView,
)
from app.services.service_spec import (
    MAX_REPLICAS,
    ServiceInput,
)
from app.services.swarm_common import (
    PinnedImage,
)

__all__ = [
    "MAX_VERIFY_SECONDS",
    "EnvView",
    "FileRef",
    "ForceImpact",
    "MountView",
    "NetworkUse",
    "NoPreviousSpec",
    "NodeCount",
    "NotReplicated",
    "Plan",
    "PortView",
    "RemoveImpact",
    "Resolved",
    "RestrictedService",
    "RollbackImpact",
    "ScaleImpact",
    "ServiceChangedSinceReview",
    "ServiceCreated",
    "ServiceCredentialsRefreshed",
    "ServiceDetail",
    "ServiceEditOffer",
    "ServiceFilters",
    "ServiceForceUpdated",
    "ServiceGone",
    "ServiceImageUpdated",
    "ServiceInvalid",
    "ServiceNameInUse",
    "ServicePage",
    "ServiceRefused",
    "ServiceRemoved",
    "ServiceRolledBack",
    "ServiceRow",
    "ServiceScaled",
    "ServiceUpdateFailed",
    "ServiceUpdated",
    "ServiceVerifyPolicy",
    "StateCount",
    "UpdateReview",
    "UpdateStatusView",
    "Verdict",
]

CHANGED_SINCE_REVIEW: Final = "This service changed since you reviewed it; nothing was changed."


MAX_VERIFY_SECONDS: Final = 300


_UPDATE_FAILURES: Final = {
    "update_paused": "The service update paused.",
    "tasks_not_running": "Fewer tasks are running than before the update.",
    "rolled_back_by_swarm": "Swarm rolled the service update back.",
    "rollback_paused": "Swarm paused while rolling the service update back.",
    "service_gone": "The service no longer exists.",
    "unverifiable": "The service update could not be verified.",
    "update_in_progress": "The service is in the middle of a rolling update.",
    "legacy_networks": "The service uses a legacy network setting that cannot be sent unchanged.",
}


class ServiceGone(ProductError):
    """The service vanished between authorisation and the Engine call."""

    def __init__(self) -> None:
        super().__init__("The service no longer exists.")


class ServiceNameInUse(ValidationFailed):
    """A service with the requested name already exists."""

    def __init__(self) -> None:
        super().__init__({"name": "A service with this name already exists."})


class ServiceChangedSinceReview(ValidationFailed):
    """The service is no longer the one the caller reviewed; nothing was changed."""

    def __init__(self) -> None:
        super().__init__({"service": CHANGED_SINCE_REVIEW})


class NotReplicated(ProductError):
    """Only a replicated service has a replica count to change."""

    def __init__(self) -> None:
        super().__init__("Only a service in replicated mode can be scaled.")


class NoPreviousSpec(ProductError):
    """The service has no earlier configuration to return to."""

    def __init__(self) -> None:
        super().__init__("This service has no previous configuration to roll back to.")


class ServiceRefused(ProductError):
    """Docker rejected the service configuration; the Engine's text is not part of the message.

    engine_detail is that text, scrubbed and cut, for a page shown to an administrator.
    """

    def __init__(self, engine_detail: str | None = None) -> None:
        super().__init__("Docker refused this service configuration.")
        self.engine_detail = engine_detail


class ServiceUpdateFailed(ProductError):
    """A rolling update, or the check that it ended well, failed; the message is fixed text.

    `update_state` is the state last read and `version` the service version read with it.
    """

    def __init__(self, reason: str, update_state: str | None, version: int | None) -> None:
        super().__init__(_UPDATE_FAILURES.get(reason, "The service update failed."))
        self.reason = reason
        self.update_state = update_state
        self.version = version


class ServiceInvalid(ValidationFailed):
    """A service request refused with named fields; reason and detail go on the failure row."""

    def __init__(
        self,
        fields: Mapping[str, str],
        *,
        reason: str = "invalid",
        detail: Mapping[str, DetailValue] | None = None,
    ) -> None:
        super().__init__(dict(fields))
        self.reason = reason
        self.detail: dict[str, DetailValue] = dict(detail or {})


class RestrictedService(ServiceInvalid):
    """A standard user's service uses options only administrators may set."""

    def __init__(self, findings: Sequence[Finding], fields: Mapping[str, str]) -> None:
        self.findings = tuple(findings)
        self.refused = refused_options(findings)
        super().__init__(fields, reason="restricted", detail={"refused": ",".join(self.refused)})


@dataclass(frozen=True, slots=True)
class PortView:
    """A published port."""

    target: int
    published: int | None
    protocol: str
    mode: str


@dataclass(frozen=True, slots=True)
class EnvView:
    """An environment variable; value is None when it is withheld from the viewer."""

    key: str
    value: str | None
    redacted: bool


@dataclass(frozen=True, slots=True)
class MountView:
    """A mount; the source of a bind mount is shown to administrators only."""

    type: str
    source: str | None
    target: str
    read_only: bool


@dataclass(frozen=True, slots=True)
class NetworkUse:
    """A network the service joins; name is None when the viewer may not see the network."""

    target: str
    name: str | None
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FileRef:
    """A config or secret exposed in the containers; linkable when the viewer may read it."""

    object_id: str
    name: str
    target: str
    uid: str
    gid: str
    mode: str
    linkable: bool


@dataclass(frozen=True, slots=True)
class UpdateStatusView:
    """A rolling update's state; the message is the Engine's text after cleaning and cutting."""

    state: str
    message: str
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ServiceRow:
    """One service in the list."""

    environment_id: int
    id: str
    short_id: str
    name: str
    mode: str
    replicas: int | None
    running: int | None
    desired: int | None
    image: str
    image_display: str
    stack: str | None
    ports: tuple[PortView, ...]
    updated_at: datetime | None
    update_state: str | None
    can_write: bool


@dataclass(frozen=True, slots=True)
class ServiceFilters:
    """List filters; empty values filter nothing."""

    q: str | None = None
    mode: str | None = None
    stack: str | None = None


@dataclass(frozen=True, slots=True)
class ServicePage:
    """One page of services the caller may see."""

    page: Page[ServiceRow]
    total: int
    visible: int
    limit: int
    environment_name: str
    is_local: bool
    can_create: bool


@dataclass(frozen=True, slots=True)
class ServiceDetail:
    """One service as the detail page shows it, redacted for the viewer."""

    environment_id: int
    environment_name: str
    is_local: bool
    id: str
    short_id: str
    name: str
    created_at: datetime | None
    updated_at: datetime | None
    version: int
    mode: str
    replicas: int | None
    running: int | None
    desired: int | None
    image: str
    image_display: str
    digest_pinned: bool
    command: str
    stack: str | None
    labels: tuple[LabelView, ...]
    env: tuple[EnvView, ...]
    ports: tuple[PortView, ...]
    endpoint_mode: str
    constraints: tuple[str, ...]
    preferences: tuple[str, ...]
    max_replicas_per_node: int | None
    resources: tuple[tuple[str, str], ...]
    restart: tuple[tuple[str, str], ...]
    update_config: tuple[tuple[str, str], ...]
    rollback_config: tuple[tuple[str, str], ...]
    networks: tuple[NetworkUse, ...]
    mounts: tuple[MountView, ...]
    configs: tuple[FileRef, ...]
    secrets: tuple[FileRef, ...]
    update_status: UpdateStatusView | None
    previous_image: str | None
    force_counter: int
    can_write: bool
    can_scale: bool
    can_roll_back: bool
    can_manage_access: bool
    replicas_maximum: int = MAX_REPLICAS


@dataclass(frozen=True, slots=True)
class ServiceEditOffer:
    """The edit form: the stored spec as form values, with every withheld value left empty."""

    environment_id: int
    environment_name: str
    service_id: str
    name: str
    version: int
    mode: str
    values: Mapping[str, str]
    administrator: bool


@dataclass(frozen=True, slots=True)
class UpdateReview:
    """What an edit would change, for the review page; values are never listed."""

    environment_id: int
    environment_name: str
    service_id: str
    name: str
    version: int
    categories: tuple[str, ...]
    image_before: str | None
    image_after: str | None
    image_changed: bool
    tasks_replaced: bool
    running_tasks: int
    restricted: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NodeCount:
    """Running tasks of a service on one node."""

    hostname: str
    tasks: int


@dataclass(frozen=True, slots=True)
class ScaleImpact:
    """What changing the replica count does."""

    environment_id: int
    environment_name: str
    service_id: str
    name: str
    version: int
    current: int
    requested: int
    stops: int
    confirm_required: bool


@dataclass(frozen=True, slots=True)
class ForceImpact:
    """The running tasks a forced update replaces."""

    environment_id: int
    environment_name: str
    service_id: str
    name: str
    version: int
    running_tasks: int
    nodes: tuple[NodeCount, ...]
    nodes_more: int


@dataclass(frozen=True, slots=True)
class RollbackImpact:
    """What returning to the previous configuration changes."""

    environment_id: int
    environment_name: str
    service_id: str
    name: str
    version: int
    categories: tuple[str, ...]
    image_before: str | None
    image_after: str | None
    running_tasks: int


@dataclass(frozen=True, slots=True)
class StateCount:
    """Tasks of a service in one state."""

    state: str
    tasks: int


@dataclass(frozen=True, slots=True)
class RemoveImpact:
    """What removing the service takes away; its containers go, its volumes stay."""

    environment_id: int
    environment_name: str
    service_id: str
    name: str
    version: int
    mode: str
    replicas: int | None
    states: tuple[StateCount, ...]
    nodes: tuple[NodeCount, ...]
    nodes_more: int
    ports: tuple[PortView, ...]
    stack: str | None


@dataclass(frozen=True, slots=True)
class ServiceCreated:
    """The outcome of a create; warning is True when the image could not be pinned."""

    environment_id: int
    service_id: str
    name: str
    digest_pinned: bool
    warning: str | None
    control_created: bool


@dataclass(frozen=True, slots=True)
class ServiceUpdated:
    """The outcome of an update; unchanged when the form changed nothing."""

    environment_id: int
    service_id: str
    name: str
    categories: tuple[str, ...]
    digest_pinned: bool
    warning: str | None
    unchanged: bool


@dataclass(frozen=True, slots=True)
class ServiceScaled:
    """The outcome of a scale."""

    environment_id: int
    service_id: str
    name: str
    replicas_before: int
    replicas_after: int


@dataclass(frozen=True, slots=True)
class ServiceForceUpdated:
    """The outcome of a forced update."""

    environment_id: int
    service_id: str
    name: str
    tasks_running: int


@dataclass(frozen=True, slots=True)
class ServiceRolledBack:
    """The outcome of a rollback."""

    environment_id: int
    service_id: str
    name: str
    categories: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ServiceImageUpdated:
    """The outcome of an image update; `verified` is None when nothing was sent."""

    environment_id: int
    service_id: str
    name: str
    image_before: str
    image_after: str
    verified: bool | None
    update_state: str | None
    version: int | None


@dataclass(frozen=True, slots=True)
class ServiceCredentialsRefreshed:
    """The outcome of renewing the registry credential stored with a service."""

    environment_id: int
    service_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ServiceVerifyPolicy:
    """How long, and how often, to watch a rollout after an image update."""

    seconds: int
    interval: float = 2.0

    def __post_init__(self) -> None:
        """Refuse a window or interval outside what the verification loop is bounded for."""
        if not 0 <= self.seconds <= MAX_VERIFY_SECONDS:
            raise ValueError(f"seconds must be between 0 and {MAX_VERIFY_SECONDS}")
        if self.interval <= 0:
            raise ValueError("interval must be positive")


@dataclass(frozen=True, slots=True)
class Verdict:
    verified: bool
    update_state: str | None
    version: int | None


@dataclass(frozen=True, slots=True)
class ServiceRemoved:
    """The outcome of a removal."""

    environment_id: int
    service_id: str
    name: str
    control_released: bool


@dataclass(frozen=True, slots=True)
class Resolved:
    networks: dict[int, str] = field(default_factory=dict)
    configs: dict[int, tuple[str, str]] = field(default_factory=dict)
    secrets: dict[int, tuple[str, str]] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Plan:
    service: ServiceInput
    spec: dict[str, Any]
    findings: tuple[Finding, ...]
    image_changed: bool
    pinned: PinnedImage | None
    manager_node_id: str | None
