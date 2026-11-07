"""The Swarm stack operation outcome and the pure plan-line and name helpers."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.db.repositories.stack import (
    StackStatus,
)
from app.docker_engine.errors import Conflict, EngineError
from app.services.access import (
    ResourceType,
    is_canonical_id,
)
from app.services.object_locks import ObjectKey
from app.services.swarm_common import PinnedImage
from app.services.swarm_stack_plan import (
    PlanLine,
)

__all__ = [
    "SwarmStackOutcome",
    "counts_of",
    "force_update_counter",
    "in_use",
    "member_keys",
    "rebinds_credential_set",
    "resource_type",
]

IN_USE_STATUSES: Final = frozenset({400, 403})


@dataclass(frozen=True, slots=True)
class SwarmStackOutcome:
    """What a deploy, redeploy or remove did; names are `kind:name`, the message is ours."""

    operation: str
    environment_id: int
    name: str
    message: str
    status: StackStatus | None
    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    unpinned: tuple[str, ...] = ()
    failed_object: str | None = None
    interrupted: bool = False
    volumes_kept: int = 0
    controls_copied: int = 0
    controls_released: int = 0


def force_update_counter(spec: Mapping[str, Any]) -> int:
    task = spec.get("TaskTemplate")
    value = task.get("ForceUpdate") if isinstance(task, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def in_use(error: EngineError) -> bool:
    return isinstance(error, Conflict) or error.status_code in IN_USE_STATUSES


def _lockable(environment_id: int, line: PlanLine) -> ObjectKey | None:
    if line.object_id is None:
        return None
    if line.action == "keep" and line.kind in ("service", "network"):
        return None
    if not is_canonical_id(line.kind, line.object_id):
        return None
    return ObjectKey(environment_id, line.kind, line.object_id)


def member_keys(environment_id: int, lines: Sequence[PlanLine]) -> list[ObjectKey]:
    return [key for line in lines if (key := _lockable(environment_id, line)) is not None]


def counts_of(removed: Sequence[str]) -> dict[str, int]:
    return {
        f"{kind}s": sum(1 for item in removed if item.startswith(f"{kind}:"))
        for kind in ("service", "network", "config", "secret")
    }


def rebinds_credential_set(pinned: PinnedImage | None) -> bool:
    """Report whether the caller chose a credential set other than the one the image is bound to."""
    if pinned is None or pinned.credentials.source != "choice":
        return False
    seen = pinned.credentials.seen
    return pinned.credentials.credential_set_id != (None if seen is None else seen.set_id)


def resource_type(kind: str) -> ResourceType | None:
    match kind:
        case "service" | "network" | "config" | "secret":
            return kind
    return None
