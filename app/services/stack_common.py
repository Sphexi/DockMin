"""Helpers the Compose and Swarm stack services share: control copies, change flags, host names."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import yaml

from app.auth.errors import ProductError
from app.db.engine import DatabaseError
from app.db.repositories.environment import Environment
from app.db.repositories.errors import RepositoryError
from app.db.repositories.resource_control import (
    ControlGrants,
    ResourceControl,
    ResourceControlRepository,
)
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, ResponseTooLarge
from app.docker_engine.models import ContainerSummary, Network, Volume
from app.services.access import (
    ResourceType,
    canonical_stack_key,
    is_canonical_id,
)
from app.services.containers import ContainerListTooLarge
from app.services.host_objects import NetworkListTooLarge, VolumeListTooLarge
from app.services.object_inspector import unavailable_from

__all__ = [
    "RECORDING_ERRORS",
    "StackControlSource",
    "changed_since_deploy",
    "concurrent",
    "copy_stack_control",
    "declared_service_images",
    "declared_service_keys",
    "load_stack_control",
    "read_labelled_containers",
    "read_labelled_names",
    "read_labelled_networks",
    "read_labelled_volumes",
    "stronger_reason",
    "swarm_namespaces_apply",
]


RECORDING_ERRORS: Final = (sqlite3.Error, DatabaseError, RepositoryError, ProductError)

_REASON_ORDER: Final = (
    "invalid",
    "restricted",
    "credential_missing",
    "credential_unavailable",
    "credential_choice_required",
)


def stronger_reason(current: str, candidate: str) -> str:
    """Return the reason that comes first in the order a refusal must be resolved in."""
    return min(current, candidate, key=_REASON_ORDER.index)


@dataclass(frozen=True, slots=True)
class StackControlSource:
    """A stack's control with its grants, read once to copy onto the objects that outlive it."""

    control: ResourceControl
    grants: ControlGrants


def changed_since_deploy(
    file_digest: str,
    deployed_file_digest: str | None,
    inputs_digest: str | None,
    deployed_inputs_digest: str | None,
) -> bool:
    """Report whether the stored file, variables or companion files differ from the last deploy."""
    return deployed_file_digest != file_digest or inputs_digest != deployed_inputs_digest


def declared_service_images(text: str) -> dict[str, str | None]:
    """Return each service the file declares with its image; empty when the file does not parse."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return {}
    declared: dict[str, str | None] = {}
    for key, service in services.items():
        if not isinstance(key, str):
            continue
        image = service.get("image") if isinstance(service, dict) else None
        declared[key] = image if isinstance(image, str) else None
    return declared


def declared_service_keys(text: str) -> list[str]:
    """Return the service keys the file declares; empty when the file does not parse."""
    return list(declared_service_images(text))


async def load_stack_control(
    repo: ResourceControlRepository, environment_id: int, name: str
) -> StackControlSource | None:
    """Read the stack's control and grants, or None when the stack has no control."""
    control = await repo.get_for(environment_id, "stack", name)
    if control is None:
        return None
    return StackControlSource(control, await repo.list_grants(control.id))


async def copy_stack_control(
    repo: ResourceControlRepository,
    environment_id: int,
    kind: ResourceType,
    object_id: str,
    source: StackControlSource,
) -> bool:
    """Give an object that outlives its stack a copy of the stack's control and grants.

    False, with nothing written, for an id the model does not key this type by and for an
    object that already has a control of its own. Runs inside the caller's transaction.
    """
    if not is_canonical_id(kind, object_id):
        return False
    if await repo.get_for(environment_id, kind, object_id) is not None:
        return False
    control = source.control
    inherited = await repo.create(
        environment_id,
        kind,
        object_id,
        is_public=control.is_public,
        is_administrators_only=control.is_administrators_only,
        access_level=control.access_level,
        owner_user_id=control.owner_user_id,
    )
    await repo.replace_grants(
        inherited.id, user_ids=source.grants.user_ids, team_ids=source.grants.team_ids
    )
    return True


def swarm_namespaces_apply(environment: Environment) -> bool:
    """Report whether the daemon reports Swarm and may have stack namespaces to collide with.

    A worker answers no Swarm list, so it is excluded; a manager the product has not yet seen
    in full is not.
    """
    if environment.type == "swarm":
        return True
    return environment.swarm_state == "active" and environment.swarm_control_available is not False


async def read_labelled_containers(client: EngineClient, label: str) -> list[ContainerSummary]:
    """Read every container, in any state, that carries the label."""
    try:
        return await client.containers.list(all=True, filters={"label": [label]})
    except ResponseTooLarge:
        raise ContainerListTooLarge() from None
    except EngineError as error:
        raise unavailable_from(error) from None


async def read_labelled_networks(client: EngineClient, label: str) -> list[Network]:
    """Read every network that carries the label."""
    try:
        return await client.networks.list(filters={"label": [label]})
    except ResponseTooLarge:
        raise NetworkListTooLarge() from None
    except EngineError as error:
        raise unavailable_from(error) from None


async def read_labelled_volumes(client: EngineClient, label: str) -> list[Volume]:
    """Read every volume visible on this host that carries the label."""
    try:
        return await client.volumes.list(filters={"label": [label]})
    except ResponseTooLarge:
        raise VolumeListTooLarge() from None
    except EngineError as error:
        raise unavailable_from(error) from None


class _Group:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[Any]] = []

    def create_task[T](self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


async def _cancel_and_settle(tasks: Sequence[asyncio.Task[Any]]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@asynccontextmanager
async def concurrent() -> AsyncIterator[_Group]:
    """Run tasks together; the first failure cancels the rest.

    Raises the failure of the earliest-created failed task as itself, and CancelledError when
    the calling task is being cancelled. Tasks are awaited when the block exits.
    """
    group = _Group()
    try:
        yield group
        if group.tasks:
            await asyncio.wait(group.tasks, return_when=asyncio.FIRST_EXCEPTION)
    except BaseException:
        await _cancel_and_settle(group.tasks)
        raise
    await _cancel_and_settle(group.tasks)
    for task in group.tasks:
        if not task.cancelled() and (error := task.exception()) is not None:
            raise error from None


def _folded(labelled: Sequence[ContainerSummary | Network | Volume], label: str) -> set[str]:
    return {canonical_stack_key(value) for item in labelled if (value := item.labels.get(label))}


async def read_labelled_names(
    client: EngineClient, labels: Sequence[str], *, network_labels: Sequence[str] = ()
) -> frozenset[str]:
    """Return the case-folded stack names the labels give containers, volumes and networks.

    `labels` are read on containers and volumes, `network_labels` on networks, all concurrently.
    Raises the list-too-large and unavailable errors rather than a partial answer, since a name
    left out lets a new stack collide with an object that exists.
    """
    reads: list[tuple[str, asyncio.Task[Sequence[ContainerSummary | Network | Volume]]]] = []
    async with concurrent() as group:
        for label in labels:
            reads.append((label, group.create_task(read_labelled_containers(client, label))))
            reads.append((label, group.create_task(read_labelled_volumes(client, label))))
        for label in network_labels:
            reads.append((label, group.create_task(read_labelled_networks(client, label))))
    found: set[str] = set()
    for label, read in reads:
        found |= _folded(read.result(), label)
    return frozenset(found)
