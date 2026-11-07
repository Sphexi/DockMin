"""What a dismissal is compared against: object tokens, member digests and the leftovers shown."""

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from re import Pattern
from typing import Final

from app.db.repositories.stack_dismissal import StoredDismissal
from app.docker_engine.models import (
    ContainerSummary,
    Network,
    SwarmConfig,
    SwarmSecret,
    SwarmService,
    Volume,
)
from app.services.object_inspector import NETWORK_ID, VOLUME_OR_NETWORK_NAME

__all__ = [
    "DISMISSIBLE_NAME",
    "MAX_LEFTOVERS_SHOWN",
    "DismissalView",
    "HostMembers",
    "LeftoverObject",
    "LeftoverVolume",
    "applies",
    "compose_members",
    "config_token",
    "dismissal_view",
    "leftover_objects",
    "leftover_volumes",
    "members_digest",
    "network_token",
    "secret_token",
    "swarm_members",
    "volume_token",
]

DISMISSIBLE_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
MAX_LEFTOVERS_SHOWN: Final = 20
MAX_LEFTOVER_LABEL: Final = 128


@dataclass(frozen=True, slots=True)
class LeftoverVolume:
    """A volume a stack entry left behind; `linkable` is false for a name no page can open."""

    name: str
    label: str
    created_at: str
    linkable: bool


@dataclass(frozen=True, slots=True)
class LeftoverObject:
    """A network, config or secret a stack entry left behind.

    `linkable` is false for an id no page can open.
    """

    id: str
    label: str
    linkable: bool


@dataclass(frozen=True, slots=True)
class DismissalView:
    """A stored dismissal as shown; `dismissed_by` is None once the user is gone.

    `applies` is false when the entry holds a container, a service or an object the dismissal
    did not name, which shows the entry again and flags it as changed.
    """

    dismissed_at: str
    dismissed_by: str | None
    applies: bool

    @property
    def changed_since(self) -> bool:
        """Report whether the entry no longer matches what was dismissed."""
        return not self.applies


@dataclass(frozen=True, slots=True)
class HostMembers:
    """What a project or namespace holds on the host: its workloads and its dismissal tokens.

    `workloads` counts containers for a Compose project and services for a Swarm namespace.
    `tokens` are sorted and unique.
    """

    workloads: int
    tokens: tuple[str, ...]

    @property
    def present(self) -> bool:
        """Report whether the host holds anything under the name."""
        return self.workloads > 0 or bool(self.tokens)


def volume_token(volume: Volume) -> str:
    """Return the token that names one volume as it was created."""
    return f"volume:{volume.name}:{volume.created_at}"


def network_token(network: Network) -> str:
    """Return the token that names one network."""
    return f"network:{network.id}"


def config_token(config: SwarmConfig) -> str:
    """Return the token that names one Swarm config."""
    return f"config:{config.id}"


def secret_token(secret: SwarmSecret) -> str:
    """Return the token that names one Swarm secret."""
    return f"secret:{secret.id}"


def members_digest(ids: Iterable[str]) -> str:
    """Return the SHA-256 over the sorted ids or tokens, joined by newlines."""
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


def compose_members(
    containers: Sequence[ContainerSummary], volumes: Sequence[Volume], networks: Sequence[Network]
) -> HostMembers:
    """Describe one Compose project from what the host reports under its name."""
    tokens = {*map(volume_token, volumes), *map(network_token, networks)}
    return HostMembers(len(containers), tuple(sorted(tokens)))


def swarm_members(
    services: Sequence[SwarmService],
    configs: Sequence[SwarmConfig],
    secrets: Sequence[SwarmSecret],
    networks: Sequence[Network],
) -> HostMembers:
    """Describe one Swarm namespace from what the manager reports under its name."""
    tokens = {
        *map(config_token, configs),
        *map(secret_token, secrets),
        *map(network_token, networks),
    }
    return HostMembers(len(services), tuple(sorted(tokens)))


def applies(dismissal: StoredDismissal, members: HostMembers) -> bool:
    """Report whether the dismissal still covers the entry: no workload and no unnamed object."""
    return members.workloads == 0 and set(members.tokens) <= set(dismissal.objects)


def dismissal_view(dismissal: StoredDismissal | None, members: HostMembers) -> DismissalView | None:
    """Return how a stored dismissal stands against the entry, or None when there is none."""
    if dismissal is None:
        return None
    return DismissalView(
        dismissed_at=dismissal.dismissed_at,
        dismissed_by=dismissal.dismissed_by,
        applies=applies(dismissal, members),
    )


def leftover_volumes(
    volumes: Iterable[Volume], limit: int = MAX_LEFTOVERS_SHOWN
) -> tuple[LeftoverVolume, ...]:
    """Return the volumes to show, by name, at most `limit`."""
    ordered = sorted(volumes, key=lambda volume: volume.name)
    return tuple(
        LeftoverVolume(
            volume.name,
            volume.name[:MAX_LEFTOVER_LABEL],
            volume.created_at,
            VOLUME_OR_NETWORK_NAME.fullmatch(volume.name) is not None,
        )
        for volume in ordered[:limit]
    )


def leftover_objects(
    objects: Iterable[Network | SwarmConfig | SwarmSecret],
    id_grammar: Pattern[str] = NETWORK_ID,
) -> tuple[LeftoverObject, ...]:
    """Return the networks, configs or secrets to show, by name, at most MAX_LEFTOVERS_SHOWN.

    An object is linkable when its id matches `id_grammar`.
    """
    ordered = sorted(objects, key=lambda item: (item.name.casefold(), item.id))
    return tuple(
        LeftoverObject(
            item.id, item.name[:MAX_LEFTOVER_LABEL], id_grammar.fullmatch(item.id) is not None
        )
        for item in ordered[:MAX_LEFTOVERS_SHOWN]
    )
