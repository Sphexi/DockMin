"""The volumes a stack import created for a stack that has not been deployed yet."""

import io
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from app.auth.errors import ProductError
from app.docker_engine.models import Volume
from app.services.host_files import VolumeHelper

__all__ = [
    "RUN_LABEL",
    "ImportedVolumes",
    "MountNotEmpty",
    "created_by_this_call",
    "mount_point_is_empty",
]

MAX_STACKS: Final = 256
RUN_LABEL: Final = "dockmin.import"
EMPTY_READ_BYTES: Final = 64 * 1024


class MountNotEmpty(ProductError):
    """The directory a new volume is backed by already holds files, or could not be read."""

    reason = "mount_not_empty"

    def __init__(self, target: str) -> None:
        super().__init__(
            f"The directory or share {target} already holds files or could not be read as empty. "
            "Nothing is written onto existing files."
        )


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

    def holds(self, environment_id: int, stack_key: str, name: str) -> bool:
        """Report whether the stack's entry names this volume."""
        return name in self._volumes.get((environment_id, stack_key), {})

    def accepts(self, environment_id: int, stack_key: str, name: str, created_at: str) -> bool:
        """Report whether a volume that exists is one this import created, unchanged."""
        held = self._volumes.get((environment_id, stack_key))
        return held is not None and held.get(name) == created_at and bool(created_at)

    def release(self, environment_id: int, stack_key: str) -> None:
        """Forget the stack's entry, after its deployment succeeded or the stack was removed."""
        self._volumes.pop((environment_id, stack_key), None)


def created_by_this_call(volume: Volume, labels: Mapping[str, str]) -> bool:
    """Report whether Docker's answer to a create is the volume this call made.

    `labels` must include `RUN_LABEL` with a value unique to the run: Docker answers a create
    for an existing name with that volume instead of an error.
    """
    return all(volume.labels.get(key) == value for key, value in labels.items())


async def mount_point_is_empty(helper: VolumeHelper) -> bool:
    """Report whether the helper's volume holds nothing; an unreadable listing is not empty."""
    stream = await helper.stream()
    data = bytearray()
    try:
        async for chunk in stream:
            data += chunk
            if len(data) > EMPTY_READ_BYTES:
                return False
    finally:
        await stream.aclose()
    try:
        with tarfile.open(fileobj=io.BytesIO(bytes(data)), mode="r:") as archive:
            return len(archive.getmembers()) <= 1
    except tarfile.TarError:
        return False
