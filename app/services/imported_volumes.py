"""The volumes a stack import created for a stack that has not been deployed yet."""

from dataclasses import dataclass, field
from typing import Final

__all__ = ["ImportedVolumes"]

MAX_STACKS: Final = 256


@dataclass(slots=True)
class ImportedVolumes:
    """Binds each volume an import created to its name and creation time, per stack and environment.

    The first deployment of a stack with an entry here accepts a project volume that exists only
    when it is one of these, unchanged; the entry ends with that deployment, or when the stack is
    removed. It is held in memory, so a restart ends it.
    """

    _volumes: dict[tuple[int, str], dict[str, str]] = field(default_factory=dict)

    def register(self, environment_id: int, stack_key: str, volumes: dict[str, str]) -> None:
        """Record the volumes created for the stack, by name, with their creation times."""
        if len(self._volumes) >= MAX_STACKS and (environment_id, stack_key) not in self._volumes:
            self._volumes.pop(next(iter(self._volumes)))
        self._volumes[(environment_id, stack_key)] = dict(volumes)

    def registered(self, environment_id: int, stack_key: str) -> bool:
        """Report whether the stack has volumes bound by an import."""
        return (environment_id, stack_key) in self._volumes

    def accepts(self, environment_id: int, stack_key: str, name: str, created_at: str) -> bool:
        """Report whether a volume that exists is one this import created, unchanged."""
        held = self._volumes.get((environment_id, stack_key))
        return held is not None and held.get(name) == created_at and bool(created_at)

    def release(self, environment_id: int, stack_key: str) -> None:
        """Forget the stack's entry, after its deployment succeeded or the stack was removed."""
        self._volumes.pop((environment_id, stack_key), None)
